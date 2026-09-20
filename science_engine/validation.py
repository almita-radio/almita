"""PREFLIGHT: offline-only gates and a memory/disk estimate BEFORE any
cube-sized allocation. A 4000x4000x8192 float64 cube (~1 TB) must be
caught here, not after it OOMs the Pi (a real 4.7 GB OOM-kill happened
on this machine when a campaign was run with the wrong beam).

`run_science_session` calls this itself, before allocating - `plan` is a
convenience view of the same checks, never the only gate.

Estimates are per-voxel byte counts, MEASURED on this Pi (see
docs/SCIENCE_ACCEPTANCE.md, "Memory"), not guessed:

  disk    cube: relative_intensity, uncertainty, weight_sum (float64) + n_pointings (int64) + valid (bool)
          = 33 B/voxel, plus three small 2D maps
  RAM     GriddingAccumulator holds 4 (Nv,Ny,Nx) arrays (3 x float64 + 1 x int64 = 32 B/voxel);
          finalize() adds value + variance + uncertainty (24 B/voxel); the valid mask is 1 B/voxel;
          add_point() works in channel blocks so its temporaries are negligible.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from science_engine.config import ScienceConfig
from science_engine.models import ScienceGrid, ScienceInput

DISK_BYTES_PER_VOXEL = 33
PEAK_RAM_BYTES_PER_VOXEL = 57          # 4 accumulator arrays (32) + finalize value/variance/uncertainty (24) + valid (1)
PEAK_RAM_BASELINE_BYTES = 100 * 1024 ** 2   # interpreter + numpy + h5py + astropy import: measured 79 MB RSS
JSON_OVERHEAD_BYTES = 1_000_000   # attrs (build_info, grid, beam JSON), velocity axis, 2D maps
MEMORY_FRACTION_OF_AVAILABLE = 0.6      # never plan to use more than this fraction of MemAvailable (swap is not counted)


def n_voxels(grid: ScienceGrid, n_velocity_channels: int) -> int:
    return int(grid.nx) * int(grid.ny) * int(n_velocity_channels)


def estimate_output_bytes(grid: ScienceGrid, n_velocity_channels: int) -> int:
    return n_voxels(grid, n_velocity_channels) * DISK_BYTES_PER_VOXEL + JSON_OVERHEAD_BYTES


INPUT_BYTES_PER_POINT_CHANNEL = 48     # ingest keeps 6 x 8-byte arrays per point (frequency, velocity, value, sigma, mask, n_contributing)


def estimate_peak_memory_bytes(grid: ScienceGrid, n_velocity_channels: int, n_points: int = 0) -> int:
    """Measured (docs/SCIENCE_ACCEPTANCE.md): 57.1-57.5 B/voxel on 3.6M-20.5M voxel cubes; the Level 1 spectra held by
    ingest add 48 B per point-channel (a real 100-point campaign measured 1227 MB against 1213 MB before this term)."""
    return (n_voxels(grid, n_velocity_channels) * PEAK_RAM_BYTES_PER_VOXEL + PEAK_RAM_BASELINE_BYTES
            + n_points * n_velocity_channels * INPUT_BYTES_PER_POINT_CHANNEL)


def available_memory_bytes() -> Optional[int]:
    """MemAvailable from /proc/meminfo (Linux); None when unavailable. Swap is deliberately NOT counted."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


class SciencePreflightBlocked(Exception):
    """Raised by run_science_session BEFORE any cube-sized allocation when a preflight check fails."""


def run_preflight(science_input: ScienceInput, grid: ScienceGrid, n_velocity_channels: int, config: ScienceConfig,
                  *, output_root: str | Path, max_memory_bytes: int = 3 * 1024 ** 3) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("has_input_points", len(science_input.points) > 0, f"{len(science_input.points)} points"))

    peak = estimate_peak_memory_bytes(grid, n_velocity_channels, len(science_input.points))
    available = available_memory_bytes()
    budget = max_memory_bytes if available is None else min(max_memory_bytes,
                                                            int(MEMORY_FRACTION_OF_AVAILABLE * available))
    checks.append(Check(
        "memory_estimate_within_budget", peak <= budget,
        f"estimated peak RAM ~{peak / 1024**2:.1f} MB for grid {grid.ny}x{grid.nx} x {n_velocity_channels} channels "
        f"({n_voxels(grid, n_velocity_channels):,} voxels); budget {budget / 1024**2:.1f} MB "
        f"(min of {max_memory_bytes / 1024**2:.0f} MB cap and {int(MEMORY_FRACTION_OF_AVAILABLE * 100)}% of "
        f"MemAvailable {('%.0f MB' % (available / 1024**2)) if available is not None else 'unknown'})"))

    output_bytes = estimate_output_bytes(grid, n_velocity_channels)
    output_root = Path(output_root)
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        writable, detail = True, str(output_root)
        free = shutil.disk_usage(output_root).free
        checks.append(Check("disk_space_sufficient", free >= 2 * output_bytes,
                            f"estimated output ~{output_bytes / 1024**2:.1f} MB, free {free / 1024**2:.0f} MB "
                            f"(need 2x headroom)"))
    except OSError as error:
        writable, detail = False, str(error)
    checks.append(Check("output_writable", writable, detail))
    return checks


def blocking_reason(checks: list[Check]):
    failed = [c for c in checks if not c.ok]
    if not failed:
        return None
    return "; ".join(f"{c.name}: {c.detail}" for c in failed)
