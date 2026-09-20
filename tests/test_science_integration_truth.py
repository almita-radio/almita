"""INTEGRATED MAP / CHANNEL MAP ACCEPTANCE (sections 45-54, 103-105): analytic truth, irregular axes, partial
windows, masked lines, negative values, uncertainty propagation and channel selection. Cubes are constructed
directly (ScienceCube) so the expected numbers come from explicit formulas, never from production helpers.
"""
import numpy as np
import pytest

from science_engine.config import ScienceConfig
from science_engine.integration import (bin_bounds, channel_map, channel_map_nearest, integrated_map,
                                        nearest_channel_index, window_overlaps)
from science_engine.models import ScienceCube, ScienceGrid


def make_cube(velocity, value_fn, sigma_fn=lambda v: 0.05 + 0 * v, ny=3, nx=4, valid_fn=None):
    """A cube whose every pixel carries value_fn(velocity) (identical spectra; pixel (1,1) may be masked by valid_fn)."""
    velocity = np.asarray(velocity, dtype=float)
    nv = velocity.shape[0]
    grid = ScienceGrid(frame="icrs", center_ra_deg=10.0, center_dec_deg=-30.0, width_deg=4.0, height_deg=3.0,
                       pixel_scale_deg=1.0, nx=nx, ny=ny)
    v = np.broadcast_to(value_fn(velocity)[:, None, None], (nv, ny, nx)).copy()
    u = np.broadcast_to(sigma_fn(velocity)[:, None, None], (nv, ny, nx)).copy()
    valid = np.ones((nv, ny, nx), dtype=bool) if valid_fn is None else valid_fn(velocity, ny, nx)
    v[~valid], u[~valid] = np.nan, np.nan
    return ScienceCube(grid=grid, velocity_lsrk_m_s=velocity, relative_intensity=v, uncertainty=u,
                       weight_sum=valid.astype(float), n_pointings=valid.astype(np.int64), valid=valid)


def cfg(vmin, vmax, **kw):
    return ScienceConfig(velocity_window_min_m_s=vmin, velocity_window_max_m_s=vmax, **kw)


def gauss(v, amp=1.0, c=0.0, fwhm=20_000.0):
    return amp * np.exp(-4 * np.log(2) * (v - c) ** 2 / fwhm ** 2)


AREA = lambda amp, fwhm: amp * fwhm * np.sqrt(np.pi / (4 * np.log(2)))
DV = 61.8343185


# ---------------------------------------------------------------- 45-48: definition, dv, units

def test_integral_uses_actual_dv_on_an_irregular_axis():
    """Section 46: nothing may assume uniform spacing. A geometric-ish axis with a constant spectrum of 1.0 must
    integrate to exactly the window width whatever the spacing (bins tile the axis)."""
    v = np.cumsum(np.concatenate([[0.0], np.linspace(20.0, 200.0, 199)]))     # dv grows 20 -> 200 m/s
    cube = make_cube(v, lambda x: np.ones_like(x))
    lo, hi = v[20], v[150]
    result = integrated_map(cube, cfg(lo, hi))
    assert np.allclose(result.value[result.valid], hi - lo, rtol=1e-12)


@pytest.mark.parametrize("descending", [False, True])
def test_integrated_gaussian_matches_analytic_area(descending, capsys):
    """Section 47: measured error of the bin-overlap rule on an analytic Gaussian, several sampling densities."""
    rows = []
    for fwhm_channels in (100, 20, 8, 4):
        v = (np.arange(-3000, 3000)) * DV
        v = v[::-1] if descending else v
        fwhm = fwhm_channels * DV
        cube = make_cube(v, lambda x: gauss(x, 2.0, 0.37 * DV, fwhm))
        res = integrated_map(cube, cfg(-2.0e5, 2.0e5))
        err = res.value[1, 1] / AREA(2.0, fwhm) - 1.0
        rows.append((fwhm_channels, err))
        assert abs(err) < {100: 1e-9, 20: 1e-5, 8: 1e-3, 4: 1e-2}[fwhm_channels]
    if not descending:
        with capsys.disabled():
            print("\nINTEGRATION RULE (bin-overlap) area error vs analytic: " +
                  "  ".join(f"FWHM={c}ch: {100*e:+.2e}%" for c, e in rows))


def test_units_are_explicit_and_never_kelvin_like():
    cube = make_cube(np.arange(100) * DV, lambda v: gauss(v, 1.0, 3000.0, 2000.0))
    result = integrated_map(cube, cfg(-1e5, 1e5))
    assert result.units == "relative_intensity_dimensionless * m/s"
    ch = channel_map(cube, 3000.0, 500.0)
    assert ch.units == "relative_intensity_dimensionless"


# ---------------------------------------------------------------- 49-52: spectral coverage, windows

