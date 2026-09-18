"""Raw spectrum -> single pointing metric (+ uncertainty), reproducibly
(Fase 8-10). Pure numpy - no scipy (not installed on this Pi).

Pipeline (fixed order, none of it silently skippable):
  1. mask invalid edges + any explicit exclusion windows (RFI, known-bad
     channels) - masked channels are EXCLUDED, never zeroed (Fase 48:
     "No NaN->0").
  2. fit a baseline (constant/linear/low-order polynomial) to the
     remaining channels OUTSIDE the HI line window.
  3. subtract the baseline from the line window.
  4. integrate the baseline-subtracted line window -> one scalar metric,
     with a propagated uncertainty estimate from the off-line channel
     noise (Fase 9's "no overfit": default is `linear`, never higher than
     `quadratic` unless a caller explicitly asks).

Deliberately NOT implemented (Fase 8's explicit "NO" list): peak-of-FFT
detection, single-bin thresholding, irreversible smoothing before masking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np


@dataclass
class SpectralPipelineConfig:
    line_window_km_s: Tuple[float, float] = (-100.0, 100.0)
    baseline_exclusion_km_s: Tuple[float, float] = (-100.0, 100.0)  # excluded from the baseline fit
    baseline_order: int = 1  # 0=constant, 1=linear, 2=quadratic (never higher without explicit override)
    edge_fraction: float = 0.05  # fraction of channels at each edge always masked (bandpass roll-off)
    extra_exclusion_windows_km_s: List[Tuple[float, float]] = field(default_factory=list)  # e.g. known RFI


@dataclass
class SpectralMetricResult:
    metric: Optional[float]           # integrated, baseline-subtracted line flux (arbitrary units * km/s)
    uncertainty: Optional[float]      # 1-sigma, from off-line channel noise
    baseline_coefficients: Optional[List[float]]  # np.polyfit convention: highest degree first
    usable_fraction: float
    masked_channels: int
    total_channels: int
    rfi_flag_fraction: float
    metric_valid: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "metric": self.metric, "uncertainty": self.uncertainty,
            "baseline_coefficients": self.baseline_coefficients,
            "usable_fraction": self.usable_fraction, "masked_channels": self.masked_channels,
            "total_channels": self.total_channels, "rfi_flag_fraction": self.rfi_flag_fraction,
            "metric_valid": self.metric_valid, "reason": self.reason,
        }


def _in_any_window(values: np.ndarray, windows) -> np.ndarray:
    mask = np.zeros(values.shape, dtype=bool)
    for lo, hi in windows:
        mask |= (values >= lo) & (values <= hi)
    return mask


def compute_spectral_metric(velocity_km_s: np.ndarray, spectrum: np.ndarray,
                             config: SpectralPipelineConfig,
                             rfi_mask: Optional[np.ndarray] = None) -> SpectralMetricResult:
    """`rfi_mask`, if given, is a boolean array (True = flagged/unusable)
    the SAME length as `spectrum` - e.g. from a separate RFI detector.
    Channels that are NaN/inf in `spectrum` are always masked regardless."""
    velocity_km_s = np.asarray(velocity_km_s, dtype=float)
    spectrum = np.asarray(spectrum, dtype=float)
    total_channels = len(spectrum)
    if total_channels == 0 or len(velocity_km_s) != total_channels:
        return SpectralMetricResult(None, None, None, 0.0, total_channels, total_channels, 1.0, False,
                                     "empty or mismatched spectrum/velocity axis")

    invalid = ~np.isfinite(spectrum)
    edge_count = max(1, int(round(config.edge_fraction * total_channels)))
    edge_mask = np.zeros(total_channels, dtype=bool)
    edge_mask[:edge_count] = True
    edge_mask[-edge_count:] = True
    external_rfi = rfi_mask.astype(bool) if rfi_mask is not None else np.zeros(total_channels, dtype=bool)

    masked = invalid | edge_mask | external_rfi
    usable = ~masked
    masked_channels = int(np.sum(masked))
    rfi_flag_fraction = float(np.sum(external_rfi)) / total_channels

    if np.sum(usable) < max(8, total_channels // 10):
        return SpectralMetricResult(None, None, None, float(np.sum(usable)) / total_channels,
                                     masked_channels, total_channels, rfi_flag_fraction, False,
                                     "insufficient usable channels after masking")

    exclusion_windows = [config.baseline_exclusion_km_s] + list(config.extra_exclusion_windows_km_s)
    in_line = _in_any_window(velocity_km_s, [config.line_window_km_s])
    in_baseline_exclusion = _in_any_window(velocity_km_s, exclusion_windows)
    baseline_fit_mask = usable & ~in_baseline_exclusion
    line_mask = usable & in_line

    if np.sum(baseline_fit_mask) < config.baseline_order + 3:
        return SpectralMetricResult(None, None, None, float(np.sum(usable)) / total_channels,
                                     masked_channels, total_channels, rfi_flag_fraction, False,
                                     "insufficient off-line channels to fit a baseline")
    if np.sum(line_mask) < 3:
        return SpectralMetricResult(None, None, None, float(np.sum(usable)) / total_channels,
                                     masked_channels, total_channels, rfi_flag_fraction, False,
                                     "insufficient usable channels inside the line window")

    coeffs = np.polyfit(velocity_km_s[baseline_fit_mask], spectrum[baseline_fit_mask], deg=config.baseline_order)
    baseline_model = np.polyval(coeffs, velocity_km_s)
    residual_off_line = spectrum[baseline_fit_mask] - baseline_model[baseline_fit_mask]
    off_line_noise_std = float(np.std(residual_off_line))

    line_v = velocity_km_s[line_mask]
    line_subtracted = spectrum[line_mask] - baseline_model[line_mask]
    order = np.argsort(line_v)
    line_v_sorted = line_v[order]
    line_subtracted_sorted = line_subtracted[order]
    metric = float(np.trapezoid(line_subtracted_sorted, line_v_sorted))

    channel_width = float(np.median(np.abs(np.diff(np.sort(velocity_km_s))))) if total_channels > 1 else 1.0
    uncertainty = off_line_noise_std * channel_width * np.sqrt(max(np.sum(line_mask), 1))

    return SpectralMetricResult(
        metric=metric, uncertainty=float(uncertainty), baseline_coefficients=list(map(float, coeffs)),
        usable_fraction=float(np.sum(usable)) / total_channels, masked_channels=masked_channels,
        total_channels=total_channels, rfi_flag_fraction=rfi_flag_fraction, metric_valid=True,
        reason="ok",
    )
