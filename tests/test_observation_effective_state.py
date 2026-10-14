"""OBSERVE: a session that ends on its own leaves the orchestrator label RUNNING (only STOP closes it). The web shows
the effective state instead (read-only; observation_orchestrator, frozen, is unchanged)."""
import almita_web_ops as ops


def _status(label="RUNNING", alive=False, cs_state="COMPLETED", cs_session="20261007_020101"):
    return {"orchestrator": {"orchestrator_state": label, "session_id": "20261007_020101", "capture_pid": 647203,
                             "capture_process_alive": alive},
            "current_session": {"session_id": cs_session, "state": cs_state, "updated_utc": "2026-10-07T05:44:20Z"}}


def test_a_session_that_completed_on_its_own_reads_completed():
    o = ops.reconcile_observation_status(_status())["orchestrator"]
    assert o["orchestrator_state"] == "RUNNING" and o["effective_state"] == "COMPLETED"
    assert "ended on its own" in o["effective_note"] and "nothing is running" in o["effective_note"]


def test_aborted_and_failed_sessions_keep_their_own_end_state():
    assert ops.reconcile_observation_status(_status(cs_state="ABORTED"))["orchestrator"]["effective_state"] == "ABORTED"
    assert ops.reconcile_observation_status(_status(label="STOPPING", cs_state="FAILED"))["orchestrator"]["effective_state"] == "FAILED"


def test_a_live_capture_or_a_terminal_label_is_left_alone():
    live = ops.reconcile_observation_status(_status(alive=True, cs_state="RUNNING"))["orchestrator"]
    assert (live["effective_state"], live["effective_note"]) == ("RUNNING", None)
    done = ops.reconcile_observation_status(_status(label="COMPLETED"))["orchestrator"]
    assert done["effective_state"] == "COMPLETED" and done["effective_note"] is None


def test_a_dead_capture_without_an_end_report_or_from_another_session_is_degraded():
    for status in (_status(cs_state="RUNNING"), _status(cs_session="20261008_000000")):
        o = ops.reconcile_observation_status(status)["orchestrator"]
        assert o["effective_state"] == "DEGRADED" and "reported no end state" in o["effective_note"]


# ------------------------------------------------------------------ what capture.py publishes about stopping the mount
def _stopped(confirmed, summary):
    s = _status(label="ABORTED", cs_state="ABORTED")
    s["current_session"]["mount_stop"] = {"confirmed": confirmed, "summary": summary, "reason": "r"}
    return ops.reconcile_observation_status(s)["orchestrator"]


def test_an_unconfirmed_mount_stop_is_never_shown_as_a_finished_stop():
    o = _stopped(False, "PARADA NO CONFIRMADA - INDI did not answer")
    assert o["effective_state"] == "DEGRADED"
    assert o["effective_note"].startswith("PARADA NO CONFIRMADA") and "check the mount NOW" in o["effective_note"]
    assert o["mount_stop"]["confirmed"] is False


def test_a_confirmed_mount_stop_keeps_the_end_state_and_says_so():
    o = _stopped(True, "MOUNT STOP CONFIRMED (9.1 s): not slewing, tracking OFF, STATIONARY")
    assert o["effective_state"] == "ABORTED" and "MOUNT STOP CONFIRMED" in o["effective_note"]


def test_a_mount_stop_of_another_session_is_ignored():
    s = _status(label="ABORTED", cs_state="ABORTED")
    s["current_session"]["mount_stop"] = {"confirmed": False, "summary": "PARADA NO CONFIRMADA"}
    s["current_session"]["session_id"] = "some-other-session"
    o = ops.reconcile_observation_status(s)["orchestrator"]
    assert "mount_stop" not in o and o["effective_state"] == "ABORTED"
