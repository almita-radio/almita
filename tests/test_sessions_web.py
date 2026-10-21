"""SESSIONS on the single web server: routes, status codes, same-origin writes, export download and the page wiring.
The catalog is pointed at a fake data/ tree in tmp_path; nothing reaches hardware or the real data/."""
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import almita_orchestrator_server as server_mod
import almita_web_sessions
import session_catalog as sc
from tests import session_fixtures as fx
from tests.test_orchestrator_web import _get, _post, running_server

CONSOLE = Path(__file__).resolve().parent.parent / "console"


@pytest.fixture()
def web(tmp_path, monkeypatch):
    paths = fx.build(tmp_path)
    monkeypatch.setattr(almita_web_sessions, "ROOTS", sc.Roots.under(tmp_path))
    with running_server() as base:
        yield base, tmp_path, paths


def _status(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_read_routes_answer_from_the_stored_files(web):
    base, root, p = web
    before = fx.snapshot(root)
    st, body = _get(base + "/api/sessions/list")
    assert st == 200 and body["data"]["total"] == 6
    q = urllib.parse.quote(fx.A)
    st, body = _get(base + f"/api/sessions/detail?id={q}")
    assert st == 200 and body["data"]["capture"]["state"] == "COMPLETED"
    for route in (f"points?id={q}", f"point?id={q}&point=1", f"temperatures?id={q}", f"files?id={q}", "pending", f"exports?id={q}",
                  f"compare?a={q}&b={urllib.parse.quote(fx.B)}", f"compare_runs?id={q}&a={p['r1'].name}&b={p['r2'].name}"):
        assert _get(base + "/api/sessions/" + route)[0] == 200, route
    assert _status(base + "/api/sessions/detail?id=NOPE-20260101-00:00:00")[0] == 404
    assert _status(base + "/api/sessions/detail?id=..%2F..%2Fetc")[0] == 400
    assert _status(base + f"/api/sessions/files?id={q}&dir=..%2F..")[0] == 400
    assert _status(base + "/api/sessions/list?page=x")[0] == 400
    assert fx.snapshot(root) == before


def test_writes_are_same_origin_only_and_touch_only_the_sessions_store(web):
    base, root, _ = web
    before = fx.snapshot(root)
    st, body = _post(base + "/api/sessions/annotate", {"id": fx.A, "patch": {"name": "renamed", "tags": ["x"]}})
    assert st == 200 and body["data"]["annotation"]["name"] == "renamed"
    assert _post(base + "/api/sessions/annotate", {"id": fx.A, "patch": {"discarded": {"reason": ""}}})[0] == 400
    cross = _status(base + "/api/sessions/annotate", data=json.dumps({"id": fx.A, "patch": {"name": "evil"}}).encode(),
                    headers={"Content-Type": "application/json", "Origin": "http://evil.example"}, method="POST")
    assert cross[0] == 403
    assert _post(base + "/api/sessions/reindex", {})[0] == 200
    st, body = _get(base + "/api/sessions/list?q=renamed")
    assert [s["campaign_id"] for s in body["data"]["sessions"]] == [fx.A]
    assert _post(base + "/api/sessions/export", {"id": fx.A, "include_iq": True})[0] == 400
    st, body = _post(base + "/api/sessions/export", {"id": fx.A})
    assert st == 200
    zrel = body["data"]["zip"]
    st, headers, data = _status(base + "/api/sessions/export_file?path=" + urllib.parse.quote(zrel))
    assert st == 200 and headers["Content-Type"] == "application/zip" and data[:2] == b"PK" and "attachment" in headers["Content-Disposition"]
    assert _status(base + "/api/sessions/export_file?path=" + urllib.parse.quote(f"data/mosaic/{fx.A}/mosaic.csv"))[0] == 404
    assert fx.snapshot(root) == before


def test_the_page_is_served_and_in_the_shared_navigation(web):
    base, _, _ = web
    st, headers, html = _status(base + "/sessions.html")
    assert st == 200 and b"<title>ALMITA Sessions" in html
    assert _status(base + "/sessions.js")[0] == 200
    assert '{ key: "SESSIONS", href: "/sessions.html" }' in (CONSOLE / "common.js").read_text()
    assert "/sessions.html" in server_mod.STATIC_FILES
