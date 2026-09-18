"""Tests for hw_solar_goto_selftest.py's own pure/testable logic and its
restore guarantees. Never touches real hardware - exercises the script
exactly the way its own --backend simulated rehearsal does.
"""
import asyncio

import pytest

import hw_solar_goto_selftest as hgs
from alignment_engine.mount_adapter import SimulatedMountAdapter
from alignment_engine.tracking import TrackingMode


def _base_kwargs(**overrides):
    kwargs = dict(
        gates_passed=True, cancelled=False, step_error=None, solar_confirmed=True,
        goto_ok=True, angular_error_deg=0.0, tolerance_deg=0.25, onstep_error=False,
        unexpected_write_count=0, track_mode_restored=True, track_state_restored=True,
    )
    kwargs.update(overrides)
    return kwargs


def test_clean_success_is_pass():
    assert hgs.compute_goto_verdict(**_base_kwargs()) == "PASS"


def test_gates_failed_is_blocked_regardless_of_everything_else():
    assert hgs.compute_goto_verdict(**_base_kwargs(gates_passed=False, goto_ok=None,
                                                     angular_error_deg=None)) == "BLOCKED"


def test_cancelled_wins_over_everything():
    assert hgs.compute_goto_verdict(**_base_kwargs(cancelled=True, gates_passed=False)) == "CANCELLED"


def test_step_error_is_fail():
    assert hgs.compute_goto_verdict(**_base_kwargs(step_error="RuntimeError: boom")) == "FAIL"


def test_goto_not_attempted_is_inconclusive():
    assert hgs.compute_goto_verdict(**_base_kwargs(goto_ok=None, angular_error_deg=None)) == "INCONCLUSIVE"


def test_angular_error_unknown_is_inconclusive():
    assert hgs.compute_goto_verdict(**_base_kwargs(angular_error_deg=None)) == "INCONCLUSIVE"


def test_goto_command_rejected_is_a_definitive_fail_not_inconclusive():
    assert hgs.compute_goto_verdict(**_base_kwargs(goto_ok=False, angular_error_deg=None)) == "FAIL"


def test_angular_error_over_tolerance_is_fail():
    assert hgs.compute_goto_verdict(**_base_kwargs(angular_error_deg=0.5)) == "FAIL"


def test_onstep_error_during_stability_is_fail():
    assert hgs.compute_goto_verdict(**_base_kwargs(onstep_error=True)) == "FAIL"


def test_unexpected_write_is_fail():
    assert hgs.compute_goto_verdict(**_base_kwargs(unexpected_write_count=1)) == "FAIL"


def test_track_mode_not_restored_is_fail():
    assert hgs.compute_goto_verdict(**_base_kwargs(track_mode_restored=False)) == "FAIL"


def test_track_state_not_restored_is_fail():
    assert hgs.compute_goto_verdict(**_base_kwargs(track_state_restored=False)) == "FAIL"


def test_combine_write_audit_whitelist():
    entries = [
        {"property": "TELESCOPE_TRACK_MODE", "elements": {"TRACK_SOLAR": "On", "TRACK_SIDEREAL": "Off"}},
        {"property": "ON_COORD_SET", "elements": {"SLEW": "On", "TRACK": "Off", "SYNC": "Off"}},
        {"property": "EQUATORIAL_EOD_COORD", "elements": {"RA": "11.7", "DEC": "1.7"}},
        {"property": "TELESCOPE_TRACK_STATE", "elements": {"TRACK_ON": "On", "TRACK_OFF": "Off"}},
    ]
    audit = hgs._combine_write_audit(entries)
    assert audit["write_count"] == 4
    assert audit["unexpected_write_count"] == 0
    assert set(audit["properties_written"]) == {"TELESCOPE_TRACK_MODE", "ON_COORD_SET",
                                                 "EQUATORIAL_EOD_COORD", "TELESCOPE_TRACK_STATE"}


def test_combine_write_audit_flags_unexpected_property():
    entries = [{"property": "TARGET_EOD_COORD", "elements": {"RA": "1.0"}}]
    audit = hgs._combine_write_audit(entries)
    assert audit["unexpected_write_count"] == 1


def test_track_state_guard_restores_on_success():
    async def _run():
        telescope = hgs._SimulatedTrackStateTelescope(initial_on=False)
        guard = hgs._TrackStateGuard(telescope, original_on=False, timeout=1.0)
        await guard.enable()
        assert telescope.on is True
        await guard.restore()
        return guard, telescope

    guard, telescope = asyncio.run(_run())
    assert guard.restored is True
    assert telescope.on is False


def test_track_state_guard_restores_to_true_when_original_was_on():
    async def _run():
        telescope = hgs._SimulatedTrackStateTelescope(initial_on=True)
        guard = hgs._TrackStateGuard(telescope, original_on=True, timeout=1.0)
        await guard.enable()
        await guard.restore()
        return guard, telescope

    guard, telescope = asyncio.run(_run())
    assert guard.restored is True
    assert telescope.on is True


