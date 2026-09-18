"""Calibration replay (Fase 37) - re-analyze an existing session's raw
captures without touching SDR/mount, same immutable-source /
separately-versioned-analysis pattern as alignment_engine/hi/replay.py.
Never overwrites captures/ or a prior analysis/analysis-*/ directory.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from calibration_engine.acquisition import read_capture_iq
from calibration_engine.bandpass import compute_bandpass
from calibration_engine.clipping import ClippingThresholds, evaluate_clipping
from calibration_engine.sample_statistics import compute_sample_statistics
from calibration_engine.stability import StabilitySample, analyze_stability
from runtime_state import atomic_write_json


def _current_commit_hash() -> Optional[str]:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                 cwd=Path(__file__).resolve().parent.parent, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class CalibrationReplayResult:
    analysis_dir: str
    n_captures_found: int
    n_captures_valid: int
    per_capture: List[Dict[str, Any]]
    stability: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {"analysis_dir": self.analysis_dir, "n_captures_found": self.n_captures_found,
                "n_captures_valid": self.n_captures_valid, "per_capture": self.per_capture,
                "stability": self.stability}


def replay_calibration_session(session_dir: str, *, label: Optional[str] = None,
                                thresholds: ClippingThresholds = ClippingThresholds()) -> CalibrationReplayResult:
    """Reads ONLY <session_dir>/captures/*.h5 (+ calibration_config.json if
    present for context). Writes ONLY under
    <session_dir>/analysis/analysis-<UTC timestamp>[-label]/, a fresh
    directory every call - never touches captures/ or an existing
    analysis/ directory."""
    session_path = Path(session_dir)
    captures_dir = session_path / "captures"
    capture_paths = sorted(captures_dir.glob("*.h5")) if captures_dir.exists() else []

    per_capture: List[Dict[str, Any]] = []
    stability_samples: List[StabilitySample] = []
    t0: Optional[float] = None
    for path in capture_paths:
        record: Dict[str, Any] = {"source_file": str(path), "valid": False}
        try:
            iq, attributes = read_capture_iq(str(path))
            sample_rate_hz = float(attributes["sample_rate_hz"])
            center_frequency_hz = float(attributes["center_frequency_hz"])
            stats = compute_sample_statistics(iq)
            clipping = evaluate_clipping(stats, thresholds)
            bandpass = compute_bandpass(iq, sample_rate_hz, center_frequency_hz)
            record.update(valid=True, sample_statistics=stats.to_dict(), clipping=clipping.to_dict(),
                          usable_band_fraction=bandpass.usable_band_fraction,
                          attributes={k: v for k, v in attributes.items() if isinstance(v, (str, int, float, bool))})
            created_at = attributes.get("created_at") or attributes.get("capture_start_utc")
            if created_at:
                try:
                    ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).timestamp()
                    if t0 is None:
                        t0 = ts
                    power = (stats.std_i ** 2 + stats.std_q ** 2) / 2.0
                    stability_samples.append(StabilitySample(str(created_at), power, ts - t0))
                except ValueError:
                    pass
        except Exception as exc:
            record["reason"] = f"{type(exc).__name__}: {exc}"
        per_capture.append(record)

    stability_dict = None
    if len(stability_samples) >= 3:
        stability_dict = analyze_stability(stability_samples).to_dict()

    analysis_id = f"analysis-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}" + (f"-{label}" if label else "")
    analysis_dir = session_path / "analysis" / analysis_id
    analysis_dir.mkdir(parents=True, exist_ok=False)

    atomic_write_json(analysis_dir / "source_session.json", {
        "source_session_dir": str(session_path), "generated_utc": datetime.now(timezone.utc).isoformat(),
        "code_commit": _current_commit_hash(),
    })
    valid_count = sum(1 for r in per_capture if r["valid"])
    atomic_write_json(analysis_dir / "replay_result.json", {
        "n_captures_found": len(capture_paths), "n_captures_valid": valid_count,
        "per_capture": per_capture, "stability": stability_dict,
    })

    return CalibrationReplayResult(analysis_dir=str(analysis_dir), n_captures_found=len(capture_paths),
                                    n_captures_valid=valid_count, per_capture=per_capture, stability=stability_dict)
