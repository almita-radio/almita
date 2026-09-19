"""Moment-like products (sections 43-46). Deliberately named
"*-like": these are measurement products, not a claim of a
professionally-reduced moment map. Only computed where signal/coverage/
uncertainty criteria are met - a centroid or dispersion computed on pure
noise is worse than useless (sections 44-45), so it is masked invalid,
never a silent NaN without a reason (section 45).
"""
from __future__ import annotations

import numpy as np

from science_engine.config import ScienceConfig
from science_engine.integration import integrated_map
from science_engine.models import ScienceCube, SpatialMap


def moment1_like(cube: ScienceCube, config: ScienceConfig) -> SpatialMap:
    """Intensity-weighted velocity centroid, computed only where
    integrated signal significance (section 46) clears
    `config.moment_min_snr` - noise has no meaningful centroid
    (section 44)."""
    moment0 = integrated_map(cube, config)
    snr = np.divide(moment0.value, moment0.uncertainty, out=np.full_like(moment0.value, np.nan),
                    where=(moment0.uncertainty > 0) & np.isfinite(moment0.uncertainty))
    signal_ok = np.isfinite(snr) & (snr >= config.moment_min_snr) & moment0.valid

    velocity = cube.velocity_lsrk_m_s
    in_window = (velocity >= config.velocity_window_min_m_s) & (velocity <= config.velocity_window_max_m_s)
    windowed_value = np.where(cube.valid[in_window], cube.relative_intensity[in_window], 0.0)
    windowed_velocity = velocity[in_window][:, None, None]

    positive_value = np.clip(windowed_value, 0.0, None)  # section 44: negative-noise excursions must not pull the centroid
    weight_sum = np.sum(positive_value, axis=0)
    centroid = np.divide(np.sum(positive_value * windowed_velocity, axis=0), weight_sum,
                         out=np.full(weight_sum.shape, np.nan), where=weight_sum > 0)

    valid = signal_ok & (weight_sum > 0)
    return SpatialMap(grid=cube.grid, kind="moment1_like_velocity_centroid",
                      value=np.where(valid, centroid, np.nan), uncertainty=None,
                      weight_sum=moment0.weight_sum, n_contributing=moment0.n_contributing, valid=valid,
                      units="m/s", metadata={"moment_min_snr": config.moment_min_snr,
                                             "reason_if_invalid": "SNR below moment_min_snr or no positive signal"})


def moment2_like(cube: ScienceCube, config: ScienceConfig) -> SpatialMap:
    """Intensity-weighted velocity dispersion around the moment-1-like
    centroid - invalid wherever moment-1 itself is invalid, or coverage
    is too thin to support a second moment (section 45: never a silent
    NaN, always with a reason in `metadata`)."""
    moment1 = moment1_like(cube, config)
    velocity = cube.velocity_lsrk_m_s
    in_window = (velocity >= config.velocity_window_min_m_s) & (velocity <= config.velocity_window_max_m_s)
    n_window_channels = int(np.sum(in_window))
    windowed_value = np.where(cube.valid[in_window], cube.relative_intensity[in_window], 0.0)
    windowed_velocity = velocity[in_window][:, None, None]

    positive_value = np.clip(windowed_value, 0.0, None)
    weight_sum = np.sum(positive_value, axis=0)
    centroid = np.where(np.isnan(moment1.value), 0.0, moment1.value)[None, :, :]
    variance = np.divide(np.sum(positive_value * (windowed_velocity - centroid) ** 2, axis=0), weight_sum,
                         out=np.full(weight_sum.shape, np.nan), where=weight_sum > 0)

    enough_channels = n_window_channels >= 3    # a dispersion from <3 channels is not meaningful (section 45)
    valid = moment1.valid & (variance >= 0) & np.isfinite(variance) & enough_channels
    dispersion = np.sqrt(np.clip(variance, 0.0, None))
    return SpatialMap(grid=cube.grid, kind="moment2_like_velocity_dispersion",
                      value=np.where(valid, dispersion, np.nan), uncertainty=None,
                      weight_sum=moment1.weight_sum, n_contributing=moment1.n_contributing, valid=valid,
                      units="m/s", metadata={"moment_min_snr": config.moment_min_snr, "n_window_channels": n_window_channels,
                                             "reason_if_invalid": "moment1 invalid, or fewer than 3 window channels"})