def test_spectral_coverage_and_partial_mask_are_reported_never_interpolated():
    """Sections 49-50: a line with its central channels masked gives a LOWER integral and a LOWER coverage; the
    masked channels are not interpolated to 'recover' the truth."""
    v = (np.arange(-500, 500)) * DV
    full = make_cube(v, lambda x: gauss(x, 1.0, 0.0, 20 * DV))
    gap = lambda vel, ny, nx: (np.abs(vel)[:, None, None] > 4 * DV) | np.zeros((1, ny, nx), dtype=bool)
    masked = make_cube(v, lambda x: gauss(x, 1.0, 0.0, 20 * DV), valid_fn=gap)
    window = cfg(-500 * DV, 499 * DV, min_spectral_coverage_fraction=0.5)
    a, b = integrated_map(full, window), integrated_map(masked, window)
    core = np.abs(v) <= 4 * DV                                       # the 9 masked channels
    missing_area = DV * np.sum(gauss(v[core], 1.0, 0.0, 20 * DV))
    assert b.value[1, 1] == pytest.approx(a.value[1, 1] - missing_area, rel=1e-9)   # EXACTLY the visible area: no fill-in
    assert b.value[1, 1] < a.value[1, 1] * 0.7                       # ~36% of this line's area lived in the core
    assert b.spectral_coverage[1, 1] < a.spectral_coverage[1, 1]     # ...and the product says so
    assert np.isclose(a.spectral_coverage[1, 1], 1.0, atol=1e-3)
    assert np.isclose(b.spectral_coverage[1, 1], 1.0 - 9 * DV / (999 * DV), atol=2e-3)
    assert b.valid[1, 1]                                             # above the 0.5 threshold: valid but flagged by coverage


def test_pixel_below_coverage_threshold_is_invalid_not_extrapolated():
    v = (np.arange(-500, 500)) * DV
    mostly = lambda vel, ny, nx: np.broadcast_to((np.abs(vel) < 100 * DV)[:, None, None], (vel.size, ny, nx)).copy()
    cube = make_cube(v, lambda x: np.ones_like(x), valid_fn=mostly)
    res = integrated_map(cube, cfg(-500 * DV, 499 * DV, min_spectral_coverage_fraction=0.5))
    assert not res.valid.any() and np.isnan(res.value).all() and np.isnan(res.uncertainty).all()
    assert np.isclose(res.spectral_coverage[0, 0], 199 / 999, atol=2e-3)     # 20% valid bins looks nothing like 100%


def test_window_partly_outside_cube_is_reported_and_lowers_coverage():
    """Section 51: never silently clipped."""
    v = np.arange(0, 1000) * 100.0                                    # cube: -50 .. 99950 m/s of bin coverage
    cube = make_cube(v, lambda x: np.ones_like(x))
    res = integrated_map(cube, cfg(50_000.0, 150_000.0, min_spectral_coverage_fraction=0.4))
    assert res.metadata["window_status"] == "PARTIAL_OUTSIDE_CUBE"
    assert res.metadata["window_covered_by_cube_fraction"] == pytest.approx(0.5, abs=2e-3)
    assert res.spectral_coverage[1, 1] == pytest.approx(0.5, abs=2e-3)       # half the requested window has data
    assert res.value[1, 1] == pytest.approx(50_000.0, rel=2e-3)              # integral of 1.0 over the covered half only
    strict = integrated_map(cube, cfg(50_000.0, 150_000.0, min_spectral_coverage_fraction=0.9))
    assert not strict.valid.any()                                            # a strict requirement blocks it


def test_window_fully_outside_cube_raises_blocked():
    cube = make_cube(np.arange(100) * 100.0, lambda x: np.ones_like(x))
    with pytest.raises(ValueError, match="BLOCKED"):
        integrated_map(cube, cfg(5e5, 6e5))
    with pytest.raises(ValueError, match="BLOCKED"):
        integrated_map(cube, cfg(-6e5, -5e5))


def test_narrow_windows_one_channel_subchannel_and_exact_boundaries():
    """Section 52: defined behaviour for a window narrower than, equal to, and aligned with channels."""
    v = np.arange(0, 21) * 100.0                     # bins are [c-50, c+50]
    cube = make_cube(v, lambda x: x / 100.0)         # value == channel index
    one_channel = integrated_map(cube, cfg(950.0, 1050.0, min_spectral_coverage_fraction=1.0))     # exactly channel 10's bin
    assert one_channel.value[1, 1] == pytest.approx(10.0 * 100.0)
    sub = integrated_map(cube, cfg(1000.0, 1020.0, min_spectral_coverage_fraction=1.0))            # 20 m/s inside ch 10
    assert sub.value[1, 1] == pytest.approx(10.0 * 20.0)
    on_centres = integrated_map(cube, cfg(1000.0, 1100.0, min_spectral_coverage_fraction=1.0))     # centre 10 -> centre 11
    assert on_centres.value[1, 1] == pytest.approx(0.5 * 100.0 * 10.0 + 0.5 * 100.0 * 11.0)        # half of each bin
    assert on_centres.spectral_coverage[1, 1] == pytest.approx(1.0)
    assert np.allclose(window_overlaps(v, 950.0, 1050.0), np.eye(21)[10] * 100.0)


