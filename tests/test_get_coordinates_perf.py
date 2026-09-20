import asyncio

import pytest

from indi_telescope_control import INDITelescopeControl


def eod_item(seq=1, ra="12.5", dec="-68", state="Ok"):
    return {
        "update_seq": seq,
        "state": state,
        "elements": {"RA": ra, "DEC": dec},
        "raw": "<setNumberVector name=\"EQUATORIAL_EOD_COORD\"/>",
    }


@pytest.mark.asyncio
async def test_cache_available_without_force_refresh_uses_latest_snapshot():
    control = INDITelescopeControl(device_name="LX200 OnStep")
    key = (control.device_name, "EQUATORIAL_EOD_COORD")
    control._property_cache[key] = eod_item(seq=9001)
    control.cached_properties = "old history that must not be parsed" * 10000

    async def unexpected_send(*args, **kwargs):
        raise AssertionError("cache hit must not request INDI")

    control._send_command = unexpected_send
    assert await control.get_coordinates(force_refresh=False) == (12.5, -68.0)


@pytest.mark.asyncio
async def test_cache_absent_requests_and_waits_after_zero():
    control = INDITelescopeControl(device_name="LX200 OnStep")
    calls = {}

    async def send(xml):
        calls["xml"] = xml

    async def wait(name, timeout, after_seq, predicate, wait_metrics=None):
        calls["wait"] = (name, timeout, after_seq)
        item = eod_item(seq=1)
        assert predicate(item)
        return item

    control._send_command = send
    control._wait_property = wait
    assert await control.get_coordinates(False) == (12.5, -68.0)
    assert calls["wait"] == ("EQUATORIAL_EOD_COORD", 5.0, 0)


@pytest.mark.asyncio
async def test_force_refresh_uses_current_baseline_and_newer_update():
    control = INDITelescopeControl(device_name="LX200 OnStep")
    key = (control.device_name, "EQUATORIAL_EOD_COORD")
    control._property_cache[key] = eod_item(seq=123, ra="1", dec="2")
    control._property_history[key] = [eod_item(seq=n) for n in range(24, 124)]
    observed = {}

    async def send(xml):
        observed["sent"] = True

    async def wait(name, timeout, after_seq, predicate, wait_metrics=None):
        observed["baseline"] = after_seq
        return eod_item(seq=124, ra="3", dec="4")

    control._send_command = send
    control._wait_property = wait
    assert await control.get_coordinates(True) == (3.0, 4.0)
    assert observed == {"sent": True, "baseline": 123}


@pytest.mark.asyncio
async def test_stale_large_history_is_not_replayed():
    control = INDITelescopeControl(device_name="LX200 OnStep")
    key = (control.device_name, "EQUATORIAL_EOD_COORD")
    control._property_cache[key] = eod_item(seq=10000, ra="7", dec="8")
    control._property_history[key] = [eod_item(seq=n) for n in range(1, 10001)]
    assert await control.get_coordinates(False) == (7.0, 8.0)


@pytest.mark.asyncio
async def test_force_refresh_timeout_returns_none():
    control = INDITelescopeControl(device_name="LX200 OnStep")
    control._property_cache[(control.device_name, "EQUATORIAL_EOD_COORD")] = eod_item(4)
    control._send_command = lambda xml: asyncio.sleep(0)

    async def timeout(*args, **kwargs):
        raise asyncio.TimeoutError

    control._wait_property = timeout
    assert await control.get_coordinates(True) == (None, None)


@pytest.mark.asyncio
async def test_socket_or_reader_error_returns_none():
    control = INDITelescopeControl(device_name="LX200 OnStep")

    async def broken_send(xml):
        raise ConnectionError("reader failed")

    control._send_command = broken_send
    assert await control.get_coordinates(True) == (None, None)
