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
    # np.interp requires an increasing x; a real velocity axis can be
    # decreasing (frequency and velocity are inversely related) - sort
    # once, explicitly, rather than assume an order (mirrors
    # alignment_engine/hi/cube_reduction.py's own explicit handling of
    # increasing-or-decreasing spectral axes).
    order = np.argsort(source_velocity_m_s)
    src_v = source_velocity_m_s[order]
    src_value = relative_intensity[order]
    src_unc = uncertainty[order]
    usable = (np.asarray(mask, dtype=np.int64)[order] == MaskFlag.GOOD.value)

    out_value = np.interp(target_velocity_m_s, src_v, src_value, left=np.nan, right=np.nan)
    out_unc = np.interp(target_velocity_m_s, src_v, src_unc, left=np.nan, right=np.nan)
    usable_at_target = np.interp(target_velocity_m_s, src_v, usable.astype(float), left=0.0, right=0.0)
    out_mask = np.where(usable_at_target >= 0.999, MaskFlag.GOOD.value, MaskFlag.MISSING.value).astype(np.int64)

    return VelocityResampleResult(velocity_lsrk_m_s=target_velocity_m_s, relative_intensity=out_value,
                                  uncertainty=out_unc, mask=out_mask)
