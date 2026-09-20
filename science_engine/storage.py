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

import contextlib
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import h5py
import numpy as np

from runtime_state import atomic_write_json
from science_engine.models import BEAM_FWHM_MAX_DEG, BEAM_FWHM_MIN_DEG, SCIENCE_SCHEMA_VERSION, ScienceCube, SpatialMap

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
        self.session_id = session_id if session_id is not None else new_science_session_id()   # "" is rejected below, never silently replaced
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

    def write_cube(self, cube: ScienceCube, *, campaign_id: str, reduce_session_id: str,
                  beam: dict[str, Any], config_hash: str, quality_state: str, data_completeness: str,
                  input_reduce_manifest_sha256: str = "") -> Path:
        h5_path = self.dir / CUBE_DIRNAME / "science_cube.h5"
        with atomic_h5(h5_path) as handle:
            handle.create_dataset("velocity_lsrk_m_s", data=cube.velocity_lsrk_m_s)
            handle.create_dataset("relative_intensity", data=cube.relative_intensity)
            handle.create_dataset("uncertainty", data=cube.uncertainty)
            handle.create_dataset("weight_sum", data=cube.weight_sum)
            handle.create_dataset("n_pointings", data=cube.n_pointings)
            handle.create_dataset("valid", data=cube.valid)
            self._common_attrs(handle, campaign_id, reduce_session_id, beam, config_hash, quality_state,
                               data_completeness, input_reduce_manifest_sha256, cube.grid.to_dict())
            handle.attrs["kind"] = "science_cube"
            handle.attrs["axis_order"] = "velocity,y,x"
            handle.attrs["velocity_axis_order"] = "ascending"
            handle.attrs["velocity_frame"] = "lsrk"
            handle.attrs["velocity_unit"] = "m/s"
            handle.attrs["shape"] = json.dumps(list(cube.relative_intensity.shape))
            handle.attrs["units_json"] = json.dumps({
                "velocity_lsrk_m_s": "m/s", "relative_intensity": INTENSITY_UNIT,
                "uncertainty": "same unit as relative_intensity (1-sigma, propagated, independent-input model)",
                "weight_sum": "sum of applied beam*inverse-variance weights (1/relative_intensity^2)",
                "n_pointings": "count of POINTINGS (spatial input points) with non-zero weight at this voxel; "
                               "unrelated to REDUCE's n_contributing (subintegrations per bin)",
                "valid": "bool, False = no usable contribution",
            })
            handle.attrs["build_info_json"] = json.dumps(cube.build_info, default=str)
        return h5_path

    def write_map(self, science_map: SpatialMap, name: str, *, campaign_id: str, reduce_session_id: str,
                 beam: dict[str, Any], config_hash: str, quality_state: str, data_completeness: str,
                 input_reduce_manifest_sha256: str = "") -> Path:
        h5_path = self.dir / MAPS_DIRNAME / f"{name}.h5"
        with atomic_h5(h5_path) as handle:
            handle.create_dataset("value", data=science_map.value)
            if science_map.uncertainty is not None:
                handle.create_dataset("uncertainty", data=science_map.uncertainty)
            handle.create_dataset("weight_sum", data=science_map.weight_sum)
            handle.create_dataset("n_pointings", data=science_map.n_pointings)
            handle.create_dataset("valid", data=science_map.valid)
            if science_map.spectral_coverage is not None:
                handle.create_dataset("spectral_coverage_fraction", data=science_map.spectral_coverage)
            self._common_attrs(handle, campaign_id, reduce_session_id, beam, config_hash, quality_state,
                               data_completeness, input_reduce_manifest_sha256, science_map.grid.to_dict())
            handle.attrs["kind"] = science_map.kind
            handle.attrs["axis_order"] = "y,x"
            handle.attrs["units"] = science_map.units
            handle.attrs["shape"] = json.dumps(list(science_map.value.shape))
            handle.attrs["metadata_json"] = json.dumps(science_map.metadata, default=str)
        return h5_path

    def _common_attrs(self, handle, campaign_id, reduce_session_id, beam, config_hash, quality_state,
                      data_completeness, input_reduce_manifest_sha256, grid_dict) -> None:
        """Everything needed to interpret the file WITHOUT the manifest sidecar (section 69/70)."""
        handle.attrs["science_schema_version"] = SCIENCE_SCHEMA_VERSION
        handle.attrs["campaign_id"] = campaign_id
        handle.attrs["reduce_session_id"] = reduce_session_id
        handle.attrs["science_session_id"] = self.session_id
        handle.attrs["input_reduce_manifest_sha256"] = input_reduce_manifest_sha256
        handle.attrs["config_hash"] = config_hash
        handle.attrs["quality_state"] = quality_state
        handle.attrs["data_completeness"] = data_completeness
        handle.attrs["coordinate_frame"] = grid_dict["frame"]
        handle.attrs["spatial_orientation"] = ("x increases with RA offset (east), y increases with Dec (north); "
                                              "row 0 = southernmost; origin='lower' for display")
        handle.attrs["intensity_unit"] = INTENSITY_UNIT
        # beam: OPERATOR-PROVIDED operational metadata - see SCIENCE_MODEL.md
        handle.attrs["beam_model"] = beam["model_type"]
        handle.attrs["beam_fwhm_deg"] = float(beam["fwhm_deg"])
        handle.attrs["beam_source"] = beam["source"]
        handle.attrs["beam_status"] = beam["status"]
        handle.attrs["beam_json"] = json.dumps(beam)
        handle.attrs["grid_json"] = json.dumps(grid_dict)


