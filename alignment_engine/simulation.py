"""Synthetic Solar/HI data for offline development (Fase 17).

Nothing here talks to hardware. The point is to let the fitter recover a
known injected offset (Fase 17's own worked example: inject dAz=+1.2,
dAlt=-0.7, expect the fitter to recover approximately the same), with
configurable noise/missing-samples/gain/baseline so the failure modes in
Fase 18's test list are actually constructible.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np
from astropy.coordinates import SkyCoord

from alignment import offset_coordinates, shifted_positions
from .scan_planner import ScanPoint


@dataclass
class SolarBeamSimConfig:
    true_offset_east_deg: float
    true_offset_north_deg: float
    fwhm_deg: float = 20.0
    peak_amplitude: float = 100.0
    noise_fraction: float = 0.03
    missing_fraction: float = 0.0
    seed: int = 1


def synthetic_solar_metrics(points: List[ScanPoint], center: SkyCoord,
                             config: SolarBeamSimConfig) -> List[Optional[float]]:
    """Radially-symmetric Gaussian beam around `center`, sampled through
    alignment.shifted_positions() - the same helper alignment.py's own
    simulate_offset_case() uses to inject a known offset - rather than a
    hand-derived +/- on raw east/north numbers. Using a different sign
    convention than shifted_positions()/estimate_template_offset() already
    agree on is exactly how this function's first draft silently recovered
    the wrong sign in testing; reusing the same helper both places removes
    that whole class of mistake instead of re-deriving the correct sign by
    hand."""
    rng = np.random.default_rng(config.seed)
    sigma = config.fwhm_deg / (2 * math.sqrt(2 * math.log(2)))
    positions = offset_coordinates(center, [p.east_deg for p in points], [p.north_deg for p in points])
    shifted = shifted_positions(positions, center, config.true_offset_east_deg, config.true_offset_north_deg)
    distance_deg = shifted.separation(center).deg
    clean = config.peak_amplitude * np.exp(-0.5 * (distance_deg / sigma) ** 2)
    noisy = clean + rng.normal(0, max(config.noise_fraction, 0.0) * config.peak_amplitude, size=len(points))
    values: List[Optional[float]] = [float(v) for v in noisy]
    if config.missing_fraction > 0:
        count = int(round(config.missing_fraction * len(points)))
        for index in rng.choice(len(points), size=count, replace=False):
            values[int(index)] = None
    return values


@dataclass
class HIMapSimConfig:
    true_offset_east_deg: float
    true_offset_north_deg: float
    gain_a: float = 1.0
    baseline_b: float = 0.0
    noise_fraction: float = 0.03
    missing_fraction: float = 0.0
    seed: int = 1


def synthetic_hi_metrics(points: List[ScanPoint], center: SkyCoord,
                          template: Callable[[SkyCoord], np.ndarray],
                          config: HIMapSimConfig) -> List[Optional[float]]:
    """Observed(point) = gain_a * Reference(point + true_offset) + baseline_b
    + noise - the exact model Fase 6 asks the fitter to invert. `template`
    is a single-argument callable bound to `center`
    (HIReferenceProvider.template_for(center, beam_fwhm_deg) - see
    targets/hi_reference.py); positions are resolved once since the HI
    target itself does not move during a scan the way the Sun does."""
    positions = offset_coordinates(center, [p.east_deg for p in points], [p.north_deg for p in points])
    shifted = shifted_positions(positions, center, config.true_offset_east_deg, config.true_offset_north_deg)
    clean = np.asarray(template(shifted))
    scaled = config.gain_a * clean + config.baseline_b
    rng = np.random.default_rng(config.seed)
    noise_scale = max(config.noise_fraction, 0.0) * (np.std(scaled) or 1.0)
    noisy = scaled + rng.normal(0, noise_scale, size=len(scaled))
    values: List[Optional[float]] = [float(v) for v in noisy]
    if config.missing_fraction > 0:
        count = int(round(config.missing_fraction * len(points)))
        for index in rng.choice(len(points), size=count, replace=False):
            values[int(index)] = None
    return values
