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

import numpy as np

from reduce_engine.models import MaskFlag
from science_engine.beam import beam_weight
from science_engine.grid import point_to_pixel_separations_deg
from science_engine.models import BeamModel, ScienceGrid

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
        self._n_contributing = np.zeros(shape, dtype=np.int64)

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
        inv_var = np.zeros_like(np.broadcast_to(sigmas, np.shape(bin_valid)), dtype=np.float64)
        np.divide(1.0, np.asarray(sigmas) ** 2, out=inv_var, where=np.asarray(bin_valid))

        if spatial_weight.ndim == 2 and np.ndim(inv_var) == 1:
            beam = spatial_weight[None, :, :]
            inv_var_b = inv_var[:, None, None]
            bin_valid_b = np.asarray(bin_valid)[:, None, None]
            values_b = np.asarray(values)[:, None, None]
        else:
            beam, inv_var_b, bin_valid_b, values_b = spatial_weight, inv_var, bin_valid, values

        w = beam * inv_var_b * bin_valid_b
        # REDUCE stores NaN (never a fabricated number) at every masked bin's relative_intensity - confirmed on
        # real data (data/reduced/.../points/5, 343/343 masked bins are NaN, 0/n GOOD bins are). w is already 0
        # at those positions via bin_valid_b, but a plain `0 * nan` is STILL nan (IEEE754), which would poison
        # this whole voxel's running sum via `+=` forever. Guard the value itself, not just its weight.
        safe_values_b = np.where(bin_valid_b, values_b, 0.0)
        self._weighted_value_sum += w * safe_values_b
        self._weight_sum += w
        # w^2 * sigma^2 == beam^2 * inv_var (algebraically: w^2*sigma^2 = beam^2*inv_var^2*sigma^2*valid =
        # beam^2*inv_var*valid, since inv_var*sigma^2==1 where valid) - avoids re-touching sigma (which may be
        # inf/nan where invalid) and its inf*0 pitfalls entirely.
        self._weight_sq_sigma_sq_sum += (beam ** 2) * inv_var_b * bin_valid_b
        self._n_contributing += (w > 0).astype(np.int64)

    def finalize(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Returns (value, uncertainty, weight_sum, n_contributing).
        value/uncertainty are NaN where weight_sum==0 (no contribution -
        never a fabricated 0, section 27/67)."""
        valid = self._weight_sum > 0
        value = np.full(self.shape, np.nan)
        variance = np.full(self.shape, np.nan)
        np.divide(self._weighted_value_sum, self._weight_sum, out=value, where=valid)
        np.divide(self._weight_sq_sigma_sq_sum, self._weight_sum ** 2, out=variance, where=valid)
        uncertainty = np.sqrt(variance)
        return value, uncertainty, self._weight_sum, self._n_contributing


def bin_validity(mask: np.ndarray, uncertainty: np.ndarray, uncertainty_floor_relative: float) -> np.ndarray:
    """A bin is usable iff REDUCE's own mask says GOOD (section 27:
    masked bins never contribute, never zero-filled) AND its uncertainty
    is finite and positive, above a relative floor derived from this same
    point's own uncertainty distribution (section 23: no hardcoded magic
    epsilon - sigma<=~0 is itself invalid data, not something to floor)."""
    good = mask == MaskFlag.GOOD.value
    finite_positive = np.isfinite(uncertainty) & (uncertainty > 0)
    if not np.any(finite_positive):
        return good & finite_positive  # all-invalid input point - nothing usable, never a floor hack
    median_sigma = np.nanmedian(uncertainty[finite_positive])
    floor = uncertainty_floor_relative * median_sigma
    return good & finite_positive & (uncertainty > floor)


def spatial_weight_for_point(grid: ScienceGrid, ra_deg: float, dec_deg: float, beam: BeamModel) -> np.ndarray:
    theta = point_to_pixel_separations_deg(grid, ra_deg, dec_deg)
    return beam_weight(theta, beam)
