"""Receiver identity + exact config snapshot (Fase 6, Fase 65).

The rtl_tcp network protocol has no serial/gain/tuner-type readback (audit
finding: sdr_capture.py never reads a serial, and
rf_chain_characterization.py's own summary literally records
"gain_effective_db": "NOT_READABLE_BY_RTL_TCP_PROTOCOL"). Receiver identity
is therefore an explicit, operator-declared fact tied to which physical
dongle a given rtl_tcp instance was started against - never queried,
never guessed. KNOWN_RECEIVERS below is that declaration, taken verbatim
from the user-provided operational description of this specific ALMITA
unit; it is not something this module can verify on its own.
"""
from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ReceiverIdentity:
    receiver_id: str      # "MAIN" | "RFI_REF"
    serial: str
    tuner: str             # e.g. "R828D" - as declared, not queried
    host: str
    port: int


# Fase 65: MAIN and RFI_REF are two different receivers of the SAME
# uncharacterized kind - never assume their gain numbers/behavior are
# equivalent, and never let a profile built from one apply to the other.
KNOWN_RECEIVERS: Dict[str, ReceiverIdentity] = {
    "MAIN": ReceiverIdentity(receiver_id="MAIN", serial="00000001", tuner="R828D",
                              host="localhost", port=1234),
    "RFI_REF": ReceiverIdentity(receiver_id="RFI_REF", serial="00000002", tuner="R828D",
                                 host="localhost", port=1235),
}


def _current_commit_hash() -> Optional[str]:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                 cwd=Path(__file__).resolve().parent.parent, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _working_tree_dirty() -> Optional[bool]:
    try:
        result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                                 cwd=Path(__file__).resolve().parent.parent, timeout=5)
        return bool(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class ReceiverConfigSnapshot:
    """Fase 6's exact list, one field per line - nothing inferred, nothing
    defaulted silently. Fields that cannot be verified against the real
    hardware today (agc_state, bias_t_state, ppm_correction) are recorded
    as declared/requested values with an explicit verification status, not
    silently presented as confirmed."""
    receiver_id: str
    serial: str
    tuner: str
    center_frequency_hz: float
    sample_rate_hz: float
    gain_requested_db: Optional[float]     # None means AGC/'auto'
    agc_enabled: bool
    bias_t_state: str                       # "ON" | "OFF" | "UNKNOWN"
    rtl_tcp_host: str
    rtl_tcp_port: int
    integration_seconds: float
    fft_size: int
    window: str
    averaging: str
    ppm_correction: Optional[float]
    frequency_offset_hz_status: str         # "UNVERIFIED" until measured against a real reference
    timestamp_utc: str
    host: str
    code_commit: Optional[str]
    working_tree_dirty: Optional[bool]
    deployment_state: str
    temperature_readings: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "receiver_id": self.receiver_id, "serial": self.serial, "tuner": self.tuner,
            "center_frequency_hz": self.center_frequency_hz, "sample_rate_hz": self.sample_rate_hz,
            "gain_requested_db": self.gain_requested_db, "gain_effective_db": "NOT_READABLE_BY_RTL_TCP_PROTOCOL",
            "agc_enabled": self.agc_enabled, "bias_t_state": self.bias_t_state,
            "rtl_tcp_endpoint": f"{self.rtl_tcp_host}:{self.rtl_tcp_port}",
            "integration_seconds": self.integration_seconds, "fft_size": self.fft_size,
            "window": self.window, "averaging": self.averaging,
            "ppm_correction": self.ppm_correction, "frequency_offset_hz_status": self.frequency_offset_hz_status,
            "timestamp_utc": self.timestamp_utc, "host": self.host, "code_commit": self.code_commit,
            "working_tree_dirty": self.working_tree_dirty, "deployment_state": self.deployment_state,
            "temperature_readings": self.temperature_readings,
        }


def build_receiver_config_snapshot(
    receiver_id: str, *, center_frequency_hz: float, sample_rate_hz: float,
    gain_requested_db: Optional[float], bias_t_state: str = "UNKNOWN",
    integration_seconds: float = 2.0, fft_size: int = 8192, window: str = "Hann (numpy.hanning)",
    averaging: str = "median", ppm_correction: Optional[float] = None,
    deployment_state: str = "UNKNOWN", temperature_readings: Optional[Dict[str, Any]] = None,
) -> ReceiverConfigSnapshot:
    if receiver_id not in KNOWN_RECEIVERS:
        raise ValueError(f"unknown receiver_id={receiver_id!r} - must be one of {sorted(KNOWN_RECEIVERS)} "
                          f"(Fase 65: receiver identity is explicit, never inferred)")
    identity = KNOWN_RECEIVERS[receiver_id]
    return ReceiverConfigSnapshot(
        receiver_id=identity.receiver_id, serial=identity.serial, tuner=identity.tuner,
        center_frequency_hz=float(center_frequency_hz), sample_rate_hz=float(sample_rate_hz),
        gain_requested_db=(None if gain_requested_db is None else float(gain_requested_db)),
        agc_enabled=(gain_requested_db is None), bias_t_state=bias_t_state,
        rtl_tcp_host=identity.host, rtl_tcp_port=identity.port,
        integration_seconds=float(integration_seconds), fft_size=int(fft_size), window=window,
        averaging=averaging, ppm_correction=ppm_correction, frequency_offset_hz_status="UNVERIFIED",
        timestamp_utc=datetime.now(timezone.utc).isoformat(), host=socket.gethostname(),
        code_commit=_current_commit_hash(), working_tree_dirty=_working_tree_dirty(),
        deployment_state=deployment_state, temperature_readings=temperature_readings or {},
    )


def configs_are_comparable(a: ReceiverConfigSnapshot, b: ReceiverConfigSnapshot) -> "ComparabilityVerdict":
    from calibration_engine.comparability import compare_calibration_context
    return compare_calibration_context(a, b)
