"""INGEST: discover campaign/point/capture structure from OBSERVE's real
output tree. Never hardcodes a campaign id, point count, or file naming
convention - everything is discovered from mosaic.csv, grid_metadata.json,
and the actual files present on disk. Missing metadata becomes UNKNOWN,
never assumed.

Real discovered quirk (kept, not "fixed"): mosaic.csv's `data_filename`
column records a `.dat` extension, but capture.py's real HDF5 files on
disk carry `.h5`. INGEST matches by filename stem, never by extension, and
records `data_filename_declared` vs `resolved_path` separately so this
mismatch is visible in provenance rather than silently papered over.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import h5py
import numpy as np

REQUIRED_ATTRS = (
    "sample_rate_hz", "center_frequency_hz", "capture_start_utc", "capture_status",
)


@dataclass
class PointRecord:
    point_index: int
    ra_hours: Optional[float]
    dec_degrees: Optional[float]
    capture_status_declared: str
    data_filename_declared: str
    resolved_path: Optional[Path]
    accepted: bool
    reject_reason: Optional[str]


@dataclass
class CampaignManifest:
    campaign_id: str
    root: Path
    session_id: Optional[str]
    grid: dict[str, Any]
    observer: dict[str, Any]
    points: list[PointRecord] = field(default_factory=list)

    def accepted_points(self) -> list[PointRecord]:
        return [p for p in self.points if p.accepted]


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _index_iq_files(campaign_root: Path) -> dict[str, Path]:
    """Map file stem -> path for every file under data/iq/, discovered
    recursively (session subdirectories are not assumed to have any fixed
    name or depth)."""
    iq_root = campaign_root / "data" / "iq"
    index: dict[str, Path] = {}
    if not iq_root.is_dir():
        return index
    for path in iq_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in (".h5", ".hdf5"):
            index[path.stem] = path
    return index


def discover_campaign(campaign_dir: str | Path) -> CampaignManifest:
    root = Path(campaign_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"campaign directory not found: {root}")
    grid_meta = _read_json_or_empty(root / "grid_metadata.json")
    observer = _read_json_or_empty(root / "observer_config.json")
    grid = grid_meta.get("grid", {})
    campaign_id = grid_meta.get("session_name") or root.name
    session_id = grid_meta.get("session_id")

    mosaic_csv = root / "mosaic.csv"
    if not mosaic_csv.exists():
        raise FileNotFoundError(f"mosaic.csv not found under {root} - not a recognizable OBSERVE campaign")

    iq_index = _index_iq_files(root)
    points: list[PointRecord] = []
    with mosaic_csv.open(newline="") as handle:
        for row in csv.DictReader(handle):
            point_index = int(row["point_number"])
            declared_status = row.get("capture_status", "UNKNOWN") or "UNKNOWN"
            declared_filename = row.get("data_filename", "") or ""
            stem = Path(declared_filename).stem if declared_filename else f"*_{point_index:04d}"
            resolved = iq_index.get(stem)
            accepted, reason = True, None
            if declared_status != "success":
                accepted, reason = False, f"capture_status_declared={declared_status}"
            elif resolved is None:
                accepted, reason = False, "no matching HDF5 file found on disk for declared data_filename stem"

            def _float_or_none(value: str | None) -> Optional[float]:
                try:
                    return float(value) if value not in (None, "") else None
                except ValueError:
                    return None

            points.append(PointRecord(
                point_index=point_index,
                ra_hours=_float_or_none(row.get("target_ra_hours")),
                dec_degrees=_float_or_none(row.get("target_dec_degrees")),
                capture_status_declared=declared_status,
                data_filename_declared=declared_filename,
                resolved_path=resolved,
                accepted=accepted,
                reject_reason=reason,
            ))
    return CampaignManifest(campaign_id=campaign_id, root=root, session_id=session_id,
                            grid=grid, observer=observer, points=points)


def _plain(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def read_capture(path: str | Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Read one raw capture read-only. Never writes to `path`. Raises
    ValueError (never silently substitutes) if the file is not a
    complete, successful capture."""
    capture_path = Path(path)
    if capture_path.name.endswith(".part"):
        raise ValueError(f"partial capture is not a valid REDUCE input: {capture_path}")
    with h5py.File(capture_path, "r") as capture:
        attrs = {key: _plain(value) for key, value in capture.attrs.items()}
        if attrs.get("capture_status") != "success":
            raise ValueError(f"capture_status != success: {capture_path}")
        missing = [name for name in REQUIRED_ATTRS if name not in attrs]
        if missing:
            raise ValueError(f"capture missing required metadata {missing}: {capture_path}")
        if "iq_data" not in capture:
            raise ValueError(f"capture has no iq_data dataset: {capture_path}")
        iq = capture["iq_data"][:]
    return iq, attrs


def metadata_field(attrs: dict[str, Any], key: str, *alt_keys: str) -> Any:
    """UNKNOWN-safe attribute lookup: tries key then each alt_keys, never
    guesses a numeric default when metadata is genuinely absent."""
    for candidate in (key, *alt_keys):
        if candidate in attrs and attrs[candidate] not in (None, "", "None"):
            return attrs[candidate]
    return "UNKNOWN"
