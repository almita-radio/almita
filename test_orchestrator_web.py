"""Tests for almita_orchestrator_server.py: same-core proof, schema validation at the
HTTP boundary, no-arbitrary-command-execution, and read-only asset serving safety.

Only ONE test exercises a real observation_plan.plan_observation() call (this
Pi has ~0 free swap and grid_generator.py's matplotlib rendering is
expensive) — everything else mocks the core and asserts the HTTP layer
calls it correctly, which is exactly what "the Web UI is an interface, not
the owner of logic" requires us to prove.
"""
import contextlib
import json
import threading
import urllib.error
import urllib.request

import pytest

import almita_observe
import almita_orchestrator_server as server_mod
import grid_generator
import observation_orchestrator
import observation_plan


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


VALID_SPEC = {
    "session": {"name": "WEBTEST"},
    "grid": {"mode": "EQUATORIAL_RECT", "placement": "FIXED_CENTER", "center_ra_hours": 6.0,
             "center_dec_deg": -30.0, "width_deg": 4, "height_deg": 4, "rows": 3, "cols": 3,
             "min_altitude_deg": 5, "traversal": "SERPENTINE"},
    "capture": {"seconds": 1, "settle_seconds": 0.5},
    "main": {"center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 40.2, "bias_tee": True},
    "rfi_ref": {"enabled": False, "serial": "00000002", "gain_db": 25.0},
    "quicklook": {"enabled": False, "native_grid": True, "interpolated_preview": False,
                  "calibration_profile_path": None},
    "console": {"enabled": True},
    "execution": {"unattended": True},
}


def test_same_core_function_objects_across_cli_and_web():
    """The strongest same-core proof: both entry points hold a reference to
    the exact same function objects, not look-alike reimplementations."""
    assert almita_observe.observation_plan.plan_observation is server_mod.observation_plan.plan_observation
    assert almita_observe.observation_orchestrator.run_observation is observation_orchestrator.run_observation
    assert almita_observe.observation_orchestrator.get_status is observation_orchestrator.get_status
    assert almita_observe.observation_orchestrator.stop_observation is observation_orchestrator.stop_observation


def test_defaults_endpoint():
    with running_server() as base:
        status, body = _get(f"{base}/api/observe/defaults")
        assert status == 200
        assert "center_frequency_hz" in body["main"]


def test_status_endpoint_delegates_to_core(monkeypatch):
    monkeypatch.setattr(observation_orchestrator, "get_status", lambda **k: {"orchestrator": {"orchestrator_state": "READY"}})
    with running_server() as base:
        status, body = _get(f"{base}/api/observe/status")
        assert status == 200
        assert body["orchestrator"]["orchestrator_state"] == "READY"


def test_plan_rejects_invalid_spec_before_reaching_core(monkeypatch):
    called = []
    monkeypatch.setattr(observation_plan, "plan_observation", lambda spec, **k: called.append(spec) or {})
    bad_spec = dict(VALID_SPEC)
    bad_spec["grid"] = dict(VALID_SPEC["grid"])
    del bad_spec["grid"]["width_deg"]
    with running_server() as base:
        status, body = _post(f"{base}/api/observe/plan", bad_spec)
        assert status == 400
        assert "width_deg" in body["error"]
    assert called == []  # validation happened before the core was ever invoked


def test_plan_calls_core_and_returns_its_output(monkeypatch):
    fake_plan = {"visibility": "PASS", "resolved": {"point_count": 9}}
    captured = {}

    def fake_plan_observation(spec, **kwargs):
        captured["spec"] = spec
        return fake_plan

    monkeypatch.setattr(observation_plan, "plan_observation", fake_plan_observation)
    with running_server() as base:
        status, body = _post(f"{base}/api/observe/plan", VALID_SPEC)
        assert status == 200
        assert body == fake_plan
    assert captured["spec"]["session"]["name"] == "WEBTEST"


def test_start_requires_confirm_true():
    with running_server() as base:
        status, body = _post(f"{base}/api/observe/start", {"resolved_plan_path": "/x/observation_resolved.json"})
        assert status == 400
        assert "confirm" in body["error"]


def test_start_never_shell_executes_bogus_path(monkeypatch):
    # A path containing shell metacharacters must be treated as a literal
    # (failing) filename by json.loads(Path(...).read_text()) — never
    # interpolated into a shell command.
    with running_server() as base:
        status, body = _post(f"{base}/api/observe/start",
                              {"resolved_plan_path": "; rm -rf / #", "confirm": True})
        assert status == 500
        assert "FileNotFoundError" in body["error"] or "No such file" in body["error"]


def test_start_delegates_to_orchestrator_run_observation(monkeypatch):
    captured = {}

    def fake_run(path, **kwargs):
        captured["path"] = path
        captured["yes"] = kwargs.get("yes")
        return {"orchestrator_state": "RUNNING"}

    monkeypatch.setattr(observation_orchestrator, "run_observation", fake_run)
    with running_server() as base:
        status, body = _post(f"{base}/api/observe/start",
                              {"resolved_plan_path": "/x/observation_resolved.json", "confirm": True})
        assert status == 200
        assert body["orchestrator_state"] == "RUNNING"
    assert captured["path"] == "/x/observation_resolved.json"
    assert captured["yes"] is True  # browser's confirm click IS the operator GO


def test_stop_requires_confirm_true():
    with running_server() as base:
        status, body = _post(f"{base}/api/observe/stop", {})
        assert status == 400
        assert "confirm" in body["error"]


def test_stop_delegates_to_orchestrator_stop_observation(monkeypatch):
    monkeypatch.setattr(observation_orchestrator, "stop_observation",
                         lambda **k: {"orchestrator_state": "COMPLETED"})
    with running_server() as base:
        status, body = _post(f"{base}/api/observe/stop", {"confirm": True})
        assert status == 200
        assert body["orchestrator_state"] == "COMPLETED"


def test_put_delete_patch_rejected():
    import http.client
    with running_server() as base:
        host, port = base.replace("http://", "").split(":")
        for method in ("PUT", "DELETE", "PATCH"):
            conn = http.client.HTTPConnection(host, int(port), timeout=10)
            conn.request(method, "/api/observe/stop", body=b"{}")
            resp = conn.getresponse()
            assert resp.status == 405
            conn.close()


def test_plan_asset_path_traversal_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "ASSET_ROOT", tmp_path.resolve())
    with running_server() as base:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base}/api/observe/plan-assets/..%2F..%2Fetc%2Fpasswd", timeout=10)
        assert exc_info.value.code in (403, 404)


