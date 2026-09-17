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
    """Pre-hardware pass, item 6: TELESCOPE_TRACK_MODE now CONFIRMED (not
    assumed) to exist on the real driver.

    Read-only evidence (2026-09-17, live indiserver at localhost:7624,
    device "LX200 OnStep", via `indi_getprop` / a plain <getProperties/>
    query - no property was ever set while gathering this):

        LX200 OnStep.TELESCOPE_TRACK_MODE._PERM=rw
        LX200 OnStep.TELESCOPE_TRACK_MODE._GROUP=Main Control
        LX200 OnStep.TELESCOPE_TRACK_MODE.TRACK_SIDEREAL=On
        LX200 OnStep.TELESCOPE_TRACK_MODE.TRACK_SOLAR=Off
        LX200 OnStep.TELESCOPE_TRACK_MODE.TRACK_LUNAR=Off
        LX200 OnStep.TELESCOPE_TRACK_MODE.TRACK_CUSTOM=Off

    This is a real, dated observation of this specific driver/firmware
    combination, not general INDI/OnStep documentation taken on faith.

    Three-stage separation (Fase 6's own requirement) - inspect, prepare,
    execute are three different methods, never blurred:
      inspect()    - read-only. Opens its own short-lived TCP connection,
                     sends exactly one <getProperties.../>, parses the
                     reply, closes. Never sends a <newSwitchVector>. Safe
                     to call against the real mount at any time (this is
                     exactly what produced the evidence above).
      prepare_set_tracking_mode(mode) - pure, no I/O at all. Returns the
                     exact <newSwitchVector> XML that WOULD be sent - this
                     is what a future `tracking --dry-run` shows an
                     operator, same philosophy as sync_flow's prepare/
                     apply/verify split.
      execute_set_tracking_mode(xml) - the only method that can write to
                     the mount. Refuses with RuntimeError unless this
                     instance was constructed with allow_real_writes=True.
                     Nothing in this pass ever passes that flag - real
                     execution needs the NEXT, separate hardware
                     authorization the design report asked for.

    Wiring this into TrackingSession (which calls get_tracking_mode()/
    set_tracking_mode() synchronously) is an OPEN DESIGN QUESTION, flagged
    rather than silently resolved: this class's I/O is `async` (matching
    indi_telescope_control.py's own asyncio-based client), while
    TrackingSession/TrackingBackend today are plain sync. A sync-over-async
    bridge (asyncio.run() per call, or making the engine's scan loop async
    throughout) is a real architectural decision for whoever does the next,
    hardware-facing pass - not made here.
    """
    DEVICE_NAME = "LX200 OnStep"
    PROPERTY_NAME = "TELESCOPE_TRACK_MODE"
    ELEMENT_BY_MODE = {
        TrackingMode.SIDEREAL: "TRACK_SIDEREAL",
        TrackingMode.SOLAR: "TRACK_SOLAR",
        TrackingMode.LUNAR: "TRACK_LUNAR",
        TrackingMode.CUSTOM: "TRACK_CUSTOM",
    }
    MODE_BY_ELEMENT = {v: k for k, v in ELEMENT_BY_MODE.items()}

    def __init__(self, host: str = "localhost", port: int = 7624,
                 device_name: Optional[str] = None, allow_real_writes: bool = False):
        self.host = host
        self.port = port
        self.device_name = device_name or self.DEVICE_NAME
        self.allow_real_writes = allow_real_writes

    async def inspect(self, timeout: float = 3.0) -> Optional["TrackingMode"]:
        """Read-only. Returns the currently-selected TrackingMode, or None
        if the property could not be read (driver absent, timeout, or an
        element combination this package doesn't recognize)."""
        raw = await _query_property_readonly(self.host, self.port, self.device_name,
                                              self.PROPERTY_NAME, timeout)
        if raw is None:
            return None
        return _parse_track_mode_xml(raw, self.MODE_BY_ELEMENT)

    def prepare_set_tracking_mode(self, mode: "TrackingMode") -> str:
        """Pure - no I/O, no connection, cannot fail against hardware
        because it never touches any. Exactly what will be sent, and
        nothing more, so an operator (or `tracking --dry-run`, future
        work) can review it before anyone authorizes execute_set_
        tracking_mode() to actually send it."""
        if mode not in self.ELEMENT_BY_MODE:
            raise ValueError(f"RealTrackingBackend cannot request mode {mode!r} - "
                              f"the real driver only exposes {list(self.ELEMENT_BY_MODE)}")
        target_element = self.ELEMENT_BY_MODE[mode]
        switches = "\n".join(
            f'  <oneSwitch name="{element}">{"On" if element == target_element else "Off"}</oneSwitch>'
            for element in self.ELEMENT_BY_MODE.values()
        )
        return (f'<newSwitchVector device="{self.device_name}" name="{self.PROPERTY_NAME}">\n'
                f'{switches}\n</newSwitchVector>')

    async def execute_set_tracking_mode(self, xml: str, timeout: float = 3.0) -> None:
        """The only method in this class that can write to the mount.
        Refuses unless allow_real_writes=True was passed to __init__ - and
        nothing in this codebase, as of this pass, ever passes that."""
        if not self.allow_real_writes:
            raise RuntimeError(
                "RealTrackingBackend.execute_set_tracking_mode() refused: "
                "allow_real_writes=False. Real tracking-mode writes require a "
                "separate, explicit hardware authorization beyond this pre-hardware pass."
            )
        await _send_command_readonly_connection(self.host, self.port, xml, timeout)

    # -- TrackingBackend Protocol: intentionally NOT implemented yet -----
    # See the class docstring's "OPEN DESIGN QUESTION" - bridging this
    # class's async I/O into TrackingSession's sync get/set_tracking_mode()
    # is a real decision for the next, hardware-facing pass, not made here
    # by quietly wrapping asyncio.run() around every call.

    def get_tracking_mode(self):
        raise NotImplementedError(
            "RealTrackingBackend.get_tracking_mode() (sync) is not wired - use "
            "'await inspect()' from async code. See this class's docstring for why "
            "a sync facade is a deliberate open design question, not an oversight."
        )

    def set_tracking_mode(self, mode):
        raise NotImplementedError(
            "RealTrackingBackend.set_tracking_mode() (sync) is not wired - use "
            "prepare_set_tracking_mode()/execute_set_tracking_mode() explicitly from "
            "async code, and only with allow_real_writes=True under a hardware authorization."
        )


