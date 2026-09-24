"""ALMITA CALIBRATE reference wizard: a three-reference procedure to measure the RELATIVE HI-line spectral
contrast this receiver chain can actually deliver, from the real point of connection outward.

    1. 50 OHM (AMBIENT_50R): a physical 50 ohm terminator, connected by hand at a declared point in the chain -
       characterizes the instrument's own response (clipping, usable band, stability) from that point, no sky
       involved. Exactly the original physical-reference test this module always did.
    2. HI ALTO: a real sky zone where the HI4PI map predicts a strong line - a real GOTO + capture.
    3. HI BAJO: a real sky zone where the HI4PI map predicts a weak line - a real GOTO + capture, for
       differential comparison against HI ALTO.

WHAT THIS MEASURES: the CONTRAST between HI ALTO's and HI BAJO's measured HI-line spectra (see
calibration_engine/spectral_contrast.py) - a RELATIVE result. It never computes total SDR power as if it
predicted the line, and never treats the HI4PI column density as anything but orientation for WHERE to point
(see calibration_engine/hi_reference_selection.py's own docstring on this).

WHAT THIS NEVER COMPUTES: a Y-factor, a noise temperature, kelvin, or an absolute RF gain. calibration_level.py's
CURRENT_LEVEL stays OPERATIONAL_RELATIVE. The earlier HOT/COLD thermal-load Y-factor engine this module used to
contain is kept, deliberately separate and NOT wired into this flow, in
calibration_engine/thermal_load_experiment.py - see that module's own docstring.

SAFETY ORDER (structural, not just a warning): the wizard's own state machine cannot reach ANY HI step (PLAN_HI
onward) until the 50 OHM step has been settled (done or explicitly skipped) AND the operator has explicitly
confirmed the antenna is connected. The mount is never moved while a terminator could still be connected.

Real chain this is built for: antenna -> Nooelec SAWbird H1 (LNA + filter) -> RTL-SDR Blog V4, rtl_tcp with
Bias-T enabled (-T). Reuses the SAME real analysis primitives calibration_operational_realtest.py itself uses
for instrument-quality diagnostics (sample_statistics -> clipping -> bandpass -> cross_capture -> quality) on
every reference's captures, and alignment_engine.hi's own real HI-line spectral pipeline
(calibration_engine/spectral_contrast.py) for the HI ALTO/HI BAJO measurement itself - no science re-derived
here, in either case.
"""
from __future__ import annotations

from dataclasses import dataclass
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
    AMBIENT_50R = "AMBIENT_50R"   # 50 ohm termination at room/ambient temperature - a physical instrument test
    HI_ALTO = "HI_ALTO"           # real sky zone, HI4PI-predicted strong line - orientation only, see module docstring
    HI_BAJO = "HI_BAJO"           # real sky zone, HI4PI-predicted weak line - differential comparison against HI_ALTO


class ConnectionPoint(str, Enum):
    LNA_INPUT = "LNA_INPUT"     # replaces the antenna itself: LNA/SAWbird + cabling + Bias-T + SDR all included
    LNA_OUTPUT = "LNA_OUTPUT"   # bypasses the LNA/SAWbird: only cabling (+ Bias-T if still downstream) + SDR
    SDR_INPUT = "SDR_INPUT"     # directly at the dongle: only the SDR itself


# What each connection point actually includes/excludes in the measurement - shown verbatim to the operator so
# the 50 OHM result at that point is never mistaken for characterizing the whole receive chain (including the
# antenna, which HI ALTO/HI BAJO do characterize, together with everything upstream of it too).
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

# Fixed order: the 50 ohm ambient termination first (a required physical-safety precondition - see module
# docstring's SAFETY ORDER - before anything is allowed to move), then HI ALTO, then HI BAJO.
WIZARD_REFERENCE_ORDER = (ReferenceKind.AMBIENT_50R.value, ReferenceKind.HI_ALTO.value, ReferenceKind.HI_BAJO.value)


class WizardStep(str, Enum):
    PREPARE_50R = "PREPARE_50R"            # operator must physically connect the 50 ohm termination next
    STABILIZE_50R = "STABILIZE_50R"        # connection confirmed; operator decides when it's settled - no auto-advance
    RESULT_50R = "RESULT_50R"              # 50 ohm captures + evaluation are in; operator reviews, then moves on
    RECONNECT_ANTENNA = "RECONNECT_ANTENNA"  # must be explicitly confirmed before ANY HI step is reachable
    PLAN_HI = "PLAN_HI"                    # HI ALTO/HI BAJO candidates proposed, awaiting operator approval
    READY_HI_ALTO = "READY_HI_ALTO"        # approved; awaiting typed MOVE + real GOTO+capture
    RESULT_HI_ALTO = "RESULT_HI_ALTO"
    READY_HI_BAJO = "READY_HI_BAJO"
    RESULT_HI_BAJO = "RESULT_HI_BAJO"
    DONE = "DONE"                          # spectral contrast computed, draft profile written
    ABORTED = "ABORTED"


