"""Shared plumbing for the :8090 web app's ALIGN/CALIBRATE routes
(almita_orchestrator_server.py). No scientific logic lives here - only
response shaping, resource-ownership detection, session listing, and a
minimal background-job runner. Every real computation still goes through
alignment_engine/calibration_engine (or their CLI wrapper functions in
almita_align.py/almita_calibrate.py, called directly - never reimplemented).
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from runtime_state import read_json_safe


def envelope(data: Any = None, *, blocked: bool = False, reason: Optional[str] = None) -> Dict[str, Any]:
    """Fase 67's common response shape for the NEW align/calibrate routes
    only - the existing /api/observe/* routes keep their own established
    raw-JSON shape unchanged (Fase 50: do not touch OBSERVE)."""
    return {"ok": not blocked, "blocked": blocked, "reason": reason, "data": data}


class SDRResourceStatus(str, Enum):
    """Fase 46: the quicklook_live.py incident showed capture_conflict.py's
    own capture.py-only check is not sufficient. This is additive - it
    reuses capture_conflict and observation_orchestrator, never modifies
    them, and never kills or interferes with anything it finds."""
    FREE = "FREE"
    CLAIMED_BY_OBSERVATION = "CLAIMED_BY_OBSERVATION"
    CLAIMED_BY_QUICKLOOK = "CLAIMED_BY_QUICKLOOK"
    CLAIMED_BY_CALIBRATION = "CLAIMED_BY_CALIBRATION"
    UNKNOWN = "UNKNOWN"


@dataclass
class ResourceCheck:
    status: SDRResourceStatus
    detail: str
    orchestrator_state: Optional[str] = None
    session_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status.value, "detail": self.detail,
                "orchestrator_state": self.orchestrator_state, "session_id": self.session_id}


def _quicklook_live_running() -> bool:
    """Read-only /proc cmdline scan for quicklook_live.py - same primitive
    alignment_engine/capture_conflict.py already uses for capture.py, just
    a different needle. Never signals or touches the process it finds."""
    root = Path("/proc")
    try:
        candidates = [p for p in root.iterdir() if p.name.isdigit()]
    except OSError:
        return False
    for entry in candidates:
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if b"quicklook_live.py" in raw:
            return True
    return False


def get_sdr_resource_status(calibration_active: bool = False) -> ResourceCheck:
    """Read-only. Checks, in order: an active orchestrator campaign
    (RUNNING orchestrator_state, or a live capture.py/quicklook_live.py),
    then whether THIS process itself currently holds a calibration
    capture (passed in by the caller, since that state lives in the web
    app's own job table, not on disk). Fails toward UNKNOWN (not FREE) on
    any read error - never tell an operator MAIN is free when we could
    not actually check."""
    if calibration_active:
        return ResourceCheck(SDRResourceStatus.CLAIMED_BY_CALIBRATION,
                              "a calibration capture is currently running from this web app")
    try:
        from alignment_engine import capture_conflict
        conflict = capture_conflict.check_no_conflicting_capture()
    except Exception as exc:
        return ResourceCheck(SDRResourceStatus.UNKNOWN, f"could not check capture conflict: {exc}")
    if conflict.conflict:
        return ResourceCheck(SDRResourceStatus.CLAIMED_BY_OBSERVATION, conflict.detail)

    try:
        import observation_orchestrator
        status = observation_orchestrator.get_status()
        orchestrator_state = (status.get("orchestrator") or {}).get("orchestrator_state")
        session_id = (status.get("orchestrator") or {}).get("session_id")
    except Exception as exc:
        return ResourceCheck(SDRResourceStatus.UNKNOWN, f"could not read orchestrator status: {exc}")

    if orchestrator_state == "RUNNING":
        return ResourceCheck(SDRResourceStatus.CLAIMED_BY_OBSERVATION,
                              f"orchestrator campaign is RUNNING (session_id={session_id})",
                              orchestrator_state, session_id)

    if _quicklook_live_running():
        return ResourceCheck(SDRResourceStatus.CLAIMED_BY_QUICKLOOK,
                              "quicklook_live.py is running and may claim the rtl_tcp client slot at any time",
                              orchestrator_state)

    return ResourceCheck(SDRResourceStatus.FREE, "no conflicting acquisition, campaign, or quicklook process found",
                          orchestrator_state)


def resolve_within_root(root: str, user_path: str) -> Optional[Path]:
    """Fase 57: no arbitrary filesystem path. `user_path` may be an
    absolute path (as replay/compare bodies naturally carry, since they
    echo a session_dir a previous response already gave the caller) or a
    bare session id relative to `root` - either way, the RESOLVED result
    must land inside `root` or this returns None. Never raises on a
    malformed path; callers get a clean None to report as invalid."""
    root_path = Path(root).resolve()
    try:
        candidate = Path(user_path)
        candidate = candidate.resolve() if candidate.is_absolute() else (root_path / candidate).resolve()
    except (OSError, RuntimeError):
        return None
    if candidate != root_path and root_path not in candidate.parents:
        return None
    return candidate


def list_sessions(root: str, limit: int = 50, prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fase 36: cheap listing - directory names (already timestamp-sortable)
    plus each session's own small state.json, never a deep file walk.
    Newest first. `prefix` filters by directory-name prefix (e.g. "SOLAR-",
    "HI-", "CAL-") when the caller wants only one mode's sessions from a
    shared root directory."""
    path = Path(root)
    if not path.is_dir():
        return []
    names = [p.name for p in path.iterdir() if p.is_dir() and (prefix is None or p.name.startswith(prefix))]
    names = sorted(names, reverse=True)[:limit]
    rows = []
    for name in names:
        state = read_json_safe(path / name / "state.json") or {}
        rows.append({"session_id": name, "phase": state.get("phase") or state.get("state") or "UNKNOWN"})
    return rows


class JobRegistry:
    """Minimal background-job tracking (Fase 30) - a job IS a session (the
    session_id doubles as the job id), this registry only remembers
    whether a background thread for it is still alive, so a poll can tell
    "still running" apart from "process died without updating state.json".
    No daemon, no queue, no external dependency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: Dict[str, threading.Thread] = {}

    def start(self, job_id: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, name=f"web-job-{job_id}", daemon=True)
        with self._lock:
            self._threads[job_id] = thread
        thread.start()

    def register(self, job_id: str, thread: threading.Thread) -> None:
        """For a job whose thread was already started elsewhere (e.g. one
        that needed to observe its own generated session_id before this
        registry could know it) - just remembers it for is_alive()."""
        with self._lock:
            self._threads[job_id] = thread

    def is_alive(self, job_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(job_id)
        return bool(thread and thread.is_alive())

    def any_alive(self, prefix: Optional[str] = None) -> bool:
        """Fase A3: a small, non-duplicating addition - lets a status
        endpoint report "is a workflow of MINE currently running" (e.g.
        prefix="CAL-" for calibration jobs) without adding a second state
        store; this registry already knows every job's own thread."""
        with self._lock:
            items = list(self._threads.items())
        return any(thread.is_alive() for job_id, thread in items if prefix is None or job_id.startswith(prefix))


JOBS = JobRegistry()


def new_job_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
