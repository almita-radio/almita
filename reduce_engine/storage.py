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


class ReduceSession:
    def __init__(self, output_root: str | Path, campaign_id: str, session_id: Optional[str] = None):
        self.campaign_id = campaign_id
        self.session_id = session_id or new_reduce_session_id()
        self.dir = Path(output_root) / campaign_id / self.session_id
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
        atomic_write_json(point_dir / "master_spectrum.json", spectrum.manifest_dict())
        return point_dir

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        atomic_write_json(self.dir / "manifest.json", manifest)

    def write_qc(self, name: str, data: dict[str, Any]) -> None:
        atomic_write_json(self.dir / QC_DIRNAME / f"{name}.json", data)


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
