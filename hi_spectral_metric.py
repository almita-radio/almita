"""Offline HI spectral metric primitives.

This module is intentionally not wired into Alignment V2 yet.  Velocity uses the
radio convention v = c * (f_rest - f) / f_rest.  Frames are explicit metadata:
frequency samples are topocentric unless a caller supplies a corrected grid.
"""

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

HI_REST_HZ = 1_420_405_751.77
LIGHT_KM_S = 299_792.458


@dataclass(frozen=True)
class HIMetricV2Result:
    metric_value: float
    metric_uncertainty: float
    metric_snr: float
    valid_fraction: float
    masked_fraction: float
    dc_masked_fraction: float
    spur_masked_fraction: float
    baseline_rms: float
    baseline_method: str
    velocity_window_km_s: float
    velocity_frame: str = "TOPOCENTRIC"


def frequency_to_radio_velocity(frequency_hz, rest_hz=HI_REST_HZ):
    return LIGHT_KM_S * (rest_hz - np.asarray(frequency_hz, float)) / rest_hz


def radio_velocity_to_frequency(velocity_km_s, rest_hz=HI_REST_HZ):
    return rest_hz * (1.0 - np.asarray(velocity_km_s, float) / LIGHT_KM_S)


def frequency_range_mask(frequency_hz, ranges_hz: Sequence[Sequence[float]] = ()):
    f = np.asarray(frequency_hz, float)
    mask = np.zeros(f.shape, dtype=bool)
    for lo, hi in ranges_hz:
        mask |= (f >= min(lo, hi)) & (f <= max(lo, hi))
    return mask


def dc_mask(frequency_hz, center_frequency_hz, half_width_hz):
    return np.abs(np.asarray(frequency_hz) - center_frequency_hz) <= half_width_hz


def measure_dc_mask_half_width(frequency_hz, median_psd, center_frequency_hz,
                               threshold_db=0.5, reference_inner_hz=20_000.0,
                               reference_outer_hz=100_000.0, guard_bins=2,
                               minimum_half_width_bins=5):
    """Measure a conservative zero-IF mask from an ensemble median spectrum.

    Starting at the center bin, contiguous bins exceeding the adjacent reference
    level are measured in both directions. Hann-leakage guard bins and a small
    minimum mask make the result stable when only the center bin is prominent.
    """
    f, p = np.asarray(frequency_hz, float), np.asarray(median_psd, float)
    if f.shape != p.shape or f.ndim != 1 or f.size < 16:
        raise ValueError("frequency and median PSD must be matching vectors")
    distance = np.abs(f - center_frequency_hz)
    reference = (distance >= reference_inner_hz) & (distance <= reference_outer_hz)
    if not reference.any():
        raise ValueError("DC reference annulus does not fit sampled band")
    level = float(np.median(p[reference]))
    excess_db = 10 * np.log10(np.maximum(p, 1e-30) / max(level, 1e-30))
    center_bin = int(np.argmin(distance)); affected = {center_bin}
    for direction in (-1, 1):
        index = center_bin
        while 0 <= index < len(f) and excess_db[index] > threshold_db:
            affected.add(index); index += direction
    bin_hz = float(np.median(np.abs(np.diff(f))))
    measured = max(abs(f[index] - center_frequency_hz) for index in affected)
    half_width = max(measured + guard_bins * bin_hz,
                     minimum_half_width_bins * bin_hz)
    return {
        "half_width_hz": float(half_width),
        "half_width_bins": float(half_width / bin_hz),
        "center_bin": center_bin,
        "affected_bins_above_threshold": len(affected),
        "threshold_db": float(threshold_db),
        "peak_excess_db": float(excess_db[center_bin]),
        "frequency_bin_hz": bin_hz,
    }


def robust_psd_from_iq(iq_bytes, sample_rate_hz, center_frequency_hz,
                       fft_size=8192, combine="median", return_segments=False):
    raw = np.asarray(iq_bytes, dtype=np.uint8)
    if raw.ndim != 1 or raw.size % 2:
        raise ValueError("IQ must be an even-length interleaved uint8 vector")
    iq = ((raw[0::2].astype(np.float32) - 127.5) +
          1j * (raw[1::2].astype(np.float32) - 127.5))
    count = iq.size // fft_size
    if count < 4:
        raise ValueError("at least four complete FFT segments are required")
    window = np.hanning(fft_size)
    blocks = iq[:count * fft_size].reshape(count, fft_size) * window
    segments = np.abs(np.fft.fftshift(np.fft.fft(blocks, axis=1), axes=1)) ** 2
    if combine == "median":
        psd = np.median(segments, axis=0)
    elif combine == "mean":
        psd = np.mean(segments, axis=0)
    else:
        raise ValueError("combine must be 'median' or 'mean'")
    frequency = center_frequency_hz + np.fft.fftshift(
        np.fft.fftfreq(fft_size, 1.0 / sample_rate_hz))
    return (frequency, psd, segments) if return_segments else (frequency, psd)


