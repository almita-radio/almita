"""Spherical coordinate geometry (sections 12-14). All angular
separations go through astropy's validated SkyCoord machinery - never a
naive sqrt((ra1-ra2)^2+(dec1-dec2)^2), which is wrong near the RA wrap
and at high declination.

Canonical internal representation: degrees (`ra_deg`, `dec_deg`), ICRS.
MasterSpectrum's own storage unit is hours (0-24) - converted exactly
once, at ingest (`ingest.py`), never re-derived downstream.
"""
from __future__ import annotations

import numpy as np
from astropy.coordinates import SkyCoord
import astropy.units as u


def ra_hours_to_deg(ra_hours: float | np.ndarray) -> float | np.ndarray:
    return np.asarray(ra_hours) * 15.0


def normalize_ra_deg(ra_deg: float | np.ndarray) -> float | np.ndarray:
    """Wrap to [0, 360) - same normalization philosophy as
    grid_generator.py's normalize_ra_hours, ported to degrees."""
    return np.mod(ra_deg, 360.0)


def angular_separation_deg(ra1_deg: np.ndarray, dec1_deg: np.ndarray,
                           ra2_deg: np.ndarray, dec2_deg: np.ndarray) -> np.ndarray:
    """Great-circle separation via astropy (Vincenty formula under the
    hood) - correct across the RA wrap and at any declination, unlike a
    naive Euclidean difference (section 14)."""
    c1 = SkyCoord(ra=np.asarray(ra1_deg) * u.deg, dec=np.asarray(dec1_deg) * u.deg, frame="icrs")
    c2 = SkyCoord(ra=np.asarray(ra2_deg) * u.deg, dec=np.asarray(dec2_deg) * u.deg, frame="icrs")
    return c1.separation(c2).to_value(u.deg)


def tangent_plane_offsets_deg(ra_deg: np.ndarray, dec_deg: np.ndarray,
                              center_ra_deg: float, center_dec_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Project onto a local tangent plane centered on (center_ra, center_dec)
    - same convention as OBSERVE's own grid_generator.py._project_tangential
    (RA offset scaled by cos(dec), wrap-safe via a signed-shortest-path
    RA difference). Appropriate for the modest (few-degree) fields V1
    targets - not a substitute for real WCS at larger extents (section 16).
    """
    ra_deg = np.asarray(ra_deg, dtype=float)
    dec_deg = np.asarray(dec_deg, dtype=float)
    # shortest signed RA difference, correct across the 0/360 wrap (section 13)
    d_ra = (ra_deg - center_ra_deg + 180.0) % 360.0 - 180.0
    cos_dec = np.cos(np.radians(center_dec_deg))
    x = d_ra * cos_dec
    y = dec_deg - center_dec_deg
    return x, y


def to_galactic(ra_deg: np.ndarray, dec_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Derived view only (section 12, 48) - never a second canonical
    storage frame. Uses astropy's real ICRS->Galactic transform."""
    c = SkyCoord(ra=np.asarray(ra_deg) * u.deg, dec=np.asarray(dec_deg) * u.deg, frame="icrs")
    gal = c.galactic
    return gal.l.to_value(u.deg), gal.b.to_value(u.deg)
