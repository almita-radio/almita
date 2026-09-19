"""MASK stage: builds the formal, multi-reason per-bin mask.

Every mask component states its own `source`:
- "calibration_profile": reused evidence from a validated reference ensemble
  (many captures) - preferred whenever a compatible profile is available.
- "measured_per_capture": derived from this capture's own FFT segments -
  used only when no profile is available.
- "configured_default": a conservative, explicitly-labeled fallback (e.g.
  edge fraction) - never silently assumed to be "the truth".

Never zero-fills a bad bin - only sets MaskFlag bits. detect_fixed_spurs and
measure_dc_mask_half_width are hi_spectral_metric's own primitives, reused
verbatim (not reimplemented here).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from hi_spectral_metric import (
    detect_fixed_spurs,
    dc_mask as compute_dc_mask,
    measure_dc_mask_half_width,
)
from reduce_engine.config import ReduceConfig
from reduce_engine.models import MaskFlag


@dataclass
class MaskResult:
    mask: np.ndarray  # int64, MaskFlag bit values per bin
    dc_source: str
    dc_half_width_hz: float
    spur_source: str
    spur_regions: list[dict[str, Any]]
    edge_source: str
    edge_bins_each_side: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dc_source": self.dc_source, "dc_half_width_hz": self.dc_half_width_hz,
            "spur_source": self.spur_source, "spur_regions": self.spur_regions,
            "edge_source": self.edge_source, "edge_bins_each_side": self.edge_bins_each_side,
            "reason_counts": {name: int(np.sum((self.mask.astype(np.int64) & flag.value) != 0))
                              for name, flag in MaskFlag.__members__.items() if flag != MaskFlag.GOOD},
        }


def build_mask(frequency_hz: np.ndarray, psd: np.ndarray, *, center_frequency_hz: float,
               config: ReduceConfig, segments: Optional[np.ndarray] = None,
               calibration_profile: Optional[dict[str, Any]] = None,
               clipping_fraction: Optional[float] = None) -> MaskResult:
    n = frequency_hz.shape[0]
    mask = np.zeros(n, dtype=np.int64)
    invalid = ~np.isfinite(psd) | (np.asarray(psd) <= 0)
    mask[invalid] |= MaskFlag.INVALID.value

    if calibration_profile is not None and np.array_equal(calibration_profile["frequency_hz"], frequency_hz):
        dc = calibration_profile["dc_mask"].astype(bool)
        spur = calibration_profile["spur_mask"].astype(bool)
        dc_source, spur_source = "calibration_profile", "calibration_profile"
        dc_half_width_hz = float(calibration_profile["metadata"]["dc_mask_half_width_hz"])
        spur_regions = calibration_profile["metadata"].get("spur_regions", [])
    else:
        dc_measurement = measure_dc_mask_half_width(
            frequency_hz, psd, center_frequency_hz,
            threshold_db=config.dc_mask_threshold_db,
            reference_inner_hz=config.dc_reference_inner_hz,
            reference_outer_hz=config.dc_reference_outer_hz,
        )
        dc = compute_dc_mask(frequency_hz, center_frequency_hz, dc_measurement["half_width_hz"])
        dc_source, dc_half_width_hz = "measured_per_capture", dc_measurement["half_width_hz"]
        if segments is not None and segments.shape[0] >= 4:
            spur, spur_clusters = detect_fixed_spurs(
                frequency_hz, segments, excluded_mask=dc,
                smooth_bins=config.spur_smooth_bins, z_threshold=config.spur_z_threshold,
                persistence_threshold=config.spur_persistence_threshold,
            )
            spur_source, spur_regions = "measured_per_capture", spur_clusters
        else:
            spur, spur_source, spur_regions = np.zeros(n, dtype=bool), "unavailable_insufficient_segments", []
    mask[dc] |= MaskFlag.DC.value
    mask[spur] |= MaskFlag.KNOWN_SPUR.value

    edge_bins = max(2, int(config.edge_fraction * n))
    edge_source = "configured_default"
    mask[:edge_bins] |= MaskFlag.EDGE.value
    mask[-edge_bins:] |= MaskFlag.EDGE.value

    if clipping_fraction is not None and clipping_fraction > 0.001:
        mask[:] |= MaskFlag.SATURATED.value  # ADC saturation affects the whole spectrum, not specific bins

    return MaskResult(mask=mask, dc_source=dc_source, dc_half_width_hz=float(dc_half_width_hz),
                      spur_source=spur_source, spur_regions=spur_regions,
                      edge_source=edge_source, edge_bins_each_side=edge_bins)


def usable_bin_mask(mask: np.ndarray) -> np.ndarray:
    """True where a bin carries NO exclusion reason at all."""
    return np.asarray(mask, dtype=np.int64) == MaskFlag.GOOD.value
