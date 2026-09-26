"""REAL operations for the :8090 web app.

This module launches the EXISTING ALMITA command lines as detached subprocesses and reports what they really did. It reimplements nothing:

    ALIGN      alignment.py                      (real INDI + real MAIN SDR; --no-sync always; --dry-run = the plan preview)
    CALIBRATE  calibration_operational_realtest.py (real MAIN captures, read-only receiver config, no mount movement)
    REDUCE     almita_reduce.py  plan | run       (REDUCE V1, frozen)
    SCIENCE    almita_science.py plan | run       (SCIENCE V1, frozen)
    OBSERVE    stays in observation_orchestrator (its own routes in almita_orchestrator_server.py)

A job = one directory data/runtime/web_ops/<job_id>/ (job.json + job.log). The subprocess is started through a tiny detached runner
(this same file, `--runner`) so it survives a restart of the web service and its exit code is recorded on disk. Verdicts (PASS / PARTIAL / FAIL)
come ONLY from the real exit code and the real result artifacts the command wrote - never from the browser. Stop = SIGINT (never SIGTERM/SIGKILL).
Physical stages need the operator's typed confirmation and a fresh real preflight. Nothing here ever sends SYNC, PARK or a tracking change.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
OPS_DIR = ROOT / "data" / "runtime" / "web_ops"
PY = sys.executable
DATA = ROOT / "data"
SERVE_ROOTS = {"mosaic": DATA / "mosaic", "reduced": DATA / "reduced", "science": DATA / "science", "alignment": DATA / "alignment",
               "calibration": DATA / "calibration", "web_ops": OPS_DIR}
SERVE_SUFFIXES = {".png": "image/png", ".json": "application/json", ".csv": "text/csv", ".txt": "text/plain", ".log": "text/plain", ".md": "text/plain"}
MAX_SERVE_BYTES = 25 * 1024 * 1024
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
DEVICE = "LX200 OnStep"


class OpsBlocked(Exception):
    """A real precondition failed (conflict, preflight BLOCK, missing confirmation): HTTP 409, nothing was started."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _within(path: Path, root: Path) -> bool:
    try:
        path = path.resolve()
        return path == root.resolve() or root.resolve() in path.parents
    except (OSError, RuntimeError):
        return False


# ------------------------------------------------------------------ real read-only mount read (getProperties only)
def read_mount(host: str = "localhost", port: int = 7624, timeout: float = 3.0) -> Dict[str, Any]:
    """Real, read-only: sends ONLY <getProperties> to the INDI server and parses the mount's answers. Never writes a property."""
    out: Dict[str, Any] = {"read_utc": _utc()}
    try:
        s = socket.create_connection((host, port), 2.0)
    except OSError as exc:
        return {**out, "error": f"INDI not reachable: {exc}"}
    props: Dict[str, Dict[str, Any]] = {}
    try:
        s.settimeout(0.4)
        for name in ("CONNECTION", "EQUATORIAL_EOD_COORD", "TELESCOPE_TRACK_STATE", "TELESCOPE_TRACK_MODE", "TELESCOPE_PARK", "TELESCOPE_PIER_SIDE", "OnStep Status", "TIME_UTC"):
            s.sendall(f'<getProperties version="1.7" device="{DEVICE}" name="{name}"/>'.encode())
        buf, end = "", time.monotonic() + timeout
        while time.monotonic() < end and not {"EQUATORIAL_EOD_COORD", "OnStep Status", "TELESCOPE_TRACK_STATE", "TELESCOPE_PARK"} <= set(props):
            try:
                d = s.recv(65536)
            except socket.timeout:
                continue
            if not d:
                break
            buf += d.decode("utf8", "replace")
            for m in re.finditer(r"<(def|set)(Number|Switch|Text)Vector\b.*?</\1\2Vector>", buf, re.S):
                try:
                    r = ET.fromstring(m.group(0))
                except ET.ParseError:
                    continue
                if r.attrib.get("device") == DEVICE:
                    props[r.attrib.get("name")] = {"state": r.attrib.get("state"), "el": {c.attrib.get("name"): (c.text or "").strip() for c in r}}
            buf = buf[-2000:] if len(buf) > 2000 else buf
    finally:
        s.close()
    if "EQUATORIAL_EOD_COORD" not in props:
        return {**out, "error": "no EQUATORIAL_EOD_COORD from the mount (driver not connected?)"}
    eod, st = props["EQUATORIAL_EOD_COORD"], props.get("OnStep Status", {}).get("el", {})
    track_mode_el = props.get("TELESCOPE_TRACK_MODE", {}).get("el", {})
    track_mode_on = [name for name, switch in (("sidereal", "TRACK_SIDEREAL"), ("solar", "TRACK_SOLAR"), ("lunar", "TRACK_LUNAR"), ("custom", "TRACK_CUSTOM")) if track_mode_el.get(switch) == "On"]
    return {**out, "connected": props.get("CONNECTION", {}).get("el", {}).get("CONNECT") == "On" if "CONNECTION" in props else None,
            "ra_h": float(eod["el"]["RA"]), "dec_deg": float(eod["el"]["DEC"]), "eod_state": eod["state"],
            "tracking": ("on" if props.get("TELESCOPE_TRACK_STATE", {}).get("el", {}).get("TRACK_ON") == "On" else "off") if "TELESCOPE_TRACK_STATE" in props else None,
            "track_mode": (track_mode_on[0] if len(track_mode_on) == 1 else "unknown") if "TELESCOPE_TRACK_MODE" in props else None,   # read-only; the operator cannot set this from the web
            "parked": (props.get("TELESCOPE_PARK", {}).get("el", {}).get("PARK") == "On") if "TELESCOPE_PARK" in props else None,
            "pier": [k for k, v in props.get("TELESCOPE_PIER_SIDE", {}).get("el", {}).items() if v == "On"],
            "mount_state": st.get("Tracking"), "onstep_error": st.get("Error"), "onstep_time_utc": props.get("TIME_UTC", {}).get("el", {}).get("UTC")}


def mount_idle_problems(m: Dict[str, Any]) -> List[str]:
    if m.get("error"):
        return [m["error"]]
    p = []
    if m.get("eod_state") not in ("Idle", "Ok"): p.append(f"mount coordinates state {m.get('eod_state')}")
    if m.get("mount_state") not in (None, "Idle"): p.append(f"mount state {m.get('mount_state')} (must be Idle)")
    if m.get("parked"): p.append("mount is parked")
    if m.get("onstep_error") not in (None, "None", "Goto No Error"): p.append(f"OnStep error {m.get('onstep_error')}")
    return p


# ------------------------------------------------------------------ real preflight (read-only)
def _check(name: str, status: str, detail: str, category: str) -> Dict[str, str]:
    return {"name": name, "status": status, "detail": detail, "category": category}


