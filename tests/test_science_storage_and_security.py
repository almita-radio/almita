"""Path safety (section 113, mirrors test_reduce_timezone_and_security.py's
already-validated pattern) and replay determinism (sections 88-89) for
science_engine.storage / science_engine.products.
"""
import os
from pathlib import Path

import numpy as np
import pytest

from science_engine.config import ScienceConfig
from science_engine.products import run_science_session
from science_engine.storage import ScienceSession, validate_science_session

REAL_SESSION = "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411"


def _skip_if_absent():
    if not Path(REAL_SESSION).is_dir():
        pytest.skip(f"real REDUCE session fixture not present: {REAL_SESSION}")


# ---------------------------------------------------------------- path safety

def test_campaign_id_with_dotdot_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ScienceSession(tmp_path, "../../etc")


def test_campaign_id_with_absolute_path_component_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ScienceSession(tmp_path, "/etc/passwd")


def test_campaign_id_with_embedded_slash_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ScienceSession(tmp_path, "campaign/../../escape")


def test_explicit_session_id_with_dotdot_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ScienceSession(tmp_path, "SAFE-CAMPAIGN", session_id="../../../escape")


def test_valid_campaign_and_session_id_still_work(tmp_path):
    session = ScienceSession(tmp_path, "SAFE-CAMPAIGN", session_id="SCIENCE-TEST-001")
    assert session.dir == (tmp_path / "SAFE-CAMPAIGN" / "SCIENCE-TEST-001").resolve()
    assert session.dir.is_dir()


def test_symlink_output_root_cannot_be_used_to_escape(tmp_path):
    real_root = tmp_path / "real_data_disk"
    real_root.mkdir()
    link_root = tmp_path / "science_link"
    os.symlink(real_root, link_root)
    session = ScienceSession(link_root, "CAMP", session_id="SCIENCE-TEST-002")
    assert session.dir == (real_root / "CAMP" / "SCIENCE-TEST-002").resolve()


def test_session_id_collision_raises_not_overwrite(tmp_path):
    ScienceSession(tmp_path, "CAMP", session_id="SCIENCE-DUP")
    with pytest.raises(FileExistsError):
        ScienceSession(tmp_path, "CAMP", session_id="SCIENCE-DUP")


# ---------------------------------------------------------------- replay determinism

def test_two_runs_of_the_same_input_and_config_are_numerically_identical(tmp_path):
    """Sections 88-89: same REDUCE input + same ScienceConfig -> the
    same science arrays, byte-for-byte where sensible, numerically
    identical everywhere else. Session ids/timestamps naturally differ."""
    _skip_if_absent()
    config = ScienceConfig(beam_fwhm_deg=1.5, pixels_per_beam=4.0)
    report_a = run_science_session(REAL_SESSION, config, output_root=str(tmp_path / "a"))
    report_b = run_science_session(REAL_SESSION, config, output_root=str(tmp_path / "b"))

    import h5py
    with h5py.File(Path(report_a.output_dir) / "cube" / "science_cube.h5") as fa, \
         h5py.File(Path(report_b.output_dir) / "cube" / "science_cube.h5") as fb:
        for key in ("velocity_lsrk_m_s", "relative_intensity", "uncertainty", "weight_sum", "n_pointings", "valid"):
            a, b = fa[key][:], fb[key][:]
            assert np.array_equal(a, b, equal_nan=True), f"{key} differs between two runs of identical input"

    assert report_a.quality_state == report_b.quality_state
    assert report_a.grid_shape == report_b.grid_shape
    assert report_a.n_input_points == report_b.n_input_points
    assert report_a.session_id != report_b.session_id  # allowed, expected to differ


def test_output_integrity_check_passes_on_a_real_run(tmp_path):
    _skip_if_absent()
    config = ScienceConfig(beam_fwhm_deg=1.5, pixels_per_beam=4.0)
    report = run_science_session(REAL_SESSION, config, output_root=str(tmp_path))
    result = validate_science_session(report.output_dir)
    assert result["ok"], result["problems"]


def test_output_integrity_check_catches_a_dangling_product_reference(tmp_path):
    _skip_if_absent()
    config = ScienceConfig(beam_fwhm_deg=1.5, pixels_per_beam=4.0)
    report = run_science_session(REAL_SESSION, config, output_root=str(tmp_path))
    cube_path = Path(report.output_dir) / "cube" / "science_cube.h5"
    cube_path.unlink()
    result = validate_science_session(report.output_dir)
    assert not result["ok"]
    assert any("dangling" in p for p in result["problems"])
