#!/usr/bin/env python3
"""ALMITA REDUCE - single HDF5 capture bridge (LEVEL 0 RAW -> LEVEL 1 REDUCED SPECTRUM, one file).

almita_reduce.py (frozen) only operates on whole OBSERVE campaigns, via reduce_engine.ingest.discover_campaign()
- which requires a mosaic.csv at the campaign root. It has no path for an arbitrary, standalone HDF5 capture
(a CALIBRATE wizard capture, a lone diagnostic capture, anything outside a grid campaign). This script fills
exactly that one gap - nothing else - by building a SYNTHETIC, single-point CampaignManifest/PointRecord from
the one chosen file and handing it to the SAME frozen reduce_engine functions almita_reduce.py itself calls
(reduce_engine.validation.run_preflight/blocking_reason, reduce_engine.pipeline.reduce_campaign) - never a
second implementation of preflight or the reduction algorithm. reduce_engine/ and almita_reduce.py are never
imported for write access and never modified by this file.

Also exposes `inspect`, a read-only metadata preview reusing calibration_engine.frequency_axis (frequency-axis
software-consistency check + the standing PPM audit finding) - see that module's own docstring for the
distinction this project must never blur: frequency_axis_correct (provable in software) vs
absolute_frequency_calibrated (always False here - no independent physical/astronomical frequency reference
exists yet).

100% offline: filesystem-only, no hardware, no INDI, no rtl_tcp, no mount, no network, never touches the
original HDF5.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _print(payload: Dict[str, Any], as_json: bool, human_lines) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in human_lines:
            print(line)


def _campaign_id_for(path: Path) -> str:
    # A stable, human-meaningful, filesystem-safe id derived from the capture itself - never invented, never
    # colliding with a real OBSERVE campaign_id (which always comes from grid_metadata.json's session_name).
    return f"SINGLE-{path.stem}"[:120]


def _build_single_capture_manifest(capture_path: Path, observer_config_path: Optional[str],
                                   ra_hours: Optional[float], dec_deg: Optional[float]):
    from reduce_engine.ingest import CampaignManifest, PointRecord
    observer: Dict[str, Any] = {}
    if observer_config_path:
        try:
            observer = json.loads(Path(observer_config_path).read_text())
        except (OSError, ValueError):
            observer = {}
    point = PointRecord(point_index=0, ra_hours=ra_hours, dec_degrees=dec_deg,
                        capture_status_declared="success", data_filename_declared=capture_path.name,
                        resolved_path=capture_path, accepted=True, reject_reason=None)
    return CampaignManifest(campaign_id=_campaign_id_for(capture_path), root=capture_path.parent,
                            session_id=None, grid={}, observer=observer, points=[point])


def inspect_capture_metadata(capture: str | Path, fft_size: int = 8192) -> Dict[str, Any]:
    """Read-only metadata preview - never a calibration claim. Flags missing required attrs and known
    contradictions (e.g. the gain='auto' placeholder) rather than silently proceeding. Pure/importable (the web
    layer calls this directly, synchronously - never a second implementation of the same attrs read)."""
    from calibration_engine.frequency_axis import audit_ppm_configuration, validate_frequency_axis
    from reduce_engine.ingest import metadata_field
    import h5py
    path = Path(capture)
    if not path.is_file():
        raise FileNotFoundError(f"capture not found: {path}")
    with h5py.File(path, "r") as handle:
        attrs = {k: (v.item() if hasattr(v, "item") else v) for k, v in handle.attrs.items()}
        n_samples = int(handle["iq_data"].shape[0]) if "iq_data" in handle else None

    required = ("sample_rate_hz", "center_frequency_hz", "capture_start_utc")
    missing = [k for k in required if k not in attrs or attrs[k] in (None, "", "None")]
    gain_raw = attrs.get("gain")
    gain_requested = metadata_field(attrs, "gain_requested_db")
    gain_is_placeholder = str(gain_raw).lower() == "auto"
    contradictions: List[str] = []
    if attrs.get("capture_status") != "success":
        contradictions.append(f"capture_status={attrs.get('capture_status')!r} (not 'success')")
    if str(path).endswith(".part"):
        contradictions.append("filename ends in .part - a partial capture, not a valid REDUCE input")
    if gain_is_placeholder and gain_requested == "UNKNOWN":
        contradictions.append("gain attribute is the literal 'auto' placeholder AND no gain_requested_db is "
                              "recorded either - no real or requested gain value is known for this capture at all")
    elif gain_is_placeholder:
        contradictions.append("gain attribute is the literal 'auto' placeholder (sdr_capture.py's own "
                              "no-metadata-override default, not a measurement) - gain_requested_db is used "
                              "below instead, as the REQUESTED value (rtl_tcp has no gain readback at all)")

    # Known, already-confirmed contamination (found this session, reading sdr_capture.py's source and
    # cross-checking against real capture attrs): its frozen _capture_network attrs-writer defaults
    # center_frequency_hz to exactly this literal unless the caller's metadata dict supplied the exact key
    # 'center_frequency_hz' - some real captures from before that was fixed in calibrate_reference_wizard.py
    # carry this placeholder, not a real measurement of what rtl_tcp was actually tuned to.
    SDR_CAPTURE_PLACEHOLDER_CENTER_FREQUENCY_HZ = 1420405752
    if attrs.get("center_frequency_hz") == SDR_CAPTURE_PLACEHOLDER_CENTER_FREQUENCY_HZ:
        contradictions.append(f"center_frequency_hz is exactly {SDR_CAPTURE_PLACEHOLDER_CENTER_FREQUENCY_HZ} Hz - "
                              "sdr_capture.py's own hardcoded metadata-writer fallback, not necessarily what "
                              "rtl_tcp was actually configured to; treat this capture's frequency as suspect unless "
                              "independently corroborated (e.g. a receiver_snapshot/service-command-line record "
                              "from the session that produced it)")

    axis = None
    if not missing:
        axis = validate_frequency_axis(float(attrs["center_frequency_hz"]), float(attrs["sample_rate_hz"]), fft_size)
        if not axis.validated:
            contradictions.append(f"frequency axis internally inconsistent: {axis.reasons}")
    ppm = audit_ppm_configuration(None)
    topology = metadata_field(attrs, "instrument_topology", "rf_input", "reference_topology")
    if topology == "UNKNOWN":
        contradictions.append("no topology/rf_input/reference_topology attribute found - REDUCE's own "
                              "calibration-compatibility check needs this to confirm a profile applies here")

    return {
        "capture": str(path), "capture_status": attrs.get("capture_status"),
        "center_frequency_hz_nominal": attrs.get("center_frequency_hz"),
        "sample_rate_hz": attrs.get("sample_rate_hz"),
        "gain_db_requested": None if gain_requested == "UNKNOWN" else gain_requested,
        "gain_raw_attribute": gain_raw,
        "duration_seconds": metadata_field(attrs, "duration_seconds", "capture_time_seconds"),
        "capture_start_utc": metadata_field(attrs, "capture_start_utc"),
        "topology": topology,
        "receiver": metadata_field(attrs, "receiver_id", "serial"),
        "n_samples": n_samples,
        "attrs_missing_required": missing,
        "contradictions": contradictions,
        "frequency_axis_check": axis.to_dict() if axis else None,
        "ppm_status": ppm.to_dict(),
        "all_attrs": attrs,
    }


def cmd_inspect(args) -> int:
    payload = inspect_capture_metadata(args.capture, fft_size=args.fft_size)
    axis = payload["frequency_axis_check"]
    _print(payload, args.json, [
        f"capture: {payload['capture']}  status: {payload['capture_status']}",
        f"center_frequency_hz (NOMINAL - as configured, not independently verified): {payload['center_frequency_hz_nominal']}",
        f"sample_rate_hz: {payload['sample_rate_hz']}",
        f"gain_db (requested; rtl_tcp has no gain readback): {payload['gain_db_requested']}",
        f"duration_seconds: {payload['duration_seconds']}   capture_start_utc: {payload['capture_start_utc']}",
        f"topology: {payload['topology']}   receiver: {payload['receiver']}",
        *([f"MISSING required attrs: {payload['attrs_missing_required']}"] if payload["attrs_missing_required"] else []),
        *([f"CONTRADICTION: {c}" for c in payload["contradictions"]]),
        f"frequency_axis_correct: {axis['frequency_axis_correct'] if axis else 'N/A (missing required attrs)'}",
        f"absolute_frequency_calibrated: False - {payload['ppm_status']['status']}",
    ])
    return 1 if (payload["attrs_missing_required"] or payload["contradictions"]) else 0


def cmd_plan(args) -> int:
    from reduce_engine.config import ReduceConfig
    from reduce_engine.validation import estimate_output_bytes, run_preflight

    capture_path = Path(args.capture)
    manifest = _build_single_capture_manifest(capture_path, args.observer_config, args.ra_hours, args.dec_deg)
    config = ReduceConfig(velocity_frame=args.velocity_frame, calibration_profile_path=args.calibration_profile)
    checks = run_preflight(manifest, config, output_root=args.output_root, calibration_profile_path=args.calibration_profile)

    # run_preflight() only confirms the profile FILE PAIR exists - it does not check that THIS capture is
    # actually compatible with it (frequency/rate/gain/topology). Add that real check here, surfaced to the
    # operator before RUN, same calibration_foundation.check_calibration_compatibility() REDUCE's own
    # calibration stage will use.
    compatibility = None
    if args.calibration_profile and capture_path.is_file():
        from reduce_engine.validation import Check
        from calibration_foundation import check_calibration_compatibility, load_calibration_profile
        try:
            profile = load_calibration_profile(args.calibration_profile)
            compatibility = check_calibration_compatibility(profile, capture_path)
            checks.append(Check("calibration_compatible_with_this_capture",
                                compatibility["status"] == "COMPATIBLE",
                                f"{compatibility['status']}: {compatibility['reason']}"))
        except (OSError, ValueError, KeyError) as exc:
            compatibility = {"status": "UNKNOWN", "reason": str(exc)}
            checks.append(Check("calibration_compatible_with_this_capture", False, str(exc)))

    estimated_bytes = estimate_output_bytes(config, len(manifest.accepted_points()))
    payload = {
        "capture": str(capture_path), "campaign_id": manifest.campaign_id, "config": config.to_dict(),
        "config_hash": config.config_hash(), "checks": [c.to_dict() for c in checks],
        "blocked": any(not c.ok for c in checks), "compatibility": compatibility,
        "estimated_output_bytes": estimated_bytes, "estimated_output_mb": round(estimated_bytes / 1e6, 2),
    }
    _print(payload, args.json, [
        f"capture: {payload['capture']}  campaign_id: {payload['campaign_id']}  config_hash: {payload['config_hash'][:12]}",
        f"estimated output size: ~{payload['estimated_output_mb']} MB",
        *[f"[{'PASS' if c.ok else 'BLOCKED'}] {c.name}: {c.detail}" for c in checks],
    ])
    return 1 if payload["blocked"] else 0


def cmd_run(args) -> int:
    from reduce_engine.config import ReduceConfig
    from reduce_engine.pipeline import reduce_campaign
    from reduce_engine.validation import blocking_reason, run_preflight

    capture_path = Path(args.capture)
    manifest = _build_single_capture_manifest(capture_path, args.observer_config, args.ra_hours, args.dec_deg)
    config = ReduceConfig(velocity_frame=args.velocity_frame, calibration_profile_path=args.calibration_profile)
    checks = run_preflight(manifest, config, output_root=args.output_root, calibration_profile_path=args.calibration_profile)
    reason = blocking_reason(checks)
    if reason:
        _print({"blocked": True, "reason": reason}, args.json, [f"BLOCKED: {reason}"])
        return 1

    # SAME frozen reduce_engine.pipeline.reduce_campaign() a real campaign RUN calls - a synthetic one-point
    # manifest is a valid CampaignManifest like any other; nothing about reduce_campaign() is single/multi-
    # point-aware beyond iterating manifest.points, so this is genuine reuse, not a parallel implementation.
    report = reduce_campaign(manifest, config, output_root=args.output_root, calibration_profile_path=args.calibration_profile)
    payload = report.__dict__
    _print(payload, args.json, [
        f"REDUCE (single capture) {report.status}",
        f"Capture:         {capture_path}",
        f"Campaign id:     {report.campaign_id}",
        f"Calibration:     {report.calibration_level_counts}",
        f"Velocity:        {report.velocity_frame_counts}",
        f"Quality:         {report.quality_counts}",
        f"Runtime:         {report.runtime_seconds:.2f}s",
        f"Output:          {report.output_dir}",
    ])
    return 0 if report.status in ("COMPLETED", "PARTIAL") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p):
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("inspect", help="read-only metadata preview of one HDF5 capture, writes nothing")
    p.add_argument("capture")
    p.add_argument("--fft-size", type=int, default=8192)
    _add_common(p)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("plan", help="preflight + compatibility checks for one capture, writes nothing")
    p.add_argument("capture")
    p.add_argument("--output-root", default="data/reduced")
    p.add_argument("--velocity-frame", default="lsrk")
    p.add_argument("--calibration-profile", default=None)
    p.add_argument("--observer-config", default=None)
    p.add_argument("--ra-hours", type=float, default=None)
    p.add_argument("--dec-deg", type=float, default=None)
    _add_common(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="run REDUCE V1 on one capture")
    p.add_argument("capture")
    p.add_argument("--output-root", default="data/reduced")
    p.add_argument("--velocity-frame", default="lsrk")
    p.add_argument("--calibration-profile", default=None)
    p.add_argument("--observer-config", default=None)
    p.add_argument("--ra-hours", type=float, default=None)
    p.add_argument("--dec-deg", type=float, default=None)
    _add_common(p)
    p.set_defaults(func=cmd_run)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
