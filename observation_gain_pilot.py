#!/usr/bin/env python3
"""Optional gain-pilot stage for OBSERVE's grid flow: a real MAIN-SDR check/adjustment of gain BEFORE the
grid's first point, plus an optional real stability check after the grid finishes. Every artifact this writes
lives inside the SAME grid session directory as observation_resolved.json/mosaic.csv
(resolved_plan['grid_session_dir']) - linked to that grid, not a separate concern.

Real chain reused verbatim, nothing reimplemented:
    - GOTO + capture: INDITelescopeControl / sdr_capture.SDRCapture (the SAME classes alignment.py uses for its
      own per-position acquire loop - this module's _capture_at() mirrors that pattern).
    - analysis: calibration_engine.sample_statistics / clipping / bandpass (the SAME real primitives
      calibration_operational_realtest.py and the CALIBRATE physical-reference wizard use).
    - candidate gain values: calibration_engine.gain_sweep.CANDIDATE_GAIN_TABLE_DB (the real, published R820T2/
      R828D discrete gain table - never an arbitrary float).
    - per-point GOTO+settle+readback timing: alignment.MEASURED_ACQUIRE_OVERHEAD_PER_POINT_S (the same real,
      measured constant ALIGN's own web duration estimate and temporal-altitude check use).
    - pilot point ORIENTATION only: hi4pi_map's real HI4PI column-density map (NOT the synthetic catalog) - its
      brightness does NOT predict total ADC power (RFI, continuum, the LNA's own response and the local RF
      environment all matter too); the actual gain decision always comes from the REAL measured pilot
      capture(s), never from the map. hi4pi_map.py itself is not modified by this file.

rtl_tcp has no gain readback (calibration_engine.hardware_inspection's own documented audit finding) - "the
effective gain" can only be confirmed by re-measuring after a change, which is exactly what verify-gain does.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from runtime_state import atomic_write_json, read_json_safe

STATE_FILENAME = "gain_pilot_state.json"
PILOT_ORDER = ("HIGH", "LOW")


# ------------------------------------------------------------------ pure config / dataclasses
@dataclass
class PilotConfig:
    enabled: bool = True
    max_pilots: int = 2                     # 1 = HIGH only ever attempted; 2 = HIGH, then LOW if HIGH's margin is thin
    capture_seconds: float = 2.0
    settle_seconds: float = 1.0
    headroom_comfortable_multiplier: float = 1.5    # same spirit as gain_sweep.recommend_operational_gain's own default
    clipping_rail_hit_fraction: float = 1.0e-4
    rfi_min_usable_band_fraction: float = 0.5
    gain_db_suggested: float = 40.2
    final_check_enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "max_pilots": self.max_pilots, "capture_seconds": self.capture_seconds,
                "settle_seconds": self.settle_seconds, "headroom_comfortable_multiplier": self.headroom_comfortable_multiplier,
                "clipping_rail_hit_fraction": self.clipping_rail_hit_fraction,
                "rfi_min_usable_band_fraction": self.rfi_min_usable_band_fraction,
                "gain_db_suggested": self.gain_db_suggested, "final_check_enabled": self.final_check_enabled}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PilotConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _state_path(session_dir: Path) -> Path:
    return session_dir / STATE_FILENAME


def _load_state(session_dir: Path) -> Dict[str, Any]:
    state = read_json_safe(_state_path(session_dir))
    if state is None:
        raise SystemExit(f"no {STATE_FILENAME} in {session_dir} - plan first")
    return state


def _save_state(session_dir: Path, state: Dict[str, Any]) -> None:
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(_state_path(session_dir), state)


def _emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


# ------------------------------------------------------------------ resolved plan / grid point access (read-only, real files PLAN already wrote)
def _load_resolved_plan(resolved_plan_path: Path) -> Dict[str, Any]:
    import observation_plan
    plan = json.loads(resolved_plan_path.read_text())
    recomputed = observation_plan.recompute_config_hash(plan)
    if recomputed != plan.get("observation_config_sha256"):
        raise SystemExit("observation_resolved.json failed its tamper/corruption check - PLAN the grid again")
    return plan


def _read_mosaic_points(mosaic_csv_path: Path) -> Dict[int, Dict[str, float]]:
    points: Dict[int, Dict[str, float]] = {}
    with open(mosaic_csv_path, newline="") as f:
        for row in csv.DictReader(f):
            points[int(row["point_id"])] = {"ra_hours": float(row["target_ra_hours"]), "dec_deg": float(row["target_dec_degrees"])}
    return points


def choose_pilot_candidates(resolved_plan: Dict[str, Any], beam_fwhm_deg: float, max_pilots: int,
                            min_elevation_deg: float, hold_seconds: float) -> Dict[str, Any]:
    """HIGH = the grid point with the greatest real HI4PI N_HI within the grid footprint; LOW = the least. Picked
    only to ORIENT which points to actually measure - see this module's own docstring for why brightness here
    does not predict SDR/ADC power. Falls back to an honest 'no HI orientation' note (geometric centre + a
    corner, never invented) if the real map is unavailable - never the synthetic catalog.

    ALTITUDE: target_list's own predicted_altitude_deg describes each point's altitude AT ITS OWN SCHEDULED
    OFFSET WITHIN THE EVENTUAL GRID RUN (t0 + scan_order*per_point_seconds from whenever the grid was last
    planned) - NOT the altitude the pilot itself would see, since the pilot runs BEFORE the grid, at
    essentially real "now" (which can also be much later than t0 if real time elapsed since PLAN without a
    re-plan - found live: a real session's displayed ~27 deg/~17 deg candidates were actually at ~20 deg/~12
    deg by the time anyone looked, real time having passed since the grid was planned). This function
    therefore computes a FRESH real-time altitude for every candidate it considers
    (calibration_engine.hi_reference_selection.point_stays_above_elevation, reused - not reimplemented) and
    only offers a candidate that clears min_elevation_deg for the whole hold_seconds a real pilot capture
    would occupy it - walking the N_HI ranking until one clears, never just taking the top/bottom value
    unconditionally. min_elevation_deg should be the SAME floor the grid PLAN itself used
    (requested.grid.min_altitude_deg) - not a new, separately-configured number."""
    from calibration_engine.hi_reference_selection import point_stays_above_elevation
    mosaic_path = Path(resolved_plan["mosaic_csv_path"])
    points = _read_mosaic_points(mosaic_path)
    target_list = {int(t["point_id"]): t for t in resolved_plan["target_list"]}
    ids = sorted(points)
    ra_deg = np.array([points[i]["ra_hours"] * 15.0 for i in ids])
    dec_deg = np.array([points[i]["dec_deg"] for i in ids])

    try:
        import hi4pi_map
        data, wcs, pix_scale_deg = hi4pi_map._load()
        smoothed = hi4pi_map._smoothed(beam_fwhm_deg)
        x_pix, y_pix = wcs.all_world2pix(ra_deg, dec_deg, 0)
        ny, nx = smoothed.shape
        xi = np.mod(np.round(x_pix).astype(int), nx)
        yi = np.round(y_pix).astype(int)
        valid = (yi >= 0) & (yi < ny)
        values = np.full(ra_deg.shape, np.nan)
        values[valid] = smoothed[yi[valid], xi[valid]]
        finite = np.isfinite(values)
        if not finite.any():
            raise hi4pi_map.HI4PIUnavailable("no grid point mapped to a finite value in the real HI4PI raster")
        order = np.argsort(np.where(finite, values, -np.inf))[::-1]   # descending, NaNs last (HIGH-first order)
        source = hi4pi_map.SOURCE
        note = ("pilot points chosen by real HI4PI N_HI contrast within the grid footprint (orientation only - "
               "see note: N_HI brightness does not predict SDR/ADC power), filtered to real-time-altitude-safe "
               "points only")
    except Exception as exc:  # noqa: BLE001 - HI4PIUnavailable or any real read failure: fall back honestly, never silently substitute
        order = np.arange(len(ids))   # geometric fallback: scan order (HIGH searches from the front, LOW from the back)
        values = None
        source = None
        note = (f"real HI4PI map unavailable ({type(exc).__name__}: {exc}) - falling back to the grid's scan order "
               "(no HI orientation); the synthetic catalog is NOT used as a substitute; still filtered to "
               "real-time-altitude-safe points only")

    def _row(idx: int, label: str, check: Dict[str, Any]) -> Dict[str, Any]:
        pid = ids[idx]
        t = target_list.get(pid, {})
        row = {"label": label, "point_id": pid, "ra_hours": points[pid]["ra_hours"], "dec_deg": points[pid]["dec_deg"],
              "predicted_altitude_deg": t.get("predicted_altitude_deg"), "scan_order": t.get("scan_order"),
              "real_time_altitude_check": check}
        if values is not None and np.isfinite(values[idx]):
            row["hi4pi_n_hi_1e20cm2"] = float(values[idx])
        return row

    def _find_safe(order_indices, exclude=None) -> Tuple[Optional[int], Optional[Dict[str, Any]], int]:
        exclude = exclude or set()
        rejected = 0
        for raw_idx in order_indices:
            idx = int(raw_idx)
            if idx in exclude:
                continue
            check = point_stays_above_elevation(float(ra_deg[idx] / 15.0), float(dec_deg[idx]), min_elevation_deg, hold_seconds)
            if check["clears"]:
                return idx, check, rejected
            rejected += 1
        return None, None, rejected

    high_idx, high_check, high_rejected = _find_safe(order)
    if high_idx is None:
        raise SystemExit(f"no grid point (of {len(ids)} considered) currently clears min_elevation_deg="
                         f"{min_elevation_deg:g} deg for a {hold_seconds:g} s hold, checked in real time just now - "
                         f"cannot propose a HIGH pilot point right now ({high_rejected} candidate(s) rejected on "
                         "altitude); the grid's own displayed predicted_altitude_deg describes a different, "
                         "scheduled-for-later moment and must not be used to judge this")
    candidates = {"HIGH": _row(high_idx, "HIGH", high_check)}
    low_note = ""
    if max_pilots >= 2:
        low_idx, low_check, low_rejected = _find_safe(order[::-1], exclude={high_idx})
        if low_idx is not None:
            candidates["LOW"] = _row(low_idx, "LOW", low_check)
        else:
            low_note = f" LOW omitted: no other grid point currently clears the same real-time altitude floor ({low_rejected} rejected)."
    return {"candidates": candidates, "note": note + low_note, "catalog_source": source,
           "grid_points_considered": len(ids), "min_elevation_deg": min_elevation_deg, "hold_seconds": hold_seconds}


# ------------------------------------------------------------------ real capture + analysis (reused primitives)
async def _capture_at(host: str, port: int, sdr_host: str, sdr_port: int, ra_hours: float, dec_deg: float,
                      gain_db: float, capture_seconds: float, settle_seconds: float, sample_rate_hz: float,
                      center_frequency_hz: float, output_path: Path) -> Dict[str, Any]:
    """Real GOTO + real MAIN capture at one point - mirrors alignment.AlignmentRunner's own connect/goto/capture
    pattern (same classes, not reimplemented). Connects and disconnects fresh each call: pilot captures are a
    handful of discrete, infrequent events, not a tight per-point loop, so the small reconnect overhead is an
    acceptable, simpler trade for never holding INDI/SDR open across separate CLI invocations."""
    from indi_telescope_control import INDITelescopeControl
    from sdr_capture import SDRCapture
    telescope = INDITelescopeControl(host, port, "LX200 OnStep", False)
    sdr = SDRCapture("network", sdr_host, sdr_port, verbose=False)
    try:
        if not await telescope.connect():
            raise RuntimeError("INDI connection failed")
        await sdr.connect()
        await sdr.configure(int(center_frequency_hz), int(sample_rate_hz), gain=gain_db)
        if not await telescope.goto(ra_hours, dec_deg):
            raise RuntimeError("GOTO failed")
        await asyncio.sleep(settle_seconds)
        mount_ra, mount_dec = await telescope.get_coordinates(force_refresh=True)
        await sdr.capture(capture_seconds, str(output_path), int(sample_rate_hz),
                          {"gain": gain_db, "purpose": "observe_gain_pilot", "target_ra_hours": ra_hours, "target_dec_deg": dec_deg})
        return {"commanded_ra_hours": ra_hours, "commanded_dec_deg": dec_deg, "mount_ra_hours": mount_ra, "mount_dec_deg": mount_dec,
               "gain_db": gain_db, "capture_seconds": capture_seconds, "sample_rate_hz": sample_rate_hz,
               "center_frequency_hz": center_frequency_hz, "captured_utc": datetime.now(timezone.utc).isoformat(),
               "hdf5_path": str(output_path)}
    finally:
        await sdr.close()
        await telescope.disconnect()


def evaluate_capture(hdf5_path: Path, sample_rate_hz: float, center_frequency_hz: float, config: PilotConfig) -> Dict[str, Any]:
    """Same real analysis calibration_operational_realtest.py/the CALIBRATE wizard use, applied to one pilot
    capture: clipping, ADC-range occupancy (percentile headroom), relative digital power, and the usable-band
    fraction this project already uses as its RFI-contamination proxy (never a direct RFI power measurement)."""
    from calibration_engine.acquisition import read_capture_iq
    from calibration_engine.bandpass import compute_bandpass
    from calibration_engine.clipping import ClippingThresholds, evaluate_clipping
    from calibration_engine.sample_statistics import compute_sample_statistics
    iq, _ = read_capture_iq(str(hdf5_path))
    stats = compute_sample_statistics(iq)
    clipping = evaluate_clipping(stats, ClippingThresholds(clipped_rail_hit_fraction=config.clipping_rail_hit_fraction))
    bandpass = compute_bandpass(iq, sample_rate_hz, center_frequency_hz)
    relative_power = (stats.std_i ** 2 + stats.std_q ** 2) / 2.0
    rfi_flag = bandpass.usable_band_fraction < config.rfi_min_usable_band_fraction
    return {"sample_statistics": stats.to_dict(), "clipping": clipping.to_dict(), "usable_band_fraction": bandpass.usable_band_fraction,
           "relative_digital_power": relative_power, "rfi_flag": rfi_flag,
           "rfi_note": f"usable_band_fraction below {config.rfi_min_usable_band_fraction:g} (bandpass-coverage proxy, NOT a direct RFI power measurement)" if rfi_flag else None}


def needs_low_pilot(high_eval: Dict[str, Any], config: PilotConfig) -> Dict[str, Any]:
    """The 'is one high-power point enough' criterion: if the HIGH point already shows a comfortable clipping
    margin, the grid is very unlikely to clip anywhere weaker, so ONE point suffices; if it's thin/clipping, a
    LOW point is measured too so the gain choice isn't derated off a single, possibly-anomalous reading."""
    clip = high_eval["clipping"]
    threshold = clip["thresholds"]["warning_percentile_margin_codes"]
    margin = clip.get("percentile_margin_codes")
    comfortable = clip["status"] == "OK" and margin is not None and margin >= threshold * config.headroom_comfortable_multiplier
    if comfortable:
        return {"need_low": False, "reason": f"HIGH point clipping={clip['status']}, headroom margin {margin:.1f} codes >= "
                                             f"{threshold * config.headroom_comfortable_multiplier:.1f} codes ({config.headroom_comfortable_multiplier:g}x the "
                                             "warning threshold) - comfortable enough that a single high-power pilot point is sufficient"}
    return {"need_low": True, "reason": f"HIGH point clipping={clip['status']}, headroom margin "
                                        f"{'unknown' if margin is None else f'{margin:.1f} codes'} is thin or already clipping - measuring a LOW-power "
                                        "point too, to see the real dynamic range before choosing a gain"}


