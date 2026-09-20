"""Capture-level integration tests for the optional RFI_REF sidecar.

Exercises the real execute_observation_plan() lifecycle (session creation,
point loop, cleanup) with MAIN's SDR faked out exactly like the existing
visibility-tracking suite, while RFI_REF's own subprocess/port-ownership
are faked the same way as in test_rfi_monitor.py. The point throughout:
RFI_REF success, failure, or absence must never change whether MAIN
completes.
"""
import asyncio
import csv
import json

import pytest

import capture
import rfi_monitor
from capture import CaptureExecutor
from runtime_state import read_json_safe

FIELDS = ["point_number", "scan_order", "target_ra_hours", "target_dec_degrees",
          "target_ra_hms", "target_dec_dms", "capture_status", "start_time", "end_time",
          "duration", "error_message", "data_filename", "session_name"]


class FakeSession:
    def __init__(self):
        self.actions = []

    def create_session(self, **kwargs):
        self.actions.append("create")
        return "rfi-int-session"

    def update_session(self, *args, **kwargs):
        self.actions.append("update")

    def complete_session(self, *args):
        self.actions.append("complete")

    def pause_session(self, *args):
        self.actions.append("pause")


class FakeTelescope:
    device_name = "fake"

    def __init__(self):
        self.events = []

    async def goto(self, *args):
        self.events.append("goto")
        return True

    async def get_onstep_status(self, timeout=1):
        return {"state": "healthy", "message": "None", "is_error": False,
                "received_at": "2026-08-20T00:00:00+00:00", "fresh": True,
                "hardware_fresh": False, "source": "indi_cached", "update_seq": 0, "reason": None}

    async def wait_onstep_status_update(self, timeout=2.5):
        return {"state": "healthy", "message": "None", "is_error": False,
                "received_at": "2026-08-20T00:00:00+00:00", "fresh": True,
                "hardware_fresh": True, "source": "indi_poll", "update_seq": 1, "reason": None}

    async def get_tracking_state(self, timeout=1):
        return "off"

    async def set_tracking(self, enable):
        self.events.append("set_on" if enable else "set_off")
        return True

    async def wait_tracking_state(self, expected_on, timeout=5):
        self.events.append("wait_on" if expected_on else "wait_off")
        return True


