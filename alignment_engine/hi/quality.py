"""HI-specific quality/sync-eligibility gate (Fase 39-40).

Deliberately standalone from alignment_engine/sync_flow.py's existing
prepare_sync() (already tested, already wired into the CLI for both
SOLAR and HI) - this is an ADDITIONAL, stricter pre-check specific to HI's
richer trust model (ReferenceTrust, not just a bare is_observational
bool), meant to run BEFORE prepare_sync() and give a human-readable
ELIGIBLE/NOT_ELIGIBLE verdict with reasons. It does not call prepare_sync()
and does not replace its own (still authoritative) eligibility check -
see the final report's "HI ARCHITECTURE" section for how these two are
meant to compose.

No override parameter exists here, matching sync_flow.py's own
tokenless-refusal precedent for synthetic references.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from alignment_engine.hi.reference_trust import SYNC_ELIGIBLE_TRUST_LEVELS, ReferenceTrust

# Conservative default (Fase 40: "Si no hay evidencia: config conservative")
# - no empirical real-hardware HI pointing-error distribution exists yet,
# so this is deliberately tight relative to the ~20 deg beam, not tuned to
# any specific observed result.
DEFAULT_MAX_OFFSET_DEG = 5.0
DEFAULT_MIN_CONFIDENCE = 0.75
DEFAULT_MIN_VALID_FRACTION = 0.6
DEFAULT_MIN_EDGE_DISTANCE_DEG = 1.0  # how far the fitted offset must stay inside the raster's own extent


@dataclass
class SyncEligibilityResult:
    verdict: str  # "ELIGIBLE" | "NOT_ELIGIBLE"
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reasons": self.reasons}


def evaluate_sync_eligibility(*, trust: ReferenceTrust, fit_rating: str, confidence: float,
                               valid_fraction: float, offset_east_deg: float, offset_north_deg: float,
                               raster_half_extent_deg: float,
                               max_offset_deg: float = DEFAULT_MAX_OFFSET_DEG,
                               min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                               min_valid_fraction: float = DEFAULT_MIN_VALID_FRACTION,
                               min_edge_distance_deg: float = DEFAULT_MIN_EDGE_DISTANCE_DEG) -> SyncEligibilityResult:
    """Every failing check is collected (not just the first) so an operator
    sees the whole picture, not one reason at a time across repeated calls."""
    reasons: List[str] = []

    if trust not in SYNC_ELIGIBLE_TRUST_LEVELS:
        reasons.append(f"reference trust is {trust.value}, not one of "
                        f"{sorted(t.value for t in SYNC_ELIGIBLE_TRUST_LEVELS)}")
    if fit_rating != "GOOD":
        reasons.append(f"fit rating is {fit_rating!r}, not GOOD")
    if confidence < min_confidence:
        reasons.append(f"confidence {confidence:.3f} below {min_confidence:.3f}")
    if valid_fraction < min_valid_fraction:
        reasons.append(f"valid_fraction {valid_fraction:.3f} below {min_valid_fraction:.3f}")

    offset_mag = (offset_east_deg ** 2 + offset_north_deg ** 2) ** 0.5
    if offset_mag > max_offset_deg:
        reasons.append(f"measured offset {offset_mag:.2f} deg exceeds the {max_offset_deg:.1f} deg safety ceiling")

    edge_distance = raster_half_extent_deg - max(abs(offset_east_deg), abs(offset_north_deg))
    if edge_distance < min_edge_distance_deg:
        reasons.append(f"fitted offset is only {edge_distance:.2f} deg from the raster edge "
                        f"(need >= {min_edge_distance_deg:.1f} deg margin) - possible edge-truncated fit")

    if reasons:
        return SyncEligibilityResult("NOT_ELIGIBLE", reasons)
    return SyncEligibilityResult("ELIGIBLE", ["all HI-specific eligibility checks passed"])
