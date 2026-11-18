"""OBSERVE window suggestion (operator 2026-11-18): WINDOW CENTRE + SWITCH OFFSET for a region's centre. A suggestion
only - these tests pin what it suggests where, and that it never touches anything."""
import numpy as np
import pytest

import fswitch_suggest as S
import fswitch_center as FC

HAVE_GASS = S.GASS_COARSE.exists() or S.GASS_FITS.exists()
needs_gass = pytest.mark.skipif(not HAVE_GASS, reason="GASS cap not on this machine (data/reference/hi/gass)")


def _eq(l, b):
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    c = SkyCoord(l=l * u.deg, b=b * u.deg, frame="galactic").icrs
    return c.ra.hour, c.dec.deg


@needs_gass
def test_large_magellanic_cloud_gets_its_own_window_and_the_short_throw():
    s = S.suggest(5.39, -69.76)
    assert s["source"] == "GASS" and s["beam_cover"] >= 0.9
    assert s["offset_hz"] == 300e3                       # at 600 kHz the local gas's ghost lands on the cloud (+253)
    assert 230 <= s["centre_km_s"] <= 270
    lo, hi = s["window_lsrk_km_s"]
    assert lo < 200 and hi > 300                         # the whole cloud (+200..+300) inside
    assert s["nonlocal_in_window"] >= 0.8
    assert any("+127" in n for n in s["notes"])          # the local ghost inside the window is said, and masked


@needs_gass
def test_small_magellanic_cloud_keeps_600_khz():
    s = S.suggest(0.88, -72.8)
    assert s["source"] == "GASS" and s["offset_hz"] == 600e3
    assert 140 <= s["centre_km_s"] <= 170
    assert s["nonlocal_in_window"] >= 0.8


def test_quiet_sky_is_the_classic_set_up():
    for l, b in ((180.0, 0.0), (20.0, -40.0), (0.0, -89.0)):
        ra, dec = _eq(l, b)
        sp = S.model_beam_spectrum(l, b, 20.0)
        best = S.best_for_offset(sp["t"], 600e3)
        assert best["centre_km_s"] == 0.0 and best["target_in_window"] > 0.95


def test_first_and_fourth_quadrant_arms_move_the_window_toward_their_terminal_side():
    q1 = S.best_for_offset(S.model_beam_spectrum(30.0, 0.0, 20.0)["t"], 600e3)
    q4 = S.best_for_offset(S.model_beam_spectrum(330.0, 0.0, 20.0)["t"], 600e3)
    assert q1["centre_km_s"] > 30 and q4["centre_km_s"] < -30


def test_galactic_centre_says_the_gas_does_not_fit():
    ra, dec = _eq(0.0, 0.0)
    s = S.suggest(ra, dec)
    if s["source"] == "MODEL":
        assert abs(s["centre_km_s"]) <= 20
        assert s["nonlocal_in_window"] < 0.6 and any("fits in any window" in n for n in s["notes"])


def test_ghost_spectrum_is_half_the_line_at_plus_and_minus_the_throw():
    t = np.zeros(S.V_AXIS.size)
    t[np.searchsorted(S.V_AXIS, 100.0)] = 2.0
    g = S.ghost_spectrum(t, 600e3)
    throw = FC.throw_km_s(600e3)
    for v in (100.0 - throw, 100.0 + throw):
        i = np.argmin(np.abs(S.V_AXIS - v))
        assert g[max(i - 1, 0):i + 2].sum() == pytest.approx(1.0, abs=1e-6)


def test_bad_coordinates_are_refused():
    with pytest.raises(ValueError):
        S.suggest(float("nan"), -30.0)
    with pytest.raises(ValueError):
        S.suggest(5.0, -95.0)


def test_endpoint_answers_and_is_display_only():
    from tests.test_orchestrator_web import _get, running_server
    with running_server() as base:
        status, body = _get(f"{base}/api/observe/fswitch-suggest?ra_hours=6.0&dec_deg=20.0")
    assert status == 200 and body["ok"]
    d = body["data"]
    assert d["suggestion_only"] is True and d["offset_hz"] in (600e3, 300e3)
    assert len(d["spectrum"]["v_km_s"]) == len(d["spectrum"]["t"]) == len(d["spectrum"]["ghost"])
