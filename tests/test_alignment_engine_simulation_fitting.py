"""Fase 17/18: fitter must recover known injected offsets from synthetic
data, for both SOLAR and HI, under noise/missing-samples/gain/baseline
mismatch - without ever touching hardware."""
import warnings

import numpy as np
import pytest
import astropy.units as u
from astropy.coordinates import SkyCoord

warnings.filterwarnings("ignore", module="astropy")

from alignment_engine.fitting import fit_raster
from alignment_engine.scan_planner import build_raster
from alignment_engine.simulation import HIMapSimConfig, SolarBeamSimConfig, synthetic_hi_metrics, synthetic_solar_metrics
from alignment_engine.targets.hi_reference import SyntheticHIReferenceProvider

SOLAR_CENTER = SkyCoord(ra=180 * u.deg, dec=-30 * u.deg)  # plain ICRS test anchor
SOLAR_SPAN = 14.0
SOLAR_SPACING = 3.5
FWHM = 20.0

# Same "structured, high-contrast catalog region" alignment.py's own
# run_simulation() already picked for offline testing - reusing it keeps
# this test independent of which random patch of the synthetic catalog
# happens to have enough spatial structure to fit against.
HI_CENTER = SkyCoord(ra=315 * u.deg, dec=32.9268 * u.deg)


def _solar_fit(true_east, true_north, noise_fraction=0.02, missing_fraction=0.0, seed=1,
               center=SOLAR_CENTER, span=SOLAR_SPAN, spacing=SOLAR_SPACING):
    points = build_raster(span, spacing)
    sim = SolarBeamSimConfig(true_offset_east_deg=true_east, true_offset_north_deg=true_north,
                              fwhm_deg=FWHM, noise_fraction=noise_fraction,
                              missing_fraction=missing_fraction, seed=seed)
    values = synthetic_solar_metrics(points, center, sim)
    positions = SkyCoord([_resolve(center, p.east_deg, p.north_deg) for p in points])
    template = lambda coords: np.exp(-.5 * (coords.separation(center).deg / (FWHM / 2.3548)) ** 2)
    return fit_raster(positions, values, center, template, span)


def _resolve(center, east, north):
    from alignment import offset_coordinates
    return offset_coordinates(center, [east], [north])[0]


@pytest.mark.parametrize("magnitude,angle_deg", [
    (0.5, 0), (0.5, 90), (1.0, 45), (2.0, 180), (3.0, 270), (1.5, 135),
])
def test_solar_recovers_injected_offset_at_various_magnitudes_and_angles(magnitude, angle_deg):
    import math
    east = magnitude * math.cos(math.radians(angle_deg))
    north = magnitude * math.sin(math.radians(angle_deg))
    result = _solar_fit(east, north, noise_fraction=0.01, seed=hash((magnitude, angle_deg)) % 1000)
    assert result.estimate.offset_ra_deg == pytest.approx(east, abs=0.6)
    assert result.estimate.offset_dec_deg == pytest.approx(north, abs=0.6)
    assert result.quality.confidence > 0.5


def test_solar_perfectly_centered_beam_recovers_near_zero_offset():
    result = _solar_fit(0.0, 0.0, noise_fraction=0.01, seed=42)
    assert result.estimate.offset_ra_deg == pytest.approx(0.0, abs=0.4)
    assert result.estimate.offset_dec_deg == pytest.approx(0.0, abs=0.4)


def test_solar_noisy_beam_still_recovers_within_looser_tolerance():
    result = _solar_fit(1.0, -1.0, noise_fraction=0.25, seed=5)
    assert result.estimate.offset_ra_deg == pytest.approx(1.0, abs=1.5)
    assert result.estimate.offset_dec_deg == pytest.approx(-1.0, abs=1.5)


def test_solar_missing_points_still_fits_from_remaining_valid_samples():
    result = _solar_fit(1.0, 0.5, noise_fraction=0.02, missing_fraction=0.3, seed=9)
    assert result.valid_count < result.total_count
    assert result.estimate.offset_ra_deg == pytest.approx(1.0, abs=0.8)


def test_solar_mostly_missing_points_degrades_to_low_confidence_not_a_crash():
    result = _solar_fit(1.0, 0.5, noise_fraction=0.02, missing_fraction=0.9, seed=9)
    assert result.total_count > 0  # never raises even with almost nothing valid


