"""RESAMPLE ACCEPTANCE (sections 7-10): brutal measurement of the velocity resampler with analytic truth.
Run with `pytest -s test_science_resample_strict.py` to print the measured tables that are copied into
docs/SCIENCE_ACCEPTANCE.md. Every number printed here is also asserted against a stated tolerance.

Measured quantities are computed WITHOUT the production resampler's own helpers: centroid by intensity-weighted
mean, FWHM by linear interpolation of the half-maximum crossings (sub-channel; not quantised to the grid),
integrated area by the trapezoid rule on the target axis, peak by the maximum.
"""
import numpy as np
import pytest

from reduce_engine.models import MaskFlag
from science_engine.resample import resample_to_velocity_axis

DV = 61.8343185  # real channel width (m/s) of the real REDUCE axis


def gauss(v, amp, center, fwhm):
    return amp * np.exp(-4 * np.log(2) * (v - center) ** 2 / fwhm ** 2)


def fwhm_by_crossings(v, y):
    """Sub-channel FWHM from linear interpolation of the two half-maximum crossings (v ascending)."""
    half = np.nanmax(y) / 2
    above = np.flatnonzero(y >= half)
    i0, i1 = above[0], above[-1]
    left = v[i0 - 1] + (half - y[i0 - 1]) * (v[i0] - v[i0 - 1]) / (y[i0] - y[i0 - 1])
    right = v[i1] + (half - y[i1]) * (v[i1 + 1] - v[i1]) / (y[i1 + 1] - y[i1])
    return right - left


