"""CLI REGRESSION (2nd-pass sections 53-54): inspect/plan/run/status/
replay/compare/validate all still work, and exit codes are correct
(0=success, nonzero=blocked/failed/invalid input) - so external scripts
can automate REDUCE without depending on parsing human text. Uses only
fast/cheap invocations (inspect/plan/status on real data; deliberately
invalid inputs for exit-code checks) - never a full expensive run here,
that is covered by the other real-campaign test files.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SMALL = "data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16"
PYTHON = sys.executable


def _run(*args):
    result = subprocess.run([PYTHON, "almita_reduce.py", *args], capture_output=True, text=True, timeout=60)
    return result.returncode, result.stdout, result.stderr


def test_status_exits_zero_and_needs_no_campaign():
    code, out, _ = _run("status", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["hardware"] == "NONE - offline only"


def test_inspect_real_campaign_exits_zero():
    if not Path(SMALL).is_dir():
        pytest.skip("real fixture campaign not present")
    code, out, _ = _run("inspect", SMALL, "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["points_accepted"] == 9


def test_inspect_missing_campaign_directory_fails_loudly_not_silently():
    code, out, err = _run("inspect", "data/mosaic/DEFINITELY-NOT-A-REAL-CAMPAIGN", "--json")
    assert code != 0


def test_plan_real_campaign_exits_zero_when_not_blocked(tmp_path):
    if not Path(SMALL).is_dir():
        pytest.skip("real fixture campaign not present")
    code, out, _ = _run("plan", SMALL, "--output-root", str(tmp_path), "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["blocked"] is False
    assert payload["estimated_output_bytes"] > 0


def test_disk_estimate_is_within_10_percent_of_a_real_measured_run():
    """Cross-check against this pass's own real 100-point performance
    measurement (~40.5 MB at fft_size=8192) - not just a plausible-looking
    formula."""
    from reduce_engine.config import ReduceConfig
    from reduce_engine.validation import estimate_output_bytes
    estimated = estimate_output_bytes(ReduceConfig(), 100)
    measured = 40_473_492
    assert 0.9 < estimated / measured < 1.1


def test_run_missing_campaign_exits_nonzero():
    code, out, err = _run("run", "data/mosaic/DEFINITELY-NOT-A-REAL-CAMPAIGN", "--json")
    assert code != 0


def test_validate_missing_session_dir_exits_nonzero():
    code, out, err = _run("validate", "/tmp/definitely-not-a-real-reduce-session", "--json")
    assert code == 1
    payload = json.loads(out)
    assert payload["output_integrity"]["ok"] is False


def test_compare_missing_sessions_exits_nonzero_not_crash_traceback_only():
    code, out, err = _run("compare", "/tmp/not-real-a", "/tmp/not-real-b")
    assert code != 0  # argparse/exception -> nonzero, never silently 0


def test_replay_missing_session_dir_exits_nonzero():
    code, out, err = _run("replay", "/tmp/not-a-real-session")
    assert code != 0


def test_cli_has_no_hardware_flags():
    """Section 70: no gain/frequency/Bias-T/mount flags anywhere in the
    CLI - confirms the help text stays free of hardware control."""
    code, out, _ = _run("--help")
    assert code == 0
    forbidden = ["--gain", "--frequency", "--bias-t", "--goto", "--sync", "--mount"]
    for flag in forbidden:
        assert flag not in out
