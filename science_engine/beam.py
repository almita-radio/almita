"""Gaussian circular beam weighting (sections 9-11, 24-25).

w_beam(theta) = exp(-4 ln(2) * theta^2 / FWHM^2)

Standard radial-Gaussian FWHM convention: theta=0 -> 1, theta=FWHM/2 ->
0.5 exactly (the definition of "full width at half maximum"). Verified
by test_science_beam.py rather than only asserted here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from science_engine.models import BeamModel

_FOUR_LN2 = 4.0 * np.log(2.0)


def beam_weight(theta_deg: np.ndarray, beam: BeamModel) -> np.ndarray:
    """Vectorized beam response at angular distance theta_deg. Returns 0.0
    exactly beyond `beam.cutoff_n_fwhm` FWHMs (section 25) - not because
    the Gaussian is truly zero there, but because a mathematically tiny
    tail contributing to a weighted sum is not worth the outlier risk it
    invites once real data has any pathological uncertainty value; the
    default N=3 leaves ~1e-11 of peak response outside the cutoff, well
    below anything a real per-point uncertainty floor would resolve."""
    theta_deg = np.asarray(theta_deg, dtype=float)
    weight = np.exp(-_FOUR_LN2 * (theta_deg ** 2) / (beam.fwhm_deg ** 2))
    cutoff_deg = beam.cutoff_n_fwhm * beam.fwhm_deg
    return np.where(theta_deg <= cutoff_deg, weight, 0.0)


def beam_footprint_radius_deg(beam: BeamModel) -> float:
    """The radius beyond which beam_weight is defined to be exactly zero
    - used to restrict which input points are even considered for a given
    output pixel (a cheap pre-filter, not the weighting itself)."""
    return beam.cutoff_n_fwhm * beam.fwhm_deg


def beam_settings_from_observer_config(path: str | Path) -> dict:
    """Read the operator's configured beam FWHM from an observer_config.json FILE THE OPERATOR NAMED and
    return every field needed to persist where it came from (path, field, sha256). This is the ONLY place
    SCIENCE reads a beam from a file, and it never searches for one (in particular it never navigates into
    data/mosaic/*/grid_metadata.json). The value is operational metadata supplied by the operator."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    field = "observation_defaults.beam_fwhm_deg"
    value = json.loads(raw)["observation_defaults"]["beam_fwhm_deg"]
    return {"beam_fwhm_deg": float(value), "beam_source": f"{path.name}:{field}", "beam_status": "CONFIGURED_OPERATIONAL",
            "beam_source_path": str(path), "beam_source_field": field,
            "beam_source_sha256": hashlib.sha256(raw).hexdigest()}
