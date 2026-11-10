"""MONITOR: the SPECTRAL STACK 3D panel never spills over the section below it (operator 2026-11-10: "el dibujo 3D está
montado por sobre el gráfico de la captura, abajo"). Its height follows the antenna column, but never below its own
content (header + viewport min-height); when the row wraps (narrow screen) it keeps its natural height."""
import re
import shutil

from tests.test_web_frontend import CONSOLE, chromium, result_of


def _layout(tmp_path, width, short_left=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name in ("styles.css", "spectral_stack_3d.js"):
        shutil.copyfile(CONSOLE / name, tmp_path / name)
    shutil.copytree(CONSOLE / "vendor", tmp_path / "vendor")
    src = (CONSOLE / "index.html").read_text()
    row = re.search(r'(    <div class="science-row">.*?</section>\n    </div>)', src, re.S).group(1)
    short = '<style>.science-row-left > :not(h3){display:none}</style>' if short_left else ''
    html = ('<!doctype html><html data-theme="night"><head><link rel="stylesheet" href="styles.css">' + short + '</head><body><main>'
            + row + '<section id="below" class="panel" style="margin:10px 0;height:200px">BELOW</section></main>'
            '<pre id="__result"></pre>'
            '<script src="vendor/three/three.min.js"></script><script src="vendor/three/OrbitControls.js"></script>'
            '<script src="spectral_stack_3d.js"></script>'
            '<script>setTimeout(() => { const p = document.getElementById("spectral-stack-panel").getBoundingClientRect();'
            ' const v = document.getElementById("spectral-stack-viewport").getBoundingClientRect();'
            ' const b = document.getElementById("below").getBoundingClientRect();'
            ' const l = document.querySelector(".science-row-left").getBoundingClientRect();'
            ' document.getElementById("__result").textContent = JSON.stringify({panel_bottom: p.bottom, viewport_bottom: v.bottom,'
            ' viewport_h: v.height, below_top: b.top, left_h: l.height, panel_h: p.height,'
            ' header_h: document.querySelector("#spectral-stack-panel header").getBoundingClientRect().height}); }, 1500);</script></body></html>')
    (tmp_path / "m.html").write_text(html)
    return result_of(chromium(tmp_path / "m.html", 6000, width=width))


def test_short_antenna_column_does_not_push_the_3d_view_over_the_section_below(tmp_path):
    out = _layout(tmp_path, 1300, short_left=True)      # no thumbnails: the antenna column is short
    assert out["left_h"] < 200
    assert out["viewport_h"] >= 259                      # the 3D view keeps its minimum size...
    assert out["viewport_bottom"] <= out["panel_bottom"] + 0.5 <= out["below_top"]   # ...inside its panel, above BELOW


def test_narrow_screen_stacks_the_panel_with_its_natural_height(tmp_path):
    out = _layout(tmp_path, 700, short_left=True)
    assert out["viewport_h"] >= 259 and out["panel_bottom"] <= out["below_top"]


def test_the_panel_is_not_taller_than_the_column_or_its_own_minimum(tmp_path):
    """The other half of the same bug: the column stretched to the panel and the panel was then pinned to the
    stretched column, so it stayed far taller than the antenna column with an empty 3D area."""
    for short in (False, True):
        out = _layout(tmp_path / str(short), 1300, short_left=short)
        need = max(out["left_h"], out["header_h"] + 262)
        assert out["panel_h"] <= need + 1, out

