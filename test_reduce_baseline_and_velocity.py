"""Unit tests for BASELINE (excludes HI line + masked bins from the fit)
and VELOCITY FRAME (astropy-backed, fail-safe on missing metadata/frame)."""
import numpy as np
import pytest

from hi_spectral_metric import HI_REST_HZ
from reduce_engine.baseline import fit_baseline
from reduce_engine.config import ReduceConfig
from reduce_engine.masks import build_mask
from reduce_engine.models import MaskFlag
from reduce_engine.simulation import SyntheticCaptureConfig, build_synthetic_iq
from reduce_engine.spectral import estimate_spectrum
from reduce_engine.velocity import compute_velocity_axis


def _estimate_and_mask(sim_config, reduce_config):
    iq = build_synthetic_iq(sim_config)
    estimate = estimate_spectrum(iq, sample_rate_hz=sim_config.sample_rate_hz,
                                 center_frequency_hz=sim_config.center_frequency_hz, config=reduce_config)
    mask_result = build_mask(estimate.frequency_hz, estimate.psd, center_frequency_hz=sim_config.center_frequency_hz,
                             config=reduce_config, segments=estimate.segments)
    return estimate, mask_result


def test_baseline_excludes_hi_line_window_from_fit_region():
    sim_config = SyntheticCaptureConfig(fft_size=2048, n_segments=32, hi_line_peak_fraction=0.4,
                                        hi_line_velocity_km_s=0.0, hi_line_fwhm_km_s=15.0)
    reduce_config = ReduceConfig(fft_size=2048)
    estimate, mask_result = _estimate_and_mask(sim_config, reduce_config)
    result = fit_baseline(estimate.frequency_hz, estimate.psd, mask_result.mask, config=reduce_config)
    from hi_spectral_metric import frequency_to_radio_velocity
    velocity = frequency_to_radio_velocity(estimate.frequency_hz)
    in_line = np.abs(velocity) <= 150.0
    assert not np.any(result.fit_mask & in_line)


def test_baseline_recovers_flat_continuum_within_tolerance_when_no_line():
    sim_config = SyntheticCaptureConfig(fft_size=2048, n_segments=64, noise_level=1.0)
    reduce_config = ReduceConfig(fft_size=2048)
    estimate, mask_result = _estimate_and_mask(sim_config, reduce_config)
    result = fit_baseline(estimate.frequency_hz, estimate.psd, mask_result.mask, config=reduce_config)
    assert result.fit_quality_rms_fraction < 0.5


def test_baseline_fails_loudly_when_all_bins_masked():
    n = 32
    frequency = np.linspace(1e9, 1.001e9, n)
    psd = np.ones(n)
    mask = np.full(n, MaskFlag.RFI.value, dtype=np.int64)
    with pytest.raises(ValueError):
        fit_baseline(frequency, psd, mask, config=ReduceConfig(fft_size=n))


def test_velocity_unavailable_when_pointing_metadata_missing():
    frequency = np.linspace(1.419e9, 1.421e9, 16)
    result = compute_velocity_axis(frequency, rest_frequency_hz=HI_REST_HZ, frame="lsrk",
                                   timestamp_utc="2026-01-01T00:00:00", observer_latitude_deg=-33.0,
                                   observer_longitude_deg=-70.0, observer_elevation_m=500,
                                   ra_hours=None, dec_degrees=None)
    assert result.frame == "UNAVAILABLE"
    assert result.velocity_m_s is None
    assert "ra_hours" in result.reason


def test_velocity_unsupported_frame_is_fail_safe_not_silent_fallback():
    frequency = np.linspace(1.419e9, 1.421e9, 16)
    result = compute_velocity_axis(frequency, rest_frequency_hz=HI_REST_HZ, frame="galactocentric",
                                   timestamp_utc="2026-01-01T00:00:00", observer_latitude_deg=-33.0,
                                   observer_longitude_deg=-70.0, observer_elevation_m=500,
                                   ra_hours=11.7, dec_degrees=-34.9)
    assert result.frame == "UNAVAILABLE"
    assert "unsupported" in result.reason


def test_velocity_topocentric_matches_hi_spectral_metric_convention_directly():
    frequency = np.array([HI_REST_HZ])
    result = compute_velocity_axis(frequency, rest_frequency_hz=HI_REST_HZ, frame="topocentric",
                                   timestamp_utc="2026-01-01T00:00:00", observer_latitude_deg=-33.0,
                                   observer_longitude_deg=-70.0, observer_elevation_m=500,
                                   ra_hours=11.7, dec_degrees=-34.9)
    assert result.frame == "topocentric"
    np.testing.assert_allclose(result.velocity_m_s, [0.0], atol=1.0)


def test_velocity_real_timestamp_with_utc_offset_suffix_parses(  ):
    """Regression: real HDF5 capture_start_utc carries a "+00:00" suffix
    that astropy.time.Time's format auto-detection rejects outright."""
    frequency = np.array([HI_REST_HZ])
    result = compute_velocity_axis(frequency, rest_frequency_hz=HI_REST_HZ, frame="lsrk",
                                   timestamp_utc="2026-09-02T16:18:20.126808+00:00", observer_latitude_deg=-33.4489,
                                   observer_longitude_deg=-70.6693, observer_elevation_m=570,
                                   ra_hours=11.708053, dec_degrees=-34.935703)
    assert result.frame == "lsrk"
    assert result.velocity_m_s is not None
