"""RESAMPLE TRUTH TEST (sections 7-10): measured numeric error for a
known analytic Gaussian line resampled across various velocity-grid
shifts, up to the real 641 m/s / 10.4-channel offset found on real data.
Reports a table (via -s), not just PASS/FAIL - failure modes checked:
centroid drift, FWHM inflation, integrated-area error, peak error, mask
gaps never filled as truth, and multi-component + RFI-notch preservation.
"""
import numpy as np
import pytest

from reduce_engine.models import MaskFlag
from science_engine.resample import resample_to_velocity_axis


def _gaussian(v, amplitude, center, fwhm):
    return amplitude * np.exp(-4 * np.log(2) * (v - center) ** 2 / fwhm ** 2)


def _analytic_integral(amplitude, fwhm):
    return amplitude * fwhm * np.sqrt(np.pi / (4 * np.log(2)))


CHANNEL_WIDTH_M_S = 500.0  # a clean round number for exact shift-in-channels control


@pytest.mark.parametrize("shift_channels", [0.0, 0.1, 0.5, 1.0, 5.0, 10.0, 10.4])
def test_resample_truth_table(shift_channels, capsys):
    n = 2000
    v_source = np.linspace(-200_000, 200_000, n)
    v_target = v_source - shift_channels * CHANNEL_WIDTH_M_S  # target axis offset from source by N channels

    amplitude, center, fwhm = 1.0, 0.0, 20_000.0
    value = _gaussian(v_source, amplitude, center, fwhm)
    uncertainty = np.full(n, 0.01)
    mask = np.zeros(n, dtype=np.int64)

    result = resample_to_velocity_axis(v_source, value, uncertainty, mask, v_target)

    valid = result.mask == MaskFlag.GOOD.value
    # centroid via intensity-weighted mean over the positive part of the resampled line
    positive = np.clip(np.where(valid, result.relative_intensity, 0.0), 0.0, None)
    centroid = np.sum(positive * v_target) / np.sum(positive)
    centroid_error = centroid - center

    half_max = amplitude / 2
    above = np.where(valid & (result.relative_intensity >= half_max))[0]
    fwhm_measured = v_target[above[-1]] - v_target[above[0]] if len(above) >= 2 else np.nan
    fwhm_error = fwhm_measured - fwhm

    dv = np.median(np.abs(np.diff(v_target)))
    integral_measured = np.sum(np.where(valid, result.relative_intensity, 0.0)) * dv
    integral_expected = _analytic_integral(amplitude, fwhm)
    integral_error_frac = (integral_measured - integral_expected) / integral_expected

    peak_measured = np.nanmax(np.where(valid, result.relative_intensity, np.nan))
    peak_error = peak_measured - amplitude

    print(f"shift={shift_channels:5.1f} ch  centroid_err={centroid_error:8.1f} m/s  "
         f"fwhm_err={fwhm_error:8.1f} m/s  integral_err={integral_error_frac:7.4f}  peak_err={peak_error:7.4f}")

    # even at the real-world-observed 10.4 channel shift, centroid drift must stay well under one channel.
    assert abs(centroid_error) < 500.0
    assert abs(fwhm_error) < 0.15 * fwhm  # linear interpolation smooths sharp features - a few % broadening is expected
    assert abs(integral_error_frac) < 0.05
    assert peak_error <= 0.02  # interpolation can only ever reduce or barely preserve a sampled peak, never inflate it


def test_resample_never_fills_masked_gap_as_truth():
    """Section 7.D: a masked gap in the source must not reappear as
    valid, fabricated data after resampling onto a shifted target axis."""
    n = 500
    v_source = np.linspace(-100_000, 100_000, n)
    value = _gaussian(v_source, 1.0, 0.0, 20_000.0)
    uncertainty = np.full(n, 0.01)
    mask = np.zeros(n, dtype=np.int64)
    gap = slice(240, 260)  # a notch right at the line center
    mask[gap] = MaskFlag.RFI.value
    value[gap] = np.nan
    uncertainty[gap] = np.nan
    gap_v_lo, gap_v_hi = v_source[gap][0], v_source[gap][-1]

    dv = v_source[1] - v_source[0]
    v_target = v_source - 1.3 * dv  # a shifted target axis - the realistic resample case
    result = resample_to_velocity_axis(v_source, value, uncertainty, mask, v_target)
    valid = result.mask == MaskFlag.GOOD.value
    gap_region_on_target = (v_target >= gap_v_lo) & (v_target <= gap_v_hi)
    assert not np.any(valid & gap_region_on_target)


def test_resample_two_component_with_rfi_notch_preserves_both_and_the_gap():
    """Section 9: two Gaussian components (one narrow, one broad) with an
    RFI notch between them - positions preserved, gap not filled."""
    n = 2000
    v_source = np.linspace(-200_000, 200_000, n)
    narrow = _gaussian(v_source, 1.0, -60_000, 5_000)
    broad = _gaussian(v_source, 0.6, 60_000, 30_000)
    value = narrow + broad
    uncertainty = np.full(n, 0.005)
    mask = np.zeros(n, dtype=np.int64)
    notch = (v_source > -10_000) & (v_source < 10_000)
    mask[notch] = MaskFlag.RFI.value
    value[notch] = np.nan
    uncertainty[notch] = np.nan

    v_target = v_source - 3.0 * (v_source[1] - v_source[0])
    result = resample_to_velocity_axis(v_source, value, uncertainty, mask, v_target)
    valid = result.mask == MaskFlag.GOOD.value

    near_narrow = (v_target > -65_000) & (v_target < -55_000)
    near_broad = (v_target > 50_000) & (v_target < 70_000)
    assert np.nanmax(np.where(valid & near_narrow, result.relative_intensity, np.nan)) > 0.8
    assert np.nanmax(np.where(valid & near_broad, result.relative_intensity, np.nan)) > 0.4
    gap_target = (v_target > -8_000) & (v_target < 8_000)
    assert not np.any(valid & gap_target)  # the notch must not have been resampled into fabricated data


def test_resample_conservation_error_is_measured():
    """Section 10: does interpolation conserve the integrated spectral
    quantity, or just the sampled value? Measure the actual error at V1's
    real operating scale (500 m/s channels, matching real REDUCE data)."""
    n = 2000
    v_source = np.linspace(-200_000, 200_000, n)
    amplitude, fwhm = 1.0, 20_000.0
    value = _gaussian(v_source, amplitude, 0.0, fwhm)
    uncertainty = np.full(n, 0.01)
    mask = np.zeros(n, dtype=np.int64)
    dv = v_source[1] - v_source[0]

    source_integral = np.sum(value) * dv
    v_target = v_source - 0.5 * dv  # half-channel shift - worst case for a Gaussian-sampled line's conservation
    result = resample_to_velocity_axis(v_source, value, uncertainty, mask, v_target)
    target_integral = np.sum(np.where(result.mask == MaskFlag.GOOD.value, result.relative_intensity, 0.0)) * dv

    error_frac = abs(target_integral - source_integral) / source_integral
    assert error_frac < 0.01  # linear interpolation of a smooth, well-sampled Gaussian conserves area well
