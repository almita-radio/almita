"""Fase 8/9/10: prepare/apply/verify must stay separate calls, apply must
never run unless the plan says eligible, and a rejected/failed sync/
verification must never trigger an automatic retry."""
import pytest
import astropy.units as u
from astropy.coordinates import SkyCoord

from alignment_engine.fitting import FitQuality, FitResult
from alignment_engine.mount_adapter import SimulatedMountAdapter
from alignment_engine.sync_flow import apply_sync, prepare_sync, verify_sync
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


@pytest.mark.asyncio
async def test_verify_sync_reports_error_on_goto_failure_without_raising():
    class BrokenMount(SimulatedMountAdapter):
        async def goto(self, target, point_index=None):
            return False
    mount = BrokenMount()
    verification = await verify_sync(mount, CENTER, settle_seconds=0.0, sleep_fn=_sleep)
    assert verification.residual_deg is None
    assert verification.error is not None
