"""Frequency switching for MAIN (operator 2026-11-09: "comienza con la conmutación de frecuencia").

Why: dividing a spectrum by a reference taken minutes earlier, elsewhere on the sky, leaves the receiver's band shape
(LNA, cable, SDR filter) as +/-2-6 % waves - ten times the radiometer noise - because that shape drifts between the two
captures (2026-11-05 roof night). Here the antenna stays put and only the SDR's LO alternates every few seconds between
fA = f0 - delta and fB = f0 + delta. The band shape belongs to the receiver's baseband, so it is the same in both and
cancels in S = (A - B) / B; the 21 cm line sits at +delta in A and at -delta in B, so S holds it twice, once positive
and once negative. Folding, T(f) = [S(f - fA) - S(f - fB)] / 2 on the RF axis, puts it back once at its real frequency.

The tuning is the project's own: sdr_tuning.tune_explicitly (gain, rate, RTL AGC off, LNA Bias-T, acknowledged by
rtl_tcp) at the start; each switch then sends ONLY the frequency (SDRCapture.configure), drains the post-retune
samples (SETTLE_S, measured by fswitch_settle.py), and every LO used is checked afterwards against rtl_tcp's own
"set freq" lines. Repeated LOs at ABBA cycle boundaries are recorded as one segment (one retune per segment), every
segment's head is checked for old-LO samples (head_trim), and each segment's spectra are computed while the next
one records. The SDR is refused while any
other ALMITA process or web job uses it. capture.py and the frozen engines are untouched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent
OUT_ROOT = ROOT / "data" / "fswitch"
NFFT = 8192
C_KMS = 299792.458
HI_REST_HZ = 1420405751.77
USABLE_HZ = 1.0e6          # |baseband| kept: the RTL band rolls off beyond ~ +/-1 MHz at 2.4 MS/s
DC_HZ = 10e3               # the DC spike of the SDR: excluded


# ------------------------------------------------------------------ pure maths (tested)
def power_spectrum(raw: np.ndarray, nfft: int = NFFT) -> np.ndarray:
    """Mean |FFT|^2 (Hann) of interleaved uint8 I/Q, fftshifted (index nfft/2 = DC)."""
    x = raw.astype(np.float32) - 127.5
    z = x[0::2] + 1j * x[1::2]
    k = len(z) // nfft
    if k == 0:
        raise ValueError("fewer samples than one FFT frame")
    win = np.hanning(nfft).astype(np.float32)
    acc = np.zeros(nfft)
    for i in range(0, k, 128):
        blk = z[i * nfft:min(k, i + 128) * nfft].reshape(-1, nfft) * win
        acc += np.sum(np.abs(np.fft.fft(blk, axis=1)) ** 2, axis=0)
    return np.fft.fftshift(acc / k)


def baseband_hz(nfft: int, sample_rate: float) -> np.ndarray:
    return (np.arange(nfft) - nfft / 2) * sample_rate / nfft


def switched(spec_a: np.ndarray, spec_b: np.ndarray) -> np.ndarray:
    """S = (A - B) / B on the common baseband axis: the receiver's band shape cancels."""
    return (spec_a - spec_b) / spec_b


def fold(s: np.ndarray, bb_hz: np.ndarray, f_a: float, f_b: float, rf_hz: np.ndarray) -> np.ndarray:
    """T(f) = [S(f - fA) - S(f - fB)] / 2 on an RF axis; NaN where either copy falls outside the usable band."""
    ok = (np.abs(bb_hz) <= USABLE_HZ) & (np.abs(bb_hz) >= DC_HZ)
    sa = np.interp(rf_hz - f_a, bb_hz, np.where(ok, s, np.nan), left=np.nan, right=np.nan)
    sb = np.interp(rf_hz - f_b, bb_hz, np.where(ok, s, np.nan), left=np.nan, right=np.nan)
    return 0.5 * (sa - sb)


