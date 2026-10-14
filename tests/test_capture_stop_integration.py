"""STOP OBSERVATION end to end: the real capture.py as a real subprocess, stopped with a real SIGINT, against a
simulated INDI mount that keeps slewing and tracking (tests/support/fake_indi_server.py) and a simulated rtl_tcp.

Why this file exists: until 2026-10-13 every stop test replaced exactly the part that failed in the instrument
(process liveness, the mount). Here nothing of the stop path is replaced - signal handling, the session loop, the
GOTO, mount_stop's INDI readback, the end-of-session announcement and the HDF5 writes are the production code. The
only stand-in inside capture.py's process is the rtl_tcp.service journal acknowledgement (see the launcher).

Everything lives under tmp_path: no data/, no hardware, no system services.
"""
import asyncio
import csv
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from indi_telescope_control import INDITelescopeControl
from sdr_capture import validate_hdf5_capture
from tests.support import stop_workspace as W

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "tests" / "support" / "run_capture_for_stop_tests.py"
DEVICE = "LX200 OnStep"
RATE = 250_000


class Session:
    def __init__(self, tmp_path, *indi_flags, capture_seconds=3.0, n_points=4, slew_seconds=4.0):
        self.dir = tmp_path / "work"
        self.plan = W.write_plan(self.dir, n_points)
        self.journal = self.dir / "indi.jsonl"
        self.indi = W.Listening("fake_indi_server.py", "--journal", str(self.journal), "--device", DEVICE,
                                "--slew-seconds", str(slew_seconds), *indi_flags)
        self.rtl = W.Listening("fake_rtl_tcp.py")
        self.capture_seconds = capture_seconds
        self.log = self.dir / "capture.log"
        self.proc = None
        self.stop_index = None

    def start(self):
        cmd = [sys.executable, str(LAUNCHER), "--csv", str(self.plan), "--settle", "0.5", "--capture", str(self.capture_seconds),
               "--host", "127.0.0.1", "--port", str(self.indi.port), "--device", DEVICE,
               "--sdr-host", "127.0.0.1", "--sdr-port", str(self.rtl.port), "--sdr-rate", str(RATE), "--sdr-freq", "1420405752",
               "--runtime-dir", str(self.dir / "runtime")]
        self.proc = subprocess.Popen(cmd, cwd=self.dir, stdout=self.log.open("w"), stderr=subprocess.STDOUT)
        return self

    def text(self):
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def wait_log(self, needle, count=1, timeout=60.0):
        ok = W.wait_for(lambda: self.text().count(needle) >= count, timeout)
        assert ok, f"never saw {count} x {needle!r} in capture.log:\n{self.text()[-1500:]}"

    def sigint(self):
        if self.stop_index is None:
            self.stop_index = len(W.journal_commands(self.journal))
        os.kill(self.proc.pid, signal.SIGINT)

    def wait_exit(self, timeout=90.0):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise AssertionError(f"capture.py did not exit within {timeout} s of SIGINT:\n{self.text()[-2000:]}")

    # ---- evidence
    def stop_report(self):
        return json.loads((self.dir / "stop_report.json").read_text())

    def announced(self):
        return json.loads((self.dir / "runtime" / "current_session.json").read_text())

    def statuses(self):
        return [r["capture_status"] for r in csv.DictReader(self.plan.open())]

    def h5(self):
        return sorted((self.dir / "data").rglob("*.h5"))

    def parts(self):
        return sorted(self.dir.rglob("*.part"))

    def commands_after_stop(self):
        return [c["name"] for c in W.journal_commands(self.journal)[self.stop_index:]]

    def mount_now(self):
        """What the simulated mount says after everything ended - read by a fresh client, with a second read 5 s
        later: a stopped mount shows RA advancing at the sidereal rate; a tracking one shows it constant."""
        async def read():
            t = INDITelescopeControl("127.0.0.1", self.indi.port, DEVICE, False)
            t.compact_console = True
            assert await t.connect()
            try:
                tracking = await t.get_tracking_state(timeout=2.0)
                ra1, _ = await t.get_coordinates(force_refresh=True)
                state = t._property_cache[(DEVICE, "EQUATORIAL_EOD_COORD")]["state"]
                await asyncio.sleep(5.0)
                ra2, _ = await t.get_coordinates(force_refresh=True)
                return tracking, state, (ra2 - ra1) * 3600.0
            finally:
                await t.disconnect()
        return asyncio.run(read())

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.indi.close()
        self.rtl.close()


