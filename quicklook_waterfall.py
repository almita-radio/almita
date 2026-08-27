#!/usr/bin/env python3
"""Offline ALMITA relative-instrumental Waterfall V1."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import time
from datetime import datetime, timezone
from pathlib import Path

import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from calibration_foundation import (
    apply_relative_calibration,
    apply_relative_calibration_to_psd,
    check_calibration_compatibility,
    load_calibration_profile,
)
from hi_spectral_metric import HI_REST_HZ, robust_psd_from_iq

SCHEMA_VERSION = "1.0"


class WaterfallError(RuntimeError):
    """Clean user-facing Waterfall input or compatibility failure."""


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _profile_paths(path):
    stem = Path(path)
    if stem.suffix in (".npz", ".json"):
        stem = stem.with_suffix("")
    return stem, stem.with_suffix(".npz"), stem.with_suffix(".json")


def _metadata(path):
    try:
        with h5py.File(path, "r") as capture:
            if "iq_data" not in capture:
                raise WaterfallError("invalid HDF5: iq_data dataset is missing")
            return {
                key: value.item() if isinstance(value, np.generic) else value
                for key, value in capture.attrs.items()
            }
    except WaterfallError:
        raise
    except (OSError, ValueError, KeyError) as error:
        raise WaterfallError(f"invalid HDF5: {error}") from error


def make_time_bin_bounds(complex_samples: int, requested_bins: int, fft_size: int):
    """Return contiguous FFT-aligned bounds covering every complete segment."""
    if requested_bins < 1:
        raise ValueError("time_bins must be positive")
    complete_segments = complex_samples // fft_size
    maximum = complete_segments // 4
    if maximum < 1:
        raise ValueError("capture has insufficient samples for four FFT segments")
    count = min(requested_bins, maximum)
    segment_edges = np.linspace(0, complete_segments, count + 1, dtype=np.int64)
    sample_edges = segment_edges * fft_size
    return np.column_stack((sample_edges[:-1], sample_edges[1:]))


def compute_native_waterfall(source_hdf5, profile, *, time_bins=64):
    """Read one HDF5 incrementally and calibrate each contiguous temporal bin."""
    source = Path(source_hdf5)
    fft_size = int(profile["metadata"]["fft_size"])
    sample_rate = float(profile["metadata"]["sample_rate_hz"])
    read_seconds = psd_seconds = calibration_seconds = 0.0
    with h5py.File(source, "r") as capture:
        dataset = capture["iq_data"]
        if dataset.ndim != 1 or dataset.size % 2:
            raise WaterfallError("iq_data must be flat interleaved I/Q bytes")
        complex_samples = dataset.size // 2
        bounds = make_time_bin_bounds(complex_samples, time_bins, fft_size)
        rows = np.empty((len(bounds), fft_size), dtype=np.float32)
        frequency = None
        used_samples = 0
        for index, (start, end) in enumerate(bounds):
            read_start = time.perf_counter()
            iq = dataset[int(start * 2):int(end * 2)]
            read_seconds += time.perf_counter() - read_start
            psd_start = time.perf_counter()
            row_frequency, psd = robust_psd_from_iq(
                iq, sample_rate, profile["metadata"]["center_frequency_hz"],
                fft_size=fft_size, combine="median",
            )
            psd_seconds += time.perf_counter() - psd_start
            calibration_start = time.perf_counter()
            calibrated = apply_relative_calibration_to_psd(
                profile, row_frequency, psd, scale_mode="none"
            )
            calibration_seconds += time.perf_counter() - calibration_start
            rows[index] = calibrated["fractional_excess"].astype(np.float32)
            frequency = row_frequency
            used_samples += ((int(end) - int(start)) // fft_size) * fft_size
    valid = profile["valid_mask"].astype(bool)
    rows[:, ~valid] = np.nan
    starts = bounds[:, 0] / sample_rate
    ends = bounds[:, 1] / sample_rate
    return {
        "frequency_hz": frequency,
        "fractional_excess": rows,
        "fractional_uncertainty": (
            profile["reference_sigma"] / np.maximum(profile["reference_psd"], 1e-30)
        ).astype(np.float32),
        "valid_mask": valid,
        "dc_mask": profile["dc_mask"].astype(bool),
        "spur_mask": profile["spur_mask"].astype(bool),
        "time_bin_start_seconds": starts,
        "time_bin_end_seconds": ends,
        "time_bin_mid_seconds": (starts + ends) / 2,
        "elapsed_seconds": ends,
        "coverage": {
            "complex_samples_total": int(complex_samples),
            "complex_samples_in_time_bounds": int(np.sum(bounds[:, 1] - bounds[:, 0])),
            "complex_samples_used_in_complete_ffts": int(used_samples),
            "trailing_samples_below_one_fft_discarded": int(complex_samples - bounds[-1, 1]),
        },
        "performance": {
            "hdf5_read_seconds": read_seconds,
            "psd_seconds": psd_seconds,
            "calibration_seconds": calibration_seconds,
        },
    }


def decimate_dashboard(frequency_hz, waterfall, valid_mask, frequency_bins=512):
    """Median-decimate native bins while carrying validity by group."""
    native_bins = len(frequency_hz)
    target = min(int(frequency_bins), native_bins)
    if target < 1:
        raise ValueError("frequency_bins must be positive")
    groups = np.array_split(np.arange(native_bins), target)
    frequency = np.asarray([np.median(frequency_hz[group]) for group in groups])
    validity = np.asarray([np.any(valid_mask[group]) for group in groups], dtype=bool)
    values = np.empty((waterfall.shape[0], target), dtype=np.float32)
    for index, group in enumerate(groups):
        group_valid = group[valid_mask[group]]
        if group_valid.size:
            values[:, index] = np.nanmedian(waterfall[:, group_valid], axis=1)
        else:
            values[:, index] = np.nan
    return frequency, values, validity


def _finite_json_matrix(values):
    return [
        [float(value) if np.isfinite(value) else None for value in row]
        for row in np.asarray(values)
    ]


def _row_metrics(waterfall, valid):
    rows = []
    for row in waterfall:
        selected = row[valid & np.isfinite(row)]
        median = float(np.median(selected))
        rows.append({
            "median_fractional_excess": median,
            "robust_sigma": float(1.4826 * np.median(np.abs(selected - median))),
            "max_valid": float(np.max(selected)),
            "min_valid": float(np.min(selected)),
            "valid_fraction": float(len(selected) / len(row)),
        })
    return rows


def _plots(output, document, native, color_min, color_max):
    start = time.perf_counter()
    masked = np.ma.masked_invalid(native["fractional_excess"])
    frequency_mhz = native["frequency_hz"] / 1e6
    starts, ends = native["time_bin_start_seconds"], native["time_bin_end_seconds"]
    figure, axis = plt.subplots(figsize=(12, 7))
    image = axis.imshow(
        masked, origin="lower", aspect="auto", interpolation="nearest",
        extent=[frequency_mhz[0], frequency_mhz[-1], starts[0], ends[-1]],
        cmap="viridis", vmin=color_min, vmax=color_max,
    )
    axis.axvline(HI_REST_HZ / 1e6, color="white", linestyle="--", linewidth=0.9,
                 label="HI rest frequency (marker only)")
    axis.set(
        xlabel="Frequency (MHz)", ylabel="Elapsed Time (s)",
        title="ALMITA — Quicklook Waterfall",
    )
    axis.text(
        0.01, 0.01,
        f"gain {document['gain_db']:.1f} dB   duration {document['capture_duration_seconds']:.2f} s   RELATIVE_INSTRUMENTAL",
        transform=axis.transAxes, color="white", fontsize=8,
    )
    axis.legend(loc="upper right", fontsize=8)
    figure.colorbar(image, ax=axis, label="Relative Instrumental Excess")
    figure.tight_layout()
    figure.savefig(output / "quicklook_waterfall.png", dpi=150)
    plt.close(figure)

    row_median = [row["median_fractional_excess"] for row in document["quicklook_metrics"]["rows"]]
    row_sigma = [row["robust_sigma"] for row in document["quicklook_metrics"]["rows"]]
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.plot(native["time_bin_mid_seconds"], row_median, label="row median")
    axis.plot(native["time_bin_mid_seconds"], row_sigma, label="row robust sigma")
    axis.set(xlabel="Elapsed Time (s)", ylabel="Fractional units",
             title="ALMITA — Waterfall Stability")
    axis.grid(True)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output / "quicklook_waterfall_stability.png", dpi=150)
    plt.close(figure)
    return time.perf_counter() - start


def generate_waterfall(source_hdf5, calibration_profile, output_dir,
                       *, time_bins=64, frequency_bins=512):
    total_start = time.perf_counter()
    source = Path(source_hdf5)
    if source.name.endswith(".part"):
        raise WaterfallError("partial HDF5 files are not accepted")
    if not source.is_file():
        raise WaterfallError(f"source HDF5 does not exist: {source}")
    stem, profile_npz, profile_json = _profile_paths(calibration_profile)
    if not profile_npz.is_file() or not profile_json.is_file():
        raise WaterfallError(f"calibration profile pair is incomplete: {stem}")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    metadata = _metadata(source)
    before = {
        "source_hdf5_sha256": _sha256(source),
        "profile_npz_sha256": _sha256(profile_npz),
        "profile_json_sha256": _sha256(profile_json),
    }
    profile = load_calibration_profile(stem)
    compatibility = check_calibration_compatibility(profile, source)
    if compatibility["status"] != "COMPATIBLE":
        raise WaterfallError(
            f"calibration compatibility {compatibility['status']}: {compatibility['reason']}"
        )
    native = compute_native_waterfall(source, profile, time_bins=time_bins)
    decimation_start = time.perf_counter()
    dashboard_frequency, dashboard_values, dashboard_valid = decimate_dashboard(
        native["frequency_hz"], native["fractional_excess"], native["valid_mask"],
        frequency_bins=frequency_bins,
    )
    decimation_seconds = time.perf_counter() - decimation_start
    valid_values = native["fractional_excess"][:, native["valid_mask"]]
    finite = valid_values[np.isfinite(valid_values)]
    color_min, color_max = (float(value) for value in np.percentile(finite, [2, 98]))
    row_metrics = _row_metrics(native["fractional_excess"], native["valid_mask"])
    absolute_index = int(np.nanargmax(np.abs(native["fractional_excess"])))
    row_index, frequency_index = np.unravel_index(
        absolute_index, native["fractional_excess"].shape
    )
    gain = metadata.get("gain_requested_db", metadata.get("gain"))
    document = {
        "schema_version": SCHEMA_VERSION,
        "status": "SUCCESS",
        "source_hdf5": str(source),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "calibration_level": profile["metadata"]["calibration_level"],
        "absolute_calibration": profile["metadata"]["absolute_calibration"],
        "calibration_profile": str(stem),
        "compatibility": compatibility,
        "environment": metadata.get("environment", "UNKNOWN"),
        "astronomical_interpretation": "NOT_PERMITTED" if metadata.get("environment") == "INDOOR_DEPARTMENT" else "NOT_ASSERTED",
        "center_frequency_hz": int(metadata["center_frequency_hz"]),
        "sample_rate_hz": int(metadata["sample_rate_hz"]),
        "gain_db": float(gain),
        "capture_duration_seconds": float(metadata.get("duration_seconds", native["time_bin_end_seconds"][-1])),
        "fft_size": int(profile["metadata"]["fft_size"]),
        "time_bins": int(len(native["time_bin_mid_seconds"])),
        "frequency_bins_native": int(len(native["frequency_hz"])),
        "frequency_bins_dashboard": int(len(dashboard_frequency)),
        "time_axis_seconds": native["time_bin_mid_seconds"].tolist(),
        "time_bin_start_seconds": native["time_bin_start_seconds"].tolist(),
        "time_bin_end_seconds": native["time_bin_end_seconds"].tolist(),
        "frequency_mhz_dashboard": (dashboard_frequency / 1e6).tolist(),
        "waterfall_dashboard": _finite_json_matrix(dashboard_values),
        "dashboard_valid_mask": dashboard_valid.tolist(),
        "valid_fraction": float(np.mean(native["valid_mask"])),
        "masked_fraction": float(1 - np.mean(native["valid_mask"])),
        "color_scale": {
            "minimum": color_min, "maximum": color_max,
            "units": "fractional_relative_instrumental",
            "method": "global percentiles 2–98 over finite native values and valid_mask",
        },
        "quicklook_metrics": {
            "rows": row_metrics,
            "temporal_rms_of_row_medians": float(np.std([row["median_fractional_excess"] for row in row_metrics])),
            "spectral_rms_global": float(np.std(finite)),
            "maximum_excursion": float(native["fractional_excess"][row_index, frequency_index]),
            "maximum_excursion_label": "FEATURE / OUTLIER",
            "time_bin_of_maximum_excursion": int(row_index),
            "time_seconds_of_maximum_excursion": float(native["time_bin_mid_seconds"][row_index]),
            "frequency_hz_of_maximum_excursion": float(native["frequency_hz"][frequency_index]),
        },
        "native_npz": "quicklook_waterfall.npz",
        "png": "quicklook_waterfall.png",
        "known_limitations": [
            "relative/instrumental calibration only",
            "no feature is automatically classified as RFI or astronomical HI",
            "per-row PSD is a median of fewer FFT segments than the full-spectrum product",
            "indoor captures permit no astronomical interpretation",
        ],
        "coverage": native["coverage"],
        "performance": {
            **native["performance"],
            "decimation_seconds": decimation_seconds,
            "json_serialization_seconds": None,
            "png_generation_seconds": None,
            "total_seconds": None,
            "peak_rss_mib": None,
            "npz_bytes": None,
            "json_bytes": None,
            "png_bytes": None,
        },
        "integrity": {**before, "unchanged": None},
    }
    png_seconds = _plots(output, document, native, color_min, color_max)
    document["performance"]["png_generation_seconds"] = png_seconds
    np.savez_compressed(
        output / "quicklook_waterfall.npz",
        frequency_hz=native["frequency_hz"],
        elapsed_seconds=native["elapsed_seconds"],
        time_bin_start_seconds=native["time_bin_start_seconds"],
        time_bin_end_seconds=native["time_bin_end_seconds"],
        time_bin_mid_seconds=native["time_bin_mid_seconds"],
        fractional_excess=native["fractional_excess"],
        valid_mask=native["valid_mask"],
        dc_mask=native["dc_mask"], spur_mask=native["spur_mask"],
        fractional_uncertainty=native["fractional_uncertainty"],
    )
    serialize_start = time.perf_counter()
    json.dumps(document, separators=(",", ":"))
    document["performance"]["json_serialization_seconds"] = time.perf_counter() - serialize_start
    after = {
        "source_hdf5_sha256": _sha256(source),
        "profile_npz_sha256": _sha256(profile_npz),
        "profile_json_sha256": _sha256(profile_json),
    }
    document["integrity"]["unchanged"] = before == after
    document["performance"]["total_seconds"] = time.perf_counter() - total_start
    document["performance"]["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    document["performance"]["npz_bytes"] = (output / "quicklook_waterfall.npz").stat().st_size
    document["performance"]["png_bytes"] = (output / "quicklook_waterfall.png").stat().st_size
    json_path = output / "quicklook_waterfall.json"
    json_path.write_text(json.dumps(document, separators=(",", ":")))
    document["performance"]["json_bytes"] = json_path.stat().st_size
    json_path.write_text(json.dumps(document, separators=(",", ":")))
    return document, native


def validate_waterfall(document, native, source_hdf5, calibration_profile):
    profile = load_calibration_profile(calibration_profile)
    spectrum = apply_relative_calibration(profile, source_hdf5, scale_mode="none")
    aggregated = np.full(native["fractional_excess"].shape[1], np.nan)
    profile_valid = native["valid_mask"]
    aggregated[profile_valid] = np.median(
        native["fractional_excess"][:, profile_valid], axis=0
    )
    valid = profile_valid & np.isfinite(aggregated)
    difference = np.abs(aggregated[valid] - spectrum["fractional_excess"][valid])
    correlation = float(np.corrcoef(
        aggregated[valid], spectrum["fractional_excess"][valid]
    )[0, 1])
    spectrum_values = spectrum["fractional_excess"][valid]
    spectrum_median = float(np.median(spectrum_values))
    spectrum_robust_sigma = float(
        1.4826 * np.median(np.abs(spectrum_values - spectrum_median))
    )
    coverage_contiguous = bool(np.allclose(
        native["time_bin_end_seconds"][:-1], native["time_bin_start_seconds"][1:]
    ))
    coverage_complete = (
        native["coverage"]["complex_samples_in_time_bounds"]
        == native["coverage"]["complex_samples_used_in_complete_ffts"]
        and native["coverage"]["trailing_samples_below_one_fft_discarded"]
        < profile["metadata"]["fft_size"]
    )
    median_difference = float(np.median(difference))
    return {
        "status": "PASS" if (
            coverage_contiguous and coverage_complete
            and median_difference <= spectrum_robust_sigma
            and np.isfinite(correlation)
        ) else "FAIL",
        "comparison_method": "median of calibrated temporal rows vs full-capture median-segment calibration",
        "expected_difference": "not bit-exact: median-of-row-medians differs from global median of FFT segments",
        "acceptance": "median absolute difference <= robust sigma of full-capture fractional spectrum; correlation is diagnostic in this noise-dominated capture",
        "median_abs_difference": median_difference,
        "max_abs_difference": float(np.max(difference)),
        "correlation": correlation,
        "full_spectrum_robust_sigma": spectrum_robust_sigma,
        "time_coverage_contiguous": coverage_contiguous,
        "time_coverage_complete": coverage_complete,
        "time_coverage_definition": "all complete FFT segments covered contiguously; final sub-FFT tail explicitly discarded",
        "calibration_level": document["calibration_level"],
        "absolute_calibration": document["absolute_calibration"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_hdf5")
    parser.add_argument("--calibration-profile", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--time-bins", type=int, default=64)
    parser.add_argument("--frequency-bins", type=int, default=512)
    args = parser.parse_args()
    try:
        document, native = generate_waterfall(
            args.source_hdf5, args.calibration_profile, args.output_dir,
            time_bins=args.time_bins, frequency_bins=args.frequency_bins,
        )
        validation = validate_waterfall(
            document, native, args.source_hdf5, args.calibration_profile
        )
        Path(args.output_dir, "quicklook_waterfall_validation.json").write_text(
            json.dumps(validation, indent=2)
        )
        print(json.dumps({
            "status": document["status"], "output_dir": args.output_dir,
            "compatibility": document["compatibility"],
            "dimensions": [document["time_bins"], document["frequency_bins_native"]],
            "dashboard_dimensions": [document["time_bins"], document["frequency_bins_dashboard"]],
            "color_scale": document["color_scale"],
            "global_metrics": {key: value for key, value in document["quicklook_metrics"].items() if key != "rows"},
            "performance": document["performance"], "validation": validation,
        }, indent=2))
    except (WaterfallError, ValueError) as error:
        parser.exit(2, f"waterfall error: {error}\n")


if __name__ == "__main__":
    main()
