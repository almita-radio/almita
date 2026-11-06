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


def sky_offsets_deg(ra_deg: np.ndarray, dec_deg: np.ndarray,
                    center_ra_deg: float, center_dec_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Exact spherical offset coordinates about (center_ra, center_dec): the longitude/latitude of each point in
    a frame whose origin is the centre - astropy's SkyOffsetFrame(origin=centre, rotation=0), the frame OBSERVE's
    grid_generator.build_spherical_grid lays every grid out in. x grows to the east, y to the north at the centre.
    Valid over the whole sphere: a field that crosses a pole or the RA wrap stays one smooth chart (2026-11-05:
    a 40 deg grid around the south pole was refused by the old linear dRA*cos(dec0) approximation). For a few-
    degree field it equals dRA*cos(dec0), dDec to second order in the offset."""
    ra = np.radians(np.asarray(ra_deg, dtype=float))
    dec = np.radians(np.asarray(dec_deg, dtype=float))
    a0, d0 = np.radians(center_ra_deg), np.radians(center_dec_deg)
    da = ra - a0
    vx, vy, vz = np.cos(dec) * np.cos(da), np.cos(dec) * np.sin(da), np.sin(dec)
    xp = np.cos(d0) * vx + np.sin(d0) * vz        # rotate the centre onto (1, 0, 0)
    zp = -np.sin(d0) * vx + np.cos(d0) * vz
    return np.degrees(np.arctan2(vy, xp)), np.degrees(np.arcsin(np.clip(zp, -1.0, 1.0)))


def offsets_to_sky_deg(x_deg: np.ndarray, y_deg: np.ndarray,
                       center_ra_deg: float, center_dec_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of sky_offsets_deg: (ra_deg in [0, 360), dec_deg) of offset coordinates about the centre."""
    lon, lat = np.radians(np.asarray(x_deg, dtype=float)), np.radians(np.asarray(y_deg, dtype=float))
    a0, d0 = np.radians(center_ra_deg), np.radians(center_dec_deg)
    xp, yp, zp = np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)
    vx = np.cos(d0) * xp - np.sin(d0) * zp
    vz = np.sin(d0) * xp + np.cos(d0) * zp
    ra = np.mod(np.degrees(np.arctan2(yp, vx)) + center_ra_deg, 360.0)
    return ra, np.degrees(np.arcsin(np.clip(vz, -1.0, 1.0)))


def tangent_plane_offsets_deg(ra_deg: np.ndarray, dec_deg: np.ndarray,
                              center_ra_deg: float, center_dec_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """The map plane every SCIENCE product uses: since 2026-11-05 the exact spherical offset frame
    (sky_offsets_deg), no longer the linear dRA*cos(dec0), dDec approximation that broke on wide fields and
    near the poles. The name is kept for its callers."""
    return sky_offsets_deg(ra_deg, dec_deg, center_ra_deg, center_dec_deg)


def to_galactic(ra_deg: np.ndarray, dec_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Derived view only (section 12, 48) - never a second canonical
    storage frame. Uses astropy's real ICRS->Galactic transform."""
    c = SkyCoord(ra=np.asarray(ra_deg) * u.deg, dec=np.asarray(dec_deg) * u.deg, frame="icrs")
    gal = c.galactic
    return gal.l.to_value(u.deg), gal.b.to_value(u.deg)
