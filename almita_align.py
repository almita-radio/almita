#!/usr/bin/env python3
"""ALMITA Alignment CLI - thin wrapper over alignment_engine (Fase 14).

Every subcommand does argument parsing and printing only; every decision
(state transitions, fitting, sync eligibility) happens inside
alignment_engine, importable and callable the exact same way a future
:8090 API would call it - nothing here is science logic.

--json switches every subcommand's stdout to a single JSON document (never
mixed with human-readable text) so this CLI is scriptable/automatable
today, ahead of any real API.

Hardware note: `solar/hi plan|preflight|run|sync|verify` only ever
construct SimulatedMountAdapter/SimulatedTrackingBackend - there is no
--mount real flag, and real GOTO/tracking-mode-write/SYNC against the
physical mount is deliberately not wired into any of those commands (see
the design report this CLI's implementation followed); `sync --apply` and
`verify` still exercise the full engine logic, but against a simulated
mount, for development and demonstration. The one exception is `solar/hi
tracking --dry-run` (3rd pass, item 10), which DOES query the real
indiserver - but read-only (RealTrackingBackend.get_tracking_mode()) - to
show CURRENT state; it never calls execute_set_tracking_mode() no matter
what flags are passed.

ASYNC BOUNDARY (3rd pass, item 6): exactly ONE asyncio.run() call exists
in this whole file - inside main(), and only for the subcommands whose
handler is `async def` (preflight/run/sync/verify/tracking - the ones that
reach into TrackingSession/mount I/O through the engine, or query the real
backend directly). plan/status/result stay plain `def` and are called
directly with no event loop at all. No handler here ever calls
asyncio.run() itself - see engine.py/tracking.py/sync_flow.py's own
docstrings for why that boundary is enforced there too, and
test_async_architecture_boundary.py for the permanent regression guard.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore", module="astropy")

from astropy.coordinates import EarthLocation
from astropy.time import Time
import astropy.units as u

from alignment_engine.config import AlignmentConfig
from alignment_engine.engine import AlignmentEngine
from alignment_engine.mount_adapter import SimulatedMountAdapter
from alignment_engine.simulation import HIMapSimConfig, SolarBeamSimConfig
from alignment_engine.targets.hi_reference import SyntheticHIReferenceProvider
from alignment_engine.targets.solar import SolarTarget
from alignment_engine.tracking import SimulatedTrackingBackend


def _location_from_config(config: AlignmentConfig) -> EarthLocation:
    observer = json.loads(Path(config.global_.observer_config_path).read_text())["observer"]
    return EarthLocation(lat=observer["latitude_deg"] * u.deg, lon=observer["longitude_deg"] * u.deg,
                          height=observer["elevation_m"] * u.m)


def _print(payload: dict, as_json: bool, human_lines) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in human_lines:
            print(line)


def _event_sink(as_json: bool, collected: list) -> callable:
    """Item 8 (progress events): the engine emits plain dicts; this is the
    CLI's formatter for them - a human progress line to stderr (never
    stdout, so it can't contaminate --json or plain-text stdout), and/or
    collection into `collected` for inclusion in a --json payload. A
    future web layer would plug in a different sink (e.g. push onto an
    asyncio.Queue / websocket) without the engine changing at all."""
    def _sink(event: dict) -> None:
        collected.append(event)
        if not as_json:
            if event["type"] == "state":
                print(f"  -> {event['state']} ({event.get('reason', '')})", file=sys.stderr)
            elif event["type"] == "fit":
                print(f"  -> fit[{event.get('stage')}] offset=({event.get('offset_east_deg'):+.3f}, "
                      f"{event.get('offset_north_deg'):+.3f}) rating={event.get('rating')}", file=sys.stderr)
    return _sink


def _engine_for(mode: str, args, resume: bool, events: Optional[list] = None) -> AlignmentEngine:
    config = AlignmentConfig.load(args.config)
    if args.observer_config:
        config.global_.observer_config_path = args.observer_config
    if getattr(args, "min_altitude", None) is not None:
        (config.solar if mode == "solar" else config.hi).min_altitude_deg = args.min_altitude
    location = _location_from_config(config)
    from alignment_engine.session import AlignmentSession
    session_id = getattr(args, "session", None)
    session = AlignmentSession(config.global_.output_root, session_id) if session_id else None
    on_event = _event_sink(args.json, events) if events is not None else None
    return AlignmentEngine(mode, config, location, SimulatedMountAdapter(), SimulatedTrackingBackend(),
                            session=session, resume=resume, on_event=on_event)


def cmd_plan(mode: str, args) -> int:
    engine = _engine_for(mode, args, resume=False)
    engine.plan()
    _print({"session_id": engine.session.session_id, "state": engine.state_machine.state.value}, args.json,
           [f"Session planned: {engine.session.session_id}",
            f"State: {engine.state_machine.state.value}"])
    return 0


async def cmd_preflight(mode: str, args) -> int:
    engine = _engine_for(mode, args, resume=True)
    provider = SolarTarget(engine.location) if mode == "solar" else SyntheticHIReferenceProvider(args.catalog)
    checks = await engine.preflight(provider)
    ok = all(c.ok for c in checks)
    _print({"session_id": engine.session.session_id, "ok": ok,
            "checks": [vars(c) for c in checks]}, args.json,
           [f"Preflight for {engine.session.session_id}: {'PASS' if ok else 'FAIL'}"] +
           [f"  [{'OK' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in checks])
    return 0 if ok else 2


async def cmd_run(mode: str, args) -> int:
    if not args.simulate:
        print("Only --simulate is supported in this version - real hardware acquisition "
              "is not wired pending the second, hardware-facing authorization.", file=sys.stderr)
        return 3
    events: list = []
    engine = _engine_for(mode, args, resume=True, events=events)
    if mode == "solar":
        sim = SolarBeamSimConfig(true_offset_east_deg=args.true_offset_east, true_offset_north_deg=args.true_offset_north,
                                  fwhm_deg=args.fwhm, noise_fraction=args.noise, seed=args.seed)
        result = await engine.run_solar_simulated(sim)
    else:
        provider = SyntheticHIReferenceProvider(args.catalog)
        sim = HIMapSimConfig(true_offset_east_deg=args.true_offset_east, true_offset_north_deg=args.true_offset_north,
                              gain_a=args.gain, baseline_b=args.baseline, noise_fraction=args.noise, seed=args.seed)
        result = await engine.run_hi_simulated(provider, sim)
    payload = result.to_dict()
    if args.json:
        payload["events"] = events  # item 8: JSON-friendly, same events the human view printed to stderr
    _print(payload, args.json,
           [f"Alignment run complete: {engine.session.session_id}",
            f"Offset RA/east:  {payload['offset_ra_deg']:+.3f} deg",
            f"Offset DEC/north: {payload['offset_dec_deg']:+.3f} deg",
            f"Fit rating: {payload['final_fit']['quality']['rating']} "
            f"(confidence={payload['final_fit']['quality']['confidence']:.3f})"])
    return 0


def cmd_status(args) -> int:
    from alignment_engine.session import AlignmentSession
    session = AlignmentSession(Path(args.session_root) if args.session_root else "data/alignment", args.session)
    state = session.read_state() or {"state": "UNKNOWN"}
    _print(state, args.json, [f"Session {args.session}: state={state.get('state')}"])
    return 0


def cmd_result(args) -> int:
    from alignment_engine.session import AlignmentSession
    session = AlignmentSession(Path(args.session_root) if args.session_root else "data/alignment", args.session)
    result = session.read_alignment_result()
    if result is None:
        print(f"No alignment_result.json for session {args.session}", file=sys.stderr)
        return 1
    _print(result, args.json,
           [f"Mode: {result['mode']}", f"Offset RA/east:  {result['offset_ra_deg']:+.3f} deg",
            f"Offset DEC/north: {result['offset_dec_deg']:+.3f} deg",
            f"Fit rating: {result['final_fit']['quality']['rating']}"])
    return 0


def _resume_engine(mode: str, args) -> AlignmentEngine:
    config = AlignmentConfig.load(args.config)
    location = _location_from_config(config)
    from alignment_engine.session import AlignmentSession
    session = AlignmentSession(Path(args.session_root) if args.session_root else config.global_.output_root,
                                args.session)
    return AlignmentEngine(mode, config, location, SimulatedMountAdapter(), SimulatedTrackingBackend(),
                            session=session, resume=True)


async def cmd_sync(args) -> int:
    from alignment_engine.engine import AlignmentResult
    from alignment_engine.fitting import FitResult
    from alignment_engine.session import AlignmentSession

    session = AlignmentSession(Path(args.session_root) if args.session_root else "data/alignment", args.session)
    stored = session.read_alignment_result()
    fit_stored = session.read_fit_result()
    if stored is None or fit_stored is None:
        print(f"No result to sync for session {args.session} - run `... run {args.session}` first.", file=sys.stderr)
        return 1

    from alignment import AlignmentEstimate
    from alignment_engine.fitting import FitQuality
    estimate = AlignmentEstimate(**fit_stored["estimate"])
    quality = FitQuality(**fit_stored["quality"])
    fit_result = FitResult(estimate=estimate, quality=quality, raw_values=[], valid_count=fit_stored["valid_count"],
                            total_count=fit_stored["total_count"], rejected_indices=fit_stored["rejected_indices"])
    result = AlignmentResult(mode=stored["mode"], session_id=args.session, coarse_fit=None, fine_fit=None,
                              final_fit=fit_result, tracking_mode=stored.get("tracking_mode"), warnings=[])

    is_observational = bool(stored.get("is_observational", stored["mode"] != "HI"))
    mode = "solar" if stored["mode"] == "SOLAR" else "hi"
    engine = _resume_engine(mode, args)
    if mode == "solar":
        # apparent_icrs_direction(), not a raw `.icrs` on current_position()
        # (see targets/solar.py's module docstring, BUG 2: finite Sun distance).
        center = SolarTarget(engine.location).apparent_icrs_direction(Time.now())
    else:
        provider = SyntheticHIReferenceProvider(args.catalog)
        center, _ = provider.choose_target(engine.location, Time.now(), engine.config.hi.min_altitude_deg,
                                            engine.config.resolved_beam_fwhm_deg("hi"))
        is_observational = provider.is_observational

    plan = engine.prepare_sync(result, center, is_observational, args.confidence_threshold)

    if not args.apply:
        lines = ["Alignment completed.", f"Measured offset: dEast={plan.measured_offset_east_deg:+.3f} deg "
                 f"dNorth={plan.measured_offset_north_deg:+.3f} deg", f"Quality: {plan.fit_rating} "
                 f"(confidence={plan.fit_confidence:.3f})", f"Eligible for SYNC: {plan.eligible} ({plan.eligibility_reason})"]
        if plan.real_indi_operations:
            lines.append("")
            lines.append("What a REAL SYNC would send (preview only - nothing sent, no hardware authorization "
                          "for real execution exists in this version):")
            for op in plan.real_indi_operations:
                elements = ", ".join(f"{k}={v}" for k, v in op["elements"].items())
                frame = f" [{op['coordinate_frame']}]" if op["coordinate_frame"] else ""
                lines.append(f"  {op['step']}. {op['device']}.{op['property']}: {elements}{frame} - {op['operation']}")
        lines.append("Run again with --apply to send SYNC (simulated mount only in this version).")
        _print(plan.to_dict(), args.json, lines)
        return 0 if plan.eligible else 2

    if not plan.eligible:
        print(f"SYNC refused: {plan.eligibility_reason}", file=sys.stderr)
        return 2
    if not args.yes:
        answer = input(f"Apply SYNC for session {args.session}? Measured offset "
                        f"dEast={plan.measured_offset_east_deg:+.3f} dNorth={plan.measured_offset_north_deg:+.3f} "
                        f"(simulated mount only) [y/N]: ")
        if answer.strip().lower() != "y":
            print("SYNC not applied (operator declined).")
            return 0

    mount = SimulatedMountAdapter()
    sync_result = await engine.apply_sync(plan, mount, engine.config.global_.settle_seconds, asyncio.sleep)
    _print(sync_result.to_dict(), args.json,
           [f"SYNC {'APPLIED' if sync_result.applied else 'FAILED'} (simulated mount)",
            f"error: {sync_result.error}" if sync_result.error else "no error"])
    return 0 if sync_result.applied else 2


async def cmd_verify(args) -> int:
    from alignment_engine.session import AlignmentSession
    session = AlignmentSession(Path(args.session_root) if args.session_root else "data/alignment", args.session)
    plan_stored = session.read_sync_plan()
    if plan_stored is None:
        print(f"No sync_plan.json for session {args.session} - run `sync {args.session}` first.", file=sys.stderr)
        return 1
    from astropy.coordinates import SkyCoord
    reference = SkyCoord(ra=plan_stored["sync_command_ra_hours"] * u.hourangle,
                          dec=plan_stored["sync_command_dec_deg"] * u.deg)
    mode = "solar" if plan_stored["mode"] == "SOLAR" else "hi"
    engine = _resume_engine(mode, args)
    mount = SimulatedMountAdapter()
    await mount.connect()
    verification = await engine.verify_sync(reference, mount, args.settle, asyncio.sleep)
    _print(verification.to_dict(), args.json,
           [f"Post-sync residual: {verification.residual_deg}", f"error: {verification.error}" if verification.error else "no error"])
    return 0 if verification.residual_deg is not None else 2


async def cmd_tracking(mode: str, args) -> int:
    """Item 10: `solar tracking --dry-run` / `hi tracking --dry-run` -
    CURRENT (real, read-only inspect()) / REQUEST / WOULD WRITE / VERIFY /
    RESTORE PLAN. Uses RealTrackingBackend.inspect() for CURRENT (item 9:
    read-only real hardware queries are authorized) but NEVER calls
    execute_set_tracking_mode() - this command cannot write to the mount
    no matter what flags are passed; --dry-run is required to make that
    explicit rather than implied."""
    if not args.dry_run:
        print("Only --dry-run is supported in this version - real tracking-mode writes "
              "are not authorized. Pass --dry-run to preview.", file=sys.stderr)
        return 3
    from alignment_engine.tracking import RealTrackingBackend, TrackingMode
    config = AlignmentConfig.load(args.config)
    backend = RealTrackingBackend(host=args.host or config.global_.mount_host,
                                   port=args.port or config.global_.mount_port,
                                   device_name=args.device or config.global_.mount_device,
                                   allow_real_writes=False)
    requested = TrackingMode.SOLAR if mode == "solar" else TrackingMode.SIDEREAL
    try:
        current = await backend.get_tracking_mode(timeout=config.global_.tracking_timeout_s)
    except Exception as exc:
        current = None
        current_error = str(exc)
    else:
        current_error = None if current is not None else "property not read within timeout"

    would_write = {element: ("On" if element == backend.ELEMENT_BY_MODE[requested] else "Off")
                   for element in backend.ELEMENT_BY_MODE.values()}
    payload = {
        "device": backend.device_name, "property": backend.PROPERTY_NAME,
        "current": {"mode": current.value if current else None, "reachable": current is not None,
                    "error": current_error},
        "request": {"mode": requested.value},
        "would_write": would_write,
        "verify": {"property": backend.PROPERTY_NAME,
                   "expect": {backend.ELEMENT_BY_MODE[requested]: "On"}},
        "restore_plan": {"original_mode": current.value if current else None,
                         "would_restore_on_exit": current.value if current else "UNKNOWN (backend unreachable)"},
        "executed": False,
        "note": "DRY-RUN ONLY - no property was written to the mount.",
    }
    lines = [
        "CURRENT:", f"  {backend.PROPERTY_NAME} = {backend.ELEMENT_BY_MODE.get(current, 'UNKNOWN') if current else 'UNREACHABLE'}",
        "", "REQUEST:", f"  mode = {requested.value}",
        "", "WOULD WRITE:", f"  device: {backend.device_name}", f"  property: {backend.PROPERTY_NAME}",
    ] + [f"  {element} = {value}" for element, value in would_write.items()] + [
        "", "VERIFY:", f"  read {backend.PROPERTY_NAME}",
        f"  expect {backend.ELEMENT_BY_MODE[requested]} = On",
        "", "RESTORE PLAN:", f"  original = {current.value if current else 'UNKNOWN'}",
        f"  would restore {current.value if current else 'UNKNOWN'} on exit",
        "", "(dry-run only - nothing was written to the mount)",
    ]
    _print(payload, args.json, lines)
    return 0


async def cmd_solar_goto_preflight(args) -> int:
    """Item 12: the safety gate a future real solar GOTO test must pass -
    implemented and testable now, but this command itself never GOTOs,
    SYNCs, or writes anything; it only reads GEOGRAPHIC_COORD (and, if
    present, 'Slew elevation Limit') read-only and reports PASS/BLOCKED.
    No override flag exists here or anywhere in solar_preflight.py."""
    from alignment_engine.solar_preflight import (
        DEFAULT_MAX_ALTITUDE_DEG, DEFAULT_MIN_SOLAR_ALTITUDE_DEG, check_solar_preflight,
    )
    from alignment_engine.tracking import parse_number_vector, read_property_readonly

    config = AlignmentConfig.load(args.config)
    host = args.host or config.global_.mount_host
    port = args.port or config.global_.mount_port
    device = args.device or config.global_.mount_device

    geo_raw = await read_property_readonly(host, port, device, "GEOGRAPHIC_COORD", args.timeout)
    geo = parse_number_vector(geo_raw)
    if not geo or geo.get("LAT") is None or geo.get("LONG") is None:
        payload = {"verdict": "BLOCKED", "reason": "mount not reachable / GEOGRAPHIC_COORD unreadable - "
                                                     "refusing to guess site location"}
        _print(payload, args.json, [f"SOLAR HARDWARE TEST: {payload['verdict']}", f"Reason: {payload['reason']}"])
        return 2

    limit_raw = await read_property_readonly(host, port, device, "Slew elevation Limit", args.timeout)
    limit = parse_number_vector(limit_raw)
    max_altitude_deg = (limit.get("maxAlt") if limit and limit.get("maxAlt") is not None
                         else DEFAULT_MAX_ALTITUDE_DEG)
    min_altitude_deg = args.min_altitude if args.min_altitude is not None else DEFAULT_MIN_SOLAR_ALTITUDE_DEG

    location = EarthLocation(lat=geo["LAT"] * u.deg, lon=geo["LONG"] * u.deg,
                              height=(geo.get("ELEV") or 0.0) * u.m)
    provider = SolarTarget(location)
    result = check_solar_preflight(provider, Time.now(), args.window_seconds,
                                    min_altitude_deg=min_altitude_deg, max_altitude_deg=max_altitude_deg)
    payload = result.to_dict()
    lines = [f"SOLAR HARDWARE TEST: {result.verdict}", f"Reason: {result.reason}"]
    _print(payload, args.json, lines)
    return 0 if result.verdict == "PASS" else 1


def cmd_hi_reference_inspect(args) -> int:
    """Fase 44-45: read-only structural inspection of a local FITS HI
    reference file - dimensions, WCS, units, NaN fraction. Never declares
    anything REAL_VALIDATED (opening successfully is not sufficient - see
    cmd_hi_reference_validate)."""
    from alignment_engine.hi.fits_reference import inspect_fits_moment_map
    try:
        info = inspect_fits_moment_map(args.fits_path)
    except Exception as exc:
        _print({"error": f"{type(exc).__name__}: {exc}"}, args.json,
               [f"Failed to open {args.fits_path}: {exc}"])
        return 1
    lines = [f"shape: {info['shape']}", f"ctype: {info['ctype']}", f"bunit: {info['bunit']}",
             f"wcs_is_celestial: {info['wcs_is_celestial']}", f"nan_fraction: {info['nan_fraction']}",
             f"finite_range: [{info['finite_min']}, {info['finite_max']}]"]
    _print(info, args.json, lines)
    return 0


def cmd_hi_reference_validate(args) -> int:
    """Fase 45: the actual REAL_VALIDATED gate - checksum + structural +
    plausibility checks, never just "did it open"."""
    from alignment_engine.hi.fits_reference import validate_reference
    from alignment_engine.hi.reference_trust import ReferenceManifest
    manifest = ReferenceManifest.read(args.manifest_path)
    result = validate_reference(args.fits_path, manifest)
    lines = [f"VALID: {result.ok}", f"Reason: {result.reason}"]
    _print(result.to_dict(), args.json, lines)
    return 0 if result.ok else 1


def cmd_deployment_show(args) -> int:
    from alignment_engine.deployment_state import read_current_deployment_state
    record = read_current_deployment_state(args.state_path)
    if record is None:
        payload = {"state": None, "detail": "no deployment state recorded (missing or corrupt file) - "
                                             "hardware movement is BLOCKED until an operator confirms FIELD"}
        _print(payload, args.json, ["DEPLOYMENT: UNKNOWN (no record)", payload["detail"]])
        return 0
    payload = record.to_dict()
    lines = [f"DEPLOYMENT: {record.state.value}", f"set: {record.timestamp_utc} by '{record.operator_action}'",
             f"hostname: {record.hostname}"]
    if record.reason:
        lines.append(f"reason: {record.reason}")
    _print(payload, args.json, lines)
    return 0


def cmd_deployment_set(state_name: str, args) -> int:
    from alignment_engine.deployment_state import DeploymentState, write_deployment_state
    if not args.confirm:
        print(f"Refusing to set deployment state to {state_name} without --confirm "
              f"(explicit operator intent required).", file=sys.stderr)
        return 3
    record = write_deployment_state(DeploymentState(state_name), operator_action=f"set-{state_name.lower()}",
                                     reason=args.reason, path=args.state_path)
    lines = [f"DEPLOYMENT SET: {record.state.value}", f"timestamp: {record.timestamp_utc}",
             f"hostname: {record.hostname}"]
    _print(record.to_dict(), args.json, lines)
    return 0


def cmd_hi_replay(args) -> int:
    """Fase 6: re-analyze an existing HI session's raw evidence - never
    opens a mount or SDR connection, never overwrites the source session."""
    from alignment_engine.hi.replay import replay_session
    try:
        result = replay_session(args.session_dir, bootstrap_iterations=args.bootstrap_iterations,
                                 stride=args.pixel_catalog_stride, label=args.label)
    except FileNotFoundError as exc:
        _print({"error": str(exc)}, args.json, [f"Replay failed: {exc}"])
        return 1
    lines = [f"Analysis: {result.analysis_dir}", f"valid points: {result.valid_count}/{result.total_count}"]
    if result.fit:
        lines.append(f"East={result.fit['estimate']['offset_ra_deg']:+.3f} "
                      f"North={result.fit['estimate']['offset_dec_deg']:+.3f}")
    if result.quality:
        lines.append(f"Quality V2: {result.quality['verdict']}")
    _print(result.to_dict(), args.json, lines)
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="alignment JSON config override")
    parser.add_argument("--observer-config", default=None)
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    for mode in ("solar", "hi"):
        mode_parser = sub.add_parser(mode)
        mode_sub = mode_parser.add_subparsers(dest="mode_command", required=True)

        if mode == "hi":
            ref_inspect_p = mode_sub.add_parser("reference-inspect")
            ref_inspect_p.add_argument("fits_path")
            _add_common(ref_inspect_p)
            ref_inspect_p.set_defaults(func=cmd_hi_reference_inspect, is_async=False)

            ref_validate_p = mode_sub.add_parser("reference-validate")
            ref_validate_p.add_argument("fits_path")
            ref_validate_p.add_argument("manifest_path")
            _add_common(ref_validate_p)
            ref_validate_p.set_defaults(func=cmd_hi_reference_validate, is_async=False)

            replay_p = mode_sub.add_parser("replay")
            replay_p.add_argument("session_dir")
            replay_p.add_argument("--bootstrap-iterations", type=int, default=20)
            replay_p.add_argument("--pixel-catalog-stride", type=int, default=None)
            replay_p.add_argument("--label", default=None)
            _add_common(replay_p)
            replay_p.set_defaults(func=cmd_hi_replay, is_async=False)

        plan_p = mode_sub.add_parser("plan")
        _add_common(plan_p)
        plan_p.set_defaults(func=lambda args, mode=mode: cmd_plan(mode, args), is_async=False)

        preflight_p = mode_sub.add_parser("preflight")
        preflight_p.add_argument("session")
        preflight_p.add_argument("--catalog", default="data/hi_sky_catalog_2000pts.csv")
        preflight_p.add_argument("--min-altitude", type=float, default=None)
        _add_common(preflight_p)
        preflight_p.set_defaults(func=lambda args, mode=mode: cmd_preflight(mode, args), is_async=True)

        run_p = mode_sub.add_parser("run")
        run_p.add_argument("session")
        run_p.add_argument("--simulate", action="store_true")
        run_p.add_argument("--dry-run", action="store_true")
        run_p.add_argument("--true-offset-east", type=float, default=0.0)
        run_p.add_argument("--true-offset-north", type=float, default=0.0)
        run_p.add_argument("--fwhm", type=float, default=20.0)
        run_p.add_argument("--gain", type=float, default=1.0)
        run_p.add_argument("--baseline", type=float, default=0.0)
        run_p.add_argument("--noise", type=float, default=0.02)
        run_p.add_argument("--seed", type=int, default=1)
        run_p.add_argument("--catalog", default="data/hi_sky_catalog_2000pts.csv")
        run_p.add_argument("--min-altitude", type=float, default=None)
        _add_common(run_p)
        run_p.set_defaults(func=lambda args, mode=mode: cmd_run(mode, args), is_async=True)

        tracking_p = mode_sub.add_parser("tracking")
        tracking_p.add_argument("--dry-run", action="store_true")
        tracking_p.add_argument("--host", default=None)
        tracking_p.add_argument("--port", type=int, default=None)
        tracking_p.add_argument("--device", default=None)
        _add_common(tracking_p)
        tracking_p.set_defaults(func=lambda args, mode=mode: cmd_tracking(mode, args), is_async=True)

        if mode == "solar":
            goto_preflight_p = mode_sub.add_parser("goto-preflight")
            goto_preflight_p.add_argument("--window-seconds", type=float, default=1800.0,
                                           help="how long the intended test/scan is expected to take")
            goto_preflight_p.add_argument("--min-altitude", type=float, default=None)
            goto_preflight_p.add_argument("--host", default=None)
            goto_preflight_p.add_argument("--port", type=int, default=None)
            goto_preflight_p.add_argument("--device", default=None)
            goto_preflight_p.add_argument("--timeout", type=float, default=5.0)
            _add_common(goto_preflight_p)
            goto_preflight_p.set_defaults(func=cmd_solar_goto_preflight, is_async=True)

    from alignment_engine.deployment_state import DEFAULT_STATE_PATH
    deployment_p = sub.add_parser("deployment")
    deployment_sub = deployment_p.add_subparsers(dest="deployment_command", required=True)

    deploy_show_p = deployment_sub.add_parser("show")
    deploy_show_p.add_argument("--state-path", default=DEFAULT_STATE_PATH)
    _add_common(deploy_show_p)
    deploy_show_p.set_defaults(func=cmd_deployment_show, is_async=False)

    for state_name in ("field", "indoor", "bench", "unknown"):
        set_p = deployment_sub.add_parser(f"set-{state_name}")
        set_p.add_argument("--confirm", action="store_true", help="required - explicit operator intent")
        set_p.add_argument("--reason", default=None)
        set_p.add_argument("--state-path", default=DEFAULT_STATE_PATH)
        _add_common(set_p)
        set_p.set_defaults(func=lambda args, s=state_name.upper(): cmd_deployment_set(s, args), is_async=False)

    status_p = sub.add_parser("status")
    status_p.add_argument("session")
    status_p.add_argument("--session-root", default=None)
    _add_common(status_p)
    status_p.set_defaults(func=cmd_status, is_async=False)

    result_p = sub.add_parser("result")
    result_p.add_argument("session")
    result_p.add_argument("--session-root", default=None)
    _add_common(result_p)
    result_p.set_defaults(func=cmd_result, is_async=False)

    sync_p = sub.add_parser("sync")
    sync_p.add_argument("session")
    sync_p.add_argument("--session-root", default=None)
    sync_p.add_argument("--apply", action="store_true")
    sync_p.add_argument("--yes", action="store_true", help="skip the interactive confirmation (still requires --apply)")
    sync_p.add_argument("--confidence-threshold", type=float, default=0.65)
    sync_p.add_argument("--catalog", default="data/hi_sky_catalog_2000pts.csv")
    _add_common(sync_p)
    sync_p.set_defaults(func=cmd_sync, is_async=True)

    verify_p = sub.add_parser("verify")
    verify_p.add_argument("session")
    verify_p.add_argument("--session-root", default=None)
    verify_p.add_argument("--settle", type=float, default=0.0)
    _add_common(verify_p)
    verify_p.set_defaults(func=cmd_verify, is_async=True)

    return parser


def main(argv: Optional[list] = None) -> int:
    """The ONE asyncio.run() boundary in this entire codebase (item 6) -
    only for subcommands whose handler is `async def` (is_async=True);
    plan/status/result run with no event loop at all, since they never
    touch a tracking backend or a mount."""
    args = build_parser().parse_args(argv)
    if getattr(args, "is_async", False):
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
