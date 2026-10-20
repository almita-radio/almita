"""MAIN RTL-SDR health tests over rtl_tcp - nothing stops the service (first run 2026-10-17, RTL-SDR V3).

A liveness (rms / DC / clipping)   B USB throughput (bytes/s vs expected)   C gain curve (R820T table)
D LNA via Bias-T ON vs OFF          E spur survey (averaged FFT)              F stability: Allan deviation of total power
Leaves rtl_tcp tuned to the operating configuration (1420405752 Hz, 2.4 MS/s, 40.2 dB, AGC off, Bias-T ON).

Usage: .venv/bin/python scripts/hardware/sdr_health.py <output_dir> [allan_seconds=900] ["setup description"]
Bias-T is switched OFF and ON during D: only with the LNA in the chain (never a 50 ohm load straight on the SDR).
Run it with the Pi otherwise idle - its own load moved the received power by ~1.6 dB on 2026-10-17."""
import json
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HOST, PORT = "127.0.0.1", 1234
F0, FS, G0 = 1420405752, 2400000, 402
GAINS = [0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496]
OUT = Path(sys.argv[1])
ALLAN_SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 900
SETUP = sys.argv[3] if len(sys.argv) > 3 else "unspecified setup"
OUT.mkdir(parents=True, exist_ok=True)


def log(*a):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}]", *a, flush=True)


class Rtl:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=10)
        hdr = self.s.recv(12)
        self.tuner = struct.unpack(">I", hdr[4:8])[0]

    def cmd(self, c, v):
        self.s.sendall(struct.pack(">BI", c, v & 0xFFFFFFFF))

    def read(self, n):
        buf = bytearray(n); view = memoryview(buf); got = 0
        while got < n:
            k = self.s.recv_into(view[got:], min(1 << 20, n - got))
            if not k:
                raise RuntimeError("rtl_tcp closed")
            got += k
        return buf

    def flush(self, seconds=0.5):
        self.read(int(FS * 2 * seconds) & ~1)

    def iq(self, seconds):
        x = np.frombuffer(self.read(int(FS * 2 * seconds) & ~1), np.uint8).astype(np.float32) - 127.5
        return x[0::2] + 1j * x[1::2], x

    def setup(self, freq=F0, gain=G0, bias=1):
        self.cmd(0x02, FS); self.cmd(0x03, 1); self.cmd(0x04, gain); self.cmd(0x08, 0); self.cmd(0x0E, bias); self.cmd(0x01, freq)
        time.sleep(0.3); self.flush()


def stats(x_real):
    rms = float(np.sqrt(np.mean(x_real ** 2)) * np.sqrt(2))       # |I+jQ| rms
    clip = float(np.mean((x_real <= -127.5) | (x_real >= 127.5)))
    return rms, clip


