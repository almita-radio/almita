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

RealHIAcquisitionBackend (authorized for the first real HI night scan)
wraps sdr_capture.SDRCapture - the exact class capture.py itself uses -
never a second, duplicated rtl_tcp client. rtl_tcp's protocol has no
gain/rate/frequency read-back command, so "actual params used" below
means "what was requested", recorded honestly as such rather than implying
an independent hardware confirmation that does not exist.
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
    async def acquire_hi_spectrum(self, point_coordinate: SkyCoord, *, point_index: int, integration_seconds: float,
                                   center_frequency_hz: float, sample_rate_hz: float,
                                   gain_db: float) -> HISpectrumAcquisition: ...


def _compute_spectrum_from_iq_file(h5_path: str, fft_size: int = 8192):
    """The same FFT pattern sun_detectability_test.py's own real-hardware-
    tested robust_broadband_metrics() uses (Hann window, chunked-and-
    averaged |FFT|^2, fftshift) - re-exposed here as (frequency_hz, power)
    ARRAYS instead of that function's summary statistics, because
    spectral_pipeline.compute_spectral_metric() needs the full spectrum
    around the HI line, not a broadband scalar. This is the one narrowly-
    scoped new piece of code Fase 8 anticipated ("compose it yourself from
    the IQ step and the FFT step") - the IQ ACQUISITION itself is entirely
    SDRCapture (sdr_capture.py), never re-implemented."""
    import h5py

    with h5py.File(h5_path) as handle:
        raw = handle["iq_data"][:]
        sample_rate = float(handle.attrs["sample_rate_hz"])
        center_frequency = float(handle.attrs["center_frequency_hz"])
    i = raw[0::2].astype(np.float32)
    q = raw[1::2].astype(np.float32)
    iq = (i - np.mean(i)) + 1j * (q - np.mean(q))
    count = len(iq) // fft_size
    if count < 8:
        raise ValueError(f"insufficient IQ samples for a {fft_size}-point FFT (got {len(iq)} samples, "
                          f"need >= {8 * fft_size})")
    window = np.hanning(fft_size).astype(np.float32)
    psd = np.zeros(fft_size, np.float64)
    chunk_segments = 64
    for start in range(0, count, chunk_segments):
        block = iq[start * fft_size:min(count, start + chunk_segments) * fft_size]
        block = block.reshape(-1, fft_size) * window
        transformed = np.fft.fftshift(np.fft.fft(block, axis=1), axes=1)
        psd += np.sum(np.abs(transformed) ** 2, axis=0)
    psd /= count
    frequency_hz = center_frequency + np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / sample_rate))
    return frequency_hz, psd


