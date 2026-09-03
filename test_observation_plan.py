"""Tests for observation_plan.py: duration/storage estimators, grid resolution,
EASTMOST_SAFE, FIXED_CENTER, stale/infeasible rejection, zero-hardware-motion during PLAN.

This Pi runs with ~0 free swap, and grid_generator.py's real matplotlib
rendering (grid_plan.png/grid_coverage.png) inside a full plan_observation()
call is the expensive part — stacking more than one or two such calls in a
single pytest process was observed to trigger an OOM kill. So this suite
keeps exactly ONE full plan_observation() call (FIXED_CENTER, covering
artifact writing + hash provenance) and tests EASTMOST_SAFE's search logic
directly against the cheap, non-plotting _resolve_eastmost_safe() function.
"""
import json
import subprocess

import pytest
from astropy.time import Time

import observation_plan as pl
import observation_spec as sp

TEST_OBSERVER_CONFIG = "observer_config.json"


def _valid_spec(**overrides):
    raw = {
        "session": {"name": "PLANTEST"},
        "grid": {
            "mode": "EQUATORIAL_RECT", "placement": "EASTMOST_SAFE",
            "center_ra_hours": None, "center_dec_deg": None,
            "width_deg": 4, "height_deg": 4, "rows": 3, "cols": 3,
            "min_altitude_deg": 10, "traversal": "SERPENTINE",
        },
        "capture": {"seconds": 1, "settle_seconds": 0.5},
        "main": {"center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 40.2, "bias_tee": True},
        "rfi_ref": {"enabled": False, "serial": "00000002", "gain_db": 25.0},
        "quicklook": {"enabled": False, "native_grid": True, "interpolated_preview": False,
                      "calibration_profile_path": None},
        "console": {"enabled": True},
        "execution": {"unattended": True},
    }
    for section, patch in overrides.items():
        raw[section].update(patch)
    return sp.validate_spec_dict(raw)


def _location_and_t0():
    observer_config = pl._load_observer_config(pl.Path(TEST_OBSERVER_CONFIG))
    location, _ = pl._observer_location(observer_config)
    return location, Time.now()


# --------------------------------------------------------------- estimators (no hardware, no grids)

def test_estimate_duration_scales_with_points_and_conservative_is_larger():
    small = pl.estimate_duration(9, 10, 2)
    large = pl.estimate_duration(90, 10, 2)
    assert large["estimated_seconds"] == pytest.approx(small["estimated_seconds"] * 10)
    assert small["conservative_seconds"] > small["estimated_seconds"]
    assert small["assumptions"]


def test_estimate_storage_matches_capture_py_formula():
    result = pl.estimate_storage(100, 10, 2_400_000, disk_safety_factor=1.25)
    expected_estimated = 100 * 10 * 2_400_000 * 2
    assert result["estimated_bytes"] == expected_estimated
    assert result["required_bytes"] == int(-(-expected_estimated * 1.25 // 1))


def test_max_recommended_start_delay_never_negative():
    predictions = [{"predicted_altitude_deg": 12.0}, {"predicted_altitude_deg": 9.5}]
    assert pl._max_recommended_start_delay_minutes(predictions, min_altitude_deg=10.0) == 0.0


# --------------------------------------------------------------- infeasibility (raises before grid
# materialization/plotting, so these stay cheap even though they call plan_observation())

def test_fixed_center_below_floor_raises(tmp_path):
    spec = _valid_spec(grid={"placement": "FIXED_CENTER", "center_ra_hours": 0.0, "center_dec_deg": 80.0,
                              "min_altitude_deg": 45})
    with pytest.raises(pl.ObservationPlanError, match="predicted minimum altitude"):
        pl.plan_observation(spec, observer_config_path=TEST_OBSERVER_CONFIG,
                             data_dir=str(tmp_path), run_preflight=False)


def test_eastmost_safe_impossible_declination_raises(tmp_path):
    spec = _valid_spec(grid={"center_dec_deg": 89.0, "min_altitude_deg": 85})
    with pytest.raises(pl.ObservationPlanError, match="not achievable this session"):
        pl.plan_observation(spec, observer_config_path=TEST_OBSERVER_CONFIG,
                             data_dir=str(tmp_path), run_preflight=False)


# --------------------------------------------------------------- EASTMOST_SAFE search: exercised
# directly against the cheap, non-plotting resolver (no GridGenerator/matplotlib involved).

def test_resolve_eastmost_safe_meets_altitude_margin_and_explains_reasoning():
    spec = _valid_spec()
    grid_spec = dict(spec["grid"])
    grid_spec["center_dec_deg"] = -33.4489  # observer latitude
    location, t0 = _location_and_t0()
    duration = pl.estimate_duration(9, 1, 0.5)
    resolved = pl._resolve_eastmost_safe(grid_spec, location, t0, duration["per_point_conservative_seconds"])
    assert resolved["min_predicted_altitude_deg"] >= grid_spec["min_altitude_deg"] + pl.EASTMOST_ALTITUDE_MARGIN_DEG - 0.5
    assert "easternmost candidate" in resolved["reasoning"]
    assert 0.0 <= resolved["center_ra_hours"] < 24.0
    assert len(resolved["points"]) == 9


def test_resolve_eastmost_safe_never_touches_indi_or_subprocess(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("EASTMOST_SAFE search must never launch a subprocess or mutate INDI state")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    import indi_telescope_control
    for method in ("connect", "goto", "sync", "set_tracking"):
        if hasattr(indi_telescope_control.INDITelescopeControl, method):
            monkeypatch.setattr(indi_telescope_control.INDITelescopeControl, method, _boom, raising=False)

    spec = _valid_spec()
    grid_spec = dict(spec["grid"])
    grid_spec["center_dec_deg"] = -33.4489
    location, t0 = _location_and_t0()
    duration = pl.estimate_duration(9, 1, 0.5)
    resolved = pl._resolve_eastmost_safe(grid_spec, location, t0, duration["per_point_conservative_seconds"])
    assert resolved["min_predicted_altitude_deg"] >= grid_spec["min_altitude_deg"]


# --------------------------------------------------------------- the ONE heavy full-pipeline call:
# resolution + real grid_generator.py artifacts + hash provenance, all for FIXED_CENTER.

@pytest.fixture(scope="module")
def fixed_center_plan(tmp_path_factory):
    spec = _valid_spec(grid={"placement": "FIXED_CENTER", "center_ra_hours": 6.0, "center_dec_deg": -30.0})
    data_dir = tmp_path_factory.mktemp("fixed_center")
    plan = pl.plan_observation(spec, observer_config_path=TEST_OBSERVER_CONFIG,
                                data_dir=str(data_dir), run_preflight=False)
    return data_dir, plan


def test_fixed_center_resolution_artifacts_and_hash(fixed_center_plan):
    data_dir, plan = fixed_center_plan

    assert plan["resolved"]["center_ra_hours"] == 6.0
    assert plan["resolved"]["center_dec_deg"] == -30.0
    assert plan["resolved"]["placement_reasoning"].startswith("Center RA/Dec taken verbatim")
    assert plan["resolved"]["point_count"] == 9
    assert "preflight" not in plan  # run_preflight=False

    session_dir = data_dir / plan["grid_session_dir"].split("/")[-1]
    assert (session_dir / "mosaic.csv").exists()
    assert (session_dir / "grid_metadata.json").exists()
    assert (session_dir / "grid_plan.png").exists()
    assert (session_dir / "grid_coverage.png").exists()
    assert (session_dir / "observer_config.json").exists()
    resolved_path = session_dir / "observation_resolved.json"
    assert resolved_path.exists()
    on_disk = json.loads(resolved_path.read_text())
    assert on_disk["observation_config_sha256"] == plan["observation_config_sha256"]

    assert pl.recompute_config_hash(plan) == plan["observation_config_sha256"]
    tampered = dict(plan)
    tampered["resolved"] = dict(plan["resolved"])
    tampered["resolved"]["center_ra_hours"] = plan["resolved"]["center_ra_hours"] + 1.0
    assert pl.recompute_config_hash(tampered) != tampered["observation_config_sha256"]


def test_fixed_center_plan_preserves_rfi_ref_bias_tee(fixed_center_plan):
    """rfi_ref (including bias_tee) is copied verbatim from spec into the
    resolved plan - both in the returned dict and in the JSON persisted to
    disk (the exact file START later reads). Reuses the module's one
    shared plan_observation() call (see module docstring: this Pi has ~0
    free swap and cannot afford a second real grid_generator.py pass)."""
    data_dir, plan = fixed_center_plan
    assert plan["rfi_ref"]["bias_tee"] is False  # _valid_spec()'s rfi_ref omits it -> schema default

    session_dir = data_dir / plan["grid_session_dir"].split("/")[-1]
    on_disk = json.loads((session_dir / "observation_resolved.json").read_text())
    assert on_disk["rfi_ref"]["bias_tee"] is False
