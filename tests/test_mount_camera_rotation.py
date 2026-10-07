"""Mount camera display rotation: persisted next to the stream URL, validated by the unified server, applied by
MONITOR to the <img> (the relayed MJPEG bytes are never touched). No camera, no network beyond 127.0.0.1."""
import contextlib
import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import almita_orchestrator_server as server_mod
import mount_camera

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "mount_camera_config.json"
    path.write_text(json.dumps({"stream_url": "http://192.168.1.164:81/stream"}))
    monkeypatch.setattr(mount_camera, "CONFIG_PATH", path)
    return path


def test_rotation_defaults_to_0_persists_and_keeps_the_url(cfg):
    relay = mount_camera.MountCameraRelay()
    assert relay.get_rotation() == 0 and relay.status()["rotation_deg"] == 0
    relay.set_rotation(180)
    saved = json.loads(cfg.read_text())
    assert saved["rotation_deg"] == 180 and saved["stream_url"] == "http://192.168.1.164:81/stream"
    assert mount_camera.MountCameraRelay().get_rotation() == 180          # survives a restart (new relay)
    with pytest.raises(ValueError):
        relay.set_rotation(45)
    cfg.write_text(json.dumps({"rotation_deg": "upside"}))
    assert relay.get_rotation() == 0                                       # anything invalid on disk reads as 0


@contextlib.contextmanager
def _server(tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    httpd = server_mod.make_server("127.0.0.1", 0, authenticator=None, public_root=public)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join()


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Origin": url.split("/mount")[0]})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_config_endpoint_sets_rotation_alone_and_rejects_bad_values(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(mount_camera, "RELAY", mount_camera.MountCameraRelay())
    with _server(tmp_path) as base:
        assert _post(base + "/mount_camera/config", {"rotation_deg": 180}) == (200, {"ok": True, "stream_url": "http://192.168.1.164:81/stream", "rotation_deg": 180})
        for bad in (45, "180", True, None):
            status, body = _post(base + "/mount_camera/config", {"rotation_deg": bad} if bad is not None else {})
            assert status == 400, bad
        with urllib.request.urlopen(base + "/mount_camera/status", timeout=10) as r:
            assert json.loads(r.read())["rotation_deg"] == 180
    assert json.loads(cfg.read_text())["rotation_deg"] == 180


def test_monitor_rotates_the_picture_by_the_configured_angle(tmp_path):
    from tests.test_web_frontend import chromium, result_of
    shutil.copytree(ROOT / "console", tmp_path / "console")
    page = (tmp_path / "console" / "index.html").read_text()
    stub = r"""<script>
const _fetch = window.fetch;
window.fetch = (url, opts) => {
  if (String(url).startsWith("/mount_camera/status")) return Promise.resolve({ ok: true, json: async () => ({ state: "STREAMING", viewers: 1, configured_url: "http://cam/stream", rotation_deg: 180 }) });
  return Promise.resolve({ ok: false, status: 404, json: async () => ({}), text: async () => "" });
};
setTimeout(() => { const i = document.getElementById("mount-camera-img");
  document.body.insertAdjacentHTML("beforeend", '<pre id="__res' + 'ult">' + JSON.stringify({ transform: i.style.transform, select: document.getElementById("mount-camera-rotation").value }) + "</pre>"); }, 2500);
</script>"""
    (tmp_path / "console" / "index.html").write_text(page.replace("</head>", stub + "</head>", 1))
    out = result_of(chromium(tmp_path / "console" / "index.html", 6000))
    assert out == {"transform": "rotate(180deg)", "select": "180"}
