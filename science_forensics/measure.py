"""Per-point feature measurement on the LEAST TRANSFORMED valid product: the Level 1 MasterSpectrum of each pointing
(gridding would mix neighbouring pointings).

The operator gives a search window (frame + centre + half-width). Inside it the feature is measured three ways so the
methods can be compared: parabolic peak, half-maximum weighted centroid, and a Gaussian + constant local offset fitted by
Levenberg-Marquardt (pure numpy, no scipy). The constant offset belongs to the WINDOW only: the spectrum is never
re-baselined. The feature is signed (emission-like or absorption-like) and masked bins are missing, never zero.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np

from reduce_engine.models import MaskFlag
from science_forensics.models import ForensicsConfig, ForensicsPoint

MASK_FLAGS_REPORTED = ("RFI", "KNOWN_SPUR", "DC", "EDGE", "INVALID", "SATURATED", "MISSING", "USER_EXCLUDED")
FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))


@dataclass
class FeatureMeasurement:
    point_index: int
    status: str                                   # OK | BELOW_DETECTION_THRESHOLD | NO_USABLE_BINS | INSUFFICIENT_BINS | WINDOW_OUTSIDE_SPECTRUM | NO_FEATURE_SIGNAL
    flags: list = field(default_factory=list)
    n_window_bins: int = 0
    n_usable_bins: int = 0
    mask_fraction: float = float("nan")
    mask_flag_fractions: dict = field(default_factory=dict)
    nonfinite_fraction: float = float("nan")
    sign: int = 0
    offset: float = float("nan")                  # local constant offset of the WINDOW (documented; not a re-baseline)
    peak_height: float = float("nan")             # signed height above the local offset
    peak_raw_value: float = float("nan")
    method_used: str = ""
    centroid_channel: float = float("nan")
    centroid_channel_sigma: float = float("nan")
    centroid_freq_hz: float = float("nan")
    centroid_freq_sigma_hz: float = float("nan")
    centroid_lsrk_m_s: float = float("nan")
    centroid_lsrk_sigma_m_s: float = float("nan")
    centroid_channel_gaussian: float = float("nan")
    centroid_channel_halfmax: float = float("nan")
    centroid_channel_peak: float = float("nan")
    fwhm_channels: float = float("nan")
    fwhm_hz: float = float("nan")
    fwhm_m_s: float = float("nan")
    area_relative_hz: float = float("nan")        # signed, relative_intensity * Hz
    area_relative_m_s: float = float("nan")       # signed, relative_intensity * m/s
    sigma_formal: float = float("nan")            # median Level 1 sigma over the usable window bins
    snr_formal: float = float("nan")
    fit_redchi2: float = float("nan")
    fit_converged: Optional[bool] = None
    fit_n_iter: int = 0
    window_first_channel: int = -1
    window_last_channel: int = -1

    def to_dict(self) -> dict:
        return asdict(self)


def window_channels(point: ForensicsPoint, config: ForensicsConfig) -> np.ndarray:
    """Channel indices (ascending) inside the operator's window, in the window's own frame and this point's own axes."""
    n = point.value.shape[0]
    c, h = config.window_center, config.window_half_width
    if config.window_frame == "lsrk_velocity":
        inside = np.abs(point.velocity_lsrk_m_s - c) <= h
    elif config.window_frame == "frequency":
        inside = np.abs(point.frequency_hz - c) <= h
    else:
        inside = np.abs(np.arange(n) - c) <= h
    return np.flatnonzero(inside)


def _usable(point: ForensicsPoint, idx: np.ndarray) -> np.ndarray:
    y, s, m = point.value[idx], point.sigma[idx], point.mask[idx]
    return (m == MaskFlag.GOOD.value) & np.isfinite(y) & np.isfinite(s) & (s > 0)


def mask_composition(mask: np.ndarray) -> dict:
    out = {}
    for name in MASK_FLAGS_REPORTED:
        bit = MaskFlag[name].value
        out[name] = float(np.mean((mask & bit) != 0)) if mask.size else float("nan")
    return out


