"""REDUCE's core data model: capture identity, bin masks, quality states, and
the MASTER SPECTRUM contract handed to the future SCIENCE module.

Everything here is a plain dataclass/enum with explicit to_dict() -
deliberately no pickle, no hidden state, JSON/HDF5-serializable only.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Flag, auto
from pathlib import Path
from typing import Any, Optional

import numpy as np

REDUCE_SCHEMA_VERSION = "1.0"


def sha256_of_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class CaptureRef:
    """Identity of one raw capture. Every Level-1 product traces back to
    one or more CaptureRef -> original raw file. Never mutated after
    construction: a CaptureRef IS the evidence pointer, not a working copy."""
    campaign_id: str
    point_index: int
    capture_id: str
    source_path: str
    timestamp_utc: str
    sha256: str
    receiver: str
    metadata_source: str  # "hdf5_attrs" | "mosaic_csv" | "UNKNOWN"

    @classmethod
    def from_capture_file(cls, path: str | Path, *, campaign_id: str, point_index: int,
                          capture_id: str, timestamp_utc: str, receiver: str) -> "CaptureRef":
        return cls(campaign_id=campaign_id, point_index=point_index, capture_id=capture_id,
                   source_path=str(Path(path)), timestamp_utc=timestamp_utc,
                   sha256=sha256_of_file(path), receiver=receiver, metadata_source="hdf5_attrs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id, "point_index": self.point_index,
            "capture_id": self.capture_id, "source_path": self.source_path,
            "timestamp_utc": self.timestamp_utc, "sha256": self.sha256,
            "receiver": self.receiver, "metadata_source": self.metadata_source,
        }


class MaskFlag(Flag):
    """Multi-reason per-bin mask. A bin can carry more than one flag (e.g.
    RFI and EDGE simultaneously) - never a single boolean. GOOD = 0, i.e.
    "no reason to exclude this bin", not an affirmative state of its own."""
    GOOD = 0
    DC = auto()
    KNOWN_SPUR = auto()
    RFI = auto()
    EDGE = auto()
    INVALID = auto()
    MISSING = auto()
    SATURATED = auto()
    USER_EXCLUDED = auto()

    @classmethod
    def combine(cls, *flags: "MaskFlag") -> "MaskFlag":
        result = cls.GOOD
        for flag in flags:
            result |= flag
        return result

    def reasons(self) -> list[str]:
        if self is MaskFlag.GOOD:
            return []
        return [member.name for member in MaskFlag if member != MaskFlag.GOOD and member in self]

    def usable(self) -> bool:
        """A bin is usable in a fit/average only if it carries no exclusion
        reason at all. EDGE and DC are exclusion reasons like any other."""
        return self is MaskFlag.GOOD


def mask_array_to_reason_matrix(mask_values: np.ndarray) -> list[list[str]]:
    return [MaskFlag(int(value)).reasons() for value in np.asarray(mask_values, dtype=np.int64)]


class QualityState:
    GOOD = "GOOD"
    WARNING = "WARNING"
    BAD = "BAD"
    UNKNOWN = "UNKNOWN"

    VALID = frozenset({GOOD, WARNING, BAD, UNKNOWN})


@dataclass
class QualityReport:
    """Never a bare verdict: `state` is always accompanied by `reasons`,
    even when GOOD (e.g. "usable_fraction=0.94"), so a reader never has to
    take a rating on faith."""
    state: str
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.state not in QualityState.VALID:
            raise ValueError(f"invalid quality state {self.state!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reasons": list(self.reasons), "metrics": dict(self.metrics)}


@dataclass
class MasterSpectrum:
    """The flagship LEVEL 1 product per sky point - REDUCE's primary
    contract to SCIENCE. SCIENCE should never need to reopen IQ, redo FFT,
    recalibrate, or redo Doppler correction from this object."""
    campaign_id: str
    point_index: int
    ra_hours: Optional[float]
    dec_degrees: Optional[float]
    timestamp_start_utc: Optional[str]
    timestamp_end_utc: Optional[str]
    frequency_hz: np.ndarray
    velocity_lsrk_m_s: Optional[np.ndarray]
    velocity_frame: str  # one of alignment_engine.hi.velocity.SUPPORTED_FRAMES, or "UNAVAILABLE"
    relative_intensity: np.ndarray
    uncertainty: np.ndarray
    mask: np.ndarray  # int64 bitmask, MaskFlag values
    n_contributing: np.ndarray  # per-bin count of captures that contributed
    integration_time_seconds: float
    quality: QualityReport
    calibration_level: str  # "RELATIVE" or "UNCALIBRATED"
    calibration_profile_id: Optional[str]
    calibration_profile_hash: Optional[str]
    capture_refs: list[CaptureRef]
    reduce_schema_version: str = REDUCE_SCHEMA_VERSION

    def __post_init__(self):
        n = self.frequency_hz.shape[0]
        for name in ("relative_intensity", "uncertainty", "mask", "n_contributing"):
            value = getattr(self, name)
            if value.shape[0] != n:
                raise ValueError(f"{name} length {value.shape[0]} != frequency_hz length {n}")
        if self.velocity_lsrk_m_s is not None and self.velocity_lsrk_m_s.shape[0] != n:
            raise ValueError("velocity_lsrk_m_s length mismatch")
        if self.calibration_level not in ("RELATIVE", "UNCALIBRATED"):
            raise ValueError("calibration_level must be RELATIVE or UNCALIBRATED (never Kelvin/dBm/Tsys/Jy claims)")

    def manifest_dict(self) -> dict[str, Any]:
        """Metadata only - the large arrays live in the HDF5 sibling file,
        never duplicated into JSON."""
        return {
            "reduce_schema_version": self.reduce_schema_version,
            "campaign_id": self.campaign_id, "point_index": self.point_index,
            "ra_hours": self.ra_hours, "dec_degrees": self.dec_degrees,
            "timestamp_start_utc": self.timestamp_start_utc, "timestamp_end_utc": self.timestamp_end_utc,
            "n_bins": int(self.frequency_hz.shape[0]),
            "velocity_frame": self.velocity_frame,
            "integration_time_seconds": self.integration_time_seconds,
            "quality": self.quality.to_dict(),
            "calibration_level": self.calibration_level,
            "calibration_profile_id": self.calibration_profile_id,
            "calibration_profile_hash": self.calibration_profile_hash,
            "capture_refs": [ref.to_dict() for ref in self.capture_refs],
        }
