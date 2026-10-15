#!/usr/bin/env python3
"""Analyze only the physical MODEL-03 campaign; never retrain a model."""
import csv, json
from collections import defaultdict
from pathlib import Path
import numpy as np

OUT=Path("mount_benchmark_targeted_30_model03_physical")
rows=list(csv.DictReader((OUT/"mount_benchmark.csv").open()))
success=[r for r in rows if r["success"].lower()=="true"]
duration=lambda r:float(r["goto_duration_external_sec"])
pairs=defaultdict(list)
for r in success:
    if r["experiment_group"]=="ra_short": pairs[r["pair_id"]].append(r)
pair_rows=[]
for pair,values in pairs.items():
    by={r["actual_ra_reversal"]:r for r in values}
    if set(by)=={"True","False"}:
        pair_rows.append({"pair_id":pair,"no_reversal_sample_id":by["False"]["sample_id"],"no_reversal_duration_sec":duration(by["False"]),"reversal_sample_id":by["True"]["sample_id"],"reversal_duration_sec":duration(by["True"]),"reversal_minus_no_reversal_sec":duration(by["True"])-duration(by["False"])})
cross=[r for r in success if r["actual_ha_zero_crossing"]=="True"]
noncross=[r for r in success if r["actual_ha_zero_crossing"]=="False"]
matches=[]
for r in cross:
    ref=min(noncross,key=lambda x:abs(float(x["angular_distance_deg"])-float(r["angular_distance_deg"])))
    matches.append({"crossing_sample_id":r["sample_id"],"crossing_duration_sec":duration(r),"reference_sample_id":ref["sample_id"],"reference_duration_sec":duration(ref),"difference_sec":duration(r)-duration(ref),"angular_distance_deg":float(r["angular_distance_deg"]),"start_ha_hours":float(r["start_ha_hours"]),"target_ha_hours":float(r["target_ha_hours"]),"start_dec_deg":float(r["start_dec_eod_deg"])})
def write(name,data):
    with (OUT/name).open("w",newline="",encoding="utf-8") as h:
        w=csv.DictWriter(h,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
write("ra_short_pair_comparisons.csv",pair_rows);write("ha_crossing_matches.csv",matches)
short=[r for r in success if r["experiment_group"]=="ra_short"]
bands={}
for band in ["-1.0--0.5","-0.5-0","0-0.5","0.5-1.0"]:
    a=[duration(r) for r in success if r["experiment_group"]=="meridian" and r["actual_ha_band"]==band]
    bands[band]={"n":len(a),"mean_sec":float(np.mean(a)) if a else None,"median_sec":float(np.median(a)) if a else None}
analysis={"counts":{"requested":30,"physically_executed":len(success),"successful":len(success),"failed":sum(r["result"]=="failed" for r in rows),"deferred":sum(r["result"].startswith("deferred") for r in rows)},"complete_ra_pairs":len(pair_rows),"paired_ra_reversal_mean_difference_sec":float(np.mean([r["reversal_minus_no_reversal_sec"] for r in pair_rows])),"ha_crossing":{"n":len(cross),"mean_sec":float(np.mean([duration(r) for r in cross])),"matched_reference_mean_sec":float(np.mean([r["reference_duration_sec"] for r in matches])),"matched_penalty_mean_sec":float(np.mean([r["difference_sec"] for r in matches]))},"ha_bands":bands,"pointing_error":{"mean_deg":float(np.mean([float(r["final_pointing_error_deg"]) for r in success])),"max_deg":float(np.max([float(r["final_pointing_error_deg"]) for r in success]))}}
(OUT/"post_campaign_analysis.json").write_text(json.dumps(analysis,indent=2)+"\n")
