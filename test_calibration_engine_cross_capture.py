"""Cross-capture (within-session) comparison tests."""
import numpy as np
import pytest

from calibration_engine.cross_capture import compute_cross_capture_summary


def test_identical_captures_are_perfectly_consistent():
    rms = [10.0] * 5
    bp = [[1.0] * 50 for _ in range(5)]
    clip = ["OK"] * 5
    dc = [[False] * 45 + [True] * 5 for _ in range(5)]
    result = compute_cross_capture_summary(rms, bp, clip, dc)
    assert result.rms_variation_fraction == 0.0
    assert result.clipping_consistent is True
    assert result.dc_consistent is True


def test_inconsistent_clipping_status_is_flagged():
    rms = [10.0, 10.0]
    bp = [[1.0] * 20, [1.0] * 20]
    clip = ["OK", "CLIPPED"]
    dc = [[False] * 20, [False] * 20]
    result = compute_cross_capture_summary(rms, bp, clip, dc)
    assert result.clipping_consistent is False


def test_requires_at_least_two_captures():
    with pytest.raises(ValueError):
        compute_cross_capture_summary([10.0], [[1.0] * 10], ["OK"], [[False] * 10])


def test_never_claims_warmup_or_thermal_model():
    rms = [10.0, 11.0]
    bp = [[1.0] * 10, [1.1] * 10]
    clip = ["OK", "OK"]
    dc = [[False] * 10, [False] * 10]
    result = compute_cross_capture_summary(rms, bp, clip, dc)
    note = result.to_dict()["note"].lower()
    assert "no warm-up" in note or "no warmup" in note or "warm-up or thermal model inference is made" in note


def test_rms_variation_reflects_real_spread():
    rms = [10.0, 20.0]
    bp = [[1.0] * 10, [1.0] * 10]
    clip = ["OK", "OK"]
    dc = [[False] * 10, [False] * 10]
    result = compute_cross_capture_summary(rms, bp, clip, dc)
    assert result.rms_variation_fraction > 0.2
