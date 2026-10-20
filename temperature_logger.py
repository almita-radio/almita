#!/usr/bin/env python3
"""Resident DS18B20 temperature log + "stable since" for STATUS (operator, 2026-10-17).

Every configured sensor (observer_config.json -> temperature_sensors: role -> 1-Wire id; any roles - sdr, lna, load,
cable...) is read every INTERVAL_S and appended to data/runtime/temperature/YYYYMMDD.jsonl, so the warm-up of the
receiver chain (sun-exposed, no radome) can be watched and a measurement started only once it settled. capture.py keeps
its own pre/post reads of sdr/lna for each capture; this log never feeds a capture and never changes the SDR or mount.

A sensor counts as STABLE when it moved at most STABLE_DELTA_C over the last STABLE_WINDOW_S; "stable since" is the
earliest time from which every sample stayed within that rule. Missing or stale samples are said so, never filled in.
The config is re-read every cycle: a sensor added to observer_config.json starts logging without a restart."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from temperature_sensors import DS18B20Reader

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "observer_config.json"
LOG_DIR = ROOT / "data" / "runtime" / "temperature"
INTERVAL_S = 60.0
STABLE_WINDOW_S = 600.0
STABLE_DELTA_C = 0.2
KEEP_DAYS = 60
# The DS18B20 power-up value of its temperature register: the first read after a power cut returns it before any
# conversion (SDR sensor at 16:17 UTC 2026-10-18 after the Pi lost power). Nothing in this chain reaches 85 C, so the log
# and the STATUS chart treat it as no reading (operator, 2026-10-18). capture.py's own reads are not changed here.
POWER_ON_RESET_C = 85.0
POWER_ON_RESET_ERROR = "85.0 C is the DS18B20 power-on reset value, not a reading"


def configured_sensors(config_path: Path = CONFIG_PATH) -> Dict[str, str]:
    try:
        sensors = json.loads(Path(config_path).read_text()).get("temperature_sensors") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {str(k).lower(): str(v) for k, v in sensors.items() if v}


def sample(sensors: Dict[str, str], reader_factory=DS18B20Reader) -> Dict[str, Any]:
    reader = reader_factory(sensors, warning=lambda message: None)
    readings = {}
    for role in sensors:
        r = reader.read(role)
        if r["valid"] and r["temperature_c"] == POWER_ON_RESET_C:
            r = {**r, "valid": False, "error": POWER_ON_RESET_ERROR}
        readings[role] = {"c": r["temperature_c"] if r["valid"] else None, "id": r["sensor_id"],
                          **({} if r["valid"] else {"error": r.get("error")})}
    return {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "readings": readings}


def append(record: Dict[str, Any], log_dir: Path = LOG_DIR) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{record['utc'][:10].replace('-', '')}.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    return path


def prune(log_dir: Path = LOG_DIR, keep_days: int = KEEP_DAYS, now: Optional[datetime] = None) -> None:
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=keep_days)).strftime("%Y%m%d")
    for f in log_dir.glob("*.jsonl"):
        if f.stem < cutoff:
            f.unlink(missing_ok=True)


def load(hours: float, log_dir: Path = LOG_DIR, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    rows: List[Dict[str, Any]] = []
    day = start.date()
    while day <= now.date():
        f = log_dir / f"{day.strftime('%Y%m%d')}.jsonl"
        if f.is_file():
            for line in f.read_text(errors="replace").splitlines():
                try:
                    # a power cut leaves NUL padding in front of the next line written after the reboot
                    rec = json.loads(line.strip("\x00 "))
                    t = datetime.fromisoformat(rec["utc"])
                except (ValueError, KeyError):
                    continue
                if start <= t <= now:
                    for reading in (rec.get("readings") or {}).values():
                        if isinstance(reading, dict) and reading.get("c") == POWER_ON_RESET_C:
                            reading.update(c=None, error=POWER_ON_RESET_ERROR)
                    rows.append(rec)
        day += timedelta(days=1)
    return rows


def stability(points: List[tuple], now: datetime, interval_s: float = INTERVAL_S,
              window_s: float = STABLE_WINDOW_S, delta_c: float = STABLE_DELTA_C) -> Dict[str, Any]:
    """points: [(datetime, celsius)] sorted, valid samples only. A sample is "settled" when the reading one window
    earlier (the latest sample at or before t - window) is within delta_c of it."""
    if not points:
        return {"state": "NO_DATA", "stable_since": None, "change_last_window_c": None, "last_c": None, "last_utc": None}
    last_t, last_c = points[-1]
    out = {"last_c": last_c, "last_utc": last_t.isoformat(timespec="seconds"), "stable_since": None, "change_last_window_c": None}
    if (now - last_t).total_seconds() > 3 * interval_s:
        return {**out, "state": "STALE"}
    settled: List[Optional[bool]] = []
    j = 0
    for i, (t, c) in enumerate(points):
        while j + 1 <= i and points[j + 1][0] <= t - timedelta(seconds=window_s):
            j += 1
        if points[j][0] > t - timedelta(seconds=window_s):
            settled.append(None)                         # not one window of history yet
        else:
            settled.append(abs(c - points[j][1]) <= delta_c)
            if i == len(points) - 1:
                out["change_last_window_c"] = c - points[j][1]
    if settled[-1] is None:
        return {**out, "state": "WARMING_UP_LOG"}         # under one window of data: cannot say yet
    if not settled[-1]:
        return {**out, "state": "CHANGING"}
    k = len(settled) - 1
    while k - 1 >= 0 and settled[k - 1]:
        k -= 1
    since = points[k][0] - timedelta(seconds=window_s)   # it has been within delta since one window before that sample
    return {**out, "state": "STABLE", "stable_since": max(since, points[0][0]).isoformat(timespec="seconds")}


def summary(hours: float = 6.0, log_dir: Path = LOG_DIR, now: Optional[datetime] = None,
            config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    rows = load(hours, log_dir, now)
    roles = list(configured_sensors(config_path))
    for rec in rows:
        for role in rec.get("readings", {}):
            if role not in roles:
                roles.append(role)
    series, status = {}, {}
    for role in roles:
        pts = [(rec["utc"], (rec["readings"].get(role) or {}).get("c")) for rec in rows if role in rec.get("readings", {})]
        series[role] = pts
        valid = [(datetime.fromisoformat(t), c) for t, c in pts if c is not None]
        status[role] = stability(valid, now)
        last = next((rec["readings"][role] for rec in reversed(rows) if role in rec.get("readings", {})), None)
        status[role]["sensor_id"] = (last or {}).get("id")
        status[role]["last_error"] = (last or {}).get("error")
    return {"generated_utc": now.isoformat(timespec="seconds"), "hours": hours, "interval_s": INTERVAL_S,
            "rule": {"window_s": STABLE_WINDOW_S, "delta_c": STABLE_DELTA_C}, "series": series, "status": status}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--interval", type=float, default=INTERVAL_S)
    a = p.parse_args()
    print(f"ALMITA TEMPERATURE LOGGER START interval={a.interval}s log_dir={LOG_DIR}", flush=True)
    while True:
        started = time.monotonic()
        try:
            sensors = configured_sensors()
            if sensors:
                append(sample(sensors))
            prune()
        except Exception as exc:  # noqa: BLE001 - one bad cycle never stops the log
            print(f"temperature logger cycle failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(1.0, a.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