def test_negative_relative_intensity_is_a_legitimate_measurement():
    """Section 105: baseline-subtracted data can be negative; nothing is masked or clipped for being negative."""
    v = np.arange(0, 200) * DV
    cube = make_cube(v, lambda x: -0.02 + 0 * x)
    res = integrated_map(cube, cfg(v[10], v[100]))
    assert res.valid.all() and (res.value < 0).all()
    assert res.value[1, 1] == pytest.approx(-0.02 * (v[100] - v[10]), rel=1e-2)
    ch = channel_map_nearest(cube, v[50])
    assert ch.valid.all() and ch.value[1, 1] == pytest.approx(-0.02)


# ---------------------------------------------------------------- 103: integrated uncertainty

def test_integrated_uncertainty_is_the_independent_channel_quadrature_with_dv():
    v = np.arange(0, 400) * DV
    sigma = 0.03
    cube = make_cube(v, lambda x: np.zeros_like(x), sigma_fn=lambda x: sigma + 0 * x)
    lo, hi = v[100], v[300]
    res = integrated_map(cube, cfg(lo, hi))
    ov = window_overlaps(v, lo, hi)
    expected = np.sqrt(np.sum((sigma * ov) ** 2))
    assert res.uncertainty[1, 1] == pytest.approx(expected, rel=1e-12)
    # and against the textbook closed form for N full channels of width dv: sigma * dv * sqrt(N)
    n_full = 200
    assert res.uncertainty[1, 1] == pytest.approx(sigma * DV * np.sqrt(n_full), rel=0.01)
    assert res.metadata["uncertainty_assumption"] == "independent channels"


def test_masked_channels_are_excluded_from_the_uncertainty_sum():
    v = np.arange(0, 100) * DV
    valid = lambda vel, ny, nx: np.broadcast_to((np.arange(vel.size) % 2 == 0)[:, None, None], (vel.size, ny, nx)).copy()
    cube = make_cube(v, lambda x: np.ones_like(x), sigma_fn=lambda x: 0.1 + 0 * x, valid_fn=valid)
    res = integrated_map(cube, cfg(v[0] - DV / 2, v[-1] + DV / 2, min_spectral_coverage_fraction=0.4))
    assert res.uncertainty[1, 1] == pytest.approx(0.1 * DV * np.sqrt(50), rel=1e-9)
    assert res.value[1, 1] == pytest.approx(DV * 50, rel=1e-9)             # only valid channels contribute (no fake I=0 rows)


# ---------------------------------------------------------------- 53, 104: channel maps

def test_channel_selection_exact_nearest_and_interval_have_no_off_by_one():
    v = np.arange(0, 50) * 100.0
    cube = make_cube(v, lambda x: x / 100.0)            # value == index
    exact = channel_map(cube, 2000.0, 0.0)              # width 0 -> exactly the channel at 2000
    assert exact.metadata["channel_indices"] == [20] and exact.value[1, 1] == 20.0
    assert exact.metadata["selected_velocity_mean_m_s"] == 2000.0
    near = channel_map_nearest(cube, 2049.0)
    assert near.metadata["channel_indices"] == [20]
    assert channel_map_nearest(cube, 2051.0).metadata["channel_indices"] == [21]
    assert nearest_channel_index(cube, -1e9) == 0 and nearest_channel_index(cube, 1e9) == 49
    interval = channel_map(cube, 2000.0, 400.0)         # [1800, 2200] inclusive centres -> indices 18..22
    assert interval.metadata["channel_indices"] == [18, 19, 20, 21, 22]
    assert interval.value[1, 1] == pytest.approx(20.0)
    assert interval.metadata["selected_velocity_min_m_s"] == 1800.0
    with pytest.raises(ValueError):
        channel_map(cube, 2050.0, 10.0)                 # no channel centre inside -> explicit error, not empty data
    with pytest.raises(ValueError):
        channel_map_nearest(cube, float("nan"))


def test_channel_map_uncertainty_equals_cube_channel_uncertainty():
    """Section 104: a single-channel map must carry the cube's own sigma, never a recomputed one."""
    rng = np.random.default_rng(3)
    v = np.arange(0, 30) * 100.0
    cube = make_cube(v, lambda x: x / 100.0)
    cube.uncertainty[:] = rng.uniform(0.01, 0.2, cube.uncertainty.shape)
    for idx in (0, 7, 29):
        ch = channel_map_nearest(cube, v[idx])
        assert np.array_equal(ch.uncertainty, cube.uncertainty[idx])
        assert np.array_equal(ch.value, cube.relative_intensity[idx])
    multi = channel_map(cube, 1000.0, 400.0)
    n = multi.metadata["n_channels"]
    idx = multi.metadata["channel_indices"]
    assert np.allclose(multi.uncertainty, np.sqrt(np.sum(cube.uncertainty[idx] ** 2, axis=0)) / n)


def test_bin_bounds_tile_the_axis_and_survive_descending_order():
    v = np.array([0.0, 10.0, 30.0, 60.0])
    lo, hi = bin_bounds(v)
    assert np.allclose(lo, [-5.0, 5.0, 20.0, 45.0]) and np.allclose(hi, [5.0, 20.0, 45.0, 75.0])
    lo_d, hi_d = bin_bounds(v[::-1])
    assert np.allclose(lo_d, lo[::-1]) and np.allclose(hi_d, hi[::-1])
