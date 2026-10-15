#!/usr/bin/env python3
"""Build RF-CHAIN-ISOLATION-02 comparison products from captured evidence."""

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from rf_chain_characterization import analyze


def run(args):
    output = Path(args.output_dir)
    chain_summary = json.loads(Path(args.chain_summary).read_text())
    chain_by_gain = {
        float(row["gain_requested_db"]): row for row in chain_summary["records"]
    }
    rows = []
    direct_records = []
    for gain in args.gains:
        direct_path = output / "direct" / f"rtl_direct_50ohm_gain_{gain:04.1f}.h5"
        direct = analyze(direct_path)
        direct_records.append({"gain_requested_db": gain, **direct})
        chain = chain_by_gain[gain]
        sigma_ratio = chain["std_combined"] / direct["std_combined"]
        psd_ratio = chain["broadband_power"] / direct["broadband_power"]
        rows.append({
            "gain_db": gain,
            "sigma_chain": chain["std_combined"],
            "sigma_direct": direct["std_combined"],
            "sigma_chain_vs_direct_db": 20 * math.log10(sigma_ratio),
            "psd_median_chain": chain["broadband_power"],
            "psd_median_direct": direct["broadband_power"],
            "psd_chain_vs_direct_db": 10 * math.log10(psd_ratio),
            "integrated_power_chain": chain["mean_psd_power"],
            "integrated_power_direct": direct["mean_psd_power"],
            "adc_range_chain_codes": chain["p999"] - chain["p001"],
            "adc_range_direct_codes": direct["p999"] - direct["p001"],
            "entropy_chain_bits": chain["entropy_bits"],
            "entropy_direct_bits": direct["entropy_bits"],
            "clipping_chain": chain["clipping_fraction"],
            "clipping_direct": direct["clipping_fraction"],
            "chain_hdf5": chain["hdf5_path"],
            "direct_hdf5": str(direct_path),
        })

    with (output / "chain_vs_direct.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    direct_scalar = [
        key for key, value in direct_records[0].items() if not isinstance(value, list)
    ]
    with (output / "rtl_direct_50ohm.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=direct_scalar, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(direct_records)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    gains = [row["gain_db"] for row in rows]
    axes[0].plot(gains, [row["sigma_chain"] for row in rows], "o-", label="LNA chain")
    axes[0].plot(gains, [row["sigma_direct"] for row in rows], "o-", label="RTL direct")
    axes[0].set(xlabel="Gain (dB)", ylabel="ADC sigma (codes)")
    axes[1].plot(gains, [row["entropy_chain_bits"] for row in rows], "o-", label="LNA chain")
    axes[1].plot(gains, [row["entropy_direct_bits"] for row in rows], "o-", label="RTL direct")
    axes[1].set(xlabel="Gain (dB)", ylabel="Entropy (bits)")
    for axis in axes:
        axis.grid(True)
        axis.legend()
    fig.tight_layout()
    fig.savefig(output / "adc_chain_vs_direct.png", dpi=150)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(10, 5))
    for gain in args.gains:
        chain = chain_by_gain[gain]
        direct = next(row for row in direct_records if row["gain_requested_db"] == gain)
        axis.plot(
            np.asarray(chain["frequency_hz"]) / 1e6,
            10 * np.log10(np.maximum(chain["psd"], 1e-30)),
            label=f"chain {gain:.1f} dB",
        )
        axis.plot(
            np.asarray(direct["frequency_hz"]) / 1e6,
            10 * np.log10(np.maximum(direct["psd"], 1e-30)),
            "--",
            label=f"direct {gain:.1f} dB",
        )
    axis.set(xlabel="Frequency (MHz)", ylabel="PSD (dB arbitrary)")
    axis.grid(True)
    axis.legend(ncol=2, fontsize=8)
    fig.tight_layout()
    fig.savefig(output / "psd_chain_vs_direct.png", dpi=150)
    plt.close(fig)

    summary = {
        "test": "ALMITA-RF-CHAIN-ISOLATION-02",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "frequency_hz": 1420405752,
        "sample_rate_hz": 2400000,
        "duration_seconds": 2.0,
        "chain_configuration": "50_OHM_TO_LNA_FILTER_CABLING_TO_RTL_SDR; BIAS_T_ON",
        "direct_configuration": "50_OHM_DIRECT_TO_RTL_SDR; BIAS_T_OFF",
        "comparison": rows,
        "rtl_sdr_direct": "ABNORMAL",
        "lna_chain": "PASS",
        "adc_underutilization_source": "RTL-SDR SIDE",
        "interpretation": (
            "Direct input remains confined to three ADC codes. The LNA chain raises "
            "sigma and PSD at high tuner gain, so it is providing gain rather than "
            "acting as a complete attenuation/failure. Absolute ADC use remains abnormal."
        ),
        "pll": {
            "diagnostic_initialization_warnings": 2,
            "stable_capture_warnings": 0,
            "first_attempt": "USB/I2C error -4; discarded before valid capture",
        },
        "temperature_note": (
            "Test A contains sensor telemetry; direct captures did not record temperature. "
            "Runs were approximately 51 minutes apart, so exact temperature equivalence "
            "cannot be demonstrated."
        ),
        "restoration": {
            "physical_chain": "OPERATOR_CONFIRMED_RESTORED",
            "service": "ACTIVE",
            "pid": 60612,
            "nrestarts": 0,
            "bias_t_argument": "-T PRESENT",
        },
    }
    (output / "rf_chain_isolation_summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    print(json.dumps(summary, indent=2))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--chain-summary", required=True)
    parser.add_argument("--gains", nargs="+", type=float, default=[0.0, 40.2, 49.6])
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
