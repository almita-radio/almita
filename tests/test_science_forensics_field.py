"""SCIENCE FORENSICS FIELD EXECUTION PACK: the wrapper orchestrates capture.py, it never reimplements it. Every external effect (git,
capture.py, mount/INDI/SDR endpoints, systemd, /proc) is injected, so NO test touches hardware or runs capture.py (the wrapper's
default runner even refuses to, under pytest)."""
import ast
import csv
import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sff", ROOT / "scripts" / "science_forensics_field.py")
F = importlib.util.module_from_spec(spec)
sys.modules["sff"] = F
spec.loader.exec_module(F)

NOW_OK = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)            # inside a negative-HA window for A, B, C (65 min run)
NOW_BAD = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)            # positions far from the planned geometry


class FakeRunner:
    """Records every subprocess request; answers git/capture.py deterministically."""
    def __init__(self, preflight_out=None, preflight_rc=0, git_diff=""):
        self.calls, self.preflight_out, self.preflight_rc, self.git_diff = [], preflight_out, preflight_rc, git_diff

    def __call__(self, argv, timeout=120, cwd=None):
        self.calls.append(list(argv))
        stamp = "2026-09-21T14:00:00+00:00"
        base = {"argv": list(argv), "stdout": "", "stderr": "", "returncode": 0, "started_utc": stamp, "ended_utc": stamp}
        if argv[0] == "git":
            if argv[1] == "rev-parse":
                base["stdout"] = "0123456789abcdef0123456789abcdef01234567\n" if argv[2] == "HEAD" else "science-forensics-v1\n"
            elif argv[1] == "diff":
                base["stdout"] = self.git_diff
        elif any(Path(a).name == "capture.py" for a in argv):
            base["stdout"] = self.preflight_out if self.preflight_out is not None else PASS_OUT
            base["returncode"] = self.preflight_rc
        return base

    def capture_calls(self):
        return [c for c in self.calls if c[0] != "git" and any(Path(a).name == "capture.py" for a in c)]


PASS_OUT = "[2026-09-21 14:00:00.000] [PASS] Grid: 65 planned points\n[2026-09-21 14:00:01.000] [PASS] SDR network: configured\n[2026-09-21 14:00:02.000] PREFLIGHT: PASS\n"
FAIL_OUT = "[t] [PASS] Grid: ok\n[t] [FAIL] INDI/mount: controller is not connected\n[t] PREFLIGHT: FAIL\nNo hardware movement executed.\n"


@pytest.fixture()
def repo(tmp_path):
    """A tiny copy of the repo surface the wrapper reads: the OFFICIAL plan/CSVs (never regenerated) and observer_config."""
    root = tmp_path / "repo"
    (root / "examples").mkdir(parents=True)
    shutil.copyfile(ROOT / "examples/science_forensics_experiment.yaml", root / "examples/science_forensics_experiment.yaml")
    shutil.copytree(ROOT / "examples/science_forensics_experiment", root / "examples/science_forensics_experiment")
    shutil.copyfile(ROOT / "observer_config.json", root / "observer_config.json")
    (root / "capture.py").write_text("# stub: tests never execute it\n")
    (root / "almita_reduce.py").write_text("# stub\n")
    (root / "data").mkdir()
    return root


def make_ctx(root, now=NOW_OK, runner=None, spawn=None, listeners=None, procs=None):
    runner = runner or FakeRunner()
    spawned = []

    def fake_spawn(argv, cwd, out, err, log=print):
        spawned.append(list(argv))
        Path(out).write_text("fake capture stdout\n")
        Path(err).write_text("")
        return {"returncode": 0, "interrupted_at_utc": None}

    ctx = F.Ctx(root=root, now=now, run=runner, listeners=listeners or (lambda: {1234, 7624}), service_active=lambda u: "active",
                capture_procs=procs or (lambda: []), spawn=spawn or fake_spawn, disk_free=lambda p: 10 ** 12)
    ctx.spawned, ctx.runner = spawned, runner
    return ctx


def tree(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in Path(root).rglob("*") if p.is_file())


def passed_preflight(ctx, exp="B"):
    ctx.runner.preflight_out, ctx.runner.preflight_rc = PASS_OUT, 0
    plan = F.load_plan(ctx)
    r = F.do_preflight(ctx, plan, exp)
    assert r["status"] == "PASS"
    return plan


# ------------------------------------------------------------------ status / precheck

def test_status_has_no_side_effects(repo, capsys):
    ctx = make_ctx(repo)
    before = tree(repo)
    assert F.main(["status"], ctx=ctx) == 0
    assert tree(repo) == before
    assert not ctx.spawned and not ctx.runner.capture_calls()
    out = capsys.readouterr().out
    assert "READY FOR LIVE PREFLIGHT" in out and "READY FOR FIELD" not in out


