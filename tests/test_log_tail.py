"""BL-098 (operator 2026-11-06): every scrolling log box in the web console shows its last line - on first fill and
while the operator is at the bottom - holds still if they scrolled up to read, and follows again once they are back
at the bottom. One rule, common.js U.followTail; the job panel, REDUCE, SCIENCE and MONITOR's ACTIVITY LOG use it."""
import shutil

from tests.test_web_frontend import CONSOLE, ROOT, STUB, chromium, result_of


def _run(tmp_path, driver, budget=20000):
    for name in ("common.js", "styles.css"):
        shutil.copyfile(CONSOLE / name, tmp_path / name)
    html = ('<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head><body>\n<script>' + STUB +
            '</script>\n<script src="common.js"></script>\n<div id="box"></div>\n<pre id="__result"></pre>\n'
            '<script>(async () => { const out = {}; try {\n' + driver +
            '\n} catch (e) { out.driver_error = String(e && e.stack || e); }\n out.errors = window.__errors; '
            '$("__result").textContent = JSON.stringify(out); })();</script></body></html>')
    (tmp_path / "h.html").write_text(html)
    out = result_of(chromium(tmp_path / "h.html", budget))
    assert "driver_error" not in out, out.get("driver_error")
    assert not out["errors"], out["errors"]
    return out


LINES = "const lines = (a, b) => Array.from({ length: b - a }, (_, i) => `line ${a + i}`).join('\\n');\n"


def test_follow_tail_shows_the_last_line_holds_when_scrolled_up_and_follows_again(tmp_path):
    out = _run(tmp_path, LINES + r"""
const U = window.AlmitaUI;
const pre = document.createElement("pre"); pre.className = "job-log"; $("box").appendChild(pre);
const bottom = () => U.logAtBottom(pre);
U.followTail(pre, () => { pre.textContent = lines(0, 200); });
out.first = { bottom: bottom(), scrolls: pre.scrollHeight > pre.clientHeight };
U.followTail(pre, () => { pre.textContent = lines(0, 260); });
out.grown = bottom();
pre.scrollTop = 0;                                   // the operator scrolls up to read
U.followTail(pre, () => { pre.textContent = lines(0, 320); });
out.held = pre.scrollTop;
pre.scrollTop = pre.scrollHeight;                    // ...and back down
U.followTail(pre, () => { pre.textContent = lines(0, 380); });
out.again = bottom();
""")
    assert out["first"] == {"bottom": True, "scrolls": True}
    assert out["grown"] is True and out["held"] == 0 and out["again"] is True


def test_job_log_box_is_reused_for_the_same_job_and_replaced_for_another(tmp_path):
    out = _run(tmp_path, r"""
const U = window.AlmitaUI;
const a = U.jobLogPre($("box"), "J1"); $("box").appendChild(a);
out.same = U.jobLogPre($("box"), "J1") === a;
const b = U.jobLogPre($("box"), "J2");
out.other = [b !== a, a.isConnected, b.dataset.jobId];
""")
    assert out["same"] is True and out["other"] == [True, False, "J2"]


def test_job_panel_keeps_the_last_line_visible_across_polls(tmp_path):
    """The shared job panel (OBSERVE, ALIGN, CALIBRATE, PIPELINE, STATUS...) through real polls of a growing log."""
    out = _run(tmp_path, LINES + r"""
const U = window.AlmitaUI;
let n = 200;
window.__routes["GET /api/ops/job/J1"] = () => { n += 40; return { body: { ok: true, data: { job_id: "J1", state: "RUNNING", verdict: "RUNNING",
  exit_code: null, elapsed_s: n, log_tail: lines(0, n), facts: {} } } }; };
const panel = U.jobPanel($("box"), null, {});
panel.attach("J1");
await until(() => $("box").querySelector("pre.job-log") && n >= 280, 15000);
const pre = $("box").querySelector("pre.job-log");
out.following = U.logAtBottom(pre) && pre.scrollHeight > pre.clientHeight;
pre.scrollTop = 0;
const seen = n; await until(() => n >= seen + 80, 15000);
out.same_node = $("box").querySelector("pre.job-log") === pre;
out.held = pre.scrollTop;
pre.scrollTop = pre.scrollHeight;
const seen2 = n; await until(() => n >= seen2 + 80, 15000);
out.again = U.logAtBottom(pre);
panel.poller && panel.poller.stop && panel.poller.stop();
""", budget=60000)
    assert out["following"] is True and out["same_node"] is True and out["held"] == 0 and out["again"] is True


def test_reduce_science_and_monitor_logs_use_the_shared_rule():
    """Wiring: the hand-built log boxes reuse their node and go through U.followTail (no more scrollTop=0 rebuilds)."""
    for page, cid in (("reduce.js", "job-reduce-run"), ("science.js", "job-science-run")):
        js = (CONSOLE / page).read_text()
        assert f'const container = $("{cid}");' in js and "U.jobLogPre(container, j.job_id)" in js
        assert "U.followTail(pre," in js
        assert 'pre.className = "job-log"; pre.textContent' not in js           # the old rebuild-every-poll code
    app = (CONSOLE / "app.js").read_text()
    assert "AlmitaUI.followTail(pre,put)" in app
    assert (ROOT / "docs" / "BACKLOG.md").read_text().count("BL-098") >= 1
