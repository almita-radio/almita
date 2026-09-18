"""Fase 9-11, 18: the repeatability comparator - CONSISTENT / MARGINAL /
INCONSISTENT for same-target/region results, DIFFERENT_REGION (not a
failure) for cross-target comparisons, UNKNOWN (never a fabricated
verdict) when uncertainty is missing."""
from unittest.mock import patch

import astropy.units as u
import pytest
from astropy.coordinates import SkyCoord

from alignment_engine.hi.repeatability import AlignmentResultSummary, compare_alignment_results


def _summary(session_id, target_label, ra_deg, dec_deg, east, north, sigma_east, sigma_north,
             beam_fwhm_deg=6.0, reference_id="HI4PI/v1/abc123"):
    return AlignmentResultSummary(
        session_id=session_id, target_label=target_label,
        sky_position=SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg),
        beam_fwhm_deg=beam_fwhm_deg, reference_id=reference_id,
        offset_east_deg=east, offset_north_deg=north,
        uncertainty_east_deg=sigma_east, uncertainty_north_deg=sigma_north,
    )


def test_user_specified_consistent_example():
    """A: East 1.0+-0.1, North -0.5+-0.1; B: East 1.05+-0.1, North
    -0.45+-0.1 => consistent (the exact synthetic example given for this
    pass)."""
    a = _summary("A", "TargetX", 200.0, -30.0, 1.0, -0.5, 0.1, 0.1)
    b = _summary("B", "TargetX", 200.0, -30.0, 1.05, -0.45, 0.1, 0.1)
    result = compare_alignment_results(a, b)
    assert result.verdict == "CONSISTENT"
    assert result.same_target is True
    assert result.same_region is True


def test_clearly_incompatible_same_target_case():
    a = _summary("A", "TargetX", 200.0, -30.0, 1.0, -0.5, 0.05, 0.05)
    b = _summary("B", "TargetX", 200.0, -30.0, 2.5, -2.0, 0.05, 0.05)
    result = compare_alignment_results(a, b)
    assert result.verdict == "INCONSISTENT"


def test_marginal_band_between_one_and_three_sigma():
    # combined sigma_east = hypot(0.1,0.1) = 0.1414; delta_east=0.2 -> ~1.41 sigma: MARGINAL
    a = _summary("A", "TargetX", 200.0, -30.0, 1.0, -0.5, 0.1, 0.1)
    b = _summary("B", "TargetX", 200.0, -30.0, 1.2, -0.5, 0.1, 0.1)
    result = compare_alignment_results(a, b)
    assert result.verdict == "MARGINAL"


def test_different_target_far_apart_is_not_labeled_inconsistent():
    """Fase 10: a large offset difference between two DIFFERENT sky
    regions is a pointing-model diagnostic, not a repeatability failure -
    must never come back as INCONSISTENT."""
    a = _summary("A", "TargetX", 50.0, 10.0, 1.0, -0.5, 0.05, 0.05)
    b = _summary("B", "TargetY", 250.0, -60.0, 5.0, 5.0, 0.05, 0.05)
    result = compare_alignment_results(a, b)
    assert result.verdict == "DIFFERENT_REGION"
    assert result.same_target is False
    assert result.same_region is False


def test_same_target_label_is_always_same_region_even_if_coordinates_differ_slightly():
    a = _summary("A", "TargetX", 200.0, -30.0, 1.0, -0.5, 0.1, 0.1)
    b = _summary("B", "TargetX", 200.01, -29.99, 1.0, -0.5, 0.1, 0.1)
    result = compare_alignment_results(a, b)
    assert result.same_target is True
    assert result.same_region is True


def test_nearby_different_target_within_one_beam_is_same_region():
    a = _summary("A", "TargetX", 200.0, -30.0, 1.0, -0.5, 0.1, 0.1, beam_fwhm_deg=6.0)
    b = _summary("B", "TargetX-recentered", 200.0, -30.0 + 2.0, 1.0, -0.5, 0.1, 0.1, beam_fwhm_deg=6.0)
    result = compare_alignment_results(a, b)
    assert result.same_target is False
    assert result.same_region is True  # 2 deg separation < 6 deg beam


def test_missing_uncertainty_on_either_side_returns_unknown_not_a_guess():
    a = _summary("A", "TargetX", 200.0, -30.0, 1.0, -0.5, None, None)
    b = _summary("B", "TargetX", 200.0, -30.0, 1.05, -0.45, 0.1, 0.1)
    result = compare_alignment_results(a, b)
    assert result.verdict == "UNKNOWN"


def test_different_reference_or_beam_is_a_warning_not_a_verdict_change():
    a = _summary("A", "TargetX", 200.0, -30.0, 1.0, -0.5, 0.1, 0.1, beam_fwhm_deg=6.0,
                 reference_id="HI4PI/v1/abc123")
    b = _summary("B", "TargetX", 200.0, -30.0, 1.05, -0.45, 0.1, 0.1, beam_fwhm_deg=8.0,
                 reference_id="HI4PI/v2/def456")
    result = compare_alignment_results(a, b)
    assert result.verdict == "CONSISTENT"
    assert any("reference" in w for w in result.warnings)
    assert any("beam" in w for w in result.warnings)


def test_from_analysis_dir_wires_a_real_replay_output_into_the_comparator(tmp_path):
    """End-to-end: replay_session()'s own output, read back through
    AlignmentResultSummary.from_analysis_dir(), feeds compare_alignment_results()
    without any manual field-mapping - replaying the SAME session twice
    (no mount/SDR involved either time) must compare as CONSISTENT with
    itself."""
    import test_alignment_engine_hi_replay as replay_fixtures
    from alignment_engine.hi.replay import replay_session

    import numpy as np

    session_dir, points = replay_fixtures._build_fixture_session(tmp_path)
    amplitude = [5.0 + 3.0 * np.exp(-((p.east_deg - 1.0) ** 2 + (p.north_deg + 0.5) ** 2) / 4.0) for p in points]

    with patch("alignment_engine.hi.replay._compute_spectrum_from_iq_file",
               side_effect=replay_fixtures._mock_reader_factory(amplitude)):
        result = replay_session(str(session_dir), bootstrap_iterations=10, label="repeat")

    if not result.fit:
        pytest.skip("fixture did not produce a fit - unrelated to the comparator wiring under test")

    summary_a = AlignmentResultSummary.from_analysis_dir(result.analysis_dir, target_label="TargetX")
    summary_b = AlignmentResultSummary.from_analysis_dir(result.analysis_dir, target_label="TargetX")
    comparison = compare_alignment_results(summary_a, summary_b)
    assert comparison.verdict in ("CONSISTENT", "UNKNOWN")  # UNKNOWN only if bootstrap+refinement both absent
    assert comparison.delta_east_deg == pytest.approx(0.0)
    assert comparison.delta_north_deg == pytest.approx(0.0)


def test_wrap_safe_angular_separation_across_ra_zero():
    """RA wrap (359.9 vs 0.1 deg) must be handled via real angular
    separation, not naive subtraction - astropy's SkyCoord.separation()
    already guarantees this; this test guards against a future
    reimplementation with naive RA delta."""
    a = _summary("A", "TargetX", 359.95, 0.0, 1.0, 0.0, 0.1, 0.1)
    b = _summary("B", "TargetX", 0.05, 0.0, 1.0, 0.0, 0.1, 0.1)
    result = compare_alignment_results(a, b)
    assert result.separation_deg < 1.0  # NOT ~359.9 deg from a naive subtraction
