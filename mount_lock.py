"""Only one process moves the mount at a time (BL-050: "INDI process lock").

The first MOVEMENT command a process sends (GOTO, SYNC, tracking ON, a tracking-mode change) claims the mount with an
flock on data/runtime/mount.lock and keeps it until the process exits (the kernel releases it even after a crash).
A movement from another process while it is held raises MountBusy naming the holder. Stopping never needs it:
abort and tracking OFF always go through (mount_stop.py, STOP MOUNT). Reading the mount never claims it.

ALMITA_MOUNT_LOCK=off disables the claim (emergency only); ALMITA_MOUNT_LOCK_PATH moves the file (tests)."""
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LOCK_PATH = Path(__file__).resolve().parent / "data" / "runtime" / "mount.lock"
_held: Optional[Tuple[int, Path]] = None


class MountBusy(RuntimeError):
    pass


def _path() -> Path:
    return Path(os.environ.get("ALMITA_MOUNT_LOCK_PATH") or LOCK_PATH)


def _read(fd: int) -> Dict[str, Any]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return json.loads(os.read(fd, 4096).decode() or "{}")
    except (OSError, ValueError):
        return {}


def claim(what: str = "movement") -> None:
    global _held
    if os.environ.get("ALMITA_MOUNT_LOCK", "").lower() == "off":
        return
    path = _path()
    if _held is not None and _held[1] == path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        other = _read(fd)
        os.close(fd)
        raise MountBusy(f"{what} refused: the mount is held by {other.get('owner', 'another process')} "
                        f"(pid {other.get('pid', '?')}, since {other.get('since_utc', '?')}) - stop that first")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps({"owner": " ".join(Path(a).name if i == 0 else a for i, a in enumerate(sys.argv[:3])),
                             "pid": os.getpid(), "since_utc": datetime.now(timezone.utc).isoformat()}).encode())
    _held = (fd, path)


def holder() -> Optional[Dict[str, Any]]:
    """Who holds the mount right now (None if nobody). Never keeps the lock."""
    if _held is not None and _held[1] == _path():
        return {**_read(_held[0]), "this_process": True}
    path = _path()
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        info = _read(fd)
        os.close(fd)
        return info or {"owner": "unknown"}
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return None
