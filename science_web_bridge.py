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
  2. THREE comparable products with a DELIBERATE, visible resolution progression left to right - map A is
     the real N x M board (build_mosaic_grid(): rows/cols derived from the campaign's own point positions and
     their median spacing, never a hardcoded shape), and maps B/C are genuinely FINER rasters
     (build_fine_grid(): the SAME physical extent/center/orientation as the board, `interp_factor_b`/
     `interp_factor_c` times denser per axis, interp_factor_c > interp_factor_b enforced) - real NEW pixel
     positions between the measured cells, never the same N x M cells enlarged, blurred or repainted at a
     different opacity (an earlier version of this module rendered B/C on the SAME board as A, which made
     them look like a washed-out copy of A at the SAME resolution - corrected here):
       A  no spatial interpolation at all - each USED point's own window-integrated value placed in its OWN
          board cell, one reading = one cell = one color, sharp edges (imshow(interpolation="nearest")).
          Cells with no real point are transparent - never zero, never interpolated. The ONLY panel with a
          per-cell identity a browser click can resolve to one real point's own spectrum. Reuses
          science_engine.integration.window_overlaps/bin_bounds and science_engine.resample directly - the
          identical bin-overlap integration formula integrated_map() uses, just evaluated per point instead
          of per output pixel.
       B  science_engine.cube.build_cube() + science_engine.integration.integrated_map() evaluated on a
          raster `interp_factor_b` times denser than the board - most output pixels sit BETWEEN measured
          positions and are genuine interpolation estimates, weighted by a declared presentation-smoothing
          kernel (NOT the instrument beam). No per-pixel click/spectrum here: a new pixel was never observed
          by the instrument.
       C  the SAME call again on an even denser raster (`interp_factor_c` > `interp_factor_b`), the SAME
          kernel and the SAME hard support radius as B (see _reconcile_used_point_sets) and the SAME physical
          footprint as A and B (no invented sky coverage, no support-radius-driven shape change) - the ONLY
          difference from B is raster density. B and C used to carry two DIFFERENT (light/heavier) kernels,
          which confounded "denser raster" with "more blended" and made the two hard to compare honestly; a
          real investigation into reported circular halos (REDUCE-20260919-234712-712870) found via
          leave-one-out cross-validation (loo_cross_validation_summary()) that the two kernel widths were
          statistically indistinguishable in held-out predictive skill, so there was no data-driven reason to
          keep them different - see MapConfig.smoothing_fwhm_deg's own docstring for the numbers.
     The real, reported instrument beam (beam_fwhm_deg) is unchanged between B and C and recorded honestly
     in the manifest, but is no longer the kernel doing either map's weighting. A/B/C share one color scale
     (robust 2nd/98th percentile of map A's own measured per-point values - "measured points", not a
     smoothed derivative of them) applied through ONE shared colour function (viridis_hex()) used identically
     for every PNG/SVG/PDF export AND the browser's own interactive board.json - never a second, JS-side
     palette. B/C's ON-SCREEN images are additionally rendered chrome-free (`_bare_export` in
     render_all_maps() - no title/colorbar/caption baked in, just the map, cropped tight) so all three panels
     display at the same visible size next to the SAME external HTML colour legend (board_json_payload's
     color_stops) - the full titled/colorbar'd PNG/SVG/PDF exports remain unchanged as downloadable,
     presentation-ready artifacts.
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
PIPELINE_VERSION = "science-web-v1.3"
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
    # The mosaic BOARD: one cell per real pointing lattice position (build_mosaic_grid()) - rows/cols and cell
    # spacing are derived from the campaign's own point positions, never a hardcoded shape. mosaic_spacing_deg
    # overrides the auto-derived (median nearest-neighbour) cell pitch; leave None for the real, measured one.
    mosaic_spacing_deg: Optional[float] = None
    # --- spatial SUPPORT (where it is legitimate to draw a value at all) - ONE radius, shared by B and C.
    # This used to be `beam_cutoff_{b,c}_n_fwhm x beam_fwhm_deg`: two DIFFERENT absolute radii on the SAME
    # (often mis-scaled - see beam_fwhm_deg's own docstring) beam, which is what let B and C disagree on
    # which points they used (a real bug fixed in this task - see _reconcile_used_point_sets()). Support is
    # now a single, physically-grounded number (None -> auto from the campaign's OWN point spacing - see
    # `auto_spatial_params()`), applied identically to B and C so their used-point sets are IDENTICAL BY
    # CONSTRUCTION, never reconciled after the fact.
    support_radius_deg: Optional[float] = None
    # --- presentation SMOOTHING (how much blend is applied for display, WITHIN that fixed support). A
    # declared Gaussian kernel width, independent of the reported instrument beam (beam_fwhm_deg, kept fixed
    # and honestly reported below, never used as a gridding kernel from here on - see build_all_products()).
    # This USED to be two DIFFERENT widths (smoothing_fwhm_b_deg light / smoothing_fwhm_c_deg = 3x heavier),
    # so B and C were never actually "the same field at two densities" - a real investigation into reported
    # circular halos (REDUCE-20260919-234712-712870) found this was masking the real question ("does a denser
    # raster alone introduce new structure?") behind a confound (kernel AND density both changing at once).
    # Leave-one-out cross-validation on that session's own 100 real points (loo_cross_validation_summary())
    # showed the light/heavy/1.5x kernels are statistically INDISTINGUISHABLE in held-out predictive skill
    # (RMS 442.3/442.3/442.3, all far worse than simply predicting the field's own mean - see that function's
    # docstring) - i.e. there was never a real reason for B and C to use different kernels; the two numbers
    # were free parameters with no data-driven justification. ONE kernel now, shared by B and C - the ONLY
    # remaining difference between them is raster density (interp_factor_b/interp_factor_c below), which a
    # matched fixed-kernel control (same session, 30x30 vs 60x60) showed is a genuine, low-distortion
    # refinement of the SAME field (r=0.946 between matched pixels) - unlike the OLD light-vs-heavy B/C pair,
    # which only correlated at r=0.785 because it was comparing two different fields, not two densities.
    smoothing_fwhm_deg: Optional[float] = None     # None -> auto (~ median nearest-neighbour point spacing)
    # Interpolated panels B/C are rendered on a FINER raster than the real N x M board (real pixels BETWEEN
    # the measured positions, not the same cells redrawn bigger/blurrier - see build_fine_grid()). Each factor
    # is how many raster pixels replace one real board cell per axis; C must be denser than B so the visual
    # progression (A: N x M -> B: denser -> C: densest) is unambiguous.
    interp_factor_b: int = 3
    interp_factor_c: int = 6
    quality_policy: str = "STANDARD"
    # Matches science_engine.config.ScienceConfig's own default (+/-100 km/s) - a wider +/-300 km/s window was
    # this bridge's OWN unjustified widening (found during this task's magnitude investigation: it roughly
    # triples the integrated value for no declared reason) and is corrected here, not inherited.
    velocity_window_min_m_s: float = -100_000.0
    velocity_window_max_m_s: float = 100_000.0
    min_spectral_coverage_fraction: float = 0.5
    uncertainty_floor_relative: float = 1e-6
    sigma_local_floor_fraction: float = 0.1
    sigma_local_window_channels: int = 129
    color_vmin: Optional[float] = None              # None -> robust auto from map A's measured points
    color_vmax: Optional[float] = None

    def __post_init__(self):
        if self.calibration_level_filter not in CALIBRATION_LEVELS:
            raise ValueError(f"calibration_level_filter must be one of {CALIBRATION_LEVELS}, "
                             f"got {self.calibration_level_filter!r} - a map is never built from a mix")
        if self.support_radius_deg is not None and not (np.isfinite(self.support_radius_deg) and self.support_radius_deg > 0):
            raise ValueError("support_radius_deg must be finite and > 0")
        if self.smoothing_fwhm_deg is not None and not (np.isfinite(self.smoothing_fwhm_deg) and self.smoothing_fwhm_deg > 0):
            raise ValueError("smoothing_fwhm_deg must be finite and > 0")
        if self.interp_factor_b < 2 or self.interp_factor_c < 2:
            raise ValueError("interp_factor_b/interp_factor_c must each be >= 2 - B and C must be genuinely "
                             "finer rasters than the real board, not the same cells redrawn")
        if self.interp_factor_c <= self.interp_factor_b:
            raise ValueError("interp_factor_c must be > interp_factor_b (C must be visually denser than B)")
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


def _kernel_science_config(cfg: MapConfig, kernel_fwhm_deg: float, cutoff_n_fwhm: float, kernel_label: str):
    """A ScienceConfig whose `beam_fwhm_deg`/`beam_cutoff_n_fwhm` are a DECLARED presentation-smoothing
    kernel - never science_web_bridge's own honestly-reported real instrument beam (cfg.beam_fwhm_deg/
    beam_source/beam_status, recorded separately in the manifest and never fed into build_cube() from here
    on - see build_all_products()). pixel_scale_deg/pixels_per_beam/extent_margin_beams are irrelevant here:
    build_cube() never reads them (only build_grid() does, and the board's grid comes from
    build_mosaic_grid() instead - see build_all_products()); left at ScienceConfig's own inert defaults."""
    from science_engine.config import ScienceConfig
    return ScienceConfig(
        reduce_session_dir=cfg.reduce_session_dir,
        beam_fwhm_deg=kernel_fwhm_deg, beam_source=kernel_label, beam_status="CONFIGURED_OPERATIONAL",
        beam_cutoff_n_fwhm=cutoff_n_fwhm,
        quality_policy=cfg.quality_policy, velocity_window_min_m_s=cfg.velocity_window_min_m_s,
        velocity_window_max_m_s=cfg.velocity_window_max_m_s,
        min_spectral_coverage_fraction=cfg.min_spectral_coverage_fraction,
        uncertainty_floor_relative=cfg.uncertainty_floor_relative,
        sigma_local_floor_fraction=cfg.sigma_local_floor_fraction,
        sigma_local_window_channels=cfg.sigma_local_window_channels,
    )


