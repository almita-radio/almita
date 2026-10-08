"""WEB FRONTEND: the real pages and scripts of console/ run in headless Chromium (already required by the console tests) with a stubbed
fetch, so no backend, INDI, SDR, mount or capture.py is involved. Each harness drives the page like an operator (double clicks, reload
during a run, dropped backend, hostile strings) and writes what it observed into #__result for the assertions below."""
import contextlib
import json
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import almita_console_server as console_server
import serve_dashboard
from runtime_state import atomic_write_json

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "console"

STUB = r"""
window.__calls = []; window.__routes = {};
function __mk(status, body, headers) {
  return { ok: status >= 200 && status < 300, status, headers: { get: (k) => (headers || {})[k] || null },
           text: async () => (body === undefined ? "" : (typeof body === "string" ? body : JSON.stringify(body))) };
}
window.fetch = (url, opts) => {
  opts = opts || {};
  const method = opts.method || "GET";
  const path = String(url).replace(/^[a-z]+:\/\/[^/]+/, "").split("?")[0];
  const key = method + " " + path;
  window.__calls.push({ key, body: opts.body === undefined ? null : JSON.parse(opts.body) });
  const h = window.__routes[key];
  return new Promise((resolve, reject) => {
    if (opts.signal) opts.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
    if (!h) { resolve(__mk(404, { error: "no stub route " + key })); return; }
    const r = typeof h === "function" ? h(opts.body ? JSON.parse(opts.body) : null, String(url)) : h;
    if (r === "network-error") { setTimeout(() => reject(new TypeError("Failed to fetch")), 1); return; }
    setTimeout(() => resolve(__mk(r.status || 200, r.body, r.headers)), r.delay || 0);
  });
};
window.__count = (key) => window.__calls.filter((c) => c.key === key).length;
window.confirm = (t) => { window.__confirmText = t; return true; };
window.__errors = [];
window.addEventListener("error", (e) => window.__errors.push(String(e.message)));
window.addEventListener("unhandledrejection", (e) => window.__errors.push("unhandled: " + String(e.reason)));
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms) { const end = Date.now() + (ms || 4000); while (Date.now() < end) { try { if (fn()) return true; } catch (e) {} await sleep(20); } return false; }
const HEALTH = { ok: true, status: "OK", data: { generated_utc: new Date().toISOString(),
  services: { observe_api: { state: "UP", detail: "x" }, field_console: { state: "UP", detail: "y" }, console_watcher: { state: "UP", detail: "z" } },
  dependencies: { indi: { state: "UP", detail: "i" }, mount: { state: "NOT_EXPOSED", detail: "m" }, main_sdr: { state: "AVAILABLE", detail: "s" }, rfi_sdr: { state: "DOWN", detail: "r", optional: true } },
  workflows: { observation: { state: "PLANNED", detail: "acquisition: IDLE" }, calibration: { state: "IDLE", detail: "c" }, alignment: { state: "IDLE", detail: "a" } },
  operational: { level: "READY", ready: true, reasons: [], degraded_by: [], note: "n" },
  version: { git_short_sha: "abc1234", started_utc: new Date().toISOString(), hostname: "h", transport: "HTTP (LAN)", project_url: "https://github.com/almita-radio/almita" } } };
window.__routes["GET /api/system/health"] = { body: HEALTH };
window.__routes["GET /api/system/version"] = { body: { ok: true, data: HEALTH.data.version } };
"""

PLAN = r"""
const PLAN = { observation_name: "WEBTEST", planning_timestamp_utc: new Date().toISOString(), max_recommended_start_delay_minutes: 30, visibility: "PASS",
  min_predicted_altitude_deg: 40.2, resolved: { rows: 3, cols: 3, point_count: 9, footprint_width_deg: 4, footprint_height_deg: 4, center_ra_hours: 6, center_dec_deg: -30,
    spacing_deg: 2, placement_reasoning: "FIXED" }, requested: { capture: { seconds: 10, settle_seconds: 2 } },
  main: { center_frequency_hz: 1420405000, sample_rate: 2400000, gain_db: 40.2, bias_tee: true }, rfi_ref: { enabled: true, bias_tee: true },
  duration: { estimated_seconds: 300, conservative_seconds: 400 }, storage: { required_bytes: 5e8, free_bytes_at_plan_time: 4e11 },
  preflight: { overall: "PASS", checks: [{ status: "PASS", name: "disk", criticality: "HIGH", detail: "ok" }] }, grid_session_dir: "data/mosaic/WEBTEST-2026",
  _resolved_plan_path: "data/mosaic/WEBTEST-2026/observation_resolved.json" };
const RUNNING = { orchestrator: { orchestrator_state: "RUNNING", session_id: "s1", capture_pid: 11, capture_process_alive: true },
  current_session: { state: "RUNNING", session_name: "WEBTEST", points_total: 25, points_success: 11, points_failed: 0, points_deferred: 1, point_current: 12,
    current_point_id: "12", last_successful_point_id: "11", last_capture_utc: new Date().toISOString(), started_utc: new Date(Date.now() - 600000).toISOString(),
    updated_utc: new Date().toISOString() } };
"""


def chromium(html_path: Path, budget_ms=6000, width=None):
    cmd = ["chromium", "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--allow-file-access-from-files",
           f"--virtual-time-budget={budget_ms}", "--dump-dom"]
    if width:
        cmd.append(f"--window-size={width},1000")
    run = subprocess.run(cmd + [html_path.as_uri()], capture_output=True, text=True, timeout=90)
    assert run.returncode == 0, run.stderr
    return run.stdout


def result_of(dom: str) -> dict:
    m = re.search(r'<pre id="__result">(.*?)</pre>', dom, re.S)
    assert m, dom[:600]
    import html
    return json.loads(html.unescape(m.group(1)))


def page_harness(tmp_path, page, routes_js, driver_js, js_files=None):
    """A copy of console/<page>.html with the stub installed BEFORE its scripts and a driver appended AFTER them."""
    for name in (js_files or ("common.js", f"{page}.js", "styles.css")):
        shutil.copyfile(CONSOLE / name, tmp_path / name)
    html = (CONSOLE / f"{page}.html").read_text()
    html = html.replace("<body>", "<body>\n<script>" + STUB + PLAN + routes_js + "</script>", 1)
    html = html.replace("</body>", '<pre id="__result"></pre>\n<script>(async () => { const out = {}; try {\n' + driver_js +
                        '\n} catch (e) { out.driver_error = String(e && e.stack || e); }\n out.errors = window.__errors; $("__result").textContent = JSON.stringify(out); })();</script>\n</body>', 1)
    target = tmp_path / f"{page}.html"
    target.write_text(html)
    return target


def run_page(tmp_path, page, routes_js, driver_js, budget=8000):
    return result_of(chromium(page_harness(tmp_path, page, routes_js, driver_js), budget))


# ------------------------------------------------------------------ common.js primitives

def test_common_js_formatting_api_errors_guard_and_poller(tmp_path):
    shutil.copyfile(CONSOLE / "common.js", tmp_path / "common.js")
    driver = r"""
const U = window.AlmitaUI;
out.esc = U.esc('<img src=x onerror="a()">&\'');
out.esc_null = U.esc(null) + "|" + U.esc(undefined);
out.nums = [U.num(NaN), U.num(Infinity), U.num(null), U.num(1234.567, 1, "Hz"), U.fixed(undefined), U.fixed(2.5, 2, "deg"), U.mhz(1420405751.77), U.mhz(NaN), U.msps(2400000),
            U.bytes(5e8), U.bytes(NaN), U.hms(3725), U.hms(NaN), U.hms(-5), U.raHours(11.83), U.decDeg(-33.449), U.raHours(NaN)];
out.utc = [U.utc("2026-09-21T14:00:05.123+00:00"), U.utc("2026-09-21T10:00:00-04:00"), U.utc(null), U.utc("garbage"), U.utcTime("2026-09-21T14:00:05Z")];
out.stateClass = [U.stateClass("NOT_EXPOSED"), U.stateClass("Not Ready"), U.stateClass(undefined)];
// ---- api: every failure kind is explicit and never throws
window.__routes["GET /ok"] = { body: { a: 1 } };
window.__routes["GET /e500"] = { status: 500, body: { error: "boom (request id abc)", request_id: "abc" }, headers: { "X-Request-Id": "abc" } };
window.__routes["GET /e409"] = { status: 409, body: { error: "already running" } };
window.__routes["GET /html"] = { body: "<html>not json</html>" };
window.__routes["GET /slow"] = { delay: 3000, body: { a: 1 } };
window.__routes["GET /down"] = "network-error";
const r_ok = await U.api("/ok"); const r500 = await U.api("/e500"); const r409 = await U.api("/e409"); const rhtml = await U.api("/html");
const rslow = await U.api("/slow", { timeoutMs: 200 }); const rdown = await U.api("/down"); const r404 = await U.api("/missing");
out.api = { ok: [r_ok.ok, r_ok.data.a], e500: [r500.ok, r500.error.kind, r500.error.retryable, r500.error.requestId], e409: [r409.error.retryable, r409.error.message],
            html: [rhtml.error.kind], slow: [rslow.error.kind, rslow.error.message.indexOf("no response within") >= 0], down: [rdown.error.kind, rdown.error.message.indexOf("not reachable") >= 0],
            r404: [r404.status, r404.error.kind] };
out.errorText = U.errorText(r500.error);
// ---- guard: a second click while running is ignored
let runs = 0;
const btn = document.createElement("button"); btn.id = "b"; btn.textContent = "GO"; document.body.appendChild(btn);
const guarded = U.guard(btn, async () => { runs += 1; await sleep(100); }, "WORKING…");
guarded(); guarded(); guarded();
out.busyDuring = [btn.disabled, btn.textContent, btn.getAttribute("aria-busy")];
await sleep(250);
out.guard = { runs, after: [btn.disabled, btn.textContent] };
guarded(); await sleep(200); out.guard.runsAfterSecond = runs;
// setEnabled keeps a disabled button disabled after a guarded run and shows the reason
const g2 = U.guard(btn, async () => { U.setEnabled(btn, false, "MAIN SDR busy"); });
await g2();
out.reason = [btn.disabled, btn.title];
U.setEnabled(btn, true, ""); out.reasonCleared = [btn.disabled, btn.title];
// ---- poller: no overlap, backoff, stop, hidden pause is only for the tab
let concurrent = 0, maxConcurrent = 0, calls = 0;
const p = U.poller(async () => { concurrent++; maxConcurrent = Math.max(maxConcurrent, concurrent); calls++; await sleep(120); concurrent--; return true; }, { intervalMs: 30 });
p.start(); await sleep(700); p.stop(); const stoppedAt = calls; await sleep(400);
out.poller = { maxConcurrent, callsAfterStop: calls - stoppedAt, ran: stoppedAt >= 3, state: p.state() };
let fails = 0; const times = [];
const q = U.poller(async () => { fails++; times.push(Date.now()); return false; }, { intervalMs: 50, maxBackoffMs: 400 });
q.start(); await sleep(2500); q.stop();
const gaps = times.slice(1).map((t, i) => t - times[i]);
out.backoff = { state: q.state(), grows: gaps.length >= 3 && gaps[gaps.length - 1] > gaps[0] * 2, maxGap: Math.max.apply(null, gaps) };
const s = U.poller(async () => true, { intervalMs: 20, staleAfterMs: 100 }); s.start(); await sleep(60); out.live = s.state(); s.stop();
""".strip()
    html = '<!doctype html><html><body>\n<script>' + STUB + '</script>\n<script src="common.js"></script>\n<pre id="__result"></pre>\n<script>(async () => { const out = {}; try {\n' + driver + \
           '\n} catch (e) { out.driver_error = String(e && e.stack || e); }\n out.errors = window.__errors; $("__result").textContent = JSON.stringify(out); })();</script></body></html>'
    (tmp_path / "h.html").write_text(html)
    out = result_of(chromium(tmp_path / "h.html", 20000))
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["esc"] == "&lt;img src=x onerror=&quot;a()&quot;&gt;&amp;&#39;" and out["esc_null"] == "|"
    n = out["nums"]
    assert n[:3] == ["—", "—", "—"] and n[3].endswith("Hz") and n[4] == "—" and n[5] == "2.50 deg" and n[6] == "1420.405752 MHz" and n[7] == "—" and n[8] == "2.40 MS/s"
    assert n[9] == "476.8 MiB" and n[10] == "—" and n[11] == "01:02:05" and n[12] == "--:--:--" and n[13] == "00:00:00" and n[14] == "11.8300 h" and n[15] == "-33.4490°" and n[16] == "—"
    assert out["utc"] == ["2026-09-21 14:00:05 UTC", "2026-09-21 14:00:00 UTC", "—", "garbage", "14:00:05 UTC"]        # always labelled UTC, offsets honoured
    assert out["stateClass"] == ["status-not_exposed", "status-not_ready", "status-unknown"]
    a = out["api"]
    assert a["ok"] == [True, 1] and a["e500"] == [False, "http", True, "abc"] and a["e409"] == [False, "already running"] and a["html"] == ["parse"]
    assert a["slow"] == ["timeout", True] and a["down"] == ["network", True] and a["r404"][0] == 404
    assert "GET /e500" in out["errorText"] and "[request abc]" in out["errorText"] and "you can retry" in out["errorText"]
    assert out["busyDuring"] == [True, "WORKING…", "true"] and out["guard"]["runs"] == 1 and out["guard"]["after"] == [False, "GO"] and out["guard"]["runsAfterSecond"] == 2
    assert out["reason"] == [True, "MAIN SDR busy"] and out["reasonCleared"] == [False, ""]
    assert out["poller"]["maxConcurrent"] == 1 and out["poller"]["callsAfterStop"] == 0 and out["poller"]["ran"]
    assert out["backoff"]["state"] == "DISCONNECTED" and out["backoff"]["grows"] and out["backoff"]["maxGap"] <= 700
    assert out["live"] == "LIVE"


# ------------------------------------------------------------------ OBSERVE

OBSERVE_ROUTES = r"""
window.__state = { orchestrator: { orchestrator_state: "PLANNED" }, current_session: null };
window.__routes["GET /api/observe/status"] = () => ({ body: window.__state });
window.__routes["GET /api/observe/defaults"] = { body: { main: { center_frequency_hz: 1420405000, sample_rate: 2400000, gain_db: 40.2 }, grid: { min_altitude_deg: 10 }, rfi_ref: { gain_db: 25 } } };
window.__routes["POST /api/observe/plan"] = () => ({ delay: 150, body: PLAN });
window.__routes["POST /api/observe/start"] = () => { window.__state = RUNNING; return { delay: 200, body: { orchestrator_state: "RUNNING" } }; };
window.__routes["POST /api/observe/stop"] = () => { window.__state = { orchestrator: { orchestrator_state: "STOPPING", session_id: "s1" }, current_session: RUNNING.current_session }; return { delay: 400, body: { orchestrator_state: "ABORTED" } }; };
"""


