"""Mount device selection (operator, 2026-10-17): every page drives ONE mount chosen in the web header - the real
LX200 OnStep (default) or INDI's Telescope Simulator. No hardware: the choice file lives in tmp_path, the INDI server
is tests/support/fake_indi_server.py on a random loopback port."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import almita_web_ops as ops
import mount_device
import observation_orchestrator as orch
import observation_preflight as pf

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def choice(tmp_path, monkeypatch):
    path = tmp_path / "runtime" / "mount_device.json"
    monkeypatch.setattr(mount_device, "STATE_PATH", path)
    return path


# ------------------------------------------------------------------ the stored choice

def test_default_is_the_real_mount_when_nothing_was_chosen(choice):
    assert mount_device.current() == mount_device.LX200 == "LX200 OnStep"
    assert mount_device.describe()["simulator"] is False


@pytest.mark.parametrize("content", ["not json", json.dumps({"device": "Some Other Mount"}), json.dumps(["x"])])
def test_an_unreadable_or_unknown_choice_falls_back_to_the_real_mount(choice, content):
    choice.parent.mkdir(parents=True)
    choice.write_text(content)
    assert mount_device.current() == mount_device.LX200


def test_set_and_read_back(choice):
    assert mount_device.set_device("Telescope Simulator")["simulator"] is True
    assert mount_device.current() == "Telescope Simulator" and mount_device.is_simulator()
    assert mount_device.set_device("LX200 OnStep")["device"] == "LX200 OnStep"
    with pytest.raises(ValueError):
        mount_device.set_device("Telescope Simulatorx")


# ------------------------------------------------------------------ switching from the web

def _no_activity(monkeypatch, busy=None, campaign=None):
    monkeypatch.setattr(ops, "busy_reason", lambda resources=("mount", "sdr"): busy)
    monkeypatch.setattr(orch, "get_status", lambda *a, **k: {"orchestrator": {}})
    monkeypatch.setattr(orch, "active_campaign_reason", lambda o: campaign)


def test_switch_is_refused_while_a_job_holds_the_mount(choice, monkeypatch):
    _no_activity(monkeypatch, busy="mount is in use by align ALIGN-1")
    with pytest.raises(ops.OpsBlocked, match="cannot switch the mount now: mount is in use"):
        ops.set_mount_device("Telescope Simulator")
    assert mount_device.current() == "LX200 OnStep"


def test_switch_is_refused_while_an_observation_is_active(choice, monkeypatch):
    _no_activity(monkeypatch, campaign="capture.py pid 11 is alive")
    with pytest.raises(ops.OpsBlocked, match="an observation is active"):
        ops.set_mount_device("Telescope Simulator")
    assert mount_device.current() == "LX200 OnStep"


def test_switch_when_idle_and_unknown_names_rejected(choice, monkeypatch):
    _no_activity(monkeypatch)
    assert ops.set_mount_device("Telescope Simulator")["device"] == "Telescope Simulator"
    with pytest.raises(ValueError):
        ops.set_mount_device("EQ6")


# ------------------------------------------------------------------ every process that moves the mount gets the choice

@pytest.mark.parametrize("device", ["LX200 OnStep", "Telescope Simulator"])
def test_align_plan_and_lobe_pass_the_selected_device(choice, monkeypatch, device):
    mount_device.set_device(device)
    argv, _ = ops.build_command("align_plan", {"reference": "hi"}, "J1")
    assert argv[argv.index("--device") + 1] == device
    monkeypatch.setattr(ops, "lobe_alignment", lambda: {"ok": True, "detail": "x"})
    argv, meta = ops.build_command("lobe", {"mode": "drift"}, "J2")
    assert argv[argv.index("--device") + 1] == device and meta["mount_device"] == device


def test_wizard_hi_capture_and_gain_pilot_capture_pass_the_selected_device(choice, tmp_path, monkeypatch):
    mount_device.set_device("Telescope Simulator")
    cal = tmp_path / "calibration"; (cal / "WIZ").mkdir(parents=True)
    mosaic = tmp_path / "mosaic"; (mosaic / "GRID").mkdir(parents=True)
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    monkeypatch.setitem(ops.SERVE_ROOTS, "calibration", cal)
    monkeypatch.setitem(ops.SERVE_ROOTS, "mosaic", mosaic)
    argv, meta = ops.build_command("calibrate_wizard_move", {"label": "HI_ALTO", "session_dir": "calibration/WIZ"}, "J3")
    assert argv[argv.index("--device") + 1] == "Telescope Simulator" and meta["mount_device"] == "Telescope Simulator"
    argv, meta = ops.build_command("observe_gain_pilot_capture", {"action": "capture_high", "session_dir": "mosaic/GRID"}, "J4")
    assert argv[argv.index("--device") + 1] == "Telescope Simulator"


def test_the_wizard_and_pilot_clis_accept_device_and_default_to_the_real_mount():
    import calibrate_reference_wizard as wiz
    import observation_gain_pilot as pilot
    for parser, cmd in ((wiz.build_parser(), ["capture-hi", "--session-dir", "x", "--label", "HI_ALTO"]),
                        (pilot.build_parser(), ["capture-high", "--session-dir", "x"]),
                        (pilot.build_parser(), ["final-check", "--session-dir", "x"])):
        assert parser.parse_args(cmd).device == "LX200 OnStep"
        assert parser.parse_args(cmd + ["--device", "Telescope Simulator"]).device == "Telescope Simulator"


def test_sync_is_blocked_with_the_simulator_before_anything_else(choice):
    mount_device.set_device("Telescope Simulator")
    with pytest.raises(ValueError, match="SYNC is not allowed with the Telescope Simulator"):
        ops.build_command("align", {"reference": "sun", "sync": True}, "J5")
    # RUN without SYNC is not refused for that reason (it fails later, on the missing approved PLAN)
    with pytest.raises(ValueError) as exc:
        ops.build_command("align", {"reference": "sun"}, "J6")
    assert "SYNC" not in str(exc.value)


def test_observation_capture_gets_the_device_only_when_given():
    plan = {"mosaic_csv_path": "m.csv", "requested": {"capture": {"settle_seconds": 1, "seconds": 2}, "grid": {"min_altitude_deg": 20}},
            "main": {"center_frequency_hz": 1, "sample_rate": 2, "gain_db": 3}, "rfi_ref": {"enabled": False}}
    assert "--device" not in orch._capture_args(plan, "/runtime")
    args = orch._capture_args(plan, "/runtime", "Telescope Simulator")
    assert args[args.index("--device") + 1] == "Telescope Simulator"


# ------------------------------------------------------------------ OnStep-only checks do not apply to the simulator

def test_simulator_rest_state_needs_no_onstep_status():
    base = {"connected": True, "eod_state": "Idle", "parked": False, "mount_state": None, "onstep_error": None}
    assert ops.mount_idle_problems({**base, "simulator": True}) == []
    assert ops.mount_idle_problems({**base, "simulator": True, "eod_state": "Busy"}) == ["mount coordinates state Busy"]
    assert ops.mount_idle_problems({**base, "simulator": True, "parked": True}) == ["mount is parked"]
    # the real mount keeps the OnStep whitelist: no status reported is still a block
    assert any("not a known rest state" in p for p in ops.mount_idle_problems({**base, "simulator": False}))


def test_onstep_time_snapshot_is_not_applicable_to_the_simulator():
    c = asyncio.run(pf._onstep_time_snapshot_check("127.0.0.1", 1, "Telescope Simulator"))
    assert c["status"] == "PASS" and "not applicable" in c["detail"]


# ------------------------------------------------------------------ read_mount names the device it read

def test_read_mount_reads_the_simulator_by_name(tmp_path):
    server = ROOT / "tests" / "support" / "fake_indi_server.py"
    proc = subprocess.Popen([sys.executable, str(server), "--port", "0", "--journal", str(tmp_path / "j.jsonl"),
                             "--device", "Telescope Simulator"], stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline().split()[1])
        m = ops.read_mount(port=port, device="Telescope Simulator")
        assert m["device"] == "Telescope Simulator" and m["simulator"] is True and m["ra_h"] is not None
        other = ops.read_mount(port=port, device="LX200 OnStep", timeout=0.5)
        assert other["simulator"] is False and "error" in other           # a mount that is not on this server is not invented
    finally:
        proc.terminate(); proc.wait(timeout=5)


def test_observe_plan_preflight_probes_the_selected_mount(choice, monkeypatch):
    """2026-10-17: PLAN's preflight looked for LX200 OnStep (the session observer_config) while the simulator was
    selected - BLOCK "failed to connect". It now probes the mount selected in the header, like START."""
    import observation_plan
    seen = {}

    async def fake(plan, **kw):
        seen.update(kw)
        return {"checks": [], "overall": "PASS", "generated_utc": "t"}
    monkeypatch.setattr(pf, "run_plan_preflight", fake)
    mount_device.set_device("Telescope Simulator")
    observation_plan._plan_time_preflight({})
    assert seen["device_name"] == "Telescope Simulator"


def test_alignment_stop_ends_by_sigint_without_a_traceback():
    code = ("import alignment\n"
            "def boom(argv=None): raise KeyboardInterrupt()\n"
            "alignment.main = boom\n"
            "alignment._main_cli()\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == -2                                            # still ends BY SIGINT, as the web expects
    assert "STOPPED by the operator (SIGINT)" in r.stdout and "Traceback" not in r.stderr


# ------------------------------------------------------------------ capture.py's OnStep gates with the simulator

def _bare_executor(device, telescope, timeout=2.0):
    import capture
    ex = object.__new__(capture.CaptureExecutor)          # only what the gates read; no CSV, no session, no SDR
    ex.device_name, ex.telescope, ex.tracking_timeout = device, telescope, timeout
    return ex


def test_capture_gate_with_the_simulator_reads_its_coordinates_and_opens(tmp_path):
    """2026-10-17: every OBSERVE with the simulator paused before the first GOTO (no "OnStep Status" ->
    onstep_status_hardware_timeout). The simulator gate asks for the coordinates and opens on a fresh answer."""
    import capture
    from indi_telescope_control import INDITelescopeControl
    server = ROOT / "tests" / "support" / "fake_indi_server.py"
    proc = subprocess.Popen([sys.executable, str(server), "--port", "0", "--journal", str(tmp_path / "j.jsonl"),
                             "--device", "Telescope Simulator"], stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline().split()[1])

        async def go():
            t = INDITelescopeControl("127.0.0.1", port, "Telescope Simulator", False)
            assert await t.connect()
            try:
                ex = _bare_executor("Telescope Simulator", t)
                return [await ex.read_onstep_status(retry_unknown=True) for _ in range(2)]
            finally:
                await t.disconnect()
        first, second = asyncio.run(go())
    finally:
        proc.terminate(); proc.wait(timeout=5)
    for st in (first, second):                                  # each gate gets its own fresh answer
        assert st["source"] == "telescope_simulator_eod" and st["reason"] == "not_applicable_simulator"
        assert capture.CaptureExecutor.onstep_status_allows_operation(st) is True
    assert second["update_seq"] > first["update_seq"]


class _SilentTelescope:
    """A driver that never answers: the simulator gate must stay closed (unknown), like OnStep's."""
    def __init__(self):
        self._property_cache, self.sent, self.onstep_waits = {}, [], 0

    async def _send_command(self, xml):
        self.sent.append(xml)

    async def _wait_property(self, name, timeout, after_seq=None, predicate=None):
        raise asyncio.TimeoutError

    @staticmethod
    def _unknown_onstep_status(reason):
        from indi_telescope_control import INDITelescopeControl
        return INDITelescopeControl._unknown_onstep_status(reason)

    async def wait_onstep_status_update(self, timeout):
        self.onstep_waits += 1
        return self._unknown_onstep_status("timeout")


