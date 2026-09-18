"""Alignment engine (Fase 5's orchestrator): the one thing CLI today and a
future :8090 API both call. No print(), no input(), no argparse in here.

MEASURE -> FIT -> SHOW RESULT -> (operator decides) -> APPLY SYNC -> VERIFY
-> SAVE EVIDENCE is enforced by construction: run_solar()/run_hi() stop at
RESULT_READY and persist alignment_result.json; nothing in this module ever
calls sync_flow.apply_sync on its own initiative. Only an explicit,
separate call (from the CLI, after an operator says yes) does that.

ASYNC BOUNDARY (3rd pass, explicit architecture decision): async exactly
where there is I/O or waiting - tracking get/set (TrackingSession), mount
GOTO/SYNC/position reads, verification. Plain sync everywhere there is
only computation - plan() (local config write, no backend contact),
scan_planner/fitting/simulation/targets (unchanged, pure). preflight() is
async because one of its checks (tracking_backend_reachable) must await
the same backend contract run_solar_simulated()/run_hi_simulated() use -
making it sync would require either blocking on that await internally
(the exact anti-pattern this pass forbids) or dropping the check.
snapshot() stays SYNC on purpose: it does not query the tracking backend
live (that would make a lightweight, pollable status call pay for a
network round-trip on every call) - instead the engine tracks its own
last-known tracking mode locally, updated only at the two points it
actually changes (TrackingSession entry/exit).

Only run_solar_simulated/run_hi_simulated/preflight/apply_sync/verify_sync
are `async def`; the CLI's single asyncio.run() boundary wraps exactly
those calls (see almita_align.py) and nothing awaits inside this module
ever creates its own event loop.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import numpy as np
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time

from . import capture_conflict, scan_planner, simulation, sync_flow
from .config import AlignmentConfig
from .fitting import FitResult, fit_raster
from .mount_adapter import MountAdapter
from .session import AlignmentSession, new_session_id
from .snapshot import AlignmentSnapshot
from .state_machine import AlignmentState, AlignmentStateMachine
from .targets.hi_reference import HIReferenceProvider
from .targets.solar import SolarTarget


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class PreflightError(RuntimeError):
    pass


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str


@dataclass
class AlignmentResult:
    mode: str
    session_id: str
    coarse_fit: Optional[FitResult]
    fine_fit: Optional[FitResult]
    final_fit: FitResult
    tracking_mode: Optional[str]
    warnings: List[str]
    # ICRS anchor the fit was measured against (Sun's current position for
    # SOLAR, the chosen HI patch center for HI) - optional only so existing
    # call sites that reconstruct an AlignmentResult from persisted JSON
    # (almita_align.py's cmd_sync) without recomputing an ephemeris
    # position still work; without it, only the tangent-plane/legacy
    # fields below are available (see Fase 8's schema clarification).
    reference_center: Optional[SkyCoord] = None

    def to_dict(self) -> dict:
        estimate = self.final_fit.estimate
        payload = {
            "mode": self.mode, "session_id": self.session_id,
            "tracking_mode": self.tracking_mode, "warnings": self.warnings,
            "final_fit": self.final_fit.to_dict(),
            # Pre-hardware pass, item 8: explicit, unambiguous coordinate
            # semantics. This offset is a TANGENT-PLANE (SkyOffsetFrame)
            # east/north displacement of the fitted beam center from
            # `reference_center` - it is NOT an RA/Dec coordinate
            # difference (1deg east is not generally +1deg RA; see
            # ra_dec_delta_deg below, present only when reference_center is
            # known, for that actual quantity).
            "tangent_east_deg": estimate.offset_ra_deg,
            "tangent_north_deg": estimate.offset_dec_deg,
            # Legacy aliases - KEPT for backward compatibility with earlier
            # sessions/tooling. Identical values to tangent_east_deg/
            # tangent_north_deg above; the "_ra_"/"_dec_" in these two
            # specific names is a naming leftover, not a claim that these
            # are RA/Dec coordinate deltas. New code should prefer
            # tangent_east_deg/tangent_north_deg.
            "offset_ra_deg": estimate.offset_ra_deg,
            "offset_dec_deg": estimate.offset_dec_deg,
            "applied_sync": False,
        }
        if self.reference_center is not None:
            from alignment import offset_coordinates
            measured = offset_coordinates(self.reference_center, [estimate.offset_ra_deg],
                                           [estimate.offset_dec_deg])[0]
            payload["expected_coordinate"] = {
                "ra_hours": float(self.reference_center.ra.hour),
                "dec_deg": float(self.reference_center.dec.deg),
                "frame": "ICRS",
            }
            payload["measured_coordinate"] = {
                "ra_hours": float(measured.ra.hour),
                "dec_deg": float(measured.dec.deg),
                "frame": "ICRS",
            }
            payload["ra_dec_delta_deg"] = {
                "delta_ra_deg": float((measured.ra - self.reference_center.ra).wrap_at("180d").deg),
                "delta_dec_deg": float(measured.dec.deg - self.reference_center.dec.deg),
                "note": "raw ICRS coordinate-component difference between measured_coordinate "
                        "and expected_coordinate - NOT the same physical angular distance as "
                        "tangent_east_deg/tangent_north_deg (delta_ra_deg carries no cos(dec) "
                        "factor); provided for coordinate-level bookkeeping only.",
            }
        if self.coarse_fit is not None:
            payload["coarse_fit"] = self.coarse_fit.to_dict()
        if self.fine_fit is not None:
            payload["fine_fit"] = self.fine_fit.to_dict()
        return payload


class AlignmentEngine:
    def __init__(self, mode: str, config: AlignmentConfig, location: EarthLocation,
                 mount: MountAdapter, tracking_backend, session: Optional[AlignmentSession] = None,
                 resume: bool = False, on_event: Optional[Callable[[Dict[str, Any]], None]] = None):
        if mode not in ("solar", "hi"):
            raise ValueError(f"unknown alignment mode: {mode}")
        self.mode = mode
        self.config = config
        self.location = location
        self.mount = mount
        self.tracking_backend = tracking_backend
        self.session = session or AlignmentSession(config.global_.output_root, new_session_id(mode))
        # `resume=True` reopens a session a previous CLI invocation already
        # advanced (Fase 14: plan/preflight/run are separate commands, each
        # a separate process) - the state machine picks up from whatever
        # state was last persisted rather than restarting at IDLE, so e.g.
        # `run SESSION` after a separate `plan`+`preflight` invocation does
        # not fail state-machine validation for skipping steps it already
        # did in an earlier process.
        initial = AlignmentState.IDLE
        if resume:
            persisted = self.session.read_state()
            if persisted and persisted.get("state"):
                initial = AlignmentState(persisted["state"])
        self.state_machine = AlignmentStateMachine(_utcnow_iso, initial=initial)
        self._start_monotonic: Optional[float] = None
        self.warnings: List[str] = []
        # Item 8 (progress events): a plain, JSON-friendly sync callback -
        # no framework. Today the CLI wires one in that formats/prints or
        # collects a list for --json; a future web layer can wire one that
        # pushes onto an asyncio.Queue or via
        # loop.call_soon_threadsafe/run_coroutine_threadsafe to a
        # websocket, without this class knowing or caring which.
        self._on_event = on_event
        # Last-known tracking mode, updated only at the two points it
        # actually changes (TrackingSession entry/exit) - see module
        # docstring for why snapshot() does not query the backend live.
        self._current_tracking_mode = None

    # -- events ---------------------------------------------------------

    def _emit(self, event_type: str, **fields: Any) -> None:
        if self._on_event is None:
            return
        payload = {"type": event_type, "session_id": self.session.session_id,
                   "mode": self.mode.upper(), "timestamp": _utcnow_iso(), **fields}
        self._on_event(payload)

    # -- snapshot -----------------------------------------------------

    def snapshot(self, point_index: int = 0, point_total: int = 0,
                 latest_metric: Optional[float] = None) -> AlignmentSnapshot:
        elapsed = (time.monotonic() - self._start_monotonic) if self._start_monotonic else 0.0
        eta = None
        if point_total and point_index:
            eta = elapsed / point_index * max(0, point_total - point_index)
        return AlignmentSnapshot(
            session_id=self.session.session_id, mode=self.mode.upper(), state=self.state_machine.state.value,
            point_index=point_index, point_total=point_total,
            tracking_mode=self._current_tracking_mode.value if self._current_tracking_mode else None,
            latest_metric=latest_metric, elapsed_s=elapsed, eta_s=eta, warnings=list(self.warnings),
        )

    def _transition(self, target: AlignmentState, reason: str = "") -> None:
        self.state_machine.transition(target, reason)
        self.session.write_state({"state": target.value, "history": self.state_machine.history_as_dicts()})
        self.session.log_event(f"STATE -> {target.value} ({reason})" if reason else f"STATE -> {target.value}")
        self._emit("state", state=target.value, reason=reason)

    # -- plan / preflight ------------------------------------------------

    def plan(self) -> None:
        self.session.write_config({"mode": self.mode, "global": vars(self.config.global_),
                                    "solar": vars(self.config.solar), "hi": vars(self.config.hi)})
        self._transition(AlignmentState.PLANNED, "config persisted")

    async def preflight(self, target_provider, obstime: Optional[Time] = None,
                         timeout: Optional[float] = None) -> List[PreflightCheck]:
        """Fase 15: never trust a historical preflight for a new run - this
        always re-evaluates against `obstime` (default: now).

        `async def` because tracking_backend_reachable must await the same
        get_tracking_mode() contract run_solar_simulated()/
        run_hi_simulated() use (see module docstring) - a real timeout here
        (item 4) means "backend unreachable", a real preflight-relevant
        fact, not an infinite hang."""
        obstime = obstime or Time.now()
        timeout = timeout if timeout is not None else self.config.global_.tracking_timeout_s
        checks: List[PreflightCheck] = []

        checks.append(PreflightCheck("output_path_writable", self.session.dir.exists(), str(self.session.dir)))

        min_alt = self.config.solar.min_altitude_deg if self.mode == "solar" else self.config.hi.min_altitude_deg
        try:
            if self.mode == "solar":
                altitude = target_provider.altitude_deg(obstime)
            else:
                center, _ = target_provider.choose_target(self.location, obstime, min_alt,
                                                            self.config.resolved_beam_fwhm_deg("hi"))
                altitude = float(center.transform_to(AltAz(obstime=obstime, location=self.location)).alt.deg)
            checks.append(PreflightCheck("target_above_altitude_floor", altitude >= min_alt,
                                          f"altitude={altitude:.1f}deg floor={min_alt:.1f}deg"))
        except Exception as exc:
            checks.append(PreflightCheck("target_above_altitude_floor", False, str(exc)))

        try:
            mode = await asyncio.wait_for(self.tracking_backend.get_tracking_mode(), timeout)
            tracking_ok = mode is not None
            detail = f"get_tracking_mode() -> {mode.value if mode else None}"
        except asyncio.TimeoutError:
            tracking_ok, detail = False, f"get_tracking_mode() timed out after {timeout}s"
        except Exception as exc:
            tracking_ok, detail = False, f"get_tracking_mode() raised {type(exc).__name__}: {exc}"
        checks.append(PreflightCheck("tracking_backend_reachable", tracking_ok, detail))

        conflict = capture_conflict.check_no_conflicting_capture(
            runtime_dir=self.config.global_.orchestrator_runtime_dir)
        checks.append(PreflightCheck("no_conflicting_capture_session", not conflict.conflict, conflict.detail))

        target = AlignmentState.PREFLIGHT_OK if all(c.ok for c in checks) else AlignmentState.PREFLIGHT_FAILED
        self._transition(target, "; ".join(f"{c.name}={c.ok}" for c in checks))
        self.session.write_target({"checks": [vars(c) for c in checks], "obstime_utc": obstime.utc.isot})
        return checks

    # -- solar ------------------------------------------------------------

    async def run_solar_simulated(self, sim: simulation.SolarBeamSimConfig) -> AlignmentResult:
        """Full MEASURE->FIT for the Sun using synthetic beam data - no
        hardware, no real capture. Two stages (coarse then fine) per
        Fase 2. `sim`'s true_offset is what the fine-stage fit must recover.

        `async def` solely because of `async with TrackingSession(...)` -
        every line of scan/fit math inside stays plain sync (item 7's own
        rule: async where there is I/O, sync where there is computation).
        Cancellation (asyncio.CancelledError or KeyboardInterrupt, e.g. a
        Ctrl+C reaching the CLI's single asyncio.run()) is caught here to
        drive the state machine to CANCELLED before re-raising - item 5's
        "no dejar estados intermedios eternos"."""
        self._start_monotonic = time.monotonic()
        target = SolarTarget(self.location)
        from .tracking import TrackingMode, TrackingSession
        try:
            async with TrackingSession(self.tracking_backend, TrackingMode.SOLAR, self.session,
                                        timeout=self.config.global_.tracking_timeout_s) as ts:
                self._current_tracking_mode = TrackingMode.SOLAR
                self._transition(AlignmentState.TRACKING_CONFIGURED, "tracking set to SOLAR")
                self._transition(AlignmentState.SCANNING, "coarse stage")
                reference_time = Time.now()
                # ICRS, not the CIRS(EOD) current_position() itself returns -
                # see targets/solar.py's module docstring for why a raw
                # `.icrs` on current_position() is unsafe (BUG 2: finite Sun
                # distance) - apparent_icrs_direction() is the fix.
                reference_center = target.apparent_icrs_direction(reference_time)
                gaussian = _gaussian_template(reference_center, self.config.resolved_beam_fwhm_deg("solar"))

                coarse_points = scan_planner.build_raster(self.config.solar.coarse_span_deg,
                                                           self.config.solar.coarse_spacing_deg)
                coarse_values = simulation.synthetic_solar_metrics(coarse_points, reference_center, sim)
                coarse_positions = SkyCoord([target.resolve_offset(p.east_deg, p.north_deg, Time.now())
                                              for p in coarse_points])
                self.session.write_raw_grid({"stage": "coarse",
                                              "points": [vars(p) for p in coarse_points], "values": coarse_values})
                self._transition(AlignmentState.FITTING, "coarse fit")
                coarse_fit = fit_raster(coarse_positions, coarse_values, reference_center, gaussian,
                                         self.config.solar.coarse_span_deg)
                self._emit("fit", stage="coarse", offset_east_deg=coarse_fit.estimate.offset_ra_deg,
                           offset_north_deg=coarse_fit.estimate.offset_dec_deg, rating=coarse_fit.quality.rating)

                self._transition(AlignmentState.SCANNING, "fine stage")
                fine_points = scan_planner.build_raster(self.config.solar.fine_span_deg,
                                                         self.config.solar.fine_spacing_deg)
                fine_points = [scan_planner.ScanPoint(p.index, p.row, p.col,
                                                       p.east_deg + coarse_fit.estimate.offset_ra_deg,
                                                       p.north_deg + coarse_fit.estimate.offset_dec_deg)
                               for p in fine_points]
                fine_values = simulation.synthetic_solar_metrics(fine_points, reference_center, sim)
                fine_positions = SkyCoord([target.resolve_offset(p.east_deg, p.north_deg, Time.now())
                                            for p in fine_points])
                self.session.write_raw_grid({"stage": "fine",
                                              "points": [vars(p) for p in fine_points], "values": fine_values})
                self._transition(AlignmentState.FITTING, "fine fit")
                fine_fit = fit_raster(fine_positions, fine_values, reference_center, gaussian,
                                       self.config.solar.fine_span_deg)
                self._emit("fit", stage="fine", offset_east_deg=fine_fit.estimate.offset_ra_deg,
                           offset_north_deg=fine_fit.estimate.offset_dec_deg, rating=fine_fit.quality.rating)
            self._current_tracking_mode = ts.restored_mode
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.cancel("solar run cancelled")
            raise
        except Exception as exc:
            self.fail(f"solar run failed: {type(exc).__name__}: {exc}")
            raise

        result = AlignmentResult(mode="SOLAR", session_id=self.session.session_id,
                                  coarse_fit=coarse_fit, fine_fit=fine_fit, final_fit=fine_fit,
                                  tracking_mode=None, warnings=self.warnings,
                                  reference_center=reference_center)
        self.session.write_fit_result(result.final_fit.to_dict())
        self.session.write_alignment_result(result.to_dict())
        self._transition(AlignmentState.RESULT_READY, "solar simulated fit complete")
        return result

    # -- HI ----------------------------------------------------------------

    async def run_hi_simulated(self, provider: HIReferenceProvider, sim: simulation.HIMapSimConfig,
                                obstime: Optional[Time] = None) -> AlignmentResult:
        """`async def` for the same reason as run_solar_simulated - only
        `async with TrackingSession(...)` needs it; the HI math stays sync."""
        self._start_monotonic = time.monotonic()
        obstime = obstime or Time.now()
        beam_fwhm = self.config.resolved_beam_fwhm_deg("hi")
        min_alt = self.config.hi.min_altitude_deg
        center, selection = provider.choose_target(self.location, obstime, min_alt, beam_fwhm)
        template = provider.template_for(center, beam_fwhm)

        from .tracking import TrackingMode, TrackingSession
        try:
            async with TrackingSession(self.tracking_backend, TrackingMode.SIDEREAL, self.session,
                                        timeout=self.config.global_.tracking_timeout_s) as ts:
                self._current_tracking_mode = TrackingMode.SIDEREAL
                self._transition(AlignmentState.TRACKING_CONFIGURED, "tracking set to SIDEREAL")
                self._transition(AlignmentState.SCANNING, "hi raster")
                points = scan_planner.build_raster(self.config.hi.raster_span_deg, self.config.hi.raster_spacing_deg)
                positions = _offset_points_to_positions(center, points)
                # Item 7 (unified spectral path): the same acquire ->
                # spectral_pipeline metric route a real HI backend will use
                # (alignment_engine/hi/acquisition.py) - only the source of
                # the spectrum (simulated here) differs from a future real run.
                from alignment import shifted_positions
                from .hi.acquisition import SimulatedHIAcquisitionBackend, acquire_and_reduce_point
                shifted = shifted_positions(positions, center, sim.true_offset_east_deg, sim.true_offset_north_deg)
                clean_expected = np.asarray(template(shifted))
                noise_scale = sim.noise_fraction * (float(np.std(clean_expected)) or 1.0)
                acquisition_backend = SimulatedHIAcquisitionBackend(
                    expected_amplitude_by_index=list(clean_expected), gain_a=sim.gain_a,
                    baseline_b=sim.baseline_b, noise_std=noise_scale,
                    missing_channel_fraction=sim.missing_fraction, seed=sim.seed)
                values = [await acquire_and_reduce_point(
                              acquisition_backend, position,
                              center_frequency_hz=self.config.hi.center_frequency_hz,
                              sample_rate_hz=self.config.hi.sample_rate_hz,
                              gain_db=self.config.hi.gain_db, integration_seconds=self.config.hi.integration_seconds)
                          for position in positions]
                self.session.write_raw_grid({"stage": "hi", "points": [vars(p) for p in points], "values": values})
                self._transition(AlignmentState.FITTING, "hi fit")
                fit = fit_raster(positions, values, center, template, self.config.hi.raster_span_deg)
                self._emit("fit", stage="hi", offset_east_deg=fit.estimate.offset_ra_deg,
                           offset_north_deg=fit.estimate.offset_dec_deg, rating=fit.quality.rating)
            self._current_tracking_mode = ts.restored_mode
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.cancel("hi run cancelled")
            raise
        except Exception as exc:
            self.fail(f"hi run failed: {type(exc).__name__}: {exc}")
            raise

        result = AlignmentResult(mode="HI", session_id=self.session.session_id,
                                  coarse_fit=None, fine_fit=None, final_fit=fit,
                                  tracking_mode=None, warnings=self.warnings,
                                  reference_center=center)
        payload = result.to_dict()
        payload["target_selection"] = selection
        payload["is_observational"] = provider.is_observational
        self.session.write_fit_result(fit.to_dict())
        self.session.write_alignment_result(payload)
        self._transition(AlignmentState.RESULT_READY, "hi simulated fit complete")
        return result

    # -- sync (thin passthrough so the CLI has one import surface) --------

    def prepare_sync(self, result: AlignmentResult, center: SkyCoord, is_observational: bool,
                      confidence_threshold: float = 0.65):
        # Idempotent re-entry: `sync SESSION` (preview) and `sync SESSION
        # --apply` (Fase 14) are two separate CLI invocations that both
        # call this - the second one resumes a session already sitting in
        # SYNC_PENDING from the first, which is a re-preparation, not an
        # invalid transition.
        if self.state_machine.state != AlignmentState.SYNC_PENDING:
            self._transition(AlignmentState.SYNC_PENDING, "sync plan prepared")
        else:
            self.session.log_event("STATE SYNC_PENDING (re-prepared)")
        plan = sync_flow.prepare_sync(result.mode, result.final_fit, center, is_observational,
                                       result.tracking_mode, confidence_threshold,
                                       session_id=result.session_id)
        self.session.write_sync_plan(plan.to_dict())
        return plan

    async def apply_sync(self, plan, mount: MountAdapter, settle_seconds: float, sleep_fn,
                          timeout: Optional[float] = None):
        """Thin state-machine-aware wrapper over sync_flow.apply_sync, so
        the state persisted in state.json advances correctly even when this
        runs as a separate CLI process from the one that produced the
        result (Fase 14: sync SESSION is its own command). Cancellation
        during a real apply must not leave state stuck in SYNC_APPLYING -
        item 5."""
        self._transition(AlignmentState.SYNC_APPLYING, "applying sync")
        try:
            sync_result = await sync_flow.apply_sync(
                plan, mount, settle_seconds, sleep_fn,
                timeout=timeout if timeout is not None else self.config.global_.goto_timeout_s)
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.cancel("sync apply cancelled")
            raise
        self.session.write_sync_result(sync_result.to_dict())
        if not sync_result.applied:
            self._transition(AlignmentState.FAILED, f"sync not applied: {sync_result.error}")
        return sync_result

    async def verify_sync(self, reference: SkyCoord, mount: MountAdapter, settle_seconds: float, sleep_fn,
                           residual_before_deg: Optional[float] = None, timeout: Optional[float] = None):
        self._transition(AlignmentState.VERIFYING, "verifying sync")
        try:
            verification = await sync_flow.verify_sync(
                mount, reference, settle_seconds, sleep_fn, residual_before_deg=residual_before_deg,
                timeout=timeout if timeout is not None else self.config.global_.goto_timeout_s)
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.cancel("sync verification cancelled")
            raise
        self.session.write_verification(verification.to_dict())
        if verification.error is None:
            self._transition(AlignmentState.COMPLETED, f"verification residual={verification.residual_deg}")
        else:
            self._transition(AlignmentState.FAILED, f"verification error: {verification.error}")
        return verification

    def cancel(self, reason: str) -> None:
        if not self.state_machine.is_terminal():
            self._transition(AlignmentState.CANCELLED, reason)

    def fail(self, reason: str) -> None:
        if not self.state_machine.is_terminal():
            self._transition(AlignmentState.FAILED, reason)


def _gaussian_template(center: SkyCoord, fwhm_deg: float) -> Callable[[SkyCoord], np.ndarray]:
    """Single-argument callable(coords) -> ndarray of predicted values (must
    stay a numpy array, not a list: estimate_template_offset() calls
    .reshape() on the result) - the same plain-Gaussian-on-separation
    template alignment.py's own Sun path uses (alignment.py:718-719)."""
    sigma = fwhm_deg / (2 * math.sqrt(2 * math.log(2)))
    return lambda coords: np.exp(-.5 * (coords.separation(center).deg / sigma) ** 2)


def _offset_points_to_positions(center: SkyCoord, points: List[scan_planner.ScanPoint]) -> SkyCoord:
    from alignment import offset_coordinates
    return offset_coordinates(center, [p.east_deg for p in points], [p.north_deg for p in points])
