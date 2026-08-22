import csv
import json
import subprocess
import sys
import time
from pathlib import Path

import h5py
import numpy as np

from capture import CaptureExecutor
from sdr_capture import write_hdf5_atomic


FIELDS = [
    "point_number", "point_id", "scan_order", "actual_capture_order",
    "target_ra_hours", "target_dec_degrees", "target_ra_hms", "target_dec_dms",
    "capture_status", "start_time", "end_time", "duration", "error_message",
    "data_filename", "session_name",
]


class FakeSessionManager:
    def __init__(self):
        self.updates = []

    def update_session(self, session_id, **kwargs):
        self.updates.append((session_id, kwargs))


def make_executor(tmp_path, statuses):
    plan = tmp_path / "plan.csv"
    with plan.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for point, status in enumerate(statuses, 1):
            writer.writerow({
                "point_number": point, "point_id": point, "scan_order": point,
                "actual_capture_order": point if status != "planned" else "",
                "target_ra_hours": point, "target_dec_degrees": -30,
                "target_ra_hms": str(point), "target_dec_dms": "-30",
                "capture_status": status, "data_filename": f"point_{point}.dat",
                "session_name": "resume_test",
            })
    (tmp_path / "observer_config.json").write_text(json.dumps({
        "observer": {"latitude_deg": -33.4, "longitude_deg": -70.6, "elevation_m": 500}
    }))
    executor = CaptureExecutor(str(plan), session_id="session-1", config_path="observer_config.json")
    executor.session_manager = FakeSessionManager()
    executor.hour_angle_for_point = lambda point, obstime=None: -1.0
    return executor


def capture_dir(tmp_path):
    directory = tmp_path / "data" / "iq" / "resume_test-previous-process"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_valid(directory, point, order=None, point_id=None):
    final = directory / f"point_{point}.h5"
    attrs = {
        "center_frequency_hz": 1420405752, "sample_rate_hz": 2400000,
        "num_samples": 2, "capture_status": "success",
        "final_filename": final.name, "created_at": "2026-08-20T00:00:00+00:00",
        "point_id": point if point_id is None else point_id,
        "session_id": "session-1", "scan_order": point,
        "actual_capture_order": point if order is None else order,
    }
    write_hdf5_atomic(str(final), np.array([1, 2, 3, 4], dtype=np.uint8), attrs, 2)
    return final


def read_rows(executor):
    with executor.csv_path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def test_state_matrix_and_actual_order_recovery(tmp_path):
    executor = make_executor(tmp_path, ["success", "success", "capturing", "capturing", "planned", "failed"])
    directory = capture_dir(tmp_path)
    write_valid(directory, 1, order=2)
    write_valid(directory, 3, order=7)
    write_valid(directory, 5, order=5)
    (directory / "point_4.h5.part").write_bytes(b"incomplete")

    assert executor.load_observation_plan(resume=True)
    rows = read_rows(executor)
    assert [row["capture_status"] for row in rows] == [
        "success", "planned", "success", "failed", "success", "failed"
    ]
    assert rows[1]["resume_reconcile_reason"] == "success_without_valid_hdf5"
    assert rows[2]["resume_reconcile_reason"] == "valid_hdf5_after_interrupted_status_update"
    assert not (directory / "point_4.h5.part").exists()
    assert rows[3]["error_code"] == "interrupted_capture"
    assert [int(point["point_number"]) for point in executor.observation_points] == [2]
    assert executor.actual_capture_order_offset == 7
    assert executor.session_manager.updates[-1][1]["points_completed"] == 3


def test_invalid_and_wrong_identity_finals_are_quarantined(tmp_path):
    executor = make_executor(tmp_path, ["success", "planned"])
    directory = capture_dir(tmp_path)
    (directory / "point_1.h5").write_bytes(b"not hdf5")
    write_valid(directory, 2, point_id=999)

    assert executor.load_observation_plan(resume=True)
    rows = read_rows(executor)
    assert [row["capture_status"] for row in rows] == ["planned", "planned"]
    assert not (directory / "point_1.h5").exists()
    assert not (directory / "point_2.h5").exists()
    assert len(list(directory.glob("*.invalid-*"))) == 2


def test_valid_final_is_authoritative_over_failed_csv_status(tmp_path):
    executor = make_executor(tmp_path, ["failed"])
    directory = capture_dir(tmp_path)
    write_valid(directory, 1, order=1)

    assert not executor.load_observation_plan(resume=True)
    row = read_rows(executor)[0]
    assert row["capture_status"] == "success"
    assert row["resume_reconcile_reason"] == "valid_hdf5_after_failed_status_update"


def test_orphan_part_is_reported_and_preserved(tmp_path, capsys):
    executor = make_executor(tmp_path, ["capturing"])
    directory = capture_dir(tmp_path)
    orphan = directory / "unknown_point.h5.part"
    orphan.write_bytes(b"evidence")

    assert not executor.load_observation_plan(resume=True)
    assert orphan.exists()
    assert "Orphan partial preserved" in capsys.readouterr().out


def test_reconciliation_never_calls_mount_or_tracking(tmp_path):
    executor = make_executor(tmp_path, ["capturing"])

    class ForbiddenHardware:
        def __getattr__(self, name):
            raise AssertionError(f"hardware API accessed: {name}")

    executor.telescope = ForbiddenHardware()
    assert not executor.load_observation_plan(resume=True)


def test_five_point_kill_restart_artifact_scenarios(tmp_path):
    executor = make_executor(tmp_path, ["success", "capturing", "capturing", "success", "planned"])
    directory = capture_dir(tmp_path)
    write_valid(directory, 1, order=1)  # completed before interruption
    (directory / "point_2.h5.part").write_bytes(b"killed during part")
    write_valid(directory, 3, order=3)  # killed after rename, before CSV status
    write_valid(directory, 4, order=4)  # killed after CSV success

    assert executor.load_observation_plan(resume=True)
    rows = read_rows(executor)
    assert [row["capture_status"] for row in rows] == [
        "success", "failed", "success", "success", "planned"
    ]
    assert rows[1]["resume_reconcile_reason"] == "interrupted_capture"
    assert [int(point["point_number"]) for point in executor.observation_points] == [5]
    assert not (directory / "point_2.h5.part").exists()
    assert executor.actual_capture_order_offset == 4


def test_real_sigkill_during_part_is_recovered(tmp_path):
    executor = make_executor(tmp_path, ["capturing"])
    directory = capture_dir(tmp_path)
    part = directory / "point_1.h5.part"
    ready = tmp_path / "writer.ready"
    program = (
        "import h5py,sys,time;"
        "f=h5py.File(sys.argv[1],'w');"
        "f.create_dataset('iq_data',data=[1,2,3,4]);f.flush();"
        "open(sys.argv[2],'w').close();time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", program, str(part), str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists() and part.exists()
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert not executor.load_observation_plan(resume=True)
    assert read_rows(executor)[0]["capture_status"] == "failed"
    assert not part.exists()
