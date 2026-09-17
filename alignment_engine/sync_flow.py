"""Explicit prepare/apply/verify SYNC split (Fase 8/9/10).

This is the architectural change relative to alignment.py's existing
--apply-sync flag: there, MEASURE/FIT and "decide to sync" are fused into
one run() call, gated only by a confidence threshold checked at the moment
the flag was already true. Here, prepare_sync() only ever *describes* what
would happen - it never touches a mount - and apply_sync() is a separate
call the CLI only makes after an operator has explicitly said yes to what
prepare_sync() printed. No function in this module reads stdin; input()
lives in almita_align.py only.

Coordinate frame note: prepare_sync's compensated GOTO and the SYNC command
itself are both expressed in the same frame the mount was actually
commanded in for the scan (ICRS `center`/`compensated` SkyCoord objects are
converted to CIRS/EOD by the mount adapter itself at the moment of the
call - see mount_adapter.RealMountAdapter - never by this module, which
stays coordinate-frame-agnostic and only deals in SkyCoord objects).

HI eligibility: SyntheticHIReferenceProvider.is_observational is always
False (see targets/hi_reference.py) - prepare_sync() reads that flag and
sets eligible=False for HI unconditionally today, mirroring alignment.py's
existing "NON-OBSERVATIONAL TEMPLATE - SYNC BLOCKED" behavior exactly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from astropy.coordinates import SkyCoord
import astropy.units as u

from alignment import offset_coordinates
from .fitting import FitResult
from .mount_adapter import MountAdapter


@dataclass
class SyncPlan:
    mode: str
    eligible: bool
    eligibility_reason: str
    expected_target_ra_hours: float
    expected_target_dec_deg: float
    measured_offset_east_deg: float
    measured_offset_north_deg: float
    fit_confidence: float
    fit_rating: str
    tracking_mode: Optional[str]
    coordinate_frame: str
    compensated_goto_ra_hours: float
    compensated_goto_dec_deg: float
    sync_command_ra_hours: float
    sync_command_dec_deg: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SyncResult:
    applied: bool
    pre_sync_ra_hours: Optional[float]
    pre_sync_dec_deg: Optional[float]
    post_sync_ra_hours: Optional[float]
    post_sync_dec_deg: Optional[float]
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationResult:
    residual_deg: Optional[float]
    repeat_ra_hours: Optional[float]
    repeat_dec_deg: Optional[float]
    improved_vs_before: Optional[bool]
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def prepare_sync(mode: str, fit_result: FitResult, center: SkyCoord, is_observational: bool,
                  # `center` MUST be ICRS (not CIRS/EOD) - see
                  # targets/solar.py's module docstring: SkyOffsetFrame
                  # geometry (used here via offset_coordinates) is wrong
                  # with a CIRS origin. Convert with `.icrs` before calling.
                  tracking_mode: Optional[str], confidence_threshold: float = 0.65) -> SyncPlan:
    """Pure - computes and describes what SYNC would do. Never calls a mount."""
    estimate = fit_result.estimate
    if not is_observational:
        eligible, reason = False, "reference is not observational ground truth (synthetic model)"
    elif estimate.confidence < confidence_threshold:
        eligible, reason = False, f"confidence {estimate.confidence:.3f} below threshold {confidence_threshold:.3f}"
    else:
        eligible, reason = True, "confidence and reference eligibility checks passed"

    # If the true sky = commanded position + measured offset, the mount
    # must be told the reference is at (commanded - offset) so the beam is
    # physically centered there - same inverse-offset logic alignment.py
    # already uses (alignment.py:733-734).
    compensated = offset_coordinates(center, [-estimate.offset_ra_deg], [-estimate.offset_dec_deg])[0]

    return SyncPlan(
        mode=mode, eligible=eligible, eligibility_reason=reason,
        expected_target_ra_hours=float(center.ra.hour), expected_target_dec_deg=float(center.dec.deg),
        measured_offset_east_deg=estimate.offset_ra_deg, measured_offset_north_deg=estimate.offset_dec_deg,
        fit_confidence=estimate.confidence, fit_rating=fit_result.quality.rating,
        tracking_mode=tracking_mode, coordinate_frame="ICRS (converted to mount frame at send time)",
        compensated_goto_ra_hours=float(compensated.ra.hour), compensated_goto_dec_deg=float(compensated.dec.deg),
        sync_command_ra_hours=float(center.ra.hour), sync_command_dec_deg=float(center.dec.deg),
    )


async def apply_sync(plan: SyncPlan, mount: MountAdapter, settle_seconds: float,
                      sleep_fn) -> SyncResult:
    """Only called after the CLI has already gotten an explicit operator
    yes AND plan.eligible is True - this function itself does not gate on
    eligible again by ask; the CLI is responsible for not calling it
    otherwise. It refuses to run against a plan that is not eligible,
    as a second line of defense against a caller bug."""
    if not plan.eligible:
        return SyncResult(applied=False, pre_sync_ra_hours=None, pre_sync_dec_deg=None,
                           post_sync_ra_hours=None, post_sync_dec_deg=None,
                           error=f"refused: plan not eligible ({plan.eligibility_reason})")
    compensated = SkyCoord(ra=plan.compensated_goto_ra_hours * u.hourangle,
                            dec=plan.compensated_goto_dec_deg * u.deg)
    reference = SkyCoord(ra=plan.sync_command_ra_hours * u.hourangle,
                          dec=plan.sync_command_dec_deg * u.deg)
    try:
        if not await mount.goto(compensated):
            return SyncResult(applied=False, pre_sync_ra_hours=None, pre_sync_dec_deg=None,
                               post_sync_ra_hours=None, post_sync_dec_deg=None,
                               error="pre-SYNC compensated GOTO failed")
        await sleep_fn(settle_seconds)
        pre = await mount.get_position()
        applied = bool(await mount.sync(reference))
        post = await mount.get_position()
        return SyncResult(
            applied=applied,
            pre_sync_ra_hours=float(pre.ra.hour) if pre else None,
            pre_sync_dec_deg=float(pre.dec.deg) if pre else None,
            post_sync_ra_hours=float(post.ra.hour) if post else None,
            post_sync_dec_deg=float(post.dec.deg) if post else None,
        )
    except Exception as exc:
        return SyncResult(applied=False, pre_sync_ra_hours=None, pre_sync_dec_deg=None,
                           post_sync_ra_hours=None, post_sync_dec_deg=None, error=str(exc))


async def verify_sync(mount: MountAdapter, reference: SkyCoord, settle_seconds: float, sleep_fn,
                       outside_offset_deg: float = 2.0,
                       residual_before_deg: Optional[float] = None) -> VerificationResult:
    """Slew away by outside_offset_deg and back, then measure residual
    separation from the reference - the same repeatability check
    alignment.py already performs after a real SYNC (alignment.py:745-756).
    Never retries or chains another SYNC automatically, whatever the
    result - Fase 10 is explicit that a worse outcome must be shown, not
    auto-corrected."""
    try:
        outside = offset_coordinates(reference, [outside_offset_deg], [0.0])[0]
        if not await mount.goto(outside):
            return VerificationResult(None, None, None, None, error="post-sync verification GOTO (outside) failed")
        await sleep_fn(settle_seconds)
        if not await mount.goto(reference):
            return VerificationResult(None, None, None, None, error="post-sync verification GOTO (return) failed")
        await sleep_fn(settle_seconds)
        repeat = await mount.get_position()
        if repeat is None:
            return VerificationResult(None, None, None, None, error="could not read position after return GOTO")
        residual = float(reference.separation(repeat).deg)
        improved = (residual < residual_before_deg) if residual_before_deg is not None else None
        return VerificationResult(residual_deg=residual, repeat_ra_hours=float(repeat.ra.hour),
                                   repeat_dec_deg=float(repeat.dec.deg), improved_vs_before=improved)
    except Exception as exc:
        return VerificationResult(None, None, None, None, error=str(exc))
