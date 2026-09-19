"""RESAMPLE TO COMMON GRID stage. Per ALMITA's existing NATIVE_GRID
contract (never interpolate when a common grid already exists), this is a
no-op whenever every input spectrum already shares the same frequency
axis (same fft_size/sample_rate/center_frequency) - which is the common
case within one campaign. Interpolation is only invoked to reconcile
genuinely different grids (e.g. combining sessions with different sample
rates), and only then does it touch masks/uncertainty at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from reduce_engine.models import MaskFlag


@dataclass
class ResampleResult:
    frequency_hz: np.ndarray
    values: list[np.ndarray]
    uncertainties: list[np.ndarray]
    masks: list[np.ndarray]
    method: str


def resample_to_common_grid(frequency_grids: list[np.ndarray], values: list[np.ndarray],
                            uncertainties: list[np.ndarray], masks: list[np.ndarray]) -> ResampleResult:
    if all(np.array_equal(frequency_grids[0], grid) for grid in frequency_grids[1:]):
        return ResampleResult(frequency_hz=frequency_grids[0], values=values, uncertainties=uncertainties,
                              masks=masks, method="native_grid_no_resampling_required")

    reference = frequency_grids[0]
    out_values, out_unc, out_mask = [], [], []
    for grid, value, unc, mask in zip(frequency_grids, values, uncertainties, masks):
        usable = (np.asarray(mask, dtype=np.int64) == MaskFlag.GOOD.value)
        interp_value = np.interp(reference, grid, value, left=np.nan, right=np.nan)
        interp_unc = np.interp(reference, grid, unc, left=np.nan, right=np.nan)
        # a resampled bin is only GOOD if the two nearest source bins it
        # was interpolated from were both GOOD - never converts a masked
        # gap into fabricated data.
        usable_at_ref = np.interp(reference, grid, usable.astype(float), left=0.0, right=0.0) >= 0.999
        interp_mask = np.where(usable_at_ref, MaskFlag.GOOD.value, MaskFlag.MISSING.value).astype(np.int64)
        out_values.append(interp_value)
        out_unc.append(interp_unc)
        out_mask.append(interp_mask)
    return ResampleResult(frequency_hz=reference, values=out_values, uncertainties=out_unc,
                          masks=out_mask, method="linear_interpolation_mask_aware")