def test_capture_gate_with_a_silent_simulator_stays_closed():
    import capture
    t = _SilentTelescope()
    st = asyncio.run(_bare_executor("Telescope Simulator", t, 0.1).read_onstep_status(retry_unknown=True))
    assert st["state"] == "unknown" and st["reason"] == "simulator_no_coordinates"
    assert capture.CaptureExecutor.onstep_status_allows_operation(st) is False
    assert len(t.sent) == 2 and t.onstep_waits == 0                   # asked twice (retry), never waited for OnStep


def test_capture_gate_with_the_real_mount_still_requires_onstep_status():
    t = _SilentTelescope()
    st = asyncio.run(_bare_executor("LX200 OnStep", t, 0.1).read_onstep_status(retry_unknown=True))
    assert t.onstep_waits == 2 and t.sent == []                      # unchanged OnStep path, no simulator request
    assert st["reason"] == "onstep_status_hardware_timeout"


# ------------------------------------------------------------------ MAIN LNA power from STATUS (2026-10-17)
def test_lna_switch_is_refused_while_main_is_in_use(monkeypatch):
    import sdr_tuning
    called = []
    monkeypatch.setattr(sdr_tuning, "set_main_bias_tee", lambda on: called.append(on))
    _no_activity(monkeypatch, busy="sdr is in use by align ALIGN-1")
    with pytest.raises(ops.OpsBlocked, match="cannot switch the LNA now: sdr is in use"):
        ops.set_lna_power(False)
    _no_activity(monkeypatch, campaign="capture.py pid 11 is alive")
    with pytest.raises(ops.OpsBlocked, match="an observation is active"):
        ops.set_lna_power(False)
    assert called == []


def test_lna_switch_when_idle_reports_rtl_tcp_own_state(monkeypatch):
    import sdr_tuning
    import almita_web_common
    _no_activity(monkeypatch)

    class Free:
        class status: value = "FREE"
        detail = "free"
    monkeypatch.setattr(almita_web_common, "get_sdr_resource_status", lambda: Free)
    monkeypatch.setattr(sdr_tuning, "set_main_bias_tee", lambda on: None)
    monkeypatch.setattr(sdr_tuning, "main_bias_tee_state", lambda unit=None: {"state": "off", "since_utc": "t", "source": "client command"})
    monkeypatch.setattr(sdr_tuning, "main_service_has_bias_tee_flag", lambda unit=None: True)
    st = ops.set_lna_power(False)
    assert st["state"] == "off" and st["service_has_T"] is True
