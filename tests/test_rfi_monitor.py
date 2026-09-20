"""Focused, offline tests for the RFI_REF sidecar (rfi_monitor.py).

Hardware is faked throughout: subprocess.Popen and listening_pid are
monkeypatched so no real rtl_tcp/RTL-SDR is required, while the TCP client
state machine itself is exercised against a real (in-process, loopback)
asyncio server so the actual read/parse/backpressure logic is genuinely
tested, not just mocked away. Real-hardware validation is a separate
SATANIC test (data/rfi_ref_satanic/...), not this suite.
"""
import asyncio

import numpy as np
import pytest

import rfi_monitor
from runtime_state import read_json_safe


class FakeProc:
    """Stands in for subprocess.Popen(["rtl_tcp", ...]) without touching any
    real process. terminate()/kill() only ever affect this fake object -
    exactly mirroring the guarantee that RFIReferenceMonitor must only ever
    signal the child it itself launched."""

    def __init__(self, pid=31337, alive=True, stdout_text=""):
        self.pid = pid
        self._alive = alive
        self.returncode = None if alive else 1
        self.terminated = False
        self.killed = False
        self.terminate_is_effective = True
        self.stdout = _FakeStdout(stdout_text)

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


class _FakeStdout:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


class _FileRedirectedProc:
    """Mirrors real subprocess.Popen semantics when stdout/stderr are
    redirected to a file rather than PIPE: proc.stdout is None (there is no
    pipe object to read from in-process; the OS writes straight to the
    file). Distinct from FakeProc, which always exposes a fake PIPE and so
    can't exercise the log-file tail-read fallback in rfi_monitor.py."""

    def __init__(self, pid=1, alive=False, returncode=1):
        self.pid = pid
        self._alive = alive
        self.returncode = returncode if not alive else None
        self.stdout = None

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        return self.returncode


def rtl0_header(tuner_type=5, gain_count=29):
    return b"RTL0" + tuner_type.to_bytes(4, "big") + gain_count.to_bytes(4, "big")


async def _start_fake_rtl_tcp(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _quiet_block(n_bytes=131072):
    return bytes([127, 128] * (n_bytes // 2))


def _dc_biased_noise_block(n_bytes=131072, dc_offset=15.0, seed=42):
    """Broadband-noise-like I/Q with a strong DC bias, mimicking the real
    RTL-SDR zero-IF LO-leakage artifact observed on real V3/V4 hardware
    (a narrow spike exactly at the tuned center frequency, ~24 dB above
    the noise floor, confirmed via data/runtime/rfi_ref_spectrum.json
    during a real field session)."""
    n = n_bytes // 2
    rng = np.random.default_rng(seed)
    i = np.clip(rng.normal(0, 3.0, n) + dc_offset + 127.5, 0, 255).astype(np.uint8)
    q = np.clip(rng.normal(0, 3.0, n) + dc_offset + 127.5, 0, 255).astype(np.uint8)
    chunk = np.empty(2 * n, dtype=np.uint8)
    chunk[0::2] = i
    chunk[1::2] = q
    return chunk.tobytes()


# ---------------------------------------------------------------- lifecycle


@pytest.mark.asyncio
async def test_disabled_never_launches_subprocess_and_capture_is_unaffected(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess.Popen must never be called while disabled")
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", boom)

    m = rfi_monitor.RFIReferenceMonitor(enabled=False, runtime_dir=tmp_path, session_id="s1")
    await m.start()
    assert m.status == "DISABLED"
    await m.stop()  # must also be a harmless no-op

    status = read_json_safe(tmp_path / "rfi_ref_status.json")
    assert status["enabled"] is False
    assert status["status"] == "DISABLED"


@pytest.mark.asyncio
async def test_port_occupied_by_foreign_pid_never_connects_or_kills(tmp_path, monkeypatch):
    monkeypatch.setattr(rfi_monitor, "listening_pid", lambda host, port: 4242)

    def boom(*a, **k):
        raise AssertionError("must never spawn a subprocess onto a foreign-owned port")
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", boom)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1", port=1235)
    await m.start()
    assert m.status == "UNAVAILABLE"
    assert "4242" in m.last_error

    await m.stop()  # ownership-safe: nothing to clean up, pid 4242 untouched
    status = read_json_safe(tmp_path / "rfi_ref_status.json")
    assert status["status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_missing_device_process_exits_before_binding_is_unavailable(tmp_path, monkeypatch):
    proc = FakeProc(alive=False, stdout_text="usb_claim_interface error -6\n")
    monkeypatch.setattr(rfi_monitor, "listening_pid", lambda host, port: None)
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", lambda *a, **k: proc)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         bind_timeout=0.5)
    await m.start()
    assert m.status in ("UNAVAILABLE", "FAILED")
    assert "usb_claim_interface" in m.last_error or "exited" in m.last_error


@pytest.mark.asyncio
async def test_successful_start_reaches_running_with_correct_config_and_session_id(tmp_path, monkeypatch):
    connected = asyncio.Event()

    async def handler(reader, writer):
        writer.write(rtl0_header())
        await writer.drain()
        connected.set()
        try:
            while True:
                writer.write(_quiet_block())
                await writer.drain()
                await asyncio.sleep(0.005)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server, port = await _start_fake_rtl_tcp(handler)

    launch_cmds = []
    proc = FakeProc(pid=31337)
    launched = {"done": False}

    def fake_popen(cmd, **kwargs):
        launch_cmds.append(cmd)
        launched["done"] = True
        return proc

    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == port and launched["done"]) else None)

    m = rfi_monitor.RFIReferenceMonitor(
        enabled=True, runtime_dir=tmp_path, session_id="field-session-42",
        device_serial="00000002", port=port, center_frequency_hz=1420405000,
        sample_rate=2_400_000, gain_db=25.0, bind_timeout=2.0,
    )
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.2)

        assert m.status == "RUNNING"
        cmd = launch_cmds[0]
        assert cmd[:3] == ["rtl_tcp", "-d", "00000002"]
        assert "1420405000" in cmd
        assert "2400000" in cmd
        assert "25.0" in cmd

        status = read_json_safe(tmp_path / "rfi_ref_status.json")
        assert status["session_id"] == "field-session-42"
        assert status["status"] == "RUNNING"
        assert status["gain_db"] == 25.0
        assert status["center_frequency_hz"] == 1420405000
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()

    assert proc.terminated  # only the fake pid we launched, never any real pid


@pytest.mark.asyncio
async def test_rfi_ref_dying_mid_session_marks_failed_without_raising(tmp_path, monkeypatch):
    connected = asyncio.Event()

    async def handler(reader, writer):
        writer.write(rtl0_header())
        await writer.drain()
        connected.set()
        writer.write(_quiet_block())
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()  # simulate the RFI_REF rtl_tcp process dying

    server, port = await _start_fake_rtl_tcp(handler)
    proc = FakeProc(pid=555)
    launched = {"done": False}
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen",
                         lambda *a, **k: (launched.__setitem__("done", True), proc)[1])
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == port and launched["done"]) else None)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         port=port, bind_timeout=2.0)
    await m.start()
    await asyncio.wait_for(connected.wait(), timeout=2.0)
    await asyncio.sleep(0.5)

    assert m.status == "FAILED"
    assert m.last_error

    await m.stop()  # must not raise even though the remote side already died
    server.close()
    await server.wait_closed()


