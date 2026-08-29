import contextlib
import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import almita_console_watcher as watcher
import almita_console_server as server_module
import capture as capture_module
import quicklook_live as quicklook_module
from runtime_state import announce_session, atomic_write_json, read_json_safe, utcnow
from serve_dashboard import make_server

ROOT = Path(__file__).parent.resolve()
CONSOLE = ROOT / "console"


def fake_telemetry(**overrides):
    base = {
        "status": "OK", "created_utc": utcnow(),
        "system": {"cpu_percent": 12.0, "memory_percent": 34.0},
        "storage": {"percent": 56.0},
        "network": {"interface": "eth0", "rx_bytes": 1, "tx_bytes": 2},
        "sdr": {"rtl_tcp_process_detected": True, "rtl_tcp_port_listening": True},
        "temperatures": {"sdr_c": 25.0, "lna_c": 24.0},
        "mount": {"status": "NOT_EXPOSED"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 1-14: watcher logic


def test_1_watcher_without_session_is_idle(tmp_path):
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=False)
    assert status["acquisition"]["state"] == "IDLE"
    assert status["quicklook"]["state"] == "IDLE"


def test_2_session_started_is_starting_or_running(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="STARTING",
                      session_name="demo", points_total=3, points_success=0, points_failed=0)
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["acquisition"]["state"] in ("STARTING", "RUNNING")
    assert status["acquisition"]["session_id"] == "s1"


def test_3_point_started_sets_current_point(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="STARTING", session_name="demo")
    announce_session(tmp_path, session_id="s1", event="POINT_STARTED", state="RUNNING",
                      point_current=2, current_point_id="p002")
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["acquisition"]["point_current"] == 2
    assert status["acquisition"]["current_point_id"] == "p002"
    assert status["acquisition"]["state"] == "RUNNING"


def test_4_point_completed_updates_counters(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="STARTING",
                      points_success=0, points_failed=0)
    announce_session(tmp_path, session_id="s1", event="POINT_COMPLETED", state="RUNNING",
                      points_success=1, last_successful_point_id="p001")
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["acquisition"]["points_success"] == 1
    assert status["acquisition"]["last_successful_point_id"] == "p001"


def test_5_session_completed_persists_final_state(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="STARTING", session_name="demo")
    announce_session(tmp_path, session_id="s1", event="SESSION_COMPLETED", state="COMPLETED",
                      points_success=9, points_total=9)
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=False)
    assert status["acquisition"]["state"] == "COMPLETED"
    assert status["last_session"]["final_state"] == "COMPLETED"
    assert status["last_session"]["points_success"] == 9
    assert read_json_safe(tmp_path / "last_session.json")["session_id"] == "s1"


def test_6_session_aborted(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_ABORTED", state="ABORTED",
                      error="interrupted by user (KeyboardInterrupt)")
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=False)
    assert status["acquisition"]["state"] == "ABORTED"
    assert "interrupted" in status["acquisition"]["error"]


def test_7_capture_disappears_during_running_is_degraded(tmp_path):
    old = "2000-01-01T00:00:00+00:00"
    atomic_write_json(tmp_path / "current_session.json", {
        "schema_version": 1, "session_id": "s1", "session_name": "demo", "state": "RUNNING",
        "updated_utc": old, "points_total": 9, "points_success": 3, "points_failed": 0,
        "current_point_id": "p003",
    })
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=False)
    assert status["acquisition"]["state"] == "DEGRADED"
    assert status["acquisition"]["session_id"] == "s1"
    assert status["acquisition"]["last_successful_point_id"] is None or status["acquisition"]["points_success"] == 3
    assert status["acquisition"]["acquisition_stale"] is True


