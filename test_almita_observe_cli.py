"""Tests for almita_observe.py: the CLI is a thin wrapper around the same core
functions the Web API uses. observation_plan._plan_time_preflight is stubbed
out to keep this suite offline/fast (real preflight is covered by
test_observation_preflight.py; the real hardware probe itself is exercised
manually during real-hardware validation).
"""
import json

import pytest
import yaml

import almita_observe as cli
import grid_generator
import observation_orchestrator as orch
import observation_plan


@pytest.fixture(autouse=True)
def _fast_preflight(monkeypatch):
    monkeypatch.setattr(observation_plan, "_plan_time_preflight",
                         lambda resolved_plan: {"overall": "PASS", "checks": [], "generated_utc": "now"})
    # grid_generator.py's real matplotlib rendering (grid_plan.png/grid_coverage.png)
    # is expensive and already covered for real by test_observation_plan.py's
    # single heavy fixture; this Pi has ~0 free swap and stacking more real
    # renders across test files was observed to OOM-kill the combined run.
    # Everything else in plan_observation() (resolution, CSV, hash, JSON) stays real.
    monkeypatch.setattr(grid_generator.GridGenerator, "_write_plot_images", lambda self, points, metadata: None)


def _write_yaml(tmp_path, **overrides):
    spec = {
        "session": {"name": "CLITEST"},
        "grid": {"mode": "EQUATORIAL_RECT", "placement": "FIXED_CENTER", "center_ra_hours": 6.0,
                 "center_dec_deg": -30.0, "width_deg": 4, "height_deg": 4, "rows": 3, "cols": 3,
                 "min_altitude_deg": 5, "traversal": "SERPENTINE"},
        "capture": {"seconds": 1, "settle_seconds": 0.5},
        "main": {"center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 40.2, "bias_tee": True},
        "rfi_ref": {"enabled": False, "serial": "00000002", "gain_db": 25.0},
        "quicklook": {"enabled": False, "native_grid": True, "interpolated_preview": False,
                      "calibration_profile_path": None},
        "console": {"enabled": True},
        "execution": {"unattended": True},
    }
    for section, patch in overrides.items():
        spec[section].update(patch)
    path = tmp_path / "obs.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def test_cmd_plan_happy_path_then_validate(tmp_path, capsys):
    # Combined into one test (instead of a separate validate test that would
    # call cmd_plan a second time): this Pi has ~0 free swap and stacking
    # more than one real plan_observation() call (grid_generator.py's
    # matplotlib rendering) per pytest process was observed to OOM-kill it.
    yaml_path = _write_yaml(tmp_path)
    plan_args = cli.argparse.Namespace(observation_yaml=str(yaml_path), config="observer_config.json",
                                        data_dir=str(tmp_path / "mosaic"))
    rc = cli.cmd_plan(plan_args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "PLAN VALID. OPERATOR GO REQUIRED." in out
    assert "NO HARDWARE HAS MOVED." in out
    assert "Resolved plan:" in out

    rc = cli.cmd_validate(cli.argparse.Namespace(path=str(yaml_path)))
    assert rc == 0
    assert "VALID: observation spec" in capsys.readouterr().out

    resolved_path = list((tmp_path / "mosaic").glob("*/observation_resolved.json"))[0]
    rc = cli.cmd_validate(cli.argparse.Namespace(path=str(resolved_path)))
    assert rc == 0
    assert "VALID: resolved plan hash matches" in capsys.readouterr().out

    tampered = json.loads(resolved_path.read_text())
    tampered["resolved"]["center_ra_hours"] = 99.0
    resolved_path.write_text(json.dumps(tampered))
    rc = cli.cmd_validate(cli.argparse.Namespace(path=str(resolved_path)))
    assert rc == 1
    assert "INVALID" in capsys.readouterr().out


def test_cmd_plan_invalid_spec_returns_nonzero(tmp_path, capsys):
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("session: {}\n")
    args = cli.argparse.Namespace(observation_yaml=str(yaml_path), config="observer_config.json",
                                   data_dir=str(tmp_path / "mosaic"))
    rc = cli.cmd_plan(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "INVALID SPEC" in out


def test_cmd_plan_blocked_visibility_returns_nonzero(tmp_path, capsys):
    yaml_path = _write_yaml(tmp_path, grid={"center_dec_deg": 80.0, "min_altitude_deg": 45})
    args = cli.argparse.Namespace(observation_yaml=str(yaml_path), config="observer_config.json",
                                   data_dir=str(tmp_path / "mosaic"))
    rc = cli.cmd_plan(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out


def test_cmd_status_reads_runtime(tmp_path, capsys, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    orch._write_runtime(str(runtime_dir), orchestrator_state="READY")
    rc = cli.cmd_status(cli.argparse.Namespace(runtime_dir=str(runtime_dir)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "READY" in out


def test_cmd_stop_requires_typed_confirmation_without_yes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "input", lambda prompt: "nope", raising=False)
    rc = cli.cmd_stop(cli.argparse.Namespace(yes=False, runtime_dir=str(tmp_path / "runtime")))
    out = capsys.readouterr().out
    assert rc == 1
    assert "aborted" in out


def test_cmd_report_happy_path(tmp_path, capsys, monkeypatch):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "mosaic.csv").write_text("capture_status,visibility_deferred,data_filename\nsuccess,False,a.dat\n")
    (session_dir / "observation_resolved.json").write_text(json.dumps({
        "observation_name": "X", "observation_config_sha256": "abc", "requested": {}, "resolved": {},
    }))
    monkeypatch.setattr(orch, "validate_hdf5_capture", lambda path: {"ok": True})
    monkeypatch.setattr(orch, "_read_runtime", lambda runtime_dir: {"orchestrator_state": "COMPLETED"})
    rc = cli.cmd_report(cli.argparse.Namespace(session_dir=str(session_dir)))
    out = capsys.readouterr().out
    assert rc == 0
    assert '"final_verdict": "SUCCESS"' in out
