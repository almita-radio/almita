"""Operational gain sweep - simulation/dry-run/analysis (Fase 9-12, 41).

Fase 9's explicit instruction: "Primero auditar qué steps de gain soporta
realmente el V4. No inventar lista." Audit result (this pass): this repo
has NO code that queries the tuner for its supported gain list at runtime
(rtl_tcp's protocol has no such readback - same audit finding as
receiver_config.py's gain_effective_db). The historical values actually
used in real hardware tests here (rf_gain_control_test.py,
rf_chain_characterization.py: 0.0, 8.7, 20.7, 29.7, 40.2, 49.6) are a
subset of the well-documented R820T2/R828D discrete gain table (the same
table librtlsdr itself ships, in tenths of dB) - NOT invented for this
pass, but also NOT independently re-verified against this specific unit.
CANDIDATE_GAIN_TABLE_DB below is that full published table, carried with
an explicit, honest provenance status rather than silently presented as
confirmed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from calibration_engine.clipping import ClippingResult, ClippingStatus, ClippingThresholds, evaluate_clipping
from calibration_engine.sample_statistics import RawSampleStatistics, compute_sample_statistics

GAIN_TABLE_PROVENANCE = "ASSUMED_FROM_R820T2_R828D_PUBLISHED_TUNER_TABLE_NOT_QUERIED_AT_RUNTIME"

# librtlsdr's own r82xx_gains table (tenths of dB -> dB). Real, physical,
# published values - not this repo's invention - but see
# GAIN_TABLE_PROVENANCE above for what "real" does NOT yet mean here.
CANDIDATE_GAIN_TABLE_DB: tuple = (
    0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 10.0, 10.6, 11.2, 12.5, 14.4, 15.7, 16.6, 19.7,
    20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9,
    44.5, 48.0, 49.6,
)


def build_gain_sweep_plan(around_gain_db: float = 40.2, n_steps: int = 5) -> List[float]:
    """Fase 9: "varios steps bajos/medios/altos alrededor de 40.2" - picks
    `n_steps` values from the REAL candidate table spanning low/mid/high
    around `around_gain_db`, never a fabricated list. Always includes the
    closest table entry to `around_gain_db` itself."""
    table = np.asarray(CANDIDATE_GAIN_TABLE_DB, dtype=float)
    center_index = int(np.argmin(np.abs(table - around_gain_db)))
    n_steps = max(3, min(n_steps, len(table)))
    indices = np.unique(np.linspace(0, len(table) - 1, n_steps).round().astype(int))
    indices = np.unique(np.append(indices, center_index))
    return sorted(float(table[i]) for i in indices)


@dataclass
class GainStepResult:
    gain_requested_db: float
    sample_statistics: RawSampleStatistics
    clipping: ClippingResult
    relative_digital_power: float     # e.g. median PSD or RMS^2, same arbitrary units across the whole sweep
    relative_contrast: Optional[float] = None   # spectral peak-to-median ratio - never called "SNR"

    def to_dict(self) -> Dict[str, Any]:
        return {"gain_requested_db": self.gain_requested_db,
                "sample_statistics": self.sample_statistics.to_dict(),
                "clipping": self.clipping.to_dict(), "relative_digital_power": self.relative_digital_power,
                "relative_contrast": self.relative_contrast}


def compute_gain_step_result(iq: np.ndarray, gain_requested_db: float,
                              thresholds: ClippingThresholds = ClippingThresholds()) -> GainStepResult:
    """The correct, AC-coupled power proxy for a gain sweep (Fase 10's
    "median PSD" list item): std_i/std_q already remove the DC pedestal
    (variance is mean-subtracted by construction), unlike raw RMS of ADC
    codes (sample_statistics.RawSampleStatistics.rms), which is dominated
    by the ~127.5 DC pedestal and stays ~flat across gain - using raw RMS
    here would make a real gain sweep look falsely non-monotonic."""
    stats = compute_sample_statistics(iq)
    clipping = evaluate_clipping(stats, thresholds)
    relative_digital_power = (stats.std_i ** 2 + stats.std_q ** 2) / 2.0
    return GainStepResult(gain_requested_db=gain_requested_db, sample_statistics=stats, clipping=clipping,
                           relative_digital_power=relative_digital_power)


@dataclass
class LinearityFinding:
    kind: str          # "plateau" | "discontinuity" | "reversal" | "clipping_onset"
    gain_low_db: float
    gain_high_db: float
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "gain_low_db": self.gain_low_db, "gain_high_db": self.gain_high_db,
                "detail": self.detail}


def analyze_gain_linearity(steps: Sequence[GainStepResult], plateau_fraction: float = 0.02,
                            discontinuity_fraction: float = 3.0) -> Dict[str, Any]:
    """Fase 11: relative-only. Detects plateaus/discontinuities/reversals
    in gain-step vs relative_digital_power - NEVER infers exact RF dB
    linearity from this (no generator, no calibrated input)."""
    ordered = sorted(steps, key=lambda s: s.gain_requested_db)
    if len(ordered) < 2:
        return {"findings": [], "monotonic": None, "reason": "fewer than 2 gain steps - cannot assess"}
    findings: List[LinearityFinding] = []
    powers = np.asarray([s.relative_digital_power for s in ordered], dtype=float)
    gains = np.asarray([s.gain_requested_db for s in ordered], dtype=float)
    deltas = np.diff(powers)
    monotonic = bool(np.all(deltas >= 0))
    for i, delta in enumerate(deltas):
        relative_step = delta / max(powers[i], 1e-30)
        if delta < 0:
            findings.append(LinearityFinding("reversal", gains[i], gains[i + 1],
                                              f"power decreased ({powers[i]:.4g} -> {powers[i+1]:.4g}) "
                                              f"as gain increased"))
        elif abs(relative_step) < plateau_fraction:
            findings.append(LinearityFinding("plateau", gains[i], gains[i + 1],
                                              f"relative change {relative_step:.4f} below {plateau_fraction} "
                                              f"threshold"))
        elif relative_step > discontinuity_fraction:
            findings.append(LinearityFinding("discontinuity", gains[i], gains[i + 1],
                                              f"relative change {relative_step:.2f} exceeds "
                                              f"{discontinuity_fraction}x threshold"))
        if ordered[i + 1].clipping.status == ClippingStatus.CLIPPED and ordered[i].clipping.status != ClippingStatus.CLIPPED:
            findings.append(LinearityFinding("clipping_onset", gains[i], gains[i + 1],
                                              "clipping begins between these two steps"))
    return {"findings": [f.to_dict() for f in findings], "monotonic": monotonic,
            "n_steps": len(ordered), "gain_range_db": [float(gains.min()), float(gains.max())]}


@dataclass
class GainRecommendation:
    recommended_gain_db: Optional[float]
    alternatives_db: List[float]
    rejected: List[Dict[str, Any]]
    status: str        # "OK" | "INCONCLUSIVE"
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"recommended_gain_db": self.recommended_gain_db, "alternatives_db": self.alternatives_db,
                "rejected": self.rejected, "status": self.status, "reason": self.reason}


def recommend_operational_gain(steps: Sequence[GainStepResult],
                                comfortable_margin_multiplier: float = 1.5) -> GainRecommendation:
    """Fase 12 + 41: evidence-based, never "highest gain wins" and never
    "we've always used this one". Prefers the HIGHEST gain that is not
    just clipping-free but has COMFORTABLE headroom margin (a multiple of
    the WARNING threshold, not just barely under it) - among ties in that
    set, the reasoning is spelled out in `reason` rather than picking
    silently."""
    if len(steps) < 2:
        return GainRecommendation(None, [], [], "INCONCLUSIVE",
                                   "fewer than 2 gain steps tested - insufficient evidence for a recommendation")

    ok_steps = [s for s in steps if s.clipping.status == ClippingStatus.OK]
    rejected = [{"gain_db": s.gain_requested_db, "status": s.clipping.status.value, "reason": s.clipping.reason}
                for s in steps if s.clipping.status != ClippingStatus.OK]
    if not ok_steps:
        return GainRecommendation(None, [], rejected, "INCONCLUSIVE",
                                   "no gain step evaluated OK for clipping - every tested gain clips or warns")

    threshold = ok_steps[0].clipping.thresholds.get("warning_percentile_margin_codes", 8.0)
    comfortable = [s for s in ok_steps
                   if s.clipping.percentile_margin_codes is not None and
                   s.clipping.percentile_margin_codes >= threshold * comfortable_margin_multiplier]
    pool = comfortable if comfortable else ok_steps
    pool_sorted = sorted(pool, key=lambda s: s.gain_requested_db, reverse=True)
    recommended = pool_sorted[0]
    alternatives = [s.gain_requested_db for s in pool_sorted[1:]]
    reason = (f"highest gain with {'comfortable' if comfortable else 'acceptable (no comfortable option found)'} "
              f"headroom margin ({recommended.clipping.percentile_margin_codes:.1f} codes, "
              f"threshold {threshold:.1f}); clipping={recommended.clipping.status.value}, "
              f"relative_contrast={recommended.relative_contrast}")
    return GainRecommendation(recommended.gain_requested_db, alternatives, rejected, "OK", reason)
