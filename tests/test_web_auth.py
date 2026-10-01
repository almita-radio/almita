"""Single web port + single-user Basic auth: closed without valid credentials, every surface (pages, API, result/runtime files,
MJPEG stream, control actions) behind the same gate, cross-origin writes refused, STOP still reachable when authorized.
No hardware, no capture: the orchestrator/ops stop calls and the camera relay are replaced by fakes."""
import base64
import contextlib
import http.client
import json
import os
import threading
from pathlib import Path

import pytest

import almita_console_server
import almita_orchestrator_server as server_mod
import almita_web_auth
import mount_camera

ROOT = Path(__file__).resolve().parents[1]
USER, PASSWORD = "operator", "correct horse battery"


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@contextlib.contextmanager
def running(tmp_path, auth_file):
    public = almita_console_server.prepare_console_web(ROOT / "console", tmp_path / "runtime", tmp_path / "public")
    (tmp_path / "runtime" / "almita_status.json").write_text("{}")
    httpd = server_mod.make_server("127.0.0.1", 0, authenticator=almita_web_auth.Authenticator(auth_file), public_root=public)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def call(port, method, path, auth=None, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    hdrs = dict(headers or {})
    if auth:
        hdrs["Authorization"] = auth
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=hdrs)
    r = conn.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    conn.close()
    return out


SURFACES = ["/", "/observe.html", "/common.js", "/api/system/version", "/api/observe/status", "/runtime/almita_status.json",
            "/api/ops/file?path=data/runtime/almita_status.json", "/mount_camera/status", "/mount_camera/stream", "/version.json"]


@pytest.fixture
def auth_file(tmp_path):
    path = tmp_path / "cfg" / "web_auth.json"
    almita_web_auth.write_credentials(path, USER, PASSWORD)
    return path


def test_closed_without_valid_configuration(tmp_path):
    missing = tmp_path / "nope.json"
    with running(tmp_path, missing) as port:
        for path in SURFACES:
            assert call(port, "GET", path, auth=_basic(USER, PASSWORD))[0] == 503, path
        assert call(port, "POST", "/api/observe/stop", auth=_basic(USER, PASSWORD), body={"confirm": True})[0] == 503


def test_group_readable_credentials_file_is_refused(tmp_path, auth_file):
    os.chmod(auth_file, 0o644)
    assert not almita_web_auth.Authenticator(auth_file).configured()


def test_hash_only_on_disk(auth_file):
    text = auth_file.read_text()
    assert PASSWORD not in text and oct(auth_file.stat().st_mode & 0o777) == "0o600"


def test_every_surface_requires_credentials(tmp_path, auth_file):
    with running(tmp_path, auth_file) as port:
        for path in SURFACES:
            status, headers, _ = call(port, "GET", path)
            assert status == 401 and headers["WWW-Authenticate"].startswith('Basic realm="ALMITA"'), path
            assert call(port, "GET", path, auth=_basic(USER, "wrong password"))[0] == 401, path
            assert call(port, "GET", path, auth=_basic("someone", PASSWORD))[0] == 401, path
        for method, path in (("POST", "/api/observe/stop"), ("POST", "/api/ops/stop/abcdef12"), ("POST", "/mount_camera/config"), ("PUT", "/api/x")):
            assert call(port, method, path, body={"confirm": True})[0] == 401, path


def test_authorized_console_api_and_runtime_on_one_port(tmp_path, auth_file):
    ok = _basic(USER, PASSWORD)
    with running(tmp_path, auth_file) as port:
        status, _, html = call(port, "GET", "/", auth=ok)
        assert status == 200 and b"ALMITA Field Console" in html and b":8090" not in html
        assert call(port, "GET", "/observe.html", auth=ok)[0] == 200
        status, _, body = call(port, "GET", "/api/system/version", auth=ok)
        assert status == 200 and json.loads(body)["ok"] is True
        status, headers, body = call(port, "GET", "/runtime/almita_status.json", auth=ok)
        assert status == 200 and body == b"{}" and "no-cache" in headers["Cache-Control"]
        assert call(port, "GET", "/api/nope", auth=ok)[0] == 404


def test_mjpeg_stream_flows_through_the_single_authenticated_port(tmp_path, auth_file, monkeypatch):
    class Sub:
        def __init__(self):
            self.frames = [b"--frame\r\nContent-Type: image/jpeg\r\n\r\nJPEGDATA\r\n", None]

        def read(self, timeout=1.0):
            return self.frames.pop(0)

    class Relay:
        content_type = "multipart/x-mixed-replace; boundary=frame"
        def subscribe(self): return Sub()
        def unsubscribe(self, sub): pass
        def wait_connected_or_failed(self, timeout): pass
        def status(self): return {"state": "STREAMING", "last_error": None}

    monkeypatch.setattr(mount_camera, "RELAY", Relay())
    with running(tmp_path, auth_file) as port:
        status, headers, body = call(port, "GET", "/mount_camera/stream", auth=_basic(USER, PASSWORD))
    assert status == 200 and headers["Content-Type"].startswith("multipart/x-mixed-replace") and b"JPEGDATA" in body


def test_abort_paths_reachable_when_authorized_and_cross_origin_refused(tmp_path, auth_file, monkeypatch):
    calls = []
    monkeypatch.setattr(server_mod.observation_orchestrator, "stop_observation", lambda confirm: calls.append(("observe", confirm)) or {"orchestrator_state": "ABORTED"})
    monkeypatch.setattr(server_mod.almita_web_ops, "stop", lambda job_id: calls.append(("ops", job_id)) or {"state": "STOPPING"})
    ok = _basic(USER, PASSWORD)
    with running(tmp_path, auth_file) as port:
        same = {"Origin": f"http://127.0.0.1:{port}"}
        evil = {"Origin": "http://evil.example"}
        assert call(port, "POST", "/api/observe/stop", auth=ok, body={"confirm": True}, headers=evil)[0] == 403
        assert call(port, "POST", "/api/ops/stop/abcdef12", auth=ok, body={"confirm": True}, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert calls == []
        status, _, body = call(port, "POST", "/api/observe/stop", auth=ok, body={"confirm": True}, headers=same)
        assert status == 200 and json.loads(body)["orchestrator_state"] == "ABORTED"
        status, _, body = call(port, "POST", "/api/ops/stop/abcdef12", auth=ok, body={"confirm": True}, headers=same)
        assert status == 200 and json.loads(body)["data"]["state"] == "STOPPING"
    assert calls == [("observe", True), ("ops", "abcdef12")]
