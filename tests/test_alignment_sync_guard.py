"""ALIGN SYNC (operator decision 2026-10-08: Sun and HI): alignment.py's one guarded SYNC path, and the web side
(the RUN command asks for SYNC only when the operator ticks it; the job result always carries the offset).
No hardware: a fake telescope records what would have been sent; web tests use a tmp_path sandbox."""
import asyncio
import json
import types

import astropy.units as u
from astropy.coordinates import SkyCoord

import alignment
import almita_web_ops as ops
from tests.test_almita_web_ops_plan_validity import _make_plan, _utc, sandboxed  # noqa: F401 (fixture)


class FakeTelescope:
    def __init__(self, goto_ok=True, sync_ok=True):
        self.calls, self.goto_ok, self.sync_ok = [], goto_ok, sync_ok
        self.ra, self.dec = 1.0, -30.0

    async def goto(self, ra, dec):
        self.calls.append(("goto", ra, dec))
        if self.goto_ok:
            self.ra, self.dec = ra, dec
        return self.goto_ok

    async def sync(self, ra, dec):
        self.calls.append(("sync", ra, dec))
        return self.sync_ok

    async def get_coordinates(self, force_refresh=False):
        return self.ra, self.dec


def _runner(apply_sync=True, threshold=0.65, max_off=5.0, telescope=None):
    r = object.__new__(alignment.AlignmentRunner)
    r.args = types.SimpleNamespace(apply_sync=apply_sync, confidence_threshold=threshold,
                                   max_sync_offset_deg=max_off, settle=0)
    r.telescope = telescope or FakeTelescope()
    return r


def _estimate(east=0.8, north=-0.5, confidence=0.9):
    center = SkyCoord(ra=1.0 * u.hourangle, dec=-30 * u.deg)
    moved = alignment.offset_coordinates(center, [east], [north])[0]
    return center, alignment.AlignmentEstimate(east, north, float(center.separation(moved).deg), confidence, 0.1, 0.9, 25)


def _sync(runner, center, estimate):
    selection = {}
    applied = asyncio.run(runner.maybe_sync(center, estimate, selection))
    return applied, runner.sync_decision, selection


def test_sync_sent_when_requested_confident_and_small():
    center, est = _estimate()
    r = _runner()
    applied, decision, selection = _sync(r, center, est)
    assert applied and decision["applied"] and decision["requested"]
    kinds = [c[0] for c in r.telescope.calls]
    assert kinds[:2] == ["goto", "sync"]                       # compensated GOTO, then SYNC to the reference
    assert r.telescope.calls[1][1:] == (center.ra.hour, center.dec.deg)
    comp = r.telescope.calls[0]
    expected = alignment.offset_coordinates(center, [-est.offset_ra_deg], [-est.offset_dec_deg])[0]
    assert abs(comp[1] - expected.ra.hour) < 1e-9 and abs(comp[2] - expected.dec.deg) < 1e-9
    assert "post_sync_repeatability" in selection


def test_sync_not_sent_when_not_requested_low_confidence_or_too_large():
    center, est = _estimate()
    for runner, est_, why in ((_runner(apply_sync=False), est, "not requested"),
                              (_runner(), _estimate(confidence=0.5)[1], "below threshold"),
                              (_runner(max_off=0.5), est, "larger than the 0.5 deg SYNC limit")):
        applied, decision, _ = _sync(runner, center, est_)
        assert not applied and not decision["applied"] and why in decision["reason"]
        assert runner.telescope.calls == []                     # nothing moved, nothing sent


def test_no_estimate_and_failed_goto_never_sync():
    center, est = _estimate()
    applied, decision, _ = _sync(_runner(), center, None)
    assert not applied and decision["reason"] == "no offset estimated"
    r = _runner(telescope=FakeTelescope(goto_ok=False))
    applied, decision, _ = _sync(r, center, est)
    assert not applied and "compensated GOTO failed" in decision["reason"]
    assert [c[0] for c in r.telescope.calls] == ["goto"]       # no SYNC after a failed GOTO


def test_cli_max_sync_offset_default_and_bounds():
    assert alignment.parse_args([]).max_sync_offset_deg == alignment.DEFAULT_MAX_SYNC_OFFSET_DEG == 5.0
    import pytest
    with pytest.raises(SystemExit):
        alignment.parse_args(["--max-sync-offset-deg", "20"])


def test_web_run_requests_sync_only_when_ticked(sandboxed):  # noqa: F811
    plan = _make_plan(sandboxed, "plan-sync-01", ended_utc=_utc(-10))
    argv, meta = ops.build_command("align", {"reference": "sun", "approved_plan_dir": plan}, "r1")
    assert "--no-sync" in argv and "--apply-sync" not in argv and "sync_requested" not in meta
    argv, meta = ops.build_command("align", {"reference": "sun", "approved_plan_dir": plan, "sync": True,
                                             "max_sync_offset_deg": 3}, "r2")
    assert "--apply-sync" in argv and "--no-sync" not in argv
    assert argv[argv.index("--max-sync-offset-deg") + 1] == "3.0" and meta["sync_requested"] is True
    argv, _ = ops.build_command("align_plan", {"reference": "sun", "sync": True}, "r3")
    assert "--no-sync" in argv and "--dry-run" in argv          # PLAN never syncs


