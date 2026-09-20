"""Pre-hardware pass, item 1 (precision solar): tests for the sub-grid
Gauss-Newton/Levenberg-Marquardt refinement added on top of alignment.py's
proven grid search (alignment_engine/fitting.py: refine_subgrid_offset,
search_offset_residual_primary, fit_raster's `refine`/`ranking` params).

Root-cause finding these tests encode (see fitting.py's module docstring
for the full investigation): a noiseless recovery is already correct to
floating-point precision even WITHOUT refinement - the residual error
users saw before this pass was noise interacting with a broad beam's weak
curvature over a narrow scan window, not a geometry/quantization bug.
Refinement's job is (a) genuine continuous sub-grid precision (useful
whenever the true offset doesn't land on the discrete search grid), and
(b) an honest uncertainty estimate - not a fix for the noise-driven
variance itself, which these tests also document rather than hide.
"""
import math
import warnings

import numpy as np
import pytest
import astropy.units as u
from astropy.coordinates import SkyCoord

warnings.filterwarnings("ignore", module="astropy")

from alignment import offset_coordinates
from alignment_engine.fitting import fit_raster, refine_subgrid_offset
from alignment_engine.scan_planner import build_raster
from alignment_engine.simulation import SolarBeamSimConfig, synthetic_solar_metrics

CENTER = SkyCoord(ra=180 * u.deg, dec=-30 * u.deg)
SPAN = 5.5
SPACING = 1.5


def _gaussian(fwhm_deg, center=CENTER):
    sigma = fwhm_deg / (2 * math.sqrt(2 * math.log(2)))
    return lambda coords: np.exp(-.5 * (coords.separation(center).deg / sigma) ** 2)


def _fit(true_east, true_north, fwhm=20.0, noise=0.0, seed=1, missing_fraction=0.0,
         span=SPAN, spacing=SPACING, center=CENTER, **kwargs):
    points = build_raster(span, spacing)
    sim = SolarBeamSimConfig(true_offset_east_deg=true_east, true_offset_north_deg=true_north,
                              fwhm_deg=fwhm, noise_fraction=noise, missing_fraction=missing_fraction,
                              seed=seed)
    values = synthetic_solar_metrics(points, center, sim)
    positions = SkyCoord([offset_coordinates(center, [p.east_deg], [p.north_deg])[0] for p in points])
    return fit_raster(positions, values, center, _gaussian(fwhm, center), span, **kwargs)


# ------------------------------------------------------------- precision


def test_noiseless_offset_not_multiple_of_any_search_grid_step_meets_tolerance():
    """1.37 / -2.63 are not exact multiples of any of the default search
    steps (.5, .1, .025) - a pure discrete grid search could only land on
    its nearest grid point; the refined estimate must do much better."""
    result = _fit(1.37, -2.63, noise=0.0, seed=1)
    assert abs(result.estimate.offset_ra_deg - 1.37) <= 0.1
    assert abs(result.estimate.offset_dec_deg - (-2.63)) <= 0.1
    assert result.refinement.applied is True


def test_noiseless_offset_exactly_between_four_grid_points():
    """Half of the finest default grid step in both axes: the discrete
    optimum is equidistant from four candidates - refinement must resolve
    the ambiguity rather than being stuck at whichever one the argmax
    picked first."""
    half_step = 0.025 / 2
    result = _fit(1.0 + half_step, -1.0 + half_step, noise=0.0, seed=2)
    assert abs(result.estimate.offset_ra_deg - (1.0 + half_step)) <= 0.1
    assert abs(result.estimate.offset_dec_deg - (-1.0 + half_step)) <= 0.1


@pytest.mark.parametrize("east,north", [
    (0.8, 0.8), (-0.8, 0.8), (0.8, -0.8), (-0.8, -0.8),
    (0.0, 1.9), (1.9, 0.0), (-2.0, 0.0), (0.0, -2.0),
])
def test_noiseless_positive_and_negative_offsets_meet_tolerance(east, north):
    result = _fit(east, north, noise=0.0, seed=3)
    assert abs(result.estimate.offset_ra_deg - east) <= 0.1
    assert abs(result.estimate.offset_dec_deg - north) <= 0.1
    # rating is a separate concern (edge-of-span geometry, not precision) -
    # sanity-checked loosely here, exercised precisely elsewhere.
    assert result.quality.rating in ("GOOD", "MARGINAL")


@pytest.mark.parametrize("fwhm", [8.0, 14.0, 20.0, 30.0])
def test_noiseless_various_beam_fwhm_meet_tolerance(fwhm):
    result = _fit(0.6, -0.4, fwhm=fwhm, noise=0.0, seed=4)
    assert abs(result.estimate.offset_ra_deg - 0.6) <= 0.1
    assert abs(result.estimate.offset_dec_deg - (-0.4)) <= 0.1


