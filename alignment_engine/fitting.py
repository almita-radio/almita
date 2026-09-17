"""Fitting (Fase 3/6/19): wraps alignment.py's proven search fitter.

alignment.estimate_template_offset already implements the model Fase 6
asks for - a coarse-to-fine grid search maximizing correlation between
observed values and a template evaluated at each candidate (east, north)
offset, with the template's own least-squares scale+baseline fit folded
into the residual (see alignment._match: `design = [expected, ones];
fit = lstsq(design, observed)`) - i.e. Observed = A*Reference(offset) + B
is already what's being fit, not reinvented here.

This module adds what alignment.py's AlignmentEstimate does not carry:
explicit RAW/FIT/RESIDUAL separation (never overwriting raw with fitted)
and objective, numeric fit-quality metrics mapped to GOOD/MARGINAL/BAD by
documented, inspectable rules (Fase 19: "no uses palabras arbitrariamente").

RA wrap / angle handling: delegated entirely to astropy SkyCoord/
SkyOffsetFrame separations, the same mechanism alignment.py's own
test_ra_wrap_and_spherical_offset already exercises - no raw-degree
subtraction is ever done here, which is exactly how a naive implementation
would get bitten by a 0/360 wrap.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, List, Optional

import numpy as np
from astropy.coordinates import SkyCoord

from alignment import AlignmentEstimate, estimate_template_offset


@dataclass
class FitQuality:
    confidence: float
    correlation: float
    residual_rms: float
    valid_fraction: float
    peak_contrast: float
    distance_from_edge_deg: float
    rating: str


@dataclass
class FitResult:
    estimate: AlignmentEstimate
    quality: FitQuality
    raw_values: List[Optional[float]]
    valid_count: int
    total_count: int
    rejected_indices: List[int]

    def to_dict(self) -> dict:
        return {
            "estimate": asdict(self.estimate),
            "quality": asdict(self.quality),
            "valid_count": self.valid_count,
            "total_count": self.total_count,
            "rejected_indices": self.rejected_indices,
        }


def rate_fit_quality(confidence: float, correlation: float, valid_fraction: float,
                      distance_from_edge_deg: float, span_deg: float) -> str:
    """Documented thresholds (Fase 19) - deliberately simple and inspectable
    rather than a learned/opaque score:

    GOOD:     confidence>=0.75 AND correlation>=0.6 AND valid_fraction>=0.8
              AND the fitted center is not within 10% of the span's edge
              (an edge-hugging "peak" usually means the true peak is outside
              the scanned area, not that it was found).
    MARGINAL: confidence>=0.4 AND valid_fraction>=0.5.
    BAD:      anything else.
    """
    edge_margin_ok = distance_from_edge_deg > 0.1 * span_deg if span_deg > 0 else True
    if confidence >= 0.75 and correlation >= 0.6 and valid_fraction >= 0.8 and edge_margin_ok:
        return "GOOD"
    if confidence >= 0.4 and valid_fraction >= 0.5:
        return "MARGINAL"
    return "BAD"


def fit_raster(positions: SkyCoord, values: List[Optional[float]], center: SkyCoord,
               template: Callable, span_deg: float,
               search_levels=((5.5, .5), (1.0, .1), (.25, .025))) -> FitResult:
    """positions/values are RAW, one entry per attempted point (values[i] is
    None for a missing/rejected sample) - never mutated or overwritten.
    Only the valid subset is handed to the search fitter."""
    valid_mask = [v is not None for v in values]
    rejected_indices = [i for i, ok in enumerate(valid_mask) if not ok]
    valid_positions = positions[np.asarray(valid_mask)]
    valid_values = np.asarray([v for v in values if v is not None], dtype=float)

    if len(valid_values) < 4:
        estimate = AlignmentEstimate(0.0, 0.0, 0.0, 0.0, float("inf"), -1.0, len(valid_values))
    else:
        estimate = estimate_template_offset(valid_positions, valid_values, center, template,
                                             search_levels=search_levels)

    peak_contrast = (
        float((np.max(valid_values) - np.min(valid_values)) / max(abs(np.max(valid_values)), 1e-12))
        if len(valid_values) else 0.0
    )
    distance_from_edge_deg = max(0.0, span_deg / 2.0 - estimate.separation_deg)
    valid_fraction = len(valid_values) / len(values) if values else 0.0
    quality = FitQuality(
        confidence=estimate.confidence,
        correlation=estimate.score,
        residual_rms=estimate.residual,
        valid_fraction=valid_fraction,
        peak_contrast=peak_contrast,
        distance_from_edge_deg=distance_from_edge_deg,
        rating=rate_fit_quality(estimate.confidence, estimate.score, valid_fraction,
                                 distance_from_edge_deg, span_deg),
    )
    return FitResult(estimate=estimate, quality=quality, raw_values=list(values),
                      valid_count=len(valid_values), total_count=len(values),
                      rejected_indices=rejected_indices)
