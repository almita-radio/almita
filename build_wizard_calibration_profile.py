#!/usr/bin/env python3
"""Build the OBSERVE/REDUCE-consumable RELATIVE calibration profile (.json + .npz) from a CALIBRATE reference
wizard session's real 50 ohm captures - no new capture, no mount movement. See
calibration_engine/wizard_profile_converter.py for the full rationale and the exact requirements enforced
(real+complete captures, LNA_INPUT connection, frequency/rate/gain coherence against the session's own
verified receiver_snapshot).

Usage:
    ./.venv/bin/python build_wizard_calibration_profile.py \\
        --session-dir data/calibration/WIZARD-... \\
        --output-dir data/calibration/profiles/wizard_v1 \\
        --antenna data/calibration/CAL-.../captures/capture_000.h5 [more antenna captures...]

Prints one JSON result to stdout: the profile path, validation/provenance report, and - only if at least one
--antenna capture was given - its real compatibility/applied-calibration check (always labeled as an INDOOR
instrument-chain check, never a celestial measurement). Exits non-zero (no profile written) if the session's
50 ohm reference does not meet every requirement.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from calibration_engine.wizard_profile_converter import (
    WizardProfileError, build_profile_from_wizard, validate_against_capture,
)


def run(args) -> int:
    try:
        built = build_profile_from_wizard(args.session_dir, args.output_stem, fft_size=args.fft_size)
    except WizardProfileError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    profile = built["profile"]
    result = {"ok": True, "profile_stem": built["profile_stem"], "report": built["report"]}

    validations = []
    recommend = False
    if args.antenna:
        for capture in args.antenna:
            v = validate_against_capture(profile, capture)
            validations.append(v)
        recommend = all(v["compatibility"]["status"] == "COMPATIBLE" for v in validations)
    result["antenna_validations"] = validations
    result["recommend_for_observe"] = recommend
    result["recommend_reason"] = (
        "all supplied antenna captures were COMPATIBLE with this profile" if args.antenna and recommend else
        "no --antenna capture supplied - compatibility not demonstrated, do not recommend this path yet" if not args.antenna else
        "at least one supplied antenna capture was NOT compatible - see antenna_validations for the reason"
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if (not args.antenna or recommend) else 3


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session-dir", required=True, help="a CALIBRATE reference wizard session directory (data/calibration/WIZARD-...)")
    p.add_argument("--output-stem", required=True, help="output path stem (writes <stem>.json and <stem>.npz)")
    p.add_argument("--fft-size", type=int, default=8192)
    p.add_argument("--antenna", nargs="*", default=[], help="real antenna-topology HDF5 capture(s) to check compatibility against")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
