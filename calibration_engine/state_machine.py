"""Explicit calibration state machine (Fase 3) - same pattern as
alignment_engine/state_machine.py (plain enum + explicit transition table,
not a generic framework), deliberately re-derived rather than imported:
calibration's happy path (no SYNC, no tracking, an ACQUIRING phase that
covers sample-statistics/gain-sweep/bandpass/stability captures rather than
a raster) is different enough that sharing one table would blur two
distinct instrumental concerns.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Optional
from enum import Enum


class CalibrationState(str, Enum):
    IDLE = "IDLE"
    PLANNED = "PLANNED"
    PREFLIGHT = "PREFLIGHT"
    READY = "READY"
    ACQUIRING = "ACQUIRING"
    ANALYZING = "ANALYZING"
    RESULT_READY = "RESULT_READY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


TERMINAL_STATES: FrozenSet[CalibrationState] = frozenset(
    {CalibrationState.COMPLETED, CalibrationState.FAILED,
     CalibrationState.CANCELLED, CalibrationState.BLOCKED}
)

_HAPPY_PATH: Dict[CalibrationState, FrozenSet[CalibrationState]] = {
    CalibrationState.IDLE: frozenset({CalibrationState.PLANNED}),
    CalibrationState.PLANNED: frozenset({CalibrationState.PREFLIGHT}),
    CalibrationState.PREFLIGHT: frozenset({CalibrationState.READY, CalibrationState.BLOCKED}),
    CalibrationState.READY: frozenset({CalibrationState.ACQUIRING}),
    # ACQUIRING -> ANALYZING -> ACQUIRING covers a gain sweep or a
    # multi-capture stability run: each step's quick sanity check may send
    # control back for the next step before the session's own final
    # analysis pass.
    CalibrationState.ACQUIRING: frozenset({CalibrationState.ANALYZING}),
    CalibrationState.ANALYZING: frozenset({CalibrationState.ACQUIRING, CalibrationState.RESULT_READY}),
    CalibrationState.RESULT_READY: frozenset({CalibrationState.COMPLETED}),
}
TRANSITIONS: Dict[CalibrationState, FrozenSet[CalibrationState]] = {
    state: (targets | {CalibrationState.FAILED, CalibrationState.CANCELLED})
    for state, targets in _HAPPY_PATH.items()
}
for _terminal in TERMINAL_STATES:
    TRANSITIONS.setdefault(_terminal, frozenset())


class InvalidTransition(ValueError):
    def __init__(self, current: CalibrationState, requested: CalibrationState):
        super().__init__(f"cannot transition from {current.value} to {requested.value}")
        self.current = current
        self.requested = requested


class CalibrationStateMachine:
    def __init__(self, utcnow_fn, initial: CalibrationState = CalibrationState.IDLE):
        self._utcnow = utcnow_fn
        self.state = initial
        self.history = [(initial, self._utcnow(), "initial")]

    def can_transition(self, target: CalibrationState) -> bool:
        return target in TRANSITIONS.get(self.state, frozenset())

    def transition(self, target: CalibrationState, reason: Optional[str] = None) -> CalibrationState:
        if not self.can_transition(target):
            raise InvalidTransition(self.state, target)
        self.state = target
        self.history.append((target, self._utcnow(), reason or ""))
        return self.state

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def history_as_dicts(self):
        return [{"state": state.value, "utc": ts, "reason": reason} for state, ts, reason in self.history]
