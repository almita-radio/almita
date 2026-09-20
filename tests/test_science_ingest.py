"""SCIENCE INPUT CONTRACT gate (section 6): validate_science_input runs
FIRST, an unknown schema/empty session is rejected before any array is
touched. Uses the real REDUCE session produced for this pass's audit.
"""
import json
import shutil
from pathlib import Path

import pytest

from science_engine.ingest import ScienceContractError, load_science_input

REAL_SESSION = "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411"


def _skip_if_absent():
    if not Path(REAL_SESSION).is_dir():
        pytest.skip(f"real REDUCE session fixture not present: {REAL_SESSION}")


def test_loads_real_session_with_all_nine_points():
    _skip_if_absent()
    science_input = load_science_input(REAL_SESSION)
    assert science_input.ok
    assert science_input.campaign_id == "ALMITA-WEB-SMALL-RUN-01"
    assert len(science_input.points) == 9
    for point in science_input.points:
        assert point.frequency_hz.shape == point.relative_intensity.shape
        assert point.velocity_lsrk_m_s is not None
        assert 0.0 <= point.ra_deg < 360.0


def test_ra_deg_matches_ra_hours_times_15():
    _skip_if_absent()
    science_input = load_science_input(REAL_SESSION)
    p = science_input.points[0]
    assert abs(p.ra_deg - p.ra_hours * 15.0) < 1e-9


def test_rejects_missing_manifest(tmp_path):
    with pytest.raises(ScienceContractError):
        load_science_input(tmp_path)


def test_rejects_unknown_point_schema_version(tmp_path):
    _skip_if_absent()
    fake = tmp_path / "corrupted"
    shutil.copytree(REAL_SESSION, fake)
    point_json = fake / "points" / "1" / "master_spectrum.json"
    data = json.loads(point_json.read_text())
    data["reduce_schema_version"] = "99.0"
    point_json.write_text(json.dumps(data))

    with pytest.raises(ScienceContractError):
        load_science_input(fake)


def test_rejects_session_with_no_completed_points(tmp_path):
    _skip_if_absent()
    fake = tmp_path / "empty"
    shutil.copytree(REAL_SESSION, fake)
    manifest_path = fake / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for point in manifest["points"]:
        point["status"] = "BLOCKED"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ScienceContractError):
        load_science_input(fake)


def test_never_reads_data_mosaic(monkeypatch):
    """Section 1/47: SCIENCE must never touch data/mosaic, not even
    indirectly through ingest."""
    _skip_if_absent()
    import builtins
    original_open = builtins.open

    def _guarded_open(file, *args, **kwargs):
        if "data/mosaic" in str(file) or "data\\mosaic" in str(file):
            raise AssertionError(f"load_science_input touched RAW: {file}")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open)
    science_input = load_science_input(REAL_SESSION)
    assert science_input.ok
