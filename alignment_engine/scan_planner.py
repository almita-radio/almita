"""Raster scan planner (Fase 2/23): COARSE/FINE, span+spacing configurable.

Produces tangent-plane OFFSETS only (east_deg, north_deg) - never an
absolute sky coordinate. Resolving an offset to an absolute coordinate is
the target provider's job (targets/solar.py resolves fresh per point
against the Sun's live position; a static HI center resolves once via
alignment.offset_coordinates) - keeping that split is what makes the Sun
raster non-frozen (Fase 2) without needing two different planners.

Deliberately not reusing grid_generator.build_spherical_grid() directly:
that function ties offset generation to resolving a single fixed SkyCoord
center in the same call, which is exactly the "frozen grid" shape Fase 2
prohibits for the Sun. The serpentine (boustrophedon) ordering convention
is kept consistent with it, and the same explicit width/height inputs, but
this planner takes spacing directly rather than deriving it from a beam
FWHM + sampling fraction, matching the brief's literal ask for a
configurable span/spacing per stage (coarse vs fine).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ScanPoint:
    index: int
    row: int
    col: int
    east_deg: float
    north_deg: float


def _axis_positions(span_deg: float, spacing_deg: float) -> List[float]:
    if span_deg <= 0 or spacing_deg <= 0:
        raise ValueError("span_deg and spacing_deg must both be positive")
    half = span_deg / 2.0
    count = max(2, int(round(span_deg / spacing_deg)) + 1)
    return [-half + i * (span_deg / (count - 1)) for i in range(count)]


def build_raster(span_deg: float, spacing_deg: float) -> List[ScanPoint]:
    """Serpentine rectangular raster of tangent-plane offsets centered on
    (0, 0), spanning +-span_deg/2 on each axis with points every
    spacing_deg (rounded to fit an integer number of samples)."""
    axis = _axis_positions(span_deg, spacing_deg)
    points: List[ScanPoint] = []
    index = 0
    for row, north in enumerate(axis):
        columns = enumerate(axis) if row % 2 == 0 else reversed(list(enumerate(axis)))
        for col, east in columns:
            points.append(ScanPoint(index=index, row=row, col=col, east_deg=east, north_deg=north))
            index += 1
    return points


def build_coarse_then_fine(coarse_span_deg: float, coarse_spacing_deg: float,
                            fine_span_deg: float, fine_spacing_deg: float,
                            coarse_peak_east_deg: float = 0.0, coarse_peak_north_deg: float = 0.0
                            ) -> "tuple[List[ScanPoint], List[ScanPoint]]":
    """Coarse raster centered on (0,0); fine raster centered on the coarse
    stage's preliminary peak (Fase 2: "alrededor del maximo preliminar").
    Call build_raster(coarse_...) first, fit/find the peak, then call this
    (or just build_raster(fine_...) shifted) for the fine stage - kept as
    two explicit calls in engine.py rather than fused here, since the
    coarse->fine handoff needs a real measurement in between."""
    coarse = build_raster(coarse_span_deg, coarse_spacing_deg)
    fine_raw = build_raster(fine_span_deg, fine_spacing_deg)
    fine = [ScanPoint(p.index, p.row, p.col,
                       p.east_deg + coarse_peak_east_deg, p.north_deg + coarse_peak_north_deg)
            for p in fine_raw]
    return coarse, fine


def total_angular_span_deg(points: List[ScanPoint]) -> float:
    """Largest offset-from-center magnitude in the pattern - useful for a
    single "does this whole raster fit inside the visibility window" check."""
    return max(math.hypot(p.east_deg, p.north_deg) for p in points) if points else 0.0