@pytest.fixture
def session(tmp_path):
    made = []

    def make(*flags, **kw):
        s = Session(tmp_path, *flags, **kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def _assert_data_intact(s, expected_complete):
    files = s.h5()
    assert len(files) == expected_complete, [f.name for f in files]
    for f in files:
        validate_hdf5_capture(f)                                   # every .h5 left behind is a complete capture
    assert s.statuses().count("success") == expected_complete


def _assert_stopped_and_proved(s, phase):
    report, announced = s.stop_report(), s.announced()
    assert report["phase_at_stop"] == phase, report["phase_at_stop"]
    assert report["mount_stop"]["confirmed"] is True, report["mount_stop"].get("reason")
    assert announced["event"] == "SESSION_ABORTED" and announced["state"] == "ABORTED"       # published by capture.py itself
    assert announced["mount_stop"]["confirmed"] is True and announced["error"] is None
    after = s.commands_after_stop()
    assert "EQUATORIAL_EOD_COORD" not in after, "a GOTO was sent after the STOP"
    assert "TELESCOPE_PARK" not in after and "TELESCOPE_HOME" not in after
    assert "TELESCOPE_TRACK_STATE" in after
    tracking, eod_state, ra_drift_s = s.mount_now()
    assert tracking == "off" and eod_state != "Busy"
    assert ra_drift_s > 2.5, f"RA advanced only {ra_drift_s:.1f} s in 5 s: the simulated mount is still tracking"
    return report


def test_stop_during_a_goto_stops_the_mount_and_it_does_not_resume_tracking(session):
    """The case that failed on the instrument on 2026-10-13: the mount finished the GOTO and kept tracking."""
    s = session(slew_seconds=12.0).start()
    s.wait_log("✓ POINT 001")                       # tracking is ON from point 1, as in the real session
    s.wait_log("MOTION    START", count=2)          # point 2's slew is under way
    time.sleep(1.0)
    s.sigint()
    assert s.wait_exit() == 130
    report = _assert_stopped_and_proved(s, "GOTO")
    assert report["mount_stop"]["abort"] == {"supported": True, "sent": True}
    assert report["interrupted_point"] == 2
    _assert_data_intact(s, expected_complete=1)
    assert report["point_status_counts"].get("success") == 1          # counted from the plan on disk
    assert s.parts() == [] and report["part_files_left_untouched"] == []
    assert "TELESCOPE_ABORT_MOTION" in s.commands_after_stop()
    assert s.statuses()[1] == "planned"             # the interrupted point was never captured, and is not "success"


def test_stop_during_a_goto_without_abort_waits_for_the_slew_then_switches_tracking_off(session):
    """A driver with no abort: the slew completes AFTER the stop and restores tracking - the late activation."""
    s = session("--no-abort", slew_seconds=6.0).start()
    s.wait_log("✓ POINT 001")
    s.wait_log("MOTION    START", count=2)
    time.sleep(0.5)
    s.sigint()
    assert s.wait_exit() == 130
    report = _assert_stopped_and_proved(s, "GOTO")
    assert report["mount_stop"]["abort"]["supported"] is False
    _assert_data_intact(s, expected_complete=1)


def test_stop_during_a_capture_leaves_no_file_for_that_point(session):
    s = session(capture_seconds=6.0).start()
    s.wait_log("SDR      FLUSH")                    # the capture of point 1 starts right after the flush
    time.sleep(2.0)
    s.sigint()
    assert s.wait_exit() == 130
    report = _assert_stopped_and_proved(s, "CAPTURE")
    assert report["interrupted_point"] == 1 and "no file" in report["interrupted_point_outcome"]
    _assert_data_intact(s, expected_complete=0)
    assert s.parts() == []


def test_stop_at_the_write_boundary_never_leaves_a_half_written_capture(session):
    """SIGINT as the capture hands over to the HDF5 write. Whichever side of the write it lands on, what is on disk
    must be consistent: complete validated .h5 files only, each counted as success, and no .part."""
    s = session(capture_seconds=1.0).start()
    s.wait_log("SDR      CAPTURE   OK")
    s.sigint()
    assert s.wait_exit() == 130
    report = s.stop_report()
    assert report["mount_stop"]["confirmed"] is True
    files = s.h5()
    for f in files:
        validate_hdf5_capture(f)
    assert s.statuses().count("success") == len(files) and len(files) in (0, 1, 2)
    assert s.parts() == [] and report["part_files_left_untouched"] == []
    assert s.announced()["state"] == "ABORTED"


def test_stop_while_a_finished_session_is_closing_keeps_its_completed_state(session):
    s = session(n_points=1, capture_seconds=1.0).start()
    s.wait_log("SESSION COMPLETE")
    s.sigint()
    code = s.wait_exit()
    assert code in (0, 130)
    announced = s.announced()
    assert announced["event"] == "SESSION_COMPLETED" and announced["state"] == "COMPLETED"
    _assert_data_intact(s, expected_complete=1)
    tracking, eod_state, ra_drift_s = s.mount_now()
    assert tracking == "off" and ra_drift_s > 2.5
    if (s.dir / "stop_report.json").exists():       # the STOP landed inside the closing sequence
        assert s.stop_report()["session_had_already_ended"] is True


def test_repeated_stop_does_not_cut_the_stop_sequence_short(session):
    """Clicking STOP again (a second SIGINT) used to raise KeyboardInterrupt in the middle of the cleanup."""
    s = session(slew_seconds=12.0).start()
    s.wait_log("✓ POINT 001")
    s.wait_log("MOTION    START", count=2)
    time.sleep(1.0)
    s.sigint()
    time.sleep(1.0)
    s.sigint()
    time.sleep(0.5)
    s.sigint()
    assert s.wait_exit() == 130
    _assert_stopped_and_proved(s, "GOTO")
    assert "SIGINT #2 ignored" in s.text() and "SIGINT #3 ignored" in s.text()
    _assert_data_intact(s, expected_complete=1)


@pytest.mark.parametrize("flag,expected", [
    ("--freeze-after-stop", "INDI did not answer"),
    ("--drop-after-stop", ""),
    ("--ignore-track-off", "tracking is on after"),
])
def test_a_stop_that_cannot_be_proved_is_published_as_parada_no_confirmada(session, flag, expected):
    """Lost INDI, a dead connection, a mount that ignores tracking OFF: capture.py ends, and says the mount stop
    was NOT confirmed - it never reports success for having sent commands or having exited."""
    s = session(flag, slew_seconds=12.0).start()
    s.wait_log("✓ POINT 001")
    s.wait_log("MOTION    START", count=2)
    time.sleep(1.0)
    s.sigint()
    assert s.wait_exit() == 130
    report, announced = s.stop_report(), s.announced()
    assert report["mount_stop"]["confirmed"] is False
    assert expected in report["mount_stop"]["reason"]
    assert announced["state"] == "ABORTED" and announced["mount_stop"]["confirmed"] is False
    assert announced["error"].startswith("PARADA NO CONFIRMADA")
    assert "PARADA NO CONFIRMADA" in s.text()
    after = s.commands_after_stop()
    assert "EQUATORIAL_EOD_COORD" not in after and "TELESCOPE_PARK" not in after and "TELESCOPE_HOME" not in after
    _assert_data_intact(s, expected_complete=1)


def test_a_slew_that_outlasts_the_time_limit_is_not_confirmed(session, monkeypatch):
    """No abort and a very long slew: the wait is bounded and the result is NOT CONFIRMED, with the reason."""
    s = session("--no-abort", slew_seconds=600.0)
    s.start_env = {**os.environ, "ALMITA_STOP_TEST_MOTION_TIMEOUT_S": "4"}
    cmd_env = s.start_env
    # the launcher shortens mount_stop's own limit through this variable (tests only)
    cmd = [sys.executable, str(LAUNCHER), "--csv", str(s.plan), "--settle", "0.5", "--capture", "2", "--host", "127.0.0.1",
           "--port", str(s.indi.port), "--device", DEVICE, "--sdr-host", "127.0.0.1", "--sdr-port", str(s.rtl.port),
           "--sdr-rate", str(RATE), "--sdr-freq", "1420405752", "--runtime-dir", str(s.dir / "runtime")]
    s.proc = subprocess.Popen(cmd, cwd=s.dir, stdout=s.log.open("w"), stderr=subprocess.STDOUT, env=cmd_env)
    s.wait_log("MOTION    START")
    time.sleep(1.0)
    s.sigint()
    assert s.wait_exit() == 130
    report = s.stop_report()
    assert report["mount_stop"]["confirmed"] is False and "still slewing after 4 s" in report["mount_stop"]["reason"]
    assert s.announced()["error"].startswith("PARADA NO CONFIRMADA")
