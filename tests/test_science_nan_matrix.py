"""ADVERSARIAL NaN MATRIX (sections 11-14): every combination of
NaN/Inf value, uncertainty, and weight that could poison
GriddingAccumulator's running sum - the exact class of bug this pass's
real run caught (0*NaN==NaN). Each case states its expected behavior
explicitly; none may silently corrupt a voxel that has at least one
sane contributor.
"""
import numpy as np
import pytest

from science_engine.gridding import GriddingAccumulator, bin_validity


def _one_point_result(beam, value, sigma, valid):
    acc = GriddingAccumulator(shape=(1, 1))
    acc.add_point(np.array([[beam]]), value, sigma, valid)
    v, u, w, n = acc.finalize()
    return v[0, 0], u[0, 0], w[0, 0], n[0, 0]


# ---------------------------------------------------------------- single-contributor adversarial cases

def test_nan_intensity_masked_excluded_cleanly():
    """NaN intensity + masked (bin_valid=False, matching real REDUCE) -> no contribution, no poisoning."""
    v, u, w, n = _one_point_result(1.0, np.nan, 1.0, False)
    assert np.isnan(v) and w == 0 and n == 0


def test_nan_intensity_unmasked_never_contributes():
    """NaN intensity + a caller that (wrongly) says bin_valid=True: the accumulator enforces validity itself -
    the NaN is EXCLUDED (weight 0, count 0), it neither contributes nor poisons. (The previous behavior,
    'fail loud with a NaN voxel', let one malformed bin destroy a voxel that has sane contributors.)"""
    v, u, w, n = _one_point_result(1.0, np.nan, 1.0, True)
    assert np.isnan(v) and w == 0 and n == 0

    acc = GriddingAccumulator(shape=(1, 1))
    acc.add_point(np.array([[1.0]]), 42.0, 2.0, True)
    acc.add_point(np.array([[1.0]]), np.nan, 1.0, True)      # valid-flagged NaN AFTER a sane contributor
    acc.add_point(np.array([[1.0]]), np.inf, 1.0, True)      # valid-flagged Inf
    v, u, w, n = acc.finalize()
    assert np.isclose(v[0, 0], 42.0) and n[0, 0] == 1
    assert acc.n_nonfinite_rejected == 2                     # visible in QC, not silent


def test_bin_validity_rejects_nonfinite_values_when_values_given():
    mask = np.zeros(3, dtype=np.int64)
    valid = bin_validity(mask, np.ones(3), 1e-6, values=np.array([1.0, np.nan, np.inf]))
    assert valid.tolist() == [True, False, False]


def test_finite_intensity_nan_uncertainty_excluded_by_bin_validity():
    mask = np.array([0])   # MaskFlag.GOOD
    uncertainty = np.array([np.nan])
    valid = bin_validity(mask, uncertainty, uncertainty_floor_relative=1e-6)
    assert not valid[0]
    v, u, w, n = _one_point_result(1.0, 5.0, np.nan, valid[0])
    assert np.isnan(v) and w == 0


def test_finite_intensity_inf_uncertainty_excluded_by_bin_validity():
    mask = np.array([0])
    uncertainty = np.array([np.inf])
    valid = bin_validity(mask, uncertainty, uncertainty_floor_relative=1e-6)
    assert not valid[0]
    v, u, w, n = _one_point_result(1.0, 5.0, np.inf, valid[0])
    assert np.isnan(v) and w == 0


def test_zero_beam_weight_excludes_contribution_without_poisoning():
    v, u, w, n = _one_point_result(0.0, 5.0, 1.0, True)
    assert np.isnan(v) and w == 0 and n == 0


@pytest.mark.parametrize("bad_weight", [np.inf, -np.inf, np.nan, -0.5])
def test_non_finite_or_negative_spatial_weight_is_a_loud_contract_error(bad_weight):
    """beam_weight() is bounded to [0, 1] by construction, so a non-finite/negative weight is a CALLER bug:
    it raises (never a quiet wrong voxel, never a NaN that looks like missing data)."""
    with pytest.raises(ValueError):
        _one_point_result(bad_weight, 5.0, 1.0, True)


