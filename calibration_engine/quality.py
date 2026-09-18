"""OperationalCalibrationQuality (Fase 33, 74) - GOOD/MARGINAL/BAD/INCONCLUSIVE
from explicit, objective rules. GOOD never means "the program finished" -
see evaluate_quality()'s docstring for the exact requirements.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from calibration_engine.clipping import ClippingStatus


@dataclass
class QualityInputs:
    metadata_complete: bool
    clipping_status: ClippingStatus
    valid_sample_fraction: float          # fraction of expected captures/samples that were actually valid
    stability_power_rms_fraction: Optional[float]     # from stability.StabilityResult, if available
    usable_band_fraction: Optional[float]
    temperature_range_c: Optional[List[float]]
    rfi_contaminated_fraction: Optional[float]        # fraction of band flagged by RFI context, if known


@dataclass
class OperationalCalibrationQuality:
    verdict: str          # "GOOD" | "MARGINAL" | "BAD" | "INCONCLUSIVE"
    reasons: List[str]
    inputs: QualityInputs

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict, "reasons": self.reasons,
            "inputs": {
                "metadata_complete": self.inputs.metadata_complete,
                "clipping_status": self.inputs.clipping_status.value,
                "valid_sample_fraction": self.inputs.valid_sample_fraction,
                "stability_power_rms_fraction": self.inputs.stability_power_rms_fraction,
                "usable_band_fraction": self.inputs.usable_band_fraction,
                "temperature_range_c": self.inputs.temperature_range_c,
                "rfi_contaminated_fraction": self.inputs.rfi_contaminated_fraction,
            },
        }


# Documented, not tuned to make any specific historical result look good
# (Fase 8's same principle applied here) - these are round, defensible
# numbers: "half the band usable" and "power varying more than the signal
# itself over the run" are self-evidently bad regardless of gain choice.
MIN_VALID_SAMPLE_FRACTION_GOOD = 0.9
MIN_USABLE_BAND_FRACTION_GOOD = 0.5
MAX_STABILITY_RMS_FRACTION_GOOD = 0.10
MAX_STABILITY_RMS_FRACTION_MARGINAL = 0.30
MIN_VALID_SAMPLE_FRACTION_MARGINAL = 0.5


def evaluate_quality(inputs: QualityInputs) -> OperationalCalibrationQuality:
    """GOOD requires ALL of: complete metadata, clipping OK, sufficient
    valid samples, acceptable stability, sufficient usable band, no severe
    anomaly. INCONCLUSIVE when required data is simply missing (never
    silently treated as GOOD or BAD)."""
    reasons: List[str] = []

    if not inputs.metadata_complete:
        return OperationalCalibrationQuality("INCONCLUSIVE", ["metadata incomplete - cannot evaluate quality"], inputs)
    if inputs.stability_power_rms_fraction is None or inputs.usable_band_fraction is None:
        return OperationalCalibrationQuality(
            "INCONCLUSIVE", ["stability or bandpass analysis unavailable - insufficient data for a verdict"], inputs)

    if inputs.clipping_status == ClippingStatus.CLIPPED:
        reasons.append("clipping detected")
        return OperationalCalibrationQuality("BAD", reasons, inputs)
    if inputs.clipping_status == ClippingStatus.UNKNOWN:
        reasons.append("clipping status unknown")
        return OperationalCalibrationQuality("INCONCLUSIVE", reasons, inputs)

    if inputs.valid_sample_fraction < MIN_VALID_SAMPLE_FRACTION_MARGINAL:
        reasons.append(f"valid_sample_fraction={inputs.valid_sample_fraction:.2f} below "
                        f"{MIN_VALID_SAMPLE_FRACTION_MARGINAL}")
        return OperationalCalibrationQuality("BAD", reasons, inputs)

    severe = False
    if inputs.clipping_status == ClippingStatus.WARNING:
        reasons.append("clipping headroom warning")
        severe = True
    if inputs.stability_power_rms_fraction > MAX_STABILITY_RMS_FRACTION_MARGINAL:
        reasons.append(f"stability_power_rms_fraction={inputs.stability_power_rms_fraction:.3f} exceeds "
                        f"{MAX_STABILITY_RMS_FRACTION_MARGINAL}")
        return OperationalCalibrationQuality("BAD", reasons, inputs)
    if inputs.usable_band_fraction < MIN_USABLE_BAND_FRACTION_GOOD:
        reasons.append(f"usable_band_fraction={inputs.usable_band_fraction:.2f} below "
                        f"{MIN_USABLE_BAND_FRACTION_GOOD}")
        severe = True
    if inputs.stability_power_rms_fraction > MAX_STABILITY_RMS_FRACTION_GOOD:
        reasons.append(f"stability_power_rms_fraction={inputs.stability_power_rms_fraction:.3f} exceeds "
                        f"{MAX_STABILITY_RMS_FRACTION_GOOD} (GOOD threshold)")
        severe = True
    if inputs.valid_sample_fraction < MIN_VALID_SAMPLE_FRACTION_GOOD:
        reasons.append(f"valid_sample_fraction={inputs.valid_sample_fraction:.2f} below "
                        f"{MIN_VALID_SAMPLE_FRACTION_GOOD} (GOOD threshold)")
        severe = True
    if inputs.rfi_contaminated_fraction is not None and inputs.rfi_contaminated_fraction > 0.5:
        reasons.append(f"rfi_contaminated_fraction={inputs.rfi_contaminated_fraction:.2f} exceeds 0.5")
        severe = True

    if severe:
        return OperationalCalibrationQuality("MARGINAL", reasons, inputs)
    if not reasons:
        reasons.append("all criteria within GOOD thresholds")
    return OperationalCalibrationQuality("GOOD", reasons, inputs)
