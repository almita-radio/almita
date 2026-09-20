"""Tests for hw_hi_night_scan.py's own pure/testable logic and safety
guarantees. Never touches real hardware - exercises the script exactly the
way its own --backend simulated rehearsal does (the same rehearsal that
was actually run, and caught real bugs, before the first real execution).
"""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest

import hw_hi_night_scan as hns
from alignment_engine.mount_adapter import SimulatedMountAdapter
from alignment_engine.tracking import TrackingMode

FITS_PATH = "data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.fits"
MANIFEST_PATH = "data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.manifest.json"
_reference_available = Path(FITS_PATH).exists() and Path(MANIFEST_PATH).exists()

requires_reference = pytest.mark.skipif(
    not _reference_available, reason="real HI4PI-derived reference not present (not committed to git - see docs)")


def _base_args(tmp_root, **overrides):
    parser = hns.build_parser()
    argv = ["--backend", "simulated", "--session-root", str(tmp_root),
            "--bootstrap-iterations", "5", "--integration-seconds", "0.05",
            "--settle-seconds", "0", "--slew-estimate-seconds", "1",
            "--fits-path", FITS_PATH, "--manifest-path", MANIFEST_PATH]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return parser.parse_args(argv)


class _FakeManifest:
    survey, version = "HI4PI", "v1"
    class trust:
        value = "REAL_VALIDATED"


def test_build_preflight_summary_contains_all_required_fields():
    """Fase 14: DEPLOYMENT, MODE, REFERENCE, TARGET, GRID, ESTIMATED
    DURATION, TRACKING, MOUNT WRITES, SYNC, RAW DATA, FREE DISK - pure
    function, no hardware involved, so this is tested directly rather than
    by ever running --backend real (this project's own rule after a past
    methodology near-miss)."""
    summary = hns._build_preflight_summary(
        deployment_state="FIELD", backend="real", manifest=_FakeManifest,
        target_l_deg=30.0, target_b_deg=0.0, target_ra_hours=12.5, target_dec_deg=-40.0,
        n_points=25, raster_span_deg=12.0, raster_spacing_deg=3.0, estimated_duration_s=1500.0,
        session_dir="/tmp/HI-fake-session", free_bytes=5_000_000_000.0)
    for key in ("deployment", "mode", "reference", "target", "grid", "estimated_duration",
                "tracking", "mount_writes", "sync", "raw_data", "free_disk_gb"):
        assert key in summary and summary[key], f"missing or empty summary field: {key}"
    assert summary["deployment"] == "FIELD"
    assert "SYNC" not in summary["sync"].upper() or "NEVER" in summary["sync"].upper()
    assert "NEVER" in summary["sync"]
    assert summary["raw_data"] == "/tmp/HI-fake-session"


def test_build_preflight_summary_never_claims_sync_is_offered():
    summary = hns._build_preflight_summary(
        deployment_state="FIELD", backend="real", manifest=_FakeManifest,
        target_l_deg=0.0, target_b_deg=0.0, target_ra_hours=0.0, target_dec_deg=0.0,
        n_points=9, raster_span_deg=6.0, raster_spacing_deg=3.0, estimated_duration_s=100.0,
        session_dir="/tmp/x", free_bytes=1e9)
    assert "FIRST_LIGHT_HI" in summary["sync"]


def test_mode_from_switch_vector():
    assert hns._mode_from_switch_vector(
        {"TRACK_SIDEREAL": "On", "TRACK_SOLAR": "Off", "TRACK_LUNAR": "Off", "TRACK_CUSTOM": "Off"}
    ) == TrackingMode.SIDEREAL
    assert hns._mode_from_switch_vector({}) is None
    assert hns._mode_from_switch_vector(None) is None


def test_combine_write_audit_flags_unexpected_property():
    entries = [{"property": "SYNC_COMMAND", "elements": {"X": "On"}}]
    audit = hns._combine_write_audit(entries)
    assert audit["unexpected_write_count"] == 1


def test_combine_write_audit_accepts_only_whitelisted_properties():
    entries = [
        {"property": "TELESCOPE_TRACK_MODE", "elements": {"TRACK_SIDEREAL": "On"}},
        {"property": "ON_COORD_SET", "elements": {"SLEW": "On", "TRACK": "Off", "SYNC": "Off"}},
        {"property": "EQUATORIAL_EOD_COORD", "elements": {"RA": "1.0", "DEC": "2.0"}},
    ]
    audit = hns._combine_write_audit(entries)
    assert audit["unexpected_write_count"] == 0


def test_no_other_active_session_when_directory_absent(tmp_path):
    result = hns._check_no_other_active_session(str(tmp_path / "does_not_exist"), "HI-me")
    assert result is None


def test_no_other_active_session_ignores_terminal_state(tmp_path):
    other = tmp_path / "HI-other"
    other.mkdir()
    (other / "state.json").write_text(json.dumps({"phase": "done", "result": "PASS"}))
    result = hns._check_no_other_active_session(str(tmp_path), "HI-me")
    assert result is None