# ---------------------------------------------------------------- bias-tee


@pytest.mark.asyncio
async def test_bias_tee_true_adds_dash_t_flag(tmp_path, monkeypatch):
    launch_cmds = []
    proc = FakeProc(pid=9001)
    launched = {"done": False}

    def fake_popen(cmd, **kwargs):
        launch_cmds.append(cmd)
        launched["done"] = True
        return proc

    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == 1235 and launched["done"]) else None)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         bias_tee=True, port=1235, bind_timeout=2.0)
    await m.start()
    await m.stop()

    assert "-T" in launch_cmds[0]


@pytest.mark.asyncio
async def test_bias_tee_false_omits_dash_t_flag(tmp_path, monkeypatch):
    launch_cmds = []
    proc = FakeProc(pid=9002)
    launched = {"done": False}

    def fake_popen(cmd, **kwargs):
        launch_cmds.append(cmd)
        launched["done"] = True
        return proc

    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == 1235 and launched["done"]) else None)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         bias_tee=False, port=1235, bind_timeout=2.0)
    await m.start()
    await m.stop()

    assert "-T" not in launch_cmds[0]


@pytest.mark.asyncio
async def test_bias_tee_defaults_to_false_when_unspecified(tmp_path, monkeypatch):
    launch_cmds = []
    proc = FakeProc(pid=9003)
    launched = {"done": False}

    def fake_popen(cmd, **kwargs):
        launch_cmds.append(cmd)
        launched["done"] = True
        return proc

    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == 1235 and launched["done"]) else None)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1", port=1235,
                                         bind_timeout=2.0)
    assert m.bias_tee is False
    await m.start()
    await m.stop()

    assert "-T" not in launch_cmds[0]


# ---------------------------------------------------------------- child logging


@pytest.mark.asyncio
async def test_log_path_redirects_child_output_to_real_file_not_pipe(tmp_path, monkeypatch):
    launch_kwargs = {}
    proc = FakeProc(pid=9010)
    launched = {"done": False}

    def fake_popen(cmd, **kwargs):
        launch_kwargs.update(kwargs)
        launched["done"] = True
        return proc

    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == 1235 and launched["done"]) else None)

    log_path = tmp_path / "rfi_ref" / "rtl_tcp.log"
    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         port=1235, bind_timeout=2.0, log_path=log_path)
    await m.start()

    # Never a bare PIPE nobody drains - either a real file or (only when no
    # log_path is configured, exercised by every other test in this module)
    # the original PIPE-and-read-on-demand behavior.
    assert launch_kwargs["stdout"] is not rfi_monitor.subprocess.PIPE
    assert launch_kwargs["stderr"] == rfi_monitor.subprocess.STDOUT
    assert log_path.exists()
    header = log_path.read_text()
    assert "launch" in header and "bias_tee=False" in header

    await m.stop()
    assert m._log_fh is None  # closed, not leaked
    footer = log_path.read_text()
    assert "shutdown" in footer
    assert f"exit_code={proc.returncode}" in footer


