#!/usr/bin/env python3
"""ALMITA Alignment CLI - thin wrapper over alignment_engine (Fase 14).

Every subcommand does argument parsing and printing only; every decision
(state transitions, fitting, sync eligibility) happens inside
alignment_engine, importable and callable the exact same way a future
:8090 API would call it - nothing here is science logic.

--json switches every subcommand's stdout to a single JSON document (never
mixed with human-readable text) so this CLI is scriptable/automatable
today, ahead of any real API.

Hardware note: this version only ever constructs SimulatedMountAdapter/
SimulatedTrackingBackend - there is no --mount real flag. Real GOTO/
tracking-mode/SYNC against the physical mount is deliberately not wired in
this version (see the design report this CLI's implementation followed);
`sync --apply` and `verify` still exercise the full engine logic, but
against a simulated mount, for development and demonstration.
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


def _engine_for(mode: str, args, resume: bool) -> AlignmentEngine:
    config = AlignmentConfig.load(args.config)
    if args.observer_config:
        config.global_.observer_config_path = args.observer_config
    if getattr(args, "min_altitude", None) is not None:
        (config.solar if mode == "solar" else config.hi).min_altitude_deg = args.min_altitude
    location = _location_from_config(config)
    from alignment_engine.session import AlignmentSession
    session_id = getattr(args, "session", None)
    session = AlignmentSession(config.global_.output_root, session_id) if session_id else None
    return AlignmentEngine(mode, config, location, SimulatedMountAdapter(), SimulatedTrackingBackend(),
                            session=session, resume=resume)


def cmd_plan(mode: str, args) -> int:
    engine = _engine_for(mode, args, resume=False)
    engine.plan()
    _print({"session_id": engine.session.session_id, "state": engine.state_machine.state.value}, args.json,
           [f"Session planned: {engine.session.session_id}",
            f"State: {engine.state_machine.state.value}"])
    return 0


def cmd_preflight(mode: str, args) -> int:
    engine = _engine_for(mode, args, resume=True)
    provider = SolarTarget(engine.location) if mode == "solar" else SyntheticHIReferenceProvider(args.catalog)
    checks = engine.preflight(provider)
    ok = all(c.ok for c in checks)
    _print({"session_id": engine.session.session_id, "ok": ok,
            "checks": [vars(c) for c in checks]}, args.json,
           [f"Preflight for {engine.session.session_id}: {'PASS' if ok else 'FAIL'}"] +
           [f"  [{'OK' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in checks])
    return 0 if ok else 2


def cmd_run(mode: str, args) -> int:
    if not args.simulate:
        print("Only --simulate is supported in this version - real hardware acquisition "
              "is not wired pending the second, hardware-facing authorization.", file=sys.stderr)
        return 3
    engine = _engine_for(mode, args, resume=True)
    if mode == "solar":
        sim = SolarBeamSimConfig(true_offset_east_deg=args.true_offset_east, true_offset_north_deg=args.true_offset_north,
                                  fwhm_deg=args.fwhm, noise_fraction=args.noise, seed=args.seed)
        result = engine.run_solar_simulated(sim)
    else:
        provider = SyntheticHIReferenceProvider(args.catalog)
        sim = HIMapSimConfig(true_offset_east_deg=args.true_offset_east, true_offset_north_deg=args.true_offset_north,
                              gain_a=args.gain, baseline_b=args.baseline, noise_fraction=args.noise, seed=args.seed)
        result = engine.run_hi_simulated(provider, sim)
    payload = result.to_dict()
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


def cmd_sync(args) -> int:
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
        center = SolarTarget(engine.location).current_position(Time.now()).icrs
    else:
        provider = SyntheticHIReferenceProvider(args.catalog)
        center, _ = provider.choose_target(engine.location, Time.now(), engine.config.hi.min_altitude_deg,
                                            engine.config.resolved_beam_fwhm_deg("hi"))
        is_observational = provider.is_observational

    plan = engine.prepare_sync(result, center, is_observational, args.confidence_threshold)

    if not args.apply:
        _print(plan.to_dict(), args.json,
               ["Alignment completed.", f"Measured offset: dEast={plan.measured_offset_east_deg:+.3f} deg "
                f"dNorth={plan.measured_offset_north_deg:+.3f} deg", f"Quality: {plan.fit_rating} "
                f"(confidence={plan.fit_confidence:.3f})", f"Eligible for SYNC: {plan.eligible} ({plan.eligibility_reason})",
                "Run again with --apply to send SYNC (simulated mount only in this version)."])
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
    sync_result = asyncio.run(engine.apply_sync(plan, mount, engine.config.global_.settle_seconds, asyncio.sleep))
    _print(sync_result.to_dict(), args.json,
           [f"SYNC {'APPLIED' if sync_result.applied else 'FAILED'} (simulated mount)",
            f"error: {sync_result.error}" if sync_result.error else "no error"])
    return 0 if sync_result.applied else 2


def cmd_verify(args) -> int:
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
    asyncio.run(mount.connect())
    verification = asyncio.run(engine.verify_sync(reference, mount, args.settle, asyncio.sleep))
    _print(verification.to_dict(), args.json,
           [f"Post-sync residual: {verification.residual_deg}", f"error: {verification.error}" if verification.error else "no error"])
    return 0 if verification.residual_deg is not None else 2


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

        plan_p = mode_sub.add_parser("plan")
        _add_common(plan_p)
        plan_p.set_defaults(func=lambda args, mode=mode: cmd_plan(mode, args))

        preflight_p = mode_sub.add_parser("preflight")
        preflight_p.add_argument("session")
        preflight_p.add_argument("--catalog", default="data/hi_sky_catalog_2000pts.csv")
        preflight_p.add_argument("--min-altitude", type=float, default=None)
        _add_common(preflight_p)
        preflight_p.set_defaults(func=lambda args, mode=mode: cmd_preflight(mode, args))

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
        run_p.set_defaults(func=lambda args, mode=mode: cmd_run(mode, args))

    status_p = sub.add_parser("status")
    status_p.add_argument("session")
    status_p.add_argument("--session-root", default=None)
    _add_common(status_p)
    status_p.set_defaults(func=cmd_status)

    result_p = sub.add_parser("result")
    result_p.add_argument("session")
    result_p.add_argument("--session-root", default=None)
    _add_common(result_p)
    result_p.set_defaults(func=cmd_result)

    sync_p = sub.add_parser("sync")
    sync_p.add_argument("session")
    sync_p.add_argument("--session-root", default=None)
    sync_p.add_argument("--apply", action="store_true")
    sync_p.add_argument("--yes", action="store_true", help="skip the interactive confirmation (still requires --apply)")
    sync_p.add_argument("--confidence-threshold", type=float, default=0.65)
    sync_p.add_argument("--catalog", default="data/hi_sky_catalog_2000pts.csv")
    _add_common(sync_p)
    sync_p.set_defaults(func=cmd_sync)

    verify_p = sub.add_parser("verify")
    verify_p.add_argument("session")
    verify_p.add_argument("--session-root", default=None)
    verify_p.add_argument("--settle", type=float, default=0.0)
    _add_common(verify_p)
    verify_p.set_defaults(func=cmd_verify)

    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
