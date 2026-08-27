#!/usr/bin/env python3
"""Build the Almita V1 relative calibration artifacts from selected captures."""

import argparse
import csv
import json
import resource
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from calibration_foundation import (
    apply_relative_calibration,
    build_calibration_profile,
    load_calibration_profile,
)


def run(args):
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    stem = output / "calibration_profile_v1"
    build_start = time.perf_counter()
    profile = build_calibration_profile(
        args.reference, stem, gain_db=40.2,
        reference_topology="50_OHM_TO_LNA_FILTER_CABLING_TO_RTL_SDR",
        bias_t_state="ON", fft_size=8192,
    )
    build_seconds = time.perf_counter() - build_start
    load_start = time.perf_counter()
    profile = load_calibration_profile(stem)
    load_seconds = time.perf_counter() - load_start
    metadata = profile["metadata"]

    with (output / "calibration_reference_summary.csv").open("w", newline="") as handle:
        fields = (
            "source_file", "timestamp", "gain_db", "center_frequency_hz",
            "sample_rate_hz", "duration_seconds", "bias_t_state", "topology",
            "temperatures_c", "adc_mean", "adc_sigma", "adc_p0_1",
            "adc_p99_9", "adc_entropy_bits", "adc_clipping_fraction",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in metadata["reference_records"]:
            adc = record["adc_statistics"]
            writer.writerow({
                **{key: record[key] for key in fields[:8]},
                "temperatures_c": json.dumps(record["temperatures_c"]),
                "adc_mean": adc["mean"], "adc_sigma": adc["sigma"],
                "adc_p0_1": adc["p0_1"], "adc_p99_9": adc["p99_9"],
                "adc_entropy_bits": adc["entropy_bits"],
                "adc_clipping_fraction": adc["clipping_fraction"],
            })

    frequency_mhz = profile["frequency_hz"] / 1e6
    valid = profile["valid_mask"].astype(bool)
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.plot(frequency_mhz, 10 * np.log10(np.maximum(profile["reference_psd"], 1e-30)))
    axis.fill_between(
        frequency_mhz, axis.get_ylim()[0], axis.get_ylim()[1],
        where=~valid, color="tab:red", alpha=0.15, label="masked",
    )
    axis.set(xlabel="Frequency (MHz)", ylabel="Reference PSD (dB arbitrary)")
    axis.grid(True)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output / "calibration_reference_spectrum.png", dpi=150)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(10, 5))
    axis.plot(frequency_mhz, profile["fractional_variability"] * 100)
    axis.set(xlabel="Frequency (MHz)", ylabel="Robust reference variability (%)")
    axis.set_ylim(0, np.nanpercentile(profile["fractional_variability"][valid] * 100, 99.5))
    axis.grid(True)
    figure.tight_layout()
    figure.savefig(output / "calibration_variability.png", dpi=150)
    plt.close(figure)

    validation = []
    apply_times = []
    figure, axis = plt.subplots(figsize=(10, 5))
    for capture in args.antenna:
        start = time.perf_counter()
        calibrated = apply_relative_calibration(profile, capture, scale_mode="none")
        apply_times.append(time.perf_counter() - start)
        selected = calibrated["fractional_excess"][valid]
        row = {
            "source_file": capture,
            "label": "INDOOR TEST — ASTRONOMICAL INTERPRETATION NOT PERMITTED",
            "compatibility": calibrated["compatibility"],
            "reference_scale": calibrated["reference_scale"],
            "masked_fraction": float(1 - np.mean(valid)),
            "residual_median": float(np.median(selected)),
            "residual_robust_sigma": float(1.4826 * np.median(np.abs(selected - np.median(selected)))),
            "residual_p01": float(np.percentile(selected, 1)),
            "residual_p99": float(np.percentile(selected, 99)),
        }
        validation.append(row)
        axis.plot(frequency_mhz[valid], calibrated["fractional_excess"][valid], label=Path(capture).stem)
    axis.set(xlabel="Frequency (MHz)", ylabel="Fractional excess")
    axis.grid(True)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output / "calibration_validation_indoor.png", dpi=150)
    plt.close(figure)

    performance = {
        "build_seconds": build_seconds,
        "load_seconds": load_seconds,
        "apply_seconds_each": apply_times,
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "npz_bytes": stem.with_suffix(".npz").stat().st_size,
        "json_bytes": stem.with_suffix(".json").stat().st_size,
    }
    validation_document = {
        "calibration_level": "RELATIVE_INSTRUMENTAL",
        "absolute_calibration": False,
        "environment": "INDOOR_DEPARTMENT",
        "astronomical_interpretation": "NOT_PERMITTED",
        "captures": validation,
        "performance": performance,
    }
    (output / "calibration_indoor_validation.json").write_text(
        json.dumps(validation_document, indent=2)
    )
    print(json.dumps({
        "profile": str(stem),
        "reference_count": metadata["reference_count"],
        "dc_mask_bins": metadata["dc_mask_bins"],
        "spur_count": len(metadata["spur_regions"]),
        "valid_fraction": metadata["valid_fraction"],
        "temperature_correction": metadata["temperature_correction"],
        "indoor_validation": validation,
        "performance": performance,
    }, indent=2))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--reference", nargs="+", required=True)
    parser.add_argument("--antenna", nargs="+", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
