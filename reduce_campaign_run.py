#!/usr/bin/env python3
"""ALMITA REDUCE - campaign RUN bridge, with a verifiable RUN-time calibration-compatibility record.

almita_reduce.py (frozen) has no hook point for capturing what calibration_foundation.
check_calibration_compatibility() found for every point right before reduce_engine.pipeline.
reduce_campaign() processes them - its own `run` command only prints the final CampaignReduceReport.
This script calls the EXACT SAME frozen functions almita_reduce.py's own cmd_run does
(reduce_engine.ingest.discover_campaign, reduce_engine.validation.run_preflight/blocking_reason,
reduce_engine.pipeline.reduce_campaign) - never a second reduction algorithm - and additionally
persists reduce_calibration_record.compute_record()'s result as calibration_compatibility_record.json
inside the real output_dir reduce_campaign() itself creates. almita_reduce.py itself is untouched;
PLAN still uses it directly (`almita_reduce.py plan`) - only RUN goes through this bridge, because
only RUN needs the pre-execution capture.

100% offline: filesystem-only, no hardware, no INDI, no rtl_tcp, no mount, no network, never touches
the original HDF5s or almita_reduce.py/reduce_engine.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict


def _print(payload: Dict[str, Any], as_json: bool, human_lines) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in human_lines:
            print(line)


def cmd_run(args) -> int:
    from reduce_engine.config import ReduceConfig
    from reduce_engine.ingest import discover_campaign
    from reduce_engine.pipeline import reduce_campaign
    from reduce_engine.validation import blocking_reason, run_preflight
    import reduce_calibration_record as calrecord

    manifest = discover_campaign(args.campaign_dir)
    config = ReduceConfig(velocity_frame=args.velocity_frame, calibration_profile_path=args.calibration_profile)
    checks = run_preflight(manifest, config, output_root=args.output_root,
                           calibration_profile_path=args.calibration_profile)
    reason = blocking_reason(checks)
    if reason:
        _print({"blocked": True, "reason": reason}, args.json, [f"BLOCKED: {reason}"])
        return 1

    # Captured HERE - immediately before the real frozen reduce_campaign() call, in this same process -
    # not from an earlier PLAN/preview, and not re-derived afterwards from whatever the files contain later.
    record = calrecord.compute_record(
        args.calibration_profile,
        [(pt.point_index, pt.resolved_path) for pt in manifest.accepted_points()],
    )

    report = reduce_campaign(manifest, config, output_root=args.output_root,
                             calibration_profile_path=args.calibration_profile)
    record_path = calrecord.write_record(report.output_dir, record)

    payload = dict(report.__dict__)
    payload["calibration_compatibility_record_path"] = record_path
    _print(payload, args.json, [
        f"REDUCE {report.status}",
        f"Campaign:        {report.campaign_id}",
        f"Points:          {report.points_discovered}",
        f"Accepted:        {report.points_accepted}",
        f"Rejected:        {report.points_rejected}",
        f"Calibration:     {report.calibration_level_counts}",
        f"Velocity:        {report.velocity_frame_counts}",
        f"Runtime:         {report.runtime_seconds:.2f}s",
        f"Output:          {report.output_dir}",
        *([f"Calibration compatibility record: {record_path}"] if record_path else []),
    ])
    return 0 if report.status in ("COMPLETED", "PARTIAL") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="run REDUCE on a campaign, recording a verifiable per-point calibration-compatibility result")
    p.add_argument("campaign_dir")
    p.add_argument("--output-root", default="data/reduced")
    p.add_argument("--velocity-frame", default="lsrk")
    p.add_argument("--calibration-profile", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_run)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
