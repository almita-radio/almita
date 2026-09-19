"""RELATIVE CALIBRATION stage: consumes calibration_foundation.py's already
validated relative/instrumental profiles. Never reimplements the reference
ensemble, DC/spur detection, or the correction formula - only orchestrates
compatibility checking and applies the existing primitive.

REDUCE never claims Kelvin/dBm/Tsys/NF/Jy/SEFD/Tant. calibration_level is
always "RELATIVE" (a compatible profile was applied) or "UNCALIBRATED"
(no profile given, or the capture was incompatible with it - BLOCKED/
WARNING per severity, never silently applied anyway).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

import calibration_foundation as cf


@dataclass
class CalibrationOutcome:
    calibration_level: str  # "RELATIVE" | "UNCALIBRATED"
    profile_id: Optional[str]
    profile_hash: Optional[str]
    compatibility_status: str  # "COMPATIBLE" | "INCOMPATIBLE" | "UNKNOWN" | "NOT_ATTEMPTED"
    compatibility_reason: str
    spectrum_before_calibration_summary: dict[str, float]
    relative_psd_db: Optional[np.ndarray]
    fractional_excess: Optional[np.ndarray]
    fractional_uncertainty: Optional[np.ndarray]

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibration_level": self.calibration_level, "calibration_profile_id": self.profile_id,
            "calibration_profile_hash": self.profile_hash,
            "compatibility_status": self.compatibility_status, "compatibility_reason": self.compatibility_reason,
            "spectrum_before_calibration_summary": self.spectrum_before_calibration_summary,
        }


def _profile_hash(profile_path: Path) -> str:
    from reduce_engine.models import sha256_of_file
    return sha256_of_file(profile_path.with_suffix(".npz"))


def _profile_id(profile_path: Optional[str], metadata: dict[str, Any]) -> Optional[str]:
    """The profile's own file stem is a stable, human-meaningful id;
    calibration_foundation.py's profile metadata has no dedicated "id"
    field of its own (only created_utc), so that is the fallback only
    when no path is available at all."""
    if profile_path:
        return Path(profile_path).stem
    return metadata.get("created_utc")


def load_profile(profile_path: Optional[str]) -> Optional[dict[str, Any]]:
    if profile_path is None:
        return None
    return cf.load_calibration_profile(profile_path)


def apply_calibration(frequency_hz: np.ndarray, psd: np.ndarray, *, capture_path: str,
                      profile: Optional[dict[str, Any]], profile_path: Optional[str]) -> CalibrationOutcome:
    before_summary = {
        "median": float(np.median(psd)), "min": float(np.min(psd)), "max": float(np.max(psd)),
    }
    if profile is None:
        return CalibrationOutcome(
            calibration_level="UNCALIBRATED", profile_id=None, profile_hash=None,
            compatibility_status="NOT_ATTEMPTED", compatibility_reason="no calibration profile supplied",
            spectrum_before_calibration_summary=before_summary,
            relative_psd_db=None, fractional_excess=None, fractional_uncertainty=None,
        )
    compatibility = cf.check_calibration_compatibility(profile, capture_path)
    if compatibility["status"] != "COMPATIBLE":
        return CalibrationOutcome(
            calibration_level="UNCALIBRATED", profile_id=_profile_id(profile_path, profile["metadata"]),
            profile_hash=_profile_hash(Path(profile_path)) if profile_path else None,
            compatibility_status=compatibility["status"], compatibility_reason=compatibility["reason"],
            spectrum_before_calibration_summary=before_summary,
            relative_psd_db=None, fractional_excess=None, fractional_uncertainty=None,
        )
    result = cf.apply_relative_calibration_to_psd(profile, frequency_hz, psd)
    return CalibrationOutcome(
        calibration_level="RELATIVE", profile_id=_profile_id(profile_path, profile["metadata"]),
        profile_hash=_profile_hash(Path(profile_path)) if profile_path else None,
        compatibility_status="COMPATIBLE", compatibility_reason=compatibility["reason"],
        spectrum_before_calibration_summary=before_summary,
        relative_psd_db=result["relative_psd_db"], fractional_excess=result["fractional_excess"],
        fractional_uncertainty=result["fractional_uncertainty"],
    )
