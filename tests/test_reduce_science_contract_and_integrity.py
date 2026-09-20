"""SCIENCE CONTRACT VALIDATOR (sections 41, 46-47) and OUTPUT INTEGRITY /
OPERATIONAL SAFETY (sections 55-57: interrupt, crash recovery, output
collision - collision itself is already covered by
test_reduce_real_historical.py's immutability test; this file adds
interrupt/crash simulation).
"""
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

from reduce_engine.config import ReduceConfig
from reduce_engine.ingest import discover_campaign
from reduce_engine.pipeline import reduce_campaign
from reduce_engine.science_contract import validate_science_input
from reduce_engine.storage import validate_session

SMALL = "data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16"
CALIBRATION_PROFILE = "data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1"


def _skip_if_absent():
    if not Path(SMALL).is_dir():
        pytest.skip(f"real fixture campaign not present: {SMALL}")


@pytest.fixture(scope="module")
def real_session(tmp_path_factory):
    _skip_if_absent()
    manifest = discover_campaign(SMALL)
    report = reduce_campaign(manifest, ReduceConfig(), output_root=str(tmp_path_factory.mktemp("reduced")),
                             calibration_profile_path=CALIBRATION_PROFILE)
    return Path(report.output_dir)


# ---------------------------------------------------------------- science contract validator

def test_real_session_passes_the_science_contract_validator(real_session):
    result = validate_science_input(real_session)
    assert result.ok, result.problems
    assert result.points_checked > 0


def test_real_session_passes_output_integrity_check(real_session):
    result = validate_session(real_session)
    assert result["ok"], result["problems"]


def test_real_session_writes_qc_products(real_session):
    """Section 51: mask occupancy, quality/calibration/velocity summaries
    - data-level QC (no images, no new plotting dependency added in V1)."""
    qc_dir = real_session / "qc"
    for name in ("mask_occupancy", "quality_summary", "calibration_summary", "velocity_summary"):
        path = qc_dir / f"{name}.json"
        assert path.exists(), f"missing QC product: {name}"
        data = json.loads(path.read_text())
        assert data  # non-empty


def test_qc_mask_occupancy_fractions_are_between_zero_and_one(real_session):
    data = json.loads((real_session / "qc" / "mask_occupancy.json").read_text())
    for fraction in data["occupancy_fraction"].values():
        assert 0.0 <= fraction <= 1.0


def test_science_contract_rejects_missing_manifest(tmp_path):
    result = validate_science_input(tmp_path)
    assert not result.ok
    assert any("manifest" in p for p in result.problems)


def test_science_contract_rejects_unknown_schema_version(real_session, tmp_path):
    import shutil
    fake = tmp_path / "corrupted_session"
    shutil.copytree(real_session, fake)
    point_json = fake / "points" / "1" / "master_spectrum.json"
    data = json.loads(point_json.read_text())
    data["reduce_schema_version"] = "99.0"  # a schema SCIENCE has never seen
    point_json.write_text(json.dumps(data))

    result = validate_science_input(fake)
    assert not result.ok
    assert any("schema" in p.lower() for p in result.problems)


def test_science_contract_rejects_dangling_point_reference(real_session, tmp_path):
    import shutil
    fake = tmp_path / "dangling_session"
    shutil.copytree(real_session, fake)
    (fake / "points" / "1" / "master_spectrum.h5").unlink()  # manifest still claims point 1 COMPLETED

    result = validate_session(fake)
    assert not result["ok"]
    assert any("dangling" in p for p in result["problems"])

    contract = validate_science_input(fake)
    assert not contract.ok


def test_science_contract_rejects_absolute_calibration_claim(real_session, tmp_path):
    import shutil
    fake = tmp_path / "fake_absolute_session"
    shutil.copytree(real_session, fake)
    point_json = fake / "points" / "1" / "master_spectrum.json"
    data = json.loads(point_json.read_text())
    data["calibration_level"] = "KELVIN"  # never a real REDUCE output, injected to prove the guard works
    point_json.write_text(json.dumps(data))

    result = validate_science_input(fake)
    assert not result.ok
    assert any("calibration_level" in p for p in result.problems)


def test_science_contract_never_opens_raw_mosaic_directory(real_session, monkeypatch):
    """Section 47: SCIENCE (and therefore the validator that stands in
    for it) must never touch data/mosaic. Monkeypatch open() to fail
    loudly if anything under data/mosaic is opened during validation."""
    import builtins
    original_open = builtins.open

    def _guarded_open(file, *args, **kwargs):
        if "data/mosaic" in str(file) or "data\\mosaic" in str(file):
            raise AssertionError(f"validate_science_input touched RAW: {file}")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open)
    result = validate_science_input(real_session)
    assert result.ok


# ---------------------------------------------------------------- 42. self-description

def test_master_spectrum_h5_is_self_describing_without_its_json_sidecar(real_session):
    """Opening master_spectrum.h5 alone - no manifest.json, no JSON
    sidecar, no README - must still answer: which schema version, which
    campaign/session/point, and what units are the arrays in."""
    h5_path = real_session / "points" / "1" / "master_spectrum.h5"
    with h5py.File(h5_path, "r") as handle:
        for key in ("reduce_schema_version", "campaign_id", "reduce_session_id", "point_index",
                   "calibration_level", "velocity_frame", "quality_state", "units_json"):
            assert key in handle.attrs, f"master_spectrum.h5 missing self-describing attr: {key}"
        assert handle.attrs["reduce_session_id"] == real_session.name
        units = json.loads(handle.attrs["units_json"])
        for array_key in ("frequency_hz", "relative_intensity", "uncertainty", "mask", "n_contributing"):
            assert array_key in units, f"units_json missing an entry for {array_key}"


def test_master_spectrum_json_carries_its_own_session_id(real_session):
    point_json = json.loads((real_session / "points" / "1" / "master_spectrum.json").read_text())
    assert point_json["reduce_session_id"] == real_session.name


# ---------------------------------------------------------------- 56. crash recovery

def test_crash_mid_campaign_leaves_completed_points_as_evidence_not_falsely_completed(tmp_path, monkeypatch):
    _skip_if_absent()
    manifest = discover_campaign(SMALL)
    call_count = {"n": 0}
    from reduce_engine import pipeline as pipeline_mod
    original_reduce_point = pipeline_mod.reduce_point

    def _crash_on_third_point(point, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated crash mid-campaign")
        return original_reduce_point(point, *args, **kwargs)

    monkeypatch.setattr(pipeline_mod, "reduce_point", _crash_on_third_point)
    with pytest.raises(RuntimeError):
        pipeline_mod.reduce_campaign(manifest, ReduceConfig(), output_root=str(tmp_path / "reduced"),
                                     calibration_profile_path=CALIBRATION_PROFILE)

    # the session directory exists with whatever was written before the
    # crash, but manifest.json was never written (never falsely reports
    # COMPLETED) - the crash propagated, exactly as a real crash should.
    session_dirs = list((tmp_path / "reduced").rglob("REDUCE-*"))
    assert len(session_dirs) == 1
    assert not (session_dirs[0] / "manifest.json").exists()
    completed_points = list((session_dirs[0] / "points").iterdir())
    assert len(completed_points) == 2  # the 2 points processed before the crash remain as real evidence
