"""Storage: data/reduced/CAMPAIGN_ID/REDUCE_SESSION_ID/{manifest.json,
config.json, provenance.json, points/<point_index>/master_spectrum.{h5,json},
qc/, logs/events.jsonl}. Arrays go in HDF5, metadata/provenance in JSON -
never pickle. A finished session is never overwritten: reprocessing always
gets a new REDUCE_SESSION_ID. Never writes inside the raw campaign
directory.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import h5py
import numpy as np

from runtime_state import atomic_write_json
from reduce_engine.models import MasterSpectrum

POINTS_DIRNAME = "points"
QC_DIRNAME = "qc"
LOGS_DIRNAME = "logs"


def new_reduce_session_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"REDUCE-{now.strftime('%Y%m%d-%H%M%S-%f')}"


def _reject_unsafe_path_component(name: str, *, field: str) -> None:
    """campaign_id (from grid_metadata.json's session_name - nominally
    trusted OBSERVE output, but not different in kind from the untrusted
    session-id strings that a real path-traversal bug was found in for
    ALIGN/CALIBRATE's web layer) must be a single path COMPONENT, never a
    path. Real-world example this must reject: a corrupted or malicious
    grid_metadata.json with session_name="../../etc"."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"unsafe {field} for REDUCE session storage: {name!r}")


class ReduceSession:
    def __init__(self, output_root: str | Path, campaign_id: str, session_id: Optional[str] = None):
        _reject_unsafe_path_component(campaign_id, field="campaign_id")
        self.campaign_id = campaign_id
        self.session_id = session_id or new_reduce_session_id()
        _reject_unsafe_path_component(self.session_id, field="session_id")
        root = Path(output_root).resolve()
        self.dir = (root / campaign_id / self.session_id).resolve()
        if self.dir != root and root not in self.dir.parents:
            raise ValueError(f"resolved REDUCE session path escapes output_root: {self.dir} not under {root}")
        if self.dir.exists():
            raise FileExistsError(f"REDUCE session already exists (immutable): {self.dir}")
        self.dir.mkdir(parents=True)
        (self.dir / POINTS_DIRNAME).mkdir()
        (self.dir / QC_DIRNAME).mkdir()
        (self.dir / LOGS_DIRNAME).mkdir()
        self._events_path = self.dir / LOGS_DIRNAME / "events.jsonl"
        self._logger = self._build_logger()

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"reduce.session.{self.session_id}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            handler = logging.FileHandler(self.dir / LOGS_DIRNAME / "session.log")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        return logger

    def log_event(self, event: str, **fields: Any) -> None:
        record = {"event": event, "utc": datetime.now(timezone.utc).isoformat(), **fields}
        with self._events_path.open("a") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        self._logger.info("%s %s", event, fields)

    def write_config(self, config_dict: dict[str, Any]) -> None:
        atomic_write_json(self.dir / "config.json", config_dict)

    def write_provenance(self, provenance: dict[str, Any]) -> None:
        atomic_write_json(self.dir / "provenance.json", provenance)

    def write_point(self, spectrum: MasterSpectrum) -> Path:
        # Section 42 (self-description): a master_spectrum.h5 must be
        # identifiable and interpretable in isolation, without its JSON
        # sidecar or an external README - reduce_point() builds the
        # MasterSpectrum before a session exists, so the session identity is
        # only known here, at persist time.
        spectrum.reduce_session_id = self.session_id
        point_dir = self.dir / POINTS_DIRNAME / str(spectrum.point_index)
        point_dir.mkdir(parents=True, exist_ok=True)
        h5_path = point_dir / "master_spectrum.h5"
        with h5py.File(h5_path, "w") as handle:
            handle.create_dataset("frequency_hz", data=spectrum.frequency_hz)
            handle.create_dataset("relative_intensity", data=spectrum.relative_intensity)
            handle.create_dataset("uncertainty", data=spectrum.uncertainty)
            handle.create_dataset("mask", data=spectrum.mask)
            handle.create_dataset("n_contributing", data=spectrum.n_contributing)
            if spectrum.velocity_lsrk_m_s is not None:
                handle.create_dataset("velocity_lsrk_m_s", data=spectrum.velocity_lsrk_m_s)
            handle.attrs["reduce_schema_version"] = spectrum.reduce_schema_version
            handle.attrs["campaign_id"] = spectrum.campaign_id
            handle.attrs["reduce_session_id"] = self.session_id
            handle.attrs["point_index"] = spectrum.point_index
            handle.attrs["calibration_level"] = spectrum.calibration_level
            handle.attrs["velocity_frame"] = spectrum.velocity_frame
            handle.attrs["quality_state"] = spectrum.quality.state
            handle.attrs["units_json"] = json.dumps({
                "frequency_hz": "Hz (topocentric)",
                "velocity_lsrk_m_s": "m/s",
                "relative_intensity": "dimensionless fractional excess over fitted/referenced continuum "
                                      "- not Kelvin/dBm/Jy unless calibration_level=ABSOLUTE (not used in V1)",
                "uncertainty": "same unit as relative_intensity",
                "mask": "int64 bitmask - see reduce_engine.models.MaskFlag (GOOD=0)",
                "n_contributing": "count of contributing captures per bin",
            })
        atomic_write_json(point_dir / "master_spectrum.json", spectrum.manifest_dict())
        return point_dir

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        atomic_write_json(self.dir / "manifest.json", manifest)

    def write_qc(self, name: str, data: dict[str, Any]) -> None:
        atomic_write_json(self.dir / QC_DIRNAME / f"{name}.json", data)


def validate_session(session_dir: str | Path) -> dict[str, Any]:
    """Output integrity check for one finished REDUCE session: every file
    manifest.json expects actually exists, point counts agree, and no
    COMPLETED point is missing its H5/JSON pair (dangling reference).
    Read-only - never modifies the session."""
    session_dir = Path(session_dir)
    problems: list[str] = []
    for required in ("manifest.json", "config.json", "provenance.json"):
        if not (session_dir / required).exists():
            problems.append(f"missing {required}")
    if problems:
        return {"ok": False, "problems": problems}

    manifest = json.loads((session_dir / "manifest.json").read_text())
    completed_points = [p for p in manifest.get("points", []) if p.get("status") == "COMPLETED"]
    if len(completed_points) != manifest.get("points_completed"):
        problems.append(f"manifest points_completed={manifest.get('points_completed')} but "
                        f"{len(completed_points)} points listed with status COMPLETED")

    for entry in completed_points:
        point_dir = session_dir / POINTS_DIRNAME / str(entry["point_index"])
        h5_path, json_path = point_dir / "master_spectrum.h5", point_dir / "master_spectrum.json"
        if not h5_path.exists() or not json_path.exists():
            problems.append(f"point {entry['point_index']}: dangling reference - "
                            f"manifest says COMPLETED but {point_dir} is missing its output files")
            continue
        try:
            point_meta = json.loads(json_path.read_text())
            with h5py.File(h5_path, "r") as handle:
                n_bins_h5 = handle["frequency_hz"].shape[0]
        except (OSError, KeyError, json.JSONDecodeError) as error:
            problems.append(f"point {entry['point_index']}: unreadable output ({error})")
            continue
        if point_meta.get("n_bins") != n_bins_h5:
            problems.append(f"point {entry['point_index']}: manifest n_bins={point_meta.get('n_bins')} "
                            f"!= actual HDF5 frequency_hz length {n_bins_h5}")

    return {"ok": not problems, "problems": problems, "points_checked": len(completed_points)}


def load_master_spectrum(point_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read-only accessor for a persisted point (used by compare/replay).
    Returns (metadata_dict, arrays_dict) - never opens the array file for
    writing."""
    point_dir = Path(point_dir)
    metadata = json.loads((point_dir / "master_spectrum.json").read_text())
    arrays: dict[str, Any] = {}
    with h5py.File(point_dir / "master_spectrum.h5", "r") as handle:
        for key in handle.keys():
            arrays[key] = handle[key][:]
    return metadata, arrays
