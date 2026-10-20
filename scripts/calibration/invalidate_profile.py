#!/usr/bin/env python3
"""Invalidate an OBSERVE/QUICKLOOK calibration profile (writes <stem>.INVALIDATED.json; moves and edits nothing).

    .venv/bin/python scripts/calibration/invalidate_profile.py <profile .json|.npz|stem> --reason "why" [--by name]

From then on calibration_foundation.load_calibration_profile() refuses it, so OBSERVE no longer proposes it and
QUICKLOOK, preflight and REDUCE reject it. To undo, delete the marker file."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import calibration_foundation  # noqa: E402

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument("profile")
p.add_argument("--reason", required=True)
p.add_argument("--by", default="operator")
a = p.parse_args()
print(calibration_foundation.invalidate_calibration_profile(a.profile, a.reason, a.by))
