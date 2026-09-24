"""Physical-reference calibration wizard (50 ohm / HOT / COLD) - tests the RECEIVER CHAIN's quality and, only
when the references genuinely justify it, produces an engineering Y-factor / noise-temperature ESTIMATE. This
module never changes CalibrationLevel: calibration_level.py's CURRENT_LEVEL stays OPERATIONAL_RELATIVE
regardless of what this wizard computes - a Y-factor from an operator-declared HOT/COLD pair (not independently
verified calibrated noise standards) is a real, honest engineering estimate, not a metrology-grade calibration,
and docs/CALIBRATION_MODEL.md's ABSOLUTE_RF_CALIBRATED remains a deliberate future decision, not something this
wizard can reach by accident.

Real chain this is built for: antenna -> Nooelec SAWbird H1 (LNA + filter) -> RTL-SDR Blog V4, rtl_tcp with
Bias-T enabled (-T). This module makes NO assumption that a "50 ohm at room temperature" reference is a "cold"
reference, and NO assumption that HOT/COLD are calibrated physical loads - the operator declares exactly what
they connected, its connection point, and its temperature (or "not measured"); nothing here invents a number.

Reuses the SAME real analysis primitives calibration_operational_realtest.py itself uses
(sample_statistics -> clipping -> bandpass -> cross_capture -> quality) - no science re-derived here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np

from calibration_engine.bandpass import compute_bandpass
from calibration_engine.clipping import ClippingStatus, ClippingThresholds, evaluate_clipping
from calibration_engine.hardware_inspection import (
    VERIFICATION_SERVICE_COMMAND_LINE, inspect_rtl_tcp_service_command_line,
)
from calibration_engine.quality import QualityInputs, evaluate_quality
from calibration_engine.sample_statistics import compute_sample_statistics


class ReferenceKind(str, Enum):
    AMBIENT_50R = "AMBIENT_50R"   # 50 ohm termination at room/ambient temperature - NOT automatically "cold"
    HOT = "HOT"
    COLD = "COLD"


class ConnectionPoint(str, Enum):
    LNA_INPUT = "LNA_INPUT"     # replaces the antenna itself: LNA/SAWbird + cabling + Bias-T + SDR all included
    LNA_OUTPUT = "LNA_OUTPUT"   # bypasses the LNA/SAWbird: only cabling (+ Bias-T if still downstream) + SDR
    SDR_INPUT = "SDR_INPUT"     # directly at the dongle: only the SDR itself


# What each connection point actually includes/excludes in the measurement - shown verbatim to the operator so
# a PASS/FAIL or a Y-factor at that point is never mistaken for characterizing the whole receive chain.
CHAIN_COMPONENTS_INCLUDED: Dict[str, List[str]] = {
    ConnectionPoint.LNA_INPUT.value: ["Nooelec SAWbird H1 (LNA + filter)", "cabling", "Bias-T", "RTL-SDR Blog V4"],
    ConnectionPoint.LNA_OUTPUT.value: ["cabling (post-LNA)", "Bias-T (if still present downstream)", "RTL-SDR Blog V4"],
    ConnectionPoint.SDR_INPUT.value: ["RTL-SDR Blog V4 only"],
}
CHAIN_COMPONENTS_EXCLUDED: Dict[str, List[str]] = {
    ConnectionPoint.LNA_INPUT.value: ["antenna itself (replaced by the reference)"],
    ConnectionPoint.LNA_OUTPUT.value: ["antenna", "Nooelec SAWbird H1 (LNA + filter) - bypassed"],
    ConnectionPoint.SDR_INPUT.value: ["antenna", "Nooelec SAWbird H1 (LNA + filter)", "upstream cabling/Bias-T"],
}

# Fixed order: the 50 ohm ambient termination first (the simplest, always-available reference - the operator
# said they have one for certain), then HOT, then COLD (neither assumed available/calibrated).
WIZARD_REFERENCE_ORDER = (ReferenceKind.AMBIENT_50R.value, ReferenceKind.HOT.value, ReferenceKind.COLD.value)


class WizardStep(str, Enum):
    PREPARE = "PREPARE"                    # operator must physically connect THIS reference next
    STABILIZE = "STABILIZE"                # connection confirmed; operator decides when it's settled - no auto-advance
    RESULT = "RESULT"                      # this reference's captures + evaluation are in; operator reviews, then moves on
    RECONNECT_ANTENNA = "RECONNECT_ANTENNA"
    DONE = "DONE"
    ABORTED = "ABORTED"


@dataclass
class WizardConfig:
    n_captures: int = 5
    capture_seconds: float = 2.0
    stabilize_seconds: float = 20.0        # SUGGESTED settle time shown as a countdown - capture still needs an explicit click
    center_frequency_hz: float = 1_420_405_000.0
    sample_rate_hz: float = 2_400_000.0
    gain_db: float = 40.2
    clipping_rail_hit_fraction: float = ClippingThresholds().clipped_rail_hit_fraction
    stability_rms_fraction_threshold: float = 0.10     # quality.MAX_STABILITY_RMS_FRACTION_GOOD, exposed as editable
    # bandpass.usable_band_fraction below this is treated as the RFI-contamination proxy this wizard has -
    # NOT a direct RFI power/occupancy measurement (that needs rfi_monitor.py's own dedicated receiver).
    rfi_min_usable_band_fraction: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {"n_captures": self.n_captures, "capture_seconds": self.capture_seconds,
                "stabilize_seconds": self.stabilize_seconds, "center_frequency_hz": self.center_frequency_hz,
                "sample_rate_hz": self.sample_rate_hz, "gain_db": self.gain_db,
                "clipping_rail_hit_fraction": self.clipping_rail_hit_fraction,
                "stability_rms_fraction_threshold": self.stability_rms_fraction_threshold,
                "rfi_min_usable_band_fraction": self.rfi_min_usable_band_fraction}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WizardConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ReferenceConfig:
    kind: str
    connection_point: str
    temperature_c: Optional[float]           # None -> "not measured"; never fabricated
    temperature_source: str                  # e.g. "not measured", "declared by operator", "measured with <instrument>"
    confirmed_utc: str

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "connection_point": self.connection_point, "temperature_c": self.temperature_c,
                "temperature_source": self.temperature_source, "confirmed_utc": self.confirmed_utc,
                "chain_components_included": CHAIN_COMPONENTS_INCLUDED[self.connection_point],
                "chain_components_excluded": CHAIN_COMPONENTS_EXCLUDED[self.connection_point]}


def bias_t_known_facts(port: int = 1234) -> Dict[str, Any]:
    """Exactly what is REALLY known about Bias-T right now - never inflated. rtl_tcp's -T flag only says the
    service was TOLD to enable it at startup (VERIFIED_BY_SERVICE_COMMAND_LINE); the protocol has no
    voltage/current readback at all, so "-T present" is NOT proof of measured voltage or current at the
    connector. Also carries the standing safety instruction: power down before touching connectors."""
    cmdline = inspect_rtl_tcp_service_command_line(port)
    enabled_flag = cmdline.parsed.get("bias_t_enabled") if cmdline.found else None
    if not cmdline.found:
        note = "rtl_tcp service command line not found - Bias-T state unknown"
    elif enabled_flag:
        note = "rtl_tcp was started with -T (VERIFIED_BY_SERVICE_COMMAND_LINE)"
    else:
        note = "rtl_tcp -T flag not found in the running service's command line"
    return {
        "flag_present_in_service_command_line": enabled_flag,
        "verification": VERIFICATION_SERVICE_COMMAND_LINE if cmdline.found else "NOT_FOUND",
        "voltage_measured": None, "current_measured": None,
        "note": note + " - this does NOT confirm actual voltage or current is present at the connector; "
                "rtl_tcp's protocol has no Bias-T readback at all.",
        "safety": "Before touching, swapping or disconnecting any connector on this chain: stop the capture/"
                  "service that is powering the Bias-T feed first. Do not assume a connector is safe to handle "
                  "live just because no capture is currently running - verify the service is actually stopped.",
    }


def evaluate_reference_captures(iq_arrays: List[np.ndarray], sample_rate_hz: float, center_frequency_hz: float,
                                config: WizardConfig) -> Dict[str, Any]:
    """Same real analysis calibration_operational_realtest.py itself uses, applied to one reference's captures.
    iq_arrays: raw interleaved uint8 arrays - real captures OR calibration_engine.simulation's
    simulate_capture_iq() output for offline testing; this function cannot tell the difference, by design (the
    same reason CALIBRATE's own SIMULATION panel can exercise this real pipeline with no hardware)."""
    from calibration_engine.cross_capture import compute_cross_capture_summary
    if len(iq_arrays) < 2:
        raise ValueError("at least 2 captures are required (cross-capture comparison needs >= 2)")
    thresholds = ClippingThresholds(clipped_rail_hit_fraction=config.clipping_rail_hit_fraction)
    per_capture, rms_values, normalized_bandpasses, clipping_statuses, dc_masks, relative_powers = [], [], [], [], [], []
    for i, iq in enumerate(iq_arrays):
        stats = compute_sample_statistics(iq)
        clipping = evaluate_clipping(stats, thresholds)
        bandpass = compute_bandpass(iq, sample_rate_hz, center_frequency_hz)
        # AC-coupled power (mean-subtracted by construction) - gain_sweep.py's own formula/rationale: raw RMS of
        # ADC codes is dominated by the ~127.5 DC pedestal and would make a real HOT-vs-COLD power difference
        # look falsely flat.
        relative_power = (stats.std_i ** 2 + stats.std_q ** 2) / 2.0
        per_capture.append({"index": i, "sample_statistics": stats.to_dict(), "clipping": clipping.to_dict(),
                            "usable_band_fraction": bandpass.usable_band_fraction,
                            "dc_half_width_hz": bandpass.dc_half_width_hz, "relative_digital_power": relative_power})
        rms_values.append(stats.rms); normalized_bandpasses.append(bandpass.normalized_bandpass)
        clipping_statuses.append(clipping.status.value); dc_masks.append(bandpass.dc_mask)
        relative_powers.append(relative_power)
    cross_capture = compute_cross_capture_summary(rms_values, normalized_bandpasses, clipping_statuses, dc_masks)
    worst_clipping = max(clipping_statuses, key=lambda s: ["OK", "WARNING", "CLIPPED", "UNKNOWN"].index(s))
    min_usable_band = min(r["usable_band_fraction"] for r in per_capture)
    quality = evaluate_quality(QualityInputs(
        metadata_complete=True, clipping_status=ClippingStatus(worst_clipping), valid_sample_fraction=1.0,
        stability_power_rms_fraction=cross_capture.rms_variation_fraction, usable_band_fraction=min_usable_band,
        temperature_range_c=None, rfi_contaminated_fraction=None))
    rfi_flag = min_usable_band < config.rfi_min_usable_band_fraction
    verdict = {"GOOD": "PASS", "MARGINAL": "PARTIAL", "BAD": "FAIL", "INCONCLUSIVE": "FAIL"}[quality.verdict]
    reasons = list(quality.reasons)
    if rfi_flag:
        reasons.append(f"usable_band_fraction={min_usable_band:.2f} below the configured RFI-proxy threshold "
                       f"{config.rfi_min_usable_band_fraction:.2f} (a bandpass-coverage proxy, NOT a direct RFI "
                       "power measurement)")
        if verdict == "PASS":
            verdict = "PARTIAL"
    return {"per_capture": per_capture, "cross_capture": cross_capture.to_dict(), "quality": quality.to_dict(),
            "mean_relative_digital_power": float(np.mean(relative_powers)), "rfi_flag": rfi_flag,
            "verdict": verdict, "verdict_reasons": reasons}


@dataclass
class YFactorResult:
    physical_units_justified: bool
    reason: str
    y_factor: Optional[float] = None
    noise_temperature_k: Optional[float] = None
    characterizes: Optional[str] = None
    caveats: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"physical_units_justified": self.physical_units_justified, "reason": self.reason,
                "y_factor": self.y_factor, "noise_temperature_k": self.noise_temperature_k,
                "characterizes": self.characterizes, "caveats": self.caveats}


