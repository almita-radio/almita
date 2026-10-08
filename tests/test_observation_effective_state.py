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