def recommend_gain(evals: Dict[str, Dict[str, Any]], current_gain_db: float, config: PilotConfig) -> Dict[str, Any]:
    """Picks a REAL admitted gain value (calibration_engine.gain_sweep's own published R820T2/R828D table) from
    the measured pilot(s) - the worst (least headroom) of whatever was actually measured decides. Never proposes
    an arbitrary float; a single fixed gain is proposed for the WHOLE grid (never per-point)."""
    from calibration_engine.gain_sweep import CANDIDATE_GAIN_TABLE_DB
    table = np.asarray(CANDIDATE_GAIN_TABLE_DB, dtype=float)
    worst_label, worst_eval = min(evals.items(), key=lambda kv: (kv[1]["clipping"]["percentile_margin_codes"] if kv[1]["clipping"]["percentile_margin_codes"] is not None else -1e9))
    clip = worst_eval["clipping"]
    threshold = clip["thresholds"]["warning_percentile_margin_codes"]
    margin = clip.get("percentile_margin_codes")
    current_idx = int(np.argmin(np.abs(table - current_gain_db)))
    if clip["status"] == "CLIPPED" or (margin is not None and margin < threshold):
        # Step down to the next lower admitted value - one step first (evidence-based, not a blind jump to minimum).
        new_idx = max(0, current_idx - 1)
        reason = f"{worst_label} point clipping={clip['status']} (margin {margin if margin is not None else 'n/a'} codes < {threshold:g}) - reducing gain one admitted step"
    elif margin is not None and margin >= threshold * config.headroom_comfortable_multiplier * 1.5 and current_idx < len(table) - 1:
        # Comfortably far from clipping even beyond the "comfortable" bar - allow one step UP for better sensitivity, still evidence-based.
        new_idx = min(len(table) - 1, current_idx + 1)
        reason = f"{worst_label} point headroom margin {margin:.1f} codes is well beyond comfortable - one admitted step up is still safe"
    else:
        new_idx = current_idx
        reason = f"{worst_label} point headroom margin {'n/a' if margin is None else f'{margin:.1f} codes'} is within the comfortable-but-not-excessive range - keeping the current gain"
    return {"recommended_gain_db": float(table[new_idx]), "current_gain_db": current_gain_db, "changed": new_idx != current_idx,
           "worst_pilot": worst_label, "reason": reason, "candidate_table_size": len(table)}


