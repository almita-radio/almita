"""Integrated relative intensity map: the primary visual SCIENCE V1 product.

    I_int(y, x) = SUM_i  overlap_i * relative_intensity_i(y, x)         [relative_intensity * m/s]
    sigma_I(y, x) = SQRT( SUM_i (overlap_i * sigma_i(y, x))^2 )         (independent channels, see below)

where the sum runs over VALID channels i and overlap_i is the width (m/s) of the intersection of
channel i's velocity bin [edge_lo_i, edge_hi_i] with the requested window [vmin, vmax]. Bin edges are the
midpoints between neighbouring channel centres computed from the ACTUAL axis (never an assumed uniform
dv), so this is exact for an irregular axis, for a window that does not fall on channel boundaries, for a
window narrower than one channel (= that channel's value x window width), and for a window that reaches
outside the cube (the outside part simply contributes nothing - and is REPORTED, see below).

    spectral_coverage(y, x) = ( SUM_{valid i} overlap_i ) / (vmax - vmin)

is the fraction of the REQUESTED window backed by valid data (masked channels AND any part of the window
outside the cube both lower it). A pixel is valid only if spectral_coverage >= min_spectral_coverage_fraction.
Masked channels are NEVER interpolated: an incomplete line gives an incomplete (lower) integral together with
a lower spectral_coverage - the honest report, not a "recovered truth".

Neighbouring channels are NOT independent (audit 2026-11-25, operator "dale nomas"): REDUCE's Hann window makes
the power in adjacent REDUCE channels correlate. For white noise the Hann window's Fourier coefficients (1/2, -1/4, -1/4)
give a power correlation of rho1 = (2/3)^2 = 4/9 between neighbours and rho2 = (1/6)^2 = 1/36 two bins apart
(measured on real captures: rho1 0.44-0.47). The variance of a sum therefore carries the cross terms

    sigma_I^2 = SUM_i s_i^2 + 2 rho1 SUM_i s_i s_(i+1) + 2 rho2 SUM_i s_i s_(i+2),     s_i = overlap_i * sigma_i

(x1.39 in sigma for a wide window; it was the independent-channel sum, 0.72 of the scatter measured across
line-free windows). rho comes from ScienceConfig.channel_correlation; (0, 0) gives the old independent sum. The
velocity resample (linear interpolation, sigma propagated as fully correlated) adds a little more correlation; the
baseline's own wander between pointings is NOT in this sigma (see linefree_noise.py). Neighbouring OUTPUT PIXELS are also
beam-correlated: a per-pixel sigma does not make neighbouring pixels independent.

Units: relative_intensity is dimensionless, so the integral is dimensionless x m/s ("relative_intensity*m/s").
SCIENCE emits dimensionless relative units only; absolute calibration is unsupported (docs/SCIENCE_MODEL.md).
"""
from __future__ import annotations

import numpy as np

from science_engine.config import ScienceConfig
from science_engine.models import ScienceCube, SpatialMap

_CHUNK_CHANNELS = 256   # channels reduced per block so temporaries stay ~chunk x Ny x Nx, never cube-sized


