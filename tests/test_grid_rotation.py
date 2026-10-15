"""Grid ANGLE (rotation about the centre): geometry, spec, plan hash, live estimate and the SCIENCE guard."""
import json
from pathlib import Path

import astropy.units as u
import pytest
from astropy.coordinates import SkyCoord

import grid_generator as gg
import observation_plan as pl
import observation_spec
from tests.test_observation_plan import TEST_OBSERVER_CONFIG, _valid_spec

ROOT = Path(__file__).resolve().parents[1]


def _grid(rot):
    return gg.build_spherical_grid(6.0, -30.0, 8.0, 8.0, 2.0, 1.0, rotation_deg=rot)


def _pa_of_up_axis(points, meta):
    c = SkyCoord(ra=meta["center_ra_hours"] * u.hourangle, dec=meta["center_dec_degrees"] * u.deg)
    mid = meta["columns"] // 2
    top = next(p for p in points if p["grid_row"] == meta["rows"] - 1 and p["grid_col"] == mid)
    return c.position_angle(SkyCoord(ra=top["target_ra_hours"] * u.hourangle, dec=top["target_dec_degrees"] * u.deg)).deg


@pytest.mark.parametrize("rot", [0.0, 30.0, -45.0, 90.0])
def test_the_grid_turns_about_its_centre_by_the_angle(rot):
    pts, meta = _grid(rot)
    assert len(pts) == 25 and meta["rotation_deg"] == rot
    assert (_pa_of_up_axis(pts, meta) - rot + 180) % 360 - 180 == pytest.approx(0, abs=1e-6)


def test_zero_angle_is_exactly_the_old_grid():
    a, _ = _grid(0.0)
    b, _ = gg.build_spherical_grid(6.0, -30.0, 8.0, 8.0, 2.0, 1.0)
    assert [(p["target_ra_hours"], p["target_dec_degrees"]) for p in a] == [(p["target_ra_hours"], p["target_dec_degrees"]) for p in b]


def test_spec_accepts_an_angle_defaults_to_zero_and_bounds_it():
    from tests.test_orchestrator_web import VALID_SPEC
    raw = json.loads(json.dumps(VALID_SPEC))
    assert observation_spec.validate_spec_dict(raw)["grid"]["rotation_deg"] == 0.0
    raw["grid"]["rotation_deg"] = 25
    assert observation_spec.validate_spec_dict(raw)["grid"]["rotation_deg"] == 25.0
    raw["grid"]["rotation_deg"] = 200
    with pytest.raises(observation_spec.ObservationSpecError):
        observation_spec.validate_spec_dict(raw)


def test_an_unrotated_plan_keeps_its_hash_and_a_rotated_one_changes_it():
    kw = dict(grid_mode="EQUATORIAL_RECT", placement="FIXED_CENTER", resolved_ra=6.0, resolved_dec=-30.0, rows=3, cols=3,
              spacing_deg=2.0, traversal="SERPENTINE", min_altitude_deg=5, capture={"seconds": 1}, main={}, rfi_ref={}, quicklook={})
    assert pl._config_hash_payload(**kw) == pl._config_hash_payload(**kw, rotation_deg=0.0)
    assert pl._config_hash_payload(**kw) != pl._config_hash_payload(**kw, rotation_deg=15.0)


def test_the_live_estimate_draws_the_turned_footprint():
    s = _valid_spec()
    cfg = str(ROOT / TEST_OBSERVER_CONFIG)
    a = pl.estimate_observation(s, observer_config_path=cfg)["sky"]["points_now"]
    s["grid"]["rotation_deg"] = 40.0
    b = pl.estimate_observation(s, observer_config_path=cfg)["sky"]["points_now"]
    assert len(a) == len(b) and any(abs(p["az_deg"] - q["az_deg"]) > 0.1 for p, q in zip(a, b))


def test_science_refuses_a_rotated_campaign_clearly(tmp_path):
    import science_web_bridge as swb
    camp = tmp_path / "CAMP"
    camp.mkdir()
    (camp / "grid_metadata.json").write_text(json.dumps({"grid": {"rows": 3, "columns": 3, "nominal_spacing_deg": 2.0,
                                                                  "center_ra_hours": 6.0, "center_dec_degrees": -30.0, "rotation_deg": 20.0}}))
    (camp / "mosaic.csv").write_text("point_number,grid_row,grid_col,target_ra_hours,target_dec_degrees\n1,0,0,6,-30\n")
    red = tmp_path / "RED"
    red.mkdir()
    (red / "manifest.json").write_text(json.dumps({"source_campaign_root": str(camp)}))
    fi = type("FI", (), {"reduce_session_dir": str(red), "points": []})()
    with pytest.raises(swb.MosaicShapeError, match="rotated 20 deg"):
        swb.load_planned_lattice(fi)


def test_observe_form_sends_the_angle():
    html = (ROOT / "console" / "observe.html").read_text()
    js = (ROOT / "console" / "observe.js").read_text()
    assert 'id="f-rotation"' in html and 'rotation_deg: $("f-rotation").value' in js


def test_a_real_plan_writes_the_turned_grid_for_capture_and_its_hash_verifies(tmp_path):
    import csv
    from tests.test_orchestrator_web import VALID_SPEC
    raw = json.loads(json.dumps(VALID_SPEC))
    raw["grid"]["rotation_deg"] = 30.0
    spec = observation_spec.validate_spec_dict(raw)
    plan = pl.plan_observation(spec, observer_config_path=str(ROOT / TEST_OBSERVER_CONFIG), data_dir=str(tmp_path))
    assert plan["resolved"]["rotation_deg"] == 30.0
    assert pl.recompute_config_hash(plan) == plan["observation_config_sha256"]        # what preflight/orchestrator check
    rows = list(csv.DictReader(open(plan["mosaic_csv_path"])))
    expected, meta = pl._grid_points(plan["resolved"]["center_ra_hours"], plan["resolved"]["center_dec_deg"],
                                     spec["grid"]["width_deg"], spec["grid"]["height_deg"], plan["resolved"]["spacing_deg"], 30.0)
    exp = {(p["grid_row"], p["grid_col"]): (p["target_ra_hours"], p["target_dec_degrees"]) for p in expected}
    for r in rows:
        ra, dec = exp[(int(r["grid_row"]), int(r["grid_col"]))]
        assert float(r["target_ra_hours"]) == pytest.approx(ra, abs=1e-6) and float(r["target_dec_degrees"]) == pytest.approx(dec, abs=1e-6)
    assert _pa_of_up_axis(expected, meta) == pytest.approx(30.0, abs=1e-6)
