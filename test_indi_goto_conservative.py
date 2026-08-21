import asyncio
import math

import pytest

from indi_telescope_control import INDITelescopeControl


def controller():
    return INDITelescopeControl(verbose=False)


def eod_vector(state, ra, dec, device="Telescope Simulator"):
    return (
        f'<setNumberVector device="{device}" name="EQUATORIAL_EOD_COORD" state="{state}">'
        f'<oneNumber name="RA">{ra}</oneNumber><oneNumber name="DEC">{dec}</oneNumber>'
        '</setNumberVector>'
    ).encode()


class QueryDrivenReader:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def read(self, _):
        return await self.queue.get()


class QueryDrivenWriter:
    def __init__(self, reader, query_responses):
        self.reader = reader
        self.query_responses = list(query_responses)
        self.messages = []

    def write(self, data):
        message = data.decode()
        self.messages.append(message)
        if '<getProperties' in message and 'EQUATORIAL_EOD_COORD' in message and self.query_responses:
            self.reader.queue.put_nowait(self.query_responses.pop(0))

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


def test_angular_distance_same_position():
    assert controller()._angular_distance_deg(12.0, -30.0, 12.0, -30.0) == pytest.approx(0.0)


def test_angular_distance_wraps_ra_24_hours():
    assert controller()._angular_distance_deg(23.9, 0.0, 0.1, 0.0) == pytest.approx(3.0)


def test_angular_distance_known_quarter_circle():
    assert controller()._angular_distance_deg(0.0, 0.0, 6.0, 0.0) == pytest.approx(90.0)


def test_angular_distance_at_high_declination():
    assert controller()._angular_distance_deg(0.0, 60.0, 12.0, 60.0) == pytest.approx(60.0)


def test_angular_distance_near_poles_is_stable():
    distance = controller()._angular_distance_deg(0.0, 89.999, 12.0, 89.999)
    assert math.isfinite(distance)
    assert distance == pytest.approx(0.002, abs=1e-9)


@pytest.mark.parametrize(
    ("ra", "expected"),
    [(-0.1, 23.9), (24.0, 0.0), (24.1, 0.1), (48.0, 0.0)],
)
def test_ra_normalization(ra, expected):
    assert controller()._validated_goto_coordinates(ra, 10.0) == pytest.approx((expected, 10.0))


@pytest.mark.parametrize(
    ("ra", "dec"),
    [(1.0, -90.1), (1.0, 90.1), (math.nan, 0.0), (1.0, math.inf)],
)
def test_invalid_coordinates_are_rejected(ra, dec):
    assert controller()._validated_goto_coordinates(ra, dec) is None


@pytest.mark.asyncio
async def test_invalid_goto_does_not_write_to_socket():
    telescope = controller()

    class FailingWriter:
        def write(self, _):
            raise AssertionError("No debe escribir al socket")

    telescope.writer = FailingWriter()
    assert await telescope.goto(math.nan, 0.0) is False


def test_goto_accepts_idle_after_stable_convergence():
    async def run_test():
        telescope = controller()

        async def fixed_coordinates(force_refresh=False):
            return (1.0, 0.0) if not force_refresh else (2.0, 0.0)

        telescope.reader = QueryDrivenReader()
        telescope.writer = QueryDrivenWriter(telescope.reader, [
            eod_vector("Idle", 1, 0),
            eod_vector("Busy", 1, 0),
            eod_vector("Idle", 2, 0),
            eod_vector("Idle", 2, 0),
        ])
        telescope.get_coordinates = fixed_coordinates

        assert await telescope.goto(2.0, 0.0) is True
        assert telescope.final_target_error_deg == pytest.approx(0.0)

    asyncio.run(run_test())


def test_goto_retries_once_after_idle_just_outside_tolerance():
    async def run_test():
        telescope = controller()

        async def fixed_coordinates(force_refresh=False):
            return (1.0, 0.0) if not force_refresh else (2.0, 0.0)

        telescope.reader = QueryDrivenReader()
        writer = QueryDrivenWriter(telescope.reader, [
            eod_vector("Idle", 1, 0),
            eod_vector("Busy", 1, 0),
            eod_vector("Idle", 1.98, 0),
            eod_vector("Busy", 1.98, 0),
            eod_vector("Idle", 2, 0),
            eod_vector("Idle", 2, 0),
        ])
        telescope.writer = writer
        telescope.get_coordinates = fixed_coordinates

        assert await telescope.goto(2.0, 0.0) is True
        coordinate_commands = [
            message for message in writer.messages
            if '<newNumberVector' in message and 'name="EQUATORIAL_EOD_COORD"' in message
        ]
        assert len(coordinate_commands) == 2
        assert telescope.final_target_error_deg == pytest.approx(0.0)

    asyncio.run(run_test())


