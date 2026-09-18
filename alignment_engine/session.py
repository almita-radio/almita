"""Alignment session directories and evidence persistence (Fase 11).

data/alignment/<MODE>-<UTC timestamp>/ - consistent with the naming already
used by the real hardware trials already in data/alignment/ (e.g.
ALMITA-ALIGNMENT-V2-SUN-HARDWARE-TEST-01-20260826T163749Z). Every write goes
through runtime_state.atomic_write_json - the same primitive the rest of
ALMITA's resident processes already use - so a crash mid-write never leaves
a corrupted JSON (Fase 18: "no partial corrupted JSON").

Raw data is never overwritten by derived/fitted data: raw_grid.json (the
measured points as captured) and fit_result.json (the fitted model +
residual) are always separate files.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from runtime_state import atomic_write_json, read_json_safe

POINTS_DIRNAME = "points"
LOGS_DIRNAME = "logs"


def new_session_id(mode: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{mode.upper()}-{now.strftime('%Y%m%d-%H%M%S')}"


class AlignmentSession:
    """One alignment attempt's directory + typed accessors for its files.

    Never deletes anything (Fase 11: "No borrar raw data"). Re-opening an
    existing session_dir (e.g. after a process restart) picks up wherever
    persisted JSON left off - callers read back what they need via the
    read_* methods rather than this class caching state itself, so a
    restarted process and a fresh one behave identically (Fase 18: "session
    survives process restart").
    """

    def __init__(self, root: Path, session_id: str):
        self.session_id = session_id
        self.dir = Path(root) / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / POINTS_DIRNAME).mkdir(exist_ok=True)
        (self.dir / LOGS_DIRNAME).mkdir(exist_ok=True)
        self._logger = self._build_logger()

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"alignment.session.{self.session_id}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        handler = logging.FileHandler(str(self.dir / LOGS_DIRNAME / "session.log"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        return logger

    def log_event(self, message: str) -> None:
        """Structured-enough for a first version: one line per event, UTC
        timestamp from the handler's own formatter. Fase 22 explicitly asks
        for state transitions/mount commands/point begin-end/etc, not a
        polling firehose - callers are responsible for not calling this in
        a tight loop."""
        self._logger.info(message)

    # -- generic JSON read/write, one method per named artifact -----------

    def _write(self, name: str, payload: Dict[str, Any]) -> None:
        atomic_write_json(self.dir / f"{name}.json", payload)

    def _read(self, name: str) -> Optional[Dict[str, Any]]:
        return read_json_safe(self.dir / f"{name}.json")

    def write_config(self, payload): self._write("alignment_config", payload)
    def read_config(self): return self._read("alignment_config")

    def write_target(self, payload): self._write("target", payload)
    def read_target(self): return self._read("target")

    def write_tracking_before(self, payload): self._write("tracking_before", payload)
    def write_tracking_during(self, payload): self._write("tracking_during", payload)
    def write_tracking_after(self, payload): self._write("tracking_after", payload)

    def write_raw_grid(self, payload): self._write("raw_grid", payload)
    def read_raw_grid(self): return self._read("raw_grid")

    def write_fit_result(self, payload): self._write("fit_result", payload)
    def read_fit_result(self): return self._read("fit_result")

    def write_alignment_result(self, payload): self._write("alignment_result", payload)
    def read_alignment_result(self): return self._read("alignment_result")

    def write_sync_plan(self, payload): self._write("sync_plan", payload)
    def read_sync_plan(self): return self._read("sync_plan")

    def write_sync_result(self, payload): self._write("sync_result", payload)
    def read_sync_result(self): return self._read("sync_result")

    def write_verification(self, payload): self._write("verification", payload)
    def read_verification(self): return self._read("verification")

    def write_state(self, payload): self._write("state", payload)
    def read_state(self): return self._read("state")

    def write_hardware_test_result(self, payload): self._write("hardware_test_result", payload)
    def read_hardware_test_result(self): return self._read("hardware_test_result")

    def write_precheck(self, payload): self._write("precheck", payload)
    def read_precheck(self): return self._read("precheck")

    def write_postcheck(self, payload): self._write("postcheck", payload)
    def read_postcheck(self): return self._read("postcheck")

    def write_motion_check(self, payload): self._write("motion_check", payload)
    def read_motion_check(self): return self._read("motion_check")

    def write_write_audit(self, payload): self._write("write_audit", payload)
    def read_write_audit(self): return self._read("write_audit")

    def point_path(self, index: int, suffix: str = "h5") -> Path:
        return self.dir / POINTS_DIRNAME / f"point_{index:04d}.{suffix}"