def test_status_json_lists_required_fields(repo, capsys):
    ctx = make_ctx(repo)
    F.main(["--json", "status", "B"], ctx=ctx)
    d = json.loads(capsys.readouterr().out)
    for k in ("git", "plan", "utc_now", "mount_indi_listening", "main_sdr_rtl_tcp_listening", "frozen_modules", "output_paths", "experiments"):
        assert k in d
    e = d["experiments"][0]
    assert e["window"]["status"] == "OK" and e["field_status"] == "READY FOR LIVE PREFLIGHT" and "next_windows" in e["window"]


def test_precheck_passes_official_csvs_for_all_experiments(repo):
    ctx = make_ctx(repo)
    plan = F.load_plan(ctx)
    for exp in F.EXPERIMENTS:
        r = F.precheck(ctx, plan, exp)
        assert r["status"] == "PASS", r["failed"]
    names = [c["name"] for c in F.precheck(ctx, plan, "B")["checks"]]
    assert "repeated sky coordinates preserved" in names and "no duplicate capture IDs" in names


@pytest.mark.parametrize("mutate,expected", [
    ("dup_id", "no duplicate capture IDs"), ("bad_dec", "capture.py plan rules"), ("drop_row", "sequence length"),
    ("wrong_label", "label sequence equals the plan"), ("edited_csv", "CSV sha256 equals the plan"),
    ("drop_column", "required capture.py columns"), ("merge_coords", "repeated sky coordinates preserved")])
def test_precheck_rejects_malformed_csv(repo, mutate, expected):
    ctx = make_ctx(repo)
    plan = F.load_plan(ctx)
    path = repo / plan["experiments"]["B"]["csv"]["path"]
    fields, rows = F._read_rows(path)
    if mutate == "dup_id":
        rows[5]["point_number"] = rows[4]["point_number"]
    elif mutate == "bad_dec":
        rows[3]["target_dec_degrees"] = "123.0"
    elif mutate == "drop_row":
        rows = rows[:-1]
    elif mutate == "wrong_label":
        rows[2]["experiment_label"] = "B" if rows[2]["experiment_label"] != "B" else "C"
    elif mutate == "edited_csv":
        rows[0]["error_message"] = "x"
    elif mutate == "drop_column":
        fields = [f for f in fields if f != "data_filename"]
        rows = [{k: v for k, v in r.items() if k != "data_filename"} for r in rows]
    elif mutate == "merge_coords":
        for r in rows:
            r["target_ra_hours"], r["target_dec_degrees"] = rows[0]["target_ra_hours"], rows[0]["target_dec_degrees"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader(); w.writerows(rows)
    r = F.precheck(ctx, plan, "B")
    assert r["status"] == "BLOCKED"
    assert any(expected in n for n in r["failed"]), r["failed"]


def test_precheck_blocks_when_endpoints_are_down_but_offline_mode_does_not(repo):
    ctx = make_ctx(repo, listeners=lambda: set())
    plan = F.load_plan(ctx)
    assert F.precheck(ctx, plan, "A")["status"] == "BLOCKED"
    assert F.precheck(ctx, plan, "A", live_endpoints=False)["status"] == "PASS"


def test_precheck_never_uses_a_connection_or_capture(repo):
    ctx = make_ctx(repo)
    F.precheck(ctx, F.load_plan(ctx), "AB")
    assert not ctx.runner.capture_calls() and not ctx.spawned


# ------------------------------------------------------------------ preflight contract

def test_preflight_invokes_the_real_capture_py_with_preflight_only(repo):
    ctx = make_ctx(repo)
    plan = passed_preflight(ctx, "B")
    calls = ctx.runner.capture_calls()
    assert len(calls) == 1
    argv = calls[0]
    assert Path(argv[1]).name == "capture.py" and "--preflight-only" in argv and "--debug" in argv
    inst = plan["instrument_config_identical_to_feature_campaign"]
    assert argv[argv.index("--capture") + 1] == str(inst["capture_seconds"]) and argv[argv.index("--settle") + 1] == str(inst["settle_seconds"])
    assert argv[argv.index("--sdr-freq") + 1] == "1420405752" and argv[argv.index("--sdr-gain") + 1] == "40.2" and "--rfi-ref-enabled" in argv
    csv_arg = Path(argv[argv.index("--csv") + 1])
    assert "_preflight" in csv_arg.parts and csv_arg.is_file()                            # a per-preflight copy, never the official CSV
    assert (csv_arg.parent / "observer_config.json").is_file()
    assert not ctx.spawned                                                              # preflight does not start an observation


def test_preflight_report_persists_evidence_and_hashes(repo):
    ctx = make_ctx(repo)
    passed_preflight(ctx, "B")
    latest = json.loads((repo / F.FIELD_ROOT / "latest_preflight_B.json").read_text())
    rep = json.loads(Path(latest["report_path"]).read_text())
    for k in ("status", "returncode", "started_utc", "ended_utc", "git_commit", "plan_sha256", "csv_source_sha256", "observer_config_sha256", "argv"):
        assert rep[k] is not None
    assert rep["git_commit"].startswith("0123456789")
    plan = F.load_plan(ctx)
    assert rep["plan_sha256"] == F.sha256_file(ctx.plan_path) and rep["csv_source_sha256"] == plan["experiments"]["B"]["csv"]["sha256"]
    assert Path(rep["stdout_file"]).read_text() == PASS_OUT


@pytest.mark.parametrize("out,rc", [(FAIL_OUT, 1), (PASS_OUT, 1), ("nothing useful\n", 0), (PASS_OUT + "[t] [FAIL] Disk space: low\n", 0)])
def test_preflight_has_only_pass_or_blocked(repo, out, rc):
    ctx = make_ctx(repo, runner=FakeRunner(preflight_out=out, preflight_rc=rc))
    r = F.do_preflight(ctx, F.load_plan(ctx), "A")
    assert r["status"] == "BLOCKED" and r["reasons"]            # never "mostly pass"


def test_preflight_block_shows_exact_reason(repo):
    ctx = make_ctx(repo, runner=FakeRunner(preflight_out=FAIL_OUT, preflight_rc=1))
    r = F.do_preflight(ctx, F.load_plan(ctx), "A")
    assert any("INDI/mount: controller is not connected" in x for x in r["reasons"]) and any("exit code 1" in x for x in r["reasons"])


def test_preflight_print_only_executes_nothing(repo):
    ctx = make_ctx(repo)
    r = F.do_preflight(ctx, F.load_plan(ctx), "B", print_only=True)
    assert r["printed_only"] and not ctx.runner.capture_calls() and not (repo / F.FIELD_ROOT).exists()


def test_failed_preflight_blocks_run(repo):
    ctx = make_ctx(repo, runner=FakeRunner(preflight_out=FAIL_OUT, preflight_rc=1))
    plan = F.load_plan(ctx)
    F.do_preflight(ctx, plan, "B")
    r = F.run_experiment(ctx, plan, "B", dry_run=False, yes=True)
    assert r["status"] == "BLOCKED" and any("preflight" in b for b in r["blockers"])
    assert not ctx.spawned and not (repo / F.MOSAIC_ROOT).exists()


def test_run_without_any_preflight_is_blocked(repo):
    ctx = make_ctx(repo)
    r = F.run_experiment(ctx, F.load_plan(ctx), "A", dry_run=False, yes=True)
    assert r["status"] == "BLOCKED" and not ctx.spawned


def test_preflight_pass_never_auto_starts_the_run(repo):
    ctx = make_ctx(repo)
    assert F.main(["preflight", "B"], ctx=ctx) == 0
    assert not ctx.spawned and not (repo / F.MOSAIC_ROOT).exists()


def test_stale_or_mismatched_preflight_is_refused(repo):
    ctx = make_ctx(repo)
    plan = passed_preflight(ctx, "B")
    later = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)
    assert not F.gate_preflight(ctx, plan, "B", now=later)["ok"]                                       # older than the limit
    assert F.gate_preflight(ctx, plan, "B", now=NOW_OK)["ok"]
    (repo / "observer_config.json").write_text((repo / "observer_config.json").read_text() + " ")
    g = F.gate_preflight(ctx, plan, "B", now=NOW_OK)
    assert not g["ok"] and "observer_config_sha256" in g["reason"]


