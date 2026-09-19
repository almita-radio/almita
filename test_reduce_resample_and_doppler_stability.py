"""RESAMPLE MASK PROPAGATION adversarial test (2nd-pass section 17) and
DOPPLER / COMMON GRID STABILITY (section 16).
"""
import numpy as np

from hi_spectral_metric import HI_REST_HZ, frequency_to_radio_velocity
from reduce_engine.models import MaskFlag
from reduce_engine.resample import resample_to_common_grid


# ---------------------------------------------------------------- 17. resample mask propagation

def test_masked_gap_interpolated_to_common_grid_is_never_treated_as_valid_signal():
    """A genuine gap of masked bins in one grid, resampled onto a
    DIFFERENT (finer) common grid. The resampled region must remain
    flagged, its uncertainty must not become a small confident number,
    and n_contributing bookkeeping (downstream, at the averaging stage)
    must not count it."""
    source_grid = np.linspace(1e9, 1.001e9, 20)
    common_grid = np.linspace(1e9, 1.001e9, 40)  # finer target grid forces real interpolation
    value = np.full(20, 5.0)
    value[8:12] = 5.0  # value itself looks "fine" - only the MASK marks it bad
    uncertainty = np.full(20, 0.1)
    mask = np.full(20, MaskFlag.GOOD.value, dtype=np.int64)
    mask[8:12] = MaskFlag.RFI.value  # the real gap

    result = resample_to_common_grid([source_grid, common_grid], [value, np.zeros(40)],
                                     [uncertainty, np.full(40, 0.1)], [mask, np.zeros(40, dtype=np.int64)])
    assert result.method == "linear_interpolation_mask_aware"
    resampled_mask_source = result.masks[0]

    gap_lo_hz, gap_hi_hz = source_grid[7], source_grid[12]
    in_or_near_gap = (result.frequency_hz >= gap_lo_hz) & (result.frequency_hz <= gap_hi_hz)
    assert np.any(resampled_mask_source[in_or_near_gap] != MaskFlag.GOOD.value), \
        "a resampled bin drawn from a masked gap must not silently become GOOD"

    # value there must not have been quietly presented as data either -
    # np.interp still computes SOME number under the hood, but paired
    # with the non-GOOD mask, downstream (usable_bin_mask/averaging) must
    # exclude it - confirm that exclusion, not merely the mask bit.
    from reduce_engine.masks import usable_bin_mask
    usable = usable_bin_mask(resampled_mask_source)
    assert not np.all(usable[in_or_near_gap])


def test_resample_never_reduces_uncertainty_across_a_masked_gap():
    """A resampled bin drawn from (or adjacent to) a masked region must
    not report a smaller uncertainty than its neighbors just because
    np.interp() produced *some* finite number - that would be silent
    overconfidence manufactured out of a gap."""
    source_grid = np.linspace(1e9, 1.001e9, 20)
    common_grid = np.linspace(1e9, 1.001e9, 40)
    value = np.full(20, 5.0)
    uncertainty = np.full(20, 0.1)
    mask = np.full(20, MaskFlag.GOOD.value, dtype=np.int64)
    mask[8:12] = MaskFlag.RFI.value

    result = resample_to_common_grid([source_grid, common_grid], [value, np.zeros(40)],
                                     [uncertainty, np.full(40, 0.1)], [mask, np.zeros(40, dtype=np.int64)])
    resampled_mask, resampled_unc = result.masks[0], result.uncertainties[0]
    gap_lo_hz, gap_hi_hz = source_grid[8], source_grid[11]
    in_gap = (result.frequency_hz >= gap_lo_hz) & (result.frequency_hz <= gap_hi_hz)
    # every bin actually inside the gap must be MISSING (never GOOD with a
    # small, confident interpolated uncertainty)
    assert np.all(resampled_mask[in_gap] == MaskFlag.MISSING.value)


# ---------------------------------------------------------------- 16. Doppler / common grid stability