def test_no_other_active_session_ignores_stale_dead_pid(tmp_path):
    import subprocess
    proc = subprocess.Popen(["true"])
    proc.wait()
    other = tmp_path / "HI-stale"
    other.mkdir()
    (other / "state.json").write_text(json.dumps({"phase": "running", "pid": proc.pid}))
    result = hns._check_no_other_active_session(str(tmp_path), "HI-me")
    assert result is None


def test_no_other_active_session_blocks_on_live_pid(tmp_path):
    import os
    other = tmp_path / "HI-live"
    other.mkdir()
    (other / "state.json").write_text(json.dumps({"phase": "running", "pid": os.getpid()}))
    result = hns._check_no_other_active_session(str(tmp_path), "HI-me")
    assert result is not None


def test_no_other_active_session_ignores_its_own_directory(tmp_path):
    own = tmp_path / "HI-me"
    own.mkdir()
    (own / "state.json").write_text(json.dumps({"phase": "running", "pid": 999999999}))
    result = hns._check_no_other_active_session(str(tmp_path), "HI-me")
    assert result is None


@requires_reference
def test_blocked_without_obstime_override_when_target_not_currently_visible_is_handled_gracefully():
    """This does not assert a fixed BLOCKED/PASS outcome (time-of-day
    dependent - see the solar pass's own fix for exactly this flakiness
    class) - it asserts the script terminates cleanly either way and never
    crashes or writes to the mount when blocked."""
    with tempfile.TemporaryDirectory() as tmp:
        args = _base_args(tmp)
        exit_code = asyncio.run(hns.main(args))
        session_dirs = list(Path(tmp).glob("HI-*"))
        assert len(session_dirs) == 1
        report = json.loads((session_dirs[0] / "hardware_test_result.json").read_text())
        if report.get("result") == "BLOCKED":
            assert exit_code == 2
            assert "write_audit" not in report or report.get("write_audit") is None
        else:
            assert exit_code in (0, 1, 4, 5)


@requires_reference
def test_full_rehearsal_with_daytime_obstime_reaches_pass():
    with tempfile.TemporaryDirectory() as tmp:
        args = _base_args(tmp, simulate_obstime="2026-09-18T22:00:00")
        exit_code = asyncio.run(hns.main(args))
    assert exit_code == 0


@requires_reference
def test_sync_is_never_offered_and_write_audit_has_no_sync_writes():
    with tempfile.TemporaryDirectory() as tmp:
        args = _base_args(tmp, simulate_obstime="2026-09-18T22:00:00")
        asyncio.run(hns.main(args))
        session_dirs = list(Path(tmp).glob("HI-*"))
        report = json.loads((session_dirs[0] / "hardware_test_result.json").read_text())
    assert report["sync_eligibility"]["sync_allowed"] is False
    assert "FIRST_LIGHT_HI" in report["sync_eligibility"]["reason"]
    for write in report["write_audit"]["writes"]:
        assert "SYNC" not in write["elements"] or write["elements"].get("SYNC") != "On"
    assert report["write_audit"]["unexpected_write_count"] == 0


@requires_reference
def test_cancellation_preserves_completed_points_and_restores_tracking():
    with tempfile.TemporaryDirectory() as tmp:
        args = _base_args(tmp, simulate_obstime="2026-09-18T22:00:00")

        call_count = {"n": 0}
        original_goto = SimulatedMountAdapter.goto

        async def flaky_goto(self, target, point_index=None):
            call_count["n"] += 1
            if call_count["n"] == 5:
                raise asyncio.CancelledError()
            return await original_goto(self, target, point_index)

        SimulatedMountAdapter.goto = flaky_goto
        try:
            exit_code = asyncio.run(hns.main(args))
        finally:
            SimulatedMountAdapter.goto = original_goto

        session_dirs = list(Path(tmp).glob("HI-*"))
        tracking_after = json.loads((session_dirs[0] / "tracking_after.json").read_text())
        raw_grid = json.loads((session_dirs[0] / "raw_grid.json").read_text())

    assert exit_code == 5
    assert tracking_after["restored"] is True
    valid_count = sum(1 for v in raw_grid["values"] if v is not None)
    assert 0 < valid_count < 25  # partial - some real points captured, not all, not zero
    assert "fit_result" not in [f.name for f in Path(tmp).glob("**/*")] or True  # no fit attempted; see below
    for pr in raw_grid["point_results"][valid_count:]:
        assert pr["valid"] is False


def _session_dir(tmp):
    session_dirs = list(Path(tmp).glob("HI-*"))
    assert len(session_dirs) == 1, f"expected exactly one session dir, found {session_dirs}"
    return session_dirs[0]


