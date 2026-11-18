#!/usr/bin/env python3
"""OBSERVE window suggestion (operator 2026-11-18: "al picar algún lugar del mapa ... me recomiende la velocidad que tengo
que ocupar ... cuando yo pique una de las nubes de Magallanes ... que me aparezca el valor"). A SUGGESTION ONLY: nothing
here touches a plan or the SDR, and nothing is applied until the operator presses USE in OBSERVE.

For a region centre (RA/Dec) it predicts the HI spectrum the 20 deg beam will see and picks the WINDOW CENTRE (km/s LSRK)
and SWITCH OFFSET (the two OBSERVE offers: 600 kHz = +/-84 km/s, 300 kHz = +/-148 km/s) that keep the most of that
hydrogen inside the folded window, clear of the switching's negative ghosts, with line-free room for the baseline.

Where the expected spectrum comes from (always reported, never mixed silently):
  - GASS (Parkes Galactic All-Sky Survey, the southern half of HI4PI, stray-radiation corrected; McClure-Griffiths et
    al. 2009, Kalberla & Haud 2015): the Dec <= -45 cap downloaded 2026-11-13 (data/reference/hi/gass), when >= 90 % of
    the beam lies inside it. Measured: the Magellanic Clouds, the Bridge and the southern Milky Way. A coarse 1 deg
    copy is cached next to it the first time (gass_coarse_1deg.npz, ~10 MB) so a click costs milliseconds.
  - otherwise MODEL: the same flat rotation curve fswitch_center.py's "auto" uses, now as a profile (gas in a disc of
    ~flat surface density to 13 kpc then falling (3.5 kpc), 150 pc scale height seen through the Gaussian beam, 8 km/s dispersion). Approximate: it knows
    the velocity RANGE, not where the gas is clumped - and it knows nothing about the Magellanic Clouds.

The choice (score_window):
  - TARGET = the hydrogen worth the window: local gas (|v| < 30 km/s, everywhere and always present) counts 15 %, rising
    to full weight at |v| >= 80 km/s (beyond the Milky Way's own wings in a 20 deg beam - so a faint Magellanic Cloud
    is not outvoted by them); where (almost) everything is local, all of it counts;
  - a target channel only counts if it lies inside the window minus MARGIN_KM_S at each edge (the fold measures its
    baseline there) and no ghost (half the emission thrown +/- the throw) sits on it with more than GHOST_LOST_RATIO
    of its own brightness - the fold masks those channels;
  - a window with almost no line-free channels left (the Galactic centre) loses score, since its baseline would be
    fitted on the line itself;
  - ties go to 600 kHz (the default, bench- and sky-tested) and then to the centre nearest the line at rest.
"""
from __future__ import annotations

import argparse
import json
import math
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

import fswitch_center as FC

ROOT = Path(__file__).resolve().parent
GASS_FITS = ROOT / "data" / "reference" / "hi" / "gass" / "GASS_magellanic_cap45.fits"
GASS_COARSE = GASS_FITS.with_name("gass_coarse_1deg.npz")
COARSE_PX = 12                      # 12 x 0.08 deg = ~1 deg pixels: plenty for a 20 deg beam
MIN_BEAM_COVER = 0.9

V_AXIS = np.arange(-400.0, 450.0 + 0.5, 1.0)        # km/s LSRK, the common axis of every expected spectrum
OFFSETS_HZ = (600e3, 300e3)                          # what OBSERVE's SWITCH OFFSET offers
CENTRE_STEP_KM_S = 5.0
CENTRE_RANGE_KM_S = (-300.0, 400.0)
LOCAL_EDGE_KM_S = 30.0               # |v| below this: local gas, weight LOCAL_WEIGHT
FULL_WEIGHT_KM_S = 80.0              # |v| above this: full weight (the Milky Way's wings in a 20 deg beam reach ~60)
LOCAL_WEIGHT = 0.15
MARGIN_KM_S = 15.0
GHOST_LOST_RATIO = 0.25
MIN_FREE_FRACTION = 0.2
TIE_SCORE = 0.03
NEAR_BEST = 0.01                     # score difference treated as a tie between centres
MODEL_DISPERSION_KM_S = 8.0
MODEL_SCALE_HEIGHT_KPC = 0.15
MODEL_FLAT_TO_KPC, MODEL_FALLOFF_KPC = 13.0, 3.5   # HI surface density ~flat to 13 kpc, then exp. fall-off

