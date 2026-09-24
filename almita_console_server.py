#!/usr/bin/env python3
"""Resident read-only HTTP server for the ALMITA field console.

Serves the static console/ frontend plus a symlinked view of the canonical
runtime status directory (data/runtime/). Reuses serve_dashboard's read-only
GET/HEAD-only handler unchanged - this script adds no new write surface.

Binds 0.0.0.0:8088 by default so it is reachable from any local interface
(WiFi, Ethernet). Does not open any port forwarding, UPnP, or firewall rule,
and never initiates outbound Internet connections; the local IPv4 listing at
startup is purely informational (read from local interface configuration).
"""
from __future__ import annotations

import argparse
import errno
import functools
import hashlib
import json
import select
import shutil
import signal
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from serve_dashboard import ReadOnlyHandler, ReadOnlyServer

import mount_camera

ROOT = Path(__file__).parent.resolve()
CONSOLE_SOURCE = ROOT / "console"

_MOUNT_CAMERA_STREAM_PATH = "/mount_camera/stream"
_MOUNT_CAMERA_STATUS_PATH = "/mount_camera/status"
_MOUNT_CAMERA_CONFIG_PATH = "/mount_camera/config"


class ConsoleHandler(ReadOnlyHandler):
    """serve_dashboard.ReadOnlyHandler (static files, GET/HEAD only, every write rejected) plus exactly
    three /mount_camera/* routes for the MOUNT CAMERA tile: GET stream (the MJPEG relay - see
    mount_camera.py), GET status, and GET/POST config. POST config is the ONE deliberate, narrow
    exception to this server's read-only contract - it writes a single JSON file holding only the
    camera's stream URL, nothing else, and every other path/method is still rejected exactly as before.
    """

    def do_GET(self):  # noqa: N802 - http.server's own naming convention
        path = urlsplit(self.path).path
        if path == _MOUNT_CAMERA_STREAM_PATH:
            return self._mount_camera_stream()
        if path == _MOUNT_CAMERA_STATUS_PATH:
            return self._json_ok(mount_camera.RELAY.status())
        if path == _MOUNT_CAMERA_CONFIG_PATH:
            return self._json_ok({"stream_url": mount_camera.RELAY.get_url(), "default_url": mount_camera.DEFAULT_STREAM_URL})
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if urlsplit(self.path).path == _MOUNT_CAMERA_CONFIG_PATH:
            return self._mount_camera_set_config()
        return self._reject()

    def _json_ok(self, payload) -> None:
        self._json(200, payload)

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _mount_camera_set_config(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            return self._json(400, {"ok": False, "error": "empty or oversized request body"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "invalid JSON body"})
        url = str(body.get("stream_url", "")).strip()
        if not mount_camera.valid_stream_url(url):
            return self._json(400, {"ok": False, "error": "stream_url must be an http:// or https:// URL, 8-500 characters"})
        mount_camera.RELAY.set_url(url)
        return self._json(200, {"ok": True, "stream_url": url})

    def _mount_camera_stream(self) -> None:
        """One subscriber to the shared relay per viewer connection - never a second upstream camera
        connection regardless of how many browsers/tabs are watching (see mount_camera.py)."""
        relay = mount_camera.RELAY
        sub = relay.subscribe()
        try:
            relay.wait_connected_or_failed(mount_camera.CONNECTED_WAIT_TIMEOUT_S)
            st = relay.status()
            if st["state"] == "ERROR":
                relay.unsubscribe(sub)
                return self._json(503, {"ok": False, "error": st["last_error"] or "camera unavailable"})
            self.send_response(200)
            self.send_header("Content-Type", relay.content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            while True:
                # Prompt disconnect detection: a write-only loop can otherwise take a long time to notice a
                # viewer that already closed its side - TCP happily accepts writes into its send buffer well
                # after the peer is gone, especially on loopback/LAN with generous buffers (found live: a
                # closed curl client was still counted as a viewer tens of seconds later). A cheap
                # non-blocking peek catches a half-closed socket immediately instead of waiting on write()
                # to eventually fail, which is what actually lets the idle timeout ever fire.
                readable, _, _ = select.select([self.connection], [], [], 0)
                if readable:
                    try:
                        if self.connection.recv(1, socket.MSG_PEEK) == b"":
                            break
                    except OSError:
                        break
                data = sub.read(timeout=1.0)
                if data is None:
                    break
                if data:
                    self.wfile.write(data)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            relay.unsubscribe(sub)


def make_console_server(root: Path, bind: str = "0.0.0.0", port: int = 8088) -> ReadOnlyServer:
    root = Path(root).resolve()
    handler = functools.partial(ConsoleHandler, directory=str(root))
    return ReadOnlyServer((bind, port), handler)


def _asset_version(path: Path) -> str:
    """Short content hash used to cache-bust a static asset's URL. Changes
    only when the file's own bytes change, so a browser that already cached
    an older app.js/styles.css always picks up a changed one on the next
    console load - with no manual query-string editing required."""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:8]


def prepare_console_web(source_dir: Path, runtime_dir: Path, public_root: Path) -> Path:
    """Assemble the served public root: static assets + a runtime/ symlink.

    Idempotent: safe to call on every startup. Copies only the three known
    static files (no directory-wide copy) and symlinks only the canonical
    runtime directory - never the wider data/ tree. index.html's references
    to styles.css/app.js are rewritten with a per-file content-hash query
    string (durable cache-busting, fully offline).
    """
    source_dir = Path(source_dir)
    public_root = Path(public_root)
    public_root.mkdir(parents=True, exist_ok=True)
    for name in ("styles.css", "app.js", "spectral_stack_3d.js"):
        shutil.copyfile(source_dir / name, public_root / name)
    if (source_dir / "common.js").is_file():          # shared web helpers (optional: a source dir without it still prepares)
        shutil.copyfile(source_dir / "common.js", public_root / "common.js")
    html = (source_dir / "index.html").read_text()
    html = html.replace('href="styles.css"', f'href="styles.css?v={_asset_version(source_dir / "styles.css")}"')
    html = html.replace('src="app.js"', f'src="app.js?v={_asset_version(source_dir / "app.js")}"')
    html = html.replace(
        'src="spectral_stack_3d.js"',
        f'src="spectral_stack_3d.js?v={_asset_version(source_dir / "spectral_stack_3d.js")}"',
    )
    if (source_dir / "common.js").is_file():
        html = html.replace('src="common.js"', f'src="common.js?v={_asset_version(source_dir / "common.js")}"')
    (public_root / "index.html").write_text(html)
    # Vendored third-party (SPECTRAL STACK 3D's three.js) - large and never
    # edited by us, so symlinked whole rather than copied like the small
    # first-party assets above; same pattern as the runtime/ symlink below.
    vendor_source = source_dir / "vendor"
    vendor_link = public_root / "vendor"
    if vendor_source.is_dir() and not vendor_link.exists():
        vendor_link.symlink_to(vendor_source.resolve(), target_is_directory=True)
    runtime_dir = Path(runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    link = public_root / "runtime"
    if link.is_symlink() or link.exists():
        if link.is_symlink() and link.resolve() == runtime_dir:
            return public_root
        if link.is_symlink():
            link.unlink()
        else:
            raise FileExistsError(f"{link} exists and is not the expected runtime symlink")
    link.symlink_to(runtime_dir, target_is_directory=True)
    return public_root


def write_version_json(public_root: Path) -> Path:
    """version.json for the footer: git short SHA + start time + hostname. No secrets, no paths."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), capture_output=True, text=True, timeout=3).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        sha = "unknown"
    info = {"project": "ALMITA", "author": "Felipe Fridman", "project_url": "https://github.com/almita-radio/almita", "component": "field_console",
            "git_short_sha": sha, "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "hostname": socket.gethostname(),
            "transport": "HTTP (LAN, no TLS, no authentication)"}
    path = Path(public_root) / "version.json"
    path.write_text(json.dumps(info, indent=2))
    return path


def list_local_ipv4() -> list[str]:
    """Best-effort, read-only listing of local IPv4 addresses. Informational
    only - never used to open ports, forward traffic, or reach the Internet.
    """
    addresses: list[str] = []
    try:
        output = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2)
        if output.returncode == 0:
            addresses = [token for token in output.stdout.split() if "." in token]
    except (OSError, subprocess.SubprocessError):
        pass
    if not addresses:
        try:
            addresses = [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if ip != "127.0.0.1"]
        except OSError:
            addresses = []
    return addresses


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console-source", default=str(CONSOLE_SOURCE))
    parser.add_argument("--runtime-dir", default=str(ROOT / "data" / "runtime"))
    parser.add_argument("--public-root", default=str(ROOT / "data" / "console_web"))
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()

    public_root = prepare_console_web(Path(args.console_source), Path(args.runtime_dir), Path(args.public_root))
    write_version_json(public_root)
    try:
        server = make_console_server(public_root, bind=args.bind, port=args.port)
    except OSError as exc:
        reason = "port already in use (another instance or service holds it; nothing was killed)" if exc.errno == errno.EADDRINUSE else str(exc)
        print(f"ALMITA CONSOLE ERROR cannot bind {args.bind}:{args.port}: {reason}", flush=True)
        return 2

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"ALMITA CONSOLE START bind={args.bind} port={server.server_port} root={public_root}", flush=True)
    for ip in list_local_ipv4():
        print(f"  http://{ip}:{server.server_port}/", flush=True)
    print(f"  http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("ALMITA CONSOLE STOP", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
