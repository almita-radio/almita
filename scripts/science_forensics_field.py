#!/usr/bin/env python3
"""ALMITA SCIENCE FORENSICS FIELD EXECUTION PACK (operator wrapper; orchestrates existing tools only).

  status     read-only: git, plan, window, services, files, conflicts, frozen-module check, effective field status
  precheck   read-only: CSV/plan/disk/venv/endpoint checks (no GOTO, no hardware command)
  preflight  runs the REAL `capture.py --preflight-only` (never reimplemented); persists the report; PASS or BLOCKED
  run        explicit A|B|AB; --dry-run prints the exact command; a live run needs a fresh PASS preflight, the planned geometry
             window, and an explicit --yes; it calls the existing capture.py on a per-campaign COPY of the official CSV
  postcheck  read-only: counts, files, HDF5 validity, timestamps, order, gaps, metadata; COMPLETE / PARTIAL / FAIL
  analyze    REDUCE Level 1 -> the existing experiment/forensics machinery, with actual capture timestamps

Nothing here changes capture.py, REDUCE, SCIENCE, forensics V1, systemd, rtl_tcp, gain, frequency or observer_config.json.
A successful preflight NEVER starts an observation. Stop a run with a single Ctrl+C (SIGINT to capture.py); never kill -9.
Field status vocabulary: BLOCKED | READY FOR LIVE PREFLIGHT | READY FOR FIELD (only after a real, fresh, matching PASS preflight).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_PLAN = "examples/science_forensics_experiment.yaml"
FIELD_ROOT = "data/science_forensics_field"
MOSAIC_ROOT = "data/mosaic"
REDUCED_ROOT = "data/reduced"
EXPERIMENTS = ("A", "B", "AB")
PREFLIGHT_MAX_AGE_MIN = 120.0
CAPTURE_REQUIRED_FIELDS = {"point_number", "scan_order", "target_ra_hours", "target_dec_degrees", "capture_status", "start_time", "end_time",
                           "duration", "error_message", "data_filename", "session_name"}
# frozen surface -> (paths, pinned commit): any difference to the pin or to HEAD is reported
FROZEN = (("reduce_engine", "2afc4c5"), ("almita_reduce.py", "2afc4c5"), ("science_engine", "f5e2f65"), ("almita_science.py", "f5e2f65"),
          ("science_forensics", "02b8e5a"), ("almita_science_forensics.py", "8f4b40a"), ("capture.py", "8f4b40a"),
          ("observation_orchestrator.py", "8f4b40a"), ("observation_spec.py", "8f4b40a"))
DEFAULT_WINDOW = (150857.0, 5607.0)          # candidate LSRK window (m/s) of the feature campaign
MIN_ALT_DEG, HA_MARGIN_H = 30.0, 0.25        # same values the plan/`windows` use
ENDPOINTS = {"MAIN rtl_tcp": 1234, "INDI": 7624}
SERVICES = ("rtl_tcp.service",)
STAMP = "%Y%m%dT%H%M%SZ"


# ------------------------------------------------------------------ context (everything external is injectable, so tests never touch hardware)

def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _real_run(argv, timeout=120, cwd=None) -> dict:
    if os.environ.get("PYTEST_CURRENT_TEST") and argv[0] != "git" and any(Path(a).name == "capture.py" for a in argv) and "--help" not in argv:
        raise RuntimeError("safety net: automated tests must never execute capture.py (inject a fake runner)")
    started = datetime.now(timezone.utc)
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, cwd=cwd)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out, err = 124, (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""), f"TIMEOUT after {timeout}s"
    except FileNotFoundError as exc:
        rc, out, err = 127, "", f"{exc}"
    return {"argv": list(argv), "returncode": rc, "stdout": out, "stderr": err, "started_utc": _utc(started), "ended_utc": _utc(datetime.now(timezone.utc))}


def _real_listeners() -> set:
    """Passive: TCP ports in LISTEN state (never connects, so it cannot disturb rtl_tcp or INDI)."""
    ports = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in Path(name).read_text().splitlines()[1:]:
                f = line.split()
                if len(f) > 3 and f[3] == "0A":
                    ports.add(int(f[1].rsplit(":", 1)[1], 16))
        except OSError:
            pass
    return ports


def _real_service_active(unit: str) -> str:
    try:
        p = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
        return p.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _real_capture_procs() -> list:
    """Running capture.py processes (read-only /proc scan)."""
    out = []
    for d in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(Path(c.decode(errors="replace")).name == "capture.py" for c in cmd[:4]) and int(d.name) != os.getpid():
            out.append({"pid": int(d.name), "cmdline": b" ".join(cmd).decode(errors="replace")[:300]})
    return out


class Ctx:
    def __init__(self, root=REPO, plan=DEFAULT_PLAN, now=None, run=None, listeners=None, service_active=None, capture_procs=None,
                 spawn=None, disk_free=None):
        self.root = Path(root)
        self.plan_path = Path(plan) if Path(plan).is_absolute() else self.root / plan
        self._now = now
        self.run = run or _real_run
        self.listeners = listeners or _real_listeners
        self.service_active = service_active or _real_service_active
        self.capture_procs = capture_procs or _real_capture_procs
        self.spawn = spawn or _real_spawn
        self.disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)
        self.real_run = run is None

    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def p(self, *parts) -> Path:
        return self.root.joinpath(*parts)

    def git(self, *args) -> str:
        r = self.run(["git", *args], timeout=60, cwd=str(self.root))
        return r["stdout"].strip() if r["returncode"] == 0 else ""


# ------------------------------------------------------------------ plan / experiment

class FieldError(Exception):
    pass


def load_plan(ctx: Ctx) -> dict:
    import yaml
    if not ctx.plan_path.is_file():
        raise FieldError(f"plan not found: {ctx.plan_path}")
    plan = yaml.safe_load(ctx.plan_path.read_text())
    if plan.get("schema") != "science_forensics_experiment":
        raise FieldError(f"not a science_forensics_experiment plan: {ctx.plan_path}")
    return plan


def experiment_info(ctx: Ctx, plan: dict, exp: str) -> dict:
    if exp not in EXPERIMENTS:
        raise FieldError(f"experiment must be one of {EXPERIMENTS}, got {exp!r} (no default experiment)")
    e = plan["experiments"][exp]
    csv_path = ctx.p(e["csv"]["path"])
    return {"experiment": exp, "csv": csv_path, "csv_sha256_plan": e["csv"]["sha256"], "n_captures": e["n_captures"],
            "sequence": e.get("sequence"), "duration_min": e["duration_min"], "name": e["name"]}


def expected_sequence(plan: dict, exp: str) -> list:
    """The label sequence of an experiment. AB has no explicit list in the plan: it is A x20 + B + A x20 (plan sequence_note)."""
    e = plan["experiments"][exp]
    if e.get("sequence"):
        return list(e["sequence"])
    if exp == "AB":
        b, half = plan["experiments"]["B"]["sequence"], (e["n_captures"] - plan["experiments"]["B"]["n_captures"]) // 2
        return ["A"] * half + list(b) + ["A"] * half
    raise FieldError(f"plan has no sequence for {exp}")


def git_state(ctx: Ctx) -> dict:
    return {"commit": ctx.git("rev-parse", "HEAD") or "UNKNOWN", "branch": ctx.git("rev-parse", "--abbrev-ref", "HEAD") or "UNKNOWN"}


def frozen_check(ctx: Ctx) -> dict:
    """Differences of the frozen surface against HEAD (working tree included) and against its pinned commit."""
    diffs = {}
    unknown = False
    for path, pin in FROZEN:
        names = set()
        for ref in ("HEAD", pin):
            r = ctx.run(["git", "diff", "--name-only", ref, "--", path], timeout=60, cwd=str(ctx.root))
            if r["returncode"] != 0:
                unknown = True
                continue
            names |= {n for n in r["stdout"].split("\n") if n}
        if names:
            diffs[path] = sorted(names)
    return {"ok": not diffs and not unknown, "differs": diffs, "git_unavailable": unknown}


# ------------------------------------------------------------------ geometry window and epoch

def _site(ctx: Ctx) -> dict:
    return json.loads(ctx.p("observer_config.json").read_text())["observer"]


def _positions(plan: dict, exp: str) -> dict:
    from science_forensics.experiment.design import Position
    labels = ("A",) if exp == "A" else ("A", "B", "C")
    return {l: Position(l, plan["positions"][l]["ra_deg"], plan["positions"][l]["dec_deg"]) for l in labels}


def check_window(ctx: Ctx, plan: dict, exp: str, now: datetime | None = None, list_windows: bool = False) -> dict:
    """Is the planned geometry valid for the WHOLE run starting now? (all positions above the minimum altitude and on ONE hour-angle
    side: capture.py re-blocks by HA sign at its own start and would silently reorder an interleaved plan.) Never changes positions."""
    import numpy as np
    from science_forensics.experiment import design as D
    now = now or ctx.now()
    site, pos = _site(ctx), _positions(plan, exp)
    dur_s = (plan["experiments"][exp]["duration_min"]["p90"] + 5.0) * 60.0
    t = np.linspace(now.timestamp(), now.timestamp() + dur_s, 9)
    best = None
    for side in ("negative", "positive"):
        v = D.visibility_ok(pos, site, t, min_alt_deg=MIN_ALT_DEG, ha_side=side, ha_margin_h=HA_MARGIN_H)
        if v["ok"]:
            best = v
            break
        best = best or v
    ok = bool(best["ok"])
    out = {"status": "OK" if ok else "BLOCKED FOR PLANNED GEOMETRY", "checked_from_utc": _utc(now), "checked_minutes": dur_s / 60.0,
           "min_altitude_deg": best["min_altitude_deg"], "hour_angle_range_h": best["hour_angle_range_h"], "ha_side": best["side"] if ok else None,
           "required": {"min_altitude_deg": MIN_ALT_DEG, "ha_margin_h": HA_MARGIN_H, "single_ha_side": True}}
    if not ok:
        out["reason"] = (f"altitude/HA constraint violated for a {dur_s / 60:.0f} min run starting {_utc(now)}: min altitude {best['min_altitude_deg']:.1f} deg, "
                         f"HA ranges {best['hour_angle_range_h']}; positions are NOT changed automatically")
    if list_windows:
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        out["next_windows"] = D.find_start_windows(pos, site, day, 2, dur_s, min_alt_deg=MIN_ALT_DEG, ha_margin_h=HA_MARGIN_H, step_min=30)
    return out


def check_epoch(ctx: Ctx, plan: dict, exp: str, now: datetime | None = None) -> dict:
    """Have the sky-shift contrasts of the plan drifted (season)? Never mutates the plan: offers a command that writes a NEW plan."""
    from science_forensics.experiment import design as D
    now = now or ctx.now()
    pos = _positions(plan, exp)
    plan_epoch = datetime.fromisoformat(plan["design_epoch_utc"])
    tag = now.strftime("%Y%m%d")
    cmd = (f"./.venv/bin/python almita_science_forensics_experiment.py design --epoch {now.strftime('%Y-%m-%dT%H:%M:00+00:00')} "
           f"--out data/experiments/EXP-{tag}/plan.yaml --emit-dir data/experiments/EXP-{tag} --copy-observer-config")
    out = {"plan_epoch_utc": plan["design_epoch_utc"], "days_from_plan_epoch": (now - plan_epoch).total_seconds() / 86400.0,
           "regenerate_command": cmd, "status": "OK"}
    if exp == "A":
        return out
    exp_now = D.expected_signatures(pos, _site(ctx), now.timestamp(), 3600.0)
    rows, worst = {}, 1.0
    for k, v in plan["predicted_lsrk_shift"]["pairwise"].items():
        if k not in exp_now["pairwise"]:
            continue
        now_ch, plan_ch = exp_now["pairwise"][k]["delta_lsrk_shift_channels"], v["delta_lsrk_shift_channels"]
        rows[k] = {"plan_channels": plan_ch, "now_channels": now_ch}
        worst = min(worst, abs(now_ch) / max(abs(plan_ch), 1e-9))
    out["pairwise_shift_channels"] = rows
    small = [k for k, v in rows.items() if abs(v["now_channels"]) < 10.0]
    if small:
        out["status"] = "BLOCKED FOR PLANNED GEOMETRY"
        out["reason"] = f"sky-shift contrast of {small} is < 10 channels at the run epoch: the design no longer separates sky from receiver; regenerate a NEW plan"
    elif worst < 0.75:
        out["status"] = "REGENERATE_RECOMMENDED"
    return out


# ------------------------------------------------------------------ precheck

def _read_rows(path: Path):
    with open(path, newline="") as fh:
        r = csv.DictReader(fh)
        return list(r.fieldnames or []), list(r)


def csv_checks(ctx: Ctx, plan: dict, exp: str) -> list:
    """Every check on the official CSV of the experiment: hash, schema, identity, order, repeated coordinates, counts."""
    from science_forensics.experiment.plan import validate_capture_csv
    info = experiment_info(ctx, plan, exp)
    checks = []

    def add(name, ok, detail, level="FAIL"):
        checks.append({"name": name, "status": "PASS" if ok else level, "detail": detail})

    path = info["csv"]
    if not path.is_file():
        add("CSV exists", False, f"missing {path}")
        return checks
    add("CSV exists", True, str(path.relative_to(ctx.root)) if path.is_relative_to(ctx.root) else str(path))
    sha = sha256_file(path)
    add("CSV sha256 equals the plan", sha == info["csv_sha256_plan"], f"file {sha[:16]}… plan {info['csv_sha256_plan'][:16]}…")
    try:
        fields, rows = _read_rows(path)
    except Exception as exc:
        add("CSV parses", False, f"{type(exc).__name__}: {exc}")
        return checks
    missing = sorted(CAPTURE_REQUIRED_FIELDS - set(fields))
    add("required capture.py columns", not missing, f"missing {missing}" if missing else "all present")
    add("experiment_label column", "experiment_label" in fields, "labels A/B/C per capture")
    try:
        v = validate_capture_csv(path)
    except Exception as exc:            # e.g. a required column is missing: the validator cannot even read the row
        v = {"ok": False, "problems": [f"validator could not read the CSV: {type(exc).__name__}: {exc}"], "n_rows": len(rows)}
    add("capture.py plan rules (range, status, unique id/file/order)", v["ok"], "; ".join(v["problems"][:5]) if v["problems"] else f"{v['n_rows']} rows")
    add("no duplicate capture IDs", len({r.get("point_number") for r in rows}) == len(rows) and len({r.get("data_filename") for r in rows}) == len(rows),
        f"{len(rows)} rows, {len({r.get('point_number') for r in rows})} distinct point_number")
    add("sequence length", len(rows) == info["n_captures"], f"{len(rows)} rows, plan {info['n_captures']}")
    labels = [r.get("experiment_label") for r in rows]
    exp_seq = expected_sequence(plan, exp)
    add("label sequence equals the plan", labels == exp_seq, f"A/B/C counts {({l: labels.count(l) for l in sorted(set(labels))})}")
    add("expected A/B/C counts", {l: labels.count(l) for l in set(exp_seq)} == {l: exp_seq.count(l) for l in set(exp_seq)}, f"plan {({l: exp_seq.count(l) for l in sorted(set(exp_seq))})}")
    order_ok = [int(r["scan_order"]) for r in rows] == list(range(1, len(rows) + 1)) if rows and all(str(r.get("scan_order", "")).isdigit() for r in rows) else False
    add("scan_order is 1..N in plan order", order_ok, "contiguous" if order_ok else "not contiguous / not numeric")
    # repeated sky coordinates preserved: rows with the same label share identical coordinates; distinct labels have distinct ones
    by_label = {}
    for r in rows:
        by_label.setdefault(r.get("experiment_label"), set()).add((r.get("target_ra_hours"), r.get("target_dec_degrees")))
    rep_ok = all(len(s) == 1 for s in by_label.values()) and len({next(iter(s)) for s in by_label.values() if s}) == len(by_label)
    n_rep = sum(labels.count(l) - 1 for l in set(labels))
    add("repeated sky coordinates preserved", rep_ok and n_rep > 0, f"{len(by_label)} distinct coordinates, {n_rep} repeated visits")
    pos_ok = True
    for l, s in by_label.items():
        if l in plan["positions"] and s:
            ra_h, dec = map(float, next(iter(s)))
            pos_ok &= abs(ra_h - plan["positions"][l]["ra_hours"]) < 1e-5 and abs(dec - plan["positions"][l]["dec_deg"]) < 1e-5
    add("CSV coordinates equal plan positions", pos_ok, "A/B/C match plan")
    add("all rows planned", all(r.get("capture_status") == "planned" for r in rows), "capture_status=planned for every row")
    return checks


def precheck(ctx: Ctx, plan: dict, exp: str, *, live_endpoints: bool = True) -> dict:
    """No GOTO and no hardware command: files, schema, disk, interpreter, passive endpoint check."""
    info = experiment_info(ctx, plan, exp)
    checks = csv_checks(ctx, plan, exp)

    def add(name, ok, detail, level="FAIL"):
        checks.append({"name": name, "status": "PASS" if ok else level, "detail": detail})

    import importlib.util
    miss = [m for m in ("numpy", "h5py", "astropy", "yaml") if importlib.util.find_spec(m) is None]
    add("venv/interpreter usable", not miss and sys.version_info >= (3, 10), f"{sys.executable}; missing modules {miss}" if miss else f"{sys.executable}")
    venv = ctx.p(".venv")
    if venv.is_dir() and not str(Path(sys.executable)).startswith(str(venv)) and sys.prefix == sys.base_prefix:
        add("running inside the project .venv", False, "run with ./.venv/bin/python", level="WARN")
    for f in ("capture.py", "observer_config.json", "almita_reduce.py"):
        add(f"required file {f}", ctx.p(f).is_file(), str(ctx.p(f)))
    est = int(info["n_captures"] * plan["instrument_config_identical_to_feature_campaign"]["capture_seconds"] *
              plan["instrument_config_identical_to_feature_campaign"]["sdr_sample_rate_hz"] * 2 * 1.25)
    data_dir = ctx.p("data")
    probe = data_dir if data_dir.exists() else ctx.root
    try:
        free = int(ctx.disk_free(probe))
        add("disk space", free >= est * 2, f"free {free / 1e9:.1f} GB, capture needs ~{est / 1e9:.2f} GB (x2 margin for REDUCE outputs not included)")
    except Exception as exc:
        add("disk space", False, f"{type(exc).__name__}: {exc}")
    for name in (MOSAIC_ROOT, FIELD_ROOT):
        anc = ctx.p(name)
        while not anc.exists() and anc != anc.parent:
            anc = anc.parent
        add(f"output destination writable ({name})", os.access(anc, os.W_OK), f"nearest existing directory {anc}")
    if live_endpoints:
        ports = ctx.listeners()
        for name, port in ENDPOINTS.items():
            add(f"{name} endpoint listening (127.0.0.1:{port}, passive)", port in ports, "listening" if port in ports else "not listening: real preflight would fail")
        for unit in SERVICES:
            st = ctx.service_active(unit)
            add(f"service {unit}", st == "active", st, level="WARN")
    else:
        checks.append({"name": "live endpoints", "status": "SKIPPED", "detail": "--offline"})
    failed = [c for c in checks if c["status"] == "FAIL"]
    return {"experiment": exp, "status": "PASS" if not failed else "BLOCKED", "checks": checks, "failed": [c["name"] for c in failed]}


# ------------------------------------------------------------------ campaign id, derived CSV, commands

def campaign_id(exp: str, now: datetime) -> str:
    return f"FORENSICS-{exp}-{now.astimezone(timezone.utc).strftime(STAMP)}"


def derive_run_csv(src: Path, dest: Path, session_name: str) -> dict:
    """Copy of the official CSV that differs ONLY in session_name and the session prefix of data_filename (capture.py names its
    sessions/files from them and rewrites the CSV in place, so the official file must never be the one it runs on)."""
    fields, rows = _read_rows(src)
    old_session = rows[0]["session_name"]
    out_rows = []
    for r in rows:
        r2 = dict(r)
        r2["session_name"] = session_name
        r2["data_filename"] = r["data_filename"].replace(old_session, session_name, 1) if r["data_filename"].startswith(old_session) else f"{session_name}_{int(r['point_number']):04d}.dat"
        out_rows.append(r2)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)
    changed = {k for r, r2 in zip(rows, out_rows) for k in r if r[k] != r2[k]}
    return {"path": str(dest), "sha256": sha256_file(dest), "rows": len(out_rows), "columns_changed": sorted(changed), "session_name": session_name}


def capture_argv(plan: dict, csv_path: Path, *, preflight: bool, python=None) -> list:
    """The exact capture.py command: the arguments the orchestrator passed for the feature campaign (audited in the plan)."""
    from science_forensics.experiment.plan import capture_command
    inst = plan["instrument_config_identical_to_feature_campaign"]
    argv = capture_command(str(csv_path), inst)
    argv[0] = python or sys.executable
    if preflight:
        argv += ["--preflight-only", "--debug"]        # --debug: compact console hides the per-check lines that explain a block
    return argv


def _atomic_json(path: Path, obj) -> None:
    from science_forensics.models import sanitize
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sanitize(obj), indent=2, sort_keys=True))
    os.replace(tmp, path)


def _hashes(ctx: Ctx, plan: dict, exp: str) -> dict:
    info = experiment_info(ctx, plan, exp)
    return {"plan_sha256": sha256_file(ctx.plan_path), "csv_source_sha256": sha256_file(info["csv"]) if info["csv"].is_file() else None,
            "observer_config_sha256": sha256_file(ctx.p("observer_config.json")) if ctx.p("observer_config.json").is_file() else None}


# ------------------------------------------------------------------ preflight

def parse_preflight(result: dict) -> dict:
    text = (result["stdout"] or "") + "\n" + (result["stderr"] or "")
    fails = [l.strip() for l in text.splitlines() if re.search(r"\[FAIL\]", l)]
    warns = [l.strip() for l in text.splitlines() if re.search(r"\[WARN\]", l)]
    errors = [l.strip() for l in text.splitlines() if re.search(r"\bERROR\b", l)]
    passed_marker = bool(re.search(r"PREFLIGHT[:\s]+PASS", text))
    failed_marker = bool(re.search(r"PREFLIGHT[:\s]+FAIL", text))
    reasons = []
    if result["returncode"] != 0:
        reasons.append(f"capture.py exit code {result['returncode']}")
    if failed_marker:
        reasons.append("capture.py reported PREFLIGHT FAIL")
    if not passed_marker and not failed_marker:
        reasons.append("no PREFLIGHT PASS marker in capture.py output")
    reasons += fails + errors[:5]
    status = "PASS" if (result["returncode"] == 0 and passed_marker and not fails and not failed_marker) else "BLOCKED"
    return {"status": status, "reasons": [] if status == "PASS" else reasons, "warnings": warns}


def do_preflight(ctx: Ctx, plan: dict, exp: str, *, print_only: bool = False, timeout: float = 180.0) -> dict:
    info = experiment_info(ctx, plan, exp)
    now = ctx.now()
    d = ctx.p(FIELD_ROOT, "_preflight", exp, now.strftime(STAMP))
    argv_preview = capture_argv(plan, d / "mosaic.csv", preflight=True)
    if print_only:
        return {"printed_only": True, "argv": argv_preview}
    pc = precheck(ctx, plan, exp, live_endpoints=False)
    if pc["status"] != "PASS":
        return {"status": "BLOCKED", "reasons": [f"precheck failed: {pc['failed']}"], "argv": argv_preview}
    d.mkdir(parents=True, exist_ok=True)
    derive_run_csv(info["csv"], d / "mosaic.csv", f"FORENSICS-{exp}-PREFLIGHT")
    shutil.copyfile(ctx.p("observer_config.json"), d / "observer_config.json")      # capture.py requires it next to the CSV (a copy; never modified)
    res = ctx.run(argv_preview, timeout=timeout, cwd=str(ctx.root))
    (d / "stdout.txt").write_text(res["stdout"] or "")
    (d / "stderr.txt").write_text(res["stderr"] or "")
    verdict = parse_preflight(res)
    git = git_state(ctx)
    report = {"kind": "live_capture_preflight", "experiment": exp, "status": verdict["status"], "reasons": verdict["reasons"], "warnings": verdict["warnings"],
              "argv": res["argv"], "returncode": res["returncode"], "started_utc": res["started_utc"], "ended_utc": res["ended_utc"],
              "git_commit": git["commit"], "branch": git["branch"], "csv_prepared": str(d / "mosaic.csv"), "csv_prepared_sha256": sha256_file(d / "mosaic.csv"),
              **_hashes(ctx, plan, exp), "stdout_file": str(d / "stdout.txt"), "stderr_file": str(d / "stderr.txt"),
              "note": "capture.py --preflight-only: no GOTO, no tracking, no IQ capture. A PASS does not start an observation."}
    _atomic_json(d / "preflight.json", report)
    _atomic_json(ctx.p(FIELD_ROOT, f"latest_preflight_{exp}.json"), {**report, "report_path": str(d / "preflight.json")})
    return report


def gate_preflight(ctx: Ctx, plan: dict, exp: str, now: datetime | None = None, max_age_min: float = PREFLIGHT_MAX_AGE_MIN) -> dict:
    """A run needs the latest LIVE preflight of THIS experiment to be PASS, fresh, and matching plan/CSV/observer_config/commit."""
    now = now or ctx.now()
    p = ctx.p(FIELD_ROOT, f"latest_preflight_{exp}.json")
    if not p.is_file():
        return {"ok": False, "reason": f"no live preflight for {exp}: run `preflight {exp}` first"}
    rep = json.loads(p.read_text())
    cur, git = _hashes(ctx, plan, exp), git_state(ctx)
    if rep.get("status") != "PASS":
        return {"ok": False, "reason": f"latest preflight is {rep.get('status')}: {rep.get('reasons')}", "report": rep}
    age = (now - datetime.fromisoformat(rep["ended_utc"])).total_seconds() / 60.0
    if age > max_age_min or age < -1:
        return {"ok": False, "reason": f"latest PASS preflight is {age:.0f} min old (limit {max_age_min:.0f}); repeat it", "report": rep}
    for k in ("plan_sha256", "csv_source_sha256", "observer_config_sha256"):
        if rep.get(k) != cur[k]:
            return {"ok": False, "reason": f"{k} changed since the preflight", "report": rep}
    if rep.get("git_commit") != git["commit"]:
        return {"ok": False, "reason": "git commit changed since the preflight", "report": rep}
    return {"ok": True, "age_min": age, "report": rep}


def field_status(ctx: Ctx, plan: dict, exp: str, window: dict, epoch: dict, pre: dict, frozen: dict, gate: dict) -> str:
    if pre["status"] != "PASS" or window["status"] != "OK" or epoch["status"].startswith("BLOCKED") or not frozen["ok"]:
        return "BLOCKED"
    return "READY FOR FIELD" if gate["ok"] else "READY FOR LIVE PREFLIGHT"


# ------------------------------------------------------------------ run

def _real_spawn(argv, cwd, stdout_path, stderr_path, log=print):
    """Run capture.py in its own session (so a terminal Ctrl+C reaches only this wrapper) and forward ONE SIGINT to it; tee its output to
    files and the terminal. The wrapper never sends SIGTERM/SIGKILL."""
    out_f, err_f = open(stdout_path, "w"), open(stderr_path, "w")
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
    state = {"interrupted_at": None}

    def tee(src, dst, term):
        for line in src:
            dst.write(line); dst.flush()
            term.write(line); term.flush()

    threads = [threading.Thread(target=tee, args=(proc.stdout, out_f, sys.stdout), daemon=True),
               threading.Thread(target=tee, args=(proc.stderr, err_f, sys.stderr), daemon=True)]
    for t in threads:
        t.start()

    def on_sigint(signum, frame):
        if state["interrupted_at"] is None:
            state["interrupted_at"] = _utc(datetime.now(timezone.utc))
            log("\n[field] SIGINT forwarded ONCE to capture.py; wait for it to close its files (never kill -9).")
            try:
                os.kill(proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        else:
            log("[field] already interrupting: waiting for capture.py to finish.")

    old = signal.signal(signal.SIGINT, on_sigint)
    try:
        rc = proc.wait()
    finally:
        signal.signal(signal.SIGINT, old)
    for t in threads:
        t.join(timeout=5)
    out_f.close(); err_f.close()
    return {"returncode": rc, "interrupted_at_utc": state["interrupted_at"]}


def run_experiment(ctx: Ctx, plan: dict, exp: str, *, dry_run: bool, yes: bool, allow_frozen_diff: bool = False, confirm_input=input,
                   log=print) -> dict:
    info = experiment_info(ctx, plan, exp)
    now = ctx.now()
    cid = campaign_id(exp, now)
    field_dir, cap_dir = ctx.p(FIELD_ROOT, cid), ctx.p(MOSAIC_ROOT, cid)
    run_csv = cap_dir / "mosaic.csv"
    argv = capture_argv(plan, run_csv, preflight=False)
    pc = precheck(ctx, plan, exp, live_endpoints=True)
    window = check_window(ctx, plan, exp, now)
    epoch = check_epoch(ctx, plan, exp, now)
    frozen = frozen_check(ctx)
    gate = gate_preflight(ctx, plan, exp, now)
    procs = ctx.capture_procs()
    blockers = []
    if pc["status"] != "PASS":
        blockers.append(f"precheck: {pc['failed']}")
    if window["status"] != "OK":
        blockers.append(f"{window['status']}: {window.get('reason', '')}")
    if epoch["status"].startswith("BLOCKED"):
        blockers.append(f"{epoch['status']}: {epoch.get('reason', '')} -> {epoch['regenerate_command']}")
    if not frozen["ok"] and not allow_frozen_diff:
        blockers.append(f"frozen modules differ: {frozen['differs'] or 'git unavailable'}")
    if not gate["ok"]:
        blockers.append(f"preflight: {gate['reason']}")
    if procs:
        blockers.append(f"another capture.py is running: {[p['pid'] for p in procs]}")
    if field_dir.exists() or cap_dir.exists():
        blockers.append(f"campaign {cid} already exists")
    summary = {"campaign_id": cid, "experiment": exp, "argv": argv, "n_captures": info["n_captures"],
               "sequence_counts": {l: expected_sequence(plan, exp).count(l) for l in sorted(set(expected_sequence(plan, exp)))},
               "sequence_head": expected_sequence(plan, exp)[:12], "expected_duration_min": info["duration_min"],
               "capture_dir": str(cap_dir), "field_dir": str(field_dir), "blockers": blockers, "epoch_status": epoch["status"]}
    if dry_run:
        summary["status"] = "DRY_RUN_WOULD_BLOCK" if blockers else "DRY_RUN_OK"
        return summary
    if blockers:
        summary["status"] = "BLOCKED"
        return summary
    if not yes:
        ans = confirm_input(f"Type the experiment name ({exp}) to start a LIVE run that moves the mount: ")
        if ans.strip() != exp:
            summary["status"] = "NOT_STARTED"
            summary["blockers"] = ["operator did not confirm"]
            return summary
    # provenance: immutable copies of the exact inputs
    inputs = field_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    for src, name in ((ctx.plan_path, "plan.yaml"), (info["csv"], "source_mosaic.csv"), (ctx.p("observer_config.json"), "observer_config.json")):
        shutil.copyfile(src, inputs / name)
        os.chmod(inputs / name, 0o444)
    cap_dir.mkdir(parents=True, exist_ok=False)
    derived = derive_run_csv(info["csv"], run_csv, cid)
    shutil.copyfile(ctx.p("observer_config.json"), cap_dir / "observer_config.json")
    (field_dir / "logs").mkdir()
    git, hashes = git_state(ctx), _hashes(ctx, plan, exp)
    start = ctx.now()
    manifest = {"campaign_id": cid, "experiment": exp, "plan": str(ctx.plan_path), "plan_sha256": hashes["plan_sha256"], "csv": info["csv"].as_posix(),
                "csv_sha256": hashes["csv_source_sha256"], "csv_run": derived["path"], "csv_run_sha256_initial": derived["sha256"],
                "csv_run_columns_changed_vs_official": derived["columns_changed"], "observer_config_sha256": hashes["observer_config_sha256"],
                "git_commit": git["commit"], "branch": git["branch"], "operator_command": " ".join(sys.argv) if sys.argv else "", "capture_argv": argv,
                "start_utc": _utc(start), "end_utc": None, "capture_exit": None, "status": "RUNNING",
                "preflight_result": gate["report"]["status"], "preflight_report": gate["report"].get("report_path"), "preflight_age_min": gate["age_min"],
                "capture_count_expected": info["n_captures"], "capture_count_actual": None, "analysis_session": None,
                "frozen_differs_allowed": bool(allow_frozen_diff and not frozen["ok"]), "capture_dir": str(cap_dir), "plan_immutable_copies": str(inputs)}
    _atomic_json(field_dir / "manifest.json", manifest)
    (field_dir / "logs" / "operator_command.txt").write_text(manifest["operator_command"] + "\n" + " ".join(argv) + "\n")
    res = ctx.spawn(argv, str(ctx.root), field_dir / "logs" / "capture_stdout.log", field_dir / "logs" / "capture_stderr.log")
    end = ctx.now()
    manifest.update({"end_utc": _utc(end), "capture_exit": res["returncode"], "interrupted_at_utc": res.get("interrupted_at_utc"),
                     "capture_count_actual": count_successes(cap_dir)})
    manifest["status"] = "CAPTURE_EXIT_0" if res["returncode"] == 0 else ("INTERRUPTED" if res.get("interrupted_at_utc") or res["returncode"] in (130, -signal.SIGINT) else "CAPTURE_EXIT_NONZERO")
    _atomic_json(field_dir / "manifest.json", manifest)         # partial or failed campaigns are evidence: nothing is deleted
    summary.update({"status": manifest["status"], "capture_exit": res["returncode"], "next": f"postcheck {cid}"})
    return summary


# ------------------------------------------------------------------ postcheck

def count_successes(cap_dir: Path) -> int:
    """Rows marked success in the run CSV whose HDF5 file exists (cheap count for the manifest; postcheck does the real validation)."""
    p = cap_dir / "mosaic.csv"
    if not p.is_file():
        return 0
    stems = {f.stem for f in (cap_dir / "data" / "iq").rglob("*.h5")} if (cap_dir / "data" / "iq").is_dir() else set()
    return sum(1 for r in _read_rows(p)[1] if r.get("capture_status") == "success" and Path(r["data_filename"]).stem in stems)


def _h5_check(path: Path) -> dict:
    """Validity from METADATA only (attributes, dataset shape/dtype); the IQ samples are never read."""
    out = {"file": path.name, "bytes": path.stat().st_size}
    if out["bytes"] == 0:
        out["problem"] = "ZERO_BYTE"
        return out
    try:
        import h5py
        with h5py.File(path, "r") as h:
            out["has_iq_data"] = "iq_data" in h
            a = h.attrs
            out["attrs"] = {k: (a[k].item() if hasattr(a[k], "item") else a[k]) for k in (
                "capture_start_utc", "capture_status", "file_state", "point_number", "scan_order", "actual_capture_order", "center_frequency_hz",
                "sample_rate_hz", "gain", "bias_tee_enabled", "sdr_host", "sdr_port", "temperature_lna_mean_c", "temperature_sdr_mean_c",
                "onstep_state_post_goto", "onstep_state_post_tracking", "tracking_state_at_capture", "ha_block", "reclassified_due_to_ha_change",
                "duration_seconds", "instrument_topology") if k in a}
            if not out["has_iq_data"]:
                out["problem"] = "NO_IQ_DATASET"
            elif h["iq_data"].shape[0] == 0:
                out["problem"] = "EMPTY_IQ_DATASET"
            elif out["attrs"].get("file_state") not in (None, "complete"):
                out["problem"] = f"FILE_STATE_{out['attrs'].get('file_state')}"
    except Exception as exc:
        out["problem"] = f"INVALID_HDF5: {type(exc).__name__}"
    return out


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def postcheck(ctx: Ctx, cid: str, plan: dict | None = None) -> dict:
    """Read-only. COMPLETE / PARTIAL / FAIL; partial-but-valid data are PARTIAL, never FAIL."""
    field_dir, cap_dir = ctx.p(FIELD_ROOT, cid), ctx.p(MOSAIC_ROOT, cid)
    man_path = field_dir / "manifest.json"
    if not man_path.is_file():
        return {"campaign_id": cid, "verdict": "FAIL", "issues": [{"severity": "critical", "what": f"manifest missing: {man_path}"}]}
    man = json.loads(man_path.read_text())
    exp = man["experiment"]
    if plan is None:
        import yaml
        plan = yaml.safe_load((field_dir / "inputs" / "plan.yaml").read_text())       # the immutable copy taken at run start
    issues = []
    csv_path = cap_dir / "mosaic.csv"
    if not csv_path.is_file():
        return {"campaign_id": cid, "verdict": "FAIL", "issues": [{"severity": "critical", "what": "run mosaic.csv missing"}]}
    fields, rows = _read_rows(csv_path)
    exp_seq = expected_sequence(plan, exp)
    n_exp = len(exp_seq)
    h5s = sorted((cap_dir / "data" / "iq").rglob("*.h5")) if (cap_dir / "data" / "iq").is_dir() else []
    stems = [p.stem for p in h5s]
    dup = sorted({s for s in stems if stems.count(s) > 1})
    if dup:
        issues.append({"severity": "error", "what": f"duplicate capture files for stems {dup}"})
    by_stem = {}
    for p in h5s:
        by_stem.setdefault(p.stem, p)
    checks = {s: _h5_check(p) for s, p in by_stem.items()}
    zero = [s for s, c in checks.items() if c.get("problem") == "ZERO_BYTE"]
    invalid = [s for s, c in checks.items() if c.get("problem") and c["problem"] != "ZERO_BYTE"]
    if zero:
        issues.append({"severity": "error", "what": f"zero-byte files: {zero}"})
    if invalid:
        issues.append({"severity": "error", "what": f"invalid HDF5: { {s: checks[s]['problem'] for s in invalid} }"})
    valid_rows, missing = [], []
    for r in rows:
        stem = Path(r["data_filename"]).stem
        ok = r.get("capture_status") == "success" and stem in checks and "problem" not in checks[stem]
        (valid_rows if ok else missing).append(r)
    labels_valid = [r.get("experiment_label") for r in valid_rows]
    n_valid = len(valid_rows)
    if missing:
        issues.append({"severity": "warning", "what": f"{len(missing)} planned captures without a valid file", "points": [int(r["point_number"]) for r in missing][:60]})
    # timestamps and order
    def key(r):
        return int(r["actual_capture_order"]) if str(r.get("actual_capture_order", "")).strip().isdigit() else int(r["scan_order"])
    ordered = sorted(valid_rows, key=key)
    ts = [_parse_ts(r.get("start_time")) for r in ordered]
    ts_ok = all(t is not None for t in ts) and all(b > a for a, b in zip(ts, ts[1:]))
    if not ts_ok:
        issues.append({"severity": "error", "what": "capture start timestamps are missing or not strictly increasing in capture order"})
    order_ok = [r.get("experiment_label") for r in ordered] == [exp_seq[int(r["scan_order"]) - 1] for r in ordered] and \
        [int(r["scan_order"]) for r in ordered] == sorted(int(r["scan_order"]) for r in ordered)
    if not order_ok:
        issues.append({"severity": "critical", "what": "capture ORDER differs from the plan (capture.py re-blocked by hour angle?): interleaved design compromised"})
    re_cls = [int(r["point_number"]) for r in rows if str(r.get("reclassified_due_to_ha_change", "")).lower() == "true"]
    if re_cls:
        issues.append({"severity": "critical", "what": f"points reclassified by hour-angle change: {re_cls}"})
    gaps = [b - a for a, b in zip(ts, ts[1:]) if a and b]
    med = sorted(gaps)[len(gaps) // 2] if gaps else None
    big = [{"after_capture": int(ordered[i]["point_number"]), "gap_s": round(g, 1)} for i, g in enumerate(gaps) if med and g > max(3 * med, med + 90)]
    if big:
        issues.append({"severity": "warning", "what": f"large gaps (> max(3x median {med:.0f}s, median+90s)): {big[:10]}"})
    dur = (ts[-1] - ts[0]) / 60.0 if len(ts) > 1 and ts[0] and ts[-1] else None
    counts = {l: labels_valid.count(l) for l in sorted(set(exp_seq))}
    plan_counts = {l: exp_seq.count(l) for l in sorted(set(exp_seq))}
    inst = plan["instrument_config_identical_to_feature_campaign"]
    meta_issues, seen = [], {"center_frequency_hz": set(), "sample_rate_hz": set(), "gain": set(), "bias_tee_enabled": set(), "sdr_port": set()}
    for c in checks.values():
        for k in seen:
            if k in c.get("attrs", {}):
                seen[k].add(c["attrs"][k])
    exp_meta = {"center_frequency_hz": inst["sdr_center_frequency_hz"], "sample_rate_hz": inst["sdr_sample_rate_hz"], "gain": inst["sdr_gain_db"]}
    for k, v in exp_meta.items():
        if seen[k] and seen[k] != {v}:
            meta_issues.append(f"{k} {sorted(seen[k])} != plan {v}")
    if any(len(v) > 1 for v in seen.values()):
        meta_issues.append("instrument metadata changes inside the campaign: " + str({k: sorted(v) for k, v in seen.items() if len(v) > 1}))
    if meta_issues:
        issues.append({"severity": "error", "what": "metadata inconsistent: " + "; ".join(meta_issues)})
    metadata = field_metadata(checks, seen, man, missing_fields=True)
    if n_valid == 0:
        verdict = "FAIL"
    elif n_valid < n_exp or zero or invalid or dup:
        verdict = "PARTIAL"
    else:
        verdict = "COMPLETE"
    rep = {"campaign_id": cid, "experiment": exp, "verdict": verdict, "capture_count_expected": n_exp, "capture_count_actual": n_valid,
           "csv_rows": len(rows), "h5_files": len(h5s), "counts_valid": counts, "counts_plan": plan_counts,
           "repeated_A_present": counts.get("A", 0) >= 2, "timestamps_monotonic": ts_ok, "design_order_ok": order_ok,
           "campaign_duration_min": dur, "median_gap_s": med, "large_gaps": big, "issues": issues, "field_metadata": metadata,
           "manifest_status": man.get("status"), "capture_exit": man.get("capture_exit"), "read_only": True}
    rep["manifest_capture_count_actual_recorded"] = man.get("capture_count_actual")
    return rep


def field_metadata(checks: dict, seen: dict, man: dict, missing_fields=True) -> dict:
    """What the captures carry about the receiver/mount; anything absent is reported missing, never invented."""
    def gather(key):
        vals = [c["attrs"][key] for c in checks.values() if key in c.get("attrs", {}) and c["attrs"][key] == c["attrs"][key]]
        return vals
    md = {}
    for label, key in (("receiver_temperature_sdr_c", "temperature_sdr_mean_c"), ("lna_temperature_c", "temperature_lna_mean_c")):
        v = gather(key)
        md[label] = {"min": min(v), "max": max(v), "n": len(v)} if v else "MISSING"
    for k in ("gain", "center_frequency_hz", "sample_rate_hz", "bias_tee_enabled", "sdr_port"):
        md[k] = sorted(seen[k]) if seen.get(k) else "MISSING"
    md["sdr_serial"] = "MISSING (not recorded in the capture HDF5; MAIN rtl_tcp.service uses serial 00000001 per its unit file)"
    md["ambient_temperature"] = "MISSING (no sensor in the capture; operator logs it by hand, runbook section 2)"
    md["mount_state"] = sorted({c["attrs"].get("onstep_state_post_goto") for c in checks.values() if c.get("attrs", {}).get("onstep_state_post_goto")}) or "MISSING"
    md["tracking_state_at_capture"] = sorted({c["attrs"].get("tracking_state_at_capture") for c in checks.values() if c.get("attrs", {}).get("tracking_state_at_capture")}) or "MISSING"
    md["observer_config_sha256"] = man.get("observer_config_sha256") or "MISSING"
    return md


# ------------------------------------------------------------------ analyze (existing machinery, actual timestamps)

def find_reduce_session(ctx: Ctx, cid: str) -> Path | None:
    base = ctx.p(REDUCED_ROOT, cid)
    cands = sorted(base.glob("REDUCE-*/manifest.json")) if base.is_dir() else []
    ok = [c.parent for c in cands if json.loads(c.read_text()).get("status") == "COMPLETED"]
    return ok[-1] if ok else None


def reduce_command(ctx: Ctx, plan: dict, cid: str) -> list:
    inst = plan["instrument_config_identical_to_feature_campaign"]
    cmd = [sys.executable, "almita_reduce.py", "run", f"{MOSAIC_ROOT}/{cid}", "--output-root", REDUCED_ROOT, "--velocity-frame", "lsrk"]
    if inst.get("calibration_profile"):
        cmd += ["--calibration-profile", inst["calibration_profile"]]
    return cmd


def spur_position_stability(points) -> dict:
    """Geometry only: where REDUCE's KNOWN_SPUR/DC masks sit in each capture (channel ranges), never their invalid intensity."""
    import numpy as np
    from reduce_engine.models import MaskFlag
    bits = MaskFlag.KNOWN_SPUR.value | MaskFlag.DC.value
    per, allr = [], []
    for p in points:
        idx = np.flatnonzero((p.mask & bits) != 0)
        rr = []
        if idx.size:
            start = prev = int(idx[0])
            for v in idx[1:]:
                if int(v) != prev + 1:
                    rr.append((start, prev)); start = int(v)
                prev = int(v)
            rr.append((start, prev))
        per.append(rr)
        allr += rr
    if not allr:
        return {"n_ranges_tracked": 0, "note": "no KNOWN_SPUR/DC-masked channels in Level 1: nothing to track geometrically"}
    ref = sorted({r for rr in per[:1] for r in rr}) or sorted(set(allr))
    out = []
    for lo, hi in ref:
        c0 = 0.5 * (lo + hi)
        cs = []
        for rr in per:
            near = [0.5 * (a + b) for a, b in rr if abs(0.5 * (a + b) - c0) <= 50]
            if near:
                cs.append(min(near, key=lambda c: abs(c - c0)))
        out.append({"reference_range": [lo, hi], "captures_with_range": len(cs), "n_captures": len(per),
                    "centre_channel_span": float(max(cs) - min(cs)) if cs else None})
    return {"n_ranges_tracked": len(out), "ranges": out, "note": "position stability from mask geometry only; masked intensity is invalid and unused"}


