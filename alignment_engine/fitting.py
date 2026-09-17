"""Fitting (Fase 3/6/19): wraps alignment.py's proven search fitter, then
adds a sub-grid continuous refinement on top (2nd pass, pre-hardware).

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

SUB-GRID REFINEMENT (2nd pass root-cause investigation): measured directly
(see the pre-hardware technical report; reproducible with
diag_solar-style scripts using this module's own functions), NOT assumed:
  - Noiseless recovery of an injected offset is already correct to
    floating-point precision (~1e-16 deg) BEFORE any refinement - i.e. the
    grid resolution, Gaussian template, coordinate frames (ICRS tangent
    plane) and east/north sign convention are all already correct. The
    residual error users saw (e.g. 1.2/-0.7 injected -> 0.95/-0.45
    recovered) is a NOISE-DRIVEN effect, not a geometry or quantization
    bug.
  - With realistic beam widths (FWHM ~20deg) sampled over the fine stage's
    narrow window (~5.5deg span), the *correlation* score alignment.py's
    grid search maximizes is extremely flat near the true peak (measured:
    only ~0.01 of dynamic range over +/-0.3deg), while the least-squares
    *residual* of the same profiled fit is far more discriminating over
    the same range (measured: ~0.0 -> 0.14). estimate_template_offset's
    own tie-break (`math.isclose(score, best_score)`) essentially never
    triggers at that resolution, so the grid argmax is effectively chosen
    on the noisiest, least informative axis of the objective.
  - refine_subgrid_offset() below fixes this by running a small numeric
    Gauss-Newton/Levenberg-Marquardt search that minimizes the
    least-squares RESIDUAL directly (the more sensitive quantity),
    starting from the grid search's own answer, using the exact same
    sample-shift convention estimate_template_offset uses (shift SAMPLE
    coordinates by the candidate offset and evaluate the fixed template
    at `center` - never rebuilding/re-centering the template itself, so
    this is cheap even for the HI LocalSphericalTemplate). It never has
    access to the injected/true offset - only to `positions`/`values`,
    exactly like the grid search it refines.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np
from astropy.coordinates import SkyCoord

from alignment import AlignmentEstimate, estimate_template_offset, offset_coordinates


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
class SubgridRefinement:
    """Diagnostic record of the Gauss-Newton refinement pass - kept
    separate from `estimate` so the grid-search answer stays inspectable
    (Fase 2's own "no arregles el resultado haciendo trampa": this is
    reported, not hidden, whether or not it changed anything)."""
    applied: bool
    converged: bool
    iterations: int
    grid_offset_east_deg: float
    grid_offset_north_deg: float
    refined_offset_east_deg: float
    refined_offset_north_deg: float
    delta_from_grid_deg: float
    uncertainty_east_deg: Optional[float]
    uncertainty_north_deg: Optional[float]
    reason: str = ""


@dataclass
class FitResult:
    estimate: AlignmentEstimate
    quality: FitQuality
    raw_values: List[Optional[float]]
    valid_count: int
    total_count: int
    rejected_indices: List[int]
    grid_estimate: Optional[AlignmentEstimate] = None
    grid_ranking: str = "correlation"
    refinement: Optional[SubgridRefinement] = None

    def to_dict(self) -> dict:
        payload = {
            "estimate": asdict(self.estimate),
            "quality": asdict(self.quality),
            "valid_count": self.valid_count,
            "total_count": self.total_count,
            "rejected_indices": self.rejected_indices,
            "grid_ranking": self.grid_ranking,
        }
        if self.grid_estimate is not None:
            payload["grid_estimate"] = asdict(self.grid_estimate)
        if self.refinement is not None:
            payload["refinement"] = asdict(self.refinement)
        return payload


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


def _profiled_fit(east: float, north: float, sample_east: np.ndarray, sample_north: np.ndarray,
                   values: np.ndarray, center: SkyCoord, template: Callable
                   ) -> Tuple[np.ndarray, np.ndarray]:
    """For a candidate (east, north) offset: shift the SAMPLE coordinates
    (never the template/center - identical convention to
    estimate_template_offset, so this is a drop-in refinement of the same
    objective, not a different model), evaluate the template, then profile
    out the linear scale+baseline via lstsq exactly like alignment._match.
    Returns (residuals, expected) - residuals is what the optimizer drives
    to zero."""
    coordinates = offset_coordinates(center, sample_east + east, sample_north + north)
    expected = np.asarray(template(coordinates), dtype=float)
    design = np.column_stack((expected, np.ones(len(expected))))
    coeffs, *_ = np.linalg.lstsq(design, values, rcond=None)
    residuals = values - design @ coeffs
    return residuals, expected


def search_offset_residual_primary(positions: SkyCoord, values: np.ndarray, center: SkyCoord,
                                    template: Callable,
                                    search_levels=((5.5, .5), (1.0, .1), (.25, .025))
                                    ) -> Tuple[float, float, float]:
    """Same coarse-to-fine grid search as alignment.estimate_template_offset
    (same batched-transform strategy for performance, same candidate grid,
    same sample-shift convention) but ranks candidates by minimizing the
    least-squares RESIDUAL of the profiled fit, not by maximizing
    correlation. Justification: measured (module docstring above) that for
    a broad beam sampled over a narrow window, correlation is nearly flat
    near the true peak while the residual is far more discriminating, and
    estimate_template_offset's own tie-break on `math.isclose(score, ...)`
    essentially never engages at that flatness - so under realistic noise
    its argmax is effectively noise, not signal. This function only
    changes which of the *same* candidates wins; it introduces no new
    physical model.

    MEASURED RESULT (not adopted as the default - see `ranking` param on
    fit_raster): a naive residual-minimizing search without the
    `np.std(expected) < 1e-12` guard below diverges catastrophically
    (measured errors up to ~73deg) because a near-constant `expected`
    makes the 2-parameter [gain, baseline] lstsq nearly rank-deficient,
    letting an inflated gain drive the residual to ~0 by fitting noise
    rather than signal - residual alone is not scale-invariant the way
    correlation is, so it is vulnerable to exactly this failure mode away
    from the true peak. With the guard in place this function is stable,
    but head-to-head statistical testing (30-seed Monte Carlo, matched
    conditions) against alignment.py's own correlation-primary search
    showed no significant precision improvement under realistic (2%)
    noise - both are within one another's sampling noise. Kept available
    (not deleted) because it is a real, tested, evidence-backed
    alternative and the degeneracy guard it demonstrates is worth keeping
    documented; not the default because it doubles search cost for no
    measured benefit in this regime. Returns (east_deg, north_deg,
    best_cost)."""
    local = positions.transform_to(_skyoffset_frame(center))
    sample_east, sample_north = local.lon.deg, local.lat.deg
    best_east = best_north = 0.0
    best_cost = float("inf")
    ones = np.ones(len(values))
    for radius, step in search_levels:
        origin_east, origin_north = best_east, best_north
        axis = np.arange(-radius, radius + step / 4, step)
        east_grid, north_grid = np.meshgrid(origin_east + axis, origin_north + axis)
        candidate_east, candidate_north = east_grid.ravel(), north_grid.ravel()
        all_east = (candidate_east[:, None] + sample_east[None, :]).ravel()
        all_north = (candidate_north[:, None] + sample_north[None, :]).ravel()
        coordinates = offset_coordinates(center, all_east, all_north)
        expected_grid = np.asarray(template(coordinates)).reshape(len(candidate_east), len(values))
        for index, expected in enumerate(expected_grid):
            if np.std(expected) < 1e-12:
                continue
            design = np.column_stack((expected, ones))
            coeffs, *_ = np.linalg.lstsq(design, values, rcond=None)
            cost = float(np.sum((values - design @ coeffs) ** 2))
            if cost < best_cost:
                best_cost = cost
                best_east, best_north = float(candidate_east[index]), float(candidate_north[index])
    return best_east, best_north, best_cost


def refine_subgrid_offset(positions: SkyCoord, values: np.ndarray, center: SkyCoord,
                           template: Callable, initial_east_deg: float, initial_north_deg: float,
                           bound_deg: float, max_iter: int = 40, fd_step_deg: float = 1e-3,
                           cost_tol: float = 1e-12) -> SubgridRefinement:
    """Levenberg-Marquardt refinement of (east, north) minimizing the
    least-squares residual of the profiled fit (see module docstring for
    why residual, not correlation, is the right quantity to minimize here).
    Pure numpy (no scipy in this environment) - 2 parameters, numeric
    (central-difference) Jacobian, standard LM damping. Never sees the
    injected/true offset - only `positions`/`values`, like the grid search
    it starts from.
    """
    local = positions.transform_to(_skyoffset_frame(center))
    sample_east, sample_north = local.lon.deg, local.lat.deg

    east, north = float(initial_east_deg), float(initial_north_deg)
    lam = 1e-3
    iterations = 0
    converged = False
    reason = ""

    def cost_at(e, n):
        r, _ = _profiled_fit(e, n, sample_east, sample_north, values, center, template)
        return r, float(np.sum(r ** 2))

    try:
        residual0, cost0 = cost_at(east, north)
    except Exception as exc:  # template/coordinate failure - never crash the caller
        return SubgridRefinement(False, False, 0, initial_east_deg, initial_north_deg,
                                  initial_east_deg, initial_north_deg, 0.0, None, None,
                                  reason=f"refinement not attempted: {exc}")

    h = fd_step_deg
    for iterations in range(1, max_iter + 1):
        try:
            r_ep, _ = cost_at(east + h, north)
            r_em, _ = cost_at(east - h, north)
            r_np, _ = cost_at(east, north + h)
            r_nm, _ = cost_at(east, north - h)
        except Exception as exc:
            reason = f"jacobian evaluation failed: {exc}"
            break
        jacobian = np.column_stack(((r_ep - r_em) / (2 * h), (r_np - r_nm) / (2 * h)))
        jtj = jacobian.T @ jacobian
        jtr = jacobian.T @ residual0
        improved = False
        for _lm_try in range(15):
            damped = jtj + lam * np.diag(np.diag(jtj) + 1e-12)
            try:
                delta = np.linalg.solve(damped, -jtr)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            trial_east, trial_north = east + float(delta[0]), north + float(delta[1])
            if abs(trial_east) > bound_deg or abs(trial_north) > bound_deg:
                lam *= 10
                continue
            try:
                trial_residual, trial_cost = cost_at(trial_east, trial_north)
            except Exception:
                lam *= 10
                continue
            if trial_cost < cost0:
                improved = True
                step_size = math.hypot(float(delta[0]), float(delta[1]))
                east, north = trial_east, trial_north
                residual0, cost0_new = trial_residual, trial_cost
                lam = max(lam / 10, 1e-8)
                if abs(cost0 - cost0_new) < cost_tol * max(cost0, 1e-12) or step_size < 1e-6:
                    cost0 = cost0_new
                    converged = True
                cost0 = cost0_new
                break
            lam *= 10
        if not improved:
            converged = True
            reason = reason or "no improving step found (converged or stuck)"
            break
        if converged:
            reason = reason or "converged (cost/step tolerance reached)"
            break
    else:
        reason = reason or "max iterations reached without full convergence"

    uncertainty_east = uncertainty_north = None
    try:
        r_ep, _ = cost_at(east + h, north)
        r_em, _ = cost_at(east - h, north)
        r_np, _ = cost_at(east, north + h)
        r_nm, _ = cost_at(east, north - h)
        jacobian = np.column_stack(((r_ep - r_em) / (2 * h), (r_np - r_nm) / (2 * h)))
        dof = max(len(residual0) - 2, 1)
        sigma2 = cost0 / dof
        covariance = sigma2 * np.linalg.pinv(jacobian.T @ jacobian)
        uncertainty_east = float(np.sqrt(max(covariance[0, 0], 0.0)))
        uncertainty_north = float(np.sqrt(max(covariance[1, 1], 0.0)))
    except Exception:
        pass  # uncertainty is best-effort diagnostics, never blocks the refined estimate

    delta_from_grid = float(math.hypot(east - initial_east_deg, north - initial_north_deg))
    return SubgridRefinement(
        applied=True, converged=converged, iterations=iterations,
        grid_offset_east_deg=initial_east_deg, grid_offset_north_deg=initial_north_deg,
        refined_offset_east_deg=east, refined_offset_north_deg=north,
        delta_from_grid_deg=delta_from_grid,
        uncertainty_east_deg=uncertainty_east, uncertainty_north_deg=uncertainty_north,
        reason=reason,
    )


def fit_raster(positions: SkyCoord, values: List[Optional[float]], center: SkyCoord,
               template: Callable, span_deg: float,
               search_levels=((5.5, .5), (1.0, .1), (.25, .025)),
               refine: bool = True, ranking: str = "correlation") -> FitResult:
    """positions/values are RAW, one entry per attempted point (values[i] is
    None for a missing/rejected sample) - never mutated or overwritten.
    Only the valid subset is handed to the search fitter.

    Pipeline (Fase 1, 2nd pass): (1) a coarse-to-fine grid search over the
    candidate offsets - `ranking="correlation"` (default) uses
    alignment.estimate_template_offset unchanged (the proven, already
    field-validated method); `ranking="residual"` uses this module's own
    search_offset_residual_primary instead (see its docstring for the
    root-cause investigation behind it, and why it is not the default -
    measured no significant precision improvement, at 2x the search cost).
    Either way the result becomes `grid_estimate`. (2) refine_subgrid_offset()
    polishes that to a continuous, sub-grid answer via Gauss-Newton/LM on
    the least-squares residual, becoming `estimate` (what SYNC planning
    uses) - unless it fails to apply, in which case `grid_estimate` is used
    unchanged. `refinement` records what step (2) did (or why it didn't),
    always."""
    if ranking not in ("correlation", "residual"):
        raise ValueError(f"unknown ranking strategy: {ranking!r}")
    valid_mask = [v is not None for v in values]
    rejected_indices = [i for i, ok in enumerate(valid_mask) if not ok]
    valid_positions = positions[np.asarray(valid_mask)]
    valid_values = np.asarray([v for v in values if v is not None], dtype=float)

    refinement: Optional[SubgridRefinement] = None
    grid_estimate: Optional[AlignmentEstimate] = None
    if len(valid_values) < 4:
        estimate = AlignmentEstimate(0.0, 0.0, 0.0, 0.0, float("inf"), -1.0, len(valid_values))
    else:
        if ranking == "correlation":
            grid_estimate = estimate_template_offset(
                valid_positions, valid_values, center, template, search_levels=search_levels)
        else:
            seed_east, seed_north, _seed_cost = search_offset_residual_primary(
                valid_positions, valid_values, center, template, search_levels=search_levels)
            grid_estimate = _estimate_at(valid_positions, valid_values, center, template,
                                          seed_east, seed_north)
        estimate = grid_estimate
        if refine:
            outer_radius = search_levels[0][0] if search_levels else 5.5
            bound_deg = 1.5 * outer_radius + 1.0
            refinement = refine_subgrid_offset(
                valid_positions, valid_values, center, template,
                grid_estimate.offset_ra_deg, grid_estimate.offset_dec_deg, bound_deg=bound_deg,
            )
            if refinement.applied:
                estimate = _estimate_at(valid_positions, valid_values, center, template,
                                         refinement.refined_offset_east_deg,
                                         refinement.refined_offset_north_deg)

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
                      rejected_indices=rejected_indices, grid_estimate=grid_estimate,
                      grid_ranking=ranking, refinement=refinement)


def _skyoffset_frame(center: SkyCoord):
    from astropy.coordinates import SkyOffsetFrame
    return SkyOffsetFrame(origin=center)


def _estimate_at(positions: SkyCoord, values: np.ndarray, center: SkyCoord, template: Callable,
                  east_deg: float, north_deg: float) -> AlignmentEstimate:
    """Build an AlignmentEstimate at an arbitrary (not necessarily grid-
    aligned) candidate offset, using the identical score/residual/
    confidence formulas alignment.estimate_template_offset uses at its own
    winning candidate - so estimates from different search strategies stay
    numerically comparable."""
    local = positions.transform_to(_skyoffset_frame(center))
    sample_east, sample_north = local.lon.deg, local.lat.deg
    residuals, expected = _profiled_fit(east_deg, north_deg, sample_east, sample_north,
                                         values, center, template)
    fit = values - residuals
    if np.std(values) < 1e-12 or np.std(expected) < 1e-12:
        score, residual_norm = -1.0, float("inf")
    else:
        score = float(np.corrcoef(values, fit)[0, 1])
        residual_norm = float(np.sqrt(np.mean(residuals ** 2)) / np.std(values))
    confidence = float(np.clip((score + 1) / 2 * math.exp(-.35 * residual_norm), 0, 1))
    corrected = offset_coordinates(center, [east_deg], [north_deg])[0]
    return AlignmentEstimate(east_deg, north_deg, float(center.separation(corrected).deg),
                              confidence, residual_norm, score, len(values))
