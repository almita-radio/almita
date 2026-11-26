"""ALMITA vs HI4PI, WITH its null tests (audit 2026-11-25, operator "dale nomas"; from the audit's a03_hi4pi.py).

    python hi4pi_compare.py data/reduced/<campaign>/<REDUCE session> [--out DIR] [--min-cover 0.4] [--map-window=-60:60]

Why the nulls: on 12-11 the shape correlation of each ALMITA spectrum with HI4PI at ITS pointing was 0.95-0.99 - and
with HI4PI at ANOTHER pointing of the same grid 0.91-0.98. A high r alone shows hydrogen with the right velocity
sign; it shows a pointing-by-pointing match only if it beats what the wrong pointings give. Every r here is
reported next to the r that the same test gives when it should fail.

SPECTRA - every point whose 20 deg beam falls on an HI4PI galactic CAR tile (data/reference/hi/hi4pi/CAR_*.fits,
the tile holding most of the beam's weight; `cover` = the share of the full beam inside it, the beam is truncated
to it), LSRK, r over |v| <= 150 km/s, ALMITA smoothed to HI4PI's 1.29 km/s channel:
  r_same      HI4PI at the point's own position
  r_shuffled  HI4PI at the OTHER points of the grid (median and how many of them beat r_same)
  r_offset    HI4PI 3, 6, 10, 15, 20 deg away (8 directions each, cover >= 0.25)
  r_mirror    HI4PI with its velocity axis flipped (the velocity-sign test)
MAP - the integral over --map-window (LSRK) of every point against the HI4PI all-sky column-density map through the
full beam (hi4pi_expected), and the same r for the values permuted among the points, for the pointing pattern
rotated about its centre and for it shifted in RA. A map r is only position-specific if it beats those.

Comparison only: nothing here feeds REDUCE or SCIENCE. Writes hi4pi_compare.json. Heavy on memory for big tiles:
run it alone (one process, under a MemoryMax).
"""
from __future__ import annotations

import argparse
import glob
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

SCHEMA = "almita.hi4pi_compare.v1"
ROOT = Path(__file__).resolve().parent
TILES_DIR = ROOT / "data" / "reference" / "hi" / "hi4pi"
FWHM_DEG = 20.0
HI4PI_CHANNEL_KM_S = 1.29
V_ABS_MAX = 150.0
OFFSETS_DEG = (3, 6, 10, 15, 20)
SEED = 20261125