def _seed_eod_cache(telescope, seq, state, ra, dec):
    raw = eod_vector(state, ra, dec).decode()
    item = {
        "tag": "setNumberVector", "state": state, "timestamp": None,
        "received_at": "test", "received_monotonic": 0.0,
        "elements": {"RA": str(ra), "DEC": str(dec)},
        "raw": raw, "update_seq": seq,
    }
    key = (telescope.device_name, "EQUATORIAL_EOD_COORD")
    telescope._property_cache[key] = item
    telescope._property_history[key] = [item]


def test_precheck_ignores_historical_busy_but_requires_fresh_idle():
    async def run_test():
        telescope = controller()
        telescope.reader = QueryDrivenReader()
        telescope.writer = QueryDrivenWriter(telescope.reader, [
            eod_vector("Idle", 1, 0),
            eod_vector("Busy", 1, 0),
            eod_vector("Idle", 2, 0),
            eod_vector("Idle", 2, 0),
        ])
        _seed_eod_cache(telescope, 100, "Busy", 1, 0)
        key = (telescope.device_name, "EQUATORIAL_EOD_COORD")
        historical = []
        for seq in range(1, 101):
            item = dict(telescope._property_cache[key])
            item["update_seq"] = seq
            historical.append(item)
        telescope._property_history[key] = historical

        async def fixed_coordinates(force_refresh=False):
            return (1.0, 0.0) if not force_refresh else (2.0, 0.0)

        telescope.get_coordinates = fixed_coordinates
        started = asyncio.get_running_loop().time()
        assert await telescope.goto(2.0, 0.0) is True
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 2.0, "El precheck no debe reproducir Busy históricos con sleeps"
        await telescope.disconnect()

    asyncio.run(run_test())


def test_precheck_fresh_busy_blocks_new_goto_until_fresh_idle():
    async def run_test():
        telescope = controller()
        telescope.reader = QueryDrivenReader()
        writer = QueryDrivenWriter(telescope.reader, [
            eod_vector("Busy", 1, 0),
            eod_vector("Busy", 1, 0),
            eod_vector("Idle", 1, 0),
            eod_vector("Busy", 1, 0),
            eod_vector("Idle", 2, 0),
            eod_vector("Idle", 2, 0),
        ])
        telescope.writer = writer
        _seed_eod_cache(telescope, 10, "Idle", 1, 0)

        async def fixed_coordinates(force_refresh=False):
            return (1.0, 0.0) if not force_refresh else (2.0, 0.0)

        telescope.get_coordinates = fixed_coordinates
        task = asyncio.create_task(telescope.goto(2.0, 0.0))
        await asyncio.sleep(0.15)
        coordinate_commands = [
            message for message in writer.messages
            if '<newNumberVector' in message and 'name="EQUATORIAL_EOD_COORD"' in message
        ]
        assert coordinate_commands == [], "No debe ordenar GOTO mientras el Busy fresco persiste"
        assert await asyncio.wait_for(task, 2) is True
        await telescope.disconnect()

    asyncio.run(run_test())


def test_goto_initial_cache_equal_target_waits_for_post_command_updates():
    async def run_test():
        telescope = controller()
        telescope.reader = QueryDrivenReader()
        telescope.writer = QueryDrivenWriter(telescope.reader, [eod_vector("Idle", 2, 0)])
        _seed_eod_cache(telescope, 100, "Idle", 2, 0)

        async def fixed_coordinates(force_refresh=False):
            return 2.0, 0.0

        telescope.get_coordinates = fixed_coordinates
        task = asyncio.create_task(telescope.goto(2.0, 0.0))
        await asyncio.sleep(0.65)
        assert not task.done(), "GOTO no debe aceptar el cache seq=100 igual al target"
        telescope.reader.queue.put_nowait(eod_vector("Idle", 1.5, 0))  # seq 101, transit
        telescope.reader.queue.put_nowait(eod_vector("Idle", 2, 0))    # seq 102, hit 1
        telescope.reader.queue.put_nowait(eod_vector("Idle", 2, 0))    # seq 103, hit 2
        assert await asyncio.wait_for(task, 2) is True
        await telescope.disconnect()

    asyncio.run(run_test())


