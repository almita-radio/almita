"""Unit tests for RFI_REF (veto/flag only, conservative coincidence
criterion) and per-capture UNCERTAINTY."""
import numpy as np
import pytest

from reduce_engine.rfi_ref import evaluate_rfi_ref
from reduce_engine.uncertainty import global_metrics, per_capture_uncertainty
from reduce_engine.models import MaskFlag


def test_rfi_ref_unavailable_when_no_reference_capture():
    frequency = np.linspace(1e9, 1.001e9, 16)
    psd = np.ones(16); baseline = np.ones(16)
    outcome = evaluate_rfi_ref(frequency, psd, baseline, ref_frequency_hz=None, ref_psd=None, ref_baseline=None,
                              time_delta_seconds=None, max_time_delta_seconds=3600, min_match_confidence=0.5)
    assert not outcome.available
    assert not outcome.used
    assert not outcome.rfi_bins.any()


def test_rfi_ref_not_used_when_time_delta_too_large():
    frequency = np.linspace(1e9, 1.001e9, 16)
    psd = np.ones(16); baseline = np.ones(16)
    outcome = evaluate_rfi_ref(frequency, psd, baseline, ref_frequency_hz=frequency, ref_psd=psd,
                              ref_baseline=baseline, time_delta_seconds=10_000, max_time_delta_seconds=3600,
                              min_match_confidence=0.5)
    assert outcome.available and not outcome.used
    assert "time delta" in outcome.reason


def test_rfi_ref_flags_only_coincident_excess_not_every_common_signal():
    n = 64
    frequency = np.linspace(1e9, 1.001e9, n)
    baseline = np.ones(n)
    main_psd = np.ones(n)
    main_psd[10] = 50.0  # significant excess in MAIN only
    ref_psd = np.ones(n)  # no coincident excess in RFI_REF
    outcome = evaluate_rfi_ref(frequency, main_psd, baseline, ref_frequency_hz=frequency, ref_psd=ref_psd,
                              ref_baseline=baseline, time_delta_seconds=10, max_time_delta_seconds=3600,
                              min_match_confidence=0.5)
    # a real astrophysical or instrumental feature seen only in MAIN must NOT be flagged as RFI
    assert not outcome.rfi_bins[10]


def test_rfi_ref_flags_coincident_excess_present_in_both():
    n = 64
    frequency = np.linspace(1e9, 1.001e9, n)
    baseline = np.ones(n)
    main_psd = np.ones(n); main_psd[20] = 50.0
    ref_psd = np.ones(n); ref_psd[20] = 50.0
    outcome = evaluate_rfi_ref(frequency, main_psd, baseline, ref_frequency_hz=frequency, ref_psd=ref_psd,
                              ref_baseline=baseline, time_delta_seconds=10, max_time_delta_seconds=3600,
                              min_match_confidence=0.5)
    assert outcome.used
    assert outcome.rfi_bins[20]


def test_rfi_ref_incompatible_frequency_axis_is_not_used():
    frequency = np.linspace(1e9, 1.001e9, 16)
    other_frequency = np.linspace(2e9, 2.001e9, 16)
    psd = np.ones(16); baseline = np.ones(16)
    outcome = evaluate_rfi_ref(frequency, psd, baseline, ref_frequency_hz=other_frequency, ref_psd=psd,
                              ref_baseline=baseline, time_delta_seconds=1, max_time_delta_seconds=3600,
                              min_match_confidence=0.5)
    assert outcome.available and not outcome.used
    assert "incompatible" in outcome.reason


# ---------------------------------------------------------------- uncertainty

def test_per_capture_uncertainty_shrinks_with_more_segments():
    rng = np.random.default_rng(0)
    few = 1.0 + 0.1 * rng.standard_normal((4, 32))
    many = 1.0 + 0.1 * rng.standard_normal((64, 32))
    unc_few = per_capture_uncertainty(few)
    unc_many = per_capture_uncertainty(many)
    assert np.median(unc_many) < np.median(unc_few)


def test_per_capture_uncertainty_nan_with_fewer_than_two_segments():
    single = np.ones((1, 8))
    result = per_capture_uncertainty(single)
    assert np.all(np.isnan(result))


def test_global_metrics_usable_fraction_and_median_noise():
    n = 10
    mask = np.zeros(n, dtype=np.int64)
    mask[:3] = MaskFlag.RFI.value
    uncertainty = np.full(n, 0.5)
    metrics = global_metrics(mask, uncertainty, integration_time_seconds=10.0)
    assert metrics["usable_fraction"] == pytest.approx(0.7)
    assert metrics["median_noise"] == pytest.approx(0.5)
    assert metrics["effective_integration_time_seconds"] == 10.0