def compute_y_factor(hot_ref: ReferenceConfig, hot_result: Dict[str, Any],
                     cold_ref: ReferenceConfig, cold_result: Dict[str, Any]) -> YFactorResult:
    """Y = P_hot / P_cold (mean relative digital power, AC-coupled) -> T_e = (T_hot - Y*T_cold) / (Y - 1), the
    standard Y-factor noise-temperature estimate. Refuses (physical_units_justified=False, explicit reason)
    unless BOTH temperatures are real declared numbers, both references were connected at the SAME point, T_hot
    is genuinely above T_cold, and the measured power actually increased from COLD to HOT with a physically
    sane result - never fabricates a number to fill in a gap."""
    if hot_ref.temperature_c is None or cold_ref.temperature_c is None:
        return YFactorResult(False, "HOT and/or COLD reference temperature was declared 'not measured' - a "
                             "Y-factor/noise-temperature figure would be fabricated without both real "
                             "temperatures; reporting OPERATIONAL_RELATIVE only")
    if hot_ref.connection_point != cold_ref.connection_point:
        return YFactorResult(False, f"HOT was connected at {hot_ref.connection_point} but COLD at "
                             f"{cold_ref.connection_point} - not the same reference plane, so a Y-factor between "
                             "them would not characterize one well-defined part of the chain")
    t_hot_k, t_cold_k = hot_ref.temperature_c + 273.15, cold_ref.temperature_c + 273.15
    if t_hot_k <= t_cold_k:
        return YFactorResult(False, f"declared HOT temperature ({hot_ref.temperature_c:.1f} C) is not above "
                             f"COLD ({cold_ref.temperature_c:.1f} C) - Y-factor requires T_hot > T_cold")
    p_hot = hot_result["mean_relative_digital_power"]
    p_cold = cold_result["mean_relative_digital_power"]
    if p_cold <= 0 or p_hot <= p_cold:
        return YFactorResult(False, f"measured relative power did not increase from COLD ({p_cold:.4g}) to HOT "
                             f"({p_hot:.4g}) - Y-factor requires P_hot > P_cold; check the connections and that "
                             "the same gain was used for both")
    y = p_hot / p_cold
    t_e = (t_hot_k - y * t_cold_k) / (y - 1.0)
    if t_e <= 0:
        return YFactorResult(False, f"computed noise temperature is non-physical (T_e={t_e:.1f} K <= 0) from "
                             f"Y={y:.3f} - the measured power ratio and declared temperatures are not "
                             "self-consistent; treat the setup/references as suspect, not this number as real")
    included = CHAIN_COMPONENTS_INCLUDED[hot_ref.connection_point]
    return YFactorResult(
        True, f"Y={y:.3f} from mean relative digital power (P_hot/P_cold), T_hot={t_hot_k:.1f} K, "
              f"T_cold={t_cold_k:.1f} K", y_factor=y, noise_temperature_k=t_e,
        characterizes=f"the chain downstream of {hot_ref.connection_point} only ({', '.join(included)})",
        caveats=[
            "single-session, single-pair Y-factor - not a repeated/averaged metrology measurement",
            "HOT/COLD are whatever the operator physically connected and declared, not independently verified "
            "calibrated noise standards",
            f"characterizes only {', '.join(included)} - never the antenna or anything upstream of the "
            "reference plane",
            "engineering estimate only: CalibrationLevel remains OPERATIONAL_RELATIVE - REDUCE must NOT apply "
            "this as an absolute flux/Kelvin conversion",
        ])
