"""Forensics input boundary: a REDUCE session directory (Level 1) and nothing else - never RAW, never data/mosaic.

Consumes the frozen `science_engine.ingest.load_science_input` (contract validation, per-point checks, sha256, ordering) and
adds only what forensics needs from the SAME Level 1 files: end timestamps, receiver id, RFI_REF flags and the session's HI rest
frequency (config.json). LSRK shift is DERIVED from the stored axes, not recomputed from ephemerides:

    v_lsrk(channel) = c * (1 - f_topocentric(channel) / f_rest) + shift_point        (verified constant across channels)
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from science_engine.ingest import ScienceContractError, load_science_input
from science_engine.spatial import to_galactic
from science_forensics.models import C_LIGHT_M_S, ForensicsInput, ForensicsPoint


class ForensicsInputError(Exception):
    pass


def _parse_utc(text: str) -> float:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


UNAVAILABLE_LEVEL1 = {
    "temperature": "not carried by Level 1 (SDR/LNA temperatures live in RAW capture metadata; RAW is never opened)",
    "gain": "not carried by Level 1 (receiver gain lives in RAW capture metadata; RAW is never opened)",
}


def load_forensics_input(reduce_session_dir: str | Path) -> ForensicsInput:
    session_dir = Path(reduce_session_dir)
    try:
        science_input = load_science_input(session_dir)
    except ScienceContractError as error:
        raise ForensicsInputError(str(error)) from error
    config_path = session_dir / "config.json"
    if not config_path.exists():
        raise ForensicsInputError(f"{config_path} missing: the HI rest frequency of the session is required")
    rest = json.loads(config_path.read_text()).get("hi_rest_frequency_hz")
    if not (isinstance(rest, (int, float)) and np.isfinite(rest) and rest > 0):
        raise ForensicsInputError("REDUCE config.json carries no valid hi_rest_frequency_hz")

    exclusions = list(science_input.exclusions)
    points: list[ForensicsPoint] = []
    for p in science_input.points:
        if p.velocity_lsrk_m_s is None:
            exclusions.append({"point_index": p.point_index, "reason": "NO_VELOCITY_AXIS"})
            continue
        meta = json.loads((session_dir / "points" / str(p.point_index) / "master_spectrum.json").read_text())
        deviation = p.velocity_lsrk_m_s - C_LIGHT_M_S * (1.0 - p.frequency_hz / rest)
        shift = float(np.median(deviation))
        if float(np.max(np.abs(deviation - shift))) > 1e-3:      # 1 mm/s: the relation must hold to numerical precision
            exclusions.append({"point_index": p.point_index, "reason": "LSRK_AXIS_NOT_A_CONSTANT_SHIFT_OF_THE_RADIO_AXIS"})
            continue
        refs = meta.get("capture_refs", [])
        try:
            t_start = _parse_utc(meta["timestamp_start_utc"])
            t_end = _parse_utc(meta.get("timestamp_end_utc") or meta["timestamp_start_utc"])
        except (KeyError, ValueError, TypeError):
            exclusions.append({"point_index": p.point_index, "reason": "MISSING_OR_INVALID_TIMESTAMP"})
            continue
        l_deg, b_deg = to_galactic(np.array([p.ra_deg]), np.array([p.dec_degrees]))
        points.append(ForensicsPoint(
            point_index=p.point_index, t_start_s=t_start, t_end_s=t_end, ra_deg=float(p.ra_deg),
            dec_deg=float(p.dec_degrees), frequency_hz=p.frequency_hz, velocity_lsrk_m_s=p.velocity_lsrk_m_s,
            value=p.relative_intensity, sigma=p.uncertainty, mask=np.asarray(p.mask, dtype=np.int64),
            lsrk_shift_m_s=shift, quality_state=p.reduce_quality_state, timestamp_utc=meta["timestamp_start_utc"],
            receiver=(refs[0].get("receiver") if refs else None), capture_ids=[r.get("capture_id") for r in refs],
            rfi_ref_available=meta.get("quality", {}).get("metrics", {}).get("rfi_ref_available"),
            source_h5_sha256=p.source_h5_sha256, source_json_sha256=p.source_json_sha256,
            l_deg=float(l_deg[0]), b_deg=float(b_deg[0])))
    if not points:
        raise ForensicsInputError("no usable Level 1 point after forensics ingest checks")
    points.sort(key=lambda q: q.point_index)
    unavailable = dict(UNAVAILABLE_LEVEL1)
    flags = {q.rfi_ref_available for q in points}
    unavailable["rfi_ref"] = ("per-point RFI_REF association is not available in REDUCE V1 (rfi_ref_available is "
                              f"{sorted(map(str, flags))} for every point); no matching is fabricated")
    return ForensicsInput(
        campaign_id=science_input.campaign_id, reduce_session_id=science_input.reduce_session_id,
        reduce_session_dir=str(session_dir), reduce_session_status=science_input.reduce_session_status,
        reduce_manifest_sha256=_sha256(session_dir / "manifest.json"), rest_frequency_hz=float(rest),
        points=points, exclusions=exclusions, unavailable=unavailable)


def site_from_observer_config(path: str | Path) -> dict:
    """Operator-provided observatory coordinates (observer_config.json:observer.{latitude,longitude}_deg) with provenance."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    observer = json.loads(raw)["observer"]
    return {"site_latitude_deg": float(observer["latitude_deg"]), "site_longitude_deg": float(observer["longitude_deg"]),
            "site_source": f"{path.name}:observer.latitude_deg/longitude_deg", "site_source_path": str(path),
            "site_source_sha256": hashlib.sha256(raw).hexdigest()}


def local_sidereal_hours(t_posix_s: np.ndarray, longitude_deg: float) -> np.ndarray:
    """Mean local sidereal time (hours), offline. UT1-UTC is set to 0 (|UT1-UTC| < 0.9 s -> < 14 arcsec): no IERS lookup."""
    from astropy.time import Time
    import astropy.units as u
    t = Time(np.asarray(t_posix_s, dtype=float), format="unix", scale="utc")
    t.delta_ut1_utc = np.zeros(t.shape)
    return np.asarray(t.sidereal_time("mean", longitude=longitude_deg * u.deg).hour, dtype=float)
