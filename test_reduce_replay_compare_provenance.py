"""REPLAY DETERMINISM (section 25), BYTE/NUMERIC DETERMINISM (26),
CROSS-SESSION COMPARE (27), and PROVENANCE CHAIN (28) - all against the
real SMALL fixture campaign (9 points, fast) to keep this file's total
real-campaign runtime small; the multi-campaign file already covers
larger campaigns.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from reduce_engine.compare import compare_sessions
from reduce_engine.config import ReduceConfig
from reduce_engine.ingest import discover_campaign
from reduce_engine.pipeline import reduce_campaign
from reduce_engine.replay import replay_session
from reduce_engine.storage import load_master_spectrum

SMALL = "data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16"
CALIBRATION_PROFILE = "data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1"


def _skip_if_absent():
    if not Path(SMALL).is_dir():
        pytest.skip(f"real fixture campaign not present: {SMALL}")


@pytest.fixture(scope="module")
def manifest():
    _skip_if_absent()
    return discover_campaign(SMALL)


@pytest.fixture(scope="module")
def output_root(tmp_path_factory):
    return tmp_path_factory.mktemp("reduced")


@pytest.fixture(scope="module")
def run_a(manifest, output_root):
    return reduce_campaign(manifest, ReduceConfig(), output_root=str(output_root),
                           calibration_profile_path=CALIBRATION_PROFILE)


@pytest.fixture(scope="module")
def run_a_replayed(run_a, output_root):
    return replay_session(run_a.output_dir, output_root=str(output_root), calibration_profile_path=CALIBRATION_PROFILE)


@pytest.fixture(scope="module")
def run_b_different_config(manifest, output_root):
    """Same raw source, different config (different fft_size)."""
    return reduce_campaign(manifest, ReduceConfig(fft_size=4096), output_root=str(output_root),
                           calibration_profile_path=CALIBRATION_PROFILE)


@pytest.fixture(scope="module")
def run_c_no_calibration(manifest, output_root):
    """Same raw source, same config, different calibration (none)."""
    return reduce_campaign(manifest, ReduceConfig(), output_root=str(output_root), calibration_profile_path=None)


# ---------------------------------------------------------------- 25/26. replay determinism

def test_replay_is_scientifically_equivalent_arrays(run_a, run_a_replayed):
    assert run_a_replayed.session_id != run_a.session_id  # A: replay never reuses the session id
    for point_index in range(1, 10):
        meta_a, arrays_a = load_master_spectrum(Path(run_a.output_dir) / "points" / str(point_index))
        meta_b, arrays_b = load_master_spectrum(Path(run_a_replayed.output_dir) / "points" / str(point_index))
        for key in ("frequency_hz", "relative_intensity", "uncertainty", "mask", "n_contributing"):
            np.testing.assert_array_equal(arrays_a[key], arrays_b[key], err_msg=f"point {point_index} array {key}")
        if "velocity_lsrk_m_s" in arrays_a:
            np.testing.assert_allclose(arrays_a["velocity_lsrk_m_s"], arrays_b["velocity_lsrk_m_s"], rtol=0, atol=0)
        assert meta_a["quality"]["state"] == meta_b["quality"]["state"]
        assert meta_a["calibration_level"] == meta_b["calibration_level"]
        # timestamps/session ids are explicitly allowed to differ - not checked


def test_replay_arrays_are_byte_identical_science_fields_numerically_identical(run_a, run_a_replayed):
    """Classification per section 26: HDF5 science arrays are asserted
    exactly equal (atol=0, rtol=0 - not just "close") since the same
    deterministic pipeline ran on the same bytes; the JSON metadata
    files are NOT byte-identical (timestamps differ) - documented, not
    treated as a bug."""
    path_a = Path(run_a.output_dir) / "points" / "1" / "master_spectrum.h5"
    path_b = Path(run_a_replayed.output_dir) / "points" / "1" / "master_spectrum.h5"
    _, arrays_a = load_master_spectrum(path_a.parent)
    _, arrays_b = load_master_spectrum(path_b.parent)
    # equal_nan=True: masked/edge bins are legitimately NaN (never
    # zero-filled, per the mask contract) - matching NaN-for-NaN across
    # both runs IS the determinism being tested, not a mismatch.
    assert np.array_equal(arrays_a["relative_intensity"], arrays_b["relative_intensity"], equal_nan=True)

    meta_a = json.loads((path_a.parent / "master_spectrum.json").read_text())
    meta_b = json.loads((path_b.parent / "master_spectrum.json").read_text())
    assert meta_a["timestamp_start_utc"] == meta_b["timestamp_start_utc"]  # same RAW -> same declared timestamp
    assert meta_a["capture_refs"][0]["sha256"] == meta_b["capture_refs"][0]["sha256"]  # same RAW file


# ---------------------------------------------------------------- 27. cross-session compare

def test_compare_a_same_session_replay_reports_scientific_equivalence(run_a, run_a_replayed):
    result = compare_sessions(run_a.output_dir, run_a_replayed.output_dir)
    assert result["same_source_campaign"]
    assert result["same_config"]
    for rms in result["per_point_rms_difference"].values():
        assert rms == pytest.approx(0.0, abs=1e-9)


def test_compare_b_same_raw_different_config_identifies_difference(run_a, run_b_different_config):
    result = compare_sessions(run_a.output_dir, run_b_different_config.output_dir)
    assert result["same_source_campaign"]
    assert not result["same_config"]
    assert "fft_size" in result["differing_config_keys"]


def test_compare_c_different_calibration_identifies_difference(run_a, run_c_no_calibration):
    result = compare_sessions(run_a.output_dir, run_c_no_calibration.output_dir)
    assert result["quality_counts_a"] != result["quality_counts_b"]  # RELATIVE/GOOD vs UNCALIBRATED/WARNING
    rms_values = list(result["per_point_rms_difference"].values())
    assert any(isinstance(v, float) and v > 0 for v in rms_values), \
        "different calibration must produce a nonzero measured difference, not silently 'same'"


def test_compare_d_different_campaign_reports_different_source(run_a, tmp_path, output_root):
    other_manifest = discover_campaign(SMALL)
    # Build a second, distinctly-labeled session from the SAME campaign to
    # stand in for "different source" without paying for a second real
    # campaign's runtime - what compare must detect is the SOURCE FIELD
    # itself, so mutate the persisted manifest's declared source for this
    # one assertion (read-only for run_a; a throwaway copy is edited).
    import shutil
    fake_dir = tmp_path / "fake_other_session"
    shutil.copytree(run_a.output_dir, fake_dir)
    manifest_path = fake_dir / "manifest.json"
    manifest_json = json.loads(manifest_path.read_text())
    manifest_json["source_campaign_root"] = "data/mosaic/SOME-OTHER-CAMPAIGN-NEVER-REAL"
    manifest_path.write_text(json.dumps(manifest_json))

    result = compare_sessions(run_a.output_dir, fake_dir)
    assert not result["same_source_campaign"]


def test_compare_never_reduces_to_bare_files_differ(run_a, run_c_no_calibration):
    result = compare_sessions(run_a.output_dir, run_c_no_calibration.output_dir)
    # must carry structured, specific fields - not a single opaque string
    for key in ("same_source_campaign", "same_config", "points_completed_a", "points_completed_b",
               "quality_counts_a", "quality_counts_b", "per_point_rms_difference"):
        assert key in result


# ---------------------------------------------------------------- 28. provenance chain

def test_provenance_chain_from_master_spectrum_to_raw_sha256(run_a):
    """A complete, real trace: master spectrum -> reduce session ->
    config hash -> calibration profile -> capture ref -> raw HDF5 ->
    raw sha256, all readable from persisted files alone."""
    point_dir = Path(run_a.output_dir) / "points" / "1"
    meta = json.loads((point_dir / "master_spectrum.json").read_text())
    config = json.loads((Path(run_a.output_dir) / "config.json").read_text())
    provenance = json.loads((Path(run_a.output_dir) / "provenance.json").read_text())
    manifest_json = json.loads((Path(run_a.output_dir) / "manifest.json").read_text())

    chain = {
        "master_spectrum_point_index": meta["point_index"],
        "reduce_session_id": manifest_json["reduce_session_id"],
        "config_hash": provenance["config_hash"],
        "config_hash_matches_config_json": provenance["config_hash"] == ReduceConfig(**config).config_hash(),
        "calibration_profile_id": meta["calibration_profile_id"],
        "calibration_profile_hash": meta["calibration_profile_hash"],
        "capture_ref": meta["capture_refs"][0],
        "raw_hdf5_path": meta["capture_refs"][0]["source_path"],
        "raw_hdf5_exists": Path(meta["capture_refs"][0]["source_path"]).exists(),
        "raw_sha256": meta["capture_refs"][0]["sha256"],
    }
    print("\nPROVENANCE CHAIN (point 1):")
    for key, value in chain.items():
        print(f"  {key}: {value}")

    assert chain["config_hash_matches_config_json"]
    assert chain["calibration_profile_id"] == "calibration_profile_v1"
    assert chain["raw_hdf5_exists"]
    from reduce_engine.models import sha256_of_file
    assert chain["raw_sha256"] == sha256_of_file(chain["raw_hdf5_path"])  # re-hash RAW right now, must still match