# ------------------------------------------------------------------ CLI subcommands (stateful, one action per web click)
def cmd_plan(args) -> int:
    resolved_plan_path = Path(args.resolved_plan_path).resolve()
    resolved_plan = _load_resolved_plan(resolved_plan_path)
    if resolved_plan.get("visibility") == "BLOCK":
        raise SystemExit("the grid PLAN itself is BLOCKed (some point falls below its own planning floor during "
                         "the run) - PLAN the grid again before planning a gain pilot against it")
    session_dir = Path(resolved_plan["grid_session_dir"]).resolve()
    config = PilotConfig(max_pilots=args.max_pilots, capture_seconds=args.capture_seconds, settle_seconds=args.settle_seconds,
                         headroom_comfortable_multiplier=args.headroom_multiplier, clipping_rail_hit_fraction=args.clipping_threshold,
                         rfi_min_usable_band_fraction=args.rfi_threshold, gain_db_suggested=args.gain_db, final_check_enabled=args.final_check)
    beam_fwhm_deg = args.beam_fwhm if args.beam_fwhm else resolved_plan["resolved"]["spacing_deg"]
    # The SAME floor the grid PLAN itself used (requested.grid.min_altitude_deg) - never a separately invented
    # number - and a real, right-now altitude check for the actual time a pilot capture would occupy the point
    # (settle + capture, not the grid's own per-point scan timing).
    min_elevation_deg = float(resolved_plan["requested"]["grid"]["min_altitude_deg"])
    hold_seconds = config.settle_seconds + config.capture_seconds
    picked = choose_pilot_candidates(resolved_plan, beam_fwhm_deg, config.max_pilots, min_elevation_deg, hold_seconds)
    n_extra_points = len(picked["candidates"])
    overhead_per_point_s = _acquire_overhead_s()
    est_capture_s = n_extra_points * (overhead_per_point_s + config.settle_seconds + config.capture_seconds)
    state = {
        "schema_version": 1, "resolved_plan_path": str(resolved_plan_path), "grid_session_dir": str(session_dir),
        "grid_config_hash": resolved_plan["observation_config_sha256"], "observation_name": resolved_plan["observation_name"],
        "config": config.to_dict(), "step": "PREPARE_HIGH", "candidates": picked["candidates"], "candidate_note": picked["note"],
        "catalog_source": picked["catalog_source"], "grid_points_considered": picked["grid_points_considered"],
        "min_elevation_deg": min_elevation_deg,
        "initial_gain_db": resolved_plan["main"]["gain_db"], "captures": {}, "evaluations": {}, "need_low": None,
        "gain_recommendation": None, "approved_gain_db": None, "verified_gain_db": None, "final_check": None,
        "estimated_duration_s": est_capture_s, "estimated_extra_points": n_extra_points,
        "duration_source": f"per-point overhead {overhead_per_point_s:g} s from alignment.MEASURED_ACQUIRE_OVERHEAD_PER_POINT_S "
                           f"(the same real, measured GOTO+settle+readback constant ALIGN uses) + settle {config.settle_seconds:g} s "
                           f"+ capture {config.capture_seconds:g} s per pilot point (up to {config.max_pilots} points, "
                           "fewer if the HIGH point alone turns out comfortable)",
        "created_utc": datetime.now(timezone.utc).isoformat(), "aborted": False,
    }
    session_dir.mkdir(parents=True, exist_ok=True)
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def _acquire_overhead_s() -> float:
    import alignment
    return alignment.MEASURED_ACQUIRE_OVERHEAD_PER_POINT_S


