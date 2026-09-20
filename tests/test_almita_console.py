import contextlib
import json
import re
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import almita_console_watcher as watcher
import almita_console_server as server_module
import capture as capture_module
import quicklook_live as quicklook_module
import wifi_health
from runtime_state import announce_session, atomic_write_json, read_json_safe, utcnow
from serve_dashboard import make_server

ROOT = Path(__file__).parent.parent.resolve()
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


def test_4b_started_utc_passed_through_for_client_side_elapsed_estimate(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="STARTING",
                      started_utc="2026-09-02T18:00:00+00:00", points_total=9)
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["acquisition"]["started_utc"] == "2026-09-02T18:00:00+00:00"


def test_4c_settle_and_capture_seconds_default_to_none_until_capture_announces_them(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="STARTING")
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["acquisition"]["settle_seconds"] is None
    assert status["acquisition"]["capture_seconds"] is None


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


def test_15b_prepare_console_web_cache_busts_static_assets(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("index.html", "styles.css", "app.js", "spectral_stack_3d.js"):
        (source / name).write_text((CONSOLE / name).read_text())
    public = server_module.prepare_console_web(source, tmp_path / "runtime", tmp_path / "public1")
    html = (public / "index.html").read_text()
    assert 'href="styles.css?v=' in html
    assert 'src="app.js?v=' in html
    assert 'src="spectral_stack_3d.js?v=' in html

    # Changing app.js's content changes its cache-busting version, so a
    # browser that cached the old URL is forced to fetch the new one.
    (source / "app.js").write_text((source / "app.js").read_text() + "\n// changed\n")
    public2 = server_module.prepare_console_web(source, tmp_path / "runtime", tmp_path / "public2")
    html2 = (public2 / "index.html").read_text()
    old_app_version = html.split('src="app.js?v=')[1].split('"')[0]
    new_app_version = html2.split('src="app.js?v=')[1].split('"')[0]
    assert old_app_version != new_app_version
    # styles.css was untouched, so its own version stays stable.
    old_css_version = html.split('href="styles.css?v=')[1].split('"')[0]
    new_css_version = html2.split('href="styles.css?v=')[1].split('"')[0]
    assert old_css_version == new_css_version


def test_15c_prepare_console_web_symlinks_vendor_directory(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("index.html", "styles.css", "app.js", "spectral_stack_3d.js"):
        (source / name).write_text((CONSOLE / name).read_text())
    vendor = source / "vendor" / "three"
    vendor.mkdir(parents=True)
    (vendor / "three.min.js").write_text("/* fake three.js for this test */")
    public = server_module.prepare_console_web(source, tmp_path / "runtime", tmp_path / "public")
    link = public / "vendor"
    assert link.is_symlink()
    assert (link / "three" / "three.min.js").read_text() == "/* fake three.js for this test */"


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
    for name in ("index.html", "styles.css", "app.js", "spectral_stack_3d.js"):
        (public / name).write_text((CONSOLE / name).read_text())
    vendor = public / "vendor" / "three"
    vendor.mkdir(parents=True)
    for name in ("three.min.js", "OrbitControls.js"):
        # Symlinked, not copied: these are large vendored (non-CDN) files
        # unrelated to this test's content and would otherwise add real I/O
        # cost across dozens of tests that call this helper.
        (vendor / name).symlink_to(CONSOLE / "vendor" / "three" / name)
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


def test_17b_quicklook_product_symlink_is_servable_read_only(tmp_path):
    """§7: the watcher's runtime/quicklook_products symlink must be
    transparently browsable through the existing read-only server, with no
    new route and no copying - it's just another file under runtime/."""
    root = console_root(tmp_path, status={"schema_version": 1})
    products_source = tmp_path / "real_quicklook_output"
    products_source.mkdir()
    (products_source / "latest_spectrum.png").write_bytes(b"\x89PNG-fixture")
    (root / "runtime" / "quicklook_products").symlink_to(products_source, target_is_directory=True)
    with running(root) as base:
        with urllib.request.urlopen(base + "/runtime/quicklook_products/latest_spectrum.png") as r:
            assert r.status == 200
            assert r.read() == b"\x89PNG-fixture"


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


def test_21c_frontend_session_panel_shows_elapsed_and_no_standalone_success_line(tmp_path):
    started = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    html = dom(console_root(tmp_path, status=status_fixture(
        "RUNNING", acquisition={**status_fixture("RUNNING")["acquisition"], "started_utc": started})))
    assert "ELAPSED / TOTAL / REMAINING" in html
    assert "SETTLE / CAPTURE" in html
    assert "<dt>SUCCESS</dt>" not in html


def test_21d_frontend_rfi_last_update_shows_time_only(tmp_path):
    # The RFI REFERENCE panel's own "LAST UPDATE" row is trimmed to HH:MM:SS;
    # the unrelated ANTENNA B products caption ("updated ...") intentionally
    # still shows the full timestamp, so this only checks the KV row itself.
    html = dom(console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={**_rfi_ref_running(), "last_update_utc": "2026-09-02T18:23:45.123456+00:00"})))
    assert "<dt>LAST UPDATE</dt><dd>18:23:45</dd>" in html


def test_21e_frontend_topbar_updated_shows_time_only(tmp_path):
    html = dom(console_root(tmp_path, status={
        **status_fixture("RUNNING"), "updated_utc": "2026-09-02T19:05:12.654321+00:00"}))
    assert '<dd id="updated">19:05:12</dd>' in html


def test_21f_frontend_activity_log_hidden_when_unavailable(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))
    assert 'id="activity-log-panel" class="panel compact" hidden' in html \
        or 'activity-log-panel" hidden' in html


def test_21g_frontend_activity_log_shows_lines_with_coloring(tmp_path):
    html = dom(console_root(tmp_path, status={
        **status_fixture("RUNNING"),
        "activity_log": {"available": True, "lines": [
            "POINT 003/010  HA=-1.20h",
            "✓ POINT 003/010   total=12.3s",
            "SESSION  elapsed=00:01:02   remaining≈00:05:00",
        ]},
    }))
    assert 'activity-log-panel" hidden' not in html
    assert '<span class="log-point">POINT 003/010  HA=-1.20h</span>' in html
    assert '<span class="log-ok">✓ POINT 003/010   total=12.3s</span>' in html
    assert '<span class="log-session">SESSION  elapsed=00:01:02   remaining≈00:05:00</span>' in html


