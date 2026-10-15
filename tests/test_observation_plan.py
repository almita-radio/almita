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

from tests.conftest import transiting_ra_hours
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
        "main": {"center_frequency_hz": 1420405752, "sample_rate": 2400000, "gain_db": 40.2, "bias_tee": True},
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


def test_resolve_eastmost_safe_long_run_is_placed_by_altitude_floor_not_meridian():
    """2026-10-14: 15x15 / 30 deg at 50 s capture (5.75 h conservative) was rejected because a meridian-centred
    start sets below the floor before the end, although starting further east keeps every point high."""
    spec = _valid_spec(grid={"width_deg": 30, "height_deg": 30, "rows": 15, "cols": 15},
                       capture={"seconds": 50, "settle_seconds": 5})
    grid_spec = dict(spec["grid"])
    grid_spec["center_dec_deg"] = -33.4489  # observer latitude
    location, t0 = _location_and_t0()
    per_point = pl.estimate_duration(225, 50, 5)["per_point_conservative_seconds"]
    lst = t0.sidereal_time("apparent", longitude=location.lon).hour
    meridian_points, _ = pl._grid_points(lst, grid_spec["center_dec_deg"], 30, 30, grid_spec["nominal_spacing_deg"])
    meridian_low = min(p["predicted_altitude_deg"] for p in pl._predicted_altitudes(meridian_points, location, t0, per_point))
    assert meridian_low < grid_spec["min_altitude_deg"] + pl.EASTMOST_ALTITUDE_MARGIN_DEG  # the old search gave up here
    resolved = pl._resolve_eastmost_safe(grid_spec, location, t0, per_point)
    assert resolved["min_predicted_altitude_deg"] >= grid_spec["min_altitude_deg"] + pl.EASTMOST_ALTITUDE_MARGIN_DEG - 0.5
    start_ha = (lst - resolved["center_ra_hours"] + 12.0) % 24.0 - 12.0
    assert start_ha < -1.0  # placed rising in the east
    assert len(resolved["points"]) == 225


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
    ra = transiting_ra_hours()               # always visible when the test runs (a fixed 6h is below the horizon at some hours)
    spec = _valid_spec(grid={"placement": "FIXED_CENTER", "center_ra_hours": ra, "center_dec_deg": -30.0})
    data_dir = tmp_path_factory.mktemp("fixed_center")
    plan = pl.plan_observation(spec, observer_config_path=TEST_OBSERVER_CONFIG,
                                data_dir=str(data_dir), run_preflight=False)
    return data_dir, plan


def test_fixed_center_resolution_artifacts_and_hash(fixed_center_plan):
    data_dir, plan = fixed_center_plan

    assert plan["resolved"]["center_ra_hours"] == plan["requested"]["grid"]["center_ra_hours"]
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
    assert plan["rfi_ref"]["bias_tee"] is True  # _valid_spec()'s rfi_ref omits it -> schema default (ON, BL-041)

    session_dir = data_dir / plan["grid_session_dir"].split("/")[-1]
    on_disk = json.loads((session_dir / "observation_resolved.json").read_text())
    assert on_disk["rfi_ref"]["bias_tee"] is True


# --------------------------------------------------------------- live estimate (OBSERVE form): PLAN's own
# resolution, nothing written to disk

def test_estimate_observation_matches_plan_duration_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # anything written would land here
    spec = _valid_spec()
    est = pl.estimate_observation(spec, observer_config_path=str(pl.Path(__file__).resolve().parents[1] / TEST_OBSERVER_CONFIG))
    assert est["feasible"] is True and est["blocking_reason"] is None
    assert est["duration"] == pl.estimate_duration(9, 1, 0.5)
    assert est["point_count"] == 9
    w = est["sky_window"]
    assert w["window_end_utc"] is not None and w["seconds_until_window_end"] >= est["duration"]["planning_seconds"] - 300
    assert list(tmp_path.iterdir()) == []


def test_estimate_observation_reports_plan_refusal_instead_of_raising():
    spec = _valid_spec(grid={"placement": "FIXED_CENTER", "center_ra_hours": 0.0, "center_dec_deg": 80.0,
                              "min_altitude_deg": 45})
    est = pl.estimate_observation(spec, observer_config_path=TEST_OBSERVER_CONFIG)
    assert est["feasible"] is False
    assert "predicted minimum altitude" in est["blocking_reason"]
    assert est["duration"]["estimated_seconds"] > 0
    assert est["sky_window"]["window_end_utc"] is None  # dec +80 never reaches 45 deg from -33


