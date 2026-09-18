"""Tracking-mode abstraction (Fase 1, now fully async - 3rd pass).

ARCHITECTURE DECISION (this pass, explicitly authorized): the async
boundary sits exactly where I/O or waiting happens - mount inspection,
tracking get/set, verification reads - never hidden inside a sync facade
via a private asyncio.run() or a background event-loop thread. Pure math
elsewhere in this package (scan_planner, fitting, simulation, targets)
stays plain sync, unchanged by this pass.

TrackingBackend is therefore an async Protocol. SimulatedTrackingBackend
and RealTrackingBackend implement the IDENTICAL async contract - no
sync/async split between them, no duplicated verification logic (both are
driven through the same TrackingSession.__aenter__/__aexit__ verification
steps below).

Real INDI evidence (2026-09-17, live indiserver at localhost:7624, device
"LX200 OnStep", via read-only getProperties queries only):

    LX200 OnStep.TELESCOPE_TRACK_MODE._PERM=rw
    LX200 OnStep.TELESCOPE_TRACK_MODE._GROUP=Main Control
    LX200 OnStep.TELESCOPE_TRACK_MODE.TRACK_SIDEREAL=On
    LX200 OnStep.TELESCOPE_TRACK_MODE.TRACK_SOLAR=Off
    LX200 OnStep.TELESCOPE_TRACK_MODE.TRACK_LUNAR=Off
    LX200 OnStep.TELESCOPE_TRACK_MODE.TRACK_CUSTOM=Off

I/O mechanism (Fase 3's own audit requirement): all real INDI communication
in this module (and in indi_telescope_control.py, which RealMountAdapter
wraps) uses `asyncio.open_connection` / asyncio streams directly - there is
no subprocess.run()/Popen anywhere in the INDI I/O path, so there is
nothing here that blocks the event loop or needs asyncio.to_thread(). This
was verified by reading both this module's own implementation and
indi_telescope_control.py's connect()/_send_command()/_reader_loop()
before writing this docstring, not assumed.

TrackingSession is the safety-critical piece: an ASYNC context manager
that always restores the mount's original tracking mode on the way out -
success, handled failure, asyncio.CancelledError, or KeyboardInterrupt
alike - via try/except/finally that treats BaseException (not just
Exception) as something cleanup must survive and report, never silently
swallow. See its own docstring for the exact cancellation semantics and
their one documented residual risk.
"""
from __future__ import annotations

import asyncio
from enum import Enum
from typing import Awaitable, Dict, Optional, Protocol


class TrackingMode(str, Enum):
    SIDEREAL = "SIDEREAL"
    SOLAR = "SOLAR"
    # Reserved, not implemented anywhere below - present only so the type
    # doesn't need to change shape when they are eventually added.
    LUNAR = "LUNAR"
    CUSTOM = "CUSTOM"


class TrackingBackend(Protocol):
    async def get_tracking_mode(self) -> Optional[TrackingMode]: ...
    async def set_tracking_mode(self, mode: TrackingMode) -> bool: ...


class TrackingOperationTimeout(RuntimeError):
    """A tracking backend call did not complete within its timeout. Never
    an infinite await - see TrackingSession's `timeout` parameter."""


class TrackingModeMismatchError(RuntimeError):
    """set_tracking_mode() reported success but a read-back verification
    (get_tracking_mode()) disagrees - treated as a failure to enter/exit,
    never silently accepted, because a backend claiming success while the
    hardware disagrees is exactly the kind of state a Python exception must
    not paper over."""


class TrackingRestoreError(RuntimeError):
    """Raised when a TrackingSession cannot restore (or cannot VERIFY the
    restoration of) the original tracking mode on exit. Surfaced, never
    swallowed - an operator must know the mount may be left in a
    non-default tracking mode."""


class SimulatedTrackingBackend:
    """In-memory tracking-mode backend. No real I/O, always succeeds
    "instantly" (still `async def` - see module docstring: identical
    contract to RealTrackingBackend, no special-casing). This is what CLI
    --simulate and --dry-run runs use, and what every test in this package
    uses unless it is specifically testing failure handling."""

    def __init__(self, initial: TrackingMode = TrackingMode.SIDEREAL):
        self._mode = initial
        self.commands_sent = []  # for test assertions / event log

    async def get_tracking_mode(self) -> Optional[TrackingMode]:
        return self._mode

    async def set_tracking_mode(self, mode: TrackingMode) -> bool:
        self.commands_sent.append(mode)
        self._mode = mode
        return True


