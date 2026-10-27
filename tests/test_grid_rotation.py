"""Grid ANGLE (rotation about the centre): geometry, spec, plan hash, live estimate and SCIENCE on a turned grid."""
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


def _rotated_campaign(tmp_path, rot, edit_rows=None):
    """A REDUCE session + campaign pair whose plan is OBSERVE's own turned grid (grid_generator, 5x5, 2 deg)."""
    import csv
    import dataclasses
    from science_engine.simulation import SyntheticPointSpec, build_synthetic_science_input
    pts, meta = gg.build_spherical_grid(6.0, -30.0, 8.0, 8.0, 2.0, 1.0, rotation_deg=rot)
    rows = [{"point_number": p["point_id"], "grid_row": p["grid_row"], "grid_col": p["grid_col"],
             "target_ra_hours": f"{p['target_ra_hours']:.8f}", "target_dec_degrees": f"{p['target_dec_degrees']:.8f}"}
            for p in pts]
    if edit_rows:
        edit_rows(rows)
    camp = tmp_path / "CAMP"
    camp.mkdir()
    with (camp / "mosaic.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (camp / "grid_metadata.json").write_text(json.dumps({"grid": meta}))
    red = tmp_path / "RED"
    red.mkdir()
    (red / "manifest.json").write_text(json.dumps({"source_campaign_root": str(camp)}))
    specs = sorted((SyntheticPointSpec(point_index=p["point_id"], ra_hours=float(f"{p['target_ra_hours']:.8f}"),
                                       dec_degrees=float(f"{p['target_dec_degrees']:.8f}"), calibration_level="UNCALIBRATED")
                    for p in pts), key=lambda sp: sp.point_index)
    si = build_synthetic_science_input(specs, noise_sigma=0.02)
    return dataclasses.replace(si, reduce_session_dir=str(red)), meta


@pytest.mark.parametrize("rot", [30.0, 60.0, -45.0, 120.0, 180.0])
def test_science_draws_a_turned_campaign_tilted_on_fixed_ra_dec_axes(tmp_path, rot):
    """BL-077: no refusal. Each point keeps its own planned cell, A draws that cell where the point is (a tilted
    board on RA/Dec axes) and the widened B/C raster covers every point."""
    import numpy as np
    import science_web_bridge as swb
    si, meta = _rotated_campaign(tmp_path, rot)
    planned = swb.load_planned_lattice(si)
    assert planned.rotation_deg == rot
    board, point_cell, n_rows, n_cols = swb.build_mosaic_grid(si, planned.spacing_deg, planned)
    assert (n_rows, n_cols) == (meta["rows"], meta["columns"]) and point_cell == planned.cell_by_point
    nodes, corners = swb.lattice_geometry(board, n_rows, n_cols, planned)
    base = swb.raster_base_grid(board, corners)
    geo = swb.mosaic_geometry_summary(si, base, nodes, point_cell, planned, 2.0, (2, 3))
    assert geo["rotation_deg"] == rot
    assert geo["point_to_a_cell_node_max_deg"] < 1e-5            # A's cell sits on its point
    assert all(not cov["uncovered_points"] for cov in geo["raster_coverage"].values())
    # the board really is tilted: the up axis of A's nodes points at the grid's angle (x = east, y = north)
    up = nodes[-1, n_cols // 2] - nodes[0, n_cols // 2]
    assert (np.degrees(np.arctan2(up[0], up[1])) - rot + 180) % 360 - 180 == pytest.approx(0, abs=1.0)


def test_a_turned_campaign_with_swapped_columns_is_still_refused(tmp_path):
    import science_web_bridge as swb

    def swap(rows):
        rows[0]["grid_col"], rows[1]["grid_col"] = rows[1]["grid_col"], rows[0]["grid_col"]
    si, _ = _rotated_campaign(tmp_path, 60.0, edit_rows=swap)
    with pytest.raises(swb.MosaicShapeError, match="not monotonic"):
        swb.build_mosaic_grid(si, 2.0, swb.load_planned_lattice(si))


def test_a_turned_campaign_builds_a_b_c_and_labels_the_maps(tmp_path):
    import science_web_bridge as swb
    from tests.test_science_web_bridge import cfg_with
    si, _ = _rotated_campaign(tmp_path, 30.0)
    built = swb.build_all_products(si, cfg_with())
    assert built["mosaic_geometry"]["rotation_deg"] == 30.0
    assert built["map_a_valid"].sum() == 25
    assert 'grid turned ${geo.rotation_deg} deg from North' in (ROOT / "console" / "science.js").read_text()
    assert 'grid turned {rotation:g}° from North' in (ROOT / "science_web_bridge.py").read_text()


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