def _classify(chi_per_dof):
    if chi_per_dof is None:
        return "UNRESOLVED"
    return "CONSISTENT WITH" if chi_per_dof < 4.0 else ("INCONSISTENT WITH" if chi_per_dof >= 9.0 else "UNRESOLVED")


def analyze_campaign(ctx: Ctx, plan: dict, cid: str, reduce_dir: Path, window=DEFAULT_WINDOW) -> dict:
    """Runs the existing experiment analysis on the REDUCE Level 1 of a campaign and adds: actual-timestamp sky-null slope and per-pair
    predictions (independent of the plan schedule), frame coherence (forensics V1), controls, spur geometry, prominent statements."""
    import types
    import numpy as np
    from science_forensics.experiment import cli_impl as C
    from science_forensics.experiment import design as D
    from science_forensics.ingest import load_forensics_input, site_from_observer_config
    from science_forensics.measure import FeatureMeasurement
    from science_forensics.models import ForensicsConfig
    from science_forensics.statistics import analyze as v1_analyze

    field_dir, cap_dir = ctx.p(FIELD_ROOT, cid), ctx.p(MOSAIC_ROOT, cid)
    man = json.loads((field_dir / "manifest.json").read_text())
    run_csv = cap_dir / "mosaic.csv"
    args = types.SimpleNamespace(reduce_session_dir=str(reduce_dir), plan_csv=str(run_csv), center=window[0], half_width=window[1])
    try:
        res = C.cmd_analyze(args)
    except SystemExit as exc:
        raise FieldError(str(exc))
    inp = load_forensics_input(reduce_dir)
    pts = inp.points
    labels = [dict((int(r["point_number"]), r["experiment_label"]) for r in _read_rows(run_csv)[1])[p.point_index] for p in pts]
    site = _site(ctx)
    k = inp.rest_frequency_hz / D.C_LIGHT_M_S
    # ---- actual timestamps + Level 1 pointing -> independent LSRK shift (frozen helper); must agree with REDUCE's own
    diffs, shift_actual = [], []
    for p in pts:
        s = D.lsrk_shift_m_s(D.Position("x", p.ra_deg, p.dec_deg), p.t_mid_s, site)
        shift_actual.append(s)
        diffs.append(s - p.lsrk_shift_m_s)
    t_h = np.array([(p.t_mid_s - pts[0].t_mid_s) / 3600.0 for p in pts])
    a_idx = [i for i, l in enumerate(labels) if l == "A"]
    sky_null = float(k * np.polyfit(t_h[a_idx], np.array(shift_actual)[a_idx], 1)[0]) if len(a_idx) >= 3 and np.ptp(t_h[a_idx]) > 0 else None
    res["actual_timestamps"] = {"first_capture_utc": datetime.fromtimestamp(pts[0].t_mid_s, tz=timezone.utc).isoformat(), "last_capture_utc": datetime.fromtimestamp(pts[-1].t_mid_s, tz=timezone.utc).isoformat(),
                                "span_hours_actual": float(t_h[-1]), "used_for": "all predictions (sky-null slope, pair offsets, matched-pair times); the planned schedule is never used",
                                "shift_crosscheck_max_abs_m_s": float(np.max(np.abs(diffs))), "shift_crosscheck_ok": bool(np.max(np.abs(diffs)) < 5.0),
                                "sky_null_slope_hz_per_hour_from_actual": sky_null}
    # ---- pair-level predictions at the ACTUAL time of every cross-position capture
    sh_l1 = np.array([p.lsrk_shift_m_s for p in pts])
    tt = np.array([p.t_mid_s for p in pts])
    pairs = res.get("matched_pairs", {}).get("cross_position_pairs", [])
    pl = {}
    for c in pairs:
        i, (b0, b1) = c["capture"], c["bracket"]        # matched_pairs reports 0-based positions in the capture list (not point_index)
        w = (tt[i] - tt[b0]) / (tt[b1] - tt[b0])
        pred = k * (sh_l1[i] - ((1 - w) * sh_l1[b0] + w * sh_l1[b1]))
        pl.setdefault(c["label"], []).append((c["delta_hz"], c["sigma_hz"], pred))
    pair_level = {}
    chi_sky = chi_zero = 0.0
    dof = 0
    for lab, rows in pl.items():
        d, s, pr = (np.array(x) for x in zip(*rows))
        w = 1 / s ** 2
        mean, se = float(np.sum(w * d) / np.sum(w)), float(np.sqrt(1 / np.sum(w)))
        se = max(se, float(np.std(d, ddof=1) / np.sqrt(len(d))) if len(d) > 2 else se)
        pm = float(np.sum(w * pr) / np.sum(w))
        pair_level[lab] = {"n_pairs": len(d), "measured_offset_hz": mean, "se_hz": se, "sky_fixed_prediction_hz": pm, "receiver_fixed_prediction_hz": 0.0,
                           "excess_over_sky_hz": mean - pm, "excess_over_sky_sigma": (mean - pm) / se}
        chi_sky += ((mean - pm) / se) ** 2
        chi_zero += (mean / se) ** 2
        dof += 1
    res["pair_level_test"] = {"by_position": pair_level, "chi2_per_dof_vs_sky_fixed": chi_sky / dof if dof else None,
                              "chi2_per_dof_vs_receiver_fixed": chi_zero / dof if dof else None, "prediction_time_basis": "actual capture timestamps"}
    # ---- frame coherence via the forensics V1 statistics on the TRACKED measurements
    cfg_kw = dict(window_center=window[0], window_half_width=window[1], window_frame="lsrk_velocity", feature_id=f"FIELD-{cid}")
    cfg_kw.update(site_from_observer_config(ctx.p("observer_config.json")))
    cfg = ForensicsConfig(**cfg_kw)
    ms = [FeatureMeasurement(**{k2: v for k2, v in m.items()}) for m in res["measurements"]]
    try:
        v1 = v1_analyze(inp, ms, cfg)
        res["frame_coherence"] = v1.get("frame_coherence")
        res["v1_status"] = v1.get("status")
    except Exception as exc:
        res["frame_coherence"] = None
        res["v1_status"] = f"UNAVAILABLE: {type(exc).__name__}: {exc}"
    res["known_spurs"] = spur_position_stability(pts)
    res["statements"] = statements(res, labels)
    res["campaign_id"] = cid
    res["labels_used"] = "experiment_label of the run CSV joined on REDUCE point_index == point_number"
    return res


