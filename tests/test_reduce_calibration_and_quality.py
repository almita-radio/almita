"""Unit tests for the RELATIVE CALIBRATION and QUALITY stages."""
import numpy as np
import pytest

from reduce_engine.calibration import apply_calibration
from reduce_engine.models import MaskFlag, QualityState
from reduce_engine.quality import assess_quality


def test_apply_calibration_without_profile_is_uncalibrated():
    frequency = np.linspace(1e9, 1.001e9, 16)
    psd = np.ones(16)
    outcome = apply_calibration(frequency, psd, capture_path="/nonexistent.h5", profile=None, profile_path=None)
    assert outcome.calibration_level == "UNCALIBRATED"
    assert outcome.compatibility_status == "NOT_ATTEMPTED"
    assert outcome.relative_psd_db is None


def test_apply_calibration_never_claims_relative_without_profile():
    frequency = np.linspace(1e9, 1.001e9, 16)
    psd = np.ones(16)
    outcome = apply_calibration(frequency, psd, capture_path="/nonexistent.h5", profile=None, profile_path=None)
    assert outcome.calibration_level in ("RELATIVE", "UNCALIBRATED")
    assert outcome.calibration_level != "RELATIVE"


GOOD = MaskFlag.GOOD.value


def _mask(n, good_fraction=1.0, rfi_fraction=0.0):
    mask = np.full(n, GOOD, dtype=np.int64)
    n_rfi = int(n * rfi_fraction)
    mask[:n_rfi] = MaskFlag.RFI.value
    return mask


def test_quality_good_when_everything_nominal():
    n = 100
    report = assess_quality(mask=_mask(n), n_contributing=np.ones(n), clipping_fraction=0.0,
                            calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                            baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk")
    assert report.state == QualityState.GOOD
    assert report.reasons


def test_quality_bad_when_clipping_detected():
    n = 100
    report = assess_quality(mask=_mask(n), n_contributing=np.ones(n), clipping_fraction=0.01,
                            calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                            baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk")
    assert report.state == QualityState.BAD
    assert any("clipping" in r for r in report.reasons)


def test_quality_bad_when_usable_fraction_too_low():
    n = 100
    report = assess_quality(mask=_mask(n, rfi_fraction=0.9), n_contributing=np.ones(n), clipping_fraction=0.0,
                            calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                            baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk")
    assert report.state == QualityState.BAD


def test_quality_warning_when_uncalibrated_but_otherwise_fine():
    n = 100
    report = assess_quality(mask=_mask(n), n_contributing=np.ones(n), clipping_fraction=0.0,
                            calibration_level="UNCALIBRATED", calibration_compatibility_status="NOT_ATTEMPTED",
                            baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk")
    assert report.state == QualityState.WARNING


def test_quality_warning_when_velocity_unavailable():
    n = 100
    report = assess_quality(mask=_mask(n), n_contributing=np.ones(n), clipping_fraction=0.0,
                            calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                            baseline_fit_quality_rms_fraction=0.05, velocity_frame="UNAVAILABLE")
    assert report.state == QualityState.WARNING


def test_quality_never_bare_state_without_reasons():
    n = 100
    for level in ("RELATIVE", "UNCALIBRATED"):
        report = assess_quality(mask=_mask(n), n_contributing=np.ones(n), clipping_fraction=0.0,
                                calibration_level=level, calibration_compatibility_status="COMPATIBLE",
                                baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk")
        assert len(report.reasons) > 0