def smooth(y: np.ndarray, n: int) -> np.ndarray:
    """Running mean that ignores NaN (NaN where the whole window is NaN)."""
    good = np.isfinite(y)
    k = np.ones(n)
    num = np.convolve(np.where(good, y, 0.0), k, "same")
    den = np.convolve(good.astype(float), k, "same")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den >= n / 2, num / den, np.nan)


def remove_baseline(v_kms: np.ndarray, y: np.ndarray, line_window=(-60.0, 80.0), order: int = 1) -> np.ndarray:
    """Subtract a low-order polynomial fitted outside the line window (never inside it)."""
    fit = np.isfinite(y) & ((v_kms < line_window[0]) | (v_kms > line_window[1]))
    if fit.sum() < order + 5:
        return y
    c = np.polyfit(v_kms[fit], y[fit], order)
    return y - np.polyval(c, v_kms)


def ripple_rms(y: np.ndarray, mask: np.ndarray) -> float:
    m = mask & np.isfinite(y)
    return float(np.std(y[m])) if m.sum() > 10 else float("nan")


def analyse(spectra: List[np.ndarray], los: List[float], sample_rate: float, offset_hz: float,
            f0_hz: float = HI_REST_HZ, smooth_hz: float = 10e3,
            centre_topo_km_s: Optional[float] = None) -> Dict[str, Any]:
    """All products of one run: mean A/B, switched, folded (percent), velocity axis and ripple figures.
    BL-105: with `centre_topo_km_s` the LOs straddle that velocity's frequency (f0_hz), the velocity axis is taken
    against the line's rest frequency and the line / baseline windows follow the centre."""
    los = np.asarray(los, float)
    f_a, f_b = float(np.min(los)), float(np.max(los))
    A = np.mean([s for s, lo in zip(spectra, los) if lo == f_a], axis=0)
    B = np.mean([s for s, lo in zip(spectra, los) if lo == f_b], axis=0)
    bb = baseband_hz(len(A), sample_rate)
    nsm = max(1, int(round(smooth_hz / (sample_rate / len(A)))))
    S = switched(A, B)
    rf = f0_hz + np.linspace(-0.45e6, 0.45e6, 1801)
    T = fold(smooth(S, nsm), bb, f_a, f_b, rf) * 100
    if centre_topo_km_s is None:
        v = -(rf - f0_hz) / f0_hz * C_KMS
        base_window, line_window = (-60.0, 80.0), (-30.0, 40.0)
    else:
        v = (HI_REST_HZ - rf) / HI_REST_HZ * C_KMS
        c = float(centre_topo_km_s)
        base_window, line_window = (c - 70.0, c + 70.0), (c - 35.0, c + 35.0)
    Tb = remove_baseline(v, T, base_window)
    band = (np.abs(bb) <= USABLE_HZ) & (np.abs(bb) >= DC_HZ)
    # the band shape itself (what position switching leaves behind): A divided by a smoothed copy of itself
    shape = smooth(A, nsm) / np.nanmedian(A[band]) - 1
    line_bb = (np.abs(bb - (f0_hz - f_a)) < 0.35e6) | (np.abs(bb - (f0_hz - f_b)) < 0.35e6)
    # half-split: the same switched spectrum from the first and the second half of the run - the stability check
    half = len(spectra) // 2
    def half_switched(sl):
        a = [s for s, lo in zip(spectra[sl], los[sl]) if lo == f_a]
        b = [s for s, lo in zip(spectra[sl], los[sl]) if lo == f_b]
        return switched(np.mean(a, axis=0), np.mean(b, axis=0)) if a and b else None
    s1, s2 = half_switched(slice(0, half)), half_switched(slice(half, None))
    split = smooth(s1 - s2, nsm) * 100 if s1 is not None and s2 is not None else None
    return {
        "f_a_hz": f_a, "f_b_hz": f_b, "baseband_hz": bb, "A": A, "B": B, "switched": S, "rf_hz": rf, "v_kms": v,
        "folded_pct": T, "folded_baselined_pct": Tb, "band_shape_pct": shape * 100,
        "ripple_band_shape_pct": ripple_rms(shape * 100, band & ~line_bb),
        "ripple_switched_pct": ripple_rms(smooth(S, nsm) * 100, band & ~line_bb),
        "ripple_half_split_pct": ripple_rms(split, band & ~line_bb) if split is not None else None,
        "line_excess_pct": float(np.nanmean(Tb[(v > line_window[0]) & (v < line_window[1])])),
        "line_window_kms": list(line_window), "centre_topo_km_s": centre_topo_km_s,
        "off_line_rms_pct": ripple_rms(Tb, (v < -60) | (v > 80)),
        "n_a": int(np.sum(los == f_a)), "n_b": int(np.sum(los == f_b)), "smooth_hz": smooth_hz,
    }


