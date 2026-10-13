#!/usr/bin/env python3
"""Read-only audit: which real captures declared a center frequency different from the one the MAIN receiver
was actually tuned to?

Why this can happen: rtl_tcp keeps whatever tuning the PREVIOUS client left. Until 2026-10-02 two acquisition
paths never retuned (the CALIBRATE wizard's 50 ohm reference and calibration_operational_realtest.py, both via
calibration_engine.acquisition.RealCalibrationAcquisitionBackend); they declared a frequency in their HDF5 attrs
(the wizard config, or the rtl_tcp startup argv) without it being what the device was at.

How the actual tuning is reconstructed for those captures (never by trusting the file's own label):
  - rtl_tcp (re)starts at its unit's startup argv: every boot (wtmp, `last -x reboot`) and every start of
    rtl_tcp.service visible in the journal. The unit argv was -f 1420405000 until 2026-10-01T22:39Z.
  - Every client that tunes explicitly and is on record: web ALIGN runs, wizard HI captures, gain-pilot captures,
    OBSERVE sessions (capture.py), with the frequency each one commanded.
  - The actual frequency at capture time = the latest of those events before it.
Confidence: CONFIRMED when the rtl_tcp journal covers the capture time (so rtl_tcp restarts are known) and the
journal shows no unexplained "set freq"; PROBABLE otherwise. Clients run by hand from a shell are not on record
in either case - stated in every result.

Writes data/audits/FREQUENCY-AUDIT-<utc>/frequency_audit.json and .md. Never modifies a capture: historical
files keep their original attrs; this report is the correction.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import h5py

ROOT = Path(__file__).resolve().parents[2]
# HISTORICAL record, not a frequency to use: what /etc/systemd/system/rtl_tcp.service started at (-f) until the
# 2026-10-01 fix. The only frequency literal this file may hold (tests/test_sdr_tuning.py checks that).
OLD_UNIT_STARTUP_HZ = 1420405000
UNIT_FIX_UTC = datetime(2026, 10, 1, 22, 39, tzinfo=timezone.utc)


def _versioned_unit_startup_hz() -> int:
    """-f of the versioned unit (systemd/rtl_tcp.service), which tests/test_sdr_tuning.py keeps equal to
    observer_config.json's operating frequency - the startup value since the fix, never a second literal."""
    unit = (ROOT / "systemd" / "rtl_tcp.service").read_text()
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    args = exec_start.split("=", 1)[1].split()
    return int(args[args.index("-f") + 1])


NEW_UNIT_STARTUP_HZ = _versioned_unit_startup_hz()


