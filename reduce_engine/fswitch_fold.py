"""BL-100 (operator 2026-11-09: "descongela y aplica"): the SPECTRAL ESTIMATE of a frequency-switched capture (fswitch_capture.py).

pipeline.reduce_point calls this instead of spectral.estimate_spectrum when the capture carries a segment table:

  1. A and B: the same estimator as every other capture (spectral.estimate_spectrum, i.e. hi_spectral_metric's
     robust PSD) over the samples taken at LO A and at LO B - each on its own baseband axis, same bins.
  2. S = (A - B) / B on the baseband axis: the receiver's band shape, fixed in baseband, cancels; the 21 cm line,
     fixed in RF, appears at +delta (seen by A) and, negative, at -delta (seen by B).
  3. Baseline on S, BEFORE folding, only where neither copy of the line can be: |bb -/+ delta| > image half-width.
     With delta 600 kHz and a +/-80 km/s half-width that is mostly the middle of the band (+/-0.22 MHz). Low order
     (linear): a slope in S folds into a constant, and a constant cancels in the fold.
  4. Fold: T(f) = [S'(f - fA) - S'(f - fB)] / 2 on the RF axis f0 + bb. Both copies only exist within
     |f - f0| <= 1 MHz - delta (+/-0.4 MHz = +/-84 km/s topocentric); outside that the bins are NaN and masked.
     T is the fractional excess over the system's own level - the same quantity REDUCE's uncalibrated path gives
     ((psd - baseline) / baseline) - so the rest of the pipeline (velocity, resample, stack, quality, persistence,
     SCIENCE) needs no change.
  5. Uncertainty from each LO's own sub-integrations (uncertainty.per_capture_uncertainty), propagated through
     (A - B)/B and the fold.

No DC mask at f0: in a switched capture f0 is where the HI line is, not where an LO is (the LOs' DC spikes sit at
f0 -/+ delta, outside the folded band, and are excluded on the baseband axis before folding).

BL-105 (operator 2026-11-10): a capture switched around a centre other than the line's rest frequency carries the
expected LINE WINDOW (fswitch_line_window_topo_km_s, written by capture.py from fswitch_center.plan_point). Then the
baseline is fitted where the line really is not: on S outside both images of that window when enough bins are free
(degree 1, as above); otherwise - a broad inner-Galaxy line fills most of the baseband - S is folded as it is (a
constant in S cancels in the fold, a slope folds into a constant) and a degree-0/1 baseline is fitted on the FOLDED
spectrum, in the part of the folded band outside the line window (the line-free margin fswitch_center leaves beyond
the edge). A capture without that attribute is folded exactly as before.
Configurable throw (2026-11-11): the offset is read from the LOs (f_b - f_a), and a capture that also records MASK
windows (fswitch_mask_windows_topo_km_s: the local gas and its ghosts, when the target is elsewhere - the Magellanic
Clouds) keeps the baseline off them too.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from reduce_engine import spectral as spectral_mod, uncertainty as uncertainty_mod
from reduce_engine.models import MaskFlag

C_KMS = 299792.458
USABLE_HZ = 1.0e6                  # |baseband| kept (RTL roll-off beyond ~ +/-1 MHz at 2.4 MS/s), as fswitch.py
DC_HZ = 10e3                       # each LO's DC spike, excluded on the baseband axis
IMAGE_HALFWIDTH_KMS = 80.0         # where the line can be around each image when fitting S's baseline
BASELINE_DEGREE = 1
MIN_FREE_BINS_S = 300              # BL-105: fewer line-free baseband bins than this -> baseline on the folded spectrum
MIN_FREE_SPAN_HZ = 1.0e6           # ... or free bins spanning less than this: a line fitted on one edge only extrapolates
MIN_FREE_BINS_FOLDED = 40


@dataclass
class FswitchSpectrum:
    frequency_hz: np.ndarray       # RF axis, f0 + baseband bins
    fractional: np.ndarray         # T: folded, baselined fractional excess (NaN outside the folded band)
    uncertainty: np.ndarray        # 1-sigma of T
    mask: np.ndarray               # MaskFlag ints
    clipping_fraction: float
    info: dict[str, Any]


def _segments(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    import json
    raw = attrs.get("fswitch_segments_json")
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(str(raw)) if raw is not None else []


def is_switched(attrs: dict[str, Any]) -> bool:
    return attrs.get("fswitch_segments_json") is not None


def _interp(x: np.ndarray, xp: np.ndarray, fp: np.ndarray) -> np.ndarray:
    return np.interp(x, xp, fp, left=np.nan, right=np.nan)


def _line_window_topo(attrs: dict[str, Any]):
    raw = attrs.get("fswitch_line_window_topo_km_s")
    if raw is None:
        return None
    import json
    if isinstance(raw, bytes):
        raw = raw.decode()
    vals = json.loads(raw) if isinstance(raw, str) else list(raw)
    return float(min(vals)), float(max(vals))


def _mask_windows_topo(attrs: dict[str, Any]):
    """Known emission that is not the target (the local gas and its ghosts, fswitch_center.plan_point): [(lo, hi)]."""
    raw = attrs.get("fswitch_mask_windows_topo_km_s")
    if raw is None:
        return []
    import json
    if isinstance(raw, bytes):
        raw = raw.decode()
    vals = json.loads(raw) if isinstance(raw, str) else list(raw)
    return [(float(min(w)), float(max(w))) for w in vals]


def _ghost_overlap(window, f0: float, throw_hz: float, rest: float) -> float:
    v = (1 - (f0 + np.linspace(-(USABLE_HZ - throw_hz / 2), USABLE_HZ - throw_hz / 2, 801)) / rest) * C_KMS
    dv = throw_hz / rest * C_KMS
    hit = ((v - dv >= window[0]) & (v - dv <= window[1])) | ((v + dv >= window[0]) & (v + dv <= window[1]))
    return round(float(hit.mean()), 3)


def _robust_fit(x: np.ndarray, y: np.ndarray, degree: int) -> np.ndarray:
    coef = np.polyfit(x, y, degree)
    for _ in range(3):                                         # robust: clip > 4 sigma outliers (RFI lines)
        r = y - np.polyval(coef, x)
        sig = 1.4826 * np.median(np.abs(r - np.median(r))) or 1e-12
        keep = np.abs(r) < 4 * sig
        coef = np.polyfit(x[keep], y[keep], degree)
    return coef


def switched_spectrum(iq: np.ndarray, attrs: dict[str, Any], config) -> FswitchSpectrum:
    rate = float(attrs["sample_rate_hz"])
    f0 = float(attrs["center_frequency_hz"])
    f_a, f_b = float(attrs["fswitch_f_a_hz"]), float(attrs["fswitch_f_b_hz"])
    delta = 0.5 * (f_b - f_a)
    seg = _segments(attrs)
    pieces = {f_a: [], f_b: []}
    for s in seg:
        lo = float(s["lo_hz"])
        a, n = int(s["start_sample"]), int(s["n_samples"])
        pieces[min(pieces, key=lambda k: abs(k - lo))].append(iq[2 * a:2 * (a + n)])
    if not pieces[f_a] or not pieces[f_b]:
        raise ValueError("frequency-switched capture without samples at both LOs")
    est = {lo: spectral_mod.estimate_spectrum(np.concatenate(p), sample_rate_hz=rate, center_frequency_hz=lo, config=config)
           for lo, p in pieces.items()}
    A, B = est[f_a].psd, est[f_b].psd
    bb = est[f_a].frequency_hz - f_a
    if not np.allclose(bb, est[f_b].frequency_hz - f_b):
        raise ValueError("LO A and LO B spectra are not on the same baseband bins")
    ok_bb = (np.abs(bb) <= USABLE_HZ) & (np.abs(bb) >= DC_HZ) & (B > 0) & np.isfinite(A) & np.isfinite(B)
    S = np.where(ok_bb, (A - B) / np.where(B > 0, B, np.nan), np.nan)
    sa = uncertainty_mod.per_capture_uncertainty(est[f_a].segments) / A
    sb = uncertainty_mod.per_capture_uncertainty(est[f_b].segments) / B
    sS = np.sqrt(sa ** 2 + sb ** 2)

    # 3. baseline of S where neither image of the line can be
    window = _line_window_topo(attrs)                          # BL-105: the capture says where the line is
    masks = _mask_windows_topo(attrs)                          # ... and where other known emission is (local gas, ghosts)
    folded_baseline = None
    if window is None:
        hw_hz = IMAGE_HALFWIDTH_KMS / C_KMS * f0
        free = ok_bb & (np.abs(bb - delta) > hw_hz) & (np.abs(bb + delta) > hw_hz) & np.isfinite(S)
        if free.sum() < 50:
            raise ValueError(f"only {int(free.sum())} line-free baseband bins to fit the switched baseline")
        x = bb / 1e6
        coef = np.polyfit(x[free], S[free], BASELINE_DEGREE)
        for _ in range(3):                                         # robust: clip > 4 sigma outliers (RFI lines)
            r = S[free] - np.polyval(coef, x[free])
            sig = 1.4826 * np.median(np.abs(r - np.median(r))) or 1e-12
            keep = np.abs(r) < 4 * sig
            coef = np.polyfit(x[free][keep], S[free][keep], BASELINE_DEGREE)
        resid = S - np.polyval(coef, x)
        fit_rms = float(np.std((S - np.polyval(coef, x))[free]))
        baseline_model = f"switched_S_polynomial_degree_{BASELINE_DEGREE}_outside_both_images"
    else:
        rest = float(config.hi_rest_frequency_hz)
        line_lo_rf, line_hi_rf = rest * (1 - window[1] / C_KMS), rest * (1 - window[0] / C_KMS)
        in_a = (bb >= line_lo_rf - f_a) & (bb <= line_hi_rf - f_a)          # the line seen by LO A ...
        in_b = (bb >= line_lo_rf - f_b) & (bb <= line_hi_rf - f_b)          # ... and by LO B
        free = ok_bb & ~in_a & ~in_b & np.isfinite(S)
        for m_lo, m_hi in masks:                                            # ... and the local gas, by either LO
            lo_rf, hi_rf = rest * (1 - m_hi / C_KMS), rest * (1 - m_lo / C_KMS)
            free &= ~((bb >= lo_rf - f_a) & (bb <= hi_rf - f_a)) & ~((bb >= lo_rf - f_b) & (bb <= hi_rf - f_b))
        x = bb / 1e6
        span = float(np.ptp(bb[free])) if free.any() else 0.0
        if free.sum() >= MIN_FREE_BINS_S and span >= MIN_FREE_SPAN_HZ:
            coef = _robust_fit(x[free], S[free], BASELINE_DEGREE)
            resid = S - np.polyval(coef, x)
            fit_rms = float(np.std(resid[free]))
            baseline_model = f"switched_S_polynomial_degree_{BASELINE_DEGREE}_outside_both_line_window_images"
        else:
            coef, resid, fit_rms = np.zeros(1), S, float("nan")
            folded_baseline = (line_lo_rf, line_hi_rf)                     # fitted after the fold, below
            baseline_model = "folded_polynomial_outside_line_window"

    # 4. fold onto the RF axis
    rf = f0 + bb
    sa_img, sb_img = _interp(rf - f_a, bb, resid), _interp(rf - f_b, bb, resid)
    T = 0.5 * (sa_img - sb_img)
    U = 0.5 * np.sqrt(_interp(rf - f_a, bb, sS) ** 2 + _interp(rf - f_b, bb, sS) ** 2)
    # only the folded band is a spectrum at all: outside it one image is missing, so those bins are dropped (not
    # carried as INVALID, which would read as a mostly-unusable capture to the quality stage)
    inside = np.abs(rf - f0) <= (USABLE_HZ - delta)
    rf, T, U = rf[inside], T[inside], U[inside]
    if folded_baseline is not None:                            # BL-105: the line filled the baseband - fit here
        lo_rf, hi_rf = folded_baseline
        out_line = np.isfinite(T) & ((rf < lo_rf) | (rf > hi_rf))
        for m_lo, m_hi in masks:                               # the local gas and its ghosts are not baseline either
            out_line &= ~((rf >= rest * (1 - m_hi / C_KMS)) & (rf <= rest * (1 - m_lo / C_KMS)))
        if out_line.sum() < MIN_FREE_BINS_FOLDED:
            raise ValueError(f"only {int(out_line.sum())} line-free folded bins: the line window "
                             f"{window[0]:+.0f}..{window[1]:+.0f} km/s fills the folded band - move the centre")
        both_sides = (rf[out_line] < lo_rf).sum() >= MIN_FREE_BINS_FOLDED and (rf[out_line] > hi_rf).sum() >= MIN_FREE_BINS_FOLDED
        degree = 1 if both_sides else 0
        xf = (rf - f0) / 1e6
        coef = _robust_fit(xf[out_line], T[out_line], degree)
        T = T - np.polyval(coef, xf)
        fit_rms = float(np.std(T[out_line]))
        free = out_line
        baseline_model = f"folded_polynomial_degree_{degree}_outside_line_window"
    mask = np.zeros(rf.size, dtype=np.int64)
    mask[~np.isfinite(T)] |= MaskFlag.INVALID.value
    clip = max(est[f_a].clipping_fraction, est[f_b].clipping_fraction)
    if clip > 0.001:
        mask[:] |= MaskFlag.SATURATED.value
    info = {
        "spectral_mode": "frequency_switched_folded", "f_a_hz": f_a, "f_b_hz": f_b, "offset_hz": delta,
        "n_segments": len(seg), "samples_a": int(sum(p.size for p in pieces[f_a]) // 2),
        "samples_b": int(sum(p.size for p in pieces[f_b]) // 2),
        "folded_band_hz": [f0 - (USABLE_HZ - delta), f0 + (USABLE_HZ - delta)],
        "baseline_model": baseline_model,
        "baseline_coefficients": [float(c) for c in coef], "image_halfwidth_km_s": IMAGE_HALFWIDTH_KMS,
        "baseline_free_bins": int(free.sum()), "baseline_fit_rms_fraction": fit_rms,
        "fft": est[f_a].fft_config,
        **({"line_window_topo_km_s": [window[0], window[1]],
            # the switch throw (2 x offset) in velocity: a line wider than it puts its other image ("ghost", negative,
            # half height) inside the folded band. Fraction of the folded band whose ghost lands in the line window:
            "ghost_overlap_fraction": _ghost_overlap(window, f0, f_b - f_a, float(config.hi_rest_frequency_hz))}
           if window is not None else {}),
        **({"mask_windows_topo_km_s": [[w[0], w[1]] for w in masks],
            "local_ghost_in_line_window": str(attrs.get("fswitch_local_ghost_in_line_window", "")).lower() in ("true", "1")}
           if masks else {}),
    }
    return FswitchSpectrum(rf, T, U, mask, clip, info)
