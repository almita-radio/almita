#!/usr/bin/env python3
"""ALMITA SURVEY pilot - runs a survey (survey.py) by itself, batch after batch, until every tile has its passes.

Each cycle:
  1. gates: the header's mount selector (SIM only if the survey allows it), no other web job running, no other
     observation active;
  2. survey.choose_batch(): tiles still owing passes, above the floor for the whole batch, away from the Sun;
  3. OBSERVE PLAN (observation_plan.plan_target_list) + RUN (observation_orchestrator.run_observation, the same call
     the web START makes, unattended) - capture.py does the pointing and the capture, with its own guards;
  4. waits for capture.py to finish; REDUCE (reduce_campaign_run.py run --velocity-frame lsrk --rfi-mask
     --estimator-v2, the options of the valid sessions) in a subprocess;
  5. credits one pass to each tile with an accepted point, saves, picks the next batch.

Safety: never SIGTERM - the user unit stops with SIGINT, which reaches capture.py too (its own clean stop); the
pilot then waits for it and saves. Three failed batches in a row, or two batches in a row where no point was
accepted (SDR mute, RFI, mount), HALT the pilot with the reason in survey.json - it does not keep capturing junk.
A batch captured but not reduced (stopped mid-REDUCE, crash) is reduced first on the next start.

  python3 almita_survey.py create SCP-PETORCA "Polo sur celeste" --radius 30 --spacing 7.5 --passes 4
  python3 almita_survey.py activate SCP-PETORCA
  python3 almita_survey.py run                 # the pilot (almita-survey.service)
  python3 almita_survey.py status [ID]
  python3 almita_survey.py simulate ID --start 2026-12-06T21:00 --hours 120   # dry run, no hardware
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import survey

ROOT = Path(__file__).resolve().parent
PY = sys.executable
MAX_FAILED_BATCHES = 3
MAX_EMPTY_BATCHES = 2
POLL_S = 15.0
WAIT_NO_TILES_S = 300.0
WAIT_GATE_S = 60.0
WAIT_AFTER_FAILURE_S = 120.0
STOP_WAIT_S = 150.0

_stop = False


def _on_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    print(f"pilot: signal {signum} - stopping after the current step (capture.py gets its own SIGINT from the unit)",
          flush=True)


def _sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def log(msg: str) -> None:
    print(f"{survey.utcnow()} {msg}", flush=True)


# ------------------------------------------------------------------------------------------------------ gates

def gate_reason(state: Dict[str, Any], check_experiment: bool = True) -> Optional[str]:
    """Why the pilot must not start a batch now, or None. The EXPERIMENT runner (2026-11-21) uses the same gates
    with check_experiment=False; for the SURVEY pilot a running experiment is another driver of the mount."""
    import mount_device
    import observation_orchestrator
    if check_experiment:
        try:
            import experiment
            if experiment.service_state().get("active"):
                return "an EXPERIMENT is running (one driver of the mount at a time)"
        except Exception as exc:  # noqa: BLE001 - an unreadable state is a reason to wait, not to go
            return f"cannot read the EXPERIMENT runner's state ({type(exc).__name__}: {exc})"
    if mount_device.is_simulator() and not state["definition"].get("allow_simulator"):
        return "the mount selector in the page header is on SIM; this survey is for the real sky (LX200)"
    try:
        import almita_web_ops
        jobs = almita_web_ops.running_jobs()
    except Exception as exc:  # noqa: BLE001 - an unreadable job list is a reason to wait, not to go
        return f"cannot read the web job list ({type(exc).__name__}: {exc})"
    if jobs:
        return "web job(s) running: " + ", ".join(f"{j.get('stage')}" for j in jobs)
    status = observation_orchestrator.get_status()
    reason = observation_orchestrator.active_campaign_reason(status.get("orchestrator") or status)
    if reason:
        return "another observation is active: " + reason
    return None


def _observation_active() -> bool:
    import observation_orchestrator
    status = observation_orchestrator.get_status()
    return observation_orchestrator.active_campaign_reason(status.get("orchestrator") or status) is not None


# ------------------------------------------------------------------------------------------------------ REDUCE

def run_reduce(campaign_dir: str) -> Dict[str, Any]:
    rel = str(Path(campaign_dir).resolve().relative_to(ROOT))
    argv = [PY, "reduce_campaign_run.py", "run", rel, "--json", "--velocity-frame", "lsrk", "--rfi-mask", "--estimator-v2"]
    r = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=3 * 3600)
    out = r.stdout or ""
    start = out.find("{")
    try:
        payload = json.loads(out[start:]) if start >= 0 else {}
    except ValueError:
        payload = {}
    return {"returncode": r.returncode, "output_dir": payload.get("output_dir"), "status": payload.get("status"),
            "stderr_tail": (r.stderr or "")[-600:]}


def reduced_points(output_dir: str) -> List[Dict[str, Any]]:
    """Every reduced point of a REDUCE run: its pointing, whether it was accepted, and its reading (the sky map's
    line area, % x km/s) - accepted means a COMPLETED master spectrum with a finite reading."""
    import h5py
    import numpy as np
    import site_sky_export
    base = Path(output_dir)
    if not base.is_absolute():
        base = ROOT / base
    status = {}
    try:
        manifest = json.loads((base / "manifest.json").read_text())
        status = {int(p["point_index"]): p.get("status") for p in manifest.get("points", [])}
    except (OSError, ValueError, KeyError):
        pass
    out = []
    for p in sorted(glob.glob(str(base / "points" / "*" / "master_spectrum.json"))):
        meta = json.loads(Path(p).read_text())
        idx = int(meta["point_index"])
        reading = None
        try:
            with h5py.File(Path(p).with_suffix(".h5"), "r") as f:
                v = f["velocity_lsrk_m_s"][:] / 1e3
                y = f["relative_intensity"][:] * 100.0
                m = f["mask"][:]
            ok = (m == 0) & np.isfinite(y) & np.isfinite(v)
            o = np.argsort(v[ok])
            reading = site_sky_export.line_area(v[ok][o], y[ok][o])
        except (OSError, KeyError, ValueError):
            reading = None
        good = reading is not None and math.isfinite(reading) and status.get(idx, "COMPLETED") == "COMPLETED"
        out.append({"point_index": idx, "ra_hours": float(meta["ra_hours"]), "dec_deg": float(meta["dec_degrees"]),
                    "accepted": bool(good), "reading": round(reading, 2) if good else None,
                    "quality": (meta.get("quality") or {}).get("state")})
    return out


# ------------------------------------------------------------------------------------------------------ one batch

def _wait_capture_done(state: Dict[str, Any]) -> None:
    while _observation_active():
        if _stop:
            deadline = time.monotonic() + STOP_WAIT_S
            while _observation_active() and time.monotonic() < deadline:
                time.sleep(2)
            return
        time.sleep(POLL_S)


def reduce_and_credit(state: Dict[str, Any], batch: Dict[str, Any]) -> Dict[str, Any]:
    survey.set_pilot(state, "REDUCING", f"REDUCE of batch {batch['batch_id']}", current_batch=batch["batch_id"])
    survey.save(state)
    red = run_reduce(batch["campaign_dir"])
    batch["reduce"] = red
    if not red.get("output_dir"):
        batch["status"] = "REDUCE_FAILED"
        batch["reduce_attempts"] = batch.get("reduce_attempts", 0) + 1
        return {"credited": [], "missed": [t["id"] for t in batch["tiles"]], "unmatched": 0}
    batch["reduce_run"] = Path(red["output_dir"]).name
    pts = reduced_points(red["output_dir"])
    batch["points"] = pts
    result = survey.record_batch(state, batch, pts)
    batch["status"] = "DONE"
    batch["credited"] = result["credited"]
    batch["missed"] = result["missed"]
    return result


def run_batch(state: Dict[str, Any], location, last_pointing) -> Dict[str, Any]:
    """Plan + run + reduce one batch. Returns {"outcome": NO_TILES|PLAN_BLOCKED|RUN_FAILED|STOPPED|DONE, ...}."""
    import mount_device
    import observation_orchestrator
    import observation_plan
    import sdr_tuning
    choice = survey.choose_batch(state, location, last_pointing=last_pointing)
    if not choice["tiles"]:
        return {"outcome": "NO_TILES", "reason": choice["reason"]}
    spec = survey.batch_spec(state, choice["tiles"], sdr_tuning.operating_config()["gain_db"])
    try:
        plan = observation_plan.plan_target_list(spec, data_dir=str(ROOT / "data" / "mosaic"))
    except observation_plan.ObservationPlanError as exc:
        return {"outcome": "PLAN_BLOCKED", "reason": str(exc)}
    batch = {"batch_id": f"B{len(state['batches']) + 1:04d}", "planned_utc": survey.utcnow(),
             "tiles": choice["tiles"], "reason": choice["reason"], "campaign_dir": plan["grid_session_dir"],
             "campaign_id": Path(plan["grid_session_dir"]).name, "resolved_plan": plan["_resolved_plan_path"],
             "mount": mount_device.current(), "status": "PLANNED"}
    state["batches"].append(batch)
    survey.set_pilot(state, "CAPTURING", f"batch {batch['batch_id']}: {choice['reason']}", current_batch=batch["batch_id"])
    survey.save(state)
    log(f"batch {batch['batch_id']} {[t['id'] for t in choice['tiles']]} -> {batch['campaign_id']}")
    try:
        observation_orchestrator.run_observation(plan["_resolved_plan_path"], yes=True, device_name=mount_device.current())
    except observation_orchestrator.OrchestratorError as exc:
        batch["status"] = "RUN_FAILED"
        batch["error"] = str(exc)[:800]
        survey.save(state)
        return {"outcome": "RUN_FAILED", "reason": str(exc)}
    batch["started_utc"] = survey.utcnow()
    survey.save(state)
    _wait_capture_done(state)
    batch["ended_utc"] = survey.utcnow()
    batch["status"] = "CAPTURED"
    survey.save(state)
    if _stop:
        return {"outcome": "STOPPED", "reason": "stopped by the operator; the batch is reduced on the next start"}
    result = reduce_and_credit(state, batch)
    survey.save(state)
    return {"outcome": "DONE", "result": result,
            "last": choice["tiles"][-1]}


# ------------------------------------------------------------------------------------------------------ the pilot

def pilot(survey_id: Optional[str] = None) -> int:
    """The pilot, with any crash written into survey.json (pilot.state = CRASHED) before it propagates: the unit then
    restarts it after a minute and it resumes."""
    sid = survey_id or survey.active_id()
    try:
        return _pilot(sid)
    except Exception as exc:
        if sid:
            try:
                st = survey.load(sid)
                survey.set_pilot(st, "CRASHED", f"{type(exc).__name__}: {exc}"[:500])
                survey.save(st)
            except Exception:  # noqa: BLE001 - the original error is the one that matters
                pass
        raise


def _pilot(sid: Optional[str]) -> int:
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if not sid:
        log("no active survey (data/surveys/active.json) - nothing to do")
        return 1
    state = survey.load(sid)
    location = survey.site_location(state, allow_override=False)
    failed, empty = 0, 0
    last_pointing = None
    log(f"pilot start: survey {sid} {survey.progress(state)}")
    # recovery: a batch captured (or whose REDUCE failed) before the last stop is reduced first
    for b in state["batches"]:
        if b.get("status") == "PLANNED":
            # the pilot died after RUN: let that capture.py finish, then keep whatever it captured
            if _observation_active():
                log(f"recovery: batch {b['batch_id']} is still capturing - waiting for it")
                _wait_capture_done(state)
            has_data = any(Path(b["campaign_dir"]).rglob("*.h5")) if b.get("campaign_dir") else False
            b["status"] = "CAPTURED" if has_data else "ABANDONED"
            b.setdefault("ended_utc", survey.utcnow())
            survey.save(state)
        if b.get("status") == "CAPTURED" or (b.get("status") == "REDUCE_FAILED" and b.get("reduce_attempts", 0) < 2):
            if _stop:
                break
            log(f"recovery: reducing batch {b['batch_id']}")
            reduce_and_credit(state, b)
            survey.save(state)
    while not _stop:
        state = survey.load(sid)
        if survey.progress(state)["complete"]:
            survey.set_pilot(state, "COMPLETE", "every tile has its passes")
            survey.save(state)
            log("survey complete")
            return 0
        reason = gate_reason(state)
        if reason:
            survey.set_pilot(state, "WAITING", reason)
            survey.save(state)
            _sleep(WAIT_GATE_S)
            continue
        out = run_batch(state, location, last_pointing)
        state = survey.load(sid)
        o = out["outcome"]
        if o == "NO_TILES":
            survey.set_pilot(state, "WAITING", out["reason"])
            survey.save(state)
            _sleep(WAIT_NO_TILES_S)
            continue
        if o == "PLAN_BLOCKED":
            survey.set_pilot(state, "WAITING", "plan blocked: " + out["reason"][:300])
            survey.save(state)
            _sleep(WAIT_AFTER_FAILURE_S)
            continue
        if o == "STOPPED":
            break
        if o == "RUN_FAILED":
            failed += 1
            if failed >= MAX_FAILED_BATCHES:
                survey.set_pilot(state, "HALTED", f"{failed} batches in a row failed to start: {out['reason'][:400]}")
                survey.save(state)
                log("HALTED: " + out["reason"])
                return 2
            survey.set_pilot(state, "WAITING", f"batch failed to start ({failed}/{MAX_FAILED_BATCHES}): {out['reason'][:300]}")
            survey.save(state)
            _sleep(WAIT_AFTER_FAILURE_S)
            continue
        failed = 0
        res = out["result"]
        last_pointing = out["last"]
        empty = empty + 1 if not res["credited"] else 0
        if empty >= MAX_EMPTY_BATCHES:
            survey.set_pilot(state, "HALTED", f"{empty} batches in a row with no accepted point (SDR mute? RFI? mount?) "
                                              "- look at the last batch before starting again")
            survey.save(state)
            log("HALTED: no accepted points")
            return 2
        survey.set_pilot(state, "RUNNING", f"last batch credited {len(res['credited'])} tile(s), missed {len(res['missed'])}")
        survey.save(state)
    state = survey.load(sid)
    survey.set_pilot(state, "STOPPED", "stopped by the operator")
    survey.save(state)
    log("pilot stopped")
    return 0


# ------------------------------------------------------------------------------------------------------ dry run

def simulate(state: Dict[str, Any], start: datetime, hours: float, success: float = 1.0) -> Dict[str, Any]:
    """Dry run: the pilot's scheduling over `hours` from `start`, assuming each batch takes its planned time and
    every point is accepted with probability `success` (deterministic: every k-th point fails). No hardware, no
    files. Returns the timeline and when the survey would complete."""
    import copy
    st = copy.deepcopy(state)
    loc = survey.site_location(st)
    per_point = survey.per_point_seconds(st["definition"])
    t, end = start, start + timedelta(hours=hours)
    last, timeline, k, completed_at = None, [], 0, None
    while t < end:
        choice = survey.choose_batch(st, loc, now=t, last_pointing=last)
        if not choice["tiles"]:
            if survey.progress(st)["complete"]:
                completed_at = completed_at or t
                break
            t += timedelta(minutes=10)
            continue
        pts = []
        for tile in choice["tiles"]:
            k += 1
            ok = success >= 1.0 or (k % max(2, int(round(1 / max(1e-6, 1 - success))))) != 0
            pts.append({"ra_hours": tile["ra_hours"], "dec_deg": tile["dec_deg"], "accepted": ok, "reading": None})
        dur = timedelta(seconds=per_point * len(choice["tiles"]) + 60)
        batch = {"tiles": choice["tiles"], "ended_utc": (t + dur).isoformat()}
        survey.record_batch(st, batch, pts)
        st["batches"].append(batch)
        timeline.append({"utc": t.isoformat(timespec="minutes"), "tiles": [x["id"] for x in choice["tiles"]],
                         "progress": survey.progress(st)["fraction"]})
        t += dur
        last = choice["tiles"][-1]
        if survey.progress(st)["complete"]:
            completed_at = t
            break
    return {"completed_utc": completed_at.isoformat(timespec="minutes") if completed_at else None,
            "hours_to_complete": round((completed_at - start).total_seconds() / 3600, 1) if completed_at else None,
            "batches": len(timeline), "progress": survey.progress(st), "timeline": timeline}


# ------------------------------------------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("survey_id")
    c.add_argument("name")
    c.add_argument("--centre-ra", type=float, default=0.0)
    c.add_argument("--centre-dec", type=float, default=-90.0)
    c.add_argument("--radius", type=float, default=30.0)
    c.add_argument("--spacing", type=float, default=7.5)
    c.add_argument("--passes", type=int, default=4)
    c.add_argument("--min-alt", type=float, default=25.0)
    c.add_argument("--capture", type=float, default=120.0)
    c.add_argument("--batch", type=int, default=6)
    c.add_argument("--site-lat", type=float, default=None)
    c.add_argument("--site-lon", type=float, default=None)
    c.add_argument("--allow-simulator", action="store_true")
    a = sub.add_parser("activate")
    a.add_argument("survey_id")
    sub.add_parser("run")
    s = sub.add_parser("status")
    s.add_argument("survey_id", nargs="?")
    m = sub.add_parser("simulate")
    m.add_argument("survey_id")
    m.add_argument("--start", required=True, help="UTC, e.g. 2026-12-07T00:00")
    m.add_argument("--hours", type=float, default=24.0)
    m.add_argument("--success", type=float, default=1.0)
    args = ap.parse_args(argv)
    if args.cmd == "create":
        st = survey.new_survey(args.survey_id, args.name, centre_ra_hours=args.centre_ra, centre_dec_deg=args.centre_dec,
                               radius_deg=args.radius, spacing_deg=args.spacing, passes=args.passes,
                               min_altitude_deg=args.min_alt, capture_seconds=args.capture, batch_points=args.batch,
                               site_lat_deg=args.site_lat, site_lon_deg=args.site_lon, allow_simulator=args.allow_simulator)
        if (survey.survey_dir(args.survey_id) / "survey.json").exists():
            print(f"survey {args.survey_id} already exists - not overwritten")
            return 1
        survey.save(st)
        print(json.dumps({"survey_id": st["survey_id"], **survey.progress(st)}, indent=2))
        return 0
    if args.cmd == "activate":
        survey.load(args.survey_id)
        survey.set_active(args.survey_id)
        print(f"active survey: {args.survey_id}")
        return 0
    if args.cmd == "run":
        return pilot()
    if args.cmd == "status":
        sid = args.survey_id or survey.active_id()
        st = survey.load(sid)
        print(json.dumps({"survey_id": sid, "pilot": st.get("pilot"), **survey.progress(st)}, indent=2))
        return 0
    if args.cmd == "simulate":
        st = survey.load(args.survey_id)
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        r = simulate(st, start, args.hours, args.success)
        r["timeline"] = r["timeline"][:3] + (["..."] if len(r["timeline"]) > 6 else []) + r["timeline"][-3:]
        print(json.dumps(r, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
