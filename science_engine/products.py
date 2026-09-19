"""SCIENCE pipeline orchestration (section 141's canonical order):

INGEST -> VALIDATE CONTRACT -> BUILD BEAM/GRID -> VALIDATE VELOCITY ->
GRID SPECTRA INTO CUBE -> DERIVE INTEGRATED MAP -> DERIVE MOMENT-LIKE
PRODUCTS -> QC -> PERSIST -> VALIDATE OUTPUT

Crash safety (sections 110-112): an exception propagates BEFORE
manifest.json is written, exactly like reduce_engine.pipeline's own
crash-recovery contract - a session directory can exist with partial
products on disk, but is never reported COMPLETED. A KeyboardInterrupt
is not caught here either, for the same reason.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from science_engine.config import ScienceConfig
from science_engine.cube import build_cube
from science_engine.grid import build_beam_model, build_grid
from science_engine.ingest import load_science_input
from science_engine.integration import integrated_map
from science_engine.moments import moment1_like, moment2_like
from science_engine.models import ScienceInput
from science_engine.provenance import build_run_provenance, close_run_provenance
from science_engine.quality import assess_science_quality
from science_engine.storage import ScienceSession, new_science_session_id
from science_engine.validation import estimate_output_bytes


@dataclass
class ScienceSessionReport:
    status: str  # COMPLETED | FAILED
    session_id: str
    campaign_id: str
    n_input_points: int
    grid_shape: tuple[int, int]
    n_velocity_channels: int
    quality_state: str
    quality_reasons: list[str]
    runtime_seconds: float
    output_dir: str

    def human_summary_lines(self) -> list[str]:
        return [
            f"SCIENCE {self.status}",
            f"Campaign:      {self.campaign_id}",
            f"Session:       {self.session_id}",
            f"Input points:  {self.n_input_points}",
            f"Grid:          {self.grid_shape[0]}x{self.grid_shape[1]} pixels",
            f"Velocity bins: {self.n_velocity_channels}",
            f"Quality:       {self.quality_state}",
            *[f"  - {r}" for r in self.quality_reasons],
            f"Runtime:       {self.runtime_seconds:.2f}s",
            f"Output:        {self.output_dir}",
        ]


def run_science_session(reduce_session_dir: str, config: ScienceConfig, *, output_root: str) -> ScienceSessionReport:
    t0 = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()

    science_input: ScienceInput = load_science_input(reduce_session_dir)  # raises ScienceContractError, never proceeds past it

    beam = build_beam_model(config)
    grid = build_grid(science_input, beam, config)
    cube = build_cube(science_input, grid, beam, config)
    moment0 = integrated_map(cube, config)
    moment1 = moment1_like(cube, config)
    moment2 = moment2_like(cube, config)
    quality = assess_science_quality(science_input, cube, config)

    session = ScienceSession(output_root, science_input.campaign_id)
    session.write_config(config.to_dict())
    provenance = build_run_provenance(config, reduce_session_dir=reduce_session_dir,
                                      reduce_session_id=science_input.reduce_session_id,
                                      reduce_schema_version=science_input.reduce_schema_version,
                                      started_utc=started_utc)
    session.log_event("SCIENCE_SESSION_BEGIN", campaign_id=science_input.campaign_id,
                      reduce_session_id=science_input.reduce_session_id)

    cube_path = session.write_cube(cube, campaign_id=science_input.campaign_id,
                                   reduce_session_id=science_input.reduce_session_id)
    products = [{"id": "science_cube", "kind": "cube", "path": str(cube_path.relative_to(session.dir))}]
    for name, science_map in (("integrated_relative_intensity", moment0),
                              ("moment1_like_velocity_centroid", moment1),
                              ("moment2_like_velocity_dispersion", moment2)):
        path = session.write_map(science_map, name, campaign_id=science_input.campaign_id,
                                 reduce_session_id=science_input.reduce_session_id)
        products.append({"id": name, "kind": "map", "path": str(path.relative_to(session.dir))})

    session.write_qc("science_quality", quality.to_dict())
    session.write_qc("coverage", {
        "spatial_coverage_fraction": quality.metrics["spatial_coverage_fraction"],
        "estimated_output_bytes": estimate_output_bytes(grid, cube.velocity_lsrk_m_s.shape[0]),
    })

    runtime_seconds = time.monotonic() - t0
    manifest = {
        "science_schema_version": "1.0", "campaign_id": science_input.campaign_id,
        "science_session_id": session.session_id, "input_reduce_session_id": science_input.reduce_session_id,
        "input_reduce_session_dir": str(reduce_session_dir), "status": "COMPLETED",
        "n_input_points": len(science_input.points), "grid": grid.to_dict(),
        "beam": beam.to_dict(), "n_velocity_channels": int(cube.velocity_lsrk_m_s.shape[0]),
        "quality": quality.to_dict(), "runtime_seconds": runtime_seconds, "products": products,
    }
    session.write_manifest(manifest)
    session.write_provenance(close_run_provenance(provenance))
    session.log_event("SCIENCE_SESSION_END", status="COMPLETED", runtime_seconds=runtime_seconds)

    return ScienceSessionReport(
        status="COMPLETED", session_id=session.session_id, campaign_id=science_input.campaign_id,
        n_input_points=len(science_input.points), grid_shape=(grid.ny, grid.nx),
        n_velocity_channels=int(cube.velocity_lsrk_m_s.shape[0]), quality_state=quality.state,
        quality_reasons=quality.reasons, runtime_seconds=runtime_seconds, output_dir=str(session.dir),
    )
