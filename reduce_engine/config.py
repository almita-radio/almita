"""ReduceConfig: every free parameter of the pipeline, explicit and hashable.
Same input + same config + same software -> same output (determinism, per
docs/REDUCE_PIPELINE.md). Never a hidden default buried in a function body
that config_hash() can't see.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Optional

PIPELINE_VERSION = "reduce-v1.0"


@dataclass(frozen=True)
class ReduceConfig:
    fft_size: int = 8192
    combine: str = "median"                 # robust_psd_from_iq's segment-combine mode
    dc_mask_threshold_db: float = 0.5
    dc_reference_inner_hz: float = 20_000.0
    dc_reference_outer_hz: float = 100_000.0
    edge_fraction: float = 0.02             # conservative default; CONFIGURED_DEFAULT unless calibration profile overrides
    spur_smooth_bins: int = 101
    spur_z_threshold: float = 8.0
    spur_persistence_threshold: float = 0.70
    baseline_degree: int = 2
    baseline_iterations: int = 5
    baseline_sigma: float = 4.0
    velocity_frame: str = "lsrk"            # one of alignment_engine.hi.velocity.SUPPORTED_FRAMES
    hi_rest_frequency_hz: float = 1_420_405_751.77
    averaging_method: str = "inverse_variance_weighted"  # fallback: "equal_weight"; see docs/REDUCE_PIPELINE.md
    sigma_clip_threshold: float = 5.0
    calibration_profile_path: Optional[str] = None
    rfi_ref_max_time_delta_seconds: float = 3600.0
    rfi_ref_min_match_confidence: float = 0.5
    random_seed: int = 20260919

    def to_dict(self) -> dict:
        return asdict(self)

    def config_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