def test_21h_frontend_activity_log_appends_hms_to_session_summary_lines(tmp_path):
    """The session-summary block (GOTO/Motion/Capture/Disk, printed verbatim
    by capture.py) gets an HH:MM:SS reading appended alongside the original
    seconds value - generic over the label, not hardcoded per line."""
    html = dom(console_root(tmp_path, status={
        **status_fixture("COMPLETED"),
        "activity_log": {"available": True, "lines": [
            "GOTO         866.1s",
            "Motion       753.2s",
            "Capture      102.3s",
            "Disk         28.7s",
        ]},
    }))
    assert "GOTO         866.1s   (00:14:26)" in html
    assert "Motion       753.2s   (00:12:33)" in html
    assert "Capture      102.3s   (00:01:42)" in html
    assert "Disk         28.7s   (00:00:29)" in html


def test_21i_frontend_activity_log_hms_accumulates_past_24_hours(tmp_path):
    html = dom(console_root(tmp_path, status={
        **status_fixture("COMPLETED"),
        "activity_log": {"available": True, "lines": ["GOTO         98104.0s"]},
    }))
    assert "(27:15:04)" in html  # hours accumulate, never wrap at 24


def test_21j_frontend_activity_log_zero_and_non_summary_lines_unaffected(tmp_path):
    html = dom(console_root(tmp_path, status={
        **status_fixture("RUNNING"),
        "activity_log": {"available": True, "lines": [
            "GOTO         0.0s",
            "MOUNT    GOTO      OK       7.6s   motion=6.5s   wait=1.1s",
            "POINT 003/010  HA=-1.20h",
        ]},
    }))
    assert "GOTO         0.0s   (00:00:00)" in html
    # A detail line that merely mentions seconds mid-line must never be
    # mistaken for the whole-session summary and get an HMS appended.
    assert "motion=6.5s   wait=1.1s   (" not in html
    assert "HA=-1.20h   (" not in html


def test_21b_frontend_shows_spectrum_thumbnail_when_available(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))
    assert 'id="quicklook-thumbs"' in html and "quicklook-thumbs\" hidden" not in html
    assert "quicklook_products/latest_spectrum.png" in html
    assert 'id="thumb-waterfall" alt="Waterfall" hidden' in html or "thumb-waterfall\" hidden" in html


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


def test_24_frontend_shows_mount_device_when_announced_by_capture(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture(
        "RUNNING", instrument={"cpu": 10.0, "ram": 20.0, "disk": 30.0, "network_interfaces": {"interface": "eth0"},
                                "rtl_tcp_process": True, "rtl_tcp_listening": True, "sdr_temperature_c": 25.0,
                                "lna_temperature_c": 24.0, "mount_state": "NOT_EXPOSED",
                                "mount_device": "LX200 OnStep", "telemetry_stale": False, "error": None})))
    assert "LX200 OnStep" in html


def test_24b_frontend_shows_na_not_zero_for_invalid_temperature(tmp_path):
    """The frontend already tolerates a null temperature (rejected as a
    glitch upstream - see temperature_sensors.py/telemetry_summary.py's
    plausibility guard) as N/A - confirmed unchanged, no frontend edit was
    needed for that fix."""
    html = dom(console_root(tmp_path, status=status_fixture(
        "RUNNING", instrument={"cpu": 10.0, "ram": 20.0, "disk": 30.0, "network_interfaces": {"interface": "eth0"},
                                "rtl_tcp_process": True, "rtl_tcp_listening": True, "sdr_temperature_c": None,
                                "lna_temperature_c": None, "mount_state": "NOT_EXPOSED",
                                "telemetry_stale": False, "error": None})))
    assert "N/A" in html
    assert "298.9" not in html
    assert "0.0 °C" not in html and "0 °C" not in html


# ---------------------------------------------------------------- Wi-Fi/SDIO health (2026-09-17 incident)


class _FakeWifiMonitor:
    def __init__(self, sample):
        self._sample = sample

    def sample(self, now_utc):
        return self._sample


_WIFI_SAMPLE_OK = {
    "interface": "wlan0", "wlan_present": True, "wlan_carrier": 1, "wlan_operstate": "up",
    "wlan_associated": True, "wlan_ssid": "Marciano5", "wlan_bssid": "44:48:b9:49:bb:07",
    "wlan_freq_mhz": 5220, "wlan_signal_dbm": -53, "wlan_tx_mbps": 433.3, "wlan_rx_mbps": 325.0,
    "networkmanager_state": "100 (connected)", "default_route_present": True,
    "sdio_txfail_total": 0, "sdio_ctrlframe_fail_total": 0, "sdio_backplane_halt_total": 0,
    "sdio_error_total": 0, "wifi_health": "OK",
    "last_wifi_error_kind": None, "last_wifi_error_message": None, "last_wifi_error_utc": None,
}


def test_watcher_build_wifi_none_monitor_reads_unknown():
    wifi = watcher.build_wifi(utcnow(), None)
    assert wifi["state"] == "UNKNOWN"


def test_watcher_build_wifi_reflects_monitor_sample():
    wifi = watcher.build_wifi(utcnow(), _FakeWifiMonitor(_WIFI_SAMPLE_OK))
    assert wifi["state"] == "OK"
    assert wifi["ssid"] == "Marciano5"
    assert wifi["signal_dbm"] == -53
    assert wifi["sdio_error_count"] == 0


def test_watcher_build_wifi_survives_monitor_exception():
    class ExplodingMonitor:
        def sample(self, now_utc):
            raise RuntimeError("boom")

    wifi = watcher.build_wifi(utcnow(), ExplodingMonitor())
    assert wifi["state"] == "UNKNOWN"


def test_watcher_system_state_degraded_when_wifi_degraded(tmp_path):
    state = watcher.WatcherState()
    state.wifi_monitor = _FakeWifiMonitor({**_WIFI_SAMPLE_OK, "wifi_health": "DEGRADED", "sdio_txfail_total": 4,
                                            "sdio_error_total": 4})
    status = watcher.build_status(utcnow(), 0.0, state, tmp_path, lambda: fake_telemetry(),
                                   capture_process_detected=False)
    assert status["wifi"]["state"] == "DEGRADED"
    assert status["system_state"] == "DEGRADED"  # everything else is nominal - this alone must flip it


def test_watcher_system_state_failed_wifi_also_flips_system_state(tmp_path):
    state = watcher.WatcherState()
    state.wifi_monitor = _FakeWifiMonitor({**_WIFI_SAMPLE_OK, "wifi_health": "FAILED",
                                            "last_wifi_error_message": "failed backplane access over SDIO"})
    status = watcher.build_status(utcnow(), 0.0, state, tmp_path, lambda: fake_telemetry(),
                                   capture_process_detected=False)
    assert status["system_state"] == "DEGRADED"
    assert "backplane" in status["wifi"]["last_error"]


