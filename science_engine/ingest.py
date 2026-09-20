"""SCIENCE INPUT CONTRACT (section 6): the only door into SCIENCE.

1. receives a REDUCE session directory
2. runs reduce_engine.science_contract.validate_science_input FIRST
3. rejects an unknown schema version
4. rejects a session with no consumable (COMPLETED) points
5. does not proceed past a failed contract check

Reads only manifest.json and each point's master_spectrum.{json,h5} -
never data/mosaic, never RAW, never OBSERVE/CALIBRATE/ALIGN runtime
state (section 1). Field names below are taken from a real REDUCE
session produced by the frozen pipeline (commit 2afc4c5), not assumed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

import h5py
import numpy as np

from reduce_engine.models import REDUCE_SCHEMA_VERSION
from reduce_engine.science_contract import validate_science_input
from science_engine.models import ScienceInput, ScienceInputPoint
from science_engine.spatial import ra_hours_to_deg


class ScienceContractError(Exception):
    """Raised when a REDUCE session fails the science contract check -
    SCIENCE must never proceed past this (section 6.5)."""


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """Cost note (SCIENCE_PIPELINE.md, input identity): a master_spectrum.h5 is ~0.35 MB (8192 bins x 5
    arrays); hashing 100 of them is ~35 MB of sequential reads, well under a second on the Pi - cheap enough to
    do for EVERY point, so a stable Level 1 identity never needs to be approximated."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _malformed_reason(frequency_hz, velocity, relative_intensity, uncertainty, mask, n_contributing) -> Optional[str]:
    n = frequency_hz.shape[0]
    for name, array in (("relative_intensity", relative_intensity), ("uncertainty", uncertainty),
                        ("mask", mask), ("n_contributing", n_contributing)):
        if array.shape != (n,):
            return f"MALFORMED_ARRAYS: {name} shape {array.shape} != frequency_hz shape {(n,)}"
    if velocity is not None:
        if velocity.shape != (n,):
            return f"MALFORMED_ARRAYS: velocity_lsrk_m_s shape {velocity.shape} != frequency_hz shape {(n,)}"
        if not np.all(np.isfinite(velocity)):
            return "MALFORMED_ARRAYS: velocity_lsrk_m_s contains NaN/Inf"
        steps = np.diff(velocity)
        if not (np.all(steps > 0) or np.all(steps < 0)):
            return "MALFORMED_ARRAYS: velocity_lsrk_m_s is not strictly monotonic"
    return None