def statements(res: dict, labels: list) -> list:
    d, out = res["decision"], []
    rs = res.get("reference_series", {})
    at = res["actual_timestamps"]
    if rs:
        excess = rs["drift_hz_per_hour"] - (at["sky_null_slope_hz_per_hour_from_actual"] if at["sky_null_slope_hz_per_hour_from_actual"] is not None else rs["sky_null_drift_hz_per_hour"])
        z = abs(excess) / rs["drift_se_hz_per_hour"] if rs["drift_se_hz_per_hour"] else float("nan")
        lab = "INCONSISTENT WITH" if d["H0_time_rejected"] else ("CONSISTENT WITH" if z < 3 else "UNRESOLVED")
        out.append({"topic": "same-sky temporal drift (A)", "label": lab,
                    "statement": f"{lab} a stationary sky-fixed line: measured {rs['drift_hz_per_hour']:.0f} +/- {rs['drift_se_hz_per_hour']:.0f} Hz/h over {rs['span_hours']:.2f} h "
                                 f"(actual timestamps), sky-fixed expectation {at['sky_null_slope_hz_per_hour_from_actual']} Hz/h; excess {excess:.0f} Hz/h ({z:.1f} sigma, "
                                 f"{d['excess_total_over_span_channels']:.1f} channels over the span)"})
    pl = res["pair_level_test"]
    if pl["by_position"]:
        cs, cz = pl["chi2_per_dof_vs_sky_fixed"], pl["chi2_per_dof_vs_receiver_fixed"]
        offs = "; ".join(f"{k}: {v['measured_offset_hz']:.0f} +/- {v['se_hz']:.0f} Hz (sky-fixed {v['sky_fixed_prediction_hz']:.0f}, receiver-fixed 0)" for k, v in pl["by_position"].items())
        out.append({"topic": "cross-sky matched pairs (A-B, A-C)", "label": _classify(cs), "statement": f"{_classify(cs)} sky-fixed offsets (chi2/dof {cs:.1f}); {_classify(cz)} receiver-fixed offsets (chi2/dof {cz:.1f}); {offs}"})
    else:
        out.append({"topic": "cross-sky matched pairs (A-B, A-C)", "label": "UNRESOLVED", "statement": "UNRESOLVED: no bracketed cross-position pairs in this campaign (experiment A, or too few detections)"})
    cand = res.get("candidate_channel_total")
    ms = res["measurements"]
    ch = [m["centroid_channel"] for m, l in zip(ms, labels) if l == "A" and m["status"] == "OK" and m["centroid_channel"] == m["centroid_channel"]]
    cand_tot = float(ch[-1] - ch[0]) if len(ch) > 1 else None
    gs = res.get("global_shift", {})
    gvc = res.get("global_vs_candidate", {})
    g_tot = gs["cumulative_shift_channels"][-1] if gs.get("cumulative_shift_channels") else None
    g_se = gs["se_channels"][-1] if gs.get("se_channels") else None
    if cand_tot is not None and g_tot is not None:
        ratio = (cand_tot / g_tot) if g_tot is not None and abs(g_tot) > 5 * max(g_se or 0, 0.05) else None
        out.append({"topic": "candidate shift vs global shift (reference position A)", "label": "MEASURED",
                    "statement": f"candidate moved {cand_tot:.1f} channels (A series, first to last capture); the whole spectrum moved {g_tot:.2f} +/- {g_se:.2f} channels; "
                                 + (f"candidate/global ratio {ratio:.2f}" if ratio is not None else "candidate/global ratio undefined (global shift not significant)")
                                 + f"; regression slope of candidate on global {gvc.get('candidate_vs_global_slope')} (tracks: {gvc.get('tracks_candidate')}, flat: {gvc.get('flat')}). Reported as measured: no cause is inferred here"})
    fc = res.get("frame_coherence")
    if fc:
        fr = fc["frames"]
        out.append({"topic": "frame coherence (forensics V1)", "label": "MEASURED",
                    "statement": "centroid RMS (channel widths): " + ", ".join(f"{k} {v['rms_channels']:.2f}" for k, v in fr.items())})
    cf = res.get("control_features") or []
    out.append({"topic": "control features", "label": "MEASURED" if cf else "UNRESOLVED",
                "statement": (f"{len(cf)} control lines tracked: " + "; ".join(f"ch {c['start_channel']} range {c.get('channel_range', float('nan')):.1f} ch, gamma {c.get('gamma_sky')}" for c in cf)) if cf
                else "UNRESOLVED: no independent narrow control line stands out in the median spectrum (z >= 8); the control test cannot be made"})
    out.append({"topic": "classification", "label": d["decision"], "statement": f"{d['decision']}: {d.get('reason', '')}"})
    return out


