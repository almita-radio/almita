"""UNCERTAINTY stage: per-bin uncertainty for one capture's PSD, derived
from the spread of its own independent FFT sub-integration segments
(robust_psd_from_iq's `segments` return), plus global QC scalars.

reduce-v1.2 (audit 2026-11-25, operator "dale nomas"): the error of the MEDIAN estimate is taken from order
statistics (it was 1.4826 MAD / sqrt(n), 0.71 of the real error on exponential periodograms)."""
from __future__ import annotations

from typing import Any

import numpy as np


Z_ORDER = 2.5758          # two-sided 99 % normal quantile used by the order-statistic standard error of the median


def per_capture_uncertainty(segments: np.ndarray, combine: str = "median") -> np.ndarray:
    """Standard error, per bin, of the capture's combined PSD, from its own sub-integration segments. Never a single
    global scalar broadcast to every bin - each bin gets its own estimate.

    combine (how the PSD was formed from `segments`):
      "median" - the classic estimate (median of single Hann periodograms). Each periodogram bin of noise is
                 EXPONENTIAL, not Gaussian, so 1.4826 x MAD / sqrt(n) (the old formula) is 0.71 of the median's real
                 error (audit 2026-11-25: x1.40 in theory, x1.32 measured by half-splits). The error is taken from the
                 order statistics instead (McKean-Schrader), which holds for any distribution:
                     SE = (X_(n-c+1) - X_(c)) / (2 z),   c = round((n+1)/2 - z sqrt(n/4)),   z = 2.5758
      "mean"   - plain mean of the segments: sample standard deviation / sqrt(n).
      "robust_mean_of_block_means" - REDUCE ESTIMATOR v2: the segments are means of many frames (near Gaussian) and a
                 few may be broadband-flagged blocks the PSD left out, so 1.4826 x MAD / sqrt(n) (the old formula, exact
                 for Gaussian segments and blind to those blocks; half-splits gave 0.93-0.96 of the real error).
    """
    n = segments.shape[0]
    if n < 2:
        return np.full(segments.shape[-1], np.nan)
    if combine == "median":
        c = max(1, min(n // 2, int(round((n + 1) / 2 - Z_ORDER * np.sqrt(n / 4.0)))))
        lo, hi = c - 1, n - c                                   # 0-based ranks of X_(c) and X_(n-c+1)
        part = np.partition(segments, (lo, hi), axis=0)
        return (part[hi] - part[lo]) / (2.0 * Z_ORDER)
    if combine == "mean":
        return np.std(segments, axis=0, ddof=1) / np.sqrt(n)
    if combine == "robust_mean_of_block_means":
        median = np.median(segments, axis=0)
        mad = np.median(np.abs(segments - median[None, :]), axis=0)
        return 1.4826 * mad / np.sqrt(n)
    raise ValueError(f"unknown combine {combine!r} for the per-capture uncertainty")


def global_metrics(mask: np.ndarray, uncertainty: np.ndarray, integration_time_seconds: float) -> dict[str, Any]:
    from reduce_engine.masks import usable_bin_mask
    usable = usable_bin_mask(mask)
    finite_unc = uncertainty[usable & np.isfinite(uncertainty)]
    return {
        "usable_fraction": float(np.mean(usable)),
        "median_noise": float(np.median(finite_unc)) if finite_unc.size else None,
        "effective_integration_time_seconds": float(integration_time_seconds),
    }
