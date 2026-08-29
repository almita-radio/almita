#!/usr/bin/env python3
"""Resident read-only watcher producing data/runtime/almita_status.json.

READ-ONLY. Never connects rtl_tcp, opens INDI, moves the mount, touches
HDF5, or modifies session.csv/mosaic.csv. It only reads:
  - data/runtime/current_session.json      (written by capture.py)
  - data/runtime/quicklook_announcement.json (written by quicklook_live.py)
  - the Quicklook Live status file the above announcement points at
  - host telemetry via telemetry_summary.collect() (read-only /proc + sysfs)
  - /proc/*/cmdline, to detect whether a capture.py process is alive

and produces (atomically):
  - data/runtime/almita_status.json
  - data/runtime/last_session.json   (small archive of the last terminal session)

Instrument/acquisition/quicklook status are computed by pure functions
(build_instrument/build_acquisition/build_quicklook/build_status) so they
can be unit tested with injected clocks and fixtures, independent of the
real 2-second polling loop.
"""
from __future__ import annotations

import argparse
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from runtime_state import atomic_write_json, read_json_safe, utcnow
import telemetry_summary

ROOT = Path(__file__).resolve().parent

TELEMETRY_STALE_SECONDS = 10.0
ACQUISITION_STALE_SECONDS = 30.0
QUICKLOOK_STALE_SECONDS = 30.0
SCHEMA_VERSION = 1

STOP_REQUESTED = False


def request_stop(signum=None, frame=None) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def _age_seconds(timestamp: Optional[str], now_utc: str) -> Optional[float]:
    try:
        then = datetime.fromisoformat(timestamp)
        now = datetime.fromisoformat(now_utc)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds()


