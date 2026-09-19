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

import json
from pathlib import Path

import h5py

from reduce_engine.models import REDUCE_SCHEMA_VERSION
from reduce_engine.science_contract import validate_science_input
from science_engine.models import ScienceInput, ScienceInputPoint
from science_engine.spatial import ra_hours_to_deg


class ScienceContractError(Exception):
    """Raised when a REDUCE session fails the science contract check -
    SCIENCE must never proceed past this (section 6.5)."""


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

    completed = [p for p in manifest.get("points", []) if p.get("status") == "COMPLETED"]
    if not completed:
        raise ScienceContractError(f"REDUCE session at {session_dir} has no COMPLETED points - nothing to ingest")

    points: list[ScienceInputPoint] = []
    for entry in completed:
        point_dir = session_dir / "points" / str(entry["point_index"])
        point_meta = json.loads((point_dir / "master_spectrum.json").read_text())
        if point_meta.get("reduce_schema_version") != REDUCE_SCHEMA_VERSION:
            raise ScienceContractError(
                f"point {entry['point_index']}: reduce_schema_version "
                f"{point_meta.get('reduce_schema_version')!r} != supported {REDUCE_SCHEMA_VERSION!r}"
            )
        with h5py.File(point_dir / "master_spectrum.h5", "r") as handle:
            frequency_hz = handle["frequency_hz"][:]
            relative_intensity = handle["relative_intensity"][:]
            uncertainty = handle["uncertainty"][:]
            mask = handle["mask"][:]
            n_contributing = handle["n_contributing"][:]
            velocity_lsrk_m_s = handle["velocity_lsrk_m_s"][:] if "velocity_lsrk_m_s" in handle else None

        ra_hours = point_meta.get("ra_hours")
        dec_degrees = point_meta.get("dec_degrees")
        if ra_hours is None or dec_degrees is None:
            # a point with no pointing metadata cannot be gridded - excluded
            # here rather than crashing the whole ingest (section 84-style
            # adversarial-input honesty: fail this point, not the session).
            continue

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
        ))

    return ScienceInput(
        reduce_session_dir=str(session_dir), campaign_id=manifest["campaign_id"],
        reduce_session_id=manifest["reduce_session_id"], reduce_schema_version=REDUCE_SCHEMA_VERSION,
        reduce_session_status=manifest["status"], points=points, contract_problems=[],
    )
