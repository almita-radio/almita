"""GriddingAccumulator numerics (section 32): weighted-mean value and
its propagated uncertainty, verified against a direct brute-force
computation - not just self-consistency.
"""
import numpy as np
import pytest

from science_engine.gridding import GriddingAccumulator, bin_validity, point_passes_quality_policy
from reduce_engine.models import MaskFlag


def _brute_force(beams, values, sigmas):
    w = beams / sigmas ** 2
    y = np.sum(w * values) / np.sum(w)
    sigma_y = np.sqrt(np.sum(w ** 2 * sigmas ** 2) / np.sum(w) ** 2)
    return y, sigma_y


@pytest.mark.parametrize("beams,values,sigmas", [
    (np.array([1.0, 0.5, 0.2]), np.array([10.0, 12.0, 8.0]), np.array([1.0, 2.0, 0.5])),
    (np.array([1.0, 1.0]), np.array([5.0, -5.0]), np.array([1.0, 1.0])),  # equal weights -> plain mean
    (np.array([0.9, 0.1, 0.01]), np.array([100.0, 0.0, -50.0]), np.array([0.1, 10.0, 100.0])),
])
def test_2d_map_accumulator_matches_brute_force(beams, values, sigmas):
    expected_y, expected_sigma = _brute_force(beams, values, sigmas)
    acc = GriddingAccumulator(shape=(1, 1))
    for b, v, s in zip(beams, values, sigmas):
        acc.add_point(np.array([[b]]), float(v), float(s), True)
    value, uncertainty, weight_sum, n = acc.finalize()
    assert np.isclose(value[0, 0], expected_y)
    assert np.isclose(uncertainty[0, 0], expected_sigma)
    assert n[0, 0] == len(beams)


def test_cube_accumulator_matches_brute_force_per_channel():
    beams = np.array([1.0, 0.5, 0.2])
    values = np.array([10.0, 12.0, 8.0])
    sigmas = np.array([1.0, 2.0, 0.5])
    expected_y, expected_sigma = _brute_force(beams, values, sigmas)

    acc = GriddingAccumulator(shape=(1, 1, 1))
    for b, v, s in zip(beams, values, sigmas):
        acc.add_point(np.array([[b]]), np.array([v]), np.array([s]), np.array([True]))
    value, uncertainty, weight_sum, n = acc.finalize()
    assert np.isclose(value[0, 0, 0], expected_y)
    assert np.isclose(uncertainty[0, 0, 0], expected_sigma)


def test_equal_beam_and_sigma_reduces_to_plain_mean():
    acc = GriddingAccumulator(shape=(1, 1))
    for v in (2.0, 4.0, 6.0):
        acc.add_point(np.array([[1.0]]), v, 1.0, True)
    value, uncertainty, weight_sum, n = acc.finalize()
    assert np.isclose(value[0, 0], 4.0)  # plain mean of 2,4,6


def test_nan_valued_masked_bin_does_not_poison_the_running_sum():
    """Regression: real REDUCE data stores NaN (never a fabricated
    number) at every masked bin's relative_intensity. A zero weight
    alone does NOT neutralize a NaN value (0*nan==nan in IEEE754) - the
    accumulator must guard the VALUE itself, not just rely on the
    weight being zero. Caught on a real 9-point run where this exact
    pattern turned the entire integrated map into NaN."""
    acc = GriddingAccumulator(shape=(3, 1, 1))
    # channel 1 is masked (bin_valid=False) with the real-world NaN value; channels 0 and 2 are good.
    acc.add_point(np.array([[1.0]]), np.array([5.0, np.nan, 7.0]), np.array([1.0, np.nan, 1.0]),
                 np.array([True, False, True]))
    value, uncertainty, weight_sum, n = acc.finalize()
    assert np.isclose(value[0, 0, 0], 5.0)
    assert np.isnan(value[1, 0, 0])   # no contribution at all here - correctly NaN, not poisoned
    assert np.isclose(value[2, 0, 0], 7.0)
    assert n[1, 0, 0] == 0


def test_no_contribution_is_nan_not_zero():
    """Section 27/67: no data is NaN, never a fabricated 0 - 0 is a
    legitimate relative_intensity value."""
    acc = GriddingAccumulator(shape=(2, 2))
    value, uncertainty, weight_sum, n = acc.finalize()
    assert np.all(np.isnan(value))
    assert np.all(np.isnan(uncertainty))
    assert np.all(weight_sum == 0)
    assert np.all(n == 0)


def test_all_bins_zero_weight_at_one_pixel_stays_nan_others_dont():
    acc = GriddingAccumulator(shape=(1, 2))
    acc.add_point(np.array([[1.0, 0.0]]), 5.0, 1.0, True)  # only pixel (0,0) gets nonzero beam weight
    value, uncertainty, weight_sum, n = acc.finalize()
    assert np.isclose(value[0, 0], 5.0)
    assert np.isnan(value[0, 1])


def test_bin_validity_excludes_masked_bins():
    mask = np.array([MaskFlag.GOOD.value, MaskFlag.RFI.value, (MaskFlag.RFI | MaskFlag.EDGE).value])
    uncertainty = np.array([1.0, 1.0, 1.0])
    valid = bin_validity(mask, uncertainty, uncertainty_floor_relative=1e-6)
    assert list(valid) == [True, False, False]


def test_bin_validity_excludes_nonpositive_and_nonfinite_uncertainty():
    mask = np.zeros(4, dtype=np.int64)  # all GOOD
    uncertainty = np.array([1.0, 0.0, -1.0, np.inf])
    valid = bin_validity(mask, uncertainty, uncertainty_floor_relative=1e-6)
    assert list(valid) == [True, False, False, False]


def test_bin_validity_all_invalid_point_returns_all_false_not_a_floor_hack():
    mask = np.zeros(3, dtype=np.int64)
    uncertainty = np.array([0.0, np.nan, -1.0])
    valid = bin_validity(mask, uncertainty, uncertainty_floor_relative=1e-6)
    assert not np.any(valid)


@pytest.mark.parametrize("state,policy,expected", [
    ("GOOD", "STRICT", True), ("WARNING", "STRICT", False), ("UNKNOWN", "STRICT", False),
    ("GOOD", "STANDARD", True), ("WARNING", "STANDARD", True), ("BAD", "STANDARD", False),
    ("UNKNOWN", "STANDARD", False),
    ("UNKNOWN", "PERMISSIVE", True), ("BAD", "PERMISSIVE", False),
])
def test_quality_policy_gate(state, policy, expected):
    assert point_passes_quality_policy(state, policy) is expected