def _median_nearest_neighbor_spacing_deg(points) -> Optional[float]:
    """Real, measured median nearest-neighbour angular separation among a set of points - the one number
    every spatial default in this module is now tied to (never to beam_fwhm_deg - see auto_spatial_params
    and build_mosaic_grid). None when fewer than 2 points (nothing to space out)."""
    if len(points) < 2:
        return None
    from science_engine.spatial import angular_separation_deg
    ra = np.array([p.ra_deg for p in points])
    dec = np.array([p.dec_degrees for p in points])
    nn = np.empty(len(points))
    for i in range(len(points)):
        d = angular_separation_deg(np.full(len(points), ra[i]), np.full(len(points), dec[i]), ra, dec)
        d[i] = np.inf
        nn[i] = d.min()
    finite = nn[np.isfinite(nn)]
    return float(np.median(finite)) if finite.size else None


def auto_spatial_params(filtered_input, cfg: MapConfig) -> dict[str, float]:
    """Resolves mosaic_spacing_deg / support_radius_deg / smoothing_fwhm_deg when left None, from THIS
    campaign's OWN point spacing (median nearest-neighbour angular separation among the points being mapped)
    - never from beam_fwhm_deg. Using the reported beam FWHM to size these was the root cause a real deployed
    run exposed: science_engine.models.BeamModel's own docstring documents a live "14.0/20.0/1.5 discrepancy"
    across beam-FWHM sources this repo carries, and a real UNCALIBRATED run's beam (20 deg, from
    observer_config.json) was ~20x the real ~1 deg point spacing of its own 36-point mosaic - producing a
    grid whose margin alone (1.5 x 20 deg per side) dwarfed the real ~6x5 deg field and a beam-weighted map
    that barely varied across it. None of that is reproduced here: every one of these numbers is now tied to
    where real measurements actually are.

    ONE smoothing_fwhm_deg, not two: an earlier round of this task gave B and C separate light/heavy kernels,
    which turned out to have no data-driven justification (see MapConfig.smoothing_fwhm_deg's own docstring
    for the leave-one-out cross-validation that found them statistically indistinguishable) and confounded
    "denser raster" with "differently smoothed" in a way that made B and C hard to compare honestly. B and C
    now share this one kernel; only their raster density (interp_factor_b/interp_factor_c) differs.
    """
    nn_median = _median_nearest_neighbor_spacing_deg(filtered_input.points)
    if nn_median is None:
        nn_median = cfg.beam_fwhm_deg   # nothing to space out - no better number available than the beam scale

    mosaic_spacing_deg = cfg.mosaic_spacing_deg if cfg.mosaic_spacing_deg is not None else nn_median
    support_radius_deg = cfg.support_radius_deg if cfg.support_radius_deg is not None else 1.25 * nn_median
    smoothing_fwhm_deg = cfg.smoothing_fwhm_deg if cfg.smoothing_fwhm_deg is not None else nn_median
    return {"nearest_neighbor_spacing_deg": nn_median, "mosaic_spacing_deg": mosaic_spacing_deg,
           "support_radius_deg": support_radius_deg, "smoothing_fwhm_deg": smoothing_fwhm_deg}


def _reconcile_used_point_sets(used_b: set[int], used_c: set[int], support_radius_deg: float) -> set[int]:
    """B and C are built with the SAME support_radius_deg and the SAME physical grid extent/center (see
    build_all_products/build_fine_grid) - only their pixel DENSITY differs. A point within support_radius_deg
    of the shared physical area is within support radius of some pixel in EITHER grid (a finer grid only adds
    pixel centers inside the same area, it never shrinks it), so used_b == used_c is expected by construction,
    not a coincidence to reconcile after the fact (verified in test_science_web_bridge.py on the real 6x6
    mosaic at the shipped interp factors). A mismatch here means that expectation was violated (a real bug,
    e.g. mismatched grid extents reaching build_cube by mistake) - this BLOCKS the comparison outright rather
    than silently reconciling it with an intersection."""
    if used_b != used_c:
        only_b, only_c = sorted(used_b - used_c), sorted(used_c - used_b)
        raise ValueError(
            "BLOCKED: map B and map C used DIFFERENT point sets despite sharing support_radius_deg="
            f"{support_radius_deg:.6g} (should be structurally impossible - see _reconcile_used_point_sets's "
            f"docstring). only in B: {only_b}; only in C: {only_c}. This is a real bug, not a cosmetic "
            "mismatch - the comparison is blocked rather than silently using an intersection."
        )
    return set(used_b)


class MosaicShapeError(ValueError):
    """Raised when the real points do not resolve to a clean rectangular lattice at the derived/declared
    spacing (e.g. two points would land in the same cell) - an honest failure, never a silent mis-placement."""


def _cluster_1d(values: np.ndarray, gap_threshold: float) -> tuple[np.ndarray, int, np.ndarray]:
    """Groups 1D values into clusters by GAP, not by rounding to a fixed multiple of a nominal spacing - a
    real 36-point mosaic's own tangent-plane RA offsets were measured to drift ~6% row-to-row (a real,
    expected effect of a single global cos(dec) tangent-plane projection applied to rows at slightly
    different real declinations, when the original mosaic was laid out in constant RA-hour steps per row -
    not a data error), which made "round(x/spacing)" collide two real points into the same nominal cell on
    a real deployed run. Splitting on a gap > gap_threshold between successive SORTED values is robust to
    that smooth drift as long as within-row/within-column jitter stays well under one spacing (true here:
    the measured drift was a few % of one spacing, gap_threshold defaults to half a spacing in
    build_mosaic_grid). Returns (cluster_index per value, n_clusters, centroid per cluster)."""
    order = np.argsort(values)
    sorted_vals = values[order]
    cluster_sorted = np.zeros(len(values), dtype=int)
    current = 0
    for i in range(1, len(sorted_vals)):
        if sorted_vals[i] - sorted_vals[i - 1] > gap_threshold:
            current += 1
        cluster_sorted[i] = current
    cluster = np.empty(len(values), dtype=int)
    cluster[order] = cluster_sorted
    n_clusters = current + 1
    centroids = np.array([sorted_vals[cluster_sorted == k].mean() for k in range(n_clusters)])
    return cluster, n_clusters, centroids


def build_mosaic_grid(filtered_input, spacing_deg: float):
    """A ScienceGrid whose pixel centers coincide with the campaign's OWN real pointing lattice - rows/cols
    derived from the real point positions and `spacing_deg` (via _cluster_1d's gap-based clustering, robust
    to real per-row/per-column projection drift - see its own docstring), never a hardcoded shape and never
    a finer sub-cell raster. This is what makes A, B and C the SAME NxM board (request: "el mismo tablero de
    6x6 cuadros... idénticos límites, orientación, posición de cada celda, tamaño de celda"): the exact same
    frozen ScienceGrid type build_cube()/integrated_map() already consume, just constructed at the mosaic's
    own native cell scale instead of a fine raster.

    Returns (grid, point_cell, n_rows, n_cols) where point_cell maps point_index -> (row, col), the nearest
    lattice cell to that point's own real position (build_cube() still weights from the point's own exact
    RA/Dec via the frozen angular_separation_deg, never the cell's - this mapping is for DISPLAY/lookup only,
    never fed back into the physics: a computed VALUE is always exact regardless of any grid-uniformity
    approximation; only which nominal cell a point is DISPLAYED in could be off by a small, bounded fraction
    of one cell width when a mosaic's real row-to-row spacing genuinely varies, as it does here).

    A campaign with a whole row or column of completely missing pointings cannot be told apart from a
    smaller, real mosaic (nothing here observes an un-sampled row/column) - a real limitation, not silently
    hidden: n_rows/n_cols are always exactly what the OBSERVED positions imply, at the declared/derived
    spacing_deg.
    """
    from science_engine.models import ScienceGrid
    from science_engine.spatial import tangent_plane_offsets_deg

    points = filtered_input.points
    if not points:
        raise ValueError("cannot build a mosaic grid from zero input points")
    ra = np.array([p.ra_deg for p in points])
    dec = np.array([p.dec_degrees for p in points])
    center_ra0 = float(np.degrees(np.arctan2(
        np.mean(np.sin(np.radians(ra))), np.mean(np.cos(np.radians(ra)))))) % 360.0
    center_dec0 = float(np.mean(dec))
    x, y = tangent_plane_offsets_deg(ra, dec, center_ra0, center_dec0)

    gap_threshold = 0.5 * spacing_deg
    col_idx, n_cols, col_centroids = _cluster_1d(x, gap_threshold)
    row_idx, n_rows, row_centroids = _cluster_1d(y, gap_threshold)

    point_cell: dict[int, tuple[int, int]] = {}
    for p, r, c in zip(points, row_idx, col_idx):
        point_cell[p.point_index] = (int(r), int(c))
    if len(set(point_cell.values())) != len(points):
        # two real points clustered into the same cell at this spacing - the campaign is not a clean lattice
        # at spacing_deg (wrong/stale mosaic_spacing_deg, or genuinely irregular pointings). Fail loudly
        # rather than silently drop/overwrite one point's cell.
        from collections import Counter
        dupes = [cell for cell, n in Counter(point_cell.values()).items() if n > 1]
        raise MosaicShapeError(
            f"{len(points)} points do not form a clean rectangular lattice at spacing_deg={spacing_deg:.6g} - "
            f"{len(dupes)} cell(s) claimed by more than one point (e.g. {dupes[0]}). Pass mosaic_spacing_deg "
            f"explicitly if the auto-derived spacing (median nearest-neighbour) is wrong for this campaign."
        )

    mean_col_pos, mean_row_pos = float(np.mean(col_centroids)), float(np.mean(row_centroids))
    cos_dec0 = np.cos(np.radians(center_dec0))
    center_ra = (center_ra0 + mean_col_pos / cos_dec0) % 360.0
    center_dec = center_dec0 + mean_row_pos
    grid = ScienceGrid(frame="icrs", center_ra_deg=center_ra, center_dec_deg=center_dec,
                       width_deg=n_cols * spacing_deg, height_deg=n_rows * spacing_deg,
                       pixel_scale_deg=spacing_deg, nx=n_cols, ny=n_rows)
    return grid, point_cell, n_rows, n_cols