def test_8_quicklook_lag_does_not_fail_acquisition(tmp_path):
    now = utcnow()
    atomic_write_json(tmp_path / "current_session.json", {
        "schema_version": 1, "session_id": "s1", "session_name": "demo", "state": "RUNNING",
        "updated_utc": now, "points_total": 9, "points_success": 5, "points_failed": 0,
    })
    quicklook_dir = tmp_path / "quicklook_out"
    quicklook_dir.mkdir()
    atomic_write_json(tmp_path / "quicklook_announcement.json",
                       {"schema_version": 1, "session_id": "s1", "quicklook_root": str(quicklook_dir)})
    atomic_write_json(quicklook_dir / "quicklook_live_status.json",
                       {"status": "OK", "points_processed": 2, "updated_utc": "2000-01-01T00:00:00+00:00"})
    status = watcher.build_status(now, 100.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["acquisition"]["state"] == "RUNNING"
    assert status["quicklook"]["quicklook_stale"] is True


def test_9_telemetry_stale_only_affects_telemetry(tmp_path):
    now = utcnow()
    atomic_write_json(tmp_path / "current_session.json", {
        "schema_version": 1, "session_id": "s1", "session_name": "demo", "state": "RUNNING",
        "updated_utc": now, "points_total": 9, "points_success": 1, "points_failed": 0,
    })
    state = watcher.WatcherState()

    def failing_collect():
        raise RuntimeError("telemetry unavailable")

    status = watcher.build_status(now, 9999.0, state, tmp_path, failing_collect, capture_process_detected=True)
    assert status["instrument"]["telemetry_stale"] is True
    assert status["acquisition"]["state"] == "RUNNING"
    assert status["acquisition"]["acquisition_stale"] is False


def test_10_restart_preserves_last_session(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_COMPLETED", state="COMPLETED",
                      points_success=4, points_total=4)
    watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                          lambda: fake_telemetry(), capture_process_detected=False)
    # Simulate a fresh watcher process (new in-memory state) after a restart,
    # with current_session.json now absent (as if a lot of time has passed).
    (tmp_path / "current_session.json").unlink()
    fresh_state = watcher.WatcherState()
    fresh_state.last_session_archive = read_json_safe(tmp_path / "last_session.json")
    status = watcher.build_status(utcnow(), 0.0, fresh_state, tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=False)
    assert status["acquisition"]["state"] == "IDLE"
    assert status["last_session"]["session_id"] == "s1"
    assert status["last_session"]["final_state"] == "COMPLETED"


def test_11_current_session_json_atomicity(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="STARTING")
    path = tmp_path / "current_session.json"
    assert path.exists()
    assert not path.with_name(path.name + ".tmp").exists()
    assert json.loads(path.read_text())["session_id"] == "s1"


def test_12_almita_status_json_atomicity(tmp_path):
    watcher.tick(tmp_path, watcher.WatcherState(), collect_fn=lambda: fake_telemetry())
    path = tmp_path / "almita_status.json"
    assert path.exists()
    assert not path.with_name(path.name + ".tmp").exists()
    assert json.loads(path.read_text())["schema_version"] == 1


def test_13_malformed_runtime_json_does_not_crash(tmp_path):
    (tmp_path / "current_session.json").write_text("{not valid json")
    (tmp_path / "quicklook_announcement.json").write_text("[]")
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=False)
    assert status["acquisition"]["state"] == "IDLE"
    assert status["quicklook"]["state"] == "IDLE"


