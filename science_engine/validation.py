"""VALIDATE / PREFLIGHT (sections 118-119): offline-only gates and a
memory/disk estimate BEFORE allocating the cube - a 4000x4000x8192
float64 cube (~1TB) must be caught here, not after it OOMs the Pi
(section 119, 123).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from science_engine.config import ScienceConfig
from science_engine.models import ScienceGrid, ScienceInput

BYTES_PER_VOXEL = 8 * 5  # relative_intensity, uncertainty, weight_sum, n_contributing(int64), valid(bool~1B, rounds to 8)
JSON_OVERHEAD_BYTES = 5_000


def estimate_output_bytes(grid: ScienceGrid, n_velocity_channels: int) -> int:
    return grid.nx * grid.ny * n_velocity_channels * BYTES_PER_VOXEL + JSON_OVERHEAD_BYTES


def estimate_peak_memory_bytes(grid: ScienceGrid, n_velocity_channels: int) -> int:
    """The GriddingAccumulator holds 4 (Nv,Ny,Nx) float64/int64 arrays at
    once (weighted_value_sum, weight_sum, weight_sq_sigma_sq_sum,
    n_contributing) plus the finalized outputs (~4 more, transient) -
    ~8x a single cube array's size is a safe upper bound."""
    single_array_bytes = grid.nx * grid.ny * n_velocity_channels * 8
    return 8 * single_array_bytes


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def run_preflight(science_input: ScienceInput, grid: ScienceGrid, n_velocity_channels: int, config: ScienceConfig,
                  *, output_root: str | Path, max_memory_bytes: int = 3 * 1024 ** 3) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("has_input_points", len(science_input.points) > 0,
                        f"{len(science_input.points)} points"))

    peak_memory = estimate_peak_memory_bytes(grid, n_velocity_channels)
    checks.append(Check("memory_estimate_within_budget", peak_memory <= max_memory_bytes,
                        f"estimated peak ~{peak_memory / 1024**2:.1f} MB "
                        f"(grid {grid.ny}x{grid.nx}, {n_velocity_channels} channels), budget "
                        f"{max_memory_bytes / 1024**2:.1f} MB"))

    output_bytes = estimate_output_bytes(grid, n_velocity_channels)
    checks.append(Check("output_size_estimated", True, f"~{output_bytes / 1024**2:.1f} MB"))

    output_root = Path(output_root)
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        writable, detail = True, str(output_root)
    except OSError as error:
        writable, detail = False, str(error)
    checks.append(Check("output_writable", writable, detail))

    return checks


def blocking_reason(checks: list[Check]):
    failed = [c for c in checks if not c.ok]
    if not failed:
        return None
    return "; ".join(f"{c.name}: {c.detail}" for c in failed)
