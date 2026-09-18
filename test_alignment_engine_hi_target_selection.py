"""Fase 15/65: target selection must reject flat/degenerate regions and
prefer real 2D structure - unit-level, independent of any FITS/catalog
provider."""
import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time

from alignment_engine.hi.target_selection import score_candidate, select_best_target_from_grid

LOCATION = EarthLocation(lat=-33.4331 * u.deg, lon=289.3336 * u.deg, height=550 * u.m)
OBSTIME = Time("2026-09-18T03:00:00")
CENTER = SkyCoord(ra=180.0, dec=-40.0, unit="deg")  # well above horizon at this southern site


def test_flat_field_is_rejected():
    def flat(coords):
        return np.full(len(SkyCoord(coords).reshape((-1,))), 5.0)
    result = score_candidate(CENTER, flat, LOCATION, OBSTIME, min_altitude_deg=-90.0)
    assert result.accepted is False
    assert "degenerate" in result.reason


def test_symmetric_gaussian_centered_exactly_on_candidate_is_rejected():
    """A perfectly symmetric peak with the candidate sitting exactly at
    the top has zero LOCAL gradient there (by symmetry) even though the
    surrounding region has real structure - this must still be rejected as
    degenerate at that exact pointing (Fase 15's "estructura simétrica
    degenerada")."""
    def symmetric_peak(coords):
        local = SkyCoord(coords).reshape((-1,)).transform_to(CENTER.skyoffset_frame())
        return 10.0 * np.exp(-0.5 * (local.lon.deg ** 2 + local.lat.deg ** 2) / 4.0)
    result = score_candidate(CENTER, symmetric_peak, LOCATION, OBSTIME, min_altitude_deg=-90.0)
    assert result.accepted is False


def test_real_gradient_region_is_accepted():
    def sloped(coords):
        local = SkyCoord(coords).reshape((-1,)).transform_to(CENTER.skyoffset_frame())
        return 5.0 + 0.8 * local.lon.deg + 0.3 * local.lat.deg
    result = score_candidate(CENTER, sloped, LOCATION, OBSTIME, min_altitude_deg=-90.0)
    assert result.accepted is True
    assert result.gradient_magnitude > 0.0
    assert result.score > 0.0


def test_below_altitude_floor_is_rejected_before_evaluating_structure():
    below_horizon = SkyCoord(ra=180.0, dec=80.0, unit="deg")  # far north, below horizon at this southern site

    def sloped(coords):
        local = SkyCoord(coords).reshape((-1,)).transform_to(below_horizon.skyoffset_frame())
        return 5.0 + local.lon.deg
    result = score_candidate(below_horizon, sloped, LOCATION, OBSTIME, min_altitude_deg=20.0)
    assert result.accepted is False
    assert "altitude" in result.reason


def test_coverage_gap_at_candidate_is_rejected_not_treated_as_zero():
    def with_gap(coords):
        return np.full(len(SkyCoord(coords).reshape((-1,))), np.nan)
    result = score_candidate(CENTER, with_gap, LOCATION, OBSTIME, min_altitude_deg=-90.0)
    assert result.accepted is False


def test_select_best_prefers_higher_structure_over_flat():
    grid = SkyCoord(ra=[10.0, 100.0, 200.0], dec=[-50.0, -45.0, -40.0], unit="deg")

    def value_fn(coords):
        pts = SkyCoord(coords).reshape((-1,))
        out = []
        for p in pts:
            sep_from_hot = p.separation(SkyCoord(ra=200.0, dec=-40.0, unit="deg")).deg
            out.append(5.0 + 3.0 * np.exp(-sep_from_hot ** 2 / 50.0) + 0.01 * p.ra.deg)
        return np.asarray(out)

    center, info = select_best_target_from_grid(grid, value_fn, LOCATION, OBSTIME,
                                                  min_altitude_deg=-90.0, beam_fwhm_deg=6.0)
    assert info["accepted"] is True


def test_select_best_raises_when_everything_is_degenerate():
    grid = SkyCoord(ra=[10.0, 100.0], dec=[-50.0, -45.0], unit="deg")

    def flat(coords):
        return np.full(len(SkyCoord(coords).reshape((-1,))), 5.0)

    with pytest.raises(RuntimeError):
        select_best_target_from_grid(grid, flat, LOCATION, OBSTIME, min_altitude_deg=-90.0, beam_fwhm_deg=6.0)
