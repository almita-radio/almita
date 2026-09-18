"""Async tracking contract + TrackingSession (3rd pass, items 1/2/16).

TrackingBackend is now an async Protocol - SimulatedTrackingBackend,
FailingTrackingBackend and RealTrackingBackend all implement the SAME
get_tracking_mode()/set_tracking_mode() async methods, and TrackingSession
is an async context manager. Every test below uses `async with` /
`pytest.mark.asyncio` accordingly - this file replaces the previous
sync-context-manager version wholesale.
"""
import asyncio

import pytest

from alignment_engine.tracking import (
    FailingTrackingBackend, RealTrackingBackend, SimulatedTrackingBackend,
    TrackingMode, TrackingModeMismatchError, TrackingOperationTimeout,
    TrackingRestoreError, TrackingSession,
)


class SucceedsOnceThenFails:
    """set succeeds the first time (entering the session), fails every time
    after (simulating a mount that refuses to restore on the way out)."""

    def __init__(self, initial):
        self._mode = initial
        self._calls = 0

    async def get_tracking_mode(self):
        return self._mode

    async def set_tracking_mode(self, mode):
        self._calls += 1
        if self._calls == 1:
            self._mode = mode
            return True
        return False


class ClaimsSuccessButDoesNotActuallyChange:
    """set_tracking_mode() returns True but the backend's real state never
    moves - exercises the verify-after-set/verify-after-restore mismatch
    path (item 2's "verificar modo esperado" requirement), something the
    2nd pass's design (which trusted the return value alone) could not
    catch."""

    def __init__(self, initial):
        self._mode = initial

    async def get_tracking_mode(self):
        return self._mode

    async def set_tracking_mode(self, mode):
        return True  # lies


class HangingBackend:
    """Never completes - for timeout tests (item 4)."""

    def __init__(self, initial=TrackingMode.SIDEREAL):
        self._mode = initial

    async def get_tracking_mode(self):
        await asyncio.sleep(999)

    async def set_tracking_mode(self, mode):
        await asyncio.sleep(999)


# ---------------------------------------------------------------- basics


@pytest.mark.asyncio
async def test_simulated_backend_round_trip():
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    assert await backend.get_tracking_mode() == TrackingMode.SIDEREAL
    assert await backend.set_tracking_mode(TrackingMode.SOLAR) is True
    assert await backend.get_tracking_mode() == TrackingMode.SOLAR


@pytest.mark.asyncio
async def test_restores_original_mode_on_clean_success():
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    async with TrackingSession(backend, TrackingMode.SOLAR) as session:
        assert await backend.get_tracking_mode() == TrackingMode.SOLAR
        assert session.original_mode == TrackingMode.SIDEREAL
    assert await backend.get_tracking_mode() == TrackingMode.SIDEREAL


@pytest.mark.asyncio
async def test_restores_original_mode_on_exception():
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    with pytest.raises(RuntimeError, match="boom"):
        async with TrackingSession(backend, TrackingMode.SOLAR):
            raise RuntimeError("boom")
    assert await backend.get_tracking_mode() == TrackingMode.SIDEREAL


@pytest.mark.asyncio
async def test_restores_original_mode_on_keyboard_interrupt():
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    with pytest.raises(KeyboardInterrupt):
        async with TrackingSession(backend, TrackingMode.SOLAR):
            raise KeyboardInterrupt()
    assert await backend.get_tracking_mode() == TrackingMode.SIDEREAL


@pytest.mark.asyncio
async def test_restores_original_mode_on_cancelled_error():
    """asyncio.CancelledError must reach __aexit__ and trigger restoration
    exactly like any other exception - item 2/3's central requirement."""
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    with pytest.raises(asyncio.CancelledError):
        async with TrackingSession(backend, TrackingMode.SOLAR):
            raise asyncio.CancelledError()
    assert await backend.get_tracking_mode() == TrackingMode.SIDEREAL


@pytest.mark.asyncio
async def test_real_task_cancellation_during_body_still_restores():
    """A more realistic cancellation: an actual asyncio.Task cancelled
    while awaiting inside the `async with` body (not a hand-raised
    CancelledError) - the closest thing to a real Ctrl+C/task.cancel()
    reaching mid-scan."""
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    entered = asyncio.Event()

    async def body():
        async with TrackingSession(backend, TrackingMode.SOLAR):
            entered.set()
            await asyncio.sleep(999)

    task = asyncio.ensure_future(body())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await backend.get_tracking_mode() == TrackingMode.SIDEREAL


