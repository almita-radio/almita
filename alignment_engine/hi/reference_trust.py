"""Reference-data trust classification and manifest schema (Fase 2/4).

Ground-truth safety rule, unchanged in spirit from the existing
alignment.py/SyntheticHIReferenceProvider behavior, now made explicit and
enforceable in code rather than a single boolean:

    SYNC is only ever potentially eligible for REAL_VALIDATED references.

No override exists in this module. A caller that wants to force SYNC
against a lower-trust reference must not be able to do so by passing a
flag here - see hi/quality.py's evaluate_sync_eligibility(), which reads
this enum and has no bypass parameter either.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class ReferenceTrust(str, Enum):
    # An analytic/model-generated map (e.g. the existing galactic-disk
    # model catalog) - never observational, never SYNC-eligible.
    SYNTHETIC = "SYNTHETIC"
    # A small, deliberately-constructed FITS file used only to test parsing
    #/coordinate/velocity code - not a real survey, never SYNC-eligible.
    TEST_FIXTURE = "TEST_FIXTURE"
    # A real survey product (e.g. HI4PI) that has been loaded but not yet
    # independently checked (units, frame, coverage, NaN handling, moment
    # sanity) - real, but not yet trusted for SYNC.
    REAL_UNVERIFIED = "REAL_UNVERIFIED"
    # A real survey product that HAS passed validate_reference() below -
    # the only trust level evaluate_sync_eligibility() can ever approve.
    REAL_VALIDATED = "REAL_VALIDATED"


SYNC_ELIGIBLE_TRUST_LEVELS = frozenset({ReferenceTrust.REAL_VALIDATED})


def sha256_of_file(path: str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ReferenceManifest:
    """Provenance for one local reference-data product. Never invented -
    every field here must come from either the actual file on disk (hash,
    size) or an operator-supplied description of what they placed there
    (survey, version, source, coordinate_system, ...). See
    hi_reference_manifest.json in the module docstring's Fase 2 spec."""

    survey: str                      # e.g. "HI4PI", "SYNTHETIC", "TEST_FIXTURE"
    version: str                     # e.g. "2016A&A...594A.116H" or "v1"
    source: str                      # e.g. "CDS/VizieR J/A+A/594/A116" or "generated locally"
    original_filename: str
    sha256: str
    coordinate_system: str           # "Galactic" | "ICRS" | ...
    spectral_axis: str                # "velocity_lsr_km_s" | "velocity_topocentric_km_s" | "none"
    beam_resolution_arcmin: Optional[float]
    units: str                        # e.g. "K" (brightness temperature)
    preprocessing: List[str] = field(default_factory=list)   # e.g. ["cutout 20x20 deg around l=30,b=0"]
    trust: ReferenceTrust = ReferenceTrust.REAL_UNVERIFIED
    generated_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "survey": self.survey, "version": self.version, "source": self.source,
            "original_filename": self.original_filename, "sha256": self.sha256,
            "coordinate_system": self.coordinate_system, "spectral_axis": self.spectral_axis,
            "beam_resolution_arcmin": self.beam_resolution_arcmin, "units": self.units,
            "preprocessing": self.preprocessing, "trust": self.trust.value,
            "generated_utc": self.generated_utc, "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReferenceManifest":
        data = dict(data)
        data["trust"] = ReferenceTrust(data.get("trust", ReferenceTrust.REAL_UNVERIFIED.value))
        return cls(**data)

    def write(self, path: str) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def read(cls, path: str) -> "ReferenceManifest":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def verify_checksum(self, data_file_path: str) -> bool:
        """Re-hash the actual file on disk and compare - a manifest is
        provenance metadata, not proof, until this has been called."""
        return sha256_of_file(data_file_path) == self.sha256


def build_manifest_for_file(data_file_path: str, *, survey: str, version: str, source: str,
                             coordinate_system: str, spectral_axis: str, units: str,
                             beam_resolution_arcmin: Optional[float] = None,
                             preprocessing: Optional[List[str]] = None,
                             trust: ReferenceTrust = ReferenceTrust.REAL_UNVERIFIED,
                             extra: Optional[Dict[str, Any]] = None) -> ReferenceManifest:
    """Computes the one field that must never be hand-typed (sha256) from
    the real file; every other field is an explicit, operator-supplied
    description - this function does not infer or guess metadata from the
    file's contents."""
    return ReferenceManifest(
        survey=survey, version=version, source=source,
        original_filename=Path(data_file_path).name,
        sha256=sha256_of_file(data_file_path),
        coordinate_system=coordinate_system, spectral_axis=spectral_axis,
        beam_resolution_arcmin=beam_resolution_arcmin, units=units,
        preprocessing=list(preprocessing or []), trust=trust, extra=dict(extra or {}),
    )
