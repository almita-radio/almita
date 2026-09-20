"""Experiment ANALYSIS on Level 1 point spectra (read-only; REDUCE is never modified).

Everything here is a DIAGNOSTIC. The decisive quantities are frame-explicit: a stationary sky line moves in topocentric frequency
by  (f0/c) * d(LSRK shift)  between positions and (very slowly) with time; a receiver-fixed line does not move at all; a
temporal instrumental drift moves everything by the same amount regardless of where the antenna points.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from reduce_engine.models import MaskFlag
from science_forensics.models import ForensicsConfig
from science_forensics.measure import measure_point, _smooth3
from science_forensics.statistics import ols

C_LIGHT_M_S = 299_792_458.0
DECISIONS = ("SKY_FIXED", "SKY_GRADIENT", "RECEIVER_FIXED", "TIME_GLOBAL", "TIME_CANDIDATE_ONLY", "MIXED_TIME_AND_SPATIAL",
             "STATIONARY_UNDISCRIMINATED", "UNRESOLVED")


def _k(f0_hz: float) -> float:
    return f0_hz / C_LIGHT_M_S


# ------------------------------------------------------------------ position identity

def assign_position_labels(points: list, plan_positions: Optional[dict] = None, tol_deg: float = 0.3) -> list:
    """Label each capture by the sky position it belongs to. With a plan, nearest plan position within tol; otherwise the
    positions are clustered in order of first appearance (A, B, C, ...). The capture keeps its own point_index: a repeated
    coordinate is several DISTINCT captures, never merged."""
    centres: list = list((plan_positions or {}).items())
    labels = []
    for p in points:
        best, bd = None, 1e9
        for lab, (ra, dec) in centres:
            dra = ((p.ra_deg - ra + 180.0) % 360.0 - 180.0) * np.cos(np.radians(dec))
            d = float(np.hypot(dra, p.dec_deg - dec))
            if d < bd:
                best, bd = lab, d
        if best is None or bd > tol_deg:
            if plan_positions is not None:
                labels.append("?")
                continue
            best = chr(ord("A") + len(centres))
            centres.append((best, (p.ra_deg, p.dec_deg)))
        labels.append(best)
    return labels


# ------------------------------------------------------------------ tracking (candidate and control lines)

def _highpass(y: np.ndarray, usable: np.ndarray, width: int = 101) -> np.ndarray:
    """Spectrum minus a running median; unusable bins -> 0 (they contribute nothing to a correlation)."""
    z = np.where(usable, y, np.nan)
    n = z.size
    h = width // 2
    out = np.zeros(n)
    idx = np.arange(0, n, max(1, h // 4))
    med = np.array([np.nanmedian(z[max(0, i - h): i + h + 1]) if np.isfinite(z[max(0, i - h): i + h + 1]).any() else 0.0 for i in idx])
    base = np.interp(np.arange(n), idx, med)
    out = np.where(usable, y - base, 0.0)
    return out


def _usable_all(p) -> np.ndarray:
    return (p.mask == MaskFlag.GOOD.value) & np.isfinite(p.value) & np.isfinite(p.sigma) & (p.sigma > 0)


def track_line(points: list, *, start_channel: float, search_half_width: int = 40, fit_half_width: int = 30, sign: int = 0,
               min_snr_formal: float = 5.0, predictor: str = "previous", method: str = "gaussian") -> list:
    """Measure one spectral line in every capture with a window that FOLLOWS it (a drift of ~9 channels per capture cadence
    would leave a fixed window). The search window is centred on the prediction (previous detection), the fit window on the
    strongest signed excursion found there. Returns FeatureMeasurement per capture (measure_point unchanged)."""
    out, pred = [], float(start_channel)
    for p in points:
        n = p.value.shape[0]
        lo, hi = int(max(0, np.floor(pred - search_half_width))), int(min(n - 1, np.ceil(pred + search_half_width)))
        idx = np.arange(lo, hi + 1)
        usable = _usable_all(p)[idx]
        if usable.sum() < 6:
            cfg = ForensicsConfig(window_center=float(pred), window_half_width=float(fit_half_width), window_frame="channel",
                                  feature_id="TRACK", sign="auto", min_snr_formal=min_snr_formal, centroid_method=method)
            out.append(measure_point(p, cfg))
            continue
        z = p.value[idx] - np.median(p.value[idx][usable])
        sm = _smooth3(z, usable)
        sgn = sign if sign else (1 if sm[np.nanargmax(np.abs(sm))] >= 0 else -1)
        k = int(np.nanargmax(np.where(usable, sgn * sm, -np.inf)))
        centre = float(idx[k])
        cfg = ForensicsConfig(window_center=centre, window_half_width=float(fit_half_width), window_frame="channel",
                              feature_id="TRACK", sign="positive" if sgn > 0 else "negative", min_snr_formal=min_snr_formal,
                              centroid_method=method)
        m = measure_point(p, cfg)
        out.append(m)
        if m.status == "OK" and np.isfinite(m.centroid_channel):
            pred = float(m.centroid_channel)
    return out


def centroid_hz(points: list, ms: list) -> tuple:
    """(indices, frequency Hz, sigma Hz) of the captures with a valid detection."""
    ii = [i for i, m in enumerate(ms) if m.status == "OK" and np.isfinite(m.centroid_freq_hz) and np.isfinite(m.centroid_freq_sigma_hz)]
    return (np.array(ii, dtype=int), np.array([ms[i].centroid_freq_hz for i in ii]), np.array([ms[i].centroid_freq_sigma_hz for i in ii]))


# ------------------------------------------------------------------ robust, non-parametric morphology

def robust_morphology(p, centre_channel: float, half_width: int = 40) -> dict:
    """Shape of a line without a Gaussian assumption: half-maximum crossings, flux-weighted centroid, skewness and width.
    Independent of the Gaussian fit; a disagreement between them is itself a diagnostic (non-Gaussian line)."""
    n = p.value.shape[0]
    lo, hi = int(max(0, centre_channel - half_width)), int(min(n, centre_channel + half_width + 1))
    idx = np.arange(lo, hi)
    usable = _usable_all(p)[idx]
    if usable.sum() < 8:
        return {"status": "INSUFFICIENT_BINS"}
    y = p.value[idx]
    q = max(2, idx.size // 5)
    edge = np.concatenate([y[:q][usable[:q]], y[-q:][usable[-q:]]])
    base = float(np.median(edge)) if edge.size else float(np.median(y[usable]))
    z = np.where(usable, y - base, 0.0)
    sgn = 1 if z[np.argmax(np.abs(z))] >= 0 else -1
    zs = sgn * _smooth3(z, usable)
    zs = np.where(np.isfinite(zs), zs, 0.0)
    i = int(np.argmax(zs))
    peak = zs[i]
    if peak <= 0:
        return {"status": "NO_SIGNAL"}
    half = 0.5 * peak
    a = i
    while a > 0 and zs[a] > half:
        a -= 1
    b = i
    while b < zs.size - 1 and zs[b] > half:
        b += 1
    xa = a + (half - zs[a]) / max(zs[a + 1] - zs[a], 1e-12) if a < i else float(a)
    xb = b - (half - zs[b]) / max(zs[b - 1] - zs[b], 1e-12) if b > i else float(b)
    w = np.clip(sgn * z[a:b + 1], 0, None)
    x = idx[a:b + 1].astype(float)
    cen = float(np.sum(w * x) / np.sum(w)) if w.sum() > 0 else float("nan")
    var = float(np.sum(w * (x - cen) ** 2) / np.sum(w)) if w.sum() > 0 else float("nan")
    skew = float(np.sum(w * (x - cen) ** 3) / np.sum(w) / var ** 1.5) if var > 0 else float("nan")
    return {"status": "OK", "sign": sgn, "peak_height": float(sgn * peak), "halfmax_centre_channel": float(idx[0] + 0.5 * (xa + xb)),
            "fwhm_channels": float(xb - xa), "moment_centroid_channel": cen, "moment_width_channels": float(np.sqrt(var)),
            "skewness": skew, "area_above_half": float(np.sum(w))}


# ------------------------------------------------------------------ global spectral shift (cross-correlation)

def _xcorr_lags(a: np.ndarray, b: np.ndarray, max_lag: int) -> np.ndarray:
    """c[l] = sum_i a[i] * b[i + l]  for l in [-max_lag, max_lag] (zero padded)."""
    n = a.size
    m = 1 << int(np.ceil(np.log2(2 * n)))
    fa, fb = np.fft.rfft(a, m), np.fft.rfft(b, m)
    full = np.fft.irfft(np.conj(fa) * fb, m)
    return np.concatenate([full[-max_lag:], full[:max_lag + 1]]) if max_lag > 0 else full[:1]


def _peak_lag(c: np.ndarray, max_lag: int) -> tuple:
    k = int(np.argmax(c))
    lag = float(k - max_lag)
    if 0 < k < c.size - 1:
        y0, y1, y2 = c[k - 1], c[k], c[k + 1]
        den = y0 - 2 * y1 + y2
        if den < 0:
            lag += 0.5 * (y0 - y2) / den
    rest = np.delete(c, slice(max(0, k - 3), k + 4))
    rms = float(np.std(rest)) if rest.size > 3 else float("nan")
    return lag, float((c[k] - np.mean(rest)) / rms) if rms and np.isfinite(rms) and rms > 0 else float("nan")


def global_shift(pa, pb, *, exclude_channels: Optional[np.ndarray] = None, max_lag: int = 80, n_segments: int = 6) -> dict:
    """Channel shift of the WHOLE spectrum of capture b relative to capture a (positive = b's structure at higher channel).
    The candidate window (and any known spur) is excluded so the estimate is independent of the feature being tested.
    Uncertainty: scatter of the per-segment estimates (segments without correlation signal are dropped)."""
    ua, ub = _usable_all(pa), _usable_all(pb)
    a, b = _highpass(pa.value, ua), _highpass(pb.value, ub)
    if exclude_channels is not None and len(exclude_channels):
        a[exclude_channels] = 0.0
        b[exclude_channels] = 0.0
    c_total = _xcorr_lags(a, b, max_lag)
    lag, snr = _peak_lag(c_total, max_lag)
    seg = np.array_split(np.arange(a.size), n_segments)
    lags = []
    for s in seg:
        aa, bb = np.zeros_like(a), np.zeros_like(b)
        aa[s], bb[s] = a[s], b[s]
        # segment of a against the FULL b: features move in and out of segment boundaries by at most max_lag
        cs = _xcorr_lags(aa, b, max_lag)
        lg, sn = _peak_lag(cs, max_lag)
        if np.isfinite(sn) and sn >= 4.0:
            lags.append(lg)
    if len(lags) >= 2:
        se = float(1.4826 * np.median(np.abs(np.array(lags) - np.median(lags))) / np.sqrt(len(lags)) + 0.02)
    else:
        se = float("nan")
    return {"shift_channels": lag, "peak_snr": snr, "n_segments_used": len(lags), "shift_se_channels": se,
            "reliable": bool(np.isfinite(snr) and snr >= 6.0 and len(lags) >= 2)}


def spur_and_masked_channels(points: list, fraction: float = 0.8) -> np.ndarray:
    """Channels masked in >= `fraction` of the captures: persistent receiver artefacts (DC spike, fixed spurs) as REDUCE flags them."""
    m = np.mean([(p.mask != MaskFlag.GOOD.value) | ~np.isfinite(p.value) for p in points], axis=0)
    return np.flatnonzero(m >= fraction)


def _window(centre: float, half: int, n: int) -> np.ndarray:
    return np.arange(int(max(0, np.floor(centre - half))), int(min(n, np.ceil(centre + half) + 1)))


def global_shift_series(points: list, labels: list, *, reference_label: str = "A", exclude_channels: Optional[np.ndarray] = None,
                        candidate_channels: Optional[np.ndarray] = None, candidate_half_width: int = 45, max_lag: int = 80,
                        only_reference_position: bool = True) -> dict:
    """Cumulative whole-spectrum topocentric shift over time, chained between consecutive captures of the SAME sky position
    (so a sky-fixed structure contributes no apparent shift between them). `candidate_channels` (one per capture, NaN when unknown)
    excludes the candidate line itself, at ITS OWN position in each capture, so the estimate never uses the feature under test."""
    idx = [i for i, lab in enumerate(labels) if (lab == reference_label) or not only_reference_position]
    times, cum, ses, rel = [], [], [], []
    total, var = 0.0, 0.0
    n = points[0].value.shape[0]
    for j, i in enumerate(idx):
        if j == 0:
            times.append(points[i].t_mid_s); cum.append(0.0); ses.append(0.0); rel.append(True)
            continue
        ex = [np.asarray(exclude_channels, dtype=int)] if exclude_channels is not None and len(exclude_channels) else []
        if candidate_channels is not None:
            for q in (idx[j - 1], i):
                if np.isfinite(candidate_channels[q]):
                    ex.append(_window(candidate_channels[q], candidate_half_width, n))
        g = global_shift(points[idx[j - 1]], points[i], exclude_channels=np.unique(np.concatenate(ex)) if ex else None, max_lag=max_lag)
        if g["reliable"]:
            total += g["shift_channels"]
            var += g["shift_se_channels"] ** 2
        times.append(points[i].t_mid_s); cum.append(total); ses.append(float(np.sqrt(var))); rel.append(g["reliable"])
    n_rel = int(np.sum(rel[1:])) if len(rel) > 1 else 0
    out = {"indices": idx, "t_s": times, "cumulative_shift_channels": cum, "se_channels": ses, "n_reliable_steps": n_rel,
           "n_steps": max(0, len(idx) - 1)}
    if n_rel >= 3 and len(idx) >= 4:
        t_h = (np.array(times) - times[0]) / 3600.0
        fit = ols(np.column_stack([np.ones_like(t_h), t_h]), np.array(cum))
        out.update({"slope_channels_per_hour": fit["coefficients"][1], "slope_se_channels_per_hour": fit["standard_errors"][1],
                    "total_over_span_channels": fit["coefficients"][1] * (t_h[-1] - t_h[0])})
    return out


# ------------------------------------------------------------------ matched pairs, bracketing, position offsets

def matched_pairs(times_s: np.ndarray, labels: list, values_hz: np.ndarray, sigma_hz: np.ndarray, indices: np.ndarray, *,
                  tol_s: float) -> dict:
    """(a) same-position pairs within tol; (b) every non-reference capture against the reference series LINEARLY INTERPOLATED to its
    time from the reference captures immediately before and after it (both within tol): a temporal drift cancels to first order,
    a position effect does not. tol_s should be ~1.5x the median real cadence."""
    lab = [labels[i] for i in indices]
    t = np.array([times_s[i] for i in indices])
    same = []
    for x in range(len(indices)):
        for y in range(x + 1, len(indices)):
            if lab[x] == lab[y] and 0 < t[y] - t[x] <= 2 * tol_s:
                same.append({"a": int(indices[x]), "b": int(indices[y]), "label": lab[x], "dt_s": float(t[y] - t[x]),
                             "delta_hz": float(values_hz[y] - values_hz[x]),
                             "sigma_hz": float(np.hypot(sigma_hz[x], sigma_hz[y]))})
    cross = []
    for x in range(len(indices)):
        if lab[x] == "A":
            continue
        before = [y for y in range(len(indices)) if lab[y] == "A" and 0 < t[x] - t[y] <= tol_s]
        after = [y for y in range(len(indices)) if lab[y] == "A" and 0 < t[y] - t[x] <= tol_s]
        if not before or not after:
            continue
        yb, ya = max(before, key=lambda y: t[y]), min(after, key=lambda y: t[y])
        w = (t[x] - t[yb]) / (t[ya] - t[yb])
        interp = (1 - w) * values_hz[yb] + w * values_hz[ya]
        sig = float(np.sqrt(sigma_hz[x] ** 2 + ((1 - w) * sigma_hz[yb]) ** 2 + (w * sigma_hz[ya]) ** 2))
        cross.append({"capture": int(indices[x]), "label": lab[x], "delta_hz": float(values_hz[x] - interp), "sigma_hz": sig,
                      "bracket": [int(indices[yb]), int(indices[ya])], "gap_s": float(t[ya] - t[yb])})
    offsets = {}
    for lb in sorted({c["label"] for c in cross}):
        d = np.array([c["delta_hz"] for c in cross if c["label"] == lb])
        s = np.array([c["sigma_hz"] for c in cross if c["label"] == lb])
        wgt = 1.0 / s ** 2
        mean = float(np.sum(wgt * d) / np.sum(wgt))
        se_formal = float(np.sqrt(1.0 / np.sum(wgt)))
        scatter = float(np.std(d, ddof=1) / np.sqrt(d.size)) if d.size > 2 else float("nan")
        offsets[lb] = {"n_pairs": int(d.size), "mean_delta_hz": mean, "se_formal_hz": se_formal, "se_scatter_hz": scatter,
                       "se_used_hz": float(max(se_formal, scatter)) if np.isfinite(scatter) else se_formal}
    return {"same_position_pairs": same, "cross_position_pairs": cross, "position_offsets": offsets, "pair_tolerance_s": tol_s}


def shift_and_difference(pa, pb, shift_hz: float, window: tuple) -> dict:
    """Difference spectrum b - a within `window` channels (lo, hi), plain and after shifting a by the PREDICTED frequency offset.
    Diagnostic only: if the prediction is right the shifted difference of a line collapses to noise."""
    lo, hi = window
    ch = np.arange(pa.value.size)
    df = float(np.median(np.diff(pa.frequency_hz)))
    ua, ub = _usable_all(pa), _usable_all(pb)
    ya = np.where(ua, pa.value, np.nan)
    yb = np.where(ub, pb.value, np.nan)
    shifted = np.interp(ch - shift_hz / df, ch, np.nan_to_num(ya, nan=0.0), left=0.0, right=0.0)
    ok_shift = np.interp(ch - shift_hz / df, ch, ua.astype(float), left=0.0, right=0.0) > 0.999
    sel = slice(lo, hi + 1)
    plain = (yb - ya)[sel]
    sd = np.where(ok_shift, yb - shifted, np.nan)[sel]
    rms = lambda v: float(np.sqrt(np.nanmean(v ** 2))) if np.isfinite(v).any() else float("nan")
    return {"plain_difference_rms": rms(plain), "shift_and_difference_rms": rms(sd), "shift_hz": float(shift_hz),
            "shift_channels": float(shift_hz / df), "diagnostic_only": True}


# ------------------------------------------------------------------ decision

def decide(*, n_time_points: int, drift_hz_per_hour: float, drift_se: float, span_h: float, sky_null_drift_hz_per_hour: float,
           stationary_chi2_per_dof: Optional[float] = None, range_channels: Optional[float] = None,
           position_offsets: dict, predicted_offsets: dict, global_tracks_candidate: Optional[bool] = None, global_flat: Optional[bool] = None,
           channel_width_hz: float = 292.96875, min_effect_channels: float = 2.0, z_crit: float = 5.0) -> dict:
    """Explicit null-hypothesis tests with EFFECT SIZE and UNCERTAINTY (no p-value hunting):
       H0_time      : the A series is stationary apart from the sky-null drift (|excess drift| < z_crit*SE  OR  total excess < min_effect)
       H0_sky       : every position offset equals the sky-stationary prediction (chi2 per dof < 4 with >= 1 dof)
       H0_frequency : every position offset is zero (receiver/frequency fixed)
       """
    excess = drift_hz_per_hour - sky_null_drift_hz_per_hour
    excess_z = abs(excess) / drift_se if drift_se and np.isfinite(drift_se) and drift_se > 0 else float("nan")
    excess_total_ch = abs(excess) * span_h / channel_width_hz
    linear_rejected = bool(np.isfinite(excess_z) and excess_z >= z_crit and excess_total_ch >= min_effect_channels)
    # non-linear temporal dependence (thermal-like wander): the stationary (constant + sky-null) model fails, with a real effect size
    nonlinear_rejected = bool(stationary_chi2_per_dof is not None and np.isfinite(stationary_chi2_per_dof) and stationary_chi2_per_dof >= 4.0
                              and range_channels is not None and range_channels >= min_effect_channels)
    time_rejected = linear_rejected or nonlinear_rejected
    labs = sorted(set(position_offsets) & set(predicted_offsets))
    chi_sky = chi_zero = 0.0
    for lb in labs:
        o, s, pr = position_offsets[lb]["mean_delta_hz"], position_offsets[lb]["se_used_hz"], predicted_offsets[lb]
        s = max(s, 1e-9)
        chi_sky += ((o - pr) / s) ** 2
        chi_zero += (o / s) ** 2
    dof = len(labs)
    sky_ok = bool(dof and chi_sky / dof < 4.0)
    zero_ok = bool(dof and chi_zero / dof < 4.0)
    # do the offsets say anything at all? largest |predicted| offset must be resolvable (>> its uncertainty)
    resolvable = bool(dof and all(abs(predicted_offsets[lb]) > 10 * max(position_offsets[lb]["se_used_hz"], 1e-9) for lb in labs))
    g_tracks, g_flat = global_tracks_candidate, global_flat
    comps = {"H0_time_rejected": time_rejected, "H0_time_rejected_by_linear_slope": linear_rejected, "H0_time_rejected_by_nonlinear_wander": nonlinear_rejected,
             "stationary_chi2_per_dof": stationary_chi2_per_dof, "series_range_channels": range_channels, "excess_drift_hz_per_hour": excess, "excess_drift_z": excess_z,
             "excess_total_over_span_channels": excess_total_ch, "H0_sky_consistent": sky_ok, "chi2_sky_per_dof": chi_sky / dof if dof else None,
             "H0_frequency_consistent": zero_ok, "chi2_zero_per_dof": chi_zero / dof if dof else None,
             "sky_vs_frequency_resolvable": resolvable, "global_shift_tracks_candidate": g_tracks, "global_shift_flat": g_flat,
             "n_position_offsets": dof}
    if n_time_points < 6:
        return {"decision": "UNRESOLVED", "reason": "too few detections", **comps}
    if time_rejected:
        if not dof:
            label = "TIME_GLOBAL" if g_tracks else ("TIME_CANDIDATE_ONLY" if g_flat else "UNRESOLVED")
            return {"decision": label, "reason": "drift with time; no cross-position data", **comps}
        if not (sky_ok or zero_ok):
            return {"decision": "MIXED_TIME_AND_SPATIAL", "reason": "drift with time AND position offsets that match neither sky nor zero", **comps}
        label = "TIME_GLOBAL" if g_tracks else ("TIME_CANDIDATE_ONLY" if g_flat else "UNRESOLVED")
        return {"decision": label, "reason": "drift with time; position offsets consistent with a stationary pattern", **comps}
    if not dof:
        return {"decision": "STATIONARY_UNDISCRIMINATED", "reason": "stationary in time at one position; sky vs receiver-fixed needs other positions", **comps}
    if not resolvable:
        return {"decision": "UNRESOLVED", "reason": "predicted sky offsets not resolvable at this precision", **comps}
    if sky_ok and not zero_ok:
        return {"decision": "SKY_FIXED", "reason": "offsets match the parameter-free sky-stationary prediction", **comps}
    if zero_ok and not sky_ok:
        return {"decision": "RECEIVER_FIXED", "reason": "offsets are zero within uncertainty", **comps}
    if not sky_ok and not zero_ok:
        return {"decision": "SKY_GRADIENT", "reason": "position dependent, but not the stationary-sky pattern", **comps}
    return {"decision": "UNRESOLVED", "reason": "offsets consistent with both", **comps}


def global_vs_candidate(gs: dict, cand_channels: np.ndarray, sky_null_ch_per_h: float, *, min_range_channels: float = 2.0) -> dict:
    """Does the whole-spectrum shift series explain the candidate's motion? Regress the candidate centroid (channels, reference
    captures) on the cumulative global shift at the same captures: slope ~1 with high R2 = the candidate moves with everything
    else (global / instrumental); slope ~0 with a moving candidate = candidate-specific. Also 'flat': the global series stays
    within its own uncertainty after removing the tiny sky-null drift."""
    idx = gs["indices"]
    g = np.array(gs["cumulative_shift_channels"], dtype=float)
    se = np.array(gs["se_channels"], dtype=float)
    t_h = (np.array(gs["t_s"]) - gs["t_s"][0]) / 3600.0
    c = np.array([cand_channels[i] for i in idx], dtype=float)
    ok = np.isfinite(c)
    out = {"n_common": int(ok.sum()), "n_reliable_steps": gs["n_reliable_steps"], "n_steps": gs["n_steps"]}
    if ok.sum() < 5 or gs["n_reliable_steps"] < max(3, 0.6 * gs["n_steps"]):
        out.update({"tracks_candidate": None, "flat": None, "reason": "global shift not reliably measurable (too few reliable steps)"})
        return out
    g_resid = g - sky_null_ch_per_h * t_h
    rng_g = float(np.ptp(g_resid[ok]))
    flat = bool(rng_g <= max(3.0 * float(np.max(se)), 1.5))
    c0 = c[ok] - c[ok][0]
    fit = ols(np.column_stack([np.ones(ok.sum()), g_resid[ok]]), c0)
    beta, beta_se = fit["coefficients"][1], fit["standard_errors"][1]
    tracks = bool((not flat) and rng_g >= min_range_channels and abs(beta - 1.0) <= max(3.0 * beta_se, 0.25) and fit["r2"] >= 0.8)
    out.update({"global_range_channels": rng_g, "candidate_vs_global_slope": beta, "candidate_vs_global_slope_se": beta_se,
                "r2": fit["r2"], "tracks_candidate": tracks, "flat": flat, "reason": "ok"})
    return out


def _weighted_slope(t_h: np.ndarray, y: np.ndarray, s: np.ndarray, extra: Optional[np.ndarray] = None) -> tuple:
    """Weighted LS  y = a + b t   (per-point sigmas, rescaled by sqrt(chi2/dof) when chi2/dof > 1)."""
    w = 1.0 / np.maximum(s, 1e-9) ** 2
    X = np.column_stack([np.ones_like(t_h), t_h])
    W = np.diag(w)
    cov = np.linalg.inv(X.T @ W @ X)
    beta = cov @ X.T @ W @ y
    r = y - X @ beta
    dof = max(1, len(y) - 2)
    chi = float(np.sum(w * r ** 2) / dof)
    scale = max(1.0, chi)
    return float(beta[1]), float(np.sqrt(cov[1, 1] * scale)), chi


def analyze_experiment(points: list, labels: list, *, start_channel: float, expected: dict, f0_hz: float, tol_s: float,
                       channel_width_hz: float, fit_half_width: int = 30, search_half_width: int = 60, sign: int = 0,
                       exclude_channels: Optional[np.ndarray] = None) -> dict:
    """Full experiment analysis for a list of captures in time order. `expected` is design.expected_signatures output
    (pairwise B-A, C-A offsets and the sky-null drift of A)."""
    ms = track_line(points, start_channel=start_channel, search_half_width=search_half_width, fit_half_width=fit_half_width, sign=sign)
    ii, f, sf = centroid_hz(points, ms)
    times = np.array([p.t_mid_s for p in points])
    detected = len(ii)
    res: dict = {"n_captures": len(points), "n_detected": int(detected), "labels": labels}
    if detected < 6:
        res["decision"] = decide(n_time_points=detected, drift_hz_per_hour=0, drift_se=float("nan"), span_h=0, sky_null_drift_hz_per_hour=0,
                                 position_offsets={}, predicted_offsets={}, channel_width_hz=channel_width_hz)
        return res
    a_mask = np.array([labels[i] == "A" for i in ii])
    t_h = (times[ii] - times[ii].min()) / 3600.0
    # temporal drift from the reference position only (same sky): the cleanest time series
    if a_mask.sum() >= 4:
        slope, slope_se, chi = _weighted_slope(t_h[a_mask], f[a_mask], sf[a_mask])
    else:
        slope, slope_se, chi = _weighted_slope(t_h, f, sf)
    span_h = float(t_h.max() - t_h.min())
    pairs = matched_pairs(times, labels, f, sf, ii, tol_s=tol_s)
    pred_off = {k.split("-")[0]: v["sky_stationary_predicts_delta_topocentric_frequency_hz"] for k, v in expected["pairwise"].items() if k.endswith("-A")}
    sky_null = expected["fixed_sky_expected_drift"]["A"]["sky_stationary_predicts_topocentric_drift_hz_per_hour"]
    cand_ch = np.full(len(points), np.nan)
    for i in ii:
        cand_ch[i] = ms[i].centroid_channel
    gs = global_shift_series(points, labels, exclude_channels=exclude_channels, candidate_channels=cand_ch)
    # stationarity of the reference series against  constant + sky-null drift
    tt, ff, ss = (t_h[a_mask], f[a_mask], sf[a_mask]) if a_mask.sum() >= 4 else (t_h, f, sf)
    resid = ff - sky_null * tt
    wgt = 1.0 / np.maximum(ss, 1e-9) ** 2
    chi_stat = float(np.sum(wgt * (resid - np.sum(wgt * resid) / np.sum(wgt)) ** 2) / max(1, len(tt) - 1))
    range_ch = float(np.ptp(resid) / channel_width_hz)
    gvc = global_vs_candidate(gs, cand_ch, sky_null / channel_width_hz)
    dec = decide(n_time_points=detected, stationary_chi2_per_dof=chi_stat, range_channels=range_ch, drift_hz_per_hour=slope, drift_se=slope_se, span_h=span_h, sky_null_drift_hz_per_hour=sky_null,
                 position_offsets=pairs["position_offsets"], predicted_offsets=pred_off, global_tracks_candidate=gvc["tracks_candidate"], global_flat=gvc["flat"], channel_width_hz=channel_width_hz)
    res.update({"reference_series": {"drift_hz_per_hour": slope, "drift_se_hz_per_hour": slope_se, "chi2_per_dof": chi,
                                     "sky_null_drift_hz_per_hour": sky_null, "span_hours": span_h, "n_points": int(a_mask.sum()),
                                     "stationary_chi2_per_dof": chi_stat, "range_channels": range_ch},
                "matched_pairs": pairs, "global_shift": {k: v for k, v in gs.items() if k not in ("indices",)}, "global_vs_candidate": gvc,
                "predicted_position_offsets_hz": pred_off, "decision": dec,
                "measurements": [m.to_dict() for m in ms]})
    return res


def regression_classifier(points: list, ms: list, f0_hz: float, channel_width_hz: float, *, z_crit: float = 5.0,
                          min_effect_channels: float = 2.0) -> dict:
    """The physically parameterised regression usable on ANY design (including the old raster):
         f_i = a + b * t_i + gamma * (f0/c) * (LSRK shift_i - mean)
    gamma = 1 -> stationary sky line, gamma = 0 -> receiver/frequency fixed, b = excess drift with time. On the old raster time and
    shift are nearly collinear, so b and gamma cannot both be resolved: the design, not the estimator, is the limit."""
    ii, f, sf = centroid_hz(points, ms)
    if len(ii) < 6:
        return {"decision": "UNRESOLVED", "reason": "too few detections"}
    t = np.array([points[i].t_mid_s for i in ii])
    t_h = (t - t.min()) / 3600.0
    sh = np.array([points[i].lsrk_shift_m_s for i in ii])
    x = _k(f0_hz) * (sh - sh.mean())
    X = np.column_stack([np.ones_like(t_h), t_h, x])
    w = 1.0 / np.maximum(sf, 1e-9) ** 2
    A = X.T @ (w[:, None] * X)
    cov = np.linalg.inv(A)
    beta = cov @ X.T @ (w * f)
    r = f - X @ beta
    chi = float(np.sum(w * r ** 2) / max(1, len(f) - 3))
    se = np.sqrt(np.diag(cov) * max(1.0, chi))
    b, g = float(beta[1]), float(beta[2])
    span_h = float(t_h.max() - t_h.min())
    time_sig = bool(abs(b) / se[1] >= z_crit and abs(b) * span_h / channel_width_hz >= min_effect_channels)
    g_resolved = bool(se[2] < 0.33)
    sky_ok, zero_ok = bool(abs(g - 1.0) < 3 * se[2]), bool(abs(g) < 3 * se[2])
    out = {"time_coefficient_hz_per_hour": b, "time_coefficient_se": float(se[1]), "gamma": g, "gamma_se": float(se[2]),
           "chi2_per_dof": chi, "time_significant": time_sig, "gamma_resolved": g_resolved}
    if time_sig:
        out["decision"] = "TIME_DRIFT" if (not g_resolved or sky_ok or zero_ok) else "MIXED_TIME_AND_SPATIAL"
        if not g_resolved:
            out["decision"] = "TIME_DRIFT"
    elif not g_resolved:
        out["decision"] = "UNRESOLVED"
        out["reason"] = "sky/receiver coefficient not resolved: time and sky are confounded in this design"
    elif sky_ok and not zero_ok:
        out["decision"] = "SKY_FIXED"
    elif zero_ok and not sky_ok:
        out["decision"] = "RECEIVER_FIXED"
    elif not (sky_ok or zero_ok):
        out["decision"] = "SKY_GRADIENT"
    else:
        out["decision"] = "UNRESOLVED"
    return out


# ------------------------------------------------------------------ expectations from Level 1 shifts, control lines, replay

def expected_from_points(points: list, labels: list, f0_hz: float) -> dict:
    """The same 'expected' structure as design.expected_signatures, but from what Level 1 recorded (REDUCE's own LSRK shift per
    capture): the parameter-free predictions for the pairwise position offsets and for the slow sky-null drift of the reference."""
    k = _k(f0_hz)
    labs = sorted(set(labels) - {"?"})
    mean_shift = {lab: float(np.mean([p.lsrk_shift_m_s for p, l in zip(points, labels) if l == lab])) for lab in labs}
    ref = "A" if "A" in labs else labs[0]
    pairwise = {f"{lab}-{ref}": {"delta_lsrk_shift_m_s": mean_shift[lab] - mean_shift[ref],
                                 "sky_stationary_predicts_delta_topocentric_frequency_hz": k * (mean_shift[lab] - mean_shift[ref]),
                                 "receiver_fixed_predicts_delta_topocentric_frequency_hz": 0.0}
                for lab in labs if lab != ref}
    ref_pts = [p for p, l in zip(points, labels) if l == ref]
    slope = 0.0
    if len(ref_pts) >= 3:
        t_h = np.array([(p.t_mid_s - ref_pts[0].t_mid_s) / 3600.0 for p in ref_pts])
        slope = float(np.polyfit(t_h, [p.lsrk_shift_m_s for p in ref_pts], 1)[0]) if np.ptp(t_h) > 0 else 0.0
    return {"lsrk_shift_m_s": mean_shift, "pairwise": pairwise,
            "fixed_sky_expected_drift": {ref: {"lsrk_shift_change_m_s_per_hour": slope, "sky_stationary_predicts_topocentric_drift_hz_per_hour": k * slope}},
            "reference_label": ref, "frequency_per_velocity_hz_per_m_s": k}


def find_control_lines(points: list, *, exclude_channels: Optional[np.ndarray] = None, n_lines: int = 5, min_separation: int = 40,
                       z_min: float = 8.0, max_width_channels: int = 12) -> list:
    """Narrow, persistent spectral lines outside the candidate: peaks of the high-passed MEDIAN spectrum standing out by z_min robust
    sigmas. They serve as controls: a line that moves the same way as the candidate is not evidence about the candidate."""
    ok = np.array([_usable_all(p) for p in points])
    vals = np.array([p.value for p in points], dtype=float)
    vals = np.where(ok, vals, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(vals, axis=0)
    usable = np.isfinite(med)
    hp = _highpass(np.nan_to_num(med, nan=0.0), usable)
    if exclude_channels is not None and len(exclude_channels):
        hp[np.asarray(exclude_channels, dtype=int)] = 0.0
    robust = 1.4826 * np.median(np.abs(hp[usable] - np.median(hp[usable]))) + 1e-12
    z = hp / robust
    out, work = [], np.abs(z).copy()
    for _ in range(n_lines * 4):
        c = int(np.argmax(work))
        if work[c] < z_min:
            break
        half = work[c] / 2.0
        a = c
        while a > 0 and work[a] > half and abs(c - a) <= max_width_channels:
            a -= 1
        b = c
        while b < work.size - 1 and work[b] > half and abs(c - b) <= max_width_channels:
            b += 1
        width = b - a
        if width <= max_width_channels:
            out.append({"channel": c, "z": float(z[c]), "width_channels": int(width), "sign": int(np.sign(hp[c]))})
        work[max(0, c - min_separation): c + min_separation + 1] = 0.0
        if len(out) >= n_lines:
            break
    return out


def control_line_tracks(points: list, lines: list, f0_hz: float, channel_width_hz: float, labels: Optional[list] = None) -> list:
    """Track each control line through the captures and describe its frame behaviour: total motion, slope with time, and the
    coefficient gamma of its topocentric frequency against the LSRK-shift term (1 = sky-stationary, 0 = receiver-fixed)."""
    res = []
    t = np.array([p.t_mid_s for p in points])
    t_h = (t - t.min()) / 3600.0
    sh = np.array([p.lsrk_shift_m_s for p in points])
    for ln in lines:
        ms = track_line(points, start_channel=ln["channel"], search_half_width=12, fit_half_width=10, sign=ln["sign"], min_snr_formal=4.0)
        ii, f, sf = centroid_hz(points, ms)
        row = {"start_channel": ln["channel"], "z": ln["z"], "n_detected": int(len(ii))}
        if len(ii) >= 5:
            ch = np.array([ms[i].centroid_channel for i in ii])
            x = _k(f0_hz) * (sh[ii] - sh[ii].mean())
            X = np.column_stack([np.ones(len(ii)), t_h[ii], x])
            rank_ok = np.linalg.matrix_rank(X) == 3 and np.corrcoef(t_h[ii], x)[0, 1] ** 2 < 0.98
            fit_t = ols(np.column_stack([np.ones(len(ii)), t_h[ii]]), f)
            row.update({"channel_first": float(ch[0]), "channel_last": float(ch[-1]), "channel_range": float(np.ptp(ch)),
                        "slope_hz_per_hour": fit_t["coefficients"][1], "slope_se_hz_per_hour": fit_t["standard_errors"][1],
                        "residual_rms_hz": fit_t["residual_rms"]})
            if rank_ok:
                fit_g = ols(X, f)
                row.update({"gamma_sky": fit_g["coefficients"][2], "gamma_se": fit_g["standard_errors"][2]})
            else:
                row["gamma_sky"] = None
                row["gamma_note"] = "time and LSRK shift are collinear over these captures; gamma not separable"
            row["moves_more_than_1_channel"] = bool(np.ptp(ch) > 1.0)
        res.append(row)
    return res
