"""OUTPUT SELF-DESCRIPTION (section 69-70): opening science_cube.h5 or a
map's own .h5 ALONE must expose schema/session identity, axis order,
units, beam model/FWHM/status/source, config hash, and quality/data
completeness - without the manifest sidecar.
"""
from pathlib import Path

import h5py
import json
import pytest

from science_engine.config import ScienceConfig
from science_engine.products import run_science_session

REAL_SESSION = "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411"


def _skip_if_absent():
    if not Path(REAL_SESSION).is_dir():
        pytest.skip(f"real REDUCE session fixture not present: {REAL_SESSION}")


@pytest.fixture(scope="module")
def real_science_session(tmp_path_factory):
    _skip_if_absent()
    config = ScienceConfig(beam_fwhm_deg=1.5, pixels_per_beam=4.0)
    report = run_science_session(REAL_SESSION, config, output_root=str(tmp_path_factory.mktemp("science")))
    return Path(report.output_dir)


def test_cube_is_self_describing_without_manifest(real_science_session):
    with h5py.File(real_science_session / "cube" / "science_cube.h5") as f:
        for key in ("science_schema_version", "axis_order", "campaign_id", "reduce_session_id",
                   "science_session_id", "config_hash", "quality_state", "data_completeness",
                   "beam_json", "units_json", "grid_json"):
            assert key in f.attrs, f"science_cube.h5 missing self-describing attr: {key}"
        beam = json.loads(f.attrs["beam_json"])
        for beam_key in ("model_type", "fwhm_deg", "source", "status", "cutoff_n_fwhm"):
            assert beam_key in beam
        assert f.attrs["axis_order"] == "velocity,y,x"
        assert f.attrs["data_completeness"] in ("COMPLETE", "PARTIAL")


def test_map_is_self_describing_without_manifest(real_science_session):
    with h5py.File(real_science_session / "maps" / "integrated_relative_intensity.h5") as f:
        for key in ("science_schema_version", "kind", "units", "campaign_id", "reduce_session_id",
                   "science_session_id", "config_hash", "quality_state", "data_completeness", "beam_json",
                   "grid_json", "metadata_json"):
            assert key in f.attrs, f"map .h5 missing self-describing attr: {key}"


def test_manifest_config_hash_matches_persisted_hdf5_config_hash(real_science_session):
    manifest = json.loads((real_science_session / "manifest.json").read_text())
    with h5py.File(real_science_session / "cube" / "science_cube.h5") as f:
        assert f.attrs["config_hash"] == manifest["config_hash"]