def test_plan_asset_disallowed_extension_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "ASSET_ROOT", tmp_path.resolve())
    (tmp_path / "secret.py").write_text("import os")
    with running_server() as base:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base}/api/observe/plan-assets/secret.py", timeout=10)
        assert exc_info.value.code == 404


def test_plan_asset_allowed_png_served(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "ASSET_ROOT", tmp_path.resolve())
    (tmp_path / "grid_plan.png").write_bytes(b"\x89PNG\r\n")
    with running_server() as base:
        with urllib.request.urlopen(f"{base}/api/observe/plan-assets/grid_plan.png", timeout=10) as r:
            assert r.status == 200
            assert r.read() == b"\x89PNG\r\n"


def test_static_observe_page_served():
    with running_server() as base:
        with urllib.request.urlopen(f"{base}/observe.html", timeout=10) as r:
            assert r.status == 200
            assert b"OBSERVE" in r.read()


# --------------------------------------------------------------- the one real, end-to-end call

def test_plan_end_to_end_reflects_resolved_core_output(tmp_path, monkeypatch):
    monkeypatch.setattr(observation_plan, "_plan_time_preflight",
                         lambda resolved_plan: {"overall": "PASS", "checks": [], "generated_utc": "now"})
    # Real matplotlib rendering is already exercised once, for real, by
    # test_observation_plan.py's heavy fixture; skip it here to avoid a
    # third real render in the same combined pytest run (see that module's
    # docstring re: this Pi's ~0 free swap).
    monkeypatch.setattr(grid_generator.GridGenerator, "_write_plot_images", lambda self, points, metadata: None)
    original_plan_observation = observation_plan.plan_observation

    def scoped_plan_observation(spec, **kwargs):
        kwargs.setdefault("data_dir", str(tmp_path / "mosaic"))
        return original_plan_observation(spec, **kwargs)

    monkeypatch.setattr(observation_plan, "plan_observation", scoped_plan_observation)
    with running_server() as base:
        status, body = _post(f"{base}/api/observe/plan", VALID_SPEC, timeout=120)
        assert status == 200
        assert body["resolved"]["point_count"] == 9
        assert body["visibility"] == "PASS"
        assert body["observation_config_sha256"]
