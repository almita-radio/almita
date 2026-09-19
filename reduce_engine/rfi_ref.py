"""RFI_REF stage: V1 use is VETO/FLAG ONLY, never adaptive cancellation or
subtraction. A bin is flagged RFI only when MAIN shows a statistically
significant excess over its own baseline AND RFI_REF shows a coincident
excess at the same frequency within the allowed time delta - "any common
signal" is deliberately NOT the criterion (that would flag the sky itself
whenever both antennas happen to see structure). Continues without
RFI_REF when unavailable - never blocks the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class RfiRefOutcome:
    available: bool
    used: bool
    time_delta_seconds: Optional[float]
    match_confidence: Optional[float]
    rfi_bins: np.ndarray  # bool, same length as frequency_hz
    reason: str


def evaluate_rfi_ref(main_frequency_hz: np.ndarray, main_psd: np.ndarray, main_baseline: np.ndarray, *,
                     ref_frequency_hz: Optional[np.ndarray], ref_psd: Optional[np.ndarray],
                     ref_baseline: Optional[np.ndarray], time_delta_seconds: Optional[float],
                     max_time_delta_seconds: float, min_match_confidence: float,
                     excess_sigma_threshold: float = 6.0) -> RfiRefOutcome:
    n = main_frequency_hz.shape[0]
    if ref_psd is None or ref_frequency_hz is None:
        return RfiRefOutcome(available=False, used=False, time_delta_seconds=time_delta_seconds,
                             match_confidence=None, rfi_bins=np.zeros(n, dtype=bool),
                             reason="RFI_REF not available for this capture")
    if not np.array_equal(main_frequency_hz, ref_frequency_hz):
        return RfiRefOutcome(available=True, used=False, time_delta_seconds=time_delta_seconds,
                             match_confidence=None, rfi_bins=np.zeros(n, dtype=bool),
                             reason="RFI_REF frequency axis incompatible with MAIN")
    if time_delta_seconds is None or time_delta_seconds > max_time_delta_seconds:
        return RfiRefOutcome(available=True, used=False, time_delta_seconds=time_delta_seconds,
                             match_confidence=None, rfi_bins=np.zeros(n, dtype=bool),
                             reason=f"RFI_REF time delta {time_delta_seconds} exceeds {max_time_delta_seconds}s")

    main_excess = (main_psd - main_baseline) / main_baseline
    ref_excess = (ref_psd - ref_baseline) / ref_baseline
    main_scale = 1.4826 * np.median(np.abs(main_excess - np.median(main_excess)))
    ref_scale = 1.4826 * np.median(np.abs(ref_excess - np.median(ref_excess)))
    main_significant = main_excess > excess_sigma_threshold * max(main_scale, 1e-12)
    ref_significant = ref_excess > excess_sigma_threshold * max(ref_scale, 1e-12)
    coincident = main_significant & ref_significant

    if not main_significant.any():
        match_confidence = 1.0  # nothing suspicious in MAIN to begin with
    else:
        match_confidence = float(np.sum(coincident) / np.sum(main_significant))
    if match_confidence < min_match_confidence and coincident.any():
        return RfiRefOutcome(available=True, used=False, time_delta_seconds=time_delta_seconds,
                             match_confidence=match_confidence, rfi_bins=np.zeros(n, dtype=bool),
                             reason=f"match_confidence {match_confidence:.2f} below threshold {min_match_confidence}")
    return RfiRefOutcome(available=True, used=True, time_delta_seconds=time_delta_seconds,
                         match_confidence=match_confidence, rfi_bins=coincident,
                         reason="coincident significant excess in MAIN and RFI_REF")
