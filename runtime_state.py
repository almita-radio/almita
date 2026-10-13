#!/usr/bin/env python3
"""Shared read/write-atomic primitives for ALMITA's resident field console.

Pure metadata side-channel. Never touches SDR, rtl_tcp, INDI, GOTO, tracking,
or HDF5. Imported by capture.py and quicklook_live.py (to announce lifecycle
events) and by almita_console_watcher.py (to read/write the canonical runtime
status files under data/runtime/).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1
SESSION_STATES = ("IDLE", "STARTING", "RUNNING", "COMPLETED", "DEGRADED", "ABORTED")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, value: Any) -> None:
    """Write JSON atomically: .tmp -> fsync -> os.replace().

    Never leaves a partially written final file, even on crash mid-write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def read_json_safe(path: Path) -> Optional[dict]:
    """Read a JSON object; return None on missing/malformed/non-dict content.

    Never raises - callers must be able to survive absent or partial files.
    """
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def announce_session(runtime_dir: Path, session_id: str, **fields) -> None:
    """Best-effort atomic upsert of current_session.json for one session_id.

    Fields from a prior announcement are preserved across calls that share
    the same session_id (so e.g. a POINT_STARTED announcement only needs to
    send what changed); a different session_id always starts from a clean
    slate. This function never raises: capture/quicklook must keep running
    even if the console side-channel fails (disk full, permissions, etc.).
    """
    try:
        path = Path(runtime_dir) / "current_session.json"
        existing = read_json_safe(path) or {}
        if existing.get("session_id") != session_id:
            existing = {}
        merged = {**existing, **fields, "session_id": session_id,
                  "schema_version": SCHEMA_VERSION, "updated_utc": utcnow()}
        atomic_write_json(path, merged)
    except Exception:
        pass


# ------------------------------------------------------------------ "is capture.py running?" (process scan)
_PYTHON_NAMES = ("python", "python3")


def is_script_cmdline(raw_cmdline: bytes, script: str = "capture.py") -> bool:
    """True only when this /proc/<pid>/cmdline EXECUTES `script`: argv[0] is the script itself, or argv[0] is a
    python interpreter whose first non-option argument is the script (by file name, any directory). A plain
    substring test also matched sdr_capture.py, pytest running tests/test_*capture*.py, `bash -c "...capture.py..."`
    or `grep capture.py` - each a false "capture in progress" that blocked the SDR and the web gates."""
    argv = [a.decode(errors="replace") for a in raw_cmdline.split(b"\0") if a]
    if not argv:
        return False
    if os.path.basename(argv[0]) == script:
        return True
    exe = os.path.basename(argv[0])
    if not (exe in _PYTHON_NAMES or (exe.startswith("python3.") and exe[8:].isdigit())):
        return False
    for arg in argv[1:]:
        if arg in ("-m", "-c"):
            return False                      # a module or inline code, never the script file
        if arg.startswith("-"):
            continue                          # interpreter options (-u, -B, -X ..., -W ...)
        return os.path.basename(arg) == script
    return False
