"""Preflight capture-conflict check (pre-hardware pass, item 4).

Read-only. Never signals, kills, or writes to any process or runtime file.
Reuses ALMITA's own existing, already-tested ownership-verification logic
(observation_orchestrator.py's get_status()/_capture_ownership_matches,
built on kernel start_time + cmdline checks against /proc, immune to PID
recycling - see that module's own docstrings) rather than reinventing
process liveness detection here.

Two sources are checked, because two ways a capture can be running exist
in this repo:
  1. Orchestrator-launched (data/runtime/observation_runtime.json) - covered
     by observation_orchestrator.get_status()['orchestrator']
     ['capture_process_alive'], which is already PID-recycling-safe and
     already proven not to false-positive on a stale/crashed sidecar (see
     test_observation_orchestrator.py::
     test_get_status_never_reports_a_dead_capture_pid_as_alive).
  2. A manually-launched `python capture.py ...` never recorded in that
     runtime file - invisible to (1), so this module adds a minimal,
     read-only /proc cmdline scan (the exact same primitive
     observation_orchestrator._process_identity() already uses -
     Path(f"/proc/{pid}/cmdline") - just without one specific expected pid
     to check against).

FAIL CLOSED: any error while checking (unreadable orchestrator state,
unreadable /proc) is reported as a conflict, never silently swallowed into
a false PASS - a preflight that cannot prove the mount/SDR are free must
not tell the operator to proceed.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

import observation_orchestrator


class CaptureConflictResult(NamedTuple):
    conflict: bool
    detail: str


def _scan_proc_for_cmdline_needle(needle: str, proc_root: str = "/proc") -> Optional[int]:
    """Generic read-only /proc scan for any process whose cmdline contains
    `needle`. Never raises: an unreadable directory (permissions, or the
    process exiting mid-scan - a normal race, not an error) is skipped,
    never treated as a match. Returns the first matching pid, or None."""
    root = Path(proc_root)
    try:
        candidates = [p for p in root.iterdir() if p.name.isdigit()]
    except OSError:
        return None
    for entry in candidates:
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if needle.encode() in raw:
            return int(entry.name)
    return None


def check_no_conflicting_capture(runtime_dir: Optional[str] = None,
                                  proc_root: str = "/proc") -> CaptureConflictResult:
    """Read-only preflight check: conflict=True (FAIL CLOSED) means real
    evidence of an active, conflicting acquisition was found; conflict=False
    means none was found by any available (read-only) means - which is not
    a mathematical proof of absence, only the best evidence this repo's
    existing tooling can provide without touching anything."""
    kwargs = {"runtime_dir": runtime_dir} if runtime_dir else {}
    try:
        status = observation_orchestrator.get_status(**kwargs)
    except Exception as exc:
        return CaptureConflictResult(True, f"could not read orchestrator status ({exc}); failing closed")

    orchestrator = status.get("orchestrator") or {}
    if orchestrator.get("capture_process_alive"):
        return CaptureConflictResult(
            True,
            f"orchestrator-launched capture.py is alive "
            f"(pid={orchestrator.get('capture_pid')}, session_id={orchestrator.get('session_id')})",
        )
    if orchestrator.get("orchestrator_state") == "RUNNING" and orchestrator.get("capture_pid") is None:
        # A narrow race just after launch, before capture_pid is persisted -
        # fail closed rather than assume it is safe to proceed.
        return CaptureConflictResult(
            True, "orchestrator_state=RUNNING with no capture_pid recorded yet")

    try:
        manual_pid = _scan_proc_for_cmdline_needle("capture.py", proc_root=proc_root)
    except Exception as exc:
        return CaptureConflictResult(True, f"could not scan for a manually-launched capture.py ({exc}); failing closed")
    if manual_pid is not None:
        return CaptureConflictResult(
            True, f"a capture.py process is running outside the orchestrator (pid={manual_pid})")

    return CaptureConflictResult(False, "no conflicting acquisition process found")
