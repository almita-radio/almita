#!/usr/bin/env python3
"""ALMITA AUDIO idle reader (operator 2026-11-17: "una función no científica ... poder escuchar la subida de volumen
cuando estemos transitando por un área que tenga más hidrógeno"). NOT a measurement: nothing is kept.

The header's AUDIO button starts this user unit; the browser turns spectra into sound (console/sdr_audio.js). While
NOTHING else uses the SDR, every --every seconds it connects to rtl_tcp, tunes explicitly (operating frequency, sample
rate and observing gain, RTL AGC off - the same transit_logger.measure the TRANSIT logger uses), takes --seconds of IQ,
lets go of the SDR again and writes the mean spectrum (2048 bins over the whole band) to
data/runtime/sdr_audio_latest.json.

Priority is always the other user's: while a capture.py / fswitch.py / web job (ALIGN, CALIBRATE, LOBE...) / the TRANSIT
logger runs, it does not touch the SDR and only writes status "sdr_busy" - during a capture the browser plays the
capture's own spectra (main_rfi_mask_latest.json) instead, so it costs the capture nothing. Between samples rtl_tcp is
free; a capture that connects while a sample is being taken waits in rtl_tcp's queue for at most ~--seconds + 1 s.

It stops by itself when no page has sent a heartbeat for HEARTBEAT_TIMEOUT_S (a closed tab must not leave the SDR
sampled all night). Stop with SIGINT (the unit's KillSignal).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / "data" / "runtime"
LATEST_NAME = "sdr_audio_latest.json"
HEARTBEAT_NAME = "sdr_audio_heartbeat.json"
HEARTBEAT_TIMEOUT_S = 60.0
OUT_BINS = 2048                      # the capture's spectrum (main_rfi_mask_latest.json) has the same 2048 bins
EDGE_FRACTION = 0.05                 # band edges (RTL roll-off) flagged out of "inner", as rfi_mask does
DC_HALF_HZ = 3000.0                  # the DC spike at the tuning centre: blanked
UNIT = "almita-sdr-audio.service"
OTHER_SAMPLERS = ("transit_logger.py",)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, default=float))
    tmp.replace(path)


def reduce_spectrum(spec: np.ndarray, center_hz: float, sample_rate_hz: float, out_bins: int = OUT_BINS) -> Dict[str, Any]:
    """Linear fftshifted spectrum (any multiple of out_bins) -> out_bins bins in dB, the frequency axis, the "inner"
    flags (edges out) and the DC bins blanked (None)."""
    spec = np.asarray(spec, float)
    k = len(spec) // out_bins
    if k < 1 or len(spec) % out_bins:
        raise ValueError(f"spectrum of {len(spec)} bins cannot be reduced to {out_bins}")
    p = spec.reshape(out_bins, k).mean(axis=1)
    n = len(spec)                                   # input bin j is center + (j - n/2) * fs / n (transit_logger)
    f = center_hz + (np.arange(out_bins) * k + (k - 1) / 2 - n / 2) * sample_rate_hz / n
    with np.errstate(divide="ignore", invalid="ignore"):
        db = 10 * np.log10(p)
    db[~np.isfinite(db) | (np.abs(f - center_hz) < DC_HALF_HZ)] = np.nan
    edge = int(round(out_bins * EDGE_FRACTION))
    inner = np.zeros(out_bins, bool)
    inner[edge:out_bins - edge] = True
    return {"frequency_hz": [round(float(x), 1) for x in f],
            "power_db": [None if not np.isfinite(x) else round(float(x), 3) for x in db],
            "inner": [bool(x) for x in inner]}


def other_sampler_running() -> Optional[str]:
    me = os.getpid()
    for name in OTHER_SAMPLERS:
        r = subprocess.run(["pgrep", "-f", f"python[0-9.]* .*{name}"], capture_output=True, text=True)
        pids = [int(p) for p in (r.stdout or "").split()]
        if any(p != me for p in pids):
            return name
    return None


def sdr_in_use() -> Optional[str]:
    """Who else holds the SDR (the transit logger's rule: capture.py, fswitch.py, web jobs...) or another sampler."""
    import transit_logger
    return transit_logger.sdr_busy() or other_sampler_running()


def heartbeat_age_s(runtime_dir: Optional[Path] = None, now: Optional[float] = None) -> float:
    try:
        t = float(json.loads(((runtime_dir or RUNTIME_DIR) / HEARTBEAT_NAME).read_text())["epoch"])
    except (OSError, ValueError, KeyError, TypeError):
        return float("inf")
    return (time.time() if now is None else now) - t


def touch_heartbeat(runtime_dir: Optional[Path] = None) -> Dict[str, Any]:
    doc = {"epoch": time.time(), "utc": utcnow()}
    write_json((runtime_dir or RUNTIME_DIR) / HEARTBEAT_NAME, doc)
    return doc


# ------------------------------------------------------------------ the unit, from the web (AUDIO button)
def _systemctl(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)


def service_state() -> Dict[str, Any]:
    try:
        r = _systemctl("is-active", UNIT, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"unit": UNIT, "active": None, "state": "unknown", "error": str(exc)}
    state = (r.stdout or "").strip() or "unknown"
    return {"unit": UNIT, "active": state == "active", "state": state}


def service_action(action: str) -> Dict[str, Any]:
    """AUDIO ON / OFF. ON writes the heartbeat first (the reader would otherwise stop at once); OFF is the unit's
    KillSignal=SIGINT: a sample in progress ends and the SDR is let go."""
    if action not in ("start", "stop"):
        raise ValueError("action must be start or stop")
    if action == "start":
        touch_heartbeat()
    try:
        r = _systemctl(action, UNIT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"systemctl --user {action} {UNIT} did not answer in 60 s")
    if r.returncode != 0:
        raise RuntimeError(f"systemctl --user {action} {UNIT} failed: {(r.stderr or r.stdout).strip()[:300]} "
                           "(installed by scripts/install_notebook.sh)")
    return {"action": action, **service_state()}


# ------------------------------------------------------------------ the loop
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--every", type=float, default=2.0, help="seconds between samples (default 2)")
    ap.add_argument("--seconds", type=float, default=0.5, help="IQ per sample, seconds (default 0.5)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1234)
    ap.add_argument("--runtime-dir", type=Path, default=None, help="default data/runtime")
    a = ap.parse_args(argv)
    a.runtime_dir = a.runtime_dir or RUNTIME_DIR

    import sdr_tuning
    from transit_logger import measure
    gain = float(sdr_tuning.operating_config()["gain_db"])
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    print(f"AUDIO idle reader: {a.seconds:g} s of IQ every {a.every:g} s while nothing else uses the SDR, gain {gain:g} dB, "
          f"rtl_tcp {a.host}:{a.port}", flush=True)
    latest = a.runtime_dir / LATEST_NAME
    n_ok = 0
    while not stop["now"]:
        tick = time.monotonic()
        age = heartbeat_age_s(a.runtime_dir)
        if age > HEARTBEAT_TIMEOUT_S:
            print(f"no page listening for {age:.0f} s: stopping", flush=True)
            break
        doc: Dict[str, Any] = {"schema": "almita.sdr_audio.v1", "updated_utc": utcnow(), "source": "idle", "pid": os.getpid()}
        busy = sdr_in_use()
        if busy:
            doc.update(status="sdr_busy", busy=busy)
        else:
            try:
                m = asyncio.run(asyncio.wait_for(measure(a.host, a.port, gain, a.seconds), timeout=a.seconds + 20))
                doc.update(status="ok", gain_db=m["gain_db"], center_frequency_hz=m["center_frequency_hz"],
                           clipping_fraction=m["clipping_fraction"], seconds=a.seconds,
                           spectrum=reduce_spectrum(m["_spectrum"], float(m["center_frequency_hz"]), float(m["sample_rate_hz"])))
                n_ok += 1
            except Exception as exc:  # noqa: BLE001 - one bad sample must not stop the reader
                doc.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
        write_json(latest, doc)
        if doc["status"] != "ok" or n_ok % 30 == 1:
            print(f"{doc['updated_utc']}  {doc['status']:9s} {doc.get('busy') or doc.get('error') or ''}", flush=True)
        while not stop["now"] and time.monotonic() - tick < a.every:
            time.sleep(0.1)
    print(f"AUDIO idle reader stopped ({n_ok} spectra)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
