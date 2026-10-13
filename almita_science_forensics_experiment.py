#!/usr/bin/env python3
"""ALMITA SCIENCE FORENSICS EXPERIMENT tool (additive; does not touch REDUCE/SCIENCE/forensics V1, never touches hardware).

  design         build the plan (examples/science_forensics_experiment.yaml) and the capture.py-compatible CSVs
  windows        UTC start windows where A, B, C stay on one hour-angle side above the minimum altitude for the whole run
  simulate       synthetic power test (confusion matrix over scenarios x designs)
  replay         new diagnostics on an EXISTING REDUCE session (read-only), e.g. the old 9-point campaign
  analyze        analyse a REDUCE session of an experiment run (labels from the plan CSV)
  validate-csv   check a plan CSV against the rules capture.py's preflight and REDUCE identity apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--observer-config", default="observer_config.json")
    common.add_argument("--epoch", default="2026-09-21T14:45:00+00:00", help="design epoch (UTC ISO) used for the predicted LSRK shifts")
    common.add_argument("--json", action="store_true")
    p = sub.add_parser("design", parents=[common])
    p.add_argument("--out", default="examples/science_forensics_experiment.yaml")
    p.add_argument("--emit-dir", default="examples/science_forensics_experiment")
    p.add_argument("--copy-observer-config", action="store_true", help="copy observer_config.json next to each CSV (capture.py requires it)")
    p = sub.add_parser("windows", parents=[common])
    p.add_argument("--date", required=True, help="first UTC day, YYYY-MM-DD")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--minutes", type=float, default=65.0, help="session length to keep inside the window")
    p.add_argument("--min-alt", type=float, default=30.0)
    p.add_argument("--ha-margin", type=float, default=0.25)
    p.add_argument("--step-min", type=int, default=15)
    p = sub.add_parser("simulate", parents=[common])
    p.add_argument("--seeds", type=int, default=12)
    p.add_argument("--out", default="science_forensics/experiment/power_test_results.json")
    p = sub.add_parser("replay", parents=[common])
    p.add_argument("reduce_session_dir")
    p.add_argument("--center", type=float, required=True, help="candidate LSRK velocity centre, m/s")
    p.add_argument("--half-width", type=float, required=True, help="candidate half width, m/s")
    p.add_argument("--out")
    p = sub.add_parser("analyze", parents=[common])
    p.add_argument("reduce_session_dir")
    p.add_argument("--plan-csv", required=True)
    p.add_argument("--center", type=float, default=150857.0)
    p.add_argument("--half-width", type=float, default=5607.0)
    p.add_argument("--out-dir", default="data/science_forensics_experiment")
    p = sub.add_parser("validate-csv", parents=[common])
    p.add_argument("csv")
    args = ap.parse_args(argv)

    from science_forensics.experiment import cli_impl as C
    from science_forensics.models import sanitize
    if args.cmd == "design":
        out = C.cmd_design(args)
        print(json.dumps(sanitize(out), indent=1))
    elif args.cmd == "windows":
        for w in C.cmd_windows(args):
            print(f"{w['side']:>8} HA side: start between {w['first_start_utc']} and {w['last_start_utc']}")
    elif args.cmd == "simulate":
        res = C.cmd_simulate(args)
        print("\n".join(C.format_power(res)))
    elif args.cmd == "replay":
        res = C.cmd_replay(args)
        text = json.dumps(sanitize(res), indent=1, sort_keys=True)
        if args.out:
            Path(args.out).write_text(text)
        print(text if args.json or not args.out else f"written {args.out}")
    elif args.cmd == "analyze":
        res = C.cmd_analyze(args)
        out = Path(args.out_dir) / res["campaign_id"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "experiment_analysis.json").write_text(json.dumps(sanitize(res), indent=1, sort_keys=True))
        (out / "summary.md").write_text(C.analysis_markdown(res))
        print(C.analysis_markdown(res))
        print(f"written {out}")
    elif args.cmd == "validate-csv":
        from science_forensics.experiment.plan import validate_capture_csv
        v = validate_capture_csv(args.csv)
        print(json.dumps(v, indent=1))
        return 0 if v["ok"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