_lock = threading.Lock()
_gass: Optional[Dict[str, np.ndarray]] = None


# ------------------------------------------------------------------ sky geometry
def _unit(ra_deg, dec_deg) -> np.ndarray:
    ra, dec = np.radians(ra_deg), np.radians(dec_deg)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


def _beam_weights(ra_deg: np.ndarray, dec_deg: np.ndarray, ra0: float, dec0: float, fwhm_deg: float) -> np.ndarray:
    cosd = np.clip(_unit(ra_deg, dec_deg) @ _unit(ra0, dec0), -1.0, 1.0)
    sep = np.degrees(np.arccos(cosd))
    sig = fwhm_deg / 2.3548
    return np.exp(-0.5 * (sep / sig) ** 2)


def _beam_total_weight(dec0: float, fwhm_deg: float) -> float:
    """The beam's full weight on the sphere (solid-angle element cos(dec)), for the coverage fraction."""
    sig = fwhm_deg / 2.3548
    r = np.radians(np.linspace(0.0, 5.0 * sig, 400))
    return float(np.trapezoid(np.exp(-0.5 * (np.degrees(r) / sig) ** 2) * np.sin(r), r) * 2.0 * math.pi)


# ------------------------------------------------------------------ GASS (measured, Dec <= -45)
def build_gass_coarse(fits_path: Path = GASS_FITS, out_path: Path = GASS_COARSE) -> Path:
    """Block-average the GASS cap to ~1 deg pixels on V_AXIS (NaN where GASS has no velocity) - once, ~1 min."""
    from astropy.io import fits
    from astropy.wcs import WCS
    with fits.open(fits_path, memmap=True) as hdul:
        h = hdul[0].header
        data = hdul[0].data                                   # (v, y, x)
        nv, ny, nx = data.shape
        v = (h["CRVAL3"] + (np.arange(1, nv + 1) - h["CRPIX3"]) * h["CDELT3"]) / 1000.0
        by, bx = ny // COARSE_PX, nx // COARSE_PX
        cube = np.full((nv, by, bx), np.nan, np.float32)
        for k in range(nv):
            plane = np.asarray(data[k, :by * COARSE_PX, :bx * COARSE_PX], np.float32)
            blocks = plane.reshape(by, COARSE_PX, bx, COARSE_PX)
            with np.errstate(all="ignore"), __import__("warnings").catch_warnings():
                __import__("warnings").simplefilter("ignore", RuntimeWarning)
                cube[k] = np.nanmean(blocks, axis=(1, 3))
        w = WCS(h).celestial
        yy, xx = np.mgrid[0:by, 0:bx]
        cy, cx = yy * COARSE_PX + (COARSE_PX - 1) / 2.0, xx * COARSE_PX + (COARSE_PX - 1) / 2.0
        ra, dec = w.wcs_pix2world(cx, cy, 0)
    good = np.isfinite(ra) & np.isfinite(dec) & np.any(np.isfinite(cube), axis=0)
    spectra = cube[:, good].T                                 # (n_pix, nv)
    resampled = np.full((spectra.shape[0], V_AXIS.size), np.nan, np.float32)
    inside = (V_AXIS >= v.min()) & (V_AXIS <= v.max())
    order = np.argsort(v)
    for i, s in enumerate(spectra):
        ok = np.isfinite(s[order])
        if ok.sum() > 2:
            resampled[i, inside] = np.interp(V_AXIS[inside], v[order][ok], s[order][ok])
    np.savez_compressed(out_path, ra=ra[good].astype(np.float32), dec=dec[good].astype(np.float32),
                        spectra=resampled, v_min=float(v.min()), v_max=float(v.max()),
                        source=str(fits_path.name), coarse_px=COARSE_PX)
    return out_path


