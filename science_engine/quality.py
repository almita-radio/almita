"""SCIENCE session/product quality (sections 106-108) - a SEPARATE layer
from REDUCE's own per-point quality (section 131): never overwritten,
never conflated. `reasons` is never empty, even for GOOD, matching
REDUCE's own QualityReport philosophy.
"""
from __future__ import annotations

import numpy as np

from science_engine.config import ScienceConfig
from science_engine.models import ScienceCube, ScienceInput, ScienceQualityReport, ScienceQualityState


def assess_science_quality(science_input: ScienceInput, cube: ScienceCube, config: ScienceConfig,
                           *, min_points_for_good: int = 3,
                           low_spatial_coverage_threshold: float = 0.3) -> ScienceQualityReport:
    reasons: list[str] = []
    metrics: dict = {}
    warning = bad = False

    n_points = len(science_input.points)
    metrics["n_input_points"] = n_points
    if n_points < min_points_for_good:
        warning = True
        reasons.append(f"INSUFFICIENT_POINTS: only {n_points} input points (< {min_points_for_good})")

    spatial_coverage_fraction = float(np.mean(np.any(cube.valid, axis=0)))
    metrics["spatial_coverage_fraction"] = spatial_coverage_fraction
    if spatial_coverage_fraction < low_spatial_coverage_threshold:
        warning = True
        reasons.append(f"LOW_SPATIAL_COVERAGE: {spatial_coverage_fraction:.3f} of the grid has any valid data")

    n_warnings_or_worse = sum(1 for p in science_input.points if p.reduce_quality_state != "GOOD")
    metrics["input_points_not_good"] = n_warnings_or_worse
    if n_warnings_or_worse > 0:
        warning = True
        reasons.append(f"INPUT_WARNINGS: {n_warnings_or_worse}/{n_points} input points are not REDUCE-GOOD")

    velocity_available = all(p.velocity_lsrk_m_s is not None for p in science_input.points)
    metrics["velocity_available"] = velocity_available
    if not velocity_available:
        bad = True
        reasons.append("NO_VELOCITY: at least one input point has no velocity_lsrk_m_s")

    if config.beam_status != "MEASURED":
        warning = True
        reasons.append(f"BEAM_MODEL_PROVISIONAL: beam status is {config.beam_status!r}, not a measured beam "
                       f"(source: {config.beam_source})")

    calibration_levels = {p.calibration_level for p in science_input.points}
    metrics["calibration_levels"] = sorted(calibration_levels)
    if "UNCALIBRATED" in calibration_levels:
        warning = True
        reasons.append("some input points are UNCALIBRATED - relative_intensity scale may not be comparable "
                       "across points")

    if not np.any(cube.valid):
        bad = True
        reasons.append("no cube voxel has any valid contribution")

    if bad:
        state = ScienceQualityState.BAD
    elif warning:
        state = ScienceQualityState.WARNING
    else:
        state = ScienceQualityState.GOOD
        reasons.append(f"n_points={n_points}, spatial_coverage_fraction={spatial_coverage_fraction:.3f}, "
                       f"all input points GOOD, velocity available")

    return ScienceQualityReport(state=state, reasons=reasons, metrics=metrics)
