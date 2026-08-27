import asyncio
import hashlib
import inspect

import pytest

from indi_telescope_control import INDITelescopeControl


ELEMENTS = [
    ":GU# return", "Tracking", "Refractoring", "Park", "Pec", "TimeSync",
    "Mount Type", "Error", "Multi-Axis Tracking", "TMC Axis1", "TMC Axis2",
]


def vector(error="None", state="Ok", timestamp="2026-08-20T17:53:10", tag="set"):
    values = {name: "" for name in ELEMENTS}
    values.update({"Tracking": "Idle", "Error": error})
    child_tag = "defText" if tag == "def" else "oneText"
    body = "".join(
        f'<{child_tag} name="{name}">{value}</{child_tag}>' for name, value in values.items()
    )
    return (
        f'<{tag}TextVector device="LX200 OnStep" name="OnStep Status" '
        f'state="{state}" timestamp="{timestamp}">{body}</{tag}TextVector>'
    )


class Writer:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(data.decode())

    async def drain(self):
        pass


class Reader:
    def __init__(self, *responses):
        self.responses = list(responses)

    async def read(self, _):
        return self.responses.pop(0).encode() if self.responses else b""


class DelayedReader:
    def __init__(self, delay, response):
        self.delay = delay
        self.response = response
        self.readers = 0
        self.max_readers = 0

    async def read(self, _):
        self.readers += 1
        self.max_readers = max(self.max_readers, self.readers)
        try:
            await asyncio.sleep(self.delay)
            response, self.response = self.response, ""
            return response.encode()
        finally:
            self.readers -= 1


def controller(*responses):
    instance = INDITelescopeControl(device_name="LX200 OnStep", verbose=False)
    instance.writer = Writer()
    instance.reader = Reader(*responses)
    return instance


@pytest.mark.asyncio
async def test_healthy_status_and_real_elements():
    status = await controller(vector()).get_onstep_status()
    assert status["state"] == "healthy" and status["is_error"] is False
    assert status["message"] == "None" and list(status["elements"]) == ELEMENTS


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Motor/Driver Fault", "Arbitrary future OnStep error"])
async def test_any_nonhealthy_error_message_is_exposed(message):
    status = await controller(vector(error=message)).get_onstep_status()
    assert status["state"] == "error" and status["is_error"] is True
    assert status["message"] == message


@pytest.mark.asyncio
async def test_meridian_limit_is_returned_verbatim():
    message = "Meridian Limit (W) Exceeded"
    status = await controller(vector(error=message)).get_onstep_status()
    assert status["message"] == message and status["state"] == "error"


@pytest.mark.asyncio
async def test_absent_property_is_unknown_not_healthy():
    status = await controller(
        '<setTextVector device="LX200 OnStep" name="OTHER" state="Ok"></setTextVector>'
    ).get_onstep_status()
    assert status["state"] == "unknown" and status["fresh"] is False
    assert status["reason"] == "absent"


@pytest.mark.asyncio
async def test_timeout_is_unknown_not_healthy():
    class TimeoutReader:
        async def read(self, _):
            await asyncio.sleep(1)

    instance = controller();instance.reader = TimeoutReader()
    status = await instance.get_onstep_status(timeout=0.001)
    assert status["state"] == "unknown" and status["reason"] == "timeout"


@pytest.mark.asyncio
async def test_vector_alert_is_distinct_and_preserves_message():
    status = await controller(vector(error="None", state="Alert")).get_onstep_status()
    assert status["state"] == "alert" and status["is_error"] is True
    assert status["vector_state"] == "Alert" and status["message"] == "None"


@pytest.mark.asyncio
async def test_latest_of_multiple_updates_wins_and_is_fresh():
    payload = vector(error="Motor/Driver Fault", timestamp="old") + vector(
        error="Meridian Limit (W) Exceeded", timestamp="new"
    )
    status = await controller(payload).get_onstep_status()
    assert status["message"] == "Meridian Limit (W) Exceeded"
    assert status["timestamp"] == "new" and status["received_at"] and status["fresh"]


@pytest.mark.asyncio
async def test_api_sends_only_the_read_query():
    instance = controller(vector())
    await instance.get_onstep_status()
    assert instance.writer.messages == [
        '<getProperties device="LX200 OnStep" name="OnStep Status" version="1.7"/>\n'
    ]
    assert not any(token in instance.writer.messages[0] for token in ("<new", "HOME", "ABORT", "PARK", "SYNC", "TRACK"))