def _local_offset(y: np.ndarray, usable: np.ndarray) -> float:
    n = y.shape[0]
    q = max(1, n // 4)
    edge = np.zeros(n, dtype=bool)
    edge[:q] = True
    edge[-q:] = True
    pool = y[edge & usable]
    if pool.size < 2:
        pool = y[usable]
    return float(np.median(pool))


def _smooth3(z: np.ndarray, usable: np.ndarray) -> np.ndarray:
    """3-bin running mean over usable neighbours only (masked bins are not zero: they are skipped)."""
    out = np.full(z.shape, np.nan)
    for i in range(z.shape[0]):
        if not usable[i]:
            continue
        lo, hi = max(0, i - 1), min(z.shape[0], i + 2)
        sel = usable[lo:hi]
        out[i] = np.mean(z[lo:hi][sel])
    return out


def _parabolic_peak(channels: np.ndarray, z: np.ndarray, usable: np.ndarray, i: int) -> float:
    if 0 < i < z.shape[0] - 1 and usable[i - 1] and usable[i] and usable[i + 1]:
        d = z[i - 1] - 2 * z[i] + z[i + 1]
        if d != 0:
            return float(channels[i] + np.clip(0.5 * (z[i - 1] - z[i + 1]) / d, -1.0, 1.0))
    return float(channels[i])


def _halfmax(channels, z, sig, usable, i_peak):
    """Contiguous half-maximum region around the peak: (centroid, sigma_centroid, fwhm) - NaN where not defined."""
    half = 0.5 * z[i_peak]
    lo = hi = i_peak
    while lo - 1 >= 0 and usable[lo - 1] and z[lo - 1] >= half:
        lo -= 1
    while hi + 1 < z.shape[0] and usable[hi + 1] and z[hi + 1] >= half:
        hi += 1
    w = z[lo:hi + 1]
    x = channels[lo:hi + 1]
    total = float(np.sum(w))
    if total <= 0:
        return float("nan"), float("nan"), float("nan")
    cen = float(np.sum(x * w) / total)
    sigma_cen = float(np.sqrt(np.sum(sig[lo:hi + 1] ** 2 * (x - cen) ** 2)) / total)
    left = right = float("nan")
    if lo - 1 >= 0 and usable[lo - 1] and z[lo - 1] < half <= z[lo]:
        left = channels[lo - 1] + (half - z[lo - 1]) * (channels[lo] - channels[lo - 1]) / (z[lo] - z[lo - 1])
    if hi + 1 < z.shape[0] and usable[hi + 1] and z[hi + 1] < half <= z[hi]:
        right = channels[hi] + (half - z[hi]) * (channels[hi + 1] - channels[hi]) / (z[hi + 1] - z[hi])
    return cen, sigma_cen, float(right - left)


def gaussian_fit(x, y, sig, a0, mu0, s0, o0, max_iter=80):
    """Levenberg-Marquardt fit of A*exp(-(x-mu)^2/(2 s^2)) + offset with weights 1/sig^2.
    Returns (params, covariance, chi2, n_iter, converged); params = [A, mu, s, offset]."""
    p = np.array([a0, mu0, max(s0, 0.6), o0], dtype=float)
    w = 1.0 / sig ** 2

    def model(q):
        return q[0] * np.exp(-0.5 * ((x - q[1]) / q[2]) ** 2) + q[3]

    def chi2(q):
        r = y - model(q)
        return float(np.sum(w * r * r))

    cur = chi2(p)
    lam = 1e-3
    converged = False
    it = 0
    cov = np.full((4, 4), np.nan)
    for it in range(1, max_iter + 1):
        g = np.exp(-0.5 * ((x - p[1]) / p[2]) ** 2)
        J = np.stack([g, p[0] * g * (x - p[1]) / p[2] ** 2, p[0] * g * (x - p[1]) ** 2 / p[2] ** 3, np.ones_like(x)], axis=1)
        r = y - model(p)
        H = J.T @ (J * w[:, None])
        b = J.T @ (w * r)
        improved = False
        for _ in range(12):
            try:
                step = np.linalg.solve(H + lam * np.diag(np.diag(H)), b)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            trial = p + step
            trial[2] = abs(trial[2])
            if not np.all(np.isfinite(trial)) or trial[2] < 0.3:
                lam *= 10
                continue
            new = chi2(trial)
            if new <= cur:
                converged = abs(cur - new) <= 1e-10 * max(cur, 1.0) and np.max(np.abs(step / (np.abs(p) + 1e-9))) < 1e-7
                p, cur, lam, improved = trial, new, max(lam / 10, 1e-12), True
                break
            lam *= 10
        if not improved:
            converged = True          # cannot improve further from here
            break
        if converged:
            break
    try:
        g = np.exp(-0.5 * ((x - p[1]) / p[2]) ** 2)
        J = np.stack([g, p[0] * g * (x - p[1]) / p[2] ** 2, p[0] * g * (x - p[1]) ** 2 / p[2] ** 3, np.ones_like(x)], axis=1)
        cov = np.linalg.inv(J.T @ (J * w[:, None]))
    except np.linalg.LinAlgError:
        pass
    return p, cov, cur, it, bool(converged)


def measure_point(point: ForensicsPoint, config: ForensicsConfig) -> FeatureMeasurement:
    n_total = point.value.shape[0]
    idx = window_channels(point, config)
    m = FeatureMeasurement(point_index=point.point_index, status="OK")
    m.n_window_bins = int(idx.size)
    if idx.size == 0:
        m.status = "WINDOW_OUTSIDE_SPECTRUM"
        return m
    m.window_first_channel, m.window_last_channel = int(idx[0]), int(idx[-1])
    if config.window_frame == "lsrk_velocity":
        axis = point.velocity_lsrk_m_s
        expected_edge = (config.window_center - config.window_half_width, config.window_center + config.window_half_width)
        if axis.min() > expected_edge[0] or axis.max() < expected_edge[1]:
            m.flags.append("WINDOW_TRUNCATED")
    elif idx[0] == 0 or idx[-1] == n_total - 1:
        m.flags.append("WINDOW_TRUNCATED")
    y_all, s_all = point.value[idx], point.sigma[idx]
    m.mask_fraction = float(np.mean(point.mask[idx] != MaskFlag.GOOD.value))
    m.mask_flag_fractions = mask_composition(point.mask[idx])
    m.nonfinite_fraction = float(np.mean(~(np.isfinite(y_all) & np.isfinite(s_all))))
    usable = _usable(point, idx)
    m.n_usable_bins = int(usable.sum())
    if m.n_usable_bins == 0:
        m.status = "NO_USABLE_BINS"
        return m
    if m.n_usable_bins < config.min_usable_bins:
        m.status = "INSUFFICIENT_BINS"
        return m
    if m.mask_fraction > 0.3:
        m.flags.append("MASK_OVERLAP")

    channels = idx.astype(float)
    y = np.where(usable, y_all, np.nan)
    s = np.where(usable, s_all, np.nan)
    m.offset = _local_offset(y_all, usable)
    z_raw = y - m.offset
    smooth = _smooth3(z_raw, usable)
    if config.sign == "positive":
        sign = 1
    elif config.sign == "negative":
        sign = -1
    else:
        sign = 1 if smooth[np.nanargmax(np.abs(smooth))] >= 0 else -1
    m.sign = sign
    zs = sign * z_raw                                  # feature-positive signal
    i_peak = int(np.nanargmax(np.where(usable, sign * smooth, -np.inf)))
    if not usable[i_peak] or not np.isfinite(zs[i_peak]) or sign * smooth[i_peak] <= 0:
        m.status = "NO_FEATURE_SIGNAL"
        return m
    m.sigma_formal = float(np.median(s[usable]))
    if i_peak in (0, idx.size - 1):
        m.flags.append("EDGE_OF_WINDOW")
    m.peak_raw_value = float(y_all[i_peak])

    m.centroid_channel_peak = _parabolic_peak(channels, np.where(usable, zs, np.nan), usable, i_peak)
    zs_filled = np.where(usable, zs, 0.0)
    cen_h, sig_h, fwhm_h = _halfmax(channels, zs_filled, np.where(usable, s_all, 0.0), usable, i_peak)
    m.centroid_channel_halfmax = cen_h
    height = float(zs[i_peak])
    fit = None
    if usable.sum() >= 5:
        p, cov, chi, n_iter, ok = gaussian_fit(channels[usable], y[usable], s[usable], sign * max(height, 1e-12),
                                               channels[i_peak], (fwhm_h / FWHM_PER_SIGMA) if np.isfinite(fwhm_h) else 2.0,
                                               m.offset, config.fit_max_iter)
        dof = int(usable.sum()) - 4
        good = (ok and np.all(np.isfinite(p)) and p[0] * sign > 0 and 0.4 <= p[2] <= 2.0 * idx.size
                and channels[0] - 0.5 <= p[1] <= channels[-1] + 0.5 and np.isfinite(cov[1, 1]) and cov[1, 1] > 0)
        m.fit_converged, m.fit_n_iter = bool(ok), int(n_iter)
        m.fit_redchi2 = float(chi / dof) if dof > 0 else float("nan")
        if good:
            fit = (p, cov)
            m.centroid_channel_gaussian = float(p[1])
        else:
            m.flags.append("GAUSSIAN_FIT_REJECTED")

    order = {"gaussian": ("gaussian", "halfmax", "peak"), "halfmax": ("halfmax", "peak"), "peak": ("peak",)}[config.centroid_method]
    for method in order:
        if method == "gaussian" and fit is not None:
            p, cov = fit
            m.method_used, m.centroid_channel, m.centroid_channel_sigma = "gaussian", float(p[1]), float(np.sqrt(cov[1, 1]))
            m.fwhm_channels = float(FWHM_PER_SIGMA * p[2])
            m.peak_height = float(p[0])
            m.offset = float(p[3])
            m.area_relative_hz = m.area_relative_m_s = float("nan")     # set below with local channel widths
            gauss_area_channels = float(p[0] * p[2] * np.sqrt(2 * np.pi))
            break
        if method == "halfmax" and np.isfinite(cen_h):
            m.method_used, m.centroid_channel, m.centroid_channel_sigma = "halfmax", cen_h, sig_h
            m.fwhm_channels, m.peak_height = fwhm_h, sign * height
            gauss_area_channels = float(np.sum(zs_filled[usable]) * sign)
            if not np.isfinite(fwhm_h):
                m.flags.append("NO_HALF_MAX_CROSSING")
            break
        if method == "peak":
            m.method_used, m.centroid_channel, m.centroid_channel_sigma = "peak", m.centroid_channel_peak, float("nan")
            m.peak_height = sign * height
            gauss_area_channels = float(np.sum(zs_filled[usable]) * sign)
            break
    if np.isfinite(m.fwhm_channels) and m.fwhm_channels > 0.5 * idx.size:
        m.flags.append("BROAD_VS_WINDOW")          # the local offset is then degenerate with the feature: widen the window
    if m.method_used and m.method_used != config.centroid_method:
        m.flags.append(f"FALLBACK_TO_{m.method_used.upper()}")
    if not m.method_used:
        m.status = "NO_FEATURE_SIGNAL"
        return m

    nv = np.arange(n_total, dtype=float)
    m.centroid_freq_hz = float(np.interp(m.centroid_channel, nv, point.frequency_hz))
    m.centroid_lsrk_m_s = float(np.interp(m.centroid_channel, nv, point.velocity_lsrk_m_s))
    i0 = int(np.clip(round(m.centroid_channel), 0, n_total - 2))
    df = abs(point.frequency_hz[i0 + 1] - point.frequency_hz[i0])
    dv = abs(point.velocity_lsrk_m_s[i0 + 1] - point.velocity_lsrk_m_s[i0])
    if np.isfinite(m.centroid_channel_sigma):
        m.centroid_freq_sigma_hz = float(m.centroid_channel_sigma * df)
        m.centroid_lsrk_sigma_m_s = float(m.centroid_channel_sigma * dv)
    if np.isfinite(m.fwhm_channels):
        m.fwhm_hz, m.fwhm_m_s = float(m.fwhm_channels * df), float(m.fwhm_channels * dv)
    m.area_relative_hz = float(gauss_area_channels * df)
    m.area_relative_m_s = float(gauss_area_channels * dv)
    m.snr_formal = float(abs(m.peak_height) / m.sigma_formal) if m.sigma_formal > 0 else float("nan")
    if not (np.isfinite(m.snr_formal) and m.snr_formal >= config.min_snr_formal):
        # numbers stay recorded (transparency) but this is NOT a detection and never enters campaign statistics
        m.status = "BELOW_DETECTION_THRESHOLD"
        m.flags.append(f"SNR_FORMAL_BELOW_{config.min_snr_formal:g}")
    return m


def scan_peaks(points, vmin_m_s: float, vmax_m_s: float, smooth_bins: bool = True) -> list:
    """Diagnostic pre-step used to CHOOSE a window from the data (not a blind line finder): per point, the strongest
    (signed) smoothed excursion of the usable spectrum inside an LSRK velocity range, in all three coordinates."""
    rows = []
    for p in points:
        idx = np.flatnonzero((p.velocity_lsrk_m_s >= vmin_m_s) & (p.velocity_lsrk_m_s <= vmax_m_s))
        if idx.size < 5:
            rows.append({"point_index": p.point_index, "status": "RANGE_OUTSIDE_SPECTRUM"})
            continue
        usable = _usable(p, idx)
        if usable.sum() < 5:
            rows.append({"point_index": p.point_index, "status": "INSUFFICIENT_USABLE_BINS"})
            continue
        z = p.value[idx] - float(np.median(p.value[idx][usable]))
        sm = _smooth3(z, usable) if smooth_bins else np.where(usable, z, np.nan)
        k = int(np.nanargmax(np.abs(sm)))
        ch = int(idx[k])
        rows.append({"point_index": p.point_index, "status": "OK", "channel": ch, "velocity_lsrk_m_s": float(p.velocity_lsrk_m_s[ch]),
                     "frequency_hz": float(p.frequency_hz[ch]), "smoothed_excursion": float(sm[k]),
                     "sigma_at_peak": float(p.sigma[ch])})
    return rows
