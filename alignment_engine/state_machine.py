"""Explicit alignment state machine (Fase 12).

A plain enum + an explicit transition table, deliberately not a generic
state-machine framework - this repo's own pattern (see runtime_state.py's
SESSION_STATES tuple) is a small, explicit set of named states, not a
library dependency.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Optional


class AlignmentState(str, Enum):
    IDLE = "IDLE"
    PLANNED = "PLANNED"
    PREFLIGHT_OK = "PREFLIGHT_OK"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    TRACKING_CONFIGURED = "TRACKING_CONFIGURED"
    SCANNING = "SCANNING"
    FITTING = "FITTING"
    RESULT_READY = "RESULT_READY"
    SYNC_PENDING = "SYNC_PENDING"
    SYNC_APPLYING = "SYNC_APPLYING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: FrozenSet[AlignmentState] = frozenset(
    {AlignmentState.COMPLETED, AlignmentState.FAILED, AlignmentState.CANCELLED,
     AlignmentState.PREFLIGHT_FAILED}
)

# Explicit adjacency: every non-terminal state may additionally transition to
# FAILED or CANCELLED (added programmatically below) - that escape hatch is
# not spelled out per-row to avoid repeating it fourteen times.
_HAPPY_PATH: Dict[AlignmentState, FrozenSet[AlignmentState]] = {
    AlignmentState.IDLE: frozenset({AlignmentState.PLANNED}),
    AlignmentState.PLANNED: frozenset({AlignmentState.PREFLIGHT_OK, AlignmentState.PREFLIGHT_FAILED}),
    AlignmentState.PREFLIGHT_OK: frozenset({AlignmentState.TRACKING_CONFIGURED}),
    AlignmentState.TRACKING_CONFIGURED: frozenset({AlignmentState.SCANNING}),
    AlignmentState.SCANNING: frozenset({AlignmentState.FITTING}),
    # FITTING -> SCANNING covers the coarse-then-fine cycle (Fase 2): the
    # coarse stage's fit result seeds where the fine stage scans, so
    # FITTING legitimately loops back into a second SCANNING pass before
    # the final RESULT_READY.
    AlignmentState.FITTING: frozenset({AlignmentState.SCANNING, AlignmentState.RESULT_READY}),
    AlignmentState.RESULT_READY: frozenset({AlignmentState.SYNC_PENDING, AlignmentState.COMPLETED}),
    AlignmentState.SYNC_PENDING: frozenset({AlignmentState.SYNC_APPLYING}),
    AlignmentState.SYNC_APPLYING: frozenset({AlignmentState.VERIFYING}),
    AlignmentState.VERIFYING: frozenset({AlignmentState.COMPLETED}),
}
TRANSITIONS: Dict[AlignmentState, FrozenSet[AlignmentState]] = {
    state: (targets | {AlignmentState.FAILED, AlignmentState.CANCELLED})
    for state, targets in _HAPPY_PATH.items()
}
for _terminal in TERMINAL_STATES:
    TRANSITIONS.setdefault(_terminal, frozenset())


class InvalidTransition(ValueError):
    def __init__(self, current: AlignmentState, requested: AlignmentState):
        super().__init__(f"cannot transition from {current.value} to {requested.value}")
        self.current = current
        self.requested = requested


class AlignmentStateMachine:
    """Tracks one alignment session's state and its transition history.

    History is kept in memory (list of (state, utc_timestamp, reason)) so a
    session's persisted JSON can include the full trail (Fase 22 logging
    requirement: "state transitions" must be recorded)."""

    def __init__(self, utcnow_fn, initial: AlignmentState = AlignmentState.IDLE):
        self._utcnow = utcnow_fn
        self.state = initial
        self.history = [(initial, self._utcnow(), "initial")]

    def can_transition(self, target: AlignmentState) -> bool:
        return target in TRANSITIONS.get(self.state, frozenset())

    def transition(self, target: AlignmentState, reason: Optional[str] = None) -> AlignmentState:
        if not self.can_transition(target):
            raise InvalidTransition(self.state, target)
        self.state = target
        self.history.append((target, self._utcnow(), reason or ""))
        return self.state

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def history_as_dicts(self):
        return [{"state": state.value, "utc": ts, "reason": reason} for state, ts, reason in self.history]
