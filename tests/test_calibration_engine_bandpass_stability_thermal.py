"""Bandpass characterization, temporal stability, averaging law, and
thermal correlation tests."""
import numpy as np
import pytest

from calibration_engine.bandpass import characterize_edge_rolloff, compute_bandpass, recommend_mask
from calibration_engine.simulation import InstrumentSimulationConfig, simulate_capture_iq
from calibration_engine.stability import (
    StabilitySample, analyze_averaging_law, analyze_stability,
)
from calibration_engine.thermal import (
    MIN_SAMPLES_FOR_CORRELATION, SensorCalibration, correlate_with_temperature, reading_record,
)


# ---------------------------------------------------------------- bandpass

def test_edge_rolloff_measured_not_fixed_at_ten_percent():
    values = np.ones(1000)
    values[:50] = np.linspace(0.0, 1.0, 50)   # real rolloff only in the first 5%
    mask = characterize_edge_rolloff(values, threshold_fraction=0.5)
    measured_fraction = mask[:100].sum() / 100.0
    assert measured_fraction < 0.5   # much less than a hypothetical fixed 10% convention would over-mask this shape
    assert not mask[500]              # interior untouched


def test_edge_rolloff_flat_band_masks_nothing():
    values = np.ones(500)
    mask = characterize_edge_rolloff(values)
    assert not mask.any()


def test_compute_bandpass_preserves_raw_psd_alongside_normalized():
    config = InstrumentSimulationConfig(seed=3, gain_linear=15.0, edge_rolloff_db=4.0, dc_spike_db=5.0)
    iq = simulate_capture_iq(config)
    result = compute_bandpass(iq, config.sample_rate_hz, config.center_frequency_hz)
    assert len(result.raw_psd) == len(result.normalized_bandpass) == config.fft_size
    assert any(result.dc_mask)               # DC spike should be caught by the DC mask
    assert 0.0 <= result.usable_band_fraction <= 1.0


def test_recommend_mask_flags_persistent_rfi_across_multiple_captures():
    frequency = None
    spectra = []
    for seed in range(6):
        config = InstrumentSimulationConfig(seed=seed, gain_linear=15.0, rfi_spike_relative_freqs=[0.25],
                                             rfi_spike_db=20.0)
        iq = simulate_capture_iq(config)
        result = compute_bandpass(iq, config.sample_rate_hz, config.center_frequency_hz)
        frequency = np.asarray(result.frequency_hz)
        spectra.append(np.asarray(result.raw_psd))
    dc = np.asarray(result.dc_mask)
    edge = np.asarray(result.edge_mask)
    mask_result = recommend_mask(frequency, np.asarray(spectra), dc, edge)
    assert mask_result["auto_applied"] is False
    assert mask_result["status"] == "DRAFT_FOR_HUMAN_REVIEW"
    assert len(mask_result["candidate_regions"]) >= 1


def test_recommend_mask_never_auto_applies():
    frequency = np.linspace(1e9, 1.1e9, 200)
    spectra = np.ones((5, 200))
    dc = np.zeros(200, dtype=bool)
    edge = np.zeros(200, dtype=bool)
    result = recommend_mask(frequency, spectra, dc, edge)
    assert result["auto_applied"] is False


# ---------------------------------------------------------------- stability

def test_stability_flat_series_has_near_zero_rms_fraction():
    samples = [StabilitySample(f"t{i}", 100.0, float(i * 60)) for i in range(10)]
    result = analyze_stability(samples)
    assert result.power_rms_fraction < 1e-9


def test_stability_detects_warmup_when_early_noise_is_worse():
    rng = np.random.default_rng(0)
    samples = []
    for i in range(40):
        elapsed = float(i * 30)
        noise = rng.normal(0, 10.0 if elapsed < 600 else 0.5)
        samples.append(StabilitySample(f"t{i}", 100.0 + noise, elapsed))
    result = analyze_stability(samples, warmup_window_seconds=600.0)
    assert result.warmup_detected is True


def test_stability_no_warmup_when_uniformly_noisy():
    rng = np.random.default_rng(1)
    samples = [StabilitySample(f"t{i}", 100.0 + rng.normal(0, 1.0), float(i * 30)) for i in range(40)]
    result = analyze_stability(samples, warmup_window_seconds=600.0)
    assert result.warmup_detected is False


def test_stability_requires_at_least_three_samples():
    with pytest.raises(ValueError):
        analyze_stability([StabilitySample("a", 1.0, 0.0), StabilitySample("b", 1.0, 1.0)])


def test_averaging_law_matches_reference_for_synthetic_one_over_sqrt_t():
    t = [1, 2, 4, 8, 16, 32]
    sigma = [1.0 / np.sqrt(x) for x in t]
    result = analyze_averaging_law(t, sigma)
    assert result.deviation_from_reference < 0.05
    assert "stationary" in result.interpretation


def test_averaging_law_flags_slow_improvement_as_non_stationary():
    t = [1, 2, 4, 8, 16, 32]
    sigma = [1.0, 0.95, 0.90, 0.87, 0.85, 0.84]   # barely improves - drift/RFI candidate
    result = analyze_averaging_law(t, sigma)
    assert result.measured_slope > -0.2
    assert "SLOWER" in result.interpretation


def test_averaging_law_rejects_nonpositive_inputs():
    with pytest.raises(ValueError):
        analyze_averaging_law([1, 2, -4], [1.0, 0.7, 0.5])


# ---------------------------------------------------------------- thermal

def test_correlation_refuses_below_minimum_sample_count():
    result = correlate_with_temperature([20.0, 21.0], [1.0, 1.1], "rms")
    assert result.pearson_r is None
    assert result.n_samples < MIN_SAMPLES_FOR_CORRELATION


def test_correlation_ignores_none_pairs():
    temps = [20.0, None, 22.0, 23.0, 24.0, 25.0]
    metrics = [1.0, 5.0, 1.1, 1.2, 1.3, 1.4]
    result = correlate_with_temperature(temps, metrics, "rms")
    assert result.n_samples == 5


def test_correlation_undefined_when_zero_variance():
    result = correlate_with_temperature([20.0] * 6, [1.0, 1.1, 0.9, 1.2, 1.0, 1.05], "rms")
    assert result.pearson_r is None
    assert "zero variance" in result.confidence_note


def test_reading_record_never_fabricates_a_value_for_invalid_reading():
    invalid_reading = {"sensor_id": "28-abc", "temperature_c": None, "valid": False, "timestamp_utc": "t"}
    record = reading_record("sdr", invalid_reading)
    assert record["temperature_raw_c"] is None
    assert record["temperature_corrected_c"] is None
    assert record["valid"] is False


def test_reading_record_corrected_only_when_sensor_calibration_is_actually_calibrated():
    reading = {"sensor_id": "28-abc", "temperature_c": 20.0, "valid": True, "timestamp_utc": "t"}
    uncalibrated = SensorCalibration(rom_id="28-abc", role="sdr")
    record = reading_record("sdr", reading, uncalibrated)
    assert record["temperature_raw_c"] == 20.0
    assert record["temperature_corrected_c"] is None

    calibrated = SensorCalibration(rom_id="28-abc", role="sdr", offset_c=0.5, slope=1.0, calibrated=True)
    record2 = reading_record("sdr", reading, calibrated)
    assert record2["temperature_corrected_c"] == pytest.approx(20.5)