def _robust_scale(values):
    values = np.asarray(values, float)
    med = np.median(values)
    return max(float(1.4826 * np.median(np.abs(values - med))), 1e-20)


def fit_polynomial_baseline(frequency_hz, psd, fit_mask, degree=2,
                            iterations=5, sigma=4.0):
    """Iteratively clipped low-order baseline in linear PSD units."""
    f, y = np.asarray(frequency_hz, float), np.asarray(psd, float)
    good = np.asarray(fit_mask, bool) & np.isfinite(y) & (y > 0)
    if good.sum() < max(32, 8 * (degree + 1)):
        raise ValueError("insufficient unmasked bins for baseline")
    origin, scale = float(np.mean(f[good])), float(np.ptp(f[good]) / 2)
    x = (f - origin) / max(scale, 1.0)
    active = good.copy()
    for _ in range(iterations):
        coef = np.polyfit(x[active], y[active], degree)
        model = np.polyval(coef, x)
        residual = y[good] - model[good]
        center = np.median(residual)
        spread = _robust_scale(residual)
        active = good & (np.abs(y - model - center) <= sigma * spread)
    baseline = np.polyval(coef, x)
    floor = max(float(np.percentile(y[good], 1)) * 0.05, 1e-20)
    return np.maximum(baseline, floor), active


def fit_reference_bandpass(frequency_hz, psd, reference_bandpass, fit_mask,
                           degree=1, iterations=5, sigma=4.0):
    """Scale a measured reference bandpass by a robust low-order ratio model.

    This is a differential baseline: it preserves position-to-position spectral
    changes but not emission common to every member used to form the reference.
    """
    f=np.asarray(frequency_hz,float);p=np.asarray(psd,float);ref=np.asarray(reference_bandpass,float)
    if ref.shape!=p.shape or np.any(ref<=0):
        raise ValueError("reference_bandpass must be positive and match PSD")
    ratio=p/ref
    model_ratio,active=fit_polynomial_baseline(f,ratio,fit_mask,degree,iterations,sigma)
    return ref*model_ratio,active


def detect_fixed_spurs(frequency_hz, spectra, excluded_mask=None,
                       smooth_bins=101, z_threshold=8.0,
                       persistence_threshold=0.70, pad_bins=2,
                       max_cluster_bins=25):
    """Detect narrow positive features persistent across independent captures."""
    s = np.asarray(spectra, float)
    if s.ndim != 2:
        raise ValueError("spectra must be capture x frequency")
    kernel = np.ones(int(smooth_bins), float) / int(smooth_bins)
    med = np.median(s, axis=0)
    smooth = np.convolve(med, kernel, mode="same")
    frac = med / np.maximum(smooth, 1e-20) - 1
    core = np.ones(frac.shape, bool) if excluded_mask is None else ~np.asarray(excluded_mask, bool)
    edge = smooth_bins // 2
    core[:edge] = False; core[-edge:] = False
    scale = _robust_scale(frac[core])
    candidate = core & (frac > z_threshold * scale)
    per = s / np.maximum(np.median(s, axis=1)[:, None], 1e-20)
    local = per / np.maximum(np.apply_along_axis(
        lambda row: np.convolve(row, kernel, mode="same"), 1, per), 1e-20) - 1
    persistence = np.mean(local > 4.0 * scale, axis=0)
    candidate &= persistence >= persistence_threshold
    indices = np.flatnonzero(candidate)
    clusters = []
    if indices.size:
        starts = np.r_[0, np.flatnonzero(np.diff(indices) > 1) + 1]
        ends = np.r_[starts[1:], len(indices)]
        for a, b in zip(starts, ends):
            group = indices[a:b]
            if len(group) <= max_cluster_bins:
                lo, hi = max(0, group[0]-pad_bins), min(len(med)-1, group[-1]+pad_bins)
                clusters.append({"lo_bin":int(lo), "hi_bin":int(hi),
                                 "frequency_hz":float(frequency_hz[group[np.argmax(frac[group])]]),
                                 "score":float(np.max(frac[group])/scale),
                                 "persistence":float(np.max(persistence[group]))})
    mask = np.zeros(med.shape, bool)
    for item in clusters: mask[item["lo_bin"]:item["hi_bin"]+1] = True
    return mask, clusters


