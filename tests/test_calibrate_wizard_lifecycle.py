"""CALIBRATE Reference Wizard: ABORT / STOP / errors / interrupted steps / new session.

No hardware: the 50 ohm step runs with its SIMULATED backend, the HI step's capture is replaced, rtl_tcp probes
are stubbed, web-ops jobs use a temporary job dir and never spawn a real runner. Covers what the operator needs:
a stopped or failed step leaves the session where it was with its files kept, a retry never overwrites them,
ABORT works from any step (also while MAIN is busy) and a new session can start right after."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import almita_web_ops as ops
import calibrate_reference_wizard as wiz
from calibration_engine import acquisition, hardware_inspection


class _Probe:
    def to_dict(self):
        return {"stub": True}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(hardware_inspection, "probe_rtl_tcp_handshake", lambda *a, **k: _Probe())
    monkeypatch.setattr(hardware_inspection, "inspect_rtl_tcp_service_command_line", lambda *a, **k: _Probe())
    monkeypatch.setattr(wiz, "bias_t_known_facts", lambda **k: {"note": "stub", "safety": "stub"})
    root = tmp_path / "calibration"

    def new_session():
        args = SimpleNamespace(session_root=str(root), n_captures=4, capture_seconds=0.05, stabilize_seconds=0,
                               hi_settle_seconds=0, center_freq=1420405752.0, sample_rate=2400000.0, gain=40.2,
                               clipping_threshold=1e-4, stability_threshold=0.1, rfi_threshold=0.5, min_elevation=20.0,
                               beam_fwhm=20.0, significance_threshold=3.0)
        assert wiz.cmd_start(args) == 0
        [d] = [p for p in root.iterdir() if p not in new_session.seen]
        new_session.seen.add(d)
        wiz.cmd_set_reference(SimpleNamespace(session_dir=str(d), connection_point="LNA_INPUT"))
        return d
    new_session.seen = set()
    return new_session


def _state(d):
    return json.loads((d / "wizard_state.json").read_text())


def _interrupt_capture_after(monkeypatch, n_ok, exc):
    real = acquisition.SimulatedCalibrationAcquisitionBackend.capture
    calls = {"n": 0}

    async def capture(self, **kw):
        if calls["n"] >= n_ok:
            raise exc
        calls["n"] += 1
        return await real(self, **kw)
    monkeypatch.setattr(acquisition.SimulatedCalibrationAcquisitionBackend, "capture", capture)


def _capture_50r(d):
    return wiz.cmd_capture_50r(SimpleNamespace(session_dir=str(d), simulate="HEALTHY"))


def test_stop_during_50r_capture_keeps_step_and_files_then_retry_never_overwrites_them(session, monkeypatch):
    d = session()
    _interrupt_capture_after(monkeypatch, 2, KeyboardInterrupt())       # operator STOP = SIGINT
    with pytest.raises(KeyboardInterrupt):
        _capture_50r(d)
    st = _state(d)
    assert st["step"] == "STABILIZE_50R"                                 # retry or abort from here
    assert st["last_interruption"]["kind"] == "STOPPED_BY_OPERATOR"
    assert st["last_interruption"]["files_on_disk"] == ["capture_000.h5", "capture_001.h5"]
    first = {p.name: p.read_bytes() for p in (d / "captures" / "AMBIENT_50R").glob("capture_*.h5")}

    monkeypatch.undo()
    _capture_50r(d)                                                      # retry: completes
    st = _state(d)
    assert st["step"] == "RESULT_50R" and "last_interruption" not in st
    [attempt] = st["interrupted_attempts"]
    kept = Path(attempt["kept_in"])
    assert attempt["files"] == ["capture_000.h5", "capture_001.h5"] and kept.parent.name == "AMBIENT_50R"
    assert {p.name: p.read_bytes() for p in kept.iterdir()} == first      # the interrupted captures, byte for byte
    assert len(list((d / "captures" / "AMBIENT_50R").glob("capture_*.h5"))) == 4


def test_failed_50r_capture_is_recorded_as_failed_with_the_error(session, monkeypatch):
    d = session()
    _interrupt_capture_after(monkeypatch, 1, ConnectionError("rtl_tcp went away"))
    with pytest.raises(SystemExit, match="AMBIENT_50R capture FAILED .*rtl_tcp went away"):    # explicit, readable exit
        _capture_50r(d)
    li = _state(d)["last_interruption"]
    assert (li["kind"], li["step"], li["files_on_disk"]) == ("FAILED", "AMBIENT_50R", ["capture_000.h5"])
    assert "rtl_tcp went away" in li["error"]


def test_abort_from_any_step_keeps_files_is_idempotent_and_a_new_session_can_start(session, monkeypatch):
    d = session()
    _interrupt_capture_after(monkeypatch, 1, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _capture_50r(d)
    wiz.cmd_abort(SimpleNamespace(session_dir=str(d)))
    st = _state(d)
    assert (st["step"], st["aborted"], st["aborted_from_step"]) == ("ABORTED", True, "STABILIZE_50R")
    assert (d / "captures" / "AMBIENT_50R" / "capture_000.h5").is_file()  # nothing deleted
    first_abort = st["aborted_utc"]
    wiz.cmd_abort(SimpleNamespace(session_dir=str(d)))                    # twice: no-op
    assert _state(d)["aborted_utc"] == first_abort
    d2 = session()                                                        # a new session right after
    assert d2 != d and _state(d2)["step"] == "STABILIZE_50R"


def test_abort_of_a_done_session_is_refused(session):
    d = session()
    st = _state(d)
    st["step"] = "DONE"
    (d / "wizard_state.json").write_text(json.dumps(st))
    with pytest.raises(SystemExit, match="already DONE"):
        wiz.cmd_abort(SimpleNamespace(session_dir=str(d)))


def test_a_capture_that_ends_after_an_abort_never_overwrites_the_abort(session, monkeypatch):
    d = session()
    real = acquisition.SimulatedCalibrationAcquisitionBackend.capture

    async def capture(self, **kw):
        out = await real(self, **kw)
        if kw["output_path"].endswith("capture_001.h5"):                  # abort lands mid-capture (other process)
            wiz.cmd_abort(SimpleNamespace(session_dir=str(d)))
        return out
    monkeypatch.setattr(acquisition.SimulatedCalibrationAcquisitionBackend, "capture", capture)
    with pytest.raises(SystemExit, match="ABORTED while this capture ran"):
        _capture_50r(d)
    assert _state(d)["step"] == "ABORTED"
    assert len(list((d / "captures" / "AMBIENT_50R").glob("capture_*.h5"))) == 4      # files kept


def test_hi_capture_failure_keeps_ready_step_and_records_it(session, monkeypatch):
    from tests.test_hi_plan_preview import PLAN
    d = session()
    st = _state(d)
    st.update(step="READY_HI_ALTO", hi_plan=PLAN, hi_plan_approved_utc="2026-10-02T01:36:00+00:00")
    (d / "wizard_state.json").write_text(json.dumps(st))

    async def boom(*a, **k):
        raise RuntimeError("mount is not at the target after settling")
    monkeypatch.setattr(wiz, "_simulate_capture_n_at", boom)
    with pytest.raises(SystemExit, match="HI_ALTO movement FAILED"):
        wiz.cmd_capture_hi(SimpleNamespace(session_dir=str(d), label="HI_ALTO", simulate="HEALTHY"))
    st = _state(d)
    assert st["step"] == "READY_HI_ALTO" and st["last_interruption"]["kind"] == "FAILED"


# ------------------------------------------------------------------ web ops: routing and the same-session guard

@pytest.fixture
def jobs(tmp_path, monkeypatch):
    cal = tmp_path / "data" / "calibration"
    (cal / "WIZ-1").mkdir(parents=True)
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    monkeypatch.setattr(ops, "OPS_DIR", tmp_path / "data" / "runtime" / "web_ops")
    monkeypatch.setitem(ops.SERVE_ROOTS, "calibration", cal)
    spawned = []
    monkeypatch.setattr(ops.subprocess, "Popen", lambda argv, **k: spawned.append(argv))
    monkeypatch.setattr(ops, "get_job", lambda job_id, tail=20: {"job_id": job_id})

    class Busy:
        status = SimpleNamespace(value="BUSY")
        detail = "an observation owns MAIN"
    import almita_web_common
    monkeypatch.setattr(almita_web_common, "get_sdr_resource_status", lambda: Busy())
    return tmp_path, spawned


def _finish_all_jobs():
    for f in ops.OPS_DIR.glob("*/job.json"):
        j = json.loads(f.read_text())
        j["exit_code"] = 0
        f.write_text(json.dumps(j))


def _fake_running(tmp_path, stage, action, output_dir, monkeypatch):
    d = ops.OPS_DIR / f"{stage.upper()}-X"
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({"job_id": d.name, "stage": stage, "meta": {"action": action, "output_dir": output_dir},
                                            "runner_pid": 1, "started_epoch": 0, "log": str(d / "job.log"), "argv": []}))
    monkeypatch.setattr(ops, "_alive", lambda pid, marker: True)
    return d.name


def test_abort_and_other_state_actions_work_while_main_is_busy(jobs):
    tmp_path, spawned = jobs
    for action in ("abort", "status", "next", "finish"):
        ops.start("calibrate_wizard", {"action": action, "session_dir": "data/calibration/WIZ-1"})
        _finish_all_jobs()                                                # each click's job ends before the next
    stages = sorted({p.parent.name.split("-")[0] for p in ops.OPS_DIR.glob("*/job.json")})
    assert stages == ["CALIBRATE_WIZARD_STATE"] and len(spawned) == 4


def test_sdr_actions_still_need_a_free_main(jobs):
    with pytest.raises(ops.OpsBlocked, match="MAIN SDR is not free"):
        ops.start("calibrate_wizard", {"action": "capture_50r", "session_dir": "data/calibration/WIZ-1"})
    with pytest.raises(ValueError, match="uses MAIN"):
        ops.build_command("calibrate_wizard_state", {"action": "capture_50r", "session_dir": "data/calibration/WIZ-1"}, "J")


def test_a_running_step_must_be_stopped_before_another_action_on_the_same_session(jobs, monkeypatch):
    tmp_path, spawned = jobs
    running = _fake_running(tmp_path, "calibrate_wizard_move", "capture_hi", "data/calibration/WIZ-1", monkeypatch)
    with pytest.raises(ops.OpsBlocked, match=f"{running} is still running on this session - STOP it first"):
        ops.start("calibrate_wizard", {"action": "abort", "session_dir": "data/calibration/WIZ-1"})
    ops.start("calibrate_wizard", {"action": "status", "session_dir": "data/calibration/WIZ-1"})   # reading is fine
    (tmp_path / "data" / "calibration" / "WIZ-2").mkdir()
    ops.start("calibrate_wizard", {"action": "abort", "session_dir": "data/calibration/WIZ-2"})    # another session too


# ------------------------------------------------------------------ 2026-10-07 incident: real 50 ohm capture failures

def test_successful_50r_capture_completes_the_step_with_no_failure_left(session):
    d = session()
    assert _capture_50r(d) in (0, 2)
    st = _state(d)
    assert st["step"] == "RESULT_50R" and st["fifty_ohm"]["status"] == "DONE" and st["fifty_ohm_result"]
    assert "last_interruption" not in st


def test_a_capture_that_never_finishes_times_out_is_recorded_and_not_retried(session, monkeypatch):
    import asyncio
    d = session()
    monkeypatch.setattr(wiz, "capture_step_timeout_seconds", lambda *a, **k: 0.3)

    async def hang(self, **kw):
        await asyncio.sleep(30)
    monkeypatch.setattr(acquisition.SimulatedCalibrationAcquisitionBackend, "capture", hang)
    monkeypatch.setenv("ALMITA_JOB_ID", "CALIBRATE_WIZARD-TEST-0001")
    with pytest.raises(SystemExit, match="AMBIENT_50R capture TIMEOUT .*no result within 0 s"):
        _capture_50r(d)
    st = _state(d)
    assert st["step"] == "STABILIZE_50R" and st["fifty_ohm"]["status"] == "PENDING_CAPTURE"
    assert (st["last_interruption"]["kind"], st["last_interruption"]["job_id"]) == ("TIMEOUT", "CALIBRATE_WIZARD-TEST-0001")


def test_a_stale_main_device_fails_before_touching_the_sdr_with_the_reason(session, monkeypatch):
    import sdr_tuning
    d = session()
    monkeypatch.setattr(sdr_tuning, "main_device_reenumerated_since_rtl_tcp_start",
                        lambda *a, **k: "the MAIN SDR (USB serial 00000001) was re-attached by the kernel at X")
    touched = []
    monkeypatch.setattr(acquisition, "RealCalibrationAcquisitionBackend", lambda **k: touched.append(1))
    with pytest.raises(SystemExit, match="AMBIENT_50R capture FAILED .*re-attached by the kernel"):
        wiz.cmd_capture_50r(SimpleNamespace(session_dir=str(d), simulate=None))
    assert touched == [] and _state(d)["last_interruption"]["kind"] == "FAILED"


def test_web_start_and_preflight_block_a_stale_main_device(jobs, monkeypatch):
    import almita_web_common
    import sdr_tuning

    class Free:
        status = SimpleNamespace(value="FREE")
        detail = ""
    monkeypatch.setattr(almita_web_common, "get_sdr_resource_status", lambda: Free())
    monkeypatch.setattr(sdr_tuning, "main_device_reenumerated_since_rtl_tcp_start", lambda *a, **k: "re-attached at X")
    with pytest.raises(ops.OpsBlocked, match="MAIN SDR cannot be driven: re-attached at X"):
        ops.start("calibrate_wizard", {"action": "capture_50r", "session_dir": "data/calibration/WIZ-1"})
    check = ops.main_sdr_device_handle_check()
    assert (check["status"], check["detail"]) == ("BLOCK", "re-attached at X")


def test_the_job_facts_carry_the_failure_so_the_page_can_show_it(tmp_path, monkeypatch):
    """The status job prints the session state; its facts must include last_interruption (it was dropped, so the
    page never showed why a capture had failed)."""
    state = {"session_id": "WIZ-1", "step": "STABILIZE_50R", "config": {}, "fifty_ohm": {"status": "PENDING_CAPTURE"},
             "last_interruption": {"step": "AMBIENT_50R", "kind": "FAILED", "utc": "2026-10-07T00:14:14Z",
                                   "error": "SDRDisconnected: Connection reset by peer", "files_on_disk": [],
                                   "job_id": "CALIBRATE_WIZARD-20261007-001408-816a"}}
    log = tmp_path / "job.log"
    log.write_text(json.dumps({"session_dir": "data/calibration/WIZ-1", "state": state}, indent=2))
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    (tmp_path / "data" / "calibration" / "WIZ-1").mkdir(parents=True)
    job = {"job_id": "S1", "stage": "calibrate_wizard_state", "meta": {"action": "status", "output_dir": "data/calibration/WIZ-1"},
           "log": str(log), "exit_code": 0, "argv": [], "params": {}, "physical": False, "started_utc": "2026-10-07T00:15:00+00:00"}
    facts = ops.classify(job)["facts"]
    assert facts["last_interruption"]["job_id"] == "CALIBRATE_WIZARD-20261007-001408-816a"
    assert facts["last_interruption"]["error"].startswith("SDRDisconnected")


def test_jobs_are_listed_newest_first_by_start_time_across_stages(jobs):
    """2026-10-07: the page took the newest job for the wizard's current state, but list_jobs sorted by job id, so
    every CALIBRATE_WIZARD_STATE-* came before a newer CALIBRATE_WIZARD-* and a successful capture was hidden."""
    for name, stage, epoch in (("CALIBRATE_WIZARD_STATE-20261007-001415-6e58", "calibrate_wizard_state", 100.0),
                               ("CALIBRATE_WIZARD-20261007-002617-dc9d", "calibrate_wizard", 200.0),
                               ("REDUCE-20261007-000000-aaaa", "reduce", 50.0)):
        d = ops.OPS_DIR / name
        d.mkdir(parents=True)
        (d / "job.json").write_text(json.dumps({"job_id": name, "stage": stage, "started_epoch": epoch, "exit_code": 0,
                                                "log": str(d / "job.log"), "argv": [], "meta": {}, "params": {}}))
    assert [r["job_id"] for r in ops.list_jobs()] == ["CALIBRATE_WIZARD-20261007-002617-dc9d",
                                                      "CALIBRATE_WIZARD_STATE-20261007-001415-6e58",
                                                      "REDUCE-20261007-000000-aaaa"]
