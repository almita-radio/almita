"""science_web_bridge.py corrections. Two rounds of real feedback on the same module, all verified against
synthetic Level 1 data (science_engine.simulation, frozen) - never a real REDUCE session, so this file runs
fast and never touches real hardware data:

  round 1: spatial SUPPORT (shared, hard radius) vs presentation SMOOTHING (declared kernel FWHM) as
  independent controls, a used-point-set guarantee that is structural rather than a post-hoc intersection
  (with an explicit adversarial case), and the corrected velocity-window default.

  round 2: A (the real N x M board), B and C (genuinely FINER interpolated rasters - build_fine_grid) with a
  strictly increasing pixel count left to right, one shared colour scale/legend, and click/spectrum
  interaction restricted to A's own real cells.
"""
import numpy as np
import pytest

from science_engine.grid import pixel_centers_deg
from science_engine.gridding import spatial_weight_for_point
from science_engine.models import BeamModel, ScienceGrid
from science_engine.simulation import build_synthetic_science_input, rectangular_grid_specs
from science_engine.spatial import angular_separation_deg

from science_web_bridge import (MapConfig, MosaicShapeError, _odd_ratio_companion_factor,
                                _reconcile_used_point_sets, auto_spatial_params, bc_exact_coordinate_consistency_summary,
                                board_json_payload, build_all_products, build_fine_grid, build_mosaic_grid,
                                loo_cross_validation_summary, noise_dominance_summary, render_all_maps,
                                spatial_confidence_summary, viridis_hex)


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


def test_smoothing_fwhm_must_be_positive():
    with pytest.raises(ValueError, match="smoothing_fwhm_deg"):
        cfg_with(smoothing_fwhm_deg=-1.0)


def test_support_radius_must_be_positive():
    with pytest.raises(ValueError, match="support_radius_deg"):
        cfg_with(support_radius_deg=-1.0)


def test_interp_factor_must_be_at_least_2():
    """Request: B/C must add real NEW pixels between measurements - a factor of 1 would just be the board
    again, which is exactly the earlier design this task corrected."""
    with pytest.raises(ValueError, match="interp_factor"):
        cfg_with(interp_factor_b=1)


def test_interp_factor_c_must_exceed_interp_factor_b():
    """Request: the resolution increase A -> B -> C must be unambiguous - C denser than B, always."""
    with pytest.raises(ValueError, match="interp_factor_c"):
        cfg_with(interp_factor_b=6, interp_factor_c=3)


# ---------------------------------------------------------------- auto_spatial_params: support tied to
# real point spacing, NOT to the (possibly mis-scaled) reported instrument beam.

def test_auto_spatial_params_independent_of_beam_fwhm():
    si = mosaic_input(n=6, spacing=1.0)
    small_beam = auto_spatial_params(si, cfg_with(beam_fwhm_deg=1.5))
    huge_beam = auto_spatial_params(si, cfg_with(beam_fwhm_deg=20.0))   # the real run's observer_config value
    assert small_beam["support_radius_deg"] == pytest.approx(huge_beam["support_radius_deg"])
    assert small_beam["smoothing_fwhm_deg"] == pytest.approx(huge_beam["smoothing_fwhm_deg"])
    assert small_beam["nearest_neighbor_spacing_deg"] == pytest.approx(1.0, abs=0.02)


def test_auto_spatial_params_smoothing_fwhm_manual_override_is_honoured():
    """B and C now share ONE smoothing_fwhm_deg (see MapConfig's own docstring for why the earlier
    light/heavier split was dropped - LOO-CV found it made no measurable difference) - an explicit override
    must be used as-is, never silently widened for one of the two maps."""
    si = mosaic_input(n=6, spacing=1.0)
    resolved = auto_spatial_params(si, cfg_with(smoothing_fwhm_deg=0.5))
    assert resolved["smoothing_fwhm_deg"] == 0.5


# ---------------------------------------------------------------- _reconcile_used_point_sets: structural
# guarantee, plus the explicit adversarial case proving the risk it guards against is real, not hypothetical.

def test_reconcile_used_point_sets_passes_through_when_identical():
    assert _reconcile_used_point_sets({1, 2, 3}, {1, 2, 3}, support_radius_deg=1.0) == {1, 2, 3}


def test_reconcile_used_point_sets_blocks_rather_than_intersecting_on_divergence():
    with pytest.raises(ValueError, match="BLOCKED"):
        _reconcile_used_point_sets({1, 2, 3}, {1, 2}, support_radius_deg=1.0)


def test_adversarial_two_different_cutoff_radii_really_can_diverge_on_the_same_grid():
    """Demonstrates the real mechanism a used-point-set divergence can come from: two DIFFERENT cutoff radii
    on the same grid CAN give a point weight under one and none under the other - constructed here directly
    against the frozen spatial_weight_for_point(), then checked that _reconcile_used_point_sets() blocks
    exactly this outcome instead of quietly intersecting."""
    grid = ScienceGrid(frame="icrs", center_ra_deg=180.0, center_dec_deg=0.0, width_deg=2.0, height_deg=2.0,
                       pixel_scale_deg=1.0, nx=2, ny=2)
    pixel_ra, pixel_dec = pixel_centers_deg(grid)
    point_ra, point_dec = grid.center_ra_deg - 1.0, grid.center_dec_deg - 1.0
    d_nearest = float(np.min(angular_separation_deg(
        np.full(pixel_ra.size, point_ra), np.full(pixel_ra.size, point_dec), pixel_ra.ravel(), pixel_dec.ravel())))
    assert 0.5 < d_nearest < 1.0   # sanity check on the geometry this test relies on

    beam_tight = BeamModel(fwhm_deg=1.0, cutoff_n_fwhm=d_nearest * 0.9)
    beam_loose = BeamModel(fwhm_deg=1.0, cutoff_n_fwhm=d_nearest * 1.1)
    w_tight = spatial_weight_for_point(grid, point_ra, point_dec, beam_tight)
    w_loose = spatial_weight_for_point(grid, point_ra, point_dec, beam_loose)
    assert not np.any(w_tight > 0), "the adversarial geometry must actually exclude the point under the tight cutoff"
    assert np.any(w_loose > 0), "...and include it under the loose cutoff - a real, structurally possible divergence"

    with pytest.raises(ValueError, match="BLOCKED"):
        _reconcile_used_point_sets({1}, {1, 2}, support_radius_deg=d_nearest)


# ---------------------------------------------------------------- build_mosaic_grid (map A's real board)

def test_build_mosaic_grid_recovers_a_clean_6x6_lattice():
    si = mosaic_input(n=6, spacing=1.0)
    grid, point_cell, n_rows, n_cols = build_mosaic_grid(si, 1.0)
    assert (n_rows, n_cols) == (6, 6)
    assert len(point_cell) == 36
    assert len(set(point_cell.values())) == 36
    assert grid.nx == 6 and grid.ny == 6
    assert grid.width_deg == pytest.approx(6.0) and grid.height_deg == pytest.approx(6.0)


