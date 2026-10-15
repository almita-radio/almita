#!/usr/bin/env python3
"""Observation Plan: spec -> resolved, reproducible ALMITA observation plan.

Turns a validated observation_spec dict into an immutable
observation_resolved.json plus the real grid_generator.py artifacts
(mosaic.csv, grid_metadata.json, grid_plan.png, grid_coverage.png), using
the existing, unmodified GridGenerator/build_spherical_grid production
code path.

PLAN never moves the mount, never enables tracking, never starts
acquisition, never launches RFI_REF, and never mutates OnStep or
configuration. It may read hardware state (via observation_preflight),
calculate, and generate files. This is verified by
test_observation_plan.py::test_plan_never_touches_indi_or_subprocess.
"""
from __future__ import annotations

import importlib
import json
import math
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import astropy_offline  # noqa: F401 - configures offline IERS policy on import, like capture.py/grid_generator.py

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_sun
from astropy.time import Time

import grid_generator
import observation_spec

SCHEMA_VERSION = 1

# --- Duration model -------------------------------------------------------
# Per-point overhead (GOTO + guards + tracking-confirm + SDR flush + HDF5
# write + orchestration bookkeeping) derived from the AFC-E2E-01 49-point
# real-hardware validation run's capture_timing CSV
# (data/e2e/AFC-E2E-01-20260827T233127Z/.../capture_timing_20260828_005847.csv),
# specifically point_total_sec minus capture_sec minus settle_sec for a
# small excerpt of that run's non-warmup points. This is a coarse estimate
# from a handful of real samples, not a large statistical sample — treated
# as illustrative, never claimed as precise.
MEASURED_OVERHEAD_MEDIAN_SEC = 19.0
MEASURED_OVERHEAD_P90_SEC = 25.0
CONSERVATIVE_SAFETY_FACTOR = 1.15

# HDF5 payload: interleaved uint8 I/Q pairs, same formula capture.py's own
# preflight disk-space check uses (bytes_per_complex_sample = 2).
BYTES_PER_COMPLEX_SAMPLE = 2
DEFAULT_DISK_SAFETY_FACTOR = 1.25

# EASTMOST_SAFE search bounds/tuning.
EASTMOST_SEARCH_FLOOR_HOURS = -11.5   # most-eastward HA considered
EASTMOST_SEARCH_CEILING_HOURS = 11.5  # most-westward HA considered: the meridian is NOT a limit, the altitude floor is
EASTMOST_SCAN_STEP_HOURS = 0.5        # coarse east->west scan for the first placement that clears the floor
EASTMOST_ALTITUDE_MARGIN_DEG = 2.0    # extra margin above min_altitude_deg
EASTMOST_BISECTION_TOLERANCE_HOURS = 0.01  # ~36s of RA

# Sky-time window shown with the live estimate: how long the whole footprint stays above the planning floor.
SKY_WINDOW_HORIZON_HOURS = 24.0
SKY_WINDOW_STEP_MINUTES = 5.0


class ObservationPlanError(ValueError):
    """Raised when a plan cannot be produced (BLOCK-level planning failure)."""


def _load_observer_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise ObservationPlanError(
            f"observer config missing at {config_path} — Orchestrator will not fabricate a default "
            "for a real campaign (same fail-closed policy as capture.py)"
        )
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _observer_location(observer_config: Dict[str, Any]) -> Tuple[EarthLocation, Dict[str, float]]:
    observer = observer_config.get("observer", {})
    lat = float(observer["latitude_deg"])
    lon = float(observer["longitude_deg"])
    elevation = float(observer.get("elevation_m", 0.0))
    location = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=elevation * u.m)
    return location, {"latitude_deg": lat, "longitude_deg": lon, "elevation_m": elevation}


