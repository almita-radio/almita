import struct
import threading

import pytest

from sdr_capture import CaptureMetrics, SDRCapture
from test_sdr_continuous_consumer import StreamingSocket


class CommandSocket(StreamingSocket):
    def __init__(self, fail_at=None):
        super().__init__(delay=.001)
        self.packets=[];self.fail_at=fail_at

    def sendall(self,packet):
        if self.fail_at is not None and len(self.packets)==self.fail_at:
            raise OSError("send failed")
        self.packets.append(struct.unpack(">BI",packet))


def configured_capture(sock=None):
    capture=SDRCapture(mode="network")
    capture.socket=sock or CommandSocket()
    capture.retune_settle_seconds=.01
    capture.gain_settle_seconds=0
    return capture


@pytest.mark.asyncio
async def test_new_connection_sends_full_manual_configuration_once():
    capture=configured_capture()
    await capture._configure_network(1420405752,2400000,20.7,CaptureMetrics())
    assert capture.socket.packets==[(0x01,1420405752),(0x02,2400000),(0x03,1),(0x04,207)]
    assert capture.manual_gain_enabled is True and capture.current_gain==20.7
    await capture.close()


@pytest.mark.asyncio
async def test_gain_only_and_identical_config_do_not_retune():
    capture=configured_capture()
    await capture._configure_network(100,200,20.7,CaptureMetrics())
    capture.socket.packets.clear()
    await capture._configure_network(100,200,29.7,CaptureMetrics())
    assert capture.socket.packets==[(0x04,297)]
    capture.socket.packets.clear()
    await capture._configure_network(100,200,29.7,CaptureMetrics())
    assert capture.socket.packets==[]
    await capture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("frequency","sample_rate","expected"),[
    (101,200,[(0x01,101)]),(100,201,[(0x02,201)]),
])
async def test_retune_sends_only_changed_value_and_single_consumer_drains(frequency,sample_rate,expected):
    capture=configured_capture()
    await capture._configure_network(100,200,20.7,CaptureMetrics())
    capture.socket.packets.clear();before=capture.bytes_discarded
    await capture._configure_network(frequency,sample_rate,20.7,CaptureMetrics())
    assert capture.socket.packets==expected
    assert capture.bytes_discarded>before
    assert len(capture.socket.recv_threads)==1
    await capture.close()


@pytest.mark.asyncio
async def test_close_invalidates_state_and_reconnect_reapplies_all_commands():
    capture=configured_capture()
    await capture._configure_network(100,200,20.7,CaptureMetrics());await capture.close()
    assert (capture.current_frequency,capture.current_sample_rate,
            capture.manual_gain_enabled,capture.current_gain)==(None,None,None,None)
    capture.socket=CommandSocket();capture.consumer_state="DISCONNECTED"
    await capture._configure_network(100,200,20.7,CaptureMetrics())
    assert capture.socket.packets==[(0x01,100),(0x02,200),(0x03,1),(0x04,207)]
    await capture.close()


@pytest.mark.asyncio
async def test_failed_send_does_not_mark_that_command_applied():
    capture=configured_capture(CommandSocket(fail_at=1))
    with pytest.raises(OSError):
        await capture._configure_network(100,200,20.7,CaptureMetrics())
    assert capture.current_frequency==100
    assert capture.current_sample_rate is None
    assert capture.manual_gain_enabled is None
    assert capture.current_gain is None
    await capture.close()


@pytest.mark.asyncio
async def test_auto_mode_uses_zero_and_manual_enable_never_does():
    capture=configured_capture()
    await capture._configure_network(100,200,"auto",CaptureMetrics())
    assert capture.socket.packets[-1]==(0x03,0)
    capture.socket.packets.clear()
    await capture._configure_network(100,200,8.7,CaptureMetrics())
    assert capture.socket.packets==[(0x03,1),(0x04,87)]
    await capture.close()
