"""Audit 2026-11-25 (operator "dale nomas"): the EMPIRICAL noise of integrals - how much the integral of a REDUCE
spectrum over a window with no line scatters from pointing to pointing - against the sigma REDUCE reports for it.

    python linefree_noise.py data/reduced/<campaign>/<REDUCE session> --windows=-80:-60,60:80 [--bmin 7] [--out DIR]

Why: REDUCE's per-bin sigma is the radiometer noise of each capture. It does not hold what changes from one pointing
to the next - the receiver's baseline wander, the switching ripple that depends on where the dish points (R1c) - and
that wander dominated every integral the audit checked (x4.7-7.3 between pointings, x1.4-1.9 inside one capture).
This tool measures it the direct way: the same line-free windows on every point, their scatter, and the ratio to
the reported sigma (with the Hann neighbouring-channel correlation, science_engine.integration). Quote that ratio,
or the scatter itself, next to any integral of the same campaign.

Windows are LSRK km/s and must be line-free for EVERY point used (--bmin keeps only |b| >= bmin; in the Galactic
plane only far negative velocities in the first quadrant are). Writes linefree_noise.json. Never changes REDUCE.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

SCHEMA = "almita.linefree_noise.v1"
MIN_CHANNELS = 50


def window_integral(v_km_s: np.ndarray, t: np.ndarray, sigma: np.ndarray, lo: float, hi: float,
                    rho: Optional[Sequence[float]] = None) -> Optional[Tuple[float, float, int]]:
    """(integral, reported sigma, channels) of one spectrum over [lo, hi] km/s; None when < MIN_CHANNELS usable.
    Reported sigma = the per-channel sigmas summed with the Hann cross terms (adjacent channels in frequency order)."""
    from science_engine.integration import correlated_sum_sigma
    v, t, sigma = np.asarray(v_km_s, float), np.asarray(t, float), np.asarray(sigma, float)
    m = np.isfinite(t) & np.isfinite(sigma) & (v >= lo) & (v <= hi)
    if m.sum() < MIN_CHANNELS:
        return None
    idx = np.flatnonzero(m)                                    # channel order = frequency order (REDUCE axis)
    dv = float(np.median(np.abs(np.diff(v[idx])))) or 1.0
    s = np.zeros(idx.max() - idx.min() + 1)
    s[idx - idx.min()] = sigma[idx]                            # masked channels: no contribution, gaps kept
    return float(np.sum(t[idx]) * dv), float(correlated_sum_sigma(s[:, None], rho)[0] * dv), int(m.sum())


def scatter(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Scatter of the window integrals around each WINDOW's own median (a constant offset per window is a
    systematic of its own, reported separately), against the median reported sigma."""
    if len(rows) < 3:
        return {"n": len(rows), "ok": False, "reason": "fewer than 3 window integrals"}
    by_w: Dict[str, List[float]] = {}
    for r in rows:
        by_w.setdefault(str(r["window"]), []).append(r["value"])
    resid = np.concatenate([np.asarray(x) - np.median(x) for x in by_w.values()])
    rep = float(np.median([r["sigma_reported"] for r in rows]))
    sd = float(np.std(resid, ddof=1)) if resid.size > 1 else float("nan")
    rob = float(1.4826 * np.median(np.abs(resid - np.median(resid))))
    return {"n": len(rows), "ok": True, "scatter": sd, "robust_scatter": rob, "sigma_reported_median": rep,
            "ratio": sd / rep if rep > 0 else None, "robust_ratio": rob / rep if rep > 0 else None,
            "window_offsets": {w: float(np.median(x)) for w, x in by_w.items()},
            "per_window_scatter": {w: float(np.std(x, ddof=1)) if len(x) > 1 else None for w, x in by_w.items()}}


def load_points(reduce_dir: Path) -> List[Dict[str, Any]]:
    import h5py
    pts = []
    for js in sorted((reduce_dir / "points").glob("*/master_spectrum.json"), key=lambda p: int(p.parent.name)):
        d = json.loads(js.read_text())
        with h5py.File(js.with_suffix(".h5")) as f:
            v = f["velocity_lsrk_m_s"][:] / 1000.0
            t = f["relative_intensity"][:].astype(float)
            u = f["uncertainty"][:].astype(float)
            m = f["mask"][:]
        t[m != 0] = np.nan
        pts.append({"point_index": d["point_index"], "ra_hours": d["ra_hours"], "dec_degrees": d["dec_degrees"],
                    "v": v, "t": t, "u": u})
    return pts


def analyse(points: Iterable[Dict[str, Any]], windows: Sequence[Tuple[float, float]], *, bmin: float = 0.0,
            galactic=None) -> Dict[str, Any]:
    rows, skipped = [], []
    for p in points:
        if bmin > 0:
            galactic = galactic or _galactic
            l, b = galactic(p["ra_hours"], p["dec_degrees"])
            if abs(b) < bmin:
                skipped.append({"point_index": p["point_index"], "reason": f"|b| = {abs(b):.1f} < {bmin}"})
                continue
        for w in windows:
            r = window_integral(p["v"], p["t"], p["u"], float(w[0]), float(w[1]))
            if r is None:
                skipped.append({"point_index": p["point_index"], "window": list(w), "reason": "too few usable channels"})
                continue
            rows.append({"point_index": p["point_index"], "window": [float(w[0]), float(w[1])], "value": r[0],
                         "sigma_reported": r[1], "channels": r[2]})
    return {"schema": SCHEMA, "windows_lsrk_km_s": [list(map(float, w)) for w in windows], "bmin_deg": bmin,
            "summary": scatter(rows), "rows": rows, "skipped": skipped,
            "note": ("scatter between pointings of integrals over windows assumed line-free; ratio = scatter / the "
                     "reported sigma (Hann cross terms included). It holds the baseline's wander, which REDUCE's "
                     "sigma does not.")}


def _galactic(ra_hours: float, dec_deg: float):
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    g = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg, frame="icrs").galactic
    return float(g.l.deg), float(g.b.deg)


def parse_windows(text: str) -> List[Tuple[float, float]]:
    out = []
    for part in text.split(","):
        a, b = part.split(":", 1)
        lo, hi = sorted((float(a), float(b)))
        out.append((lo, hi))
    if not out:
        raise ValueError("no windows")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reduce_session")
    ap.add_argument("--windows", required=True, help="LSRK km/s windows lo:hi,lo:hi (write --windows=-80:-60,60:80)")
    ap.add_argument("--bmin", type=float, default=0.0, help="use only points with |b| >= bmin (deg)")
    ap.add_argument("--out", default=None, help="output folder (default: the REDUCE session)")
    a = ap.parse_args(argv)
    rd = Path(a.reduce_session)
    res = analyse(load_points(rd), parse_windows(a.windows), bmin=a.bmin)
    res["reduce_session"] = str(rd)
    out = Path(a.out) if a.out else rd
    out.mkdir(parents=True, exist_ok=True)
    (out / "linefree_noise.json").write_text(json.dumps(res, indent=2, default=float))
    s = res["summary"]
    if s.get("ok"):
        print(f"line-free integrals: n={s['n']}  scatter={s['scatter']:.4g}  reported sigma={s['sigma_reported_median']:.4g}"
              f"  ratio={s['ratio']:.2f} (robust {s['robust_ratio']:.2f})")
    else:
        print("line-free integrals:", s.get("reason"))
    print("written", out / "linefree_noise.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
