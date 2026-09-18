"""Dynamic Sun target (Fase 2: "la grilla NO debe quedar congelada").

Reuses alignment.py's sun_eod() (the one place in the repo that already
documents and performs the ICRS Sun -> CIRS(EOD) conversion). Every point's
absolute sky coordinate is resolved fresh, at the instant it is about to be
used, from the current Sun position plus a fixed tangent-plane offset - a
raster generated once at the start and never re-resolved would silently
drift off the real Sun over a multi-minute scan.

Real bug found while testing this module (verified independently against
alignment.py's own multiscale_pattern(), not something introduced here):
astropy's SkyOffsetFrame computes badly wrong local tangent-plane geometry
when its `origin` is a CIRS-frame SkyCoord (as sun_eod() returns) - a
requested 5deg offset came out ~80deg away. Converting the origin to ICRS
first (`sun_eod(obstime).icrs`) fixes it completely and was confirmed by
direct comparison. All tangent-plane/offset geometry in this package is
therefore done in ICRS; CIRS(EOD) is used only for the one place it is
actually required - the real mount GOTO command itself
(mount_adapter.RealMountAdapter converts at the moment of sending, not
here). The ICRS/CIRS difference for the Sun (~20 arcsec, aberration-order)
is utterly negligible next to a ~20deg beam, so this substitution costs no
real accuracy.
"""
from __future__ import annotations

from dataclasses import dataclass

from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time

from alignment import sun_eod, offset_coordinates


@dataclass
class SolarTarget:
    location: EarthLocation

    def current_position(self, obstime: Time) -> SkyCoord:
        """The Sun's real position right now, in the same CIRS(EOD) frame
        the mount is commanded in - not a snapshot taken once at plan time."""
        return sun_eod(obstime)

    def resolve_offset(self, east_deg: float, north_deg: float, obstime: Time) -> SkyCoord:
        """Absolute sky coordinate for one raster point: current Sun
        position + this point's fixed tangent-plane offset, evaluated at
        `obstime` - call this again with a fresh Time for every point.
        Offset geometry is computed in ICRS (see module docstring) even
        though current_position() itself returns CIRS(EOD)."""
        center_icrs = self.current_position(obstime).icrs
        return offset_coordinates(center_icrs, [east_deg], [north_deg])[0]

    def altitude_deg(self, obstime: Time) -> float:
        position = self.current_position(obstime)
        return float(position.transform_to(AltAz(obstime=obstime, location=self.location)).alt.deg)

    def azimuth_deg(self, obstime: Time) -> float:
        """Additive, not used by altitude_deg() above (kept untouched to
        avoid any regression risk to its existing callers/tests) - needed
        by solar_preflight.py's GOTO gate, which cares about az as well as
        alt for its before/after-window sampling."""
        position = self.current_position(obstime)
        return float(position.transform_to(AltAz(obstime=obstime, location=self.location)).az.deg) % 360.0
