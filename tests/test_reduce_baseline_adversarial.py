"""BASELINE ACCEPTANCE (2nd-pass sections 13-14): additional adversarial
synthetic scenarios beyond the first pass's single-line check - broad
HI-like feature, narrow line, multiple components, strong slope, ripple,
large masked region, RFI close to the scientific feature. Verifies the
baseline never eats the line, never invents a dip, never explodes with
masks, and measures the actual bias on an injected line.
"""
import numpy as np
import pytest

from hi_spectral_metric import HI_REST_HZ, frequency_to_radio_velocity
from reduce_engine.baseline import fit_baseline
from reduce_engine.config import ReduceConfig
from reduce_engine.masks import build_mask
from reduce_engine.models import MaskFlag
from reduce_engine.simulation import SyntheticCaptureConfig, build_synthetic_iq
from reduce_engine.spectral import estimate_spectrum

CENTER = 1_420_405_752.0


def _run(sim_config, reduce_config):
    iq = build_synthetic_iq(sim_config)
    estimate = estimate_spectrum(iq, sample_rate_hz=sim_config.sample_rate_hz,
                                 center_frequency_hz=sim_config.center_frequency_hz, config=reduce_config)
    mask_result = build_mask(estimate.frequency_hz, estimate.psd, center_frequency_hz=sim_config.center_frequency_hz,
                             config=reduce_config, segments=estimate.segments)
    baseline_result = fit_baseline(estimate.frequency_hz, estimate.psd, mask_result.mask, config=reduce_config)
    return estimate, mask_result, baseline_result


def _line_integral_bias(estimate, baseline_result, true_peak_fraction, true_fwhm_km_s, true_velocity_km_s):
    velocity = frequency_to_radio_velocity(estimate.frequency_hz, rest_hz=HI_REST_HZ)
    window = np.abs(velocity - true_velocity_km_s) <= true_fwhm_km_s * 1.5
    residual_fraction = (estimate.psd - baseline_result.baseline) / baseline_result.baseline
    dv = float(np.median(np.abs(np.diff(velocity))))
    measured_integral = float(np.sum(residual_fraction[window]) * dv)
    sigma_km_s = true_fwhm_km_s / 2.354820045
    expected_integral = true_peak_fraction * sigma_km_s * np.sqrt(2 * np.pi)
    return measured_integral, expected_integral


def test_broad_hi_like_feature_is_not_eaten_by_the_baseline():
    reduce_config = ReduceConfig(fft_size=4096, baseline_degree=2)
    sim_config = SyntheticCaptureConfig(fft_size=4096, n_segments=32, hi_line_peak_fraction=0.5,
                                        hi_line_fwhm_km_s=120.0, hi_line_velocity_km_s=0.0)
    estimate, _, baseline_result = _run(sim_config, reduce_config)
    measured, expected = _line_integral_bias(estimate, baseline_result, 0.5, 120.0, 0.0)
    assert measured > 0, "a real broad feature must not be fit away entirely"
    assert measured > expected * 0.3, "baseline is eating too much of a broad feature"


def test_narrow_line_recovered_without_bias_explosion():
    reduce_config = ReduceConfig(fft_size=4096, baseline_degree=2)
    sim_config = SyntheticCaptureConfig(fft_size=4096, n_segments=32, hi_line_peak_fraction=0.4,
                                        hi_line_fwhm_km_s=8.0, hi_line_velocity_km_s=20.0)
    estimate, _, baseline_result = _run(sim_config, reduce_config)
    measured, expected = _line_integral_bias(estimate, baseline_result, 0.4, 8.0, 20.0)
    assert measured > 0
    assert abs(measured - expected) / expected < 1.0  # order-of-magnitude sane, not exact


def test_strong_bandpass_slope_does_not_invent_a_line():
    """No injected line at all - a strong slope must not create a false
    positive "dip" or "bump" the baseline fails to absorb."""
    reduce_config = ReduceConfig(fft_size=4096, baseline_degree=2)
    sim_config = SyntheticCaptureConfig(fft_size=4096, n_segments=32, bandpass_slope=0.8, hi_line_peak_fraction=0.0)
    estimate, mask_result, baseline_result = _run(sim_config, reduce_config)
    from reduce_engine.masks import usable_bin_mask
    usable = usable_bin_mask(mask_result.mask)
    residual_fraction = (estimate.psd - baseline_result.baseline) / baseline_result.baseline
    assert float(np.std(residual_fraction[usable])) < 0.5, "slope leaked into residual - baseline underfitting"


def test_ripple_does_not_get_misread_as_multiple_lines():
    reduce_config = ReduceConfig(fft_size=4096, baseline_degree=2)
    sim_config = SyntheticCaptureConfig(fft_size=4096, n_segments=32, bandpass_ripple_amplitude=0.15,
                                        bandpass_ripple_period_hz=300_000.0, hi_line_peak_fraction=0.0)
    estimate, mask_result, baseline_result = _run(sim_config, reduce_config)
    from reduce_engine.masks import usable_bin_mask
    usable = usable_bin_mask(mask_result.mask)
    residual_fraction = (estimate.psd - baseline_result.baseline) / baseline_result.baseline
    # a low-order polynomial baseline cannot follow fast ripple by design
    # (documented limitation) - it should not, however, blow up or go
    # non-finite.
    assert np.all(np.isfinite(residual_fraction[usable]))


