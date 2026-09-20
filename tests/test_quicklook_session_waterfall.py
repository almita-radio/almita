import json
from pathlib import Path

import pytest
from PIL import Image

from quicklook_session_waterfall import update_session_waterfall
from quicklook_spectrum import generate_quicklook

ROOT = Path(__file__).parent.parent
PROFILE = ROOT / "data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1.npz"
SOURCE = ROOT / "data/rf_characterization/INDOOR-ANTENNA-COUPLING-CHECK-01-20260827T003104Z/antenna_a.h5"


@pytest.fixture(scope="module")
def spectrum_arrays(tmp_path_factory):
    output = tmp_path_factory.mktemp("session_waterfall_spectrum_fixture")
    _, arrays = generate_quicklook(SOURCE, PROFILE, output)
    return arrays


def _update(tmp_path, arrays, point_id, **kwargs):
    update_session_waterfall(
        tmp_path, session_id="SESS-1", point_id=point_id,
        frequency_hz=arrays["frequency_hz"], relative_db=arrays["relative_psd_db"],
        valid_mask=arrays["valid_mask"], **kwargs,
    )


def test_creates_state_and_products(tmp_path, spectrum_arrays):
    _update(tmp_path, spectrum_arrays, "P001")
    assert (tmp_path / "session_waterfall_state.json").is_file()
    assert (tmp_path / "session_waterfall.png").is_file()
    document = json.loads((tmp_path / "session_waterfall.json").read_text())
    assert document["session_id"] == "SESS-1"
    assert document["point_ids"] == ["P001"]
    assert document["point_count"] == 1


def test_repeated_point_id_replaces_not_duplicates(tmp_path, spectrum_arrays):
    _update(tmp_path, spectrum_arrays, "P001")
    _update(tmp_path, spectrum_arrays, "P001")
    document = json.loads((tmp_path / "session_waterfall.json").read_text())
    assert document["point_ids"] == ["P001"]


def test_distinct_point_ids_accumulate_in_order(tmp_path, spectrum_arrays):
    for point_id in ("P001", "P002", "P003"):
        _update(tmp_path, spectrum_arrays, point_id)
    document = json.loads((tmp_path / "session_waterfall.json").read_text())
    assert document["point_ids"] == ["P001", "P002", "P003"]
    assert document["point_count"] == 3


def test_max_rows_evicts_oldest(tmp_path, spectrum_arrays):
    for point_id in ("P001", "P002", "P003"):
        _update(tmp_path, spectrum_arrays, point_id, max_rows=2)
    document = json.loads((tmp_path / "session_waterfall.json").read_text())
    assert document["point_ids"] == ["P002", "P003"]


def test_frequency_decimated_to_requested_bin_count(tmp_path, spectrum_arrays):
    _update(tmp_path, spectrum_arrays, "P001", frequency_bins=64)
    document = json.loads((tmp_path / "session_waterfall.json").read_text())
    assert len(document["frequency_mhz"]) == 64
    assert len(document["frequency_mhz"]) < len(spectrum_arrays["frequency_hz"])


def test_session_id_mismatch_resets_history(tmp_path, spectrum_arrays):
    update_session_waterfall(
        tmp_path, session_id="OLD-SESSION", point_id="P001",
        frequency_hz=spectrum_arrays["frequency_hz"], relative_db=spectrum_arrays["relative_psd_db"],
        valid_mask=spectrum_arrays["valid_mask"],
    )
    _update(tmp_path, spectrum_arrays, "P002")
    document = json.loads((tmp_path / "session_waterfall.json").read_text())
    assert document["session_id"] == "SESS-1"
    assert document["point_ids"] == ["P002"]


def test_png_dimensions_are_fixed_regardless_of_row_count(tmp_path, spectrum_arrays):
    # Regression guard for the grid_generator.py bbox_inches="tight" incident:
    # this renderer must never let canvas size grow with the amount of data.
    for index in range(40):
        _update(tmp_path, spectrum_arrays, f"P{index:03d}")
    with Image.open(tmp_path / "session_waterfall.png") as image:
        dimensions_at_40_rows = image.size
    assert dimensions_at_40_rows == (1800, 900)  # figsize=(12,6) @ dpi=150

    single_row_dir = tmp_path / "single_row"
    _update(single_row_dir, spectrum_arrays, "P000")
    with Image.open(single_row_dir / "session_waterfall.png") as image:
        dimensions_at_1_row = image.size
    assert dimensions_at_1_row == dimensions_at_40_rows
