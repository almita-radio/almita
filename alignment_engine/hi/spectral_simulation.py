"""Synthetic HI spectrum generator (Fase 28) - feeds spectral_pipeline.py's
REAL pipeline (not a shortcut scalar), so Monte Carlo characterization
exercises the same code a real spectrum would go through.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

import numpy as np


@dataclass
class GaussianComponent:
    amplitude_k: float          # peak brightness temperature (or arbitrary units), before gain/baseline
    center_km_s: float
    fwhm_km_s: float


@dataclass
class SpectralSimConfig:
    velocity_range_km_s: Tuple[float, float] = (-150.0, 150.0)
    n_channels: int = 512
    components: List[GaussianComponent] = field(default_factory=lambda: [GaussianComponent(1.0, 0.0, 20.0)])
    gain: float = 1.0
    baseline_coefficients: Tuple[float, ...] = (0.0,)   # constant term first, then linear, quadratic, ...
    noise_std: float = 0.05
    rfi_spike_count: int = 0
    rfi_spike_amplitude: float = 5.0
    missing_channel_fraction: float = 0.0
    seed: int = 1


def generate_spectrum(config: SpectralSimConfig) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (velocity_km_s, spectrum, rfi_mask). `rfi_mask` marks the
    channels this simulator itself corrupted with an RFI spike - a
    real pipeline would have to detect these on its own; Monte Carlo runs
    that want to test detection should NOT pass this mask through, only
    ones that want to isolate spatial-fit behavior from RFI-detection
    behavior should."""
    rng = np.random.default_rng(config.seed)
    velocity = np.linspace(config.velocity_range_km_s[0], config.velocity_range_km_s[1], config.n_channels)

    line = np.zeros(config.n_channels)
    for comp in config.components:
        sigma = comp.fwhm_km_s / (2.0 * np.sqrt(2.0 * np.log(2.0)))
        line += comp.amplitude_k * np.exp(-0.5 * ((velocity - comp.center_km_s) / sigma) ** 2)

    baseline = np.zeros(config.n_channels)
    for order, coeff in enumerate(config.baseline_coefficients):
        baseline += coeff * (velocity ** order)

    spectrum = config.gain * line + baseline
    spectrum += rng.normal(0.0, config.noise_std, size=config.n_channels)

    rfi_mask = np.zeros(config.n_channels, dtype=bool)
    if config.rfi_spike_count > 0:
        indices = rng.choice(config.n_channels, size=min(config.rfi_spike_count, config.n_channels), replace=False)
        spectrum[indices] += config.rfi_spike_amplitude * rng.choice([-1.0, 1.0], size=len(indices))
        rfi_mask[indices] = True

    if config.missing_channel_fraction > 0:
        count = int(round(config.missing_channel_fraction * config.n_channels))
        if count > 0:
            missing = rng.choice(config.n_channels, size=count, replace=False)
            spectrum[missing] = np.nan

    return velocity, spectrum, rfi_mask
