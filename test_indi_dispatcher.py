import asyncio

import pytest

from indi_telescope_control import INDITelescopeControl


class QueueReader:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.active = 0
        self.max_active = 0

    async def read(self, _):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            return await self.queue.get()
        finally:
            self.active -= 1


class Writer:
    def __init__(self):
        self.messages = []
        self.drains = 0
        self.max_drains = 0

    def write(self, data):
        self.messages.append(data.decode())

    async def drain(self):
        self.drains += 1
        self.max_drains = max(self.max_drains, self.drains)
        await asyncio.sleep(0.01)
        self.drains -= 1

    def close(self):
        pass

    async def wait_closed(self):
        pass


def control():
    c = INDITelescopeControl(device_name="Scope")
    c.reader, c.writer = QueueReader(), Writer()
    return c


def vector(name, value="1", device="Scope", tag="setTextVector"):
    child = "oneText" if tag.startswith("set") else "defText"
    return (f'<{tag} device="{device}" name="{name}" state="Ok">'
            f'<{child} name="VALUE">{value}</{child}></{tag}>').encode()


@pytest.mark.asyncio
async def test_one_reader_routes_interleaved_properties_to_two_waiters():
    c = control()
    a = asyncio.create_task(c._wait_property("A", 1, 0))
    b = asyncio.create_task(c._wait_property("B", 1, 0))
    await c.reader.queue.put(vector("B", "two") + vector("A", "one"))
    assert (await a)["elements"]["VALUE"] == "one"
    assert (await b)["elements"]["VALUE"] == "two"
    assert c.reader.max_active == 1
    await c.disconnect()


@pytest.mark.asyncio
async def test_fragmented_xml_and_multiple_vectors_are_preserved():
    c = control()
    a = asyncio.create_task(c._wait_property("A", 1, 0))
    b = asyncio.create_task(c._wait_property("B", 1, 0))
    payload = vector("A") + vector("B")
    await c.reader.queue.put(payload[:31])
    await c.reader.queue.put(payload[31:])
    assert (await a)["update_seq"] == 1 and (await b)["update_seq"] == 1
    await c.disconnect()


@pytest.mark.asyncio
async def test_timeout_and_cancelled_waiter_do_not_kill_dispatcher():
    c = control()
    with pytest.raises(asyncio.TimeoutError):
        await c._wait_property("missing", 0.01, 0)
    cancelled = asyncio.create_task(c._wait_property("cancel", 1, 0))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    live = asyncio.create_task(c._wait_property("live", 1, 0))
    await c.reader.queue.put(vector("live"))
    assert (await live)["elements"]["VALUE"] == "1"
    await c.disconnect()


@pytest.mark.asyncio
async def test_socket_close_wakes_waiter():
    c = control()
    waiter = asyncio.create_task(c._wait_property("A", 1, 0))
    await c.reader.queue.put(b"")
    with pytest.raises(ConnectionError):
        await waiter
    await c.disconnect()


@pytest.mark.asyncio
async def test_concurrent_writes_are_serialized():
    c = control()
    await asyncio.gather(c._send_command("<one/>"), c._send_command("<two/>"))
    assert c.writer.max_drains == 1
    assert c.writer.messages == ["<one/>\n", "<two/>\n"]


@pytest.mark.asyncio
async def test_identical_sets_increment_but_definition_is_distinct():
    c = control()
    await c._ensure_dispatcher()
    await c.reader.queue.put(vector("S") + vector("S") + vector("S", tag="defTextVector"))
    await asyncio.sleep(0.01)
    history = c._property_history[("Scope", "S")]
    assert [item["update_seq"] for item in history] == [1, 2, 3]
    assert [item["tag"] for item in history] == ["setTextVector", "setTextVector", "defTextVector"]
    await c.disconnect()