def estimate_duration(num_points: int, capture_s: float, settle_s: float) -> Dict[str, Any]:
    """Estimate total session duration. Returns estimated/conservative seconds + assumptions."""
    per_point_estimated = capture_s + settle_s + MEASURED_OVERHEAD_MEDIAN_SEC
    per_point_conservative = (capture_s + settle_s + MEASURED_OVERHEAD_P90_SEC) * CONSERVATIVE_SAFETY_FACTOR
    # Planning value (operator, 2026-10-15): the mean of the projected and the conservative pace - real campaigns keep
    # to the projection closely, the conservative one over-reserves sky. It schedules the altitude predictions and the
    # sky-window check; the conservative figure stays reported as the outer bound.
    per_point_planning = (per_point_estimated + per_point_conservative) / 2.0
    return {
        "estimated_seconds": num_points * per_point_estimated,
        "conservative_seconds": num_points * per_point_conservative,
        "planning_seconds": num_points * per_point_planning,
        "per_point_conservative_seconds": per_point_conservative,
        "per_point_planning_seconds": per_point_planning,
        "assumptions": [
            f"per-point overhead (GOTO+guards+tracking-confirm+SDR flush+HDF5 write) modeled as a "
            f"constant {MEASURED_OVERHEAD_MEDIAN_SEC:.1f}s median / {MEASURED_OVERHEAD_P90_SEC:.1f}s p90, "
            "derived from a small excerpt of the AFC-E2E-01 49-point real-hardware validation run — "
            "not a large statistical sample.",
            f"conservative estimate applies an additional {CONSERVATIVE_SAFETY_FACTOR}x safety factor "
            "on top of the p90 overhead.",
            "does not model unusually long slews between distant grid corners; assumes roughly uniform "
            "per-point pacing across the session.",
        ],
    }


def estimate_storage(num_points: int, capture_s: float, sample_rate_hz: int,
                      disk_safety_factor: float = DEFAULT_DISK_SAFETY_FACTOR) -> Dict[str, Any]:
    """Estimate HDF5 storage. Reuses capture.py's own preflight disk-space formula verbatim."""
    estimated_bytes = int(num_points * capture_s * sample_rate_hz * BYTES_PER_COMPLEX_SAMPLE)
    required_bytes = int(math.ceil(estimated_bytes * disk_safety_factor))
    return {
        "estimated_bytes": estimated_bytes,
        "required_bytes": required_bytes,
        "disk_safety_factor": disk_safety_factor,
    }


def _predicted_altitudes(points: List[Dict[str, Any]], location: EarthLocation, t0: Time,
                          per_point_seconds: float) -> List[Dict[str, Any]]:
    """For points (already in scan_order), predict execution time + altitude for each."""
    results = []
    offsets_sec = [i * per_point_seconds for i in range(len(points))]
    times = t0 + [timedelta(seconds=s) for s in offsets_sec]
    ra_deg = [p["target_ra_hours"] * 15.0 for p in points]
    dec_deg = [p["target_dec_degrees"] for p in points]
    coords = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    altaz_frame = AltAz(obstime=times, location=location)
    altaz = coords.transform_to(altaz_frame)
    for point, when, alt in zip(points, times, altaz.alt.deg):
        results.append({
            "point_id": point["point_id"],
            "scan_order": point["scan_order"],
            "predicted_execution_time_utc": when.utc.isot + "Z",
            "predicted_altitude_deg": float(alt),
        })
    return results


