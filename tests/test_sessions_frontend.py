"""SESSIONS page in headless Chromium with a stubbed fetch fed by the real catalog functions on the fake data/ tree:
the catalog renders, a session opens on every tab, the stored 3D stack never asks for MONITOR's live artifact, backend
strings stay text, and the only writes the page sends are the operator's own SAVE / EXPORT."""
import json
import shutil
from pathlib import Path

import session_catalog as sc
from tests import session_fixtures as fx
from tests.test_web_frontend import run_page

CONSOLE = Path(__file__).resolve().parent.parent / "console"
HOSTILE = "<img src=x onerror=window.__pwned=1>"


def _routes(tmp_path):
    data = tmp_path / "datatree"
    fx.build(data)
    roots = sc.Roots.under(data)
    sc.build_index(roots)
    sc.annotate(fx.A, {"name": HOSTILE, "notes": "<script>window.__pwned=2</script>", "tags": ["V3"]}, roots)
    listing = sc.list_sessions({}, roots)
    detail = sc.session_detail(fx.A, roots)
    stack_doc = json.loads((data / detail["sections"]["quicklook"]["stack3d"]["path"]).read_text())
    payload = {
        "GET /api/sessions/list": listing, "GET /api/sessions/detail": detail, "GET /api/sessions/points": sc.session_points(fx.A, roots),
        "GET /api/sessions/point": sc.point_detail(fx.A, "1", roots), "GET /api/sessions/temperatures": sc.session_temperatures(fx.A, roots),
        "GET /api/sessions/files": sc.session_files(fx.A, "", roots), "GET /api/sessions/exports": {"exports": []},
        "GET /api/ops/file": stack_doc,
    }
    js = "".join(f"window.__routes[{json.dumps(k)}] = {{ body: {{ ok: true, data: {json.dumps(v)} }} }};\n" for k, v in payload.items())
    js += 'window.__routes["GET /api/ops/file"] = { body: ' + json.dumps(stack_doc) + " };\n"
    js += 'window.__routes["POST /api/sessions/annotate"] = (b) => ({ body: { ok: true, data: { annotation: b.patch } } });\n'
    return js.replace("</", "<\\/")                  # data inside an inline <script>: a literal </script> would end it


DRIVER = r"""
await until(() => document.querySelectorAll("#tbl-sessions .ses-open").length > 0, 6000);
out.rows = document.querySelectorAll("#tbl-sessions tr").length;
const openA = document.querySelector(`#tbl-sessions button[aria-label="open ${window.__A}"]`);
out.firstName = openA.closest("tr").querySelector(".ses-name").textContent;
openA.click();
await until(() => !$("session-panel").hidden && $("summary-cards").children.length > 0, 6000);
out.badge = $("session-badge").textContent;
out.tabs = [...document.querySelectorAll("#session-tabs button")].map((b) => b.textContent);
const seen = {};
for (const b of document.querySelectorAll("#session-tabs button")) {
  b.click(); await sleep(150);
  const sec = [...document.querySelectorAll(".ses-section")].find((s) => !s.hidden);
  seen[b.textContent] = sec ? sec.textContent.length : 0;
}
out.seen = seen;
document.querySelector('#session-tabs button[data-tab="CAPTURES / QUICKLOOK"]').click();
await until(() => document.querySelectorAll("#tbl-points tr").length > 0, 4000);
out.points = document.querySelectorAll("#tbl-points tr").length;
$("btn-stack").click(); await sleep(400);
out.stackBadge = $("ses-stack-badge").textContent;
document.querySelector("#tbl-points tr").click();
await until(() => !$("point-detail").hidden && $("point-kv").children.length > 0, 4000);
out.pointTitle = $("point-title").textContent;
document.querySelector('#session-tabs button[data-tab="ORGANIZE"]').click(); await sleep(100);
$("a-tags").value = "V3, night"; $("btn-save").click(); await sleep(400);
out.calls = window.__calls.map((c) => c.key);
out.annotateBody = (window.__calls.find((c) => c.key === "POST /api/sessions/annotate") || {}).body;
out.pwned = window.__pwned || null;
out.nameHtml = document.querySelector("#h-session").innerHTML;
"""


def test_sessions_page_catalog_viewer_and_isolation_from_the_live_session(tmp_path):
    for name in ("spectral_stack_3d.js",):
        shutil.copyfile(CONSOLE / name, tmp_path / name)
    shutil.copytree(CONSOLE / "vendor", tmp_path / "vendor")
    out = run_page(tmp_path, "sessions", _routes(tmp_path) + f"window.__A = {json.dumps(fx.A)};\n", DRIVER, budget=30000)
    assert not out.get("driver_error"), out.get("driver_error")
    assert out["errors"] == [], out["errors"]
    assert out["rows"] == 6 and out["firstName"] == HOSTILE and out["pwned"] is None
    assert "&lt;img" in out["nameHtml"] and "<img" not in out["nameHtml"]
    assert out["badge"] == "COMPLETED" and len(out["tabs"]) == 11 and all(v > 0 for v in out["seen"].values()), out["seen"]
    assert out["points"] == 3 and out["pointTitle"] == "POINT 1"
    assert out["stackBadge"] in ("STORED", "ERROR")                     # ERROR only where headless Chromium has no WebGL
    assert not [c for c in out["calls"] if "/runtime/" in c or "quicklook_products" in c]
    writes = [c for c in out["calls"] if not c.startswith("GET ")]
    assert writes == ["POST /api/sessions/annotate"], writes
    assert out["annotateBody"]["id"] == fx.A and out["annotateBody"]["patch"]["tags"] == ["V3", "night"]