def analysis_markdown(res: dict) -> str:
    L = [f"# Field analysis {res['campaign_id']}", "", f"**Classification: {res['decision']['decision']}**  ({res['decision'].get('reason', '')})", ""]
    for s in res["statements"]:
        L.append(f"- **{s['topic']}** [{s['label']}]: {s['statement']}")
    at = res["actual_timestamps"]
    L += ["", f"Actual timestamps: {at['first_capture_utc']} .. {at['last_capture_utc']} ({at['span_hours_actual']:.2f} h); LSRK-shift cross-check vs REDUCE max {at['shift_crosscheck_max_abs_m_s']:.2f} m/s (ok: {at['shift_crosscheck_ok']}).",
          f"Known spurs (mask geometry): {res['known_spurs'].get('note')}", ""]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ quick diagnostic

def quick_diagnostic(ctx: Ctx, cid: str, reduce_dir: Path, window=DEFAULT_WINDOW) -> dict:
    """Diagnostic only (no classification): A repetitions, candidate presence and SNR, centroid range, global shift range, mask fraction."""
    import numpy as np
    from science_forensics.experiment import analysis as A
    from science_forensics.ingest import load_forensics_input
    from science_forensics.measure import measure_point
    from science_forensics.models import ForensicsConfig
    inp = load_forensics_input(reduce_dir)
    pts = inp.points
    lab_of = {int(r["point_number"]): r["experiment_label"] for r in _read_rows(ctx.p(MOSAIC_ROOT, cid, "mosaic.csv"))[1]}
    labels = [lab_of.get(p.point_index, "?") for p in pts]
    a_pts = [p for p, l in zip(pts, labels) if l == "A"]
    cfg = ForensicsConfig(window_center=window[0], window_half_width=window[1], window_frame="lsrk_velocity", feature_id="QUICK")
    m0 = measure_point(a_pts[0], cfg) if a_pts else None
    start = m0.centroid_channel if m0 is not None and np.isfinite(m0.centroid_channel) else None
    out = {"campaign_id": cid, "diagnostic_only": True, "n_points_level1": len(pts), "A_repetitions": len(a_pts),
           "mask_fraction_median": float(np.median([np.mean((p.mask != 0) | ~np.isfinite(p.value)) for p in pts]))}
    if start is None:
        out["candidate_window_present"] = False
        out["note"] = "no measurable feature in the candidate window of the first A capture"
        return out
    ms = A.track_line(a_pts, start_channel=float(start), search_half_width=75, fit_half_width=30)
    ok = [m for m in ms if m.status == "OK"]
    snr = [m.snr_formal for m in ok if np.isfinite(m.snr_formal)]
    ch = [m.centroid_channel for m in ok]
    gs = A.global_shift_series(a_pts, ["A"] * len(a_pts), exclude_channels=A.spur_and_masked_channels(pts),
                               candidate_channels=np.array([m.centroid_channel if m.status == "OK" else np.nan for m in ms]), candidate_half_width=60, max_lag=100)
    out.update({"candidate_window_present": bool(len(ok) >= 0.5 * len(ms)), "candidate_detected_fraction": len(ok) / len(ms), "candidate_median_snr_formal": float(np.median(snr)) if snr else None,
                "candidate_centroid_range_channels": float(np.ptp(ch)) if ch else None, "global_shift_range_channels": float(np.ptp(gs["cumulative_shift_channels"])),
                "global_shift_reliable_steps": f"{gs['n_reliable_steps']}/{gs['n_steps']}"})
    return out


