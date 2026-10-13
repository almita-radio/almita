"""Write-policy regression tests for the calibration acquisition backend.

POLICY CHANGED 2026-10-02: RealCalibrationAcquisitionBackend used to NEVER call configure() so a capture was
read-only at the rtl_tcp protocol level - and it then recorded the service argv as the frequency. On 2026-10-01
that let the wizard's 50 ohm captures run at 1420405000 Hz (what rtl_tcp was left at) while declaring
1420405752. Now the backend must tune explicitly (sdr_tuning.tune_explicitly) before it will capture, and must
refuse to capture at anything other than what was tuned.

Also covers hardware_inspection.py's two read-only probes (handshake + service command line) against a local
fake TCP server / mocked subprocess - never against a live rtl_tcp instance in this test file.
"""
import ast
import asyncio
import socket
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from calibration_engine.acquisition import RealCalibrationAcquisitionBackend
from calibration_engine.hardware_inspection import (
    inspect_rtl_tcp_service_command_line, probe_rtl_tcp_handshake,
)


def _fake_sdr():
    fake_sdr = MagicMock()
    fake_sdr.connect = AsyncMock()
    fake_sdr.configure = AsyncMock()
    fake_sdr.capture = AsyncMock()
    fake_sdr.close = AsyncMock()
    return fake_sdr


def test_real_backend_refuses_to_capture_without_explicit_tuning():
    import sdr_tuning

    async def _run():
        backend = RealCalibrationAcquisitionBackend(host="localhost", port=1234)
        fake_sdr = _fake_sdr()
        with patch("sdr_capture.SDRCapture", return_value=fake_sdr):
            await backend.connect()
            with pytest.raises(sdr_tuning.TuningIncoherent):
                await backend.capture(duration_seconds=1.0, output_path="/tmp/does_not_matter.h5",
                                      center_frequency_hz=1_420_405_752.0, sample_rate_hz=2_400_000.0,
                                      gain_db=40.2, metadata={})
            await backend.close()
        fake_sdr.capture.assert_not_called()
    asyncio.run(_run())


def test_real_backend_tunes_explicitly_then_captures_with_requested_and_applied_attrs():
    async def _run():
        backend = RealCalibrationAcquisitionBackend(host="localhost", port=1234)
        fake_sdr = _fake_sdr()
        with patch("sdr_capture.SDRCapture", return_value=fake_sdr):
            await backend.connect()
            await backend.tune(1_420_405_752, 2_400_000, 40.2)
            await backend.capture(duration_seconds=1.0, output_path="/tmp/does_not_matter.h5",
                                  center_frequency_hz=1_420_405_752.0, sample_rate_hz=2_400_000.0,
                                  gain_db=40.2, metadata={"reference_kind": "AMBIENT_50R"})
            await backend.close()
        fake_sdr.configure.assert_awaited_once_with(center_freq=1_420_405_752, sample_rate=2_400_000, gain=40.2)
        attrs = fake_sdr.capture.await_args.args[3]
        assert attrs["requested_center_frequency_hz"] == 1_420_405_752
        assert attrs["applied_center_frequency_hz"] == 1_420_405_752
        assert attrs["center_frequency_hz"] == attrs["applied_center_frequency_hz"]
        assert attrs["tuning_evidence"] == "TEST_STUB"          # conftest stub; never claims a real acknowledgement
        assert "configuration_source" not in attrs             # the old argv "verification" label is gone
    asyncio.run(_run())


def test_real_backend_refuses_a_capture_at_a_frequency_other_than_the_tuned_one():
    import sdr_tuning

    async def _run():
        backend = RealCalibrationAcquisitionBackend(host="localhost", port=1234)
        fake_sdr = _fake_sdr()
        with patch("sdr_capture.SDRCapture", return_value=fake_sdr):
            await backend.connect()
            await backend.tune(1_420_405_752, 2_400_000, 40.2)
            with pytest.raises(sdr_tuning.TuningIncoherent):
                await backend.capture(duration_seconds=1.0, output_path="/tmp/does_not_matter.h5",
                                      center_frequency_hz=1_420_405_000.0, sample_rate_hz=2_400_000.0,
                                      gain_db=40.2, metadata={})
            await backend.close()
        fake_sdr.capture.assert_not_called()
    asyncio.run(_run())


def _fake_rtl_tcp_server(magic=b"RTL0", tuner_code=6, gain_count=29):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    received = []

    def _serve():
        conn, _ = server.accept()
        with conn:
            handshake = magic + tuner_code.to_bytes(4, "big") + gain_count.to_bytes(4, "big")
            conn.sendall(handshake)
            try:
                conn.settimeout(0.5)
                data = conn.recv(4096)
                if data:
                    received.append(data)
            except socket.timeout:
                pass
        server.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return port, thread, received


def test_probe_rtl_tcp_handshake_reads_without_writing():
    port, thread, received = _fake_rtl_tcp_server()
    info = probe_rtl_tcp_handshake("127.0.0.1", port, timeout=2.0)
    thread.join(timeout=2.0)
    assert info.reachable is True
    assert info.magic == "RTL0"
    assert info.tuner_type == "R828D"
    assert info.gain_count == 29
    assert received == []  # the probe never sent anything


def test_probe_rtl_tcp_handshake_reports_unreachable_cleanly():
    info = probe_rtl_tcp_handshake("127.0.0.1", 1, timeout=0.5)
    assert info.reachable is False


def test_inspect_service_command_line_parses_real_style_argv():
    fake_ps_output = ("  PID CMD\n"
                       "  858 /usr/bin/rtl_tcp -d 00000001 -a 127.0.0.1 -p 1234 -f 1420405000 -s 2400000 -g 40.2 -T\n")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=fake_ps_output)
        info = inspect_rtl_tcp_service_command_line(1234)
    assert info.found is True
    assert info.pid == 858
    assert info.parsed["serial"] == "00000001"
    assert info.parsed["center_frequency_hz"] == 1_420_405_000.0
    assert info.parsed["gain_db"] == 40.2
    assert info.parsed["bias_t_enabled"] is True


def test_inspect_service_command_line_reports_not_found_for_missing_port():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="  PID CMD\n")
        info = inspect_rtl_tcp_service_command_line(9999)
    assert info.found is False


def test_hardware_inspection_never_imports_write_capable_modules():
    """Structural guard: this module must never import sdr_capture (the
    only thing in this repo allowed to write rtl_tcp config)."""
    import calibration_engine.hardware_inspection as module
    tree = ast.parse(Path(module.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "sdr_capture" not in imported
