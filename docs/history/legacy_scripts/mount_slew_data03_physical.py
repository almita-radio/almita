#!/usr/bin/env python3
"""Execute the frozen MODEL-03 plan with per-transition geometry checks."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from astropy.coordinates import AltAz, CIRS, EarthLocation, ICRS, SkyCoord
from astropy.time import Time
import astropy.units as u

from indi_telescope_control import INDITelescopeControl
from mount_benchmark import angular_distance_deg, axis_motion_class, load_observer, observer_location, wrapped_delta_ra_hours
from mount_slew_data03 import generate_plan


PLAN = Path("mount_benchmark_targeted_30_model03/mount_benchmark_plan.csv")
OUTPUT = Path("mount_benchmark_targeted_30_model03_physical")
RESULT = OUTPUT / "mount_benchmark.csv"
MIN_ALTITUDE = 30.0
DEVICE = "LX200 OnStep"


def read_plan() -> list[dict[str, str]]:
    with PLAN.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def sign(value: float, epsilon: float = 0.01) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def bool_value(value: object) -> bool:
    return str(value).lower() in {"true", "1", "yes"}


def exact_ha_band(value: float) -> str:
    if -1 <= value < -0.5: return "-1.0--0.5"
    if -0.5 <= value < 0: return "-0.5-0"
    if 0 <= value < 0.5: return "0-0.5"
    if 0.5 <= value <= 1: return "0.5-1.0"
    return "outside"


def indi_snapshot() -> dict[str, str]:
    queries = [
        "CONNECTION._STATE", "CONNECTION.CONNECT", "EQUATORIAL_EOD_COORD._STATE",
        "TELESCOPE_MOTION_NS._STATE", "TELESCOPE_MOTION_NS.MOTION_NORTH", "TELESCOPE_MOTION_NS.MOTION_SOUTH",
        "TELESCOPE_MOTION_WE._STATE", "TELESCOPE_MOTION_WE.MOTION_WEST", "TELESCOPE_MOTION_WE.MOTION_EAST",
        "TELESCOPE_TRACK_STATE._STATE", "TELESCOPE_TRACK_STATE.TRACK_ON", "TELESCOPE_TRACK_STATE.TRACK_OFF",
        "TELESCOPE_HOME._STATE", "TELESCOPE_HOME.GO", "SAFETY_STATUS._STATE", "SAFETY_STATUS.SAFETY",
    ]
    command = ["indi_getprop", "-h", "localhost", "-p", "7624"] + [f"{DEVICE}.{q}" for q in queries]
    output = subprocess.check_output(command, text=True, timeout=5)
    return {line.split("=", 1)[0].split(f"{DEVICE}.", 1)[1]: line.split("=", 1)[1] for line in output.splitlines()}


def validate_snapshot(snapshot: dict[str, str]) -> tuple[bool, list[str]]:
    failures = []
    expected = {
        "CONNECTION._STATE": {"Ok", "Idle"}, "CONNECTION.CONNECT": {"On"},
        "EQUATORIAL_EOD_COORD._STATE": {"Ok", "Idle"},
        "TELESCOPE_MOTION_NS._STATE": {"Ok", "Idle"}, "TELESCOPE_MOTION_NS.MOTION_NORTH": {"Off"}, "TELESCOPE_MOTION_NS.MOTION_SOUTH": {"Off"},
        "TELESCOPE_MOTION_WE._STATE": {"Ok", "Idle"}, "TELESCOPE_MOTION_WE.MOTION_WEST": {"Off"}, "TELESCOPE_MOTION_WE.MOTION_EAST": {"Off"},
        "TELESCOPE_TRACK_STATE.TRACK_ON": {"Off"}, "TELESCOPE_TRACK_STATE.TRACK_OFF": {"On"},
        "SAFETY_STATUS._STATE": {"Ok", "Idle"}, "SAFETY_STATUS.SAFETY": {"Idle", "Ok"},
    }
    for key, allowed in expected.items():
        if snapshot.get(key) not in allowed: failures.append(f"{key}={snapshot.get(key)!r}")
    return not failures, failures


def write_csv(rows: list[dict[str, object]]) -> None:
    fields = list(rows[0]) if rows else []
    with RESULT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def transition_geometry(row: dict[str, str], start_ra: float, start_dec: float, location: EarthLocation) -> dict[str, object]:
    now = Time.now(); frame = AltAz(obstime=now, location=location)
    start = SkyCoord(ra=start_ra*u.hourangle, dec=start_dec*u.deg, frame=CIRS(obstime=now, location=location))
    target_icrs = SkyCoord(ra=float(row["target_ra_icrs_hours"])*u.hourangle, dec=float(row["target_dec_icrs_deg"])*u.deg, frame=ICRS())
    target = target_icrs.transform_to(CIRS(obstime=now, location=location)); local = target_icrs.transform_to(frame); start_local = start.transform_to(frame)
    lst = float(now.sidereal_time("apparent", longitude=location.lon).hour)
    target_ra, target_dec = float(target.ra.hour), float(target.dec.deg)
    dra = wrapped_delta_ra_hours(start_ra, target_ra)*15; ddec = target_dec-start_dec
    start_ha, target_ha = wrapped_delta_ra_hours(start_ra, lst), wrapped_delta_ra_hours(target_ra, lst)
    return {
        "timestamp_geometry_utc": now.utc.isot, "local_sidereal_time_hours": lst,
        "start_ra_eod_hours": start_ra, "start_dec_eod_deg": start_dec,
        "start_alt_deg": float(start_local.alt.deg), "start_az_deg": float(start_local.az.deg), "start_ha_hours": start_ha,
        "target_ra_eod_hours": target_ra, "target_dec_eod_deg": target_dec,
        "target_alt_deg": float(local.alt.deg), "target_az_deg": float(local.az.deg), "target_ha_hours": target_ha,
        "angular_distance_deg": angular_distance_deg(start_ra,start_dec,target_ra,target_dec),
        "delta_ra_deg_equivalent": dra, "delta_dec_deg": ddec, "axis_motion_class": axis_motion_class(dra,ddec),
        "mean_abs_dec_deg": (abs(start_dec)+abs(target_dec))/2,
        "delta_ha_hours": target_ha-start_ha, "min_abs_ha_along_transition": 0.0 if start_ha*target_ha<0 else min(abs(start_ha),abs(target_ha)),
        "actual_ha_zero_crossing": start_ha*target_ha<0, "actual_ha_band": exact_ha_band(target_ha),
    }


def geometry_matches(row: dict[str, str], g: dict[str, object], previous: dict[str, object] | None) -> tuple[bool, str]:
    if float(g["target_alt_deg"]) < MIN_ALTITUDE: return False, "deferred_visibility"
    group = row["experiment_group"]
    if group == "ra_short":
        if float(g["angular_distance_deg"]) > 5 or g["axis_motion_class"] != "ra_dominant": return False, "deferred_short_geometry"
        intended = bool_value(row["intended_ra_reversal"])
        actual = bool(previous) and sign(float(g["delta_ra_deg_equivalent"])) != sign(float(previous["delta_ra_deg_equivalent"]))
        if actual != intended: return False, "deferred_ra_reversal_mismatch"
    if group == "meridian":
        if not 10 <= float(g["angular_distance_deg"]) <= 60 or g["actual_ha_band"] != row["intended_ha_band"]: return False, "deferred_meridian_geometry"
    if bool(g["actual_ha_zero_crossing"]) != bool_value(row["intended_ha_zero_crossing"]): return False, "deferred_ha_crossing_mismatch"
    return True, ""


async def run() -> None:
    if OUTPUT.exists() and any(OUTPUT.iterdir()): raise FileExistsError(f"refusing to overwrite {OUTPUT}")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    original_plan = read_plan(); (OUTPUT/"original_dry_run_plan.csv").write_bytes(PLAN.read_bytes())
    initial = indi_snapshot(); ok, failures = validate_snapshot(initial)
    metadata = {"campaign_name":"targeted_30_model03_physical", "started_at":Time.now().utc.isot, "requested":len(original_plan), "precheck":initial, "precheck_pass":ok, "precheck_failures":failures,
                "plan_temporally_recomputed":True,"plan_replacement_reason":"frozen geometry no longer preserved one reversal and three intended HA bands","source_plan":str(PLAN)}
    (OUTPUT/"mount_benchmark_metadata.json").write_text(json.dumps(metadata,indent=2)+"\n")
    if not ok: raise RuntimeError(f"initial precheck failed: {failures}")
    controller = INDITelescopeControl(device_name=DEVICE, verbose=False); results=[]; previous=None; previous_end=None
    if not await controller.connect(): raise RuntimeError("could not connect to mount")
    try:
        real_ra,real_dec=await controller.get_coordinates(force_refresh=True)
        if real_ra is None or real_dec is None: raise RuntimeError("mount did not provide coordinates for temporal replanning")
        plan=generate_plan(location,float(real_ra),float(real_dec),Time.now())
        with (OUTPUT/"mount_benchmark_plan.csv").open("w",newline="",encoding="utf-8") as handle:
            writer=csv.DictWriter(handle,fieldnames=list(plan[0])); writer.writeheader(); writer.writerows(plan)
        for row in plan:
            snapshot=indi_snapshot(); state_ok,state_failures=validate_snapshot(snapshot)
            if not state_ok: raise RuntimeError(f"pre-movement check failed: {state_failures}")
            start_ra,start_dec=await controller.get_coordinates(force_refresh=True)
            if start_ra is None or start_dec is None or not all(map(math.isfinite,[start_ra,start_dec])): raise RuntimeError("invalid real coordinates")
            g=transition_geometry(row,float(start_ra),float(start_dec),location)
            valid,reason=geometry_matches(row,g,previous)
            actual_reversal = "" if previous is None else sign(float(g["delta_ra_deg_equivalent"])) != sign(float(previous["delta_ra_deg_equivalent"]))
            actual_dec_reversal = "" if previous is None else sign(float(g["delta_dec_deg"])) != sign(float(previous["delta_dec_deg"]))
            base={**row,**g,"actual_ra_reversal":actual_reversal,"actual_dec_reversal":actual_dec_reversal,
                  "previous_sample_id":"" if previous is None else previous["sample_id"],"previous_distance_deg":"" if previous is None else previous["angular_distance_deg"],
                  "previous_duration_sec":"" if previous is None else previous["goto_duration_external_sec"],"previous_delta_ra_sign":"" if previous is None else sign(float(previous["delta_ra_deg_equivalent"])),
                  "previous_delta_dec_sign":"" if previous is None else sign(float(previous["delta_dec_deg"])),"ra_direction_reversal":actual_reversal,"dec_direction_reversal":actual_dec_reversal,
                  "seconds_since_previous_goto":"" if previous_end is None else time.perf_counter()-previous_end,"success":False,"result":reason or "pending","error_message":"",
                  "timestamp_command_utc":"","timestamp_end_utc":"","goto_duration_external_sec":0.0,"goto_duration_controller_sec":"","end_ra_eod_hours":"","end_dec_eod_deg":"","final_pointing_error_deg":""}
            if not valid:
                base["timestamp_end_utc"]=Time.now().utc.isot; results.append(base); write_csv(results); continue
            started=time.perf_counter(); base["timestamp_command_utc"]=Time.now().utc.isot
            success=bool(await controller.goto(float(g["target_ra_eod_hours"]),float(g["target_dec_eod_deg"])))
            base["goto_duration_external_sec"]=time.perf_counter()-started; base["goto_duration_controller_sec"]=controller.last_slew_command_to_ok_sec or ""
            end_ra,end_dec=await controller.get_coordinates(force_refresh=True); base["end_ra_eod_hours"],base["end_dec_eod_deg"]=end_ra,end_dec
            if end_ra is not None and end_dec is not None: base["final_pointing_error_deg"]=angular_distance_deg(float(g["target_ra_eod_hours"]),float(g["target_dec_eod_deg"]),end_ra,end_dec)
            base["success"]=success; base["result"]="success" if success else "failed"; base["error_message"]="" if success else "controller.goto returned False"; base["timestamp_end_utc"]=Time.now().utc.isot
            results.append(base); write_csv(results)
            if not success: break
            previous=base; previous_end=time.perf_counter()
    finally:
        try: await controller.set_tracking(False)
        finally: await controller.disconnect()
    final=indi_snapshot(); final_ok,final_failures=validate_snapshot(final)
    metadata.update({"ended_at":Time.now().utc.isot,"executed":len(results),"successful":sum(bool_value(r["success"]) for r in results),"failed":sum(r["result"]=="failed" for r in results),"deferred":sum(str(r["result"]).startswith("deferred") for r in results),"final_state":final,"final_state_pass":final_ok,"final_state_failures":final_failures})
    (OUTPUT/"mount_benchmark_metadata.json").write_text(json.dumps(metadata,indent=2)+"\n")


if __name__ == "__main__":
    location=observer_location(load_observer("observer_config.json")); asyncio.run(run())