def test_default_runner_refuses_capture_py_under_pytest():
    with pytest.raises(RuntimeError):
        F._real_run([sys.executable, "capture.py", "--csv", "x.csv"])


# ------------------------------------------------------------------ dry run and live run construction

def test_dry_run_prints_exact_command_and_touches_nothing(repo, capsys):
    ctx = make_ctx(repo)
    passed_preflight(ctx, "B")
    before = tree(repo)
    n_calls = len(ctx.runner.calls)
    assert F.main(["run", "B", "--dry-run"], ctx=ctx) == 0
    out = capsys.readouterr().out
    assert "capture.py --csv" in out and "FORENSICS-B-20260921T140000Z" in out and "DRY_RUN_OK" in out and "no hardware touched" in out
    assert tree(repo) == before and not ctx.spawned
    assert not [c for c in ctx.runner.calls[n_calls:] if c[0] != "git" and any(Path(a).name == "capture.py" for a in c)]


def test_dry_run_reports_blockers_without_failing_hard(repo):
    ctx = make_ctx(repo)
    r = F.run_experiment(ctx, F.load_plan(ctx), "AB", dry_run=True, yes=False)
    assert r["status"] == "DRY_RUN_WOULD_BLOCK" and any("preflight" in b for b in r["blockers"]) and not ctx.spawned


def test_experiment_must_be_explicit(repo):
    with pytest.raises(SystemExit):
        F.main(["run"], ctx=make_ctx(repo))
    with pytest.raises(F.FieldError):
        F.experiment_info(make_ctx(repo), F.load_plan(make_ctx(repo)), "C")


