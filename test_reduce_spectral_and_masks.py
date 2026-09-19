"""Unit tests for the SPECTRAL ESTIMATE and MASK stages, using
reduce_engine.simulation's synthetic IQ (known ground truth)."""
import numpy as np
import pytest

from reduce_engine.config import ReduceConfig
from reduce_engine.masks import build_mask, usable_bin_mask
from reduce_engine.models import MaskFlag
from reduce_engine.simulation import SyntheticCaptureConfig, build_synthetic_iq, expected_frequency_axis
from reduce_engine.spectral import estimate_spectrum


def test_frequency_axis_matches_expected_grid():
    sim_config = SyntheticCaptureConfig(fft_size=1024, n_segments=8)
    iq = build_synthetic_iq(sim_config)
    config = ReduceConfig(fft_size=1024)
    estimate = estimate_spectrum(iq, sample_rate_hz=sim_config.sample_rate_hz,
                                 center_frequency_hz=sim_config.center_frequency_hz, config=config)
    np.testing.assert_allclose(estimate.frequency_hz, expected_frequency_axis(sim_config))


def test_estimate_spectrum_is_deterministic_for_same_input():
    sim_config = SyntheticCaptureConfig(fft_size=512, n_segments=8)
    iq = build_synthetic_iq(sim_config)
    config = ReduceConfig(fft_size=512)
    a = estimate_spectrum(iq, sample_rate_hz=sim_config.sample_rate_hz,
                          center_frequency_hz=sim_config.center_frequency_hz, config=config)
    b = estimate_spectrum(iq, sample_rate_hz=sim_config.sample_rate_hz,
                          center_frequency_hz=sim_config.center_frequency_hz, config=config)
    np.testing.assert_array_equal(a.psd, b.psd)


def test_clipping_fraction_detects_injected_clipping():
    clipped = SyntheticCaptureConfig(fft_size=512, n_segments=8, clipping=True, gain_scale=50.0)
    clean = SyntheticCaptureConfig(fft_size=512, n_segments=8, clipping=False)
    config = ReduceConfig(fft_size=512)
    est_clipped = estimate_spectrum(build_synthetic_iq(clipped), sample_rate_hz=clipped.sample_rate_hz,
                                    center_frequency_hz=clipped.center_frequency_hz, config=config)
    est_clean = estimate_spectrum(build_synthetic_iq(clean), sample_rate_hz=clean.sample_rate_hz,
                                  center_frequency_hz=clean.center_frequency_hz, config=config)
    assert est_clipped.clipping_fraction > est_clean.clipping_fraction


def test_dc_mask_flags_bins_near_center_frequency():
    sim_config = SyntheticCaptureConfig(fft_size=1024, n_segments=16, dc_spike_amplitude=50.0)
    iq = build_synthetic_iq(sim_config)
    config = ReduceConfig(fft_size=1024)
    estimate = estimate_spectrum(iq, sample_rate_hz=sim_config.sample_rate_hz,
                                 center_frequency_hz=sim_config.center_frequency_hz, config=config)
    result = build_mask(estimate.frequency_hz, estimate.psd, center_frequency_hz=sim_config.center_frequency_hz,
                        config=config, segments=estimate.segments)
    center_bin = int(np.argmin(np.abs(estimate.frequency_hz - sim_config.center_frequency_hz)))
    assert (result.mask[center_bin] & MaskFlag.DC.value) != 0


def test_edge_mask_flags_first_and_last_configured_fraction():
    sim_config = SyntheticCaptureConfig(fft_size=1024, n_segments=8)
    iq = build_synthetic_iq(sim_config)
    config = ReduceConfig(fft_size=1024, edge_fraction=0.05)
    estimate = estimate_spectrum(iq, sample_rate_hz=sim_config.sample_rate_hz,
                                 center_frequency_hz=sim_config.center_frequency_hz, config=config)
    result = build_mask(estimate.frequency_hz, estimate.psd, center_frequency_hz=sim_config.center_frequency_hz,
                        config=config, segments=estimate.segments)
    assert (result.mask[0] & MaskFlag.EDGE.value) != 0
    assert (result.mask[-1] & MaskFlag.EDGE.value) != 0
    assert result.edge_bins_each_side == max(2, int(0.05 * 1024))


def test_spur_detection_flags_persistent_narrow_rfi_line():
    rfi_hz = 1_420_405_752.0 + 400_000.0
    sim_config = SyntheticCaptureConfig(fft_size=1024, n_segments=32, rfi_lines_hz=(rfi_hz,), rfi_amplitude=20.0)
    iq = build_synthetic_iq(sim_config)
    config = ReduceConfig(fft_size=1024)
    estimate = estimate_spectrum(iq, sample_rate_hz=sim_config.sample_rate_hz,
                                 center_frequency_hz=sim_config.center_frequency_hz, config=config)
    result = build_mask(estimate.frequency_hz, estimate.psd, center_frequency_hz=sim_config.center_frequency_hz,
                        config=config, segments=estimate.segments)
    rfi_bin = int(np.argmin(np.abs(estimate.frequency_hz - rfi_hz)))
    window = slice(max(0, rfi_bin - 3), rfi_bin + 4)
    assert np.any((result.mask[window] & MaskFlag.KNOWN_SPUR.value) != 0)


def test_saturated_flag_set_when_clipping_fraction_exceeds_tolerance():
    n = 32
    frequency = np.linspace(1e9, 1.001e9, n)
    psd = np.ones(n)
    config = ReduceConfig(fft_size=n, edge_fraction=0.0)
    result = build_mask(frequency, psd, center_frequency_hz=1e9, config=config, clipping_fraction=0.01)
    assert np.all((result.mask & MaskFlag.SATURATED.value) != 0)


def test_invalid_bins_flagged_for_nan_and_nonpositive_values():
    n = 32
    frequency = np.linspace(1e9, 1.001e9, n)
    psd = np.ones(n)
    psd[10], psd[11], psd[12], psd[13] = np.nan, -1.0, 0.0, np.inf
    config = ReduceConfig(fft_size=n, edge_fraction=0.0)
    result = build_mask(frequency, psd, center_frequency_hz=1e9, config=config)
    invalid = (result.mask & MaskFlag.INVALID.value) != 0
    assert invalid[10] and invalid[11] and invalid[12] and invalid[13]
    assert not invalid[20]


def test_usable_bin_mask_excludes_any_flagged_bin():
    mask = np.array([MaskFlag.GOOD.value, MaskFlag.DC.value, MaskFlag.combine(MaskFlag.RFI, MaskFlag.EDGE).value])
    usable = usable_bin_mask(mask)
    np.testing.assert_array_equal(usable, [True, False, False])
