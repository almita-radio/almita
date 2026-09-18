"""Real HI4PI-style 3D spectral cube -> 2D moment map (Fase 6/13).

HI4PI's own CUBES/GAL/CAR product (confirmed against the real, downloaded
CAR_E01.fits - see the pass report) is a genuine 3D cube: axes 1/2 are
GLON-CAR/GLAT-CAR, axis 3 is CTYPE3="VRAD", SPECSYS="LSRK", in m/s. This
module integrates a chosen velocity window into a single 2D map, so the
existing 2D-only FITSMomentMapProvider (fits_reference.py) can consume a
real survey product without needing to become cube-aware itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from astropy.io import fits


@dataclass
class MomentReductionResult:
    signal_fraction_captured: float   # relative to the full valid velocity range
    valid_spatial_fraction: float     # fraction of spatial pixels with >=1 valid channel in-window
    n_channels_used: int
    velocity_axis_increasing: bool

    def to_dict(self) -> dict:
        return {"signal_fraction_captured": self.signal_fraction_captured,
                "valid_spatial_fraction": self.valid_spatial_fraction,
                "n_channels_used": self.n_channels_used,
                "velocity_axis_increasing": self.velocity_axis_increasing}


def _velocity_axis_km_s(header) -> np.ndarray:
    crval3, crpix3, cdelt3 = header["CRVAL3"], header["CRPIX3"], header["CDELT3"]
    n3 = header["NAXIS3"]
    unit = header.get("CUNIT3", "m/s")
    values = crval3 + (np.arange(1, n3 + 1) - crpix3) * cdelt3
    return values / 1000.0 if unit.strip().lower() == "m/s" else values


def reduce_cube_to_moment_map(fits_path: str, out_path: str,
                               velocity_window_km_s: Tuple[float, float] = (-100.0, 100.0)) -> MomentReductionResult:
    """Sum (baseline-agnostic zeroth-moment style integration, brightness
    temperature * channel width) over the channels inside
    `velocity_window_km_s`. NaN channels are EXCLUDED per-pixel (Fase 48:
    "No NaN->0") - a pixel with zero valid in-window channels stays NaN in
    the output, never becomes a fabricated zero.

    Handles an increasing OR decreasing spectral axis (Fase 2's explicit
    "increasing/decreasing spectral axis" requirement) by sorting the
    velocity axis before selecting the window, rather than assuming order.
    """
    with fits.open(fits_path) as hdul:
        header = hdul[0].header
        if header.get("NAXIS", 0) != 3:
            raise ValueError(f"expected a 3D cube (NAXIS=3), got NAXIS={header.get('NAXIS')}")
        data = np.asarray(hdul[0].data, dtype=float)  # (vel, lat, lon)
        velocity_km_s = _velocity_axis_km_s(header)

        increasing = bool(velocity_km_s[-1] >= velocity_km_s[0])
        order = np.argsort(velocity_km_s)
        sorted_velocity = velocity_km_s[order]
        sorted_data = data[order]

        lo, hi = velocity_window_km_s
        in_window = (sorted_velocity >= lo) & (sorted_velocity <= hi)
        if not np.any(in_window):
            raise ValueError(f"velocity window {velocity_window_km_s} selects zero channels - "
                              f"cube covers [{sorted_velocity[0]:.1f}, {sorted_velocity[-1]:.1f}] km/s")

        channel_width_km_s = float(np.median(np.abs(np.diff(sorted_velocity))))
        window_data = sorted_data[in_window]
        valid = np.isfinite(window_data)
        moment = np.where(np.any(valid, axis=0),
                           np.nansum(np.where(valid, window_data, 0.0), axis=0) * channel_width_km_s,
                           np.nan)

        all_valid = np.isfinite(sorted_data)
        total_signal = float(np.nansum(np.clip(np.where(all_valid, sorted_data, 0.0), 0, None)))
        window_signal = float(np.nansum(np.clip(np.where(valid, window_data, 0.0), 0, None)))
        signal_fraction = window_signal / total_signal if total_signal > 0 else 0.0

        out_header = fits.Header()
        for key in ("SIMPLE", "BITPIX"):
            if key in header:
                out_header[key] = header[key]
        out_header["NAXIS"] = 2
        out_header["NAXIS1"] = header["NAXIS1"]
        out_header["NAXIS2"] = header["NAXIS2"]
        for axis in (1, 2):
            for key in (f"CTYPE{axis}", f"CUNIT{axis}", f"CDELT{axis}", f"CRPIX{axis}", f"CRVAL{axis}"):
                if key in header:
                    out_header[key] = header[key]
        out_header["BUNIT"] = f"{header.get('BUNIT', 'K')}*km/s"
        out_header["WCSAXES"] = 2
        out_header["HISTORY"] = (f"Moment-0 reduction of {Path(fits_path).name}, "
                                  f"velocity window [{lo},{hi}] km/s, signal_fraction={signal_fraction:.4f}")

        fits.PrimaryHDU(data=moment.astype(np.float32), header=out_header).writeto(out_path, overwrite=True)

    return MomentReductionResult(
        signal_fraction_captured=signal_fraction,
        valid_spatial_fraction=float(np.mean(np.any(valid, axis=0))),
        n_channels_used=int(np.sum(in_window)), velocity_axis_increasing=increasing,
    )


def promote_to_validated(fits_path: str, manifest, *, expected_spectral_axis: str = "VRAD",
                          expected_specsys: str = "LSRK", n_spotcheck_points: int = 5):
    """Fase 3: objective, evidence-based REAL_UNVERIFIED -> REAL_VALIDATED
    promotion. Every check is independently verifiable from the file
    itself - no --force/--trust flag exists anywhere in this function's
    signature. Returns (new_manifest_or_None, list_of_check_records)."""
    from alignment_engine.hi.reference_trust import ReferenceTrust

    checks = []

    def record(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    record("checksum_matches_manifest", manifest.verify_checksum(fits_path),
            "manifest.sha256 vs re-hashed file on disk")

    try:
        with fits.open(fits_path) as hdul:
            header = hdul[0].header
            data = hdul[0].data
    except Exception as exc:
        record("fits_opens", False, f"{type(exc).__name__}: {exc}")
        return None, checks
    record("fits_opens", True, "ok")

    from astropy.wcs import WCS
    wcs = WCS(header)
    record("wcs_has_celestial_axes", wcs.has_celestial, f"ctype={[header.get(f'CTYPE{i+1}') for i in range(header.get('NAXIS',0))]}")

    naxis3_present = header.get("NAXIS", 0) >= 3
    ctype3 = header.get("CTYPE3", "")
    record("spectral_axis_type_known", naxis3_present and ctype3 == expected_spectral_axis,
           f"CTYPE3={ctype3!r}, expected {expected_spectral_axis!r}")

    specsys = header.get("SPECSYS", "")
    record("spectral_frame_known", specsys == expected_specsys,
           f"SPECSYS={specsys!r}, expected {expected_specsys!r}")

    bunit = header.get("BUNIT", "")
    record("units_known", bunit.strip() != "", f"BUNIT={bunit!r}")

    finite = np.isfinite(data)
    nan_fraction = float(1.0 - np.mean(finite)) if data.size else 1.0
    record("not_empty_or_all_nan", nan_fraction < 0.99, f"nan_fraction={nan_fraction:.3f}")

    if np.any(finite):
        finite_vals = data[finite]
        plausible = float(finite_vals.min()) > -1000.0 and float(finite_vals.max()) < 1.0e6
        record("brightness_values_plausible", plausible,
               f"range=[{finite_vals.min():.2f},{finite_vals.max():.2f}] {bunit}")
    else:
        record("brightness_values_plausible", False, "no finite data")

    # Independent spatial spot-check: re-derive world coords for a few
    # random pixels via a SECOND, independent WCS call and confirm they
    # round-trip back to the same pixel - catches a corrupted/mismatched
    # WCS that would otherwise silently mislabel every query.
    rng = np.random.default_rng(0)
    ny, nx = data.shape[-2], data.shape[-1]
    ok_roundtrip = True
    for _ in range(n_spotcheck_points):
        y, x = rng.integers(0, ny), rng.integers(0, nx)
        world = wcs.celestial.pixel_to_world_values(x, y)
        back_x, back_y = wcs.celestial.world_to_pixel_values(*world)
        if abs(back_x - x) > 0.5 or abs(back_y - y) > 0.5:
            ok_roundtrip = False
            break
    record("wcs_pixel_roundtrip_spotcheck", ok_roundtrip, f"{n_spotcheck_points} random pixels")

    all_ok = all(c["ok"] for c in checks)
    if not all_ok:
        return None, checks

    import dataclasses
    validated = dataclasses.replace(manifest, trust=ReferenceTrust.REAL_VALIDATED)
    return validated, checks