def sky_window(points: List[Dict[str, Any]], location: EarthLocation, t0: Time, min_altitude_deg: float,
               horizon_hours: float = SKY_WINDOW_HORIZON_HOURS, step_minutes: float = SKY_WINDOW_STEP_MINUTES
               ) -> Dict[str, Any]:
    """The next window (the current one when it is already open) in which the WHOLE footprint - its lowest point -
    is above min_altitude_deg, sampled every step_minutes over the next horizon_hours, and the time from t0 until that
    window closes (the region sets). An EASTMOST_SAFE region can still be rising at t0: the plan visits its low points
    after they have risen, so the time that matters for a run is seconds_until_window_end. Only the footprint's edge points are evaluated (the lowest point of a region away from the nadir is on its edge)."""
    last_row = max(p["grid_row"] for p in points)
    last_col = max(p["grid_col"] for p in points)
    edge = [p for p in points if p["grid_row"] in (0, last_row) or p["grid_col"] in (0, last_col)]
    coords = SkyCoord(ra=[p["target_ra_hours"] * 15.0 for p in edge] * u.deg,
                      dec=[p["target_dec_degrees"] for p in edge] * u.deg, frame="icrs")
    steps = int(horizon_hours * 60 / step_minutes) + 1
    times = t0 + np.arange(steps) * step_minutes * u.min
    alt = coords[np.newaxis, :].transform_to(AltAz(obstime=times[:, np.newaxis], location=location)).alt.deg
    lowest = alt.min(axis=1)
    up = lowest >= min_altitude_deg

    out: Dict[str, Any] = {"min_altitude_deg": min_altitude_deg, "step_minutes": step_minutes,
                           "horizon_hours": horizon_hours, "lowest_altitude_now_deg": float(lowest[0]),
                           "visible_now": bool(up[0]), "best_lowest_altitude_deg": float(lowest.max()),
                           "window_start_utc": None, "window_end_utc": None, "window_seconds": None,
                           "seconds_until_window_end": None, "window_open_past_horizon": False}
    ups = np.flatnonzero(up)
    if ups.size == 0:  # never wholly above the floor within the horizon
        return out
    start = int(ups[0])
    end = start
    while end < steps and up[end]:
        end += 1
    # the last sample still wholly up: the true setting time lies before the next sample, so ending the window there
    # never promises sky that is not there (it used to end at the first sample already below: up to one step late)
    last = end - 1
    out.update(window_start_utc=times[start].utc.isot + "Z", window_end_utc=times[last].utc.isot + "Z",
               window_seconds=(last - start) * step_minutes * 60.0,
               seconds_until_window_end=last * step_minutes * 60.0, window_open_past_horizon=end >= steps)
    return out


def _grid_points(center_ra_hours: float, center_dec_deg: float, width_deg: float, height_deg: float,
                  nominal_spacing_deg: float) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    return grid_generator.build_spherical_grid(
        center_ra_hours=center_ra_hours,
        center_dec_deg=center_dec_deg,
        width_deg=width_deg,
        height_deg=height_deg,
        beam_fwhm_deg=nominal_spacing_deg,
        beam_sampling_fraction=1.0,
    )