def test_watcher_system_state_stays_ready_when_wifi_ok(tmp_path):
    state = watcher.WatcherState()
    state.wifi_monitor = _FakeWifiMonitor(_WIFI_SAMPLE_OK)
    status = watcher.build_status(utcnow(), 0.0, state, tmp_path, lambda: fake_telemetry(),
                                   capture_process_detected=False)
    assert status["system_state"] == "READY"


def test_24c_frontend_wifi_ok(tmp_path):
    html = dom(console_root(tmp_path, status={**status_fixture("RUNNING"),
                                                "wifi": {"state": "OK", "last_error": None, "sdio_error_count": 0}}))
    assert "<dt>WI-FI</dt><dd>OK</dd>" in html


def test_24d_frontend_wifi_degraded_shows_error_count(tmp_path):
    html = dom(console_root(tmp_path, status={**status_fixture("RUNNING"),
                                                "wifi": {"state": "DEGRADED", "last_error": "brcmf_sdio_txfail",
                                                         "sdio_error_count": 12}}))
    assert "DEGRADED — 12 SDIO errors" in html


def test_24e_frontend_wifi_failed_backplane_message(tmp_path):
    html = dom(console_root(tmp_path, status={**status_fixture("RUNNING"),
                                                "wifi": {"state": "FAILED",
                                                         "last_error": "failed backplane access over SDIO",
                                                         "sdio_error_count": 340}}))
    assert "FAILED — Broadcom SDIO backplane halted" in html


def test_24f_frontend_wifi_absent_from_status_reads_as_unknown_not_broken(tmp_path):
    """An older almita_status.json without a "wifi" key at all (pre-rollout)
    must not break the console - render() defaults it to {}."""
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))
    assert "<dt>WI-FI</dt><dd>UNKNOWN</dd>" in html


# ---------------------------------------------------------------- 25-28: RFI_REF sidecar


def test_25_watcher_rfi_ref_missing_status_file_defaults_to_disabled(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["status"] == "DISABLED"
    assert status["rfi_ref"]["enabled"] is False


def test_26_watcher_rfi_ref_matching_session_is_reflected(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {
        "schema_version": 1, "session_id": "s1", "enabled": True, "status": "RUNNING",
        "device_serial": "00000002", "center_frequency_hz": 1420405000, "sample_rate": 2400000,
        "gain_db": 25.0, "fft_duty_fraction": 0.05, "clipping_fraction": 0.0,
        "occupancy_fraction": 0.0016, "peak_dbfs": -42.3, "processed_blocks": 100,
        "skipped_blocks": 1900, "dropped_blocks": 0, "last_update_utc": utcnow(), "last_error": None,
    })
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["status"] == "RUNNING"
    assert status["rfi_ref"]["gain_db"] == 25.0
    assert status["rfi_ref"]["device_serial"] == "00000002"
    # RFI_REF degradation must never be conflated with acquisition state.
    assert status["acquisition"]["state"] == "RUNNING"


def test_27_watcher_rfi_ref_stale_session_falls_back_to_disabled(tmp_path):
    """A leftover rfi_ref_status.json from a previous session (older/mismatched
    session_id) must never bleed into the current session's console view -
    same discipline the existing quicklook_announcement.json check already
    applies, so older sessions/artifacts stay compatible."""
    announce_session(tmp_path, session_id="s2", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {
        "schema_version": 1, "session_id": "s1-old", "enabled": True, "status": "RUNNING",
        "device_serial": "00000002", "last_update_utc": utcnow(), "last_error": None,
    })
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["status"] == "DISABLED"


def test_28_frontend_rfi_ref_panel_shows_running_metrics(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={
            "enabled": True, "status": "RUNNING", "device_serial": "00000002",
            "center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 25.0,
            "fft_duty_fraction": 0.05, "clipping_fraction": 0.0, "occupancy_fraction": 0.0016,
            "peak_dbfs": -42.3, "processed_blocks": 100, "skipped_blocks": 1900,
            "dropped_blocks": 0, "last_update_utc": utcnow(), "last_error": None,
        })))
    assert "RFI REFERENCE" in html
    assert ">RUNNING<" in html
    assert "00000002" in html


def test_29_frontend_rfi_ref_missing_field_renders_disabled_not_broken(tmp_path):
    """Backward compatibility: a status.json produced by a watcher/session
    that predates RFI_REF (no 'rfi_ref' key at all) must still render."""
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))
    assert "RFI REFERENCE" in html
    assert "DISABLED" in html


# ---------------------------------------------------------------- 30-38: ANTENNA B / RFI_REF spectrum


def _rfi_ref_running(**overrides):
    base = {
        "enabled": True, "status": "RUNNING", "device_serial": "00000002",
        "center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 25.0,
        "fft_duty_fraction": 0.05, "clipping_fraction": 0.0, "occupancy_fraction": 0.0016,
        "peak_dbfs": -42.3, "processed_blocks": 100, "skipped_blocks": 1900,
        "dropped_blocks": 0, "last_update_utc": utcnow(), "last_error": None,
    }
    base.update(overrides)
    return base


def _rfi_ref_spectrum(session_id, **overrides):
    base = {
        "schema_version": 1, "session_id": session_id, "updated_utc": utcnow(),
        "center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 25.0,
        "device_serial": "00000002",
        "frequency_hz": [1420405000 + i * 1000 for i in range(-5, 5)],
        "power_dbfs": [-60.0 + i for i in range(10)],
    }
    base.update(overrides)
    return base


