"""Fase 39 test matrix: synthetic/fixture/unverified always blocked; only
REAL_VALIDATED + GOOD + tight-enough numbers is ELIGIBLE."""
from alignment_engine.hi.quality import evaluate_sync_eligibility
from alignment_engine.hi.reference_trust import ReferenceTrust

GOOD_KWARGS = dict(fit_rating="GOOD", confidence=0.9, valid_fraction=0.9,
                    offset_east_deg=1.0, offset_north_deg=-0.5, raster_half_extent_deg=6.0)


def test_synthetic_reference_is_always_blocked():
    result = evaluate_sync_eligibility(trust=ReferenceTrust.SYNTHETIC, **GOOD_KWARGS)
    assert result.verdict == "NOT_ELIGIBLE"


def test_test_fixture_reference_is_always_blocked():
    result = evaluate_sync_eligibility(trust=ReferenceTrust.TEST_FIXTURE, **GOOD_KWARGS)
    assert result.verdict == "NOT_ELIGIBLE"


def test_real_unverified_reference_is_blocked():
    result = evaluate_sync_eligibility(trust=ReferenceTrust.REAL_UNVERIFIED, **GOOD_KWARGS)
    assert result.verdict == "NOT_ELIGIBLE"


def test_real_validated_but_bad_fit_is_blocked():
    kwargs = dict(GOOD_KWARGS)
    kwargs["fit_rating"] = "BAD"
    result = evaluate_sync_eligibility(trust=ReferenceTrust.REAL_VALIDATED, **kwargs)
    assert result.verdict == "NOT_ELIGIBLE"


def test_real_validated_and_good_is_eligible():
    result = evaluate_sync_eligibility(trust=ReferenceTrust.REAL_VALIDATED, **GOOD_KWARGS)
    assert result.verdict == "ELIGIBLE"


def test_low_confidence_blocks_even_if_validated_and_good():
    kwargs = dict(GOOD_KWARGS)
    kwargs["confidence"] = 0.5
    result = evaluate_sync_eligibility(trust=ReferenceTrust.REAL_VALIDATED, **kwargs)
    assert result.verdict == "NOT_ELIGIBLE"


def test_low_valid_fraction_blocks():
    kwargs = dict(GOOD_KWARGS)
    kwargs["valid_fraction"] = 0.2
    result = evaluate_sync_eligibility(trust=ReferenceTrust.REAL_VALIDATED, **kwargs)
    assert result.verdict == "NOT_ELIGIBLE"


def test_huge_offset_is_blocked_by_the_safety_ceiling():
    kwargs = dict(GOOD_KWARGS)
    kwargs["offset_east_deg"], kwargs["offset_north_deg"] = 15.0, 0.0
    result = evaluate_sync_eligibility(trust=ReferenceTrust.REAL_VALIDATED, **kwargs)
    assert result.verdict == "NOT_ELIGIBLE"
    assert any("safety ceiling" in r for r in result.reasons)


def test_offset_near_raster_edge_is_blocked():
    kwargs = dict(GOOD_KWARGS)
    kwargs["offset_east_deg"] = 5.8  # raster_half_extent_deg=6.0, margin < 1.0
    result = evaluate_sync_eligibility(trust=ReferenceTrust.REAL_VALIDATED, **kwargs)
    assert result.verdict == "NOT_ELIGIBLE"
    assert any("edge" in r for r in result.reasons)


def test_all_failure_reasons_are_collected_not_just_the_first():
    result = evaluate_sync_eligibility(trust=ReferenceTrust.SYNTHETIC, fit_rating="BAD", confidence=0.1,
                                        valid_fraction=0.1, offset_east_deg=20.0, offset_north_deg=20.0,
                                        raster_half_extent_deg=6.0)
    assert len(result.reasons) >= 4
