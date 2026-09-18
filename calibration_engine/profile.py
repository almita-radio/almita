"""CalibrationProfile: versioned, traceable, review-gated (Fase 35-36, 42,
62). Every profile this pass can produce is DRAFT - there is no code path
in this package that sets ACTIVE. Activation is a future, explicit,
separate decision (Fase 62), never automatic.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_state import atomic_write_json


class ProfileStatus:
    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


def _current_commit_hash() -> Optional[str]:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                 cwd=Path(__file__).resolve().parent.parent, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class OperationalEnvelope:
    """Fase 42: what "normal" looked like when this profile was built - so
    a future observation can be flagged OUTSIDE_VALIDATED_ENVELOPE instead
    of silently assumed still-valid forever."""
    gain_range_tested_db: List[float]
    temperature_range_observed_c: Optional[List[float]]
    clipping_maximum_observed: str          # ClippingStatus value seen among the accepted sessions
    stability_rms_fraction_expected_max: float
    usable_band_fraction: float
    known_artifacts: List[Dict[str, Any]]   # from bandpass.recommend_mask()'s candidate_regions, human-reviewed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gain_range_tested_db": self.gain_range_tested_db,
            "temperature_range_observed_c": self.temperature_range_observed_c,
            "clipping_maximum_observed": self.clipping_maximum_observed,
            "stability_rms_fraction_expected_max": self.stability_rms_fraction_expected_max,
            "usable_band_fraction": self.usable_band_fraction, "known_artifacts": self.known_artifacts,
        }


class EnvelopeStatus:
    WITHIN_ENVELOPE = "WITHIN_ENVELOPE"
    MARGINAL = "MARGINAL"
    OUTSIDE_ENVELOPE = "OUTSIDE_ENVELOPE"
    UNKNOWN = "UNKNOWN"


def evaluate_envelope(envelope: OperationalEnvelope, *, gain_db: Optional[float] = None,
                       temperature_c: Optional[float] = None, stability_rms_fraction: Optional[float] = None) -> str:
    """Fase 42/67: does NOT abort anything in V1 - callers decide what to
    do with the status. Returns UNKNOWN when the relevant data is missing
    rather than guessing WITHIN_ENVELOPE by default (fail-informative, not
    fail-open)."""
    if gain_db is None and temperature_c is None and stability_rms_fraction is None:
        return EnvelopeStatus.UNKNOWN
    outside = []
    marginal = []
    if gain_db is not None and envelope.gain_range_tested_db:
        lo, hi = min(envelope.gain_range_tested_db), max(envelope.gain_range_tested_db)
        if gain_db < lo or gain_db > hi:
            outside.append("gain")
    if temperature_c is not None and envelope.temperature_range_observed_c:
        lo, hi = envelope.temperature_range_observed_c
        if temperature_c < lo or temperature_c > hi:
            outside.append("temperature")
    if stability_rms_fraction is not None:
        if stability_rms_fraction > envelope.stability_rms_fraction_expected_max * 2:
            outside.append("stability")
        elif stability_rms_fraction > envelope.stability_rms_fraction_expected_max:
            marginal.append("stability")
    if outside:
        return EnvelopeStatus.OUTSIDE_ENVELOPE
    if marginal:
        return EnvelopeStatus.MARGINAL
    return EnvelopeStatus.WITHIN_ENVELOPE


@dataclass
class CalibrationProfile:
    profile_id: str
    created_utc: str
    code_commit: Optional[str]
    source_calibration_sessions: List[str]
    receiver_id: str
    receiver_serial: str
    conditions: Dict[str, Any]        # deployment_state, temperature range, bias_t, etc at build time
    status: str
    recommended_gain_db: Optional[float]
    recommended_usable_band: Optional[Dict[str, Any]]
    known_masks: List[Dict[str, Any]]
    warmup_recommendation: Optional[Dict[str, Any]]
    envelope: OperationalEnvelope
    limitations: List[str]
    validity_note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id, "created_utc": self.created_utc, "code_commit": self.code_commit,
            "source_calibration_sessions": self.source_calibration_sessions, "receiver_id": self.receiver_id,
            "receiver_serial": self.receiver_serial, "conditions": self.conditions, "status": self.status,
            "recommended_gain_db": self.recommended_gain_db, "recommended_usable_band": self.recommended_usable_band,
            "known_masks": self.known_masks, "warmup_recommendation": self.warmup_recommendation,
            "operational_envelope": self.envelope.to_dict(), "limitations": self.limitations,
            "validity_note": self.validity_note, "active": False,
            "note": "DRAFT until a human reviews it (Fase 62) - never auto-applied to capture pipeline "
                    "or science (Fase 61, Fase 49).",
        }


def new_profile_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"PROFILE-{now.strftime('%Y%m%d-%H%M%S')}"


def build_draft_profile(*, receiver_id: str, receiver_serial: str, source_calibration_sessions: List[str],
                         conditions: Dict[str, Any], recommended_gain_db: Optional[float],
                         recommended_usable_band: Optional[Dict[str, Any]], known_masks: List[Dict[str, Any]],
                         warmup_recommendation: Optional[Dict[str, Any]], envelope: OperationalEnvelope,
                         limitations: List[str]) -> CalibrationProfile:
    if not source_calibration_sessions:
        raise ValueError("a profile must be traceable to at least one source calibration session (Fase 54)")
    return CalibrationProfile(
        profile_id=new_profile_id(), created_utc=datetime.now(timezone.utc).isoformat(),
        code_commit=_current_commit_hash(), source_calibration_sessions=list(source_calibration_sessions),
        receiver_id=receiver_id, receiver_serial=receiver_serial, conditions=conditions,
        status=ProfileStatus.DRAFT, recommended_gain_db=recommended_gain_db,
        recommended_usable_band=recommended_usable_band, known_masks=known_masks,
        warmup_recommendation=warmup_recommendation, envelope=envelope, limitations=limitations,
        validity_note="NOT_ACTIVE - review required before any future use; see Fase 62",
    )


def write_profile(profile: CalibrationProfile, profiles_dir: Path) -> Path:
    """Fase 36: never overwrite a historical profile - each profile_id
    gets its own file, forever."""
    profiles_dir = Path(profiles_dir)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / f"{profile.profile_id}.json"
    if path.exists():
        raise FileExistsError(f"profile {path} already exists - profiles are never overwritten")
    atomic_write_json(path, profile.to_dict())
    return path
