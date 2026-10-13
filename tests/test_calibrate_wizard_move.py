"""HI ALTO/HI BAJO real-movement path of the CALIBRATE wizard.

1. observation_orchestrator.active_campaign_reason(): a RUNNING sidecar left behind by a session that ended
   without STOP used to refuse every physical web stage (409) and the operational calibration test's
   no_active_orchestrator_campaign precheck - both on 2026-10-01. The block stays for a live campaign.
2. calibrate_reference_wizard._capture_n_at(): a capture never starts unless the GOTO converged AND the mount
   is still at the target after the settle time; a failure raises before any SDR capture.

Fakes only: no INDI server, mount or SDR is touched."""
import asyncio
import sys
import types

import pytest

import observation_orchestrator as oo
import calibrate_reference_wizard as wiz
from indi_telescope_control import INDITelescopeControl

_real_distance = INDITelescopeControl._angular_distance_deg


# ------------------------------------------------------------------ active_campaign_reason (shared gate rule)
def _orch(**over):
    base = {"orchestrator_state": "RUNNING", "session_id": "20260928_024636", "capture_pid": 3364691,
            "capture_start_time": 1, "quicklook_pid": 3364837, "quicklook_start_time": 2}
    base.update(over)
    return base


@pytest.fixture
def alive(monkeypatch):
    def set_alive(capture=False, quicklook=False):
        monkeypatch.setattr(oo, "_capture_ownership_matches", lambda r: capture)
        monkeypatch.setattr(oo, "_quicklook_ownership_matches", lambda r: quicklook)
    return set_alive


def test_stale_running_sidecar_is_not_active(alive):
    alive(capture=False, quicklook=False)
    assert oo.active_campaign_reason(_orch()) is None
    assert oo.active_campaign_reason(_orch(orchestrator_state="STOPPING")) is None


def test_live_capture_or_live_quicklook_keeps_the_block(alive):
    alive(capture=True)
    assert "capture.py" in oo.active_campaign_reason(_orch())
    alive(capture=False, quicklook=True)    # quicklook cycles rtl_tcp between points: still a campaign
    assert "quicklook_live" in oo.active_campaign_reason(_orch())
    alive(capture=True)    # DEGRADED: capture.py still runs, only its announcement/quicklook is missing
    assert "capture.py" in oo.active_campaign_reason(_orch(orchestrator_state="DEGRADED"))


def test_states_without_a_capture_process_fail_closed(alive):
    alive(capture=False, quicklook=False)
    assert oo.active_campaign_reason(_orch(orchestrator_state="PREFLIGHT"))
    assert oo.active_campaign_reason(_orch(orchestrator_state="READY"))
    assert "no capture_pid" in oo.active_campaign_reason(_orch(capture_pid=None))
    for st in ("PLANNED", "COMPLETED", "ABORTED", "FAILED", "DEGRADED", None):
        assert oo.active_campaign_reason(_orch(orchestrator_state=st)) is None


def test_real_dead_pids_are_not_active():
    # no monkeypatch: real /proc lookup of PIDs that are not running with that start time
    assert oo.active_campaign_reason(_orch(capture_pid=2**22 - 1, quicklook_pid=2**22 - 2)) is None


# ------------------------------------------------------------------ _capture_n_at
class _FakeTelescope:
    def __init__(self, positions, goto_ok=True, tracking_confirms=True, tracking_drops_after=None):
        self.positions = list(positions)   # successive get_coordinates() answers
        self.goto_ok = goto_ok
        self.gotos = []
        self.last_slew_busy_duration_sec = 12.5
        self._angular_distance_deg = _real_distance
        self.tracking = "off"
        self.tracking_confirms = tracking_confirms
        self.tracking_drops_after = tracking_drops_after   # number of get_tracking_state() calls while ON
        self.tracking_reads_on = 0
        self.track_mode = None
        self.tracking_writes = []

    async def get_tracking_state(self, timeout=1.0):
        if self.tracking == "on":
            self.tracking_reads_on += 1
            if self.tracking_drops_after is not None and self.tracking_reads_on > self.tracking_drops_after:
                return "off"
        return self.tracking
    async def set_track_mode(self, mode):
        self.track_mode = mode
        return True
    async def wait_track_mode(self, expected, timeout=5.0):
        return self.track_mode == expected
    async def set_tracking(self, enable):
        self.tracking_writes.append(enable)
        if enable and not self.tracking_confirms:
            return True
        self.tracking = "on" if enable else "off"
        return True
    async def wait_tracking_state(self, expected_on, timeout=5.0):
        return (self.tracking == "on") == expected_on

    async def connect(self): return True
    async def disconnect(self): return None
    async def goto(self, ra, dec):
        self.gotos.append((ra, dec))
        return self.goto_ok
    async def get_coordinates(self, force_refresh=False):
        return self.positions.pop(0) if len(self.positions) > 1 else self.positions[0]