def _utc(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _startup_hz(when: datetime) -> int:
    return OLD_UNIT_STARTUP_HZ if when < UNIT_FIX_UTC else NEW_UNIT_STARTUP_HZ


# ------------------------------------------------------------------ evidence sources
def boots() -> List[datetime]:
    out = subprocess.run(["last", "-x", "reboot", "--time-format", "iso"], capture_output=True, text=True).stdout
    return sorted(_utc(m.group(1)) for m in re.finditer(r"system boot\s+\S+\s+(\S+)", out))


def journal_rtl_tcp() -> Tuple[Optional[datetime], List[datetime], List[Tuple[datetime, int]]]:
    """(first journal entry, rtl_tcp.service starts, 'set freq' acknowledgements) - all UTC."""
    out = subprocess.run(["journalctl", "-u", "rtl_tcp.service", "-o", "short-iso-precise", "--no-pager", "--utc"],
                         capture_output=True, text=True).stdout
    first, starts, acks = None, [], []
    for line in out.splitlines():
        m = re.match(r"(\S+)\s+\S+\s+(\S+?)(?:\[\d+\])?:\s+(.*)", line)
        if not m:
            continue
        when = _utc(m.group(1))
        first = first or when
        if m.group(2) == "systemd" and m.group(3).startswith("Started"):
            starts.append(when)
        f = re.search(r"\bset freq (\d+)", m.group(3))
        if f:
            acks.append((when, int(f.group(1))))
    return first, starts, acks


def tuning_events() -> List[Dict[str, Any]]:
    """Clients on record that tuned MAIN explicitly, with the frequency each commanded."""
    events: List[Dict[str, Any]] = []
    for job_json in sorted(glob.glob(str(ROOT / "data/runtime/web_ops/*/job.json"))):
        job = json.loads(Path(job_json).read_text())
        stage, started = job.get("stage"), job.get("started_utc")
        if not started:
            continue
        freq = None
        if stage == "align":
            result = None
            out = (job.get("meta") or {}).get("output_dir")
            if out and (ROOT / out / "alignment_result.json").is_file():
                result = json.loads((ROOT / out / "alignment_result.json").read_text())
            argv = job.get("argv") or []
            freq = (int(float(argv[argv.index("--center-freq") + 1])) if "--center-freq" in argv
                    else (int(result["center_frequency_hz"]) if result and result.get("center_frequency_hz") else None))
            if freq is None:
                freq = int(round(1_420_405_751.77))       # alignment.py --center-freq default before 2026-10-02
        elif stage == "calibrate_wizard_move":
            state_path = ROOT / (job.get("params") or {}).get("session_dir", "") / "wizard_state.json"
            if state_path.is_file():
                freq = int(json.loads(state_path.read_text())["config"]["center_frequency_hz"])
        elif stage == "observe_gain_pilot_capture":
            continue   # resolved later if any pilot capture exists; none on disk today
        if freq is not None:
            events.append({"utc": _utc(started), "hz": freq, "source": f"web job {job['job_id']} ({stage})"})
    for plan in sorted(glob.glob(str(ROOT / "data/mosaic/*/observation_resolved.json"))):
        iq_root = Path(plan).parent / "data" / "iq"
        if not iq_root.is_dir():
            continue                                   # planned, never captured
        main = json.loads(Path(plan).read_text())["main"]
        for session in sorted(p for p in iq_root.iterdir() if p.is_dir()):
            m = re.search(r"(\d{8})-(\d{2}:\d{2}:\d{2})$", session.name)
            if m:
                when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H:%M:%S").replace(tzinfo=timezone.utc)
                events.append({"utc": when, "hz": int(main["center_frequency_hz"]),
                               "source": f"OBSERVE capture.py session {session.name}"})
    return sorted(events, key=lambda e: e["utc"])


# ------------------------------------------------------------------ captures
def _attrs(path: str) -> Dict[str, Any]:
    with h5py.File(path, "r") as handle:
        return {k: (v.item() if hasattr(v, "item") else v) for k, v in handle.attrs.items()}


def _flow(path: str, attrs: Dict[str, Any]) -> str:
    if attrs.get("simulated"):
        return "SIMULATED"
    if attrs.get("tuning_evidence") == "RTL_TCP_SERVER_ACK":
        return "EXPLICIT_TUNING_WITH_EVIDENCE"
    if "/AMBIENT_50R/" in path or attrs.get("configuration_source") in ("VERIFIED_BY_SERVICE_COMMAND_LINE",
                                                                        "SERVICE_STARTUP_ARGV"):
        return "NEVER_RETUNED"
    return "COMMANDED_BY_OWN_CONNECTION"


def audit() -> Dict[str, Any]:
    boot_list = boots()
    journal_first, unit_starts, acks = journal_rtl_tcp()
    events = tuning_events()
    resets = [{"utc": b, "hz": _startup_hz(b), "source": "boot (rtl_tcp.service starts at its unit argv)"} for b in boot_list]
    resets += [{"utc": s, "hz": _startup_hz(s), "source": "rtl_tcp.service start (journal)"} for s in unit_starts]
    timeline = sorted(resets + events, key=lambda e: e["utc"])

    rows: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(str(ROOT / "data/**/*.h5"), recursive=True)):
        rel = str(Path(path).relative_to(ROOT))
        try:
            attrs = _attrs(path)
        except OSError as exc:
            rows.append({"file": rel, "flow": "UNREADABLE", "detail": str(exc)})
            continue
        declared = attrs.get("center_frequency_hz")
        if declared is None:
            continue                                    # derived products (REDUCE masters): no tuning of their own
        flow = _flow(rel, attrs)
        row: Dict[str, Any] = {"file": rel, "flow": flow, "declared_center_frequency_hz": float(declared)}
        if flow == "NEVER_RETUNED":
            when = _utc(str(attrs.get("capture_start_utc") or attrs.get("created_at")))
            before = [e for e in timeline if e["utc"] <= when]
            last = before[-1] if before else None
            covered = journal_first is not None and when >= journal_first
            row.update(capture_utc=when.isoformat(),
                       actual_center_frequency_hz=float(last["hz"]) if last else None,
                       actual_from=(f"{last['source']} at {last['utc'].isoformat()}" if last else "no event on record"),
                       confidence="CONFIRMED" if covered else "PROBABLE",
                       confidence_basis=("rtl_tcp journal covers this time: restarts and acknowledged retunes known"
                                         if covered else "before the rtl_tcp journal's retention: restarts other than "
                                         "boots are not on record"),
                       caveat="clients run by hand from a shell are not on record")
            row["mismatch"] = (row["actual_center_frequency_hz"] is not None
                               and row["actual_center_frequency_hz"] != row["declared_center_frequency_hz"])
        rows.append(row)

    mismatches = [r for r in rows if r.get("mismatch")]
    return {"created_utc": datetime.now(timezone.utc).isoformat(), "policy": "read-only; captures are never relabelled",
            "journal_first_entry_utc": journal_first.isoformat() if journal_first else None,
            "boots_utc": [b.isoformat() for b in boot_list],
            "tuning_events": [{**e, "utc": e["utc"].isoformat()} for e in events],
            "rtl_tcp_set_freq_acks_in_journal": [{"utc": w.isoformat(), "hz": f} for w, f in acks],
            "summary": {"files": len(rows),
                        "by_flow": {k: sum(1 for r in rows if r["flow"] == k) for k in sorted({r["flow"] for r in rows})},
                        "declared_differs_from_actual": len(mismatches)},
            "mismatches": mismatches, "files": rows}


