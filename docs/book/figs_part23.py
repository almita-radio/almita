"""Figures for Parts II (capture) and III (processing) of the ALMITA book.

Measured figures read docs/book/data_part2.npz (made by extract_part2.py from 2026-11-12 captures);
model figures are simulated here with fixed seeds. Prints every number quoted in the text.
Run: .venv/bin/python docs/book/figs_part23.py
"""
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[1]))
import fswitch as F  # noqa: E402
import reduce_estimator_v2 as V2  # noqa: E402

INK, MUTED, GRID = "#1b1b1f", "#6b6b75", "#d9d9de"
BLUE, RUST, GREEN = "#1f5a8c", "#b5532a", "#3d7a46"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": MUTED,
    "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.6, "svg.fonttype": "none", "lines.linewidth": 2,
})


def save(fig, name):
    fig.savefig(OUT / f"{name}.svg", bbox_inches="tight", transparent=True)
    plt.close(fig)


OUT = HERE / "fig"
D = np.load(HERE / "data_part2.npz")
bb = D["bb_hz"] / 1e3   # kHz
NU0 = 1420405752.0
KMS = 299792.458
rng = np.random.default_rng(7)


def smooth(y, n):
    return np.convolve(y, np.ones(n) / n, "same")


def db(x):
    return 10 * np.log10(x)


# ---------- F6.2 complex samples: +f and -f differ only in Q ----------
t = np.linspace(0, 20, 400)  # microseconds
fig, axs = plt.subplots(1, 2, figsize=(6.6, 2.3), sharey=True)
for ax, f, title in ((axs[0], .1, "señal en +100 kHz (sobre la sintonía)"), (axs[1], -.1, "señal en −100 kHz (bajo la sintonía)")):
    ax.plot(t, np.cos(2 * np.pi * f * t), color=BLUE, label="I")
    ax.plot(t, np.sin(2 * np.pi * f * t), color=RUST, label="Q")
    ax.set_title(title, fontsize=9); ax.set_xlabel("tiempo (µs)")
axs[0].legend(frameon=False, ncol=2, loc="lower left", fontsize=8)
axs[0].set_yticks([-1, 0, 1])
save(fig, "f6_iq")

# ---------- F6.3 aliasing ----------
fig, ax = plt.subplots(figsize=(6.4, 2.2))
tt = np.linspace(0, 2, 2000)
ax.plot(tt, np.cos(2 * np.pi * 3 * tt), color=MUTED, lw=1.2, label="señal real: 3 ciclos por segundo")
ts = np.arange(0, 2.001, .25)
ax.plot(ts, np.cos(2 * np.pi * 3 * ts), "o", color=RUST, ms=6, label="muestras: 4 por segundo")
ax.plot(tt, np.cos(2 * np.pi * 1 * tt), color=RUST, lw=1.2, ls="--", label="lo que parece: 1 ciclo por segundo")
ax.set_xlabel("tiempo (s)"); ax.set_yticks([]); ax.legend(frameon=False, fontsize=7.5, loc="upper center", ncol=3, bbox_to_anchor=(.5, 1.22))
save(fig, "f6_alias")

# ---------- F6.4 real samples and histogram ----------
raw = D["raw_first"].astype(float) - 127.5
hist = D["hist"]
fig, axs = plt.subplots(1, 2, figsize=(6.6, 2.4), gridspec_kw={"width_ratios": [1.6, 1]})
ax = axs[0]
n = np.arange(60)
ax.step(n / 2.4, raw[0:120:2], where="mid", color=BLUE, lw=1.2, label="I")
ax.step(n / 2.4, raw[1:120:2], where="mid", color=RUST, lw=1.2, label="Q")
ax.set_xlabel("tiempo (µs)"); ax.set_ylabel("código − 127,5"); ax.legend(frameon=False, fontsize=8, ncol=2)
ax = axs[1]
codes = np.arange(256)
ax.bar(codes, hist / hist.sum() * 100, width=1, color=BLUE)
ax.set_xlim(0, 255); ax.set_xlabel("código del ADC (0–255)"); ax.set_ylabel("% de las muestras")
ax.axvline(127.5, color=MUTED, lw=.8)
ax.text(200, ax.get_ylim()[1] * .7, "rango\nsin usar", ha="center", fontsize=7.5, color=MUTED)
ax.text(55, ax.get_ylim()[1] * .7, "rango\nsin usar", ha="center", fontsize=7.5, color=MUTED)
save(fig, "f6_raw")
print("rms codes: load %.2f, sky low %.2f, sky high %.2f ; (load/sky)^2 = %.2f" % (
    D["rms_AMBIENT_50R"], D["rms_HI_BAJO"], D["rms_HI_ALTO"], (D["rms_AMBIENT_50R"] / D["rms_HI_BAJO"]) ** 2))
