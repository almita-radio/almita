#!/usr/bin/env python3
"""ANTENNA A - OPTIONAL interpolated visual preview. NOT the science map.

Deliberately kept in its own module, separate from quicklook_map.py (which
is guarded by test_no_interpolation_keyword_used_in_source - a regression
test that scans that entire file to guarantee it never contains smoothing/
interpolation rendering code). The NATIVE_GRID map in quicklook_map.py
remains the exact, non-interpolated science product; this module only adds
a purely visual companion that linearly triangulation-interpolates BETWEEN
the same real observed points already in the native grid - never inventing
a new observation, never replacing or modifying the native grid itself.

Every document this module produces carries interpolated=True and an
explicit warning string, and every rendered PNG says "INTERPOLATED
PREVIEW - not raw data" in its own title, so nothing downstream can
mistake this for the exact NATIVE_GRID product.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from quicklook_map import MapError, MapPoint, interpolate_visual, project_offsets, robust_spherical_center


def _json_float(value: float):
    return float(value) if np.isfinite(value) else None


def build_interpolated_preview_document(geometry: dict[str, Any], points_state: dict[str, dict[str, Any]],
                                        session_id: str, grid_size: int = 60) -> dict[str, Any]:
    """OPTIONAL, purely visual companion to Antenna A's NATIVE_GRID map.

    Returns {"available": False, ...} rather than raising when fewer than
    three processed points exist yet (interpolation is undefined) - the
    caller treats that as "not ready yet", same as the native grid's own
    early-session state.
    """
    processed = {pid: item for pid, item in points_state.items()
                if item.get("result") == "PROCESSED" and item.get("map_point")}
    base = {
        "schema_version": "1.0", "product": "NATIVE_GRID_INTERPOLATED_PREVIEW",
        "map_mode": "INTERPOLATED_PREVIEW", "interpolated": True,
        "warning": "VISUAL PREVIEW ONLY - linearly interpolated between real observations, "
                  "not raw/exact data. The NATIVE_GRID product (quicklook_map.json) is the "
                  "exact, non-interpolated science map.",
        "created_utc": datetime.now(timezone.utc).isoformat(), "source_campaign": session_id,
        "calibration_level": "RELATIVE_INSTRUMENTAL", "absolute_calibration": False,
    }
    if len(processed) < 3:
        return {**base, "available": False, "reason": "fewer than 3 processed points",
                "n_points_used": len(processed)}
    ra = [geometry["cells"][pid]["ra_deg"] for pid in processed]
    dec = [geometry["cells"][pid]["dec_deg"] for pid in processed]
    try:
        center = robust_spherical_center(ra, dec)
        x_off, y_off = project_offsets(ra, dec, center)
    except MapError as error:
        return {**base, "available": False, "reason": str(error), "n_points_used": len(processed)}
    map_points = [
        MapPoint(pid, item.get("source_hdf5", ""), "COMPATIBLE",
                (item["map_point"]).get("coordinate_source", "PLANNED"),
                geometry["cells"][pid]["ra_deg"], geometry["cells"][pid]["dec_deg"],
                x_offset_deg=float(x), y_offset_deg=float(y),
                map_value=float(item["map_point"]["map_value"]),
                map_uncertainty=float(item["map_point"]["map_uncertainty"]))
        for (pid, item), x, y in zip(processed.items(), x_off, y_off)
    ]
    try:
        grid = interpolate_visual(map_points, grid_size=grid_size)
    except MapError as error:
        return {**base, "available": False, "reason": str(error), "n_points_used": len(processed)}
    values = grid["value"]
    finite = values[np.isfinite(values)]
    if finite.size >= 2:
        lo, hi = (float(v) for v in np.percentile(finite, [2, 98]))
    else:
        lo, hi = float(finite[0]) - 1e-4, float(finite[0]) + 1e-4
    return {
        **base, "available": True,
        "x_offset_deg": grid["x_deg"].tolist(), "y_offset_deg": grid["y_deg"].tolist(),
        "values": [[_json_float(v) for v in row] for row in values],
        "coverage_mask": grid["coverage_mask"].tolist(),
        "method": grid["method"],
        "color_scale": {"minimum": lo, "maximum": hi, "method": "point percentiles 2-98"},
        "n_points_used": len(processed),
        "real_observation_points": [{"point_id": p.point_id, "x_offset_deg": p.x_offset_deg,
                                     "y_offset_deg": p.y_offset_deg} for p in map_points],
    }


def write_interpolated_preview_png(path: Path, document: dict[str, Any]) -> None:
    """Not called for a document with available=False - the caller checks
    first, same convention as quicklook_map.write_native_grid_png."""
    values = np.array([[np.nan if v is None else v for v in row] for row in document["values"]])
    x = document["x_offset_deg"]; y = document["y_offset_deg"]
    extent = [x[0], x[-1], y[0], y[-1]]
    cmap = matplotlib.colormaps["viridis"].with_extremes(bad="#20262b")
    masked = np.ma.masked_invalid(values)
    fig, axis = plt.subplots(figsize=(7, 6), constrained_layout=True)
    image = axis.imshow(masked, origin="lower", extent=extent, aspect="auto", cmap=cmap,
                        interpolation="bilinear", vmin=document["color_scale"]["minimum"],
                        vmax=document["color_scale"]["maximum"])
    real_x = [p["x_offset_deg"] for p in document["real_observation_points"]]
    real_y = [p["y_offset_deg"] for p in document["real_observation_points"]]
    axis.scatter(real_x, real_y, marker="o", facecolors="none", edgecolors="white",
                s=40, linewidths=1.0, label="real observation")
    axis.set(title="ALMITA - Native Grid (INTERPOLATED PREVIEW - not raw data)",
             xlabel="East offset [deg]", ylabel="North offset [deg]")
    axis.text(.01, .01, f"{document['n_points_used']} real points used | visual preview only",
              transform=axis.transAxes, color="white", fontsize=9,
              bbox={"facecolor": "black", "alpha": .5, "edgecolor": "none"})
    axis.legend(loc="upper right", fontsize=8)
    fig.colorbar(image, ax=axis, label="Median fractional excess (interpolated)")
    fig.savefig(path, dpi=130)
    plt.close(fig)
