#!/usr/bin/env python3
"""Builds docs/MANUAL_CORTAPALOS.pdf from docs/MANUAL_CORTAPALOS.md (the Markdown is the source; edit it, then run
this). Uses the system python3 `markdown` package and headless chromium's print-to-PDF - both already on the Pi,
nothing is installed. Offline: no network, no hardware.

    /usr/bin/python3 scripts/build_manual_pdf.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "MANUAL_CORTAPALOS.md"
OUT = ROOT / "docs" / "MANUAL_CORTAPALOS.pdf"

CSS = """
@page { size: A4; margin: 14mm 13mm 16mm 13mm; }
body { font-family: "DejaVu Sans", sans-serif; font-size: 9.6pt; line-height: 1.38; color: #1b1f23; }
h1 { font-size: 19pt; margin: 0 0 4pt; border-bottom: 2.5pt solid #1d5f7a; padding-bottom: 4pt; color: #12384a; }
h2 { font-size: 13.5pt; margin: 14pt 0 5pt; color: #12384a; border-bottom: 1pt solid #b7c8d0; padding-bottom: 2pt;
     break-after: avoid; }
h3 { font-size: 11pt; margin: 11pt 0 4pt; color: #1d5f7a; break-after: avoid; }
p, li { orphans: 3; widows: 3; }
ul, ol { margin: 3pt 0 6pt; padding-left: 18pt; }
li { margin: 1.5pt 0; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.4pt; background: #eef3f5; padding: 0 2pt;
       border-radius: 2pt; }
pre { background: #eef3f5; border: 0.6pt solid #c9d6dc; padding: 6pt 8pt; font-size: 8pt; line-height: 1.3;
      white-space: pre-wrap; break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8pt; }
table { border-collapse: collapse; width: 100%; margin: 5pt 0 8pt; font-size: 8.3pt; break-inside: auto; }
tr { break-inside: avoid; }
th { background: #1d5f7a; color: #fff; text-align: left; padding: 3pt 4pt; }
td { border-bottom: 0.5pt solid #c9d6dc; padding: 3pt 4pt; vertical-align: top; }
blockquote { margin: 6pt 0; padding: 5pt 9pt; background: #fff4e0; border-left: 3pt solid #d98a00; break-inside: avoid; }
blockquote p { margin: 0; }
hr { border: none; border-top: 0.6pt solid #b7c8d0; margin: 10pt 0; }
strong { color: #0f2733; }
.footer-note { margin-top: 14pt; font-size: 7.5pt; color: #6a7a82; }
"""


def build() -> Path:
    body = markdown.markdown(SRC.read_text(encoding="utf-8"), extensions=["tables", "fenced_code", "sane_lists"])
    html = (f"<!doctype html><html lang='es'><head><meta charset='utf-8'><title>ALMITA — Manual cortapalos</title>"
            f"<style>{CSS}</style></head><body>{body}"
            f"<p class='footer-note'>Generado desde docs/MANUAL_CORTAPALOS.md con scripts/build_manual_pdf.py.</p>"
            f"</body></html>")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "manual.html"
        page.write_text(html, encoding="utf-8")
        subprocess.run(["chromium", "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                        f"--user-data-dir={tmp}/profile", f"--print-to-pdf={OUT}", page.as_uri()],
                       check=True, capture_output=True, timeout=180)
    return OUT


if __name__ == "__main__":
    out = build()
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    sys.exit(0)
