from __future__ import annotations

import argparse
import os
import subprocess
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parent
DEFAULT_VIEWER_DIR = ROOT / "outputs" / "panorama_viewer"
BROWSER_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


class NoCacheRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


def _powershell(command: str) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or "").strip()


def _find_listener_pid(port: int) -> int | None:
    if os.name != "nt" or port == 0:
        return None
    output = _powershell(
        f"$listenerPid = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty OwningProcess; if ($listenerPid) { $listenerPid }"
    )
    if not output:
        return None
    try:
        return int(output.splitlines()[-1].strip())
    except ValueError:
        return None


def _process_command_line(pid: int) -> str:
    if os.name != "nt":
        return ""
    return _powershell(
        f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\" -ErrorAction SilentlyContinue; "
        "if ($p) { $p.CommandLine }"
    )


def _stop_stale_viewer(port: int) -> None:
    pid = _find_listener_pid(port)
    if not pid or pid == os.getpid():
        return
    command_line = _process_command_line(pid)
    own_script = str(Path(__file__).resolve()).replace('/', '\\').casefold()
    if own_script not in command_line.replace('/', '\\').casefold():
        return
    result = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return
    time.sleep(0.5)
    print(f"Stopped stale panorama viewer process on port {port}: PID {pid}")


def _open_in_browser(url: str) -> None:
    for candidate in BROWSER_CANDIDATES:
        if candidate.exists():
            subprocess.Popen([str(candidate), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"Opened in browser: {candidate}")
            return
    if webbrowser.open(url):
        print("Opened in default browser.")
        return
    print(f"Open this URL manually: {url}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the local panorama viewer over HTTP.")
    parser.add_argument("--dir", default=str(DEFAULT_VIEWER_DIR), help="Viewer directory to serve.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Preferred port. Defaults to a random free port.",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not automatically open the browser.")
    return parser.parse_args()


def _create_server(host: str, port: int, directory: Path | None = None) -> ThreadingHTTPServer:
    candidates = [port] + ([] if port == 0 else [0])
    last_error: OSError | None = None
    if port != 0:
        _stop_stale_viewer(port)
    for candidate in candidates:
        try:
            handler = partial(NoCacheRequestHandler, directory=str(directory or DEFAULT_VIEWER_DIR))
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("Unable to start panorama viewer server.")


def main() -> None:
    args = parse_args()
    viewer_dir = Path(args.dir).resolve()
    index_path = viewer_dir / "index.html"
    if not index_path.exists():
        raise FileNotFoundError(
            f"Viewer entry file not found: {index_path}\nRun `python build_panorama_viewer.py` first."
        )

    server = _create_server(args.host, args.port, viewer_dir)

    with server:
        host, port = server.server_address[:2]
        version = index_path.stat().st_mtime_ns
        url = f"http://{host}:{port}/index.html?{urlencode({'v': str(version)})}"
        print(f"Serving panorama viewer from: {viewer_dir}")
        print(f"Open in browser: {url}")
        print("Press Ctrl+C to stop the local server.")
        try:
            if not args.no_open:
                try:
                    _open_in_browser(url)
                except Exception as exc:
                    print(f"Browser auto-open failed: {exc}")
                    print(f"Open this URL manually: {url}")
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nPanorama viewer server stopped.")


if __name__ == "__main__":
    main()
