#!/usr/bin/env python3
"""ALMITA SCIENCE CLI - LEVEL 1 (REDUCED SPECTRA) -> LEVEL 2 (SCIENCE
PRODUCTS) only.

See docs/SCIENCE_SCOPE.md. SCIENCE consumes only a REDUCE session
directory - never data/mosaic, never RAW, never hardware. Every
relative_intensity value is dimensionless (fractional excess), never
Kelvin/Jy/dBm - see docs/SCIENCE_MODEL.md's units contract.

100% offline: `inspect`, `plan`, `run`, `status`, `validate` are all
filesystem-only operations over an already-produced REDUCE session.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _print(payload: dict, as_json: bool, human_lines) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in human_lines:
            print(line)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def _config_from_args(args) -> "ScienceConfig":
    from science_engine.config import ScienceConfig
    kwargs: dict[str, Any] = {}
    if args.beam_fwhm_deg is not None:
        kwargs["beam_fwhm_deg"] = args.beam_fwhm_deg
        kwargs["beam_source"] = "operator (--beam-fwhm-deg)"
        kwargs["beam_status"] = "CONFIGURED_OPERATIONAL"
    if args.velocity_window_min_m_s is not None:
        kwargs["velocity_window_min_m_s"] = args.velocity_window_min_m_s
    if args.velocity_window_max_m_s is not None:
        kwargs["velocity_window_max_m_s"] = args.velocity_window_max_m_s
    if args.quality_policy is not None:
        kwargs["quality_policy"] = args.quality_policy
    return ScienceConfig(**kwargs)


def cmd_inspect(args) -> int:
    from reduce_engine.science_contract import validate_science_input
    contract = validate_science_input(args.reduce_session_dir)
    payload = {"reduce_session_dir": args.reduce_session_dir, "science_contract_ok": contract.ok,
              "problems": contract.problems, "points_checked": contract.points_checked}
    if contract.ok:
        from science_engine.ingest import load_science_input
        science_input = load_science_input(args.reduce_session_dir)
        ra = [p.ra_deg for p in science_input.points]
        dec = [p.dec_degrees for p in science_input.points]
        has_velocity = all(p.velocity_lsrk_m_s is not None for p in science_input.points)
        payload.update({
            "campaign_id": science_input.campaign_id, "reduce_session_id": science_input.reduce_session_id,
            "n_points": len(science_input.points), "ra_deg_range": [min(ra), max(ra)] if ra else None,
            "dec_deg_range": [min(dec), max(dec)] if dec else None, "velocity_available": has_velocity,
            "calibration_levels": sorted({p.calibration_level for p in science_input.points}),
        })
    _print(payload, args.json, [
        f"science_contract: {'OK' if contract.ok else 'PROBLEMS'}",
        *[f"  - {p}" for p in contract.problems],
        *([f"campaign: {payload.get('campaign_id')}  points: {payload.get('n_points')}",
          f"RA range (deg): {payload.get('ra_deg_range')}  DEC range (deg): {payload.get('dec_deg_range')}",
          f"velocity available: {payload.get('velocity_available')}",
          f"calibration levels: {payload.get('calibration_levels')}"] if contract.ok else []),
    ])
    return 0 if contract.ok else 1


def cmd_plan(args) -> int:
    from science_engine.ingest import load_science_input
    from science_engine.grid import build_beam_model, build_grid
    from science_engine.cube import canonical_velocity_axis
    from science_engine.validation import blocking_reason, run_preflight

    config = _config_from_args(args)
    science_input = load_science_input(args.reduce_session_dir)
    beam = build_beam_model(config)
    grid = build_grid(science_input, beam, config)
    velocity_axis = canonical_velocity_axis(science_input)
    checks = run_preflight(science_input, grid, velocity_axis.shape[0], config, output_root=args.output_root)
    reason = blocking_reason(checks)
    payload = {"config": config.to_dict(), "beam": beam.to_dict(), "grid": grid.to_dict(),
              "n_velocity_channels": int(velocity_axis.shape[0]), "checks": [c.to_dict() for c in checks],
              "blocked": reason is not None}
    _print(payload, args.json, [
        f"grid: {grid.ny}x{grid.nx} pixels, {velocity_axis.shape[0]} velocity channels",
        f"beam: fwhm={beam.fwhm_deg} deg, status={beam.status}, source={beam.source}",
        *[f"  [{'OK' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in checks],
        f"blocked: {reason}" if reason else "not blocked",
    ])
    return 0 if reason is None else 1


def cmd_run(args) -> int:
    from science_engine.products import run_science_session
    config = _config_from_args(args)
    report = run_science_session(args.reduce_session_dir, config, output_root=args.output_root)
    _print(report.__dict__, args.json, report.human_summary_lines())
    return 0 if report.status == "COMPLETED" else 1


def cmd_validate(args) -> int:
    from science_engine.storage import validate_science_session
    result = validate_science_session(args.science_session_dir)
    _print(result, args.json, [
        f"output_integrity: {'OK' if result['ok'] else 'PROBLEMS'}",
        *[f"  - {p}" for p in result["problems"]],
    ])
    return 0 if result["ok"] else 1


def cmd_status(args) -> int:
    from science_engine.config import SCIENCE_PIPELINE_VERSION
    from science_engine.models import SCIENCE_SCHEMA_VERSION
    payload = {"science_pipeline_version": SCIENCE_PIPELINE_VERSION, "science_schema_version": SCIENCE_SCHEMA_VERSION}
    _print(payload, args.json, [f"{k}: {v}" for k, v in payload.items()])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="almita_science.py")
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_science_config_args(p):
        p.add_argument("--beam-fwhm-deg", dest="beam_fwhm_deg", type=float, default=None)
        p.add_argument("--velocity-window-min-m-s", dest="velocity_window_min_m_s", type=float, default=None)
        p.add_argument("--velocity-window-max-m-s", dest="velocity_window_max_m_s", type=float, default=None)
        p.add_argument("--quality-policy", dest="quality_policy", choices=("STRICT", "STANDARD", "PERMISSIVE"),
                      default=None)

    p = sub.add_parser("inspect", help="read-only: contract check + campaign/points/coordinates/velocity summary")
    p.add_argument("reduce_session_dir")
    _add_common(p)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("plan", help="no writes: grid/beam/velocity-channel shape, memory/disk estimate, blocking checks")
    p.add_argument("reduce_session_dir")
    p.add_argument("--output-root", default="data/science")
    _add_science_config_args(p)
    _add_common(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="build and persist a SCIENCE session")
    p.add_argument("reduce_session_dir")
    p.add_argument("--output-root", default="data/science")
    _add_science_config_args(p)
    _add_common(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("validate", help="output integrity check for a finished SCIENCE session")
    p.add_argument("science_session_dir")
    _add_common(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("status", help="pipeline/schema version, no session required")
    _add_common(p)
    p.set_defaults(func=cmd_status)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
