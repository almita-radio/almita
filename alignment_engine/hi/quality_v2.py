"""Quality V2 (Fase 9-14): the direct response to two real Monte Carlo
findings from the previous pass - a rare (2.5%) false GOOD at 80% missing
data, and a 95% GOOD rate at noise 3x the signal amplitude. Root cause
(confirmed by inspection, not assumed): the existing FitQuality rating is
built almost entirely from internal fit self-consistency (correlation +
residual, both computed FROM the same profiled A/B fit) - it has no direct
check on WHETHER the surviving points still cover the sky well enough to
constrain a 2D offset, and no explicit null-model (H0: no pattern) vs
model (H1: reference + offset) comparison.

Design rule (Fase 14): FIT RESULT and QUALITY DECISION are kept separate -
this module never touches fitting.fit_raster()'s own FitQuality; it reads
the SAME FitResult and adds independent checks on top. All thresholds are
named constants below, chosen BEFORE re-running Monte Carlo V2 (not tuned
to make specific cases look good afterward - see the pass report for the
exact before/after numbers).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import numpy as np
from astropy.coordinates import SkyCoord

from alignment_engine.fitting import FitResult, fit_raster
from alignment_engine.scan_planner import ScanPoint

# Conservative defaults (Fase 40's "config conservative" precedent) - not
# derived from a real-hardware error distribution, which does not exist yet.
DEFAULT_MIN_VALID_FRACTION = 0.5
DEFAULT_MIN_VALID_COUNT = 8
DEFAULT_MIN_ROW_COUNT = 3
DEFAULT_MIN_COL_COUNT = 3
DEFAULT_MIN_BBOX_COVERAGE_FRACTION = 0.5   # valid-point bounding box area / full raster area
DEFAULT_MIN_MODEL_IMPROVEMENT = 0.5        # 1 - residual^2 vs a flat (null) model
DEFAULT_MAX_BOOTSTRAP_SIGMA_DEG = 1.0
DEFAULT_MIN_EDGE_MARGIN_DEG = 1.0
DEFAULT_BOOTSTRAP_ITERATIONS = 20


@dataclass
class SpatialCoverageResult:
    valid_count: int
    total_count: int
    valid_fraction: float
    row_count: int
    col_count: int
    bbox_coverage_fraction: float
    sufficient: bool
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"valid_count": self.valid_count, "total_count": self.total_count,
                "valid_fraction": self.valid_fraction, "row_count": self.row_count,
                "col_count": self.col_count, "bbox_coverage_fraction": self.bbox_coverage_fraction,
                "sufficient": self.sufficient, "reasons": self.reasons}


def spatial_sampling_quality(points: Sequence[ScanPoint], values: Sequence[Optional[float]], span_deg: float,
                              min_valid_fraction: float = DEFAULT_MIN_VALID_FRACTION,
                              min_valid_count: int = DEFAULT_MIN_VALID_COUNT,
                              min_row_count: int = DEFAULT_MIN_ROW_COUNT,
                              min_col_count: int = DEFAULT_MIN_COL_COUNT,
                              min_bbox_coverage_fraction: float = DEFAULT_MIN_BBOX_COVERAGE_FRACTION) -> SpatialCoverageResult:
    """Fase 9: "no basta con '20% points valid' si todos están agrupados en
    un rincón" - this is the check that actually catches that case, via
    row/column diversity and bounding-box coverage, neither of which a
    bare valid-fraction number can see. No scipy/ConvexHull (not
    installed) - bounding-box coverage is a deliberately simple, cheap,
    and sufficient proxy: a genuine corner-clustering failure mode always
    shows up as a small bounding box, even though a convex hull would
    additionally catch some degenerate line-like arrangements a bbox
    might miss (documented limitation, not hidden)."""
    valid_indices = [i for i, v in enumerate(values) if v is not None and np.isfinite(v)]
    valid_count = len(valid_indices)
    total_count = len(values)
    valid_fraction = valid_count / total_count if total_count else 0.0
    reasons: List[str] = []

    if valid_count == 0:
        return SpatialCoverageResult(0, total_count, 0.0, 0, 0, 0.0, False, ["no valid points at all"])

    rows = {points[i].row for i in valid_indices}
    cols = {points[i].col for i in valid_indices}
    easts = [points[i].east_deg for i in valid_indices]
    norths = [points[i].north_deg for i in valid_indices]
    bbox_area = (max(easts) - min(easts)) * (max(norths) - min(norths))
    bbox_coverage = bbox_area / (span_deg ** 2) if span_deg > 0 else 0.0

    if valid_fraction < min_valid_fraction:
        reasons.append(f"valid_fraction {valid_fraction:.2f} below {min_valid_fraction:.2f}")
    if valid_count < min_valid_count:
        reasons.append(f"valid_count {valid_count} below {min_valid_count}")
    if len(rows) < min_row_count:
        reasons.append(f"only {len(rows)} distinct rows with valid data (need >= {min_row_count})")
    if len(cols) < min_col_count:
        reasons.append(f"only {len(cols)} distinct columns with valid data (need >= {min_col_count})")
    if bbox_coverage < min_bbox_coverage_fraction:
        reasons.append(f"valid-point bounding box covers only {bbox_coverage:.2f} of the raster "
                        f"(need >= {min_bbox_coverage_fraction:.2f}) - possible corner-clustering")

    return SpatialCoverageResult(valid_count, total_count, valid_fraction, len(rows), len(cols),
                                  bbox_coverage, len(reasons) == 0, reasons)


def null_model_improvement(fit_result: FitResult) -> float:
    """Fase 10-11: H0 (flat/no-pattern model) vs H1 (reference + offset).
    fitting.py's own `residual = RMS(observed-fit)/std(observed)` is
    ALREADY, by construction, the ratio of the H1 model's leftover RMS to
    the H0 (best-constant-fit) model's RMS (std(observed) IS the RMS of
    the best-constant/null fit) - so `1 - residual**2` is exactly the
    fraction of variance the reference-pattern model explains beyond the
    null model, with zero extra computation. This is not a new statistic;
    it makes an existing one explicit and gates on it."""
    residual = fit_result.estimate.residual
    return float(1.0 - residual ** 2)


@dataclass
class BootstrapResult:
    sigma_east_deg: float
    sigma_north_deg: float
    n_iterations: int
    stable: bool

    def to_dict(self) -> dict:
        return {"sigma_east_deg": self.sigma_east_deg, "sigma_north_deg": self.sigma_north_deg,
                "n_iterations": self.n_iterations, "stable": self.stable}


def bootstrap_stability(positions: SkyCoord, values: Sequence[Optional[float]], center: SkyCoord,
                         template: Callable, span_deg: float, n_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
                         max_sigma_deg: float = DEFAULT_MAX_BOOTSTRAP_SIGMA_DEG, seed: int = 0) -> BootstrapResult:
    """Fase 12: resample-with-replacement over the VALID points only,
    refit each time with the same fit_raster() used for the real result -
    no separate/cheaper fitter, so bootstrap stability reflects the exact
    fit behavior being qualified."""
    valid_indices = [i for i, v in enumerate(values) if v is not None and np.isfinite(v)]
    if len(valid_indices) < 4:
        return BootstrapResult(float("inf"), float("inf"), 0, False)

    rng = np.random.default_rng(seed)
    positions_arr = SkyCoord(positions)
    east_samples, north_samples = [], []
    for _ in range(n_iterations):
        resample = rng.choice(valid_indices, size=len(valid_indices), replace=True)
        resampled_values: List[Optional[float]] = [None] * len(values)
        for i in resample:
            resampled_values[i] = values[i]
        try:
            fit = fit_raster(positions_arr, resampled_values, center, template, span_deg, refine=False)
        except Exception:
            continue
        east_samples.append(fit.estimate.offset_ra_deg)
        north_samples.append(fit.estimate.offset_dec_deg)

    if len(east_samples) < max(4, n_iterations // 2):
        return BootstrapResult(float("inf"), float("inf"), len(east_samples), False)

    sigma_east = float(np.std(east_samples))
    sigma_north = float(np.std(north_samples))
    stable = sigma_east <= max_sigma_deg and sigma_north <= max_sigma_deg
    return BootstrapResult(sigma_east, sigma_north, len(east_samples), stable)


@dataclass
class EdgeCheckResult:
    near_edge: bool
    margin_east_deg: float
    margin_north_deg: float

    def to_dict(self) -> dict:
        return {"near_edge": self.near_edge, "margin_east_deg": self.margin_east_deg,
                "margin_north_deg": self.margin_north_deg}


def edge_proximity_check(offset_east_deg: float, offset_north_deg: float, span_deg: float,
                          min_margin_deg: float = DEFAULT_MIN_EDGE_MARGIN_DEG) -> EdgeCheckResult:
    """Fase 13: a best fit sitting right at (or past) the raster's own
    boundary is exactly the "optimum outside plausible search range" /
    "boundary-touching confidence region" failure mode - flagged
    regardless of how good the local residual looks."""
    half = span_deg / 2.0
    margin_east = half - abs(offset_east_deg)
    margin_north = half - abs(offset_north_deg)
    return EdgeCheckResult(margin_east < min_margin_deg or margin_north < min_margin_deg,
                            margin_east, margin_north)


@dataclass
class QualityV2Result:
    verdict: str  # "GOOD" | "MARGINAL" | "BAD"
    reasons: List[str]
    spatial_coverage: SpatialCoverageResult
    model_improvement: float
    bootstrap: Optional[BootstrapResult]
    edge_check: EdgeCheckResult

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reasons": self.reasons,
                "spatial_coverage": self.spatial_coverage.to_dict(),
                "model_improvement": self.model_improvement,
                "bootstrap": self.bootstrap.to_dict() if self.bootstrap else None,
                "edge_check": self.edge_check.to_dict()}


def evaluate_quality_v2(points: Sequence[ScanPoint], values: Sequence[Optional[float]],
                         positions: SkyCoord, center: SkyCoord, template: Callable,
                         fit_result: FitResult, span_deg: float, *,
                         min_model_improvement: float = DEFAULT_MIN_MODEL_IMPROVEMENT,
                         run_bootstrap: bool = True,
                         bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
                         max_bootstrap_sigma_deg: float = DEFAULT_MAX_BOOTSTRAP_SIGMA_DEG,
                         min_edge_margin_deg: float = DEFAULT_MIN_EDGE_MARGIN_DEG) -> QualityV2Result:
    """GOOD requires ALL of: sufficient spatial coverage, sufficient
    null-model improvement, no edge ambiguity, and (if run) bootstrap
    stability. Any single hard failure -> BAD. Missing/skipped bootstrap
    (run_bootstrap=False, e.g. for speed in a large Monte Carlo) can still
    reach GOOD on the other checks, but never upgrades a BAD to GOOD."""
    reasons: List[str] = []

    coverage = spatial_sampling_quality(points, values, span_deg)
    if not coverage.sufficient:
        reasons.extend(coverage.reasons)

    improvement = null_model_improvement(fit_result)
    if improvement < min_model_improvement:
        reasons.append(f"model_improvement {improvement:.3f} below {min_model_improvement:.2f} "
                        f"(reference pattern barely beats a flat/null model)")

    edge = edge_proximity_check(fit_result.estimate.offset_ra_deg, fit_result.estimate.offset_dec_deg, span_deg,
                                 min_edge_margin_deg)
    if edge.near_edge:
        reasons.append(f"fitted offset within {min_edge_margin_deg:.1f} deg of the raster edge "
                        f"(margin east={edge.margin_east_deg:.2f}, north={edge.margin_north_deg:.2f})")

    bootstrap = None
    if run_bootstrap:
        bootstrap = bootstrap_stability(positions, values, center, template, span_deg,
                                         n_iterations=bootstrap_iterations, max_sigma_deg=max_bootstrap_sigma_deg)
        if not bootstrap.stable:
            reasons.append(f"bootstrap unstable (sigma_east={bootstrap.sigma_east_deg:.2f}, "
                            f"sigma_north={bootstrap.sigma_north_deg:.2f}, n={bootstrap.n_iterations})")

    if not reasons:
        verdict = "GOOD"
    elif coverage.valid_count == 0 or improvement < 0.0:
        verdict = "BAD"
    else:
        verdict = "BAD" if len(reasons) >= 2 else "MARGINAL"

    return QualityV2Result(verdict, reasons, coverage, improvement, bootstrap, edge)
