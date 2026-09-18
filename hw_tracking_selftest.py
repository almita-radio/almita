#!/usr/bin/env python3
"""First real-hardware test (explicitly authorized, one specific scope
only): confirm the new async tracking backend can, against the REAL LX200
OnStep mount, read the current tracking mode, switch it to SOLAR, verify
the switch, restore the original mode, and verify the restore.

NEVER does, and cannot be made to do via any flag on this script:
GOTO, SLEW, SYNC, PARK, UNPARK, HOME, writes to EQUATORIAL_EOD_COORD,
ON_COORD_SET, TARGET_EOD_COORD, or TELESCOPE_TRACK_STATE. The only
property this script ever writes is TELESCOPE_TRACK_MODE, and only
through RealTrackingBackend/TrackingSession - the same production classes
alignment_engine itself uses, not a parallel hand-rolled client. Every
write it performs is recorded by _WriteAuditingBackend below and checked
against that one-property whitelist before a PASS is ever reported.

Safety design:
  - Defaults to --backend simulated (a full rehearsal of this script's own
    orchestration logic against SimulatedTrackingBackend) - real hardware
    is touched ONLY with --backend real --yes together.
  - Preflight: capture-conflict check (reused from alignment_engine,
    read-only, fail-closed) and a live mount-reachability read, BEFORE
    RealTrackingBackend is ever constructed with allow_real_writes=True.
  - original tracking mode is whatever get_tracking_mode() reports - never
    assumed to be SIDEREAL.
  - The SOLAR<->original round trip goes through TrackingSession, so the
    restore-on-any-failure guarantee is the exact same one every other
    async engine path already relies on and this pass's own tests cover.
  - EQUATORIAL_EOD_COORD and ON_COORD_SET are read (never written) before
    and after, to confirm no side effect.
  - Motion detection uses alignment_engine.motion_detector's Hour-Angle
    model (see that module's docstring) instead of a naive RA/DEC delta,
    which produces false positives for an idle (TRACK_STATE=OFF) mount as
    time passes - discovered on the first real run of this script
    (data/alignment/HW-TRACKING-20260918-004605/), root-caused with
    evidence rather than papered over with a looser threshold.
  - Every step's evidence is persisted under a dedicated
    data/alignment/HW-TRACKING-<UTC timestamp>/ session directory.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from alignment_engine import capture_conflict
from alignment_engine.motion_detector import classify_motion, track_state_from_switch_vector
from alignment_engine.session import AlignmentSession
from alignment_engine.tracking import (
    RealTrackingBackend, SimulatedTrackingBackend, TrackingMode, TrackingSession,
    parse_number_vector, parse_switch_vector, read_property_readonly,
)

DEVICE_NAME = "LX200 OnStep"
# The only property this harness is ever allowed to write - checked, not
# just asserted in a docstring (see _WriteAuditingBackend / write_audit).
WRITE_WHITELIST = {"TELESCOPE_TRACK_MODE"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class _WriteAuditingBackend:
    """Wraps a TrackingBackend and records every write it performs. This is
    test-harness-local instrumentation (the harness auditing its OWN
    actions), not a new core abstraction - it does not belong in
    tracking.py, which has no business knowing about a specific script's
    write whitelist. get_tracking_mode() is passed through unchanged;
    set_tracking_mode() is the only place a write can originate here."""

    def __init__(self, inner):
        self._inner = inner
        self.writes: list = []  # [{"property": str, "elements": {name: "On"/"Off"}}]

    async def get_tracking_mode(self, *args, **kwargs):
        return await self._inner.get_tracking_mode(*args, **kwargs)

    async def set_tracking_mode(self, mode, *args, **kwargs):
        result = await self._inner.set_tracking_mode(mode, *args, **kwargs)
        element_by_mode = getattr(self._inner, "ELEMENT_BY_MODE", RealTrackingBackend.ELEMENT_BY_MODE)
        target_element = element_by_mode.get(mode)
        elements = {name: ("On" if name == target_element else "Off") for name in element_by_mode.values()}
        self.writes.append({"property": "TELESCOPE_TRACK_MODE", "elements": elements})
        return result

    def audit(self) -> dict:
        properties_written = sorted({w["property"] for w in self.writes})
        elements_written = sorted({name for w in self.writes for name, value in w["elements"].items() if value == "On"})
        unexpected = [w for w in self.writes if w["property"] not in WRITE_WHITELIST]
        return {
            "write_count": len(self.writes),
            "properties_written": properties_written,
            "elements_written": elements_written,
            "unexpected_write_count": len(unexpected),
            "unexpected_writes": unexpected,
            "writes": self.writes,
        }


def _mode_from_switch_vector(vector: dict):
    """Returns a TrackingMode, or None if the vector is missing/ambiguous/
    unreadable/uses an element this package doesn't recognize - never
    guesses. `vector` is {'TRACK_SIDEREAL': 'On', ...} (INDI element
    names), which RealTrackingBackend.MODE_BY_ELEMENT maps to TrackingMode."""
    if not vector:
        return None
    on = [k for k, v in vector.items() if v == "On"]
    if len(on) != 1:
        return None
    return RealTrackingBackend.MODE_BY_ELEMENT.get(on[0])


async def _snapshot(host: str, port: int, timeout: float) -> dict:
    """Read-only. TELESCOPE_TRACK_MODE/STATE, EQUATORIAL_EOD_COORD,
    ON_COORD_SET, GEOGRAPHIC_COORD - never writes anything."""
    track_mode_raw = await read_property_readonly(host, port, DEVICE_NAME, "TELESCOPE_TRACK_MODE", timeout)
    track_state_raw = await read_property_readonly(host, port, DEVICE_NAME, "TELESCOPE_TRACK_STATE", timeout)
    coord_raw = await read_property_readonly(host, port, DEVICE_NAME, "EQUATORIAL_EOD_COORD", timeout)
    on_coord_set_raw = await read_property_readonly(host, port, DEVICE_NAME, "ON_COORD_SET", timeout)
    geo_raw = await read_property_readonly(host, port, DEVICE_NAME, "GEOGRAPHIC_COORD", timeout)
    return {
        "timestamp_utc": _utcnow_iso(),
        "TELESCOPE_TRACK_MODE": parse_switch_vector(track_mode_raw),
        "TELESCOPE_TRACK_STATE": parse_switch_vector(track_state_raw),
        "EQUATORIAL_EOD_COORD": parse_number_vector(coord_raw),
        "ON_COORD_SET": parse_switch_vector(on_coord_set_raw),
        "GEOGRAPHIC_COORD": parse_number_vector(geo_raw),
    }


def compute_final_verdict(*, step_error: Optional[str], solar_confirmed: bool, restore_confirmed: bool,
                           on_coord_set_unchanged: bool, unexpected_write_count: int,
                           motion_verdict: str) -> str:
    """Pure, no I/O - "PASS" / "FAIL" / "INCONCLUSIVE". Item 5's rule:
    INCONCLUSIVE only ever downgrades a would-be PASS, and never masks an
    actual FAIL condition (a hard-safety failure always wins)."""
    hard_fail = (
        step_error is not None
        or not solar_confirmed
        or not restore_confirmed
        or not on_coord_set_unchanged
        or unexpected_write_count != 0
        or motion_verdict == "FAIL"
    )
    if hard_fail:
        return "FAIL"
    if motion_verdict == "INCONCLUSIVE":
        return "INCONCLUSIVE"
    return "PASS"


async def main(args) -> int:
    host, port, timeout = args.host, args.port, args.timeout
    report: dict = {
        "test": "HW-TRACKING-SOLAR-ROUNDTRIP", "backend": args.backend,
        "device": DEVICE_NAME, "host": host, "port": port,
        "started_utc": _utcnow_iso(),
    }

    session_id = f"HW-TRACKING-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    session = AlignmentSession(args.session_root, session_id)
    print(f"Session: {session.dir}")

    # ---------------------------------------------------------------- A. PRECHECK
    precheck: dict = {"timestamp_utc": _utcnow_iso()}

    conflict = capture_conflict.check_no_conflicting_capture(runtime_dir=args.orchestrator_runtime_dir)
    precheck["capture_conflict"] = {"conflict": conflict.conflict, "detail": conflict.detail}
    print(f"[PRECHECK] capture conflict: {conflict.conflict} ({conflict.detail})")

    if args.backend == "real":
        before_snapshot = await _snapshot(host, port, timeout)
    else:
        before_snapshot = {
            "timestamp_utc": _utcnow_iso(),
            "TELESCOPE_TRACK_MODE": {"TRACK_SIDEREAL": "On", "TRACK_SOLAR": "Off",
                                      "TRACK_LUNAR": "Off", "TRACK_CUSTOM": "Off"},
            "TELESCOPE_TRACK_STATE": {"TRACK_ON": "Off", "TRACK_OFF": "On"},
            "EQUATORIAL_EOD_COORD": {"RA": 12.0, "DEC": -80.0},
            "ON_COORD_SET": {"TRACK": "Off", "SLEW": "On", "SYNC": "Off"},
            # A fake-but-labeled site, present only so this rehearsal exercises the
            # PASS branch of the motion check too (not just INCONCLUSIVE) - a real
            # `--backend real` run always reads this from GEOGRAPHIC_COORD instead.
            "GEOGRAPHIC_COORD": {"LAT": -33.4331, "LONG": 289.3336, "ELEV": 550.0},
        }
    precheck["snapshot_before"] = before_snapshot
    session.write_precheck(precheck)

    mount_reachable = before_snapshot.get("TELESCOPE_TRACK_MODE") is not None
    original_mode = _mode_from_switch_vector(before_snapshot.get("TELESCOPE_TRACK_MODE") or {})
    print(f"[PRECHECK] mount reachable: {mount_reachable}")
    print(f"[PRECHECK] TELESCOPE_TRACK_MODE (original): {original_mode.value if original_mode else None}")
    print(f"[PRECHECK] TELESCOPE_TRACK_STATE: {before_snapshot.get('TELESCOPE_TRACK_STATE')}")
    print(f"[PRECHECK] EQUATORIAL_EOD_COORD: {before_snapshot.get('EQUATORIAL_EOD_COORD')}")
    print(f"[PRECHECK] ON_COORD_SET: {before_snapshot.get('ON_COORD_SET')}")

    report["precheck"] = precheck

    if conflict.conflict:
        report["result"] = "FAIL"
        report["reason"] = f"aborted before any write: capture conflict ({conflict.detail})"
        session.write_hardware_test_result(report)
        print(f"\nRESULT: FAIL - {report['reason']}")
        return 2

    if not mount_reachable:
        report["result"] = "FAIL"
        report["reason"] = "aborted before any write: mount not reachable / TELESCOPE_TRACK_MODE unreadable"
        session.write_hardware_test_result(report)
        print(f"\nRESULT: FAIL - {report['reason']}")
        return 2

    if original_mode is None:
        report["result"] = "FAIL"
        report["reason"] = (f"aborted before any write: unrecognized/ambiguous original mode "
                             f"{before_snapshot.get('TELESCOPE_TRACK_MODE')!r}")
        session.write_hardware_test_result(report)
        print(f"\nRESULT: FAIL - {report['reason']}")
        return 2

    if original_mode != TrackingMode.SIDEREAL:
        print(f"[PRECHECK] NOTE: original tracking mode is {original_mode.value}, not SIDEREAL - "
              f"proceeding with ORIGINAL -> SOLAR -> ORIGINAL as instructed (never assuming SIDEREAL).")

    # ---------------------------------------------------------------- backend construction
    if args.backend == "real":
        if not args.yes:
            print("\nRefusing to touch real hardware without --yes (explicit human authorization "
                  "already given in this session, but this script still requires the flag).", file=sys.stderr)
            return 3
        backend = RealTrackingBackend(host=host, port=port, device_name=DEVICE_NAME,
                                       allow_real_writes=True, default_timeout=timeout)
    else:
        backend = SimulatedTrackingBackend(original_mode)
    audited_backend = _WriteAuditingBackend(backend)

    # ---------------------------------------------------------------- B/C/D/E/F via TrackingSession
    timings: dict = {}
    solar_readback = None
    restore_readback = None
    step_error = None
    ts_object = None  # populated only if TrackingSession.__aenter__ succeeds
    t_before_exit = None  # checkpoint set right before the `async with` block ends

    t_enter_start = time.monotonic()
    try:
        async with TrackingSession(audited_backend, TrackingMode.SOLAR, session, timeout=timeout) as ts:
            ts_object = ts
            timings["enter_seconds"] = time.monotonic() - t_enter_start  # B (write) + C (verify)
            solar_readback = await audited_backend.get_tracking_mode()
            print(f"[VERIFY SOLAR] readback: {solar_readback.value if solar_readback else None} "
                  f"(in {timings['enter_seconds']:.3f}s)")

            # D: wait only long enough to confirm property stability - no slew.
            print(f"[STABILITY WAIT] sleeping {args.stability_wait_s}s (no slew, no coordinate writes)")
            await asyncio.sleep(args.stability_wait_s)
            solar_readback_after_wait = await audited_backend.get_tracking_mode()
            timings["stable_after_wait"] = solar_readback_after_wait == TrackingMode.SOLAR
            t_before_exit = time.monotonic()  # E+F (restore+verify) timed from here to after the block
    except Exception as exc:
        step_error = f"{type(exc).__name__}: {exc}"
        print(f"[ERROR] {step_error}", file=sys.stderr)
    finally:
        if t_before_exit is not None:
            timings["exit_seconds"] = time.monotonic() - t_before_exit  # E (restore) + F (verify)
        else:
            # __aenter__ itself failed (or raised before reaching the last
            # body line) - there is no well-defined "exit phase" duration.
            timings["exit_seconds"] = None

    restore_error = getattr(ts_object, "restore_error", None)
    ts_restored_mode = getattr(ts_object, "restored_mode", None)

    # A fresh, independent read-only confirmation of the final state -
    # never trust only what TrackingSession itself recorded internally.
    # This is exactly the "one read-only diagnostic read" the authorization
    # allows even after a failure - never another write.
    try:
        restore_readback = await audited_backend.get_tracking_mode()
    except Exception as exc:
        restore_readback = None
        step_error = step_error or f"final readback failed: {type(exc).__name__}: {exc}"
    print(f"[VERIFY RESTORE] readback: {restore_readback.value if restore_readback else None}")

    write_audit = audited_backend.audit()
    session.write_write_audit(write_audit)
    print(f"[WRITE AUDIT] write_count={write_audit['write_count']} "
          f"properties_written={write_audit['properties_written']} "
          f"unexpected_write_count={write_audit['unexpected_write_count']}")

    # ---------------------------------------------------------------- F. POSTCHECK
    postcheck: dict = {"timestamp_utc": _utcnow_iso()}
    try:
        if args.backend == "real":
            after_snapshot = await _snapshot(host, port, timeout)
        else:
            after_snapshot = dict(before_snapshot)
            after_snapshot["timestamp_utc"] = _utcnow_iso()
    except Exception as exc:
        after_snapshot = {"timestamp_utc": _utcnow_iso(), "error": f"{type(exc).__name__}: {exc}"}
    postcheck["snapshot_after"] = after_snapshot
    session.write_postcheck(postcheck)

    coord_before = before_snapshot.get("EQUATORIAL_EOD_COORD") or {}
    coord_after = after_snapshot.get("EQUATORIAL_EOD_COORD") or {}
    coord_delta = None
    if coord_before.get("RA") is not None and coord_after.get("RA") is not None:
        coord_delta = {
            "delta_ra_hours": coord_after["RA"] - coord_before["RA"],
            "delta_dec_deg": coord_after["DEC"] - coord_before["DEC"],
        }

    # Physical motion check (Hour Angle + DEC model - see motion_detector.py
    # module docstring for why a raw RA/DEC delta is the wrong invariant
    # here). Site location comes from GEOGRAPHIC_COORD read read-only from
    # the mount itself - never hardcoded, never guessed.
    geo_before = before_snapshot.get("GEOGRAPHIC_COORD") or {}
    track_state_before_str = track_state_from_switch_vector(before_snapshot.get("TELESCOPE_TRACK_STATE"))
    track_state_after_str = track_state_from_switch_vector(after_snapshot.get("TELESCOPE_TRACK_STATE"))
    motion = classify_motion(
        ra_before_hours=coord_before.get("RA"), dec_before_deg=coord_before.get("DEC"),
        ra_after_hours=coord_after.get("RA"), dec_after_deg=coord_after.get("DEC"),
        ts_before_utc=_parse_iso(before_snapshot.get("timestamp_utc")),
        ts_after_utc=_parse_iso(after_snapshot.get("timestamp_utc")),
        track_state_before=track_state_before_str, track_state_after=track_state_after_str,
        site_lat_deg=geo_before.get("LAT"), site_lon_deg=geo_before.get("LONG"),
    )
    session.write_motion_check(motion.to_dict())

    on_coord_set_unchanged = (before_snapshot.get("ON_COORD_SET") == after_snapshot.get("ON_COORD_SET"))

    print(f"[POSTCHECK] EQUATORIAL_EOD_COORD before/after: {coord_before} / {coord_after}")
    print(f"[POSTCHECK] coordinate delta (raw, informational only): {coord_delta}")
    print(f"[POSTCHECK] ON_COORD_SET unchanged: {on_coord_set_unchanged}")
    print(f"[POSTCHECK] motion check: {motion.verdict} - {motion.reason}")

    # ---------------------------------------------------------------- G. FINAL VERDICT
    solar_confirmed = solar_readback == TrackingMode.SOLAR
    restore_confirmed = restore_readback == original_mode
    final_verdict = compute_final_verdict(
        step_error=step_error, solar_confirmed=solar_confirmed, restore_confirmed=restore_confirmed,
        on_coord_set_unchanged=on_coord_set_unchanged,
        unexpected_write_count=write_audit["unexpected_write_count"], motion_verdict=motion.verdict,
    )

    report.update({
        "original_mode": original_mode.value,
        "requested_mode": TrackingMode.SOLAR.value,
        "solar_readback": solar_readback.value if solar_readback else None,
        "solar_confirmed": solar_confirmed,
        "restore_readback": restore_readback.value if restore_readback else None,
        "restore_confirmed": restore_confirmed,
        "ts_restored_mode": ts_restored_mode.value if ts_restored_mode else None,
        "restore_error": restore_error,
        "step_error": step_error,
        "timings_seconds": timings,
        "write_audit": write_audit,
        "postcheck": {
            "coordinate_delta": coord_delta,
            "on_coord_set_unchanged": on_coord_set_unchanged,
            "motion_check": motion.to_dict(),
        },
        "result": final_verdict,
        "finished_utc": _utcnow_iso(),
    })
    session.write_hardware_test_result(report)

    print("\n" + "=" * 60)
    print(f"RESULT: {report['result']}")
    print(f"  original={original_mode.value} solar_confirmed={solar_confirmed} "
          f"restore_confirmed={restore_confirmed} motion={motion.verdict} "
          f"on_coord_set_unchanged={on_coord_set_unchanged} "
          f"unexpected_write_count={write_audit['unexpected_write_count']}")
    if step_error:
        print(f"  step_error: {step_error}")
    if restore_error:
        print(f"  restore_error: {restore_error}")
    print(f"Evidence: {session.dir}")
    print("=" * 60)

    if final_verdict == "PASS":
        return 0
    if final_verdict == "INCONCLUSIVE":
        return 4
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["simulated", "real"], default="simulated",
                         help="default 'simulated' rehearses this script's own logic; 'real' touches hardware")
    parser.add_argument("--yes", action="store_true", help="required together with --backend real")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7624)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--stability-wait-s", type=float, default=3.0)
    parser.add_argument("--session-root", default="data/alignment")
    parser.add_argument("--orchestrator-runtime-dir", default=None)
    return parser


if __name__ == "__main__":
    parsed = build_parser().parse_args()
    raise SystemExit(asyncio.run(main(parsed)))
