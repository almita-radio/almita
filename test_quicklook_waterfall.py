import hashlib
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

from hi_spectral_metric import robust_psd_from_iq
from quicklook_waterfall import (
    WaterfallError,
    compute_native_waterfall,
    decimate_dashboard,
    generate_waterfall,
    make_time_bin_bounds,
    validate_waterfall,
)


ROOT = Path(__file__).parent
PROFILE = ROOT / "data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1.npz"
SOURCE = ROOT / "data/rf_characterization/INDOOR-ANTENNA-COUPLING-CHECK-01-20260827T003104Z/antenna_a.h5"
DIRECT = ROOT / "data/rf_characterization/RF-CHAIN-ISOLATION-02-20260826T210837Z/direct/rtl_direct_50ohm_gain_40.2.h5"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    output = tmp_path_factory.mktemp("waterfall")
    before = (digest(SOURCE), digest(PROFILE), digest(PROFILE.with_suffix(".json")))
    document, native = generate_waterfall(
        SOURCE, PROFILE, output, time_bins=16, frequency_bins=256
    )
    after = (digest(SOURCE), digest(PROFILE), digest(PROFILE.with_suffix(".json")))
    return output, document, native, before, after


def minimal_hdf5(path, *, topology=True):
    with h5py.File(path, "w") as capture:
        capture.create_dataset("iq_data", data=np.full(8192 * 8 * 2, 127, np.uint8))
        capture.attrs["capture_status"] = "success"
        capture.attrs["center_frequency_hz"] = 1420405752
        capture.attrs["sample_rate_hz"] = 2400000
        capture.attrs["gain_requested_db"] = 40.2
        if topology:
            capture.attrs["rf_input"] = "ANTENNA_AT_LNA_INPUT_INDOOR"


def test_reject_part_and_nonexistent(tmp_path):
    with pytest.raises(WaterfallError, match="partial"):
        generate_waterfall(tmp_path / "x.h5.part", PROFILE, tmp_path / "out")
    with pytest.raises(WaterfallError, match="does not exist"):
        generate_waterfall(tmp_path / "missing.h5", PROFILE, tmp_path / "out2")


def test_reject_invalid_hdf5(tmp_path):
    path = tmp_path / "bad.h5"
    with h5py.File(path, "w"):
        pass
    with pytest.raises(WaterfallError, match="iq_data"):
        generate_waterfall(path, PROFILE, tmp_path / "out")


def test_compatibility_guards(tmp_path):
    with pytest.raises(WaterfallError, match="INCOMPATIBLE"):
        generate_waterfall(DIRECT, PROFILE, tmp_path / "direct")
    unknown = tmp_path / "unknown.h5"
    minimal_hdf5(unknown, topology=False)
    with pytest.raises(WaterfallError, match="UNKNOWN"):
        generate_waterfall(unknown, PROFILE, tmp_path / "unknown_out")


