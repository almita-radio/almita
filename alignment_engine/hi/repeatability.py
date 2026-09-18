"""Repeatability comparator (Fase 9-11): given two alignment results,
decide whether they agree within their own measurement uncertainty - the
question an operator actually has after "scan A, wait, scan B, no SYNC in
between" (Fase 11's repeatability experiment design, see
docs/HI_FIRST_LIGHT_RUNBOOK.md and REPEATABILITY_EXPERIMENT below).

This is deliberately SEPARATE from sync_policy.check_repeatability(),
which only answers a bool satisfied/not-satisfied across N solutions for
the SYNC gate itself (Fase 21). This module answers the richer,
pre-SYNC, human-facing question: CONSISTENT / MARGINAL / INCONSISTENT,
PLUS whether the two results are even directly comparable in the first
place.

LOCAL VS GLOBAL OFFSET (Fase 10): two alignment results in DIFFERENT sky
regions can legitimately differ - polar misalignment, mount flexure,
non-orthogonality, feed offset, and other mount-model effects are all
real, physical, and position-dependent. A large offset difference between
two DIFFERENT targets/regions is therefore a POINTING-MODEL DIAGNOSTIC,
not necessarily a failure - this module never labels that case
INCONSISTENT. Strict CONSISTENT/MARGINAL/INCONSISTENT verdicts are only
produced when the two results are of the SAME target OR within one beam
width of each other (see `region_radius_deg` below); anything else gets
its own verdict, DIFFERENT_REGION, with the raw numbers still reported
for a human to interpret as a pointing-model diagnostic afterward.

NO MAGIC THRESHOLDS (explicit user requirement): the sigma bands below
are the SAME conventional statistical language already used elsewhere in
this codebase (sync_policy.check_repeatability's own 3-sigma "clearly
disagree" bar, and fitting.py's own GOOD/MARGINAL/BAD quality bands) -
1-sigma = "within measurement noise" and 3-sigma = "the existing
disagreement bar" are standard statistical convention, not new numbers
invented for this specific comparison. When neither result carries any
uncertainty estimate, this module refuses to guess one - it reports
UNKNOWN rather than fabricate a verdict from nothing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from astropy.coordinates import SkyCoord
import astropy.units as u

CONSISTENT_SIGMA = 1.0    # within this many combined-sigma: within measurement noise
INCONSISTENT_SIGMA = 3.0  # beyond this many combined-sigma: the existing disagreement bar
                            # (matches sync_policy.check_repeatability's own default)
DEFAULT_REGION_RADIUS_DEG = 5.0  # only used when NEITHER result carries a beam_fwhm_deg


@dataclass
class AlignmentResultSummary:
    """Everything compare_alignment_results() needs from ONE analysis.
    Build one directly, or from a replay's analysis directory via
    from_analysis_dir() (reads analysis_result.json + analysis_config.json
    + source_session.json + that session's own target.json - all
    read-only, no mount/SDR)."""
    session_id: str
    target_label: str
    sky_position: SkyCoord
    beam_fwhm_deg: Optional[float]
    reference_id: Optional[str]  # e.g. "<survey>/<version>/<sha256[:12]>"
    offset_east_deg: float
    offset_north_deg: float
    uncertainty_east_deg: Optional[float]
    uncertainty_north_deg: Optional[float]

    @classmethod
    def from_analysis_dir(cls, analysis_dir: str, *, target_label: Optional[str] = None
                           ) -> "AlignmentResultSummary":
        analysis_path = Path(analysis_dir)
        result = json.loads((analysis_path / "analysis_result.json").read_text())
        config = json.loads((analysis_path / "analysis_config.json").read_text())
        source = json.loads((analysis_path / "source_session.json").read_text())
        session_dir = Path(source["source_session_dir"])
        target = json.loads((session_dir / "target.json").read_text())

        fit = result.get("fit") or {}
        if not fit:
            raise ValueError(f"{analysis_dir}: no fit result (valid_count/total_count="
                              f"{result.get('valid_count')}/{result.get('total_count')}) - "
                              f"cannot build a comparable summary from a fit-less analysis")
        estimate = fit.get("estimate", {})
        quality = result.get("quality") or {}
        bootstrap = quality.get("bootstrap")
        refinement = fit.get("refinement")

        uncertainty_east = uncertainty_north = None
        if bootstrap and bootstrap.get("stable"):
            uncertainty_east = bootstrap.get("sigma_east_deg")
            uncertainty_north = bootstrap.get("sigma_north_deg")
        elif refinement:
            uncertainty_east = refinement.get("uncertainty_east_deg")
            uncertainty_north = refinement.get("uncertainty_north_deg")

        manifest_path = Path(config.get("manifest_path", ""))
        reference_id = None
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            reference_id = f"{manifest.get('survey')}/{manifest.get('version')}/{(manifest.get('sha256') or '')[:12]}"

        sky_position = SkyCoord(ra=target["icrs_ra_hours"] * u.hourangle, dec=target["icrs_dec_deg"] * u.deg)
        return cls(
            session_id=session_dir.name, target_label=target_label or session_dir.name,
            sky_position=sky_position, beam_fwhm_deg=config.get("beam_fwhm_deg"), reference_id=reference_id,
            offset_east_deg=estimate.get("offset_ra_deg", float("nan")),
            offset_north_deg=estimate.get("offset_dec_deg", float("nan")),
            uncertainty_east_deg=uncertainty_east, uncertainty_north_deg=uncertainty_north,
        )


@dataclass
class ComparisonResult:
    verdict: str  # "CONSISTENT" | "MARGINAL" | "INCONSISTENT" | "DIFFERENT_REGION" | "UNKNOWN"
    reason: str
    same_target: bool
    same_region: bool
    delta_east_deg: float
    delta_north_deg: float
    sigma_east: Optional[float]
    sigma_north: Optional[float]
    separation_deg: float
    warnings: List[str]

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason, "same_target": self.same_target,
                "same_region": self.same_region, "delta_east_deg": self.delta_east_deg,
                "delta_north_deg": self.delta_north_deg, "sigma_east": self.sigma_east,
                "sigma_north": self.sigma_north, "separation_deg": self.separation_deg,
                "warnings": self.warnings}


def compare_alignment_results(result_a: AlignmentResultSummary, result_b: AlignmentResultSummary,
                               *, region_radius_deg: Optional[float] = None) -> ComparisonResult:
    """Strict repeatability (CONSISTENT/MARGINAL/INCONSISTENT) only applies
    when the two results are of the SAME target or within `region_radius_deg`
    of each other on sky - default None means "derive from the beam": the
    larger of the two beam_fwhm_deg values (two pointings within one beam
    width are, physically, probing the same patch of sky), falling back to
    DEFAULT_REGION_RADIUS_DEG only if neither result carries a beam size."""
    warnings: List[str] = []
    same_target = result_a.target_label == result_b.target_label
    separation_deg = float(result_a.sky_position.separation(result_b.sky_position).deg)

    if region_radius_deg is None:
        beams = [b for b in (result_a.beam_fwhm_deg, result_b.beam_fwhm_deg) if b]
        region_radius_deg = max(beams) if beams else DEFAULT_REGION_RADIUS_DEG
        if not beams:
            warnings.append(f"neither result carries beam_fwhm_deg - falling back to a "
                             f"{DEFAULT_REGION_RADIUS_DEG} deg same-region radius")
    same_region = same_target or separation_deg <= region_radius_deg

    if result_a.reference_id and result_b.reference_id and result_a.reference_id != result_b.reference_id:
        warnings.append(f"different reference used: {result_a.reference_id} vs {result_b.reference_id} "
                         f"- a real offset difference could be a reference artifact, not just the mount")
    if result_a.beam_fwhm_deg and result_b.beam_fwhm_deg and result_a.beam_fwhm_deg != result_b.beam_fwhm_deg:
        warnings.append(f"different beam_fwhm_deg: {result_a.beam_fwhm_deg} vs {result_b.beam_fwhm_deg}")

    delta_east = result_b.offset_east_deg - result_a.offset_east_deg
    delta_north = result_b.offset_north_deg - result_a.offset_north_deg

    if not same_region:
        return ComparisonResult(
            "DIFFERENT_REGION",
            f"{result_a.target_label} ({result_a.session_id}) and {result_b.target_label} "
            f"({result_b.session_id}) are {separation_deg:.2f} deg apart, beyond the "
            f"{region_radius_deg:.2f} deg same-region radius - a difference here is a POINTING-MODEL "
            f"diagnostic (polar misalignment, flexure, non-orthogonality), not a repeatability failure",
            same_target, same_region, delta_east, delta_north, None, None, separation_deg, warnings)

    have_sigma_east = result_a.uncertainty_east_deg is not None and result_b.uncertainty_east_deg is not None
    have_sigma_north = result_a.uncertainty_north_deg is not None and result_b.uncertainty_north_deg is not None
    if not (have_sigma_east and have_sigma_north):
        return ComparisonResult(
            "UNKNOWN", "same region/target, but at least one result is missing an uncertainty estimate "
                       "(no stable bootstrap and no subgrid-refinement uncertainty) - refusing to guess a "
                       "verdict; report the raw offsets to the operator instead",
            same_target, same_region, delta_east, delta_north, None, None, separation_deg, warnings)

    sigma_east = (result_a.uncertainty_east_deg ** 2 + result_b.uncertainty_east_deg ** 2) ** 0.5 or 1e-9
    sigma_north = (result_a.uncertainty_north_deg ** 2 + result_b.uncertainty_north_deg ** 2) ** 0.5 or 1e-9
    d_east = abs(delta_east) / sigma_east
    d_north = abs(delta_north) / sigma_north
    worst = max(d_east, d_north)

    if worst <= CONSISTENT_SIGMA:
        verdict = "CONSISTENT"
    elif worst <= INCONSISTENT_SIGMA:
        verdict = "MARGINAL"
    else:
        verdict = "INCONSISTENT"
    reason = (f"East differs by {d_east:.2f} sigma, North by {d_north:.2f} sigma "
              f"(bands: CONSISTENT<={CONSISTENT_SIGMA:.1f}, MARGINAL<={INCONSISTENT_SIGMA:.1f}, "
              f"else INCONSISTENT)")
    return ComparisonResult(verdict, reason, same_target, same_region, delta_east, delta_north,
                             sigma_east, sigma_north, separation_deg, warnings)
