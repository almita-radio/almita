"""SCIENCE's core data model: ingested input, beam, grid, cube, and map
products. Plain dataclasses with explicit to_dict()/from arrays - no
pickle, matching reduce_engine.models's own convention.

SCIENCE reuses reduce_engine.models.MaskFlag directly (the bitmask
semantics are REDUCE's contract, not something SCIENCE redefines) but
never imports reduce_engine's pipeline/storage internals - only the
frozen MasterSpectrum-level vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

SCIENCE_SCHEMA_VERSION = "1.0"


class ScienceQualityState:
    """Deliberately the same 4-state vocabulary as REDUCE's QualityState,
    but a SEPARATE layer - a science product's quality is never conflated
    with or overwritten by its input points' REDUCE quality (section 131).
    """
    GOOD = "GOOD"
    WARNING = "WARNING"
    BAD = "BAD"
    UNKNOWN = "UNKNOWN"

    VALID = frozenset({GOOD, WARNING, BAD, UNKNOWN})


@dataclass
class ScienceQualityReport:
    state: str
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.state not in ScienceQualityState.VALID:
            raise ValueError(f"invalid science quality state {self.state!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reasons": list(self.reasons), "metrics": dict(self.metrics)}


@dataclass
class ScienceInputPoint:
    """One REDUCE MasterSpectrum, as consumed by SCIENCE - a read-only
    view, never mutated after ingest."""
    campaign_id: str
    reduce_session_id: str
    point_index: int
    ra_hours: float
    dec_degrees: float
    ra_deg: float  # converted once, explicitly, at ingest (section 12) - hours*15
    timestamp_start_utc: Optional[str]
    frequency_hz: np.ndarray
    velocity_lsrk_m_s: Optional[np.ndarray]
    velocity_frame: str
    relative_intensity: np.ndarray
    uncertainty: np.ndarray
    mask: np.ndarray
    n_contributing: np.ndarray
    integration_time_seconds: float
    reduce_quality_state: str
    calibration_level: str


@dataclass
class ScienceInput:
    """The full, validated ingest result for one REDUCE session -
    SCIENCE's only entry point. `contract_problems` is empty iff
    `validate_science_input` (reduce_engine.science_contract) passed."""
    reduce_session_dir: str
    campaign_id: str
    reduce_session_id: str
    reduce_schema_version: str
    reduce_session_status: str
    points: list[ScienceInputPoint]
    contract_problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.contract_problems


@dataclass(frozen=True)
class BeamModel:
    """Gaussian circular beam. `status` must never be silently omitted:
    a beam FWHM not physically measured is CONFIGURED_OPERATIONAL or
    PROVISIONAL_OPERATIONAL, never presented as measured truth (section
    10, 107). See docs/SCIENCE_MODEL.md's Beam section for where the
    default actually comes from and the known 14.0/20.0/1.5 discrepancy
    this repo carries across different beam-FWHM sources."""
    model_type: str = "gaussian_circular"
    fwhm_deg: float = 20.0
    source: str = "observer_config.json:observation_defaults.beam_fwhm_deg"
    status: str = "CONFIGURED_OPERATIONAL"
    cutoff_n_fwhm: float = 3.0

    def __post_init__(self):
        if self.fwhm_deg <= 0:
            raise ValueError(f"beam fwhm_deg must be > 0, got {self.fwhm_deg}")
        if self.model_type != "gaussian_circular":
            raise ValueError(f"only gaussian_circular is supported in V1, got {self.model_type!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"model_type": self.model_type, "fwhm_deg": self.fwhm_deg, "source": self.source,
                "status": self.status, "cutoff_n_fwhm": self.cutoff_n_fwhm}


@dataclass(frozen=True)
class ScienceGrid:
    """A local tangent-plane spatial grid (section 16), same projection
    convention as OBSERVE's own grid_generator.py ("tangent-plane",
    RA scaled by cos(dec)) - not a full WCS reprojection, appropriate for
    the modest (few-degree) fields V1 targets. `frame` is always "icrs";
    Galactic l/b is a documented derived view, never a second storage
    frame (section 12)."""
    frame: str
    center_ra_deg: float
    center_dec_deg: float
    width_deg: float
    height_deg: float
    pixel_scale_deg: float
    nx: int
    ny: int
    projection: str = "tangent-plane"

    def __post_init__(self):
        if self.frame != "icrs":
            raise ValueError(f"only icrs is supported as the canonical storage frame, got {self.frame!r}")
        if self.pixel_scale_deg <= 0 or self.nx <= 0 or self.ny <= 0:
            raise ValueError("grid pixel_scale_deg, nx, ny must all be > 0")

    def to_dict(self) -> dict[str, Any]:
        return {"frame": self.frame, "center_ra_deg": self.center_ra_deg, "center_dec_deg": self.center_dec_deg,
                "width_deg": self.width_deg, "height_deg": self.height_deg,
                "pixel_scale_deg": self.pixel_scale_deg, "nx": self.nx, "ny": self.ny,
                "projection": self.projection}


@dataclass
class ScienceCube:
    """SCIENCE's flagship LEVEL 2 product. Axis order is always
    [velocity, y, x] (matches alignment_engine/hi/cube_reduction.py's own
    (vel, lat, lon) convention, for consistency across this repo's two
    independent cube producers) - `test_reduce...` no, see
    test_science_cube_axis_order pins this so a future change can never
    silently transpose axes."""
    grid: ScienceGrid
    velocity_lsrk_m_s: np.ndarray               # (Nv,)
    relative_intensity: np.ndarray               # (Nv, Ny, Nx)
    uncertainty: np.ndarray                       # (Nv, Ny, Nx)
    weight_sum: np.ndarray                        # (Nv, Ny, Nx) - beam*uncertainty weight actually applied
    n_contributing: np.ndarray                    # (Nv, Ny, Nx) - integer count of points contributing
    valid: np.ndarray                             # (Nv, Ny, Nx) bool - False = no usable contribution

    def __post_init__(self):
        nv, ny, nx = self.velocity_lsrk_m_s.shape[0], self.grid.ny, self.grid.nx
        expected = (nv, ny, nx)
        for name in ("relative_intensity", "uncertainty", "weight_sum", "n_contributing", "valid"):
            shape = getattr(self, name).shape
            if shape != expected:
                raise ValueError(f"{name} shape {shape} != expected {expected}")


@dataclass
class SpatialMap:
    """A generic 2D (Ny, Nx) science product - used for the integrated
    map, coverage map, uncertainty map, quality map, N-contributing map,
    RFI-fraction map, and each channel map. `kind` says which."""
    grid: ScienceGrid
    kind: str
    value: np.ndarray             # (Ny, Nx)
    uncertainty: Optional[np.ndarray]
    weight_sum: np.ndarray        # (Ny, Nx)
    n_contributing: np.ndarray    # (Ny, Nx)
    valid: np.ndarray             # (Ny, Nx) bool
    units: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        expected = (self.grid.ny, self.grid.nx)
        if self.value.shape != expected:
            raise ValueError(f"value shape {self.value.shape} != grid shape {expected}")