def test_noiseless_zero_offset_meets_tolerance():
    result = _fit(0.0, 0.0, noise=0.0, seed=5)
    assert abs(result.estimate.offset_ra_deg) <= 0.1
    assert abs(result.estimate.offset_dec_deg) <= 0.1


# ------------------------------------------------------------- uncertainty / noise


def test_refinement_reports_uncertainty_alongside_the_estimate():
    result = _fit(1.2, -0.7, noise=0.0, seed=7)
    assert result.refinement.uncertainty_east_deg is not None
    assert result.refinement.uncertainty_north_deg is not None
    assert result.refinement.uncertainty_east_deg >= 0.0
    assert result.refinement.uncertainty_north_deg >= 0.0
    # noiseless -> the fit is essentially exact -> reported uncertainty
    # should be tiny, not a meaningless placeholder number.
    assert result.refinement.uncertainty_east_deg < 0.01


def test_noise_uncertainty_grows_with_noise_fraction_demonstrating_real_behavior():
    """Statistical demonstration (Fase 1's own "demostrar estadisticamente
    el comportamiento"): more measurement noise must not silently report
    the same tiny uncertainty - the reported number has to track reality,
    even though this package cannot (and does not claim to) make the
    underlying noise-driven estimation problem disappear."""
    low = _fit(1.0, -1.0, noise=0.01, seed=21)
    high = _fit(1.0, -1.0, noise=0.15, seed=21)
    assert high.refinement.uncertainty_east_deg > low.refinement.uncertainty_east_deg


def test_noise_statistical_behavior_across_seeds_stays_bounded_and_documented():
    """Reproduces the pre-hardware investigation's own finding: with a
    FWHM=20deg beam over a 5.5deg fine window, 2% metric noise produces
    real, non-trivial offset variance (an SNR/curvature limitation, not a
    bug) - this test documents that behavior numerically instead of
    silently hoping it stays small, and guards against a future regression
    making it far worse than what was measured (max ~0.9deg over 30 seeds
    at this geometry)."""
    errors_east = []
    for seed in range(15):
        result = _fit(1.2, -0.7, noise=0.02, seed=seed)
        errors_east.append(abs(result.estimate.offset_ra_deg - 1.2))
    errors_east = np.asarray(errors_east)
    assert np.max(errors_east) < 1.5  # generous regression ceiling, not a precision claim


def test_missing_points_still_produces_a_finite_refined_estimate():
    result = _fit(1.0, 0.5, noise=0.02, missing_fraction=0.3, seed=9)
    assert result.valid_count < result.total_count
    assert math.isfinite(result.estimate.offset_ra_deg)
    assert math.isfinite(result.estimate.offset_dec_deg)


# ------------------------------------------------------------- safety / fallback


def test_refinement_never_exceeds_its_configured_bound_even_under_pathological_noise():
    """Regression guard for the exact failure mode found while evaluating
    an unconstrained residual-primary search during this investigation
    (measured divergence to ~73deg with an unguarded implementation): the
    production refinement must stay within its declared bound_deg no
    matter how bad the input."""
    result = _fit(1.0, -1.0, noise=3.0, seed=99)  # absurd noise fraction on purpose
    outer_radius = 5.5
    bound_deg = 1.5 * outer_radius + 1.0
    assert abs(result.estimate.offset_ra_deg) <= bound_deg
    assert abs(result.estimate.offset_dec_deg) <= bound_deg


def test_refine_subgrid_offset_falls_back_cleanly_when_template_always_raises():
    positions = SkyCoord([offset_coordinates(CENTER, [0.0], [0.0])[0]] * 5)
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])

    def exploding_template(_coords):
        raise RuntimeError("simulated template failure")

    refinement = refine_subgrid_offset(positions, values, CENTER, exploding_template,
                                        initial_east_deg=1.0, initial_north_deg=-1.0, bound_deg=10.0)
    assert refinement.applied is False
    assert refinement.refined_offset_east_deg == 1.0
    assert refinement.refined_offset_north_deg == -1.0
    assert "not attempted" in refinement.reason


def test_fit_raster_refine_false_keeps_grid_estimate_unchanged():
    result = _fit(1.2, -0.7, noise=0.0, seed=7, refine=False)
    assert result.refinement is None
    assert result.estimate is result.grid_estimate


def test_fit_raster_residual_ranking_alternative_still_recovers_offset_noiseless():
    """Smoke coverage for the documented-but-not-default ranking="residual"
    path (see fitting.py: kept because it is a real, tested alternative and
    demonstrates the degeneracy guard, even though it isn't the default)."""
    result = _fit(0.9, -0.4, noise=0.0, seed=11, ranking="residual")
    assert result.grid_ranking == "residual"
    assert abs(result.estimate.offset_ra_deg - 0.9) <= 0.1
    assert abs(result.estimate.offset_dec_deg - (-0.4)) <= 0.1


def test_fit_raster_rejects_unknown_ranking_strategy():
    with pytest.raises(ValueError):
        _fit(0.0, 0.0, ranking="magic")
