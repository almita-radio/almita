#!/usr/bin/env python3
"""Render a polled dashboard DOM to a deterministic offline screenshot."""
import argparse
import re
import subprocess
import tempfile
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument("url");parser.add_argument("output");parser.add_argument("--width",type=int,default=1600);parser.add_argument("--height",type=int,default=1400)
args=parser.parse_args();output=Path(args.output);html=output.with_suffix(".html")
common=["chromium","--headless","--no-sandbox","--disable-gpu","--disable-dev-shm-usage"]
dump=subprocess.run(common+["--dump-dom",args.url],capture_output=True,text=True,check=True).stdout
base=args.url.split("?",1)[0]
dump=dump.replace("<head>",f'<head><base href="{base}">',1)
dump=re.sub(r'<script src="app\.js"></script>',"",dump)
html.write_text(dump)
with tempfile.TemporaryDirectory(prefix="almita-dashboard-chromium-") as profile:
    subprocess.run(common+[f"--user-data-dir={profile}","--hide-scrollbars",f"--window-size={args.width},{args.height}",f"--screenshot={output.resolve()}",html.resolve().as_uri()],check=True)
print(output,output.stat().st_size)
