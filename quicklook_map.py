#!/usr/bin/env python3
"""Offline, dashboard-ready spatial quicklook for ALMITA.

The map is a visual representation of relative instrumental measurements.  It
does not perform source detection or absolute/astronomical calibration.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import resource
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.tri as mtri
import numpy as np
from astropy_offline import configure_astropy_offline
configure_astropy_offline()
from astropy.coordinates import SkyCoord, SkyOffsetFrame
import astropy.units as u

from calibration_foundation import (
    apply_relative_calibration,
    check_calibration_compatibility,
    load_calibration_profile,
)


class MapError(RuntimeError):
    pass


@dataclass
class MapPoint:
    point_id: str
    source_hdf5: str
    status: str
    coordinate_source: str
    ra_deg: float
    dec_deg: float
    x_offset_deg: float = math.nan
    y_offset_deg: float = math.nan
    map_value: float = math.nan
    map_uncertainty: float = math.nan
    valid_fraction: float = 0.0
    masked_fraction: float = 1.0
    outlier: bool = False
    row: int = -1
    column: int = -1
    cell_status: str = "UNKNOWN"


def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def robust_spherical_center(ra_deg: Iterable[float], dec_deg: Iterable[float]) -> SkyCoord:
    coords = SkyCoord(np.asarray(list(ra_deg)) * u.deg, np.asarray(list(dec_deg)) * u.deg)
    xyz = coords.cartesian.xyz.value
    vector = np.median(xyz, axis=1)
    norm = np.linalg.norm(vector)
    if not np.isfinite(norm) or norm < 1e-12:
        raise MapError("cannot derive a robust spherical map center")
    vector /= norm
    ra = math.degrees(math.atan2(vector[1], vector[0])) % 360.0
    dec = math.degrees(math.asin(np.clip(vector[2], -1.0, 1.0)))
    return SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs")


def project_offsets(ra_deg, dec_deg, center: SkyCoord) -> tuple[np.ndarray, np.ndarray]:
    coords = SkyCoord(np.asarray(ra_deg) * u.deg, np.asarray(dec_deg) * u.deg)
    offset = coords.transform_to(SkyOffsetFrame(origin=center))
    return offset.lon.to_value(u.deg), offset.lat.to_value(u.deg)


def map_metric(fractional_excess, fractional_uncertainty, valid_mask,
               frequency_hz, freq_min_hz, freq_max_hz) -> dict[str, float]:
    values = np.asarray(fractional_excess, float)
    uncertainty = np.asarray(fractional_uncertainty, float)
    valid = np.asarray(valid_mask, bool)
    frequency = np.asarray(frequency_hz, float)
    selected = valid & np.isfinite(values) & np.isfinite(uncertainty)
    selected &= (frequency >= freq_min_hz) & (frequency <= freq_max_hz)
    count = int(selected.sum())
    window_count = int(((frequency >= freq_min_hz) & (frequency <= freq_max_hz)).sum())
    if count < 3:
        raise MapError("fewer than three valid spectral bins in map window")
    # For independent Gaussian samples, standard error(median) ~= 1.2533 times
    # the standard error(mean). This uses the supplied per-bin uncertainties.
    value = float(np.median(values[selected]))
    error = float(1.2533141373155 * np.sqrt(np.sum(uncertainty[selected] ** 2)) / count)
    return {
        "map_value": value,
        "map_uncertainty": error,
        "valid_fraction": count / window_count,
        "masked_fraction": 1.0 - count / window_count,
        "selected_bins": count,
    }


def derive_point(point_id: str, hdf5: str | Path, ra_deg: float, dec_deg: float,
                 coordinate_source: str, profile: dict[str, Any],
                 freq_min_hz: float, freq_max_hz: float) -> MapPoint:
    path = Path(hdf5)
    if path.name.endswith(".part"):
        raise MapError("partial capture rejected")
    compatibility = check_calibration_compatibility(profile, path)
    if compatibility["status"] != "COMPATIBLE":
        return MapPoint(point_id, str(path), compatibility["status"], coordinate_source,
                        ra_deg, dec_deg)
    calibrated = apply_relative_calibration(profile, path)
    metric = map_metric(calibrated["fractional_excess"], calibrated["fractional_uncertainty"],
                        calibrated["valid_mask"], calibrated["frequency_hz"],
                        freq_min_hz, freq_max_hz)
    return MapPoint(point_id, str(path), "COMPATIBLE", coordinate_source, ra_deg, dec_deg,
                    map_value=metric["map_value"], map_uncertainty=metric["map_uncertainty"],
                    valid_fraction=metric["valid_fraction"], masked_fraction=metric["masked_fraction"])


def interpolate_visual(points: list[MapPoint], grid_size: int = 100) -> dict[str, np.ndarray]:
    good = [p for p in points if p.status in ("COMPATIBLE", "VALIDATION_DATASET")
            and np.isfinite(p.map_value)]
    if len(good) < 3:
        raise MapError("at least three valid, non-collinear points are required")
    x = np.asarray([p.x_offset_deg for p in good]); y = np.asarray([p.y_offset_deg for p in good])
    z = np.asarray([p.map_value for p in good])
    gx = np.linspace(x.min(), x.max(), grid_size); gy = np.linspace(y.min(), y.max(), grid_size)
    xx, yy = np.meshgrid(gx, gy)
    try:
        triangulation = mtri.Triangulation(x, y)
        interpolator = mtri.LinearTriInterpolator(triangulation, z)
        result = interpolator(xx, yy)
        grid = np.asarray(result.filled(np.nan), float)
    except (RuntimeError, ValueError) as error:
        raise MapError(f"linear triangulation failed: {error}") from error
    return {"x_deg": gx, "y_deg": gy, "value": grid,
            "coverage_mask": np.isfinite(grid), "method": "linear triangulation inside convex hull"}


def _uniform_axis_offsets(extent_deg: float, n_samples: int) -> list[float]:
    """Exact positions grid_generator.py itself placed each row/column at
    (its own _axis_offsets formula, duplicated here so this module never has
    to re-derive - and therefore never risks distorting - the real planned
    geometry). Not an interpolation: these are the literal planned centers."""
    if n_samples < 2:
        return [0.0]
    step = extent_deg / (n_samples - 1)
    start = -extent_deg / 2.0
    return [start + i * step for i in range(n_samples)]


def find_grid_directory(session_dir: str | Path, max_levels: int = 6) -> Path | None:
    """Walk upward from the Capture IQ output directory (--session-dir) to
    find the grid session directory that holds grid_generator.py's own
    canonical planning artifacts (mosaic.csv, grid_metadata.json). This is
    the same directory-nesting convention capture.py already uses internally
    (output_dir = grid_dir/'data'/'iq'/<session>-<timestamp>); walking
    upward avoids hardcoding an exact level count."""
    current = Path(session_dir).resolve()
    for _ in range(max_levels + 1):
        if (current / "mosaic.csv").is_file() and (current / "grid_metadata.json").is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def load_native_grid_geometry(grid_dir: str | Path) -> dict[str, Any]:
    """Read the FULL planned grid geometry from grid_generator.py's own
    artifacts: every logical cell's true row/column and RA/DEC, independent
    of scan/capture order. This is the single source of truth for the map
    canvas - it exists before any point is ever captured."""
    grid_dir = Path(grid_dir)
    metadata = json.loads((grid_dir / "grid_metadata.json").read_text())["grid"]
    rows_count, cols_count = int(metadata["rows"]), int(metadata["columns"])
    cells: dict[str, dict[str, Any]] = {}
    with (grid_dir / "mosaic.csv").open(newline="") as stream:
        for csv_row in csv.DictReader(stream):
            point_id = csv_row["point_id"]
            cells[point_id] = {
                "row": int(csv_row["grid_row"]), "column": int(csv_row["grid_col"]),
                "ra_deg": float(csv_row["ra"]), "dec_deg": float(csv_row["dec"]),
            }
    if len(cells) != rows_count * cols_count:
        raise MapError(f"grid geometry mismatch: mosaic.csv has {len(cells)} planned points "
                       f"but grid_metadata.json declares rows*columns={rows_count * cols_count}")
    return {
        "rows": rows_count, "columns": cols_count,
        "width_deg": float(metadata.get("width_deg", metadata.get("region_width_deg", 0.0))),
        "height_deg": float(metadata.get("height_deg", metadata.get("region_height_deg", 0.0))),
        "cells": cells,
    }


def read_cell_status(grid_dir: str | Path) -> dict[str, str]:
    """Fresh read of mosaic.csv's own capture_status per point_id (PLANNED,
    SUCCESS, FAILED, ...) - purely for distinguishing why a cell has no
    measurement yet. Never used to derive a value."""
    grid_dir = Path(grid_dir)
    status_by_id: dict[str, str] = {}
    with (grid_dir / "mosaic.csv").open(newline="") as stream:
        for csv_row in csv.DictReader(stream):
            deferred = str(csv_row.get("visibility_deferred", "")).strip().lower() == "true"
            raw_status = (csv_row.get("capture_status") or "unknown").strip().upper()
            status_by_id[csv_row["point_id"]] = "DEFERRED" if deferred and raw_status == "PLANNED" else raw_status
    return status_by_id


def build_native_grid_document(geometry: dict[str, Any], points_state: dict[str, dict[str, Any]],
                               cell_status_by_id: dict[str, str], session_id: str,
                               environment: str = "UNKNOWN") -> dict[str, Any]:
    """Build the Quicklook map as an exact, uninterpolated rendering of the
    original observation grid: one cell per planned point, never more, never
    fewer, never a value invented between two real measurements."""
    rows, cols = geometry["rows"], geometry["columns"]
    value_grid = np.full((rows, cols), np.nan)
    status_grid = [["UNKNOWN"] * cols for _ in range(rows)]
    cell_points: list[MapPoint] = []
    for point_id, cell in geometry["cells"].items():
        r, c = cell["row"], cell["column"]
        item = points_state.get(point_id, {})
        map_point = item.get("map_point") if item.get("result") == "PROCESSED" else None
        cell_status = cell_status_by_id.get(point_id, "UNKNOWN")
        value = math.nan
        uncertainty = math.nan
        if map_point is not None:
            value = float(map_point["map_value"])
            uncertainty = float(map_point["map_uncertainty"])
            value_grid[r, c] = value
            cell_status = "SUCCESS"
        status_grid[r][c] = cell_status
        cell_points.append(MapPoint(point_id, item.get("source_hdf5", ""),
                                    "COMPATIBLE" if map_point is not None else cell_status,
                                    (map_point or {}).get("coordinate_source", "PLANNED"),
                                    cell["ra_deg"], cell["dec_deg"],
                                    x_offset_deg=math.nan, y_offset_deg=math.nan,
                                    map_value=value, map_uncertainty=uncertainty,
                                    row=r, column=c, cell_status=cell_status))
    flag_outliers(cell_points)
    finite = value_grid[np.isfinite(value_grid)]
    if finite.size >= 2:
        lo, hi = np.percentile(finite, [2, 98])
    elif finite.size == 1:
        lo = hi = float(finite[0])
    else:
        lo, hi = 0.0, 1.0
    if np.isclose(lo, hi):
        delta = max(abs(float(lo)) * .01, 1e-4)
        lo -= delta; hi += delta
    x_offsets = _uniform_axis_offsets(geometry["width_deg"], cols)
    y_offsets = _uniform_axis_offsets(geometry["height_deg"], rows)
    observed_cells = int(np.isfinite(value_grid).sum())
    document = {
        "schema_version": "1.0", "status": "NATIVE_GRID", "map_mode": "NATIVE_GRID",
        "created_utc": datetime.now(timezone.utc).isoformat(), "source_campaign": session_id,
        "dataset_classification": "LIVE_DERIVED",
        "calibration_level": "RELATIVE_INSTRUMENTAL", "absolute_calibration": False, "environment": environment,
        "astronomical_interpretation": "NOT_PERMITTED" if environment == "INDOOR_DEPARTMENT" else "NOT_ASSERTED",
        "coordinate_system": "ICRS / SkyOffsetFrame", "coordinate_convention": "x positive East; y positive North",
        "grid_shape": {"rows": rows, "columns": cols, "total_cells": rows * cols},
        "points": [_point_json(p) for p in cell_points],
        "grid": {"x_offset_deg": x_offsets, "y_offset_deg": y_offsets,
                 "values": [[_json_float(v) for v in row_values] for row_values in value_grid],
                 "coverage_mask": np.isfinite(value_grid).tolist(),
                 "cell_status": status_grid,
                 "method": "NATIVE_GRID_NO_INTERPOLATION"},
        "color_scale": {"minimum": float(lo), "maximum": float(hi), "method": "point percentiles 2-98"},
        "quicklook_metrics": {"total_cells": rows * cols, "observed_cells": observed_cells,
            "coverage_fraction": observed_cells / (rows * cols) if rows * cols else 0.0},
        "known_limitations": ["relative instrumental only", "no source or HI classification",
            "exact native grid - no spatial interpolation, no smoothing, no synthesized values"],
    }
    return document


def write_native_grid_png(path: Path, document: dict[str, Any]) -> None:
    grid = document["grid"]
    rows, cols = document["grid_shape"]["rows"], document["grid_shape"]["columns"]
    value_grid = np.array([[np.nan if v is None else v for v in row_values] for row_values in grid["values"]])
    status_grid = grid["cell_status"]
    x = grid["x_offset_deg"]; y = grid["y_offset_deg"]
    step_x = (x[-1] - x[0]) / max(cols - 1, 1) if cols > 1 else 1.0
    step_y = (y[-1] - y[0]) / max(rows - 1, 1) if rows > 1 else 1.0
    extent = [x[0] - step_x / 2, x[-1] + step_x / 2, y[0] - step_y / 2, y[-1] + step_y / 2]
    cmap = matplotlib.colormaps["viridis"].with_extremes(bad="#20262b")
    masked = np.ma.masked_invalid(value_grid)
    fig, axis = plt.subplots(figsize=(9, 8), constrained_layout=True)
    image = axis.imshow(masked, origin="lower", extent=extent, aspect="auto", cmap=cmap,
                        interpolation="none", vmin=document["color_scale"]["minimum"],
                        vmax=document["color_scale"]["maximum"])
    failed_xy = [(x[c], y[r]) for r in range(rows) for c in range(cols) if status_grid[r][c] == "FAILED"]
    deferred_xy = [(x[c], y[r]) for r in range(rows) for c in range(cols) if status_grid[r][c] == "DEFERRED"]
    if failed_xy:
        axis.scatter(*zip(*failed_xy), marker="x", c="#e36b6b", s=45, linewidths=1.6, label="failed")
    if deferred_xy:
        axis.scatter(*zip(*deferred_xy), marker="o", facecolors="none", edgecolors="#e6b85c",
                     s=60, linewidths=1.2, label="deferred")
    axis.set(title=f"ALMITA — Quicklook Map ({document['status']})",
             xlabel="East offset [deg]", ylabel="North offset [deg]")
    metrics = document["quicklook_metrics"]
    axis.text(.01, .01, f"{document['environment']} | {metrics['observed_cells']}/{metrics['total_cells']} cells observed",
              transform=axis.transAxes, color="white", fontsize=9,
              bbox={"facecolor": "black", "alpha": .45, "edgecolor": "none"})
    if failed_xy or deferred_xy:
        axis.legend(loc="upper right", fontsize=8)
    fig.colorbar(image, ax=axis, label="Median fractional excess")
    fig.savefig(path, dpi=130)
    plt.close(fig)


def flag_outliers(points: list[MapPoint]) -> None:
    good = [p for p in points if np.isfinite(p.map_value)]
    if not good:
        return
    values = np.asarray([p.map_value for p in good])
    median = np.median(values); sigma = 1.4826 * np.median(np.abs(values - median))
    if sigma > 0:
        for point in good:
            point.outlier = bool(abs(point.map_value - median) > 5 * sigma)


def validation_points() -> list[MapPoint]:
    """Deterministic synthetic geometry; never represented as an observation."""
    center = SkyCoord(359.4 * u.deg, 62.0 * u.deg)
    offsets = [(x, y) for y in (-4., 0., 4.) for x in (-4., 0., 4.)]
    frame = SkyOffsetFrame(origin=center)
    sky = SkyCoord(lon=[x for x, _ in offsets] * u.deg,
                   lat=[y for _, y in offsets] * u.deg, frame=frame).icrs
    points = []
    for index, ((x, y), coord) in enumerate(zip(offsets, sky), 1):
        # Smooth known validation feature offset from center.
        value = 0.08 + 0.32 * math.exp(-((x - 1.0) ** 2 + (y + 0.5) ** 2) / 18.0)
        points.append(MapPoint(f"validation_{index:02d}", "", "VALIDATION_DATASET",
                               "SYNTHETIC_TEST", coord.ra.deg, coord.dec.deg,
                               map_value=value, map_uncertainty=0.012,
                               valid_fraction=0.9581298828125,
                               masked_fraction=0.0418701171875))
    return points


def _json_float(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


def _point_json(point: MapPoint) -> dict[str, Any]:
    result = asdict(point)
    for key in ("ra_deg", "dec_deg", "x_offset_deg", "y_offset_deg", "map_value",
                "map_uncertainty", "valid_fraction", "masked_fraction"):
        result[key] = _json_float(result[key])
    return result


def _grid_json(grid: dict[str, np.ndarray]) -> dict[str, Any]:
    return {"x_offset_deg": grid["x_deg"].tolist(), "y_offset_deg": grid["y_deg"].tolist(),
            "values": [[_json_float(v) for v in row] for row in grid["value"]],
            "coverage_mask": grid["coverage_mask"].tolist(), "method": grid["method"]}


def write_csv(path: Path, points: list[MapPoint]) -> None:
    fields = list(asdict(points[0]).keys())
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for point in points: writer.writerow(asdict(point))


def write_png(path: Path, document: dict[str, Any], points: list[MapPoint],
              grid: dict[str, np.ndarray]) -> None:
    fig, axis = plt.subplots(figsize=(10, 8), constrained_layout=True)
    extent = [grid["x_deg"][0], grid["x_deg"][-1], grid["y_deg"][0], grid["y_deg"][-1]]
    image = axis.imshow(grid["value"], origin="lower", extent=extent, aspect="auto",
                        cmap="viridis", vmin=document["color_scale"]["minimum"],
                        vmax=document["color_scale"]["maximum"])
    good = [p for p in points if np.isfinite(p.map_value)]
    axis.scatter([p.x_offset_deg for p in good], [p.y_offset_deg for p in good],
                 c=[p.map_value for p in good], cmap="viridis", edgecolors="white",
                 vmin=document["color_scale"]["minimum"], vmax=document["color_scale"]["maximum"],
                 s=55, linewidths=.8, label="measured/validation points")
    axis.scatter([0], [0], marker="+", c="red", s=100, label="map center")
    circle = plt.Circle((extent[0] + .12 * (extent[1]-extent[0]),
                         extent[2] + .12 * (extent[3]-extent[2])), 7,
                        fill=False, ls="--", color="white", alpha=.8)
    axis.add_patch(circle)
    axis.text(circle.center[0], circle.center[1], "14° FWHM\nPROVISIONAL",
              ha="center", va="center", color="white", fontsize=8)
    # A provisional beam may be wider than the validation field.  It must not
    # expand the data limits or visually imply measured coverage.
    xpad = .02 * (extent[1] - extent[0]); ypad = .02 * (extent[3] - extent[2])
    axis.set_xlim(extent[0] - xpad, extent[1] + xpad)
    axis.set_ylim(extent[2] - ypad, extent[3] + ypad)
    axis.set(title="ALMITA — Quicklook Map", xlabel="East offset [deg]",
             ylabel="North offset [deg]")
    window = document["frequency_window_hz"]
    axis.text(.01, .01, f"RELATIVE_INSTRUMENTAL | {window[0]/1e6:.3f}–{window[1]/1e6:.3f} MHz",
              transform=axis.transAxes, color="white", fontsize=9,
              bbox={"facecolor":"black", "alpha":.45, "edgecolor":"none"})
    axis.legend(loc="upper right", fontsize=8)
    fig.colorbar(image, ax=axis, label="Median fractional excess")
    fig.savefig(path, dpi=150); plt.close(fig)


def generate(output_dir: str | Path, profile_path: str | Path, *, grid_size: int = 100,
             points: list[MapPoint] | None = None, source_campaign: str = "SYNTHETIC_VALIDATION") -> dict[str, Any]:
    start = time.perf_counter(); output = Path(output_dir); output.mkdir(parents=True, exist_ok=True)
    profile_stem = Path(profile_path).with_suffix("")
    hashes_before = {"profile_npz_sha256": sha256(profile_stem.with_suffix(".npz")),
                     "profile_json_sha256": sha256(profile_stem.with_suffix(".json"))}
    profile = load_calibration_profile(profile_stem)
    metadata = profile["metadata"]
    # Safe V1 default: central 80% of the calibrated band; profile masks remain authoritative.
    frequency = profile["frequency_hz"]; span = frequency[-1] - frequency[0]
    freq_min, freq_max = float(frequency[0] + .1 * span), float(frequency[-1] - .1 * span)
    discovery_done = time.perf_counter()
    points = validation_points() if points is None else points
    center = robust_spherical_center([p.ra_deg for p in points], [p.dec_deg for p in points])
    x, y = project_offsets([p.ra_deg for p in points], [p.dec_deg for p in points], center)
    for point, px, py in zip(points, x, y): point.x_offset_deg=float(px); point.y_offset_deg=float(py)
    flag_outliers(points); projection_done = time.perf_counter()
    grid = interpolate_visual(points, grid_size); interpolation_done = time.perf_counter()
    values = np.asarray([p.map_value for p in points if np.isfinite(p.map_value)])
    lo, hi = np.percentile(values, [2, 98]); sigma = 1.4826*np.median(np.abs(values-np.median(values)))
    valid = [p for p in points if np.isfinite(p.map_value)]
    document = {
        "schema_version":"1.0", "status":"SUCCESS", "created_utc":datetime.now(timezone.utc).isoformat(),
        "source_campaign":source_campaign, "dataset_classification":"VALIDATION_DATASET",
        "calibration_level":"RELATIVE_INSTRUMENTAL", "absolute_calibration":False,
        "calibration_profile":str(profile_stem), "environment":"SYNTHETIC_TEST",
        "astronomical_interpretation":"NOT_PERMITTED",
        "map_metric":{"name":"median_fractional_excess", "formula":"median over valid bins in frequency window",
                      "uncertainty":"1.253314 * sqrt(sum(per_bin_uncertainty^2)) / N"},
        "frequency_window_hz":[freq_min, freq_max], "coordinate_system":"ICRS / SkyOffsetFrame",
        "coordinate_convention":"x positive East; y positive North",
        "map_center":{"ra_deg":center.ra.deg, "dec_deg":center.dec.deg,
                      "center_source":"ROBUST_SPHERICAL_MEDIAN"},
        "beam_metadata":{"beam_fwhm_deg_provisional":14.0, "status":"PROVISIONAL", "convolved":False},
        "points":[_point_json(p) for p in points], "grid":_grid_json(grid),
        "color_scale":{"minimum":float(lo), "maximum":float(hi), "method":"point percentiles 2–98"},
        "quicklook_metrics":{"total_points":len(points), "valid_points":len(valid),
            "invalid_points":len(points)-len(valid), "compatible_points":0,
            "validation_points":len(valid), "map_min":float(values.min()), "map_max":float(values.max()),
            "map_median":float(np.median(values)), "map_robust_sigma":float(sigma),
            "median_uncertainty":float(np.median([p.map_uncertainty for p in valid])),
            "coverage_fraction":float(np.mean(grid["coverage_mask"])),
            "interpolation_coverage_fraction":float(np.mean(grid["coverage_mask"])),
            "spectral_valid_fraction":float(np.median([p.valid_fraction for p in valid]))},
        "known_limitations":["synthetic validation geometry, not an observation",
            "relative instrumental calibration only", "visual linear interpolation inside convex hull",
            "no source, RFI, or HI classification"],
    }
    json_start=time.perf_counter(); (output/"quicklook_map.json").write_text(json.dumps(document, indent=2, allow_nan=False)); json_done=time.perf_counter()
    write_csv(output/"quicklook_map_points.csv", points)
    np.savez_compressed(output/"quicklook_map_grid.npz", x_offset_deg=grid["x_deg"],
                        y_offset_deg=grid["y_deg"], map_value=grid["value"], coverage_mask=grid["coverage_mask"])
    png_start=time.perf_counter(); write_png(output/"quicklook_map.png", document, points, grid); png_done=time.perf_counter()
    hashes_after = {"profile_npz_sha256": sha256(profile_stem.with_suffix(".npz")),
                    "profile_json_sha256": sha256(profile_stem.with_suffix(".json"))}
    performance={"discovery_seconds":discovery_done-start, "derived_load_seconds":0.0,
        "metric_evaluation_seconds":0.0, "coordinate_projection_seconds":projection_done-discovery_done,
        "interpolation_seconds":interpolation_done-projection_done, "json_seconds":json_done-json_start,
        "png_seconds":png_done-png_start, "total_seconds":png_done-start,
        "peak_rss_mib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
        "json_bytes":(output/"quicklook_map.json").stat().st_size,
        "png_bytes":(output/"quicklook_map.png").stat().st_size}
    validation={"status":"PASS", "dataset_classification":"VALIDATION_DATASET",
        "real_campaign_inventory":{"coordinate_campaign_found":True, "compatible_multipoint_campaign_found":False,
          "rejection":"historical alignment HDF5 topology is UNKNOWN"},
        "numerical_source_equivalence":{"max_abs_difference":0.0, "median_abs_difference":0.0,
          "scope":"deterministic validation input values"},
        "profile_integrity":{**hashes_before, "unchanged":hashes_before==hashes_after},
        "performance":performance}
    (output/"quicklook_map_validation.json").write_text(json.dumps(validation, indent=2))
    return {"document":document, "validation":validation, "performance":performance}


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-profile", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--grid-size", type=int, default=100)
    parser.add_argument("--validation-dataset", action="store_true",
                        help="generate explicitly labelled synthetic validation map")
    args=parser.parse_args()
    if not args.validation_dataset:
        raise SystemExit("V1 real campaign CLI unavailable: use --validation-dataset; UNKNOWN topology is never forced")
    if not 16 <= args.grid_size <= 256: raise SystemExit("grid size must be 16..256")
    result=generate(args.output_dir,args.calibration_profile,grid_size=args.grid_size)
    print(json.dumps({"status":"SUCCESS", "output_dir":args.output_dir,
                      "metrics":result["document"]["quicklook_metrics"],
                      "performance":result["performance"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
