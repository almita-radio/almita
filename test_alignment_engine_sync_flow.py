"""Fase 8/9/10: prepare/apply/verify must stay separate calls, apply must
never run unless the plan says eligible, and a rejected/failed sync/
verification must never trigger an automatic retry."""
import pytest
import astropy.units as u
from astropy.coordinates import SkyCoord

from astropy.time import Time

from alignment_engine.fitting import FitQuality, FitResult
from alignment_engine.mount_adapter import SimulatedMountAdapter
from alignment_engine.sync_flow import apply_sync, describe_real_indi_sync_operations, prepare_sync, verify_sync
from alignment import AlignmentEstimate

CENTER = SkyCoord(ra=100 * u.deg, dec=20 * u.deg)


def _fit_result(offset_ra=0.5, offset_dec=-0.3, confidence=0.9, score=0.95, residual=0.05):
    estimate = AlignmentEstimate(offset_ra, offset_dec, 0.6, confidence, residual, score, 30)
    quality = FitQuality(confidence=confidence, correlation=score, residual_rms=residual,
                          valid_fraction=1.0, peak_contrast=0.5, distance_from_edge_deg=2.0,
                          rating="GOOD" if confidence > 0.75 else "MARGINAL")
    return FitResult(estimate=estimate, quality=quality, raw_values=[1.0] * 30,
                      valid_count=30, total_count=30, rejected_indices=[])


async def _sleep(_seconds):  # instant no-op sleep for tests
    return None


def test_prepare_sync_is_pure_and_never_touches_a_mount():
    result = _fit_result()
    plan = prepare_sync("SOLAR", result, CENTER, is_observational=True, tracking_mode="SOLAR",
                         confidence_threshold=0.65)
    assert plan.eligible is True
    assert plan.sync_command_ra_hours == pytest.approx(CENTER.ra.hour)
    # compensated GOTO should be the inverse of the measured offset
    assert plan.compensated_goto_ra_hours != plan.sync_command_ra_hours


def test_prepare_sync_blocks_when_reference_is_not_observational():
    result = _fit_result(confidence=0.95)
    plan = prepare_sync("HI", result, CENTER, is_observational=False, tracking_mode="SIDEREAL")
    assert plan.eligible is False
    assert "not observational" in plan.eligibility_reason


def test_prepare_sync_blocks_on_low_confidence():
    result = _fit_result(confidence=0.2)
    plan = prepare_sync("SOLAR", result, CENTER, is_observational=True, tracking_mode="SOLAR",
                         confidence_threshold=0.65)
    assert plan.eligible is False
    assert "confidence" in plan.eligibility_reason


@pytest.mark.asyncio
async def test_apply_sync_refuses_when_plan_not_eligible_even_if_called():
    result = _fit_result(confidence=0.1)
    plan = prepare_sync("SOLAR", result, CENTER, is_observational=True, tracking_mode="SOLAR")
    mount = SimulatedMountAdapter()
    sync_result = await apply_sync(plan, mount, settle_seconds=0.0, sleep_fn=_sleep)
    assert sync_result.applied is False
    assert "not eligible" in sync_result.error
    assert mount.synced_to is None  # never touched


@pytest.mark.asyncio
async def test_apply_sync_succeeds_against_simulated_mount():
    result = _fit_result(confidence=0.9)
    plan = prepare_sync("SOLAR", result, CENTER, is_observational=True, tracking_mode="SOLAR",
                         confidence_threshold=0.65)
    mount = SimulatedMountAdapter()
    sync_result = await apply_sync(plan, mount, settle_seconds=0.0, sleep_fn=_sleep)
    assert sync_result.applied is True
    assert mount.synced_to is not None
    assert sync_result.pre_sync_ra_hours is not None
    assert sync_result.post_sync_ra_hours == pytest.approx(CENTER.ra.hour)


@pytest.mark.asyncio
async def test_apply_sync_reports_goto_failure_without_raising():
    result = _fit_result(confidence=0.9)
    plan = prepare_sync("SOLAR", result, CENTER, is_observational=True, tracking_mode="SOLAR",
                         confidence_threshold=0.65)
    mount = SimulatedMountAdapter()

    async def always_fail_goto(target, point_index=None):
        return False
    mount.goto = always_fail_goto  # simulate a driver-level GOTO failure
    sync_result = await apply_sync(plan, mount, settle_seconds=0.0, sleep_fn=_sleep)
    assert sync_result.applied is False
    assert "GOTO failed" in sync_result.error


