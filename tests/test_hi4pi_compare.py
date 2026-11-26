"""hi4pi_compare.py (audit 2026-11-25): every r with the r its null test gives. Fake HI4PI tiles, no FITS."""
import math

import numpy as np
import pytest

import hi4pi_compare as hc

VH = np.arange(-160, 160, 1.29)


class FakeTiles:
    """HI4PI through the beam: a Gaussian line whose velocity follows l (specific) or is the same everywhere."""

    def __init__(self, specific=True):
        self.specific = specific

    def beam(self, l0, b0, fwhm=20.0):
        c = 3.0 * l0 if self.specific else 10.0
        return VH, np.exp(-0.5 * ((VH - c) / 9.0) ** 2) + 0.3 * np.exp(-0.5 * (VH / 30.0) ** 2), 0.8


def _points(tiles, n=7, mirror=False, seed=0):
    rng = np.random.default_rng(seed)
    pts = []
    v = np.arange(-200, 200, 0.2)
    for i in range(n):
        l, b = -15 + 5 * i, 2.0
        vh, hs, _ = tiles.beam(l, b)
        t = np.interp(v, vh, hs, left=0, right=0)
        if mirror:
            t = t[::-1]
        pts.append({"i": i, "ra": 270.0 + i, "dec": -20.0, "l": l, "b": b, "v": v, "t": t + 0.01 * rng.standard_normal(v.size)})
    return pts


def test_a_position_specific_sky_is_called_position_specific():
    tiles = FakeTiles(specific=True)
    res = hc.spectra(_points(tiles), tiles)
    s = res["summary"]
    assert s["verdict"] == "POSITION-SPECIFIC MATCH"
    assert s["r_same_range"][0] > 0.95 and s["r_shuffled_median_range"][1] < s["r_same_range"][0]


def test_a_sky_that_looks_the_same_everywhere_is_not_a_pointing_match():
    """The 12-11 case: r_same is high, but HI4PI at any other pointing gives as much."""
    tiles = FakeTiles(specific=False)
    s = hc.spectra(_points(tiles), tiles)["summary"]
    assert s["r_same_range"][0] > 0.95
    assert s["verdict"] == "HYDROGEN WITH THE RIGHT VELOCITY SIGN - NOT A POINTING-BY-POINTING MATCH"
    assert s["points_beating_95pct_of_shuffled"] == 0


def test_a_mirrored_velocity_axis_fails():
    tiles = FakeTiles(specific=True)
    pts = _points(tiles, mirror=True)
    s = hc.spectra(pts, tiles)["summary"]
    assert s["verdict"] == "NO MATCH BEYOND THE NULLS"


def test_map_null_catches_a_smooth_gradient_that_any_shifted_pattern_also_follows():
    pts = [{"i": i, "ra": 260.0 + 2 * (i % 5), "dec": -30.0 + 2 * (i // 5), "v": np.arange(-100, 100, 0.5)} for i in range(25)]
    grad = lambda ra, dec: 10.0 + 0.1 * dec            # depends on dec only: an RA shift keeps it
    for p in pts:
        p["t"] = np.full(p["v"].size, grad(p["ra"], p["dec"]) / 120.0) + 0.0001 * np.random.default_rng(p["i"]).standard_normal(p["v"].size)
    m = hc.map_test(pts, grad, n_perm=500)
    assert m["r"] > 0.95 and m["perm_p"] < 0.05
    assert m["verdict"].startswith("CORRELATED, BUT MOVED/ROTATED PATTERNS DO AS WELL")
