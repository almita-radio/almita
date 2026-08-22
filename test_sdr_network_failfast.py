import socket

import pytest

from sdr_capture import SDRCapture, SDRDisconnected, SDRStallNoBytes, validate_hdf5_capture


class FakeNetworkSocket:
    def __init__(self, receiver):
        self._receiver = receiver
        self.closed = False
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, size):
        return self._receiver(size)

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_network_capture_completes_by_exact_sample_count(tmp_path):
    output = tmp_path / "normal.h5"
    capture = SDRCapture(mode="network")
    capture.socket = FakeNetworkSocket(lambda size: bytes([128]) * size)

    metrics = await capture.capture(0.02, str(output), sample_rate=100)

    assert metrics.total_samples == 2
    assert validate_hdf5_capture(output, expected_samples=2)["capture_status"] == "success"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.asyncio
async def test_network_disconnect_fails_without_hdf5_or_part(tmp_path, capsys):
    output = tmp_path / "disconnect.h5"
    capture = SDRCapture(mode="network")
    network_socket = FakeNetworkSocket(lambda _size: b"")
    capture.socket = network_socket

    with pytest.raises(SDRDisconnected, match="SDR_DISCONNECTED"):
        await capture.capture(20.0, str(output), sample_rate=100)

    assert network_socket.closed
    assert capture.socket is None
    assert not output.exists()
    assert not list(tmp_path.glob("*.part"))
    # A disconnect may be observed by the permanent consumer before capture
    # activation; either way it is a differentiated failure with no artifact.


@pytest.mark.asyncio
async def test_network_stall_timeout_fails_without_hdf5_or_part(tmp_path):
    output = tmp_path / "stall.h5"

    def stalled(_size):
        raise socket.timeout("no samples")

    capture = SDRCapture(mode="network")
    network_socket = FakeNetworkSocket(stalled)
    capture.socket = network_socket

    capture.stall_timeout = 0.05
    with pytest.raises(SDRStallNoBytes, match="SDR_STALL_NO_BYTES"):
        await capture.capture(20.0, str(output), sample_rate=100)

    assert network_socket.closed
    assert capture.socket is None
    assert network_socket.timeouts == [pytest.approx(0.25, abs=0.1)]
    assert not output.exists()
    assert not list(tmp_path.glob("*.part"))
