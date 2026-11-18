"""rotation_curve.py (BL-105): the Milky Way's rotation curve by the tangent-point method from REDUCE spectra, with
HI4PI seen through the same beam and the same edge method as the honest comparison."""
import json
import math

import numpy as np
import pytest

import rotation_curve as rc


def _spectrum(v_t, sign=+1, peak=1.0, tangent=0.04, noise=0.002, seed=1):
    """Local gas (a broad peak near 0) + a low plateau out to the terminal velocity, then nothing."""
    rng = np.random.default_rng(seed)
    v = np.arange(-250, 250, 0.3)
    t = peak * np.exp(-0.5 * (v / 15) ** 2)
    s = sign * v
    t = t + tangent / (1 + np.exp(-(s - 5) / 4)) / (1 + np.exp((s - abs(v_t)) / 4))
    return v, t + rng.normal(0, noise, v.size)


def test_the_edge_is_found_on_the_terminal_side_of_each_quadrant():
    for sign, vt in ((+1, 150.0), (-1, -120.0)):
        v, t = _spectrum(vt, sign)
        e = rc.terminal_velocity(v, t, sign)
        assert e["ok"] and e["terminal_km_s"] == pytest.approx(vt, abs=6)


def test_a_line_running_off_the_window_is_refused_not_guessed():
    v, t = _spectrum(150.0)
    keep = v < 160                                         # the window ends just past the edge
    e = rc.terminal_velocity(v[keep], t[keep], +1)
    assert not e["ok"] and "window ends" in e["reason"]


def test_tangent_point_geometry_and_quadrants():
    tp = rc.tangent_point(30.0, 110.0)
    assert tp["R_kpc"] == pytest.approx(rc.R0_KPC * 0.5) and tp["V_km_s"] == pytest.approx(110 + rc.V0_KM_S * 0.5)
    assert rc.quadrant_sign(30) == 1 and rc.quadrant_sign(330) == -1 and rc.quadrant_sign(180) == 0 and rc.quadrant_sign(90) == 0
    flat = rc.V0_KM_S * (1 - math.sin(math.radians(40)))  # a flat curve's terminal velocity gives back V0
    assert rc.tangent_point(40.0, flat)["V_km_s"] == pytest.approx(rc.V0_KM_S)


def _write_session(tmp_path, pts):
    import h5py
    for i, (ra, dec, v, t) in enumerate(pts, 1):
        d = tmp_path / "points" / str(i)
        d.mkdir(parents=True)
        (d / "master_spectrum.json").write_text(json.dumps({"point_index": i, "ra_hours": ra, "dec_degrees": dec,
                                                             "quality": {"state": "WARNING"}}))
        with h5py.File(d / "master_spectrum.h5", "w") as f:
            f["velocity_lsrk_m_s"] = v * 1000.0
            f["relative_intensity"] = t
            f["mask"] = np.zeros(v.size, np.int64)
    return tmp_path


def test_a_flat_galaxy_gives_back_a_flat_curve_and_off_plane_points_are_left_out(tmp_path):
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    pts = []
    for l in (25.0, 40.0, 55.0, 320.0):
        c = SkyCoord(l=l * u.deg, b=0 * u.deg, frame="galactic").icrs
        sign = rc.quadrant_sign(l)
        vt = sign * rc.V0_KM_S * (1 - abs(math.sin(math.radians(l))))
        v, t = _spectrum(vt, sign, seed=int(l))
        pts.append((c.ra.hour, c.dec.deg, v, t))
    c = SkyCoord(l=40 * u.deg, b=45 * u.deg, frame="galactic").icrs
    pts.append((c.ra.hour, c.dec.deg, *_spectrum(30.0)))
    res = rc.analyse(_write_session(tmp_path, pts), hi4pi=rc.Hi4piTiles(tmp_path / "none"))
    used = [r for r in res["points"] if r.get("used")]
    assert len(used) == 4 and res["points"][-1]["used"] is False
    for r in used:
        assert r["V_km_s"] == pytest.approx(rc.V0_KM_S, abs=8)
    files = rc.plots(res, tmp_path, tmp_path)
    assert {"rotation_curve.png", "lv_diagram.png"} <= {p.split("/")[-1] for p in files}


def test_hi4pi_is_averaged_through_the_beam_only_where_the_tile_holds_it(tmp_path):
    from astropy.io import fits
    n, nv = 121, 200
    cube = np.zeros((nv, n, n), np.float32)
    v = (np.arange(nv) - 100) * 2.0
    cube[:, :, :] = np.exp(-0.5 * (v / 10) ** 2)[:, None, None] * 50.0
    h = fits.Header()
    for k, val in dict(CTYPE1="GLON-CAR", CTYPE2="GLAT-CAR", CTYPE3="VRAD", CRVAL1=30.0, CRPIX1=61.0, CDELT1=-0.5,
                       CRVAL2=0.0, CRPIX2=61.0, CDELT2=0.5, CRVAL3=0.0, CRPIX3=101.0, CDELT3=2000.0).items():
        h[k] = val
    fits.PrimaryHDU(cube, h).writeto(tmp_path / "CAR_T01.fits")
    tiles = rc.Hi4piTiles(tmp_path)
    got = tiles.beam_spectrum(30.0, 0.0, 20.0)
    assert got is not None and got[2] > 0.9 and np.nanmax(got[1]) == pytest.approx(50.0, rel=1e-3)
    assert tiles.beam_spectrum(58.0, 0.0, 20.0) is None    # the beam hangs off the tile: no partial average
