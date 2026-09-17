"""Alignment configuration (Fase 23) - plain dataclasses, no framework.

Defaults are the spans/spacings suggested in the brief, but every one of
them is overridable from JSON or CLI - none of this is "law" per the
brief's own instruction. beam_fwhm_deg defaults from observer_config.json
when not explicitly overridden, rather than hardcoding a second, possibly
inconsistent constant the way alignment.py's PROVISIONAL_BEAM_FWHM_DEG=14.0
already disagrees with observer_config.json's beam_fwhm_deg=20.0 (both
exist in this repo today - this module picks observer_config.json as the
single source of truth for the new package, and documents the mismatch
rather than silently picking one).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional


def _load_json(path) -> dict:
    return json.loads(Path(path).read_text())


@dataclass
class GlobalConfig:
    observer_config_path: str = "observer_config.json"
    output_root: str = "data/alignment"
    altitude_floor_deg: float = 20.0
    goto_timeout_s: float = 120.0
    mount_host: str = "localhost"
    mount_port: int = 7624
    mount_device: str = "LX200 OnStep"
    sdr_host: str = "localhost"
    sdr_port: int = 1234
    settle_seconds: float = 2.0
    # None -> observation_orchestrator's own DEFAULT_RUNTIME_DIR
    # (data/runtime) - overridable so tests never read/depend on this
    # machine's real, possibly-live runtime state (Fase 4's preflight
    # capture-conflict check reads this directory).
    orchestrator_runtime_dir: Optional[str] = None


@dataclass
class SolarScanConfig:
    # Defaults per the brief: coarse ~12-15deg span / 3-4deg spacing,
    # fine ~5-6deg span / 1-2deg spacing around the coarse peak.
    coarse_span_deg: float = 14.0
    coarse_spacing_deg: float = 3.5
    fine_span_deg: float = 5.5
    fine_spacing_deg: float = 1.5
    integration_seconds: float = 2.0
    settle_seconds: float = 2.0
    sun_gain_db: float = 20.0
    expected_fwhm_deg: Optional[float] = None  # None -> read from observer_config
    max_clipping_fraction: float = 0.01
    min_altitude_deg: float = 20.0


@dataclass
class HIScanConfig:
    raster_span_deg: float = 12.0
    raster_spacing_deg: float = 3.0
    integration_seconds: float = 20.0
    settle_seconds: float = 2.0
    gain_db: float = 40.2
    center_frequency_hz: float = 1_420_405_752.0
    sample_rate_hz: float = 2_400_000.0
    velocity_window_km_s: float = 200.0
    reference_catalog_path: str = "data/hi_sky_catalog_2000pts.csv"
    expected_fwhm_deg: Optional[float] = None
    min_altitude_deg: float = 20.0
    minimum_valid_positions: int = 8


def _dataclass_from_dict(cls, data: dict):
    valid_keys = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in valid_keys})


@dataclass
class AlignmentConfig:
    global_: GlobalConfig = field(default_factory=GlobalConfig)
    solar: SolarScanConfig = field(default_factory=SolarScanConfig)
    hi: HIScanConfig = field(default_factory=HIScanConfig)

    @classmethod
    def load(cls, path: Optional[str] = None, overrides: Optional[dict] = None) -> "AlignmentConfig":
        """Load defaults, then a JSON file (if given), then explicit CLI-style
        overrides (dict of dotted keys like "solar.coarse_span_deg") - each
        layer wins over the previous one, defaults are never mutated."""
        config = cls()
        if path:
            raw = _load_json(path)
            config.global_ = _dataclass_from_dict(GlobalConfig, raw.get("global", {}))
            config.solar = _dataclass_from_dict(SolarScanConfig, raw.get("solar", {}))
            config.hi = _dataclass_from_dict(HIScanConfig, raw.get("hi", {}))
        for dotted_key, value in (overrides or {}).items():
            section_name, _, field_name = dotted_key.partition(".")
            section = {"global": config.global_, "solar": config.solar, "hi": config.hi}.get(section_name)
            if section is None or not hasattr(section, field_name):
                raise ValueError(f"unknown config override: {dotted_key}")
            setattr(section, field_name, value)
        return config

    def resolved_beam_fwhm_deg(self, mode: str) -> float:
        """SolarScanConfig/HIScanConfig.expected_fwhm_deg wins if set;
        otherwise observer_config.json's beam_fwhm_deg; otherwise the
        repo-wide historical default (20deg) - never alignment.py's own
        14deg constant, which this package deliberately does not import
        to avoid silently picking between two disagreeing values."""
        section = self.solar if mode == "solar" else self.hi
        if section.expected_fwhm_deg is not None:
            return section.expected_fwhm_deg
        try:
            observer = _load_json(self.global_.observer_config_path)
            return float(observer["observation_defaults"]["beam_fwhm_deg"])
        except (OSError, ValueError, KeyError):
            return 20.0