def test_30_watcher_rfi_spectrum_available_when_session_matches(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {**_rfi_ref_running(), "session_id": "s1"})
    atomic_write_json(tmp_path / "rfi_ref_spectrum.json", _rfi_ref_spectrum("s1"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["spectrum_available"] is True
    assert status["rfi_ref"]["spectrum_updated_utc"] is not None


def test_31_watcher_rfi_spectrum_rejected_on_session_mismatch(tmp_path):
    announce_session(tmp_path, session_id="s2", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {**_rfi_ref_running(), "session_id": "s2"})
    atomic_write_json(tmp_path / "rfi_ref_spectrum.json", _rfi_ref_spectrum("s1-old"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["status"] == "RUNNING"  # aggregate status still reflects this session
    assert status["rfi_ref"]["spectrum_available"] is False
    assert status["rfi_ref"]["spectrum_updated_utc"] is None


def test_32_watcher_rfi_spectrum_stale_during_running_is_rejected(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {**_rfi_ref_running(), "session_id": "s1"})
    atomic_write_json(tmp_path / "rfi_ref_spectrum.json",
                       _rfi_ref_spectrum("s1", updated_utc="2000-01-01T00:00:00+00:00"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["spectrum_available"] is False


def test_33_watcher_rfi_spectrum_stopped_retains_final_spectrum_regardless_of_age(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_COMPLETED", state="COMPLETED")
    atomic_write_json(tmp_path / "rfi_ref_status.json",
                       {**_rfi_ref_running(status="STOPPED"), "session_id": "s1"})
    atomic_write_json(tmp_path / "rfi_ref_spectrum.json",
                       _rfi_ref_spectrum("s1", updated_utc="2000-01-01T00:00:00+00:00"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=False)
    assert status["rfi_ref"]["status"] == "STOPPED"
    assert status["rfi_ref"]["spectrum_available"] is True


def test_34_watcher_rfi_spectrum_missing_file_is_clean_unavailable(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {**_rfi_ref_running(), "session_id": "s1"})
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["spectrum_available"] is False
    assert status["rfi_ref"]["spectrum_updated_utc"] is None


def test_35_frontend_antenna_labels_present(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))
    assert "ANTENNA A" in html and "SCIENCE" in html
    assert "ANTENNA B" in html and "RFI REFERENCE" in html


def test_36_frontend_rfi_spectrum_renders_when_available(tmp_path):
    public = console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={**_rfi_ref_running(), "spectrum_available": True,
                             "spectrum_updated_utc": utcnow()}))
    atomic_write_json(public / "runtime" / "rfi_ref_spectrum.json", _rfi_ref_spectrum("s1"))
    html = dom(public)
    assert "ANTENNA B / RFI REF" in html
    assert "RTL-SDR V3" in html
    assert "WAITING FOR RFI PRODUCTS" not in html
    assert 'id="rfi-spectrum-canvas"' in html


def test_37_frontend_rfi_spectrum_disabled_shows_placeholder(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))  # no rfi_ref -> DISABLED
    assert 'id="rfi-products-placeholder"' in html
    assert "WAITING FOR RFI PRODUCTS" not in html


def test_38_frontend_rfi_spectrum_waiting_when_enabled_but_not_yet_available(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={**_rfi_ref_running(), "spectrum_available": False, "waterfall_available": False})))
    assert "WAITING FOR RFI PRODUCTS" in html


# ---------------------------------------------------------------- 39-47: ANTENNA B waterfall + occupancy map


def _rfi_ref_waterfall(session_id, **overrides):
    base = {
        "schema_version": 1, "session_id": session_id, "device_serial": "00000002",
        "center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 25.0,
        "frequency_hz": [1420405000 + i * 1000 for i in range(-5, 5)],
        "rows": [{"utc": utcnow(), "power_dbfs": [-60.0 + i for i in range(10)]}],
        "updated_utc": utcnow(),
    }
    base.update(overrides)
    return base


def _quicklook_running(**overrides):
    base = {
        "state": "OK", "points_processed": 3, "last_product_utc": utcnow(),
        "spectrum_available": True, "waterfall_available": True, "map_available": True,
        "rfi_occupancy_map_available": True, "interpolated_map_available": True,
        "quicklook_stale": False, "error": None,
    }
    base.update(overrides)
    return base


def test_39_watcher_rfi_waterfall_available_when_session_matches(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {**_rfi_ref_running(), "session_id": "s1"})
    atomic_write_json(tmp_path / "rfi_ref_waterfall.json", _rfi_ref_waterfall("s1"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["waterfall_available"] is True
    assert status["rfi_ref"]["waterfall_updated_utc"] is not None


def test_40_watcher_rfi_waterfall_rejected_on_session_mismatch(tmp_path):
    announce_session(tmp_path, session_id="s2", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {**_rfi_ref_running(), "session_id": "s2"})
    atomic_write_json(tmp_path / "rfi_ref_waterfall.json", _rfi_ref_waterfall("s1-old"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["waterfall_available"] is False
    assert status["rfi_ref"]["waterfall_updated_utc"] is None


def test_41_watcher_rfi_waterfall_stale_during_running_is_rejected(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {**_rfi_ref_running(), "session_id": "s1"})
    atomic_write_json(tmp_path / "rfi_ref_waterfall.json",
                       _rfi_ref_waterfall("s1", updated_utc="2000-01-01T00:00:00+00:00"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["waterfall_available"] is False


def test_42_watcher_rfi_waterfall_stopped_retains_final_regardless_of_age(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_COMPLETED", state="COMPLETED")
    atomic_write_json(tmp_path / "rfi_ref_status.json",
                       {**_rfi_ref_running(status="STOPPED"), "session_id": "s1"})
    atomic_write_json(tmp_path / "rfi_ref_waterfall.json",
                       _rfi_ref_waterfall("s1", updated_utc="2000-01-01T00:00:00+00:00"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=False)
    assert status["rfi_ref"]["status"] == "STOPPED"
    assert status["rfi_ref"]["waterfall_available"] is True


def test_42b_watcher_rfi_session_waterfall_available_when_session_matches(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {**_rfi_ref_running(), "session_id": "s1"})
    atomic_write_json(tmp_path / "rfi_ref_session_waterfall.json", _rfi_ref_waterfall("s1"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["session_waterfall_available"] is True
    assert status["rfi_ref"]["session_waterfall_updated_utc"] is not None


def test_42c_watcher_rfi_session_waterfall_rejected_on_session_mismatch(tmp_path):
    announce_session(tmp_path, session_id="s2", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {**_rfi_ref_running(), "session_id": "s2"})
    atomic_write_json(tmp_path / "rfi_ref_session_waterfall.json", _rfi_ref_waterfall("s1-old"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["session_waterfall_available"] is False
    assert status["rfi_ref"]["session_waterfall_updated_utc"] is None


def test_42d_watcher_rfi_session_waterfall_stale_during_running_is_rejected(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(tmp_path / "rfi_ref_status.json", {**_rfi_ref_running(), "session_id": "s1"})
    atomic_write_json(tmp_path / "rfi_ref_session_waterfall.json",
                       _rfi_ref_waterfall("s1", updated_utc="2000-01-01T00:00:00+00:00"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["rfi_ref"]["session_waterfall_available"] is False


def test_42e_watcher_rfi_session_waterfall_stopped_retains_final_regardless_of_age(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_COMPLETED", state="COMPLETED")
    atomic_write_json(tmp_path / "rfi_ref_status.json",
                       {**_rfi_ref_running(status="STOPPED"), "session_id": "s1"})
    atomic_write_json(tmp_path / "rfi_ref_session_waterfall.json",
                       _rfi_ref_waterfall("s1", updated_utc="2000-01-01T00:00:00+00:00"))
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=False)
    assert status["rfi_ref"]["status"] == "STOPPED"
    assert status["rfi_ref"]["session_waterfall_available"] is True


def test_43_watcher_quicklook_rfi_occupancy_map_available_via_existing_gate(tmp_path):
    """The occupancy map rides the SAME session/staleness gate already
    applied to Antenna A's own quicklook products - no separate mechanism."""
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    quicklook_root = tmp_path / "quicklook_out"
    quicklook_root.mkdir()
    atomic_write_json(tmp_path / "quicklook_announcement.json",
                       {"schema_version": 1, "session_id": "s1", "quicklook_root": str(quicklook_root)})
    atomic_write_json(quicklook_root / "quicklook_live_status.json",
                       {"status": "OK", "points_processed": 1, "updated_utc": utcnow()})
    (quicklook_root / "rfi_occupancy_map.json").write_text("{}")
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["quicklook"]["rfi_occupancy_map_available"] is True
    assert status["quicklook"]["map_available"] is False  # Antenna A's own map is a distinct file


def test_43b_watcher_quicklook_waterfall_available_matches_served_file(tmp_path):
    """Regression for the confirmed contract bug: the console's WATERFALL
    thumb serves session_waterfall.png (see console/app.js), so the gate
    must reflect session_waterfall.json's existence - not
    latest_waterfall.json's, a different, per-point product that exists on
    every point regardless of whether the whole-session accumulator
    (independently best-effort in quicklook_live.py) ever wrote anything."""
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    quicklook_root = tmp_path / "quicklook_out"
    quicklook_root.mkdir()
    atomic_write_json(tmp_path / "quicklook_announcement.json",
                       {"schema_version": 1, "session_id": "s1", "quicklook_root": str(quicklook_root)})
    atomic_write_json(quicklook_root / "quicklook_live_status.json",
                       {"status": "OK", "points_processed": 1, "updated_utc": utcnow()})
    (quicklook_root / "session_waterfall.json").write_text("{}")  # no latest_waterfall.json at all
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["quicklook"]["waterfall_available"] is True


def test_43c_watcher_quicklook_waterfall_unavailable_when_only_the_per_point_file_exists(tmp_path):
    """The exact historical failure mode this fixes: a per-point capture
    always writes latest_waterfall.json, but if the whole-session
    accumulator (session_waterfall.json/.png) never successfully wrote for
    this session, the gate must not claim availability - that would have
    the console request session_waterfall.png and get a 404 despite the
    JSON-based gate reading True."""
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="RUNNING")
    quicklook_root = tmp_path / "quicklook_out"
    quicklook_root.mkdir()
    atomic_write_json(tmp_path / "quicklook_announcement.json",
                       {"schema_version": 1, "session_id": "s1", "quicklook_root": str(quicklook_root)})
    atomic_write_json(quicklook_root / "quicklook_live_status.json",
                       {"status": "OK", "points_processed": 1, "updated_utc": utcnow()})
    (quicklook_root / "latest_waterfall.json").write_text("{}")  # per-point file only
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["quicklook"]["waterfall_available"] is False


def test_43d_contract_console_waterfall_thumb_file_matches_watcher_gate_file(tmp_path):
    """Static contract guard: whatever filename console/app.js's WATERFALL
    thumb actually requests (minus its .png extension) must be the exact
    stem almita_console_watcher.py's waterfall_available gate checks for -
    the naming drift that caused this bug (e92caad updated the served file
    but not the gate) must fail this test immediately if it recurs for any
    product, not just be caught by luck in a fixture."""
    app_js = (Path(__file__).parent.parent / "console" / "app.js").read_text()
    match = re.search(r'\["waterfall","([a-zA-Z0-9_.]+)\.png"', app_js)
    assert match, "could not find the WATERFALL thumb's served filename in console/app.js"
    served_stem = match.group(1)
    watcher_src = Path(watcher.__file__).read_text()
    gate_match = re.search(r'"waterfall_available":\s*\(root\s*/\s*"([a-zA-Z0-9_.]+)\.json"\)', watcher_src)
    assert gate_match, "could not find the waterfall_available gate's checked filename"
    assert gate_match.group(1) == served_stem


def test_44_frontend_rfi_thumbs_show_all_three_when_available(tmp_path):
    public = console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={**_rfi_ref_running(), "spectrum_available": True,
                             "session_waterfall_available": True},
        quicklook=_quicklook_running()))
    atomic_write_json(public / "runtime" / "rfi_ref_spectrum.json", _rfi_ref_spectrum("s1"))
    atomic_write_json(public / "runtime" / "rfi_ref_session_waterfall.json", _rfi_ref_waterfall("s1"))
    html = dom(public)
    assert "RFI SPECTRUM" in html and "RFI WATERFALL" in html and "RFI OCCUPANCY MAP" in html
    assert 'id="rfi-waterfall-canvas"' in html
    assert "WAITING FOR RFI PRODUCTS" not in html


def test_44b_frontend_rfi_spectrum_and_waterfall_links_open_image_not_json(tmp_path):
    """Regression for the confirmed bug: clicking RFI SPECTRUM/RFI WATERFALL
    used to open the raw JSON directly (console/app.js set the link's href
    to the .json URL). Neither product has a server-rendered PNG (unlike
    Antenna A and the occupancy map below) - they are drawn only
    client-side - so the fix exports the same already-drawn chart, at a
    larger size, as a PNG data URL via the browser's own Canvas API
    (canvas.toDataURL), and that image must be the link's destination."""
    public = console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={**_rfi_ref_running(), "spectrum_available": True,
                             "session_waterfall_available": True},
        quicklook=_quicklook_running()))
    atomic_write_json(public / "runtime" / "rfi_ref_spectrum.json", _rfi_ref_spectrum("s1"))
    atomic_write_json(public / "runtime" / "rfi_ref_session_waterfall.json", _rfi_ref_waterfall("s1"))
    html = dom(public)
    spectrum_href = re.search(r'id="rfi-spectrum-link"[^>]*href="([^"]*)"', html)
    waterfall_href = re.search(r'id="rfi-waterfall-link"[^>]*href="([^"]*)"', html)
    assert spectrum_href, "rfi-spectrum-link has no href"
    assert waterfall_href, "rfi-waterfall-link has no href"
    assert spectrum_href.group(1).startswith("data:image/png")
    assert waterfall_href.group(1).startswith("data:image/png")
    assert "rfi_ref_spectrum.json" not in spectrum_href.group(1)
    assert "rfi_ref_session_waterfall.json" not in waterfall_href.group(1)
    # target="_blank" rel="noopener" (already on both anchors, untouched)
    # is what makes the click open in a new tab rather than navigating the
    # live console away - confirm it is still there.
    assert 'id="rfi-spectrum-link" target="_blank" rel="noopener"' in html
    assert 'id="rfi-waterfall-link" target="_blank" rel="noopener"' in html


def test_44c_frontend_antenna_a_thumb_links_unaffected_by_rfi_fix(tmp_path):
    """Antenna A's own thumbnails (real server-rendered PNGs) must keep
    linking straight to their PNG file, exactly as before - untouched by
    the Antenna B click-target fix."""
    public = console_root(tmp_path, status=status_fixture("RUNNING", quicklook=_quicklook_running()))
    html = dom(public)
    spectrum_href = re.search(r'id="thumb-spectrum-link"[^>]*href="([^"]*)"', html)
    assert spectrum_href
    assert "quicklook_products/latest_spectrum.png" in spectrum_href.group(1)


def test_44d_frontend_rfi_enlarged_views_match_antenna_a_measured_dimensions(tmp_path):
    """Fase B5: dimensions must be MEASURED from Antenna A's own real
    products, not guessed. quicklook_spectrum.py and
    quicklook_session_waterfall.py (the actual file renderThumbs() links
    to - not the older, unlinked quicklook_waterfall.py) both save
    figsize=(12,6) at dpi=150 -> 1800x900. Antenna B's enlarged views must
    be exactly that, for both products."""
    import base64
    import struct
    public = console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={**_rfi_ref_running(), "spectrum_available": True,
                             "session_waterfall_available": True},
        quicklook=_quicklook_running()))
    atomic_write_json(public / "runtime" / "rfi_ref_spectrum.json", _rfi_ref_spectrum("s1"))
    atomic_write_json(public / "runtime" / "rfi_ref_session_waterfall.json", _rfi_ref_waterfall("s1"))
    html = dom(public)
    for link_id in ("rfi-spectrum-link", "rfi-waterfall-link"):
        href = re.search(rf'id="{link_id}"[^>]*href="(data:image/png;base64,[^"]+)"', html)
        assert href, f"{link_id} has no PNG data URL"
        png_bytes = base64.b64decode(href.group(1).split(",", 1)[1])
        width, height = struct.unpack(">II", png_bytes[16:24])
        assert (width, height) == (1800, 900), f"{link_id}: got {width}x{height}, expected 1800x900"


def test_44e_frontend_rfi_enlarged_views_are_titled_and_labeled(tmp_path):
    """Fase B6/B4: the enlarged view must say which receiver produced it
    (title) and carry real axis labels - the small 320x140 thumbnail has
    no room for either and never had them; the enlarged view must."""
    import base64
    public = console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={**_rfi_ref_running(), "spectrum_available": True,
                             "session_waterfall_available": True},
        quicklook=_quicklook_running()))
    atomic_write_json(public / "runtime" / "rfi_ref_spectrum.json", _rfi_ref_spectrum("s1"))
    atomic_write_json(public / "runtime" / "rfi_ref_session_waterfall.json", _rfi_ref_waterfall("s1"))
    html = dom(public)
    spectrum_href = re.search(r'id="rfi-spectrum-link"[^>]*href="([^"]+)"', html).group(1)
    waterfall_href = re.search(r'id="rfi-waterfall-link"[^>]*href="([^"]+)"', html).group(1)
    # A PNG data URL can't be grepped for text directly - but the SOURCE
    # that produced it can: confirm the exact title strings this pass
    # introduced are present in console/app.js's own draw-call sites,
    # which is what actually ends up burned into the pixels.
    app_js = (CONSOLE / "app.js").read_text()
    assert "ALMITA — RFI REF — Spectrum" in app_js
    assert "ALMITA — RFI REF — Waterfall" in app_js
    assert '"Frequency (MHz)"' in app_js
    assert spectrum_href.startswith("data:image/png") and waterfall_href.startswith("data:image/png")


def test_44f_frontend_rfi_waterfall_frequency_axis_uses_mhz_not_raw_hz(tmp_path):
    """Regression for a real bug found via this pass's own visual QA:
    drawRfiWaterfall's caller passed data.frequency_hz (raw Hz, per the
    JSON's own field name) straight through unconverted, while the axis
    was labeled "Frequency (MHz)" - the tiny 320x140 thumbnail never had
    room to show the resulting wrong tick values legibly, but decoding the
    actual enlarged PNG bytes makes the mismatch checkable exactly:
    rfi_ref_session_waterfall.json's frequency_hz values here span
    1420400000-1420409000 Hz (1420.400-1420.409 MHz); if the fix
    regresses, the axis would render "1420400000" instead of "1420.4"."""
    public = console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={**_rfi_ref_running(), "session_waterfall_available": True},
        quicklook=_quicklook_running()))
    atomic_write_json(public / "runtime" / "rfi_ref_session_waterfall.json", _rfi_ref_waterfall("s1"))
    dom(public)  # renders without throwing - the real assertion is in app.js's own source below
    app_js = (CONSOLE / "app.js").read_text()
    # The call site must divide by 1e6 before handing frequency_hz to
    # drawRfiWaterfall, exactly like the spectrum call site already did.
    waterfall_call_region = app_js[app_js.index('fetchJson("rfi_ref_session_waterfall.json")'):]
    waterfall_call_region = waterfall_call_region[:waterfall_call_region.index("}else{")]
    assert "data.frequency_hz.map(f=>f/1e6)" in waterfall_call_region
    assert "drawRfiWaterfall(wfCanvas,data.rows,freqMHz)" in waterfall_call_region


def test_44g_frontend_rfi_waterfall_uses_viridis_like_antenna_a(tmp_path):
    """Fase B4: color scale consistency - Antenna A's own
    quicklook_session_waterfall.py uses matplotlib's cmap="viridis";
    Antenna B's hand-drawn heatmap must use the same color story (a small
    anchor-based approximation, no external colormap library) rather than
    a visually unrelated palette."""
    app_js = (CONSOLE / "app.js").read_text()
    assert "VIRIDIS_ANCHORS" in app_js
    assert "_viridis(" in app_js
    assert "drawRfiWaterfall" in app_js


def test_44h_frontend_rfi_click_never_triggers_a_new_request(tmp_path):
    """Fase B14: the enlarged view must reuse already-fetched JSON, drawn
    onto an offscreen canvas - no additional network request (i.e. no new
    acquisition/processing trigger) results from "clicking" (in this
    headless DOM-dump test, from the href simply existing after the page's
    own normal load sequence, with no extra requests beyond what a normal
    page load already made)."""
    public = console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={**_rfi_ref_running(), "spectrum_available": True,
                             "session_waterfall_available": True},
        quicklook=_quicklook_running()))
    atomic_write_json(public / "runtime" / "rfi_ref_spectrum.json", _rfi_ref_spectrum("s1"))
    atomic_write_json(public / "runtime" / "rfi_ref_session_waterfall.json", _rfi_ref_waterfall("s1"))
    # _fullSizeCanvasImageUrl must never itself call fetch/fetchJson - the
    # enlarged image is built entirely from data already fetched once for
    # the thumbnail. A structural source check, since a DOM dump cannot
    # observe "no further network calls" once rendering has already
    # settled.
    app_js = (CONSOLE / "app.js").read_text()
    full_size_fn = app_js[app_js.index("function _fullSizeCanvasImageUrl"):]
    full_size_fn = full_size_fn[:full_size_fn.index("\n}")]
    assert "fetch" not in full_size_fn


def test_45_frontend_rfi_map_thumb_hidden_when_occupancy_map_unavailable(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture(
        "RUNNING", rfi_ref={**_rfi_ref_running(), "spectrum_available": True},
        quicklook=_quicklook_running(rfi_occupancy_map_available=False))))
    assert 'id="rfi-map-thumb" alt="RFI Occupancy Map" hidden' in html \
        or 'rfi-map-thumb" hidden' in html


def test_46_frontend_antenna_a_thumbs_unaffected_by_antenna_b(tmp_path):
    """Antenna A's own three thumbnails must keep working exactly as before,
    independent of whatever Antenna B is doing."""
    public = console_root(tmp_path, status=status_fixture("RUNNING", quicklook=_quicklook_running()))
    html = dom(public)
    assert 'id="quicklook-thumbs"' in html and "quicklook-thumbs\" hidden" not in html
    assert "SPECTRUM" in html and "WATERFALL" in html and "MAP" in html


def test_47_frontend_rfi_thumbs_stopped_state_retains_products(tmp_path):
    public = console_root(tmp_path, status=status_fixture(
        "COMPLETED", rfi_ref={**_rfi_ref_running(status="STOPPED"), "spectrum_available": True,
                              "session_waterfall_available": True},
        quicklook=_quicklook_running()))
    atomic_write_json(public / "runtime" / "rfi_ref_spectrum.json", _rfi_ref_spectrum("s1"))
    atomic_write_json(public / "runtime" / "rfi_ref_session_waterfall.json", _rfi_ref_waterfall("s1"))
    html = dom(public)
    assert ">STOPPED<" in html
    assert "WAITING FOR RFI PRODUCTS" not in html


def test_48_frontend_antenna_a_interpolated_preview_thumb_shown_when_available(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture(
        "RUNNING", quicklook=_quicklook_running(interpolated_map_available=True))))
    assert "NATIVE GRID (INTERPOLATED PREVIEW)" in html
    assert 'id="thumb-map-interpolated" alt="Native Grid (Interpolated Preview)" hidden' not in html


def test_49_frontend_antenna_a_interpolated_preview_hidden_when_unavailable(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture(
        "RUNNING", quicklook=_quicklook_running(interpolated_map_available=False))))
    assert 'id="thumb-map-interpolated" alt="Native Grid (Interpolated Preview)" hidden' in html \
        or 'thumb-map-interpolated" hidden' in html
    # Antenna A's exact map is completely unaffected by preview availability.
    assert 'id="thumb-map" alt="Map" hidden' not in html and 'thumb-map" hidden' not in html


# ---------------------------------------------------------------- SPECTRAL STACK 3D (Antenna A)


def test_60_spectral_stack_pure_functions(tmp_path):
    """Exercises parseDocument/colormap/robustLimits for real in a browser
    (reusing the chromium harness already required by every dom() test
    above, rather than adding a `node`-on-PATH dependency this suite can't
    rely on) - no THREE.js or canvas needed since boot() no-ops without one,
    leaving window.AlmitaSpectralStack3D free to exercise directly."""
    root = tmp_path / "public"
    root.mkdir()
    (root / "spectral_stack_3d.js").write_text((CONSOLE / "spectral_stack_3d.js").read_text())
    harness = """<!doctype html><html><body><pre id="out"></pre>
<script src="spectral_stack_3d.js"></script>
<script>
const {colormap, robustLimits, parseDocument} = window.AlmitaSpectralStack3D;
const lines = [];
function check(name, cond) { lines.push((cond ? "ok: " : "FAIL: ") + name); }

check("colormap clamps below 0", JSON.stringify(colormap(-5)) === JSON.stringify(colormap(0)));
check("colormap clamps above 1", JSON.stringify(colormap(5)) === JSON.stringify(colormap(1)));

const values = Array.from({length:100}, (_,i)=>i+1).concat([-10000, 10000]);
const limits = robustLimits([{values}]);
check("robustLimits clips low outlier", limits.lo > -100);
check("robustLimits clips high outlier", limits.hi < 200);
const nullLimits = robustLimits([{values:[null,null]}]);
check("robustLimits handles all-null", Number.isFinite(nullLimits.lo) && Number.isFinite(nullLimits.hi));

const doc = {session_id:"s1", frequency_mhz:[1,2,3],
  rows:[{point_id:"3",relative_db:[.3,.3,.3]},{point_id:"1",relative_db:[.1,.1,.1]},{point_id:"2",relative_db:[.2,.2,.2]}]};
check("parseDocument rejects session mismatch", parseDocument(doc,"s2")===null);
const parsed = parseDocument(doc,"s1");
check("parseDocument sorts by scan_order", JSON.stringify(parsed.rows.map(r=>r.scanOrder))==='[1,2,3]');

const badDoc = {session_id:"s1", frequency_mhz:[1,2,3],
  rows:[{point_id:"1",relative_db:[.1,.2]},{point_id:"2",relative_db:[.1,.2,.3]},{point_id:"nope",relative_db:[.1,.2,.3]}]};
const badParsed = parseDocument(badDoc,"s1");
check("parseDocument drops mismatched-length and non-numeric rows", badParsed.rows.length===1 && badParsed.rows[0].pointId==="2");

document.getElementById("out").textContent = lines.join("\\n") +
  "\\n" + (lines.some(l=>l.startsWith("FAIL")) ? "RESULT: FAILURES" : "RESULT: ALL PASSED");
</script></body></html>"""
    (root / "index.html").write_text(harness)
    html = dom(root, query="/")
    assert "RESULT: ALL PASSED" in html, html


def test_61_spectral_stack_panel_sits_beside_antenna_a_and_b_column(tmp_path):
    """Requested layout: a single tall panel to the right of a left column
    holding BOTH ANTENNA A and ANTENNA B stacked, so it visually spans the
    combined height of both (flex row, align-items:stretch) - not literally
    between RFI and LAST KNOWN SESSION (that's the ACTIVITY LOG panel)."""
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))
    thumbs_index = html.find('id="quicklook-thumbs"')
    antenna_b_index = html.find("ANTENNA B")
    rfi_thumbs_index = html.find('id="rfi-thumbs"')
    panel_index = html.find('id="spectral-stack-panel"')
    last_session_index = html.find('id="last-session"')
    assert -1 not in (thumbs_index, antenna_b_index, rfi_thumbs_index, panel_index, last_session_index)
    # Antenna A, then Antenna B, both before the panel (left column comes
    # first in source order); the panel itself comes after both, and
    # everything is still before the unrelated LAST KNOWN SESSION panel.
    assert thumbs_index < antenna_b_index < rfi_thumbs_index < panel_index < last_session_index
    assert "SPECTRAL STACK 3D" in html and "FREQUENCY × CAPTURE × POWER" in html


