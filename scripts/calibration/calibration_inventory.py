#!/usr/bin/env python3
"""Inventory candidate 50-ohm calibration captures and selection reasons."""

import argparse
import csv
from pathlib import Path

import h5py


CAMPAIGNS = (
    "RF-CHAIN-01-*", "RF-GAIN-ISOLATION-01-*",
    "SDR-CONFIG-SEQUENCE-FIX-01-*", "RF-CHAIN-ISOLATION-02-*",
    "INDOOR-ANTENNA-COUPLING-CHECK-01-*",
)


def run(args):
    root = Path(args.root)
    selected = {str(Path(path)) for path in args.selected}
    rows = []
    for pattern in CAMPAIGNS:
        for campaign in sorted(root.glob(pattern)):
            for path in sorted(campaign.rglob("*.h5")):
                with h5py.File(path, "r") as capture:
                    attrs = dict(capture.attrs)
                gain = attrs.get("gain_requested_db", attrs.get("gain"))
                rf_input = str(attrs.get("rf_input", ""))
                if "direct" in str(path).lower():
                    topology = "50_OHM_DIRECT_RTL_SDR"
                elif "antenna" in path.name.lower() or "ANTENNA" in rf_input:
                    topology = "ANTENNA_TO_LNA_FILTER_CABLING_TO_RTL_SDR"
                else:
                    topology = "50_OHM_TO_LNA_FILTER_CABLING_TO_RTL_SDR"
                path_text = str(path)
                if path_text in selected:
                    decision, reason = "SELECTED", "valid 40.2 dB productive-chain reference"
                elif topology != "50_OHM_TO_LNA_FILTER_CABLING_TO_RTL_SDR":
                    decision, reason = "EXCLUDED", "reference topology/input is not 50 ohm at LNA input"
                elif str(gain) not in ("40.2", "40.200000"):
                    decision, reason = "EXCLUDED", "gain differs from V1 profile gain 40.2 dB"
                elif "RF-CHAIN-01" in path_text:
                    decision, reason = "EXCLUDED", "pre SDR-sequence-fix campaign"
                else:
                    decision, reason = "EXCLUDED", "technically usable but redundant; bounded V1 subset selected"
                temperatures = [
                    float(value) for key, value in attrs.items()
                    if "temperature" in key.lower() and isinstance(value, (int, float))
                ]
                rows.append({
                    "source_file": path_text,
                    "campaign": campaign.name,
                    "timestamp": attrs.get("capture_start_utc", attrs.get("created_at")),
                    "gain_db": gain,
                    "center_frequency_hz": attrs.get("center_frequency_hz"),
                    "sample_rate_hz": attrs.get("sample_rate_hz"),
                    "duration_seconds": attrs.get("duration_seconds"),
                    "bias_t_state": "OFF" if topology == "50_OHM_DIRECT_RTL_SDR" else "ON",
                    "topology": topology,
                    "temperatures_c": temperatures,
                    "decision": decision,
                    "reason": reason,
                })
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"inventory_rows={len(rows)} selected={sum(row['decision']=='SELECTED' for row in rows)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--selected", nargs="+", required=True)
    run(parser.parse_args())