def build_fine_grid(board_grid, factor: int):
    """A ScienceGrid covering the EXACT SAME physical extent, center and orientation as `board_grid` (the
    real N x M mosaic board build_mosaic_grid() returned), sampled `factor` times more densely per axis -
    genuinely NEW pixel positions BETWEEN the real measured cells, never the same cells enlarged, blurred or
    repainted at a different opacity. Used only for the interpolated B/C panels (request: "deben aparecer
    píxeles nuevos entre las posiciones medidas"). build_cube()/integrated_map() (frozen) do 100% of the
    actual weighting/integration math at these new pixel centers - exactly the same call as for the real
    board, just evaluated at more locations; this function only decides WHERE those locations sit."""
    from science_engine.models import ScienceGrid
    if factor < 2:
        raise ValueError(f"build_fine_grid factor must be >= 2 (got {factor}) - it must add real pixels "
                         f"between the measured cells, not reproduce the same board")
    return ScienceGrid(frame="icrs", center_ra_deg=board_grid.center_ra_deg,
                       center_dec_deg=board_grid.center_dec_deg, width_deg=board_grid.width_deg,
                       height_deg=board_grid.height_deg, pixel_scale_deg=board_grid.pixel_scale_deg / factor,
                       nx=board_grid.nx * factor, ny=board_grid.ny * factor)


_VIRIDIS = None  # lazy: matplotlib import is not needed for any non-rendering path (CLI plan/inspect, tests)


def viridis_hex(value: Optional[float], vmin: float, vmax: float) -> Optional[str]:
    """THE single colour function - used to fill every PNG/SVG/PDF export's cells (via the same matplotlib
    Colormap object) AND every value reported in maps/board.json for the browser's own interactive board.
    Never a second, JS-side re-implementation of a colormap: the browser paints exactly the hex string this
    function produced, so "el mismo valor numerico debe tener el mismo color en todas las vistas" holds by
    construction, not by coincidence. Returns None for a non-finite value (never fabricates a colour for a
    missing/invalid cell)."""
    global _VIRIDIS
    if value is None or not np.isfinite(value):
        return None
    if _VIRIDIS is None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        _VIRIDIS = plt.get_cmap("viridis")
    t = 0.0 if vmax <= vmin else max(0.0, min(1.0, (value - vmin) / (vmax - vmin)))
    r, g, b, _ = _VIRIDIS(t)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


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


