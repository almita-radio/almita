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

from science_web_bridge import (MapConfig, MosaicShapeError, _reconcile_used_point_sets, auto_spatial_params,
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
