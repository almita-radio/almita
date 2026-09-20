"""SCIENCE pipeline orchestration:

INGEST -> BUILD BEAM/GRID -> PREFLIGHT (blocks BEFORE any cube-sized allocation) -> CREATE SESSION
(fail-closed on collision) -> GRID SPECTRA INTO CUBE -> INTEGRATED MAP -> MOMENT-LIKE PRODUCTS -> QC ->
PERSIST (atomic HDF5) -> INDEX + MANIFEST

Two SEPARATE status axes (never one ambiguous word):

  status             pipeline EXECUTION state: RUNNING -> COMPLETED | FAILED | CANCELLED
  data_completeness  scientific/data completeness of the INPUT campaign: COMPLETE | PARTIAL
                     (a PARTIAL REDUCE session can be processed to COMPLETED execution while the
                      science product is still honestly marked PARTIAL)

Crash safety: the session directory is created BEFORE any computation and its manifest starts as RUNNING.
If anything raises, the manifest is rewritten FAILED (or CANCELLED for KeyboardInterrupt) with the error
and the exception propagates - a session is never left looking COMPLETED. HDF5 products are written to a
temporary name and renamed only after a clean close, so no half-written canonical cube can exist.
"""
from __future__ import annotations

import resource
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from science_engine.config import ScienceConfig
from science_engine.cube import build_cube, canonical_velocity_axis
from science_engine.grid import build_beam_model, build_grid
from science_engine.ingest import load_science_input
from science_engine.integration import integrated_map
from science_engine.moments import moment1_like, moment2_like
from science_engine.models import SCIENCE_SCHEMA_VERSION, ScienceInput
from science_engine.provenance import build_run_provenance, close_run_provenance, reduce_manifest_hash
from science_engine.quality import assess_science_quality
from science_engine.storage import ScienceSession, arrays_sha256, sha256_file
from science_engine.validation import (SciencePreflightBlocked, blocking_reason, estimate_output_bytes,
                                       run_preflight)

DATA_LOSS_REASON_CODES = ("INPUT_PARTIAL", "POINTS_EXCLUDED", "SPECTRAL_COVERAGE_LOW", "INTEGRATION_WINDOW_PARTIAL",
                          "LOW_SPATIAL_COVERAGE")


@dataclass
class ScienceSessionReport:
    status: str  # pipeline EXECUTION state: COMPLETED (run_science_session raises instead of returning FAILED)
    data_completeness: str  # scientific/data completeness of the INPUT campaign: COMPLETE | PARTIAL
    session_id: str
    campaign_id: str
    n_input_points: int
    grid_shape: tuple[int, int]
    n_velocity_channels: int
    quality_state: str
    quality_reasons: list[str]
    runtime_seconds: float
    output_dir: str
    input_reduce_session_id: str = ""
    n_points_used: int = 0
    n_points_excluded: int = 0
    beam: dict[str, Any] = field(default_factory=dict)
    velocity_range_m_s: tuple[float, float] = (float("nan"), float("nan"))
    velocity_resampled: bool = False
    max_velocity_offset_channels: float = 0.0
    integrated_window_m_s: tuple[float, float] = (float("nan"), float("nan"))
    valid_map_fraction: float = float("nan")
    median_uncertainty: float = float("nan")
    peak_rss_mb: float = float("nan")
    limitations: list[str] = field(default_factory=list)

    def human_summary_lines(self) -> list[str]:
        b = self.beam
        return [
            f"SCIENCE {self.status}",
            f"Input REDUCE:      {self.input_reduce_session_id}",
            f"Campaign:          {self.campaign_id}",
            f"Session:           {self.session_id}",
            f"Data completeness: {self.data_completeness}",
            f"Input points:      {self.n_input_points}",
            f"Used points:       {self.n_points_used}",
            f"Excluded points:   {self.n_points_excluded}",
            f"Beam:              {b.get('model_type')} FWHM={b.get('fwhm_deg')} deg  status={b.get('status')}  "
            f"source={b.get('source')}  (operator-provided operational metadata)",
            f"Grid:              {self.grid_shape[0]}x{self.grid_shape[1]} pixels",
            f"Cube:              {self.n_velocity_channels} x {self.grid_shape[0]} x {self.grid_shape[1]} "
            f"[velocity, y, x]",
            f"Velocity range:    {self.velocity_range_m_s[0]:.0f} .. {self.velocity_range_m_s[1]:.0f} m/s (LSRK, ascending)",
            f"Velocity resampled: {'yes' if self.velocity_resampled else 'no'} "
            f"(max point-to-point offset {self.max_velocity_offset_channels:.1f} channels)",
            f"Integrated window: {self.integrated_window_m_s[0]:.0f} .. {self.integrated_window_m_s[1]:.0f} m/s",
            f"Valid map fraction: {self.valid_map_fraction:.3f}",
            f"Median uncertainty: {self.median_uncertainty:.4g} (relative_intensity units)",
            f"Quality:           {self.quality_state}",
            *[f"  - {r}" for r in self.quality_reasons],
            *([f"Limitations:       {', '.join(self.limitations)}"] if self.limitations else []),
            f"Runtime:           {self.runtime_seconds:.2f}s",
            f"Peak RSS:          {self.peak_rss_mb:.0f} MB",
            f"Output:            {self.output_dir}",
        ]


