"""Tests for the ALIGN/CALIBRATE routes added to almita_orchestrator_server.py
(Fase 48). Same running_server() pattern as test_orchestrator_web.py - a
real ThreadingHTTPServer on a random port, real HTTP requests. Confirms:
simulation end-to-end, real hardware policy-blocked states, sync always
blocked, replay never touches hardware, session detail, invalid session,
and - critically - that path traversal is rejected at the HTTP boundary
even when a client bypasses any frontend validation entirely (Fase 44/57).
"""
import contextlib
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import almita_orchestrator_server as server_mod


@contextlib.contextmanager
def running_server():
    httpd = server_mod.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read())


def _post(url, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _wait_for_session(base, kind, session_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, body = _get(f"{base}/api/{kind}/session/{session_id}")
        if body["data"] and not body["data"]["job_running"]:
            return body
        time.sleep(0.3)
    raise TimeoutError(f"session {session_id} did not finish within {timeout}s")


# ---------------------------------------------------------------- static pages unchanged

def test_observe_page_still_loads(running_server=running_server):
    with running_server() as base:
        with urllib.request.urlopen(f"{base}/observe.html", timeout=10) as r:
            assert r.status == 200
            assert b"ALMITA OBSERVE" in r.read()


def test_align_and_calibrate_pages_load():
    with running_server() as base:
        for page, title in (("align.html", b"ALMITA ALIGN"), ("calibrate.html", b"ALMITA CALIBRATE")):
            with urllib.request.urlopen(f"{base}/{page}", timeout=10) as r:
                assert r.status == 200
                assert title in r.read()


# ---------------------------------------------------------------- ALIGN

def test_align_status_never_crashes_and_reports_hi_first_light():
    with running_server() as base:
        status, body = _get(f"{base}/api/align/status")
        assert status == 200
        assert body["data"]["hi"]["phase"] == "FIRST_LIGHT_HI"
        assert body["data"]["hi"]["sync"]["sync_allowed"] is False


def test_align_sync_prepare_and_apply_always_blocked_hi():
    with running_server() as base:
        for step in ("prepare", "apply"):
            status, body = _post(f"{base}/api/align/sync/{step}/hi", {})
            assert status == 200
            assert body["blocked"] is True
            assert "FIRST_LIGHT_HI" in body["reason"]


def test_align_sync_prepare_and_apply_always_blocked_solar():
    with running_server() as base:
        for step in ("prepare", "apply"):
            status, body = _post(f"{base}/api/align/sync/{step}/solar", {})
            assert status == 200
            assert body["blocked"] is True
            assert "not" in body["reason"].lower()


def test_align_hi_plan_preflight_run_end_to_end():
    """The one real end-to-end proof (like test_orchestrator_web.py's own
    "only one real call" policy) - HI has enough sky targets that a
    preflight pass is realistically available regardless of time of day,
    unlike Solar."""
    with running_server() as base:
        status, body = _post(f"{base}/api/align/plan/hi", {})
        assert status == 200 and not body["blocked"]
        session_id = body["data"]["session_id"]
        try:
            status, body = _post(f"{base}/api/align/preflight/hi", {"session_id": session_id})
            assert status == 200
            if body["blocked"]:
                pytest.skip(f"HI not currently visible: {body['reason']}")
            status, body = _post(f"{base}/api/align/run/hi", {"session_id": session_id})
            assert status == 200 and not body["blocked"]
            final = _wait_for_session(base, "align", session_id)
            assert final["data"]["result"] is not None
            assert final["data"]["result"]["final_fit"]["quality"]["rating"] in ("GOOD", "MARGINAL", "BAD")
        finally:
            shutil.rmtree(Path("data/alignment") / session_id, ignore_errors=True)


def test_align_session_not_found_reports_blocked_not_500():
    with running_server() as base:
        status, body = _get(f"{base}/api/align/session/NOT-A-REAL-SESSION")
        assert status == 200
        assert body["blocked"] is True
        assert body["data"] is None


def test_align_replay_rejects_session_dir_outside_root():
    with running_server() as base:
        status, body = _post(f"{base}/api/align/replay", {"session_dir": "/etc/passwd"})
        assert status == 200
        assert body["blocked"] is True
        assert "data/alignment" in body["reason"]


def test_align_run_rejects_session_id_outside_root():
    with running_server() as base:
        status, body = _post(f"{base}/api/align/run/hi", {"session_id": "/tmp/evil"})
        assert status == 200
        assert body["blocked"] is True
        assert not Path("/tmp/evil").exists()


def test_align_compare_rejects_paths_outside_root():
    with running_server() as base:
        status, body = _post(f"{base}/api/align/compare", {"analysis_dir_a": "/etc", "analysis_dir_b": "/etc"})
        assert status == 200
        assert body["blocked"] is True


# ---------------------------------------------------------------- CALIBRATE

def test_calibrate_status_shows_operational_relative_and_verification_tiers():
    with running_server() as base:
        status, body = _get(f"{base}/api/calibrate/status")
        assert status == 200
        assert body["data"]["calibration_level"] == "OPERATIONAL_RELATIVE"
        assert body["data"]["absolute_calibration"] is False
        assert "gain_table" in body["data"]


def test_calibrate_run_simulation_end_to_end_and_quality_present():
    with running_server() as base:
        status, body = _post(f"{base}/api/calibrate/run",
                              {"scenario": "HEALTHY", "n_captures": 3, "capture_seconds": 0.3})
        assert status == 200 and not body["blocked"]
        session_id = body["data"]["session_id"]
        try:
            final = _wait_for_session(base, "calibrate", session_id)
            assert final["data"]["result"]["quality"]["verdict"] in ("GOOD", "MARGINAL", "BAD", "INCONCLUSIVE")
            assert final["data"]["result"]["absolute_calibration"] is False
        finally:
            shutil.rmtree(Path("data/calibration") / session_id, ignore_errors=True)


def test_calibrate_run_simulation_clipped_scenario_reports_bad():
    """n_captures=3 is the real minimum stability.analyze_stability()
    itself requires (see calibration_engine/stability.py) - below that,
    evaluate_quality() correctly returns INCONCLUSIVE regardless of
    clipping, since it refuses to rate GOOD/BAD without a stability
    number at all. That is the engine's own rule, not a web-layer choice,
    so this test uses enough captures to actually reach a verdict."""
    with running_server() as base:
        status, body = _post(f"{base}/api/calibrate/run",
                              {"scenario": "CLIPPED", "n_captures": 3, "capture_seconds": 0.3})
        assert not body["blocked"]
        session_id = body["data"]["session_id"]
        try:
            final = _wait_for_session(base, "calibrate", session_id)
            assert final["data"]["result"]["quality"]["verdict"] == "BAD"
        finally:
            shutil.rmtree(Path("data/calibration") / session_id, ignore_errors=True)


def test_calibrate_session_not_found_reports_blocked_not_500():
    with running_server() as base:
        status, body = _get(f"{base}/api/calibrate/session/NOT-A-REAL-SESSION")
        assert status == 200
        assert body["blocked"] is True


def test_calibrate_replay_rejects_path_outside_root():
    with running_server() as base:
        status, body = _post(f"{base}/api/calibrate/replay", {"session_dir": "/etc"})
        assert status == 200
        assert body["blocked"] is True


def test_calibrate_compare_rejects_session_ids_outside_root_and_never_creates_directories():
    with running_server() as base:
        status, body = _post(f"{base}/api/calibrate/compare",
                              {"session_a": "../../../../tmp/evil_a", "session_b": "../../../../tmp/evil_b"})
        assert status == 200
        assert body["blocked"] is True
        assert not Path("/tmp/evil_a").exists()
        assert not Path("/tmp/evil_b").exists()


def test_calibrate_profile_build_rejects_session_id_outside_root():
    with running_server() as base:
        status, body = _post(f"{base}/api/calibrate/profile/build", {"session_id": "/tmp/evil_profile"})
        assert status == 200
        assert body["blocked"] is True


# ---------------------------------------------------------------- path traversal (Fase 57), literal ".."

def test_align_session_dotdot_id_is_rejected_not_the_parent_directory():
    with running_server() as base:
        req = urllib.request.Request(f"{base}/api/align/session/..")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        assert body["blocked"] is True
        assert body["data"] is None


def test_calibrate_session_dotdot_id_is_rejected_not_the_parent_directory():
    with running_server() as base:
        req = urllib.request.Request(f"{base}/api/calibrate/session/..")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        assert body["blocked"] is True
        assert body["data"] is None


# ---------------------------------------------------------------- unrelated route unaffected

def test_unknown_align_calibrate_route_is_404():
    with running_server() as base:
        req = urllib.request.Request(f"{base}/api/align/not-a-real-route", method="GET")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=10)
        assert exc_info.value.code == 404
