#!/usr/bin/env python3
"""ALMITA Observation Orchestrator CLI: plan / run / status / stop / validate / report.

Thin argparse wrapper around observation_spec.py / observation_plan.py /
observation_preflight.py / observation_orchestrator.py — the SAME core
almita_orchestrator_server.py (the Web API) calls. No planning/validation/
safety/execution logic lives in this file.

Examples:
  python3 almita_observe.py plan examples/overnight-400.yaml
  python3 almita_observe.py run data/mosaic/<session>-<ts>/observation_resolved.json --yes
  python3 almita_observe.py status
  python3 almita_observe.py stop --yes
  python3 almita_observe.py validate observation.yaml
  python3 almita_observe.py report data/mosaic/<session>-<ts>
"""
from __future__ import annotations

import argparse
import json
import sys

import observation_orchestrator
import observation_plan
import observation_spec


def _format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _gib(num_bytes: float) -> float:
    return num_bytes / (1024 ** 3)


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        spec = observation_spec.load_and_validate(args.observation_yaml)
    except observation_spec.ObservationSpecError as exc:
        print(f"INVALID SPEC: {exc}")
        return 1

    try:
        plan = observation_plan.plan_observation(spec, observer_config_path=args.config, data_dir=args.data_dir)
    except observation_plan.ObservationPlanError as exc:
        print(f"BLOCKED: {exc}")
        return 1

    r = plan["resolved"]
    print("ALMITA OBSERVATION PLAN")
    print("")
    print(f"Session             {plan['observation_name']}")
    print(f"Grid                {r['rows']} x {r['cols']} = {r['point_count']}")
    print(f"Footprint           {r['footprint_width_deg']:.2f} deg x {r['footprint_height_deg']:.2f} deg")
    print(f"Center              RA {r['center_ra_hours']:.4f}h  DEC {r['center_dec_deg']:+.4f} deg")
    print(f"Spacing             {r['spacing_deg']:.3f} deg")
    print(f"Capture             {plan['requested']['capture']['seconds']} s")
    print(f"Settle              {plan['requested']['capture']['settle_seconds']} s")
    print("")
    print(f"Estimated duration  {_format_duration(plan['duration']['estimated_seconds'])}")
    print(f"Conservative        {_format_duration(plan['duration']['conservative_seconds'])}")
    print(f"Estimated storage   {_gib(plan['storage']['required_bytes']):.2f} GiB")
    print(f"Free storage        {_gib(plan['storage']['free_bytes_at_plan_time']):.2f} GiB")
    print("")
    print(f"Visibility          {plan['visibility']}")
    print(f"Min predicted alt   {plan['min_predicted_altitude_deg']:.1f} deg")
    print(f"Placement reasoning {r['placement_reasoning']}")
    print("")
    print("NO HARDWARE HAS MOVED.")
    if plan["visibility"] == "BLOCK":
        print("PLAN BLOCKED.")
        print(f"Resolved plan (BLOCKED): {plan['_resolved_plan_path']}")
        return 1
    print("PLAN VALID. OPERATOR GO REQUIRED.")
    print(f"Resolved plan: {plan['_resolved_plan_path']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        result = observation_orchestrator.run_observation(
            args.resolved_plan, yes=args.yes, runtime_dir=args.runtime_dir,
            host=args.host, port=args.port, device_name=args.device,
        )
    except observation_orchestrator.OrchestratorError as exc:
        print(f"BLOCKED: {exc}")
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("orchestrator_state") in ("RUNNING", "READY") else 1


def cmd_status(args: argparse.Namespace) -> int:
    status = observation_orchestrator.get_status(runtime_dir=args.runtime_dir)
    print(json.dumps(status, indent=2))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    if not args.yes:
        typed = input("Type STOP to send SIGINT to the running observation: ")
        if typed.strip() != "STOP":
            print("aborted, nothing signaled")
            return 1
    try:
        result = observation_orchestrator.stop_observation(runtime_dir=args.runtime_dir, confirm=True)
    except observation_orchestrator.OrchestratorError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("orchestrator_state") == "COMPLETED" else 1


def cmd_validate(args: argparse.Namespace) -> int:
    path = args.path
    if path.endswith((".yaml", ".yml")):
        try:
            observation_spec.load_and_validate(path)
            print("VALID: observation spec")
            return 0
        except observation_spec.ObservationSpecError as exc:
            print(f"INVALID: {exc}")
            return 1
    plan = json.loads(open(path, encoding="utf-8").read())
    recomputed = observation_plan.recompute_config_hash(plan)
    if recomputed == plan.get("observation_config_sha256"):
        print("VALID: resolved plan hash matches")
        return 0
    print(f"INVALID: hash mismatch — recomputed {recomputed[:16]}..., stored "
          f"{plan.get('observation_config_sha256', '')[:16]}...")
    return 1


def cmd_report(args: argparse.Namespace) -> int:
    report = observation_orchestrator.generate_final_report(args.session_dir)
    print(json.dumps(report, indent=2))
    return 0 if report["final_verdict"] == "SUCCESS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="ALMITA Observation Orchestrator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="Validate + resolve an observation.yaml (zero hardware motion)")
    p_plan.add_argument("observation_yaml")
    p_plan.add_argument("--config", default="observer_config.json")
    p_plan.add_argument("--data-dir", default="./data/mosaic")
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="Execute a resolved plan (requires explicit operator GO)")
    p_run.add_argument("resolved_plan")
    p_run.add_argument("--yes", action="store_true", help="Unattended: proceed past WARNINGs without a TTY prompt")
    p_run.add_argument("--runtime-dir", default=observation_orchestrator.DEFAULT_RUNTIME_DIR)
    p_run.add_argument("--host", default="localhost")
    p_run.add_argument("--port", type=int, default=7624)
    p_run.add_argument("--device", default=None)
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="Read-only orchestrator + canonical acquisition status")
    p_status.add_argument("--runtime-dir", default=observation_orchestrator.DEFAULT_RUNTIME_DIR)
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="Clean SIGINT stop of the orchestrator-owned observation")
    p_stop.add_argument("--yes", action="store_true")
    p_stop.add_argument("--runtime-dir", default=observation_orchestrator.DEFAULT_RUNTIME_DIR)
    p_stop.set_defaults(func=cmd_stop)

    p_validate = sub.add_parser("validate", help="Validate a .yaml spec or an observation_resolved.json hash")
    p_validate.add_argument("path")
    p_validate.set_defaults(func=cmd_validate)

    p_report = sub.add_parser("report", help="Generate final_report.json/.md for a grid session directory")
    p_report.add_argument("session_dir")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
