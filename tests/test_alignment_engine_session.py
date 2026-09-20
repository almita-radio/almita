import json

from alignment_engine.session import AlignmentSession, new_session_id


def test_new_session_id_format():
    from datetime import datetime, timezone
    session_id = new_session_id("solar", datetime(2026, 9, 17, 12, 30, 45, tzinfo=timezone.utc))
    assert session_id == "SOLAR-20260917-123045"


def test_session_creates_expected_directory_layout(tmp_path):
    session = AlignmentSession(tmp_path, "SOLAR-TEST")
    assert session.dir.is_dir()
    assert (session.dir / "points").is_dir()
    assert (session.dir / "logs").is_dir()


def test_writes_are_atomic_and_readable_back(tmp_path):
    session = AlignmentSession(tmp_path, "SOLAR-TEST")
    session.write_alignment_result({"offset_ra_deg": 1.23, "status": "PASS"})
    result = session.read_alignment_result()
    assert result["offset_ra_deg"] == 1.23
    # the .tmp file used during the atomic write must never be left behind
    assert not (session.dir / "alignment_result.json.tmp").exists()


def test_raw_grid_and_fit_result_are_kept_in_separate_files(tmp_path):
    session = AlignmentSession(tmp_path, "SOLAR-TEST")
    session.write_raw_grid({"points": [1, 2, 3], "values": [10, 20, 30]})
    session.write_fit_result({"estimate": {"offset_ra_deg": 0.1}})
    assert session.read_raw_grid()["values"] == [10, 20, 30]
    assert session.read_fit_result()["estimate"]["offset_ra_deg"] == 0.1
    # never the same file - raw must never be overwritten by fitted data
    assert (session.dir / "raw_grid.json").exists()
    assert (session.dir / "fit_result.json").exists()


def test_session_survives_process_restart_by_reopening_same_directory(tmp_path):
    first = AlignmentSession(tmp_path, "SOLAR-TEST")
    first.write_alignment_result({"status": "PASS"})
    first.write_state({"state": "RESULT_READY"})
    # simulate a fresh process: brand new AlignmentSession object, same dir
    reopened = AlignmentSession(tmp_path, "SOLAR-TEST")
    assert reopened.read_alignment_result()["status"] == "PASS"
    assert reopened.read_state()["state"] == "RESULT_READY"


def test_missing_json_reads_as_none_not_an_exception(tmp_path):
    session = AlignmentSession(tmp_path, "SOLAR-TEST")
    assert session.read_alignment_result() is None
    assert session.read_sync_plan() is None


def test_log_event_writes_to_session_log_file(tmp_path):
    session = AlignmentSession(tmp_path, "SOLAR-TEST")
    session.log_event("STATE -> PLANNED")
    log_text = (session.dir / "logs" / "session.log").read_text()
    assert "STATE -> PLANNED" in log_text


def test_point_path_is_zero_padded_and_under_points_dir(tmp_path):
    session = AlignmentSession(tmp_path, "SOLAR-TEST")
    path = session.point_path(7)
    assert path.name == "point_0007.h5"
    assert path.parent.name == "points"


def test_result_json_is_never_left_partially_written(tmp_path, monkeypatch):
    """Simulate a crash mid-write (os.replace never called) and confirm the
    previous good file, if any, is untouched - atomic_write_json's own
    .tmp -> fsync -> os.replace() sequence guarantees this; this test just
    confirms the session layer doesn't bypass it."""
    session = AlignmentSession(tmp_path, "SOLAR-TEST")
    session.write_alignment_result({"status": "PASS", "revision": 1})

    import runtime_state
    original_replace = runtime_state.os.replace

    def boom(*_args, **_kwargs):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(runtime_state.os, "replace", boom)
    try:
        session.write_alignment_result({"status": "PASS", "revision": 2})
    except OSError:
        pass
    monkeypatch.setattr(runtime_state.os, "replace", original_replace)

    # the OLD, valid content must still be intact - never a half-written file
    result = session.read_alignment_result()
    assert result["revision"] == 1
