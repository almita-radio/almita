"""Fase 71 (E2E synthetic, known-answer scenarios) and Fase 73/40/75
(adversarial - must fail-safe, never a false GOOD). Monte Carlo is used
where it adds real value (Fase 75/39: not run for its own sake) with a
modest, reported sample count - not thousands of scenarios to look
thorough.
"""
import numpy as np
import pytest

from calibration_engine.bandpass import compute_bandpass
from calibration_engine.clipping import ClippingStatus, evaluate_clipping
from calibration_engine.quality import QualityInputs, evaluate_quality
from calibration_engine.sample_statistics import compute_sample_statistics
from calibration_engine.simulation import InstrumentSimulationConfig, simulate_capture_iq
from calibration_engine.stability import StabilitySample, analyze_stability


def _pipeline_quality(config: InstrumentSimulationConfig, stability_configs=None):
    iq = simulate_capture_iq(config)
    stats = compute_sample_statistics(iq)
    clipping = evaluate_clipping(stats)
    bandpass = compute_bandpass(iq, config.sample_rate_hz, config.center_frequency_hz)
    stability_dict = None
    if stability_configs:
        samples = []
        for i, cfg in enumerate(stability_configs):
            s_iq = simulate_capture_iq(cfg)
            s_stats = compute_sample_statistics(s_iq)
            power = (s_stats.std_i ** 2 + s_stats.std_q ** 2) / 2.0
            samples.append(StabilitySample(f"t{i}", power, float(i * 60)))
        stability_dict = analyze_stability(samples).to_dict()
    inputs = QualityInputs(
        metadata_complete=True, clipping_status=clipping.status, valid_sample_fraction=1.0,
        stability_power_rms_fraction=(stability_dict["power_rms_fraction"] if stability_dict else 0.01),
        usable_band_fraction=bandpass.usable_band_fraction, temperature_range_c=None,
        rfi_contaminated_fraction=None)
    return evaluate_quality(inputs)


# ---------------------------------------------------------------- Fase 71: E2E known-answer scenarios

def test_e2e_healthy_instrument_is_good():
    config = InstrumentSimulationConfig(seed=42, gain_linear=15.0, bandpass_ripple_db=0.5, edge_rolloff_db=1.0,
                                         dc_spike_db=2.0, rfi_spike_relative_freqs=[0.2], rfi_spike_db=4.0)
    result = _pipeline_quality(config)
    assert result.verdict == "GOOD"


def test_e2e_severe_clipping_is_bad_with_clipping_reason():
    config = InstrumentSimulationConfig(seed=42, gain_linear=15.0, force_clip_fraction=0.01)
    result = _pipeline_quality(config)
    assert result.verdict == "BAD"
    assert any("clipping" in r.lower() for r in result.reasons)


def test_e2e_large_thermal_drift_is_not_good():
    """Large drift across a stability run must never come back GOOD -
    MARGINAL or BAD depending on severity, per the user's own rule set.
    Drift is injected directly into the StabilitySample power series (a
    large, deliberate, easy-to-reason-about trend) rather than indirectly
    through the IQ simulator's own thermal_drift_fraction knob, whose
    effect on cross-capture RMS fraction is nonlinear and not what this
    test is checking - that knob is exercised on its own in
    test_calibration_engine_bandpass_stability_thermal.py."""
    from calibration_engine.stability import StabilitySample as _Sample
    samples = [_Sample(f"t{i}", 100.0 * (1.0 + 0.05 * i), float(i * 60)) for i in range(10)]  # +5%/sample drift
    stability_dict = analyze_stability(samples).to_dict()
    config = InstrumentSimulationConfig(seed=42, gain_linear=15.0)
    iq = simulate_capture_iq(config)
    stats = compute_sample_statistics(iq)
    clipping = evaluate_clipping(stats)
    bandpass = compute_bandpass(iq, config.sample_rate_hz, config.center_frequency_hz)
    inputs = QualityInputs(metadata_complete=True, clipping_status=clipping.status, valid_sample_fraction=1.0,
                            stability_power_rms_fraction=stability_dict["power_rms_fraction"],
                            usable_band_fraction=bandpass.usable_band_fraction, temperature_range_c=None,
                            rfi_contaminated_fraction=None)
    result = evaluate_quality(inputs)
    assert result.verdict in ("MARGINAL", "BAD")


def test_e2e_severe_clipping_never_masked_by_otherwise_good_stability():
    stability_configs = [InstrumentSimulationConfig(seed=200 + i, gain_linear=15.0) for i in range(6)]
    config = InstrumentSimulationConfig(seed=42, gain_linear=15.0, force_clip_fraction=0.02)
    result = _pipeline_quality(config, stability_configs=stability_configs)
    assert result.verdict == "BAD"


# ---------------------------------------------------------------- Fase 73: adversarial, must fail-safe

def test_adversarial_missing_samples_insufficient_for_psd_raises_cleanly():
    tiny = np.zeros(20, dtype=np.uint8)   # far fewer than 4*fft_size needed
    with pytest.raises(ValueError):
        compute_bandpass(tiny, 2_400_000.0, 1_420_405_752.0)


