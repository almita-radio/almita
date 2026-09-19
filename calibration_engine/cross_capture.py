"""Cross-capture comparison WITHIN one session (the first real test's own
requirement: "entre las 5 capturas" - RMS variation, bandpass correlation,
clipping/DC consistency). Deliberately separate from stability.py's own
analyze_stability() (which reasons about a TIME SERIES with elapsed
seconds and warm-up detection) - a short back-to-back burst like this one
is NOT enough evidence for a warm-up/thermal claim (explicit instruction:
"No inferir warm-up con solo esta prueba corta. No inferir thermal
model."), so this module only DESCRIBES the small window's own
consistency, nothing more.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class CrossCaptureSummary:
    n_captures: int
    rms_values: List[float]
    rms_mean: float
    rms_variation_fraction: float           # std/mean - dimensionless
    bandpass_correlation_to_mean: List[float]   # per capture, Pearson r against the mean normalized bandpass
    bandpass_rms_difference: float           # mean absolute per-bin deviation from the mean bandpass shape
    clipping_statuses: List[str]
    clipping_consistent: bool
    dc_mask_fractions: List[float]
    dc_consistent: bool
    note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_captures": self.n_captures, "rms_values": self.rms_values, "rms_mean": self.rms_mean,
            "rms_variation_fraction": self.rms_variation_fraction,
            "bandpass_correlation_to_mean": self.bandpass_correlation_to_mean,
            "bandpass_rms_difference": self.bandpass_rms_difference,
            "clipping_statuses": self.clipping_statuses, "clipping_consistent": self.clipping_consistent,
            "dc_mask_fractions": self.dc_mask_fractions, "dc_consistent": self.dc_consistent,
            "note": self.note,
        }


def compute_cross_capture_summary(rms_values: List[float], normalized_bandpasses: List[List[float]],
                                   clipping_statuses: List[str], dc_masks: List[List[bool]]) -> CrossCaptureSummary:
    if len(rms_values) < 2:
        raise ValueError("at least 2 captures are required for a cross-capture comparison")
    rms_arr = np.asarray(rms_values, dtype=float)
    rms_mean = float(np.mean(rms_arr))
    rms_variation = float(np.std(rms_arr) / max(rms_mean, 1e-30))

    bandpass_matrix = np.asarray(normalized_bandpasses, dtype=float)
    mean_bandpass = np.mean(bandpass_matrix, axis=0)
    correlations = []
    for row in bandpass_matrix:
        if np.std(row) < 1e-12 or np.std(mean_bandpass) < 1e-12:
            correlations.append(float("nan"))
        else:
            correlations.append(float(np.corrcoef(row, mean_bandpass)[0, 1]))
    bandpass_rms_diff = float(np.mean(np.abs(bandpass_matrix - mean_bandpass)))

    dc_arr = np.asarray(dc_masks, dtype=bool)
    dc_fractions = [float(np.mean(row)) for row in dc_arr]

    return CrossCaptureSummary(
        n_captures=len(rms_values), rms_values=list(map(float, rms_arr)), rms_mean=rms_mean,
        rms_variation_fraction=rms_variation, bandpass_correlation_to_mean=correlations,
        bandpass_rms_difference=bandpass_rms_diff, clipping_statuses=list(clipping_statuses),
        clipping_consistent=(len(set(clipping_statuses)) == 1), dc_mask_fractions=dc_fractions,
        dc_consistent=bool(np.ptp(dc_fractions) < 1e-9) if dc_fractions else True,
        note="describes ONLY this short back-to-back window's own consistency - no warm-up or thermal "
             "model inference is made from this alone (explicit instruction for this test)",
    )
