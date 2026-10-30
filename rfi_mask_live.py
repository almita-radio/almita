#!/usr/bin/env python3
"""Live RFI masks of MAIN during an OBSERVE run (operator 2026-10-30): follows the session capture.py announces
(runtime/current_session.json), masks every new successful capture with rfi_mask.py, keeps the side-cars next to the
session (data/mosaic/<grid>/data/rfi_mask/<session>/) and publishes the latest masked spectrum for the monitor:

    runtime/main_rfi_mask_latest.json   - last capture: clean spectrum, raw spectrum, mask codes, what was masked
    runtime/main_rfi_mask_session.json  - one summary row per capture of the session

Independent of QUICKLOOK on purpose: it needs no calibration profile (the profile only accepts captures at its own
gain, and the gain pilot may choose another), and it only READS the HDF5 files. Stop it with SIGINT.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import rfi_mask
from quicklook_live import read_manifest
from runtime_state import atomic_write_json, read_json_safe

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = ROOT / "data" / "runtime"
LATEST, SESSION = "main_rfi_mask_latest.json", "main_rfi_mask_session.json"
STOP = False
KNOWN_AFTER_HITS = 2


def _stop(signum=None, frame=None):
    global STOP
    STOP = True


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sidecar_dir(session_root: Path) -> Path:
    """data/mosaic/<grid>/data/iq/<session>  ->  data/mosaic/<grid>/data/rfi_mask/<session>"""
    return session_root.parent.parent / "rfi_mask" / session_root.name


class MaskLive:
    def __init__(self, runtime_dir: Path = DEFAULT_RUNTIME_DIR, session_root: Optional[Path] = None,
                 sidecar_override: Optional[Path] = None):
        self.sidecar_override = Path(sidecar_override) if sidecar_override else None
        self.runtime_dir = Path(runtime_dir)
        self.fixed_root = Path(session_root) if session_root else None
        self.session_root: Optional[Path] = None
        self.session_id: Optional[str] = None
        self.done: Dict[str, Dict[str, Any]] = {}
        self.rows: List[Dict[str, Any]] = []
        self.line_hits: List[List[float]] = []     # [frequency_hz, captures where it was found over the NEW-line bar]
        self.started_utc = utcnow()                # only sessions that start after this process are followed

    def _current(self) -> Optional[tuple]:
        if self.fixed_root:
            return self.fixed_root, self.fixed_root.name
        cur = read_json_safe(self.runtime_dir / "current_session.json") or {}
        root = cur.get("session_root")
        if not root or str(cur.get("started_utc") or "") < self.started_utc:
            return None                                # an older session: never re-processed by accident
        root = Path(root) if Path(root).is_absolute() else ROOT / root
        return root, str(cur.get("session_id") or root.name)

    def _reset(self, root: Path, session_id: str) -> None:
        self.session_root, self.session_id = root, session_id
        self.done, self.rows, self.line_hits = {}, [], []
        prior = read_json_safe(self.runtime_dir / SESSION) or {}
        if prior.get("session_id") == session_id:                       # restarted mid-session: keep what was done
            self.rows = list(prior.get("rows") or [])
            self.done = {r["point_id"]: r for r in self.rows}
            self.line_hits = [list(h) for h in prior.get("line_hits") or []]

    @property
    def known_lines(self) -> List[float]:
        """A line is "known" (masked from the lower bar) only once it was found over the new-line bar in at least
        KNOWN_AFTER_HITS captures: one strong capture alone no longer lowers the bar for the whole session."""
        return [f for f, n in self.line_hits if n >= KNOWN_AFTER_HITS]

    def _count_hits(self, lines: List[Dict[str, Any]], line_db: float) -> None:
        for ln in lines:
            if ln.get("dc") or ln["excess_db"] < line_db:
                continue
            for h in self.line_hits:
                if abs(h[0] - ln["frequency_hz"]) < rfi_mask.KNOWN_MATCH_HZ:
                    h[1] += 1
                    break
            else:
                self.line_hits.append([ln["frequency_hz"], 1])

    def _sidecars(self, root: Path) -> Path:
        return self.sidecar_override or sidecar_dir(root)

    def scan_once(self) -> int:
        cur = self._current()
        if cur is None:
            return 0
        root, sid = cur
        if root != self.session_root or sid != self.session_id:
            self._reset(root, sid)
        processed = 0
        for row in read_manifest(root):
            pid = row.get("point_id", "")
            if STOP or pid in self.done or row.get("status", "").upper() != "SUCCESS":
                continue
            src = Path(row.get("source_hdf5", ""))
            src = src if src.is_absolute() else root / src
            if src.name.endswith(".part") or not src.is_file():
                continue
            started = time.perf_counter()
            try:
                result = rfi_mask.analyze_capture(src, self.known_lines)
            except Exception as exc:                                      # one bad file never stops the run
                self.done[pid] = {"point_id": pid, "error": f"{type(exc).__name__}: {exc}"}
                continue
            paths = rfi_mask.write_sidecar(result, self._sidecars(root), src.stem)
            self._count_hits(result["lines"], result["thresholds"]["line_db"])
            summ = {"point_id": pid, "source_hdf5": str(src), "sidecar_json": paths["json"],
                    "capture_start_utc": result["capture_start_utc"], "gain_db": result["gain_db"],
                    "tooth_max_db": result["tooth_max_db"], "comb_channels": result["comb_channels"],
                    "line_channels": result["line_channels"], "lines": len(result["lines"]),
                    "masked_channel_fraction": result["masked_channel_fraction"],
                    "pulse_fraction": result["pulse_fraction"], "adc_rail_hit_fraction": result["adc_rail_hit_fraction"],
                    "seconds": round(time.perf_counter() - started, 2)}
            self.done[pid] = summ
            self.rows.append(summ)
            atomic_write_json(self.runtime_dir / LATEST, {
                "schema": rfi_mask.SCHEMA, "session_id": self.session_id, "updated_utc": utcnow(), **summ,
                "center_frequency_hz": result["center_frequency_hz"], "code_names": rfi_mask.CODE_NAMES,
                "teeth": result["teeth"], "line_list": result["lines"],
                "spectrum": rfi_mask.display_spectrum(result)})
            atomic_write_json(self.runtime_dir / SESSION, {
                "schema": rfi_mask.SCHEMA, "session_id": self.session_id, "session_root": str(root),
                "sidecar_dir": str(self._sidecars(root)), "updated_utc": utcnow(), "rows": self.rows,
                "known_lines_hz": self.known_lines, "line_hits": self.line_hits})
            processed += 1
            del result
        return processed

    def follow_from(self, utc: str) -> "MaskLive":
        self.started_utc = utc
        return self

    def run(self, poll: float = 2.0, once: bool = False) -> None:
        while not STOP:
            self.scan_once()
            if once:
                return
            time.sleep(poll)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    p.add_argument("--session-root", type=Path, help="mask this session instead of the announced one")
    p.add_argument("--sidecar-dir", type=Path, help="write the side-cars here instead of next to the session")
    p.add_argument("--since-utc", help="also follow an announced session started after this ISO time (restart mid-run)")
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--once", action="store_true")
    a = p.parse_args(argv)
    signal.signal(signal.SIGINT, _stop)
    live = MaskLive(a.runtime_dir, a.session_root, a.sidecar_dir)
    if a.since_utc:
        live.follow_from(a.since_utc)
    live.run(a.poll, a.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
