"""Replay mode (Fase 6-8): re-analyze an existing HI session's RAW evidence
without touching mount or SDR - and without ever overwriting the original,
immutable observation. Each replay is a new, separately versioned
`analyses/analysis-<UTC timestamp>/` directory under the SAME session,
carrying its own config/commit/results - the raw session directory itself
is never written to by this module (see `replay_session`'s docstring for
the exact list of files it reads and the exact list it writes).

This exists because a real observation (mount pointed somewhere, SDR
samples actually taken) is a physical fact that cannot be re-created - but
almost everything AFTER that (spectral reduction, fit, quality, bootstrap)
is pure computation over already-recorded raw IQ, and will legitimately
need to be re-run as the baseline method / RFI masks / quality thresholds
/ fitter / beam model evolve. "Same raw session, analysis v1/v2/v3" is the
model; this module is the "vN" side of that, never the raw side.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from astropy.coordinates import EarthLocation, SkyCoord
import astropy.units as u

from alignment import offset_coordinates
from alignment_engine.hi.acquisition import _compute_spectrum_from_iq_file
from alignment_engine.hi.fits_reference import FITSMomentMapProvider
from alignment_engine.hi.quality_v2 import evaluate_quality_v2
from alignment_engine.hi.reference_trust import ReferenceManifest
from alignment_engine.hi.spectral_pipeline import SpectralPipelineConfig, compute_spectral_metric
from alignment_engine.fitting import fit_raster
from runtime_state import atomic_write_json


def _current_commit_hash() -> Optional[str]:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                 cwd=Path(__file__).resolve().parents[2], timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class ReplayResult:
    analysis_dir: str
    valid_count: int
    total_count: int
    fit: Optional[dict]
    quality: Optional[dict]

    def to_dict(self) -> dict:
        return {"analysis_dir": self.analysis_dir, "valid_count": self.valid_count,
                "total_count": self.total_count, "fit": self.fit, "quality": self.quality}


def replay_session(session_dir: str, *, pipeline_config: Optional[SpectralPipelineConfig] = None,
                    bootstrap_iterations: int = 20, stride: Optional[int] = None,
                    label: Optional[str] = None) -> ReplayResult:
    """Reads ONLY: <session_dir>/alignment_config.json (fits_path,
    manifest_path, beam_fwhm_deg, pixel_catalog_stride, raster geometry),
    <session_dir>/target.json (the resolved center), <session_dir>/raw_grid.json
    (which point indices were valid), and <session_dir>/points/point_NNNN.h5
    (the raw IQ - session.point_path()'s own naming convention) for every
    valid index. NEVER opens a mount or SDR connection. NEVER writes
    anything under <session_dir> itself - only under
    <session_dir>/analyses/analysis-<UTC timestamp>/, a fresh directory
    every call."""
    session_path = Path(session_dir)
    config = json.loads((session_path / "alignment_config.json").read_text())
    target = json.loads((session_path / "target.json").read_text())
    raw_grid = json.loads((session_path / "raw_grid.json").read_text())

    center = SkyCoord(ra=target["icrs_ra_hours"] * u.hourangle, dec=target["icrs_dec_deg"] * u.deg)
    manifest = ReferenceManifest.read(config["manifest_path"])
    provider = FITSMomentMapProvider(config["fits_path"], manifest, require_validated=True)
    effective_stride = stride if stride is not None else config.get("pixel_catalog_stride", 10)
    template = provider.template_for(center, config["beam_fwhm_deg"], stride=effective_stride)

    points = raw_grid["points"]
    positions = offset_coordinates(center, [p["east_deg"] for p in points], [p["north_deg"] for p in points])
    pipeline_config = pipeline_config or SpectralPipelineConfig(
        line_window_km_s=tuple(config.get("velocity_window_km_s", [-100.0, 100.0])),
        baseline_exclusion_km_s=tuple(config.get("velocity_window_km_s", [-100.0, 100.0])))

    values = []
    reduction_details = []
    for i, point in enumerate(points):
        raw_path = session_path / "points" / f"point_{i:04d}.h5"
        if not raw_path.exists():
            values.append(None)
            reduction_details.append({"index": i, "valid": False, "reason": "no raw IQ file at this index "
                                                                              "(point was skipped/not attempted)"})
            continue
        try:
            frequency_hz, power = _compute_spectrum_from_iq_file(str(raw_path))
            rest_freq_hz = 1_420_405_751.77
            velocity_km_s = (rest_freq_hz - frequency_hz) / rest_freq_hz * 299792.458
            metric_result = compute_spectral_metric(velocity_km_s, power, pipeline_config)
            values.append(metric_result.metric if metric_result.metric_valid else None)
            reduction_details.append({"index": i, "valid": metric_result.metric_valid,
                                       "reason": metric_result.reason})
        except Exception as exc:
            values.append(None)
            reduction_details.append({"index": i, "valid": False, "reason": f"{type(exc).__name__}: {exc}"})

    valid_count = sum(1 for v in values if v is not None)

    fit_dict = None
    quality_dict = None
    if valid_count >= 8:
        fit = fit_raster(positions, values, center, template, span_deg=config["raster_span_deg"])
        from alignment_engine.scan_planner import ScanPoint
        scan_points = [ScanPoint(index=i, row=p.get("row", 0), col=p.get("col", 0),
                                  east_deg=p["east_deg"], north_deg=p["north_deg"]) for i, p in enumerate(points)]
        quality = evaluate_quality_v2(scan_points, values, positions, center, template, fit,
                                       config["raster_span_deg"], run_bootstrap=True,
                                       bootstrap_iterations=bootstrap_iterations)
        fit_dict = fit.to_dict()
        quality_dict = quality.to_dict()

    analysis_id = f"analysis-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}" + (f"-{label}" if label else "")
    analysis_dir = session_path / "analyses" / analysis_id
    analysis_dir.mkdir(parents=True, exist_ok=False)

    atomic_write_json(analysis_dir / "source_session.json", {
        "source_session_dir": str(session_path), "generated_utc": datetime.now(timezone.utc).isoformat(),
        "code_commit": _current_commit_hash(),
    })
    atomic_write_json(analysis_dir / "analysis_config.json", {
        "beam_fwhm_deg": config["beam_fwhm_deg"], "pixel_catalog_stride": effective_stride,
        "raster_span_deg": config["raster_span_deg"],
        "spectral_pipeline": {"line_window_km_s": list(pipeline_config.line_window_km_s),
                               "baseline_exclusion_km_s": list(pipeline_config.baseline_exclusion_km_s),
                               "baseline_order": pipeline_config.baseline_order},
        "bootstrap_iterations": bootstrap_iterations,
        "fits_path": config["fits_path"], "manifest_path": config["manifest_path"],
    })
    atomic_write_json(analysis_dir / "reduction_details.json", {"points": reduction_details, "values": values})
    if fit_dict is not None:
        atomic_write_json(analysis_dir / "fit_result.json", fit_dict)
        atomic_write_json(analysis_dir / "quality.json", quality_dict)
    atomic_write_json(analysis_dir / "analysis_result.json", {
        "valid_count": valid_count, "total_count": len(points), "fit": fit_dict, "quality": quality_dict,
    })

    return ReplayResult(analysis_dir=str(analysis_dir), valid_count=valid_count, total_count=len(points),
                         fit=fit_dict, quality=quality_dict)
