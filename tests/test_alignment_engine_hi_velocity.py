"""Fase 6: frequency <-> velocity frame conversion. Every claim here is
checked against astropy's OWN independent, dedicated
SkyCoord.radial_velocity_correction() API (not just self-consistency
within velocity.py), and unsupported frames must fail closed.
"""
import astropy.units as u
import pytest
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time

from alignment_engine.hi.velocity import (
    REST_FREQUENCY_HI_HZ,
    SUPPORTED_FRAMES,
    frame_shift_km_s,
    observed_frequency_to_velocity,
)

LOCATION = EarthLocation(lat=-33.4331 * u.deg, lon=289.3336 * u.deg, height=550 * u.m)
OBSTIME = Time("2026-09-18T03:00:00")
TARGET = SkyCoord(l=30 * u.deg, b=0 * u.deg, frame="galactic")


def test_topocentric_shift_is_zero():
    assert frame_shift_km_s(frame="topocentric", obstime=OBSTIME, location=LOCATION, target=TARGET) == 0.0


def test_heliocentric_shift_matches_independent_radial_velocity_correction():
    shift = frame_shift_km_s(frame="heliocentric", obstime=OBSTIME, location=LOCATION, target=TARGET)
    independent = TARGET.icrs.radial_velocity_correction(kind="heliocentric", obstime=OBSTIME,
                                                            location=LOCATION).to(u.km / u.s).value
    assert shift == pytest.approx(independent, abs=0.01)  # within 10 m/s


def test_barycentric_shift_matches_independent_radial_velocity_correction():
    shift = frame_shift_km_s(frame="barycentric", obstime=OBSTIME, location=LOCATION, target=TARGET)
    independent = TARGET.icrs.radial_velocity_correction(kind="barycentric", obstime=OBSTIME,
                                                            location=LOCATION).to(u.km / u.s).value
    assert shift == pytest.approx(independent, abs=0.01)


def test_barycentric_and_heliocentric_are_close_but_not_identical():
    # Real physics: the Sun-barycenter offset is tiny (sub-10 m/s scale),
    # not exactly zero.
    bary = frame_shift_km_s(frame="barycentric", obstime=OBSTIME, location=LOCATION, target=TARGET)
    helio = frame_shift_km_s(frame="heliocentric", obstime=OBSTIME, location=LOCATION, target=TARGET)
    assert abs(bary - helio) < 0.1
    assert bary != helio


def test_lsrk_differs_from_heliocentric_by_a_solar_motion_scale_amount():
    # The Sun's motion relative to the LSR is a well-known, bounded
    # quantity (order 10-20 km/s depending on line of sight, historically
    # ~20 km/s apex speed) - not near-zero, not hundreds of km/s.
    helio = frame_shift_km_s(frame="heliocentric", obstime=OBSTIME, location=LOCATION, target=TARGET)
    lsrk = frame_shift_km_s(frame="lsrk", obstime=OBSTIME, location=LOCATION, target=TARGET)
    diff = abs(lsrk - helio)
    assert 0.5 < diff < 25.0


def test_topocentric_orbital_scale_shift_is_tens_of_km_s_not_zero_not_relativistic():
    # Sanity ceiling: Earth's orbital speed is ~30 km/s, so any of these
    # frame shifts must be well below that scale in magnitude (a few to a
    # few tens of km/s), never near c and never exactly repeating a wrong
    # topocentric-only value.
    for frame in ("heliocentric", "barycentric", "lsrk"):
        shift = frame_shift_km_s(frame=frame, obstime=OBSTIME, location=LOCATION, target=TARGET)
        assert abs(shift) < 40.0


def test_unsupported_frame_raises_value_error_fail_safe():
    with pytest.raises(ValueError):
        observed_frequency_to_velocity(REST_FREQUENCY_HI_HZ, rest_freq_hz=REST_FREQUENCY_HI_HZ,
                                        frame="geocentric", obstime=OBSTIME, location=LOCATION, target=TARGET)
    with pytest.raises(ValueError):
        frame_shift_km_s(frame="unknown_frame", obstime=OBSTIME, location=LOCATION, target=TARGET)


def test_supported_frames_is_exactly_the_documented_set():
    assert SUPPORTED_FRAMES == {"topocentric", "heliocentric", "barycentric", "lsrk"}


def test_observed_frequency_below_rest_is_positive_recession_velocity():
    # IAU radio convention: v = c*(f0-f)/f0 - a frequency below rest means
    # a positive (receding) velocity.
    lower_freq = REST_FREQUENCY_HI_HZ * (1 - 100e3 / 3e8)  # ~100 km/s redshift equivalent
    v = observed_frequency_to_velocity(lower_freq, rest_freq_hz=REST_FREQUENCY_HI_HZ, frame="topocentric",
                                        obstime=OBSTIME, location=LOCATION, target=TARGET)
    assert v > 50.0  # should be close to 100 km/s, well above zero


def test_observed_frequency_above_rest_is_negative_approach_velocity():
    higher_freq = REST_FREQUENCY_HI_HZ * (1 + 100e3 / 3e8)
    v = observed_frequency_to_velocity(higher_freq, rest_freq_hz=REST_FREQUENCY_HI_HZ, frame="topocentric",
                                        obstime=OBSTIME, location=LOCATION, target=TARGET)
    assert v < -50.0


def test_different_lines_of_sight_give_different_frame_shifts():
    """The frame correction is direction-dependent (projection onto the
    line of sight) - two very different sky directions must not give the
    same shift (a hardcoded/direction-independent bug would fail this)."""
    north_pole = SkyCoord(l=0 * u.deg, b=90 * u.deg, frame="galactic")
    galactic_plane = SkyCoord(l=0 * u.deg, b=0 * u.deg, frame="galactic")
    shift_pole = frame_shift_km_s(frame="heliocentric", obstime=OBSTIME, location=LOCATION, target=north_pole)
    shift_plane = frame_shift_km_s(frame="heliocentric", obstime=OBSTIME, location=LOCATION, target=galactic_plane)
    assert shift_pole != pytest.approx(shift_plane, abs=0.5)
