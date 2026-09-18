"""Motion-detection pass, item 8: physical (Hour-Angle-based) motion
detector must not repeat the naive-RA/DEC-delta false positive found on the
first real hardware test (data/alignment/HW-TRACKING-20260918-004605/) and
must not hide real motion either.
"""
from datetime import datetime, timedelta, timezone

import pytest

from alignment_engine.motion_detector import (
    DEFAULT_MOTION_TOLERANCE_ARCSEC,
    RA_QUANTIZATION_ARCSEC,
    SIDEREAL_RATE_RATIO,
    _angular_separation_arcsec,
    _wrap_hours_signed,
    classify_motion,
    track_state_from_switch_vector,
)

SITE_LAT = -33.4331
SITE_LON = 289.3336
T0 = datetime(2026, 9, 18, 0, 46, 6, tzinfo=timezone.utc)


def _ra_after_sidereal_drift(ra_before_hours: float, elapsed_s: float) -> float:
    """The RA an idle (TRACK_STATE=OFF) mount will report after `elapsed_s`
    real seconds, per the mechanism this module itself asserts (HA fixed,
    RA = LST - HA increases with LST at the sidereal rate)."""
    return ra_before_hours + elapsed_s * SIDEREAL_RATE_RATIO / 3600.0


def test_track_off_axes_still_ra_advances_with_lst_is_pass():
    elapsed = 3.786353
    ra_before = 13.373888888888889
    dec = -84.25972222222222
    ra_after = _ra_after_sidereal_drift(ra_before, elapsed)
    result = classify_motion(
        ra_before_hours=ra_before, dec_before_deg=dec,
        ra_after_hours=ra_after, dec_after_deg=dec,
        ts_before_utc=T0, ts_after_utc=T0 + timedelta(seconds=elapsed),
        track_state_before="OFF", track_state_after="OFF",
        site_lat_deg=SITE_LAT, site_lon_deg=SITE_LON,
    )
    assert result.verdict == "PASS"
    assert result.angular_separation_arcsec < 1.0


def test_real_ha_change_is_fail():
    # A deliberate +2 arcmin hour-angle shift AT THE EQUATOR (dec=0, so the
    # cos(dec) factor is 1 and HA-arcmin maps 1:1 to true angular
    # separation) - well above tolerance and well above the 1"-quantization
    # the driver's coarse RA reporting could plausibly explain.
    elapsed = 3.786353
    ra_before = 13.373888888888889
    dec = 0.0
    ra_expected_if_idle = _ra_after_sidereal_drift(ra_before, elapsed)
    ra_after = ra_expected_if_idle - (2.0 / 60.0) / 15.0  # -2 arcmin of HA -> RA moves the other way
    result = classify_motion(
        ra_before_hours=ra_before, dec_before_deg=dec,
        ra_after_hours=ra_after, dec_after_deg=dec,
        ts_before_utc=T0, ts_after_utc=T0 + timedelta(seconds=elapsed),
        track_state_before="OFF", track_state_after="OFF",
        site_lat_deg=SITE_LAT, site_lon_deg=SITE_LON,
    )
    assert result.verdict == "FAIL"


def test_real_ha_change_near_pole_is_attenuated_by_cos_dec_but_a_big_enough_shift_still_fails():
    # Confirms the near-pole shrinkage is real (see the dedicated cos(dec)
    # test below) AND that the detector still catches a big enough real
    # motion even there - a 60 arcmin (1 deg) HA shift at dec=-84.26 deg
    # still exceeds tolerance once cos(dec) is applied (1 deg * cos(84.26deg)
    # = 1 deg * 0.098 = ~5.9 arcmin, still >> 30 arcsec).
    elapsed = 3.786353
    ra_before = 13.373888888888889
    dec = -84.25972222222222
    ra_expected_if_idle = _ra_after_sidereal_drift(ra_before, elapsed)
    ra_after = ra_expected_if_idle - 1.0 / 15.0  # -1 deg of HA
    result = classify_motion(
        ra_before_hours=ra_before, dec_before_deg=dec,
        ra_after_hours=ra_after, dec_after_deg=dec,
        ts_before_utc=T0, ts_after_utc=T0 + timedelta(seconds=elapsed),
        track_state_before="OFF", track_state_after="OFF",
        site_lat_deg=SITE_LAT, site_lon_deg=SITE_LON,
    )
    assert result.verdict == "FAIL"


