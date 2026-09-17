"""CLI tests (Fase 14): thin wrapper only - every assertion here is about
argument wiring/output shape, not science (that's covered by the
alignment_engine test files). Runs the real script via subprocess, matching
this repo's existing CLI-test convention (see test_almita_system_blackbox.py).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.resolve()
PYTHON = str(ROOT / ".venv" / "bin" / "python")


def _run(args, cwd=None):
    return subprocess.run([PYTHON, "almita_align.py", *args], cwd=cwd or ROOT,
                           capture_output=True, text=True, timeout=30)


def _config_file(tmp_path) -> str:
    config_path = tmp_path / "align_config.json"
    config_path.write_text(json.dumps({
        "global": {"output_root": str(tmp_path / "alignment"),
                   # isolate from this machine's real data/runtime/ state
                   "orchestrator_runtime_dir": str(tmp_path / "orchestrator_runtime")},
        "solar": {"min_altitude_deg": -90.0, "coarse_span_deg": 14.0, "coarse_spacing_deg": 4.0,
                  "fine_span_deg": 5.0, "fine_spacing_deg": 1.5},
        "hi": {"min_altitude_deg": -90.0, "raster_span_deg": 12.0, "raster_spacing_deg": 3.0},
    }))
    return str(config_path)


def _plan(tmp_path, mode="solar") -> str:
    result = _run([mode, "plan", "--config", _config_file(tmp_path), "--json"])
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["session_id"]


def test_solar_plan_returns_valid_json_with_session_id(tmp_path):
    result = _run(["solar", "plan", "--config", _config_file(tmp_path), "--json"])
    assert result.returncode == 0
    payload = json.loads(result.stdout)  # must be pure JSON, no mixed human text
    assert payload["session_id"].startswith("SOLAR-")
    assert payload["state"] == "PLANNED"


def test_solar_plan_human_output_is_not_json(tmp_path):
    result = _run(["solar", "plan", "--config", _config_file(tmp_path)])
    assert result.returncode == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
    assert "Session planned" in result.stdout


def test_preflight_pass_then_run_then_result_full_solar_flow(tmp_path):
    config = _config_file(tmp_path)
    session_id = _plan(tmp_path, "solar")

    preflight = _run(["solar", "preflight", session_id, "--config", config, "--json"])
    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout)["ok"] is True

    run = _run(["solar", "run", session_id, "--simulate",
                "--true-offset-east", "1.2", "--true-offset-north", "-0.7",
                "--noise", "0.02", "--seed", "7", "--config", config, "--json"])
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["mode"] == "SOLAR"
    assert result["applied_sync"] is False
    assert abs(result["offset_ra_deg"] - 1.2) < 0.8
    assert abs(result["offset_dec_deg"] - (-0.7)) < 0.8

    fetched = _run(["result", session_id, "--session-root", str(tmp_path / "alignment"), "--json"])
    assert fetched.returncode == 0
    assert json.loads(fetched.stdout)["mode"] == "SOLAR"


def test_run_without_simulate_refuses_hardware_path(tmp_path):
    config = _config_file(tmp_path)
    session_id = _plan(tmp_path, "solar")
    _run(["solar", "preflight", session_id, "--config", config, "--json"])
    result = _run(["solar", "run", session_id, "--config", config])
    assert result.returncode == 3
    assert "hardware" in result.stderr.lower()


def test_hi_flow_recovers_offset_and_blocks_sync_as_non_observational(tmp_path):
    config = _config_file(tmp_path)
    session_id = _plan(tmp_path, "hi")
    _run(["hi", "preflight", session_id, "--config", config, "--json"])
    run = _run(["hi", "run", session_id, "--simulate",
                "--true-offset-east", "-0.8", "--true-offset-north", "1.5",
                "--gain", "2.3", "--baseline", "5.0", "--noise", "0.02", "--seed", "11",
                "--config", config, "--json"])
    assert run.returncode == 0, run.stderr

    sync = _run(["sync", session_id, "--session-root", str(tmp_path / "alignment"),
                 "--config", config, "--json"])
    plan = json.loads(sync.stdout)
    assert plan["eligible"] is False
    assert "not observational" in plan["eligibility_reason"]


def test_sync_apply_never_asks_for_input_when_dashdash_yes_given(tmp_path):
    """The CLI must never depend on stdin unless the operator omits --yes -
    with --yes it must complete without blocking on a prompt."""
    config = _config_file(tmp_path)
    session_id = _plan(tmp_path, "solar")
    _run(["solar", "preflight", session_id, "--config", config, "--json"])
    _run(["solar", "run", session_id, "--simulate", "--true-offset-east", "0.3",
          "--true-offset-north", "0.1", "--noise", "0.01", "--seed", "3", "--config", config, "--json"])
    result = subprocess.run(
        [PYTHON, "almita_align.py", "sync", session_id, "--session-root", str(tmp_path / "alignment"),
         "--config", config, "--apply", "--yes", "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["applied"] is True


def test_sync_without_apply_never_touches_a_mount_and_is_reentrant(tmp_path):
    """Calling `sync` (preview) twice in a row must not blow up the state
    machine - Fase 14's own two-invocation design (preview, then apply)."""
    config = _config_file(tmp_path)
    session_id = _plan(tmp_path, "solar")
    _run(["solar", "preflight", session_id, "--config", config, "--json"])
    _run(["solar", "run", session_id, "--simulate", "--true-offset-east", "0.3",
          "--true-offset-north", "0.1", "--noise", "0.01", "--seed", "3", "--config", config, "--json"])
    first = _run(["sync", session_id, "--session-root", str(tmp_path / "alignment"), "--config", config, "--json"])
    second = _run(["sync", session_id, "--session-root", str(tmp_path / "alignment"), "--config", config, "--json"])
    assert first.returncode == 0 and second.returncode == 0


def test_status_reports_state_progression(tmp_path):
    config = _config_file(tmp_path)
    session_id = _plan(tmp_path, "solar")
    status = _run(["status", session_id, "--session-root", str(tmp_path / "alignment"), "--json"])
    assert json.loads(status.stdout)["state"] == "PLANNED"


def test_result_for_unknown_session_fails_cleanly_not_a_traceback(tmp_path):
    result = _run(["result", "NOT-A-REAL-SESSION", "--session-root", str(tmp_path / "alignment")])
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