# ------------------------------------------------------------------ CLI

def _print(obj, as_json=False, lines=None):
    if as_json or lines is None:
        from science_forensics.models import sanitize
        print(json.dumps(sanitize(obj), indent=2, sort_keys=True))
    else:
        print("\n".join(lines))


def _now_arg(args):
    return datetime.fromisoformat(args.now).astimezone(timezone.utc) if getattr(args, "now", None) else None


def cmd_status(ctx: Ctx, args) -> int:
    plan = load_plan(ctx)
    git, frozen = git_state(ctx), frozen_check(ctx)
    exps = [args.experiment] if args.experiment else list(EXPERIMENTS)
    ports = ctx.listeners()
    sess_active = []
    sp = ctx.p("data", "IQ", "session.csv")
    if sp.is_file():
        sess_active = [r["session_id"] for r in _read_rows(sp)[1] if r.get("status") in ("active", "paused")]
    procs = ctx.capture_procs()
    rows = []
    for exp in exps:
        pc = precheck(ctx, plan, exp, live_endpoints=True)
        window = check_window(ctx, plan, exp, _now_arg(args), list_windows=True)
        epoch = check_epoch(ctx, plan, exp, _now_arg(args))
        gate = gate_preflight(ctx, plan, exp, _now_arg(args))
        conflicts = sorted(p.name for p in ctx.p(MOSAIC_ROOT).glob(f"FORENSICS-{exp}-*")) if ctx.p(MOSAIC_ROOT).is_dir() else []
        rows.append({"experiment": exp, "precheck": pc["status"], "precheck_failed": pc["failed"], "window": window, "epoch": epoch, "preflight_gate": {k: v for k, v in gate.items() if k != "report"},
                     "existing_campaigns": conflicts, "field_status": field_status(ctx, plan, exp, window, epoch, pc, frozen, gate)})
    payload = {"git": git, "plan": str(ctx.plan_path), "plan_sha256": sha256_file(ctx.plan_path), "utc_now": _utc(ctx.now()),
               "mount_indi_listening": ENDPOINTS["INDI"] in ports, "main_sdr_rtl_tcp_listening": ENDPOINTS["MAIN rtl_tcp"] in ports,
               "services": {u: ctx.service_active(u) for u in SERVICES}, "capture_processes_running": procs, "stale_active_or_paused_sessions_in_data_IQ": sess_active[-5:],
               "frozen_modules": frozen, "output_paths": {"campaign_data": f"{MOSAIC_ROOT}/<CAMPAIGN_ID>/", "field_metadata": f"{FIELD_ROOT}/<CAMPAIGN_ID>/"},
               "experiments": rows, "note": "read-only; nothing was created or changed"}
    if args.json:
        _print(payload, True)
    else:
        print(f"commit {git['commit'][:12]} on {git['branch']} | plan {ctx.plan_path} | UTC {payload['utc_now']}")
        print(f"MAIN rtl_tcp :1234 listening={payload['main_sdr_rtl_tcp_listening']} | INDI :7624 listening={payload['mount_indi_listening']} | services {payload['services']} | capture.py running: {len(procs)}")
        print(f"frozen modules: {'UNCHANGED' if frozen['ok'] else 'DIFFER ' + str(frozen['differs'] or 'git unavailable')}")
        for r in rows:
            w = r["window"]
            print(f"[{r['experiment']}] precheck {r['precheck']} {r['precheck_failed']} | window {w['status']} | epoch {r['epoch']['status']} | conflicts {r['existing_campaigns']} | => {r['field_status']}")
            for nw in w.get("next_windows", [])[:4]:
                print(f"      window ({nw['side']} HA): start {nw['first_start_utc']} .. {nw['last_start_utc']}")
    return 0