def _require_step(state: Dict[str, Any], *expected: str) -> None:
    if state["step"] not in expected:
        raise SystemExit(f"gain pilot is at step {state['step']}, not {expected} - refusing")


def _check_grid_still_matches(state: Dict[str, Any]) -> None:
    resolved_plan = _load_resolved_plan(Path(state["resolved_plan_path"]))
    if resolved_plan["observation_config_sha256"] != state["grid_config_hash"]:
        raise SystemExit("the grid PLAN changed since this gain-pilot PLAN was made (observation_config_sha256 "
                         "differs) - PLAN the gain pilot again")


async def _simulate_capture_at(ra_hours: float, dec_deg: float, gain_db: float, capture_seconds: float,
                               sample_rate_hz: float, center_frequency_hz: float, output_path: Path, scenario: str) -> Dict[str, Any]:
    """Offline-testing only, real hardware never touched: writes a real HDF5 file (same iq_data/attrs shape a
    real capture has) via calibration_engine.simulation, so evaluate_capture() and the whole state machine can
    be exercised end-to-end without INDI/SDR - never used for a real pilot measurement."""
    from calibration_engine.simulation import NAMED_SCENARIOS, flat_noisy_config
    import h5py
    cfg = NAMED_SCENARIOS.get(scenario, flat_noisy_config)(seed=abs(hash((ra_hours, dec_deg, gain_db))) % 10_000)
    from calibration_engine.simulation import simulate_capture_iq
    iq = simulate_capture_iq(cfg)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(output_path, "w") as handle:
        handle.create_dataset("iq_data", data=iq, compression="gzip", compression_opts=4)
        handle.attrs.update(capture_status="success", simulated=True, simulated_scenario=scenario,
                            center_frequency_hz=center_frequency_hz, sample_rate_hz=sample_rate_hz,
                            gain_requested_db=gain_db, duration_seconds=capture_seconds,
                            created_at=datetime.now(timezone.utc).isoformat())
    return {"commanded_ra_hours": ra_hours, "commanded_dec_deg": dec_deg, "mount_ra_hours": ra_hours, "mount_dec_deg": dec_deg,
           "gain_db": gain_db, "capture_seconds": capture_seconds, "sample_rate_hz": sample_rate_hz,
           "center_frequency_hz": center_frequency_hz, "captured_utc": datetime.now(timezone.utc).isoformat(),
           "hdf5_path": str(output_path), "simulated": True, "simulated_scenario": scenario}


