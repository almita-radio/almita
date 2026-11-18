import json
from pathlib import Path

import pytest
from PIL import Image

from quicklook_session_waterfall import update_session_waterfall
from quicklook_spectrum import generate_quicklook

ROOT = Path(__file__).parent.parent
# inputs never come from data/: real V1 profile in tests/fixtures/, a synthetic antenna capture in tmp
from tests.conftest import FOUNDATION_PROFILE as PROFILE, write_capture  # noqa: E402
SOURCE = write_capture(Path(__import__("tempfile").mkdtemp(prefix="rf_captures_")) / "antenna_a.h5")


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


def test_retuned_points_are_placed_on_the_first_points_frequency_axis(tmp_path):
    """BL-105 (2026-11-18): with a WINDOW CENTRE every point is retuned by its own LSRK correction. A feature at one
    topocentric frequency must stay in ONE column for every row (rows used to be drawn on the last point's axis)."""
    import numpy as np
    f1 = 1419.5e6 + np.arange(4096) * 195.3125            # ~0.8 MHz folded window
    f2 = f1 + 40_000.0                                    # the next point, retuned 40 kHz up
    line = 1419.7e6

    def spec(f):
        return np.where(np.abs(f - line) < 3000, 10.0, 0.0), np.ones(f.size, bool)

    v1, m1 = spec(f1)
    v2, m2 = spec(f2)
    update_session_waterfall(tmp_path, session_id="s", point_id="p1", frequency_hz=f1, relative_db=v1, valid_mask=m1,
                             frequency_bins=512)
    update_session_waterfall(tmp_path, session_id="s", point_id="p2", frequency_hz=f2, relative_db=v2, valid_mask=m2,
                             frequency_bins=512)
    state = json.loads((tmp_path / "session_waterfall_state.json").read_text())
    axis = np.array(state["frequency_mhz"]) * 1e6
    assert abs(axis[0] - f1[0]) < 1000                    # the FIRST point's axis is kept
    rows = [np.array([np.nan if x is None else x for x in r["relative_db"]]) for r in state["rows"]]
    assert abs(int(np.nanargmax(rows[0])) - int(np.nanargmax(rows[1]))) <= 1   # same column (decimation rounding); was 25 off
    assert np.isnan(rows[1][:5]).all() and np.isfinite(rows[1][-5:]).all()   # p2 did not measure the low edge
