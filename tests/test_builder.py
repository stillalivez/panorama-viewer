import json
import hashlib
import sys
import tempfile
import unittest
import os
import threading
import urllib.request
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_panorama_viewer as builder
import start_panorama_viewer as launcher


class BuilderTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / 'outputs' / 'test-runs'
        parent.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'photo.jpg'
        exif = Image.Exif()
        exif[36867] = '2020:01:02 03:04:05'
        Image.new('RGB', (128, 64), 'blue').save(self.source, exif=exif)
        self.output = self.root / 'viewer'
        self.kwargs = dict(labels=['first', 'second'], output_dir=self.output,
                           title='全景', subtitle='测试', max_width=0, seam_blend=0,
                           auto_seam_shift=False, seam_shift_threshold=1.45,
                           thumbnail_width=64, quality=80)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, images):
        return builder.build_viewer(images, **self.kwargs)

    def snapshot(self):
        return {p.relative_to(self.output): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.output.rglob('*') if p.is_file()}

    def test_exif_date_precedes_filename_and_file_times(self):
        self.assertEqual(builder._capture_text(self.source)[0], '2020-01-02 03:04:05')

    def test_no_metadata_is_unknown(self):
        plain = self.root / 'plain.jpg'
        Image.new('RGB', (128, 64)).save(plain)
        self.assertEqual(builder._capture_text(plain)[0], '拍摄时间未知')

    def test_filename_date_and_invalid_date(self):
        self.assertEqual(builder._capture_text(Path('DJI_20240229123456.jpg'))[0], '2024-02-29 12:34:56')
        self.assertEqual(builder._capture_text(Path('DJI_20240230123456.jpg'))[0], '拍摄时间未知')

    def test_nonfinite_headings_are_unknown(self):
        for value in ['nan', 'inf', '-inf', '', None]:
            self.assertIsNone(builder._parse_float(value))
        self.assertEqual(builder._parse_float('0'), 0)

    def test_bad_second_file_does_not_modify_existing_viewer(self):
        self.build([self.source, self.source])
        before = self.snapshot()
        Image.new('RGB', (128, 64), 'red').save(self.source)
        broken = self.root / 'broken.jpg'
        broken.write_bytes(b'broken')
        with self.assertRaises(Exception):
            self.build([self.source, broken])
        self.assertEqual(self.snapshot(), before)

    def test_disk_failure_does_not_modify_existing_viewer(self):
        self.build([self.source])
        before = self.snapshot()
        with patch.object(builder, '_save_thumb', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.build([self.source])
        self.assertEqual(self.snapshot(), before)

    def test_publish_failure_rolls_back(self):
        self.build([self.source])
        before = self.snapshot()
        original = Path.rename
        def fail_new_directory(path, target):
            if Path(target) == self.output and path.name == 'staged':
                raise OSError('publish blocked')
            return original(path, target)
        with patch.object(Path, 'rename', fail_new_directory):
            with self.assertRaises(OSError):
                self.build([self.source])
        self.assertEqual(self.snapshot(), before)

    def test_rejects_non_panorama_without_changing_output(self):
        self.build([self.source])
        before = self.snapshot()
        square = self.root / 'square.jpg'
        Image.new('RGB', (64, 64)).save(square)
        with self.assertRaisesRegex(ValueError, '2:1'):
            self.build([square])
        self.assertEqual(self.snapshot(), before)

    def test_empty_viewer_build_and_assets(self):
        self.build([])
        config = json.loads((self.output / 'viewer_config.json').read_text(encoding='utf-8'))
        self.assertEqual(config['scene_count'], 0)
        self.assertTrue((self.output / 'assets/app-icon.ico').exists())

    def test_preserved_png_keeps_correct_extension(self):
        source = self.root / 'pano.png'
        Image.new('RGB', (128, 64)).save(source)
        self.build([source])
        config = json.loads((self.output / 'viewer_config.json').read_text(encoding='utf-8'))
        self.assertTrue(config['scenes'][0]['image'].endswith('.png'))
        with Image.open(self.output / config['scenes'][0]['image']) as image:
            self.assertEqual(image.format, 'PNG')

    def test_preserves_unrelated_output_files(self):
        self.build([self.source])
        (self.output / 'notes.txt').write_text('keep', encoding='utf-8')
        self.build([self.source])
        self.assertEqual((self.output / 'notes.txt').read_text(encoding='utf-8'), 'keep')

    def test_source_inside_output_can_be_rebuilt(self):
        self.build([self.source])
        source = self.output / 'assets/panoramas/1-first.jpg'
        expected = source.read_bytes()
        self.build([source])
        self.assertEqual(source.read_bytes(), expected)

    def test_template_missing_is_explicit(self):
        with patch.object(builder, 'TEMPLATE_PATH', self.root / 'missing.html'):
            with self.assertRaises(FileNotFoundError):
                builder._render_html({'scenes': []}, 'x', 'y', 'z')

    def test_render_preserves_placeholder_like_labels(self):
        rendered = builder._render_html({'scenes': [{'label': '__TITLE__ </script>'}]}, 'title', 'sub', 'now')
        self.assertIn('__TITLE__ <\\/script>', rendered)

    def test_default_images_reuse_existing_generated_scenes(self):
        with patch.object(builder, 'DEFAULT_OUTPUT_DIR', self.output):
            self.assertEqual(builder._default_images(), [])
            self.build([self.source])
            paths = builder._default_images()
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].is_relative_to(self.output.resolve()))
            config_path = self.output / 'viewer_config.json'
            config = json.loads(config_path.read_text(encoding='utf-8'))
            config['scenes'][0]['image'] = '../private.jpg'
            config_path.write_text(json.dumps(config), encoding='utf-8')
            with self.assertRaises(ValueError):
                builder._default_images()

    def test_launcher_avoids_readonly_pid(self):
        with patch.object(launcher, '_powershell', return_value='123') as powershell:
            self.assertEqual(launcher._find_listener_pid(8765), 123)
        self.assertNotIn('$pid =', powershell.call_args.args[0].lower())

    @unittest.skipUnless(os.name == 'nt', 'Windows process lookup')
    def test_launcher_resolves_actual_listener(self):
        server = launcher._create_server('127.0.0.1', 0)
        try:
            self.assertEqual(launcher._find_listener_pid(server.server_address[1]), os.getpid())
        finally:
            server.server_close()

    def test_running_server_can_serve_a_replaced_build(self):
        self.build([self.source])
        original_cwd = Path.cwd()
        server = launcher._create_server('127.0.0.1', 0, self.output)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.build([])
            url = f'http://127.0.0.1:{server.server_address[1]}/viewer_config.json'
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertEqual(json.load(response)['scene_count'], 0)
                self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertEqual(Path.cwd(), original_cwd)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
