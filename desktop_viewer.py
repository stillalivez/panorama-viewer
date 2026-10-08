from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import mimetypes
import os
import secrets
import shutil
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps

import build_panorama_viewer as builder


APP_NAME = '720°全景照片查看器'
APP_VERSION = '1.0.0'
MAX_OPEN_FILES = 100
MAX_RECENT_FILES = 20
LOG = logging.getLogger('panorama.desktop')


def resource_root() -> Path:
    return Path(sys.executable).parent / 'resources' if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


def file_id(path: Path) -> str:
    return 'file-' + hashlib.sha256(os.path.normcase(str(path.resolve())).encode('utf-8')).hexdigest()[:24]


def finite(value, default: float, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return max(low, min(high, float(value)))


def clean_view(value) -> dict:
    value = value if isinstance(value, dict) else {}
    heading = value.get('manual_heading')
    return dict(yaw=finite(value.get('yaw'), 0, -1e6, 1e6),
                pitch=finite(value.get('pitch'), 0, -1.562, 1.562),
                fov=finite(value.get('fov'), 75, 20, 100),
                manual_heading=(finite(heading, 0, 0, 360) % 360
                                if type(heading) in (int, float) and math.isfinite(heading) else None))


class SettingsStore:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'settings.json'
        self.lock = threading.RLock()
        self.warning = ''
        self.data = dict(version=1, open_files=[], recent_files=[], views={}, current_scene_id='',
                         window=dict(width=1280, height=820, maximized=False))
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(raw, dict):
                    raise ValueError('设置应为 JSON 对象')
                for key, limit in [('open_files', MAX_OPEN_FILES), ('recent_files', MAX_RECENT_FILES)]:
                    values = raw.get(key, [])
                    if isinstance(values, list):
                        self.data[key] = list(dict.fromkeys(p for p in values if isinstance(p, str) and len(p) < 32768))[:limit]
                views = raw.get('views', {})
                if isinstance(views, dict):
                    self.data['views'] = {str(k): clean_view(v) for k, v in list(views.items())[-200:]}
                if isinstance(raw.get('current_scene_id'), str):
                    self.data['current_scene_id'] = raw['current_scene_id'][:128]
                window = raw.get('window', {})
                if isinstance(window, dict):
                    self.data['window'] = dict(width=int(finite(window.get('width'), 1280, 800, 7680)),
                                               height=int(finite(window.get('height'), 820, 540, 4320)),
                                               maximized=window.get('maximized') is True)
            except (OSError, ValueError, TypeError):
                LOG.exception('Could not read settings')
                backup = self.path.with_name(f'settings-invalid-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.json')
                shutil.copy2(self.path, backup)
                self.warning = '原设置文件无法读取，已保留备份并使用默认设置。'

    def write(self):
        with self.lock:
            temporary = self.path.with_suffix('.json.tmp')
            try:
                with temporary.open('w', encoding='utf-8') as stream:
                    json.dump(self.data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)


class DesktopSession:
    """Own selected file references, metadata and a bounded recent-file history."""

    def __init__(self, viewer_dir: Path, store: SettingsStore):
        self.viewer_dir = viewer_dir.resolve()
        self.store = store
        self.lock = threading.RLock()
        self.resources: dict[str, Path] = {}
        self.scenes: dict[str, dict] = {}
        self.external: dict[str, Path] = {}
        self.sequence = -1
        self.startup_messages = [store.warning] if store.warning else []
        config_path = self.viewer_dir / 'viewer_config.json'
        if not config_path.exists():
            config_path = resource_root() / 'assets/empty-viewer.json'
        self.config = json.loads(config_path.read_text(encoding='utf-8'))
        for raw in self.config.get('scenes', []):
            scene = dict(raw)
            for key in ('image', 'thumbnail'):
                path = (self.viewer_dir / scene[key]).resolve()
                if not path.is_relative_to(self.viewer_dir):
                    raise ValueError('内置资源路径超出查看器目录')
                scene[key] = self.register(path)
            scene['desktop_file'] = False
            self.apply_view(scene)
            self.scenes[scene['id']] = scene
        self.config['desktop'] = True
        self.config['app_version'] = APP_VERSION
        self.config['scenes'] = []
        self.config['desktop_messages'] = self.startup_messages

    def register(self, path: Path) -> str:
        key = hashlib.sha256(str(path).encode('utf-8')).hexdigest()
        self.resources[key] = path
        return './media/' + key

    def apply_view(self, scene: dict):
        saved = self.store.data['views'].get(scene['id'])
        if saved:
            scene['saved_view'] = dict(saved)
            scene['manual_heading'] = saved['manual_heading']

    def inspect_file(self, path: Path) -> dict:
        path = path.resolve(strict=True)
        if not path.is_file() or path.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
            raise ValueError('请选择 JPEG、PNG、WebP 全景图片')
        stat = path.stat()
        signature = hashlib.sha256(f'{path}|{stat.st_size}|{stat.st_mtime_ns}'.encode('utf-8')).hexdigest()
        thumb_dir = self.store.directory / 'thumbnails'
        thumb_dir.mkdir(exist_ok=True)
        thumbnail = thumb_dir / f'{signature}.jpg'
        if not thumbnail.exists():
            with Image.open(path) as check:
                check.verify()
        with Image.open(path) as image:
            if image.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('图片内容不是 JPEG、PNG 或 WebP')
            width, height = image.size
            if image.getexif().get(274, 1) in (5, 6, 7, 8):
                width, height = height, width
            if height <= 0 or abs(width / height - 2) > .02:
                raise ValueError(f'需要 2:1 全景，当前为 {width}×{height}')
            if not thumbnail.exists():
                image = ImageOps.exif_transpose(image)
                image.thumbnail((480, 240), Image.Resampling.LANCZOS)
                image.convert('RGB').save(thumbnail, 'JPEG', quality=78)
        capture, capture_source = builder._capture_metadata(path)
        heading, heading_source = builder._heading_metadata(path)
        scene = dict(id=file_id(path), label=path.stem, source_name=path.name,
                     source_path=str(path), image=self.register(path), thumbnail=self.register(thumbnail),
                     desktop_file=True, capture_time=capture, capture_time_label=capture,
                     capture_time_source=capture_source, heading_degrees=heading, heading_source=heading_source,
                     file_modified=datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                     original_width=width, original_height=height, optimized_width=width, optimized_height=height)
        self.apply_view(scene)
        return scene

    def add_files(self, paths, restore=False) -> dict:
        failures, added = [], []
        duplicates, selected = 0, ''
        with self.lock, self.store.lock:
            for value in list(paths)[:MAX_OPEN_FILES]:
                try:
                    path = Path(value).resolve()
                    sid = file_id(path)
                    if sid in self.external:
                        duplicates += 1
                        selected = selected or sid
                    else:
                        if len(self.external) >= MAX_OPEN_FILES:
                            raise ValueError(f'最多同时打开 {MAX_OPEN_FILES} 张图片，请先移除部分场景')
                        scene = self.inspect_file(path)
                        self.scenes[sid] = scene
                        self.external[sid] = path
                        added.append(scene)
                        selected = selected or sid
                    if not restore:
                        recent = [p for p in self.store.data['recent_files'] if file_id(Path(p)) != sid]
                        self.store.data['recent_files'] = [str(path), *recent][:MAX_RECENT_FILES]
                except (OSError, ValueError, TypeError, Image.DecompressionBombError) as exc:
                    failures.append(f'{Path(str(value)).name}：{exc}')
                    LOG.info('File not opened: %s', exc)
            self.store.data['open_files'] = [str(p) for p in self.external.values()]
            if not restore:
                self.store.write()
            return dict(scenes=added, selected=selected, duplicates=duplicates, failures=failures, recent=self.recent())

    def restore(self):
        result = self.add_files(self.store.data['open_files'], restore=True)
        self.startup_messages.extend(result['failures'])

    def recent(self) -> list[dict]:
        return [dict(id=file_id(Path(p)), name=Path(p).name, path=p, available=Path(p).is_file())
                for p in self.store.data['recent_files']]

    def html_config(self) -> dict:
        with self.lock:
            return dict(self.config, scenes=list(self.scenes.values()), scene_count=len(self.scenes),
                        desktop_current=self.store.data['current_scene_id'], desktop_sequence=self.sequence, desktop_recent=self.recent(),
                        desktop_messages=list(self.startup_messages))

    def save_session(self, snapshot) -> dict:
        if not isinstance(snapshot, dict):
            raise ValueError('无效的查看状态')
        with self.lock, self.store.lock:
            sequence = snapshot.get('sequence')
            if type(sequence) is not int or sequence <= self.sequence:
                return dict(saved=False)
            views = snapshot.get('views', {})
            if not isinstance(views, dict):
                raise ValueError('无效的视角数据')
            for sid, value in views.items():
                if sid in self.scenes:
                    clean = clean_view(value)
                    self.store.data['views'][sid] = clean
                    self.scenes[sid]['saved_view'] = clean
                    self.scenes[sid]['manual_heading'] = clean['manual_heading']
            self.store.data['views'] = dict(list(self.store.data['views'].items())[-200:])
            sid = snapshot.get('current_scene_id')
            if sid in self.scenes:
                self.store.data['current_scene_id'] = sid
            self.store.write()
            self.sequence = sequence
            return dict(saved=True)

    def remove_file(self, sid: str) -> dict:
        with self.lock, self.store.lock:
            if sid not in self.external:
                return dict(removed=False)
            self.external.pop(sid)
            scene = self.scenes.pop(sid)
            for key in ('image', 'thumbnail'):
                self.resources.pop(scene[key].rsplit('/', 1)[-1], None)
            self.store.data['open_files'] = [str(p) for p in self.external.values()]
            self.store.write()
            return dict(removed=True, recent=self.recent())

    def clear_recent(self):
        with self.store.lock:
            self.store.data['recent_files'] = []
            self.store.write()
        return []


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, session: DesktopSession, template: Path):
        self.session = session
        self.template = template
        self.token = secrets.token_urlsafe(24)
        super().__init__(('127.0.0.1', 0), ViewerHandler)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.url = f'{self.origin}/{self.token}/index.html'


class ViewerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_content(False)

    def do_HEAD(self):
        self.send_content(True)

    def send_content(self, head: bool):
        # Only explicit image registrations and the application page are exposed.
        prefix = f'/{self.server.token}/'
        route = urlsplit(self.path).path
        if not route.startswith(prefix):
            self.send_error(404)
            return
        route = route[len(prefix):]
        try:
            if route == 'index.html':
                config = self.server.session.html_config()
                values = {'__CONFIG__': builder._script_json(config), '__TITLE__': builder.html.escape(config['title']),
                          '__SUBTITLE__': '桌面版', '__TIME__': builder.html.escape(config.get('generated_at', ''))}
                page = builder.re.sub(r'__CONFIG__|__TITLE__|__SUBTITLE__|__TIME__', lambda m: values[m.group()],
                                      self.server.template.read_text(encoding='utf-8'))
                self.send_bytes(page.encode('utf-8'), 'text/html; charset=utf-8', head)
            elif route == 'assets/app-icon.ico':
                icon = self.server.session.viewer_dir / 'assets/app-icon.ico'
                if not icon.is_file():
                    icon = resource_root() / 'assets/app-icon.ico'
                self.send_file(icon, head)
            elif route.startswith('media/') and route[6:] in self.server.session.resources:
                self.send_file(self.server.session.resources[route[6:]], head)
            else:
                self.send_error(404)
        except (OSError, KeyError):
            self.send_error(404, 'File is no longer available')

    def send_headers(self, length: int, content_type: str):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()

    def send_bytes(self, data: bytes, content_type: str, head: bool):
        self.send_headers(len(data), content_type)
        if not head:
            self.wfile.write(data)

    def send_file(self, path: Path, head: bool):
        with path.open('rb') as stream:
            self.send_headers(os.fstat(stream.fileno()).st_size, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            if not head:
                shutil.copyfileobj(stream, self.wfile, 256 * 1024)

    def log_message(self, *_):
        pass


class DesktopAPI:
    def __init__(self, session: DesktopSession):
        self._session = session
        self._window = None
        self._busy = threading.Lock()
        self._fullscreen = False
        self._fullscreen_lock = threading.Lock()
        self._restore_maximized = False

    def _notify(self, payload: dict):
        if self._window:
            self._window.evaluate_js(f'window.panoramaDesktop?.receive({builder._script_json(payload)})')

    def _open(self, paths):
        if not self._busy.acquire(blocking=False):
            return dict(scenes=[], failures=['正在处理上一批图片，请稍等。'])
        try:
            return self._session.add_files(paths)
        finally:
            self._busy.release()

    def _accept_drop(self, event):
        paths = [f.get('pywebviewFullPath') for f in event.get('dataTransfer', {}).get('files', [])]
        paths = [p for p in paths if p]
        if not paths:
            self._notify(dict(scenes=[], failures=['未能取得拖入文件的路径，请使用“打开全景图”。']))
            return
        self._notify(dict(working=True))
        try:
            self._notify(self._open(paths))
        except Exception:
            LOG.exception('Dropped files could not be opened')
            self._notify(dict(scenes=[], failures=['未能处理拖入图片，请检查文件和设置目录后重新打开。']))

    def open_files(self):
        import webview
        paths = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                    file_types=('全景照片 (*.jpg;*.jpeg;*.png;*.webp)',))
        return self._open(paths) if paths else dict(cancelled=True)

    def open_recent(self, sid):
        path = next((p for p in self._session.store.data['recent_files'] if file_id(Path(p)) == sid), None)
        if not path:
            return dict(scenes=[], failures=['此记录已不存在，请重新选择图片。'])
        return self._open([path])

    def remove_file(self, sid):
        return self._session.remove_file(sid)

    def clear_recent(self):
        return self._session.clear_recent()

    def save_session(self, snapshot):
        return self._session.save_session(snapshot)

    def set_fullscreen(self, active):
        if type(active) is not bool:
            raise ValueError('无效的全屏状态')
        with self._fullscreen_lock:
            if active == self._fullscreen:
                return active
            if active:
                self._restore_maximized = self._session.store.data['window']['maximized']
                self._fullscreen = True
                try:
                    self._window.toggle_fullscreen()
                except Exception:
                    self._fullscreen = False
                    raise
            else:
                self._window.toggle_fullscreen()
                if self._restore_maximized:
                    self._window.maximize()
                self._fullscreen = False
            return active