async def _do_capture(state: Dict[str, Any], label: str, gain_db: float, simulate: Optional[str] = None) -> Dict[str, Any]:
    resolved_plan = _load_resolved_plan(Path(state["resolved_plan_path"]))
    cand = state["candidates"][label]
    session_dir = Path(state["grid_session_dir"])
    captures_dir = session_dir / "gain_pilot_captures"
    captures_dir.mkdir(exist_ok=True)
    if not simulate:
        # Real GOTO ahead - re-check the point's altitude RIGHT NOW, never trust the candidate's own
        # predicted_altitude_deg (that describes the grid's own scheduled-for-later moment, or simply how
        # things looked whenever PLAN GAIN PILOT last ran - either can be stale by the time an operator
        # actually clicks a capture button). min_elevation_deg falls back to a fresh read of the grid's own
        # floor for a session planned before this field existed.
        from calibration_engine.hi_reference_selection import point_stays_above_elevation
        min_elevation_deg = state.get("min_elevation_deg")
        if min_elevation_deg is None:
            min_elevation_deg = float(resolved_plan["requested"]["grid"]["min_altitude_deg"])
        hold_seconds = state["config"]["settle_seconds"] + state["config"]["capture_seconds"]
        fresh = point_stays_above_elevation(cand["ra_hours"], cand["dec_deg"], min_elevation_deg, hold_seconds)
        if not fresh["clears"]:
            raise SystemExit(f"{label} no longer clears min_elevation_deg={min_elevation_deg:g} deg, checked in "
                             f"real time right now (worst-case altitude {fresh['worst_case_altitude_deg']:.1f} deg, "
                             f"margin {fresh['margin_deg']:+.1f} deg over a {hold_seconds:g} s hold) - refusing "
                             f"the real GOTO; PLAN the gain pilot again ({fresh})")
    idx = sum(1 for k in state["captures"] if k.startswith(label))
    path = captures_dir / f"{label.lower()}_{idx:02d}.h5"
    if simulate:
        record = await _simulate_capture_at(ra_hours=cand["ra_hours"], dec_deg=cand["dec_deg"], gain_db=gain_db,
                                            capture_seconds=state["config"]["capture_seconds"],
                                            sample_rate_hz=resolved_plan["main"]["sample_rate"],
                                            center_frequency_hz=resolved_plan["main"]["center_frequency_hz"],
                                            output_path=path, scenario=simulate)
    else:
        record = await _capture_at(host="localhost", port=7624, sdr_host="localhost", sdr_port=1234,
                                   ra_hours=cand["ra_hours"], dec_deg=cand["dec_deg"], gain_db=gain_db,
                                   capture_seconds=state["config"]["capture_seconds"], settle_seconds=state["config"]["settle_seconds"],
                                   sample_rate_hz=resolved_plan["main"]["sample_rate"], center_frequency_hz=resolved_plan["main"]["center_frequency_hz"],
                                   output_path=path)
    config = PilotConfig.from_dict(state["config"])
    evaluation = evaluate_capture(path, resolved_plan["main"]["sample_rate"], resolved_plan["main"]["center_frequency_hz"], config)
    # Recorded on the evaluation itself (not just in the capture record) so any later comparison - especially the
    # final stability check - can require an EXACT match on every condition that matters (point, gain, frequency,
    # sample rate, duration) without having to cross-reference a separate capture entry: this is exactly what
    # the capture that produced this evaluation actually used, not an assumption that "nothing changed".
    evaluation["gain_db"] = gain_db
    evaluation["capture_seconds"] = state["config"]["capture_seconds"]
    evaluation["sample_rate_hz"] = resolved_plan["main"]["sample_rate"]
    evaluation["center_frequency_hz"] = resolved_plan["main"]["center_frequency_hz"]
    evaluation["ra_hours"] = cand["ra_hours"]
    evaluation["dec_deg"] = cand["dec_deg"]
    evaluation["point_label"] = label
    return {"capture": record, "evaluation": evaluation}


