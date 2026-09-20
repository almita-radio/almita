"""UNCERTAINTY CALIBRATION (sections 35-37, 96-98, 101, 103, 115): Monte Carlo through the PRODUCTION cube path
(resample + validity + beam*inverse-variance accumulation + integrated map). Only the pointing geometry is
memoised (it does not depend on the noise), so hundreds of realisations stay fast.
"""
import numpy as np
import pytest

import science_engine.cube as cube_module
from science_engine.config import ScienceConfig
from science_engine.cube import build_cube
from science_engine.grid import build_beam_model, build_grid
from science_engine.gridding import spatial_weight_for_point as real_spatial_weight
from science_engine.integration import integrated_map
from science_engine.simulation import build_synthetic_science_input, rectangular_grid_specs

SIGMA = 0.05


@pytest.fixture
def memoised_geometry(monkeypatch):
    cache = {}

    def cached(grid, ra, dec, beam):
        key = (round(ra, 9), round(dec, 9), id(grid))
        if key not in cache:
            cache[key] = real_spatial_weight(grid, ra, dec, beam)
        return cache[key]

    monkeypatch.setattr(cube_module, "spatial_weight_for_point", cached)


def mc(trials, *, offsets, n_channels=48, truth_amp=0.5, seed=0):
    specs = rectangular_grid_specs(12.0, -30.0, 3, 3, spacing_deg=1.0)
    for i, s in enumerate(specs):
        s.velocity_offset_m_s = offsets[i % len(offsets)]
    rng = np.random.default_rng(seed)
    config = ScienceConfig(beam_fwhm_deg=1.5, pixels_per_beam=2.0, extent_margin_beams=0.5,
                           velocity_window_min_m_s=-1.9e5, velocity_window_max_m_s=1.9e5)
    beam = build_beam_model(config)
    si0 = build_synthetic_science_input(specs, n_channels=n_channels, line_amplitude_fn=lambda r, d: truth_amp,
                                        line_fwhm_m_s=1e9, noise_sigma=SIGMA, rng=rng)
    grid = build_grid(si0, beam, config)
    cubes, ints = [], []
    for t in range(trials):
        si = build_synthetic_science_input(specs, n_channels=n_channels, line_amplitude_fn=lambda r, d: truth_amp,
                                           line_fwhm_m_s=1e9, noise_sigma=SIGMA, rng=rng)
        cube = build_cube(si, grid, beam, config)
        cubes.append(cube)
        ints.append(integrated_map(cube, config))
    truth_spectrum = truth_amp * np.exp(-4 * np.log(2) * cubes[0].velocity_lsrk_m_s ** 2 / 1e18)
    return grid, cubes, ints, truth_spectrum, config


def summarise(cubes, ints, truth_spectrum, grid, sel_channels):
    stack = np.stack([c.relative_intensity for c in cubes])                    # (T, Nv, Ny, Nx)
    reported = cubes[0].uncertainty
    cy, cx = grid.ny // 2, grid.nx // 2
    out = {}
    emp = stack[:, sel_channels, cy, cx].std(axis=0, ddof=1)
    rep = reported[sel_channels, cy, cx]
    out["ratio_channel"] = float(np.mean(emp / rep))
    truth = truth_spectrum[sel_channels]
    z = (stack[:, sel_channels, cy, cx] - truth[None]) / rep[None]
    out["within_1sigma"] = float(np.mean(np.abs(z) < 1))
    out["within_2sigma"] = float(np.mean(np.abs(z) < 2))
    i_stack = np.stack([m.value for m in ints])
    i_rep = ints[0].uncertainty
    out["ratio_integrated"] = float(i_stack[:, cy, cx].std(ddof=1) / i_rep[cy, cx])
    return out, stack


def test_calibration_without_resampling_is_textbook_and_reports_coverage(memoised_geometry, capsys):
    grid, cubes, ints, truth, config = mc(600, offsets=[0.0])
    sel = np.arange(4, 44)
    out, _ = summarise(cubes, ints, truth, grid, sel)
    with capsys.disabled():
        print(f"\nUNCERTAINTY MC, no resampling, 600 realisations: empirical/reported per-channel={out['ratio_channel']:.3f}  "
              f"integrated={out['ratio_integrated']:.3f}  truth within 1sigma={out['within_1sigma']:.3f} (0.683)  "
              f"within 2sigma={out['within_2sigma']:.3f} (0.954)")
    assert out["ratio_channel"] == pytest.approx(1.0, abs=0.04)
    assert out["ratio_integrated"] == pytest.approx(1.0, abs=0.12)      # 600 trials -> ~3% statistical error on a std
    assert out["within_1sigma"] == pytest.approx(0.683, abs=0.03)
    assert out["within_2sigma"] == pytest.approx(0.954, abs=0.02)