def _classify_run(tmp_path, result):
    out = tmp_path / "data" / "alignment" / "WEB-SUN-x"
    out.mkdir(parents=True)
    (out / "alignment_result.json").write_text(json.dumps(result))
    log = tmp_path / "job.log"
    log.write_text("")
    job = {"job_id": "j", "stage": "align", "meta": {"output_dir": "data/alignment/WEB-SUN-x"}, "log": str(log),
           "exit_code": 0, "started_utc": _utc(-60), "ended_utc": _utc(0)}
    return ops.classify(job)


def test_result_always_shows_offset_and_sync_outcome(sandboxed):  # noqa: F811
    offsets = {"offset_ra_deg": 0.8, "offset_dec_deg": -0.5, "separation_deg": 0.94, "confidence": 0.9,
               "residual": 0.1, "score": 0.9, "samples": 25}
    base = {"reference": "sun", "measured_positions": [{"status": "VALID"}] * 25, "offsets": offsets}
    c = _classify_run(sandboxed, {**base, "result_status": "PASS", "sync_applied": True,
                                  "sync_decision": {"requested": True, "applied": True, "reason": "sent"}})
    assert c["verdict"] == "PASS" and c["facts"]["offset"]["separation_deg"] == 0.94
    assert "offset dRA(east) +0.800 deg, dDec -0.500 deg" in c["detail"] and "SYNC APPLIED" in c["detail"]


def test_result_requested_sync_not_sent_is_partial_with_reason(sandboxed):  # noqa: F811
    offsets = {"offset_ra_deg": 6.0, "offset_dec_deg": 0.0, "separation_deg": 6.0, "confidence": 0.9,
               "residual": 0.1, "score": 0.9, "samples": 25}
    c = _classify_run(sandboxed, {"reference": "hi", "measured_positions": [{"status": "VALID"}] * 25,
                                  "offsets": offsets, "result_status": "PASS", "sync_applied": False,
                                  "sync_decision": {"requested": True, "applied": False,
                                                    "reason": "offset 6.000 deg larger than the 5 deg SYNC limit"}})
    assert c["verdict"] == "PARTIAL" and "SYNC NOT SENT: offset 6.000 deg larger" in c["detail"]


def test_hi_run_with_an_estimate_is_judged_like_the_sun_and_can_sync(tmp_path, monkeypatch):
    """The HI branch of run(): with enough robust positions it estimates an offset, PASSes on the same confidence
    threshold as the Sun and goes through maybe_sync (formerly always 'NON-OBSERVATIONAL TEMPLATE - SYNC
    BLOCKED'). Hardware, acquisition and the fit are faked; only the decision flow is under test."""
    from tests.test_alignment_hi_metric_v2_integration import make_args
    args = make_args(tmp_path)
    args.apply_sync = True
    runner = alignment.AlignmentRunner(args)
    center, est = _estimate(confidence=0.9)
    tel = FakeTelescope()
    tel.disconnect = lambda: asyncio.sleep(0)
    monkeypatch.setattr(runner, "resolve_reference", lambda: ("hi", center, {}))
    monkeypatch.setattr(alignment, "pattern_temporal_altitudes",
                        lambda *a, **k: {"min_altitude_deg": 80.0, "min_altitude_point_index": 0, "points": [{}]})

    async def connect(reference):
        runner.telescope = tel
        return 40.2
    records = [{"status": "VALID", "commanded_ra_hours": 1.0 + i * 0.01, "commanded_dec_deg": -30 + i * 0.1,
                "metric_value": float(i), "metric_snr": 9.0} for i in range(10)]

    async def acquire(reference, positions, gain):
        return records
    monkeypatch.setattr(runner, "connect_hardware", connect)
    monkeypatch.setattr(runner, "acquire", acquire)
    monkeypatch.setattr(runner, "analyze_hi_ensemble", lambda recs: (None, {"status": "PASS"}))
    monkeypatch.setattr(alignment, "LocalSphericalTemplate", lambda *a, **k: (lambda coords: [0.0] * len(coords)))
    monkeypatch.setattr(alignment, "estimate_template_offset", lambda *a, **k: est)
    monkeypatch.setattr(runner, "result_png", lambda *a, **k: None)
    code = asyncio.run(runner.run())
    result = json.loads((runner.output_dir / "alignment_result.json").read_text())
    assert code == 0 and result["result_status"] == "PASS"
    assert result["sync_applied"] is True and result["sync_decision"]["applied"] is True
    assert result["sync_eligible"] is True and result["offsets"]["confidence"] == 0.9
    assert [c[0] for c in tel.calls][:2] == ["goto", "sync"]
