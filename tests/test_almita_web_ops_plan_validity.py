"""almita_web_ops.py: PLAN_VALIDITY_SECONDS (180s) server-side enforcement for the REAL ALIGN "align" (RUN)
stage. build_command("align", ...) must independently re-derive and check the same deadline the web UI shows -
from the approved PLAN's own job record (server-recorded ended_utc), never from anything the request itself
claims about timing - and refuse to build the RUN command once it has passed, or if that PLAN never PASSed.

These tests call build_command() directly (it only validates input and builds a subprocess argv list - it
never starts alignment.py, moves anything, or touches a real mount/SDR) against synthetic job/result fixtures
under an isolated tmp_path (ROOT, DATA, OPS_DIR and SERVE_ROOTS["alignment"] are monkeypatched there for the
duration of each test) - never the project's real data/ directory or session data."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import almita_web_ops as ops


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    """Redirect every path almita_web_ops.py resolves against to tmp_path, so these tests never read or write
    the real project's data/ directory."""
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    monkeypatch.setattr(ops, "DATA", tmp_path / "data")
    monkeypatch.setattr(ops, "OPS_DIR", tmp_path / "data" / "runtime" / "web_ops")
    monkeypatch.setitem(ops.SERVE_ROOTS, "alignment", tmp_path / "data" / "alignment")
    return tmp_path


def _make_plan(tmp_path, plan_id="plan-fixture-001", *, ended_utc=None, result_status="PASS", write_job=True):
    """A synthetic, already-finished align_plan job: its real-shaped alignment_result.json artifact plus (by
    default) the job.json record build_command() looks up for its server-recorded completion time."""
    plan_dir_rel = f"data/alignment/{plan_id}"
    plan_dir = tmp_path / plan_dir_rel
    plan_dir.mkdir(parents=True)
    (plan_dir / "alignment_result.json").write_text(json.dumps({"result_status": result_status, "reference": "hi"}))
    if write_job:
        job_dir = ops.OPS_DIR / plan_id
        job_dir.mkdir(parents=True)
        job = {"job_id": plan_id, "stage": "align_plan", "meta": {"output_dir": plan_dir_rel},
               "started_utc": ended_utc, "ended_utc": ended_utc, "exit_code": 0}
        (job_dir / "job.json").write_text(json.dumps(job))
    return plan_dir_rel


def _utc(delta_seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def test_run_accepted_just_inside_the_180s_window(sandboxed):
    plan_rel = _make_plan(sandboxed, "plan-fresh-01", ended_utc=_utc(-170))
    argv, meta = ops.build_command("align", {"reference": "hi", "approved_plan_dir": plan_rel}, "run-1")
    assert meta["approved_plan_dir"] == plan_rel
    assert "--approved-plan" in argv


def test_run_rejected_just_past_the_180s_window(sandboxed):
    plan_rel = _make_plan(sandboxed, "plan-old-01", ended_utc=_utc(-190))
    with pytest.raises(ValueError, match=r"180s validity window"):
        ops.build_command("align", {"reference": "hi", "approved_plan_dir": plan_rel}, "run-2")


def test_run_rejected_well_past_the_window_with_clear_age_in_message(sandboxed):
    plan_rel = _make_plan(sandboxed, "plan-old-02", ended_utc=_utc(-600))
    with pytest.raises(ValueError, match=r"passed 60\d?s ago"):
        ops.build_command("align", {"reference": "hi", "approved_plan_dir": plan_rel}, "run-3")


def test_run_rejected_when_plan_did_not_pass_even_if_fresh(sandboxed):
    plan_rel = _make_plan(sandboxed, "plan-failed-01", ended_utc=_utc(-1), result_status="TEMPORAL ALTITUDE CHECK FAILED")
    with pytest.raises(ValueError, match=r"did not PASS"):
        ops.build_command("align", {"reference": "hi", "approved_plan_dir": plan_rel}, "run-4")


def test_run_rejected_when_no_job_record_exists_for_that_plan_dir(sandboxed):
    """A real alignment_result.json PASS artifact but no matching job.json (e.g. the job record was pruned, or
    someone points RUN at a directory build_command() never actually produced) - no server-recorded completion
    time to check the deadline against, so it must be refused rather than silently treated as fresh."""
    plan_rel = _make_plan(sandboxed, "plan-no-job-01", ended_utc=_utc(-1), write_job=False)
    with pytest.raises(ValueError, match=r"no server record of when that PLAN finished"):
        ops.build_command("align", {"reference": "hi", "approved_plan_dir": plan_rel}, "run-5")


def test_deadline_is_timed_from_the_plan_jobs_own_ended_utc_not_the_request(sandboxed):
    """The request carries no timing information of its own (no client-reported timestamp field exists in the
    RUN payload at all) - this just re-confirms the age is computed purely from the server's own record."""
    plan_rel = _make_plan(sandboxed, "plan-boundary-01", ended_utc=_utc(-179))
    ops.build_command("align", {"reference": "hi", "approved_plan_dir": plan_rel}, "run-6")   # does not raise
    plan_rel2 = _make_plan(sandboxed, "plan-boundary-02", ended_utc=_utc(-181))
    with pytest.raises(ValueError, match=r"180s validity window"):
        ops.build_command("align", {"reference": "hi", "approved_plan_dir": plan_rel2}, "run-7")
