"""mount_control.py (manual CLI): connect() is awaited and every action runs on the connected client in one loop.
A fake controller only - no INDI, no mount."""
import sys

import pytest

import mount_control


class FakeController:
    def __init__(self, connected=True):
        self.calls, self._connected = [], connected

    async def connect(self):
        self.calls.append("connect")
        return self._connected

    async def get_coordinates(self):
        self.calls.append("get_coordinates")
        return 5.5, -33.4

    async def goto(self, ra, dec):
        self.calls.append(("goto", ra, dec))
        return True

    async def sync(self, ra, dec):
        self.calls.append(("sync", ra, dec))
        return True

    async def set_tracking(self, on):
        self.calls.append(("track", on))
        return True

    async def disconnect(self):
        self.calls.append("disconnect")


def _run(monkeypatch, argv, fake):
    monkeypatch.setattr(sys, "argv", ["mount_control.py", *argv])
    monkeypatch.setattr(mount_control, "INDITelescopeControl", lambda **k: fake)
    with pytest.raises(SystemExit) as exc:
        mount_control.main()
    return exc.value.code


def test_status_awaits_connect_prints_coordinates_and_disconnects(monkeypatch, capsys):
    fake = FakeController()
    assert _run(monkeypatch, ["--status"], fake) == 0
    assert fake.calls == ["connect", "get_coordinates", "disconnect"]
    assert "RA 5.5 h  DEC -33.4 deg" in capsys.readouterr().out


def test_goto_runs_on_the_connected_client(monkeypatch):
    fake = FakeController()
    assert _run(monkeypatch, ["--goto", "05:30:00", "-33.4"], fake) == 0      # negative Dec: decimal (argparse reads -33:24:00 as an option)
    assert fake.calls == ["connect", ("goto", 5.5, -33.4), "disconnect"]


def test_a_failed_connection_moves_nothing(monkeypatch):
    fake = FakeController(connected=False)
    assert _run(monkeypatch, ["--goto", "5.5", "-33.4"], fake) == 1
    assert fake.calls == ["connect"]


# ------------------------------------------------------------------ southern declinations on the command line
@pytest.mark.parametrize("dec_text,expected", [("-33:27:00", -33.45), ("-33.4", -33.4), ("+38:47:01", 38.78361), ("-0:30:00", -0.5)])
def test_goto_accepts_a_negative_declination_in_sexagesimal_and_decimal(dec_text, expected):
    """A site at -33 deg points south all the time: "-33:27:00" used to be rejected by argparse as an option."""
    args = mount_control.build_parser().parse_args(["--goto", "18:36:56.3", dec_text])
    ra, dec = mount_control.parse_coordinates(*args.goto)
    assert ra == pytest.approx(18.61564, abs=1e-4) and dec == pytest.approx(expected, abs=1e-4)


def test_the_wider_negative_number_rule_does_not_swallow_real_options():
    parser = mount_control.build_parser()
    assert parser.parse_args(["--status", "-v"]).verbose is True
    with pytest.raises(SystemExit):
        parser.parse_args(["--goto", "18:36:56.3", "--status"])     # an option is still not a declination