@pytest.mark.asyncio
async def test_getproperties_definition_is_indi_cached_not_hardware_fresh():
    status = await controller(vector(tag="def")).get_onstep_status()
    assert status["indi_fresh"] is True and status["fresh"] is True
    assert status["hardware_fresh"] is False
    assert status["source"] == "indi_cached" and status["update_seq"] == 0


@pytest.mark.asyncio
async def test_spontaneous_publication_is_hardware_fresh_and_sends_nothing():
    instance = controller(vector())
    status = await instance.wait_onstep_status_update()
    assert status["hardware_fresh"] is True and status["source"] == "indi_poll"
    assert status["update_seq"] == 1 and instance.writer.messages == []


@pytest.mark.asyncio
async def test_delayed_spontaneous_publication_wakes_wait_without_background_reader():
    instance = controller()
    reader = DelayedReader(0.01, vector(timestamp="after-wait"))
    instance.reader = reader
    status = await instance.wait_onstep_status_update(timeout=0.1)
    assert status["hardware_fresh"] is True and status["timestamp"] == "after-wait"
    assert reader.max_readers == 1


@pytest.mark.asyncio
async def test_identical_later_publications_each_increment_sequence():
    class SequencedDelayedReader:
        def __init__(self):
            self.responses = [vector(), vector()]

        async def read(self, _):
            await asyncio.sleep(0.01)
            return self.responses.pop(0).encode() if self.responses else b""

    instance = controller(); instance.reader = SequencedDelayedReader()
    first = await instance.wait_onstep_status_update()
    second = await instance.wait_onstep_status_update()
    assert first["elements"] == second["elements"]
    assert (first["update_seq"], second["update_seq"]) == (1, 2)


@pytest.mark.asyncio
async def test_timeout_without_poll_is_not_hardware_fresh_or_healthy():
    class TimeoutReader:
        async def read(self, _):
            await asyncio.sleep(1)

    instance = controller(); instance.reader = TimeoutReader()
    status = await instance.wait_onstep_status_update(timeout=0.001)
    assert status["hardware_fresh"] is False and status["indi_fresh"] is False
    assert status["state"] == "unknown" and status["reason"] == "timeout"


@pytest.mark.asyncio
async def test_cached_meridian_is_ignored_then_spontaneous_healthy_wins():
    payload = vector(error="Meridian Limit (W) Exceeded", tag="def") + vector(
        error="Goto No Error", timestamp="later"
    )
    status = await controller(payload).wait_onstep_status_update()
    assert status["hardware_fresh"] is True and status["state"] == "healthy"
    assert status["message"] == "Goto No Error" and status["update_seq"] == 1


@pytest.mark.asyncio
async def test_other_property_does_not_wake_status_wait():
    other = '<setTextVector device="LX200 OnStep" name="OTHER"><oneText name="Error">None</oneText></setTextVector>'
    instance = controller(other)
    status = await instance.wait_onstep_status_update()
    assert status["hardware_fresh"] is False and status["state"] == "unknown"


@pytest.mark.asyncio
async def test_wrong_device_does_not_wake_status_wait():
    wrong = vector().replace('device="LX200 OnStep"', 'device="Another Mount"')
    instance = controller(wrong)
    status = await instance.wait_onstep_status_update()
    assert status["hardware_fresh"] is False and status["state"] == "unknown"


def test_freshness_apis_have_no_control_or_direct_serial_operations():
    source = "\n".join(inspect.getsource(getattr(INDITelescopeControl, name)) for name in (
        "get_onstep_status", "wait_onstep_status_update"
    ))
    assert "<new" not in source
    assert "/dev/tty" not in source and "serial." not in source


def test_dispatcher_migration_keeps_reviewed_goto_and_tracking_structure():
    expected = {
        # Re-reviewed after compact trace suppression plus the minimal abort fix:
        # KeyboardInterrupt returns failure; CancelledError propagates unchanged.
        "goto": "e19aaa55df91c292c50c64beedf50fb6acd02edf938408126485017f09d6c8d8",
        "set_tracking": "6a4e3bc1832284a4a03ab3945dc08932ac1f0f623b879163a14b42d96bc346fe",
        "get_tracking_state": "f38553b64ab83039b83e4a58287d131e195f2714734ece23c053822a503bfa0d",
        "wait_tracking_state": "3ad71d3aa87200b051dc260890bde7f7d5632ee9c7dfa13af702b5e61a3078b0",
    }
    actual = {
        name: hashlib.sha256(inspect.getsource(getattr(INDITelescopeControl, name)).encode()).hexdigest()
        for name in expected
    }
    assert actual == expected
