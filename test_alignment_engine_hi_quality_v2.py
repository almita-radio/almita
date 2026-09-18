"""Fase 24: quality V2 regression matrix - spatial coverage, null model,
bootstrap, edge check, and the two real false-GOOD modes found in the
previous pass's Monte Carlo."""
import numpy as np
import pytest
from astropy.coordinates import EarthLocation, SkyCoord

from alignment import offset_coordinates, shifted_positions
from alignment_engine.fitting import fit_raster
from alignment_engine.hi.quality_v2 import (
    bootstrap_stability,
    edge_proximity_check,
    evaluate_quality_v2,
    null_model_improvement,
    spatial_sampling_quality,
)
from alignment_engine.scan_planner import build_raster
from alignment_engine.targets.hi_reference import SyntheticHIReferenceProvider
import astropy.units as u
from astropy.time import Time

LOCATION = EarthLocation(lat=-33.4331 * u.deg, lon=289.3336 * u.deg, height=550 * u.m)
OBSTIME = Time("2026-09-18T03:00:00")
SPAN_DEG = 12.0
provider = SyntheticHIReferenceProvider("data/hi_sky_catalog_2000pts.csv")
CENTER, _ = provider.choose_target(LOCATION, OBSTIME, min_altitude_deg=-90.0, beam_fwhm_deg=20.0)
TEMPLATE = provider.template_for(CENTER, 20.0)
POINTS = build_raster(SPAN_DEG, 3.0)
POSITIONS = offset_coordinates(CENTER, [p.east_deg for p in POINTS], [p.north_deg for p in POINTS])


def _clean_values(true_east=0.5, true_north=-0.3):
    shifted = shifted_positions(POSITIONS, CENTER, true_east, true_north)
    return list(np.asarray(TEMPLATE(shifted)))


def test_spatial_coverage_accepts_well_distributed_valid_points():
    values = _clean_values()
    result = spatial_sampling_quality(POINTS, values, SPAN_DEG)
    assert result.sufficient


def test_spatial_coverage_rejects_corner_clustered_points_even_with_enough_count():
    values = [None] * len(POINTS)
    # Fill only points in one corner (small east/north) - same total valid
    # count as many "good" cases, but degenerate geometry.
    corner_indices = [i for i, p in enumerate(POINTS) if p.east_deg <= 0 and p.north_deg <= 0]
    clean = _clean_values()
    for i in corner_indices:
        values[i] = clean[i]
    assert len(corner_indices) >= 8  # sanity: enough "valid_count" to pass that check alone
    result = spatial_sampling_quality(POINTS, values, SPAN_DEG)
    assert result.sufficient is False
    assert any("bounding box" in r or "rows" in r or "columns" in r for r in result.reasons)


def test_spatial_coverage_rejects_too_few_valid_points():
    values = _clean_values()
    values = [v if i < 5 else None for i, v in enumerate(values)]
    result = spatial_sampling_quality(POINTS, values, SPAN_DEG)
    assert result.sufficient is False


def test_null_model_improvement_high_for_a_strong_clean_fit():
    values = _clean_values()
    fit = fit_raster(POSITIONS, values, CENTER, TEMPLATE, SPAN_DEG)
    improvement = null_model_improvement(fit)
    assert improvement > 0.8


def test_null_model_improvement_low_for_pure_noise():
    rng = np.random.default_rng(1)
    values = list(rng.normal(0, 1, len(POINTS)))
    fit = fit_raster(POSITIONS, values, CENTER, TEMPLATE, SPAN_DEG)
    improvement = null_model_improvement(fit)
    assert improvement < 0.5


def test_bootstrap_stable_for_clean_data():
    values = _clean_values()
    result = bootstrap_stability(POSITIONS, values, CENTER, TEMPLATE, SPAN_DEG, n_iterations=10)
    assert result.stable


def test_bootstrap_unstable_with_very_few_points():
    values = [None] * len(POINTS)
    clean = _clean_values()
    values[0], values[1], values[2] = clean[0], clean[1], clean[2]
    result = bootstrap_stability(POSITIONS, values, CENTER, TEMPLATE, SPAN_DEG, n_iterations=10)
    assert result.stable is False


def test_edge_proximity_flags_offset_near_boundary():
    result = edge_proximity_check(SPAN_DEG / 2 - 0.2, 0.0, SPAN_DEG)
    assert result.near_edge is True


def test_edge_proximity_passes_for_central_offset():
    result = edge_proximity_check(0.5, -0.3, SPAN_DEG)
    assert result.near_edge is False


def test_evaluate_quality_v2_good_for_clean_case():
    values = _clean_values()
    fit = fit_raster(POSITIONS, values, CENTER, TEMPLATE, SPAN_DEG)
    result = evaluate_quality_v2(POINTS, values, POSITIONS, CENTER, TEMPLATE, fit, SPAN_DEG,
                                  run_bootstrap=True, bootstrap_iterations=10)
    assert result.verdict == "GOOD"


def test_evaluate_quality_v2_bad_for_flat_reference():
    def flat_template(coords):
        return np.full(len(SkyCoord(coords).reshape((-1,))), 5.0)
    values = list(flat_template(POSITIONS))
    fit = fit_raster(POSITIONS, values, CENTER, flat_template, SPAN_DEG)
    result = evaluate_quality_v2(POINTS, values, POSITIONS, CENTER, flat_template, fit, SPAN_DEG,
                                  run_bootstrap=False)
    assert result.verdict != "GOOD"


def test_evaluate_quality_v2_rejects_extreme_missing_data_even_if_v1_would_say_good():
    rng = np.random.default_rng(7)
    clean = _clean_values(true_east=1.0, true_north=1.0)
    values = list(clean)
    missing_count = int(round(0.8 * len(values)))
    for i in rng.choice(len(values), size=missing_count, replace=False):
        values[i] = None
    fit = fit_raster(POSITIONS, values, CENTER, TEMPLATE, SPAN_DEG)
    result = evaluate_quality_v2(POINTS, values, POSITIONS, CENTER, TEMPLATE, fit, SPAN_DEG,
                                  run_bootstrap=True, bootstrap_iterations=10)
    # Either the coverage check or the bootstrap catches this - both are
    # legitimate reasons at 80% missing, and the assertion is deliberately
    # about the outcome (never GOOD), not which specific check fires.
    assert result.verdict != "GOOD"
