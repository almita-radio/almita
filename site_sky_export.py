#!/usr/bin/env python3
"""Exports ALMITA's real sky measurements + an HI4PI "ghost" background for the public sky map page (site/sky/).

What it writes (all derived, nothing here is read back by the observing software):
  site/sky/ghost_full.png    HI4PI N_HI, galactic Mollweide, full survey resolution, faded ("the ghost")
  site/sky/ghost_beam.png    the same map smoothed to ALMITA's 20 deg beam (what ALMITA could ever see)
  site/sky/sky_data.js       window.ALMITA_SKY = {sessions, points, colour scale, ...}

Honesty rules baked in:
  * Only sessions captured on the REAL mount (LX200 OnStep) with a REDUCE run are used; simulator/bench runs never.
  * VALID sessions only (operator 2026-11-18): real mount + REDUCE + frequency switching in every point.
    Unswitched first-light runs (2026-11-05) are left out - their receiver bandpass (+-30..70 %) swamps the line
    integral (r vs HI4PI 0.07..0.66, inconsistent scale).
  * ALMITA's integral is relative (% x km/s). It is put on the HI4PI colour scale with ONE global factor (median
    ALMITA/HI4PI ratio over the coloured points), printed on the page. Agreement point by point is then visible,
    not forced: the factor is a single number for every session.

Usage:  .venv/bin/python site_sky_export.py [--out site/sky]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

import astropy_offline
astropy_offline.configure_astropy_offline()
import astropy.units as u
from astropy.coordinates import SkyCoord
from PIL import Image

import hi4pi_map

ROOT = Path(__file__).resolve().parent
BEAM_FWHM_DEG = 20.0
NHI_MIN, NHI_MAX = 1.0, 300.0           # colour scale, units of 1e20 cm^-2, logarithmic
IMG_W, IMG_H = 2000, 1000
CHILE = timezone(timedelta(hours=-3))


def inferno_lut(name: str = "inferno") -> list:
    from matplotlib import colormaps
    cmap = colormaps[name]
    return [[int(round(c * 255)) for c in cmap(i / 255)[:3]] for i in range(256)]


def mollweide_inverse(x: np.ndarray, y: np.ndarray):
    """x in [-2sqrt2, 2sqrt2], y in [-sqrt2, sqrt2] -> (l_deg, b_deg, inside). l grows to the LEFT (sky convention)."""
    r2 = math.sqrt(2.0)
    inside = (x / (2 * r2)) ** 2 + (y / r2) ** 2 <= 1.0
    theta = np.arcsin(np.clip(y / r2, -1, 1))
    lat = np.arcsin(np.clip((2 * theta + np.sin(2 * theta)) / math.pi, -1, 1))
    with np.errstate(divide="ignore", invalid="ignore"):
        lon = math.pi * x / (2 * r2 * np.cos(theta))
    l_deg = np.mod(-np.degrees(lon), 360.0)
    return l_deg, np.degrees(lat), inside & (np.abs(lon) <= math.pi)


def render_ghost(nhi_map: np.ndarray, wcs, out_png: Path, lut: list) -> None:
    r2 = math.sqrt(2.0)
    xs = (np.arange(IMG_W) + 0.5) / IMG_W * 4 * r2 - 2 * r2
    ys = r2 - (np.arange(IMG_H) + 0.5) / IMG_H * 2 * r2
    X, Y = np.meshgrid(xs, ys)
    l_deg, b_deg, inside = mollweide_inverse(X, Y)
    c = SkyCoord(l=l_deg[inside] * u.deg, b=b_deg[inside] * u.deg, frame="galactic").icrs
    px, py = wcs.all_world2pix(c.ra.deg, c.dec.deg, 0)
    ny, nx = nhi_map.shape
    ix = np.mod(np.round(px).astype(int), nx)
    iy = np.clip(np.round(py).astype(int), 0, ny - 1)
    vals = np.clip(nhi_map[iy, ix], 1e-3, None)
    t = np.clip((np.log10(vals) - math.log10(NHI_MIN)) / (math.log10(NHI_MAX) - math.log10(NHI_MIN)), 0, 1)
    idx = np.round(t * 255).astype(int)
    rgb = np.array(lut, dtype=np.uint8)[idx]
    img = np.zeros((IMG_H, IMG_W, 4), dtype=np.uint8)
    img[inside, :3] = rgb
    img[inside, 3] = 255
    Image.fromarray(img, "RGBA").save(out_png, optimize=True)


def sessions_from_catalog() -> list:
    cat = json.loads((ROOT / "data" / "sessions" / "catalog_index.json").read_text())
    out = []
    for s in cat["sessions"]:
        runs = (s.get("reduce") or {}).get("runs") or []
        if s.get("mount") != "LX200 OnStep" or not runs:
            continue
        out.append({"campaign_id": s["campaign_id"], "started_utc": s["started_utc"], "reduce_run": runs[-1],
                    "points_captured": s.get("points_captured")})
    return sorted(out, key=lambda s: s["started_utc"])


def read_points(reduce_run: str):
    base = ROOT / "data" / "reduced" / "ALMITA-OBSERVE" / reduce_run
    import h5py
    pts = []
    for p in sorted(glob.glob(str(base / "points" / "*" / "master_spectrum.h5")), key=lambda q: int(Path(q).parent.name)):
        meta = json.loads(Path(p).with_suffix(".json").read_text())
        with h5py.File(p, "r") as f:
            v = f["velocity_lsrk_m_s"][:] / 1e3
            y = f["relative_intensity"][:] * 100.0
            m = f["mask"][:]
        ok = (m == 0) & np.isfinite(y) & np.isfinite(v)
        v, y = v[ok], y[ok]
        o = np.argsort(v)
        pts.append({"meta": meta, "v": v[o], "y": y[o]})
    return pts


def binned(v: np.ndarray, y: np.ndarray, step: float = 2.0):
    edges = np.arange(math.floor(v.min() / step) * step, v.max() + step, step)
    idx = np.digitize(v, edges)
    vv, yy = [], []
    for k in range(1, len(edges)):
        s = idx == k
        if s.sum() >= 3:
            vv.append(round(float(edges[k - 1] + step / 2), 1))
            yy.append(round(float(np.median(y[s])), 3))
    return vv, yy


LINE_WINDOW_KM_S = 60.0       # the local + inner-Galaxy gas ALMITA's +-84 km/s switched window can hold
EDGE_KM_S = 65.0              # baseline: median of the window's edges, beyond the line


def line_area(v: list, y: list) -> float:
    """ALMITA's reading at one pointing, % x km/s: the area of the line over a baseline measured at the window's
    edges (|v| > 65 km/s), on a 10 km/s running mean, within +-60 km/s. Chosen 2026-11-18 against HI4PI on the
    50 valid pointings: r 0.92 vs 0.87 for the plain window integral, which lets baseline wander and noise at the
    window's ends dominate the faint high-latitude points. Not clipped to positive: clipping adds noise area and
    brightens the empty sky."""
    v = np.asarray(v, float); y = np.asarray(y, float)
    edge = np.abs(v) > EDGE_KM_S
    base = float(np.median(y[edge])) if edge.sum() > 3 else 0.0
    ys = np.convolve(y - base, np.ones(5) / 5, mode="same")
    m = np.abs(v) <= LINE_WINDOW_KM_S
    return float(np.trapezoid(ys[m], v[m])) if m.sum() > 3 else 0.0


def is_frequency_switched(meta: dict) -> bool:
    """Evidence, not a guess: REDUCE copies the capture's switching facts (fswitch_offset_hz, ...) into every
    frequency-switched point; an unswitched point has none."""
    return "fswitch_offset_hz" in json.dumps(meta)


def _hi4pi_maps():
    data, wcs, _ = hi4pi_map._load()
    full = np.clip(data, 0, None) * 1e-20
    # Strong continuum sources on the plane (Cas A, Cyg A, ...) show as HI ABSORPTION (N_HI <= 0) in the survey
    # integral: black pin-holes in a bright band. Fill only those pixels from the 1-deg smoothed map.
    one = hi4pi_map._smoothed(1.0)
    holes = (full < 0.5) & (one > 5.0)
    full[holes] = one[holes]
    return full, hi4pi_map._smoothed(BEAM_FWHM_DEG), wcs


def render_ghosts(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    full, beam, wcs = _hi4pi_maps()
    lut = inferno_lut()
    render_ghost(full, wcs, out / "ghost_full.png", lut)
    render_ghost(beam, wcs, out / "ghost_beam.png", lut)


def build_payload() -> dict:
    """VALID sessions only (operator 2026-11-18): real mount (LX200 OnStep) + a REDUCE run + frequency switching
    in every point. Simulator, bench and unswitched first-light runs are left out entirely."""
    _, beam, wcs = _hi4pi_maps()
    lut = inferno_lut()
    sessions, points = [], []
    for s in sessions_from_catalog():
        pts = read_points(s["reduce_run"])
        if not pts or not all(is_frequency_switched(p["meta"]) and p["v"].size > 10 for p in pts):
            continue
        t0 = datetime.fromisoformat(s["started_utc"].replace("Z", "+00:00"))
        sid = len(sessions)
        sessions.append({
            "id": sid, "campaign_id": s["campaign_id"], "reduce_run": s["reduce_run"],
            "started_utc": s["started_utc"], "started_chile": t0.astimezone(CHILE).strftime("%Y-%m-%d %H:%M"),
            "frequency_switching": True, "points": len(pts),
        })
        for p in pts:
            m = p["meta"]
            c = SkyCoord(ra=m["ra_hours"] * 15 * u.deg, dec=m["dec_degrees"] * u.deg, frame="icrs").galactic
            x, yy = wcs.all_world2pix(m["ra_hours"] * 15, m["dec_degrees"], 0)
            ref = float(beam[int(round(float(yy))), int(round(float(x))) % beam.shape[1]])
            vv, ys = binned(p["v"], p["y"])
            points.append({
                "s": sid, "i": int(m["point_index"]), "l": round(float(c.l.deg), 3), "b": round(float(c.b.deg), 3),
                "ra_h": round(float(m["ra_hours"]), 4), "dec": round(float(m["dec_degrees"]), 3),
                "t_utc": m.get("timestamp_start_utc"), "integral_pct_kms": round(line_area(vv, ys), 2),
                "hi4pi_beam_1e20": round(ref, 2), "spec_v": vv, "spec_y": ys,
            })

    for sess in sessions:      # grid spacing = median nearest-neighbour distance between the session's pointings
        q = [p for p in points if p["s"] == sess["id"]]
        if len(q) > 1:
            u_ = np.array([[math.cos(math.radians(p["b"])) * math.cos(math.radians(p["l"])),
                            math.cos(math.radians(p["b"])) * math.sin(math.radians(p["l"])), math.sin(math.radians(p["b"]))] for p in q])
            dd = np.degrees(np.arccos(np.clip(u_ @ u_.T, -1, 1))); np.fill_diagonal(dd, 999.0)
            sess["spacing_deg"] = round(float(np.median(dd.min(axis=1))), 2)
        else:
            sess["spacing_deg"] = BEAM_FWHM_DEG / 2
    for sess in sessions:
        q = [p for p in points if p["s"] == sess["id"]]
        sess["decon"] = deconvolve_session(q, sess["spacing_deg"]) if len(q) > 3 else {"iterations": 0, "grid": []}
    for sess in sessions:      # display-only shift (see DISPLAY_SHIFTS_DEG); true l/b stay in "l"/"b"
        sh = DISPLAY_SHIFTS_DEG.get(sess["campaign_id"])
        sess["display_shift_deg"] = sh
        dl, db = (sh["dl"], sh["db"]) if sh else (0.0, 0.0)
        for p in points:
            if p["s"] == sess["id"]:
                p["l_disp"], p["b_disp"] = round((p["l"] + dl) % 360, 3), round(p["b"] + db, 3)
        if sh and sess.get("decon"):
            sess["decon"]["grid"] = [[round((l + dl) % 360, 3), round(b + db, 3), v] for l, b, v in sess["decon"]["grid"]]
    ref_pts = [p for p in points if p["hi4pi_beam_1e20"] > 3.0]
    factor = float(np.median([p["integral_pct_kms"] / p["hi4pi_beam_1e20"] for p in ref_pts])) if ref_pts else None
    for p in points:
        p["nhi_equiv_1e20"] = round(p["integral_pct_kms"] / factor, 2) if factor else None
    r = r_log = None
    if len(points) > 3:
        a = np.array([p["integral_pct_kms"] for p in points]); b = np.array([p["hi4pi_beam_1e20"] for p in points])
        r = round(float(np.corrcoef(a, b)[0, 1]), 2)
        r_log = round(float(np.corrcoef(np.log10(np.clip(a, 1e-2, None)), np.log10(b))[0, 1]), 2)
    return {
        "schema": "almita.site_sky.v1", "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "beam_fwhm_deg": BEAM_FWHM_DEG, "scale_1e20": [NHI_MIN, NHI_MAX], "lut": lut,
        "factor_pct_kms_per_1e20": None if factor is None else round(factor, 3), "r_vs_hi4pi": r, "log_r_vs_hi4pi": r_log,
        "reading": "line area over an edge baseline, +-60 km/s, % x km/s (site_sky_export.line_area)",
        "image": {"w": IMG_W, "h": IMG_H},
        # STATUS tiles (operator 2026-11-18): ALMITA's OWN reading, linear, viridis like SCIENCE - not forced onto
        # the HI4PI scale: one factor cannot fit the plane (window misses gas beyond +-84 km/s, ~0.6 of HI4PI) and
        # the high-latitude sky (~1.3) at once.
        "tiles_lut": inferno_lut("viridis"),
        "tiles_scale_pct_kms": [0.0, round(float(np.percentile([p["integral_pct_kms"] for p in points], 98)), 1) if points else 1.0],
        "decon": {"method": "Richardson-Lucy, iterations per session by leave-one-out", "beam_fwhm_deg": BEAM_FWHM_DEG,
                  "step_deg": DECON_STEP_DEG, "expected_resolution_deg": [11, 15],
                  "note": "a model of the sky consistent with the readings and the beam, not a measurement"},
        "selection": "real mount (LX200 OnStep) + REDUCE + frequency switching in every point",
        "sessions": sessions, "points": points,
        "credits": {"hi4pi": "HI4PI Collaboration, Ben Bekhti N. et al. 2016, A&A 594, A116"},
    }


# DISPLAY-ONLY shifts (operator 2026-11-18: "solo para efectos de observación y visualización en el status"): a
# night whose dish was not aligned (no solar ALIGN) can be drawn moved on the map. Readings, r vs HI4PI and the
# deconvolution all use the TRUE pointing; only the drawn position moves, and the page says so.
# 2026-11-12 22:38:42 (l=20 grid): ALMITA's weighted centroid sits at b = +5.4 deg vs HI4PI's +0.8 -> moved -5 deg
# (two 2.5 deg tiles) in galactic latitude; operator then asked one tile more -> -7.5 deg.
DISPLAY_SHIFTS_DEG = {"ALMITA-OBSERVE-20261112-22:38:42": {"dl": 0.0, "db": -7.5}}


# ---- deconvolution (operator 2026-11-18: "quiero el gráfico de status con deconvolución") ----
# Simulation 2026-11-18 (HI4PI -> 20 deg beam -> grid + noise -> Richardson-Lucy): 10-30 iterations give ~11-15 deg
# effective resolution from 20-24 deg; beyond ~100 the noise starts to invent structure (error rises).
DECON_STEP_DEG = 1.25


ITERATION_CHOICES = (1, 2, 3, 4, 6, 8, 10, 15, 20, 30)   # capped at 30: past ~100 the noise invents structure


def _decon_setup(pts: list, beam: float):
    lon = np.array([p["l"] for p in pts]); lat = np.array([p["b"] for p in pts])
    y = np.array([p["integral_pct_kms"] for p in pts], float)
    cen = SkyCoord(l=float(np.degrees(np.angle(np.mean(np.exp(1j * np.radians(lon)))))) * u.deg, b=float(np.mean(lat)) * u.deg, frame="galactic")
    off = cen.skyoffset_frame()
    sp = SkyCoord(l=lon * u.deg, b=lat * u.deg, frame="galactic").transform_to(off)
    samp = np.stack([sp.lon.wrap_at(180 * u.deg).deg, sp.lat.deg], 1)
    gx = np.arange(samp[:, 0].min() - beam, samp[:, 0].max() + beam + 1e-9, DECON_STEP_DEG)
    gy = np.arange(samp[:, 1].min() - beam, samp[:, 1].max() + beam + 1e-9, DECON_STEP_DEG)
    grid = np.stack([a.ravel() for a in np.meshgrid(gx, gy)], 1)
    A = np.exp(-0.5 * ((samp[:, None, :] - grid[None, :, :]) ** 2).sum(-1) / (beam / 2.3548) ** 2)
    A /= A.sum(1, keepdims=True)
    # readings can be negative (FS baseline ripple): a constant floor in, taken off after (rows of A sum to 1, so a
    # constant sky passes through unchanged)
    floor = max(0.0, -float(y.min())) + 0.05 * float(np.abs(y).max() or 1.0)
    return off, samp, grid, A, y + floor, floor


def _rl(A: np.ndarray, y: np.ndarray, iterations: int, x0=None) -> np.ndarray:
    x = np.full(A.shape[1], float(y.mean())) if x0 is None else x0.copy()
    norm = np.clip(A.T @ np.ones_like(y), 1e-12, None)
    for _ in range(iterations):
        x = x * (A.T @ (y / np.clip(A @ x, 1e-12, None))) / norm
    return x


def choose_iterations(A: np.ndarray, y: np.ndarray) -> dict:
    """Leave-one-out: deconvolve without one pointing, predict its reading through the beam; the iteration count
    that predicts the unseen pointings best wins. Data only - HI4PI is never used to tune it. On 2026-11-18 it chose
    30 for the bright l=20 field and 3 for the faint b=-50 one, matching what HI4PI says is best (15-25 and ~2)."""
    err = {k: 0.0 for k in ITERATION_CHOICES}
    for i in range(len(y)):
        m = np.ones(len(y), bool); m[i] = False
        x, done = None, 0
        for k in ITERATION_CHOICES:
            x = _rl(A[m], y[m], k - done, x); done = k
            err[k] += float((A[i] @ x - y[i]) ** 2)
    rms = {k: math.sqrt(v / len(y)) for k, v in err.items()}
    return {"iterations": min(rms, key=rms.get), "loo_rms_pct_kms": {str(k): round(v, 1) for k, v in rms.items()}}


def deconvolve_session(pts: list, spacing: float, beam: float = BEAM_FWHM_DEG, iterations: int = None) -> dict:
    """Richardson-Lucy of one session's readings on a fine grid (local offset frame around the field's centre),
    Gaussian beam of FWHM `beam`, iterations chosen by leave-one-out unless given. Returns the grid only inside the
    cells the pointings sample (within 0.75 x spacing of one) - outside there is no data to constrain it."""
    off, samp, grid, A, y, floor = _decon_setup(pts, beam)
    choice = choose_iterations(A, y) if iterations is None else {"iterations": iterations}
    x = _rl(A, y, choice["iterations"]) - floor
    near = np.sqrt(((grid[:, None, :] - samp[None, :, :]) ** 2).sum(-1)).min(1) <= 0.75 * spacing
    gc = SkyCoord(lon=grid[near, 0] * u.deg, lat=grid[near, 1] * u.deg, frame=off).galactic
    choice["grid"] = [[round(float(a), 3), round(float(b), 3), round(float(v), 2)] for a, b, v in zip(gc.l.deg, gc.b.deg, x[near])]
    return choice


# ---- cached access for the web (STATUS) ----
CACHE_DIR = ROOT / "data" / "site_sky"
_payload_cache: dict = {"key": None, "payload": None}


def _inputs_key() -> tuple:
    """Changes whenever the session catalog or any REDUCE run changes - then the payload is rebuilt."""
    cat = ROOT / "data" / "sessions" / "catalog_index.json"
    red = ROOT / "data" / "reduced"
    runs = sorted((p.name, p.stat().st_mtime) for p in red.glob("*/REDUCE-*")) if red.is_dir() else []
    return (cat.stat().st_mtime if cat.is_file() else None, tuple(runs))


def cached_payload() -> dict:
    key = _inputs_key()
    if _payload_cache["key"] != key:
        _payload_cache["payload"] = build_payload()
        _payload_cache["key"] = key
    return _payload_cache["payload"]


def ghost_path(kind: str) -> Path:
    if kind not in ("full", "beam"):
        raise ValueError(kind)
    path = CACHE_DIR / f"ghost_{kind}.png"
    if not path.is_file():
        render_ghosts(CACHE_DIR)
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/sky")
    args = ap.parse_args()
    out = ROOT / args.out
    render_ghosts(out)
    payload = build_payload()
    (out / "sky_data.js").write_text("window.ALMITA_SKY = " + json.dumps(payload, separators=(",", ":")) + ";\n")
    print(f"sessions {len(payload['sessions'])}  points {len(payload['points'])}  factor {payload['factor_pct_kms_per_1e20']}"
          f"  r {payload['r_vs_hi4pi']}  log-r {payload['log_r_vs_hi4pi']}  -> {out}")


if __name__ == "__main__":
    main()