def test_build_mosaic_grid_survives_realistic_row_to_row_projection_drift():
    """Regression test for a REAL bug found on the real 36-point run: each row's tangent-plane RA spacing
    measurably drifted ~6% row-to-row (an expected effect of one global cos(dec) projection applied to rows
    at slightly different real declinations), which made a naive round(x/spacing) collide two real points
    into the same nominal cell. Reproduced synthetically with the same magnitude of drift; the gap-based
    clustering (_cluster_1d) still recovers a clean 6x6 board."""
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
    from science_engine.simulation import SyntheticPointSpec
    specs = rectangular_grid_specs(12.0, -30.0, 3, 3, spacing_deg=1.0, calibration_level="UNCALIBRATED")
    specs.append(SyntheticPointSpec(point_index=100, ra_hours=specs[0].ra_hours + 0.0005,
                                    dec_degrees=specs[0].dec_degrees + 0.05, calibration_level="UNCALIBRATED"))
    si = build_synthetic_science_input(specs, noise_sigma=0.0)
    with pytest.raises(MosaicShapeError, match="do not form a clean rectangular lattice"):
        build_mosaic_grid(si, 1.0)


def test_partial_campaign_leaves_the_gap_transparent_never_fabricated():
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


# ---------------------------------------------------------------- build_fine_grid + progressive resolution
# (the CURRENT task: "la progresión de resolución sea evidente de izquierda a derecha").

def test_build_fine_grid_same_physical_footprint_denser_pixels():
    si = mosaic_input(n=6, spacing=1.0)
    board, _, n_rows, n_cols = build_mosaic_grid(si, 1.0)
    fine = build_fine_grid(board, 4)
    assert (fine.ny, fine.nx) == (n_rows * 4, n_cols * 4)
    assert fine.width_deg == pytest.approx(board.width_deg)
    assert fine.height_deg == pytest.approx(board.height_deg)
    assert fine.center_ra_deg == pytest.approx(board.center_ra_deg)
    assert fine.center_dec_deg == pytest.approx(board.center_dec_deg)


def test_build_fine_grid_rejects_a_factor_below_2():
    si = mosaic_input(n=6, spacing=1.0)
    board, _, _, _ = build_mosaic_grid(si, 1.0)
    with pytest.raises(ValueError):
        build_fine_grid(board, 1)


def test_grid_dimensions_increase_strictly_a_lt_b_lt_c():
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0, interp_factor_b=3, interp_factor_c=6))
    n_a = built["n_rows"] * built["n_cols"]
    n_b = built["map_b"].value.size
    n_c = built["map_c"].value.size
    assert (built["n_rows"], built["n_cols"]) == (6, 6)
    assert built["map_b"].value.shape == (18, 18)
    assert built["map_c"].value.shape == (36, 36)
    assert n_a < n_b < n_c


def test_a_b_c_share_the_exact_same_physical_footprint():
    """Request #3: "los tres paneles deben... tener la misma extension espacial y orientacion"."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    board, gb, gc = built["board_grid"], built["grid_b"], built["grid_c"]
    for g in (gb, gc):
        assert g.width_deg == pytest.approx(board.width_deg)
        assert g.height_deg == pytest.approx(board.height_deg)
        assert g.center_ra_deg == pytest.approx(board.center_ra_deg)
        assert g.center_dec_deg == pytest.approx(board.center_dec_deg)


def test_interpolated_pixels_contain_genuinely_new_values_not_just_raw_readings():
    """Request #2: "deben aparecer pixeles nuevos... la interpolacion estima valores entre mediciones" - most
    of B's pixels must be genuinely NEW numbers, not one of the 36 real readings repeated/enlarged."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    raw_values = set(np.round(built["map_a_value"][built["map_a_valid"]], 6).tolist())
    b_values = built["map_b"].value[built["map_b"].valid]
    novel = [v for v in b_values if round(float(v), 6) not in raw_values]
    assert len(novel) > 0.9 * len(b_values)


def test_used_point_set_still_matches_between_b_and_c_at_different_resolutions():
    """The support-radius/used-point-set guarantee must survive B and C now being DIFFERENT-resolution
    grids over the SAME physical footprint (previously they were the identical grid)."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    assert len(built["used_point_set"]) == 36
    assert "matched exactly" in built["used_point_set_note"]


def test_build_all_products_board_is_not_sized_off_the_oversized_reported_beam():
    """Regression test for the real root cause of an earlier review: beam_fwhm_deg=20 (observer_config.json's
    default) on a real ~6x5 deg, 36-point mosaic used to produce a ~65x65 deg grid. The real board (map A) is
    sized from the campaign's own point spacing, never from the reported beam."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    assert built["board_grid"].width_deg < 15.0
    assert built["board_grid"].height_deg < 15.0


def test_map_a_values_match_per_point_integrated_values_exactly():
    """"Los 36 valores de A coinciden con points.csv y no cambiaron por razones de presentacion" -
    map_a_value/uncertainty are placed FROM per_point_integrated_values()'s own output, never recomputed."""
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


# ---------------------------------------------------------------- color consistency (shared scale/legend
# across A/B/C - request #3).

def test_viridis_hex_matches_the_exact_colormap_object_the_png_exports_use():
    """viridis_hex() is not an approximation of the PNG/SVG/PDF renderer's colour - render_all_maps() colours
    every panel with plt.get_cmap("viridis") too, so both call the IDENTICAL matplotlib colormap on the same
    normalised value. Checked here against several concrete values across the scale."""
    import matplotlib.pyplot as plt
    cmap = plt.get_cmap("viridis")
    vmin, vmax = -81581.966, -77870.681   # a real run's own color limits
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


