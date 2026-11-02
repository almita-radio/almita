"""Where the instrument services run: this machine, or the Pi at the tripod reached over SSH.

The Pi 4 at the tripod serves only rtl_tcp, INDI and the DS18 sensors; the notebook runs everything else. The
sockets need nothing here - an SSH tunnel brings the Pi's 1234 (rtl_tcp) and 7624 (INDI) to the notebook's
localhost, so every "localhost:1234" in the code keeps working. What does need the Pi is the control and the
evidence of rtl_tcp.service: systemctl, journalctl (tuning ACKs, Bias-T, USB re-enumeration) and ps.

Config lives OUTSIDE the repository, on the machine that runs the code (like the web credentials):
    ~/.config/almita/instrument.json      {"ssh": "almita-pi4"}
or the file named by $ALMITA_INSTRUMENT_FILE. No file, or no "ssh" key = the services are local (the Pi 5 setup).
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import List, Optional, Sequence

INSTRUMENT_FILE_ENV = "ALMITA_INSTRUMENT_FILE"
DEFAULT_INSTRUMENT_FILE = Path.home() / ".config" / "almita" / "instrument.json"
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5")


def instrument_file() -> Path:
    return Path(os.environ.get(INSTRUMENT_FILE_ENV) or DEFAULT_INSTRUMENT_FILE)


def ssh_target() -> Optional[str]:
    """The SSH destination of the instrument Pi, or None when the services run on this machine."""
    try:
        data = json.loads(instrument_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    target = data.get("ssh") if isinstance(data, dict) else None
    return target.strip() if isinstance(target, str) and target.strip() else None


def command(argv: Sequence[str]) -> List[str]:
    """argv as it must be run to act on the instrument services: unchanged locally, wrapped in ssh otherwise."""
    target = ssh_target()
    if target is None:
        return list(argv)
    return ["ssh", *SSH_OPTIONS, target, shlex.join(argv)]


def where() -> str:
    target = ssh_target()
    return "this machine" if target is None else f"{target} (ssh)"


def read_bytes(path: Path, timeout: float = 10.0) -> bytes:
    """A file of the instrument machine (e.g. /proc/<pid>/cmdline of rtl_tcp): read locally, or with cat over ssh."""
    if ssh_target() is None:
        return Path(path).read_bytes()
    import subprocess
    out = subprocess.run(command(["cat", str(path)]), capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise OSError(f"{where()}: cannot read {path}: {out.stderr.decode(errors='replace').strip()}")
    return out.stdout
