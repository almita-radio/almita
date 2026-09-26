#!/usr/bin/env python3
"""ALMITA SCIENCE WEB BRIDGE - LEVEL 1 (REDUCE) -> three comparable heatmaps for the console.

Reuses science_engine (frozen) for every real scientific computation - ingest/contract, beam+grid
construction, beam-weighted cube gridding, the window-overlap integrated-intensity formula, velocity-axis
resampling, quality assessment, and the REDUCE-manifest-hash provenance primitive. Nothing here
re-derives a Doppler correction, re-applies the 50-ohm relative profile, or invents a physical unit -
every array stays "relative_intensity_dimensionless" (or that times m/s for the integrated map), exactly
as science_engine.integration's own docstring defines it.

What this file adds - real gaps science_engine's own top-level run_science_session() does not cover,
never a duplicate of its math:
  1. An explicit, mandatory calibration-level split (RELATIVE vs UNCALIBRATED): run_science_session()
     grids whatever load_science_input() returns, mixed levels included. This module filters the
     ScienceInput to ONE calibration level before any gridding, so a "RELATIVE" map is only ever built
     from RELATIVE points and an "UNCALIBRATED" (instrumental) map only from UNCALIBRATED ones - never
     both on the same color scale.
  2. THREE comparable products from one filtered input and one real beam:
       A  no spatial interpolation at all - each USED point's own window-integrated value, plotted as a
          filled footprint (radius = beam_fwhm_deg/2) at its own tangent-plane position. Reuses
          science_engine.integration.window_overlaps/bin_bounds and science_engine.resample directly -
          the identical bin-overlap integration formula integrated_map() uses, just evaluated per point
          instead of per output pixel.
       B  science_engine.cube.build_cube() + science_engine.integration.integrated_map(), a real,
          conservative beam_cutoff_n_fwhm (points beyond this many beam-widths never contribute).
       C  the SAME build_cube()+integrated_map() call again, SAME beam FWHM (the real, reported
          instrument beam never changes between B and C), a LARGER beam_cutoff_n_fwhm - visibly more
          smoothed because contributions reach further, a real declared, reproducible parameter change,
          never a palette trick.
     A/B/C share one ScienceGrid (grid geometry depends only on beam.fwhm_deg and pixel scale - never
     on cutoff - see science_engine.grid.build_grid) and one color scale (robust 2nd/98th percentile of
     map A's own measured per-point values - "measured points", not a smoothed derivative of them).
  3. Presentation-ready combined exports (PNG at slide resolution, SVG, PDF) with the title, calibration
     label, beam/cutoff/window parameters, color limits and any caveat baked into the image itself - not
     only shown in the HTML.
  4. Its own manifest/provenance/config under a NEW directory, data/science/<campaign_id>/
     SCIENCE_WEB-<timestamp>/ - never a science_engine.storage.ScienceSession (a different, additive
     location; nothing under data/reduced, data/calibration or an existing data/science/.../SCIENCE-*
     session is ever opened for writing).

100% offline: reads only an already-completed REDUCE session directory. No hardware, no mount, no SDR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

SCHEMA_VERSION = "1.0"
PIPELINE_VERSION = "science-web-v1.0"
CALIBRATION_LEVELS = ("RELATIVE", "UNCALIBRATED")


def _print(payload: dict, as_json: bool, human_lines) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in human_lines:
            print(line)


# ------------------------------------------------------------------ config
@dataclass(frozen=True)
class MapConfig:
    reduce_session_dir: str
    calibration_level_filter: str                  # "RELATIVE" | "UNCALIBRATED" - never "ALL" (section: no mixed scale)
    beam_fwhm_deg: float
    beam_source: str
    beam_status: str
    beam_source_path: Optional[str] = None
    beam_source_field: Optional[str] = None
    beam_source_sha256: Optional[str] = None
    pixel_scale_deg: Optional[float] = None
    pixels_per_beam: float = 4.0
    extent_margin_beams: float = 1.5
    beam_cutoff_b_n_fwhm: float = 2.0               # map B: conservative smoothing
    beam_cutoff_c_n_fwhm: float = 5.0                # map C: heavier smoothing - same beam, wider reach only
    quality_policy: str = "STANDARD"
    velocity_window_min_m_s: float = -300_000.0
    velocity_window_max_m_s: float = 300_000.0
    min_spectral_coverage_fraction: float = 0.5
    uncertainty_floor_relative: float = 1e-6
    sigma_local_floor_fraction: float = 0.1
    sigma_local_window_channels: int = 129
    color_vmin: Optional[float] = None              # None -> robust auto from map A's measured points
    color_vmax: Optional[float] = None
    footprint_radius_deg: Optional[float] = None     # None -> beam_fwhm_deg / 2

    def __post_init__(self):
        if self.calibration_level_filter not in CALIBRATION_LEVELS:
            raise ValueError(f"calibration_level_filter must be one of {CALIBRATION_LEVELS}, "
                             f"got {self.calibration_level_filter!r} - a map is never built from a mix")
        if self.beam_cutoff_c_n_fwhm <= self.beam_cutoff_b_n_fwhm:
            raise ValueError("beam_cutoff_c_n_fwhm must be > beam_cutoff_b_n_fwhm (C is the MORE smoothed map)")
        if (self.color_vmin is None) != (self.color_vmax is None):
            raise ValueError("color_vmin/color_vmax must both be set or both left auto")
        if self.color_vmin is not None and not (np.isfinite(self.color_vmin) and np.isfinite(self.color_vmax)
                                                 and self.color_vmin < self.color_vmax):
            raise ValueError("color_vmin must be finite and < color_vmax")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _science_config_for(cfg: MapConfig, cutoff_n_fwhm: float):
    from science_engine.config import ScienceConfig
    return ScienceConfig(
        reduce_session_dir=cfg.reduce_session_dir, pixel_scale_deg=cfg.pixel_scale_deg,
        pixels_per_beam=cfg.pixels_per_beam, extent_margin_beams=cfg.extent_margin_beams,
        beam_fwhm_deg=cfg.beam_fwhm_deg, beam_source=cfg.beam_source, beam_status=cfg.beam_status,
        beam_source_path=cfg.beam_source_path, beam_source_field=cfg.beam_source_field,
        beam_source_sha256=cfg.beam_source_sha256, beam_cutoff_n_fwhm=cutoff_n_fwhm,
        quality_policy=cfg.quality_policy, velocity_window_min_m_s=cfg.velocity_window_min_m_s,
        velocity_window_max_m_s=cfg.velocity_window_max_m_s,
        min_spectral_coverage_fraction=cfg.min_spectral_coverage_fraction,
        uncertainty_floor_relative=cfg.uncertainty_floor_relative,
        sigma_local_floor_fraction=cfg.sigma_local_floor_fraction,
        sigma_local_window_channels=cfg.sigma_local_window_channels,
    )


# ------------------------------------------------------------------ ingest + calibration-level filter
def inspect_reduce_session(reduce_session_dir: str) -> dict[str, Any]:
    """Read-only: real coverage (planned/measured/reduced/valid/excluded), calibration-level counts,
    per-point LSRK availability - BEFORE any science_engine contract gate, so a caller sees exactly why
    a point is missing rather than a bare contract failure."""
    from reduce_engine.science_contract import validate_science_input
    session_dir = Path(reduce_session_dir)
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"not a REDUCE session (no manifest.json): {session_dir}")
    manifest = json.loads(manifest_path.read_text())
    contract = validate_science_input(session_dir)
    out: dict[str, Any] = {
        "reduce_session_dir": str(session_dir), "campaign_id": manifest.get("campaign_id"),
        "reduce_session_id": manifest.get("reduce_session_id"), "reduce_status": manifest.get("status"),
        "points_planned": manifest.get("points_discovered"), "points_reduced_completed": manifest.get("points_completed"),
        "points_reduced_blocked": manifest.get("points_blocked"), "points_reduced_failed": manifest.get("points_failed"),
        "science_contract_ok": contract.ok, "science_contract_problems": contract.problems,
    }
    if not contract.ok:
        out.update(calibration_level_counts={}, velocity_available_count=0, velocity_missing_points=[],
                   n_points_ingestable=0, ra_deg_range=None, dec_deg_range=None)
        return out
    from science_engine.ingest import ScienceContractError, load_science_input
    try:
        si = load_science_input(session_dir)
    except ScienceContractError as exc:
        # validate_science_input() (the lighter, real-file-existence contract check above) passed, but
        # load_science_input()'s own additional ingest-time checks (schema version, zero COMPLETED points
        # ingestable, a manifest-wide identity problem) do not - a real, distinct failure mode, not a
        # server error. Reported the same honest way as the lighter check, never a 500.
        out.update(science_contract_ok=False, science_contract_problems=out["science_contract_problems"] + [str(exc)],
                   calibration_level_counts={}, velocity_available_count=0, velocity_missing_points=[],
                   n_points_ingestable=0, ra_deg_range=None, dec_deg_range=None)
        return out
    calib_counts: dict[str, int] = {}
    missing_vel = []
    for p in si.points:
        calib_counts[p.calibration_level] = calib_counts.get(p.calibration_level, 0) + 1
        if p.velocity_lsrk_m_s is None:
            missing_vel.append(p.point_index)
    ra = [p.ra_deg for p in si.points]
    dec = [p.dec_degrees for p in si.points]
    out.update(
        calibration_level_counts=calib_counts, n_points_ingestable=len(si.points),
        velocity_available_count=len(si.points) - len(missing_vel), velocity_missing_points=missing_vel,
        ra_deg_range=[min(ra), max(ra)] if ra else None, dec_deg_range=[min(dec), max(dec)] if dec else None,
        ingest_exclusions=si.exclusions, n_points_in_reduce_manifest=si.n_points_in_manifest,
    )
    return out


def load_filtered_input(reduce_session_dir: str, calibration_level_filter: str):
    """load_science_input() (frozen, reused) then a NEW, additive filter to one calibration level - a
    real, separate ScienceInput copy, never mutating the frozen ingest result. Filtered-out points are
    recorded as exclusions with a distinct, honest reason (never silently dropped)."""
    from science_engine.ingest import load_science_input
    from science_engine.models import ScienceInput
    si = load_science_input(reduce_session_dir)
    counts: dict[str, int] = {}
    for p in si.points:
        counts[p.calibration_level] = counts.get(p.calibration_level, 0) + 1
    kept = [p for p in si.points if p.calibration_level == calibration_level_filter]
    newly_excluded = [{"point_index": p.point_index,
                       "reason": f"CALIBRATION_LEVEL_FILTER_KEEPS_ONLY_{calibration_level_filter}"}
                      for p in si.points if p.calibration_level != calibration_level_filter]
    if not kept:
        raise ValueError(f"no {calibration_level_filter} points in this REDUCE session (counts: {counts}) - "
                         f"choose the other calibration level")
    filtered = ScienceInput(reduce_session_dir=si.reduce_session_dir, campaign_id=si.campaign_id,
                            reduce_session_id=si.reduce_session_id, reduce_schema_version=si.reduce_schema_version,
                            reduce_session_status=si.reduce_session_status, points=kept,
                            contract_problems=list(si.contract_problems),
                            exclusions=list(si.exclusions) + newly_excluded,
                            n_points_in_manifest=si.n_points_in_manifest)
    return filtered, counts, si


# ------------------------------------------------------------------ map A: per-point, no spatial interpolation
def per_point_integrated_values(filtered_input, science_config, velocity_axis: np.ndarray) -> list[dict[str, Any]]:
    """Each USED point's own window-integrated value - the SAME bin-overlap formula
    science_engine.integration.integrated_map() applies per pixel, applied here per point's own
    (canonical-axis-resampled, exactly as build_cube() would) spectrum. No spatial gridding: this is
    what map A plots directly, one footprint per point, never blended with a neighbour."""
    from reduce_engine.models import MaskFlag
    from science_engine.cube import _ascending
    from science_engine.gridding import bin_validity, point_passes_quality_policy, sigma_suspect_bins
    from science_engine.integration import window_overlaps
    from science_engine.resample import resample_to_velocity_axis

    vmin, vmax = science_config.velocity_window_min_m_s, science_config.velocity_window_max_m_s
    channel_width = float(np.median(np.abs(np.diff(velocity_axis))))
    overlap_full = window_overlaps(velocity_axis, vmin, vmax)
    idx = np.flatnonzero(overlap_full > 0)
    ov = overlap_full[idx]
    requested_width = vmax - vmin

    rows: list[dict[str, Any]] = []
    suspect_cache: dict[bytes, np.ndarray] = {}
    for point in filtered_input.points:
        entry: dict[str, Any] = {
            "point_index": point.point_index, "ra_deg": point.ra_deg, "dec_degrees": point.dec_degrees,
            "calibration_level": point.calibration_level, "reduce_quality_state": point.reduce_quality_state,
            "timestamp_start_utc": point.timestamp_start_utc,
        }
        if point.velocity_lsrk_m_s is None:
            entry.update(status="EXCLUDED", reason="NO_VELOCITY", value=None, uncertainty=None, spectral_coverage=None)
            rows.append(entry)
            continue
        if not point_passes_quality_policy(point.reduce_quality_state, science_config.quality_policy):
            entry.update(status="EXCLUDED",
                        reason=f"QUALITY_POLICY_{science_config.quality_policy}_EXCLUDES_{point.reduce_quality_state}",
                        value=None, uncertainty=None, spectral_coverage=None)
            rows.append(entry)
            continue

        v_pt, values_pt, unc_pt, mask_pt = _ascending(point.velocity_lsrk_m_s, point.relative_intensity,
                                                       point.uncertainty, point.mask)
        key = hashlib.blake2b(unc_pt.tobytes() + np.asarray(mask_pt).tobytes(), digest_size=16).digest()
        if key not in suspect_cache:
            suspect_cache[key] = sigma_suspect_bins(mask_pt, unc_pt, science_config.sigma_local_floor_fraction,
                                                    science_config.sigma_local_window_channels)
        suspect = suspect_cache[key]
        if suspect.any():
            mask_pt = np.where(suspect, MaskFlag.INVALID.value, mask_pt)

        if float(np.max(np.abs(v_pt - velocity_axis))) <= 1e-3 * channel_width:
            values, uncertainty, mask = values_pt, unc_pt, mask_pt
        else:
            r = resample_to_velocity_axis(v_pt, values_pt, unc_pt, mask_pt, velocity_axis)
            values, uncertainty, mask = r.relative_intensity, r.uncertainty, r.mask

        valid = bin_validity(mask, uncertainty, science_config.uncertainty_floor_relative, values)
        valid_i = valid[idx]
        val_i = np.where(valid_i, values[idx], 0.0)
        unc_i = np.where(valid_i, uncertainty[idx], 0.0)
        value = float(np.sum(ov * val_i))
        sigma = float(np.sqrt(np.sum((ov * unc_i) ** 2)))
        valid_width = float(np.sum(np.where(valid_i, ov, 0.0)))
        coverage = valid_width / requested_width if requested_width > 0 else 0.0
        is_valid = coverage >= science_config.min_spectral_coverage_fraction
        entry.update(status=("USED" if is_valid else "EXCLUDED"),
                    reason=(None if is_valid else f"SPECTRAL_COVERAGE_BELOW_THRESHOLD:{coverage:.3f}"),
                    value=(value if is_valid else None), uncertainty=(sigma if is_valid else None),
                    spectral_coverage=coverage)
        rows.append(entry)
    return rows


def robust_color_limits(point_rows: list[dict[str, Any]], override_vmin, override_vmax) -> tuple[float, float, str]:
    if override_vmin is not None and override_vmax is not None:
        return float(override_vmin), float(override_vmax), "operator override"
    values = np.array([r["value"] for r in point_rows if r["status"] == "USED" and r["value"] is not None])
    if values.size == 0:
        return -1.0, 1.0, "no usable per-point value - arbitrary [-1, 1] fallback (map will show no data anyway)"
    if values.size < 5:
        lo, hi = float(values.min()), float(values.max())
        if lo == hi:
            lo, hi = lo - 1.0, hi + 1.0
        return lo, hi, f"min/max of {values.size} measured point(s) - too few for a percentile"
    lo, hi = (float(v) for v in np.percentile(values, [2, 98]))
    if lo == hi:
        lo, hi = float(values.min()), float(values.max())
    return lo, hi, "2nd/98th percentile of measured (map A) point values"


# ------------------------------------------------------------------ B/C: the frozen beam-gridding, called twice
def memory_check(grid, n_velocity_channels: int, n_points: int) -> dict[str, Any]:
    """Reuses science_engine.validation's own measured (not guessed) per-voxel byte budget and
    MemAvailable read - the SAME real check run_science_session() itself runs before allocating. This
    bridge builds TWO full (Nv,Ny,Nx) cubes in sequence (never simultaneously - see build_all_products()),
    so ONE cube's estimate is the real peak, not two; still a genuine blocking gate, not a guess - a
    5.9 GB cube on this Pi's real RAM was measured to SIGKILL (-9) the process before this check existed."""
    from science_engine.validation import (MEMORY_FRACTION_OF_AVAILABLE, available_memory_bytes,
                                           estimate_peak_memory_bytes, n_voxels)
    peak = estimate_peak_memory_bytes(grid, n_velocity_channels, n_points)
    available = available_memory_bytes()
    cap = 3 * 1024 ** 3
    budget = cap if available is None else min(cap, int(MEMORY_FRACTION_OF_AVAILABLE * available))
    return {"name": "memory_estimate_within_budget", "ok": peak <= budget,
           "detail": f"estimated peak RAM ~{peak / 1024**2:.1f} MB for one cube {grid.ny}x{grid.nx} x "
                     f"{n_velocity_channels} channels ({n_voxels(grid, n_velocity_channels):,} voxels); "
                     f"budget {budget / 1024**2:.1f} MB (a SECOND cube is built only after the first is freed - "
                     f"see build_all_products())"}