def preflight() -> Dict[str, Any]:
    """PASS / WARNING / BLOCK from the REAL system: services, INDI, mount, MAIN SDR, storage, clock, configuration, frozen-module integrity."""
    import almita_web_system
    checks: List[Dict[str, str]] = []
    h = almita_web_system.collect_health()
    for name, svc in h["services"].items():
        checks.append(_check(f"service {name}", "PASS" if svc["state"] == "UP" else "WARNING", f"{svc['state']}: {svc['detail']}", "services"))
    deps = h["dependencies"]
    checks.append(_check("INDI server", "PASS" if deps["indi"]["state"] == "UP" else "BLOCK", deps["indi"]["detail"], "hardware"))
    mount = read_mount() if deps["indi"]["state"] == "UP" else {"error": "INDI server not listening"}
    if mount.get("error"):
        checks.append(_check("mount (LX200 OnStep)", "BLOCK", mount["error"], "hardware"))
    else:
        probs = mount_idle_problems(mount)
        checks.append(_check("mount (LX200 OnStep)", "BLOCK" if probs else "PASS",
                             (f"RA {mount['ra_h']:.5f} h  Dec {mount['dec_deg']:.4f} deg  tracking={mount['tracking']}  park={'PARKED' if mount['parked'] else 'unparked'}  "
                              f"state={mount['mount_state']}  error={mount['onstep_error']}") + (f"  PROBLEMS: {probs}" if probs else ""), "hardware"))
    sdr = deps["main_sdr"]
    checks.append(_check("MAIN SDR (rtl_tcp)", {"AVAILABLE": "PASS", "BUSY": "WARNING"}.get(sdr["state"], "BLOCK"),
                         f"{sdr['state']}: {sdr['detail']}; USB 00000001 {'present' if sdr.get('usb_serial_00000001_present') else 'NOT seen'}", "hardware"))
    rfi = deps["rfi_sdr"]
    checks.append(_check("RFI_REF SDR (optional)", "PASS" if rfi["state"] == "AVAILABLE" else "WARNING", f"{rfi['state']}: {rfi['detail']}", "hardware"))
    du = shutil.disk_usage(DATA)
    free_gb = du.free / 1e9
    checks.append(_check("storage (data/)", "BLOCK" if free_gb < 5 else ("WARNING" if free_gb < 20 else "PASS"), f"{free_gb:.1f} GB free of {du.total / 1e9:.0f} GB", "storage"))
    try:
        mem = {k: int(v.split()[0]) for k, v in (l.split(":", 1) for l in open("/proc/meminfo"))}
        checks.append(_check("memory", "WARNING" if mem["MemAvailable"] < 800_000 else "PASS", f"MemAvailable {mem['MemAvailable'] // 1024} MB, SwapFree {mem['SwapFree'] // 1024} MB", "storage"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("memory", "WARNING", f"unreadable: {exc}", "storage"))
    try:
        td = dict(l.split("=", 1) for l in subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "-p", "Timezone"], capture_output=True, text=True, timeout=5).stdout.split() if "=" in l)
        synced = td.get("NTPSynchronized") == "yes"
        checks.append(_check("system clock", "PASS" if synced else "WARNING", f"NTPSynchronized={td.get('NTPSynchronized', 'unknown')} (timezone {td.get('Timezone', '?')})", "clock"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("system clock", "WARNING", f"timedatectl unavailable: {exc}", "clock"))
    if mount.get("onstep_time_utc"):
        try:
            delta = datetime.fromisoformat(mount["onstep_time_utc"]).replace(tzinfo=timezone.utc).timestamp() - time.time()
            checks.append(_check("OnStep TIME_UTC vs system", "WARNING" if abs(delta) > 60 else "PASS",
                                 f"OnStep {mount['onstep_time_utc']} is {delta / 3600:+.2f} h from the system clock (system clock is the authority; not modified)", "clock"))
        except ValueError:
            pass
    try:
        cfg = json.loads((ROOT / "observer_config.json").read_text())
        obs = cfg.get("observer", {})
        checks.append(_check("observer_config.json", "PASS", f"{obs.get('name')} lat {obs.get('latitude_deg')} lon {obs.get('longitude_deg')}", "config"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("observer_config.json", "BLOCK", f"unreadable: {exc}", "config"))
    try:
        from alignment_engine.deployment_state import DEFAULT_STATE_PATH, read_current_deployment_state
        dep = read_current_deployment_state(DEFAULT_STATE_PATH)
        checks.append(_check("deployment state", "PASS", f"{dep.state.value if dep else 'UNKNOWN/not recorded'} (informational)", "config"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("deployment state", "WARNING", f"unreadable: {exc}", "config"))
    try:
        frozen = ["reduce_engine", "science_engine", "almita_reduce.py", "almita_science.py", "capture.py", "sdr_capture.py", "indi_telescope_control.py"]
        dirty = subprocess.run(["git", "status", "--porcelain", "--", *frozen], cwd=str(ROOT), capture_output=True, text=True, timeout=10).stdout.strip()
        checks.append(_check("frozen modules unchanged", "WARNING" if dirty else "PASS", ("modified: " + dirty.replace("\n", "; ")) if dirty else "REDUCE/SCIENCE/capture/SDR/INDI files match HEAD", "config"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("frozen modules unchanged", "WARNING", f"git unavailable: {exc}", "config"))
    active = [j for j in list_jobs(limit=50) if j["state"] == "RUNNING"]
    checks.append(_check("web-launched jobs", "PASS" if not active else "WARNING", "none running" if not active else "running: " + ", ".join(f"{j['stage']}:{j['job_id']}" for j in active), "services"))
    overall = "BLOCK" if any(c["status"] == "BLOCK" for c in checks) else ("WARNING" if any(c["status"] == "WARNING" for c in checks) else "PASS")
    return {"generated_utc": _utc(), "overall": overall, "checks": checks, "mount": mount, "operational": h["operational"]}


# ------------------------------------------------------------------ stages -> the real command lines
STAGES: Dict[str, Dict[str, Any]] = {
    "align_plan": {"physical": False, "resources": ()},                      # alignment.py --dry-run: resolves the region and the pattern, moves nothing
    "align": {"physical": True, "resources": ("mount", "sdr")},
    "calibrate": {"physical": False, "resources": ("sdr",)},
    # calibrate_wizard: one stage, many NON-MOVING actions (start/set_reference/skip_reference/capture_50r/next/
    # confirm_antenna/plan_hi/approve_hi_plan/abort/finish/status) - every web click is one short
    # calibrate_reference_wizard.py invocation against a session dir it persists to disk between clicks. "sdr"
    # resource claim covers every action (even the non-capturing ones) so only one wizard step can be mid-flight
    # at a time and it can never overlap a real align/calibrate run on MAIN.
    "calibrate_wizard": {"physical": False, "resources": ("sdr",)},
    # calibrate_wizard_move: the ONE action (capture_hi) that does a real GOTO for the HI ALTO/HI BAJO step -
    # physical=True gives it the SAME real-preflight + typed-MOVE-confirmation gate as ALIGN's real RUN and
    # OBSERVE's gain-pilot capture (reused unmodified below in start()); never reachable while the 50 ohm
    # terminator could still be connected (calibrate_reference_wizard.py's own state machine enforces that
    # structurally, not just by convention here).
    "calibrate_wizard_move": {"physical": True, "resources": ("mount", "sdr")},
    # OBSERVE's optional gain-pilot stage: PLAN never moves anything; the actual pilot captures need a real
    # GOTO (physical=True, same real-preflight + typed MOVE confirmation gate as ALIGN's real RUN); the small
    # admin actions (approve a gain value, abort, poll status) touch neither the mount nor the SDR.
    "observe_gain_pilot_plan": {"physical": False, "resources": ()},
    "observe_gain_pilot_capture": {"physical": True, "resources": ("mount", "sdr")},
    "observe_gain_pilot_admin": {"physical": False, "resources": ()},
    "reduce_plan": {"physical": False, "resources": ("cpu",)},
    "reduce": {"physical": False, "resources": ("cpu",)},
    # reduce_single_capture.py bridges the ONE gap almita_reduce.py itself has (it only reads whole OBSERVE
    # campaigns via mosaic.csv): reducing one standalone HDF5 capture that isn't part of a grid campaign. Same
    # frozen reduce_engine functions underneath (see reduce_single_capture.py's own docstring) - never a second
    # reduction algorithm.
    "reduce_capture_plan": {"physical": False, "resources": ("cpu",)},
    "reduce_capture": {"physical": False, "resources": ("cpu",)},
    "science_plan": {"physical": False, "resources": ("cpu",)},
    "science": {"physical": False, "resources": ("cpu",)},
}


def _float(p: Dict[str, Any], key: str, lo: float, hi: float, default: Optional[float] = None) -> Optional[float]:
    v = p.get(key, default)
    if v is None or v == "":
        return default
    v = float(v)
    if not (lo <= v <= hi) or v != v:
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
    return v


def _path_in(p: Dict[str, Any], key: str, root: Path) -> Path:
    raw = p.get(key)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError(f"{key} (string) is required")
    cand = Path(raw)
    cand = cand if cand.is_absolute() else ROOT / cand
    if not _within(cand, root) or not cand.is_dir():
        raise ValueError(f"{key} must be an existing directory inside {root.relative_to(ROOT)}")
    return cand.resolve()


def _file_in(p: Dict[str, Any], key: str, roots: Tuple[Path, ...]) -> Path:
    """Like _path_in but for a FILE (e.g. one HDF5 capture) allowed under any of several roots - a standalone
    capture worth reducing on its own can live under a grid campaign (data/mosaic) or a CALIBRATE session
    (data/calibration), never assumed to be in only one of them."""
    raw = p.get(key)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError(f"{key} (string) is required")
    cand = Path(raw)
    cand = cand if cand.is_absolute() else ROOT / cand
    if not any(_within(cand, r) for r in roots) or not cand.is_file():
        names = ", ".join(str(r.relative_to(ROOT)) for r in roots)
        raise ValueError(f"{key} must be an existing file inside one of: {names}")
    return cand.resolve()


def _ring_pattern(p: Dict[str, Any]) -> Optional[Tuple[List[float], int]]:
    """Ring pattern override for ALIGN (center + N rings of the SAME point count each), as alignment.py's own
    multiscale_pattern() shapes it. None -> the caller passes no flags and alignment.py keeps its own default
    (5.0, 2.0, 0.6 deg / 16 points, the original fixed pattern). alignment.py re-validates independently; this
    only catches obviously bad input before a real process is spawned, and bounds it to a sane exploration range."""
    radii, points = p.get("ring_radii"), p.get("ring_points")
    if radii is None and points is None:
        return None
    if not isinstance(radii, list) or not (1 <= len(radii) <= 8):
        raise ValueError("ring_radii must be a list of 1 to 8 degree values")
    try:
        radii = [float(r) for r in radii]
    except (TypeError, ValueError):
        raise ValueError("ring_radii must all be numbers")
    if any(not math.isfinite(r) or r <= 0 or r > 45 for r in radii):
        raise ValueError("each ring_radii value must be a finite degree > 0 and <= 45")
    if len(set(round(r, 9) for r in radii)) != len(radii):
        raise ValueError("ring_radii must not repeat the same radius twice")
    points = int(points if points is not None else 16)
    if not (3 <= points <= 64):
        raise ValueError("ring_points must be between 3 and 64")
    return radii, points


def _file_signature(path: Path) -> Dict[str, Any]:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _reduce_input_signatures(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Cheap (size, mtime_ns) signatures for exactly the real files a REDUCE PLAN/RUN identity (meta: the
    same dict build_command() returns and job.json persists) points at right now - a capture (or every
    accepted point's capture, for a campaign) and the calibration profile's .json+.npz, if any. Used to
    detect a file being replaced or edited between PLAN and RUN, without hashing every raw IQ capture's full
    content (a 10+ MB file each, for a large campaign - see reduce_calibration_record.py's own docstring for
    the same tradeoff). Never a partial/best-effort result: a missing campaign/capture/profile file raises,
    same as it would when almita_reduce.py/reduce_single_capture.py themselves try to read it."""
    sigs: Dict[str, Any] = {"points": {}, "profile": None}
    if meta.get("campaign_dir"):
        from reduce_engine.ingest import discover_campaign
        camp = (ROOT / meta["campaign_dir"]).resolve()
        manifest = discover_campaign(camp)
        for pt in manifest.accepted_points():
            sigs["points"][str(pt.point_index)] = _file_signature(pt.resolved_path) if pt.resolved_path else None
    elif meta.get("capture"):
        cap = (ROOT / meta["capture"]).resolve()
        sigs["points"]["0"] = _file_signature(cap)
    if meta.get("calibration_profile"):
        pp = (ROOT / meta["calibration_profile"]).resolve()
        sigs["profile"] = {"json": _file_signature(pp.with_suffix(".json")), "npz": _file_signature(pp.with_suffix(".npz"))}
    return sigs


def _require_fresh_plan(run_stage: str, run_meta: Dict[str, Any], p: Dict[str, Any]) -> None:
    """Server-side PLAN-before-RUN gate for REDUCE. almita_reduce.py/reduce_single_capture.py have no
    ALIGN-style --approved-plan flag to re-validate (PLAN writes nothing to disk - see their own docstrings),
    so the check lives entirely here: RUN must name a real PLAN job (plan_job_id, the job_id a prior PLAN
    call returned), that job must have EXITED with blocked=False, its recorded input identity/parameters
    (meta) must match this RUN's byte for byte, AND the real files that identity points at (every accepted
    point's capture, plus the calibration profile) must not have changed since PLAN computed their
    signatures - a path staying the same says nothing about its content staying the same. Without this, only
    the browser's own JS staleness check (paramsEqual in reduce.js) enforced replanning - real, but
    bypassable by any direct HTTP call, and blind to a file being edited/replaced in place - which is exactly
    what this closes."""
    plan_stage = {"reduce": "reduce_plan", "reduce_capture": "reduce_capture_plan"}[run_stage]
    job_id = p.get("plan_job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("plan_job_id (string) is required: RUN must reference a PLAN job with these exact "
                         "same inputs and parameters - PLAN first, then RUN")
    try:
        j = _load(job_id)
    except (OSError, ValueError):
        raise ValueError(f"plan_job_id {job_id!r} does not refer to a known job - PLAN again")
    if j.get("stage") != plan_stage:
        raise ValueError(f"plan_job_id {job_id!r} is a {j.get('stage')!r} job, not {plan_stage!r} - PLAN again")
    if _state(j) != "EXITED":
        raise ValueError(f"plan_job_id {job_id!r} has not finished (state={_state(j)}) - wait for PLAN to "
                         "finish, or PLAN again")
    facts = classify(j).get("facts") or {}
    if facts.get("blocked", True):
        raise ValueError(f"plan_job_id {job_id!r} was BLOCKED - resolve the blocking checks and PLAN again")
    plan_meta = j.get("meta") or {}
    if plan_meta != run_meta:
        raise ValueError("inputs or parameters changed since that PLAN "
                         f"(planned: {plan_meta}, now: {run_meta}) - PLAN again")
    plan_sigs = facts.get("file_signatures")
    try:
        current_sigs = _reduce_input_signatures(run_meta)
    except (OSError, FileNotFoundError, ValueError) as exc:
        raise ValueError(f"could not re-check the campaign/capture files that PLAN reviewed: {exc} - PLAN again")
    if plan_sigs is None or plan_sigs != current_sigs:
        raise ValueError(
            "the capture file(s) and/or calibration profile reviewed by that PLAN have changed on disk since "
            "(a file was added, removed, replaced, or its content edited) - a preview and PLAN reflect what "
            "was on disk when they ran, not necessarily what is there now: request a fresh preview and PLAN "
            "before RUN."
        )


def _require_uncalibrated_confirmation(campaign_rel: str, profile_rel: str, p: Dict[str, Any]) -> None:
    """Server-side gate for RUNning a campaign whose profile is not COMPATIBLE with every accepted point.
    The campaign is still allowed to run (this is a real, existing, supported mode: incompatible/unverifiable
    points are reduced UNCALIBRATED by reduce_engine.calibration.apply_calibration() itself, never partially
    corrected) - but only after the operator has explicitly, and typedly, acknowledged it. Computed fresh
    here (not trusted from an earlier PLAN or from the browser's own count) via the SAME per-point preview the
    UI shows before RUN (reduce_campaign_calibration_preview) - so what gates RUN and what the operator saw are
    always the same real check, never a stale or client-reported one."""
    preview = reduce_campaign_calibration_preview(campaign_rel, profile_rel)
    if preview["requires_confirmation"]:
        token = p.get("confirm_uncalibrated")
        if token != "RUN UNCALIBRATED":
            c = preview["counts"]
            raise ValueError(
                f"this campaign's calibration profile is COMPATIBLE with only {c.get('COMPATIBLE', 0)} of "
                f"{preview['total_accepted_points']} accepted points ({c.get('INCOMPATIBLE', 0)} incompatible, "
                f"{c.get('UNKNOWN', 0)} unverifiable) - those points will be reduced UNCALIBRATED, the profile "
                "will NOT be partially applied to them. To proceed anyway, resend with confirm_uncalibrated = "
                "\"RUN UNCALIBRATED\" (exact text) - the operator's explicit, typed acknowledgement."
            )


def build_command(stage: str, p: Dict[str, Any], job_id: str) -> Tuple[List[str], Dict[str, Any]]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if stage in ("align_plan", "align"):
        ref = str(p.get("reference", "auto"))
        if ref not in ("auto", "sun", "hi"):
            raise ValueError("reference must be auto, sun or hi")
        out = f"data/alignment/WEB{'PLAN' if stage == 'align_plan' else ''}-{ref.upper()}-{stamp}"
        argv = [PY, "alignment.py", "--reference", ref, "--no-sync", "--output-dir", out]
        if stage == "align_plan":
            argv.append("--dry-run")
        for key, flag, lo, hi in (("gain", "--gain", 0, 50), ("capture_time", "--capture-time", 0.5, 120), ("min_elevation", "--min-elevation", 5, 89),
                                  ("settle", "--settle", 0, 60), ("beam_fwhm", "--beam-fwhm", 1, 60)):
            v = _float(p, key, lo, hi)
            if v is not None:
                argv += [flag, str(v)]
        ring = _ring_pattern(p)
        meta = {"output_dir": out, "reference": ref}
        if ring is not None:
            radii, points = ring
            argv += ["--ring-radii", ",".join(str(r) for r in radii), "--ring-points", str(points)]
            meta["ring_radii_deg"] = radii
            meta["ring_points"] = points
            meta["total_positions"] = 1 + len(radii) * points
        if stage == "align_plan" and ref == "hi" and p.get("center_ra_hours") is not None and p.get("center_dec_deg") is not None:
            # The operator picked this HI area visually (A/B/C from the sky view, or its own coordinates) -
            # PLAN must use it exactly, never let choose_hi_region() pick a different one silently.
            ra = _float(p, "center_ra_hours", 0, 24)
            dec = _float(p, "center_dec_deg", -90, 90)
            argv += ["--center-ra-hours", str(ra), "--center-dec-deg", str(dec)]
            meta["operator_selected_center"] = {"ra_hours": ra, "dec_deg": dec}
        if stage == "align":
            # RUN must always point at an already-approved PLAN (align_plan) output directory - this is the fix for
            # the PLAN-vs-RUN center mismatch: RUN never calls resolve_reference()/choose_hi_region() itself again.
            plan_dir = _path_in(p, "approved_plan_dir", SERVE_ROOTS["alignment"])
            approved = plan_dir / "alignment_result.json"
            if not approved.is_file():
                raise ValueError("approved_plan_dir has no alignment_result.json - PLAN first")
            argv += ["--approved-plan", str(approved.relative_to(ROOT))]
            meta["approved_plan_dir"] = str(plan_dir.relative_to(ROOT))
        return argv, meta
    if stage == "calibrate":
        n, secs = int(_float(p, "n_captures", 1, 20, 5)), _float(p, "capture_seconds", 0.5, 30, 2.0)
        return [PY, "calibration_operational_realtest.py", "--n-captures", str(n), "--capture-seconds", str(secs)], {"n_captures": n, "capture_seconds": secs}
    if stage == "calibrate_wizard":
        action = str(p.get("action", ""))
        valid_actions = {"start", "set_reference", "skip_reference", "capture_50r", "next", "confirm_antenna",
                         "plan_hi", "approve_hi_plan", "abort", "finish", "status"}
        if action not in valid_actions:
            raise ValueError(f"action must be one of {sorted(valid_actions)}")
        if action == "start":
            n = int(_float(p, "n_captures", 2, 20, 5))
            cs = _float(p, "capture_seconds", 0.5, 30, 2.0)
            ss = _float(p, "stabilize_seconds", 0, 600, 20.0)
            hss = _float(p, "hi_settle_seconds", 0, 120, 2.0)
            cf = _float(p, "center_frequency_hz", 1e6, 2e9, 1_420_405_000.0)
            sr = _float(p, "sample_rate_hz", 200_000, 4e6, 2_400_000.0)
            gain = _float(p, "gain_db", 0, 60, 40.2)
            ct = _float(p, "clipping_threshold", 1e-6, 0.1, 1e-4)
            st = _float(p, "stability_threshold", 0.001, 1.0, 0.10)
            rt = _float(p, "rfi_threshold", 0.0, 1.0, 0.5)
            me = _float(p, "min_elevation_deg", 5, 89, 20.0)
            bf = _float(p, "beam_fwhm_deg", 1, 90, 20.0)
            sig = _float(p, "significance_threshold", 1.0, 10.0, 3.0)
            argv = [PY, "calibrate_reference_wizard.py", "start", "--session-root", "data/calibration",
                   "--n-captures", str(n), "--capture-seconds", str(cs), "--stabilize-seconds", str(ss),
                   "--hi-settle-seconds", str(hss), "--center-freq", str(cf), "--sample-rate", str(sr),
                   "--gain", str(gain), "--clipping-threshold", str(ct), "--stability-threshold", str(st),
                   "--rfi-threshold", str(rt), "--min-elevation", str(me), "--beam-fwhm", str(bf),
                   "--significance-threshold", str(sig)]
            return argv, {"action": action}
        session_dir = _path_in(p, "session_dir", SERVE_ROOTS["calibration"])
        meta = {"action": action, "output_dir": str(session_dir.relative_to(ROOT))}
        rel = str(session_dir.relative_to(ROOT))
        if action == "set_reference":
            cp = str(p.get("connection_point", ""))
            if cp not in ("LNA_INPUT", "LNA_OUTPUT", "SDR_INPUT"):
                raise ValueError("connection_point must be LNA_INPUT, LNA_OUTPUT or SDR_INPUT")
            argv = [PY, "calibrate_reference_wizard.py", "set-reference", "--session-dir", rel, "--connection-point", cp]
        elif action == "skip_reference":
            argv = [PY, "calibrate_reference_wizard.py", "skip-reference", "--session-dir", rel]
            if p.get("reason"):
                argv += ["--reason", str(p["reason"])[:200]]
        elif action == "capture_50r":
            argv = [PY, "calibrate_reference_wizard.py", "capture-50r", "--session-dir", rel]
            sim = p.get("simulate")
            if sim:
                if sim not in ("HEALTHY", "CLIPPED", "THERMAL_DRIFT", "RFI_CONTAMINATED"):
                    raise ValueError("simulate must be one of HEALTHY, CLIPPED, THERMAL_DRIFT, RFI_CONTAMINATED")
                argv += ["--simulate", str(sim)]
        elif action == "next":
            argv = [PY, "calibrate_reference_wizard.py", "next", "--session-dir", rel]
        elif action == "confirm_antenna":
            argv = [PY, "calibrate_reference_wizard.py", "confirm-antenna", "--session-dir", rel]
        elif action == "plan_hi":
            argv = [PY, "calibrate_reference_wizard.py", "plan-hi", "--session-dir", rel]
        elif action == "approve_hi_plan":
            argv = [PY, "calibrate_reference_wizard.py", "approve-hi-plan", "--session-dir", rel]
        elif action == "abort":
            argv = [PY, "calibrate_reference_wizard.py", "abort", "--session-dir", rel]
        elif action == "finish":
            argv = [PY, "calibrate_reference_wizard.py", "finish", "--session-dir", rel]
        else:  # status
            argv = [PY, "calibrate_reference_wizard.py", "status", "--session-dir", rel]
        return argv, meta
    if stage == "calibrate_wizard_move":
        # The ONLY action: a real GOTO + capture at the (already operator-approved) HI ALTO or HI BAJO
        # candidate. --simulate is never forwarded here - a physical stage's own start() gate (typed MOVE +
        # real preflight) must never be bypassable from a web param; offline testing calls the CLI directly,
        # the same convention observe_gain_pilot_capture already established.
        label = str(p.get("label", ""))
        if label not in ("HI_ALTO", "HI_BAJO"):
            raise ValueError("label must be HI_ALTO or HI_BAJO")
        session_dir = _path_in(p, "session_dir", SERVE_ROOTS["calibration"])
        rel = str(session_dir.relative_to(ROOT))
        return [PY, "calibrate_reference_wizard.py", "capture-hi", "--session-dir", rel, "--label", label], \
              {"action": "capture_hi", "label": label, "output_dir": rel}
    if stage == "observe_gain_pilot_plan":
        raw = p.get("resolved_plan_path")
        if not isinstance(raw, str) or not raw:
            raise ValueError("resolved_plan_path (string) is required")
        rp = (ROOT / raw).resolve()
        if not _within(rp, SERVE_ROOTS["mosaic"]) or not rp.is_file():
            raise ValueError("resolved_plan_path must be an existing file inside data/mosaic")
        max_pilots = int(_float(p, "max_pilots", 1, 2, 2))
        cs = _float(p, "capture_seconds", 0.5, 30, 2.0)
        ss = _float(p, "settle_seconds", 0, 60, 1.0)
        hm = _float(p, "headroom_multiplier", 1.0, 5.0, 1.5)
        ct = _float(p, "clipping_threshold", 1e-6, 0.1, 1e-4)
        rt = _float(p, "rfi_threshold", 0.0, 1.0, 0.5)
        gain = _float(p, "gain_db", 0, 60, 40.2)
        final_check = bool(p.get("final_check", True))
        argv = [PY, "observation_gain_pilot.py", "plan", "--resolved-plan-path", str(rp.relative_to(ROOT)),
               "--max-pilots", str(max_pilots), "--capture-seconds", str(cs), "--settle-seconds", str(ss),
               "--headroom-multiplier", str(hm), "--clipping-threshold", str(ct), "--rfi-threshold", str(rt),
               "--gain-db", str(gain), "--final-check", "true" if final_check else "false"]
        beam_fwhm = p.get("beam_fwhm")
        if beam_fwhm not in (None, ""):
            argv += ["--beam-fwhm", str(_float(p, "beam_fwhm", 1, 90))]
        return argv, {"resolved_plan_path": str(rp.relative_to(ROOT))}
    if stage == "observe_gain_pilot_capture":
        action = str(p.get("action", ""))
        cmd_map = {"capture_high": "capture-high", "capture_low": "capture-low", "verify_gain": "verify-gain", "final_check": "final-check"}
        if action not in cmd_map:
            raise ValueError(f"action must be one of {sorted(cmd_map)}")
        session_dir = _path_in(p, "session_dir", SERVE_ROOTS["mosaic"])
        rel = str(session_dir.relative_to(ROOT))
        return [PY, "observation_gain_pilot.py", cmd_map[action], "--session-dir", rel], {"action": action, "output_dir": rel}
    if stage == "observe_gain_pilot_admin":
        action = str(p.get("action", ""))
        session_dir = _path_in(p, "session_dir", SERVE_ROOTS["mosaic"])
        rel = str(session_dir.relative_to(ROOT))
        if action == "set_gain":
            gain = _float(p, "gain_db", 0, 60)
            return [PY, "observation_gain_pilot.py", "set-gain", "--session-dir", rel, "--gain-db", str(gain)], {"action": action, "output_dir": rel}
        if action == "abort":
            return [PY, "observation_gain_pilot.py", "abort", "--session-dir", rel], {"action": action, "output_dir": rel}
        if action == "status":
            return [PY, "observation_gain_pilot.py", "status", "--session-dir", rel], {"action": action, "output_dir": rel}
        raise ValueError("action must be one of set_gain, abort, status")
    if stage in ("reduce_plan", "reduce"):
        camp = _path_in(p, "campaign_dir", SERVE_ROOTS["mosaic"])
        # PLAN still goes straight through the frozen almita_reduce.py CLI (unchanged). RUN goes through
        # reduce_campaign_run.py instead - a thin bridge (same frozen reduce_engine calls underneath) that
        # additionally records a verifiable, RUN-time calibration-compatibility result per point - see its
        # own docstring and reduce_calibration_record.py's.
        script = "almita_reduce.py" if stage == "reduce_plan" else "reduce_campaign_run.py"
        argv = [PY, script, "plan" if stage == "reduce_plan" else "run", str(camp.relative_to(ROOT)), "--json"]
        vf = str(p.get("velocity_frame") or "lsrk")
        if vf not in ("topocentric", "heliocentric", "barycentric", "lsrk"):
            raise ValueError("velocity_frame must be one of topocentric, heliocentric, barycentric, lsrk")
        argv += ["--velocity-frame", vf]
        prof = p.get("calibration_profile")
        if prof:
            pp = (ROOT / str(prof)).resolve()
            if not _within(pp, SERVE_ROOTS["calibration"]) or not pp.is_file():
                raise ValueError("calibration_profile must be an existing file inside data/calibration")
            argv += ["--calibration-profile", str(pp.relative_to(ROOT))]
        meta = {"campaign_dir": str(camp.relative_to(ROOT)), "calibration_profile": str(prof) if prof else None, "velocity_frame": vf}
        if stage == "reduce":
            _require_fresh_plan("reduce", meta, p)
            if prof:
                _require_uncalibrated_confirmation(str(camp.relative_to(ROOT)), str(prof), p)
        return argv, meta
    if stage in ("reduce_capture_plan", "reduce_capture"):
        cap = _file_in(p, "capture", (SERVE_ROOTS["mosaic"], SERVE_ROOTS["calibration"]))
        argv = [PY, "reduce_single_capture.py", "plan" if stage == "reduce_capture_plan" else "run",
               str(cap.relative_to(ROOT)), "--json"]
        vf = str(p.get("velocity_frame") or "topocentric")
        if vf not in ("topocentric", "heliocentric", "barycentric", "lsrk"):
            raise ValueError("velocity_frame must be one of topocentric, heliocentric, barycentric, lsrk")
        argv += ["--velocity-frame", vf]
        prof = p.get("calibration_profile")
        if prof:
            pp = (ROOT / str(prof)).resolve()
            if not _within(pp, SERVE_ROOTS["calibration"]) or not pp.is_file():
                raise ValueError("calibration_profile must be an existing file inside data/calibration")
            argv += ["--calibration-profile", str(pp.relative_to(ROOT))]
        obs_cfg = ROOT / "observer_config.json"
        if obs_cfg.is_file():
            argv += ["--observer-config", str(obs_cfg.relative_to(ROOT))]
        ra = p.get("ra_hours")
        dec = p.get("dec_deg")
        if ra not in (None, "") and dec not in (None, ""):
            argv += ["--ra-hours", str(_float(p, "ra_hours", 0, 24)), "--dec-deg", str(_float(p, "dec_deg", -90, 90))]
        meta = {"capture": str(cap.relative_to(ROOT)), "calibration_profile": str(prof) if prof else None, "velocity_frame": vf}
        if stage == "reduce_capture":
            _require_fresh_plan("reduce_capture", meta, p)
        return argv, meta
    if stage in ("science_plan", "science"):
        red = _path_in(p, "reduce_session_dir", SERVE_ROOTS["reduced"])
        argv = [PY, "almita_science.py", "plan" if stage == "science_plan" else "run", str(red.relative_to(ROOT))]
        beam = str(p.get("beam", "observer_config"))              # SCIENCE has no silent default beam: the operator's choice is passed through, never invented here
        if beam == "observer_config":
            argv.append("--beam-from-observer-config")
        elif beam == "fwhm":
            v = _float(p, "beam_fwhm_deg", 0.1, 90)
            if v is None:
                raise ValueError("beam_fwhm_deg is required when beam = fwhm")
            argv += ["--beam-fwhm-deg", str(v)]
        else:
            raise ValueError("beam must be observer_config or fwhm")
        if p.get("quality_policy"):
            if p["quality_policy"] not in ("STRICT", "STANDARD", "PERMISSIVE"):
                raise ValueError("quality_policy must be STRICT, STANDARD or PERMISSIVE")
            argv += ["--quality-policy", p["quality_policy"]]
        return argv, {"reduce_session_dir": str(red.relative_to(ROOT)), "beam": beam}
    raise ValueError(f"unknown stage {stage!r}")


# ------------------------------------------------------------------ jobs
def _job_dir(job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id or ""):
        raise ValueError("invalid job id")
    return OPS_DIR / job_id


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def _alive(pid: Optional[int], needle: bytes) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return needle in Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ProcessLookupError):
        return False


def _load(job_id: str) -> Dict[str, Any]:
    return json.loads((_job_dir(job_id) / "job.json").read_text())


def list_jobs(limit: int = 30) -> List[Dict[str, Any]]:
    if not OPS_DIR.is_dir():
        return []
    names = sorted((d.name for d in OPS_DIR.iterdir() if d.is_dir() and (d / "job.json").exists()), reverse=True)[:limit]
    rows = []
    for n in names:
        try:
            j = _load(n)
        except (OSError, ValueError):
            continue
        rows.append({"job_id": n, "stage": j["stage"], "state": _state(j), "started_utc": j.get("started_utc"), "ended_utc": j.get("ended_utc"), "exit_code": j.get("exit_code"),
                     "verdict": classify(j)["verdict"]})
    return rows


def _state(j: Dict[str, Any]) -> str:
    if j.get("exit_code") is not None:
        return "EXITED"
    if j.get("runner_pid") is None and time.time() - float(j.get("started_epoch", 0)) < 15:
        return "RUNNING"                                        # the detached runner has not written its pid yet
    return "RUNNING" if _alive(j.get("runner_pid"), b"almita_web_ops.py") else "LOST"


def _busy_resources() -> Dict[str, str]:
    busy: Dict[str, str] = {}
    if OPS_DIR.is_dir():
        for d in OPS_DIR.iterdir():
            try:
                j = json.loads((d / "job.json").read_text())
            except (OSError, ValueError):
                continue
            if _state(j) == "RUNNING":
                for r in STAGES.get(j["stage"], {}).get("resources", ()):
                    busy[r] = f"{j['stage']} {j['job_id']}"
    return busy


def busy_reason(resources: Tuple[str, ...] = ("mount", "sdr")) -> Optional[str]:
    """For other routes (OBSERVE START): is a web-launched job currently holding the mount or MAIN?"""
    busy = _busy_resources()
    for r in resources:
        if r in busy:
            return f"{r} is in use by {busy[r]}"
    return None


def start(stage: str, params: Dict[str, Any], confirm: Optional[str] = None) -> Dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    spec = STAGES[stage]
    job_id = f"{stage.upper()}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    argv, meta = build_command(stage, params, job_id)
    busy = _busy_resources()
    for r in spec["resources"]:
        if r in busy and r != "cpu":
            raise OpsBlocked(f"{r} is in use by {busy[r]}")
        if r == "cpu" and "cpu" in busy:
            raise OpsBlocked(f"another REDUCE/SCIENCE job is running ({busy['cpu']}): one at a time on this Pi")
    if "sdr" in spec["resources"]:
        from almita_web_common import get_sdr_resource_status
        res = get_sdr_resource_status()
        if res.status.value != "FREE":
            raise OpsBlocked(f"MAIN SDR is not free: {res.status.value}: {res.detail}")
    pf = None
    if spec["physical"]:
        if confirm != "MOVE":
            raise OpsBlocked("physical movement needs the operator's typed confirmation: send confirm = \"MOVE\" (uppercase, exactly)")
        pf = preflight()
        if pf["overall"] == "BLOCK":
            raise OpsBlocked("real preflight is BLOCK: " + "; ".join(f"{c['name']}: {c['detail']}" for c in pf["checks"] if c["status"] == "BLOCK"))
        probs = mount_idle_problems(pf["mount"])
        if probs:
            raise OpsBlocked("mount is not ready for movement: " + "; ".join(probs))
        import observation_orchestrator
        if ((observation_orchestrator.get_status().get("orchestrator") or {}).get("orchestrator_state")) in ("RUNNING", "STOPPING", "PREFLIGHT"):
            raise OpsBlocked("an observation is active: the mount belongs to it")
    d = _job_dir(job_id)
    d.mkdir(parents=True, exist_ok=False)
    job = {"job_id": job_id, "stage": stage, "argv": argv, "cwd": str(ROOT), "params": params, "meta": meta, "physical": spec["physical"], "started_utc": _utc(),
           "started_epoch": time.time(), "log": str(d / "job.log"), "preflight_overall": pf["overall"] if pf else None, "exit_code": None}
    _write_json(d / "job.json", job)
    subprocess.Popen([PY, str(Path(__file__).resolve()), "--runner", str(d / "job.json")], cwd=str(ROOT), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)       # the runner records its own pid / exit code in job.json
    return get_job(job_id, tail=20)


def stop(job_id: str) -> Dict[str, Any]:
    j = _load(job_id)
    if _state(j) != "RUNNING" or not j.get("runner_pid"):
        raise OpsBlocked(f"job {job_id} is not running (or is still starting)")
    os.kill(int(j["runner_pid"]), signal.SIGINT)              # SIGINT only; the runner forwards it to the real command
    return {"job_id": job_id, "requested": "SIGINT sent to the runner; the command decides when it stops (a real slew may finish its current step)", "utc": _utc()}


def _tail(path: Path, n: int) -> str:
    try:
        data = path.read_bytes()[-200_000:].decode("utf8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-n:])


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _artifacts(out_dir: Optional[Path], limit: int = 120) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not out_dir or not out_dir.is_dir():
        return rows
    base = out_dir.resolve()
    for p in sorted(base.rglob("*")):
        if len(rows) >= limit:
            break
        if p.is_file() and len(p.relative_to(base).parts) <= 4 and (p.suffix.lower() in SERVE_SUFFIXES or p.suffix.lower() in (".h5", ".hdf5", ".npy", ".u8")):
            rows.append({"path": str(p.relative_to(ROOT)), "size": p.stat().st_size, "viewable": p.suffix.lower() in SERVE_SUFFIXES})
    return rows


def classify(j: Dict[str, Any]) -> Dict[str, Any]:
    """PASS / PARTIAL / FAIL / RUNNING from the REAL exit code + the REAL result artifacts. Never PASS unless the command itself confirmed it."""
    stage, state = j["stage"], _state(j)
    log = _tail(Path(j["log"]), 100000) if j.get("log") else ""       # parse the whole (bounded) log, not only its display tail
    rc = j.get("exit_code")
    out: Dict[str, Any] = {"verdict": "RUNNING" if state == "RUNNING" else "FAIL", "detail": "", "output_dir": None, "facts": {}}
    if state == "RUNNING":
        out["detail"] = "running"
    elif state == "LOST":
        out["detail"] = "the runner disappeared without recording an exit code"
    def grab(rx: str) -> Optional[str]:
        m = re.search(rx, log, re.M)
        return m.group(1).strip() if m else None
    if stage in ("align", "align_plan"):
        od = ROOT / j["meta"]["output_dir"]
        res = _read_json(od / "alignment_result.json")
        out["output_dir"] = str(od.relative_to(ROOT))
        if res:
            recs = res.get("measured_positions") or []
            valid = sum(1 for r in recs if r.get("status") == "VALID")
            pc = res.get("pattern_config") or {}
            planned = res.get("planned_positions") or []
            sel = (res.get("expected_template_metric") or {}).get("selection") or {}
            out["facts"] = {"reference": res.get("reference"), "result_status": res.get("result_status"), "center": res.get("center_coordinates"), "positions": len(recs), "valid_positions": valid,
                            "sync_applied": res.get("sync_applied"), "pattern_config": pc or None, "run_config": res.get("run_config"),
                            "approved_plan_path": (res.get("run_config") or {}).get("approved_plan_path"),
                            "solar_track_mode_previous": sel.get("solar_track_mode_previous"), "solar_track_mode_confirmed": sel.get("solar_track_mode_confirmed"),
                            "solar_track_mode_readback": sel.get("solar_track_mode_readback"),
                            # temporal_check: PLAN's real evaluation of every pattern point's altitude at the wall-clock
                            # time it would actually be captured over the WHOLE run (not just the instant PLAN ran) -
                            # RUN re-checks this fresh before its first movement using the same function.
                            "temporal_check": res.get("temporal_check")}
            if planned:                                                   # PLAN (dry-run) only: the real computed preview, before any RUN
                out["facts"]["planned_positions"] = planned
                out["facts"]["max_separation_from_center_deg"] = max((row["separation_from_center_deg"] for row in planned), default=None)
        if state == "EXITED":
            if not res:
                # alignment.py's own exit code is not an error signal: real, honest non-PASS conclusions (no defendible structure,
                # confidence too low, SYNC blocked) return 2 by design (see its run()). An --approved-plan refusal (stale/conflicting
                # plan) is exit 3 with a clear one-line reason on stdout, not a crash; only a truly missing result file is FAIL-worthy.
                blocked = grab(r"^Result\s+BLOCKED:\s*(.+)$")
                out["detail"] = blocked or f"exit {rc}; no alignment_result.json"
            elif stage == "align_plan":
                out["verdict"] = "PASS" if res.get("result_status") == "PASS" else "FAIL"
                tc = res.get("temporal_check") or {}
                if res.get("result_status") == "TEMPORAL ALTITUDE CHECK FAILED":
                    out["detail"] = (f"plan only, nothing moved; the pattern would reach {tc.get('min_altitude_deg'):.2f} deg altitude at "
                                     f"point {tc.get('min_altitude_point_index')} (~{tc.get('min_altitude_utc')}), below MIN ELEVATION "
                                     f"{tc.get('min_elevation_deg')} deg, before this ~{(tc.get('run_duration_s') or 0)/60:.1f} min run "
                                     "would finish - pick a different candidate or shrink the pattern")
                else:
                    out["detail"] = f"plan only, nothing moved; status {res.get('result_status')}"
            else:
                st = str(res.get("result_status"))
                v = out["facts"]["valid_positions"]
                if st == "SOLAR TRACK MODE NOT CONFIRMED":
                    out["verdict"], out["detail"] = "FAIL", "blocked before any GOTO: OnStep did not confirm TRACK_SOLAR (see solar_track_mode_readback)"
                elif v == 0 or st.startswith("INSUFFICIENT"):
                    out["verdict"], out["detail"] = "FAIL", f"real run finished but {st} ({v} valid positions)"
                else:
                    out["verdict"], out["detail"] = "PARTIAL", (f"real motion + capture done ({v} valid positions); alignment result: {st}. The HI template is non-observational: "
                                                                "no alignment offset is claimed and SYNC was never sent")
    elif stage == "calibrate":
        sd = grab(r"^Session evidence:\s+(.+)$")
        cdir = Path(sd) if sd else None
        cdir = (cdir if cdir.is_absolute() else ROOT / cdir) if cdir else None
        out["output_dir"] = str(cdir.resolve().relative_to(ROOT)) if cdir and cdir.exists() and _within(cdir, ROOT) else None
        res = _read_json(cdir / "calibration_result.json") if cdir else None
        if res:
            q = (res.get("quality") or {})
            out["facts"] = {"quality_verdict": q.get("verdict"), "quality_reasons": q.get("reasons"), "calibration_level": res.get("calibration_level"), "absolute_calibration": res.get("absolute_calibration"),
                            "captures": len(res.get("per_capture") or []), "error": res.get("error")}
        if state == "EXITED":
            if rc != 0 or not res or res.get("error"):
                out["detail"] = f"exit {rc}; {(res or {}).get('error') or 'no calibration_result.json'}"
            else:
                qv = out["facts"]["quality_verdict"]
                out["verdict"] = "PASS" if qv == "GOOD" else "PARTIAL"
                out["detail"] = (f"real MAIN captures; quality {qv}; level {res.get('calibration_level')} (absolute_calibration={res.get('absolute_calibration')}: "
                                 "engineering/non-science, NO physical units claimed)")
    elif stage in ("calibrate_wizard", "calibrate_wizard_move"):
        # Every calibrate_reference_wizard.py subcommand prints exactly one JSON blob (session_dir + state) and
        # nothing else to stdout - extract it from the combined stdout+stderr log rather than needing a second
        # result file, the same "one script invocation, one real artifact" contract as every other stage.
        blob = None
        m = re.search(r"\{.*\}", log, re.S)
        if m:
            try:
                blob = json.loads(m.group(0))
            except ValueError:
                blob = None
        meta = j.get("meta") or {}
        action = meta.get("action")
        if blob and blob.get("state"):
            st = blob["state"]
            out["output_dir"] = str((ROOT / blob["session_dir"]).resolve().relative_to(ROOT)) if blob.get("session_dir") else out["output_dir"]
            out["facts"] = {"session_id": st.get("session_id"), "session_dir": blob.get("session_dir"), "action": action,
                            "step": st.get("step"), "config": st.get("config"),
                            "fifty_ohm": st.get("fifty_ohm"), "fifty_ohm_result": st.get("fifty_ohm_result"),
                            "reconnect_antenna_confirmed_utc": st.get("reconnect_antenna_confirmed_utc"),
                            "hi_plan": st.get("hi_plan"), "hi_plan_approved_utc": st.get("hi_plan_approved_utc"),
                            # hi_references: every HI kind's full result (not just the one just captured) - the
                            # web UI's RESULT step needs this on every render, including after a page reload/
                            # recovery where the LAST job might be "next"/"status", not "capture-hi" itself.
                            "hi_references": st.get("hi_references"),
                            "bias_t_facts": st.get("bias_t_facts"), "receiver_snapshot": st.get("receiver_snapshot"),
                            "spectral_contrast": st.get("spectral_contrast"), "profile_path": st.get("profile_path"),
                            # observe_profile: the OBSERVE/REDUCE-consumable .json+.npz pair FINISH built
                            # automatically from this session's real 50R captures (or why it could not) -
                            # deliberately a separate field from profile_path (the operational DRAFT above),
                            # so the web UI can never present one as if it were the other.
                            "observe_profile": st.get("observe_profile")}
            if action == "capture_50r" and st.get("fifty_ohm_result"):
                out["facts"]["last_reference_result"] = st["fifty_ohm_result"]
            elif action == "capture_hi" and meta.get("label") and st.get("hi_references", {}).get(meta["label"]):
                out["facts"]["last_reference_result"] = st["hi_references"][meta["label"]]["quality_result"]
        if state == "EXITED":
            if rc != 0 or not blob:
                out["detail"] = f"exit {rc}; {_tail(Path(j['log']), 5) if not blob else 'no JSON result from calibrate_reference_wizard.py'}"
            elif action == "capture_50r" and out["facts"].get("last_reference_result"):
                r = out["facts"]["last_reference_result"]
                out["verdict"] = r["verdict"]
                out["detail"] = f"50 ohm reference: {r['verdict']} ({'; '.join(r['verdict_reasons'])})"
            elif action == "capture_hi" and out["facts"].get("last_reference_result"):
                r = out["facts"]["last_reference_result"]
                out["verdict"] = r["verdict"]
                out["detail"] = f"{meta.get('label')}: quality {r['verdict']} ({'; '.join(r['verdict_reasons'])}) - see spectral_result for the real HI-line measurement"
            elif action == "finish" and st.get("spectral_contrast"):
                sc = st["spectral_contrast"]
                op = st.get("observe_profile") or {}
                out["verdict"] = "PASS" if sc["verdict"] == "DEFENSIBLE_CONTRAST" else "PARTIAL"
                profile_note = (f"OBSERVE profile READY: {op.get('profile_path_for_observe')}" if op.get("status") == "READY"
                               else f"NO USABLE OBSERVE PROFILE - {op.get('reason', 'unknown reason')}")
                out["detail"] = f"spectral contrast: {sc['verdict']} - {sc['reason']} | {profile_note}"
            else:
                out["verdict"] = "PASS"
                out["detail"] = f"wizard step '{action}' OK - now at {out['facts'].get('step')}"
    elif stage in ("observe_gain_pilot_plan", "observe_gain_pilot_capture", "observe_gain_pilot_admin"):
        # Same "one JSON blob on stdout" contract as calibrate_wizard - real state read back from
        # gain_pilot_state.json (via observation_gain_pilot.py's own emit), never re-derived here.
        blob = None
        m = re.search(r"\{.*\}", log, re.S)
        if m:
            try:
                blob = json.loads(m.group(0))
            except ValueError:
                blob = None
        action = (j.get("meta") or {}).get("action")
        if blob and blob.get("state"):
            st = blob["state"]
            out["output_dir"] = str((ROOT / blob["session_dir"]).resolve().relative_to(ROOT)) if blob.get("session_dir") else out["output_dir"]
            out["facts"] = {"session_dir": blob.get("session_dir"), "action": action, "step": st.get("step"),
                            "observation_name": st.get("observation_name"), "candidates": st.get("candidates"),
                            "candidate_note": st.get("candidate_note"), "catalog_source": st.get("catalog_source"),
                            "grid_points_considered": st.get("grid_points_considered"), "config": st.get("config"),
                            "evaluations": st.get("evaluations"), "need_low": st.get("need_low"),
                            "gain_recommendation": st.get("gain_recommendation"), "initial_gain_db": st.get("initial_gain_db"),
                            "approved_gain_db": st.get("approved_gain_db"), "verified_gain_db": st.get("verified_gain_db"),
                            "final_check": st.get("final_check"), "estimated_duration_s": st.get("estimated_duration_s"),
                            "estimated_extra_points": st.get("estimated_extra_points"), "duration_source": st.get("duration_source"),
                            "grid_config_hash": st.get("grid_config_hash"), "min_elevation_deg": st.get("min_elevation_deg")}
        if state == "EXITED":
            # A real, honest non-PASS conclusion (e.g. final-check's "no same-gain baseline on record") returns a
            # non-zero exit by design, same convention as ALIGN's own non-crash exit codes - only a MISSING JSON
            # blob means the script itself failed to produce a real result at all.
            if not blob:
                out["detail"] = f"exit {rc}; no JSON result from observation_gain_pilot.py: {_tail(Path(j['log']), 5)}"
            elif stage == "observe_gain_pilot_plan":
                out["verdict"] = "PASS"
                out["detail"] = (f"plan only, nothing moved; {out['facts'].get('estimated_extra_points')} pilot point(s), "
                                 f"~{(out['facts'].get('estimated_duration_s') or 0):.0f}s estimated")
            elif stage == "observe_gain_pilot_capture":
                ev_key = {"capture_high": "HIGH", "capture_low": "LOW", "verify_gain": "VERIFY", "final_check": None}.get(action)
                ev = (out["facts"].get("evaluations") or {}).get(ev_key) if ev_key else None
                fc = out["facts"].get("final_check")
                if action == "final_check" and fc:
                    if fc.get("evaluation") is not None:
                        out["verdict"] = "PASS"
                        out["detail"] = (f"stability check: relative power delta {fc['relative_power_delta_fraction']:.3f} at "
                                         f"{fc.get('compared_gain_db')} dB vs a same-gain baseline ({fc['label']})")
                    else:
                        out["verdict"] = "PARTIAL"
                        out["detail"] = f"{fc.get('label')}: {fc.get('reason')}"
                elif ev:
                    cs = ev["clipping"]["status"]
                    out["verdict"] = "PASS" if cs == "OK" else ("PARTIAL" if cs == "WARNING" else "FAIL")
                    margin = ev["clipping"].get("percentile_margin_codes")
                    out["detail"] = f"{action}: clipping={cs}, headroom margin {margin if margin is None else round(margin, 1)} codes"
                else:
                    out["verdict"] = "PASS"
                    out["detail"] = f"{action} completed"
            else:
                out["verdict"] = "PASS"
                out["detail"] = f"{action} OK - now at {out['facts'].get('step')}"
    elif stage in ("reduce", "reduce_plan", "reduce_capture_plan", "reduce_capture"):
        # almita_reduce.py / reduce_single_capture.py are both always invoked with --json now - one real JSON
        # blob on stdout, same "one script invocation, one real artifact" contract as every other stage.
        # reduce_single_capture.py's plan/run payload shapes are DELIBERATELY IDENTICAL to almita_reduce.py's
        # own (run's is the literal same CampaignReduceReport) - this block never needs to know which one ran.
        blob = None
        m = re.search(r"\{.*\}", log, re.S)
        if m:
            try:
                blob = json.loads(m.group(0))
            except ValueError:
                blob = None
        is_plan = stage.endswith("_plan")
        if blob is not None:
            out["facts"] = blob
            if is_plan:
                # Persisted once by the runner right when this PLAN subprocess actually finished (see
                # _runner()) - a real snapshot of what was on disk at PLAN time, NOT recomputed here: classify()
                # runs fresh on every poll, so computing it here would silently reflect the CURRENT disk
                # instead of PLAN's own moment, defeating the whole point of a staleness check.
                out["facts"]["file_signatures"] = j.get("file_signatures")
            if not is_plan and blob.get("output_dir"):
                odp = ROOT / blob["output_dir"]
                out["output_dir"] = str(odp.resolve().relative_to(ROOT)) if odp.exists() and _within(odp, ROOT) else None
        if state == "EXITED":
            script = ("reduce_campaign_run.py" if stage == "reduce" else
                     "reduce_single_capture.py" if "capture" in stage else "almita_reduce.py")
            if not blob:
                out["detail"] = f"exit {rc}; no JSON result from {script}: {_tail(Path(j['log']), 5)}"
            elif is_plan:
                checks = blob.get("checks") or []
                failing = [c["name"] for c in checks if not c.get("ok", True)]
                out["verdict"] = "FAIL" if blob.get("blocked") else "PASS"
                out["detail"] = (f"BLOCKED: {'; '.join(failing)}" if blob.get("blocked") else
                                 f"PLAN OK - config_hash {blob.get('config_hash', '')[:12]} - real engine "
                                 f"preflight passed ({len(checks)} checks)")
            else:
                status = blob.get("status")
                qc = blob.get("quality_counts") or {}
                bad_or_unknown = qc.get("BAD", 0) + qc.get("UNKNOWN", 0)
                out["verdict"] = ("PASS" if status == "COMPLETED" and not bad_or_unknown else
                                  "PARTIAL" if status in ("COMPLETED", "PARTIAL") else "FAIL")
                out["detail"] = (f"REDUCE {status} - calibration {blob.get('calibration_level_counts')}, "
                                 f"velocity {blob.get('velocity_frame_counts')}, quality {qc}")
    elif stage in ("science", "science_plan"):
        kind = "SCIENCE"
        od = grab(r"^Output:\s+(.+)$")
        odp = (Path(od) if Path(od).is_absolute() else ROOT / od) if od else None
        out["output_dir"] = str(odp.resolve().relative_to(ROOT)) if odp and odp.exists() and _within(odp, ROOT) else None
        status = grab(rf"^{kind} (\w+)")
        blocked = grab(r"^BLOCKED:\s*(.+)$")
        out["facts"] = {"status": status, "blocked": blocked, "campaign": grab(r"^Campaign:\s+(.+)$"), "accepted": grab(r"^Accepted:\s+(.+)$"), "quality": grab(r"^Quality:\s+(.+)$")}
        if state == "EXITED":
            if stage.endswith("_plan"):
                out["verdict"] = "PASS" if rc == 0 else "FAIL"
                out["detail"] = "plan/preflight only (nothing computed)"
            elif blocked or rc != 0 or status not in ("COMPLETED", "PARTIAL", "VALID"):
                out["detail"] = f"exit {rc}; {blocked or status or 'no status line'}"
            else:
                out["verdict"] = "PASS" if status in ("COMPLETED", "VALID") and out["facts"]["quality"] in (None, "GOOD") else "PARTIAL"
                out["detail"] = f"{kind} {status} (real engine result)" + (f"; quality {out['facts']['quality']}" if out["facts"]["quality"] else "")
    if state == "EXITED" and j.get("stopped_by_operator"):
        if out["verdict"] == "PASS":
            out["verdict"] = "PARTIAL"
        elif out["verdict"] == "FAIL":
            out["verdict"] = "STOPPED"                              # the operator sent SIGINT before the command produced its result
        out["detail"] = "stopped by the operator (SIGINT); " + out["detail"]
    return out


def _sep_deg(ra1_h: float, dec1: float, ra2_h: float, dec2: float) -> float:
    import math
    a1, a2, b1, b2 = math.radians(ra1_h * 15), math.radians(ra2_h * 15), math.radians(dec1), math.radians(dec2)
    y = math.hypot(math.cos(b2) * math.sin(a2 - a1), math.cos(b1) * math.sin(b2) - math.sin(b1) * math.cos(b2) * math.cos(a2 - a1))
    return math.degrees(math.atan2(y, math.sin(b1) * math.sin(b2) + math.cos(b1) * math.cos(b2) * math.cos(a2 - a1)))


def _plan_slew(j: Dict[str, Any], c: Dict[str, Any]) -> None:
    """For a finished alignment PLAN: how far is the planned region centre from where the mount really is (real INDI read, cached once in job.json)."""
    center = (c["facts"] or {}).get("center")
    if not center or j.get("exit_code") != 0:
        return
    pm = j.get("plan_mount")
    if not pm:
        m = read_mount()
        if m.get("error"):
            c["facts"]["mount_now"] = {"error": m["error"]}
            return
        pm = {"ra_h": m["ra_h"], "dec_deg": m["dec_deg"], "read_utc": m["read_utc"], "separation_deg": _sep_deg(m["ra_h"], m["dec_deg"], center["ra_hours"], center["dec_deg"])}
        j["plan_mount"] = pm
        _write_json(_job_dir(j["job_id"]) / "job.json", j)
    c["facts"]["mount_now"] = pm
    c["facts"]["slew_from_mount_deg"] = pm["separation_deg"]


def get_job(job_id: str, tail: int = 80) -> Dict[str, Any]:
    j = _load(job_id)
    c = classify(j)
    if j["stage"] == "align_plan" and _state(j) == "EXITED":
        _plan_slew(j, c)
    od = ROOT / c["output_dir"] if c.get("output_dir") else None
    out = {"job_id": job_id, "stage": j["stage"], "state": _state(j), "argv": j["argv"], "started_utc": j["started_utc"], "ended_utc": j.get("ended_utc"), "exit_code": j.get("exit_code"),
           "physical": j["physical"], "params": j["params"], "verdict": c["verdict"], "detail": c["detail"], "facts": c["facts"], "output_dir": c["output_dir"],
           "artifacts": _artifacts(od), "log_tail": _tail(Path(j["log"]), tail), "stopped_by_operator": bool(j.get("stopped_by_operator")),
           "elapsed_s": ((datetime.fromisoformat(j["ended_utc"]) if j.get("ended_utc") else datetime.now(timezone.utc)) - datetime.fromisoformat(j["started_utc"])).total_seconds()}
    if j["stage"] == "align" and od:                              # live progress: real samples written so far
        out["progress"] = {"samples_written": len(list(od.glob("alignment_sample_*.h5"))) or len(list(od.glob("**/alignment_sample_*.h5")))}
    return out


def resolve_file(rel: str) -> Tuple[Path, str]:
    """A file the UI may show (png/json/csv/txt/log/md) under the known data roots. Never anything else."""
    if not isinstance(rel, str) or "\x00" in rel:
        raise ValueError("invalid path")
    p = (ROOT / rel).resolve()
    if not any(_within(p, r) for r in SERVE_ROOTS.values()) or not p.is_file():
        raise FileNotFoundError(rel)
    ctype = SERVE_SUFFIXES.get(p.suffix.lower())
    if not ctype or p.stat().st_size > MAX_SERVE_BYTES:
        raise ValueError("file type/size not served")
    return p, ctype


def campaigns(limit: int = 40) -> Dict[str, List[Dict[str, Any]]]:
    """What the pipeline can act on right now (real directories): grid campaigns to REDUCE, REDUCE sessions to run SCIENCE on."""
    def newest(root: Path, depth: int, marker: str) -> List[Dict[str, Any]]:
        rows = []
        if not root.is_dir():
            return rows
        for p in root.glob("/".join(["*"] * depth)):
            if p.is_dir() and (p / marker).exists():
                rows.append({"path": str(p.relative_to(ROOT)), "mtime": p.stat().st_mtime, "name": p.name})
        return sorted(rows, key=lambda r: -r["mtime"])[:limit]
    profiles = sorted(SERVE_ROOTS["calibration"].glob("**/calibration_profile*.json"), key=lambda q: -q.stat().st_mtime)[:limit] if SERVE_ROOTS["calibration"].is_dir() else []
    return {"campaigns": newest(SERVE_ROOTS["mosaic"], 1, "observation_resolved.json"), "reduce_sessions": newest(SERVE_ROOTS["reduced"], 2, "manifest.json"),
            "science_sessions": newest(SERVE_ROOTS["science"], 2, "manifest.json"), "profiles": [{"path": str(q.relative_to(ROOT)), "name": q.name} for q in profiles]}


# ------------------------------------------------------------------ REDUCE: discovery + metadata preview + compatibility (all read-only, no hardware)
def reduce_list_captures(limit: int = 80) -> List[Dict[str, Any]]:
    """Standalone HDF5 captures worth reducing on their own (real directories, real files) - grid-campaign
    point captures under data/mosaic and CALIBRATE session captures under data/calibration. Never a synthetic
    listing: every row is a file that genuinely exists right now."""
    rows: List[Dict[str, Any]] = []
    for root, glob_pat in ((SERVE_ROOTS["mosaic"], "*/data/iq/*.h5"), (SERVE_ROOTS["calibration"], "*/captures/**/*.h5")):
        if not root.is_dir():
            continue
        for f in root.glob(glob_pat):
            if f.is_file() and not f.name.endswith(".part"):
                rows.append({"path": str(f.relative_to(ROOT)), "name": f.name, "mtime": f.stat().st_mtime,
                            "size_bytes": f.stat().st_size})
    return sorted(rows, key=lambda r: -r["mtime"])[:limit]


_CAMPAIGN_DIR_TS_RE = re.compile(r"(\d{8}-\d{2}:\d{2}:\d{2})$")


def _campaign_session_label(camp: Path, session_id: Optional[str]) -> str:
    """A real date/session identifier for the operator to tell campaigns apart - OBSERVE's own session_id
    (from grid_metadata.json) when present, else the timestamp OBSERVE always suffixes onto the directory
    name itself (<name>-<YYYYMMDD>-<HH:MM:SS>), else (only for a directory that somehow has neither) the
    directory's own mtime, clearly labeled as a fallback rather than presented as a real session id."""
    if session_id:
        return str(session_id)
    m = _CAMPAIGN_DIR_TS_RE.search(camp.name)
    if m:
        return m.group(1)
    return datetime.fromtimestamp(camp.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC (mtime - no session id found)")


_REDUCE_CAMPAIGN_CACHE_PATH = ROOT / "data" / "runtime" / "reduce_campaign_readiness_cache.json"


def _campaign_readiness_signature(camp: Path) -> Dict[str, Any]:
    """Cheap (stat-only, no HDF5 opened) proxy for "has this campaign's real data changed since it was last
    classified". A finished OBSERVE campaign's HDF5s are write-once (capture.py never edits a completed
    file), so once a campaign stops changing, re-validating every one of its files on every single page load
    is pure waste - _reduce_campaign_readiness() opens every declared-success point's real HDF5
    (sdr_capture.validate_hdf5_capture()), which is correct but measured at ~50s across this Pi's ~90 real
    campaign directories; this signature lets reduce_list_campaigns() skip that work entirely for any
    campaign whose files have not changed since it was last computed. Never a full content hash (that would
    reintroduce the same cost this exists to avoid) - mosaic.csv's own (size, mtime_ns) plus a count and
    total size of every file under data/iq/ is enough to detect a campaign still being captured, replayed,
    or having files added/removed/replaced."""
    mosaic_csv = camp / "mosaic.csv"
    try:
        cst = mosaic_csv.stat()
    except OSError:
        return {}
    iq_root = camp / "data" / "iq"
    n_files, total_size = 0, 0
    if iq_root.is_dir():
        for f in iq_root.rglob("*"):
            if f.is_file():
                n_files += 1
                try:
                    total_size += f.stat().st_size
                except OSError:
                    pass
    return {"mosaic_size": cst.st_size, "mosaic_mtime_ns": cst.st_mtime_ns, "iq_file_count": n_files, "iq_total_size": total_size}


def _load_campaign_readiness_cache() -> Dict[str, Any]:
    try:
        return json.loads(_REDUCE_CAMPAIGN_CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_campaign_readiness_cache(cache: Dict[str, Any]) -> None:
    try:
        _REDUCE_CAMPAIGN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_json(_REDUCE_CAMPAIGN_CACHE_PATH, cache)
    except OSError:
        pass  # the cache is purely a speed optimization - never let a write failure break the listing itself


def _reduce_campaign_readiness_cached(camp: Path, cache: Dict[str, Any]) -> Dict[str, Any]:
    """_reduce_campaign_readiness(), but reusing a cached result when the campaign's own signature (see
    _campaign_readiness_signature()) has not changed since it was last computed - mutates `cache` in place
    (the caller persists it once, after the whole listing loop, not per-campaign)."""
    key = str(camp.relative_to(ROOT)) if _within(camp, ROOT) else str(camp)
    sig = _campaign_readiness_signature(camp)
    cached = cache.get(key)
    if cached is not None and cached.get("signature") == sig:
        return cached["readiness"]
    readiness = _reduce_campaign_readiness(camp)
    cache[key] = {"signature": sig, "readiness": readiness}
    return readiness


def _reduce_campaign_readiness(camp: Path) -> Dict[str, Any]:
    """The real per-point classification behind REDUCE's campaign selector and inspector - never just a
    status-column tally. Three real, independent things are cross-checked for every point in mosaic.csv
    (the plan's own manifest), exactly as asked:
      1. manifest: reduce_engine.ingest.discover_campaign()'s own declared-status + accepted/rejected split
         (the SAME thing run_preflight()'s "has_accepted_points" check relies on - never a second,
         diverging definition of "accepted").
      2. paths: a point is only a candidate at all if discover_campaign() resolved a real, non-.part file
         for it (its own by-stem indexer already only looks at .h5/.hdf5 files, so a .part is never even a
         candidate).
      3. valid captures: every candidate is independently re-opened with sdr_capture.validate_hdf5_capture()
         - the SAME frozen validator capture.py's own atomic writer and observation_orchestrator.py's
         generate_final_report() use (metadata/shape only, never reads the full IQ array) - to catch a file
         that mosaic.csv declares "success" and that exists, but is itself truncated/corrupt/incoherent.
    A point mosaic.csv's own visibility_deferred column marks True (observation_orchestrator.py's real,
    existing semantics: the plan itself determined this point was not visible at its scheduled capture time)
    is excluded from the "expected" denominator entirely - a campaign is not INCOMPLETE for a point its own
    plan decided not to capture, so completeness is judged against points_expected = points_total -
    points_deferred, never against the raw row count or a bare status counter.
    """
    import csv as csv_module

    from reduce_engine.ingest import discover_campaign
    from sdr_capture import validate_hdf5_capture

    manifest = discover_campaign(camp)
    deferred_by_point: Dict[int, bool] = {}
    mosaic_csv = camp / "mosaic.csv"
    try:
        with mosaic_csv.open(newline="") as handle:
            for row in csv_module.DictReader(handle):
                try:
                    idx = int(row["point_number"])
                except (KeyError, ValueError, TypeError):
                    continue
                deferred_by_point[idx] = row.get("visibility_deferred") == "True"
    except OSError:
        pass

    points: List[Dict[str, Any]] = []
    for pt in manifest.points:
        deferred = deferred_by_point.get(pt.point_index, False)
        entry: Dict[str, Any] = {
            "point_index": pt.point_index, "deferred": deferred,
            "capture_status_declared": pt.capture_status_declared,
            "resolved_path": str(pt.resolved_path.relative_to(ROOT)) if pt.resolved_path is not None and _within(pt.resolved_path, ROOT) else None,
        }
        if deferred:
            entry["status"] = "DEFERRED"
            entry["reason"] = "not visible at its scheduled capture time - excluded by the plan itself, not missing data"
        elif not pt.accepted:
            entry["status"] = "MISSING"
            entry["reason"] = pt.reject_reason
        elif pt.resolved_path is None or pt.resolved_path.name.endswith(".part"):
            entry["status"] = "MISSING"
            entry["reason"] = "no complete (non-.part) HDF5 file resolved"
        else:
            try:
                validate_hdf5_capture(pt.resolved_path)
                entry["status"] = "USABLE"
                entry["reason"] = None
            except Exception as exc:  # noqa: BLE001 - any h5py/validation failure means genuinely not usable
                entry["status"] = "INVALID"
                entry["reason"] = f"HDF5 failed validation: {exc}"
        points.append(entry)

    total = len(points)
    deferred_n = sum(1 for p in points if p["status"] == "DEFERRED")
    usable_n = sum(1 for p in points if p["status"] == "USABLE")
    expected_n = total - deferred_n
    missing_or_invalid_n = sum(1 for p in points if p["status"] in ("MISSING", "INVALID"))
    if usable_n == 0:
        completeness = "SIN_DATOS"
    elif expected_n > 0 and usable_n >= expected_n:
        completeness = "COMPLETA"
    else:
        completeness = "PARCIAL"

    return {
        "campaign_dir": str(camp.relative_to(ROOT)) if _within(camp, ROOT) else str(camp),
        "campaign_id": manifest.campaign_id, "session_id": manifest.session_id,
        "session_label": _campaign_session_label(camp, manifest.session_id),
        "points_total": total, "points_expected": expected_n, "points_deferred": deferred_n,
        "points_usable": usable_n, "points_missing_or_invalid": missing_or_invalid_n,
        "completeness": completeness, "points": points,
    }


def reduce_list_campaigns(limit: int = 60, scan_budget: int = 200) -> Dict[str, Any]:
    """CAMPAIGN/SESSION selector for REDUCE: only campaigns with at least one USABLE capture (the same real
    classification _reduce_campaign_readiness() computes - see its own docstring) appear in the main list.
    A plan that never captured anything usable (0 accepted, or every declared-success file turning out
    invalid) would otherwise let an operator PLAN/RUN REDUCE against zero real data - exactly the
    400-points/0-accepted confusion this was built to fix. Nothing is deleted, moved, or modified: a
    zero-usable campaign is still discovered and returned under "no_data", for diagnosis, never silently
    dropped from the filesystem or from this function's own result - only kept out of the MAIN list."""
    root = SERVE_ROOTS["mosaic"]
    if not root.is_dir():
        return {"campaigns": [], "no_data": []}
    candidates = sorted(
        (p for p in root.iterdir() if p.is_dir() and (p / "mosaic.csv").is_file()),
        key=lambda p: -p.stat().st_mtime,
    )
    usable_rows: List[Dict[str, Any]] = []
    no_data_rows: List[Dict[str, Any]] = []
    cache = _load_campaign_readiness_cache()
    for p in candidates[:scan_budget]:
        if len(usable_rows) >= limit and len(no_data_rows) >= limit:
            break
        try:
            r = _reduce_campaign_readiness_cached(p, cache)
            row = {
                "campaign_dir": r["campaign_dir"], "name": p.name, "session_label": r["session_label"],
                "points_usable": r["points_usable"], "points_expected": r["points_expected"],
                "points_total": r["points_total"], "points_deferred": r["points_deferred"],
                "completeness": r["completeness"], "mtime": p.stat().st_mtime,
            }
        except (OSError, ValueError, KeyError) as exc:
            row = {"campaign_dir": str(p.relative_to(ROOT)), "name": p.name, "session_label": _campaign_session_label(p, None),
                  "points_usable": 0, "points_expected": None, "points_total": None, "points_deferred": None,
                  "completeness": "ERROR", "error": str(exc), "mtime": p.stat().st_mtime}
        (usable_rows if row["points_usable"] else no_data_rows).append(row)
    _save_campaign_readiness_cache(cache)
    return {"campaigns": usable_rows[:limit], "no_data": no_data_rows[:limit],
           "scanned": min(len(candidates), scan_budget), "discovered": len(candidates)}


def reduce_inspect_capture(rel_path: str) -> Dict[str, Any]:
    """Real, read-only metadata preview of ONE HDF5 capture - reuses reduce_single_capture.py's own function,
    never a second implementation of the attrs read."""
    cap = _file_in({"capture": rel_path}, "capture", (SERVE_ROOTS["mosaic"], SERVE_ROOTS["calibration"]))
    import reduce_single_capture
    return reduce_single_capture.inspect_capture_metadata(cap)


def reduce_inspect_campaign(rel_path: str) -> Dict[str, Any]:
    """Real, read-only campaign discovery - reuses reduce_engine.ingest.discover_campaign() (the SAME function
    almita_reduce.py's own `inspect` subcommand calls) AND _reduce_campaign_readiness() (the same real,
    cross-checked usable/deferred/invalid classification the campaign selector itself uses), never a second,
    diverging definition of "has data" between the list and this per-campaign detail view."""
    camp = _path_in({"campaign_dir": rel_path}, "campaign_dir", SERVE_ROOTS["mosaic"])
    from reduce_engine.ingest import discover_campaign
    manifest = discover_campaign(camp)
    cache = _load_campaign_readiness_cache()
    readiness = _reduce_campaign_readiness_cached(camp, cache)
    _save_campaign_readiness_cache(cache)
    usable_points = [p for p in readiness["points"] if p["status"] == "USABLE"]
    sample = usable_points[0] if usable_points else None
    sample_metadata = None
    if sample is not None and sample["resolved_path"]:
        import reduce_single_capture
        try:
            sample_metadata = reduce_single_capture.inspect_capture_metadata(ROOT / sample["resolved_path"])
        except (OSError, ValueError) as exc:
            sample_metadata = {"error": str(exc)}
    accepted = manifest.accepted_points()  # kept for compatibility with existing callers/tests
    return {
        "campaign_id": manifest.campaign_id, "root": str(manifest.root.relative_to(ROOT)) if _within(manifest.root, ROOT) else str(manifest.root),
        "session_id": manifest.session_id, "session_label": readiness["session_label"],
        "grid": manifest.grid, "observer": manifest.observer.get("observer", {}),
        "points_discovered": len(manifest.points), "points_accepted": len(accepted),
        "points_rejected": [{"point_index": pt.point_index, "reason": pt.reject_reason} for pt in manifest.points if not pt.accepted],
        # The real, cross-checked classification (manifest + paths + independently-validated HDF5 content):
        "points_total": readiness["points_total"], "points_expected": readiness["points_expected"],
        "points_usable": readiness["points_usable"], "points_deferred": readiness["points_deferred"],
        "points_missing_or_invalid": readiness["points_missing_or_invalid"],
        "completeness": readiness["completeness"], "points_detail": readiness["points"],
        "sample_point_index": sample["point_index"] if sample else None,
        "sample_point_metadata": sample_metadata,
    }


def reduce_point_result(rel_session_dir: str, point_index: int) -> Dict[str, Any]:
    """Real, read-only Level-1 result for one REDUCE point - reuses reduce_engine.storage.load_master_spectrum()
    (the same accessor compare/replay use), never re-reads the HDF5 by hand. Arrays are downsampled for the web
    view only when large; the full-resolution master_spectrum.h5/.json remain the real, unmodified artifacts."""
    session_dir = _path_in({"session_dir": rel_session_dir}, "session_dir", SERVE_ROOTS["reduced"])
    point_dir = session_dir / "points" / str(int(point_index))
    if not point_dir.is_dir():
        raise FileNotFoundError(f"no point {point_index} under {rel_session_dir}")
    from reduce_engine.storage import load_master_spectrum
    metadata, arrays = load_master_spectrum(point_dir)
    n = arrays["frequency_hz"].shape[0]
    step = max(1, n // 1600)          # web-view decimation only - the real files keep every bin
    out_arrays = {}
    for key in ("frequency_hz", "relative_intensity", "uncertainty", "mask", "n_contributing", "velocity_lsrk_m_s"):
        if key in arrays:
            out_arrays[key] = [(v.item() if hasattr(v, "item") else v) for v in arrays[key][::step]]
    return {"metadata": metadata, "arrays": out_arrays, "decimation_step": step, "n_bins_full": int(n),
           "master_spectrum_json": str((point_dir / "master_spectrum.json").relative_to(ROOT)),
           "master_spectrum_h5": str((point_dir / "master_spectrum.h5").relative_to(ROOT))}


def reduce_check_compatibility(*, capture_rel: Optional[str], campaign_rel: Optional[str], profile_rel: str) -> Dict[str, Any]:
    """Real compatibility check via calibration_foundation.check_calibration_compatibility() - the SAME
    function REDUCE's own calibration stage and OBSERVE's quicklook use, never reimplemented. For a campaign,
    checks its first accepted point as a representative sample - disclosed as such, never presented as a
    guarantee for every point in the campaign."""
    from calibration_foundation import check_calibration_compatibility, load_calibration_profile
    pp = (ROOT / profile_rel).resolve()
    if not _within(pp, SERVE_ROOTS["calibration"]) or not pp.with_suffix(".json").is_file() or not pp.with_suffix(".npz").is_file():
        raise ValueError("calibration_profile must be an existing .json+.npz pair inside data/calibration")
    profile = load_calibration_profile(pp)
    if capture_rel:
        cap = _file_in({"capture": capture_rel}, "capture", (SERVE_ROOTS["mosaic"], SERVE_ROOTS["calibration"]))
        result = check_calibration_compatibility(profile, cap)
        return {"basis": "single_capture", "capture": str(cap.relative_to(ROOT)), "profile": str(pp.relative_to(ROOT)), **result}
    if campaign_rel:
        camp = _path_in({"campaign_dir": campaign_rel}, "campaign_dir", SERVE_ROOTS["mosaic"])
        from reduce_engine.ingest import discover_campaign
        manifest = discover_campaign(camp)
        accepted = manifest.accepted_points()
        if not accepted or accepted[0].resolved_path is None:
            return {"basis": "campaign_representative_point", "campaign_dir": str(camp.relative_to(ROOT)),
                   "profile": str(pp.relative_to(ROOT)), "status": "UNKNOWN", "reason": "no accepted point with a resolved capture file to check"}
        sample = accepted[0]
        result = check_calibration_compatibility(profile, sample.resolved_path)
        return {"basis": "campaign_representative_point", "campaign_dir": str(camp.relative_to(ROOT)),
               "profile": str(pp.relative_to(ROOT)), "sample_point_index": sample.point_index, **result}
    raise ValueError("either capture or campaign_dir is required")


def reduce_campaign_calibration_preview(campaign_rel: str, profile_rel: str) -> Dict[str, Any]:
    """Per-point compatibility of a CALIBRATE-produced relative profile against EVERY accepted point in a
    campaign - not the single representative sample reduce_check_compatibility() uses for its early, cheap
    banner. Same calibration_foundation.check_calibration_compatibility() the real calibration stage itself
    calls at RUN time (reduce_engine/calibration.py, frozen) - called once per accepted point's real HDF5
    attrs here, read-only, before anything is reduced. This is also the SAME function build_command() calls,
    server-side, to decide whether RUN needs the operator's explicit confirm_uncalibrated - so what this
    preview shows before RUN and what actually gates RUN are never two different computations."""
    from calibration_foundation import check_calibration_compatibility, load_calibration_profile
    from reduce_engine.ingest import discover_campaign
    pp = (ROOT / profile_rel).resolve()
    if not _within(pp, SERVE_ROOTS["calibration"]) or not pp.with_suffix(".json").is_file() or not pp.with_suffix(".npz").is_file():
        raise ValueError("calibration_profile must be an existing .json+.npz pair inside data/calibration")
    profile = load_calibration_profile(pp)
    camp = _path_in({"campaign_dir": campaign_rel}, "campaign_dir", SERVE_ROOTS["mosaic"])
    manifest = discover_campaign(camp)
    accepted = manifest.accepted_points()
    counts = {"COMPATIBLE": 0, "INCOMPATIBLE": 0, "UNKNOWN": 0}
    points: List[Dict[str, Any]] = []
    for pt in accepted:
        if pt.resolved_path is None:
            status, reason = "UNKNOWN", "no resolved capture file for this point"
        else:
            try:
                result = check_calibration_compatibility(profile, pt.resolved_path)
                status, reason = result["status"], result["reason"]
            except (OSError, ValueError, KeyError) as exc:
                status, reason = "UNKNOWN", str(exc)
        counts[status] = counts.get(status, 0) + 1
        points.append({"point_index": pt.point_index, "status": status, "reason": reason})
    all_compatible = counts.get("INCOMPATIBLE", 0) == 0 and counts.get("UNKNOWN", 0) == 0
    return {
        "campaign_dir": str(camp.relative_to(ROOT)), "profile": str(pp.relative_to(ROOT)),
        "total_accepted_points": len(accepted), "counts": counts, "points": points,
        "all_compatible": all_compatible,
        # Whenever any point would NOT get the real relative correction, RUN needs the operator's explicit,
        # typed acknowledgement (confirm_uncalibrated="RUN UNCALIBRATED") - never a silent partial apply.
        "requires_confirmation": not all_compatible,
    }


# ------------------------------------------------------------------ ALIGN: suggested defaults + real sky view (both read-only, no hardware)
def align_defaults() -> Dict[str, Any]:
    """Suggested (never blank) web defaults, from real sources: alignment.py's own argparse defaults for
    everything except beam FWHM and ring_points, which this task deliberately overrides for the web UI:
      - ring_points suggests 8 (25 positions total) - the density already validated by the two most recent real
        ALIGN runs - rather than alignment.py's own historical CLI default of 16.
      - beam FWHM: alignment.py's own PROVISIONAL_BEAM_FWHM_DEG (14.0) and observer_config.json's
        observation_defaults.beam_fwhm_deg (20.0) are a KNOWN, DOCUMENTED disagreement - see
        alignment_engine/config.py's own comment and docs/SCIENCE_SCOPE.md's "Beam FWHM: a documented gap, not a
        guess". SCIENCE V1 refuses to guess between them; alignment_engine/config.py's own resolved_beam_fwhm_deg()
        already prefers observer_config.json over alignment.py's placeholder, so this suggests the same source."""
    import alignment
    a = alignment.parse_args([])
    beam_fwhm, beam_source = a.beam_fwhm, "alignment.py PROVISIONAL_BEAM_FWHM_DEG (placeholder, no documented physical basis)"
    try:
        obs_defaults = json.loads((ROOT / "observer_config.json").read_text()).get("observation_defaults", {})
        if obs_defaults.get("beam_fwhm_deg") is not None:
            beam_fwhm, beam_source = float(obs_defaults["beam_fwhm_deg"]), "observer_config.json: observation_defaults.beam_fwhm_deg"
    except (OSError, ValueError, KeyError):
        pass
    return {"ring_radii_deg": [5.0, 2.0, 0.6], "ring_points": 8, "min_elevation_deg": a.min_elevation, "settle_s": a.settle,
            "capture_time_s": a.integration_seconds, "gain_db": a.gain, "sun_gain_db": a.sun_gain,
            "beam_fwhm_deg": beam_fwhm, "beam_fwhm_source": beam_source,
            "beam_fwhm_note": "alignment.py's own PROVISIONAL_BEAM_FWHM_DEG=14.0 and observer_config.json's beam_fwhm_deg=20.0 disagree "
                             "(documented in alignment_engine/config.py and docs/SCIENCE_SCOPE.md); neither is a measured physical beam. "
                             "This suggests observer_config.json's value, the same preference alignment_engine/config.py's own resolver uses.",
            "duration_model": {
                # overhead_per_point_s: single source of truth is alignment.MEASURED_ACQUIRE_OVERHEAD_PER_POINT_S -
                # the SAME constant alignment.py's own pattern_temporal_altitudes() uses to schedule each real
                # point's capture time, so the web's duration estimate and the real per-point altitude check it
                # feeds (via hi4pi_map.sky_grid_and_ranking()) can never silently disagree.
                "overhead_per_point_s": alignment.MEASURED_ACQUIRE_OVERHEAD_PER_POINT_S,
                "psd_per_20s_capture_s": 16.6, "metric_per_20s_capture_s": 21.0,
                "solar_metric_per_20s_capture_s": 5.8, "ensemble_fixed_s": 0.1,
                "source": f"overhead_per_point_s: {alignment.MEASURED_OVERHEAD_SOURCE}. psd/metric/ensemble: the SAME run's Pass 2 "
                         "analysis: 813.4 s PSD sub-pass / 49 = 16.6 s/point, 1031.3 s metric sub-pass / 49 = 21.0 s/point, "
                         "ensemble+spur detection 0.07 s fixed) and a real timing-only benchmark of compute_sun_metric() on an "
                         "existing capture (SOLAR analysis is one simple pass, no ensemble/spur detection, so much cheaper than "
                         "HI's two full re-reads of every capture). PSD/metric/solar costs are assumed to scale linearly with "
                         "capture time - not independently re-measured at other durations."}}


def align_sky(mode: str, ring_radii_deg: List[float], ring_points: int, min_elevation_deg: float, beam_fwhm_deg: float,
             capture_time_s: float, top_n: int = 3) -> Dict[str, Any]:
    """Real-time (system clock, NTP-checked elsewhere), read-only Alt/Az sky computation - no hardware, no mount
    read. mode='solar': the Sun's current real position and ONE scan area centred on it (never A/B/C, never uses
    the HI map to decide). mode='hi': a continuous Alt/Az raster of the REAL HI4PI column-density map (hi4pi_map.py;
    HI4PI Collaboration 2016, CDS J/A+A/594/A116 - NOT the synthetic model in data/hi_sky_catalog_2000pts.csv) plus
    up to `top_n` candidates ranked from THAT SAME smoothed map/grid (contrast x log1p(mean) within the exterior
    area, centres >= one exterior diameter apart). Both modes' area(s) carry a TEMPORAL check, not just an
    instantaneous one: alignment.pattern_temporal_altitudes() (the SAME function RUN itself calls before its first
    movement) walks every real pattern point (ring_radii_deg/ring_points) forward to the actual wall-clock time it
    would be captured during a run of this length starting now - a ~30-minute, 25-point run visits its last points
    long after PLAN/RUN was clicked, and a centre that clears the limit right now can still have a later point
    fall below it before the run finishes. A HI candidate whose pattern dips below min_elevation_deg at ANY point
    in the run is discarded outright; the SOLAR area is always offered (there is no A/B/C alternative to fall back
    to) but carries the same honest margin/point/time so the operator sees it before RUN's own pre-movement check
    would block it. If the real HI4PI map is missing/unreadable this returns an honest empty ranking with the
    reason - it never falls back to the synthetic model silently; manual HI ALIGN (no suggested area) remains
    available regardless."""
    import alignment
    from astropy.time import Time
    now = Time.now()
    obs = json.loads((ROOT / "observer_config.json").read_text())["observer"]
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    import astropy.units as u
    location = EarthLocation(lat=obs["latitude_deg"] * u.deg, lon=obs["longitude_deg"] * u.deg, height=obs["elevation_m"] * u.m)
    exterior_radius = max(ring_radii_deg) if ring_radii_deg else 5.0
    sun = alignment.sun_eod(now)
    sun_altaz = sun.transform_to(AltAz(obstime=now, location=location))
    out: Dict[str, Any] = {"computed_utc": _utc(), "mode": mode, "exterior_radius_deg": exterior_radius,
                           "sun": {"ra_hours": float(sun.ra.hour), "dec_deg": float(sun.dec.deg), "az_deg": float(sun_altaz.az.deg),
                                   "alt_deg": float(sun_altaz.alt.deg), "above_horizon": bool(sun_altaz.alt.deg > 0)}}
    if mode == "solar":
        area = {"label": "SUN", "ra_hours": out["sun"]["ra_hours"], "dec_deg": out["sun"]["dec_deg"],
                "az_deg": out["sun"]["az_deg"], "alt_deg": out["sun"]["alt_deg"], "radius_deg": exterior_radius}
        temporal_ok = True
        if out["sun"]["above_horizon"]:
            temporal = alignment.pattern_temporal_altitudes(sun, ring_radii_deg, ring_points, location, now, capture_time_s,
                                                            alignment.MEASURED_ACQUIRE_OVERHEAD_PER_POINT_S)
            margin = temporal["min_altitude_deg"] - min_elevation_deg
            temporal_ok = margin >= 0
            area.update(pattern_min_altitude_deg=temporal["min_altitude_deg"], pattern_margin_deg=margin,
                        pattern_min_altitude_point_index=temporal["min_altitude_point_index"],
                        pattern_min_altitude_utc=temporal["min_altitude_utc"], run_duration_s=temporal["run_duration_s"])
            note = (f"Sun's pattern clears {min_elevation_deg:g} deg altitude for the whole "
                    f"~{temporal['run_duration_s']/60:.0f} min run (worst margin {margin:+.2f} deg, at point "
                    f"{temporal['min_altitude_point_index']}, ~{temporal['min_altitude_utc']})" if temporal_ok else
                    f"WARNING: the Sun's pattern would fall to {temporal['min_altitude_deg']:.2f} deg altitude "
                    f"(below {min_elevation_deg:g} deg) at point {temporal['min_altitude_point_index']} "
                    f"(~{temporal['min_altitude_utc']}) before this ~{temporal['run_duration_s']/60:.0f} min run "
                    f"finishes - RUN will refuse to move once it reaches that point; PLAN again closer to RUN "
                    f"time or shrink the pattern/reduce MIN ELEVATION")
        else:
            note = "Sun is below the horizon right now - no temporal check performed"
        out["areas"] = [area]
        out["ranking_defendible"] = True
        out["ranking_note"] = f"solar: always one area centred on the Sun's real current position; the HI map/ranking never chooses solar's area. {note}"
        return out
    if mode != "hi":
        raise ValueError("mode must be hi or solar")
    import hi4pi_map
    try:
        real = hi4pi_map.sky_grid_and_ranking(location, now, min_elevation_deg, beam_fwhm_deg, exterior_radius,
                                              ring_radii_deg, ring_points, capture_time_s, grid_n=90, top_n=top_n)
    except hi4pi_map.HI4PIUnavailable as exc:
        out["areas"], out["ranking_defendible"] = [], False
        out["hi4pi_error"] = str(exc)
        out["ranking_note"] = f"real HI4PI map unavailable ({exc}) - use manual HI ALIGN (no suggested area); the synthetic model is NOT used as a substitute"
        return out
    out["catalog_source"] = real["source"]
    out["hi4pi_grid"] = {"n": real["grid_n"], "values_1e20cm2": real["grid"], "value_range_1e20cm2": real["value_range_1e20cm2"]}
    out["areas"] = real["areas"]
    for a_ in out["areas"]:
        a_["radius_deg"] = exterior_radius
    out["ranking_defendible"] = bool(real["areas"])
    n_found, n_wanted = len(real["areas"]), top_n
    run_min = (real["areas"][0]["run_duration_s"] / 60) if real["areas"] else None
    pattern_bit = (f"each candidate's WHOLE ring pattern ({len(ring_radii_deg)} ring(s) x {ring_points} pts, max radius "
                   f"{exterior_radius:g} deg) clears {min_elevation_deg:g} deg altitude not just now but AT THE REAL TIME "
                   f"each point would be captured throughout the ~{run_min:.0f} min run" if run_min is not None else
                   f"each candidate's WHOLE ring pattern ({len(ring_radii_deg)} ring(s) x {ring_points} pts, max radius "
                   f"{exterior_radius:g} deg) - not just its centre - clears {min_elevation_deg:g} deg altitude throughout the run"
                   f" (worst margin shown per candidate)")
    if n_found == n_wanted and n_found > 0:
        out["ranking_note"] = (f"ranked by contrast x log1p(mean N_HI) within the exterior area, from the REAL HI4PI map beam-smoothed by "
                               f"{beam_fwhm_deg:g} deg FWHM ({real['candidates_considered']} candidates above {min_elevation_deg:g} deg altitude); "
                               f"{pattern_bit}; centres >= {2 * exterior_radius:.2f} deg apart (one exterior diameter); "
                               f"the operator picks A, B or C - never chosen automatically")
    elif n_found > 0:
        out["ranking_note"] = (f"only {n_found} of {n_wanted} requested candidates found: {real['candidates_pattern_rejected']} higher-ranked "
                               f"candidate(s) were discarded because part of their ring pattern falls below {min_elevation_deg:g} deg altitude "
                               f"at some point during the ~{run_min:.0f} min run (worst rejected margin "
                               f"{real['worst_rejected_pattern_margin_deg']:+.2f} deg) - showing only the {n_found} valid one(s); {pattern_bit}. "
                               f"Lower MIN ELEVATION, shrink the ring radii/points, or wait, then refresh.")
    else:
        reason = (f"{real['candidates_pattern_rejected']} candidate(s) scored but every one's ring pattern dips below "
                  f"{min_elevation_deg:g} deg altitude at some point during the run (worst rejected margin "
                  f"{real['worst_rejected_pattern_margin_deg']:+.2f} deg)"
                  if real["candidates_pattern_rejected"] else
                  f"no HI4PI region clears {min_elevation_deg:g} deg altitude right now")
        out["ranking_note"] = f"{reason} - no defendible ranking; keep the existing manual HI ALIGN"
    return out


# ------------------------------------------------------------------ detached runner (this file, --runner)
def _runner(job_path: str) -> int:
    p = Path(job_path)
    job = json.loads(p.read_text())
    log = open(job["log"], "ab", buffering=0)
    log.write(f"[{_utc()}] $ {' '.join(job['argv'])}\n".encode())
    state = {"stopped": False}
    child = subprocess.Popen(job["argv"], cwd=job["cwd"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    job = json.loads(p.read_text())
    job.update(child_pid=child.pid, runner_pid=os.getpid())
    _write_json(p, job)

    def forward(signum, frame):  # noqa: ARG001
        state["stopped"] = True
        try:
            child.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGINT, forward)
    rc = child.wait()
    log.write(f"[{_utc()}] exit code {rc}\n".encode())
    job = json.loads(p.read_text())
    job.update(exit_code=rc, ended_utc=_utc(), stopped_by_operator=state["stopped"])
    if job.get("stage") in ("reduce_plan", "reduce_capture_plan") and rc == 0:
        # Captured ONCE, right here, the moment PLAN's own subprocess actually finished reading these files -
        # a true snapshot, never recomputed later. classify() is a stateless log parser re-run on every poll;
        # computing "PLAN-time" signatures there would silently re-read the CURRENT disk instead (the bug this
        # replaces - caught during this feature's own verification: a file mutated after PLAN was still
        # reported as matching, because the signature was being freshly recomputed on every classify() call
        # rather than pinned to when PLAN actually ran).
        try:
            job["file_signatures"] = _reduce_input_signatures(job.get("meta") or {})
        except (OSError, FileNotFoundError, ValueError):
            job["file_signatures"] = None
    _write_json(p, job)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--runner":
        sys.exit(_runner(sys.argv[2]))
    sys.exit("almita_web_ops.py is a library for almita_orchestrator_server.py (and its own --runner)")
