import asyncio
import math

import pytest

from indi_telescope_control import INDITelescopeControl


def controller():
    return INDITelescopeControl(verbose=False)


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

        class RecordingWriter:
            def write(self, _):
                pass

            async def drain(self):
                pass

        class SequencedReader:
            def __init__(self):
                self.responses = [
                    '<setNumberVector device="Telescope Simulator" '
                    'name="EQUATORIAL_EOD_COORD" state="Idle">'
                    '<oneNumber name="RA">1</oneNumber>'
                    '<oneNumber name="DEC">0</oneNumber></setNumberVector>',
                    '<setNumberVector device="Telescope Simulator" '
                    'name="EQUATORIAL_EOD_COORD" state="Busy">'
                    '<oneNumber name="RA">1</oneNumber>'
                    '<oneNumber name="DEC">0</oneNumber></setNumberVector>',
                    '<setNumberVector device="Telescope Simulator" '
                    'name="EQUATORIAL_EOD_COORD" state="Idle">'
                    '<oneNumber name="RA">2</oneNumber>'
                    '<oneNumber name="DEC">0</oneNumber></setNumberVector>',
                    '<setNumberVector device="Telescope Simulator" '
                    'name="EQUATORIAL_EOD_COORD" state="Idle">'
                    '<oneNumber name="RA">2</oneNumber>'
                    '<oneNumber name="DEC">0</oneNumber></setNumberVector>',
                ]

            async def read(self, _):
                return self.responses.pop(0).encode()

        async def fixed_coordinates(force_refresh=False):
            return (1.0, 0.0) if not force_refresh else (2.0, 0.0)

        telescope.writer = RecordingWriter()
        telescope.reader = SequencedReader()
        telescope.get_coordinates = fixed_coordinates

        assert await telescope.goto(2.0, 0.0) is True
        assert telescope.final_target_error_deg == pytest.approx(0.0)

    asyncio.run(run_test())


def test_goto_retries_once_after_idle_just_outside_tolerance():
    async def run_test():
        telescope = controller()

        class RecordingWriter:
            def __init__(self):
                self.messages = []

            def write(self, data):
                self.messages.append(data.decode())

            async def drain(self):
                pass

        class SequencedReader:
            def __init__(self):
                self.responses = [
                    '<setNumberVector name="EQUATORIAL_EOD_COORD" state="Idle">'
                    '<oneNumber name="RA">1</oneNumber><oneNumber name="DEC">0</oneNumber>'
                    '</setNumberVector>',
                    '<setNumberVector name="EQUATORIAL_EOD_COORD" state="Busy">'
                    '<oneNumber name="RA">1</oneNumber><oneNumber name="DEC">0</oneNumber>'
                    '</setNumberVector>',
                    '<setNumberVector name="EQUATORIAL_EOD_COORD" state="Idle">'
                    '<oneNumber name="RA">1.98</oneNumber><oneNumber name="DEC">0</oneNumber>'
                    '</setNumberVector>',
                    '<setNumberVector name="EQUATORIAL_EOD_COORD" state="Busy">'
                    '<oneNumber name="RA">1.98</oneNumber><oneNumber name="DEC">0</oneNumber>'
                    '</setNumberVector>',
                    '<setNumberVector name="EQUATORIAL_EOD_COORD" state="Idle">'
                    '<oneNumber name="RA">2</oneNumber><oneNumber name="DEC">0</oneNumber>'
                    '</setNumberVector>',
                    '<setNumberVector name="EQUATORIAL_EOD_COORD" state="Idle">'
                    '<oneNumber name="RA">2</oneNumber><oneNumber name="DEC">0</oneNumber>'
                    '</setNumberVector>',
                ]

            async def read(self, _):
                return self.responses.pop(0).encode()

        async def fixed_coordinates(force_refresh=False):
            return (1.0, 0.0) if not force_refresh else (2.0, 0.0)

        writer = RecordingWriter()
        telescope.writer = writer
        telescope.reader = SequencedReader()
        telescope.get_coordinates = fixed_coordinates

        assert await telescope.goto(2.0, 0.0) is True
        coordinate_commands = [
            message for message in writer.messages
            if '<newNumberVector' in message and 'name="EQUATORIAL_EOD_COORD"' in message
        ]
        assert len(coordinate_commands) == 2
        assert telescope.final_target_error_deg == pytest.approx(0.0)

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
