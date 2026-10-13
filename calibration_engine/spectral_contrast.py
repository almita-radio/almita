"""Relative HI-line spectral contrast between two real captures (HI ALTO vs HI BAJO) for the CALIBRATE
reference wizard - NOT a Y-factor, NOT a noise temperature, NOT an absolute gain: a RELATIVE comparison of the
SAME real HI-line metric alignment.py's own real captures already use.

Reused verbatim, not reimplemented:
    - alignment_engine.hi.acquisition._compute_spectrum_from_iq_file: raw IQ -> (frequency_hz, power) via the
      same Hann-windowed, chunked-and-averaged |FFT|^2 pattern used everywhere else in this project.
    - alignment_engine.hi.spectral_pipeline.compute_spectral_metric: baseline-fit + line-window integration ->
      one scalar metric with a propagated 1-sigma uncertainty from the off-line channel noise, and RFI/masked-
      channel accounting - the exact "metrica HI" alignment.py's real per-position reduction already applies.
    - alignment_engine.hi.velocity.REST_FREQUENCY_HI_HZ: the one rest-frequency constant this project uses.

Topocentric-only velocity axis (v = c*(f0-f)/f0, no LSRK/heliocentric frame correction) - the SAME simplification
alignment_engine.hi.acquisition.acquire_and_reduce_point() itself uses for the real operational reduction path
(alignment_engine.hi.velocity.observed_frequency_to_velocity() offers the frame-aware version but operates on
one frequency at a time, not a whole spectrum, and is not what the real per-position path already uses).

WHAT THIS NEVER COMPUTES: a Y-factor, a noise temperature, an absolute flux/Kelvin figure, or an absolute RF
gain. See calibration_engine/reference_wizard.py's module docstring and
calibration_engine/thermal_load_experiment.py (kept separate, not wired into this flow) for that boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from alignment_engine.hi.acquisition import _compute_spectrum_from_iq_file
from alignment_engine.hi.spectral_pipeline import SpectralMetricResult, SpectralPipelineConfig, compute_spectral_metric
from alignment_engine.hi.velocity import REST_FREQUENCY_HI_HZ


def _velocity_axis(frequency_hz: np.ndarray) -> np.ndarray:
    """Topocentric radio-velocity axis (km/s) - see module docstring for why this, not the frame-aware
    per-frequency function, matches the real operational reduction path this reuses."""
    return (REST_FREQUENCY_HI_HZ - frequency_hz) / REST_FREQUENCY_HI_HZ * 299792.458


def evaluate_hi_spectral_captures(hdf5_paths: List[Path], config: Optional[SpectralPipelineConfig] = None) -> Dict[str, Any]:
    """Per-capture HI-line metrics (for a real cross-capture stability check, same spirit as the quality
    analysis's own cross_capture summary) PLUS one COMBINED metric+uncertainty computed on the capture-averaged
    spectrum (better SNR than averaging independent per-capture metrics; compute_spectral_metric's own
    off-line-noise uncertainty formula then already reflects that reduced noise - no extra propagation needed).
    Every capture must share the same frequency axis (same center_frequency_hz/sample_rate_hz - the wizard holds
    these fixed across the whole session; this function still verifies it rather than assuming it)."""
    config = config or SpectralPipelineConfig()
    if not hdf5_paths:
        return {"per_capture": [], "combined": None, "reason": "no capture files given"}
    freq0 = None
    spectra = []
    per_capture = []
    for path in hdf5_paths:
        frequency_hz, power = _compute_spectrum_from_iq_file(str(path))
        if freq0 is None:
            freq0 = frequency_hz
        elif frequency_hz.shape != freq0.shape or not np.allclose(frequency_hz, freq0, rtol=0, atol=1.0):
            return {"per_capture": per_capture, "combined": None,
                    "reason": f"{path}: frequency axis does not match the other captures in this reference "
                             "(center_frequency_hz/sample_rate_hz must be identical within one reference step)"}
        velocity_km_s = _velocity_axis(frequency_hz)
        result = compute_spectral_metric(velocity_km_s, power, config)
        per_capture.append({"path": str(path), **result.to_dict()})
        spectra.append(power)
    stacked = np.stack(spectra, axis=0)
    mean_spectrum = np.mean(stacked, axis=0)
    velocity_km_s = _velocity_axis(freq0)
    combined = compute_spectral_metric(velocity_km_s, mean_spectrum, config)
    valid_metrics = [c["metric"] for c in per_capture if c["metric_valid"]]
    cross_capture_std = float(np.std(valid_metrics)) if len(valid_metrics) >= 2 else None
    return {"per_capture": per_capture, "n_captures": len(hdf5_paths),
            "combined": combined.to_dict(), "cross_capture_metric_std": cross_capture_std,
            "mean_spectrum_preview": {"velocity_km_s": [round(float(v), 3) for v in velocity_km_s[::max(1, len(velocity_km_s) // 400)]],
                                      "power": [round(float(p), 6) for p in mean_spectrum[::max(1, len(mean_spectrum) // 400)]]}}


@dataclass
class ContrastResult:
    verdict: str                              # "DEFENSIBLE_CONTRAST" | "INCONCLUSIVE"
    reason: str
    contrast_metric: Optional[float] = None           # HI_ALTO metric - HI_BAJO metric (line-flux units, RELATIVE only)
    combined_uncertainty: Optional[float] = None       # sqrt(unc_alto^2 + unc_bajo^2)
    significance: Optional[float] = None               # |contrast| / combined_uncertainty
    significance_threshold: float = 3.0
    alto_metric: Optional[float] = None
    bajo_metric: Optional[float] = None
    rfi_channels_flagged_alto: Optional[float] = None
    rfi_channels_flagged_bajo: Optional[float] = None
    caveats: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason, "contrast_metric": self.contrast_metric,
                "combined_uncertainty": self.combined_uncertainty, "significance": self.significance,
                "significance_threshold": self.significance_threshold, "alto_metric": self.alto_metric,
                "bajo_metric": self.bajo_metric, "rfi_channels_flagged_alto": self.rfi_channels_flagged_alto,
                "rfi_channels_flagged_bajo": self.rfi_channels_flagged_bajo, "caveats": self.caveats,
                "label": "RELATIVE spectral contrast only - not a Y-factor, not a noise temperature, not an "
                         "absolute gain or flux calibration"}


def compute_spectral_contrast(alto: Dict[str, Any], bajo: Dict[str, Any], significance_threshold: float = 3.0,
                              min_usable_fraction: float = 0.5) -> ContrastResult:
    """Compares HI ALTO's and HI BAJO's COMBINED spectral metrics (from evaluate_hi_spectral_captures). Refuses
    (INCONCLUSIVE, explicit reason) rather than reporting a number whenever either side's metric is not valid,
    either side's usable channel fraction is too low, or the contrast is not at least significance_threshold
    (default 3-sigma) times the combined uncertainty above zero. Never computes a Y-factor/temperature/gain -
    see this module's own docstring."""
    caveats = ["single-session HI ALTO vs HI BAJO comparison - not a repeated/averaged multi-night measurement",
               "HI4PI oriented WHICH sky zones to point at; it does not predict the measured line flux itself",
               "topocentric velocity axis (no LSRK/heliocentric frame correction applied)"]
    a, b = alto.get("combined"), bajo.get("combined")
    if not a or not b:
        return ContrastResult("INCONCLUSIVE", f"missing combined spectral result "
                              f"({'HI ALTO' if not a else 'HI BAJO'}: {alto.get('reason') or bajo.get('reason')})",
                              significance_threshold=significance_threshold, caveats=caveats)
    if not a["metric_valid"] or not b["metric_valid"]:
        return ContrastResult("INCONCLUSIVE", f"spectral metric not valid (HI ALTO: {a['reason']}; HI BAJO: {b['reason']})",
                              significance_threshold=significance_threshold, caveats=caveats)
    if a["usable_fraction"] < min_usable_fraction or b["usable_fraction"] < min_usable_fraction:
        return ContrastResult("INCONCLUSIVE",
                              f"usable channel fraction too low to trust the comparison (HI ALTO={a['usable_fraction']:.2f}, "
                              f"HI BAJO={b['usable_fraction']:.2f}, threshold={min_usable_fraction:.2f}) - likely RFI/clipping",
                              significance_threshold=significance_threshold,
                              rfi_channels_flagged_alto=a["rfi_flag_fraction"], rfi_channels_flagged_bajo=b["rfi_flag_fraction"],
                              caveats=caveats)
    contrast = a["metric"] - b["metric"]
    combined_unc = float(np.hypot(a["uncertainty"], b["uncertainty"])) if a["uncertainty"] and b["uncertainty"] else None
    if not combined_unc or combined_unc <= 0:
        return ContrastResult("INCONCLUSIVE", "could not establish a real uncertainty for either capture "
                              "(off-line channel noise estimate was zero/unavailable) - refusing to claim a contrast",
                              contrast_metric=contrast, alto_metric=a["metric"], bajo_metric=b["metric"],
                              significance_threshold=significance_threshold,
                              rfi_channels_flagged_alto=a["rfi_flag_fraction"], rfi_channels_flagged_bajo=b["rfi_flag_fraction"],
                              caveats=caveats)
    significance = abs(contrast) / combined_unc
    if significance < significance_threshold:
        return ContrastResult("INCONCLUSIVE",
                              f"contrast {contrast:.4g} is only {significance:.2f} sigma "
                              f"(< {significance_threshold:g} sigma threshold) - not a defensible detection of a difference "
                              "between HI ALTO and HI BAJO with this integration time",
                              contrast_metric=contrast, combined_uncertainty=combined_unc, significance=significance,
                              alto_metric=a["metric"], bajo_metric=b["metric"], significance_threshold=significance_threshold,
                              rfi_channels_flagged_alto=a["rfi_flag_fraction"], rfi_channels_flagged_bajo=b["rfi_flag_fraction"],
                              caveats=caveats)
    if contrast <= 0:
        caveats = caveats + ["HI ALTO's measured line flux was NOT higher than HI BAJO's, despite the significant "
                             "difference - the candidate selection's HI4PI orientation did not predict the measured "
                             "outcome at this site/time; report the real result, do not relabel it"]
    return ContrastResult("DEFENSIBLE_CONTRAST",
                          f"contrast {contrast:.4g} is {significance:.2f} sigma (>= {significance_threshold:g} sigma "
                          "threshold) - a real, repeatable difference in measured HI-line flux between the two points",
                          contrast_metric=contrast, combined_uncertainty=combined_unc, significance=significance,
                          alto_metric=a["metric"], bajo_metric=b["metric"], significance_threshold=significance_threshold,
                          rfi_channels_flagged_alto=a["rfi_flag_fraction"], rfi_channels_flagged_bajo=b["rfi_flag_fraction"],
                          caveats=caveats)
