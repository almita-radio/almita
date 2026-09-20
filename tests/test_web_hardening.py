"""WEB HARDENING: the :8090 API/pages and the :8088 console, exercised over real HTTP on ephemeral ports with every hardware-touching
dependency mocked (no INDI, no SDR, no capture.py, no mount). Covers health layering, error contract (status codes, request ids, no
tracebacks, NaN), input validation, path safety, conflicts/double submit, static assets, offline dependencies and shutdown."""
import contextlib
import http.client
import json
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import almita_console_server as console_server
import almita_orchestrator_server as server_mod
import almita_web_align
import almita_web_calibrate
import almita_web_system as web_system
import observation_orchestrator
import observation_plan
import serve_dashboard
from almita_web_common import JOBS, ResourceCheck, SDRResourceStatus

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "console"
NOW = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
VALID_SPEC = {
    "session": {"name": "WEBTEST"},
    "grid": {"mode": "EQUATORIAL_RECT", "placement": "FIXED_CENTER", "center_ra_hours": 6.0, "center_dec_deg": -30.0, "width_deg": 4, "height_deg": 4,
             "rows": 3, "cols": 3, "min_altitude_deg": 5, "traversal": "SERPENTINE"},
    "capture": {"seconds": 1, "settle_seconds": 0.5},
    "main": {"center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 40.2, "bias_tee": True},
    "rfi_ref": {"enabled": False, "serial": "00000002", "gain_db": 25.0},
    "quicklook": {"enabled": False, "native_grid": True, "interpolated_preview": False, "calibration_profile_path": None},
    "console": {"enabled": True}, "execution": {"unattended": True},
}


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


def http_call(base, method, path, body=None, headers=None, raw=None):
    """Returns (status, headers, body_bytes); never raises on HTTP errors."""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    hdrs = dict({"Content-Type": "application/json"} if data is not None else {}, **(headers or {}))
    req = urllib.request.Request(base + path, method=method, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def jget(base, path):
    status, headers, body = http_call(base, "GET", path)
    return status, headers, json.loads(body)


def strict_json(raw: bytes):
    """json.loads that REJECTS NaN/Infinity (a browser's res.json() does too)."""
    def bad(token):
        raise ValueError(f"non-JSON constant {token}")
    return json.loads(raw, parse_constant=bad)


_REAL_COLLECT = web_system.collect_health


class Fake:
    """Injectable collaborators for web_system.collect_health."""
    def __init__(self, ports=(8088, 1234, 7624, 8090), usb=("00000001",), status=None, orch_state="PLANNED", resource="FREE", resource_detail="no conflict",
                 orch_raises=False, jobs=None):
        self.ports, self.usb = set(ports), set(usb)
        self.status = status if status is not None else {"updated_utc": NOW.isoformat(), "acquisition": {"state": "IDLE"}, "rfi_ref": {"status": "DISABLED"}}
        self.orch_state, self.resource, self.resource_detail, self.orch_raises = orch_state, resource, resource_detail, orch_raises
        self.jobs = jobs or type("J", (), {"any_alive": staticmethod(lambda prefix=None: False)})()

    def health(self, **over):
        def orchestrator():
            if self.orch_raises:
                raise RuntimeError("runtime file corrupt")
            return {"orchestrator": {"orchestrator_state": self.orch_state, "session_id": "s1"}}
        kw = dict(now=NOW, ports=lambda: self.ports, usb=lambda: self.usb, status_reader=lambda rd: self.status, orchestrator=orchestrator,
                  resource=lambda cal: {"status": self.resource, "detail": self.resource_detail}, jobs=self.jobs)
        kw.update(over)
        return _REAL_COLLECT(**kw)


# ------------------------------------------------------------------ health: service vs dependency vs operational

def test_all_healthy_is_ready_and_layers_are_separate():
    h = Fake().health()
    assert set(h) >= {"services", "dependencies", "workflows", "operational", "version"}
    assert h["services"]["observe_api"]["state"] == "UP" and h["services"]["field_console"]["state"] == "UP" and h["services"]["console_watcher"]["state"] == "UP"
    assert h["dependencies"]["indi"]["state"] == "UP" and h["dependencies"]["main_sdr"]["state"] == "AVAILABLE"
    assert h["operational"]["level"] == "READY" and h["operational"]["ready"] is True and h["operational"]["reasons"] == []
    assert h["dependencies"]["mount"]["state"] == "NOT_EXPOSED"                       # never invented: the read-only stack cannot read the mount


def test_indi_down_service_up_but_operational_not_ready():
    h = Fake(ports=(8088, 1234, 8090)).health()
    assert h["services"]["observe_api"]["state"] == "UP"                            # service UP ...
    assert h["dependencies"]["indi"]["state"] == "DOWN"
    assert h["operational"]["level"] == "NOT_READY" and any("INDI" in r for r in h["operational"]["reasons"])   # ... operational NOT READY


def test_main_sdr_busy_down_and_unknown():
    busy = Fake(resource="CLAIMED_BY_OBSERVATION", resource_detail="orchestrator campaign is RUNNING").health()
    assert busy["dependencies"]["main_sdr"]["state"] == "BUSY" and busy["operational"]["level"] == "NOT_READY"
    down = Fake(ports=(8088, 7624, 8090)).health()
    assert down["dependencies"]["main_sdr"]["state"] == "DOWN" and down["operational"]["level"] == "NOT_READY"
    unknown = Fake(resource="UNKNOWN").health()
    assert unknown["dependencies"]["main_sdr"]["state"] == "UNKNOWN" and unknown["operational"]["level"] == "NOT_READY"
    q = Fake(resource="CLAIMED_BY_QUICKLOOK").health()
    assert q["dependencies"]["main_sdr"]["state"] == "BUSY" and "CLAIMED_BY_QUICKLOOK" in q["dependencies"]["main_sdr"]["detail"]


def test_no_observation_running_and_partial_observation_states():
    assert Fake(orch_state="PLANNED").health()["workflows"]["observation"]["state"] == "PLANNED"
    running = Fake(orch_state="RUNNING", status={"updated_utc": NOW.isoformat(), "acquisition": {"state": "RUNNING"}, "rfi_ref": {"status": "RUNNING"}}).health()
    assert running["workflows"]["observation"]["state"] == "RUNNING" and running["workflows"]["observation"]["detail"] == "acquisition: RUNNING"
    assert running["operational"]["level"] == "NOT_READY" and any("RUNNING" in r for r in running["operational"]["reasons"])
    partial = Fake(orch_state="ABORTED").health()                                   # a stopped (partial) campaign does not make the instrument unusable
    assert partial["workflows"]["observation"]["state"] == "ABORTED" and partial["operational"]["level"] == "READY"
    failed = Fake(orch_state="FAILED").health()
    assert failed["workflows"]["observation"]["state"] == "FAILED"


def test_unreadable_runtime_state_degrades_that_row_only():
    h = Fake(orch_raises=True).health()
    assert h["workflows"]["observation"]["state"] == "UNKNOWN" and "unreadable" in h["workflows"]["observation"]["detail"]
    assert h["dependencies"]["indi"]["state"] == "UP"                                # the rest of the view survives


def test_watcher_and_console_down_only_degrade():
    stale = Fake(status={"updated_utc": (NOW - timedelta(minutes=5)).isoformat(), "acquisition": {"state": "IDLE"}}).health()
    assert stale["services"]["console_watcher"]["state"] == "STALE" and stale["operational"]["level"] == "DEGRADED" and stale["operational"]["degraded_by"]
    missing = Fake(status={}).health()
    assert missing["services"]["console_watcher"]["state"] == "DOWN"
    no_console = Fake(ports=(1234, 7624, 8090)).health()
    assert no_console["services"]["field_console"]["state"] == "DOWN" and no_console["operational"]["level"] == "DEGRADED" and no_console["operational"]["ready"] is False


def test_rfi_sdr_is_optional_and_never_blocks_readiness():
    absent = Fake(usb=("00000001",)).health()
    assert absent["dependencies"]["rfi_sdr"]["state"] == "DOWN" and absent["dependencies"]["rfi_sdr"]["optional"] is True and absent["operational"]["level"] == "READY"
    present = Fake(usb=("00000001", "00000002")).health()
    assert present["dependencies"]["rfi_sdr"]["state"] == "AVAILABLE"
    failed = Fake(status={"updated_utc": NOW.isoformat(), "acquisition": {"state": "RUNNING"}, "rfi_ref": {"status": "FAILED"}}).health()
    assert failed["dependencies"]["rfi_sdr"]["state"] == "DOWN"


def test_calibration_and_alignment_jobs_are_reported_from_the_job_table():
    class Jobs:
        @staticmethod
        def any_alive(prefix=None):
            return prefix in ("CAL-", "HI-")
    h = Fake(jobs=Jobs()).health()
    assert h["workflows"]["calibration"]["state"] == "RUNNING" and h["workflows"]["alignment"]["state"] == "RUNNING"


def test_health_and_version_never_leak_secrets_or_paths():
    text = json.dumps(Fake().health())
    assert "@" not in text and "password" not in text.lower() and "token" not in text.lower() and str(ROOT) not in text
    v = web_system.version_info()
    assert v["author"] == "Felipe Fridman" and v["project_url"] == "https://github.com/almita-radio/almita" and "HTTP" in v["transport"]
    assert re.fullmatch(r"[0-9a-f]{4,40}|unknown", v["git_short_sha"])


def test_health_endpoint_over_http_is_read_only_and_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(web_system, "collect_health", lambda **k: calls.append(1) or Fake().health())
    web_system._cache.update(t=0.0, value=None)
    with running_server() as base:
        for _ in range(3):
            status, headers, body = jget(base, "/api/system/health")
            assert status == 200 and body["data"]["operational"]["level"] == "READY" and headers["Cache-Control"] == "no-store"
    assert len(calls) == 1                                                            # 3 open pages, 1 collection
    web_system._cache.update(t=0.0, value=None)


def test_liveness_is_not_dependency_health():
    with running_server() as base:
        status, _, body = jget(base, "/healthz")
        assert status == 200 and body["status"] == "ALIVE" and "dependencies" not in json.dumps(body["data"])
        assert http_call(base, "HEAD", "/healthz")[2] == b""                        # HEAD: headers, no body


# ------------------------------------------------------------------ error contract

def http_client(base):
    host, port = base.replace("http://", "").split(":")
    return http.client.HTTPConnection(host, int(port), timeout=20)


def test_bad_json_bodies_are_400_with_request_id_and_no_traceback():
    with running_server() as base:
        for raw in (b"{not json", b"[1,2]", b"", b'"str"'):
            status, headers, body = http_call(base, "POST", "/api/observe/plan", raw=raw)
            assert status == 400, raw
            data = json.loads(body)
            assert data["ok"] is False and data["error"] and headers["X-Request-Id"] and "Traceback" not in body.decode()
        conn = http_client(base)
        conn.request("POST", "/api/observe/plan", body=b"{}", headers={"Content-Length": "abc", "Content-Type": "application/json"})
        assert conn.getresponse().status == 400
        conn.close()
        big = http_call(base, "POST", "/api/observe/plan", raw=b"{" + b" " * (server_mod.MAX_BODY_BYTES + 10) + b"}")
        assert big[0] == 400


def test_unknown_api_route_is_json_404_and_methods_are_405():
    with running_server() as base:
        status, headers, body = http_call(base, "GET", "/api/nope")
        assert status == 404 and json.loads(body)["ok"] is False and headers["Content-Type"].startswith("application/json")
        status, headers, body = http_call(base, "PUT", "/api/observe/plan", body={})
        assert status == 405 and headers["Allow"] == "GET, HEAD, POST" and json.loads(body)["error"]


def test_unexpected_exception_is_500_with_request_id_and_hides_the_traceback(monkeypatch):
    def boom(**k):
        raise RuntimeError("secret internal detail /home/x/y.py line 12")
    monkeypatch.setattr(observation_orchestrator, "get_status", boom)
    with running_server() as base:
        status, headers, body = http_call(base, "GET", "/api/observe/status")
    text = body.decode()
    data = json.loads(text)
    assert status == 500 and data["request_id"] == headers["X-Request-Id"] and data["request_id"] in data["error"]
    assert "Traceback" not in text and "/home/x" not in text and "secret internal detail" not in text and "RuntimeError" in data["error"]


def test_dependency_oserror_is_503_not_500(monkeypatch):
    def gone(**k):
        raise FileNotFoundError("data/runtime missing")
    monkeypatch.setattr(observation_orchestrator, "get_status", gone)
    with running_server() as base:
        status, _, body = http_call(base, "GET", "/api/observe/status")
    assert status == 503 and "dependency unavailable" in json.loads(body)["error"]


def test_orchestrator_conflict_is_409_and_validation_is_400(monkeypatch):
    def refuse(*a, **k):
        raise observation_orchestrator.OrchestratorError("an observation is already RUNNING (session_id=s1)")
    monkeypatch.setattr(observation_orchestrator, "run_observation", refuse)
    plan = str(server_mod.ASSET_ROOT / "X-20260101-00:00:00" / "observation_resolved.json")
    with running_server() as base:
        status, _, body = http_call(base, "POST", "/api/observe/start", body={"resolved_plan_path": plan, "confirm": True})
        assert status == 409 and "already RUNNING" in json.loads(body)["error"]
        status, _, body = http_call(base, "POST", "/api/observe/plan", body={"session": {"name": "x"}})
        assert status == 400 and "observation spec" in json.loads(body)["error"]


def test_nan_and_infinity_never_reach_the_browser(monkeypatch):
    monkeypatch.setattr(observation_orchestrator, "get_status", lambda **k: {"orchestrator": {"orchestrator_state": "RUNNING", "x": float("nan"), "y": float("inf"), "z": [float("-inf"), 1.5]}})
    with running_server() as base:
        status, _, body = http_call(base, "GET", "/api/observe/status")
    data = strict_json(body)                                                          # strict parse: NaN/Infinity would raise
    assert status == 200 and data["orchestrator"]["x"] is None and data["orchestrator"]["y"] is None and data["orchestrator"]["z"] == [None, 1.5]


def test_json_responses_are_not_cacheable_and_carry_no_cors():
    with running_server() as base:
        for path in ("/api/system/version", "/healthz", "/api/nope"):
            _, headers, _ = http_call(base, "GET", path)
            assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
            assert not [h for h in headers if h.lower().startswith("access-control")]


# ------------------------------------------------------------------ input validation and path safety

@pytest.mark.parametrize("path", ["/etc/passwd", "../../etc/passwd", "; rm -rf / #", "", "data/mosaic/X/other.json", "data/mosaic/../../etc/observation_resolved.json",
                                  "/tmp/observation_resolved.json", "data/mosaic/X/observation_resolved.json\x00.png", 123, None, ["a"]])
def test_start_only_accepts_plans_inside_data_mosaic(path, monkeypatch):
    called = []
    monkeypatch.setattr(observation_orchestrator, "run_observation", lambda *a, **k: called.append(a) or {})
    with running_server() as base:
        status, _, body = http_call(base, "POST", "/api/observe/start", body={"resolved_plan_path": path, "confirm": True})
    assert status == 400 and json.loads(body)["error"] and not called


def test_start_missing_plan_file_is_404(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("gone")
    monkeypatch.setattr(observation_orchestrator, "run_observation", missing)
    plan = str(server_mod.ASSET_ROOT / "X-20260101-00:00:00" / "observation_resolved.json")
    with running_server() as base:
        status, _, body = http_call(base, "POST", "/api/observe/start", body={"resolved_plan_path": plan, "confirm": True})
    assert status == 404 and "plan again" in json.loads(body)["error"]


@pytest.mark.parametrize("rel", ["..%2F..%2Fetc%2Fpasswd", "%2e%2e/%2e%2e/etc/passwd", "X/../../../etc/passwd", "X%00.png", "X/grid_plan.exe", "nonexistent/grid_plan.png"])
def test_plan_assets_reject_traversal_nul_and_other_suffixes(rel):
    with running_server() as base:
        status, _, body = http_call(base, "GET", f"/api/observe/plan-assets/{rel}")
    assert status in (400, 403, 404) and b"root:" not in body


def test_query_strings_do_not_break_static_or_api_routes():
    with running_server() as base:
        assert http_call(base, "GET", "/observe.html?v=abc123")[0] == 200
        assert http_call(base, "GET", "/common.js?v=1")[0] == 200
        assert http_call(base, "GET", "/api/system/version?x=1")[0] == 200


def test_session_ids_with_traversal_are_rejected_over_http():
    with running_server() as base:
        for sid in ("..", "%2e%2e", "a%2Fb", "x" * 300):
            status, _, body = http_call(base, "GET", f"/api/align/session/{sid}")
            assert status in (200, 400, 404)
            data = json.loads(body)
            assert data.get("blocked") is True or data.get("ok") is False


# ------------------------------------------------------------------ conflicts, double submit

def test_second_plan_while_planning_is_409(monkeypatch):
    gate, entered = threading.Event(), threading.Event()

    def slow_plan(spec, **k):
        entered.set()
        gate.wait(10)
        return {"observation_name": "X", "resolved": {}, "_resolved_plan_path": "x"}
    monkeypatch.setattr(observation_plan, "plan_observation", slow_plan)
    results = []
    with running_server() as base:
        t = threading.Thread(target=lambda: results.append(http_call(base, "POST", "/api/observe/plan", body=VALID_SPEC)[0]))
        t.start()
        assert entered.wait(10)
        status, _, body = http_call(base, "POST", "/api/observe/plan", body=VALID_SPEC)
        gate.set()
        t.join(10)
    assert status == 409 and "already in progress" in json.loads(body)["error"] and results == [200]


def test_align_run_twice_for_the_same_session_starts_one_job():
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(10), daemon=True)
    thread.start()
    JOBS.register("HI-DOUBLE-SUBMIT", thread)
    try:
        res = almita_web_align.run_simulation("hi", {"session_id": "HI-DOUBLE-SUBMIT"})
        assert res["blocked"] is True and "already in progress" in res["reason"]
    finally:
        release.set()
        thread.join()


def test_calibrate_run_is_refused_while_another_is_starting_or_running():
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(10), daemon=True)
    thread.start()
    JOBS.register("CAL-DOUBLE-SUBMIT", thread)
    try:
        res = almita_web_calibrate.run_simulation({"scenario": "HEALTHY"})
        assert res["blocked"] is True and "already" in res["reason"]
    finally:
        release.set()
        thread.join()
    assert almita_web_calibrate._RUN_LAUNCH_LOCK.acquire(blocking=False)              # lock held by "a run starting"
    try:
        assert almita_web_calibrate.run_simulation({"scenario": "HEALTHY"})["blocked"] is True
    finally:
        almita_web_calibrate._RUN_LAUNCH_LOCK.release()


def test_calibrate_status_does_not_probe_rtl_tcp_while_an_observation_owns_it(monkeypatch):
    import calibration_engine.hardware_inspection as hi
    probes = []
    monkeypatch.setattr(almita_web_calibrate, "get_sdr_resource_status", lambda *a, **k: ResourceCheck(SDRResourceStatus.CLAIMED_BY_OBSERVATION, "campaign RUNNING"))
    monkeypatch.setattr(hi, "probe_rtl_tcp_handshake", lambda *a, **k: probes.append(1))
    res = almita_web_calibrate.get_status()
    assert not probes and res["data"]["resource"]["status"] == "CLAIMED_BY_OBSERVATION"
    monkeypatch.setattr(almita_web_calibrate, "get_sdr_resource_status", lambda *a, **k: ResourceCheck(SDRResourceStatus.FREE, "free"))
    monkeypatch.setattr(hi, "probe_rtl_tcp_handshake", lambda *a, **k: (probes.append(1), hi.RtlTcpHandshakeInfo(False, None, None, None, "MOCK", "mock"))[1])
    almita_web_calibrate.get_status()
    assert probes == [1]


# ------------------------------------------------------------------ static assets, offline, security lint

LOCAL_REF = re.compile(r'(?:src|href)="([^"#]+)"')


def _console_public(tmp_path):
    public = console_server.prepare_console_web(CONSOLE, tmp_path / "runtime", tmp_path / "public")
    console_server.write_version_json(public)
    return public


@contextlib.contextmanager
def running_console(public):
    server = serve_dashboard.make_server(public, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_every_local_reference_of_every_8090_page_resolves():
    with running_server() as base:
        for page in ("observe.html", "align.html", "calibrate.html", "status.html"):
            status, _, html = http_call(base, "GET", "/" + page)
            assert status == 200 and b"<title>ALMITA" in html                          # every page has a title
            for ref in LOCAL_REF.findall(html.decode()):
                if ref.startswith(("http://", "https://", "mailto:", "data:")):
                    continue
                target = "/" + ref.lstrip("/").split("?")[0]
                assert http_call(base, "GET", target)[0] == 200, (page, ref)
        for asset in ("/observe.js", "/align.js", "/calibrate.js", "/status.js", "/common.js", "/styles.css"):
            assert http_call(base, "GET", asset)[0] == 200


def test_console_8088_serves_all_assets_and_version(tmp_path):
    public = _console_public(tmp_path)
    with running_console(public) as base:
        status, _, html = http_call(base, "GET", "/")
        assert status == 200 and b"<title>ALMITA Field Console" in html and b'src="common.js?v=' in html
        for ref in LOCAL_REF.findall(html.decode()):
            if ref.startswith(("http://", "https://", "#", "data:")):
                continue
            path = "/" + ref.lstrip("/").split("?")[0]
            if path in ("/", "/runtime"):
                continue
            assert http_call(base, "GET", path)[0] == 200, ref
        status, headers, body = http_call(base, "GET", "/version.json")
        info = json.loads(body)
        assert status == 200 and info["component"] == "field_console" and "@" not in body.decode()
        assert headers["X-Frame-Options"] == "SAMEORIGIN" and headers["Referrer-Policy"] == "same-origin"
        assert http_call(base, "POST", "/", body={})[0] == 405                          # read-only server
        assert http_call(base, "GET", "/nope.html")[0] == 404
        assert http_call(base, "GET", "/runtime/../../etc/passwd")[0] in (400, 403, 404)


def test_no_external_or_cdn_dependency_in_any_page_script_or_style():
    files = [p for p in CONSOLE.glob("*") if p.suffix in (".html", ".js", ".css")]
    assert files
    for f in files:
        text = f.read_text()
        assert not re.search(r"<script[^>]+src=[\"']https?://", text) and not re.search(r"<link[^>]+href=[\"']https?://", text), f.name
        assert "@import" not in text and not re.search(r"url\(\s*[\"']?https?://", text), f.name
        assert not re.search(r"fetch\(\s*[\"'`]https?://", text) and "googleapis" not in text and "cdn." not in text.lower(), f.name
        assert "WebSocket(" not in text and "EventSource(" not in text, f.name
    # the only absolute URL in first-party code is the project link the user may click (never fetched)
    urls = set()
    for f in files:
        urls |= set(re.findall(r"https?://[A-Za-z0-9./_-]+", f.read_text()))
    assert urls <= {"https://github.com/almita-radio/almita"}, urls


def test_no_shell_eval_or_unsafe_dom_patterns_in_the_web_layer():
    py = ["almita_orchestrator_server.py", "almita_web_align.py", "almita_web_calibrate.py", "almita_web_common.py", "almita_web_system.py",
          "almita_console_server.py", "serve_dashboard.py"]
    for name in py:
        text = (ROOT / name).read_text()
        assert "shell=True" not in text and "os.system(" not in text and not re.search(r"\beval\(|\bexec\(", text), name
    for f in ("common.js", "observe.js", "align.js", "calibrate.js", "status.js"):
        text = (CONSOLE / f).read_text()
        assert "innerHTML" not in text and "insertAdjacentHTML" not in text and "document.write" not in text and "eval(" not in text and "new Function" not in text, f
    lines = (CONSOLE / "app.js").read_text().splitlines()
    for i, line in enumerate(lines):
        if "innerHTML" in line and not line.lstrip().startswith("//"):
            block = line
            j = i
            def open_count(text):
                return sum(text.count(c) for c in "([{") - sum(text.count(c) for c in ")]}")
            while (open_count(block) > 0 or not block.rstrip().endswith(";")) and j + 1 < len(lines) and j - i < 30:   # the whole (balanced) statement
                j += 1
                block += lines[j]
            assert any(tok in block for tok in ("pair(", "esc(", "_escapeHtml", '=""')), block.strip()[:120]     # every dynamic innerHTML goes through an escaper


def test_button_semantics_forms_have_labels_and_action_buttons_are_buttons():
    for page in ("observe", "align", "calibrate", "status"):
        html = (CONSOLE / f"{page}.html").read_text()
        assert not re.search(r"<div[^>]+onclick", html) and not re.search(r"<a[^>]+onclick", html)
        for m in re.finditer(r"<button(?![^>]*type=)[^>]*>", html):
            if 'id="btn-plan"' in m.group(0):
                continue                                                              # the OBSERVE form's own submit button
            pytest.fail(f"{page}: button without explicit type: {m.group(0)}")
        for m in re.finditer(r'<input[^>]+id="([^"]+)"[^>]*>', html):
            iid = m.group(1)
            if 'type="checkbox"' in m.group(0):
                continue
            assert f'for="{iid}"' in html or "aria-label" in m.group(0), (page, iid)


# ------------------------------------------------------------------ startup, port conflict, shutdown

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_port_conflict_gives_a_clear_error_and_kills_nothing(monkeypatch, capsys):
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    tmp_public, tmp_rt = ROOT / "data" / "console_web_test_tmp", ROOT / "data" / "console_web_test_tmp_rt"
    try:
        monkeypatch.setattr(sys, "argv", ["almita_orchestrator_server.py", "--host", "127.0.0.1", "--port", str(port)])
        assert server_mod.main() == 2
        assert "port already in use" in capsys.readouterr().out
        monkeypatch.setattr(sys, "argv", ["almita_console_server.py", "--bind", "127.0.0.1", "--port", str(port), "--public-root", str(tmp_public), "--runtime-dir", str(tmp_rt)])
        assert console_server.main() == 2
        assert "port already in use" in capsys.readouterr().out
    finally:
        holder.close()
        shutil.rmtree(tmp_public, ignore_errors=True)
        shutil.rmtree(tmp_rt, ignore_errors=True)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_orchestrator_server_shuts_down_cleanly_on_signal(sig):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "almita_orchestrator_server.py"), "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                    assert r.status == 200
                    break
            except OSError:
                time.sleep(0.3)
        else:
            pytest.fail("server did not come up")
        proc.send_signal(sig)
        out, _ = proc.communicate(timeout=20)
        assert proc.returncode == 0 and "ALMITA ORCHESTRATOR API STOP" in out
    finally:
        if proc.poll() is None:
            proc.kill()


def test_server_starts_without_any_hardware_and_reports_dependencies_down():
    """Nothing listening on 1234/7624: the app answers and says so (no crash loop)."""
    h = Fake(ports=(8090,), usb=()).health()
    assert h["services"]["observe_api"]["state"] == "UP" and h["dependencies"]["indi"]["state"] == "DOWN" and h["dependencies"]["main_sdr"]["state"] == "DOWN"
    assert h["operational"]["level"] == "NOT_READY"


# ------------------------------------------------------------------ systemd (read-only audit of the unit files)

def test_systemd_units_are_consistent_with_the_documented_ports_and_paths():
    units = {u.name: u.read_text() for u in (ROOT / "systemd").glob("almita-*.service")}
    web = {"almita-console-web.service": ("almita_console_server.py", "8088"), "almita-observe-api.service": ("almita_orchestrator_server.py", "8090")}
    for name, (script, port) in web.items():
        text = units[name]
        assert "WorkingDirectory=/home/stellarmate/almita" in text and f"/home/stellarmate/almita/{script}" in text and port in text
        assert "Restart=on-failure" in text and "/.venv/bin/python" in text
    assert "KillMode=process" in units["almita-observe-api.service"]                 # the observation must outlive an API restart
    for text in units.values():
        assert "sh -c" not in text.split("ExecStart=")[1].split("\n")[0]


def test_successful_polls_are_not_logged_but_failures_and_actions_are(capsys):
    with running_server() as base:
        http_call(base, "GET", "/healthz")
        http_call(base, "GET", "/api/system/health")
        http_call(base, "GET", "/api/nope")
        http_call(base, "POST", "/api/observe/plan", raw=b"{bad")
    out = capsys.readouterr().out
    assert "/healthz" not in out and "/api/system/health" not in out
    assert "/api/nope" in out and "404" in out and "POST /api/observe/plan" in out and "400" in out
    line = next(l for l in out.splitlines() if "/api/nope" in l)
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00 INFO  almita_orchestrator_server req=[0-9a-f]{8} ", line)      # UTC timestamp, level, component, request id
