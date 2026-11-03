#!/usr/bin/env python3
"""ALMITA TRANSIT logger (operator 2026-11-02): the antenna stands still, pointed at the ecliptic, and the Sun drifts
through the beam. One sample every --every seconds (10 by default): connect to rtl_tcp, tune explicitly (operating
frequency, sample rate and the observing gain, RTL AGC off - sdr_tuning.tune_explicitly), take --seconds of IQ, let go
of the SDR again. Band power with LOBE's metric (lobe_transit.band_power: running median over the bins, DC and band
edges out, so narrow lines do not leak into the continuum) and the Sun's alt/az at that instant.

It never moves the mount and never keeps the SDR: between samples rtl_tcp is free. While another SDR user runs (an
OBSERVE capture.py, a web job such as ALIGN/CALIBRATE/RFI check) the sample is skipped and logged as SDR_BUSY.

Output:
  data/transit/transit_<UTC date>.csv  one row per sample, appended as it happens (the record)
  data/runtime/transit_latest.json     the last WINDOW_MIN minutes, for the TRANSIT page (rewritten every sample)
The IQ itself is not kept (a 1 s capture is ~5 MB; one every 10 s would be ~40 GB a day).
Stop with SIGINT.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "transit"
RUNTIME_DIR = ROOT / "data" / "runtime"
LATEST_NAME = "transit_latest.json"
CLEARED_NAME = "transit_cleared.json"     # CLEAR on the page: the graphs start after this instant (the CSVs are kept)
WINDOW_MIN = 180                       # what the page shows at most
FIELDS = ("utc", "status", "power_db", "power_lin", "clipping_fraction", "adc_std_codes", "gain_db",
          "sun_alt_deg", "sun_az_deg", "pi_temp_c", "error")
SDR_PROCESSES = ("capture.py", "local_rfi_check.py", "lobe_transit.py")   # SDR users outside the web job list


def sdr_busy() -> Optional[str]:
    """Who else is using the SDR right now, or None."""
    for name in SDR_PROCESSES:
        r = subprocess.run(["pgrep", "-f", f"python[0-9.]* .*{name}"], capture_output=True, text=True)
        if r.returncode == 0:
            return name
    try:
        import almita_web_ops
        jobs = almita_web_ops.running_jobs()
    except Exception:  # noqa: BLE001 - no job list: rely on the process check above
        jobs = []
    return f"web job {jobs[0].get('stage')}" if jobs else None


def pi_temp_c() -> Optional[float]:
    """SoC temperature of the Pi the SDR hangs from: the capture Pi through its agent, else this machine."""
    import instrument_host
    if instrument_host.remote_host():
        t = instrument_host.agent_health(timeout=3.0).get("cpu_temp_c")
        return float(t) if t is not None else None
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def sun_altaz(when: datetime, location) -> Dict[str, float]:
    from astropy.coordinates import AltAz, get_sun
    from astropy.time import Time
    t = Time(when)
    aa = get_sun(t).transform_to(AltAz(obstime=t, location=location))
    return {"sun_alt_deg": round(float(aa.alt.deg), 3), "sun_az_deg": round(float(aa.az.deg), 3)}


async def measure(host: str, port: int, gain_db: float, seconds: float) -> Dict[str, Any]:
    import h5py
    import sdr_tuning
    from lobe_transit import band_power
    from sdr_capture import SDRCapture, read_hdf5_iq_components
    operating = sdr_tuning.operating_config()
    tmpdir = "/dev/shm" if os.path.isdir("/dev/shm") else None
    fd, path = tempfile.mkstemp(prefix="almita_transit_", suffix=".h5", dir=tmpdir)
    os.close(fd)
    sdr = SDRCapture("network", host, port, verbose=False)
    sdr.compact_console = True
    try:
        await sdr.connect()
        tuning = await sdr_tuning.tune_explicitly(sdr, operating["center_frequency_hz"], operating["sample_rate_hz"],
                                                  gain_db, operating=operating)
        if not str(tuning.get("rtl_agc", "")).startswith("off"):
            raise RuntimeError(f"RTL AGC not confirmed off: {tuning.get('rtl_agc')}")
        await sdr.flush_buffer()
        await sdr.capture(seconds, path, int(operating["sample_rate_hz"]),
                          {**sdr_tuning.tuning_attrs(tuning), "gain_requested_db": gain_db, "purpose": "transit_logger"})
        with h5py.File(path, "r") as f:
            i_, q_ = read_hdf5_iq_components(f)
        raw = np.empty(i_.size * 2, dtype=np.uint8)
        raw[0::2], raw[1::2] = i_, q_
        m = band_power(raw)
        return {"power_db": round(m["power_db"], 4), "power_lin": m["power_lin"],
                "clipping_fraction": m["clipping_fraction"], "adc_std_codes": round(m["adc_std_codes"], 3),
                "gain_db": tuning["applied"]["gain_db"]}
    finally:
        await sdr.close()
        try:
            os.unlink(path)
        except OSError:
            pass


UNIT = "almita-transit-logger.service"


def _systemctl(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)


def service_state() -> Dict[str, Any]:
    """The logger's user unit as systemd sees it (the TRANSIT page's START/STOP)."""
    try:
        r = _systemctl("is-active", UNIT, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"unit": UNIT, "active": None, "state": "unknown", "error": str(exc)}
    state = (r.stdout or "").strip() or "unknown"
    return {"unit": UNIT, "active": state == "active", "state": state}


def service_action(action: str) -> Dict[str, Any]:
    """START / STOP from the TRANSIT page (operator 2026-11-02: "no va a estar todo el rato graficando"). STOP is the
    unit's KillSignal=SIGINT: the sample in progress ends and the logger exits cleanly."""
    if action not in ("start", "stop"):
        raise ValueError("action must be start or stop")
    try:
        r = _systemctl(action, UNIT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"systemctl --user {action} {UNIT} did not answer in 60 s")
    if r.returncode != 0:
        raise RuntimeError(f"systemctl --user {action} {UNIT} failed: {(r.stderr or r.stdout).strip()[:300]} "
                           "(installed by scripts/install_notebook.sh)")
    return {"action": action, **service_state()}