used = hist > hist.sum() * 1e-6
print("codes used (>1e-6): %d of 256, from %d to %d" % (used.sum(), codes[used].min(), codes[used].max()))

# ---------- F7.1 window leakage (model) ----------
N = 64
fig, ax = plt.subplots(figsize=(6.4, 2.5))
x = np.arange(N)
sig = np.exp(2j * np.pi * 10.5 * x / N)
for win, col, lab in ((np.ones(N), MUTED, "sin ventana (rectangular)"), (np.hanning(N), BLUE, "ventana de Hann")):
    p = np.abs(np.fft.fft(sig * win, 4096)) ** 2
    p = p / p.max()
    ax.plot(np.arange(4096) / 4096 * N, db(p + 1e-12), color=col, label=lab)
ax.set_xlim(0, 24); ax.set_ylim(-80, 3)
ax.set_xlabel("canal"); ax.set_ylabel("dB respecto del máximo")
ax.legend(frameon=False, fontsize=8, loc="upper right")
ax.text(4.5, -12, "la fuga de energía de una\nportadora entre dos canales", fontsize=7.5, color=MUTED, ha="center")
save(fig, "f7_window")

# ---------- F7.2 averaging frames (measured) ----------
fig, ax = plt.subplots(figsize=(6.4, 2.8))
sel = (bb > 100) & (bb < 160)
ref = np.median(D["mean128"][sel])
for key, col, lab, off in (("single_frame", GRID, "1 cuadro (3,4 ms)", 0), ("mean10", MUTED, "10 cuadros (34 ms)", 0),
                           ("mean128", BLUE, "128 cuadros (0,44 s)", 0)):
    ax.plot(bb[sel], D[key][sel] / ref, color=col, lw=1 if key != "mean128" else 1.6, label=lab)
ax.set_xlabel("frecuencia respecto de la sintonía (kHz)"); ax.set_ylabel("potencia / su mediana")
ax.set_ylim(0, 5); ax.legend(frameon=False, fontsize=8, loc="upper right")
save(fig, "f7_avg")
for key in ("single_frame", "mean10", "mean128"):
    v = D[key][sel] / np.median(D[key][sel])
    print(f"{key}: relative scatter {np.std(v)/np.mean(v)*100:.1f} %")

# ---------- F8.1 band shape (measured) ----------
fig, ax = plt.subplots(figsize=(6.4, 3.0))
for key, col, lab in (("AMBIENT_50R", RUST, "carga de 50 Ω (≈ 301 K)"), ("HI_BAJO", BLUE, "cielo frío (HI BAJO, 74° de altura)"),
                      ("HI_ALTO", GREEN, "plano galáctico (HI ALTO)")):
    y = smooth(D[f"psd_{key}"], 33)
    ax.plot(bb, db(y / np.median(D["psd_HI_BAJO"])), color=col, lw=1.3, label=lab)
ax.set_xlim(-1200, 1200); ax.set_ylim(-9, 7)
ax.axvline(0, color=MUTED, lw=.6, ls=":")
ax.set_xlabel("frecuencia respecto de la sintonía (kHz)"); ax.set_ylabel("dB (0 = mediana del cielo frío)")
ax.legend(frameon=False, fontsize=8, loc="lower center")
save(fig, "f8_band")
inner = np.abs(bb) < 1000
y = smooth(D["psd_AMBIENT_50R"], 101)[inner]
print("load band shape within +-1 MHz: +%.1f / %.1f %% around median" % ((y.max() / np.median(y) - 1) * 100, (y.min() / np.median(y) - 1) * 100))
print("load/sky median ratio = %.3f (%.2f dB)" % (np.median(D["psd_AMBIENT_50R"][inner]) / np.median(D["psd_HI_BAJO"][inner]),
                                                     db(np.median(D["psd_AMBIENT_50R"][inner]) / np.median(D["psd_HI_BAJO"][inner]))))

# ---------- F8.2 dividing two spectra (measured) ----------
fig, ax = plt.subplots(figsize=(6.4, 2.6))
r = D["psd_HI_ALTO"] / D["psd_HI_BAJO"]
r = smooth(r, 65) / np.median(r[inner]) - 1
v = -bb * 1e3 / NU0 * KMS
ok = inner & (np.abs(bb) > 10)
ax.plot(v[ok], r[ok] * 100, color=BLUE, lw=1.3)
ax.axhline(0, color=MUTED, lw=.7)
ax.set_xlabel("velocidad topocéntrica (km/s)"); ax.set_ylabel("ALTO / BAJO − 1 (%)")
ax.set_xlim(-210, 210)
save(fig, "f8_ratio")
print("ALTO/BAJO peak %.1f %% at %.0f km/s" % (np.nanmax(r[ok]) * 100, v[ok][np.nanargmax(r[ok])]))

