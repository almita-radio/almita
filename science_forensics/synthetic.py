"""Synthetic Level 1 campaigns with KNOWN truth for validating the forensic diagnostics (no RAW, no REDUCE run).

Every campaign shares one topocentric frequency axis (like real REDUCE sessions) and gives each point its own LSRK shift, so
v_lsrk = c(1 - f/f_rest) + shift_i exactly as in real Level 1. A feature is placed either fixed in LSRK velocity (sky-like),
fixed in topocentric frequency (receiver-like), drifting in frequency with time, or following a velocity gradient across the sky.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from science_forensics.models import C_LIGHT_M_S, ForensicsInput, ForensicsPoint

F_REST = 1_420_405_751.77
DF_HZ = 2_400_000.0 / 8192.0                  # real REDUCE channel width (Hz)
DV_M_S = C_LIGHT_M_S * DF_HZ / F_REST         # ~61.8 m/s
T0_POSIX = 1_788_365_900.0                    # 2026-09-02T16:18:20Z (the real 9-point campaign start)


@dataclass
class SyntheticTruth:
    kind: str
    parameters: dict = field(default_factory=dict)


def serpentine_positions(n_rows, n_cols, spacing_deg=0.75, ra0=177.45, dec0=-33.4):
    """Raster in capture order with alternating row direction: time is entangled with Dec (row) and partly with RA."""
    pos = []
    for r in range(n_rows):
        cols = range(n_cols) if r % 2 == 0 else range(n_cols - 1, -1, -1)
        for c in cols:
            pos.append((ra0 + (c - (n_cols - 1) / 2) * spacing_deg / np.cos(np.radians(dec0)), dec0 + (r - (n_rows - 1) / 2) * spacing_deg))
    return pos


def random_positions(n, seed, ra0=177.45, dec0=-33.4, half_deg=1.6):
    rng = np.random.default_rng(seed)
    return [(ra0 + rng.uniform(-half_deg, half_deg) / np.cos(np.radians(dec0)), dec0 + rng.uniform(-half_deg, half_deg)) for _ in range(n)]


def build_campaign(kind: str, *, n_points: int = 20, seed: int = 0, positions: Optional[list] = None, dt_s: float = 40.0,
                   n_channels: int = 1024, feature_channel: float = 512.0, amplitude: float = 1.0, fwhm_channels: float = 12.0,
                   noise_sigma: float = 0.03, shift_spread_m_s: float = 1200.0, shift_mode: str = "random",
                   drift_hz_per_hour: float = 60_000.0, gradient_m_s_per_deg: float = 4_000.0, gradient_axis: str = "ra",
                   masked_slice: Optional[slice] = None, campaign_id: str = "SYNTHETIC-FORENSICS") -> tuple:
    """kind: sky_fixed | receiver_fixed | time_drift | sky_gradient. Returns (ForensicsInput, SyntheticTruth)."""
    rng = np.random.default_rng(seed)
    positions = positions if positions is not None else random_positions(n_points, seed + 1)
    n_points = len(positions)
    k = np.arange(n_channels, dtype=float)
    f = F_REST - (n_channels / 2 - k) * DF_HZ + 0.0          # ascending topocentric frequency, common axis
    f_feature_ref = f[int(feature_channel)]
    if shift_mode == "random":
        shifts = -19_500.0 + rng.uniform(-0.5, 0.5, n_points) * shift_spread_m_s
    elif shift_mode == "zero":
        shifts = np.full(n_points, -19_500.0)
    else:                                                     # smooth trend with capture order (worst case for time/shift confusion)
        shifts = -19_500.0 + np.linspace(-0.5, 0.5, n_points) * shift_spread_m_s
    ra_mean = np.mean([p[0] for p in positions])
    dec_mean = np.mean([p[1] for p in positions])
    points, centres_true = [], []
    for i, (ra, dec) in enumerate(positions):
        t_h = i * dt_s / 3600.0
        shift = float(shifts[i])
        v_axis = C_LIGHT_M_S * (1.0 - f / F_REST) + shift
        v_feature_ref = C_LIGHT_M_S * (1.0 - f_feature_ref / F_REST) + (-19_500.0)   # LSRK velocity of the reference feature
        if kind == "sky_fixed":
            f_c = F_REST * (1.0 - (v_feature_ref - shift) / C_LIGHT_M_S)
        elif kind == "receiver_fixed":
            f_c = f_feature_ref
        elif kind == "time_drift":
            f_c = f_feature_ref + drift_hz_per_hour * t_h
        elif kind == "sky_gradient":
            offset = (ra - ra_mean) * np.cos(np.radians(-33.4)) if gradient_axis == "ra" else (dec - dec_mean)
            v_c = v_feature_ref + gradient_m_s_per_deg * offset
            f_c = F_REST * (1.0 - (v_c - shift) / C_LIGHT_M_S)
        else:
            raise ValueError(f"unknown synthetic kind {kind!r}")
        ch_c = (f_c - f[0]) / DF_HZ
        s = fwhm_channels / 2.3548
        y = amplitude * np.exp(-0.5 * ((k - ch_c) / s) ** 2) + rng.normal(0.0, noise_sigma, n_channels)
        mask = np.zeros(n_channels, dtype=np.int64)
        sig = np.full(n_channels, noise_sigma)
        if masked_slice is not None:
            mask[masked_slice] = 4                       # MaskFlag.RFI
            y = y.copy(); y[masked_slice] = np.nan; sig = sig.copy(); sig[masked_slice] = np.nan
        t = T0_POSIX + i * dt_s
        points.append(ForensicsPoint(
            point_index=i + 1, t_start_s=t, t_end_s=t + 10.0, ra_deg=float(ra % 360.0), dec_deg=float(dec), frequency_hz=f.copy(),
            velocity_lsrk_m_s=v_axis, value=y, sigma=sig, mask=mask, lsrk_shift_m_s=shift, quality_state="GOOD",
            timestamp_utc="", receiver="SYNTHETIC", capture_ids=[f"SYN_{i + 1:04d}"], rfi_ref_available=False,
            source_h5_sha256=f"{i + 1:064x}", source_json_sha256=f"{i + 1:064x}"))
        centres_true.append(ch_c)
    from science_engine.spatial import to_galactic
    ra_a = np.array([p.ra_deg for p in points]); dec_a = np.array([p.dec_deg for p in points])
    l, b = to_galactic(ra_a, dec_a)
    for p, li, bi in zip(points, l, b):
        p.l_deg, p.b_deg = float(li), float(bi)
    inp = ForensicsInput(campaign_id=campaign_id, reduce_session_id="REDUCE-SYNTHETIC", reduce_session_dir="<synthetic>",
                         reduce_session_status="COMPLETED", reduce_manifest_sha256="0" * 64, rest_frequency_hz=F_REST, points=points,
                         unavailable={"temperature": "synthetic", "gain": "synthetic", "rfi_ref": "synthetic"})
    truth = SyntheticTruth(kind, {"drift_hz_per_hour": drift_hz_per_hour, "gradient_m_s_per_deg": gradient_m_s_per_deg, "gradient_axis": gradient_axis,
                                  "true_centre_channels": centres_true, "shifts_m_s": shifts.tolist(), "dt_s": dt_s,
                                  "amplitude": amplitude, "fwhm_channels": fwhm_channels, "noise_sigma": noise_sigma,
                                  "channel_width_hz": DF_HZ, "channel_width_m_s": DV_M_S, "reference_channel": feature_channel})
    return inp, truth


def write_reduce_session_fixture(dest, inp: ForensicsInput):
    """Write a ForensicsInput as a REDUCE-format session directory (Level 1 layout + config.json with the rest frequency), so the
    full CLI/ingest path can be tested from an isolated copy. Reuses the frozen SCIENCE simulation writer."""
    import json
    from datetime import datetime, timezone
    from pathlib import Path
    from science_engine.models import ScienceInput, ScienceInputPoint
    from science_engine.simulation import write_synthetic_reduce_session

    pts = []
    for p in inp.points:
        iso = datetime.fromtimestamp(p.t_start_s, tz=timezone.utc).isoformat()
        pts.append(ScienceInputPoint(
            campaign_id=inp.campaign_id, reduce_session_id=inp.reduce_session_id, point_index=p.point_index, ra_hours=p.ra_deg / 15.0,
            dec_degrees=p.dec_deg, ra_deg=p.ra_deg, timestamp_start_utc=iso, frequency_hz=p.frequency_hz,
            velocity_lsrk_m_s=p.velocity_lsrk_m_s, velocity_frame="lsrk", relative_intensity=p.value, uncertainty=p.sigma,
            mask=p.mask, n_contributing=np.ones(p.value.shape[0], dtype=np.int64), integration_time_seconds=10.0,
            reduce_quality_state=p.quality_state, calibration_level="RELATIVE"))
    si = ScienceInput(reduce_session_dir="<synthetic>", campaign_id=inp.campaign_id, reduce_session_id=inp.reduce_session_id,
                      reduce_schema_version="1.0", reduce_session_status="COMPLETED", points=pts)
    d = write_synthetic_reduce_session(dest, si)
    Path(d, "config.json").write_text(json.dumps({"hi_rest_frequency_hz": inp.rest_frequency_hz}))
    return d