def test_62_spectral_stack_degrades_gracefully_without_webgl(tmp_path):
    """This sandbox's headless chromium has no GPU/WebGL - real proof the
    panel's own try/catch keeps the rest of the console working rather than
    breaking the page, not just an assumption. A real desktop browser has
    WebGL and reaches LIVE/COMPLETED instead - only verifiable by a human,
    hence the SSH-visual-check steps handed back to the operator."""
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))
    assert 'id="spectral-stack-badge" class="badge status-error">ERROR<' in html
    # The rest of the console must be completely unaffected by that failure.
    assert ">RUNNING<" in html and "demo" in html
    assert 'id="quicklook-thumbs"' in html


def test_63_spectral_stack_existing_thumbs_layout_unaffected(tmp_path):
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))
    assert "SPECTRUM" in html and "WATERFALL" in html and "MAP" in html
    assert 'id="thumb-spectrum-link"' in html and 'id="thumb-waterfall-link"' in html


def test_64_spectral_stack_panel_height_pinned_to_antenna_column(tmp_path):
    """align-items:stretch alone was unreliable (real screenshot showed the
    panel far taller than the antenna column, with a big empty area) - the
    panel's height is now set explicitly in JS from the antenna column's
    own measured height, verified here as a real non-empty pixel value."""
    html = dom(console_root(tmp_path, status=status_fixture("RUNNING")))
    match = re.search(r'id="spectral-stack-panel"[^>]*style="height: (\d+)px;"', html)
    assert match, html
    assert int(match.group(1)) > 0


