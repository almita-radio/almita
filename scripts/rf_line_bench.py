#!/usr/bin/env python3
"""ALMITA RF LINE BENCH (BL-071, and BL-068's ADC level): where do the narrow local lines come from?

INSTRUMENT DIAGNOSTIC, NOT ASTRONOMY. Valid indoors. Never moves the mount: this tool does not import capture.py,
INDI or any mount module. It changes ONE physical thing per step and measures, in each short capture:
  - MAIN's line near 1419.5 MHz (continuous, wandering; present in the 2026-10-14 50 ohm reference),
  - RFI_REF's intermittent comb (1419.642, 1419.998, 1420.110, 1420.710, 1421.067 MHz ...) and its fixed 1420.035 line,
  - the raw ADC spread (std in codes), for BL-068.

  plan                         the steps, what to connect for each, and the Bias-T rule. Touches nothing.
  capture --step S --yes       one capture for step S (MAIN through rtl_tcp.service on :1234, or with --main-private through
                               a private rtl_tcp WITHOUT -T; RFI_REF through a private rtl_tcp on :1236 that this tool
                               starts and stops). --simulate writes synthetic data.
  watch [--main-private]       live: the moving line's frequency/strength every 5 s, to hunt the source by unplugging things
  analyze [--session DIR]      metrics per capture, a comparison table, analysis.json and a PNG.

Bias-T rule (operator, 2026-10-14): a 50 ohm terminator that is NOT behind an LNA takes the Bias-T DC. Such a step is
refused while the receiver's rtl_tcp runs with -T unless --dc-block-confirmed (a DC block is in line). MAIN's service
always runs with -T; RFI_REF's private rtl_tcp gets -T only when the step needs it (antenna B's in-line LNA).
Refused too while an observation, a wizard or a gain pilot is running (they own the receivers).
Output: data/bench/rf_lines/<session>/ (never data/mosaic, data/calibration or REDUCE paths).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

OUT_ROOT = REPO / "data" / "bench" / "rf_lines"
MAIN_PORT, RFI_PORT = 1234, 1236          # 1236: not the sidecar's 1235, so a stale sidecar can never be confused with us
RFI_SERIAL = "00000002"
MAIN_SERIAL = "00000001"
SAMPLE_RATE = 2_400_000
NFFT = 256                                # QUICKLOOK / RFI_REF waterfall resolution (9.375 kHz), so numbers compare
MAIN_LINE_WINDOW_MHZ = (1419.25, 1419.95)   # the line wanders (1419.30-1419.93 seen); tracked in 10 s blocks
TRACK_BLOCK_SECONDS = 10
RFI_COMB_MHZ = (1419.642, 1419.998, 1420.110, 1420.354, 1420.523, 1420.710, 1420.945, 1421.067, 1421.357, 1421.423)
RFI_FIXED_MHZ = 1420.035
BUSY_SCRIPTS = ("capture.py", "calibrate_reference_wizard.py", "observation_gain_pilot.py", "observation_orchestrator.py")

# One physical change per step. load_not_behind_lna: the terminator ends the coax that carries the Bias-T feed.
STEPS: Dict[str, Dict[str, Any]] = {
    "M1": {"receiver": "main", "load_not_behind_lna": True,
           "setup": "50 ohm terminator DIRECTLY on the MAIN SDR (no LNA, no filter, no cable) - with a DC BLOCK in line"},
    "M2": {"receiver": "main", "load_not_behind_lna": False,
           "setup": "50 ohm terminator at the LNA INPUT, full chain (the wizard's AMBIENT_50R) - baseline"},
    "M3": {"receiver": "main", "load_not_behind_lna": False,
           "setup": "as M2, with the RFI_REF dongle UNPLUGGED from USB"},
    "M4": {"receiver": "main", "load_not_behind_lna": False, "disk_load": True,
           "setup": "as M2, while this tool writes to the disk (USB/storage activity)"},
    "M5": {"receiver": "main", "load_not_behind_lna": False,
           "setup": "antenna connected to the LNA, mount still"},
    "M6": {"receiver": "main", "load_not_behind_lna": True,
           "setup": "50 ohm at the END of MAIN's coax (cable to the SDR as usual, LNA/filter removed) - DC block or --main-private"},
    "M7": {"receiver": "main", "load_not_behind_lna": True,
           "setup": "antenna DIRECTLY to the MAIN SDR, no LNA/filter - Bias-T OFF (--main-private) so no DC reaches the antenna"},
    "R0": {"receiver": "rfi", "load_not_behind_lna": False, "bias_t": True,
           "setup": "antenna B as in the sessions (Bias-T ON powers its in-line LNA) - baseline"},
    "R1": {"receiver": "rfi", "load_not_behind_lna": True, "bias_t": False,
           "setup": "50 ohm terminator DIRECTLY on the RFI_REF dongle (Bias-T OFF by default)"},
    "R2": {"receiver": "rfi", "load_not_behind_lna": False, "bias_t": True,
           "setup": "antenna B, RFI_REF dongle on ANOTHER USB port and ANOTHER cable"},
    "R3": {"receiver": "rfi", "load_not_behind_lna": False, "bias_t": True, "disk_load": True,
           "setup": "as R0, while this tool writes to the disk"},
    "R4": {"receiver": "rfi", "load_not_behind_lna": False, "bias_t": True, "needs_main_stopped": True,
           "setup": "as R0, with MAIN's rtl_tcp.service STOPPED by the operator (this tool never stops services)"},
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ gates
def busy_processes(proc: Path = Path("/proc")) -> List[str]:
    from runtime_state import is_script_cmdline
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        found += [f"{s} (pid {entry.name})" for s in BUSY_SCRIPTS if is_script_cmdline(raw, s)]
    return found


def port_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def main_bias_t_flag() -> Optional[bool]:
    from calibration_engine.reference_wizard import bias_t_known_facts
    return bias_t_known_facts(port=MAIN_PORT).get("flag_present_in_service_command_line")


def capture_refusal(step: str, *, dc_block_confirmed: bool, rfi_bias_t: Optional[bool], simulate: bool,
                    busy: Optional[List[str]] = None, main_bias: Optional[bool] = None,
                    main_listening: Optional[bool] = None, main_private: bool = False) -> Optional[str]:
    """None when step may be captured now; otherwise why not. Pure (inputs injected) so it is testable."""
    spec = STEPS[step]
    if not simulate and busy:
        return f"receivers in use by {', '.join(busy)} - wait until it ends"
    if spec["receiver"] == "main" and main_private:
        # MAIN through this tool's own rtl_tcp WITHOUT -T: the service must be stopped (it holds the dongle and -T)
        if not simulate and main_listening:
            return "--main-private: MAIN's rtl_tcp.service (:1234) is still running - stop it first (it holds the dongle and the Bias-T)"
        return None
    if spec["receiver"] == "main":
        bias = main_bias
        if spec["load_not_behind_lna"] and bias is not False and not dc_block_confirmed:
            return (f"{step}: terminator not behind the LNA and MAIN's rtl_tcp "
                    f"{'runs with -T' if bias else 'Bias-T state unknown'} - put a DC block in line and pass "
                    "--dc-block-confirmed")
        if not simulate and main_listening is False:
            return "MAIN rtl_tcp (:1234) is not listening"
    else:
        bias = spec["bias_t"] if rfi_bias_t is None else rfi_bias_t
        if spec["load_not_behind_lna"] and bias and not dc_block_confirmed:
            return f"{step}: terminator on the RFI_REF dongle with Bias-T ON - use --rfi-bias-t off or --dc-block-confirmed"
        if spec.get("needs_main_stopped") and not simulate and main_listening:
            return f"{step}: MAIN's rtl_tcp (:1234) is still running - the operator stops it first"
    return None


# ------------------------------------------------------------------ capture
class DiskLoad:
    """Bounded disk activity during a capture: 8 MB fsync'd chunks to one temp file, removed afterwards."""

    def __init__(self, directory: Path, max_bytes: int = 400 * 1024 * 1024):
        self.path = directory / ".disk_load.tmp"
        self.max_bytes = max_bytes
        self.written = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        chunk = os.urandom(8 * 1024 * 1024)
        with open(self.path, "wb") as fh:
            while not self._stop.is_set() and self.written < self.max_bytes:
                fh.write(chunk); fh.flush(); os.fsync(fh.fileno()); self.written += len(chunk)
                if self.written >= self.max_bytes:
                    fh.seek(0); self.written = 0          # keep writing, never grow past max_bytes

    def __enter__(self):
        self._thread.start(); return self

    def __exit__(self, *exc):
        self._stop.set(); self._thread.join(timeout=30); self.path.unlink(missing_ok=True)


