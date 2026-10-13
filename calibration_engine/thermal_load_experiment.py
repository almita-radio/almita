"""Thermal-load Y-factor / noise-temperature experiment - NOT wired into the active CALIBRATE web wizard.

This is the original physical-reference engine (50 ohm ambient / HOT / COLD, Y-factor, noise temperature)
that calibrate_reference_wizard.py used before it was rebuilt around a three-reference HI-line spectral-
contrast procedure (50 ohm / HI ALTO / HI BAJO - see calibration_engine/reference_wizard.py's own module
docstring for the current, active flow and the scientific reasoning for the change). It is kept here, unused
by anything else in this project right now, because a real HOT/COLD thermal-load Y-factor measurement remains
a genuinely valuable FUTURE engineering experiment for this receiver chain - it is just a different question
(an absolute-ish noise-temperature ESTIMATE from two physical thermal loads) from what the active wizard now
measures (a RELATIVE HI-line spectral contrast between two sky zones), and the two must never be mixed into
one result: a Y-factor here says nothing about the active wizard's HI ALTO/HI BAJO contrast, and vice versa.

Reuses the SAME real analysis primitives calibration_operational_realtest.py and the active wizard's
evaluate_reference_captures() use (sample_statistics -> clipping -> bandpass -> cross_capture -> quality) -
still no science re-derived here, same as before.

Real chain this is built for: antenna -> Nooelec SAWbird H1 (LNA + filter) -> RTL-SDR Blog V4, rtl_tcp with
Bias-T enabled (-T). This module makes NO assumption that a "50 ohm at room temperature" reference is a "cold"
reference, and NO assumption that HOT/COLD are calibrated physical loads - the operator would declare exactly
what they connected, its connection point, and its temperature (or "not measured"); nothing here invents a
number. calibration_level.py's CURRENT_LEVEL stays OPERATIONAL_RELATIVE regardless of what this module
computes - a Y-factor from an operator-declared HOT/COLD pair (not independently verified calibrated noise
standards) would be a real, honest engineering estimate, not a metrology-grade calibration.

To actually use this again: a future, explicitly separate web/CLI flow would need to be built around it (the
active calibrate_reference_wizard.py/console/calibrate.js code does not call anything in this file) - do not
wire it into the active wizard's state machine or its RESULT/comparison step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from calibration_engine.reference_wizard import CHAIN_COMPONENTS_INCLUDED, ConnectionPoint


class ThermalReferenceKind(str, Enum):
    HOT = "HOT"
    COLD = "COLD"


@dataclass
class ThermalReferenceConfig:
    kind: str
    connection_point: str
    temperature_c: Optional[float]           # None -> "not measured"; never fabricated
    temperature_source: str                  # e.g. "not measured", "declared by operator", "measured with <instrument>"
    confirmed_utc: str

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "connection_point": self.connection_point, "temperature_c": self.temperature_c,
                "temperature_source": self.temperature_source, "confirmed_utc": self.confirmed_utc,
                "chain_components_included": CHAIN_COMPONENTS_INCLUDED[self.connection_point]}


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


def compute_y_factor(hot_ref: ThermalReferenceConfig, hot_result: Dict[str, Any],
                     cold_ref: ThermalReferenceConfig, cold_result: Dict[str, Any]) -> YFactorResult:
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
            "NOT wired into the active CALIBRATE web wizard - a separate, deliberate future step would need to "
            "call this function; it does not run as part of the 50 ohm / HI ALTO / HI BAJO flow",
        ])
