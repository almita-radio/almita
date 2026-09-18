"""Real (not NotImplemented) FITS-based HI reference provider (Fase 3/46).

MVP scope, explicit and deliberate (Fase 7A, "HI_SPATIAL_MOMENT"): a single
2D moment/column-density map with a WCS (Galactic or ICRS, any of the
CAR/TAN/SIN projections HI4PI's own small cutout products use - see the
survey-recommendation section of the final report) - not yet a full
spectral cube (Fase 7B, "HI_SPECTRAL_PROFILE"). The provider protocol
(HIReferenceProvider in targets/hi_reference.py) does not close the door on
that: choose_target()/template_for() return the same shape either way, so
a future cube-aware subclass can be added without touching callers.

No FITS file ships with this repo and none is downloaded automatically
(the project's offline-first, no-mass-download policy) - see
validate_reference() and the "REFERENCE" section of the final report for
what a real HI4PI cutout would need before this class could load it.
Tested here only against a small SYNTHETIC FITS fixture built in-memory by
the test suite (Fase 46) - never a real survey product.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS

from alignment_engine.hi.reference_trust import ReferenceManifest, ReferenceTrust


@dataclass
class FITSMomentMapValidation:
    ok: bool
    reason: str
    shape: Optional[Tuple[int, int]] = None
    coordinate_system: Optional[str] = None
    nan_fraction: Optional[float] = None
    finite_min: Optional[float] = None
    finite_max: Optional[float] = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "shape": self.shape,
                "coordinate_system": self.coordinate_system, "nan_fraction": self.nan_fraction,
                "finite_min": self.finite_min, "finite_max": self.finite_max}


def inspect_fits_moment_map(fits_path: str) -> dict:
    """Read-only description of a local FITS file's structure - dimensions,
    axes, units, NaN fraction, WCS coordinate system - never used to
    silently decide REAL_VALIDATED (see validate_reference() below,
    Fase 45's explicit "no declarar REAL_VALIDATED simplemente porque FITS abre")."""
    with fits.open(fits_path) as hdul:
        header = hdul[0].header
        data = hdul[0].data
        wcs = WCS(header)
        finite = np.isfinite(data) if data is not None else np.array([])
        return {
            "shape": tuple(data.shape) if data is not None else None,
            "naxis": header.get("NAXIS"),
            "ctype": [header.get(f"CTYPE{i+1}") for i in range(header.get("NAXIS", 0))],
            "bunit": header.get("BUNIT"),
            "wcs_is_celestial": bool(wcs.has_celestial),
            "nan_fraction": float(1.0 - np.mean(finite)) if data is not None and data.size else None,
            "finite_min": float(np.min(data[finite])) if data is not None and np.any(finite) else None,
            "finite_max": float(np.max(data[finite])) if data is not None and np.any(finite) else None,
        }


def validate_reference(fits_path: str, manifest: ReferenceManifest) -> FITSMomentMapValidation:
    """Fase 45: opening successfully is NOT sufficient for REAL_VALIDATED.
    Checks, in order: file opens and has a celestial WCS; checksum in
    `manifest` matches the file on disk; data is 2D; not empty/all-NaN;
    finite value range is physically plausible for a brightness-
    temperature or column-density map (non-negative, not absurdly large -
    a hard sanity ceiling, not a precision claim)."""
    if not manifest.verify_checksum(fits_path):
        return FITSMomentMapValidation(False, "checksum in manifest does not match file on disk")
    try:
        info = inspect_fits_moment_map(fits_path)
    except Exception as exc:
        return FITSMomentMapValidation(False, f"failed to open/parse FITS file: {type(exc).__name__}: {exc}")
    if not info["wcs_is_celestial"]:
        return FITSMomentMapValidation(False, "FITS WCS has no celestial axes")
    if info["shape"] is None or len(info["shape"]) != 2:
        return FITSMomentMapValidation(False, f"expected a 2D moment map, got shape {info['shape']}")
    if info["nan_fraction"] is None or info["nan_fraction"] >= 0.99:
        return FITSMomentMapValidation(False, "data is empty or entirely NaN")
    if info["finite_min"] is not None and info["finite_min"] < -1000.0:
        return FITSMomentMapValidation(False, f"implausible minimum value {info['finite_min']} for a HI map")
    if info["finite_max"] is not None and info["finite_max"] > 1.0e6:
        return FITSMomentMapValidation(False, f"implausible maximum value {info['finite_max']} for a HI map")
    return FITSMomentMapValidation(True, "ok", shape=info["shape"],
                                    coordinate_system=manifest.coordinate_system,
                                    nan_fraction=info["nan_fraction"],
                                    finite_min=info["finite_min"], finite_max=info["finite_max"])


class FITSMomentMapProvider:
    """Loads a local, already-validated 2D FITS moment map and implements
    the SAME HIReferenceProvider interface SyntheticHIReferenceProvider
    does, so engine.py never needs to know which provider it holds."""

    def __init__(self, fits_path: str, manifest: ReferenceManifest, require_validated: bool = True):
        if require_validated and manifest.trust != ReferenceTrust.REAL_VALIDATED:
            raise ValueError(
                f"FITSMomentMapProvider refuses to load a reference with trust={manifest.trust.value} "
                f"when require_validated=True - call validate_reference() and update the manifest first, "
                f"or pass require_validated=False for explicit, deliberate testing only."
            )
        self.manifest = manifest
        self.is_observational = manifest.trust in (ReferenceTrust.REAL_UNVERIFIED, ReferenceTrust.REAL_VALIDATED)
        with fits.open(fits_path) as hdul:
            self._data = np.asarray(hdul[0].data, dtype=float)
            self._wcs = WCS(hdul[0].header)
        self._pixel_catalog_cache: Dict[int, Tuple[SkyCoord, np.ndarray]] = {}

    def _pixel_catalog(self, stride: int) -> Tuple[SkyCoord, np.ndarray]:
        """Flattens every `stride`-th finite pixel into a (coords, values)
        catalog, cached per stride - reused by BOTH choose_target() and
        template_for() via alignment.gaussian_convolved_template(), the
        SAME exact-spherical-separation Gaussian convolution the synthetic
        catalog path already uses (not a second, ad hoc approximation).
        Subsampling is safe because the beam FWHM (~20 deg) is vastly
        larger than this survey's ~0.083 deg pixel scale - see the pass
        report's runtime measurement for the chosen default stride."""
        if stride not in self._pixel_catalog_cache:
            ny, nx = self._data.shape
            ys, xs = np.mgrid[0:ny:stride, 0:nx:stride]
            finite = np.isfinite(self._data[ys, xs])
            lon, lat = self._wcs.celestial.pixel_to_world_values(xs[finite], ys[finite])
            frame = "galactic" if self.manifest.coordinate_system.lower().startswith("gal") else "icrs"
            coords = (SkyCoord(l=lon, b=lat, unit="deg", frame="galactic").icrs if frame == "galactic"
                      else SkyCoord(ra=lon, dec=lat, unit="deg", frame="icrs"))
            self._pixel_catalog_cache[stride] = (coords, self._data[ys, xs][finite])
        return self._pixel_catalog_cache[stride]

    def beam_convolved_value_fn(self, beam_fwhm_deg: float, stride: int = 3):
        """A callable(SkyCoord array) -> beam-convolved values, usable
        both for target SELECTION (Fase 15: must score the beam-convolved
        sky, not raw pixels) and directly as a fitting template centered
        implicitly at each query point (no separate `center` needed - this
        evaluates the true convolution at whatever points are asked for)."""
        from alignment import gaussian_convolved_template
        catalog_coords, catalog_values = self._pixel_catalog(stride)

        def _fn(coords: SkyCoord) -> np.ndarray:
            query = SkyCoord(coords).reshape((-1,))
            return gaussian_convolved_template(query, catalog_coords, catalog_values, beam_fwhm_deg)
        return _fn

    def _value_at(self, coords: SkyCoord) -> np.ndarray:
        frame = "galactic" if self.manifest.coordinate_system.lower().startswith("gal") else "icrs"
        transformed = coords.transform_to(frame)
        lon = transformed.spherical.lon.deg if frame == "icrs" else transformed.l.deg
        lat = transformed.spherical.lat.deg if frame == "icrs" else transformed.b.deg
        x, y = self._wcs.celestial.world_to_pixel_values(lon, lat)
        x = np.round(np.atleast_1d(x)).astype(int)
        y = np.round(np.atleast_1d(y)).astype(int)
        out = np.full(x.shape, np.nan)
        in_bounds = (x >= 0) & (x < self._data.shape[1]) & (y >= 0) & (y < self._data.shape[0])
        out[in_bounds] = self._data[y[in_bounds], x[in_bounds]]
        return out

    def choose_target(self, location: EarthLocation, obstime: Time, min_altitude_deg: float,
                       beam_fwhm_deg: float, stride: int = 3) -> Tuple[SkyCoord, Dict]:
        from alignment_engine.hi.target_selection import select_best_target_from_grid
        value_fn = self.beam_convolved_value_fn(beam_fwhm_deg, stride=stride)
        return select_best_target_from_grid(self._sample_grid(), value_fn, location, obstime,
                                             min_altitude_deg, beam_fwhm_deg)

    def _sample_grid(self) -> SkyCoord:
        ny, nx = self._data.shape
        ys, xs = np.mgrid[0:ny:max(1, ny // 60), 0:nx:max(1, nx // 60)]
        lon, lat = self._wcs.celestial.pixel_to_world_values(xs.ravel(), ys.ravel())
        frame = "galactic" if self.manifest.coordinate_system.lower().startswith("gal") else "icrs"
        if frame == "galactic":
            return SkyCoord(l=lon, b=lat, unit="deg", frame="galactic").icrs
        return SkyCoord(ra=lon, dec=lat, unit="deg", frame="icrs")

    def template_for(self, center: SkyCoord, beam_fwhm_deg: float, stride: int = 3):
        """`center` is accepted for interface compatibility with
        SyntheticHIReferenceProvider (Fase 3: the fitter must not know
        which provider it holds) but is not otherwise needed here - the
        real beam convolution (beam_convolved_value_fn) evaluates
        correctly at ANY query point directly, unlike the coarse
        probe-pattern approximation this replaced. `stride` trades pixel-
        catalog density for speed - the beam FWHM (~20 deg) is vastly
        larger than this survey's ~0.083 deg pixel scale, so a coarser
        stride costs negligible accuracy but matters a lot for a fit that
        gets re-evaluated many times (bootstrap - see quality_v2.py)."""
        return self.beam_convolved_value_fn(beam_fwhm_deg, stride=stride)
