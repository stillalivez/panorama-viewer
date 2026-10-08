import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
from urllib.request import urlopen

from PIL import Image

from desktop_viewer import DesktopAPI, DesktopSession, SettingsStore, ViewerServer, clean_view, file_id
from build_desktop import copy_viewer_resources

ROOT = Path(__file__).resolve().parents[1]


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)
        self.viewer = self.root / 'viewer'
        (self.viewer / 'assets').mkdir(parents=True)
        self.picture = self.root / '中文全景照片.jpg'
        shutil.copy2(ROOT / 'tests/fixtures/exif-date.jpg', self.picture)
        shutil.copy2(self.picture, self.viewer / 'assets/default.jpg')
        shutil.copy2(ROOT / 'assets/app-icon.ico', self.viewer / 'assets/app-icon.ico')
        config = dict(title='桌面测试', generated_at='2026-09-08', scenes=[
            dict(id='builtin', label='内置', image='./assets/default.jpg', thumbnail='./assets/default.jpg')])
        (self.viewer / 'viewer_config.json').write_text(json.dumps(config), encoding='utf-8')
        self.store = SettingsStore(self.root / 'profile')
        self.session = DesktopSession(self.viewer, self.store)

    def tearDown(self):
        self.work.cleanup()

    def test_unicode_image_import_uses_original_and_exif(self):
        before = hashlib.sha256(self.picture.read_bytes()).digest()
        result = self.session.add_files([self.picture])
        scene = result['scenes'][0]
        self.assertEqual(scene['source_path'], str(self.picture))
        self.assertEqual(scene['capture_time'], '2020-01-02 03:04:05')
        self.assertIsNone(scene['heading_degrees'])
        self.assertEqual(hashlib.sha256(self.picture.read_bytes()).digest(), before)
        self.assertEqual(self.store.data['open_files'], [str(self.picture)])

    def test_mixed_batch_keeps_valid_rejects_bad_and_square(self):
        bad = self.root / 'broken.jpg'
        bad.write_bytes(b'invalid')
        square = self.root / 'square.png'
        Image.new('RGB', (80, 80)).save(square)
        result = self.session.add_files([bad, self.picture, square])
        self.assertEqual(len(result['scenes']), 1)
        self.assertEqual(len(result['failures']), 2)
        self.assertEqual(len(self.session.external), 1)

    def test_duplicate_file_selects_existing_scene(self):
        self.session.add_files([self.picture])
        result = self.session.add_files([self.picture])
        self.assertEqual(result['duplicates'], 1)
        self.assertFalse(result['scenes'])
        self.assertEqual(result['selected'], file_id(self.picture))
        self.assertEqual(len(result['recent']), 1)

    def test_view_and_zero_calibration_survive_reopen(self):
        self.session.add_files([self.picture])
        sid = file_id(self.picture)
        self.session.save_session(dict(sequence=1, current_scene_id=sid,
                                 views={sid: dict(yaw=.75, pitch=-.5, fov=42, manual_heading=0)}))
        restored = DesktopSession(self.viewer, SettingsStore(self.store.directory))
        restored.restore()
        self.assertEqual(restored.scenes[sid]['saved_view']['fov'], 42)
        self.assertEqual(restored.scenes[sid]['manual_heading'], 0)
        self.assertEqual(restored.html_config()['desktop_current'], sid)

    def test_stale_save_cannot_overwrite_newer_view(self):
        self.session.save_session(dict(sequence=3, current_scene_id='builtin', views={'builtin': dict(fov=40)}))
        result = self.session.save_session(dict(sequence=2, current_scene_id='builtin', views={'builtin': dict(fov=90)}))
        self.assertFalse(result['saved'])
        self.assertEqual(self.store.data['views']['builtin']['fov'], 40)
        self.assertEqual(self.session.html_config()['desktop_sequence'], 3)

    def test_removed_scene_can_be_reopened_without_deleting_photo(self):
        self.session.add_files([self.picture])
        sid = file_id(self.picture)
        self.assertTrue(self.session.remove_file(sid)['removed'])
        self.assertTrue(self.picture.exists())
        self.assertFalse(self.store.data['open_files'])
        self.assertEqual(len(self.session.recent()), 1)
        self.assertEqual(len(self.session.add_files([self.picture])['scenes']), 1)

    def test_missing_restored_file_keeps_builtin_usable(self):
        self.store.data['open_files'] = [str(self.root / 'moved.jpg')]
        self.session.restore()
        self.assertEqual(list(self.session.scenes), ['builtin'])
        self.assertTrue(self.session.startup_messages)

    def test_corrupt_settings_keeps_backup(self):
        self.store.path.write_text('{broken', encoding='utf-8')
        new = SettingsStore(self.store.directory)
        self.assertTrue(new.warning)
        backups = list(self.store.directory.glob('settings-invalid-*.json'))
        self.assertEqual(backups[0].read_text(encoding='utf-8'), '{broken')
        new.write()
        self.assertEqual(json.loads(new.path.read_text(encoding='utf-8'))['version'], 1)

    def test_failed_settings_replace_preserves_previous_file(self):
        self.store.write()
        original = self.store.path.read_bytes()
        self.store.data['current_scene_id'] = 'changed'
        with patch.object(Path, 'replace', side_effect=OSError('disk blocked')):
            with self.assertRaises(OSError):
                self.store.write()
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_settings_values_are_bounded_and_finite(self):
        result = clean_view(dict(yaw=float('nan'), pitch=99, fov=999, manual_heading=False))
        self.assertEqual(result, dict(yaw=0, pitch=1.562, fov=100, manual_heading=None))

    def test_recent_history_is_bounded_and_clear_does_not_remove_open_files(self):
        for n in range(23):
            path = self.root / f'{n}.jpg'
            shutil.copy2(self.picture, path)
            self.session.add_files([path])
        self.assertEqual(len(self.session.recent()), 20)
        before = copy.deepcopy(self.store.data['open_files'])
        self.session.clear_recent()
        self.assertEqual(self.session.recent(), [])
        self.assertEqual(self.store.data['open_files'], before)

    def test_image_service_only_serves_registered_files_and_stops(self):
        server = ViewerServer(self.session, ROOT / 'panorama_viewer_template.html')
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        try:
            with urlopen(server.url) as response:
                html = response.read().decode('utf-8')
                self.assertIn('"desktop":true', html)
            image_url = server.url.rsplit('/', 1)[0] + self.session.scenes['builtin']['image'][1:]
            with urlopen(image_url) as response:
                self.assertEqual(response.read(), (self.viewer / 'assets/default.jpg').read_bytes())
            for suffix in ['/index.html', f'/{server.token}/../settings.json', f'/{server.token}/media/unregistered']:
                with self.assertRaises(HTTPError) as error:
                    urlopen(server.origin + suffix)
                self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_native_fullscreen_is_idempotent_and_restores_maximized(self):
        api = DesktopAPI(self.session)
        api._window = Mock()
        self.store.data['window']['maximized'] = True
        api.set_fullscreen(True)
        api.set_fullscreen(True)
        self.assertEqual(api._window.toggle_fullscreen.call_count, 1)
        api.set_fullscreen(False)
        self.assertEqual(api._window.toggle_fullscreen.call_count, 2)
        api._window.maximize.assert_called_once()
        self.assertFalse(api._fullscreen)

    def test_native_drop_event_opens_selected_path_and_reports_missing_path(self):
        api = DesktopAPI(self.session)
        api._notify = Mock()
        api._accept_drop({'dataTransfer': {'files': [{'name': self.picture.name, 'pywebviewFullPath': str(self.picture)}]}})
        self.assertEqual(api._notify.call_args.args[0]['selected'], file_id(self.picture))
        api._accept_drop({'dataTransfer': {'files': [{'name': 'unknown.jpg'}]}})
        self.assertTrue(api._notify.call_args.args[0]['failures'])

    def test_fresh_source_checkout_starts_empty_and_accepts_images(self):
        session = DesktopSession(self.root / 'not-generated', self.store)
        self.assertEqual(session.html_config()['scenes'], [])
        self.assertFalse((self.root / 'not-generated').exists())
        result = session.add_files([self.picture])
        self.assertEqual(len(result['scenes']), 1)

    def test_public_resources_exclude_existing_private_photos(self):
        # The checkout can contain personal viewer output; public builds must ignore it.
        resources = self.root / 'public-resources'
        copy_viewer_resources(resources, public=True)
        config = json.loads((resources / 'viewer/viewer_config.json').read_text(encoding='utf-8'))
        self.assertEqual(config['scenes'], [])
        self.assertEqual({str(p.relative_to(resources)).replace('\\', '/') for p in resources.rglob('*') if p.is_file()},
                         {'panorama_viewer_template.html', 'viewer/viewer_config.json', 'viewer/assets/app-icon.ico'})

    def test_invalid_existing_config_is_not_silently_replaced(self):
        (self.viewer / 'viewer_config.json').write_text('{bad', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            DesktopSession(self.viewer, self.store)


if __name__ == '__main__':
    unittest.main()
