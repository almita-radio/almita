#!/usr/bin/env python3
"""Capture one operator-confirmed stage of the indoor coupling check."""

import argparse
import asyncio
import json
from pathlib import Path

from rf_chain_characterization import analyze
from sdr_capture import SDRCapture


async def run(args):
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{args.stage.lower()}.h5"
    sdr = SDRCapture("network", "127.0.0.1", 1234, verbose=True)
    try:
        await sdr.connect()
        await sdr.configure(1420405752, 2400000, 40.2)
        await sdr.capture(
            2.0, str(path), 2400000,
            {
                "test": "ALMITA-INDOOR-ANTENNA-COUPLING-CHECK-01",
                "environment": "INDOOR_DEPARTMENT",
                "condition": args.stage,
                "rf_input": args.rf_input,
                "gain_requested_db": 40.2,
                "connection_limitation": "NEW_IDENTICAL_CONNECTION_PER_STAGE_AFTER_USB_STATUS_5",
            },
        )
    finally:
        await sdr.close()
    result = analyze(path)
    scalar = {key: value for key, value in result.items() if not isinstance(value, list)}
    (output / f"{args.stage.lower()}_metrics.json").write_text(json.dumps(scalar, indent=2))
    print(json.dumps({"stage": args.stage, **scalar}, indent=2))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--rf-input", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
