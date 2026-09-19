#!/usr/bin/env python3
"""ALMITA Calibration CLI - OPERATIONAL/RELATIVE calibration only.

See docs/CALIBRATION_SCOPE.md before reading any output of this tool:
every result carries calibration_level=OPERATIONAL_RELATIVE and
absolute_calibration=false. Separate from almita_align.py by design -
calibration and alignment are different instrumental concerns that share
infrastructure, not one nested inside the other.

Hardware policy (Fase 50, still in force): `run` only accepts
--backend simulated in this pass. --backend real is a recognized choice
that always refuses, by construction, with a message pointing to
docs/CALIBRATION_FIRST_TEST_RUNBOOK.md - never wired to reach SDRCapture
in this codebase pass. No gain is ever changed automatically, no rtl_tcp
config is touched, no service is restarted.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional


def _print(payload: dict, as_json: bool, human_lines) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in human_lines:
            print(line)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def cmd_audit(args) -> int:
    from calibration_engine.calibration_level import ABSOLUTE_CALIBRATION_AVAILABLE, CURRENT_LEVEL
    from calibration_engine.gain_sweep import CANDIDATE_GAIN_TABLE_DB, GAIN_TABLE_PROVENANCE
    from calibration_engine.receiver_config import KNOWN_RECEIVERS
    from alignment_engine.deployment_state import DEFAULT_STATE_PATH, read_current_deployment_state

    deployment = read_current_deployment_state(DEFAULT_STATE_PATH)
    payload = {
        "calibration_level": CURRENT_LEVEL.value, "absolute_calibration": ABSOLUTE_CALIBRATION_AVAILABLE,
        "deployment_state": deployment.to_dict() if deployment else None,
        "known_receivers": {k: v.__dict__ for k, v in KNOWN_RECEIVERS.items()},
        "gain_table_provenance": GAIN_TABLE_PROVENANCE,
        "gain_table_size": len(CANDIDATE_GAIN_TABLE_DB),
        "scope_doc": "docs/CALIBRATION_SCOPE.md",
    }
    lines = [f"CalibrationLevel: {payload['calibration_level']} (absolute_calibration={payload['absolute_calibration']})",
             f"Deployment state: {deployment.state.value if deployment else 'MISSING/UNKNOWN'}",
             f"Known receivers: {', '.join(payload['known_receivers'])}",
             f"Gain table provenance: {GAIN_TABLE_PROVENANCE}",
             f"Gain table entries: {len(CANDIDATE_GAIN_TABLE_DB)}",
             "See docs/CALIBRATION_SCOPE.md for what can/cannot be measured."]
    _print(payload, args.json, lines)
    return 0


def cmd_plan(args) -> int:
    from calibration_engine.gain_sweep import build_gain_sweep_plan
    plan = build_gain_sweep_plan(args.around_gain, args.n_steps)
    per_step_seconds = args.capture_seconds + args.settle_seconds
    payload = {"gain_plan_db": plan, "n_steps": len(plan), "capture_seconds": args.capture_seconds,
               "settle_seconds": args.settle_seconds, "estimated_duration_seconds": len(plan) * per_step_seconds}
    lines = [f"Gain sweep plan around {args.around_gain} dB ({len(plan)} steps): {plan}",
             f"Estimated duration: {payload['estimated_duration_seconds']:.0f}s "
             f"({payload['estimated_duration_seconds']/60:.1f} min)"]
    _print(payload, args.json, lines)
    return 0


def cmd_preflight(args) -> int:
    """Fase 5: Calibration V1 does NOT require FIELD (or even a recorded
    deployment state) for most of its tests - a missing/UNKNOWN deployment
    state is recorded as a LABEL on the session (Fase 5 also says INDOOR/
    BENCH results must never be silently pooled with FIELD, which is what
    comparability.py enforces later), never a blocking preflight gate."""
    from alignment_engine.deployment_state import DEFAULT_STATE_PATH, read_current_deployment_state
    from calibration_engine.receiver_config import KNOWN_RECEIVERS
    import os

    gates = []          # blocking
    informational = []  # never blocks - recorded only

    def gate(name, passed, detail):
        gates.append({"name": name, "passed": passed, "detail": detail})

    def info(name, detail):
        informational.append({"name": name, "detail": detail})

    gate("receiver_known", args.receiver in KNOWN_RECEIVERS, args.receiver)
    deployment = read_current_deployment_state(DEFAULT_STATE_PATH)
    info("deployment_state", deployment.state.value if deployment else "UNKNOWN (no record) - will be "
                                                                         "labeled UNKNOWN in the session")
    try:
        stat = os.statvfs(args.session_root if Path(args.session_root).exists() else ".")
        free_bytes = stat.f_bavail * stat.f_frsize
        gate("disk_space_sufficient", free_bytes > 200_000_000, f"{free_bytes/1e9:.2f} GB free")
    except OSError as exc:
        gate("disk_space_sufficient", False, str(exc))
    Path(args.session_root).mkdir(parents=True, exist_ok=True)
    gate("output_writable", os.access(args.session_root, os.W_OK), args.session_root)

    failing = [g for g in gates if not g["passed"]]
    payload = {"gates": gates, "informational": informational, "result": "BLOCKED" if failing else "READY"}
    lines = [f"[{'ok' if g['passed'] else 'FAIL'}] {g['name']}: {g['detail']}" for g in gates]
    lines += [f"[info] {g['name']}: {g['detail']}" for g in informational]
    lines.append(f"PREFLIGHT: {payload['result']}")
    _print(payload, args.json, lines)
    return 0 if not failing else 2


def cmd_run(args) -> int:
    if args.backend == "real":
        payload = {"result": "REFUSED", "reason": "Calibration V1 (this pass) never authorizes --backend real. "
                                                    "See docs/CALIBRATION_FIRST_TEST_RUNBOOK.md for the designed "
                                                    "(not executed) first real test."}
        _print(payload, args.json, [f"REFUSED: {payload['reason']}"])
        return 3

    import asyncio
    from calibration_engine.acquisition import SimulatedCalibrationAcquisitionBackend
    from calibration_engine.bandpass import compute_bandpass
    from calibration_engine.calibration_level import ABSOLUTE_CALIBRATION_AVAILABLE, CURRENT_LEVEL
    from calibration_engine.clipping import evaluate_clipping
    from calibration_engine.quality import QualityInputs, evaluate_quality
    from calibration_engine.receiver_config import build_receiver_config_snapshot
    from calibration_engine.sample_statistics import compute_sample_statistics
    from calibration_engine.session import CalibrationSession, new_session_id
    from calibration_engine.simulation import InstrumentSimulationConfig, NAMED_SCENARIOS
    from calibration_engine.stability import StabilitySample, analyze_stability
    from calibration_engine.state_machine import CalibrationState, CalibrationStateMachine
    from alignment_engine.deployment_state import DEFAULT_STATE_PATH, read_current_deployment_state
    from runtime_state import utcnow

    session = CalibrationSession(args.session_root, new_session_id())
    machine = CalibrationStateMachine(utcnow)
    session.log_event("CALIBRATION_BEGIN", backend=args.backend, n_captures=args.n_captures)
    machine.transition(CalibrationState.PLANNED, "session created")

    deployment = read_current_deployment_state(DEFAULT_STATE_PATH)
    receiver_config = build_receiver_config_snapshot(
        args.receiver, center_frequency_hz=args.center_frequency_hz, sample_rate_hz=args.sample_rate_hz,
        gain_requested_db=args.gain_db, bias_t_state=args.bias_t_state,
        deployment_state=(deployment.state.value if deployment else "UNKNOWN"))
    session.write_receiver_config(receiver_config.to_dict())
    session.write_config({k: v for k, v in vars(args).items() if not callable(v)})
    session.write_identity({"session_id": session.session_id, "receiver_id": args.receiver})
    machine.transition(CalibrationState.PREFLIGHT, "config recorded")
    machine.transition(CalibrationState.READY, "no blocking preflight condition")

    scenario = getattr(args, "scenario", None)
    if scenario:
        if scenario not in NAMED_SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r} - must be one of {sorted(NAMED_SCENARIOS)}")
        scenario_factory = NAMED_SCENARIOS[scenario]
        config_factory = lambda i: scenario_factory(seed=i)  # noqa: E731
    else:
        config_factory = lambda i: InstrumentSimulationConfig(seed=i, gain_linear=args.gain_db / 2.0)  # noqa: E731
    backend = SimulatedCalibrationAcquisitionBackend(simulation_config_factory=config_factory)

    async def _acquire_all():
        results = []
        stability_samples = []
        for i in range(args.n_captures):
            session.log_event("CAPTURE_BEGIN", index=i)
            path = session.capture_path(f"capture_{i:03d}")
            await backend.capture(duration_seconds=args.capture_seconds, output_path=str(path),
                                   center_frequency_hz=args.center_frequency_hz, sample_rate_hz=args.sample_rate_hz,
                                   gain_db=args.gain_db, metadata={"index": i}, index=i)
            from calibration_engine.acquisition import read_capture_iq
            iq, _ = read_capture_iq(str(path))
            stats = compute_sample_statistics(iq)
            clipping = evaluate_clipping(stats)
            bandpass = compute_bandpass(iq, args.sample_rate_hz, args.center_frequency_hz)
            power = (stats.std_i ** 2 + stats.std_q ** 2) / 2.0
            stability_samples.append(StabilitySample(f"capture_{i:03d}", power, float(i * args.capture_seconds)))
            results.append({"index": i, "sample_statistics": stats.to_dict(), "clipping": clipping.to_dict(),
                             "usable_band_fraction": bandpass.usable_band_fraction})
            session.log_event("CAPTURE_END", index=i, clipping_status=clipping.status.value)
        return results, stability_samples

    machine.transition(CalibrationState.ACQUIRING, "starting captures")
    results, stability_samples = asyncio.run(_acquire_all())
    machine.transition(CalibrationState.ANALYZING, "captures complete")
    session.log_event("ANALYSIS_BEGIN")

    stability = analyze_stability(stability_samples).to_dict() if len(stability_samples) >= 3 else None
    worst_clipping = max((r["clipping"]["status"] for r in results), key=lambda s: ["OK", "WARNING", "CLIPPED", "UNKNOWN"].index(s))
    from calibration_engine.clipping import ClippingStatus
    quality_inputs = QualityInputs(
        metadata_complete=True, clipping_status=ClippingStatus(worst_clipping),
        valid_sample_fraction=1.0, stability_power_rms_fraction=(stability["power_rms_fraction"] if stability else None),
        usable_band_fraction=min(r["usable_band_fraction"] for r in results), temperature_range_c=None,
        rfi_contaminated_fraction=None)
    quality = evaluate_quality(quality_inputs)
    session.log_event("ANALYSIS_COMPLETE", verdict=quality.verdict)
    machine.transition(CalibrationState.RESULT_READY, "quality evaluated")

    result_payload = {
        "calibration_level": CURRENT_LEVEL.value, "absolute_calibration": ABSOLUTE_CALIBRATION_AVAILABLE,
        "receiver": receiver_config.to_dict(), "per_capture": results, "stability": stability,
        "quality": quality.to_dict(), "state_history": machine.history_as_dicts(),
    }
    session.write_result(result_payload)
    machine.transition(CalibrationState.COMPLETED, "session complete")
    session.write_state({"phase": machine.state.value, "history": machine.history_as_dicts()})

    lines = [f"Session: {session.dir}", f"Quality: {quality.verdict} ({'; '.join(quality.reasons)})",
             f"Worst clipping status: {worst_clipping}"]
    _print(result_payload, args.json, lines)
    return 0


def cmd_status(args) -> int:
    from calibration_engine.session import CalibrationSession
    session = CalibrationSession(Path(args.session_dir).parent, Path(args.session_dir).name)
    state = session.read_state()
    _print(state or {}, args.json, [json.dumps(state, indent=2)] if state else ["No state recorded"])
    return 0 if state else 1


def cmd_result(args) -> int:
    from calibration_engine.session import CalibrationSession
    session = CalibrationSession(Path(args.session_dir).parent, Path(args.session_dir).name)
    result = session.read_result()
    _print(result or {}, args.json, [json.dumps(result, indent=2)] if result else ["No result recorded"])
    return 0 if result else 1


def cmd_replay(args) -> int:
    from calibration_engine.replay import replay_calibration_session
    result = replay_calibration_session(args.session_dir, label=args.label)
    lines = [f"Analysis: {result.analysis_dir}", f"Captures: {result.n_captures_valid}/{result.n_captures_found} valid"]
    _print(result.to_dict(), args.json, lines)
    return 0


def cmd_compare(args) -> int:
    from calibration_engine.comparability import compare_calibration_sessions
    from calibration_engine.receiver_config import ReceiverConfigSnapshot
    from calibration_engine.session import CalibrationSession

    def _load(session_dir):
        session = CalibrationSession(Path(session_dir).parent, Path(session_dir).name)
        config = session.read_receiver_config()
        result = session.read_result()
        if config is None or result is None:
            raise SystemExit(f"{session_dir}: missing receiver_config.json or calibration_result.json")
        snapshot = ReceiverConfigSnapshot(
            receiver_id=config["receiver_id"], serial=config["serial"], tuner=config["tuner"],
            center_frequency_hz=config["center_frequency_hz"], sample_rate_hz=config["sample_rate_hz"],
            gain_requested_db=config["gain_requested_db"], agc_enabled=config["agc_enabled"],
            bias_t_state=config["bias_t_state"], rtl_tcp_host="", rtl_tcp_port=0,
            integration_seconds=config["integration_seconds"], fft_size=config["fft_size"],
            window=config["window"], averaging=config["averaging"], ppm_correction=config["ppm_correction"],
            frequency_offset_hz_status=config["frequency_offset_hz_status"], timestamp_utc=config["timestamp_utc"],
            host=config["host"], code_commit=config["code_commit"], working_tree_dirty=config["working_tree_dirty"],
            deployment_state=config["deployment_state"], temperature_readings=config["temperature_readings"])
        rms = float(np.sqrt(np.mean([r["sample_statistics"]["std_i"] ** 2 + r["sample_statistics"]["std_q"] ** 2
                                      for r in result["per_capture"]])))
        clipping_fraction = float(np.mean([r["clipping"]["rail_hit_fraction"] or 0.0 for r in result["per_capture"]]))
        return snapshot, rms, clipping_fraction

    import numpy as np
    snapshot_a, rms_a, clip_a = _load(args.session_a)
    snapshot_b, rms_b, clip_b = _load(args.session_b)
    result = compare_calibration_sessions(snapshot_a, snapshot_b, rms_a, rms_b, clip_a, clip_b)
    _print(result.to_dict(), args.json, [f"Verdict: {result.verdict}", f"Reason: {result.reason}"])
    return 0


def cmd_profile_build(args) -> int:
    from calibration_engine.gain_sweep import CANDIDATE_GAIN_TABLE_DB
    from calibration_engine.profile import OperationalEnvelope, build_draft_profile, write_profile
    from calibration_engine.session import CalibrationSession

    session = CalibrationSession(Path(args.session_dir).parent, Path(args.session_dir).name)
    result = session.read_result()
    receiver = session.read_receiver_config()
    if result is None or receiver is None:
        _print({"error": "missing result or receiver_config"}, args.json, ["ERROR: session has no result/receiver_config"])
        return 1
    envelope = OperationalEnvelope(
        gain_range_tested_db=[receiver["gain_requested_db"]], temperature_range_observed_c=None,
        clipping_maximum_observed=max((r["clipping"]["status"] for r in result["per_capture"]),
                                       key=lambda s: ["OK", "WARNING", "CLIPPED", "UNKNOWN"].index(s)),
        stability_rms_fraction_expected_max=(result["stability"]["power_rms_fraction"] if result.get("stability") else 1.0),
        usable_band_fraction=min(r["usable_band_fraction"] for r in result["per_capture"]), known_artifacts=[])
    profile = build_draft_profile(
        receiver_id=receiver["receiver_id"], receiver_serial=receiver["serial"],
        source_calibration_sessions=[session.session_id],
        conditions={"deployment_state": receiver["deployment_state"], "bias_t_state": receiver["bias_t_state"]},
        recommended_gain_db=receiver["gain_requested_db"], recommended_usable_band=None, known_masks=[],
        warmup_recommendation=None, envelope=envelope,
        limitations=["single session only", "no absolute RF calibration", "DRAFT - not reviewed"])
    path = write_profile(profile, Path(args.profiles_dir))
    _print(profile.to_dict(), args.json, [f"Profile written: {path}", f"Status: {profile.status} (never auto-ACTIVE)"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    audit_p = sub.add_parser("audit")
    _add_common(audit_p)
    audit_p.set_defaults(func=cmd_audit)

    plan_p = sub.add_parser("plan")
    plan_p.add_argument("--around-gain", type=float, default=40.2)
    plan_p.add_argument("--n-steps", type=int, default=6)
    plan_p.add_argument("--capture-seconds", type=float, default=2.0)
    plan_p.add_argument("--settle-seconds", type=float, default=1.0)
    _add_common(plan_p)
    plan_p.set_defaults(func=cmd_plan)

    preflight_p = sub.add_parser("preflight")
    preflight_p.add_argument("--receiver", default="MAIN")
    preflight_p.add_argument("--session-root", default="data/calibration")
    _add_common(preflight_p)
    preflight_p.set_defaults(func=cmd_preflight)

    run_p = sub.add_parser("run")
    run_p.add_argument("--backend", choices=["simulated", "real"], default="simulated")
    run_p.add_argument("--receiver", default="MAIN")
    run_p.add_argument("--session-root", default="data/calibration")
    run_p.add_argument("--center-frequency-hz", type=float, default=1_420_405_752.0)
    run_p.add_argument("--sample-rate-hz", type=float, default=2_400_000.0)
    run_p.add_argument("--gain-db", type=float, default=40.2)
    run_p.add_argument("--bias-t-state", default="ON")
    run_p.add_argument("--n-captures", type=int, default=5)
    run_p.add_argument("--capture-seconds", type=float, default=2.0)
    run_p.add_argument("--scenario", choices=["HEALTHY", "CLIPPED", "THERMAL_DRIFT", "RFI_CONTAMINATED"], default=None,
                        help="--backend simulated only: named instrument-behavior preset (Fase 16); "
                             "omit for the plain gain-only simulation")
    _add_common(run_p)
    run_p.set_defaults(func=cmd_run)

    status_p = sub.add_parser("status")
    status_p.add_argument("session_dir")
    _add_common(status_p)
    status_p.set_defaults(func=cmd_status)

    result_p = sub.add_parser("result")
    result_p.add_argument("session_dir")
    _add_common(result_p)
    result_p.set_defaults(func=cmd_result)

    replay_p = sub.add_parser("replay")
    replay_p.add_argument("session_dir")
    replay_p.add_argument("--label", default=None)
    _add_common(replay_p)
    replay_p.set_defaults(func=cmd_replay)

    compare_p = sub.add_parser("compare")
    compare_p.add_argument("session_a")
    compare_p.add_argument("session_b")
    _add_common(compare_p)
    compare_p.set_defaults(func=cmd_compare)

    profile_p = sub.add_parser("profile")
    profile_sub = profile_p.add_subparsers(dest="profile_command", required=True)
    profile_build_p = profile_sub.add_parser("build")
    profile_build_p.add_argument("session_dir")
    profile_build_p.add_argument("--profiles-dir", default="data/calibration/profiles")
    _add_common(profile_build_p)
    profile_build_p.set_defaults(func=cmd_profile_build)

    gain_sweep_p = sub.add_parser("gain-sweep")
    gain_sweep_p.add_argument("--dry-run", action="store_true", default=True)
    gain_sweep_p.add_argument("--around-gain", type=float, default=40.2)
    gain_sweep_p.add_argument("--n-steps", type=int, default=6)
    gain_sweep_p.add_argument("--capture-seconds", type=float, default=2.0)
    gain_sweep_p.add_argument("--settle-seconds", type=float, default=1.0)
    _add_common(gain_sweep_p)
    gain_sweep_p.set_defaults(func=cmd_plan)  # --dry-run gain-sweep is exactly the plan command today

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
