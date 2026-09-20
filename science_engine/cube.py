"""ScienceCube assembly (sections 18-21, 42): grids every input point's
spectrum onto a common (velocity, y, x) cube.

Real-data finding (see science_engine/resample.py's own docstring for
the full story): REDUCE guarantees a common FREQUENCY grid within a
campaign, but each point's `velocity_lsrk_m_s` is direction+time
dependent and genuinely differs - up to several channel widths on real
data. SCIENCE V1 therefore resamples each point's spectrum onto ONE
canonical ASCENDING velocity axis (the lowest-index point's own axis) before
gridding - an interpolation of an already-Doppler-corrected axis, never
a Doppler recomputation (section 19). A point whose own axis matches the
canonical one within tolerance is passed through untouched (no
resampling when none is needed, section 21).
"""
from __future__ import annotations

import hashlib
import time

import numpy as np

from science_engine.config import ScienceConfig
from reduce_engine.models import MaskFlag
from science_engine.gridding import GriddingAccumulator, bin_validity, point_passes_quality_policy, \
    sigma_suspect_bins, spatial_weight_for_point
from science_engine.models import BeamModel, ScienceCube, ScienceGrid, ScienceInput
from science_engine.resample import resample_to_velocity_axis


class IncompatibleVelocityGridError(Exception):
    """Raised when no usable common velocity axis can be established at
    all (e.g. no point carries velocity, or frequency grids differ -
    REDUCE's own campaign-wide config guarantee is violated, which V1
    treats as a hard block, never a silent partial cube)."""


def _ascending(velocity: np.ndarray, *arrays: np.ndarray) -> tuple[np.ndarray, ...]:
    """Return (velocity, *arrays) in strictly increasing velocity order. A REDUCE Level 1 axis is
    DESCENDING (frequency increases -> radio velocity decreases); the SCIENCE cube axis is always
    ascending. Reversing is a lossless permutation - never an interpolation."""
    if velocity[0] > velocity[-1]:
        return (velocity[::-1], *[a[::-1] for a in arrays])
    return (velocity, *arrays)


def canonical_velocity_axis(science_input: ScienceInput) -> np.ndarray:
    """The cube's velocity axis: the ASCENDING axis of the reference point = the point with the lowest
    `point_index` that carries velocity_lsrk_m_s (ingest sorts points by point_index, so this never depends
    on manifest/filesystem order). Every other point is interpolated onto this axis; a point's bins that
    fall outside the reference axis' range are lost (reported in build_info) and canonical bins outside a
    point's own range are MISSING for that point (reported per voxel via n_pointings/weight_sum).

    Rationale (vs intersection / union / median): a common frequency grid is guaranteed by REDUCE, so all
    axes are the SAME grid shifted by a per-point LSRK offset (measured up to ~84 channels = 5.2 km/s on a
    real 100-point campaign) - the reference choice only decides which ~1% of edge channels lose support,
    far from any HI signal window. It is deterministic and never inflates the cube beyond one real axis.
    """
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
    return _ascending(points_with_velocity[0].velocity_lsrk_m_s)[0].copy()