@pytest.mark.asyncio
async def test_missing_device_with_log_path_reads_tail_from_log_file_not_pipe(tmp_path, monkeypatch):
    """When output goes to a real file, proc.stdout is None (unlike the PIPE
    case) - the error tail must come from the log file on disk instead."""
    proc = _FileRedirectedProc(alive=False, returncode=1)

    def fake_popen(cmd, stdout=None, **kwargs):
        if stdout is not None:
            stdout.write("usb_claim_interface error -6\n")
            stdout.flush()
        return proc

    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rfi_monitor, "listening_pid", lambda host, port: None)

    log_path = tmp_path / "rfi_ref" / "rtl_tcp.log"
    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         bind_timeout=0.3, log_path=log_path)
    await m.start()

    assert m.status in ("UNAVAILABLE", "FAILED")
    assert "usb_claim_interface" in m.last_error
    assert "usb_claim_interface" in log_path.read_text()


# ---------------------------------------------------------------- stop() error-state hygiene


@pytest.mark.asyncio
async def test_stop_preserves_stale_error_as_last_runtime_error_and_clears_last_error(tmp_path, monkeypatch):
    """Regression test for the real field finding: stop() used to leave
    last_error holding a stale DEGRADED-period message next to a STOPPED
    status, reading as if the clean stop itself had failed."""
    m, server, connected = await _running_monitor(tmp_path, monkeypatch, 900, session_id="s1")
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        # connected only means the fake server sent the header - give the
        # client side its own turn to read it and reach RUNNING before we
        # overwrite state, or _run_consumer's own post-connect
        # `self.last_error = None` can race and clobber the write below.
        await asyncio.sleep(0.2)
        assert m.status == "RUNNING"
        # Simulate the real-world DEGRADED condition directly rather than
        # waiting 15s of real time for three consecutive read timeouts.
        m.status = "DEGRADED"
        m.last_error = "no data from RFI_REF for >=15s"
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()

    status = read_json_safe(tmp_path / "rfi_ref_status.json")
    assert status["status"] == "STOPPED"
    assert status["last_error"] is None
    assert status["last_runtime_error"] == "no data from RFI_REF for >=15s"


# ---------------------------------------------------------------- backpressure


@pytest.mark.asyncio
async def test_slow_fft_drops_blocks_without_stalling_its_own_read_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("ALMITA_RFI_TEST_FFT_DELAY_S", "0.3")
    blocks_sent = {"n": 0}
    stop = asyncio.Event()

    async def handler(reader, writer):
        writer.write(rtl0_header())
        await writer.drain()
        try:
            while not stop.is_set():
                writer.write(_quiet_block())
                await writer.drain()
                blocks_sent["n"] += 1
                await asyncio.sleep(0.01)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server, port = await _start_fake_rtl_tcp(handler)
    proc = FakeProc(pid=777)
    launched = {"done": False}
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen",
                         lambda *a, **k: (launched.__setitem__("done", True), proc)[1])
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == port and launched["done"]) else None)

    # quicklook_every=2: half the blocks are FFT-eligible, so a slow (0.3s)
    # FFT against a ~10ms block cadence backs up almost immediately.
    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         port=port, quicklook_every=2, bind_timeout=2.0)
    try:
        await m.start()
        await asyncio.sleep(1.0)
        assert m.status == "RUNNING"
        # The read loop itself was never blocked by the slow FFT: many more
        # blocks were received than the slow FFT could ever have processed.
        assert (m.processed_blocks + m.skipped_blocks + m.dropped_blocks) > 20
        assert m.dropped_blocks > 0
        assert m.processed_blocks < m.dropped_blocks + 3
    finally:
        stop.set()
        await m.stop()
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------- cleanup


@pytest.mark.asyncio
async def test_cleanup_escalates_to_kill_only_for_its_own_pid(tmp_path, monkeypatch):
    proc = FakeProc(pid=888)
    proc.terminate_is_effective = False  # force the SIGTERM -> SIGKILL escalation path
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(rfi_monitor, "listening_pid", lambda host, p: None)  # never binds

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         bind_timeout=0.3)
    await m.start()
    assert m.status in ("UNAVAILABLE", "FAILED")
    assert proc.terminated
    assert proc.killed  # SIGTERM was ineffective, so (and only so) we escalated
    assert m.proc is None


