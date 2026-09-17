import pytest

from alignment_engine.tracking import (
    FailingTrackingBackend, RealTrackingBackend, SimulatedTrackingBackend,
    TrackingMode, TrackingRestoreError, TrackingSession,
)


class SucceedsOnceThenFails:
    """set succeeds the first time (entering the session), fails every time
    after (simulating a mount that refuses to restore on the way out)."""

    def __init__(self, initial):
        self._mode = initial
        self._calls = 0

    def get_tracking_mode(self):
        return self._mode

    def set_tracking_mode(self, mode):
        self._calls += 1
        if self._calls == 1:
            self._mode = mode
            return True
        return False


def test_simulated_backend_round_trip():
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    assert backend.get_tracking_mode() == TrackingMode.SIDEREAL
    assert backend.set_tracking_mode(TrackingMode.SOLAR) is True
    assert backend.get_tracking_mode() == TrackingMode.SOLAR


def test_restores_original_mode_on_clean_success():
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    with TrackingSession(backend, TrackingMode.SOLAR) as session:
        assert backend.get_tracking_mode() == TrackingMode.SOLAR
        assert session.original_mode == TrackingMode.SIDEREAL
    assert backend.get_tracking_mode() == TrackingMode.SIDEREAL


def test_restores_original_mode_on_exception():
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    with pytest.raises(RuntimeError, match="boom"):
        with TrackingSession(backend, TrackingMode.SOLAR):
            raise RuntimeError("boom")
    assert backend.get_tracking_mode() == TrackingMode.SIDEREAL


def test_restores_original_mode_on_keyboard_interrupt():
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    with pytest.raises(KeyboardInterrupt):
        with TrackingSession(backend, TrackingMode.SOLAR):
            raise KeyboardInterrupt()
    assert backend.get_tracking_mode() == TrackingMode.SIDEREAL


def test_set_failure_on_entry_never_enters_the_body():
    backend = FailingTrackingBackend(TrackingMode.SIDEREAL)
    entered = False
    with pytest.raises(RuntimeError, match="failed to set"):
        with TrackingSession(backend, TrackingMode.SOLAR):
            entered = True
    assert entered is False
    # never actually left SIDEREAL, since set_tracking_mode always failed
    assert backend.get_tracking_mode() == TrackingMode.SIDEREAL


def test_restore_failure_on_exit_is_surfaced_loudly_not_swallowed():
    backend = SucceedsOnceThenFails(TrackingMode.SIDEREAL)
    with pytest.raises(TrackingRestoreError):
        with TrackingSession(backend, TrackingMode.SOLAR):
            pass


def test_restore_failure_chains_onto_original_exception_rather_than_masking_it():
    backend = SucceedsOnceThenFails(TrackingMode.SIDEREAL)
    with pytest.raises(TrackingRestoreError) as excinfo:
        with TrackingSession(backend, TrackingMode.SOLAR):
            raise ValueError("scan failed mid-way")
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_session_log_records_set_and_restore(tmp_path):
    from alignment_engine.session import AlignmentSession
    session = AlignmentSession(tmp_path, "SOLAR-TEST")
    backend = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    with TrackingSession(backend, TrackingMode.SOLAR, session):
        pass
    before = session.dir.joinpath("tracking_before.json")
    during = session.dir.joinpath("tracking_during.json")
    after = session.dir.joinpath("tracking_after.json")
    assert before.exists() and during.exists() and after.exists()
    import json
    assert json.loads(before.read_text())["mode"] == "SIDEREAL"
    assert json.loads(during.read_text())["mode"] == "SOLAR"
    assert json.loads(after.read_text())["restored"] is True


def test_real_backend_sync_facade_is_intentionally_unimplemented():
    """The TrackingBackend Protocol's sync get/set_tracking_mode() are
    deliberately NOT wired to real I/O (see the class docstring's "OPEN
    DESIGN QUESTION" about bridging async INDI I/O into TrackingSession's
    sync calls) - constructing the backend itself must succeed (it does no
    I/O), only the sync facade must refuse."""
    backend = RealTrackingBackend()
    with pytest.raises(NotImplementedError):
        backend.get_tracking_mode()
    with pytest.raises(NotImplementedError):
        backend.set_tracking_mode(TrackingMode.SOLAR)


def test_real_backend_execute_refuses_without_explicit_authorization():
    """execute_set_tracking_mode() must refuse (never silently no-op, never
    silently write) unless allow_real_writes=True - and this pass never
    passes that flag anywhere in this codebase."""
    import asyncio
    backend = RealTrackingBackend(allow_real_writes=False)
    xml = backend.prepare_set_tracking_mode(TrackingMode.SOLAR)
    with pytest.raises(RuntimeError, match="allow_real_writes=False"):
        asyncio.run(backend.execute_set_tracking_mode(xml))


