#!/usr/bin/env python3
"""ALMITA EXPERIMENT runner - one recipe run (experiment.py), batch by batch, then its verdict.

Each batch, in the recipe's order:
  1. gates: the header's mount selector (SIM only if the recipe allows it), no web job, no other observation,
     and the SURVEY pilot not running (one driver of the mount at a time);
  2. experiment.resolve_step(): the batch's pointings for a start now (same-altitude OFFs computed here), dropping
     what is too low or near the Sun - a batch with nothing left is SKIPPED, the run goes on;
  3. OBSERVE PLAN (observation_plan.plan_target_list) + RUN (observation_orchestrator.run_observation, the same
     unattended call the SURVEY pilot makes) in the batch's switching mode;
  4. waits for capture.py, then REDUCE at once (reduce_campaign_run.py run --velocity-frame lsrk --rfi-mask
     --estimator-v2 - almita_survey.run_reduce), so a mute SDR is caught after one batch, not after the night;
  5. after the last batch: the recipe's analysis and verdict, written into run.json.

Safety (the SURVEY pilot's rules): never SIGTERM - the user unit stops with SIGINT, which reaches capture.py too;
the runner waits for it and saves; the captured batch is reduced on RESUME. Three failed starts in a row, or two
batches in a row with no accepted point, HALT the run (exit 2, the unit does not restart it). A run stopped or
halted still gets a (partial) analysis of what it has.

  python3 almita_experiment.py recipes                     # what can run now / when
  python3 almita_experiment.py start VEL-AB --open-sky     # new run + start the unit (as the page's START does)
  python3 almita_experiment.py run                         # the runner itself (almita-experiment.service)
  python3 almita_experiment.py analyse RUN_ID              # (re)compute a run's verdict
  python3 almita_experiment.py replay SMC-ONOFF --survey MC-SMC-ONOFF   # try a recipe's verdict on old data
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import experiment as X

ROOT = Path(__file__).resolve().parent
MAX_FAILED_STARTS = 3
MAX_EMPTY_BATCHES = 2
MAX_PLAN_BLOCKED = 2
WAIT_GATE_S = 60.0
WAIT_AFTER_FAILURE_S = 120.0
POLL_S = 10.0
STOP_WAIT_S = 150.0

_stop = False


def _on_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    print(f"experiment: signal {signum} - stopping after the current step (capture.py gets its own SIGINT)", flush=True)


def _sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def log(msg: str) -> None:
    print(f"{X.utcnow()} {msg}", flush=True)


def gate_reason(run: Dict[str, Any]) -> Optional[str]:
    import almita_survey
    import survey
    if survey.service_state().get("active"):
        return "the SURVEY pilot is running (one driver of the mount at a time)"
    return almita_survey.gate_reason({"definition": {"allow_simulator": bool(run["recipe"].get("allow_simulator"))}},
                                     check_experiment=False)


def _observation_active() -> bool:
    import almita_survey
    return almita_survey._observation_active()


def _wait_capture_done() -> None:
    while _observation_active():
        if _stop:
            deadline = time.monotonic() + STOP_WAIT_S
            while _observation_active() and time.monotonic() < deadline:
                time.sleep(2)
            return
        time.sleep(POLL_S)


def reduce_step(run: Dict[str, Any], step: Dict[str, Any]) -> int:
    """REDUCE one captured batch and match its points to the batch targets. Returns accepted points."""
    import almita_survey
    X.set_pilot(run, "REDUCING", f"REDUCE of batch {step['index'] + 1}", step=step["index"])
    X.save_run(run)
    red = almita_survey.run_reduce(step["campaign_dir"])
    step["reduce"] = {k: red.get(k) for k in ("returncode", "status", "stderr_tail")}
    if not red.get("output_dir"):
        step["status"] = "REDUCE_FAILED"
        step["reduce_attempts"] = step.get("reduce_attempts", 0) + 1
        return 0
    step["output_dir"] = red["output_dir"]
    step["reduce_run"] = Path(red["output_dir"]).name
    step["points"] = X.match_points(step, almita_survey.reduced_points(red["output_dir"]))
    step["status"] = "DONE"
    return sum(1 for p in step["points"] if p.get("accepted"))


def run_step(run: Dict[str, Any], step: Dict[str, Any], location) -> str:
    """Resolve + plan + run + reduce one batch. Returns SKIPPED | PLAN_BLOCKED | RUN_FAILED | STOPPED | DONE."""
    import mount_device
    import observation_orchestrator
    import observation_plan
    import sdr_tuning
    rc = run["recipe"]
    res = X.resolve_step(rc, step, location)
    step["dropped"] = res["dropped"]
    if not res["targets"]:
        step["status"] = "SKIPPED"
        step["skipped_reason"] = "; ".join(f"{d['id']}: {d['reason']}" for d in res["dropped"])[:600]
        return "SKIPPED"
    spec = X.batch_spec(rc, run["run_id"], step, res["targets"], sdr_tuning.operating_config()["gain_db"])
    try:
        plan = observation_plan.plan_target_list(spec, data_dir=str(ROOT / "data" / "mosaic"))
    except observation_plan.ObservationPlanError as exc:
        step["plan_blocked"] = step.get("plan_blocked", 0) + 1
        step["error"] = str(exc)[:600]
        return "PLAN_BLOCKED"
    step.update({"targets": res["targets"], "campaign_dir": plan["grid_session_dir"], "campaign_id": Path(plan["grid_session_dir"]).name,
                 "resolved_plan": plan["_resolved_plan_path"], "mount": mount_device.current(), "planned_utc": X.utcnow(),
                 "status": "PLANNED"})
    X.set_pilot(run, "CAPTURING", f"batch {step['index'] + 1}/{len(run['steps'])}: mode {step['mode']} · "
                                  f"{', '.join(t['id'] for t in res['targets'])}", step=step["index"])
    X.save_run(run)
    log(f"{run['run_id']} batch {step['index'] + 1} mode {step['mode']} {[t['id'] for t in res['targets']]} -> {step['campaign_id']}")
    try:
        observation_orchestrator.run_observation(plan["_resolved_plan_path"], yes=True, device_name=mount_device.current())
    except observation_orchestrator.OrchestratorError as exc:
        step["status"] = "PENDING"           # the step is tried again
        step["error"] = str(exc)[:600]
        step["start_failures"] = step.get("start_failures", 0) + 1
        X.save_run(run)
        return "RUN_FAILED"
    step["started_utc"] = X.utcnow()
    X.save_run(run)
    _wait_capture_done()
    step["ended_utc"] = X.utcnow()
    step["status"] = "CAPTURED"
    X.save_run(run)
    if _stop:
        return "STOPPED"
    step["accepted"] = reduce_step(run, step)
    X.save_run(run)
    return "DONE"


def finish(run: Dict[str, Any], status: str, note: str) -> None:
    run["status"] = status
    if any(s.get("status") == "DONE" for s in run["steps"]):
        try:
            run["analysis"] = X.analyse(run)
            run["analysis"]["partial"] = any(s.get("status") not in ("DONE", "SKIPPED") for s in run["steps"])
        except Exception as exc:  # noqa: BLE001 - the data stay; the verdict can be recomputed with `analyse`
            run["analysis"] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
    X.set_pilot(run, status, note)
    X.save_run(run)


def runner(run_id: Optional[str] = None) -> int:
    rid = run_id or X.active_id()
    try:
        return _runner(rid)
    except Exception as exc:
        if rid:
            try:
                r = X.load_run(rid)
                X.set_pilot(r, "CRASHED", f"{type(exc).__name__}: {exc}"[:500])
                r["status"] = "CRASHED"
                X.save_run(r)
            except Exception:  # noqa: BLE001
                pass
        raise


def _runner(rid: Optional[str]) -> int:
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if not rid:
        log("no active experiment run (data/experiments/active.json) - nothing to do")
        return 1
    run = X.load_run(rid)
    if run["status"] in ("DONE", "REPLAY"):
        log(f"run {rid} is {run['status']} - nothing to do")
        return 0
    location = X.site_location()
    run["status"] = "RUNNING"
    X.set_pilot(run, "RUNNING", "started")
    X.save_run(run)
    log(f"experiment start: {rid} ({len(run['steps'])} batches)")
    # recovery: a batch the last stop left captured (or half-done) is reduced first
    for s in run["steps"]:
        if s.get("status") == "PLANNED":
            if _observation_active():
                log(f"recovery: batch {s['index'] + 1} is still capturing - waiting")
                _wait_capture_done()
            has = any(Path(s["campaign_dir"]).rglob("*.h5")) if s.get("campaign_dir") else False
            s["status"] = "CAPTURED" if has else "PENDING"
            X.save_run(run)
        if s.get("status") == "CAPTURED" or (s.get("status") == "REDUCE_FAILED" and s.get("reduce_attempts", 0) < 2):
            if _stop:
                break
            log(f"recovery: reducing batch {s['index'] + 1}")
            s["accepted"] = reduce_step(run, s)
            X.save_run(run)
    failed, empty = 0, 0
    while not _stop:
        run = X.load_run(rid)
        step = next((s for s in run["steps"] if s.get("status") == "PENDING"), None)
        if step is None:
            finish(run, "DONE", "every batch done - verdict written")
            log(f"experiment done: {rid}")
            return 0
        reason = gate_reason(run)
        if reason:
            X.set_pilot(run, "WAITING", reason, step=step["index"])
            X.save_run(run)
            _sleep(WAIT_GATE_S)
            continue
        out = run_step(run, step, location)
        if out == "SKIPPED":
            log(f"batch {step['index'] + 1} skipped: {step.get('skipped_reason')}")
            X.save_run(run)
            continue
        if out == "PLAN_BLOCKED":
            if step["plan_blocked"] >= MAX_PLAN_BLOCKED:
                step["status"] = "SKIPPED"
                step["skipped_reason"] = "plan blocked: " + step.get("error", "")[:400]
            X.set_pilot(run, "WAITING", "plan blocked: " + step.get("error", "")[:300], step=step["index"])
            X.save_run(run)
            _sleep(WAIT_AFTER_FAILURE_S if step["status"] == "PENDING" else 0)
            continue
        if out == "RUN_FAILED":
            failed += 1
            if failed >= MAX_FAILED_STARTS:
                finish(run, "HALTED", f"{failed} batches in a row failed to start: {step.get('error', '')[:300]}")
                return 2
            X.set_pilot(run, "WAITING", f"batch failed to start ({failed}/{MAX_FAILED_STARTS}): {step.get('error', '')[:300]}")
            X.save_run(run)
            _sleep(WAIT_AFTER_FAILURE_S)
            continue
        if out == "STOPPED":
            break
        failed = 0
        empty = empty + 1 if not step.get("accepted") else 0
        if empty >= MAX_EMPTY_BATCHES:
            finish(run, "HALTED", f"{empty} batches in a row with no accepted point (SDR mute? RFI? mount?) - look before resuming")
            return 2
        X.set_pilot(run, "RUNNING", f"batch {step['index'] + 1} done: {step.get('accepted', 0)} point(s) accepted")
        X.save_run(run)
    run = X.load_run(rid)
    finish(run, "STOPPED", "stopped by the operator - RESUME continues where it stopped")
    log("experiment stopped")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("recipes")
    s = sub.add_parser("start")
    s.add_argument("recipe_id")
    s.add_argument("--open-sky", action="store_true", help="the antenna is under open sky (required)")
    s.add_argument("--note", default="")
    rs = sub.add_parser("resume")
    rs.add_argument("run_id")
    sub.add_parser("run")
    sub.add_parser("list")
    a = sub.add_parser("analyse")
    a.add_argument("run_id")
    rp = sub.add_parser("replay")
    rp.add_argument("recipe_id")
    rp.add_argument("--survey", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "recipes":
        loc = X.site_location()
        for r in X.load_recipes():
            rd = X.readiness(r, loc)
            print(f"{r['id']:10s} {rd['state']:9s} {rd['duration_s'] / 60:5.0f} min  "
                  f"{rd.get('start_utc', rd.get('reason', ''))}  {r['name']}")
        return 0
    if args.cmd == "start":
        print(json.dumps(X.start_run(args.recipe_id, open_sky_confirmed=args.open_sky, note=args.note), indent=2))
        return 0
    if args.cmd == "resume":
        print(json.dumps(X.resume_run(args.run_id), indent=2))
        return 0
    if args.cmd == "run":
        return runner()
    if args.cmd == "list":
        for r in X.list_runs():
            v = r.get("verdict") or {}
            print(f"{r['run_id']:34s} {r['status']:8s} {r['steps_done']}/{r['steps']}  {v.get('code') or '-'}  {v.get('title') or ''}")
        return 0
    if args.cmd == "analyse":
        run = X.load_run(args.run_id)
        run["analysis"] = X.analyse(run)
        X.save_run(run)
        print(json.dumps(run["analysis"]["verdict"], indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "replay":
        import survey
        rc = X.get_recipe(args.recipe_id)
        run = X.replay_from_survey(rc, survey.load(args.survey))
        if not run["steps"]:
            print("no batch of that survey matches this recipe's modes")
            return 1
        run["analysis"] = X.analyse(run)
        X.save_run(run)
        print(run["run_id"])
        print(json.dumps(run["analysis"]["verdict"], indent=2, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
