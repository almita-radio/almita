"""Tests for the hardware self-test harness's own pure/testable logic:
final-verdict combination, write auditing, and mode parsing. Does not touch
real or simulated hardware I/O paths (those are exercised by actually
running the script with --backend simulated, as already rehearsed before
every real-hardware invocation).
"""
import asyncio

import pytest

import hw_tracking_selftest as hts
from alignment_engine.tracking import SimulatedTrackingBackend, TrackingMode


def test_mode_change_correct_and_motion_safe_is_pass():
    verdict = hts.compute_final_verdict(
        step_error=None, solar_confirmed=True, restore_confirmed=True,
        on_coord_set_unchanged=True, unexpected_write_count=0, motion_verdict="PASS",
    )
    assert verdict == "PASS"


def test_restore_mismatch_is_fail():
    verdict = hts.compute_final_verdict(
        step_error=None, solar_confirmed=True, restore_confirmed=False,
        on_coord_set_unchanged=True, unexpected_write_count=0, motion_verdict="PASS",
    )
    assert verdict == "FAIL"


def test_unexpected_indi_write_is_fail_even_if_everything_else_passed():
    verdict = hts.compute_final_verdict(
        step_error=None, solar_confirmed=True, restore_confirmed=True,
        on_coord_set_unchanged=True, unexpected_write_count=1, motion_verdict="PASS",
    )
    assert verdict == "FAIL"


def test_step_error_is_fail():
    verdict = hts.compute_final_verdict(
        step_error="RuntimeError: boom", solar_confirmed=True, restore_confirmed=True,
        on_coord_set_unchanged=True, unexpected_write_count=0, motion_verdict="PASS",
    )
    assert verdict == "FAIL"


def test_on_coord_set_changed_is_fail():
    verdict = hts.compute_final_verdict(
        step_error=None, solar_confirmed=True, restore_confirmed=True,
        on_coord_set_unchanged=False, unexpected_write_count=0, motion_verdict="PASS",
    )
    assert verdict == "FAIL"


def test_motion_fail_overrides_everything_else_passing():
    verdict = hts.compute_final_verdict(
        step_error=None, solar_confirmed=True, restore_confirmed=True,
        on_coord_set_unchanged=True, unexpected_write_count=0, motion_verdict="FAIL",
    )
    assert verdict == "FAIL"


def test_motion_inconclusive_downgrades_an_otherwise_clean_pass():
    verdict = hts.compute_final_verdict(
        step_error=None, solar_confirmed=True, restore_confirmed=True,
        on_coord_set_unchanged=True, unexpected_write_count=0, motion_verdict="INCONCLUSIVE",
    )
    assert verdict == "INCONCLUSIVE"


def test_inconclusive_never_masks_a_hard_fail():
    verdict = hts.compute_final_verdict(
        step_error=None, solar_confirmed=False, restore_confirmed=True,
        on_coord_set_unchanged=True, unexpected_write_count=0, motion_verdict="INCONCLUSIVE",
    )
    assert verdict == "FAIL"


def test_mode_from_switch_vector():
    assert hts._mode_from_switch_vector(
        {"TRACK_SIDEREAL": "On", "TRACK_SOLAR": "Off", "TRACK_LUNAR": "Off", "TRACK_CUSTOM": "Off"}
    ) == TrackingMode.SIDEREAL
    assert hts._mode_from_switch_vector({}) is None
    assert hts._mode_from_switch_vector(None) is None
    assert hts._mode_from_switch_vector({"TRACK_SIDEREAL": "Off", "TRACK_SOLAR": "Off"}) is None


def test_write_auditing_backend_records_only_real_writes_and_correct_elements():
    async def _run():
        inner = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
        audited = hts._WriteAuditingBackend(inner)
        await audited.set_tracking_mode(TrackingMode.SOLAR)
        await audited.get_tracking_mode()  # must NOT be recorded as a write
        await audited.set_tracking_mode(TrackingMode.SIDEREAL)
        return audited.audit()

    audit = asyncio.run(_run())
    assert audit["write_count"] == 2
    assert audit["properties_written"] == ["TELESCOPE_TRACK_MODE"]
    assert audit["elements_written"] == ["TRACK_SIDEREAL", "TRACK_SOLAR"]
    assert audit["unexpected_write_count"] == 0


def test_write_audit_flags_a_property_outside_the_whitelist():
    audited = hts._WriteAuditingBackend(SimulatedTrackingBackend(TrackingMode.SIDEREAL))
    audited.writes.append({"property": "EQUATORIAL_EOD_COORD", "elements": {"RA": "12.0"}})
    audit = audited.audit()
    assert audit["unexpected_write_count"] == 1
    assert audit["unexpected_writes"][0]["property"] == "EQUATORIAL_EOD_COORD"


def test_full_simulated_rehearsal_reaches_pass_end_to_end():
    """The exact rehearsal path required before every --backend real
    invocation - must reach PASS (not crash, not silently swallow an
    exception) using only the simulated backend."""
    parser = hts.build_parser()
    args = parser.parse_args(["--backend", "simulated", "--stability-wait-s", "0.01"])
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        args.session_root = tmp
        exit_code = asyncio.run(hts.main(args))
    assert exit_code == 0