# ---------------------------------------------------------------- activity log (orchestrator_capture.log tail)


def test_50_activity_log_unavailable_without_session_id(tmp_path):
    result = watcher.build_activity_log(tmp_path, None)
    assert result == {"available": False, "lines": []}


def test_51_activity_log_unavailable_without_observation_runtime_file(tmp_path):
    result = watcher.build_activity_log(tmp_path, "s1")
    assert result == {"available": False, "lines": []}


def test_52_activity_log_unavailable_on_session_mismatch(tmp_path):
    log = tmp_path / "orchestrator_capture.log"
    log.write_text("POINT 001/010\nline2\n")
    atomic_write_json(tmp_path / "observation_runtime.json",
                       {"session_id": "other-session", "capture_log": str(log)})
    result = watcher.build_activity_log(tmp_path, "s1")
    assert result == {"available": False, "lines": []}


def test_53_activity_log_tails_matching_session_log(tmp_path):
    log = tmp_path / "orchestrator_capture.log"
    log.write_text("line1\nline2\nline3\n")
    atomic_write_json(tmp_path / "observation_runtime.json",
                       {"session_id": "s1", "capture_log": str(log)})
    result = watcher.build_activity_log(tmp_path, "s1")
    assert result == {"available": True, "lines": ["line1", "line2", "line3"]}