def cmd_precheck(ctx: Ctx, args) -> int:
    plan = load_plan(ctx)
    r = precheck(ctx, plan, args.experiment, live_endpoints=not args.offline)
    if args.json:
        _print(r, True)
    else:
        for c in r["checks"]:
            print(f"[{c['status']:<7}] {c['name']}: {c['detail']}")
        print(f"PRECHECK {args.experiment}: {r['status']}")
    return 0 if r["status"] == "PASS" else 1


def cmd_preflight(ctx: Ctx, args) -> int:
    plan = load_plan(ctx)
    r = do_preflight(ctx, plan, args.experiment, print_only=args.print_only)
    if r.get("printed_only"):
        print(" ".join(r["argv"]))
        return 0
    print(f"PREFLIGHT {args.experiment}: {r['status']}")
    for x in r.get("reasons", []):
        print(f"  BLOCKED because: {x}")
    if r["status"] == "PASS":
        print("  Nothing was started. Next: run --dry-run, then run (explicit).")
    return 0 if r["status"] == "PASS" else 1


def cmd_run(ctx: Ctx, args) -> int:
    plan = load_plan(ctx)
    if not args.dry_run and args.now:
        raise FieldError("--now is only for status/dry-run/tests; a live run uses the real clock")
    r = run_experiment(ctx, plan, args.experiment, dry_run=args.dry_run, yes=args.yes, allow_frozen_diff=args.allow_frozen_diff)
    if args.json:
        _print(r, True)
    else:
        print(f"campaign {r['campaign_id']} experiment {r['experiment']}: {r['status']}")
        print("command: " + " ".join(r["argv"]))
        print(f"sequence: {r['n_captures']} captures {r['sequence_counts']} head {r['sequence_head']} expected {r['expected_duration_min']['median']:.0f} min (p90 {r['expected_duration_min']['p90']:.0f})")
        for b in r["blockers"]:
            print(f"  BLOCKER: {b}")
        if r["status"] in ("DRY_RUN_OK", "DRY_RUN_WOULD_BLOCK"):
            print("dry run: no directory created, no hardware touched")
        elif r.get("next"):
            print(f"next: ./.venv/bin/python scripts/science_forensics_field.py postcheck {r['campaign_id']}")
    return 0 if r["status"] in ("DRY_RUN_OK", "CAPTURE_EXIT_0") else (1 if r["status"] != "INTERRUPTED" else 130)


