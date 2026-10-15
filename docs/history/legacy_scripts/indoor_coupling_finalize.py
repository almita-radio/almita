#!/usr/bin/env python3
"""Finalize the four-stage indoor load-change comparison."""

import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from rf_chain_characterization import analyze


def relative_difference(a, b):
    return float(abs(a - b) / np.mean([a, b]))


def run(directory):
    output = Path(directory)
    labels = ("50OHM_A", "ANTENNA_A", "50OHM_B", "ANTENNA_B")
    records = {
        label: analyze(output / f"{label.lower()}.h5") for label in labels
    }
    scalar_keys = (
        "std_combined", "broadband_power", "mean_psd_power", "entropy_bits",
        "p001", "p999", "clipping_fraction",
    )
    repeatability = {
        "50OHM": {
            key: relative_difference(records["50OHM_A"][key], records["50OHM_B"][key])
            for key in scalar_keys[:-3]
        },
        "ANTENNA": {
            key: relative_difference(records["ANTENNA_A"][key], records["ANTENNA_B"][key])
            for key in scalar_keys[:-3]
        },
    }
    comparison = {}
    for key in scalar_keys:
        baseline = float(np.mean([records["50OHM_A"][key], records["50OHM_B"][key]]))
        antenna = float(np.mean([records["ANTENNA_A"][key], records["ANTENNA_B"][key]]))
        comparison[key] = {
            "mean_50ohm": baseline,
            "mean_antenna": antenna,
            "relative_difference": (antenna - baseline) / baseline if baseline else 0.0,
        }

    psd_50 = np.mean([records["50OHM_A"]["psd"], records["50OHM_B"]["psd"]], axis=0)
    psd_antenna = np.mean([records["ANTENNA_A"]["psd"], records["ANTENNA_B"]["psd"]], axis=0)
    count = len(psd_50)
    mask = np.ones(count, dtype=bool)
    mask[:count // 10] = False
    mask[-count // 10:] = False
    mask[count // 2 - 8:count // 2 + 9] = False
    ratio_db = 10 * np.log10(np.maximum(psd_antenna, 1e-30) / np.maximum(psd_50, 1e-30))
    median_ratio_db = float(np.median(ratio_db[mask]))
    fraction_higher = float(np.mean(psd_antenna[mask] > psd_50[mask]))
    sigma_effect = abs(comparison["std_combined"]["relative_difference"])
    psd_effect = abs(comparison["broadband_power"]["relative_difference"])
    sigma_variability = max(
        repeatability["50OHM"]["std_combined"], repeatability["ANTENNA"]["std_combined"]
    )
    psd_variability = max(
        repeatability["50OHM"]["broadband_power"], repeatability["ANTENNA"]["broadband_power"]
    )
    response = "PASS" if (
        sigma_effect > 3 * sigma_variability
        and psd_effect > 3 * psd_variability
        and (fraction_higher >= 0.75 or fraction_higher <= 0.25)
        and all(records[label]["clipping_fraction"] == 0 for label in labels)
    ) else "INCONCLUSIVE"
    summary = {
        "test": "ALMITA-INDOOR-ANTENNA-COUPLING-CHECK-01",
        "environment": "INDOOR_DEPARTMENT",
        "configuration": {"frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain_db": 40.2, "duration_seconds": 2.0},
        "connection_limitation": "50OHM_A used initial persistent connection; later stages used new identical connections after USB transfer status 5",
        "records": {
            label: {key: records[label][key] for key in scalar_keys} for label in labels
        },
        "repeatability_relative": repeatability,
        "condition_comparison": comparison,
        "broadband_median_ratio_db": median_ratio_db,
        "broadband_fraction_antenna_higher": fraction_higher,
        "50ohm_repeatability": "PASS" if max(repeatability["50OHM"].values()) < 0.01 else "INCONCLUSIVE",
        "antenna_repeatability": "PASS" if max(repeatability["ANTENNA"].values()) < 0.01 else "INCONCLUSIVE",
        "antenna_vs_50ohm_relative_response": response,
        "astronomical_interpretation": "NOT_PERMITTED",
    }
    (output / "indoor_coupling_summary.json").write_text(json.dumps(summary, indent=2))
    with (output / "indoor_coupling_metrics.csv").open("w", newline="") as handle:
        fields = ["condition", *scalar_keys]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for label in labels:
            writer.writerow({"condition": label, **{key: records[label][key] for key in scalar_keys}})
    frequency = np.asarray(records[labels[0]]["frequency_hz"]) / 1e6
    figure, axis = plt.subplots(figsize=(10, 5))
    for label in labels:
        axis.plot(frequency, 10 * np.log10(np.maximum(records[label]["psd"], 1e-30)), label=label)
    axis.set(xlabel="Frequency (MHz)", ylabel="PSD (dB arbitrary)")
    axis.grid(True)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output / "indoor_50ohm_antenna_spectra.png", dpi=150)
    plt.close(figure)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    run(parser.parse_args().directory)
