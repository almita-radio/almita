import json
import csv
import math
from pathlib import Path

import numpy as np
import pytest

from quicklook_map import (
    build_native_grid_document, find_grid_directory, load_native_grid_geometry,
    read_cell_status, write_native_grid_png,
)


def make_grid_dir(tmp_path, rows=10, cols=10, width_deg=30.0, height_deg=30.0,
                  status_overrides=None):
    """Write grid_generator.py's own canonical artifacts: point_id is
    row-major (row*cols+col+1), independent of any serpentine scan order."""
    grid_dir = tmp_path / "grid"
    grid_dir.mkdir(exist_ok=True)
    (grid_dir / "grid_metadata.json").write_text(json.dumps({
        "grid": {"rows": rows, "columns": cols, "total_points": rows * cols,
                 "width_deg": width_deg, "height_deg": height_deg}}))
    fields = ["point_number", "point_id", "scan_order", "grid_row", "grid_col", "row", "column",
              "ra", "dec", "capture_status", "visibility_deferred", "session_name"]
    status_overrides = status_overrides or {}
    with (grid_dir / "mosaic.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        scan_order = 1
        for r in range(rows):
            col_sequence = range(cols) if r % 2 == 0 else range(cols - 1, -1, -1)
            for c in col_sequence:
                point_id = r * cols + c + 1  # geometric identity: row-major, NOT scan order
                capture_status, deferred = status_overrides.get(point_id, ("planned", "false"))
                writer.writerow({"point_number": point_id, "point_id": point_id, "scan_order": scan_order,
                                 "grid_row": r, "grid_col": c, "row": r, "column": c,
                                 "ra": 10.0 + c, "dec": -20.0 + r, "capture_status": capture_status,
                                 "visibility_deferred": deferred, "session_name": "s"})
                scan_order += 1
    return grid_dir


def processed(point_id, value, uncertainty=0.1, coordinate_source="COMMANDED"):
    return {str(point_id): {"result": "PROCESSED", "source_hdf5": f"p{point_id}.h5",
            "map_point": {"map_value": value, "map_uncertainty": uncertainty,
                          "coordinate_source": coordinate_source}}}


def merge(*dicts):
    out = {}
    for d in dicts:
        out.update(d)
    return out


def finite_grid(document):
    return np.array([[np.nan if v is None else v for v in row] for row in document["grid"]["values"]])


# ---------------------------------------------------------------- §1/§4: exact 100 logical positions

def test_10x10_grid_has_exactly_100_logical_positions(tmp_path):
    grid_dir = make_grid_dir(tmp_path)
    geometry = load_native_grid_geometry(grid_dir)
    assert geometry["rows"] == 10 and geometry["columns"] == 10
    assert len(geometry["cells"]) == 100
    doc = build_native_grid_document(geometry, {}, {}, "s", "INDOOR_DEPARTMENT")
    assert doc["grid_shape"]["total_cells"] == 100
    assert len(doc["points"]) == 100


def test_one_point_processed_is_1_finite_99_nan(tmp_path):
    grid_dir = make_grid_dir(tmp_path)
    geometry = load_native_grid_geometry(grid_dir)
    doc = build_native_grid_document(geometry, processed(1, 42.0), read_cell_status(grid_dir), "s", "X")
    values = finite_grid(doc)
    assert np.isfinite(values).sum() == 1
    assert np.isnan(values).sum() == 99
    assert doc["quicklook_metrics"]["observed_cells"] == 1


def test_eight_points_processed_is_8_finite_92_empty(tmp_path):
    grid_dir = make_grid_dir(tmp_path)
    geometry = load_native_grid_geometry(grid_dir)
    state = merge(*(processed(pid, float(pid)) for pid in range(1, 9)))
    doc = build_native_grid_document(geometry, state, read_cell_status(grid_dir), "s", "X")
    values = finite_grid(doc)
    assert np.isfinite(values).sum() == 8
    assert np.isnan(values).sum() == 92


def test_hundred_points_processed_is_100_finite(tmp_path):
    grid_dir = make_grid_dir(tmp_path)
    geometry = load_native_grid_geometry(grid_dir)
    state = merge(*(processed(pid, float(pid)) for pid in range(1, 101)))
    doc = build_native_grid_document(geometry, state, read_cell_status(grid_dir), "s", "X")
    values = finite_grid(doc)
    assert np.isfinite(values).sum() == 100
    assert doc["quicklook_metrics"]["observed_cells"] == 100
    assert doc["quicklook_metrics"]["coverage_fraction"] == 1.0


# ---------------------------------------------------------------- §7: no fabricated intensity

def test_deferred_points_have_no_intensity(tmp_path):
    grid_dir = make_grid_dir(tmp_path, status_overrides={5: ("planned", "true")})
    geometry = load_native_grid_geometry(grid_dir)
    cell_status = read_cell_status(grid_dir)
    assert cell_status["5"] == "DEFERRED"
    doc = build_native_grid_document(geometry, {}, cell_status, "s", "X")
    values = finite_grid(doc)
    assert np.isnan(values).all()
    cell = next(p for p in doc["points"] if p["point_id"] == "5")
    assert cell["cell_status"] == "DEFERRED" and cell["map_value"] is None


def test_failed_points_have_no_intensity(tmp_path):
    grid_dir = make_grid_dir(tmp_path, status_overrides={7: ("failed", "false")})
    geometry = load_native_grid_geometry(grid_dir)
    cell_status = read_cell_status(grid_dir)
    doc = build_native_grid_document(geometry, {}, cell_status, "s", "X")
    cell = next(p for p in doc["points"] if p["point_id"] == "7")
    assert cell["cell_status"] == "FAILED" and cell["map_value"] is None
    assert np.isnan(finite_grid(doc)).all()


def test_pending_points_have_no_intensity(tmp_path):
    grid_dir = make_grid_dir(tmp_path)  # everything still "planned"
    geometry = load_native_grid_geometry(grid_dir)
    doc = build_native_grid_document(geometry, {}, read_cell_status(grid_dir), "s", "X")
    assert all(p["map_value"] is None for p in doc["points"])
    assert all(p["cell_status"] == "PLANNED" for p in doc["points"])


# ---------------------------------------------------------------- §5/§11: anti-interpolation

def test_no_synthetic_intermediate_values_between_two_far_measurements(tmp_path):
    """The exact test the task demands: two far-apart cells with 1 and 100,
    everything else must remain NaN - never a synthesized in-between value."""
    grid_dir = make_grid_dir(tmp_path)
    geometry = load_native_grid_geometry(grid_dir)
    state = merge(processed(1, 1.0), processed(100, 100.0))  # (row0,col0) and (row9,col9)
    doc = build_native_grid_document(geometry, state, read_cell_status(grid_dir), "s", "X")
    values = finite_grid(doc)
    finite_values = sorted(values[np.isfinite(values)].tolist())
    assert finite_values == [1.0, 100.0]
    assert np.isfinite(values).sum() == 2


# ---------------------------------------------------------------- §12: synthetic visual validation

def test_synthetic_validation_6_points_then_complete_to_100(tmp_path):
    grid_dir = make_grid_dir(tmp_path)
    geometry = load_native_grid_geometry(grid_dir)
    cell_status = read_cell_status(grid_dir)
    six = merge(*(processed(pid, float(pid)) for pid in (1, 2, 3, 14, 25, 50)))
    doc = build_native_grid_document(geometry, six, cell_status, "s", "X")
    values = finite_grid(doc)
    assert np.isfinite(values).sum() == 6
    assert np.isnan(values).sum() == 94

    full = merge(*(processed(pid, float(pid)) for pid in range(1, 101)))
    doc_full = build_native_grid_document(geometry, full, cell_status, "s", "X")
    assert np.isfinite(finite_grid(doc_full)).sum() == 100


# ---------------------------------------------------------------- §5: serpentine does not define geometry

def test_serpentine_scan_order_does_not_alter_cell_geometry(tmp_path):
    """point_id 4 in a 3-wide grid is at scan position 4 in raster order but
    at a DIFFERENT column once row 1 is serpentine-reversed. The map must
    place it at its true row/column, not at its sequential/scan position."""
    grid_dir = make_grid_dir(tmp_path, rows=3, cols=3)
    geometry = load_native_grid_geometry(grid_dir)
    # point_id = row*cols+col+1 (geometric) regardless of the file's scan_order
    # column ordering (row 1 is written right-to-left in mosaic.csv).
    assert geometry["cells"]["4"] == {"row": 1, "column": 0, "ra_deg": 10.0, "dec_deg": -19.0}
    assert geometry["cells"]["6"] == {"row": 1, "column": 2, "ra_deg": 12.0, "dec_deg": -19.0}
    state = processed(4, 99.0)
    doc = build_native_grid_document(geometry, state, read_cell_status(grid_dir), "s", "X")
    values = finite_grid(doc)
    assert values[1, 0] == 99.0  # true geometric position (row=1,col=0)
    assert np.isnan(values[0, 0]) and np.isnan(values[1, 2])


def test_processing_order_does_not_alter_final_geometry(tmp_path):
    grid_dir = make_grid_dir(tmp_path, rows=3, cols=3)
    geometry = load_native_grid_geometry(grid_dir)
    cell_status = read_cell_status(grid_dir)
    forward = build_native_grid_document(geometry, merge(processed(1, 1.0), processed(9, 9.0)),
                                         cell_status, "s", "X")
    reverse = build_native_grid_document(geometry, merge(processed(9, 9.0), processed(1, 1.0)),
                                         cell_status, "s", "X")
    assert np.array_equal(finite_grid(forward), finite_grid(reverse), equal_nan=True)


# ---------------------------------------------------------------- §4: RA/DEC correctness

def test_ra_dec_match_grid_geometry_not_manifest(tmp_path):
    grid_dir = make_grid_dir(tmp_path, rows=3, cols=3)
    geometry = load_native_grid_geometry(grid_dir)
    cell = next(c for pid, c in geometry["cells"].items() if pid == "5")
    assert cell["row"] == 1 and cell["column"] == 1
    assert cell["ra_deg"] == 11.0 and cell["dec_deg"] == -19.0
    doc = build_native_grid_document(geometry, processed(5, 1.0), read_cell_status(grid_dir), "s", "X")
    point = next(p for p in doc["points"] if p["point_id"] == "5")
    assert point["ra_deg"] == 11.0 and point["dec_deg"] == -19.0


# ---------------------------------------------------------------- §3/§10: map_mode contract

def test_map_mode_is_native_grid():
    geometry = {"rows": 1, "columns": 1, "width_deg": 1.0, "height_deg": 1.0,
                "cells": {"1": {"row": 0, "column": 0, "ra_deg": 1.0, "dec_deg": 2.0}}}
    doc = build_native_grid_document(geometry, processed(1, 5.0), {}, "s", "X")
    assert doc["status"] == "NATIVE_GRID"
    assert doc["map_mode"] == "NATIVE_GRID"
    assert doc["grid"]["method"] == "NATIVE_GRID_NO_INTERPOLATION"


def test_grid_directory_discovery_walks_upward(tmp_path):
    grid_dir = make_grid_dir(tmp_path)
    nested_session_dir = grid_dir / "data" / "iq" / "s-20260101-00:00:00"
    nested_session_dir.mkdir(parents=True)
    found = find_grid_directory(nested_session_dir)
    assert found == grid_dir.resolve()


def test_grid_directory_discovery_returns_none_when_absent(tmp_path):
    lonely = tmp_path / "no_grid_here" / "data" / "iq" / "x"
    lonely.mkdir(parents=True)
    assert find_grid_directory(lonely) is None


# ---------------------------------------------------------------- §6: PNG generated, no interpolation kwarg

def test_png_is_generated_and_uses_no_interpolation():
    geometry = {"rows": 2, "columns": 2, "width_deg": 4.0, "height_deg": 4.0,
                "cells": {str(i): {"row": (i - 1) // 2, "column": (i - 1) % 2, "ra_deg": float(i), "dec_deg": float(i)}
                          for i in range(1, 5)}}
    doc = build_native_grid_document(geometry, processed(1, 3.0), {}, "s", "X")
    out = Path("/tmp") / "native_grid_test.png"
    try:
        write_native_grid_png(out, doc)
        assert out.is_file() and out.stat().st_size > 0
    finally:
        out.unlink(missing_ok=True)


def test_no_interpolation_keyword_used_in_source():
    source = Path("quicklook_map.py").read_text()
    assert 'interpolation="none"' in source
    assert "griddata(" not in source
    for token in ("interpolation=\"bilinear\"", "interpolation=\"bicubic\"", "gaussian_filter", "scipy.ndimage"):
        assert token not in source


# ---------------------------------------------------------------- §9/§11: Console map_available integration

def test_console_watcher_detects_map_available_from_native_grid_document(tmp_path):
    import almita_console_watcher as watcher
    from runtime_state import atomic_write_json, announce_session

    runtime_dir = tmp_path / "runtime"
    quicklook_root = tmp_path / "quicklook_out"
    quicklook_root.mkdir()
    grid_dir = make_grid_dir(tmp_path)
    geometry = load_native_grid_geometry(grid_dir)
    doc = build_native_grid_document(geometry, processed(1, 1.0), read_cell_status(grid_dir), "S1", "X")
    atomic_write_json(quicklook_root / "quicklook_map.json", doc)
    atomic_write_json(quicklook_root / "quicklook_live_status.json",
                       {"status": "OK", "points_processed": 1, "updated_utc": "2026-08-30T00:00:00+00:00"})
    announce_session(runtime_dir, session_id="S1", event="SESSION_STARTED", state="RUNNING")
    atomic_write_json(runtime_dir / "quicklook_announcement.json",
                       {"schema_version": 1, "session_id": "S1", "quicklook_root": str(quicklook_root),
                        "updated_utc": "2026-08-30T00:00:00+00:00"})
    status = watcher.build_status("2026-08-30T00:00:01+00:00", 0.0, watcher.WatcherState(), runtime_dir,
                                  lambda: {"system": {}, "storage": {}, "network": {}, "sdr": {}, "temperatures": {}, "mount": {}},
                                  capture_process_detected=True)
    assert status["quicklook"]["map_available"] is True


# ---------------------------------------------------------------- §13: offline-first

def test_offline_first_no_network_in_map_module():
    source = Path("quicklook_map.py").read_text()
    for token in ("requests", "urllib.request", "http.client", "socket.connect"):
        assert token not in source