def test_14_missing_quicklook_is_waiting_not_crash(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["quicklook"]["state"] == "WAITING"
    assert status["quicklook"]["spectrum_available"] is False


# ---------------------------------------------------------------- 15-19: server


def test_15_server_binds_0000_8088_by_default():
    source = (ROOT / "almita_console_server.py").read_text()
    assert '"--bind", default="0.0.0.0"' in source
    assert '"--port", type=int, default=8088' in source


@contextlib.contextmanager
def running(root):
    server = make_server(root, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown(); server.server_close(); thread.join()


def console_root(tmp_path, status=None):
    public = tmp_path / "public"
    public.mkdir()
    for name in ("index.html", "styles.css", "app.js"):
        (public / name).write_text((CONSOLE / name).read_text())
    runtime = public / "runtime"
    runtime.mkdir()
    if status is not None:
        atomic_write_json(runtime / "almita_status.json", status)
    return public


def test_16_get_root_200(tmp_path):
    with running(console_root(tmp_path)) as base:
        with urllib.request.urlopen(base + "/") as r:
            assert r.status == 200 and r.headers.get_content_type() == "text/html"


def test_17_get_almita_status_json_200(tmp_path):
    with running(console_root(tmp_path, status={"schema_version": 1})) as base:
        with urllib.request.urlopen(base + "/runtime/almita_status.json") as r:
            assert r.status == 200


def test_18_write_methods_405(tmp_path):
    with running(console_root(tmp_path)) as base:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            try:
                urllib.request.urlopen(urllib.request.Request(base + "/", method=method, data=b"x"))
                assert False, method
            except urllib.error.HTTPError as e:
                assert e.code == 405


def test_19_path_traversal_rejected(tmp_path):
    with running(console_root(tmp_path)) as base:
        for path in ("/../etc/passwd", "/missing.json"):
            try:
                urllib.request.urlopen(base + path)
                assert False, path
            except urllib.error.HTTPError as e:
                assert e.code == 404


# ---------------------------------------------------------------- 20-23: frontend rendering


def status_fixture(acquisition_state, **overrides):
    base = {
        "schema_version": 1, "updated_utc": utcnow(), "system_state": "READY",
        "instrument": {"cpu": 10.0, "ram": 20.0, "disk": 30.0, "network_interfaces": {"interface": "eth0"},
                        "rtl_tcp_process": True, "rtl_tcp_listening": True, "sdr_temperature_c": 25.0,
                        "lna_temperature_c": 24.0, "mount_state": "NOT_EXPOSED", "telemetry_stale": False,
                        "error": None},
        "acquisition": {"state": acquisition_state, "session_id": "s1" if acquisition_state != "IDLE" else None,
                         "session_name": "demo", "point_current": 3, "points_total": 9, "points_success": 2,
                         "points_failed": 0, "points_deferred": 0, "current_point_id": "p003",
                         "last_successful_point_id": "p002", "last_capture_utc": utcnow(),
                         "capture_process_detected": True, "acquisition_stale": False, "error": None},
        "quicklook": {"state": "OK" if acquisition_state == "RUNNING" else "IDLE", "points_processed": 2,
                      "last_product_utc": utcnow(), "spectrum_available": True, "waterfall_available": False,
                      "map_available": False, "quicklook_stale": False, "error": None},
        "last_session": None,
    }
    base.update(overrides)
    return base


def dom(root, query="/?snapshot=1"):
    with running(root) as base:
        run = subprocess.run(["chromium", "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                               "--virtual-time-budget=1200", "--dump-dom", base + query],
                              capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_20_frontend_idle_render(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture("IDLE")))
    assert ">IDLE<" in html and "NO ACTIVE SESSION" in html


def test_21_frontend_running_render(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))
    assert ">RUNNING<" in html and "p003" in html and "demo" in html


def test_22_frontend_degraded_render(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture(
        "DEGRADED", system_state="DEGRADED",
        acquisition={"state": "DEGRADED", "session_id": "s1", "session_name": "demo", "point_current": 5,
                     "points_total": 9, "points_success": 5, "points_failed": 0, "points_deferred": 0,
                     "current_point_id": "p005", "last_successful_point_id": "p005", "last_capture_utc": utcnow(),
                     "capture_process_detected": False, "acquisition_stale": True,
                     "error": "capture process not detected"})))
    assert ">DEGRADED<" in html and "capture process not detected" in html


def test_23_frontend_completed_render(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture(
        "COMPLETED",
        acquisition={"state": "COMPLETED", "session_id": "s1", "session_name": "demo", "point_current": 9,
                     "points_total": 9, "points_success": 9, "points_failed": 0, "points_deferred": 0,
                     "current_point_id": None, "last_successful_point_id": "p009", "last_capture_utc": utcnow(),
                     "capture_process_detected": False, "acquisition_stale": True, "error": None},
        last_session={"session_id": "s1", "session_name": "demo", "final_state": "COMPLETED",
                      "completed_utc": utcnow(), "points_success": 9, "points_total": 9})))
    assert ">COMPLETED<" in html and "LAST KNOWN SESSION" in html