def build_all_products(filtered_input, cfg: MapConfig) -> dict[str, Any]:
    import gc

    from science_engine.cube import build_cube, canonical_velocity_axis
    from science_engine.grid import build_beam_model, build_grid
    from science_engine.integration import integrated_map
    from science_engine.quality import assess_science_quality

    sc_b = _science_config_for(cfg, cfg.beam_cutoff_b_n_fwhm)
    sc_c = _science_config_for(cfg, cfg.beam_cutoff_c_n_fwhm)
    beam_b = build_beam_model(sc_b)
    beam_c = build_beam_model(sc_c)   # same fwhm/source/status as beam_b - only cutoff differs
    grid = build_grid(filtered_input, beam_b, sc_b)   # grid geometry depends on fwhm, never on cutoff - shared with C
    velocity_axis = canonical_velocity_axis(filtered_input)

    mem = memory_check(grid, int(velocity_axis.shape[0]), len(filtered_input.points))
    if not mem["ok"]:
        raise ValueError(f"BLOCKED before allocating: {mem['detail']}")

    # B and C are each a full (Nv, Ny, Nx) cube - measured to OOM-kill this Pi (exit -9) when both were held
    # in memory at once (a real bug found and fixed during this task's own verification: science_engine's
    # own preflight only ever budgets for ONE cube, since run_science_session() itself only ever builds one).
    # Each cube is reduced to its 2D integrated map (and the small facts this module needs) and freed before
    # the next is built - one cube in memory at a time, never two.
    cube_b = build_cube(filtered_input, grid, beam_b, sc_b)
    map_b = integrated_map(cube_b, sc_b)
    quality_b = assess_science_quality(filtered_input, cube_b, sc_b, map_b)
    used_b = set(cube_b.build_info["used_point_indices"])
    del cube_b
    gc.collect()

    cube_c = build_cube(filtered_input, grid, beam_c, sc_c)
    map_c = integrated_map(cube_c, sc_c)
    used_c = set(cube_c.build_info["used_point_indices"])
    del cube_c
    gc.collect()

    used_common = used_b & used_c
    if used_b != used_c:
        # A wider cutoff can only ever ADD support, never remove it - if this ever fires, treat the
        # intersection as authoritative and say so plainly, rather than let A/B/C silently disagree.
        note = (f"beam-support point set differs between cutoff_b ({sorted(used_b)}) and cutoff_c "
               f"({sorted(used_c)}) - using the intersection ({sorted(used_common)}) for all three maps")
    else:
        note = None

    point_rows = per_point_integrated_values(filtered_input, sc_b, velocity_axis)
    for row in point_rows:
        if row["status"] == "USED" and row["point_index"] not in used_common:
            row["status"] = "EXCLUDED_FOR_MAP_COMPARABILITY"
            row["reason"] = "outside the shared beam-support point set used for maps A/B/C (see used_point_set_note)"

    vmin, vmax, vmin_vmax_basis = robust_color_limits(point_rows, cfg.color_vmin, cfg.color_vmax)

    return {
        "sc_b": sc_b, "sc_c": sc_c, "beam_b": beam_b, "beam_c": beam_c, "grid": grid,
        "velocity_axis": velocity_axis, "map_b": map_b, "map_c": map_c,
        "quality_b": quality_b, "used_point_set": sorted(used_common), "used_point_set_note": note,
        "point_rows": point_rows, "color_vmin": vmin, "color_vmax": vmax, "color_limits_basis": vmin_vmax_basis,
    }


