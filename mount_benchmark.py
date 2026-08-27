#!/usr/bin/env python3
"""Plan and measure ALMITA mount slews without duplicating INDI logic."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import subprocess
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from astropy_offline import configure_astropy_offline
configure_astropy_offline()
import astropy.units as u
import matplotlib
import numpy as np
from astropy.coordinates import AltAz, CIRS, EarthLocation, ICRS, SkyCoord
from astropy.time import Time

matplotlib.use("Agg")
import matplotlib.pyplot as plt


PLAN_FIELDS = [
    "benchmark_id", "campaign_name", "target_cell", "sample_id", "source_point_id", "target_point_id", "seed",
    "generated_at_utc", "target_ra_icrs_hours", "target_dec_icrs_deg",
    "target_ra_eod_hours", "target_dec_eod_deg", "target_alt_deg",
    "target_az_deg", "planned_angular_distance_deg", "distance_band",
    "planned_axis_motion_class", "planned_target_ha_hours",
]

RESULT_FIELDS = [
    "benchmark_id", "campaign_name", "target_cell", "sample_id", "source_point_id", "target_point_id", "seed",
    "timestamp_start_utc", "timestamp_command_utc", "timestamp_end_utc",
    "local_sidereal_time_hours", "start_ra_eod_hours", "start_dec_eod_deg",
    "start_alt_deg", "start_az_deg", "start_ha_hours",
    "target_ra_icrs_hours", "target_dec_icrs_deg", "target_ra_eod_hours",
    "target_dec_eod_deg", "planned_target_alt", "actual_target_alt_at_send",
    "target_alt_deg", "target_az_deg",
    "target_ha_hours", "angular_distance_deg", "delta_ra_hours_wrapped",
    "delta_ra_deg_equivalent", "abs_delta_ra_hours", "delta_dec_deg",
    "abs_delta_dec_deg", "delta_alt_deg", "delta_az_deg_wrapped",
    "distance_band", "axis_motion_class", "ha_band", "end_ra_eod_hours",
    "end_dec_eod_deg", "final_pointing_error_deg",
    "goto_duration_external_sec", "goto_duration_controller_sec",
    "settle_duration_sec", "total_duration_sec", "success", "result",
    "error_message",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_observer(config_path: Path | str) -> dict[str, Any]:
    with Path(config_path).open(encoding="utf-8") as handle:
        config = json.load(handle)
    observer = config["observer"]
    return {
        "name": observer.get("name", "Unknown"),
        "latitude_deg": float(observer["latitude_deg"]),
        "longitude_deg": float(observer["longitude_deg"]),
        "elevation_m": float(observer.get("elevation_m", 0.0)),
    }


def observer_location(observer: dict[str, Any]) -> EarthLocation:
    return EarthLocation(
        lat=observer["latitude_deg"] * u.deg,
        lon=observer["longitude_deg"] * u.deg,
        height=observer["elevation_m"] * u.m,
    )


def angular_distance_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    first = SkyCoord(ra=ra1 * u.hourangle, dec=dec1 * u.deg, frame="cirs")
    second = SkyCoord(ra=ra2 * u.hourangle, dec=dec2 * u.deg, frame="cirs")
    return float(first.separation(second).deg)


def wrapped_delta_ra_hours(start: float, target: float) -> float:
    return (target - start + 12.0) % 24.0 - 12.0


def distance_band(distance_deg: float) -> str:
    if distance_deg <= 5.0:
        return "0-5"
    if distance_deg <= 10.0:
        return ">5-10"
    if distance_deg <= 30.0:
        return ">10-30"
    if distance_deg <= 60.0:
        return ">30-60"
    return ">60"


def wrapped_delta_deg(start: float, target: float) -> float:
    return (target - start + 180.0) % 360.0 - 180.0


def axis_motion_class(delta_ra_deg: float, delta_dec_deg: float) -> str:
    ra, dec = abs(delta_ra_deg), abs(delta_dec_deg)
    if ra <= 5.0 and dec <= 5.0:
        return "small"
    if ra > 1.5 * dec:
        return "ra_dominant"
    if dec > 1.5 * ra:
        return "dec_dominant"
    return "mixed"


TARGETED_QUOTAS = {
    "ra_dominant_short": 8,
    "mixed_short": 8,
    "dec_dominant_long": 6,
    "mixed_long": 6,
    "ra_dominant_meridian": 8,
}


def targeted_cell(distance: float, motion_class: str, target_ha: float) -> str | None:
    """Classify a transition using the unchanged benchmark definitions."""
    if 0.0 < distance <= 5.0 and motion_class == "ra_dominant":
        return "ra_dominant_short"
    if 0.0 < distance <= 5.0 and motion_class == "mixed":
        return "mixed_short"
    if distance > 60.0 and motion_class == "dec_dominant":
        return "dec_dominant_long"
    if distance > 60.0 and motion_class == "mixed":
        return "mixed_long"
    if 10.0 <= distance <= 60.0 and motion_class == "ra_dominant" and abs(target_ha) <= 1.0:
        return "ra_dominant_meridian"
    return None


def _targeted_sequence() -> list[str]:
    # Long DEC moves leave the high-|DEC| region needed to make a <=5 degree
    # mixed mechanical move possible. A long mixed move returns there before
    # the next short/meridian transitions. Categories remain interleaved.
    cycle = ["ra_dominant_short", "mixed_short", "dec_dominant_long",
             "mixed_long", "ra_dominant_meridian"]
    sequence = cycle * 6
    sequence += ["ra_dominant_short", "mixed_short", "ra_dominant_meridian"] * 2
    return sequence


def generate_targeted_plan(
    min_altitude_deg: float, seed: int, location: EarthLocation,
    start_ra_hours: float, start_dec_deg: float,
    obstime: Time | None = None, benchmark_id: str | None = None,
) -> list[dict[str, Any]]:
    """Greedily build the reproducible, interleaved targeted-36 campaign."""
    obstime = obstime or Time.now()
    benchmark_id = benchmark_id or f"almita-targeted-{obstime.utc.strftime('%Y%m%dT%H%M%S')}-{seed}"
    rng = np.random.default_rng(seed)
    # Short mixed-axis transitions occupy a very small fraction of the visible
    # hemisphere; a dense deterministic pool avoids relaxing their definition.
    count = 200000
    azimuth = rng.uniform(0, 360, count)
    altitude = np.degrees(np.arcsin(rng.uniform(math.sin(math.radians(min_altitude_deg)), 1, count)))
    local = SkyCoord(az=azimuth * u.deg, alt=altitude * u.deg,
                     frame=AltAz(obstime=obstime, location=location))
    icrs = local.transform_to(ICRS()); eod = icrs.transform_to(CIRS(obstime=obstime, location=location))
    lst = float(obstime.sidereal_time("apparent", longitude=location.lon).hour)
    available = set(range(count)); current_ra, current_dec = start_ra_hours, start_dec_deg
    all_ra = np.asarray(eod.ra.hour); all_dec = np.asarray(eod.dec.deg)
    all_ha = (lst - all_ra + 12.0) % 24.0 - 12.0
    plan = []
    meridian_sign = -1
    for sample_id, wanted in enumerate(_targeted_sequence(), 1):
        candidates = np.array(sorted(available), dtype=int)
        target_ra, target_dec = all_ra[candidates], all_dec[candidates]
        dra = ((target_ra - current_ra + 12.0) % 24.0 - 12.0) * 15.0
        ddec = target_dec - current_dec
        dec1, dec2 = np.radians(current_dec), np.radians(target_dec)
        cos_distance = np.sin(dec1) * np.sin(dec2) + np.cos(dec1) * np.cos(dec2) * np.cos(np.radians(dra))
        distances = np.degrees(np.arccos(np.clip(cos_distance, -1.0, 1.0)))
        ra_abs, dec_abs = np.abs(dra), np.abs(ddec)
        classes = np.full(len(candidates), "mixed", dtype="U12")
        small = (ra_abs <= 5) & (dec_abs <= 5)
        classes[small] = "small"
        classes[(~small) & (ra_abs > 1.5 * dec_abs)] = "ra_dominant"
        classes[(~small) & (dec_abs > 1.5 * ra_abs)] = "dec_dominant"
        ha = all_ha[candidates]
        if wanted == "ra_dominant_short":
            mask = (distances > 0) & (distances <= 5) & (classes == "ra_dominant")
        elif wanted == "mixed_short":
            mask = (distances > 0) & (distances <= 5) & (classes == "mixed")
        elif wanted == "dec_dominant_long":
            mask = (distances > 60) & (classes == "dec_dominant")
        elif wanted == "mixed_long":
            mask = (distances > 60) & (classes == "mixed") & (target_dec <= -55)
        else:
            mask = (distances >= 10) & (distances <= 60) & (classes == "ra_dominant") & (np.abs(ha) <= 1)
            mask &= target_dec <= -55
            mask &= (ha < 0) if meridian_sign < 0 else (ha >= 0)
        chosen_pool = candidates[mask][:200].tolist()
        if not chosen_pool:
            raise RuntimeError(f"could not generate safe geometry for targeted cell {wanted}")
        index = int(rng.choice(chosen_pool)); available.remove(index)
        target_ra, target_dec = float(eod[index].ra.hour), float(eod[index].dec.deg)
        distance = angular_distance_deg(current_ra, current_dec, target_ra, target_dec)
        previous_id = plan[-1]["target_point_id"] if plan else "START_REAL"
        plan.append({
            "benchmark_id": benchmark_id, "campaign_name": "targeted_36", "target_cell": wanted,
            "sample_id": sample_id, "source_point_id": previous_id, "target_point_id": f"T{index + 1:05d}",
            "seed": seed, "generated_at_utc": obstime.utc.isot,
            "target_ra_icrs_hours": float(icrs[index].ra.hour), "target_dec_icrs_deg": float(icrs[index].dec.deg),
            "target_ra_eod_hours": target_ra, "target_dec_eod_deg": target_dec,
            "target_alt_deg": float(local[index].alt.deg), "target_az_deg": float(local[index].az.deg),
            "planned_angular_distance_deg": distance, "distance_band": distance_band(distance),
            "planned_axis_motion_class": axis_motion_class(
                wrapped_delta_ra_hours(current_ra, target_ra) * 15.0, target_dec - current_dec
            ),
            "planned_target_ha_hours": wrapped_delta_ra_hours(target_ra, lst),
        })
        current_ra, current_dec = target_ra, target_dec
        if wanted == "ra_dominant_meridian":
            meridian_sign *= -1
    return plan


def ha_band(ha_hours: float) -> str:
    if ha_hours < -4.0:
        return "<-4"
    if ha_hours < -2.0:
        return "-4--2"
    if ha_hours < -1.0:
        return "-2--1"
    if ha_hours < 0.0:
        return "-1-0"
    if ha_hours < 1.0:
        return "0-1"
    if ha_hours < 2.0:
        return "1-2"
    if ha_hours <= 4.0:
        return "2-4"
    return ">4"


def _mixed_order(coords: SkyCoord, rng: np.random.Generator, requested_count: int) -> list[int]:
    """Choose a reproducible path with deliberate coverage of five slew bands."""
    count = len(coords)
    requested = min(count, requested_count)
    if requested < 2:
        return list(range(requested))
    remaining = set(range(count))
    current = int(rng.integers(count))
    order = [current]
    remaining.remove(current)
    weights = [("0-5", 15), (">5-10", 15), (">10-30", 25), (">30-60", 25), (">60", 20)]
    desired = [band for band, weight in weights for _ in range(weight)]
    rng.shuffle(desired)
    for step in range(1, requested):
        candidates = np.array(sorted(remaining), dtype=int)
        separations = coords[current].separation(coords[candidates]).deg
        wanted = desired[(step - 1) % len(desired)]
        matches = [
            int(idx) for idx, separation in zip(candidates, separations)
            if distance_band(float(separation)) == wanted
        ]
        chosen = int(rng.choice(matches if matches else candidates))
        order.append(chosen)
        remaining.remove(chosen)
        current = chosen
    return order


def generate_plan(
    samples: int,
    min_altitude_deg: float,
    seed: int,
    location: EarthLocation,
    obstime: Time | None = None,
    benchmark_id: str | None = None,
) -> list[dict[str, Any]]:
    if samples <= 0:
        raise ValueError("samples must be positive")
    if not -90.0 < min_altitude_deg < 90.0:
        raise ValueError("min_altitude must be between -90 and 90 degrees")
    obstime = obstime or Time.now()
    benchmark_id = benchmark_id or f"almita-{obstime.utc.strftime('%Y%m%dT%H%M%S')}-{seed}"
    rng = np.random.default_rng(seed)
    candidate_count = max(samples * 30, 3000)
    azimuth = rng.uniform(0.0, 360.0, candidate_count)
    sin_altitude = rng.uniform(math.sin(math.radians(min_altitude_deg)), 1.0, candidate_count)
    altitude = np.degrees(np.arcsin(sin_altitude))
    local = SkyCoord(
        az=azimuth * u.deg,
        alt=altitude * u.deg,
        frame=AltAz(obstime=obstime, location=location),
    )
    icrs = local.transform_to(ICRS())
    order = _mixed_order(icrs, rng, samples)
    generated_at = obstime.utc.isot
    plan: list[dict[str, Any]] = []
    for sample_index, point_index in enumerate(order, 1):
        point_icrs = icrs[point_index]
        point_local = local[point_index]
        point_eod = point_icrs.transform_to(CIRS(obstime=obstime, location=location))
        previous = plan[-1] if plan else None
        planned_distance = (
            angular_distance_deg(
                previous["target_ra_eod_hours"], previous["target_dec_eod_deg"],
                float(point_eod.ra.hour), float(point_eod.dec.deg),
            ) if previous else 0.0
        )
        plan.append({
            "benchmark_id": benchmark_id,
            "campaign_name": "general",
            "target_cell": "",
            "sample_id": sample_index,
            "source_point_id": previous["target_point_id"] if previous else "START_REAL",
            "target_point_id": f"P{point_index + 1:03d}",
            "seed": seed,
            "generated_at_utc": generated_at,
            "target_ra_icrs_hours": float(point_icrs.ra.hour),
            "target_dec_icrs_deg": float(point_icrs.dec.deg),
            "target_ra_eod_hours": float(point_eod.ra.hour),
            "target_dec_eod_deg": float(point_eod.dec.deg),
            "target_alt_deg": float(point_local.alt.deg),
            "target_az_deg": float(point_local.az.deg),
            "planned_angular_distance_deg": planned_distance,
            "distance_band": distance_band(planned_distance) if previous else "START",
            "planned_axis_motion_class": "",
            "planned_target_ha_hours": "",
        })
    return plan


def _to_float(value: Any) -> float | None:
    return None if value is None else float(value)


async def execute_plan(
    controller: Any,
    plan: Iterable[dict[str, Any]],
    location: EarthLocation,
    min_altitude_deg: float,
    settle_seconds: float,
    *,
    perf_counter: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], Any] = asyncio.sleep,
    time_now: Callable[[], Time] = Time.now,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for target in plan:
        timestamp_start = utc_now_iso()
        start_ra, start_dec = await controller.get_coordinates(force_refresh=True)
        if start_ra is None or start_dec is None:
            raise RuntimeError("mount did not provide a real starting position")
        obstime = time_now()
        local_frame = AltAz(obstime=obstime, location=location)
        start_coord = SkyCoord(
            ra=start_ra * u.hourangle, dec=start_dec * u.deg,
            frame=CIRS(obstime=obstime, location=location),
        )
        start_local = start_coord.transform_to(local_frame)
        target_icrs = SkyCoord(
            ra=target["target_ra_icrs_hours"] * u.hourangle,
            dec=target["target_dec_icrs_deg"] * u.deg,
            frame=ICRS(),
        )
        target_eod = target_icrs.transform_to(CIRS(obstime=obstime, location=location))
        target_local = target_icrs.transform_to(local_frame)
        lst = float(obstime.sidereal_time("apparent", longitude=location.lon).hour)
        target_ra = float(target_eod.ra.hour)
        target_dec = float(target_eod.dec.deg)
        delta_ra_hours = wrapped_delta_ra_hours(float(start_ra), target_ra)
        delta_ra_deg = delta_ra_hours * 15.0
        delta_dec = target_dec - float(start_dec)
        target_alt = float(target_local.alt.deg)
        target_az = float(target_local.az.deg)
        target_ha = wrapped_delta_ra_hours(target_ra, lst)
        base = {
            "benchmark_id": target["benchmark_id"],
            "campaign_name": target.get("campaign_name", "general"),
            "target_cell": target.get("target_cell", ""),
            "sample_id": target["sample_id"],
            "source_point_id": target["source_point_id"],
            "target_point_id": target["target_point_id"],
            "seed": target["seed"],
            "timestamp_start_utc": timestamp_start,
            "timestamp_command_utc": None,
            "timestamp_end_utc": None,
            "local_sidereal_time_hours": lst,
            "start_ra_eod_hours": float(start_ra),
            "start_dec_eod_deg": float(start_dec),
            "start_alt_deg": float(start_local.alt.deg),
            "start_az_deg": float(start_local.az.deg),
            "start_ha_hours": wrapped_delta_ra_hours(float(start_ra), lst),
            "target_ra_icrs_hours": target["target_ra_icrs_hours"],
            "target_dec_icrs_deg": target["target_dec_icrs_deg"],
            "target_ra_eod_hours": target_ra,
            "target_dec_eod_deg": target_dec,
            "planned_target_alt": target["target_alt_deg"],
            "actual_target_alt_at_send": target_alt,
            "target_alt_deg": target_alt,
            "target_az_deg": target_az,
            "target_ha_hours": target_ha,
            "angular_distance_deg": angular_distance_deg(start_ra, start_dec, target_ra, target_dec),
            "delta_ra_hours_wrapped": delta_ra_hours,
            "delta_ra_deg_equivalent": delta_ra_deg,
            "abs_delta_ra_hours": abs(delta_ra_hours),
            "delta_dec_deg": delta_dec,
            "abs_delta_dec_deg": abs(delta_dec),
            "delta_alt_deg": target_alt - float(start_local.alt.deg),
            "delta_az_deg_wrapped": wrapped_delta_deg(float(start_local.az.deg), target_az),
            "distance_band": "",
            "axis_motion_class": axis_motion_class(delta_ra_deg, delta_dec),
            "ha_band": ha_band(target_ha),
            "end_ra_eod_hours": None,
            "end_dec_eod_deg": None,
            "final_pointing_error_deg": None,
            "goto_duration_external_sec": 0.0,
            "goto_duration_controller_sec": None,
            "settle_duration_sec": 0.0,
            "total_duration_sec": 0.0,
            "success": False,
            "result": "failed",
            "error_message": "",
        }
        base["distance_band"] = distance_band(base["angular_distance_deg"])
        if base["campaign_name"] == "targeted_36":
            actual_cell = targeted_cell(
                base["angular_distance_deg"], base["axis_motion_class"], base["target_ha_hours"]
            )
            if actual_cell is None:
                base["timestamp_end_utc"] = utc_now_iso()
                base["result"] = "deferred_cell_mismatch"
                base["error_message"] = "real transition no longer matches any targeted cell"
                results.append(base)
                continue
            base["target_cell"] = actual_cell
        if base["actual_target_alt_at_send"] < min_altitude_deg:
            base["timestamp_end_utc"] = utc_now_iso()
            base["result"] = "deferred_visibility"
            base["error_message"] = "target below minimum altitude immediately before GOTO"
            results.append(base)
            continue
        started = perf_counter()
        base["timestamp_command_utc"] = utc_now_iso()
        try:
            success = bool(await controller.goto(target_ra, target_dec))
            base["goto_duration_external_sec"] = perf_counter() - started
            base["goto_duration_controller_sec"] = _to_float(
                getattr(controller, "last_slew_command_to_ok_sec", None)
            )
            end_ra, end_dec = await controller.get_coordinates(force_refresh=True)
            base["end_ra_eod_hours"] = _to_float(end_ra)
            base["end_dec_eod_deg"] = _to_float(end_dec)
            if end_ra is not None and end_dec is not None:
                base["final_pointing_error_deg"] = angular_distance_deg(
                    target_ra, target_dec, end_ra, end_dec
                )
            if success and settle_seconds > 0:
                settle_started = perf_counter()
                await sleep(settle_seconds)
                base["settle_duration_sec"] = perf_counter() - settle_started
            base["success"] = success
            base["result"] = "success" if success else "failed"
            if not success:
                base["error_message"] = "controller.goto returned False"
        except Exception as exc:
            base["goto_duration_external_sec"] = perf_counter() - started
            base["error_message"] = f"{type(exc).__name__}: {exc}"
        base["timestamp_end_utc"] = utc_now_iso()
        base["total_duration_sec"] = (
            base["goto_duration_external_sec"] + base["settle_duration_sec"]
        )
        results.append(base)
        if not base["success"]:
            break
    return results


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_plan(path: Path) -> list[dict[str, Any]]:
    """Load a persisted plan with numeric fields restored for safe resumption."""
    rows = _read_csv(path)
    integer_fields = {"sample_id", "seed"}
    numeric_fields = {
        "target_ra_icrs_hours", "target_dec_icrs_deg", "target_ra_eod_hours",
        "target_dec_eod_deg", "target_alt_deg", "target_az_deg",
        "planned_angular_distance_deg", "planned_target_ha_hours",
    }
    for row in rows:
        for field in integer_fields:
            row[field] = int(row[field])
        for field in numeric_fields:
            if row[field] != "":
                row[field] = float(row[field])
    return rows


def _git_hash() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def summarize(plan: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    successes = [
        row for row in results
        if row["success"] is True or str(row["success"]).lower() == "true"
    ]
    durations = np.array([row["goto_duration_external_sec"] for row in successes], dtype=float)
    distances = np.array([row["angular_distance_deg"] for row in successes], dtype=float)
    errors = np.array([
        row["final_pointing_error_deg"] for row in successes
        if row["final_pointing_error_deg"] is not None
    ], dtype=float)
    def distribution(field: str, include_percentiles: bool = False) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key in sorted({str(row[field]) for row in successes}):
            values = np.array([
                float(row["goto_duration_external_sec"])
                for row in successes if str(row[field]) == key
            ])
            stats = {
                "count": int(values.size),
                "mean_duration_sec": float(np.mean(values)),
                "median_duration_sec": float(np.median(values)),
            }
            if include_percentiles:
                stats.update({
                    "p90_duration_sec": float(np.percentile(values, 90)),
                    "p95_duration_sec": float(np.percentile(values, 95)),
                })
            output[key] = stats
        return output

    return {
        "total_planned": len(plan),
        "total_executed": len(results),
        "successful_samples": len(successes),
        "failed_samples": sum(row["result"] == "failed" for row in results),
        "deferred_samples": sum(str(row["result"]).startswith("deferred_") for row in results),
        "goto_duration_mean_sec": float(np.mean(durations)) if durations.size else None,
        "goto_duration_median_sec": float(np.median(durations)) if durations.size else None,
        "goto_duration_p50_sec": float(np.percentile(durations, 50)) if durations.size else None,
        "goto_duration_p75_sec": float(np.percentile(durations, 75)) if durations.size else None,
        "goto_duration_p90_sec": float(np.percentile(durations, 90)) if durations.size else None,
        "goto_duration_p95_sec": float(np.percentile(durations, 95)) if durations.size else None,
        "goto_duration_p99_sec": float(np.percentile(durations, 99)) if durations.size else None,
        "goto_duration_min_sec": float(np.min(durations)) if durations.size else None,
        "goto_duration_max_sec": float(np.max(durations)) if durations.size else None,
        "angular_distance_mean_deg": float(np.mean(distances)) if distances.size else None,
        "pointing_error_mean_deg": float(np.mean(errors)) if errors.size else None,
        "pointing_error_median_deg": float(np.median(errors)) if errors.size else None,
        "pointing_error_p90_deg": float(np.percentile(errors, 90)) if errors.size else None,
        "pointing_error_p95_deg": float(np.percentile(errors, 95)) if errors.size else None,
        "pointing_error_max_deg": float(np.max(errors)) if errors.size else None,
        "distance_band_distribution": distribution("distance_band", True),
        "axis_motion_class_distribution": distribution("axis_motion_class"),
        "ha_band_distribution": distribution("ha_band"),
        "target_cell_distribution": distribution("target_cell") if successes and "target_cell" in successes[0] else {},
    }


def _plot_plan(plan: list[dict[str, Any]], path: Path, min_altitude: float) -> None:
    fig, ax = plt.subplots(figsize=(8, 8), subplot_kw={"projection": "polar"})
    az = np.radians([row["target_az_deg"] for row in plan])
    radius = 90.0 - np.array([row["target_alt_deg"] for row in plan])
    ax.plot(az, radius, color="0.65", linewidth=0.7, alpha=0.8)
    points = ax.scatter(az, radius, c=np.arange(len(plan)), cmap="turbo", s=18)
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_ylim(0, 90.0 - min_altitude)
    ax.set_title("ALMITA mount benchmark plan (ordered path)")
    fig.colorbar(points, ax=ax, label="Execution order")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _plot_distances(plan: list[dict[str, Any]], path: Path) -> None:
    values = [row["planned_angular_distance_deg"] for row in plan[1:]]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(values, bins=np.arange(0, 185, 10), edgecolor="black")
    ax.set_xlabel("Angular distance (deg)")
    ax.set_ylabel("Count")
    ax.set_title("Planned slew distance distribution")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _plot_axis_motion(results: list[dict[str, Any]], path: Path) -> None:
    rows = [row for row in results if str(row.get("success", "")).lower() == "true"]
    fig, ax = plt.subplots(figsize=(7, 6))
    for label in ("ra_dominant", "dec_dominant", "mixed", "small"):
        selected = [row for row in rows if row["axis_motion_class"] == label]
        if selected:
            ax.scatter(
                [abs(float(row["delta_ra_deg_equivalent"])) for row in selected],
                [abs(float(row["delta_dec_deg"])) for row in selected],
                label=label, alpha=0.75,
            )
    ax.set_xlabel("abs(delta RA) equivalent (deg)")
    ax.set_ylabel("abs(delta DEC) (deg)")
    ax.set_title("Executed axis-motion coverage")
    if rows:
        ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _plot_ha_distribution(results: list[dict[str, Any]], path: Path) -> None:
    rows = [row for row in results if str(row.get("success", "")).lower() == "true"]
    values = [float(row["target_ha_hours"]) for row in rows]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(values, bins=[-12, -4, -2, -1, 0, 1, 2, 4, 12], edgecolor="black")
    ax.set_xlabel("Target hour angle (hours)")
    ax.set_ylabel("Count")
    ax.set_title("Executed target hour-angle distribution")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _plot_targeted_coverage(plan: list[dict[str, Any]], path: Path) -> None:
    labels = list(TARGETED_QUOTAS)
    counts = Counter(str(row.get("target_cell", "")) for row in plan)
    fig, ax = plt.subplots(figsize=(10, 5))
    positions = np.arange(len(labels))
    ax.bar(positions, [counts[label] for label in labels], label="Planned")
    ax.scatter(positions, [TARGETED_QUOTAS[label] for label in labels],
               color="black", marker="_", s=300, label="Target")
    ax.set_xticks(positions, labels, rotation=20, ha="right")
    ax.set_ylabel("Transitions")
    ax.set_title("Targeted campaign cell coverage")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def write_outputs(
    output_dir: Path,
    plan: list[dict[str, Any]],
    results: list[dict[str, Any]],
    metadata: dict[str, Any],
    min_altitude: float,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "mount_benchmark_plan.csv", plan, PLAN_FIELDS)
    _write_csv(output_dir / "mount_benchmark.csv", results, RESULT_FIELDS)
    summary = summarize(plan, results)
    metadata.update({
        "completed_samples": len(results),
        "successful_samples": summary["successful_samples"],
        "failed_samples": summary["failed_samples"],
    })
    (output_dir / "mount_benchmark_metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    (output_dir / "mount_benchmark_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    _plot_plan(plan, output_dir / "mount_benchmark_plan.png", min_altitude)
    _plot_distances(plan, output_dir / "mount_benchmark_distances.png")
    _plot_axis_motion(results, output_dir / "mount_benchmark_axis_motion.png")
    _plot_ha_distribution(results, output_dir / "mount_benchmark_ha_distribution.png")
    if metadata.get("campaign_name") == "targeted_36":
        _plot_targeted_coverage(plan, output_dir / "mount_benchmark_targeted_coverage.png")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--min-altitude", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=20260819)
    parser.add_argument("--settle-seconds", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--campaign", choices=["general", "targeted_36"], default="general")
    parser.add_argument(
        "--source-dataset", type=Path,
        default=Path("mount_benchmark_physical_100_final/mount_benchmark.csv"),
        help="Successful dataset whose final real position seeds targeted planning",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("mount_benchmark_output"))
    parser.add_argument(
        "--resume-dir", type=Path,
        help="Resume the next unprocessed sample from an existing benchmark directory",
    )
    parser.add_argument("--config", type=Path, default=Path("observer_config.json"))
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7624)
    parser.add_argument("--device", default="LX200 OnStep")
    return parser


async def async_main(args: argparse.Namespace) -> int:
    observer = load_observer(args.config)
    location = observer_location(observer)
    if args.resume_dir:
        if args.dry_run:
            raise ValueError("--resume-dir cannot be combined with --dry-run")
        output_dir = args.resume_dir
        plan = load_plan(output_dir / "mount_benchmark_plan.csv")
        results = _read_csv(output_dir / "mount_benchmark.csv")
        metadata = json.loads(
            (output_dir / "mount_benchmark_metadata.json").read_text(encoding="utf-8")
        )
        min_altitude = float(metadata["min_altitude_deg"])
        settle_seconds = float(metadata["settle_seconds"])
        completed_ids = {int(row["sample_id"]) for row in results}
        pending_plan = [row for row in plan if int(row["sample_id"]) not in completed_ids]
        metadata["resumed_at"] = utc_now_iso()
        metadata["resume_count"] = int(metadata.get("resume_count", 0)) + 1
    else:
        output_dir = args.output_dir
        if output_dir.exists() and any(output_dir.iterdir()):
            raise FileExistsError(f"refusing to overwrite non-empty output directory: {output_dir}")
        generated = Time.now()
        benchmark_id = f"almita-{generated.utc.strftime('%Y%m%dT%H%M%S')}-{args.seed}"
        if args.campaign == "targeted_36":
            source_rows = [row for row in _read_csv(args.source_dataset)
                           if str(row.get("success", "")).lower() == "true"]
            if not source_rows:
                raise ValueError("source dataset has no successful movements")
            last = source_rows[-1]
            plan = generate_targeted_plan(
                args.min_altitude, args.seed, location,
                float(last["end_ra_eod_hours"]), float(last["end_dec_eod_deg"]),
                generated, benchmark_id,
            )
        else:
            plan = generate_plan(
                args.samples, args.min_altitude, args.seed, location, generated, benchmark_id
            )
        metadata = {
            "benchmark_id": benchmark_id,
            "generated_at": generated.utc.isot,
            "observer": observer,
            "min_altitude_deg": args.min_altitude,
            "requested_samples": len(plan),
            "campaign_name": args.campaign,
            "source_dataset": str(args.source_dataset) if args.campaign == "targeted_36" else None,
            "seed": args.seed,
            "settle_seconds": args.settle_seconds,
            "controller": None if args.dry_run else args.device,
            "git_commit": _git_hash(),
            "dry_run": args.dry_run,
        }
        results = []
        pending_plan = plan
        min_altitude = args.min_altitude
        settle_seconds = args.settle_seconds
    if not args.dry_run:
        from indi_telescope_control import INDITelescopeControl

        controller = INDITelescopeControl(
            host=args.host, port=args.port, device_name=args.device, verbose=False
        )
        if not await controller.connect():
            raise RuntimeError("could not connect to mount")
        try:
            new_results = await execute_plan(
                controller, pending_plan, location, min_altitude, settle_seconds
            )
            results.extend(new_results)
        finally:
            try:
                await controller.set_tracking(False)
            finally:
                await controller.disconnect()
    write_outputs(output_dir, plan, results, metadata, min_altitude)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
