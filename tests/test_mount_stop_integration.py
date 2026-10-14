"""mount_stop.stop_mount_and_confirm() against a real INDI socket server whose simulated mount keeps moving and
tracking (tests/support/fake_indi_server.py), driven by the real INDITelescopeControl. Nothing of the stop logic is
replaced. No hardware, no data/: the server is a subprocess on a random loopback port."""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import mount_stop
from indi_telescope_control import INDITelescopeControl

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "tests" / "support" / "fake_indi_server.py"


SERVERS: dict = {}


class FakeIndi:
    def __init__(self, tmp_path, *flags):
        self.journal = tmp_path / "indi.jsonl"
        self.proc = subprocess.Popen([sys.executable, str(SERVER), "--port", "0", "--journal", str(self.journal), *flags],
                                     stdout=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline()
        assert line.startswith("LISTENING"), line
        self.port = int(line.split()[1])
        SERVERS[self.port] = self

    def commands(self, since: int = 0):
        """Commands (newXVector) the server received, from index `since` on - an index, not a time: the stop
        follows the GOTO command within milliseconds."""
        if not self.journal.exists():
            return []
        rows = [json.loads(x) for x in self.journal.read_text().splitlines() if x.strip()]
        return [r for r in rows if r["kind"] == "new"][since:]

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


@pytest.fixture
def indi(tmp_path):
    servers = []

    def start(*flags):
        s = FakeIndi(tmp_path, *flags)
        servers.append(s)
        return s
    yield start
    for s in servers:
        s.close()


async def _telescope(port):
    t = INDITelescopeControl("127.0.0.1", port, "Telescope Simulator", False)
    t.compact_console = True
    assert await t.connect()
    return t


async def _goto_in_background(t, ra=6.5, dec=-45.0):
    """Starts a real GOTO and returns once the mount reports Busy - the moment a STOP arrives mid-slew."""
    task = asyncio.create_task(t.goto(ra, dec))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        item = t._property_cache.get((t.device_name, "EQUATORIAL_EOD_COORD"))
        if item and item["state"] == "Busy":
            return task
        await asyncio.sleep(0.05)
    raise AssertionError("the simulated mount never started slewing")


async def _stop_mid_slew(port, **kwargs):
    t = await _telescope(port)
    try:
        task = await _goto_in_background(t)
        await asyncio.sleep(0.3)                        # let the server journal the GOTO command itself
        stopped_at = len(SERVERS[port].commands())      # everything from here on was sent by the stop
        task.cancel()                                   # what SIGINT does to capture.py's GOTO
        with pytest.raises(asyncio.CancelledError):
            await task
        report = await mount_stop.stop_mount_and_confirm(t, **kwargs)
        return report, stopped_at
    finally:
        await t.disconnect()


def _no_new_movement(server, since):
    names = [c["name"] for c in server.commands(since)]
    assert "EQUATORIAL_EOD_COORD" not in names, "a new GOTO/SYNC was commanded after the stop"
    assert "TELESCOPE_PARK" not in names and "TELESCOPE_HOME" not in names
    return names


def test_stop_during_a_goto_aborts_the_slew_and_tracking_ends_off_despite_the_late_reactivation(indi):
    """The 2026-10-13 failure: tracking was ON before the GOTO; the mount restores it when the slew ends."""
    server = indi("--start-tracking", "--slew-seconds", "20")
    report, since = asyncio.run(_stop_mid_slew(server.port))
    assert report["confirmed"], report["reason"]
    assert report["abort"] == {"supported": True, "sent": True}
    assert report["motion_stopped"] and report["tracking_off"] and report["stillness"]["verdict"] == "STATIONARY"
    assert report["duration_s"] < 20                     # the 20 s slew was cut short, not waited for
    names = _no_new_movement(server, since)
    assert "TELESCOPE_ABORT_MOTION" in names and "TELESCOPE_TRACK_STATE" in names


def test_without_an_abort_property_the_slew_is_waited_for_then_tracking_is_switched_off(indi):
    server = indi("--start-tracking", "--slew-seconds", "4", "--no-abort")
    report, since = asyncio.run(_stop_mid_slew(server.port))
    assert report["confirmed"], report["reason"]
    assert report["abort"] == {"supported": False, "sent": False}
    assert report["stillness"]["verdict"] == "STATIONARY"
    _no_new_movement(server, since)


def test_an_abort_that_does_not_take_falls_back_to_waiting_the_slew_out(indi):
    server = indi("--start-tracking", "--slew-seconds", "5", "--abort-ignored")
    report, _ = asyncio.run(_stop_mid_slew(server.port, abort_settle_timeout_s=1.5))
    assert report["confirmed"], report["reason"]
    assert any(s["step"] == "abort" and s["ok"] is False for s in report["steps"])


def test_tracking_switched_on_again_after_the_stop_is_caught_and_switched_off_again(indi):
    """Late activation: the mount reports not-Busy, tracking is switched OFF, and THEN something turns it back on."""
    server = indi("--slew-seconds", "1")

    async def scenario():
        t = await _telescope(server.port)
        try:
            await t.set_tracking(True)

            async def late_on():
                await asyncio.sleep(2.2)                 # between two of the OFF confirmation reads
                await t._send_command('<newSwitchVector device="Telescope Simulator" name="TELESCOPE_TRACK_STATE">'
                                      '<oneSwitch name="TRACK_ON">On</oneSwitch><oneSwitch name="TRACK_OFF">Off</oneSwitch></newSwitchVector>')
            asyncio.create_task(late_on())
            return await mount_stop.stop_mount_and_confirm(t)
        finally:
            await t.disconnect()
    report = asyncio.run(scenario())
    assert report["confirmed"], report["reason"]
    assert report["tracking_reactivations"] >= 1


@pytest.mark.parametrize("flags,expected", [
    (("--start-tracking", "--ignore-track-off"), "tracking is on after"),
    (("--start-tracking", "--lie-track-off-after-stop"), "still following the sky"),
    (("--start-tracking", "--freeze-after-stop"), "INDI did not answer"),
    (("--start-tracking", "--drop-after-stop"), ""),
])
def test_a_stop_that_cannot_be_proved_is_reported_as_not_confirmed_with_its_reason(indi, flags, expected):
    server = indi("--slew-seconds", "20", *flags)
    report, since = asyncio.run(_stop_mid_slew(server.port))
    assert report["confirmed"] is False and report["reason"]
    assert expected in report["reason"], report["reason"]
    assert mount_stop.summary_line(report).startswith("PARADA NO CONFIRMADA")
    _no_new_movement(server, since)


def test_a_slew_that_never_ends_is_not_confirmed_after_the_time_limit(indi):
    server = indi("--start-tracking", "--slew-seconds", "60", "--no-abort")
    report, _ = asyncio.run(_stop_mid_slew(server.port, motion_timeout_s=3.0))
    assert report["confirmed"] is False and report["motion_stopped"] is False
    assert "still slewing after 3 s" in report["reason"]


def test_no_indi_connection_is_not_confirmed():
    report = asyncio.run(mount_stop.stop_mount_and_confirm(None))
    assert report["confirmed"] is False and "no INDI connection" in report["reason"]


def test_near_the_pole_the_ra_check_is_advisory(indi):
    server = indi("--start-dec", "-89.9", "--pinned", "--slew-seconds", "1")

    async def scenario():
        t = await _telescope(server.port)
        try:
            return await mount_stop.stop_mount_and_confirm(t)
        finally:
            await t.disconnect()
    report = asyncio.run(scenario())
    assert report["confirmed"] and report["stillness"]["verdict"] == "NOT_ASSESSABLE_NEAR_POLE"


# ------------------------------------------------------------------ the web's PARAR MONTURA (almita_web_ops.stop_mount)
def test_web_stop_mount_stops_a_tracking_mount_and_reports_the_readback(indi, tmp_path, monkeypatch):
    import almita_web_ops as ops
    server = indi("--start-tracking", "--slew-seconds", "1")
    monkeypatch.setattr(ops, "list_jobs", lambda limit=40: [])            # never the operator's real job list
    result = ops.stop_mount(runtime_dir=str(tmp_path / "runtime"), host="127.0.0.1", port=server.port, device="Telescope Simulator")
    assert result["confirmed"] is True and result["summary"].startswith("MOUNT STOP CONFIRMED")
    assert result["signalled"] == [] and "not a physical brake" in result["limits"]
    _no_new_movement(server, 0)


def test_web_stop_mount_signals_running_physical_jobs_and_only_those(indi, tmp_path, monkeypatch):
    import almita_web_ops as ops
    server = indi("--slew-seconds", "1")
    jobs = [{"job_id": "ALIGN-1", "stage": "align", "state": "RUNNING"},
            {"job_id": "REDUCE-1", "stage": "reduce", "state": "RUNNING"},             # moves nothing: left alone
            {"job_id": "ALIGN-0", "stage": "align", "state": "EXITED"}]
    stopped = []
    monkeypatch.setattr(ops, "list_jobs", lambda limit=40: jobs)
    monkeypatch.setattr(ops, "stop", lambda job_id: stopped.append(job_id))
    result = ops.stop_mount(runtime_dir=str(tmp_path / "runtime"), host="127.0.0.1", port=server.port, device="Telescope Simulator")
    assert stopped == ["ALIGN-1"] and result["signalled"] == ["job ALIGN-1"] and result["confirmed"] is True


def test_web_stop_mount_without_indi_is_parada_no_confirmada(tmp_path, monkeypatch):
    import socket
    import almita_web_ops as ops
    with socket.socket() as probe:                                        # a port nobody listens on
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    monkeypatch.setattr(ops, "list_jobs", lambda limit=40: [])
    result = ops.stop_mount(runtime_dir=str(tmp_path / "runtime"), host="127.0.0.1", port=dead_port, device="Telescope Simulator")
    assert result["confirmed"] is False and result["summary"].startswith("PARADA NO CONFIRMADA")


# ------------------------------------------------------------------ the same stop in the gain pilot and the wizard
def _rtl(tmp_path):
    from tests.support import stop_workspace as W
    return W.Listening("fake_rtl_tcp.py")


def test_gain_pilot_capture_cancelled_during_its_goto_stops_the_mount(indi, tmp_path):
    import observation_gain_pilot as gp
    server = indi("--start-tracking", "--slew-seconds", "20", "--device", "LX200 OnStep")
    rtl = _rtl(tmp_path)

    async def scenario():
        task = asyncio.create_task(gp._capture_at("127.0.0.1", server.port, "127.0.0.1", rtl.port, 6.5, -45.0, 40.2, 1.0, 0.2,
                                                  250000, 1420405752, tmp_path / "pilot.h5"))
        await asyncio.sleep(3.0)                         # connected, tuned, slewing
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    try:
        mount_stop.LAST_REPORT = None
        asyncio.run(scenario())
    finally:
        rtl.close()
    assert mount_stop.LAST_REPORT is not None and mount_stop.LAST_REPORT["confirmed"], mount_stop.LAST_REPORT
    assert not (tmp_path / "pilot.h5").exists()


def test_wizard_hi_capture_cancelled_during_its_goto_stops_the_mount_and_records_it(indi, tmp_path, monkeypatch):
    """STOP CURRENT STEP during the HI GOTO: on 2026-10-13 the mount finished the slew on its own."""
    import calibrate_reference_wizard as wiz
    import indi_telescope_control
    import sdr_capture
    server = indi("--slew-seconds", "20", "--device", "LX200 OnStep")
    rtl = _rtl(tmp_path)
    real_indi, real_sdr = indi_telescope_control.INDITelescopeControl, sdr_capture.SDRCapture
    # the wizard hard-codes localhost:7624 / :1234 - point the REAL classes at the test servers, nothing else
    monkeypatch.setattr(indi_telescope_control, "INDITelescopeControl", lambda host, port, device, verbose: real_indi("127.0.0.1", server.port, device, verbose))
    monkeypatch.setattr(sdr_capture, "SDRCapture", lambda mode, host, port, verbose=False: real_sdr(mode, "127.0.0.1", rtl.port, verbose=verbose))
    cap_dir = tmp_path / "captures" / "HI_ALTO"
    cap_dir.mkdir(parents=True)

    async def scenario():
        task = asyncio.create_task(wiz._capture_n_at(6.5, -45.0, 40.2, 2, 1.0, 0.2, 250000, 1420405752, cap_dir))
        await asyncio.sleep(3.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    try:
        mount_stop.LAST_REPORT = None
        asyncio.run(scenario())
    finally:
        rtl.close()
    report = mount_stop.LAST_REPORT
    assert report is not None and report["confirmed"], report
    assert report["abort"]["sent"] is True and report["duration_s"] < 20        # the 20 s slew was cut short
    assert list(cap_dir.glob("*.h5")) == []
    # ... and the wizard's own record of the interruption carries it
    session = tmp_path / "WIZARD-T"
    session.mkdir()
    wiz._state_path(session).write_text(json.dumps({"step": "READY_HI_ALTO"}))
    wiz._record_interruption(session, "HI_ALTO", KeyboardInterrupt(), cap_dir)
    saved = json.loads(wiz._state_path(session).read_text())["last_interruption"]
    assert saved["kind"] == "STOPPED_BY_OPERATOR" and saved["mount_stop"]["confirmed"] is True