# ------------------------------------------------------------------ rendering (matplotlib, lazy import)
def _title_block(cfg: MapConfig, campaign_id: str, reduce_session_id: str, panel: str) -> str:
    vmin_kms, vmax_kms = cfg.velocity_window_min_m_s / 1000.0, cfg.velocity_window_max_m_s / 1000.0
    calib_label = "RELATIVE (calibrated, 50Ω profile applied by REDUCE)" if cfg.calibration_level_filter == "RELATIVE" \
        else "UNCALIBRATED (instrumental - no relative profile applied)"
    return (f"{campaign_id} / {reduce_session_id}  —  {panel}\n"
           f"integrated relative intensity, LSRK window [{vmin_kms:.1f}, {vmax_kms:.1f}] km/s  —  {calib_label}\n"
           f"units: relative_intensity_dimensionless × m/s (fractional excess — no Kelvin/Jy/N_HI claimed)")


def render_all_maps(built: dict[str, Any], cfg: MapConfig, campaign_id: str, reduce_session_id: str,
                    out_dir: Path) -> dict[str, list[str]]:
    """Renders A (footprints), B, C (imshow, shared color scale, measured locations overlaid), plus an
    uncertainty/SNR map from B's own propagated sigma. Every export (PNG/SVG/PDF) carries the same
    title/units/caveats baked in - not only shown in the browser.

    Layout: the title uses fig.suptitle() (spans the FULL figure width, never clipped by the narrower
    axes a colorbar leaves behind - ax.set_title() was measured to clip its left edge on a real 3-line
    title) and every figure reserves FIXED top/bottom margins via subplots_adjust() (never
    tight_layout(), which does not know about a separately-placed fig.text() caption and was measured
    overlapping the caption with the x-axis label on a real render)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle

    from science_engine.spatial import tangent_plane_offsets_deg

    grid = built["grid"]
    vmin, vmax = built["color_vmin"], built["color_vmax"]
    cmap = plt.get_cmap("viridis").with_extremes(bad=(0, 0, 0, 0))
    half_w, half_h = grid.width_deg / 2, grid.height_deg / 2
    used_rows = [r for r in built["point_rows"] if r["status"] == "USED"]
    xs = [tangent_plane_offsets_deg(r["ra_deg"], r["dec_degrees"], grid.center_ra_deg, grid.center_dec_deg)[0]
         for r in used_rows]
    ys = [tangent_plane_offsets_deg(r["ra_deg"], r["dec_degrees"], grid.center_ra_deg, grid.center_dec_deg)[1]
         for r in used_rows]
    footprint_r = cfg.footprint_radius_deg if cfg.footprint_radius_deg is not None else cfg.beam_fwhm_deg / 2.0

    written: dict[str, list[str]] = {}

    def _finish(fig, ax, title: str, name: str, extra_caption: str, exts=("png", "svg", "pdf")) -> None:
        ax.set_xlabel(f"RA offset (deg, cos(dec)-scaled) from center RA={grid.center_ra_deg:.4f}°", fontsize=8)
        ax.set_ylabel(f"Dec offset (deg) from center Dec={grid.center_dec_deg:.4f}°", fontsize=8)
        ax.set_xlim(half_w, -half_w)   # RA increases to the LEFT, conventional sky orientation
        ax.set_ylim(-half_h, half_h)
        ax.set_aspect("equal")
        fig.suptitle(title, fontsize=8.5, y=0.985)
        fig.text(0.5, 0.01, extra_caption, ha="center", va="bottom", fontsize=6.5, wrap=True)
        fig.subplots_adjust(top=0.82, bottom=0.20, left=0.11, right=0.99)
        paths = []
        for ext in exts:
            p = out_dir / f"{name}.{ext}"
            # bbox_inches="tight": recomputes the saved canvas from every artist's ACTUAL rendered extent
            # (suptitle, caption, colorbar label included) - fixed margins alone were measured to still clip
            # a long colorbar label on the right edge; this is robust to title/label length instead of
            # requiring a new hand-tuned margin per plot.
            fig.savefig(p, dpi=200 if ext == "png" else None, bbox_inches="tight", pad_inches=0.15)
            paths.append(str(p.name))
        plt.close(fig)
        written[name] = paths

    # ---- A: no interpolation - footprints at measured positions only ----
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    for r, x, y in zip(used_rows, xs, ys):
        ax.add_patch(Circle((x, y), footprint_r, facecolor=cmap((r["value"] - vmin) / max(vmax - vmin, 1e-30)),
                            edgecolor="black", linewidth=0.3))
    if used_rows:
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(vmin, vmax))
        sm.set_array([])
        cbar = fig.colorbar(sm, ax=ax)
        cbar.set_label("integrated relative_intensity_dimensionless × m/s")
    _finish(fig, ax, _title_block(cfg, campaign_id, reduce_session_id, "A: NO SPATIAL INTERPOLATION (measured footprints)"),
           "map_a_no_interp",
           f"{len(used_rows)} measured point(s) shown as their own footprint (radius={footprint_r:.3f} deg); "
           f"unmeasured sky left transparent — no value is invented between points.")

    # ---- B / C: beam-gridded, shared scale, measured locations overlaid ----
    for key, label, sc, cutoff in (("map_b_smooth", "B: SMOOTH INTERPOLATION", built["sc_b"], cfg.beam_cutoff_b_n_fwhm),
                                   ("map_c_heavy", "C: HEAVIER INTERPOLATION", built["sc_c"], cfg.beam_cutoff_c_n_fwhm)):
        science_map = built["map_b"] if key == "map_b_smooth" else built["map_c"]
        masked = np.ma.masked_where(~science_map.valid, science_map.value)
        fig, ax = plt.subplots(figsize=(7.2, 6.0))
        im = ax.imshow(masked, origin="lower", cmap=cmap, vmin=vmin, vmax=vmax,
                       extent=(half_w, -half_w, -half_h, half_h))
        ax.scatter(xs, ys, s=10, facecolor="none", edgecolor="white", linewidth=0.6, label="measured point")
        cbar = fig.colorbar(im, ax=ax)
        cbar.set_label("integrated relative_intensity_dimensionless × m/s")
        _finish(fig, ax, _title_block(cfg, campaign_id, reduce_session_id, label), key,
               f"method: Gaussian beam-weighted gridding (science_engine.gridding), beam FWHM="
               f"{cfg.beam_fwhm_deg:.4g} deg ({sc.beam_status}), cutoff={cutoff:.2g}×FWHM — pixels beyond "
               f"that many beam-widths from every point get zero weight. White circles = real measured "
               f"positions. Color limits: [{vmin:.4g}, {vmax:.4g}] ({built['color_limits_basis']}).")

    # ---- uncertainty / SNR (from map B's own propagated sigma - the smooth map's real uncertainty) ----
    unc = built["map_b"].uncertainty
    if unc is not None and np.any(np.isfinite(unc)):
        with np.errstate(divide="ignore", invalid="ignore"):
            snr = np.abs(built["map_b"].value) / unc
        masked_snr = np.ma.masked_where(~built["map_b"].valid, snr)
        fig, ax = plt.subplots(figsize=(7.2, 6.0))
        im = ax.imshow(masked_snr, origin="lower", cmap=plt.get_cmap("magma").with_extremes(bad=(0, 0, 0, 0)),
                       extent=(half_w, -half_w, -half_h, half_h))
        cbar = fig.colorbar(im, ax=ax)
        cbar.set_label("SNR (dimensionless)")
        _finish(fig, ax, _title_block(cfg, campaign_id, reduce_session_id, "SNR = |integrated value| / propagated 1σ (map B)"),
               "map_snr",
               "1-sigma uncertainty propagated from REDUCE per-channel sigma assuming independent channels "
               "(see science_engine.integration docstring) - a statistical, not systematic, error bar.",
               exts=("png", "svg"))
        quality_kind = "uncertainty/SNR (propagated statistical 1-sigma)"
    else:
        quality_kind = "no propagable uncertainty available"

    # ---- coverage: points valid vs excluded, spatial ----
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    for r in built["point_rows"]:
        x, y = tangent_plane_offsets_deg(r["ra_deg"], r["dec_degrees"], grid.center_ra_deg, grid.center_dec_deg)
        color = {"USED": "tab:green"}.get(r["status"], "tab:red")
        ax.scatter([x], [y], c=color, s=24, edgecolor="black", linewidth=0.3)
    ax.scatter([], [], c="tab:green", label="used")
    ax.scatter([], [], c="tab:red", label="excluded")
    ax.legend(loc="upper right", fontsize=7)
    _finish(fig, ax, f"{campaign_id} / {reduce_session_id} — coverage: measured points used vs excluded",
           "map_coverage",
           "green = used in maps A/B/C; red = excluded (see points.csv/manifest for each point's real reason).",
           exts=("png", "svg"))
    return written, quality_kind


# ------------------------------------------------------------------ persistence (this bridge's OWN, non-frozen)
def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    import os
    os.replace(tmp, path)


def _new_session_id() -> str:
    return f"SCIENCE_WEB-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}"


def cmd_inspect(args) -> int:
    payload = inspect_reduce_session(args.reduce_session_dir)
    _print(payload, args.json, [
        f"campaign: {payload['campaign_id']}  reduce_status: {payload['reduce_status']}",
        f"points planned/completed/blocked/failed: {payload['points_planned']}/{payload['points_reduced_completed']}/"
        f"{payload['points_reduced_blocked']}/{payload['points_reduced_failed']}",
        f"science_contract_ok: {payload['science_contract_ok']}",
        *([f"  problem: {p}" for p in payload['science_contract_problems']]),
        f"calibration_level_counts: {payload.get('calibration_level_counts')}",
        f"velocity available: {payload.get('velocity_available_count')}/{payload.get('n_points_ingestable')}  "
        f"missing: {payload.get('velocity_missing_points')}",
    ])
    return 0 if payload["science_contract_ok"] else 1


def cmd_plan(args) -> int:
    cfg = _config_from_args(args)
    filtered, counts, _ = load_filtered_input(cfg.reduce_session_dir, cfg.calibration_level_filter)
    from science_engine.cube import canonical_velocity_axis
    from science_engine.grid import build_beam_model, build_grid
    beam = build_beam_model(_science_config_for(cfg, cfg.beam_cutoff_b_n_fwhm))
    grid = build_grid(filtered, beam, _science_config_for(cfg, cfg.beam_cutoff_b_n_fwhm))
    velocity_axis = canonical_velocity_axis(filtered)
    from science_engine.integration import window_overlaps
    overlap = window_overlaps(velocity_axis, cfg.velocity_window_min_m_s, cfg.velocity_window_max_m_s)
    window_blocked = not np.any(overlap > 0)
    checks = [
        {"name": "has_filtered_points", "ok": len(filtered.points) > 0,
        "detail": f"{len(filtered.points)} {cfg.calibration_level_filter} point(s) of {sum(counts.values())} total ingestable"},
        {"name": "velocity_window_overlaps_axis", "ok": not window_blocked,
        "detail": f"requested [{cfg.velocity_window_min_m_s:.0f}, {cfg.velocity_window_max_m_s:.0f}] m/s vs cube "
                  f"[{velocity_axis.min():.0f}, {velocity_axis.max():.0f}] m/s"},
        {"name": "grid_buildable", "ok": True, "detail": f"{grid.ny}x{grid.nx} pixels, {grid.pixel_scale_deg:.4g} deg/pixel"},
        memory_check(grid, int(velocity_axis.shape[0]), len(filtered.points)),
    ]
    blocked = any(not c["ok"] for c in checks)
    payload = {
        "config": cfg.to_dict(), "config_hash": cfg.config_hash(), "calibration_level_counts": counts,
        "grid": grid.to_dict(), "beam": beam.to_dict(), "n_velocity_channels": int(velocity_axis.shape[0]),
        "checks": checks, "blocked": blocked,
        "will_process_points": [p.point_index for p in filtered.points],
    }
    _print(payload, args.json, [
        f"calibration_level_filter: {cfg.calibration_level_filter}  counts: {counts}",
        f"grid: {grid.ny}x{grid.nx} px  beam fwhm={beam.fwhm_deg} deg ({beam.status})",
        f"will process {len(filtered.points)} point(s): {payload['will_process_points']}",
        *[f"[{'PASS' if c['ok'] else 'BLOCKED'}] {c['name']}: {c['detail']}" for c in checks],
    ])
    return 1 if blocked else 0


def cmd_run(args) -> int:
    from science_engine.provenance import reduce_manifest_hash
    cfg = _config_from_args(args)
    t0 = datetime.now(timezone.utc)
    filtered, counts, unfiltered = load_filtered_input(cfg.reduce_session_dir, cfg.calibration_level_filter)
    built = build_all_products(filtered, cfg)

    root = Path(args.output_root)
    session_id = _new_session_id()
    out_dir = root / filtered.campaign_id / session_id
    out_dir.mkdir(parents=True, exist_ok=False)   # fail closed on collision, same discipline as REDUCE/SCIENCE
    maps_dir = out_dir / "maps"
    maps_dir.mkdir()

    exports, unc_kind = render_all_maps(built, cfg, filtered.campaign_id, filtered.reduce_session_id, maps_dir)

    # tabular per-point export for reproducibility (section: "exporta valores tabulares de puntos")
    import csv
    points_csv = out_dir / "points.csv"
    with points_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["point_index", "ra_deg", "dec_degrees", "calibration_level", "reduce_quality_state",
                   "status", "reason", "integrated_value", "integrated_uncertainty", "spectral_coverage",
                   "timestamp_start_utc"])
        for r in built["point_rows"]:
            w.writerow([r["point_index"], r["ra_deg"], r["dec_degrees"], r["calibration_level"],
                       r["reduce_quality_state"], r["status"], r["reason"], r["value"], r["uncertainty"],
                       r["spectral_coverage"], r["timestamp_start_utc"]])

    # arrays for the web viewer (JSON - small grids; the browser never needs the HDF5 to render)
    def _map_json(name: str, science_map) -> None:
        payload = {
            "kind": science_map.kind, "units": science_map.units, "grid": science_map.grid.to_dict(),
            "value": np.where(science_map.valid, science_map.value, None).tolist(),
            "valid": science_map.valid.tolist(),
            "uncertainty": (np.where(science_map.valid, science_map.uncertainty, None).tolist()
                           if science_map.uncertainty is not None else None),
            "metadata": science_map.metadata,
        }
        _atomic_write_json(maps_dir / f"{name}.json", payload)

    _map_json("map_b_smooth", built["map_b"])
    _map_json("map_c_heavy", built["map_c"])
    _atomic_write_json(maps_dir / "map_a_no_interp.json", {
        "kind": "no_interpolation_footprints", "units": built["map_b"].units,
        "footprint_radius_deg": cfg.footprint_radius_deg or cfg.beam_fwhm_deg / 2.0,
        "grid_center_ra_deg": built["grid"].center_ra_deg, "grid_center_dec_deg": built["grid"].center_dec_deg,
        "points": built["point_rows"],
    })

    thermal = _thermal_drift_note(unfiltered)
    manifest_hash = reduce_manifest_hash(cfg.reduce_session_dir)
    completeness = "COMPLETE" if unfiltered.reduce_session_status == "COMPLETED" and not unfiltered.exclusions else "PARTIAL"
    manifest = {
        "science_web_schema_version": SCHEMA_VERSION, "science_web_pipeline_version": PIPELINE_VERSION,
        "science_web_session_id": session_id, "campaign_id": filtered.campaign_id,
        "reduce_session_id": filtered.reduce_session_id, "reduce_session_dir": str(cfg.reduce_session_dir),
        "input_reduce_manifest_sha256": manifest_hash, "data_completeness": completeness,
        "calibration_level_filter": cfg.calibration_level_filter, "calibration_level_counts_full_session": counts,
        "config": cfg.to_dict(), "config_hash": cfg.config_hash(),
        "beam_b": built["beam_b"].to_dict(), "beam_c": built["beam_c"].to_dict(), "grid": built["grid"].to_dict(),
        "velocity_window_m_s": [cfg.velocity_window_min_m_s, cfg.velocity_window_max_m_s],
        "n_velocity_channels": int(built["velocity_axis"].shape[0]),
        "color_vmin": built["color_vmin"], "color_vmax": built["color_vmax"],
        "color_limits_basis": built["color_limits_basis"],
        "used_point_set": built["used_point_set"], "used_point_set_note": built["used_point_set_note"],
        "n_points_filtered_in": len(filtered.points), "n_points_used": len(built["used_point_set"]),
        "quality_b": built["quality_b"].to_dict(), "uncertainty_kind": unc_kind,
        "thermal_drift": thermal,
        "exports": exports, "points_csv": "points.csv",
        "started_utc": t0.isoformat(), "ended_utc": datetime.now(timezone.utc).isoformat(),
        "status": "COMPLETED",
    }
    _atomic_write_json(out_dir / "manifest.json", manifest)
    _atomic_write_json(out_dir / "config.json", cfg.to_dict())
    _atomic_write_json(out_dir / "provenance.json", {
        "science_web_pipeline_version": PIPELINE_VERSION, "input_reduce_manifest_sha256": manifest_hash,
        "input_reduce_session_dir": str(cfg.reduce_session_dir), "config_hash": cfg.config_hash(),
        "git_commit": _git_commit(), "started_utc": manifest["started_utc"], "ended_utc": manifest["ended_utc"],
    })
    payload = {"status": "COMPLETED", "output_dir": str(out_dir), "session_id": session_id,
              "campaign_id": filtered.campaign_id, "n_points_used": len(built["used_point_set"]),
              "calibration_level_filter": cfg.calibration_level_filter, "exports": exports}
    _print(payload, args.json, [
        f"SCIENCE WEB COMPLETED", f"Campaign: {filtered.campaign_id}  session: {session_id}",
        f"Calibration level: {cfg.calibration_level_filter}  points used: {len(built['used_point_set'])}",
        f"Output: {out_dir}",
    ])
    return 0


def _thermal_drift_note(science_input) -> dict[str, Any]:
    """Looks for REAL, dated LNA/SDR temperature readings tied to this campaign's own captures
    (environment.json next to each capture, DS18B20 sysfs readings via temperature_sensors.py). Never
    estimates drift from elapsed time and never treats the 50-ohm profile's own HI-ALTO/HI-BAJO contrast
    as a temperature measurement - those are explicitly out of scope per the task."""
    checked = 0
    found = 0
    for p in science_input.points[:20]:   # a representative sample is enough to answer "does this exist at all"
        for ref in p.capture_refs:
            src = ref.get("source_path") or ref.get("capture_id")
            if not src:
                continue
            env_path = Path(src).parent.parent / "environment.json" if src else None
            checked += 1
            try:
                if env_path and env_path.is_file():
                    env = json.loads(env_path.read_text())
                    if env.get("temperature_before") or env.get("temperature_after"):
                        found += 1
            except (OSError, ValueError):
                continue
    if found:
        return {"status": "AVAILABLE", "note": f"{found}/{checked} sampled captures have a non-empty recorded "
                                               "temperature - inspect environment.json per session for values."}
    return {"status": "NOT_EVALUABLE",
           "note": "deriva térmica no evaluable: no real, dated LNA/SDR temperature reading was found for this "
                   "campaign's captures (environment.json's temperature_before/after are empty on this hardware). "
                   "Not estimated from elapsed time; the 50-ohm HI-ALTO/HI-BAJO contrast is a relative spectral "
                   "reference, never a temperature measurement, and is not used as one here."}


def _git_commit() -> Optional[str]:
    import subprocess
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           cwd=Path(__file__).resolve().parent, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _config_from_args(args) -> MapConfig:
    beam_kwargs: dict[str, Any] = {}
    if args.beam_fwhm_deg is not None and args.beam_from_observer_config is not None:
        raise ValueError("give either --beam-fwhm-deg or --beam-from-observer-config, not both")
    if args.beam_fwhm_deg is not None:
        beam_kwargs.update(beam_fwhm_deg=args.beam_fwhm_deg, beam_source="operator_config",
                          beam_status="CONFIGURED_OPERATIONAL")
    elif args.beam_from_observer_config is not None:
        from science_engine.beam import beam_settings_from_observer_config
        beam_kwargs.update(beam_settings_from_observer_config(args.beam_from_observer_config))
    else:
        raise ValueError("no beam specified: pass --beam-fwhm-deg DEG or --beam-from-observer-config [PATH]")
    kwargs: dict[str, Any] = dict(reduce_session_dir=args.reduce_session_dir,
                                  calibration_level_filter=args.calibration_level_filter, **beam_kwargs)
    for cli_name, cfg_name in (
        ("pixel_scale_deg", "pixel_scale_deg"), ("pixels_per_beam", "pixels_per_beam"),
        ("extent_margin_beams", "extent_margin_beams"), ("beam_cutoff_b_n_fwhm", "beam_cutoff_b_n_fwhm"),
        ("beam_cutoff_c_n_fwhm", "beam_cutoff_c_n_fwhm"), ("quality_policy", "quality_policy"),
        ("velocity_window_min_m_s", "velocity_window_min_m_s"), ("velocity_window_max_m_s", "velocity_window_max_m_s"),
        ("min_spectral_coverage_fraction", "min_spectral_coverage_fraction"), ("color_vmin", "color_vmin"),
        ("color_vmax", "color_vmax"), ("footprint_radius_deg", "footprint_radius_deg"),
    ):
        value = getattr(args, cli_name, None)
        if value is not None:
            kwargs[cfg_name] = value
    return MapConfig(**kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def _common_config_args(p):
        p.add_argument("--calibration-level-filter", required=True, choices=CALIBRATION_LEVELS)
        p.add_argument("--beam-fwhm-deg", type=float, default=None)
        p.add_argument("--beam-from-observer-config", nargs="?", const="observer_config.json", default=None)
        p.add_argument("--pixel-scale-deg", type=float, default=None)
        p.add_argument("--pixels-per-beam", type=float, default=None)
        p.add_argument("--extent-margin-beams", type=float, default=None)
        p.add_argument("--beam-cutoff-b-n-fwhm", type=float, default=None)
        p.add_argument("--beam-cutoff-c-n-fwhm", type=float, default=None)
        p.add_argument("--quality-policy", choices=("STRICT", "STANDARD", "PERMISSIVE"), default=None)
        p.add_argument("--velocity-window-min-m-s", type=float, default=None)
        p.add_argument("--velocity-window-max-m-s", type=float, default=None)
        p.add_argument("--min-spectral-coverage-fraction", type=float, default=None)
        p.add_argument("--color-vmin", type=float, default=None)
        p.add_argument("--color-vmax", type=float, default=None)
        p.add_argument("--footprint-radius-deg", type=float, default=None)
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("inspect", help="read-only coverage + calibration-level + velocity summary")
    p.add_argument("reduce_session_dir")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("plan", help="no writes: grid/beam, will-process points, blocking checks")
    p.add_argument("reduce_session_dir")
    _common_config_args(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="build the three heatmaps + coverage/SNR + tabular export")
    p.add_argument("reduce_session_dir")
    p.add_argument("--output-root", default="data/science")
    _common_config_args(p)
    p.set_defaults(func=cmd_run)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as error:  # noqa: BLE001
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
