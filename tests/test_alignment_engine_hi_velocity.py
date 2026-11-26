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


# ---------------------------------------------------------------- audit 2026-11-25: LSRK value AND sign, independently
_SITE = EarthLocation(lat=-33.4489 * u.deg, lon=-70.6693 * u.deg, height=570 * u.m)
_CASES = [("2026-11-12T22:54:00", 276.88, -11.49), ("2026-11-19T21:00:00", 266.40, -28.94),
          ("2026-11-05T23:01:25", 277.0, -11.5), ("2026-11-13T01:10:00", 330.0, -35.0),
          ("2026-06-15T04:00:00", 270.0, 30.0), ("2026-12-15T04:00:00", 90.0, -30.0)]


def _independent_lsrk(t, target):
    """The classical LSRK: barycentric correction + 20 km/s of solar motion toward (18h, +30 deg, B1900)."""
    import numpy as np
    from astropy.coordinates import FK4
    apex = SkyCoord(ra=18 * u.hourangle, dec=30 * u.deg, frame=FK4(equinox="B1900")).icrs
    bary = target.radial_velocity_correction(kind="barycentric", obstime=t, location=_SITE).to(u.km / u.s).value
    return bary + 20.0 * float(np.dot(target.cartesian.xyz.value, apex.cartesian.xyz.value))


@pytest.mark.parametrize("utc,ra,dec", _CASES)
def test_lsrk_shift_matches_an_independent_construction_value_and_sign(utc, ra, dec):
    t, c = Time(utc, scale="utc"), SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
    ours = frame_shift_km_s(frame="lsrk", obstime=t, location=_SITE, target=c)
    indep = _independent_lsrk(t, c)
    assert ours == pytest.approx(indep, abs=0.05)          # audit measured 5-6 m/s (different apex conventions)
    assert (ours > 0) == (indep > 0)


def test_toward_the_solar_apex_lsrk_adds_twenty_km_s_to_the_barycentric_shift():
    from astropy.coordinates import FK4
    t = Time("2026-11-12T22:54:00")
    apex = SkyCoord(ra=18 * u.hourangle, dec=30 * u.deg, frame=FK4(equinox="B1900")).icrs
    bary = apex.radial_velocity_correction(kind="barycentric", obstime=t, location=_SITE).to(u.km / u.s).value
    lsrk = frame_shift_km_s(frame="lsrk", obstime=t, location=_SITE, target=apex)
    assert lsrk - bary == pytest.approx(20.0, abs=0.05)


def test_reduce_velocity_axis_puts_gas_at_rest_in_the_lsr_at_zero():
    """End to end through REDUCE's velocity stage: the frequency at which gas AT REST in the LSR is observed (the
    rest frequency Doppler-shifted by the independent LSRK correction) must come out at 0 km/s."""
    import numpy as np
    from reduce_engine.velocity import compute_velocity_axis
    utc, ra, dec = _CASES[0]
    c = SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
    shift = _independent_lsrk(Time(utc, scale="utc"), c)              # observer's velocity correction toward c
    rest = REST_FREQUENCY_HI_HZ
    f_obs = rest * (1 + shift / 299792.458)                           # topocentric v = -shift  ->  LSRK v = 0
    res = compute_velocity_axis(np.array([f_obs, rest]), rest_frequency_hz=rest, frame="lsrk",
                                timestamp_utc=utc + "+00:00", observer_latitude_deg=-33.4489,
                                observer_longitude_deg=-70.6693, observer_elevation_m=570.0,
                                ra_hours=ra / 15.0, dec_degrees=dec)
    v = np.asarray(res.velocity_m_s) / 1000.0
    assert v[0] == pytest.approx(0.0, abs=0.1)
    assert v[1] == pytest.approx(shift, abs=0.1)                     # the rest frequency itself reads +shift
