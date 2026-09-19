"""PROVENANCE: answers "how exactly was this produced" without needing
anything beyond what this record itself contains - git commit, config and
its hash, calibration profile id/hash, input file hashes, software
versions, UTC start/end, hostname. No pickle, no hidden global state."""
from __future__ import annotations

import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from reduce_engine.config import PIPELINE_VERSION, ReduceConfig
from reduce_engine.models import REDUCE_SCHEMA_VERSION


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


def build_run_provenance(config: ReduceConfig, *, calibration_profile_hash: Optional[str],
                         input_hashes: list[str], started_utc: str) -> dict[str, Any]:
    return {
        "reduce_schema_version": REDUCE_SCHEMA_VERSION, "pipeline_version": PIPELINE_VERSION,
        "git_commit": _git_commit(), "config": config.to_dict(), "config_hash": config.config_hash(),
        "calibration_profile_hash": calibration_profile_hash, "input_hashes": input_hashes,
        "software_versions": _software_versions(), "started_utc": started_utc,
        "ended_utc": None, "hostname": platform.node(), "random_seed": config.random_seed,
    }


def close_run_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    provenance = dict(provenance)
    provenance["ended_utc"] = datetime.now(timezone.utc).isoformat()
    return provenance
