"""Data model of SCIENCE FEATURE FORENSICS V1: configuration, candidate definition, per-point ForensicsPoint."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import numpy as np

C_LIGHT_M_S = 299_792_458.0
WINDOW_FRAMES = ("lsrk_velocity", "frequency", "channel")
SIGN_MODES = ("auto", "positive", "negative")
CENTROID_METHODS = ("gaussian", "halfmax", "peak")
SORT_MODES = ("capture", "ra", "dec", "galactic_l")
CANDIDATE_STATUSES = ("UNCLASSIFIED", "SKY_COHERENT", "TIME_COHERENT", "INSTRUMENT_COHERENT", "RFI_SUSPECT",
                      "INSUFFICIENT_EVIDENCE")     # V1 assigns only UNCLASSIFIED or INSUFFICIENT_EVIDENCE


def sanitize(obj: Any) -> Any:
    """JSON-safe copy: numpy scalars/arrays -> python, non-finite floats -> None (never a bare NaN literal)."""
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return sanitize(obj.tolist())
    if isinstance(obj, (np.floating, float)):
        return float(obj) if np.isfinite(obj) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def canonical_json(obj: Any) -> str:
    return json.dumps(sanitize(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class ForensicsConfig:
    """Every free parameter, explicit and hashable. Window units follow `window_frame`:
    lsrk_velocity -> m/s, frequency -> Hz (topocentric), channel -> channel index."""
    window_center: float = float("nan")
    window_half_width: float = float("nan")
    window_frame: str = "lsrk_velocity"
    feature_id: str = "FEATURE-1"
    sign: str = "auto"                      # signed feature: emission or absorption-like, never assumed
    centroid_method: str = "gaussian"       # primary; falls back gaussian -> halfmax -> peak (recorded per point)
    min_usable_bins: int = 6                # usable bins in the window for a point to be measured
    min_usable_points: int = 4              # measured points for campaign-level statistics
    min_snr_formal: float = 5.0             # detection gate: |fitted peak| / formal sigma; below it the point is NOT a detection
    fit_max_iter: int = 80
    # control windows / negative-map audit
    control_guard_fraction: float = 0.5     # gap between candidate and control window, in candidate widths
    control_min_valid_fraction: float = 0.8
    integration_window_min_m_s: float = -100_000.0     # default = SCIENCE V1 default window
    integration_window_max_m_s: float = 100_000.0
    n_velocity_intervals: int = 8
    sort_modes: tuple = ("capture", "ra", "dec", "galactic_l")
    confounding_corr_threshold: float = 0.7
    frame_ratio_threshold: float = 3.0      # RMS ratio between frames needed to say one frame is more coherent
    detection_method: str = "operator_window"
    # optional site for sidereal time (operator-provided, source recorded)
    site_latitude_deg: Optional[float] = None
    site_longitude_deg: Optional[float] = None
    site_source: Optional[str] = None
    site_source_path: Optional[str] = None
    site_source_sha256: Optional[str] = None

    def __post_init__(self):
        if self.window_frame not in WINDOW_FRAMES:
            raise ValueError(f"window_frame must be one of {WINDOW_FRAMES}")
        if self.sign not in SIGN_MODES:
            raise ValueError(f"sign must be one of {SIGN_MODES}")
        if self.centroid_method not in CENTROID_METHODS:
            raise ValueError(f"centroid_method must be one of {CENTROID_METHODS}")
        if not np.isfinite(self.window_center):
            raise ValueError("window_center must be finite")
        if not (np.isfinite(self.window_half_width) and self.window_half_width > 0):
            raise ValueError("window_half_width must be finite and > 0 (zero-width window rejected)")
        if not (isinstance(self.min_usable_bins, int) and self.min_usable_bins >= 4):
            raise ValueError("min_usable_bins must be an integer >= 4 (a 4-parameter fit needs more bins than parameters)")
        if not (np.isfinite(self.min_snr_formal) and self.min_snr_formal >= 0):
            raise ValueError("min_snr_formal must be finite and >= 0")
        if not (isinstance(self.min_usable_points, int) and self.min_usable_points >= 3):
            raise ValueError("min_usable_points must be an integer >= 3")
        if not (np.isfinite(self.integration_window_min_m_s) and np.isfinite(self.integration_window_max_m_s)
                and self.integration_window_min_m_s < self.integration_window_max_m_s):
            raise ValueError("integration window must be finite with min < max")
        if not (isinstance(self.n_velocity_intervals, int) and self.n_velocity_intervals >= 2):
            raise ValueError("n_velocity_intervals must be an integer >= 2")
        for mode in self.sort_modes:
            if mode not in SORT_MODES:
                raise ValueError(f"unknown sort mode {mode!r}; allowed {SORT_MODES}")
        if not (0.0 <= self.control_min_valid_fraction <= 1.0) or not (0.0 < self.confounding_corr_threshold <= 1.0):
            raise ValueError("control_min_valid_fraction in [0,1] and confounding_corr_threshold in (0,1] required")
        if (self.site_latitude_deg is None) != (self.site_longitude_deg is None):
            raise ValueError("site latitude and longitude must be given together")
        if self.site_latitude_deg is not None and not (-90 <= self.site_latitude_deg <= 90
                                                       and -180 <= self.site_longitude_deg <= 360):
            raise ValueError("site coordinates out of range")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sort_modes"] = list(self.sort_modes)
        return d

    def config_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode()).hexdigest()


@dataclass
class FeatureCandidate:
    """Definition + campaign-level summary of a candidate. `status` is deliberately conservative: V1 never names the nature."""
    id: str
    velocity_lsrk_center: Optional[float]      # m/s, campaign median of measured centroids
    velocity_width: Optional[float]            # m/s, campaign median FWHM
    frequency_equivalent: Optional[float]      # Hz (topocentric), campaign median centroid
    search_window: dict
    source: str
    detection_method: str
    status: str = "UNCLASSIFIED"

    def to_dict(self) -> dict:
        if self.status not in CANDIDATE_STATUSES:
            raise ValueError(f"invalid candidate status {self.status!r}")
        return sanitize(asdict(self))


@dataclass
class ForensicsPoint:
    """One REDUCE MasterSpectrum as seen by forensics (read-only view of Level 1)."""
    point_index: int
    t_start_s: float                 # POSIX seconds
    t_end_s: float
    ra_deg: float
    dec_deg: float
    frequency_hz: np.ndarray         # topocentric, as stored by REDUCE
    velocity_lsrk_m_s: np.ndarray
    value: np.ndarray                # relative_intensity
    sigma: np.ndarray
    mask: np.ndarray                 # int64 MaskFlag bits, 0 = GOOD
    lsrk_shift_m_s: float            # v_lsrk - c(1 - f/f0): constant across channels (verified at ingest)
    quality_state: str = "UNKNOWN"
    timestamp_utc: str = ""
    receiver: Optional[str] = None
    capture_ids: list = field(default_factory=list)
    rfi_ref_available: Optional[bool] = None
    source_h5_sha256: Optional[str] = None
    source_json_sha256: Optional[str] = None
    l_deg: float = float("nan")
    b_deg: float = float("nan")

    @property
    def t_mid_s(self) -> float:
        return 0.5 * (self.t_start_s + self.t_end_s)


@dataclass
class ForensicsInput:
    campaign_id: str
    reduce_session_id: str
    reduce_session_dir: str
    reduce_session_status: str
    reduce_manifest_sha256: str
    rest_frequency_hz: float
    points: list                                     # ForensicsPoint sorted by point_index
    exclusions: list = field(default_factory=list)
    unavailable: dict = field(default_factory=dict)  # metadata Level 1 does not carry (never inferred, RAW never opened)