# ---------------------------------------------------------------- closeout: canonical runtime_dir


def test_capture_executor_without_runtime_dir_does_not_write_to_repo(tmp_path, monkeypatch):
    from test_capture_preflight import make_executor
    monkeypatch.chdir(tmp_path)
    ex = make_executor(tmp_path)
    assert ex.runtime_dir is None
    ex._announce(event="SESSION_STARTED", state="STARTING")  # must be a true no-op
    assert not (Path(capture_module.DEFAULT_RUNTIME_DIR) / "current_session.json").exists()


def test_quicklooklive_without_runtime_dir_does_not_write_to_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    live = object.__new__(quicklook_module.QuicklookLive)
    live.runtime_dir = None
    live.session_id = "s1"
    live.output = tmp_path / "out"
    live.output.mkdir()
    live._announce()  # must be a true no-op
    assert not (Path(quicklook_module.DEFAULT_RUNTIME_DIR) / "quicklook_announcement.json").exists()


def test_capture_cli_runtime_dir_defaults_to_canonical_constant():
    source = (ROOT / "capture.py").read_text()
    assert "default=DEFAULT_RUNTIME_DIR" in source
    assert capture_module.DEFAULT_RUNTIME_DIR == str(ROOT / "data" / "runtime")
    assert Path(capture_module.DEFAULT_RUNTIME_DIR).is_absolute()


def test_quicklook_cli_runtime_dir_defaults_to_canonical_constant():
    source = (ROOT / "quicklook_live.py").read_text()
    assert "default=DEFAULT_RUNTIME_DIR" in source
    assert quicklook_module.DEFAULT_RUNTIME_DIR == str(ROOT / "data" / "runtime")


def test_capture_and_quicklook_announce_to_the_same_default_runtime_dir():
    assert capture_module.DEFAULT_RUNTIME_DIR == quicklook_module.DEFAULT_RUNTIME_DIR


def test_console_watcher_and_server_also_default_to_the_same_canonical_runtime_dir():
    assert str(watcher.ROOT / "data" / "runtime") == capture_module.DEFAULT_RUNTIME_DIR
    assert str(server_module.ROOT / "data" / "runtime") == capture_module.DEFAULT_RUNTIME_DIR


def test_capture_executor_explicit_runtime_dir_override_still_works(tmp_path):
    from test_capture_preflight import make_executor
    from capture import CaptureExecutor
    make_executor(tmp_path)  # writes plan.csv + observer_config.json into tmp_path
    custom = tmp_path / "custom_runtime"
    ex = CaptureExecutor(str(tmp_path / "plan.csv"), config_path="observer_config.json",
                          runtime_dir=str(custom))
    assert ex.runtime_dir == custom
    ex.session_id = "override-test"
    ex._announce(event="SESSION_STARTED", state="STARTING")
    assert (custom / "current_session.json").is_file()
    assert json.loads((custom / "current_session.json").read_text())["session_id"] == "override-test"


# ---------------------------------------------------------------- offline-first audit


def test_offline_first_no_cdn_or_external_calls():
    for name in ("index.html", "styles.css", "app.js"):
        text = (CONSOLE / name).read_text()
        assert "http://" not in text and "https://" not in text
        assert "cdn" not in text.lower() and "googleapis" not in text.lower()
    server_source = (ROOT / "almita_console_server.py").read_text()
    watcher_source = (ROOT / "almita_console_watcher.py").read_text()
    for source in (server_source, watcher_source):
        assert "requests" not in source
        assert "urllib.request.urlopen(\"http" not in source