def build_cube(science_input: ScienceInput, grid: ScienceGrid, beam: BeamModel, config: ScienceConfig,
              *, velocity_match_tol_channels: float = 1e-3) -> ScienceCube:
    """`velocity_match_tol_channels`: a point whose axis equals the canonical one to within this fraction
    of a channel is passed through untouched (no interpolation when none is needed)."""
    velocity_axis = canonical_velocity_axis(science_input)
    nv = velocity_axis.shape[0]
    channel_width = float(np.median(np.abs(np.diff(velocity_axis))))
    accumulator = GriddingAccumulator(shape=(nv, grid.ny, grid.nx))

    used: list[int] = []
    excluded: list[dict] = list(science_input.exclusions)
    offsets_m_s: list[float] = []
    edge_bins_lost: list[int] = []
    masked_before: list[float] = []
    good_after: list[float] = []
    resampled_indices: list[int] = []
    suspect_cache: dict = {}        # REDUCE's sigma profile is identical across points -> compute the suspects once
    n_suspect: list[int] = []
    timings = {"spatial_weight": 0.0, "resample": 0.0, "accumulate": 0.0, "finalize": 0.0}
    reference_point_index = next(p.point_index for p in science_input.points if p.velocity_lsrk_m_s is not None)

    for point in science_input.points:      # ingest guarantees ascending point_index -> deterministic summation order
        if point.velocity_lsrk_m_s is None:
            excluded.append({"point_index": point.point_index, "reason": "NO_VELOCITY"})
            continue
        if not point_passes_quality_policy(point.reduce_quality_state, config.quality_policy):
            excluded.append({"point_index": point.point_index,
                             "reason": f"QUALITY_POLICY_{config.quality_policy}_EXCLUDES_{point.reduce_quality_state}"})
            continue
        t = time.perf_counter()
        spatial_weight = spatial_weight_for_point(grid, point.ra_deg, point.dec_degrees, beam)
        timings["spatial_weight"] += time.perf_counter() - t
        if not np.any(spatial_weight > 0):
            excluded.append({"point_index": point.point_index, "reason": "OUTSIDE_BEAM_SUPPORT_OF_GRID"})
            continue

        v_pt, values_pt, unc_pt, mask_pt = _ascending(point.velocity_lsrk_m_s, point.relative_intensity,
                                                       point.uncertainty, point.mask)
        # sigma consistency BEFORE resampling: an implausibly small isolated sigma must not be interpolated into
        # neighbours as if it were real; the flagged bins become INVALID (counted), their neighbours' targets MISSING
        key = hashlib.blake2b(unc_pt.tobytes() + np.asarray(mask_pt).tobytes(), digest_size=16).digest()
        if key not in suspect_cache:
            suspect_cache[key] = sigma_suspect_bins(mask_pt, unc_pt, config.sigma_local_floor_fraction,
                                                    config.sigma_local_window_channels)
        suspect = suspect_cache[key]
        n_suspect.append(int(suspect.sum()))
        if suspect.any():
            mask_pt = np.where(suspect, MaskFlag.INVALID.value, mask_pt)
        offset = v_pt - velocity_axis
        max_offset = float(np.max(np.abs(offset)))
        if max_offset <= velocity_match_tol_channels * channel_width:
            values, uncertainty, mask = values_pt, unc_pt, mask_pt
        else:
            t = time.perf_counter()
            resampled = resample_to_velocity_axis(v_pt, values_pt, unc_pt, mask_pt, velocity_axis)
            timings["resample"] += time.perf_counter() - t
            values, uncertainty, mask = resampled.relative_intensity, resampled.uncertainty, resampled.mask
            resampled_indices.append(point.point_index)
        offsets_m_s.append(float(np.median(offset)))
        edge_bins_lost.append(int(np.sum((velocity_axis < v_pt[0]) | (velocity_axis > v_pt[-1]))))
        masked_before.append(float(np.mean(mask_pt != 0)))

        valid = bin_validity(mask, uncertainty, config.uncertainty_floor_relative, values)
        good_after.append(float(np.mean(valid)))
        t = time.perf_counter()
        accumulator.add_point(spatial_weight, values, uncertainty, valid)
        timings["accumulate"] += time.perf_counter() - t
        used.append(point.point_index)

    t = time.perf_counter()
    value, uncertainty, weight_sum, n_pointings = accumulator.finalize()
    timings["finalize"] += time.perf_counter() - t
    abs_offsets = np.abs(np.array(offsets_m_s)) if offsets_m_s else np.array([0.0])
    build_info = {
        "canonical_velocity_axis": {"order": "ascending", "reference_point_index": int(reference_point_index),
                                    "n_channels": int(nv), "channel_width_m_s": channel_width,
                                    "min_m_s": float(velocity_axis[0]), "max_m_s": float(velocity_axis[-1])},
        "n_points_input": len(science_input.points), "n_points_used": len(used), "used_point_indices": used,
        "n_points_excluded": len(excluded), "excluded": excluded,
        "resample": {
            "n_points_resampled": len(resampled_indices), "n_points_passthrough": len(used) - len(resampled_indices),
            "fraction_requiring_interpolation": (len(resampled_indices) / len(used)) if used else 0.0,
            "max_abs_offset_m_s": float(abs_offsets.max()), "median_abs_offset_m_s": float(np.median(abs_offsets)),
            "max_abs_offset_channels": float(abs_offsets.max() / channel_width),
            "pairwise_spread_m_s": float(np.ptp(offsets_m_s)) if offsets_m_s else 0.0,
            "pairwise_spread_channels": float(np.ptp(offsets_m_s) / channel_width) if offsets_m_s else 0.0,
            "edge_bins_lost_max": max(edge_bins_lost) if edge_bins_lost else 0,
            "edge_bins_lost_median": float(np.median(edge_bins_lost)) if edge_bins_lost else 0.0,
            "mean_masked_fraction_input": float(np.mean(masked_before)) if masked_before else 0.0,
            "mean_valid_fraction_after_resample": float(np.mean(good_after)) if good_after else 0.0,
            "sigma_convention": "linear interpolation of per-bin sigma (see resample.py)",
        },
        "n_nonfinite_rejected": int(accumulator.n_nonfinite_rejected),
        "sigma_local_check": {"fraction": config.sigma_local_floor_fraction, "window_channels": config.sigma_local_window_channels,
                              "mean_bins_excluded_per_point": float(np.mean(n_suspect)) if n_suspect else 0.0,
                              "max_bins_excluded_per_point": max(n_suspect) if n_suspect else 0},
        "timings_s": timings,
    }
    return ScienceCube(grid=grid, velocity_lsrk_m_s=velocity_axis, relative_intensity=value,
                       uncertainty=uncertainty, weight_sum=weight_sum, n_pointings=n_pointings,
                       valid=weight_sum > 0, build_info=build_info)