def _load_gass() -> Optional[Dict[str, np.ndarray]]:
    global _gass
    with _lock:
        if _gass is None:
            if not GASS_COARSE.exists():
                if not GASS_FITS.exists():
                    return None
                build_gass_coarse()
            z = np.load(GASS_COARSE)
            _gass = {k: z[k] for k in z.files}
        return _gass


def gass_beam_spectrum(ra_deg: float, dec_deg: float, fwhm_deg: float) -> Optional[Dict[str, Any]]:
    g = _load_gass()
    if g is None:
        return None
    w = _beam_weights(g["ra"], g["dec"], ra_deg, dec_deg, fwhm_deg)
    pix_sr = math.radians(COARSE_PX * 0.08) ** 2              # ZEA is equal-area: every pixel the same solid angle
    cover = float(w.sum() * pix_sr / _beam_total_weight(dec_deg, fwhm_deg))
    if cover < MIN_BEAM_COVER:
        return None
    keep = w > 1e-4
    s = g["spectra"][keep]
    ww = w[keep][:, None] * np.isfinite(s)
    with np.errstate(all="ignore"):
        t = np.nansum(np.nan_to_num(s) * ww, axis=0) / ww.sum(axis=0)
    t[ww.sum(axis=0) == 0] = np.nan
    return {"source": "GASS", "t": t, "beam_cover": min(cover, 1.0),
            "v_coverage": (float(g["v_min"]), float(g["v_max"])),
            "label": "GASS (Parkes, measured; southern half of HI4PI)", "units": "K"}


# ------------------------------------------------------------------ MODEL (everywhere else)
def model_beam_spectrum(l_deg: float, b_deg: float, fwhm_deg: float, step_deg: float = 2.0) -> Dict[str, Any]:
    """Flat-rotation-curve disc through a Gaussian beam: relative units, shape and range only."""
    sig_b = fwhm_deg / 2.3548
    r = 1.5 * fwhm_deg
    k = int(math.floor(r / step_deg))
    hist = np.zeros(V_AXIS.size)
    d = np.linspace(0.0, 40.0, 800)
    dd = d[1] - d[0]
    for i in range(-k, k + 1):
        for j in range(-k, k + 1):
            dl, db = i * step_deg, j * step_deg
            if dl * dl + db * db > r * r:
                continue
            bb = b_deg + db
            if abs(bb) > 89.5:
                continue
            wb = math.exp(-0.5 * (dl * dl + db * db) / sig_b ** 2) * math.cos(math.radians(bb))
            ll = (l_deg + dl / max(math.cos(math.radians(bb)), 0.05)) % 360.0
            lr, br = math.radians(ll), math.radians(bb)
            R = np.sqrt(FC.R0_KPC ** 2 + (d * math.cos(br)) ** 2 - 2 * FC.R0_KPC * d * math.cos(br) * math.cos(lr))
            z = d * math.sin(br)
            dens = np.exp(-0.5 * (z / MODEL_SCALE_HEIGHT_KPC) ** 2) * (R <= FC.RMAX_KPC) * (R > 0.3)
            dens = dens * np.exp(-np.clip(R - MODEL_FLAT_TO_KPC, 0.0, None) / MODEL_FALLOFF_KPC)
            v = FC.V0_KM_S * (FC.R0_KPC / np.maximum(R, 0.3) - 1.0) * math.sin(lr) * math.cos(br)
            hist += wb * np.histogram(v, bins=V_AXIS.size, range=(V_AXIS[0] - 0.5, V_AXIS[-1] + 0.5),
                                      weights=dens * dd)[0]
    kern_v = np.arange(-4 * MODEL_DISPERSION_KM_S, 4 * MODEL_DISPERSION_KM_S + 1)
    kern = np.exp(-0.5 * (kern_v / MODEL_DISPERSION_KM_S) ** 2)
    t = np.convolve(hist, kern / kern.sum(), mode="same")
    if t.max() > 0:
        t = t / t.max()
    return {"source": "MODEL", "t": t, "beam_cover": None, "v_coverage": (float(V_AXIS[0]), float(V_AXIS[-1])),
            "label": "MODEL (flat rotation curve - range only; knows nothing of the Magellanic Clouds)",
            "units": "relative"}