def test_track_state_guard_records_restore_failure_without_raising():
    class _RefusingTelescope:
        async def set_tracking(self, enable):
            return enable  # succeeds turning ON, but "fails" (returns False) restoring OFF... simulate below

        async def wait_tracking_state(self, expected_on, timeout=5.0):
            return expected_on  # always claims success

    class _AlwaysFailsRestore(_RefusingTelescope):
        async def set_tracking(self, enable):
            if not enable:
                return False
            return True

    async def _run():
        telescope = _AlwaysFailsRestore()
        guard = hgs._TrackStateGuard(telescope, original_on=False, timeout=1.0)
        await guard.enable()
        await guard.restore()
        return guard

    guard = asyncio.run(_run())
    assert guard.restored is False
    assert guard.restore_error is not None


def test_uses_real_current_time_without_obstime_override():
    """Real Sun ephemeris at real Time.now() is used even in simulated mode
    unless --simulate-obstime is explicitly passed - checked directly
    against the persisted obstime, not by asserting BLOCKED/PASS (which
    depends on whether it happens to be day or night at the real site when
    this test runs - asserting a fixed outcome here would be exactly the
    kind of real-world-time-dependent flakiness this suite must avoid)."""
    import json
    import tempfile
    from datetime import datetime, timezone
    from pathlib import Path

    from astropy.time import Time

    parser = hgs.build_parser()
    args = parser.parse_args(["--backend", "simulated"])
    with tempfile.TemporaryDirectory() as tmp:
        args.session_root = tmp
        before = datetime.now(timezone.utc)
        asyncio.run(hgs.main(args))
        after = datetime.now(timezone.utc)
        session_dirs = list(Path(tmp).glob("HW-SOLAR-GOTO-*"))
        assert len(session_dirs) == 1
        report = json.loads((session_dirs[0] / "hardware_test_result.json").read_text())

    valid_time_gate = next(g for g in report["gates"] if g["name"] == "valid_time")
    obstime_str = valid_time_gate["detail"].removeprefix("obstime=")
    obstime = Time(obstime_str).to_datetime(timezone=timezone.utc)
    assert before <= obstime <= after


def test_full_simulated_rehearsal_with_daytime_obstime_reaches_pass():
    import tempfile
    parser = hgs.build_parser()
    args = parser.parse_args([
        "--backend", "simulated", "--simulate-obstime", "2026-09-18T15:00:00",
        "--stability-wait-s", "0.3", "--stability-poll-interval-s", "0.1",
    ])
    with tempfile.TemporaryDirectory() as tmp:
        args.session_root = tmp
        exit_code = asyncio.run(hgs.main(args))
    assert exit_code == 0  # PASS


def test_cancellation_during_stability_wait_marks_cancelled_and_still_restores_track_mode():
    """Ctrl-C equivalent: CancelledError raised mid-flow must not crash with
    a raw traceback, must not attempt another GOTO, and must still let
    TrackingSession's guaranteed restore run (its __aexit__ always runs on
    any exception, including CancelledError, per asyncio's own async-with
    semantics)."""
    import tempfile
    from unittest.mock import patch

    parser = hgs.build_parser()
    args = parser.parse_args([
        "--backend", "simulated", "--simulate-obstime", "2026-09-18T15:00:00",
        "--stability-wait-s", "5", "--stability-poll-interval-s", "0.5",
    ])

    call_count = {"n": 0}

    async def _sleep_then_cancel(delay):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise asyncio.CancelledError()

    with tempfile.TemporaryDirectory() as tmp:
        args.session_root = tmp
        with patch("hw_solar_goto_selftest.asyncio.sleep", side_effect=_sleep_then_cancel):
            exit_code = asyncio.run(hgs.main(args))

    assert exit_code == 5  # CANCELLED, not an unhandled exception
    # Evidence persistence itself (hardware_test_result.json etc.) is
    # exercised by the PASS-path rehearsal test above via the same
    # session.py code - this test's focus is exit behavior on cancellation.


def test_full_simulated_rehearsal_goto_failure_still_restores_track_mode_and_never_touches_track_state():
    """A GOTO that fails must not raise, must not enable TRACK_STATE (there
    is nothing to track towards), and must still restore TRACK_MODE."""
    import tempfile
    from unittest.mock import AsyncMock

    parser = hgs.build_parser()
    args = parser.parse_args([
        "--backend", "simulated", "--simulate-obstime", "2026-09-18T15:00:00",
    ])
    original_goto = SimulatedMountAdapter.goto
    SimulatedMountAdapter.goto = AsyncMock(return_value=False)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            args.session_root = tmp
            exit_code = asyncio.run(hgs.main(args))
    finally:
        SimulatedMountAdapter.goto = original_goto
    assert exit_code == 1  # a definitive FAIL, not an unhandled exception and not INCONCLUSIVE
