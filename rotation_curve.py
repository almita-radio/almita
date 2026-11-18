"""BL-105 (operator 2026-11-10: the rotation curve "me interesa harto"): the Milky Way's rotation curve from ALMITA's
own spectra by the tangent-point method, and the longitude-velocity diagram.

    python rotation_curve.py data/reduced/<campaign>/<REDUCE session> [--out DIR] [--edge-fraction 0.03] [--bmax 10]

Method (plain words in docs/BACKLOG.md BL-105):
  - every point near the plane (|b| <= bmax) in the first (0 < l < 90) or fourth (270 < l < 360) quadrant;
  - its REDUCE spectrum (LSRK), smoothed to ~4 km/s; noise from the line-free end of the terminal side;
  - the TERMINAL velocity: going inward from the terminal end of the spectrum (positive in QI, negative in QIV), the
    first velocity where the line reaches max(nsigma x noise, edge_fraction x peak);
  - tangent point: R = R0 |sin l|; rotation speed V(R) = |v_t| + V0 |sin l| (R0 = 8.2 kpc, V0 = 220 km/s).
  - edge_fraction 0.03 (2026-11-11, measured on HI4PI): through a 20 deg beam the tangent-point gas is only ~2-5 % of
    the spectrum's peak (the local gas dominates it); 0.2 would stop on the local gas's shoulder.
  - The SAME edge method is applied, where an HI4PI cube tile covers the beam, to HI4PI's spectrum seen through
    ALMITA's Gaussian beam (alignment of method, not of units: the edge is a fraction of each spectrum's own peak).
    That comparison is what says how much the 20 deg beam itself shifts the edge.

Outputs: rotation_curve.json (every point, its numbers and why a point was left out), rotation_curve.png,
lv_diagram.png. Relative / instrumental: a measured edge, never a fitted model; no point is invented or filled.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

R0_KPC, V0_KM_S = 8.2, 220.0
SMOOTH_KM_S = 4.0
NOISE_SPAN_KM_S = 25.0
HI4PI_DIR = Path(__file__).resolve().parent / "data" / "reference" / "hi" / "hi4pi"


def galactic(ra_hours: float, dec_deg: float):
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    g = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg, frame="icrs").galactic
    return float(g.l.deg), float(g.b.deg)


def quadrant_sign(l_deg: float) -> int:
    l = l_deg % 360.0
    return 1 if 0.0 < l < 90.0 else (-1 if 270.0 < l < 360.0 else 0)


def smooth(v: np.ndarray, t: np.ndarray, width_km_s: float = SMOOTH_KM_S) -> np.ndarray:
    dv = float(np.median(np.abs(np.diff(v)))) or 1.0
    k = max(1, int(round(width_km_s / dv)))
    good = np.isfinite(t)
    num = np.convolve(np.where(good, t, 0.0), np.ones(k), "same")
    den = np.convolve(good.astype(float), np.ones(k), "same")
    out = num / np.where(den > 0, den, np.nan)
    out[~good] = np.nan
    return out


EDGE_FRACTION = 0.03                  # HI4PI through a 20 deg beam: the tangent gas is only ~2-5 % of the spectrum's peak


def terminal_velocity(v_km_s: np.ndarray, t: np.ndarray, sign: int, *, edge_fraction: float = EDGE_FRACTION,
                      nsigma: float = 4.0) -> Dict[str, Any]:
    """The terminal edge of one spectrum (see the module docstring). `sign` +1 = edge on the positive side."""
    order = np.argsort(v_km_s)
    v, raw = v_km_s[order], np.asarray(t, float)[order]
    ts = smooth(v, raw)
    ok = np.isfinite(ts) & np.isfinite(raw)
    if ok.sum() < 20:
        return {"ok": False, "reason": "fewer than 20 usable channels"}
    v, ts, raw = v[ok], ts[ok], raw[ok]
    if sign < 0:                                       # work as if the edge were always on the positive side
        v, ts, raw = -v[::-1], ts[::-1], raw[::-1]
    end = v.max()
    tail = v > end - NOISE_SPAN_KM_S
    # noise of the SMOOTHED spectrum from channel-to-channel differences of the raw one (blind to a slow step),
    # divided by sqrt(channels averaged)
    dv = float(np.median(np.abs(np.diff(v)))) or 1.0
    k = max(1, int(round(SMOOTH_KM_S / dv)))
    d = np.diff(raw[tail])
    noise = (1.4826 * float(np.median(np.abs(d - np.median(d)))) / math.sqrt(2) / math.sqrt(k)) if tail.sum() > 5 else float("nan")
    base = float(np.median(ts[tail])) if tail.sum() > 5 else 0.0
    peak = float(np.nanmax(ts) - base)
    if not (peak > 0 and math.isfinite(noise)):
        return {"ok": False, "reason": "no line above the end of the spectrum"}
    thr = max(nsigma * noise, edge_fraction * peak)
    # the zero is taken from the last 25 km/s: they must be line-free, i.e. flat - a step there as large as half the
    # edge threshold means the line is still going down when the window ends
    tv, tt = v[tail], ts[tail]
    half = tv < np.median(tv)
    if half.sum() > 2 and (~half).sum() > 2 and abs(float(np.median(tt[half]) - np.median(tt[~half]))) > 0.5 * thr:
        return {"ok": False, "reason": "the line is still dropping in the last 25 km/s: the window ends before the line does"}
    above = np.where(ts - base >= thr)[0]
    if above.size == 0:
        return {"ok": False, "reason": "the line never reaches the edge threshold"}
    i = int(above.max())                                # outermost channel still at or above the threshold
    vt = float(v[i])
    if vt > end - NOISE_SPAN_KM_S:
        return {"ok": False, "reason": "edge inside the last 25 km/s: the window ends before the line does",
                "terminal_km_s": sign * vt}
    # uncertainty: where the line crosses threshold +/- noise
    hi = np.where(ts - base >= thr - noise)[0]
    lo = np.where(ts - base >= thr + noise)[0]
    err = 0.5 * abs((float(v[hi.max()]) if hi.size else vt) - (float(v[lo.max()]) if lo.size else vt))
    return {"ok": True, "terminal_km_s": sign * vt, "terminal_err_km_s": max(err, SMOOTH_KM_S / 2),
            "peak": peak, "noise": noise, "threshold": thr, "snr_peak": peak / noise if noise > 0 else None}


def tangent_point(l_deg: float, v_t_km_s: float, err_km_s: float = 0.0) -> Dict[str, float]:
    s = abs(math.sin(math.radians(l_deg)))
    return {"R_kpc": R0_KPC * s, "V_km_s": abs(v_t_km_s) + V0_KM_S * s, "V_err_km_s": err_km_s}


# ------------------------------------------------------------------ HI4PI through ALMITA's beam
class Hi4piTiles:
    """HI4PI CAR tiles in galactic coordinates (GLON/GLAT x VRAD, K), opened lazily."""

    def __init__(self, directory: Path = HI4PI_DIR):
        self.tiles = []
        for path in sorted(glob.glob(str(directory / "CAR_*.fits"))):
            try:
                from astropy.io import fits
                h = fits.getheader(path)
                if h.get("CTYPE1", "").startswith("GLON") and h.get("CTYPE3", "").startswith("VRAD"):
                    self.tiles.append((path, h))
            except Exception:  # noqa: BLE001 - an unreadable tile is simply not used
                continue

    @staticmethod
    def _axes(h):
        n1, n2, n3 = h["NAXIS1"], h["NAXIS2"], h["NAXIS3"]
        l = h["CRVAL1"] + (np.arange(1, n1 + 1) - h["CRPIX1"]) * h["CDELT1"]
        b = h["CRVAL2"] + (np.arange(1, n2 + 1) - h["CRPIX2"]) * h["CDELT2"]
        v = (h["CRVAL3"] + (np.arange(1, n3 + 1) - h["CRPIX3"]) * h["CDELT3"]) / 1000.0
        return l, b, v

    def beam_spectrum(self, l0: float, b0: float, fwhm_deg: float = 20.0, min_cover: float = 0.9):
        """(v km/s, T K) of HI4PI averaged in a Gaussian beam, from the one tile that holds >= min_cover of its weight;
        None when no tile does (never a partial beam passed off as a full one)."""
        from astropy.io import fits
        sig = fwhm_deg / 2.3548
        for path, h in self.tiles:
            l, b, v = self._axes(h)
            dl = ((l - l0 + 180.0) % 360.0) - 180.0
            DL, B = np.meshgrid(dl, b)
            d2 = (DL * np.cos(np.radians(B))) ** 2 + (B - b0) ** 2
            w = np.exp(-0.5 * d2 / sig ** 2)
            # the share of the full beam's weight this tile holds (pixel solid angle ~ cos b)
            full = 2 * math.pi * (math.radians(sig)) ** 2
            pix = math.radians(abs(h["CDELT1"])) * math.radians(abs(h["CDELT2"]))
            cover = float((w * np.cos(np.radians(B))).sum() * pix / full)
            if cover < min_cover:
                continue
            sel = w > 1e-3 * w.max()
            jj, ii = np.nonzero(sel)
            with fits.open(path, memmap=True) as f:
                data = f[0].data
                cube = np.asarray(data[:, jj.min():jj.max() + 1, ii.min():ii.max() + 1], dtype=float)
            ww = (w * np.cos(np.radians(B)))[jj.min():jj.max() + 1, ii.min():ii.max() + 1]
            spec = np.nansum(cube * ww[None, :, :], axis=(1, 2)) / np.nansum(ww)
            return v, spec, cover
        return None


# ------------------------------------------------------------------ campaign
def load_points(reduce_dir: Path) -> List[Dict[str, Any]]:
    import h5py
    pts = []
    for js in sorted((reduce_dir / "points").glob("*/master_spectrum.json"), key=lambda p: int(p.parent.name)):
        d = json.loads(js.read_text())
        with h5py.File(js.with_suffix(".h5")) as f:
            v = f["velocity_lsrk_m_s"][:] / 1000.0
            t = f["relative_intensity"][:].astype(float)
            m = f["mask"][:]
        t[m != 0] = np.nan
        pts.append({"point_index": d["point_index"], "ra_hours": d["ra_hours"], "dec_degrees": d["dec_degrees"],
                    "v": v, "t": t, "quality": (d.get("quality") or {}).get("state")})
    return pts


def analyse(reduce_dir: Path, *, bmax: float = 10.0, edge_fraction: float = EDGE_FRACTION, nsigma: float = 4.0,
            beam_fwhm_deg: float = 20.0, hi4pi: Optional[Hi4piTiles] = None) -> Dict[str, Any]:
    hi4pi = hi4pi if hi4pi is not None else Hi4piTiles()
    rows = []
    for p in load_points(reduce_dir):
        l, b = galactic(p["ra_hours"], p["dec_degrees"])
        row: Dict[str, Any] = {"point_index": p["point_index"], "l_deg": round(l, 2), "b_deg": round(b, 2)}
        sign = quadrant_sign(l)
        if abs(b) > bmax or sign == 0:
            row.update(used=False, reason=f"not a tangent-point direction (|b| > {bmax} or outside quadrants I/IV)")
            rows.append(row); continue
        e = terminal_velocity(p["v"], p["t"], sign, edge_fraction=edge_fraction, nsigma=nsigma)
        if not e["ok"]:
            row.update(used=False, reason=e["reason"], **{k: v for k, v in e.items() if k not in ("ok", "reason")})
            rows.append(row); continue
        row.update(used=True, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in e.items() if k != "ok"})
        row.update({k: round(v, 3) for k, v in tangent_point(l, e["terminal_km_s"], e["terminal_err_km_s"]).items()})
        # HI4PI through OUR beam (what an antenna like ALMITA should see) and through a narrow 2 deg one (close to
        # the true curve): the gap between the two is the beam's own effect on the edge
        for key, fwhm in (("hi4pi", beam_fwhm_deg), ("hi4pi_narrow", 2.0)):
            ref = hi4pi.beam_spectrum(l, b, fwhm) if hi4pi.tiles else None
            if ref is not None:
                hv, hs, cover = ref
                he = terminal_velocity(hv, hs, sign, edge_fraction=edge_fraction, nsigma=nsigma)
                if he["ok"]:
                    row[key] = {"terminal_km_s": round(he["terminal_km_s"], 2), "beam_cover": round(cover, 3),
                                "beam_fwhm_deg": fwhm,
                                **{k: round(v, 3) for k, v in tangent_point(l, he["terminal_km_s"]).items()}}
        rows.append(row)
    used = [r for r in rows if r.get("used")]
    return {"schema": "almita.rotation_curve.v1", "reduce_session": str(reduce_dir),
            "method": {"R0_kpc": R0_KPC, "V0_km_s": V0_KM_S, "smoothing_km_s": SMOOTH_KM_S, "edge_fraction": edge_fraction,
                       "nsigma": nsigma, "bmax_deg": bmax, "beam_fwhm_deg": beam_fwhm_deg,
                       "hi4pi_tiles": [Path(t[0]).name for t in hi4pi.tiles]},
            "points": rows, "used": len(used),
            "median_V_km_s": round(float(np.median([r["V_km_s"] for r in used])), 1) if used else None,
            "note": "relative/instrumental spectra; the edge is a fraction of each spectrum's own peak - no Kelvin needed"}


def plots(result: Dict[str, Any], reduce_dir: Path, out: Path) -> List[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    files = []
    used = [r for r in result["points"] if r.get("used")]
    fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
    ax.axhline(V0_KM_S, color="#9a9a9a", lw=1, ls="--", label=f"flat curve, {V0_KM_S:.0f} km/s")
    if used:
        ax.errorbar([r["R_kpc"] for r in used], [r["V_km_s"] for r in used], yerr=[r["V_err_km_s"] for r in used],
                    fmt="o", color="#2a6fdb", ms=6, label="ALMITA (tangent points)")
    ref = [r for r in used if "hi4pi" in r]
    if ref:
        ax.plot([r["hi4pi"]["R_kpc"] for r in ref], [r["hi4pi"]["V_km_s"] for r in ref], "s", mfc="none",
                color="#333333", ms=7, label="HI4PI through the same 20° beam, same method")
    nar = [r for r in used if "hi4pi_narrow" in r]
    if nar:
        ax.plot([r["hi4pi_narrow"]["R_kpc"] for r in nar], [r["hi4pi_narrow"]["V_km_s"] for r in nar], "^", mfc="none",
                color="#8a5a00", ms=7, label="HI4PI through a 2° beam (close to the true curve)")
    ax.set_xlabel("galactocentric radius R [kpc]"); ax.set_ylabel("rotation speed V [km/s]")
    ax.set_xlim(0, R0_KPC + 0.5); ax.set_ylim(0, 320); ax.grid(color="#e3e3e3", lw=0.6)
    ax.legend(loc="lower right", frameon=False)
    ax.set_title(f"ALMITA · Milky Way rotation curve (tangent points) · {len(used)} points", loc="left")
    p = out / "rotation_curve.png"; fig.savefig(p, dpi=110); plt.close(fig); files.append(str(p))
    # longitude-velocity diagram of every |b| <= bmax point
    pts = load_points(reduce_dir)
    lv = []
    for q in pts:
        l, b = galactic(q["ra_hours"], q["dec_degrees"])
        if abs(b) <= result["method"]["bmax_deg"]:
            lv.append((((l + 180) % 360) - 180, q["v"], smooth(q["v"], q["t"])))
    if lv:
        fig, ax = plt.subplots(figsize=(9, 5), constrained_layout=True)
        lv.sort(key=lambda x: x[0])
        vmax = np.nanpercentile(np.concatenate([x[2] for x in lv]), 99)
        for i, (l, v, t) in enumerate(lv):
            half = 0.5 * (abs(lv[min(i + 1, len(lv) - 1)][0] - l) or 2.0)
            ax.scatter(np.full(v.size, l), v, c=t, s=4, marker="s", cmap="viridis", vmin=0, vmax=vmax, linewidths=0)
        for r in used:
            ax.plot(((r["l_deg"] + 180) % 360) - 180, r["terminal_km_s"], "x", color="#e8590c", ms=8, mew=2)
        ax.invert_xaxis(); ax.set_xlabel("galactic longitude l [deg]"); ax.set_ylabel("LSRK velocity [km/s]")
        ax.set_title("ALMITA · longitude-velocity diagram (|b| small) · × = measured terminal edge", loc="left")
        p = out / "lv_diagram.png"; fig.savefig(p, dpi=110); plt.close(fig); files.append(str(p))
    return files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reduce_session")
    ap.add_argument("--out", default=None, help="output folder (default: <reduce session>/rotation_curve)")
    ap.add_argument("--bmax", type=float, default=10.0)
    ap.add_argument("--edge-fraction", type=float, default=EDGE_FRACTION)
    ap.add_argument("--nsigma", type=float, default=4.0)
    ap.add_argument("--beam-fwhm", type=float, default=20.0)
    a = ap.parse_args(argv)
    rd = Path(a.reduce_session)
    out = Path(a.out) if a.out else rd / "rotation_curve"
    out.mkdir(parents=True, exist_ok=True)
    res = analyse(rd, bmax=a.bmax, edge_fraction=a.edge_fraction, nsigma=a.nsigma, beam_fwhm_deg=a.beam_fwhm)
    res["files"] = plots(res, rd, out)
    (out / "rotation_curve.json").write_text(json.dumps(res, indent=2, default=float))
    print(f"rotation curve: {res['used']} tangent points used of {len(res['points'])}; median V = {res['median_V_km_s']} km/s")
    for r in res["points"]:
        if r.get("used"):
            extra = f"  HI4PI {r['hi4pi']['terminal_km_s']:+.0f}" if "hi4pi" in r else ""
            print(f"  l={r['l_deg']:6.1f} b={r['b_deg']:+5.1f}  v_t={r['terminal_km_s']:+6.1f}±{r['terminal_err_km_s']:.0f}  "
                  f"R={r['R_kpc']:.2f} kpc  V={r['V_km_s']:.0f} km/s{extra}")
        else:
            print(f"  l={r['l_deg']:6.1f} b={r['b_deg']:+5.1f}  left out: {r['reason']}")
    print("files:", ", ".join(res["files"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
