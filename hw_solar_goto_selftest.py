#!/usr/bin/env python3
"""First real-hardware SOLAR GOTO test (explicitly authorized, one specific
scope only): a SINGLE real GOTO to the Sun's current position, gated
unconditionally on alignment_engine.solar_preflight - if that gate reports
BLOCKED for any reason, this script aborts before any write, with no
override anywhere in it or in solar_preflight.py itself.

NEVER does, and cannot be made to do via any flag: SYNC, raster, coarse/fine
scan, HI, pointing-model changes, mount-limit changes, firmware/INDI config
changes, or a second GOTO. Writes only:
  - TELESCOPE_TRACK_MODE (via RealTrackingBackend/TrackingSession - the
    same production path validated by hw_tracking_selftest.py's real run).
  - TELESCOPE_TRACK_STATE, ONLY if the single GOTO actually converged
    (there is nothing to track otherwise) - see _TrackStateGuard.
  - ON_COORD_SET + EQUATORIAL_EOD_COORD, exactly once, via
    alignment_engine.mount_adapter.RealMountAdapter, which wraps
    indi_telescope_control.INDITelescopeControl - the same mature, already
    hardware-proven GOTO implementation capture.py/alignment.py already
    use (not a hand-rolled LX200 command).

Coordinate correctness: the Sun's position for this test comes from
SolarTarget.current_position(obstime), i.e. sun_eod() - a single centered
point (offset east=0, north=0). This deliberately never touches
SkyOffsetFrame/resolve_offset() - the tangent-plane geometry path that has
the documented CIRS-origin-without-obstime bug - so that bug cannot affect
this test even in principle; it only matters for raster/offset points,
none of which this test performs.

Safety design (mirrors hw_tracking_selftest.py):
  - Defaults to --backend simulated - a full rehearsal (including a
    --simulate-obstime override so the rehearsal can exercise the PASS
    path even at night) - real hardware only with --backend real --yes.
  - The solar preflight gate is evaluated with the REAL current time
    whenever --backend real is used - never overridable.
  - Every property is read before being written; original tracking mode
    AND tracking state are restored via guaranteed restore-on-exit
    machinery (TrackingSession for TRACK_MODE, _TrackStateGuard below for
    TRACK_STATE) covering success, exception, and cancellation alike.
  - No abort/second-GOTO/SYNC is ever sent - see module docstring.
  - Full evidence persisted under
    data/alignment/HW-SOLAR-GOTO-<UTC timestamp>/.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import astropy.units as u
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time

from alignment_engine import capture_conflict
from alignment_engine.mount_adapter import RealMountAdapter, SimulatedMountAdapter
from alignment_engine.motion_detector import track_state_from_switch_vector
from alignment_engine.session import AlignmentSession
from alignment_engine.solar_preflight import (
    DEFAULT_MAX_ALTITUDE_DEG, DEFAULT_MIN_SOLAR_ALTITUDE_DEG, check_solar_preflight,
)
from alignment_engine.targets.solar import SolarTarget
from alignment_engine.tracking import (
    RealTrackingBackend, SimulatedTrackingBackend, TrackingMode, TrackingSession,
    parse_number_vector, parse_switch_vector, parse_text_vector, parse_vector_state,
    read_property_readonly,
)
from hw_tracking_selftest import _WriteAuditingBackend, _mode_from_switch_vector

DEVICE_NAME = "LX200 OnStep"
# The real, already-hardware-proven convergence tolerance from
# indi_telescope_control.py's own goto() polling loop (convergence_tol_deg,
# settle_tol_deg=0.02 over 2 stable hits) - not invented here, and not
# adjusted after seeing a result. That existing implementation is reused
# unmodified for the GOTO itself; this is only the pass/fail threshold this
# harness applies to the SAME final position it already read back.
ARRIVAL_TOLERANCE_DEG = 0.25
WRITE_WHITELIST = {"TELESCOPE_TRACK_MODE", "TELESCOPE_TRACK_STATE", "ON_COORD_SET", "EQUATORIAL_EOD_COORD"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _SimulatedTrackStateTelescope:
    """Rehearsal-only stand-in for INDITelescopeControl's TRACK_STATE
    surface - SimulatedMountAdapter has no equivalent, and this test needs
    one to rehearse the enable/restore path before ever touching hardware."""

    def __init__(self, initial_on: bool = False):
        self.on = initial_on

    async def set_tracking(self, enable: bool) -> bool:
        self.on = enable
        return True

    async def wait_tracking_state(self, expected_on: bool, timeout: float = 5.0) -> bool:
        return self.on == expected_on


class _TrackStateGuard:
    """Restore-guaranteed TELESCOPE_TRACK_STATE toggle - the same
    restore-on-any-exit guarantee TrackingSession gives TRACK_MODE, kept
    separate (not folded into TrackingSession) because TRACK_STATE is a
    distinct concept: TRACK_MODE selects the RATE, TRACK_STATE is the
    drive on/off switch - conflating them was explicitly ruled out.
    `telescope` is anything exposing async set_tracking(bool)->bool and
    wait_tracking_state(bool, timeout)->bool: INDITelescopeControl for
    real, _SimulatedTrackStateTelescope for rehearsal."""

    def __init__(self, telescope, original_on: bool, session=None, timeout: float = 10.0):
        self.telescope = telescope
        self.original_on = original_on
        self.session = session
        self.timeout = timeout
        self.enabled = False
        self.restored = False
        self.restore_error: Optional[str] = None
        self.writes: list = []

    def _log(self, message: str) -> None:
        if self.session is not None:
            self.session.log_event(f"TRACK_STATE {message}")

    async def enable(self) -> None:
        ok = await asyncio.wait_for(self.telescope.set_tracking(True), self.timeout)
        self.writes.append({"property": "TELESCOPE_TRACK_STATE",
                             "elements": {"TRACK_ON": "On", "TRACK_OFF": "Off"}})
        if not ok:
            raise RuntimeError("failed to enable TELESCOPE_TRACK_STATE for post-GOTO tracking")
        confirmed = await asyncio.wait_for(self.telescope.wait_tracking_state(True, self.timeout), self.timeout)
        if not confirmed:
            raise RuntimeError("TELESCOPE_TRACK_STATE did not confirm ON within timeout")
        self.enabled = True
        self._log("ENABLED (post-GOTO tracking, required to actually hold the Sun after arrival)")

    async def restore(self) -> None:
        try:
            ok = await asyncio.shield(asyncio.wait_for(self.telescope.set_tracking(self.original_on), self.timeout))
            self.writes.append({"property": "TELESCOPE_TRACK_STATE",
                                 "elements": {"TRACK_ON": "On" if self.original_on else "Off",
                                              "TRACK_OFF": "Off" if self.original_on else "On"}})
            if ok:
                confirmed = await asyncio.shield(asyncio.wait_for(
                    self.telescope.wait_tracking_state(self.original_on, self.timeout), self.timeout))
                self.restored = bool(confirmed)
                if not confirmed:
                    self.restore_error = "set_tracking reported success but readback did not confirm original state"
            else:
                self.restore_error = "backend refused to restore original TRACK_STATE"
        except (asyncio.CancelledError, KeyboardInterrupt) as restore_exc:
            self.restore_error = f"restore cancelled: {restore_exc!r}"
        except Exception as restore_exc:
            self.restore_error = f"{type(restore_exc).__name__}: {restore_exc}"
        outcome = "RESTORED" if self.restored else f"RESTORE_FAILED ({self.restore_error})"
        self._log(f"{outcome} target={'ON' if self.original_on else 'OFF'}")


def _combine_write_audit(entries: list) -> dict:
    properties_written = sorted({e["property"] for e in entries})
    elements_written = sorted({name for e in entries for name, v in e["elements"].items() if v == "On"})
    unexpected = [e for e in entries if e["property"] not in WRITE_WHITELIST]
    return {
        "write_count": len(entries), "properties_written": properties_written,
        "elements_written": elements_written, "unexpected_write_count": len(unexpected),
        "unexpected_writes": unexpected, "writes": entries,
        "note": "call-level audit of this script's OWN write operations (TRACK_MODE, TRACK_STATE, "
                "and the single GOTO's ON_COORD_SET+EQUATORIAL_EOD_COORD) - not a byte-level "
                "interception of indi_telescope_control.py's internal XML, which is trusted, "
                "existing, already hardware-proven code this script reuses rather than reimplements.",
    }


async def _snapshot(host: str, port: int, timeout: float) -> dict:
    """Read-only, everything the mandatory precheck needs."""
    names = ["TELESCOPE_TRACK_MODE", "TELESCOPE_TRACK_STATE", "EQUATORIAL_EOD_COORD",
             "TARGET_EOD_COORD", "ON_COORD_SET", "TELESCOPE_PIER_SIDE", "TELESCOPE_PARK",
             "OnStep Status", "Slew elevation Limit", "GEOGRAPHIC_COORD"]
    raw = {name: await read_property_readonly(host, port, DEVICE_NAME, name, timeout) for name in names}
    return {
        "timestamp_utc": _utcnow_iso(),
        "TELESCOPE_TRACK_MODE": parse_switch_vector(raw["TELESCOPE_TRACK_MODE"]),
        "TELESCOPE_TRACK_STATE": parse_switch_vector(raw["TELESCOPE_TRACK_STATE"]),
        "EQUATORIAL_EOD_COORD": parse_number_vector(raw["EQUATORIAL_EOD_COORD"]),
        "EQUATORIAL_EOD_COORD_state": parse_vector_state(raw["EQUATORIAL_EOD_COORD"]),
        "TARGET_EOD_COORD": parse_number_vector(raw["TARGET_EOD_COORD"]),
        "ON_COORD_SET": parse_switch_vector(raw["ON_COORD_SET"]),
        "TELESCOPE_PIER_SIDE": parse_switch_vector(raw["TELESCOPE_PIER_SIDE"]),
        "TELESCOPE_PARK": parse_switch_vector(raw["TELESCOPE_PARK"]),
        "OnStep_Status": parse_text_vector(raw["OnStep Status"]),
        "Slew_elevation_Limit": parse_number_vector(raw["Slew elevation Limit"]),
        "GEOGRAPHIC_COORD": parse_number_vector(raw["GEOGRAPHIC_COORD"]),
    }


def compute_goto_verdict(*, gates_passed: bool, cancelled: bool, step_error: Optional[str],
                          solar_confirmed: bool, goto_ok: Optional[bool],
                          angular_error_deg: Optional[float], tolerance_deg: float,
                          onstep_error: bool, unexpected_write_count: int,
                          track_mode_restored: bool, track_state_restored: bool) -> str:
    """Pure, no I/O. PASS only if every one of the user's stated conditions
    holds; INCONCLUSIVE only when there truly isn't enough evidence (never
    invented); FAIL otherwise; CANCELLED/BLOCKED are their own terminal
    states, checked first so they are never masked by a later condition."""
    if cancelled:
        return "CANCELLED"
    if not gates_passed:
        return "BLOCKED"
    if step_error is not None:
        return "FAIL"
    if goto_ok is None:
        return "INCONCLUSIVE"  # GOTO was never attempted at all
    if goto_ok is False:
        return "FAIL"  # command was explicitly rejected/timed out - a definitive, known failure, not a missing-evidence case
    if angular_error_deg is None:
        return "INCONCLUSIVE"  # GOTO reported success but the final position could not be verified
    hard_fail = (
        not solar_confirmed
        or angular_error_deg > tolerance_deg
        or onstep_error
        or unexpected_write_count != 0
        or not track_mode_restored
        or not track_state_restored
    )
    return "FAIL" if hard_fail else "PASS"


async def _run_stability_check(host: str, port: int, timeout: float, duration_s: float,
                                poll_interval_s: float, backend: str) -> dict:
    """READ-ONLY. Confirms the mount stays put and fault-free for
    `duration_s` after landing - see module note below on what it does NOT
    claim to measure."""
    samples = []
    deadline = time.monotonic() + duration_s
    busy_detected = False
    error_detected = False
    mode_changed = False
    while time.monotonic() < deadline:
        if backend == "real":
            mode = parse_switch_vector(await read_property_readonly(host, port, DEVICE_NAME, "TELESCOPE_TRACK_MODE", timeout))
            coord_raw = await read_property_readonly(host, port, DEVICE_NAME, "EQUATORIAL_EOD_COORD", timeout)
            coord_state = parse_vector_state(coord_raw)
            status = parse_text_vector(await read_property_readonly(host, port, DEVICE_NAME, "OnStep Status", timeout))
        else:
            mode = {"TRACK_SIDEREAL": "Off", "TRACK_SOLAR": "On", "TRACK_LUNAR": "Off", "TRACK_CUSTOM": "Off"}
            coord_state = "Ok"
            status = {"Error": "None"}
        samples.append({"timestamp_utc": _utcnow_iso(), "track_mode": mode, "eod_state": coord_state, "onstep_status": status})
        if coord_state == "Busy":
            busy_detected = True
        if status and status.get("Error") not in (None, "None", "Goto No Error"):
            error_detected = True
        if mode and mode.get("TRACK_SOLAR") != "On":
            mode_changed = True
        await asyncio.sleep(poll_interval_s)
    return {
        "duration_s": duration_s, "sample_count": len(samples), "samples": samples,
        "busy_detected": busy_detected, "error_detected": error_detected, "mode_changed": mode_changed,
        "note": "Over this short a window, the solar-vs-sidereal tracking rate difference (~1 part in "
                "365, a few hundredths of an arcsecond over 10-20s) is far below what this check "
                "resolves. It confirms the mount stayed put, kept SOLAR selected, and reported no "
                "fault - not a tracking-rate measurement. Signal quality is explicitly out of scope "
                "(this is a mount-only test).",
    }


async def main(args) -> int:
    host, port, timeout = args.host, args.port, args.timeout
    report: dict = {"test": "HW-SOLAR-GOTO", "backend": args.backend, "device": DEVICE_NAME,
                     "host": host, "port": port, "started_utc": _utcnow_iso()}
    session_id = f"HW-SOLAR-GOTO-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    session = AlignmentSession(args.session_root, session_id)
    print(f"Session: {session.dir}")

    obstime_provider = Time.now
    if args.backend == "simulated" and args.simulate_obstime:
        fixed = Time(args.simulate_obstime)
        obstime_provider = lambda: fixed  # noqa: E731 - rehearsal only, never reachable for --backend real

    # ================================================================ PRECHECK
    gates = []  # [(name, passed: bool, detail: str)]

    conflict = capture_conflict.check_no_conflicting_capture(runtime_dir=args.orchestrator_runtime_dir)
    gates.append(("no_capture_conflict", not conflict.conflict, conflict.detail))

    if args.backend == "real":
        snapshot = await _snapshot(host, port, timeout)
    else:
        snapshot = {
            "timestamp_utc": _utcnow_iso(),
            "TELESCOPE_TRACK_MODE": {"TRACK_SIDEREAL": "On", "TRACK_SOLAR": "Off", "TRACK_LUNAR": "Off", "TRACK_CUSTOM": "Off"},
            "TELESCOPE_TRACK_STATE": {"TRACK_ON": "Off", "TRACK_OFF": "On"},
            "EQUATORIAL_EOD_COORD": {"RA": 12.0, "DEC": -80.0}, "EQUATORIAL_EOD_COORD_state": "Ok",
            "TARGET_EOD_COORD": {"RA": 12.0, "DEC": -80.0},
            "ON_COORD_SET": {"TRACK": "Off", "SLEW": "On", "SYNC": "Off"},
            "TELESCOPE_PIER_SIDE": {"PIER_WEST": "Off", "PIER_EAST": "On"},
            "TELESCOPE_PARK": {"PARK": "Off", "UNPARK": "On"},
            "OnStep_Status": {"Tracking": "Idle", "Error": "None", "Park": "UnParked"},
            "Slew_elevation_Limit": {"minAlt": 0.0, "maxAlt": 90.0},
            # Fake-but-labeled site so this rehearsal exercises the real
            # ephemeris math end to end (never used for --backend real).
            "GEOGRAPHIC_COORD": {"LAT": -33.4331, "LONG": 289.3336, "ELEV": 550.0},
        }
    session.write_mount_before(snapshot)

    mount_reachable = snapshot.get("TELESCOPE_TRACK_MODE") is not None
    gates.append(("mount_reachable", mount_reachable, "TELESCOPE_TRACK_MODE unreadable" if not mount_reachable else "ok"))

    original_mode = _mode_from_switch_vector(snapshot.get("TELESCOPE_TRACK_MODE") or {})
    gates.append(("original_tracking_mode_known", original_mode is not None,
                  f"{original_mode.value if original_mode else None}"))

    original_track_state_str = track_state_from_switch_vector(snapshot.get("TELESCOPE_TRACK_STATE"))
    gates.append(("original_track_state_known", original_track_state_str is not None,
                  f"{original_track_state_str}"))

    park = snapshot.get("TELESCOPE_PARK") or {}
    parked = park.get("PARK") == "On"
    gates.append(("not_parked", not parked, f"TELESCOPE_PARK={park}"))

    slewing = snapshot.get("EQUATORIAL_EOD_COORD_state") == "Busy"
    gates.append(("not_slewing", not slewing, f"EQUATORIAL_EOD_COORD state={snapshot.get('EQUATORIAL_EOD_COORD_state')}"))

    geo = snapshot.get("GEOGRAPHIC_COORD") or {}
    geo_valid = (geo.get("LAT") is not None and geo.get("LONG") is not None
                 and -90.0 <= geo["LAT"] <= 90.0)
    gates.append(("observer_location_valid", geo_valid, f"GEOGRAPHIC_COORD={geo}"))

    obstime = obstime_provider()
    gates.append(("valid_time", obstime is not None and bool(obstime.jd > 0),
                  f"obstime={obstime.utc.isot if obstime else None}"))

    preflight_result = None
    if geo_valid:
        location = EarthLocation(lat=geo["LAT"] * u.deg, lon=geo["LONG"] * u.deg, height=(geo.get("ELEV") or 0.0) * u.m)
        target = SolarTarget(location)
        max_alt = (snapshot.get("Slew_elevation_Limit") or {}).get("maxAlt")
        max_alt = max_alt if max_alt is not None else DEFAULT_MAX_ALTITUDE_DEG
        min_alt = args.min_altitude if args.min_altitude is not None else DEFAULT_MIN_SOLAR_ALTITUDE_DEG
        preflight_result = check_solar_preflight(target, obstime, args.preflight_window_s,
                                                  min_altitude_deg=min_alt, max_altitude_deg=max_alt)
        gates.append(("solar_preflight", preflight_result.verdict == "PASS", preflight_result.reason))
        session.write_preflight(preflight_result.to_dict())
    else:
        gates.append(("solar_preflight", False, "skipped - observer location invalid, refusing to guess"))
        session.write_preflight({"verdict": "BLOCKED", "reason": "observer location invalid"})

    for name, passed, detail in gates:
        print(f"[PRECHECK] {name}: {'ok' if passed else 'FAIL'} ({detail})")

    failing = [(n, d) for n, p, d in gates if not p]
    report["gates"] = [{"name": n, "passed": p, "detail": d} for n, p, d in gates]
    if failing:
        reason = "; ".join(f"{n}: {d}" for n, d in failing)
        report["result"] = "BLOCKED"
        report["reason"] = reason
        session.write_hardware_test_result(report)
        print("\n" + "=" * 60)
        print("SOLAR HARDWARE TEST: BLOCKED")
        print(f"Reason: {reason}")
        print(f"Evidence: {session.dir}")
        print("=" * 60)
        return 2

    if args.backend == "real" and not args.yes:
        print("\nRefusing to touch real hardware without --yes.", file=sys.stderr)
        return 3

    # ================================================================ SOLAR TARGET
    #
    # CRITICAL, NEWLY-CONFIRMED FINDING (not present in the earlier
    # "~0.15 deg, corrected" report): calling `.icrs` DIRECTLY on a Sun
    # coordinate that carries its real ~1 AU distance (as
    # SolarTarget.current_position()/sun_eod() returns) is NOT a small
    # rotation - astropy correctly performs a full 3D barycentric
    # conversion, and because the Sun's true distance from the solar-
    # system barycenter is comparable to the scale of that shift, the
    # result is a wildly wrong direction (measured 2026-09-18: ~78 deg off,
    # not the ~20 arcsec the rest of this codebase assumes). This is
    # exactly what alignment_engine/targets/solar.py's resolve_offset()
    # does (`self.current_position(obstime).icrs`) - see
    # test_alignment_engine_cirs_icrs_regression.py's new regression test
    # for the full reproduction. It affects any FUTURE solar raster/offset
    # GOTO through resolve_offset(), NOT this test: the actual command
    # below is built from `sun_eod_to_send` directly (CIRS, never touching
    # `.icrs`), and this SOLAR TARGET section only needs a *label* for the
    # log/evidence, computed correctly here by stripping the distance
    # before the frame rotation (a legitimate, distance-independent
    # re-expression of the same apparent direction, not a body position).
    sun_apparent = target.current_position(obstime)
    sun_icrs = SkyCoord(ra=sun_apparent.ra, dec=sun_apparent.dec,
                         frame=sun_apparent.frame.replicate_without_data()).icrs
    solar_target_record = {
        "obstime_utc": obstime.utc.isot,
        "sun_ra_icrs_hours": float(sun_icrs.ra.hour), "sun_dec_icrs_deg": float(sun_icrs.dec.deg),
        "site_lat_deg": geo["LAT"], "site_lon_deg": geo["LONG"], "site_elev_m": geo.get("ELEV"),
        "altitude_now_deg": preflight_result.altitude_now_deg, "azimuth_now_deg": preflight_result.azimuth_now_deg,
        "altitude_end_deg": preflight_result.altitude_end_deg, "azimuth_end_deg": preflight_result.azimuth_end_deg,
        "note": ("Single centered GOTO (offset east=0, north=0) via SolarTarget.current_position() / "
                 "sun_eod() directly - no SkyOffsetFrame/resolve_offset() tangent-plane geometry is "
                 "invoked anywhere in this path, so the documented CIRS-origin-without-obstime bug in "
                 "alignment.py V2's offset math cannot affect this test."),
    }
    session.write_solar_target(solar_target_record)
    print(f"[SOLAR TARGET] ICRS RA={sun_icrs.ra.hour:.6f}h DEC={sun_icrs.dec.deg:.4f} deg "
          f"| Alt now={preflight_result.altitude_now_deg:.2f} deg")

    # ================================================================ MAIN FLOW
    if args.backend == "real":
        tracking_backend_inner = RealTrackingBackend(host=host, port=port, device_name=DEVICE_NAME,
                                                       allow_real_writes=True, default_timeout=timeout)
        mount_adapter = RealMountAdapter(host=host, port=port, device=DEVICE_NAME)
    else:
        tracking_backend_inner = SimulatedTrackingBackend(original_mode)
        mount_adapter = SimulatedMountAdapter()
    audited_tracking = _WriteAuditingBackend(tracking_backend_inner)

    write_entries: list = []
    cancelled = False
    step_error: Optional[str] = None
    ts_object = None
    track_state_guard = None
    goto_ok: Optional[bool] = None
    angular_error_deg: Optional[float] = None
    onstep_error = False
    target_ra_dec = None
    reported_ra_dec = None
    goto_duration_s: Optional[float] = None
    stability = None

    await mount_adapter.connect()
    try:
        async with TrackingSession(audited_tracking, TrackingMode.SOLAR, session, timeout=timeout) as ts:
            ts_object = ts
            print(f"[TRACKING] mode SOLAR confirmed by readback (original was {original_mode.value})")

            send_time = obstime_provider()
            sun_eod_to_send = target.current_position(send_time)
            target_ra_dec = {"ra_hours": float(sun_eod_to_send.ra.hour), "dec_deg": float(sun_eod_to_send.dec.deg)}
            goto_command_record = {
                "property": "EQUATORIAL_EOD_COORD", "on_coord_set": "SLEW",
                "target_ra_hours": target_ra_dec["ra_hours"], "target_dec_deg": target_ra_dec["dec_deg"],
                "frame": "CIRS(obstime=send_time) - of-date, matches EQUATORIAL_EOD_COORD's documented contract",
                "send_time_utc": send_time.utc.isot, "issued_utc": _utcnow_iso(),
            }
            session.write_goto_command(goto_command_record)
            write_entries.append({"property": "ON_COORD_SET", "elements": {"TRACK": "Off", "SLEW": "On", "SYNC": "Off"}})
            write_entries.append({"property": "EQUATORIAL_EOD_COORD",
                                   "elements": {"RA": str(target_ra_dec["ra_hours"]), "DEC": str(target_ra_dec["dec_deg"])}})

            print(f"[GOTO] single GOTO to RA={target_ra_dec['ra_hours']:.6f}h DEC={target_ra_dec['dec_deg']:.4f} deg (EOD)")
            t_goto_start = time.monotonic()
            goto_ok = await mount_adapter.goto(sun_eod_to_send)
            goto_duration_s = time.monotonic() - t_goto_start
            session.write_goto_progress({
                "started_utc": goto_command_record["issued_utc"], "finished_utc": _utcnow_iso(),
                "duration_s": goto_duration_s, "returned_ok": goto_ok,
                "note": "incremental polling happens inside indi_telescope_control.INDITelescopeControl.goto() "
                        "(already hardware-proven, not re-implemented here); this harness observes only start/end.",
            })
            print(f"[GOTO] returned_ok={goto_ok} duration={goto_duration_s:.2f}s")

            reported_position = await mount_adapter.get_position()
            if goto_ok and reported_position is not None:
                reported_ra_dec = {"ra_hours": float(reported_position.ra.hour), "dec_deg": float(reported_position.dec.deg)}
                angular_error_deg = float(sun_eod_to_send.separation(reported_position).deg)
            final_status = "OK" if (goto_ok and angular_error_deg is not None and angular_error_deg <= ARRIVAL_TOLERANCE_DEG) else "FAIL"
            session.write_goto_result({
                "target_ra_dec": target_ra_dec, "reported_ra_dec": reported_ra_dec,
                "angular_error_deg": angular_error_deg, "tolerance_deg": ARRIVAL_TOLERANCE_DEG,
                "slew_duration_s": goto_duration_s, "final_status": final_status,
            })
            print(f"[VERIFY GOTO] angular_error_deg={angular_error_deg} tolerance={ARRIVAL_TOLERANCE_DEG} status={final_status}")

            if final_status == "OK":
                telescope_for_track_state = (mount_adapter._telescope if args.backend == "real"
                                              else _SimulatedTrackStateTelescope(original_track_state_str == "ON"))
                track_state_guard = _TrackStateGuard(telescope_for_track_state,
                                                      original_on=(original_track_state_str == "ON"),
                                                      session=session, timeout=timeout)
                try:
                    await track_state_guard.enable()
                    print(f"[TRACK_STATE] enabled for post-GOTO stability check "
                          f"(original was {original_track_state_str})")
                    stability = await _run_stability_check(host, port, timeout, args.stability_wait_s,
                                                             args.stability_poll_interval_s, args.backend)
                    session.write_stability_check(stability)
                    onstep_error = bool(stability.get("error_detected") or stability.get("busy_detected")
                                         or stability.get("mode_changed"))
                    print(f"[STABILITY] {stability['sample_count']} samples over {stability['duration_s']}s "
                          f"busy_detected={stability['busy_detected']} error_detected={stability['error_detected']} "
                          f"mode_changed={stability['mode_changed']}")
                finally:
                    if track_state_guard.enabled:
                        await track_state_guard.restore()
                        write_entries.extend(track_state_guard.writes)
            else:
                print("[TRACK_STATE] not enabled - GOTO did not converge within tolerance, nothing to track")
    except asyncio.CancelledError as exc:
        cancelled = True
        step_error = f"CancelledError: {exc!r}"
    except KeyboardInterrupt as exc:
        cancelled = True
        step_error = f"KeyboardInterrupt: {exc!r}"
    except Exception as exc:
        step_error = f"{type(exc).__name__}: {exc}"
        print(f"[ERROR] {step_error}", file=sys.stderr)
    finally:
        if args.backend == "real":
            await mount_adapter.disconnect()

    track_mode_restored = bool(getattr(ts_object, "restored_mode", None))
    track_state_restored = track_state_guard.restored if (track_state_guard and track_state_guard.enabled) else True
    write_entries.extend(audited_tracking.audit()["writes"])
    write_audit = _combine_write_audit(write_entries)
    session.write_write_audit(write_audit)
    print(f"[WRITE AUDIT] write_count={write_audit['write_count']} properties={write_audit['properties_written']} "
          f"unexpected={write_audit['unexpected_write_count']}")

    # ================================================================ MOUNT AFTER / POSTCHECK
    try:
        after_snapshot = await _snapshot(host, port, timeout) if args.backend == "real" else dict(snapshot, timestamp_utc=_utcnow_iso())
    except Exception as exc:
        after_snapshot = {"timestamp_utc": _utcnow_iso(), "error": f"{type(exc).__name__}: {exc}"}
    session.write_mount_after(after_snapshot)

    verdict = compute_goto_verdict(
        gates_passed=True, cancelled=cancelled, step_error=step_error,
        solar_confirmed=bool(ts_object is not None), goto_ok=goto_ok,
        angular_error_deg=angular_error_deg, tolerance_deg=ARRIVAL_TOLERANCE_DEG,
        onstep_error=onstep_error, unexpected_write_count=write_audit["unexpected_write_count"],
        track_mode_restored=track_mode_restored, track_state_restored=track_state_restored,
    )

    report.update({
        "original_mode": original_mode.value, "original_track_state": original_track_state_str,
        "solar_target": solar_target_record, "target_ra_dec": target_ra_dec, "reported_ra_dec": reported_ra_dec,
        "angular_error_deg": angular_error_deg, "tolerance_deg": ARRIVAL_TOLERANCE_DEG,
        "goto_ok": goto_ok, "goto_duration_s": goto_duration_s,
        "track_mode_restored": track_mode_restored, "track_state_restored": track_state_restored,
        "onstep_error": onstep_error, "step_error": step_error, "cancelled": cancelled,
        "write_audit": write_audit, "stability": stability, "result": verdict, "finished_utc": _utcnow_iso(),
    })
    session.write_hardware_test_result(report)
    session.write_state({"phase": "done", "result": verdict})

    print("\n" + "=" * 60)
    print(f"RESULT: {verdict}")
    print(f"Evidence: {session.dir}")
    print("=" * 60)
    return {"PASS": 0, "BLOCKED": 2, "CANCELLED": 5, "INCONCLUSIVE": 4}.get(verdict, 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["simulated", "real"], default="simulated")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7624)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--min-altitude", type=float, default=None)
    parser.add_argument("--preflight-window-s", type=float, default=240.0)
    parser.add_argument("--stability-wait-s", type=float, default=15.0)
    parser.add_argument("--stability-poll-interval-s", type=float, default=3.0)
    parser.add_argument("--session-root", default="data/alignment")
    parser.add_argument("--orchestrator-runtime-dir", default=None)
    parser.add_argument("--simulate-obstime", default=None,
                         help="ISO time override for --backend simulated rehearsals ONLY - "
                              "ignored (never applied) for --backend real")
    return parser


if __name__ == "__main__":
    parsed = build_parser().parse_args()
    raise SystemExit(asyncio.run(main(parsed)))