# ---------------------------------------------------------------- multi-contributor adversarial cases

def test_all_contributors_nan_stays_nan_not_zero():
    acc = GriddingAccumulator(shape=(1, 1))
    for _ in range(3):
        acc.add_point(np.array([[1.0]]), np.nan, 1.0, False)
    v, u, w, n = acc.finalize()
    assert np.isnan(v[0, 0]) and w[0, 0] == 0 and n[0, 0] == 0


def test_one_sane_contributor_among_many_nan_ones_is_not_poisoned():
    """The exact real-run failure mode: several masked/NaN contributors
    plus ONE sane one at the same voxel - the sane one must survive."""
    acc = GriddingAccumulator(shape=(1, 1))
    acc.add_point(np.array([[1.0]]), np.nan, 1.0, False)   # masked/NaN
    acc.add_point(np.array([[1.0]]), np.nan, np.nan, False)  # masked/NaN, both value and sigma bad
    acc.add_point(np.array([[1.0]]), 42.0, 2.0, True)       # the one sane contributor
    acc.add_point(np.array([[1.0]]), np.nan, 1.0, False)   # masked/NaN
    v, u, w, n = acc.finalize()
    assert np.isclose(v[0, 0], 42.0)
    assert n[0, 0] == 1


def test_all_masked_point_is_excluded_entirely_via_bin_validity():
    mask = np.full(8, MaskFlagRFI())
    uncertainty = np.full(8, 1.0)
    valid = bin_validity(mask, uncertainty, uncertainty_floor_relative=1e-6)
    assert not np.any(valid)


def MaskFlagRFI():
    from reduce_engine.models import MaskFlag
    return MaskFlag.RFI.value


# ---------------------------------------------------------------- zero/tiny/negative sigma (section 30)

@pytest.mark.parametrize("sigma", [0.0, -1.0, 1e-300, 1e-160, np.nan, np.inf])
def test_pathological_sigma_never_produces_infinite_or_nan_weight_silently(sigma):
    mask = np.array([0])
    uncertainty = np.array([sigma])
    valid = bin_validity(mask, uncertainty, uncertainty_floor_relative=1e-6)
    v, u, w, n = _one_point_result(1.0, 5.0, sigma, bool(valid[0]))
    assert np.isfinite(w)  # never inf, never nan - either 0 (excluded) or a real finite weight
    if not valid[0]:
        assert w == 0.0


def test_many_contributors_at_the_smallest_accepted_sigma_do_not_overflow():
    """The absolute sigma floor is DERIVED so that 2**20 weights of 1/sigma_min^2 still fit in float64
    (see gridding.SIGMA_ABSOLUTE_FLOOR). The previous floor (sqrt(tiny) ~ 1.5e-154) overflowed after 4."""
    from science_engine.gridding import SIGMA_ABSOLUTE_FLOOR
    acc = GriddingAccumulator(shape=(1, 1))
    for _ in range(1000):
        acc.add_point(np.array([[1.0]]), 5.0, SIGMA_ABSOLUTE_FLOOR * 1.0001, True)
    v, u, w, n = acc.finalize()
    assert np.isfinite(w[0, 0]) and np.isclose(v[0, 0], 5.0) and n[0, 0] == 1000
    assert np.isclose(u[0, 0], SIGMA_ABSOLUTE_FLOOR * 1.0001 / np.sqrt(1000), rtol=1e-9)   # never a false sigma == 0


def test_zero_is_a_measurement_not_missing():
    """Section 14: a valid relative_intensity of exactly 0.0 contributes and is VALID; a masked bin does not.
    They must never be conflated in the voxel or in the finalised arrays."""
    acc = GriddingAccumulator(shape=(2, 1, 1))     # two channels, one pixel
    acc.add_point(np.array([[1.0]]), np.array([0.0, np.nan]), np.array([1.0, np.nan]), np.array([True, False]))
    v, u, w, n = acc.finalize()
    assert v[0, 0, 0] == 0.0 and w[0, 0, 0] > 0 and n[0, 0, 0] == 1       # measured zero
    assert np.isnan(v[1, 0, 0]) and w[1, 0, 0] == 0 and n[1, 0, 0] == 0   # missing
