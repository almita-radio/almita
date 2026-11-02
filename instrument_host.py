"""Where the instrument services run: this machine (the Pi 5 setup), or the capture Pi at the tripod (Pi 4).

The capture Pi serves only rtl_tcp (1234), INDI (7624) and the DS18 sensors; the notebook runs everything else.
- Sockets: instrument_forwarder.py, a service on the notebook, carries 127.0.0.1:1234 and :7624 to the capture
  Pi, so every "localhost:1234" / "localhost:7624" in the code keeps working unchanged.
- Control and evidence of rtl_tcp.service (systemctl, journalctl for the tuning ACKs / Bias-T / USB re-attachment,
  ps, /proc/<pid>/...) are not sockets: run() sends them to instrument_agent.py on the capture Pi, which runs only a
  fixed list of read-only commands.

The choice lives OUTSIDE the repository, on the machine that runs the code (like the web credentials):
    ~/.config/almita/instrument.json      {"host": "192.168.50.254"}
or the file named by $ALMITA_INSTRUMENT_FILE. No file / no host = the services are on this machine. The web's
STATUS page reads and writes it (set_host)."""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

INSTRUMENT_FILE_ENV = "ALMITA_INSTRUMENT_FILE"
DEFAULT_INSTRUMENT_FILE = Path.home() / ".config" / "almita" / "instrument.json"
AGENT_PORT = 8099
RTL_TCP_PORT, INDI_PORT = 1234, 7624
_HOSTNAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def instrument_file() -> Path:
    return Path(os.environ.get(INSTRUMENT_FILE_ENV) or DEFAULT_INSTRUMENT_FILE)


def _config() -> Dict[str, Any]:
    try:
        data = json.loads(instrument_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remote_host() -> Optional[str]:
    """The capture Pi's IP or hostname, or None when the instrument services run on this machine."""
    host = _config().get("host")
    return host.strip() if isinstance(host, str) and valid_host(host.strip()) else None


def valid_host(host: str) -> bool:
    if not isinstance(host, str) or not host or host != host.strip():
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(host))


def where() -> str:
    host = remote_host()
    return "this machine" if host is None else host


def set_host(host: Optional[str]) -> Dict[str, Any]:
    """Store the capture Pi (None / "" = this machine). Written atomically; the forwarder and every later run() read it."""
    host = (host or "").strip()
    if host and not valid_host(host):
        raise ValueError(f"not an IP address or hostname: {host!r}")
    path = instrument_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"host": host} if host else {}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return describe()


def describe() -> Dict[str, Any]:
    host = remote_host()
    return {"host": host, "remote": host is not None, "where": where(), "config_file": str(instrument_file()),
            "agent_port": AGENT_PORT, "rtl_tcp_port": RTL_TCP_PORT, "indi_port": INDI_PORT}


# ------------------------------------------------------------------ commands on the instrument machine
def _agent_call(host: str, path: str, body: Optional[dict], timeout: float) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    for attempt in (1, 2):           # every agent command is read-only: one retry after a dropped connection is safe
        req = urllib.request.Request(f"http://{host}:{AGENT_PORT}{path}", data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except (ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected):
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def run(argv: Sequence[str], *, capture_output: bool = True, text: bool = False, timeout: float = 10.0,
        **_: Any) -> subprocess.CompletedProcess:
    """subprocess.run() for the instrument services: local when they run here, through the capture Pi's agent
    otherwise. Same result shape; an unreachable agent or a refused command raises OSError/SubprocessError like a
    missing binary would, so the callers' existing error paths apply."""
    host = remote_host()
    if host is None:
        return subprocess.run(list(argv), capture_output=capture_output, text=text, timeout=timeout)
    try:
        out = _agent_call(host, "/run", {"argv": list(argv), "timeout": timeout}, timeout + 5)
    except urllib.error.HTTPError as exc:
        raise subprocess.SubprocessError(f"instrument agent on {host} refused {argv[0]}: "
                                         f"{exc.read().decode(errors='replace')[:200]}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, ConnectionError, http.client.HTTPException) as exc:
        raise OSError(f"instrument agent on {host}:{AGENT_PORT} unreachable: {exc}") from exc
    stdout = base64.b64decode(out.get("stdout_b64", ""))
    stderr = base64.b64decode(out.get("stderr_b64", ""))
    if text:
        stdout, stderr = stdout.decode(errors="replace"), stderr.decode(errors="replace")
    return subprocess.CompletedProcess(list(argv), int(out.get("returncode", 1)), stdout, stderr)


def read_bytes(path: Path, timeout: float = 10.0) -> bytes:
    """A file of the instrument machine (e.g. /proc/<pid>/cmdline of rtl_tcp)."""
    if remote_host() is None:
        return Path(path).read_bytes()
    out = run(["cat", str(path)], timeout=timeout)
    if out.returncode != 0:
        raise OSError(f"{where()}: cannot read {path}: {out.stderr.decode(errors='replace').strip()}")
    return out.stdout


def agent_health(host: Optional[str] = None, timeout: float = 3.0) -> Dict[str, Any]:
    host = host or remote_host()
    if host is None:
        return {"ok": True, "detail": "instrument services on this machine - no agent needed"}
    try:
        return {"ok": True, **_agent_call(host, "/health", None, timeout)}
    except Exception as exc:  # noqa: BLE001 - a health probe reports, never raises
        return {"ok": False, "detail": f"agent {host}:{AGENT_PORT} unreachable: {type(exc).__name__}: {exc}"}
