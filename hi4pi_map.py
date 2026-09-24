"""Real, observational all-sky HI column density map for the ALIGN web sky view - NOT the synthetic model in
data/hi_sky_catalog_2000pts.csv (generate_hi_catalog.py), which this module does not use or fall back to.

SOURCE (verified, not guessed):
    HI4PI: a full-sky HI survey based on EBHIS and GASS.
    HI4PI Collaboration, Ben Bekhti N., et al. 2016, Astron. Astrophys. 594, A116 (2016A&A...594A.116H).
    Retrieved from the CDS/VizieR catalog mirror: J/A+A/594/A116
      https://cdsarc.cds.unistra.fr/ftp/J/A+A/594/A116/NHI/EQ2000/CAR_-600_600.fits
    Product: all-sky HI column density (N_HI), integrated over the FULL published LSR velocity range
    (-600..+600 km/s), Equatorial J2000, CAR (Plate Carree) projection, ~0.0833 deg/pixel (4323x2163).
    Unit (from the file's own BUNIT header): cm**-2. This is a column density, NOT a brightness temperature -
    the earlier synthetic overlay's "tb_kelvin" field does not apply here.
    License: no separate machine-readable license file was found on the CDS mirror at the time of writing.
    CDS/VizieR's own long-standing, well-documented policy is free access to catalog data for scientific and
    educational use with citation of the original publication; this is NOT a commercial-use grant, and this
    module is used here strictly as an offline engineering/alignment reference for an amateur radio telescope,
    not to republish or resell the data. See fetch_hi4pi_map.py for the exact download step and citation text
    shown verbatim to the operator in the web UI.

WHY NOT HEALPix: HI4PI also ships a HEALPix (nside=1024) product, but it is far larger and needs an extra
dependency (healpy/astropy_healpix) this project does not otherwise use. The CAR-projection product above is a
plain, small (~36 MB) RA/Dec pixel grid that astropy.wcs already reads directly - no HEALPix library needed.
"""
from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

import astropy_offline
astropy_offline.configure_astropy_offline()
import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, ICRS, SkyCoord
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS

ROOT = Path(__file__).resolve().parent
MAP_PATH = ROOT / "data" / "reference" / "hi4pi" / "CAR_-600_600.fits"
SOURCE_URL = "https://cdsarc.cds.unistra.fr/ftp/J/A+A/594/A116/NHI/EQ2000/CAR_-600_600.fits"

SOURCE = {
    "name": "HI4PI", "product": "all-sky HI column density (N_HI), integrated -600..+600 km/s LSR, Equatorial CAR projection",
    "citation": "HI4PI Collaboration, Ben Bekhti N. et al. 2016, A&A 594, A116 (2016A&A...594A.116H)",
    "retrieved_from": f"CDS/VizieR catalog J/A+A/594/A116 ({SOURCE_URL})",
    "unit": "cm^-2 (HI column density, N_HI - NOT a brightness temperature)",
    "resolution_deg": None,       # filled in once the file is read (its own CDELT)
    "license_note": "CDS/VizieR catalog data; no separate machine-readable license file found on the CDS mirror. "
                    "CDS's established policy is free scientific/research reuse with citation of the original "
                    "paper above; this is not a commercial-use grant. Used here only as an offline alignment "
                    "reference, not redistributed.",
    "not_almita_data": True,
}

_lock = threading.Lock()
_cache: Dict[str, Any] = {"data": None, "wcs": None, "pix_scale_deg": None, "smoothed": {}}


class HI4PIUnavailable(Exception):
    """The real map is missing or unreadable - callers must say so plainly and offer manual selection,
    never silently substitute the synthetic model."""


