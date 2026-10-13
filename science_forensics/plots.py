"""Diagnostic figures (PNG is never canonical). Labels state frame and unit; relative units only, no physical-unit claim."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from reduce_engine.models import MaskFlag  # noqa: E402

REL = "relative_intensity (dimensionless)"


def _ok_rows(track):
    return [r for r in track if r["status"] == "OK" and np.isfinite(r["centroid_channel"])]


def _fit_line(x, y):
    if len(x) < 3 or np.std(x) == 0:
        return None
    b, a = np.polyfit(x, y, 1)
    return a, b


def plot_centroid_vs(track, x_key: str, xlabel: str, title: str, path: Path):
    rows = _ok_rows(track)
    x = np.array([r[x_key] for r in rows], dtype=float)
    fig, axes = plt.subplots(2, 1, figsize=(8, 7), sharex=True)
    for ax, key, ekey, scale, ylabel in (
            (axes[0], "centroid_lsrk_m_s", "centroid_lsrk_sigma_m_s", 1e-3, "feature centroid, LSRK velocity (km/s)"),
            (axes[1], "centroid_freq_hz", "centroid_freq_sigma_hz", 1e-3, "feature centroid, topocentric frequency (kHz)")):
        y = np.array([r[key] for r in rows], dtype=float) * scale
        e = np.array([r[ekey] if r[ekey] is not None else np.nan for r in rows], dtype=float) * scale
        ax.errorbar(x, y, yerr=np.where(np.isfinite(e), e, 0), fmt="o", ms=4, capsize=2)
        fit = _fit_line(x, y)
        if fit:
            xx = np.linspace(x.min(), x.max(), 50)
            ax.plot(xx, fit[0] + fit[1] * xx, "--", lw=1, label=f"linear fit: {fit[1]:.4g} per unit of x")
            ax.legend(fontsize=8)
        ax.set_ylabel(ylabel)
        ax.grid(alpha=.3)
    axes[1].set_xlabel(xlabel)
    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_sky(track, path: Path, title: str):
    rows = _ok_rows(track)
    ra = np.array([r["ra_deg"] for r in rows]); dec = np.array([r["dec_deg"] for r in rows])
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6))
    for ax, key, label, scale in ((axes[0], "centroid_lsrk_m_s", "LSRK centroid (km/s)", 1e-3),
                                  (axes[1], "centroid_freq_hz", "topocentric frequency centroid (kHz)", 1e-3),
                                  (axes[2], "t_minutes", "capture time (min since first point)", 1.0)):
        c = np.array([r[key] for r in rows], dtype=float) * scale
        ax.plot(ra, dec, "-", color="0.75", lw=0.8, zorder=1)                 # scan path in capture order
        sc = ax.scatter(ra, dec, c=c, s=60, cmap="viridis", zorder=2)
        for r in rows:
            ax.annotate(str(r["point_index"]), (r["ra_deg"], r["dec_deg"]), fontsize=7, xytext=(3, 3), textcoords="offset points")
        ax.set_xlabel("RA (deg)"); ax.set_ylabel("Dec (deg)")
        ax.set_title(label, fontsize=9)
        ax.invert_xaxis()
        fig.colorbar(sc, ax=ax)
    fig.suptitle(title + "  (grey line = capture order; numbers = point index)", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def _sort_key(mode):
    return {"capture": lambda p: p.point_index, "ra": lambda p: p.ra_deg, "dec": lambda p: p.dec_deg,
            "galactic_l": lambda p: p.l_deg}[mode]


def plot_waterfall(points, track, config, frame: str, sort_mode: str, path: Path, span_factor: float = 3.0):
    """Point-order x spectral-axis image of Level 1 relative intensity (NOT a RAW waterfall). Rows use each point's own axis."""
    order = sorted(range(len(points)), key=lambda i: _sort_key(sort_mode)(points[i]))
    trk = {r["point_index"]: r for r in track}
    centre, half = trk_center(track, config)
    lo_v, hi_v = centre - span_factor * half, centre + span_factor * half
    ch_lo = min(int(np.flatnonzero(points[i].velocity_lsrk_m_s >= lo_v)[-1]) if np.any(points[i].velocity_lsrk_m_s >= lo_v) else 0
                for i in range(len(points)))
    ch_hi = max(int(np.flatnonzero(points[i].velocity_lsrk_m_s <= hi_v)[0]) if np.any(points[i].velocity_lsrk_m_s <= hi_v) else 0
                for i in range(len(points)))
    c0, c1 = sorted((ch_lo, ch_hi))
    cols = np.arange(c0, c1 + 1)
    Z = np.full((len(order), cols.size), np.nan)
    X = np.zeros_like(Z)
    for row, i in enumerate(order):
        p = points[i]
        good = (p.mask[cols] == MaskFlag.GOOD.value) & np.isfinite(p.value[cols])
        Z[row] = np.where(good, p.value[cols], np.nan)
        X[row] = (p.velocity_lsrk_m_s[cols] * 1e-3) if frame == "lsrk" else (p.frequency_hz[cols] * 1e-3)
    Y = np.repeat(np.arange(len(order))[:, None], cols.size, axis=1)
    finite = Z[np.isfinite(Z)]
    vmin, vmax = (np.percentile(finite, 2), np.percentile(finite, 98)) if finite.size else (-1, 1)
    fig, ax = plt.subplots(figsize=(9, 0.32 * len(order) + 3))
    cmap = plt.get_cmap("viridis").with_extremes(bad="0.85")
    mesh = ax.pcolormesh(X, Y, np.ma.masked_invalid(Z), shading="nearest", cmap=cmap, vmin=vmin, vmax=vmax)
    for row, i in enumerate(order):
        r = trk.get(points[i].point_index)
        if r and r["status"] == "OK" and np.isfinite(r["centroid_channel"]):
            ax.plot(r["centroid_lsrk_m_s"] * 1e-3 if frame == "lsrk" else r["centroid_freq_hz"] * 1e-3, row, "w.", ms=6, mec="k", mew=.5)
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels([str(points[i].point_index) for i in order], fontsize=6)
    ax.set_ylabel(f"point index (rows sorted by {sort_mode})")
    ax.set_xlabel("LSRK velocity (km/s), each row on its own axis" if frame == "lsrk" else "topocentric frequency (kHz)")
    ax.set_title(f"Level 1 point spectra, {sort_mode} order, {'LSRK velocity' if frame == 'lsrk' else 'topocentric frequency'} "
                 f"(white dots = measured centroid; grey = masked)", fontsize=8)
    fig.colorbar(mesh, ax=ax, label=REL)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def trk_center(track, config):
    """Display centre/half-width in LSRK m/s for waterfalls: the measured campaign median centroid if available."""
    rows = _ok_rows(track)
    if config.window_frame == "lsrk_velocity":
        return config.window_center, config.window_half_width
    if rows:
        return float(np.median([r["centroid_lsrk_m_s"] for r in rows])), max(3000.0, 2 * float(np.ptp([r["centroid_lsrk_m_s"] for r in rows])))
    return 0.0, 10000.0