@pytest.mark.asyncio
async def test_set_failure_on_entry_never_enters_the_body():
    backend = FailingTrackingBackend(TrackingMode.SIDEREAL)
    entered = False
    with pytest.raises(RuntimeError, match="failed to set"):
        async with TrackingSession(backend, TrackingMode.SOLAR):
            entered = True
    assert entered is False
    # never actually left SIDEREAL, since set_tracking_mode always failed
    assert await backend.get_tracking_mode() == TrackingMode.SIDEREAL


@pytest.mark.asyncio
async def test_restore_failure_on_exit_is_surfaced_loudly_not_swallowed():
    backend = SucceedsOnceThenFails(TrackingMode.SIDEREAL)
    with pytest.raises(TrackingRestoreError):
        async with TrackingSession(backend, TrackingMode.SOLAR):
            pass


@pytest.mark.asyncio
async def test_restore_failure_chains_onto_original_exception_rather_than_masking_it():
    backend = SucceedsOnceThenFails(TrackingMode.SIDEREAL)
    with pytest.raises(TrackingRestoreError) as excinfo:
        async with TrackingSession(backend, TrackingMode.SOLAR):
            raise ValueError("scan failed mid-way")
    assert isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_session_log_records_set_and_restore(tmp_path):
    from alignment_engine.session import AlignmentSession
    session = AlignmentSession(tmp_path, "SOLAR-TEST")
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    async with TrackingSession(backend, TrackingMode.SOLAR, session):
        pass
    before = session.dir.joinpath("tracking_before.json")
    during = session.dir.joinpath("tracking_during.json")
    after = session.dir.joinpath("tracking_after.json")
    assert before.exists() and during.exists() and after.exists()
    import json
    assert json.loads(before.read_text())["mode"] == "SIDEREAL"
    assert json.loads(during.read_text())["mode"] == "SOLAR"
    assert json.loads(after.read_text())["restored"] is True


# ---------------------------------------------------------------- verify (item 2)


@pytest.mark.asyncio
async def test_enter_verification_catches_a_backend_that_lies_about_success():
    backend = ClaimsSuccessButDoesNotActuallyChange(TrackingMode.SIDEREAL)
    with pytest.raises(TrackingModeMismatchError):
        async with TrackingSession(backend, TrackingMode.SOLAR):
            pass  # pragma: no cover - must never be reached


@pytest.mark.asyncio
async def test_exit_verification_catches_a_backend_that_lies_about_restoring():
    """set_tracking_mode(original) returns True during restore, but the
    read-back still shows SOLAR - must be reported as a restore failure,
    not silently accepted."""
    class LiesOnlyOnRestore:
        def __init__(self):
            self._mode = TrackingMode.SIDEREAL
            self._entered = False

        async def get_tracking_mode(self):
            return self._mode

        async def set_tracking_mode(self, mode):
            if not self._entered:
                self._entered = True
                self._mode = mode
                return True
            return True  # claims success but never actually changes _mode back

    backend = LiesOnlyOnRestore()
    with pytest.raises(TrackingRestoreError, match="read-back shows"):
        async with TrackingSession(backend, TrackingMode.SOLAR):
            pass


# ---------------------------------------------------------------- timeouts (item 4)


@pytest.mark.asyncio
async def test_enter_get_tracking_mode_timeout_raises_structured_error():
    backend = HangingBackend()
    with pytest.raises(TrackingOperationTimeout):
        async with TrackingSession(backend, TrackingMode.SOLAR, timeout=0.05):
            pass  # pragma: no cover


@pytest.mark.asyncio
async def test_enter_set_tracking_mode_timeout_raises_structured_error():
    class HangsOnlyOnSet:
        async def get_tracking_mode(self):
            return TrackingMode.SIDEREAL

        async def set_tracking_mode(self, mode):
            await asyncio.sleep(999)

    with pytest.raises(TrackingOperationTimeout):
        async with TrackingSession(HangsOnlyOnSet(), TrackingMode.SOLAR, timeout=0.05):
            pass  # pragma: no cover


@pytest.mark.asyncio
async def test_restore_timeout_is_reported_as_restore_error_not_silently_lost():
    class HangsOnlyOnRestore:
        def __init__(self):
            self._mode = TrackingMode.SIDEREAL
            self._entered = False

        async def get_tracking_mode(self):
            return self._mode

        async def set_tracking_mode(self, mode):
            if not self._entered:
                self._entered = True
                self._mode = mode
                return True
            await asyncio.sleep(999)

    with pytest.raises(TrackingRestoreError):
        async with TrackingSession(HangsOnlyOnRestore(), TrackingMode.SOLAR, timeout=0.05):
            pass


# ---------------------- real backend: async contract + safety gates ------