def test_board_json_payload_is_map_a_only_and_colors_match_viridis_hex():
    """board.json now drives ONLY the interactive panel A (B/C are plain images, no per-cell payload,
    no contributors, no click) - see board_json_payload's own docstring."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    payload = board_json_payload(built)
    assert len(payload["cells"]) == built["n_rows"] * built["n_cols"] == 36
    assert payload["grid"] == built["board_grid"].to_dict()
    assert payload["color_stops"] and len(payload["color_stops"]) >= 5
    assert "color_units" in payload
    vmin, vmax = payload["color_vmin"], payload["color_vmax"]
    n_valid = 0
    for cell in payload["cells"]:
        assert "b" not in cell and "c" not in cell and "contributors" not in cell
        if cell["valid"]:
            assert cell["color"] == viridis_hex(cell["value"], vmin, vmax)
            n_valid += 1
        else:
            assert cell["color"] is None
    assert n_valid > 0


# ---------------------------------------------------------------- rendering: files exist, dims progress,
# B-C per-pixel diff is gone (no longer meaningful across differing resolutions).

def test_render_all_maps_writes_progressively_denser_exports(tmp_path):
    si = mosaic_input(n=6, spacing=1.0)
    cfg = cfg_with(beam_fwhm_deg=20.0)
    built = build_all_products(si, cfg)
    exports, quality_kind = render_all_maps(built, cfg, si.campaign_id, si.reduce_session_id, tmp_path)
    assert "map_abc_combined" in exports
    assert "map_b_minus_c" not in exports   # explicitly removed: no longer well-defined across differing resolutions
    for name in ("map_a_no_interp", "map_b_smooth", "map_c_heavy"):
        assert (tmp_path / f"{name}.png").is_file()
    for ext in ("png", "svg", "pdf"):
        assert (tmp_path / f"map_abc_combined.{ext}").is_file()
    assert (tmp_path / "map_coverage.png").is_file()
    # the authoritative resolution check is the underlying arrays these PNGs are a rendering of:
    assert built["map_a_value"].shape == (6, 6)
    assert built["map_b"].value.shape == (18, 18)
    assert built["map_c"].value.shape == (36, 36)


def test_render_all_maps_writes_bare_chrome_free_images_for_b_and_c(tmp_path):
    """Request: "A llena su panel, pero B y C aparecen como graficos pequenos dentro de grandes cajas" -
    the on-screen images for B/C must be plain, chrome-free renderings (no title/colorbar/caption baked in,
    same footprint as A) so the browser can size all three panels identically; the full titled/colorbar'd
    exports must still be written unchanged for download."""
    si = mosaic_input(n=6, spacing=1.0)
    cfg = cfg_with(beam_fwhm_deg=20.0)
    built = build_all_products(si, cfg)
    exports, _ = render_all_maps(built, cfg, si.campaign_id, si.reduce_session_id, tmp_path)
    for name in ("map_b_smooth", "map_c_heavy"):
        assert f"{name}_bare" in exports
        bare_path = tmp_path / exports[f"{name}_bare"][0]
        assert bare_path.is_file()
        full_path = tmp_path / f"{name}.png"
        assert full_path.is_file()
        # chrome-free image must be smaller (no title/colorbar/margins) than the full presentation PNG
        assert bare_path.stat().st_size < full_path.stat().st_size


# ---------------------------------------------------------------- one shared kernel for B and C (dropped the
# earlier light/heavier split - see MapConfig.smoothing_fwhm_deg's own docstring for the LOO-CV finding that
# justified it): B and C now differ ONLY in raster density.

def test_build_all_products_uses_one_shared_kernel_for_b_and_c():
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    assert "sc" in built and "beam" in built
    assert "sc_b" not in built and "sc_c" not in built and "beam_b" not in built and "beam_c" not in built
    assert set(built["spatial_params"].keys()) >= {"support_radius_deg", "smoothing_fwhm_deg"}
    assert "smoothing_fwhm_b_deg" not in built["spatial_params"]


# ---------------------------------------------------------------- round 3: circular halos traced to
# individual points' own noise dominating their neighbourhood under Gaussian-weighted interpolation - an
# honest coverage/confidence cue (n_pointings, already computed by the frozen integrated_map(), never
# re-derived), never a blur, per the explicit "no ocultes el problema con un blur" instruction.

def test_spatial_confidence_flags_single_point_pixels_when_support_smaller_than_spacing():
    """With a support radius smaller than the point spacing, a pixel right next to one point and far from
    any other must be backed by exactly that ONE point (n_pointings==1) - the real mechanism behind a
    circular halo: that pixel's value is entirely one point's own reading, un-corroborated."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0, support_radius_deg=0.3,
                                            smoothing_fwhm_deg=0.3))
    conf = spatial_confidence_summary(built)
    for key in ("b", "c"):
        assert conf[key]["n_single_point_pixels"] > 0
        assert 0 < conf[key]["single_point_fraction"] <= 1.0
        assert conf[key]["max_n_pointings"] >= 1


def test_spatial_confidence_summary_shape():
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    conf = spatial_confidence_summary(built)
    assert set(conf.keys()) == {"b", "c"}
    for key in ("b", "c"):
        for field in ("n_valid_pixels", "n_single_point_pixels", "single_point_fraction", "max_n_pointings"):
            assert field in conf[key]


def test_outlier_point_creates_a_locally_dominant_blob_that_persists_at_higher_resolution():
    """Direct, controlled reproduction of the real diagnosis (REDUCE-20260919-234712-712870, point 30 at
    RA=356.02 Dec=-36.08, raw value -998.4): a single point with an extreme value creates a local bump
    centered on ITS OWN cell in the interpolated maps - and that centering survives a real increase in
    raster density with the SAME method (Gaussian-weighted averaging, same support/smoothing), proving the
    blob is a property of the data + method, not a resolution/rendering artifact."""
    specs = rectangular_grid_specs(12.0, -30.0, 6, 6, spacing_deg=1.0, calibration_level="UNCALIBRATED")
    outlier_idx = 18
    outlier_spec = next(s for s in specs if s.point_index == outlier_idx)
    outlier_ra_deg = outlier_spec.ra_hours * 15.0

    def amp(ra_deg, dec_deg):
        return 40.0 if abs(ra_deg - outlier_ra_deg) < 1e-6 and abs(dec_deg - outlier_spec.dec_degrees) < 1e-6 else 0.0

    # a tiny but nonzero noise_sigma - science_engine's own bin_validity() correctly treats an EXACTLY zero
    # reported uncertainty as invalid data (sigma<=0 is never valid), not a usable "noiseless" measurement.
    si = build_synthetic_science_input(specs, noise_sigma=1e-4, line_amplitude_fn=amp)
    board_r, board_c = build_mosaic_grid(si, 1.0)[1][outlier_idx]

    for factor_c in (6, 18):   # same method, increasingly dense raster - the same 6x6 board's own spacing
        built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0, interp_factor_b=3, interp_factor_c=factor_c))
        map_c = built["map_c"]
        finite = np.where(map_c.valid, map_c.value, -np.inf)
        r_peak, c_peak = np.unravel_index(np.nanargmax(finite), map_c.value.shape)
        assert (r_peak // factor_c, c_peak // factor_c) == (board_r, board_c), (
            f"at {factor_c}x, the peak pixel's board cell moved away from the real outlier point's own cell")


def test_render_all_maps_confidence_overlay_never_mutates_the_underlying_values(tmp_path):
    """The honest coverage cue is presentation-only (dimming/hatching in the saved image) - it must never
    touch the real, already-computed map_b/map_c arrays render_all_maps was given."""
    si = mosaic_input(n=6, spacing=1.0)
    cfg = cfg_with(beam_fwhm_deg=20.0)
    built = build_all_products(si, cfg)
    b_before = built["map_b"].value.copy()
    c_before = built["map_c"].value.copy()
    b_np_before = built["map_b"].n_pointings.copy()
    render_all_maps(built, cfg, si.campaign_id, si.reduce_session_id, tmp_path)
    assert np.array_equal(built["map_b"].value, b_before, equal_nan=True)
    assert np.array_equal(built["map_c"].value, c_before, equal_nan=True)
    assert np.array_equal(built["map_b"].n_pointings, b_np_before)