def test_live_run_command_construction_and_provenance(repo):
    ctx = make_ctx(repo)
    plan = passed_preflight(ctx, "B")
    r = F.run_experiment(ctx, plan, "B", dry_run=False, yes=True)
    assert r["status"] == "CAPTURE_EXIT_0" and len(ctx.spawned) == 1
    argv = ctx.spawned[0]
    cid = r["campaign_id"]
    assert cid == "FORENSICS-B-20260921T140000Z"
    inst = plan["instrument_config_identical_to_feature_campaign"]
    expect = [sys.executable, "capture.py", "--csv", str(repo / F.MOSAIC_ROOT / cid / "mosaic.csv"), "--settle", "2.0", "--capture", "10.0", "--sdr-freq", "1420405752",
              "--sdr-rate", "2400000", "--sdr-gain", "40.2", "--input-topology", "antenna", "--min-altitude", "10.0", "--rfi-ref-enabled",
              "--rfi-ref-gain-db", "25.0", "--rfi-ref-serial", "00000002"]
    assert argv == expect and "--preflight-only" not in argv and inst["sdr_gain_db"] == 40.2
    man = json.loads((repo / F.FIELD_ROOT / cid / "manifest.json").read_text())
    for k in ("campaign_id", "experiment", "plan", "plan_sha256", "csv", "csv_sha256", "git_commit", "start_utc", "end_utc", "capture_exit", "preflight_result",
              "capture_count_expected", "capture_count_actual", "analysis_session", "observer_config_sha256", "operator_command"):
        assert k in man
    assert man["plan_sha256"] == F.sha256_file(ctx.plan_path) and man["csv_sha256"] == plan["experiments"]["B"]["csv"]["sha256"]
    assert man["preflight_result"] == "PASS" and man["capture_count_expected"] == 65 and man["capture_exit"] == 0
    # the official CSV is untouched; the run copy differs ONLY in session_name / data_filename
    assert F.sha256_file(repo / plan["experiments"]["B"]["csv"]["path"]) == plan["experiments"]["B"]["csv"]["sha256"]
    assert set(man["csv_run_columns_changed_vs_official"]) == {"session_name", "data_filename"}
    _, a = F._read_rows(repo / plan["experiments"]["B"]["csv"]["path"])
    _, b = F._read_rows(repo / F.MOSAIC_ROOT / cid / "mosaic.csv")
    for x, y in zip(a, b):
        assert {k: v for k, v in x.items() if k not in ("session_name", "data_filename")} == {k: v for k, v in y.items() if k not in ("session_name", "data_filename")}
        assert y["session_name"] == cid and y["data_filename"].startswith(cid)
    assert [r["experiment_label"] for r in b] == plan["experiments"]["B"]["sequence"]           # repeated A/B/C visits preserved in order


def test_inputs_are_immutable_provenance_copies(repo):
    ctx = make_ctx(repo)
    plan = passed_preflight(ctx, "A")
    r = F.run_experiment(ctx, plan, "A", dry_run=False, yes=True)
    inputs = repo / F.FIELD_ROOT / r["campaign_id"] / "inputs"
    for name in ("plan.yaml", "source_mosaic.csv", "observer_config.json"):
        p = inputs / name
        assert p.is_file() and not os.access(p, os.W_OK) or os.geteuid() == 0
    assert F.sha256_file(inputs / "plan.yaml") == F.sha256_file(ctx.plan_path)


def test_run_refuses_when_confirmation_missing(repo):
    ctx = make_ctx(repo)
    plan = passed_preflight(ctx, "A")
    r = F.run_experiment(ctx, plan, "A", dry_run=False, yes=False, confirm_input=lambda prompt: "no")
    assert r["status"] == "NOT_STARTED" and not ctx.spawned
    r = F.run_experiment(ctx, plan, "A", dry_run=False, yes=False, confirm_input=lambda prompt: "A")
    assert r["status"] == "CAPTURE_EXIT_0" and len(ctx.spawned) == 1


def test_wrong_window_blocks_run_and_never_changes_positions(repo):
    ctx = make_ctx(repo, now=NOW_BAD)
    plan = F.load_plan(ctx)
    pos_before = json.dumps(plan["positions"], sort_keys=True)
    w = F.check_window(ctx, plan, "B", NOW_BAD)
    assert w["status"] == "BLOCKED FOR PLANNED GEOMETRY" and "NOT changed" in w["reason"]
    ctx.runner.preflight_out = PASS_OUT
    F.do_preflight(ctx, plan, "B")
    r = F.run_experiment(ctx, plan, "B", dry_run=False, yes=True)
    assert r["status"] == "BLOCKED" and any("BLOCKED FOR PLANNED GEOMETRY" in b for b in r["blockers"]) and not ctx.spawned
    assert json.dumps(F.load_plan(ctx)["positions"], sort_keys=True) == pos_before
    assert F.field_status(ctx, plan, "B", w, {"status": "OK"}, {"status": "PASS"}, {"ok": True}, {"ok": True}) == "BLOCKED"


def test_run_crossing_the_meridian_mid_run_is_blocked(repo):
    late = datetime(2026, 9, 21, 15, 50, tzinfo=timezone.utc)          # starts negative, would cross to positive HA during the run
    w = F.check_window(make_ctx(repo, now=late), F.load_plan(make_ctx(repo)), "B", late)
    assert w["status"] == "BLOCKED FOR PLANNED GEOMETRY"


