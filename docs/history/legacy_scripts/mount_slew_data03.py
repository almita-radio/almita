#!/usr/bin/env python3
"""Forensics and dry-run design for the targeted MOUNT-SLEW-DATA-03 campaign."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import astropy.units as u
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from astropy.coordinates import AltAz, CIRS, ICRS, SkyCoord
from astropy.time import Time
from scipy.stats import mannwhitneyu

import mount_benchmark as mb
from mount_slew_model import SlewModel


SOURCES = [
    ("baseline_100", Path("mount_benchmark_physical_100_final/mount_benchmark.csv")),
    ("targeted_36", Path("mount_benchmark_targeted_36_physical/mount_benchmark.csv")),
]
OUTPUT = Path("mount_benchmark_targeted_30_model03")
SEED = 20260820
HA_BANDS = [(-1.0, -0.5, "-1.0--0.5"), (-0.5, 0.0, "-0.5-0"),
            (0.0, 0.5, "0-0.5"), (0.5, 1.0, "0.5-1.0")]


def _sign(value: float, threshold: float = 1e-6) -> int:
    return 1 if value > threshold else -1 if value < -threshold else 0


def load_history() -> list[dict[str, object]]:
    model = SlewModel.load()
    output = []
    for source, path in SOURCES:
        raw = list(csv.DictReader(path.open()))
        successful = [r for r in raw if r["success"].lower() == "true" and r["result"] == "success"]
        previous = None
        for row in successful:
            current = dict(row); current["training_source"] = source
            numeric = ["goto_duration_external_sec", "angular_distance_deg", "delta_ra_hours_wrapped",
                       "delta_dec_deg", "start_ra_eod_hours", "start_dec_eod_deg", "target_ra_eod_hours",
                       "target_dec_eod_deg", "start_ha_hours", "target_ha_hours", "start_alt_deg", "start_az_deg",
                       "target_alt_deg", "target_az_deg"]
            for field in numeric: current[field] = float(current[field])
            prediction = model.predict_from_distance(current["angular_distance_deg"])["predicted_slew_sec"]
            current["predicted_duration_sec"] = prediction
            current["residual_sec"] = current["goto_duration_external_sec"] - prediction
            current["abs_delta_ra_deg"] = abs(current["delta_ra_hours_wrapped"] * 15)
            current["abs_delta_dec_deg"] = abs(current["delta_dec_deg"])
            current["delta_ha_hours"] = current["target_ha_hours"] - current["start_ha_hours"]
            current["min_abs_ha_along_transition"] = (0.0 if current["start_ha_hours"] * current["target_ha_hours"] < 0
                                                       else min(abs(current["start_ha_hours"]), abs(current["target_ha_hours"])))
            current["ha_zero_crossing"] = current["start_ha_hours"] * current["target_ha_hours"] < 0
            if previous is None:
                for field in ["previous_sample_id", "previous_distance_deg", "previous_duration_sec",
                              "previous_delta_ra_sign", "previous_delta_dec_sign", "previous_axis_motion_class",
                              "seconds_since_previous_goto"]: current[field] = None
                current["ra_direction_reversal"] = current["dec_direction_reversal"] = current["both_axes_reversal"] = False
            else:
                current["previous_sample_id"] = previous["sample_id"]
                current["previous_distance_deg"] = previous["angular_distance_deg"]
                current["previous_duration_sec"] = previous["goto_duration_external_sec"]
                pra, pdec = _sign(previous["delta_ra_hours_wrapped"]), _sign(previous["delta_dec_deg"])
                cra, cdec = _sign(current["delta_ra_hours_wrapped"]), _sign(current["delta_dec_deg"])
                current["previous_delta_ra_sign"] = pra; current["previous_delta_dec_sign"] = pdec
                current["previous_axis_motion_class"] = previous["axis_motion_class"]
                current["ra_direction_reversal"] = pra != 0 and cra != 0 and pra != cra
                current["dec_direction_reversal"] = pdec != 0 and cdec != 0 and pdec != cdec
                current["both_axes_reversal"] = current["ra_direction_reversal"] and current["dec_direction_reversal"]
                end = datetime.fromisoformat(str(previous["timestamp_end_utc"])); start = datetime.fromisoformat(str(current["timestamp_command_utc"]))
                current["seconds_since_previous_goto"] = (start - end).total_seconds()
            output.append(current); previous = current
    return output


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str] | None = None) -> None:
    if not rows: return
    fields = fields or list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)


def group_stats(rows: list[dict[str, object]]) -> dict[str, float]:
    values = np.array([float(r["goto_duration_external_sec"]) for r in rows])
    residual = np.array([float(r["residual_sec"]) for r in rows])
    safe = np.array([SlewModel.load().predict_from_distance(float(r["angular_distance_deg"]))["predicted_slew_safe_sec"] for r in rows])
    return {"count": len(rows), "mean_duration_sec": float(values.mean()), "median_duration_sec": float(np.median(values)),
            "p90_duration_sec": float(np.percentile(values, 90)), "p95_duration_sec": float(np.percentile(values, 95)),
            "mean_central_residual_sec": float(residual.mean()), "mean_safe_residual_sec": float(np.mean(values-safe)),
            "safe_coverage_percent": float(100*np.mean(values <= safe))}


def forensic_analysis(rows: list[dict[str, object]]) -> dict[str, object]:
    fields = ["sample_id", "training_source", "timestamp_command_utc", "goto_duration_external_sec",
              "predicted_duration_sec", "residual_sec", "angular_distance_deg", "delta_ra_hours_wrapped",
              "abs_delta_ra_deg", "delta_dec_deg", "abs_delta_dec_deg", "axis_motion_class",
              "start_ra_eod_hours", "start_dec_eod_deg", "target_ra_eod_hours", "target_dec_eod_deg",
              "start_ha_hours", "target_ha_hours", "start_alt_deg", "start_az_deg", "target_alt_deg", "target_az_deg",
              "previous_sample_id", "previous_distance_deg", "previous_duration_sec", "previous_delta_ra_sign",
              "previous_delta_dec_sign", "previous_axis_motion_class", "ra_direction_reversal",
              "dec_direction_reversal", "both_axes_reversal", "seconds_since_previous_goto",
              "delta_ha_hours", "min_abs_ha_along_transition", "ha_zero_crossing"]
    selections = {
        "short_le_5": lambda r: float(r["angular_distance_deg"]) <= 5,
        "target_ha_le_1": lambda r: abs(float(r["target_ha_hours"])) <= 1,
        "central_residual_gt_30": lambda r: float(r["residual_sec"]) > 30,
        "slow_le_10": lambda r: float(r["angular_distance_deg"]) <= 10 and float(r["goto_duration_external_sec"]) > 50,
    }
    for name, predicate in selections.items(): write_csv(OUTPUT / f"forensic_{name}.csv", [r for r in rows if predicate(r)], fields)
    short = [r for r in rows if float(r["angular_distance_deg"]) <= 5]
    normal = [r for r in short if float(r["goto_duration_external_sec"]) < 30]
    slow = [r for r in short if float(r["goto_duration_external_sec"]) >= 30]
    comparisons = {}
    for field in ["previous_distance_deg", "previous_duration_sec", "seconds_since_previous_goto",
                  "target_ha_hours", "target_alt_deg", "start_dec_eod_deg"]:
        a=np.array([float(r[field]) for r in normal if r[field] is not None]); b=np.array([float(r[field]) for r in slow if r[field] is not None])
        comparisons[field]={"normal_mean":float(a.mean()),"slow_mean":float(b.mean()),"standardized_difference":float((b.mean()-a.mean())/np.std(np.r_[a,b])),"mann_whitney_p":float(mannwhitneyu(a,b).pvalue)}
    for field in ["ra_direction_reversal", "dec_direction_reversal", "axis_motion_class"]:
        comparisons[field]={"normal":dict(Counter(str(r[field]) for r in normal)),"slow":dict(Counter(str(r[field]) for r in slow))}
    ha_summary={}
    for low,high,label in HA_BANDS:
        selected=[r for r in rows if low <= float(r["target_ha_hours"]) < high]
        ha_summary[label]=group_stats(selected)
    crossing={str(value):group_stats([r for r in rows if bool(r["ha_zero_crossing"]) is value]) for value in [True,False]}
    report={"movement_count":len(rows),"short_count":len(short),"short_normal_count":len(normal),"short_slow_count":len(slow),
            "short_comparisons":comparisons,"ha_bands":ha_summary,"ha_zero_crossing":crossing}
    (OUTPUT/"forensic_report.json").write_text(json.dumps(report,indent=2)+"\n")
    return report


def _sequence() -> list[dict[str, object]]:
    sequence=[]
    for pair in range(1,7):
        sequence += [{"experiment_group":"meridian","precedes_short":True,"pair_id":f"H{2*pair-1:02d}"},
                     {"experiment_group":"ra_short","intended_ra_reversal":False,"pair_id":f"R{pair:02d}"},
                     {"experiment_group":"ra_short","intended_ra_reversal":True,"pair_id":f"R{pair:02d}"},
                     {"experiment_group":"ha_crossing","intended_ha_zero_crossing":True,"pair_id":f"X{pair:02d}"},
                     {"experiment_group":"meridian","precedes_short":False,"pair_id":f"H{2*pair:02d}"}]
    return sequence


def generate_plan(location, start_ra: float, start_dec: float, obstime: Time) -> list[dict[str, object]]:
    rng=np.random.default_rng(SEED); count=250000
    az=rng.uniform(0,360,count); alt=np.degrees(np.arcsin(rng.uniform(math.sin(math.radians(30)),1,count)))
    local=SkyCoord(az=az*u.deg,alt=alt*u.deg,frame=AltAz(obstime=obstime,location=location));icrs=local.transform_to(ICRS());eod=icrs.transform_to(CIRS(obstime=obstime,location=location))
    ras=np.asarray(eod.ra.hour);decs=np.asarray(eod.dec.deg);lst=float(obstime.sidereal_time("apparent",longitude=location.lon).hour);has=((lst-ras+12)%24)-12
    available=np.ones(count,dtype=bool);ra,dec=start_ra,start_dec;prev_sign=1;plan=[];pair_magnitude={};band_remaining={label:3 for _,_,label in HA_BANDS}
    for sample,spec in enumerate(_sequence(),1):
        dra=((ras-ra+12)%24-12)*15;ddec=decs-dec;a,b=np.radians(dec),np.radians(decs);dist=np.degrees(np.arccos(np.clip(np.sin(a)*np.sin(b)+np.cos(a)*np.cos(b)*np.cos(np.radians(dra)),-1,1)))
        small=(np.abs(dra)<=5)&(np.abs(ddec)<=5);cls=np.full(count,"mixed",dtype="U12");cls[small]="small";cls[(~small)&(np.abs(dra)>1.5*np.abs(ddec))]="ra_dominant";cls[(~small)&(np.abs(ddec)>1.5*np.abs(dra))]="dec_dominant"
        mask=available&(decs<=-55)
        group=spec["experiment_group"]
        start_ha=((lst-ra+12)%24)-12
        if group=="ra_short":
            wanted=-prev_sign if spec["intended_ra_reversal"] else prev_sign
            mask&=(dist>1)&(dist<=5)&(cls=="ra_dominant")&(np.sign(dra)==wanted)&(np.abs(ddec)<=1)&(has*start_ha>0)
            if spec["intended_ra_reversal"] and spec["pair_id"] in pair_magnitude: mask&=np.abs(np.abs(dra)-pair_magnitude[spec["pair_id"]])<=2
        elif group=="meridian":
            choices=[x for x in HA_BANDS if band_remaining[x[2]] and ((x[0]>=0)==(start_ha>=0))]
            chooser=max if spec["precedes_short"] else min
            low,high,label=chooser(choices,key=lambda x:abs((x[0]+x[1])/2))
            spec["intended_ha_band"]=label
            mask&=(has>=low)&(has<high)&(dist>=10)&(dist<=60)&(has*start_ha>0)
            if spec["precedes_short"]: mask&=np.sign(dra)==-np.sign(has)
        else:
            mask&=(has*start_ha<0)&(dist>=5)&(dist<=60)&(np.abs(has)<=1)
        candidates=np.flatnonzero(mask)
        if not len(candidates): raise RuntimeError(f"no candidate for sample {sample}: {spec}")
        idx=int(rng.choice(candidates[:500]));available[idx]=False
        if group=="ra_short" and not spec["intended_ra_reversal"]:pair_magnitude[spec["pair_id"]]=abs(float(dra[idx]))
        if group=="meridian": band_remaining[spec["intended_ha_band"]]-=1
        actual_class=mb.axis_motion_class(float(dra[idx]),float(ddec[idx]));cross=(((lst-ra+12)%24)-12)*float(has[idx])<0
        row={"benchmark_id":f"almita-data03-{obstime.utc.strftime('%Y%m%dT%H%M%S')}-{SEED}","campaign_name":"targeted_30_model03","sample_id":sample,"seed":SEED,
             "source_point_id":plan[-1]["target_point_id"] if plan else "START_REAL","target_point_id":f"D{idx+1:06d}","generated_at_utc":obstime.utc.isot,
             "target_ra_icrs_hours":float(icrs[idx].ra.hour),"target_dec_icrs_deg":float(icrs[idx].dec.deg),"target_ra_eod_hours":float(ras[idx]),"target_dec_eod_deg":float(decs[idx]),
             "target_alt_deg":float(alt[idx]),"target_az_deg":float(az[idx]),"planned_angular_distance_deg":float(dist[idx]),"planned_delta_ra_deg":float(dra[idx]),"planned_delta_dec_deg":float(ddec[idx]),
             "planned_axis_motion_class":actual_class,"planned_target_ha_hours":float(has[idx]),"planned_ha_zero_crossing":bool(cross),
             "experiment_group":group,"pair_id":spec["pair_id"],"intended_ra_reversal":spec.get("intended_ra_reversal",""),"intended_ha_band":spec.get("intended_ha_band",""),"intended_ha_zero_crossing":spec.get("intended_ha_zero_crossing",False)}
        plan.append(row);prev_sign=_sign(float(dra[idx]));ra,dec=float(ras[idx]),float(decs[idx])
    return plan


def plot_plan(plan):
    colors={"ra_short":"tab:blue","meridian":"tab:orange","ha_crossing":"tab:green"}
    plt.figure(figsize=(8,6))
    for g in colors:
        r=[x for x in plan if x["experiment_group"]==g];plt.scatter([x["planned_target_ha_hours"] for x in r],[x["planned_angular_distance_deg"] for x in r],label=g,color=colors[g])
    plt.axvline(0,color="black",ls="--");plt.xlabel("Target HA (h)");plt.ylabel("Planned distance (deg)");plt.legend();plt.tight_layout();plt.savefig(OUTPUT/"ha_coverage.png",dpi=160);plt.close()
    short=[x for x in plan if x["experiment_group"]=="ra_short"];plt.figure(figsize=(8,5));plt.scatter(range(1,len(short)+1),[abs(x["planned_delta_ra_deg"]) for x in short],c=["red" if x["intended_ra_reversal"] else "blue" for x in short]);plt.xlabel("RA-short transition");plt.ylabel("abs planned delta RA (deg)");plt.tight_layout();plt.savefig(OUTPUT/"short_reversal_design.png",dpi=160);plt.close()
    plt.figure(figsize=(8,6));plt.plot([x["target_az_deg"] for x in plan],[x["target_alt_deg"] for x in plan],"o-");plt.axhline(30,color="red",ls="--");plt.xlabel("Azimuth (deg)");plt.ylabel("Altitude (deg)");plt.tight_layout();plt.savefig(OUTPUT/"proposed_transitions.png",dpi=160);plt.close()


def main():
    if OUTPUT.exists() and any(OUTPUT.iterdir()): raise FileExistsError(OUTPUT)
    OUTPUT.mkdir(parents=True)
    rows=load_history();report=forensic_analysis(rows)
    observer=mb.load_observer("observer_config.json");location=mb.observer_location(observer);now=Time.now()
    last=list(csv.DictReader(open("mount_benchmark_targeted_36_physical/mount_benchmark.csv")));last=[r for r in last if r["success"].lower()=="true"][-1]
    plan=generate_plan(location,float(last["end_ra_eod_hours"]),float(last["end_dec_eod_deg"]),now)
    fields=list(plan[0]);write_csv(OUTPUT/"mount_benchmark_plan.csv",plan,fields)
    coverage={"total":len(plan),"experiment_groups":dict(Counter(r["experiment_group"] for r in plan)),"short_reversal":dict(Counter(str(r["intended_ra_reversal"]) for r in plan if r["experiment_group"]=="ra_short")),"ha_bands":dict(Counter(r["intended_ha_band"] for r in plan if r["experiment_group"]=="meridian")),"ha_crossings":sum(bool(r["planned_ha_zero_crossing"]) for r in plan),"min_altitude_deg":min(r["target_alt_deg"] for r in plan),"max_altitude_deg":max(r["target_alt_deg"] for r in plan)}
    (OUTPUT/"coverage_summary.json").write_text(json.dumps(coverage,indent=2)+"\n");(OUTPUT/"metadata.json").write_text(json.dumps({"campaign_name":"targeted_30_model03","dry_run":True,"seed":SEED,"generated_at":now.utc.isot,"source_samples":len(rows)},indent=2)+"\n");plot_plan(plan)
    print(json.dumps({"forensics":report,"coverage":coverage},indent=2))


if __name__=="__main__":main()
