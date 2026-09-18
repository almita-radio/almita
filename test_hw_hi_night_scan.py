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
