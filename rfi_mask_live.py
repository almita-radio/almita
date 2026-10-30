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
STACK = "main_rfi_mask_session_waterfall_state.json"   # same format as quicklook's session_waterfall_state.json
STACK_BINS = 256                                        # quicklook_session_waterfall.DEFAULT_FREQUENCY_BINS
STOP = False
KNOWN_AFTER_HITS = 2
STACK_WAIT_S = 60.0                                     # how long a clean stack row waits for QUICKLOOK's point


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
        self.stack_pending: Dict[str, Dict[str, Any]] = {}   # point -> clean-minus-raw and codes, until QUICKLOOK has it
        self.stack_rows: List[Dict[str, Any]] = []
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
        self.stack_pending, self.stack_rows = {}, []
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
            a = result["arrays"]
            self.stack_pending[pid] = {"freq_hz": a["freq_hz"], "codes": a["codes"], "inner": a["inner"],
                                       "clean_db": a["mean_db_clean"], "delta_db": a["mean_db_clean"] - a["mean_db_raw"],
                                       "utc": result["capture_start_utc"], "since": time.monotonic()}
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
        self._update_stack(root)
        return processed

    def _quicklook_point(self, root: Path, pid: str) -> Optional[Path]:
        """data/mosaic/<grid>/data/iq/<session> -> data/mosaic/<grid>/quicklook/points/<pid>/spectrum/..."""
        path = root.parent.parent.parent / "quicklook" / "points" / pid / "spectrum" / "quicklook_spectrum.json"
        return path if path.is_file() else None

    def _update_stack(self, root: Path) -> None:
        """Clean rows for the 3D stack, in QUICKLOOK's own units when it has the point: its profile-relative PSD plus
        (pulse-free mean - all-frames mean) of the same capture, the masked channels left out of the median
        decimation exactly like its spur channels. Without QUICKLOOK (profile refused the gain) after STACK_WAIT_S:
        the pulse-free mean itself, offset to its median (the band shape stays)."""
        import json as _json
        import numpy as np
        from quicklook_waterfall import decimate_dashboard
        changed = False
        for pid, p in list(self.stack_pending.items()):
            ql = self._quicklook_point(root, pid)
            freq, good = p["freq_hz"], p["inner"] & (p["codes"] == rfi_mask.CODE_OK)
            values, basis = None, None
            if ql is not None:
                try:
                    q = _json.loads(ql.read_text())
                    qf = np.asarray(q["frequency_hz"], float)
                    if len(qf) == len(freq) and np.allclose(qf, freq, atol=1.0):
                        rel = np.array([np.nan if v is None else v for v in q["relative_psd_db"]], float)
                        good = good & np.asarray(q["valid_mask"], bool) & ~np.asarray(q["spur_mask"], bool)
                        values, basis = rel + p["delta_db"], "quicklook_relative_plus_pulse_correction"
                except (OSError, ValueError, KeyError):
                    values = None
            if values is None:
                if time.monotonic() - p["since"] < STACK_WAIT_S:
                    continue                                   # QUICKLOOK may still be on this point
                values = p["clean_db"] - np.median(p["clean_db"][good])
                basis = "pulse_free_mean_minus_median"
            f, v, _ = decimate_dashboard(freq, values[None, :], good, frequency_bins=STACK_BINS)
            self.stack_rows = [r for r in self.stack_rows if r["point_id"] != pid]
            self.stack_rows.append({"point_id": pid, "utc": p["utc"], "basis": basis,
                                    "relative_db": [float(x) if np.isfinite(x) else None for x in v[0]]})
            self.stack_freq_mhz = (f / 1e6).tolist()
            del self.stack_pending[pid]
            changed = True
        if changed:
            atomic_write_json(self.runtime_dir / STACK, {
                "schema_version": 1, "session_id": self.session_id, "source": "rfi_mask_live.py (clean)",
                "frequency_mhz": self.stack_freq_mhz, "rows": self.stack_rows, "updated_utc": utcnow()})

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
