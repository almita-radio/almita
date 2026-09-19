"""SCIENCE provenance (sections 59-60): answers "how exactly was this
produced", including the full back-reference chain to the REDUCE session
consumed (section 52) - science product -> science session -> REDUCE
session -> input hashes. Mirrors reduce_engine/provenance.py's own
pattern (not imported from it - SCIENCE owns its own provenance record).
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from science_engine.config import SCIENCE_PIPELINE_VERSION, ScienceConfig
from science_engine.models import SCIENCE_SCHEMA_VERSION


def _git_commit() -> Optional[str]:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parent.parent, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _software_versions() -> dict[str, str]:
    versions = {"python": sys.version.split()[0]}
    for module_name in ("numpy", "h5py", "astropy"):
        try:
            module = __import__(module_name)
            versions[module_name] = getattr(module, "__version__", "UNKNOWN")
        except ImportError:
            versions[module_name] = "NOT_INSTALLED"
    return versions


def reduce_manifest_hash(reduce_session_dir: str | Path) -> str:
    """A single hash standing in for "which exact REDUCE session was
    this built from" - hashes manifest.json's bytes (which itself embeds
    the campaign's own input_hashes list, so this transitively covers
    every RAW capture that fed the REDUCE session)."""
    manifest_path = Path(reduce_session_dir) / "manifest.json"
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def build_run_provenance(config: ScienceConfig, *, reduce_session_dir: str, reduce_session_id: str,
                         reduce_schema_version: str, started_utc: str) -> dict[str, Any]:
    return {
        "science_schema_version": SCIENCE_SCHEMA_VERSION, "science_pipeline_version": SCIENCE_PIPELINE_VERSION,
        "git_commit": _git_commit(), "config": config.to_dict(), "config_hash": config.config_hash(),
        "input_reduce_session_dir": str(reduce_session_dir), "input_reduce_session_id": reduce_session_id,
        "input_reduce_schema_version": reduce_schema_version,
        "input_reduce_manifest_hash": reduce_manifest_hash(reduce_session_dir),
        "software_versions": _software_versions(), "started_utc": started_utc,
        "ended_utc": None, "hostname": platform.node(),
    }


def close_run_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    provenance = dict(provenance)
    provenance["ended_utc"] = datetime.now(timezone.utc).isoformat()
    return provenance
