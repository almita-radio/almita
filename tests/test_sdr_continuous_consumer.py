import asyncio
import socket
import threading
import time

import pytest

from sdr_capture import (
    SDRCapture,
    SDRDisconnected,
    SDRStallNoBytes,
    SDRThroughputTooLow,
    SDRWallTimeout,
)


class StreamingSocket:
    def __init__(self, chunks=None, delay=0.0, repeat=b"\x80\x80", max_chunk=None):
        self.chunks = list(chunks or [])
        self.delay = delay
        self.repeat = repeat
        self.closed = False
        self.recv_threads = set()
        self.recv_count = 0
        self.max_chunk = max_chunk

    def settimeout(self, _timeout):
        pass

    def recv(self, size):
        self.recv_threads.add(threading.get_ident())
        self.recv_count += 1
        if self.delay:
            time.sleep(self.delay)
        if self.closed:
            return b""
        if self.chunks:
            value = self.chunks.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value[:size]
        if self.repeat is None:
            raise socket.timeout()
        size = min(size, self.max_chunk) if self.max_chunk else size
        return (self.repeat * ((size // len(self.repeat)) + 1))[:size]

    def shutdown(self, _how):
        self.closed = True

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_discard_drains_and_single_thread_owns_stream_recv():
    capture = SDRCapture(mode="network")
    sock = StreamingSocket(delay=0.001)
    capture.socket = sock
    capture._ensure_consumer_started()
    await asyncio.sleep(0.03)
    telemetry = capture.get_network_telemetry()
    await capture.close()
    assert telemetry["bytes_discarded"] > 0
    assert telemetry["consumer_state"] == "DISCARD"
    assert len(sock.recv_threads) == 1


@pytest.mark.asyncio
async def test_capture_boundary_exact_size_and_immediate_discard(tmp_path):
    capture = SDRCapture(mode="network")
    capture.socket = StreamingSocket(delay=0.001)
    capture._ensure_consumer_started()
    await asyncio.sleep(0.02)
    discarded_before = capture.get_network_telemetry()["bytes_discarded"]
    metrics = await capture.capture(0.01, str(tmp_path / "capture.h5"), sample_rate=100)
    at_complete = capture.get_network_telemetry()
    await asyncio.sleep(0.02)
    after = capture.get_network_telemetry()
    await capture.close()
    assert metrics.received_capture_bytes == 2
    assert metrics.buffer_used == metrics.buffer_capacity == 2
    assert at_complete["consumer_state"] == "DISCARD"
    assert after["bytes_discarded"] > discarded_before


@pytest.mark.asyncio
async def test_disconnect_is_typed_and_bounded(tmp_path):
    capture = SDRCapture(mode="network")
    capture.socket = StreamingSocket(chunks=[b""])
    with pytest.raises(SDRDisconnected) as raised:
        await capture.capture(1, str(tmp_path / "x.h5"), sample_rate=10)
    assert raised.value.code == "SDR_DISCONNECTED"


@pytest.mark.asyncio
async def test_stall_is_typed(tmp_path):
    capture = SDRCapture(mode="network")
    capture.stall_timeout = 0.05
    capture.socket = StreamingSocket(repeat=None)
    with pytest.raises(SDRStallNoBytes):
        await capture.capture(1, str(tmp_path / "x.h5"), sample_rate=10)


@pytest.mark.asyncio
async def test_low_throughput_is_typed(tmp_path):
    capture = SDRCapture(mode="network")
    capture.stall_timeout = 1
    capture.throughput_grace = 0.05
    capture.throughput_ratio = 0.8
    capture.socket = StreamingSocket(delay=0.03, repeat=b"\x80", max_chunk=1)
    with pytest.raises(SDRThroughputTooLow):
        await capture.capture(1, str(tmp_path / "x.h5"), sample_rate=1000)


@pytest.mark.asyncio
async def test_wall_timeout_is_typed(tmp_path):
    capture = SDRCapture(mode="network")
    capture.stall_timeout = 1
    capture.throughput_grace = 10
    capture.max_capture_wall_override = 0.08
    capture.socket = StreamingSocket(delay=0.03, repeat=b"\x80", max_chunk=1)
    with pytest.raises(SDRWallTimeout):
        await capture.capture(1, str(tmp_path / "x.h5"), sample_rate=1000)


@pytest.mark.asyncio
async def test_cancellation_returns_consumer_to_discard(tmp_path):
    capture = SDRCapture(mode="network")
    capture.socket = StreamingSocket(delay=0.02, repeat=b"\x80", max_chunk=10)
    task = asyncio.create_task(capture.capture(10, str(tmp_path / "x.h5"), sample_rate=1000))
    await asyncio.sleep(0.04)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert capture.get_network_telemetry()["consumer_state"] == "DISCARD"
    await capture.close()