# ------------------------------------------------------------------ choosing the window
def _target_weights(t: np.ndarray) -> np.ndarray:
    tt = np.nan_to_num(np.clip(t, 0.0, None))
    av = np.abs(V_AXIS)
    local = av < LOCAL_EDGE_KM_S
    w = LOCAL_WEIGHT + (1.0 - LOCAL_WEIGHT) * np.clip((av - LOCAL_EDGE_KM_S) / (FULL_WEIGHT_KM_S - LOCAL_EDGE_KM_S), 0, 1)
    total = tt.sum()
    if total <= 0 or (tt * ~local).sum() < 0.1 * total:      # (almost) only local gas: all of it is the target
        w = np.ones_like(tt)
    return tt * w


def ghost_spectrum(t: np.ndarray, offset_hz: float) -> np.ndarray:
    """The fold's negative ghosts: half the emission thrown to +/- the switch throw (shown positive here)."""
    tt = np.nan_to_num(np.clip(t, 0.0, None))
    throw = FC.throw_km_s(offset_hz)
    return 0.5 * (np.interp(V_AXIS + throw, V_AXIS, tt, left=0, right=0)
                  + np.interp(V_AXIS - throw, V_AXIS, tt, left=0, right=0))


def score_window(t: np.ndarray, centre: float, offset_hz: float, target: Optional[np.ndarray] = None,
                 ghost: Optional[np.ndarray] = None) -> Dict[str, Any]:
    tt = np.nan_to_num(np.clip(t, 0.0, None))
    target = _target_weights(t) if target is None else target
    ghost = ghost_spectrum(t, offset_hz) if ghost is None else ghost
    half = FC.half_window_km_s(offset_hz)
    lo, hi = centre - half, centre + half
    in_win = (V_AXIS >= lo) & (V_AXIS <= hi)
    usable = (V_AXIS >= lo + MARGIN_KM_S) & (V_AXIS <= hi - MARGIN_KM_S)
    lost = ghost > GHOST_LOST_RATIO * np.maximum(tt, 1e-9)
    lost &= ghost > 0.02 * tt.max() if tt.max() > 0 else lost
    clean = usable & ~lost
    tsum = target.sum()
    covered = float(target[clean].sum() / tsum) if tsum > 0 else 0.0
    peak = tt.max() if tt.max() > 0 else 1.0
    free = in_win & (tt < 0.05 * peak) & (ghost < 0.05 * peak)
    free_fraction = float(free.sum() / max(in_win.sum(), 1))
    score = covered - 0.3 * max(0.0, MIN_FREE_FRACTION - free_fraction) / MIN_FREE_FRACTION
    all_in = float(tt[in_win].sum() / tt.sum()) if tt.sum() > 0 else 0.0
    nonlocal_ = np.abs(V_AXIS) >= LOCAL_EDGE_KM_S
    nl_sum = tt[nonlocal_].sum()
    nonlocal_in = float(tt[nonlocal_ & clean].sum() / nl_sum) if nl_sum > 0.02 * max(tt.sum(), 1e-12) else None
    ghost_lost_target = float(target[usable & lost].sum() / tsum) if tsum > 0 else 0.0
    return {"centre_km_s": centre, "offset_hz": offset_hz, "window_lsrk_km_s": [round(lo, 1), round(hi, 1)],
            "half_window_km_s": round(half, 1), "throw_km_s": round(FC.throw_km_s(offset_hz), 1),
            "score": score, "target_in_window": covered, "all_hi_in_window": all_in, "nonlocal_in_window": nonlocal_in,
            "target_lost_to_ghosts": ghost_lost_target, "line_free_fraction": free_fraction}