# ------------------------------------------------------------------ acquisition
# The pause after a retune, MEASURED (fswitch_settle.py, 2026-11-09, data/fswitch/settle_20261109T100031Z): the R820T
# changes frequency in one clean step - no relock dip, no slow settling (40 switches aligned and averaged: 0 ms tail;
# 6 same-frequency controls: nothing) - but the old LO's samples already in flight (USB, rtl_tcp, cable, forwarder)
# keep arriving for 40-86 ms (median 72). 0.15 s = 1.7 x the worst seen. Every segment is checked anyway (head_trim).
SETTLE_S = 0.15


def lo_segments(cycles: int) -> List[tuple]:
    """ABBA order as (lo_index, dwells) runs: A B | B A | A B ... -> A, BB, AA, ..., one retune per run. 0 = A, 1 = B."""
    seq = [lo for c in range(cycles) for lo in ((0, 1) if c % 2 == 0 else (1, 0))]
    runs: List[list] = []
    for lo in seq:
        if runs and runs[-1][0] == lo:
            runs[-1][1] += 1
        else:
            runs.append([lo, 1])
    return [tuple(r) for r in runs]


def head_trim(raw: np.ndarray, sample_rate: float, block_s: float = 0.01, search_s: float = 0.3,
              nsig: float = 5.0) -> int:
    """Samples to drop at the start of a segment: leading 10 ms blocks whose total power is off the segment's own
    level by more than nsig robust sigmas (old-LO samples are ~10 % off in power here). 0 when the head is clean."""
    x = raw.astype(np.float32) - 127.5
    n = int(block_s * sample_rate)
    k = len(x) // (2 * n)
    if k < 10:
        return 0
    pw = (x[:k * 2 * n].reshape(k, 2 * n) ** 2).mean(axis=1)
    body = pw[int(np.ceil(search_s / block_s)):]
    if body.size < 5:
        return 0
    med = float(np.median(body))
    sig = 1.4826 * float(np.median(np.abs(body - med))) or 1e-9
    drop = 0
    for i in range(min(k, int(np.ceil(search_s / block_s)))):
        if abs(pw[i] - med) > nsig * sig:
            drop = i + 1
        else:
            break
    return drop * n