async def _running_monitor_with_proc(tmp_path, monkeypatch, pid, **kwargs):
    """Like _running_monitor, but also returns the FakeProc so callers can
    flip terminate_is_effective or instrument terminate()/kill() - needed
    for the stop()-path cleanup tests below, none of which the existing
    _running_monitor helper (which hides proc in a closure) can support."""
    connected = asyncio.Event()

    async def handler(reader, writer):
        writer.write(rtl0_header())
        await writer.drain()
        connected.set()
        try:
            while True:
                writer.write(_quiet_block())
                await writer.drain()
                await asyncio.sleep(0.005)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server, port = await _start_fake_rtl_tcp(handler)
    proc = FakeProc(pid=pid)
    launched = {"done": False}
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen",
                         lambda *a, **k: (launched.__setitem__("done", True), proc)[1])
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == port and launched["done"]) else None)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, port=port,
                                         bind_timeout=2.0, **kwargs)
    await m.start()
    await asyncio.wait_for(connected.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert m.status == "RUNNING"
    return m, server, proc


@pytest.mark.asyncio
async def test_stop_normal_terminates_cleanly_and_leaves_no_runtime_error(tmp_path, monkeypatch):
    """Child honors SIGTERM (the common case in tests, though not - per the
    2026-09 direct hardware reproduction - the real installed rtl_tcp):
    no forced kill, no runtime error, clean STOPPED."""
    m, server, proc = await _running_monitor_with_proc(tmp_path, monkeypatch, 1001, session_id="s1")
    try:
        pass
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()

    assert proc.terminated
    assert not proc.killed
    assert m.status == "STOPPED"
    assert m.last_runtime_error is None
    status = read_json_safe(tmp_path / "rfi_ref_status.json")
    assert status["status"] == "STOPPED"
    assert status["last_runtime_error"] is None


@pytest.mark.asyncio
async def test_stop_called_twice_is_idempotent_no_duplicate_signals(tmp_path, monkeypatch):
    m, server, proc = await _running_monitor_with_proc(tmp_path, monkeypatch, 1002, session_id="s1")
    calls = {"terminate": 0, "kill": 0}
    orig_terminate, orig_kill = proc.terminate, proc.kill
    proc.terminate = lambda: (calls.__setitem__("terminate", calls["terminate"] + 1), orig_terminate())
    proc.kill = lambda: (calls.__setitem__("kill", calls["kill"] + 1), orig_kill())

    await m.stop()
    first_status = m.status
    await m.stop()  # second call must be a no-op: no re-signal, no state corruption
    server.close()
    await server.wait_closed()

    assert calls["terminate"] == 1
    assert calls["kill"] == 0
    assert m.status == first_status == "STOPPED"
    assert m.proc is None


@pytest.mark.asyncio
async def test_stop_closes_client_connection_before_signaling_child(tmp_path, monkeypatch):
    """Confirms the order stop() actually uses: OUR OWN client transport is
    closed (writer.close()+wait_closed() completes) before the child is
    ever signaled. This is the guarantee the code actually makes and the
    order the 2026-09 hardware repro exercised (which still needed SIGKILL
    regardless - see the force-kill tests below). Deliberately does not
    assert on when the remote peer's asyncio task notices the disconnect -
    that depends on the peer's own scheduling, not on anything this class
    controls or promises."""
    events = []

    connected = asyncio.Event()
    orig_writer_close = asyncio.StreamWriter.close

    def recording_close(self):
        events.append("our_writer_close_called")
        return orig_writer_close(self)
    monkeypatch.setattr(asyncio.StreamWriter, "close", recording_close)

    async def handler(reader, writer):
        writer.write(rtl0_header())
        await writer.drain()
        connected.set()
        try:
            while True:
                writer.write(_quiet_block())
                await writer.drain()
                await asyncio.sleep(0.005)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server, port = await _start_fake_rtl_tcp(handler)
    proc = FakeProc(pid=1003)
    orig_terminate = proc.terminate
    proc.terminate = lambda: (events.append("terminate_called"), orig_terminate())
    launched = {"done": False}
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen",
                         lambda *a, **k: (launched.__setitem__("done", True), proc)[1])
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == port and launched["done"]) else None)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         port=port, bind_timeout=2.0)
    await m.start()
    await asyncio.wait_for(connected.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    await m.stop()
    server.close()
    await server.wait_closed()

    assert "our_writer_close_called" in events
    assert "terminate_called" in events
    assert events.index("our_writer_close_called") < events.index("terminate_called")


@pytest.mark.asyncio
async def test_stop_force_kill_records_forced_kill_in_last_runtime_error_and_status_stays_stopped(
        tmp_path, monkeypatch):
    """Reproduces (in fake form) the real field finding confirmed by direct
    hardware repro: rtl_tcp catches SIGTERM but never actually exits, so we
    escalate to SIGKILL (exit_code=-9). That forced kill must be visible in
    last_runtime_error, and the final status must still read STOPPED -
    RFI_REF genuinely did stop, just not on its own."""
    m, server, proc = await _running_monitor_with_proc(tmp_path, monkeypatch, 1004, session_id="s1")
    proc.terminate_is_effective = False  # mirrors the real rtl_tcp: catches SIGTERM but never exits

    await m.stop()
    server.close()
    await server.wait_closed()

    assert proc.terminated
    assert proc.killed
    assert m.status == "STOPPED"  # RFI_REF did stop - just forcibly
    assert m.last_runtime_error is not None
    assert "force-killed" in m.last_runtime_error
    assert "exit_code=-9" in m.last_runtime_error  # negative returncode surfaced correctly, not swallowed

    status = read_json_safe(tmp_path / "rfi_ref_status.json")
    assert status["status"] == "STOPPED"
    assert "force-killed" in status["last_runtime_error"]


@pytest.mark.asyncio
async def test_stop_force_kill_appends_to_existing_last_runtime_error_without_losing_either(
        tmp_path, monkeypatch):
    """A pre-existing DEGRADED-period error and a forced kill are two
    different facts; stop() must keep both, never let the later one
    overwrite the earlier one."""
    m, server, proc = await _running_monitor_with_proc(tmp_path, monkeypatch, 1005, session_id="s1")
    proc.terminate_is_effective = False
    m.status = "DEGRADED"
    m.last_error = "no data from RFI_REF for >=15s"

    await m.stop()
    server.close()
    await server.wait_closed()

    assert m.status == "STOPPED"
    assert "no data from RFI_REF for >=15s" in m.last_runtime_error
    assert "force-killed" in m.last_runtime_error


# ---------------------------------------------------------------- pure FFT helper


def test_quicklook_fft_detects_full_clipping():
    clipped_block = bytes([0, 255] * 65536)
    peak_dbfs, clip_fraction, occupancy, freq, power = rfi_monitor._quicklook_fft(clipped_block, 2_400_000)
    assert clip_fraction == 1.0
    assert peak_dbfs <= 0.5  # ~dBFS scale, never wildly above full scale


def test_quicklook_fft_quiet_block_has_low_occupancy():
    quiet_block = _quiet_block()
    peak_dbfs, clip_fraction, occupancy, freq, power = rfi_monitor._quicklook_fft(quiet_block, 2_400_000)
    assert clip_fraction == 0.0
    assert 0.0 <= occupancy <= 1.0


def test_quicklook_fft_suppresses_rtl_sdr_dc_spike():
    """Regression test for the real-hardware finding: RTL-SDR's zero-IF/LO
    leakage produces a narrow, strong spike exactly at the tuned center
    frequency (0 Hz baseband) on both V3 and V4 - a hardware artifact, not
    real RFI. _quicklook_fft must blank/interpolate it so peak_dbfs,
    occupancy, the spectrum, waterfall, and (downstream) the RFI occupancy
    map all reflect genuine RF content instead."""
    sample_rate = 2_400_000
    block = _dc_biased_noise_block(dc_offset=15.0)
    peak_dbfs, clip_fraction, occupancy, freq, power = rfi_monitor._quicklook_fft(block, sample_rate)
    center_idx = len(power) // 2
    peak_idx = int(np.argmax(power))
    # The peak must not land in the DC-guarded region - if suppression were
    # absent, the DC-biased synthetic block would peak exactly there.
    assert abs(peak_idx - center_idx) > 1
    noise_floor = float(np.median(power))
    assert peak_dbfs < noise_floor + 10.0  # no longer a dominant spike


def test_suppress_dc_spike_blanks_only_the_guard_band():
    sample_rate = 2_400_000
    n = 1024
    power = np.full(n, -80.0, dtype=np.float32)
    power[n // 2] = 0.0  # a lone, unrealistically strong DC bin
    result = rfi_monitor._suppress_dc_spike(power.copy(), sample_rate, n, guard_hz=1000.0)
    assert result[n // 2] < -70.0  # the spike itself is gone
    assert result[0] == -80.0 and result[-1] == -80.0  # far bins untouched


# ---------------------------------------------------------------- ANTENNA B spectrum product


def test_quicklook_fft_spectrum_is_bounded_with_correct_frequency_axis():
    sample_rate = 2_400_000
    block = _quiet_block()
    peak_dbfs, clip_fraction, occupancy, freq_offsets, power = rfi_monitor._quicklook_fft(block, sample_rate)
    assert len(freq_offsets) == rfi_monitor.N_SPECTRUM_BINS
    assert len(power) == rfi_monitor.N_SPECTRUM_BINS
    # Complex-baseband FFT spans the full +/- sample_rate/2 bandwidth.
    assert freq_offsets[0] < -sample_rate / 2 * 0.9
    assert freq_offsets[-1] > sample_rate / 2 * 0.9
    assert all(freq_offsets[i] < freq_offsets[i + 1] for i in range(len(freq_offsets) - 1))


def test_quicklook_fft_reuses_single_fft_no_second_computation(monkeypatch):
    calls = {"n": 0}
    real_fft = np.fft.fft

    def counting_fft(*a, **k):
        calls["n"] += 1
        return real_fft(*a, **k)
    monkeypatch.setattr(rfi_monitor.np.fft, "fft", counting_fft)
    rfi_monitor._quicklook_fft(_quiet_block(), 2_400_000)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_spectrum_published_from_real_fft_block_with_session_id(tmp_path, monkeypatch):
    connected = asyncio.Event()

    async def handler(reader, writer):
        writer.write(rtl0_header())
        await writer.drain()
        connected.set()
        try:
            while True:
                writer.write(_quiet_block())
                await writer.drain()
                await asyncio.sleep(0.005)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server, port = await _start_fake_rtl_tcp(handler)
    proc = FakeProc(pid=42)
    launched = {"done": False}
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen",
                         lambda *a, **k: (launched.__setitem__("done", True), proc)[1])
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == port and launched["done"]) else None)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="spec-session-1",
                                         port=port, quicklook_every=2, spectrum_write_interval=0.0,
                                         center_frequency_hz=1420405000, sample_rate=2_400_000,
                                         bind_timeout=2.0)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.3)

        spectrum = read_json_safe(tmp_path / "rfi_ref_spectrum.json")
        assert spectrum is not None
        assert spectrum["session_id"] == "spec-session-1"
        assert spectrum["center_frequency_hz"] == 1420405000
        assert spectrum["sample_rate"] == 2_400_000
        assert spectrum["device_serial"] == "00000002"
        assert len(spectrum["frequency_hz"]) == rfi_monitor.N_SPECTRUM_BINS
        assert len(spectrum["power_dbfs"]) == rfi_monitor.N_SPECTRUM_BINS
        # Bounded product only - never raw IQ (65536 complex samples/block).
        assert len(spectrum["frequency_hz"]) < 65536
        # Absolute frequency axis is centered on the tuned frequency.
        assert min(spectrum["frequency_hz"]) < 1420405000 < max(spectrum["frequency_hz"])
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_spectrum_write_is_throttled_independent_of_fft_rate(tmp_path, monkeypatch):
    connected = asyncio.Event()

    async def handler(reader, writer):
        writer.write(rtl0_header())
        await writer.drain()
        connected.set()
        try:
            while True:
                writer.write(_quiet_block())
                await writer.drain()
                await asyncio.sleep(0.005)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server, port = await _start_fake_rtl_tcp(handler)
    proc = FakeProc(pid=43)
    launched = {"done": False}
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen",
                         lambda *a, **k: (launched.__setitem__("done", True), proc)[1])
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == port and launched["done"]) else None)

    write_calls = []
    real_atomic_write = rfi_monitor.atomic_write_json

    def counting_write(path, value):
        if str(path).endswith("rfi_ref_spectrum.json"):
            write_calls.append(1)
        return real_atomic_write(path, value)
    monkeypatch.setattr(rfi_monitor, "atomic_write_json", counting_write)

    # quicklook_every=2 against a ~5ms block cadence makes many FFTs
    # complete well within a 60s throttle window.
    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         port=port, quicklook_every=2, spectrum_write_interval=60.0,
                                         bind_timeout=2.0)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.5)
        assert m.processed_blocks > 5  # many FFTs completed
        assert len(write_calls) == 1  # but the spectrum file was written only once
    finally:
        await m.stop()  # the final stop() flush adds exactly one more write
        server.close()
        await server.wait_closed()
    assert len(write_calls) == 2


