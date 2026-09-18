#!/usr/bin/env python3
"""FIRST REAL HI NIGHT SCAN (explicitly authorized, OBSERVATIONAL ONLY):
a single real 5x5 raster around a real HI4PI-selected target, using
SIDEREAL tracking, real GOTOs, and real MAIN SDR acquisition - never SYNC,
never a pointing-model change, never a second automatic scan.

Objective (verbatim from the authorization): "does ALMITA observe a
spatial HI distribution compatible with HI4PI's beam-convolved
prediction" - and, secondarily, what pointing offset the fitter estimates.
The offset is a MEASUREMENT, not an action - this script cannot SYNC no
matter what flags are passed (alignment_engine.hi.sync_policy.
AlignmentPhase.FIRST_LIGHT_HI has no override anywhere in its own module).

Safety design (same lineage as hw_tracking_selftest.py /
hw_solar_goto_selftest.py):
  - Defaults to --backend simulated - a full rehearsal of this script's
    own orchestration (including the real spectral pipeline, real
    Quality V2 with mandatory bootstrap, and real GOTO-write-audit logic)
    against SimulatedMountAdapter/SimulatedTrackingBackend/
    SimulatedHIAcquisitionBackend - real hardware only with
    --backend real --yes together.
  - Three preflight stages (system / astronomical / reference-structure),
    each capable of independently aborting BEFORE any mount write - never
    silently degraded (a point outside limits blocks the whole raster
    rather than being silently dropped from the plan; a weak reference
    region blocks rather than being observed anyway).
  - Every raster point is re-checked against altitude limits immediately
    before its own GOTO, not just once during preflight.
  - Writes only TELESCOPE_TRACK_MODE (SIDEREAL, restored on exit) and
    ON_COORD_SET+EQUATORIAL_EOD_COORD (one GOTO per raster point) - an
    explicit write audit checks this and nothing else was written; TRACK_STATE
    is never touched (not requested, and a wide ~20deg beam makes idle-axis
    drift during one integration utterly negligible - documented, not
    assumed).
  - Raw IQ for every point is preserved (session's own points/ directory,
    one HDF5 per point) - never overwritten by a derived product.
  - Quality V2 runs WITH bootstrap (mandatory - no GOOD verdict is
    possible without it, per the previous pass's own finding that
    coverage+null-model checks alone can be fooled by overfitting).
  - SYNC is never offered, never prompted for, and cannot be forced -
    AlignmentPhase.FIRST_LIGHT_HI unconditionally blocks it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import astropy.units as u
import numpy as np
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time

from alignment import offset_coordinates
from alignment_engine import capture_conflict
from alignment_engine.config import HIScanConfig
from alignment_engine.fitting import fit_raster
from alignment_engine.hi.acquisition import (
    RealHIAcquisitionBackend,
    SimulatedHIAcquisitionBackend,
    acquire_and_reduce_point,
)
from alignment_engine.hi.fits_reference import FITSMomentMapProvider, validate_reference
from alignment_engine.hi.quality_v2 import evaluate_quality_v2, spatial_sampling_quality
from alignment_engine.hi.reference_trust import ReferenceManifest, ReferenceTrust
from alignment_engine.hi.spectral_pipeline import SpectralPipelineConfig
from alignment_engine.hi.sync_policy import AlignmentPhase, evaluate_phase_gate
from alignment_engine.hi.target_selection import score_candidate
from alignment_engine.mount_adapter import RealMountAdapter, SimulatedMountAdapter
from alignment_engine.scan_planner import build_raster
from alignment_engine.session import AlignmentSession
from alignment_engine.tracking import (
    RealTrackingBackend, SimulatedTrackingBackend, TrackingMode, TrackingSession,
    parse_number_vector, parse_switch_vector, read_property_readonly,
)
from hw_tracking_selftest import _WriteAuditingBackend

DEVICE_NAME = "LX200 OnStep"
WRITE_WHITELIST = {"TELESCOPE_TRACK_MODE", "ON_COORD_SET", "EQUATORIAL_EOD_COORD"}
REST_FREQUENCY_HZ = 1_420_405_751.77


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _combine_write_audit(entries: list) -> dict:
    properties_written = sorted({e["property"] for e in entries})
    elements_written = sorted({name for e in entries for name, v in e["elements"].items() if v == "On"})
    unexpected = [e for e in entries if e["property"] not in WRITE_WHITELIST]
    return {"write_count": len(entries), "properties_written": properties_written,
            "elements_written": elements_written, "unexpected_write_count": len(unexpected),
            "unexpected_writes": unexpected, "writes": entries}


def _mode_from_switch_vector(vector):
    if not vector:
        return None
    on = [k for k, v in vector.items() if v == "On"]
    if len(on) != 1:
        return None
    return RealTrackingBackend.MODE_BY_ELEMENT.get(on[0])


def _check_no_other_active_session(session_root: str, own_session_id: str) -> Optional[str]:
    """Fail-closed session-ownership check: any sibling HI-* session
    directory whose state.json claims phase="running" AND whose recorded
    PID is still alive blocks this run. A stale (dead-PID) "running"
    marker does NOT block - matches capture_conflict.py's own stale-PID
    handling precedent."""
    import os
    root = Path(session_root)
    if not root.exists():
        return None
    for child in root.glob("HI-*"):
        if child.name == own_session_id or not child.is_dir():
            continue
        state_path = child / "state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if state.get("phase") != "running":
            continue
        pid = state.get("pid")
        if pid is None:
            return f"{child.name} has phase=running with no recorded pid - cannot confirm staleness, failing closed"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue  # stale - process is gone, does not block
        except PermissionError:
            return f"{child.name} phase=running, pid {pid} exists (owned by another user) - blocking"
        else:
            return f"{child.name} phase=running, pid {pid} is alive - another session may be active"
    return None


async def main(args) -> int:
    import os
    location = None
    host, port, timeout = args.host, args.port, args.timeout
    report: dict = {"test": "HI-NIGHT-SCAN", "backend": args.backend, "device": DEVICE_NAME,
                     "started_utc": _utcnow_iso()}
    session_id = f"HI-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    session = AlignmentSession(args.session_root, session_id)
    print(f"Session: {session.dir}")
    session.write_state({"phase": "running", "pid": os.getpid(), "started_utc": report["started_utc"]})

    gates: List[tuple] = []

    def gate(name: str, passed: bool, detail: str):
        gates.append((name, passed, detail))
        print(f"[PREFLIGHT] {name}: {'ok' if passed else 'FAIL'} ({detail})")

    # ================================================================ PREFLIGHT 1: SYSTEM
    conflict = capture_conflict.check_no_conflicting_capture(runtime_dir=args.orchestrator_runtime_dir)
    gate("no_capture_conflict", not conflict.conflict, conflict.detail)

    other_active = _check_no_other_active_session(args.session_root, session_id)
    gate("no_other_active_session", other_active is None, other_active or "ok")

    manifest_path = Path(args.manifest_path)
    fits_path = Path(args.fits_path)
    gate("reference_manifest_exists", manifest_path.exists(), str(manifest_path))
    gate("reference_fits_exists", fits_path.exists(), str(fits_path))

    manifest = None
    validation = None
    if manifest_path.exists() and fits_path.exists():
        try:
            manifest = ReferenceManifest.read(str(manifest_path))
            gate("reference_manifest_parses", True, f"survey={manifest.survey} trust={manifest.trust.value}")
        except Exception as exc:
            gate("reference_manifest_parses", False, f"{type(exc).__name__}: {exc}")
        if manifest is not None:
            validation = validate_reference(str(fits_path), manifest)
            gate("reference_checksum_and_structure_valid", validation.ok, validation.reason)
            gate("reference_trust_is_validated", manifest.trust == ReferenceTrust.REAL_VALIDATED,
                 f"trust={manifest.trust.value}")
            session.write_reference_validation({"manifest": manifest.to_dict(), "validation": validation.to_dict()})

    try:
        free_bytes = os.statvfs(args.session_root if Path(args.session_root).exists() else ".").f_bavail * \
                     os.statvfs(args.session_root if Path(args.session_root).exists() else ".").f_frsize
        gate("disk_space_sufficient", free_bytes > 1_000_000_000, f"{free_bytes / 1e9:.1f} GB free (need > 1 GB)")
    except OSError as exc:
        gate("disk_space_sufficient", False, f"{type(exc).__name__}: {exc}")

    try:
        test_file = Path(session.dir) / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        gate("output_writable", True, str(session.dir))
    except OSError as exc:
        gate("output_writable", False, f"{type(exc).__name__}: {exc}")

    if args.backend == "real":
        geo_raw = await read_property_readonly(host, port, DEVICE_NAME, "GEOGRAPHIC_COORD", timeout)
        geo = parse_number_vector(geo_raw)
        mode_raw = await read_property_readonly(host, port, DEVICE_NAME, "TELESCOPE_TRACK_MODE", timeout)
        mode_vec = parse_switch_vector(mode_raw)
        park_raw = await read_property_readonly(host, port, DEVICE_NAME, "TELESCOPE_PARK", timeout)
        park_vec = parse_switch_vector(park_raw)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(args.sdr_host, args.sdr_port), 3.0)
            writer.close()
            sdr_reachable = True
        except (OSError, asyncio.TimeoutError):
            sdr_reachable = False
    else:
        geo = {"LAT": -33.4331, "LONG": 289.3336, "ELEV": 550.0}
        mode_vec = {"TRACK_SIDEREAL": "On", "TRACK_SOLAR": "Off", "TRACK_LUNAR": "Off", "TRACK_CUSTOM": "Off"}
        park_vec = {"PARK": "Off", "UNPARK": "On"}
        sdr_reachable = True

    gate("mount_reachable", mode_vec is not None, f"TELESCOPE_TRACK_MODE={mode_vec}")
    gate("observer_location_valid", bool(geo and geo.get("LAT") is not None), f"GEOGRAPHIC_COORD={geo}")
    gate("mount_not_parked", bool(park_vec and park_vec.get("PARK") != "On"), f"TELESCOPE_PARK={park_vec}")
    gate("sdr_reachable", sdr_reachable, f"{args.sdr_host}:{args.sdr_port}")

    original_mode = _mode_from_switch_vector(mode_vec) if mode_vec else None
    gate("original_tracking_mode_known", original_mode is not None, f"{original_mode.value if original_mode else None}")

    report["gates_system"] = [{"name": n, "passed": p, "detail": d} for n, p, d in gates]
    failing = [(n, d) for n, p, d in gates if not p]
    if failing:
        reason = "; ".join(f"{n}: {d}" for n, d in failing)
        report["result"] = "BLOCKED"
        report["reason"] = reason
        session.write_hardware_test_result(report)
        session.write_state({"phase": "done", "result": "BLOCKED"})
        print(f"\nFIRST REAL HI NIGHT SCAN: BLOCKED\nReason: {reason}\nEvidence: {session.dir}")
        return 2

    if args.backend == "real" and not args.yes:
        print("\nRefusing to touch real hardware without --yes.", file=sys.stderr)
        session.write_state({"phase": "done", "result": "ABORTED_NO_YES"})
        return 3

    location = EarthLocation(lat=geo["LAT"] * u.deg, lon=geo["LONG"] * u.deg, height=(geo.get("ELEV") or 0.0) * u.m)

    # ================================================================ TARGET (recomputed, never hardcoded)
    if args.backend == "simulated" and args.simulate_obstime:
        _fixed_obstime = Time(args.simulate_obstime)
        obstime_provider = lambda: _fixed_obstime  # noqa: E731 - rehearsal only, never reachable for --backend real
    else:
        obstime_provider = Time.now
    provider = FITSMomentMapProvider(str(fits_path), manifest, require_validated=True)
    obstime_now = obstime_provider()
    center, selection = provider.choose_target(location, obstime_now, args.min_altitude, args.beam_fwhm_deg,
                                                 stride=args.pixel_catalog_stride)
    template = provider.template_for(center, args.beam_fwhm_deg, stride=args.pixel_catalog_stride)
    gal = center.galactic
    print(f"[TARGET] l={gal.l.deg:.3f} b={gal.b.deg:.3f} RA={center.ra.hour:.4f}h DEC={center.dec.deg:.3f} deg "
          f"(selection score={selection.get('score', 0):.2f})")
    session.write_target({"galactic_l_deg": float(gal.l.deg), "galactic_b_deg": float(gal.b.deg),
                           "icrs_ra_hours": float(center.ra.hour), "icrs_dec_deg": float(center.dec.deg),
                           "selection": selection, "obstime_utc": obstime_now.utc.isot})

    # ================================================================ PREFLIGHT 2: ASTRONOMICAL
    points = build_raster(args.raster_span_deg, args.raster_spacing_deg)
    positions = offset_coordinates(center, [p.east_deg for p in points], [p.north_deg for p in points])
    n_points = len(points)
    per_point_seconds = args.integration_seconds + args.settle_seconds + args.slew_estimate_seconds
    estimated_duration_s = n_points * per_point_seconds
    sample_times = obstime_now + np.linspace(0, estimated_duration_s, 5) * u.s

    astro_ok = True
    astro_reasons = []
    for i, position in enumerate(positions):
        altaz = position.transform_to(AltAz(obstime=sample_times, location=location))
        alt = altaz.alt.deg
        if np.any(alt < args.min_altitude) or np.any(alt > args.max_altitude):
            astro_ok = False
            astro_reasons.append(f"point {i} (east={points[i].east_deg},north={points[i].north_deg}): "
                                  f"altitude range [{alt.min():.1f},{alt.max():.1f}] outside "
                                  f"[{args.min_altitude},{args.max_altitude}]")
    gate("all_raster_points_visible_for_estimated_duration", astro_ok,
         "ok" if astro_ok else "; ".join(astro_reasons[:3]) + (f" (+{len(astro_reasons)-3} more)" if len(astro_reasons) > 3 else ""))
    print(f"[PREFLIGHT] estimated duration: {estimated_duration_s:.0f}s ({estimated_duration_s/60:.1f} min) "
          f"for {n_points} points")

    # ================================================================ PREFLIGHT 3: REFERENCE STRUCTURE
    expected_values = np.asarray(template(positions))
    finite_expected = expected_values[np.isfinite(expected_values)]
    structure_ok = len(finite_expected) >= args.min_valid_positions and float(np.std(finite_expected)) > 1e-6
    structure_detail = (f"valid={len(finite_expected)}/{n_points} std={float(np.std(finite_expected)) if len(finite_expected) else 0:.4f}")
    gate("sufficient_reference_structure", structure_ok, structure_detail)

    report["gates_astro_and_reference"] = [{"name": n, "passed": p, "detail": d} for n, p, d in gates
                                            if n in ("all_raster_points_visible_for_estimated_duration",
                                                     "sufficient_reference_structure")]
    still_failing = [(n, d) for n, p, d in gates if not p]
    if still_failing:
        reason = "; ".join(f"{n}: {d}" for n, d in still_failing)
        report["result"] = "BLOCKED"
        report["reason"] = reason
        session.write_hardware_test_result(report)
        session.write_state({"phase": "done", "result": "BLOCKED"})
        print(f"\nFIRST REAL HI NIGHT SCAN: BLOCKED\nReason: {reason}\nEvidence: {session.dir}")
        return 2

    session.write_expected_map({"points": [vars(p) for p in points],
                                 "expected_metric": [float(v) if np.isfinite(v) else None for v in expected_values]})

    # ================================================================ BACKEND CONSTRUCTION
    if args.backend == "real":
        tracking_backend = RealTrackingBackend(host=host, port=port, device_name=DEVICE_NAME,
                                                allow_real_writes=True, default_timeout=timeout)
        mount_adapter = RealMountAdapter(host=host, port=port, device=DEVICE_NAME)
        acquisition_backend = RealHIAcquisitionBackend(args.sdr_host, args.sdr_port, session)
    else:
        tracking_backend = SimulatedTrackingBackend(original_mode)
        mount_adapter = SimulatedMountAdapter()
        acquisition_backend = SimulatedHIAcquisitionBackend(
            expected_amplitude_by_index=[max(v, 0.0) if np.isfinite(v) else 0.0 for v in expected_values],
            gain_a=1.0, baseline_b=0.0, noise_std=0.1, seed=1)
    audited_tracking = _WriteAuditingBackend(tracking_backend)

    pipeline_config = SpectralPipelineConfig(line_window_km_s=tuple(args.velocity_window_km_s),
                                              baseline_exclusion_km_s=tuple(args.velocity_window_km_s))

    write_entries: list = []
    point_results: List[dict] = []
    cancelled = False
    step_error: Optional[str] = None
    ts_object = None

    await mount_adapter.connect()
    t_scan_start = time.monotonic()
    try:
        async with TrackingSession(audited_tracking, TrackingMode.SIDEREAL, session, timeout=args.timeout) as ts:
            ts_object = ts
            print(f"[TRACKING] mode SIDEREAL confirmed by readback (original was {original_mode.value})")
            if args.backend == "real":
                await acquisition_backend.prepare(center_frequency_hz=args.center_frequency_hz,
                                                   sample_rate_hz=args.sample_rate_hz, gain_db=args.gain_db)

            for i, (point, position) in enumerate(zip(points, positions)):
                altaz_now = position.transform_to(AltAz(obstime=obstime_provider(), location=location))
                alt_now = float(altaz_now.alt.deg)
                if not (args.min_altitude <= alt_now <= args.max_altitude):
                    point_results.append({"index": i, "valid": False, "metric": None,
                                           "reason": f"altitude {alt_now:.1f} deg outside limits at execution time"})
                    print(f"[POINT {i}] SKIPPED - altitude {alt_now:.1f} deg outside limits")
                    continue

                t_point_start = time.monotonic()
                write_entries.append({"property": "ON_COORD_SET", "elements": {"TRACK": "Off", "SLEW": "On", "SYNC": "Off"}})
                write_entries.append({"property": "EQUATORIAL_EOD_COORD",
                                       "elements": {"RA": str(float(position.ra.hour)), "DEC": str(float(position.dec.deg))}})
                goto_ok = await mount_adapter.goto(position, point_index=i)
                if not goto_ok:
                    point_results.append({"index": i, "valid": False, "metric": None, "reason": "GOTO failed/timed out"})
                    print(f"[POINT {i}] GOTO FAILED")
                    continue

                await asyncio.sleep(args.settle_seconds)

                try:
                    metric = await acquire_and_reduce_point(
                        acquisition_backend, position, center_frequency_hz=args.center_frequency_hz,
                        sample_rate_hz=args.sample_rate_hz, gain_db=args.gain_db,
                        integration_seconds=args.integration_seconds, pipeline_config=pipeline_config)
                except Exception as exc:
                    metric = None
                    point_results.append({"index": i, "valid": False, "metric": None,
                                           "reason": f"acquisition/processing failed: {type(exc).__name__}: {exc}"})
                    print(f"[POINT {i}] ACQUISITION FAILED: {exc}")
                    continue

                point_dt = time.monotonic() - t_point_start
                valid = metric is not None
                point_results.append({"index": i, "valid": valid, "metric": metric, "reason": "ok" if valid else
                                       "insufficient valid channels / excessive RFI (see spectral pipeline result)",
                                       "duration_s": point_dt, "timestamp_utc": _utcnow_iso(),
                                       "ra_hours": float(position.ra.hour), "dec_deg": float(position.dec.deg)})
                print(f"[POINT {i}/{n_points-1}] valid={valid} metric={metric} ({point_dt:.1f}s)")
        ts_restored_mode = ts.restored_mode
    except asyncio.CancelledError as exc:
        cancelled = True
        step_error = f"CancelledError: {exc!r}"
    except KeyboardInterrupt as exc:
        cancelled = True
        step_error = f"KeyboardInterrupt: {exc!r}"
    except Exception as exc:
        step_error = f"{type(exc).__name__}: {exc}"
        print(f"[ERROR] {step_error}", file=sys.stderr)
        ts_restored_mode = None
    else:
        pass
    finally:
        if args.backend == "real":
            await mount_adapter.disconnect()
            await acquisition_backend.close()
        scan_duration_s = time.monotonic() - t_scan_start

    track_mode_restored = bool(getattr(ts_object, "restored_mode", None))
    write_entries.extend(audited_tracking.audit()["writes"])
    write_audit = _combine_write_audit(write_entries)
    session.write_write_audit(write_audit)

    values = [pr["metric"] if pr["valid"] else None for pr in point_results]
    # Pad values/point_results for any points skipped entirely by an early
    # cancellation - fit_raster and quality checks must see the SAME
    # length as `points`, with unattempted points recorded as invalid.
    while len(values) < n_points:
        idx = len(values)
        point_results.append({"index": idx, "valid": False, "metric": None, "reason": "not attempted (scan ended early)"})
        values.append(None)

    session.write_raw_grid({"points": [vars(p) for p in points], "values": values, "point_results": point_results})
    session.write_observed_map({"points": [vars(p) for p in points], "observed_metric": values})

    # ================================================================ FIT + QUALITY (mandatory bootstrap)
    coverage = spatial_sampling_quality(points, values, args.raster_span_deg)
    fit = None
    quality = None
    if coverage.sufficient:
        fit = fit_raster(positions, values, center, template, span_deg=args.raster_span_deg)
        quality = evaluate_quality_v2(points, values, positions, center, template, fit, args.raster_span_deg,
                                       run_bootstrap=True, bootstrap_iterations=args.bootstrap_iterations)
        model_values = [float(v) for v in np.asarray(template(offset_coordinates(
            center, [p.east_deg + fit.estimate.offset_ra_deg for p in points],
            [p.north_deg + fit.estimate.offset_dec_deg for p in points])))]
        residuals = [(o - m) if (o is not None) else None for o, m in zip(values, model_values)]
        session.write_model_map({"points": [vars(p) for p in points], "model_metric": model_values,
                                  "note": "beam-convolved reference model evaluated at best-fit offset - "
                                          "an INTERPOLATED PREVIEW at each raster point, not a second observation"})
        session.write_residual_map({"points": [vars(p) for p in points], "residual": residuals})
        session.write_fit_result(fit.to_dict())
        session.write_quality(quality.to_dict())
        if quality.bootstrap:
            session.write_bootstrap(quality.bootstrap.to_dict())

    # ================================================================ SYNC POLICY (always blocked)
    phase_gate = evaluate_phase_gate(AlignmentPhase.FIRST_LIGHT_HI, "N/A (FIRST_LIGHT_HI never evaluates eligibility)")
    print(f"\nSYNC ELIGIBILITY:\nBLOCKED BY FIRST_LIGHT_HI POLICY\nReason: {phase_gate.reason}")

    # ================================================================ VERDICT
    valid_count = sum(1 for v in values if v is not None)
    if cancelled:
        observation_verdict = "CANCELLED"
    elif step_error is not None:
        observation_verdict = "FAIL"
    elif valid_count >= args.min_valid_positions:
        observation_verdict = "PASS"
    elif valid_count > 0:
        observation_verdict = "PARTIAL"
    else:
        observation_verdict = "FAIL"

    hi_pattern_detected = "INCONCLUSIVE"
    pointing_solution = "INCONCLUSIVE"
    if quality is not None and fit is not None:
        from alignment_engine.hi.quality_v2 import null_model_improvement
        improvement = null_model_improvement(fit)
        hi_pattern_detected = "YES" if improvement > 0.5 else ("NO" if improvement < 0.1 else "INCONCLUSIVE")
        pointing_solution = quality.verdict  # GOOD | MARGINAL | BAD

    report.update({
        "session_id": session_id, "target": {"galactic_l_deg": float(gal.l.deg), "galactic_b_deg": float(gal.b.deg),
                                              "icrs_ra_hours": float(center.ra.hour), "icrs_dec_deg": float(center.dec.deg)},
        "original_tracking_mode": original_mode.value, "tracking_restored": track_mode_restored,
        "n_points": n_points, "valid_count": valid_count, "cancelled": cancelled, "step_error": step_error,
        "scan_duration_s": scan_duration_s, "write_audit": write_audit,
        "fit": fit.to_dict() if fit else None, "quality": quality.to_dict() if quality else None,
        "sync_eligibility": phase_gate.to_dict(),
        "observation_verdict": observation_verdict, "hi_pattern_detected": hi_pattern_detected,
        "pointing_solution": pointing_solution,
        "measured_offset_east_deg": fit.estimate.offset_ra_deg if fit else None,
        "measured_offset_north_deg": fit.estimate.offset_dec_deg if fit else None,
        "finished_utc": _utcnow_iso(),
    })
    session.write_hardware_test_result(report)
    session.write_state({"phase": "done", "result": observation_verdict})

    print("\n" + "=" * 60)
    print(f"OBSERVATION: {observation_verdict}")
    print(f"HI PATTERN DETECTED: {hi_pattern_detected}")
    print(f"POINTING SOLUTION: {pointing_solution}")
    if fit:
        print(f"Measured offset: East={fit.estimate.offset_ra_deg:+.3f} deg North={fit.estimate.offset_dec_deg:+.3f} deg")
    print("SYNC: BLOCKED BY FIRST_LIGHT_HI")
    print(f"Evidence: {session.dir}")
    print("=" * 60)

    if cancelled:
        return 5
    if step_error is not None or observation_verdict == "FAIL":
        return 1
    if observation_verdict == "PARTIAL":
        return 4
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["simulated", "real"], default="simulated")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7624)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--sdr-host", default="localhost")
    parser.add_argument("--sdr-port", type=int, default=1234)
    parser.add_argument("--fits-path", default="data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.fits")
    parser.add_argument("--manifest-path", default="data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.manifest.json")
    parser.add_argument("--min-altitude", type=float, default=HIScanConfig().min_altitude_deg)
    parser.add_argument("--max-altitude", type=float, default=90.0)
    parser.add_argument("--raster-span-deg", type=float, default=HIScanConfig().raster_span_deg)
    parser.add_argument("--raster-spacing-deg", type=float, default=HIScanConfig().raster_spacing_deg)
    parser.add_argument("--integration-seconds", type=float, default=HIScanConfig().integration_seconds)
    parser.add_argument("--settle-seconds", type=float, default=HIScanConfig().settle_seconds)
    parser.add_argument("--slew-estimate-seconds", type=float, default=10.0)
    parser.add_argument("--beam-fwhm-deg", type=float, default=20.0)
    parser.add_argument("--velocity-window-km-s", type=float, nargs=2, default=[-100.0, 100.0])
    parser.add_argument("--center-frequency-hz", type=float, default=HIScanConfig().center_frequency_hz)
    parser.add_argument("--sample-rate-hz", type=float, default=HIScanConfig().sample_rate_hz)
    parser.add_argument("--gain-db", type=float, default=HIScanConfig().gain_db)
    parser.add_argument("--min-valid-positions", type=int, default=HIScanConfig().minimum_valid_positions)
    parser.add_argument("--bootstrap-iterations", type=int, default=20)
    parser.add_argument("--pixel-catalog-stride", type=int, default=10,
                         help="coarser=faster fits (matters a lot for bootstrap); the beam FWHM is "
                              "vastly larger than the survey's pixel scale, so this costs negligible accuracy")
    parser.add_argument("--session-root", default="data/alignment")
    parser.add_argument("--orchestrator-runtime-dir", default=None)
    parser.add_argument("--simulate-obstime", default=None,
                         help="ISO time override for --backend simulated rehearsals ONLY - "
                              "ignored (never applied) for --backend real")
    return parser


if __name__ == "__main__":
    parsed = build_parser().parse_args()
    raise SystemExit(asyncio.run(main(parsed)))
