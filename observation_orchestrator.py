#!/usr/bin/env python3
"""Observation Orchestrator: RUN / STATUS / STOP core shared by CLI and Web.

Launches capture.py (and, if enabled, quicklook_live.py) as detached
session-leader processes (start_new_session=True, to survive an orchestrator/
API-server restart or crash — see systemd/almita-observe-api.service) —
never forking their logic. STOP signals only the exact PID it itself
launched, verified by kernel start_time + cmdline + expected mosaic_csv_path
(never a process group — capture.py's own finally: block owns stopping its
own children, e.g. RFI_REF's disposable rtl_tcp). Owns exactly one new
runtime file, data/runtime/observation_runtime.json, written via the same
runtime_state.atomic_write_json() helper capture.py/quicklook_live.py
already use. Never writes to current_session.json or almita_status.json
(those remain capture.py's / the console watcher's exclusive domain).
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import csv as csv_module

import observation_plan
import observation_preflight
import runtime_state
from sdr_capture import validate_hdf5_capture

STATES = ("PLANNED", "PREFLIGHT", "READY", "RUNNING", "STOPPING",
          "COMPLETED", "DEGRADED", "FAILED", "ABORTED")

DEFAULT_RUNTIME_DIR = str(Path(__file__).resolve().parent / "data" / "runtime")
RUNTIME_FILENAME = "observation_runtime.json"
SESSION_ANNOUNCE_TIMEOUT_SEC = 60.0
SESSION_ANNOUNCE_POLL_SEC = 1.0
STOP_WAIT_TIMEOUT_SEC = 120.0
STOP_POLL_SEC = 1.0


class OrchestratorError(RuntimeError):
    """Raised for RUN/STOP preconditions that must abort before touching hardware/processes."""


def _runtime_path(runtime_dir: str) -> Path:
    return Path(runtime_dir) / RUNTIME_FILENAME


def _write_runtime(runtime_dir: str, **fields: Any) -> Dict[str, Any]:
    path = _runtime_path(runtime_dir)
    existing = runtime_state.read_json_safe(path) or {}
    merged = {**existing, **fields, "schema_version": 1, "updated_utc": runtime_state.utcnow()}
    runtime_state.atomic_write_json(path, merged)
    return merged


def _read_runtime(runtime_dir: str) -> Optional[Dict[str, Any]]:
    return runtime_state.read_json_safe(_runtime_path(runtime_dir))


def _load_resolved_plan(resolved_plan_path: str) -> Dict[str, Any]:
    plan = json.loads(Path(resolved_plan_path).read_text(encoding="utf-8"))
    recomputed = observation_plan.recompute_config_hash(plan)
    if recomputed != plan.get("observation_config_sha256"):
        raise OrchestratorError(
            f"observation_resolved.json failed tamper/corruption check: recomputed hash "
            f"{recomputed[:16]}... does not match stored {plan.get('observation_config_sha256', '')[:16]}..."
        )
    return plan


def _check_stale(plan: Dict[str, Any], *, now: Optional[datetime] = None) -> None:
    planning_time = datetime.fromisoformat(plan["planning_timestamp_utc"].rstrip("Z")).replace(tzinfo=timezone.utc)
    budget = timedelta(minutes=plan["max_recommended_start_delay_minutes"])
    now = now or datetime.now(timezone.utc)
    if now > planning_time + budget:
        raise OrchestratorError(
            f"stale plan: planned at {plan['planning_timestamp_utc']} with a "
            f"{plan['max_recommended_start_delay_minutes']:.1f}-minute start-delay budget, "
            f"now is {now.isoformat()} — re-plan required (PLAN is not silently recomputed at RUN time)"
        )


_ACTIVE_STATES = ("PREFLIGHT", "READY", "RUNNING", "STOPPING")


@contextlib.contextmanager
def _run_lock(runtime_dir: str):
    """Cross-process advisory lock serializing run_observation() attempts.

    Closes the double-START race a pure state-file check can't: two nearly
    simultaneous requests (double-click, browser retry, a concurrent CLI
    invocation) both reading "not running" before either has written
    PREFLIGHT. Non-blocking — a second concurrent attempt fails fast with a
    clear error instead of silently queuing behind the first.
    """
    lock_path = Path(runtime_dir) / ".run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OrchestratorError(
                "another RUN attempt is currently starting (lock held) — refusing a concurrent start"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _refuse_if_already_active(runtime_dir: str) -> None:
    """Fast, clear rejection of a sequential double-START (click START, still
    RUNNING, click START again) — independent of the lock above, which only
    covers truly concurrent attempts."""
    existing = _read_runtime(runtime_dir)
    if not existing or existing.get("orchestrator_state") not in _ACTIVE_STATES:
        return
    if _capture_ownership_matches(existing):
        raise OrchestratorError(
            f"an observation is already {existing['orchestrator_state']} "
            f"(capture_pid={existing.get('capture_pid')}, session_id={existing.get('session_id')}) — "
            "refusing to start a second one; stop it first"
        )
    # Stale state (process no longer alive/matching): safe to proceed: this
    # is a crash-recovery case, not an active double-start.


def _cmdline_matches(pid: int, needle: str) -> bool:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    cmdline = raw.decode(errors="replace").replace("\x00", " ")
    return needle in cmdline


def _proc_stat_start_time(pid: int) -> Optional[int]:
    """The kernel's starttime field (jiffies since boot) for pid, from
    /proc/<pid>/stat. Immutable for the life of a pid — if a later process
    reuses the same pid, its starttime will differ, making this the
    strongest available cross-check against PID recycling (man proc(5))."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    close = raw.rfind(")")  # comm field is "(name)" and may itself contain ')' or spaces
    if close == -1:
        return None
    fields = raw[close + 1:].split()
    try:
        return int(fields[19])  # starttime is field 22 overall = fields[19] after (pid, comm) removed
    except (IndexError, ValueError):
        return None


