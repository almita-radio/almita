"""ScienceCube assembly (sections 18-21, 42): grids every input point's
spectrum onto a common (velocity, y, x) cube.

Real-data finding (see science_engine/resample.py's own docstring for
the full story): REDUCE guarantees a common FREQUENCY grid within a
campaign, but each point's `velocity_lsrk_m_s` is direction+time
dependent and genuinely differs - up to several channel widths on real
data. SCIENCE V1 therefore resamples each point's spectrum onto ONE
canonical velocity axis (the first accepted point's own axis) before
gridding - an interpolation of an already-Doppler-corrected axis, never
a Doppler recomputation (section 19). A point whose own axis matches the
canonical one within tolerance is passed through untouched (no
resampling when none is needed, section 21).
"""
from __future__ import annotations

import numpy as np

from science_engine.config import ScienceConfig
from science_engine.gridding import GriddingAccumulator, bin_validity, point_passes_quality_policy, \
    spatial_weight_for_point
from science_engine.models import BeamModel, ScienceCube, ScienceGrid, ScienceInput
from science_engine.resample import resample_to_velocity_axis


class IncompatibleVelocityGridError(Exception):
    """Raised when no usable common velocity axis can be established at
    all (e.g. no point carries velocity, or frequency grids differ -
    REDUCE's own campaign-wide config guarantee is violated, which V1
    treats as a hard block, never a silent partial cube)."""


def canonical_velocity_axis(science_input: ScienceInput) -> np.ndarray:
    points_with_velocity = [p for p in science_input.points if p.velocity_lsrk_m_s is not None]
    if not points_with_velocity:
        raise IncompatibleVelocityGridError(
            "no input point carries velocity_lsrk_m_s - velocity-dependent products must BLOCK, never guess"
        )
    reference_freq = points_with_velocity[0].frequency_hz
    reference_n = points_with_velocity[0].velocity_lsrk_m_s.shape[0]
    for p in points_with_velocity[1:]:
        if not np.array_equal(p.frequency_hz, reference_freq):
            raise IncompatibleVelocityGridError(
                f"point {p.point_index}'s frequency_hz differs from point "
                f"{points_with_velocity[0].point_index}'s - REDUCE's per-campaign common-grid guarantee "
                f"is violated; V1 has no resample path for frequency (only for the derived velocity axis)"
            )
        if p.velocity_lsrk_m_s.shape[0] != reference_n:
            raise IncompatibleVelocityGridError(f"point {p.point_index} has a different velocity-bin count")
    return points_with_velocity[0].velocity_lsrk_m_s


def build_cube(science_input: ScienceInput, grid: ScienceGrid, beam: BeamModel, config: ScienceConfig,
              *, velocity_match_rtol: float = 1e-6) -> ScienceCube:
    velocity_axis = canonical_velocity_axis(science_input)
    nv = velocity_axis.shape[0]
    accumulator = GriddingAccumulator(shape=(nv, grid.ny, grid.nx))
    resampled_count = 0

    for point in science_input.points:
        if point.velocity_lsrk_m_s is None:
            continue
        if not point_passes_quality_policy(point.reduce_quality_state, config.quality_policy):
            continue
        spatial_weight = spatial_weight_for_point(grid, point.ra_deg, point.dec_degrees, beam)
        if not np.any(spatial_weight > 0):
            continue  # point is outside the beam cutoff for every pixel of this grid

        if (point.velocity_lsrk_m_s.shape == velocity_axis.shape
                and np.allclose(point.velocity_lsrk_m_s, velocity_axis, rtol=velocity_match_rtol, equal_nan=True)):
            values, uncertainty, mask = point.relative_intensity, point.uncertainty, point.mask
        else:
            resampled = resample_to_velocity_axis(point.velocity_lsrk_m_s, point.relative_intensity,
                                                   point.uncertainty, point.mask, velocity_axis)
            values, uncertainty, mask = resampled.relative_intensity, resampled.uncertainty, resampled.mask
            resampled_count += 1

        valid = bin_validity(mask, uncertainty, config.uncertainty_floor_relative)
        accumulator.add_point(spatial_weight, values, uncertainty, valid)

    value, uncertainty, weight_sum, n_contributing = accumulator.finalize()
    return ScienceCube(grid=grid, velocity_lsrk_m_s=velocity_axis, relative_intensity=value,
                       uncertainty=uncertainty, weight_sum=weight_sum, n_contributing=n_contributing,
                       valid=weight_sum > 0)
