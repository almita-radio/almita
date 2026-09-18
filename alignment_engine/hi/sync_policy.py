"""alignment_phase policy (Fase 20-21): REAL_VALIDATED reference + GOOD fit
is necessary but NOT sufficient for SYNC. The first real HI night scan is
observational only - this module makes that a checked, testable rule
instead of an operator promise.

FIRST_LIGHT_HI: the very first real-hardware HI scan (and every one after
it until an operator explicitly advances the phase - there is no
automatic promotion). SYNC is unconditionally blocked in this phase,
regardless of trust or quality - see evaluate_phase_gate() below, which
has no override parameter.

ESTABLISHED_HI: reachable only after REPEATABILITY_REQUIREMENT (Fase 21)
is met - at least two independent solutions (different scans and/or
different targets) whose offsets agree within their combined uncertainty.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class AlignmentPhase(str, Enum):
    FIRST_LIGHT_HI = "FIRST_LIGHT_HI"
    ESTABLISHED_HI = "ESTABLISHED_HI"


@dataclass
class PastSolution:
    session_id: str
    target_label: str
    offset_east_deg: float
    offset_north_deg: float
    uncertainty_east_deg: float
    uncertainty_north_deg: float


@dataclass
class RepeatabilityResult:
    satisfied: bool
    reason: str
    compared_sessions: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"satisfied": self.satisfied, "reason": self.reason, "compared_sessions": self.compared_sessions}


def check_repeatability(candidate: PastSolution, history: List[PastSolution],
                         agreement_sigma: float = 3.0, min_independent_solutions: int = 2) -> RepeatabilityResult:
    """Fase 21: at least `min_independent_solutions` (candidate + history)
    total, with pairwise offsets agreeing within `agreement_sigma` combined
    uncertainty. Not a hardcoded number pulled from nowhere: 2 independent
    solutions is the minimum that can demonstrate repeatability AT ALL
    (one solution can never be "repeatable" by itself); 3-sigma agreement
    is the same conventional bar used elsewhere in this codebase's quality
    gates. Architecture (this function's signature) supports comparing
    across different targets, not just repeated scans of one target."""
    solutions = history + [candidate]
    if len(solutions) < min_independent_solutions:
        return RepeatabilityResult(False, f"only {len(solutions)} independent solution(s), "
                                           f"need >= {min_independent_solutions}", [])

    compared = [s.session_id for s in solutions]
    for i, a in enumerate(solutions):
        for b in solutions[i + 1:]:
            combined_sigma_east = math.hypot(a.uncertainty_east_deg, b.uncertainty_east_deg) or 1e-6
            combined_sigma_north = math.hypot(a.uncertainty_north_deg, b.uncertainty_north_deg) or 1e-6
            d_east = abs(a.offset_east_deg - b.offset_east_deg) / combined_sigma_east
            d_north = abs(a.offset_north_deg - b.offset_north_deg) / combined_sigma_north
            if d_east > agreement_sigma or d_north > agreement_sigma:
                return RepeatabilityResult(
                    False, f"{a.session_id} vs {b.session_id} disagree by "
                           f"{d_east:.1f}sigma(E)/{d_north:.1f}sigma(N), exceeds {agreement_sigma:.1f}sigma",
                    compared)
    return RepeatabilityResult(True, f"{len(solutions)} independent solutions agree within "
                                      f"{agreement_sigma:.1f} sigma", compared)


@dataclass
class PhaseGateResult:
    sync_allowed: bool
    reason: str

    def to_dict(self) -> dict:
        return {"sync_allowed": self.sync_allowed, "reason": self.reason}


def evaluate_phase_gate(phase: AlignmentPhase, sync_eligibility_verdict: str,
                         repeatability: Optional[RepeatabilityResult] = None,
                         deployment_gate: Optional["MovementGateResult"] = None) -> PhaseGateResult:
    """The final word on whether SYNC may even be attempted, layered ON
    TOP of (never replacing) hi/quality.py's own ELIGIBLE/NOT_ELIGIBLE
    check and quality_v2's GOOD/MARGINAL/BAD verdict. No parameter here
    can force sync_allowed=True during FIRST_LIGHT_HI - that is the whole
    point of this function existing.

    `deployment_gate` (Fase's deployment interlock, alignment_engine.
    deployment_state.check_hardware_movement_allowed): a future SYNC is a
    mount write like any other, so it is gated on FIELD deployment exactly
    like GOTO/raster movement - there is no separate "SYNC doesn't count
    as movement" carve-out. Passing None here is the fail-closed default
    (same as passing repeatability=None): it blocks ESTABLISHED_HI's own
    sync_allowed=True, it does not silently permit it. This function does
    not itself perform any hardware operation - callers must independently
    also check deployment/eligibility/quality before ever reaching this
    point; this is the architecture, not an executable "sync now" path
    (none exists yet)."""
    if phase == AlignmentPhase.FIRST_LIGHT_HI:
        return PhaseGateResult(False, "alignment_phase=FIRST_LIGHT_HI: SYNC is unconditionally disabled for the "
                                       "first real HI night scan, regardless of reference trust or fit quality - "
                                       "this phase is observational only ('do we see the predicted pattern', "
                                       "not 'do we trust it enough to correct the mount')")
    if deployment_gate is None or not deployment_gate.allowed:
        detail = deployment_gate.reason if deployment_gate is not None else \
            "no deployment gate result provided - failing closed, never assuming FIELD"
        return PhaseGateResult(False, f"deployment interlock not satisfied: {detail}")
    if sync_eligibility_verdict != "ELIGIBLE":
        return PhaseGateResult(False, f"sync_eligibility={sync_eligibility_verdict}")
    if repeatability is None:
        return PhaseGateResult(False, "no repeatability evidence provided - see check_repeatability()")
    if not repeatability.satisfied:
        return PhaseGateResult(False, f"repeatability not satisfied: {repeatability.reason}")
    return PhaseGateResult(True, f"ESTABLISHED_HI, FIELD-deployed, ELIGIBLE, and repeatability satisfied "
                                  f"({repeatability.reason})")
