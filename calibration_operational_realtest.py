#!/usr/bin/env python3
"""First real operational-calibration capture - MAIN receiver, current
config, no changes. Explicitly, narrowly authorized: 5 x 2s captures,
sample statistics, clipping/headroom, relative bandpass, cross-capture
repeatability. NO gain/PPM/Bias-T/frequency/sample-rate change, NO mount
movement, NO GOTO, NO tracking change, NO SYNC.

Never calls SDRCapture.configure() (see calibration_engine/acquisition.py's
RealCalibrationAcquisitionBackend docstring for why that is the actual
mechanism that makes this genuinely read-only at the rtl_tcp protocol
level) - center_frequency/sample_rate/gain/Bias-T are read from the LIVE
rtl_tcp process's own command line (read-only `ps` inspection) and
recorded as VERIFIED_BY_SERVICE_COMMAND_LINE, never transmitted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import numpy as np


def _gate(gates, name, passed, detail):
    gates.append({"name": name, "passed": bool(passed), "detail": detail})
    print(f"[PRECHECK] {name}: {'ok' if passed else 'FAIL'} ({detail})")


async def main(args) -> int:
    from alignment_engine import capture_conflict
    from alignment_engine.deployment_state import DEFAULT_STATE_PATH, read_current_deployment_state
    from calibration_engine.acquisition import RealCalibrationAcquisitionBackend, read_capture_iq
    from calibration_engine.bandpass import compute_bandpass
    from calibration_engine.calibration_level import ABSOLUTE_CALIBRATION_AVAILABLE, CURRENT_LEVEL
    from calibration_engine.clipping import evaluate_clipping
    from calibration_engine.cross_capture import compute_cross_capture_summary
    from calibration_engine.hardware_inspection import (
        inspect_rtl_tcp_service_command_line, probe_rtl_tcp_handshake,
    )
    from calibration_engine.quality import QualityInputs, evaluate_quality
    from calibration_engine.receiver_config import _current_commit_hash, _working_tree_dirty
    from calibration_engine.sample_statistics import compute_sample_statistics
    from calibration_engine.session import CalibrationSession, new_session_id
    from calibration_engine.thermal import DS18B20Reader

    gates = []
    deployment = read_current_deployment_state(DEFAULT_STATE_PATH)
    _gate(gates, "deployment_state_recorded_informational", True,
          deployment.state.value if deployment else "UNKNOWN/MISSING (informational only - not required for "
                                                      "this test per Fase 5)")

    conflict = capture_conflict.check_no_conflicting_capture()
    _gate(gates, "no_capture_conflict", not conflict.conflict, conflict.detail)

    # Fase-authorized-test finding: check_no_conflicting_capture() only
    # looks for a literal `capture.py` process or the orchestrator's
    # point-in-time capture_process_alive flag - it does NOT know about
    # quicklook_live.py or any other long-running rtl_tcp consumer serving
    # an ACTIVE orchestrator campaign between capture points. A real
    # attempt against this MAIN receiver was reset mid-capture
    # ("Connection reset by peer") while orchestrator_state was RUNNING
    # for a multi-day observation - this gate closes that specific gap
    # for THIS script without modifying the shared, frozen
    # capture_conflict.py module.
    try:
        import observation_orchestrator
        status = observation_orchestrator.get_status()
        orchestrator_state = (status.get("orchestrator") or {}).get("orchestrator_state")
    except Exception as exc:
        orchestrator_state = f"UNKNOWN ({type(exc).__name__}: {exc})"
    _gate(gates, "no_active_orchestrator_campaign", orchestrator_state != "RUNNING",
          f"orchestrator_state={orchestrator_state} - an ACTIVE campaign may hold or cycle the single "
          f"rtl_tcp client slot (e.g. via quicklook_live.py) even with no capture.py alive right now")

    handshake = probe_rtl_tcp_handshake(args.host, args.port)
    _gate(gates, "rtl_tcp_reachable_device_readback", handshake.reachable, handshake.detail)

    cmdline = inspect_rtl_tcp_service_command_line(args.port)
    _gate(gates, "service_command_line_found", cmdline.found, cmdline.detail)

    expected_serial = "00000001"
    found_serial = cmdline.parsed.get("serial") if cmdline.found else None
    _gate(gates, "expected_main_serial", found_serial == expected_serial,
          f"expected {expected_serial}, found {found_serial}")

    try:
        stat = os.statvfs(args.session_root if Path(args.session_root).exists() else ".")
        free_bytes = stat.f_bavail * stat.f_frsize
        _gate(gates, "disk_space_sufficient", free_bytes > 200_000_000, f"{free_bytes/1e9:.2f} GB free")
    except OSError as exc:
        _gate(gates, "disk_space_sufficient", False, str(exc))

    failing = [g for g in gates if not g["passed"] and g["name"] != "deployment_state_recorded_informational"]
    if failing:
        reason = "; ".join(f"{g['name']}: {g['detail']}" for g in failing)
        print(f"\nBLOCKED: {reason}")
        return 2

    # Everything below this line is genuinely known-good to proceed:
    # rtl_tcp is reachable, the running service is confirmed (via its own
    # argv, read-only) to be MAIN, and no conflicting capture is active.
    verified_center_frequency_hz = cmdline.parsed["center_frequency_hz"]
    verified_sample_rate_hz = cmdline.parsed["sample_rate_hz"]
    verified_gain_db = cmdline.parsed["gain_db"]
    verified_bias_t = cmdline.parsed["bias_t_enabled"]

    session = CalibrationSession(args.session_root, new_session_id())
    print(f"Session: {session.dir}")
    session.log_event("CALIBRATION_BEGIN", backend="real", receiver="MAIN", n_captures=args.n_captures)
    session.write_preflight({"gates": gates})

    observer_config_path = Path("observer_config.json")
    temperature_sensor_map = {}
    if observer_config_path.exists():
        try:
            temperature_sensor_map = json.loads(observer_config_path.read_text())
        except json.JSONDecodeError:
            temperature_sensor_map = {}
    sensors = DS18B20Reader(temperature_sensor_map)
    temperature_before = sensors.read_all()
    session.write_environment({"temperature_before": temperature_before,
                                "temperature_sensor_config_source": str(observer_config_path)})

    receiver_payload = {
        "receiver_id": "MAIN",
        "serial": {"value": found_serial, "verification": "VERIFIED_BY_SERVICE_COMMAND_LINE"},
        "tuner_type": {"value": handshake.tuner_type, "verification": "VERIFIED_BY_DEVICE_READBACK"},
        "gain_count_reported_by_device": {"value": handshake.gain_count, "verification": "VERIFIED_BY_DEVICE_READBACK"},
        "center_frequency_hz": {"value": verified_center_frequency_hz, "verification": "VERIFIED_BY_SERVICE_COMMAND_LINE"},
        "sample_rate_hz": {"value": verified_sample_rate_hz, "verification": "VERIFIED_BY_SERVICE_COMMAND_LINE"},
        "gain_requested_db": {"value": verified_gain_db, "verification": "VERIFIED_BY_SERVICE_COMMAND_LINE"},
        "gain_effective_db": {"value": None, "verification": "NOT_READABLE_BY_RTL_TCP_PROTOCOL"},
        "bias_t_state": {"value": "ON" if verified_bias_t else "OFF", "verification": "VERIFIED_BY_SERVICE_COMMAND_LINE"},
        "rtl_tcp_endpoint": f"{args.host}:{args.port}",
        "deployment_state": deployment.state.value if deployment else "UNKNOWN",
        "code_commit": _current_commit_hash(), "working_tree_dirty": _working_tree_dirty(),
        "note": "center_frequency/sample_rate/gain/Bias-T are read from the live rtl_tcp process's own "
                "argv (ps, read-only) - this codebase never sent them and rtl_tcp has no protocol "
                "readback for them. Only tuner_type/gain_count come from the connection handshake, a "
                "genuine device-side confirmation.",
    }
    session.write_receiver_config(receiver_payload)
    session.write_config(vars(args))

    per_capture = []
    rms_values, normalized_bandpasses, clipping_statuses, dc_masks = [], [], [], []
    backend = RealCalibrationAcquisitionBackend(host=args.host, port=args.port)
    await backend.connect()
    try:
        try:
            for i in range(args.n_captures):
                session.log_event("CAPTURE_BEGIN", index=i)
                path = session.capture_path(f"capture_{i:03d}")
                await backend.capture(
                    duration_seconds=args.capture_seconds, output_path=str(path),
                    center_frequency_hz=verified_center_frequency_hz, sample_rate_hz=verified_sample_rate_hz,
                    gain_db=verified_gain_db,
                    # sdr_capture.py's own attrs-writing logic (_capture_network)
                    # defaults center_frequency_hz to a HARDCODED 1420405752 unless
                    # metadata['center_frequency_hz'] is supplied directly (it reads
                    # metadata.get('center_freq', ...) for its OWN internal default,
                    # but capture_metadata.update(metadata) afterward lets an exact
                    # key match override that) - found by reading the source before
                    # ever running this against real hardware; without this key the
                    # HDF5 attrs would silently record the WRONG (stale, 752 Hz off)
                    # frequency instead of the one actually verified from the live
                    # rtl_tcp process's own command line.
                    metadata={"index": i, "receiver_id": "MAIN", "serial": found_serial,
                              "configuration_source": "VERIFIED_BY_SERVICE_COMMAND_LINE",
                              "center_frequency_hz": verified_center_frequency_hz,
                              "gain_requested_db": verified_gain_db, "rf_input": "ANTENNA_CURRENT_OPERATIONAL"})
                iq, attributes = read_capture_iq(str(path))
                unsigned_uint8 = iq.dtype == np.uint8
                nominal_midpoint_check = abs(float(np.mean(iq)) - 127.5) < 20.0
                stats = compute_sample_statistics(iq)
                clipping = evaluate_clipping(stats)
                bandpass = compute_bandpass(iq, verified_sample_rate_hz, verified_center_frequency_hz)
                record = {
                    "index": i, "source_file": str(path), "raw_format_uint8_confirmed": bool(unsigned_uint8),
                    "raw_format_nominal_midpoint_plausible": bool(nominal_midpoint_check),
                    "sample_statistics": stats.to_dict(), "clipping": clipping.to_dict(),
                    "usable_band_fraction": bandpass.usable_band_fraction,
                    "dc_half_width_hz": bandpass.dc_half_width_hz,
                }
                per_capture.append(record)
                rms_values.append(stats.rms)
                normalized_bandpasses.append(bandpass.normalized_bandpass)
                clipping_statuses.append(clipping.status.value)
                dc_masks.append(bandpass.dc_mask)
                print(f"[CAPTURE {i}] rms={stats.rms:.2f} clipping={clipping.status.value} "
                      f"usable_band={bandpass.usable_band_fraction:.3f}")
                session.log_event("CAPTURE_END", index=i, clipping_status=clipping.status.value)
        except Exception as exc:
            # Fase 20's own principle (from the HI pass) applied here: a
            # session must never end with an unhandled exception and no
            # recorded state - write exactly what happened and how far we
            # got before re-raising, so the evidence trail is honest even
            # on failure. Found the hard way: the first real attempt at
            # this test left no state.json at all until this fix.
            session.log_event("CALIBRATION_FAILED", error=f"{type(exc).__name__}: {exc}",
                               captures_completed=len(per_capture))
            session.write_result({
                "calibration_level": CURRENT_LEVEL.value, "absolute_calibration": ABSOLUTE_CALIBRATION_AVAILABLE,
                "receiver": receiver_payload, "per_capture": per_capture,
                "error": f"{type(exc).__name__}: {exc}", "captures_completed": len(per_capture),
                "captures_requested": args.n_captures,
            })
            session.write_state({"phase": "FAILED", "error": f"{type(exc).__name__}: {exc}",
                                  "captures_completed": len(per_capture)})
            print(f"\nFAILED after {len(per_capture)}/{args.n_captures} captures: {type(exc).__name__}: {exc}")
            print(f"Session evidence: {session.dir}")
            raise
    finally:
        await backend.close()

    temperature_after = sensors.read_all()
    session.write_environment({"temperature_before": temperature_before, "temperature_after": temperature_after,
                                "temperature_sensor_config_source": str(observer_config_path)})

    cross_capture = compute_cross_capture_summary(rms_values, normalized_bandpasses, clipping_statuses, dc_masks)
    session.log_event("ANALYSIS_COMPLETE")

    worst_clipping = max(clipping_statuses, key=lambda s: ["OK", "WARNING", "CLIPPED", "UNKNOWN"].index(s))
    from calibration_engine.clipping import ClippingStatus
    quality_inputs = QualityInputs(
        metadata_complete=True, clipping_status=ClippingStatus(worst_clipping), valid_sample_fraction=1.0,
        stability_power_rms_fraction=cross_capture.rms_variation_fraction,
        usable_band_fraction=min(r["usable_band_fraction"] for r in per_capture),
        temperature_range_c=None, rfi_contaminated_fraction=None)
    quality = evaluate_quality(quality_inputs)

    result_payload = {
        "calibration_level": CURRENT_LEVEL.value, "absolute_calibration": ABSOLUTE_CALIBRATION_AVAILABLE,
        "receiver": receiver_payload, "per_capture": per_capture, "cross_capture": cross_capture.to_dict(),
        "quality": quality.to_dict(),
        "temperature_before": temperature_before, "temperature_after": temperature_after,
    }
    session.write_result(result_payload)
    session.write_state({"phase": "COMPLETED"})
    session.log_event("CALIBRATION_COMPLETE", quality_verdict=quality.verdict)

    print(f"\nQuality: {quality.verdict} ({'; '.join(quality.reasons)})")
    print(f"Session evidence: {session.dir}")
    print(json.dumps(result_payload, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1234)
    parser.add_argument("--session-root", default="data/calibration")
    parser.add_argument("--n-captures", type=int, default=5)
    parser.add_argument("--capture-seconds", type=float, default=2.0)
    return parser


if __name__ == "__main__":
    sys.exit(asyncio.run(main(build_parser().parse_args())))
