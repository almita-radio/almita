"""Field console watcher: a session stopped with STOP OBSERVATION is ABORTED, not "went silent" (first real STOP,
2026-10-13: capture.py leaves current_session.json at RUNNING when it is interrupted with SIGINT)."""
import almita_console_watcher as watcher

NOW = "2026-10-13T17:55:00+00:00"
SESSION = {"session_id": "20261013_174835", "session_name": "TEST-STOP", "state": "RUNNING", "points_total": 25,
           "points_success": 3, "point_current": 4, "updated_utc": "2026-10-13T17:50:25+00:00"}


def test_stale_running_session_with_an_operator_stop_recorded_is_aborted():
    for label in ("STOPPING", "ABORTED"):
        a = watcher.build_acquisition(NOW, SESSION, False, {"session_id": "20261013_174835", "orchestrator_state": label})
        assert a["state"] == "ABORTED" and "stopped by the operator" in a["error"] and a["points_success"] == 3


def test_without_a_recorded_stop_a_silent_session_is_still_degraded():
    assert watcher.build_acquisition(NOW, SESSION, False)["state"] == "DEGRADED"
    assert watcher.build_acquisition(NOW, SESSION, False, {"session_id": "20261013_174835", "orchestrator_state": "RUNNING"})["state"] == "DEGRADED"
    # a STOP recorded for ANOTHER session says nothing about this one
    other = watcher.build_acquisition(NOW, SESSION, False, {"session_id": "20261008_223519", "orchestrator_state": "ABORTED"})
    assert other["state"] == "DEGRADED" and "went silent" in other["error"]


def test_a_live_capture_is_never_relabelled():
    a = watcher.build_acquisition(NOW, SESSION, True, {"session_id": "20261013_174835", "orchestrator_state": "STOPPING"})
    assert a["state"] == "RUNNING"


def test_instrument_reports_one_minute_load_with_core_count():
    snapshot = {"system": {"cpu_percent": 12.5, "memory_percent": 25.0, "load_1m": 1.37}, "storage": {"percent": 18.0}}
    inst = watcher.build_instrument(100.0, watcher.WatcherState(), lambda: snapshot)
    assert inst["load_1m"] == 1.37 and inst["cpu_cores"] >= 1
    assert inst["cpu"] == 12.5  # aggregate over all cores, 0-100
