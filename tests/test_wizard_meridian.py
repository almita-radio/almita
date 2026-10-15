"""BL-060: the wizard and OnStep's meridian limit - zones that reach it are not proposed, CAPTURE re-checks it before
any GOTO, and the zones can be proposed again after approval (keeping a measured HI ALTO). No hardware."""
import copy
import json
from types import SimpleNamespace

import pytest
from astropy.time import Time

import almita_web_ops as ops
import calibrate_reference_wizard as wiz
from calibration_engine import hi_reference_selection as hrs
from tests.test_calibrate_wizard_lifecycle import _state, session  # noqa: F401 - fixture
from tests.test_hi_plan_preview import PLAN

LOC = hrs._observer_location()
T0 = Time("2026-10-15T03:00:00", scale="utc")


def _ra_at_hour_angle(ha_h):
    lst = T0.sidereal_time("apparent", longitude=LOC.lon).hour
    return (lst - ha_h) % 24.0


def test_a_point_that_reaches_the_limit_during_the_hold_does_not_clear():
    c = hrs.meridian_limit_check(_ra_at_hour_angle(0.10), 10 * 60, LOC, T0)          # +6 min now, 10 min hold
    assert c["crosses_limit"] and not c["clears"] and c["minutes_to_limit"] == pytest.approx(6.0, abs=0.1)


@pytest.mark.parametrize("ha", [-2.0, 0.30, 5.0])
def test_points_well_east_or_already_past_the_limit_clear(ha):
    assert hrs.meridian_limit_check(_ra_at_hour_angle(ha), 10 * 60, LOC, T0)["clears"]


def _to_plan_hi(d):
    st = _state(d)
    st.update(step="PLAN_HI", reconnect_antenna_confirmed_utc="2026-10-15T00:00:00+00:00")
    (d / "wizard_state.json").write_text(json.dumps(st))


def test_repropose_after_approval_needs_a_new_approval(session, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(hrs, "select_hi_alto_bajo_candidates", lambda **k: (calls.append(k), copy.deepcopy(PLAN))[1])
    d = session()
    _to_plan_hi(d)
    assert wiz.cmd_plan_hi(SimpleNamespace(session_dir=str(d))) == 0
    assert wiz.cmd_approve_hi_plan(SimpleNamespace(session_dir=str(d))) == 0
    assert _state(d)["step"] == "READY_HI_ALTO"
    assert wiz.cmd_plan_hi(SimpleNamespace(session_dir=str(d))) == 0                  # RE-PROPOSE from READY
    st = _state(d)
    assert st["step"] == "PLAN_HI" and st["hi_plan_approved_utc"] is None and len(st["hi_plan_history"]) == 1
    assert calls[-1]["fixed_high"] is None


def test_repropose_after_hi_alto_keeps_it_and_continues_at_hi_bajo(session, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(hrs, "select_hi_alto_bajo_candidates", lambda **k: (calls.append(k), copy.deepcopy(PLAN))[1])
    d = session()
    _to_plan_hi(d)
    st = _state(d)
    st.update(step="READY_HI_BAJO", hi_plan=PLAN, hi_plan_approved_utc="2026-10-15T00:10:00+00:00",
              hi_references={"HI_ALTO": {"candidate": PLAN["candidates"]["HI_ALTO"], "quality_result": {"verdict": "PASS"}}})
    (d / "wizard_state.json").write_text(json.dumps(st))
    assert wiz.cmd_plan_hi(SimpleNamespace(session_dir=str(d))) == 0
    assert calls[-1]["fixed_high"]["ra_hours"] == PLAN["candidates"]["HI_ALTO"]["ra_hours"]
    assert wiz.cmd_approve_hi_plan(SimpleNamespace(session_dir=str(d))) == 0
    assert _state(d)["step"] == "READY_HI_BAJO"


def test_capture_refuses_before_any_goto_when_the_hold_would_cross_the_limit(session, monkeypatch):  # noqa: F811
    d = session()
    st = _state(d)
    st.update(step="READY_HI_ALTO", hi_plan=PLAN, hi_plan_approved_utc="2026-10-15T00:10:00+00:00")
    (d / "wizard_state.json").write_text(json.dumps(st))
    monkeypatch.setattr(hrs, "point_stays_above_elevation", lambda *a, **k: {"clears": True, "margin_deg": 20.0})
    monkeypatch.setattr(hrs, "meridian_limit_check", lambda *a, **k: {"clears": False, "limit_min": 12.0, "minutes_to_limit": 3.0})
    moved = []
    monkeypatch.setattr(wiz, "_capture_n_at", lambda *a, **k: moved.append(1))
    with pytest.raises(SystemExit, match="meridian limit"):
        wiz.cmd_capture_hi(SimpleNamespace(session_dir=str(d), label="HI_ALTO", simulate=None))
    assert moved == [] and _state(d)["step"] == "READY_HI_ALTO"


def test_mount_gate_says_how_to_clear_a_latched_meridian_error():
    probs = ops.mount_idle_problems({"connected": True, "eod_state": "Idle", "mount_state": "Idle", "parked": False,
                                     "onstep_error": "Meridian Limit (W) Exceeded"})
    assert any("tracking OFF and then ON in Ekos" in p for p in probs)