def test_live_run_rejects_now_override(repo):
    ctx = make_ctx(repo)
    with pytest.raises(F.FieldError):
        F.cmd_run(ctx, F.build_parser().parse_args(["--now", "2026-09-21T14:00:00+00:00", "run", "A"]))


def test_epoch_check_offers_a_new_plan_and_never_mutates(repo):
    ctx = make_ctx(repo)
    plan = F.load_plan(ctx)
    sha = F.sha256_file(ctx.plan_path)
    e = F.check_epoch(ctx, plan, "B", datetime(2027, 3, 21, 14, 0, tzinfo=timezone.utc))
    assert "design --epoch 2027-03-21" in e["regenerate_command"] and "data/experiments/EXP-20270321/plan.yaml" in e["regenerate_command"]
    assert F.sha256_file(ctx.plan_path) == sha


def test_frozen_module_difference_blocks_run_but_is_recorded_when_allowed(repo):
    ctx = make_ctx(repo, runner=FakeRunner(git_diff="reduce_engine/pipeline.py\n"))
    plan = passed_preflight(ctx, "A")
    r = F.run_experiment(ctx, plan, "A", dry_run=False, yes=True)
    assert r["status"] == "BLOCKED" and any("frozen modules differ" in b for b in r["blockers"])
    r = F.run_experiment(ctx, plan, "A", dry_run=False, yes=True, allow_frozen_diff=True)
    assert r["status"] == "CAPTURE_EXIT_0"
    assert json.loads((repo / F.FIELD_ROOT / r["campaign_id"] / "manifest.json").read_text())["frozen_differs_allowed"] is True


def test_running_capture_process_and_existing_campaign_block(repo):
    ctx = make_ctx(repo, procs=lambda: [{"pid": 4242, "cmdline": "python capture.py"}])
    plan = passed_preflight(ctx, "A")
    assert F.run_experiment(ctx, plan, "A", dry_run=False, yes=True)["status"] == "BLOCKED"
    ctx2 = make_ctx(repo)
    (repo / F.MOSAIC_ROOT / "FORENSICS-A-20260921T140000Z").mkdir(parents=True)
    assert F.run_experiment(ctx2, plan, "A", dry_run=False, yes=True)["status"] == "BLOCKED"


def test_failed_or_interrupted_run_is_kept_as_evidence(repo):
    def spawn(argv, cwd, out, err, log=print):
        Path(out).write_text("partial\n"); Path(err).write_text("Observation interrupted by user\n")
        return {"returncode": 130, "interrupted_at_utc": "2026-09-21T14:10:00+00:00"}
    ctx = make_ctx(repo, spawn=spawn)
    plan = passed_preflight(ctx, "A")
    r = F.run_experiment(ctx, plan, "A", dry_run=False, yes=True)
    assert r["status"] == "INTERRUPTED"
    cid = r["campaign_id"]
    assert (repo / F.MOSAIC_ROOT / cid / "mosaic.csv").is_file() and (repo / F.FIELD_ROOT / cid / "logs" / "capture_stderr.log").is_file()
    assert json.loads((repo / F.FIELD_ROOT / cid / "manifest.json").read_text())["status"] == "INTERRUPTED"


def test_ready_for_field_only_after_a_fresh_matching_live_pass(repo, capsys):
    ctx = make_ctx(repo)
    F.main(["--json", "status", "B"], ctx=ctx)
    assert json.loads(capsys.readouterr().out)["experiments"][0]["field_status"] == "READY FOR LIVE PREFLIGHT"
    passed_preflight(ctx, "B")
    F.main(["--json", "status", "B"], ctx=ctx)
    assert json.loads(capsys.readouterr().out)["experiments"][0]["field_status"] == "READY FOR FIELD"


# ------------------------------------------------------------------ postcheck

