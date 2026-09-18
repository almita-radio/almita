"""Fase 9-10: baseline/RFI/moment pipeline test matrix."""
import numpy as np
import pytest

from alignment_engine.hi.spectral_pipeline import SpectralPipelineConfig, compute_spectral_metric
from alignment_engine.hi.spectral_simulation import GaussianComponent, SpectralSimConfig, generate_spectrum

CFG = SpectralPipelineConfig(line_window_km_s=(-60.0, 60.0), baseline_exclusion_km_s=(-60.0, 60.0),
                              baseline_order=1)


def test_flat_baseline_no_line_gives_metric_near_zero():
    sim = SpectralSimConfig(components=[GaussianComponent(0.0, 0.0, 20.0)], gain=1.0,
                             baseline_coefficients=(2.0,), noise_std=0.01, seed=1)
    v, s, rfi = generate_spectrum(sim)
    result = compute_spectral_metric(v, s, CFG, rfi_mask=rfi)
    assert result.metric_valid
    assert abs(result.metric) < 5.0  # near zero given a true zero-amplitude "line"


def test_sloped_baseline_is_correctly_removed():
    sim = SpectralSimConfig(components=[GaussianComponent(0.0, 0.0, 20.0)], baseline_coefficients=(1.0, 0.05),
                             noise_std=0.01, seed=2)
    v, s, rfi = generate_spectrum(sim)
    result = compute_spectral_metric(v, s, CFG, rfi_mask=rfi)
    assert result.metric_valid
    # np.polyfit's own convention: highest degree first, so for a linear
    # fit coefficients[0] is the slope, coefficients[1] the intercept.
    assert abs(result.baseline_coefficients[0] - 0.05) < 0.01  # slope recovered
    assert abs(result.metric) < 5.0


def test_curved_baseline_needs_higher_order_or_biases_the_metric():
    sim = SpectralSimConfig(components=[GaussianComponent(0.0, 0.0, 20.0)],
                             baseline_coefficients=(1.0, 0.0, 0.01), noise_std=0.01, seed=3)
    v, s, rfi = generate_spectrum(sim)
    linear_cfg = SpectralPipelineConfig(line_window_km_s=(-60.0, 60.0), baseline_exclusion_km_s=(-60.0, 60.0),
                                         baseline_order=1)
    quad_cfg = SpectralPipelineConfig(line_window_km_s=(-60.0, 60.0), baseline_exclusion_km_s=(-60.0, 60.0),
                                       baseline_order=2)
    linear_result = compute_spectral_metric(v, s, linear_cfg, rfi_mask=rfi)
    quad_result = compute_spectral_metric(v, s, quad_cfg, rfi_mask=rfi)
    # The quadratic fit must do measurably better (smaller |metric| for a
    # true zero-amplitude line) than forcing a linear fit onto curvature.
    assert abs(quad_result.metric) < abs(linear_result.metric)


def test_real_hi_line_is_detected_with_expected_sign_and_scale():
    sim = SpectralSimConfig(components=[GaussianComponent(2.0, 0.0, 20.0)], gain=1.0,
                             baseline_coefficients=(0.5,), noise_std=0.02, seed=4)
    v, s, rfi = generate_spectrum(sim)
    result = compute_spectral_metric(v, s, CFG, rfi_mask=rfi)
    assert result.metric_valid
    # integral of a Gaussian amplitude=2, FWHM=20 -> sigma=20/2.3548=8.49,
    # integral = amplitude*sigma*sqrt(2*pi) ~= 2*8.49*2.5066 ~= 42.6
    assert result.metric == pytest.approx(42.6, rel=0.15)


def test_rfi_spike_is_excluded_not_zeroed_into_the_metric():
    sim = SpectralSimConfig(components=[GaussianComponent(1.0, 0.0, 20.0)], noise_std=0.02,
                             rfi_spike_count=10, rfi_spike_amplitude=50.0, seed=5)
    v, s, rfi = generate_spectrum(sim)
    with_mask = compute_spectral_metric(v, s, CFG, rfi_mask=rfi)
    without_mask = compute_spectral_metric(v, s, CFG, rfi_mask=None)
    assert with_mask.metric_valid
    # Without RFI masking the spikes should corrupt the metric far more.
    clean_sim = SpectralSimConfig(components=[GaussianComponent(1.0, 0.0, 20.0)], noise_std=0.02, seed=5)
    v2, s2, _ = generate_spectrum(clean_sim)
    clean_result = compute_spectral_metric(v2, s2, CFG, rfi_mask=None)
    assert abs(with_mask.metric - clean_result.metric) < abs(without_mask.metric - clean_result.metric)


def test_missing_channels_are_excluded_not_treated_as_zero():
    sim = SpectralSimConfig(components=[GaussianComponent(1.0, 0.0, 20.0)], noise_std=0.02,
                             missing_channel_fraction=0.2, seed=6)
    v, s, rfi = generate_spectrum(sim)
    assert np.any(np.isnan(s))
    result = compute_spectral_metric(v, s, CFG, rfi_mask=rfi)
    assert result.metric_valid
    assert result.usable_fraction < 1.0
    assert result.usable_fraction > 0.5


def test_insufficient_usable_channels_is_invalid_not_a_guessed_metric():
    sim = SpectralSimConfig(components=[GaussianComponent(1.0, 0.0, 20.0)], noise_std=0.02,
                             missing_channel_fraction=0.95, seed=7)
    v, s, rfi = generate_spectrum(sim)
    result = compute_spectral_metric(v, s, CFG, rfi_mask=rfi)
    assert result.metric_valid is False
    assert result.metric is None


def test_empty_spectrum_is_invalid():
    result = compute_spectral_metric(np.array([]), np.array([]), CFG)
    assert result.metric_valid is False


def test_all_nan_spectrum_is_invalid():
    v = np.linspace(-100, 100, 256)
    s = np.full(256, np.nan)
    result = compute_spectral_metric(v, s, CFG)
    assert result.metric_valid is False


def test_uncertainty_grows_with_noise():
    low_noise_sim = SpectralSimConfig(components=[GaussianComponent(1.0, 0.0, 20.0)], noise_std=0.01, seed=8)
    high_noise_sim = SpectralSimConfig(components=[GaussianComponent(1.0, 0.0, 20.0)], noise_std=0.2, seed=8)
    v1, s1, _ = generate_spectrum(low_noise_sim)
    v2, s2, _ = generate_spectrum(high_noise_sim)
    r1 = compute_spectral_metric(v1, s1, CFG)
    r2 = compute_spectral_metric(v2, s2, CFG)
    assert r1.uncertainty < r2.uncertainty
