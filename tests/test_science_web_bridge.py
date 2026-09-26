"""science_web_bridge.py corrections (Felipe's real-run review): spatial SUPPORT (shared, hard radius) vs
presentation SMOOTHING (declared kernel FWHM, independent controls - request #2), a used-point-set guarantee
that is structural rather than a post-hoc intersection (request #3, with an explicit adversarial case), and
the corrected velocity-window default. Synthetic Level 1 only (science_engine.simulation, frozen) - never a
real REDUCE session, so this file runs fast and never touches real hardware data.
"""
import numpy as np
import pytest

from science_engine.grid import pixel_centers_deg
from science_engine.gridding import spatial_weight_for_point
from science_engine.models import BeamModel, ScienceGrid
from science_engine.simulation import build_synthetic_science_input, rectangular_grid_specs
from science_engine.spatial import angular_separation_deg

from science_web_bridge import (MapConfig, MosaicShapeError, _reconcile_used_point_sets, auto_spatial_params,
                                bc_diagnostic, board_json_payload, build_all_products, build_mosaic_grid,
                                render_all_maps, viridis_hex)


def mosaic_input(n=6, spacing=1.0, **kw):
    specs = rectangular_grid_specs(12.0, -30.0, n, n, spacing_deg=spacing, calibration_level="UNCALIBRATED", **kw)
    return build_synthetic_science_input(specs, noise_sigma=0.02)


def cfg_with(beam_fwhm_deg=20.0, **overrides):
    base = dict(reduce_session_dir="<synthetic>", calibration_level_filter="UNCALIBRATED",
               beam_fwhm_deg=beam_fwhm_deg, beam_source="test", beam_status="CONFIGURED_OPERATIONAL")
    base.update(overrides)
    return MapConfig(**base)


# ---------------------------------------------------------------- MapConfig validation

def test_default_velocity_window_matches_frozen_engine_default():
    """A real deviation this task found and fixed: the bridge's own default used to be +/-300 km/s, three
    times science_engine.config.ScienceConfig's own +/-100 km/s default, for no declared reason."""
    from science_engine.config import ScienceConfig
    cfg = cfg_with()
    engine_default = ScienceConfig(reduce_session_dir="x", beam_fwhm_deg=1.0)
    assert cfg.velocity_window_min_m_s == engine_default.velocity_window_min_m_s == -100_000.0
    assert cfg.velocity_window_max_m_s == engine_default.velocity_window_max_m_s == 100_000.0


def test_smoothing_c_must_exceed_smoothing_b_when_both_given():
    with pytest.raises(ValueError, match="smoothing_fwhm_c_deg"):
        cfg_with(smoothing_fwhm_b_deg=2.0, smoothing_fwhm_c_deg=1.0)


def test_support_radius_must_be_positive():
    with pytest.raises(ValueError, match="support_radius_deg"):
        cfg_with(support_radius_deg=-1.0)


# ---------------------------------------------------------------- auto_spatial_params: support tied to
# real point spacing, NOT to the (possibly mis-scaled) reported instrument beam - request #2's "mantén fijo
# el haz físico" / separation of concerns, and the direct fix for the real run's root cause.

def test_auto_spatial_params_independent_of_beam_fwhm():
    si = mosaic_input(n=6, spacing=1.0)
    small_beam = auto_spatial_params(si, cfg_with(beam_fwhm_deg=1.5))
    huge_beam = auto_spatial_params(si, cfg_with(beam_fwhm_deg=20.0))   # the real run's observer_config value
    assert small_beam["support_radius_deg"] == pytest.approx(huge_beam["support_radius_deg"])
    assert small_beam["smoothing_fwhm_b_deg"] == pytest.approx(huge_beam["smoothing_fwhm_b_deg"])
    assert small_beam["smoothing_fwhm_c_deg"] == pytest.approx(huge_beam["smoothing_fwhm_c_deg"])
    assert small_beam["nearest_neighbor_spacing_deg"] == pytest.approx(1.0, abs=0.02)
    assert small_beam["smoothing_fwhm_c_deg"] > small_beam["smoothing_fwhm_b_deg"]


def test_auto_spatial_params_c_gt_b_even_with_manual_b_only():
    si = mosaic_input(n=6, spacing=1.0)
    resolved = auto_spatial_params(si, cfg_with(smoothing_fwhm_b_deg=0.5))
    assert resolved["smoothing_fwhm_c_deg"] > resolved["smoothing_fwhm_b_deg"] == 0.5


