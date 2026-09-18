"""Target-region selection with an explicit information-content score
(Fase 14-15) - extends alignment.py's existing choose_hi_region() (which
already does altitude filtering + a probe-pattern contrast score) with a
local GRADIENT check, so a region whose only "contrast" comes from one
distant unrelated feature (still flat right where the beam actually sits)
is not preferred over a region with real local structure.

Score components, all computed from the SAME beam-convolved value
function the fitter itself will use (never a different, unconvolved
estimate that could disagree with what is actually observable):
  - local gradient magnitude (finite differences at a small step - "is
    there a slope HERE, not just somewhere in the wider probe pattern")
  - probe-pattern contrast (std over a multiscale ring pattern, as
    choose_hi_region already computes - catches larger-scale structure a
    purely local gradient could miss)
  - signal level (log1p(mean) - a flat but very bright region is still
    penalized relative to one with real structure, but not to zero)

A region is REJECTED (score = -inf, never silently accepted) if either
component is at or below its own degenerate floor - Fase 15's explicit
"evitar mapa plano" / "estructura simétrica degenerada" requirement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import numpy as np
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time

from alignment import multiscale_pattern, offset_coordinates

MIN_GRADIENT_FLOOR = 1e-6
MIN_CONTRAST_FLOOR = 1e-6


@dataclass
class TargetScore:
    accepted: bool
    reason: str
    gradient_magnitude: Optional[float] = None
    probe_contrast: Optional[float] = None
    mean_signal: Optional[float] = None
    altitude_deg: Optional[float] = None
    score: float = float("-inf")

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "reason": self.reason,
                "gradient_magnitude": self.gradient_magnitude, "probe_contrast": self.probe_contrast,
                "mean_signal": self.mean_signal, "altitude_deg": self.altitude_deg, "score": self.score}


def score_candidate(center: SkyCoord, value_fn: Callable[[SkyCoord], np.ndarray],
                     location: EarthLocation, obstime: Time, min_altitude_deg: float,
                     gradient_step_deg: float = 1.0) -> TargetScore:
    altaz = center.transform_to(AltAz(obstime=obstime, location=location))
    altitude = float(altaz.alt.deg)
    if altitude < min_altitude_deg:
        return TargetScore(False, f"altitude {altitude:.1f} deg below floor {min_altitude_deg:.1f} deg",
                            altitude_deg=altitude)

    probe_points = multiscale_pattern(center, (5.0, 2.0), 8)
    probe_values = np.asarray(value_fn(probe_points), dtype=float)
    finite_probe = probe_values[np.isfinite(probe_values)]
    if finite_probe.size < 4:
        return TargetScore(False, "insufficient valid probe samples (reference coverage gap?)",
                            altitude_deg=altitude)
    probe_contrast = float(np.std(finite_probe))
    mean_signal = float(np.mean(finite_probe))

    grad_points = offset_coordinates(center, [gradient_step_deg, -gradient_step_deg, 0.0, 0.0],
                                      [0.0, 0.0, gradient_step_deg, -gradient_step_deg])
    grad_values = np.asarray(value_fn(grad_points), dtype=float)
    if not np.all(np.isfinite(grad_values)):
        return TargetScore(False, "gradient probe hit a reference coverage gap", altitude_deg=altitude,
                            probe_contrast=probe_contrast, mean_signal=mean_signal)
    d_east = (grad_values[0] - grad_values[1]) / (2 * gradient_step_deg)
    d_north = (grad_values[2] - grad_values[3]) / (2 * gradient_step_deg)
    gradient_magnitude = float(math.hypot(d_east, d_north))

    if gradient_magnitude <= MIN_GRADIENT_FLOOR:
        return TargetScore(False, "degenerate: no local gradient at this exact pointing (flat or symmetric)",
                            gradient_magnitude=gradient_magnitude, probe_contrast=probe_contrast,
                            mean_signal=mean_signal, altitude_deg=altitude)
    if probe_contrast <= MIN_CONTRAST_FLOOR:
        return TargetScore(False, "degenerate: no structure across the wider probe pattern",
                            gradient_magnitude=gradient_magnitude, probe_contrast=probe_contrast,
                            mean_signal=mean_signal, altitude_deg=altitude)

    score = gradient_magnitude * probe_contrast * math.log1p(max(mean_signal, 0.0))
    return TargetScore(True, "ok", gradient_magnitude=gradient_magnitude, probe_contrast=probe_contrast,
                        mean_signal=mean_signal, altitude_deg=altitude, score=score)


def select_best_target_from_grid(candidates: SkyCoord, value_fn: Callable[[SkyCoord], np.ndarray],
                                  location: EarthLocation, obstime: Time, min_altitude_deg: float,
                                  beam_fwhm_deg: float) -> Tuple[SkyCoord, Dict]:
    """Evaluates score_candidate() over a coarse subsample of `candidates`
    (a grid provider hands in, e.g. every Nth pixel center) and returns the
    single best-scoring one. Raises RuntimeError if NONE score above the
    degenerate floor - never silently returns a bad target."""
    candidates = SkyCoord(candidates).reshape((-1,))
    step = max(1, candidates.size // 200)
    best: Optional[Tuple[float, SkyCoord, TargetScore]] = None
    for center in candidates[::step]:
        result = score_candidate(center, value_fn, location, obstime, min_altitude_deg)
        if result.accepted and (best is None or result.score > best[0]):
            best = (result.score, center, result)
    if best is None:
        raise RuntimeError("no candidate target passed visibility + information-content checks "
                            "(all flat, degenerate, below altitude floor, or in a coverage gap)")
    _, center, result = best
    return center, {"provider": "fits_moment_map", "is_observational": True, **result.to_dict()}