@dataclass
class WizardConfig:
    n_captures: int = 5
    capture_seconds: float = 2.0
    stabilize_seconds: float = 20.0        # SUGGESTED settle time for the 50 ohm step - capture still needs an explicit click
    hi_settle_seconds: float = 2.0         # REAL settle after a GOTO, before the HI captures start (mount/tracking to stop moving)
    center_frequency_hz: float = 1_420_405_000.0
    sample_rate_hz: float = 2_400_000.0
    gain_db: float = 40.2
    clipping_rail_hit_fraction: float = ClippingThresholds().clipped_rail_hit_fraction
    stability_rms_fraction_threshold: float = 0.10     # quality.MAX_STABILITY_RMS_FRACTION_GOOD, exposed as editable
    # bandpass.usable_band_fraction below this is treated as the RFI-contamination proxy this wizard has -
    # NOT a direct RFI power/occupancy measurement (that needs rfi_monitor.py's own dedicated receiver).
    rfi_min_usable_band_fraction: float = 0.5
    min_elevation_deg: float = 20.0        # HI candidate selection + real altitude-hold check before any GOTO
    beam_fwhm_deg: float = 20.0            # HI candidate selection's beam-averaging radius (see hi_reference_selection.py)
    contrast_significance_threshold: float = 3.0   # sigma - see spectral_contrast.compute_spectral_contrast()

    def to_dict(self) -> Dict[str, Any]:
        return {"n_captures": self.n_captures, "capture_seconds": self.capture_seconds,
                "stabilize_seconds": self.stabilize_seconds, "hi_settle_seconds": self.hi_settle_seconds,
                "center_frequency_hz": self.center_frequency_hz, "sample_rate_hz": self.sample_rate_hz,
                "gain_db": self.gain_db, "clipping_rail_hit_fraction": self.clipping_rail_hit_fraction,
                "stability_rms_fraction_threshold": self.stability_rms_fraction_threshold,
                "rfi_min_usable_band_fraction": self.rfi_min_usable_band_fraction,
                "min_elevation_deg": self.min_elevation_deg, "beam_fwhm_deg": self.beam_fwhm_deg,
                "contrast_significance_threshold": self.contrast_significance_threshold}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WizardConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def hi_hold_seconds(self) -> float:
        """How long a real GOTO'd point is occupied for one HI reference: settle + all its captures back to
        back, no further GOTO in between - what the pre-GOTO altitude-hold check (hi_reference_selection.
        point_stays_above_elevation) must clear."""
        return self.hi_settle_seconds + self.n_captures * self.capture_seconds


@dataclass
class PhysicalReferenceConfig:
    """The 50 ohm termination only - HI ALTO/HI BAJO are real sky positions, not a hand-connected reference;
    see HISelectionConfirmation below for those."""
    kind: str
    connection_point: str
    confirmed_utc: str

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "connection_point": self.connection_point, "confirmed_utc": self.confirmed_utc,
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
                  "live just because no capture is currently running - verify the service is actually stopped. "
                  "Never move the mount while the 50 ohm terminator could still be connected in place of the antenna.",
    }


def evaluate_reference_captures(iq_arrays: List[np.ndarray], sample_rate_hz: float, center_frequency_hz: float,
                                config: WizardConfig) -> Dict[str, Any]:
    """Same real analysis calibration_operational_realtest.py itself uses, applied to one reference's captures -
    an INSTRUMENT-quality diagnostic (clipping, usable band / RFI proxy, cross-capture stability), independent
    of and orthogonal to the HI-line spectral-contrast measurement (calibration_engine.spectral_contrast) run
    separately on the same HI ALTO/HI BAJO capture files. iq_arrays: raw interleaved uint8 arrays - real
    captures OR calibration_engine.simulation's simulate_capture_iq() output for offline testing; this function
    cannot tell the difference, by design (the same reason CALIBRATE's own SIMULATION panel can exercise this
    real pipeline with no hardware)."""
    from calibration_engine.cross_capture import compute_cross_capture_summary
    if len(iq_arrays) < 2:
        raise ValueError("at least 2 captures are required (cross-capture comparison needs >= 2)")
    thresholds = ClippingThresholds(clipped_rail_hit_fraction=config.clipping_rail_hit_fraction)
    per_capture, rms_values, normalized_bandpasses, clipping_statuses, dc_masks, relative_powers = [], [], [], [], [], []
    for i, iq in enumerate(iq_arrays):
        stats = compute_sample_statistics(iq)
        clipping = evaluate_clipping(stats, thresholds)
        bandpass = compute_bandpass(iq, sample_rate_hz, center_frequency_hz)
        # AC-coupled power (mean-subtracted by construction) - an instrument-quality/headroom diagnostic only;
        # NEVER used here or anywhere downstream as a stand-in for the real HI-line spectral metric.
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