# ------------------------------------------------------------------ HI4PI galactic tiles (coarse, |v| <= 160)
class Tile:
    def __init__(self, path: Path, coarse: int = 6):
        from astropy.io import fits
        self.path = Path(path)
        with fits.open(path, memmap=True) as f:
            h = f[0].header
            n1, n2, n3 = h["NAXIS1"], h["NAXIS2"], h["NAXIS3"]
            l = h["CRVAL1"] + (np.arange(1, n1 + 1) - h["CRPIX1"]) * h["CDELT1"]
            b = h["CRVAL2"] + (np.arange(1, n2 + 1) - h["CRPIX2"]) * h["CDELT2"]
            v = (h["CRVAL3"] + (np.arange(1, n3 + 1) - h["CRPIX3"]) * h["CDELT3"]) / 1000.0
            sel = np.abs(v) <= V_ABS_MAX + 10
            fct = max(1, int(coarse))
            nb, nl = (n2 // fct) * fct, (n1 // fct) * fct
            sub = np.asarray(f[0].data[sel, :nb, :nl], dtype=np.float32)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            self.c = np.nanmean(sub.reshape(sub.shape[0], nb // fct, fct, nl // fct, fct), axis=(2, 4))
        self.l = l[:nl].reshape(-1, fct).mean(1)
        self.b = b[:nb].reshape(-1, fct).mean(1)
        self.v = v[sel]
        self.pix = math.radians(abs(h["CDELT1"]) * fct) * math.radians(abs(h["CDELT2"]) * fct)

    def beam(self, l0: float, b0: float, fwhm: float = FWHM_DEG):
        """(v, spectrum through the beam truncated to this tile, cover = share of the full beam's weight inside)."""
        sig = fwhm / 2.3548
        DL, B = np.meshgrid(((self.l - l0 + 180.0) % 360.0) - 180.0, self.b)
        w = np.exp(-0.5 * ((DL * np.cos(np.radians(B))) ** 2 + (B - b0) ** 2) / sig ** 2) * np.cos(np.radians(B))
        cover = float(w.sum() * self.pix / (2 * math.pi * math.radians(sig) ** 2))
        if w.sum() <= 0:
            return self.v, np.full(self.v.size, np.nan), 0.0
        return self.v, np.nansum(self.c * w[None], axis=(1, 2)) / w.sum(), cover


class Tiles:
    def __init__(self, directory: Path = TILES_DIR, loader=Tile):
        self.paths = [Path(p) for p in sorted(glob.glob(str(Path(directory) / "CAR_*.fits")))]
        self._loader, self._open = loader, {}

    def _tile(self, p: Path):
        if p not in self._open:
            self._open[p] = self._loader(p)
        return self._open[p]

    def beam(self, l0: float, b0: float, fwhm: float = FWHM_DEG):
        best = None
        for p in self.paths:
            r = self._tile(p).beam(l0, b0, fwhm)
            if best is None or r[2] > best[2]:
                best = r
        return best if best is not None else (None, None, 0.0)


# ------------------------------------------------------------------ helpers
def on_hi4pi_axis(vh: np.ndarray, v: np.ndarray, t: np.ndarray) -> np.ndarray:
    ok = np.isfinite(t)
    if ok.sum() < 50:
        return np.full(vh.size, np.nan)
    v, t = v[ok], t[ok]
    o = np.argsort(v)
    v, t = v[o], t[o]
    dv = float(np.median(np.diff(v))) or 1.0
    k = max(1, int(round(HI4PI_CHANNEL_KM_S / dv)))
    y = np.interp(vh, v, np.convolve(t, np.ones(k) / k, "same"), left=np.nan, right=np.nan)
    y[np.abs(vh) > V_ABS_MAX] = np.nan
    return y


def r_of(a: np.ndarray, b: np.ndarray) -> float:
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 30 or np.std(a[m]) == 0 or np.std(b[m]) == 0:
        return float("nan")
    return float(np.corrcoef(a[m], b[m])[0, 1])


def galactic(ra_deg: float, dec_deg: float):
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    g = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs").galactic
    return float(g.l.wrap_at(180 * u.deg).deg), float(g.b.deg)


def load_points(reduce_dir: Path) -> List[Dict[str, Any]]:
    import h5py
    pts = []
    for js in sorted((Path(reduce_dir) / "points").glob("*/master_spectrum.json"), key=lambda p: int(p.parent.name)):
        m = json.loads(js.read_text())
        with h5py.File(js.with_suffix(".h5")) as f:
            v = f["velocity_lsrk_m_s"][:] / 1e3
            t = f["relative_intensity"][:].astype(float)
            mk = f["mask"][:]
        t[mk != 0] = np.nan
        ra, dec = m["ra_hours"] * 15.0, m["dec_degrees"]
        l, b = galactic(ra, dec)
        pts.append({"i": m["point_index"], "ra": ra, "dec": dec, "l": l, "b": b, "v": v, "t": t})
    return pts


# ------------------------------------------------------------------ spectra
def spectra(points: Sequence[Dict[str, Any]], tiles, *, min_cover: float = 0.4, fwhm: float = FWHM_DEG) -> Dict[str, Any]:
    inside = []
    for p in points:
        vh, hs, cov = tiles.beam(p["l"], p["b"], fwhm)
        if vh is None or cov < min_cover:
            continue
        inside.append({"p": p, "vh": vh, "y": on_hi4pi_axis(vh, p["v"], p["t"]), "h": hs, "cover": cov})
    rows = []
    for i, a in enumerate(inside):
        vh = a["vh"]
        r_same = r_of(a["y"], a["h"])
        shuffled = [r_of(a["y"], np.interp(vh, o["vh"], o["h"])) for j, o in enumerate(inside) if j != i]
        mirror = r_of(a["y"], np.interp(vh, -vh[::-1], a["h"][::-1]))
        offs = {}
        for dd in OFFSETS_DEG:
            vals = []
            for ang in range(0, 360, 45):
                l1 = a["p"]["l"] + dd * math.cos(math.radians(ang)) / max(0.2, math.cos(math.radians(a["p"]["b"])))
                b1 = a["p"]["b"] + dd * math.sin(math.radians(ang))
                v2, h2, c2 = tiles.beam(l1, b1, fwhm)
                if v2 is not None and c2 >= 0.25:
                    vals.append(r_of(a["y"], np.interp(vh, v2, h2)))
            offs[str(dd)] = [round(x, 4) for x in vals if math.isfinite(x)]
        sh = [x for x in shuffled if math.isfinite(x)]
        rows.append({"point": a["p"]["i"], "l": round(a["p"]["l"], 2), "b": round(a["p"]["b"], 2),
                     "cover": round(a["cover"], 3), "r_same": r_same, "r_mirror": mirror,
                     "r_shuffled_median": float(np.median(sh)) if sh else None,
                     "n_shuffled": len(sh), "n_shuffled_at_or_above": int(sum(x >= r_same for x in sh)),
                     "r_offset": offs})
    return {"points": rows, "summary": spectra_verdict(rows), "min_cover": min_cover}


def spectra_verdict(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    good = [r for r in rows if math.isfinite(r["r_same"])]
    if not good:
        return {"n": 0, "verdict": "NO HI4PI TILE COVERS THESE POINTS"}
    same = np.array([r["r_same"] for r in good])
    mirror = np.array([r["r_mirror"] for r in good])
    sh = np.array([r["r_shuffled_median"] for r in good if r["r_shuffled_median"] is not None])
    beaten = sum(1 for r in good if r["n_shuffled"] and r["n_shuffled_at_or_above"] / r["n_shuffled"] <= 0.05)
    sign_ok = bool(np.median(same) > np.median(mirror) + 0.2)
    specific = bool(sh.size and beaten >= max(1, int(math.ceil(0.5 * len(good)))))
    verdict = ("POSITION-SPECIFIC MATCH" if specific and sign_ok else
               "HYDROGEN WITH THE RIGHT VELOCITY SIGN - NOT A POINTING-BY-POINTING MATCH" if sign_ok else
               "NO MATCH BEYOND THE NULLS")
    return {"n": len(good), "r_same_range": [float(same.min()), float(same.max())],
            "r_mirror_range": [float(np.nanmin(mirror)), float(np.nanmax(mirror))],
            "r_shuffled_median_range": [float(sh.min()), float(sh.max())] if sh.size else None,
            "points_beating_95pct_of_shuffled": beaten, "cover_range": [min(r["cover"] for r in good), max(r["cover"] for r in good)],
            "verdict": verdict}


# ------------------------------------------------------------------ map
def window_integral(p: Dict[str, Any], lo: float, hi: float) -> float:
    ok = np.isfinite(p["t"]) & (p["v"] >= lo) & (p["v"] <= hi)
    if ok.sum() < 100:
        return float("nan")
    o = np.argsort(p["v"][ok])
    return float(np.trapezoid(p["t"][ok][o], p["v"][ok][o]))


def _rotate(ra, dec, ra0, dec0, theta):
    def vec(a, d):
        a, d = math.radians(a), math.radians(d)
        return np.array([math.cos(d) * math.cos(a), math.cos(d) * math.sin(a), math.sin(d)])
    k, x, t = vec(ra0, dec0), vec(ra, dec), math.radians(theta)
    xr = x * math.cos(t) + np.cross(k, x) * math.sin(t) + k * np.dot(k, x) * (1 - math.cos(t))
    return math.degrees(math.atan2(xr[1], xr[0])) % 360.0, math.degrees(math.asin(float(np.clip(xr[2], -1, 1))))


def map_test(points: Sequence[Dict[str, Any]], beam_at, *, window=(-60.0, 60.0), n_perm: int = 5000,
             seed: int = SEED) -> Dict[str, Any]:
    """beam_at(ra_deg, dec_deg) -> HI4PI column density through the beam (hi4pi_expected._Beam.at)."""
    rng = np.random.default_rng(seed)
    val = np.array([window_integral(p, *window) for p in points])
    hv = np.array([beam_at(p["ra"], p["dec"]) for p in points], dtype=float)
    ok = np.isfinite(val) & np.isfinite(hv)
    val, hv, P = val[ok], hv[ok], [p for p, k in zip(points, ok) if k]
    if val.size < 5:
        return {"n": int(val.size), "verdict": "FEWER THAN 5 POINTS"}
    r = float(np.corrcoef(val, hv)[0, 1])
    perm = np.array([np.corrcoef(rng.permutation(val), hv)[0, 1] for _ in range(n_perm)])
    ra0 = math.degrees(math.atan2(np.mean([math.sin(math.radians(p["ra"])) for p in P]),
                                  np.mean([math.cos(math.radians(p["ra"])) for p in P]))) % 360.0
    dec0 = float(np.mean([p["dec"] for p in P]))
    rot = {}
    for th in range(30, 360, 30):
        hr = np.array([beam_at(*_rotate(p["ra"], p["dec"], ra0, dec0, th)) for p in P], dtype=float)
        rot[str(th)] = r_of(val, hr) if np.isfinite(hr).sum() >= 5 else None
    shift = {}
    for s in (-60, -40, -20, 20, 40, 60):
        hs = np.array([beam_at((p["ra"] + s) % 360.0, p["dec"]) for p in P], dtype=float)
        m = np.isfinite(hs)
        shift[str(s)] = float(np.corrcoef(val[m], hs[m])[0, 1]) if m.sum() >= 5 else None
    geo = [x for x in list(rot.values()) + list(shift.values()) if x is not None and math.isfinite(x)]
    perm_p = float(np.mean(perm >= r))
    beats_geometry = bool(geo) and r > float(np.quantile(geo, 0.9))
    verdict = ("POSITION-SPECIFIC" if perm_p < 0.05 and beats_geometry else
               "CORRELATED, BUT MOVED/ROTATED PATTERNS DO AS WELL - NOT POSITION-SPECIFIC" if perm_p < 0.05 else
               "NOT ABOVE THE PERMUTATION NULL")
    return {"n": int(val.size), "window_lsrk_km_s": list(window), "r": r, "perm_p": perm_p,
            "perm_95": float(np.quantile(perm, 0.95)), "rotated": rot, "shifted_ra": shift,
            "geometry_null_90pct": float(np.quantile(geo, 0.9)) if geo else None, "verdict": verdict,
            "ours": val.tolist(), "hi4pi": hv.tolist()}


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reduce_session")
    ap.add_argument("--out", default=None, help="output folder (default: <reduce session>/hi4pi_compare)")
    ap.add_argument("--min-cover", type=float, default=0.4)
    ap.add_argument("--beam-fwhm", type=float, default=FWHM_DEG)
    ap.add_argument("--map-window", default="-60:60", help="LSRK km/s lo:hi for the map test (write --map-window=-60:60)")
    ap.add_argument("--no-map", action="store_true")
    a = ap.parse_args(argv)
    rd = Path(a.reduce_session)
    pts = load_points(rd)
    res: Dict[str, Any] = {"schema": SCHEMA, "reduce_session": str(rd), "beam_fwhm_deg": a.beam_fwhm,
                           "spectra": spectra(pts, Tiles(), min_cover=a.min_cover, fwhm=a.beam_fwhm)}
    if not a.no_map:
        import hi4pi_expected as he
        path = he.nhi_path()
        if path is not None and Path(path).exists():
            sky, vec, w = he._load_sky(Path(path), a.beam_fwhm)
            beam = he._Beam(sky, vec, w, a.beam_fwhm)
            lo, hi = sorted(float(x) for x in a.map_window.split(":", 1))
            res["map"] = map_test(pts, beam.at, window=(lo, hi))
        else:
            res["map"] = {"verdict": "NO HI4PI COLUMN-DENSITY MAP ON DISK"}
    out = Path(a.out) if a.out else rd / "hi4pi_compare"
    out.mkdir(parents=True, exist_ok=True)
    (out / "hi4pi_compare.json").write_text(json.dumps(res, indent=2, default=float))
    s = res["spectra"]["summary"]
    print(f"spectra: {s.get('verdict')}  (n={s.get('n')}, r same {s.get('r_same_range')}, "
          f"other pointings {s.get('r_shuffled_median_range')}, mirrored {s.get('r_mirror_range')})")
    if "map" in res:
        m = res["map"]
        print(f"map: {m.get('verdict')}  (r={m.get('r')}, permutation p={m.get('perm_p')}, "
              f"moved/rotated 90% {m.get('geometry_null_90pct')})")
    print("written", out / "hi4pi_compare.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
