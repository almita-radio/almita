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


def rtl0_header(tuner_type=5, gain_count=29):
    return b"RTL0" + tuner_type.to_bytes(4, "big") + gain_count.to_bytes(4, "big")


async def _start_fake_rtl_tcp(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _quiet_block(n_bytes=131072):
    return bytes([127, 128] * (n_bytes // 2))


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
