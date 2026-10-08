#!/usr/bin/env python3
"""Instrument-only RTL-SDR gain characterization. No INDI, GOTO, or SYNC."""

import argparse, asyncio, csv, json, math
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np

from sdr_capture import SDRCapture
from temperature_sensors import DS18B20Reader

GAINS = (0.0, 8.7, 20.7, 29.7, 40.2, 49.6)


def analyze(path, fft_size=8192):
    with h5py.File(path) as h:
        raw=h["iq_data"][:]; sr=float(h.attrs["sample_rate_hz"]); cf=float(h.attrs["center_frequency_hz"])
    i=raw[0::2].astype(np.float32);q=raw[1::2].astype(np.float32)
    ic=i-i.mean();qc=q-q.mean(); count=len(i)//fft_size
    window=np.hanning(fft_size).astype(np.float32); psd=np.zeros(fft_size,np.float64)
    for start in range(0,count,64):
        stop=min(count,start+64)
        z=(ic[start*fft_size:stop*fft_size]+1j*qc[start*fft_size:stop*fft_size]).reshape(-1,fft_size)*window
        psd += np.sum(np.abs(np.fft.fftshift(np.fft.fft(z,axis=1),axes=1))**2,axis=0)
    psd/=count
    frequency=cf+np.fft.fftshift(np.fft.fftfreq(fft_size,1/sr))
    hist=np.bincount(raw,minlength=256).astype(float);prob=hist/hist.sum();nz=prob>0
    percentiles=np.percentile(raw,[.1,1,50,99,99.9])
    return {"mean_i":float(i.mean()),"mean_q":float(q.mean()),"std_i":float(i.std()),"std_q":float(q.std()),
            "std_combined":float(np.sqrt((i.var()+q.var())/2)),"centered_rms":float(np.sqrt(np.mean(ic*ic+qc*qc))),
            "minimum":int(raw.min()),"maximum":int(raw.max()),"p001":float(percentiles[0]),"p01":float(percentiles[1]),
            "p50":float(percentiles[2]),"p99":float(percentiles[3]),"p999":float(percentiles[4]),
            "entropy_bits":float(-np.sum(prob[nz]*np.log2(prob[nz]))),
            "clipping_fraction":float(np.mean((raw==0)|(raw==255))),"broadband_power":float(np.median(psd)),
            "mean_psd_power":float(np.mean(psd)),"adc_fraction_p001_p999":float((percentiles[4]-percentiles[0])/255),
            "frequency_hz":frequency.tolist(),"psd":psd.tolist(),"histogram":hist.astype(int).tolist()}


async def run(args):
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=False)
    cfg=json.loads(Path(args.observer_config).read_text());sensors=DS18B20Reader(cfg.get("temperature_sensors",{}))
    sdr=SDRCapture("network",args.host,args.port,verbose=True);rows=[]
    try:
        await sdr.connect()
        for gain in args.gains:
            print(f"RF_GAIN_REQUEST_DB={gain:.1f}",flush=True)
            await sdr.configure(args.center_freq,args.sample_rate,gain=gain)
            pre=sensors.read_all();path=out/f"rf_50ohm_gain_{gain:04.1f}.h5"
            await sdr.capture(args.seconds,str(path),args.sample_rate,{"gain_requested_db":gain,"rf_input":"50_OHM_AT_LNA_INPUT"})
            post=sensors.read_all();metrics=analyze(path)
            row={"gain_requested_db":gain,"gain_effective_db":None,"hdf5_path":str(path),"temperature_pre":pre,
                 "temperature_post":post,**metrics};rows.append(row)
            print(f"RF_GAIN_RESULT gain={gain:.1f} std={metrics['std_combined']:.6f} power={metrics['broadband_power']:.6g} "
                  f"p001={metrics['p001']:.1f} p999={metrics['p999']:.1f} clip={metrics['clipping_fraction']:.3%}",flush=True)
    finally: await sdr.close()
    std=np.asarray([r["std_combined"] for r in rows]);power=np.asarray([r["broadband_power"] for r in rows])
    systematic=bool(np.all(np.diff(std)>0) and np.all(np.diff(power)>0))
    summary={"test":"ALMITA-RF-CHAIN-CHARACTERIZATION-01-TEST-A","timestamp_utc":datetime.now(timezone.utc).isoformat(),
             "configuration":"50_OHM_AT_LNA_INPUT","gains_requested_db":args.gains,"gain_effective_db":"NOT_READABLE_BY_RTL_TCP_PROTOCOL",
             "records":rows,"gain_control":"PASS" if systematic else "FAIL","rtl_sdr_response":"PASS" if systematic else "INCONCLUSIVE",
             "lna_power":"BIAS_T_SOFTWARE_ENABLED_PHYSICAL_POWER_NOT_YET_VERIFIED","fifty_ohm_response":"PASS" if systematic else "FAIL",
             "antenna_response":"INCONCLUSIVE","sun_off":"NOT TESTED","sync":"NO","next_authorized":"REVIEW_TEST_A"}
    (out/"rf_chain_summary.json").write_text(json.dumps(summary,indent=2))
    scalar=[k for k,v in rows[0].items() if not isinstance(v,(dict,list))]
    with (out/"gain_sweep_50ohm.csv").open("w",newline="") as h:
        w=csv.DictWriter(h,fieldnames=scalar);w.writeheader();w.writerows(rows)
    import matplotlib;matplotlib.use("Agg");import matplotlib.pyplot as plt
    gains=[r["gain_requested_db"] for r in rows]
    fig,ax=plt.subplots(1,2,figsize=(10,4));ax[0].plot(gains,std,"o-");ax[0].set(xlabel="gain dB",ylabel="ADC std counts")
    ax[1].plot(gains,power,"o-");ax[1].set(xlabel="gain dB",ylabel="median PSD");fig.tight_layout();fig.savefig(out/"gain_response.png",dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(9,5))
    for r in rows: ax.step(range(256),r["histogram"],where="mid",label=f"{r['gain_requested_db']} dB")
    ax.set_yscale("log");ax.set(xlabel="ADC code",ylabel="count");ax.legend();fig.tight_layout();fig.savefig(out/"adc_histograms.png",dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(10,5))
    for r in rows: ax.plot(np.asarray(r["frequency_hz"])/1e6,10*np.log10(np.maximum(r["psd"],1e-30)),label=f"{r['gain_requested_db']} dB")
    ax.set(xlabel="MHz",ylabel="PSD dB arbitrary");ax.legend();fig.tight_layout();fig.savefig(out/"psd_gain_comparison.png",dpi=150);plt.close(fig)
    print(json.dumps({k:summary[k] for k in ("gain_control","rtl_sdr_response","fifty_ohm_response","next_authorized")},indent=2))
    return 0 if systematic else 2


def parse_args():
    p=argparse.ArgumentParser();p.add_argument("--output-dir",required=True);p.add_argument("--gains",type=float,nargs="+",default=list(GAINS))
    p.add_argument("--seconds",type=float,default=2);p.add_argument("--center-freq",type=int,default=1420405752)
    p.add_argument("--sample-rate",type=int,default=2400000);p.add_argument("--host",default="localhost");p.add_argument("--port",type=int,default=1234)
    p.add_argument("--observer-config",default="observer_config.json");return p.parse_args()
if __name__=="__main__":raise SystemExit(asyncio.run(run(parse_args())))
