"""Which INDI mount the web drives: the real LX200 OnStep (default) or INDI's Telescope Simulator.

One server-side value for every page and every process launched from the web (ALIGN, LOBE, OBSERVE, the CALIBRATE
wizard, the gain pilot, STOP MOUNT), so one page can never point at one mount while STOP MOUNT stops the other.
Operator, 2026-10-17: the mount is not connected; the simulator lets the whole chain run indoors. Anything produced
while the simulator is selected records it (job meta, capture attrs, result files) and is not sky data.

The choice lives in data/runtime/mount_device.json. A missing or unreadable file, or an unknown name, means the real
mount: the default never depends on a file being present."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

LX200 = "LX200 OnStep"
SIMULATOR = "Telescope Simulator"
CHOICES = (LX200, SIMULATOR)
DEFAULT = LX200
STATE_PATH = Path(__file__).resolve().parent / "data" / "runtime" / "mount_device.json"


def current(path: Optional[Path] = None) -> str:
    """The selected INDI device name; DEFAULT (the real mount) when nothing valid was ever chosen."""
    try:
        name = json.loads(Path(path or STATE_PATH).read_text(encoding="utf-8")).get("device")
    except (OSError, ValueError, AttributeError):
        return DEFAULT
    return name if name in CHOICES else DEFAULT


def is_simulator(name: Optional[str] = None) -> bool:
    return (current() if name is None else name) == SIMULATOR


def describe(path: Optional[Path] = None) -> Dict[str, Any]:
    name = current(path)
    return {"device": name, "simulator": name == SIMULATOR, "choices": list(CHOICES), "default": DEFAULT}


def set_device(name: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Stores the choice (atomic replace). The caller checks nothing is using the mount first."""
    if name not in CHOICES:
        raise ValueError(f"mount device must be one of {list(CHOICES)} (got {name!r})")
    target = Path(path or STATE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"device": name, "set_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
                   encoding="utf-8")
    os.replace(tmp, target)
    return describe(target)
