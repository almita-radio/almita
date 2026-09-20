"""Velocity-axis resampling (sections 20-21, real-data finding): REDUCE
guarantees a common FREQUENCY grid within a campaign (confirmed: every
point in every real campaign audited shares identical `frequency_hz`),
but `velocity_lsrk_m_s` is direction+time dependent (real LSRK physics -
Earth's rotational/orbital motion projected along each point's own line
of sight, at each point's own capture time) and genuinely differs point
to point - up to several channel widths across a real 9-point, ~3deg
campaign (measured: up to 641 m/s, ~10.4 channels, on
ALMITA-WEB-SMALL-RUN-01). A cube CANNOT be built by matching channel
index across points without resampling first - that would silently
misalign the line by several channels across the field.

This is NOT "SCIENCE recalculates Doppler" (forbidden, section 19): the
velocity axis being resampled here was already computed by REDUCE from
RAW; this module only interpolates that already-Doppler-corrected
per-point axis onto one shared axis - the same class of operation as
REDUCE's own frequency-axis resample.py (frozen, not touched here), and
deliberately mirroring its exact mask-aware convention for consistency:
linear interpolation, NaN/MISSING outside the source range, a resampled
bin is GOOD only if interpolation drew (to numerical certainty) only
from GOOD source bins.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from reduce_engine.models import MaskFlag


@dataclass
class VelocityResampleResult:
    velocity_lsrk_m_s: np.ndarray
    relative_intensity: np.ndarray
    uncertainty: np.ndarray
    mask: np.ndarray


def resample_to_velocity_axis(source_velocity_m_s: np.ndarray, relative_intensity: np.ndarray,
                              uncertainty: np.ndarray, mask: np.ndarray,
                              target_velocity_m_s: np.ndarray) -> VelocityResampleResult:
    """Linear interpolation of value and sigma onto `target_velocity_m_s`.

    Explicit bracket-and-weight implementation (not np.interp) so mask/NaN semantics are exact:

    - A target bin is GOOD iff every source bin that carries non-zero interpolation weight is GOOD
      (mask == GOOD, finite value, finite positive sigma). A target that lands exactly on a GOOD source
      bin depends on that bin ONLY, whatever its neighbours are.
    - Otherwise (a masked/non-finite contributor, or target outside the source range) the target bin is
      MISSING with value/sigma NaN. There is no tolerance: leakage of a masked neighbour, however small,
      makes the bin MISSING (conservative - a masked bin can widen by up to one channel per side).
      The previous np.interp version accepted >=0.999 GOOD weight and could therefore emit a GOOD bin
      whose value was NaN (masked neighbour with ~1e-4 weight), poisoning the downstream sum.
    - sigma is interpolated linearly with the same weights (documented convention, mirrors REDUCE's own
      frequency resample). For independent source noise this over-states the per-channel sigma by up to
      sqrt(2) at a half-channel shift and ignores the neighbour correlation the interpolation introduces;
      the two effects cancel in the INTEGRATED-map variance (measured in test_science_resample_truth.py).

    Source axis may be increasing or decreasing; it is sorted once here, never assumed."""
    source_velocity_m_s = np.asarray(source_velocity_m_s, dtype=np.float64)
    order = np.argsort(source_velocity_m_s, kind="stable")
    src_v = source_velocity_m_s[order]
    src_value = np.asarray(relative_intensity, dtype=np.float64)[order]
    src_unc = np.asarray(uncertainty, dtype=np.float64)[order]
    usable = ((np.asarray(mask, dtype=np.int64)[order] == MaskFlag.GOOD.value)
              & np.isfinite(src_value) & np.isfinite(src_unc) & (src_unc > 0))
    target = np.asarray(target_velocity_m_s, dtype=np.float64)
    n = src_v.shape[0]

    out_value = np.full(target.shape, np.nan)
    out_unc = np.full(target.shape, np.nan)
    if n < 2 or not np.all(np.diff(src_v) > 0):
        raise ValueError("source velocity axis must have >= 2 strictly distinct samples")

    in_range = np.isfinite(target) & (target >= src_v[0]) & (target <= src_v[-1])
    j = np.clip(np.searchsorted(src_v, target, side="right") - 1, 0, n - 2)
    t = np.where(in_range, (target - src_v[j]) / (src_v[j + 1] - src_v[j]), 0.0)   # 0 <= t <= 1 where in range
    w0, w1 = 1.0 - t, t
    good = in_range & ((w0 == 0.0) | usable[j]) & ((w1 == 0.0) | usable[j + 1])

    # Select-then-multiply: unusable neighbours are replaced by a finite placeholder before the
    # arithmetic and their result is discarded by `good`; no NaN is ever an operand of the sum.
    v0, v1 = np.where(usable[j], src_value[j], 0.0), np.where(usable[j + 1], src_value[j + 1], 0.0)
    u0, u1 = np.where(usable[j], src_unc[j], 0.0), np.where(usable[j + 1], src_unc[j + 1], 0.0)
    out_value = np.where(good, w0 * v0 + w1 * v1, np.nan)
    out_unc = np.where(good, w0 * u0 + w1 * u1, np.nan)
    out_mask = np.where(good, MaskFlag.GOOD.value, MaskFlag.MISSING.value).astype(np.int64)

    return VelocityResampleResult(velocity_lsrk_m_s=target, relative_intensity=out_value,
                                  uncertainty=out_unc, mask=out_mask)