def cmd_capture_high(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, "PREPARE_HIGH")
    _check_grid_still_matches(state)
    gain = state["initial_gain_db"]
    result = asyncio.run(_do_capture(state, "HIGH", gain, simulate=args.simulate))
    state["captures"]["HIGH_0"] = result["capture"]
    state["evaluations"]["HIGH"] = result["evaluation"]
    if result["evaluation"]["clipping"]["status"] == "CLIPPED":
        # A clipping HIGH point means the grid WILL clip there - measuring LOW does not resolve that risk (a
        # healthy LOW point says nothing about HIGH still clipping). Go straight to a gain recommendation built
        # from HIGH alone; LOW stays reachable separately (capture-low, from GAIN_DECISION) as optional extra
        # information, never a required step that could substitute for re-verifying HIGH itself.
        state["need_low"] = {"need_low": False, "reason": "HIGH point is CLIPPED - a LOW measurement would not "
                             "resolve this; proceeding straight to a gain recommendation from HIGH, then a "
                             "mandatory HIGH re-capture at the new gain before READY"}
        state["gain_recommendation"] = recommend_gain(state["evaluations"], gain, PilotConfig.from_dict(state["config"]))
        state["step"] = "GAIN_DECISION"
    else:
        decision = needs_low_pilot(result["evaluation"], PilotConfig.from_dict(state["config"]))
        state["need_low"] = decision
        if decision["need_low"] and state["config"]["max_pilots"] >= 2 and "LOW" in state["candidates"]:
            state["step"] = "PREPARE_LOW"
        else:
            state["gain_recommendation"] = recommend_gain(state["evaluations"], gain, PilotConfig.from_dict(state["config"]))
            state["step"] = "GAIN_DECISION"
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def cmd_capture_low(args) -> int:
    session_dir = Path(args.session_dir)
    # Reachable from PREPARE_LOW (the "is one point enough" flow) OR from GAIN_DECISION (optional extra
    # information the operator asked for - see cmd_capture_high's docstring note: never required, never a
    # substitute for HIGH's own verification, so it never changes what step comes next).
    state = _load_state(session_dir)
    _require_step(state, "PREPARE_LOW", "GAIN_DECISION")
    _check_grid_still_matches(state)
    gain = state["initial_gain_db"]
    result = asyncio.run(_do_capture(state, "LOW", gain, simulate=args.simulate))
    state["captures"][f"LOW_{sum(1 for k in state['captures'] if k.startswith('LOW'))}"] = result["capture"]
    state["evaluations"]["LOW"] = result["evaluation"]
    state["gain_recommendation"] = recommend_gain(state["evaluations"], gain, PilotConfig.from_dict(state["config"]))
    state["step"] = "GAIN_DECISION"
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def cmd_set_gain(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, "GAIN_DECISION")
    from calibration_engine.gain_sweep import CANDIDATE_GAIN_TABLE_DB
    table = CANDIDATE_GAIN_TABLE_DB
    nearest = min(table, key=lambda g: abs(g - args.gain_db))
    if abs(nearest - args.gain_db) > 0.15:
        raise SystemExit(f"--gain-db {args.gain_db} is not one of the real admitted R820T2/R828D values (nearest "
                         f"is {nearest:g} dB) - pass an admitted value")
    state["approved_gain_db"] = nearest
    # READY may ONLY be reached with a HIGH-equivalent evaluation, AT EXACTLY this approved gain, that already
    # cleared the configured clipping/headroom thresholds - "the gain happens to be unchanged" is NOT on its own
    # enough (found live: approving the SAME gain HIGH had just clipped at used to skip straight to READY with
    # no passing check at all). Whenever that is not already on record, a real HIGH re-capture is required.
    latest_high_eval = state["evaluations"].get("VERIFY") or state["evaluations"].get("HIGH")
    gain_already_verified = (latest_high_eval is not None and latest_high_eval.get("gain_db") is not None and
                             abs(latest_high_eval["gain_db"] - nearest) <= 1e-6 and
                             latest_high_eval["clipping"]["status"] in ("OK", "WARNING"))
    if gain_already_verified:
        state["verified_gain_db"] = nearest
        state["step"] = "READY"
    else:
        state["step"] = "VERIFY_GAIN"
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def cmd_verify_gain(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, "VERIFY_GAIN")
    _check_grid_still_matches(state)
    gain = state["approved_gain_db"]
    result = asyncio.run(_do_capture(state, "HIGH", gain, simulate=args.simulate))
    idx = sum(1 for k in state["captures"] if k.startswith("VERIFY"))
    state["captures"][f"VERIFY_{idx}"] = result["capture"]
    state["evaluations"]["VERIFY"] = result["evaluation"]
    clip = result["evaluation"]["clipping"]
    ok = clip["status"] in ("OK", "WARNING")
    if ok:
        state["verified_gain_db"] = gain
        state["initial_gain_db"] = gain   # this gain is now what a future set-gain/recommend cycle should compare against
        state["step"] = "READY"
    else:
        state["gain_recommendation"] = recommend_gain({"VERIFY": result["evaluation"]}, gain, PilotConfig.from_dict(state["config"]))
        state["step"] = "GAIN_DECISION"
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def _stability_reference_conditions(state: Dict[str, Any]) -> Dict[str, Any]:
    """The exact point/frequency/sample-rate/duration a final-check baseline (and the final capture itself)
    MUST match - read fresh from this session's own tamper-checked resolved plan, its fixed config, and the
    HIGH candidate, never assumed unchanged just because nothing is SUPPOSED to change mid-session."""
    resolved_plan = _load_resolved_plan(Path(state["resolved_plan_path"]))
    cand = state["candidates"]["HIGH"]
    return {"ra_hours": cand["ra_hours"], "dec_deg": cand["dec_deg"], "capture_seconds": state["config"]["capture_seconds"],
           "sample_rate_hz": resolved_plan["main"]["sample_rate"], "center_frequency_hz": resolved_plan["main"]["center_frequency_hz"]}


