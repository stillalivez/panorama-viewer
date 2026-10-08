"""Build a Windows portable folder and ZIP, keeping photos outside the exe."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'outputs' / 'desktop-release'
BUILD = ROOT / 'outputs' / 'desktop-build'


def copy_viewer_resources(resources: Path, public: bool = False):
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'panorama_viewer_template.html', resources / 'panorama_viewer_template.html')
    viewer = resources / 'viewer'
    viewer.mkdir(exist_ok=True)
    original = ROOT / 'outputs/panorama_viewer'
    config_path = ROOT / 'assets/empty-viewer.json' if public else original / 'viewer_config.json'
    if not config_path.exists():
        config_path = ROOT / 'assets/empty-viewer.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    # Only resources referenced by the shipped scene configuration are distributed.
    for scene in config['scenes']:
        scene.pop('source_path', None)
        for key in ('image', 'thumbnail'):
            relative = Path(scene[key])
            source = (original / relative).resolve()
            destination = (viewer / relative).resolve()
            if not source.is_relative_to(original.resolve()) or not destination.is_relative_to(viewer.resolve()):
                raise ValueError('场景资源必须位于查看器目录内')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    (viewer / 'assets').mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'assets/app-icon.ico', viewer / 'assets/app-icon.ico')
    (viewer / 'viewer_config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='构建 Windows 桌面便携包')
    parser.add_argument('--public', action='store_true', help='构建开源分发包，不包含任何本机照片或场景配置')
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise SystemExit('请在 Windows 上构建 Windows 桌面版。')
    output = ROOT / 'outputs/public-release' if args.public else OUTPUT
    build = ROOT / 'outputs/public-build' if args.public else BUILD
    output.mkdir(parents=True, exist_ok=True)
    build.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed',
                    '--name', 'PanoramaViewer', '--icon', str(ROOT / 'assets/app-icon.ico'),
                    '--version-file', str(ROOT / 'desktop_version.txt'),
                    '--distpath', str(output), '--workpath', str(build / 'work'), '--specpath', str(build),
                    '--collect-data', 'webview', '--copy-metadata', 'pywebview',
                    str(ROOT / 'desktop_viewer.py')], cwd=ROOT, check=True)
    app = output / 'PanoramaViewer'
    copy_viewer_resources(app / 'resources', public=args.public)
    shutil.copy2(ROOT / 'desktop_README.md', app / '使用说明.md')
    shutil.copy2(ROOT / 'LICENSE', app / 'LICENSE')
    notices = ['720°全景照片查看器 — 第三方组件许可\n']
    distributions = sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower())
    for dist in distributions:
        notices.append(f'\n===== {dist.metadata["Name"]} {dist.version} =====\n')
        for file in dist.files or []:
            if '/licenses/' in str(file).lower() or file.name.lower().startswith(('license', 'copying')):
                path = Path(dist.locate_file(file))
                if path.is_file() and path.stat().st_size < 1_000_000:
                    notices.append(path.read_text(encoding='utf-8', errors='replace') + '\n')
    (app / 'THIRD_PARTY_NOTICES.txt').write_text(''.join(notices), encoding='utf-8')
    manifest = {str(p.relative_to(app)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(app.rglob('*')) if p.is_file() and p.name != 'SHA256.json'}
    (app / 'SHA256.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    archive = shutil.make_archive(str(output / 'PanoramaViewer-1.0.0-windows-x64'), 'zip', output, 'PanoramaViewer')
    print(f'Portable executable: {app / "PanoramaViewer.exe"}')
    print(f'ZIP package: {archive}')


if __name__ == '__main__':
    main()
