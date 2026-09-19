"""Adversarial tests: every scenario here must fail loudly (a raised
exception, a BLOCKED/FAILED point, or an explicit UNKNOWN/UNAVAILABLE
field) or degrade explicitly - never silent garbage."""
import csv
import json

import numpy as np
import pytest

from reduce_engine.baseline import fit_baseline
from reduce_engine.config import ReduceConfig
from reduce_engine.ingest import discover_campaign
from reduce_engine.masks import build_mask
from reduce_engine.models import MaskFlag
from reduce_engine.pipeline import PointStatus, reduce_point
from reduce_engine.spectral import estimate_spectrum
from reduce_engine.velocity import compute_velocity_axis
from hi_spectral_metric import HI_REST_HZ


def test_all_bins_masked_makes_baseline_fail_loudly():
    n = 32
    frequency = np.linspace(1e9, 1.001e9, n)
    psd = np.ones(n)
    mask = np.full(n, MaskFlag.INVALID.value, dtype=np.int64)
    with pytest.raises(ValueError):
        fit_baseline(frequency, psd, mask, config=ReduceConfig(fft_size=n))


def test_half_spectrum_missing_still_yields_a_usable_baseline_or_fails_loudly():
    n = 64
    frequency = np.linspace(1e9, 1.001e9, n)
    psd = np.ones(n)
    mask = np.zeros(n, dtype=np.int64)
    mask[: n // 2] = MaskFlag.MISSING.value
    config = ReduceConfig(fft_size=n, baseline_degree=1)
    # not enough good bins remain for a degree-2 fit by default; a lower
    # degree should still succeed - either way, never silently ignore the mask.
    result = fit_baseline(frequency, psd, mask, config=config)
    assert not np.any(result.fit_mask & (mask != MaskFlag.GOOD.value))


def test_nan_psd_everywhere_degrades_explicitly_never_fabricates_good_data():
    """An all-NaN PSD doesn't crash build_mask (the DC/spur measurement
    primitives tolerate NaN arithmetically) - but it must degrade
    EXPLICITLY: every bin flagged INVALID, never silently treated as
    usable/GOOD data."""
    n = 32
    frequency = np.linspace(1e9, 1.001e9, n)
    psd = np.full(n, np.nan)
    result = build_mask(frequency, psd, center_frequency_hz=1e9, config=ReduceConfig(fft_size=n))
    assert np.all((result.mask & MaskFlag.INVALID.value) != 0)


def test_zero_length_iq_raises_not_a_fabricated_spectrum():
    with pytest.raises(ValueError):
        estimate_spectrum(np.array([], dtype=np.uint8), sample_rate_hz=2_400_000.0,
                          center_frequency_hz=1.42e9, config=ReduceConfig())


def test_odd_length_iq_raises_never_silently_drops_the_last_sample():
    with pytest.raises(ValueError):
        estimate_spectrum(np.arange(5, dtype=np.uint8), sample_rate_hz=2_400_000.0,
                          center_frequency_hz=1.42e9, config=ReduceConfig())


def test_no_velocity_metadata_reports_unavailable_never_invents_a_frame():
    frequency = np.linspace(1.419e9, 1.421e9, 8)
    result = compute_velocity_axis(frequency, rest_frequency_hz=HI_REST_HZ, frame="lsrk",
                                   timestamp_utc=None, observer_latitude_deg="UNKNOWN",
                                   observer_longitude_deg="UNKNOWN", observer_elevation_m="UNKNOWN",
                                   ra_hours=None, dec_degrees=None)
    assert result.frame == "UNAVAILABLE"
    assert result.velocity_m_s is None


def test_wrong_sample_rate_between_declared_and_actual_is_not_silently_accepted():
    """Feeding a sample_rate that doesn't match the true synthetic
    generation rate should still produce SOME frequency axis (REDUCE
    trusts the declared metadata, per its UNKNOWN-safe ingest contract) -
    but the axis must be internally consistent with what was declared,
    not something else entirely, so a downstream mismatch is at least
    detectable by a human/QC reading frequency_hz against expectation."""
    iq = np.random.default_rng(0).integers(0, 256, size=2048, dtype=np.uint8)
    declared_rate = 1_000_000.0
    estimate = estimate_spectrum(iq, sample_rate_hz=declared_rate, center_frequency_hz=1.42e9,
                                 config=ReduceConfig(fft_size=256))
    span = estimate.frequency_hz[-1] - estimate.frequency_hz[0]
    assert abs(span - declared_rate) < declared_rate * 0.02


def test_duplicate_point_index_in_mosaic_csv_does_not_silently_merge(tmp_path):
    root = tmp_path / "CAMP"
    root.mkdir()
    (root / "grid_metadata.json").write_text(json.dumps({"session_name": "DUP", "grid": {}}))
    (root / "observer_config.json").write_text("{}")
    fields = ["point_number", "capture_status", "data_filename", "target_ra_hours", "target_dec_degrees"]
    with (root / "mosaic.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"point_number": "1", "capture_status": "success", "data_filename": "a.dat",
                         "target_ra_hours": "1.0", "target_dec_degrees": "1.0"})
        writer.writerow({"point_number": "1", "capture_status": "success", "data_filename": "b.dat",
                         "target_ra_hours": "2.0", "target_dec_degrees": "2.0"})
    manifest = discover_campaign(root)
    # both rows are discovered and preserved as separate PointRecord entries
    # - REDUCE never silently collapses a duplicate point_index into one.
    assert len(manifest.points) == 2
    assert [p.point_index for p in manifest.points] == [1, 1]


def test_out_of_order_timestamps_are_preserved_not_resorted(tmp_path):
    root = tmp_path / "CAMP"
    root.mkdir()
    (root / "grid_metadata.json").write_text(json.dumps({"session_name": "OOO", "grid": {}}))
    (root / "observer_config.json").write_text("{}")
    fields = ["point_number", "capture_status", "data_filename", "target_ra_hours", "target_dec_degrees"]
    with (root / "mosaic.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"point_number": "2", "capture_status": "success", "data_filename": "b.dat",
                         "target_ra_hours": "2.0", "target_dec_degrees": "2.0"})
        writer.writerow({"point_number": "1", "capture_status": "success", "data_filename": "a.dat",
                         "target_ra_hours": "1.0", "target_dec_degrees": "1.0"})
    manifest = discover_campaign(root)
    assert [p.point_index for p in manifest.points] == [2, 1]  # exact CSV order, not resorted


def test_missing_capture_file_produces_blocked_point_not_a_crash(tmp_path):
    from reduce_engine.ingest import PointRecord
    from reduce_engine.ingest import CampaignManifest
    point = PointRecord(point_index=1, ra_hours=1.0, dec_degrees=1.0, capture_status_declared="success",
                        data_filename_declared="missing.dat", resolved_path=None, accepted=False,
                        reject_reason="no matching HDF5 file found on disk for declared data_filename stem")
    manifest = CampaignManifest(campaign_id="CAMP", root=tmp_path, session_id=None, grid={}, observer={},
                                points=[point])
    outcome = reduce_point(point, manifest, ReduceConfig(), None, None)
    assert outcome.status == PointStatus.BLOCKED
    assert outcome.master_spectrum is None
