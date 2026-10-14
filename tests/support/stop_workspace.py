"""Builds a throw-away capture workspace (plan CSV, observer config) and starts the fake servers - shared by the
STOP integration tests. Everything lives under the given temporary directory; nothing touches data/ or hardware."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = Path(__file__).resolve().parent
FIELDS = ["point_number", "point_id", "scan_order", "grid_row", "grid_col", "row", "column", "ra", "dec",
          "target_ra_hours", "target_dec_degrees", "target_ra_hms", "target_dec_dms", "capture_status", "start_time",
          "end_time", "duration", "error_message", "data_filename", "session_name"]


def _hms(hours: float) -> str:
    total = hours * 3600.0
    return f"{int(total // 3600):02d}:{int(total % 3600 // 60):02d}:{total % 60:06.3f}"


def local_sidereal_hours() -> float:
    sys.path.insert(0, str(ROOT))
    import astropy_offline  # noqa: F401  (offline IERS, as the product does)
    import astropy.units as u
    from astropy.coordinates import EarthLocation
    from astropy.time import Time
    loc = EarthLocation(lat=-33.4489 * u.deg, lon=-70.6693 * u.deg, height=570 * u.m)
    return float(Time.now().sidereal_time("apparent", loc.lon).hour)


def write_plan(workdir: Path, n_points: int = 4, spacing_hours: float = 0.4) -> Path:
    """Points near the zenith of the observer right now (always visible), far enough apart for a real slew."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "observer_config.json").write_text(json.dumps(
        {"observer": {"latitude_deg": -33.4489, "longitude_deg": -70.6693, "elevation_m": 570}}))
    lst = local_sidereal_hours()
    plan = workdir / "mosaic.csv"
    with plan.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for i in range(n_points):
            ra = (lst - 1.0 + spacing_hours * i) % 24.0
            writer.writerow({"point_number": i + 1, "point_id": i + 1, "scan_order": i + 1, "grid_row": 0, "grid_col": i,
                             "row": 0, "column": i, "ra": f"{ra:.6f}", "dec": "-33.000000", "target_ra_hours": f"{ra:.6f}",
                             "target_dec_degrees": "-33.000000", "target_ra_hms": _hms(ra), "target_dec_dms": "-33:00:00.00",
                             "capture_status": "planned", "data_filename": f"STOPTEST_{i + 1:04d}.dat",
                             "session_name": "STOPTEST"})
    return plan


class Listening:
    """A support server started as a subprocess; .port is read from its "LISTENING <port>" line."""

    def __init__(self, script: str, *args: str):
        self.proc = subprocess.Popen([sys.executable, str(SUPPORT / script), "--port", "0", *args],
                                     stdout=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline()
        if not line.startswith("LISTENING"):
            raise RuntimeError(f"{script} did not start: {line!r}")
        self.port = int(line.split()[1])

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def journal_commands(journal: Path) -> list:
    if not journal.exists():
        return []
    return [r for r in (json.loads(x) for x in journal.read_text().splitlines() if x.strip()) if r["kind"] == "new"]


def wait_for(predicate, timeout: float, period: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(period)
    return False