def _conditions_mismatch(ev: Dict[str, Any], ref: Dict[str, Any], gain_db: float) -> Optional[str]:
    """None -> ev was captured at exactly ref's point/frequency/sample-rate/duration AND at gain_db. Otherwise
    the SPECIFIC field that differs (never a generic "doesn't match"), so a real cause can be shown/fixed."""
    for field, expected, tol in (("gain_db", gain_db, 1e-6), ("ra_hours", ref["ra_hours"], 1e-9),
                                 ("dec_deg", ref["dec_deg"], 1e-9), ("capture_seconds", ref["capture_seconds"], 1e-6),
                                 ("sample_rate_hz", ref["sample_rate_hz"], 1e-6),
                                 ("center_frequency_hz", ref["center_frequency_hz"], 1e-6)):
        actual = ev.get(field)
        if actual is None or abs(actual - expected) > tol:
            return f"{field} differs (recorded={actual}, required={expected})"
    return None


def _stability_baseline(state: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The HIGH-equivalent evaluation that matches EVERY condition a valid comparison needs - same HIGH point,
    same frequency, sample rate, capture duration, and the verified gain - checked field by field via
    _conditions_mismatch(), never assumed from "this session never changed anything". Prefers the most recent
    gain-change verification (VERIFY) over the original HIGH capture, since a verified gain change supersedes
    the original measurement as the only valid reference. Returns (baseline, None) on a full match, or
    (None, reason) naming exactly what could not be confirmed."""
    verified = state.get("verified_gain_db")
    if verified is None:
        return None, "no verified gain is on record for this session"
    ref = _stability_reference_conditions(state)
    mismatches: List[str] = []
    for key in ("VERIFY", "HIGH"):
        ev = state["evaluations"].get(key)
        if ev is None:
            continue
        problem = _conditions_mismatch(ev, ref, verified)
        if problem is None:
            return ev, None
        mismatches.append(f"{key}: {problem}")
    if not mismatches:
        return None, "no HIGH-equivalent pilot capture is on record at all"
    return None, "no pilot capture on record matches every required condition (" + "; ".join(mismatches) + ")"


def cmd_final_check(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    if state["step"] != "READY":
        raise SystemExit("gain pilot must have reached READY before a final check")
    _check_grid_still_matches(state)
    baseline, reason = _stability_baseline(state)
    if baseline is None:
        # Never compare across a different point/gain/frequency/sample-rate/duration - that difference IS what
        # would show up as "drift", not any real instability. Without a real matching baseline on record, say
        # so plainly (with the exact reason) instead of computing a misleading number.
        state["final_check"] = {
            "evaluation": None, "label": "STABILITY NOT EVALUABLE", "reason": reason,
            "checked_utc": datetime.now(timezone.utc).isoformat(),
        }
        _save_state(session_dir, state)
        _emit({"session_dir": str(session_dir), "state": state})
        return 2
    gain = state["verified_gain_db"]
    result = asyncio.run(_do_capture(state, "HIGH", gain, simulate=args.simulate))
    idx = sum(1 for k in state["captures"] if k.startswith("FINAL"))
    state["captures"][f"FINAL_{idx}"] = result["capture"]
    now = result["evaluation"]
    # Defense in depth: the capture just taken must ALSO match the reference conditions - by construction it
    # always should (same candidate, same fixed session config, same resolved plan), but this is verified
    # explicitly rather than assumed, exactly like the baseline was.
    ref = _stability_reference_conditions(state)
    now_problem = _conditions_mismatch(now, ref, gain)
    if now_problem:
        state["final_check"] = {
            "evaluation": None, "label": "STABILITY NOT EVALUABLE",
            "reason": f"the final capture itself did not match the required conditions ({now_problem}) - no comparison made",
            "checked_utc": datetime.now(timezone.utc).isoformat(),
        }
        _save_state(session_dir, state)
        _emit({"session_dir": str(session_dir), "state": state})
        return 2
    p_base, p_now = baseline["relative_digital_power"], now["relative_digital_power"]
    delta_fraction = abs(p_now - p_base) / max(p_base, 1e-30)
    state["final_check"] = {
        "evaluation": now, "baseline_relative_digital_power": p_base, "current_relative_digital_power": p_now,
        "relative_power_delta_fraction": delta_fraction, "baseline_clipping": baseline["clipping"]["status"],
        "current_clipping": now["clipping"]["status"], "baseline_gain_db": baseline["gain_db"], "compared_gain_db": gain,
        "baseline_point": {"ra_hours": baseline["ra_hours"], "dec_deg": baseline["dec_deg"]},
        "baseline_capture_seconds": baseline["capture_seconds"], "baseline_sample_rate_hz": baseline["sample_rate_hz"],
        "baseline_center_frequency_hz": baseline["center_frequency_hz"],
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "label": f"STABILITY CHECK (confirmed: same HIGH point, same {gain:g} dB gain, same frequency, sample "
                f"rate and capture duration as the baseline pilot capture within this session) - NOT an absolute calibration",
    }
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def cmd_abort(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    state["step"] = "ABORTED"
    state["aborted"] = True
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

    pl = sub.add_parser("plan")
    pl.add_argument("--resolved-plan-path", required=True)
    pl.add_argument("--max-pilots", type=int, default=2)
    pl.add_argument("--capture-seconds", type=float, default=2.0)
    pl.add_argument("--settle-seconds", type=float, default=1.0)
    pl.add_argument("--headroom-multiplier", type=float, default=1.5)
    pl.add_argument("--clipping-threshold", type=float, default=1.0e-4)
    pl.add_argument("--rfi-threshold", type=float, default=0.5)
    pl.add_argument("--gain-db", type=float, default=40.2)
    pl.add_argument("--beam-fwhm", type=float, default=None)
    pl.add_argument("--final-check", type=lambda v: str(v).lower() == "true", default=True)
    pl.set_defaults(func=cmd_plan)

    sim_help = "offline-testing only: HEALTHY|CLIPPED|THERMAL_DRIFT|RFI_CONTAMINATED - never used for a real pilot measurement"
    ch = sub.add_parser("capture-high"); ch.add_argument("--session-dir", required=True)
    ch.add_argument("--simulate", default=None, help=sim_help); ch.set_defaults(func=cmd_capture_high)
    cl = sub.add_parser("capture-low"); cl.add_argument("--session-dir", required=True)
    cl.add_argument("--simulate", default=None, help=sim_help); cl.set_defaults(func=cmd_capture_low)
    sg = sub.add_parser("set-gain"); sg.add_argument("--session-dir", required=True); sg.add_argument("--gain-db", type=float, required=True); sg.set_defaults(func=cmd_set_gain)
    vg = sub.add_parser("verify-gain"); vg.add_argument("--session-dir", required=True)
    vg.add_argument("--simulate", default=None, help=sim_help); vg.set_defaults(func=cmd_verify_gain)
    fc = sub.add_parser("final-check"); fc.add_argument("--session-dir", required=True)
    fc.add_argument("--simulate", default=None, help=sim_help); fc.set_defaults(func=cmd_final_check)
    ab = sub.add_parser("abort"); ab.add_argument("--session-dir", required=True); ab.set_defaults(func=cmd_abort)
    st = sub.add_parser("status"); st.add_argument("--session-dir", required=True); st.set_defaults(func=cmd_status)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())


# ------------------------------------------------------------------ START-time gate (called from the web server, not the CLI)
def check_ready_for_grid_start(resolved_plan_path: Path) -> Optional[str]:
    """None -> START may proceed (either the gain-pilot stage was never used for this grid session at all - the
    'stage disabled, OBSERVE behaves as before' case - or it reached READY and still matches the grid plan
    about to run). A non-None string is the exact reason to show the operator and block START; it always says
    to PLAN the gain pilot again rather than letting a stale/partial pilot silently pass."""
    resolved_plan_path = Path(resolved_plan_path).resolve()
    session_dir = resolved_plan_path.parent
    state = read_json_safe(_state_path(session_dir))
    if state is None:
        return None    # never used for this grid session - unchanged OBSERVE behaviour
    if state.get("step") == "ABORTED":
        return None    # explicitly aborted - the operator chose not to use it after all
    resolved_plan = json.loads(resolved_plan_path.read_text())
    import observation_plan
    if observation_plan.recompute_config_hash(resolved_plan) != resolved_plan.get("observation_config_sha256"):
        return "observation_resolved.json failed its tamper/corruption check - PLAN the grid again"
    if state.get("grid_config_hash") != resolved_plan.get("observation_config_sha256"):
        return "the grid plan changed since the gain pilot was planned/run against it - PLAN the gain pilot again"
    if state.get("step") != "READY":
        return f"the gain pilot has not reached READY yet (currently at {state.get('step')}) - finish it or abort it before START"
    verified = state.get("verified_gain_db")
    grid_gain = resolved_plan.get("main", {}).get("gain_db")
    if verified is None or grid_gain is None or abs(float(verified) - float(grid_gain)) > 1e-6:
        return (f"the gain pilot verified {verified} dB but the grid plan is currently set to {grid_gain} dB - "
               "PLAN the grid again with the approved gain, then re-check the gain pilot against that new plan")
    return None