def metric_v2_from_psd(frequency_hz, psd, center_frequency_hz,
                       velocity_window_km_s=150.0, dc_mask_half_width_hz=5_000.0,
                       spur_ranges_hz=(), baseline_degree=2,
                       rest_hz=HI_REST_HZ, uncertainty=None,
                       reference_bandpass=None):
    """Signed fractional line integral in km/s with explicit masks.

    metric = sum(valid line bins) ((PSD-baseline)/baseline) * |dv|.
    No positive clipping is performed.  Baseline fits exclude the entire line
    window, DC, fixed spurs, and the outermost 2 percent of the sampled band.
    """
    f, p = np.asarray(frequency_hz, float), np.asarray(psd, float)
    if f.shape != p.shape or f.ndim != 1 or f.size < 64:
        raise ValueError("frequency and PSD must be matching one-dimensional arrays")
    velocity = frequency_to_radio_velocity(f, rest_hz)
    line = np.abs(velocity) <= velocity_window_km_s
    dc = dc_mask(f, center_frequency_hz, dc_mask_half_width_hz)
    spur = frequency_range_mask(f, spur_ranges_hz)
    edge = np.zeros(f.shape, bool); nedge=max(2,int(.02*f.size)); edge[:nedge]=True;edge[-nedge:]=True
    fit = ~(line | dc | spur | edge)
    if reference_bandpass is None:
        baseline, used = fit_polynomial_baseline(f, p, fit, degree=baseline_degree)
        baseline_name=f"robust_poly{baseline_degree}"
    else:
        baseline, used = fit_reference_bandpass(
            f,p,reference_bandpass,fit,degree=min(baseline_degree,1))
        baseline_name="ensemble_reference_scaled_linear"
    residual_fraction = (p - baseline) / baseline
    valid_line = line & ~(dc | spur)
    if valid_line.sum() < 8:
        raise ValueError("too few valid bins remain in HI window")
    dv = float(np.median(np.abs(np.diff(velocity))))
    value = float(np.sum(residual_fraction[valid_line]) * dv)
    side = used & ~line
    baseline_rms = _robust_scale(residual_fraction[side])
    propagated = float(baseline_rms * dv * np.sqrt(1.5 * valid_line.sum()))
    unc = propagated if uncertainty is None else max(float(uncertainty), propagated)
    total_line = int(line.sum())
    dc_fraction = float(np.sum(line & dc) / total_line)
    spur_fraction = float(np.sum(line & spur & ~dc) / total_line)
    masked = dc_fraction + spur_fraction
    return HIMetricV2Result(
        metric_value=value, metric_uncertainty=unc,
        metric_snr=value / unc if unc > 0 else float("nan"),
        valid_fraction=float(valid_line.sum()/total_line), masked_fraction=masked,
        dc_masked_fraction=dc_fraction, spur_masked_fraction=spur_fraction,
        baseline_rms=float(baseline_rms), baseline_method=baseline_name,
        velocity_window_km_s=float(velocity_window_km_s)), baseline, residual_fraction


def compute_hi_metric_v2(iq_bytes, sample_rate_hz, center_frequency_hz,
                         velocity_window_km_s=150.0,
                         dc_mask_half_width_hz=5_000.0, spur_ranges_hz=(),
                         fft_size=8192, uncertainty_groups=8,
                         reference_bandpass=None):
    frequency, psd, segments = robust_psd_from_iq(
        iq_bytes, sample_rate_hz, center_frequency_hz, fft_size, return_segments=True)
    group_values=[]
    for group in np.array_split(segments, uncertainty_groups):
        if len(group):
            r, _, _ = metric_v2_from_psd(
                frequency, np.median(group,axis=0), center_frequency_hz,
                velocity_window_km_s, dc_mask_half_width_hz, spur_ranges_hz,
                reference_bandpass=reference_bandpass)
            group_values.append(r.metric_value)
    empirical = (float(np.std(group_values,ddof=1)/np.sqrt(len(group_values)))
                 if len(group_values)>1 else None)
    result, baseline, residual = metric_v2_from_psd(
        frequency, psd, center_frequency_hz, velocity_window_km_s,
        dc_mask_half_width_hz, spur_ranges_hz, uncertainty=empirical,
        reference_bandpass=reference_bandpass)
    return result, {"frequency_hz":frequency, "psd":psd, "baseline":baseline,
                    "residual_fraction":residual,
                    "subintegration_metric_values":group_values}


def inject_gaussian_psd(psd, baseline, frequency_hz, center_velocity_km_s,
                        fwhm_km_s, peak_fraction):
    """Add a non-negative Gaussian line in linear PSD units.

    peak_fraction is peak line power divided by local baseline power.  Injection
    is made after detection in PSD space, preserving measured ripple/noise/DC.
    """
    velocity = frequency_to_radio_velocity(frequency_hz)
    sigma = fwhm_km_s / 2.354820045
    profile = np.exp(-0.5*((velocity-center_velocity_km_s)/sigma)**2)
    return np.asarray(psd,float) + np.asarray(baseline,float)*peak_fraction*profile


def expected_gaussian_integral(peak_fraction, fwhm_km_s):
    sigma = fwhm_km_s / 2.354820045
    return float(peak_fraction * sigma * np.sqrt(2*np.pi))


def expected_hi_spectrum_interface(ra, dec, timestamp, beam_fwhm,
                                   velocity_grid, *, survey):
    """Future interface contract; no observational survey is bundled.

    ``survey`` must declare Tb units, velocity convention/frame, spectral and
    angular resolution, provenance, coverage, and beam/convolution metadata.
    """
    raise NotImplementedError("observational HI survey integration is future work")