@pytest.mark.parametrize("mode,expected_element", [
    (TrackingMode.SIDEREAL, "TRACK_SIDEREAL"),
    (TrackingMode.SOLAR, "TRACK_SOLAR"),
    (TrackingMode.LUNAR, "TRACK_LUNAR"),
    (TrackingMode.CUSTOM, "TRACK_CUSTOM"),
])
def test_real_backend_prepare_set_tracking_mode_is_pure_and_exact(mode, expected_element):
    """prepare_set_tracking_mode() must do no I/O (constructing with an
    unreachable host proves it) and must turn exactly one element On."""
    backend = RealTrackingBackend(host="192.0.2.1")  # TEST-NET-1, guaranteed unreachable
    xml = backend.prepare_set_tracking_mode(mode)
    assert f'device="LX200 OnStep"' in xml
    assert f'name="TELESCOPE_TRACK_MODE"' in xml
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


def test_inspect_against_real_live_indiserver_confirms_track_mode_property():
    """Read-only inspection against whatever real indiserver this test
    machine has running, if any - authorized (Fase 5: read-only INDI audit).
    Skips cleanly if no indiserver is reachable (e.g. CI without hardware);
    never sends anything but a single getProperties, exactly like
    `indi_getprop` itself."""
    import asyncio
    backend = RealTrackingBackend()
    try:
        mode = asyncio.run(backend.inspect(timeout=2.0))
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no reachable indiserver for a live read-only check: {exc}")
    if mode is None:
        pytest.skip("indiserver reachable but TELESCOPE_TRACK_MODE not read within timeout "
                    "(no real mount driver running in this environment)")
    assert isinstance(mode, TrackingMode)


# ---- pre-hardware pass item 2: "no asumas SIDEREAL como regla fija" -----
# TrackingSession.__exit__ restores whatever get_tracking_mode() reported
# on entry, not a hardcoded TrackingMode.SIDEREAL constant. These tests
# fail if that logic ever regresses to a hardcoded default.


def test_restores_original_lunar_mode_not_hardcoded_sidereal():
    backend = SimulatedTrackingBackend(TrackingMode.LUNAR)
    with TrackingSession(backend, TrackingMode.SOLAR) as session:
        assert session.original_mode == TrackingMode.LUNAR
        assert backend.get_tracking_mode() == TrackingMode.SOLAR
    assert backend.get_tracking_mode() == TrackingMode.LUNAR


def test_restores_original_custom_mode_on_exception_not_hardcoded_sidereal():
    backend = SimulatedTrackingBackend(TrackingMode.CUSTOM)
    with pytest.raises(RuntimeError, match="boom"):
        with TrackingSession(backend, TrackingMode.SOLAR):
            raise RuntimeError("boom")
    assert backend.get_tracking_mode() == TrackingMode.CUSTOM


def test_restores_original_lunar_mode_on_keyboard_interrupt():
    backend = SimulatedTrackingBackend(TrackingMode.LUNAR)
    with pytest.raises(KeyboardInterrupt):
        with TrackingSession(backend, TrackingMode.SOLAR):
            raise KeyboardInterrupt()
    assert backend.get_tracking_mode() == TrackingMode.LUNAR


def test_restores_original_custom_mode_on_cancellation_style_early_return():
    """A cancelled run typically breaks out of the `with` body normally
    (no exception) rather than raising - confirm that path (not just the
    exception paths above) also restores the true original, not SIDEREAL."""
    backend = SimulatedTrackingBackend(TrackingMode.CUSTOM)
    with TrackingSession(backend, TrackingMode.SOLAR):
        pass  # operator/engine decided to stop; body exits normally
    assert backend.get_tracking_mode() == TrackingMode.CUSTOM


def test_unknown_original_mode_falls_back_to_sidereal_and_this_is_documented():
    """The ONLY case TrackingSession defaults to SIDEREAL: the backend
    itself cannot report an original mode at all (get_tracking_mode() ->
    None) - a real backend limitation, not an assumption this package
    makes about what mode the mount was "probably" in."""
    class UnknownInitialBackend:
        def __init__(self):
            self._mode = None

        def get_tracking_mode(self):
            return self._mode

        def set_tracking_mode(self, mode):
            self._mode = mode
            return True

    backend = UnknownInitialBackend()
    with TrackingSession(backend, TrackingMode.SOLAR) as session:
        assert session.original_mode is None
    assert backend.get_tracking_mode() == TrackingMode.SIDEREAL
