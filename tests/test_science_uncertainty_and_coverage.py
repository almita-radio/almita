"""UNCERTAINTY PROPAGATION (sections 29-37, 99-104): closed-form worked
examples, a Monte Carlo empirical-scatter-vs-reported-uncertainty check
at the full cube level, and the beam+sigma interaction case.
"""
import numpy as np

from science_engine.config import ScienceConfig
from science_engine.cube import build_cube
from science_engine.gridding import GriddingAccumulator
from science_engine.grid import build_beam_model, build_grid
from science_engine.simulation import SyntheticPointSpec, build_synthetic_science_input

CENTER_RA_H, CENTER_DEC = 12.0, -30.0


# ---------------------------------------------------------------- section 101: equal-weight overlap

def test_two_equal_sigma_equal_beam_measurements_improve_uncertainty_by_sqrt2():
    acc = GriddingAccumulator(shape=(1, 1))
    acc.add_point(np.array([[1.0]]), 10.0, 2.0, True)
    acc.add_point(np.array([[1.0]]), 10.0, 2.0, True)
    value, uncertainty, weight_sum, n = acc.finalize()
    assert np.isclose(value[0, 0], 10.0)
    assert np.isclose(uncertainty[0, 0], 2.0 / np.sqrt(2), rtol=1e-9)


# ---------------------------------------------------------------- section 102: beam x sigma interaction

def test_beam_and_sigma_weighting_interact_multiplicatively_not_independently():
    """point A: closer (higher beam weight) but noisier; point B:
    farther (lower beam weight) but precise. Manually compute the
    expected combined result with the SAME formula, independently typed
    out here, and compare - not reusing GriddingAccumulator's own
    internals to generate the expectation (section 99)."""
    beam_a, sigma_a, value_a = 0.9, 5.0, 100.0    # closer, noisy
    beam_b, sigma_b, value_b = 0.3, 0.5, 20.0     # farther, precise

    w_a, w_b = beam_a / sigma_a ** 2, beam_b / sigma_b ** 2
    expected_value = (w_a * value_a + w_b * value_b) / (w_a + w_b)
    expected_variance = (w_a ** 2 * sigma_a ** 2 + w_b ** 2 * sigma_b ** 2) / (w_a + w_b) ** 2

    acc = GriddingAccumulator(shape=(1, 1))
    acc.add_point(np.array([[beam_a]]), value_a, sigma_a, True)
    acc.add_point(np.array([[beam_b]]), value_b, sigma_b, True)
    value, uncertainty, weight_sum, n = acc.finalize()

    assert np.isclose(value[0, 0], expected_value, rtol=1e-9)
    assert np.isclose(uncertainty[0, 0], np.sqrt(expected_variance), rtol=1e-9)
    # sanity: point B (precise) should have dominated despite its smaller beam weight, since sigma
    # enters the weight quadratically (1/sigma^2) while beam enters only linearly.
    assert abs(value[0, 0] - value_b) < abs(value[0, 0] - value_a)


# ---------------------------------------------------------------- section 99: fully independent manual gridding check

def test_independent_manual_two_point_grid_matches_production_code():
    """A tiny 1x1 pixel, 1-channel case computed by hand (not by calling
    ANY science_engine function to generate the expectation) and
    compared against the real production pipeline end to end."""
    specs = [SyntheticPointSpec(point_index=1, ra_hours=CENTER_RA_H, dec_degrees=CENTER_DEC)]
    si = build_synthetic_science_input(specs, n_channels=4, line_amplitude_fn=lambda ra, dec: 0.0, noise_sigma=1e-6,
                                       rng=np.random.default_rng(5))
    si.points[0].relative_intensity[:] = [3.0, 3.0, 3.0, 3.0]
    si.points[0].uncertainty[:] = [1.0, 1.0, 1.0, 1.0]

    config = ScienceConfig(beam_fwhm_deg=2.0, pixel_scale_deg=2.0, extent_margin_beams=0.0)
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)

    # manual expectation: a single point exactly at the grid center -> theta=0 -> beam weight=1.0
    # exactly; w = 1.0/1.0^2 = 1.0; weighted mean of one contributor is just its own value.
    center_y, center_x = grid.ny // 2, grid.nx // 2
    assert np.isclose(cube.relative_intensity[0, center_y, center_x], 3.0)
    assert np.isclose(cube.uncertainty[0, center_y, center_x], 1.0)


# ---------------------------------------------------------------- section 36: Monte Carlo coverage at cube level

def test_uncertainty_monte_carlo_coverage_at_cube_level(capsys):
    """Many noise realizations of the SAME synthetic campaign -> compare
    empirical cross-realization scatter to the reported per-voxel
    uncertainty, and +-1/+-2 sigma coverage. Not demanding textbook
    exactness (section 36) - detecting gross miscalibration only."""
    specs = rectangular_grid_specs_local()
    n_realizations = 40
    amplitude, fwhm_m_s = 0.4, 20_000.0
    velocity_min, velocity_max, n_channels = -200_000.0, 200_000.0, 16
    rng_master = np.random.default_rng(2026)

    # read the channel closest to the line center (v=0), and the TRUE expected value there is the
    # Gaussian evaluated at that channel's actual velocity - not the peak amplitude itself, since a
    # coarse 16-channel axis rarely samples exactly v=0 (this is the bug the first draft of this test had).
    velocity_axis = np.linspace(velocity_min, velocity_max, n_channels)
    channel = int(np.argmin(np.abs(velocity_axis)))
    truth = amplitude * np.exp(-4 * np.log(2) * velocity_axis[channel] ** 2 / fwhm_m_s ** 2)

    center_values = []
    reported_uncertainties = []
    for i in range(n_realizations):
        si = build_synthetic_science_input(specs, n_channels=n_channels, velocity_min_m_s=velocity_min,
                                           velocity_max_m_s=velocity_max, line_amplitude_fn=lambda ra, dec: amplitude,
                                           line_fwhm_m_s=fwhm_m_s, noise_sigma=0.05,
                                           rng=np.random.default_rng(rng_master.integers(1 << 30)))
        config = ScienceConfig(beam_fwhm_deg=1.5, pixels_per_beam=2.0, extent_margin_beams=0.5)
        beam = build_beam_model(config)
        grid = build_grid(si, beam, config)
        cube = build_cube(si, grid, beam, config)
        cy, cx = grid.ny // 2, grid.nx // 2
        center_values.append(cube.relative_intensity[channel, cy, cx])
        reported_uncertainties.append(cube.uncertainty[channel, cy, cx])

    center_values = np.array(center_values)
    reported_uncertainties = np.array(reported_uncertainties)
    empirical_scatter = np.std(center_values)
    median_reported = np.median(reported_uncertainties)
    ratio = empirical_scatter / median_reported

    within_1sigma = np.mean(np.abs(center_values - truth) <= reported_uncertainties)
    within_2sigma = np.mean(np.abs(center_values - truth) <= 2 * reported_uncertainties)

    print(f"empirical_scatter={empirical_scatter:.4f}  median_reported_uncertainty={median_reported:.4f}  "
         f"ratio={ratio:.3f}  within_1sigma={within_1sigma:.2f}  within_2sigma={within_2sigma:.2f}")

    assert 0.3 < ratio < 3.0          # not wildly under/over-confident (section 36's own tolerance framing)
    assert within_1sigma > 0.35       # gross-miscalibration detector, not textbook 68%
    assert within_2sigma > 0.75


def rectangular_grid_specs_local():
    from science_engine.simulation import rectangular_grid_specs
    return rectangular_grid_specs(CENTER_RA_H, CENTER_DEC, 3, 3, spacing_deg=1.0)
