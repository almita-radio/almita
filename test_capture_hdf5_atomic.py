import json
from pathlib import Path

import h5py
import numpy as np
import pytest

import sdr_capture
from capture import CaptureExecutor


def metadata():
    return {
        "center_frequency_hz": 1420405752,
        "sample_rate_hz": 2400000,
        "num_samples": 2,
        "capture_status": "success",
        "final_filename": "point.h5",
        "created_at": "2026-08-20T00:00:00+00:00",
        "scan_order": 7,
        "actual_capture_order": 4,
        "target_ra_hours": 12.5,
        "target_dec_deg": -33.2,
        "mount_start_ra_hours": 1.0,
        "mount_start_dec_deg": 2.0,
        "mount_capture_ra_hours": 12.49,
        "mount_capture_dec_deg": -33.19,
        "altitude_deg_at_goto": 45.0,
        "azimuth_deg_at_goto": 120.0,
        "ha_hours_at_goto": -0.2,
        "tracking_requested": True,
        "tracking_confirmed": True,
        "tracking_state_at_capture": "on",
        "goto_started_at": "2026-08-20T00:00:00+00:00",
        "capture_started_at": "2026-08-20T00:00:05+00:00",
        "beam_fwhm_deg": 17.25,
        "beam_sampling_fraction": 0.41,
        "nominal_spacing_deg": 7.0725,
    }


def test_atomic_success_and_complete_metadata(tmp_path):
    final = tmp_path / "point.h5"
    result = sdr_capture.write_hdf5_atomic(
        str(final), np.array([1, 2, 3, 4], dtype=np.uint8), metadata(), 2
    )
    assert result == final and final.exists()
    assert not Path(f"{final}.part").exists()
    with h5py.File(final, "r") as capture_file:
        assert capture_file["iq_data"].shape == (4,)
        for key, value in metadata().items():
            assert key in capture_file.attrs
            if isinstance(value, float):
                assert capture_file.attrs[key] == pytest.approx(value)
        assert "hdf5_completed_at" in capture_file.attrs


def test_final_absent_during_validation_and_same_directory_rename(monkeypatch, tmp_path):
    final = tmp_path / "point.h5"
    real_validate = sdr_capture._validate_hdf5_capture
    real_replace = sdr_capture.os.replace
    observed = {}

    def validate(part, expected):
        assert part.exists() and not final.exists()
        real_validate(part, expected)

    def replace(source, destination):
        observed["paths"] = (Path(source), Path(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(sdr_capture, "_validate_hdf5_capture", validate)
    monkeypatch.setattr(sdr_capture.os, "replace", replace)
    sdr_capture.write_hdf5_atomic(
        str(final), np.array([1, 2, 3, 4], dtype=np.uint8), metadata(), 2
    )
    source, destination = observed["paths"]
    assert source.parent == destination.parent == tmp_path


@pytest.mark.parametrize("failure", ["iq", "metadata", "validation"])
def test_failures_leave_neither_final_nor_part(monkeypatch, tmp_path, failure):
    final = tmp_path / "point.h5"
    iq = np.array([1, 2, 3, 4], dtype=np.uint8)
    attrs = metadata()
    if failure == "iq":
        iq = np.array([object(), object()], dtype=object)
    elif failure == "metadata":
        attrs["invalid_metadata"] = object()
    else:
        monkeypatch.setattr(
            sdr_capture, "_validate_hdf5_capture",
            lambda *_: (_ for _ in ()).throw(ValueError("injected before rename")),
        )
    with pytest.raises(Exception):
        sdr_capture.write_hdf5_atomic(str(final), iq, attrs, 2)
    assert not final.exists()
    assert not Path(f"{final}.part").exists()


def test_beam_values_are_loaded_from_grid_metadata(tmp_path):
    plan = tmp_path / "plan.csv"
    plan.write_text("point_number,capture_status\n1,planned\n")
    (tmp_path / "observer_config.json").write_text(
        json.dumps({"observer": {"latitude_deg": 0, "longitude_deg": 0}})
    )
    expected = {
        "beam_fwhm_deg": 17.25,
        "beam_sampling_fraction": 0.41,
        "nominal_spacing_deg": 7.0725,
    }
    (tmp_path / "grid_metadata.json").write_text(json.dumps({"grid": expected}))
    executor = CaptureExecutor(str(plan), config_path="observer_config.json")
    assert executor.grid_metadata == expected