def test_calibration_with_realistic_lsrk_offsets_is_conservative_per_channel_and_right_when_integrated(
        memoised_geometry, capsys):
    """Offsets of 0..10 channels (real data: up to 84) force the resample path for 8 of 9 points."""
    dv = 400_000.0 / 47
    offsets = [0.0, 0.5 * dv, 1.7 * dv, 3.25 * dv, 5.0 * dv, 7.4 * dv, 10.3 * dv]
    grid, cubes, ints, truth, config = mc(600, offsets=offsets, seed=3)
    sel = np.arange(14, 34)                                  # interior channels: full support from every point
    out, _ = summarise(cubes, ints, truth, grid, sel)
    with capsys.disabled():
        print(f"UNCERTAINTY MC, with LSRK offsets (resampled): per-channel={out['ratio_channel']:.3f}  "
              f"integrated={out['ratio_integrated']:.3f}  within 1sigma={out['within_1sigma']:.3f}  "
              f"within 2sigma={out['within_2sigma']:.3f}")
    assert cubes[0].build_info["resample"]["n_points_resampled"] >= 6
    assert 0.75 <= out["ratio_channel"] <= 1.03              # never OVER-confident per channel (>=0.75: <= sqrt(2) conservative)
    assert out["within_1sigma"] >= 0.62 and out["within_2sigma"] >= 0.93
    assert out["ratio_integrated"] == pytest.approx(1.0, abs=0.15)      # integrated sigma is right, not just conservative


def test_neighbouring_pixels_are_beam_correlated_so_per_pixel_sigma_is_not_independence(memoised_geometry, capsys):
    """Section 37: documents (with a measurement) that adjacent output pixels share contributors."""
    grid, cubes, ints, truth, config = mc(400, offsets=[0.0], seed=5)
    stack = np.stack([c.relative_intensity[10] for c in cubes])            # (T, Ny, Nx) one channel
    cy, cx = grid.ny // 2, grid.nx // 2
    a, b = stack[:, cy, cx], stack[:, cy, cx + 1]
    r = float(np.corrcoef(a, b)[0, 1])
    far = float(np.corrcoef(a, stack[:, cy, min(cx + 6, grid.nx - 1)])[0, 1])
    with capsys.disabled():
        print(f"SPATIAL CORRELATION of the noise: adjacent pixels r={r:.3f}; 6 pixels away r={far:.3f}")
    assert r > 0.5 and r > far


def test_uncertainty_falls_as_coverage_rises_no_gross_inversion(memoised_geometry):
    """Section 115: rank correlation between weight_sum and 1/sigma must be strongly positive."""
    grid, cubes, ints, truth, config = mc(1, offsets=[0.0])
    c = cubes[0]
    w = c.weight_sum[10][c.valid[10]]
    inv_sigma = 1.0 / c.uncertainty[10][c.valid[10]]
    ranks = lambda a: np.argsort(np.argsort(a))
    spearman = np.corrcoef(ranks(w), ranks(inv_sigma))[0, 1]
    assert spearman > 0.9


def test_coverage_hole_lowers_weight_and_raises_sigma_but_is_not_blanked():
    """Sections 97-98: remove the central pointing of a constant sky; neighbouring beams still cover it."""
    specs_full = rectangular_grid_specs(12.0, -30.0, 5, 5, spacing_deg=1.0)
    specs_hole = [s for s in specs_full if s.point_index != 13]
    kw = dict(n_channels=4, line_amplitude_fn=lambda r, d: 0.0, noise_sigma=0.0)
    config = ScienceConfig(beam_fwhm_deg=1.5, pixels_per_beam=2.0, extent_margin_beams=1.0)
    beam = build_beam_model(config)
    si_full = build_synthetic_science_input(specs_full, **kw)
    grid = build_grid(si_full, beam, config)
    si_hole = build_synthetic_science_input(specs_hole, **kw)
    for si in (si_full, si_hole):
        for p in si.points:
            p.relative_intensity[:], p.uncertainty[:] = 1.0, 0.1
    full, hole = build_cube(si_full, grid, beam, config), build_cube(si_hole, grid, beam, config)
    c = (grid.ny // 2, grid.nx // 2)
    assert hole.valid[0][c] and full.valid[0][c]                                # neighbours' beams still cover the centre
    assert hole.weight_sum[0][c] < full.weight_sum[0][c] * 0.85                 # the missing measurement is visible in weight
    assert hole.n_pointings[0][c] == full.n_pointings[0][c] - 1                 # ...and in the pointing count
    assert hole.uncertainty[0][c] > full.uncertainty[0][c]                      # ...and in sigma
    assert hole.relative_intensity[0][c] == pytest.approx(1.0, abs=1e-14)       # constant sky stays constant
    # away from the hole nothing changes at all
    corner = (0, 0)      # ~2.8 deg from the removed pointing: its beam weight there is ~6e-5, so the change is negligible
    assert abs(hole.weight_sum[0][corner] / full.weight_sum[0][corner] - 1.0) < 1e-3
