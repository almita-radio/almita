#!/usr/bin/env python3
"""ALMITA REDUCE CLI - LEVEL 0 (RAW) -> LEVEL 1 (REDUCED SPECTRA) only.

See docs/REDUCE_SCOPE.md before reading any output of this tool. REDUCE
never produces spatial heatmaps, RA/DEC/velocity cubes, or astrophysical
interpretation - that is SCIENCE's job (not implemented yet). Every
calibration_level in this tool's output is RELATIVE or UNCALIBRATED,
never Kelvin/dBm/Tsys/NF/Jy/SEFD/Tant.

100% offline: no hardware, no INDI, no rtl_tcp, no mount, no network -
`inspect`, `plan`, `preflight`, `run`, `replay`, and `compare` are all
filesystem-only operations over already-captured RAW evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _print(payload: dict, as_json: bool, human_lines) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in human_lines:
            print(line)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def cmd_inspect(args) -> int:
    from reduce_engine.ingest import discover_campaign
    manifest = discover_campaign(args.campaign_dir)
    accepted = manifest.accepted_points()
    payload = {
        "campaign_id": manifest.campaign_id, "session_id": manifest.session_id,
        "root": str(manifest.root), "grid": manifest.grid,
        "observer": manifest.observer.get("observer", {}),
        "points_discovered": len(manifest.points), "points_accepted": len(accepted),
        "points_rejected": [{"point_index": p.point_index, "reason": p.reject_reason}
                            for p in manifest.points if not p.accepted],
    }
    _print(payload, args.json, [
        f"campaign: {payload['campaign_id']}  root: {payload['root']}",
        f"points: {payload['points_discovered']} discovered, {payload['points_accepted']} accepted",
        *([f"  rejected point {r['point_index']}: {r['reason']}" for r in payload["points_rejected"]]),
    ])
    return 0


def cmd_plan(args) -> int:
    from reduce_engine.config import ReduceConfig
    from reduce_engine.ingest import discover_campaign
    from reduce_engine.validation import estimate_output_bytes, run_preflight

    manifest = discover_campaign(args.campaign_dir)
    config = ReduceConfig(velocity_frame=args.velocity_frame, calibration_profile_path=args.calibration_profile)
    checks = run_preflight(manifest, config, output_root=args.output_root,
                           calibration_profile_path=args.calibration_profile)
    estimated_bytes = estimate_output_bytes(config, len(manifest.accepted_points()))
    payload = {
        "campaign_id": manifest.campaign_id, "config": config.to_dict(), "config_hash": config.config_hash(),
        "checks": [c.to_dict() for c in checks], "blocked": any(not c.ok for c in checks),
        "estimated_output_bytes": estimated_bytes, "estimated_output_mb": round(estimated_bytes / 1e6, 1),
    }
    _print(payload, args.json, [
        f"campaign: {payload['campaign_id']}  config_hash: {payload['config_hash'][:12]}",
        f"estimated output size: ~{payload['estimated_output_mb']} MB "
        f"({len(manifest.accepted_points())} accepted points)",
        *[f"[{'PASS' if c.ok else 'BLOCKED'}] {c.name}: {c.detail}" for c in checks],
    ])
    return 1 if payload["blocked"] else 0


def cmd_run(args) -> int:
    from reduce_engine.config import ReduceConfig
    from reduce_engine.ingest import discover_campaign
    from reduce_engine.pipeline import reduce_campaign
    from reduce_engine.validation import blocking_reason, run_preflight

    manifest = discover_campaign(args.campaign_dir)
    config = ReduceConfig(velocity_frame=args.velocity_frame, calibration_profile_path=args.calibration_profile)
    checks = run_preflight(manifest, config, output_root=args.output_root,
                           calibration_profile_path=args.calibration_profile)
    reason = blocking_reason(checks)
    if reason:
        _print({"blocked": True, "reason": reason}, args.json, [f"BLOCKED: {reason}"])
        return 1

    report = reduce_campaign(manifest, config, output_root=args.output_root,
                             calibration_profile_path=args.calibration_profile)
    payload = report.__dict__
    q = report.quality_counts
    _print(payload, args.json, [
        f"REDUCE {report.status}",
        f"Campaign:        {report.campaign_id}",
        f"Points:          {report.points_discovered}",
        f"Accepted:        {report.points_accepted}",
        f"Rejected:        {report.points_rejected}",
        f"Calibration:     {report.calibration_level_counts}",
        f"Velocity:        {report.velocity_frame_counts}",
        f"Median usable:   {_fmt_pct(report.median_usable_fraction)}",
        f"Median base RMS: {_fmt_pct(report.median_baseline_rms_fraction)}",
        f"GOOD:            {q.get('GOOD', 0)}",
        f"WARNING:         {q.get('WARNING', 0)}",
        f"BAD:             {q.get('BAD', 0)}",
        f"UNKNOWN:         {q.get('UNKNOWN', 0)}",
        f"Runtime:         {report.runtime_seconds:.2f}s",
        f"Output:          {report.output_dir}",
    ])
    return 0 if report.status in ("COMPLETED", "PARTIAL") else 1


def _fmt_pct(value) -> str:
    return "—" if value is None else f"{value:.3f}"


def cmd_replay(args) -> int:
    from reduce_engine.replay import replay_session
    report = replay_session(args.session_dir, output_root=args.output_root,
                            calibration_profile_path=args.calibration_profile)
    _print(report.__dict__, args.json, [f"REPLAY {report.status}: {report.session_id} -> {report.output_dir}"])
    return 0


def cmd_compare(args) -> int:
    from reduce_engine.compare import compare_sessions
    result = compare_sessions(args.session_dir_a, args.session_dir_b)
    _print(result, args.json, [f"{k}: {v}" for k, v in result.items() if k != "per_point_rms_difference"])
    return 0


def cmd_validate(args) -> int:
    from reduce_engine.science_contract import validate_science_input
    from reduce_engine.storage import validate_session
    integrity = validate_session(args.session_dir)
    contract = validate_science_input(args.session_dir)
    payload = {"output_integrity": integrity, "science_contract": contract.to_dict()}
    ok = integrity["ok"] and contract.ok
    _print(payload, args.json, [
        f"output_integrity: {'OK' if integrity['ok'] else 'PROBLEMS'}",
        *[f"  - {p}" for p in integrity["problems"]],
        f"science_contract: {'OK' if contract.ok else 'PROBLEMS'} ({contract.points_checked} points checked)",
        *[f"  - {p}" for p in contract.problems],
    ])
    return 0 if ok else 1


def cmd_status(args) -> int:
    from reduce_engine.config import PIPELINE_VERSION
    from reduce_engine.models import REDUCE_SCHEMA_VERSION
    payload = {"pipeline_version": PIPELINE_VERSION, "reduce_schema_version": REDUCE_SCHEMA_VERSION,
              "level": "LEVEL_0_RAW -> LEVEL_1_REDUCED_SPECTRA", "hardware": "NONE - offline only"}
    _print(payload, args.json, [f"{k}: {v}" for k, v in payload.items()])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="discover campaign/point/capture structure, write nothing")
    p.add_argument("campaign_dir")
    _add_common(p)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("plan", help="preflight checks, write nothing")
    p.add_argument("campaign_dir")
    p.add_argument("--output-root", default="data/reduced")
    p.add_argument("--velocity-frame", default="lsrk")
    p.add_argument("--calibration-profile", default=None)
    _add_common(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="run the full REDUCE pipeline for a campaign")
    p.add_argument("campaign_dir")
    p.add_argument("--output-root", default="data/reduced")
    p.add_argument("--velocity-frame", default="lsrk")
    p.add_argument("--calibration-profile", default=None)
    _add_common(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("replay", help="reprocess a previous session's raw source into a new session")
    p.add_argument("session_dir")
    p.add_argument("--output-root", default="data/reduced")
    p.add_argument("--calibration-profile", default=None)
    _add_common(p)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("compare", help="regression-compare two REDUCE sessions")
    p.add_argument("session_dir_a")
    p.add_argument("session_dir_b")
    _add_common(p)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("validate", help="output integrity + SCIENCE-contract check for a finished session")
    p.add_argument("session_dir")
    _add_common(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("status", help="pipeline/schema version, no campaign required")
    _add_common(p)
    p.set_defaults(func=cmd_status)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
