"""CALIBRATE web route handlers for almita_orchestrator_server.py.

Every handler calls almita_calibrate.py's own CLI functions (cmd_plan,
cmd_preflight, cmd_run) directly - via stdout-JSON capture for the
thin/simple ones, or as a background job for cmd_run - or calibration_engine
functions directly for replay/compare/profile-build, exactly mirroring
what almita_calibrate.py itself does for those. No science is re-derived
here.
"""
from __future__ import annotations

import contextlib
import io
import json
from types import SimpleNamespace
from typing import Any, Dict, Optional

import threading

import almita_calibrate
from almita_web_common import JOBS, envelope, get_sdr_resource_status, list_sessions, resolve_within_root

SESSION_ROOT = "data/calibration"
_RUN_LAUNCH_LOCK = threading.Lock()  # serializes the brief CalibrationSession.__init__ patch below


def _capture_json(fn, args) -> Dict[str, Any]:
    """Calls an almita_calibrate.py cmd_* function exactly as the CLI
    would (args.json=True) and parses back its own stdout JSON - reuses
    the function wholesale instead of re-deriving its logic."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        fn(args)
    return json.loads(buffer.getvalue())


def get_status() -> Dict[str, Any]:
    from alignment_engine.deployment_state import DEFAULT_STATE_PATH, read_current_deployment_state
    from calibration_engine.calibration_level import ABSOLUTE_CALIBRATION_AVAILABLE, CURRENT_LEVEL
    from calibration_engine.gain_sweep import CANDIDATE_GAIN_TABLE_DB, GAIN_TABLE_PROVENANCE
    from calibration_engine.hardware_inspection import inspect_rtl_tcp_service_command_line, probe_rtl_tcp_handshake
    from calibration_engine.receiver_config import KNOWN_RECEIVERS

    deployment = read_current_deployment_state(DEFAULT_STATE_PATH)
    resource = get_sdr_resource_status()
    handshake = probe_rtl_tcp_handshake("localhost", 1234, timeout=1.5)
    cmdline = inspect_rtl_tcp_service_command_line(1234)

    receiver: Dict[str, Any] = {"receiver_id": "MAIN", "serial": KNOWN_RECEIVERS["MAIN"].serial,
                                 "tuner": "R828D (declared - see docs/CALIBRATION_SCOPE.md)"}
    if handshake.reachable:
        receiver["tuner_type"] = {"value": handshake.tuner_type, "verification": "VERIFIED_BY_DEVICE_READBACK"}
        receiver["gain_count_reported_by_device"] = {"value": handshake.gain_count,
                                                       "verification": "VERIFIED_BY_DEVICE_READBACK"}
    if cmdline.found:
        for key in ("center_frequency_hz", "sample_rate_hz", "gain_db"):
            if key in cmdline.parsed:
                receiver[key] = {"value": cmdline.parsed[key], "verification": "VERIFIED_BY_SERVICE_COMMAND_LINE"}
        receiver["bias_t_state"] = {"value": "ON" if cmdline.parsed.get("bias_t_enabled") else "OFF",
                                     "verification": "VERIFIED_BY_SERVICE_COMMAND_LINE"}
    else:
        receiver["note"] = "rtl_tcp service command line not found - values below are CONFIGURED/EXPECTED only"
        receiver["center_frequency_hz"] = {"value": 1_420_405_752.0, "verification": "CONFIGURED_EXPECTED"}
        receiver["sample_rate_hz"] = {"value": 2_400_000.0, "verification": "CONFIGURED_EXPECTED"}
        receiver["gain_db"] = {"value": 40.2, "verification": "CONFIGURED_EXPECTED"}

    frequency_audit = None
    center = receiver.get("center_frequency_hz", {}).get("value")
    if center is not None:
        nominal_hi_rest_hz = 1_420_405_751.77
        frequency_audit = {
            "service_center_frequency_hz": center, "nominal_hi_rest_hz": nominal_hi_rest_hz,
            "difference_hz": round(nominal_hi_rest_hz - center, 2),
            "label": "CONFIG_MISMATCH_DIFFERENCE_NOT_A_MEASURED_FREQUENCY_ERROR",
        }

    return envelope({
        "calibration_level": CURRENT_LEVEL.value, "absolute_calibration": ABSOLUTE_CALIBRATION_AVAILABLE,
        "deployment_state": deployment.state.value if deployment else "UNKNOWN",
        # Fase A3: CALIBRATION WORKFLOW is a concept independent of
        # resource/observation state - whether THIS app has a simulation
        # job in flight right now, not whether MAIN is claimed by anything
        # else. Survives a page reload (unlike a client-only flag) since
        # JOBS is server-side.
        "calibration_workflow_active": JOBS.any_alive(prefix="CAL-"),
        "resource": resource.to_dict(), "receiver": receiver,
        "gain_table": {"candidate_table_size": len(CANDIDATE_GAIN_TABLE_DB), "provenance": GAIN_TABLE_PROVENANCE,
                        "device_reported_gain_count": handshake.gain_count if handshake.reachable else None,
                        "status": ("UNVERIFIED_FOR_THIS_DEVICE" if not handshake.reachable or
                                   handshake.gain_count != len(CANDIDATE_GAIN_TABLE_DB) else "MATCHES_DEVICE_COUNT")},
        "frequency_audit": frequency_audit,
        "sessions": list_sessions(SESSION_ROOT, limit=8, prefix="CAL-"),
        "profiles": list_sessions(f"{SESSION_ROOT}/profiles", limit=8, prefix="PROFILE-"),
    })


def plan(body: Dict[str, Any]) -> Dict[str, Any]:
    args = SimpleNamespace(around_gain=float(body.get("around_gain", 40.2)), n_steps=int(body.get("n_steps", 6)),
                            capture_seconds=float(body.get("capture_seconds", 2.0)),
                            settle_seconds=float(body.get("settle_seconds", 1.0)), json=True)
    return envelope(_capture_json(almita_calibrate.cmd_plan, args))


def preflight(body: Dict[str, Any]) -> Dict[str, Any]:
    args = SimpleNamespace(receiver=body.get("receiver", "MAIN"), session_root=SESSION_ROOT, json=True)
    payload = _capture_json(almita_calibrate.cmd_preflight, args)
    blocked = payload.get("result") == "BLOCKED"
    return envelope(payload, blocked=blocked, reason=None if not blocked else "one or more gates failed - see gates[]")


def run_simulation(body: Dict[str, Any]) -> Dict[str, Any]:
    """Background job (Fase 30). REAL is never reachable here - only
    --backend simulated, matching almita_calibrate.py's own standing
    policy (--backend real always refuses).

    cmd_run() picks its own session_id internally (CalibrationSession's
    own new_session_id()) - to hand the caller a pollable id without a
    race, this patches CalibrationSession.__init__ just long enough to
    observe the id at construction time (the very first thing cmd_run()
    does) and signals an Event, rather than guessing or duplicating
    cmd_run()'s own session-naming logic."""
    import threading
    from calibration_engine.session import CalibrationSession

    scenario = body.get("scenario")
    args = SimpleNamespace(
        backend="simulated", receiver=body.get("receiver", "MAIN"), session_root=SESSION_ROOT,
        center_frequency_hz=float(body.get("center_frequency_hz", 1_420_405_752.0)),
        sample_rate_hz=float(body.get("sample_rate_hz", 2_400_000.0)), gain_db=float(body.get("gain_db", 40.2)),
        bias_t_state=body.get("bias_t_state", "ON"), n_captures=int(body.get("n_captures", 5)),
        capture_seconds=float(body.get("capture_seconds", 2.0)), scenario=scenario, json=True,
    )

    captured: Dict[str, Optional[str]] = {"session_id": None}
    ready = threading.Event()
    original_init = CalibrationSession.__init__

    def _observing_init(self, root, session_id, *a, **kw):
        original_init(self, root, session_id, *a, **kw)
        captured["session_id"] = session_id
        ready.set()

    def _job() -> None:
        with _RUN_LAUNCH_LOCK:
            CalibrationSession.__init__ = _observing_init
            try:
                almita_calibrate.cmd_run(args)
            except Exception:
                pass
            finally:
                CalibrationSession.__init__ = original_init
                ready.set()

    job_thread = threading.Thread(target=_job, name="web-calibrate-run", daemon=True)
    job_thread.start()
    ready.wait(timeout=10.0)
    session_id = captured["session_id"]
    if session_id is None:
        return envelope(blocked=True, reason="run did not start a session within 10s (server busy with another run?)")
    JOBS.register(session_id, job_thread)
    return envelope({"session_id": session_id, "state": "STARTED", "scenario": scenario})


