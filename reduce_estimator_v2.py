"""REDUCE ESTIMATOR v2 (operator 2026-11-10: "arma el estimador para correr de nuevo reduce") - OPT-IN, off by default.

Why: REDUCE's frozen spectral estimate (hi_spectral_metric.robust_psd_from_iq) cuts a capture into consecutive,
NON-overlapping Hann frames and takes the MEDIAN of their |FFT|^2. Both choices throw integration away. Simulated
on pure noise and measured after smoothing to ~19 kHz (the HI line is hundreds of channels wide), that estimate
uses ~32 % of the capture's time; Hann 50 % overlap + MEAN uses ~95 % (noise / 1.7, ~3x the effective time).
The median was there for robustness against RFI; here that job is done by the spectral kurtosis instead, which
removes only what is not noise.

What it does, per capture (and per LO for a frequency-switched one, through the same estimate_spectrum call):
  1. Blocks of BLOCK_FRAMES consecutive fft_size spans (128 x 8192 samples = 0.44 s at 2.4 MS/s); inside each block
     Hann frames every fft_size/2 samples (2B-1 frames, so nothing at a frame edge is lost but one half frame per
     block).
  2. Spectral kurtosis per channel from the block's B NON-overlapping frames (independent):
        SK = (M+1)/(M-1) * (M*S2/S1^2 - 1)
     Gaussian noise - the sky, the load AND the HI line itself - gives SK = 1 (sigma ~ 2/sqrt(M)). A pulse makes a
     few frames huge -> SK > 1; a steady carrier is too even -> SK < 1. A (block, channel) cell outside
     [1 - LOWER_SIGMA*sigma, 1 + UPPER_SIGMA*sigma] is left out (false alarm on pure noise ~0.02 %, simulated).
     A block with more than BROADBAND_FRACTION of its channels flagged is a broadband event: left out entirely.
  3. PSD = mean of the remaining cells (each block weighted by its frame count) x ln 2. ln 2 is the median of an
     exponential distribution over its mean, so on noise the level matches the classic median estimate: calibration
     profiles, baselines and every downstream number keep their scale.
  4. `segments` (REDUCE's per-sub-integration spectra, used for its per-bin uncertainty and its fixed-spur detector)
     become the per-block means x ln 2: independent, so uncertainty = scatter of block means / sqrt(n_blocks).
  5. Channels with more than CHANNEL_RFI_FRACTION of their time left out get MaskFlag.RFI in REDUCE's mask (flagged,
     never filled - NATIVE_GRID); a channel with nothing left keeps the plain mean so the PSD stays finite.

reduce_engine is FROZEN (scripts/science_forensics_field.py pins it): no file of it changes. Inside the REDUCE RUN
process only (reduce_campaign_run.py --estimator-v2), install() wraps reduce_engine's capture read, spectral
estimate and mask build, and restores them on exit. Without the flag nothing here runs and REDUCE gives exactly what
it gave before. With --rfi-mask too, this estimate is the inner one: the RFI MASK's COMB/LINE channel flags still
apply, its PULSE frame cut steps aside (the kurtosis already removed pulses, per channel and per block).
"""
from __future__ import annotations

import contextlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import numpy as np

SCHEMA = "almita.reduce_estimator_v2.v1"
ESTIMATOR = "v2: Hann 50% overlap, mean of spectral-kurtosis-clean cells x ln2"
REPORT_NAME = "estimator_v2_report.json"
BLOCK_FRAMES = 128            # independent frames per kurtosis block (0.44 s at 2.4 MS/s, fft 8192)
MIN_BLOCK_FRAMES = 32
MIN_BLOCKS = 4
LOWER_SIGMA, UPPER_SIGMA = 3.0, 6.0     # the SK distribution has a light lower tail and a heavy upper one
BROADBAND_FRACTION = 0.20
CHANNEL_RFI_FRACTION = 0.50
LEVEL_MATCH = math.log(2.0)


def sk_sigma(m: int) -> float:
    """Standard deviation of the SK estimator on Gaussian noise for M independent frames (Nita & Gary 2010)."""
    return math.sqrt(4.0 * m * m / ((m - 1.0) * (m + 2.0) * (m + 3.0)))


def sk_limits(m: int) -> tuple:
    s = sk_sigma(m)
    return max(0.0, 1.0 - LOWER_SIGMA * s), 1.0 + UPPER_SIGMA * s


