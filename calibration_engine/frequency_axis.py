"""Frequency axis validation (Fase 21-23) - fully offline, no hardware.

Separates two DIFFERENT claims that are easy to conflate:

- frequency_axis_correct: the software axis (center + sample_rate -> FFT
  bin frequencies, DC bin, edges, ordering) is INTERNALLY consistent.
  Provable by software alone - this module does exactly that.
- absolute_frequency_calibrated: the axis matches a real physical
  frequency reference (oscillator PPM measured against something known).
  Requires a real reference this project does not have yet - always
  UNVERIFIED here, never inferred from the axis being internally correct.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np


@dataclass
class FrequencyAxisValidation:
    validated: bool
    reasons: list
    center_frequency_hz: float
    sample_rate_hz: float
    fft_size: int
    bin_spacing_hz: float
    dc_bin_index: int
    dc_bin_frequency_hz: float
    lowest_frequency_hz: float
    highest_frequency_hz: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frequency_axis_correct": self.validated, "reasons": self.reasons,
            "center_frequency_hz": self.center_frequency_hz, "sample_rate_hz": self.sample_rate_hz,
            "fft_size": self.fft_size, "bin_spacing_hz": self.bin_spacing_hz,
            "dc_bin_index": self.dc_bin_index, "dc_bin_frequency_hz": self.dc_bin_frequency_hz,
            "lowest_frequency_hz": self.lowest_frequency_hz, "highest_frequency_hz": self.highest_frequency_hz,
            "absolute_frequency_calibrated": False,
            "absolute_frequency_status": "UNVERIFIED - requires an independent physical/astronomical "
                                          "frequency reference this project does not have yet (Fase 22)",
        }


def validate_frequency_axis(center_frequency_hz: float, sample_rate_hz: float, fft_size: int) -> FrequencyAxisValidation:
    """Fully offline, exact arithmetic checks - no capture required."""
    reasons = []
    frequency = center_frequency_hz + np.fft.fftshift(np.fft.fftfreq(fft_size, 1.0 / sample_rate_hz))
    bin_spacing = sample_rate_hz / fft_size
    diffs = np.diff(frequency)
    if not np.allclose(diffs, bin_spacing, rtol=1e-9):
        reasons.append(f"bin spacing not uniform: expected {bin_spacing}, found range "
                        f"[{diffs.min()}, {diffs.max()}]")
    if not np.all(diffs > 0):
        reasons.append("frequency axis is not monotonically increasing after fftshift")
    dc_bin = int(np.argmin(np.abs(frequency - center_frequency_hz)))
    if abs(frequency[dc_bin] - center_frequency_hz) > bin_spacing / 2 + 1e-6:
        reasons.append(f"no bin found within half a bin width of center_frequency_hz "
                        f"(closest is {frequency[dc_bin]}, off by {abs(frequency[dc_bin] - center_frequency_hz)})")
    expected_dc_bin = fft_size // 2
    if dc_bin != expected_dc_bin:
        reasons.append(f"DC bin at index {dc_bin}, expected {expected_dc_bin} for an fftshift'd axis of size {fft_size}")
    span = frequency[-1] - frequency[0]
    expected_span = sample_rate_hz - bin_spacing
    if not np.isclose(span, expected_span, rtol=1e-9):
        reasons.append(f"total span {span} does not match expected {expected_span} for sample_rate {sample_rate_hz}")
    return FrequencyAxisValidation(
        validated=(len(reasons) == 0), reasons=reasons, center_frequency_hz=center_frequency_hz,
        sample_rate_hz=sample_rate_hz, fft_size=fft_size, bin_spacing_hz=bin_spacing,
        dc_bin_index=dc_bin, dc_bin_frequency_hz=float(frequency[dc_bin]),
        lowest_frequency_hz=float(frequency[0]), highest_frequency_hz=float(frequency[-1]),
    )


@dataclass
class PPMStatus:
    ppm_correction_configured: Optional[float]
    exposed_by_protocol: bool
    status: str

    def to_dict(self) -> Dict[str, Any]:
        return {"ppm_correction_configured": self.ppm_correction_configured,
                "exposed_by_protocol": self.exposed_by_protocol, "status": self.status}


def audit_ppm_configuration(ppm_correction: Optional[float]) -> PPMStatus:
    """Fase 23: audit finding - rtl_tcp's command 0x05 sets PPM correction,
    but sdr_capture.py never calls it (grep audited, no send(0x05, ...)
    anywhere in this repo), so any PPM correction is unset/whatever the
    dongle's own default is. Status is UNVERIFIED until measured against a
    real reference regardless of what value is configured - never adjusted
    automatically here (Fase 23, Fase 49)."""
    return PPMStatus(
        ppm_correction_configured=ppm_correction, exposed_by_protocol=False,
        status="UNVERIFIED - not set by this codebase's current sdr_capture.py; no PPM measurement "
               "against a real reference exists yet",
    )