def _load() -> Tuple[np.ndarray, WCS, float]:
    with _lock:
        if _cache["data"] is None:
            if not MAP_PATH.is_file():
                raise HI4PIUnavailable(f"HI4PI map not found at {MAP_PATH} - run fetch_hi4pi_map.py to download it "
                                       f"from {SOURCE_URL} ({SOURCE['citation']})")
            try:
                with fits.open(MAP_PATH) as hdul:
                    data = np.nan_to_num(hdul[0].data.astype(np.float32), nan=0.0)
                    hdr = hdul[0].header
                    wcs = WCS(hdr)
                    pix_scale = abs(float(hdr["CDELT1"]))
            except (OSError, ValueError, KeyError) as exc:
                raise HI4PIUnavailable(f"HI4PI map at {MAP_PATH} could not be read: {type(exc).__name__}: {exc}") from exc
            _cache.update(data=data, wcs=wcs, pix_scale_deg=pix_scale, smoothed={})
        return _cache["data"], _cache["wcs"], _cache["pix_scale_deg"]


def _smoothed(fwhm_deg: float) -> np.ndarray:
    """Beam-smoothed map (Gaussian, sigma = FWHM / 2.3548), cached per (rounded) FWHM. Values are pre-scaled by
    1e-20 (still real cm^-2 data, just in units of 1e20 cm^-2) so single/double-precision statistics on it never
    overflow - the true physical values are ~1e18-1e22 cm^-2, whose squares overflow float32."""
    data, _, pix_scale = _load()
    key = round(float(fwhm_deg), 1)
    with _lock:
        cached = _cache["smoothed"].get(key)
        if cached is not None:
            return cached
        sigma_pix = max(0.5, (fwhm_deg / 2.3548) / pix_scale)
        ny, nx = data.shape
        ky, kx = np.fft.fftfreq(ny), np.fft.fftfreq(nx)
        kx2, ky2 = np.meshgrid(kx, ky)
        gauss_kernel = np.exp(-2 * (math.pi ** 2) * (sigma_pix ** 2) * (kx2 ** 2 + ky2 ** 2))
        smoothed = np.real(np.fft.ifft2(np.fft.fft2(np.clip(data, 0, None)) * gauss_kernel)).astype(np.float32) * 1e-20
        if len(_cache["smoothed"]) >= 4:                # small cache: a handful of recently-used FWHM values
            _cache["smoothed"].pop(next(iter(_cache["smoothed"])))
        _cache["smoothed"][key] = smoothed
        return smoothed


def _local_contrast(smoothed: np.ndarray, wcs: WCS, pix_scale_deg: float, ra_deg: float, dec_deg: float, radius_deg: float) -> Optional[Tuple[float, float]]:
    """Real std/mean (in units of 1e20 cm^-2) of the beam-smoothed map within radius_deg of (ra_deg, dec_deg)."""
    ny, nx = smoothed.shape
    x0f, y0f = wcs.all_world2pix(ra_deg, dec_deg, 0)
    x0, y0 = int(round(float(x0f))), int(round(float(y0f)))
    if not (0 <= y0 < ny):
        return None
    r = int(math.ceil(radius_deg / pix_scale_deg))
    y1, y2 = max(0, y0 - r), min(ny, y0 + r + 1)
    xs = np.arange(x0 - r, x0 + r + 1) % nx               # RA wraps around
    patch = smoothed[y1:y2][:, xs]
    yy, xx = np.mgrid[y1:y2, x0 - r:x0 + r + 1]
    within = np.hypot(xx - x0, yy - y0) <= (radius_deg / pix_scale_deg)
    vals = patch[within].astype(np.float64)
    if vals.size < 10:
        return None
    return float(np.std(vals)), float(np.mean(np.clip(vals, 0, None)))


# Safety cap on how many candidates get the (comparatively expensive, one astropy transform each) full-pattern
# temporal check, walked in descending-score order - bounds worst-case latency when many top-scoring candidates
# turn out to have a pattern that dips below the limit at some point during the run (e.g. min_elevation close to
# the exterior radius, or a candidate that is actively setting).
_MAX_PATTERN_CHECKS = 60


