"""BASELINE stage: low-order continuum fit, reusing
hi_spectral_metric.fit_polynomial_baseline verbatim. The HI line window is
always excluded from the fit region (never blindly used to fit its own
baseline), together with every masked bin (DC/spur/RFI/edge/invalid)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from hi_spectral_metric import fit_polynomial_baseline, frequency_to_radio_velocity
from reduce_engine.config import ReduceConfig
from reduce_engine.masks import usable_bin_mask

DEFAULT_LINE_WINDOW_KM_S = 150.0


@dataclass
class BaselineResult:
    baseline: np.ndarray
    fit_mask: np.ndarray  # bins actually used by the robust fit (after internal sigma-clipping)
    baseline_model: str
    baseline_parameters: dict[str, Any]
    fit_quality_rms_fraction: float


def fit_baseline(frequency_hz: np.ndarray, psd: np.ndarray, mask: np.ndarray, *,
                 config: ReduceConfig, line_window_km_s: float = DEFAULT_LINE_WINDOW_KM_S) -> BaselineResult:
    velocity = frequency_to_radio_velocity(frequency_hz, rest_hz=config.hi_rest_frequency_hz)
    line_window = np.abs(velocity) <= line_window_km_s
    fit_region = usable_bin_mask(mask) & ~line_window
    baseline, active = fit_polynomial_baseline(
        frequency_hz, psd, fit_region, degree=config.baseline_degree,
        iterations=config.baseline_iterations, sigma=config.baseline_sigma,
    )
    residual_fraction = (psd[active] - baseline[active]) / baseline[active]
    fit_quality_rms = float(np.sqrt(np.mean(residual_fraction ** 2))) if active.any() else float("nan")
    return BaselineResult(
        baseline=baseline, fit_mask=active, baseline_model=f"robust_polynomial_degree_{config.baseline_degree}",
        baseline_parameters={"degree": config.baseline_degree, "iterations": config.baseline_iterations,
                             "sigma": config.baseline_sigma, "line_window_km_s": line_window_km_s},
        fit_quality_rms_fraction=fit_quality_rms,
    )
