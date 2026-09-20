"""Beam-weighted spatial+spectral gridding (sections 22-33).

Per-voxel weight = beam_response(angular_distance) x point_quality_gate x
inverse_variance(uncertainty) - each factor physically/documentedly
defensible on its own (section 22), never mixed arbitrarily.

Masked bins (REDUCE's own MaskFlag != GOOD) NEVER contribute - never
zero-filled (section 27): a masked bin is a missing contribution, exactly
like an invalid/non-finite uncertainty (section 83-84). Accumulation
loops over input points (not a single N_points x N_pixels x N_velocity
tensor - see docs/SCIENCE_PIPELINE.md's Performance/Memory section for
why) so peak memory scales with the OUTPUT cube size, not with point
count.
"""
from __future__ import annotations

import warnings

import numpy as np

from reduce_engine.models import MaskFlag
from science_engine.beam import beam_weight
from science_engine.grid import point_to_pixel_separations_deg
from science_engine.models import BeamModel, ScienceGrid

# ---- NaN / validity contract (SCIENCE_MODEL.md "NaN policy") -------------------------------------------
# 1. A masked/invalid value MAY be NaN (REDUCE stores NaN at every masked bin).
# 2. NaN/Inf NEVER contributes to any sum. Validity is applied BEFORE arithmetic accumulation, by
#    selecting (np.where / boolean indexing), never by multiplying by a zero weight: 0 * NaN == NaN and
#    0 * Inf == NaN in IEEE754, so "weight zero" does NOT neutralise a non-finite value.
# 3. A voxel with no valid contributor is NaN + weight_sum 0 + valid False - never a fabricated 0.
# 4. A finite 0.0 relative_intensity with a valid mask IS a measurement (contributes, valid True);
#    zero is never conflated with missing.
#
# Numerical guard (NOT a scientific threshold): the smallest sigma accepted so that the running
# weight sum cannot overflow float64. weight = 1/sigma^2, and up to SIGMA_GUARD_MAX_CONTRIBUTORS
# such weights are summed into one voxel, so 1/sigma_min^2 * N must stay < finfo.max:
#     sigma_min = sqrt(N / finfo.max)      (N = 2**20  ->  ~7.6e-152)
SIGMA_GUARD_MAX_CONTRIBUTORS = 2 ** 20
SIGMA_ABSOLUTE_FLOOR = float(np.sqrt(SIGMA_GUARD_MAX_CONTRIBUTORS / np.finfo(np.float64).max))

QUALITY_POLICY_ALLOWED_STATES = {
    "STRICT": frozenset({"GOOD"}),
    "STANDARD": frozenset({"GOOD", "WARNING"}),
    "PERMISSIVE": frozenset({"GOOD", "WARNING", "UNKNOWN"}),
}


def point_passes_quality_policy(reduce_quality_state: str, quality_policy: str) -> bool:
    return reduce_quality_state in QUALITY_POLICY_ALLOWED_STATES[quality_policy]


