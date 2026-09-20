import csv
import json
from pathlib import Path

import almita_console_watcher as watcher
from capture import CaptureExecutor
from quicklook_live import resolve_session_id
from runtime_state import announce_session, atomic_write_json, read_json_safe

FIELDS = ["point_number", "scan_order", "target_ra_hours", "target_dec_degrees", "capture_status",
          "start_time", "end_time", "duration", "error_message", "data_filename", "session_name"]


def make_plan(tmp_path, session_name="test", n=1):
    plan = tmp_path / "plan.csv"
    with plan.open("w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=FIELDS)
        w.writeheader()
        for i in range(1, n + 1):
            w.writerow({"point_number": i, "scan_order": i, "target_ra_hours": i, "target_dec_degrees": -40,
                        "capture_status": "planned", "data_filename": f"p{i}.dat", "session_name": session_name})
    return plan


def make_observer_config(tmp_path, telescope=None):
    cfg = {"observer": {"latitude_deg": -33.4, "longitude_deg": -70.6, "elevation_m": 550}}
    if telescope:
        cfg["hardware"] = {"telescope": telescope}
    (tmp_path / "observer_config.json").write_text(json.dumps(cfg))


def fake_telemetry():
    return {"system": {}, "storage": {}, "network": {}, "sdr": {}, "temperatures": {}, "mount": {}}


# ---------------------------------------------------------------- device_name / banner (§8, §9)

def test_device_name_resolves_from_observer_config_hardware_telescope(tmp_path):
    make_plan(tmp_path)
    make_observer_config(tmp_path, telescope="LX200 OnStep")
    ex = CaptureExecutor(str(tmp_path / "plan.csv"), config_path="observer_config.json")
    assert ex.device_name == "LX200 OnStep"


def test_device_name_explicit_override_still_wins(tmp_path):
    make_plan(tmp_path)
    make_observer_config(tmp_path, telescope="LX200 OnStep")
    ex = CaptureExecutor(str(tmp_path / "plan.csv"), config_path="observer_config.json",
                          device_name="Telescope Simulator")
    assert ex.device_name == "Telescope Simulator"


def test_device_name_falls_back_when_config_has_no_hardware_section(tmp_path):
    make_plan(tmp_path)
    make_observer_config(tmp_path, telescope=None)
    ex = CaptureExecutor(str(tmp_path / "plan.csv"), config_path="observer_config.json")
    assert ex.device_name == "Telescope Simulator"


def test_banner_shows_real_gain_and_bias_t_on(tmp_path, capsys):
    make_plan(tmp_path)
    make_observer_config(tmp_path, telescope="LX200 OnStep")
    ex = CaptureExecutor(str(tmp_path / "plan.csv"), config_path="observer_config.json",
                          sdr_gain_db=40.2, bias_tee_enabled=True)
    ex.observation_points = [{"session_name": "test"}]
    ex.print_console_header(2.0, 10.0)
    out = capsys.readouterr().out
    assert "40.2 dB" in out
    assert "ON" in out
    assert "auto" not in out.lower()
    assert "N/D" not in out
    assert "LX200 OnStep" in out


def test_banner_shows_bias_t_off_when_disabled(tmp_path, capsys):
    make_plan(tmp_path)
    make_observer_config(tmp_path, telescope="LX200 OnStep")
    ex = CaptureExecutor(str(tmp_path / "plan.csv"), config_path="observer_config.json",
                          bias_tee_enabled=False)
    ex.observation_points = [{"session_name": "test"}]
    ex.print_console_header(2.0, 10.0)
    assert "OFF" in capsys.readouterr().out


# ---------------------------------------------------------------- capture side identity (§3, §4, §12)

def test_capture_persists_session_identity_once(tmp_path):
    make_plan(tmp_path)
    make_observer_config(tmp_path)
    ex = CaptureExecutor(str(tmp_path / "plan.csv"), config_path="observer_config.json")
    ex.session_id = "20260829_214747"
    output_dir = tmp_path / "iq_out"
    output_dir.mkdir()
    ex._persist_session_identity(output_dir, "DEMO-EAST-100-30X30-10S")
    identity = read_json_safe(output_dir / "session_identity.json")
    assert identity["session_id"] == "20260829_214747"
    assert identity["session_name"] == "DEMO-EAST-100-30X30-10S"
    assert identity["session_root"] == str(output_dir)
    assert identity["schema_version"] == 1
    assert "created_utc" in identity
    assert not (output_dir / "session_identity.json.tmp").exists()


def test_capture_session_identity_write_failure_is_a_true_noop(tmp_path, monkeypatch):
    make_plan(tmp_path)
    make_observer_config(tmp_path)
    ex = CaptureExecutor(str(tmp_path / "plan.csv"), config_path="observer_config.json")
    ex.session_id = "x"
    import capture as capture_module
    monkeypatch.setattr(capture_module, "atomic_write_json",
                         lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    ex._persist_session_identity(tmp_path / "some_dir", "s")  # must not raise


def test_capture_constructor_has_no_side_effects_without_persist_call(tmp_path):
    make_plan(tmp_path)
    make_observer_config(tmp_path)
    CaptureExecutor(str(tmp_path / "plan.csv"), config_path="observer_config.json")
    assert not (tmp_path / "session_identity.json").exists()


# ---------------------------------------------------------------- quicklook side identity (§5, §12)

def test_quicklook_resolves_canonical_session_id_not_basename(tmp_path):
    session_dir = tmp_path / "DEMO-EAST-100-30X30-10S-20260829-21:48:50"
    session_dir.mkdir()
    atomic_write_json(session_dir / "session_identity.json", {
        "schema_version": 1, "session_id": "20260829_214747", "session_name": "DEMO-EAST-100-30X30-10S",
        "session_root": str(session_dir), "created_utc": "2026-08-29T21:48:50+00:00",
    })
    assert resolve_session_id(session_dir) == "20260829_214747"
    assert resolve_session_id(session_dir) != session_dir.name


def test_quicklook_falls_back_to_basename_for_historical_session(tmp_path):
    session_dir = tmp_path / "legacy_session_20260101-00:00:00"
    session_dir.mkdir()
    assert resolve_session_id(session_dir) == session_dir.name


def test_quicklook_ignores_malformed_identity_file(tmp_path):
    session_dir = tmp_path / "weird-session"
    session_dir.mkdir()
    (session_dir / "session_identity.json").write_text("{not valid json")
    assert resolve_session_id(session_dir) == session_dir.name


def test_quicklook_ignores_identity_file_missing_session_id_field(tmp_path):
    session_dir = tmp_path / "weird-session-2"
    session_dir.mkdir()
    atomic_write_json(session_dir / "session_identity.json", {"schema_version": 1, "session_name": "x"})
    assert resolve_session_id(session_dir) == session_dir.name


def test_restart_resolves_same_identity_from_disk_each_time(tmp_path):
    """A watcher/quicklook restart re-reads the durable file - identity does
    not depend on any in-memory state surviving a process restart."""
    session_dir = tmp_path / "any-basename"
    session_dir.mkdir()
    atomic_write_json(session_dir / "session_identity.json",
                       {"schema_version": 1, "session_id": "20260829_214747", "session_name": "s",
                        "session_root": str(session_dir), "created_utc": "2026-08-29T21:48:50+00:00"})
    first = resolve_session_id(session_dir)
    second = resolve_session_id(session_dir)  # simulates a fresh process re-reading it
    assert first == second == "20260829_214747"


# ---------------------------------------------------------------- §11: exact bug reproduction

def test_bug_reproduction_console_waiting_before_fix_active_after():
    """Reproduces the exact reported failure: Capture announces session_id A;
    Quicklook's --session-dir has a completely different basename (its own
    IQ-output-folder timestamp, not the SessionManager id). Before this fix,
    Quicklook derived its own id from that basename and the watcher reported
    Quicklook as WAITING forever despite Quicklook being healthy. After the
    fix, Quicklook reads A from session_identity.json and the watcher
    correlates immediately."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        runtime_dir = tmp_path / "runtime"
        session_id_a = "20260829_214747"
        announce_session(runtime_dir, session_id=session_id_a, event="SESSION_STARTED",
                          session_name="DEMO-EAST-100-30X30-10S", state="RUNNING",
                          points_total=100, points_success=8, points_failed=0,
                          mount_device="LX200 OnStep")

        session_dir = tmp_path / "DEMO-EAST-100-30X30-10S-20260829-21:48:50"
        session_dir.mkdir()
        assert session_dir.name != session_id_a, "test setup must reproduce the real mismatch"

        # --- BEFORE the fix: Quicklook had no session_identity.json to read,
        # so it announced its own basename-derived id (old behavior) ---
        quicklook_id_before = session_dir.name
        atomic_write_json(runtime_dir / "quicklook_announcement.json",
                           {"schema_version": 1, "session_id": quicklook_id_before,
                            "quicklook_root": str(session_dir), "updated_utc": "2026-08-29T21:49:59+00:00"})
        status_before = watcher.build_status("2026-08-29T21:50:00+00:00", 0.0, watcher.WatcherState(),
                                              runtime_dir, fake_telemetry, capture_process_detected=True)
        assert status_before["quicklook"]["state"] == "WAITING"  # reproduces the reported bug exactly

        # --- AFTER the fix: capture persists canonical identity in the session
        # dir; quicklook reads it instead of the basename ---
        atomic_write_json(session_dir / "session_identity.json",
                           {"schema_version": 1, "session_id": session_id_a,
                            "session_name": "DEMO-EAST-100-30X30-10S", "session_root": str(session_dir),
                            "created_utc": "2026-08-29T21:48:50+00:00"})
        quicklook_id_after = resolve_session_id(session_dir)
        assert quicklook_id_after == session_id_a

        atomic_write_json(runtime_dir / "quicklook_announcement.json",
                           {"schema_version": 1, "session_id": quicklook_id_after,
                            "quicklook_root": str(session_dir), "updated_utc": "2026-08-29T21:50:05+00:00"})
        atomic_write_json(session_dir / "quicklook_live_status.json",
                           {"status": "OK", "points_processed": 8, "updated_utc": "2026-08-29T21:50:05+00:00"})
        (session_dir / "latest_spectrum.json").write_text("{}")
        (session_dir / "latest_waterfall.json").write_text("{}")
        # session_waterfall.json is the file the console actually gates its
        # WATERFALL thumb on (it serves session_waterfall.png, not
        # latest_waterfall.png) - both exist in a real session, so both are
        # present here too.
        (session_dir / "session_waterfall.json").write_text("{}")
        (session_dir / "quicklook_map.json").write_text("{}")

        status_after = watcher.build_status("2026-08-29T21:50:06+00:00", 100.0, watcher.WatcherState(),
                                             runtime_dir, fake_telemetry, capture_process_detected=True)
        assert status_after["acquisition"]["session_id"] == session_id_a
        assert status_after["quicklook"]["state"] == "OK"
        assert status_after["quicklook"]["points_processed"] == 8
        assert status_after["quicklook"]["spectrum_available"] is True
        assert status_after["quicklook"]["waterfall_available"] is True
        assert status_after["quicklook"]["map_available"] is True
        assert status_after["instrument"]["mount_device"] == "LX200 OnStep"


# ---------------------------------------------------------------- §6: watcher correlation safety

def test_watcher_rejects_quicklook_announcement_from_a_different_session(tmp_path):
    announce_session(tmp_path, session_id="A", event="SESSION_STARTED", state="RUNNING")
    other_dir = tmp_path / "unrelated_session_products"
    other_dir.mkdir()
    atomic_write_json(other_dir / "quicklook_live_status.json",
                       {"status": "OK", "points_processed": 99, "updated_utc": "2026-08-29T21:50:00+00:00"})
    atomic_write_json(tmp_path / "quicklook_announcement.json",
                       {"schema_version": 1, "session_id": "B", "quicklook_root": str(other_dir),
                        "updated_utc": "2026-08-29T21:50:00+00:00"})
    status = watcher.build_status("2026-08-29T21:50:01+00:00", 0.0, watcher.WatcherState(),
                                   tmp_path, fake_telemetry, capture_process_detected=True)
    assert status["acquisition"]["session_id"] == "A"
    assert status["quicklook"]["state"] == "WAITING"  # session B's products are never attributed to A
    assert status["quicklook"]["points_processed"] is None


# ---------------------------------------------------------------- §13: simulated end-to-end integration

def test_simulated_integration_capture_quicklook_watcher_share_one_identity(tmp_path):
    runtime_dir = tmp_path / "runtime"
    session_id = "20260829_999999"

    # 1. Capture announces SESSION_STARTED with the canonical id.
    announce_session(runtime_dir, session_id=session_id, event="SESSION_STARTED",
                      session_name="INTEGRATION-DEMO", state="STARTING", mount_device="LX200 OnStep")

    # 2. The session directory exists with canonical metadata (as Capture
    # would have written at its first point).
    session_dir = tmp_path / "INTEGRATION-DEMO-20260829-99:99:99"
    session_dir.mkdir()
    atomic_write_json(session_dir / "session_identity.json",
                       {"schema_version": 1, "session_id": session_id, "session_name": "INTEGRATION-DEMO",
                        "session_root": str(session_dir), "created_utc": "2026-08-29T22:00:00+00:00"})
    announce_session(runtime_dir, session_id=session_id, event="POINT_STARTED", state="RUNNING",
                      session_root=str(session_dir), points_total=3, points_success=0, points_failed=0)

    # 3. Quicklook receives only --session-dir and resolves the same id.
    quicklook_id = resolve_session_id(session_dir)
    assert quicklook_id == session_id

    # 4. Quicklook publishes fixtures under that id.
    atomic_write_json(runtime_dir / "quicklook_announcement.json",
                       {"schema_version": 1, "session_id": quicklook_id, "quicklook_root": str(session_dir),
                        "updated_utc": "2026-08-29T22:00:05+00:00"})
    atomic_write_json(session_dir / "quicklook_live_status.json",
                       {"status": "OK", "points_processed": 2, "updated_utc": "2026-08-29T22:00:05+00:00"})
    (session_dir / "latest_spectrum.json").write_text("{}")
    (session_dir / "latest_waterfall.json").write_text("{}")
    (session_dir / "session_waterfall.json").write_text("{}")
    (session_dir / "quicklook_map.json").write_text("{}")
    announce_session(runtime_dir, session_id=session_id, event="POINT_COMPLETED", state="RUNNING",
                      points_success=2)

    # 5. Watcher runs one iteration.
    status = watcher.tick(runtime_dir, watcher.WatcherState(), collect_fn=fake_telemetry)

    # 6. almita_status.json reflects one unified identity.
    assert status["acquisition"]["session_id"] == session_id
    assert status["acquisition"]["state"] == "RUNNING"
    assert status["acquisition"]["points_success"] == 2
    assert status["quicklook"]["state"] == "OK"
    assert status["quicklook"]["points_processed"] == 2
    assert status["quicklook"]["spectrum_available"] is True
    assert status["quicklook"]["waterfall_available"] is True
    assert status["quicklook"]["map_available"] is True
    assert status["instrument"]["mount_device"] == "LX200 OnStep"
    on_disk = read_json_safe(runtime_dir / "almita_status.json")
    assert on_disk["acquisition"]["session_id"] == session_id


# ---------------------------------------------------------------- §7: quicklook product UX (read-only symlink)

def test_watcher_refreshes_quicklook_products_symlink_to_current_session(tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    quicklook_root = tmp_path / "quicklook_out"
    quicklook_root.mkdir()
    (quicklook_root / "latest_spectrum.png").write_bytes(b"png")
    atomic_write_json(runtime_dir / "quicklook_announcement.json",
                       {"schema_version": 1, "session_id": "A", "quicklook_root": str(quicklook_root),
                        "updated_utc": "2026-08-29T22:00:00+00:00"})
    watcher.refresh_quicklook_products_link(runtime_dir, "A")
    link = runtime_dir / "quicklook_products"
    assert link.is_symlink()
    assert link.resolve() == quicklook_root.resolve()
    assert (link / "latest_spectrum.png").read_bytes() == b"png"  # no copy - same bytes, zero-copy


def test_watcher_does_not_link_products_from_a_different_session(tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    quicklook_root = tmp_path / "quicklook_out"
    quicklook_root.mkdir()
    atomic_write_json(runtime_dir / "quicklook_announcement.json",
                       {"schema_version": 1, "session_id": "B", "quicklook_root": str(quicklook_root),
                        "updated_utc": "2026-08-29T22:00:00+00:00"})
    watcher.refresh_quicklook_products_link(runtime_dir, "A")  # current session is A, announcement is B
    assert not (runtime_dir / "quicklook_products").exists()


def test_watcher_link_refresh_is_noop_safe_without_announcement(tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    watcher.refresh_quicklook_products_link(runtime_dir, None)  # must not raise
    assert not (runtime_dir / "quicklook_products").exists()


# ---------------------------------------------------------------- §10: no second INDI reader

def test_watcher_and_server_never_import_or_reference_indi():
    for path in ("almita_console_watcher.py", "almita_console_server.py"):
        source = Path(path).read_text()
        for token in ("indi_telescope_control", "INDITelescopeControl", "simple_indi_client",
                      "import pyindi", "indiserver"):
            assert token not in source, f"{path} unexpectedly references {token!r}"
