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

    # 4. fold onto the RF axis
    rf = f0 + bb
    sa_img, sb_img = _interp(rf - f_a, bb, resid), _interp(rf - f_b, bb, resid)
    T = 0.5 * (sa_img - sb_img)
    U = 0.5 * np.sqrt(_interp(rf - f_a, bb, sS) ** 2 + _interp(rf - f_b, bb, sS) ** 2)
    # only the folded band is a spectrum at all: outside it one image is missing, so those bins are dropped (not
    # carried as INVALID, which would read as a mostly-unusable capture to the quality stage)
    inside = np.abs(rf - f0) <= (USABLE_HZ - delta)
    rf, T, U = rf[inside], T[inside], U[inside]
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
        "baseline_model": f"switched_S_polynomial_degree_{BASELINE_DEGREE}_outside_both_images",
        "baseline_coefficients": [float(c) for c in coef], "image_halfwidth_km_s": IMAGE_HALFWIDTH_KMS,
        "baseline_free_bins": int(free.sum()), "baseline_fit_rms_fraction": fit_rms,
        "fft": est[f_a].fft_config,
    }
    return FswitchSpectrum(rf, T, U, mask, clip, info)