def test_render_all_maps_writes_coverage_density_export(tmp_path):
    si = mosaic_input(n=6, spacing=1.0)
    cfg = cfg_with(beam_fwhm_deg=20.0)
    built = build_all_products(si, cfg)
    exports, _ = render_all_maps(built, cfg, si.campaign_id, si.reduce_session_id, tmp_path)
    assert "map_coverage_density" in exports
    for ext in ("png", "svg"):
        assert (tmp_path / f"map_coverage_density.{ext}").is_file()


# ---------------------------------------------------------------- noise_dominance_summary: the direct,
# quantitative version of the same diagnosis - reproduces the real numbers found on
# REDUCE-20260919-234712-712870 (median |nearest-neighbour diff| ~281 vs sqrt(2)*median(unc) ~236,
# correlation ~-0.09) in controlled, synthetic form.

def test_noise_dominance_flags_pure_noise_as_consistent():
    """36 points, real per-point noise, NO spatial signal (line_amplitude_fn left at its zero default) -
    must be reported as statistically consistent with pure noise at the point spacing."""
    si = mosaic_input(n=6, spacing=1.0)
    cfg = cfg_with(beam_fwhm_deg=20.0)
    from science_web_bridge import per_point_integrated_values
    from science_engine.cube import canonical_velocity_axis
    velocity_axis = canonical_velocity_axis(si)
    spatial = auto_spatial_params(si, cfg)
    from science_web_bridge import _kernel_science_config
    sc = _kernel_science_config(cfg, spatial["smoothing_fwhm_deg"],
                               spatial["support_radius_deg"] / spatial["smoothing_fwhm_deg"], "k")
    point_rows = per_point_integrated_values(si, sc, velocity_axis)
    summary = noise_dominance_summary(point_rows)
    assert summary is not None
    assert summary["n_points"] == 36
    assert summary["consistent_with_pure_noise_at_point_spacing"] is True
    assert 0.3 < summary["ratio_observed_to_expected_noise"] < 3.0
    assert abs(summary["nearest_neighbor_value_correlation"]) < 0.5


def test_noise_dominance_flags_a_real_smooth_gradient_as_not_pure_noise():
    """The SAME point layout, but with a real, smooth (no noise) spatial signal that varies gently across
    the field - must NOT be flagged as noise: neighbouring points should correlate strongly and differ far
    less than independent noise alone would predict, since the injected sigma is tiny and the true signal
    dominates every point's own value."""
    specs = rectangular_grid_specs(12.0, -30.0, 6, 6, spacing_deg=1.0, calibration_level="UNCALIBRATED")

    def smooth_amp(ra_deg, dec_deg):
        return 10.0 + 2.0 * (dec_deg - specs[0].dec_degrees)   # a gentle, real linear gradient across the field

    si = build_synthetic_science_input(specs, noise_sigma=1e-4, line_amplitude_fn=smooth_amp)
    cfg = cfg_with(beam_fwhm_deg=20.0)
    from science_web_bridge import _kernel_science_config, per_point_integrated_values
    from science_engine.cube import canonical_velocity_axis
    velocity_axis = canonical_velocity_axis(si)
    spatial = auto_spatial_params(si, cfg)
    sc = _kernel_science_config(cfg, spatial["smoothing_fwhm_deg"],
                               spatial["support_radius_deg"] / spatial["smoothing_fwhm_deg"], "k")
    point_rows = per_point_integrated_values(si, sc, velocity_axis)
    summary = noise_dominance_summary(point_rows)
    assert summary is not None
    assert summary["consistent_with_pure_noise_at_point_spacing"] is False
    assert summary["nearest_neighbor_value_correlation"] > 0.3


def test_noise_dominance_summary_returns_none_with_too_few_points():
    assert noise_dominance_summary([{"status": "USED", "value": 1.0, "uncertainty": 0.1,
                                     "ra_deg": 0.0, "dec_degrees": 0.0}]) is None


def test_build_all_products_exposes_noise_dominance():
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    assert built["noise_dominance"] is not None
    assert "consistent_with_pure_noise_at_point_spacing" in built["noise_dominance"]


# ---------------------------------------------------------------- loo_cross_validation_summary: the direct,
# real leave-one-out validation request #3 asked for - "retira por turnos mediciones reales, predicelas
# usando las demas y compara prediccion contra valor medido". Reproduces (in controlled, synthetic form) the
# real result on REDUCE-20260919-234712-712870: predicted-vs-measured correlation ~0 for pure noise, RMS
# comparable to or worse than the field's own std - and shows the SAME check correctly recognises a real
# smooth signal as predictable, so it is not just "always says no".

def test_loo_cross_validation_returns_none_with_too_few_points():
    from science_engine.cube import canonical_velocity_axis
    from science_web_bridge import per_point_integrated_values
    si = mosaic_input(n=2, spacing=1.0)   # 4 points - below the function's own 5-point floor
    cfg = cfg_with(beam_fwhm_deg=20.0)
    spatial = auto_spatial_params(si, cfg)
    velocity_axis = canonical_velocity_axis(si)
    from science_web_bridge import _kernel_science_config
    sc = _kernel_science_config(cfg, spatial["smoothing_fwhm_deg"],
                               spatial["support_radius_deg"] / spatial["smoothing_fwhm_deg"], "k")
    point_rows = per_point_integrated_values(si, sc, velocity_axis)
    assert loo_cross_validation_summary(si, cfg, spatial, point_rows) is None