def get_session(session_id: str) -> Dict[str, Any]:
    from pathlib import Path
    from calibration_engine.session import CalibrationSession
    root = Path(SESSION_ROOT).resolve()
    candidate = (root / session_id).resolve()
    if candidate != root and root not in candidate.parents:
        return envelope(blocked=True, reason="invalid session id")  # Fase 57: no path traversal
    if not candidate.is_dir():
        return envelope(blocked=True, reason=f"no session found: {session_id}")
    session = CalibrationSession(SESSION_ROOT, session_id)
    state = session.read_state()
    result = session.read_result()
    return envelope({"session_id": session_id, "state": state, "result": result,
                      "job_running": JOBS.is_alive(session_id)})


def replay(body: Dict[str, Any]) -> Dict[str, Any]:
    from calibration_engine.replay import replay_calibration_session
    session_dir = body.get("session_dir")
    if not session_dir:
        return envelope(blocked=True, reason="session_dir is required")
    resolved = resolve_within_root(SESSION_ROOT, session_dir)
    if resolved is None:
        return envelope(blocked=True, reason="session_dir must resolve inside data/calibration")  # Fase 57
    result = replay_calibration_session(str(resolved), label=body.get("label"))
    return envelope(result.to_dict())


def compare(body: Dict[str, Any]) -> Dict[str, Any]:
    from calibration_engine.comparability import compare_calibration_sessions
    from calibration_engine.receiver_config import ReceiverConfigSnapshot
    from calibration_engine.session import CalibrationSession
    import numpy as np

    def _load(session_id):
        # Fase 57: validate BEFORE constructing CalibrationSession, whose
        # own __init__ calls mkdir(parents=True, exist_ok=True) on whatever
        # path it is given - an unvalidated session_id (e.g. an absolute
        # path, which Path.__truediv__ would let override the root
        # entirely) could otherwise make this endpoint create arbitrary
        # directories as the service user, not just read them.
        resolved = resolve_within_root(SESSION_ROOT, session_id)
        if resolved is None:
            raise ValueError(f"invalid session id: {session_id!r}")
        session = CalibrationSession(SESSION_ROOT, resolved.name)
        config = session.read_receiver_config()
        result = session.read_result()
        if config is None or result is None:
            raise ValueError(f"{session_id}: missing receiver_config.json or calibration_result.json")
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

    session_a, session_b = body.get("session_a"), body.get("session_b")
    if not session_a or not session_b:
        return envelope(blocked=True, reason="session_a and session_b are required")
    try:
        snap_a, rms_a, clip_a = _load(session_a)
        snap_b, rms_b, clip_b = _load(session_b)
    except ValueError as exc:
        return envelope(blocked=True, reason=str(exc))
    result = compare_calibration_sessions(snap_a, snap_b, rms_a, rms_b, clip_a, clip_b)
    return envelope(result.to_dict())