def bin_bounds(velocity_m_s: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-channel [lo, hi] velocity bin edges from the actual axis (any monotonic order, any spacing)."""
    velocity_m_s = np.asarray(velocity_m_s, dtype=np.float64)
    if velocity_m_s.shape[0] < 2:
        raise ValueError("need >= 2 velocity channels to define bin widths")
    order = np.argsort(velocity_m_s, kind="stable")
    sv = velocity_m_s[order]
    mids = 0.5 * (sv[1:] + sv[:-1])
    lo_sorted = np.concatenate([[sv[0] - (mids[0] - sv[0])], mids])
    hi_sorted = np.concatenate([mids, [sv[-1] + (sv[-1] - mids[-1])]])
    lo, hi = np.empty_like(sv), np.empty_like(sv)
    lo[order], hi[order] = lo_sorted, hi_sorted
    return lo, hi


def window_overlaps(velocity_m_s: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    lo, hi = bin_bounds(velocity_m_s)
    return np.clip(np.minimum(hi, vmax) - np.maximum(lo, vmin), 0.0, None)


def integrated_map(cube: ScienceCube, config: ScienceConfig) -> SpatialMap:
    velocity = cube.velocity_lsrk_m_s
    vmin, vmax = config.velocity_window_min_m_s, config.velocity_window_max_m_s
    overlap = window_overlaps(velocity, vmin, vmax)
    if not np.any(overlap > 0):
        raise ValueError(
            f"velocity window [{vmin}, {vmax}] m/s does not overlap the cube "
            f"[{velocity.min():.0f}, {velocity.max():.0f}] m/s - BLOCKED (nothing to integrate)")
    requested_width = vmax - vmin
    window_covered_fraction = float(np.sum(overlap) / requested_width)
    channels = np.flatnonzero(overlap > 0)

    ny, nx = cube.grid.ny, cube.grid.nx
    value = np.zeros((ny, nx))
    variance = np.zeros((ny, nx))
    valid_width = np.zeros((ny, nx))
    weight_sum_2d = np.zeros((ny, nx))
    n_pointings_2d = np.zeros((ny, nx), dtype=cube.n_pointings.dtype)
    rho = tuple(config.channel_correlation)
    prev = None                                        # last two s-slices of the previous chunk (cross terms)
    for start in range(0, channels.shape[0], _CHUNK_CHANNELS):
        idx = channels[start:start + _CHUNK_CHANNELS]
        ov = overlap[idx][:, None, None]
        valid = cube.valid[idx]
        # select first, multiply second: an invalid (NaN) channel is replaced by a finite 0 BEFORE any
        # arithmetic; its absence is carried by valid_width/spectral_coverage, never faked as I = 0.
        value += np.sum(np.where(valid, cube.relative_intensity[idx], 0.0) * ov, axis=0)
        s_chunk = np.where(valid, cube.uncertainty[idx], 0.0) * ov
        variance += _correlated_variance(s_chunk, prev, rho)
        prev = s_chunk[-2:]
        valid_width += np.sum(np.where(valid, ov, 0.0), axis=0)
        weight_sum_2d += np.sum(np.where(valid, cube.weight_sum[idx], 0.0), axis=0)
        n_pointings_2d = np.maximum(n_pointings_2d, np.max(np.where(valid, cube.n_pointings[idx], 0), axis=0))

    spectral_coverage = valid_width / requested_width
    valid_2d = spectral_coverage >= config.min_spectral_coverage_fraction
    value = np.where(valid_2d, value, np.nan)          # below threshold: invalid - never extrapolated
    uncertainty = np.where(valid_2d, np.sqrt(variance), np.nan)

    return SpatialMap(
        grid=cube.grid, kind="integrated_relative_intensity", value=value, uncertainty=uncertainty,
        weight_sum=weight_sum_2d, n_pointings=n_pointings_2d, valid=valid_2d,
        units="relative_intensity_dimensionless * m/s", spectral_coverage=spectral_coverage,
        metadata={"velocity_window_min_m_s": vmin, "velocity_window_max_m_s": vmax,
                 "requested_window_width_m_s": requested_width,
                 "window_covered_by_cube_fraction": window_covered_fraction,
                 "window_status": "FULL" if window_covered_fraction >= 1.0 - 1e-9 else "PARTIAL_OUTSIDE_CUBE",
                 "n_window_channels": int(channels.shape[0]),
                 "integration_rule": "bin-overlap: sum(overlap_i * value_i), overlap from actual channel bin edges",
                 "uncertainty_assumption": _assumption(rho),
                 "channel_correlation": list(rho),
                 "min_spectral_coverage_fraction": config.min_spectral_coverage_fraction},
    )


def _correlated_variance(s: np.ndarray, prev, rho) -> np.ndarray:
    """Variance of SUM s_i for channels in index (= frequency) order, with correlation rho[k-1] between channels
    k apart (k = 1, 2). `prev` = the last two s-slices of the previous chunk, so cross terms across chunk borders
    are kept."""
    full = s if prev is None else np.concatenate([prev, s], axis=0)
    n_prev = 0 if prev is None else prev.shape[0]
    var = np.sum(s * s, axis=0)
    for k, r in enumerate(rho, start=1):
        if r == 0 or full.shape[0] <= k:
            continue
        a, b = full[:-k], full[k:]
        # only the pairs whose LATER channel is in this chunk (earlier pairs were counted with the previous chunk)
        first = max(0, n_prev - k)
        var = var + 2.0 * r * np.sum(a[first:] * b[first:], axis=0)
    return var


def _assumption(rho) -> str:
    if not any(rho):
        return "independent channels"
    return "adjacent channels correlated (Hann window): " + ", ".join(f"rho{k}={r:.4f}" for k, r in enumerate(rho, 1))


def correlated_sum_sigma(sigmas: np.ndarray, rho=None) -> np.ndarray:
    """sigma of the SUM over axis 0 of channels with the given per-channel sigmas (same correlation model)."""
    from science_engine.config import HANN_CHANNEL_CORRELATION
    rho = HANN_CHANNEL_CORRELATION if rho is None else rho
    return np.sqrt(_correlated_variance(np.asarray(sigmas, dtype=np.float64), None, tuple(rho)))


def _channel_map_from_indices(cube: ScienceCube, indices: np.ndarray, metadata: dict) -> SpatialMap:
    windowed_valid = cube.valid[indices]
    n_channels = int(indices.shape[0])
    n_valid = np.sum(windowed_valid, axis=0)
    coverage = n_valid / n_channels
    # mean of the VALID channels: select first (NaN never enters the sum), divide by the valid count
    safe_count = np.maximum(n_valid, 1)
    value = np.sum(np.where(windowed_valid, cube.relative_intensity[indices], 0.0), axis=0) / safe_count
    # mean of n channels: sigma = sigma(sum) / n_valid with the Hann cross terms between neighbouring selected
    # channels (n_valid = 1 -> exactly the cube sigma). Channel indices are consecutive for an interval map.
    from science_engine.config import HANN_CHANNEL_CORRELATION
    s_sel = np.where(windowed_valid, cube.uncertainty[indices], 0.0)
    contiguous = indices.shape[0] < 2 or bool(np.all(np.diff(indices) == 1))
    uncertainty = correlated_sum_sigma(s_sel, HANN_CHANNEL_CORRELATION if contiguous else (0.0, 0.0)) / safe_count
    weight_sum_2d = np.sum(np.where(windowed_valid, cube.weight_sum[indices], 0.0), axis=0)
    n_pointings_2d = np.max(np.where(windowed_valid, cube.n_pointings[indices], 0), axis=0)
    valid_2d = n_valid > 0
    selected = cube.velocity_lsrk_m_s[indices]
    metadata = {**metadata, "n_channels": n_channels, "channel_indices": [int(i) for i in indices],
                "selected_velocity_min_m_s": float(selected.min()), "selected_velocity_max_m_s": float(selected.max()),
                "selected_velocity_mean_m_s": float(selected.mean())}
    return SpatialMap(grid=cube.grid, kind="channel_map", value=np.where(valid_2d, value, np.nan),
                      uncertainty=np.where(valid_2d, uncertainty, np.nan), weight_sum=weight_sum_2d,
                      n_pointings=n_pointings_2d, valid=valid_2d, units="relative_intensity_dimensionless",
                      spectral_coverage=coverage, metadata=metadata)


def channel_map(cube: ScienceCube, velocity_center_m_s: float, velocity_width_m_s: float) -> SpatialMap:
    """Velocity-INTERVAL map: mean over the channels whose CENTRE lies in [center-width/2, center+width/2]
    (inclusive). The actually selected channel indices and velocities are reported in metadata."""
    lo, hi = velocity_center_m_s - velocity_width_m_s / 2, velocity_center_m_s + velocity_width_m_s / 2
    in_window = (cube.velocity_lsrk_m_s >= lo) & (cube.velocity_lsrk_m_s <= hi)
    if not np.any(in_window):
        raise ValueError(f"channel window [{lo}, {hi}] m/s selects zero channels")
    return _channel_map_from_indices(cube, np.flatnonzero(in_window),
                                     {"mode": "interval", "velocity_center_m_s": velocity_center_m_s,
                                      "velocity_width_m_s": velocity_width_m_s})


def nearest_channel_index(cube: ScienceCube, velocity_m_s: float) -> int:
    """Index of the channel whose centre is nearest `velocity_m_s` (ties -> the lower index)."""
    if not np.isfinite(velocity_m_s):
        raise ValueError("velocity must be finite")
    return int(np.argmin(np.abs(cube.velocity_lsrk_m_s - velocity_m_s)))


def channel_map_nearest(cube: ScienceCube, velocity_m_s: float) -> SpatialMap:
    """Single-channel map at the channel nearest `velocity_m_s`. value/uncertainty equal the cube's own slice
    exactly (no recomputation); the ACTUAL velocity selected is reported in metadata."""
    index = nearest_channel_index(cube, velocity_m_s)
    return _channel_map_from_indices(cube, np.array([index]), {"mode": "nearest",
                                                                "requested_velocity_m_s": float(velocity_m_s)})