DELETE_DRIVER = r"""
await until(() => document.querySelectorAll("#tbl-sessions input[type=checkbox]").length > 0, 6000);
const boxes = [...document.querySelectorAll("#tbl-sessions input[type=checkbox]")];
boxes[0].click(); boxes[1].click(); await sleep(50);
out.afterTicks = $("sel-count").textContent;
out.postsAfterTicks = window.__calls.filter((c) => !c.key.startsWith("GET ")).length;
$("btn-sel-all").click();
await until(() => /beyond this page/.test($("sel-count").textContent), 4000);
out.afterAll = $("sel-count").textContent;
$("btn-del-sessions").click();
await until(() => !$("delete-panel").hidden, 4000);
out.previewBody = window.__calls.find((c) => c.key === "POST /api/sessions/delete/preview").body;
out.label = $("btn-del-confirm").textContent;
out.blockedRow = [...document.querySelectorAll("#tbl-delete tr")].some((tr) => tr.classList.contains("row-warn"));
$("btn-del-cancel").click(); await sleep(100);
out.hiddenAfterCancel = $("delete-panel").hidden;
out.executeAfterCancel = window.__count("POST /api/sessions/delete/execute");
$("btn-del-sessions").click();
await until(() => !$("delete-panel").hidden, 4000);
$("btn-del-confirm").click();
await until(() => $("delete-result").textContent.includes("RESULT"), 4000);
out.executeBody = window.__calls.find((c) => c.key === "POST /api/sessions/delete/execute").body;
out.result = $("delete-result").textContent;
"""


def test_sessions_selection_preview_cancel_and_one_confirmation(tmp_path):
    data = tmp_path / "datatree"
    fx.build(data)
    roots = sc.Roots.under(data)
    sc.build_index(roots)
    listing = sc.list_sessions({"per_page": "5", "page": "1", "ids": "1"}, roots)
    ids = [s["campaign_id"] for s in listing["selection"]]
    preview = {"token": "a" * 64, "mode": "sessions", "categories": [], "ids": ids, "deletable": 5, "files": 40,
               "space": {"recoverable_bytes": 123456, "kept_alive_by_outside_hard_links": 0, "kept_alive_bytes": 0, "estimate_note": "estimate"},
               "blocked": [{"campaign_id": ids[0], "why": "PRESERVED: x"}], "busy": [], "kept_resources": [{"path": fx.PROFILE_A, "why": "calibration profile"}],
               "confirm_label": "DELETE 5 SESSIONS — IRREVERSIBLE",
               "sessions": [{"campaign_id": i, "name": i, "started_chile": None, "capture_state": "COMPLETED", "files": 8,
                             "space": {"recoverable_bytes": 1000}, "items": [], "kept": [], "warnings": [], "blocked": "PRESERVED: x" if n == 0 else None}
                            for n, i in enumerate(ids)]}
    result = {"batch_id": "DEL-1", "audit_dir": "data/sessions/deletions/DEL-1", "bytes_removed_estimate": 123456, "free_bytes_change_observed": 120000,
              "catalog_sessions_after": 1, "sessions": [{"campaign_id": i, "status": "DELETED", "files_removed": 8, "bytes_removed_estimate": 1000,
                                                         "error_count": 0, "errors": [], "blocked": None} for i in ids[1:]]}
    js = (f"window.__routes['GET /api/sessions/list'] = {{ body: {{ ok: true, data: {json.dumps(listing)} }} }};\n"
          f"window.__routes['POST /api/sessions/delete/preview'] = {{ body: {{ ok: true, data: {json.dumps(preview)} }} }};\n"
          f"window.__routes['POST /api/sessions/delete/execute'] = {{ body: {{ ok: true, data: {json.dumps(result)} }} }};\n"
          "window.__routes['GET /api/sessions/deletions'] = { body: { ok: true, data: { batches: [] } } };\n").replace("</", "<\\/")
    out = run_page(tmp_path, "sessions", js, DELETE_DRIVER, budget=20000)
    assert not out.get("driver_error"), out.get("driver_error")
    assert out["errors"] == [], out["errors"]
    assert out["afterTicks"].startswith("2 selected") and out["postsAfterTicks"] == 0          # ticking never deletes
    assert out["afterAll"].startswith(f"{len(ids)} selected") and "beyond this page" in out["afterAll"]
    assert sorted(out["previewBody"]["ids"]) == sorted(ids) and out["previewBody"]["mode"] == "sessions"
    assert out["label"] == "DELETE 5 SESSIONS — IRREVERSIBLE" and out["blockedRow"]
    assert out["hiddenAfterCancel"] and out["executeAfterCancel"] == 0
    assert out["executeBody"]["token"] == "a" * 64 and out["executeBody"]["confirm"] == "DELETE"
    assert "free space change observed" in out["result"] and "DEL-1" in out["result"]