def sky_grid_and_ranking(location: EarthLocation, obstime: Time, min_elevation_deg: float, beam_fwhm_deg: float,
                         exterior_radius_deg: float, ring_radii_deg: Sequence[float], ring_points: int,
                         capture_time_s: float, grid_n: int = 90, top_n: int = 3) -> Dict[str, Any]:
    """Real, no-hardware computation for the ALIGN HI sky view: a continuous Alt/Az raster of the REAL HI4PI map
    (already beam-smoothed by beam_fwhm_deg) for the current time/location, PLUS the A/B/C ranking computed from
    THE SAME smoothed map and the SAME grid points (so the colour shown and the ranking always correspond to the
    same data). A candidate is only offered if its WHOLE real ring pattern (ring_radii_deg/ring_points - the SAME
    pattern RUN will command) clears min_elevation_deg not just right now but AT THE REAL WALL-CLOCK TIME each of
    its points would actually be captured during a run starting at `obstime` (alignment.py's own
    pattern_temporal_altitudes(), reused here - not reimplemented, so this always matches what RUN itself will
    check before its first movement): a ~30-minute, 25-point run visits its last points long after PLAN/RUN was
    clicked, and a centre that clears the limit right now can still have a later point fall below it before the
    run finishes. Candidates whose pattern dips below the limit at ANY point in the run are discarded, never
    returned as if usable. Raises HI4PIUnavailable if the map cannot be read - callers must not fall back to the
    synthetic model silently."""
    data, wcs, pix_scale_deg = _load()
    smoothed = _smoothed(beam_fwhm_deg)

    # Disk raster in the SAME normalized (px,py) convention as the client's drawSkyView(): r=hypot(px,py)<=1,
    # alt = 90*(1-r), az measured clockwise from north (px = sin(az), py = -cos(az)).
    lin = np.linspace(-1, 1, grid_n)
    px, py = np.meshgrid(lin, lin)
    r = np.hypot(px, py)
    inside = r <= 1.0
    alt = 90.0 * (1.0 - r[inside])
    az = np.degrees(np.arctan2(px[inside], -py[inside])) % 360.0
    altaz = SkyCoord(AltAz(az=az * u.deg, alt=alt * u.deg, obstime=obstime, location=location))
    icrs = altaz.transform_to(ICRS())
    ra_deg, dec_deg = icrs.ra.deg, icrs.dec.deg

    x_pix, y_pix = wcs.all_world2pix(ra_deg, dec_deg, 0)
    ny, nx = smoothed.shape
    xi = np.mod(np.round(x_pix).astype(int), nx)
    yi = np.round(y_pix).astype(int)
    valid = (yi >= 0) & (yi < ny)
    values = np.full(ra_deg.shape, np.nan, dtype=np.float64)
    values[valid] = smoothed[yi[valid], xi[valid]]

    grid = np.full((grid_n, grid_n), np.nan, dtype=np.float64)
    grid[inside] = values
    finite = np.isfinite(values) & (values > 0)
    vlo, vhi = (float(np.percentile(values[finite], 2)), float(np.percentile(values[finite], 98))) if finite.any() else (0.0, 1.0)
    if vhi <= vlo:
        vhi = vlo + 1e-6

    # Ranking candidates: the same grid points, restricted to real altitude >= min_elevation.
    candidates_mask = alt >= min_elevation_deg
    cand_ra, cand_dec, cand_alt, cand_az = ra_deg[candidates_mask], dec_deg[candidates_mask], alt[candidates_mask], az[candidates_mask]
    scored: List[Tuple[float, float, float, float, float, float, float]] = []
    for i in range(cand_ra.size):
        stat = _local_contrast(smoothed, wcs, pix_scale_deg, float(cand_ra[i]), float(cand_dec[i]), exterior_radius_deg)
        if stat is None:
            continue
        contrast, mean = stat
        score = contrast * math.log1p(max(mean, 0.0))
        scored.append((score, float(cand_ra[i]), float(cand_dec[i]), float(cand_alt[i]), float(cand_az[i]), contrast, mean))
    from alignment import pattern_temporal_altitudes, MEASURED_ACQUIRE_OVERHEAD_PER_POINT_S

    scored.sort(key=lambda row: -row[0])
    min_sep_deg = 2.0 * exterior_radius_deg
    chosen: List[Dict[str, Any]] = []
    labels = ["A", "B", "C", "D", "E", "F"][:max(top_n, 0)]
    pattern_checks = 0
    pattern_rejections: List[float] = []          # margin (negative) of each candidate rejected for its pattern
    # A ring point at radius r from a centre of altitude h cannot be higher than h+r nor lower than h-r RIGHT NOW
    # (the ring is, to first order, a ROTATED circle of the same angular radius in Alt/Az regardless of
    # parallactic angle - small-angle, so this bound holds for the pattern radii ALIGN actually uses). Cheaply
    # reject a candidate whose OWN current altitude can never clear the limit even in the best case, before
    # spending an astropy transform on it - otherwise, when the sky's highest-contrast structure sits right at
    # the horizon-ish edge (exactly the case this check exists for), every one of the top-scored candidates is a
    # near-miss and the real, further-away valid candidates would never be reached within a bounded number of
    # (expensive) exact checks. This is only a fast pre-filter on the CURRENT instant - the exact temporal check
    # below (which also accounts for the run's ~30 minutes of sky motion) is what actually decides.
    max_radius = max(ring_radii_deg) if ring_radii_deg else 0.0
    for row in scored:
        c = SkyCoord(ra=row[1] * u.deg, dec=row[2] * u.deg)
        if any(c.separation(SkyCoord(ra=ch["ra_hours"] * 15 * u.deg, dec=ch["dec_deg"] * u.deg)).deg < min_sep_deg for ch in chosen):
            continue
        if row[3] < min_elevation_deg + max_radius:    # row[3] = this candidate's own (centre) altitude, right now
            pattern_rejections.append(row[3] - max_radius - min_elevation_deg)   # a conservative worst-case margin estimate
            continue
        if pattern_checks >= _MAX_PATTERN_CHECKS:
            break
        temporal = pattern_temporal_altitudes(c, ring_radii_deg, ring_points, location, obstime, capture_time_s,
                                              MEASURED_ACQUIRE_OVERHEAD_PER_POINT_S)
        pattern_checks += 1
        margin = temporal["min_altitude_deg"] - min_elevation_deg
        if margin < 0:
            pattern_rejections.append(margin)
            continue
        chosen.append({"label": labels[len(chosen)], "score": row[0], "ra_hours": row[1] / 15.0, "dec_deg": row[2],
                       "alt_deg": row[3], "az_deg": row[4], "contrast_1e20cm2": row[5], "mean_1e20cm2": row[6],
                       "pattern_min_altitude_deg": temporal["min_altitude_deg"], "pattern_margin_deg": margin,
                       "pattern_min_altitude_point_index": temporal["min_altitude_point_index"],
                       "pattern_min_altitude_utc": temporal["min_altitude_utc"],
                       "run_duration_s": temporal["run_duration_s"], "temporal_profile": temporal["points"]})
        if len(chosen) >= top_n:
            break

    return {"grid_n": grid_n, "grid": [None if not np.isfinite(v) else round(float(v), 6) for v in grid.ravel()],
            "value_range_1e20cm2": [vlo, vhi], "areas": chosen,
            "candidates_considered": int(cand_ra.size), "candidates_pattern_checked": pattern_checks,
            "candidates_pattern_rejected": len(pattern_rejections),
            "worst_rejected_pattern_margin_deg": min(pattern_rejections) if pattern_rejections else None,
            "pattern_check_exhausted": pattern_checks >= _MAX_PATTERN_CHECKS and len(chosen) < top_n,
            "source": {**SOURCE, "resolution_deg": pix_scale_deg}}
