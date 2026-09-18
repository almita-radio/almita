"""Objective clipping/headroom classification (Fase 8) - never "looks ugly".

Thresholds are configurable and their defaults are documented below with
the reasoning, not picked to make any particular historical gain (e.g.
40.2 dB) come out looking good - they are evaluated the same way
regardless of which gain produced the statistics.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from calibration_engine.sample_statistics import RawSampleStatistics


class ClippingStatus(str, Enum):
    OK = "OK"
    WARNING = "WARNING"
    CLIPPED = "CLIPPED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ClippingThresholds:
    # Any actual rail hit (code 0 or 255) above this fraction is a real,
    # unambiguous clipping event - not a judgment call. 1e-4 (1 in 10000
    # samples) is small enough that a handful of coincidental rail-value
    # noise samples at a healthy gain won't trip it, but a genuinely
    # saturated front end (which rails on a large fraction of samples)
    # trips it immediately.
    clipped_rail_hit_fraction: float = 1.0e-4
    # Below this rail-hit fraction but above this near-rail fraction: the
    # signal is close enough to full scale that a small additional gain
    # bump, or a slightly stronger real signal, would clip it - flagged as
    # a headroom warning, not a failure.
    warning_near_rail_fraction: float = 0.01
    # A percentile-to-full-scale measure: if the 99.9th percentile is
    # already within this many codes of the rail, headroom is thin even
    # with zero actual rail hits.
    warning_percentile_margin_codes: float = 8.0


@dataclass
class ClippingResult:
    status: ClippingStatus
    reason: str
    rail_hit_fraction: Optional[float]
    near_rail_fraction: Optional[float]
    crest_factor: Optional[float]
    percentile_margin_codes: Optional[float]
    thresholds: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value, "reason": self.reason,
            "rail_hit_fraction": self.rail_hit_fraction, "near_rail_fraction": self.near_rail_fraction,
            "crest_factor": self.crest_factor, "percentile_margin_codes": self.percentile_margin_codes,
            "thresholds": self.thresholds,
        }


def evaluate_clipping(stats: Optional[RawSampleStatistics],
                       thresholds: ClippingThresholds = ClippingThresholds()) -> ClippingResult:
    threshold_dict = {
        "clipped_rail_hit_fraction": thresholds.clipped_rail_hit_fraction,
        "warning_near_rail_fraction": thresholds.warning_near_rail_fraction,
        "warning_percentile_margin_codes": thresholds.warning_percentile_margin_codes,
    }
    if stats is None:
        return ClippingResult(ClippingStatus.UNKNOWN, "no sample statistics available",
                               None, None, None, None, threshold_dict)
    if stats.n_samples == 0:
        return ClippingResult(ClippingStatus.UNKNOWN, "zero samples", None, None, None, None, threshold_dict)

    crest_factor = (max(abs(stats.maximum - 127.5), abs(stats.minimum - 127.5)) /
                     max(1e-9, (stats.std_i + stats.std_q) / 2))
    percentile_margin = min(255.0 - stats.percentiles["p99_9"], stats.percentiles["p0_1"] - 0.0)

    if stats.rail_hit_fraction > thresholds.clipped_rail_hit_fraction:
        return ClippingResult(ClippingStatus.CLIPPED,
                               f"rail_hit_fraction={stats.rail_hit_fraction:.2e} exceeds "
                               f"{thresholds.clipped_rail_hit_fraction:.2e}",
                               stats.rail_hit_fraction, stats.near_rail_fraction, crest_factor,
                               percentile_margin, threshold_dict)
    if (stats.near_rail_fraction > thresholds.warning_near_rail_fraction or
            percentile_margin < thresholds.warning_percentile_margin_codes):
        return ClippingResult(ClippingStatus.WARNING,
                               f"near_rail_fraction={stats.near_rail_fraction:.4f} or "
                               f"percentile_margin={percentile_margin:.1f} codes indicates thin headroom",
                               stats.rail_hit_fraction, stats.near_rail_fraction, crest_factor,
                               percentile_margin, threshold_dict)
    return ClippingResult(ClippingStatus.OK, "no rail hits, sufficient headroom margin",
                           stats.rail_hit_fraction, stats.near_rail_fraction, crest_factor,
                           percentile_margin, threshold_dict)