def segment_spectra(raw: np.ndarray, parts: int, drop_samples: int) -> List[np.ndarray]:
    """One power spectrum per dwell of a merged segment (after dropping the head): the per-dwell spectra the analysis
    and the half-split stability check expect, without a retune between them."""
    body = raw[2 * drop_samples:]
    per = (len(body) // 2 // parts) * 2
    return [power_spectrum(body[i * per:(i + 1) * per]) for i in range(parts)]


def _segment_job(raw: np.ndarray, sample_rate: float, parts: int):
    """Off the event loop: head check, per-dwell spectra, ADC rms of one recorded segment."""
    drop = head_trim(raw, sample_rate)
    rms = float(np.std(raw[: int(2 * sample_rate // 5)].astype(np.float32) - 127.5))
    return segment_spectra(raw, parts, drop), drop, rms


async def acquire(cycles: int, dwell_s: float, offset_hz: float, gain_db: Optional[float], settle_s: float = SETTLE_S,
                  host: str = "127.0.0.1", port: int = 1234, progress=print,
                  centre_topo_km_s: Optional[float] = None) -> Dict[str, Any]:
    import sdr_tuning
    from sdr_capture import SDRCapture
    operating = sdr_tuning.operating_config()
    f0 = int(operating["center_frequency_hz"])
    if centre_topo_km_s is not None:      # BL-105: straddle that velocity's frequency instead of the line at rest
        f0 = int(round(HI_REST_HZ * (1.0 - float(centre_topo_km_s) / C_KMS)))
    rate = int(operating["sample_rate_hz"])
    gain = float(gain_db if gain_db is not None else operating.get("gain_db", 40.2))
    f_a, f_b = int(round(f0 - offset_hz)), int(round(f0 + offset_hz))
    ack = sdr_tuning.JournalAckSource()
    sdr = SDRCapture("network", host, port, verbose=False)
    sdr.compact_console = True
    sdr.retune_settle_seconds = settle_s
    spectra, los, stamps, adc, trims = [], [], [], [], []
    segs = lo_segments(cycles)
    pending = None                       # spectra of the previous segment, computed while the next one records

    async def collect(job):
        lo, parts, fut = job
        sps, drop, raw_rms = await fut
        if drop:
            progress(f"segment at {lo / 1e6:.4f} MHz: head trimmed {drop / rate * 1e3:.0f} ms (old-LO samples)")
        for sp in sps:
            spectra.append(sp)
            los.append(lo)
            stamps.append(datetime.now(timezone.utc).isoformat())
            adc.append(raw_rms)
        trims.append(drop / rate * 1e3)

    t_start = time.monotonic()
    timing: List[tuple] = []                 # per segment: retune+drain, capture beyond its dwell, wait for the previous segment's maths
    try:
        await sdr.connect()
        tuning = await sdr_tuning.tune_explicitly(sdr, f_a, rate, gain, operating=operating)
        if not str(tuning.get("rtl_agc", "")).startswith("off"):
            raise RuntimeError(f"RTL AGC not confirmed off: {tuning.get('rtl_agc')}")
        ack.mark()
        await asyncio.sleep(settle_s)                              # the initial tune's own in-flight samples
        t_loop = time.monotonic()
        for n, (li, parts) in enumerate(segs):
            lo = (f_a, f_b)[li]
            t0 = time.monotonic()
            await sdr.configure(center_freq=lo, sample_rate=rate, gain=str(gain))   # retune + measured drain
            await sdr.flush_buffer()
            t1 = time.monotonic()
            raw = await sdr.capture_iq(parts * dwell_s, rate)       # in memory: no HDF5 per segment
            t2 = time.monotonic()
            if pending is not None:
                await collect(pending)
            t3 = time.monotonic()
            timing.append((t1 - t0, t2 - t1 - parts * dwell_s, t3 - t2))
            pending = (lo, parts, asyncio.create_task(asyncio.to_thread(_segment_job, raw, rate, parts)))
            if (n + 1) % 4 == 0 or n == len(segs) - 1:
                progress(f"segment {n + 1}/{len(segs)} recorded (ADC rms so far {adc[-1] if adc else float('nan'):.2f})")
        if pending is not None:
            await collect(pending)
        wall = time.monotonic() - t_start
        lines = ack.lines()
    finally:
        await sdr.close()
    seen = {int(m.group(1)) for ln in lines for m in [re.search(r"\bset freq (\d+)\b", ln)] if m}
    recorded = len(spectra) * dwell_s - sum(trims) / 1e3
    return {"spectra": spectra, "los": los, "utc": stamps, "adc_rms": adc, "f0_hz": f0, "sample_rate_hz": rate,
            "centre_topo_km_s": centre_topo_km_s,
            "gain_db": gain, "f_a_hz": f_a, "f_b_hz": f_b, "offset_hz": offset_hz, "dwell_s": dwell_s,
            "settle_s": settle_s, "tuning": tuning, "segments": len(segs),
            "head_trim_ms": {"segments_trimmed": sum(1 for t in trims if t > 0), "max": max(trims, default=0.0)},
            "efficiency": {"wall_s": round(wall, 2), "recorded_s": round(recorded, 2),
                           "fraction": round(recorded / wall, 3) if wall else None,
                           "setup_s": round(t_loop - t_start, 2),
                           "loop_fraction": round(recorded / (wall - (t_loop - t_start)), 3),
                           "per_segment_overhead_s": {k: round(float(np.mean([t[i] for t in timing])), 3)
                                                      for i, k in enumerate(("retune_drain", "capture_extra",
                                                                             "wait_previous"))} if timing else None},
            "lo_acknowledged": {"f_a": f_a in seen, "f_b": f_b in seen, "seen": sorted(seen)},
            "pll_not_locked_count": sdr_tuning.parse_acks(lines).get("pll_not_locked_count")}


def save(run: Dict[str, Any], res: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(out_dir / "spectra.npz", spectra=np.array(run["spectra"], np.float32),
                        lo_hz=np.array(run["los"]), utc=np.array(run["utc"]), adc_rms=np.array(run["adc_rms"]))
    summary = {k: run.get(k) for k in ("f0_hz", "centre_topo_km_s", "sample_rate_hz", "gain_db", "f_a_hz", "f_b_hz", "offset_hz", "dwell_s",
                                       "settle_s", "segments", "head_trim_ms", "efficiency", "lo_acknowledged",
                                       "pll_not_locked_count")}
    summary["tuning"] = {k: run["tuning"].get(k) for k in ("requested", "applied", "evidence", "rtl_agc", "bias_tee")}
    summary["adc_rms_range"] = [min(run["adc_rms"]), max(run["adc_rms"])]
    summary["started_utc"], summary["ended_utc"] = run["utc"][0], run["utc"][-1]
    summary["result"] = {k: res[k] for k in ("ripple_band_shape_pct", "ripple_switched_pct", "ripple_half_split_pct",
                                             "line_excess_pct", "off_line_rms_pct", "n_a", "n_b", "smooth_hz")}
    (out_dir / "run.json").write_text(json.dumps(summary, indent=1, default=str))
    # what STATUS draws: the folded, baselined spectrum on the velocity axis (finite points only, 0.01 % resolution)
    v, t = res["v_kms"], res["folded_baselined_pct"]
    ok = np.isfinite(t)
    (out_dir / "folded.json").write_text(json.dumps({
        "v_kms": [round(float(x), 2) for x in v[ok]], "folded_pct": [round(float(x), 3) for x in t[ok]],
        "line_window_kms": res.get("line_window_kms", [-30.0, 40.0]), "summary": summary}, default=str))
    plot(res, out_dir / "fswitch.png", summary)
    return summary


def plot(res: Dict[str, Any], path: Path, summary: Dict[str, Any]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    bb = res["baseband_hz"] / 1e3
    band = (np.abs(res["baseband_hz"]) <= USABLE_HZ) & (np.abs(res["baseband_hz"]) >= DC_HZ)
    nsm = max(1, int(round(res["smooth_hz"] / (abs(bb[1] - bb[0]) * 1e3))))
    fig, ax = plt.subplots(3, 1, figsize=(11, 11))
    ax[0].plot(bb[band], res["band_shape_pct"][band], color="0.4")
    ax[0].set_title(f"1. The receiver's band shape (LO A, smoothed): ±{res['ripple_band_shape_pct']:.2f} % rms - "
                    "what dividing by another pointing has to cancel")
    ax[1].plot(bb[band], smooth(res["switched"], nsm)[band] * 100, color="C0")
    ax[1].set_title(f"2. Switched (A−B)/B: ±{res['ripple_switched_pct']:.2f} % rms off the line images "
                    f"(half-split stability ±{res['ripple_half_split_pct'] or float('nan'):.2f} %)")
    for a in ax[:2]:
        a.set_xlabel("baseband offset [kHz]")
        a.set_ylabel("%")
        a.axhline(0, color="0.7", lw=.6)
    v = res["v_kms"]
    ax[2].plot(v, res["folded_baselined_pct"], color="C3")
    ax[2].axvspan(-30, 40, color="y", alpha=.15)
    ax[2].set_xlim(v.max(), v.min())
    ax[2].axhline(0, color="0.7", lw=.6)
    ax[2].set_xlabel("topocentric velocity [km/s] (receding → left)")
    ax[2].set_ylabel("%")
    ax[2].set_title(f"3. Folded spectrum: line window −30..+40 km/s {res['line_excess_pct']:+.2f} %, "
                    f"off-line rms ±{res['off_line_rms_pct']:.2f} %")
    fig.suptitle(f"ALMITA frequency switching · {summary['started_utc'][:19]} UTC · LO {summary['f_a_hz']/1e6:.4f} / "
                 f"{summary['f_b_hz']/1e6:.4f} MHz · {res['n_a']}+{res['n_b']} × {summary['dwell_s']} s · gain {summary['gain_db']} dB")
    plt.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cycles", type=int, default=20, help="A/B pairs (ABBA order)")
    ap.add_argument("--dwell", type=float, default=2.0, help="seconds per LO")
    ap.add_argument("--offset-hz", type=float, default=600e3, help="delta: LOs at f0 -/+ delta")
    ap.add_argument("--gain-db", type=float, default=None)
    ap.add_argument("--settle", type=float, default=SETTLE_S, help="seconds discarded after each retune (measured)")
    ap.add_argument("--label", default="")
    ap.add_argument("--out-dir", default=None, help="output folder (default data/fswitch/<UTC>[_label])")
    ap.add_argument("--web-job", default=None, help="the web job this run is (it does not count as another SDR user)")
    ap.add_argument("--center-velocity", type=float, default=None,
                    help="BL-105: centre the +/-84 km/s window on this TOPOCENTRIC velocity (km/s) instead of the line at "
                         "rest - STATUS does not know where the antenna points, so no LSRK correction here")
    a = ap.parse_args(argv)
    from transit_logger import sdr_busy
    busy = sdr_busy(ignore_job_id=a.web_job)
    if busy:
        print(f"REFUSED: the SDR is in use by {busy}")
        return 2
    print(f"FSWITCH {a.cycles} cycles x 2 x {a.dwell} s, LO f0 -/+ {a.offset_hz / 1e3:.0f} kHz, pause {a.settle} s", flush=True)
    if a.center_velocity is not None and not (math.isfinite(a.center_velocity) and abs(a.center_velocity) <= 600):
        print("REFUSED: --center-velocity must be within +/-600 km/s")
        return 2
    run = asyncio.run(acquire(a.cycles, a.dwell, a.offset_hz, a.gain_db, a.settle,
                              progress=lambda m: print(m, flush=True), centre_topo_km_s=a.center_velocity))
    res = analyse(run["spectra"], run["los"], run["sample_rate_hz"], run["offset_hz"],
                  **({"f0_hz": run["f0_hz"], "centre_topo_km_s": a.center_velocity} if a.center_velocity is not None else {}))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out_dir) if a.out_dir else OUT_ROOT / (stamp + (f"_{a.label}" if a.label else ""))
    out = out if out.is_absolute() else ROOT / out
    summary = save(run, res, out)
    print(json.dumps({"out": str(out), **summary["result"], "lo_acknowledged": summary["lo_acknowledged"],
                      "adc_rms_range": summary["adc_rms_range"], "efficiency": summary["efficiency"],
                      "head_trim_ms": summary["head_trim_ms"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
