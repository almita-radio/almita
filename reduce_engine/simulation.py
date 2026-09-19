"""Synthetic instrument/spectrum simulator for REDUCE validation. Produces
real uint8 interleaved IQ bytes (the same format robust_psd_from_iq
expects) with independently injectable, known ground truths: HI-like
line (position, width, amplitude), Gaussian noise level, bandpass
slope/ripple, DC spike, RFI lines, gain scale, missing/clipped samples,
and Doppler offset - so pipeline recovery can be checked against a known
answer, not just "the shape looks plausible".

This is REDUCE's own simulator (like calibration_engine/simulation.py's
precedent for its domain) - it does not reuse or duplicate any existing
simulator because none of the audited modules build synthetic IQ time
series with a controllable target PSD shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from hi_spectral_metric import HI_REST_HZ, frequency_to_radio_velocity


@dataclass
class SyntheticCaptureConfig:
    sample_rate_hz: float = 2_400_000.0
    center_frequency_hz: float = 1_420_405_752.0
    fft_size: int = 1024
    n_segments: int = 64
    noise_level: float = 1.0
    bandpass_slope: float = 0.0                 # fractional change in level across the band
    bandpass_ripple_amplitude: float = 0.0       # fractional
    bandpass_ripple_period_hz: float = 300_000.0
    dc_spike_amplitude: float = 0.0              # fractional excess at DC
    dc_spike_width_hz: float = 3_000.0
    edge_rolloff: bool = False
    rfi_lines_hz: Sequence[float] = field(default_factory=tuple)
    rfi_amplitude: float = 5.0                   # fractional excess
    hi_line_velocity_km_s: Optional[float] = 0.0
    hi_line_fwhm_km_s: float = 20.0
    hi_line_peak_fraction: float = 0.0
    gain_scale: float = 1.0
    clipping: bool = False
    random_seed: int = 20260919


def _target_psd_shape(freq_natural_hz: np.ndarray, config: SyntheticCaptureConfig) -> np.ndarray:
    center = config.center_frequency_hz
    span = config.sample_rate_hz
    fractional_position = (freq_natural_hz - center) / span  # -0.5..0.5 roughly
    shape = np.ones_like(freq_natural_hz) * config.noise_level
    shape *= (1.0 + config.bandpass_slope * fractional_position)
    if config.bandpass_ripple_amplitude:
        shape *= (1.0 + config.bandpass_ripple_amplitude *
                 np.sin(2 * np.pi * (freq_natural_hz - center) / config.bandpass_ripple_period_hz))
    if config.dc_spike_amplitude:
        shape += config.dc_spike_amplitude * config.noise_level * np.exp(
            -0.5 * ((freq_natural_hz - center) / config.dc_spike_width_hz) ** 2)
    for rfi_hz in config.rfi_lines_hz:
        shape += config.rfi_amplitude * config.noise_level * np.exp(
            -0.5 * ((freq_natural_hz - rfi_hz) / (span / config.fft_size)) ** 2)
    if config.hi_line_peak_fraction:
        velocity = frequency_to_radio_velocity(freq_natural_hz, rest_hz=HI_REST_HZ)
        sigma_km_s = config.hi_line_fwhm_km_s / 2.354820045
        shape += config.hi_line_peak_fraction * config.noise_level * np.exp(
            -0.5 * ((velocity - config.hi_line_velocity_km_s) / sigma_km_s) ** 2)
    if config.edge_rolloff:
        n = freq_natural_hz.shape[0]
        window = np.ones(n)
        ramp = max(2, n // 20)
        window[:ramp] = np.linspace(0.05, 1.0, ramp)
        window[-ramp:] = np.linspace(1.0, 0.05, ramp)
        shape *= window
    return np.maximum(shape, 1e-6)


def build_synthetic_iq(config: SyntheticCaptureConfig, *, rng: Optional[np.random.Generator] = None) -> np.ndarray:
    """Returns interleaved uint8 IQ bytes reproducing `config`'s injected
    features when analyzed by hi_spectral_metric.robust_psd_from_iq with
    the SAME fft_size."""
    rng = rng or np.random.default_rng(config.random_seed)
    freq_natural = np.fft.fftfreq(config.fft_size, 1.0 / config.sample_rate_hz) + config.center_frequency_hz
    psd_shape = _target_psd_shape(freq_natural, config)
    amplitude = np.sqrt(psd_shape / 2.0)

    segments = []
    for _ in range(config.n_segments):
        freq_domain = amplitude * (rng.standard_normal(config.fft_size) + 1j * rng.standard_normal(config.fft_size))
        segments.append(np.fft.ifft(freq_domain))
    time_series = np.concatenate(segments) * config.gain_scale

    scale = 20.0 if not config.clipping else 200.0
    real = np.clip(np.round(127.5 + scale * time_series.real), 0, 255).astype(np.uint8)
    imag = np.clip(np.round(127.5 + scale * time_series.imag), 0, 255).astype(np.uint8)
    interleaved = np.empty(real.size * 2, dtype=np.uint8)
    interleaved[0::2] = real
    interleaved[1::2] = imag
    return interleaved


def expected_frequency_axis(config: SyntheticCaptureConfig) -> np.ndarray:
    return config.center_frequency_hz + np.fft.fftshift(np.fft.fftfreq(config.fft_size, 1.0 / config.sample_rate_hz))
