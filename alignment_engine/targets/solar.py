"""Dynamic Sun target (Fase 2: "la grilla NO debe quedar congelada").

Reuses alignment.py's sun_eod() (the one place in the repo that already
documents and performs the ICRS Sun -> CIRS(EOD) conversion). Every point's
absolute sky coordinate is resolved fresh, at the instant it is about to be
used, from the current Sun position plus a fixed tangent-plane offset - a
raster generated once at the start and never re-resolved would silently
drift off the real Sun over a multi-minute scan.

TWO DISTINCT bugs have been found in this tangent-plane-geometry path -
they must not be conflated, and both are now fixed the same way (see
apparent_icrs_direction() below):

BUG 1 (obstime inheritance, ~0.15deg at the current epoch): building
`SkyOffsetFrame(origin=some_cirs_coord)` WITHOUT also passing
`obstime=some_cirs_coord.obstime` leaves the offset frame's own obstime at
its class default (J2000.000). Since CIRS<->ICRS is itself obstime-
dependent (precession/nutation), this introduces a small, real error that
grows with how far the observation is from J2000. See
test_alignment_engine_cirs_icrs_regression.py's first three tests.

BUG 2 (finite-distance `.icrs`, found 2026-09-18, tens of degrees - the
more severe one): `sun_eod(obstime)` returns a CIRS coordinate with the
Sun's REAL physical distance attached (~1 AU). Calling `.icrs` DIRECTLY on
that coordinate is not "the same apparent direction, expressed using ICRS
axes" - astropy correctly performs a full 3D barycentric-origin
conversion, and because the Sun's true distance from the solar-system
barycenter is comparable to the scale of that origin shift, the resulting
RA/Dec (once the real distance is discarded, exactly as SkyOffsetFrame
does when it uses `origin`'s angular position to define its polar axis) is
dominated by the Sun's small, ~12-year Jupiter-driven wobble around the
barycenter - a real, correctly-computed, but entirely different and
useless quantity for "where does the Sun appear in the sky". This is NOT
an astropy bug: `.icrs` on a real-distance coordinate is a mathematically
correct 3D point conversion; the bug was this codebase's own semantic
misuse - treating that conversion's RA/Dec as a direction-only rotation.
See apparent_icrs_direction() below for the fix, and
test_alignment_engine_cirs_icrs_regression.py's newer tests for the full
reproduction (both the mechanism and this file's prior exposure to it).

All tangent-plane/offset geometry in this package is done in ICRS (via
apparent_icrs_direction(), never a raw `.icrs` on current_position()'s
output); CIRS(EOD) is used only for the one place it is actually required
- the real mount GOTO command itself (mount_adapter.RealMountAdapter
converts at the moment of sending, and hw_solar_goto_selftest.py's single
centered GOTO builds its target directly from current_position(), with no
ICRS round-trip at all - offset=(0,0) needs no tangent-plane geometry).
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
        the mount is commanded in - not a snapshot taken once at plan time.
        Carries the Sun's REAL ~1 AU distance - never call `.icrs` on this
        result directly for tangent-plane/offset geometry (see module
        docstring, BUG 2) - use apparent_icrs_direction() instead."""
        return sun_eod(obstime)

    def apparent_icrs_direction(self, obstime: Time) -> SkyCoord:
        """The SAFE way to get the Sun's apparent direction expressed with
        ICRS-oriented axes, suitable as a SkyOffsetFrame origin for
        tangent-plane geometry (see module docstring, BUG 2). The Sun's
        real distance is discarded FIRST - via
        `frame.replicate_without_data()`, which keeps only the angular
        (lon/lat) representation, not by naively subtracting a number -
        so the subsequent frame transform is a pure, small
        (aberration/frame-bias order, arcminute-scale) rotation, not a
        finite-distance 3D barycentric-origin shift."""
        apparent = self.current_position(obstime)
        direction_only = SkyCoord(ra=apparent.ra, dec=apparent.dec,
                                   frame=apparent.frame.replicate_without_data())
        return direction_only.icrs

    def resolve_offset(self, east_deg: float, north_deg: float, obstime: Time) -> SkyCoord:
        """Absolute sky coordinate for one raster point: current Sun
        position + this point's fixed tangent-plane offset, evaluated at
        `obstime` - call this again with a fresh Time for every point.
        Offset geometry is computed in ICRS via apparent_icrs_direction()
        (see module docstring, BUG 2 - never a raw `.icrs` on
        current_position()'s real-distance result)."""
        center_icrs = self.apparent_icrs_direction(obstime)
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
