"""SIGMA CONSISTENCY (section 30 with REAL data). Measured on real REDUCE Level 1 (100 points): the uncertainty
array is one fixed per-bin profile shared by every point; 63/7849 bins have sigma < 0.1 x median while the
between-point scatter observed at those bins equals that of normal bins (~0.085) - i.e. sigma is 20-850x too
small there (false precision), and it neutralises per-channel SNR gating. SCIENCE therefore excludes (never floors)
bins whose sigma is an isolated dip relative to their own neighbourhood.
"""
from pathlib import Path

import numpy as np
import pytest

from science_engine.config import ScienceConfig
from science_engine.cube import build_cube
from science_engine.grid import build_beam_model, build_grid
from science_engine.gridding import sigma_suspect_bins
from science_engine.simulation import build_synthetic_science_input, rectangular_grid_specs

GOOD = np.zeros(400, dtype=np.int64)
REAL_SESSION = Path("data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411")


def flat(n=400, v=0.04):
    return np.full(n, v)


def test_flat_and_smoothly_varying_sigma_are_never_suspect():
    assert not sigma_suspect_bins(GOOD, flat(), 0.1, 129).any()
    ramp = np.linspace(0.02, 0.06, 400)
    assert not sigma_suspect_bins(GOOD, ramp, 0.1, 129).any()
    bowl = 0.03 + 0.03 * ((np.arange(400) - 200) / 200) ** 2
    assert not sigma_suspect_bins(GOOD, bowl, 0.1, 129).any()


def test_isolated_dips_are_flagged_exactly_and_only_them():
    u = flat()
    dips = [5, 60, 199, 200, 350, 399]                      # incl. both array edges and an adjacent pair
    u[dips] = [1e-4, 3e-3, 2e-3, 3.9e-3, 1e-6, 1e-4]
    suspect = sigma_suspect_bins(GOOD, u, 0.1, 129)
    assert sorted(np.flatnonzero(suspect)) == dips


def test_a_broad_genuinely_low_noise_stretch_is_not_flagged():
    u = flat()
    u[100:300] = 0.005                                      # 200 channels: wider than the 129-channel window
    assert not sigma_suspect_bins(GOOD, u, 0.1, 129).any()


def test_masked_and_invalid_bins_are_ignored_by_the_neighbourhood_and_never_flagged():
    u = flat()
    mask = GOOD.copy()
    mask[50:150] = 4                                        # RFI
    u[50:150] = np.nan
    u[300] = -1.0                                           # invalid sigma: not GOOD-usable, left to bin_validity
    u[10] = 1e-4
    suspect = sigma_suspect_bins(mask, u, 0.1, 129)
    assert np.flatnonzero(suspect).tolist() == [10]
    all_masked = sigma_suspect_bins(np.full(400, 4), flat(), 0.1, 129)
    assert not all_masked.any()


def test_disabled_and_degenerate_inputs():
    u = flat()
    u[7] = 1e-6
    assert not sigma_suspect_bins(GOOD, u, 0.0, 129).any()
    assert sigma_suspect_bins(np.zeros(2, dtype=np.int64), np.array([0.04, 1e-9]), 0.1, 129).shape == (2,)


@pytest.mark.parametrize("kw", [dict(sigma_local_floor_fraction=-0.1), dict(sigma_local_floor_fraction=1.0),
                                dict(sigma_local_floor_fraction=float("nan")), dict(sigma_local_window_channels=128),
                                dict(sigma_local_window_channels=1), dict(sigma_local_window_channels=129.0)])
def test_invalid_sigma_check_configuration_is_rejected(kw):
    with pytest.raises(ValueError):
        ScienceConfig(**kw)


def _cube(fraction):
    specs = rectangular_grid_specs(12.0, -30.0, 3, 3, spacing_deg=1.0)
    si = build_synthetic_science_input(specs, n_channels=400, line_amplitude_fn=lambda r, d: 0.0, noise_sigma=0.04,
                                       descending=True, rng=np.random.default_rng(1))
    dips = [40, 120, 211, 300]
    for p in si.points:                                     # REDUCE gives every point the SAME sigma profile
        p.uncertainty[dips] = 1e-4
    config = ScienceConfig(beam_fwhm_deg=1.5, sigma_local_floor_fraction=fraction)
    beam = build_beam_model(config)
    grid = build_grid(si, beam, config)
    return build_cube(si, grid, beam, config), dips


def test_dips_no_longer_create_false_precision_in_the_cube():
    guarded, dips = _cube(0.1)
    unguarded, _ = _cube(0.0)
    centre = (guarded.grid.ny // 2, guarded.grid.nx // 2)
    # input axes are DESCENDING, the cube's is ascending: input channel d is cube channel 399 - d
    dip_channels = [399 - d for d in dips]
    normal = np.delete(np.arange(400), dip_channels)
    typical = np.nanmedian(guarded.uncertainty[normal][:, centre[0], centre[1]])
    tiny = np.array([unguarded.uncertainty[ch][centre] for ch in dip_channels])
    assert np.all(tiny < 0.05 * typical)                    # WITHOUT the check: sigma >20x too small at the dips
    assert all(np.isnan(guarded.relative_intensity[ch][centre]) for ch in dip_channels)     # excluded, not "precise"
    assert np.nanmin(guarded.uncertainty[:, centre[0], centre[1]]) > 0.5 * typical
    info = guarded.build_info["sigma_local_check"]
    assert info["max_bins_excluded_per_point"] == 4 and info["mean_bins_excluded_per_point"] == 4.0
    assert unguarded.build_info["sigma_local_check"]["max_bins_excluded_per_point"] == 0


@pytest.mark.skipif(not REAL_SESSION.is_dir(), reason="real REDUCE session not present")
def test_real_level1_sigma_profile_has_exactly_the_measured_isolated_dips():
    from science_engine.ingest import load_science_input
    point = load_science_input(REAL_SESSION).points[0]
    suspect = sigma_suspect_bins(point.mask, point.uncertainty, 0.1, 129)
    # the audit's 63 used a GLOBAL median (<0.1 x median); the LOCAL running-median criterion flags 67 (same dips)
    assert suspect.sum() == 67
    good = (point.mask == 0) & np.isfinite(point.uncertainty)
    assert suspect.sum() / good.sum() < 0.01                # < 1% of the spectrum
    u = point.uncertainty
    assert u[suspect].max() < 0.15 * np.median(u[good])   # local criterion: 0.111 x the GLOBAL median at worst