def allan(power):
    y = np.asarray(power) / np.mean(power); taus, adev = [], []
    m = 1
    while m <= len(y) // 3:
        yb = y[: (len(y) // m) * m].reshape(-1, m).mean(axis=1)
        if len(yb) >= 3:
            taus.append(m); adev.append(float(np.sqrt(0.5 * np.mean(np.diff(yb) ** 2))))
        m *= 2
    return taus, adev


def load_snapshot():
    try:
        load1 = float(Path("/proc/loadavg").read_text().split()[0])
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return {}
    return {"load1": load1, "cpu_temp_c": temp}


report = {"started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "setup": SETUP,
          "operating": {"freq_hz": F0, "rate": FS, "gain_db": G0 / 10}, "pi_at_start": load_snapshot()}
r = Rtl(); report["tuner_type_code"] = r.tuner
log("connected, tuner type", r.tuner, "|", SETUP)

r.setup()
z, x = r.iq(1.0)
rms, clip = stats(x)
report["A_liveness"] = {"rms": rms, "dc_i": float(np.mean(x[0::2])), "dc_q": float(np.mean(x[1::2])), "clip_fraction": clip,
                        "iq_imbalance_db": float(10 * np.log10(np.var(x[0::2]) / np.var(x[1::2])))}
log("A liveness", report["A_liveness"])

t0 = time.monotonic(); n = 0; chunk = FS * 2 // 10
while time.monotonic() - t0 < 30:
    n += len(r.read(chunk))
dt = time.monotonic() - t0
report["B_throughput"] = {"seconds": dt, "bytes_per_s": n / dt, "expected_bytes_per_s": FS * 2, "ratio": n / dt / (FS * 2)}
log("B throughput", report["B_throughput"])

curve = []
for g in GAINS:
    r.cmd(0x04, g); time.sleep(0.25); r.flush(0.3)
    _, x = r.iq(0.5); rms, clip = stats(x)
    curve.append({"gain_db": g / 10, "rms": rms, "power_db": float(20 * np.log10(max(rms, 1e-9))), "clip_fraction": clip})
r.cmd(0x04, G0); time.sleep(0.25); r.flush()
report["C_gain_curve"] = curve
log("C gain curve", [(c["gain_db"], round(c["rms"], 2)) for c in curve])

d = {}
for label, b in (("on_before", 1), ("off", 0), ("on_after", 1)):
    r.cmd(0x0E, b); time.sleep(1.0); r.flush(0.5)
    _, x = r.iq(1.0); d[label] = stats(x)[0]
d["lna_contribution_db"] = float(20 * np.log10(d["on_after"] / d["off"]))
d["on_repeatability_db"] = float(20 * np.log10(d["on_after"] / d["on_before"]))
report["D_bias_tee_lna"] = d
log("D bias-T", d)

NFFT = 8192
psd_acc = np.zeros(NFFT); psd_n = 0
power, pi_load = [], []
log(f"E/F start: {ALLAN_SECONDS} s at 1420.405752 MHz, 40.2 dB, Bias-T ON")
t_start = time.monotonic()
for sec in range(ALLAN_SECONDS):
    z, x = r.iq(1.0)
    power.append(float(np.mean(np.abs(z) ** 2)))
    if sec % 10 == 0:
        pi_load.append({"second": sec, **load_snapshot()})
    if sec < 20:
        segs = z[: (len(z) // NFFT) * NFFT].reshape(-1, NFFT) * np.hanning(NFFT)
        psd_acc += np.mean(np.abs(np.fft.fftshift(np.fft.fft(segs, axis=1), axes=1)) ** 2, axis=0); psd_n += 1
    if sec % 60 == 59:
        log(f"  {sec + 1}/{ALLAN_SECONDS} s, power drift so far {10 * np.log10(power[-1] / power[0]):+.3f} dB, Pi {load_snapshot()}")
elapsed = time.monotonic() - t_start
psd = 10 * np.log10(psd_acc / psd_n)
freqs = F0 + np.fft.fftshift(np.fft.fftfreq(NFFT, 1 / FS))
edge = int(NFFT * 0.05); core = slice(edge, NFFT - edge)
base = np.convolve(psd, np.ones(65) / 65, mode="same")
excess = psd - base
dc = np.abs(freqs - F0) < 3000
peaks = []
for i in [i for i in np.where((excess > 3.0) & ~dc)[0] if core.start <= i < core.stop]:
    if not peaks or i - peaks[-1]["bin"] > 3:
        peaks.append({"bin": int(i), "freq_hz": float(freqs[i]), "excess_db": float(excess[i])})
    elif excess[i] > peaks[-1]["excess_db"]:
        peaks[-1].update(bin=int(i), freq_hz=float(freqs[i]), excess_db=float(excess[i]))
report["E_spurs"] = {"nfft": NFFT, "bin_hz": FS / NFFT, "threshold_db": 3.0, "count": len(peaks),
                     "top": sorted(peaks, key=lambda p: -p["excess_db"])[:15],
                     "dc_spike_db": float(np.max(excess[dc])), "passband_ripple_db": float(np.ptp(base[core]))}
log("E spurs", report["E_spurs"]["count"], "candidates; DC", round(report["E_spurs"]["dc_spike_db"], 1), "dB")

p = np.array(power)
taus, adev = allan(p)
radiometer = [1 / np.sqrt(FS * t) for t in taus]
imin = int(np.argmin(adev))
report["F_allan"] = {"seconds": len(p), "wall_seconds": elapsed, "tau_s": taus, "adev": adev, "radiometer_ideal": radiometer,
                     "best_tau_s": taus[imin], "best_adev": adev[imin],
                     "total_drift_db": float(10 * np.log10(np.mean(p[-30:]) / np.mean(p[:30]))),
                     "power_series_mean_per_s": power, "pi_load_every_10s": pi_load}
log("F Allan", list(zip(taus, [f"{a:.2e}" for a in adev])), "best tau", taus[imin], "s")

r.setup(F0, G0, 1)
_, x = r.iq(1.0)
report["restored"] = {"freq_hz": F0, "gain_db": G0 / 10, "agc": "off", "bias_tee": "on", "rms_after": stats(x)[0]}
r.s.close()
report["ended_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
(OUT / "sdr_health.json").write_text(json.dumps(report, indent=1))
np.savez(OUT / "sdr_health_arrays.npz", psd_db=psd, freqs_hz=freqs, power=p)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
fig, ax = plt.subplots(2, 2, figsize=(13, 8))
ax[0, 0].plot([c["gain_db"] for c in curve], [c["power_db"] for c in curve], "o-"); ax[0, 0].set(title="C gain curve (1420.4 MHz)", xlabel="tuner gain (dB)", ylabel="20·log10(rms) (dB)"); ax[0, 0].grid(alpha=.3)
ax[0, 1].plot((freqs - F0) / 1e3, psd, lw=.6)
for q in report["E_spurs"]["top"]:
    ax[0, 1].axvline((q["freq_hz"] - F0) / 1e3, color="r", alpha=.3)
ax[0, 1].set(title="E averaged spectrum (20 s), red = spur candidates", xlabel="offset from 1420.405752 MHz (kHz)", ylabel="dB (arb.)"); ax[0, 1].grid(alpha=.3)
ax[1, 0].loglog(taus, adev, "o-", label="measured"); ax[1, 0].loglog(taus, radiometer, "--", label="ideal radiometer 1/√(Bτ)")
ax[1, 0].set(title="F Allan deviation of total power", xlabel="τ (s)", ylabel="σ/P"); ax[1, 0].legend(); ax[1, 0].grid(alpha=.3, which="both")
ax[1, 1].plot(np.arange(len(p)) / 60, 10 * np.log10(p / p[0])); ax[1, 1].set(title="F total power vs time", xlabel="minutes", ylabel="dB vs first second"); ax[1, 1].grid(alpha=.3)
fig.suptitle(f"MAIN RTL-SDR health - {report['started_utc']} - {SETUP}")
fig.tight_layout(); fig.savefig(OUT / "sdr_health.png", dpi=110)
log("done ->", OUT)
