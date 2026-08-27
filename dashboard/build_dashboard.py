#!/usr/bin/env python3
"""Dependency-free static build for Dashboard V1."""
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parent
DIST=ROOT/"dist"
DIST.mkdir(exist_ok=True)
for name in ("index.html","styles.css","app.js","README.md"):
    shutil.copy2(ROOT/name,DIST/name)
print(f"dashboard build: {DIST}")
for path in sorted(DIST.iterdir()):
    print(path.name,path.stat().st_size)
