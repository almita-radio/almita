"""ALMITA instrument agent - runs on the capture Pi (Pi 4 at the tripod), standard library only.

The notebook runs the web, capture and processing; the capture Pi serves rtl_tcp, INDI and the DS18 sensors. The
sockets reach the notebook through instrument_forwarder.py. What is not a socket - the evidence and state of
rtl_tcp.service that sdr_tuning and the preflight read (journalctl ACK lines, systemctl properties, ps, the
process's argv/environment, the kernel's USB attach lines) - is served here.

Only a FIXED list of read-only commands runs (allowed()): no shell, no restart, nothing that moves or retunes
anything. Anything else is refused with 403.

    POST /run      {"argv": [...], "timeout": s}  -> {"returncode", "stdout_b64", "stderr_b64"}
    GET  /health   hostname, UTC time, rtl_tcp.service state, RTL-SDR USB serials, DS18 sensor ids

Usage: python3 instrument_agent.py [--bind 0.0.0.0] [--port 8099]
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional, Sequence

UNIT = "rtl_tcp.service"
MAX_TIMEOUT = 20.0
_JOURNAL_FLAGS = {"-n", "--show-cursor", "--no-pager", "-o", "--since", "--utc", "-k"}
_JOURNAL_VALUES = {"-n": re.compile(r"^\d{1,6}$"), "-o": re.compile(r"^(cat|short-iso|short-iso-precise)$"),
                   "--since": re.compile(r"^[\w :.+\-]{1,64}$")}
_SYSTEMCTL_PROPS = {"ExecStart", "ActiveEnterTimestamp", "MainPID", "ActiveState"}
_PROC_FILE = re.compile(r"^/proc/\d{1,9}/(cmdline|environ)$")
_W1_FILE = re.compile(r"^/sys/bus/w1/devices/28-[0-9a-f]{12}/w1_slave$")


def _journal_ok(args: Sequence[str]) -> bool:
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--after-cursor="):
            if not re.match(r"^--after-cursor=[\w;=\-]{1,512}$", a):
                return False
            i += 1
            continue
        if a == "-u":
            if i + 1 >= len(args) or args[i + 1] != UNIT:
                return False
            i += 2
            continue
        if a not in _JOURNAL_FLAGS:
            return False
        if a in _JOURNAL_VALUES:
            if i + 1 >= len(args) or not _JOURNAL_VALUES[a].match(args[i + 1]):
                return False
            i += 2
            continue
        i += 1
    return True


def allowed(argv: Sequence[str]) -> bool:
    """The whole command surface of the agent. Read-only, rtl_tcp.service and the DS18 sensors only."""
    if not argv or not all(isinstance(a, str) for a in argv):
        return False
    argv = list(argv)
    if argv[0] == "journalctl":
        return _journal_ok(argv[1:]) and ("-u" in argv or "-k" in argv)
    if argv[0] == "systemctl":
        if argv[1:] == ["is-active", UNIT]:
            return True
        return (len(argv) == 6 and argv[1:3] == ["show", "-p"] and argv[3] in _SYSTEMCTL_PROPS
                and argv[4] == "--value" and argv[5] == UNIT)
    if argv == ["ps", "-eo", "pid,args"]:
        return True
    if argv[0] == "cat" and len(argv) == 2:
        return bool(_PROC_FILE.match(argv[1]) or _W1_FILE.match(argv[1]))
    return False


def rtlsdr_serials(root: Path = Path("/sys/bus/usb/devices")) -> List[str]:
    out = set()
    for dev in root.glob("*"):
        try:
            if (dev / "idVendor").read_text().strip() == "0bda" and (dev / "serial").exists():
                out.add((dev / "serial").read_text().strip())
        except OSError:
            continue
    return sorted(out)


def ds18_ids(root: Path = Path("/sys/bus/w1/devices")) -> List[str]:
    return sorted(p.name for p in root.glob("28-*")) if root.is_dir() else []


def health() -> dict:
    try:
        state = subprocess.run(["systemctl", "is-active", UNIT], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        state = f"unknown ({exc})"
    return {"agent": "almita-instrument-agent", "hostname": socket.gethostname(),
            "utc": datetime.now(timezone.utc).isoformat(), "rtl_tcp_service": state,
            "rtlsdr_serials": rtlsdr_serials(), "ds18_ids": ds18_ids()}


class Handler(BaseHTTPRequestHandler):
    server_version = "almita-instrument-agent/1"

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            return self._send(200, health())
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 65536)).decode())
            argv = body.get("argv")
            timeout = min(float(body.get("timeout", 10.0)), MAX_TIMEOUT)
        except (ValueError, AttributeError):
            return self._send(400, {"error": "bad request"})
        if not allowed(argv):
            self.log_message("REFUSED %r", argv)
            return self._send(403, {"error": "command not allowed", "argv": argv})
        try:
            out = subprocess.run(list(argv), capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return self._send(200, {"returncode": 127, "stdout_b64": "",
                                    "stderr_b64": base64.b64encode(str(exc).encode()).decode()})
        return self._send(200, {"returncode": out.returncode, "stdout_b64": base64.b64encode(out.stdout).decode(),
                                "stderr_b64": base64.b64encode(out.stderr).decode()})

    def log_message(self, fmt: str, *args) -> None:
        if self.command == "GET":
            return                                    # health polls are not logged
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8099)
    a = ap.parse_args(argv)
    server = ThreadingHTTPServer((a.bind, a.port), Handler)
    sys.stderr.write(f"instrument agent on {a.bind}:{a.port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