def _plan_and_campaign(repo, exp="B", n_success=None, mutate=None, now=NOW_OK):
    """A fake completed/partial campaign with REAL (tiny) HDF5 files carrying the attributes capture.py writes."""
    import h5py
    ctx = make_ctx(repo, now=now)
    plan = passed_preflight(ctx, exp)
    r = F.run_experiment(ctx, plan, exp, dry_run=False, yes=True)
    cid = r["campaign_id"]
    cap = repo / F.MOSAIC_ROOT / cid
    fields, rows = F._read_rows(cap / "mosaic.csv")
    n = len(rows) if n_success is None else n_success
    for extra in ("actual_capture_order", "reclassified_due_to_ha_change"):          # columns capture.py appends when it rewrites the CSV
        if extra not in fields:
            fields.append(extra)
    iq = cap / "data" / "iq" / f"{cid}-14:00:10"
    iq.mkdir(parents=True)
    t0 = datetime(2026, 9, 21, 14, 0, 10, tzinfo=timezone.utc).timestamp()
    for i, row in enumerate(rows[:n]):
        ts = datetime.fromtimestamp(t0 + i * 32.0, tz=timezone.utc).isoformat()
        row["capture_status"], row["start_time"], row["actual_capture_order"] = "success", ts, str(i + 1)
        with h5py.File(iq / (Path(row["data_filename"]).stem + ".h5"), "w") as h:
            h.create_dataset("iq_data", data=np.zeros(100, dtype=np.uint8))
            for k, v in {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain": 40.2, "bias_tee_enabled": True, "sdr_port": 1234,
                         "capture_status": "success", "file_state": "complete", "temperature_lna_mean_c": 27.0, "temperature_sdr_mean_c": 34.0,
                         "onstep_state_post_goto": "healthy", "tracking_state_at_capture": "on"}.items():
                h.attrs[k] = v
    if mutate:
        mutate(rows, iq)
    with open(cap / "mosaic.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader(); w.writerows(rows)
    return ctx, plan, cid, iq


def test_postcheck_complete_campaign(repo):
    ctx, plan, cid, _ = _plan_and_campaign(repo, "B")
    r = F.postcheck(ctx, cid)
    assert r["verdict"] == "COMPLETE" and r["capture_count_actual"] == 65 and r["design_order_ok"] and r["timestamps_monotonic"]
    assert r["counts_valid"] == {"A": 33, "B": 16, "C": 16} and r["repeated_A_present"] and r["issues"] == []
    assert r["field_metadata"]["ambient_temperature"].startswith("MISSING") and r["field_metadata"]["gain"] == [40.2]


def test_postcheck_partial_campaign_is_partial_not_fail(repo):
    ctx, plan, cid, _ = _plan_and_campaign(repo, "B", n_success=30)
    r = F.postcheck(ctx, cid)
    assert r["verdict"] == "PARTIAL" and r["capture_count_actual"] == 30 and r["capture_count_expected"] == 65
    assert any("without a valid file" in i["what"] for i in r["issues"])
    assert (repo / F.MOSAIC_ROOT / cid / "mosaic.csv").is_file()                     # never deleted


def test_postcheck_fail_when_nothing_valid(repo):
    ctx, plan, cid, _ = _plan_and_campaign(repo, "A", n_success=0)
    assert F.postcheck(ctx, cid)["verdict"] == "FAIL"


def test_postcheck_flags_zero_byte_and_invalid_hdf5(repo):
    def mutate(rows, iq):
        files = sorted(iq.glob("*.h5"))
        files[1].write_bytes(b"")
        files[2].write_bytes(b"not hdf5 at all")
    ctx, plan, cid, _ = _plan_and_campaign(repo, "A", mutate=mutate)
    r = F.postcheck(ctx, cid)
    assert r["verdict"] == "PARTIAL" and r["capture_count_actual"] == 58
    text = " ".join(i["what"] for i in r["issues"])
    assert "zero-byte" in text and "invalid HDF5" in text


def test_postcheck_detects_non_monotonic_timestamps_and_reorder(repo):
    def swap_time(rows, iq):
        rows[10]["start_time"], rows[11]["start_time"] = rows[11]["start_time"], rows[10]["start_time"]
    ctx, plan, cid, _ = _plan_and_campaign(repo, "B", mutate=swap_time)
    r = F.postcheck(ctx, cid)
    assert not r["timestamps_monotonic"] and any("not strictly increasing" in i["what"] for i in r["issues"])

    def reorder(rows, iq):
        for r_ in rows[6:]:                                   # capture.py re-blocked the tail: labels in capture order differ from the plan
            r_["actual_capture_order"] = str(int(r_["actual_capture_order"]) + 1 if int(r_["actual_capture_order"]) % 2 else int(r_["actual_capture_order"]) - 1)
        rows[3]["reclassified_due_to_ha_change"] = "True"
    ctx2, _, cid2, _ = _plan_and_campaign(repo, "B", mutate=reorder, now=datetime(2026, 9, 21, 14, 5, tzinfo=timezone.utc))
    r2 = F.postcheck(ctx2, cid2)
    assert any(i["severity"] == "critical" for i in r2["issues"])


def test_postcheck_large_gap_and_metadata_change(repo):
    def gap(rows, iq):
        for r_ in rows[20:]:
            r_["start_time"] = datetime.fromtimestamp(_ts(r_["start_time"]) + 900, tz=timezone.utc).isoformat()
        import h5py
        with h5py.File(sorted(iq.glob("*.h5"))[5], "r+") as h:
            h.attrs["gain"] = 30.0
    def _ts(s):
        return datetime.fromisoformat(s).timestamp()
    ctx, plan, cid, _ = _plan_and_campaign(repo, "B", mutate=gap)
    r = F.postcheck(ctx, cid)
    assert r["large_gaps"] and any("metadata inconsistent" in i["what"] for i in r["issues"])


def test_postcheck_is_read_only_apart_from_its_own_report(repo):
    ctx, plan, cid, _ = _plan_and_campaign(repo, "A", n_success=10)
    snap = {k: v for k, v in tree(repo) if "postcheck.json" not in k}
    assert F.main(["postcheck", cid], ctx=ctx) == 2                                 # PARTIAL
    assert {k: v for k, v in tree(repo) if "postcheck.json" not in k} == snap


# ------------------------------------------------------------------ analyze: actual timestamps, repeated A

_L1_CACHE = {}


def _build_level1(scenario, tmp_factory, **kw):
    """A REDUCE-format session of the B design whose ACTUAL capture times are 1.35x slower than the plan and contain a 6 min stop."""
    key = (scenario, tuple(sorted(kw.items())))
    if key in _L1_CACHE:
        return _L1_CACHE[key]
    from science_forensics.experiment import design as D
    from science_forensics.experiment.synthetic import build_experiment_campaign
    from science_forensics.synthetic import write_reduce_session_fixture
    site = json.loads((ROOT / "observer_config.json").read_text())["observer"]
    plan = yaml.safe_load((ROOT / "examples/science_forensics_experiment.yaml").read_text())
    pos = {l: D.Position(l, plan["positions"][l]["ra_deg"], plan["positions"][l]["dec_deg"]) for l in "ABC"}
    t0 = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc).timestamp()
    exp = D.expected_signatures(pos, site, t0, 3600.0)
    seq = plan["experiments"]["B"]["sequence"]
    caps = D.build_schedule(pos, seq, 10.0, D.TimingModel())
    planned_span_h = (caps[-1].t_mid_s - caps[0].t_mid_s) / 3600.0
    for c in caps:                                                              # actual != planned
        c.t_mid_s = c.t_mid_s * 1.35 + (360.0 if c.order > 30 else 0.0)
    inp = build_experiment_campaign(caps, scenario, D.shift_model_from_expected(exp), seed=11, t0_posix=t0, **kw)
    d = tmp_factory.mktemp("l1") / "REDUCE-SYN"
    write_reduce_session_fixture(d, inp)
    _L1_CACHE[key] = (d, inp, planned_span_h, plan)
    return _L1_CACHE[key]


