"""CALIBRATION MATRIX (2nd-pass sections 10-11): compatible / absent /
incompatible profile, each with a direct assertion - and confirming
calibration failure never hides behind a bare "quality=GOOD" without
an explicit reason in calibration_level/quality/manifest.
"""
from pathlib import Path

import numpy as np
import pytest

import calibration_foundation as cf
from reduce_engine.calibration import apply_calibration, load_profile

REAL_PROFILE = "data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1"
REAL_COMPATIBLE_CAPTURE = ("data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16/data/iq/"
                          "ALMITA-WEB-SMALL-RUN-01-20260902-16:18:20/ALMITA-WEB-SMALL-RUN-01_0001.h5")


def _skip_if_absent(*paths):
    for path in paths:
        if not Path(path).exists() and not Path(path + ".json").exists():
            pytest.skip(f"real fixture not present: {path}")


# ---------------------------------------------------------------- A. compatible

def test_a_compatible_profile_yields_relative():
    _skip_if_absent(REAL_PROFILE + ".json", REAL_COMPATIBLE_CAPTURE)
    profile = load_profile(REAL_PROFILE)
    frequency, psd, attrs, _ = cf.canonical_psd_from_hdf5(REAL_COMPATIBLE_CAPTURE, profile["metadata"]["fft_size"])
    outcome = apply_calibration(frequency, psd, capture_path=REAL_COMPATIBLE_CAPTURE,
                                profile=profile, profile_path=REAL_PROFILE)
    assert outcome.calibration_level == "RELATIVE"
    assert outcome.compatibility_status == "COMPATIBLE"
    assert outcome.fractional_excess is not None
    assert outcome.profile_id == "calibration_profile_v1"
    assert outcome.profile_hash is not None


# ---------------------------------------------------------------- B. absent

def test_b_no_profile_yields_uncalibrated_with_explicit_reason():
    frequency = np.linspace(1e9, 1.001e9, 32)
    psd = np.ones(32)
    outcome = apply_calibration(frequency, psd, capture_path="/nonexistent.h5", profile=None, profile_path=None)
    assert outcome.calibration_level == "UNCALIBRATED"
    assert outcome.compatibility_status == "NOT_ATTEMPTED"
    assert "no calibration profile supplied" in outcome.compatibility_reason
    assert outcome.profile_id is None and outcome.profile_hash is None
    assert outcome.fractional_excess is None  # never fabricated


# ---------------------------------------------------------------- C. incompatible (real profile, mismatched capture)

def test_c_incompatible_sample_rate_never_silently_applied():
    _skip_if_absent(REAL_PROFILE + ".json")
    profile = load_profile(REAL_PROFILE)
    # a real profile's metadata, with sample_rate mutated - simulates a
    # real capture whose declared sample_rate genuinely differs.
    frequency = profile["frequency_hz"]
    psd = np.abs(profile["reference_psd"])  # a plausible-looking spectrum, not garbage

    class _FakeH5:
        pass

    # check_calibration_compatibility reads real HDF5 attrs from a path -
    # exercise it directly against the real API contract instead of
    # faking a file: build a tiny real HDF5 capture with a mismatched
    # sample_rate and confirm INCOMPATIBLE, never silently applied.
    import h5py
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mismatched.h5"
        with h5py.File(path, "w") as handle:
            handle.create_dataset("iq_data", data=np.zeros(1024, dtype=np.uint8))
            handle.attrs["capture_status"] = "success"
            handle.attrs["center_frequency_hz"] = profile["metadata"]["center_frequency_hz"]
            handle.attrs["sample_rate_hz"] = profile["metadata"]["sample_rate_hz"] * 2  # deliberately wrong
            handle.attrs["gain_requested_db"] = profile["metadata"]["gain_db"]
        outcome = apply_calibration(frequency, psd, capture_path=str(path), profile=profile, profile_path=REAL_PROFILE)
        assert outcome.calibration_level == "UNCALIBRATED"
        assert outcome.compatibility_status == "INCOMPATIBLE"
        assert "sample rate" in outcome.compatibility_reason.lower()
        assert outcome.fractional_excess is None


def test_c_incompatible_gain_never_silently_applied():
    _skip_if_absent(REAL_PROFILE + ".json")
    profile = load_profile(REAL_PROFILE)
    frequency = profile["frequency_hz"]
    psd = np.abs(profile["reference_psd"])
    import h5py
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mismatched_gain.h5"
        with h5py.File(path, "w") as handle:
            handle.create_dataset("iq_data", data=np.zeros(1024, dtype=np.uint8))
            handle.attrs["capture_status"] = "success"
            handle.attrs["center_frequency_hz"] = profile["metadata"]["center_frequency_hz"]
            handle.attrs["sample_rate_hz"] = profile["metadata"]["sample_rate_hz"]
            handle.attrs["gain_requested_db"] = profile["metadata"]["gain_db"] + 10.0  # deliberately wrong
        outcome = apply_calibration(frequency, psd, capture_path=str(path), profile=profile, profile_path=REAL_PROFILE)
        assert outcome.calibration_level == "UNCALIBRATED"
        assert outcome.compatibility_status == "INCOMPATIBLE"
        assert "gain" in outcome.compatibility_reason.lower()


# ---------------------------------------------------------------- 11. must not fake success

def test_calibration_failure_reason_survives_into_quality():
    """Section 11: an UNCALIBRATED result must show up in quality's
    reasons, not just be a quiet metadata field while quality reads GOOD
    without context."""
    from reduce_engine.quality import assess_quality
    from reduce_engine.models import MaskFlag, QualityState
    n = 100
    mask = np.full(n, MaskFlag.GOOD.value, dtype=np.int64)
    report = assess_quality(mask=mask, n_contributing=np.ones(n), clipping_fraction=0.0,
                            calibration_level="UNCALIBRATED", calibration_compatibility_status="INCOMPATIBLE",
                            baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk")
    # documented criterion (docs/REDUCE_PIPELINE.md): absence of
    # calibration is WARNING, not a silent GOOD and not BAD on its own -
    # but it MUST be visible in reasons either way.
    assert report.state == QualityState.WARNING
    assert any("INCOMPATIBLE" in r or "calibration" in r.lower() for r in report.reasons)