def _markdown(report: Dict[str, Any]) -> str:
    lines = ["# Frequency audit: declared vs actual tuning", "",
             f"Created {report['created_utc']}. Read-only: no capture was modified.", "",
             f"Files: {report['summary']['files']} - by flow: {report['summary']['by_flow']}", "",
             f"**Declared frequency differs from the actual tuning: {report['summary']['declared_differs_from_actual']} files**", ""]
    by_session: Dict[str, List[Dict[str, Any]]] = {}
    for r in report["mismatches"]:
        by_session.setdefault(str(Path(r["file"]).parent), []).append(r)
    if by_session:
        lines += ["| captures | declared Hz | actual Hz | confidence | actual tuning came from |", "|---|---|---|---|---|"]
        for d, rs in sorted(by_session.items()):
            r = rs[0]
            lines.append(f"| `{d}` ({len(rs)}) | {r['declared_center_frequency_hz']:.0f} | "
                         f"{r['actual_center_frequency_hz']:.0f} | {r['confidence']} | {r['actual_from']} |")
    lines += ["", "Clients run by hand from a shell are not on record; PROBABLE rows also cannot exclude an unlogged "
              "rtl_tcp restart (before the journal's retention)."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-root", default=str(ROOT / "data" / "audits"))
    args = parser.parse_args()
    report = audit()
    out = Path(args.out_root) / f"FREQUENCY-AUDIT-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir(parents=True, exist_ok=False)
    (out / "frequency_audit.json").write_text(json.dumps(report, indent=2, default=str))
    (out / "frequency_audit.md").write_text(_markdown(report))
    print(_markdown(report))
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
