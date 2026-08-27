import json
import math

import h5py
import numpy as np

from capture import CaptureExecutor
from sdr_capture import read_hdf5_iq_components, write_hdf5_atomic
from temperature_sensors import DS18B20Reader, format_temperatures, temperature_metadata


def slave(tmp_path, sensor_id, temperature="32125", crc="YES"):
    path = tmp_path / sensor_id
    path.mkdir()
    (path / "w1_slave").write_text(
        f"aa bb cc dd ee ff gg hh ii : crc=ii {crc}\n"
        f"aa bb cc dd ee ff gg hh ii t={temperature}\n"
    )


def test_two_valid_sensors_and_fixed_role_assignment(tmp_path):
    slave(tmp_path, "28-cool", "21500")
    slave(tmp_path, "28-hot", "42750")
    reader = DS18B20Reader({"sdr": "28-hot", "lna": "28-cool"}, tmp_path)
    readings = reader.read_all()
    assert readings["sdr"]["sensor_id"] == "28-hot"
    assert readings["sdr"]["temperature_c"] == 42.75
    assert readings["lna"]["temperature_c"] == 21.5
    assert format_temperatures(readings, "pre") == "TEMP     PRE      SDR=42.8°C   LNA=21.5°C"


def test_crc_failure_is_nonfatal(tmp_path):
    slave(tmp_path, "28-bad", crc="NO")
    warnings = []
    result = DS18B20Reader({"sdr": "28-bad"}, tmp_path, warnings.append).read("sdr")
    assert not result["valid"] and result["temperature_c"] is None
    assert "CRC" in warnings[0]


def test_missing_sensor_is_nonfatal(tmp_path):
    warnings = []
    result = DS18B20Reader({"lna": "28-missing"}, tmp_path, warnings.append).read("lna")
    assert not result["valid"] and warnings


def test_invalid_temperature_is_nonfatal(tmp_path):
    slave(tmp_path, "28-bad", "not-a-number")
    assert not DS18B20Reader({"sdr": "28-bad"}, tmp_path).read("sdr")["valid"]


def test_repeated_85_warns(tmp_path):
    slave(tmp_path, "28-85", "85000")
    warnings = []
    reader = DS18B20Reader({"sdr": "28-85"}, tmp_path, warnings.append)
    assert reader.read("sdr")["valid"]
    assert reader.read("sdr")["valid"]
    assert len(warnings) == 1 and "repeatedly" in warnings[0]


def test_minus_127_is_invalid(tmp_path):
    slave(tmp_path, "28-neg", "-127000")
    assert not DS18B20Reader({"lna": "28-neg"}, tmp_path).read("lna")["valid"]


def test_hdf5_temperature_attributes_and_only_iq_dataset(tmp_path):
    base = {"sensor_id": "28-sdr", "temperature_c": 40.0, "valid": True, "timestamp_utc": "a"}
    pre = {"sdr": base, "lna": {**base, "sensor_id": "28-lna", "temperature_c": 20.0}}
    post = {"sdr": {**base, "temperature_c": 42.0, "timestamp_utc": "b"},
            "lna": {**pre["lna"], "valid": False, "temperature_c": None, "timestamp_utc": "b"}}
    metadata = {
        "center_frequency_hz": 1, "sample_rate_hz": 1, "num_samples": 2,
        "capture_status": "success", "final_filename": "x.h5", "created_at": "now",
        **temperature_metadata(pre, post),
    }
    path = tmp_path / "x.h5"
    write_hdf5_atomic(str(path), np.array([1, 2, 3, 4], dtype=np.uint8), metadata, 2)
    with h5py.File(path, "r") as capture_file:
        assert set(capture_file) == {"iq_data"}
        assert capture_file.attrs["temperature_sdr_mean_c"] == 41.0
        assert capture_file.attrs["temperature_lna_mean_c"] == 20.0
        assert capture_file.attrs["temperature_lna_valid_count"] == 1
        assert not capture_file.attrs["temperature_lna_valid_post"]


def test_historical_hdf5_without_temperatures_remains_readable(tmp_path):
    path = tmp_path / "old.h5"
    with h5py.File(path, "w") as capture_file:
        capture_file.create_dataset("iq_data", data=np.array([1, 2], dtype=np.uint8))
    with h5py.File(path, "r") as capture_file:
        i_data, q_data = read_hdf5_iq_components(capture_file)
        assert list(i_data) == [1] and list(q_data) == [2]
        assert capture_file.attrs.get("temperature_sdr_mean_c") is None


def test_executor_sensor_failure_does_not_raise(tmp_path):
    plan = tmp_path / "plan.csv"
    plan.write_text("point_number,capture_status\n1,planned\n")
    config = tmp_path / "observer_config.json"
    config.write_text(json.dumps({
        "observer": {"latitude_deg": 0, "longitude_deg": 0},
        "temperature_sensors": {"sdr": "28-missing-a", "lna": "28-missing-b"},
    }))
    executor = CaptureExecutor(str(plan))
    readings = executor.temperature_reader.read_all()
    assert not readings["sdr"]["valid"] and not readings["lna"]["valid"]
    metadata = temperature_metadata(readings, readings)
    assert metadata["temperature_sdr_valid_count"] == 0
    assert math.isnan(metadata["temperature_sdr_mean_c"])
