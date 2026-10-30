#!/usr/bin/env python3
"""RFI masks for MAIN captures (operator 2026-10-30): find the noise that has a pattern and say which channels and
which moments to leave out - WITHOUT touching the capture.

Indoors on 2026-10-28/30 the Pi 5 radiated a 357.142857 kHz comb (125 MHz / 350, crystal-stable, teeth narrower than
14 Hz) into the feed from ~1 m, with or without the DS18 harness; other narrow lines come and go with the room, and
near the Pi the noise also arrives in bursts. The HI line is broad (tens to hundreds of kHz) and smooth, so a few
narrow channels and a few short bursts can be left out without touching it.

Three masks, all measured on the capture itself:
  - PULSE:  FFT frames (8192 samples, 3.4 ms) whose band power is far over the capture's robust level are dropped
            before averaging (robust z over the median/MAD of all frames).
  - COMB:   the comb harmonics inside the band, each searched within +-TOOTH_WINDOW_HZ (the SDR's ppm offset); a tooth
            that stands out gets its channels masked.
  - LINE:   any other narrow peak over the rolling median of the pulse-free mean spectrum. Its width is capped
            (LINE_MAX_HALF_WIDTH_BINS) so nothing HI-wide can ever be called a line.
A line seen in earlier captures of the same session ("known") is masked from KNOWN_LINE_DB on, a lower bar.

Contract (NATIVE_GRID): channels are FLAGGED, never interpolated or filled; the raw HDF5 is only read. The result is
kept next to the session as a side-car (write_sidecar), so any analysis can be redone with or without the mask.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

FFT_SIZE = 8192                         # 293 Hz channels at 2.4 MS/s, same as local_rfi_check
BLOCK_SECONDS = 0.25                    # one row of the mask spectrogram
READ_CHUNK_FRAMES = 256                 # frames read and transformed at a time (~4 MB of uint8): keeps the Pi's RAM low
MEDIAN_BINS = 101                       # rolling median the narrow excess is measured against (~30 kHz)
EDGE_FRACTION = 0.05                    # anti-alias roll-off at the band edges: never analysed
COMB_SPACING_HZ = 125e6 / 350           # 357 142.857 Hz, measured 2026-10-28
TOOTH_WINDOW_HZ = 6e3
# Thresholds follow the capture's own channel noise: a mean of N frames varies ~4.34/sqrt(N) dB per channel
# (0.08 dB for 10 s, 0.18 dB for 2 s). A new line needs LINE_SIGMAS of that and at least LINE_DB_FLOOR; HI is broad and
# smooth and never reaches it over a 30 kHz rolling median.
LINE_SIGMAS, LINE_DB_FLOOR = 8.0, 1.0   # a new narrow line
KNOWN_SIGMAS, KNOWN_DB_FLOOR = 5.0, 0.6 # a comb tooth (known frequency) or a line already seen in this session
EDGE_SIGMAS, EDGE_DB_FLOOR = 3.0, 0.4   # a masked region extends while the excess stays over this
DC_HALF_WIDTH_BINS = 2                  # the RTL's own DC spike at the centre: always masked (QUICKLOOK masks it too)
MIN_HALF_WIDTH_BINS = 2
LINE_MAX_HALF_WIDTH_BINS = 20           # +-5.9 kHz: a "line" is never wider than this; HI is far wider
PULSE_Z = 6.0                           # robust z of a frame's band power
PULSE_MIN_RATIO = 1.02                  # ...and at least 2 % over the median, so a very steady capture flags nothing
KNOWN_MATCH_HZ = 1.5e3

CODE_OK, CODE_COMB, CODE_LINE, CODE_PULSE = 0, 1, 2, 3
CODE_NAMES = {CODE_OK: "ok", CODE_COMB: "comb", CODE_LINE: "line", CODE_PULSE: "pulse"}
SCHEMA = "almita.rfi_mask.v1"


def _rolling_median(x: np.ndarray, width: int) -> np.ndarray:
    from local_rfi_check import _rolling_median as rm
    return rm(x, width)


def read_capture_frames(path: Path, fft_size: int = FFT_SIZE, chunk_frames: int = READ_CHUNK_FRAMES):
    """Yield (frame power spectra [n, fft_size] fftshifted, rail hits, samples) chunk by chunk. Read only."""
    import h5py
    window = np.hanning(fft_size).astype(np.float32)
    with h5py.File(path, "r") as f:
        if f.attrs.get("capture_status") != "success":
            raise ValueError(f"capture is not complete/successful: {path}")
        ds = f["iq_data"]
        per_frame = 2 * fft_size
        n_frames = ds.shape[0] // per_frame
        for start in range(0, n_frames, chunk_frames):
            stop = min(n_frames, start + chunk_frames)
            raw = ds[start * per_frame:stop * per_frame]
            rails = int(np.count_nonzero((raw == 0) | (raw == 255)))
            x = raw.astype(np.float32) - 127.5
            z = (x[0::2] + 1j * x[1::2]).reshape(-1, fft_size) * window
            yield np.fft.fftshift(np.abs(np.fft.fft(z, axis=1)) ** 2, axes=1).astype(np.float32), rails, raw.size


def capture_attrs(path: Path) -> Dict[str, Any]:
    import h5py
    with h5py.File(path, "r") as f:
        return {k: (v.item() if hasattr(v, "item") else (v.decode() if isinstance(v, bytes) else v)) for k, v in f.attrs.items()}


def comb_teeth_hz(center_hz: float, sample_rate_hz: float) -> List[float]:
    half = sample_rate_hz / 2 * (1 - 2 * EDGE_FRACTION)
    first = int(np.ceil((center_hz - half) / COMB_SPACING_HZ))
    last = int(np.floor((center_hz + half) / COMB_SPACING_HZ))
    return [n * COMB_SPACING_HZ for n in range(first, last + 1)]


def thresholds(n_frames: int) -> Dict[str, float]:
    sigma = 4.34 / np.sqrt(max(n_frames, 1))
    return {"channel_sigma_db": round(float(sigma), 4), "line_db": round(max(LINE_DB_FLOOR, LINE_SIGMAS * sigma), 3),
            "known_db": round(max(KNOWN_DB_FLOOR, KNOWN_SIGMAS * sigma), 3), "edge_db": round(max(EDGE_DB_FLOOR, EDGE_SIGMAS * sigma), 3)}


def _grow(excess: np.ndarray, peak: int, inner: np.ndarray, max_half: int, edge_db: float) -> Tuple[int, int]:
    lo = hi = peak
    while lo - 1 >= 0 and inner[lo - 1] and peak - lo < max_half and excess[lo - 1] > edge_db:
        lo -= 1
    while hi + 1 < len(excess) and inner[hi + 1] and hi - peak < max_half and excess[hi + 1] > edge_db:
        hi += 1
    lo, hi = min(lo, peak - MIN_HALF_WIDTH_BINS), max(hi, peak + MIN_HALF_WIDTH_BINS)
    return max(lo, 0), min(hi, len(excess) - 1)


def pulse_frames(frame_power: np.ndarray) -> Tuple[np.ndarray, Dict[str, float]]:
    med = float(np.median(frame_power))
    mad = float(np.median(np.abs(frame_power - med))) * 1.4826
    if med <= 0:
        return np.zeros(frame_power.shape, bool), {"median": med, "mad": mad}
    z = (frame_power - med) / mad if mad > 0 else np.zeros_like(frame_power)
    return (z > PULSE_Z) & (frame_power > PULSE_MIN_RATIO * med), {"median": med, "mad": mad}


def find_channel_mask(freq: np.ndarray, mean_db: np.ndarray, center_hz: float, sample_rate_hz: float,
                      known_lines_hz: Iterable[float] = (), n_frames: int = 2929) -> Dict[str, Any]:
    """Comb, DC and line channels of one pulse-free mean spectrum (a mean of n_frames FFT frames)."""
    th = thresholds(n_frames)
    n = len(freq)
    excess = mean_db - _rolling_median(mean_db, MEDIAN_BINS)
    edge = int(n * EDGE_FRACTION)
    inner = np.zeros(n, bool)
    inner[edge:n - edge] = True
    codes = np.zeros(n, np.uint8)
    bin_hz = float(freq[1] - freq[0])
    teeth = []
    for t in comb_teeth_hz(center_hz, sample_rate_hz):
        sel = np.flatnonzero(inner & (np.abs(freq - t) < TOOTH_WINDOW_HZ))
        if not sel.size:
            continue
        peak = int(sel[np.argmax(excess[sel])])
        tooth = {"harmonic_hz": round(t, 1), "peak_hz": round(float(freq[peak]), 1), "excess_db": round(float(excess[peak]), 2),
                 "masked": bool(excess[peak] >= th["known_db"])}
        if tooth["masked"]:
            lo, hi = _grow(excess, peak, inner, LINE_MAX_HALF_WIDTH_BINS, th["edge_db"])
            codes[lo:hi + 1] = CODE_COMB
            tooth["masked_hz"] = [round(float(freq[lo] - bin_hz / 2), 1), round(float(freq[hi] + bin_hz / 2), 1)]
        teeth.append(tooth)
    dc = int(np.argmin(np.abs(freq - center_hz)))
    codes[max(dc - DC_HALF_WIDTH_BINS, 0):dc + DC_HALF_WIDTH_BINS + 1] = CODE_LINE
    known = np.asarray(list(known_lines_hz), float)
    lines = [{"frequency_hz": round(float(freq[dc]), 1), "excess_db": round(float(excess[dc]), 2), "known": True, "dc": True,
              "masked_hz": [round(float(freq[max(dc - DC_HALF_WIDTH_BINS, 0)] - bin_hz / 2), 1),
                            round(float(freq[dc + DC_HALF_WIDTH_BINS] + bin_hz / 2), 1)]}]
    for i in range(max(edge, 5), min(n - edge, n - 5)):
        if codes[i] or excess[i] != excess[i - 5:i + 6].max():
            continue
        is_known = known.size > 0 and bool(np.min(np.abs(known - freq[i])) < KNOWN_MATCH_HZ)
        if excess[i] >= th["line_db"] or (is_known and excess[i] >= th["known_db"]):
            lo, hi = _grow(excess, i, inner, LINE_MAX_HALF_WIDTH_BINS, th["edge_db"])
            codes[lo:hi + 1] = np.where(codes[lo:hi + 1] == CODE_OK, CODE_LINE, codes[lo:hi + 1])
            lines.append({"frequency_hz": round(float(freq[i]), 1), "excess_db": round(float(excess[i]), 2),
                          "known": is_known, "masked_hz": [round(float(freq[lo] - bin_hz / 2), 1),
                                                           round(float(freq[hi] + bin_hz / 2), 1)]})
    codes[~inner] = CODE_OK
    return {"codes": codes, "excess_db": excess, "inner": inner, "teeth": teeth, "lines": lines, "thresholds": th}


def analyze_capture(path: Path, known_lines_hz: Iterable[float] = (), fft_size: int = FFT_SIZE,
                    block_seconds: float = BLOCK_SECONDS) -> Dict[str, Any]:
    """Masks of one capture. Arrays: freq_hz, mean_db_raw (all frames), mean_db_clean (pulse-free frames), codes,
    excess_db, block_pulse_fraction, block_codes (per block: the channel codes, with every channel of a block that
    is mostly pulse set to PULSE)."""
    attrs = capture_attrs(path)
    fc = float(attrs.get("center_frequency_hz"))
    fs = float(attrs.get("sample_rate_hz"))
    frame_power, rails, samples = [], 0, 0
    spectra_sum = np.zeros(fft_size, np.float64)
    # pass 1: every frame's band power (and the all-frames mean), chunk by chunk
    edge = int(fft_size * EDGE_FRACTION)
    for spec, r, s in read_capture_frames(path, fft_size):
        frame_power.append(spec[:, edge:fft_size - edge].mean(axis=1))
        spectra_sum += spec.sum(axis=0)
        rails += r
        samples += s
    frame_power = np.concatenate(frame_power) if frame_power else np.zeros(0)
    n_frames = len(frame_power)
    if n_frames == 0:
        raise ValueError(f"{path}: no complete FFT frame")
    pulses, pulse_stats = pulse_frames(frame_power)
    # pass 2: the pulse-free mean (a second read keeps memory flat instead of holding every frame)
    clean_sum = np.zeros(fft_size, np.float64)
    k = 0
    for spec, _, _ in read_capture_frames(path, fft_size):
        keep = ~pulses[k:k + len(spec)]
        clean_sum += spec[keep].sum(axis=0)
        k += len(spec)
    n_clean = int((~pulses).sum())
    freq = fc + np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / fs))
    mean_raw = 10 * np.log10(np.maximum(spectra_sum / n_frames, 1e-20))
    mean_clean = 10 * np.log10(np.maximum(clean_sum / max(n_clean, 1), 1e-20)) if n_clean else mean_raw
    found = find_channel_mask(freq, mean_clean, fc, fs, known_lines_hz, max(n_clean, 1))
    frames_per_block = max(1, int(round(block_seconds * fs / fft_size)))
    n_blocks = int(np.ceil(n_frames / frames_per_block))
    block_pulse = np.array([pulses[b * frames_per_block:(b + 1) * frames_per_block].mean() for b in range(n_blocks)])
    block_codes = np.repeat(found["codes"][None, :], n_blocks, axis=0)
    block_codes[block_pulse > 0.5] = CODE_PULSE
    masked = found["codes"] != CODE_OK
    inner = found["inner"]
    return {
        "schema": SCHEMA, "source_hdf5": str(path), "center_frequency_hz": fc, "sample_rate_hz": fs,
        "gain_db": attrs.get("applied_gain_db", attrs.get("gain")), "capture_start_utc": attrs.get("capture_start_utc"),
        "fft_size": fft_size, "frames": n_frames, "frames_per_block": frames_per_block,
        "pulse_fraction": round(float(pulses.mean()), 5), "pulse_frames": int(pulses.sum()), "pulse_stats": pulse_stats,
        "adc_rail_hit_fraction": rails / samples if samples else float("nan"),
        "masked_channel_fraction": round(float(masked[inner].mean()), 5),
        "comb_channels": int((found["codes"] == CODE_COMB).sum()), "line_channels": int((found["codes"] == CODE_LINE).sum()),
        "teeth": found["teeth"], "lines": found["lines"], "thresholds": found["thresholds"],
        "tooth_max_db": max((t["excess_db"] for t in found["teeth"]), default=0.0),
        "arrays": {"freq_hz": freq, "mean_db_raw": mean_raw.astype(np.float32), "mean_db_clean": mean_clean.astype(np.float32),
                   "excess_db": found["excess_db"].astype(np.float32), "codes": found["codes"], "inner": inner,
                   "frame_pulse": pulses, "block_pulse_fraction": block_pulse.astype(np.float32),
                   "block_codes": block_codes},
    }


def summary(result: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in result.items() if k != "arrays"}


def write_sidecar(result: Dict[str, Any], out_dir: Path, stem: str) -> Dict[str, str]:
    """<stem>.rfimask.npz (every array) + <stem>.rfimask.json (the summary). Written atomically; never the HDF5."""
    out_dir.mkdir(parents=True, exist_ok=True)
    npz, js = out_dir / f"{stem}.rfimask.npz", out_dir / f"{stem}.rfimask.json"
    tmp = out_dir / f".{stem}.rfimask.tmp.npz"
    np.savez_compressed(tmp, **result["arrays"])
    tmp.replace(npz)
    tmp_js = out_dir / f".{stem}.rfimask.json.tmp"
    tmp_js.write_text(json.dumps({**summary(result), "code_names": CODE_NAMES}, default=float))
    tmp_js.replace(js)
    return {"npz": str(npz), "json": str(js)}


def display_spectrum(result: Dict[str, Any], columns: int = 2048) -> Dict[str, Any]:
    """Decimated for the web: power averaged per column, a column is masked when any of its channels is."""
    a = result["arrays"]
    n = len(a["freq_hz"])
    step = max(1, n // columns)
    m = n // step * step
    freq = a["freq_hz"][:m].reshape(-1, step).mean(axis=1)
    lin = 10 ** (a["mean_db_clean"][:m].astype(np.float64) / 10)
    db = 10 * np.log10(lin.reshape(-1, step).mean(axis=1))
    raw = 10 * np.log10((10 ** (a["mean_db_raw"][:m].astype(np.float64) / 10)).reshape(-1, step).mean(axis=1))
    code = a["codes"][:m].reshape(-1, step).max(axis=1)
    inner = a["inner"][:m].reshape(-1, step).all(axis=1)
    return {"frequency_hz": [round(float(f), 1) for f in freq], "power_db": [round(float(d), 2) for d in db],
            "raw_power_db": [round(float(d), 2) for d in raw], "code": [int(c) for c in code],
            "inner": [bool(i) for i in inner]}


def display_block_rows(result: Dict[str, Any], columns: int = 512) -> List[List[int]]:
    """Spectrogram rows of the mask: per block, per column, the highest code (PULSE > LINE > COMB > OK)."""
    bc = result["arrays"]["block_codes"]
    n = bc.shape[1]
    step = max(1, n // columns)
    m = n // step * step
    return bc[:, :m].reshape(bc.shape[0], -1, step).max(axis=2).astype(int).tolist()


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="RFI masks of MAIN captures, written as side-cars (raw HDF5 untouched).")
    p.add_argument("captures", nargs="+", type=Path)
    p.add_argument("--out-dir", type=Path, required=True)
    a = p.parse_args(argv)
    known: List[float] = []
    for c in a.captures:
        r = analyze_capture(c, known)
        write_sidecar(r, a.out_dir, c.stem)
        known += [ln["frequency_hz"] for ln in r["lines"] if not ln.get("dc")]
        print(f"{c.name}: tooth max {r['tooth_max_db']:+.1f} dB, comb ch {r['comb_channels']}, lines {len(r['lines'])} "
              f"({r['line_channels']} ch), masked {100 * r['masked_channel_fraction']:.2f} %, pulses {100 * r['pulse_fraction']:.2f} %")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
