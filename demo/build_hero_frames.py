"""Home-page hero animation from REAL data: ALMITA's frequency-switched capture of 2026-11-12 22:42 UTC (l=5 b=0,
ALMITA-OBSERVE point 3, 40 s), averaged up progressively so the page can show the real noise going down.

Each tuning (LO A, LO B) is cut into 0.02 s chunks in time order; frame k is the fold of the mean PSD of the first k
chunks of each tuning (k x 0.02 s per tuning; frames at log-spaced k). The fold, its baseline and the RF axis are reduce_engine.fswitch_fold's
own code (the frozen engine is only called, never changed): its spectral estimate is fed the accumulated PSDs.
The velocity axis (LSRK) is the official REDUCE run's (REDUCE-20261112-231215-610238), on the same RF bins.
Chunks are combined with a MEAN (so the noise averages down as 1/sqrt(k)); the official REDUCE used a median, so the
last frame is close to, not identical with, the published spectrum.

Usage (one IQ file, ~1 GB peak):  .venv/bin/python demo/build_hero_frames.py --out demo/static/project/hero_frames.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import h5py
import numpy as np

from hi_spectral_metric import robust_psd_from_iq
from reduce_engine import fswitch_fold
from reduce_engine.config import ReduceConfig
from reduce_engine.spectral import SpectralEstimate

IQ = Path("data/mosaic/ALMITA-OBSERVE-20261112-22:38:42/data/iq/ALMITA-OBSERVE-20261112-22:40:04/ALMITA-OBSERVE_0003.h5")
REDUCE = Path("data/reduced/ALMITA-OBSERVE/REDUCE-20261112-231215-610238")
CHUNK_S = 0.02                                     # ~4 FFTs of 8192 at 2.4 MS/s: the shortest honest chunk
N_FRAMES = 240                                     # frames at log-spaced k (the noise falls as 1/sqrt(k))
V_MIN, V_MAX, V_STEP = -91.4, 76.2, 1.2882          # km/s LSRK, the published hero grid (audit a03 figure grid)


def chunk_psds(iq: np.ndarray, attrs: dict, cfg: ReduceConfig):
    rate = float(attrs["sample_rate_hz"])
    n_chunk = int(round(CHUNK_S * rate))
    f_a, f_b = float(attrs["fswitch_f_a_hz"]), float(attrs["fswitch_f_b_hz"])
    out = {f_a: [], f_b: []}
    for s in json.loads(attrs["fswitch_segments_json"]):
        lo = min(out, key=lambda k: abs(k - float(s["lo_hz"])))
        a, n = int(s["start_sample"]), int(s["n_samples"])
        for c in range(a, a + n - n_chunk + 1, n_chunk):
            _, psd = robust_psd_from_iq(iq[2 * c:2 * (c + n_chunk)], rate, lo, fft_size=cfg.fft_size, combine="mean")
            out[lo].append(psd.astype(np.float64))
    return f_a, f_b, {k: np.array(v) for k, v in out.items()}


def fold(A: np.ndarray, B: np.ndarray, f_a: float, f_b: float, freq_bb: np.ndarray, attrs: dict, cfg: ReduceConfig):
    """reduce_engine.fswitch_fold.switched_spectrum with its spectral estimate replaced by the accumulated PSDs."""
    psd = {f_a: A, f_b: B}

    def fake_estimate(_iq, *, sample_rate_hz, center_frequency_hz, config):
        lo = min(psd, key=lambda k: abs(k - center_frequency_hz))
        return SpectralEstimate(frequency_hz=freq_bb + lo, psd=psd[lo], segments=np.vstack([psd[lo], psd[lo]]),
                                clipping_fraction=0.0, fft_config={"uncertainty_combine": "mean"})

    tiny = dict(attrs)
    segs = json.loads(attrs["fswitch_segments_json"])
    tiny["fswitch_segments_json"] = json.dumps([{**s, "start_sample": 0, "n_samples": 1} for s in segs])
    real_est, real_unc = fswitch_fold.spectral_mod.estimate_spectrum, fswitch_fold.uncertainty_mod.per_capture_uncertainty
    fswitch_fold.spectral_mod.estimate_spectrum = fake_estimate
    fswitch_fold.uncertainty_mod.per_capture_uncertainty = lambda seg, combine="median": np.ones(seg.shape[1])
    try:
        sw = fswitch_fold.switched_spectrum(np.zeros(2, dtype=np.uint8), tiny, cfg)
    finally:
        fswitch_fold.spectral_mod.estimate_spectrum, fswitch_fold.uncertainty_mod.per_capture_uncertainty = real_est, real_unc
    return sw.frequency_hz, sw.fractional


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    cfg_raw = json.loads((REDUCE / "config.json").read_text())
    cfg = ReduceConfig(**{k: v for k, v in cfg_raw.items() if k in ReduceConfig.__dataclass_fields__})
    with h5py.File(IQ, "r") as f:
        attrs = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in f.attrs.items()}
        iq = f["iq_data"][()]
    f_a, f_b, ch = chunk_psds(iq, attrs, cfg)
    del iq
    freq_bb, _ = robust_psd_from_iq(np.full(16 * cfg.fft_size, 127, dtype=np.uint8), float(attrs["sample_rate_hz"]), 0.0,
                                    fft_size=cfg.fft_size)
    with h5py.File(REDUCE / "points" / "3" / "master_spectrum.h5", "r") as m:
        m_f, m_v, m_y = m["frequency_hz"][()], m["velocity_lsrk_m_s"][()] / 1e3, m["relative_intensity"][()]
    grid = np.arange(V_MIN, V_MAX + 1e-6, V_STEP)
    order = np.argsort(m_v)
    k_max = min(len(ch[f_a]), len(ch[f_b]))
    cA, cB = np.cumsum(ch[f_a][:k_max], axis=0), np.cumsum(ch[f_b][:k_max], axis=0)
    frames, ks = [], sorted({int(round(x)) for x in np.geomspace(1, k_max, N_FRAMES)})
    for k in ks:
        rf, T = fold(cA[k - 1] / k, cB[k - 1] / k, f_a, f_b, freq_bb, attrs, cfg)
        v = np.interp(rf, m_f, m_v * 1.0, left=np.nan, right=np.nan)
        ok = np.isfinite(v) & np.isfinite(T)
        # bin-average onto the hero's velocity grid (several RF bins per 1.29 km/s cell)
        idx = np.round((v[ok] - V_MIN) / V_STEP).astype(int)
        good = (idx >= 0) & (idx < grid.size)
        s = np.bincount(idx[good], weights=T[ok][good], minlength=grid.size)
        c = np.bincount(idx[good], minlength=grid.size)
        frames.append(np.where(c > 0, s / np.maximum(c, 1), np.nan)[::-1] * 100)   # % , increasing frequency
    last = frames[-1]
    pub = np.interp(grid, m_v[order], m_y[order])[::-1] * 100
    r = float(np.corrcoef(last[np.isfinite(last)], pub[np.isfinite(last)])[0, 1])
    rest, c_kms = float(cfg.hi_rest_frequency_hz), 299792.458
    out = {
        "source": f"{IQ} (ALMITA-OBSERVE point 3, l=5 b=0, 2026-11-12 22:42 UTC, 40 s, frequency switched +/-600 kHz)",
        "method": __doc__.strip().splitlines()[2:7],
        "chunk_s": CHUNK_S, "seconds_per_tuning": [round(CHUNK_S * k, 2) for k in ks],
        "freq_mhz": [round(rest * (1 - V_MAX / c_kms) / 1e6, 4), round(rest * (1 - V_MIN / c_kms) / 1e6, 4)],
        "rest_mhz": rest / 1e6, "r_last_vs_published": round(r, 4),
        "frames": [[None if not np.isfinite(x) else round(float(x), 2) for x in fr] for fr in frames],
    }
    Path(args.out).write_text(json.dumps(out, separators=(",", ":")))
    sd = [float(np.nanstd(fr[(np.arange(fr.size) < 25)])) for fr in (frames[0], frames[len(frames) // 4], frames[len(frames) // 2], frames[-1])]
    print(f"frames {len(frames)}  r(last, published) {r:.3f}  peak last {np.nanmax(last):.2f} %  "
          f"edge std (first, 1/4, 1/2, last of {k_max} chunks): {[round(x, 2) for x in sd]}")


if __name__ == "__main__":
    main()