def plot_negative_map(audit, cum, path: Path, title: str):
    fig, axes = plt.subplots(1, 3, figsize=(17, 4.8))
    # (a) mean relative intensity per point in the named windows
    ax = axes[0]
    pts = audit["per_point"]
    x = [p["point_index"] for p in pts]
    for name, marker in (("candidate", "o"), ("control_left", "s"), ("control_right", "^"), ("integration_window", "x")):
        y = [p.get(f"{name}_mean_value") for p in pts]
        ax.plot(x, [np.nan if v is None else v for v in y], marker, label=name, ms=5)
    ax.axhline(0, color="k", lw=.6)
    ax.set_xlabel("point index"); ax.set_ylabel("mean " + REL); ax.legend(fontsize=8); ax.grid(alpha=.3)
    ax.set_title("per-point mean value in each window", fontsize=9)
    # (b) interval decomposition
    ax = axes[1]
    ints = [r for r in audit["interval_decomposition"]["intervals"] if r.get("status") == "OK"]
    centres = [0.5 * (r["lsrk_range_m_s"][0] + r["lsrk_range_m_s"][1]) * 1e-3 for r in ints]
    med = np.array([r["median_mean_relative_intensity"] for r in ints]); p16 = np.array([r["p16"] for r in ints]); p84 = np.array([r["p84"] for r in ints])
    width = (ints[0]["lsrk_range_m_s"][1] - ints[0]["lsrk_range_m_s"][0]) * 1e-3 * 0.85 if ints else 1.0
    ax.bar(centres, med, width=width, yerr=[med - p16, p84 - med], capsize=2, color=["tab:red" if m < 0 else "tab:blue" for m in med])
    ax.axhline(0, color="k", lw=.6)
    ax.set_xlabel("LSRK velocity (km/s)"); ax.set_ylabel("median across points of mean " + REL)
    ax.set_title("equal-velocity intervals of the integration window (bars: p16-p84)", fontsize=9); ax.grid(alpha=.3)
    # (c) cumulative integral
    ax = axes[2]
    g = cum["grid_lsrk_m_s"] * 1e-3
    ax.fill_between(g, cum["p16"], cum["p84"], alpha=.25, label="p16-p84 across points")
    ax.plot(g, cum["median"], "k", label="campaign median")
    for k, v in cum["representative"].items():
        ax.plot(g, v, lw=.8, label=f"point {k}")
    ax.axhline(0, color="k", lw=.6)
    ax.set_xlabel("LSRK velocity (km/s)"); ax.set_ylabel("cumulative integral (relative_intensity * m/s)")
    ax.legend(fontsize=7); ax.grid(alpha=.3); ax.set_title("cumulative integral along velocity", fontsize=9)
    fig.suptitle(title, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_median_spectrum(ms, audit, path: Path, title: str):
    if ms.get("status") != "OK":
        return None
    fig, ax = plt.subplots(figsize=(11, 4.5))
    v = ms["mean_lsrk_m_s"] * 1e-3
    order = np.argsort(v)
    ax.fill_between(v[order], (ms["median"] - ms["robust_spread"])[order], (ms["median"] + ms["robust_spread"])[order], alpha=.25,
                    label="median +/- robust spread across points")
    ax.plot(v[order], ms["median"][order], lw=.7, label="median across points")
    for name, col in (("candidate", "tab:red"), ("control_left", "tab:green"), ("control_right", "tab:green")):
        lo, hi = audit["windows"][name]["lsrk_range_m_s"]
        ax.axvspan(lo * 1e-3, hi * 1e-3, color=col, alpha=.12)
    imin, imax = audit["integration_window_m_s"]
    ax.axvline(imin * 1e-3, color="k", ls=":"); ax.axvline(imax * 1e-3, color="k", ls=":")
    ax.axhline(0, color="k", lw=.6)
    ax.set_xlabel("mean LSRK velocity across points (km/s)"); ax.set_ylabel(REL)
    ax.legend(fontsize=8); ax.grid(alpha=.3)
    ax.set_title(title + " (red: candidate; green: control windows; dotted: integration window)", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
