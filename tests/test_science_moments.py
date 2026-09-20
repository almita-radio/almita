"""Moment-like product correctness (section 95) and pitfalls (44-45)."""
import numpy as np

from science_engine.config import ScienceConfig
from science_engine.cube import build_cube
from science_engine.grid import build_beam_model, build_grid
from science_engine.moments import moment1_like, moment2_like
from science_engine.simulation import SyntheticPointSpec, build_synthetic_science_input

CENTER_RA_H, CENTER_DEC = 12.0, -30.0


def _cube_with_line(centroid_m_s=30_000.0, fwhm_m_s=20_000.0, amplitude=1.0, noise_sigma=1e-6):
    spec = SyntheticPointSpec(point_index=1, ra_hours=CENTER_RA_H, dec_degrees=CENTER_DEC)
    si = build_synthetic_science_input([spec], n_channels=512, velocity_min_m_s=-300_000.0,
                                       velocity_max_m_s=300_000.0, line_amplitude_fn=lambda ra, dec: amplitude,
                                       line_center_m_s_fn=lambda ra, dec: centroid_m_s, line_fwhm_m_s=fwhm_m_s,
                                       noise_sigma=noise_sigma, rng=np.random.default_rng(11))
    config = ScienceConfig(beam_fwhm_deg=2.0, pixels_per_beam=1.0, extent_margin_beams=2.0,
                           velocity_window_min_m_s=-300_000.0, velocity_window_max_m_s=300_000.0)
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)
    return cube, config, grid


def test_moment1_recovers_known_centroid():
    centroid = 30_000.0
    cube, config, grid = _cube_with_line(centroid_m_s=centroid)
    result = moment1_like(cube, config)
    center_y, center_x = grid.ny // 2, grid.nx // 2
    assert result.valid[center_y, center_x]
    assert np.isclose(result.value[center_y, center_x], centroid, atol=2_000.0)


def test_moment2_recovers_known_dispersion():
    """A Gaussian with FWHM=F has sigma = F / (2*sqrt(2 ln 2))."""
    fwhm = 20_000.0
    expected_sigma = fwhm / (2 * np.sqrt(2 * np.log(2)))
    cube, config, grid = _cube_with_line(fwhm_m_s=fwhm)
    result = moment2_like(cube, config)
    center_y, center_x = grid.ny // 2, grid.nx // 2
    assert result.valid[center_y, center_x]
    assert np.isclose(result.value[center_y, center_x], expected_sigma, rtol=0.1)


def test_moment1_invalid_on_pure_noise():
    """Section 44: no signal -> no meaningful centroid, not garbage."""
    cube, config, grid = _cube_with_line(amplitude=0.0, noise_sigma=0.05)
    result = moment1_like(cube, config)
    center_y, center_x = grid.ny // 2, grid.nx // 2
    assert not result.valid[center_y, center_x]
    assert np.isnan(result.value[center_y, center_x])


def test_moment2_invalid_when_moment1_invalid():
    cube, config, grid = _cube_with_line(amplitude=0.0, noise_sigma=0.05)
    result = moment2_like(cube, config)
    center_y, center_x = grid.ny // 2, grid.nx // 2
    assert not result.valid[center_y, center_x]
