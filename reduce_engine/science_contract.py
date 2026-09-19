"""SCIENCE CONTRACT VALIDATOR: a small, pure function that checks whether
a REDUCE session is consumable by the future SCIENCE module, without
executing any science and without opening RAW.

This is the boundary contract from docs/REDUCE_SCOPE.md made checkable:
SCIENCE should only ever need a session's manifest, MASTER SPECTRA,
coordinates, masks, uncertainty, quality, and provenance - never IQ, never
a re-run FFT, never recalibration, never a redone Doppler correction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reduce_engine.models import REDUCE_SCHEMA_VERSION

REQUIRED_MANIFEST_FIELDS = (
    "campaign_id", "reduce_session_id", "status", "points_discovered", "points_completed",
    "quality_counts", "source_campaign_root",
)
REQUIRED_POINT_JSON_FIELDS = (
    "reduce_schema_version", "reduce_session_id", "campaign_id", "point_index", "n_bins", "velocity_frame",
    "integration_time_seconds", "quality", "calibration_level", "capture_refs",
)
REQUIRED_ARRAY_KEYS = ("frequency_hz", "relative_intensity", "uncertainty", "mask", "n_contributing")


@dataclass
class ScienceContractResult:
    ok: bool
    problems: list[str] = field(default_factory=list)
    points_checked: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": list(self.problems), "points_checked": self.points_checked}


def validate_science_input(session_dir: str | Path, *, max_points_to_open: int = 5) -> ScienceContractResult:
    """Read-only. Opens only MASTER SPECTRUM files (LEVEL 1) - never RAW,
    never IQ, never a campaign directory under data/mosaic. Sampling
    `max_points_to_open` real point files is enough to catch a systemic
    schema problem without re-reading a whole large campaign."""
    session_dir = Path(session_dir)
    problems: list[str] = []

    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return ScienceContractResult(ok=False, problems=[f"no manifest.json at {session_dir}"])
    manifest = json.loads(manifest_path.read_text())
    for required in REQUIRED_MANIFEST_FIELDS:
        if required not in manifest:
            problems.append(f"manifest missing required field: {required}")

    if manifest.get("status") not in ("COMPLETED", "PARTIAL", "FAILED"):
        problems.append(f"manifest status {manifest.get('status')!r} is not a recognized session status")

    completed = [p for p in manifest.get("points", []) if p.get("status") == "COMPLETED"]
    if not completed:
        problems.append("no COMPLETED points - nothing SCIENCE could consume from this session")

    checked = 0
    for entry in completed[:max_points_to_open]:
        point_dir = session_dir / "points" / str(entry["point_index"])
        json_path, h5_path = point_dir / "master_spectrum.json", point_dir / "master_spectrum.h5"
        if not json_path.exists() or not h5_path.exists():
            problems.append(f"point {entry['point_index']}: output files missing")
            continue
        point_meta = json.loads(json_path.read_text())
        for required in REQUIRED_POINT_JSON_FIELDS:
            if required not in point_meta:
                problems.append(f"point {entry['point_index']}: missing required field {required}")
        if point_meta.get("reduce_schema_version") != REDUCE_SCHEMA_VERSION:
            problems.append(f"point {entry['point_index']}: schema_version "
                            f"{point_meta.get('reduce_schema_version')!r} != supported {REDUCE_SCHEMA_VERSION!r} "
                            f"- SCIENCE must reject an unknown schema, never guess its meaning")
        if point_meta.get("calibration_level") not in ("RELATIVE", "UNCALIBRATED"):
            problems.append(f"point {entry['point_index']}: calibration_level "
                            f"{point_meta.get('calibration_level')!r} is not RELATIVE/UNCALIBRATED")
        if not point_meta.get("capture_refs"):
            problems.append(f"point {entry['point_index']}: no capture_refs - provenance to RAW is missing")

        import h5py
        with h5py.File(h5_path, "r") as handle:
            for key in REQUIRED_ARRAY_KEYS:
                if key not in handle:
                    problems.append(f"point {entry['point_index']}: HDF5 missing array {key!r}")
            if "frequency_hz" in handle and "relative_intensity" in handle:
                if handle["frequency_hz"].shape != handle["relative_intensity"].shape:
                    problems.append(f"point {entry['point_index']}: frequency_hz/relative_intensity shape mismatch")
        checked += 1

    return ScienceContractResult(ok=not problems, problems=problems, points_checked=checked)
