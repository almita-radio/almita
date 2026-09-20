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
from science_engine.integration import integrated_map, window_overlaps
from science_engine.models import ScienceCube, SpatialMap


def _signal_weights(cube: ScienceCube, config: ScienceConfig, in_window: np.ndarray) -> np.ndarray:
    """Per-channel SIGNAL GATING shared by both moments. A channel contributes only if it is valid AND its value is at
    least `moment_min_snr` times its own propagated sigma. Clipping only the negatives (the first implementation)
    let the positive half of the noise of the whole velocity window into the sums; measured on the golden fixture
    that biased the centroid of a high-SNR pixel by ~1.3 channels (noise weight 0.36 vs signal 2.28)."""
    value = cube.relative_intensity[in_window]
    sigma = cube.uncertainty[in_window]
    passes = cube.valid[in_window] & np.isfinite(value) & np.isfinite(sigma) & (sigma > 0) & (
        value >= config.moment_min_snr * sigma)
    return np.where(passes, value, 0.0)


def moment1_like(cube: ScienceCube, config: ScienceConfig, moment0: SpatialMap | None = None) -> SpatialMap:
    """Intensity-weighted velocity centroid, computed only where
    integrated signal significance (section 46) clears
    `config.moment_min_snr` - noise has no meaningful centroid
    (section 44)."""
    moment0 = moment0 if moment0 is not None else integrated_map(cube, config)
    snr = np.divide(moment0.value, moment0.uncertainty, out=np.full_like(moment0.value, np.nan),
                    where=(moment0.uncertainty > 0) & np.isfinite(moment0.uncertainty))
    signal_ok = np.isfinite(snr) & (snr >= config.moment_min_snr) & moment0.valid

    velocity = cube.velocity_lsrk_m_s
    in_window = window_overlaps(velocity, config.velocity_window_min_m_s, config.velocity_window_max_m_s) > 0
    windowed_velocity = velocity[in_window][:, None, None]

    positive_value = _signal_weights(cube, config, in_window)   # signal-gated: noise channels carry weight 0
    weight_sum = np.sum(positive_value, axis=0)
    centroid = np.divide(np.sum(positive_value * windowed_velocity, axis=0), weight_sum,
                         out=np.full(weight_sum.shape, np.nan), where=weight_sum > 0)

    valid = signal_ok & (weight_sum > 0)
    return SpatialMap(grid=cube.grid, kind="moment1_like_velocity_centroid",
                      value=np.where(valid, centroid, np.nan), uncertainty=None,
                      weight_sum=moment0.weight_sum, n_pointings=moment0.n_pointings, valid=valid,
                      units="m/s", metadata={"moment_min_snr": config.moment_min_snr,
                                             "gating": "integrated SNR >= moment_min_snr AND per-channel value >= "
                                                       "moment_min_snr * channel sigma",
                                             "reason_if_invalid": "SNR below moment_min_snr or no signal channel"})


def moment2_like(cube: ScienceCube, config: ScienceConfig, moment1: SpatialMap | None = None) -> SpatialMap:
    """Intensity-weighted velocity dispersion around the moment-1-like
    centroid - invalid wherever moment-1 itself is invalid, or coverage
    is too thin to support a second moment (section 45: never a silent
    NaN, always with a reason in `metadata`)."""
    moment1 = moment1 if moment1 is not None else moment1_like(cube, config)
    velocity = cube.velocity_lsrk_m_s
    in_window = window_overlaps(velocity, config.velocity_window_min_m_s, config.velocity_window_max_m_s) > 0
    n_window_channels = int(np.sum(in_window))
    windowed_velocity = velocity[in_window][:, None, None]

    positive_value = _signal_weights(cube, config, in_window)
    weight_sum = np.sum(positive_value, axis=0)
    centroid = np.where(np.isnan(moment1.value), 0.0, moment1.value)[None, :, :]
    variance = np.divide(np.sum(positive_value * (windowed_velocity - centroid) ** 2, axis=0), weight_sum,
                         out=np.full(weight_sum.shape, np.nan), where=weight_sum > 0)

    enough_channels = n_window_channels >= 3    # a dispersion from <3 channels is not meaningful (section 45)
    valid = moment1.valid & (variance >= 0) & np.isfinite(variance) & enough_channels
    dispersion = np.sqrt(np.clip(variance, 0.0, None))
    return SpatialMap(grid=cube.grid, kind="moment2_like_velocity_dispersion",
                      value=np.where(valid, dispersion, np.nan), uncertainty=None,
                      weight_sum=moment1.weight_sum, n_pointings=moment1.n_pointings, valid=valid,
                      units="m/s", metadata={"moment_min_snr": config.moment_min_snr, "n_window_channels": n_window_channels,
                                             "reason_if_invalid": "moment1 invalid, or fewer than 3 window channels"})
