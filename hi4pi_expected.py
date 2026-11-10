"""What HI4PI says ALMITA should have seen, drawn under SCIENCE's A/B/C (BL-099, operator 2026-11-06: "poner debajo
del abc, inmediatamente abajo, el plot de la misma área capturada pero desde HI4PI").

Same frame, angle and extent as SCIENCE's own maps (science_engine's spherical offset frame about the board centre,
x east to the right, y north): A = the HI4PI column density seen through a Gaussian beam of the instrument's FWHM
at each real pointing, drawn in SCIENCE's own A cells (board.json corners); B/C = the same beam-convolved sky on
SCIENCE's B/C pixels, shown only where SCIENCE's B/C have data. Compare SHAPES only: SCIENCE is relative x m/s,
this is K km/s (NHI / 1.823e18, optically thin), and the all-sky NHI map integrates every velocity while SCIENCE
integrates its own LSRK window. Display/comparison only - nothing here feeds SCIENCE's products, and a missing or
unreadable HI4PI file is reported, never an error.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent
DEFAULT_NHI_FITS = ROOT / "data" / "reference" / "hi4pi" / "CAR_-600_600.fits"
ENV = "ALMITA_HI4PI_NHI_FITS"          # path override; "" or "off" disables
NHI_PER_K_KMS = 1.823e18               # cm^-2 per K km/s, optically thin 21 cm
VELOCITY_NOTE = ("HI4PI all-velocity column density (every velocity); SCIENCE integrates its own LSRK window. Toward "
                 "the Milky Way plane a +/-100 km/s window holds ~87-99.7 % of it (HI4PI cube, l~0-20 deg), so "
                 "compare shapes, not values.")


def nhi_path() -> Optional[Path]:
    raw = os.environ.get(ENV)
    if raw is None:
        return DEFAULT_NHI_FITS
    if raw.strip().lower() in ("", "off", "none", "0"):
        return None
    return Path(raw)


def _load_sky(path: Path, beam_fwhm_deg: float):
    """(map, unit vectors (3, ny, nx), area weights) of an equatorial CAR NHI map, block-averaged to <= beam/10."""
    from astropy.io import fits
    with fits.open(path, memmap=False) as hdul:
        hdu = next(h for h in hdul if getattr(h, "data", None) is not None and h.data.ndim == 2)
        data, h = hdu.data.astype(np.float64), hdu.header
    if not str(h.get("CTYPE1", "")).startswith("RA") or not str(h.get("CTYPE2", "")).startswith("DEC"):
        raise ValueError(f"{path.name}: expected an equatorial RA/DEC map, got {h.get('CTYPE1')}/{h.get('CTYPE2')}")
    step = abs(float(h["CDELT1"]))
    f = max(1, int(round(min(1.0, beam_fwhm_deg / 10.0) / step)))
    ny, nx = (data.shape[0] // f) * f, (data.shape[1] // f) * f
    with np.errstate(invalid="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            sky = np.nanmean(data[:ny, :nx].reshape(ny // f, f, nx // f, f), axis=(1, 3))
    ra = h["CRVAL1"] + (np.arange(nx // f) * f + (f - 1) / 2 + 1 - h["CRPIX1"]) * h["CDELT1"]
    dec = h["CRVAL2"] + (np.arange(ny // f) * f + (f - 1) / 2 + 1 - h["CRPIX2"]) * h["CDELT2"]
    a, d = np.meshgrid(np.radians(ra), np.radians(dec))
    vec = np.array([np.cos(d) * np.cos(a), np.cos(d) * np.sin(a), np.sin(d)])
    return sky, vec, np.cos(d)


class _Beam:
    def __init__(self, sky, vec, w, fwhm_deg):
        self.sky, self.vec, self.w, self.fwhm = sky, vec, w, fwhm_deg
        self.ok = np.isfinite(sky)

    def at(self, ra_deg: float, dec_deg: float) -> float:
        r, d = np.radians(ra_deg), np.radians(dec_deg)
        x = np.array([np.cos(d) * np.cos(r), np.cos(d) * np.sin(r), np.sin(d)])
        sep = np.degrees(np.arccos(np.clip(np.tensordot(x, self.vec, 1), -1.0, 1.0)))
        m = self.ok & (sep < 2.5 * self.fwhm)
        if not m.any():
            return float("nan")
        g = np.exp(-4 * np.log(2) * (sep[m] / self.fwhm) ** 2) * self.w[m]
        return float(np.sum(self.sky[m] * g) / np.sum(g)) / NHI_PER_K_KMS


def expected_products(built: Dict[str, Any], cfg, maps_dir: Path, path: Optional[Path] = "default") -> Dict[str, Any]:
    """Writes maps/map_hi4pi_expected.png (+ .svg) and returns the manifest section. Never raises."""
    try:
        return _expected_products(built, cfg, Path(maps_dir), nhi_path() if path == "default" else path)
    except Exception as exc:   # a comparison panel must never sink a SCIENCE run
        return {"status": "FAILED", "reason": f"{type(exc).__name__}: {exc}"}


def _expected_products(built, cfg, maps_dir: Path, path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {"status": "DISABLED", "reason": f"{ENV} disables it"}
    if not Path(path).is_file():
        return {"status": "UNAVAILABLE", "reason": f"HI4PI map not on disk: {path}"}
    fwhm = float(cfg.beam_fwhm_deg or 0)
    if not np.isfinite(fwhm) or fwhm <= 0:
        return {"status": "UNAVAILABLE", "reason": "no instrument beam FWHM in the SCIENCE config"}

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PatchCollection
    from matplotlib.patches import Polygon
    from scipy.interpolate import RegularGridInterpolator

    from science_engine.grid import pixel_centers_deg
    from science_engine.spatial import offsets_to_sky_deg, sky_offsets_deg
    from science_web_bridge import board_json_payload

    beam = _Beam(*_load_sky(Path(path), fwhm), fwhm)
    board = board_json_payload(built)
    cells = board["cells"]
    c_ra, c_dec = board["raster_base_grid"]["center_ra_deg"], board["raster_base_grid"]["center_dec_deg"]
    hw, hh = board["extent_deg"]["half_w"], board["extent_deg"]["half_h"]

    exp_a = np.array([beam.at(c["ra_deg"], c["dec_degrees"]) for c in cells])
    # B/C: the beam-convolved sky on a coarse grid in SCIENCE's frame (the beam is far wider than a B/C pixel),
    # interpolated onto SCIENCE's own B/C pixels and kept only where SCIENCE's B/C hold data
    step = min(1.0, fwhm / 10.0)
    gx, gy = np.arange(-hw, hw + step, step), np.arange(-hh, hh + step, step)
    GX, GY = np.meshgrid(gx, gy)
    gra, gde = offsets_to_sky_deg(GX, GY, c_ra, c_dec)
    coarse = np.array([beam.at(r, d) for r, d in zip(gra.ravel(), gde.ravel())]).reshape(GX.shape)
    interp = RegularGridInterpolator((gy, gx), coarse, bounds_error=False, fill_value=np.nan)

    def raster(grid, science_map):
        ra, dec = pixel_centers_deg(grid)
        x, y = sky_offsets_deg(ra, dec, c_ra, c_dec)
        return np.where(np.asarray(science_map.valid, bool), interp((y, x)), np.nan)

    exp_b, exp_c = raster(built["grid_b"], built["map_b"]), raster(built["grid_c"], built["map_c"])

    meas = np.array([c["value"] if c.get("valid") and c.get("value") is not None else np.nan for c in cells], float)
    both = np.isfinite(meas) & np.isfinite(exp_a)
    corr = float(np.corrcoef(meas[both], exp_a[both])[0, 1]) if both.sum() >= 3 and np.std(meas[both]) > 0 else None

    finite = np.concatenate([v[np.isfinite(v)] for v in (exp_a, exp_b, exp_c)])
    vmin, vmax = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
    fig, ax = plt.subplots(1, 3, figsize=(20, 7.6))
    pc = PatchCollection([Polygon(c["corners_xy"]) for c in cells], cmap="viridis", edgecolor="0.3", linewidth=0.3)
    pc.set_array(np.ma.masked_invalid(exp_a))
    pc.set_clim(vmin, vmax)
    ax[0].add_collection(pc)
    ext = [-hw, hw, -hh, hh]
    ax[1].imshow(exp_b, origin="lower", extent=ext, cmap="viridis", vmin=vmin, vmax=vmax, interpolation="nearest")
    ax[2].imshow(exp_c, origin="lower", extent=ext, cmap="viridis", vmin=vmin, vmax=vmax, interpolation="bilinear")
    titles = (f"A: HI4PI EXPECTED at the {len(cells)} pointings", f"B: HI4PI EXPECTED ({exp_b.shape[1]}x{exp_b.shape[0]} px)",
              f"C: HI4PI EXPECTED ({exp_c.shape[1]}x{exp_c.shape[0]} px)")
    for a, t in zip(ax, titles):
        a.set_title(t)
        a.set_xlim(-hw, hw)
        a.set_ylim(-hh, hh)
        a.set_aspect("equal")
        a.set_facecolor("black")
        a.set_xlabel("x = offset east [deg] (east →)")
    ax[0].set_ylabel("y = offset north [deg]")
    fig.colorbar(plt.cm.ScalarMappable(norm=plt.Normalize(vmin, vmax), cmap="viridis"), ax=ax, fraction=0.015, pad=0.02,
                 label=f"expected ∫T dv [K km/s] ({fwhm:g}° beam)")
    rot = (built.get("mosaic_geometry") or {}).get("rotation_deg") or 0.0
    corr_txt = f"shape vs measured A: r = {corr:+.2f} (n={int(both.sum())})" if corr is not None else "shape vs measured A: n/a"
    fig.suptitle(f"HI4PI · what ALMITA should see · same frame, extent{f' and angle ({rot:g}°)' if rot else ''} as A/B/C above"
                 f" · {corr_txt}", fontsize=13)
    fig.text(0.5, 0.01, VELOCITY_NOTE + " Units differ from SCIENCE (K km/s vs relative × m/s).", ha="center",
             fontsize=9, color="0.35")
    files = []
    for ext_name in ("png", "svg"):
        p = maps_dir / f"map_hi4pi_expected.{ext_name}"
        fig.savefig(p, dpi=150 if ext_name == "png" else None, bbox_inches="tight", facecolor="white")
        files.append(p.name)
    plt.close(fig)
    return {"status": "OK", "files": files, "source": str(path), "beam_fwhm_deg": fwhm,
            "units": "K km/s (NHI / 1.823e18, optically thin)", "velocity_note": VELOCITY_NOTE,
            "correlation_with_a": corr, "n_points_compared": int(both.sum()),
            "expected_range_k_km_s": [vmin, vmax]}
