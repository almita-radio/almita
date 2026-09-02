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