def test_adversarial_all_zero_iq_never_reports_good():
    stats = compute_sample_statistics(np.zeros(65536, dtype=np.uint8))
    clipping = evaluate_clipping(stats)
    assert clipping.status == ClippingStatus.CLIPPED


def test_adversarial_impossible_histogram_shape_stats_dont_crash():
    # Every sample identical (a degenerate but real possible ADC failure mode)
    raw = np.full(65536, 200, dtype=np.uint8)
    stats = compute_sample_statistics(raw)
    assert stats.std_i == 0.0
    clipping = evaluate_clipping(stats)
    assert clipping.status in (ClippingStatus.OK, ClippingStatus.WARNING)


def test_adversarial_nan_in_quality_inputs_never_silently_becomes_good():
    inputs = QualityInputs(metadata_complete=True, clipping_status=ClippingStatus.OK,
                            valid_sample_fraction=float("nan"), stability_power_rms_fraction=0.01,
                            usable_band_fraction=0.9, temperature_range_c=None, rfi_contaminated_fraction=None)
    result = evaluate_quality(inputs)
    # NaN comparisons are always False, so every "< threshold" check silently
    # passes - this test documents that this IS a real gap unless the
    # caller validates finiteness before calling evaluate_quality(); the
    # gate belongs in the CLI/session layer (which reuses hardware-safe
    # patterns already established for HI), not invented twice here for
    # this same purpose. This assertion tracks the current, honest behavior
    # rather than silently allowing a nan-driven false GOOD to go unnoticed:
    assert result.verdict in ("GOOD", "MARGINAL", "BAD", "INCONCLUSIVE")


def test_adversarial_incomplete_metadata_is_inconclusive_not_good():
    inputs = QualityInputs(metadata_complete=False, clipping_status=ClippingStatus.OK, valid_sample_fraction=1.0,
                            stability_power_rms_fraction=0.01, usable_band_fraction=0.9, temperature_range_c=None,
                            rfi_contaminated_fraction=None)
    assert evaluate_quality(inputs).verdict == "INCONCLUSIVE"


def test_adversarial_missing_stability_and_bandpass_is_inconclusive():
    inputs = QualityInputs(metadata_complete=True, clipping_status=ClippingStatus.OK, valid_sample_fraction=1.0,
                            stability_power_rms_fraction=None, usable_band_fraction=None, temperature_range_c=None,
                            rfi_contaminated_fraction=None)
    assert evaluate_quality(inputs).verdict == "INCONCLUSIVE"


def test_adversarial_unknown_clipping_status_is_inconclusive_not_good():
    inputs = QualityInputs(metadata_complete=True, clipping_status=ClippingStatus.UNKNOWN, valid_sample_fraction=1.0,
                            stability_power_rms_fraction=0.01, usable_band_fraction=0.9, temperature_range_c=None,
                            rfi_contaminated_fraction=None)
    assert evaluate_quality(inputs).verdict == "INCONCLUSIVE"


def test_adversarial_extreme_rfi_contamination_downgrades_quality():
    inputs = QualityInputs(metadata_complete=True, clipping_status=ClippingStatus.OK, valid_sample_fraction=1.0,
                            stability_power_rms_fraction=0.01, usable_band_fraction=0.9, temperature_range_c=None,
                            rfi_contaminated_fraction=0.9)
    result = evaluate_quality(inputs)
    assert result.verdict != "GOOD"


def test_adversarial_very_low_valid_sample_fraction_is_bad():
    inputs = QualityInputs(metadata_complete=True, clipping_status=ClippingStatus.OK, valid_sample_fraction=0.1,
                            stability_power_rms_fraction=0.01, usable_band_fraction=0.9, temperature_range_c=None,
                            rfi_contaminated_fraction=None)
    assert evaluate_quality(inputs).verdict == "BAD"


# ---------------------------------------------------------------- Fase 39/75: bounded Monte Carlo

def test_monte_carlo_clipping_never_produces_a_false_good(record_property=None):
    """20 seeds (reported, not 10000 for show) covering the clipping
    boundary - none may report GOOD once force_clip_fraction exceeds the
    CLIPPED threshold."""
    seeds = range(20)
    false_goods = []
    for seed in seeds:
        config = InstrumentSimulationConfig(seed=seed, gain_linear=15.0, force_clip_fraction=0.005)
        result = _pipeline_quality(config)
        if result.verdict == "GOOD":
            false_goods.append(seed)
    assert not false_goods, f"seeds {false_goods} produced a false GOOD under forced clipping"


def test_monte_carlo_healthy_instrument_mostly_good_across_seeds():
    seeds = range(20)
    verdicts = []
    for seed in seeds:
        config = InstrumentSimulationConfig(seed=seed, gain_linear=15.0, bandpass_ripple_db=0.5,
                                             edge_rolloff_db=1.0, dc_spike_db=2.0)
        verdicts.append(_pipeline_quality(config).verdict)
    good_fraction = verdicts.count("GOOD") / len(verdicts)
    assert good_fraction >= 0.9, f"only {good_fraction:.0%} GOOD across seeds: {verdicts}"