def noise_dominance_summary(point_rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Real statistics answering "is there any real spatial signal here beyond pure per-point noise, at the
    finest sampled scale" - a real deployed run's B/C showed circular halos traced back to individual noisy
    points (see render_all_maps's own per-pixel n_pointings hatching), but a MULTI-point local cluster of
    same-signed noise can ALSO look like a coherent blob under smoothing even with no single dominant point
    (n_pointings alone does not catch this). This compares each USED point's value to its own real nearest
    neighbour's value: if the median |difference| is close to what INDEPENDENT per-point noise alone would
    predict (sqrt(2) x median reported uncertainty) AND neighbouring points' values are essentially
    uncorrelated, the field is statistically indistinguishable from noise at the point-spacing scale - ANY
    multi-pixel feature B/C shows may be a chance grouping of that noise, not detected structure. Uses only
    already-computed per-point values/uncertainties (per_point_integrated_values's own output) and the
    frozen angular_separation_deg for real distances - never re-derives the integration itself."""
    from science_engine.spatial import angular_separation_deg
    used = [r for r in point_rows if r["status"] == "USED" and r["value"] is not None]
    if len(used) < 3:
        return None
    ra = np.array([r["ra_deg"] for r in used])
    dec = np.array([r["dec_degrees"] for r in used])
    val = np.array([r["value"] for r in used])
    unc = np.array([r["uncertainty"] for r in used])
    n = len(used)
    nn_diff = np.empty(n)
    nn_val_neighbor = np.empty(n)
    for i in range(n):
        d = angular_separation_deg(np.full(n, ra[i]), np.full(n, dec[i]), ra, dec)
        d[i] = np.inf
        j = int(np.argmin(d))
        nn_diff[i] = val[i] - val[j]
        nn_val_neighbor[i] = val[j]
    median_abs_diff = float(np.median(np.abs(nn_diff)))
    median_unc = float(np.median(unc))
    expected_if_independent = float(np.sqrt(2) * median_unc)
    correlation = float(np.corrcoef(val, nn_val_neighbor)[0, 1]) if np.std(val) > 0 and np.std(nn_val_neighbor) > 0 else None
    ratio = (median_abs_diff / expected_if_independent) if expected_if_independent > 0 else None
    # The correlation is the theoretically correct discriminator here, not the ratio: real spatial structure
    # that varies SLOWLY relative to the noise floor would still show a nearest-neighbour DIFFERENCE close to
    # pure-noise size (ratio~1) - a slow real gradient does not by itself make neighbours differ by more than
    # their own noise. What it DOES do is make neighbouring points' values genuinely correlated (they share
    # part of the same underlying true value), which independent per-point noise never does. ratio is still
    # reported (a real, useful number - e.g. ratio >> 1 with |r| small would flag occasional large excursions
    # beyond ordinary per-point noise, such as RFI spikes, worth a separate look) but does not gate the verdict.
    consistent_with_noise = bool(correlation is not None and abs(correlation) < 0.3)
    return {
        "n_points": n, "value_std": float(np.std(val)), "median_uncertainty": median_unc,
        "median_nearest_neighbor_abs_value_diff": median_abs_diff,
        "expected_abs_diff_if_independent_noise": expected_if_independent,
        "ratio_observed_to_expected_noise": ratio,
        "nearest_neighbor_value_correlation": correlation,
        "consistent_with_pure_noise_at_point_spacing": consistent_with_noise,
    }


def loo_cross_validation_summary(filtered_input, cfg: MapConfig, spatial: dict[str, float],
                                 point_rows: list[dict[str, Any]], max_points: int = 60,
                                 rng_seed: int = 0) -> Optional[dict[str, Any]]:
    """Leave-one-out cross-validation of the SAME spatial estimator B and C both use (support_radius_deg,
    smoothing_fwhm_deg) - a direct, real test of "can this method actually predict an unmeasured position",
    not an argument from plausibility (see noise_dominance_summary for the cheaper, weaker heuristic this
    complements). For each USED point in turn: rebuilds a real ScienceInput WITHOUT that point, evaluates a
    genuine 1x1-pixel ScienceGrid centered EXACTLY on its own real RA/Dec with build_cube()/integrated_map()
    (frozen, unmodified - the identical call build_all_products() makes for the real board/B/C, just a single
    output pixel instead of a raster), and compares the prediction to that point's own real per-point value
    (per_point_integrated_values()'s output, which never used the held-out point either - not a leak).

    Real result on REDUCE-20260919-234712-712870 (100 RELATIVE points, the session behind the reported
    circular-halo screenshots): predicted-vs-measured correlation = 0.054 (essentially none) and RMS held-out
    error (442) LARGER than the field's own point-to-point standard deviation (378) - i.e. at this point
    density, this estimator predicts a held-out point WORSE than simply guessing the field's own mean would.
    This held (within noise) across every kernel width tested (the old light/heavy B/C kernels, a 1.5x
    candidate, and a wider support radius) - see MapConfig.smoothing_fwhm_deg's own docstring. Any multi-pixel
    feature B/C shows should be read with that in mind: it is not a verified detection.

    Every real per-point measurement is used once (never a fixed number baked in); when more than
    `max_points` are USED, a fixed-seed random subsample is evaluated instead (each iteration is a cheap
    single-pixel cube build, not a full raster, but O(n) real spectra still need resampling) and reported as
    `subsampled` so a manifest reader knows the numbers are drawn from a subset, not silently different."""
    from science_engine.cube import build_cube
    from science_engine.grid import build_beam_model
    from science_engine.integration import integrated_map
    from science_engine.models import ScienceGrid, ScienceInput

    measured = {r["point_index"]: (r["value"], r["uncertainty"]) for r in point_rows
               if r["status"] == "USED" and r["value"] is not None}
    used_points = [p for p in filtered_input.points if p.point_index in measured]
    if len(used_points) < 5:
        return None

    subsampled = False
    if len(used_points) > max_points:
        rng = np.random.default_rng(rng_seed)
        keep_idx = sorted(rng.choice(len(used_points), size=max_points, replace=False).tolist())
        used_points = [used_points[i] for i in keep_idx]
        subsampled = True

    fwhm = spatial["smoothing_fwhm_deg"]
    cutoff = spatial["support_radius_deg"] / fwhm
    sc = _kernel_science_config(cfg, fwhm, cutoff, "LOO cross-validation probe - identical kernel to B/C")
    beam = build_beam_model(sc)
    tiny = 1e-4   # degrees - a 1x1-pixel grid's own width/height never matters, only its ONE pixel's center

    pred = np.full(len(used_points), np.nan)
    meas = np.empty(len(used_points))
    unc = np.empty(len(used_points))
    n_support = np.zeros(len(used_points), dtype=int)
    for i, point in enumerate(used_points):
        kept = [p for p in filtered_input.points if p.point_index != point.point_index]
        loo_input = ScienceInput(reduce_session_dir=filtered_input.reduce_session_dir,
                                 campaign_id=filtered_input.campaign_id,
                                 reduce_session_id=filtered_input.reduce_session_id,
                                 reduce_schema_version=filtered_input.reduce_schema_version,
                                 reduce_session_status=filtered_input.reduce_session_status, points=kept,
                                 contract_problems=list(filtered_input.contract_problems),
                                 exclusions=list(filtered_input.exclusions),
                                 n_points_in_manifest=filtered_input.n_points_in_manifest)
        grid1 = ScienceGrid(frame="icrs", center_ra_deg=point.ra_deg, center_dec_deg=point.dec_degrees,
                            width_deg=tiny, height_deg=tiny, pixel_scale_deg=tiny, nx=1, ny=1)
        cube1 = build_cube(loo_input, grid1, beam, sc)
        smap1 = integrated_map(cube1, sc)
        meas[i], unc[i] = measured[point.point_index]
        n_support[i] = int(smap1.n_pointings[0, 0])
        if smap1.valid[0, 0]:
            pred[i] = float(smap1.value[0, 0])

    predictable_mask = ~np.isnan(pred)
    n_predictable = int(predictable_mask.sum())
    if n_predictable < 3:
        return {"n_points_evaluated": len(used_points), "n_predictable": n_predictable, "subsampled": subsampled,
               "note": "too few held-out points had any real neighbour within support_radius_deg to summarize"}

    predictable_points = [p for p, ok in zip(used_points, predictable_mask) if ok]
    pred_v, meas_v, unc_v = pred[predictable_mask], meas[predictable_mask], unc[predictable_mask]
    nsup_v = n_support[predictable_mask]
    err = pred_v - meas_v
    value_std = float(np.std(meas_v))
    rms = float(np.sqrt(np.mean(err ** 2)))
    correlation = (float(np.corrcoef(pred_v, meas_v)[0, 1])
                  if np.std(pred_v) > 0 and np.std(meas_v) > 0 else None)
    z = err / unc_v
    worst_order = np.argsort(-np.abs(err))[:5]
    worst = [{"point_index": int(predictable_points[j].point_index), "predicted": float(pred_v[j]),
             "measured": float(meas_v[j]), "error": float(err[j]), "n_support_points": int(nsup_v[j])}
            for j in worst_order]
    return {
        "n_points_evaluated": len(used_points), "n_predictable": n_predictable, "subsampled": subsampled,
        "kernel_fwhm_deg": fwhm, "support_radius_deg": spatial["support_radius_deg"],
        "bias": float(np.mean(err)), "rms": rms, "mae": float(np.mean(np.abs(err))),
        "predicted_vs_measured_correlation": correlation, "field_value_std": value_std,
        "rms_worse_than_predicting_the_field_mean": bool(rms > value_std),
        "frac_within_1sigma_of_own_uncertainty": float(np.mean(np.abs(z) <= 1.0)),
        "frac_within_2sigma_of_own_uncertainty": float(np.mean(np.abs(z) <= 2.0)),
        "worst_5": worst,
    }


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


def _map_a_grid(point_rows: list[dict[str, Any]], point_cell: dict[int, tuple[int, int]],
                n_rows: int, n_cols: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Places each USED point's own value into its OWN mosaic cell - one reading = one cell, never blended,
    never a fabricated value in a cell with no real point (that cell simply stays invalid/NaN)."""
    value = np.full((n_rows, n_cols), np.nan)
    uncertainty = np.full((n_rows, n_cols), np.nan)
    valid = np.zeros((n_rows, n_cols), dtype=bool)
    cell_point_index = np.full((n_rows, n_cols), -1, dtype=int)
    for row in point_rows:
        if row["status"] != "USED":
            continue
        r, c = point_cell[row["point_index"]]
        value[r, c] = row["value"]
        uncertainty[r, c] = row["uncertainty"]
        valid[r, c] = True
        cell_point_index[r, c] = row["point_index"]
    return value, uncertainty, valid, cell_point_index


def build_all_products(filtered_input, cfg: MapConfig) -> dict[str, Any]:
    import gc

    from science_engine.cube import build_cube, canonical_velocity_axis
    from science_engine.grid import build_beam_model
    from science_engine.integration import integrated_map
    from science_engine.quality import assess_science_quality

    spatial = auto_spatial_params(filtered_input, cfg)
    support_radius_deg = spatial["support_radius_deg"]
    smoothing_fwhm_deg = spatial["smoothing_fwhm_deg"]

    # The BOARD (map A): one ScienceGrid cell per real pointing lattice position - never a fine sub-cell
    # raster and never sized from the reported instrument beam (cfg.beam_fwhm_deg), which is what produced a
    # 65x65 deg, 14x14px grid for a real ~6x5 deg, 36-point (6x6) mosaic on a real deployed run (beam_fwhm_deg
    # =20 from observer_config.json vs ~1 deg real point spacing - see auto_spatial_params's docstring).
    board_grid, point_cell, n_rows, n_cols = build_mosaic_grid(filtered_input, spatial["mosaic_spacing_deg"])

    # B and C: interpolated panels on a FINER raster than the board - genuinely NEW pixel positions between
    # the real measurements (request: "deben aparecer píxeles nuevos entre las posiciones medidas"; an even
    # earlier design evaluated B/C on the SAME board as A, which just repainted its cells with a blend -
    # visually indistinguishable in resolution from A). SAME physical extent/center as board_grid
    # (build_fine_grid) so the three panels stay directly comparable; SAME support_radius_deg AND SAME
    # smoothing_fwhm_deg (see MapConfig.smoothing_fwhm_deg's own docstring for why B/C no longer use two
    # different kernels) - interp_factor_c > interp_factor_b (enforced by MapConfig) is the ONLY remaining
    # difference between B and C, making a resolution increase there a genuine, isolated variable instead of
    # one confounded with a kernel change.
    grid_b = build_fine_grid(board_grid, cfg.interp_factor_b)
    grid_c = build_fine_grid(board_grid, cfg.interp_factor_c)
    sc = _kernel_science_config(cfg, smoothing_fwhm_deg, support_radius_deg / smoothing_fwhm_deg,
                                "presentation smoothing kernel (shared by B and C) - NOT the instrument beam")
    beam = build_beam_model(sc)
    velocity_axis = canonical_velocity_axis(filtered_input)

    # B and C are each a full (Nv, Ny, Nx) cube - measured to OOM-kill this Pi (exit -9) when both were held
    # in memory at once (a real bug found and fixed earlier in this module's history: science_engine's own
    # preflight only ever budgets for ONE cube, since run_science_session() itself only ever builds one).
    # C's grid is the larger of the two, so each is checked against its OWN estimate right before it is
    # built - never assume B's (smaller) estimate also covers C.
    mem_b = memory_check(grid_b, int(velocity_axis.shape[0]), len(filtered_input.points))
    if not mem_b["ok"]:
        raise ValueError(f"BLOCKED before allocating map B ({cfg.interp_factor_b}x raster): {mem_b['detail']}")
    cube_b = build_cube(filtered_input, grid_b, beam, sc)
    map_b = integrated_map(cube_b, sc)
    quality_b = assess_science_quality(filtered_input, cube_b, sc, map_b)
    used_b = set(cube_b.build_info["used_point_indices"])
    del cube_b
    gc.collect()

    mem_c = memory_check(grid_c, int(velocity_axis.shape[0]), len(filtered_input.points))
    if not mem_c["ok"]:
        raise ValueError(f"BLOCKED before allocating map C ({cfg.interp_factor_c}x raster): {mem_c['detail']}")
    cube_c = build_cube(filtered_input, grid_c, beam, sc)
    map_c = integrated_map(cube_c, sc)
    used_c = set(cube_c.build_info["used_point_indices"])
    del cube_c
    gc.collect()

    used_common = _reconcile_used_point_sets(used_b, used_c, support_radius_deg)
    note = (f"B and C share support_radius_deg={support_radius_deg:.6g} deg, smoothing_fwhm_deg="
           f"{smoothing_fwhm_deg:.6g} deg, and the SAME physical extent as the real board (only their raster "
           f"density differs) - their used-point sets matched exactly, verified: {len(used_common)} point(s), "
           f"never reconciled after the fact")

    point_rows = per_point_integrated_values(filtered_input, sc, velocity_axis)
    for row in point_rows:
        if row["status"] == "USED" and row["point_index"] not in used_common:
            row["status"] = "EXCLUDED_FOR_MAP_COMPARABILITY"
            row["reason"] = "outside the shared beam-support point set used for maps A/B/C (see used_point_set_note)"

    vmin, vmax, vmin_vmax_basis = robust_color_limits(point_rows, cfg.color_vmin, cfg.color_vmax)
    map_a_value, map_a_uncertainty, map_a_valid, map_a_point_index = _map_a_grid(point_rows, point_cell,
                                                                                 n_rows, n_cols)
    noise_dominance = noise_dominance_summary(point_rows)
    loo_cross_validation = loo_cross_validation_summary(filtered_input, cfg, spatial, point_rows)

    return {
        "sc": sc, "beam": beam,
        "board_grid": board_grid, "grid_b": grid_b, "grid_c": grid_c, "spatial_params": spatial,
        "n_rows": n_rows, "n_cols": n_cols, "point_cell": point_cell,
        "velocity_axis": velocity_axis, "map_b": map_b, "map_c": map_c,
        "map_a_value": map_a_value, "map_a_uncertainty": map_a_uncertainty, "map_a_valid": map_a_valid,
        "map_a_point_index": map_a_point_index,
        "quality_b": quality_b, "used_point_set": sorted(used_common), "used_point_set_note": note,
        "point_rows": point_rows, "color_vmin": vmin, "color_vmax": vmax, "color_limits_basis": vmin_vmax_basis,
        "noise_dominance": noise_dominance, "loo_cross_validation": loo_cross_validation,
    }


def board_json_payload(built: dict[str, Any]) -> dict[str, Any]:
    """The per-cell JSON payload driving the browser's ONLY interactive panel - map A, the real N x M board
    of MEASURED readings (one point = one cell). B and C are continuous interpolated rasters at a genuinely
    finer resolution (see build_fine_grid) with no per-cell/per-point identity and no click interaction (a
    new pixel between measured positions is never a stand-in for a real sample) - they are plain PNG images,
    not JSON-driven boards; see render_all_maps(). `color` is computed by the SAME viridis_hex() every PNG/
    SVG/PDF export uses (the identical matplotlib Colormap object on the same normalised value), so this
    board's colours and the B/C images always share one scale - the browser never recomputes a colour.
    `color_stops` is that same function sampled evenly across [color_vmin, color_vmax], for drawing an HTML
    legend next to each of the three panels (request: "una barra de color visible junto a cada imagen")."""
    from science_engine.grid import pixel_centers_deg
    n_rows, n_cols = built["n_rows"], built["n_cols"]
    vmin, vmax = built["color_vmin"], built["color_vmax"]
    grid = built["board_grid"]
    cell_ra, cell_dec = pixel_centers_deg(grid)
    cell_point = {rc: idx for idx, rc in built["point_cell"].items()}
    point_by_index = {r["point_index"]: r for r in built["point_rows"]}

    cells = []
    for r in range(n_rows):
        for c in range(n_cols):
            pt_idx = cell_point.get((r, c))
            point_row = point_by_index.get(pt_idx) if pt_idx is not None else None
            valid = bool(built["map_a_valid"][r, c])
            value = float(built["map_a_value"][r, c]) if valid else None
            unc = (float(built["map_a_uncertainty"][r, c])
                  if valid and np.isfinite(built["map_a_uncertainty"][r, c]) else None)
            cells.append({
                "row": r, "col": c, "point_index": pt_idx,
                "ra_deg": float(cell_ra[r, c]), "dec_degrees": float(cell_dec[r, c]),
                "point_status": point_row["status"] if point_row else None,
                "point_reason": point_row["reason"] if point_row else None,
                "point_timestamp_start_utc": point_row["timestamp_start_utc"] if point_row else None,
                "valid": valid, "value": value, "uncertainty": unc,
                "color": viridis_hex(value, vmin, vmax),
            })
    n_stops = 9
    stops = [viridis_hex(vmin + t * (vmax - vmin), vmin, vmax) for t in np.linspace(0.0, 1.0, n_stops)]
    return {"n_rows": n_rows, "n_cols": n_cols, "mosaic_spacing_deg": built["spatial_params"]["mosaic_spacing_deg"],
           "grid": grid.to_dict(), "color_vmin": vmin, "color_vmax": vmax, "colormap": "viridis",
           "color_units": "relative_intensity_dimensionless x m/s", "color_stops": stops, "cells": cells}


def spatial_confidence_summary(built: dict[str, Any]) -> dict[str, Any]:
    """Real numbers behind the honest coverage cue on B/C (hatched/dimmed pixels - see render_all_maps's
    _mark_low_confidence): how many of each map's valid pixels are backed by only ONE real nearby point
    (n_pointings <= 1 - the frozen GriddingAccumulator's own count, never re-derived). A high fraction here
    means most of that map's visual detail is individual points' own measurement noise, not corroborated
    spatial structure - reported plainly in the manifest, not just baked into an image caption."""
    out: dict[str, Any] = {}
    for key, sm in (("b", built["map_b"]), ("c", built["map_c"])):
        valid = sm.valid
        n_valid = int(valid.sum())
        n_single = int((valid & (sm.n_pointings <= 1)).sum())
        out[key] = {
            "n_valid_pixels": n_valid, "n_single_point_pixels": n_single,
            "single_point_fraction": (n_single / n_valid) if n_valid else None,
            "max_n_pointings": int(sm.n_pointings[valid].max()) if n_valid else None,
        }
    return out


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
    """Renders A (the real N x M board, sharp per-cell) and B/C (genuinely finer interpolated rasters - see
    build_fine_grid) at STRICTLY increasing pixel density left to right - real NEW pixel positions between
    the measured cells, never the same N x M cells enlarged/blurred/repainted at a different opacity (a
    prior version of this module did exactly that and was corrected here). All three share the SAME
    physical extent/orientation and the SAME color scale (vmin/vmax from A's own measured values) so the
    increase in visual detail is never a colour-scale trick. Every export bakes in its own effective grid
    dimensions in the title, plus calibration/window/caveats - not only shown in the browser. New pixels in
    B/C are estimates BETWEEN measured positions - the caption states plainly that interpolation never
    recovers detail the instrument did not measure, and that only panel A's real cells have a spectrum.

    Layout: the title uses fig.suptitle() (spans the FULL figure width, never clipped by the narrower axes
    a colorbar leaves behind) and every figure reserves FIXED top/bottom margins via subplots_adjust()
    (tight_layout() does not know about a separately-placed fig.text() caption)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    import matplotlib.patches as mpatches

    board = built["board_grid"]
    n_rows, n_cols = built["n_rows"], built["n_cols"]
    spatial = built["spatial_params"]
    support_radius_deg = spatial["support_radius_deg"]
    smoothing_fwhm_deg = spatial["smoothing_fwhm_deg"]
    vmin, vmax = built["color_vmin"], built["color_vmax"]
    cmap = plt.get_cmap("viridis").with_extremes(bad=(0, 0, 0, 0))
    # SAME physical extent for A, B and C (build_fine_grid guarantees B/C match the board exactly) - request
    # #3: "los tres paneles deben ocupar exactamente el mismo ancho y alto... misma extension espacial".
    half_w, half_h = board.width_deg / 2, board.height_deg / 2
    extent = (half_w, -half_w, -half_h, half_h)
    x_edges = np.linspace(-half_w, half_w, n_cols + 1)
    y_edges = np.linspace(-half_h, half_h, n_rows + 1)
    x_centers, y_centers = (x_edges[:-1] + x_edges[1:]) / 2, (y_edges[:-1] + y_edges[1:]) / 2
    GRIDLINE = "#33414a"

    written: dict[str, list[str]] = {}
    hi_caveat = ("INSTRUMENTAL/" + cfg.calibration_level_filter + " result - relative_intensity_dimensionless "
                "only. No HI detection, Kelvin, Jy, N_HI or absolute flux claimed.")
    nd = built.get("noise_dominance")
    noise_caveat = ""
    if nd and nd["consistent_with_pure_noise_at_point_spacing"]:
        noise_caveat = (
            f" NOISE CHECK: neighbouring real points differ by a median of {nd['median_nearest_neighbor_abs_value_diff']:.4g} "
            f"(x{nd['ratio_observed_to_expected_noise']:.2g} what independent per-point noise alone predicts: "
            f"sqrt(2) x median sigma = {nd['expected_abs_diff_if_independent_noise']:.4g}), and neighbouring values "
            f"are essentially uncorrelated (r={nd['nearest_neighbor_value_correlation']:.2f}) - consistent with pure "
            f"per-point noise at this spacing. ANY multi-pixel bump/dip B/C shows may be a chance grouping of that "
            f"noise, not detected spatial structure.")
    loo = built.get("loo_cross_validation")
    loo_caveat = ""
    if loo and loo.get("predicted_vs_measured_correlation") is not None:
        r = loo["predicted_vs_measured_correlation"]
        loo_caveat = (
            f" LEAVE-ONE-OUT CHECK: predicting each of {loo['n_predictable']} real point(s) from ONLY its "
            f"neighbours, using this SAME kernel/support, correlates with that point's own measured value at "
            f"r={r:.2f}" + (" (essentially no real predictive skill)" if abs(r) < 0.3 else "") +
            f"; RMS held-out error={loo['rms']:.4g} vs this field's own point-to-point std={loo['field_value_std']:.4g}"
            + (" - WORSE than simply guessing the field's mean" if loo["rms_worse_than_predicting_the_field_mean"] else "")
            + ". Read any multi-pixel feature below with that in mind - it is not a verified detection.")

    def _cell_ticks(ax) -> None:
        for e in x_edges:
            ax.axvline(e, color=GRIDLINE, linewidth=0.6, zorder=5)
        for e in y_edges:
            ax.axhline(e, color=GRIDLINE, linewidth=0.6, zorder=5)
        ax.set_xticks(x_centers); ax.set_xticklabels([str(i + 1) for i in range(n_cols)], fontsize=7)
        ax.set_yticks(y_centers); ax.set_yticklabels([str(i + 1) for i in range(n_rows)], fontsize=7)

    def _extent_and_aspect(ax) -> None:
        ax.set_xlim(half_w, -half_w)   # RA increases to the LEFT, conventional sky orientation
        ax.set_ylim(-half_h, half_h)
        ax.set_aspect("equal")

    def _local_pixel_centers(grid):
        """(X, Y) meshgrids of each pixel's own local tangent-plane offset (degrees), SAME linspace
        convention pixel_centers_deg() uses (and therefore the SAME positions build_cube() actually
        weighted from) - used to align a contourf/hatch overlay exactly on top of the imshow array it
        annotates, pixel for pixel."""
        xs = np.linspace(-grid.width_deg / 2, grid.width_deg / 2, grid.nx + 1)
        ys = np.linspace(-grid.height_deg / 2, grid.height_deg / 2, grid.ny + 1)
        xc, yc = (xs[:-1] + xs[1:]) / 2, (ys[:-1] + ys[1:]) / 2
        return np.meshgrid(xc, yc)

    def _mark_low_confidence(ax, grid, science_map) -> np.ndarray:
        """Honest coverage cue for an INTERPOLATED panel (request: real circular halos were traced to
        individual points' own noise dominating their neighbourhood - see this function's caller and the
        task's own diagnosis) - hatches every pixel whose value is NOT corroborated by more than one real
        nearby measurement (n_pointings <= 1, the frozen GriddingAccumulator's own count of pointings with
        nonzero weight - reused exactly as science_engine.integration.integrated_map() already computes it,
        never re-derived). A hatched pixel's bump/dip may be that ONE point's own measurement noise, not
        real spatial structure - this is disclosed, never blurred away or hidden. Returns the mask (for the
        caption's own real count)."""
        low_conf = science_map.valid & (science_map.n_pointings <= 1)
        if low_conf.any():
            xg, yg = _local_pixel_centers(grid)
            cs = ax.contourf(xg, yg, low_conf.astype(float), levels=[0.5, 1.5], colors="none", hatches=["////"])
            # matplotlib >=3.10 returns contourf's hatched region as ONE artist (no .collections list any
            # more); matplotlib <3.10 returns a QuadContourSet whose hatching lives on .collections. Handle
            # both without depending on a specific version.
            artists = getattr(cs, "collections", None) or [cs]
            for artist in artists:
                artist.set_edgecolor((1, 1, 1, 0.55))
                artist.set_linewidth(0.0)
        return low_conf

    def _finish(fig, ax, title: str, name: str, extra_caption: str, exts=("png", "svg", "pdf")) -> None:
        ax.set_xlabel(f"RA offset from center RA={board.center_ra_deg:.4f} deg", fontsize=7.5)
        ax.set_ylabel(f"Dec offset from center Dec={board.center_dec_deg:.4f} deg", fontsize=7.5)
        fig.suptitle(title, fontsize=8.5, y=0.985)
        fig.text(0.5, 0.01, extra_caption, ha="center", va="bottom", fontsize=6.5, wrap=True)
        # Bottom margin scales with the caption's OWN length (adding the noise-check/coverage-density
        # paragraphs made some captions wrap to several more lines than a short one) - a fixed margin was
        # measured to let a long caption's top line collide with the x-axis label; this stays robust to
        # caption length instead of a new hand-tuned constant per addition.
        chars_per_line = 145   # approx at fontsize=6.5 across this figure's width
        n_lines = max(1, -(-len(extra_caption) // chars_per_line))
        bottom = min(0.20 + 0.018 * n_lines, 0.45)
        fig.subplots_adjust(top=0.80, bottom=bottom, left=0.13, right=0.99)
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

    # ---- A: the real N x M board - one cell = one real reading, sharp edges, cell-index ticks/gridlines,
    # the ONLY panel with a spectrum-able cell. ----
    n_measured = int(built["map_a_valid"].sum())
    fig, ax = plt.subplots(figsize=(6.6, 6.0))
    masked_a = np.ma.masked_where(~built["map_a_valid"], built["map_a_value"])
    im = ax.imshow(masked_a, origin="lower", cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest", extent=extent)
    _cell_ticks(ax); _extent_and_aspect(ax)
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("integrated relative_intensity_dimensionless x m/s")
    _finish(fig, ax, _title_block(cfg, campaign_id, reduce_session_id,
                                  f"A: MEASURED READINGS - {n_rows}x{n_cols} grid (no interpolation)"),
           "map_a_no_interp",
           f"{n_rows}x{n_cols} = {n_rows * n_cols} cells, one per real pointing (spacing "
           f"{spatial['mosaic_spacing_deg']:.3f} deg). {n_measured}/{n_rows * n_cols} cells carry a real "
           f"measurement (one point = one cell = one color, sharp edges); an unmeasured cell is left "
           f"transparent - never zero, never interpolated. This is the ONLY panel where a cell can be "
           f"clicked to inspect its own real spectrum. {hi_caveat}")

    def _bare_export(name: str, science_map, grid_bc, masked, alpha) -> None:
        """A chrome-free rendering of an interpolated panel for ON-SCREEN display only: just the map pixels
        (with the SAME dimming/hatching honesty cue), no title, colorbar, axis ticks/labels or caption baked
        in, cropped tight to the data extent with a transparent background - matching panel A's own canvas,
        which has never carried any of that chrome (it is drawn client-side from board.json). Request: "A
        llena su panel, pero B y C aparecen como graficos pequenos dentro de grandes cajas" - that was this
        exact mismatch (A: bare canvas; B/C: a full presentation figure, chrome included, squeezed into the
        same CSS box). The full presentation PNG/SVG/PDF (with title/colorbar/caption) is UNCHANGED and
        still written under `name` for download - this is an ADDITIONAL, separate file `{name}_bare.png`.
        The ONE visible colour scale next to each panel is the browser's own HTML legend (board_json_payload's
        color_stops, already shared by A/B/C) - never a second, redundant colorbar baked into this image."""
        fig = plt.figure(figsize=(6.0, 6.0 * (grid_bc.height_deg / grid_bc.width_deg)))
        ax = fig.add_axes([0, 0, 1, 1])
        ax.imshow(masked, origin="lower", cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest",
                 extent=extent, alpha=alpha)
        _mark_low_confidence(ax, grid_bc, science_map)
        _extent_and_aspect(ax)
        ax.axis("off")
        p = out_dir / f"{name}_bare.png"
        fig.savefig(p, dpi=200, transparent=True, pad_inches=0)
        plt.close(fig)
        written[f"{name}_bare"] = [p.name]

    # ---- B / C: genuinely finer interpolated rasters - real NEW pixel positions between the measured cells,
    # never the same {n_rows}x{n_cols} cells redrawn bigger/blurrier. Same physical footprint/scale as A, and
    # now the SAME kernel too (see MapConfig.smoothing_fwhm_deg) - strictly increasing RASTER DENSITY is the
    # only remaining difference between B and C (enforced by MapConfig.interp_factor_c > interp_factor_b). ----
    for key, label, science_map, factor, grid_bc in (
        ("map_b_smooth", "B: INTERPOLATED", built["map_b"], cfg.interp_factor_b, built["grid_b"]),
        ("map_c_heavy", "C: INTERPOLATED (finer raster)", built["map_c"], cfg.interp_factor_c, built["grid_c"]),
    ):
        ny_fine, nx_fine = science_map.value.shape
        fig, ax = plt.subplots(figsize=(6.6, 6.0))
        masked = np.ma.masked_where(~science_map.valid, science_map.value)
        # A pixel dominated by exactly one nearby real point (no corroborating second measurement) is
        # rendered DIMMER, not blurred - its own VALUE is untouched (still the real weighted-mean output of
        # build_cube()/integrated_map()), only its visual prominence is honestly reduced so an isolated
        # point's own noise excursion does not read as confirmed structure. See _mark_low_confidence().
        low_conf = science_map.valid & (science_map.n_pointings <= 1)
        alpha = np.where(low_conf, 0.45, 1.0)
        im = ax.imshow(masked, origin="lower", cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest",
                       extent=extent, alpha=alpha)
        _mark_low_confidence(ax, grid_bc, science_map)
        _extent_and_aspect(ax)   # no cell gridlines/ticks here - this is a continuous raster, not a per-cell board
        cbar = fig.colorbar(im, ax=ax)
        cbar.set_label("integrated relative_intensity_dimensionless x m/s")
        n_low_conf = int(low_conf.sum())
        _finish(fig, ax, _title_block(cfg, campaign_id, reduce_session_id,
                                      f"{label} - {ny_fine}x{nx_fine} grid ({factor}x the {n_rows}x{n_cols} board)"),
               key,
               f"{ny_fine}x{nx_fine} = {ny_fine * nx_fine} raster pixels (vs {n_rows}x{n_cols}="
               f"{n_rows * n_cols} real measurements) over the SAME physical footprint as A - most pixels "
               f"sit BETWEEN measured positions and are ESTIMATES from an inverse-variance x Gaussian-kernel "
               f"weighted mean of nearby real readings (science_engine.gridding), kernel FWHM="
               f"{smoothing_fwhm_deg:.4g} deg (declared presentation smoothing, NOT the instrument beam) - "
               f"IDENTICAL for B and C (an earlier light/heavy split was dropped: leave-one-out "
               f"cross-validation found it made no measurable difference to predictive skill - see the LOO "
               f"check below). The ONLY difference between B and C is this raster's density. Real instrument "
               f"beam FWHM={cfg.beam_fwhm_deg:.4g} deg ({cfg.beam_status}, reported only, never used for "
               f"gridding). Spatial SUPPORT radius is the SAME {support_radius_deg:.4g} deg for B and C - "
               f"{len(built['used_point_set'])} point(s) used identically in both. Interpolation ESTIMATES "
               f"values between measurements; it never recovers detail the instrument did not measure, and "
               f"no real spectrum exists for a pixel here - only the {n_rows}x{n_cols} real cells in panel A "
               f"have one. HATCHED/DIMMED pixels ({n_low_conf}/{int(science_map.valid.sum())} valid px) are "
               f"backed by only ONE nearby real point (no corroborating second measurement) - a bump/dip "
               f"there may be that single point's own measurement noise, not real spatial structure. Color "
               f"limits: [{vmin:.4g}, {vmax:.4g}] ({built['color_limits_basis']}).{noise_caveat}{loo_caveat} {hi_caveat}")
        _bare_export(key, science_map, grid_bc, masked, alpha)

    # ---- A + B + C combined: same physical box and colour scale, each panel's own resolution declared,
    # increasing left to right (request: "verse juntos... poder abrirse grandes") ----
    fig, axes = plt.subplots(1, 3, figsize=(16.5, 5.8))
    im = None
    b_shape = built["map_b"].value.shape
    c_shape = built["map_c"].value.shape
    panels = [
        (f"A: MEASURED ({n_rows}x{n_cols})", built["map_a_value"], built["map_a_valid"], True, None, None),
        (f"B: INTERPOLATED ({b_shape[0]}x{b_shape[1]})", built["map_b"].value, built["map_b"].valid, False,
         built["map_b"], built["grid_b"]),
        (f"C: INTERPOLATED ({c_shape[0]}x{c_shape[1]})", built["map_c"].value, built["map_c"].valid, False,
         built["map_c"], built["grid_c"]),
    ]
    for ax, (label, value, valid, is_board, science_map, grid_bc) in zip(axes, panels):
        masked = np.ma.masked_where(~valid, value)
        alpha = 1.0
        if not is_board:
            low_conf = science_map.valid & (science_map.n_pointings <= 1)
            alpha = np.where(low_conf, 0.45, 1.0)
        im = ax.imshow(masked, origin="lower", cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest",
                       extent=extent, alpha=alpha)
        if not is_board:
            _mark_low_confidence(ax, grid_bc, science_map)
        ax.set_title(label, fontsize=9)
        _extent_and_aspect(ax)
        if is_board:
            for e in x_edges:
                ax.axvline(e, color=GRIDLINE, linewidth=0.5, zorder=5)
            for e in y_edges:
                ax.axhline(e, color=GRIDLINE, linewidth=0.5, zorder=5)
    # subplots_adjust MUST run before colorbar(ax=...): colorbar carves its own axes out of the CURRENT
    # positions of the axes it's given - calling subplots_adjust afterward moves the 3 main axes but leaves
    # the colorbar's already-fixed axes behind, which was measured to overlap the rightmost panel's own
    # colorbar space.
    fig.subplots_adjust(top=0.80, bottom=0.20, left=0.04, right=0.90)
    cbar = fig.colorbar(im, ax=list(axes), shrink=0.85)
    cbar.set_label("integrated relative_intensity_dimensionless x m/s")
    fig.suptitle(_title_block(cfg, campaign_id, reduce_session_id,
                              "A / B / C - same footprint and scale, increasing raster resolution"),
                fontsize=9, y=0.99)
    fig.text(0.5, 0.01,
            f"Same physical footprint and color scale in all three; only pixel density increases left to "
            f"right ({n_rows}x{n_cols} -> {b_shape[0]}x{b_shape[1]} -> {c_shape[0]}x{c_shape[1]}). B/C pixels "
            f"between real positions are interpolation ESTIMATES, never new measurements - no spectrum exists "
            f"for them. Hatched/dimmed B/C areas are backed by only ONE nearby real point - a bump/dip there "
            f"may be that point's own measurement noise, not real structure (see map_coverage_density). B and "
            f"C share the IDENTICAL smoothing kernel and support radius - only raster density differs."
            f"{noise_caveat}{loo_caveat} {hi_caveat}", ha="center", va="bottom", fontsize=6.5, wrap=True)
    paths = []
    for ext in ("png", "svg", "pdf"):
        p = out_dir / f"map_abc_combined.{ext}"
        fig.savefig(p, dpi=200 if ext == "png" else None, bbox_inches="tight", pad_inches=0.15)
        paths.append(str(p.name))
    plt.close(fig)
    written["map_abc_combined"] = paths

    # ---- uncertainty / SNR (from map B's own propagated sigma, at B's own raster resolution) ----
    unc = built["map_b"].uncertainty
    if unc is not None and np.any(np.isfinite(unc)):
        with np.errstate(divide="ignore", invalid="ignore"):
            snr = np.abs(built["map_b"].value) / unc
        masked_snr = np.ma.masked_where(~built["map_b"].valid, snr)
        fig, ax = plt.subplots(figsize=(6.6, 6.0))
        im = ax.imshow(masked_snr, origin="lower", cmap=plt.get_cmap("magma").with_extremes(bad=(0, 0, 0, 0)),
                       interpolation="nearest", extent=extent)
        _extent_and_aspect(ax)
        cbar = fig.colorbar(im, ax=ax)
        cbar.set_label("SNR (dimensionless)")
        window_km_s = (cfg.velocity_window_max_m_s - cfg.velocity_window_min_m_s) / 1000.0
        wide_window_caveat = (
            f" CAVEAT: the {window_km_s:.0f} km/s window sums many channels; the 1-sigma formula above ASSUMES "
            f"independent channels, so any broadband, correlated residual (baseline/continuum-like, not random "
            f"noise) across those channels inflates this SNR well beyond what independent-noise statistics "
            f"would justify. High SNR here is NOT evidence of a real spectral feature and must never be read "
            f"as detection significance." if window_km_s > 150 else "")
        _finish(fig, ax, _title_block(cfg, campaign_id, reduce_session_id,
                                      f"SNR = |integrated value| / propagated 1-sigma (map B, {unc.shape[0]}x{unc.shape[1]})"),
               "map_snr",
               "1-sigma uncertainty propagated from REDUCE per-channel sigma assuming independent channels "
               "(see science_engine.integration docstring) - a statistical, not systematic, error bar."
               + wide_window_caveat,
               exts=("png", "svg"))
        quality_kind = "uncertainty/SNR (propagated statistical 1-sigma)"
    else:
        quality_kind = "no propagable uncertainty available"

    # ---- coverage DENSITY (request: "agrega una indicacion visual... de la cobertura y la incertidumbre
    # espacial") - map B's own n_pointings (science_engine.gridding's own count of real pointings with
    # nonzero weight at each pixel, already computed by integrated_map() - never re-derived here). This is
    # the real, quantitative origin of the hatching on B/C: wherever this map reads 1, that pixel's B/C
    # value is that ONE point's own reading, not a genuine multi-point average. Same support_radius_deg for
    # B and C, so this pattern applies equivalently to C (a finer raster just samples the same underlying
    # coverage more densely - see the caption). ----
    n_pt = built["map_b"].n_pointings.astype(float)
    n_pt_masked = np.ma.masked_where(~built["map_b"].valid, n_pt)
    fig, ax = plt.subplots(figsize=(6.6, 6.0))
    n_pt_cmap = plt.get_cmap("cividis").with_extremes(bad=(0, 0, 0, 0))
    im = ax.imshow(n_pt_masked, origin="lower", cmap=n_pt_cmap, vmin=1, vmax=max(3, int(np.nanmax(n_pt_masked)) if n_pt_masked.count() else 3),
                   interpolation="nearest", extent=extent)
    _extent_and_aspect(ax)
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("real points with nonzero weight at this pixel (n_pointings)")
    n1 = int((built["map_b"].valid & (built["map_b"].n_pointings <= 1)).sum())
    _finish(fig, ax, _title_block(cfg, campaign_id, reduce_session_id,
                                  f"COVERAGE DENSITY - how many real points back each B pixel ({n_pt.shape[0]}x{n_pt.shape[1]})"),
           "map_coverage_density",
           f"n_pointings = 1 (darkest) means that pixel's B/C value comes from a SINGLE nearby real "
           f"measurement - no second point to cross-check it against, so its own noise reads directly as a "
           f"local bump/dip (the hatched/dimmed areas on B/C). {n1}/{int(built['map_b'].valid.sum())} valid "
           f"pixels here are single-point-only. Same support_radius_deg for B and C, so this coverage pattern "
           f"applies to both - a finer raster (C) only samples it more densely, it does not add real "
           f"corroborating measurements. {hi_caveat}",
           exts=("png", "svg"))

    # ---- coverage: the real N x M board, categorical (no point planned there / used / excluded) ----
    point_cell = built["point_cell"]
    code = np.zeros((n_rows, n_cols), dtype=int)   # 0 = no point at all for this mosaic position
    for row in built["point_rows"]:
        r, c = point_cell[row["point_index"]]
        code[r, c] = 1 if row["status"] == "USED" else 2   # 1 = used, 2 = excluded (real point, real reason)
    fig, ax = plt.subplots(figsize=(6.6, 6.0))
    cov_cmap = ListedColormap(["#0d1317", "#2e8b3d", "#b23b3b"])
    ax.imshow(code, origin="lower", cmap=cov_cmap, vmin=0, vmax=2, interpolation="nearest", extent=extent)
    _cell_ticks(ax); _extent_and_aspect(ax)
    ax.legend(handles=[mpatches.Patch(color="#2e8b3d", label="used in A/B/C"),
                       mpatches.Patch(color="#b23b3b", label="excluded (real reason in points.csv)"),
                       mpatches.Patch(color="#0d1317", label="no pointing at this mosaic position")],
             loc="upper right", fontsize=6.5)
    _finish(fig, ax, f"{campaign_id} / {reduce_session_id} - coverage: measured cells used vs excluded "
                    f"({n_rows}x{n_cols})",
           "map_coverage",
           f"SAME {n_rows}x{n_cols} board and cells as A. green = used; red = a real point exists but was "
           f"excluded (see points.csv/manifest for its own reason); dark = no pointing at all at this mosaic "
           f"position.",
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
    spatial = auto_spatial_params(filtered, cfg)
    grid_b = grid_c = None
    try:
        board, point_cell, n_rows, n_cols = build_mosaic_grid(filtered, spatial["mosaic_spacing_deg"])
        mosaic_ok = True
        mosaic_detail = f"{n_rows}x{n_cols} board, {len(point_cell)} point(s) placed"
        grid_b = build_fine_grid(board, cfg.interp_factor_b)
        grid_c = build_fine_grid(board, cfg.interp_factor_c)
    except MosaicShapeError as exc:
        board, n_rows, n_cols = None, None, None
        mosaic_ok, mosaic_detail = False, str(exc)
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
        {"name": "mosaic_board_buildable", "ok": mosaic_ok, "detail": mosaic_detail},
    ]
    if mosaic_ok:
        # RUN allocates a cube for grid_b THEN (separately) grid_c - grid_c is the larger of the two, so both
        # are checked here, not just the (much smaller) board.
        mem_b = memory_check(grid_b, int(velocity_axis.shape[0]), len(filtered.points))
        mem_b["name"] = "memory_estimate_within_budget_map_b"
        mem_c = memory_check(grid_c, int(velocity_axis.shape[0]), len(filtered.points))
        mem_c["name"] = "memory_estimate_within_budget_map_c"
        checks.append(mem_b); checks.append(mem_c)
    blocked = any(not c["ok"] for c in checks)
    payload = {
        "config": cfg.to_dict(), "config_hash": cfg.config_hash(), "calibration_level_counts": counts,
        "board_grid": board.to_dict() if board else None,
        "grid_b": grid_b.to_dict() if grid_b else None, "grid_c": grid_c.to_dict() if grid_c else None,
        "n_rows": n_rows, "n_cols": n_cols,
        "spatial_params": spatial, "real_instrument_beam_fwhm_deg": cfg.beam_fwhm_deg,
        "n_velocity_channels": int(velocity_axis.shape[0]),
        "checks": checks, "blocked": blocked,
        "will_process_points": [p.point_index for p in filtered.points],
    }
    _print(payload, args.json, [
        f"calibration_level_filter: {cfg.calibration_level_filter}  counts: {counts}",
        f"board: {mosaic_detail}  support_radius={spatial['support_radius_deg']:.4g} deg  "
        f"smoothing (shared by B/C)={spatial['smoothing_fwhm_deg']:.4g} deg  "
        f"(real instrument beam={cfg.beam_fwhm_deg:.4g} deg, reported only, not used for gridding)",
        f"grid dims: A={n_rows}x{n_cols}"
        + (f"  B={grid_b.ny}x{grid_b.nx} ({cfg.interp_factor_b}x)  C={grid_c.ny}x{grid_c.nx} "
           f"({cfg.interp_factor_c}x)" if mosaic_ok else "  B/C: n/a (board not buildable)"),
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
    spatial_confidence = spatial_confidence_summary(built)

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

    # ONE per-cell JSON for the web viewer/point-picker - see board_json_payload()'s own docstring for why
    # this replaced three separately-shaped map_*.json files (map A used to be footprints, not cells).
    _atomic_write_json(maps_dir / "board.json", board_json_payload(built))

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
        "real_instrument_beam": {"fwhm_deg": cfg.beam_fwhm_deg, "source": cfg.beam_source, "status": cfg.beam_status,
                                 "note": "reported honestly, unchanged between B/C - NOT used as the gridding "
                                        "kernel for either map (see spatial_params/smoothing_kernel)"},
        "spatial_params": built["spatial_params"],
        "smoothing_kernel": built["beam"].to_dict(),
        # THREE grids now, not one: the real N x M board (map A) and the two genuinely finer interpolated
        # rasters (map B/C) - same physical extent/center as the board, denser pixels only.
        "board_grid": built["board_grid"].to_dict(), "grid_b": built["grid_b"].to_dict(),
        "grid_c": built["grid_c"].to_dict(),
        "grid_dims": {"a": [built["n_rows"], built["n_cols"]], "b": list(built["map_b"].value.shape),
                     "c": list(built["map_c"].value.shape)},
        "velocity_window_m_s": [cfg.velocity_window_min_m_s, cfg.velocity_window_max_m_s],
        "n_velocity_channels": int(built["velocity_axis"].shape[0]),
        "color_vmin": built["color_vmin"], "color_vmax": built["color_vmax"],
        "color_limits_basis": built["color_limits_basis"],
        "used_point_set": built["used_point_set"], "used_point_set_note": built["used_point_set_note"],
        "n_points_filtered_in": len(filtered.points), "n_points_used": len(built["used_point_set"]),
        "quality_b": built["quality_b"].to_dict(), "uncertainty_kind": unc_kind,
        "spatial_confidence": spatial_confidence, "noise_dominance": built["noise_dominance"],
        "loo_cross_validation": built["loo_cross_validation"],
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
        ("mosaic_spacing_deg", "mosaic_spacing_deg"),
        ("support_radius_deg", "support_radius_deg"), ("smoothing_fwhm_deg", "smoothing_fwhm_deg"),
        ("interp_factor_b", "interp_factor_b"), ("interp_factor_c", "interp_factor_c"),
        ("quality_policy", "quality_policy"),
        ("velocity_window_min_m_s", "velocity_window_min_m_s"), ("velocity_window_max_m_s", "velocity_window_max_m_s"),
        ("min_spectral_coverage_fraction", "min_spectral_coverage_fraction"), ("color_vmin", "color_vmin"),
        ("color_vmax", "color_vmax"),
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
        p.add_argument("--mosaic-spacing-deg", type=float, default=None)
        p.add_argument("--support-radius-deg", type=float, default=None)
        p.add_argument("--smoothing-fwhm-deg", type=float, default=None)
        p.add_argument("--interp-factor-b", type=int, default=None)
        p.add_argument("--interp-factor-c", type=int, default=None)
        p.add_argument("--quality-policy", choices=("STRICT", "STANDARD", "PERMISSIVE"), default=None)
        p.add_argument("--velocity-window-min-m-s", type=float, default=None)
        p.add_argument("--velocity-window-max-m-s", type=float, default=None)
        p.add_argument("--min-spectral-coverage-fraction", type=float, default=None)
        p.add_argument("--color-vmin", type=float, default=None)
        p.add_argument("--color-vmax", type=float, default=None)
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