def synthetic_iq(seconds: float, step: str, seed: int = 0) -> np.ndarray:
    """Synthetic interleaved uint8 IQ: noise at ~0.63 codes, MAIN line near 1419.5, RFI comb in 10 % bursts."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    z = rng.normal(0, 0.6, n) + 1j * rng.normal(0, 0.6, n)
    import sdr_tuning
    fc = float(sdr_tuning.operating_center_frequency_hz())
    if STEPS[step]["receiver"] == "main" and step != "M3":
        z += 0.25 * np.exp(2j * np.pi * (1419.50e6 - fc) * t)
    if STEPS[step]["receiver"] == "rfi":
        burst = (np.floor(t) % 10) == 3
        for f in RFI_COMB_MHZ:
            z += burst * 1.2 * np.exp(2j * np.pi * (f * 1e6 - fc) * t)
        z += 3.0 * np.exp(2j * np.pi * (RFI_FIXED_MHZ * 1e6 - fc) * t)
    iq = np.empty(2 * n, dtype=np.uint8)
    iq[0::2] = np.clip(np.round(z.real + 127.4), 0, 255)
    iq[1::2] = np.clip(np.round(z.imag + 127.4), 0, 255)
    return iq


async def _capture_main(path: Path, seconds: float, gain_db: float, meta: Dict[str, Any]) -> Dict[str, Any]:
    import sdr_tuning
    from sdr_capture import SDRCapture
    sdr = SDRCapture("network", "localhost", MAIN_PORT, verbose=False)
    await sdr.connect()
    try:
        record = await sdr_tuning.tune_explicitly(sdr, sdr_tuning.operating_center_frequency_hz(), SAMPLE_RATE, gain_db,
                                                  operating=sdr_tuning.operating_config())
        await sdr.flush_buffer()
        await sdr.capture(duration=seconds, output_file=str(path), sample_rate=SAMPLE_RATE,
                          metadata={**meta, **sdr_tuning.tuning_attrs(record)})
        return record
    finally:
        await sdr.close()


async def _capture_private(path: Path, seconds: float, gain_db: float, bias_t: bool, meta: Dict[str, Any],
                           serial: str = RFI_SERIAL) -> Dict[str, Any]:
    import sdr_tuning
    from sdr_capture import SDRCapture
    if port_listening(RFI_PORT):
        raise RuntimeError(f"port {RFI_PORT} already in use - refusing to touch it")
    log = path.with_suffix(".rtl_tcp.log")
    fc = sdr_tuning.operating_center_frequency_hz()
    cmd = ([shutil.which("stdbuf"), "-oL"] if shutil.which("stdbuf") else []) + [
        "rtl_tcp", "-d", serial, "-a", "127.0.0.1", "-p", str(RFI_PORT), "-f", str(fc), "-s", str(SAMPLE_RATE),
        "-g", str(gain_db)] + (["-T"] if bias_t else [])
    with open(log, "a", buffering=1) as fh:
        fh.write(f"=== rf_line_bench rtl_tcp launch utc={utcnow()} bias_tee={bias_t} cmd={' '.join(cmd)} ===\n")
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 10
            while not port_listening(RFI_PORT):
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"private rtl_tcp for {serial} did not start (see {log})")
                await asyncio.sleep(0.2)
            sdr = SDRCapture("network", "127.0.0.1", RFI_PORT, verbose=False)
            await sdr.connect()
            try:
                record = await sdr_tuning.tune_explicitly(sdr, fc, SAMPLE_RATE, gain_db,
                                                          ack_source=sdr_tuning.LogFileAckSource(log))
                await sdr.flush_buffer()
                await sdr.capture(duration=seconds, output_file=str(path), sample_rate=SAMPLE_RATE,
                                  metadata={**meta, **sdr_tuning.tuning_attrs(record)})
                return record
            finally:
                await sdr.close()
        finally:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()


def cmd_capture(args) -> int:
    spec = STEPS[args.step]
    rfi_bias = None if args.rfi_bias_t is None else args.rfi_bias_t == "on"
    if not args.yes and not args.simulate:
        print("refusing: a real capture needs --yes (it opens the receiver; it never moves the mount)"); return 2
    refusal = capture_refusal(
        args.step, dc_block_confirmed=args.dc_block_confirmed, rfi_bias_t=rfi_bias, simulate=args.simulate,
        busy=[] if args.simulate else busy_processes(),
        main_bias=True if args.simulate else main_bias_t_flag(),
        main_listening=None if args.simulate else port_listening(MAIN_PORT), main_private=args.main_private)
    if refusal:
        print(f"refusing: {refusal}"); return 2
    session = Path(args.session) if args.session else OUT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%d")
    session.mkdir(parents=True, exist_ok=True)
    n = len(list(session.glob(f"{args.step}_*.h5")))
    path = session / f"{args.step}_{n:02d}.h5"
    gain = args.gain if args.gain is not None else (40.2 if spec["receiver"] == "main" else 25.0)
    bias = ((not args.main_private) if spec["receiver"] == "main" else (spec["bias_t"] if rfi_bias is None else rfi_bias))
    meta = {"bench": "rf_line_bench", "step": args.step, "setup": spec["setup"], "receiver": spec["receiver"],
            "bias_t_requested": bias, "dc_block_confirmed": bool(args.dc_block_confirmed),
            "main_private_rtl_tcp_without_bias_t": bool(args.main_private),
            "disk_load": bool(spec.get("disk_load")), "note": args.note or "", "simulated": bool(args.simulate),
            "not_science": True}
    print(f"{args.step}: {spec['setup']}\n   {args.seconds:g} s, gain {gain} dB, Bias-T {'ON' if bias else 'OFF'} -> {path}")
    started = utcnow()
    load_ctx = DiskLoad(session) if spec.get("disk_load") else None
    try:
        if load_ctx:
            load_ctx.__enter__()
        if args.simulate:
            import sdr_tuning
            from sdr_capture import write_hdf5_atomic
            iq = synthetic_iq(args.seconds, args.step, seed=n)
            write_hdf5_atomic(str(path), iq, {**meta, "center_frequency_hz": sdr_tuning.operating_center_frequency_hz(), "sample_rate_hz": SAMPLE_RATE,
                                               "num_samples": len(iq) // 2, "capture_status": "complete",
                                               "final_filename": path.name, "created_at": utcnow()},
                              expected_samples=len(iq) // 2)
            tuning = {"evidence": "SIMULATED"}
        elif spec["receiver"] == "main" and args.main_private:
            tuning = asyncio.run(_capture_private(path, args.seconds, gain, False, meta, serial=MAIN_SERIAL))
        elif spec["receiver"] == "main":
            tuning = asyncio.run(_capture_main(path, args.seconds, gain, meta))
        else:
            tuning = asyncio.run(_capture_private(path, args.seconds, gain, bias, meta))
    finally:
        if load_ctx:
            load_ctx.__exit__(None, None, None)
    manifest = session / "manifest.json"
    entries = json.loads(manifest.read_text()) if manifest.exists() else []
    entries.append({**meta, "file": path.name, "seconds": args.seconds, "gain_db": gain, "started_utc": started,
                    "ended_utc": utcnow(), "tuning_evidence": tuning.get("evidence")})
    manifest.write_text(json.dumps(entries, indent=2))
    print(f"   saved. Next: analyze --session {session}")
    return 0


# ------------------------------------------------------------------ analysis
def _running_median(a: np.ndarray, k: int = 15) -> np.ndarray:
    p = k // 2
    padded = np.pad(a, (p, p), mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, k), axis=1)


def analyze_iq(iq, center_hz: Optional[float] = None) -> Dict[str, Any]:
    """Metrics of one capture: mean spectrum lines (excess over a 15-bin running median), per-second burst
    fraction of the RFI comb, MAIN's line near 1419.5 and the raw ADC spread. iq: interleaved uint8, an array or an
    h5py dataset - read one second at a time (a 60 s capture is 288 MB of samples; this Pi has ~0 free swap)."""
    if center_hz is None:
        import sdr_tuning
        center_hz = float(sdr_tuning.operating_center_frequency_hz())
    block = 2 * SAMPLE_RATE                                  # one second of interleaved samples
    frames = SAMPLE_RATE // NFFT
    window = np.hanning(NFFT)
    sec_spectra, s1, s2, count = [], 0.0, 0.0, 0
    for start in range(0, len(iq) - block + 1, block):
        x = np.asarray(iq[start:start + block]).astype(np.float32)
        i_, q_ = x[0::2], x[1::2]
        s1 += float(i_.sum()); s2 += float((i_ * i_).sum()); count += len(i_)
        z = (i_ - 127.4) + 1j * (q_ - 127.4)
        sp = np.abs(np.fft.fftshift(np.fft.fft(z[:frames * NFFT].reshape(frames, NFFT) * window, axis=1), axes=1)) ** 2
        sec_spectra.append(sp.mean(axis=0))
    if not sec_spectra:
        raise ValueError("capture shorter than one second")
    sec = np.array(sec_spectra)
    std_codes = float(np.sqrt(max(0.0, s2 / count - (s1 / count) ** 2)))
    freqs = (center_hz + (np.arange(NFFT) - NFFT // 2) * SAMPLE_RATE / NFFT) / 1e6
    mean_db = 10 * np.log10(sec.mean(axis=0) + 1e-20)
    excess = mean_db - _running_median(mean_db)
    sec_db = 10 * np.log10(sec + 1e-20)
    sec_excess = sec_db - np.array([_running_median(r) for r in sec_db])

    def at(f): return int(np.argmin(np.abs(freqs - f)))
    w = np.where((freqs >= MAIN_LINE_WINDOW_MHZ[0]) & (freqs <= MAIN_LINE_WINDOW_MHZ[1]))[0]
    # A drifting line smears out of a whole-capture mean (2026-10-14: +0.45 dB per 10 s block read as +0.10 over
    # 60 s while it moved 40 kHz), so it is followed block by block and reported as the median of the block peaks.
    nb = max(1, len(sec) // TRACK_BLOCK_SECONDS)
    track = []
    for k in range(nb):
        bdb = 10 * np.log10(sec[k * TRACK_BLOCK_SECONDS:(k + 1) * TRACK_BLOCK_SECONDS].mean(axis=0) + 1e-20)
        bex = bdb - _running_median(bdb)
        j = w[np.argmax(bex[w])]
        track.append((float(freqs[j]), float(bex[j])))
    tf, te = np.array(track).T
    comb = {f"{f:.3f}": {"mean_excess_db": round(float(excess[at(f)]), 2),
                         "burst_fraction": round(float((sec_excess[:, at(f)] > 3).mean()), 3)} for f in RFI_COMB_MHZ}
    strongest = np.argsort(excess)[::-1][:6]
    return {
        "adc_std_codes": round(std_codes, 3),
        "main_line": {"freq_mhz": round(float(np.median(tf)), 4), "excess_db": round(float(np.median(te)), 2),
                      "freq_min_mhz": round(float(tf.min()), 4), "freq_max_mhz": round(float(tf.max()), 4),
                      "blocks": [[round(f, 4), round(e, 2)] for f, e in track]},
        "rfi_fixed_1420_035": round(float(excess[at(RFI_FIXED_MHZ)]), 2),
        "rfi_comb": comb,
        "rfi_comb_burst_fraction_max": max(v["burst_fraction"] for v in comb.values()),
        "strongest_lines": [{"freq_mhz": round(float(freqs[k]), 4), "excess_db": round(float(excess[k]), 2)} for k in strongest],
        "seconds_analyzed": len(sec),
        "_freqs": freqs, "_excess": excess,
    }


def cmd_analyze(args) -> int:
    import h5py
    session = Path(args.session) if args.session else max(OUT_ROOT.glob("*"), key=lambda p: p.stat().st_mtime)
    entries = json.loads((session / "manifest.json").read_text())
    results = []
    for e in entries:
        with h5py.File(session / e["file"], "r") as h:
            r = analyze_iq(h["iq_data"])
        results.append({**{k: e[k] for k in ("step", "file", "receiver", "setup", "bias_t_requested", "dc_block_confirmed",
                                             "gain_db", "started_utc", "simulated")}, **r})
    print(f"RF LINE BENCH - {session}  (instrument diagnostic, not science)\n")
    print(f"{'step':5} {'file':10} {'rx':4} {'gain':>5} {'ADC std':>7} | {'MAIN moving line (10 s blocks)':>37} | {'comb burst %':>12} {'1419.642':>9} {'1420.035':>9}")
    for r in results:
        ml = r["main_line"]
        print(f"{r['step']:5} {r['file'][:10]:10} {r['receiver']:4} {r['gain_db']:5.1f} {r['adc_std_codes']:7.3f} | "
              f"{ml['freq_mhz']:.4f} {ml['excess_db']:+6.2f} dB ({ml['freq_min_mhz']:.3f}-{ml['freq_max_mhz']:.3f}) | "
              f"{100 * r['rfi_comb_burst_fraction_max']:11.0f}% "
              f"{r['rfi_comb']['1419.642']['mean_excess_db']:+8.2f} {r['rfi_fixed_1420_035']:+8.2f}")
    print("\nReading: MAIN line = median of the 10 s block peaks in 1419.25-1419.95 MHz; > ~+0.2 dB = present. Comb burst % = share of 1 s blocks where a comb line is >3 dB.")
    out = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
    (session / "analysis.json").write_text(json.dumps({"session": str(session), "analyzed_utc": utcnow(), "captures": out}, indent=2))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2, 1, figsize=(12, 8), constrained_layout=True)
        for ax, rx in zip(axes, ("main", "rfi")):
            for r in (r for r in results if r["receiver"] == rx):
                ax.plot(r["_freqs"], r["_excess"], lw=0.9, label=f"{r['step']} {r['file']}")
            ax.set_title(f"{rx.upper()} - excess over local median (dB)"); ax.set_xlabel("MHz")
            if ax.lines:
                ax.legend(fontsize=7)
            ax.axvspan(*MAIN_LINE_WINDOW_MHZ, color="0.85", zorder=0)
        fig.savefig(session / "analysis.png", dpi=90)
    except Exception as exc:  # noqa: BLE001 - the table and json are the result; the plot is a convenience
        print(f"(plot skipped: {exc})")
    print(f"\nwritten: {session / 'analysis.json'}, {session / 'analysis.png'}")
    return 0


def _reading(raw: bytes, center_hz: float, window=None, sample_rate: int = SAMPLE_RATE, spectrum_out=None) -> tuple:
    """(peak MHz, excess dB) of the moving line in one block of raw interleaved uint8 IQ."""
    x = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    z = (x[0::2] - 127.4) + 1j * (x[1::2] - 127.4)
    m = len(z) // NFFT
    sp = (np.abs(np.fft.fftshift(np.fft.fft(z[:m * NFFT].reshape(m, NFFT) * np.hanning(NFFT), axis=1), axes=1)) ** 2).mean(axis=0)
    db = 10 * np.log10(sp + 1e-20)
    ex = db - _running_median(db)
    if spectrum_out is not None:
        spectrum_out.append(ex)
    freqs = (center_hz + (np.arange(NFFT) - NFFT // 2) * sample_rate / NFFT) / 1e6
    lo, hi = window or MAIN_LINE_WINDOW_MHZ
    w = np.where((freqs >= lo) & (freqs <= hi))[0]
    j = w[np.argmax(ex[w])]
    return float(freqs[j]), float(ex[j])


def cmd_watch(args) -> int:
    """Live: every --every seconds, the moving line's frequency and strength, so the operator can unplug devices one
    by one and see when it drops. Nothing is saved except a CSV log. MAIN only."""
    import sdr_tuning
    if busy_processes():
        print(f"refusing: receivers in use by {', '.join(busy_processes())}"); return 2
    if args.main_private and port_listening(MAIN_PORT):
        print("refusing: --main-private needs rtl_tcp.service stopped"); return 2
    if not args.main_private and main_bias_t_flag() is not False and not args.yes_bias_t_ok:
        print("note: MAIN's service runs with -T; the chain must be LNA-terminated (add --yes-bias-t-ok to confirm)"); return 2
    # --center-offset-khz retunes on purpose: a line from outside keeps its RF frequency, one made inside the receiver
    # (tuner spur, LO leaking out and reflected back) moves with the tuning
    fc = sdr_tuning.operating_center_frequency_hz() + args.center_offset_khz * 1e3
    # whole band minus the DC bin and the edges; or the moving line's usual window
    # --sample-rate changes how anything outside the band folds back in: a line made at a fixed frequency inside the
    # receiver keeps its offset, one folded in from outside lands somewhere else
    sr = int(args.sample_rate)
    half = 1.155 * sr / SAMPLE_RATE
    window = (fc / 1e6 - half, fc / 1e6 + half) if args.full_band else MAIN_LINE_WINDOW_MHZ
    proc = None
    port = RFI_PORT if args.main_private else MAIN_PORT
    if args.main_private:
        if port_listening(RFI_PORT):
            print(f"refusing: port {RFI_PORT} in use"); return 2
        proc = subprocess.Popen(["rtl_tcp", "-d", MAIN_SERIAL, "-a", "127.0.0.1", "-p", str(RFI_PORT), "-f", str(fc),
                                 "-s", str(sr), "-g", str(args.gain)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if port_listening(RFI_PORT):
                break
            time.sleep(0.2)
    log = OUT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%d") / f"watch_{datetime.now(timezone.utc):%H%M%S}.csv"
    log.parent.mkdir(parents=True, exist_ok=True)
    nbytes = int(2 * sr * min(args.every, 2.0))     # analyse up to 2 s of each interval (CPU), discard the rest
    end = time.monotonic() + args.duration
    print(f"WATCH MAIN (port {port}{', NO Bias-T' if args.main_private else ''}, center {fc / 1e6:.6f} MHz, {sr / 1e6:g} MS/s) - strongest narrow line in {window[0]}-{window[1]} MHz every {args.every:g} s; log {log}")
    print("hora UTC  | línea MHz | dB     |")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sk, open(log, "w") as fh, \
                open(log.with_suffix(".spectra.f32") if args.save_spectra else os.devnull, "wb") as sfh:
            fh.write("utc,freq_mhz,excess_db\n")
            sk.recv(12)                                       # rtl_tcp header
            if not args.main_private:                         # the service keeps its own tuning; set ours explicitly
                for cmd, val in ((1, fc), (2, sr), (3, 1), (4, int(round(args.gain * 10))), (8, 0)):   # 8, 0: RTL AGC off (BL-076)
                    sk.sendall(bytes([cmd]) + int(val).to_bytes(4, "big"))
            while time.monotonic() < end:
                t0 = time.monotonic()
                buf = bytearray()
                while len(buf) < nbytes:
                    chunk = sk.recv(min(1 << 20, nbytes - len(buf)))
                    if not chunk:
                        raise RuntimeError("rtl_tcp closed the stream")
                    buf += chunk
                spec = [] if args.save_spectra else None
                f, e = _reading(bytes(buf), fc, window, sr, spec)
                if spec:                                      # excess spectrum per reading, for lines the peak misses
                    sfh.write(np.asarray(spec[0], dtype=np.float32).tobytes()); sfh.flush()
                now = datetime.now(timezone.utc).strftime("%H:%M:%S")
                bar = "#" * max(0, int(round(e * 20)))
                print(f"{now}  | {f:.4f} | {e:+.2f}  | {bar}", flush=True)
                fh.write(f"{now},{f:.4f},{e:.3f}\n"); fh.flush()
                # drain the rest of the interval so readings stay current
                sk.settimeout(0.05)
                while time.monotonic() - t0 < args.every:
                    try:
                        if not sk.recv(1 << 20):
                            break
                    except socket.timeout:
                        pass
                sk.settimeout(5)
    except KeyboardInterrupt:
        pass
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


def cmd_plan(args) -> int:
    print(__doc__.split("\n\n")[0], "\n")
    for k, s in STEPS.items():
        flags = []
        if s["load_not_behind_lna"]:
            flags.append("Bias-T rule: DC block or Bias-T off")
        if s.get("disk_load"):
            flags.append("writes up to 400 MB to a temp file during the capture")
        if s.get("needs_main_stopped"):
            flags.append("operator stops rtl_tcp.service first")
        print(f"  {k}  [{s['receiver'].upper():4}] {s['setup']}" + (f"\n        ({'; '.join(flags)})" if flags else ""))
    print("\nSuggested order: M2 (baseline) -> M3 -> M1 -> M6 -> M4 -> M5, then R0 -> R1 -> R2 -> R3 (-> R4).")
    print("Before touching any connector: stop whatever powers the Bias-T feed (nothing of this tool runs between captures).")
    print("Each: python scripts/rf_line_bench.py capture --step M2 --seconds 60 --yes   then   analyze")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="ALMITA RF line bench (BL-071 / BL-068) - never moves the mount")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").set_defaults(func=cmd_plan)
    c = sub.add_parser("capture")
    c.add_argument("--step", required=True, choices=sorted(STEPS))
    c.add_argument("--seconds", type=float, default=60.0)
    c.add_argument("--gain", type=float, default=None, help="dB (default 40.2 MAIN / 25 RFI_REF); vary it for BL-068")
    c.add_argument("--rfi-bias-t", choices=("on", "off"), default=None, help="override the step's RFI_REF Bias-T")
    c.add_argument("--dc-block-confirmed", action="store_true", help="a DC block is in line before the terminator")
    c.add_argument("--main-private", action="store_true",
                   help="MAIN through this tool's own rtl_tcp WITHOUT -T (Bias-T off; no DC block needed). The operator "
                        "stops rtl_tcp.service first and starts it again afterwards")
    c.add_argument("--session", default=None, help="output dir (default data/bench/rf_lines/<UTC date>)")
    c.add_argument("--note", default=None)
    c.add_argument("--yes", action="store_true", help="really open the receiver")
    c.add_argument("--simulate", action="store_true", help="synthetic data, no hardware")
    c.set_defaults(func=cmd_capture)
    wt = sub.add_parser("watch")
    wt.add_argument("--duration", type=float, default=600.0, help="seconds (Ctrl-C stops earlier)")
    wt.add_argument("--every", type=float, default=5.0)
    wt.add_argument("--gain", type=float, default=40.2)
    wt.add_argument("--main-private", action="store_true", help="own rtl_tcp WITHOUT -T (service stopped)")
    wt.add_argument("--full-band", action="store_true", help="search the whole band, not only 1419.25-1419.95 MHz")
    wt.add_argument("--yes-bias-t-ok", action="store_true", help="through the service (-T): the chain is LNA-terminated")
    wt.add_argument("--save-spectra", action="store_true", help=f"also write each reading's {NFFT}-bin excess spectrum (float32) next to the CSV")
    wt.add_argument("--sample-rate", type=float, default=SAMPLE_RATE, help="samples/s (the service is left at it: restart it after)")
    wt.add_argument("--center-offset-khz", type=float, default=0.0, help="tune this far from the operating center")
    wt.set_defaults(func=cmd_watch)
    a = sub.add_parser("analyze")
    a.add_argument("--session", default=None)
    a.set_defaults(func=cmd_analyze)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
