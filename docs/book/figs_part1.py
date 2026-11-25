"""Figures for Part I (physics) of the ALMITA book.

Writes SVG files into docs/book/fig/ and prints the numbers quoted in the text,
so every figure in the book can be traced back to a computation.
Run: .venv/bin/python docs/book/figs_part1.py
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Circle, Ellipse

OUT = Path(__file__).with_name("fig")
OUT.mkdir(exist_ok=True)

INK, MUTED, GRID = "#1b1b1f", "#6b6b75", "#d9d9de"
BLUE, RUST, GREEN = "#1f5a8c", "#b5532a", "#3d7a46"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": MUTED,
    "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.6, "svg.fonttype": "none",
    "lines.linewidth": 2,
})

h, k, c = 6.62607015e-34, 1.380649e-23, 299792458.0
NU0 = 1420405752.0
KPC = 1.0
R0, V0 = 8.2, 220.0


def save(fig, name):
    fig.savefig(OUT / f"{name}.svg", bbox_inches="tight", transparent=True)
    plt.close(fig)


# ---------- numbers used in the text ----------
lam = c / NU0
print(f"lambda = {lam*100:.3f} cm ; h nu / k = {h*NU0/k:.4f} K ; dE = {h*NU0/1.602176634e-19*1e6:.3f} ueV")
A10 = 2.8843e-15
print(f"1/A10 = {1/A10/3.156e7/1e6:.1f} Myr")
print(f"1 km/s = {NU0*1e3/c/1e3:.3f} kHz ; 2.4 MHz half band = {1.2e6/NU0*c/1e3:.1f} km/s")
print(f"256-bin channel = {2.4e6/256/1e3:.3f} kHz = {2.4e6/256/NU0*c/1e3:.2f} km/s ; 8192 ch = {2.4e6/8192:.0f} Hz = {2.4e6/8192/NU0*c:.0f} m/s")
print(f"carrier 1420.0398 -> {(NU0-1420.0398e6)/NU0*c/1e3:.1f} km/s")
print(f"kTB 122 K 2.4 MHz = {k*122*2.4e6:.2e} W = {10*np.log10(k*122*2.4e6/1e-3):.1f} dBm")
print(f"earth rotation at -33.45: {0.4651*np.cos(np.radians(33.4489)):.3f} km/s")
for D in (0.9, 0.6):
    print(f"beam 1.2 lam/D D={D}: {np.degrees(1.2*lam/D):.1f} deg")
Y = 10**(4.71/10); Tl = 273.15+27.8
Trx = (Tl - Y*122)/(Y-1)  # if Tsys_sky = 122, solve Trx from Y = (Tl+Trx)/(Tsky+Trx)
print(f"Y 4.71 dB = {Y:.2f}; load {Tl:.1f} K")
for L_db in (0.2, 1.0, 4.0):
    L = 10**(L_db/10)
    print(f"loss {L_db} dB at 290 K adds {(L-1)*290:.0f} K at the far side")
S = 70e-26*1e4  # 70 sfu in W/m2/Hz (1 sfu = 1e-22)
S = 70e-22
for Aeff in (0.31,):
    print(f"Sun 70 sfu, Aeff {Aeff}: T_A = {S*Aeff/(2*k):.0f} K")
print(f"radiometer 2.4 MHz 1 s: {1/np.sqrt(2.4e6):.2e}")
B = 2.4e6/256; t = 30*0.95/2
s1 = 1/np.sqrt(B*t); print(f"shirt point: per tuning {s1*100:.3f} %, folded {s1*np.sqrt(2)/np.sqrt(2)*100:.3f} %")
for l in (20, 35, 10):
    s = np.sin(np.radians(l))
    print(f"l={l}: R_tan={R0*s:.2f} kpc, d_tan={R0*np.cos(np.radians(l)):.2f} kpc, v_tan(flat)={V0*(1-s):.0f} km/s")


# ---------- F1.1 Planck curves and the Rayleigh-Jeans regime ----------
def planck(nu, T):
    return 2*h*nu**3/c**2/np.expm1(h*nu/(k*T))


nu = np.logspace(7, 15.5, 600)
fig, ax = plt.subplots(figsize=(6.4, 3.4))
for T, col, lab in ((2.725, MUTED, "2,7 K (fondo cósmico)"), (122, BLUE, "122 K (nuestra Tsys)"),
                    (300, GREEN, "300 K (la carga, el suelo)"), (5800, RUST, "5800 K (superficie del Sol)")):
    ax.loglog(nu, planck(nu, T), color=col, label=lab)
    rj = nu < 3e12 if T > 100 else nu < 1e11
    ax.loglog(nu[rj], 2*nu[rj]**2*k*T/c**2, color=col, lw=1, ls=(0, (2, 2)))
ax.axvline(NU0, color=INK, lw=1)
ax.text(NU0*1.25, 1e-14, "1420 MHz\n(21 cm)", fontsize=8.5, color=INK, va="top")
ax.axvspan(4e14, 7.5e14, color="#f2d98a", alpha=.5, lw=0)
ax.text(5.4e14, 3e-25, "luz\nvisible", fontsize=8, ha="center", color=INK)
ax.set_xlim(1e7, 3e15); ax.set_ylim(1e-27, 1e-6)
ax.set_xlabel("frecuencia (Hz)"); ax.set_ylabel("brillo  (W m⁻² Hz⁻¹ sr⁻¹)")
ax.legend(frameon=False, fontsize=8, loc="upper left")
save(fig, "f1_planck")


# ---------- F3.1 the frequency axis is a velocity axis ----------
fig, ax = plt.subplots(figsize=(6.4, 1.9))
ax.set_xlim(1419.205, 1421.605); ax.set_ylim(0, 1)
ax.set_yticks([]); ax.spines["left"].set_visible(False); ax.grid(False)
ax.set_xlabel("frecuencia (MHz), tal como la muestra QUICKLOOK")
sec = ax.secondary_xaxis("top", functions=(lambda f: (NU0/1e6 - f)/(NU0/1e6)*c/1e3,
                                           lambda v: NU0/1e6*(1 - v*1e3/c)))
sec.set_xlabel("velocidad (km/s): positiva = se aleja")
for f, lab, col in ((NU0/1e6, "0 km/s\n1420,4058", INK), (1420.0398, "portadora\n+77 km/s", RUST)):
    ax.axvline(f, color=col, lw=1.4)
    ax.text(f, .52, lab, fontsize=8, ha="center", color=col,
            bbox=dict(fc="white", ec="none", pad=1))
x = np.linspace(1419.205, 1421.605, 500)
v = (NU0/1e6 - x)/(NU0/1e6)*c/1e3
ax.fill_between(x, 0, .38*np.exp(-.5*((v-20)/25)**2), color=BLUE, alpha=.35, lw=0)
ax.text(1420.15, .08, "hidrógeno\nque se aleja", fontsize=7.5, color=BLUE, ha="right")
save(fig, "f3_axis")


# ---------- F3.2 how fast everything moves ----------
items = [("Rotación de la Tierra (Santiago)", 0.39), ("Sol respecto de sus vecinas (LSR)", 20),
         ("Tierra alrededor del Sol", 29.8), ("Hidrógeno en el punto tangente, l = 20°", 145),
         ("Sol alrededor de la galaxia", 220)]
fig, ax = plt.subplots(figsize=(6.4, 2.2))
names = [i[0] for i in items][::-1]; vals = [i[1] for i in items][::-1]
cols = [BLUE if "Hidr" in n else MUTED for n in names]
ax.barh(names, vals, color=cols, height=.55)
for i, vv in enumerate(vals):
    ax.text(vv + 3, i, f"{vv:g} km/s".replace(".", ","), va="center", fontsize=8.5, color=INK)
ax.set_xlim(0, 270); ax.set_xlabel("km/s"); ax.grid(axis="y", visible=False)
save(fig, "f3_speeds")


# ---------- F4.1 top view of the Galaxy, line of sight l = 35 ----------
def vr_along(l_deg, d):
    l = np.radians(l_deg)
    R = np.sqrt(R0**2 + d**2 - 2*R0*d*np.cos(l))
    return V0*(R0/R - 1)*np.sin(l), R


fig, ax = plt.subplots(figsize=(5.2, 5.2))
ax.set_aspect("equal"); ax.grid(False); ax.axis("off")
for R in np.arange(2, 16, 2):
    ax.add_patch(Circle((0, 0), R, fill=False, color=GRID, lw=.6))
ax.add_patch(Circle((0, 0), 15, color="#eef2f7", zorder=-2))
ax.add_patch(Circle((0, 0), R0, fill=False, color=MUTED, lw=1, ls="--"))
l = 35
Rt = R0*np.sin(np.radians(l))
ax.add_patch(Circle((0, 0), Rt, fill=False, color=RUST, lw=1, ls=":"))
sun = np.array([0, -R0])
dirv = np.array([np.sin(np.radians(l)), np.cos(np.radians(l))])
d = np.linspace(0, 21, 400)
pts = sun[None, :] + d[:, None]*dirv[None, :]
inside = np.hypot(*pts.T) < 15
vr, _ = vr_along(l, d)
sc = ax.scatter(pts[inside, 0], pts[inside, 1], c=vr[inside], cmap="RdBu_r", vmin=-100, vmax=100, s=7, zorder=3)
dt = R0*np.cos(np.radians(l)); tp = sun + dt*dirv
ax.plot(*tp, "o", color=RUST, ms=6, zorder=4)
ax.annotate("punto tangente\nR = 4,7 kpc\nv máx ≈ +94 km/s", tp, (tp[0]+2.2, tp[1]-6.5), fontsize=8,
            color=RUST, arrowprops=dict(arrowstyle="-", color=RUST, lw=.8))
ax.plot(0, 0, "*", color=INK, ms=11); ax.text(.4, .3, "centro", fontsize=8)
ax.plot(*sun, "o", color="#e3a21a", ms=9, mec=INK, zorder=5); ax.text(sun[0]-.6, sun[1]-1.1, "Sol", fontsize=8.5, ha="center")
ax.text(-7.2, -7.6, "círculo solar\nR₀ = 8,2 kpc", fontsize=7.5, color=MUTED, ha="center")
ax.annotate("rotación", (-11.5, 4.5), (-12.6, 1.0), fontsize=8, color=MUTED,
            arrowprops=dict(arrowstyle="->", color=MUTED, connectionstyle="arc3,rad=-.25"))
ax.text(9.2, 10.2, "l = 35°", fontsize=9, color=INK)
ax.set_xlim(-15.5, 15.5); ax.set_ylim(-15.5, 15.5)
cb = fig.colorbar(sc, ax=ax, shrink=.55, pad=.02); cb.set_label("velocidad vista desde el Sol (km/s)")
cb.outline.set_visible(False)
save(fig, "f4_topview")


# ---------- F4.2 velocity vs distance along l = 20 and 35 ----------
fig, ax = plt.subplots(figsize=(6.4, 2.8))
d = np.linspace(0, 20, 500)
for l, col in ((20, BLUE), (35, RUST)):
    vr, R = vr_along(l, d)
    ax.plot(d, vr, color=col, label=f"l = {l}°")
    i = np.argmax(vr); ax.plot(d[i], vr[i], "o", color=col)
    ax.text(d[i], vr[i]+9, f"tangente: +{vr[i]:.0f}", fontsize=8, ha="center", color=col)
ax.axhline(0, color=MUTED, lw=.8)
ax.annotate("cercano y lejano dan\nla misma velocidad", (2.6, 60), (0.3, 118), fontsize=7.5, color=MUTED,
            arrowprops=dict(arrowstyle="-", color=MUTED, lw=.7))
ax.text(17, -48, "disco exterior:\nse acerca (v < 0)", fontsize=7.5, color=MUTED, ha="center")
ax.set_xlabel("distancia desde el Sol (kpc)"); ax.set_ylabel("velocidad (km/s)")
ax.set_ylim(-110, 175); ax.legend(frameon=False, loc="upper right")
save(fig, "f4_vd")


# ---------- F4.3 flat rotation curve vs Kepler ----------
fig, ax = plt.subplots(figsize=(6.4, 2.6))
R = np.linspace(1, 16, 300)
ax.plot(R, np.full_like(R, V0), color=BLUE, label="lo que se mide: plana")
ax.plot(R, V0*np.sqrt(4/R), color=MUTED, ls="--", label="si toda la masa estuviera en el centro (Kepler)")
ax.axvspan(1.1, 2.5, color=GREEN, alpha=.18, lw=0)
ax.text(1.8, 120, "nuestro método\ncon HI4PI:\n211–223 km/s", fontsize=7.5, ha="center", color=GREEN)
ax.axvline(R0, color=MUTED, lw=.8, ls=":"); ax.text(R0+.2, 300, "Sol", fontsize=8, color=MUTED)
ax.set_ylim(0, 460); ax.set_xlabel("radio galactocéntrico R (kpc)"); ax.set_ylabel("V (km/s)")
ax.legend(frameon=False, fontsize=8, loc="upper right")
save(fig, "f4_curve")


# ---------- F5.1 beam sizes ----------
fig, ax = plt.subplots(figsize=(6.4, 3.0))
ax.set_aspect("equal"); ax.grid(False)
ax.add_patch(Ellipse((0, 0), 16.1, 24.2, color=BLUE, alpha=.18))
ax.add_patch(Ellipse((0, 0), 16.1, 24.2, fill=False, color=BLUE, lw=1.5))
ax.text(0, -14.2, "haz de ALMITA ≈ 16° × 24°", ha="center", fontsize=8.5, color=BLUE)
ax.add_patch(Circle((17, 0), .25, color=INK)); ax.text(17, -2.2, "Luna\n0,5°", ha="center", fontsize=8, va="top")
ax.add_patch(Circle((22, 0), .135, color=RUST)); ax.text(22, -2.2, "haz de\nHI4PI\n0,27°", ha="center", fontsize=8, va="top", color=RUST)
ax.add_patch(Ellipse((-19, 0), 10.5, 9.1, fill=False, color=MUTED, lw=1, ls="--"))
ax.text(-19, -6.6, "Nube Chica\nde Magallanes\n(HI, aprox.)", ha="center", fontsize=7.5, va="top", color=MUTED)
ax.set_xlim(-27, 26); ax.set_ylim(-17, 14); ax.axis("off")
save(fig, "f5_beam")


# ---------- F5.2 where the kelvins of Tsys come from ----------
fig, ax = plt.subplots(figsize=(6.4, 1.75))
parts = [("receptor (LNA y lo que sigue)", 59, MUTED), ("antena: suelo, pérdidas, cielo", 63, BLUE)]
x = 0
for lab, val, col in parts:
    ax.barh(0, val, left=x, color=col, height=.5, edgecolor="white", lw=2)
    ax.text(x + val/2, 0, f"{lab}\n≈ {val} K", ha="center", va="center", fontsize=8, color="white")
    x += val
ax.barh(0, 20, left=x, color=RUST, height=.5, edgecolor="white", lw=2)
ax.text(x + 10, .45, "+ la línea HI en el plano\n≈ 15–18 % ≈ 20 K", ha="center", fontsize=7.5, color=RUST, va="bottom")
ax.set_yticks([]); ax.set_xlim(0, 150); ax.set_ylim(-.4, 1.2); ax.set_xlabel("kelvin"); ax.grid(axis="y", visible=False)
ax.spines["left"].set_visible(False)
save(fig, "f5_tsys")


# ---------- F5.3 radiometer equation ----------
fig, ax = plt.subplots(figsize=(6.4, 3.0))
t = np.logspace(-2, 3, 200)
for B, col, lab in ((2.4e6, MUTED, "toda la banda, 2,4 MHz"), (9375, BLUE, "un canal de 9,4 kHz (256 canales)")):
    ax.loglog(t, 100/np.sqrt(B*t), color=col, label=lab)
ax.plot(1, 6.98e-2, "o", color=RUST, ms=7, zorder=5)
ax.annotate("medido 17-10, carga de 50 Ω:\n0,070 % a 1 s (ideal 0,065 %)", (1, 6.98e-2), (2.3, .006),
            fontsize=7.5, color=RUST, arrowprops=dict(arrowstyle="-", color=RUST, lw=.8))
ax.axvspan(8, 1e3, color=RUST, alpha=.07, lw=0)
ax.text(60, 4, "sobre ~8 s manda la\nderiva de ganancia:\nla curva deja de bajar", fontsize=7.5, color=RUST, ha="center")
ax.axhline(15, color=GREEN, lw=1, ls="--"); ax.text(.012, 17, "la línea en el plano ≈ 15 %", fontsize=7.5, color=GREEN)
ax.axhline(1, color=GREEN, lw=1, ls=":"); ax.text(.012, 1.12, "la Nube Chica ≈ 1 %", fontsize=7.5, color=GREEN)
ax.set_xlabel("tiempo integrado τ (s)"); ax.set_ylabel("ruido σ / Tsys (%)")
ax.set_ylim(3e-3, 60); ax.legend(frameon=False, fontsize=8, loc="lower left")
save(fig, "f5_radiometer")
print("figures:", sorted(p.name for p in OUT.glob("*.svg")))