def _product_status(valid_fraction: float, quality_reasons: list[str], quality_state: str = "GOOD") -> tuple[str, list[str]]:
    """BLOCKED when there is no valid data at all; PARTIAL when a documented data-loss condition applies;
    otherwise VALID. Reasons are the applicable quality reasons, never empty for BLOCKED/PARTIAL."""
    if quality_state == "BAD":
        return "BLOCKED", [r for r in quality_reasons if r.split(":")[0].isupper()] or ["session quality is BAD"]
    if not valid_fraction > 0:
        return "BLOCKED", ["no valid voxel/pixel in this product"]
    lost = [r for r in quality_reasons if r.split(":")[0] in DATA_LOSS_REASON_CODES]
    return ("PARTIAL", lost) if lost else ("VALID", [])


def _index_entry(session: ScienceSession, product_id: str, kind: str, path: Path, shape, unit: str,
                 status: str, reasons: list[str]) -> dict[str, Any]:
    return {"id": product_id, "kind": kind, "path": str(path.relative_to(session.dir)), "shape": list(shape),
            "unit": unit, "status": status, "reasons": reasons, "sha256": sha256_file(path),
            "arrays_sha256": arrays_sha256(path)}


def _peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0    # Linux: KiB


def run_science_session(reduce_session_dir: str, config: ScienceConfig, *, output_root: str,
                        max_memory_bytes: int = 3 * 1024 ** 3) -> ScienceSessionReport:
    t0 = time.monotonic()
    timings: dict[str, float] = {}
    started_utc = datetime.now(timezone.utc).isoformat()

    def stage(name: str, since: float) -> float:
        now = time.monotonic()
        timings[name] = timings.get(name, 0.0) + (now - since)
        return now

    mark = time.monotonic()
    science_input: ScienceInput = load_science_input(reduce_session_dir)  # ScienceContractError: never proceeds
    mark = stage("ingest_validate", mark)

    data_completeness = ("COMPLETE" if science_input.reduce_session_status == "COMPLETED"
                         and not science_input.exclusions else "PARTIAL")

    beam = build_beam_model(config)
    grid = build_grid(science_input, beam, config)
    n_channels = int(canonical_velocity_axis(science_input).shape[0])
    mark = stage("grid_and_axis", mark)

    # PREFLIGHT INSIDE the run: blocks before any cube-sized allocation and before a session dir exists.
    checks = run_preflight(science_input, grid, n_channels, config, output_root=output_root,
                           max_memory_bytes=max_memory_bytes)
    reason = blocking_reason(checks)
    if reason is not None:
        raise SciencePreflightBlocked(reason)

    session = ScienceSession(output_root, science_input.campaign_id)   # FileExistsError on collision (fail closed)
    manifest_hash = reduce_manifest_hash(reduce_session_dir)
    base_manifest = {
        "science_schema_version": SCIENCE_SCHEMA_VERSION, "campaign_id": science_input.campaign_id,
        "science_session_id": session.session_id, "input_reduce_session_id": science_input.reduce_session_id,
        "input_reduce_session_dir": str(reduce_session_dir),
        "input_reduce_schema_version": science_input.reduce_schema_version,
        "input_reduce_session_status": science_input.reduce_session_status,
        "input_reduce_manifest_sha256": manifest_hash, "config_hash": config.config_hash(),
        "data_completeness": data_completeness, "beam": beam.to_dict(), "grid": grid.to_dict(),
    }
    session.write_config(config.to_dict())
    session.write_manifest({**base_manifest, "status": "RUNNING", "products": []})
    session.log_event("SCIENCE_SESSION_BEGIN", campaign_id=science_input.campaign_id,
                      reduce_session_id=science_input.reduce_session_id, preflight=[c.to_dict() for c in checks])

    try:
        provenance = build_run_provenance(config, reduce_session_dir=reduce_session_dir,
                                          reduce_session_id=science_input.reduce_session_id,
                                          reduce_schema_version=science_input.reduce_schema_version,
                                          started_utc=started_utc)
        mark = stage("provenance_init", mark)
        cube = build_cube(science_input, grid, beam, config)
        mark = stage("cube_total", mark)
        moment0 = integrated_map(cube, config)
        moment1 = moment1_like(cube, config, moment0)
        moment2 = moment2_like(cube, config, moment1)
        mark = stage("integrated_products", mark)
        quality = assess_science_quality(science_input, cube, config, moment0)
        mark = stage("qc", mark)

        info = cube.build_info
        used = set(info["used_point_indices"])
        excluded_reasons = {e["point_index"]: e["reason"] for e in info["excluded"]}
        provenance["input_points"] = [
            {"point_index": p.point_index, "used_in_cube": p.point_index in used,
             "exclusion_reason": excluded_reasons.get(p.point_index),
             "reduce_quality_state": p.reduce_quality_state, "master_spectrum_h5_sha256": p.source_h5_sha256,
             "master_spectrum_json_sha256": p.source_json_sha256, "capture_refs": p.capture_refs}
            for p in science_input.points]
        provenance["ingest_exclusions"] = science_input.exclusions

        self_description = dict(campaign_id=science_input.campaign_id,
                                reduce_session_id=science_input.reduce_session_id, beam=beam.to_dict(),
                                config_hash=config.config_hash(), quality_state=quality.state,
                                data_completeness=data_completeness, input_reduce_manifest_sha256=manifest_hash)

        products = []
        cube_status, cube_reasons = _product_status(float(np.mean(cube.valid)), quality.reasons, quality.state)
        cube_path = session.write_cube(cube, **self_description)
        products.append(_index_entry(session, "science_cube", "cube", cube_path, cube.relative_intensity.shape,
                                     "relative_intensity_dimensionless", cube_status, cube_reasons))
        for name, science_map in (("integrated_relative_intensity", moment0),
                                  ("moment1_like_velocity_centroid", moment1),
                                  ("moment2_like_velocity_dispersion", moment2)):
            status, reasons = _product_status(float(np.mean(science_map.valid)), quality.reasons, quality.state)
            path = session.write_map(science_map, name, **self_description)
            products.append(_index_entry(session, name, "map", path, science_map.value.shape, science_map.units,
                                         status, reasons))
        mark = stage("persist_products", mark)

        finite_unc = cube.uncertainty[np.isfinite(cube.uncertainty)]
        session.write_qc("science_quality", quality.to_dict())
        session.write_qc("resample_qc", {**info["resample"], "canonical_velocity_axis": info["canonical_velocity_axis"],
                                          "n_nonfinite_rejected": info["n_nonfinite_rejected"],
                                          "sigma_local_check": info["sigma_local_check"],
                                          "cube_stage_seconds": info["timings_s"]})
        session.write_qc("coverage", {
            "pointing_count_max": int(cube.n_pointings.max()),
            "weight_sum_max": float(cube.weight_sum.max()),
            "spatial_coverage_fraction": quality.metrics["spatial_coverage_fraction"],
            "valid_voxel_fraction": quality.metrics["valid_voxel_fraction"],
            "valid_map_fraction": quality.metrics.get("valid_map_fraction"),
            "median_spectral_coverage_fraction": quality.metrics.get("median_spectral_coverage_fraction"),
            "median_uncertainty": float(np.median(finite_unc)) if finite_unc.size else None,
            "effective_integration": "not produced in V1 (see SCIENCE_MODEL.md coverage semantics)",
            "estimated_output_bytes": estimate_output_bytes(grid, n_channels),
        })

        runtime_seconds = time.monotonic() - t0
        peak_rss_mb = _peak_rss_mb()
        velocity = cube.velocity_lsrk_m_s
        manifest = {
            **base_manifest, "status": "COMPLETED", "n_input_points": len(science_input.points),
            "n_points_in_reduce_manifest": science_input.n_points_in_manifest,
            "n_points_used": info["n_points_used"], "n_points_excluded": info["n_points_excluded"],
            "n_velocity_channels": n_channels,
            "velocity": {"frame": "lsrk", "unit": "m/s", "order": "ascending", "n_channels": n_channels,
                         "min_m_s": float(velocity[0]), "max_m_s": float(velocity[-1]),
                         "channel_width_m_s": info["canonical_velocity_axis"]["channel_width_m_s"],
                         "resampled": info["resample"]["n_points_resampled"] > 0,
                         "max_point_offset_channels": info["resample"]["max_abs_offset_channels"]},
            "integrated_window_m_s": [config.velocity_window_min_m_s, config.velocity_window_max_m_s],
            "quality": quality.to_dict(), "products": products,
            "runtime": {"runtime_seconds": runtime_seconds, "peak_rss_mb": peak_rss_mb, "stage_seconds": timings},
            "provenance_file": "provenance.json",
        }
        session.write_provenance(close_run_provenance(provenance))
        session.write_manifest(manifest)      # LAST: its presence with status COMPLETED is the commit point
        session.log_event("SCIENCE_SESSION_END", status="COMPLETED", runtime_seconds=runtime_seconds)
    except BaseException as error:            # incl. KeyboardInterrupt: the session must never look COMPLETED
        status = "CANCELLED" if isinstance(error, KeyboardInterrupt) else "FAILED"
        session.write_manifest({**base_manifest, "status": status, "products": [],
                                "error": f"{type(error).__name__}: {error}"})
        session.log_event("SCIENCE_SESSION_END", status=status, error=f"{type(error).__name__}: {error}")
        raise

    return ScienceSessionReport(
        status="COMPLETED", data_completeness=data_completeness, session_id=session.session_id,
        campaign_id=science_input.campaign_id, n_input_points=len(science_input.points),
        grid_shape=(grid.ny, grid.nx), n_velocity_channels=n_channels, quality_state=quality.state,
        quality_reasons=quality.reasons, runtime_seconds=runtime_seconds, output_dir=str(session.dir),
        input_reduce_session_id=science_input.reduce_session_id, n_points_used=info["n_points_used"],
        n_points_excluded=info["n_points_excluded"], beam=beam.to_dict(),
        velocity_range_m_s=(float(velocity[0]), float(velocity[-1])),
        velocity_resampled=info["resample"]["n_points_resampled"] > 0,
        max_velocity_offset_channels=info["resample"]["max_abs_offset_channels"],
        integrated_window_m_s=(config.velocity_window_min_m_s, config.velocity_window_max_m_s),
        valid_map_fraction=quality.metrics.get("valid_map_fraction", float("nan")),
        median_uncertainty=quality.metrics.get("median_uncertainty", float("nan")), peak_rss_mb=peak_rss_mb,
        limitations=quality.limitations,
    )
