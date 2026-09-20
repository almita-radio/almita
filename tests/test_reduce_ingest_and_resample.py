"""Unit tests for INGEST (real campaign discovery, including the real
data_filename .dat-vs-.h5 mismatch) and RESAMPLE TO COMMON GRID (NATIVE_GRID
no-interpolation contract)."""
import csv
import json

import numpy as np
import pytest

from reduce_engine.ingest import discover_campaign, metadata_field
from reduce_engine.models import MaskFlag
from reduce_engine.resample import resample_to_common_grid

MOSAIC_FIELDS = ["point_number", "capture_status", "data_filename", "target_ra_hours", "target_dec_degrees"]


def _write_campaign(root, rows):
    root.mkdir(parents=True, exist_ok=True)
    (root / "grid_metadata.json").write_text(json.dumps({
        "session_name": "TEST-CAMPAIGN", "session_id": "20260101-000000",
        "grid": {"rows": 1, "columns": len(rows)},
    }))
    (root / "observer_config.json").write_text(json.dumps({"observer": {"latitude_deg": -33.0}}))
    with (root / "mosaic.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MOSAIC_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    iq_dir = root / "data" / "iq" / "session"
    iq_dir.mkdir(parents=True, exist_ok=True)
    return iq_dir


def test_discover_campaign_reads_grid_and_observer_metadata(tmp_path):
    root = tmp_path / "CAMP"
    _write_campaign(root, [{"point_number": "1", "capture_status": "success", "data_filename": "x_0001.dat",
                            "target_ra_hours": "1.0", "target_dec_degrees": "-10.0"}])
    manifest = discover_campaign(root)
    assert manifest.campaign_id == "TEST-CAMPAIGN"
    assert manifest.grid["columns"] == 1
    assert manifest.observer["observer"]["latitude_deg"] == -33.0


def test_discover_campaign_matches_dat_declared_name_to_real_h5_file(tmp_path):
    """Real discovered quirk: mosaic.csv says .dat, capture.py writes .h5."""
    root = tmp_path / "CAMP"
    iq_dir = _write_campaign(root, [{"point_number": "1", "capture_status": "success", "data_filename": "x_0001.dat",
                                     "target_ra_hours": "1.0", "target_dec_degrees": "-10.0"}])
    (iq_dir / "x_0001.h5").write_bytes(b"fake")
    manifest = discover_campaign(root)
    point = manifest.points[0]
    assert point.accepted
    assert point.resolved_path == iq_dir / "x_0001.h5"


def test_discover_campaign_rejects_point_with_no_matching_file(tmp_path):
    root = tmp_path / "CAMP"
    _write_campaign(root, [{"point_number": "1", "capture_status": "success", "data_filename": "missing_0001.dat",
                            "target_ra_hours": "1.0", "target_dec_degrees": "-10.0"}])
    manifest = discover_campaign(root)
    point = manifest.points[0]
    assert not point.accepted
    assert "no matching" in point.reject_reason


def test_discover_campaign_rejects_non_success_capture_status_even_if_file_exists(tmp_path):
    root = tmp_path / "CAMP"
    iq_dir = _write_campaign(root, [{"point_number": "1", "capture_status": "failed", "data_filename": "x_0001.dat",
                                     "target_ra_hours": "1.0", "target_dec_degrees": "-10.0"}])
    (iq_dir / "x_0001.h5").write_bytes(b"fake")
    manifest = discover_campaign(root)
    point = manifest.points[0]
    assert not point.accepted
    assert "capture_status_declared=failed" in point.reject_reason


def test_discover_campaign_raises_when_mosaic_csv_missing(tmp_path):
    root = tmp_path / "NOT_A_CAMPAIGN"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        discover_campaign(root)


def test_metadata_field_is_unknown_safe_never_guesses():
    assert metadata_field({}, "gain_requested_db", "gain") == "UNKNOWN"
    assert metadata_field({"gain": 40.2}, "gain_requested_db", "gain") == 40.2
    assert metadata_field({"gain_requested_db": 40.2, "gain": "auto"}, "gain_requested_db", "gain") == 40.2


# ---------------------------------------------------------------- resample

GOOD = MaskFlag.GOOD.value


def test_resample_is_noop_when_grids_already_match():
    grid = np.linspace(1e9, 1.001e9, 16)
    v, u, m = np.ones(16), np.full(16, 0.1), np.full(16, GOOD, dtype=np.int64)
    result = resample_to_common_grid([grid, grid], [v, v], [u, u], [m, m])
    assert result.method == "native_grid_no_resampling_required"
    assert result.frequency_hz is grid


def test_resample_interpolates_when_grids_differ_and_masks_gaps_conservatively():
    grid_a = np.linspace(1e9, 1.001e9, 16)
    grid_b = np.linspace(1e9, 1.001e9, 8)
    v_a, u_a, m_a = np.ones(16), np.full(16, 0.1), np.full(16, GOOD, dtype=np.int64)
    v_b, u_b, m_b = np.ones(8), np.full(8, 0.1), np.full(8, GOOD, dtype=np.int64)
    result = resample_to_common_grid([grid_a, grid_b], [v_a, v_b], [u_a, u_b], [m_a, m_b])
    assert result.method == "linear_interpolation_mask_aware"
    np.testing.assert_array_equal(result.frequency_hz, grid_a)


def test_resample_never_fabricates_data_across_a_masked_gap():
    grid_a = np.linspace(1e9, 1.001e9, 16)
    grid_b = np.linspace(1e9, 1.001e9, 8)
    v_a, u_a = np.ones(16), np.full(16, 0.1)
    m_a = np.full(16, GOOD, dtype=np.int64)
    m_a[3:6] = MaskFlag.RFI.value  # a real gap in the source grid
    v_b, u_b, m_b = np.ones(8), np.full(8, 0.1), np.full(8, GOOD, dtype=np.int64)
    result = resample_to_common_grid([grid_a, grid_b], [v_a, v_b], [u_a, u_b], [m_a, m_b])
    resampled_mask_a = result.masks[0]
    # bins interpolated from within/near the masked gap must not silently become GOOD
    assert np.any(resampled_mask_a != GOOD)
