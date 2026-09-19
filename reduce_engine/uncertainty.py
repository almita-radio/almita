"""UNCERTAINTY stage: per-bin uncertainty for one capture's PSD, derived
from the spread of its own independent FFT sub-integration segments
(robust_psd_from_iq's `segments` return), plus global QC scalars."""
from __future__ import annotations

from typing import Any

import numpy as np


def per_capture_uncertainty(segments: np.ndarray) -> np.ndarray:
    """Robust (MAD-based) standard error of the median-combined PSD, per
    bin, from its own sub-integration segments. Never a single global
    scalar broadcast to every bin - each bin gets its own estimate."""
    if segments.shape[0] < 2:
        return np.full(segments.shape[-1], np.nan)
    median = np.median(segments, axis=0)
    mad = np.median(np.abs(segments - median[None, :]), axis=0)
    sigma = 1.4826 * mad
    return sigma / np.sqrt(segments.shape[0])


def global_metrics(mask: np.ndarray, uncertainty: np.ndarray, integration_time_seconds: float) -> dict[str, Any]:
    from reduce_engine.masks import usable_bin_mask
    usable = usable_bin_mask(mask)
    finite_unc = uncertainty[usable & np.isfinite(uncertainty)]
    return {
        "usable_fraction": float(np.mean(usable)),
        "median_noise": float(np.median(finite_unc)) if finite_unc.size else None,
        "effective_integration_time_seconds": float(integration_time_seconds),
    }