def find_capture_process(proc: Path = Path("/proc")) -> bool:
    """Read-only /proc/*/cmdline scan for a running capture.py process."""
    try:
        entries = list(proc.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if "capture.py" in cmdline:
            return True
    return False


@dataclass
class WatcherState:
    last_telemetry: Optional[dict] = None
    last_telemetry_monotonic: Optional[float] = None
    last_session_archive: Optional[dict] = None


def build_instrument(now_monotonic: float, state: WatcherState, collect_fn: Callable[[], dict]) -> dict:
    error = None
    try:
        snapshot = collect_fn()
        state.last_telemetry = snapshot
        state.last_telemetry_monotonic = now_monotonic
    except Exception as exc:
        snapshot = state.last_telemetry
        error = f"{type(exc).__name__}: {exc}"
    stale = (
        state.last_telemetry_monotonic is None
        or (now_monotonic - state.last_telemetry_monotonic) > TELEMETRY_STALE_SECONDS
    )
    if snapshot is None:
        return {
            "cpu": None, "ram": None, "disk": None, "network_interfaces": None,
            "rtl_tcp_process": False, "rtl_tcp_listening": None,
            "sdr_temperature_c": None, "lna_temperature_c": None,
            "mount_state": None, "telemetry_stale": True,
            "error": error or "no telemetry snapshot yet",
        }
    system = snapshot.get("system", {}) or {}
    storage = snapshot.get("storage", {}) or {}
    sdr = snapshot.get("sdr", {}) or {}
    temperatures = snapshot.get("temperatures", {}) or {}
    mount = snapshot.get("mount", {}) or {}
    return {
        "cpu": system.get("cpu_percent"),
        "ram": system.get("memory_percent"),
        "disk": storage.get("percent"),
        "network_interfaces": snapshot.get("network"),
        "rtl_tcp_process": bool(sdr.get("rtl_tcp_process_detected")),
        "rtl_tcp_listening": sdr.get("rtl_tcp_port_listening"),
        "sdr_temperature_c": temperatures.get("sdr_c"),
        "lna_temperature_c": temperatures.get("lna_c"),
        "mount_state": mount.get("status"),
        "telemetry_stale": stale,
        "error": error,
    }


_ACQUISITION_IDLE = {
    "state": "IDLE", "session_id": None, "session_name": None, "point_current": None,
    "points_total": None, "points_success": None, "points_failed": None,
    "points_deferred": None, "current_point_id": None, "last_successful_point_id": None,
    "last_capture_utc": None, "capture_process_detected": False,
    "acquisition_stale": False, "error": None,
}


def build_acquisition(now_utc: str, current_session: Optional[dict], capture_process_detected: bool) -> dict:
    if not current_session:
        return {**_ACQUISITION_IDLE, "capture_process_detected": capture_process_detected}
    age = _age_seconds(current_session.get("updated_utc"), now_utc)
    stale = age is None or age > ACQUISITION_STALE_SECONDS
    state = current_session.get("state") or "IDLE"
    error = current_session.get("error")
    if state in ("RUNNING", "STARTING") and stale and not capture_process_detected:
        state = "DEGRADED"
        error = error or "capture process not detected; session went silent while RUNNING"
    return {
        "state": state,
        "session_id": current_session.get("session_id"),
        "session_name": current_session.get("session_name"),
        "point_current": current_session.get("point_current"),
        "points_total": current_session.get("points_total"),
        "points_success": current_session.get("points_success"),
        "points_failed": current_session.get("points_failed"),
        "points_deferred": current_session.get("points_deferred"),
        "current_point_id": current_session.get("current_point_id"),
        "last_successful_point_id": current_session.get("last_successful_point_id"),
        "last_capture_utc": current_session.get("last_capture_utc"),
        "capture_process_detected": capture_process_detected,
        "acquisition_stale": stale,
        "error": error,
    }


_QUICKLOOK_IDLE = {
    "state": "IDLE", "points_processed": None, "last_product_utc": None,
    "spectrum_available": False, "waterfall_available": False, "map_available": False,
    "quicklook_stale": False, "error": None,
}
_QUICKLOOK_WAITING = {**_QUICKLOOK_IDLE, "state": "WAITING"}


def build_quicklook(now_utc: str, runtime_dir: Path, session_id: Optional[str]) -> dict:
    if not session_id:
        return dict(_QUICKLOOK_IDLE)
    announcement = read_json_safe(Path(runtime_dir) / "quicklook_announcement.json")
    if not announcement or announcement.get("session_id") != session_id or not announcement.get("quicklook_root"):
        return dict(_QUICKLOOK_WAITING)
    root = Path(announcement["quicklook_root"])
    status = read_json_safe(root / "quicklook_live_status.json")
    if status is None:
        return dict(_QUICKLOOK_WAITING)
    age = _age_seconds(status.get("updated_utc"), now_utc)
    stale = age is None or age > QUICKLOOK_STALE_SECONDS
    errors = status.get("errors") or []
    return {
        "state": status.get("status", "UNKNOWN"),
        "points_processed": status.get("points_processed"),
        "last_product_utc": status.get("updated_utc"),
        "spectrum_available": (root / "latest_spectrum.json").is_file(),
        "waterfall_available": (root / "latest_waterfall.json").is_file(),
        "map_available": (root / "quicklook_map.json").is_file(),
        "quicklook_stale": stale,
        "error": errors[0].get("warning") if errors and isinstance(errors[0], dict) else None,
    }


def build_status(now_utc: str, now_monotonic: float, state: WatcherState, runtime_dir: Path,
                  collect_fn: Callable[[], dict], capture_process_detected: bool) -> dict:
    current_session = read_json_safe(Path(runtime_dir) / "current_session.json")
    instrument = build_instrument(now_monotonic, state, collect_fn)
    acquisition = build_acquisition(now_utc, current_session, capture_process_detected)
    quicklook = build_quicklook(now_utc, runtime_dir, acquisition["session_id"])

    if acquisition["state"] in ("COMPLETED", "DEGRADED", "ABORTED") and acquisition["session_id"]:
        archive = {
            "session_id": acquisition["session_id"],
            "session_name": acquisition["session_name"],
            "final_state": acquisition["state"],
            "completed_utc": (current_session or {}).get("updated_utc", now_utc),
            "points_success": acquisition["points_success"],
            "points_total": acquisition["points_total"],
        }
        state.last_session_archive = archive
        try:
            atomic_write_json(Path(runtime_dir) / "last_session.json", archive)
        except Exception:
            pass

    last_session = state.last_session_archive
    if last_session is None:
        last_session = read_json_safe(Path(runtime_dir) / "last_session.json")

    system_state = "READY"
    if instrument.get("telemetry_stale") or acquisition["state"] == "DEGRADED":
        system_state = "DEGRADED"

    return {
        "schema_version": SCHEMA_VERSION,
        "updated_utc": now_utc,
        "system_state": system_state,
        "instrument": instrument,
        "acquisition": acquisition,
        "quicklook": quicklook,
        "last_session": last_session,
    }


def tick(runtime_dir: Path, state: WatcherState, collect_fn: Callable[[], dict] = telemetry_summary.collect,
          proc_root: Path = Path("/proc")) -> dict:
    now_utc = utcnow()
    now_monotonic = time.monotonic()
    capture_detected = find_capture_process(proc_root)
    status = build_status(now_utc, now_monotonic, state, runtime_dir, collect_fn, capture_detected)
    atomic_write_json(Path(runtime_dir) / "almita_status.json", status)
    return status


def run(runtime_dir: Path, interval: float = 2.0, iterations: Optional[int] = None) -> None:
    state = WatcherState()
    # Seed the last-session archive so a restart never invents an active session.
    state.last_session_archive = read_json_safe(Path(runtime_dir) / "last_session.json")
    count = 0
    while iterations is None or count < iterations:
        tick(runtime_dir, state)
        count += 1
        if (iterations is not None and count >= iterations) or STOP_REQUESTED:
            break
        # Sleep in short slices so SIGINT/SIGTERM (which only set a flag,
        # they don't raise) are honored within a fraction of a second
        # instead of waiting out the full poll interval.
        remaining = interval
        while remaining > 0 and not STOP_REQUESTED:
            slice_seconds = min(0.2, remaining)
            time.sleep(slice_seconds)
            remaining -= slice_seconds
        if STOP_REQUESTED:
            break


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default=str(ROOT / "data" / "runtime"))
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    print(f"ALMITA CONSOLE WATCHER START runtime_dir={Path(args.runtime_dir).resolve()}", flush=True)
    try:
        run(Path(args.runtime_dir), interval=args.interval, iterations=1 if args.once else None)
    finally:
        print("ALMITA CONSOLE WATCHER STOP", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