def test_sky_window_open_now_and_closes_when_the_region_sets():
    location, t0 = _location_and_t0()
    lst = t0.sidereal_time("apparent", longitude=location.lon).hour
    points, _ = pl._grid_points(lst, -33.4489, 4, 4, 2)  # on the meridian at the zenith now
    w = pl.sky_window(points, location, t0, 10.0)
    assert w["visible_now"] is True and w["window_start_utc"] == t0.utc.isot + "Z"
    assert 4 * 3600 < w["seconds_until_window_end"] < 8 * 3600  # sets ~5-6 h after transit at this altitude floor
    assert w["seconds_until_window_end"] == w["window_seconds"]


def test_sky_window_ends_on_the_last_sample_still_wholly_up():
    """Conservative end: the window never includes the sample where the region is already below the floor."""
    import astropy.units as u
    from astropy.coordinates import AltAz, SkyCoord
    cfg = pl._load_observer_config(pl.Path(__file__).resolve().parents[1] / TEST_OBSERVER_CONFIG)
    location, _ = pl._observer_location(cfg)
    points, _ = pl._grid_points(transiting_ra_hours(), -30.0, 4, 4, 2.0)
    t0 = pl.Time.now()
    w = pl.sky_window(points, location, t0, 30.0)
    end = pl.Time(w["window_end_utc"].rstrip("Z"), scale="utc")
    coords = SkyCoord(ra=[p["target_ra_hours"] * 15 for p in points] * u.deg, dec=[p["target_dec_degrees"] for p in points] * u.deg)
    lowest = lambda t: coords.transform_to(AltAz(obstime=t, location=location)).alt.deg.min()  # noqa: E731
    assert lowest(end) >= 30.0 > lowest(end + w["step_minutes"] * u.min)


def test_estimate_returns_each_point_where_it_will_be_captured():
    spec = _valid_spec()
    est = pl.estimate_observation(spec, observer_config_path=str(pl.Path(__file__).resolve().parents[1] / TEST_OBSERVER_CONFIG))
    sky = est["sky"]
    assert len(sky["points"]) == 9 and [p["scan_order"] for p in sky["points"]] == list(range(1, 10))
    assert all(0 <= p["az_deg"] < 360 and isinstance(p["below_min"], bool) for p in sky["points"])
    assert sky["min_altitude_deg"] == spec["grid"]["min_altitude_deg"] and "alt_deg" in sky["sun"]


def test_observe_page_draws_the_grid_with_the_minimum_altitude_circle():
    root = pl.Path(__file__).resolve().parents[1]
    assert 'id="est-skymap"' in (root / "console" / "observe.html").read_text()
    js = (root / "console" / "observe.js").read_text()
    assert "minAlt: sky.min_altitude_deg" in js and "points: pts" in js and "path" not in js.split("function renderEstimateSky")[1].split("function renderEstimate(")[0]
    assert "o.minAlt" in (root / "console" / "common.js").read_text()


def test_planning_pace_is_the_mean_of_projected_and_conservative():
    d = pl.estimate_duration(100, 10, 2)
    assert d["planning_seconds"] == pytest.approx((d["estimated_seconds"] + d["conservative_seconds"]) / 2)
    assert d["estimated_seconds"] < d["planning_seconds"] < d["conservative_seconds"]
    assert d["per_point_planning_seconds"] * 100 == pytest.approx(d["planning_seconds"])


def test_no_console_page_repeats_an_element_id():
    """A repeated id made OBSERVE draw its sky map on the SKY TIME LEFT text, throw, and stop updating (2026-10-15)."""
    import re
    from collections import Counter
    root = pl.Path(__file__).resolve().parents[1] / "console"
    for page in sorted(root.glob("*.html")):
        ids = Counter(re.findall(r'\bid="([^"]+)"', page.read_text()))
        assert not [i for i, n in ids.items() if n > 1], (page.name, [i for i, n in ids.items() if n > 1])


def test_estimate_also_returns_the_footprint_at_the_start_coloured_by_capture_altitude():
    spec = _valid_spec()
    sky = pl.estimate_observation(spec, observer_config_path=str(pl.Path(__file__).resolve().parents[1] / TEST_OBSERVER_CONFIG))["sky"]
    assert len(sky["points_now"]) == len(sky["points"])
    assert sky["points_now"][0]["az_deg"] == pytest.approx(sky["points"][0]["az_deg"], abs=0.5)    # first point: now = its capture
    assert [p["below_min"] for p in sky["points_now"]] == [p["below_min"] for p in sky["points"]]
