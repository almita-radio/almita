"""Calibration session directories and evidence persistence (Fase 4).

data/calibration/CAL-YYYYMMDD-HHMMSS/ - same atomic-write discipline as
alignment_engine/session.py (never a partial/corrupted JSON), same
"never overwrite/delete raw" rule. Two differences from AlignmentSession,
both deliberate:

- captures/ (not points/) - a calibration session's raw units are
  independent captures (a gain-sweep step, a stability sample), not
  raster points with a row/col.
- events.jsonl alongside logs/session.log - Fase 46 wants named,
  JSON-friendly progress events (CALIBRATION_BEGIN, CAPTURE_BEGIN, ...)
  a future web UI can consume directly; the plain-text log stays for a
  human tailing it at the terminal. log_event() writes both from one call.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from runtime_state import atomic_write_json, read_json_safe

CAPTURES_DIRNAME = "captures"
ANALYSIS_DIRNAME = "analysis"
PLOTS_DIRNAME = "plots"
LOGS_DIRNAME = "logs"


def new_session_id(now: Optional[datetime] = None) -> str:
    """Microsecond resolution (not just seconds, unlike
    alignment_engine.session.new_session_id's own precedent) - found via
    testing that two `almita_calibrate.py run` invocations completing
    within the same wall-clock second would otherwise silently collide
    into ONE session directory (CalibrationSession's mkdir(exist_ok=True)
    does not fail on this), overwriting each other's captures/result.
    Calibration's own captures can complete fast enough for this to be a
    real risk in a way alignment's multi-point rasters generally are not."""
    now = now or datetime.now(timezone.utc)
    return f"CAL-{now.strftime('%Y%m%d-%H%M%S-%f')}"


class CalibrationSession:
    def __init__(self, root: Path, session_id: str):
        self.session_id = session_id
        self.dir = Path(root) / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / CAPTURES_DIRNAME).mkdir(exist_ok=True)
        (self.dir / ANALYSIS_DIRNAME).mkdir(exist_ok=True)
        (self.dir / PLOTS_DIRNAME).mkdir(exist_ok=True)
        (self.dir / LOGS_DIRNAME).mkdir(exist_ok=True)
        self._events_path = self.dir / "events.jsonl"
        self._logger = self._build_logger()

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"calibration.session.{self.session_id}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        handler = logging.FileHandler(str(self.dir / LOGS_DIRNAME / "session.log"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        return logger

    def log_event(self, event_type: str, **fields: Any) -> None:
        """Writes one line to events.jsonl (Fase 46's named JSON-friendly
        events - CALIBRATION_BEGIN, CAPTURE_BEGIN, CAPTURE_END,
        GAIN_STEP_BEGIN, GAIN_STEP_RESULT, STABILITY_SAMPLE,
        ANALYSIS_BEGIN, ANALYSIS_COMPLETE, PROFILE_READY, ...) and one
        human-readable line to logs/session.log from the same call, so the
        two never drift apart."""
        record = {"event": event_type, "utc": datetime.now(timezone.utc).isoformat(), **fields}
        with self._events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        self._logger.info(f"{event_type} {fields}" if fields else event_type)

    def capture_path(self, name: str, suffix: str = "h5") -> Path:
        return self.dir / CAPTURES_DIRNAME / f"{name}.{suffix}"

    def analysis_dir(self, analysis_id: str) -> Path:
        path = self.dir / ANALYSIS_DIRNAME / analysis_id
        path.mkdir(parents=True, exist_ok=False)
        return path

    # -- generic JSON read/write, one method per named artifact -----------

    def _write(self, name: str, payload: Dict[str, Any]) -> None:
        atomic_write_json(self.dir / f"{name}.json", payload)

    def _read(self, name: str) -> Optional[Dict[str, Any]]:
        return read_json_safe(self.dir / f"{name}.json")

    def write_config(self, payload): self._write("calibration_config", payload)
    def read_config(self): return self._read("calibration_config")

    def write_identity(self, payload): self._write("session_identity", payload)
    def read_identity(self): return self._read("session_identity")

    def write_environment(self, payload): self._write("environment", payload)
    def read_environment(self): return self._read("environment")

    def write_receiver_config(self, payload): self._write("receiver_config", payload)
    def read_receiver_config(self): return self._read("receiver_config")

    def write_preflight(self, payload): self._write("preflight", payload)
    def read_preflight(self): return self._read("preflight")

    def write_result(self, payload): self._write("calibration_result", payload)
    def read_result(self): return self._read("calibration_result")

    def write_state(self, payload): self._write("state", payload)
    def read_state(self): return self._read("state")