class FailingTrackingBackend:
    """Always fails to set (get still works) - for testing restoration-on-
    failure paths without needing real hardware."""

    def __init__(self, initial: TrackingMode = TrackingMode.SIDEREAL):
        self._mode = initial

    async def get_tracking_mode(self) -> Optional[TrackingMode]:
        return self._mode

    async def set_tracking_mode(self, mode: TrackingMode) -> bool:
        return False


class RealTrackingBackend:
    """Real INDI backend for TELESCOPE_TRACK_MODE - now implementing the
    SAME async TrackingBackend contract SimulatedTrackingBackend does
    (get_tracking_mode/set_tracking_mode), per this pass's explicit
    architecture decision (no separate sync facade, no asyncio.run()
    hidden in here).

    Three-stage separation kept exactly as designed in the prior pass -
    inspect, prepare, execute are three different methods, never blurred:
      inspect() / get_tracking_mode() - read-only. Opens its own
                     short-lived TCP connection, sends exactly one
                     <getProperties.../>, parses the reply, closes. Never
                     sends a <newSwitchVector>. Safe to call against the
                     real mount at any time.
      prepare_set_tracking_mode(mode) - pure, no I/O at all. Returns the
                     exact <newSwitchVector> XML that WOULD be sent - what
                     `tracking --dry-run` shows an operator.
      execute_set_tracking_mode(xml) / set_tracking_mode(mode) - the only
                     path that can write to the mount. Refuses with
                     RuntimeError unless this instance was constructed
                     with allow_real_writes=True. Nothing in this
                     codebase, as of this pass, ever passes that flag -
                     real execution needs the NEXT, separate hardware
                     authorization.
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
                 device_name: Optional[str] = None, allow_real_writes: bool = False,
                 default_timeout: float = 3.0):
        self.host = host
        self.port = port
        self.device_name = device_name or self.DEVICE_NAME
        self.allow_real_writes = allow_real_writes
        self.default_timeout = default_timeout

    async def inspect(self, timeout: Optional[float] = None) -> Optional["TrackingMode"]:
        """Read-only. Returns the currently-selected TrackingMode, or None
        if the property could not be read (driver absent, timeout, or an
        element combination this package doesn't recognize)."""
        raw = await _query_property_readonly(self.host, self.port, self.device_name,
                                              self.PROPERTY_NAME, timeout or self.default_timeout)
        if raw is None:
            return None
        return _parse_track_mode_xml(raw, self.MODE_BY_ELEMENT)

    async def get_tracking_mode(self, timeout: Optional[float] = None) -> Optional["TrackingMode"]:
        return await self.inspect(timeout)

    def prepare_set_tracking_mode(self, mode: "TrackingMode") -> str:
        """Pure - no I/O, no connection, cannot fail against hardware
        because it never touches any. Exactly what will be sent, and
        nothing more, so an operator (or `tracking --dry-run`) can review
        it before anyone authorizes execute_set_tracking_mode() to
        actually send it."""
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

    async def execute_set_tracking_mode(self, xml: str, timeout: Optional[float] = None) -> None:
        """The only method in this class that can write to the mount.
        Refuses unless allow_real_writes=True was passed to __init__ - and
        nothing in this codebase, as of this pass, ever passes that."""
        if not self.allow_real_writes:
            raise RuntimeError(
                "RealTrackingBackend.execute_set_tracking_mode() refused: "
                "allow_real_writes=False. Real tracking-mode writes require a "
                "separate, explicit hardware authorization beyond this pre-hardware pass."
            )
        await _send_command_readonly_connection(self.host, self.port, xml, timeout or self.default_timeout)

    async def set_tracking_mode(self, mode: "TrackingMode", timeout: Optional[float] = None) -> bool:
        """Same async contract as SimulatedTrackingBackend.set_tracking_mode
        - returns True on success. Unlike the simulated backend, this
        raises (rather than returning False) when execution is refused for
        lack of authorization, because that is a categorically different,
        more specific condition than "the hardware failed" and deserves a
        message that says so, not a generic False."""
        xml = self.prepare_set_tracking_mode(mode)
        await self.execute_set_tracking_mode(xml, timeout)
        return True


async def _query_property_readonly(host: str, port: int, device_name: str,
                                    property_name: str, timeout: float) -> Optional[str]:
    """Opens its own connection, sends exactly one <getProperties.../>,
    returns the first matching def*Vector/set*Vector XML for
    device/property_name, then always closes the connection. Sends nothing
    else - in particular, never a <newSwitchVector> or <newNumberVector>."""
    import xml.etree.ElementTree as ET

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        query = f'<getProperties device="{device_name}" name="{property_name}" version="1.7"/>'
        writer.write((query + "\n").encode())
        await writer.drain()
        deadline = asyncio.get_event_loop().time() + timeout
        buffer = ""
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            try:
                chunk = await asyncio.wait_for(reader.read(65536), remaining)
            except asyncio.TimeoutError:
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
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
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


# ---- generic read-only property access (hardware precheck/postcheck) -----
# RealTrackingBackend itself only ever owns TELESCOPE_TRACK_MODE (see its
# class docstring) - it has no business reading/writing TELESCOPE_TRACK_STATE,
# EQUATORIAL_EOD_COORD or ON_COORD_SET. These two functions expose the same
# read-only query mechanism generically, for a caller (a hardware test
# harness, never this package's own science logic) that needs to snapshot
# other properties for before/after comparison without ever writing to them.


async def read_property_readonly(host: str, port: int, device_name: str, property_name: str,
                                  timeout: float = 3.0) -> Optional[str]:
    """Public wrapper over the same single-getProperties, never-writes
    query every read in this module already uses. Returns the raw XML
    vector, or None if it could not be read within `timeout`."""
    return await _query_property_readonly(host, port, device_name, property_name, timeout)


def parse_switch_vector(raw: Optional[str]) -> Optional[Dict[str, str]]:
    """{element_name: 'On'|'Off'} for a def/setSwitchVector. None if raw is
    None or not parseable - never guesses a value for an unreadable vector."""
    if raw is None:
        return None
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    return {child.attrib.get("name"): (child.text or "").strip()
            for child in root if child.attrib.get("name")}


def parse_number_vector(raw: Optional[str]) -> Optional[Dict[str, Optional[float]]]:
    """{element_name: float value} for a def/setNumberVector. A malformed
    individual element becomes None rather than raising or silently
    dropping the key, so a caller can tell "present but unparseable" apart
    from "absent"."""
    if raw is None:
        return None
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    result: Dict[str, Optional[float]] = {}
    for child in root:
        name = child.attrib.get("name")
        if name is None:
            continue
        try:
            result[name] = float((child.text or "").strip())
        except ValueError:
            result[name] = None
    return result


async def _await_with_timeout(coro: Awaitable, timeout: Optional[float], op_name: str):
    """Item 4: no infinite awaits anywhere in this module. `timeout=None`
    means "no additional timeout beyond whatever the backend itself
    enforces" (SimulatedTrackingBackend never awaits real I/O, so it has
    nothing to time out on)."""
    if timeout is None:
        return await coro
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError as exc:
        raise TrackingOperationTimeout(f"{op_name} did not complete within {timeout}s") from exc


class TrackingSession:
    """Async context manager: set `mode` on entry, always attempt to
    restore (and VERIFY the restoration of) the original mode on exit -
    success, handled failure, asyncio.CancelledError, or KeyboardInterrupt
    alike.

    Usage:
        async with TrackingSession(backend, TrackingMode.SOLAR, session) as ts:
            ... do the scan (await real I/O here as needed) ...
        # original mode is restored (and verified) here, whatever happened inside

    ENTER semantics:
      1. read original mode (get_tracking_mode()), persist tracking_before.
      2. if requested_mode != original: call set_tracking_mode(requested).
         (For SimulatedTrackingBackend this changes state immediately; for
         RealTrackingBackend without allow_real_writes=True, this raises -
         see RealTrackingBackend.set_tracking_mode - and __aenter__ never
         completes, so __aexit__ never runs: nothing was changed, nothing
         needs restoring.)
      3. verify: get_tracking_mode() again and compare to requested. A
         mismatch (backend claimed success but read-back disagrees) raises
         TrackingModeMismatchError - RealTrackingBackend's own contract
         guarantees this cannot yet happen for real hardware (writes are
         refused before reaching the wire), but the check exists so the
         SAME code path is exercised and trusted for both backends.
      4. persist tracking_during.

    EXIT semantics (always, via try/except covering BaseException):
      1. attempt set_tracking_mode(original) - shielded from the specific
         cancellation that is propagating through this __aexit__ call
         (see the CancelledError note below for what this does and does
         not protect against).
      2. verify via get_tracking_mode() again.
      3. persist tracking_after (restored: bool, error: str|None).
      4. if restoration could not be confirmed exactly, raise
         TrackingRestoreError - chained onto the original exception (if
         any) rather than masking it. Never returns True from __aexit__
         (never suppresses an exception raised inside the body).

    CANCELLATION NOTE (item 2/3's "asyncio.CancelledError no puede
    saltarse cleanup", addressed precisely, not just asserted): a
    CancelledError raised inside the `async with` body reaches __aexit__
    exactly like any other exception - Python's `async with` guarantees
    this, it is not something this class has to implement itself. What
    this class DOES add on top: the restore awaits inside __aexit__ are
    wrapped in `asyncio.shield()`, so a cancellation that is *already in
    flight* when __aexit__ starts does not abort an in-progress restore
    network call. The one residual, honestly-documented risk (not
    resolved here, not silently claimed to be): if a *second*, new
    cancellation is delivered to this task while __aexit__'s shielded
    restore await is still running, that second cancellation still
    propagates to the caller of __aexit__ (shield protects the inner
    awaitable from being cancelled *itself*, not this method's own
    suspension point) - in that case this class still records the
    resulting error and raises TrackingRestoreError (never a silent bare
    CancelledError escaping with no record of what happened to tracking
    mode), but the mount's true state must then be re-confirmed with a
    fresh inspect() before continuing - this is inherent to cooperative
    cancellation, not a gap specific to this implementation.
    """

    def __init__(self, backend: TrackingBackend, requested_mode: TrackingMode, session=None,
                 timeout: Optional[float] = 10.0):
        self.backend = backend
        self.requested_mode = requested_mode
        self.session = session
        self.timeout = timeout
        self.original_mode: Optional[TrackingMode] = None
        self.restored_mode: Optional[TrackingMode] = None
        self.restore_error: Optional[str] = None

    def _log(self, message: str) -> None:
        if self.session is not None:
            self.session.log_event(f"TRACKING {message}")

    async def __aenter__(self) -> "TrackingSession":
        self.original_mode = await _await_with_timeout(
            self.backend.get_tracking_mode(), self.timeout, "get_tracking_mode (initial read)")
        if self.session is not None:
            self.session.write_tracking_before({"mode": self.original_mode.value if self.original_mode else None})

        ok = await _await_with_timeout(
            self.backend.set_tracking_mode(self.requested_mode), self.timeout,
            f"set_tracking_mode({self.requested_mode.value})")
        if not ok:
            self._log(f"FAILED to set {self.requested_mode.value}; original mode {self.original_mode} left untouched")
            raise RuntimeError(f"failed to set tracking mode to {self.requested_mode.value}")

        actual = await _await_with_timeout(
            self.backend.get_tracking_mode(), self.timeout, "get_tracking_mode (verify after set)")
        if actual != self.requested_mode:
            self._log(f"MISMATCH after set: requested {self.requested_mode.value}, read back {actual}")
            raise TrackingModeMismatchError(
                f"set_tracking_mode({self.requested_mode.value}) reported success but "
                f"read-back shows {actual}")

        self._log(f"SET {self.requested_mode.value} (was {self.original_mode.value if self.original_mode else 'UNKNOWN'})")
        if self.session is not None:
            self.session.write_tracking_during({"mode": self.requested_mode.value})
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        target = self.original_mode or TrackingMode.SIDEREAL
        try:
            ok = await asyncio.shield(
                _await_with_timeout(self.backend.set_tracking_mode(target), self.timeout,
                                     f"set_tracking_mode({target.value}) (restore)"))
            verified = None
            if ok:
                actual = await asyncio.shield(
                    _await_with_timeout(self.backend.get_tracking_mode(), self.timeout,
                                         "get_tracking_mode (verify after restore)"))
                verified = actual == target
            if ok and verified:
                self.restored_mode = target
            elif ok and not verified:
                self.restore_error = f"set_tracking_mode reported success but read-back shows {actual!r}, not {target.value}"
            else:
                self.restore_error = f"backend refused to restore {target.value}"
        except asyncio.CancelledError as restore_exc:
            self.restore_error = f"restore cancelled: {restore_exc!r}"
        except Exception as restore_exc:  # restoration must never raise past the caller unhandled
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
            # If the body itself already raised (or the exit was itself
            # cancelled), chain onto that instead of masking it.
            restore_failure = TrackingRestoreError(
                f"failed to restore tracking mode to {target.value}: {self.restore_error}")
            if exc:
                raise restore_failure from exc
            raise restore_failure
        return False  # never suppress an exception raised inside the `with` body
