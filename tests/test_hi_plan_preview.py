"""CALIBRATE reference wizard: Alt/Az preview of the HI plan (calibration_engine/hi_plan_preview.py).

Pure function tests - no INDI, SDR, mount, server or files. The plan fixture has the exact shape
calibrate_reference_wizard.cmd_plan_hi() saves (hi_reference_selection.select_hi_alto_bajo_candidates)."""
import copy

import pytest

import almita_web_ops as ops
from calibration_engine.hi_plan_preview import HI_ORDER, build_preview

PLAN = {
    "generated_utc": "2026-10-02T01:35:38.044Z", "grid_points_considered": 15244, "beam_fwhm_deg": 20.0,
    "min_elevation_deg": 20.0, "hold_seconds": 12.0, "min_separation_deg": 40.0,
    "candidates": {
        "HI_ALTO": {"label": "HI_ALTO", "ra_hours": 16.1088, "dec_deg": -51.3040, "alt_deg": 30.2670, "az_deg": 225.6821,
                    "mean_n_hi_1e20cm2": 53.208, "reason": "highest beam-averaged N_HI",
                    "altitude_check": {"altitude_now_deg": 30.267, "worst_case_altitude_deg": 30.237, "min_elevation_deg": 20.0,
                                       "margin_deg": 10.237, "clears": True, "hold_seconds": 12.0}},
        "HI_BAJO": {"label": "HI_BAJO", "ra_hours": 23.0980, "dec_deg": -43.1565, "alt_deg": 69.7755, "az_deg": 124.8753,
                    "mean_n_hi_1e20cm2": 1.235, "reason": "lowest beam-averaged N_HI",
                    "altitude_check": {"altitude_now_deg": 69.775, "worst_case_altitude_deg": 69.775, "min_elevation_deg": 20.0,
                                       "margin_deg": 49.775, "clears": True, "hold_seconds": 12.0}},
    },
}


def test_no_plan_no_preview():
    assert build_preview(None) is None
    assert build_preview({"candidates": {}}) is None


def test_positions_are_the_plans_own_numbers_in_visiting_order():
    pv = build_preview(PLAN)
    assert [p["label"] for p in pv["points"]] == list(HI_ORDER)
    assert [p["order"] for p in pv["points"]] == [1, 2]
    for p in pv["points"]:
        c = PLAN["candidates"][p["label"]]
        assert (p["az_deg"], p["alt_deg"], p["ra_hours"], p["dec_deg"]) == (c["az_deg"], c["alt_deg"], c["ra_hours"], c["dec_deg"])
        assert p["worst_case_altitude_deg"] == c["altitude_check"]["worst_case_altitude_deg"]
        assert p["radius_deg"] == 10.0 and p["within_limits"] is True
    assert pv["reference_utc"] == PLAN["generated_utc"]
    assert pv["all_within_limits"] is True


def test_points_outside_the_configured_limits_are_flagged():
    plan = copy.deepcopy(PLAN)
    plan["candidates"]["HI_ALTO"]["altitude_check"].update(worst_case_altitude_deg=19.4, clears=False, margin_deg=-0.6)
    plan["candidates"]["HI_BAJO"]["alt_deg"] = 12.0
    pv = build_preview(plan)
    alto, bajo = pv["points"]
    assert not alto["within_limits"] and any("worst-case altitude 19.4" in m for m in alto["limit_problems"])
    assert any("does not clear" in m for m in alto["limit_problems"])
    assert not bajo["within_limits"] and any("altitude 12.0 deg below" in m for m in bajo["limit_problems"])
    assert pv["all_within_limits"] is False


def test_never_claims_the_sky_is_free_of_obstacles():
    pv = build_preview(PLAN)
    assert pv["horizon"]["available"] is False
    assert "NOT evaluated" in pv["horizon"]["note"]


