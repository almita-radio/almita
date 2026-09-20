"""BEAM CONTRACT (sections 15-20, 24, 118): exact formula values at more
than one point (catches FWHM-vs-radius-vs-sigma confusion), invalid-
input rejection, and a measured (not aesthetic) beam-cutoff sensitivity
study.
"""
import numpy as np
import pytest

from science_engine.beam import beam_weight
from science_engine.config import ScienceConfig
from science_engine.cube import build_cube
from science_engine.grid import build_beam_model, build_grid
from science_engine.models import BeamModel
from science_engine.simulation import SyntheticPointSpec, build_synthetic_science_input, rectangular_grid_specs


def test_beam_weight_exact_values_at_zero_half_and_full_fwhm():
    """theta=0 -> 1, theta=FWHM/2 -> 0.5, theta=FWHM -> 0.0625 - this
    third point specifically catches FWHM/radius/sigma confusion: a
    sigma-based (not FWHM-based) Gaussian would give a very different
    value at theta=FWHM."""
    beam = BeamModel(fwhm_deg=10.0, cutoff_n_fwhm=10.0)
    theta = np.array([0.0, 5.0, 10.0])
    w = beam_weight(theta, beam)
    assert np.isclose(w[0], 1.0, atol=1e-9)
    assert np.isclose(w[1], 0.5, atol=1e-9)
    assert np.isclose(w[2], 0.0625, atol=1e-6)


# ---------------------------------------------------------------- section 118: invalid beam inputs

@pytest.mark.parametrize("fwhm", [0.0, -5.0, np.nan, np.inf, -np.inf, 180.0001, 200.0, 1e-12, 5e-4])
def test_invalid_or_extreme_fwhm_fails_clearly_at_both_layers(fwhm):
    from science_engine.config import ScienceConfig
    with pytest.raises(ValueError, match="fwhm"):
        BeamModel(fwhm_deg=fwhm)
    with pytest.raises(ValueError, match="fwhm"):
        ScienceConfig(beam_fwhm_deg=fwhm)


@pytest.mark.parametrize("fwhm", [1e-3, 0.5, 1.5, 20.0, 180.0])
def test_valid_fwhm_range_edges_are_accepted(fwhm):
    beam = BeamModel(fwhm_deg=fwhm)
    w = beam_weight(np.array([0.0, fwhm / 2]), beam)
    assert np.isclose(w[0], 1.0) and np.isclose(w[1], 0.5)


def test_nan_velocity_window_bounds_are_rejected():
    """Section 120: `nan >= x` and `x >= nan` are both False in
    IEEE754 - a bare >= check would silently let a NaN bound through."""
    from science_engine.config import ScienceConfig
    with pytest.raises(ValueError):
        ScienceConfig(velocity_window_min_m_s=np.nan, velocity_window_max_m_s=100.0)
    with pytest.raises(ValueError):
        ScienceConfig(velocity_window_min_m_s=-100.0, velocity_window_max_m_s=np.nan)
    with pytest.raises(ValueError):
        ScienceConfig(velocity_window_min_m_s=np.inf, velocity_window_max_m_s=np.inf)


def test_smallest_accepted_fwhm_does_not_produce_nan_or_inf_weight():
    beam = BeamModel(fwhm_deg=1e-3, cutoff_n_fwhm=3.0)
    w = beam_weight(np.array([0.0, 1e-3, 1.0]), beam)
    assert np.all(np.isfinite(w))
    assert w[0] == 1.0
    assert w[2] == 0.0  # 1 degree is far beyond a 0.001 degree beam's cutoff


# ---------------------------------------------------------------- section 19: cutoff sensitivity study

CENTER_RA_H, CENTER_DEC = 12.0, -30.0


def _cube_for_cutoff(cutoff_n_fwhm):
    specs = rectangular_grid_specs(CENTER_RA_H, CENTER_DEC, 4, 4, spacing_deg=1.0)
    si = build_synthetic_science_input(specs, n_channels=32, line_amplitude_fn=lambda ra, dec: 1.0,
                                       noise_sigma=0.01, rng=np.random.default_rng(99))
    config = ScienceConfig(beam_fwhm_deg=1.2, pixels_per_beam=3.0, extent_margin_beams=1.0,
                           beam_cutoff_n_fwhm=cutoff_n_fwhm)
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    cube = build_cube(si, grid, beam, config)
    return cube, grid


def test_beam_cutoff_sensitivity_study(capsys):
    """Measured, not aesthetic (section 19): report coverage/edge/
    integrated-flux-proxy across cutoff choices; default (3.0x) is kept
    because it is already effectively converged relative to 2.0x, with
    no evidence a change would matter."""
    results = {}
    for cutoff in (1.0, 1.5, 2.0, 3.0):
        cube, grid = _cube_for_cutoff(cutoff)
        coverage = float(np.mean(np.any(cube.valid, axis=0)))
        flux_proxy = float(np.nansum(np.where(cube.valid[16], cube.relative_intensity[16], 0.0)))
        results[cutoff] = (coverage, flux_proxy)
        print(f"cutoff={cutoff:.1f}xFWHM  coverage={coverage:.4f}  flux_proxy(ch16)={flux_proxy:.4f}")

    # Coverage must be monotonically non-decreasing as the cutoff widens (a wider beam footprint can
    # only add contributing pixels, never remove them).
    coverages = [results[c][0] for c in (1.0, 1.5, 2.0, 3.0)]
    assert all(coverages[i] <= coverages[i + 1] + 1e-9 for i in range(len(coverages) - 1))
    # 2.0x -> 3.0x flux proxy change should be small (near-converged) - if this test ever shows a large
    # jump, that IS evidence a default change might be warranted, per section 19's own rule.
    flux_2, flux_3 = results[2.0][1], results[3.0][1]
    relative_change = abs(flux_3 - flux_2) / max(abs(flux_2), 1e-9)
    print(f"2.0x->3.0x flux_proxy relative change: {relative_change:.4f}")
    assert relative_change < 0.05