def test_time_bins_complete_without_overlap_or_gaps():
    bounds = make_time_bin_bounds(4_800_000, 64, 8192)
    assert len(bounds) == 64
    assert bounds[0, 0] == 0
    assert bounds[-1, 1] == (4_800_000 // 8192) * 8192
    assert 4_800_000 - bounds[-1, 1] < 8192
    assert np.array_equal(bounds[:-1, 1], bounds[1:, 0])
    assert np.all(bounds[:, 1] > bounds[:, 0])


def test_frequency_axis_calibration_and_masks(generated):
    _, document, native, _, _ = generated
    assert native["fractional_excess"].shape == (16, 8192)
    assert np.allclose(np.diff(native["frequency_hz"]), 2400000 / 8192)
    assert np.isnan(native["fractional_excess"][:, ~native["valid_mask"]]).all()
    assert not np.any(native["valid_mask"] & native["dc_mask"])
    assert not np.any(native["valid_mask"] & native["spur_mask"])
    assert document["compatibility"]["status"] == "COMPATIBLE"


def test_dashboard_median_decimation():
    frequency = np.arange(16.0)
    waterfall = np.arange(32.0).reshape(2, 16)
    valid = np.ones(16, bool)
    valid[0:4] = False
    f, values, dashboard_valid = decimate_dashboard(
        frequency, waterfall, valid, frequency_bins=4
    )
    assert values.shape == (2, 4)
    assert not dashboard_valid[0]
    assert np.isnan(values[:, 0]).all()
    assert values[0, 1] == np.median(waterfall[0, 4:8])
    assert np.array_equal(f, [1.5, 5.5, 9.5, 13.5])


def test_synthetic_temporal_feature_recovery(tmp_path):
    fft_size = 8192
    segments_per_half = 6
    half = fft_size * segments_per_half
    rng = np.random.default_rng(14)
    noise_i = rng.normal(0, 1.0, half * 2)
    noise_q = rng.normal(0, 1.0, half * 2)
    tone_bin = 700
    n = np.arange(half)
    noise_i[half:] += 10 * np.cos(2 * np.pi * tone_bin * n / fft_size)
    noise_q[half:] += 10 * np.sin(2 * np.pi * tone_bin * n / fft_size)
    raw = np.empty(half * 4, np.uint8)
    raw[0::2] = np.clip(np.rint(127.5 + noise_i), 0, 255).astype(np.uint8)
    raw[1::2] = np.clip(np.rint(127.5 + noise_q), 0, 255).astype(np.uint8)
    path = tmp_path / "synthetic.h5"
    with h5py.File(path, "w") as capture:
        capture.create_dataset("iq_data", data=raw)
    frequency, reference = robust_psd_from_iq(
        raw[:half * 2], 2400000, 1420405752, fft_size=fft_size
    )
    profile = {
        "metadata": {"fft_size": fft_size, "sample_rate_hz": 2400000, "center_frequency_hz": 1420405752},
        "frequency_hz": frequency, "reference_psd": reference,
        "reference_sigma": np.zeros(fft_size), "valid_mask": np.ones(fft_size, bool),
        "dc_mask": np.zeros(fft_size, bool), "spur_mask": np.zeros(fft_size, bool),
    }
    result = compute_native_waterfall(path, profile, time_bins=2)
    delta = result["fractional_excess"][1] - result["fractional_excess"][0]
    recovered = int(np.nanargmax(delta))
    expected = fft_size // 2 + tone_bin
    assert abs(recovered - expected) <= 1
    assert result["time_bin_mid_seconds"][1] > result["time_bin_mid_seconds"][0]


def test_json_npz_png_and_no_absolute_units(generated):
    output, document, _, _, _ = generated
    stored = json.loads((output / "quicklook_waterfall.json").read_text())
    required = {
        "schema_version", "status", "source_hdf5", "created_utc",
        "calibration_level", "absolute_calibration", "calibration_profile",
        "compatibility", "environment", "astronomical_interpretation",
        "center_frequency_hz", "sample_rate_hz", "gain_db",
        "capture_duration_seconds", "fft_size", "time_bins",
        "frequency_bins_native", "frequency_bins_dashboard", "time_axis_seconds",
        "frequency_mhz_dashboard", "waterfall_dashboard", "valid_fraction",
        "masked_fraction", "color_scale", "quicklook_metrics", "known_limitations",
    }
    assert required <= stored.keys()
    assert stored["calibration_level"] == "RELATIVE_INSTRUMENTAL"
    assert stored["absolute_calibration"] is False
    assert not ({"temperature_kelvin", "antenna_temperature_kelvin", "flux_jy"} & stored.keys())
    arrays = np.load(output / "quicklook_waterfall.npz")
    assert {"frequency_hz", "elapsed_seconds", "fractional_excess", "valid_mask", "fractional_uncertainty"} <= set(arrays.files)
    assert (output / "quicklook_waterfall.png").stat().st_size > 1000
    assert (output / "quicklook_waterfall_stability.png").stat().st_size > 1000
    assert len(document["waterfall_dashboard"]) == 16


def test_source_profile_immutability(generated):
    _, document, _, before, after = generated
    assert before == after
    assert document["integrity"]["unchanged"] is True


def test_cross_validation_is_consistent_with_full_spectrum(generated):
    _, document, native, _, _ = generated
    validation = validate_waterfall(document, native, SOURCE, PROFILE)
    assert validation["status"] == "PASS"
    assert validation["time_coverage_contiguous"] is True
    assert validation["time_coverage_complete"] is True
    assert validation["median_abs_difference"] <= validation["full_spectrum_robust_sigma"]


def test_deterministic_native_waterfall(generated):
    _, _, first, _, _ = generated
    from calibration_foundation import load_calibration_profile
    second = compute_native_waterfall(SOURCE, load_calibration_profile(PROFILE), time_bins=16)
    for key in ("frequency_hz", "fractional_excess", "valid_mask", "time_bin_mid_seconds"):
        assert np.array_equal(first[key], second[key], equal_nan=True)
