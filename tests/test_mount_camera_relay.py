"""MONITOR mount-camera relay: what a viewer is told while the single upstream connection is being retried.

No camera and no network: urllib.request.urlopen is replaced by a fake. Found in the journal of 2026-10-11/12:
/mount_camera/stream answered 503 and, a minute later, 200 - the relay marked every failed connect attempt as
ERROR, so a viewer whose wait ended between two attempts was refused although the next attempt connected."""
import threading
import time

import pytest

import mount_camera


class _FakeResponse:
    headers = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}

    def __init__(self, release):
        self._release = release

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, _n):
        if self._release.wait(timeout=0.02):
            return b""                       # the "camera" closes its end
        return b"--frame\r\n\xff\xd8jpeg\xff\xd9\r\n"


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setattr(mount_camera, "CONFIG_PATH", tmp_path / "mount_camera_config.json")
    monkeypatch.setattr(mount_camera, "RETRY_COOLDOWN_S", 0.15)
    return mount_camera.MountCameraRelay()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_failed_attempt_with_a_retry_pending_is_connecting_not_error(relay, monkeypatch):
    release, attempts = threading.Event(), []

    def fake_urlopen(req, timeout=None):
        attempts.append(req.full_url)
        if len(attempts) < 3:
            raise ConnectionRefusedError(111, "Connection refused")   # single-client slot not freed yet
        return _FakeResponse(release)
    monkeypatch.setattr(mount_camera.urllib.request, "urlopen", fake_urlopen)

    sub = relay.subscribe()
    try:
        assert _wait_for(lambda: len(attempts) >= 1 and relay.status()["last_error"])
        seen = set()
        while len(attempts) < 3:
            seen.add(relay.status()["state"])
            time.sleep(0.005)
        # between attempts the viewer's request must not be answered 503: the state it reads is never the final ERROR
        assert "CONNECTING" in seen and "STREAMING" not in seen
        relay.wait_connected_or_failed(5.0)
        st = relay.status()
        assert st["state"] == "STREAMING" and st["last_error"] is None and len(attempts) == 3
        assert _wait_for(lambda: bool(sub.read(timeout=0.2)))
    finally:
        relay.unsubscribe(sub)
        release.set()
        relay._stop_upstream.set()


def test_giving_up_reports_error_and_releases_every_waiting_viewer(relay, monkeypatch):
    attempts = []

    def fake_urlopen(req, timeout=None):
        attempts.append(1)
        raise TimeoutError("timed out")
    monkeypatch.setattr(mount_camera.urllib.request, "urlopen", fake_urlopen)

    first, second = relay.subscribe(), relay.subscribe()
    relay.wait_connected_or_failed(5.0)
    assert _wait_for(lambda: relay.status()["state"] == "ERROR")
    st = relay.status()
    assert len(attempts) == mount_camera.MAX_CONNECT_RETRIES and "TimeoutError" in st["last_error"]
    # nobody feeds these viewers any more: both are closed (read() -> None), never left waiting on a dead relay
    assert _wait_for(lambda: relay._thread is None)
    assert first.read(timeout=0.5) is None and second.read(timeout=0.5) is None
    relay.unsubscribe(first)
    relay.unsubscribe(second)

    # a later viewer starts a fresh upstream attempt (the give-up is not sticky)
    before = len(attempts)
    third = relay.subscribe()
    assert _wait_for(lambda: len(attempts) > before)
    relay.unsubscribe(third)
    relay._stop_upstream.set()


def test_the_failure_cause_is_logged(relay, monkeypatch, caplog):
    def fake_urlopen(req, timeout=None):
        raise ConnectionRefusedError(111, "Connection refused")
    monkeypatch.setattr(mount_camera.urllib.request, "urlopen", fake_urlopen)
    with caplog.at_level("WARNING", logger="almita.mount_camera"):
        sub = relay.subscribe()
        relay.wait_connected_or_failed(5.0)
        assert _wait_for(lambda: relay._thread is None)
        relay.unsubscribe(sub)
    text = caplog.text
    assert "ConnectionRefusedError" in text and "giving up after 4 connect attempts" in text
