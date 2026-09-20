"""Comprehensive geometric correctness matrix for
SolarTarget.resolve_offset() (Fase 5 of the solar-fix pass): every offset
must land at exactly the requested angular separation from the Sun's TRUE
apparent position, in the requested direction - verified two ways that do
NOT reuse SkyOffsetFrame internally, so a shared-bug false pass (the
failure mode that let the finite-distance `.icrs` bug go undetected for so
long - see test_alignment_engine_cirs_icrs_regression.py) is not possible
here:

  1. separation(): astropy's general-purpose great-circle distance, not
     SkyOffsetFrame's own internal geometry.
  2. position_angle(): astropy's general-purpose spherical bearing, used
     to confirm EAST offsets bear ~90 deg (due east) and NORTH offsets
     bear ~0 deg (due north) from the true center - a property that would
     be violated if resolve_offset() pointed the wrong direction even
     while getting the magnitude right.
"""
import astropy.units as u
import pytest
from astropy.coordinates import EarthLocation
from astropy.time import Time

from alignment_engine.targets.solar import SolarTarget

LOCATIONS = [
    EarthLocation(lat=-33.4489 * u.deg, lon=-70.6693 * u.deg, height=570 * u.m),  # Santiago-ish
    EarthLocation(lat=51.5074 * u.deg, lon=-0.1278 * u.deg, height=35 * u.m),      # London-ish (northern hemisphere)
    EarthLocation(lat=0.0 * u.deg, lon=0.0 * u.deg, height=0 * u.m),               # equator/prime meridian
]

# A spread of dates/times across the year (different Sun RA/Dec, including
# near the RA=0h/24h wrap around the March equinox) and different times of
# day (different LST).
OBSTIMES = [
    Time("2026-01-15T03:00:00"),
    Time("2026-03-20T09:00:00"),   # near equinox - Sun RA close to 0h/24h wrap
    Time("2026-03-20T21:00:00"),
    Time("2026-06-21T15:00:00"),   # solstice
    Time("2026-09-18T12:00:00"),
    Time("2026-12-21T18:00:00"),
]


def _true_center(location, obstime):
    return SolarTarget(location).apparent_icrs_direction(obstime)


@pytest.mark.parametrize("location", LOCATIONS)
@pytest.mark.parametrize("obstime", OBSTIMES)
def test_zero_offset_lands_exactly_on_the_sun(location, obstime):
    target = SolarTarget(location)
    center = _true_center(location, obstime)
    result = target.resolve_offset(0.0, 0.0, obstime)
    assert center.separation(result).deg < 1e-6


@pytest.mark.parametrize("east_deg,north_deg", [
    (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
    (5.0, 0.0), (-5.0, 0.0), (0.0, 5.0), (0.0, -5.0),
    (10.0, 0.0), (0.0, 10.0),
    (5.0, 5.0), (-5.0, 5.0), (5.0, -5.0), (-5.0, -5.0),  # diagonals
])
@pytest.mark.parametrize("obstime", OBSTIMES)
def test_offset_magnitude_matches_requested_angular_separation(east_deg, north_deg, obstime):
    location = LOCATIONS[0]
    target = SolarTarget(location)
    center = _true_center(location, obstime)
    result = target.resolve_offset(east_deg, north_deg, obstime)

    expected_deg = (east_deg ** 2 + north_deg ** 2) ** 0.5
    actual_deg = center.separation(result).deg
    # SkyOffsetFrame's lon/lat are an exact spherical rotation, not a
    # small-angle approximation, so for a diagonal offset the true
    # separation is not exactly sqrt(e^2+n^2) but very close for these
    # magnitudes (<= ~14 deg diagonal) - loose enough to not be a
    # small-angle-approximation test, tight enough to catch a real
    # magnitude bug (tens of degrees, or even ~10%).
    assert actual_deg == pytest.approx(expected_deg, abs=0.05)


@pytest.mark.parametrize("obstime", OBSTIMES)
def test_east_offset_bears_due_east_from_center(obstime):
    location = LOCATIONS[0]
    target = SolarTarget(location)
    center = _true_center(location, obstime)
    east_point = target.resolve_offset(3.0, 0.0, obstime)
    bearing = center.position_angle(east_point).deg  # 0=North, 90=East, 180=South, 270=West
    assert bearing == pytest.approx(90.0, abs=0.5)


@pytest.mark.parametrize("obstime", OBSTIMES)
def test_west_offset_bears_due_west_from_center(obstime):
    location = LOCATIONS[0]
    target = SolarTarget(location)
    center = _true_center(location, obstime)
    west_point = target.resolve_offset(-3.0, 0.0, obstime)
    bearing = center.position_angle(west_point).deg
    assert bearing == pytest.approx(270.0, abs=0.5)


@pytest.mark.parametrize("obstime", OBSTIMES)
def test_north_offset_bears_due_north_from_center(obstime):
    location = LOCATIONS[0]
    target = SolarTarget(location)
    center = _true_center(location, obstime)
    north_point = target.resolve_offset(0.0, 3.0, obstime)
    bearing = center.position_angle(north_point).deg
    assert bearing == pytest.approx(0.0, abs=0.5)


@pytest.mark.parametrize("obstime", OBSTIMES)
def test_south_offset_bears_due_south_from_center(obstime):
    location = LOCATIONS[0]
    target = SolarTarget(location)
    center = _true_center(location, obstime)
    south_point = target.resolve_offset(0.0, -3.0, obstime)
    bearing = center.position_angle(south_point).deg
    assert bearing == pytest.approx(180.0, abs=0.5)


@pytest.mark.parametrize("obstime", OBSTIMES)
def test_northeast_diagonal_bears_between_north_and_east(obstime):
    location = LOCATIONS[0]
    target = SolarTarget(location)
    center = _true_center(location, obstime)
    point = target.resolve_offset(5.0, 5.0, obstime)
    bearing = center.position_angle(point).deg
    assert 0.0 < bearing < 90.0


def test_near_equinox_ra_wrap_does_not_break_geometry():
    """Around the March equinox the Sun's RA crosses 0h/24h - resolve_offset
    must not produce a discontinuity or wrong-magnitude result right at
    that boundary."""
    location = LOCATIONS[0]
    target = SolarTarget(location)
    for obstime in (Time("2026-03-19T12:00:00"), Time("2026-03-20T12:00:00"), Time("2026-03-21T12:00:00")):
        center = _true_center(location, obstime)
        for east_deg, north_deg in [(2.0, 0.0), (-2.0, 0.0), (0.0, 2.0), (0.0, -2.0)]:
            result = target.resolve_offset(east_deg, north_deg, obstime)
            expected = (east_deg ** 2 + north_deg ** 2) ** 0.5
            assert center.separation(result).deg == pytest.approx(expected, abs=0.05)


def test_ten_degree_offset_still_matches_independent_separation():
    location = LOCATIONS[0]
    obstime = OBSTIMES[0]
    target = SolarTarget(location)
    center = _true_center(location, obstime)
    result = target.resolve_offset(10.0, 0.0, obstime)
    assert center.separation(result).deg == pytest.approx(10.0, abs=0.05)
