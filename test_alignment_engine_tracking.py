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


def test_real_backend_is_intentionally_unimplemented():
    with pytest.raises(NotImplementedError):
        RealTrackingBackend()
