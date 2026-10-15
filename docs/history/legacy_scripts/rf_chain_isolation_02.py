#!/usr/bin/env python3
"""Controlled 50-ohm direct RTL-SDR isolation capture; no INDI or mount I/O."""

import argparse
import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from rf_chain_characterization import analyze
from sdr_capture import SDRCapture


async def run(args):
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    sdr = SDRCapture("network", args.host, args.port, verbose=True)
    records = []
    try:
        await sdr.connect()
        for gain in args.gains:
            await sdr.configure(args.frequency, args.sample_rate, gain)
            capture_path = output / f"rtl_direct_50ohm_gain_{gain:04.1f}.h5"
            await sdr.capture(
                args.seconds,
                str(capture_path),
                args.sample_rate,
                {
                    "test": "ALMITA-RF-CHAIN-ISOLATION-02",
                    "rf_input": "50_OHM_DIRECT_RTL_SDR",
                    "bias_t": "OFF_RTL_TCP_STARTED_WITHOUT_T",
                    "gain_requested_db": gain,
                    "center_frequency_hz": args.frequency,
                    "sample_rate_hz": args.sample_rate,
                },
            )
            metrics = analyze(capture_path)
            row = {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "gain_requested_db": gain,
                "frequency_hz_configured": args.frequency,
                "sample_rate_hz": args.sample_rate,
                "duration_seconds": args.seconds,
                "rf_input": "50_OHM_DIRECT_RTL_SDR",
                "bias_t": "OFF_RTL_TCP_STARTED_WITHOUT_T",
                "hdf5_path": str(capture_path),
                **metrics,
            }
            records.append(row)
            print(
                f"RTL_DIRECT gain={gain:.1f} sigma={metrics['std_combined']:.6f} "
                f"psd_median={metrics['broadband_power']:.6g} "
                f"p001={metrics['p001']:.1f} p999={metrics['p999']:.1f} "
                f"entropy={metrics['entropy_bits']:.4f} "
                f"clipping={metrics['clipping_fraction']:.3%}",
                flush=True,
            )
    finally:
        await sdr.close()

    scalar_fields = [
        key for key, value in records[0].items()
        if not isinstance(value, (dict, list))
    ]
    with (output / "rtl_direct_50ohm.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=scalar_fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(records)
    (output / "rtl_direct_capture_summary.json").write_text(
        json.dumps({"records": records}, indent=2)
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1234)
    parser.add_argument("--frequency", type=int, default=1420405752)
    parser.add_argument("--sample-rate", type=int, default=2400000)
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--gains", nargs="+", type=float, default=[0.0, 40.2, 49.6])
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