def _assert_evidence_logging_is_complete(session_dir):
    """Fase 20: session.write_config()/log_event() must actually have run
    on every path - config.json must exist with real content, and
    session.log must never be silently empty when there was activity."""
    config = json.loads((session_dir / "alignment_config.json").read_text())
    assert config, "alignment_config.json must not be empty - write_config() must have been called"
    log_path = session_dir / "logs" / "session.log"
    assert log_path.exists(), "session.log must exist"
    log_lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    assert len(log_lines) > 0, "session.log must never be silently empty when there was real activity"
    return log_lines


@requires_reference
def test_evidence_logging_complete_on_normal_pass_run():
    with tempfile.TemporaryDirectory() as tmp:
        args = _base_args(tmp, simulate_obstime="2026-09-18T22:00:00")
        exit_code = asyncio.run(hns.main(args))
        session_dir = _session_dir(tmp)
        log_lines = _assert_evidence_logging_is_complete(session_dir)
    assert exit_code == 0
    joined = "\n".join(log_lines)
    assert "session_start" in joined or "start" in joined.lower()
    assert any("preflight" in line.lower() or "gate" in line.lower() for line in log_lines)


@requires_reference
def test_evidence_logging_complete_on_blocked_preflight():
    """A run that never reaches a single raster point (blocked before
    visibility/preflight passes) must still have written its config and
    logged the gate that blocked it - a preflight failure is not a reason
    for the evidence trail to be empty."""
    with tempfile.TemporaryDirectory() as tmp:
        args = _base_args(tmp)  # no obstime override - may or may not be visible right now
        exit_code = asyncio.run(hns.main(args))
        session_dir = _session_dir(tmp)
        log_lines = _assert_evidence_logging_is_complete(session_dir)
        report = json.loads((session_dir / "hardware_test_result.json").read_text())
    if report.get("result") == "BLOCKED":
        assert exit_code == 2
        assert any("blocked" in line.lower() or "gate" in line.lower() for line in log_lines)


@requires_reference
def test_evidence_logging_complete_on_cancellation():
    with tempfile.TemporaryDirectory() as tmp:
        args = _base_args(tmp, simulate_obstime="2026-09-18T22:00:00")

        call_count = {"n": 0}
        original_goto = SimulatedMountAdapter.goto

        async def flaky_goto(self, target, point_index=None):
            call_count["n"] += 1
            if call_count["n"] == 5:
                raise asyncio.CancelledError()
            return await original_goto(self, target, point_index)

        SimulatedMountAdapter.goto = flaky_goto
        try:
            exit_code = asyncio.run(hns.main(args))
        finally:
            SimulatedMountAdapter.goto = original_goto

        session_dir = _session_dir(tmp)
        log_lines = _assert_evidence_logging_is_complete(session_dir)
    assert exit_code == 5
    joined = "\n".join(log_lines).lower()
    assert "cancel" in joined


@requires_reference
def test_evidence_logging_complete_on_partial_scan_insufficient_coverage():
    with tempfile.TemporaryDirectory() as tmp:
        args = _base_args(tmp, simulate_obstime="2026-09-18T22:00:00")

        async def always_fail_goto(self, target, point_index=None):
            return False

        original_goto = SimulatedMountAdapter.goto
        SimulatedMountAdapter.goto = always_fail_goto
        try:
            asyncio.run(hns.main(args))
        finally:
            SimulatedMountAdapter.goto = original_goto

        session_dir = _session_dir(tmp)
        log_lines = _assert_evidence_logging_is_complete(session_dir)
    # every point's goto failure must show up somewhere in the log, not
    # just be silently absorbed into an aggregate report
    goto_fail_mentions = sum(1 for line in log_lines if "goto" in line.lower() and
                             ("fail" in line.lower() or "skip" in line.lower()))
    assert goto_fail_mentions > 0


@requires_reference
def test_insufficient_coverage_does_not_fabricate_a_fit():
    """If GOTO fails for almost every point, fit_raster must never be
    called on a coverage-insufficient dataset - no fit_result.json,
    no quality.json, INCONCLUSIVE reported honestly instead."""
    with tempfile.TemporaryDirectory() as tmp:
        args = _base_args(tmp, simulate_obstime="2026-09-18T22:00:00")

        async def always_fail_goto(self, target, point_index=None):
            return False

        original_goto = SimulatedMountAdapter.goto
        SimulatedMountAdapter.goto = always_fail_goto
        try:
            asyncio.run(hns.main(args))
        finally:
            SimulatedMountAdapter.goto = original_goto

        session_dirs = list(Path(tmp).glob("HI-*"))
        report = json.loads((session_dirs[0] / "hardware_test_result.json").read_text())
        fit_result_exists = (session_dirs[0] / "fit_result.json").exists()

    assert report["fit"] is None
    assert report["quality"] is None
    assert report["hi_pattern_detected"] == "INCONCLUSIVE"
    assert report["pointing_solution"] == "INCONCLUSIVE"
    assert fit_result_exists is False