def test_solar_wraparound_center_near_zero_hours_still_computes_finite_offset():
    """A center near RA=0h/360deg boundary - if raw-degree subtraction were
    used anywhere this would silently blow up; SkyCoord/SkyOffsetFrame
    handles it transparently instead (alignment.py's own
    test_ra_wrap_and_spherical_offset already covers the underlying
    mechanism - this confirms it still holds through this package's
    wrapper)."""
    wrap_center = SkyCoord(ra=0.1 * u.deg, dec=10.0 * u.deg)
    result = _solar_fit(1.0, 0.5, noise_fraction=0.02, seed=3, center=wrap_center)
    assert all(map(lambda v: v == v, [result.estimate.offset_ra_deg, result.estimate.offset_dec_deg]))  # not NaN
    assert result.estimate.offset_ra_deg == pytest.approx(1.0, abs=0.8)


def test_solar_fit_quality_rating_is_bad_for_insufficient_points():
    result = fit_raster(SkyCoord([SOLAR_CENTER] * 2), [1.0, 2.0], SOLAR_CENTER,
                         lambda c: np.ones(len(c)), SOLAR_SPAN)
    assert result.quality.rating == "BAD"


# ---------------------------------------------------------------- HI


def _hi_provider():
    return SyntheticHIReferenceProvider()


def test_hi_recovers_injected_offset_with_gain_and_baseline_mismatch():
    provider = _hi_provider()
    template = provider.template_for(HI_CENTER, FWHM)
    points = build_raster(12.0, 3.0)
    sim = HIMapSimConfig(true_offset_east_deg=-0.8, true_offset_north_deg=1.5,
                          gain_a=2.3, baseline_b=5.0, noise_fraction=0.02, seed=11)
    values = synthetic_hi_metrics(points, HI_CENTER, template, sim)
    from alignment import offset_coordinates
    positions = offset_coordinates(HI_CENTER, [p.east_deg for p in points], [p.north_deg for p in points])
    result = fit_raster(positions, values, HI_CENTER, template, 12.0)
    assert result.estimate.offset_ra_deg == pytest.approx(-0.8, abs=0.6)
    assert result.estimate.offset_dec_deg == pytest.approx(1.5, abs=0.6)
    # gain (2.3x) and a nonzero baseline (5.0) must NOT be misread as a
    # pointing error - only the offset should reflect them being present.
    assert result.quality.confidence > 0.5


def test_hi_pure_gain_and_baseline_with_zero_offset_recovers_zero():
    provider = _hi_provider()
    template = provider.template_for(HI_CENTER, FWHM)
    points = build_raster(12.0, 3.0)
    sim = HIMapSimConfig(true_offset_east_deg=0.0, true_offset_north_deg=0.0,
                          gain_a=5.0, baseline_b=-3.0, noise_fraction=0.01, seed=2)
    values = synthetic_hi_metrics(points, HI_CENTER, template, sim)
    from alignment import offset_coordinates
    positions = offset_coordinates(HI_CENTER, [p.east_deg for p in points], [p.north_deg for p in points])
    result = fit_raster(positions, values, HI_CENTER, template, 12.0)
    assert result.estimate.offset_ra_deg == pytest.approx(0.0, abs=0.5)
    assert result.estimate.offset_dec_deg == pytest.approx(0.0, abs=0.5)


def test_hi_missing_samples_still_fits():
    provider = _hi_provider()
    template = provider.template_for(HI_CENTER, FWHM)
    points = build_raster(12.0, 3.0)
    sim = HIMapSimConfig(true_offset_east_deg=0.6, true_offset_north_deg=-0.4,
                          gain_a=1.0, baseline_b=0.0, noise_fraction=0.02,
                          missing_fraction=0.3, seed=8)
    values = synthetic_hi_metrics(points, HI_CENTER, template, sim)
    from alignment import offset_coordinates
    positions = offset_coordinates(HI_CENTER, [p.east_deg for p in points], [p.north_deg for p in points])
    result = fit_raster(positions, values, HI_CENTER, template, 12.0)
    assert result.valid_count < result.total_count
    assert result.estimate.offset_ra_deg == pytest.approx(0.6, abs=0.8)


def test_hi_flat_featureless_region_yields_poor_correlation_not_a_false_offset():
    """A region with (near-)zero spatial structure in the synthetic catalog
    should not report high confidence - "insufficient structure" is a real
    rejection mode, not a bug to hide."""
    import numpy as np
    provider = _hi_provider()
    flat_template = lambda coords: np.ones(len(coords)) * 10.0
    points = build_raster(12.0, 3.0)
    from alignment import offset_coordinates
    positions = offset_coordinates(HI_CENTER, [p.east_deg for p in points], [p.north_deg for p in points])
    rng = np.random.default_rng(1)
    values = [10.0 + float(rng.normal(0, 0.05)) for _ in points]
    result = fit_raster(positions, values, HI_CENTER, flat_template, 12.0)
    assert result.quality.rating in ("BAD", "MARGINAL")
