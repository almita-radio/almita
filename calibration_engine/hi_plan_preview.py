"""Alt/Az preview of the CALIBRATE reference wizard's HI plan - derived ONLY from the plan the wizard itself saved.

The wizard's `plan-hi` step (calibrate_reference_wizard.cmd_plan_hi -> hi_reference_selection) already computes,
for each candidate, its altitude/azimuth at the plan instant and a real altitude check over the hold. This module
never recomputes a position: it re-shapes those exact numbers for the web sky view, so the preview cannot diverge
from what `capture-hi` will point at. The only thing that changes the points is a new plan.

Honesty rules:
  - positions are valid at the plan's own instant (`generated_utc`); the sky moves afterwards and `capture-hi`
    re-checks the altitude right before its GOTO. The preview says so instead of extrapolating.
  - a point is flagged when the plan's own numbers put it outside the configured limits (minimum elevation over
    the hold, or the altitude check not clearing).
  - there is no local horizon / obstacle model in this project, so the preview never claims a zone is free of
    obstacles: `horizon.available` is False and the note says the operator must check the sky physically.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Order capture-hi visits the HI points (WizardStep: READY_HI_ALTO before READY_HI_BAJO).
HI_ORDER = ("HI_ALTO", "HI_BAJO")
HORIZON_NOTE = ("No local horizon or obstacle model is configured for this site: obstacles (walls, trees, roofs) "
                "are NOT evaluated. Check physically that both zones and the slew between them are clear.")


def _limit_problems(cand: Dict[str, Any], min_elevation_deg: Optional[float]) -> List[str]:
    problems: List[str] = []
    check = cand.get("altitude_check") or {}
    alt = cand.get("alt_deg")
    if alt is None or cand.get("az_deg") is None:
        problems.append("position missing from the plan")
    if min_elevation_deg is not None and alt is not None and alt < min_elevation_deg:
        problems.append(f"altitude {alt:.1f} deg below the {min_elevation_deg:g} deg minimum at the plan instant")
    worst = check.get("worst_case_altitude_deg")
    if min_elevation_deg is not None and worst is not None and worst < min_elevation_deg:
        problems.append(f"worst-case altitude {worst:.1f} deg over the {check.get('hold_seconds', '?')} s hold is "
                        f"below the {min_elevation_deg:g} deg minimum")
    if check and check.get("clears") is False:
        problems.append("the plan's own altitude check does not clear")
    if alt is not None and alt > 90:
        problems.append(f"altitude {alt:.1f} deg is not physical")
    return problems


def build_preview(hi_plan: Optional[Dict[str, Any]], *, current_label: Optional[str] = None,
                  measured: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Preview payload for the sky view, or None when there is no plan yet."""
    if not hi_plan or not hi_plan.get("candidates"):
        return None
    min_el = hi_plan.get("min_elevation_deg")
    beam = hi_plan.get("beam_fwhm_deg")
    measured = list(measured or [])
    points: List[Dict[str, Any]] = []
    for order, label in enumerate(HI_ORDER, 1):
        cand = hi_plan["candidates"].get(label)
        if not cand:
            continue
        problems = _limit_problems(cand, min_el)
        check = cand.get("altitude_check") or {}
        points.append({
            "order": order, "label": label,
            "az_deg": cand.get("az_deg"), "alt_deg": cand.get("alt_deg"),
            "ra_hours": cand.get("ra_hours"), "dec_deg": cand.get("dec_deg"),
            "radius_deg": (beam / 2.0) if beam else None,        # beam half-width: the zone the antenna sees
            "worst_case_altitude_deg": check.get("worst_case_altitude_deg"),
            "margin_deg": check.get("margin_deg"),
            "within_limits": not problems, "limit_problems": problems,
            "status": "MEASURED" if label in measured else ("NEXT" if label == current_label else "PLANNED"),
        })
    return {
        "reference_utc": hi_plan.get("generated_utc"),
        "reference_note": ("positions at the plan instant; the sky moves afterwards - capture-hi re-checks the "
                           "altitude just before its GOTO, and RE-PROPOSE refreshes them"),
        "limits": {"min_elevation_deg": min_el, "hold_seconds": hi_plan.get("hold_seconds"),
                   "min_separation_deg": hi_plan.get("min_separation_deg"), "beam_fwhm_deg": beam},
        "points": points,
        "all_within_limits": all(p["within_limits"] for p in points) and len(points) == len(HI_ORDER),
        "horizon": {"available": False, "note": HORIZON_NOTE},
        "source": "the wizard's own saved hi_plan (no position recomputed here)",
    }
