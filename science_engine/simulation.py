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
    velocity_offset_m_s: float = 0.0               # this point's LSRK axis is shifted by this (real: up to ~5 km/s)
    noise_sigma: Optional[float] = None            # actual noise std for this point (None -> builder default)
    reported_sigma: Optional[float] = None         # sigma written to `uncertainty` (None -> the actual noise std)
    amplitude_scale: float = 1.0
    extra_line_fn: Optional[object] = None         # callable(velocity, ra_deg, dec_deg) -> array, added to the signal


def build_synthetic_science_input(
    specs: list[SyntheticPointSpec], *,
    n_channels: int = 256,
    velocity_min_m_s: float = -200_000.0, velocity_max_m_s: float = 200_000.0,
    descending: bool = False,                             # real REDUCE Level 1 axes are DESCENDING
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
        velocity = np.linspace(velocity_min_m_s, velocity_max_m_s, n_channels) + spec.velocity_offset_m_s
        if descending:
            velocity = velocity[::-1].copy()
        ra_deg = spec.ra_hours * 15.0
        centroid = line_center_m_s_fn(ra_deg, spec.dec_degrees)
        amplitude = line_amplitude_fn(ra_deg, spec.dec_degrees)
        signal = spec.amplitude_scale * amplitude * np.exp(
            -4 * np.log(2) * (velocity - centroid) ** 2 / line_fwhm_m_s ** 2)
        if spec.extra_line_fn is not None:
            signal = signal + spec.extra_line_fn(velocity, ra_deg, spec.dec_degrees)
        actual_sigma = noise_sigma if spec.noise_sigma is None else spec.noise_sigma
        noise = rng.normal(0.0, actual_sigma, size=n_channels) if actual_sigma > 0 else np.zeros(n_channels)
        relative_intensity = signal + noise
        uncertainty = np.full(n_channels, actual_sigma if spec.reported_sigma is None else spec.reported_sigma)
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


def write_synthetic_reduce_session(dest_dir, science_input: ScienceInput, *, status: str = "COMPLETED",
                                   extra_manifest_points: Optional[list[dict]] = None,
                                   reduce_schema_version: str = "1.0", write_order_reversed: bool = False):
    """Write `science_input` as a REDUCE-format session directory (manifest.json + points/N/master_spectrum.{json,h5})
    identical in layout to a real REDUCE V1 session, so the FULL ingest -> run path can be tested from an isolated
    copy with controlled ground truth. Never runs REDUCE and never touches RAW."""
    import json
    from pathlib import Path
    import h5py

    dest = Path(dest_dir)
    (dest / "points").mkdir(parents=True, exist_ok=True)
    points = list(science_input.points)
    manifest_points = []
    for p in (reversed(points) if write_order_reversed else points):
        pdir = dest / "points" / str(p.point_index)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "master_spectrum.json").write_text(json.dumps({
            "reduce_schema_version": reduce_schema_version, "reduce_session_id": science_input.reduce_session_id,
            "campaign_id": science_input.campaign_id, "point_index": p.point_index, "ra_hours": p.ra_hours,
            "dec_degrees": p.dec_degrees, "timestamp_start_utc": p.timestamp_start_utc, "n_bins": int(p.frequency_hz.shape[0]),
            "velocity_frame": p.velocity_frame, "integration_time_seconds": p.integration_time_seconds,
            "quality": {"state": p.reduce_quality_state, "reasons": ["synthetic"], "metrics": {}},
            "calibration_level": p.calibration_level,
            "capture_refs": [{"campaign_id": science_input.campaign_id, "point_index": p.point_index,
                              "capture_id": f"SYNTH_{p.point_index:04d}.h5", "source_path": "<synthetic: no RAW>",
                              "sha256": f"{p.point_index:064x}", "timestamp_utc": p.timestamp_start_utc}],
        }))
        with h5py.File(pdir / "master_spectrum.h5", "w") as h:
            h.create_dataset("frequency_hz", data=p.frequency_hz)
            h.create_dataset("relative_intensity", data=p.relative_intensity)
            h.create_dataset("uncertainty", data=p.uncertainty)
            h.create_dataset("mask", data=p.mask)
            h.create_dataset("n_contributing", data=p.n_contributing)
            if p.velocity_lsrk_m_s is not None:
                h.create_dataset("velocity_lsrk_m_s", data=p.velocity_lsrk_m_s)
        manifest_points.append({"point_index": p.point_index, "status": "COMPLETED", "reason": None})
    manifest_points.extend(extra_manifest_points or [])
    quality_counts: dict[str, int] = {}
    for p in points:
        quality_counts[p.reduce_quality_state] = quality_counts.get(p.reduce_quality_state, 0) + 1
    (dest / "manifest.json").write_text(json.dumps({
        "campaign_id": science_input.campaign_id, "reduce_session_id": science_input.reduce_session_id,
        "status": status, "points_discovered": len(manifest_points), "points_accepted": len(manifest_points),
        "points_rejected": 0, "points_completed": len(points), "points_blocked": 0, "points_failed": 0,
        "quality_counts": quality_counts, "source_campaign_root": "<synthetic>", "points": manifest_points}))
    return dest