def cleared_since(runtime_dir: Path = RUNTIME_DIR) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(json.loads((runtime_dir / CLEARED_NAME).read_text())["cleared_utc"].replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def read_window(now: datetime, minutes: int = WINDOW_MIN, out_dir: Path = OUT_DIR,
                runtime_dir: Path = RUNTIME_DIR) -> List[Dict[str, Any]]:
    """Rows of the last `minutes` from the daily CSVs (today's and, around 00 UTC, yesterday's), none from before the
    last CLEAR."""
    since = now - timedelta(minutes=minutes)
    cleared = cleared_since(runtime_dir)
    if cleared and cleared > since:
        since = cleared
    rows: List[Dict[str, Any]] = []
    paths = [p for day in sorted({since.date(), now.date()})
             for p in out_dir.glob(f"transit_{day:%Y%m%d}*.csv")]       # *_vN.csv: the day's rows before a column was added
    for p in paths:
        with open(p, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    t = datetime.fromisoformat(r["utc"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if t > since or (t == since and not cleared):
                    rows.append({"utc": r["utc"], "status": r.get("status"),
                                 **{k: (float(r[k]) if r.get(k) not in (None, "") else None)
                                    for k in ("power_db", "clipping_fraction", "sun_alt_deg", "sun_az_deg", "pi_temp_c")}})
    rows.sort(key=lambda r: r["utc"])
    return rows


def write_latest(rows: List[Dict[str, Any]], meta: Dict[str, Any], runtime_dir: Path = RUNTIME_DIR) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    p = runtime_dir / LATEST_NAME
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"schema": "almita.transit_logger.v1", **meta, "rows": rows}, default=float))
    tmp.replace(p)


def clear_view(runtime_dir: Path = RUNTIME_DIR) -> Dict[str, Any]:
    """CLEAR (operator 2026-11-02: "limpiar el gráfico y partir de cero"): the page's graphs start empty from now.
    Only the view: every sample stays in data/transit/*.csv."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    tmp = runtime_dir / (CLEARED_NAME + ".tmp")
    tmp.write_text(json.dumps({"cleared_utc": now}))
    tmp.replace(runtime_dir / CLEARED_NAME)
    p = runtime_dir / LATEST_NAME
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        doc = {"schema": "almita.transit_logger.v1"}
    doc["rows"], doc["cleared_utc"] = [], now
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, default=float))
    tmp.replace(p)
    return {"cleared_utc": now}


def append_row(row: Dict[str, Any], out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"transit_{row['utc'][:10].replace('-', '')}.csv"
    if p.is_file():
        with open(p, newline="") as fh:
            header = next(csv.reader(fh), [])
        if tuple(header) != FIELDS:                                   # older columns: keep it beside, start a new file
            k = 1
            while p.with_name(f"{p.stem}_v{k}.csv").exists():
                k += 1
            p.rename(p.with_name(f"{p.stem}_v{k}.csv"))
    new = not p.is_file()
    with open(p, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k) for k in FIELDS})
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--every", type=float, default=10.0, help="seconds between samples (default 10)")
    ap.add_argument("--seconds", type=float, default=1.0, help="IQ per sample, seconds (default 1)")
    ap.add_argument("--gain", type=float, default=None, help="dB, fixed; default the observing gain (observer_config)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1234)
    a = ap.parse_args(argv)

    from astropy_offline import configure_astropy_offline
    configure_astropy_offline()
    import astropy.units as u
    from astropy.coordinates import EarthLocation
    import sdr_tuning
    cfg = json.loads((ROOT / "observer_config.json").read_text())
    obs = cfg.get("observer", cfg)
    location = EarthLocation(lat=float(obs["latitude_deg"]) * u.deg, lon=float(obs["longitude_deg"]) * u.deg,
                             height=float(obs.get("elevation_m", 0.0)) * u.m)
    gain = a.gain if a.gain is not None else float(sdr_tuning.operating_config()["gain_db"])
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    print(f"TRANSIT logger: one {a.seconds:g} s sample every {a.every:g} s, gain {gain:g} dB fixed, rtl_tcp {a.host}:{a.port}", flush=True)
    while not stop["now"]:
        tick = time.monotonic()
        now = datetime.now(timezone.utc)
        row: Dict[str, Any] = {"utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"), "gain_db": gain}
        try:
            row.update(sun_altaz(now, location))
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"sun: {exc}"
        try:
            row["pi_temp_c"] = pi_temp_c()
        except Exception:  # noqa: BLE001 - a temperature is never worth a lost sample
            row["pi_temp_c"] = None
        busy = sdr_busy()
        if busy:
            row["status"] = "SDR_BUSY"
            row["error"] = f"skipped: {busy} is using the SDR"
        else:
            try:
                row.update(asyncio.run(asyncio.wait_for(measure(a.host, a.port, gain, a.seconds), timeout=a.seconds + 20)))
                row["status"] = "VALID" if row["clipping_fraction"] <= 1e-4 else "CLIPPED"
            except Exception as exc:  # noqa: BLE001 - one bad sample must not stop the logger
                row["status"] = "FAILED"
                row["error"] = f"{type(exc).__name__}: {exc}"
        append_row(row)
        write_latest(read_window(now), {"updated_utc": row["utc"], "cleared_utc": (lambda c: c and c.isoformat().replace("+00:00", "Z"))(cleared_since()), "started_utc": started, "every_s": a.every,
                                        "seconds": a.seconds, "gain_db": gain, "pid": os.getpid(),
                                        "location": (cfg.get("site_info") or {}).get("location")})
        p = row.get("power_db")
        print(f"{row['utc']}  {row['status']:8s}  P {p if p is not None else '—'} dB  sun alt {row.get('sun_alt_deg')}  "
              f"{row.get('error') or ''}", flush=True)
        while not stop["now"] and time.monotonic() - tick < a.every:
            time.sleep(0.2)
    print("TRANSIT logger stopped (SIGINT)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