@pytest.fixture(scope="module")
def synthetic_level1(tmp_path_factory):
    return _build_level1("sky_fixed", tmp_path_factory)


def _window(inp):
    """LSRK window around the synthetic feature (channel 1300 of the synthetic axis), as the operator would set it for a real one."""
    return (float(inp.points[0].velocity_lsrk_m_s[1300]), 4000.0)


def _field_campaign(repo, plan_csv_rows_from):
    ctx = make_ctx(repo)
    plan = passed_preflight(ctx, "B")
    r = F.run_experiment(ctx, plan, "B", dry_run=False, yes=True)
    return ctx, plan, r["campaign_id"]


def test_analysis_uses_actual_timestamps_and_handles_repeated_A(repo, synthetic_level1):
    d, inp, planned_span_h, _ = synthetic_level1
    ctx, plan, cid = _field_campaign(repo, None)
    res = F.analyze_campaign(ctx, plan, cid, d, window=_window(inp))
    at = res["actual_timestamps"]
    actual_span_h = (inp.points[-1].t_mid_s - inp.points[0].t_mid_s) / 3600.0
    assert abs(at["span_hours_actual"] - actual_span_h) < 1e-6 and actual_span_h > 1.35 * planned_span_h        # NOT the planned schedule
    assert at["shift_crosscheck_ok"], at["shift_crosscheck_max_abs_m_s"]
    rs = res["reference_series"]
    assert rs["n_points"] == 33 and res["labels"].count("A") == 33                                              # repeated A: 33 distinct captures
    assert abs(rs["span_hours"] - actual_span_h) < 0.05
    assert abs(rs["sky_null_drift_hz_per_hour"] - at["sky_null_slope_hz_per_hour_from_actual"]) < 30.0          # Level 1 == independent, from actual times
    assert res["decision"]["decision"] == "SKY_FIXED"


def test_analysis_prominent_statements_and_vocabulary(repo, synthetic_level1):
    d, inp, *_ = synthetic_level1
    ctx, plan, cid = _field_campaign(repo, None)
    res = F.analyze_campaign(ctx, plan, cid, d, window=_window(inp))
    topics = {s["topic"]: s for s in res["statements"]}
    for t in ("same-sky temporal drift (A)", "cross-sky matched pairs (A-B, A-C)", "candidate shift vs global shift (reference position A)", "frame coherence (forensics V1)", "control features", "classification"):
        assert t in topics
    assert topics["same-sky temporal drift (A)"]["label"] == "CONSISTENT WITH"
    assert topics["cross-sky matched pairs (A-B, A-C)"]["label"] == "CONSISTENT WITH" and "receiver-fixed" in topics["cross-sky matched pairs (A-B, A-C)"]["statement"]
    pl = res["pair_level_test"]
    assert pl["chi2_per_dof_vs_sky_fixed"] < 4 and pl["chi2_per_dof_vs_receiver_fixed"] > 100
    assert res["frame_coherence"] is not None
    assert "known_spurs" in res and "geometry" in res["known_spurs"]["note"] or res["known_spurs"]["n_ranges_tracked"] == 0
    g = topics["candidate shift vs global shift (reference position A)"]["statement"]
    assert "no cause is inferred" in g