def test_loo_cross_validation_flags_pure_noise_as_unpredictable():
    """36 real points, real per-point noise, NO spatial signal - a held-out point's neighbours must NOT
    meaningfully predict it: |correlation| small and RMS at least comparable to the field's own std (not
    dramatically better, the way a real detected structure would give)."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    loo = built["loo_cross_validation"]
    assert loo is not None and loo["n_predictable"] >= 3
    assert loo["predicted_vs_measured_correlation"] is None or abs(loo["predicted_vs_measured_correlation"]) < 0.6
    assert loo["rms"] > 0.5 * loo["field_value_std"]


def test_loo_cross_validation_flags_a_real_smooth_gradient_as_predictable():
    """The SAME point layout with a real, smooth (near-noiseless) spatial gradient injected - a held-out
    point's neighbours SHOULD predict it well here: high correlation, RMS well below the field's own std.
    This is the discriminating case that shows the check is not simply always negative."""
    specs = rectangular_grid_specs(12.0, -30.0, 6, 6, spacing_deg=1.0, calibration_level="UNCALIBRATED")

    def smooth_amp(ra_deg, dec_deg):
        return 10.0 + 2.0 * (dec_deg - specs[0].dec_degrees)

    si = build_synthetic_science_input(specs, noise_sigma=1e-4, line_amplitude_fn=smooth_amp)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    loo = built["loo_cross_validation"]
    assert loo is not None and loo["n_predictable"] >= 3
    assert loo["predicted_vs_measured_correlation"] > 0.8
    assert loo["rms"] < 0.5 * loo["field_value_std"]
    assert loo["rms_worse_than_predicting_the_field_mean"] is False


def test_loo_cross_validation_subsamples_when_more_points_than_max():
    from science_engine.cube import canonical_velocity_axis
    from science_web_bridge import _kernel_science_config, per_point_integrated_values
    si = mosaic_input(n=9, spacing=1.0)   # 81 points
    cfg = cfg_with(beam_fwhm_deg=20.0)
    spatial = auto_spatial_params(si, cfg)
    velocity_axis = canonical_velocity_axis(si)
    sc = _kernel_science_config(cfg, spatial["smoothing_fwhm_deg"],
                               spatial["support_radius_deg"] / spatial["smoothing_fwhm_deg"], "k")
    point_rows = per_point_integrated_values(si, sc, velocity_axis)
    loo = loo_cross_validation_summary(si, cfg, spatial, point_rows, max_points=10)
    assert loo is not None
    assert loo["subsampled"] is True
    assert loo["n_points_evaluated"] == 10


def test_build_all_products_exposes_loo_cross_validation():
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    loo = built["loo_cross_validation"]
    assert loo is not None
    for field in ("bias", "rms", "mae", "predicted_vs_measured_correlation", "field_value_std",
                 "rms_worse_than_predicting_the_field_mean", "worst_5"):
        assert field in loo


# ---------------------------------------------------------------- bc_exact_coordinate_consistency_summary:
# a follow-up question ("por que r=0.946 no es ~1 si es el mismo campo") revealed the earlier "B vs C at
# matched coordinates" check (used to produce that r) was really NEAREST-PIXEL-INDEX matching, not a same-
# coordinate comparison - build_fine_grid()'s pixel centers only exactly coincide across two factors when
# their ratio is an ODD integer (proved in _odd_ratio_companion_factor's own docstring). These tests verify
# that algebra and the resulting exact (zero-diff) consistency check directly, without resampling any image.

def test_odd_ratio_companion_factor_finds_exact_nesting_for_odd_ratios():
    assert _odd_ratio_companion_factor(3) == (1, 3)     # matches shipped interp_factor_b
    assert _odd_ratio_companion_factor(6) == (2, 3)     # matches shipped interp_factor_c
    assert _odd_ratio_companion_factor(10) == (2, 5)
    assert _odd_ratio_companion_factor(12) == (4, 3)


def test_odd_ratio_companion_factor_is_degenerate_for_pure_powers_of_two():
    """A pure power of 2 has NO smaller grid whose centers exactly nest inside it under this convention -
    the function honestly returns itself (k=1) rather than a false companion; callers must check for this
    (see bc_exact_coordinate_consistency_summary's degenerate_self_comparison flag)."""
    for f in (2, 4, 8, 16):
        factor_lo, k = _odd_ratio_companion_factor(f)
        assert factor_lo == f and k == 1


def test_bc_exact_coordinate_consistency_is_exact_at_shared_coordinates():
    """The real, direct test: evaluate the SAME build_cube()/integrated_map() call at coordinates shared
    exactly by B/C and a small odd-ratio companion grid (never a resampled image) - must match to float
    precision, proving B and C compute the identical field and any earlier nonzero "B vs C" difference was a
    nearest-pixel quantization artifact of that comparison method, not a computational inconsistency."""
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0, interp_factor_b=3, interp_factor_c=6))
    bcc = built["bc_exact_coordinate_consistency"]
    for key in ("b", "c"):
        r = bcc[key]
        assert r["degenerate_self_comparison"] is False   # 3 and 6 both have a real, smaller companion
        assert r["max_coordinate_mismatch_deg"] < 1e-9
        assert r["n_both_valid"] > 0
        assert r["exact_match"] is True
        assert r["max_abs_diff"] < 1e-6
        assert r["rms_diff"] < 1e-6
    # the nearest-pixel reference (kept only for context) must show a REAL, nonzero coordinate offset -
    # otherwise this test would not be distinguishing the two methods at all.
    ref = bcc["nearest_pixel_reference"]
    assert ref["max_coordinate_offset_deg"] > 0


def test_bc_exact_coordinate_consistency_flags_degenerate_power_of_two_factors():
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0, interp_factor_b=2, interp_factor_c=4))
    bcc = built["bc_exact_coordinate_consistency"]
    assert bcc["b"]["degenerate_self_comparison"] is True
    assert bcc["c"]["degenerate_self_comparison"] is True
    # a degenerate (self) comparison is still trivially exact - it just proves nothing beyond determinism
    assert bcc["b"]["exact_match"] is True and bcc["c"]["exact_match"] is True


def test_build_all_products_exposes_bc_exact_coordinate_consistency():
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    bcc = built["bc_exact_coordinate_consistency"]
    assert bcc is not None
    assert set(bcc.keys()) == {"b", "c", "nearest_pixel_reference"}


# ---------------------------------------------------------------- planned lattice (campaign grid_row/grid_col)
# A real 400-point campaign (20x20, 30x30 deg, dec -33) failed mosaic_board_buildable at ANY spacing: its
# plan is a tangent-plane lattice, and science_engine's linear (dRA cos dec0, dDec) projection spreads one
# planned column over >3 cells there, so clustering projected positions cannot recover rows/columns.

def _wide_campaign(tmp_path, n=20, width_deg=30.0, center_ra_h=4.94, center_dec=-33.4489, edit_rows=None):
    """A REDUCE session dir + campaign dir pair like the real one: points on a gnomonic lattice, the plan's
    grid_row/grid_col in mosaic.csv, the manifest's source_campaign_root pointing at the campaign."""
    import csv
    import dataclasses
    import json
    from science_engine.simulation import SyntheticPointSpec
    spacing = width_deg / (n - 1)
    a0, d0 = np.radians(center_ra_h * 15.0), np.radians(center_dec)
    rows, specs = [], []
    for r in range(n):
        for c in range(n):
            xi, eta = np.radians((c - (n - 1) / 2) * spacing), np.radians((r - (n - 1) / 2) * spacing)
            rho = np.hypot(xi, eta)
            cc = np.arctan(rho)
            dec = np.arcsin(np.cos(cc) * np.sin(d0) + (eta * np.sin(cc) * np.cos(d0) / rho if rho else 0.0))
            ra = a0 + np.arctan2(xi * np.sin(cc), rho * np.cos(d0) * np.cos(cc) - eta * np.sin(d0) * np.sin(cc))
            idx = r * n + c + 1
            ra_h, dec_d = (np.degrees(ra) % 360.0) / 15.0, float(np.degrees(dec))
            rows.append({"point_number": idx, "grid_row": r, "grid_col": c,
                         "target_ra_hours": f"{ra_h:.6f}", "target_dec_degrees": f"{dec_d:.6f}"})
            specs.append(SyntheticPointSpec(point_index=idx, ra_hours=float(f"{ra_h:.6f}"),
                                            dec_degrees=float(f"{dec_d:.6f}"), calibration_level="UNCALIBRATED"))
    if edit_rows:
        edit_rows(rows)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    with (campaign / "mosaic.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (campaign / "grid_metadata.json").write_text(json.dumps({"grid": {
        "rows": n, "columns": n, "nominal_spacing_deg": spacing, "center_ra_hours": center_ra_h,
        "center_dec_degrees": center_dec, "projection": "tangent-plane"}}))
    reduce_dir = tmp_path / "reduce"
    reduce_dir.mkdir()
    (reduce_dir / "manifest.json").write_text(json.dumps({"source_campaign_root": str(campaign)}))
    si = build_synthetic_science_input(specs, noise_sigma=0.02)
    return dataclasses.replace(si, reduce_session_dir=str(reduce_dir)), spacing