def profile_build(body: Dict[str, Any]) -> Dict[str, Any]:
    from calibration_engine.profile import OperationalEnvelope, build_draft_profile, write_profile
    from calibration_engine.session import CalibrationSession

    session_id = body.get("session_id")
    if not session_id:
        return envelope(blocked=True, reason="session_id is required")
    resolved = resolve_within_root(SESSION_ROOT, session_id)  # Fase 57 - see compare()'s own note
    if resolved is None:
        return envelope(blocked=True, reason="invalid session id")
    session = CalibrationSession(SESSION_ROOT, resolved.name)
    result = session.read_result()
    receiver = session.read_receiver_config()
    if result is None or receiver is None:
        return envelope(blocked=True, reason="session has no result/receiver_config")
    envelope_obj = OperationalEnvelope(
        gain_range_tested_db=[receiver["gain_requested_db"]], temperature_range_observed_c=None,
        clipping_maximum_observed=max((r["clipping"]["status"] for r in result["per_capture"]),
                                       key=lambda s: ["OK", "WARNING", "CLIPPED", "UNKNOWN"].index(s)),
        stability_rms_fraction_expected_max=(result["stability"]["power_rms_fraction"] if result.get("stability") else 1.0),
        usable_band_fraction=min(r["usable_band_fraction"] for r in result["per_capture"]), known_artifacts=[])
    profile = build_draft_profile(
        receiver_id=receiver["receiver_id"], receiver_serial=receiver["serial"],
        source_calibration_sessions=[session_id],
        conditions={"deployment_state": receiver["deployment_state"], "bias_t_state": receiver["bias_t_state"]},
        recommended_gain_db=receiver["gain_requested_db"], recommended_usable_band=None, known_masks=[],
        warmup_recommendation=None, envelope=envelope_obj,
        limitations=["single session only", "no absolute RF calibration", "DRAFT - not reviewed"])
    path = write_profile(profile, f"{SESSION_ROOT}/profiles")
    return envelope({"profile_path": str(path), **profile.to_dict()})
