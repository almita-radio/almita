"""Temporal stability (Fase 15, 16, 20, 27, 28) - reproducibility across
seconds/minutes/hours, warm-up characterization, and the sigma-vs-
integration-time averaging law. All relative; nothing here claims an
absolute noise floor or sensitivity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import numpy as np


@dataclass
class StabilitySample:
    timestamp_utc: str
    relative_power: float          # e.g. median PSD of one capture, in the SAME arbitrary units throughout a run
    elapsed_seconds: float          # since the first sample in the run


@dataclass
class StabilityResult:
    n_samples: int
    duration_seconds: float
    median_power: float
    power_rms_fraction: float       # std(power)/median(power) - dimensionless, comparable across runs
    drift_slope_per_hour: float     # linear trend of relative_power vs time, normalized by median
    max_deviation_fraction: float
    warmup_detected: bool
    warmup_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_samples": self.n_samples, "duration_seconds": self.duration_seconds,
            "median_power": self.median_power, "power_rms_fraction": self.power_rms_fraction,
            "drift_slope_per_hour": self.drift_slope_per_hour,
            "max_deviation_fraction": self.max_deviation_fraction,
            "warmup_detected": self.warmup_detected, "warmup_reason": self.warmup_reason,
        }


def analyze_stability(samples: Sequence[StabilitySample], warmup_window_seconds: float = 600.0,
                       warmup_excess_fraction: float = 0.05) -> StabilityResult:
    """Fase 20: warm-up is DETECTED from data (the first `warmup_window_seconds`
    of a run showing meaningfully higher variability than the rest), never
    assumed and never used to invent a fixed "wait X minutes" rule - this
    only reports whether the data SUPPORTS investigating one."""
    if len(samples) < 3:
        raise ValueError("at least 3 stability samples are required")
    ordered = sorted(samples, key=lambda s: s.elapsed_seconds)
    power = np.asarray([s.relative_power for s in ordered], dtype=float)
    elapsed = np.asarray([s.elapsed_seconds for s in ordered], dtype=float)
    median_power = float(np.median(power))
    rms_fraction = float(np.std(power) / max(median_power, 1e-30))
    duration = float(elapsed[-1] - elapsed[0])
    max_deviation = float(np.max(np.abs(power - median_power)) / max(median_power, 1e-30))

    if duration > 0:
        slope = float(np.polyfit(elapsed, power, 1)[0])
        drift_per_hour = slope * 3600.0 / max(median_power, 1e-30)
    else:
        drift_per_hour = 0.0

    warmup_mask = elapsed <= warmup_window_seconds
    late_mask = ~warmup_mask
    warmup_detected, warmup_reason = False, "insufficient late-window samples to compare"
    if np.sum(warmup_mask) >= 2 and np.sum(late_mask) >= 2:
        early_variability = float(np.std(power[warmup_mask]) / max(median_power, 1e-30))
        late_variability = float(np.std(power[late_mask]) / max(median_power, 1e-30))
        if early_variability > late_variability * (1 + warmup_excess_fraction) and early_variability > late_variability:
            warmup_detected = True
            warmup_reason = (f"first {warmup_window_seconds:.0f}s variability "
                              f"({early_variability:.4f}) exceeds later variability "
                              f"({late_variability:.4f})")
        else:
            warmup_reason = (f"first {warmup_window_seconds:.0f}s variability "
                              f"({early_variability:.4f}) not meaningfully worse than later "
                              f"({late_variability:.4f})")

    return StabilityResult(
        n_samples=len(ordered), duration_seconds=duration, median_power=median_power,
        power_rms_fraction=rms_fraction, drift_slope_per_hour=drift_per_hour,
        max_deviation_fraction=max_deviation, warmup_detected=warmup_detected, warmup_reason=warmup_reason,
    )


@dataclass
class AveragingLawResult:
    integration_seconds: List[float]
    sigma_relative: List[float]           # std/median at each integration time
    reference_slope: float                 # theoretical -0.5 (1/sqrt(t)) on a log-log fit
    measured_slope: float
    deviation_from_reference: float
    interpretation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "integration_seconds": self.integration_seconds, "sigma_relative": self.sigma_relative,
            "reference_slope": self.reference_slope, "measured_slope": self.measured_slope,
            "deviation_from_reference": self.deviation_from_reference, "interpretation": self.interpretation,
        }


def analyze_averaging_law(integration_seconds: Sequence[float], sigma_relative: Sequence[float]) -> AveragingLawResult:
    """Fase 28: compares against ~1/sqrt(t) as a REFERENCE, never an
    obligation - large deviations are reported as a fact (drift/RFI/
    nonstationarity candidates), not silently corrected or hidden."""
    t = np.asarray(integration_seconds, dtype=float)
    sigma = np.asarray(sigma_relative, dtype=float)
    if len(t) < 3:
        raise ValueError("at least 3 integration times are required to fit a slope")
    if np.any(t <= 0) or np.any(sigma <= 0):
        raise ValueError("integration times and sigma values must be strictly positive")
    log_t, log_sigma = np.log(t), np.log(sigma)
    measured_slope = float(np.polyfit(log_t, log_sigma, 1)[0])
    reference_slope = -0.5
    deviation = abs(measured_slope - reference_slope)
    if deviation < 0.15:
        interpretation = "consistent with stationary-noise averaging (~1/sqrt(t))"
    elif measured_slope > reference_slope:
        interpretation = "averaging improves variance SLOWER than 1/sqrt(t) - possible drift, RFI, or nonstationarity"
    else:
        interpretation = "averaging improves variance FASTER than 1/sqrt(t) - unexpected, worth independent review"
    return AveragingLawResult(
        integration_seconds=list(map(float, t)), sigma_relative=list(map(float, sigma)),
        reference_slope=reference_slope, measured_slope=measured_slope,
        deviation_from_reference=deviation, interpretation=interpretation,
    )
