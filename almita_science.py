#!/usr/bin/env python3
"""ALMITA SCIENCE CLI - LEVEL 1 (REDUCED SPECTRA) -> LEVEL 2 (SCIENCE
PRODUCTS) only.

See docs/SCIENCE_SCOPE.md. SCIENCE consumes only a REDUCE session
directory - never data/mosaic, never RAW, never hardware. Every
relative_intensity value is dimensionless (fractional excess), never
physical units - see docs/SCIENCE_MODEL.md's units contract.

100% offline: `inspect`, `plan`, `run`, `replay`, `compare`, `status`, `validate` are all
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


class BeamNotSpecifiedError(Exception):
    pass


def _config_from_args(args) -> "ScienceConfig":
    """Beam is NEVER defaulted silently. Either --beam-fwhm-deg (source recorded as operator_config) or
    --beam-from-observer-config [PATH] (path, field and sha256 of that file recorded). SCIENCE never searches
    for a beam (in particular never in data/mosaic); the value is operational metadata supplied by the operator."""
    from science_engine.beam import beam_settings_from_observer_config
    from science_engine.config import ScienceConfig
    kwargs: dict[str, Any] = {}
    if args.beam_fwhm_deg is not None and args.beam_from_observer_config is not None:
        raise BeamNotSpecifiedError("give either --beam-fwhm-deg or --beam-from-observer-config, not both")
    if args.beam_fwhm_deg is not None:
        kwargs.update(beam_fwhm_deg=args.beam_fwhm_deg, beam_source="operator_config",
                      beam_status="CONFIGURED_OPERATIONAL")
    elif args.beam_from_observer_config is not None:
        kwargs.update(beam_settings_from_observer_config(args.beam_from_observer_config))
    else:
        raise BeamNotSpecifiedError(
            "no beam specified. SCIENCE has no silent default beam: pass --beam-fwhm-deg DEG (operator-provided "
            "value) or --beam-from-observer-config [PATH] (reads observation_defaults.beam_fwhm_deg and records "
            "the file's path and sha256). The beam is operational metadata supplied by the operator.")
    for cli_name, cfg_name in (("velocity_window_min_m_s", "velocity_window_min_m_s"),
                               ("velocity_window_max_m_s", "velocity_window_max_m_s"),
                               ("quality_policy", "quality_policy"), ("pixel_scale_deg", "pixel_scale_deg"),
                               ("pixels_per_beam", "pixels_per_beam"), ("beam_cutoff_n_fwhm", "beam_cutoff_n_fwhm"),
                               ("min_spectral_coverage_fraction", "min_spectral_coverage_fraction")):
        value = getattr(args, cli_name, None)
        if value is not None:
            kwargs[cfg_name] = value
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
    from science_engine.validation import available_memory_bytes, estimate_output_bytes, estimate_peak_memory_bytes
    payload = {"config": config.to_dict(), "beam": beam.to_dict(), "grid": grid.to_dict(),
              "n_velocity_channels": int(velocity_axis.shape[0]), "checks": [c.to_dict() for c in checks],
              "estimated_peak_ram_bytes": estimate_peak_memory_bytes(grid, int(velocity_axis.shape[0]), len(science_input.points)),
              "estimated_output_bytes": estimate_output_bytes(grid, int(velocity_axis.shape[0])),
              "mem_available_bytes": available_memory_bytes(), "blocked": reason is not None}
    _print(payload, args.json, [
        f"grid: {grid.ny}x{grid.nx} pixels ({grid.pixel_scale_deg:.4g} deg/pixel), {velocity_axis.shape[0]} velocity channels",
        f"estimated peak RAM ~{payload['estimated_peak_ram_bytes'] / 1024**2:.0f} MB, "
        f"output ~{payload['estimated_output_bytes'] / 1024**2:.0f} MB",
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


def cmd_replay(args) -> int:
    """Re-run a previous SCIENCE session's exact inputs: same REDUCE session + same ScienceConfig -> a NEW session
    (the original is never touched). Refuses if the REDUCE manifest changed since (unless --allow-changed-input)."""
    import json
    from pathlib import Path
    from science_engine.compare import compare_sessions
    from science_engine.config import ScienceConfig
    from science_engine.provenance import reduce_manifest_hash
    from science_engine.products import run_science_session
    old = Path(args.science_session_dir).resolve()
    manifest = json.loads((old / "manifest.json").read_text())
    config = ScienceConfig(**json.loads((old / "config.json").read_text()))
    reduce_dir = manifest["input_reduce_session_dir"]
    now_hash = reduce_manifest_hash(reduce_dir)
    if now_hash != manifest.get("input_reduce_manifest_sha256") and not args.allow_changed_input:
        print(f"ERROR: REDUCE manifest at {reduce_dir} changed since the original run "
              f"({manifest.get('input_reduce_manifest_sha256')} -> {now_hash}); refusing a replay that is not the same "
              f"input (use --allow-changed-input to override)", file=sys.stderr)
        return 1
    output_root = args.output_root or str(old.parent.parent)
    report = run_science_session(reduce_dir, config, output_root=output_root)
    result = compare_sessions(old, report.output_dir)
    payload = {"replay_session_dir": report.output_dir, "original_session_dir": str(old), "compare": result}
    _print(payload, args.json, [*report.human_summary_lines(), "", *_compare_lines(result)])
    return 0 if result["verdict"] == "EQUIVALENT" else 1


def _compare_lines(result: dict) -> list[str]:
    lines = [f"COMPARE verdict: {result['verdict']}",
             f"  same input: {result['same_input']}   same config: {result['same_config']}   "
             f"quality: {result['quality_a']} vs {result['quality_b']}"]
    for label, key in (("config", "config_diff"), ("beam", "beam_diff"), ("grid", "grid_diff")):
        for k, (va, vb) in result[key].items():
            lines.append(f"  {label} differs: {k}: {va!r} -> {vb!r}")
    if result["velocity_window_diff"]:
        lines.append(f"  velocity window differs: {result['velocity_window_diff']}")
    lines.append(f"  cube shape: {result['cube_shape'][0]} vs {result['cube_shape'][1]}")
    for pid, item in result["products"].items():
        lines.append(f"  [{item['level']}] {pid}")
    return lines


def cmd_compare(args) -> int:
    from science_engine.compare import compare_sessions
    result = compare_sessions(args.session_a, args.session_b)
    _print(result, args.json, _compare_lines(result))
    return 0 if result["verdict"] == "EQUIVALENT" else 1


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
        p.add_argument("--beam-fwhm-deg", dest="beam_fwhm_deg", type=float, default=None,
                      help="operator-provided beam FWHM in degrees (operational metadata)")
        p.add_argument("--beam-from-observer-config", dest="beam_from_observer_config", nargs="?",
                      const="observer_config.json", default=None, metavar="PATH",
                      help="read observation_defaults.beam_fwhm_deg from this file (default ./observer_config.json); "
                           "path/field/sha256 are recorded")
        p.add_argument("--pixel-scale-deg", dest="pixel_scale_deg", type=float, default=None)
        p.add_argument("--pixels-per-beam", dest="pixels_per_beam", type=float, default=None)
        p.add_argument("--beam-cutoff-n-fwhm", dest="beam_cutoff_n_fwhm", type=float, default=None)
        p.add_argument("--min-spectral-coverage-fraction", dest="min_spectral_coverage_fraction", type=float,
                      default=None)
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

    p = sub.add_parser("replay", help="re-run a previous session's exact REDUCE input + config into a NEW session, "
                                     "then compare (the original is never modified)")
    p.add_argument("science_session_dir")
    p.add_argument("--output-root", default=None, help="default: the original session's output root")
    p.add_argument("--allow-changed-input", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("compare", help="read-only: identity/config/beam/grid/window/shape + per-artifact numeric "
                                     "equivalence of two SCIENCE sessions")
    p.add_argument("session_a")
    p.add_argument("session_b")
    _add_common(p)
    p.set_defaults(func=cmd_compare)

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
    except KeyboardInterrupt:
        print("CANCELLED: interrupted (any started session is marked CANCELLED, never COMPLETED)", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
