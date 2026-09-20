from alignment_engine.state_machine import (
    AlignmentState, AlignmentStateMachine, InvalidTransition, TERMINAL_STATES, TRANSITIONS,
)


def _clock():
    ticks = iter(range(1000))
    return lambda: f"t{next(ticks)}"


def test_initial_state_is_idle_with_history():
    sm = AlignmentStateMachine(_clock())
    assert sm.state == AlignmentState.IDLE
    assert sm.history_as_dicts()[0]["state"] == "IDLE"


def test_happy_path_solar_transitions_succeed():
    sm = AlignmentStateMachine(_clock())
    path = [AlignmentState.PLANNED, AlignmentState.PREFLIGHT_OK, AlignmentState.TRACKING_CONFIGURED,
            AlignmentState.SCANNING, AlignmentState.FITTING, AlignmentState.RESULT_READY,
            AlignmentState.SYNC_PENDING, AlignmentState.SYNC_APPLYING, AlignmentState.VERIFYING,
            AlignmentState.COMPLETED]
    for state in path:
        sm.transition(state)
    assert sm.state == AlignmentState.COMPLETED
    assert sm.is_terminal()


def test_coarse_then_fine_loop_back_to_scanning_is_valid():
    sm = AlignmentStateMachine(_clock())
    for state in (AlignmentState.PLANNED, AlignmentState.PREFLIGHT_OK,
                  AlignmentState.TRACKING_CONFIGURED, AlignmentState.SCANNING,
                  AlignmentState.FITTING, AlignmentState.SCANNING, AlignmentState.FITTING,
                  AlignmentState.RESULT_READY):
        sm.transition(state)
    assert sm.state == AlignmentState.RESULT_READY


def test_result_ready_can_skip_sync_straight_to_completed():
    sm = AlignmentStateMachine(_clock())
    for state in (AlignmentState.PLANNED, AlignmentState.PREFLIGHT_OK,
                  AlignmentState.TRACKING_CONFIGURED, AlignmentState.SCANNING,
                  AlignmentState.FITTING, AlignmentState.RESULT_READY, AlignmentState.COMPLETED):
        sm.transition(state)
    assert sm.state == AlignmentState.COMPLETED


def test_invalid_transition_raises_and_does_not_mutate_state():
    sm = AlignmentStateMachine(_clock())
    try:
        sm.transition(AlignmentState.COMPLETED)  # IDLE -> COMPLETED is not a real path
        assert False, "expected InvalidTransition"
    except InvalidTransition as exc:
        assert exc.current == AlignmentState.IDLE
        assert exc.requested == AlignmentState.COMPLETED
    assert sm.state == AlignmentState.IDLE  # unchanged


def test_every_non_terminal_state_can_reach_failed_and_cancelled():
    for state, targets in TRANSITIONS.items():
        if state in TERMINAL_STATES:
            continue
        assert AlignmentState.FAILED in targets, f"{state} cannot reach FAILED"
        assert AlignmentState.CANCELLED in targets, f"{state} cannot reach CANCELLED"


def test_terminal_states_have_no_outgoing_transitions():
    for state in TERMINAL_STATES:
        assert TRANSITIONS.get(state) == frozenset()


def test_preflight_failed_is_terminal():
    sm = AlignmentStateMachine(_clock())
    sm.transition(AlignmentState.PLANNED)
    sm.transition(AlignmentState.PREFLIGHT_FAILED)
    assert sm.is_terminal()