def _process_identity(pid: int) -> Optional[Dict[str, Any]]:
    """cmdline + kernel start_time for pid, or None if it doesn't exist."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    start_time = _proc_stat_start_time(pid)
    if start_time is None:
        return None
    return {"cmdline": raw.decode(errors="replace").replace("\x00", " "), "start_time": start_time}


def _capture_ownership_matches(runtime: Dict[str, Any]) -> bool:
    """Strong ownership check before ever signaling the recorded capture_pid:
    exact PID's kernel start_time must match what was persisted at launch
    (rules out PID recycling), its cmdline must contain the expected marker
    and the exact mosaic_csv_path this orchestrator launched it with (rules
    out confusing an orchestrator-launched run with an unrelated/manually
    launched capture.py that happens to reuse the same pid)."""
    pid = runtime.get("capture_pid")
    if not pid:
        return False
    identity = _process_identity(pid)
    if identity is None:
        return False
    if identity["start_time"] != runtime.get("capture_start_time"):
        return False
    if runtime.get("capture_cmd_needle", "capture.py") not in identity["cmdline"]:
        return False
    expected_csv = runtime.get("capture_expected_csv")
    if expected_csv and expected_csv not in identity["cmdline"]:
        return False
    return True


def _quicklook_ownership_matches(runtime: Dict[str, Any]) -> bool:
    pid = runtime.get("quicklook_pid")
    if not pid:
        return False
    identity = _process_identity(pid)
    if identity is None:
        return False
    if identity["start_time"] != runtime.get("quicklook_start_time"):
        return False
    return runtime.get("quicklook_cmd_needle", "quicklook_live.py") in identity["cmdline"]


def _capture_args(plan: Dict[str, Any], runtime_dir: str) -> List[str]:
    args = [
        sys.executable, str(Path(__file__).resolve().parent / "capture.py"),
        "--csv", plan["mosaic_csv_path"],
        "--settle", str(plan["requested"]["capture"]["settle_seconds"]),
        "--capture", str(plan["requested"]["capture"]["seconds"]),
        "--sdr-freq", str(plan["main"]["center_frequency_hz"]),
        "--sdr-rate", str(plan["main"]["sample_rate"]),
        "--sdr-gain", str(plan["main"]["gain_db"]),
        "--input-topology", "antenna",
        "--min-altitude", str(plan["requested"]["grid"]["min_altitude_deg"]),
        "--runtime-dir", runtime_dir,
    ]
    if plan["rfi_ref"]["enabled"]:
        args += [
            "--rfi-ref-enabled",
            "--rfi-ref-gain-db", str(plan["rfi_ref"]["gain_db"]),
            "--rfi-ref-serial", plan["rfi_ref"]["serial"],
        ]
    return args


def _quicklook_args(plan: Dict[str, Any], session_dir: str, output_dir: str, runtime_dir: str) -> List[str]:
    return [
        sys.executable, str(Path(__file__).resolve().parent / "quicklook_live.py"),
        "--session-dir", session_dir,
        "--calibration-profile", plan["quicklook"]["calibration_profile_path"],
        "--output-dir", output_dir,
        "--runtime-dir", runtime_dir,
    ]


def run_observation(resolved_plan_path: str, *, yes: bool = False, runtime_dir: str = DEFAULT_RUNTIME_DIR,
                     host: str = "localhost", port: int = 7624, device_name: Optional[str] = None,
                     confirm_prompt=input) -> Dict[str, Any]:
    """RUN: validate, re-preflight, require GO, launch detached children. Returns orchestrator state."""
    with _run_lock(runtime_dir):
        return _run_observation_locked(resolved_plan_path, yes=yes, runtime_dir=runtime_dir, host=host,
                                        port=port, device_name=device_name, confirm_prompt=confirm_prompt)


def _run_observation_locked(resolved_plan_path: str, *, yes: bool, runtime_dir: str, host: str, port: int,
                             device_name: Optional[str], confirm_prompt) -> Dict[str, Any]:
    _refuse_if_already_active(runtime_dir)
    plan = _load_resolved_plan(resolved_plan_path)
    _check_stale(plan)

    _write_runtime(runtime_dir, orchestrator_state="PREFLIGHT", resolved_plan_path=str(resolved_plan_path),
                    observation_name=plan["observation_name"])

    report = asyncio.run(observation_preflight.run_execution_preflight(
        plan, host=host, port=port, device_name=device_name, runtime_dir=runtime_dir,
    ))
    if report["overall"] == "BLOCK":
        blockers = [c for c in report["checks"] if c["status"] == "BLOCK"]
        _write_runtime(runtime_dir, orchestrator_state="FAILED", preflight=report)
        raise OrchestratorError(
            "preflight BLOCKED, no processes launched: " +
            "; ".join(f"{c['name']}: {c['detail']}" for c in blockers)
        )

    if report["overall"] == "WARNING" and not yes:
        print("Preflight warnings:")
        for c in report["checks"]:
            if c["status"] == "WARNING":
                print(f"  [WARNING] {c['name']}: {c['detail']}")
        typed = confirm_prompt("Type GO to proceed, anything else to abort: ")
        if typed.strip() != "GO":
            _write_runtime(runtime_dir, orchestrator_state="ABORTED", preflight=report)
            raise OrchestratorError("operator did not confirm GO; aborted, no processes launched")

    _write_runtime(runtime_dir, orchestrator_state="READY", preflight=report)

    log_dir = Path(plan["grid_session_dir"])
    capture_log = log_dir / "orchestrator_capture.log"
    with capture_log.open("wb") as handle:
        capture_proc = _popen_detached(_capture_args(plan, runtime_dir), handle)
    # Read identity immediately after Popen() returns: the kernel assigns
    # starttime at fork(), before exec() — safe to read right away, and
    # this is the value STOP will later verify against to rule out PID
    # recycling (see _capture_ownership_matches).
    capture_start_time = _proc_stat_start_time(capture_proc.pid)

    _write_runtime(
        runtime_dir, orchestrator_state="RUNNING",
        capture_pid=capture_proc.pid, capture_start_time=capture_start_time,
        capture_cmd_needle="capture.py", capture_expected_csv=plan["mosaic_csv_path"],
        capture_log=str(capture_log),
        preflight=report, started_utc=runtime_state.utcnow(),
    )

    announced = _wait_for_session_announcement(runtime_dir, timeout=SESSION_ANNOUNCE_TIMEOUT_SEC)
    if announced is None:
        _write_runtime(runtime_dir, orchestrator_state="DEGRADED",
                        note="capture.py did not announce current_session.json in time; "
                             "quicklook_live.py was not started if enabled (MAIN unaffected)")
        return _read_runtime(runtime_dir)

    _write_runtime(runtime_dir, session_id=announced.get("session_id"))

    if plan["quicklook"]["enabled"]:
        quicklook_output_dir = str(log_dir / "quicklook")
        quicklook_log = log_dir / "orchestrator_quicklook.log"
        with quicklook_log.open("wb") as handle:
            quicklook_proc = _popen_detached(
                _quicklook_args(plan, announced["session_root"], quicklook_output_dir, runtime_dir), handle,
            )
        _write_runtime(runtime_dir, quicklook_pid=quicklook_proc.pid,
                        quicklook_start_time=_proc_stat_start_time(quicklook_proc.pid),
                        quicklook_log=str(quicklook_log), quicklook_cmd_needle="quicklook_live.py")

    return _read_runtime(runtime_dir)


def _popen_detached(args: List[str], log_handle) -> subprocess.Popen:
    return subprocess.Popen(args, stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True)


def _wait_for_session_announcement(runtime_dir: str, *, timeout: float) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = runtime_state.read_json_safe(Path(runtime_dir) / "current_session.json")
        if current and current.get("session_root"):
            return current
        time.sleep(SESSION_ANNOUNCE_POLL_SEC)
    return None


def get_status(*, runtime_dir: str = DEFAULT_RUNTIME_DIR) -> Dict[str, Any]:
    """STATUS: read-only merge of orchestrator state + canonical acquisition/instrument truth."""
    orchestrator = _read_runtime(runtime_dir) or {"orchestrator_state": "PLANNED"}
    current_session = runtime_state.read_json_safe(Path(runtime_dir) / "current_session.json")
    almita_status = runtime_state.read_json_safe(Path(runtime_dir) / "almita_status.json")
    return {
        "orchestrator": orchestrator,
        "current_session": current_session,
        "almita_status": almita_status,
    }


def stop_observation(*, runtime_dir: str = DEFAULT_RUNTIME_DIR, confirm: bool = False) -> Dict[str, Any]:
    """STOP: SIGINT to the exact capture.py PID only — never killpg, never SIGTERM/SIGKILL.

    capture.py's own finally: block owns stopping its own children (e.g.
    RFI_REF's disposable rtl_tcp via RFIReferenceMonitor.stop()) — the
    orchestrator never signals them directly, it only ever signals the one
    PID it itself launched and can still verify by kernel start_time +
    cmdline + expected mosaic_csv_path (see _capture_ownership_matches).
    """
    runtime = _read_runtime(runtime_dir)
    if not runtime or not runtime.get("capture_pid"):
        raise OrchestratorError("no orchestrator-owned observation is currently running")

    capture_pid = runtime["capture_pid"]
    if not _capture_ownership_matches(runtime):
        _write_runtime(runtime_dir, orchestrator_state="DEGRADED",
                        note=f"pid {capture_pid} no longer matches the exact process this orchestrator "
                             "launched (start-time/cmdline/csv-path mismatch) — refusing to signal an "
                             "unverified PID; possible PID recycling or an unrelated capture.py")
        return _read_runtime(runtime_dir)

    _write_runtime(runtime_dir, orchestrator_state="STOPPING")
    try:
        os.kill(capture_pid, signal.SIGINT)  # exact PID, never killpg
    except ProcessLookupError:
        pass

    if runtime.get("quicklook_pid") and _quicklook_ownership_matches(runtime):
        try:
            os.kill(runtime["quicklook_pid"], signal.SIGINT)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + STOP_WAIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if not _pid_alive(capture_pid):
            current = runtime_state.read_json_safe(Path(runtime_dir) / "current_session.json")
            final_state = (current or {}).get("state")
            resolved_state = "COMPLETED" if final_state == "COMPLETED" else "ABORTED"
            return _write_runtime(runtime_dir, orchestrator_state=resolved_state)
        time.sleep(STOP_POLL_SEC)

    # Timeout: never escalate to SIGKILL. Report DEGRADED and hand off to the operator.
    return _write_runtime(
        runtime_dir, orchestrator_state="DEGRADED",
        note=f"capture.py (pid {capture_pid}) did not exit within {STOP_WAIT_TIMEOUT_SEC:.0f}s of SIGINT; "
             "orchestrator will not escalate to SIGKILL — operator must intervene manually",
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def generate_final_report(session_dir: str) -> Dict[str, Any]:
    """Write final_report.json/.md into the session directory. Reads mosaic.csv + sidecar JSON only."""
    session_path = Path(session_dir)
    mosaic_csv = session_path / "mosaic.csv"
    resolved_path = session_path / "observation_resolved.json"
    runtime_dir = DEFAULT_RUNTIME_DIR

    resolved = json.loads(resolved_path.read_text()) if resolved_path.exists() else {}
    with mosaic_csv.open("r", newline="") as handle:
        rows = list(csv_module.DictReader(handle))

    counts = {"success": 0, "failed": 0, "deferred": 0, "planned": 0}
    hdf5_valid, hdf5_invalid = 0, 0
    for row in rows:
        status = row.get("capture_status", "")
        if row.get("visibility_deferred") == "True":
            counts["deferred"] += 1
        elif status in counts:
            counts[status] += 1
        data_filename = row.get("data_filename", "")
        if status == "success" and data_filename:
            h5_path = session_path / "data" / "iq" / Path(data_filename).with_suffix(".h5").name
            try:
                validate_hdf5_capture(h5_path)
                hdf5_valid += 1
            except Exception:
                hdf5_invalid += 1

    rfi_ref_status = runtime_state.read_json_safe(Path(runtime_dir) / "rfi_ref_status.json")
    quicklook_status = runtime_state.read_json_safe(session_path / "quicklook" / "quicklook_live_status.json")
    orchestrator_runtime = _read_runtime(runtime_dir)

    report = {
        "schema_version": 1,
        "observation_name": resolved.get("observation_name"),
        "session_id": (orchestrator_runtime or {}).get("session_id"),
        "observation_config_sha256": resolved.get("observation_config_sha256"),
        "requested": resolved.get("requested"),
        "resolved": resolved.get("resolved"),
        "points_planned": len(rows),
        "points_success": counts["success"],
        "points_failed": counts["failed"],
        "points_deferred": counts["deferred"],
        "hdf5_valid": hdf5_valid,
        "hdf5_invalid": hdf5_invalid,
        "quicklook_status": quicklook_status,
        "rfi_ref_status": rfi_ref_status,
        "orchestrator_final_state": (orchestrator_runtime or {}).get("orchestrator_state"),
        "generated_utc": runtime_state.utcnow(),
        "final_verdict": "SUCCESS" if counts["failed"] == 0 and hdf5_invalid == 0 else "DEGRADED",
    }

    (session_path / "final_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_lines = [
        f"# Final Report: {report['observation_name']}", "",
        f"- session_id: {report['session_id']}",
        f"- config hash: {report['observation_config_sha256']}",
        f"- points planned/success/failed/deferred: "
        f"{report['points_planned']}/{report['points_success']}/{report['points_failed']}/{report['points_deferred']}",
        f"- HDF5 valid/invalid: {report['hdf5_valid']}/{report['hdf5_invalid']}",
        f"- orchestrator final state: {report['orchestrator_final_state']}",
        f"- verdict: {report['final_verdict']}",
    ]
    (session_path / "final_report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return report
