"""SCIENCE on wide fields and across a pole (2026-11-05): the map frame is the exact spherical offset frame OBSERVE
lays its grids out in (astropy SkyOffsetFrame), not the linear dRA*cos(dec0) approximation that refused the real
40 deg grid around the south pole (ALMITA-OBSERVE-20261106-00:01:55, 15x15, centre RA 5.035 h, Dec -82.96)."""
import csv
import dataclasses
import json

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord, SkyOffsetFrame

import grid_generator as gg
from science_engine.grid import pixel_centers_deg
from science_engine.models import ScienceGrid
from science_engine.spatial import offsets_to_sky_deg, sky_offsets_deg, tangent_plane_offsets_deg

CENTRES = [(0.0, 0.0), (75.5, -82.96), (359.9, -30.0), (180.0, 89.0), (10.0, -89.5), (300.0, 45.0)]


@pytest.mark.parametrize("c_ra,c_dec", CENTRES)
def test_offsets_are_astropys_sky_offset_frame(c_ra, c_dec):
    rng = np.random.default_rng(7)
    ra, dec = rng.uniform(0, 360, 200), np.degrees(np.arcsin(rng.uniform(-1, 1, 200)))
    centre = SkyCoord(ra=c_ra * u.deg, dec=c_dec * u.deg)
    off = SkyCoord(ra=ra * u.deg, dec=dec * u.deg).transform_to(SkyOffsetFrame(origin=centre))
    x, y = sky_offsets_deg(ra, dec, c_ra, c_dec)
    lon = (off.lon.deg + 180) % 360 - 180
    assert np.allclose(x, lon, atol=1e-8) and np.allclose(y, off.lat.deg, atol=1e-8)
    assert tangent_plane_offsets_deg is not None and np.allclose(tangent_plane_offsets_deg(ra, dec, c_ra, c_dec)[0], x)


@pytest.mark.parametrize("c_ra,c_dec", CENTRES)
def test_offsets_round_trip_back_to_the_sky(c_ra, c_dec):
    xx, yy = np.meshgrid(np.linspace(-40, 40, 17), np.linspace(-40, 40, 17))
    ra, dec = offsets_to_sky_deg(xx, yy, c_ra, c_dec)
    assert np.all((ra >= 0) & (ra < 360))
    x, y = sky_offsets_deg(ra, dec, c_ra, c_dec)
    assert np.allclose(x, xx, atol=1e-8) and np.allclose(y, yy, atol=1e-8)


def test_a_few_degree_field_is_still_the_old_linear_projection():
    """Backwards check: at 1 deg from a centre at Dec -30 the new frame differs from dRA*cos(dec0), dDec by < 0.01 deg."""
    ra = 100.0 + np.array([1.0, -1.0, 0.5, 0.0]) / np.cos(np.radians(-30.0))
    dec = -30.0 + np.array([0.0, 0.5, -1.0, 1.0])
    x, y = sky_offsets_deg(ra, dec, 100.0, -30.0)
    old_x, old_y = (ra - 100.0) * np.cos(np.radians(-30.0)), dec + 30.0
    assert np.max(np.abs(x - old_x)) < 0.01 and np.max(np.abs(y - old_y)) < 0.01


def test_a_tile_keeps_the_parent_pixels_across_the_pole():
    import science_web_bridge as swb
    grid = ScienceGrid(frame="icrs", center_ra_deg=75.5, center_dec_deg=-82.96, width_deg=60.0, height_deg=60.0,
                       pixel_scale_deg=1.0, nx=60, ny=60)
    ra, dec = pixel_centers_deg(grid)
    tile = swb._tile_grid(grid, 10, 35, 40, 60)
    tra, tdec = pixel_centers_deg(tile)
    d_ra = (tra - ra[10:35, 40:60] + 180) % 360 - 180
    assert tra.shape == (25, 20) and np.allclose(d_ra * np.cos(np.radians(tdec)), 0, atol=1e-9)
    assert np.allclose(tdec, dec[10:35, 40:60], atol=1e-9)


def _campaign(tmp_path, ra_h, dec, size, n, edit_rows=None):
    """A REDUCE session + campaign pair whose plan is OBSERVE's own grid (grid_generator), n x n over size deg."""
    from science_engine.simulation import SyntheticPointSpec, build_synthetic_science_input
    pts, meta = gg.build_spherical_grid(ra_h, dec, size, size, size / (n - 1), 1.0)
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


def test_the_real_polar_grid_is_accepted_and_every_cell_sits_on_its_point(tmp_path):
    """The night's 15x15, 40 deg grid over the south pole: no refusal, A's cells on their points, B/C cover all."""
    import science_web_bridge as swb
    si, meta = _campaign(tmp_path, 5.035, -82.96, 40.0, 15)
    decs = [p.dec_degrees for p in si.points]
    assert min(decs) < -88 and len({round(p.ra_deg) for p in si.points}) > 100     # it really wraps round the pole
    planned = swb.load_planned_lattice(si)
    board, point_cell, n_rows, n_cols = swb.build_mosaic_grid(si, planned.spacing_deg, planned)
    assert (n_rows, n_cols) == (15, 15) and point_cell == planned.cell_by_point
    nodes, corners = swb.lattice_geometry(board, n_rows, n_cols, planned)
    base = swb.raster_base_grid(board, corners)
    geo = swb.mosaic_geometry_summary(si, base, nodes, point_cell, planned, 40.0 / 14, (2, 3))
    assert geo["point_to_a_cell_node_max_deg"] < 1e-6
    assert geo["regular_board_offset_max_deg"] < 1e-6      # OBSERVE's lattice IS regular in this frame
    assert all(not cov["uncovered_points"] for cov in geo["raster_coverage"].values())


def test_a_polar_grid_with_swapped_columns_is_still_refused(tmp_path):
    import science_web_bridge as swb

    def swap(rows):
        rows[0]["grid_col"], rows[1]["grid_col"] = rows[1]["grid_col"], rows[0]["grid_col"]
    si, _ = _campaign(tmp_path, 5.035, -82.96, 40.0, 15, edit_rows=swap)
    with pytest.raises(swb.MosaicShapeError, match="not monotonic"):
        swb.build_mosaic_grid(si, 40.0 / 14, swb.load_planned_lattice(si))


def test_a_polar_campaign_builds_maps_a_b_c(tmp_path):
    import science_web_bridge as swb
    from tests.test_science_web_bridge import cfg_with
    si, _ = _campaign(tmp_path, 5.035, -82.96, 30.0, 7)
    built = swb.build_all_products(si, cfg_with())
    assert built["map_a_valid"].sum() == 49
    assert "spherical offset frame" in built["mosaic_geometry"]["projection"]
