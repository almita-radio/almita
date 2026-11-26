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


def test_smoothed_noise_from_differences_holds_the_hann_correlation():
    """Audit 2026-11-25: on real Hann power spectra the noise of a k-channel running mean, estimated from
    channel-to-channel differences, must match its true scatter (the old 1/sqrt(2k) was ~0.56 of it)."""
    import math
    rng = np.random.default_rng(2)
    n, frames, k = 4096, 8, 13
    win = np.hanning(n)
    x = rng.standard_normal((frames, n)) + 1j * rng.standard_normal((frames, n))
    p = np.mean(np.abs(np.fft.fft(x * win, axis=1)) ** 2, axis=0)
    t = p / np.median(p) - 1.0
    d = np.diff(t)
    est = 1.4826 * float(np.median(np.abs(d - np.median(d)))) * rc.smoothed_noise_factor(k)
    true = float(np.std(np.convolve(t, np.ones(k) / k, "valid")))
    assert est / true == pytest.approx(1.0, abs=0.12)
    old = 1.4826 * float(np.median(np.abs(d - np.median(d)))) / math.sqrt(2) / math.sqrt(k)
    assert old / true < 0.75


def test_rotation_speed_reports_measured_edge_and_assumed_v0_apart():
    tp = rc.tangent_point(30.0, 104.0, 2.0)
    assert tp["V_measured_edge_km_s"] == pytest.approx(104.0)
    assert tp["V_assumed_V0_part_km_s"] == pytest.approx(110.0, abs=0.01)
    assert tp["V_km_s"] == pytest.approx(214.0, abs=0.01)
    assert tp["V0_share"] == pytest.approx(110 / 214, abs=1e-3)


def test_threshold_sensitivity_finds_the_edge_at_every_fraction_and_reports_the_spread():
    v = np.linspace(-150, 200, 3000)
    t = 0.2 * np.exp(-0.5 * (v / 12) ** 2) + 0.03 * np.clip(1 - (v - 0) / 110, 0, 1) * (v > 0)   # a ramp to +110
    rng = np.random.default_rng(1)
    t = t + 0.0005 * rng.standard_normal(v.size)
    s = rc.threshold_sensitivity(v, t, +1)
    assert s["all_found"] and set(s["edge_by_fraction_km_s"]) == {"0.02", "0.03", "0.05"}
    e = s["edge_by_fraction_km_s"]
    assert e["0.02"] >= e["0.03"] >= e["0.05"]                   # a lower threshold reaches further out on the ramp
    assert s["spread_km_s"] == pytest.approx(e["0.02"] - e["0.05"])
