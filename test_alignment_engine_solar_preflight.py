"""Item 12: the solar GOTO preflight gate must BLOCK unless the Sun is (and
stays) within operational altitude limits for the whole intended test
window - never a fake/silent PASS, and no override anywhere in this module.
"""
import pytest
from astropy.time import Time

from alignment_engine.solar_preflight import (
    DEFAULT_MAX_ALTITUDE_DEG,
    DEFAULT_MIN_SOLAR_ALTITUDE_DEG,
    check_solar_preflight,
)

T0 = Time("2026-09-18T12:00:00")


class _ConstantProvider:
    """altitude/azimuth as a function of elapsed seconds since T0 - lets
    tests control the whole trajectory deterministically, independent of
    the real Sun's actual position (which would make these tests flaky and
    time-dependent)."""

    def __init__(self, altitude_fn, azimuth_fn=lambda t: 180.0):
        self._altitude_fn = altitude_fn
        self._azimuth_fn = azimuth_fn

    def _elapsed(self, obstime: Time) -> float:
        return (obstime - T0).sec

    def altitude_deg(self, obstime: Time) -> float:
        return self._altitude_fn(self._elapsed(obstime))

    def azimuth_deg(self, obstime: Time) -> float:
        return self._azimuth_fn(self._elapsed(obstime))


def test_sun_below_horizon_is_blocked():
    provider = _ConstantProvider(lambda t: -10.0)
    result = check_solar_preflight(provider, T0, window_seconds=600.0)
    assert result.verdict == "BLOCKED"
    assert "below operational altitude" in result.reason
    assert "horizon" in result.reason


def test_sun_above_horizon_but_below_floor_is_blocked():
    provider = _ConstantProvider(lambda t: 5.0)  # above horizon, below default 20 deg floor
    result = check_solar_preflight(provider, T0, window_seconds=600.0)
    assert result.verdict == "BLOCKED"
    assert "below operational altitude" in result.reason
    assert "floor" in result.reason


def test_valid_now_but_drops_below_floor_before_window_ends_is_blocked():
    # Starts comfortably high, then descends below the floor partway
    # through the window - must be caught even though sample 0 is fine.
    def altitude(t):
        return 40.0 - (t / 600.0) * 30.0  # 40 deg at t=0 -> 10 deg at t=600

    provider = _ConstantProvider(altitude)
    result = check_solar_preflight(provider, T0, window_seconds=600.0, sample_count=7)
    assert result.verdict == "BLOCKED"
    assert "insufficient visibility window" in result.reason


def test_valid_for_the_entire_window_is_pass():
    provider = _ConstantProvider(lambda t: 45.0 - (t / 600.0) * 5.0)  # 45 -> 40 deg, always inside limits
    result = check_solar_preflight(provider, T0, window_seconds=600.0)
    assert result.verdict == "PASS"
    assert result.altitude_now_deg == pytest.approx(45.0)
    assert result.altitude_end_deg == pytest.approx(40.0)


def test_above_mount_max_altitude_is_blocked():
    provider = _ConstantProvider(lambda t: 95.0)
    result = check_solar_preflight(provider, T0, window_seconds=600.0, max_altitude_deg=90.0)
    assert result.verdict == "BLOCKED"
    assert "outside mount limits" in result.reason


def test_rises_above_mount_max_before_window_ends_is_blocked():
    def altitude(t):
        return 85.0 + (t / 600.0) * 10.0  # 85 -> 95, crosses the 90 deg ceiling

    provider = _ConstantProvider(altitude)
    result = check_solar_preflight(provider, T0, window_seconds=600.0, max_altitude_deg=90.0, sample_count=7)
    assert result.verdict == "BLOCKED"
    assert "insufficient visibility window" in result.reason


def test_azimuth_wraps_into_0_360_range():
    provider = _ConstantProvider(lambda t: 45.0, azimuth_fn=lambda t: -30.0)  # raw -30 deg
    result = check_solar_preflight(provider, T0, window_seconds=600.0)
    assert 0.0 <= result.azimuth_now_deg < 360.0
    assert result.azimuth_now_deg == pytest.approx(330.0)


def test_azimuth_over_360_also_wraps():
    provider = _ConstantProvider(lambda t: 45.0, azimuth_fn=lambda t: 370.0)
    result = check_solar_preflight(provider, T0, window_seconds=600.0)
    assert result.azimuth_now_deg == pytest.approx(10.0)


def test_no_override_parameter_exists():
    import inspect

    sig = inspect.signature(check_solar_preflight)
    for name in sig.parameters:
        assert "override" not in name.lower()
        assert "force" not in name.lower()
        assert "bypass" not in name.lower()


def test_defaults_match_the_rest_of_the_package():
    assert DEFAULT_MIN_SOLAR_ALTITUDE_DEG == 20.0  # matches GlobalConfig.altitude_floor_deg
    assert DEFAULT_MAX_ALTITUDE_DEG == 90.0


def test_sample_count_must_cover_both_ends():
    provider = _ConstantProvider(lambda t: 45.0)
    with pytest.raises(ValueError):
        check_solar_preflight(provider, T0, window_seconds=600.0, sample_count=1)


def test_negative_window_rejected():
    provider = _ConstantProvider(lambda t: 45.0)
    with pytest.raises(ValueError):
        check_solar_preflight(provider, T0, window_seconds=-1.0)


def test_result_to_dict_is_json_serializable():
    import json

    provider = _ConstantProvider(lambda t: 45.0)
    result = check_solar_preflight(provider, T0, window_seconds=600.0)
    json.dumps(result.to_dict())  # must not raise
