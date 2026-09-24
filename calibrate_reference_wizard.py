#!/usr/bin/env python3
"""ALMITA physical-reference calibration wizard (50 ohm / HOT / COLD) - stateful CLI, one subcommand per
discrete web-driven step (start / set-reference / skip-reference / capture / next / finish / abort / status).
Real MAIN captures only in `capture` without --simulate; no mount movement anywhere in this script, ever.

Real chain this is built for: antenna -> Nooelec SAWbird H1 (LNA + filter) -> RTL-SDR Blog V4, rtl_tcp with
Bias-T enabled (-T). See calibration_engine/reference_wizard.py for the real analysis/Y-factor logic this
script only orchestrates and persists to data/calibration/WIZARD-.../wizard_state.json - no science here.

Each web click is exactly one invocation of this script against an existing session directory: the operator's
explicit confirmation (typing/clicking "connected", "capture now", "reconnect confirmed") IS the corresponding
subcommand call - nothing here advances on a timer.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from runtime_state import atomic_write_json, read_json_safe

from calibration_engine.reference_wizard import (
    WIZARD_REFERENCE_ORDER, ReferenceConfig, WizardConfig, WizardStep,
    bias_t_known_facts, compute_y_factor, evaluate_reference_captures,
)

SESSION_PREFIX = "WIZARD"


def _new_session_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{SESSION_PREFIX}-{now.strftime('%Y%m%d-%H%M%S-%f')}"


def _state_path(session_dir: Path) -> Path:
    return session_dir / "wizard_state.json"


def _load_state(session_dir: Path) -> Dict[str, Any]:
    state = read_json_safe(_state_path(session_dir))
    if state is None:
        raise SystemExit(f"no wizard_state.json in {session_dir}")
    return state


def _save_state(session_dir: Path, state: Dict[str, Any]) -> None:
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(_state_path(session_dir), state)


def _emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _require_step(state: Dict[str, Any], expected: WizardStep) -> None:
    if state["step"] != expected.value:
        raise SystemExit(f"wizard is at step {state['step']}, not {expected.value} - refusing")


def _current_kind(state: Dict[str, Any]) -> Optional[str]:
    i = state["reference_index"]
    return WIZARD_REFERENCE_ORDER[i] if i < len(WIZARD_REFERENCE_ORDER) else None


def cmd_start(args) -> int:
    from calibration_engine.hardware_inspection import inspect_rtl_tcp_service_command_line, probe_rtl_tcp_handshake
    session_id = _new_session_id()
    session_dir = Path(args.session_root) / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    (session_dir / "captures").mkdir(exist_ok=True)
    config = WizardConfig(n_captures=args.n_captures, capture_seconds=args.capture_seconds,
                          stabilize_seconds=args.stabilize_seconds, center_frequency_hz=args.center_freq,
                          sample_rate_hz=args.sample_rate, gain_db=args.gain,
                          clipping_rail_hit_fraction=args.clipping_threshold,
                          stability_rms_fraction_threshold=args.stability_threshold,
                          rfi_min_usable_band_fraction=args.rfi_threshold)
    bias_t = bias_t_known_facts(port=1234)
    # Real, read-only inspection only (handshake + argv) - never a config write, matching
    # calibration_operational_realtest.py's own established precheck pattern.
    handshake = probe_rtl_tcp_handshake("localhost", 1234, timeout=2.0)
    cmdline = inspect_rtl_tcp_service_command_line(1234)
    state = {
        "schema_version": 1, "session_id": session_id, "config": config.to_dict(),
        "step": WizardStep.PREPARE.value, "reference_index": 0, "references": [], "results": {},
        "bias_t_facts": bias_t,
        "receiver_snapshot": {"handshake": handshake.to_dict(), "service_command_line": cmdline.to_dict()},
        "created_utc": datetime.now(timezone.utc).isoformat(), "aborted": False,
        "reconnect_antenna_confirmed_utc": None, "y_factor": None, "profile_path": None,
    }
    _save_state(session_dir, state)
    _emit({"session_id": session_id, "session_dir": str(session_dir), "state": state})
    return 0


def cmd_set_reference(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.PREPARE)
    kind = _current_kind(state)
    if kind is None:
        raise SystemExit("all references already declared")
    if args.kind != kind:
        raise SystemExit(f"expected the next reference to be {kind} (fixed order: {list(WIZARD_REFERENCE_ORDER)}), "
                         f"got {args.kind!r}")
    ref = ReferenceConfig(kind=kind, connection_point=args.connection_point,
                          temperature_c=args.temperature_c, temperature_source=args.temperature_source,
                          confirmed_utc=datetime.now(timezone.utc).isoformat())
    state["references"].append({**ref.to_dict(), "status": "PENDING_CAPTURE"})
    state["step"] = WizardStep.STABILIZE.value
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def cmd_skip_reference(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.PREPARE)
    kind = _current_kind(state)
    if kind is None:
        raise SystemExit("all references already declared")
    state["references"].append({"kind": kind, "status": "SKIPPED", "reason": args.reason or "not available",
                                "confirmed_utc": datetime.now(timezone.utc).isoformat()})
    state["reference_index"] += 1
    state["step"] = (WizardStep.RECONNECT_ANTENNA.value if state["reference_index"] >= len(WIZARD_REFERENCE_ORDER)
                     else WizardStep.PREPARE.value)
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


async def _do_capture(session_dir: Path, state: Dict[str, Any], simulate: Optional[str]) -> Dict[str, Any]:
    from calibration_engine.acquisition import (
        RealCalibrationAcquisitionBackend, SimulatedCalibrationAcquisitionBackend, read_capture_iq,
    )
    cfg = WizardConfig.from_dict(state["config"])
    kind = state["references"][-1]["kind"]
    cap_dir = session_dir / "captures" / kind
    cap_dir.mkdir(parents=True, exist_ok=True)
    backend: Any
    if simulate:
        from calibration_engine.simulation import NAMED_SCENARIOS, flat_noisy_config
        scenario_fn = NAMED_SCENARIOS.get(simulate, flat_noisy_config)
        # Deterministic per (kind, index) seed - offline testing only, never used for a real capture.
        backend = SimulatedCalibrationAcquisitionBackend(
            simulation_config_factory=lambda i, _fn=scenario_fn, _k=kind: _fn(seed=(abs(hash((_k, i))) % 10_000)))
    else:
        backend = RealCalibrationAcquisitionBackend(host="localhost", port=1234)
        await backend.connect()
    iq_arrays = []
    try:
        for i in range(cfg.n_captures):
            path = cap_dir / f"capture_{i:03d}.h5"
            kwargs = dict(duration_seconds=cfg.capture_seconds, output_path=str(path),
                         center_frequency_hz=cfg.center_frequency_hz, sample_rate_hz=cfg.sample_rate_hz,
                         gain_db=cfg.gain_db)
            if simulate:
                # SimulatedCalibrationAcquisitionBackend.capture() already sets attrs["simulated"]=True itself -
                # this metadata dict must not repeat that key (found live: a duplicate keyword TypeError).
                await backend.capture(**kwargs, metadata={"reference_kind": kind, "simulated_scenario": simulate},
                                      index=i)
            else:
                await backend.capture(**kwargs, metadata={"reference_kind": kind,
                                                           "configuration_source": "VERIFIED_BY_SERVICE_COMMAND_LINE"})
            iq, _ = read_capture_iq(str(path))
            iq_arrays.append(iq)
    finally:
        if not simulate:
            await backend.close()
    return evaluate_reference_captures(iq_arrays, cfg.sample_rate_hz, cfg.center_frequency_hz, cfg)


def cmd_capture(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.STABILIZE)
    result = asyncio.run(_do_capture(session_dir, state, args.simulate))
    state["references"][-1]["status"] = "DONE"
    state["results"][state["references"][-1]["kind"]] = result
    state["step"] = WizardStep.RESULT.value
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0 if result["verdict"] != "FAIL" else 2


def cmd_next(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.RESULT)
    state["reference_index"] += 1
    state["step"] = (WizardStep.RECONNECT_ANTENNA.value if state["reference_index"] >= len(WIZARD_REFERENCE_ORDER)
                     else WizardStep.PREPARE.value)
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def cmd_abort(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    state["step"] = WizardStep.ABORTED.value
    state["aborted"] = True
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def _build_and_write_profile(session_dir: Path, state: Dict[str, Any]):
    from calibration_engine.profile import OperationalEnvelope, build_draft_profile, write_profile
    from calibration_engine.receiver_config import KNOWN_RECEIVERS
    done_refs = [r for r in state["references"] if r["status"] == "DONE"]
    if not done_refs:
        return None
    cfg = state["config"]
    worst_clip = "OK"
    usable_fracs = []
    for r in done_refs:
        inputs = state["results"][r["kind"]]["quality"]["inputs"]
        worst_clip = max(worst_clip, inputs["clipping_status"],
                         key=lambda s: ["OK", "WARNING", "CLIPPED", "UNKNOWN"].index(s))
        if inputs.get("usable_band_fraction") is not None:
            usable_fracs.append(inputs["usable_band_fraction"])
    envelope = OperationalEnvelope(
        gain_range_tested_db=[cfg["gain_db"]], temperature_range_observed_c=None,
        clipping_maximum_observed=worst_clip,
        stability_rms_fraction_expected_max=cfg["stability_rms_fraction_threshold"],
        usable_band_fraction=(min(usable_fracs) if usable_fracs else 0.0), known_artifacts=[])
    limitations = [
        "DRAFT - not reviewed (profile activation is a separate, future, explicit step)",
        "REDUCE may use ONLY this profile's recommended_gain_db and receiver identity",
        "REDUCE must NOT apply any y_factor/noise_temperature_k figure here as an absolute flux/Kelvin conversion",
        "calibration_level stays OPERATIONAL_RELATIVE regardless of the y_factor result",
    ]
    skipped = [r["kind"] for r in state["references"] if r["status"] == "SKIPPED"]
    if skipped:
        limitations.append(f"reference(s) not tested: {', '.join(skipped)}")
    if state.get("y_factor"):
        if state["y_factor"]["physical_units_justified"]:
            limitations.append(f"y_factor/noise_temperature_k characterizes only: {state['y_factor']['characterizes']}")
        else:
            limitations.append(f"Y-factor NOT computed: {state['y_factor']['reason']}")
    profile = build_draft_profile(
        receiver_id="MAIN", receiver_serial=KNOWN_RECEIVERS["MAIN"].serial,
        source_calibration_sessions=[state["session_id"]],
        conditions={"references_tested": [r["kind"] for r in done_refs], "bias_t_facts": state["bias_t_facts"],
                   "y_factor": state.get("y_factor")},
        recommended_gain_db=cfg["gain_db"], recommended_usable_band=None, known_masks=[],
        warmup_recommendation={"stabilize_seconds": cfg["stabilize_seconds"]}, envelope=envelope,
        limitations=limitations)
    return write_profile(profile, session_dir.parent / "profiles")


def cmd_finish(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.RECONNECT_ANTENNA)
    if not args.confirm:
        raise SystemExit("--confirm true is required to finish (operator must confirm the antenna is reconnected)")
    state["reconnect_antenna_confirmed_utc"] = datetime.now(timezone.utc).isoformat()

    refs_by_kind = {r["kind"]: r for r in state["references"]}
    y_factor = None
    hot, cold = refs_by_kind.get("HOT"), refs_by_kind.get("COLD")
    if hot and hot.get("status") == "DONE" and cold and cold.get("status") == "DONE":
        hot_ref = ReferenceConfig(kind="HOT", connection_point=hot["connection_point"],
                                  temperature_c=hot["temperature_c"], temperature_source=hot["temperature_source"],
                                  confirmed_utc=hot["confirmed_utc"])
        cold_ref = ReferenceConfig(kind="COLD", connection_point=cold["connection_point"],
                                   temperature_c=cold["temperature_c"], temperature_source=cold["temperature_source"],
                                   confirmed_utc=cold["confirmed_utc"])
        y_factor = compute_y_factor(hot_ref, state["results"]["HOT"], cold_ref, state["results"]["COLD"]).to_dict()
    state["y_factor"] = y_factor

    profile_path = _build_and_write_profile(session_dir, state)
    state["profile_path"] = str(profile_path) if profile_path else None
    state["step"] = WizardStep.DONE.value
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def cmd_status(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("--session-root", default="data/calibration")
    s.add_argument("--n-captures", type=int, default=5)
    s.add_argument("--capture-seconds", type=float, default=2.0)
    s.add_argument("--stabilize-seconds", type=float, default=20.0)
    s.add_argument("--center-freq", type=float, default=1_420_405_000.0)
    s.add_argument("--sample-rate", type=float, default=2_400_000.0)
    s.add_argument("--gain", type=float, default=40.2)
    s.add_argument("--clipping-threshold", type=float, default=1.0e-4)
    s.add_argument("--stability-threshold", type=float, default=0.10)
    s.add_argument("--rfi-threshold", type=float, default=0.5)
    s.set_defaults(func=cmd_start)

    sr = sub.add_parser("set-reference")
    sr.add_argument("--session-dir", required=True)
    sr.add_argument("--kind", required=True, choices=list(WIZARD_REFERENCE_ORDER))
    sr.add_argument("--connection-point", required=True, choices=["LNA_INPUT", "LNA_OUTPUT", "SDR_INPUT"])
    sr.add_argument("--temperature-c", type=float, default=None)
    sr.add_argument("--temperature-source", default="not measured")
    sr.set_defaults(func=cmd_set_reference)

    sk = sub.add_parser("skip-reference")
    sk.add_argument("--session-dir", required=True)
    sk.add_argument("--reason", default=None)
    sk.set_defaults(func=cmd_skip_reference)

    c = sub.add_parser("capture")
    c.add_argument("--session-dir", required=True)
    c.add_argument("--simulate", default=None,
                   help="offline-testing only: HEALTHY|CLIPPED|THERMAL_DRIFT|RFI_CONTAMINATED - never used for a real reference")
    c.set_defaults(func=cmd_capture)

    n = sub.add_parser("next")
    n.add_argument("--session-dir", required=True)
    n.set_defaults(func=cmd_next)

    a = sub.add_parser("abort")
    a.add_argument("--session-dir", required=True)
    a.set_defaults(func=cmd_abort)

    f = sub.add_parser("finish")
    f.add_argument("--session-dir", required=True)
    f.add_argument("--confirm", type=lambda v: str(v).lower() == "true", default=False)
    f.set_defaults(func=cmd_finish)

    st = sub.add_parser("status")
    st.add_argument("--session-dir", required=True)
    st.set_defaults(func=cmd_status)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
