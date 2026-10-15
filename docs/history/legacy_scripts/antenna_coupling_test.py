#!/usr/bin/env python3
"""Passive antenna-vs-50-ohm coupling gate; no mount or INDI operations."""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from rf_chain_characterization import analyze
from sdr_capture import SDRCapture


async def run(args):
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    antenna_path = output / "antenna_baseline_gain_40.2.h5"
    sdr = SDRCapture("network", args.host, args.port, verbose=True)
    try:
        await sdr.connect()
        await sdr.configure(args.frequency, args.sample_rate, 40.2)
        await sdr.capture(
            2.0,
            str(antenna_path),
            args.sample_rate,
            {
                "test": "ALMITA-ALIGNMENT-RETURN-TO-SKY-01",
                "rf_input": "ANTENNA_AT_LNA_INPUT",
                "gain_requested_db": 40.2,
            },
        )
    finally:
        await sdr.close()

    antenna = analyze(antenna_path)
    references = [analyze(Path(path)) for path in args.reference]
    scalar_keys = [
        "std_combined", "broadband_power", "mean_psd_power", "entropy_bits",
        "p001", "p999", "clipping_fraction",
    ]
    baseline = {
        key: float(np.mean([record[key] for record in references]))
        for key in scalar_keys
    }
    baseline_std = {
        key: float(np.std([record[key] for record in references], ddof=1))
        for key in scalar_keys
    }
    ratios = {
        "sigma_ratio": antenna["std_combined"] / baseline["std_combined"],
        "psd_ratio": antenna["broadband_power"] / baseline["broadband_power"],
        "integrated_power_ratio": antenna["mean_psd_power"] / baseline["mean_psd_power"],
    }
    changed = (
        ratios["sigma_ratio"] > 1.05
        and ratios["psd_ratio"] > 1.10
        and antenna["clipping_fraction"] == 0.0
    )
    summary = {
        "test": "ALMITA-ALIGNMENT-RETURN-TO-SKY-01-ANTENNA-COUPLING",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "frequency_hz": args.frequency,
        "sample_rate_hz": args.sample_rate,
        "gain_db": 40.2,
        "duration_seconds": 2.0,
        "antenna_hdf5": str(antenna_path),
        "reference_hdf5": args.reference,
        "antenna": {key: antenna[key] for key in scalar_keys},
        "baseline_50ohm_mean": baseline,
        "baseline_50ohm_std": baseline_std,
        "ratios": ratios,
        "antenna_coupling": "PASS" if changed else "FAIL",
        "gate_note": "Relative passive gate only; not an absolute RF calibration.",
    }
    (output / "antenna_coupling_summary.json").write_text(json.dumps(summary, indent=2))

    frequency = np.asarray(antenna["frequency_hz"]) / 1e6
    figure, axis = plt.subplots(figsize=(10, 5))
    for index, record in enumerate(references, start=1):
        axis.plot(
            frequency, 10 * np.log10(np.maximum(record["psd"], 1e-30)),
            alpha=0.65, label=f"50 ohm reference {index}",
        )
    axis.plot(
        frequency, 10 * np.log10(np.maximum(antenna["psd"], 1e-30)),
        label="antenna", linewidth=1.2,
    )
    axis.set(xlabel="Frequency (MHz)", ylabel="PSD (dB arbitrary)")
    axis.grid(True)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output / "antenna_vs_50ohm_spectrum.png", dpi=150)
    plt.close(figure)
    print(json.dumps(summary, indent=2))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--reference", nargs="+", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1234)
    parser.add_argument("--frequency", type=int, default=1420405752)
    parser.add_argument("--sample-rate", type=int, default=2400000)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
