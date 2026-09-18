"""Relative bandpass characterization (Fase 13, 14, 25, 26) - never absolute
power. Reuses hi_spectral_metric.py's already-tested PSD/DC/spur primitives
rather than reimplementing spectral analysis; this module only adds the
calibration-specific framing (normalized shape, edge measurement, DC
characterization, persistent-bad-region candidate mask) on top of them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

from hi_spectral_metric import dc_mask as _dc_mask, detect_fixed_spurs, measure_dc_mask_half_width, robust_psd_from_iq


@dataclass
class BandpassResult:
    frequency_hz: List[float]
    raw_psd: List[float]                 # untouched - Fase 13: "No aplanar destructivamente"
    normalized_bandpass: List[float]     # raw_psd / median(raw_psd[valid])
    dc_mask: List[bool]
    dc_half_width_hz: float
    edge_mask: List[bool]
    usable_band_fraction: float
    edge_rolloff_threshold: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frequency_hz": self.frequency_hz, "raw_psd": self.raw_psd,
            "normalized_bandpass": self.normalized_bandpass, "dc_mask": self.dc_mask,
            "dc_half_width_hz": self.dc_half_width_hz, "edge_mask": self.edge_mask,
            "usable_band_fraction": self.usable_band_fraction,
            "edge_rolloff_threshold": self.edge_rolloff_threshold,
        }


def characterize_edge_rolloff(normalized_bandpass: np.ndarray, threshold_fraction: float = 0.5) -> np.ndarray:
    """Fase 26: measured, not assumed. `threshold_fraction` is a relative
    drop from the flat interior median (default 0.5 = -3.01dB is the
    conventional half-power point, not a repo-specific custom value) -
    starting from each edge, bins are masked out while they stay below
    that fraction of the interior level; the mask stops growing the moment
    a bin recovers, so this reports the REAL measured rolloff extent
    rather than a fixed percentage of the band."""
    values = np.asarray(normalized_bandpass, dtype=float)
    n = len(values)
    interior = values[n // 4: 3 * n // 4]
    interior_level = float(np.median(interior[np.isfinite(interior)])) if interior.size else 1.0
    mask = np.zeros(n, dtype=bool)
    threshold = threshold_fraction * interior_level
    for start, step in ((0, 1), (n - 1, -1)):
        index = start
        while 0 <= index < n and (not np.isfinite(values[index]) or values[index] < threshold):
            mask[index] = True
            index += step
    return mask


def compute_bandpass(iq: np.ndarray, sample_rate_hz: float, center_frequency_hz: float,
                      fft_size: int = 8192, edge_rolloff_threshold: float = 0.5) -> BandpassResult:
    frequency, psd = robust_psd_from_iq(iq, sample_rate_hz, center_frequency_hz, fft_size=fft_size)
    dc_measurement = measure_dc_mask_half_width(frequency, psd, center_frequency_hz)
    dc = _dc_mask(frequency, center_frequency_hz, dc_measurement["half_width_hz"])
    valid_for_norm = ~dc
    median_level = float(np.median(psd[valid_for_norm])) if np.any(valid_for_norm) else float(np.median(psd))
    normalized = psd / max(median_level, 1e-30)
    edge = characterize_edge_rolloff(normalized, edge_rolloff_threshold)
    usable_fraction = float(np.mean(~(dc | edge)))
    return BandpassResult(
        frequency_hz=frequency.tolist(), raw_psd=psd.tolist(), normalized_bandpass=normalized.tolist(),
        dc_mask=dc.tolist(), dc_half_width_hz=dc_measurement["half_width_hz"], edge_mask=edge.tolist(),
        usable_band_fraction=usable_fraction, edge_rolloff_threshold=edge_rolloff_threshold,
    )


@dataclass
class BadRegionCandidate:
    lo_bin: int
    hi_bin: int
    frequency_hz: float
    score: float
    persistence: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"lo_bin": self.lo_bin, "hi_bin": self.hi_bin, "frequency_hz": self.frequency_hz,
                "score": self.score, "persistence": self.persistence, "reason": self.reason}


def recommend_mask(frequency_hz: np.ndarray, spectra: np.ndarray, dc: np.ndarray, edge: np.ndarray,
                    persistence_threshold: float = 0.70) -> Dict[str, Any]:
    """Fase 14: with MULTIPLE captures under the same config, find bins
    that persistently misbehave (DC artifact already excluded via `dc`,
    edge rolloff via `edge`) and produce a recommended_mask.json for HUMAN
    REVIEW ONLY - never auto-applied to science (Fase 14, Fase 49)."""
    excluded = np.asarray(dc, dtype=bool) | np.asarray(edge, dtype=bool)
    spur_mask, clusters = detect_fixed_spurs(frequency_hz, spectra, excluded_mask=excluded,
                                              persistence_threshold=persistence_threshold)
    regions = [BadRegionCandidate(c["lo_bin"], c["hi_bin"], c["frequency_hz"], c["score"], c["persistence"],
                                   "persistent_narrow_feature_across_captures").to_dict()
               for c in clusters]
    return {
        "status": "DRAFT_FOR_HUMAN_REVIEW", "auto_applied": False,
        "n_captures": int(np.asarray(spectra).shape[0]), "persistence_threshold": persistence_threshold,
        "candidate_regions": regions, "candidate_mask": spur_mask.tolist(),
        "note": "This mask is a recommendation only (Fase 14/49) - it is never applied to science "
                "automatically. A human reviews candidate_regions and decides.",
    }
