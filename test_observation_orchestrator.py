"""Tests for observation_orchestrator.py: RUN/STATUS/STOP core, process ownership,
signal-handling policy (SIGINT only, never SIGKILL/SIGTERM), and final reporting.

All subprocess launches and preflight probes are mocked — no real capture.py,
quicklook_live.py, or hardware is involved.
"""
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import observation_orchestrator as orch
import observation_plan
import runtime_state


def _fixture_resolved_plan(tmp_path, *, rfi_ref_enabled=False, quicklook_enabled=False,
                            planning_timestamp_utc=None, max_start_delay_minutes=60.0):
    grid_dir = tmp_path / "session"
    grid_dir.mkdir()
    mosaic = grid_dir / "mosaic.csv"
    mosaic.write_text("point_number\n1\n")
    plan = {
        "schema_version": 1,
        "observation_name": "TESTOBS",
        "planning_timestamp_utc": planning_timestamp_utc or datetime.now(timezone.utc).isoformat(),
        "resolved": {"mode": "EQUATORIAL_RECT", "placement": "FIXED_CENTER", "center_ra_hours": 6.0,
                     "center_dec_deg": -30.0, "rows": 3, "cols": 3, "spacing_deg": 2.0, "traversal": "SERPENTINE"},
        "requested": {
            "grid": {"min_altitude_deg": 10}, "capture": {"seconds": 1, "settle_seconds": 0.5},
            "main": {"center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 40.2, "bias_tee": True},
            "rfi_ref": {"enabled": rfi_ref_enabled, "serial": "00000002", "gain_db": 25.0},
            "quicklook": {"enabled": quicklook_enabled, "native_grid": True, "interpolated_preview": False,
                          "calibration_profile_path": "cal.json" if quicklook_enabled else None},
        },
        "main": {"center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 40.2, "bias_tee": True},
        "rfi_ref": {"enabled": rfi_ref_enabled, "serial": "00000002", "gain_db": 25.0, "port": 1235},
        "quicklook": {"enabled": quicklook_enabled, "native_grid": True, "interpolated_preview": False,
                      "calibration_profile_path": "cal.json" if quicklook_enabled else None},
        "grid_session_dir": str(grid_dir),
        "mosaic_csv_path": str(mosaic),
        "max_recommended_start_delay_minutes": max_start_delay_minutes,
    }
    plan["observation_config_sha256"] = observation_plan.recompute_config_hash(plan)
    resolved_path = grid_dir / "observation_resolved.json"
    resolved_path.write_text(json.dumps(plan))
    return str(resolved_path), plan


# --------------------------------------------------------------- load / staleness / tamper detection

def test_load_resolved_plan_detects_tampering(tmp_path):
    path, plan = _fixture_resolved_plan(tmp_path)
    on_disk = json.loads(open(path).read())
    on_disk["resolved"]["center_ra_hours"] = 99.0  # tamper without recomputing the hash
    open(path, "w").write(json.dumps(on_disk))
    with pytest.raises(orch.OrchestratorError, match="tamper"):
        orch._load_resolved_plan(path)


def test_check_stale_within_budget_ok(tmp_path):
    path, plan = _fixture_resolved_plan(tmp_path, max_start_delay_minutes=60.0)
    orch._check_stale(plan, now=datetime.now(timezone.utc) + timedelta(minutes=10))


def test_check_stale_past_budget_raises(tmp_path):
    path, plan = _fixture_resolved_plan(tmp_path, max_start_delay_minutes=5.0)
    with pytest.raises(orch.OrchestratorError, match="stale plan"):
        orch._check_stale(plan, now=datetime.now(timezone.utc) + timedelta(minutes=10))


# --------------------------------------------------------------- argument construction

def test_capture_args_include_rfi_ref_flags_only_when_enabled(tmp_path):
    _, plan = _fixture_resolved_plan(tmp_path, rfi_ref_enabled=True)
    args = orch._capture_args(plan, "/runtime")
    assert "--rfi-ref-enabled" in args
    assert "--rfi-ref-gain-db" in args
    assert "25.0" in args
    assert "--csv" in args and plan["mosaic_csv_path"] in args
    assert "--settle" in args and "0.5" in args
    assert "--capture" in args and "1" in args


def test_capture_args_omit_rfi_ref_flags_when_disabled(tmp_path):
    _, plan = _fixture_resolved_plan(tmp_path, rfi_ref_enabled=False)
    args = orch._capture_args(plan, "/runtime")
    assert "--rfi-ref-enabled" not in args


def test_quicklook_args_reference_calibration_profile(tmp_path):
    _, plan = _fixture_resolved_plan(tmp_path, quicklook_enabled=True)
    args = orch._quicklook_args(plan, "/session/root", "/session/quicklook", "/runtime")
    assert "--session-dir" in args and "/session/root" in args
    assert "--calibration-profile" in args and "cal.json" in args


# --------------------------------------------------------------- cmdline ownership verification

def test_cmdline_matches_current_process_for_a_real_substring():
    assert orch._cmdline_matches(os.getpid(), "pytest") or orch._cmdline_matches(os.getpid(), "python")


def test_cmdline_matches_false_for_nonexistent_pid():
    assert orch._cmdline_matches(999999999, "capture.py") is False


# --------------------------------------------------------------- run_observation: explicit GO required

class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _patch_common(monkeypatch, tmp_path, *, preflight_overall="PASS"):
    async def fake_preflight(plan, **kwargs):
        return {"overall": preflight_overall, "checks": [], "generated_utc": "now"}

    monkeypatch.setattr(orch.observation_preflight, "run_execution_preflight", fake_preflight)
    popen_calls = []

    def fake_popen(args, log_handle):
        popen_calls.append(args)
        return _FakeProc(pid=10000 + len(popen_calls))

    monkeypatch.setattr(orch, "_popen_detached", fake_popen)
    runtime_dir = tmp_path / "runtime"
    return popen_calls, str(runtime_dir)


def test_run_observation_blocked_preflight_launches_nothing(tmp_path, monkeypatch):
    path, plan = _fixture_resolved_plan(tmp_path)
    popen_calls, runtime_dir = _patch_common(monkeypatch, tmp_path, preflight_overall="BLOCK")
    with pytest.raises(orch.OrchestratorError, match="preflight BLOCKED"):
        orch.run_observation(path, yes=True, runtime_dir=runtime_dir)
    assert popen_calls == []
    state = runtime_state.read_json_safe(orch._runtime_path(runtime_dir))
    assert state["orchestrator_state"] == "FAILED"


def test_run_observation_warning_without_yes_requires_typed_go(tmp_path, monkeypatch):
    path, plan = _fixture_resolved_plan(tmp_path)
    popen_calls, runtime_dir = _patch_common(monkeypatch, tmp_path, preflight_overall="WARNING")
    with pytest.raises(orch.OrchestratorError, match="did not confirm GO"):
        orch.run_observation(path, yes=False, runtime_dir=runtime_dir, confirm_prompt=lambda _: "nope")
    assert popen_calls == []


def test_run_observation_warning_with_typed_go_proceeds(tmp_path, monkeypatch):
    path, plan = _fixture_resolved_plan(tmp_path)
    popen_calls, runtime_dir = _patch_common(monkeypatch, tmp_path, preflight_overall="WARNING")
    monkeypatch.setattr(orch, "_wait_for_session_announcement", lambda *a, **k: None)
    result = orch.run_observation(path, yes=False, runtime_dir=runtime_dir, confirm_prompt=lambda _: "GO")
    assert len(popen_calls) == 1
    assert result["orchestrator_state"] == "DEGRADED"  # session never announced in this test


def test_run_observation_refuses_double_start_while_running(tmp_path, monkeypatch):
    """A second START (double-click, browser retry) while an observation is
    already RUNNING must be refused before touching preflight/subprocess —
    never launch a second capture.py against the same or another plan."""
    path, plan = _fixture_resolved_plan(tmp_path)
    popen_calls, runtime_dir = _patch_common(monkeypatch, tmp_path, preflight_overall="PASS")
    monkeypatch.setattr(orch, "_wait_for_session_announcement",
                         lambda *a, **k: {"session_id": "SID-FIRST", "session_root": "/x"})
    # _popen_detached returns a fake pid in this test; make ownership
    # verification agree it's the real, matching, live process (as it
    # would be for a genuine subprocess.Popen pid in production).
    monkeypatch.setattr(orch, "_capture_ownership_matches", lambda runtime: True)

    first = orch.run_observation(path, yes=True, runtime_dir=runtime_dir)
    assert first["orchestrator_state"] == "RUNNING"
    assert len(popen_calls) == 1

    with pytest.raises(orch.OrchestratorError, match="already RUNNING"):
        orch.run_observation(path, yes=True, runtime_dir=runtime_dir)
    assert len(popen_calls) == 1  # still just the one from the first call


def test_run_observation_allows_restart_after_stale_dead_pid(tmp_path, monkeypatch):
    """If the runtime file claims RUNNING but the recorded pid is dead/doesn't
    match (e.g. after an orchestrator crash+restart), a new RUN must be
    allowed rather than wedged forever."""
    path, plan = _fixture_resolved_plan(tmp_path)
    popen_calls, runtime_dir = _patch_common(monkeypatch, tmp_path, preflight_overall="PASS")
    monkeypatch.setattr(orch, "_wait_for_session_announcement",
                         lambda *a, **k: {"session_id": "SID-NEW", "session_root": "/x"})
    orch._write_runtime(runtime_dir, orchestrator_state="RUNNING",
                         capture_pid=999999999, capture_start_time=1, capture_cmd_needle="capture.py")

    result = orch.run_observation(path, yes=True, runtime_dir=runtime_dir)
    assert result["orchestrator_state"] == "RUNNING"
    assert len(popen_calls) == 1


def test_run_lock_rejects_concurrent_start(tmp_path):
    runtime_dir = str(tmp_path / "runtime")
    with orch._run_lock(runtime_dir):
        with pytest.raises(orch.OrchestratorError, match="another RUN attempt"):
            with orch._run_lock(runtime_dir):
                pass  # never reached


def test_run_observation_yes_with_warning_proceeds_without_prompt(tmp_path, monkeypatch):
    path, plan = _fixture_resolved_plan(tmp_path)
    popen_calls, runtime_dir = _patch_common(monkeypatch, tmp_path, preflight_overall="WARNING")
    monkeypatch.setattr(orch, "_wait_for_session_announcement",
                         lambda *a, **k: {"session_id": "SID1", "session_root": "/x"})

    def boom_prompt(_):
        raise AssertionError("must not prompt when yes=True")

    result = orch.run_observation(path, yes=True, runtime_dir=runtime_dir, confirm_prompt=boom_prompt)
    assert result["orchestrator_state"] == "RUNNING"
    assert result["session_id"] == "SID1"


def test_run_observation_launches_quicklook_only_when_enabled(tmp_path, monkeypatch):
    path, plan = _fixture_resolved_plan(tmp_path, quicklook_enabled=True)
    popen_calls, runtime_dir = _patch_common(monkeypatch, tmp_path, preflight_overall="PASS")
    monkeypatch.setattr(orch, "_wait_for_session_announcement",
                         lambda *a, **k: {"session_id": "SID2", "session_root": "/session/root"})
    result = orch.run_observation(path, yes=True, runtime_dir=runtime_dir)
    assert len(popen_calls) == 2  # capture.py + quicklook_live.py
    assert result.get("quicklook_pid") is not None


def test_run_observation_session_announce_timeout_is_degraded_but_capture_already_launched(tmp_path, monkeypatch):
    path, plan = _fixture_resolved_plan(tmp_path, quicklook_enabled=True)
    popen_calls, runtime_dir = _patch_common(monkeypatch, tmp_path, preflight_overall="PASS")
    monkeypatch.setattr(orch, "_wait_for_session_announcement", lambda *a, **k: None)
    result = orch.run_observation(path, yes=True, runtime_dir=runtime_dir)
    assert len(popen_calls) == 1  # capture.py launched; quicklook never attempted (MAIN unaffected)
    assert result["orchestrator_state"] == "DEGRADED"


# --------------------------------------------------------------- ownership: PID recycling / identity

def _self_identity():
    """The current test process's own real (pid, start_time, a safe cmdline needle) —
    used to fabricate a runtime record that _capture_ownership_matches will
    genuinely, correctly evaluate against this live process."""
    pid = os.getpid()
    start_time = orch._proc_stat_start_time(pid)
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").replace("\x00", " ")
    needle = Path(cmdline.split(" ")[0]).name or cmdline.split(" ")[0]
    return pid, start_time, needle


def test_capture_ownership_matches_for_the_real_live_process():
    pid, start_time, needle = _self_identity()
    runtime = {"capture_pid": pid, "capture_start_time": start_time, "capture_cmd_needle": needle}
    assert orch._capture_ownership_matches(runtime) is True


def test_capture_ownership_rejects_pid_recycling_start_time_mismatch():
    """A live process whose recorded start_time doesn't match the real kernel
    value must be rejected — this is exactly what happens when a pid is
    recycled: same pid number, different (later) start_time."""
    pid, real_start_time, needle = _self_identity()
    runtime = {"capture_pid": pid, "capture_start_time": real_start_time - 12345, "capture_cmd_needle": needle}
    assert orch._capture_ownership_matches(runtime) is False


def test_capture_ownership_rejects_unrelated_manually_launched_process():
    """A live process that matches pid+start_time but was never launched by
    this orchestrator for THIS plan (cmdline/csv-path don't match) must be
    rejected — protects against confusing an operator's manual capture.py
    with the one the orchestrator itself launched."""
    pid, start_time, needle = _self_identity()
    runtime = {"capture_pid": pid, "capture_start_time": start_time,
               "capture_cmd_needle": "totally-unrelated-marker-not-in-our-cmdline"}
    assert orch._capture_ownership_matches(runtime) is False

    runtime2 = {"capture_pid": pid, "capture_start_time": start_time, "capture_cmd_needle": needle,
                "capture_expected_csv": "/some/other/session/mosaic.csv"}
    assert orch._capture_ownership_matches(runtime2) is False


def test_capture_ownership_rejects_dead_pid():
    assert orch._capture_ownership_matches(
        {"capture_pid": 999999999, "capture_start_time": 1, "capture_cmd_needle": "capture.py"}
    ) is False


def test_quicklook_ownership_independent_of_capture():
    pid, start_time, needle = _self_identity()
    matching = {"quicklook_pid": pid, "quicklook_start_time": start_time, "quicklook_cmd_needle": needle}
    assert orch._quicklook_ownership_matches(matching) is True
    mismatched = {"quicklook_pid": pid, "quicklook_start_time": start_time - 999, "quicklook_cmd_needle": needle}
    assert orch._quicklook_ownership_matches(mismatched) is False


# --------------------------------------------------------------- stop_observation: SIGINT-only, exact PID

def test_stop_observation_no_runtime_raises(tmp_path):
    with pytest.raises(orch.OrchestratorError, match="no orchestrator-owned observation"):
        orch.stop_observation(runtime_dir=str(tmp_path / "runtime"))


def test_stop_observation_refuses_to_signal_unverified_pid(tmp_path, monkeypatch):
    """Captura manual ajena / PID reciclado: an unverifiable pid must never
    be signaled at all."""
    runtime_dir = tmp_path / "runtime"
    orch._write_runtime(str(runtime_dir), orchestrator_state="RUNNING",
                         capture_pid=999999999, capture_start_time=1, capture_cmd_needle="capture.py")

    def boom_kill(*args, **kwargs):
        raise AssertionError("must never signal an unverified pid")

    monkeypatch.setattr(os, "kill", boom_kill)
    result = orch.stop_observation(runtime_dir=str(runtime_dir))
    assert result["orchestrator_state"] == "DEGRADED"


def test_stop_observation_sends_sigint_to_exact_pid_never_killpg(tmp_path, monkeypatch):
    pid, start_time, needle = _self_identity()
    runtime_dir = tmp_path / "runtime"
    orch._write_runtime(str(runtime_dir), orchestrator_state="RUNNING",
                         capture_pid=pid, capture_start_time=start_time, capture_cmd_needle=needle)
    signals_sent = []
    monkeypatch.setattr(os, "kill", lambda p, sig: signals_sent.append((p, sig)))

    def boom_killpg(*args, **kwargs):
        raise AssertionError("STOP must never call os.killpg")

    monkeypatch.setattr(os, "killpg", boom_killpg)
    alive_sequence = iter([True, False])
    monkeypatch.setattr(orch, "_pid_alive", lambda p: next(alive_sequence, False))
    monkeypatch.setattr(orch.time, "sleep", lambda s: None)
    runtime_state.atomic_write_json(runtime_dir / "current_session.json", {"state": "COMPLETED"})

    result = orch.stop_observation(runtime_dir=str(runtime_dir))
    assert signals_sent == [(pid, signal.SIGINT)]  # exactly the capture pid, exactly once
    assert result["orchestrator_state"] == "COMPLETED"


def test_stop_observation_signals_only_capture_and_matching_quicklook_never_a_third_pid(tmp_path, monkeypatch):
    """No RFI_REF pid exists in observation_runtime.json for the orchestrator
    to touch (RFI_REF is capture.py's own internal child, stopped by
    capture.py's own finally: block) — this test locks that invariant: the
    set of pids STOP ever signals is exactly {capture_pid[, quicklook_pid]},
    nothing else, ever."""
    pid, start_time, needle = _self_identity()
    runtime_dir = tmp_path / "runtime"
    orch._write_runtime(str(runtime_dir), orchestrator_state="RUNNING",
                         capture_pid=pid, capture_start_time=start_time, capture_cmd_needle=needle,
                         quicklook_pid=pid, quicklook_start_time=start_time, quicklook_cmd_needle=needle)
    signals_sent = []
    monkeypatch.setattr(os, "kill", lambda p, sig: signals_sent.append((p, sig)))
    monkeypatch.setattr(orch, "_pid_alive", lambda p: False)
    runtime_state.atomic_write_json(runtime_dir / "current_session.json", {"state": "COMPLETED"})

    orch.stop_observation(runtime_dir=str(runtime_dir))
    assert set(signals_sent) == {(pid, signal.SIGINT)}  # same real pid used for both roles here -> one entry
    assert all(sig == signal.SIGINT for _, sig in signals_sent)


def test_stop_observation_skips_quicklook_when_ownership_does_not_match(tmp_path, monkeypatch):
    pid, start_time, needle = _self_identity()
    runtime_dir = tmp_path / "runtime"
    orch._write_runtime(str(runtime_dir), orchestrator_state="RUNNING",
                         capture_pid=pid, capture_start_time=start_time, capture_cmd_needle=needle,
                         quicklook_pid=pid, quicklook_start_time=start_time - 999, quicklook_cmd_needle=needle)
    signals_sent = []
    monkeypatch.setattr(os, "kill", lambda p, sig: signals_sent.append((p, sig)))
    monkeypatch.setattr(orch, "_pid_alive", lambda p: False)
    runtime_state.atomic_write_json(runtime_dir / "current_session.json", {"state": "COMPLETED"})

    orch.stop_observation(runtime_dir=str(runtime_dir))
    # Only the one os.kill call for capture_pid — quicklook's mismatched
    # start_time means _quicklook_ownership_matches() is False, so it's
    # skipped even though quicklook_pid == capture_pid numerically here.
    assert signals_sent == [(pid, signal.SIGINT)]


def test_stop_observation_never_escalates_to_sigkill_on_timeout(tmp_path, monkeypatch):
    pid, start_time, needle = _self_identity()
    runtime_dir = tmp_path / "runtime"
    orch._write_runtime(str(runtime_dir), orchestrator_state="RUNNING",
                         capture_pid=pid, capture_start_time=start_time, capture_cmd_needle=needle)
    signals_sent = []
    monkeypatch.setattr(os, "kill", lambda p, sig: signals_sent.append((p, sig)))
    monkeypatch.setattr(orch, "_pid_alive", lambda p: True)  # never exits
    # Fast-forward the timeout loop instead of really sleeping ~120s.
    clock = {"t": 0.0}
    monkeypatch.setattr(orch.time, "monotonic", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += orch.STOP_WAIT_TIMEOUT_SEC  # jump straight past the deadline
    monkeypatch.setattr(orch.time, "sleep", fake_sleep)

    result = orch.stop_observation(runtime_dir=str(runtime_dir))
    assert signals_sent == [(pid, signal.SIGINT)]  # exactly one SIGINT, never a second/stronger signal
    assert result["orchestrator_state"] == "DEGRADED"
    assert "SIGKILL" in result["note"]


def test_no_killpg_or_sigkill_or_sigterm_signal_usage_in_orchestrator_source():
    # Prose mentioning "SIGKILL"/"SIGTERM"/"killpg" (e.g. explaining what is
    # refused) is fine; actually *calling* them is not.
    source = open("observation_orchestrator.py").read()
    assert "signal.SIGKILL" not in source
    assert "signal.SIGTERM" not in source
    assert "os.killpg(" not in source


# --------------------------------------------------------------- get_status: read-only merge

def test_get_status_merges_without_writing(tmp_path):
    runtime_dir = tmp_path / "runtime"
    orch._write_runtime(str(runtime_dir), orchestrator_state="RUNNING")
    runtime_state.atomic_write_json(runtime_dir / "current_session.json", {"session_id": "S1", "state": "RUNNING"})
    status = orch.get_status(runtime_dir=str(runtime_dir))
    assert status["orchestrator"]["orchestrator_state"] == "RUNNING"
    assert status["current_session"]["session_id"] == "S1"


# --------------------------------------------------------------- final report

def test_generate_final_report_counts_and_verdict(tmp_path, monkeypatch):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "mosaic.csv").write_text(
        "capture_status,visibility_deferred,data_filename\n"
        "success,False,a.dat\n"
        "failed,False,b.dat\n"
        "planned,True,c.dat\n"
    )
    (session_dir / "observation_resolved.json").write_text(json.dumps({
        "observation_name": "TESTOBS", "observation_config_sha256": "abc123",
        "requested": {}, "resolved": {},
    }))
    monkeypatch.setattr(orch, "validate_hdf5_capture", lambda path: {"ok": True})
    monkeypatch.setattr(orch, "_read_runtime", lambda runtime_dir: {"orchestrator_state": "COMPLETED", "session_id": "S1"})
    report = orch.generate_final_report(str(session_dir))
    assert report["points_success"] == 1
    assert report["points_failed"] == 1
    assert report["points_deferred"] == 1
    assert report["final_verdict"] == "DEGRADED"  # one failure -> not SUCCESS
    assert (session_dir / "final_report.json").exists()
    assert (session_dir / "final_report.md").exists()