@pytest.mark.asyncio
async def test_real_backend_implements_the_same_async_contract_as_simulated():
    """No separate sync facade any more (item 1: identical contract) -
    get_tracking_mode/set_tracking_mode are both `async def` on both
    backends. Uses an unreachable host so get_tracking_mode() returns None
    quickly rather than touching any real network."""
    backend = RealTrackingBackend(host="192.0.2.1", default_timeout=0.2)
    result = await backend.get_tracking_mode()
    assert result is None  # unreachable -> None, never raises


@pytest.mark.asyncio
async def test_real_backend_set_tracking_mode_refuses_without_explicit_authorization():
    """set_tracking_mode() must refuse (raise, never silently no-op or
    return True) unless allow_real_writes=True - and this pass never
    passes that flag anywhere in this codebase."""
    backend = RealTrackingBackend(allow_real_writes=False)
    with pytest.raises(RuntimeError, match="allow_real_writes=False"):
        await backend.set_tracking_mode(TrackingMode.SOLAR)


@pytest.mark.asyncio
async def test_real_backend_execute_refuses_without_explicit_authorization():
    backend = RealTrackingBackend(allow_real_writes=False)
    xml = backend.prepare_set_tracking_mode(TrackingMode.SOLAR)
    with pytest.raises(RuntimeError, match="allow_real_writes=False"):
        await backend.execute_set_tracking_mode(xml)


@pytest.mark.parametrize("mode,expected_element", [
    (TrackingMode.SIDEREAL, "TRACK_SIDEREAL"),
    (TrackingMode.SOLAR, "TRACK_SOLAR"),
    (TrackingMode.LUNAR, "TRACK_LUNAR"),
    (TrackingMode.CUSTOM, "TRACK_CUSTOM"),
])
def test_real_backend_prepare_set_tracking_mode_is_pure_and_exact(mode, expected_element):
    """prepare_set_tracking_mode() must do no I/O (constructing with an
    unreachable host proves it) and must turn exactly one element On. This
    one test stays a plain (non-async) function - the method itself is
    sync/pure by design (item 1: "prepare_sync puede seguir siendo puro")."""
    backend = RealTrackingBackend(host="192.0.2.1")  # TEST-NET-1, guaranteed unreachable
    xml = backend.prepare_set_tracking_mode(mode)
    assert 'device="LX200 OnStep"' in xml
    assert 'name="TELESCOPE_TRACK_MODE"' in xml
    assert f'<oneSwitch name="{expected_element}">On</oneSwitch>' in xml
    other_elements = [e for e in RealTrackingBackend.ELEMENT_BY_MODE.values() if e != expected_element]
    for element in other_elements:
        assert f'<oneSwitch name="{element}">Off</oneSwitch>' in xml


def test_real_backend_prepare_set_tracking_mode_rejects_unknown_mode():
    backend = RealTrackingBackend(host="192.0.2.1")
    with pytest.raises(ValueError):
        backend.prepare_set_tracking_mode("not-a-real-mode")


def test_parse_track_mode_xml_reads_the_one_on_element():
    from alignment_engine.tracking import _parse_track_mode_xml
    raw = (
        '<defSwitchVector device="LX200 OnStep" name="TELESCOPE_TRACK_MODE">'
        '<defSwitch name="TRACK_SIDEREAL">On</defSwitch>'
        '<defSwitch name="TRACK_SOLAR">Off</defSwitch>'
        '<defSwitch name="TRACK_LUNAR">Off</defSwitch>'
        '<defSwitch name="TRACK_CUSTOM">Off</defSwitch>'
        '</defSwitchVector>'
    )
    mode = _parse_track_mode_xml(raw, RealTrackingBackend.MODE_BY_ELEMENT)
    assert mode == TrackingMode.SIDEREAL


def test_parse_track_mode_xml_returns_none_for_ambiguous_vector():
    """Never guess: zero or more than one element On is not a valid state
    to silently resolve to some default."""
    from alignment_engine.tracking import _parse_track_mode_xml
    all_off = (
        '<defSwitchVector device="LX200 OnStep" name="TELESCOPE_TRACK_MODE">'
        '<defSwitch name="TRACK_SIDEREAL">Off</defSwitch>'
        '<defSwitch name="TRACK_SOLAR">Off</defSwitch>'
        '</defSwitchVector>'
    )
    assert _parse_track_mode_xml(all_off, RealTrackingBackend.MODE_BY_ELEMENT) is None


