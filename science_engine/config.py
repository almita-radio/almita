"""ScienceConfig: every free parameter of the SCIENCE pipeline, explicit
and hashable - same determinism discipline as reduce_engine.config
(same input + same config + same REDUCE session -> same output).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np

from science_engine.models import BEAM_FWHM_MAX_DEG, BEAM_FWHM_MIN_DEG

SCIENCE_PIPELINE_VERSION = "science-v1.0"

QUALITY_POLICIES = ("STRICT", "STANDARD", "PERMISSIVE")

# Beam vocabulary. FWHM is OPERATOR-PROVIDED operational metadata - REDUCE V1 does not carry beam information
# and no beam characterisation exists in this repo. Only status MEASURED (future evidence) may ever be described
# as a characterised beam.
BEAM_STATUSES = ("PROVISIONAL_DEFAULT", "CONFIGURED_OPERATIONAL", "MEASURED")


@dataclass(frozen=True)
class ScienceConfig:
    # --- input ---
    reduce_session_dir: str = ""

    # --- spatial grid (section 15-16) ---
    pixel_scale_deg: Optional[float] = None   # None -> derived from beam_fwhm_deg / pixels_per_beam
    pixels_per_beam: float = 4.0              # visualization sampling only - never claims better resolution (section 11)
    extent_margin_beams: float = 1.5          # grid extent = point bounding box + this many beam FWHMs of margin

    # --- beam (section 9-11) ---
    # The library default is a PLACEHOLDER, labelled as such: it is NOT read from observer_config.json or any
    # other file (an earlier revision claimed it was). The CLI refuses to run without an explicit beam.
    beam_fwhm_deg: float = 20.0
    beam_source: str = "library_default_placeholder (not read from any file)"
    beam_status: str = "PROVISIONAL_DEFAULT"
    beam_source_path: Optional[str] = None     # set when the value was READ from a file (observer_config.json)
    beam_source_field: Optional[str] = None
    beam_source_sha256: Optional[str] = None
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
    # LOCAL consistency of sigma (measured on real REDUCE Level 1, see docs/SCIENCE_ACCEPTANCE.md "sigma audit"):
    # 63 of 7849 bins/point have sigma 20-850x below the between-point scatter actually observed there. A bin whose
    # sigma is below `sigma_local_floor_fraction` x the running median of its +-window/2 neighbours cannot be trusted
    # (an isolated dip is not physical noise) and is EXCLUDED and counted, never floored (a floor would keep a
    # false-precision estimate). 0 disables the check.
    sigma_local_floor_fraction: float = 0.1
    sigma_local_window_channels: int = 129

    # --- moment products (section 43-46) ---
    moment_min_snr: float = 3.0                   # section 44-45: signal selection threshold before centroid/dispersion

    def __post_init__(self):
        def finite(name, value, *, positive=False, non_negative=False):
            if not np.isfinite(value) or (positive and not value > 0) or (non_negative and value < 0):
                raise ValueError(f"{name} must be finite{' and > 0' if positive else ''}"
                                 f"{' and >= 0' if non_negative else ''}, got {value!r}")

        if self.quality_policy not in QUALITY_POLICIES:
            raise ValueError(f"quality_policy must be one of {QUALITY_POLICIES}, got {self.quality_policy!r}")
        # `nan <= 0` is False in IEEE754, so every check below is written as a positive test on a finite value.
        finite("beam_fwhm_deg", self.beam_fwhm_deg, positive=True)
        if not (BEAM_FWHM_MIN_DEG <= self.beam_fwhm_deg <= BEAM_FWHM_MAX_DEG):
            raise ValueError(f"beam_fwhm_deg must be within [{BEAM_FWHM_MIN_DEG}, {BEAM_FWHM_MAX_DEG}] deg, "
                             f"got {self.beam_fwhm_deg}")
        if self.beam_status not in BEAM_STATUSES:
            raise ValueError(f"beam_status must be one of {BEAM_STATUSES}, got {self.beam_status!r}")
        if self.pixel_scale_deg is not None:
            finite("pixel_scale_deg", self.pixel_scale_deg, positive=True)
        finite("pixels_per_beam", self.pixels_per_beam, positive=True)
        finite("extent_margin_beams", self.extent_margin_beams, non_negative=True)
        finite("beam_cutoff_n_fwhm", self.beam_cutoff_n_fwhm, positive=True)
        finite("uncertainty_floor_relative", self.uncertainty_floor_relative, non_negative=True)
        finite("moment_min_snr", self.moment_min_snr, non_negative=True)
        finite("sigma_local_floor_fraction", self.sigma_local_floor_fraction, non_negative=True)
        if self.sigma_local_floor_fraction >= 1.0:
            raise ValueError("sigma_local_floor_fraction must be < 1")
        if not (isinstance(self.sigma_local_window_channels, int) and self.sigma_local_window_channels >= 3
                and self.sigma_local_window_channels % 2 == 1):
            raise ValueError("sigma_local_window_channels must be an odd integer >= 3")
        # `nan >= x` and `x >= nan` are both False, so a NaN bound would silently pass a bare `>=` check.
        if not (np.isfinite(self.velocity_window_min_m_s) and np.isfinite(self.velocity_window_max_m_s)):
            raise ValueError("velocity_window_min_m_s/_max_m_s must both be finite (not NaN, not Inf)")
        if self.velocity_window_min_m_s >= self.velocity_window_max_m_s:
            raise ValueError("velocity_window_min_m_s must be < velocity_window_max_m_s (zero/negative width rejected)")
        if not (0.0 <= self.min_spectral_coverage_fraction <= 1.0):
            raise ValueError("min_spectral_coverage_fraction must be in [0, 1]")

    def to_dict(self) -> dict:
        return asdict(self)

    def config_hash(self) -> str:
        # canonical serialization: sorted keys, fixed separators -> the hash never depends on field order
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