class RealHIAcquisitionBackend:
    """Wraps sdr_capture.SDRCapture (network/rtl_tcp mode) - the SAME class
    capture.py itself uses - never a second, duplicated rtl_tcp client.
    connect()/configure() happen ONCE (lazily, on first
    acquire_hi_spectrum() call, or explicitly via prepare()); every point
    reuses the same connection and only issues a new capture(). Raw IQ for
    EVERY point is written to its own HDF5 file under the session's
    points/ directory (session.point_path(index)) and never overwritten or
    replaced by a derived product (Fase's raw-data policy)."""

    def __init__(self, host: str, port: int, session, verbose: bool = False):
        self.host = host
        self.port = port
        self.session = session
        self.verbose = verbose
        self._sdr = None
        self._configured_params = None
        self._point_index = 0

    async def prepare(self, *, center_frequency_hz: float, sample_rate_hz: float, gain_db) -> dict:
        """Connect and configure once, ahead of the raster, so per-point
        capture() calls don't pay reconnect/reconfigure cost - and so a
        connection failure surfaces at PREFLIGHT time, not mid-raster.
        Returns the actual params requested (rtl_tcp has no read-back
        command - see acquisition.py module docstring - so "actual" here
        means "what we asked for", recorded honestly as such)."""
        from sdr_capture import SDRCapture
        self._sdr = SDRCapture("network", self.host, self.port, verbose=self.verbose)
        await self._sdr.connect()
        await self._sdr.configure(center_freq=int(center_frequency_hz), sample_rate=int(sample_rate_hz), gain=gain_db)
        self._configured_params = {"center_frequency_hz": center_frequency_hz,
                                    "sample_rate_hz": sample_rate_hz, "gain_db": gain_db}
        return dict(self._configured_params)

    async def close(self) -> None:
        if self._sdr is not None:
            await self._sdr.close()
            self._sdr = None

    async def acquire_hi_spectrum(self, point_coordinate: SkyCoord, *, point_index: int, integration_seconds: float,
                                   center_frequency_hz: float, sample_rate_hz: float,
                                   gain_db) -> HISpectrumAcquisition:
        from datetime import datetime, timezone
        if self._sdr is None or self._configured_params != {"center_frequency_hz": center_frequency_hz,
                                                              "sample_rate_hz": sample_rate_hz, "gain_db": gain_db}:
            await self.prepare(center_frequency_hz=center_frequency_hz, sample_rate_hz=sample_rate_hz, gain_db=gain_db)

        # `point_index` is the CALLER's raster index, passed explicitly -
        # NOT an internal auto-incrementing counter. A counter would desync
        # from the real raster index the moment any point is skipped
        # (altitude/GOTO failure) before reaching acquisition, silently
        # mislabeling every subsequent point's raw IQ file (found while
        # designing replay mode, which depends on this mapping being exact).
        index = point_index
        raw_path = str(self.session.point_path(index, suffix="h5"))
        timestamp_utc = datetime.now(timezone.utc).isoformat()
        await self._sdr.capture(integration_seconds, raw_path, sample_rate=int(sample_rate_hz),
                                 metadata={"point_index": index, "ra_deg": float(point_coordinate.icrs.ra.deg),
                                           "dec_deg": float(point_coordinate.icrs.dec.deg),
                                           "timestamp_utc": timestamp_utc})
        frequency_hz, power = _compute_spectrum_from_iq_file(raw_path)
        return HISpectrumAcquisition(
            frequency_hz=frequency_hz, power=power, timestamp_utc=timestamp_utc,
            integration_seconds=integration_seconds, center_frequency_hz=center_frequency_hz,
            sample_rate_hz=sample_rate_hz, gain_db=gain_db, valid_mask=None,
            capture_metadata={"simulated": False, "point_index": index, "raw_iq_path": raw_path},
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

    async def acquire_hi_spectrum(self, point_coordinate: SkyCoord, *, point_index: int, integration_seconds: float,
                                   center_frequency_hz: float, sample_rate_hz: float,
                                   gain_db: float) -> HISpectrumAcquisition:
        from datetime import datetime, timezone
        index = point_index
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
                                    point_index: int, center_frequency_hz: float, sample_rate_hz: float,
                                    gain_db: float, integration_seconds: float,
                                    pipeline_config: Optional[SpectralPipelineConfig] = None) -> Optional[float]:
    """The ONE code path from "acquire a spectrum" to "one scalar metric" -
    used by both the simulated engine run and (once authorized) a real
    hardware run. Returns None if the pipeline could not produce a valid
    metric (never a fabricated 0). `point_index` must be the caller's own
    raster index (see RealHIAcquisitionBackend's docstring on why an
    internal counter is unsafe)."""
    pipeline_config = pipeline_config or SpectralPipelineConfig()
    acquisition = await backend.acquire_hi_spectrum(
        point_coordinate, point_index=point_index, integration_seconds=integration_seconds,
        center_frequency_hz=center_frequency_hz, sample_rate_hz=sample_rate_hz, gain_db=gain_db)
    rest_freq_hz = 1_420_405_751.77
    velocity_km_s = (rest_freq_hz - acquisition.frequency_hz) / rest_freq_hz * 299792.458
    rfi_mask = ~acquisition.valid_mask if acquisition.valid_mask is not None else None
    result = compute_spectral_metric(velocity_km_s, acquisition.power, pipeline_config, rfi_mask=rfi_mask)
    return result.metric if result.metric_valid else None