def _resolve_eastmost_safe(grid_spec: Dict[str, Any], location: EarthLocation, t0: Time,
                            per_point_seconds: float) -> Dict[str, Any]:
    """The most-eastward center RA for which every point clears the altitude floor (+margin) at its predicted
    execution time over the planned window (mean of projected and conservative pace). The reference is the altitude floor, not the meridian: candidates
    are scanned east -> west over the whole hour-angle range and the first one that clears is refined by bisection
    against its (failing) eastern neighbour. A long run is therefore placed rising in the east, where it has the
    most time ahead of it, instead of being rejected because a meridian-centred start would set before the end
    (2026-10-14: 15x15 at 50 s capture rejected while starting 3 h east of the meridian kept every point >= 42 deg)."""
    width = grid_spec["width_deg"]
    height = grid_spec["height_deg"]
    spacing = grid_spec["nominal_spacing_deg"]
    min_alt = grid_spec["min_altitude_deg"] + EASTMOST_ALTITUDE_MARGIN_DEG
    lst = t0.sidereal_time("apparent", longitude=location.lon).hour
    # The grid around a centre at RA 0 shifted in RA IS the grid around any other centre at the same declination
    # (equal to a fresh build within 1e-9 arcsec), so the search builds it once; the chosen centre is built for real.
    template, _ = _grid_points(0.0, grid_spec["center_dec_deg"], width, height, spacing)

    def lowest(ha_hours: float) -> float:
        ra_hours = (lst - ha_hours) % 24.0
        shifted = [{**pt, "target_ra_hours": (pt["target_ra_hours"] + ra_hours) % 24.0} for pt in template]
        return min(p["predicted_altitude_deg"] for p in _predicted_altitudes(shifted, location, t0, per_point_seconds))

    steps = int(round((EASTMOST_SEARCH_CEILING_HOURS - EASTMOST_SEARCH_FLOOR_HOURS) / EASTMOST_SCAN_STEP_HOURS))
    best_ha, best_low = None, -math.inf
    previous, first_ok = None, None
    for k in range(steps + 1):
        ha = EASTMOST_SEARCH_FLOOR_HOURS + k * EASTMOST_SCAN_STEP_HOURS
        low = lowest(ha)
        if low > best_low:
            best_ha, best_low = ha, low
        if low >= min_alt:
            first_ok = ha
            break
        previous = ha
    if first_ok is None:
        raise ObservationPlanError(
            f"EASTMOST_SAFE: no placement keeps every point above {min_alt:.1f} deg planning altitude "
            f"({grid_spec['min_altitude_deg']:.1f} + {EASTMOST_ALTITUDE_MARGIN_DEG:.1f} margin) for the whole "
            f"conservative window (best: lowest point {best_low:.1f} deg, starting at hour angle {best_ha:+.1f}h) "
            "— requested declination/min_altitude/duration is not achievable this session"
        )
    resolved_ha = first_ok
    if previous is not None:  # refine the boundary between the last failing and the first clearing candidate
        lo, hi = previous, first_ok
        while hi - lo > EASTMOST_BISECTION_TOLERANCE_HOURS:
            mid = (lo + hi) / 2.0
            if lowest(mid) >= min_alt:
                hi = mid
            else:
                lo = mid
        resolved_ha = hi
    resolved_ra = (lst - resolved_ha) % 24.0
    points, metadata = _grid_points(resolved_ra, grid_spec["center_dec_deg"], width, height, spacing)
    predictions = _predicted_altitudes(points, location, t0, per_point_seconds)
    min_predicted = min(p["predicted_altitude_deg"] for p in predictions)

    reasoning = (
        f"Center RA chosen as the easternmost candidate (hour angle {resolved_ha:+.2f}h at plan time) "
        f"for which all {len(points)} predicted execution points remain above "
        f"{grid_spec['min_altitude_deg']:.1f} deg planning altitude (+{EASTMOST_ALTITUDE_MARGIN_DEG:.1f} deg "
        f"safety margin) over the planned session window (mean of projected and conservative pace); minimum predicted altitude "
        f"{min_predicted:.1f} deg."
    )
    return {
        "center_ra_hours": resolved_ra,
        "center_dec_deg": grid_spec["center_dec_deg"],
        "points": points,
        "metadata": metadata,
        "predictions": predictions,
        "min_predicted_altitude_deg": min_predicted,
        "reasoning": reasoning,
    }


def _config_hash_payload(*, grid_mode: str, placement: str, resolved_ra: float, resolved_dec: float,
                          rows: int, cols: int, spacing_deg: float, traversal: str, min_altitude_deg: float,
                          capture: Dict[str, Any], main: Dict[str, Any], rfi_ref: Dict[str, Any],
                          quicklook: Dict[str, Any]) -> Dict[str, Any]:
    """The exact payload compute_config_hash() is applied to. Single source of truth so
    plan_observation() (at PLAN time) and recompute_config_hash() (at PREFLIGHT/RUN time,
    used to detect tampering/corruption of a resolved plan file) can never drift apart."""
    return {
        "schema_version": SCHEMA_VERSION,
        "grid": {
            "mode": grid_mode, "placement": placement,
            "resolved_center_ra_hours": round(resolved_ra, 8),
            "resolved_center_dec_deg": round(resolved_dec, 8),
            "rows": rows, "cols": cols,
            "nominal_spacing_deg": round(spacing_deg, 10),
            "traversal": traversal, "min_altitude_deg": min_altitude_deg,
        },
        "capture": capture,
        "main": main,
        "rfi_ref": rfi_ref,
        "quicklook": {k: v for k, v in quicklook.items()},
    }


def recompute_config_hash(resolved_plan: Dict[str, Any]) -> str:
    """Recompute observation_config_sha256 from an already-written resolved plan dict.

    Used by observation_preflight.py and observation_orchestrator.py to detect
    tampering/corruption of observation_resolved.json before acting on it.
    """
    resolved = resolved_plan["resolved"]
    requested = resolved_plan["requested"]
    payload = _config_hash_payload(
        grid_mode=resolved["mode"], placement=resolved["placement"],
        resolved_ra=resolved["center_ra_hours"], resolved_dec=resolved["center_dec_deg"],
        rows=resolved["rows"], cols=resolved["cols"], spacing_deg=resolved["spacing_deg"],
        traversal=resolved["traversal"], min_altitude_deg=requested["grid"]["min_altitude_deg"],
        capture=requested["capture"], main=requested["main"], rfi_ref=requested["rfi_ref"],
        quicklook=requested["quicklook"],
    )
    return observation_spec.compute_config_hash(payload)