def test_reverse_goto_does_not_accept_stale_previous_endpoint():
    async def run_test():
        telescope = controller()
        telescope.reader = QueryDrivenReader()
        writer = QueryDrivenWriter(telescope.reader, [eod_vector("Idle", 1, 0)])
        telescope.writer = writer
        _seed_eod_cache(telescope, 100, "Idle", 1, 0)  # A

        async def coordinates(force_refresh=False):
            return (2.0, 0.0) if force_refresh else (1.0, 0.0)

        telescope.get_coordinates = coordinates
        outward = asyncio.create_task(telescope.goto(2.0, 0.0))  # A -> B
        await asyncio.sleep(0.65)
        telescope.reader.queue.put_nowait(eod_vector("Idle", 1.5, 0))
        telescope.reader.queue.put_nowait(eod_vector("Idle", 2, 0))
        telescope.reader.queue.put_nowait(eod_vector("Idle", 2, 0))
        assert await asyncio.wait_for(outward, 2) is True

        # Publicación tardía obsoleta de A justo antes de ordenar B -> A.
        telescope.reader.queue.put_nowait(eod_vector("Idle", 1, 0))  # seq 104
        await asyncio.sleep(0.05)
        writer.query_responses.append(eod_vector("Idle", 2, 0))

        async def reverse_coordinates(force_refresh=False):
            return (1.0, 0.0) if force_refresh else (2.0, 0.0)

        telescope.get_coordinates = reverse_coordinates
        reverse = asyncio.create_task(telescope.goto(1.0, 0.0))
        await asyncio.sleep(0.65)
        assert not reverse.done(), "B -> A no debe retornar usando el A stale pre-command"
        telescope.reader.queue.put_nowait(eod_vector("Idle", 1.5, 0))  # seq 105
        telescope.reader.queue.put_nowait(eod_vector("Idle", 1, 0))    # seq 106, hit 1
        telescope.reader.queue.put_nowait(eod_vector("Idle", 1, 0))    # seq 107, hit 2
        assert await asyncio.wait_for(reverse, 2) is True
        await telescope.disconnect()

    asyncio.run(run_test())


def test_goto_without_busy_succeeds_on_two_distinct_fresh_updates():
    async def run_test():
        telescope = controller()
        telescope.reader = QueryDrivenReader()
        telescope.writer = QueryDrivenWriter(telescope.reader, [eod_vector("Idle", 1, 0)])
        _seed_eod_cache(telescope, 10, "Idle", 1, 0)

        async def fixed_coordinates(force_refresh=False):
            return (2.0, 0.0) if force_refresh else (1.0, 0.0)

        telescope.get_coordinates = fixed_coordinates
        task = asyncio.create_task(telescope.goto(2.0, 0.0))
        await asyncio.sleep(0.65)
        telescope.reader.queue.put_nowait(eod_vector("Idle", 1.5, 0))  # seq 11
        telescope.reader.queue.put_nowait(eod_vector("Ok", 2, 0))     # seq 12
        telescope.reader.queue.put_nowait(eod_vector("Idle", 2, 0))   # seq 13
        assert await asyncio.wait_for(task, 2) is True
        assert telescope.last_slew_busy_duration_sec is None
        await telescope.disconnect()

    asyncio.run(run_test())


def test_same_update_seq_cannot_count_as_two_stable_hits():
    async def run_test():
        telescope = controller()
        telescope.reader = QueryDrivenReader()
        telescope.writer = QueryDrivenWriter(telescope.reader, [])
        _seed_eod_cache(telescope, 10, "Idle", 2, 0)
        first = await telescope._wait_property("EQUATORIAL_EOD_COORD", 0.01, 9)
        assert first["update_seq"] == 10
        with pytest.raises(asyncio.TimeoutError):
            await telescope._wait_property("EQUATORIAL_EOD_COORD", 0.01, 10)
        await telescope.disconnect()

    asyncio.run(run_test())


def test_incremental_xml_split_between_reads():
    telescope = controller()
    first = telescope._feed_xml_messages(
        '<setNumberVector device="Mount" name="EQUATORIAL_EOD_COORD" state="Busy">'
        '<oneNumber name="RA">23.9</oneNumber>'
    )
    second = telescope._feed_xml_messages(
        '<oneNumber name="DEC">-20</oneNumber></setNumberVector>'
    )
    assert first == []
    assert telescope._extract_eod_update(second) == ("Busy", 23.9, -20.0)


def test_multiple_vectors_in_one_read_and_attribute_order():
    telescope = controller()
    messages = telescope._feed_xml_messages(
        '<setNumberVector state="Ok" name="OTHER" device="Mount">'
        '<oneNumber name="X">1</oneNumber></setNumberVector>'
        '<setNumberVector state="Ok" device="Mount" timestamp="now" '
        'name="EQUATORIAL_EOD_COORD">'
        '<oneNumber format="%g" name="DEC">12.5</oneNumber>'
        '<oneNumber label="RA" name="RA">4.25</oneNumber></setNumberVector>'
    )
    assert len(messages) == 2
    assert telescope._extract_eod_update(messages) == ("Ok", 4.25, 12.5)


def test_final_eod_error_uses_spherical_distance():
    telescope = controller()
    error = telescope._final_eod_error_deg(23.9, 0.0, 0.1, 0.0)
    assert error == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("ra", "dec"),
    [(None, None), (1.0, None), (math.nan, 0.0), (1.0, 91.0)],
)
def test_invalid_final_read_has_no_fake_error(ra, dec):
    assert controller()._final_eod_error_deg(1.0, 2.0, ra, dec) is None