@pytest.mark.asyncio
async def test_stop_preserves_final_spectrum_from_the_session(tmp_path, monkeypatch):
    connected = asyncio.Event()

    async def handler(reader, writer):
        writer.write(rtl0_header())
        await writer.drain()
        connected.set()
        try:
            while True:
                writer.write(_quiet_block())
                await writer.drain()
                await asyncio.sleep(0.005)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server, port = await _start_fake_rtl_tcp(handler)
    proc = FakeProc(pid=44)
    launched = {"done": False}
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen",
                         lambda *a, **k: (launched.__setitem__("done", True), proc)[1])
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == port and launched["done"]) else None)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="stopped-session",
                                         port=port, quicklook_every=2, spectrum_write_interval=60.0,
                                         bind_timeout=2.0)
    await m.start()
    await asyncio.wait_for(connected.wait(), timeout=2.0)
    await asyncio.sleep(0.3)
    await m.stop()
    server.close()
    await server.wait_closed()

    status = read_json_safe(tmp_path / "rfi_ref_status.json")
    spectrum = read_json_safe(tmp_path / "rfi_ref_spectrum.json")
    assert status["status"] == "STOPPED"
    assert spectrum is not None
    assert spectrum["session_id"] == "stopped-session"
    assert len(spectrum["power_dbfs"]) == rfi_monitor.N_SPECTRUM_BINS