@pytest.mark.asyncio
async def test_inspect_against_real_live_indiserver_confirms_track_mode_property():
    """Read-only inspection against whatever real indiserver this test
    machine has running, if any - authorized (Fase 5/9: read-only INDI
    audit). Skips cleanly if no indiserver is reachable (e.g. CI without
    hardware); never sends anything but a single getProperties, exactly
    like `indi_getprop` itself."""
    backend = RealTrackingBackend()
    try:
        mode = await backend.inspect(timeout=2.0)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no reachable indiserver for a live read-only check: {exc}")
    if mode is None:
        pytest.skip("indiserver reachable but TELESCOPE_TRACK_MODE not read within timeout "
                    "(no real mount driver running in this environment)")
    assert isinstance(mode, TrackingMode)


@pytest.mark.asyncio
async def test_get_tracking_mode_against_real_live_indiserver_agrees_with_inspect():
    """Same live check via the actual TrackingBackend-contract method
    (get_tracking_mode()), not just inspect() directly - confirms the
    contract method really is wired to the same read-only query."""
    backend = RealTrackingBackend()
    try:
        via_contract = await backend.get_tracking_mode(timeout=2.0)
        via_inspect = await backend.inspect(timeout=2.0)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no reachable indiserver for a live read-only check: {exc}")
    if via_contract is None:
        pytest.skip("indiserver reachable but TELESCOPE_TRACK_MODE not read within timeout")
    assert via_contract == via_inspect


def test_real_backend_is_never_constructed_with_writes_allowed_by_default():
    """Safety-by-default: allow_real_writes must default to False."""
    backend = RealTrackingBackend()
    assert backend.allow_real_writes is False


# ---- pre-hardware pass item 2: "no asumas SIDEREAL como regla fija" -----
# TrackingSession.__aexit__ restores whatever get_tracking_mode() reported
# on entry, not a hardcoded TrackingMode.SIDEREAL constant. These tests
# fail if that logic ever regresses to a hardcoded default. Item 16 asks
# explicitly for SIDEREAL/SOLAR/LUNAR/CUSTOM as the ORIGINAL mode.


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [TrackingMode.SIDEREAL, TrackingMode.SOLAR,
                                       TrackingMode.LUNAR, TrackingMode.CUSTOM])
async def test_restores_original_mode_whatever_it_was_on_clean_success(original):
    backend = SimulatedTrackingBackend(original)
    requested = TrackingMode.SOLAR if original != TrackingMode.SOLAR else TrackingMode.SIDEREAL
    async with TrackingSession(backend, requested) as session:
        assert session.original_mode == original
        assert await backend.get_tracking_mode() == requested
    assert await backend.get_tracking_mode() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [TrackingMode.SIDEREAL, TrackingMode.SOLAR,
                                       TrackingMode.LUNAR, TrackingMode.CUSTOM])
async def test_restores_original_mode_whatever_it_was_on_exception(original):
    backend = SimulatedTrackingBackend(original)
    requested = TrackingMode.SOLAR if original != TrackingMode.SOLAR else TrackingMode.SIDEREAL
    with pytest.raises(RuntimeError, match="boom"):
        async with TrackingSession(backend, requested):
            raise RuntimeError("boom")
    assert await backend.get_tracking_mode() == original


@pytest.mark.asyncio
async def test_restores_original_lunar_mode_on_keyboard_interrupt():
    backend = SimulatedTrackingBackend(TrackingMode.LUNAR)
    with pytest.raises(KeyboardInterrupt):
        async with TrackingSession(backend, TrackingMode.SOLAR):
            raise KeyboardInterrupt()
    assert await backend.get_tracking_mode() == TrackingMode.LUNAR


@pytest.mark.asyncio
async def test_restores_original_custom_mode_on_cancellation_style_early_return():
    """A cancelled run typically breaks out of the body normally (no
    exception) rather than raising - confirm that path (not just the
    exception paths above) also restores the true original, not SIDEREAL."""
    backend = SimulatedTrackingBackend(TrackingMode.CUSTOM)
    async with TrackingSession(backend, TrackingMode.SOLAR):
        pass  # operator/engine decided to stop; body exits normally
    assert await backend.get_tracking_mode() == TrackingMode.CUSTOM


@pytest.mark.asyncio
async def test_unknown_original_mode_falls_back_to_sidereal_and_this_is_documented():
    """The ONLY case TrackingSession defaults to SIDEREAL: the backend
    itself cannot report an original mode at all (get_tracking_mode() ->
    None) - a real backend limitation, not an assumption this package
    makes about what mode the mount was "probably" in."""
    class UnknownInitialBackend:
        def __init__(self):
            self._mode = None

        async def get_tracking_mode(self):
            return self._mode

        async def set_tracking_mode(self, mode):
            self._mode = mode
            return True

    backend = UnknownInitialBackend()
    async with TrackingSession(backend, TrackingMode.SOLAR) as session:
        assert session.original_mode is None
    assert await backend.get_tracking_mode() == TrackingMode.SIDEREAL