def cmd_postcheck(ctx: Ctx, args) -> int:
    plan = None
    r = postcheck(ctx, args.campaign_id, plan)
    if args.quick or args.reduce_session:
        red = Path(args.reduce_session) if args.reduce_session else find_reduce_session(ctx, args.campaign_id)
        if red is None:
            r["quick_diagnostic"] = {"status": "NEEDS_REDUCE", "command": " ".join(reduce_command(ctx, load_plan(ctx), args.campaign_id))}
        else:
            r["quick_diagnostic"] = quick_diagnostic(ctx, args.campaign_id, red, window=(getattr(args, "center", DEFAULT_WINDOW[0]), getattr(args, "half_width", DEFAULT_WINDOW[1])))
    out = ctx.p(FIELD_ROOT, args.campaign_id)
    if out.is_dir():
        _atomic_json(out / "postcheck.json", r)
    if args.json:
        _print(r, True)
    else:
        print(f"POSTCHECK {args.campaign_id}: {r['verdict']}  expected {r.get('capture_count_expected')} valid {r.get('capture_count_actual')} "
              f"counts {r.get('counts_valid')} (plan {r.get('counts_plan')}) duration {r.get('campaign_duration_min')} min, order ok {r.get('design_order_ok')}")
        for i in r["issues"]:
            print(f"  [{i['severity']}] {i['what']}")
        if "quick_diagnostic" in r:
            print(f"  quick: {r['quick_diagnostic']}")
    return {"COMPLETE": 0, "PARTIAL": 2, "FAIL": 1}[r["verdict"]]