def test_real_dec_change_is_fail():
    elapsed = 3.786353
    ra_before = 13.373888888888889
    dec_before = -84.25972222222222
    ra_after = _ra_after_sidereal_drift(ra_before, elapsed)
    dec_after = dec_before + 0.1  # 6 arcmin - a real slew, not jitter
    result = classify_motion(
        ra_before_hours=ra_before, dec_before_deg=dec_before,
        ra_after_hours=ra_after, dec_after_deg=dec_after,
        ts_before_utc=T0, ts_after_utc=T0 + timedelta(seconds=elapsed),
        track_state_before="OFF", track_state_after="OFF",
        site_lat_deg=SITE_LAT, site_lon_deg=SITE_LON,
    )
    assert result.verdict == "FAIL"


def test_wrap_ra_across_24h_boundary():
    elapsed = 2.0
    ra_before = 23.9998
    dec = -10.0
    ra_after = _ra_after_sidereal_drift(ra_before, elapsed) % 24.0  # wraps past 24h -> near 0h
    assert ra_after < ra_before  # sanity: the naive (unwrapped) delta would look huge and negative
    result = classify_motion(
        ra_before_hours=ra_before, dec_before_deg=dec,
        ra_after_hours=ra_after, dec_after_deg=dec,
        ts_before_utc=T0, ts_after_utc=T0 + timedelta(seconds=elapsed),
        track_state_before="OFF", track_state_after="OFF",
        site_lat_deg=SITE_LAT, site_lon_deg=SITE_LON,
    )
    assert result.verdict == "PASS"
    assert result.angular_separation_arcsec < 1.0


def test_wrap_hours_signed_handles_ha_boundary():
    assert _wrap_hours_signed(11.9999) == pytest.approx(11.9999)
    assert _wrap_hours_signed(12.0001) == pytest.approx(-11.9999)
    assert _wrap_hours_signed(-12.0001) == pytest.approx(11.9999)
    assert _wrap_hours_signed(23.9) == pytest.approx(-0.1)


def test_dec_near_pole_true_angular_separation_shrinks_with_cos_dec():
    # Same |delta_HA| at the equator vs near the pole: the exact spherical
    # separation must be far smaller near the pole (cos(dec) factor), not
    # naively equal.
    equator_sep = _angular_separation_arcsec(0.0, 0.0, 0.01, 0.0)
    polar_sep = _angular_separation_arcsec(0.0, 89.9, 0.01, 89.9)
    assert polar_sep < equator_sep * 0.05


def test_dec_near_pole_does_not_blow_up_or_return_nan():
    result = classify_motion(
        ra_before_hours=13.373888888888889, dec_before_deg=-89.999,
        ra_after_hours=13.375, dec_after_deg=-89.999,
        ts_before_utc=T0, ts_after_utc=T0 + timedelta(seconds=3.786353),
        track_state_before="OFF", track_state_after="OFF",
        site_lat_deg=SITE_LAT, site_lon_deg=SITE_LON,
    )
    assert result.angular_separation_arcsec is not None
    assert result.angular_separation_arcsec == result.angular_separation_arcsec  # not NaN
    assert result.verdict in ("PASS", "FAIL")


def test_one_ra_second_quantization_alone_is_within_tolerance():
    # Exactly the real observed case: 4 RA-seconds of raw RA change over
    # ~3.79s at DEC ~ -84.26 deg - this is the scenario that produced the
    # original false FAIL.
    elapsed = 3.786353
    ra_before = 13.373888888888889
    dec = -84.25972222222222
    ra_after = 13.375  # rounds to the nearest whole RA-second, per real driver behavior
    result = classify_motion(
        ra_before_hours=ra_before, dec_before_deg=dec,
        ra_after_hours=ra_after, dec_after_deg=dec,
        ts_before_utc=T0, ts_after_utc=T0 + timedelta(seconds=elapsed),
        track_state_before="OFF", track_state_after="OFF",
        site_lat_deg=SITE_LAT, site_lon_deg=SITE_LON,
    )
    assert result.verdict == "PASS"
    assert result.tolerance_arcsec == pytest.approx(2 * RA_QUANTIZATION_ARCSEC)