def test_large_masked_region_does_not_make_the_fit_explode():
    reduce_config = ReduceConfig(fft_size=4096, baseline_degree=1)
    sim_config = SyntheticCaptureConfig(fft_size=4096, n_segments=32, hi_line_peak_fraction=0.3,
                                        hi_line_fwhm_km_s=20.0)
    estimate, mask_result, _ = _run(sim_config, reduce_config)
    mask = mask_result.mask.copy()
    n = mask.shape[0]
    mask[: int(n * 0.6)] |= MaskFlag.USER_EXCLUDED.value  # 60% of the band masked out
    baseline_result = fit_baseline(estimate.frequency_hz, estimate.psd, mask, config=reduce_config)
    assert np.all(np.isfinite(baseline_result.baseline))
    assert not np.any(baseline_result.fit_mask & (mask != MaskFlag.GOOD.value))


def test_large_masked_region_raises_when_truly_insufficient():
    reduce_config = ReduceConfig(fft_size=1024, baseline_degree=2)
    sim_config = SyntheticCaptureConfig(fft_size=1024, n_segments=8, hi_line_peak_fraction=0.3)
    estimate, mask_result, _ = _run(sim_config, reduce_config)
    mask = mask_result.mask.copy()
    mask[:] |= MaskFlag.USER_EXCLUDED.value
    mask[:20] = MaskFlag.GOOD.value  # far too few bins for degree-2
    with pytest.raises(ValueError):
        fit_baseline(estimate.frequency_hz, estimate.psd, mask, config=reduce_config)


def test_rfi_close_to_scientific_feature_does_not_bias_the_line():
    reduce_config = ReduceConfig(fft_size=4096, baseline_degree=2)
    rfi_hz = CENTER + 40_000.0  # close to, but outside, the +-150 km/s line window used by fit_baseline's default
    sim_config = SyntheticCaptureConfig(fft_size=4096, n_segments=32, hi_line_peak_fraction=0.4,
                                        hi_line_fwhm_km_s=15.0, hi_line_velocity_km_s=0.0,
                                        rfi_lines_hz=(rfi_hz,), rfi_amplitude=25.0)
    estimate, mask_result, baseline_result = _run(sim_config, reduce_config)
    measured, expected = _line_integral_bias(estimate, baseline_result, 0.4, 15.0, 0.0)
    assert measured > expected * 0.3, "nearby RFI biased the line measurement down too much"
    assert measured < expected * 3.0, "nearby RFI biased the line measurement up too much"


def test_multi_component_line_both_peaks_survive_baseline_fit():
    """Two blended HI-like components, injected directly in PSD space via
    hi_spectral_metric.inject_gaussian_psd (an existing, reused primitive,
    not reimplemented here) onto a real noise+slope capture. Baseline
    must not treat the valley between the two peaks as continuum (both
    are still inside the +-150 km/s line window fit_baseline excludes)."""
    from hi_spectral_metric import inject_gaussian_psd
    reduce_config = ReduceConfig(fft_size=4096, baseline_degree=2)
    sim_config = SyntheticCaptureConfig(fft_size=4096, n_segments=32, bandpass_slope=0.2, hi_line_peak_fraction=0.0)
    estimate, mask_result, baseline_result = _run(sim_config, reduce_config)

    psd_two_lines = inject_gaussian_psd(estimate.psd, baseline_result.baseline, estimate.frequency_hz,
                                        center_velocity_km_s=-25.0, fwhm_km_s=10.0, peak_fraction=0.35)
    psd_two_lines = inject_gaussian_psd(psd_two_lines, baseline_result.baseline, estimate.frequency_hz,
                                        center_velocity_km_s=25.0, fwhm_km_s=10.0, peak_fraction=0.35)

    mask_result_2 = build_mask(estimate.frequency_hz, psd_two_lines, center_frequency_hz=sim_config.center_frequency_hz,
                               config=reduce_config, segments=estimate.segments)
    baseline_result_2 = fit_baseline(estimate.frequency_hz, psd_two_lines, mask_result_2.mask, config=reduce_config)
    residual = (psd_two_lines - baseline_result_2.baseline) / baseline_result_2.baseline
    velocity = frequency_to_radio_velocity(estimate.frequency_hz, rest_hz=HI_REST_HZ)

    peak_a = float(np.max(residual[np.abs(velocity - (-25.0)) <= 8.0]))
    peak_b = float(np.max(residual[np.abs(velocity - 25.0) <= 8.0]))
    valley = float(np.median(residual[np.abs(velocity) <= 8.0]))
    assert peak_a > 0.15 and peak_b > 0.15, "one or both injected components were eaten by the baseline"
    assert valley > -0.15, "baseline over-fit a false dip between the two real components"
