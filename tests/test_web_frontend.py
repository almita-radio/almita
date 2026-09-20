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
    const r = typeof h === "function" ? h(opts.body ? JSON.parse(opts.body) : null) : h;
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
    assert out["initial"][:4] == [True, True, True, "blocked by policy: real hardware alignment is not available from the web"]
    assert "blocked by policy" in out["initial"][4] and "plan first" in out["initial"][5] and out["header"] == "ALMITA ALIGN"
    assert out["planPosts"] == 1 and out["runEnabled"] is False and out["runPosts"] == 1 and out["runBtn"] == [True, "a simulation is running"] and out["simFlag"] is False
    assert out["afterJob"] == [False]
    assert "POST /api/align/plan/solar" in out["err"] and "unexpected error (OSError)" in out["err"] and "[request r7]" in out["err"]
    assert "Status request failed" in out["statusErr"] and "backend not reachable" in out["statusErr"]


def test_align_page_reload_adopts_a_running_session(tmp_path):
    routes = ALIGN_ROUTES + "\nwindow.__adopt = true;\n"
    driver = 'await sleep(1500); out.badge = $("mode-badge").textContent; out.body = $("mode-body").textContent; out.run = [$("btn-run-sim").disabled, $("btn-run-sim").title]; out.polls = window.__count("GET /api/align/session/SOLAR-1");'
    out = run_page(tmp_path, "align", routes, driver, budget=10000)
    assert out["badge"] == "RUNNING" and "run is in progress on the server" in out["body"] and out["run"] == [True, "a simulation is running"] and out["polls"] >= 1


CAL_ROUTES = r"""
window.__routes["GET /api/calibrate/status"] = () => ({ body: { ok: true, blocked: false, data: { calibration_level: "OPERATIONAL_RELATIVE", calibration_workflow_active: !!window.__active,
  resource: { status: "FREE", orchestrator_state: "PLANNED", detail: "free" }, receiver: { serial: %s, center_frequency_hz: { value: 1420405751.77, verification: "VERIFIED_BY_SERVICE_COMMAND_LINE" },
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
    assert any("1420.405752 MHz" in c and "VERIFIED BY SERVICE COMMAND LINE" in c for c in out["freq"]) and any("2.40 MS/s" in c for c in out["freq"])   # MHz with units + provenance tag
    assert out["real"] == [True, "blocked by policy: real capture is not wired to hardware from the web", True]
    assert out["runPosts"] == 1 and out["runBtn"] == [True, "a calibration simulation is running"]
    assert "No captures" in out["empty"]
    assert "NaN" not in out["nan"] and "undefined" not in out["nan"] and out["after"][0] is False


def test_calibrate_page_reload_adopts_the_running_job_and_status_polls_slowly(tmp_path):
    routes = CAL_ROUTES + "\nwindow.__active = true;\n"
    driver = 'await sleep(2500); out.polls = window.__count("GET /api/calibrate/session/CAL-1"); out.run = [$("btn-run-sim").disabled, $("btn-run-sim").title]; out.workflow = $("st-calibration-workflow").textContent; out.statusCalls = window.__count("GET /api/calibrate/status");'
    out = run_page(tmp_path, "calibrate", routes, driver, budget=10000)
    assert out["polls"] >= 1 and out["run"] == [True, "a calibration simulation is running"] and out["workflow"] == "SIMULATION RUNNING" and out["statusCalls"] <= 3


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
    assert "<dt>UPDATED (UTC)</dt>" in html and '<dd id="updated">19:05:12</dd>' in html and 'id="nav-status"' in html and ":8090/status.html" in html
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
