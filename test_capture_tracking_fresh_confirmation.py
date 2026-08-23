import pytest

from capture import CaptureExecutor
from indi_telescope_control import INDITelescopeControl


class TrackingTelescope:
    def __init__(self, initial, *, set_result=True, wait_result=True, final=None):
        self.initial = initial
        self.set_result = set_result
        self.wait_result = wait_result
        self.final = initial if final is None else final
        self.events = []
        self.reads = 0

    async def get_tracking_state(self, timeout=1.0):
        self.events.append("get")
        self.reads += 1
        return self.initial if self.reads == 1 else self.final

    async def set_tracking(self, enable):
        self.events.append("set_on" if enable else "set_off")
        return self.set_result

    async def wait_tracking_state(self, expected_on, timeout=5.0):
        self.events.append("wait_on" if expected_on else "wait_off")
        return self.wait_result


def executor_with(telescope):
    executor = CaptureExecutor("unused.csv")
    executor.telescope = telescope
    return executor


@pytest.mark.asyncio
async def test_initial_fresh_on_passes_without_write_or_second_wait():
    telescope = TrackingTelescope("on")
    assert await executor_with(telescope).confirm_tracking_on() == (True, "on", False)
    assert telescope.events == ["get"]


@pytest.mark.asyncio
async def test_initial_fresh_off_sends_track_on_and_waits_for_fresh_on():
    telescope = TrackingTelescope("off", wait_result=True)
    assert await executor_with(telescope).confirm_tracking_on() == (True, "on", True)
    assert telescope.events == ["get", "set_on", "wait_on"]


@pytest.mark.asyncio
async def test_initial_unknown_preserves_safe_command_and_confirmation_path():
    telescope = TrackingTelescope("unknown", wait_result=True)
    assert await executor_with(telescope).confirm_tracking_on() == (True, "on", True)
    assert telescope.events == ["get", "set_on", "wait_on"]


@pytest.mark.asyncio
async def test_track_on_sent_but_never_confirmed_fails():
    telescope = TrackingTelescope("off", wait_result=False, final="unknown")
    assert await executor_with(telescope).confirm_tracking_on() == (
        False, "unknown", True
    )
    assert telescope.events == ["get", "set_on", "wait_on", "get"]


@pytest.mark.asyncio
async def test_alert_remains_fail_safe_without_write():
    telescope = TrackingTelescope("alert")
    assert await executor_with(telescope).confirm_tracking_on() == (
        False, "alert", False
    )
    assert telescope.events == ["get"]


class Writer:
    def write(self, data):
        pass

    async def drain(self):
        pass


class Reader:
    def __init__(self, payload):
        self.payload = payload

    async def read(self, size):
        payload, self.payload = self.payload, b""
        return payload


@pytest.mark.asyncio
async def test_stale_cached_on_is_not_accepted_as_fresh():
    controller = INDITelescopeControl(device_name="LX200 OnStep")
    controller.writer = Writer()
    controller.reader = Reader(
        b'<setSwitchVector device="LX200 OnStep" name="OTHER" state="Ok"/>'
    )
    key = (controller.device_name, "TELESCOPE_TRACK_STATE")
    stale = {
        "tag": "setSwitchVector",
        "state": "Busy",
        "timestamp": None,
        "received_at": "old",
        "received_monotonic": 0.0,
        "elements": {"TRACK_ON": "On", "TRACK_OFF": "Off"},
        "raw": (
            '<setSwitchVector device="LX200 OnStep" '
            'name="TELESCOPE_TRACK_STATE" state="Busy">'
            '<oneSwitch name="TRACK_ON">On</oneSwitch>'
            '<oneSwitch name="TRACK_OFF">Off</oneSwitch>'
            '</setSwitchVector>'
        ),
        "update_seq": 1,
    }
    controller._property_cache[key] = stale
    controller._property_history[key] = [stale]
    assert await controller.get_tracking_state(timeout=0.05) == "unknown"


def test_unhealthy_onstep_still_fails_guard():
    assert not CaptureExecutor.onstep_status_allows_operation(
        {
            "hardware_fresh": True,
            "state": "error",
            "is_error": True,
        }
    )
