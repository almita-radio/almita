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
import shutil
import signal
import socket
import subprocess
from pathlib import Path

from serve_dashboard import make_server

ROOT = Path(__file__).parent.resolve()
CONSOLE_SOURCE = ROOT / "console"


def prepare_console_web(source_dir: Path, runtime_dir: Path, public_root: Path) -> Path:
    """Assemble the served public root: static assets + a runtime/ symlink.

    Idempotent: safe to call on every startup. Copies only the three known
    static files (no directory-wide copy) and symlinks only the canonical
    runtime directory - never the wider data/ tree.
    """
    public_root = Path(public_root)
    public_root.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "styles.css", "app.js"):
        shutil.copyfile(Path(source_dir) / name, public_root / name)
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
    server = make_server(public_root, bind=args.bind, port=args.port)

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