def test_observe_page_double_submit_summary_and_disabled_reasons(tmp_path):
    driver = r"""
await until(() => $("almita-footer").textContent.indexOf("build abc1234") >= 0 && $("hdr-status").textContent.indexOf("PLANNED") >= 0, 3000);
out.start_initial = [$("btn-start").disabled, $("btn-start").title, document.querySelector('.reason[data-for="btn-start"]').textContent];
out.stop_initial = [$("btn-stop").disabled, $("btn-stop").title];
out.header = [document.querySelector("#almita-header h1").textContent, document.querySelector('.obs-nav-link.active').textContent, document.querySelector('.obs-nav-link.active').getAttribute("aria-current")];
out.strip = $("hdr-status").textContent;
out.footer = $("almita-footer").textContent;
// two submits in the same tick -> one POST /plan
$("observe-form").requestSubmit(); $("observe-form").requestSubmit();
await sleep(20); out.planBusy = [$("btn-plan").disabled, $("btn-plan").textContent];
await until(() => !$("plan-result").hidden, 3000);
out.planPosts = window.__count("POST /api/observe/plan");
out.planBadge = $("plan-badge").textContent;
out.startAfterPlan = [$("btn-start").disabled, $("btn-start").title];
out.startSummary = $("start-summary").textContent;
out.planSummary = $("plan-summary").textContent;
// rapid double click START -> one POST /start, dialog shows the critical parameters
$("btn-start").click(); $("btn-start").click(); $("btn-start").click();
await until(() => !$("run-status").hidden, 3000);
out.startPosts = window.__count("POST /api/observe/start");
out.startBody = window.__calls.filter((c) => c.key === "POST /api/observe/start")[0].body;
out.confirm = window.__confirmText;
await sleep(2500);
out.runBadge = $("run-badge").textContent; out.runKind = $("run-kind").textContent; out.runSummary = $("run-summary").textContent;
out.progress = [$("run-progress").value, $("run-progress-text").textContent];
out.statusPolls = window.__count("GET /api/observe/status");
out.stopEnabled = [$("btn-stop").disabled, $("btn-stop").title];
// STOP twice -> one POST; request is not completion
$("btn-stop").click(); $("btn-stop").click();
await sleep(150);
out.stopNote = $("stop-note").textContent; out.stopNoteHidden = $("stop-note").hidden;
await sleep(2600);
out.stopPosts = window.__count("POST /api/observe/stop");
out.afterStop = [$("run-badge").textContent, $("run-kind").textContent, $("stop-note").textContent];
""".strip()
    out = run_page(tmp_path, "observe", OBSERVE_ROUTES, driver, budget=30000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["start_initial"][0] is True and out["start_initial"][1] == "plan first" and "plan first" in out["start_initial"][2]        # disabled WITH a reason
    assert out["stop_initial"] == [True, "no observation is running"]
    assert out["header"] == ["ALMITA OBSERVE", "OBSERVE", "page"] and "PLANNED" in out["strip"] and "AVAILABLE" in out["strip"] and "UP" in out["strip"]
    assert "Felipe Fridman" in out["footer"] and "GitHub project" in out["footer"] and "abc1234" in out["footer"]
    assert out["planPosts"] == 1 and out["planBusy"][0] is True and out["planBadge"] == "READY"
    assert out["startAfterPlan"] == [False, ""]
    for needle in ("WEBTEST", "9 / 9", "1420.405000 MHz", "2.40 MS/s", "40.2 dB", "WILL slew", "MAIN SDR free", "00:05:00"):
        assert needle in out["startSummary"], needle
    assert "Start before" in out["planSummary"] and "UTC" in out["planSummary"] and "6.0000 h" in out["planSummary"] and "-30.0000°" in out["planSummary"]      # units, UTC label
    assert out["startPosts"] == 1 and out["startBody"] == {"resolved_plan_path": "data/mosaic/WEBTEST-2026/observation_resolved.json", "confirm": True}
    assert "START OBSERVATION now?" in out["confirm"] and "WEBTEST" in out["confirm"] and "1420.405000 MHz" in out["confirm"] and "slew" in out["confirm"]
    assert out["runBadge"] == "RUNNING" and out["runKind"].startswith("RUNNING") and out["statusPolls"] >= 3 and out["stopEnabled"] == [False, ""]
    assert "point index: 12 / 25" in out["runSummary"] and "succeeded: 11" in out["runSummary"] and "elapsed: 00:10:0" in out["runSummary"] and "~remaining (estimate" in out["runSummary"]
    assert abs(out["progress"][0] - 44.0) < 0.1 and "11 / 25" in out["progress"][1]
    assert out["stopPosts"] == 1 and "STOP REQUESTED" in out["stopNote"] and "not completion" in out["stopNote"] and out["stopNoteHidden"] is False
    assert out["afterStop"][0] in ("ABORTED", "STOPPING") and ("PARTIAL" in out["afterStop"][1] or "STOP REQUESTED" in out["afterStop"][1])


def test_observe_page_errors_are_visible_and_start_survives_a_timeout(tmp_path):
    routes = OBSERVE_ROUTES + r"""
window.__routes["POST /api/observe/start"] = () => ({ status: 409, body: { ok: false, error: "an observation is already RUNNING (session_id=s9) (request id r409)", request_id: "r409" }, headers: { "X-Request-Id": "r409" } });
"""
    driver = r"""
await sleep(100);
$("observe-form").requestSubmit(); await until(() => !$("plan-result").hidden, 3000);
$("btn-start").click(); await sleep(400);
out.conflict = [$("error-banner").hidden, $("error-banner").textContent, $("btn-start").disabled, $("btn-start").textContent, $("run-status").hidden];
window.__routes["POST /api/observe/plan"] = "network-error";
$("btn-replan").click(); $("observe-form").requestSubmit(); await sleep(400);
out.down = [$("error-banner").hidden, $("error-banner").textContent, $("btn-plan").disabled, $("btn-plan").textContent];
window.__routes["POST /api/observe/plan"] = { status: 500, body: { ok: false, error: "unexpected error (KeyError) (request id r500)", request_id: "r500" } };
$("observe-form").requestSubmit(); await sleep(400);
out.err500 = $("error-banner").textContent;
""".strip()
    out = run_page(tmp_path, "observe", routes, driver, budget=12000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    hidden, text, disabled, label, run_hidden = out["conflict"]
    assert hidden is False and "POST /api/observe/start" in text and "already RUNNING" in text and "[request r409]" in text        # what failed, where, request id
    assert disabled is False and label == "START OBSERVATION" and run_hidden is True                                              # button usable again, no phantom run
    assert out["down"][0] is False and "backend not reachable" in out["down"][1] and "you can retry" in out["down"][1] and out["down"][2] is False and out["down"][3] == "PLAN OBSERVATION"
    assert "unexpected error (KeyError)" in out["err500"] and "[request r500]" in out["err500"]


def test_observe_page_start_timeout_is_not_reported_as_not_started(tmp_path):
    routes = OBSERVE_ROUTES + r"""
window.__routes["POST /api/observe/start"] = () => { window.__state = RUNNING; return "network-error"; };
"""
    driver = r"""
$("observe-form").requestSubmit(); await until(() => !$("plan-result").hidden, 3000);
$("btn-start").click(); await sleep(1500);
out.banner = $("error-banner").textContent; out.runVisible = !$("run-status").hidden; out.badge = $("run-badge").textContent;
""".strip()
    out = run_page(tmp_path, "observe", routes, driver, budget=12000)
    assert "may have taken effect" in out["banner"] and out["runVisible"] is True and out["badge"] == "RUNNING"          # the real state is looked up, not assumed


def test_observe_page_reload_during_a_run_rebuilds_state_from_the_backend(tmp_path):
    routes = OBSERVE_ROUTES + "\nwindow.__state = RUNNING;\n"
    driver = r"""
await until(() => !$("run-status").hidden, 3000); await sleep(1200);
out.runVisible = !$("run-status").hidden; out.planHidden = $("plan-result").hidden; out.badge = $("run-badge").textContent; out.summary = $("run-summary").textContent;
out.stop = [$("btn-stop").disabled]; out.polls = window.__count("GET /api/observe/status");
""".strip()
    out = run_page(tmp_path, "observe", routes, driver, budget=10000)
    assert out["runVisible"] is True and out["badge"] == "RUNNING" and "point index: 12 / 25" in out["summary"] and out["stop"] == [False] and out["polls"] >= 2       # not "IDLE" after a reload


@pytest.mark.parametrize("final,expect", [("ABORTED", "PARTIAL — 11 of 25 points captured (ABORTED)"), ("FAILED", "PARTIAL — 11 of 25 points captured (FAILED)")])
def test_observe_page_terminal_partial_campaign_says_partial(tmp_path, final, expect):
    routes = OBSERVE_ROUTES + "\nwindow.__state = { orchestrator: { orchestrator_state: '%s', session_id: 's1' }, current_session: Object.assign({}, RUNNING.current_session, { state: '%s' }) };\n" % (final, final)
    driver = 'await until(() => !$("run-status").hidden, 3000); await sleep(300); out.kind = $("run-kind").textContent; out.polls = window.__count("GET /api/observe/status"); await sleep(3000); out.polls2 = window.__count("GET /api/observe/status");'
    out = run_page(tmp_path, "observe", routes, driver, budget=10000)
    assert out["kind"] == "LAST RUN — " + expect and out["polls"] == out["polls2"]                                                                    # terminal: no endless polling


def test_observe_page_shows_no_eta_before_enough_points_and_never_nan(tmp_path):
    routes = OBSERVE_ROUTES + r"""
window.__state = { orchestrator: { orchestrator_state: "RUNNING", session_id: "s1", capture_process_alive: false }, current_session: { state: "RUNNING", points_total: null, points_success: "x",
  point_current: NaN, started_utc: "garbage", updated_utc: null } };
"""
    driver = 'await until(() => !$("run-status").hidden, 3000); await sleep(500); out.summary = $("run-summary").textContent; out.progressHidden = $("run-progress-wrap").hidden;'
    out = run_page(tmp_path, "observe", routes, driver, budget=8000)
    assert "NaN" not in out["summary"] and "undefined" not in out["summary"] and "remaining" not in out["summary"] and out["progressHidden"] is True
    assert "recorded capture process is NOT alive" in out["summary"]


def test_observe_page_treats_backend_strings_as_text_not_markup(tmp_path):
    hostile = "<img src=x onerror=window.__pwned=1>"
    routes = OBSERVE_ROUTES + """
window.__routes["POST /api/observe/plan"] = () => ({ body: Object.assign({}, PLAN, { observation_name: %s, preflight: { overall: "WARNING", checks: [{ status: "WARNING", name: %s, criticality: "LOW", detail: %s }] } }) });
window.__routes["GET /api/observe/status"] = () => ({ body: { orchestrator: { orchestrator_state: "FAILED", session_id: %s, note: %s }, current_session: null } });
""" % ((json.dumps(hostile),) * 5)
    driver = 'await sleep(300); $("observe-form").requestSubmit(); await until(() => !$("plan-result").hidden, 3000); await sleep(200); out.pwned = window.__pwned === undefined; out.imgs = document.querySelectorAll("img[src=x]").length; out.text = $("plan-summary").textContent + $("preflight-list").textContent + $("run-summary").textContent;'
    out = run_page(tmp_path, "observe", routes, driver, budget=8000)
    assert out["pwned"] is True and out["imgs"] == 0 and hostile in out["text"]


# ------------------------------------------------------------------ ALIGN / CALIBRATE

ALIGN_ROUTES = r"""
window.__routes["GET /api/align/status"] = { body: { ok: true, blocked: false, data: { deployment_state: "INDOORS", resource: { status: "FREE", orchestrator_state: "PLANNED" },
  hi: { phase: "FIRST_LIGHT_HI", sync: { sync_allowed: false } }, solar: { sync_authorized: false }, sessions: { solar: [{ session_id: "SOLAR-1", phase: "DONE" }], hi: [] } } } };
window.__routes["POST /api/align/plan/solar"] = { delay: 50, body: { ok: true, blocked: false, data: { session_id: "SOLAR-NEW", state: "PLANNED" } } };
window.__routes["POST /api/align/preflight/solar"] = { delay: 50, body: { ok: true, blocked: false, data: { ok: true, checks: [{ ok: true, name: "c", detail: "d" }] } } };
window.__routes["POST /api/align/run/solar"] = { delay: 200, body: { ok: true, blocked: false, data: { session_id: "SOLAR-NEW", state: "STARTED" } } };
window.__routes["GET /api/align/session/SOLAR-NEW"] = () => ({ body: { ok: true, blocked: false, data: { session_id: "SOLAR-NEW", state: { state: "RUNNING" }, result: null, job_running: window.__jobRunning !== false } } });
window.__routes["GET /api/align/session/SOLAR-1"] = () => ({ body: { ok: true, blocked: false, data: { session_id: "SOLAR-1", state: { state: "RUNNING" }, result: null, job_running: !!window.__adopt } } });
"""


def test_align_page_guards_double_run_explains_disabled_buttons_and_shows_errors(tmp_path):
    driver = r"""
await sleep(300);
out.initial = [$("btn-preflight").disabled, $("btn-run-sim").disabled, $("btn-run-real").disabled, $("btn-run-real").title,
               document.querySelector('.reason[data-for="btn-run-real"]').textContent, document.querySelector('.reason[data-for="btn-preflight"]').textContent];
out.header = document.querySelector("#almita-header h1").textContent;
$("btn-plan").click(); $("btn-plan").click(); await sleep(300);
out.planPosts = window.__count("POST /api/align/plan/solar");
$("btn-preflight").click(); await sleep(300);
out.runEnabled = $("btn-run-sim").disabled;
$("btn-run-sim").click(); $("btn-run-sim").click(); $("btn-run-sim").click(); await sleep(600);
out.runPosts = window.__count("POST /api/align/run/solar"); out.runBtn = [$("btn-run-sim").disabled, $("btn-run-sim").title];
out.simFlag = $("simulation-flag").hidden;
window.__jobRunning = false; await sleep(2500);
out.afterJob = [$("btn-run-sim").disabled];
window.__routes["POST /api/align/plan/solar"] = { status: 500, body: { ok: false, error: "unexpected error (OSError) (request id r7)", request_id: "r7" } };
$("btn-plan").click(); await sleep(300); out.err = $("error-banner").textContent;
window.__routes["GET /api/align/status"] = "network-error"; await sleep(11000); out.statusErr = $("error-banner").textContent;
""".strip()
    out = run_page(tmp_path, "align", ALIGN_ROUTES, driver, budget=60000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["initial"][:4] == [True, True, False, ""]                     # REAL alignment is enabled from its own panel (typed MOVE + real plan gate it there)
    assert out["initial"][4] == "" and "plan first" in out["initial"][5] and out["header"] == "ALMITA ALIGN"
    assert out["planPosts"] == 1 and out["runEnabled"] is False and out["runPosts"] == 1 and out["runBtn"] == [True, "a simulation is running"] and out["simFlag"] is False
    assert out["afterJob"] == [False]
    assert "POST /api/align/plan/solar" in out["err"] and "unexpected error (OSError)" in out["err"] and "[request r7]" in out["err"]
    assert "Status request failed" in out["statusErr"] and "backend not reachable" in out["statusErr"]


def test_align_page_reload_adopts_a_running_session(tmp_path):
    routes = ALIGN_ROUTES + "\nwindow.__adopt = true;\n"
    driver = 'await sleep(1500); out.badge = $("mode-badge").textContent; out.body = $("mode-body").textContent; out.run = [$("btn-run-sim").disabled, $("btn-run-sim").title]; out.polls = window.__count("GET /api/align/session/SOLAR-1");'
    out = run_page(tmp_path, "align", routes, driver, budget=10000)
    assert out["badge"] == "RUNNING" and "run is in progress on the server" in out["body"] and out["run"] == [True, "a simulation is running"] and out["polls"] >= 1


# ------------------------------------------------------------------ ALIGN: REAL ALIGNMENT panel (real-plan/real-run,
# the /api/ops/... job-based panel - separate from the simulation/legacy "real" toggle above) freeze-on-PLAN
# regression. Reported live: refreshSky() runs every 30s and recomputes A/B/C's coordinates; comparing a fresh
# planParams() (which re-looked-up the selected area's CURRENT, just-refreshed coordinates) against an approved
# PLAN's own recorded params made the PLAN go OBSOLETE on its own, with no operator action, well inside the
# operator's review window. The fix compares against the approved PLAN's own frozen centre (comparisonParams(),
# console/align.js) as long as the operator hasn't explicitly changed mode/area, and RUN now always sends the
# job's own recorded params - never a fresh live one - as its payload.

REAL_ALIGN_ROUTES = r"""
window.__routes["GET /api/ops/align/defaults"] = { body: { data: {
  duration_model: { overhead_per_point_s: 5, ensemble_fixed_s: 10, psd_per_20s_capture_s: 1, metric_per_20s_capture_s: 1, solar_metric_per_20s_capture_s: 1 },
  ring_radii_deg: [5.0, 2.0], ring_points: 4, capture_time_s: 20, beam_fwhm_deg: 20, min_elevation_deg: 20,
  plan_validity_seconds: 180, beam_fwhm_source: "test", beam_fwhm_note: "test note" } } };
window.__routes["GET /api/ops/mount"] = { body: { data: { mount_state: "Tracking", tracking: "on", track_mode: "sidereal", eod_state: "Ok", parked: false,
  onstep_error: "None", ra_h: 6.0, dec_deg: -30.0, pier: ["EAST"], read_utc: new Date().toISOString(), connected: true } } };
window.__routes["GET /api/ops/jobs"] = { body: { data: [] } };
window.__skyCall = 0;
window.__routes["GET /api/ops/align/sky"] = () => {
  window.__skyCall++;
  // a real refresh recomputing candidate coordinates: same labels every time, slightly different numbers each
  // call - exactly what refreshSky()'s 30s auto-refresh (or an operator-pressed REFRESH SKY VIEW) does live.
  const drift = window.__skyCall * 0.01;
  const areas = [
    { label: "A", ra_hours: 6.000 + drift, dec_deg: -30.000 - drift, az_deg: 90, alt_deg: 45, radius_deg: 5, score: 0.9, contrast_1e20cm2: 1.0, mean_1e20cm2: 2.0 },
    { label: "B", ra_hours: 8.000 + drift, dec_deg: -20.000 - drift, az_deg: 120, alt_deg: 40, radius_deg: 5, score: 0.8, contrast_1e20cm2: 1.0, mean_1e20cm2: 2.0 },
    { label: "C", ra_hours: 10.000 + drift, dec_deg: -10.000 - drift, az_deg: 150, alt_deg: 35, radius_deg: 5, score: 0.7, contrast_1e20cm2: 1.0, mean_1e20cm2: 2.0 } ];
  window.__lastAreaA = areas[0];
  return { body: { data: { computed_utc: new Date().toISOString(), sun: { az_deg: 180, alt_deg: -10, above_horizon: false },
    areas, ranking_defendible: true, ranking_note: "test ranking", catalog_source: null } } };
};
window.__jobs = {}; window.__planSeq = 0; window.__planAgeAtCreationS = 0;
window.__routes["POST /api/ops/start/align_plan"] = (body) => {
  window.__planSeq++;
  const jobId = "plan-" + window.__planSeq;
  const c = (body.params.center_ra_hours != null) ? { ra_hours: body.params.center_ra_hours, dec_deg: body.params.center_dec_deg } : null;
  // ended_utc backdated by window.__planAgeAtCreationS (default 0) - lets a driver simulate a PLAN that
  // finished some real seconds ago (e.g. past the 180s validity window) without actually waiting for it,
  // exactly mirroring the server's own real, wall-clock-based ended_utc field.
  const endedUtc = new Date(Date.now() - (window.__planAgeAtCreationS || 0) * 1000).toISOString();
  window.__jobs[jobId] = { job_id: jobId, state: "EXITED", verdict: "PASS", exit_code: 0, elapsed_s: 1, params: body.params,
    facts: { center: c,
      pattern_config: { total_positions: 9, ring_radii_deg: body.params.ring_radii, ring_points_per_ring: body.params.ring_points },
      planned_positions: [{ index: 0, ra_hours: c ? c.ra_hours : 6, dec_deg: c ? c.dec_deg : -30, separation_from_center_deg: 0, elapsed_offset_s: 0, capture_start_utc: new Date().toISOString(), alt_min_deg: 45 }],
      max_separation_from_center_deg: 5.0,
      temporal_check: { ok: true, min_altitude_deg: 45, min_altitude_point_index: 0, min_altitude_utc: new Date().toISOString(), margin_deg: 25, run_duration_s: 300, min_elevation_deg: 20 } },
    output_dir: "data/align/PLAN-" + jobId, started_utc: endedUtc, ended_utc: endedUtc, log_tail: "" };
  return { body: { data: { job_id: jobId } } };
};
window.__routes["GET /api/ops/job/plan-1"] = () => ({ body: { data: window.__jobs["plan-1"] } });
window.__routes["GET /api/ops/job/plan-2"] = () => ({ body: { data: window.__jobs["plan-2"] } });
window.__routes["POST /api/ops/start/align"] = (body) => {
  window.__jobs["run-1"] = { job_id: "run-1", state: "EXITED", verdict: "PASS", exit_code: 0, elapsed_s: 1, params: body.params, facts: {},
    output_dir: "data/align/RUN-1", started_utc: new Date().toISOString(), log_tail: "" };
  return { body: { data: { job_id: "run-1" } } };
};
window.__routes["GET /api/ops/job/run-1"] = () => ({ body: { data: window.__jobs["run-1"] } });
"""

_PICK_AREA_JS = 'function pick(label) { const b = [...document.querySelectorAll("#real-hi-areas button")].find((x) => x.textContent.startsWith(label + ":")); b.click(); }'


def test_align_real_hi_plan_survives_periodic_sky_refresh_but_explicit_change_requires_new_plan(tmp_path):
    driver = (_PICK_AREA_JS + r"""
await until(() => document.querySelectorAll("#real-hi-areas button").length >= 3, 5000);
$("real-confirm").value = "MOVE"; $("real-confirm").dispatchEvent(new Event("input"));
pick("A");
$("real-plan").click();
await until(() => $("real-job-align_plan").textContent.includes("PASS"), 5000);
await sleep(1100);
out.afterPlan = { stale: $("real-plan-stale").hidden, runDisabled: $("real-run").disabled };
// simulate several automatic sky refreshes (same refreshSky() the 30s setInterval calls) - candidate coordinates
// drift, the approved area's IDENTITY does not.
for (let i = 0; i < 3; i++) { $("real-sky-refresh").click(); await sleep(200); }
await sleep(1100);
out.afterRefreshes = { stale: $("real-plan-stale").hidden, runDisabled: $("real-run").disabled, skyCalls: window.__skyCall };
// an EXPLICIT operator action: pick a different HI candidate
pick("B");
await sleep(600);
out.afterExplicitChange = { stale: $("real-plan-stale").hidden, runDisabled: $("real-run").disabled, staleText: $("real-plan-stale").textContent };
""").strip()
    out = run_page(tmp_path, "align", REAL_ALIGN_ROUTES, driver, budget=40000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["afterPlan"] == {"stale": True, "runDisabled": False}                        # fresh PLAN: not stale, RUN enabled
    # several real automatic-refresh-equivalent calls happened (skyCalls counts the initial load's own refresh + 3 more)...
    assert out["afterRefreshes"]["skyCalls"] == 4
    assert out["afterRefreshes"] == {"stale": True, "runDisabled": False, "skyCalls": 4}     # ...and the PLAN is still NOT obsolete
    assert out["afterExplicitChange"]["stale"] is False and out["afterExplicitChange"]["runDisabled"] is True
    assert "PLAN OBSOLETE" in out["afterExplicitChange"]["staleText"]                        # ...but picking a different area does invalidate it


def test_align_real_run_sends_the_approved_plan_centre_not_a_live_recompute(tmp_path):
    driver = (_PICK_AREA_JS + r"""
await until(() => document.querySelectorAll("#real-hi-areas button").length >= 3, 5000);
$("real-confirm").value = "MOVE"; $("real-confirm").dispatchEvent(new Event("input"));
pick("A");
$("real-plan").click();
await until(() => $("real-job-align_plan").textContent.includes("PASS"), 5000);
await sleep(200);
out.approvedCenter = { ra: window.__jobs["plan-1"].params.center_ra_hours, dec: window.__jobs["plan-1"].params.center_dec_deg };
// several benign refreshes AFTER approval - the live sky view's "A" coordinates drift away from what was approved
for (let i = 0; i < 4; i++) { $("real-sky-refresh").click(); await sleep(200); }
await sleep(1100);
out.runEnabledAfterRefreshes = !$("real-run").disabled;
out.liveAreaA = window.__lastAreaA;
$("real-run").click();
await until(() => window.__count("POST /api/ops/start/align") >= 1, 5000);
const call = window.__calls.find((c) => c.key === "POST /api/ops/start/align");
out.sentParams = call.body.params;
""").strip()
    out = run_page(tmp_path, "align", REAL_ALIGN_ROUTES, driver, budget=40000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["runEnabledAfterRefreshes"] is True                     # benign refreshes never blocked RUN
    # the live sky view really did drift away from what was approved (otherwise this test would prove nothing)
    assert out["liveAreaA"]["ra_hours"] != out["approvedCenter"]["ra"]
    # RUN sent the ORIGINALLY APPROVED centre, not the live, drifted one
    assert out["sentParams"]["center_ra_hours"] == out["approvedCenter"]["ra"]
    assert out["sentParams"]["center_dec_deg"] == out["approvedCenter"]["dec"]
    assert out["sentParams"]["center_ra_hours"] != out["liveAreaA"]["ra_hours"]


def test_align_real_run_sends_sync_choice_and_always_shows_the_offset(tmp_path):
    """RUN sends the SYNC checkbox (ticked by default) and MAX SYNC OFFSET; when the run ends, the offset box shows
    the estimated offset and whether SYNC was sent, from the job's own facts."""
    routes = REAL_ALIGN_ROUTES + r"""
window.__routes["POST /api/ops/start/align"] = (body) => {
  window.__jobs["run-1"] = { job_id: "run-1", stage: "align", state: "EXITED", verdict: "PARTIAL", exit_code: 0, elapsed_s: 1,
    params: body.params, output_dir: "data/align/RUN-1", started_utc: new Date().toISOString(), ended_utc: new Date().toISOString(), log_tail: "",
    facts: { result_status: "PASS", sync_applied: false,
      offset: { offset_ra_deg: 0.812, offset_dec_deg: -0.5, separation_deg: 0.95, confidence: 0.71, residual: 0.2, samples: 17 },
      sync_decision: { requested: true, applied: false, confidence_threshold: 0.65, reason: "offset 0.950 deg larger than the 0.5 deg SYNC limit" } } };
  return { body: { data: { job_id: "run-1" } } };
};
"""
    driver = (_PICK_AREA_JS + r"""
await until(() => document.querySelectorAll("#real-hi-areas button").length >= 3, 5000);
out.syncDefault = $("real-sync").checked;
$("real-max-sync").value = "0.5";
$("real-confirm").value = "MOVE"; $("real-confirm").dispatchEvent(new Event("input"));
pick("A");
$("real-plan").click();
await until(() => $("real-job-align_plan").textContent.includes("PASS"), 5000);
await sleep(1100);
$("real-run").click();
await until(() => window.__count("POST /api/ops/start/align") >= 1, 5000);
out.sentParams = window.__calls.find((c) => c.key === "POST /api/ops/start/align").body.params;
await until(() => !$("real-offset").hidden, 5000);
out.offsetText = $("real-offset").textContent;
""").strip()
    out = run_page(tmp_path, "align", routes, driver, budget=40000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["syncDefault"] is True
    assert out["sentParams"]["sync"] is True and out["sentParams"]["max_sync_offset_deg"] == 0.5
    assert "ΔRA (east) +0.812°" in out["offsetText"] and "ΔDec -0.500°" in out["offsetText"]
    assert "confidence 0.710 (threshold 0.65)" in out["offsetText"]
    assert "SYNC     NOT SENT — offset 0.950 deg larger" in out["offsetText"]


# ------------------------------------------------------------------ ALIGN: REAL ALIGNMENT panel - PLAN validity
# window (180s from when PLAN itself finished with PASS, server-recorded). window.__planAgeAtCreationS
# backdates a fixture PLAN's own ended_utc so these can exercise both sides of the boundary without a real
# 180s wait; the server-side enforcement itself (almita_web_ops.py build_command()) is covered separately in
# tests/test_almita_web_ops_plan_validity.py (pure Python, no browser needed there).

def test_align_real_plan_countdown_survives_refresh_and_run_disables_exactly_at_expiry(tmp_path):
    driver = (_PICK_AREA_JS + r"""
await until(() => document.querySelectorAll("#real-hi-areas button").length >= 3, 5000);
$("real-confirm").value = "MOVE"; $("real-confirm").dispatchEvent(new Event("input"));
pick("A");
// a PLAN backdated to already be 170s old the instant it "finishes" - ~10s of its 180s window left, no real wait
window.__planAgeAtCreationS = 170;
$("real-plan").click();
await until(() => $("real-job-align_plan").textContent.includes("PASS"), 5000);
await sleep(200);
out.nearExpiry = { runDisabled: $("real-run").disabled, validityHidden: $("real-plan-validity").hidden, validityText: $("real-plan-validity").textContent };
const expiresPhrase = (out.nearExpiry.validityText.match(/expires [^)]+\)/) || [""])[0];
// an automatic-refresh-equivalent sky redraw meanwhile must not touch the countdown's anchor at all
$("real-sky-refresh").click(); await sleep(300);
out.afterRefresh = { runDisabled: $("real-run").disabled, expiresPhraseUnchanged: (($("real-plan-validity").textContent.match(/expires [^)]+\)/) || [""])[0]) === expiresPhrase };
// now a PLAN backdated to already be 200s old (past the 180s window) the instant it "finishes"
window.__planAgeAtCreationS = 200;
$("real-plan").click();
await until(() => window.__planSeq >= 2 && $("real-job-align_plan").textContent.includes("PASS"), 5000);
await sleep(1100);
out.expired = { runDisabled: $("real-run").disabled, runTitle: $("real-run").title,
                validityHidden: $("real-plan-validity").hidden, validityText: $("real-plan-validity").textContent };
""").strip()
    out = run_page(tmp_path, "align", REAL_ALIGN_ROUTES, driver, budget=40000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    # ~10s of real window left: RUN still enabled, a countdown is shown (not the OBSOLETE/EXPIRED wording)
    assert out["nearExpiry"]["runDisabled"] is False
    assert out["nearExpiry"]["validityHidden"] is False
    assert "PLAN valid for" in out["nearExpiry"]["validityText"] and "expires" in out["nearExpiry"]["validityText"]
    # a benign sky refresh must not reset or otherwise touch the deadline
    assert out["afterRefresh"]["runDisabled"] is False
    assert out["afterRefresh"]["expiresPhraseUnchanged"] is True
    # past the 180s window: RUN disabled with a clear reason, and the box switches to an explicit EXPIRED notice
    assert out["expired"]["runDisabled"] is True
    assert "validity window has passed" in out["expired"]["runTitle"]
    assert out["expired"]["validityHidden"] is False
    assert "PLAN EXPIRED" in out["expired"]["validityText"] and "180s" in out["expired"]["validityText"]


CAL_ROUTES = r"""
window.__routes["GET /api/calibrate/status"] = () => ({ body: { ok: true, blocked: false, data: { calibration_level: "OPERATIONAL_RELATIVE", calibration_workflow_active: !!window.__active,
  resource: { status: "FREE", orchestrator_state: "PLANNED", detail: "free" }, receiver: { serial: %s, center_frequency_hz: { value: 1420405751.77, verification: "SERVICE_STARTUP_ARGV" },
  sample_rate_hz: { value: 2400000, verification: "CONFIGURED_EXPECTED" }, gain_db: { value: 40.2, verification: "CONFIGURED_EXPECTED" }, bias_t_state: { value: %s, verification: null }, tuner_type: { value: "R828D", verification: "VERIFIED_BY_DEVICE_READBACK" } },
  gain_table: { candidate_table_size: 29, provenance: "p", device_reported_gain_count: null, status: "UNVERIFIED_FOR_THIS_DEVICE" }, frequency_audit: { label: "L", service_center_frequency_hz: 1420405751.77, nominal_hi_rest_hz: 1420405751.77, difference_hz: 0 },
  sessions: [{ session_id: "CAL-1", phase: "DONE" }], profiles: [] } } });
window.__routes["POST /api/calibrate/run"] = { delay: 200, body: { ok: true, blocked: false, data: { session_id: "CAL-1", state: "STARTED" } } };
window.__routes["GET /api/calibrate/session/CAL-1"] = () => ({ body: { ok: true, blocked: false, data: { session_id: "CAL-1", state: { phase: "RUNNING" }, result: window.__result || null, job_running: window.__jobRunning !== false } } });
""" % (json.dumps("<b onclick=window.__pwned=1>SER</b>"), json.dumps("<i>ON</i>"))


def test_calibrate_page_hostile_receiver_strings_are_text_and_double_run_is_guarded(tmp_path):
    driver = r"""
await sleep(400);
out.cards = document.getElementById("receiver-cards").textContent; out.bolds = document.querySelectorAll("#receiver-cards b, #receiver-cards i").length;
out.freq = [...document.querySelectorAll("#receiver-cards .card")].map((c) => c.textContent);
out.real = [$("btn-run-real").disabled, $("btn-run-real").title, $("btn-profile-build").disabled];
$("btn-run-sim").click(); $("btn-run-sim").click(); await sleep(500);
out.runPosts = window.__count("POST /api/calibrate/run"); out.runBtn = [$("btn-run-sim").disabled, $("btn-run-sim").title];
// empty per_capture, then NaN-ish histogram: explicit empty states instead of a crash
window.__result = { per_capture: [], stability: null, quality: null };
await sleep(2200); out.empty = document.getElementById("overview-cards").textContent;
window.__result = { per_capture: [{ clipping: { status: "OK", percentile_margin_codes: null }, usable_band_fraction: null, sample_statistics: { histogram: [null, NaN, 0] }, dc_half_width_hz: null }], stability: { power_rms_fraction: null, drift_slope_per_hour: NaN, warmup_detected: false }, quality: { verdict: "OK" } };
window.__jobRunning = false; await sleep(2500);
out.nan = document.getElementById("overview-cards").textContent + document.getElementById("stability-summary").textContent + document.getElementById("bandpass-notes").textContent;
out.after = [$("btn-run-sim").disabled, $("btn-profile-build").disabled];
""".strip()
    out = run_page(tmp_path, "calibrate", CAL_ROUTES, driver, budget=60000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert "<b onclick" in out["cards"] and out["bolds"] == 0                                                                     # text, not markup
    assert any("1420.405752 MHz" in c and "SERVICE STARTUP ARGV" in c for c in out["freq"]) and any("2.40 MS/s" in c for c in out["freq"])   # MHz with units + provenance tag
    assert out["real"] == [False, "", True]                                  # REAL calibration is enabled (MAIN free in the stub): its panel runs the real command
    assert out["runPosts"] == 1 and out["runBtn"] == [True, "a calibration simulation is running"]
    assert "No captures" in out["empty"]
    assert "NaN" not in out["nan"] and "undefined" not in out["nan"] and out["after"][0] is False


def test_calibrate_page_reload_adopts_the_running_job_and_status_polls_slowly(tmp_path):
    routes = CAL_ROUTES + "\nwindow.__active = true;\n"
    driver = 'await sleep(2500); out.polls = window.__count("GET /api/calibrate/session/CAL-1"); out.run = [$("btn-run-sim").disabled, $("btn-run-sim").title]; out.workflow = $("st-calibration-workflow").textContent; out.statusCalls = window.__count("GET /api/calibrate/status");'
    out = run_page(tmp_path, "calibrate", routes, driver, budget=10000)
    assert out["polls"] >= 1 and out["run"] == [True, "a calibration simulation is running"] and out["workflow"] == "SIMULATION RUNNING" and out["statusCalls"] <= 3


# ------------------------------------------------------------------ CALIBRATE: wizard form loads its CENTER FREQUENCY /
# SAMPLE RATE / GAIN from the server (never the old hardcoded HTML value="1420405000") and START stays disabled
# until that resolves - the actual frontend half of the real incident's fix (see
# tests/test_frequency_single_source_of_truth.py for the backend/single-source-of-truth half).

def test_calibrate_wizard_loads_its_frequency_from_the_server_not_a_hardcoded_html_value(tmp_path):
    routes = CAL_ROUTES + r"""
window.__routes["GET /api/ops/calibrate/wizard_defaults"] = { delay: 60, body: { ok: true, data: { center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2 } } };
"""
    driver = r"""
out.beforeLoad = { freq: $w("wz-freq").value, startDisabled: $w("wz-start").disabled };
await until(() => $w("wz-freq").value === "1420405752", 3000);
out.afterLoad = { freq: $w("wz-freq").value, rate: $w("wz-rate").value, gain: $w("wz-gain").value, startDisabled: $w("wz-start").disabled };
"""
    # calibrate.js's wizard section defines its own local $w() inside an IIFE block - expose the same lookup to the driver
    driver = 'const $w = (id) => document.getElementById(id);\n' + driver
    out = run_page(tmp_path, "calibrate", routes, driver, budget=15000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["beforeLoad"]["freq"] == "" and out["beforeLoad"]["startDisabled"] is True    # no stale/hardcoded value shown, START not yet usable
    assert out["afterLoad"] == {"freq": "1420405752", "rate": "2400000", "gain": "40.2", "startDisabled": False}


def test_calibrate_wizard_start_recovers_if_defaults_request_fails(tmp_path):
    routes = CAL_ROUTES + r"""
window.__routes["GET /api/ops/calibrate/wizard_defaults"] = { status: 500, body: { ok: false, error: "boom" } };
"""
    driver = r"""
const $w = (id) => document.getElementById(id);
await sleep(600);
out.startDisabled = $w("wz-start").disabled;
out.err = document.getElementById("error-banner").textContent;
"""
    out = run_page(tmp_path, "calibrate", routes, driver, budget=15000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["startDisabled"] is False                 # never permanently stuck disabled by a backend hiccup
    assert "could not load wizard defaults" in out["err"]


# ------------------------------------------------------------------ STATUS page

def test_status_page_layers_disconnect_and_recovery(tmp_path):
    routes = r"""
window.__health = HEALTH.data;
window.__routes["GET /api/system/health"] = () => window.__down ? "network-error" : { body: { ok: true, data: window.__health } };
"""
    driver = r"""
await until(() => $("tbl-deps").rows.length > 1, 3000);
out.services = [...$("tbl-services").rows].map((r) => r.textContent); out.deps = [...$("tbl-deps").rows].map((r) => r.textContent);
out.op = [$("op-badge").textContent, $("op-reasons").textContent]; out.updated = $("updated").textContent;
out.build = $("version-kv").textContent;
window.__health = Object.assign({}, HEALTH.data, { dependencies: Object.assign({}, HEALTH.data.dependencies, { indi: { state: "DOWN", detail: "nothing listening on port 7624" } }),
  operational: { level: "NOT_READY", ready: false, reasons: ["INDI server not listening"], degraded_by: [], note: "service UP is not operational READY" } });
$("btn-refresh").click(); $("btn-refresh").click(); await sleep(300);
out.notReady = [$("op-badge").textContent, $("op-reasons").textContent, [...$("tbl-deps").rows][0].textContent];
out.healthCalls = window.__count("GET /api/system/health");
window.__down = true; $("btn-refresh").click(); await sleep(300); $("btn-refresh").click(); await sleep(300);
out.down = [$("error-banner").hidden, $("error-banner").textContent, $("hdr-status").textContent];
window.__down = false; $("btn-refresh").click(); await sleep(300);
out.recovered = [$("error-banner").hidden, $("hdr-status").textContent];
""".strip()
    out = run_page(tmp_path, "status", routes, driver, budget=40000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert any("OBSERVE API" in r and "UP" in r for r in out["services"]) and any("CONSOLE WATCHER" in r for r in out["services"])
    assert any("MOUNT" in r and "NOT_EXPOSED" in r for r in out["deps"]) and any("RFI SDR" in r and "DOWN" in r for r in out["deps"])
    assert out["op"][0] == "READY" and "UTC" in out["updated"] and "abc1234" in out["build"]
    assert out["notReady"][0] == "NOT_READY" and "INDI server not listening" in out["notReady"][1] and "DOWN" in out["notReady"][2]
    assert out["down"][0] is False and "backend not reachable" in out["down"][1] and "DISCONNECTED" in out["down"][2]
    assert out["recovered"][0] is True and "LIVE" in out["recovered"][1]


# ------------------------------------------------------------------ SCIENCE: regression for a real reported
# failure - "PLAN failed: can't access property "toFixed", facts.spatial_params.smoothing_fwhm_b_deg is
# undefined". science_web_bridge commit 782711b replaced spatial_params' two per-map kernels
# (smoothing_fwhm_b_deg/smoothing_fwhm_c_deg) with ONE shared smoothing_fwhm_deg; a stale-loaded page still
# running the OLD science.js (which read the removed field name straight off a FRESH, new-shape PLAN result)
# crashed the whole PLAN view. science.js now goes through ONE formatter (formatSharedSmoothingFwhm) for
# every spatial_params consumer, which never assumes a shape - these tests drive the REAL PLAN button against
# a stubbed backend (no live server, no real REDUCE session) with each spatial_params shape PLAN can actually
# meet, and assert the page never throws.

def _science_facts(spatial_params):
    """A minimal, real-shaped PLAN `facts` payload (see science_web_bridge.cmd_plan's own payload) - only
    spatial_params varies between the tests below."""
    return {
        "config": {"calibration_level_filter": "RELATIVE", "interp_factor_b": 3, "interp_factor_c": 6},
        "config_hash": "deadbeef", "calibration_level_counts": {"RELATIVE": 10},
        "board_grid": {"nx": 10, "ny": 10}, "grid_b": {"ny": 30, "nx": 30}, "grid_c": {"ny": 60, "nx": 60},
        "n_rows": 10, "n_cols": 10, "spatial_params": spatial_params,
        "real_instrument_beam_fwhm_deg": 20.0, "n_velocity_channels": 8192,
        "checks": [{"name": "has_filtered_points", "ok": True, "detail": "10 point(s)"}], "blocked": False,
        "will_process_points": list(range(1, 11)),
    }


SCIENCE_ROUTES = r"""
window.__routes["GET /api/ops/science/reduce_sessions"] = { body: { data: { sessions: [
  { reduce_session_dir: "data/reduced/FAKE-CAMPAIGN/REDUCE-1", status: "COMPLETED", points_completed: 10, points_discovered: 10,
    calibration_level_counts: { RELATIVE: 10 }, campaign_id: "FAKE-CAMPAIGN", reduce_session_id: "REDUCE-1" } ], no_data: [] } } };
window.__routes["GET /api/ops/science/inspect_reduce_session"] = { body: { data: {
  campaign_id: "FAKE-CAMPAIGN", reduce_session_id: "REDUCE-1", reduce_status: "COMPLETED",
  points_planned: 10, points_reduced_completed: 10, points_reduced_blocked: 0, points_reduced_failed: 0,
  n_points_ingestable: 10, science_contract_ok: true, science_contract_problems: [],
  calibration_level_counts: { RELATIVE: 10 }, velocity_available_count: 10, velocity_missing_points: [],
  ra_deg_range: [10.0, 20.0], dec_deg_range: [-40.0, -30.0] } } };
window.__routes["POST /api/ops/start/science_heatmaps_plan"] = { body: { data: { job_id: "plan-job-1" } } };
window.__routes["GET /api/ops/job/plan-job-1"] = { body: { data: {
  job_id: "plan-job-1", state: "EXITED", verdict: "PASS", facts: %s } } };
"""

SCIENCE_DRIVER = r"""
await until(() => $("in-session").options.length > 1, 4000);
$("in-session").value = "data/reduced/FAKE-CAMPAIGN/REDUCE-1";
$("in-session").dispatchEvent(new Event("change"));
await until(() => !$("plan-panel").hidden, 4000);
$("btn-plan").click();
await until(() => $("plan-badge").textContent !== "—" || !$("error-banner").hidden, 4000);
out.badge = $("plan-badge").textContent;
out.summary = $("plan-summary").textContent;
out.errorBanner = { hidden: $("error-banner").hidden, text: $("error-banner").textContent };
"""


def test_science_plan_renders_shared_smoothing_fwhm_without_crashing(tmp_path):
    """Exact reproduction of the reported failure's PRECONDITION: a fresh PLAN result whose spatial_params
    has the NEW shape (smoothing_fwhm_deg, no smoothing_fwhm_b_deg at all) - the old code crashed reading a
    field that no longer exists; the fix must render the shared value plainly instead."""
    facts = _science_facts({"nearest_neighbor_spacing_deg": 1.1098, "mosaic_spacing_deg": 1.1098,
                            "support_radius_deg": 1.3873, "smoothing_fwhm_deg": 1.1098})
    out = run_page(tmp_path, "science", SCIENCE_ROUTES % json.dumps(facts), SCIENCE_DRIVER, budget=15000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []                      # no window "error"/"unhandledrejection" event fired
    # the app's own click handler catches a thrown error and shows it via #error-banner (not an uncaught
    # window error) - this is exactly how the real report surfaced: "PLAN failed: can't access property
    # "toFixed", ...smoothing_fwhm_b_deg is undefined". Reproduced against the pre-fix science.js/html pair
    # while writing this test: badge went READY, plan-summary stayed "", and the banner showed
    # "PLAN failed: Cannot read properties of undefined (reading 'toFixed')" - so both are checked here.
    assert out["errorBanner"]["hidden"] is True, out["errorBanner"]
    assert "PLAN failed" not in out["errorBanner"]["text"]
    assert out["badge"] == "READY"
    assert "smoothing kernel (B & C): 1.1098 deg (shared by B & C)" in out["summary"]
    assert "support radius (B & C, shared): 1.3873 deg" in out["summary"]


def test_science_plan_renders_legacy_per_map_smoothing_fwhm_without_crashing(tmp_path):
    """The OLD (pre-782711b) two-kernel shape - a manifest/config.json written before that change would still
    have this shape on disk; PLAN itself never reads one back (it always computes fresh - see cmd_plan), but
    the formatter must still degrade gracefully rather than assume the new field is always present."""
    facts = _science_facts({"nearest_neighbor_spacing_deg": 1.1098, "mosaic_spacing_deg": 1.1098,
                            "support_radius_deg": 1.3873, "smoothing_fwhm_b_deg": 1.1098, "smoothing_fwhm_c_deg": 3.3294})
    out = run_page(tmp_path, "science", SCIENCE_ROUTES % json.dumps(facts), SCIENCE_DRIVER, budget=15000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["errorBanner"]["hidden"] is True, out["errorBanner"]
    assert out["badge"] == "READY"
    assert "B=1.1098 / C=3.3294 deg" in out["summary"] and "legacy" in out["summary"]
    assert "1.1098 deg (shared by B & C)" not in out["summary"]   # never conflate the two shapes


def test_science_plan_handles_missing_spatial_params_without_crashing(tmp_path):
    """Neither shape at all (or spatial_params missing entirely) - must degrade to an explicit placeholder,
    never a fabricated number, and never throw."""
    for spatial_params, expect in (({}, "smoothing kernel not reported in this result"),
                                   (None, "no spatial_params in this result")):
        facts = _science_facts(spatial_params)
        if spatial_params is None:
            del facts["spatial_params"]
        out = run_page(tmp_path, "science", SCIENCE_ROUTES % json.dumps(facts), SCIENCE_DRIVER, budget=15000)
        assert "driver_error" not in out, out.get("driver_error")
        assert out["errors"] == []
        assert out["errorBanner"]["hidden"] is True, out["errorBanner"]
        assert out["badge"] == "READY"
        assert expect in out["summary"], out["summary"]


def test_science_plan_shows_the_board_lattice_from_the_campaign_plan(tmp_path):
    """The board of a wide campaign comes from the plan's grid_row/grid_col; PLAN says so, with the spacing's
    source, the shared A/B/C footprint and what a rectangular board would have misplaced (3.29 deg)."""
    facts = _science_facts({"nearest_neighbor_spacing_deg": 1.5638, "mosaic_spacing_deg": 1.578947,
                            "mosaic_spacing_source": "campaign_plan", "support_radius_deg": 1.9548,
                            "smoothing_fwhm_deg": 1.5638})
    facts["mosaic_geometry"] = {"lattice_source": "campaign_plan", "campaign_plan": "data/mosaic/C-1",
                                "raster_extent_deg": [37.89, 33.16], "point_to_a_cell_node_max_deg": 0.0,
                                "regular_board_offset_max_deg": 3.2935}
    out = run_page(tmp_path, "science", SCIENCE_ROUTES % json.dumps(facts), SCIENCE_DRIVER, budget=15000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert "board lattice: campaign plan data/mosaic/C-1   spacing: 1.5789 deg (campaign_plan)" in out["summary"]
    assert "one footprint 37.89 x 33.16 deg" in out["summary"]
    assert "a rectangular board would be off by up to 3.29 deg" in out["summary"]


SCIENCE_RESULT_ROUTES = r"""
window.__routes["POST /api/ops/start/science_heatmaps"] = { body: { data: { job_id: "run-1" } } };
window.__routes["GET /api/ops/job/run-1"] = { body: { data: { job_id: "run-1", state: "EXITED", verdict: "PASS",
  elapsed_s: 1, output_dir: "data/science/FAKE/S1", facts: { status: "COMPLETED" } } } };
window.__routes["GET /api/ops/file"] = { body: {
  campaign_id: "FAKE", reduce_session_id: "REDUCE-1", calibration_level_filter: "RELATIVE", data_completeness: "COMPLETE",
  n_points_used: 2, n_points_filtered_in: 2, velocity_window_m_s: [-100000, 100000], color_vmin: 0, color_vmax: 2,
  color_limits_basis: "test", grid_dims: { a: [1, 2], b: [8, 16], c: [16, 32] },
  config: { interp_factor_b: 2, interp_factor_c: 4 }, spatial_params: { smoothing_fwhm_deg: 1 },
  real_instrument_beam: { fwhm_deg: 20 }, spatial_confidence: { b: { single_point_fraction: 0 }, c: { single_point_fraction: 0 } },
  quality_b: { state: "GOOD", reasons: [] }, thermal_drift: { status: "OK", note: "" }, exports: {} } };
window.__routes["GET /api/ops/science/map"] = { body: { data: {
  n_rows: 1, n_cols: 2, extent_deg: { half_w: 4, half_h: 2 }, color_vmin: 0, color_vmax: 2, color_stops: ["#440154", "#fde725"],
  cells: [
    { row: 0, col: 0, point_index: 1, valid: true, value: 1, uncertainty: 0.1, color: "#440154", ra_deg: 1, dec_degrees: -30,
      point_status: "USED", corners_xy: [[-4, -2], [0, -2], [0, 0], [-3, 0]] },
    { row: 0, col: 1, point_index: 2, valid: true, value: 2, uncertainty: 0.1, color: "#fde725", ra_deg: 2, dec_degrees: -30,
      point_status: "USED", corners_xy: [[0, -2], [4, -2], [3, 0], [0, 0]] } ] } } };
window.__routes["GET /api/ops/reduce/point"] = { status: 404, body: { error: "no spectrum in this test" } };
"""


def test_science_board_a_draws_quadrilaterals_and_clicks_inside_them(tmp_path):
    """Map A on the canvas: each cell is its corners_xy quadrilateral, fitted into the square box like the B/C
    images (object-fit: contain, equal scale, centred), west (col 0) left / east right; a click selects only
    the cell whose polygon contains it - outside every polygon selects nothing."""
    driver = SCIENCE_DRIVER + r"""
const canvas = $("board-canvas-a");
canvas.style.width = "400px"; canvas.style.height = "400px";
$("btn-run").click();
await until(() => window.__count("GET /api/ops/science/map") > 0, 6000);
await new Promise((r) => setTimeout(r, 300));
const ctx = canvas.getContext("2d");
const dpr = window.devicePixelRatio || 1;
const px = (x, y) => Array.from(ctx.getImageData(Math.round(x * dpr), Math.round(y * dpr), 1, 1).data.slice(0, 3));
// box 400x400, footprint 8x4 deg -> 50 px/deg, centred: y in [100, 300]; cells span y 200..300 (dec -2..0)
out.left = px(120, 260); out.right = px(280, 260); out.above = px(200, 150);
const rect = canvas.getBoundingClientRect();
const click = (x, y) => canvas.dispatchEvent(new MouseEvent("click", { clientX: rect.left + x, clientY: rect.top + y, bubbles: true }));
click(280, 260); await new Promise((r) => setTimeout(r, 100)); out.detailRight = $("cell-detail").textContent;
$("cell-detail").textContent = ""; click(10, 210); await new Promise((r) => setTimeout(r, 100));
out.detailOutsideSlantedEdge = $("cell-detail").textContent;
"""
    routes = SCIENCE_ROUTES % json.dumps(_science_facts({})) + SCIENCE_RESULT_ROUTES
    out = run_page(tmp_path, "science", routes, driver, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["left"] == [0x44, 0x01, 0x54]      # west (col 0) on the left
    assert out["right"] == [0xfd, 0xe7, 0x25]     # east on the right
    assert out["above"] == [0x0d, 0x13, 0x17]     # inside the footprint but outside every cell: background
    assert "real point 2" in out["detailRight"]
    # (10, 210) is inside cell 0's bounding box but left of its slanted edge (-4,-2)->(-3,0), at x=45 px there
    assert out["detailOutsideSlantedEdge"] == ""


# ------------------------------------------------------------------ Field Console (8088): escaping, LINK state, empty data

def _status(**over):
    now = datetime.now(timezone.utc).isoformat()
    base = {"schema_version": 1, "updated_utc": now, "system_state": "READY",
            "instrument": {"cpu": 10.0, "ram": 20.0, "disk": 30.0, "rtl_tcp_process": True, "rtl_tcp_listening": True, "sdr_temperature_c": 25.0, "lna_temperature_c": 24.0,
                           "mount_state": "NOT_EXPOSED", "telemetry_stale": False, "error": None},
            "acquisition": {"state": "RUNNING", "session_id": "s1", "session_name": "demo", "point_current": 3, "points_total": 9, "points_success": 2, "points_failed": 0,
                            "points_deferred": 0, "current_point_id": "p003", "last_successful_point_id": "p002", "acquisition_stale": False, "error": None},
            "quicklook": {"state": "IDLE"}, "last_session": None}
    base.update(over)
    return base


@contextlib.contextmanager
def console_running(tmp_path, status):
    public = console_server.prepare_console_web(CONSOLE, tmp_path / "runtime", tmp_path / "public")
    if status is not None:
        atomic_write_json(tmp_path / "runtime" / "almita_status.json", status)
    server = serve_dashboard.make_server(public, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def dom_of(url, budget=3500):
    run = subprocess.run(["chromium", "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", f"--virtual-time-budget={budget}", "--dump-dom", url],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_console_escapes_backend_strings_and_survives_empty_or_nonfinite_values(tmp_path):
    hostile = "<img src=x onerror=window.__pwned=1>"
    status = _status(acquisition={**_status()["acquisition"], "session_name": hostile, "error": "<script>alert(1)</script> & more", "points_total": None, "point_current": "x"},
                     last_session={"session_name": hostile, "session_id": "<b>id</b>", "final_state": "COMPLETED", "points_success": None, "points_total": None},
                     instrument={**_status()["instrument"], "cpu": None, "ram": None, "sdr_temperature_c": None, "error": hostile})
    with console_running(tmp_path, status) as base:
        html = dom_of(base + "/")
    assert "<img src=x" not in html and "&lt;img src=x" in html and "<script>alert(1)" not in html and "&lt;script&gt;alert(1)" in html
    assert "<b>id</b>" not in html and "&lt;b&gt;id&lt;/b&gt;" in html
    body = html.split("<main")[1]
    assert "NaN" not in body and "undefined" not in body and "N/A" in body


def test_console_link_state_is_live_stale_or_disconnected(tmp_path):
    with console_running(tmp_path / "live", _status()) as base:
        assert '<dd id="link-state" class="badge status-live">LIVE</dd>' in dom_of(base + "/")
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with console_running(tmp_path / "stale", _status(updated_utc=old)) as base:
        html = dom_of(base + "/")
        assert '<dd id="link-state" class="badge status-stale">STALE</dd>' in html
    with console_running(tmp_path / "gone", None) as base:                                    # no status file at all
        html = dom_of(base + "/", 12000)
        assert '<dd id="link-state" class="badge status-disconnected">DISCONNECTED</dd>' in html and "DATA CONNECTION DEGRADED" in html


def test_console_dt_labels_timestamps_as_utc_and_has_footer_and_status_link(tmp_path):
    with console_running(tmp_path, _status(updated_utc="2026-09-02T19:05:12.654321+00:00")) as base:
        html = dom_of(base + "/")
    assert "<dt>UPDATED (UTC)</dt>" in html and '<dd id="updated">19:05:12</dd>' in html and 'id="nav-status" href="/status.html"' in html and ":8090" not in html
    assert "Felipe Fridman" in html and "GitHub project" in html and "build" in html


# ------------------------------------------------------------------ responsive layout (no horizontal overflow at 1024 / 768 / 480)

@pytest.mark.parametrize("width", [1024, 768, 480])
def test_pages_do_not_overflow_horizontally(tmp_path, width):
    for name in ("common.js", "styles.css"):
        shutil.copyfile(CONSOLE / name, tmp_path / name)
    for page in ("observe", "align", "calibrate", "status"):
        shutil.copyfile(CONSOLE / f"{page}.html", tmp_path / f"{page}.html")
        shutil.copyfile(CONSOLE / f"{page}.js", tmp_path / f"{page}.js")
    harness = f"""<!doctype html><html><body style="margin:0"><pre id="__result"></pre>
<script>
const pages = ["observe", "align", "calibrate", "status"]; const out = {{}};
let pending = pages.length;
for (const p of pages) {{
  const f = document.createElement("iframe"); f.style.cssText = "width:{width}px;height:900px;border:0"; f.src = p + ".html";
  f.onload = () => setTimeout(() => {{ const d = f.contentDocument.documentElement; out[p] = [d.scrollWidth, {width}];
    if (--pending === 0) document.getElementById("__result").textContent = JSON.stringify(out); }}, 1500);
  document.body.appendChild(f);
}}
</script></body></html>"""
    (tmp_path / "measure.html").write_text(harness)
    out = result_of(chromium(tmp_path / "measure.html", 20000))
    for page, (scroll, avail) in out.items():
        assert scroll <= avail + 1, f"{page} overflows horizontally at {avail}px (scrollWidth {scroll})"


# ------------------------------------------------------------------ status indicator: centred on the page everywhere

@pytest.mark.parametrize("width", [1280, 390])
def test_status_indicator_is_centred_on_every_page_and_stays_centred_when_its_content_changes(tmp_path, width):
    """The header status indicator (.hdr-status, MONITOR's <dl>) sits on its own row, its visible chips centred
    on the page at desktop and phone widths, and still centred after its content grows (navigation re-renders
    it, values change length)."""
    shutil.copytree(CONSOLE, tmp_path / "console")
    pages = ["index", "pipeline", "observe", "align", "calibrate", "reduce", "science", "status"]
    harness = f"""<!doctype html><html><body style="margin:0"><pre id="__result"></pre>
<script>
const pages = {json.dumps(pages)}; const out = {{}};
let pending = pages.length;
function centre(doc) {{
  const strip = doc.querySelector(".hdr-status");
  if (!strip || !strip.children.length) return null;
  let l = Infinity, r = -Infinity;
  for (const c of strip.children) {{ const b = c.getBoundingClientRect(); l = Math.min(l, b.left); r = Math.max(r, b.right); }}
  return [(l + r) / 2, doc.documentElement.clientWidth / 2];
}}
for (const p of pages) {{
  const f = document.createElement("iframe"); f.style.cssText = "width:{width}px;height:900px;border:0"; f.src = "console/" + p + ".html";
  f.onload = () => setTimeout(() => {{
    const d = f.contentDocument; const before = centre(d);
    const strip = d.querySelector(".hdr-status");
    if (strip) {{ const extra = d.createElement(strip.tagName === "DL" ? "div" : "span"); extra.className = "chip";
                  extra.textContent = "OBSERVATION RUNNING · a much longer value than before"; strip.appendChild(extra); }}
    out[p] = {{ before, after: centre(d) }};
    if (--pending === 0) document.getElementById("__result").textContent = JSON.stringify(out); }}, 1500);
  document.body.appendChild(f);
}}
</script></body></html>"""
    (tmp_path / "measure.html").write_text(harness)
    out = result_of(chromium(tmp_path / "measure.html", 25000, width=width + 40))
    assert set(out) == set(pages)
    for page, m in out.items():
        for when in ("before", "after"):
            assert m[when] is not None, f"{page}: no status indicator"
            got, mid = m[when]
            assert abs(got - mid) <= 2, f"{page} at {width}px ({when} content change): indicator centre {got:.1f} vs page centre {mid:.1f}"


# ------------------------------------------------------------------ long run: a dashboard left open must not grow

TIMER_TRACKING = r"""
(function () {
  const live = new Set();
  const st = window.setTimeout, ct = window.clearTimeout, si = window.setInterval, ci = window.clearInterval;
  window.setTimeout = (fn, ms, ...a) => { const id = st(() => { live.delete(id); fn(...a); }, ms); live.add(id); return id; };
  window.clearTimeout = (id) => { live.delete(id); return ct(id); };
  window.setInterval = (fn, ms, ...a) => { const id = si(fn, ms, ...a); live.add(id); return id; };
  window.clearInterval = (id) => { live.delete(id); return ci(id); };
  window.__liveTimers = () => live.size;
})();
"""

METRICS = r"""
const metrics = () => ({ nodes: document.getElementsByTagName("*").length, timers: window.__liveTimers(), calls: window.__calls.length,
                         heap: performance.memory ? performance.memory.usedJSHeapSize : 0 });
"""


def test_observe_and_status_pages_open_for_30_minutes_do_not_grow(tmp_path):
    routes = TIMER_TRACKING + OBSERVE_ROUTES + "\nwindow.__state = RUNNING;\n"
    driver = METRICS + r"""
await sleep(60000); const m1 = metrics();
await sleep(29 * 60000); const m2 = metrics();
out.m1 = m1; out.m2 = m2; out.rate_first = m1.calls / 1; out.rate_rest = (m2.calls - m1.calls) / 29;
out.summaryLen = $("run-summary").textContent.length;
""".strip()
    out = run_page(tmp_path, "observe", routes, driver, budget=1_900_000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    m1, m2 = out["m1"], out["m2"]
    assert abs(m2["nodes"] - m1["nodes"]) <= 5                                     # DOM does not grow
    assert m1["timers"] == m2["timers"] and m2["timers"] <= 12                     # no leaked / duplicated timers
    assert 20 <= out["rate_rest"] <= 80 and abs(out["rate_rest"] - out["rate_first"]) <= 12   # steady polling (run every 2 s + health every 5 s), no runaway
    if m1["heap"]:
        assert m2["heap"] < 3 * m1["heap"] + 5_000_000                             # no linear heap growth over 29 more minutes
    routes2 = TIMER_TRACKING + r"""
window.__routes["GET /api/system/health"] = () => ({ body: HEALTH });
"""
    out2 = run_page(tmp_path / "s", "status", routes2, driver.replace('$("run-summary").textContent.length', "0"), budget=1_900_000) if (tmp_path / "s").mkdir() is None else None
    assert out2["errors"] == [] and abs(out2["m2"]["nodes"] - out2["m1"]["nodes"]) <= 3 and out2["m1"]["timers"] == out2["m2"]["timers"]
    assert 10 <= out2["rate_rest"] <= 14                                          # status page: one poller at 5 s = 12/min, nothing else


def test_field_console_left_open_for_30_minutes_keeps_two_timers_and_a_stable_dom(tmp_path):
    with console_running(tmp_path, _status()) as base:
        probe = tmp_path / "probe.html"
        html = (tmp_path / "public" / "index.html").read_text()
        probe_js = TIMER_TRACKING + r"""
window.__fetches = 0; const _f = window.fetch; window.fetch = (...a) => { window.__fetches++; return _f(...a); };
"""
        html = html.replace("<body", "<body", 1).replace("<head>", "<head><script>" + probe_js + "</script>", 1)
        html = html.replace("</body>", METRICS + r"""
<pre id="__result"></pre><script>(async () => { const out = {}; const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
 const m = () => ({ nodes: document.getElementsByTagName("*").length, timers: window.__liveTimers(), fetches: window.__fetches, heap: performance.memory ? performance.memory.usedJSHeapSize : 0 });
 await sleep(60000); out.m1 = m(); await sleep(29 * 60000); out.m2 = m(); document.getElementById("__result").textContent = JSON.stringify(out); })();</script></body>""".replace(METRICS, ""), 1)
        (tmp_path / "public" / "probe.html").write_text(html)
        dom = dom_of(base + "/probe.html", 1_900_000)
    out = result_of(dom)
    m1, m2 = out["m1"], out["m2"]
    assert abs(m2["nodes"] - m1["nodes"]) <= 5 and m1["timers"] == m2["timers"] and m2["timers"] <= 8
    per_min = (m2["fetches"] - m1["fetches"]) / 29
    assert 25 <= per_min <= 60                                                       # ~30 polls/min at 2 s (+ optional artifacts): no runaway polling


# ------------------------------------------------------------------ CALIBRATE wizard: Alt/Az preview of the HI plan
# The facts come from the real backend function (calibration_engine.hi_plan_preview.build_preview) applied to a plan of
# the exact shape the wizard saves, so the page is tested against what the server really sends.

def _wizard_plan_routes():
    import copy
    from calibration_engine.hi_plan_preview import build_preview
    from tests.test_hi_plan_preview import PLAN
    plan2 = copy.deepcopy(PLAN)
    plan2["generated_utc"] = "2026-10-02T02:10:00.000Z"
    plan2["candidates"]["HI_ALTO"]["altitude_check"].update(worst_case_altitude_deg=19.5, clears=False)

    def facts(plan):
        return {"session_id": "WIZARD-T", "session_dir": "data/calibration/WIZARD-T", "action": "plan_hi", "step": "PLAN_HI",
                "config": {"n_captures": 5, "stabilize_seconds": 20}, "fifty_ohm": {"status": "DONE"}, "hi_references": {},
                "hi_plan": plan, "hi_plan_preview": build_preview(plan)}
    return CAL_ROUTES + r"""
window.__routes["GET /api/ops/calibrate/wizard_defaults"] = { body: { ok: true, data: { center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2 } } };
window.__routes["GET /api/ops/jobs"] = { body: { ok: true, data: [{ job_id: "J1", stage: "calibrate_wizard" }] } };
window.__routes["GET /api/ops/job/J1"] = { body: { ok: true, data: { job_id: "J1", state: "EXITED", facts: %s } } };
window.__routes["POST /api/ops/start/calibrate_wizard"] = { body: { ok: true, data: { job_id: "J2" } } };
window.__routes["GET /api/ops/job/J2"] = { body: { ok: true, data: { job_id: "J2", state: "EXITED", facts: %s } } };
""" % (json.dumps(facts(PLAN)), json.dumps(facts(plan2)))


def test_calibrate_wizard_plan_preview_shows_order_limits_reference_and_no_obstacle_claim(tmp_path):
    driver = r"""
const $w = (id) => document.getElementById(id);
await until(() => !$w("wz-plan-preview").hidden && document.querySelectorAll("#wz-plan-preview-table tbody tr").length === 2, 5000);
const rows = () => [...document.querySelectorAll("#wz-plan-preview-table tbody tr")].map((tr) => [...tr.children].map((td) => td.textContent));
const painted = () => { const c = $w("wz-plan-sky"); const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; };
out.first = { rows: rows(), ref: $w("wz-plan-preview-ref").textContent, limits: $w("wz-plan-preview-limits").textContent,
              horizon: $w("wz-plan-preview-horizon").textContent, painted: painted(), warnRows: document.querySelectorAll("#wz-plan-preview-table tr.row-warn").length };
$w("wz-replan-hi").click();
await until(() => $w("wz-plan-preview-ref").textContent.includes("02:10:00"), 5000);
out.second = { rows: rows(), ref: $w("wz-plan-preview-ref").textContent, limits: $w("wz-plan-preview-limits").textContent,
               warnRows: document.querySelectorAll("#wz-plan-preview-table tr.row-warn").length };
"""
    out = run_page(tmp_path, "calibrate", _wizard_plan_routes(), driver, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    first = out["first"]
    assert [r[:4] for r in first["rows"]] == [["1", "HI_ALTO", "225.7°", "30.3°"], ["2", "HI_BAJO", "124.9°", "69.8°"]]
    assert all(r[5] == "OK" for r in first["rows"]) and first["warnRows"] == 0
    assert "2026-10-02T01:35:38.044Z" in first["ref"] and "Both zones within" in first["limits"]
    assert first["horizon"].startswith("OBSTACLES NOT EVALUATED")
    assert first["painted"] > 1000                                    # the sky view really drew something
    second = out["second"]                                            # RE-PROPOSE: the preview follows the new plan
    assert "02:10:00" in second["ref"] and second["warnRows"] == 1
    assert second["rows"][0][5].startswith("OUT OF LIMITS") and "WARNING" in second["limits"]


def test_calibrate_wizard_map_draws_the_hi4pi_layer_of_the_plan_instant_with_its_scale_and_active_point(tmp_path):
    """The preview carries ALIGN's HI4PI layer computed at the plan instant (preview.sky): the page draws it under
    the zones, shows the colour scale and names the order and the active zone; in READY_HI_BAJO the same map
    marks HI_ALTO as measured and HI_BAJO as active."""
    import copy
    from calibration_engine.hi_plan_preview import build_preview
    from tests.test_hi_plan_preview import PLAN
    sky = {"hi4pi_grid": {"n": 4, "values_1e20cm2": [None, 2, 3, None, 2, 10, 20, 3, 4, 30, 40, 5, None, 5, 6, None],
                          "value_range_1e20cm2": [2, 40]}, "obstime_utc": PLAN["generated_utc"], "beam_fwhm_deg": 20.0}

    def facts(step, measured):
        pv = build_preview(PLAN, current_label="HI_BAJO" if step == "READY_HI_BAJO" else None, measured=measured)
        pv["sky"] = sky
        return {"session_id": "WIZARD-T", "session_dir": "data/calibration/WIZARD-T", "action": "plan_hi", "step": step,
                "config": {"n_captures": 5, "stabilize_seconds": 20}, "fifty_ohm": {"status": "DONE"},
                "hi_references": {m: {} for m in measured}, "hi_plan": copy.deepcopy(PLAN), "hi_plan_approved_utc": "x",
                "hi_plan_preview": pv}
    routes = CAL_ROUTES + r"""
window.__routes["GET /api/ops/calibrate/wizard_defaults"] = { body: { ok: true, data: { center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2 } } };
window.__routes["GET /api/ops/jobs"] = { body: { ok: true, data: [{ job_id: "J1", stage: "calibrate_wizard" }] } };
window.__routes["GET /api/ops/job/J1"] = () => window.__job;
window.__job = { body: { ok: true, data: { job_id: "J1", state: "EXITED", facts: %s } } };
window.__ready = { body: { ok: true, data: { job_id: "J1", state: "EXITED", facts: %s } } };
""" % (json.dumps(facts("PLAN_HI", [])), json.dumps(facts("READY_HI_BAJO", ["HI_ALTO"])))
    driver = r"""
const $w = (id) => document.getElementById(id);
await until(() => !$w("wz-plan-preview").hidden && !$w("wz-plan-sky-legend").hidden, 5000);
const c = $w("wz-plan-sky");
out.square = Math.abs(c.clientWidth - c.clientHeight) <= 1;
const cx = c.getBoundingClientRect(), pr = c.parentElement.getBoundingClientRect();
out.centred = Math.abs((cx.left + cx.right) / 2 - (pr.left + pr.right) / 2) <= 1;
out.planNote = $w("wz-plan-sky-note").textContent;
"""
    out = run_page(tmp_path, "calibrate", routes, driver, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["square"] and out["centred"]
    assert "HI4PI layer at the plan instant 2026-10-02T01:35:38.044Z (beam 20°)" in out["planNote"]
    assert "order: 1 HI_ALTO → 2 HI_BAJO" in out["planNote"] and "active" not in out["planNote"]

    ready_driver = r"""
const $w = (id) => document.getElementById(id);
await until(() => !$w("wz-step-ready-hi").hidden && !$w("wz-ready-sky-legend").hidden, 5000);
out.readyNote = $w("wz-ready-sky-note").textContent;
"""
    out = run_page(tmp_path, "calibrate", routes + "\nwindow.__job = window.__ready;\n", ready_driver, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert "order: 1 HI_ALTO (measured) → 2 HI_BAJO · active: HI_BAJO" in out["readyNote"]


def _wizard_state(step, **extra):
    return {"session_id": "WIZ-1", "session_dir": "data/calibration/WIZ-1", "step": step,
            "config": {"n_captures": 4, "stabilize_seconds": 0}, "fifty_ohm": {"status": "PENDING_CAPTURE"},
            "hi_references": {}, **extra}


def test_calibrate_wizard_recovers_an_interrupted_session_and_shows_what_was_kept(tmp_path):
    """A capture STOPPED by the operator prints no state: on reload the page still finds the session (the job's
    output_dir), reads its REAL state with "status" and says what happened and which files were kept."""
    state = _wizard_state("STABILIZE_50R", last_interruption={"step": "AMBIENT_50R", "kind": "STOPPED_BY_OPERATOR",
                          "utc": "2026-10-06T00:10:00+00:00", "error": "", "files_on_disk": ["capture_000.h5", "capture_001.h5"]})
    routes = CAL_ROUTES + r"""
window.__routes["GET /api/ops/calibrate/wizard_defaults"] = { body: { ok: true, data: { center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2 } } };
window.__routes["GET /api/ops/jobs"] = { body: { ok: true, data: [{ job_id: "J9", stage: "calibrate_wizard" }] } };
window.__routes["GET /api/ops/job/J9"] = { body: { ok: true, data: { job_id: "J9", state: "EXITED", facts: null, stopped_by_operator: true,
  output_dir: "data/calibration/WIZ-1", params: { action: "capture_50r", session_dir: "data/calibration/WIZ-1" } } } };
window.__routes["POST /api/ops/start/calibrate_wizard"] = (b) => ({ body: { ok: true, data: { job_id: "S1" } } });
window.__routes["GET /api/ops/job/S1"] = { body: { ok: true, data: { job_id: "S1", state: "EXITED", facts: %s } } };
""" % json.dumps(state)
    driver = r"""
const $w = (id) => document.getElementById(id);
await until(() => !$w("wizard-active").hidden && !$w("wz-50r-failure").hidden, 5000);
out.banner = $w("wz-50r-failure").textContent;
out.globalBannerHidden = $w("wz-interruption").hidden;            // same interruption: shown once, in the step
out.statusCall = window.__calls.filter((c) => c.key === "POST /api/ops/start/calibrate_wizard").map((c) => c.body.params);
out.stopShown = !$w("wz-running").hidden;
"""
    out = run_page(tmp_path, "calibrate", routes, driver, budget=15000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["statusCall"] == [{"action": "status", "session_dir": "data/calibration/WIZ-1"}]
    assert "LAST AMBIENT_50R CAPTURE STOPPED by the operator" in out["banner"] and "2 file(s) kept" in out["banner"]
    assert out["globalBannerHidden"] is True
    assert out["stopShown"] is False


def test_calibrate_wizard_running_step_has_stop_and_abort_stops_it_first(tmp_path):
    """While a capture runs on the server the page shows STOP; ABORT first stops that job (SIGINT via
    /api/ops/stop), waits for it to end, then aborts the session and returns to the setup form."""
    routes = CAL_ROUTES + r"""
window.__routes["GET /api/ops/calibrate/wizard_defaults"] = { body: { ok: true, data: { center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2 } } };
window.__routes["GET /api/ops/jobs"] = { body: { ok: true, data: [{ job_id: "J9", stage: "calibrate_wizard" }] } };
window.__j9 = "RUNNING";
window.__routes["GET /api/ops/job/J9"] = () => ({ body: { ok: true, data: { job_id: "J9", state: window.__j9, facts: window.__j9 === "RUNNING" ? null : null,
  output_dir: "data/calibration/WIZ-1", params: { action: "capture_50r", session_dir: "data/calibration/WIZ-1" } } } });
window.__routes["POST /api/ops/stop/J9"] = () => { window.__j9 = "EXITED"; return { body: { ok: true, data: { job_id: "J9" } } }; };
window.__routes["POST /api/ops/start/calibrate_wizard"] = (b) => ({ body: { ok: true, data: { job_id: b.params.action === "abort" ? "A1" : "S1" } } });
window.__routes["GET /api/ops/job/A1"] = { body: { ok: true, data: { job_id: "A1", state: "EXITED", facts: %s } } };
window.__routes["GET /api/ops/job/S1"] = { body: { ok: true, data: { job_id: "S1", state: "EXITED", facts: %s } } };
""" % (json.dumps(_wizard_state("ABORTED", aborted=True)), json.dumps(_wizard_state("STABILIZE_50R")))
    driver = r"""
const $w = (id) => document.getElementById(id);
await until(() => !$w("wz-running").hidden, 5000);
out.runningNote = $w("wz-running-note").textContent;
document.getElementById("wizard-active").hidden = false;
$w("wz-abort").click();
await until(() => !$w("wizard-setup").hidden && $w("wizard-active").hidden, 8000);
const keys = window.__calls.map((c) => c.key);
out.order = keys.filter((k) => k === "POST /api/ops/stop/J9" || k === "POST /api/ops/start/calibrate_wizard");
out.lastStart = window.__calls.filter((c) => c.key === "POST /api/ops/start/calibrate_wizard").pop().body.params;
out.stopHidden = $w("wz-running").hidden;
"""
    out = run_page(tmp_path, "calibrate", routes, driver, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert "capture_50r (J9)" in out["runningNote"]
    assert out["order"][0] == "POST /api/ops/stop/J9" and out["lastStart"] == {"action": "abort", "session_dir": "data/calibration/WIZ-1"}
    assert out["stopHidden"] is True


def test_calibrate_wizard_failed_50r_capture_is_shown_in_the_step_and_never_silently_reset(tmp_path):
    """The 2026-10-07 incident, as the page saw it: CAPTURE NOW -> the job exits 1 (rtl_tcp reset) -> the session is
    still STABILIZE_50R. The step itself must say the capture FAILED, with the reason and the job; the button turns
    into RETRY; the stabilization countdown is not restarted; nothing is re-run automatically."""
    li = {"step": "AMBIENT_50R", "kind": "FAILED", "utc": "2026-10-07T00:14:14+00:00", "files_on_disk": [],
          "error": "SDRDisconnected: SDR_DISCONNECTED: rtl_tcp socket error: [Errno 104] Connection reset by peer",
          "job_id": "CALIBRATE_WIZARD-20261007-001408-816a"}
    before = _wizard_state("STABILIZE_50R", fifty_ohm={"status": "PENDING_CAPTURE", "confirmed_utc": "2026-10-07T00:12:32Z"},
                           config={"n_captures": 5, "stabilize_seconds": 20})
    after = dict(before, last_interruption=li)
    routes = CAL_ROUTES + r"""
window.__routes["GET /api/ops/calibrate/wizard_defaults"] = { body: { ok: true, data: { center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2 } } };
window.__routes["GET /api/ops/jobs"] = { body: { ok: true, data: [{ job_id: "J0", stage: "calibrate_wizard_state" }] } };
window.__routes["GET /api/ops/job/J0"] = { body: { ok: true, data: { job_id: "J0", state: "EXITED", facts: %s } } };
window.__routes["POST /api/ops/start/calibrate_wizard"] = (b) => ({ body: { ok: true, data: { job_id: b.params.action === "capture_50r" ? "CAP" : "STATUS" } } });
window.__routes["GET /api/ops/job/CAP"] = { body: { ok: true, data: { job_id: "CAP", state: "EXITED", exit_code: 1, facts: null,
  detail: "exit 1; AMBIENT_50R capture FAILED (step stays STABILIZE_50R, nothing recorded as done): SDRDisconnected: ...", output_dir: "data/calibration/WIZ-1" } } };
window.__routes["GET /api/ops/job/STATUS"] = { body: { ok: true, data: { job_id: "STATUS", state: "EXITED", facts: %s } } };
""" % (json.dumps(before), json.dumps(after))
    driver = r"""
const $w = (id) => document.getElementById(id);
await until(() => !$w("wz-step-stabilize").hidden, 5000);
out.countdownBefore = $w("wz-countdown").textContent;
$w("wz-capture").click();
await until(() => !$w("wz-50r-failure").hidden, 6000);
out.failure = $w("wz-50r-failure").textContent;
out.topError = document.getElementById("error-banner").textContent;
out.button = $w("wz-capture").textContent;
out.countdownAfter = $w("wz-countdown").textContent;
out.step = !$w("wz-step-stabilize").hidden;
out.captureStarts = window.__calls.filter((c) => c.key === "POST /api/ops/start/calibrate_wizard" && c.body.params.action === "capture_50r").length;
await new Promise((r) => setTimeout(r, 1500));
out.captureStartsLater = window.__calls.filter((c) => c.key === "POST /api/ops/start/calibrate_wizard" && c.body.params.action === "capture_50r").length;
"""
    out = run_page(tmp_path, "calibrate", routes, driver, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert "suggested stabilization" in out["countdownBefore"]
    assert "LAST AMBIENT_50R CAPTURE FAILED" in out["failure"] and "job CALIBRATE_WIZARD-20261007-001408-816a" in out["failure"]
    assert "Connection reset by peer" in out["failure"]
    assert "capture failed: exit 1; AMBIENT_50R capture FAILED" in out["topError"]
    assert out["button"].startswith("RETRY CAPTURE") and out["step"] is True
    assert "NOT retried automatically" in out["countdownAfter"]
    assert out["captureStarts"] == out["captureStartsLater"] == 1


def test_reduce_profiles_with_the_same_file_name_are_told_apart_and_picked_from_the_server_explorer(tmp_path):
    """Every wizard session writes observe_profile/calibration_profile_v1.json, so REDUCE listed two identical names.
    The list now names each profile's session, creation time and Hz/sps/dB; BROWSE ALMITA SERVER opens the shared
    explorer, compares against the campaign's validated sample point and SELECT sets the profile REDUCE will use."""
    a = "data/calibration/WIZARD-20261002-013321-160933/observe_profile/calibration_profile_v1.json"
    b = "data/calibration/WIZARD-20261007-010706-387765/observe_profile/calibration_profile_v1.json"
    routes = r"""
window.__routes["GET /api/ops/reduce/captures"] = { body: { ok: true, data: [] } };
window.__routes["GET /api/ops/reduce/campaigns"] = { body: { ok: true, data: { campaigns: [{ campaign_dir: "data/mosaic/C1", completeness: "COMPLETA",
  points_usable: 676, points_expected: 676, session_label: "20261007-02:00:15", name: "C1" }], no_data: [] } } };
window.__routes["GET /api/ops/campaigns"] = { body: { ok: true, data: { profiles: [
  { path: "%(b)s", name: "calibration_profile_v1.json", session: "WIZARD-20261007-010706-387765", created_utc: "2026-10-07T01:20:11+00:00", center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2 },
  { path: "%(a)s", name: "calibration_profile_v1.json", session: "WIZARD-20261002-013321-160933", created_utc: "2026-10-02T01:39:58+00:00", center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2 } ] } } };
window.__routes["GET /api/ops/reduce/inspect_campaign"] = { body: { ok: true, data: { campaign_id: "ALMITA-OBSERVE", session: "20261007-02:00:15",
  completeness: "COMPLETA", points_usable: 676, points_expected: 676, points_deferred: 0, points_missing_or_invalid: 0, grid: null, observer: { name: "Santiago" },
  sample_point_index: 1, sample_point_metadata: { center_frequency_hz_nominal: 1420405752, sample_rate_hz: 2400000, gain_db_requested: 40.2, topology: "ANTENNA_TO_LNA_FILTER_CABLING_TO_RTL_SDR" } } } };
window.__routes["GET /api/ops/reduce/campaign_calibration_preview"] = { body: { ok: true, data: { all_compatible: true, counts: { COMPATIBLE: 676 }, total_accepted_points: 676, points: [] } } };
window.__routes["GET /api/observe/calibration-profiles/browse"] = { body: { ok: true, data: { disk: "server", root: "data/calibration",
  dir: "data/calibration/WIZARD-20261007-010706-387765/observe_profile", parent: "data/calibration/WIZARD-20261007-010706-387765",
  breadcrumbs: [{ name: "calibration", path: "data/calibration" }], dirs: [],
  files: [{ type: "profile", name: "calibration_profile_v1.json", dir: "data/calibration/WIZARD-20261007-010706-387765/observe_profile", path: "%(b)s",
            valid: true, selectable: true, summary: { center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2, created_utc: "2026-10-07T01:20:11+00:00" },
            compatibility: { status: "COMPATIBLE", reason: "frequency, sample rate, gain and topology match" } }] } } };
""" % {"a": a, "b": b}
    driver = r"""
$("mode-campaign").click();
await until(() => $("in-campaign").options.length > 1, 4000);
$("in-campaign").value = "data/mosaic/C1"; $("in-campaign").dispatchEvent(new Event("change"));
await until(() => !$("profile-panel").hidden, 4000);
out.options = [...$("in-profile").options].map((o) => o.textContent);
$("rd-cal-browse").click();
await until(() => !$("rd-cal-picker").hidden, 4000);
out.browseQuery = window.__calls.filter((c) => c.key === "GET /api/observe/calibration-profiles/browse").length;
[...document.querySelectorAll("#rd-cal-table tbody button")].find((x) => x.textContent === "SELECT").click();
await until(() => $("in-profile").value !== "", 3000);
out.value = $("in-profile").value; out.selected = $("rd-cal-selected").textContent; out.pickerHidden = $("rd-cal-picker").hidden;
"""
    out = run_page(tmp_path, "reduce", routes, driver, budget=15000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["options"][1].startswith("WIZARD-20261007-010706-387765 · created") and "1420405752 Hz · 2400000 sps · 40.2 dB" in out["options"][1]
    assert out["options"][2].startswith("WIZARD-20261002-013321-160933 · created") and out["options"][1] != out["options"][2]
    assert out["value"] == b and out["pickerHidden"] is True
    assert "SERVER directory: data/calibration/WIZARD-20261007-010706-387765/observe_profile/" in out["selected"]


def test_observe_shows_a_session_that_ended_on_its_own_as_completed_not_running(tmp_path):
    """2026-10-08 report: the session finished at 05:44 (676/676) but OBSERVE kept saying RUNNING, because the
    orchestrator label is only closed by STOP. The page acts on the server's effective_state."""
    status = {"orchestrator": {"orchestrator_state": "RUNNING", "effective_state": "COMPLETED", "session_id": "20261007_020101",
                               "capture_pid": 647203, "quicklook_pid": 647431, "capture_process_alive": False,
                               "effective_note": "the capture ended on its own (COMPLETED at 2026-10-07T05:44:20Z); the orchestrator label RUNNING is only closed by STOP, so it was never updated - nothing is running"},
              "current_session": {"session_id": "20261007_020101", "state": "COMPLETED", "session_name": "ALMITA-OBSERVE", "point_current": 676,
                                  "points_total": 676, "points_success": 676, "points_failed": 0, "points_deferred": 0,
                                  "started_utc": "2026-10-07T02:01:01Z", "updated_utc": "2026-10-07T05:44:20Z"}}
    routes = OBSERVE_ROUTES + '\nwindow.__routes["GET /api/observe/status"] = { body: %s };\n' % json.dumps(status)
    driver = r"""
await until(() => !$("run-status").hidden && $("run-badge").textContent !== "", 4000);
out.badge = $("run-badge").textContent; out.kind = $("run-kind").textContent; out.summary = $("run-summary").textContent;
out.stopDisabled = $("btn-stop").disabled;
"""
    out = run_page(tmp_path, "observe", routes, driver, budget=12000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["badge"] == "COMPLETED" and out["kind"].startswith("LAST RUN — SUCCESS")
    assert "orchestrator_state: RUNNING   →   effective: COMPLETED" in out["summary"] and "nothing is running" in out["summary"]
    assert "STALE" not in out["summary"] and out["stopDisabled"] is True


def test_gain_pilot_capture_failure_is_shown_in_the_panel(tmp_path):
    """2026-10-08: CAPTURE PILOT NOW got a 404 (start route too short for observe_gain_pilot_capture) and the page
    showed nothing - the handler had no catch. Any refused/failed pilot action is now reported inside the panel."""
    facts = {"step": "PREPARE_HIGH", "observation_name": "WEBTEST", "estimated_extra_points": 2, "estimated_duration_s": 36,
             "min_elevation_deg": 5, "initial_gain_db": 40.2,
             "candidates": {"HIGH": {"label": "HIGH", "point_id": 24, "ra_hours": 2.9511, "dec_deg": -18.174, "predicted_altitude_deg": 13.4,
                                     "real_time_altitude_check": {"worst_case_altitude_deg": 10.3, "margin_deg": 5.3}, "n_hi_1e20cm2": 2.783}}}
    routes = OBSERVE_ROUTES + r"""
window.__routes["POST /api/ops/start/observe_gain_pilot_admin"] = { body: { ok: true, data: { job_id: "GPS" } } };
window.__routes["GET /api/ops/job/GPS"] = { body: { ok: true, data: { job_id: "GPS", state: "EXITED", facts: %s } } };
window.__routes["POST /api/ops/start/observe_gain_pilot_capture"] = { status: 404, body: { ok: false, error: "not found" } };
""" % json.dumps(facts)
    driver = r"""
$("observe-form").requestSubmit();
await until(() => !$("gain-pilot-panel").hidden && !$("gp-step-prepare").hidden, 6000);
out.errorBefore = $("gp-error").hidden;
$("gp-move-confirm").value = "MOVE";
$("gp-capture-btn").click();
await until(() => !$("gp-error").hidden, 4000);
out.error = $("gp-error").textContent;
"""
    out = run_page(tmp_path, "observe", routes, driver, budget=15000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errorBefore"] is True                       # a silent status lookup never shows a false error
    assert out["error"].startswith("capture_high FAILED") and "not found" in out["error"]


def test_gain_pilot_approved_gain_replans_the_grid_and_prepares_one_recheck_instead_of_looping(tmp_path):
    """2026-10-08 report: gain pilot READY at 42.1 dB, grid planned at 40.2 -> START said re-plan; re-planning made a
    new grid whose pilot panel was NOT PLANNED, and the new plan was still 40.2 (START would have ignored the pilot)."""
    ready = {"step": "READY", "verified_gain_db": 42.1, "grid_config_hash": "HASH-A", "observation_name": "WEBTEST",
             "estimated_extra_points": 2, "estimated_duration_s": 36, "candidates": {}, "config": {"final_check_enabled": False}}
    routes = OBSERVE_ROUTES + r"""
window.__planN = 0;
window.__routes["POST /api/observe/plan"] = (body) => {
  window.__planN += 1;
  const second = window.__planN > 1;
  return { delay: 50, body: { ...PLAN, observation_config_sha256: second ? "HASH-B" : "HASH-A",
    grid_session_dir: second ? "data/mosaic/WEBTEST-B" : "data/mosaic/WEBTEST-A",
    _resolved_plan_path: second ? "data/mosaic/WEBTEST-B/observation_resolved.json" : "data/mosaic/WEBTEST-A/observation_resolved.json",
    main: { ...PLAN.main, gain_db: body.main.gain_db } } };
};
window.__routes["POST /api/ops/start/observe_gain_pilot_admin"] = (b) => ({ body: { ok: true, data: { job_id: b.params.session_dir.endsWith("-A") ? "GPA" : "GPB" } } });
window.__routes["GET /api/ops/job/GPA"] = { body: { ok: true, data: { job_id: "GPA", state: "EXITED", facts: %s } } };
window.__routes["GET /api/ops/job/GPB"] = { body: { ok: true, data: { job_id: "GPB", state: "EXITED", exit_code: 1, facts: null, detail: "no gain_pilot_state.json" } } };
""" % json.dumps(ready)
    driver = r"""
$("f-gain").value = 40.2;
$("observe-form").requestSubmit();
await until(() => !$("gp-step-ready").hidden && !$("gp-replan-row").hidden, 6000);
out.startBefore = $("btn-start").title;
out.replanLabel = $("gp-replan-btn").textContent;
$("gp-replan-btn").click();
await until(() => window.__planN === 2 && !$("gp-carry-note").hidden, 6000);
out.secondPlanGain = window.__calls.filter((c) => c.key === "POST /api/observe/plan").pop().body.main.gain_db;
out.carry = $("gp-carry-note").textContent; out.maxPilots = $("gp-max-pilots").value; out.gpGain = $("gp-gain").value;
out.startBlocked = [$("btn-start").disabled, $("btn-start").title];
out.gpError = $("gp-error").hidden;
$("gp-enabled").click();
await sleep(50);
out.startWithoutPilot = $("btn-start").disabled;
"""
    out = run_page(tmp_path, "observe", routes, driver, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert "verified 42.1 dB but the grid plan uses 40.2 dB" in out["startBefore"]
    assert out["replanLabel"] == "RE-PLAN GRID AT 42.1 dB"
    assert out["secondPlanGain"] == 42.1
    assert "re-planned at 42.1 dB" in out["carry"] and "data/mosaic/WEBTEST-A" in out["carry"]
    assert (out["maxPilots"], out["gpGain"]) == ("1", "42.1") and out["gpError"] is True
    assert out["startBlocked"][0] is True and "re-check it on this grid" in out["startBlocked"][1]
    assert out["startWithoutPilot"] is False


def test_gain_pilot_warns_before_approving_a_gain_the_quicklook_profile_does_not_cover(tmp_path):
    """2026-10-08: the pilot approved 42.1 dB with a 40.2 dB QUICKLOOK profile and the re-planned grid was BLOCKED
    only afterwards. GAIN_DECISION now asks the server (/validate, same compatibility as the PLAN preflight) and
    warns before APPROVE, listing profiles that already match; editing back to the profile's gain clears it."""
    decision = {"step": "GAIN_DECISION", "observation_name": "WEBTEST", "estimated_extra_points": 2, "estimated_duration_s": 36,
                "initial_gain_db": 40.2, "candidates": {}, "evaluations": {},
                "gain_recommendation": {"recommended_gain_db": 42.1, "current_gain_db": 40.2, "reason": "headroom"}}
    routes = OBSERVE_ROUTES + r"""
const P40 = "data/calibration/WIZARD-40/observe_profile/calibration_profile_v1.json";
const P42 = "data/calibration/WIZARD-42/observe_profile/calibration_profile_v1.json";
window.__routes["POST /api/observe/plan"] = () => ({ body: { ...PLAN, quicklook: { enabled: true, calibration_profile_path: P40 } } });
const gainOf = (url) => Number(new URL(url, "http://x").searchParams.get("gain_db"));
window.__routes["GET /api/observe/calibration-profiles/validate"] = (b, url) => ({ body: { ok: true, data: { path: P40, summary: { gain_db: 40.2 },
  compatibility: gainOf(url) === 40.2 ? { status: "COMPATIBLE", reason: "match" } : { status: "INCOMPATIBLE", reason: "gain " + gainOf(url) + " != 40.2" } } } });
window.__routes["GET /api/observe/calibration-profiles"] = (b, url) => ({ body: { ok: true, data: { profiles: [
  { path: P42, valid: true, compatibility: { status: gainOf(url) === 42.1 ? "COMPATIBLE" : "INCOMPATIBLE" } },
  { path: P40, valid: true, compatibility: { status: gainOf(url) === 40.2 ? "COMPATIBLE" : "INCOMPATIBLE" } } ] } } });
window.__routes["POST /api/ops/start/observe_gain_pilot_admin"] = { body: { ok: true, data: { job_id: "GPD" } } };
window.__routes["GET /api/ops/job/GPD"] = { body: { ok: true, data: { job_id: "GPD", state: "EXITED", facts: %s } } };
""" % json.dumps(decision)
    driver = r"""
$("observe-form").requestSubmit();
await until(() => !$("gp-step-decision").hidden && !$("gp-profile-warning").hidden, 6000);
out.warning = $("gp-profile-warning").textContent;
out.approveEnabled = !$("gp-approve-btn").disabled;
$("gp-approve-gain").value = "40.2"; $("gp-approve-gain").dispatchEvent(new Event("input"));
await until(() => $("gp-profile-warning").hidden, 4000);
out.clearedAt402 = $("gp-profile-warning").hidden;
"""
    out = run_page(tmp_path, "observe", routes, driver, budget=15000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    w = out["warning"]
    assert w.startswith("⚠ 42.1 dB does not match the QUICKLOOK calibration profile") and "INCOMPATIBLE: gain 42.1 != 40.2" in w
    assert "BLOCKED by \"Quicklook / calibration match\"" in w and "REFERENCE WIZARD with GAIN = 42.1 dB" in w
    assert "WIZARD-42/observe_profile/calibration_profile_v1.json" in w and "WIZARD-40/observe" not in w.split("already match")[1]
    assert out["approveEnabled"] is True                    # a warning, never a block
    assert out["clearedAt402"] is True


# ------------------------------------------------------------------ OBSERVE: calibration profile selector (server disk)

def test_observe_calibration_profile_explorer_browses_server_dirs_selects_and_rejects(tmp_path):
    """OBSERVE's profile selector is a file explorer of the ALMITA SERVER's data/calibration: breadcrumbs, folders,
    each profile with its frequency / sample rate / gain and compatibility; an INCOMPATIBLE or invalid file cannot
    be selected and says why; SELECT fills the path and shows the server directory and file name; a path from the
    browser's own disk is rejected. Typing stays possible (validated the same way)."""
    routes = OBSERVE_ROUTES + r"""
const GOOD = "data/calibration/WIZARD-A/observe_profile/calibration_profile_v1.json";
const VIEWS = {
  "": { disk: "server", root: "data/calibration", dir: "data/calibration", parent: null,
        breadcrumbs: [{ name: "calibration", path: "data/calibration" }],
        dirs: [{ type: "dir", name: "WIZARD-A", path: "data/calibration/WIZARD-A" }, { type: "dir", name: "WIZARD-OLD", path: "data/calibration/WIZARD-OLD" }], files: [] },
  "data/calibration/WIZARD-A": { disk: "server", root: "data/calibration", dir: "data/calibration/WIZARD-A", parent: "data/calibration",
        breadcrumbs: [{ name: "calibration", path: "data/calibration" }, { name: "WIZARD-A", path: "data/calibration/WIZARD-A" }],
        dirs: [{ type: "dir", name: "observe_profile", path: "data/calibration/WIZARD-A/observe_profile" }],
        files: [{ type: "other", name: "wizard_state.json", dir: "data/calibration/WIZARD-A", path: "data/calibration/WIZARD-A/wizard_state.json",
                  valid: false, selectable: false, error: "no .npz arrays next to it", summary: null, compatibility: null }] },
  "data/calibration/WIZARD-A/observe_profile": { disk: "server", root: "data/calibration", dir: "data/calibration/WIZARD-A/observe_profile",
        parent: "data/calibration/WIZARD-A",
        breadcrumbs: [{ name: "calibration", path: "data/calibration" }, { name: "WIZARD-A", path: "data/calibration/WIZARD-A" },
                      { name: "observe_profile", path: "data/calibration/WIZARD-A/observe_profile" }], dirs: [],
        files: [{ type: "profile", name: "calibration_profile_v1.json", dir: "data/calibration/WIZARD-A/observe_profile", path: GOOD,
                  valid: true, selectable: true, error: null,
                  summary: { center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2 },
                  compatibility: { status: "COMPATIBLE", reason: "frequency, sample rate, gain and topology match" } },
                { type: "profile", name: "old_profile.json", dir: "data/calibration/WIZARD-A/observe_profile", path: "data/calibration/WIZARD-A/observe_profile/old_profile.json",
                  valid: true, selectable: false, error: null,
                  summary: { center_frequency_hz: 1420405000, sample_rate_hz: 2400000, gain_db: 40.2 },
                  compatibility: { status: "INCOMPATIBLE", reason: "center frequency: 1420405752 != 1420405000" } }] } };
window.__routes["GET /api/observe/calibration-profiles/browse"] = () => ({ body: { ok: true, data: VIEWS[window.__nextDir || ""] } });
window.__routes["GET /api/observe/calibration-profiles/validate"] = () => window.__validateReply;
window.__validateReply = { body: { ok: true, data: { path: GOOD, disk: "server", valid: true,
  summary: { center_frequency_hz: 1420405752, sample_rate_hz: 2400000, gain_db: 40.2 },
  compatibility: { status: "COMPATIBLE", reason: "frequency, sample rate, gain and topology match" } } } };
"""
    driver = r"""
await until(() => $("f-freq").value !== "", 3000);
const rows = () => [...document.querySelectorAll("#f-ql-cal-table tbody tr")];
const text = () => rows().map((tr) => [...tr.children].slice(0, 3).map((td) => td.textContent));
const openRow = async (name, dir) => { window.__nextDir = dir; rows().find((tr) => tr.children[0].textContent === name).querySelector("button").click();
                                       await until(() => $("f-ql-cal-picker-note").textContent.startsWith(dir + "/"), 3000); };
$("f-ql-cal-browse").click();
await until(() => !$("f-ql-cal-picker").hidden, 3000);
out.root = { rows: text(), crumbs: $("f-ql-cal-crumbs").textContent, disk: document.querySelector(".fx-disk").textContent };
await openRow("WIZARD-A/", "data/calibration/WIZARD-A");
out.wizard = { rows: text(), disabled: rows().map((tr) => tr.querySelector("button").disabled) };
await openRow("observe_profile/", "data/calibration/WIZARD-A/observe_profile");
out.profiles = { rows: text(), disabled: rows().map((tr) => tr.querySelector("button").disabled), crumbs: $("f-ql-cal-crumbs").textContent,
                 warn: document.querySelectorAll("#f-ql-cal-table tr.row-warn").length };
rows().find((tr) => tr.children[0].textContent === "calibration_profile_v1.json").querySelector("button").click();
await until(() => $("f-ql-cal-status").textContent.includes("COMPATIBLE"), 3000);
out.afterSelect = { value: $("f-ql-cal").value, selected: $("f-ql-cal-selected").textContent, status: $("f-ql-cal-status").textContent,
                    hidden: $("f-ql-cal-picker").hidden };
window.__validateReply = { status: 400, body: { ok: false, error: "this looks like a path on the browser's computer - the ALMITA server cannot read it; pick a profile that exists on the server (BROWSE ALMITA SERVER)" } };
$("f-ql-cal").value = "C:\\fakepath\\calibration_profile_v1.json";
$("f-ql-cal").dispatchEvent(new Event("change"));
await until(() => $("f-ql-cal-status").textContent.startsWith("REJECTED"), 3000);
out.manual = { status: $("f-ql-cal-status").textContent, selected: $("f-ql-cal-selected").textContent, readOnly: $("f-ql-cal").readOnly };
"""
    out = run_page(tmp_path, "observe", routes, driver, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["root"]["disk"] == "ALMITA SERVER" and out["root"]["crumbs"] == "data/calibration"
    assert [r[0] for r in out["root"]["rows"]] == ["WIZARD-A/", "WIZARD-OLD/"]
    assert out["wizard"]["rows"][0][0] == "../" and ["wizard_state.json", "—", "REJECTED: no .npz arrays next to it"] in out["wizard"]["rows"]
    assert out["wizard"]["disabled"] == [False, False, True]
    assert out["profiles"]["crumbs"] == "data/calibration/WIZARD-A/observe_profile"
    assert out["profiles"]["rows"][1:] == [
        ["calibration_profile_v1.json", "1420405752 Hz · 2400000 sps · 40.2 dB", "COMPATIBLE: frequency, sample rate, gain and topology match"],
        ["old_profile.json", "1420405000 Hz · 2400000 sps · 40.2 dB", "REJECTED: center frequency: 1420405752 != 1420405000"]]
    assert out["profiles"]["disabled"] == [False, False, True] and out["profiles"]["warn"] == 1
    a = out["afterSelect"]
    assert a["value"] == "data/calibration/WIZARD-A/observe_profile/calibration_profile_v1.json" and a["hidden"] is True
    assert a["selected"] == ("SERVER directory: data/calibration/WIZARD-A/observe_profile/\nfile: calibration_profile_v1.json\n"
                             "1420405752 Hz · 2400000 sps · 40.2 dB")
    assert "valid server profile · COMPATIBLE" in a["status"]
    assert out["manual"]["status"].startswith("REJECTED: this looks like a path on the browser's computer")
    assert out["manual"]["selected"] == "" and out["manual"]["readOnly"] is False