@pytest.mark.asyncio
async def test_spectrum_publication_failure_does_not_break_rfi_ref_or_propagate(tmp_path, monkeypatch):
    connected = asyncio.Event()

    async def handler(reader, writer):
        writer.write(rtl0_header())
        await writer.drain()
        connected.set()
        try:
            while True:
                writer.write(_quiet_block())
                await writer.drain()
                await asyncio.sleep(0.005)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server, port = await _start_fake_rtl_tcp(handler)
    proc = FakeProc(pid=45)
    launched = {"done": False}
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen",
                         lambda *a, **k: (launched.__setitem__("done", True), proc)[1])
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == port and launched["done"]) else None)

    # Only the spectrum write is broken - status writes go through the real
    # implementation via a wrapper.
    real_atomic_write = rfi_monitor.atomic_write_json

    def selective_boom(path, value):
        if str(path).endswith("rfi_ref_spectrum.json"):
            raise OSError("simulated disk failure writing spectrum")
        return real_atomic_write(path, value)
    monkeypatch.setattr(rfi_monitor, "atomic_write_json", selective_boom)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, session_id="s1",
                                         port=port, quicklook_every=2, spectrum_write_interval=0.0,
                                         bind_timeout=2.0)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.3)
        # RFI_REF itself must be entirely unaffected by the spectrum write
        # failure: still RUNNING, still updating its own status/metrics.
        assert m.status == "RUNNING"
        assert m.processed_blocks > 0
        status = read_json_safe(tmp_path / "rfi_ref_status.json")
        assert status["status"] == "RUNNING"
        assert not (tmp_path / "rfi_ref_spectrum.json").exists()
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------- ANTENNA B waterfall + history


