"""Integrated map correctness (sections 38-40, 94)."""
import numpy as np
import pytest

from science_engine.config import ScienceConfig
from science_engine.cube import build_cube
from science_engine.grid import build_beam_model, build_grid
from science_engine.integration import channel_map, integrated_map
from science_engine.simulation import SyntheticPointSpec, build_synthetic_science_input

CENTER_RA_H, CENTER_DEC = 12.0, -30.0


def _single_point_cube(amplitude=1.0, fwhm_m_s=20_000.0, noise_sigma=1e-6, n_channels=256,
                       velocity_min=-200_000.0, velocity_max=200_000.0, masked_channel_slice=None):
    spec = SyntheticPointSpec(point_index=1, ra_hours=CENTER_RA_H, dec_degrees=CENTER_DEC,
                              masked_channel_slice=masked_channel_slice)
    si = build_synthetic_science_input([spec], n_channels=n_channels, velocity_min_m_s=velocity_min,
                                       velocity_max_m_s=velocity_max, line_amplitude_fn=lambda ra, dec: amplitude,
                                       line_fwhm_m_s=fwhm_m_s, noise_sigma=noise_sigma,
                                       rng=np.random.default_rng(42))
    config = ScienceConfig(beam_fwhm_deg=2.0, pixels_per_beam=1.0, extent_margin_beams=2.0,
                           velocity_window_min_m_s=velocity_min, velocity_window_max_m_s=velocity_max)
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)
    return cube, config, grid


def test_integrated_intensity_matches_analytic_gaussian_integral():
    """Section 94: known analytic line -> computed integral within
    tolerance. Integral of A*exp(-4ln2 x^2/FWHM^2) dx over all x is
    A * FWHM * sqrt(pi/(4 ln 2))."""
    amplitude, fwhm = 2.0, 20_000.0
    cube, config, grid = _single_point_cube(amplitude=amplitude, fwhm_m_s=fwhm, noise_sigma=1e-6,
                                            velocity_min=-300_000.0, velocity_max=300_000.0)
    expected_integral = amplitude * fwhm * np.sqrt(np.pi / (4 * np.log(2)))
    result = integrated_map(cube, config)
    center_y, center_x = grid.ny // 2, grid.nx // 2
    measured = result.value[center_y, center_x]
    assert np.isclose(measured, expected_integral, rtol=0.02)


def test_masked_window_reduces_below_threshold_becomes_invalid():
    """Section 40: coverage below min_spectral_coverage_fraction ->
    invalid, never extrapolated."""
    cube, config, grid = _single_point_cube(amplitude=1.0, noise_sigma=1e-6,
                                            masked_channel_slice=slice(0, 200))  # mask most of 256 channels
    config = ScienceConfig(**{**config.to_dict(), "min_spectral_coverage_fraction": 0.5})
    result = integrated_map(cube, config)
    center_y, center_x = grid.ny // 2, grid.nx // 2
    assert not result.valid[center_y, center_x]
    assert np.isnan(result.value[center_y, center_x])


def test_masked_window_above_threshold_still_valid_and_reports_coverage():
    cube, config, grid = _single_point_cube(amplitude=1.0, noise_sigma=1e-6, masked_channel_slice=slice(0, 20))
    result = integrated_map(cube, config)
    center_y, center_x = grid.ny // 2, grid.nx // 2
    assert result.valid[center_y, center_x]
    coverage = np.array(result.metadata["spectral_coverage_fraction"])
    assert coverage[center_y, center_x] < 1.0
    assert coverage[center_y, center_x] > 0.9


def test_window_selecting_zero_channels_raises():
    cube, config, grid = _single_point_cube()
    bad_config = ScienceConfig(**{**config.to_dict(), "velocity_window_min_m_s": 1e9,
                                 "velocity_window_max_m_s": 2e9})
    with pytest.raises(ValueError):
        integrated_map(cube, bad_config)


def test_channel_map_selects_only_requested_window():
    cube, config, grid = _single_point_cube(amplitude=1.0, noise_sigma=1e-6)
    result = channel_map(cube, velocity_center_m_s=0.0, velocity_width_m_s=5_000.0)
    center_y, center_x = grid.ny // 2, grid.nx // 2
    assert result.valid[center_y, center_x]
    assert result.value[center_y, center_x] > 0.5  # near the line peak