def test_a_new_plan_replaces_the_preview():
    plan2 = copy.deepcopy(PLAN)
    plan2["generated_utc"] = "2026-10-02T02:10:00.000Z"
    plan2["candidates"]["HI_ALTO"]["az_deg"] = 231.0
    assert build_preview(plan2)["points"][0]["az_deg"] == 231.0
    assert build_preview(plan2)["reference_utc"] == "2026-10-02T02:10:00.000Z"


def test_ops_facts_carry_the_preview_with_next_and_measured_status():
    state = {"step": "READY_HI_BAJO", "hi_plan": PLAN, "hi_references": {"HI_ALTO": {}}}
    pv = ops._hi_plan_preview(state)
    assert [p["status"] for p in pv["points"]] == ["MEASURED", "NEXT"]
    assert ops._hi_plan_preview({"step": "PLAN_HI"}) is None


# ------------------------------------------------------------------ the HI4PI layer at the plan instant (web ops)

def test_web_preview_adds_the_hi4pi_layer_at_the_plans_own_instant_and_beam(monkeypatch):
    import almita_web_ops as ops
    import hi4pi_map
    calls = []

    def fake_sky_grid(location, obstime, beam_fwhm_deg, grid_n=90):
        calls.append((obstime.utc.isot, beam_fwhm_deg))
        return {"grid_n": 2, "grid": [1.0, 2.0, None, 3.0], "value_range_1e20cm2": [1.0, 3.0],
                "obstime_utc": obstime.utc.isot + "Z", "beam_fwhm_deg": beam_fwhm_deg}
    monkeypatch.setattr(hi4pi_map, "sky_grid", fake_sky_grid)
    ops._HI_PLAN_SKY_CACHE.clear()
    state = {"step": "READY_HI_BAJO", "hi_plan": PLAN, "hi_references": {"HI_ALTO": {}}}
    pv = ops._hi_plan_preview(state)
    assert calls == [("2026-10-02T01:35:38.044", PLAN["beam_fwhm_deg"])]       # the plan's instant and beam
    assert pv["sky"]["hi4pi_grid"] == {"n": 2, "values_1e20cm2": [1.0, 2.0, None, 3.0], "value_range_1e20cm2": [1.0, 3.0]}
    assert [(p["label"], p["status"]) for p in pv["points"]] == [("HI_ALTO", "MEASURED"), ("HI_BAJO", "NEXT")]
    ops._hi_plan_preview(state)
    assert len(calls) == 1                                                     # a fixed instant is computed once


def test_web_preview_without_the_hi4pi_map_still_shows_the_zones(monkeypatch):
    import almita_web_ops as ops
    import hi4pi_map

    def unavailable(*a, **k):
        raise hi4pi_map.HI4PIUnavailable("HI4PI map not found")
    monkeypatch.setattr(hi4pi_map, "sky_grid", unavailable)
    ops._HI_PLAN_SKY_CACHE.clear()
    pv = ops._hi_plan_preview({"step": "PLAN_HI", "hi_plan": PLAN, "hi_references": {}})
    assert "HI4PI map not found" in pv["sky"]["hi4pi_error"] and len(pv["points"]) == 2


def test_sky_grid_is_the_same_layer_align_draws():
    """hi4pi_map.sky_grid() is the raster sky_grid_and_ranking() returns to ALIGN, without the ranking."""
    import hi4pi_map
    from astropy.coordinates import EarthLocation
    from astropy.time import Time
    import astropy.units as u
    try:
        hi4pi_map._load()
    except hi4pi_map.HI4PIUnavailable:
        pytest.skip("real HI4PI map not present")
    loc = EarthLocation(lat=-33.4489 * u.deg, lon=-70.6693 * u.deg, height=570 * u.m)
    t = Time("2026-10-02T01:35:38.044", scale="utc")
    grid = hi4pi_map.sky_grid(loc, t, 20.0, grid_n=40)
    ranked = hi4pi_map.sky_grid_and_ranking(loc, t, 20.0, 20.0, 5.0, [2.5, 5.0], 8, 10.0, grid_n=40, top_n=1)
    assert grid["grid"] == ranked["grid"] and grid["value_range_1e20cm2"] == ranked["value_range_1e20cm2"]