def load_science_input(reduce_session_dir: str | Path, *, max_points_to_check: int = 5) -> ScienceInput:
    session_dir = Path(reduce_session_dir)
    contract = validate_science_input(session_dir, max_points_to_open=max_points_to_check)
    if not contract.ok:
        raise ScienceContractError(
            f"REDUCE session at {session_dir} fails the science contract - refusing to ingest: "
            f"{contract.problems}"
        )

    manifest_path = session_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    if manifest.get("reduce_schema_version", manifest.get("schema_version")) not in (None, REDUCE_SCHEMA_VERSION):
        # manifest.json itself doesn't carry reduce_schema_version today (only
        # each point's master_spectrum.json does) - guard anyway in case a
        # future REDUCE version adds it, per section 45's forward-compat rule.
        raise ScienceContractError(
            f"REDUCE session manifest schema version is not {REDUCE_SCHEMA_VERSION!r} - refusing to guess its meaning"
        )

    if manifest.get("status") == "FAILED":
        raise ScienceContractError(f"REDUCE session at {session_dir} has status FAILED - its pipeline did not finish, "
                                   f"so its Level 1 outputs are not trusted; refusing to ingest")
    manifest_points = manifest.get("points", [])
    exclusions: list[dict] = []
    completed = []
    seen_indices: set = set()
    for entry in manifest_points:
        index = entry.get("point_index")
        if index in seen_indices:
            # a duplicated identity must never double-count a measurement (more integration, lower sigma)
            raise ScienceContractError(f"REDUCE manifest lists point_index {index!r} more than once - refusing to "
                                       f"ingest (a duplicate would fake extra integration and lower uncertainty)")
        seen_indices.add(index)
        if entry.get("status") == "COMPLETED":
            completed.append(entry)
        else:
            exclusions.append({"point_index": index, "reason": f"REDUCE_POINT_STATUS_{entry.get('status')}"})
    if not completed:
        raise ScienceContractError(f"REDUCE session at {session_dir} has no COMPLETED points - nothing to ingest")
    completed.sort(key=lambda entry: entry["point_index"])   # canonical order: never depends on manifest/filesystem order

    points: list[ScienceInputPoint] = []
    capture_owner: dict[str, int] = {}     # capture sha256 -> the first point that used that RAW capture
    for entry in completed:
        point_dir = session_dir / "points" / str(entry["point_index"])
        json_path, h5_path = point_dir / "master_spectrum.json", point_dir / "master_spectrum.h5"
        point_meta = json.loads(json_path.read_text())
        if point_meta.get("reduce_schema_version") != REDUCE_SCHEMA_VERSION:
            raise ScienceContractError(
                f"point {entry['point_index']}: reduce_schema_version "
                f"{point_meta.get('reduce_schema_version')!r} != supported {REDUCE_SCHEMA_VERSION!r}"
            )
        with h5py.File(h5_path, "r") as handle:
            frequency_hz = handle["frequency_hz"][:]
            relative_intensity = handle["relative_intensity"][:]
            uncertainty = handle["uncertainty"][:]
            mask = handle["mask"][:]
            n_contributing = handle["n_contributing"][:]
            velocity_lsrk_m_s = handle["velocity_lsrk_m_s"][:] if "velocity_lsrk_m_s" in handle else None

        ra_hours = point_meta.get("ra_hours")
        dec_degrees = point_meta.get("dec_degrees")
        problem = None
        if point_meta.get("point_index") != entry["point_index"]:
            problem = (f"MALFORMED_IDENTITY: manifest point_index {entry['point_index']} != "
                       f"master_spectrum.json point_index {point_meta.get('point_index')!r}")
        elif ra_hours is None or dec_degrees is None:
            problem = "MISSING_COORDINATES"
        elif not (np.isfinite(ra_hours) and np.isfinite(dec_degrees)) or not (-90.0 <= dec_degrees <= 90.0):
            problem = f"INVALID_COORDINATES: ra_hours={ra_hours!r} dec_degrees={dec_degrees!r}"
        elif velocity_lsrk_m_s is not None and point_meta.get("velocity_frame") != "lsrk":
            problem = f"VELOCITY_FRAME_NOT_LSRK: {point_meta.get('velocity_frame')!r}"
        else:
            problem = _malformed_reason(frequency_hz, velocity_lsrk_m_s, relative_intensity, uncertainty, mask,
                                        n_contributing)
        capture_shas = {ref.get("sha256") for ref in point_meta.get("capture_refs", []) if ref.get("sha256")}
        if problem is None and capture_shas and all(sha in capture_owner for sha in capture_shas):
            # the same RAW capture(s) already fed an earlier point: a duplicate measurement must never fake more
            # integration, a lower uncertainty or higher confidence
            owner = capture_owner[next(iter(capture_shas))]
            problem = f"DUPLICATE_CAPTURE_OF_POINT_{owner}"
        if problem:
            # this point cannot be gridded honestly - excluded WITH a recorded reason, never silently dropped
            exclusions.append({"point_index": entry["point_index"], "reason": problem})
            continue

        for sha in capture_shas:
            capture_owner.setdefault(sha, entry["point_index"])
        points.append(ScienceInputPoint(
            campaign_id=point_meta["campaign_id"],
            reduce_session_id=point_meta["reduce_session_id"],
            point_index=point_meta["point_index"],
            ra_hours=ra_hours, dec_degrees=dec_degrees,
            ra_deg=float(ra_hours_to_deg(ra_hours)),
            timestamp_start_utc=point_meta.get("timestamp_start_utc"),
            frequency_hz=frequency_hz, velocity_lsrk_m_s=velocity_lsrk_m_s,
            velocity_frame=point_meta["velocity_frame"],
            relative_intensity=relative_intensity, uncertainty=uncertainty,
            mask=mask, n_contributing=n_contributing,
            integration_time_seconds=point_meta["integration_time_seconds"],
            reduce_quality_state=point_meta["quality"]["state"],
            calibration_level=point_meta["calibration_level"],
            source_h5_sha256=_sha256_file(h5_path), source_json_sha256=_sha256_file(json_path),
            capture_refs=[{"capture_id": ref.get("capture_id"), "sha256": ref.get("sha256"),
                           "timestamp_utc": ref.get("timestamp_utc")}
                          for ref in point_meta.get("capture_refs", [])],
        ))

    if not points:
        raise ScienceContractError(f"REDUCE session at {session_dir}: every COMPLETED point was excluded at ingest: "
                                   f"{exclusions}")

    return ScienceInput(
        reduce_session_dir=str(session_dir), campaign_id=manifest["campaign_id"],
        reduce_session_id=manifest["reduce_session_id"], reduce_schema_version=REDUCE_SCHEMA_VERSION,
        reduce_session_status=manifest["status"], points=points, contract_problems=[],
        exclusions=exclusions, n_points_in_manifest=len(manifest_points),
    )
