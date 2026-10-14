"""Stop the mount and PROVE it stopped - one sequence for every path that can leave it moving.

First real STOP OBSERVATION during a GOTO (2026-10-13): capture.py asked for tracking OFF while the mount was still
slewing, did not wait and sent no abort; OnStep finished the GOTO and went on tracking with nobody watching. This
module is the answer, shared by capture.py, the CALIBRATE wizard, ALIGN, the gain pilot and the web's own
STOP MOUNT action:

    1. abort the slew with the driver's own TELESCOPE_ABORT_MOTION when it exposes one;
    2. wait - bounded - until EQUATORIAL_EOD_COORD is no longer Busy (so a GOTO that could not be aborted is
       waited for, never raced);
    3. switch tracking OFF and keep reading it: a GOTO that completes late turns tracking back ON, so OFF must
       hold for several consecutive reads, and it is switched OFF again if it comes back;
    4. read the coordinates twice: a mount that is neither slewing nor tracking shows RA advancing at the sidereal
       rate with Dec fixed; RA standing still means it is still tracking, anything else means it is still moving.

The result says `confirmed` only when INDI's own readback shows all of that. Commands having been sent, or the
calling process having exited, are never taken as proof. It never parks, homes or starts another movement.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

ABORT_SETTLE_TIMEOUT_S = 15.0      # after an accepted abort the slew must end within this
MOTION_TIMEOUT_S = 120.0           # a slew that cannot be aborted is waited for at most this long (same as a GOTO)
TRACK_OFF_STABLE_READS = 3         # consecutive fresh OFF reads required ...
TRACK_OFF_READ_PERIOD_S = 1.0      # ... this far apart (covers a GOTO that re-enables tracking as it completes)
TRACK_OFF_MAX_RESENDS = 3
TRACK_OFF_TIMEOUT_S = 20.0
STILLNESS_WINDOW_S = 5.0           # time between the two coordinate reads of the kinematic check
READ_TIMEOUT_S = 3.0
SIDEREAL_RA_SECONDS_PER_SECOND = 1.0027379
POLE_DEC_DEG = 85.0                # closer to a pole than this, RA says nothing about motion: check is advisory


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fresh(telescope: Any, name: str, timeout: float = READ_TIMEOUT_S) -> Dict[str, Any]:
    """A property value the driver sent AFTER this call asked for it (never a cached one)."""
    key = (telescope.device_name, name)
    baseline = telescope._property_cache.get(key, {}).get("update_seq", 0)
    await telescope._send_command(f'<getProperties device="{telescope.device_name}" name="{name}" version="1.7"/>')
    return await telescope._wait_property(name, timeout, baseline)


def _coords(item: Dict[str, Any]) -> tuple:
    el = item.get("elements") or {}
    return float(el["RA"]), float(el["DEC"])


async def stop_mount_and_confirm(telescope: Any, *, log: Optional[Callable[[str], None]] = None,
                                 motion_timeout_s: float = MOTION_TIMEOUT_S,
                                 abort_settle_timeout_s: float = ABORT_SETTLE_TIMEOUT_S,
                                 stillness_window_s: float = STILLNESS_WINDOW_S,
                                 track_off_read_period_s: float = TRACK_OFF_READ_PERIOD_S,
                                 track_off_timeout_s: float = TRACK_OFF_TIMEOUT_S) -> Dict[str, Any]:
    """Runs the sequence above on a CONNECTED INDITelescopeControl. Never raises: every failure is a reason in the
    returned report, with confirmed=False."""
    say = log or (lambda _m: None)
    started = time.monotonic()
    report: Dict[str, Any] = {
        "confirmed": False, "reason": None, "started_utc": _utc(), "steps": [],
        "abort": {"supported": None, "sent": False}, "motion_stopped": None, "tracking_off": None,
        "tracking_reactivations": 0, "stillness": None,
        "limits": "software stop over INDI - not a physical brake; no park, home or new movement is ever commanded",
    }

    def step(name: str, ok: Optional[bool], detail: str) -> None:
        report["steps"].append({"step": name, "ok": ok, "detail": detail, "t_s": round(time.monotonic() - started, 2)})
        say(f"MOUNT STOP  {name}: {'ok' if ok else 'FAILED' if ok is False else 'info'} - {detail}")

    def fail(reason: str) -> Dict[str, Any]:
        report["reason"] = reason
        report["ended_utc"] = _utc()
        report["duration_s"] = round(time.monotonic() - started, 2)
        say(f"MOUNT STOP  NOT CONFIRMED - {reason}")
        return report

    try:
        if telescope is None or getattr(telescope, "writer", None) is None:
            return fail("no INDI connection to the mount")

        # ---- 1. abort
        abort_known = (telescope.device_name, "TELESCOPE_ABORT_MOTION") in telescope._property_cache
        if not abort_known:
            try:
                await _fresh(telescope, "TELESCOPE_ABORT_MOTION", 2.0)
                abort_known = True
            except (asyncio.TimeoutError, ConnectionError):
                abort_known = False
        report["abort"]["supported"] = abort_known
        if abort_known:
            await telescope.abort_motion()
            report["abort"]["sent"] = True
            step("abort", True, "TELESCOPE_ABORT_MOTION sent")
        else:
            step("abort", None, "the driver exposes no TELESCOPE_ABORT_MOTION - waiting for the movement to end")

        # ---- 2. wait until not slewing
        deadline = time.monotonic() + (abort_settle_timeout_s if abort_known else motion_timeout_s)
        hard_deadline = time.monotonic() + motion_timeout_s
        state = None
        while True:
            try:
                eod = await _fresh(telescope, "EQUATORIAL_EOD_COORD")
            except (asyncio.TimeoutError, ConnectionError) as exc:
                return fail(f"INDI did not answer while waiting for the movement to end ({type(exc).__name__}: {exc})")
            state = eod.get("state")
            if state != "Busy":
                break
            now = time.monotonic()
            if now >= deadline:
                if abort_known and now < hard_deadline:      # abort did not take: fall back to waiting it out
                    step("abort", False, f"still slewing {abort_settle_timeout_s:g} s after the abort - waiting for it to end")
                    deadline = hard_deadline
                    abort_known = False
                    continue
                report["motion_stopped"] = False
                return fail(f"the mount was still slewing after {motion_timeout_s:g} s")
            await asyncio.sleep(0.5)
        report["motion_stopped"] = True
        step("motion", True, f"EQUATORIAL_EOD_COORD state {state} (not slewing)")

        # ---- 3. tracking OFF, and it must stay OFF
        consecutive, resends = 0, 0
        track_deadline = time.monotonic() + track_off_timeout_s
        first = True
        while consecutive < TRACK_OFF_STABLE_READS:
            if time.monotonic() >= track_deadline:
                report["tracking_off"] = False
                return fail(f"tracking did not stay OFF within {track_off_timeout_s:g} s "
                            f"(switched off {resends} time(s), came back {report['tracking_reactivations']} time(s))")
            tracking = await telescope.get_tracking_state(timeout=READ_TIMEOUT_S)
            if tracking == "off":
                consecutive += 1
            else:
                if not first and tracking == "on":
                    report["tracking_reactivations"] += 1
                    step("tracking", False, "tracking came back ON after being switched OFF (late GOTO completion) - switching it OFF again")
                if resends >= TRACK_OFF_MAX_RESENDS:
                    report["tracking_off"] = False
                    return fail(f"tracking is {tracking} after {resends} OFF command(s)")
                consecutive = 0
                resends += 1
                if not await telescope.set_tracking(False):
                    report["tracking_off"] = False
                    return fail("the tracking OFF command could not be sent")
                first = False
                continue
            first = False
            if consecutive < TRACK_OFF_STABLE_READS:
                await asyncio.sleep(track_off_read_period_s)
        report["tracking_off"] = True
        step("tracking", True, f"TELESCOPE_TRACK_STATE OFF on {TRACK_OFF_STABLE_READS} consecutive reads"
                               + (f" (after {report['tracking_reactivations']} late reactivation(s))" if report["tracking_reactivations"] else ""))

        # ---- 4. kinematic check: neither slewing nor tracking
        try:
            a = await _fresh(telescope, "EQUATORIAL_EOD_COORD")
            t_a = time.monotonic()
            await asyncio.sleep(stillness_window_s)
            b = await _fresh(telescope, "EQUATORIAL_EOD_COORD")
            dt = time.monotonic() - t_a
            (ra_a, dec_a), (ra_b, dec_b) = _coords(a), _coords(b)
        except (asyncio.TimeoutError, ConnectionError, KeyError, ValueError) as exc:
            return fail(f"the mount position could not be read back ({type(exc).__name__}: {exc})")
        d_ra_s = (((ra_b - ra_a + 12.0) % 24.0) - 12.0) * 3600.0          # seconds of time, wrap-safe
        d_dec_arcsec = (dec_b - dec_a) * 3600.0
        expected = dt * SIDEREAL_RA_SECONDS_PER_SECOND
        tolerance = max(2.0, 0.5 * dt)
        still = {"window_s": round(dt, 2), "ra_change_s": round(d_ra_s, 2), "dec_change_arcsec": round(d_dec_arcsec, 1),
                 "expected_ra_change_s": round(expected, 2), "tolerance_s": round(tolerance, 2),
                 "eod_state": b.get("state"), "dec_deg": dec_b, "verdict": None}
        report["stillness"] = still
        near_pole = abs(dec_b) >= POLE_DEC_DEG
        if b.get("state") == "Busy" or abs(d_dec_arcsec) > 60.0:
            still["verdict"] = "MOVING"
            return fail(f"the mount is still moving: Dec changed {d_dec_arcsec:.0f} arcsec in {dt:.1f} s, state {b.get('state')}")
        if abs(d_ra_s - expected) <= tolerance:
            still["verdict"] = "STATIONARY"
        elif near_pole:
            still["verdict"] = "NOT_ASSESSABLE_NEAR_POLE"
        elif abs(d_ra_s) <= tolerance:
            still["verdict"] = "TRACKING"
            return fail(f"RA did not advance in {dt:.1f} s ({d_ra_s:+.1f} s): the mount is still following the sky "
                        "although TELESCOPE_TRACK_STATE reads OFF")
        else:
            still["verdict"] = "MOVING"
            return fail(f"RA changed {d_ra_s:+.1f} s in {dt:.1f} s (a stopped mount shows {expected:+.1f} s): it is still moving")
        step("stillness", True, f"{still['verdict']}: RA {d_ra_s:+.1f} s / Dec {d_dec_arcsec:+.0f} arcsec in {dt:.1f} s")

        report["confirmed"] = True
        report["reason"] = "INDI readback: not slewing, tracking OFF and holding, position consistent with a stopped mount"
        report["ended_utc"] = _utc()
        report["duration_s"] = round(time.monotonic() - started, 2)
        say(f"MOUNT STOP  CONFIRMED in {report['duration_s']:.1f} s")
        return report
    except (asyncio.CancelledError, KeyboardInterrupt):
        # A second interruption while proving the stop: say so instead of pretending, then let it through.
        fail("interrupted before the stop could be confirmed")
        raise
    except Exception as exc:  # noqa: BLE001 - whatever INDI/the socket does, the answer is "not confirmed"
        return fail(f"{type(exc).__name__}: {exc}")


LAST_REPORT: Optional[Dict[str, Any]] = None       # the most recent report of this process (read by callers whose
                                                   # interruption surfaces as a different exception object)


async def stop_after_interruption(telescope: Any, exc: BaseException,
                                  log: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    """For `except BaseException as exc:` around code that moves the mount (wizard, ALIGN, gain pilot): when the
    exception is an operator stop or a timeout cancellation, stop the mount and prove it before the caller
    disconnects. Any other failure returns None and is left to the caller. Never raises."""
    global LAST_REPORT
    if not isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
        return None
    try:
        report = await stop_mount_and_confirm(telescope, log=log)
    except BaseException as again:  # noqa: BLE001 - interrupted a second time: recorded, the first exception wins
        report = {"confirmed": False, "reason": f"stop sequence interrupted ({type(again).__name__})", "steps": []}
    LAST_REPORT = report
    (log or print)(summary_line(report))
    return report


def summary_line(report: Optional[Dict[str, Any]]) -> str:
    """One line for logs, session files and the web."""
    if not report:
        return "PARADA NO CONFIRMADA - no stop report"
    if report.get("confirmed"):
        still = report.get("stillness") or {}
        return (f"MOUNT STOP CONFIRMED ({report.get('duration_s')} s): not slewing, tracking OFF"
                + (f" after {report['tracking_reactivations']} late reactivation(s)" if report.get("tracking_reactivations") else "")
                + (f", {still.get('verdict')}" if still.get("verdict") else ""))
    return f"PARADA NO CONFIRMADA - {report.get('reason')}"


def interruption_points(report: Dict[str, Any]) -> List[str]:
    return [f"{s['step']}: {s['detail']}" for s in report.get("steps", [])]
