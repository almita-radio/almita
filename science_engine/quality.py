"""SCIENCE session/product quality: a SEPARATE layer from REDUCE's own
per-point quality - never overwritten, never conflated. `reasons` is
never empty (even for GOOD), matching REDUCE's own QualityReport
philosophy.

Two distinct lists, deliberately:

  reasons      why `state` is what it is. Anything here that is WARNING/BAD-level changes the state.
  limitations  true statements about how to READ the product that do NOT downgrade it:
                 PROVISIONAL_BEAM_MODEL  the beam FWHM is operator-provided, not measured - a metadata
                                         limitation of the whole V1 pipeline, not bad data
                 VELOCITY_RESAMPLED      per-point LSRK axes differ (real physics: up to ~84 channels on a real
                                         100-point campaign) and were interpolated onto one axis - expected,
                                         never a defect
                 INPUT_PARTIAL           the REDUCE input session is PARTIAL (see also data_completeness)

State rules (conservative; nothing here can turn bad data into GOOD):
  BAD      no velocity, no used point, no valid cube voxel, or the integrated map has no valid pixel
  WARNING  too few points, low spatial coverage, some input not GOOD, points excluded (policy/ingest),
           UNCALIBRATED inputs, INPUT_PARTIAL, integration window partly outside the cube,
           low spectral coverage in the integrated map
  GOOD     otherwise
"""
from __future__ import annotations

import numpy as np

from science_engine.config import ScienceConfig
from science_engine.models import ScienceCube, ScienceInput, ScienceQualityReport, ScienceQualityState, SpatialMap


# relative_intensity is a dimensionless FRACTIONAL EXCESS: its natural scale is 1.0 (= 100% of the reference level).
# A median per-voxel sigma at or above that scale means the noise is at least as large as the whole reference level.
# (Real REDUCE Level 1 medians are ~0.03-0.04.) This is a physical scale of the unit, not a tuned threshold.
EXTREME_RELATIVE_UNCERTAINTY = 1.0