class GriddingAccumulator:
    """Running sums for a weighted accumulation into an (Nv, Ny, Nx) - or
    (Ny, Nx) for a single-channel product - grid. Call `add_point()` once
    per input point; call `finalize()` once at the end. Allocates its
    arrays once (section 119), so peak memory scales with the OUTPUT
    shape, never with the number of input points.

    Weight per (point, channel, pixel): w = beam(theta) * (1/sigma^2),
    zero wherever the bin is masked/invalid (`bin_validity`) or the point
    fails the quality policy (weight passed in as already zeroed for that
    point - see `spatial_weight_for_point`). This is a general weighted
    mean, NOT a pure inverse-variance combination once the beam factor is
    folded in, so its propagated uncertainty (section 32) needs the
    general formula:

        Var(y) = sum(w_i^2 * sigma_i^2) / (sum(w_i))^2

    not the pure-inverse-variance shortcut 1/sqrt(sum(w)) (which only
    holds when every w_i IS exactly 1/sigma_i^2 with no other factor) -
    verified against a direct brute-force computation in
    test_science_uncertainty.py.
    """

    def __init__(self, shape: tuple[int, ...]):
        self.shape = shape
        self._weighted_value_sum = np.zeros(shape, dtype=np.float64)
        self._weight_sum = np.zeros(shape, dtype=np.float64)
        self._weight_sq_sigma_sq_sum = np.zeros(shape, dtype=np.float64)  # sum(w_i^2 * sigma_i^2)
        self._n_pointings = np.zeros(shape, dtype=np.int64)  # count of POINTINGS with w>0 (not REDUCE's n_contributing)
        self.n_nonfinite_rejected = 0

    def add_point(self, spatial_weight: np.ndarray, values: np.ndarray, sigmas: np.ndarray,
                 bin_valid: np.ndarray) -> None:
        """Two calling conventions, matching this accumulator's own shape:
        - 2D-map accumulator (`shape=(Ny,Nx)`): `spatial_weight` is
          (Ny,Nx); `values`/`sigmas`/`bin_valid` are plain Python/0-d
          scalars (one number per point).
        - cube accumulator (`shape=(Nv,Ny,Nx)`): `spatial_weight` is
          still (Ny,Nx) (the beam doesn't depend on velocity);
          `values`/`sigmas`/`bin_valid` are (Nv,) arrays, broadcast
          against the spatial map internally. Verified against a direct
          brute-force computation in both modes
          (test_science_gridding.py)."""
        spatial_weight = np.asarray(spatial_weight, dtype=np.float64)
        if not np.all(np.isfinite(spatial_weight)) or np.any(spatial_weight < 0):
            # a non-finite/negative spatial weight is a CALLER contract violation (beam_weight() is bounded
            # to [0, 1] by construction) - fail loudly rather than emit a silently-wrong voxel.
            raise ValueError("spatial_weight must be finite and >= 0")
        values = np.asarray(values, dtype=np.float64)
        sigmas = np.asarray(sigmas, dtype=np.float64)
        bin_valid = np.asarray(bin_valid, dtype=bool)

        # Effective validity = what the caller promised AND what is actually finite/usable. Enforced HERE
        # (defence in depth) so a NaN/Inf value or sigma can never contribute even if the caller's
        # `bin_valid` was wrong (e.g. a resample edge case).
        effective = bin_valid & np.isfinite(values) & np.isfinite(sigmas) & (sigmas >= SIGMA_ABSOLUTE_FLOOR)
        self.n_nonfinite_rejected += int(np.sum(bin_valid & ~effective))

        if spatial_weight.ndim == 2 and effective.ndim == 1:
            # cube mode: process CHANNEL_BLOCK channels at a time so temporaries are (block, Ny, Nx), never
            # cube-sized (peak memory then scales with the 4 accumulator arrays only)
            for start in range(0, effective.shape[0], self.CHANNEL_BLOCK):
                sl = slice(start, start + self.CHANNEL_BLOCK)
                self._accumulate(sl, spatial_weight[None, :, :], effective[sl, None, None],
                                 values[sl, None, None], sigmas[sl, None, None])
        else:
            self._accumulate(Ellipsis, spatial_weight, effective, values, sigmas)

    CHANNEL_BLOCK = 256

    def _accumulate(self, sl, beam, effective, values, sigmas) -> None:
        # Select first, multiply second: invalid positions get a *finite placeholder* (value 0, sigma 1)
        # that is then multiplied by a weight of exactly 0. No NaN/Inf is ever an operand of an
        # arithmetic accumulation.
        safe_values = np.where(effective, values, 0.0)
        safe_sigmas = np.where(effective, sigmas, 1.0)
        with np.errstate(over="ignore"):    # sigma > ~1e154: sigma^2 -> inf, 1/inf == 0: the weight underflows to exactly 0
            inv_var = np.where(effective, 1.0 / (safe_sigmas * safe_sigmas), 0.0)
        w = beam * inv_var
        self._weighted_value_sum[sl] += w * safe_values
        self._weight_sum[sl] += w
        # Var(y) = sum(w_i^2 sigma_i^2)/(sum w_i)^2 and w_i^2 sigma_i^2 == beam_i^2 * inv_var_i, algebraically.
        self._weight_sq_sigma_sq_sum[sl] += (beam * beam) * inv_var
        self._n_pointings[sl] += (w > 0)

    def finalize(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Returns (value, uncertainty, weight_sum, n_pointings).
        value/uncertainty are NaN where weight_sum==0 (no contribution -
        never a fabricated 0, section 27/67)."""
        valid = self._weight_sum > 0
        value = np.full(self.shape, np.nan)
        variance = np.full(self.shape, np.nan)
        np.divide(self._weighted_value_sum, self._weight_sum, out=value, where=valid)
        # (sum w^2 sigma^2 / sum w) / sum w  - NOT / (sum w)**2: squaring a large weight sum overflows to inf and
        # would silently report sigma == 0 (false infinite certainty).
        np.divide(self._weight_sq_sigma_sq_sum, self._weight_sum, out=variance, where=valid)
        np.divide(variance, self._weight_sum, out=variance, where=valid)
        uncertainty = np.sqrt(variance)
        return value, uncertainty, self._weight_sum, self._n_pointings


def sigma_suspect_bins(mask: np.ndarray, uncertainty: np.ndarray, fraction: float, window: int) -> np.ndarray:
    """True where a GOOD bin's sigma is implausibly small compared with its own neighbourhood: sigma <
    `fraction` x the running median (over `window` channels, centred, NaN-aware) of the neighbouring GOOD sigmas.
    An isolated dip is a defect of the sigma estimate, not noise; a broad, genuinely low-noise stretch (wider than
    the window) moves the median with it and is NOT flagged. fraction == 0 disables the check."""
    from numpy.lib.stride_tricks import sliding_window_view
    uncertainty = np.asarray(uncertainty, dtype=np.float64)
    suspect = np.zeros(uncertainty.shape, dtype=bool)
    if fraction <= 0 or uncertainty.shape[0] < 3:
        return suspect
    usable = (np.asarray(mask) == MaskFlag.GOOD.value) & np.isfinite(uncertainty) & (uncertainty > 0)
    sigma = np.where(usable, uncertainty, np.nan)
    half = window // 2
    padded = np.pad(sigma, (half, half), constant_values=np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)          # all-NaN neighbourhoods (fully masked stretches)
        local_median = np.nanmedian(sliding_window_view(padded, window), axis=1)
    with np.errstate(invalid="ignore"):
        suspect = usable & (sigma < fraction * local_median)
    return suspect


def bin_validity(mask: np.ndarray, uncertainty: np.ndarray, uncertainty_floor_relative: float,
                 values: np.ndarray | None = None) -> np.ndarray:
    """A bin is usable iff REDUCE's own mask says GOOD (masked bins never
    contribute, never zero-filled), its uncertainty is finite and
    positive above a relative floor derived from this same point's own
    uncertainty distribution (no hardcoded epsilon: sigma <= ~0 is
    invalid data, not something to floor), and - when `values` is given -
    its value is finite.

    Independently of the relative floor, sigma below SIGMA_ABSOLUTE_FLOOR
    is rejected: a pure numerical-overflow guard derived from float64's
    range (see the constant's derivation above), needed because the
    relative floor cannot protect a single isolated adversarial bin with
    nothing to compare against (sigma=1e-300 in isolation)."""
    good = mask == MaskFlag.GOOD.value
    finite_positive = np.isfinite(uncertainty) & (uncertainty > 0)
    usable = good & finite_positive
    if values is not None:
        usable = usable & np.isfinite(values)
    if not np.any(finite_positive):
        return np.zeros_like(usable)  # all-invalid input point - nothing usable, never a floor hack
    median_sigma = np.nanmedian(uncertainty[finite_positive])
    floor = max(uncertainty_floor_relative * median_sigma, SIGMA_ABSOLUTE_FLOOR)
    return usable & (uncertainty > floor)


def spatial_weight_for_point(grid: ScienceGrid, ra_deg: float, dec_deg: float, beam: BeamModel) -> np.ndarray:
    theta = point_to_pixel_separations_deg(grid, ra_deg, dec_deg)
    return beam_weight(theta, beam)