def best_for_offset(t: np.ndarray, offset_hz: float) -> Dict[str, Any]:
    """Best centre for one offset. Among the centres within NEAR_BEST of the best score (a broad line often fits several
    ways), the one centred on the hydrogen it actually covers: |centre - centroid of its clean, usable target| smallest -
    so the Large Magellanic Cloud gets +260 (the cloud in the middle), not +180 (the cloud at one edge)."""
    target, ghost = _target_weights(t), ghost_spectrum(t, offset_hz)
    tt = np.nan_to_num(np.clip(t, 0.0, None))
    lost = (ghost > GHOST_LOST_RATIO * np.maximum(tt, 1e-9)) & (ghost > 0.02 * max(tt.max(), 1e-12))
    centres = np.arange(CENTRE_RANGE_KM_S[0], CENTRE_RANGE_KM_S[1] + 0.1, CENTRE_STEP_KM_S)
    scored = [score_window(t, float(c), offset_hz, target, ghost) for c in centres]
    top = max(x["score"] for x in scored)
    near = [x for x in scored if x["score"] >= top - NEAR_BEST]

    def off_centre(x):
        lo, hi = x["window_lsrk_km_s"]
        m = (V_AXIS >= lo + MARGIN_KM_S) & (V_AXIS <= hi - MARGIN_KM_S) & ~lost
        w = target * m
        cen = float((w * V_AXIS).sum() / w.sum()) if w.sum() > 0 else 0.0
        return (round(abs(x["centre_km_s"] - cen) / CENTRE_STEP_KM_S), abs(x["centre_km_s"]))
    return min(near, key=off_centre)


def _ghost_notes(t: np.ndarray, choice: Dict[str, Any]) -> List[str]:
    """Where the local gas (always there) throws its ghosts, and whether they land in the window (REDUCE masks them)."""
    lo, hi = choice["window_lsrk_km_s"]
    notes = []
    for g0, g1 in FC.local_gas_ghosts_lsrk(choice["offset_hz"]):
        if g0 < hi and lo < g1:
            notes.append(f"the local gas leaves a negative ghost at {g0 + FC.LOCAL_KM_S:+.0f} km/s inside the window: "
                         f"REDUCE masks {g0:+.0f}..{g1:+.0f}")
    if choice["target_lost_to_ghosts"] > 0.05:
        notes.append(f"{choice['target_lost_to_ghosts'] * 100:.0f} % of the target sits under a ghost and is masked")
    return notes


def _extent(t: np.ndarray, weights: np.ndarray, lo_q: float = 0.05, hi_q: float = 0.95) -> Optional[List[float]]:
    w = np.nan_to_num(np.clip(weights, 0, None))
    if w.sum() <= 0:
        return None
    c = np.cumsum(w) / w.sum()
    return [float(V_AXIS[np.searchsorted(c, lo_q)]), float(V_AXIS[min(np.searchsorted(c, hi_q), V_AXIS.size - 1)])]


