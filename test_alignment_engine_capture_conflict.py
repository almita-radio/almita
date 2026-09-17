"""Pre-hardware pass, item 4: preflight capture-conflict check must be
read-only, fail-closed, reuse ALMITA's existing PID-recycling-safe
ownership logic (observation_orchestrator.py), and never false-FAIL on
stale state (a crashed process's leftover runtime file, or PID reuse).
"""
import subprocess
import sys
import time

import pytest

import observation_orchestrator as orch
from alignment_engine.capture_conflict import check_no_conflicting_capture


def test_nothing_active_passes(tmp_path):
    runtime_dir = tmp_path / "runtime"
    result = check_no_conflicting_capture(runtime_dir=str(runtime_dir), proc_root=str(_empty_proc(tmp_path)))
    assert result.conflict is False


def test_orchestrator_reported_alive_capture_fails(tmp_path, monkeypatch):
    """Mirrors this repo's own test convention for a genuinely alive,
    ownership-verified capture (test_observation_orchestrator.py monkeypatches
    _capture_ownership_matches the same way for the identical reason: a real
    subprocess.Popen pid is what production actually has, not something a
    unit test needs to fork to prove the wiring is correct)."""
    runtime_dir = tmp_path / "runtime"
    orch._write_runtime(str(runtime_dir), orchestrator_state="RUNNING",
                         capture_pid=424242, capture_start_time=1,
                         capture_cmd_needle="capture.py", session_id="SID-LIVE")
    monkeypatch.setattr(orch, "_capture_ownership_matches", lambda runtime: True)

    result = check_no_conflicting_capture(runtime_dir=str(runtime_dir), proc_root=str(_empty_proc(tmp_path)))
    assert result.conflict is True
    assert "SID-LIVE" in result.detail


def test_stale_dead_pid_does_not_false_fail(tmp_path):
    """A runtime file claiming RUNNING with a pid that is not (or no longer)
    a real, matching capture.py process (the exact scenario
    test_observation_orchestrator.py's own
    test_get_status_never_reports_a_dead_capture_pid_as_alive covers for
    get_status() itself) must PASS here, not FAIL."""
    runtime_dir = tmp_path / "runtime"
    orch._write_runtime(str(runtime_dir), orchestrator_state="RUNNING",
                         capture_pid=999999999, capture_start_time=1,
                         capture_cmd_needle="capture.py")
    result = check_no_conflicting_capture(runtime_dir=str(runtime_dir), proc_root=str(_empty_proc(tmp_path)))
    assert result.conflict is False


def test_old_runtime_state_without_a_real_process_does_not_false_fail(tmp_path):
    """An old COMPLETED/FAILED/ABORTED runtime file (no live process implied
    at all) must never be read as a conflict."""
    runtime_dir = tmp_path / "runtime"
    for state in ("COMPLETED", "FAILED", "ABORTED", "PLANNED"):
        orch._write_runtime(str(runtime_dir), reset=True, orchestrator_state=state)
        result = check_no_conflicting_capture(runtime_dir=str(runtime_dir), proc_root=str(_empty_proc(tmp_path)))
        assert result.conflict is False, f"state={state} incorrectly reported as a conflict"


def test_running_state_with_no_capture_pid_yet_fails_closed(tmp_path):
    """A narrow race just after launch (orchestrator_state flipped to
    RUNNING before capture_pid is persisted) must fail closed rather than
    assume it's safe - "fail closed" per Fase 4's own instruction."""
    runtime_dir = tmp_path / "runtime"
    orch._write_runtime(str(runtime_dir), orchestrator_state="RUNNING")
    result = check_no_conflicting_capture(runtime_dir=str(runtime_dir), proc_root=str(_empty_proc(tmp_path)))
    assert result.conflict is True


def test_manually_launched_capture_outside_orchestrator_is_detected(tmp_path):
    """A capture.py started directly (not through the orchestrator) leaves
    no observation_runtime.json trace - only the read-only /proc cmdline
    scan can catch it. Spawns a real short-lived process whose script is
    literally named capture.py so its cmdline matches, exactly the same
    kind of controlled real-subprocess test
    test_observation_orchestrator_lifecycle.py already uses for its own
    liveness checks."""
    fake_capture = tmp_path / "capture.py"
    fake_capture.write_text("import time\ntime.sleep(5)\n")
    proc = subprocess.Popen([sys.executable, str(fake_capture)])
    try:
        time.sleep(0.2)  # let the child actually exec() before scanning /proc
        runtime_dir = tmp_path / "runtime"  # no orchestrator record at all
        result = check_no_conflicting_capture(runtime_dir=str(runtime_dir))
        assert result.conflict is True
        assert str(proc.pid) in result.detail
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_orchestrator_status_read_failure_fails_closed(tmp_path, monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("simulated read failure")

    monkeypatch.setattr(orch, "get_status", boom)
    result = check_no_conflicting_capture(runtime_dir=str(tmp_path / "runtime"))
    assert result.conflict is True
    assert "failing closed" in result.detail


def _empty_proc(tmp_path):
    """A guaranteed-empty fake /proc directory, so tests asserting
    conflict=False are never coupled to whatever unrelated processes
    happen to be running on the real machine running the test suite."""
    empty = tmp_path / "fake_proc"
    empty.mkdir(exist_ok=True)
    return empty
