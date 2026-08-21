import csv

import pytest

import capture
from test_capture_visibility_tracking import (
    FakeSDR, FakeTelescope, csv_rows, make_executor, visibility_map,
)


def status(state="healthy", message="None", *, fresh=True, hardware_fresh=True, received_at="2026-08-20T00:00:00+00:00", reason=None, update_seq=1, source=None):
    return {
        "state": state, "message": message, "is_error": state in ("error", "alert"),
        "vector_state": "Alert" if state == "alert" else "Ok",
        "timestamp": received_at, "received_at": received_at, "fresh": fresh,
        "hardware_fresh": hardware_fresh,
        "source": source or ("indi_poll" if hardware_fresh else "indi_cached"),
        "update_seq": update_seq,
        "reason": reason, "elements": {"Error": message or ""}, "raw": "<status/>",
    }


@pytest.fixture(autouse=True)
def fake_sdr(monkeypatch):
    monkeypatch.setattr(capture, "SDRCapture", FakeSDR)


def prepared(tmp_path, count=2, telescope=None):
    executor = make_executor(tmp_path, count=count)
    visibility_map(executor, {point: 40 for point in range(1, count + 1)})
    executor.telescope = telescope or FakeTelescope()
    return executor


@pytest.mark.asyncio
async def test_pre_goto_healthy_allows_goto(tmp_path):
    executor = prepared(tmp_path, count=1)
    assert await executor.execute_observation_plan(0, 0)
    assert executor.telescope.events.count("goto") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["error", "alert"])
async def test_pre_goto_critical_status_pauses_with_zero_goto_and_keeps_planned(tmp_path, blocked):
    telescope = FakeTelescope(onstep_statuses=[status(blocked, "Motor/Driver Fault")])
    executor = prepared(tmp_path / blocked, telescope=telescope)
    assert not await executor.execute_observation_plan(0, 0)
    assert telescope.events.count("goto") == 0
    assert [row["capture_status"] for row in csv_rows(executor)] == ["planned", "planned"]
    assert "pause" in executor.session_manager.actions


@pytest.mark.asyncio
async def test_single_unknown_is_retried_once_then_healthy_continues(tmp_path):
    telescope = FakeTelescope(onstep_statuses=[
        status("unknown", None, fresh=False, received_at=None, reason="timeout"),
        status(), status(), status(),
    ])
    executor = prepared(tmp_path, count=1, telescope=telescope)
    assert await executor.execute_observation_plan(0, 0)
    assert telescope.events.count("onstep") == 4
    assert telescope.events.count("goto") == 1


@pytest.mark.asyncio
async def test_two_unknowns_pause_before_goto(tmp_path):
    unknown = status("unknown", None, fresh=False, hardware_fresh=False, received_at=None, reason="timeout", update_seq=None)
    telescope = FakeTelescope(onstep_statuses=[unknown, unknown])
    executor = prepared(tmp_path, telescope=telescope)
    assert not await executor.execute_observation_plan(0, 0)
    assert telescope.events.count("onstep") == 2 and "goto" not in telescope.events
    assert [row["capture_status"] for row in csv_rows(executor)] == ["planned", "planned"]


@pytest.mark.asyncio
async def test_cached_healthy_cannot_authorize_without_hardware_update(tmp_path):
    timeout = status("unknown", None, fresh=False, hardware_fresh=False, received_at=None, reason="timeout", update_seq=None)
    telescope = FakeTelescope(
        cached_onstep_status=status(hardware_fresh=False),
        onstep_statuses=[timeout, timeout],
    )
    executor = prepared(tmp_path, count=1, telescope=telescope)
    assert not await executor.execute_observation_plan(0, 0)
    row = csv_rows(executor)[0]
    assert telescope.events.count("goto") == 0 and "pause" in executor.session_manager.actions
    assert row["capture_status"] == "planned"
    assert row["onstep_message_pre_goto"] == ""
    assert row["onstep_pre_goto_hardware_fresh"] == "false"


@pytest.mark.asyncio
async def test_cached_error_followed_by_hardware_healthy_allows_goto(tmp_path):
    telescope = FakeTelescope(cached_onstep_status=status("error", "Meridian Limit (W) Exceeded", hardware_fresh=False))
    executor = prepared(tmp_path, count=1, telescope=telescope)
    assert await executor.execute_observation_plan(0, 0)
    assert telescope.events.count("goto") == 1 and "onstep_cached" not in telescope.events


@pytest.mark.asyncio
async def test_cached_healthy_followed_by_hardware_error_blocks_goto(tmp_path):
    telescope = FakeTelescope(
        cached_onstep_status=status(hardware_fresh=False),
        onstep_statuses=[status("error", "Meridian Limit (W) Exceeded")],
    )
    executor = prepared(tmp_path, count=1, telescope=telescope)
    assert not await executor.execute_observation_plan(0, 0)
    assert "goto" not in telescope.events