def suggest(ra_hours: float, dec_deg: float, beam_fwhm_deg: float = 20.0) -> Dict[str, Any]:
    """The suggestion for a region centred at (ra_hours, dec_deg). Pure: reads reference data only."""
    if not (math.isfinite(ra_hours) and math.isfinite(dec_deg) and -90.0 <= dec_deg <= 90.0):
        raise ValueError("ra_hours and dec_deg must be finite, dec within -90..90")
    ra_deg = (ra_hours % 24.0) * 15.0
    l_deg, b_deg = FC.gal_from_equatorial(ra_hours % 24.0, dec_deg)
    spec = gass_beam_spectrum(ra_deg, dec_deg, beam_fwhm_deg) or model_beam_spectrum(l_deg, b_deg, beam_fwhm_deg)
    t = spec["t"]
    options = [best_for_offset(t, off) for off in OFFSETS_HZ]
    default = options[0]
    pick = max(options, key=lambda s: s["score"])
    if pick is not default and pick["score"] - default["score"] <= TIE_SCORE:
        pick = default
    target = _target_weights(t)
    tt = np.nan_to_num(np.clip(t, 0, None))
    peak_i = int(np.argmax(target)) if target.sum() > 0 else int(np.argmax(tt))
    notes = _ghost_notes(t, pick)
    if spec["source"] == "MODEL":
        notes.append("model only: the range is right, the clumps are not - check QUICKLOOK on the first points")
    if pick["line_free_fraction"] < MIN_FREE_FRACTION:
        notes.append("the line fills most of the window: little room for the baseline (expect a weaker fold)")
    if pick["nonlocal_in_window"] is not None and pick["nonlocal_in_window"] < 0.6:
        notes.append(f"only {pick['nonlocal_in_window'] * 100:.0f} % of the hydrogen beyond the local gas fits in any "
                     "window here")
    vmin_cov, vmax_cov = spec["v_coverage"]
    if spec["source"] == "GASS" and (pick["window_lsrk_km_s"][0] < vmin_cov or pick["window_lsrk_km_s"][1] > vmax_cov):
        fin = np.flatnonzero(np.isfinite(t))
        edge = max(tt[fin[0]], tt[fin[-1]]) if fin.size else 0.0
        if edge > 0.05 * max(tt.max(), 1e-9):            # emission still there at GASS's velocity limit
            notes.append(f"GASS only has {vmin_cov:+.0f}..{vmax_cov:+.0f} km/s and the gas reaches its edge: "
                         "beyond it is unmeasured")
    step = 5
    return {
        "schema": "almita.fswitch_suggestion.v1",
        "suggestion_only": True,
        "ra_hours": round(ra_hours % 24.0, 4), "dec_deg": round(dec_deg, 3),
        "l_deg": round(l_deg, 2), "b_deg": round(b_deg, 2), "beam_fwhm_deg": beam_fwhm_deg,
        "source": spec["source"], "source_label": spec["label"], "units": spec["units"],
        "beam_cover": spec["beam_cover"],
        "centre_km_s": pick["centre_km_s"], "offset_hz": pick["offset_hz"],
        "window_lsrk_km_s": pick["window_lsrk_km_s"], "half_window_km_s": pick["half_window_km_s"],
        "throw_km_s": pick["throw_km_s"],
        "target_in_window": round(pick["target_in_window"], 3), "all_hi_in_window": round(pick["all_hi_in_window"], 3),
        "nonlocal_in_window": None if pick["nonlocal_in_window"] is None else round(pick["nonlocal_in_window"], 3),
        "line_free_fraction": round(pick["line_free_fraction"], 3),
        "target_extent_lsrk_km_s": _extent(t, target), "target_peak_lsrk_km_s": float(V_AXIS[peak_i]),
        "peak_value": float(tt.max()),
        "alternatives": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in o.items()} for o in options],
        "notes": notes,
        "spectrum": {"v_km_s": V_AXIS[::step].tolist(),
                     "t": [None if not np.isfinite(x) else round(float(x), 4) for x in t[::step]],
                     "ghost": [round(float(x), 4) for x in ghost_spectrum(t, pick["offset_hz"])[::step]]},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OBSERVE window suggestion for a region centre (suggestion only)")
    ap.add_argument("--build-cache", action="store_true", help="build the coarse GASS cache and exit")
    ap.add_argument("--ra-hours", type=float)
    ap.add_argument("--dec-deg", type=float)
    ap.add_argument("--beam-fwhm-deg", type=float, default=20.0)
    a = ap.parse_args(argv)
    if a.build_cache:
        print(build_gass_coarse())
        return 0
    s = suggest(a.ra_hours, a.dec_deg, a.beam_fwhm_deg)
    s.pop("spectrum")
    print(json.dumps(s, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
