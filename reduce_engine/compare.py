"""COMPARE: regression-oriented session comparator. No astrophysical
comparison here - only "did REDUCE produce the same thing given the same
inputs", or "how did the numbers move when config/calibration changed".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from reduce_engine.storage import load_master_spectrum


def compare_sessions(session_dir_a: str | Path, session_dir_b: str | Path) -> dict[str, Any]:
    a_dir, b_dir = Path(session_dir_a), Path(session_dir_b)
    manifest_a = json.loads((a_dir / "manifest.json").read_text())
    manifest_b = json.loads((b_dir / "manifest.json").read_text())
    config_a = json.loads((a_dir / "config.json").read_text())
    config_b = json.loads((b_dir / "config.json").read_text())

    result: dict[str, Any] = {
        "same_source_campaign": manifest_a["source_campaign_root"] == manifest_b["source_campaign_root"],
        "same_config": config_a == config_b,
        "same_point_count": manifest_a["points_completed"] == manifest_b["points_completed"],
        "points_completed_a": manifest_a["points_completed"], "points_completed_b": manifest_b["points_completed"],
        "quality_counts_a": manifest_a["quality_counts"], "quality_counts_b": manifest_b["quality_counts"],
        "differing_config_keys": sorted(k for k in config_a if config_a.get(k) != config_b.get(k)),
        "per_point_rms_difference": {},
    }
    points_a = {p["point_index"] for p in manifest_a["points"] if p["status"] == "COMPLETED"}
    points_b = {p["point_index"] for p in manifest_b["points"] if p["status"] == "COMPLETED"}
    for point_index in sorted(points_a & points_b):
        _, arrays_a = load_master_spectrum(a_dir / "points" / str(point_index))
        _, arrays_b = load_master_spectrum(b_dir / "points" / str(point_index))
        if arrays_a["relative_intensity"].shape == arrays_b["relative_intensity"].shape:
            diff = arrays_a["relative_intensity"] - arrays_b["relative_intensity"]
            result["per_point_rms_difference"][str(point_index)] = float(np.sqrt(np.nanmean(diff ** 2)))
        else:
            result["per_point_rms_difference"][str(point_index)] = "shape_mismatch"
    return result