def measure(shift_channels, fwhm_channels, n=4001, descending=False):
    v_src = (np.arange(n) - n // 2) * DV
    center = 0.0
    fwhm = fwhm_channels * DV
    y = gauss(v_src, 1.0, center, fwhm)
    unc = np.full(n, 0.01)
    mask = np.zeros(n, dtype=np.int64)
    v_tgt = v_src + shift_channels * DV
    src_order = slice(None, None, -1) if descending else slice(None)
    r = resample_to_velocity_axis(v_src[src_order], y[src_order], unc[src_order], mask[src_order], v_tgt)
    ok = r.mask == MaskFlag.GOOD.value
    v, val = v_tgt[ok], r.relative_intensity[ok]
    pos = np.clip(val, 0, None)
    centroid = float(np.sum(pos * v) / np.sum(pos))
    area = float(np.trapezoid(val, v)) if hasattr(np, "trapezoid") else float(np.trapz(val, v))
    area_true = 1.0 * fwhm * np.sqrt(np.pi / (4 * np.log(2)))
    return {"centroid_err_m_s": centroid - center, "fwhm_err_m_s": fwhm_by_crossings(v, val) - fwhm,
            "area_err_frac": area / area_true - 1.0, "peak_err": float(val.max()) - 1.0,
            "fwhm_err_frac": (fwhm_by_crossings(v, val) - fwhm) / fwhm}


SHIFTS = [0.1, 0.5, 1.0, 5.0, 10.0, 10.368, 17.425, 83.7]   # last three = real 9-pt max offset, real 9-pt spread, real 100-pt spread


def test_truth_table_broad_line_real_hi_scale(capsys):
    """Broad line (FWHM 100 channels ~ 6.2 km/s ~ a real HI feature)."""
    rows = []
    for shift in SHIFTS:
        m = measure(shift, 100.0)
        rows.append((shift, m))
        assert abs(m["centroid_err_m_s"]) < 0.02 * DV          # centroid preserved to 2% of a channel
        assert abs(m["fwhm_err_frac"]) < 1e-3                  # < 0.1% broadening for a well-sampled line
        assert abs(m["area_err_frac"]) < 1e-4                  # integrated area conserved to 0.01%
        assert m["peak_err"] <= 1e-9 and m["peak_err"] > -2e-3
    with capsys.disabled():
        print("\nBROAD LINE (FWHM=100 ch): shift[ch]  centroid_err[m/s]  fwhm_err[%]  area_err[%]  peak_err")
        for shift, m in rows:
            print(f"   {shift:7.3f}  {m['centroid_err_m_s']:12.5f}  {100*m['fwhm_err_frac']:10.5f}  "
                  f"{100*m['area_err_frac']:10.6f}  {m['peak_err']:9.6f}")


def test_truth_table_narrow_line_worst_case(capsys):
    """Narrow line (FWHM 4 channels): the worst case for linear interpolation. Documents the real limit."""
    rows = []
    for shift in [0.0, 0.1, 0.25, 0.5, 1.0, 10.368]:
        m = measure(shift, 4.0)
        rows.append((shift, m))
        assert abs(m["centroid_err_m_s"]) < 0.02 * DV
        assert abs(m["area_err_frac"]) < 5e-3     # area conserved to 0.5% even for a 4-channel line
        assert abs(m["fwhm_err_frac"]) < 0.10     # broadening bounded (<10%), worst at half-channel shift
    worst_peak = max(abs(m["peak_err"]) for _, m in rows)
    with capsys.disabled():
        print("\nNARROW LINE (FWHM=4 ch): shift[ch]  centroid_err[m/s]  fwhm_err[%]  area_err[%]  peak_err")
        for shift, m in rows:
            print(f"   {shift:7.3f}  {m['centroid_err_m_s']:12.5f}  {100*m['fwhm_err_frac']:10.4f}  "
                  f"{100*m['area_err_frac']:10.5f}  {m['peak_err']:9.5f}")
        print(f"   worst peak loss: {worst_peak:.4f}")


def test_descending_source_axis_gives_identical_result():
    """Real Level 1 axes are DESCENDING; the resampler sorts explicitly and must not care."""
    for shift in (0.3, 10.368):
        asc, desc = measure(shift, 30.0), measure(shift, 30.0, descending=True)
        for key in asc:
            assert asc[key] == pytest.approx(desc[key], abs=1e-12)


def test_edge_truncation_targets_outside_source_range_are_missing_not_extrapolated():
    n = 200
    v = np.arange(n) * DV
    y, u, mk = np.ones(n), np.full(n, 0.1), np.zeros(n, dtype=np.int64)
    target = v + 10.4 * DV                     # shifted up by 10.4 channels
    r = resample_to_velocity_axis(v, y, u, mk, target)
    outside = target > v[-1]
    assert outside.sum() == 11                                   # i + 10.4 > 199  <=>  i >= 189: exactly 11 bins past the end
    assert np.all(r.mask[outside] == MaskFlag.MISSING.value)
    assert np.all(np.isnan(r.relative_intensity[outside])) and np.all(np.isnan(r.uncertainty[outside]))
    assert np.all(r.mask[~outside] == MaskFlag.GOOD.value)
    assert np.allclose(r.relative_intensity[~outside], 1.0)      # a constant stays exactly constant (no edge bias)
    # target exactly ON the last source sample is still in range
    r2 = resample_to_velocity_axis(v, y, u, mk, np.array([v[-1]]))
    assert r2.mask[0] == MaskFlag.GOOD.value and r2.relative_intensity[0] == 1.0


def test_exact_hit_on_good_bin_ignores_a_masked_neighbour():
    n = 10
    v = np.arange(n) * 100.0
    y, u = np.arange(n, dtype=float), np.full(n, 0.5)
    mk = np.zeros(n, dtype=np.int64)
    mk[5] = MaskFlag.RFI.value
    y[5], u[5] = np.nan, np.nan
    r = resample_to_velocity_axis(v, y, u, mk, np.array([400.0, 500.0, 600.0, 450.0, 550.0]))
    assert r.mask.tolist() == [0, MaskFlag.MISSING.value, 0, MaskFlag.MISSING.value, MaskFlag.MISSING.value]
    assert r.relative_intensity[0] == 4.0 and r.relative_intensity[2] == 6.0     # exact hits, neighbour ignored


def test_regression_good_bin_with_nan_value_can_never_be_emitted():
    """The pre-hardening np.interp version emitted a GOOD bin with a NaN value when the target sat within 1e-3
    channel of a good bin next to a masked one, which then poisoned the cube voxel."""
    n = 50
    v = np.linspace(0, 4900.0, n)
    y, u, mk = np.ones(n), np.full(n, 0.1), np.zeros(n, dtype=np.int64)
    mk[20], y[20], u[20] = MaskFlag.RFI.value, np.nan, np.nan
    for eps in (0.0, 1e-12, 1e-9, 1e-6, 1e-4, 5e-4, 1e-3, 0.0999):
        tgt = np.array([v[19] + eps * 100.0])
        r = resample_to_velocity_axis(v, y, u, mk, tgt)
        good = r.mask == MaskFlag.GOOD.value
        assert np.all(np.isfinite(r.relative_intensity[good])) and np.all(np.isfinite(r.uncertainty[good]))
        assert good[0] == (eps == 0.0)          # only the exact hit stays GOOD; any leakage of the masked bin -> MISSING


def test_random_masks_never_produce_good_nonfinite_or_extrapolated_bins():
    rng = np.random.default_rng(7)
    for _ in range(200):
        n = int(rng.integers(5, 60))
        v = np.sort(rng.uniform(-1e4, 1e4, n)) + np.arange(n) * 1e-3
        y, u = rng.normal(size=n), rng.uniform(0.01, 1.0, n)
        mk = np.where(rng.random(n) < 0.3, MaskFlag.RFI.value, 0).astype(np.int64)
        y[mk != 0], u[mk != 0] = np.nan, np.nan
        tgt = rng.uniform(v[0] - 500, v[-1] + 500, 80)
        r = resample_to_velocity_axis(v, y, u, mk, tgt)
        good = r.mask == MaskFlag.GOOD.value
        assert np.all(np.isfinite(r.relative_intensity[good])) and np.all(np.isfinite(r.uncertainty[good]))
        assert not np.any(good & ((tgt < v[0]) | (tgt > v[-1])))
        assert np.all(np.isnan(r.relative_intensity[~good]))


def test_irregular_source_axis_is_interpolated_on_actual_positions():
    v = np.array([0.0, 10.0, 30.0, 60.0, 100.0])
    y = 2.0 * v + 1.0                     # exactly linear -> linear interpolation must be exact on ANY spacing
    r = resample_to_velocity_axis(v, y, np.ones(5), np.zeros(5, dtype=np.int64), np.array([5.0, 20.0, 45.0, 99.0]))
    assert np.allclose(r.relative_intensity, 2.0 * np.array([5.0, 20.0, 45.0, 99.0]) + 1.0)


def test_source_axis_must_be_strictly_monotonic_distinct():
    with pytest.raises(ValueError):
        resample_to_velocity_axis(np.array([0.0, 1.0, 1.0]), np.ones(3), np.ones(3), np.zeros(3, dtype=np.int64),
                                  np.array([0.5]))


def test_two_components_with_rfi_notch_positions_amplitudes_and_gap(capsys):
    v_src = (np.arange(4001) - 2000) * DV
    narrow = gauss(v_src, 1.0, -60_000.0, 5_000.0)
    broad = gauss(v_src, 0.6, 60_000.0, 30_000.0)
    y = narrow + broad
    u = np.full(v_src.shape, 0.005)
    mk = np.zeros(v_src.shape, dtype=np.int64)
    notch = np.abs(v_src) < 10_000.0
    mk[notch], y[notch], u[notch] = MaskFlag.RFI.value, np.nan, np.nan
    v_tgt = v_src + 10.368 * DV
    r = resample_to_velocity_axis(v_src, y, u, mk, v_tgt)
    good = r.mask == MaskFlag.GOOD.value
    n_win = (v_tgt > -66_000) & (v_tgt < -54_000)
    b_win = (v_tgt > 40_000) & (v_tgt < 80_000)
    n_pos = v_tgt[n_win & good][np.argmax(r.relative_intensity[n_win & good])]
    b_pos = v_tgt[b_win & good][np.argmax(r.relative_intensity[b_win & good])]
    n_amp, b_amp = np.nanmax(r.relative_intensity[n_win & good]), np.nanmax(r.relative_intensity[b_win & good])
    assert abs(n_pos + 60_000.0) < 1.5 * DV and abs(b_pos - 60_000.0) < 1.5 * DV
    assert abs(n_amp - 1.0) < 5e-3 and abs(b_amp - 0.6) < 1e-3
    inside_notch = (v_tgt > -9_000.0) & (v_tgt < 9_000.0)
    assert not np.any(good & inside_notch)            # the gap is never filled as truth
    grown = np.sum(~good & ~notch_target(v_tgt))      # mask growth: only the 1-channel dilation at the notch edges
    with capsys.disabled():
        print(f"\nTWO COMPONENTS + NOTCH: narrow pos err {n_pos + 60000:.1f} m/s amp {n_amp:.4f}; "
              f"broad pos err {b_pos - 60000:.1f} m/s amp {b_amp:.4f}; notch-edge dilated bins {grown}")


def notch_target(v):
    return np.abs(v) < 10_000.0 + DV * 1.5


def test_uncertainty_convention_is_measured_not_assumed(capsys):
    """Section 7E/35: Monte Carlo of the linear sigma-interpolation convention.
    Per-CHANNEL: reported sigma overstates the true resampled scatter by up to sqrt(2) at a half-channel shift.
    INTEGRATED: with an independent-channel sum the two effects cancel (the sum of a linear interpolant of white
    noise keeps its variance), so the integrated sigma is right. Both facts are asserted from simulation."""
    rng = np.random.default_rng(11)
    n, sigma, trials = 400, 0.1, 4000
    v = np.arange(n) * DV
    mask = np.zeros(n, dtype=np.int64)
    unc = np.full(n, sigma)
    out = {}
    for shift in (0.0, 0.25, 0.5):
        tgt = v[:-1] + shift * DV
        res = np.empty((trials, tgt.size))
        rep = None
        for t in range(trials):
            r = resample_to_velocity_axis(v, rng.normal(0, sigma, n), unc, mask, tgt)
            res[t] = r.relative_intensity
            rep = r.uncertainty
        per_channel_ratio = float(np.mean(np.std(res, axis=0)) / np.mean(rep))
        sums = res.sum(axis=1)
        integrated_ratio = float(np.std(sums) / np.sqrt(np.sum(rep ** 2)))
        out[shift] = (per_channel_ratio, integrated_ratio)
    with capsys.disabled():
        print("\nSIGMA CONVENTION MC (empirical/reported): shift[ch]  per-channel  integrated")
        for shift, (pc, ig) in out.items():
            print(f"   {shift:5.2f}   {pc:8.4f}   {ig:8.4f}")
    assert out[0.0][0] == pytest.approx(1.0, abs=0.03)
    assert out[0.5][0] == pytest.approx(1 / np.sqrt(2), abs=0.03)     # per-channel overstated by sqrt(2) at 0.5 ch
    for shift in out:
        assert out[shift][1] == pytest.approx(1.0, abs=0.04)          # integrated sigma correct at every shift