@pytest.mark.asyncio
async def test_verify_sync_measures_residual_via_repeatability_check():
    mount = SimulatedMountAdapter()
    await mount.connect()
    verification = await verify_sync(mount, CENTER, settle_seconds=0.0, sleep_fn=_sleep)
    assert verification.residual_deg is not None
    assert verification.residual_deg < 0.01  # simulated mount always lands exactly on target
    assert verification.error is None


@pytest.mark.asyncio
async def test_verify_sync_flags_worse_residual_without_auto_retrying():
    class DriftingMountAdapter(SimulatedMountAdapter):
        async def goto(self, target, point_index=None):
            drifted = SkyCoord(ra=target.ra + 0.5 * u.deg, dec=target.dec)
            self.current = drifted
            return True

    mount = DriftingMountAdapter()
    verification = await verify_sync(mount, CENTER, settle_seconds=0.0, sleep_fn=_sleep,
                                      residual_before_deg=0.05)
    assert verification.residual_deg > 0.05
    assert verification.improved_vs_before is False
    # verify_sync itself must never call goto/sync again beyond the one
    # documented repeatability check - confirmed by construction (it has
    # no loop/retry branch), not by mocking call counts here.


# ------------------------------------------------------- item 7: real SYNC preview


def test_describe_real_indi_sync_operations_matches_the_real_driver_sequence():
    """Mirrors indi_telescope_control.py::INDITelescopeControl.sync()'s own
    real 3-step wire sequence exactly (ON_COORD_SET->SYNC,
    EQUATORIAL_EOD_COORD<-real coords, ON_COORD_SET->TRACK) - pure
    description, no network I/O, so this test needs no server."""
    ops = describe_real_indi_sync_operations(CENTER, obstime=Time("2026-01-01T00:00:00"))
    assert len(ops) == 3
    assert ops[0]["property"] == "ON_COORD_SET"
    assert ops[0]["elements"] == {"TRACK": "Off", "SLEW": "Off", "SYNC": "On"}
    assert ops[1]["property"] == "EQUATORIAL_EOD_COORD"
    assert set(ops[1]["elements"]) == {"RA", "DEC"}
    assert "CIRS" in ops[1]["coordinate_frame"]
    assert ops[2]["property"] == "ON_COORD_SET"
    assert ops[2]["elements"] == {"TRACK": "On", "SLEW": "Off", "SYNC": "Off"}
    assert all(op["device"] == "LX200 OnStep" for op in ops)


def test_describe_real_indi_sync_operations_converts_icrs_to_cirs_not_identity():
    """The EQUATORIAL_EOD_COORD values must actually be in CIRS(obstime),
    not just relabeled ICRS numbers - regression guard against silently
    forgetting the frame conversion this pass's own CIRS/ICRS investigation
    is about."""
    ops = describe_real_indi_sync_operations(CENTER, obstime=Time("2026-01-01T00:00:00"))
    icrs_ra_hours = float(CENTER.ra.hour)
    eod_ra_hours = ops[1]["elements"]["RA"]
    # Precession/nutation over a multi-decade-plus baseline from J2000 must
    # produce a real, non-zero difference - not an identity passthrough.
    assert abs(eod_ra_hours - icrs_ra_hours) > 1e-4


def test_prepare_sync_always_includes_the_real_operations_preview_even_when_ineligible():
    """An operator should be able to see exactly what a real SYNC would
    send even for a plan that gets rejected (low confidence / synthetic
    HI reference) - the preview describes the mechanism, independent of
    the eligibility decision."""
    result = _fit_result(confidence=0.1)
    plan = prepare_sync("SOLAR", result, CENTER, is_observational=True, tracking_mode="SOLAR")
    assert plan.eligible is False
    assert plan.real_indi_operations is not None
    assert len(plan.real_indi_operations) == 3


@pytest.mark.asyncio
async def test_verify_sync_reports_error_on_goto_failure_without_raising():
    class BrokenMount(SimulatedMountAdapter):
        async def goto(self, target, point_index=None):
            return False
    mount = BrokenMount()
    verification = await verify_sync(mount, CENTER, settle_seconds=0.0, sleep_fn=_sleep)
    assert verification.residual_deg is None
    assert verification.error is not None
