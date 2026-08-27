#!/usr/bin/env python3
"""Offline ALMITA relative-instrumental spectrum Quicklook V1."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from calibration_foundation import (
    apply_relative_calibration,
    check_calibration_compatibility,
    load_calibration_profile,
)
from hi_spectral_metric import HI_REST_HZ

SCHEMA_VERSION = "1.0"
TITLE = "ALMITA — Quicklook Spectrum"


class QuicklookError(RuntimeError):
    """Clean user-facing Quicklook input or compatibility failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _profile_paths(path: str | Path) -> tuple[Path, Path, Path]:
    stem = Path(path)
    if stem.suffix in (".npz", ".json"):
        stem = stem.with_suffix("")
    return stem, stem.with_suffix(".npz"), stem.with_suffix(".json")


def _capture_metadata(path: Path) -> dict[str, Any]:
    try:
        with h5py.File(path, "r") as capture:
            if "iq_data" not in capture:
                raise QuicklookError("invalid HDF5: iq_data dataset is missing")
            return {
                key: value.item() if isinstance(value, np.generic) else value
                for key, value in capture.attrs.items()
            }
    except QuicklookError:
        raise
    except (OSError, KeyError, ValueError) as error:
        raise QuicklookError(f"invalid HDF5: {error}") from error


def _mask_regions(frequency_hz: np.ndarray, mask: np.ndarray):
    indices = np.flatnonzero(mask)
    if not indices.size:
        return []
    starts = np.r_[0, np.flatnonzero(np.diff(indices) > 1) + 1]
    ends = np.r_[starts[1:], len(indices)]
    bin_hz = float(np.median(np.diff(frequency_hz)))
    return [
        (
            float(frequency_hz[indices[start]] - bin_hz / 2),
            float(frequency_hz[indices[end - 1]] + bin_hz / 2),
        )
        for start, end in zip(starts, ends)
    ]


def _robust_limits(values: np.ndarray, valid: np.ndarray, low=1.0, high=99.0):
    selected = np.asarray(values)[valid & np.isfinite(values)]
    if selected.size < 16:
        raise QuicklookError("insufficient valid bins for robust autoscale")
    lower, upper = np.percentile(selected, [low, high])
    padding = max(float(upper - lower) * 0.08, 1e-6)
    return float(lower - padding), float(upper + padding)


def _draw_masks(axis, frequency_hz, dc_mask, spur_mask):
    for index, (lo, hi) in enumerate(_mask_regions(frequency_hz, dc_mask)):
        axis.axvspan(lo / 1e6, hi / 1e6, color="tab:red", alpha=0.20,
                    label="zero-IF masked" if index == 0 else None)
    for index, (lo, hi) in enumerate(_mask_regions(frequency_hz, spur_mask)):
        axis.axvspan(lo / 1e6, hi / 1e6, color="tab:orange", alpha=0.25,
                    label="instrumental spur masked" if index == 0 else None)


def _write_plots(output: Path, document: dict[str, Any], arrays: dict[str, np.ndarray],
                 capture_metadata: dict[str, Any]) -> float:
    started = time.perf_counter()
    frequency_hz = arrays["frequency_hz"]
    frequency_mhz = frequency_hz / 1e6
    valid = arrays["valid_mask"]
    relative_db = arrays["relative_psd_db"]
    fractional = arrays["fractional_excess"]
    uncertainty = arrays["fractional_uncertainty"]
    dc = arrays["dc_mask"]
    spur = arrays["spur_mask"]
    metadata_line = (
        f"UTC {document['capture_utc']}   gain {document['gain_db']:.1f} dB   "
        f"duration {document['capture_duration_seconds']:.2f} s   "
        f"sample rate {document['sample_rate_hz']/1e6:.1f} MS/s   RELATIVE_INSTRUMENTAL"
    )

    figure, axis = plt.subplots(figsize=(12, 6))
    axis.plot(frequency_mhz[valid], relative_db[valid], linewidth=0.8, color="tab:blue")
    axis.axvline(HI_REST_HZ / 1e6, color="tab:green", linestyle="--", linewidth=1,
                 label="HI rest frequency (marker only)")
    _draw_masks(axis, frequency_hz, dc, spur)
    axis.set_ylim(*_robust_limits(relative_db, valid))
    axis.set(xlabel="Frequency (MHz)", ylabel="Relative PSD (dB)", title=TITLE)
    axis.text(0.01, 0.01, metadata_line, transform=axis.transAxes, fontsize=8)
    axis.grid(True, alpha=0.3)
    axis.legend(fontsize=8, loc="upper right")
    figure.tight_layout()
    figure.savefig(output / "quicklook_spectrum.png", dpi=150)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(12, 6))
    axis.plot(frequency_mhz[valid], fractional[valid], linewidth=0.7,
              color="tab:purple", label="fractional excess")
    axis.fill_between(
        frequency_mhz[valid], -uncertainty[valid], uncertainty[valid],
        color="tab:gray", alpha=0.22, label="reference ± robust uncertainty",
    )
    axis.axvline(HI_REST_HZ / 1e6, color="tab:green", linestyle="--", linewidth=1,
                 label="HI rest frequency (marker only)")
    _draw_masks(axis, frequency_hz, dc, spur)
    axis.set_ylim(*_robust_limits(fractional, valid))
    axis.set(
        xlabel="Frequency (MHz)", ylabel="Fractional excess",
        title="ALMITA — Quicklook Fractional Excess",
    )
    axis.text(0.01, 0.01, metadata_line, transform=axis.transAxes, fontsize=8)
    axis.grid(True, alpha=0.3)
    axis.legend(fontsize=8, loc="upper right")
    figure.tight_layout()
    figure.savefig(output / "quicklook_fractional_excess.png", dpi=150)
    plt.close(figure)
    return time.perf_counter() - started


