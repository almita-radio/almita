"""Focused tests for the ANTENNA B / RFI_REF occupancy-quality map.

Never a fake sky map: this only overlays an RFI contamination diagnostic
onto Antenna A's own native grid geometry, correlated purely by
overlapping wall-clock timestamps between MAIN's per-point capture window
(mosaic.csv) and RFI_REF's own bounded scalar history.
"""
import csv
import json
import math
from pathlib import Path

import pytest

import rfi_occupancy_map as rom
from quicklook_map import load_native_grid_geometry, read_cell_status
from runtime_state import atomic_write_json


def write_mosaic_csv(grid_dir: Path, rows_data):
    """rows_data: list of dicts with at least point_id/grid_row/grid_col/ra/dec;
    capture_status/start_time/end_time default to SUCCESS with no timing."""
    fields = ["point_number", "point_id", "scan_order", "grid_row", "grid_col", "row", "column",
              "ra", "dec", "capture_status", "start_time", "end_time", "duration",
              "visibility_deferred", "session_name"]
    with (grid_dir / "mosaic.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows_data:
            base = {"point_number": r["point_id"], "point_id": r["point_id"], "scan_order": r["point_id"],
                    "grid_row": r["grid_row"], "grid_col": r["grid_col"], "row": r["grid_row"],
                    "column": r["grid_col"], "ra": r.get("ra", 10.0), "dec": r.get("dec", -30.0),
                    "capture_status": r.get("capture_status", "SUCCESS"),
                    "start_time": r.get("start_time", ""), "end_time": r.get("end_time", ""),
                    "duration": "", "visibility_deferred": "false", "session_name": "s"}
            w.writerow(base)


def make_grid_dir(tmp_path, rows, cols, points):
    grid_dir = tmp_path / "grid"
    grid_dir.mkdir()
    (grid_dir / "grid_metadata.json").write_text(json.dumps(
        {"grid": {"rows": rows, "columns": cols, "width_deg": 10.0, "height_deg": 10.0}}))
    write_mosaic_csv(grid_dir, points)
    return grid_dir


def sample(utc, occ=0.05, clip=0.0, peak=-60.0):
    return {"utc": utc, "occupancy_fraction": occ, "clipping_fraction": clip, "peak_dbfs": peak}


# ---------------------------------------------------------------- read_point_timing


def test_read_point_timing_reads_mosaic_csv(tmp_path):
    grid_dir = make_grid_dir(tmp_path, 1, 2, [
        {"point_id": "1", "grid_row": 0, "grid_col": 0, "capture_status": "SUCCESS",
         "start_time": "2026-09-02T00:00:00+00:00", "end_time": "2026-09-02T00:00:10+00:00"},
        {"point_id": "2", "grid_row": 0, "grid_col": 1, "capture_status": "FAILED",
         "start_time": "", "end_time": ""},
    ])
    timing = rom.read_point_timing(grid_dir)
    assert timing["1"]["capture_status"] == "SUCCESS"
    assert timing["1"]["start_time"] == "2026-09-02T00:00:00+00:00"
    assert timing["2"]["capture_status"] == "FAILED"
    assert timing["2"]["start_time"] is None


def test_read_point_timing_missing_file_returns_empty(tmp_path):
    assert rom.read_point_timing(tmp_path / "does-not-exist") == {}


# ---------------------------------------------------------------- read_rfi_history


def test_read_rfi_history_matching_session(tmp_path):
    atomic_write_json(tmp_path / "rfi_ref_history.json", {
        "schema_version": 1, "session_id": "s1", "device_serial": "00000002",
        "samples": [sample("2026-09-02T00:00:05+00:00")], "updated_utc": "2026-09-02T00:00:05+00:00",
    })
    samples = rom.read_rfi_history(tmp_path, "s1")
    assert len(samples) == 1


def test_read_rfi_history_session_mismatch_is_ignored(tmp_path):
    atomic_write_json(tmp_path / "rfi_ref_history.json", {
        "schema_version": 1, "session_id": "s-old", "device_serial": "00000002",
        "samples": [sample("2026-09-02T00:00:05+00:00")], "updated_utc": "2026-09-02T00:00:05+00:00",
    })
    assert rom.read_rfi_history(tmp_path, "s1") == []


def test_read_rfi_history_missing_file_returns_empty(tmp_path):
    assert rom.read_rfi_history(tmp_path, "s1") == []


# ---------------------------------------------------------------- correlate_rfi_with_points


def test_correlate_only_temporally_overlapping_samples_used():
    timing = {"1": {"start_time": "2026-09-02T00:00:00+00:00", "end_time": "2026-09-02T00:00:10+00:00",
                    "capture_status": "SUCCESS"}}
    samples = [sample("2026-09-02T00:00:05+00:00", occ=0.10),
               sample("2026-09-02T00:00:20+00:00", occ=0.99)]  # outside the window
    metrics = rom.correlate_rfi_with_points(timing, samples)
    assert metrics["1"]["n_observations"] == 1
    assert metrics["1"]["mean_occupancy_fraction"] == pytest.approx(0.10)


def test_correlate_ignores_non_success_points():
    timing = {"1": {"start_time": "2026-09-02T00:00:00+00:00", "end_time": "2026-09-02T00:00:10+00:00",
                    "capture_status": "FAILED"}}
    samples = [sample("2026-09-02T00:00:05+00:00")]
    assert rom.correlate_rfi_with_points(timing, samples) == {}


def test_correlate_point_without_overlapping_samples_is_absent_not_zero():
    timing = {"1": {"start_time": "2026-09-02T00:00:00+00:00", "end_time": "2026-09-02T00:00:10+00:00",
                    "capture_status": "SUCCESS"}}
    metrics = rom.correlate_rfi_with_points(timing, [sample("2026-09-02T05:00:00+00:00")])
    assert "1" not in metrics  # never a fake/synthesized 0.0


def test_correlate_missing_or_invalid_timing_excluded():
    timing = {
        "1": {"start_time": None, "end_time": None, "capture_status": "SUCCESS"},
        "2": {"start_time": "2026-09-02T00:00:10+00:00", "end_time": "2026-09-02T00:00:00+00:00",
              "capture_status": "SUCCESS"},  # end before start
    }
    metrics = rom.correlate_rfi_with_points(timing, [sample("2026-09-02T00:00:05+00:00")])
    assert metrics == {}


def test_correlate_geometry_row_col_order_does_not_affect_correlation():
    """Serpentine/scan order lives only in geometry cell placement, never in
    the point_id-keyed timing/correlation dict itself - shuffling the
    dict's insertion order must not change any result."""
    timing_a = {
        "1": {"start_time": "2026-09-02T00:00:00+00:00", "end_time": "2026-09-02T00:00:05+00:00", "capture_status": "SUCCESS"},
        "2": {"start_time": "2026-09-02T00:00:10+00:00", "end_time": "2026-09-02T00:00:15+00:00", "capture_status": "SUCCESS"},
    }
    timing_b = {"2": timing_a["2"], "1": timing_a["1"]}  # reversed insertion order
    samples = [sample("2026-09-02T00:00:02+00:00", occ=0.1), sample("2026-09-02T00:00:12+00:00", occ=0.2)]
    assert rom.correlate_rfi_with_points(timing_a, samples) == rom.correlate_rfi_with_points(timing_b, samples)


# ---------------------------------------------------------------- build_rfi_occupancy_map_document


def _geometry_2x2():
    return {"rows": 2, "columns": 2, "width_deg": 10.0, "height_deg": 10.0, "cells": {
        "1": {"row": 0, "column": 0, "ra_deg": 10.0, "dec_deg": -30.0},
        "2": {"row": 0, "column": 1, "ra_deg": 11.0, "dec_deg": -30.0},
        "3": {"row": 1, "column": 0, "ra_deg": 10.0, "dec_deg": -31.0},
        "4": {"row": 1, "column": 1, "ra_deg": 11.0, "dec_deg": -31.0},
    }}


def test_document_is_not_a_sky_map():
    doc = rom.build_rfi_occupancy_map_document(_geometry_2x2(), {}, {}, "s1")
    assert doc["title"] == "ANTENNA B - RFI OCCUPANCY MAP"
    assert doc["product"] == "RFI_OCCUPANCY_MAP"
    assert "coordinate_system" not in doc  # never dressed up as a sky-offset map
    assert any("not a sky brightness" in limitation for limitation in doc["known_limitations"])


def test_document_unobserved_cells_are_nan_no_interpolation():
    metrics = {"1": {"mean_occupancy_fraction": 0.2, "max_occupancy_fraction": 0.3,
                      "median_occupancy_fraction": 0.2, "max_clipping_fraction": 0.0,
                      "max_peak_dbfs": -50.0, "median_peak_dbfs": -55.0, "n_observations": 5}}
    doc = rom.build_rfi_occupancy_map_document(_geometry_2x2(), metrics,
                                                {"1": "SUCCESS", "2": "FAILED", "3": "PLANNED", "4": "DEFERRED"}, "s1")
    values = doc["grid"]["values"]
    assert values[0][0] == pytest.approx(0.2)
    # JSON-facing values use None (JSON null), never a raw NaN float - the
    # document must survive strict JSON serialization (allow_nan=False),
    # same convention as quicklook_map.py's own native grid document.
    assert values[0][1] is None
    assert values[1][0] is None
    assert values[1][1] is None
    assert doc["grid"]["method"] == "NATIVE_GRID_NO_INTERPOLATION"
    assert doc["quicklook_metrics"]["observed_cells"] == 1
    assert doc["quicklook_metrics"]["total_cells"] == 4


def test_document_metric_field_selectable():
    metrics = {"1": {"mean_occupancy_fraction": 0.2, "max_occupancy_fraction": 0.9,
                      "median_occupancy_fraction": 0.2, "max_clipping_fraction": 0.0,
                      "max_peak_dbfs": -50.0, "median_peak_dbfs": -55.0, "n_observations": 5}}
    doc = rom.build_rfi_occupancy_map_document(_geometry_2x2(), metrics, {}, "s1",
                                                metric_key="max_occupancy_fraction")
    assert doc["grid"]["values"][0][0] == pytest.approx(0.9)
    assert doc["metric"] == "max_occupancy_fraction"


def test_document_survives_strict_json_serialization():
    """Regression guard: quicklook_live.py's atomic_json writer uses
    allow_nan=False (strict JSON) - a raw NaN float anywhere in the
    document would raise ValueError at write time and silently produce no
    occupancy map at all (caught by the caller's best-effort try/except)."""
    doc = rom.build_rfi_occupancy_map_document(_geometry_2x2(), {}, {}, "s1")
    json.dumps(doc, allow_nan=False)  # must not raise


def test_write_rfi_occupancy_png(tmp_path):
    doc = rom.build_rfi_occupancy_map_document(_geometry_2x2(), {}, {}, "s1")
    out = tmp_path / "rfi_occupancy_map.png"
    rom.write_rfi_occupancy_png(out, doc)
    assert out.stat().st_size > 0
