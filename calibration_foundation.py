"""Relative/instrumental spectral calibration for Almita HDF5 IQ captures.

This module deliberately provides no Kelvin, Jansky, antenna-temperature, or
absolute flux calibration. Profiles describe repeatable instrument response.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import h5py
import numpy as np

from hi_spectral_metric import (
    dc_mask as make_dc_mask,
    detect_fixed_spurs,
    measure_dc_mask_half_width,
    robust_psd_from_iq,
)

SCHEMA_VERSION = "1.0"
CALIBRATION_LEVEL = "RELATIVE_INSTRUMENTAL"
ABSOLUTE_FIELDS = {
    "temperature_kelvin", "antenna_temperature_kelvin", "flux_jy"
}


def _plain(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _gain_from_attributes(attributes: dict[str, Any]) -> float | None:
    value = attributes.get("gain_requested_db", attributes.get("gain"))
    if value is None or str(value).lower() == "auto":
        return None
    return float(value)


def _read_capture(path: str | Path) -> tuple[np.ndarray, dict[str, Any]]:
    capture_path = Path(path)
    if capture_path.name.endswith(".part"):
        raise ValueError("partial HDF5 captures are not calibration inputs")
    with h5py.File(capture_path, "r") as capture:
        if capture.attrs.get("capture_status") != "success":
            raise ValueError(f"capture is not complete/successful: {capture_path}")
        iq = capture["iq_data"][:]
        attributes = {key: _plain(value) for key, value in capture.attrs.items()}
    return iq, attributes


def _adc_statistics(iq: np.ndarray) -> dict[str, float]:
    values = np.asarray(iq, dtype=np.uint8)
    probability = np.bincount(values, minlength=256).astype(float)
    probability /= probability.sum()
    populated = probability > 0
    p001, p999 = np.percentile(values, [0.1, 99.9])
    return {
        "mean": float(np.mean(values)),
        "sigma": float(np.std(values)),
        "p0_1": float(p001),
        "p99_9": float(p999),
        "effective_range_codes": float(p999 - p001),
        "entropy_bits": float(-np.sum(probability[populated] * np.log2(probability[populated]))),
        "clipping_fraction": float(np.mean((values == 0) | (values == 255))),
    }


def canonical_psd_from_hdf5(path: str | Path, fft_size: int = 8192):
    """Return canonical frequency/PSD plus capture metadata and ADC statistics."""
    iq, attributes = _read_capture(path)
    frequency, psd = robust_psd_from_iq(
        iq,
        float(attributes["sample_rate_hz"]),
        float(attributes["center_frequency_hz"]),
        fft_size=fft_size,
        combine="median",
    )
    return frequency, psd, attributes, _adc_statistics(iq)


def _temperature_values(attributes: dict[str, Any]) -> list[float]:
    values = []
    for key, value in attributes.items():
        if "temperature" in key.lower() and isinstance(value, (int, float)):
            values.append(float(value))
    return values


def build_calibration_profile(
    reference_files: Iterable[str | Path], output_stem: str | Path,
    *, gain_db: float, reference_topology: str,
    bias_t_state: str = "ON", fft_size: int = 8192,
) -> dict[str, Any]:
    """Build and persist a gain-specific relative reference ensemble."""
    files = [str(Path(path)) for path in reference_files]
    if len(files) < 2:
        raise ValueError("at least two independent reference captures are required")
    spectra, records, frequency = [], [], None
    source_signatures = {}
    for filename in files:
        f, psd, attributes, adc = canonical_psd_from_hdf5(filename, fft_size)
        signature = (
            int(attributes["center_frequency_hz"]),
            int(attributes["sample_rate_hz"]),
            _gain_from_attributes(attributes),
        )
        source_signatures[filename] = signature
        if signature[:2] != (1420405752, 2400000) or signature[2] != float(gain_db):
            raise ValueError(f"incompatible reference configuration: {filename} {signature}")
        if frequency is not None and not np.array_equal(frequency, f):
            raise ValueError("reference frequency axes differ")
        frequency = f
        spectra.append(psd)
        temperatures = _temperature_values(attributes)
        records.append({
            "source_file": filename,
            "timestamp": attributes.get("capture_start_utc", attributes.get("created_at")),
            "gain_db": signature[2],
            "center_frequency_hz": signature[0],
            "sample_rate_hz": signature[1],
            "duration_seconds": attributes.get("duration_seconds"),
            "bias_t_state": bias_t_state,
            "topology": reference_topology,
            "temperatures_c": temperatures,
            "adc_statistics": adc,
        })
    stack = np.asarray(spectra, dtype=np.float64)
    reference = np.median(stack, axis=0)
    median_absolute_deviation = np.median(np.abs(stack - reference), axis=0)
    reference_sigma = 1.4826 * median_absolute_deviation
    dc_measurement = measure_dc_mask_half_width(
        frequency, reference, 1420405752,
    )
    dc = make_dc_mask(frequency, 1420405752, dc_measurement["half_width_hz"])
    spur, spur_regions = detect_fixed_spurs(
        frequency, stack, excluded_mask=dc,
        persistence_threshold=max(0.5, 1.0 - 1.0 / len(files)),
    )
    edge = np.zeros(frequency.shape, dtype=bool)
    edge_bins = max(2, int(0.02 * len(edge)))
    edge[:edge_bins] = True
    edge[-edge_bins:] = True
    valid = ~(dc | spur | edge)
    fractional_variability = reference_sigma / np.maximum(reference, 1e-30)
    global_levels = np.median(stack[:, valid] / reference[valid], axis=1)
    temperatures = [value for record in records for value in record["temperatures_c"]]
    bin_hz = float(np.median(np.diff(frequency)))
    for region in spur_regions:
        region["width_hz"] = float((region["hi_bin"] - region["lo_bin"] + 1) * abs(bin_hz))
        region["reason"] = "persistent_narrow_feature_in_50ohm_ensemble"
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "calibration_level": CALIBRATION_LEVEL,
        "absolute_calibration": False,
        "temperature_kelvin": None,
        "antenna_temperature_kelvin": None,
        "flux_jy": None,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "reference_topology": reference_topology,
        "instrument_chain": "LNA_FILTER_CABLING_TO_RTL_SDR",
        "bias_t_state": bias_t_state,
        "center_frequency_hz": 1420405752,
        "frequency_hz": {"start": float(frequency[0]), "stop": float(frequency[-1]), "bin_width": bin_hz},
        "sample_rate_hz": 2400000,
        "gain_db": float(gain_db),
        "fft_size": int(fft_size),
        "window": "Hann (numpy.hanning)",
        "aggregation": "median FFT power within capture; median across captures",
        "iq_center": 127.5,
        "reference_count": len(files),
        "reference_files": files,
        "reference_records": records,
        "dc_frequency_hz": 1420405752,
        "dc_mask": dc_measurement,
        "dc_mask_bins": int(np.sum(dc)),
        "dc_mask_half_width_hz": dc_measurement["half_width_hz"],
        "spur_regions": spur_regions,
        "edge_mask_bins_each_side": edge_bins,
        "valid_fraction": float(np.mean(valid)),
        "temperature_range": ([min(temperatures), max(temperatures)] if temperatures else None),
        "temperature_correction": "NOT_ESTABLISHED",
        "adc_statistics": {
            "reference_records": [record["adc_statistics"] for record in records],
            "absolute_meaning": "INCONCLUSIVE_NO_KNOWN_RF_SOURCE",
        },
        "global_reference_level": {
            "median": float(np.median(global_levels)),
            "robust_sigma": float(1.4826 * np.median(np.abs(global_levels - np.median(global_levels)))),
            "min": float(np.min(global_levels)),
            "max": float(np.max(global_levels)),
        },
        "relative_calibration": {
            "default_scale_mode": "none",
            "formula": "fractional_excess=(PSD-reference_scaled)/reference_scaled",
            "none": "reference_scaled=reference_psd; preserves broadband changes",
            "median_scalar": "scale=median(PSD/reference_psd over valid_mask); removes global level drift and broadband continuum",
        },
        "quicklook_contract": [
            "frequency_hz", "relative_psd_db", "fractional_excess",
            "valid_mask", "fractional_uncertainty", "calibration_metadata",
        ],
        "known_limitations": [
            "relative/instrumental only; no Kelvin or Jansky calibration",
            "profile is specific to 40.2 dB and the declared topology",
            "median-scalar mode can remove astrophysical broadband continuum",
            "source metadata historically contains gain='auto'; gain_requested_db takes precedence",
            "ADC absolute scale remains inconclusive without a known RF source",
        ],
    }
    if any(metadata[field] is not None for field in ABSOLUTE_FIELDS):
        raise ValueError("absolute fields must remain null without an absolute reference")
    stem = Path(output_stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        stem.with_suffix(".npz"), frequency_hz=frequency,
        reference_psd=reference, reference_sigma=reference_sigma,
        fractional_variability=fractional_variability,
        valid_mask=valid, dc_mask=dc, spur_mask=spur,
    )
    stem.with_suffix(".json").write_text(json.dumps(metadata, indent=2))
    return load_calibration_profile(stem)


def load_calibration_profile(path: str | Path) -> dict[str, Any]:
    stem = Path(path)
    if stem.suffix in (".json", ".npz"):
        stem = stem.with_suffix("")
    metadata = json.loads(stem.with_suffix(".json").read_text())
    arrays = dict(np.load(stem.with_suffix(".npz"), allow_pickle=False))
    if metadata.get("absolute_calibration") is not False:
        raise ValueError("V1 profile must explicitly disable absolute calibration")
    if any(metadata.get(field) is not None for field in ABSOLUTE_FIELDS):
        raise ValueError("relative profile contains forbidden absolute values")
    return {"metadata": metadata, **arrays}


def check_calibration_compatibility(
    profile: dict[str, Any], capture: str | Path,
    *, topology: str | None = None,
) -> dict[str, str]:
    path = Path(capture)
    if path.name.endswith(".part"):
        return {"status": "INCOMPATIBLE", "reason": "partial capture"}
    try:
        with h5py.File(path, "r") as source:
            attributes = {key: _plain(value) for key, value in source.attrs.items()}
    except (OSError, KeyError) as error:
        return {"status": "INCOMPATIBLE", "reason": str(error)}
    metadata = profile["metadata"]
    checks = (
        ("center frequency", attributes.get("center_frequency_hz"), metadata["center_frequency_hz"]),
        ("sample rate", attributes.get("sample_rate_hz"), metadata["sample_rate_hz"]),
        ("gain", _gain_from_attributes(attributes), metadata["gain_db"]),
    )
    for name, actual, expected in checks:
        if actual is None:
            return {"status": "UNKNOWN", "reason": f"capture {name} unavailable"}
        if float(actual) != float(expected):
            return {"status": "INCOMPATIBLE", "reason": f"{name}: {actual} != {expected}"}
    capture_fft_size = attributes.get("fft_size", attributes.get("calibration_fft_size"))
    if capture_fft_size is not None and int(capture_fft_size) != int(metadata["fft_size"]):
        return {
            "status": "INCOMPATIBLE",
            "reason": f"FFT size: {capture_fft_size} != {metadata['fft_size']}",
        }
    capture_topology = topology or attributes.get("reference_topology") or attributes.get("rf_input")
    if capture_topology is None:
        return {"status": "UNKNOWN", "reason": "capture topology unavailable"}
    expected_topology = metadata["instrument_chain"]
    aliases = {
        metadata["reference_topology"]: expected_topology,
        "50_OHM_AT_LNA_INPUT": expected_topology,
        "ANTENNA_AT_LNA_INPUT_INDOOR": expected_topology,
        "ANTENNA_TO_LNA_FILTER_CABLING_TO_RTL_SDR": expected_topology,
    }
    normalized = aliases.get(str(capture_topology), str(capture_topology))
    if normalized != expected_topology:
        return {"status": "INCOMPATIBLE", "reason": f"instrument chain: {normalized} != {expected_topology}"}
    return {"status": "COMPATIBLE", "reason": "frequency, sample rate, gain and topology match"}


def apply_relative_calibration(
    profile: dict[str, Any], capture: str | Path,
    *, topology: str | None = None, scale_mode: str = "none",
) -> dict[str, Any]:
    compatibility = check_calibration_compatibility(profile, capture, topology=topology)
    if compatibility["status"] != "COMPATIBLE":
        raise ValueError(compatibility["reason"])
    frequency, psd, _, _ = canonical_psd_from_hdf5(
        capture, profile["metadata"]["fft_size"]
    )
    if not np.array_equal(frequency, profile["frequency_hz"]):
        raise ValueError("FFT frequency axis is incompatible with profile")
    result = apply_relative_calibration_to_psd(
        profile, frequency, psd, scale_mode=scale_mode
    )
    result["compatibility"] = compatibility
    return result


def apply_relative_calibration_to_psd(
    profile: dict[str, Any], frequency_hz, psd,
    *, scale_mode: str = "none",
) -> dict[str, Any]:
    """Apply the canonical V1 profile formula to an already computed PSD.

    This is the reusable temporal-processing primitive. It does not estimate a
    reference, uncertainty, DC mask, or spur mask from the target spectrum.
    """
    frequency = np.asarray(frequency_hz, dtype=float)
    spectrum = np.asarray(psd, dtype=float)
    if frequency.shape != profile["frequency_hz"].shape or not np.array_equal(
        frequency, profile["frequency_hz"]
    ):
        raise ValueError("PSD frequency axis is incompatible with profile")
    if spectrum.shape != frequency.shape or not np.all(np.isfinite(spectrum)):
        raise ValueError("PSD must be a finite vector matching the profile frequency axis")
    reference = profile["reference_psd"]
    valid = profile["valid_mask"].astype(bool)
    if scale_mode == "none":
        scale = 1.0
    elif scale_mode == "median_scalar":
        scale = float(np.median(spectrum[valid] / reference[valid]))
    else:
        raise ValueError("scale_mode must be 'none' or 'median_scalar'")
    scaled = reference * scale
    fractional = (spectrum - scaled) / np.maximum(scaled, 1e-30)
    uncertainty = profile["reference_sigma"] * scale / np.maximum(scaled, 1e-30)
    relative_db = 10 * np.log10(np.maximum(spectrum, 1e-30) / np.maximum(scaled, 1e-30))
    return {
        "frequency_hz": frequency,
        "relative_psd_db": relative_db,
        "fractional_excess": fractional,
        "valid_mask": valid,
        "fractional_uncertainty": uncertainty,
        "reference_scale": scale,
        "scale_mode": scale_mode,
        "calibration_metadata": profile["metadata"],
    }
