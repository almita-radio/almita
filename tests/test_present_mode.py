"""BL-094 presentation mode: /present/<page> is the same page, zoomed for a projector (no hardware, no real data)."""
import contextlib
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import almita_console_server
import almita_orchestrator_server as server_mod
from tests.test_web_frontend import STUB, chromium, result_of

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"


@contextlib.contextmanager
def running_server(public_root):
    httpd = server_mod.make_server("127.0.0.1", 0, authenticator=None, public_root=public_root)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode()


@pytest.fixture
def public_root(tmp_path):
    return almita_console_server.prepare_console_web(CONSOLE, tmp_path / "runtime", tmp_path / "public")


def test_present_html_marks_the_page_anchors_it_and_keeps_links_inside_present():
    page = server_mod.present_html((CONSOLE / "index.html").read_text())
    assert '<html data-present="1"' in page and '<head><base href="/">' in page
    assert 'href="/present/observe.html"' in page and 'href="/observe.html"' not in page
    assert 'class="obs-nav-link active" href="/present/"' in page            # MONITOR's own link
    assert 'href="/">EXIT PRESENT</a></nav>' in page
    assert 'href="/vnc/"' in page                                             # the desktop stays where it is
    other = server_mod.present_html((CONSOLE / "observe.html").read_text())
    assert '<html data-present="1"' in other and "EXIT PRESENT" not in other  # its nav is built by common.js


def test_present_routes_serve_the_same_pages_and_leave_the_normal_ones_alone(public_root):
    with running_server(public_root) as base:
        status, normal = _get(f"{base}/observe.html")
        assert status == 200 and "data-present" not in normal and "<base " not in normal
        status, present = _get(f"{base}/present/observe.html")
        assert status == 200 and '<html data-present="1"' in present and 'src="observe.js"' in present
        status, monitor = _get(f"{base}/present/")
        assert status == 200 and '<html data-present="1"' in monitor and "app.js?v=" in monitor
        assert _get(f"{base}/present")[0] == 200
        for bad in ("/present/observe.js", "/present/nope.html", "/present/../almita_web_auth.py", "/present/data/x.html"):
            with pytest.raises(urllib.error.HTTPError) as err:
                _get(base + bad)
            assert err.value.code == 404, bad


def test_the_zoom_lives_only_under_data_present_and_canvases_use_the_helpers():
    css = (CONSOLE / "styles.css").read_text()
    assert "html[data-present]{zoom:1.5}" in css
    for name in ("common.js", "lobe.js", "status.js", "sessions.js", "science.js", "reduce.js"):
        js = (CONSOLE / name).read_text()
        assert "devicePixelRatio" not in js.replace("(window.devicePixelRatio || 1) * U.uiZoom()", ""), name
    assert "U.pageHref(`${U.consoleBase()}/`)" in (CONSOLE / "observe.js").read_text()


def _sky_page(tmp_path, present):
    shutil.copyfile(CONSOLE / "common.js", tmp_path / "common.js")
    shutil.copyfile(CONSOLE / "styles.css", tmp_path / "styles.css")
    driver = r"""
const U = window.AlmitaUI;
const c = document.getElementById("sky");
U.drawSkyView(c, { sun: { az_deg: 0, alt_deg: -10, above_horizon: false }, areas: [] }, {});
const r = c.getBoundingClientRect();
const at = (fx, fy) => U.skyAzAlt(c, { clientX: r.left + r.width * fx, clientY: r.top + r.height * fy });
out.backing = [c.width, c.clientWidth, U.uiZoom()];
out.centre = at(0.5, 0.5);
out.north = at(0.5, 0.25);
const header = document.getElementById("almita-header");
U.mountHeader("OBSERVE", "test");
out.links = Array.from(header.querySelectorAll("a")).map((a) => [a.textContent, a.getAttribute("href")]);
"""
    attr = ' data-present="1"' if present else ""
    html = (f'<!doctype html><html{attr}><head><link rel="stylesheet" href="styles.css"></head><body>\n<script>' + STUB + '</script>\n'
            '<header id="almita-header"></header><canvas id="sky" style="width:300px;height:300px;display:block"></canvas>\n'
            '<script src="common.js"></script>\n<pre id="__result"></pre>\n<script>(async () => { const out = {}; try {\n' + driver +
            '\n} catch (e) { out.driver_error = String(e && e.stack || e); }\n out.errors = window.__errors; $("__result").textContent = JSON.stringify(out); })();</script></body></html>')
    (tmp_path / "p.html").write_text(html)
    return result_of(chromium(tmp_path / "p.html", 8000))


@pytest.mark.parametrize("present", [False, True])
def test_sky_map_is_sharp_and_clicks_land_where_drawn_in_both_modes(tmp_path, present):
    out = _sky_page(tmp_path, present)
    assert "driver_error" not in out, out.get("driver_error")
    zoom = 1.5 if present else 1.0
    assert out["backing"] == [round(300 * zoom), 300, zoom]                  # backing store follows the zoom: no blur
    assert out["centre"]["alt_deg"] == pytest.approx(90, abs=0.5)           # the zenith is still where it is drawn
    assert out["north"]["az_deg"] == pytest.approx(0, abs=1) or out["north"]["az_deg"] == pytest.approx(360, abs=1)
    links = dict(out["links"])
    if present:
        assert links["OBSERVE"] == "/present/observe.html" and links["MONITOR"] == "/present/" and links["DESKTOP"] == "/vnc/"
        assert links["EXIT PRESENT"] == (tmp_path / "p.html").as_posix()      # this page's own path, without /present
    else:
        assert links["OBSERVE"] == "/observe.html" and "EXIT PRESENT" not in links
