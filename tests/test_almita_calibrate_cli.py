"""CLI smoke tests for almita_calibrate.py - subprocess-free (imports the
module directly and calls its cmd_* functions), consistent with this
repo's own preference for fast, in-process CLI tests. --backend real must
always refuse (Fase 50)."""
import json
from pathlib import Path

import pytest

import almita_calibrate as cli


def _args(**overrides):
    parser = cli.build_parser()
    return parser


def test_audit_runs_and_reports_operational_relative(capsys):
    args = cli.build_parser().parse_args(["audit", "--json"])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["calibration_level"] == "OPERATIONAL_RELATIVE"
    assert out["absolute_calibration"] is False


def test_plan_lists_a_real_gain_table_subset(capsys):
    args = cli.build_parser().parse_args(["plan", "--around-gain", "40.2", "--n-steps", "5", "--json"])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert 40.2 in out["gain_plan_db"]


def test_preflight_ready_for_valid_receiver(tmp_path, capsys):
    args = cli.build_parser().parse_args(["preflight", "--receiver", "MAIN",
                                           "--session-root", str(tmp_path), "--json"])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["result"] == "READY"


def test_preflight_blocked_for_unknown_receiver(tmp_path, capsys):
    args = cli.build_parser().parse_args(["preflight", "--receiver", "NOT_REAL",
                                           "--session-root", str(tmp_path), "--json"])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["result"] == "BLOCKED"


def test_run_backend_real_is_always_refused(tmp_path, capsys):
    args = cli.build_parser().parse_args(["run", "--backend", "real", "--session-root", str(tmp_path), "--json"])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert out["result"] == "REFUSED"
    # confirm nothing was written under session-root by the refusal
    assert not any(tmp_path.iterdir())


def test_run_backend_simulated_produces_a_session_with_result(tmp_path, capsys):
    args = cli.build_parser().parse_args(["run", "--backend", "simulated", "--session-root", str(tmp_path),
                                           "--n-captures", "3", "--capture-seconds", "1.0", "--json"])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["absolute_calibration"] is False
    session_dirs = list(tmp_path.glob("CAL-*"))
    assert len(session_dirs) == 1
    assert (session_dirs[0] / "calibration_result.json").exists()


def test_status_and_result_round_trip_through_a_real_run(tmp_path, capsys):
    run_args = cli.build_parser().parse_args(["run", "--backend", "simulated", "--session-root", str(tmp_path),
                                               "--n-captures", "3", "--capture-seconds", "1.0", "--json"])
    run_args.func(run_args)
    capsys.readouterr()
    session_dir = str(next(tmp_path.glob("CAL-*")))

    status_args = cli.build_parser().parse_args(["status", session_dir, "--json"])
    rc = status_args.func(status_args)
    status = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert status["phase"] == "COMPLETED"

    result_args = cli.build_parser().parse_args(["result", session_dir, "--json"])
    rc = result_args.func(result_args)
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["quality"]["verdict"] in ("GOOD", "MARGINAL", "BAD", "INCONCLUSIVE")


def test_replay_cli_produces_a_new_analysis_directory(tmp_path, capsys):
    run_args = cli.build_parser().parse_args(["run", "--backend", "simulated", "--session-root", str(tmp_path),
                                               "--n-captures", "3", "--capture-seconds", "1.0", "--json"])
    run_args.func(run_args)
    capsys.readouterr()
    session_dir = str(next(tmp_path.glob("CAL-*")))
    replay_args = cli.build_parser().parse_args(["replay", session_dir, "--json"])
    rc = replay_args.func(replay_args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert Path(out["analysis_dir"]).exists()


def test_compare_cli_reports_consistent_for_two_similar_runs(tmp_path, capsys):
    for _ in range(2):
        run_args = cli.build_parser().parse_args(["run", "--backend", "simulated", "--session-root", str(tmp_path),
                                                   "--n-captures", "3", "--capture-seconds", "1.0", "--json"])
        run_args.func(run_args)
        capsys.readouterr()
    session_dirs = sorted(tmp_path.glob("CAL-*"))
    compare_args = cli.build_parser().parse_args(["compare", str(session_dirs[0]), str(session_dirs[1]), "--json"])
    rc = compare_args.func(compare_args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["verdict"] in ("CONSISTENT", "MARGINAL", "INCONSISTENT")


def test_profile_build_cli_writes_a_draft_profile(tmp_path, capsys):
    run_args = cli.build_parser().parse_args(["run", "--backend", "simulated", "--session-root", str(tmp_path),
                                               "--n-captures", "3", "--capture-seconds", "1.0", "--json"])
    run_args.func(run_args)
    capsys.readouterr()
    session_dir = str(next(tmp_path.glob("CAL-*")))
    profiles_dir = tmp_path / "profiles"
    profile_args = cli.build_parser().parse_args(["profile", "build", session_dir,
                                                   "--profiles-dir", str(profiles_dir), "--json"])
    rc = profile_args.func(profile_args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "DRAFT"
    assert out["active"] is False
    assert len(list(profiles_dir.glob("PROFILE-*.json"))) == 1


def test_gain_sweep_dry_run_alias_matches_plan(capsys):
    args = cli.build_parser().parse_args(["gain-sweep", "--around-gain", "40.2", "--n-steps", "5", "--json"])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "gain_plan_db" in out
