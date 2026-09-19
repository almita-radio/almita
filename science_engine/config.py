"""ScienceConfig: every free parameter of the SCIENCE pipeline, explicit
and hashable - same determinism discipline as reduce_engine.config
(same input + same config + same REDUCE session -> same output).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Optional

SCIENCE_PIPELINE_VERSION = "science-v1.0"

QUALITY_POLICIES = ("STRICT", "STANDARD", "PERMISSIVE")


@dataclass(frozen=True)
class ScienceConfig:
    # --- input ---
    reduce_session_dir: str = ""

    # --- spatial grid (section 15-16) ---
    pixel_scale_deg: Optional[float] = None   # None -> derived from beam_fwhm_deg / pixels_per_beam
    pixels_per_beam: float = 4.0              # visualization sampling only - never claims better resolution (section 11)
    extent_margin_beams: float = 1.5          # grid extent = point bounding box + this many beam FWHMs of margin

    # --- beam (section 9-11) ---
    beam_fwhm_deg: float = 20.0               # default source: observer_config.json (see BeamModel docstring)
    beam_source: str = "observer_config.json:observation_defaults.beam_fwhm_deg"
    beam_status: str = "CONFIGURED_OPERATIONAL"
    beam_cutoff_n_fwhm: float = 3.0           # section 25: weight below this radius is treated as zero

    # --- quality policy (section 28-29) ---
    quality_policy: str = "STANDARD"          # STRICT | STANDARD | PERMISSIVE
    # STANDARD (default, conservative): GOOD usable full weight, WARNING usable full weight (REDUCE already
    # demoted it with a documented reason), BAD/UNKNOWN excluded. STRICT: only GOOD usable. PERMISSIVE:
    # GOOD/WARNING/UNKNOWN usable, only BAD excluded. Never silently different from this - see SCIENCE_MODEL.md.

    # --- velocity window (section 35-36: explicit only, no auto-detection in V1) ---
    velocity_window_min_m_s: float = -100_000.0
    velocity_window_max_m_s: float = 100_000.0
    min_spectral_coverage_fraction: float = 0.5   # section 40: below this, integrated value is invalid, not extrapolated

    # --- uncertainty weighting (section 23) ---
    uncertainty_floor_relative: float = 1e-6      # sigma <= this * per-input median sigma is treated as invalid, not floored

    # --- moment products (section 43-46) ---
    moment_min_snr: float = 3.0                   # section 44-45: signal selection threshold before centroid/dispersion

    def __post_init__(self):
        if self.quality_policy not in QUALITY_POLICIES:
            raise ValueError(f"quality_policy must be one of {QUALITY_POLICIES}, got {self.quality_policy!r}")
        if self.beam_fwhm_deg <= 0:
            raise ValueError(f"beam_fwhm_deg must be > 0, got {self.beam_fwhm_deg}")
        if self.velocity_window_min_m_s >= self.velocity_window_max_m_s:
            raise ValueError("velocity_window_min_m_s must be < velocity_window_max_m_s")
        if not (0.0 <= self.min_spectral_coverage_fraction <= 1.0):
            raise ValueError("min_spectral_coverage_fraction must be in [0, 1]")

    def to_dict(self) -> dict:
        return asdict(self)

    def config_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
