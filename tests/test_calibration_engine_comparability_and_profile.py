"""Comparability (Fase 29, 43, 65, 68) and profile DRAFT/versioning/
envelope (Fase 35-36, 42, 62)."""
import pytest

from calibration_engine.comparability import (
    ComparabilityVerdict, compare_calibration_context, compare_calibration_sessions,
)
from calibration_engine.profile import (
    EnvelopeStatus, OperationalEnvelope, ProfileStatus, build_draft_profile, evaluate_envelope, write_profile,
)
from calibration_engine.receiver_config import build_receiver_config_snapshot


def _snapshot(**overrides):
    defaults = dict(receiver_id="MAIN", center_frequency_hz=1_420_405_752.0, sample_rate_hz=2_400_000.0,
                     gain_requested_db=40.2, bias_t_state="ON", deployment_state="INDOOR")
    defaults.update(overrides)
    return build_receiver_config_snapshot(**defaults)


# ---------------------------------------------------------------- comparability

def test_identical_context_is_comparable():
    a, b = _snapshot(), _snapshot()
    result = compare_calibration_context(a, b)
    assert result.verdict == ComparabilityVerdict.COMPARABLE


def test_different_receiver_is_not_comparable():
    a = _snapshot(receiver_id="MAIN")
    b = _snapshot(receiver_id="RFI_REF")
    result = compare_calibration_context(a, b)
    assert result.verdict == ComparabilityVerdict.NOT_COMPARABLE


def test_different_sample_rate_is_not_comparable():
    a = _snapshot(sample_rate_hz=2_400_000.0)
    b = _snapshot(sample_rate_hz=1_200_000.0)
    result = compare_calibration_context(a, b)
    assert result.verdict == ComparabilityVerdict.NOT_COMPARABLE


def test_different_gain_is_partially_comparable_not_disqualifying():
    a = _snapshot(gain_requested_db=40.2)
    b = _snapshot(gain_requested_db=20.7)
    result = compare_calibration_context(a, b)
    assert result.verdict == ComparabilityVerdict.PARTIALLY_COMPARABLE
    assert any("gain" in flag for flag in result.flags)


def test_different_deployment_flags_indoor_vs_field_fase_68():
    a = _snapshot(deployment_state="INDOOR")
    b = _snapshot(deployment_state="FIELD")
    result = compare_calibration_context(a, b)
    assert result.verdict == ComparabilityVerdict.PARTIALLY_COMPARABLE
    assert any("deployment" in flag.lower() for flag in result.flags)


def test_different_bias_t_is_flagged():
    a = _snapshot(bias_t_state="ON")
    b = _snapshot(bias_t_state="OFF")
    result = compare_calibration_context(a, b)
    assert any("bias-t" in flag.lower() for flag in result.flags)


def test_session_comparison_not_comparable_short_circuits_before_metrics():
    a, b = _snapshot(sample_rate_hz=2_400_000.0), _snapshot(sample_rate_hz=1_200_000.0)
    result = compare_calibration_sessions(a, b, 10.0, 999.0, 0.0, 0.9)
    assert result.verdict == "NOT_COMPARABLE"
    assert result.metric_deltas == {}


def test_session_comparison_consistent_for_small_deltas():
    a, b = _snapshot(), _snapshot()
    result = compare_calibration_sessions(a, b, 10.0, 10.2, 0.0, 0.0)
    assert result.verdict == "CONSISTENT"


def test_session_comparison_inconsistent_for_large_rms_delta():
    a, b = _snapshot(), _snapshot()
    result = compare_calibration_sessions(a, b, 10.0, 30.0, 0.0, 0.0)
    assert result.verdict == "INCONSISTENT"


def test_session_comparison_sensitive_to_clipping_fraction_change():
    a, b = _snapshot(), _snapshot()
    result = compare_calibration_sessions(a, b, 10.0, 10.0, 0.0, 0.05)
    assert result.verdict in ("MARGINAL", "INCONSISTENT")


# ---------------------------------------------------------------- profile

def _envelope(**overrides):
    defaults = dict(gain_range_tested_db=[20.7, 40.2], temperature_range_observed_c=[15.0, 30.0],
                     clipping_maximum_observed="OK", stability_rms_fraction_expected_max=0.05,
                     usable_band_fraction=0.9, known_artifacts=[])
    defaults.update(overrides)
    return OperationalEnvelope(**defaults)


def test_draft_profile_is_never_active():
    profile = build_draft_profile(receiver_id="MAIN", receiver_serial="00000001",
                                   source_calibration_sessions=["CAL-1"], conditions={}, recommended_gain_db=40.2,
                                   recommended_usable_band=None, known_masks=[], warmup_recommendation=None,
                                   envelope=_envelope(), limitations=[])
    assert profile.status == ProfileStatus.DRAFT
    assert profile.to_dict()["active"] is False


def test_profile_requires_at_least_one_source_session():
    with pytest.raises(ValueError):
        build_draft_profile(receiver_id="MAIN", receiver_serial="00000001", source_calibration_sessions=[],
                             conditions={}, recommended_gain_db=40.2, recommended_usable_band=None, known_masks=[],
                             warmup_recommendation=None, envelope=_envelope(), limitations=[])


def test_write_profile_never_overwrites(tmp_path):
    profile = build_draft_profile(receiver_id="MAIN", receiver_serial="00000001",
                                   source_calibration_sessions=["CAL-1"], conditions={}, recommended_gain_db=40.2,
                                   recommended_usable_band=None, known_masks=[], warmup_recommendation=None,
                                   envelope=_envelope(), limitations=[])
    path = write_profile(profile, tmp_path)
    assert path.exists()
    with pytest.raises(FileExistsError):
        write_profile(profile, tmp_path)


def test_envelope_within_when_all_metrics_inside_range():
    status = evaluate_envelope(_envelope(), gain_db=30.0, temperature_c=20.0, stability_rms_fraction=0.02)
    assert status == EnvelopeStatus.WITHIN_ENVELOPE


def test_envelope_outside_for_gain_beyond_tested_range():
    status = evaluate_envelope(_envelope(), gain_db=49.6, temperature_c=20.0, stability_rms_fraction=0.02)
    assert status == EnvelopeStatus.OUTSIDE_ENVELOPE


def test_envelope_marginal_for_stability_moderately_above_expected():
    status = evaluate_envelope(_envelope(), gain_db=30.0, temperature_c=20.0, stability_rms_fraction=0.08)
    assert status == EnvelopeStatus.MARGINAL


def test_envelope_unknown_when_nothing_is_provided():
    assert evaluate_envelope(_envelope()) == EnvelopeStatus.UNKNOWN


def test_envelope_never_aborts_anything_v1_is_advisory_only():
    """Fase 42/67: the function only classifies - it has no side effect
    and no exception path for OUTSIDE_ENVELOPE, by construction."""
    status = evaluate_envelope(_envelope(), gain_db=1000.0)
    assert status == EnvelopeStatus.OUTSIDE_ENVELOPE  # classified, not raised