class _FakeSDR:
    captures = []
    def __init__(self, *a, **k): pass
    async def connect(self): return None
    async def configure(self, *a, **k): return None
    async def close(self): return None
    async def capture(self, seconds, path, sr, meta):
        _FakeSDR.captures.append(path)


@pytest.fixture
def fake_hw(monkeypatch):
    holder = {}

    def install(telescope):
        holder["t"] = telescope
        monkeypatch.setitem(sys.modules, "indi_telescope_control",
                            types.SimpleNamespace(INDITelescopeControl=lambda *a, **k: telescope))
        monkeypatch.setitem(sys.modules, "sdr_capture", types.SimpleNamespace(SDRCapture=_FakeSDR))
        _FakeSDR.captures = []
    return install


def _run(tmp_path, settle=0.0):
    return asyncio.run(wiz._capture_n_at(16.13, -51.55, 40.2, 2, 1.0, settle, 2.4e6, 1420405752.0, tmp_path))


def test_failed_goto_raises_before_any_capture(fake_hw, tmp_path):
    fake_hw(_FakeTelescope([(21.54, -20.43)], goto_ok=False))
    with pytest.raises(RuntimeError, match="NO capture taken"):
        _run(tmp_path)
    assert _FakeSDR.captures == []


def test_mount_off_target_after_settle_raises_before_any_capture(fake_hw, tmp_path):
    # converged per goto(), but the readback after settling is 1 deg away
    fake_hw(_FakeTelescope([(21.54, -20.43), (16.13, -50.55)]))
    with pytest.raises(RuntimeError, match="not at the target after settling"):
        _run(tmp_path)
    assert _FakeSDR.captures == []


def test_arrival_recorded_and_captures_taken_only_at_target(fake_hw, tmp_path):
    t = _FakeTelescope([(21.54, -20.43), (16.1301, -51.551), (16.1302, -51.551)])
    fake_hw(t)
    out = _run(tmp_path)
    assert t.gotos == [(16.13, -51.55)]
    assert len(_FakeSDR.captures) == 2
    assert out["pre_goto_ra_hours"] == 21.54 and out["pre_goto_dec_deg"] == -20.43
    assert out["arrival_error_deg"] < wiz.ARRIVAL_TOLERANCE_DEG
    assert out["slew_distance_deg"] > 50
    assert out["post_capture_ra_hours"] == 16.1302


def test_tracking_not_confirmed_takes_no_capture(fake_hw, tmp_path):
    fake_hw(_FakeTelescope([(21.54, -20.43), (16.1301, -51.551)], tracking_confirms=False))
    with pytest.raises(RuntimeError, match="tracking ON not confirmed"):
        _run(tmp_path)
    assert _FakeSDR.captures == []


def test_target_lost_mid_hold_stops_the_captures(fake_hw, tmp_path):
    # tracking reads "on" for the first 2 samples, then the mount reports it off
    t = _FakeTelescope([(21.54, -20.43), (16.1301, -51.551)], tracking_drops_after=2)
    fake_hw(t)
    with pytest.raises(RuntimeError, match="target NOT held"):
        _run(tmp_path)
    assert len(_FakeSDR.captures) == 1
    assert t.tracking_writes[-1] is False          # tracking restored OFF even on failure


def test_hold_samples_recorded_and_tracking_restored(fake_hw, tmp_path):
    t = _FakeTelescope([(21.54, -20.43), (16.1301, -51.551)])
    fake_hw(t)
    out = _run(tmp_path)
    assert t.track_mode == "sidereal"
    assert t.tracking_writes == [True, False]
    tr = out["tracking"]
    assert tr["mode"] == "sidereal" and tr["state_before_goto_hold"] == "off"
    assert len(tr["samples"]) == 4 and all(x["tracking"] == "on" for x in tr["samples"])
    assert tr["max_hold_error_deg"] < wiz.ARRIVAL_TOLERANCE_DEG
