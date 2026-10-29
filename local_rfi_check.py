#!/usr/bin/env python3
"""Quick local-RFI check of MAIN from STATUS (operator, 2026-10-29): a few seconds of MAIN at the operating frequency,
its spectrum with the narrow lines brought out, and the teeth of the 357.14 kHz comb that the Pi radiated through the
DS18 thermometer harness on 2026-10-28 (a ferrite on the harness took it from a tooth sum of ~40 down to ~12; with the
feed 3 m from the Pi it vanished). Meant to be run before every outing, to see in seconds whether the comb or the line
forest came back (a ferrite fell off, a new device nearby).

Also reports whether the NanoVNA is plugged into the Pi: while it sweeps it radiates into the SDR (1420.000 MHz is one
of its sweep points), so it is a known noise source whenever it is connected.

Same acquisition path as the gain pilot (SDRCapture + sdr_tuning.tune_explicitly with rtl_tcp's acknowledgement);
the capture is kept in data/rfi_check/ next to the result, so any check can be re-analysed later.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent
CHECK_DIR = ROOT / "data" / "rfi_check"
USB_DEVICES = Path("/sys/bus/usb/devices")

COMB_SPACING_HZ = 125e6 / 350          # 357 142.857 Hz, measured 2026-10-28 (= 25 MHz / 70): a crystal clock of the Pi
TOOTH_WINDOW_HZ = 6e3                  # each tooth is searched within ±6 kHz: the V3 read them ~3 kHz low (~2 ppm)
FFT_SIZE = 8192
MEDIAN_BINS = 101                      # rolling median that the narrow lines are measured against
LINE_THRESHOLD_DB = 6.0                # a "line": a local maximum this far over the rolling median
EDGE_FRACTION = 0.05                   # band edges (anti-alias roll-off) are left out of the line count
# Verdict lines, from the 2026-10-28 hunt (indoors, SAWbird + V3 at 19.7 dB): clean at 3 m (teeth ~0 dB, 0 lines),
# harness without ferrite ~+10 dB teeth and ~40 lines, with the ferrite +2..+4 dB.
COMB_PRESENT_DB = 6.0
COMB_FAINT_DB = 3.0
LINES_MANY = 10
DEFAULT_GAIN_DB = 19.7                 # the gain of the 2026-10-28 hunt, so results compare with it
DEFAULT_SECONDS = 2.0
NANOVNA_USB_ID = ("0483", "5740")      # STMicroelectronics Virtual COM Port, as the NanoVNA-H enumerates on the Pi


# ------------------------------------------------------------------ NanoVNA presence
def nanovna_present(usb_root: Path = USB_DEVICES) -> Dict[str, Any]:
    """Is a NanoVNA on the Pi's USB? By vendor:product id (and the product name when the device gives one)."""
    found: List[Dict[str, str]] = []
    if usb_root.is_dir():
        for dev in sorted(usb_root.iterdir()):
            try:
                vid = (dev / "idVendor").read_text().strip()
                pid = (dev / "idProduct").read_text().strip()
            except OSError:
                continue
            product = ""
            try:
                product = (dev / "product").read_text().strip()
            except OSError:
                pass
            if (vid, pid) == NANOVNA_USB_ID or "nanovna" in product.lower():
                found.append({"usb_path": dev.name, "usb_id": f"{vid}:{pid}", "product": product or "—"})
    if found:
        return {"present": True, "devices": found,
                "detail": "NanoVNA plugged into the Pi: while it sweeps it radiates into the SDR (1420.000 MHz is a sweep "
                          "point). Unplug it (or pause its sweep) before measuring."}
    return {"present": False, "devices": [], "detail": "no NanoVNA on the Pi's USB"}


# ------------------------------------------------------------------ analysis (pure)
def comb_teeth_hz(center_frequency_hz: float, sample_rate_hz: float) -> List[float]:
    """Every harmonic of the comb spacing inside the usable band (edges left out)."""
    half = sample_rate_hz / 2 * (1 - 2 * EDGE_FRACTION)
    first = int(np.ceil((center_frequency_hz - half) / COMB_SPACING_HZ))
    last = int(np.floor((center_frequency_hz + half) / COMB_SPACING_HZ))
    return [n * COMB_SPACING_HZ for n in range(first, last + 1)]


def _rolling_median(x: np.ndarray, width: int) -> np.ndarray:
    from numpy.lib.stride_tricks import sliding_window_view
    pad = width // 2
    return np.pad(np.median(sliding_window_view(x, width), axis=1), pad, mode="edge")


