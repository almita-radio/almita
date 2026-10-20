"""Report for lna_gain_night.py: levels per phase, LNA-off floor per state, P8 Allan + temperature, spectral ratio.

Usage: .venv/bin/python scripts/hardware/lna_gain_night_report.py <output_dir>   (writes report.json + report.png there)"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(sys.argv[1])
R = list(csv.DictReader(open(OUT / "power_1s.csv")))
t = np.array([float(r["epoch"]) for r in R]); p = np.array([float(r["power"]) for r in R])
ph = np.array([r["phase"] for r in R]); bias = np.array([r["bias"] for r in R]); evn = [r["event"] for r in R]
rep = {"phases": {}}

for name in dict.fromkeys(ph):
    m = (ph == name) & (bias == "1")
    rep["phases"][name] = {"rms_median": round(float(np.sqrt(np.median(p[m]))), 3),
                           "rms_min": round(float(np.sqrt(np.percentile(p[m], 1))), 3),
                           "rms_max": round(float(np.sqrt(np.percentile(p[m], 99))), 3)}

# LNA-off floor, 2nd second after switching off (the 1st one holds the decay)
off_idx = [i for i, e in enumerate(evn) if e == "bias0" and i + 1 < len(R)]
rep["lna_off_floor"] = {ph[i]: [] for i in off_idx}
for i in off_idx:
    rep["lna_off_floor"][ph[i]].append(round(float(np.sqrt(p[i + 1])), 4))

# P8: Allan + temperature regression (per-minute means vs the logger's 60 s rows)
m8 = ph == "P8_long"; y = p[m8] / p[m8].mean(); t8 = t[m8]
rep["P8_drift_db"] = round(float(10 * np.log10(p[m8][-600:].mean() / p[m8][:600].mean())), 3)
taus, adev = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096], []
for tau in taus:
    k = len(y) // tau; a = y[:k * tau].reshape(k, tau).mean(1); adev.append(float(np.sqrt(0.5 * np.mean(np.diff(a) ** 2))))
rep["P8_allan"] = dict(zip(map(str, taus), [f"{v:.2e}" for v in adev]))
temps = []
for f in sorted((ROOT / "data/runtime/temperature").glob("20261018.jsonl")):
    for line in open(f):
        d = json.loads(line); e = datetime.fromisoformat(d["utc"]).timestamp()
        if t[0] <= e <= t[-1]:
            temps.append((e, {k: v["c"] for k, v in d["readings"].items() if v.get("c") is not None}))
te = np.array([e for e, _ in temps])
roles = ["sdr", "lna", "load", "cable"]
T = {r: np.array([d.get(r, np.nan) for _, d in temps]) for r in roles}
pm = np.array([p[m8][(t8 >= e - 30) & (t8 < e + 30)].mean() if np.any((t8 >= e - 30) & (t8 < e + 30)) else np.nan for e in te])
ok = ~np.isnan(pm)
rep["P8_temp_range_c"] = {r: [round(float(np.nanmin(T[r][ok])), 2), round(float(np.nanmax(T[r][ok])), 2)] for r in roles}
rep["P8_power_vs_temp_db_per_c"] = {}
for r in roles:
    g = ok & ~np.isnan(T[r])
    if g.sum() > 10 and np.ptp(T[r][g]) > 0:
        s, _ = np.polyfit(T[r][g], 10 * np.log10(pm[g]), 1)
        rep["P8_power_vs_temp_db_per_c"][r] = {"slope": round(float(s), 4), "corr": round(float(np.corrcoef(T[r][g], pm[g])[0, 1]), 3)}

# spectra: high (P9) over low (end of P8)
S = np.load(OUT / "spectra.npy"); M = json.load(open(OUT / "spectra_meta.json")); sph = np.array([m["phase"] for m in M])
f = (np.arange(S.shape[1]) - S.shape[1] / 2) * 2400 / S.shape[1]
lo, hi = S[sph == "P8_long"][-10:].mean(0), S[sph == "P9_rtl_tcp_restart"].mean(0)
ratio = 10 * np.log10(hi / lo)
band = (np.abs(f) > 30) & (np.abs(f) < 1100)
rep["spectral_ratio_high_over_low_db"] = {"mean": round(float(ratio[band].mean()), 3), "std": round(float(ratio[band].std()), 3)}
(OUT / "report.json").write_text(json.dumps(rep, indent=1))

fig, ax = plt.subplots(2, 2, figsize=(14, 9))
h = (t - t[0]) / 3600
on = bias == "1"
ax[0, 0].plot(h[on], np.sqrt(p[on]), ",", color="#3987e5")
for name in dict.fromkeys(ph):
    hm = h[ph == name]; ax[0, 0].axvspan(hm[0], hm[-1], alpha=0.06 if list(dict.fromkeys(ph)).index(name) % 2 else 0)
    ax[0, 0].text(hm.mean(), 17.6, name.split("_")[0], ha="center", fontsize=8)
ax[0, 0].set(title="rms per second, LNA on (all phases)", xlabel="hours since start", ylabel="rms", ylim=(13.3, 17.8))
ax[0, 1].loglog(taus, adev, "o-", label="P8 measured"); ax[0, 1].loglog(taus, [1 / np.sqrt(2.4e6 * x) for x in taus], "--", label="radiometer ideal")
ax[0, 1].set(title="Allan deviation, P8 (4 h untouched)", xlabel="tau [s]"); ax[0, 1].legend()
ax2 = ax[1, 0]; ax2.plot((te[ok] - te[ok][0]) / 3600, 10 * np.log10(pm[ok] / np.nanmean(pm[ok])), "k-", label="power (dB)")
ax3 = ax2.twinx()
for r, c in zip(roles, ["#3987e5", "#d95926", "#199e70", "#c98500"]):
    ax3.plot((te[ok] - te[ok][0]) / 3600, T[r][ok], color=c, lw=1, label=r)
ax2.set(title="P8: power vs temperatures", xlabel="hours", ylabel="dB"); ax3.set_ylabel("°C"); ax3.legend(loc="lower right", fontsize=8)
ax[1, 1].plot(f, ratio, lw=0.5); ax[1, 1].set(title="spectrum ratio high/low state (dB)", xlabel="kHz", ylim=(1.0, 2.5))
fig.tight_layout(); fig.savefig(OUT / "report.png", dpi=90)
print(json.dumps(rep, indent=1))