INTENSITY_UNIT = "relative_intensity_dimensionless (fractional excess over the REDUCE baseline; no absolute calibration)"


@contextlib.contextmanager
def atomic_h5(path: Path):
    """Write an HDF5 file to `<name>.tmp` and os.replace() it into place only after a clean close, so a crash or
    interrupt can never leave a truncated file at the canonical path (a `.tmp` leftover is removed)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with h5py.File(tmp, "w") as handle:
            yield handle
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def arrays_sha256(path: Path) -> str:
    """Hash of the SCIENTIFIC ARRAYS only (dataset name, dtype, shape, bytes; sorted by name) - independent of
    attributes such as the session id, so two runs of the same input+config compare equal even though their
    files differ by identity metadata."""
    digest = hashlib.sha256()
    with h5py.File(path, "r") as handle:
        for name in sorted(handle.keys()):
            data = handle[name][()]
            digest.update(name.encode())
            digest.update(str(data.dtype).encode())
            digest.update(str(data.shape).encode())
            digest.update(np.ascontiguousarray(data).tobytes())
    return digest.hexdigest()


PRODUCT_STATUSES = ("VALID", "PARTIAL", "BLOCKED", "FAILED")
SESSION_STATUSES = ("COMPLETED", "FAILED", "CANCELLED", "RUNNING")
REQUIRED_H5_ATTRS = ("science_schema_version", "campaign_id", "reduce_session_id", "science_session_id", "config_hash",
                     "quality_state", "data_completeness", "coordinate_frame", "intensity_unit", "beam_model",
                     "beam_fwhm_deg", "beam_source", "beam_status", "grid_json", "kind", "axis_order")


def validate_science_session(session_dir: str | Path) -> dict[str, Any]:
    """Read-only integrity check of a finished SCIENCE session: manifest/schema/config/provenance consistency,
    every indexed product exists, is readable HDF5, matches its recorded sha256, has correctly-shaped datasets,
    the required self-description attributes and beam metadata, and no unindexed/temporary canonical file
    exists. Fails closed on an unknown schema version - it never guesses a meaning."""
    session_dir = Path(session_dir)
    problems: list[str] = []
    for required in ("manifest.json", "config.json", "provenance.json"):
        if not (session_dir / required).exists():
            problems.append(f"missing {required}")
    if problems:
        try:    # still say WHY the session is unusable when the manifest is readable (e.g. FAILED / CANCELLED / RUNNING)
            status = json.loads((session_dir / "manifest.json").read_text()).get("status") if (
                session_dir / "manifest.json").exists() else None
            if status and status != "COMPLETED":
                problems.insert(0, f"session status is {status} - not a finished science product")
        except (json.JSONDecodeError, OSError):
            pass
        return {"ok": False, "problems": problems}

    try:
        manifest = json.loads((session_dir / "manifest.json").read_text())
        config_dict = json.loads((session_dir / "config.json").read_text())
        provenance = json.loads((session_dir / "provenance.json").read_text())
    except (json.JSONDecodeError, OSError) as error:
        return {"ok": False, "problems": [f"unreadable JSON: {error}"]}

    if manifest.get("science_schema_version") != SCIENCE_SCHEMA_VERSION:
        return {"ok": False, "problems": [f"unknown science_schema_version {manifest.get('science_schema_version')!r} "
                                          f"(this validator supports {SCIENCE_SCHEMA_VERSION!r}) - refusing to guess"]}
    status = manifest.get("status")
    if status not in SESSION_STATUSES:
        problems.append(f"invalid session status {status!r}")
    elif status != "COMPLETED":
        problems.append(f"session status is {status} - not a finished science product")
    if manifest.get("data_completeness") not in ("COMPLETE", "PARTIAL"):
        problems.append(f"invalid data_completeness {manifest.get('data_completeness')!r}")
    elif (manifest.get("input_reduce_session_status") != "COMPLETED") and manifest.get("data_completeness") == "COMPLETE":
        problems.append("data_completeness COMPLETE but the input REDUCE session was not COMPLETED")

    try:
        from science_engine.config import ScienceConfig
        recomputed = ScienceConfig(**config_dict).config_hash()
        if recomputed != manifest.get("config_hash"):
            problems.append("config.json does not hash to manifest.config_hash")
        if provenance.get("config_hash") != manifest.get("config_hash"):
            problems.append("provenance.config_hash != manifest.config_hash")
    except (TypeError, ValueError) as error:
        problems.append(f"config.json is not a valid ScienceConfig: {error}")
    if not provenance.get("input_reduce_manifest_hash"):
        problems.append("provenance missing input_reduce_manifest_hash")

    beam = manifest.get("beam") or {}
    fwhm = beam.get("fwhm_deg")
    if not (isinstance(fwhm, (int, float)) and np.isfinite(fwhm) and BEAM_FWHM_MIN_DEG <= fwhm <= BEAM_FWHM_MAX_DEG):
        problems.append(f"invalid beam fwhm_deg in manifest: {fwhm!r}")
    if not beam.get("source") or not beam.get("status"):
        problems.append("beam source/status missing in manifest")

    indexed_paths = set()
    products = manifest.get("products", [])
    grid = manifest.get("grid") or {}
    ny, nx, nv = grid.get("ny"), grid.get("nx"), manifest.get("n_velocity_channels")
    for product in products:
        pid = product.get("id", "?")
        for key in ("id", "kind", "path", "shape", "unit", "status", "reasons", "sha256"):
            if key not in product:
                problems.append(f"product {pid}: index entry missing {key!r}")
        if product.get("status") not in PRODUCT_STATUSES:
            problems.append(f"product {pid}: invalid status {product.get('status')!r}")
        path = session_dir / product.get("path", "")
        indexed_paths.add(str((session_dir / product.get("path", "")).resolve()))
        if not path.is_file():
            problems.append(f"product {pid}: dangling reference - {path} missing")
            continue
        if product.get("sha256") and sha256_file(path) != product["sha256"]:
            problems.append(f"product {pid}: sha256 mismatch (file modified or corrupted)")
        try:
            with h5py.File(path, "r") as handle:
                for attr in REQUIRED_H5_ATTRS:
                    if attr not in handle.attrs:
                        problems.append(f"product {pid}: HDF5 missing self-description attribute {attr!r}")
                if handle.attrs.get("config_hash") != manifest.get("config_hash"):
                    problems.append(f"product {pid}: HDF5 config_hash != manifest")
                if handle.attrs.get("science_session_id") != manifest.get("science_session_id"):
                    problems.append(f"product {pid}: HDF5 science_session_id != manifest")
                if handle.attrs.get("data_completeness") != manifest.get("data_completeness"):
                    problems.append(f"product {pid}: HDF5 data_completeness != manifest")
                if "beam_fwhm_deg" in handle.attrs and abs(float(handle.attrs["beam_fwhm_deg"]) - float(fwhm or np.nan)) > 0:
                    problems.append(f"product {pid}: HDF5 beam_fwhm_deg != manifest")
                if product.get("kind") == "cube":
                    missing = [name for name in ("velocity_lsrk_m_s", "relative_intensity", "uncertainty",
                                                 "weight_sum", "n_pointings", "valid") if name not in handle]
                    problems.extend(f"product {pid}: missing dataset {name!r}" for name in missing)
                    if not missing:
                        velocity = handle["velocity_lsrk_m_s"][:]
                        expected = (velocity.shape[0], ny, nx)
                        if nv is not None and velocity.shape[0] != nv:
                            problems.append(f"product {pid}: velocity axis length {velocity.shape[0]} != manifest {nv}")
                        if velocity.shape[0] > 1 and not np.all(np.diff(velocity) > 0):
                            problems.append(f"product {pid}: velocity axis is not strictly ascending")
                        for name in ("relative_intensity", "uncertainty", "weight_sum", "n_pointings", "valid"):
                            if handle[name].shape != expected:
                                problems.append(f"product {pid}: {name} shape {handle[name].shape} != {expected}")
                else:
                    if "value" not in handle:
                        problems.append(f"product {pid}: missing dataset 'value'")
                    elif (ny, nx) != (None, None) and handle["value"].shape != (ny, nx):
                        problems.append(f"product {pid}: value shape {handle['value'].shape} != {(ny, nx)}")
                if list(json.loads(str(handle.attrs.get("shape", "[]")))) != list(product.get("shape", [])):
                    problems.append(f"product {pid}: HDF5 shape attribute != index shape")
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
            problems.append(f"product {pid}: not readable as a valid science HDF5 ({type(error).__name__}: {error})")

    for sub in (CUBE_DIRNAME, MAPS_DIRNAME):
        for file in sorted((session_dir / sub).glob("*")) if (session_dir / sub).is_dir() else []:
            if file.name.endswith(".tmp"):
                problems.append(f"leftover temporary file {file.name} (interrupted write)")
            elif file.suffix == ".h5" and str(file.resolve()) not in indexed_paths:
                problems.append(f"canonical file {sub}/{file.name} is not in the manifest product index")
    return {"ok": not problems, "problems": problems, "products_checked": len(products)}
