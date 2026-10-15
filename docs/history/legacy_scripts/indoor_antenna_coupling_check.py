#!/usr/bin/env python3
"""Alternating indoor 50-ohm/antenna check with one persistent SDR connection."""

import argparse
import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from rf_chain_characterization import analyze
from sdr_capture import SDRCapture


STAGES = (
    ("50OHM_A", "50_OHM_AT_LNA_INPUT"),
    ("ANTENNA_A", "ANTENNA_AT_LNA_INPUT_INDOOR"),
    ("50OHM_B", "50_OHM_AT_LNA_INPUT"),
    ("ANTENNA_B", "ANTENNA_AT_LNA_INPUT_INDOOR"),
)


async def run(args):
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    sdr = SDRCapture("network", args.host, args.port, verbose=True)
    records = []
    try:
        await sdr.connect()
        await sdr.configure(args.frequency, args.sample_rate, 40.2)
        for index, (label, rf_input) in enumerate(STAGES):
            if index:
                await asyncio.get_running_loop().run_in_executor(
                    None, input, f"READY REQUIRED FOR {label}; press Enter only after operator confirmation: "
                )
            path = output / f"{label.lower()}.h5"
            await sdr.capture(
                2.0, str(path), args.sample_rate,
                {
                    "test": "ALMITA-INDOOR-ANTENNA-COUPLING-CHECK-01",
                    "environment": "INDOOR_DEPARTMENT",
                    "condition": label,
                    "rf_input": rf_input,
                    "gain_requested_db": 40.2,
                },
            )
            metrics = analyze(path)
            row = {
                "condition": label,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "hdf5_path": str(path),
                **metrics,
            }
            records.append(row)
            print(
                f"RESULT {label} sigma={metrics['std_combined']:.9f} "
                f"psd={metrics['broadband_power']:.6f} "
                f"integrated={metrics['mean_psd_power']:.6f} "
                f"entropy={metrics['entropy_bits']:.6f} "
                f"range={metrics['p001']:.0f}..{metrics['p999']:.0f} "
                f"clipping={metrics['clipping_fraction']:.6%}", flush=True,
            )
    finally:
        await sdr.close()

    scalar_fields = [key for key, value in records[0].items() if not isinstance(value, list)]
    with (output / "indoor_coupling_metrics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=scalar_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    by_label = {record["condition"]: record for record in records}
    metrics = ("std_combined", "broadband_power", "mean_psd_power", "entropy_bits")
    repeatability = {}
    comparison = {}
    for condition in ("50OHM", "ANTENNA"):
        first, second = by_label[f"{condition}_A"], by_label[f"{condition}_B"]
        repeatability[condition] = {
            key: abs(first[key] - second[key]) / np.mean([first[key], second[key]])
            for key in metrics
        }
    for key in metrics:
        baseline = np.mean([by_label["50OHM_A"][key], by_label["50OHM_B"][key]])
        antenna = np.mean([by_label["ANTENNA_A"][key], by_label["ANTENNA_B"][key]])
        comparison[key] = {
            "mean_50ohm": float(baseline),
            "mean_antenna": float(antenna),
            "relative_difference": float((antenna - baseline) / baseline),
        }

    # Broadband consistency: exclude DC, outer 10%, and bins with extreme narrowband excess.
    psd_50 = np.mean([by_label["50OHM_A"]["psd"], by_label["50OHM_B"]["psd"]], axis=0)
    psd_ant = np.mean([by_label["ANTENNA_A"]["psd"], by_label["ANTENNA_B"]["psd"]], axis=0)
    count = len(psd_50)
    mask = np.ones(count, dtype=bool)
    mask[:count // 10] = False
    mask[-count // 10:] = False
    mask[count // 2 - 8:count // 2 + 9] = False
    ratio_db = 10 * np.log10(np.maximum(psd_ant, 1e-30) / np.maximum(psd_50, 1e-30))
    center = np.median(ratio_db[mask])
    spread = np.median(np.abs(ratio_db[mask] - center))
    broadband_mask = mask & (np.abs(ratio_db - center) <= max(0.25, 6 * spread))
    broadband_fraction_antenna_higher = float(np.mean(psd_ant[broadband_mask] > psd_50[broadband_mask]))
    sigma_effect = abs(comparison["std_combined"]["relative_difference"])
    sigma_variability = max(
        repeatability["50OHM"]["std_combined"],
        repeatability["ANTENNA"]["std_combined"],
    )
    psd_effect = abs(comparison["broadband_power"]["relative_difference"])
    psd_variability = max(
        repeatability["50OHM"]["broadband_power"],
        repeatability["ANTENNA"]["broadband_power"],
    )
    passed = (
        sigma_effect > 3 * max(sigma_variability, 1e-6)
        and psd_effect > 3 * max(psd_variability, 1e-6)
        and (broadband_fraction_antenna_higher >= 0.75 or broadband_fraction_antenna_higher <= 0.25)
        and all(record["clipping_fraction"] == 0 for record in records)
    )
    summary = {
        "test": "ALMITA-INDOOR-ANTENNA-COUPLING-CHECK-01",
        "environment": "INDOOR_DEPARTMENT",
        "frequency_hz": args.frequency,
        "sample_rate_hz": args.sample_rate,
        "gain_db": 40.2,
        "duration_seconds": 2.0,
        "records": [{key: value for key, value in record.items() if key not in ("psd", "frequency_hz", "histogram")} for record in records],
        "repeatability_relative": repeatability,
        "condition_comparison": comparison,
        "broadband_fraction_antenna_higher": broadband_fraction_antenna_higher,
        "antenna_vs_50ohm_relative_response": "PASS" if passed else "INCONCLUSIVE",
        "astronomical_interpretation": "NOT_PERMITTED",
    }
    (output / "indoor_coupling_summary.json").write_text(json.dumps(summary, indent=2))

    frequency = np.asarray(records[0]["frequency_hz"]) / 1e6
    figure, axis = plt.subplots(figsize=(10, 5))
    for record in records:
        axis.plot(frequency, 10 * np.log10(np.maximum(record["psd"], 1e-30)), label=record["condition"])
    axis.set(xlabel="Frequency (MHz)", ylabel="PSD (dB arbitrary)")
    axis.grid(True)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output / "indoor_50ohm_antenna_spectra.png", dpi=150)
    plt.close(figure)
    print(json.dumps(summary, indent=2), flush=True)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1234)
    parser.add_argument("--frequency", type=int, default=1420405752)
    parser.add_argument("--sample-rate", type=int, default=2400000)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
