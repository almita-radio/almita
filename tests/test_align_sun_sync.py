"""ALIGN SUN SYNC button: the web layer runs scripts/mount/manual_sun_sync.py as its own process (a SYNC claims the
mount lock until the process exits) and the script refuses on every guard. No INDI, no mount: all faked here."""
import asyncio
import importlib.util
import json
import types
from pathlib import Path

import pytest

import almita_web_align

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("manual_sun_sync", ROOT / "scripts" / "mount" / "manual_sun_sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_apply_needs_an_explicit_confirm():
    with pytest.raises(ValueError):
        almita_web_align.sun_sync_apply(None)
    with pytest.raises(ValueError):
        almita_web_align.sun_sync_apply("yes")


def test_the_web_runs_the_script_as_its_own_process_and_reads_its_last_json_line(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return types.SimpleNamespace(returncode=0, stdout="INDI log line\n" + json.dumps({"ok": False, "refused": "the simulator is selected"}) + "\n", stderr="")
    monkeypatch.setattr("subprocess.run", fake_run)
    r = almita_web_align.sun_sync_apply(True)
    assert r == {"ok": False, "refused": "the simulator is selected"}
    assert seen["argv"][1].endswith("manual_sun_sync.py") and seen["argv"][2:5] == ["sync", "--yes", "--json"]
    assert seen["argv"][-1] == str(almita_web_align.SUN_SYNC_MAX_SEP_DEG)


def test_a_script_without_json_output_is_an_error(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback: boom"))
    with pytest.raises(RuntimeError, match="boom"):
        almita_web_align.sun_sync_check()


def _state(device="LX200 OnStep", sep=2.0):
    return {"utc": "x", "device": device, "mount": {"ra_h": 13.0, "dec_deg": -10.0, "error": None}, "sun_eod": {"ra_h": 13.1, "dec_deg": -10.0},
            "separation_deg": sep}


@pytest.mark.parametrize("device,busy,holder,sep,why", [
    ("Telescope Simulator", [], None, 1.0, "simulator"),
    ("LX200 OnStep", ["an observation is active: orchestrator RUNNING"], None, 1.0, "observation is active"),
    ("LX200 OnStep", [], {"pid": 1, "what": "GOTO"}, 1.0, "mount lock held"),
    ("LX200 OnStep", [], None, 25.0, "pier side"),
])
def test_the_script_refuses_before_touching_indi(monkeypatch, device, busy, holder, sep, why):
    m = _script()
    import session_deletion
    monkeypatch.setattr(m, "state", lambda: _state(device, sep))
    monkeypatch.setattr(m.mount_device, "is_simulator", lambda d: "Simulator" in d)
    monkeypatch.setattr(session_deletion, "default_busy_probe", lambda: busy)
    monkeypatch.setattr(m.mount_lock, "holder", lambda: holder)
    monkeypatch.setattr(m, "INDITelescopeControl", lambda *a, **k: pytest.fail("INDI must not be touched"))
    with pytest.raises(m.Refused, match=why):
        asyncio.run(m.do_sync(10.0, True))