def show_error(message: str):
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    else:
        print(message, file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME + '桌面版')
    parser.add_argument('images', nargs='*', help='启动时打开的图片；也可拖到 exe 图标上')
    parser.add_argument('--data-dir', type=Path, help='独立设置目录，用于便携工作区或验证')
    parser.add_argument('--viewer-dir', type=Path, help='自定义查看器资源目录')
    args = parser.parse_args(argv)
    root = resource_root()
    data_dir = (args.data_dir or Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'PanoramaViewer').resolve()
    server = None
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=data_dir / 'desktop.log', level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
        LOG.info('Starting desktop viewer %s', APP_VERSION)
        import webview
        from webview.dom import DOMEventHandler

        store = SettingsStore(data_dir)
        viewer = args.viewer_dir or (root / 'viewer' if getattr(sys, 'frozen', False) else root / 'outputs/panorama_viewer')
        session = DesktopSession(viewer, store)
        session.restore()
        if args.images:
            opened = session.add_files(args.images)
            session.startup_messages.extend(opened['failures'])
            if opened['selected']:
                store.data['current_scene_id'] = opened['selected']
        server = ViewerServer(session, root / 'panorama_viewer_template.html')
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        worker.start()
        api = DesktopAPI(session)
        geometry = store.data['window']
        window = webview.create_window(APP_NAME + ' · 桌面版', server.url, js_api=api,
                    width=geometry['width'], height=geometry['height'], maximized=geometry['maximized'],
                    min_size=(800, 540), background_color='#15110f', text_select=True)
        api._window = window
        close_state = {'flushing': False, 'allowed': False, 'maximized': geometry['maximized']}

        def loaded():
            try:
                window.dom.document.events.dragover += DOMEventHandler(lambda _: None, True, True, debounce=250)
                window.dom.document.events.drop += DOMEventHandler(api._accept_drop, True, True)
                window.evaluate_js('window.panoramaDesktop?.connected()')
                LOG.info('Desktop bridge and drag/drop ready')
            except Exception:
                LOG.exception('Desktop setup failed')

        def save_geometry(width=None, height=None):
            if close_state['maximized'] or api._fullscreen:
                return
            if width and height:
                with store.lock:
                    store.data['window'].update(width=max(800, int(width)), height=max(540, int(height)))

        def maximized():
            if not api._fullscreen:
                close_state['maximized'] = True
                store.data['window']['maximized'] = True

        def restored():
            if not api._fullscreen:
                close_state['maximized'] = False
                store.data['window']['maximized'] = False

        def finish_close():
            try:
                if window.events.loaded.is_set():
                    snapshot = window.evaluate_js('window.panoramaDesktop?.snapshot()')
                    if snapshot:
                        session.save_session(snapshot)
                with store.lock:
                    store.data['window']['maximized'] = close_state['maximized']
                    store.write()
            except Exception:
                LOG.exception('Final settings save failed')
            finally:
                close_state['allowed'] = True
                window.destroy()

        def closing():
            # Evaluate JavaScript outside the UI thread to avoid a close-time deadlock.
            if close_state['allowed']:
                return True
            if not close_state['flushing']:
                close_state['flushing'] = True
                threading.Thread(target=finish_close, daemon=True).start()
            return False

        window.events.loaded += loaded
        window.events.resized += save_geometry
        window.events.maximized += maximized
        window.events.restored += restored
        window.events.closing += closing
        webview.settings['ALLOW_FILE_URLS'] = False
        webview.settings['SHOW_DEFAULT_MENUS'] = False
        # A dedicated cache avoids writing beside the exe and is disposable on exit.
        with tempfile.TemporaryDirectory(prefix='webview-', dir=data_dir) as cache:
            webview.start(gui='edgechromium', debug=False, private_mode=True, storage_path=cache)
        LOG.info('Desktop window closed')
        return 0
    except Exception as exc:
        LOG.exception('Desktop startup failed')
        show_error(f'桌面查看器未能启动：{exc}\n\n请确认 resources 和 _internal 文件夹完整，并已安装 Microsoft Edge WebView2 Runtime。\n日志：{data_dir / "desktop.log"}')
        return 1
    finally:
        if server:
            server.shutdown()
            server.server_close()
            LOG.info('Local image service stopped')


if __name__ == '__main__':
    raise SystemExit(main())