@pytest.mark.asyncio
async def test_post_goto_hardware_timeout_prevents_tracking(tmp_path):
    timeout = status("unknown", None, fresh=False, hardware_fresh=False, received_at=None, reason="timeout", update_seq=None)
    telescope = FakeTelescope(onstep_statuses=[status(), timeout, timeout])
    executor = prepared(tmp_path, count=2, telescope=telescope)
    assert not await executor.execute_observation_plan(0, 0)
    assert telescope.events.count("goto") == 1 and "set_on" not in telescope.events
    assert [row["capture_status"] for row in csv_rows(executor)] == ["failed", "planned"]


@pytest.mark.asyncio
async def test_post_tracking_hardware_timeout_pauses(tmp_path):
    timeout = status("unknown", None, fresh=False, hardware_fresh=False, received_at=None, reason="timeout", update_seq=None)
    telescope = FakeTelescope(onstep_statuses=[status(update_seq=4), status(update_seq=5), timeout])
    executor = prepared(tmp_path, count=1, telescope=telescope)
    assert not await executor.execute_observation_plan(0, 0)
    assert csv_rows(executor)[0]["onstep_post_tracking_hardware_fresh"] == "false"


@pytest.mark.asyncio
async def test_hardware_sequence_is_persisted_for_all_guard_phases(tmp_path):
    telescope = FakeTelescope(onstep_statuses=[status(update_seq=11), status(update_seq=12), status(update_seq=13)])
    executor = prepared(tmp_path, count=1, telescope=telescope)
    assert await executor.execute_observation_plan(0, 0)
    row = csv_rows(executor)[0]
    assert [row[f"onstep_{phase}_update_seq"] for phase in ("pre_goto", "post_goto", "post_tracking")] == ["11", "12", "13"]
    assert all(row[f"onstep_{phase}_hardware_fresh"] == "true" for phase in ("pre_goto", "post_goto", "post_tracking"))


@pytest.mark.asyncio
async def test_post_goto_error_prevents_tracking_and_pauses(tmp_path):
    telescope = FakeTelescope(onstep_statuses=[status(), status("error", "Dec Limit Exceeded")])
    executor = prepared(tmp_path, telescope=telescope)
    assert not await executor.execute_observation_plan(0, 0)
    assert telescope.events.count("goto") == 1 and "get_tracking" not in telescope.events
    assert csv_rows(executor)[0]["capture_status"] == "failed"
    assert csv_rows(executor)[1]["capture_status"] == "planned"


@pytest.mark.asyncio
async def test_healthy_post_goto_and_tracking_capture_normally(tmp_path):
    executor = prepared(tmp_path, count=1)
    assert await executor.execute_observation_plan(0, 0)
    row = csv_rows(executor)[0]
    assert row["capture_status"] == "success"
    assert row["onstep_state_post_goto"] == "healthy"
    assert row["onstep_state_post_tracking"] == "healthy"


@pytest.mark.asyncio
async def test_tracking_failure_with_healthy_onstep_pauses_after_one_goto(tmp_path):
    telescope = FakeTelescope(state="off", wait_on=False)
    executor = prepared(tmp_path, telescope=telescope)
    assert not await executor.execute_observation_plan(0, 0)
    assert telescope.events.count("goto") == 1
    assert [row["capture_status"] for row in csv_rows(executor)] == ["failed", "planned"]
    assert "OnStep healthy: None" in csv_rows(executor)[0]["error_message"]


@pytest.mark.asyncio
async def test_tracking_failure_persists_meridian_limit_verbatim(tmp_path):
    meridian = "Meridian Limit (W) Exceeded"
    telescope = FakeTelescope(
        state="off", wait_on=False,
        onstep_statuses=[status(), status(), status("error", meridian)],
    )
    executor = prepared(tmp_path, telescope=telescope)
    assert not await executor.execute_observation_plan(0, 0)
    rows = csv_rows(executor)
    assert rows[0]["onstep_message_post_tracking"] == meridian
    assert meridian in rows[0]["error_message"]
    assert telescope.events.count("goto") == 1 and rows[1]["capture_status"] == "planned"


@pytest.mark.asyncio
async def test_critical_status_between_points_blocks_second_goto(tmp_path):
    telescope = FakeTelescope(onstep_statuses=[
        status(), status(), status(),
        status("error", "Arbitrary critical OnStep error"),
    ])
    executor = prepared(tmp_path, telescope=telescope)
    assert not await executor.execute_observation_plan(0, 0)
    assert telescope.events.count("goto") == 1
    assert [row["capture_status"] for row in csv_rows(executor)] == ["success", "planned"]


@pytest.mark.asyncio
async def test_csv_and_hdf5_metadata_have_causal_timestamps(monkeypatch, tmp_path):
    captured = {}

    class InspectingSDR(FakeSDR):
        async def capture(self, **kwargs):
            captured.update(kwargs["metadata"])
            return await super().capture(**kwargs)

    monkeypatch.setattr(capture, "SDRCapture", InspectingSDR)
    executor = prepared(tmp_path, count=1)
    assert await executor.execute_observation_plan(0, 0)
    row = csv_rows(executor)[0]
    for field in (
        "goto_command_started_at", "goto_completed_at", "tracking_request_at",
        "tracking_confirmation_at", "onstep_received_at_pre_goto",
        "onstep_received_at_post_goto", "onstep_received_at_post_tracking",
    ):
        assert row[field]
        assert captured[field]
