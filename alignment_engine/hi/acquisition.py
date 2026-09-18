"""HI acquisition backend contract (Fase 8) - the ONE interface both the
simulated path and a future real path go through, so engine.py's per-
point loop never needs a second implementation for "where did this
spectrum come from" (Fase 7's explicit "no mantener dos implementaciones
paralelas").

Async for the same reason TrackingBackend is (item 7's rule: async where
there is I/O or waiting, sync where there is pure computation) - a real
backend will await the SDR; the simulated one awaits nothing but keeps the
identical signature so engine.py's calling code never branches on which
backend it holds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol

import numpy as np
from astropy.coordinates import SkyCoord

from alignment_engine.hi.spectral_pipeline import SpectralPipelineConfig, compute_spectral_metric
from alignment_engine.hi.spectral_simulation import GaussianComponent, SpectralSimConfig, generate_spectrum


@dataclass
class HISpectrumAcquisition:
    """What acquire_hi_spectrum() returns - real or simulated, same shape."""
    frequency_hz: np.ndarray
    power: np.ndarray
    timestamp_utc: str
    integration_seconds: float
    center_frequency_hz: float
    sample_rate_hz: float
    gain_db: float
    valid_mask: Optional[np.ndarray] = None   # True = usable; None = "all usable, caller must still check finiteness"
    capture_metadata: dict = field(default_factory=dict)


class HIAcquisitionBackend(Protocol):
    async def acquire_hi_spectrum(self, point_coordinate: SkyCoord, *, integration_seconds: float,
                                   center_frequency_hz: float, sample_rate_hz: float,
                                   gain_db: float) -> HISpectrumAcquisition: ...


class RealHIAcquisitionBackend:
    """NOT IMPLEMENTED - designed, not executed, matching the same
    inspect/prepare/execute-authorization precedent RealTrackingBackend and
    RealMountAdapter already established. Real HI acquisition is not
    authorized this pass (Fase 26: no real SDR capture) - this class
    documents exactly what it will need to wrap (ALMITA's existing MAIN SDR
    capture path, capture.py's IQ acquisition + FFT, never a second,
    duplicated SDR client) once that authorization exists."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "RealHIAcquisitionBackend requires a separate, explicit hardware/SDR-capture "
            "authorization not granted this pass. It must wrap ALMITA's existing capture.py "
            "SDR path (never a second, duplicated rtl_tcp client) once authorized."
        )


class SimulatedHIAcquisitionBackend:
    """Generates a synthetic spectrum via spectral_simulation.generate_spectrum()
    for the requested point, using the SAME GaussianComponent/baseline/
    noise/RFI/missing-channel model the standalone Monte Carlo scripts use
    - this is the "simulation backend generates data" half of Fase 8, and
    the ONLY thing that changes between this and a real backend is where
    the spectrum numbers come from; everything downstream (spectral_pipeline)
    is identical."""

    def __init__(self, expected_amplitude_by_index: List[float], gain_a: float = 1.0,
                 baseline_b: float = 0.0, noise_std: float = 0.05, rfi_spike_count: int = 0,
                 missing_channel_fraction: float = 0.0, line_fwhm_km_s: float = 20.0, seed: int = 1):
        self._expected = expected_amplitude_by_index
        self.gain_a = gain_a
        self.baseline_b = baseline_b
        self.noise_std = noise_std
        self.rfi_spike_count = rfi_spike_count
        self.missing_channel_fraction = missing_channel_fraction
        self.line_fwhm_km_s = line_fwhm_km_s
        self.seed = seed
        self._call_index = 0

    async def acquire_hi_spectrum(self, point_coordinate: SkyCoord, *, integration_seconds: float,
                                   center_frequency_hz: float, sample_rate_hz: float,
                                   gain_db: float) -> HISpectrumAcquisition:
        from datetime import datetime, timezone
        index = self._call_index
        self._call_index += 1
        expected = self._expected[index] if index < len(self._expected) else 0.0
        amplitude = max(self.gain_a * expected, 0.0)
        sim = SpectralSimConfig(
            components=[GaussianComponent(amplitude_k=amplitude, center_km_s=0.0, fwhm_km_s=self.line_fwhm_km_s)],
            gain=1.0, baseline_coefficients=(self.baseline_b,), noise_std=self.noise_std,
            rfi_spike_count=self.rfi_spike_count, missing_channel_fraction=self.missing_channel_fraction,
            seed=self.seed * 10000 + index,
        )
        velocity_km_s, power, rfi_mask = generate_spectrum(sim)
        # velocity -> frequency, topocentric-equivalent for this synthetic
        # path (no real Doppler frame needed - see velocity.py for the
        # real conversion a real backend would need).
        frequency_hz = center_frequency_hz * (1.0 - velocity_km_s * 1000.0 / 299792458.0)
        return HISpectrumAcquisition(
            frequency_hz=frequency_hz, power=power, timestamp_utc=datetime.now(timezone.utc).isoformat(),
            integration_seconds=integration_seconds, center_frequency_hz=center_frequency_hz,
            sample_rate_hz=sample_rate_hz, gain_db=gain_db, valid_mask=~rfi_mask,
            capture_metadata={"simulated": True, "point_index": index},
        )


async def acquire_and_reduce_point(backend: HIAcquisitionBackend, point_coordinate: SkyCoord, *,
                                    center_frequency_hz: float, sample_rate_hz: float, gain_db: float,
                                    integration_seconds: float,
                                    pipeline_config: Optional[SpectralPipelineConfig] = None) -> Optional[float]:
    """The ONE code path from "acquire a spectrum" to "one scalar metric" -
    used by both the simulated engine run and (once authorized) a real
    hardware run. Returns None if the pipeline could not produce a valid
    metric (never a fabricated 0)."""
    pipeline_config = pipeline_config or SpectralPipelineConfig()
    acquisition = await backend.acquire_hi_spectrum(
        point_coordinate, integration_seconds=integration_seconds, center_frequency_hz=center_frequency_hz,
        sample_rate_hz=sample_rate_hz, gain_db=gain_db)
    rest_freq_hz = 1_420_405_751.77
    velocity_km_s = (rest_freq_hz - acquisition.frequency_hz) / rest_freq_hz * 299792.458
    rfi_mask = ~acquisition.valid_mask if acquisition.valid_mask is not None else None
    result = compute_spectral_metric(velocity_km_s, acquisition.power, pipeline_config, rfi_mask=rfi_mask)
    return result.metric if result.metric_valid else None
