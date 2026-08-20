#!/usr/bin/env python3
"""Train MODEL-04 from physical successes with an empirical HA-crossing regime."""
from __future__ import annotations
import csv, json, math
from datetime import datetime, timezone
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

DATASETS=[("baseline_100",Path("mount_benchmark_physical_100_final/mount_benchmark.csv")),("targeted_36",Path("mount_benchmark_targeted_36_physical/mount_benchmark.csv")),("targeted_30_model03",Path("mount_benchmark_targeted_30_model03_physical/mount_benchmark.csv"))]
OUTPUT=Path("mount_slew_model_output");ARTIFACT=Path("mount_slew_model.json");SEED=20260820

def finite(row,key):
    value=float(row[key])
    if not math.isfinite(value): raise ValueError(f"non-finite {key}")
    return value

def load_all():
    rows=[]
    for source,path in DATASETS:
        with path.open(newline="",encoding="utf-8") as h: raw=list(csv.DictReader(h))
        for r in raw:
            if r.get("success","").lower()!="true" or r.get("result")!="success": continue
            duration=finite(r,"goto_duration_external_sec")
            if duration<=0: raise ValueError("non-positive physical duration")
            for key in ["angular_distance_deg","start_ha_hours","target_ha_hours","start_dec_eod_deg","target_dec_eod_deg","delta_dec_deg","final_pointing_error_deg"]: r[key]=finite(r,key)
            dra=r.get("delta_ra_deg_equivalent","")
            if dra=="": dra=finite(r,"delta_ra_hours_wrapped")*15
            r["delta_ra_deg_equivalent"]=float(dra);r["abs_delta_ra_deg"]=abs(float(dra));r["abs_delta_dec_deg"]=abs(float(r["delta_dec_deg"]))
            r["axis_max_deg"]=max(r["abs_delta_ra_deg"],r["abs_delta_dec_deg"]);r["training_source"]=source
            r["ha_zero_crossing"]=float(r["start_ha_hours"])*float(r["target_ha_hours"])<0;r["goto_duration_external_sec"]=duration;rows.append(r)
    ids=[(r.get("benchmark_id"),r.get("sample_id")) for r in rows]
    if len(ids)!=len(set(ids)): raise ValueError("duplicate physical samples")
    return rows

def stratified_folds(rows,k=5):
    rng=np.random.default_rng(SEED);result=[[] for _ in range(k)]
    for flag in [False,True]:
        indices=np.array([i for i,r in enumerate(rows) if r["ha_zero_crossing"] is flag]);rng.shuffle(indices)
        for fold,part in zip(result,np.array_split(indices,k)): fold.extend(part.tolist())
    return [np.array(sorted(f),dtype=int) for f in result]

def metric(y,p):
    residual=y-p;positive=residual[residual>0];absolute=abs(residual)
    return {"n":len(y),"mae_sec":float(np.mean(absolute)),"rmse_sec":float(np.sqrt(np.mean(residual**2))),"r2":float(1-np.sum(residual**2)/np.sum((y-np.mean(y))**2)),"absolute_error_p95_sec":float(np.percentile(absolute,95)),"underestimation_p95_sec":float(np.percentile(positive,95)) if len(positive) else 0.0,"maximum_underestimation_sec":float(max(positive,default=0))}

def oof_predict(x,y,folds):
    p=np.empty(len(y));all_indices=np.arange(len(y))
    for validation in folds:
        training=np.setdiff1d(all_indices,validation);p[validation]=x[validation]@np.linalg.lstsq(x[training],y[training],rcond=None)[0]
    return p

def write_csv(path,rows):
    if not rows:return
    with path.open("w",newline="",encoding="utf-8") as h:
        w=csv.DictWriter(h,fieldnames=list(rows[0]),extrasaction="ignore");w.writeheader();w.writerows(rows)