# ---------- F10.1 frequency switching step by step (measured) ----------
A, B = D["fs_A"], D["fs_B"]
fa, fb = float(D["fs_fa"]), float(D["fs_fb"])
S = F.switched(A, B)
rf = np.linspace(NU0 - 1.2e6, NU0 + 1.2e6, 8192)
T = F.fold(S, D["bb_hz"], fa, fb, rf)
vrf = (NU0 - rf) / NU0 * KMS
fig, axs = plt.subplots(3, 1, figsize=(6.4, 6.4))
ax = axs[0]
ax.plot(bb, db(smooth(A, 33) / np.median(B)), color=BLUE, lw=1.1, label="A: sintonía 600 kHz abajo")
ax.plot(bb, db(smooth(B, 33) / np.median(B)), color=RUST, lw=1.1, label="B: sintonía 600 kHz arriba")
ax.set_xlim(-1200, 1200); ax.set_ylim(-6, 4); ax.set_ylabel("dB")
ax.set_title("1 · los dos espectros crudos: la forma del receptor domina", fontsize=9, loc="left")
ax.legend(frameon=False, fontsize=7.5, loc="lower center", ncol=2)
ax = axs[1]
okb = (np.abs(bb) < 1000) & (np.abs(bb) > 10)
ax.plot(bb[okb], smooth(S, 33)[okb] * 100, color=INK, lw=1)
ax.axvline(600, color=BLUE, lw=.8, ls=":"); ax.axvline(-600, color=RUST, lw=.8, ls=":")
ax.text(600, 9, "línea en A", color=BLUE, fontsize=7.5, ha="center"); ax.text(-600, -14, "línea en B (negativa)", color=RUST, fontsize=7.5, ha="center")
ax.set_xlim(-1200, 1200); ax.set_ylabel("(A − B)/B  (%)")
ax.set_title("2 · al dividir, la forma se cancela y queda la línea dos veces", fontsize=9, loc="left")
ax.set_xlabel("frecuencia respecto de la sintonía de cada mitad (kHz)", fontsize=8)
ax = axs[2]
ok = np.isfinite(T)
ax.plot(vrf[ok], smooth(np.nan_to_num(T), 33)[ok] * 100, color=GREEN, lw=1.4)
ax.axhline(0, color=MUTED, lw=.7)
ax.set_xlim(-210, 210); ax.set_ylabel("exceso (%)"); ax.set_xlabel("velocidad topocéntrica (km/s)")
ax.set_title("3 · plegado: la línea una vez, en su frecuencia real, en una ventana de ±84 km/s", fontsize=9, loc="left")
fig.tight_layout()
save(fig, "f10_fs")
print("fold peak %.1f %% ; band ripple A within +-1MHz: %.1f %%" % (np.nanmax(smooth(np.nan_to_num(T), 33)[ok]) * 100,
      (smooth(A, 101)[inner].max() / np.median(A[inner]) - 1) * 100))
print("fold window km/s: %.0f..%.0f" % (vrf[ok].min(), vrf[ok].max()))

# ---------- F10.2 window and ghosts vs throw (model) ----------
fig, axs = plt.subplots(2, 1, figsize=(6.4, 2.8), sharex=True)
vv = np.linspace(-300, 300, 1200)
line = np.exp(-.5 * (vv / 18) ** 2)
for ax, d in zip(axs, (600, 300)):
    dv = d * 1e3 / NU0 * KMS
    win = (1000 - d) * 1e3 / NU0 * KMS
    y = line - .5 * np.exp(-.5 * ((vv - 2 * dv) / 18) ** 2) - .5 * np.exp(-.5 * ((vv + 2 * dv) / 18) ** 2)
    ax.axvspan(-win, win, color=GREEN, alpha=.12, lw=0)
    ax.plot(vv, y, color=INK, lw=1.3)
    ax.text(-290, .55, f"salto ±{d} kHz", fontsize=8.5, color=INK)
    ax.text(0, -.85, f"ventana útil ±{win:.0f} km/s", fontsize=7.5, color=GREEN, ha="center")
    ax.text(2 * dv, -.85, f"fantasma\n+{2*dv:.0f}", fontsize=7.5, color=RUST, ha="center")
    ax.text(-2 * dv, -.85, f"fantasma\n−{2*dv:.0f}", fontsize=7.5, color=RUST, ha="center")
    ax.set_ylim(-1.2, 1.2); ax.set_yticks([])
