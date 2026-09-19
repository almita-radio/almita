"""Synthetic LEVEL 1 input (section 90-91): builds ScienceInputPoint/
ScienceInput objects directly - never RAW IQ, never a REDUCE run. Tests
that need controlled ground truth (known source position, known line
center, known noise) use this, not reduce_engine internals, so SCIENCE's
own test suite never depends on REDUCE being importable/correct beyond
the frozen MasterSpectrum-level contract it already tests separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from reduce_engine.models import MaskFlag
from science_engine.models import ScienceInput, ScienceInputPoint


@dataclass
class SyntheticPointSpec:
    point_index: int
    ra_hours: float
    dec_degrees: float
    reduce_quality_state: str = "GOOD"
    calibration_level: str = "RELATIVE"
    integration_time_seconds: float = 10.0
    masked_channel_slice: Optional[slice] = None   # e.g. slice(100, 150) - forces MaskFlag.RFI there


def build_synthetic_science_input(
    specs: list[SyntheticPointSpec], *,
    n_channels: int = 256,
    velocity_min_m_s: float = -200_000.0, velocity_max_m_s: float = 200_000.0,
    line_center_m_s_fn=lambda ra_deg, dec_deg: 0.0,     # allows a velocity gradient across the field (section 92)
    line_amplitude_fn=lambda ra_deg, dec_deg: 0.0,       # allows a spatial source profile (section 73-75)
    line_fwhm_m_s: float = 20_000.0,
    noise_sigma: float = 0.02,
    rng: Optional[np.random.Generator] = None,
    campaign_id: str = "SYNTHETIC-GOLDEN", reduce_session_id: str = "REDUCE-SYNTHETIC-0001",
) -> ScienceInput:
    rng = rng or np.random.default_rng(20260920)
    frequency_hz = np.linspace(1_420_000_000.0, 1_421_000_000.0, n_channels)  # not physically used by SCIENCE

    points = []
    for spec in specs:
        velocity = np.linspace(velocity_min_m_s, velocity_max_m_s, n_channels)
        ra_deg = spec.ra_hours * 15.0
        centroid = line_center_m_s_fn(ra_deg, spec.dec_degrees)
        amplitude = line_amplitude_fn(ra_deg, spec.dec_degrees)
        signal = amplitude * np.exp(-4 * np.log(2) * (velocity - centroid) ** 2 / line_fwhm_m_s ** 2)
        noise = rng.normal(0.0, noise_sigma, size=n_channels)
        relative_intensity = signal + noise
        uncertainty = np.full(n_channels, noise_sigma)
        mask = np.zeros(n_channels, dtype=np.int64)
        if spec.masked_channel_slice is not None:
            mask[spec.masked_channel_slice] = MaskFlag.RFI.value
            # matches real REDUCE data exactly (confirmed: 343/343 masked bins are NaN on a real point,
            # 0/n GOOD bins are) - a masked bin's relative_intensity is NEVER a fabricated finite number.
            relative_intensity[spec.masked_channel_slice] = np.nan
            uncertainty[spec.masked_channel_slice] = np.nan
        n_contributing = np.ones(n_channels, dtype=np.int64)

        points.append(ScienceInputPoint(
            campaign_id=campaign_id, reduce_session_id=reduce_session_id, point_index=spec.point_index,
            ra_hours=spec.ra_hours, dec_degrees=spec.dec_degrees, ra_deg=ra_deg,
            timestamp_start_utc=None, frequency_hz=frequency_hz, velocity_lsrk_m_s=velocity,
            velocity_frame="lsrk", relative_intensity=relative_intensity, uncertainty=uncertainty,
            mask=mask, n_contributing=n_contributing, integration_time_seconds=spec.integration_time_seconds,
            reduce_quality_state=spec.reduce_quality_state, calibration_level=spec.calibration_level,
        ))

    return ScienceInput(reduce_session_dir="<synthetic>", campaign_id=campaign_id,
                        reduce_session_id=reduce_session_id, reduce_schema_version="1.0",
                        reduce_session_status="COMPLETED", points=points, contract_problems=[])


def rectangular_grid_specs(center_ra_hours: float, center_dec_deg: float, n_rows: int, n_cols: int,
                           spacing_deg: float, **point_kwargs) -> list[SyntheticPointSpec]:
    """A simple synthetic NxM pointing grid, like a real OBSERVE mosaic -
    used by the golden/adversarial tests that need many points."""
    specs = []
    idx = 1
    cos_dec = np.cos(np.radians(center_dec_deg))
    for row in range(n_rows):
        for col in range(n_cols):
            dx_deg = (col - (n_cols - 1) / 2) * spacing_deg
            dy_deg = (row - (n_rows - 1) / 2) * spacing_deg
            ra_hours = center_ra_hours + (dx_deg / cos_dec) / 15.0
            dec_deg = center_dec_deg + dy_deg
            specs.append(SyntheticPointSpec(point_index=idx, ra_hours=ra_hours, dec_degrees=dec_deg,
                                            **point_kwargs))
            idx += 1
    return specs
