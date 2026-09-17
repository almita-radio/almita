"""HI reference sky provider (Fase 4/7).

Confirmed by repo audit: no real HI survey (LAB Survey, HI4PI, or any FITS)
exists anywhere in this repo. data/hi_sky_catalog_2000pts.csv is generated
by generate_hi_catalog.py, whose own docstring says "Estimates brightness
temperature based on galactic distribution model" - an analytic disk model,
not observational data. This is exactly why alignment.py's own HI path
marks its result template_ground_truth=False and hard-blocks SYNC
regardless of confidence (analysis["sync_eligible"] = False, status
"NON-OBSERVATIONAL TEMPLATE - SYNC BLOCKED"). This provider preserves that
same honesty: SyntheticHIReferenceProvider.is_observational is always False.

A real survey integration (LAB Survey or HI4PI, as a local FITS file) is
future work requiring explicit authorization - never an automatic download,
per this repo's offline-first policy (astropy_offline.py). FITSHIReferenceProvider
below documents the expected interface without pretending to implement it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Protocol, Tuple

from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time

from alignment import LocalSphericalTemplate, choose_hi_region, load_hi_catalog


class HIReferenceProvider(Protocol):
    is_observational: bool

    def choose_target(self, location: EarthLocation, obstime: Time,
                       min_altitude_deg: float, beam_fwhm_deg: float) -> Tuple[SkyCoord, Dict]: ...

    def template_for(self, center: SkyCoord, beam_fwhm_deg: float):
        """Returns a single-argument callable(SkyCoord array) -> predicted
        values, bound to `center` and the given beam FWHM (Fase 6's
        `Reference(point + offset)`) - the same single-arg convention
        alignment.estimate_template_offset expects everywhere else."""
        ...


@dataclass
class SyntheticHIReferenceProvider:
    """Wraps the existing analytic-model catalog. Honest about what it is:
    is_observational=False, so callers (the sync flow) can refuse to ever
    treat its output as ground truth for a real SYNC - matching
    alignment.py's own existing behavior."""

    catalog_path: str = "data/hi_sky_catalog_2000pts.csv"
    is_observational: bool = False

    def __post_init__(self):
        self._coords, self._values = load_hi_catalog(self.catalog_path)

    def choose_target(self, location: EarthLocation, obstime: Time,
                       min_altitude_deg: float, beam_fwhm_deg: float) -> Tuple[SkyCoord, Dict]:
        center, info = choose_hi_region(self._coords, self._values, location, obstime,
                                         min_altitude_deg, beam_fwhm_deg)
        return center, {**info, "provider": "synthetic_galactic_model", "is_observational": False}

    def template_for(self, center: SkyCoord, beam_fwhm_deg: float):
        return LocalSphericalTemplate(center, self._coords, self._values, beam_fwhm_deg)


class FITSHIReferenceProvider:
    """NOT IMPLEMENTED - documents what a real survey integration needs.

    Expected input: a local FITS file (e.g. a LAB Survey or HI4PI cutout)
    with a WCS mapping pixels to Galactic or ICRS coordinates and a
    brightness-temperature data array - obtained and placed on disk by an
    operator ahead of time, never downloaded automatically by this code
    (this repo's astropy offline policy applies here too: no IERS-style
    auto-fetch, and no dataset auto-fetch either). Loading it would mean:
    read the FITS with astropy.io.fits + astropy.wcs.WCS, expose the same
    choose_target()/template() interface as SyntheticHIReferenceProvider so
    engine.py never needs to know which provider it is holding, and set
    is_observational=True so the sync flow can allow a real SYNC from an
    HI-based result for the first time.
    """

    is_observational = True

    def __init__(self, fits_path: str):
        raise NotImplementedError(
            "FITSHIReferenceProvider requires a real local HI survey FITS file "
            "(e.g. LAB Survey or HI4PI) that this repo does not have yet. "
            "See this class's docstring for the expected interface. Do not "
            "add automatic dataset downloading - obtain the file manually "
            "and place it on disk first."
        )