# ---------------------------------------------------------------- _reconcile_used_point_sets: the structural
# fix for request #3, plus the explicit adversarial case proving the risk it fixes is real, not hypothetical.

def test_reconcile_used_point_sets_passes_through_when_identical():
    assert _reconcile_used_point_sets({1, 2, 3}, {1, 2, 3}, support_radius_deg=1.0) == {1, 2, 3}


def test_reconcile_used_point_sets_blocks_rather_than_intersecting_on_divergence():
    """The OLD behaviour (compute B and C, then silently use the intersection - the exact thing request #3
    called insufficient) is gone: any divergence now raises and blocks the whole comparison."""
    with pytest.raises(ValueError, match="BLOCKED"):
        _reconcile_used_point_sets({1, 2, 3}, {1, 2}, support_radius_deg=1.0)


def test_adversarial_two_different_cutoff_radii_really_can_diverge_on_the_same_grid():
    """Demonstrates the real mechanism request #3 warned about: the OLD design (beam_cutoff_{b,c}_n_fwhm on
    ONE beam) computes spatial support as cutoff_n_fwhm x fwhm_deg - two DIFFERENT absolute radii - and nothing
    stops a point from having weight under the looser radius but none at all under the tighter one. This is
    not a hypothetical: constructed here directly against the frozen spatial_weight_for_point(), then checked
    that _reconcile_used_point_sets() blocks exactly this outcome instead of quietly intersecting."""
    grid = ScienceGrid(frame="icrs", center_ra_deg=180.0, center_dec_deg=0.0, width_deg=2.0, height_deg=2.0,
                       pixel_scale_deg=1.0, nx=2, ny=2)
    pixel_ra, pixel_dec = pixel_centers_deg(grid)
    # A point at the grid's own corner sits ~0.707 deg (half the pixel diagonal) from the nearest pixel centre.
    point_ra, point_dec = grid.center_ra_deg - 1.0, grid.center_dec_deg - 1.0
    d_nearest = float(np.min(angular_separation_deg(
        np.full(pixel_ra.size, point_ra), np.full(pixel_ra.size, point_dec), pixel_ra.ravel(), pixel_dec.ravel())))
    assert 0.5 < d_nearest < 1.0   # sanity check on the geometry this test relies on

    beam_tight = BeamModel(fwhm_deg=1.0, cutoff_n_fwhm=d_nearest * 0.9)   # OLD map B: excludes the point
    beam_loose = BeamModel(fwhm_deg=1.0, cutoff_n_fwhm=d_nearest * 1.1)   # OLD map C: includes the point
    w_tight = spatial_weight_for_point(grid, point_ra, point_dec, beam_tight)
    w_loose = spatial_weight_for_point(grid, point_ra, point_dec, beam_loose)
    assert not np.any(w_tight > 0), "the adversarial geometry must actually exclude the point under the tight cutoff"
    assert np.any(w_loose > 0), "...and include it under the loose cutoff - a real, structurally possible divergence"

    used_b, used_c = {1}, {1}  # a hypothetical second point that both would use, plus...
    used_b_missing_point = set()          # ...this point, present in C's set (looser radius) but not B's (tight)
    with pytest.raises(ValueError, match="BLOCKED"):
        _reconcile_used_point_sets(used_b | used_b_missing_point, used_c | {2}, support_radius_deg=d_nearest)


# ---------------------------------------------------------------- build_all_products: end-to-end, on a
# mosaic shaped like the real flagged run (36 points, ~1 deg spacing, a mis-scaled 20 deg reported beam).

def test_build_all_products_shares_identical_used_point_set_by_construction():
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    assert len(built["used_point_set"]) == 36
    assert "guaranteed identical" in built["used_point_set_note"]