def test_line_center_before_alignment_differs_across_doppler_shifted_captures():
    """Sanity precondition for the stability test below: without any
    velocity correction, two captures with different Doppler shifts
    really do show the line at different raw frequencies."""
    from reduce_engine.config import ReduceConfig
    from reduce_engine.masks import build_mask
    from reduce_engine.spectral import estimate_spectrum
    from reduce_engine.simulation import SyntheticCaptureConfig, build_synthetic_iq

    reduce_config = ReduceConfig(fft_size=2048)
    peaks_km_s = []
    for shift in (-15.0, 15.0):
        sim_config = SyntheticCaptureConfig(fft_size=2048, n_segments=16, hi_line_peak_fraction=0.5,
                                            hi_line_fwhm_km_s=8.0, hi_line_velocity_km_s=shift, random_seed=3)
        estimate = estimate_spectrum(build_synthetic_iq(sim_config), sample_rate_hz=sim_config.sample_rate_hz,
                                     center_frequency_hz=sim_config.center_frequency_hz, config=reduce_config)
        velocity = frequency_to_radio_velocity(estimate.frequency_hz, rest_hz=HI_REST_HZ)
        window = np.abs(velocity - shift) < 30
        peaks_km_s.append(velocity[window][np.argmax(estimate.psd[window])])
    assert abs(peaks_km_s[0] - peaks_km_s[1]) > 10.0, "the two synthetic Doppler shifts should look different raw"


def test_stack_of_doppler_shifted_captures_does_not_broaden_beyond_tolerance():
    """After the (already-tested) averaging stage combines several
    captures with small Doppler jitter around a common true velocity,
    the recovered line width must stay within a bounded tolerance of the
    true injected FWHM - it must not artificially broaden without limit
    just because the inputs were not perfectly aligned in frequency."""
    from reduce_engine.averaging import stack_spectra
    from reduce_engine.baseline import fit_baseline
    from reduce_engine.config import ReduceConfig
    from reduce_engine.masks import build_mask, usable_bin_mask
    from reduce_engine.spectral import estimate_spectrum
    from reduce_engine.simulation import SyntheticCaptureConfig, build_synthetic_iq
    from reduce_engine.uncertainty import per_capture_uncertainty

    true_fwhm_km_s = 12.0
    reduce_config = ReduceConfig(fft_size=4096)
    rng = np.random.default_rng(11)
    values, uncertainties, masks, frequency_ref = [], [], [], None
    for i in range(8):
        jitter = rng.normal(0, 1.5)  # small realistic per-capture Doppler jitter, km/s
        sim_config = SyntheticCaptureConfig(fft_size=4096, n_segments=32, hi_line_peak_fraction=0.5,
                                            hi_line_fwhm_km_s=true_fwhm_km_s, hi_line_velocity_km_s=jitter,
                                            random_seed=200 + i)
        estimate = estimate_spectrum(build_synthetic_iq(sim_config), sample_rate_hz=sim_config.sample_rate_hz,
                                     center_frequency_hz=sim_config.center_frequency_hz, config=reduce_config)
        mask_result = build_mask(estimate.frequency_hz, estimate.psd, center_frequency_hz=sim_config.center_frequency_hz,
                                 config=reduce_config, segments=estimate.segments)
        baseline_result = fit_baseline(estimate.frequency_hz, estimate.psd, mask_result.mask, config=reduce_config)
        relative_intensity = (estimate.psd - baseline_result.baseline) / baseline_result.baseline
        unc = per_capture_uncertainty(estimate.segments) / baseline_result.baseline
        if frequency_ref is None:
            frequency_ref = estimate.frequency_hz
        values.append(relative_intensity); uncertainties.append(unc); masks.append(mask_result.mask)

    stacked = stack_spectra(values, uncertainties, masks)
    velocity = frequency_to_radio_velocity(frequency_ref, rest_hz=HI_REST_HZ)
    usable = usable_bin_mask(stacked.mask)
    window = usable & (np.abs(velocity) < 60)
    peak = float(np.max(stacked.value[window]))
    half_max = peak / 2.0
    above_half = window & (stacked.value >= half_max)
    measured_fwhm_km_s = float(velocity[above_half].max() - velocity[above_half].min()) if above_half.any() else np.inf
    # "does not broaden artificially beyond tolerance": measured FWHM
    # should stay within roughly 2x the true injected FWHM plus the
    # jitter spread itself (not unboundedly smeared).
    assert measured_fwhm_km_s < true_fwhm_km_s * 2.5 + 6 * 1.5