def test_54_activity_log_caps_at_max_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "ACTIVITY_LOG_MAX_LINES", 3)
    log = tmp_path / "orchestrator_capture.log"
    log.write_text("\n".join(f"line{n}" for n in range(1, 21)) + "\n")
    atomic_write_json(tmp_path / "observation_runtime.json",
                       {"session_id": "s1", "capture_log": str(log)})
    result = watcher.build_activity_log(tmp_path, "s1")
    assert result == {"available": True, "lines": ["line18", "line19", "line20"]}


def test_55_activity_log_missing_log_file_is_clean_unavailable(tmp_path):
    atomic_write_json(tmp_path / "observation_runtime.json",
                       {"session_id": "s1", "capture_log": str(tmp_path / "missing.log")})
    result = watcher.build_activity_log(tmp_path, "s1")
    assert result == {"available": False, "lines": []}


def test_56_watcher_build_status_includes_activity_log(tmp_path):
    announce_session(tmp_path, session_id="s1", event="SESSION_STARTED", state="STARTING")
    log = tmp_path / "orchestrator_capture.log"
    log.write_text("POINT 001/010\n✓ POINT 001/010   total=12.3s\n")
    atomic_write_json(tmp_path / "observation_runtime.json",
                       {"session_id": "s1", "capture_log": str(log)})
    status = watcher.build_status(utcnow(), 0.0, watcher.WatcherState(), tmp_path,
                                   lambda: fake_telemetry(), capture_process_detected=True)
    assert status["activity_log"]["available"] is True
    assert status["activity_log"]["lines"][-1] == "✓ POINT 001/010   total=12.3s"


