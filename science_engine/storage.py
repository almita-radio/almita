"""ScienceSession persistence (sections 53-58, 113-114):

data/science/CAMPAIGN_ID/SCIENCE_SESSION_ID/
    manifest.json config.json provenance.json
    grid/ cube/ maps/ spectra/ qc/ logs/

Immutable once created (same session id -> FileExistsError, never
overwrite); path-safety check reuses the exact pattern validated in
reduce_engine.storage._reject_unsafe_path_component (section 113: "reuse
a validated pattern when possible") - re-implemented here rather than
imported, since SCIENCE owns its own package and this repo's convention
(REDUCE's own path-safety function is private/module-internal) doesn't
export it as shared API.
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
from science_engine.models import ScienceCube, SpatialMap

GRID_DIRNAME, CUBE_DIRNAME, MAPS_DIRNAME, SPECTRA_DIRNAME, QC_DIRNAME, LOGS_DIRNAME = (
    "grid", "cube", "maps", "spectra", "qc", "logs")


def new_science_session_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"SCIENCE-{now.strftime('%Y%m%d-%H%M%S-%f')}"


def _reject_unsafe_path_component(name: str, *, field: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"unsafe {field} for SCIENCE session storage: {name!r}")


class ScienceSession:
    def __init__(self, output_root: str | Path, campaign_id: str, session_id: Optional[str] = None):
        _reject_unsafe_path_component(campaign_id, field="campaign_id")
        self.campaign_id = campaign_id
        self.session_id = session_id or new_science_session_id()
        _reject_unsafe_path_component(self.session_id, field="session_id")
        root = Path(output_root).resolve()
        self.dir = (root / campaign_id / self.session_id).resolve()
        if self.dir != root and root not in self.dir.parents:
            raise ValueError(f"resolved SCIENCE session path escapes output_root: {self.dir} not under {root}")
        if self.dir.exists():
            raise FileExistsError(f"SCIENCE session already exists (immutable): {self.dir}")
        self.dir.mkdir(parents=True)
        for sub in (GRID_DIRNAME, CUBE_DIRNAME, MAPS_DIRNAME, SPECTRA_DIRNAME, QC_DIRNAME, LOGS_DIRNAME):
            (self.dir / sub).mkdir()
        self._events_path = self.dir / LOGS_DIRNAME / "events.jsonl"
        self._logger = self._build_logger()

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"science.session.{self.session_id}")
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

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        atomic_write_json(self.dir / "manifest.json", manifest)

    def write_qc(self, name: str, data: dict[str, Any]) -> None:
        atomic_write_json(self.dir / QC_DIRNAME / f"{name}.json", data)

    def write_cube(self, cube: ScienceCube, *, campaign_id: str, reduce_session_id: str) -> Path:
        h5_path = self.dir / CUBE_DIRNAME / "science_cube.h5"
        with h5py.File(h5_path, "w") as handle:
            handle.create_dataset("velocity_lsrk_m_s", data=cube.velocity_lsrk_m_s)
            handle.create_dataset("relative_intensity", data=cube.relative_intensity)
            handle.create_dataset("uncertainty", data=cube.uncertainty)
            handle.create_dataset("weight_sum", data=cube.weight_sum)
            handle.create_dataset("n_contributing", data=cube.n_contributing)
            handle.create_dataset("valid", data=cube.valid)
            handle.attrs["science_schema_version"] = "1.0"
            handle.attrs["axis_order"] = "velocity,y,x"
            handle.attrs["campaign_id"] = campaign_id
            handle.attrs["reduce_session_id"] = reduce_session_id
            handle.attrs["science_session_id"] = self.session_id
            handle.attrs["units_json"] = json.dumps({
                "velocity_lsrk_m_s": "m/s", "relative_intensity": "dimensionless fractional excess (see REDUCE units)",
                "uncertainty": "same unit as relative_intensity", "weight_sum": "sum of applied beam*inverse-variance weight",
                "n_contributing": "count of contributing input points per voxel", "valid": "bool, False = no contribution",
            })
            handle.attrs["grid_json"] = json.dumps(cube.grid.to_dict())
        return h5_path

    def write_map(self, science_map: SpatialMap, name: str, *, campaign_id: str, reduce_session_id: str) -> Path:
        h5_path = self.dir / MAPS_DIRNAME / f"{name}.h5"
        with h5py.File(h5_path, "w") as handle:
            handle.create_dataset("value", data=science_map.value)
            if science_map.uncertainty is not None:
                handle.create_dataset("uncertainty", data=science_map.uncertainty)
            handle.create_dataset("weight_sum", data=science_map.weight_sum)
            handle.create_dataset("n_contributing", data=science_map.n_contributing)
            handle.create_dataset("valid", data=science_map.valid)
            handle.attrs["science_schema_version"] = "1.0"
            handle.attrs["kind"] = science_map.kind
            handle.attrs["units"] = science_map.units
            handle.attrs["campaign_id"] = campaign_id
            handle.attrs["reduce_session_id"] = reduce_session_id
            handle.attrs["science_session_id"] = self.session_id
            handle.attrs["grid_json"] = json.dumps(science_map.grid.to_dict())
            handle.attrs["metadata_json"] = json.dumps(science_map.metadata, default=str)
        return h5_path


def validate_science_session(session_dir: str | Path) -> dict[str, Any]:
    """Output integrity check (section 41 analog): every file the
    manifest expects actually exists. Read-only."""
    session_dir = Path(session_dir)
    problems: list[str] = []
    for required in ("manifest.json", "config.json", "provenance.json"):
        if not (session_dir / required).exists():
            problems.append(f"missing {required}")
    if problems:
        return {"ok": False, "problems": problems}

    manifest = json.loads((session_dir / "manifest.json").read_text())
    for product in manifest.get("products", []):
        path = session_dir / product["path"]
        if not path.exists():
            problems.append(f"product {product['id']}: dangling reference - {path} missing")
    return {"ok": not problems, "problems": problems, "products_checked": len(manifest.get("products", []))}
