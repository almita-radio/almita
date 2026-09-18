"""Session/context comparability (Fase 29, 43, 65, 68) - same spirit as
alignment_engine/hi/repeatability.py's compare_alignment_results(), applied
to calibration context and results instead of sky offsets.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np


class ComparabilityVerdict(str, Enum):
    COMPARABLE = "COMPARABLE"
    PARTIALLY_COMPARABLE = "PARTIALLY_COMPARABLE"
    NOT_COMPARABLE = "NOT_COMPARABLE"


@dataclass
class ContextComparisonResult:
    verdict: ComparabilityVerdict
    reasons: List[str]
    flags: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict.value, "reasons": self.reasons, "flags": self.flags}


def compare_calibration_context(a, b, *, temperature_flag_delta_c: float = 5.0) -> ContextComparisonResult:
    """`a`/`b`: anything with the ReceiverConfigSnapshot attributes
    (receiver_id, serial, sample_rate_hz, center_frequency_hz, gain_requested_db,
    bias_t_state, deployment_state, temperature_readings). Hard
    disqualifiers (Fase 43): different receiver, different sample rate ->
    NOT_COMPARABLE outright. Gain/temperature/deployment differences are
    flagged, not necessarily disqualifying, per metric."""
    reasons: List[str] = []
    flags: List[str] = []

    if a.receiver_id != b.receiver_id or a.serial != b.serial:
        reasons.append(f"different receiver: {a.receiver_id}/{a.serial} vs {b.receiver_id}/{b.serial}")
        return ContextComparisonResult(ComparabilityVerdict.NOT_COMPARABLE, reasons, flags)
    if a.sample_rate_hz != b.sample_rate_hz:
        reasons.append(f"different sample rate: {a.sample_rate_hz} vs {b.sample_rate_hz}")
        return ContextComparisonResult(ComparabilityVerdict.NOT_COMPARABLE, reasons, flags)
    if a.center_frequency_hz != b.center_frequency_hz:
        reasons.append(f"different center frequency: {a.center_frequency_hz} vs {b.center_frequency_hz}")
        return ContextComparisonResult(ComparabilityVerdict.NOT_COMPARABLE, reasons, flags)

    partial = False
    if a.gain_requested_db != b.gain_requested_db:
        flags.append(f"different gain: {a.gain_requested_db} vs {b.gain_requested_db} - "
                      f"comparable for gain-independent metrics only (e.g. frequency axis), "
                      f"NOT for relative power/bandpass level")
        partial = True
    if a.bias_t_state != b.bias_t_state:
        flags.append(f"different Bias-T state: {a.bias_t_state} vs {b.bias_t_state}")
        partial = True
    if a.deployment_state != b.deployment_state:
        flags.append(f"different deployment context: {a.deployment_state} vs {b.deployment_state} - "
                      f"INDOOR/BENCH results are not portable to sky-bandpass/RFI-environment/"
                      f"antenna-response conclusions (Fase 68)")
        partial = True

    temps_a = [v.get("temperature_c") for v in getattr(a, "temperature_readings", {}).values()
               if isinstance(v, dict) and v.get("valid")]
    temps_b = [v.get("temperature_c") for v in getattr(b, "temperature_readings", {}).values()
               if isinstance(v, dict) and v.get("valid")]
    if temps_a and temps_b:
        delta = abs(np.mean(temps_a) - np.mean(temps_b))
        if delta > temperature_flag_delta_c:
            flags.append(f"temperature differs by {delta:.1f}C (> {temperature_flag_delta_c}C flag threshold)")
            partial = True

    if not reasons and not flags:
        reasons.append("receiver, sample rate, center frequency, gain, Bias-T, deployment and "
                        "temperature all match within tolerance")
        return ContextComparisonResult(ComparabilityVerdict.COMPARABLE, reasons, flags)
    verdict = ComparabilityVerdict.PARTIALLY_COMPARABLE if partial else ComparabilityVerdict.COMPARABLE
    if not reasons:
        reasons.append("core config (receiver/sample rate/center frequency) matches; see flags for differences")
    return ContextComparisonResult(verdict, reasons, flags)


@dataclass
class SessionComparisonResult:
    verdict: str        # "CONSISTENT" | "MARGINAL" | "INCONSISTENT" | "NOT_COMPARABLE"
    context: ContextComparisonResult
    reason: str
    metric_deltas: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "context": self.context.to_dict(), "reason": self.reason,
                "metric_deltas": self.metric_deltas}


# Same "documented, not invented per-comparison" spirit as
# alignment_engine/hi/repeatability.py's sigma bands - these are fractional
# differences in already-relative (unitless) quantities, not new physical
# thresholds.
CONSISTENT_MAX_FRACTIONAL_DELTA = 0.10
MARGINAL_MAX_FRACTIONAL_DELTA = 0.30


def compare_calibration_sessions(config_a, config_b, rms_a: float, rms_b: float,
                                  clipping_fraction_a: float, clipping_fraction_b: float) -> SessionComparisonResult:
    context = compare_calibration_context(config_a, config_b)
    if context.verdict == ComparabilityVerdict.NOT_COMPARABLE:
        return SessionComparisonResult("NOT_COMPARABLE", context,
                                        "sessions are not comparable at the context level - see context.reasons",
                                        {})
    rms_delta = abs(rms_a - rms_b) / max(min(rms_a, rms_b), 1e-30)
    clip_delta = abs(clipping_fraction_a - clipping_fraction_b)
    deltas = {"rms_fractional_delta": rms_delta, "clipping_fraction_delta": clip_delta}
    worst = max(rms_delta, clip_delta * 10)  # a clipping fraction change is weighted more heavily than RMS drift
    if worst <= CONSISTENT_MAX_FRACTIONAL_DELTA:
        verdict = "CONSISTENT"
    elif worst <= MARGINAL_MAX_FRACTIONAL_DELTA:
        verdict = "MARGINAL"
    else:
        verdict = "INCONSISTENT"
    reason = f"rms_fractional_delta={rms_delta:.3f}, clipping_fraction_delta={clip_delta:.4f}"
    if context.flags:
        reason += f"; context flags: {'; '.join(context.flags)}"
    return SessionComparisonResult(verdict, context, reason, deltas)