# ---------------------------------------------------------------- closeout: canonical runtime_dir


def _snapshot(path):
    """(exists, mtime_ns, content) for a real file that may legitimately
    already exist on this machine from unrelated prior real usage - tests
    must assert 'unchanged by this call', never 'absent', since ambient repo
    state (e.g. a real field session run earlier today) is out of our control."""
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_mtime_ns, path.read_bytes())


def test_capture_executor_without_runtime_dir_does_not_write_to_repo(tmp_path, monkeypatch):
    from test_capture_preflight import make_executor
    monkeypatch.chdir(tmp_path)
    ex = make_executor(tmp_path)
    assert ex.runtime_dir is None
    target = Path(capture_module.DEFAULT_RUNTIME_DIR) / "current_session.json"
    before = _snapshot(target)
    ex._announce(event="SESSION_STARTED", state="STARTING")  # must be a true no-op
    assert _snapshot(target) == before


def test_quicklooklive_without_runtime_dir_does_not_write_to_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    live = object.__new__(quicklook_module.QuicklookLive)
    live.runtime_dir = None
    live.session_id = "s1"
    live.output = tmp_path / "out"
    live.output.mkdir()
    target = Path(quicklook_module.DEFAULT_RUNTIME_DIR) / "quicklook_announcement.json"
    before = _snapshot(target)
    live._announce()  # must be a true no-op
    assert _snapshot(target) == before


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
