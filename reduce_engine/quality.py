"""QUALITY stage: never a bare GOOD/BAD number. Combines capture integrity,
clipping, usable fraction, RFI occupancy, baseline fit, calibration
compatibility, spectral coverage, contributing-spectra count, and velocity
availability into one QualityReport with explicit reasons - reasons are
attached at every state, including GOOD.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from reduce_engine.models import MaskFlag, QualityReport, QualityState


def _rfi_occupancy(mask: np.ndarray) -> float:
    return float(np.mean((mask.astype(np.int64) & MaskFlag.RFI.value) != 0))


def assess_quality(*, mask: np.ndarray, n_contributing: np.ndarray, clipping_fraction: float,
                   calibration_level: str, calibration_compatibility_status: str,
                   baseline_fit_quality_rms_fraction: float, velocity_frame: str,
                   usable_fraction_threshold: float = 0.5,
                   baseline_rms_bad_threshold: float = 0.5) -> QualityReport:
    reasons: list[str] = []
    metrics: dict[str, Any] = {}
    from reduce_engine.masks import usable_bin_mask
    usable_fraction = float(np.mean(usable_bin_mask(mask)))
    rfi_occupancy = _rfi_occupancy(mask)
    metrics.update({
        "usable_fraction": usable_fraction, "rfi_occupancy": rfi_occupancy,
        "clipping_fraction": clipping_fraction,
        "median_n_contributing": float(np.median(n_contributing)) if n_contributing.size else 0.0,
        "baseline_fit_quality_rms_fraction": baseline_fit_quality_rms_fraction,
    })

    bad = False
    if clipping_fraction > 0.001:
        bad = True
        reasons.append(f"ADC clipping fraction {clipping_fraction:.4f} exceeds tolerance")
    if usable_fraction < usable_fraction_threshold:
        bad = True
        reasons.append(f"usable_fraction {usable_fraction:.3f} below threshold {usable_fraction_threshold}")
    if np.median(n_contributing) < 1:
        bad = True
        reasons.append("no contributing spectra reached at least one bin")
    if not np.isnan(baseline_fit_quality_rms_fraction) and baseline_fit_quality_rms_fraction > baseline_rms_bad_threshold:
        bad = True
        reasons.append(f"baseline fit RMS fraction {baseline_fit_quality_rms_fraction:.3f} exceeds threshold")

    warning = False
    if calibration_level == "UNCALIBRATED":
        warning = True
        reasons.append(f"no relative calibration applied ({calibration_compatibility_status})")
    if velocity_frame == "UNAVAILABLE":
        warning = True
        reasons.append("velocity frame unavailable - missing pointing/time/location metadata")
    if rfi_occupancy > 0.3:
        warning = True
        reasons.append(f"RFI occupancy {rfi_occupancy:.3f} is high")

    if bad:
        state = QualityState.BAD
    elif warning:
        state = QualityState.WARNING
    else:
        state = QualityState.GOOD
        reasons.append(f"usable_fraction={usable_fraction:.3f}, rfi_occupancy={rfi_occupancy:.3f}, "
                       f"calibration_level={calibration_level}")
    return QualityReport(state=state, reasons=reasons, metrics=metrics)