async def _running_monitor(tmp_path, monkeypatch, pid, **kwargs):
    """Shared setup: a fake in-process rtl_tcp server + a monitor pointed at
    it, ownership-faked exactly like the other hardware-free tests above.
    Returns (monitor, server, connected_event) - caller awaits connected,
    exercises the monitor, then must stop() the monitor and close() the
    server (no try/finally here so callers control exact sequencing)."""
    connected = asyncio.Event()

    async def handler(reader, writer):
        writer.write(rtl0_header())
        await writer.drain()
        connected.set()
        try:
            while True:
                writer.write(_quiet_block())
                await writer.drain()
                await asyncio.sleep(0.005)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    server, port = await _start_fake_rtl_tcp(handler)
    proc = FakeProc(pid=pid)
    launched = {"done": False}
    monkeypatch.setattr(rfi_monitor.subprocess, "Popen",
                         lambda *a, **k: (launched.__setitem__("done", True), proc)[1])
    monkeypatch.setattr(rfi_monitor, "listening_pid",
                         lambda host, p: proc.pid if (p == port and launched["done"]) else None)

    m = rfi_monitor.RFIReferenceMonitor(enabled=True, runtime_dir=tmp_path, port=port,
                                         quicklook_every=2, bind_timeout=2.0, **kwargs)
    return m, server, connected


@pytest.mark.asyncio
async def test_waterfall_bounded_rows_via_deque_eviction(tmp_path, monkeypatch):
    m, server, connected = await _running_monitor(
        tmp_path, monkeypatch, 50, session_id="s1", waterfall_max_rows=3, spectrum_write_interval=0.0)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.5)  # far more than 3 throttle ticks at 0s interval
        waterfall = read_json_safe(tmp_path / "rfi_ref_waterfall.json")
        assert waterfall is not None
        assert len(waterfall["rows"]) <= 3
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_waterfall_reuses_spectrum_no_second_fft(tmp_path, monkeypatch):
    """The waterfall's latest row must be the exact same power_dbfs/
    frequency_hz already published in the spectrum product from the same
    publish tick - proof it is downsampled from the one FFT already
    computed, never a second one."""
    m, server, connected = await _running_monitor(
        tmp_path, monkeypatch, 51, session_id="s1", spectrum_write_interval=60.0)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.3)
        m._maybe_publish(force=True)
        spectrum = read_json_safe(tmp_path / "rfi_ref_spectrum.json")
        waterfall = read_json_safe(tmp_path / "rfi_ref_waterfall.json")
        assert waterfall["frequency_hz"] == spectrum["frequency_hz"]
        assert waterfall["rows"][-1]["power_dbfs"] == spectrum["power_dbfs"]
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_waterfall_timestamps_monotonic_and_session_tagged(tmp_path, monkeypatch):
    m, server, connected = await _running_monitor(
        tmp_path, monkeypatch, 52, session_id="waterfall-session", spectrum_write_interval=0.05)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.4)
        waterfall = read_json_safe(tmp_path / "rfi_ref_waterfall.json")
        assert waterfall["session_id"] == "waterfall-session"
        assert waterfall["device_serial"] == "00000002"
        rows = waterfall["rows"]
        assert len(rows) >= 2
        timestamps = [row["utc"] for row in rows]
        assert timestamps == sorted(timestamps)
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_waterfall_stopped_preserves_final_waterfall(tmp_path, monkeypatch):
    m, server, connected = await _running_monitor(
        tmp_path, monkeypatch, 53, session_id="s1", spectrum_write_interval=60.0)
    await m.start()
    await asyncio.wait_for(connected.wait(), timeout=2.0)
    await asyncio.sleep(0.3)
    await m.stop()
    server.close()
    await server.wait_closed()

    status = read_json_safe(tmp_path / "rfi_ref_status.json")
    waterfall = read_json_safe(tmp_path / "rfi_ref_waterfall.json")
    assert status["status"] == "STOPPED"
    assert waterfall is not None and waterfall["session_id"] == "s1"
    assert len(waterfall["rows"]) >= 1