async def _query_property_readonly(host: str, port: int, device_name: str,
                                    property_name: str, timeout: float) -> Optional[str]:
    """Opens its own connection, sends exactly one <getProperties.../>,
    returns the first matching def*Vector/set*Vector XML for
    device/property_name, then always closes the connection. Sends nothing
    else - in particular, never a <newSwitchVector> or <newNumberVector>."""
    import asyncio as _asyncio
    import xml.etree.ElementTree as ET

    try:
        reader, writer = await _asyncio.wait_for(_asyncio.open_connection(host, port), timeout)
    except (OSError, _asyncio.TimeoutError):
        return None
    try:
        query = f'<getProperties device="{device_name}" name="{property_name}" version="1.7"/>'
        writer.write((query + "\n").encode())
        await writer.drain()
        deadline = _asyncio.get_event_loop().time() + timeout
        buffer = ""
        while True:
            remaining = deadline - _asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            try:
                chunk = await _asyncio.wait_for(reader.read(65536), remaining)
            except _asyncio.TimeoutError:
                return None
            if not chunk:
                return None
            buffer += chunk.decode(errors="replace")
            while True:
                start = buffer.find("<")
                if start < 0:
                    break
                end = buffer.find(">", start)
                if end < 0:
                    break
                # Find the matching close tag for this vector element.
                tag_name = buffer[start + 1:end].split()[0]
                if tag_name.startswith("/"):
                    buffer = buffer[end + 1:]
                    continue
                close = f"</{tag_name}>"
                close_idx = buffer.find(close, end)
                if close_idx < 0:
                    break  # incomplete message, wait for more data
                message = buffer[start:close_idx + len(close)]
                buffer = buffer[close_idx + len(close):]
                try:
                    root = ET.fromstring(message)
                except ET.ParseError:
                    continue
                if root.attrib.get("name") == property_name:
                    return message
    finally:
        writer.close()
        with __import__("contextlib").suppress(Exception):
            await writer.wait_closed()


async def _send_command_readonly_connection(host: str, port: int, xml: str, timeout: float) -> None:
    """Used ONLY by execute_set_tracking_mode(), which itself refuses to
    run unless allow_real_writes=True - kept as a separate function (not
    reusing _query_property_readonly) so the read path can never
    accidentally be handed a write payload."""
    import asyncio as _asyncio

    reader, writer = await _asyncio.wait_for(_asyncio.open_connection(host, port), timeout)
    try:
        writer.write((xml + "\n").encode())
        await writer.drain()
    finally:
        writer.close()
        with __import__("contextlib").suppress(Exception):
            await writer.wait_closed()


def _parse_track_mode_xml(raw: str, mode_by_element: dict) -> Optional["TrackingMode"]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    on_elements = [
        child.attrib.get("name") for child in root
        if (child.text or "").strip() == "On" and child.attrib.get("name") in mode_by_element
    ]
    if len(on_elements) != 1:
        return None  # ambiguous/alert vector - never guess
    return mode_by_element[on_elements[0]]


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
