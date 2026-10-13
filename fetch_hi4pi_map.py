#!/usr/bin/env python3
"""Download the real HI4PI all-sky HI column density map used by the ALIGN web sky view (hi4pi_map.py).

Source (verified, not guessed): HI4PI Collaboration, Ben Bekhti N. et al. 2016, A&A 594, A116
(2016A&A...594A.116H), retrieved from the CDS/VizieR catalog mirror J/A+A/594/A116:
    https://cdsarc.cds.unistra.fr/ftp/J/A+A/594/A116/NHI/EQ2000/CAR_-600_600.fits
Product: all-sky N_HI (HI column density, cm^-2), integrated over the full published LSR velocity range
(-600..+600 km/s), Equatorial J2000, CAR (Plate Carree) projection, ~36 MB, ~0.0833 deg/pixel.

License: no separate machine-readable license file exists on the CDS mirror. CDS/VizieR's long-standing
policy is free access for scientific/research/educational use with citation of the paper above - this script
does not claim any further right, and this file is only used here as an offline alignment reference (never
redistributed, never called "Almita data").

Run once, from the repo root:  ./.venv/bin/python fetch_hi4pi_map.py
Idempotent: does nothing if the file already exists (use --force to re-download).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

from hi4pi_map import MAP_PATH, SOURCE, SOURCE_URL


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--force", action="store_true", help="re-download even if the file already exists")
    args = p.parse_args()

    if MAP_PATH.is_file() and not args.force:
        size = MAP_PATH.stat().st_size
        print(f"already present: {MAP_PATH} ({size / 1e6:.1f} MB) - use --force to re-download")
        return 0

    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {SOURCE_URL}")
    print(f"citation: {SOURCE['citation']}")
    tmp = MAP_PATH.with_suffix(".fits.part")
    try:
        with urllib.request.urlopen(SOURCE_URL, timeout=120) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length", 0))
            written = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                written += len(chunk)
                if total:
                    print(f"\r  {written / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print()
    except Exception as exc:  # noqa: BLE001 - report and leave no partial file behind
        tmp.unlink(missing_ok=True)
        print(f"download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    tmp.rename(MAP_PATH)
    digest = hashlib.sha256(MAP_PATH.read_bytes()).hexdigest()
    print(f"saved {MAP_PATH} ({MAP_PATH.stat().st_size / 1e6:.1f} MB), sha256={digest}")
    print(f"license note: {SOURCE['license_note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
