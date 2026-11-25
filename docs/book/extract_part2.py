"""Extract the real measurements used by Parts II-III of the book from 2026-11-12 captures.

Reads (never writes) raw HDF5 IQ and saves small arrays to docs/book/data_part2.npz.
Heavy: run one at a time under a memory cap, e.g.
  systemd-run --user --scope -p MemoryMax=3G .venv/bin/python docs/book/extract_part2.py
"""
import json
import sys
from pathlib import Path

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import fswitch as F  # noqa: E402
import reduce_estimator_v2 as V2  # noqa: E402

WIZ = ROOT / "data/calibration/WIZARD-20261112-231442-936893/captures"
FS = ROOT / "data/mosaic/ALMITA-OBSERVE-20261112-22:38:42/data/iq/ALMITA-OBSERVE-20261112-22:40:04/ALMITA-OBSERVE_0013.h5"
NFFT = 8192
FSAMP = 2.4e6
out = {}


def mean_psd(paths):
    acc = None
    for p in paths:
        with h5py.File(p, "r") as f:
            raw = f["iq_data"][:]
        s = F.power_spectrum(raw, NFFT)
        acc = s if acc is None else acc + s
    return acc / len(paths)


for kind in ("AMBIENT_50R", "HI_BAJO", "HI_ALTO"):
    ps = sorted((WIZ / kind).glob("capture_*.h5"))
    out[f"psd_{kind}"] = mean_psd(ps)
    with h5py.File(ps[0], "r") as f:
        out[f"rms_{kind}"] = float(f.attrs["adc_rms_codes"])
out["bb_hz"] = F.baseband_hz(NFFT, FSAMP)

# raw samples and histogram (sky, HI_ALTO)
with h5py.File(sorted((WIZ / "HI_ALTO").glob("capture_*.h5"))[0], "r") as f:
    raw = f["iq_data"][:]
out["raw_first"] = raw[:400].astype(np.int16)
out["hist"] = np.bincount(raw, minlength=256)

# frequency switching, one real 40 s point on the plane
with h5py.File(FS, "r") as f:
    segs = json.loads(f.attrs["fswitch_segments_json"])
    fa, fb = float(f.attrs["fswitch_f_a_hz"]), float(f.attrs["fswitch_f_b_hz"])
    acc = {fa: [], fb: []}
    sk_rows = []
    for sgm in segs:
        a, n = sgm["start_sample"], sgm["n_samples"]
        part = f["iq_data"][2 * a:2 * (a + n)]
        acc[float(sgm["lo_hz"])].append((F.power_spectrum(part, NFFT), n))
        if len(sk_rows) < 1:  # spectral kurtosis of one 0.44 s block at LO A
            blk = part[: 2 * 128 * NFFT].astype(np.float32) - 127.5
            z = (blk[0::2] + 1j * blk[1::2]).reshape(128, NFFT) * np.hanning(NFFT)
            p = np.abs(np.fft.fftshift(np.fft.fft(z, axis=1), axes=1)) ** 2
            sk_rows.append(V2.spectral_kurtosis(p))
            out["single_frame"] = p[0]
            out["mean10"] = p[:10].mean(0)
            out["mean128"] = p.mean(0)
    for lo in (fa, fb):
        w = np.array([n for _, n in acc[lo]], float)
        out[f"fs_{'A' if lo == fa else 'B'}"] = sum(s * n for s, n in acc[lo]) / w.sum()
    out["fs_fa"], out["fs_fb"] = fa, fb
    out["sk_block"] = sk_rows[0]
    out["sk_limits"] = np.array(V2.sk_limits(128))

np.savez_compressed(Path(__file__).with_name("data_part2.npz"), **out)
print({k: (np.shape(v) if hasattr(v, "shape") else v) for k, v in out.items()})
