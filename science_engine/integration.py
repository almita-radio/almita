"""Integrated relative intensity map (sections 34-40): the primary
visual SCIENCE V1 product. Integrates Sum(I(v) * dv) over an EXPLICIT
velocity window (section 35-36: no auto-window in V1) using the cube's
own per-voxel weighting - never a plain sum(y) (section 38: bin width
matters).
"""
from __future__ import annotations

import numpy as np

from science_engine.config import ScienceConfig
from science_engine.models import ScienceCube, SpatialMap


def integrated_map(cube: ScienceCube, config: ScienceConfig) -> SpatialMap:
    velocity = cube.velocity_lsrk_m_s
    in_window = (velocity >= config.velocity_window_min_m_s) & (velocity <= config.velocity_window_max_m_s)
    if not np.any(in_window):
        raise ValueError(
            f"velocity window [{config.velocity_window_min_m_s}, {config.velocity_window_max_m_s}] m/s "
            f"selects zero channels - cube covers [{velocity.min():.0f}, {velocity.max():.0f}] m/s"
        )

    # channel width via the SORTED axis (velocity can be increasing or decreasing) - section 38/128
    order = np.argsort(velocity)
    sorted_velocity = velocity[order]
    channel_width_m_s = float(np.median(np.abs(np.diff(sorted_velocity))))

    windowed_valid = cube.valid[in_window]                              # (Nv_win, Ny, Nx)
    windowed_value = cube.relative_intensity[in_window]
    windowed_uncertainty = cube.uncertainty[in_window]
    windowed_weight = cube.weight_sum[in_window]
    windowed_n = cube.n_contributing[in_window]

    n_window_channels = int(np.sum(in_window))
    spectral_coverage_fraction = np.sum(windowed_valid, axis=0) / n_window_channels   # (Ny, Nx)

    # Sum(I*dv) over valid channels only - an invalid (NaN) channel contributes 0 to the sum, its
    # ABSENCE is instead reflected in spectral_coverage_fraction (section 39), never faked as I=0.
    safe_value = np.where(windowed_valid, windowed_value, 0.0)
    value = np.sum(safe_value, axis=0) * channel_width_m_s

    # uncertainty of a sum of independent channel means, each with its own propagated uncertainty
    # (science_engine.gridding's per-voxel uncertainty) - channels are NOT independent in general
    # (adjacent channels share contributing points, section 33), so this is a documented
    # lower-bound/approximation, not an exact propagation; stated explicitly rather than hidden.
    safe_variance = np.where(windowed_valid, windowed_uncertainty ** 2, 0.0)
    uncertainty = np.sqrt(np.sum(safe_variance, axis=0)) * channel_width_m_s

    weight_sum_2d = np.sum(np.where(windowed_valid, windowed_weight, 0.0), axis=0)
    n_contributing_2d = np.max(np.where(windowed_valid, windowed_n, 0), axis=0)

    valid_2d = spectral_coverage_fraction >= config.min_spectral_coverage_fraction
    value = np.where(valid_2d, value, np.nan)          # section 40: below threshold, invalid - never extrapolated
    uncertainty = np.where(valid_2d, uncertainty, np.nan)

    return SpatialMap(
        grid=cube.grid, kind="integrated_relative_intensity", value=value, uncertainty=uncertainty,
        weight_sum=weight_sum_2d, n_contributing=n_contributing_2d, valid=valid_2d,
        units="relative_intensity_dimensionless * m/s",
        metadata={"velocity_window_min_m_s": config.velocity_window_min_m_s,
                 "velocity_window_max_m_s": config.velocity_window_max_m_s,
                 "n_window_channels": n_window_channels, "channel_width_m_s": channel_width_m_s,
                 "min_spectral_coverage_fraction": config.min_spectral_coverage_fraction,
                 "spectral_coverage_fraction": spectral_coverage_fraction.tolist()},
    )


def channel_map(cube: ScienceCube, velocity_center_m_s: float, velocity_width_m_s: float) -> SpatialMap:
    """A single velocity-interval slice map (section 41)."""
    lo, hi = velocity_center_m_s - velocity_width_m_s / 2, velocity_center_m_s + velocity_width_m_s / 2
    in_window = (cube.velocity_lsrk_m_s >= lo) & (cube.velocity_lsrk_m_s <= hi)
    if not np.any(in_window):
        raise ValueError(f"channel window [{lo}, {hi}] m/s selects zero channels")
    windowed_valid = cube.valid[in_window]
    windowed_value = cube.relative_intensity[in_window]
    windowed_uncertainty = cube.uncertainty[in_window]
    n_channels = int(np.sum(in_window))
    coverage = np.sum(windowed_valid, axis=0) / n_channels
    value = np.nanmean(np.where(windowed_valid, windowed_value, np.nan), axis=0)
    uncertainty = np.sqrt(np.nanmean(np.where(windowed_valid, windowed_uncertainty ** 2, np.nan), axis=0) / np.maximum(
        np.sum(windowed_valid, axis=0), 1))
    weight_sum_2d = np.sum(np.where(windowed_valid, cube.weight_sum[in_window], 0.0), axis=0)
    n_contributing_2d = np.max(np.where(windowed_valid, cube.n_contributing[in_window], 0), axis=0)
    valid_2d = coverage > 0
    return SpatialMap(grid=cube.grid, kind="channel_map", value=np.where(valid_2d, value, np.nan),
                      uncertainty=np.where(valid_2d, uncertainty, np.nan), weight_sum=weight_sum_2d,
                      n_contributing=n_contributing_2d, valid=valid_2d,
                      units="relative_intensity_dimensionless",
                      metadata={"velocity_center_m_s": velocity_center_m_s, "velocity_width_m_s": velocity_width_m_s,
                               "n_channels": n_channels})
