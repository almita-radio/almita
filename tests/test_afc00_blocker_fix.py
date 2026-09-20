import asyncio
import csv
import inspect
import json
from pathlib import Path

import h5py
import numpy as np

from calibration_foundation import check_calibration_compatibility, load_calibration_profile
from capture import (CaptureExecutor, INPUT_TOPOLOGIES, QUICKLOOK_SESSION_FIELDS,
                     write_quicklook_manifest_row)
from indi_telescope_control import INDITelescopeControl
from quicklook_live import QuicklookLive
from sdr_capture import write_hdf5_atomic


PROFILE = Path("data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1.npz")


def future_capture(path: Path, point: int) -> Path:
    rng = np.random.default_rng(100 + point)
    samples = 8192 * 8
    iq = np.clip(rng.normal(127.5 + point * .05, 2.0, samples * 2), 0, 255).astype(np.uint8)
    metadata = {
        "capture_start_utc": f"2026-08-27T12:00:0{point}+00:00",
        "created_at": f"2026-08-27T12:00:0{point}+00:00",
        "center_frequency_hz": 1420405752,
        "sample_rate_hz": 2400000,
        "gain": 40.2,
        "gain_requested_db": 40.2,
        "gain_mode": "manual",
        "instrument_topology": INPUT_TOPOLOGIES["antenna"],
        "rf_input": INPUT_TOPOLOGIES["antenna"],
        "bias_tee_enabled": True,
        "point_id": str(point),
        "ra_deg": 150.0 + point,
        "dec_deg": -30.0 + point,
        "coordinate_source": "COMMANDED",
        "duration_seconds": samples / 2400000,
        "requested_capture_duration_sec": samples / 2400000,
        "num_samples": samples,
        "capture_status": "success",
        "final_filename": path.name,
    }
    return write_hdf5_atomic(str(path), iq, metadata, samples)


def manifest_row(point, status, source=""):
    return {
        "point_id": str(point), "status": status, "source_hdf5": source,
        "ra_deg": 150.0 + point, "dec_deg": -30.0 + point,
        "coordinate_source": "COMMANDED", "frequency_hz": 1420405752,
        "sample_rate_hz": 2400000, "gain_requested_db": 40.2,
        "instrument_topology": INPUT_TOPOLOGIES["antenna"],
        "bias_tee_enabled": "true", "timestamp": "2026-08-27T12:00:00+00:00",
        "capture_duration_seconds": (8192 * 8) / 2400000,
    }


def test_capture_defaults_are_manual_fixed_gain_and_explicit_topology(tmp_path):
    executor = CaptureExecutor(str(tmp_path / "plan.csv"))
    assert executor.sdr_gain_db == 40.2
    assert executor.input_topology == INPUT_TOPOLOGIES["antenna"]
    assert executor.bias_tee_enabled is True
    source = inspect.getsource(CaptureExecutor.execute_observation_plan)
    assert "gain=self.sdr_gain_db" in source and "gain='auto'" not in source


def test_future_hdf5_and_manifest_contract(tmp_path):
    source = future_capture(tmp_path / "point_1.h5", 1)
    write_quicklook_manifest_row(tmp_path, manifest_row(1, "SUCCESS", source.name))
    write_quicklook_manifest_row(tmp_path, manifest_row(2, "FAILED"))
    write_quicklook_manifest_row(tmp_path, manifest_row(3, "DEFERRED"))
    with h5py.File(source) as capture:
        for field in ("gain_requested_db", "gain_mode", "instrument_topology",
                      "bias_tee_enabled", "point_id", "ra_deg", "dec_deg",
                      "coordinate_source", "center_frequency_hz", "sample_rate_hz"):
            assert field in capture.attrs
        assert capture.attrs["gain_requested_db"] == 40.2
        assert capture.attrs["gain_mode"] == "manual"
    with (tmp_path / "session.csv").open(newline="") as stream:
        reader = csv.DictReader(stream); rows = list(reader)
    assert tuple(reader.fieldnames) == QUICKLOOK_SESSION_FIELDS
    assert [row["status"] for row in rows] == ["SUCCESS", "FAILED", "DEFERRED"]
    assert not rows[0]["source_hdf5"].endswith(".part")
    profile = load_calibration_profile(PROFILE)
    assert check_calibration_compatibility(profile, source)["status"] == "COMPATIBLE"


def test_future_three_point_session_runs_real_quicklook(tmp_path):
    session = tmp_path / "SOFTWARE_VALIDATION_FIXTURE"
    output = tmp_path / "quicklook"
    for point in (1, 2, 3):
        source = future_capture(session / f"point_{point}.h5", point)
        write_quicklook_manifest_row(session, manifest_row(point, "SUCCESS", source.name))
    status = QuicklookLive(session, PROFILE, output).run(once=True)
    map_document = json.loads((output / "quicklook_map.json").read_text())
    assert status["points_processed"] == 3
    assert len(map_document["points"]) == 3
    assert {point["coordinate_source"] for point in map_document["points"]} == {"COMMANDED"}
    assert (output / "latest_spectrum.png").exists()
    assert (output / "latest_waterfall.png").exists()


def test_goto_keyboard_interrupt_returns_failure_and_cancel_propagates(monkeypatch):
    controller = INDITelescopeControl()
    controller.explain_indi = lambda *_: None
    controller.log = lambda *_args, **_kwargs: None
    controller.get_coordinates = lambda *args, **kwargs: asyncio.sleep(0, result=(1.0, 2.0))
    monkeypatch.setattr(controller, "_validated_goto_coordinates", lambda *_: (1.0, 2.0))
    monkeypatch.setattr(controller, "_wait_property", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    controller._send_command = lambda *_args, **_kwargs: asyncio.sleep(0)
    assert asyncio.run(controller.goto(1.0, 2.0)) is False

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError
    controller._wait_property = cancelled
    try:
        asyncio.run(controller.goto(1.0, 2.0))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("CancelledError must propagate unchanged")
