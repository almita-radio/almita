"""Unit + robustness tests for the AVERAGING/STACKING stage.

Includes the Monte Carlo comparison (Gaussian noise, single RFI spike,
gain drift, missing bins, one pathological spectrum) that documents why
inverse-variance-weighted + sigma-clip is REDUCE's default, per
docs/REDUCE_PIPELINE.md.
"""
import numpy as np
import pytest

from reduce_engine.averaging import integration_gain_curve, stack_spectra
from reduce_engine.models import MaskFlag

GOOD = MaskFlag.GOOD.value
RFI = MaskFlag.RFI.value


def test_stack_requires_at_least_one_spectrum():
    with pytest.raises(ValueError):
        stack_spectra([], [], [])


def test_two_identical_spectra_average_to_the_same_value():
    v = np.array([1.0, 2.0, 3.0])
    u = np.array([0.1, 0.1, 0.1])
    m = np.array([GOOD, GOOD, GOOD])
    result = stack_spectra([v, v], [u, u], [m, m])
    np.testing.assert_allclose(result.value, v)
    assert np.all(result.n_contributing == 2)


def test_masked_bin_in_one_capture_is_excluded_from_that_bin():
    v1 = np.array([1.0, 1.0])
    v2 = np.array([1.0, 100.0])  # second bin corrupted in capture 2
    u = np.array([0.1, 0.1])
    m1 = np.array([GOOD, GOOD])
    m2 = np.array([GOOD, RFI])
    result = stack_spectra([v1, v2], [u, u], [m1, m2])
    assert result.value[1] == pytest.approx(1.0)
    assert result.n_contributing[1] == 1


def test_inverse_variance_weighting_favors_lower_uncertainty_capture():
    v = np.array([0.0, 10.0])
    u_precise = np.array([0.01, 0.01])
    u_noisy = np.array([100.0, 100.0])
    result = stack_spectra([v[:1].repeat(1), np.array([5.0])], [u_precise[:1], u_noisy[:1]], [np.array([GOOD]), np.array([GOOD])])
    assert result.value[0] < 2.5  # closer to the precise 0.0 than the noisy 5.0


def test_equal_weight_fallback_when_no_uncertainty_available():
    v1, v2 = np.array([0.0]), np.array([10.0])
    u = np.array([np.nan])
    m = np.array([GOOD])
    result = stack_spectra([v1, v2], [u, u], [m, m], method="equal_weight")
    assert result.weighting == "equal_weight"
    assert result.value[0] == pytest.approx(5.0)


def test_sigma_clipping_removes_one_pathological_spectrum():
    rng = np.random.default_rng(1)
    n_good, n_bins = 20, 50
    good = 1.0 + 0.05 * rng.standard_normal((n_good, n_bins))
    pathological = np.full((1, n_bins), 1000.0)
    values = list(good) + list(pathological)
    unc = [np.full(n_bins, 0.05)] * (n_good + 1)
    masks = [np.full(n_bins, GOOD, dtype=np.int64)] * (n_good + 1)
    result = stack_spectra(values, unc, masks, sigma_clip_threshold=5.0)
    assert np.all(np.abs(result.value - 1.0) < 0.2)
    assert result.clipped_fraction > 0


def test_integration_gain_curve_is_diagnostic_reference_only():
    curve = integration_gain_curve(1.0, [1, 4, 9])
    np.testing.assert_allclose(curve["theoretical_rms"], [1.0, 0.5, 1 / 3])
    assert "diagnostic" in curve["note"]


# ---------------------------------------------------------------- robustness comparison (evidence for the default)

def _simulate_scenario(scenario: str, n_captures: int, n_bins: int, rng: np.random.Generator):
    true_value = np.ones(n_bins)
    values, uncertainties, masks = [], [], []
    for i in range(n_captures):
        v = true_value + 0.05 * rng.standard_normal(n_bins)
        u = np.full(n_bins, 0.05)
        m = np.full(n_bins, GOOD, dtype=np.int64)
        if scenario == "single_rfi_spike" and i == 0:
            v[n_bins // 2] += 50.0
        if scenario == "gain_drift":
            v *= (1.0 + 0.02 * i)
        if scenario == "missing_bins" and i % 3 == 0:
            v[: n_bins // 4] = np.nan  # genuinely absent, not merely flagged despite being fine
            m[: n_bins // 4] = MaskFlag.MISSING.value
        if scenario == "pathological_spectrum" and i == n_captures - 1:
            v[:] = 1000.0
        values.append(v); uncertainties.append(u); masks.append(m)
    return values, uncertainties, masks, true_value


@pytest.mark.parametrize("scenario", ["gaussian_noise", "single_rfi_spike", "gain_drift",
                                      "missing_bins", "pathological_spectrum"])
def test_default_method_recovers_true_value_better_than_naive_mean(scenario):
    rng = np.random.default_rng(42)
    values, uncertainties, masks, true_value = _simulate_scenario(scenario, n_captures=12, n_bins=64, rng=rng)

    naive_mean = np.nanmean(np.asarray(values), axis=0)  # even naive numpy needs nanmean or NaN poisons everything
    naive_error = float(np.sqrt(np.nanmean((naive_mean - true_value) ** 2)))

    default_result = stack_spectra(values, uncertainties, masks, method="inverse_variance_weighted",
                                   sigma_clip_threshold=5.0)
    default_error = float(np.sqrt(np.nanmean((default_result.value - true_value) ** 2)))

    # The whole point of mask-awareness + sigma-clipping: never worse than
    # ignoring masks/outliers entirely (naive mean), and meaningfully
    # better whenever a scenario actually contains an outlier/gap.
    assert default_error <= naive_error + 1e-9
    if scenario in ("single_rfi_spike", "pathological_spectrum"):
        # nanmean already gets partial credit for NaN-tolerance on
        # missing_bins, so the >=2x margin is reserved for the scenarios
        # where naive averaging has no such accidental escape route.
        assert default_error < naive_error * 0.5