def test_analyze_command_writes_outputs_and_records_session(repo, synthetic_level1, capsys):
    d, inp, *_ = synthetic_level1
    ctx, plan, cid = _field_campaign(repo, None)
    w = _window(inp)
    assert F.main(["analyze", cid, "--reduce-session", str(d), "--center", str(w[0]), "--half-width", str(w[1])], ctx=ctx) == 0
    out = repo / F.FIELD_ROOT / cid / "analysis"
    assert (out / "experiment_analysis.json").is_file() and (out / "summary.md").is_file()
    man = json.loads((repo / F.FIELD_ROOT / cid / "manifest.json").read_text())
    assert man["analysis_session"] == str(out)
    assert "same-sky temporal drift" in capsys.readouterr().out


def test_analyze_without_reduce_prints_the_existing_reduce_command(repo, capsys):
    ctx, plan, cid = _field_campaign(repo, None)
    assert F.main(["analyze", cid], ctx=ctx) == 3
    out = capsys.readouterr().out
    assert "almita_reduce.py run data/mosaic/" + cid in out and "--velocity-frame lsrk" in out


def test_quick_diagnostic_fields(repo, synthetic_level1):
    d, inp, *_ = synthetic_level1
    ctx, plan, cid = _field_campaign(repo, None)
    q = F.quick_diagnostic(ctx, cid, d, window=_window(inp))
    assert q["diagnostic_only"] and q["A_repetitions"] == 33 and q["candidate_window_present"]
    for k in ("candidate_median_snr_formal", "candidate_centroid_range_channels", "global_shift_range_channels", "mask_fraction_median"):
        assert k in q


def test_spur_geometry_is_position_only():
    from science_forensics.experiment.synthetic import build_experiment_campaign
    from science_forensics.experiment import design as D
    pos = {"A": D.Position("A", 177.45, -33.449)}
    caps = D.build_schedule(pos, ["A"] * 6, 10.0, D.TimingModel())
    inp = build_experiment_campaign(caps, "receiver_fixed", {"A": (-19600.0, -60.0)}, seed=2)
    for i, p in enumerate(inp.points):
        p.mask[500:506] = 8 if i % 2 else 8                                     # a fixed DC/known-spur mask
    from reduce_engine.models import MaskFlag
    for p in inp.points:
        p.mask[500:506] = MaskFlag.KNOWN_SPUR.value
    r = F.spur_position_stability(inp.points)
    assert r["n_ranges_tracked"] == 1 and r["ranges"][0]["centre_channel_span"] == 0.0 and "masked intensity is invalid" in r["note"]


# ------------------------------------------------------------------ boundaries

def test_wrapper_never_imports_hardware_or_reimplements_capture():
    tree_ = ast.parse((ROOT / "scripts" / "science_forensics_field.py").read_text())
    mods = set()
    for n in ast.walk(tree_):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert not (mods & {"socket", "serial", "indi", "PyIndi", "rtlsdr", "requests", "urllib", "telnetlib"})
    text = (ROOT / "scripts" / "science_forensics_field.py").read_text()
    import re
    assert not re.search(r"signal\.SIG(KILL|TERM)|os\.killpg|\.kill\(\)|\.terminate\(\)", text)
    assert not re.search(r"^\s*(import|from)\s+capture\b", text, re.M)


def test_frozen_surface_unchanged_against_pins():
    r = F.frozen_check(F.Ctx(root=ROOT))
    if r["git_unavailable"]:
        pytest.skip("git unavailable")
    assert r["ok"], r["differs"]


@pytest.mark.parametrize("scenario,kw,decision,pair_label_sky,pair_label_rx", [
    ("receiver_fixed", {}, "RECEIVER_FIXED", "INCONSISTENT WITH", "CONSISTENT WITH"),
    ("candidate_only_drift", {"drift_hz_per_hour": -80_000.0}, "TIME_CANDIDATE_ONLY", None, None)])
def test_analysis_separates_receiver_fixed_and_candidate_only_drift(repo, tmp_path_factory, scenario, kw, decision, pair_label_sky, pair_label_rx):
    d, inp, *_ = _build_level1(scenario, tmp_path_factory, **kw)
    ctx, plan, cid = _field_campaign(repo, None)
    res = F.analyze_campaign(ctx, plan, cid, d, window=_window(inp))
    assert res["decision"]["decision"] == decision
    topics = {s["topic"]: s for s in res["statements"]}
    if pair_label_sky:
        st = topics["cross-sky matched pairs (A-B, A-C)"]["statement"]
        assert st.startswith(pair_label_sky) and f"; {pair_label_rx} receiver-fixed" in st
    else:
        assert topics["same-sky temporal drift (A)"]["label"] == "INCONSISTENT WITH"
        g = topics["candidate shift vs global shift (reference position A)"]["statement"]
        m = __import__("re").search(r"candidate moved (-?[0-9.]+) channels.*whole spectrum moved (-?[0-9.]+) \+/- ([0-9.]+)", g)
        assert m and abs(float(m.group(1))) > 100 and abs(float(m.group(2))) < 3 * max(float(m.group(3)), 0.3)      # "candidate 300 ch, global 0" said exactly
        assert "ratio undefined" in g and "no cause is inferred" in g