def test_wide_campaign_cannot_be_clustered_but_builds_from_its_plan(tmp_path):
    from science_web_bridge import load_planned_lattice
    si, spacing = _wide_campaign(tmp_path)
    with pytest.raises(MosaicShapeError, match="do not form a clean rectangular lattice"):
        build_mosaic_grid(si, spacing)                      # the old path, even at the plan's own spacing
    planned = load_planned_lattice(si)
    spatial = auto_spatial_params(si, cfg_with(), planned)
    assert spatial["mosaic_spacing_source"] == "campaign_plan"
    assert spatial["mosaic_spacing_deg"] == pytest.approx(spacing)
    board, point_cell, n_rows, n_cols = build_mosaic_grid(si, spatial["mosaic_spacing_deg"], planned)
    assert (n_rows, n_cols) == (20, 20)
    assert point_cell == {i: ((i - 1) // 20, (i - 1) % 20) for i in range(1, 401)}   # every point, its own cell
    assert board.center_ra_deg == pytest.approx(4.94 * 15.0)
    assert board.center_dec_deg == pytest.approx(-33.4489)
    # coordinates are never touched: each point keeps its own position, which is its planned target
    for p in si.points:
        ra_deg, dec_deg = planned.target_by_point[p.point_index]
        assert (p.ra_deg, p.dec_degrees) == pytest.approx((ra_deg, dec_deg), abs=1e-9)


def test_wide_campaign_a_cells_follow_the_shared_projection_and_the_raster_covers_them(tmp_path):
    """A, B and C in ONE projection: A's cells sit where the lattice really lies (deformed quads), the B/C
    base raster is widened to contain every cell, and every point stays inside B/C support."""
    from science_web_bridge import (lattice_geometry, load_planned_lattice, mosaic_geometry_summary,
                                    raster_base_grid)
    si, spacing = _wide_campaign(tmp_path)
    planned = load_planned_lattice(si)
    spatial = auto_spatial_params(si, cfg_with(), planned)
    board, point_cell, n_rows, n_cols = build_mosaic_grid(si, spatial["mosaic_spacing_deg"], planned)
    nodes, corners = lattice_geometry(board, n_rows, n_cols, planned)
    base = raster_base_grid(board, corners)
    assert corners.shape == (21, 21, 2)
    assert base.width_deg / 2 >= np.abs(corners[..., 0]).max() and base.height_deg / 2 >= np.abs(corners[..., 1]).max()
    assert base.nx > board.nx                       # the board's own rectangle would cut the far corners off
    assert base.center_ra_deg == board.center_ra_deg and base.center_dec_deg == board.center_dec_deg
    # orientation: x grows with column along every row, y with row along every column
    assert np.all(np.diff(nodes[..., 0], axis=1) > 0) and np.all(np.diff(nodes[..., 1], axis=0) > 0)
    geo = mosaic_geometry_summary(si, base, nodes, point_cell, planned, spatial["support_radius_deg"], (2, 3, 6))
    assert geo["lattice_source"] == "campaign_plan"
    assert geo["point_to_planned_target_max_deg"] < 1e-5
    assert geo["point_to_a_cell_node_max_deg"] < 1e-5        # A draws each point's cell where the point is
    assert geo["regular_board_offset_max_deg"] > spacing     # ...which a rectangular board would not
    assert all(not cov["uncovered_points"] for cov in geo["raster_coverage"].values())
    # a support radius smaller than the corners' distance to the raster: those points are named, not dropped
    tight = mosaic_geometry_summary(si, base, nodes, point_cell, planned, 1e-3, (2,))
    assert tight["raster_coverage"]["2"]["uncovered_points"]


def test_regular_board_keeps_its_square_cells_and_extent():
    from science_web_bridge import lattice_geometry, raster_base_grid
    si = mosaic_input(n=6, spacing=1.0)
    board, _, n_rows, n_cols = build_mosaic_grid(si, 1.0)
    _, corners = lattice_geometry(board, n_rows, n_cols, None)
    assert np.allclose(corners[0, :, 0], np.linspace(-3, 3, 7)) and np.allclose(corners[:, 0, 1], np.linspace(-3, 3, 7))
    base = raster_base_grid(board, corners)
    assert (base.nx, base.ny, base.width_deg, base.height_deg) == (board.nx, board.ny, board.width_deg, board.height_deg)


def test_tile_grid_reproduces_the_full_grids_pixel_centres():
    from science_web_bridge import _tile_grid
    grid = ScienceGrid(frame="icrs", center_ra_deg=74.1, center_dec_deg=-33.45, width_deg=37.9, height_deg=33.2,
                       pixel_scale_deg=1.579 / 6, nx=144, ny=126)
    ra, dec = pixel_centers_deg(grid)
    for (y0, y1) in ((0, 7), (7, 64), (64, 126)):
        for (x0, x1) in ((0, 13), (13, 100), (100, 144)):
            bra, bdec = pixel_centers_deg(_tile_grid(grid, y0, y1, x0, x1))
            assert np.allclose(bra, ra[y0:y1, x0:x1], atol=1e-9, rtol=0), (y0, x0)
            assert np.allclose(bdec, dec[y0:y1, x0:x1], atol=1e-9, rtol=0), (y0, x0)


def test_tiled_map_equals_the_single_cube_map(monkeypatch):
    """The row-band builder must return the SAME map (values, sigma, coverage, n_pointings, used points) and
    the same quality verdict as one full cube - bands only bound memory."""
    import science_web_bridge as swb
    from science_engine.cube import build_cube
    from science_engine.grid import build_beam_model
    from science_engine.integration import integrated_map
    from science_engine.quality import assess_science_quality
    si = mosaic_input(n=6, spacing=1.0)
    cfg = cfg_with()
    spatial = auto_spatial_params(si, cfg)
    sc = swb._kernel_science_config(cfg, spatial["smoothing_fwhm_deg"], spatial["support_radius_deg"] / spatial["smoothing_fwhm_deg"], "test")
    beam = build_beam_model(sc)
    board, _, _, _ = build_mosaic_grid(si, 1.0)
    grid = build_fine_grid(board, 3)
    cube = build_cube(si, grid, beam, sc)
    ref = integrated_map(cube, sc)
    ref_quality = assess_science_quality(si, cube, sc, ref)
    real_plan = swb.tile_plan
    monkeypatch.setattr(swb, "tile_plan", lambda *a, **k: {**real_plan(*a, **k), "tile_shape": [4, 5]})
    banded, info, qcube = swb.integrated_map_in_tiles(si, grid, beam, sc, keep_quality_inputs=True)
    assert info["tiles"]["n_tiles"] == 5 * 4                     # 18x18 px in 4x5 tiles
    for name in ("value", "uncertainty", "spectral_coverage", "weight_sum"):
        # a tile's pixel centres equal the full grid's to ~1e-13 deg (float rounding): differences stay at ~1e-14
        # of the field's own scale - a per-pixel relative test would only measure how close a value is to zero
        scale = np.nanmax(np.abs(getattr(ref, name)))
        assert np.allclose(getattr(banded, name), getattr(ref, name), rtol=0, atol=1e-12 * scale, equal_nan=True), name
    assert np.array_equal(banded.valid, ref.valid) and np.array_equal(banded.n_pointings, ref.n_pointings)
    assert info["used_point_indices"] == cube.build_info["used_point_indices"]
    quality = assess_science_quality(si, qcube, sc, banded)
    assert quality.state == ref_quality.state and quality.reasons == ref_quality.reasons
    assert quality.metrics.keys() == ref_quality.metrics.keys()
    for key, ref_value in ref_quality.metrics.items():
        if isinstance(ref_value, float):
            assert quality.metrics[key] == pytest.approx(ref_value, rel=1e-12), key
        else:
            assert quality.metrics[key] == ref_value, key


def test_tile_plan_fits_the_real_c_raster_in_the_unchanged_budget():
    """The real 400-point session's C raster: one cube needs ~6.6 GB; bands keep the estimate inside 3 GB."""
    from science_web_bridge import tile_plan
    grid = ScienceGrid(frame="icrs", center_ra_deg=74.1, center_dec_deg=-33.45, width_deg=37.9, height_deg=33.2,
                       pixel_scale_deg=1.579 / 6, nx=144, ny=126)
    plan = tile_plan(grid, 8192, 400, keep_quality_inputs=False)
    assert plan["single_cube_estimate_bytes"] > 3 * 1024 ** 3
    assert plan["ok"] and plan["n_tiles"] > 1
    assert plan["estimated_peak_bytes"] <= plan["budget_bytes"] <= 3 * 1024 ** 3


def test_board_json_carries_each_cells_quadrilateral_and_the_shared_extent(tmp_path):
    si = mosaic_input(n=6, spacing=1.0)
    built = build_all_products(si, cfg_with(beam_fwhm_deg=20.0))
    board = board_json_payload(built)
    s = built["board_grid"].pixel_scale_deg       # the auto (nearest-neighbour) pitch of this synthetic board
    assert board["extent_deg"]["half_w"] == pytest.approx(3 * s) and board["extent_deg"]["half_h"] == pytest.approx(3 * s)
    cell = next(c for c in board["cells"] if (c["row"], c["col"]) == (0, 0))
    assert cell["corners_xy"] == [pytest.approx([-3 * s, -3 * s]), pytest.approx([-2 * s, -3 * s]),
                                  pytest.approx([-2 * s, -2 * s]), pytest.approx([-3 * s, -2 * s])]


def test_plan_that_does_not_match_the_points_is_refused(tmp_path):
    from science_web_bridge import load_planned_lattice

    def shift_one_target(rows):
        rows[0]["target_dec_degrees"] = f"{float(rows[0]['target_dec_degrees']) + 1.0:.6f}"
    si, spacing = _wide_campaign(tmp_path, edit_rows=shift_one_target)
    planned = load_planned_lattice(si)
    with pytest.raises(MosaicShapeError, match="from its planned target"):
        build_mosaic_grid(si, spacing, planned)


def test_plan_with_swapped_columns_is_refused(tmp_path):
    from science_web_bridge import load_planned_lattice

    def swap(rows):
        rows[0]["grid_col"], rows[1]["grid_col"] = rows[1]["grid_col"], rows[0]["grid_col"]
    si, spacing = _wide_campaign(tmp_path, edit_rows=swap)
    with pytest.raises(MosaicShapeError, match="not monotonic"):
        build_mosaic_grid(si, spacing, load_planned_lattice(si))


def test_explicit_spacing_that_contradicts_the_plan_is_refused(tmp_path):
    from science_web_bridge import load_planned_lattice
    si, spacing = _wide_campaign(tmp_path)
    with pytest.raises(MosaicShapeError, match="contradicts the campaign plan"):
        build_mosaic_grid(si, spacing * 0.99, load_planned_lattice(si))


def test_no_campaign_plan_falls_back_to_measured_positions():
    from science_web_bridge import load_planned_lattice
    si = mosaic_input(n=6, spacing=1.0)
    assert load_planned_lattice(si) is None
    assert auto_spatial_params(si, cfg_with())["mosaic_spacing_source"] == "median_nearest_neighbor"


# ---------------------------------------------------------------- tiled B/C vs ONE full cube: hostile inputs
# The tiled build must reproduce the frozen single-cube build_cube()+integrated_map() whatever the per-channel
# structure: masks that differ per point AND per channel, sigmas that vary per channel and per point, non-finite
# values, LSRK-shifted axes (resampling), points sitting on the support limit, and tile edges cutting through
# the field at awkward places.
#
# Tolerance: a tile's pixel centres equal the full grid's to float rounding (~1e-13 deg, see _tile_grid). That
# moves each beam weight by a relative ~1e-13, so values/sigmas agree to ~1e-14 of the field's own scale
# (measured on the real 400-point session: 1.2e-14). 1e-11 of the scale leaves margin without hiding any real
# difference: a genuinely different weighting (e.g. integrate-first) differs by ~1e-1 of the scale. Everything
# discrete - valid, n_pointings, used/excluded points, quality state and reasons - must be identical.

def _hostile_input(n=6, spacing=1.0, n_channels=192, seed=7):
    import dataclasses
    from science_engine.simulation import SyntheticPointSpec
    rng = np.random.default_rng(seed)
    specs = []
    base = rectangular_grid_specs(12.0, -30.0, n, n, spacing_deg=spacing, calibration_level="UNCALIBRATED")
    for s in base:
        specs.append(SyntheticPointSpec(point_index=s.point_index, ra_hours=s.ra_hours, dec_degrees=s.dec_degrees,
                                        calibration_level="UNCALIBRATED",
                                        velocity_offset_m_s=float(rng.uniform(-5000, 5000)),   # resampled axes
                                        noise_sigma=float(rng.uniform(0.01, 0.05))))
    si = build_synthetic_science_input(specs, n_channels=n_channels, noise_sigma=0.02,
                                       line_amplitude_fn=lambda ra, dec: 0.3 + 0.1 * np.sin(ra) * np.cos(dec))
    points = []
    for p in si.points:
        mask = np.array(p.mask, copy=True)
        mask[rng.random(n_channels) < 0.06] = 4                       # MaskFlag.RFI on different channels per point
        unc = np.asarray(p.uncertainty, dtype=float) * rng.uniform(0.4, 2.0, n_channels)   # per-channel sigma
        val = np.array(p.relative_intensity, dtype=float, copy=True)
        val[rng.integers(0, n_channels, 2)] = np.nan                  # non-finite values must never contribute
        points.append(dataclasses.replace(p, mask=mask, uncertainty=unc, relative_intensity=val))
    return dataclasses.replace(si, points=points)


@pytest.mark.parametrize("tile_shape", [[1, 1], [3, 5], [7, 4], [18, 2]])
def test_tiled_map_equals_one_full_cube_with_hostile_masks_sigmas_and_tile_edges(monkeypatch, tile_shape):
    import science_web_bridge as swb
    from science_engine.cube import build_cube
    from science_engine.grid import build_beam_model
    from science_engine.integration import integrated_map
    from science_engine.quality import assess_science_quality
    si = _hostile_input()
    cfg = cfg_with(velocity_window_min_m_s=-60_000.0, velocity_window_max_m_s=60_000.0)
    spatial = auto_spatial_params(si, cfg)
    sc = swb._kernel_science_config(cfg, spatial["smoothing_fwhm_deg"],
                                    spatial["support_radius_deg"] / spatial["smoothing_fwhm_deg"], "test")
    beam = build_beam_model(sc)
    board, _, _, _ = build_mosaic_grid(si, spatial["mosaic_spacing_deg"])
    # widen the raster beyond the points so its rim lies OUTSIDE every point's support (invalid pixels) and some
    # pixels sit right at the support limit
    from science_engine.models import ScienceGrid
    wide = ScienceGrid(frame="icrs", center_ra_deg=board.center_ra_deg, center_dec_deg=board.center_dec_deg,
                       width_deg=board.width_deg + 6 * spatial["support_radius_deg"], height_deg=board.height_deg + 6 * spatial["support_radius_deg"],
                       pixel_scale_deg=board.pixel_scale_deg / 3, nx=board.nx * 3 + 18, ny=board.ny * 3 + 18)
    cube = build_cube(si, wide, beam, sc)
    ref = integrated_map(cube, sc)
    ref_quality = assess_science_quality(si, cube, sc, ref)
    assert ref.valid.any() and not ref.valid.all(), "the rim must really be outside support"
    real_plan = swb.tile_plan
    monkeypatch.setattr(swb, "tile_plan", lambda *a, **k: {**real_plan(*a, **k), "tile_shape": tile_shape})
    tiled, info, qcube = swb.integrated_map_in_tiles(si, wide, beam, sc, keep_quality_inputs=True)
    for name in ("value", "uncertainty", "spectral_coverage", "weight_sum"):
        a, b = getattr(tiled, name), getattr(ref, name)
        assert np.array_equal(np.isnan(a), np.isnan(b)), name
        scale = np.nanmax(np.abs(b))
        assert np.nanmax(np.abs(a - b)) <= 1e-11 * scale, (name, np.nanmax(np.abs(a - b)) / scale)
    assert np.array_equal(tiled.valid, ref.valid) and np.array_equal(tiled.n_pointings, ref.n_pointings)
    assert info["used_point_indices"] == cube.build_info["used_point_indices"]
    assert sorted((e["point_index"], e["reason"]) for e in info["excluded"]) == \
        sorted((e["point_index"], e["reason"]) for e in cube.build_info["excluded"])
    quality = assess_science_quality(si, qcube, sc, tiled)
    assert (quality.state, quality.reasons, quality.limitations) == (ref_quality.state, ref_quality.reasons, ref_quality.limitations)
    for key, ref_value in ref_quality.metrics.items():
        if isinstance(ref_value, float):
            assert quality.metrics[key] == pytest.approx(ref_value, rel=1e-12, abs=1e-15), key
        else:
            assert quality.metrics[key] == ref_value, key


def test_tiled_build_names_a_point_outside_every_tile_exactly_like_the_full_cube(monkeypatch):
    """A point whose support reaches no pixel of the grid is OUTSIDE_BEAM_SUPPORT_OF_GRID once - not once per tile,
    and never while another tile used it."""
    import dataclasses
    import science_web_bridge as swb
    from science_engine.cube import build_cube
    from science_engine.grid import build_beam_model
    si = mosaic_input(n=6, spacing=1.0)
    far = dataclasses.replace(si.points[0], point_index=999, ra_deg=si.points[0].ra_deg + 40.0,
                              ra_hours=si.points[0].ra_hours + 40.0 / 15.0)
    si = dataclasses.replace(si, points=list(si.points) + [far])
    cfg = cfg_with()
    spatial = auto_spatial_params(si, cfg)
    sc = swb._kernel_science_config(cfg, spatial["smoothing_fwhm_deg"], 1.25, "test")
    beam = build_beam_model(sc)
    board, _, _, _ = build_mosaic_grid(dataclasses.replace(si, points=si.points[:-1]), 1.0)
    grid = build_fine_grid(board, 3)
    ref_info = build_cube(si, grid, beam, sc).build_info
    real_plan = swb.tile_plan
    monkeypatch.setattr(swb, "tile_plan", lambda *a, **k: {**real_plan(*a, **k), "tile_shape": [5, 5]})
    _, info, _ = swb.integrated_map_in_tiles(si, grid, beam, sc)
    assert [e for e in info["excluded"] if e["point_index"] == 999] == [{"point_index": 999, "reason": "OUTSIDE_BEAM_SUPPORT_OF_GRID"}]
    assert info["used_point_indices"] == ref_info["used_point_indices"]


def test_exports_have_one_size_short_captions_and_notes_file(tmp_path):
    """Every single-panel export (A, B, C and the three diagnostics) has the SAME pixel size; the technical
    detail lives in NOTES.md (noise / consistency / leave-one-out / method / hatching), not in tiny captions."""
    from PIL import Image
    si = mosaic_input(n=6, spacing=1.0)
    cfg = cfg_with(beam_fwhm_deg=20.0)
    built = build_all_products(si, cfg)
    exports, _ = render_all_maps(built, cfg, si.campaign_id, si.reduce_session_id, tmp_path)
    sizes = {n: Image.open(tmp_path / f"{n}.png").size for n in
             ("map_a_no_interp", "map_b_smooth", "map_c_heavy", "map_snr", "map_coverage_density", "map_coverage")}
    assert len(set(sizes.values())) == 1, sizes
    notes = (tmp_path / "NOTES.md").read_text()
    assert exports["notes"] == ["NOTES.md"]
    for section in ("## Noise check", "## B/C consistency", "## Method", "Hatched / dimmed B/C pixels", "## SNR map"):
        assert section in notes, section
