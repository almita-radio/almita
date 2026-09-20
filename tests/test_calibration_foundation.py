import hashlib
from pathlib import Path

import h5py
import numpy as np
import pytest

from calibration_foundation import (
    apply_relative_calibration,
    apply_relative_calibration_to_psd,
    build_calibration_profile,
    canonical_psd_from_hdf5,
    check_calibration_compatibility,
    load_calibration_profile,
)


TOPOLOGY = "50_OHM_TO_LNA_FILTER_CABLING_TO_RTL_SDR"


def make_capture(path, *, frequency=1420405752, sample_rate=2400000,
                 gain=40.2, rf_input="50_OHM_AT_LNA_INPUT", seed=1,
                 offset=0):
    rng = np.random.default_rng(seed)
    samples = 8192 * 8
    raw = np.clip(np.rint(rng.normal(127.5 + offset, 2.0, samples * 2)), 0, 255).astype(np.uint8)
    with h5py.File(path, "w") as capture:
        capture.create_dataset("iq_data", data=raw)
        capture.attrs["capture_status"] = "success"
        capture.attrs["center_frequency_hz"] = frequency
        capture.attrs["sample_rate_hz"] = sample_rate
        capture.attrs["gain_requested_db"] = gain
        capture.attrs["gain"] = "auto"
        capture.attrs["rf_input"] = rf_input
        capture.attrs["duration_seconds"] = samples / sample_rate
        capture.attrs["created_at"] = "2026-01-01T00:00:00+00:00"
    return path


@pytest.fixture
def profile_fixture(tmp_path):
    first = make_capture(tmp_path / "first.h5", seed=1)
    second = make_capture(tmp_path / "second.h5", seed=2)
    stem = tmp_path / "calibration_profile_v1"
    profile = build_calibration_profile(
        [first, second], stem, gain_db=40.2, reference_topology=TOPOLOGY
    )
    return profile, stem, first, second


def test_profile_creation_frequency_dc_spur_and_uncertainty(profile_fixture):
    profile, _, _, _ = profile_fixture
    assert profile["frequency_hz"].shape == (8192,)
    assert np.allclose(np.diff(profile["frequency_hz"]), 2400000 / 8192)
    assert profile["dc_mask"].any()
    assert profile["dc_mask"][4096]
    assert profile["spur_mask"].dtype == np.bool_
    assert np.all(profile["reference_sigma"] >= 0)
    assert np.array_equal(
        profile["valid_mask"],
        ~(profile["dc_mask"] | profile["spur_mask"] |
          ~profile["valid_mask"] & ~(profile["dc_mask"] | profile["spur_mask"]))
    )


def test_reference_is_binwise_median(profile_fixture):
    profile, _, first, second = profile_fixture
    _, a, _, _ = canonical_psd_from_hdf5(first)
    _, b, _, _ = canonical_psd_from_hdf5(second)
    assert np.allclose(profile["reference_psd"], np.median([a, b], axis=0))


def test_serialization_round_trip_and_no_absolute_units(profile_fixture):
    profile, stem, _, _ = profile_fixture
    loaded = load_calibration_profile(stem.with_suffix(".json"))
    assert np.array_equal(loaded["reference_psd"], profile["reference_psd"])
    metadata = loaded["metadata"]
    assert metadata["calibration_level"] == "RELATIVE_INSTRUMENTAL"
    assert metadata["absolute_calibration"] is False
    assert metadata["temperature_kelvin"] is None
    assert metadata["antenna_temperature_kelvin"] is None
    assert metadata["flux_jy"] is None


def test_compatibility_pass_and_antenna_same_chain(profile_fixture, tmp_path):
    profile, _, first, _ = profile_fixture
    assert check_calibration_compatibility(profile, first)["status"] == "COMPATIBLE"
    antenna = make_capture(
        tmp_path / "antenna.h5", rf_input="ANTENNA_AT_LNA_INPUT_INDOOR"
    )
    assert check_calibration_compatibility(profile, antenna)["status"] == "COMPATIBLE"


@pytest.mark.parametrize(("field", "value"), [
    ("frequency", 1420405753), ("sample_rate", 2000000), ("gain", 29.7),
])
def test_incompatibility_configuration(profile_fixture, tmp_path, field, value):
    profile, _, _, _ = profile_fixture
    kwargs = {field: value}
    capture = make_capture(tmp_path / f"bad_{field}.h5", **kwargs)
    result = check_calibration_compatibility(profile, capture)
    assert result["status"] == "INCOMPATIBLE"
    label = {"frequency": "center frequency", "sample_rate": "sample rate", "gain": "gain"}[field]
    assert label in result["reason"]


def test_incompatibility_direct_topology(profile_fixture, tmp_path):
    profile, _, _, _ = profile_fixture
    direct = make_capture(tmp_path / "direct.h5", rf_input="50_OHM_DIRECT_RTL_SDR")
    assert check_calibration_compatibility(profile, direct)["status"] == "INCOMPATIBLE"


def test_incompatibility_fft_configuration(profile_fixture, tmp_path):
    profile, _, _, _ = profile_fixture
    capture = make_capture(tmp_path / "fft.h5")
    with h5py.File(capture, "r+") as source:
        source.attrs["calibration_fft_size"] = 4096
    result = check_calibration_compatibility(profile, capture)
    assert result["status"] == "INCOMPATIBLE"
    assert "FFT size" in result["reason"]


def test_relative_calibration_and_explicit_scaling(profile_fixture, tmp_path):
    profile, _, _, _ = profile_fixture
    target = make_capture(
        tmp_path / "target.h5", rf_input="ANTENNA_AT_LNA_INPUT_INDOOR", seed=3
    )
    raw = apply_relative_calibration(profile, target)
    scaled = apply_relative_calibration(profile, target, scale_mode="median_scalar")
    assert raw["fractional_excess"].shape == (8192,)
    assert raw["reference_scale"] == 1.0
    assert scaled["reference_scale"] > 0
    assert np.isfinite(raw["relative_psd_db"][raw["valid_mask"]]).all()


def test_psd_application_primitive_matches_hdf5_api(profile_fixture, tmp_path):
    profile, _, _, _ = profile_fixture
    target = make_capture(tmp_path / "primitive.h5", seed=9)
    frequency, psd, _, _ = canonical_psd_from_hdf5(target)
    direct = apply_relative_calibration_to_psd(profile, frequency, psd)
    hdf5_result = apply_relative_calibration(profile, target)
    for key in ("fractional_excess", "relative_psd_db", "fractional_uncertainty"):
        assert np.array_equal(direct[key], hdf5_result[key])


def test_source_hdf5_unchanged(profile_fixture):
    _, _, first, _ = profile_fixture
    before = hashlib.sha256(Path(first).read_bytes()).hexdigest()
    canonical_psd_from_hdf5(first)
    after = hashlib.sha256(Path(first).read_bytes()).hexdigest()
    assert after == before


def test_part_rejected(profile_fixture, tmp_path):
    profile, _, _, _ = profile_fixture
    partial = make_capture(tmp_path / "capture.h5.part")
    assert check_calibration_compatibility(profile, partial)["status"] == "INCOMPATIBLE"
    with pytest.raises(ValueError, match="partial"):
        canonical_psd_from_hdf5(partial)
