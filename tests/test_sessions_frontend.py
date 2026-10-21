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
