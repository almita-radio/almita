"""UNCERTAINTY CALIBRATION and COVERAGE (2nd-pass sections 20-21): Monte
Carlo over many realizations of a known signal, comparing measured
scatter between realizations to reported uncertainty (ratio should be
~1 within reasonable tolerance), plus a coverage test (what fraction of
realizations fall within +-1 sigma / +-2 sigma of the true value).

Not required to be Gaussian-perfect - required to catch gross
under-confidence (reported uncertainty far too small) or over-confidence
(far too large), either of which would make REDUCE's uncertainty
untrustworthy for SCIENCE.
"""
import numpy as np

from reduce_engine.config import ReduceConfig
from reduce_engine.masks import build_mask, usable_bin_mask
from reduce_engine.simulation import SyntheticCaptureConfig, build_synthetic_iq
from reduce_engine.spectral import estimate_spectrum
from reduce_engine.uncertainty import per_capture_uncertainty

N_REALIZATIONS = 40


def _realization(seed: int, reduce_config: ReduceConfig):
    sim_config = SyntheticCaptureConfig(fft_size=reduce_config.fft_size, n_segments=48, noise_level=1.0,
                                        random_seed=seed)
    estimate = estimate_spectrum(build_synthetic_iq(sim_config), sample_rate_hz=sim_config.sample_rate_hz,
                                 center_frequency_hz=sim_config.center_frequency_hz, config=reduce_config)
    mask_result = build_mask(estimate.frequency_hz, estimate.psd, center_frequency_hz=sim_config.center_frequency_hz,
                             config=reduce_config, segments=estimate.segments)
    reported_uncertainty = per_capture_uncertainty(estimate.segments)
    return estimate.psd, reported_uncertainty, usable_bin_mask(mask_result.mask)


def test_uncertainty_scale_ratio_is_within_reasonable_bounds():
    reduce_config = ReduceConfig(fft_size=1024)
    psd_realizations, reported_uncertainties, usable_masks = [], [], []
    for seed in range(N_REALIZATIONS):
        psd, unc, usable = _realization(1000 + seed, reduce_config)
        psd_realizations.append(psd); reported_uncertainties.append(unc); usable_masks.append(usable)

    psd_stack = np.asarray(psd_realizations)          # (N, n_bins)
    unc_stack = np.asarray(reported_uncertainties)
    usable_stack = np.asarray(usable_masks)

    measured_scatter = np.std(psd_stack, axis=0, ddof=1)    # true scatter ACROSS independent realizations
    mean_reported_uncertainty = np.mean(unc_stack, axis=0)  # each realization's own internal estimate

    common_usable = np.all(usable_stack, axis=0)
    ratio = measured_scatter[common_usable] / np.maximum(mean_reported_uncertainty[common_usable], 1e-30)
    median_ratio = float(np.median(ratio))
    # Not required to be exactly 1.0 - per_capture_uncertainty estimates
    # the uncertainty of ONE capture's own median-combined PSD from ITS
    # OWN sub-integration spread, while measured_scatter is the
    # independent, across-realization scatter of the whole capture - a
    # systematic, expected offset exists between the two, but it must
    # stay in a defensible regime (not orders of magnitude off, which
    # would mean the reported uncertainty is scientifically useless).
    assert 0.3 < median_ratio < 3.0, f"median (measured/reported) uncertainty ratio: {median_ratio:.2f}"


def test_uncertainty_coverage_one_and_two_sigma():
    """Ground truth here is the EMPIRICAL cross-realization mean, not the
    simulator's theoretical noise_level parameter.

    Real finding made while writing this test: hi_spectral_metric's
    "median" combine mode (robust_psd_from_iq's default, reused verbatim
    by reduce_engine.spectral) is a biased estimator of the underlying
    Gaussian noise variance - the median of a chi-squared-like per-bin
    power distribution is systematically below its mean by a large,
    well-known factor (~ln(2) for a single sample; ensemble median over
    many FFT segments narrows but does not remove this). Using the
    simulator's injected `noise_level` as "truth" therefore made EVERY
    z-score huge and coverage exactly 0.00 - not an uncertainty-scaling
    bug, but a mismatched reference. Since REDUCE only ever claims
    RELATIVE units (this bias is a self-consistent multiplicative
    constant, not something that leaks into calibrated/baseline-corrected
    fractional quantities), the scientifically meaningful coverage
    question is answered against the empirical population mean instead.
    """
    reduce_config = ReduceConfig(fft_size=1024)
    psd_realizations, reported_uncertainties, usable_masks = [], [], []
    for seed in range(N_REALIZATIONS):
        psd, unc, usable = _realization(2000 + seed, reduce_config)
        psd_realizations.append(psd); reported_uncertainties.append(unc); usable_masks.append(usable)
    psd_stack = np.asarray(psd_realizations)
    unc_stack = np.asarray(reported_uncertainties)
    usable_stack = np.asarray(usable_masks)
    common_usable = np.all(usable_stack, axis=0)

    empirical_true_value = np.mean(psd_stack, axis=0)

    z_scores = (psd_stack[:, common_usable] - empirical_true_value[common_usable]) / \
        np.maximum(unc_stack[:, common_usable], 1e-30)
    within_1_sigma = float(np.mean(np.abs(z_scores) <= 1.0))
    within_2_sigma = float(np.mean(np.abs(z_scores) <= 2.0))

    # Not demanding Gaussian perfection (~68%/~95%) - but must not be
    # brutally miscalibrated in either direction.
    assert 0.35 < within_1_sigma < 0.95, f"+-1 sigma coverage: {within_1_sigma:.2f}"
    assert 0.65 < within_2_sigma < 0.999, f"+-2 sigma coverage: {within_2_sigma:.2f}"