def _plan_time_preflight(resolved_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Informational read-only preflight probe run during PLAN (spec: PLAN "performs a
    read-only preflight"). Failures here are informational, shown to the operator in the
    PLAN summary — they never block plan_observation() from producing a resolved plan.
    RUN re-runs the same preflight as a hard gate (see observation_orchestrator.run_observation).
    Imported lazily to avoid a circular import (observation_preflight imports this module
    for recompute_config_hash())."""
    import asyncio
    import observation_preflight
    try:
        return asyncio.run(observation_preflight.run_plan_preflight(resolved_plan))
    except Exception as exc:  # noqa: BLE001 - never let a hardware probe failure abort PLAN
        return {
            "checks": [{"name": "Preflight probe", "category": "SOFTWARE", "criticality": "OPTIONAL",
                        "status": "WARNING", "detail": f"preflight probe itself failed: {type(exc).__name__}: {exc}"}],
            "overall": "WARNING",
            "generated_utc": datetime.now(timezone.utc).isoformat(),
        }


def _resolve_grid(spec: Dict[str, Any], observer_config_path: str, now: Optional[datetime],
                  duration: Dict[str, Any]) -> Dict[str, Any]:
    """Placement + predicted altitudes over the planned window (mean of projected and conservative pace): everything PLAN computes before it touches
    the disk. Shared by plan_observation() and estimate_observation() so the live estimate can never disagree
    with the plan. Raises ObservationPlanError exactly as PLAN does."""
    observer_config = _load_observer_config(Path(observer_config_path))
    location, observer_summary = _observer_location(observer_config)

    grid_spec = dict(spec["grid"])
    if grid_spec["center_dec_deg"] is None:
        # EASTMOST_SAFE with no explicit declination: default to the
        # observer's own latitude so the field transits near zenith.
        grid_spec["center_dec_deg"] = observer_summary["latitude_deg"]

    t0 = Time(now.astimezone(timezone.utc)) if now else Time.now()

    per_point_seconds = duration["per_point_planning_seconds"]

    if grid_spec["placement"] == "FIXED_CENTER":
        points, metadata = _grid_points(grid_spec["center_ra_hours"], grid_spec["center_dec_deg"],
                                         grid_spec["width_deg"], grid_spec["height_deg"],
                                         grid_spec["nominal_spacing_deg"])
        predictions = _predicted_altitudes(points, location, t0, per_point_seconds)
        min_predicted = min(p["predicted_altitude_deg"] for p in predictions)
        resolved_ra = grid_spec["center_ra_hours"]
        reasoning = "Center RA/Dec taken verbatim from grid.placement=FIXED_CENTER."
        if min_predicted < grid_spec["min_altitude_deg"]:
            raise ObservationPlanError(
                f"FIXED_CENTER: predicted minimum altitude {min_predicted:.1f} deg falls below "
                f"planning floor {grid_spec['min_altitude_deg']:.1f} deg at some point during the "
                "planned session window (mean of projected and conservative pace)"
            )
    elif grid_spec["placement"] == "EASTMOST_SAFE":
        resolved = _resolve_eastmost_safe(grid_spec, location, t0, per_point_seconds)
        points = resolved["points"]
        metadata = resolved["metadata"]
        predictions = resolved["predictions"]
        min_predicted = resolved["min_predicted_altitude_deg"]
        resolved_ra = resolved["center_ra_hours"]
        reasoning = resolved["reasoning"]
    else:  # pragma: no cover - guarded by observation_spec's enum validation
        raise ObservationPlanError(f"unsupported placement: {grid_spec['placement']}")
    return {"observer_config": observer_config, "location": location, "grid_spec": grid_spec, "t0": t0, "duration": duration,
            "points": points, "metadata": metadata, "predictions": predictions, "min_predicted": min_predicted,
            "resolved_ra": resolved_ra, "reasoning": reasoning}


def sky_view(points: List[Dict[str, Any]], location: EarthLocation, t0: Time, per_point_seconds: float,
             min_altitude_deg: float) -> Dict[str, Any]:
    """Where each point will be in the sky WHEN it is captured (scan order, the same schedule as the altitude
    prediction): az/alt for OBSERVE's sky map, plus the Sun now. Read-only."""
    times = t0 + [timedelta(seconds=i * per_point_seconds) for i in range(len(points))]
    coords = SkyCoord(ra=[p["target_ra_hours"] * 15.0 for p in points] * u.deg,
                      dec=[p["target_dec_degrees"] for p in points] * u.deg, frame="icrs")
    aa = coords.transform_to(AltAz(obstime=times, location=location))
    now = coords.transform_to(AltAz(obstime=t0, location=location))       # the footprint itself: every point at t0
    sun = get_sun(t0).transform_to(AltAz(obstime=t0, location=location))
    return {"computed_utc": t0.utc.isot + "Z", "min_altitude_deg": min_altitude_deg,
            "points": [{"scan_order": int(p["scan_order"]), "az_deg": float(az), "alt_deg": float(alt),
                        "below_min": bool(alt < min_altitude_deg)} for p, az, alt in zip(points, aa.az.deg, aa.alt.deg)],
            # red means "below the floor WHEN captured" in both views: a point low now may be captured after it rose
            "points_now": [{"scan_order": int(p["scan_order"]), "az_deg": float(az), "alt_deg": float(alt), "alt_now_deg": float(alt),
                            "below_min": bool(cap < min_altitude_deg)}
                           for p, az, alt, cap in zip(points, now.az.deg, now.alt.deg, aa.alt.deg)],
            "sun": {"az_deg": float(sun.az.deg), "alt_deg": float(sun.alt.deg), "above_horizon": bool(sun.alt.deg > 0)}}


def estimate_observation(spec: Dict[str, Any], *, observer_config_path: str = "observer_config.json",
                         now: Optional[datetime] = None) -> Dict[str, Any]:
    """What PLAN would say right now, without writing anything (no grid directory, no plots, no preflight):
    the duration estimate, whether the placement clears the altitude floor and how long the region stays above it
    (sky_window). A planning failure comes back as
    feasible=False with PLAN's own message instead of an exception."""
    duration = estimate_duration(spec["grid"]["rows"] * spec["grid"]["cols"],
                                 spec["capture"]["seconds"], spec["capture"]["settle_seconds"])
    out: Dict[str, Any] = {"duration": duration, "requested_point_count": spec["grid"]["rows"] * spec["grid"]["cols"]}
    try:
        r = _resolve_grid(spec, observer_config_path, now, duration)
    except ValueError as exc:  # ObservationPlanError, or a footprint the sky geometry rejects (e.g. past a pole)
        return {**out, "feasible": False, "blocking_reason": str(exc), "sky_window": _fixed_center_window(spec, observer_config_path, now),
                "sky": _fixed_center_sky(spec, observer_config_path, now, duration)}
    md = r["metadata"]
    out["sky"] = sky_view(r["points"], r["location"], r["t0"], r["duration"]["per_point_planning_seconds"],
                          r["grid_spec"]["min_altitude_deg"])
    return {**out, "feasible": True, "blocking_reason": None,
            "point_count": md["total_points"], "rows": md["rows"], "cols": md["columns"],
            "spacing_deg": md["nominal_spacing_deg"],
            "center_ra_hours": r["resolved_ra"], "center_dec_deg": r["grid_spec"]["center_dec_deg"],
            "min_predicted_altitude_deg": r["min_predicted"],
            "max_recommended_start_delay_minutes": _max_recommended_start_delay_minutes(
                r["predictions"], r["grid_spec"]["min_altitude_deg"]),
            "sky_window": sky_window(r["points"], r["location"], r["t0"], r["grid_spec"]["min_altitude_deg"]),
            "estimated_end_utc": (r["t0"] + timedelta(seconds=duration["estimated_seconds"])).utc.isot + "Z",
            "conservative_end_utc": (r["t0"] + timedelta(seconds=duration["conservative_seconds"])).utc.isot + "Z",
            "planning_end_utc": (r["t0"] + timedelta(seconds=duration["planning_seconds"])).utc.isot + "Z"}


def _fixed_center_sky(spec: Dict[str, Any], observer_config_path: str, now: Optional[datetime],
                      duration: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The requested FIXED_CENTER points on the sky map even when PLAN rejects them (so the operator SEES why)."""
    g = spec["grid"]
    if g["placement"] != "FIXED_CENTER":
        return None
    try:
        location, _ = _observer_location(_load_observer_config(Path(observer_config_path)))
        points, _ = _grid_points(g["center_ra_hours"], g["center_dec_deg"], g["width_deg"], g["height_deg"],
                                 g["nominal_spacing_deg"])
        t0 = Time(now.astimezone(timezone.utc)) if now else Time.now()
        return sky_view(points, location, t0, duration["per_point_planning_seconds"], g["min_altitude_deg"])
    except Exception:  # noqa: BLE001 - a footprint the geometry rejects (e.g. past a pole) simply has no map
        return None


def _fixed_center_window(spec: Dict[str, Any], observer_config_path: str,
                         now: Optional[datetime]) -> Optional[Dict[str, Any]]:
    """A FIXED_CENTER region PLAN rejects is still a real region: say when it IS up (EASTMOST_SAFE has no fixed
    region to ask about)."""
    g = spec["grid"]
    if g["placement"] != "FIXED_CENTER":
        return None
    try:
        location, _ = _observer_location(_load_observer_config(Path(observer_config_path)))
        points, _ = _grid_points(g["center_ra_hours"], g["center_dec_deg"], g["width_deg"], g["height_deg"],
                                 g["nominal_spacing_deg"])
    except ValueError:
        return None
    t0 = Time(now.astimezone(timezone.utc)) if now else Time.now()
    return sky_window(points, location, t0, g["min_altitude_deg"])


def plan_observation(spec: Dict[str, Any], *, observer_config_path: str = "observer_config.json",
                      data_dir: str = "./data/mosaic", now: Optional[datetime] = None,
                      run_preflight: bool = True) -> Dict[str, Any]:
    """Produce a resolved plan and materialize the real grid session directory.

    Zero hardware motion. Returns the resolved-plan dict (also written to
    observation_resolved.json inside the newly created grid session dir).
    """
    duration = estimate_duration(spec["grid"]["rows"] * spec["grid"]["cols"],
                                  spec["capture"]["seconds"], spec["capture"]["settle_seconds"])
    r = _resolve_grid(spec, observer_config_path, now, duration)
    observer_config, grid_spec, t0 = r["observer_config"], r["grid_spec"], r["t0"]
    points, metadata, predictions = r["points"], r["metadata"], r["predictions"]
    min_predicted, resolved_ra, reasoning = r["min_predicted"], r["resolved_ra"], r["reasoning"]

    storage = estimate_storage(len(points), spec["capture"]["seconds"], spec["main"]["sample_rate"])
    free_bytes = int(shutil.disk_usage(data_dir if Path(data_dir).exists() else ".").free)

    # Materialize the real grid session directory via the untouched
    # production GridGenerator — zero forked projection/serpentine logic.
    generator = grid_generator.GridGenerator(
        session_name=spec["session"]["name"],
        base_dir=data_dir,
        beam_fwhm_deg=grid_spec["nominal_spacing_deg"],
        beam_sampling_fraction=1.0,
    )
    generator.persist_observer_config(Path(observer_config_path))
    generator.generate_grid_plan(
        center_ra=resolved_ra,
        center_dec=grid_spec["center_dec_deg"],
        width_deg=grid_spec["width_deg"],
        height_deg=grid_spec["height_deg"],
    )

    max_start_delay_minutes = _max_recommended_start_delay_minutes(predictions, grid_spec["min_altitude_deg"])

    config_hash = observation_spec.compute_config_hash(_config_hash_payload(
        grid_mode=grid_spec["mode"], placement=grid_spec["placement"], resolved_ra=resolved_ra,
        resolved_dec=grid_spec["center_dec_deg"], rows=metadata["rows"], cols=metadata["columns"],
        spacing_deg=metadata["nominal_spacing_deg"], traversal=grid_spec["traversal"],
        min_altitude_deg=grid_spec["min_altitude_deg"], capture=spec["capture"], main=spec["main"],
        rfi_ref=spec["rfi_ref"], quicklook=spec["quicklook"],
    ))

    resolved_plan = {
        "schema_version": SCHEMA_VERSION,
        "observation_name": spec["session"]["name"],
        "planning_timestamp_utc": t0.utc.isot + "Z",
        "observer_config": observer_config,
        "requested": spec,
        "resolved": {
            "mode": grid_spec["mode"],
            "placement": grid_spec["placement"],
            "frame": "ICRS",
            "center_ra_hours": resolved_ra,
            "center_dec_deg": grid_spec["center_dec_deg"],
            "rows": metadata["rows"],
            "cols": metadata["columns"],
            "point_count": metadata["total_points"],
            "footprint_width_deg": metadata["width_deg"],
            "footprint_height_deg": metadata["height_deg"],
            "spacing_deg": metadata["nominal_spacing_deg"],
            "traversal": grid_spec["traversal"],
            "placement_reasoning": reasoning,
        },
        "target_list": predictions,
        "duration": duration,
        "storage": {**storage, "free_bytes_at_plan_time": free_bytes},
        "main": spec["main"],
        "rfi_ref": spec["rfi_ref"],
        "quicklook": spec["quicklook"],
        "safety_policy": _static_safety_policy(),
        "software_versions": _software_versions(),
        "observation_config_sha256": config_hash,
        "grid_session_dir": str(generator.output_dir),
        "mosaic_csv_path": str(generator.csv_filepath),
        "min_predicted_altitude_deg": min_predicted,
        "visibility": "PASS" if min_predicted >= grid_spec["min_altitude_deg"] else "BLOCK",
        "max_recommended_start_delay_minutes": max_start_delay_minutes,
    }

    if run_preflight:
        resolved_plan["preflight"] = _plan_time_preflight(resolved_plan)

    resolved_path = generator.output_dir / "observation_resolved.json"
    resolved_path.write_text(json.dumps(resolved_plan, indent=2), encoding="utf-8")
    resolved_plan["_resolved_plan_path"] = str(resolved_path)
    return resolved_plan


def _max_recommended_start_delay_minutes(predictions: List[Dict[str, Any]], min_altitude_deg: float) -> float:
    """How much slack (minutes) the plan can tolerate before RUN should refuse as stale.

    Conservative: the smallest observed altitude margin over the whole
    session, converted to an approximate time budget using ~0.25 deg/min
    (a conservative diurnal rate bound near the celestial equator; using a
    single conservative rate avoids re-deriving per-point rates here).
    """
    if not predictions:
        return 0.0
    margins = [p["predicted_altitude_deg"] - min_altitude_deg for p in predictions]
    smallest_margin = max(0.0, min(margins))
    conservative_rate_deg_per_min = 0.25
    return round(smallest_margin / conservative_rate_deg_per_min, 1)


def _static_safety_policy() -> Dict[str, Any]:
    return {
        "never_automated": [
            "SYNC mount", "HOME mount", "alter alignment", "correct OnStep clock",
            "change observer location", "lower hard horizon limit", "bypass altitude gates",
            "force rejected GOTO", "retry unsafe GOTO", "change MAIN gain during active science",
            "enable AGC", "restart rtl_tcp.service",
        ],
        "planning_altitude_floor_deg": "operator-configured; never overrides capture.py's own "
                                        "--min-altitude runtime gate or OnStep's independent hard limits",
    }


def _software_versions() -> Dict[str, Any]:
    versions = {"python": sys.version.split()[0]}
    for pkg in ("astropy", "numpy", "h5py", "yaml"):
        try:
            versions[pkg] = importlib.metadata.version(pkg if pkg != "yaml" else "PyYAML")
        except Exception:
            versions[pkg] = "unknown"
    return versions