class FakeMainSDR:
    """Stands in for MAIN's SDRCapture - never touches rtl_tcp.service."""
    def __init__(self, **kwargs):
        self.events = []

    async def connect(self):
        self.events.append("connect")

    async def configure(self, **kwargs):
        self.events.append("configure")

    async def flush_buffer(self):
        return 0

    async def capture(self, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(capture_time=0.01, disk_write_time=0.0,
                                throughput_mbps=4.8, total_samples=100)

    async def close(self):
        self.events.append("close")


class FakeRfiProc:
    def __init__(self, pid, alive=True):
        self.pid = pid
        self._alive = alive
        self.returncode = None if alive else 1
        self.terminated = False
        self.killed = False
        self.terminate_is_effective = True  # set False to force the SIGTERM -> SIGKILL escalation
        self.stdout = type("S", (), {"read": lambda self: ""})()

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminated = True
        if self.terminate_is_effective:
            self._alive = False
            self.returncode = 0

    def kill(self):
        self.killed = True
        self._alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        if self._alive:
            raise __import__("subprocess").TimeoutExpired(cmd="rtl_tcp", timeout=timeout)
        return self.returncode


def make_executor(tmp_path, count=2, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan = tmp_path / "plan.csv"
    with plan.open("w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=FIELDS)
        w.writeheader()
        for n in range(1, count + 1):
            w.writerow({"point_number": n, "scan_order": n, "target_ra_hours": n,
                        "target_dec_degrees": -40, "target_ra_hms": str(n), "target_dec_dms": "-40",
                        "capture_status": "planned", "data_filename": f"p{n}.dat", "session_name": "rfi-int"})
    (tmp_path / "observer_config.json").write_text(json.dumps(
        {"observer": {"latitude_deg": -33.4, "longitude_deg": -70.6, "elevation_m": 550}}))
    ex = CaptureExecutor(str(plan), config_path="observer_config.json", min_altitude_deg=-90, **kwargs)
    ex.session_manager = FakeSession()
    ex.hour_angle_for_point = lambda p, obstime=None: -1
    ex.visibility_for_point = lambda point, obstime=None: {
        "altitude_deg_at_goto": 45.0, "azimuth_deg_at_goto": 100, "ha_hours_at_goto": -1,
        "visibility_checked_at": "2026-08-20T00:00:00.000", "min_altitude_deg": ex.min_altitude_deg,
    }
    assert ex.load_observation_plan()
    ex.telescope = FakeTelescope()
    return ex


@pytest.mark.asyncio
async def test_rfi_ref_disabled_by_default_capture_unaffected(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "SDRCapture", FakeMainSDR)

    def boom(*a, **k):
        raise AssertionError("RFI_REF subprocess must never launch when disabled")
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", boom)

    runtime_dir = tmp_path / "runtime"
    ex = make_executor(tmp_path, runtime_dir=str(runtime_dir))
    assert ex.rfi_ref_enabled is False
    assert ex.rfi_ref.enabled is False

    assert await ex.execute_observation_plan(0, 0)
    assert not (runtime_dir / "rfi_ref_status.json").exists()


@pytest.mark.asyncio
async def test_session_started_announces_settle_and_capture_seconds(monkeypatch, tmp_path):
    """The console's Session panel needs these to show requested SETTLE/
    CAPTURE times - previously SESSION_STARTED never announced them."""
    monkeypatch.setattr(capture, "SDRCapture", FakeMainSDR)

    runtime_dir = tmp_path / "runtime"
    ex = make_executor(tmp_path, runtime_dir=str(runtime_dir))

    assert await ex.execute_observation_plan(settle_time=3.5, capture_time=12.0)
    current_session = read_json_safe(runtime_dir / "current_session.json")
    assert current_session["settle_seconds"] == 3.5
    assert current_session["capture_seconds"] == 12.0


@pytest.mark.asyncio
async def test_rfi_ref_enabled_uses_canonical_session_id_and_own_config(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "SDRCapture", FakeMainSDR)

    async def rfi_ref_handler(reader, writer):
        writer.write(b"RTL0" + (5).to_bytes(4, "big") + (29).to_bytes(4, "big"))
        await writer.drain()
        try:
            while True:
                writer.write(bytes([127, 128] * 65536))
                await writer.drain()
                await asyncio.sleep(0.01)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server = await asyncio.start_server(rfi_ref_handler, "127.0.0.1", 0)
    fake_port = server.sockets[0].getsockname()[1]

    launched = {"done": False}
    proc = FakeRfiProc(pid=42424)

    def fake_popen(cmd, **kwargs):
        launched["done"] = True
        return proc
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (launched["done"] and p == fake_port) else None)

    runtime_dir = tmp_path / "runtime"
    try:
        ex = make_executor(tmp_path, runtime_dir=str(runtime_dir),
                            rfi_ref_enabled=True, rfi_ref_gain_db=25.0, rfi_ref_port=fake_port,
                            rfi_ref_serial="00000002")
        assert ex.rfi_ref.gain_db == 25.0
        assert ex.rfi_ref.port == fake_port
        assert ex.rfi_ref.device_serial == "00000002"
        # RFI_REF mirrors MAIN's frequency/sample rate automatically, never a
        # second independent value someone could forget to keep in sync.
        assert ex.rfi_ref.center_frequency_hz == ex.sdr_freq
        assert ex.rfi_ref.sample_rate == ex.sdr_sample_rate

        assert await ex.execute_observation_plan(0, 0)
        assert ex.rfi_ref.session_id == ex.session_id  # same canonical id, never a second identity

        status = read_json_safe(runtime_dir / "rfi_ref_status.json")
        assert status["session_id"] == ex.session_id
        assert status["status"] == "STOPPED"  # cleanly stopped at end of session
        assert proc.terminated
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_rfi_ref_bias_tee_reaches_rtl_tcp_command_without_touching_main(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "SDRCapture", FakeMainSDR)

    async def rfi_ref_handler(reader, writer):
        writer.write(b"RTL0" + (5).to_bytes(4, "big") + (29).to_bytes(4, "big"))
        await writer.drain()
        try:
            while True:
                writer.write(bytes([127, 128] * 65536))
                await writer.drain()
                await asyncio.sleep(0.01)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server = await asyncio.start_server(rfi_ref_handler, "127.0.0.1", 0)
    fake_port = server.sockets[0].getsockname()[1]

    launch_cmds = []
    launched = {"done": False}
    proc = FakeRfiProc(pid=51515)

    def fake_popen(cmd, **kwargs):
        launch_cmds.append(cmd)
        launched["done"] = True
        return proc
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (launched["done"] and p == fake_port) else None)

    runtime_dir = tmp_path / "runtime"
    try:
        ex = make_executor(tmp_path, runtime_dir=str(runtime_dir),
                            rfi_ref_enabled=True, rfi_ref_gain_db=25.0, rfi_ref_port=fake_port,
                            rfi_ref_serial="00000002", rfi_ref_bias_tee=True)

        assert await ex.execute_observation_plan(0, 0)

        assert "-T" in launch_cmds[0]  # RFI_REF's own bias-tee reached the real command
        assert "00000002" in launch_cmds[0]

        # MAIN completed every point regardless of RFI_REF's bias-tee setting.
        rows = list(csv.DictReader(ex.csv_path.open()))
        assert all(row["capture_status"] == "success" for row in rows)

        # Child output was persisted to a real per-session log file, not a
        # PIPE nobody drains.
        log_path = ex.csv_path.parent / "rfi_ref" / "rtl_tcp.log"
        assert log_path.exists()
        assert "bias_tee=True" in log_path.read_text()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_rfi_ref_foreign_port_does_not_block_main_session(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "SDRCapture", FakeMainSDR)
    monkeypatch.setattr(rfi_monitor, "listening_pid", lambda host, port: 9999)

    def boom(*a, **k):
        raise AssertionError("must never launch onto a foreign-owned port")
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", boom)

    runtime_dir = tmp_path / "runtime"
    ex = make_executor(tmp_path, runtime_dir=str(runtime_dir), rfi_ref_enabled=True)

    assert await ex.execute_observation_plan(0, 0)  # MAIN completes regardless
    status = read_json_safe(runtime_dir / "rfi_ref_status.json")
    assert status["status"] == "UNAVAILABLE"

    rows = list(csv.DictReader(ex.csv_path.open()))
    assert all(row["capture_status"] == "success" for row in rows)


@pytest.mark.asyncio
async def test_rfi_ref_forced_kill_does_not_affect_main_completion(monkeypatch, tmp_path):
    """Reproduces (in fake form) the real field finding: RFI_REF's rtl_tcp
    catches SIGTERM but never exits, forcing a SIGKILL at end-of-session
    teardown. MAIN must complete every point regardless, and the forced
    kill must be visible in rfi_ref_status.json rather than hidden behind
    a bare STOPPED."""
    monkeypatch.setattr(capture, "SDRCapture", FakeMainSDR)

    async def rfi_ref_handler(reader, writer):
        writer.write(b"RTL0" + (5).to_bytes(4, "big") + (29).to_bytes(4, "big"))
        await writer.drain()
        try:
            while True:
                writer.write(bytes([127, 128] * 65536))
                await writer.drain()
                await asyncio.sleep(0.01)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server = await asyncio.start_server(rfi_ref_handler, "127.0.0.1", 0)
    fake_port = server.sockets[0].getsockname()[1]

    launched = {"done": False}
    proc = FakeRfiProc(pid=61616)
    proc.terminate_is_effective = False  # mirrors the real rtl_tcp: catches SIGTERM but never exits

    def fake_popen(cmd, **kwargs):
        launched["done"] = True
        return proc
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (launched["done"] and p == fake_port) else None)

    runtime_dir = tmp_path / "runtime"
    try:
        ex = make_executor(tmp_path, runtime_dir=str(runtime_dir),
                            rfi_ref_enabled=True, rfi_ref_gain_db=25.0, rfi_ref_port=fake_port,
                            rfi_ref_serial="00000002")

        assert await ex.execute_observation_plan(0, 0)  # MAIN completes regardless

        rows = list(csv.DictReader(ex.csv_path.open()))
        assert all(row["capture_status"] == "success" for row in rows)

        assert proc.terminated
        assert proc.killed  # SIGTERM was ineffective, so (and only so) we escalated

        status = read_json_safe(runtime_dir / "rfi_ref_status.json")
        assert status["status"] == "STOPPED"  # RFI_REF did stop - just forcibly
        assert status["last_runtime_error"] is not None
        assert "force-killed" in status["last_runtime_error"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_rfi_ref_launch_exception_is_caught_and_logged_main_completes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(capture, "SDRCapture", FakeMainSDR)

    def boom(*a, **k):
        raise RuntimeError("simulated RFI_REF launch failure")
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", boom)
    monkeypatch.setattr(rfi_monitor, "listening_pid", lambda host, port: None)

    ex = make_executor(tmp_path, rfi_ref_enabled=True, runtime_dir=str(tmp_path / "runtime"))
    assert await ex.execute_observation_plan(0, 0)
    out = capsys.readouterr().out
    assert "auxiliary only" in out or "RFI_REF" in out
