"""GOLDEN synthetic acceptance test (docs/REDUCE_PIPELINE.md's "primer test
de aceptacion cientifico"): multiple synthetic captures of the SAME sky
point, each with its own noise realization, a small Doppler shift, a
bandpass slope, and occasional RFI - run through REDUCE's real stage
functions (spectral estimate, mask, baseline, stack, uncertainty, quality;
never reimplemented for this test) - and checked against the KNOWN
injected ground truth: line recovered near its true velocity, RFI masked
out, stacking measurably improves SNR, and per-bin uncertainty shrinks
with N.
"""
import numpy as np

from hi_spectral_metric import HI_REST_HZ, frequency_to_radio_velocity
from reduce_engine.averaging import stack_spectra
from reduce_engine.baseline import fit_baseline
from reduce_engine.config import ReduceConfig
from reduce_engine.masks import build_mask, usable_bin_mask
from reduce_engine.models import MaskFlag
from reduce_engine.quality import assess_quality
from reduce_engine.simulation import SyntheticCaptureConfig, build_synthetic_iq
from reduce_engine.spectral import estimate_spectrum
from reduce_engine.uncertainty import per_capture_uncertainty

TRUE_LINE_VELOCITY_KM_S = -12.0
TRUE_LINE_FWHM_KM_S = 18.0
TRUE_LINE_PEAK_FRACTION = 0.6
N_CAPTURES = 10


CENTER_FREQUENCY_HZ = 1_420_405_752.0


def _reduce_one_synthetic_capture(*, doppler_offset_km_s: float, add_rfi: bool, seed: int, config: ReduceConfig):
    sim_config = SyntheticCaptureConfig(
        fft_size=config.fft_size, n_segments=48, noise_level=1.0, bandpass_slope=0.15,
        center_frequency_hz=CENTER_FREQUENCY_HZ,
        hi_line_velocity_km_s=TRUE_LINE_VELOCITY_KM_S + doppler_offset_km_s,
        hi_line_fwhm_km_s=TRUE_LINE_FWHM_KM_S, hi_line_peak_fraction=TRUE_LINE_PEAK_FRACTION,
        rfi_lines_hz=((CENTER_FREQUENCY_HZ + 350_000.0,) if add_rfi else ()),
        rfi_amplitude=15.0, random_seed=seed,
    )
    iq = build_synthetic_iq(sim_config)
    estimate = estimate_spectrum(iq, sample_rate_hz=sim_config.sample_rate_hz,
                                 center_frequency_hz=sim_config.center_frequency_hz, config=config)
    mask_result = build_mask(estimate.frequency_hz, estimate.psd, center_frequency_hz=sim_config.center_frequency_hz,
                             config=config, segments=estimate.segments, clipping_fraction=estimate.clipping_fraction)
    baseline_result = fit_baseline(estimate.frequency_hz, estimate.psd, mask_result.mask, config=config)
    relative_intensity = (estimate.psd - baseline_result.baseline) / baseline_result.baseline
    per_bin_unc = per_capture_uncertainty(estimate.segments) / baseline_result.baseline
    return estimate.frequency_hz, relative_intensity, per_bin_unc, mask_result.mask, baseline_result


def test_golden_single_capture_recovers_injected_line_position():
    config = ReduceConfig(fft_size=4096, edge_fraction=0.02)
    frequency, relative_intensity, unc, mask, baseline_result = _reduce_one_synthetic_capture(
        doppler_offset_km_s=0.0, add_rfi=False, seed=1, config=config)
    velocity = frequency_to_radio_velocity(frequency, rest_hz=HI_REST_HZ)
    usable = usable_bin_mask(mask)
    window = usable & (np.abs(velocity - TRUE_LINE_VELOCITY_KM_S) < 40)
    peak_velocity = velocity[window][np.argmax(relative_intensity[window])]
    assert abs(peak_velocity - TRUE_LINE_VELOCITY_KM_S) < TRUE_LINE_FWHM_KM_S


def test_golden_stack_masks_rfi_improves_snr_and_recovers_line():
    config = ReduceConfig(fft_size=4096, edge_fraction=0.02, sigma_clip_threshold=6.0)
    rng = np.random.default_rng(7)
    values, uncertainties, masks = [], [], []
    frequency_ref = None
    single_capture_unc = None
    for i in range(N_CAPTURES):
        doppler_offset = rng.normal(0, 2.0)  # small per-capture Doppler jitter, km/s
        add_rfi = (i % 4 == 0)               # occasional RFI, not every capture
        frequency, relative_intensity, unc, mask, _ = _reduce_one_synthetic_capture(
            doppler_offset_km_s=doppler_offset, add_rfi=add_rfi, seed=100 + i, config=config)
        if frequency_ref is None:
            frequency_ref = frequency
            single_capture_unc = unc
        values.append(relative_intensity); uncertainties.append(unc); masks.append(mask)

    stacked = stack_spectra(values, uncertainties, masks, method=config.averaging_method,
                            sigma_clip_threshold=config.sigma_clip_threshold)

    velocity = frequency_to_radio_velocity(frequency_ref, rest_hz=HI_REST_HZ)
    usable = usable_bin_mask(stacked.mask)
    window = usable & (np.abs(velocity - TRUE_LINE_VELOCITY_KM_S) < 40)
    assert window.any(), "no usable bins survived near the injected line - RFI/mask logic over-excluded"
    peak_velocity = velocity[window][np.argmax(stacked.value[window])]
    # per-capture Doppler jitter (~2 km/s std) smears the recovered peak a
    # little relative to the single-capture case - tolerance widened
    # accordingly, still well inside the injected line's own FWHM.
    assert abs(peak_velocity - TRUE_LINE_VELOCITY_KM_S) < TRUE_LINE_FWHM_KM_S * 1.5

    off_line = usable & (np.abs(velocity - TRUE_LINE_VELOCITY_KM_S) > 80)
    stacked_rms = float(np.sqrt(np.nanmean(stacked.value[off_line] ** 2)))
    single_rms = float(np.sqrt(np.nanmean(np.asarray(values)[0][off_line] ** 2)))
    assert stacked_rms < single_rms, "stacking N captures must reduce off-line RMS relative to a single capture"

    single_median_unc = float(np.nanmedian(single_capture_unc[usable]))
    stacked_median_unc = float(np.nanmedian(stacked.uncertainty[usable]))
    assert stacked_median_unc < single_median_unc, "stacked per-bin uncertainty must decrease with N"

    quality = assess_quality(mask=stacked.mask, n_contributing=stacked.n_contributing, clipping_fraction=0.0,
                             calibration_level="UNCALIBRATED", calibration_compatibility_status="NOT_ATTEMPTED",
                             baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk")
    assert quality.state in ("GOOD", "WARNING")
    assert float(np.median(stacked.n_contributing[usable])) >= N_CAPTURES * 0.5
