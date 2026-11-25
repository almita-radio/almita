"""Build the ALMITA book: parte1.html (full document) + parte2..5 fragments, figures inlined, PDF via headless Chrome.
Usage: python3 docs/book/build.py
"""
import base64, re, subprocess
from pathlib import Path

HERE = Path(__file__).parent
IMAGES = {"cover": ("cover_20261105.png", None), "aquila": ("sim_aquila.png", "width:118mm;background:#000"),
          "rotation": ("rotation_20261119.png", "width:100%"), "smc": ("ALMITA_SMC_20261119.png", "width:100%")}

html = (HERE / "parte1.html").read_text(encoding="utf-8")
frags = "\n".join((HERE / f"parte{i}.html").read_text(encoding="utf-8") for i in range(2, 6))
html = html.replace("</body>", frags + "\n</body>")


def fig(m):
    svg = (HERE / "fig" / f"{m.group(1)}.svg").read_text(encoding="utf-8")
    return svg[svg.index("<svg"):]


def img(m):
    name, style = IMAGES[m.group(1)]
    data = base64.b64encode((HERE / "img" / name).read_bytes()).decode()
    st = f' style="{style}"' if style else ""
    return f'<img src="data:image/png;base64,{data}" alt="{m.group(1)}"{st}>'


# --- print imposition for a ring-bound, double-sided copy (Chrome ignores break-before: right) ---
BLANK = '<div class="blank"></div>'
html = html.replace("</style>", ".blank { page: blank; break-before: page; break-after: page; height: 10mm; } .mk { font-size: 1px; color: #fff; }\n</style>", 1)
# the back of every part title page stays blank
html = re.sub(r'(<section class="partpage">.*?</section>)', lambda m: m.group(1) + BLANK, html, flags=re.S)
# markers on what must open on a right-hand (odd) page: the introduction, each part title, the first annex
targets = [r'<h1 style="break-before: right"><span class="num">Antes de empezar', r'<section class="partpage">',
           r'<h1><span class="num">Anexo A']
k = 0
def mark(m):
    global k
    k += 1
    return m.group(0) + f'<span class="mk">MK{k:02d}MK</span>'
for t in targets:
    html = re.sub(re.escape(t) if not t.startswith("<section") else t, mark, html)
html = re.sub(r"\{\{fig:(\w+)\}\}", fig, html)
html = re.sub(r"\{\{img:(\w+)\}\}", img, html)
def render(h):
    built = HERE / "build" / "libro.html"; built.parent.mkdir(exist_ok=True); built.write_text(h, encoding="utf-8")
    pdf = HERE / "ALMITA_libro.pdf"
    subprocess.run(["google-chrome", "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=20000",
                    f"--print-to-pdf={pdf}", built.as_uri()], check=True, capture_output=True)
    return pdf


def marker_pages(pdf):
    n = int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout).group(1))
    pages = {}
    for p in range(1, n + 1):
        t = subprocess.run(["pdftotext", "-f", str(p), "-l", str(p), str(pdf), "-"], capture_output=True, text=True).stdout
        for m in re.findall(r"MK(\d\d)MK", re.sub(r"\s+", "", t)):
            pages.setdefault(int(m), p)
    return pages


pdf = render(html)
pages = marker_pages(pdf)
added = 0
for i in sorted(pages):
    if (pages[i] + added) % 2 == 0:                 # would open on a left-hand page: push it one page on
        tag = f'<span class="mk">MK{i:02d}MK</span>'
        j = html.index(tag); start = max(html.rfind("<section", 0, j), html.rfind("<h1", 0, j))
        html = html[:start] + BLANK + html[start:]; added += 1
pdf = render(html)
pages = marker_pages(pdf)
print("opens on pages:", pages, "all odd:", all(p % 2 for p in pages.values()))
print(pdf, pdf.stat().st_size // 1024, "KB")
