"""Tracking-mode abstraction (Fase 1).

Confirmed by direct repo audit (see the design report): this codebase's
INDI mount driver (indi_telescope_control.py) only exposes
TELESCOPE_TRACK_STATE (motor on/off). No TELESCOPE_TRACK_MODE (sidereal/
solar/lunar rate) property is referenced anywhere in the repo. Whether the
real OnStep INDI driver actually exposes TELESCOPE_TRACK_MODE with
TRACK_SIDEREAL/TRACK_SOLAR switches is NOT confirmed - that is general INDI/
OnStep driver knowledge, not evidence from this repo, and is explicitly not
assumed here.

Consequently this module ships two backends only:
  - SimulatedTrackingBackend: in-memory, always succeeds - safe for tests
    and for developing everything above this layer.
  - a documented-but-unimplemented real backend (see RealTrackingBackend
    below) that raises NotImplementedError with the exact property this
    module expects to need, so the first person who wires it up to real
    hardware has a concrete, falsifiable starting point instead of a blank
    page - and so it can never be constructed by accident and silently do
    nothing (or something unintended) against a real mount.

TrackingSession is the safety-critical piece: a context manager that always
restores the mount's original tracking mode on the way out - success,
handled failure, or KeyboardInterrupt/any other exception - via a plain
try/finally. "No quiero una excepcion Python dejando la montura
accidentalmente en tracking solar" is enforced structurally here, not by
convention at each call site.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Protocol


class TrackingMode(str, Enum):
    SIDEREAL = "SIDEREAL"
    SOLAR = "SOLAR"
    # Reserved, not implemented anywhere below - present only so the type
    # doesn't need to change shape when they are eventually added.
    LUNAR = "LUNAR"
    CUSTOM = "CUSTOM"


class TrackingBackend(Protocol):
    def get_tracking_mode(self) -> Optional[TrackingMode]: ...
    def set_tracking_mode(self, mode: TrackingMode) -> bool: ...


class SimulatedTrackingBackend:
    """In-memory tracking-mode backend. No I/O, always succeeds. This is
    what CLI --simulate and --dry-run runs use, and what every test in this
    package uses unless it is specifically testing failure handling."""

    def __init__(self, initial: TrackingMode = TrackingMode.SIDEREAL):
        self._mode = initial
        self.commands_sent = []  # for test assertions / event log

    def get_tracking_mode(self) -> Optional[TrackingMode]:
        return self._mode

    def set_tracking_mode(self, mode: TrackingMode) -> bool:
        self.commands_sent.append(mode)
        self._mode = mode
        return True


class FailingTrackingBackend:
    """Always fails to set (get still works) - for testing restoration-on-
    failure paths without needing real hardware."""

    def __init__(self, initial: TrackingMode = TrackingMode.SIDEREAL):
        self._mode = initial

    def get_tracking_mode(self) -> Optional[TrackingMode]:
        return self._mode

    def set_tracking_mode(self, mode: TrackingMode) -> bool:
        return False


class RealTrackingBackend:
    """NOT IMPLEMENTED - placeholder documenting the expected mechanism.

    Expected (UNCONFIRMED - general INDI/OnStep driver knowledge, not
    evidence from this repo): a switch vector named TELESCOPE_TRACK_MODE
    with elements TRACK_SIDEREAL / TRACK_SOLAR / TRACK_LUNAR / TRACK_CUSTOM,
    set the same way indi_telescope_control.py already sets ON_COORD_SET
    (a <newSwitchVector> with the target element On and the others Off).

    This class deliberately does not attempt that - raising here rather
    than silently falling back to simulated behavior is the point: no code
    path in this package can end up "pretending" to control real tracking
    mode. Wiring this up for real requires: (1) a read-only getProperties
    inspection against the live indiserver to confirm TELESCOPE_TRACK_MODE
    actually exists on this OnStep driver, then (2) implementing set/get
    against indi_telescope_control.INDITelescopeControl - both pending the
    second, hardware-facing authorization this package's design report
    asked for.
    """

    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError(
            "RealTrackingBackend is intentionally unimplemented pending field "
            "confirmation of TELESCOPE_TRACK_MODE against the real OnStep INDI "
            "driver. Use SimulatedTrackingBackend for now."
        )


class TrackingRestoreError(RuntimeError):
    """Raised when a TrackingSession cannot restore the original tracking
    mode on exit. This is surfaced, never swallowed - an operator must know
    the mount may be left in a non-default tracking mode."""


class TrackingSession:
    """Context manager: set `mode` on entry, always attempt to restore the
    original mode on exit (success, exception, or KeyboardInterrupt alike).

    Usage:
        with TrackingSession(backend, TrackingMode.SOLAR, session) as ts:
            ... do the scan ...
        # original mode is restored here, whatever happened inside
    """

    def __init__(self, backend: TrackingBackend, requested_mode: TrackingMode, session=None):
        self.backend = backend
        self.requested_mode = requested_mode
        self.session = session
        self.original_mode: Optional[TrackingMode] = None
        self.restored_mode: Optional[TrackingMode] = None
        self.restore_error: Optional[str] = None

    def _log(self, message: str) -> None:
        if self.session is not None:
            self.session.log_event(f"TRACKING {message}")

    def __enter__(self) -> "TrackingSession":
        self.original_mode = self.backend.get_tracking_mode()
        if self.session is not None:
            self.session.write_tracking_before({"mode": self.original_mode.value if self.original_mode else None})
        if not self.backend.set_tracking_mode(self.requested_mode):
            self._log(f"FAILED to set {self.requested_mode.value}; original mode {self.original_mode} left untouched")
            raise RuntimeError(f"failed to set tracking mode to {self.requested_mode.value}")
        self._log(f"SET {self.requested_mode.value} (was {self.original_mode.value if self.original_mode else 'UNKNOWN'})")
        if self.session is not None:
            self.session.write_tracking_during({"mode": self.requested_mode.value})
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        target = self.original_mode or TrackingMode.SIDEREAL
        try:
            ok = self.backend.set_tracking_mode(target)
            self.restored_mode = target if ok else None
            if not ok:
                self.restore_error = f"backend refused to restore {target.value}"
        except Exception as restore_exc:  # restoration must never raise past the caller
            self.restore_error = f"{type(restore_exc).__name__}: {restore_exc}"
        outcome = "RESTORED" if self.restored_mode else f"RESTORE_FAILED ({self.restore_error})"
        self._log(f"{outcome} target={target.value} triggered_by={exc_type.__name__ if exc_type else 'normal exit'}")
        if self.session is not None:
            self.session.write_tracking_after({
                "requested_restore": target.value,
                "restored": bool(self.restored_mode),
                "error": self.restore_error,
                "triggered_by_exception": exc_type.__name__ if exc_type else None,
            })
        if not self.restored_mode:
            # Surface the failure loudly rather than silently continuing -
            # an operator must be told the mount may be in the wrong mode.
            # If the body itself already raised, chain onto that instead of
            # masking it.
            restore_failure = TrackingRestoreError(
                f"failed to restore tracking mode to {target.value}: {self.restore_error}")
            if exc:
                raise restore_failure from exc
            raise restore_failure
        return False  # never suppress an exception raised inside the `with` body
