"""Converts a CALIBRATE reference wizard session's REAL 50 ohm captures into the RELATIVE_INSTRUMENTAL
calibration profile (.json + .npz) OBSERVE's Quicklook / REDUCE actually consume
(calibration_foundation.load_calibration_profile() / check_calibration_compatibility() /
apply_relative_calibration_to_psd() - reused verbatim here, never reimplemented). This never re-captures
anything and never moves the mount - it only reads files a wizard session already wrote.

WHY NOT calibration_foundation.build_calibration_profile() directly: that function validates each reference
file's (center_frequency_hz, sample_rate_hz, gain) signature against HARDCODED LITERALS (1420405752, 2400000,
and a caller-supplied gain_db compared against the file's OWN gain_requested_db attribute). Two real, found-
by-reading-the-code problems with trusting that for a wizard session's real captures:
  1. sdr_capture.py's own HDF5-attrs-writing code (_capture_network, frozen, never modified here) defaults
     center_frequency_hz to that SAME literal 1420405752 and gain to the string "auto" whenever the caller's
     metadata dict does not supply the exact keys 'center_frequency_hz'/'gain_requested_db' - which the wizard
     CLI did not do for real captures before this session's fix (see calibrate_reference_wizard.py's
     _do_50r_capture). This means a real session's per-capture HDF5 attributes for center_frequency_hz/gain
     can silently be that PLACEHOLDER, not a real measurement - trusting them (or a hardcoded expectation of
     them) would be exactly the "no uses los valores fijos del constructor antiguo" this module exists to
     avoid.
  2. A wizard session's REAL configured frequency need not even be 1420405752 Hz - the wizard's own recorded
     WizardConfig and its receiver_snapshot (a real, live rtl_tcp service argv read via `ps`,
     VERIFIED_BY_SERVICE_COMMAND_LINE - the same fact-finding calibration_operational_realtest.py's own
     "REAL CALIBRATION" uses) are the actual, independently verified ground truth for what this session's
     captures were really taken at - never assumed equal to some other session's literal.

This module therefore: (a) requires the wizard's own real, complete, LNA_INPUT-connected 50 ohm captures
(never SKIPPED, never simulated, never partial); (b) cross-checks the wizard's DECLARED config against its
real, service-argv-VERIFIED receiver_snapshot for frequency/sample-rate/gain COHERENCE, refusing on any
disagreement rather than picking one source silently; (c) builds the reference ensemble using the SAME real
DSP primitives calibration_foundation.build_calibration_profile() itself uses
(hi_spectral_metric.robust_psd_from_iq / measure_dc_mask_half_width / dc_mask / detect_fixed_spurs - reused,
not reimplemented) but parametrized on the session's own VERIFIED values, never a hardcoded literal; (d)
writes the profile in the EXACT SAME .json+.npz schema calibration_foundation.load_calibration_profile()
already reads, so REDUCE/Quicklook need no changes to consume it.

CalibrationLevel stays RELATIVE_INSTRUMENTAL; absolute_calibration is always False. This profile is built from
the 50 OHM reference ONLY - it never incorporates the wizard's HI ALTO/HI BAJO captures (those measure a
DIFFERENT thing, a relative sky-position contrast, not the receiver's own reference response) and is never
used to assert anything about celestial HI.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import h5py
import numpy as np

from calibration_foundation import _plain, load_calibration_profile
from hi_spectral_metric import dc_mask as make_dc_mask, detect_fixed_spurs, measure_dc_mask_half_width, robust_psd_from_iq

SCHEMA_VERSION = "1.0"
CALIBRATION_LEVEL = "RELATIVE_INSTRUMENTAL"
INSTRUMENT_CHAIN = "LNA_FILTER_CABLING_TO_RTL_SDR"          # matches calibration_foundation.py's own convention
REFERENCE_TOPOLOGY = "AMBIENT_50R_AT_LNA_INPUT_WIZARD"       # distinct, honest label for THIS builder's output
FREQ_COHERENCE_TOLERANCE_HZ = 1.0
RATE_COHERENCE_TOLERANCE_HZ = 1.0
GAIN_COHERENCE_TOLERANCE_DB = 0.05


class WizardProfileError(Exception):
    """A real, named reason this session's 50 ohm captures cannot be turned into a profile - never a
    fabricated/best-effort profile instead."""


@dataclass
class VerifiedCaptureConfig:
    center_frequency_hz: float
    sample_rate_hz: float
    gain_db: float
    bias_t_verified_on: bool
    source: str                      # how this was established, for the profile's own provenance record
    caveats: List[str] = field(default_factory=list)


def _read_wizard_state(session_dir: Path) -> Dict[str, Any]:
    path = session_dir / "wizard_state.json"
    if not path.is_file():
        raise WizardProfileError(f"no wizard_state.json in {session_dir}")
    return json.loads(path.read_text())


def resolve_verified_config(state: Dict[str, Any]) -> VerifiedCaptureConfig:
    """The session's real, cross-checked frequency/rate/gain - NEVER read from the per-capture HDF5 attrs
    (see module docstring for why those can be a sdr_capture.py placeholder), always from the wizard's own
    DECLARED config cross-checked against its real, ps-based receiver_snapshot. Raises on any disagreement -
    never averages or silently prefers one source."""
    cfg = state.get("config") or {}
    snap = ((state.get("receiver_snapshot") or {}).get("service_command_line") or {})
    if not snap.get("found"):
        raise WizardProfileError("wizard_state.json has no verified receiver_snapshot.service_command_line "
                                 "(rtl_tcp service argv was not found at session start) - cannot establish a "
                                 "real, verified frequency/rate/gain for this session")
    parsed = snap.get("parsed") or {}
    caveats: List[str] = []

    def _coherent(label: str, declared: Optional[float], verified: Optional[float], tol: float) -> float:
        if declared is None or verified is None:
            raise WizardProfileError(f"{label}: missing (declared={declared}, verified={verified})")
        if abs(float(declared) - float(verified)) > tol:
            raise WizardProfileError(f"{label} INCOHERENT: wizard config declared {declared}, but the real "
                                     f"rtl_tcp service (VERIFIED_BY_SERVICE_COMMAND_LINE at session start) was "
                                     f"actually running {verified} - refusing to guess which is real")
        return float(verified)

    center_frequency_hz = _coherent("center_frequency_hz", cfg.get("center_frequency_hz"), parsed.get("center_frequency_hz"), FREQ_COHERENCE_TOLERANCE_HZ)
    sample_rate_hz = _coherent("sample_rate_hz", cfg.get("sample_rate_hz"), parsed.get("sample_rate_hz"), RATE_COHERENCE_TOLERANCE_HZ)
    gain_db = _coherent("gain_db", cfg.get("gain_db"), parsed.get("gain_db"), GAIN_COHERENCE_TOLERANCE_DB)
    bias_t_on = bool(parsed.get("bias_t_enabled"))
    if not bias_t_on:
        caveats.append("Bias-T was NOT verified ON in the real service command line at session start")
    return VerifiedCaptureConfig(
        center_frequency_hz=center_frequency_hz, sample_rate_hz=sample_rate_hz, gain_db=gain_db,
        bias_t_verified_on=bias_t_on, caveats=caveats,
        source="wizard_state.json config cross-checked against receiver_snapshot.service_command_line "
              "(VERIFIED_BY_SERVICE_COMMAND_LINE, a real ps-based read of the live rtl_tcp process argv at "
              "session start) - agreement within tolerance required, never assumed",
    )


def _read_capture(path: Path) -> tuple:
    if path.name.endswith(".part"):
        raise WizardProfileError(f"partial capture, not usable: {path}")
    if not path.is_file():
        raise WizardProfileError(f"missing capture file: {path}")
    with h5py.File(path, "r") as handle:
        attrs = {k: _plain(v) for k, v in handle.attrs.items()}
        if attrs.get("capture_status") != "success":
            raise WizardProfileError(f"capture not marked success: {path} (capture_status={attrs.get('capture_status')})")
        if attrs.get("simulated"):
            raise WizardProfileError(f"capture is SIMULATED, not real: {path} - refusing to build a profile from it")
        iq = handle["iq_data"][:]
    return iq, attrs


def require_fifty_ohm_ready(state: Dict[str, Any], session_dir: Path) -> List[Path]:
    """Real, complete, LNA_INPUT-connected 50 ohm captures, or raises with the exact reason. Returns the
    ordered list of real capture file paths."""
    fifty = state.get("fifty_ohm")
    if not fifty:
        raise WizardProfileError("this session never declared a 50 ohm reference")
    if fifty.get("status") != "DONE":
        raise WizardProfileError(f"50 ohm reference status is {fifty.get('status')!r}, not DONE - "
                                 "(SKIPPED or still pending cannot be converted into a profile)")
    if fifty.get("connection_point") != "LNA_INPUT":
        raise WizardProfileError(f"50 ohm was connected at {fifty.get('connection_point')!r}, not LNA_INPUT - "
                                 "a profile meant to characterize the antenna's own replacement point requires "
                                 "the reference at LNA_INPUT specifically (see CHAIN_COMPONENTS_INCLUDED)")
    n_expected = int((state.get("config") or {}).get("n_captures", 0))
    if n_expected < 2:
        raise WizardProfileError(f"n_captures={n_expected} in this session's config - at least 2 required")
    cap_dir = session_dir / "captures" / "AMBIENT_50R"
    paths = [cap_dir / f"capture_{i:03d}.h5" for i in range(n_expected)]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise WizardProfileError(f"{len(missing)} of {n_expected} expected 50 ohm capture file(s) missing: {missing}")
    return paths


def build_profile_from_wizard(session_dir: str | Path, output_stem: str | Path, fft_size: int = 8192) -> Dict[str, Any]:
    """Builds and writes the RELATIVE_INSTRUMENTAL profile (.json+.npz) from a wizard session's real 50 ohm
    captures. Raises WizardProfileError (never writes a partial/fabricated profile) if any requirement in the
    module docstring is not met. Returns {"profile": <loaded profile dict>, "report": <validation/provenance
    report dict>}."""
    # No blanket "wizard step must be DONE" gate: the real, sufficient requirement is the 50 ohm reference's
    # OWN status (checked below by require_fifty_ohm_ready), independent of whether HI ALTO/HI BAJO/contrast
    # have happened yet - this is what lets calibrate_reference_wizard.py's own cmd_finish call this AS PART
    # OF the transition into DONE (the on-disk wizard_state.json still reads its PREVIOUS step at that exact
    # point, since this runs before that step is saved).
    session_dir = Path(session_dir)
    state = _read_wizard_state(session_dir)
    capture_paths = require_fifty_ohm_ready(state, session_dir)
    verified = resolve_verified_config(state)

    spectra: List[np.ndarray] = []
    frequency: Optional[np.ndarray] = None
    records: List[Dict[str, Any]] = []
    per_file_placeholder_notes: List[str] = []
    for path in capture_paths:
        iq, attrs = _read_capture(path)
        file_sample_rate = attrs.get("sample_rate_hz")
        if file_sample_rate is None or abs(float(file_sample_rate) - verified.sample_rate_hz) > RATE_COHERENCE_TOLERANCE_HZ:
            raise WizardProfileError(f"{path}: recorded sample_rate_hz={file_sample_rate} does not match the "
                                     f"session's verified {verified.sample_rate_hz} - refusing")
        # The per-file center_frequency_hz/gain attrs are NOT trusted for the profile itself (see module
        # docstring - they can be sdr_capture.py's own placeholder); disclosed here, never silently dropped.
        file_freq, file_gain = attrs.get("center_frequency_hz"), attrs.get("gain_requested_db", attrs.get("gain"))
        if file_freq is None or abs(float(file_freq) - verified.center_frequency_hz) > FREQ_COHERENCE_TOLERANCE_HZ:
            per_file_placeholder_notes.append(f"{path.name}: recorded center_frequency_hz={file_freq!r} differs "
                                              f"from the session-verified {verified.center_frequency_hz} - "
                                              "profile built using the verified value, not this file attribute "
                                              "(a known sdr_capture.py placeholder-default gap; see module docstring)")
        if file_gain in (None, "auto"):
            per_file_placeholder_notes.append(f"{path.name}: recorded gain={file_gain!r} (no real per-capture "
                                              f"gain readback) - profile built using the session-verified "
                                              f"{verified.gain_db} dB")
        f, psd = robust_psd_from_iq(iq, verified.sample_rate_hz, verified.center_frequency_hz, fft_size=fft_size, combine="median")
        if frequency is not None and not np.array_equal(frequency, f):
            raise WizardProfileError(f"{path}: FFT frequency axis differs from the other captures")
        frequency = f
        spectra.append(psd)
        records.append({
            "source_file": str(path), "timestamp": attrs.get("capture_start_utc", attrs.get("created_at")),
            "duration_seconds": attrs.get("duration_seconds"), "recorded_center_frequency_hz": file_freq,
            "recorded_gain": file_gain,
        })

    stack = np.asarray(spectra, dtype=np.float64)
    reference = np.median(stack, axis=0)
    mad = np.median(np.abs(stack - reference), axis=0)
    reference_sigma = 1.4826 * mad
    dc_measurement = measure_dc_mask_half_width(frequency, reference, verified.center_frequency_hz)
    dc = make_dc_mask(frequency, verified.center_frequency_hz, dc_measurement["half_width_hz"])
    spur, spur_regions = detect_fixed_spurs(frequency, stack, excluded_mask=dc,
                                            persistence_threshold=max(0.5, 1.0 - 1.0 / len(capture_paths)))
    edge = np.zeros(frequency.shape, dtype=bool)
    edge_bins = max(2, int(0.02 * len(edge)))
    edge[:edge_bins] = True
    edge[-edge_bins:] = True
    valid = ~(dc | spur | edge)
    fractional_variability = reference_sigma / np.maximum(reference, 1e-30)
    bin_hz = float(np.median(np.diff(frequency)))
    for region in spur_regions:
        region["width_hz"] = float((region["hi_bin"] - region["lo_bin"] + 1) * abs(bin_hz))
        region["reason"] = "persistent_narrow_feature_in_50ohm_ensemble"

    metadata: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "calibration_level": CALIBRATION_LEVEL, "absolute_calibration": False,
        "temperature_kelvin": None, "antenna_temperature_kelvin": None, "flux_jy": None,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": "calibration_engine.wizard_profile_converter (from a CALIBRATE reference wizard session's "
                  "real 50 ohm captures, no new capture, no mount movement)",
        "source_wizard_session": state.get("session_id"), "source_wizard_session_dir": str(session_dir),
        "reference_topology": REFERENCE_TOPOLOGY, "instrument_chain": INSTRUMENT_CHAIN,
        "bias_t_state": "ON" if verified.bias_t_verified_on else "UNKNOWN",
        "center_frequency_hz": verified.center_frequency_hz, "sample_rate_hz": verified.sample_rate_hz,
        "gain_db": verified.gain_db,
        "frequency_verification": verified.source,
        "frequency_hz": {"start": float(frequency[0]), "stop": float(frequency[-1]), "bin_width": bin_hz},
        "fft_size": int(fft_size), "window": "Hann (numpy.hanning)",
        "aggregation": "median FFT power within capture; median across captures",
        "iq_center": 127.5, "reference_count": len(capture_paths), "reference_files": [str(p) for p in capture_paths],
        "reference_records": records,
        "dc_frequency_hz": verified.center_frequency_hz, "dc_mask": dc_measurement, "dc_mask_bins": int(np.sum(dc)),
        "dc_mask_half_width_hz": dc_measurement["half_width_hz"], "spur_regions": spur_regions,
        "edge_mask_bins_each_side": edge_bins, "valid_fraction": float(np.mean(valid)),
        "temperature_correction": "NOT_ESTABLISHED",
        "relative_calibration": {
            "default_scale_mode": "none",
            "formula": "fractional_excess=(PSD-reference_scaled)/reference_scaled",
            "none": "reference_scaled=reference_psd; preserves broadband changes",
            "median_scalar": "scale=median(PSD/reference_psd over valid_mask); removes global level drift and broadband continuum",
        },
        "quicklook_contract": ["frequency_hz", "relative_psd_db", "fractional_excess", "valid_mask",
                              "fractional_uncertainty", "calibration_metadata"],
        "known_limitations": [
            "relative/instrumental only; no Kelvin or Jansky calibration",
            f"profile is specific to {verified.gain_db:g} dB, {verified.center_frequency_hz:g} Hz, "
            f"{verified.sample_rate_hz:g} Hz and the declared topology",
            "median-scalar mode can remove astrophysical broadband continuum",
            "built from the wizard's 50 ohm reference ONLY - the same session's HI ALTO/HI BAJO captures were "
            "NOT used and characterize a different, unrelated relative sky-position contrast",
            "ADC absolute scale remains inconclusive without a known RF source",
        ] + verified.caveats + per_file_placeholder_notes,
    }
    stem = Path(output_stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(stem.with_suffix(".npz"), frequency_hz=frequency, reference_psd=reference,
                        reference_sigma=reference_sigma, fractional_variability=fractional_variability,
                        valid_mask=valid, dc_mask=dc, spur_mask=spur)
    stem.with_suffix(".json").write_text(json.dumps(metadata, indent=2))
    # Verify the file pair this function just wrote is genuinely usable by re-loading it through the SAME
    # loader OBSERVE's real quicklook_spectrum.generate_quicklook() calls first - never leave a half-good-
    # looking .json+.npz pair on disk if that fails; delete both rather than let a caller mistake an unverified
    # write for a working profile.
    try:
        profile = load_calibration_profile(stem)
    except Exception as exc:  # noqa: BLE001 - any failure here means "not usable", regardless of cause
        stem.with_suffix(".json").unlink(missing_ok=True)
        stem.with_suffix(".npz").unlink(missing_ok=True)
        raise WizardProfileError(f"profile was written but failed OBSERVE's own loader verification "
                                 f"({type(exc).__name__}: {exc}) - deleted, not left on disk as a false success") from exc
    report = {
        "verified_config": {"center_frequency_hz": verified.center_frequency_hz, "sample_rate_hz": verified.sample_rate_hz,
                            "gain_db": verified.gain_db, "bias_t_verified_on": verified.bias_t_verified_on,
                            "source": verified.source},
        "caveats": verified.caveats + per_file_placeholder_notes,
        "reference_count": len(capture_paths), "valid_fraction": float(np.mean(valid)),
        "dc_mask_bins": int(np.sum(dc)), "spur_count": len(spur_regions),
    }
    return {"profile": profile, "report": report, "profile_stem": str(stem)}


def validate_against_capture(profile: Dict[str, Any], capture_path: str | Path) -> Dict[str, Any]:
    """Real compatibility + a real applied-calibration sanity pass against an already-captured HDF5 (never a
    new capture). Uses calibration_foundation's own compatibility/apply functions unmodified. The caller MUST
    treat any resulting fractional_excess as an INDOOR INSTRUMENT-CHAIN CHECK, never a celestial measurement -
    see this module's and the CLI wrapper's own labeling."""
    from calibration_foundation import apply_relative_calibration, check_calibration_compatibility
    compatibility = check_calibration_compatibility(profile, capture_path)
    result: Dict[str, Any] = {"capture_path": str(capture_path), "compatibility": compatibility}
    if compatibility["status"] == "COMPATIBLE":
        applied = apply_relative_calibration(profile, capture_path, scale_mode="none")
        valid = applied["valid_mask"]
        residual = applied["fractional_excess"][valid]
        result["applied"] = {
            "reference_scale": applied["reference_scale"],
            "residual_median": float(np.median(residual)),
            "residual_robust_sigma": float(1.4826 * np.median(np.abs(residual - np.median(residual)))),
            "environment": "INDOOR", "astronomical_interpretation": "NOT_PERMITTED",
        }
    return result
