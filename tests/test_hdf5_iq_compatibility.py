import h5py
import numpy as np

from sdr_capture import read_hdf5_iq_components, write_hdf5_atomic


def metadata(filename="point.h5"):
    return {
        "center_frequency_hz": 1420405752,
        "sample_rate_hz": 2400000,
        "num_samples": 3,
        "capture_status": "success",
        "final_filename": filename,
        "created_at": "2026-08-23T00:00:00+00:00",
    }


def test_new_file_contains_only_canonical_iq_and_derives_identical_components(tmp_path):
    interleaved = np.array([1, 2, 3, 4, 5, 6], dtype=np.uint8)
    final = tmp_path / "point.h5"
    write_hdf5_atomic(str(final), interleaved, metadata(), 3)
    with h5py.File(final, "r") as capture_file:
        assert set(capture_file.keys()) == {"iq_data"}
        i_samples, q_samples = read_hdf5_iq_components(capture_file)
        np.testing.assert_array_equal(i_samples, np.array([1, 3, 5], dtype=np.uint8))
        np.testing.assert_array_equal(q_samples, np.array([2, 4, 6], dtype=np.uint8))
    assert not final.with_name(final.name + ".part").exists()


def test_historical_three_dataset_file_prefers_canonical_iq(tmp_path):
    path = tmp_path / "historical.h5"
    with h5py.File(path, "w") as capture_file:
        capture_file.create_dataset("iq_data", data=np.array([10, 20, 30, 40], dtype=np.uint8))
        capture_file.create_dataset("i_samples", data=np.array([99, 99], dtype=np.uint8))
        capture_file.create_dataset("q_samples", data=np.array([88, 88], dtype=np.uint8))
    with h5py.File(path, "r") as capture_file:
        i_samples, q_samples = read_hdf5_iq_components(capture_file)
    np.testing.assert_array_equal(i_samples, [10, 30])
    np.testing.assert_array_equal(q_samples, [20, 40])


def test_historical_split_datasets_equal_components_derived_from_iq_data(tmp_path):
    path = tmp_path / "historical_matching.h5"
    interleaved = np.array([11, 12, 21, 22, 31, 32], dtype=np.uint8)
    with h5py.File(path, "w") as capture_file:
        capture_file.create_dataset("iq_data", data=interleaved)
        capture_file.create_dataset("i_samples", data=interleaved[0::2])
        capture_file.create_dataset("q_samples", data=interleaved[1::2])
    with h5py.File(path, "r") as capture_file:
        expected_i = capture_file["i_samples"][:]
        expected_q = capture_file["q_samples"][:]
        actual_i, actual_q = read_hdf5_iq_components(capture_file)
    np.testing.assert_array_equal(actual_i, expected_i)
    np.testing.assert_array_equal(actual_q, expected_q)


def test_legacy_split_only_file_remains_readable(tmp_path):
    path = tmp_path / "legacy.h5"
    with h5py.File(path, "w") as capture_file:
        capture_file.create_dataset("i_samples", data=np.array([7, 8], dtype=np.uint8))
        capture_file.create_dataset("q_samples", data=np.array([9, 10], dtype=np.uint8))
    with h5py.File(path, "r") as capture_file:
        i_samples, q_samples = read_hdf5_iq_components(capture_file)
    np.testing.assert_array_equal(i_samples, [7, 8])
    np.testing.assert_array_equal(q_samples, [9, 10])
