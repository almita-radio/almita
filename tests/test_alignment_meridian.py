"""ALIGN and OnStep's meridian limit (BL-060 applied to ALIGN): a solar ALIGN around local noon must be refused before
any movement, one well away from the meridian must pass. No hardware."""
import alignment
import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time
from calibration_engine.hi_reference_selection import _observer_location

LOC = _observer_location()
T0 = Time("2026-10-15T15:00:00", scale="utc")


def _centre_at_hour_angle(ha_h, dec=-8.5):
    lst = T0.sidereal_time("apparent", longitude=LOC.lon).hour
    return SkyCoord(ra=((lst - ha_h) % 24) * u.hourangle, dec=dec * u.deg)


def _check(ha_h):
    c = _centre_at_hour_angle(ha_h)
    temporal = alignment.pattern_temporal_altitudes(c, (5.0, 2.0, 0.6), 8, LOC, T0, 20.0)
    return alignment.meridian_limit_crossings(temporal, c, LOC)


def test_a_run_that_starts_just_east_of_the_meridian_is_refused():
    m = _check(-0.1)                                      # 6 min before transit: limit in ~18 min, run ~14.5 min + 5 SYNC
    assert not m["ok"] and m["centre_crosses_before_end"] and 17 < m["centre_minutes_to_limit"] < 19
    assert _check(-0.2)["ok"]                             # limit in ~24 min: the run and the SYNC end before it


def test_runs_well_east_or_already_past_the_limit_pass():
    assert _check(-2.0)["ok"]
    assert _check(0.4)["ok"]


def test_web_solar_align_runs_at_the_operating_gain_not_the_20_db_default():
    import almita_web_ops as ops
    argv, _ = ops.build_command("align_plan", {"reference": "sun"}, "JOB-1")
    assert argv[argv.index("--sun-gain") + 1] == "40.2"
    argv, _ = ops.build_command("align_plan", {"reference": "sun", "sun_gain": 30}, "JOB-1")
    assert argv[argv.index("--sun-gain") + 1] == "30.0"
    argv, _ = ops.build_command("align_plan", {"reference": "hi"}, "JOB-1")
    assert "--sun-gain" not in argv
