"""Focused tests for the OPTIONAL Antenna A interpolated preview - a purely
visual companion to the exact NATIVE_GRID map, never a replacement.

Lives in its own module (quicklook_map_interpolated_preview.py) precisely
so quicklook_map.py's own regression test
(test_no_interpolation_keyword_used_in_source) keeps guaranteeing that the
NATIVE_GRID science product itself never contains smoothing/interpolation
rendering code.
"""
import math

import numpy as np
import pytest

import quicklook_map_interpolated_preview as qmip


def _geometry_3x3():
    cells = {}
    pid = 1
    for r in range(3):
        for c in range(3):
            cells[str(pid)] = {"row": r, "column": c, "ra_deg": 10.0 + c, "dec_deg": -30.0 + r}
            pid += 1
    return {"rows": 3, "columns": 3, "width_deg": 10.0, "height_deg": 10.0, "cells": cells}


def _processed_point(source="p.h5", value=0.1, uncertainty=0.01):
    return {"result": "PROCESSED", "source_hdf5": source,
            "map_point": {"map_value": value, "map_uncertainty": uncertainty,
                          "coordinate_source": "COMMANDED"}}


def test_unavailable_with_fewer_than_three_points():
    points_state = {"1": _processed_point(), "2": _processed_point()}
    doc = qmip.build_interpolated_preview_document(_geometry_3x3(), points_state, "s1")
    assert doc["available"] is False
    assert doc["interpolated"] is True
    assert doc["n_points_used"] == 2


def test_available_document_is_clearly_labeled_and_never_confused_with_native_grid():
    points_state = {str(i): _processed_point(value=0.1 * i) for i in range(1, 6)}
    doc = qmip.build_interpolated_preview_document(_geometry_3x3(), points_state, "s1")
    assert doc["available"] is True
    assert doc["interpolated"] is True
    assert doc["product"] == "NATIVE_GRID_INTERPOLATED_PREVIEW"
    assert doc["map_mode"] != "NATIVE_GRID"
    assert "PREVIEW" in doc["warning"] and "not raw" in doc["warning"]
    assert "NATIVE_GRID" in doc["warning"]  # explicitly points to the real science product
    assert doc["n_points_used"] == 5
    assert len(doc["real_observation_points"]) == 5


def test_real_observations_are_never_fabricated_only_space_between_them():
    """The interpolation grid must only fill the convex hull of the real
    points - it must never claim coverage far outside where real
    observations actually are."""
    points_state = {str(i): _processed_point(value=0.1 * i) for i in range(1, 6)}
    doc = qmip.build_interpolated_preview_document(_geometry_3x3(), points_state, "s1", grid_size=20)
    assert doc["n_points_used"] == len(doc["real_observation_points"])
    values = doc["values"]
    coverage = doc["coverage_mask"]
    finite_count = sum(1 for row in values for v in row if v is not None)
    covered_count = sum(1 for row in coverage for v in row if v)
    assert finite_count == covered_count
    total_cells = sum(len(row) for row in values)
    assert finite_count < total_cells  # never claims full coverage from just 5 real points


def test_write_interpolated_preview_png(tmp_path):
    points_state = {str(i): _processed_point(value=0.1 * i) for i in range(1, 6)}
    doc = qmip.build_interpolated_preview_document(_geometry_3x3(), points_state, "s1")
    out = tmp_path / "quicklook_map_interpolated.png"
    qmip.write_interpolated_preview_png(out, doc)
    assert out.stat().st_size > 0


# ------------------------------------------------------------------ limited reach (operator, 2026-10-14: "excessive")
def _geometry(n):
    cells, pid = {}, 1
    for r in range(n):
        for c in range(n):
            cells[str(pid)] = {"row": r, "column": c, "ra_deg": 10.0 + c, "dec_deg": r * 1.0}   # ~1 deg pitch at the equator
            pid += 1
    return {"rows": n, "columns": n, "width_deg": n - 1.0, "height_deg": n - 1.0, "cells": cells}


def _nearest_real(doc, ix, iy):
    x, y = doc["x_offset_deg"][ix], doc["y_offset_deg"][iy]
    return min(np.hypot(x - p["x_offset_deg"], y - p["y_offset_deg"]) for p in doc["real_observation_points"])


def test_the_preview_never_paints_far_from_a_real_observation():
    """Three processed points at the corners of a 7x7 grid used to produce one smooth triangle over half the map."""
    geometry = _geometry(7)
    corners = {"1": _processed_point(value=0.0), "7": _processed_point(value=1.0), "43": _processed_point(value=0.5)}
    doc = qmip.build_interpolated_preview_document(geometry, corners, "s1")
    assert doc["available"] and "limited to" in doc["method"]
    mask = np.asarray(doc["coverage_mask"])
    reach = qmip.REACH_IN_SPACINGS * 1.0
    for iy, ix in zip(*np.nonzero(mask)):
        assert _nearest_real(doc, ix, iy) <= reach + 1e-6
    assert mask.mean() < 0.15                        # the old convex-hull fill covered about half of the canvas


def test_neighbouring_observations_still_join_into_one_patch():
    geometry = _geometry(4)
    doc = qmip.build_interpolated_preview_document(geometry, {pid: _processed_point(value=float(pid)) for pid in geometry["cells"]}, "s1")
    mask = np.asarray(doc["coverage_mask"])
    inner = mask[1:-1, 1:-1]                         # the outermost pixel ring lies on the hull edge itself
    assert inner.mean() > 0.9                        # a fully observed grid is still a continuous picture
    assert len(doc["values"]) <= 4 * 4 + 2           # about PIXELS_PER_SPACING pixels per cell, not a fixed 60x60


def test_grid_geometry_reads_the_csv_ra_column_as_hours(tmp_path):
    """mosaic.csv's "ra" is in hours (same value as target_ra_hours); stored as degrees unchanged it squeezed every
    east-west offset by 15."""
    import csv
    import json
    from quicklook_map import load_native_grid_geometry
    (tmp_path / "grid_metadata.json").write_text(json.dumps({"grid": {"rows": 1, "columns": 2, "width_deg": 15.0, "height_deg": 0.0}}))
    with (tmp_path / "mosaic.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["point_id", "grid_row", "grid_col", "ra", "dec", "target_ra_hours"])
        writer.writeheader()
        writer.writerow({"point_id": "1", "grid_row": 0, "grid_col": 0, "ra": "1.0", "dec": "0.0", "target_ra_hours": "1.0"})
        writer.writerow({"point_id": "2", "grid_row": 0, "grid_col": 1, "ra": "2.0", "dec": "0.0", "target_ra_hours": "2.0"})
    cells = load_native_grid_geometry(tmp_path)["cells"]
    assert cells["1"]["ra_deg"] == 15.0 and cells["2"]["ra_deg"] == 30.0
