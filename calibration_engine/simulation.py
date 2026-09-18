"""Synthetic instrument-behavior simulator (Fase 38) - generates realistic
raw interleaved uint8 IQ so the REAL analysis pipeline (sample_statistics,
clipping, bandpass, stability) can be exercised end-to-end without any
hardware, with KNOWN ground truth to check detection against (Fase 39-41,
71-75). Shape is built in the frequency domain (magnitude template ->
random phase -> inverse FFT per block) so slope/ripple/edge-rolloff/RFI
spikes/DC spike are all real spectral features a PSD computation will
actually see, not just time-domain tricks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np


@dataclass
class InstrumentSimulationConfig:
    n_blocks: int = 64                     # >= 4*fft_size samples needed by robust_psd_from_iq (4 segments min)
    fft_size: int = 8192
    sample_rate_hz: float = 2_400_000.0
    center_frequency_hz: float = 1_420_405_752.0
    seed: int = 0
    gain_linear: float = 20.0              # amplitude scale before quantization - the simulation's own "gain knob"
    dc_offset_i: float = 0.0
    dc_offset_q: float = 0.0
    bandpass_slope_db_per_mhz: float = 0.0
    bandpass_ripple_db: float = 0.0
    ripple_period_hz: float = 400_000.0
    edge_rolloff_db: float = 0.0            # attenuation applied at the outer 10% of bins, ramped
    dc_spike_db: float = 0.0                # extra power added at the exact center bin
    rfi_spike_relative_freqs: List[float] = field(default_factory=list)   # fraction of half-band, e.g. 0.3
    rfi_spike_db: float = 0.0
    thermal_drift_fraction: float = 0.0     # fractional amplitude drift from start to end of the buffer
    force_clip_fraction: float = 0.0        # hard-force this fraction of samples to rail values (severe clipping)
    frequency_offset_hz: float = 0.0        # metadata-only "as if the oscillator were off by this much"


def _magnitude_template(config: InstrumentSimulationConfig) -> np.ndarray:
    freq = config.center_frequency_hz + np.fft.fftshift(
        np.fft.fftfreq(config.fft_size, 1.0 / config.sample_rate_hz))
    db = np.zeros(config.fft_size, dtype=float)
    mhz_from_center = (freq - config.center_frequency_hz) / 1.0e6
    db += config.bandpass_slope_db_per_mhz * mhz_from_center
    if config.bandpass_ripple_db:
        db += config.bandpass_ripple_db * np.sin(2 * np.pi * (freq - config.center_frequency_hz) / max(config.ripple_period_hz, 1.0))
    if config.edge_rolloff_db:
        n = config.fft_size
        edge_bins = max(1, int(0.10 * n))
        ramp = np.linspace(config.edge_rolloff_db, 0.0, edge_bins)
        db[:edge_bins] -= ramp[::-1]
        db[-edge_bins:] -= ramp
    if config.dc_spike_db:
        center_bin = config.fft_size // 2
        db[center_bin] += config.dc_spike_db
    for relative in config.rfi_spike_relative_freqs:
        offset_hz = relative * (config.sample_rate_hz / 2.0)
        bin_index = int(np.argmin(np.abs((freq - config.center_frequency_hz) - offset_hz)))
        db[bin_index] += config.rfi_spike_db
    return 10 ** (db / 10.0)


def simulate_capture_iq(config: InstrumentSimulationConfig) -> np.ndarray:
    """Returns a raw interleaved uint8 numpy array, ready for
    sample_statistics/clipping/bandpass exactly like a real HDF5 capture's
    iq_data would be."""
    rng = np.random.default_rng(config.seed)
    template = _magnitude_template(config)
    n = config.fft_size
    blocks = []
    for block_index in range(config.n_blocks):
        phases = rng.uniform(0, 2 * np.pi, size=n)
        spectrum = np.sqrt(template) * np.exp(1j * phases)
        time_block = np.fft.ifft(np.fft.ifftshift(spectrum))
        blocks.append(time_block)
    signal = np.concatenate(blocks)
    signal = signal / max(np.std(signal), 1e-30)   # normalize to unit std before applying gain

    if config.thermal_drift_fraction:
        drift = 1.0 + np.linspace(0.0, config.thermal_drift_fraction, len(signal))
        signal = signal * drift

    scaled = signal * config.gain_linear
    i = 127.5 + config.dc_offset_i + scaled.real
    q = 127.5 + config.dc_offset_q + scaled.imag

    if config.force_clip_fraction > 0:
        n_force = int(config.force_clip_fraction * len(i))
        force_indices = rng.choice(len(i), size=n_force, replace=False)
        rail_choice = rng.choice([0.0, 255.0], size=n_force)
        i[force_indices] = rail_choice
        q[force_indices] = rail_choice

    i_bytes = np.clip(np.round(i), 0, 255).astype(np.uint8)
    q_bytes = np.clip(np.round(q), 0, 255).astype(np.uint8)
    raw = np.empty(2 * len(i_bytes), dtype=np.uint8)
    raw[0::2] = i_bytes
    raw[1::2] = q_bytes
    return raw


# Named presets used by the E2E/adversarial test suite (Fase 71-75) - kept
# here rather than duplicated per test, so the "ground truth" a test checks
# against is defined in exactly one place.

def healthy_config(seed: int = 0) -> InstrumentSimulationConfig:
    return InstrumentSimulationConfig(seed=seed, gain_linear=15.0, bandpass_ripple_db=1.0,
                                       edge_rolloff_db=2.0, dc_spike_db=3.0,
                                       rfi_spike_relative_freqs=[0.3], rfi_spike_db=6.0)


def severe_clipping_config(seed: int = 0) -> InstrumentSimulationConfig:
    return InstrumentSimulationConfig(seed=seed, gain_linear=15.0, force_clip_fraction=0.01)


def thermal_drift_config(seed: int = 0, drift_fraction: float = 0.6) -> InstrumentSimulationConfig:
    return InstrumentSimulationConfig(seed=seed, gain_linear=15.0, thermal_drift_fraction=drift_fraction)


def flat_noisy_config(seed: int = 0) -> InstrumentSimulationConfig:
    return InstrumentSimulationConfig(seed=seed, gain_linear=15.0)