axs[1].set_xlabel("velocidad (km/s) · gas local en 0")
save(fig, "f10_ghost")

# ---------- F11.1 spectral kurtosis: model ----------
M = 128
lo, hi = V2.sk_limits(M)
noise = rng.exponential(1, (4000, M))
sk_noise = V2.spectral_kurtosis(noise.T)
puls = rng.exponential(1, (4000, M)); puls[:, :3] *= 25
sk_puls = V2.spectral_kurtosis(puls.T)
car = np.abs(4 + (rng.normal(size=(4000, M)) + 1j * rng.normal(size=(4000, M))) / np.sqrt(2)) ** 2
sk_car = V2.spectral_kurtosis(car.T)
fig, ax = plt.subplots(figsize=(6.4, 2.6))
bins = np.linspace(0, 3.2, 160)
for s, col, lab in ((sk_noise, BLUE, "ruido (cielo, carga, línea HI)"), (sk_puls, RUST, "ruido + pulsos (3 cuadros de 128)"),
                    (sk_car, GREEN, "portadora fuerte y estable")):
    ax.hist(np.clip(s, 0, 3.19), bins=bins, color=col, alpha=.75, label=lab)
ax.axvspan(lo, hi, color=GRID, alpha=.5, lw=0)
ax.text((lo + hi) / 2, ax.get_ylim()[1] * .9, "se acepta", ha="center", fontsize=8)
ax.set_xlabel("curtosis espectral SK (M = 128 cuadros)"); ax.set_yticks([])
ax.legend(frameon=False, fontsize=7.5, loc="upper right")
save(fig, "f11_sk_model")
print(f"SK limits M=128: {lo:.3f} .. {hi:.3f} ; sigma {V2.sk_sigma(M):.3f}; noise flagged {np.mean((sk_noise<lo)|(sk_noise>hi))*100:.3f} %")

# ---------- F11.2 spectral kurtosis on a real block ----------
sk = D["sk_block"]
fig, ax = plt.subplots(figsize=(6.4, 2.4))
okb = (np.abs(bb) < 1140)
ax.plot(bb[okb], sk[okb], ",", color=BLUE, alpha=.6)
ax.axhline(D["sk_limits"][0], color=RUST, lw=.8, ls="--"); ax.axhline(D["sk_limits"][1], color=RUST, lw=.8, ls="--")
ax.set_ylim(0.3, 2.2); ax.set_xlabel("frecuencia respecto de la sintonía (kHz)"); ax.set_ylabel("SK")
save(fig, "f11_sk_real")
flag = ((sk < D["sk_limits"][0]) | (sk > D["sk_limits"][1]))[okb]
print("real block: SK median %.3f, flagged %d of %d channels (%.2f %%)" % (np.median(sk[okb]), flag.sum(), flag.size, flag.mean() * 100))

# ---------- F11.3 median vs mean (model) ----------
trials, nfr = 3000, 64
x = rng.exponential(1, (trials, nfr))
med = np.median(x, axis=1) / np.log(2)
mea = x.mean(axis=1)
print("median vs mean scatter ratio (64 frames): %.2f -> time efficiency %.0f %%" % (med.std() / mea.std(), 100 * (mea.std() / med.std()) ** 2))

# ---------- F12.2 baseline fit (model on a measured fold) ----------
yb = smooth(np.nan_to_num(T), 33)
okk = np.isfinite(T)
fit_ok = okk & (np.abs(vrf) > 60)
pp = np.polyfit(vrf[fit_ok], yb[fit_ok], 1)
fig, ax = plt.subplots(figsize=(6.4, 2.4))
ax.plot(vrf[okk], yb[okk] * 100, color=INK, lw=1.1, label="espectro plegado (12-11, l ≈ 20°)")
ax.plot(vrf[okk], np.polyval(pp, vrf[okk]) * 100, color=RUST, lw=1.4, ls="--", label="línea de base ajustada")
ax.axvspan(-60, 60, color=GREEN, alpha=.1, lw=0)
ax.text(0, ax.get_ylim()[0] + .5, "zona de la línea: no se usa para ajustar", ha="center", fontsize=7.5, color=GREEN)
ax.set_xlim(-90, 90); ax.set_xlabel("velocidad topocéntrica (km/s)"); ax.set_ylabel("%")
ax.legend(frameon=False, fontsize=7.5, loc="upper left")
save(fig, "f12_base")
print("done")