@pytest.mark.asyncio
async def test_waterfall_write_failure_isolated_from_status_and_spectrum(tmp_path, monkeypatch):
    m, server, connected = await _running_monitor(
        tmp_path, monkeypatch, 54, session_id="s1", spectrum_write_interval=0.0)
    real_atomic_write = rfi_monitor.atomic_write_json

    def selective_boom(path, value):
        if str(path).endswith("rfi_ref_waterfall.json"):
            raise OSError("simulated disk failure writing waterfall")
        return real_atomic_write(path, value)
    monkeypatch.setattr(rfi_monitor, "atomic_write_json", selective_boom)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.3)
        assert m.status == "RUNNING"
        assert read_json_safe(tmp_path / "rfi_ref_status.json")["status"] == "RUNNING"
        assert read_json_safe(tmp_path / "rfi_ref_spectrum.json") is not None
        assert not (tmp_path / "rfi_ref_waterfall.json").exists()
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_session_waterfall_bounded_rows_via_deque_eviction(tmp_path, monkeypatch):
    m, server, connected = await _running_monitor(
        tmp_path, monkeypatch, 60, session_id="s1", session_waterfall_max_rows=3,
        session_waterfall_interval=0.0, spectrum_write_interval=0.0)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.5)
        session_waterfall = read_json_safe(tmp_path / "rfi_ref_session_waterfall.json")
        assert session_waterfall is not None
        assert len(session_waterfall["rows"]) <= 3
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_session_waterfall_reuses_spectrum_no_second_fft(tmp_path, monkeypatch):
    m, server, connected = await _running_monitor(
        tmp_path, monkeypatch, 61, session_id="s1",
        spectrum_write_interval=60.0, session_waterfall_interval=60.0)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.3)
        m._maybe_publish(force=True)
        spectrum = read_json_safe(tmp_path / "rfi_ref_spectrum.json")
        session_waterfall = read_json_safe(tmp_path / "rfi_ref_session_waterfall.json")
        assert session_waterfall["frequency_hz"] == spectrum["frequency_hz"]
        assert session_waterfall["rows"][-1]["power_dbfs"] == spectrum["power_dbfs"]
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_session_waterfall_cadence_independent_of_live_waterfall(tmp_path, monkeypatch):
    """The whole point of the session product: it must accumulate far
    slower than the live ~2s waterfall, so the same row cap spans a full
    session instead of only a few minutes."""
    m, server, connected = await _running_monitor(
        tmp_path, monkeypatch, 62, session_id="s1",
        spectrum_write_interval=0.0, session_waterfall_interval=60.0)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.5)  # many fast publish ticks at 0s interval
        live_waterfall = read_json_safe(tmp_path / "rfi_ref_waterfall.json")
        session_waterfall = read_json_safe(tmp_path / "rfi_ref_session_waterfall.json")
        assert len(session_waterfall["rows"]) == 1
        assert len(live_waterfall["rows"]) > len(session_waterfall["rows"])
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_session_waterfall_write_failure_isolated_from_status_and_spectrum(tmp_path, monkeypatch):
    m, server, connected = await _running_monitor(
        tmp_path, monkeypatch, 63, session_id="s1",
        spectrum_write_interval=0.0, session_waterfall_interval=0.0)
    real_atomic_write = rfi_monitor.atomic_write_json

    def selective_boom(path, value):
        if str(path).endswith("rfi_ref_session_waterfall.json"):
            raise OSError("simulated disk failure writing session waterfall")
        return real_atomic_write(path, value)
    monkeypatch.setattr(rfi_monitor, "atomic_write_json", selective_boom)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.3)
        assert m.status == "RUNNING"
        assert read_json_safe(tmp_path / "rfi_ref_status.json")["status"] == "RUNNING"
        assert read_json_safe(tmp_path / "rfi_ref_waterfall.json") is not None
        assert not (tmp_path / "rfi_ref_session_waterfall.json").exists()
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_history_bounded_and_session_tagged_no_full_iq(tmp_path, monkeypatch):
    m, server, connected = await _running_monitor(
        tmp_path, monkeypatch, 55, session_id="s1", history_max_samples=3, spectrum_write_interval=0.0)
    try:
        await m.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        await asyncio.sleep(0.5)
        history = read_json_safe(tmp_path / "rfi_ref_history.json")
        assert history is not None
        assert history["session_id"] == "s1"
        assert len(history["samples"]) <= 3
        sample = history["samples"][-1]
        assert set(sample) == {"utc", "occupancy_fraction", "clipping_fraction", "peak_dbfs"}
        # No spectrum/IQ arrays anywhere in the scalar history product.
        assert "power_dbfs" not in sample and "frequency_hz" not in sample
    finally:
        await m.stop()
        server.close()
        await server.wait_closed()
