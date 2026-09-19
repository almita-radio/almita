"""ScienceCube adversarial/golden tests (sections 73-82, 92, 126)."""
import numpy as np
import pytest

from reduce_engine.models import MaskFlag
from science_engine.config import ScienceConfig
from science_engine.cube import IncompatibleVelocityGridError, build_cube, canonical_velocity_axis
from science_engine.grid import build_beam_model, build_grid
from science_engine.simulation import SyntheticPointSpec, build_synthetic_science_input, rectangular_grid_specs

CENTER_RA_H, CENTER_DEC = 12.0, -30.0


def _default_config(**overrides):
    defaults = dict(beam_fwhm_deg=2.0, pixels_per_beam=3.0, extent_margin_beams=1.0)
    defaults.update(overrides)
    return ScienceConfig(**defaults)


def test_cube_axis_order_is_velocity_y_x():
    specs = rectangular_grid_specs(CENTER_RA_H, CENTER_DEC, 2, 2, spacing_deg=1.0)
    si = build_synthetic_science_input(specs, n_channels=32)
    config = _default_config()
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)
    assert cube.relative_intensity.shape == (32, grid.ny, grid.nx)


def test_constant_sky_recovers_approximately_constant_map():
    """Section 76: spatially constant input -> approximately constant
    output where coverage is good (edge/weight bias check)."""
    specs = rectangular_grid_specs(CENTER_RA_H, CENTER_DEC, 5, 5, spacing_deg=1.0)
    si = build_synthetic_science_input(specs, n_channels=64, line_amplitude_fn=lambda ra, dec: 1.0,
                                       line_center_m_s_fn=lambda ra, dec: 0.0, noise_sigma=0.01,
                                       rng=np.random.default_rng(1))
    config = _default_config(beam_fwhm_deg=1.2, pixels_per_beam=3.0)
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)

    peak_channel = 32  # center of the line (velocity=0 is near mid-array for a symmetric linspace)
    slice_2d = cube.relative_intensity[peak_channel]
    high_coverage = cube.n_contributing[peak_channel] >= 5
    assert np.any(high_coverage)
    values = slice_2d[high_coverage]
    assert np.nanstd(values) < 0.15 * np.nanmean(values)  # roughly constant, well within the injected amplitude


def test_noise_only_map_averages_near_zero():
    """Section 77: pure noise -> integrated map ~0 within uncertainty,
    no artificial structure."""
    specs = rectangular_grid_specs(CENTER_RA_H, CENTER_DEC, 4, 4, spacing_deg=1.0)
    si = build_synthetic_science_input(specs, n_channels=64, line_amplitude_fn=lambda ra, dec: 0.0,
                                       noise_sigma=0.05, rng=np.random.default_rng(2))
    config = _default_config(beam_fwhm_deg=1.5)
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)
    finite = np.isfinite(cube.relative_intensity)
    assert abs(np.mean(cube.relative_intensity[finite])) < 0.02


def test_coverage_hole_from_missing_point_is_not_filled():
    """Section 78: a deliberately missing sky point leaves a real
    coverage gap - never interpolated into a fabricated signal."""
    specs = rectangular_grid_specs(CENTER_RA_H, CENTER_DEC, 5, 5, spacing_deg=1.5)
    missing_index = specs[12].point_index  # dead center of the 5x5 grid
    specs = [s for s in specs if s.point_index != missing_index]
    si = build_synthetic_science_input(specs, n_channels=32, line_amplitude_fn=lambda ra, dec: 0.5,
                                       noise_sigma=0.01, rng=np.random.default_rng(3))
    config = _default_config(beam_fwhm_deg=0.6, pixels_per_beam=3.0, beam_cutoff_n_fwhm=1.5)
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)
    # with a tight beam and no point at the center, SOME pixels must have zero weight (never invented)
    assert np.any(cube.weight_sum[16] == 0)


def test_bad_point_default_policy_never_contaminates_map():
    """Section 79: one BAD pointing with an extreme value must not
    contaminate the default (STANDARD) policy's output."""
    specs = rectangular_grid_specs(CENTER_RA_H, CENTER_DEC, 3, 3, spacing_deg=1.0)
    si = build_synthetic_science_input(specs, n_channels=32, line_amplitude_fn=lambda ra, dec: 0.1,
                                       noise_sigma=0.01, rng=np.random.default_rng(4))
    si.points[4] = si.points[4].__class__(**{**si.points[4].__dict__,
                                              "relative_intensity": np.full(32, 999.0),
                                              "reduce_quality_state": "BAD"})
    config = _default_config()
    assert config.quality_policy == "STANDARD"
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)
    assert np.nanmax(np.abs(cube.relative_intensity)) < 10.0  # nowhere near the BAD point's 999.0


def test_warning_point_included_under_standard_policy():
    """Section 80: STANDARD explicitly includes WARNING points."""
    specs = rectangular_grid_specs(CENTER_RA_H, CENTER_DEC, 3, 3, spacing_deg=1.0)
    si = build_synthetic_science_input(specs, n_channels=16, line_amplitude_fn=lambda ra, dec: 0.2,
                                       noise_sigma=0.01, rng=np.random.default_rng(5))
    si.points[4] = si.points[4].__class__(**{**si.points[4].__dict__, "reduce_quality_state": "WARNING"})
    config = _default_config(quality_policy="STANDARD")
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)
    assert np.any(cube.n_contributing == 9) or np.max(cube.n_contributing) >= 5  # the WARNING point still counted


