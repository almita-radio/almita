"""DÍA / NOCHE theme (operator 2026-10-18: outdoors in sunlight the dark console cannot be read).

Static checks on console/ (every page applies the stored theme in <head>, every localStorage access is wrapped in try/catch,
every colour token has a DAY value) plus the real pages in headless Chromium with the stubbed fetch of test_web_frontend:
the two-button toggle sits in every page header beside the mount buttons, NOCHE is the default, switching changes the
tokens, redraws without a single request, and a blocked localStorage only means NOCHE."""
import re
import shutil

from tests.test_web_frontend import CONSOLE, chromium, page_harness, result_of

PAGES = ("status", "pipeline", "lobe", "align", "calibrate", "observe", "reduce", "science")   # + MONITOR (index.html) below


def _token_block(css: str, selector: str) -> dict:
    m = re.search(re.escape(selector) + r"\{([^}]*)\}", css)
    assert m, f"no {selector} block in styles.css"
    return dict(re.findall(r"(--[\w-]+):([^;]+)", m.group(1)))


def test_every_colour_token_has_a_day_value_and_night_is_unchanged():
    css = (CONSOLE / "styles.css").read_text()
    night = _token_block(css, ":root")
    day = _token_block(css, ':root[data-theme="day"]')
    assert set(night) == set(day), f"tokens without a DAY value: {sorted(set(night) - set(day))}; DAY-only: {sorted(set(day) - set(night))}"
    # the NIGHT palette is the original dark one
    assert {k: night[k] for k in ("--bg", "--panel", "--line", "--text", "--muted", "--ok", "--warn", "--error", "--accent")} == {
        "--bg": "#0b1014", "--panel": "#121a20", "--line": "#2b3942", "--text": "#e6edf1", "--muted": "#91a2ad",
        "--ok": "#55c985", "--warn": "#e6b85c", "--error": "#e36b6b", "--accent": "#65b7d8"}
    assert day["--bg"] == "#ffffff" and day["--text"] == "#05080a" and "color-scheme:light" in css
    for k in ("--ok", "--warn", "--error", "--accent"):
        assert day[k] != night[k], k
    # outside the two token blocks no colour literal is left, except the logo's own backdrop and the hatch legend swatch
    rest = css.replace(re.search(r":root\{[^}]*\}", css).group(0), "").replace(re.search(r':root\[data-theme="day"\]\{[^}]*\}', css).group(0), "")
    rest = re.sub(r'url\("data:[^"]*"\)', "", rest)
    left = set(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", rest))
    assert left <= {"#eef4fb", "#5a6670", "#d8e2e8"}, left


def test_every_page_applies_the_stored_theme_in_head_and_storage_is_always_wrapped():
    for html in sorted(CONSOLE.glob("*.html")):
        text = html.read_text()
        head = text.split("</head>", 1)[0]
        assert "almita-theme" in head and 'setAttribute("data-theme"' in head, f"{html.name}: no pre-paint theme script in <head>"
        assert text.count("almita-theme") == 1
    sources = sorted(CONSOLE.glob("*.js")) + sorted(CONSOLE.glob("*.html"))
    hits = 0
    for f in sources:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if ("localStorage" in line or "sessionStorage" in line) and not line.strip().startswith(("//", "*", "/*")):
                hits += 1
                assert re.search(r"try\s*\{", line), f"{f.name}:{n}: storage access outside a try/catch on its line: {line.strip()[:120]}"
    assert hits >= 3                                    # the head script, the read and the write in common.js


DRIVER = r"""
await until(() => document.querySelector("#theme-toggle button"), 5000);
const box = document.getElementById("theme-toggle");
const btns = [...document.querySelectorAll("#theme-toggle button")];
out.kinds = btns.map((b) => b.tagName + ":" + b.textContent);
out.selects = document.querySelectorAll(".topbar select").length;
out.besideMount = !!box && box.previousElementSibling && box.previousElementSibling.id === "mount-device"
                  && box.nextElementSibling && box.nextElementSibling.id === "btn-mount-stop";
out.initial = document.documentElement.getAttribute("data-theme");
out.activeInitial = (document.querySelector("#theme-toggle .active") || {}).textContent || null;
let events = 0; window.addEventListener("almita-theme", () => { events++; });
const nErr = window.__errors.length, nCalls = window.__calls.length;
btns.find((b) => b.textContent === "DÍA").click();
out.callsDuringSwitch = window.__calls.length - nCalls;
out.day = document.documentElement.getAttribute("data-theme");
out.dayBg = getComputedStyle(document.body).backgroundColor;
out.dayText = getComputedStyle(document.body).color;
out.activeDay = document.querySelector("#theme-toggle .active").textContent;
out.stored = (() => { try { return localStorage.getItem("almita-theme"); } catch (e) { return "BLOCKED"; } })();
btns.find((b) => b.textContent === "NOCHE").click();
out.night = document.documentElement.getAttribute("data-theme");
out.nightBg = getComputedStyle(document.body).backgroundColor;
out.events = events;
await sleep(200);
out.newErrors = window.__errors.slice(nErr);
""".strip()


# MONITOR's app.js declares its own global $, so it gets this smaller stub (every request answers 404; none is expected).
MONITOR_STUB = r"""
window.__calls = []; window.__errors = [];
window.fetch = (url, opts) => { window.__calls.push(String(url)); return Promise.resolve({ ok: false, status: 404, headers: { get: () => null },
  json: async () => ({}), text: async () => "{}" }); };
window.addEventListener("error", (e) => window.__errors.push(String(e.message)));
window.addEventListener("unhandledrejection", (e) => window.__errors.push("unhandled: " + String(e.reason)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms) { const end = Date.now() + (ms || 4000); while (Date.now() < end) { try { if (fn()) return true; } catch (e) {} await sleep(20); } return false; }
"""


def _frames_harness(tmp_path, names):
    frames = ", ".join(f'"{n}.html"' for n in names)
    (tmp_path / "frames.html").write_text(f"""<!doctype html><html><body><pre id="__result"></pre><script>
const names = [{frames}]; const out = {{}}; let pending = names.length;
for (const n of names) {{
  const f = document.createElement("iframe"); f.style.cssText = "width:1300px;height:900px;border:0"; f.src = n;
  document.body.appendChild(f);
  const poll = setInterval(() => {{ try {{ const r = f.contentDocument.getElementById("__result");
    if (r && r.textContent) {{ out[n] = JSON.parse(r.textContent); clearInterval(poll);
      if (--pending === 0) document.getElementById("__result").textContent = JSON.stringify(out); }} }} catch (e) {{}} }}, 50);
}}
</script></body></html>""")
    return tmp_path / "frames.html"


def _assert_toggle(name, o):
    assert "driver_error" not in o, (name, o.get("driver_error"))
    assert o["kinds"] == ["BUTTON:DÍA", "BUTTON:NOCHE"] and o["selects"] == 0, (name, o)
    assert o["besideMount"] is True, (name, "toggle not between the mount buttons and STOP MOUNT")
    assert o["initial"] == "night" and o["activeInitial"] == "NOCHE", (name, o)                      # default NOCHE
    assert o["day"] == "day" and o["activeDay"] == "DÍA" and o["dayBg"] == "rgb(255, 255, 255)" and o["dayText"] == "rgb(5, 8, 10)", (name, o)
    assert o["night"] == "night" and o["nightBg"] == "rgb(11, 16, 20)", (name, o)
    assert o["events"] == 2 and o["callsDuringSwitch"] == 0, (name, o)                                 # redraw only, no request
    assert o["stored"] in ("day", "BLOCKED"), (name, o)
    assert o["newErrors"] == [], (name, o["newErrors"])


def test_theme_toggle_is_in_every_page_header_switches_and_never_requests(tmp_path):
    for page in PAGES:
        page_harness(tmp_path, page, "", DRIVER)
    out = result_of(chromium(_frames_harness(tmp_path, PAGES), 30000))
    assert sorted(out) == sorted(f"{p}.html" for p in PAGES), sorted(out)
    for name, o in out.items():
        _assert_toggle(name, o)


def test_monitor_page_has_the_toggle_too(tmp_path):
    for name in ("spectral_stack_3d.js",):
        shutil.copyfile(CONSOLE / name, tmp_path / name)
    target = page_harness(tmp_path, "index", "", DRIVER, js_files=("common.js", "app.js", "styles.css"))
    html = target.read_text()                           # MONITOR's <body> carries an attribute, so install the stub here
    body = re.search(r"<body[^>]*>", html).group(0)
    target.write_text(html.replace(body, body + "\n<script>" + MONITOR_STUB + "</script>", 1))
    out = result_of(chromium(target, 12000))
    _assert_toggle("index.html", out)


def test_blocked_storage_means_night_and_a_stored_day_is_applied_before_the_body(tmp_path):
    driver = r"""
out.atBody = window.__themeAtBody;
await until(() => document.querySelector("#theme-toggle .active"), 5000);
out.active = document.querySelector("#theme-toggle .active").textContent;
out.bg = getComputedStyle(document.body).backgroundColor;
const nErr = window.__errors.length;
[...document.querySelectorAll("#theme-toggle button")].find((b) => b.textContent === window.__flip).click();
out.after = document.documentElement.getAttribute("data-theme");
await sleep(100);
out.newErrors = window.__errors.slice(nErr);
""".strip()
    probe = "window.__themeAtBody = document.documentElement.getAttribute('data-theme');"
    blocked = "<script>Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('blocked', 'SecurityError'); } });</script>"
    stored_day = ("<script>(() => { const m = { 'almita-theme': 'day' }; Object.defineProperty(window, 'localStorage', { value: "
                  "{ getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); } } }); })();</script>")
    results = {}
    for label, inject, flip in (("blocked", blocked, "DÍA"), ("day", stored_day, "NOCHE")):
        sub = tmp_path / label
        sub.mkdir()
        target = page_harness(sub, "status", probe + f"window.__flip = {flip!r};", driver)
        html = target.read_text()
        target.write_text(html.replace("<head>", "<head>" + inject, 1))
        results[label] = result_of(chromium(target, 8000))
    b, d = results["blocked"], results["day"]
    assert "driver_error" not in b and "driver_error" not in d, (b, d)
    assert b["atBody"] == "night" and b["active"] == "NOCHE" and b["bg"] == "rgb(11, 16, 20)"
    assert b["after"] == "day" and b["newErrors"] == []                 # still switches for this page; nothing thrown
    assert d["atBody"] == "day" and d["active"] == "DÍA" and d["bg"] == "rgb(255, 255, 255)"   # applied in <head>, before the body
    assert d["after"] == "night" and d["newErrors"] == []