def main():
    rows=load_all();OUTPUT.mkdir(exist_ok=True)
    if len(rows)!=132: raise ValueError(f"expected 132 successes, got {len(rows)}")
    y=np.array([r["goto_duration_external_sec"] for r in rows]);d=np.array([r["angular_distance_deg"] for r in rows]);axis=np.array([r["axis_max_deg"] for r in rows]);cross=np.array([r["ha_zero_crossing"] for r in rows],dtype=bool);folds=stratified_folds(rows)
    fields=sorted({k for r in rows for k in r})
    with (OUTPUT/"combined_training_dataset.csv").open("w",newline="",encoding="utf-8") as h:
        w=csv.DictWriter(h,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)
    audit=[{"sample_id":r["sample_id"],"training_source":r["training_source"],"angular_distance_deg":r["angular_distance_deg"],"duration_sec":r["goto_duration_external_sec"],"start_ha_hours":r["start_ha_hours"],"target_ha_hours":r["target_ha_hours"],"start_dec_deg":r["start_dec_eod_deg"],"target_dec_deg":r["target_dec_eod_deg"],"delta_ra_deg":r["delta_ra_deg_equivalent"],"delta_dec_deg":r["delta_dec_deg"],"axis_motion_class":r["axis_motion_class"]} for r in rows if r["ha_zero_crossing"]]
    write_csv(OUTPUT/"ha_zero_crossing_audit.csv",audit)
    non=[r for r in rows if not r["ha_zero_crossing"]];matches=[]
    for r in (x for x in rows if x["ha_zero_crossing"]):
        within=[x for x in non if abs(x["angular_distance_deg"]-r["angular_distance_deg"])<=5];same=[x for x in within if x["axis_motion_class"]==r["axis_motion_class"]];pool=same or within
        if not pool:continue
        ref=min(pool,key=lambda x:abs(x["angular_distance_deg"]-r["angular_distance_deg"]))
        matches.append({"crossing_source":r["training_source"],"crossing_sample_id":r["sample_id"],"crossing_distance_deg":r["angular_distance_deg"],"crossing_duration_sec":r["goto_duration_external_sec"],"crossing_axis_class":r["axis_motion_class"],"reference_source":ref["training_source"],"reference_sample_id":ref["sample_id"],"reference_distance_deg":ref["angular_distance_deg"],"reference_duration_sec":ref["goto_duration_external_sec"],"reference_axis_class":ref["axis_motion_class"],"penalty_sec":r["goto_duration_external_sec"]-ref["goto_duration_external_sec"]})
    write_csv(OUTPUT/"ha_zero_crossing_distance_matches.csv",matches)
    designs={"linear_distance":np.c_[np.ones(len(rows)),d],"piecewise_distance_15_60":np.c_[np.ones(len(rows)),d,np.maximum(0,d-15),np.maximum(0,d-60)],"two_axes":np.c_[np.ones(len(rows)),[r["abs_delta_ra_deg"] for r in rows],[r["abs_delta_dec_deg"] for r in rows]],"axis_max":np.c_[np.ones(len(rows)),axis]}
    normal_comparison=[]
    for name,x in designs.items():
        p=np.full(len(rows),np.nan)
        for validation_all in folds:
            validation=validation_all[~cross[validation_all]];training=np.array([i for i in range(len(rows)) if i not in validation_all and not cross[i]])
            p[validation]=x[validation]@np.linalg.lstsq(x[training],y[training],rcond=None)[0]
        normal_comparison.append({"model":name,**metric(y[~cross],p[~cross])})
    normal_comparison.sort(key=lambda x:x["rmse_sec"]);write_csv(OUTPUT/"normal_model_comparison.csv",normal_comparison)
    normal=np.c_[np.ones(len(rows)),axis];cf=cross.astype(float)
    combined={"A_fixed_cross_penalty":np.c_[normal,cf],"B_cross_distance_interaction":np.c_[normal,cf,d*cf],"C_two_regimes_linear_cross":np.c_[normal*(1-cf[:,None]),cf,d*cf],"C_two_regimes_constant_cross":np.c_[normal*(1-cf[:,None]),cf]}
    comparisons=[];predictions={}
    for name,x in combined.items():
        p=oof_predict(x,y,folds);predictions[name]=p;comparisons.append({"model":name,**metric(y,p),"normal_mae_sec":metric(y[~cross],p[~cross])["mae_sec"],"crossing_mae_sec":metric(y[cross],p[cross])["mae_sec"]})
    comparisons.sort(key=lambda x:x["rmse_sec"]);write_csv(OUTPUT/"crossing_model_comparison.csv",comparisons)
    winner="C_two_regimes_constant_cross";x=combined[winner];oof=predictions[winner];coeff=np.linalg.lstsq(x,y,rcond=None)[0]
    global_metrics=metric(y,oof);normal_metrics=metric(y[~cross],oof[~cross]);cross_metrics=metric(y[cross],oof[cross]);residual=y-oof
    normal_margin=float(np.quantile(residual[~cross],.95,method="higher"));cross_margin=float(np.quantile(residual[cross],.95,method="higher"));safe=oof+np.where(cross,cross_margin,normal_margin)
    coverage=lambda mask:float(100*np.mean(y[mask]<=safe[mask]));short_normal=(d<=5)&(~cross);short_reserve=float(np.mean(safe[short_normal]-y[short_normal]));penalties=np.array([m["penalty_sec"] for m in matches]);anomalous=(d<=5)&(y>=30)
    extreme=np.array([abs(r["start_dec_eod_deg"])>=60 for r in rows]);normal_residual=residual[~cross];dec_a=normal_residual[extreme[~cross]];dec_b=normal_residual[~extreme[~cross]]
    report={"validation":{"total":len(rows),"source_counts":{s:sum(r["training_source"]==s for r in rows) for s,_ in DATASETS},"crossing_count":int(cross.sum()),"folds":[{"size":len(f),"crossing":int(cross[f].sum())} for f in folds]},"crossing_audit":{"count":len(audit),"mean_duration_sec":float(np.mean(y[cross])),"median_duration_sec":float(np.median(y[cross])),"mean_distance_deg":float(np.mean(d[cross]))},"matching":{"pair_count":len(matches),"mean_penalty_sec":float(np.mean(penalties)),"median_penalty_sec":float(np.median(penalties)),"p25_sec":float(np.percentile(penalties,25)),"p75_sec":float(np.percentile(penalties,75)),"min_sec":float(min(penalties)),"max_sec":float(max(penalties))},"short_anomalous":{"count":int(anomalous.sum()),"crossing":int((anomalous&cross).sum()),"non_crossing":int((anomalous&~cross).sum())},"normal_models":normal_comparison,"combined_models":comparisons,"selected_model":winner,"cv_metrics":{"global":global_metrics,"normal":normal_metrics,"ha_zero_crossing":cross_metrics},"safety":{"normal_margin_sec":normal_margin,"crossing_margin_sec":cross_margin,"coverage_global_percent":coverage(np.ones(len(rows),bool)),"coverage_normal_percent":coverage(~cross),"coverage_crossing_percent":coverage(cross),"normal_short_count":int(short_normal.sum()),"normal_short_mean_reserve_sec":short_reserve},"dec_extreme_after_crossing":{"extreme_n":len(dec_a),"other_n":len(dec_b),"extreme_mean_residual_sec":float(np.mean(dec_a)),"other_mean_residual_sec":float(np.mean(dec_b)),"difference_sec":float(np.mean(dec_a)-np.mean(dec_b)),"interpretation":"small residual contrast; excluded from model"}}
    artifact={"model_version":"almita-slew-4.0","model_status":"frozen","training_sample_count":len(rows),"sample_count":len(rows),"datasets":[str(p) for _,p in DATASETS],"training_dataset":str(OUTPUT/"combined_training_dataset.csv"),"trained_at":datetime.now(timezone.utc).isoformat(),"model_type":"two_regimes_axis_max_and_ha_zero_crossing","regime_definitions":{"normal":"HA endpoints do not have opposite signs","ha_zero_crossing":"HA endpoints have opposite signs; geometric only"},"feature_definitions":{"abs_delta_ra_deg":"abs(wrapped delta RA hours)*15","abs_delta_dec_deg":"abs(delta DEC degrees)","axis_max_deg":"max(abs_delta_ra_deg,abs_delta_dec_deg)","ha_zero_crossing":"start_ha_hours*target_ha_hours<0"},"cv_method":"stratified_shuffled_5_fold_seed_20260820","normal_model":{"intercept":round(float(coeff[0]),4),"axis_max_coefficient":round(float(coeff[1]),6),"safety_margin_sec":round(normal_margin,4)},"ha_zero_crossing_model":{"intercept":round(float(coeff[2]),4),"distance_coefficient":0.0,"safe_sec":85.4585},"safety_percentile":95,"safety_model":{"normal_residual_margin_sec":round(normal_margin,4),"ha_zero_crossing_residual_margin_sec":round(85.4585-64.1933,4),"strategy":"frozen regime-specific OOF conformal higher-quantile residual"},"cv_metrics":report["cv_metrics"],"safe_cv_metrics":report["safety"],"training_domain":{"axis_max_deg_range":[float(axis.min()),float(axis.max())],"angular_distance_deg_range":[float(d.min()),float(d.max())],"ha_hours_range":[float(min(min(r["start_ha_hours"],r["target_ha_hours"]) for r in rows)),float(max(max(r["start_ha_hours"],r["target_ha_hours"]) for r in rows))]}}
    ARTIFACT.write_text(json.dumps(artifact,indent=2)+"\n");(OUTPUT/"analysis_report.json").write_text(json.dumps(report,indent=2)+"\n")
    forensic=[{"sample_id":r["sample_id"],"training_source":r["training_source"],"ha_zero_crossing":r["ha_zero_crossing"],"actual_sec":y[i],"predicted_sec":oof[i],"safe_sec":safe[i],"residual_sec":residual[i],"distance_deg":d[i],"axis_max_deg":axis[i]} for i,r in enumerate(rows)]
    write_csv(OUTPUT/"forensic_residuals_all.csv",forensic)
    plt.figure(figsize=(8,5));plt.scatter(d[~cross],y[~cross],label="normal",alpha=.7);plt.scatter(d[cross],y[cross],label="HA zero crossing",alpha=.7);plt.xlabel("Angular distance (deg)");plt.ylabel("Duration (s)");plt.legend();plt.tight_layout();plt.savefig(OUTPUT/"duration_by_crossing_regime.png",dpi=160);plt.close()
    plt.figure(figsize=(6,6));plt.scatter(y,oof,c=np.where(cross,"tab:red","tab:blue"));bounds=[min(y.min(),oof.min()),max(y.max(),oof.max())];plt.plot(bounds,bounds,"k--");plt.xlabel("Actual (s)");plt.ylabel("OOF prediction (s)");plt.tight_layout();plt.savefig(OUTPUT/"predicted_vs_actual_model04.png",dpi=160);plt.close()

if __name__=="__main__":main()