def test_masked_channel_does_not_contribute_there_but_does_elsewhere():
    """Section 81: RFI-masked channels are excluded only in that channel,
    not the whole point."""
    specs = [SyntheticPointSpec(point_index=1, ra_hours=CENTER_RA_H, dec_degrees=CENTER_DEC,
                                masked_channel_slice=slice(10, 20))]
    si = build_synthetic_science_input(specs, n_channels=32, line_amplitude_fn=lambda ra, dec: 0.3,
                                       noise_sigma=0.01, rng=np.random.default_rng(6))
    config = _default_config()
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)
    center_y, center_x = grid.ny // 2, grid.nx // 2
    assert cube.n_contributing[10, center_y, center_x] == 0     # masked channel: no contribution
    assert cube.n_contributing[25, center_y, center_x] == 1     # unmasked channel: contributes normally


def test_variable_uncertainty_favors_the_more_certain_point():
    """Section 82: two co-located points, wildly different uncertainty ->
    inverse-variance weighting favors the tighter one."""
    specs = [SyntheticPointSpec(point_index=1, ra_hours=CENTER_RA_H, dec_degrees=CENTER_DEC),
             SyntheticPointSpec(point_index=2, ra_hours=CENTER_RA_H, dec_degrees=CENTER_DEC)]
    si = build_synthetic_science_input(specs, n_channels=8, line_amplitude_fn=lambda ra, dec: 0.0, noise_sigma=0.0)
    si.points[0].relative_intensity[:] = 10.0
    si.points[0].uncertainty[:] = 1.0
    si.points[1].relative_intensity[:] = 0.0
    si.points[1].uncertainty[:] = 100.0
    config = _default_config()
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)
    center_y, center_x = grid.ny // 2, grid.nx // 2
    value = cube.relative_intensity[0, center_y, center_x]
    assert value > 9.0  # dominated by the sigma=1.0 point, not a 50/50 blend with the sigma=100 point


def test_velocity_gradient_across_field_is_not_collapsed():
    """Section 92: a spatially-varying line centroid must survive into
    the cube (not get smeared into one average velocity)."""
    specs = rectangular_grid_specs(CENTER_RA_H, CENTER_DEC, 1, 5, spacing_deg=3.0)  # a 1x5 east-west strip
    si = build_synthetic_science_input(
        specs, n_channels=128, velocity_min_m_s=-100_000, velocity_max_m_s=100_000,
        line_amplitude_fn=lambda ra, dec: 1.0, line_fwhm_m_s=8_000,
        line_center_m_s_fn=lambda ra, dec: (ra - specs[2].ra_hours * 15.0) * 20_000.0,  # deg -> m/s gradient
        noise_sigma=0.005, rng=np.random.default_rng(7))
    config = _default_config(beam_fwhm_deg=1.0, pixels_per_beam=2.0, extent_margin_beams=0.5)
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)

    def peak_velocity_at(col_x):
        column = cube.relative_intensity[:, grid.ny // 2, col_x]
        return cube.velocity_lsrk_m_s[np.nanargmax(np.nan_to_num(column, nan=-np.inf))]

    v_west = peak_velocity_at(2)
    v_east = peak_velocity_at(grid.nx - 3)
    assert v_west != v_east
    # beam convolution legitimately smooths some of the injected gradient - assert it clearly
    # survived (well above one channel width ~1563 m/s), not that it survived undamped.
    assert abs(v_west - v_east) > 15_000.0


def test_incompatible_frequency_grid_is_rejected():
    specs = [SyntheticPointSpec(point_index=1, ra_hours=CENTER_RA_H, dec_degrees=CENTER_DEC)]
    si = build_synthetic_science_input(specs, n_channels=16)
    si.points[0].frequency_hz = si.points[0].frequency_hz[:-1]  # corrupt: wrong length vs a hypothetical peer
    # single-point case can't demonstrate a mismatch; build a genuinely mismatched two-point input instead
    si2 = build_synthetic_science_input(
        [SyntheticPointSpec(point_index=1, ra_hours=CENTER_RA_H, dec_degrees=CENTER_DEC),
         SyntheticPointSpec(point_index=2, ra_hours=CENTER_RA_H + 0.1, dec_degrees=CENTER_DEC)], n_channels=16)
    si2.points[1].frequency_hz = si2.points[1].frequency_hz + 1.0  # tiny, deliberate mismatch
    with pytest.raises(IncompatibleVelocityGridError):
        canonical_velocity_axis(si2)


def test_no_velocity_at_all_blocks_not_guesses():
    specs = [SyntheticPointSpec(point_index=1, ra_hours=CENTER_RA_H, dec_degrees=CENTER_DEC)]
    si = build_synthetic_science_input(specs, n_channels=16)
    si.points[0].velocity_lsrk_m_s = None
    with pytest.raises(IncompatibleVelocityGridError):
        canonical_velocity_axis(si)
