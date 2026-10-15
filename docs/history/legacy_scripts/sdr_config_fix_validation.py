#!/usr/bin/env python3
"""Hardware validation for the productive SDRCapture config sequence."""

import argparse, asyncio, csv, json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

from rf_chain_characterization import analyze
from sdr_capture import SDRCapture

GAINS=(0.0,8.7,20.7,29.7,40.2,49.6)


def spearman(x,y):
    def rank(v):
        order=np.argsort(v);r=np.empty(len(v));r[order]=np.arange(len(v));return r
    return float(np.corrcoef(rank(x),rank(y))[0,1])


async def main(args):
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=False)
    sdr=SDRCapture("network",args.host,args.port,verbose=True);records=[]
    async def take(label,gain,frequency):
        await sdr.configure(frequency,args.sample_rate,gain)
        path=out/f"{label}.h5"
        await sdr.capture(args.seconds,str(path),args.sample_rate,{"gain":gain,"center_freq":frequency,"validation_label":label})
        metric=analyze(path);row={"label":label,"timestamp_utc":datetime.now(timezone.utc).isoformat(),
          "gain_requested_db":gain,"configured_frequency_hz":frequency,
          "sample_rate_hz":args.sample_rate,"hdf5_path":str(path),**metric}
        records.append(row);print(f"PRODUCTIVE {label} gain={gain:.1f} f={frequency} std={metric['std_combined']:.6f} "
          f"power={metric['broadband_power']:.6g} p001={metric['p001']:.1f} p999={metric['p999']:.1f} clip={metric['clipping_fraction']:.3%}",flush=True)
    try:
        await sdr.connect()
        for direction,gains in (("ascending",GAINS),("descending",reversed(GAINS))):
            for gain in gains:await take(f"gain_{direction}_{gain:04.1f}",gain,args.frequency)
        await take("pre_retune_nominal",20.7,args.frequency)
        await take("retune_plus_100khz",20.7,args.frequency+100000)
        await take("retune_nominal_restored",20.7,args.frequency)
        await take("post_retune_gain_only",29.7,args.frequency)
    finally:await sdr.close()
    sweep=records[:12];means=[]
    for gain in GAINS:
        pair=[r for r in sweep if r["gain_requested_db"]==gain]
        means.append({"gain":gain,"sigma":float(np.mean([r["std_combined"] for r in pair])),"power":float(np.mean([r["broadband_power"] for r in pair])),
          "sigma_disagreement":float(abs(pair[0]["std_combined"]-pair[1]["std_combined"])/np.mean([r["std_combined"] for r in pair])),
          "power_disagreement":float(abs(pair[0]["broadband_power"]-pair[1]["broadband_power"])/np.mean([r["broadband_power"] for r in pair]))})
    rho_s=spearman([m["gain"] for m in means],[m["sigma"] for m in means]);rho_p=spearman([m["gain"] for m in means],[m["power"] for m in means])
    vio_s=int(sum(np.diff([m["sigma"] for m in means])<=0));vio_p=int(sum(np.diff([m["power"] for m in means])<=0))
    prior_iso=json.loads((Path(args.isolation_dir)/"rf_gain_isolation_summary.json").read_text())
    prior_a=json.loads((Path(args.test_a_dir)/"rf_chain_summary.json").read_text());comparison=[]
    for m in means:
        a=next(r for r in prior_a["records"] if r["gain_requested_db"]==m["gain"]);iso=next(x for x in prior_iso["gain_means"] if x["gain"]==m["gain"])
        comparison.append({"gain_db":m["gain"],"sigma_test_a":a["std_combined"],"sigma_isolation":iso["sigma"],"sigma_productive_fixed":m["sigma"],
          "psd_test_a":a["broadband_power"],"psd_isolation":iso["power"],"psd_productive_fixed":m["power"]})
    passed=rho_s>=.9 and rho_p>=.9 and vio_s<=1 and vio_p<=1
    retune={"plus_frequency_hz":args.frequency+100000,"restored_frequency_hz":args.frequency,"capture_valid":all(Path(r["hdf5_path"]).is_file() for r in records[12:]),
      "post_retune_gain_only_valid":Path(records[15]["hdf5_path"]).is_file(),"pll":"PENDING_JOURNAL_CORRELATION"}
    summary={"test":"ALMITA-SDR-CONFIG-SEQUENCE-FIX-01","timestamp_utc":datetime.now(timezone.utc).isoformat(),"records":records,"gain_means":means,
      "spearman_sigma":rho_s,"spearman_power":rho_p,"monotonic_violations_sigma":vio_s,"monotonic_violations_power":vio_p,
      "productive_gain_only":"PASS" if passed else "FAIL","adc_underutilization":"STILL PRESENT","retune_control":retune,"sync":"NO"}
    (out/"sdr_config_fix_summary.json").write_text(json.dumps(summary,indent=2));(out/"retune_control_test.json").write_text(json.dumps(retune,indent=2))
    scalar=[k for k,v in sweep[0].items() if not isinstance(v,(list,dict))]
    with (out/"productive_gain_sweep.csv").open("w",newline="") as h:w=csv.DictWriter(h,fieldnames=scalar,extrasaction="ignore");w.writeheader();w.writerows(sweep)
    with (out/"comparison_three_way.csv").open("w",newline="") as h:w=csv.DictWriter(h,fieldnames=list(comparison[0]));w.writeheader();w.writerows(comparison)
    import matplotlib;matplotlib.use("Agg");import matplotlib.pyplot as plt
    fig,ax=plt.subplots(1,2,figsize=(10,4))
    for direction in ("ascending","descending"):
        rows=sorted([r for r in sweep if direction in r["label"]],key=lambda r:r["gain_requested_db"])
        ax[0].plot([r["gain_requested_db"] for r in rows],[r["std_combined"] for r in rows],"o-",label=direction)
        ax[1].plot([r["gain_requested_db"] for r in rows],[r["broadband_power"] for r in rows],"o-",label=direction)
    for a in ax:a.grid();a.legend();fig.tight_layout();fig.savefig(out/"productive_gain_response.png",dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(10,5))
    for r in sweep:ax.plot(np.asarray(r["frequency_hz"])/1e6,10*np.log10(np.maximum(r["psd"],1e-30)),label=r["label"])
    ax.legend(ncol=2,fontsize=7);fig.tight_layout();fig.savefig(out/"productive_gain_psd.png",dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(9,5))
    for r in sweep:ax.step(range(256),r["histogram"],where="mid",label=r["label"])
    ax.set_yscale("log");ax.set_xlim(120,136);ax.legend(ncol=2,fontsize=7);fig.tight_layout();fig.savefig(out/"productive_gain_adc.png",dpi=150);plt.close(fig)
    print(json.dumps({k:summary[k] for k in ("spearman_sigma","spearman_power","monotonic_violations_sigma","monotonic_violations_power","productive_gain_only","adc_underutilization")},indent=2))
    return 0 if passed else 2

def parse_args():
    p=argparse.ArgumentParser();p.add_argument("--output-dir",required=True);p.add_argument("--test-a-dir",required=True);p.add_argument("--isolation-dir",required=True)
    p.add_argument("--host",default="localhost");p.add_argument("--port",type=int,default=1234);p.add_argument("--frequency",type=int,default=1420405752)
    p.add_argument("--sample-rate",type=int,default=2400000);p.add_argument("--seconds",type=float,default=2);return p.parse_args()
if __name__=="__main__":raise SystemExit(asyncio.run(main(parse_args())))
