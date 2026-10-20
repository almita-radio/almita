"""A calibration profile can be invalidated by the operator (2026-10-17: a wizard "50 ohm" reference taken with the
antenna connected). The marker moves/edits nothing; every loader-based path refuses the profile afterwards."""
import json

import numpy as np
import pytest

import calibration_foundation as cf
import calibration_profile_catalog as catalog


def _profile(dirpath, name="calibration_profile_v1"):
    dirpath.mkdir(parents=True)
    stem = dirpath / name
    stem.with_suffix(".json").write_text(json.dumps({
        "schema_version": "1.0", "calibration_level": "RELATIVE_INSTRUMENTAL", "absolute_calibration": False,
        "temperature_kelvin": None, "antenna_temperature_kelvin": None, "flux_jy": None,
        "reference_topology": "AMBIENT_50R_AT_LNA_INPUT_WIZARD", "instrument_chain": "LNA_FILTER_CABLING_TO_RTL_SDR",
        "bias_t_state": "ON", "center_frequency_hz": 1420405752.0, "sample_rate_hz": 2400000.0, "gain_db": 40.2,
        "fft_size": 8192, "created_utc": "2026-10-17T06:47:00+00:00"}))
    np.savez(stem.with_suffix(".npz"), reference_psd=np.ones(8192), valid_mask=np.ones(8192, bool))
    return stem


def test_an_invalidated_profile_is_refused_with_its_reason_and_files_stay(tmp_path):
    stem = _profile(tmp_path / "WIZ" / "observe_profile")
    assert cf.load_calibration_profile(stem.with_suffix(".json"))["metadata"]["gain_db"] == 40.2
    marker = cf.invalidate_calibration_profile(stem.with_suffix(".npz"), "50 ohm reference taken with the antenna connected")
    assert marker.name == "calibration_profile_v1.INVALIDATED.json"
    with pytest.raises(cf.CalibrationProfileInvalidated, match="antenna connected"):
        cf.load_calibration_profile(stem)
    assert stem.with_suffix(".json").is_file() and stem.with_suffix(".npz").is_file()            # nothing moved
    marker.unlink()
    cf.load_calibration_profile(stem)                                                               # undo = delete marker


def test_the_catalog_lists_it_as_invalid_so_observe_never_proposes_it(tmp_path):
    root = tmp_path / "data" / "calibration"
    good = _profile(root / "WIZ-GOOD" / "observe_profile")
    bad = _profile(root / "WIZ-BAD" / "observe_profile")
    cf.invalidate_calibration_profile(bad, "no load")
    listing = catalog.list_profiles(root, tmp_path)
    by_path = {e["path"]: e for e in listing["profiles"]}
    assert by_path[str(good.with_suffix(".json").relative_to(tmp_path))]["valid"] is True
    entry = by_path[str(bad.with_suffix(".json").relative_to(tmp_path))]
    assert entry["valid"] is False and "INVALIDATED" in entry["error"] and "no load" in entry["error"]
    assert len(listing["profiles"]) == 2                                                            # the marker is not a profile


def test_invalidating_needs_a_real_profile_and_a_reason(tmp_path):
    with pytest.raises(FileNotFoundError):
        cf.invalidate_calibration_profile(tmp_path / "nothing", "x")
    stem = _profile(tmp_path / "P")
    with pytest.raises(ValueError):
        cf.invalidate_calibration_profile(stem, "  ")
