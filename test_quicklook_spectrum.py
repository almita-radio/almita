import hashlib
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

from calibration_foundation import apply_relative_calibration, load_calibration_profile
from quicklook_spectrum import QuicklookError, generate_quicklook


ROOT = Path(__file__).parent
PROFILE = ROOT / "data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1.npz"
SOURCE = ROOT / "data/rf_characterization/INDOOR-ANTENNA-COUPLING-CHECK-01-20260827T003104Z/antenna_a.h5"
DIRECT = ROOT / "data/rf_characterization/RF-CHAIN-ISOLATION-02-20260826T210837Z/direct/rtl_direct_50ohm_gain_40.2.h5"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    output = tmp_path_factory.mktemp("quicklook")
    before = (digest(SOURCE), digest(PROFILE), digest(PROFILE.with_suffix(".json")))
    document, arrays = generate_quicklook(SOURCE, PROFILE, output)
    after = (digest(SOURCE), digest(PROFILE), digest(PROFILE.with_suffix(".json")))
    return output, document, arrays, before, after


def minimal_hdf5(path, *, topology=True, gain=40.2):
    with h5py.File(path, "w") as capture:
        capture.create_dataset("iq_data", data=np.full(8192 * 8 * 2, 127, np.uint8))
        capture.attrs["capture_status"] = "success"
        capture.attrs["center_frequency_hz"] = 1420405752
        capture.attrs["sample_rate_hz"] = 2400000
        capture.attrs["gain_requested_db"] = gain
        if topology:
            capture.attrs["rf_input"] = "ANTENNA_AT_LNA_INPUT_INDOOR"


def test_reject_part(tmp_path):
    with pytest.raises(QuicklookError, match="partial"):
        generate_quicklook(tmp_path / "capture.h5.part", PROFILE, tmp_path / "out")


def test_reject_nonexistent(tmp_path):
    with pytest.raises(QuicklookError, match="does not exist"):
        generate_quicklook(tmp_path / "missing.h5", PROFILE, tmp_path / "out")


def test_reject_invalid_hdf5(tmp_path):
    invalid = tmp_path / "invalid.h5"
    with h5py.File(invalid, "w"):
        pass
    with pytest.raises(QuicklookError, match="iq_data"):
        generate_quicklook(invalid, PROFILE, tmp_path / "out")


def test_compatible_metadata_and_no_absolute_units(generated):
    _, document, _, _, _ = generated
    assert document["compatibility"]["status"] == "COMPATIBLE"
    assert document["calibration_level"] == "RELATIVE_INSTRUMENTAL"
    assert document["absolute_calibration"] is False
    forbidden = {"temperature_kelvin", "antenna_temperature_kelvin", "flux_jy"}
    assert forbidden.isdisjoint(document)


def test_reject_incompatible(tmp_path):
    with pytest.raises(QuicklookError, match="INCOMPATIBLE"):
        generate_quicklook(DIRECT, PROFILE, tmp_path / "out")


def test_reject_unknown_compatibility(tmp_path):
    capture = tmp_path / "unknown.h5"
    minimal_hdf5(capture, topology=False)
    with pytest.raises(QuicklookError, match="UNKNOWN"):
        generate_quicklook(capture, PROFILE, tmp_path / "out")


def test_json_schema_and_dashboard_independence(generated):
    output, document, _, _, _ = generated
    stored = json.loads((output / "quicklook_spectrum.json").read_text())
    required = {
        "schema_version", "status", "source_hdf5", "created_utc",
        "calibration_level", "absolute_calibration", "calibration_profile",
        "compatibility", "center_frequency_hz", "sample_rate_hz", "gain_db",
        "capture_duration_seconds", "fft_size", "frequency_hz",
        "relative_psd_db", "fractional_excess", "fractional_uncertainty",
        "valid_mask", "dc_mask", "spur_mask", "masked_fraction",
        "quicklook_metrics", "known_limitations",
    }
    assert required <= stored.keys()
    assert isinstance(stored["frequency_hz"], list)
    assert "iq_data" not in stored
    assert stored["status"] == document["status"]


def test_pngs_generated(generated):
    output, _, _, _, _ = generated
    assert (output / "quicklook_spectrum.png").stat().st_size > 1000
    assert (output / "quicklook_fractional_excess.png").stat().st_size > 1000


def test_valid_mask_dc_spur_excluded_from_statistics(generated):
    _, document, arrays, _, _ = generated
    valid = arrays["valid_mask"]
    assert not np.any(valid & arrays["dc_mask"])
    assert not np.any(valid & arrays["spur_mask"])
    expected = np.median(arrays["fractional_excess"][valid])
    assert document["quicklook_metrics"]["median_fractional_excess"] == expected


def test_source_and_profile_unchanged(generated):
    _, document, _, before, after = generated
    assert before == after
    assert document["integrity"]["unchanged"] is True


def test_quicklook_equals_calibration_foundation(generated):
    _, _, arrays, _, _ = generated
    profile = load_calibration_profile(PROFILE)
    expected = apply_relative_calibration(profile, SOURCE, scale_mode="none")
    for key in ("fractional_excess", "relative_psd_db", "fractional_uncertainty"):
        delta = np.abs(arrays[key] - expected[key])
        assert np.max(delta) == 0
        assert np.median(delta) == 0
    assert np.array_equal(arrays["valid_mask"], expected["valid_mask"])


def test_deterministic_numerical_arrays(generated, tmp_path):
    _, _, first, _, _ = generated
    _, second = generate_quicklook(SOURCE, PROFILE, tmp_path / "again")
    for key in first:
        assert np.array_equal(first[key], second[key])