def cmd_analyze(ctx: Ctx, args) -> int:
    plan = load_plan(ctx)
    cid = args.campaign_id
    red = Path(args.reduce_session) if args.reduce_session else find_reduce_session(ctx, cid)
    if red is None:
        cmd = reduce_command(ctx, plan, cid)
        if not args.run_reduce:
            print("No COMPLETED REDUCE session for this campaign. Run REDUCE first (existing tool, one heavy job at a time):")
            print("  " + " ".join(cmd))
            print("or re-run analyze with --run-reduce")
            return 3
        r = ctx.run(cmd, timeout=3600, cwd=str(ctx.root))
        if r["returncode"] != 0:
            print(f"REDUCE failed (exit {r['returncode']}):\n{r['stderr'][-800:]}")
            return 1
        red = find_reduce_session(ctx, cid)
    res = analyze_campaign(ctx, plan, cid, red, window=(args.center, args.half_width))
    out = ctx.p(FIELD_ROOT, cid, "analysis")
    out.mkdir(parents=True, exist_ok=True)
    _atomic_json(out / "experiment_analysis.json", res)
    (out / "summary.md").write_text(analysis_markdown(res))
    man_p = ctx.p(FIELD_ROOT, cid, "manifest.json")
    man = json.loads(man_p.read_text())
    man["analysis_session"] = str(out)
    man["reduce_session"] = str(red)
    if ctx.p(FIELD_ROOT, cid, "inputs", "plan.yaml").is_file() and sha256_file(ctx.p(FIELD_ROOT, cid, "inputs", "plan.yaml")) != man["plan_sha256"]:
        man["provenance_warning"] = "inputs/plan.yaml no longer matches the recorded plan_sha256"
    _atomic_json(man_p, man)
    print(analysis_markdown(res))
    print(f"written {out}")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", default=DEFAULT_PLAN)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", help="UTC ISO time override (status/precheck/dry-run/tests only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status"); p.add_argument("experiment", nargs="?", choices=EXPERIMENTS)
    p = sub.add_parser("precheck"); p.add_argument("experiment", choices=EXPERIMENTS); p.add_argument("--offline", action="store_true", help="skip the passive endpoint checks (desk use)")
    p = sub.add_parser("preflight"); p.add_argument("experiment", choices=EXPERIMENTS); p.add_argument("--print-only", action="store_true", help="print the exact capture.py command; execute nothing")
    p = sub.add_parser("run"); p.add_argument("experiment", choices=EXPERIMENTS)
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--yes", action="store_true", help="skip the interactive confirmation of a LIVE run")
    p.add_argument("--allow-frozen-diff", action="store_true", help="recorded in the manifest")
    p = sub.add_parser("postcheck"); p.add_argument("campaign_id"); p.add_argument("--quick", action="store_true"); p.add_argument("--reduce-session")
    p.add_argument("--center", type=float, default=DEFAULT_WINDOW[0]); p.add_argument("--half-width", type=float, default=DEFAULT_WINDOW[1])
    p = sub.add_parser("analyze"); p.add_argument("campaign_id"); p.add_argument("--reduce-session"); p.add_argument("--run-reduce", action="store_true")
    p.add_argument("--center", type=float, default=DEFAULT_WINDOW[0], help="candidate LSRK velocity centre (m/s)")
    p.add_argument("--half-width", type=float, default=DEFAULT_WINDOW[1], help="candidate LSRK half width (m/s)")
    return ap


def main(argv=None, ctx: Ctx | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = ctx or Ctx(root=REPO, plan=args.plan, now=_now_arg(args))
    try:
        return {"status": cmd_status, "precheck": cmd_precheck, "preflight": cmd_preflight, "run": cmd_run, "postcheck": cmd_postcheck, "analyze": cmd_analyze}[args.cmd](ctx, args)
    except FieldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
