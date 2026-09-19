"""SPECTRAL ESTIMATE stage: raw IQ -> canonical PSD, reusing
hi_spectral_metric.robust_psd_from_iq verbatim (uint8 -> centered@127.5 ->
complex -> Hann-windowed FFT -> median-combined |FFT|^2 across segments).
Same capture + same config -> same result (determinism)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from hi_spectral_metric import robust_psd_from_iq
from reduce_engine.config import ReduceConfig


@dataclass
class SpectralEstimate:
    frequency_hz: np.ndarray
    psd: np.ndarray
    segments: np.ndarray  # per-sub-integration PSD, used for spur detection and uncertainty
    clipping_fraction: float
    fft_config: dict[str, Any]


def _clipping_fraction(iq_bytes: np.ndarray) -> float:
    values = np.asarray(iq_bytes, dtype=np.uint8)
    if values.size == 0:
        return 1.0
    return float(np.mean((values == 0) | (values == 255)))


def estimate_spectrum(iq_bytes: np.ndarray, *, sample_rate_hz: float, center_frequency_hz: float,
                      config: ReduceConfig) -> SpectralEstimate:
    frequency, psd, segments = robust_psd_from_iq(
        iq_bytes, sample_rate_hz, center_frequency_hz,
        fft_size=config.fft_size, combine=config.combine, return_segments=True,
    )
    fft_config = {
        "fft_size": config.fft_size, "window": "Hann (numpy.hanning)", "combine": config.combine,
        "sample_rate_hz": float(sample_rate_hz), "center_frequency_hz": float(center_frequency_hz),
        "iq_center": 127.5, "n_segments": int(segments.shape[0]),
    }
    return SpectralEstimate(frequency_hz=frequency, psd=psd, segments=segments,
                            clipping_fraction=_clipping_fraction(iq_bytes), fft_config=fft_config)