def test_build_all_products_grid_is_not_sized_off_the_oversized_reported_beam():
    """Regression test for the real root cause: beam_fwhm_deg=20 (observer_config.json's default) on a real
    ~6x5 deg, 36-point mosaic used to produce a ~65x65 deg grid (extent_margin_beams x beam_fwhm_deg margin
    alone dwarfing the real field). Grid sizing is now tied to support_radius_deg (point-spacing derived)."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    assert built["grid"].width_deg < 15.0
    assert built["grid"].height_deg < 15.0


def test_build_all_products_b_and_c_genuinely_differ_not_just_in_cutoff_label():
    """Request #1: never assert 'more smoothing' from the cutoff/kernel parameter alone - check real numbers."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    diag = bc_diagnostic(built)
    assert diag["n_support_only_b"] == 0 and diag["n_support_only_c"] == 0   # identical support (request #3)
    assert diag["n_support_common"] > 0
    assert diag["b_minus_c_common_support"]["std"] > 0                      # genuinely different values
    assert diag["c_mean_abs_gradient"] < diag["b_mean_abs_gradient"]        # C is genuinely flatter (heavier)


def test_map_a_values_match_per_point_integrated_values_exactly():
    """Explicit verification request: "los 36 valores de A coinciden con points.csv y no cambiaron por
    razones de presentación" - map_a_value/uncertainty are placed FROM per_point_integrated_values()'s own
    output, never recomputed/rounded/re-derived for display."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    n_checked = 0
    for row in built["point_rows"]:
        if row["status"] != "USED":
            continue
        r, c = built["point_cell"][row["point_index"]]
        assert built["map_a_value"][r, c] == row["value"]
        assert built["map_a_uncertainty"][r, c] == row["uncertainty"]
        n_checked += 1
    assert n_checked == 36


# ---------------------------------------------------------------- build_mosaic_grid: the SAME NxM board for
# A/B/C (request: "el mismo tablero de 6x6 cuadros... idénticos límites... posición de cada celda").

def test_build_mosaic_grid_recovers_a_clean_6x6_lattice():
    si = mosaic_input(n=6, spacing=1.0)
    grid, point_cell, n_rows, n_cols = build_mosaic_grid(si, 1.0)
    assert (n_rows, n_cols) == (6, 6)
    assert len(point_cell) == 36
    assert len(set(point_cell.values())) == 36   # no two points share a cell
    assert grid.nx == 6 and grid.ny == 6
    assert grid.width_deg == pytest.approx(6.0) and grid.height_deg == pytest.approx(6.0)


def test_build_mosaic_grid_survives_realistic_row_to_row_projection_drift():
    """Regression test for a REAL bug this task found on the real 36-point run: each row's tangent-plane RA
    spacing measurably drifted ~6% row-to-row (an expected effect of one global cos(dec) projection applied
    to rows at slightly different real declinations - see build_mosaic_grid's own docstring), which made a
    naive round(x/spacing) collide two real points into the same nominal cell (`3 cell(s) claimed by more
    than one point` on the real deployed run). Reproduced synthetically here with the same magnitude of
    drift, and confirmed the gap-based clustering (_cluster_1d) still recovers a clean 6x6 board."""
    from science_engine.simulation import SyntheticPointSpec
    center_dec = -33.4
    specs = []
    idx = 1
    for row in range(6):
        dec = center_dec + (row - 2.5) * 1.0
        row_spacing = 1.03 - row * 0.012   # ~6% drift end to end, matching the real measured pattern
        for col in range(6):
            dra_deg = (col - 2.5) * row_spacing / np.cos(np.radians(dec))
            specs.append(SyntheticPointSpec(point_index=idx, ra_hours=(321.6 + dra_deg) / 15.0,
                                            dec_degrees=dec, calibration_level="UNCALIBRATED"))
            idx += 1
    si = build_synthetic_science_input(specs, noise_sigma=0.02)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    assert built["n_rows"] == 6 and built["n_cols"] == 6
    assert len(built["point_cell"]) == 36
    assert len(set(built["point_cell"].values())) == 36


def test_build_mosaic_grid_raises_on_a_genuine_cell_collision():
    """Adversarial: two points closer together than half the declared spacing must never be silently placed
    in the same cell (one overwriting the other) - build_mosaic_grid fails loudly instead."""
    from science_engine.simulation import SyntheticPointSpec
    specs = rectangular_grid_specs(12.0, -30.0, 3, 3, spacing_deg=1.0, calibration_level="UNCALIBRATED")
    specs.append(SyntheticPointSpec(point_index=100, ra_hours=specs[0].ra_hours + 0.0005,
                                    dec_degrees=specs[0].dec_degrees + 0.05, calibration_level="UNCALIBRATED"))
    si = build_synthetic_science_input(specs, noise_sigma=0.0)
    with pytest.raises(MosaicShapeError, match="do not form a clean rectangular lattice"):
        build_mosaic_grid(si, 1.0)


def test_partial_campaign_leaves_the_gap_transparent_never_fabricated():
    """Request: "una celda faltante en campaña parcial no se hace pasar por observación" - a real 35/36
    partial run must still recover the full 6x6 board (from the other 35 points) with the missing cell
    left invalid/NaN in map A, never zero, never silently filled."""
    specs = [s for s in rectangular_grid_specs(12.0, -30.0, 6, 6, spacing_deg=1.0,
                                               calibration_level="UNCALIBRATED") if s.point_index != 18]
    si = build_synthetic_science_input(specs, noise_sigma=0.02)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    assert built["n_rows"] == 6 and built["n_cols"] == 6
    all_possible = {(r, c) for r in range(6) for c in range(6)}
    missing = all_possible - set(built["point_cell"].values())
    assert len(missing) == 1
    r, c = next(iter(missing))
    assert not built["map_a_valid"][r, c]
    assert np.isnan(built["map_a_value"][r, c])


# ---------------------------------------------------------------- color consistency (request: "el mismo valor
# numerico debe tener el mismo color en todas las vistas... comprobación automática con valores concretos").

def test_viridis_hex_matches_the_exact_colormap_object_the_png_exports_use():
    """viridis_hex() is not an approximation of the PNG/SVG/PDF renderer's colour - render_all_maps() colours
    every board with plt.get_cmap("viridis") too, so both call the IDENTICAL matplotlib colormap on the same
    normalised value. Checked here against several concrete values across the scale."""
    import matplotlib.pyplot as plt
    cmap = plt.get_cmap("viridis")
    vmin, vmax = -81581.966, -77870.681   # the real flagged run's own color limits
    for value in (vmin, -80000.123, -79000.5, -78500.0, vmax):
        t = (value - vmin) / (vmax - vmin)
        r, g, b, _ = cmap(t)
        expected = "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))
        assert viridis_hex(value, vmin, vmax) == expected


def test_viridis_hex_never_fabricates_a_color_for_invalid_values():
    assert viridis_hex(None, 0.0, 10.0) is None
    assert viridis_hex(float("nan"), 0.0, 10.0) is None
    assert viridis_hex(float("inf"), 0.0, 10.0) is None


def test_equal_values_get_identical_colors_every_time():
    assert viridis_hex(-79000.0, -81000.0, -78000.0) == viridis_hex(-79000.0, -81000.0, -78000.0)


def test_board_json_payload_colors_match_viridis_hex_for_every_cell():
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    payload = board_json_payload(built)
    vmin, vmax = payload["color_vmin"], payload["color_vmax"]
    n_valid = 0
    for cell in payload["cells"]:
        for key in ("a", "b", "c"):
            d = cell[key]
            if d["valid"]:
                assert d["color"] == viridis_hex(d["value"], vmin, vmax)
                n_valid += 1
            else:
                assert d["color"] is None
    assert n_valid > 0


def test_board_json_payload_cells_share_exact_grid_geometry_as_a_b_c():
    """Request: "celda (fila,columna) tiene la misma RA/Dec en las tres" - board.json's per-cell RA/Dec IS
    the same grid pixel_centers_deg() build_cube()/integrated_map() computed A/B/C from - one geometry."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    payload = board_json_payload(built)
    assert len(payload["cells"]) == built["n_rows"] * built["n_cols"]
    assert payload["grid"] == built["grid"].to_dict()


# ---------------------------------------------------------------- rendering smoke test (files, not just existence
# of a status code) - includes the new B-C difference and combined A/B/C exports.

def test_render_all_maps_writes_the_bc_difference_and_combined_exports(tmp_path):
    si = mosaic_input(n=6, spacing=1.0)
    cfg = cfg_with(beam_fwhm_deg=20.0)
    built = build_all_products(si, cfg)
    exports, quality_kind, diag = render_all_maps(built, cfg, si.campaign_id, si.reduce_session_id, tmp_path)
    assert "map_b_minus_c" in exports and "map_abc_combined" in exports
    for ext in ("png", "svg"):
        assert (tmp_path / f"map_b_minus_c.{ext}").is_file()
    for ext in ("png", "svg", "pdf"):
        assert (tmp_path / f"map_abc_combined.{ext}").is_file()
    assert (tmp_path / "map_a_no_interp.png").is_file()
    assert diag["n_support_only_b"] == 0 and diag["n_support_only_c"] == 0
