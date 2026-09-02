#!/usr/bin/env python3
"""ANTENNA B / RFI_REF occupancy-quality map: NOT a sky brightness map.

RTL-SDR V3 (RFI_REF) is a fixed reference dipole. It does not perform
Antenna A's pointing geometry, so it can never produce a genuine sky
mosaic of its own. This module instead maps a contamination/quality
diagnostic - "how occupied was the RFI environment while this MAIN point
was being observed?" - onto Antenna A's own native grid geometry, reusing
quicklook_map.load_native_grid_geometry/read_cell_status unchanged so the
cell layout is byte-identical to Antenna A's map.

No interpolation, no smoothing, no invented cells, no active cancellation,
and never a blind subtraction of B from A - this is a diagnostic overlay,
built entirely from RFI_REF's own bounded scalar history
(rfi_ref_history.json, already computed by the existing ~5% FFT path) and
MAIN's own per-point capture-window timestamps (mosaic.csv, the same file
Antenna A's map already reads). Association between a MAIN point and RFI
samples is purely by overlapping wall-clock timestamps - no channel-level
coincidence yet; this only lays the data-model groundwork for that later.
"""
from __future__ import annotations

import csv
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from runtime_state import read_json_safe

METRIC_KEYS = (
    "mean_occupancy_fraction", "max_occupancy_fraction", "median_occupancy_fraction",
    "max_clipping_fraction", "max_peak_dbfs", "median_peak_dbfs", "n_observations",
)
DEFAULT_METRIC = "mean_occupancy_fraction"


def read_point_timing(grid_dir: Path) -> dict[str, dict[str, Any]]:
    """point_id -> {start_time, end_time, capture_status} from mosaic.csv -
    the same planning/manifest file quicklook_map.load_native_grid_geometry
    and read_cell_status already read, so point identity is guaranteed
    consistent with Antenna A's own map."""
    timing: dict[str, dict[str, Any]] = {}
    path = Path(grid_dir) / "mosaic.csv"
    if not path.is_file():
        return timing
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            point_id = row.get("point_id")
            if not point_id:
                continue
            timing[point_id] = {
                "start_time": row.get("start_time") or None,
                "end_time": row.get("end_time") or None,
                "capture_status": (row.get("capture_status") or "").strip().upper(),
            }
    return timing


