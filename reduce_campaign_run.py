#!/usr/bin/env python3
"""ALMITA REDUCE - campaign RUN bridge, with a verifiable RUN-time calibration-compatibility record.

almita_reduce.py (frozen) has no hook point for capturing what calibration_foundation.
check_calibration_compatibility() found for every point right before reduce_engine.pipeline.
reduce_campaign() processes them - its own `run` command only prints the final CampaignReduceReport.
This script calls the EXACT SAME frozen functions almita_reduce.py's own cmd_run does
(reduce_engine.ingest.discover_campaign, reduce_engine.validation.run_preflight/blocking_reason,
reduce_engine.pipeline.reduce_campaign) - never a second reduction algorithm - and additionally
persists reduce_calibration_record.compute_record()'s result as calibration_compatibility_record.json
inside the real output_dir reduce_campaign() itself creates. almita_reduce.py itself is untouched;
PLAN still uses it directly (`almita_reduce.py plan`) - only RUN goes through this bridge, because
only RUN needs the pre-execution capture.

100% offline: filesystem-only, no hardware, no INDI, no rtl_tcp, no mount, no network, never touches
the original HDF5s or almita_reduce.py/reduce_engine.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


def _print(payload: Dict[str, Any], as_json: bool, human_lines) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in human_lines:
            print(line)


class _ProgressFollower(threading.Thread):
    """Prints one stderr line per processed point while reduce_campaign() runs, so the web job log shows real
    progress. It only READS the logs/events.jsonl that the frozen ReduceSession itself appends to (the new
    session dir under output_root/campaign_id) - reduce_engine is not touched or patched. Lines carry no
    braces: the web parses the final JSON result as the first '{' .. last '}' of the log."""

    def __init__(self, session_parent: Path, total: int, stream=None, poll_s: float = 1.0):
        super().__init__(daemon=True)
        self.parent, self.total, self.poll_s = session_parent, total, poll_s
        self.stream = stream or sys.stderr
        self.before = set(session_parent.iterdir()) if session_parent.is_dir() else set()
        self.stop_event = threading.Event()
        self.started = time.monotonic()
        self.done = 0
        self.counts: Dict[str, int] = {}
        self._offset = 0
        self._events: Optional[Path] = None

    def _find_events(self) -> Optional[Path]:
        if not self.parent.is_dir():
            return None
        new = [d for d in self.parent.iterdir() if d not in self.before and (d / "logs" / "events.jsonl").exists()]
        return max(new, key=lambda d: d.stat().st_mtime) / "logs" / "events.jsonl" if new else None

    def _emit(self, text: str) -> None:
        print(f"[REDUCE] {text.replace('{', '(').replace('}', ')')}", file=self.stream, flush=True)

    def _drain(self) -> None:
        if self._events is None:
            self._events = self._find_events()
            if self._events is None:
                return
        with self._events.open("rb") as fh:
            fh.seek(self._offset)
            chunk = fh.read()
        complete = chunk[:chunk.rfind(b"\n") + 1]            # never parse a half-written last line
        self._offset += len(complete)
        for raw in complete.splitlines():
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if ev.get("event") == "POINT_PROCESSED":
                self.done += 1
                status = str(ev.get("status") or "?").split(".")[-1]
                self.counts[status] = self.counts.get(status, 0) + 1
                timing = ev.get("timing") or {}
                took = sum(v for v in timing.values() if isinstance(v, (int, float)))
                elapsed = time.monotonic() - self.started
                eta = elapsed / self.done * (self.total - self.done) if self.done and self.total else None
                line = (f"point {ev.get('point_index')} - {self.done}/{self.total} - {status} ({took:.1f} s) - "
                        f"elapsed {_hms(elapsed)}" + (f" - remaining ~{_hms(eta)}" if eta is not None else ""))
                if ev.get("reason"):
                    line += f" - {ev['reason']}"
                self._emit(line)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._drain()
            except OSError:
                pass
            self.stop_event.wait(self.poll_s)

    def finish(self) -> None:
        self.stop_event.set()
        if self.ident is not None:                             # started (finish() is safe either way)
            self.join(timeout=5)
        try:
            self._drain()                                     # the last points written after the final poll
        except OSError:
            pass
        summary = ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items())) or "no points"
        self._emit(f"done: {self.done}/{self.total} points processed ({summary}) in "
                   f"{_hms(time.monotonic() - self.started)} - writing the result")


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def cmd_run(args) -> int:
    from reduce_engine.config import ReduceConfig
    from reduce_engine.ingest import discover_campaign
    from reduce_engine.pipeline import reduce_campaign
    from reduce_engine.validation import blocking_reason, run_preflight
    import reduce_calibration_record as calrecord

    manifest = discover_campaign(args.campaign_dir)
    config = ReduceConfig(velocity_frame=args.velocity_frame, calibration_profile_path=args.calibration_profile)
    checks = run_preflight(manifest, config, output_root=args.output_root,
                           calibration_profile_path=args.calibration_profile)
    reason = blocking_reason(checks)
    if reason:
        _print({"blocked": True, "reason": reason}, args.json, [f"BLOCKED: {reason}"])
        return 1

    # Captured HERE - immediately before the real frozen reduce_campaign() call, in this same process -
    # not from an earlier PLAN/preview, and not re-derived afterwards from whatever the files contain later.
    record = calrecord.compute_record(
        args.calibration_profile,
        [(pt.point_index, pt.resolved_path) for pt in manifest.accepted_points()],
    )

    follower = _ProgressFollower(Path(args.output_root).resolve() / manifest.campaign_id, len(manifest.points))
    print(f"[REDUCE] starting: {len(manifest.points)} points, velocity frame {args.velocity_frame}",
          file=sys.stderr, flush=True)
    follower.start()
    try:
        report = reduce_campaign(manifest, config, output_root=args.output_root,
                                 calibration_profile_path=args.calibration_profile)
    finally:
        follower.finish()
    record_path = calrecord.write_record(report.output_dir, record)

    payload = dict(report.__dict__)
    payload["calibration_compatibility_record_path"] = record_path
    _print(payload, args.json, [
        f"REDUCE {report.status}",
        f"Campaign:        {report.campaign_id}",
        f"Points:          {report.points_discovered}",
        f"Accepted:        {report.points_accepted}",
        f"Rejected:        {report.points_rejected}",
        f"Calibration:     {report.calibration_level_counts}",
        f"Velocity:        {report.velocity_frame_counts}",
        f"Runtime:         {report.runtime_seconds:.2f}s",
        f"Output:          {report.output_dir}",
        *([f"Calibration compatibility record: {record_path}"] if record_path else []),
    ])
    return 0 if report.status in ("COMPLETED", "PARTIAL") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="run REDUCE on a campaign, recording a verifiable per-point calibration-compatibility result")
    p.add_argument("campaign_dir")
    p.add_argument("--output-root", default="data/reduced")
    p.add_argument("--velocity-frame", default="lsrk")
    p.add_argument("--calibration-profile", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_run)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
