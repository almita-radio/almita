"""Unit tests for reduce_engine.models: CaptureRef identity, MaskFlag
multi-reason masking, QualityReport, and MasterSpectrum's own shape/level
guards."""
import numpy as np
import pytest

from reduce_engine.models import (
    CaptureRef, MaskFlag, MasterSpectrum, QualityReport, QualityState, sha256_of_file,
)


def test_sha256_of_file_is_stable_for_same_content(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"almita-raw-evidence")
    assert sha256_of_file(path) == sha256_of_file(path)


def test_sha256_of_file_differs_for_different_content(tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"one"); b.write_bytes(b"two")
    assert sha256_of_file(a) != sha256_of_file(b)


def test_capture_ref_from_capture_file_is_immutable(tmp_path):
    path = tmp_path / "c.h5"
    path.write_bytes(b"fake-hdf5-bytes")
    ref = CaptureRef.from_capture_file(path, campaign_id="CAMP", point_index=1, capture_id="c1",
                                       timestamp_utc="2026-01-01T00:00:00", receiver="RX1")
    with pytest.raises(Exception):
        ref.point_index = 2  # frozen dataclass


def test_maskflag_combine_carries_multiple_reasons():
    combined = MaskFlag.combine(MaskFlag.DC, MaskFlag.EDGE)
    assert set(combined.reasons()) == {"DC", "EDGE"}
    assert not combined.usable()


def test_maskflag_good_has_no_reasons_and_is_usable():
    assert MaskFlag.GOOD.reasons() == []
    assert MaskFlag.GOOD.usable()


def test_quality_report_rejects_invalid_state():
    with pytest.raises(ValueError):
        QualityReport(state="PERFECT")


def _minimal_spectrum(n=16, **overrides):
    base = dict(
        campaign_id="CAMP", point_index=1, ra_hours=1.0, dec_degrees=-30.0,
        timestamp_start_utc="2026-01-01T00:00:00", timestamp_end_utc="2026-01-01T00:00:10",
        frequency_hz=np.linspace(1.42e9, 1.421e9, n), velocity_lsrk_m_s=None, velocity_frame="UNAVAILABLE",
        relative_intensity=np.zeros(n), uncertainty=np.ones(n), mask=np.zeros(n, dtype=np.int64),
        n_contributing=np.ones(n, dtype=np.int64), integration_time_seconds=10.0,
        quality=QualityReport(state=QualityState.GOOD, reasons=["ok"]),
        calibration_level="UNCALIBRATED", calibration_profile_id=None, calibration_profile_hash=None,
        capture_refs=[],
    )
    base.update(overrides)
    return MasterSpectrum(**base)


def test_master_spectrum_rejects_mismatched_array_lengths():
    with pytest.raises(ValueError):
        _minimal_spectrum(n=16, uncertainty=np.ones(8))


def test_master_spectrum_rejects_absolute_calibration_claim():
    with pytest.raises(ValueError):
        _minimal_spectrum(calibration_level="KELVIN")


def test_master_spectrum_manifest_dict_never_embeds_large_arrays():
    spectrum = _minimal_spectrum()
    manifest = spectrum.manifest_dict()
    assert "relative_intensity" not in manifest
    assert "frequency_hz" not in manifest
    assert manifest["n_bins"] == 16
