"""AVERAGING/STACKING stage: the one central combination function. Never a
naive np.mean() over raw arrays - always mask-aware, weighted, and
sigma-clipped so a single pathological spectrum (RFI burst, gain glitch)
does not silently corrupt the stack.

Default method is inverse-variance-weighted with iterative sigma-clipping;
see test_reduce_averaging.py's synthetic comparison (Gaussian noise, single
RFI spike, gain drift, missing bins, one pathological spectrum) for the
evidence behind this choice, per docs/REDUCE_PIPELINE.md section on
averaging robustness. Equal-weight is the explicit, registered fallback
when no valid per-capture uncertainty exists.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

import numpy as np

from reduce_engine.models import MaskFlag


@dataclass
class AverageResult:
    value: np.ndarray
    uncertainty: np.ndarray
    n_contributing: np.ndarray
    mask: np.ndarray  # GOOD only where at least one contributor was unmasked there
    method: str
    weighting: str  # "inverse_variance" | "equal_weight"
    clipped_fraction: float

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method, "weighting": self.weighting,
                "clipped_fraction": self.clipped_fraction,
                "mean_n_contributing": float(np.mean(self.n_contributing))}


def stack_spectra(values: list[np.ndarray], uncertainties: list[np.ndarray], masks: list[np.ndarray], *,
                  method: str = "inverse_variance_weighted", sigma_clip_threshold: float = 5.0,
                  clip_iterations: int = 2) -> AverageResult:
    if not values:
        raise ValueError("stack_spectra requires at least one spectrum")
    v = np.asarray(values, dtype=np.float64)          # (n_captures, n_bins)
    u = np.asarray(uncertainties, dtype=np.float64)
    m = np.asarray(masks, dtype=np.int64)
    base_usable = (m == MaskFlag.GOOD.value) & np.isfinite(v)
    valid_uncertainty = np.isfinite(u) & (u > 0)

    if method == "equal_weight":
        usable, weighting = base_usable, "equal_weight"
    elif method == "inverse_variance_weighted":
        usable = base_usable & valid_uncertainty
        if not usable.any():
            usable, weighting = base_usable, "equal_weight"  # explicit, registered fallback - never silent
        else:
            weighting = "inverse_variance"
    else:
        raise ValueError(f"unknown averaging method {method!r}")

    # Outlier rejection uses a ROBUST (median/MAD) center and spread, never
    # the weighted mean/variance being solved for - a single extreme
    # pathological spectrum otherwise inflates its own detection threshold
    # ("swamping") and survives every clipping iteration undetected.
    active = usable.copy()
    for _ in range(clip_iterations):
        masked_v = np.where(active, v, np.nan)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            # a bin with zero active contributors (e.g. every capture masks
            # the same edge bin) legitimately has no median - nanmedian's
            # "All-NaN slice" RuntimeWarning is expected there, not a bug.
            warnings.simplefilter("ignore", category=RuntimeWarning)
            median = np.nanmedian(masked_v, axis=0)
            mad = np.nanmedian(np.abs(masked_v - median[None, :]), axis=0)
        robust_sigma = np.where(mad > 0, 1.4826 * mad, np.inf)
        outlier = active & (np.abs(v - median[None, :]) > sigma_clip_threshold * robust_sigma[None, :]) & \
            (np.sum(active, axis=0) > 2)
        if not outlier.any():
            break
        active &= ~outlier

    if weighting == "inverse_variance":
        weights = np.where(active, 1.0 / np.maximum(u, 1e-30) ** 2, 0.0)
    else:
        weights = active.astype(np.float64)

    w = weights * active
    total_w = np.sum(w, axis=0)
    n_contributing = np.sum(active, axis=0)
    value = np.full(v.shape[1], np.nan)
    uncertainty = np.full(v.shape[1], np.nan)
    has_data = total_w > 0
    value[has_data] = np.sum(w[:, has_data] * v[:, has_data], axis=0) / total_w[has_data]
    if weighting == "inverse_variance":
        uncertainty[has_data] = 1.0 / np.sqrt(total_w[has_data])
    else:
        with np.errstate(invalid="ignore"):
            dispersion = np.sqrt(np.sum(active[:, has_data] * (v[:, has_data] - value[has_data]) ** 2, axis=0) /
                                 np.maximum(n_contributing[has_data] - 1, 1))
        uncertainty[has_data] = dispersion / np.sqrt(np.maximum(n_contributing[has_data], 1))

    combined_mask = np.where(n_contributing > 0, MaskFlag.GOOD.value, MaskFlag.MISSING.value)
    clipped_fraction = float(np.mean(usable & ~active)) if usable.any() else 0.0
    return AverageResult(value=value, uncertainty=uncertainty, n_contributing=n_contributing,
                         mask=combined_mask.astype(np.int64), method=method, weighting=weighting,
                         clipped_fraction=clipped_fraction)


def integration_gain_curve(single_spectrum_rms: float, n_values: list[int]) -> dict[str, Any]:
    """1/sqrt(N) diagnostic ONLY - never asserted as a universal law.
    Returns the theoretical reference curve for QC comparison against
    measured RMS vs N; callers persist both, never just this one."""
    n_values = sorted(set(int(n) for n in n_values if n >= 1))
    return {"n": n_values, "theoretical_rms": [single_spectrum_rms / np.sqrt(n) for n in n_values],
           "note": "diagnostic reference only; real behavior may deviate (correlated noise, RFI, drift)"}