def read_rfi_history(runtime_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """RFI_REF's own bounded sample history, gated by the canonical
    session_id - a history file left over from a different/older session is
    never correlated against this session's points."""
    history = read_json_safe(Path(runtime_dir) / "rfi_ref_history.json")
    if not history or history.get("session_id") != session_id:
        return []
    samples = history.get("samples")
    return samples if isinstance(samples, list) else []


def _parse_utc(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def correlate_rfi_with_points(point_timing: dict[str, dict[str, Any]],
                               rfi_samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each successfully-captured MAIN point with a valid
    [start_time, end_time] capture window, aggregate only the RFI samples
    whose own timestamp falls inside that window. No interpolation, no
    synthesized values: a point with zero overlapping RFI samples is simply
    absent from the returned dict (renders as NaN/blank, never a fake 0)."""
    parsed = [(ts, sample) for sample in rfi_samples if (ts := _parse_utc(sample.get("utc"))) is not None]

    metrics: dict[str, dict[str, Any]] = {}
    for point_id, timing in point_timing.items():
        if timing.get("capture_status") != "SUCCESS":
            continue
        start = _parse_utc(timing.get("start_time"))
        end = _parse_utc(timing.get("end_time"))
        if start is None or end is None or end < start:
            continue
        window = [s for ts, s in parsed if start <= ts <= end]
        occ = [float(s["occupancy_fraction"]) for s in window if s.get("occupancy_fraction") is not None]
        if not occ:
            continue
        clip = [float(s["clipping_fraction"]) for s in window if s.get("clipping_fraction") is not None]
        peak = [float(s["peak_dbfs"]) for s in window if s.get("peak_dbfs") is not None]
        metrics[point_id] = {
            "mean_occupancy_fraction": statistics.fmean(occ),
            "max_occupancy_fraction": max(occ),
            "median_occupancy_fraction": statistics.median(occ),
            "max_clipping_fraction": max(clip) if clip else None,
            "max_peak_dbfs": max(peak) if peak else None,
            "median_peak_dbfs": statistics.median(peak) if peak else None,
            "n_observations": len(window),
        }
    return metrics


def build_rfi_occupancy_map_document(geometry: dict[str, Any], point_metrics: dict[str, dict[str, Any]],
                                      cell_status_by_id: dict[str, str], session_id: str,
                                      metric_key: str = DEFAULT_METRIC) -> dict[str, Any]:
    """Same NATIVE_GRID contract as quicklook_map.build_native_grid_document:
    one cell per planned point, never more, never fewer, no spatial
    interpolation. Values are an RFI contamination diagnostic for MAIN's own
    grid geometry, never a brightness measurement from Antenna B itself."""
    rows, cols = geometry["rows"], geometry["columns"]
    value_grid = [[math.nan for _ in range(cols)] for _ in range(rows)]
    status_grid = [["UNKNOWN"] * cols for _ in range(rows)]
    points_json = []
    for point_id, cell in geometry["cells"].items():
        r, c = cell["row"], cell["column"]
        cell_status = cell_status_by_id.get(point_id, "UNKNOWN")
        metric = point_metrics.get(point_id)
        if metric is not None:
            value = metric.get(metric_key)
            if value is not None:
                value_grid[r][c] = float(value)
                cell_status = "SUCCESS"
        status_grid[r][c] = cell_status
        points_json.append({
            "point_id": point_id, "row": r, "column": c,
            "ra_deg": cell["ra_deg"], "dec_deg": cell["dec_deg"],
            "cell_status": cell_status, "rfi_metrics": metric,
        })
    finite = [v for row_v in value_grid for v in row_v if not math.isnan(v)]
    observed_cells = len(finite)
    lo, hi = (min(finite), max(finite)) if finite else (0.0, 1.0)
    if math.isclose(lo, hi):
        hi = lo + max(abs(lo) * 0.01, 1e-4)
    return {
        "schema_version": "1.0", "product": "RFI_OCCUPANCY_MAP", "map_mode": "NATIVE_GRID",
        "title": "ANTENNA B - RFI OCCUPANCY MAP",
        "created_utc": datetime.now(timezone.utc).isoformat(), "session_id": session_id,
        "grid_shape": {"rows": rows, "columns": cols, "total_cells": rows * cols},
        "metric": metric_key, "metric_units": "fraction (0-1)",
        "points": points_json,
        "grid": {
            "values": [[None if math.isnan(v) else v for v in row_v] for row_v in value_grid],
            "coverage_mask": [[not math.isnan(v) for v in row_v] for row_v in value_grid],
            "cell_status": status_grid,
            "method": "NATIVE_GRID_NO_INTERPOLATION",
        },
        "color_scale": {"minimum": lo, "maximum": hi},
        "quicklook_metrics": {"total_cells": rows * cols, "observed_cells": observed_cells,
            "coverage_fraction": observed_cells / (rows * cols) if rows * cols else 0.0},
        "known_limitations": [
            "RFI contamination diagnostic only - not a sky brightness or calibrated map",
            "Antenna B is a fixed reference dipole with no per-point pointing geometry of its own",
            "exact native grid - no spatial interpolation, no smoothing, no synthesized values",
            "association with MAIN points is by overlapping wall-clock timestamp only, "
            "no channel-level A/B coincidence yet",
            "no active cancellation or subtraction of B from A",
        ],
    }


def write_rfi_occupancy_png(path: Path, document: dict[str, Any]) -> None:
    grid = document["grid"]
    rows, cols = document["grid_shape"]["rows"], document["grid_shape"]["columns"]
    value_grid = np.array([[np.nan if v is None else v for v in row_values] for row_values in grid["values"]])
    status_grid = grid["cell_status"]
    cmap = matplotlib.colormaps["magma"].with_extremes(bad="#20262b")
    masked = np.ma.masked_invalid(value_grid)
    fig, axis = plt.subplots(figsize=(7, 6), constrained_layout=True)
    image = axis.imshow(masked, origin="lower", aspect="auto", cmap=cmap, interpolation="none",
                        vmin=document["color_scale"]["minimum"], vmax=document["color_scale"]["maximum"])
    failed_xy = [(c, r) for r in range(rows) for c in range(cols) if status_grid[r][c] == "FAILED"]
    deferred_xy = [(c, r) for r in range(rows) for c in range(cols) if status_grid[r][c] == "DEFERRED"]
    if failed_xy:
        axis.scatter(*zip(*failed_xy), marker="x", c="#65b7d8", s=45, linewidths=1.6, label="failed")
    if deferred_xy:
        axis.scatter(*zip(*deferred_xy), marker="o", facecolors="none", edgecolors="#65b7d8",
                     s=60, linewidths=1.2, label="deferred")
    axis.set(title="ANTENNA B - RFI OCCUPANCY MAP", xlabel="grid column", ylabel="grid row")
    metrics = document["quicklook_metrics"]
    axis.text(.01, .01, f"{metrics['observed_cells']}/{metrics['total_cells']} cells observed | "
                        f"metric={document['metric']}",
              transform=axis.transAxes, color="white", fontsize=8,
              bbox={"facecolor": "black", "alpha": .45, "edgecolor": "none"})
    if failed_xy or deferred_xy:
        axis.legend(loc="upper right", fontsize=8)
    fig.colorbar(image, ax=axis, label=f"{document['metric']} ({document['metric_units']})")
    fig.savefig(path, dpi=130)
    plt.close(fig)