def generate_quicklook(
    source_hdf5: str | Path, calibration_profile: str | Path,
    output_dir: str | Path,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Generate dashboard-ready JSON and two diagnostic PNG products."""
    total_start = time.perf_counter()
    source = Path(source_hdf5)
    if source.name.endswith(".part"):
        raise QuicklookError("partial HDF5 files are not accepted")
    if not source.is_file():
        raise QuicklookError(f"source HDF5 does not exist: {source}")
    stem, profile_npz, profile_json = _profile_paths(calibration_profile)
    if not profile_npz.is_file() or not profile_json.is_file():
        raise QuicklookError(f"calibration profile pair is incomplete: {stem}")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    integrity_before = {
        "source_hdf5_sha256": _sha256(source),
        "profile_npz_sha256": _sha256(profile_npz),
        "profile_json_sha256": _sha256(profile_json),
    }
    capture_metadata = _capture_metadata(source)
    try:
        profile = load_calibration_profile(stem)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise QuicklookError(f"invalid calibration profile: {error}") from error
    compatibility = check_calibration_compatibility(profile, source)
    if compatibility["status"] != "COMPATIBLE":
        raise QuicklookError(
            f"calibration compatibility {compatibility['status']}: {compatibility['reason']}"
        )
    calibration_start = time.perf_counter()
    calibrated = apply_relative_calibration(profile, source, scale_mode="none")
    calibration_seconds = time.perf_counter() - calibration_start
    arrays = {
        "frequency_hz": np.asarray(calibrated["frequency_hz"]),
        "relative_psd_db": np.asarray(calibrated["relative_psd_db"]),
        "fractional_excess": np.asarray(calibrated["fractional_excess"]),
        "fractional_uncertainty": np.asarray(calibrated["fractional_uncertainty"]),
        "valid_mask": np.asarray(calibrated["valid_mask"], dtype=bool),
        "dc_mask": np.asarray(profile["dc_mask"], dtype=bool),
        "spur_mask": np.asarray(profile["spur_mask"], dtype=bool),
    }
    valid = arrays["valid_mask"]
    residual = arrays["fractional_excess"][valid]
    median = float(np.median(residual))
    gain_value = capture_metadata.get("gain_requested_db", capture_metadata.get("gain"))
    document = {
        "schema_version": SCHEMA_VERSION,
        "status": "SUCCESS",
        "source_hdf5": str(source),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "capture_utc": capture_metadata.get("capture_start_utc", capture_metadata.get("created_at")),
        "environment": capture_metadata.get("environment", "UNKNOWN"),
        "astronomical_interpretation": "NOT_PERMITTED" if capture_metadata.get("environment") == "INDOOR_DEPARTMENT" else "NOT_ASSERTED",
        "calibration_level": calibrated["calibration_metadata"]["calibration_level"],
        "absolute_calibration": calibrated["calibration_metadata"]["absolute_calibration"],
        "calibration_profile": str(stem),
        "compatibility": compatibility,
        "center_frequency_hz": int(capture_metadata["center_frequency_hz"]),
        "sample_rate_hz": int(capture_metadata["sample_rate_hz"]),
        "gain_db": float(gain_value),
        "capture_duration_seconds": float(capture_metadata.get("duration_seconds", capture_metadata.get("capture_time_seconds", 0))),
        "fft_size": int(calibrated["calibration_metadata"]["fft_size"]),
        "processing": {
            "sample_fraction": 1.0,
            "sample_selection": "FULL_CAPTURE_VIA_CALIBRATION_FOUNDATION_PUBLIC_API",
            "reference_scale_mode": calibrated["scale_mode"],
            "autoscale_percentiles": [1.0, 99.0],
        },
        "frequency_hz": arrays["frequency_hz"].tolist(),
        "relative_psd_db": arrays["relative_psd_db"].tolist(),
        "fractional_excess": arrays["fractional_excess"].tolist(),
        "fractional_uncertainty": arrays["fractional_uncertainty"].tolist(),
        "valid_mask": arrays["valid_mask"].tolist(),
        "dc_mask": arrays["dc_mask"].tolist(),
        "spur_mask": arrays["spur_mask"].tolist(),
        "masked_fraction": float(1.0 - np.mean(valid)),
        "quicklook_metrics": {
            "valid_fraction": float(np.mean(valid)),
            "masked_fraction": float(1.0 - np.mean(valid)),
            "median_fractional_excess": median,
            "robust_sigma_fractional_excess": float(1.4826 * np.median(np.abs(residual - median))),
            "max_positive_fractional_excess": float(np.max(residual)),
            "min_fractional_excess": float(np.min(residual)),
        },
        "known_limitations": [
            "relative/instrumental calibration only",
            "no spectral feature is identified as astronomical HI",
            "full HDF5 processing retained because public calibration apply is already lightweight",
            "indoor captures permit no astronomical interpretation",
        ],
        "performance": {
            "hdf5_read_and_calibration_seconds": calibration_seconds,
            "json_serialization_seconds": None,
            "png_generation_seconds": None,
            "total_seconds": None,
            "peak_rss_mib": None,
            "json_bytes": None,
            "spectrum_png_bytes": None,
            "fractional_png_bytes": None,
        },
        "integrity": {**integrity_before, "unchanged": None},
    }
    png_seconds = _write_plots(output, document, arrays, capture_metadata)
    document["performance"]["png_generation_seconds"] = png_seconds
    serialization_start = time.perf_counter()
    json.dumps(document, separators=(",", ":"))
    serialization_seconds = time.perf_counter() - serialization_start
    document["performance"]["json_serialization_seconds"] = serialization_seconds
    integrity_after = {
        "source_hdf5_sha256": _sha256(source),
        "profile_npz_sha256": _sha256(profile_npz),
        "profile_json_sha256": _sha256(profile_json),
    }
    document["integrity"]["unchanged"] = integrity_before == integrity_after
    document["performance"]["total_seconds"] = time.perf_counter() - total_start
    document["performance"]["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    json_path = output / "quicklook_spectrum.json"
    json_path.write_text(json.dumps(document, separators=(",", ":")))
    document["performance"]["json_bytes"] = json_path.stat().st_size
    document["performance"]["spectrum_png_bytes"] = (output / "quicklook_spectrum.png").stat().st_size
    document["performance"]["fractional_png_bytes"] = (output / "quicklook_fractional_excess.png").stat().st_size
    json_path.write_text(json.dumps(document, separators=(",", ":")))
    return document, arrays


def validate_against_calibration_foundation(
    document: dict[str, Any], arrays: dict[str, np.ndarray], source_hdf5: str | Path,
    calibration_profile: str | Path,
) -> dict[str, Any]:
    profile = load_calibration_profile(calibration_profile)
    expected = apply_relative_calibration(profile, source_hdf5, scale_mode="none")
    differences = {}
    for key in ("fractional_excess", "relative_psd_db", "fractional_uncertainty"):
        delta = np.abs(arrays[key] - np.asarray(expected[key]))
        differences[key] = {
            "max_abs_difference": float(np.max(delta)),
            "median_abs_difference": float(np.median(delta)),
        }
    return {
        "status": "PASS" if all(item["max_abs_difference"] == 0 for item in differences.values()) else "FAIL",
        "source_hdf5": document["source_hdf5"],
        "calibration_profile": document["calibration_profile"],
        "array_differences": differences,
        "valid_mask_equal": bool(np.array_equal(arrays["valid_mask"], expected["valid_mask"])),
        "calibration_level": document["calibration_level"],
        "absolute_calibration": document["absolute_calibration"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_hdf5")
    parser.add_argument("--calibration-profile", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    try:
        document, arrays = generate_quicklook(
            args.source_hdf5, args.calibration_profile, args.output_dir
        )
        validation = validate_against_calibration_foundation(
            document, arrays, args.source_hdf5, args.calibration_profile
        )
        Path(args.output_dir, "quicklook_validation.json").write_text(
            json.dumps(validation, indent=2)
        )
        print(json.dumps({
            "status": document["status"],
            "output_dir": args.output_dir,
            "compatibility": document["compatibility"],
            "quicklook_metrics": document["quicklook_metrics"],
            "performance": document["performance"],
            "validation": validation,
        }, indent=2))
    except QuicklookError as error:
        parser.exit(2, f"quicklook error: {error}\n")


if __name__ == "__main__":
    main()