@pytest.mark.parametrize("missing_field", ["ra_before_hours", "ts_after_utc", "site_lat_deg"])
def test_missing_data_is_inconclusive_never_a_false_pass_or_fail(missing_field):
    kwargs = dict(
        ra_before_hours=13.0, dec_before_deg=-80.0, ra_after_hours=13.001, dec_after_deg=-80.0,
        ts_before_utc=T0, ts_after_utc=T0 + timedelta(seconds=3.0),
        track_state_before="OFF", track_state_after="OFF",
        site_lat_deg=SITE_LAT, site_lon_deg=SITE_LON,
    )
    kwargs[missing_field] = None
    result = classify_motion(**kwargs)
    assert result.verdict == "INCONCLUSIVE"


def test_track_state_on_is_inconclusive_not_a_guessed_pass():
    result = classify_motion(
        ra_before_hours=13.0, dec_before_deg=-80.0, ra_after_hours=13.0, dec_after_deg=-80.0,
        ts_before_utc=T0, ts_after_utc=T0 + timedelta(seconds=3.0),
        track_state_before="ON", track_state_after="ON",
        site_lat_deg=SITE_LAT, site_lon_deg=SITE_LON,
    )
    assert result.verdict == "INCONCLUSIVE"
    # numbers are still computed and reported, just not turned into a verdict
    assert result.ha_before_hours is not None
    assert result.ha_after_hours is not None


def test_track_state_changed_mid_test_is_inconclusive():
    result = classify_motion(
        ra_before_hours=13.0, dec_before_deg=-80.0, ra_after_hours=13.0, dec_after_deg=-80.0,
        ts_before_utc=T0, ts_after_utc=T0 + timedelta(seconds=3.0),
        track_state_before="OFF", track_state_after="ON",
        site_lat_deg=SITE_LAT, site_lon_deg=SITE_LON,
    )
    assert result.verdict == "INCONCLUSIVE"


def test_track_state_from_switch_vector():
    assert track_state_from_switch_vector({"TRACK_ON": "Off", "TRACK_OFF": "On"}) == "OFF"
    assert track_state_from_switch_vector({"TRACK_ON": "On", "TRACK_OFF": "Off"}) == "ON"
    assert track_state_from_switch_vector(None) is None
    assert track_state_from_switch_vector({}) is None
    assert track_state_from_switch_vector({"TRACK_ON": "Off", "TRACK_OFF": "Off"}) is None  # ambiguous - never guessed
    assert track_state_from_switch_vector({"TRACK_ON": "On", "TRACK_OFF": "On"}) is None  # ambiguous - never guessed


def test_default_tolerance_is_derived_from_quantization_not_arbitrary():
    assert DEFAULT_MOTION_TOLERANCE_ARCSEC == pytest.approx(2 * RA_QUANTIZATION_ARCSEC)
    assert RA_QUANTIZATION_ARCSEC == pytest.approx(15.0)  # 1 RA-second = 15 arcsec, by definition


def test_regression_reproduces_the_real_hw_20260918_session_as_pass():
    """The exact real numbers from data/alignment/HW-TRACKING-20260918-004605/
    (precheck.json/postcheck.json) - the session whose naive-threshold
    verdict this pass's motion detector was built to correct."""
    result = classify_motion(
        ra_before_hours=13.373888888888889, dec_before_deg=-84.25972222222222,
        ra_after_hours=13.375, dec_after_deg=-84.25972222222222,
        ts_before_utc=datetime.fromisoformat("2026-09-18T00:46:06.436684+00:00"),
        ts_after_utc=datetime.fromisoformat("2026-09-18T00:46:10.223037+00:00"),
        track_state_before="OFF", track_state_after="OFF",
        site_lat_deg=-33.433055555555554861, site_lon_deg=289.33361111111111086,
    )
    assert result.verdict == "PASS"
    assert result.angular_separation_arcsec < 1.0