def assess_science_quality(science_input: ScienceInput, cube: ScienceCube, config: ScienceConfig,
                           integrated: SpatialMap | None = None,
                           *, min_points_for_good: int = 3,
                           low_spatial_coverage_threshold: float = 0.3,
                           low_valid_map_fraction_threshold: float = 0.5) -> ScienceQualityReport:
    reasons: list[str] = []
    limitations: list[str] = []
    metrics: dict = {}
    warning = bad = False
    info = cube.build_info or {}

    n_points = len(science_input.points)
    n_used = int(info.get("n_points_used", n_points))
    metrics.update({"n_input_points": n_points, "n_points_used": n_used,
                    "n_points_excluded": int(info.get("n_points_excluded", 0)),
                    "n_points_in_reduce_manifest": science_input.n_points_in_manifest})
    if n_used == 0:
        bad = True
        reasons.append("NO_USED_POINTS: no input point survived the quality policy / beam support / ingest checks")
    elif n_used < min_points_for_good:
        warning = True
        reasons.append(f"INSUFFICIENT_POINTS: only {n_used} used points (< {min_points_for_good})")

    excluded = info.get("excluded", [])
    if excluded:
        warning = True
        reasons.append(f"POINTS_EXCLUDED: {len(excluded)} REDUCE point(s) not used (policy, ingest or beam support) - "
                       f"see manifest resample/qc and provenance.input_points for each reason")

    spatial_coverage_fraction = float(np.mean(np.any(cube.valid, axis=0)))
    metrics["spatial_coverage_fraction"] = spatial_coverage_fraction
    metrics["valid_voxel_fraction"] = float(np.mean(cube.valid))
    if spatial_coverage_fraction < low_spatial_coverage_threshold:
        warning = True
        reasons.append(f"LOW_SPATIAL_COVERAGE: {spatial_coverage_fraction:.3f} of the grid has any valid data")

    not_good = sum(1 for p in science_input.points if p.reduce_quality_state != "GOOD")
    metrics["input_points_not_good"] = not_good
    metrics["input_quality_counts"] = {state: sum(1 for p in science_input.points if p.reduce_quality_state == state)
                                       for state in ("GOOD", "WARNING", "BAD", "UNKNOWN")}
    if not_good > 0:
        warning = True
        reasons.append(f"INPUT_WARNINGS: {not_good}/{n_points} input points are not REDUCE-GOOD")

    velocity_available = all(p.velocity_lsrk_m_s is not None for p in science_input.points)
    metrics["velocity_available"] = velocity_available
    if not velocity_available:
        bad = True
        reasons.append("NO_VELOCITY: at least one input point has no velocity_lsrk_m_s")

    calibration_levels = {p.calibration_level for p in science_input.points}
    metrics["calibration_levels"] = sorted(calibration_levels)
    if "UNCALIBRATED" in calibration_levels:
        warning = True
        reasons.append("UNCALIBRATED: some input points are UNCALIBRATED - relative_intensity scale may not be "
                       "comparable across points")

    if science_input.reduce_session_status != "COMPLETED":
        warning = True
        reasons.append(f"INPUT_PARTIAL: REDUCE session status is {science_input.reduce_session_status}")
        limitations.append("INPUT_PARTIAL")

    if not np.any(cube.valid):
        bad = True
        reasons.append("NO_VALID_VOXEL: no cube voxel has any valid contribution")

    if integrated is not None:
        valid_map_fraction = float(np.mean(integrated.valid))
        supported = np.any(cube.valid, axis=0)
        supported_fraction = float(np.mean(supported))
        metrics["valid_map_fraction"] = valid_map_fraction
        metrics["window_status"] = integrated.metadata.get("window_status")
        metrics["window_covered_by_cube_fraction"] = integrated.metadata.get("window_covered_by_cube_fraction")
        if supported_fraction > 0 and integrated.spectral_coverage is not None:
            metrics["median_spectral_coverage_fraction"] = float(np.median(integrated.spectral_coverage[supported]))
        if not np.any(integrated.valid):
            bad = True
            reasons.append("NO_VALID_MAP_PIXEL: the integrated map has no valid pixel (window blocked or "
                           "spectral coverage below min_spectral_coverage_fraction everywhere)")
        elif supported_fraction > 0 and valid_map_fraction / supported_fraction < low_valid_map_fraction_threshold:
            warning = True
            reasons.append(f"SPECTRAL_COVERAGE_LOW: only {valid_map_fraction / supported_fraction:.2f} of supported "
                           f"pixels reach min_spectral_coverage_fraction={config.min_spectral_coverage_fraction}")
        if integrated.metadata.get("window_status") == "PARTIAL_OUTSIDE_CUBE":
            warning = True
            reasons.append(f"INTEGRATION_WINDOW_PARTIAL: only "
                           f"{integrated.metadata['window_covered_by_cube_fraction']:.2f} of the requested velocity "
                           f"window lies inside the cube")
    if np.any(np.isfinite(cube.uncertainty)):
        median_unc = float(np.nanmedian(cube.uncertainty))
        metrics["median_uncertainty"] = median_unc
        if median_unc >= EXTREME_RELATIVE_UNCERTAINTY:
            bad = True
            reasons.append(f"UNCERTAINTY_EXTREME: median per-voxel sigma {median_unc:.3g} >= {EXTREME_RELATIVE_UNCERTAINTY} "
                           f"(noise at least 100% of the reference level of a fractional-excess quantity - the data "
                           f"carry no usable information)")

    if config.beam_status != "MEASURED":
        limitations.append("PROVISIONAL_BEAM_MODEL")
    if info.get("resample", {}).get("n_points_resampled", 0) > 0:
        limitations.append("VELOCITY_RESAMPLED")

    if bad:
        state = ScienceQualityState.BAD
    elif warning:
        state = ScienceQualityState.WARNING
    else:
        state = ScienceQualityState.GOOD
        reasons.append(f"n_points_used={n_used}, spatial_coverage_fraction={spatial_coverage_fraction:.3f}, "
                       f"all input points GOOD, velocity available")

    return ScienceQualityReport(state=state, reasons=reasons, metrics=metrics, limitations=limitations)
