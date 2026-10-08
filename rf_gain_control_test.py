#!/usr/bin/env python3
"""Gain-only rtl_tcp isolation test. No product pipeline or INDI usage."""

import argparse, csv, json, socket, struct, time
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np

from rf_chain_characterization import analyze

GAINS=(0.0,8.7,20.7,29.7,40.2,49.6)


def send_command(sock,command,value):
    sock.sendall(struct.pack(">BI",command,int(value)))


def receive_exact(sock,size):
    data=bytearray(size);view=memoryview(data);used=0
    while used<size:
        count=sock.recv_into(view[used:])
        if not count:raise ConnectionError("rtl_tcp closed stream")
        used+=count
    return data


def discard_for(sock,seconds):
    deadline=time.monotonic()+seconds;total=0
    buffer=bytearray(262144);view=memoryview(buffer)
    while time.monotonic()<deadline:
        count=sock.recv_into(view)
        if not count:raise ConnectionError("rtl_tcp closed during settle")
        total+=count
    return total


def ranks(values):
    order=np.argsort(values);result=np.empty(len(values),float);result[order]=np.arange(len(values));return result


def spearman(x,y):return float(np.corrcoef(ranks(np.asarray(x)),ranks(np.asarray(y)))[0,1])


def main():
    p=argparse.ArgumentParser();p.add_argument("--output-dir",required=True);p.add_argument("--host",default="localhost")
    p.add_argument("--port",type=int,default=1234);p.add_argument("--frequency",type=int,default=1420405752)
    p.add_argument("--sample-rate",type=int,default=2400000);p.add_argument("--seconds",type=float,default=2)
    p.add_argument("--settle",type=float,default=1);p.add_argument("--test-a-dir",required=True);args=p.parse_args()
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=False)
    sequence=[("ascending",g) for g in GAINS]+[("descending",g) for g in reversed(GAINS)]
    records=[];sock=socket.create_connection((args.host,args.port),timeout=10);sock.settimeout(10)
    try:
        handshake=bytes(receive_exact(sock,12));
        if handshake[:4]!=b"RTL0":raise RuntimeError(f"invalid rtl_tcp handshake {handshake!r}")
        initialized=datetime.now(timezone.utc).isoformat()
        send_command(sock,0x01,args.frequency);send_command(sock,0x02,args.sample_rate);send_command(sock,0x03,1)
        initial_discard=discard_for(sock,args.settle)
        capture_bytes=int(args.seconds*args.sample_rate)*2
        for index,(direction,gain) in enumerate(sequence,1):
            raw_value=round(gain*10);sent=datetime.now(timezone.utc).isoformat();send_command(sock,0x04,raw_value)
            discarded=discard_for(sock,args.settle);captured=receive_exact(sock,capture_bytes)
            path=out/f"gain_only_{index:02d}_{direction}_{gain:04.1f}.h5"
            with h5py.File(path,"w") as h:
                h.create_dataset("iq_data",data=np.frombuffer(captured,dtype=np.uint8),compression="gzip",compression_opts=4,shuffle=True)
                h.attrs.update(file_state="complete",sample_rate_hz=args.sample_rate,center_frequency_hz=args.frequency,
                               gain_requested_db=gain,gain_command_tenths_db=raw_value,direction=direction,
                               gain_settle_seconds=args.settle,bytes_discarded_during_settle=discarded,
                               num_samples=capture_bytes//2,created_at=datetime.now(timezone.utc).isoformat())
            metric=analyze(path);record={"index":index,"timestamp_command_utc":sent,"direction":direction,
                "gain_requested_db":gain,"gain_command_tenths_db":raw_value,"gain_settle_seconds":args.settle,
                "bytes_discarded_during_settle":discarded,"samples_analyzed":capture_bytes//2,"hdf5_path":str(path),**metric}
            records.append(record);print(f"GAIN_ONLY {direction} gain={gain:.1f} raw={raw_value} discard={discarded} "
                f"std={metric['std_combined']:.6f} power={metric['broadband_power']:.6g} p001={metric['p001']:.1f} "
                f"p999={metric['p999']:.1f} clip={metric['clipping_fraction']:.3%}",flush=True)
    finally:sock.close()
    means=[]
    for gain in GAINS:
        pair=[r for r in records if r["gain_requested_db"]==gain]
        means.append({"gain":gain,"sigma":float(np.mean([r["std_combined"] for r in pair])),
                      "power":float(np.mean([r["broadband_power"] for r in pair])),
                      "sigma_relative_disagreement":float(abs(pair[0]["std_combined"]-pair[1]["std_combined"])/np.mean([r["std_combined"] for r in pair])),
                      "power_relative_disagreement":float(abs(pair[0]["broadband_power"]-pair[1]["broadband_power"])/np.mean([r["broadband_power"] for r in pair]))})
    rho_sigma=spearman([m["gain"] for m in means],[m["sigma"] for m in means]);rho_power=spearman([m["gain"] for m in means],[m["power"] for m in means])
    violations_sigma=sum(np.diff([m["sigma"] for m in means])<=0);violations_power=sum(np.diff([m["power"] for m in means])<=0)
    passing=max(rho_sigma,rho_power)>=.9 and min(violations_sigma,violations_power)<=1
    test_a=json.loads((Path(args.test_a_dir)/"rf_chain_summary.json").read_text());comparison=[]
    for mean in means:
        old=next(r for r in test_a["records"] if r["gain_requested_db"]==mean["gain"])
        comparison.append({"gain_db":mean["gain"],"sigma_test_a":old["std_combined"],"sigma_gain_only":mean["sigma"],
                           "power_test_a":old["broadband_power"],"power_gain_only":mean["power"]})
    summary={"test":"ALMITA-RF-GAIN-CONTROL-ISOLATION-01","timestamp_utc":datetime.now(timezone.utc).isoformat(),
      "rtl_tcp_handshake":handshake.hex(),"initialization_timestamp_utc":initialized,"frequency_command_count":1,
      "sample_rate_command_count":1,"manual_gain_command_count":1,"manual_gain_command_value":1,
      "gain_command_count":len(records),"initial_bytes_discarded":initial_discard,"gain_settle_seconds":args.settle,
      "records":records,"gain_means":means,"spearman_sigma":rho_sigma,"spearman_power":rho_power,
      "monotonic_violations_sigma":int(violations_sigma),"monotonic_violations_power":int(violations_power),
      "gain_control":"PASS" if passing else "FAIL","pll_analysis":"PENDING_JOURNAL_CORRELATION","sync":"NO"}
    (out/"rf_gain_isolation_summary.json").write_text(json.dumps(summary,indent=2))
    scalar=[k for k,v in records[0].items() if not isinstance(v,(dict,list))]
    with (out/"gain_only_sweep.csv").open("w",newline="") as h:
        w=csv.DictWriter(h,fieldnames=scalar,extrasaction="ignore");w.writeheader();w.writerows(records)
    with (out/"test_a_vs_gain_only.csv").open("w",newline="") as h:
        w=csv.DictWriter(h,fieldnames=list(comparison[0]));w.writeheader();w.writerows(comparison)
    import matplotlib;matplotlib.use("Agg");import matplotlib.pyplot as plt
    fig,ax=plt.subplots(1,2,figsize=(10,4))
    for direction in ("ascending","descending"):
        subset=sorted((r for r in records if r["direction"]==direction),key=lambda r:r["gain_requested_db"])
        ax[0].plot([r["gain_requested_db"] for r in subset],[r["std_combined"] for r in subset],"o-",label=direction)
        ax[1].plot([r["gain_requested_db"] for r in subset],[r["broadband_power"] for r in subset],"o-",label=direction)
    ax[0].set(xlabel="gain dB",ylabel="ADC std");ax[1].set(xlabel="gain dB",ylabel="median PSD")
    for a in ax:a.grid();a.legend()
    fig.tight_layout();fig.savefig(out/"gain_only_response.png",dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(9,5))
    for r in records:ax.step(range(256),r["histogram"],where="mid",label=f"{r['direction'][0]} {r['gain_requested_db']}")
    ax.set_yscale("log");ax.set_xlim(120,136);ax.legend(ncol=2,fontsize=7);fig.tight_layout();fig.savefig(out/"gain_only_adc_histograms.png",dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(10,5))
    for r in records:ax.plot(np.asarray(r["frequency_hz"])/1e6,10*np.log10(np.maximum(r["psd"],1e-30)),label=f"{r['direction'][0]} {r['gain_requested_db']}")
    ax.legend(ncol=2,fontsize=7);ax.set(xlabel="MHz",ylabel="PSD dB arbitrary");fig.tight_layout();fig.savefig(out/"gain_only_psd.png",dpi=150);plt.close(fig)
    print(json.dumps({k:summary[k] for k in ("manual_gain_command_value","spearman_sigma","spearman_power","monotonic_violations_sigma","monotonic_violations_power","gain_control")},indent=2))
    return 0 if passing else 2

if __name__=="__main__":raise SystemExit(main())