def spectral_kurtosis(p: np.ndarray) -> np.ndarray:
    """SK per channel from M independent power spectra p (M, channels)."""
    m = p.shape[0]
    s1, s2 = p.sum(axis=0), np.square(p).sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        sk = (m + 1.0) / (m - 1.0) * (m * s2 / np.square(s1) - 1.0)
    return np.where(s1 > 0, sk, 1.0)


def block_frames(n_frames: int) -> Optional[int]:
    """Independent frames per block: BLOCK_FRAMES when the capture has room for MIN_BLOCKS of them, otherwise as
    many as give MIN_BLOCKS blocks, never fewer than MIN_BLOCK_FRAMES (None = too short, use the classic estimate)."""
    if n_frames >= BLOCK_FRAMES * MIN_BLOCKS:
        return BLOCK_FRAMES
    b = n_frames // MIN_BLOCKS
    return b if b >= MIN_BLOCK_FRAMES else None


def estimate(iq_bytes: np.ndarray, sample_rate_hz: float, center_frequency_hz: float, fft_size: int) -> Optional[Dict[str, Any]]:
    """The v2 estimate of one capture (interleaved uint8 I/Q), or None when it is too short for kurtosis blocks."""
    raw = np.asarray(iq_bytes, dtype=np.uint8)
    if raw.ndim != 1 or raw.size % 2:
        raise ValueError("IQ must be an even-length interleaved uint8 vector")
    n = raw.size // 2
    n_frames = n // fft_size
    b = block_frames(n_frames)
    if b is None:
        return None
    hop = fft_size // 2
    window = np.hanning(fft_size).astype(np.float32)
    starts: List[int] = list(range(0, (n_frames // b) * b, b))
    if n_frames - len(starts) * b >= MIN_BLOCK_FRAMES:          # a usable partial block at the end
        starts.append(len(starts) * b)
    means, weights, flags, n_indep = [], [], [], []
    for s in starts:
        m_ind = min(b, n_frames - s)
        a, z = s * fft_size, (s + m_ind) * fft_size
        x = raw[2 * a:2 * z].astype(np.float32) - 127.5
        iq = (x[0::2] + 1j * x[1::2]).astype(np.complex64)
        idx = np.arange(0, iq.size - fft_size + 1, hop)
        frames = np.lib.stride_tricks.as_strided(iq, shape=(idx.size, fft_size), strides=(iq.strides[0] * hop, iq.strides[0]))
        p = np.square(np.abs(np.fft.fftshift(np.fft.fft(frames * window, axis=1), axes=1)))
        sk = spectral_kurtosis(p[0::2])                         # even frames = the non-overlapping, independent ones
        lo, hi = sk_limits(m_ind)
        bad = (sk < lo) | (sk > hi)
        means.append(p.mean(axis=0))
        weights.append(float(p.shape[0]))
        flags.append(bad)
        n_indep.append(m_ind)
    means_a, flags_a, w = np.array(means), np.array(flags), np.array(weights)
    broadband = flags_a.mean(axis=1) > BROADBAND_FRACTION
    flags_a[broadband, :] = True
    keep_w = (~flags_a) * w[:, None]
    kept_sum = keep_w.sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        psd = (means_a * keep_w).sum(axis=0) / kept_sum
    all_gone = kept_sum <= 0
    psd[all_gone] = (means_a[:, all_gone] * w[:, None]).sum(axis=0) / w.sum()
    flagged_fraction = 1.0 - kept_sum / w.sum()
    frequency = center_frequency_hz + np.fft.fftshift(np.fft.fftfreq(fft_size, 1.0 / sample_rate_hz))
    used_frames = int(sum(n_indep))
    return {
        "frequency_hz": frequency, "psd": psd * LEVEL_MATCH, "segments": means_a * LEVEL_MATCH,
        "channel_flagged_fraction": flagged_fraction, "rfi_channels": flagged_fraction > CHANNEL_RFI_FRACTION,
        "stats": {"blocks": int(len(starts)), "frames_per_block": int(b), "block_seconds": round(b * fft_size / sample_rate_hz, 4),
                  "frames_overlapped": int(w.sum()), "samples_used_fraction": round(used_frames / max(n_frames, 1), 5),
                  "sk_limits_full_block": [round(v, 4) for v in sk_limits(b)],
                  "cells_flagged_fraction": round(float(flags_a.mean()), 6),
                  "blocks_broadband_flagged": int(broadband.sum()),
                  "channels_rfi": int((flagged_fraction > CHANNEL_RFI_FRACTION).sum()),
                  "channels_all_flagged": int(all_gone.sum())},
    }


class EstimatorV2:
    """Wraps the frozen pipeline's read/estimate/mask for one RUN and keeps a per-point report."""

    def __init__(self):
        self.current: Optional[str] = None
        self.points: Dict[str, Dict[str, Any]] = {}
        self._last_psd = None
        self._last_rfi = None
        self._recent: list = []        # (lo_hz, frequency_hz, rfi_channels) of each estimate since the last fold/read

    def _rec(self) -> Dict[str, Any]:
        key = self.current or "unknown capture"
        return self.points.setdefault(key, {"capture": key, "estimates": []})

    def read_capture(self, original, path, *a, **k):
        self.current = str(path)
        self._recent = []
        return original(path, *a, **k)

    def estimate_spectrum(self, original, iq_bytes, *, sample_rate_hz, center_frequency_hz, config):
        from reduce_engine.spectral import SpectralEstimate, _clipping_fraction
        rec = self._rec()
        try:
            r = estimate(iq_bytes, float(sample_rate_hz), float(center_frequency_hz), int(config.fft_size))
        except Exception as exc:  # noqa: BLE001 - a capture v2 cannot handle is reduced the classic way, and said so
            rec["estimates"].append({"applied": False, "reason": f"{type(exc).__name__}: {exc}"})
            r = None
        if r is None:
            if not rec["estimates"] or rec["estimates"][-1].get("applied", True):
                rec["estimates"].append({"applied": False, "reason": "capture too short for kurtosis blocks - classic estimate"})
            self._last_psd = self._last_rfi = None
            self._recent.append((float(center_frequency_hz), None, None))
            return original(iq_bytes, sample_rate_hz=sample_rate_hz, center_frequency_hz=center_frequency_hz, config=config)
        rec["estimates"].append({"applied": True, "center_frequency_hz": float(center_frequency_hz), **r["stats"]})
        fft_config = {"fft_size": int(config.fft_size), "window": "Hann (numpy.hanning), 50% overlap",
                      "combine": "mean of SK-clean (block, channel) cells x ln2 (level-matched to the median estimate)",
                      "estimator": ESTIMATOR, "sample_rate_hz": float(sample_rate_hz),
                      "center_frequency_hz": float(center_frequency_hz), "iq_center": 127.5,
                      "n_segments": int(r["segments"].shape[0]), "segments_are": "per-block means (independent)",
                      **{f"v2_{k}": v for k, v in r["stats"].items()}}
        est = SpectralEstimate(frequency_hz=r["frequency_hz"], psd=r["psd"], segments=r["segments"],
                               clipping_fraction=_clipping_fraction(iq_bytes), fft_config=fft_config)
        self._last_psd, self._last_rfi = est.psd, r["rfi_channels"]
        self._recent.append((float(center_frequency_hz), np.asarray(r["frequency_hz"], float), r["rfi_channels"]))
        return est

    def switched_spectrum(self, original, iq, attrs, config):
        """A frequency-switched capture (reduce_engine/fswitch_fold.py, frozen) never goes through build_mask, so the
        channels kurtosis flagged at LO A and LO B would be lost in the fold. Here they are carried over: every folded
        bin computed from a flagged baseband channel (through either image) gets MaskFlag.RFI."""
        from reduce_engine.models import MaskFlag
        self._recent = []
        sw = original(iq, attrs, config)
        flagged = [(lo, f, rfi) for lo, f, rfi in self._recent if rfi is not None]
        self._recent = []
        if not flagged:
            return sw
        lo0, f0, _ = flagged[0]
        bb = f0 - lo0
        bad_bb = np.zeros(bb.size, bool)
        for lo, f, rfi in flagged:
            if f.shape == bb.shape and np.allclose(f - lo, bb, rtol=0, atol=1.0):
                bad_bb |= rfi
        los = [float(attrs["fswitch_f_a_hz"]), float(attrs["fswitch_f_b_hz"])]
        bad = folded_bins_touching(np.asarray(sw.frequency_hz, float), los, bb, bad_bb)
        sw.mask[bad] |= MaskFlag.RFI.value
        rec = self._rec()
        rec["fswitch_baseband_channels_flagged"] = int(bad_bb.sum())
        rec["fswitch_folded_bins_flagged"] = int(bad.sum())
        return sw

    def build_mask(self, original, frequency_hz, psd, **k):
        from reduce_engine.models import MaskFlag
        result = original(frequency_hz, psd, **k)
        if self._last_psd is not None and psd is self._last_psd and self._last_rfi is not None and self._last_rfi.shape == result.mask.shape:
            bad = self._last_rfi
            result.mask[bad] |= MaskFlag.RFI.value
            rec = self._rec()
            if rec["estimates"]:
                rec["estimates"][-1]["rfi_channels_flagged_in_mask"] = int(bad.sum())
        return result

    def report(self) -> Dict[str, Any]:
        pts = list(self.points.values())
        ests = [e for p in pts for e in p["estimates"]]
        applied = [e for e in ests if e.get("applied")]
        med = (lambda key: float(np.median([e[key] for e in applied])) if applied else None)
        return {"schema": SCHEMA, "enabled": True, "estimator": ESTIMATOR,
                "what": ("Hann 50% overlap + mean instead of non-overlapping Hann + median; (block, channel) cells whose "
                         "spectral kurtosis is not noise-like left out; level x ln2 so it matches the median estimate"),
                "simulated_effective_time": {"classic_median": 0.32, "v2": 0.95, "note": "pure noise, after ~19 kHz smoothing"},
                "points": len(pts), "estimates": len(ests), "estimates_v2": len(applied),
                "estimates_classic_fallback": len(ests) - len(applied),
                "median_cells_flagged_fraction": med("cells_flagged_fraction"),
                "blocks_broadband_flagged": int(sum(e["blocks_broadband_flagged"] for e in applied)),
                "median_channels_rfi": med("channels_rfi"),
                "fswitch_points": sum(1 for p in pts if "fswitch_folded_bins_flagged" in p),
                "fswitch_folded_bins_flagged": int(sum(p.get("fswitch_folded_bins_flagged", 0) for p in pts)),
                "per_point": pts}


def folded_bins_touching(rf_hz: np.ndarray, los_hz: List[float], bb_hz: np.ndarray, bad_bb: np.ndarray) -> np.ndarray:
    """Folded bins (RF axis) whose value was interpolated from a flagged baseband channel through any of the LOs:
    fswitch_fold reads the switched spectrum at rf - lo for each LO, linearly between the two nearest channels."""
    out = np.zeros(rf_hz.size, bool)
    if not bad_bb.any():
        return out
    step = float(bb_hz[1] - bb_hz[0])
    for lo in los_hz:
        x = (rf_hz - lo - bb_hz[0]) / step
        i0 = np.floor(x + 1e-6).astype(int)
        frac = x - i0
        for idx, use in ((i0, np.ones(x.size, bool)), (i0 + 1, frac > 1e-6)):
            ok = use & (idx >= 0) & (idx < bb_hz.size)
            out[ok] |= bad_bb[idx[ok]]
    return out


@contextlib.contextmanager
def install() -> Iterator[EstimatorV2]:
    """Wrap the frozen pipeline's three functions for the duration of one RUN in this process; always restored.
    Install it BEFORE reduce_rfi_mask.install() so the RFI MASK wraps this estimate, not the classic one."""
    from reduce_engine import fswitch_fold, masks as masks_mod, pipeline, spectral as spectral_mod
    ev = EstimatorV2()
    saved = (pipeline.read_capture, spectral_mod.estimate_spectrum, masks_mod.build_mask, fswitch_fold.switched_spectrum)
    orig_read, orig_est, orig_mask, orig_sw = saved
    pipeline.read_capture = lambda path, *a, **k: ev.read_capture(orig_read, path, *a, **k)
    spectral_mod.estimate_spectrum = lambda *a, **k: ev.estimate_spectrum(orig_est, *a, **k)
    masks_mod.build_mask = lambda *a, **k: ev.build_mask(orig_mask, *a, **k)
    fswitch_fold.switched_spectrum = lambda *a, **k: ev.switched_spectrum(orig_sw, *a, **k)
    try:
        yield ev
    finally:
        (pipeline.read_capture, spectral_mod.estimate_spectrum, masks_mod.build_mask,
         fswitch_fold.switched_spectrum) = saved


def write_report(output_dir: str, report: Dict[str, Any]) -> str:
    path = Path(output_dir) / REPORT_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2, default=float))
    tmp.replace(path)
    return str(path)