def analyze(iq: np.ndarray, sample_rate_hz: float, center_frequency_hz: float) -> Dict[str, Any]:
    """Spectrum of interleaved uint8 IQ, narrow-line excess over a rolling median, comb teeth, line count, verdict."""
    from sdr_capture import adc_level_summary
    x = iq.astype(np.float32) - 127.5
    z = x[0::2] + 1j * x[1::2]
    n_seg = len(z) // FFT_SIZE
    if n_seg < 1:
        raise ValueError(f"capture too short: {len(z)} complex samples, need at least {FFT_SIZE}")
    seg = z[: n_seg * FFT_SIZE].reshape(n_seg, FFT_SIZE) * np.hanning(FFT_SIZE)
    power = np.fft.fftshift(np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0))
    freq = center_frequency_hz + np.fft.fftshift(np.fft.fftfreq(FFT_SIZE, 1 / sample_rate_hz))
    db = 10 * np.log10(np.maximum(power, 1e-20))
    excess = db - _rolling_median(db, MEDIAN_BINS)
    edge = int(FFT_SIZE * EDGE_FRACTION)
    inner = np.zeros(FFT_SIZE, dtype=bool)
    inner[edge:FFT_SIZE - edge] = True
    teeth = []
    for t in comb_teeth_hz(center_frequency_hz, sample_rate_hz):
        sel = (freq > t - TOOTH_WINDOW_HZ) & (freq < t + TOOTH_WINDOW_HZ)
        if sel.any():
            teeth.append({"frequency_hz": round(t, 1), "excess_db": round(float(excess[sel].max()), 2)})
    lines = []
    for i in range(max(edge, 5), min(FFT_SIZE - edge, FFT_SIZE - 5)):
        if excess[i] > LINE_THRESHOLD_DB and excess[i] == excess[i - 5:i + 6].max():
            lines.append({"frequency_hz": round(float(freq[i]), 1), "excess_db": round(float(excess[i]), 2)})
    tooth_max = max((t["excess_db"] for t in teeth), default=0.0)
    tooth_sum = sum(t["excess_db"] for t in teeth)
    if tooth_max >= COMB_PRESENT_DB or len(lines) >= LINES_MANY:
        verdict, verdict_text = "NOISY", "comb and/or many lines present: check the ferrites, the harness and what is near the feed"
    elif tooth_max >= COMB_FAINT_DB or lines:
        verdict, verdict_text = "FAINT", "faint comb or a few lines: acceptable, but compare with the last clean check"
    else:
        verdict, verdict_text = "CLEAN", "no comb and no narrow lines over the floor"
    return {"verdict": verdict, "verdict_text": verdict_text,
            "tooth_max_db": round(tooth_max, 2), "tooth_sum_db": round(tooth_sum, 1), "teeth": teeth,
            "lines_over_threshold": len(lines), "lines": lines[:60], "line_threshold_db": LINE_THRESHOLD_DB,
            "floor_db": round(float(np.median(db[inner])), 2), **adc_level_summary(iq),
            "spectrum": {"frequency_hz": [round(float(f), 1) for f in freq], "excess_db": [round(float(e), 2) for e in excess],
                         "power_db": [round(float(d), 2) for d in db], "fft_size": FFT_SIZE, "segments": n_seg}}


# ------------------------------------------------------------------ acquisition
async def _capture(output_path: Path, gain_db: float, seconds: float, host: str, port: int) -> Dict[str, Any]:
    import sdr_tuning
    from sdr_capture import SDRCapture
    operating = sdr_tuning.operating_config()
    sdr = SDRCapture("network", host, port, verbose=False)
    try:
        await sdr.connect()
        tuning = await sdr_tuning.tune_explicitly(sdr, operating["center_frequency_hz"], operating["sample_rate_hz"],
                                                  gain_db, operating=operating)
        await sdr.flush_buffer()
        await sdr.capture(seconds, str(output_path), int(operating["sample_rate_hz"]),
                          {**sdr_tuning.tuning_attrs(tuning), "gain_requested_db": gain_db, "purpose": "local_rfi_check"})
        return {"tuning": tuning, "center_frequency_hz": operating["center_frequency_hz"],
                "sample_rate_hz": operating["sample_rate_hz"]}
    finally:
        await sdr.close()


def run_check(gain_db: float = DEFAULT_GAIN_DB, seconds: float = DEFAULT_SECONDS, host: str = "127.0.0.1",
              port: int = 1234, out_dir: Path = CHECK_DIR) -> Dict[str, Any]:
    """Capture, analyse and record one check. The caller makes sure nothing else holds MAIN."""
    from calibration_engine.acquisition import read_capture_iq
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    h5 = out_dir / f"rfi_check_{stamp}.h5"
    nanovna = nanovna_present()
    acq = asyncio.run(_capture(h5, gain_db, seconds, host, port))
    iq, _ = read_capture_iq(str(h5))
    result = {"utc": datetime.now(timezone.utc).isoformat(), "gain_db": gain_db, "capture_seconds": seconds,
              "center_frequency_hz": acq["center_frequency_hz"], "sample_rate_hz": acq["sample_rate_hz"],
              "hdf5_path": str(h5), "nanovna": nanovna,
              **analyze(np.asarray(iq), acq["sample_rate_hz"], acq["center_frequency_hz"])}
    (out_dir / f"rfi_check_{stamp}.json").write_text(json.dumps(result))
    tmp = out_dir / "last_check.json.tmp"
    tmp.write_text(json.dumps(result))
    tmp.replace(out_dir / "last_check.json")
    return result


def last_check(out_dir: Path = CHECK_DIR) -> Optional[Dict[str, Any]]:
    try:
        return json.loads((out_dir / "last_check.json").read_text())
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--gain", type=float, default=DEFAULT_GAIN_DB)
    p.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    a = p.parse_args(argv)
    r = run_check(a.gain, a.seconds)
    print(f"{r['verdict']}: {r['verdict_text']}")
    print(f"teeth max {r['tooth_max_db']:+.1f} dB, sum {r['tooth_sum_db']:+.0f} dB, lines {r['lines_over_threshold']}, "
          f"rms {r['adc_rms_codes']} codes, rails {r['adc_rail_hit_fraction']:.2%}")
    if r["nanovna"]["present"]:
        print("WARNING: " + r["nanovna"]["detail"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
