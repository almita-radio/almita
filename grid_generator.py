#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Grid Generator for Radio Astronomy Sky Surveys
Generates CSV with planned observation points (NO INDI CONNECTION REQUIRED)

This script ONLY calculates grid points and creates a CSV plan.
Use this CSV later for actual observations with your capture script.

Output: ./data/mosaic/{session_name}-{timestamp}/mosaic.csv
Output: ./data/mosaic/{session_name}-{timestamp}/mosaic.png (if matplotlib available)
Output: ./data/mosaic/{session_name}-{timestamp}/grid_metadata.json
"""

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    from astropy_offline import configure_astropy_offline
    configure_astropy_offline()
    import astropy.units as u
    from astropy.coordinates import SkyCoord, SkyOffsetFrame
except ImportError as exc:
    raise ImportError(
        "Astropy is required for spherical grid generation. "
        "Install with: pip install astropy"
    ) from exc


def normalize_ra_hours(ra_hours: float) -> float:
    """Normalize RA to [0, 24)."""
    return ra_hours % 24.0


def compute_nominal_spacing_deg(beam_fwhm_deg: float, beam_sampling_fraction: float) -> float:
    """Compute nominal point spacing from beam and sampling settings."""
    if beam_fwhm_deg <= 0:
        raise ValueError("beam_fwhm_deg must be > 0")
    if beam_sampling_fraction <= 0:
        raise ValueError("beam_sampling_fraction must be > 0")
    return beam_fwhm_deg * beam_sampling_fraction


def _samples_for_extent(total_extent_deg: float, nominal_spacing_deg: float) -> int:
    """Return the number of samples needed to cover an extent with edge-inclusive spacing."""
    if total_extent_deg <= 0:
        raise ValueError("total_extent_deg must be > 0")
    if nominal_spacing_deg <= 0:
        raise ValueError("nominal_spacing_deg must be > 0")

    raw_steps = total_extent_deg / nominal_spacing_deg
    if abs(raw_steps - round(raw_steps)) < 1e-9:
        raw_steps = round(raw_steps)
    steps = int(math.ceil(raw_steps))
    return max(2, steps + 1)


def calculate_grid_shape(width_deg: float, height_deg: float, nominal_spacing_deg: float) -> Tuple[int, int]:
    """
    Calculate rows/columns needed to cover region extents with target spacing.

    Uses edge-inclusive sampling so both region edges are covered.
    """
    if width_deg <= 0 or height_deg <= 0:
        raise ValueError("width_deg and height_deg must be > 0")
    if nominal_spacing_deg <= 0:
        raise ValueError("nominal_spacing_deg must be > 0")

    cols = _samples_for_extent(width_deg, nominal_spacing_deg)
    rows = _samples_for_extent(height_deg, nominal_spacing_deg)
    return rows, cols


def _axis_offsets(total_extent_deg: float, n_samples: int) -> List[float]:
    """Build symmetric offsets from -extent/2 to +extent/2."""
    if n_samples < 2:
        return [0.0]
    step = total_extent_deg / (n_samples - 1)
    start = -total_extent_deg / 2.0
    return [start + idx * step for idx in range(n_samples)]


def build_spherical_grid(
    center_ra_hours: float,
    center_dec_deg: float,
    width_deg: float,
    height_deg: float,
    beam_fwhm_deg: float,
    beam_sampling_fraction: float,
) -> Tuple[List[Dict], Dict]:
    """
    Build a spherical grid around center coordinates.

    The grid is defined in an offset frame centered on the target, then transformed
    to ICRS RA/DEC. This keeps geometry angularly meaningful over the sphere.
    """
    if not (-90.0 <= center_dec_deg <= 90.0):
        raise ValueError("center_dec_deg must be between -90 and +90")

    nominal_spacing_deg = compute_nominal_spacing_deg(beam_fwhm_deg, beam_sampling_fraction)
    rows, cols = calculate_grid_shape(width_deg, height_deg, nominal_spacing_deg)

    lon_offsets = _axis_offsets(width_deg, cols)
    lat_offsets = _axis_offsets(height_deg, rows)

    center = SkyCoord(ra=center_ra_hours * u.hourangle, dec=center_dec_deg * u.deg, frame="icrs")
    offset_frame = SkyOffsetFrame(origin=center)

    # Geometric identity is row-major; scan order is assigned separately.
    geometric_points: Dict[Tuple[int, int], Dict] = {}
    for row in range(rows):
        for col in range(cols):
            offset_coord = SkyCoord(lon=lon_offsets[col] * u.deg, lat=lat_offsets[row] * u.deg, frame=offset_frame)
            sky_coord = offset_coord.transform_to("icrs")

            point_id = row * cols + col + 1
            center_distance_deg = center.separation(sky_coord).deg
            geometric_points[(row, col)] = {
                "point_id": point_id,
                "row": row,
                "column": col,
                "grid_row": row,
                "grid_col": col,
                "ra": normalize_ra_hours(sky_coord.ra.hour),
                "dec": sky_coord.dec.deg,
                "target_ra_hours": normalize_ra_hours(sky_coord.ra.hour),
                "target_dec_degrees": sky_coord.dec.deg,
                "center_distance_deg": center_distance_deg,
            }

    points: List[Dict] = []
    scan_order = 1
    for row in range(rows):
        col_sequence = range(cols) if row % 2 == 0 else range(cols - 1, -1, -1)
        for col in col_sequence:
            point = dict(geometric_points[(row, col)])
            point["scan_order"] = scan_order
            points.append(point)
            scan_order += 1

    metadata = {
        "center_ra_hours": normalize_ra_hours(center_ra_hours),
        "center_ra": normalize_ra_hours(center_ra_hours),
        "center_dec_degrees": center_dec_deg,
        "center_dec": center_dec_deg,
        "width_deg": width_deg,
        "height_deg": height_deg,
        "region_width_deg": width_deg,
        "region_height_deg": height_deg,
        "beam_fwhm_deg": beam_fwhm_deg,
        "beam_sampling_fraction": beam_sampling_fraction,
        "nominal_spacing_deg": nominal_spacing_deg,
        "rows": rows,
        "columns": cols,
        "total_points": len(points),
        "scan_strategy": "serpentine",
        "actual_spacing_ra_axis_deg": width_deg / (cols - 1),
        "actual_spacing_dec_axis_deg": height_deg / (rows - 1),
    }

    return points, metadata


class GridGenerator:
    """Generates grid observation plans and planning plots for spherical sky grids."""

    def __init__(
        self,
        session_name: str,
        base_dir: str = "./data/mosaic",
        beam_fwhm_deg: float = 20.0,
        beam_sampling_fraction: float = 0.3333333333,
    ):
        self.session_name = session_name
        self.base_dir = base_dir
        self.beam_fwhm_deg = beam_fwhm_deg
        self.beam_sampling_fraction = beam_sampling_fraction

        self.session_timestamp = datetime.now(timezone.utc)
        self.session_id = self.session_timestamp.strftime("%Y%m%d-%H:%M:%S")

        self.output_dir = Path(base_dir) / f"{session_name}-{self.session_id}"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.csv_filepath = self.output_dir / "mosaic.csv"
        self.metadata_filepath = self.output_dir / "grid_metadata.json"

        self.log("Grid Generator initialized")
        self.log(f"Session: {session_name}")
        self.log(f"Session ID: {self.session_id}")
        self.log(f"Output directory: {self.output_dir}")
        self.log(f"CSV file: {self.csv_filepath}")

        self.csv_fieldnames = [
            "point_number",
            "point_id",
            "scan_order",
            "grid_row",
            "grid_col",
            "row",
            "column",
            "ra",
            "dec",
            "target_ra_hours",
            "target_dec_degrees",
            "target_ra_hms",
            "target_dec_dms",
            "center_distance_deg",
            "nominal_spacing_deg",
            "capture_status",
            "start_time",
            "end_time",
            "duration",
            "error_message",
            "data_filename",
            "session_name",
            "session_id",
        ]

        self._initialize_csv()

    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        print(f"[{timestamp}] [{level}] {message}")
        sys.stdout.flush()

    def _initialize_csv(self):
        try:
            with open(self.csv_filepath, "w", newline="") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=self.csv_fieldnames)
                writer.writeheader()
            self.log(f"CSV file created with {len(self.csv_fieldnames)} fields")
        except Exception as exc:
            self.log(f"Error creating CSV file: {exc}", "ERROR")
            raise

    def _ra_to_hms(self, ra_hours: float) -> str:
        ra_hours = normalize_ra_hours(ra_hours)
        hours = int(ra_hours)
        minutes_decimal = (ra_hours - hours) * 60
        minutes = int(minutes_decimal)
        seconds = (minutes_decimal - minutes) * 60
        return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"

    def _dec_to_dms(self, dec_degrees: float) -> str:
        sign = "+" if dec_degrees >= 0 else "-"
        dec_abs = abs(dec_degrees)
        degrees = int(dec_abs)
        arcmin_decimal = (dec_abs - degrees) * 60
        arcmin = int(arcmin_decimal)
        arcsec = (arcmin_decimal - arcmin) * 60
        return f"{sign}{degrees:02d}:{arcmin:02d}:{arcsec:05.2f}"

    def append_point(self, point_data: Dict):
        try:
            row = {field: point_data.get(field, "") for field in self.csv_fieldnames}
            with open(self.csv_filepath, "a", newline="") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=self.csv_fieldnames)
                writer.writerow(row)
        except Exception as exc:
            self.log(f"Error appending to CSV: {exc}", "ERROR")
            raise

    def _write_metadata(self, metadata: Dict):
        try:
            metadata_payload = {
                "session_name": self.session_name,
                "session_id": self.session_id,
                "grid": metadata,
            }
            metadata_payload["grid"].setdefault("projection", "tangent-plane")
            with open(self.metadata_filepath, "w", encoding="utf-8") as outfile:
                json.dump(metadata_payload, outfile, indent=2)
            self.log(f"Metadata file written: {self.metadata_filepath}")
        except Exception as exc:
            self.log(f"Error writing metadata JSON: {exc}", "WARNING")

    def _project_tangential(self, ra_deg: float, dec_deg: float, center_ra_deg: float, center_dec_deg: float) -> Tuple[float, float]:
        """Project sky coordinates to a tangent-plane view centered on the observation in degrees."""
        ra_rad = math.radians(ra_deg)
        dec_rad = math.radians(dec_deg)
        center_ra_rad = math.radians(center_ra_deg)
        center_dec_rad = math.radians(center_dec_deg)

        cos_dec = math.cos(dec_rad)
        sin_dec = math.sin(dec_rad)
        cos_center_dec = math.cos(center_dec_rad)
        sin_center_dec = math.sin(center_dec_rad)

        cos_delta_ra = math.cos(ra_rad - center_ra_rad)
        sin_delta_ra = math.sin(ra_rad - center_ra_rad)

        denominator = sin_dec * sin_center_dec + cos_dec * cos_center_dec * cos_delta_ra
        if denominator == 0:
            denominator = 1e-12

        x_deg = math.degrees(math.atan2(cos_dec * sin_delta_ra, denominator))
        y_deg = math.degrees(math.atan2(
            sin_dec * cos_center_dec - cos_dec * sin_center_dec * cos_delta_ra,
            denominator,
        ))

        return x_deg, y_deg

    def _beam_footprint_points(self, ra_deg: float, dec_deg: float, fwhm_deg: float, center_ra_deg: float, center_dec_deg: float, n_points: int = 60) -> List[Tuple[float, float]]:
        """Approximate the beam footprint in the tangent plane using degree-based angular coordinates."""
        radius_deg = fwhm_deg / 2.0
        angle_step = 2.0 * math.pi / n_points
        footprint = []
        for idx in range(n_points):
            ang = idx * angle_step
            x = radius_deg * math.cos(ang)
            y = radius_deg * math.sin(ang)
            footprint.append((x, y))
        return footprint

    def _build_plot_geometry(self, points: List[Dict], metadata: Dict) -> Dict:
        """Prepare plotting data for the plan and coverage views."""
        center_ra_deg = float(metadata["center_ra"]) * 15.0
        center_dec_deg = float(metadata["center_dec"])
        beam_fwhm_deg = float(metadata["beam_fwhm_deg"])

        projected_points = []
        for point in points:
            ra_deg = float(point["ra"]) * 15.0
            dec_deg = float(point["dec"])
            x, y = self._project_tangential(ra_deg, dec_deg, center_ra_deg, center_dec_deg)
            projected_points.append(
                {
                    **point,
                    "x_proj": x,
                    "y_proj": y,
                    "beam_footprint": self._beam_footprint_points(ra_deg, dec_deg, beam_fwhm_deg, center_ra_deg, center_dec_deg),
                }
            )

        span = max(float(metadata["width_deg"]), float(metadata["height_deg"])) * 1.1
        margin = span / 5.0
        x_vals = [p["x_proj"] for p in projected_points]
        y_vals = [p["y_proj"] for p in projected_points]
        x_min = min(x_vals) - margin
        x_max = max(x_vals) + margin
        y_min = min(y_vals) - margin
        y_max = max(y_vals) + margin

        return {
            "center_ra_deg": center_ra_deg,
            "center_dec_deg": center_dec_deg,
            "beam_fwhm_deg": beam_fwhm_deg,
            "projected_points": projected_points,
            "x_min": x_min,
            "x_max": x_max,
            "y_min": y_min,
            "y_max": y_max,
            "span_deg": span,
        }

    def build_plot_data(self, center_ra: float, center_dec: float, width_deg: float, height_deg: float) -> Tuple[List[Dict], Dict]:
        """Build points and metadata for plotting without writing files."""
        points, metadata = build_spherical_grid(
            center_ra_hours=center_ra,
            center_dec_deg=center_dec,
            width_deg=width_deg,
            height_deg=height_deg,
            beam_fwhm_deg=self.beam_fwhm_deg,
            beam_sampling_fraction=self.beam_sampling_fraction,
        )
        metadata.update(
            {
                "grid_center_ra": metadata["center_ra"],
                "grid_center_dec": metadata["center_dec"],
                "grid_width_deg": metadata["width_deg"],
                "grid_height_deg": metadata["height_deg"],
                "projection": "tangent-plane",
            }
        )
        return points, metadata

    def _build_equatorial_grid_overlay(self, center_ra_deg: float, center_dec_deg: float, span_deg: float) -> Dict:
        """Build an equatorial RA/DEC grid overlaid on the tangent-plane plot using Astropy frames."""
        center_coord = SkyCoord(ra=center_ra_deg * u.deg, dec=center_dec_deg * u.deg, frame="icrs")
        offset_frame = SkyOffsetFrame(origin=center_coord)

        if span_deg >= 30.0:
            ra_hours_step = 2.0
            dec_step_deg = 10.0
        else:
            ra_hours_step = 1.0
            dec_step_deg = 5.0

        meridians = []
        for ra_hours in np.arange(0.0, 24.0, ra_hours_step):
            ra_deg = ra_hours * 15.0
            dec_values = np.linspace(
                max(-90.0, center_dec_deg - span_deg * 0.7),
                min(90.0, center_dec_deg + span_deg * 0.7),
                140,
            )
            points = []
            for dec_deg in dec_values:
                sky_coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
                shifted = sky_coord.transform_to(offset_frame)
                points.append((shifted.lon.deg, shifted.lat.deg))
            meridians.append({"ra_hours": ra_hours, "points": points})

        parallels = []
        dec_values = np.arange(
            max(-90.0, center_dec_deg - span_deg * 0.7),
            min(90.0, center_dec_deg + span_deg * 0.7) + 1e-9,
            dec_step_deg,
        )
        for dec_deg in dec_values:
            if abs(dec_deg - center_dec_deg) > span_deg * 0.7 + 1e-9:
                continue
            ra_values = np.linspace(0.0, 360.0, 180)
            points = []
            for ra_deg in ra_values:
                sky_coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
                shifted = sky_coord.transform_to(offset_frame)
                points.append((shifted.lon.deg, shifted.lat.deg))
            parallels.append({"dec_deg": dec_deg, "points": points})

        return {"meridians": meridians, "parallels": parallels}

    def _draw_equatorial_overlay(self, ax, center_ra_deg: float, center_dec_deg: float, span_deg: float, show_labels: bool = True, alpha: float = 0.28, zorder: float = 1.0) -> None:
        """Draw a subtle equatorial RA/DEC overlay in the tangent-plane view."""
        overlay = self._build_equatorial_grid_overlay(center_ra_deg, center_dec_deg, span_deg)

        for meridian in overlay["meridians"]:
            xs = [p[0] for p in meridian["points"]]
            ys = [p[1] for p in meridian["points"]]
            ax.plot(xs, ys, color="#2563eb", linewidth=0.7, alpha=alpha, linestyle=(0, (2, 2)), zorder=zorder)

        for parallel in overlay["parallels"]:
            xs = [p[0] for p in parallel["points"]]
            ys = [p[1] for p in parallel["points"]]
            ax.plot(xs, ys, color="#f59e0b", linewidth=0.7, alpha=alpha, linestyle=(0, (2, 2)), zorder=zorder)

        if not show_labels:
            return

        meridian_stride = max(1, len(overlay["meridians"]) // 6)
        for idx, meridian in enumerate(overlay["meridians"]):
            if idx % meridian_stride != 0:
                continue
            p_idx = min(len(meridian["points"]) - 1, max(0, len(meridian["points"]) // 2))
            x_label, y_label = meridian["points"][p_idx]
            ra_hours = float(meridian["ra_hours"]) % 24.0
            label = f"{int(ra_hours)}h"
            ax.text(x_label, y_label, label, color="#1e3a8a", fontsize=7, ha="center", va="center", zorder=zorder + 1)

        parallel_stride = max(1, len(overlay["parallels"]) // 5)
        for idx, parallel in enumerate(overlay["parallels"]):
            if idx % parallel_stride != 0:
                continue
            p_idx = min(len(parallel["points"]) - 1, max(0, len(parallel["points"]) // 4))
            x_label, y_label = parallel["points"][p_idx]
            dec_label = f"{parallel['dec_deg']:+.0f}°"
            ax.text(x_label, y_label, dec_label, color="#92400e", fontsize=7, ha="center", va="center", zorder=zorder + 1)

    def _write_plot_images(self, points: List[Dict], metadata: Dict) -> None:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from matplotlib.colors import ListedColormap
            from matplotlib.patches import Circle

            self.log("Generating planning plots...")
            plot_data = self._build_plot_geometry(points, metadata)
            projected_points = plot_data["projected_points"]

            fig, ax = plt.subplots(figsize=(12, 10))
            ax.set_aspect("equal", adjustable="box")
            ax.set_facecolor("#f7f7f7")
            ax.set_title(f"Grid Plan - {self.session_name}", fontsize=14, fontweight="bold")
            ax.set_xlabel("Tangential X")
            ax.set_ylabel("Tangential Y")

            region_margin = max(float(metadata["width_deg"]), float(metadata["height_deg"])) / 2.0
            outline_x = [-region_margin, region_margin, region_margin, -region_margin, -region_margin]
            outline_y = [-region_margin, -region_margin, region_margin, region_margin, -region_margin]
            ax.plot(outline_x, outline_y, "r--", linewidth=1.5, alpha=0.7, label="Requested region")

            self._draw_equatorial_overlay(
                ax,
                plot_data["center_ra_deg"],
                plot_data["center_dec_deg"],
                plot_data["span_deg"],
                show_labels=True,
                alpha=0.22,
                zorder=1.0,
            )

            ax.text(
                0.02,
                0.94,
                "Equatorial grid overlay (RA/DEC)",
                transform=ax.transAxes,
                fontsize=8,
                color="#374151",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="white", alpha=0.85),
            )

            beam_radius = float(plot_data["beam_fwhm_deg"]) / 2.0
            beam_ref = Circle((0.0, 0.0), beam_radius, fill=False, edgecolor="#5b5b5b", linewidth=1.1, alpha=0.7, zorder=2)
            ax.add_patch(beam_ref)
            ax.text(0.0, beam_radius + 1.0, "Beam FWHM", ha="center", va="bottom", fontsize=8, color="#4b5563")

            for point in projected_points:
                x = point["x_proj"]
                y = point["y_proj"]
                ax.scatter([x], [y], s=60, color="#1f77b4", edgecolors="black", linewidth=0.4, zorder=3)
                ax.text(x, y, str(int(point["point_id"])), fontsize=7, ha="center", va="center", color="black")

            ordered_points = sorted(projected_points, key=lambda p: int(p["scan_order"]))
            xs = [p["x_proj"] for p in ordered_points]
            ys = [p["y_proj"] for p in ordered_points]
            ax.plot(xs, ys, color="#d62728", linewidth=1.2, alpha=0.8, label="Scan order")
            if ordered_points:
                ax.scatter([ordered_points[0]["x_proj"]], [ordered_points[0]["y_proj"]], s=140, color="green", marker="o", label="First")
                ax.scatter([ordered_points[-1]["x_proj"]], [ordered_points[-1]["y_proj"]], s=140, color="purple", marker="o", label="Last")

            ax.scatter([0.0], [0.0], s=180, color="red", marker="*", zorder=5, label="Center")
            ax.text(
                0.02,
                0.98,
                (
                    f"RA center: {metadata['center_ra']:.4f} h\n"
                    f"DEC center: {metadata['center_dec']:.4f}°\n"
                    f"Width: {metadata['width_deg']:.2f}°\n"
                    f"Height: {metadata['height_deg']:.2f}°\n"
                    f"Beam FWHM: {metadata['beam_fwhm_deg']:.2f}°\n"
                    f"Sampling: {metadata['beam_sampling_fraction']:.6f}\n"
                    f"Spacing: {metadata['nominal_spacing_deg']:.3f}°\n"
                    f"Rows/Cols: {metadata['rows']} / {metadata['columns']}\n"
                    f"Points: {metadata['total_points']}\n"
                    f"Scan: {metadata['scan_strategy']}"
                ),
                transform=ax.transAxes,
                fontsize=8,
                va="top",
                ha="left",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.85),
            )

            ax.grid(True, alpha=0.15)
            ax.legend(loc="upper right", fontsize=8)
            fig.tight_layout()
            plan_path = self.output_dir / "grid_plan.png"
            fig.savefig(plan_path, dpi=150, bbox_inches="tight")
            plt.close(fig)
            self.log(f"  Plan plot saved: {plan_path}")

            fig, ax = plt.subplots(figsize=(12, 10))
            ax.set_aspect("equal", adjustable="box")
            ax.set_facecolor("#f7f7f7")
            ax.set_title("Beam Coverage Count - Planning View", fontsize=14, fontweight="bold")
            ax.set_xlabel("Tangential X")
            ax.set_ylabel("Tangential Y")

            x_min = plot_data["x_min"]
            x_max = plot_data["x_max"]
            y_min = plot_data["y_min"]
            y_max = plot_data["y_max"]
            ax.set_xlim(x_min, x_max)
            ax.set_ylim(y_min, y_max)

            nx = 80
            ny = 80
            xs = np.linspace(x_min, x_max, nx)
            ys = np.linspace(y_min, y_max, ny)
            coverage = np.zeros((ny, nx), dtype=int)
            for point in projected_points:
                x = point["x_proj"]
                y = point["y_proj"]
                radius_deg = float(metadata["beam_fwhm_deg"]) / 2.0
                for iy, yy in enumerate(ys):
                    for ix, xx in enumerate(xs):
                        if (xx - x) ** 2 + (yy - y) ** 2 <= radius_deg ** 2:
                            coverage[iy, ix] += 1

            cmap = ListedColormap(["#f7f7f7", "#d0f0c0", "#7bc043", "#2b7a4b"])
            ax.imshow(
                coverage.T,
                extent=[x_min, x_max, y_min, y_max],
                origin="lower",
                cmap=cmap,
                alpha=0.78,
                aspect="equal",
                zorder=1,
            )
            self._draw_equatorial_overlay(
                ax,
                plot_data["center_ra_deg"],
                plot_data["center_dec_deg"],
                plot_data["span_deg"],
                show_labels=True,
                alpha=0.48,
                zorder=3.0,
            )
            ax.plot(outline_x, outline_y, "k--", linewidth=1.2, alpha=0.7, zorder=6, label="Requested region")
            ax.scatter([0.0], [0.0], s=140, color="red", marker="*", zorder=6)
            ax.text(0.02, 0.02, "SCP / center", transform=ax.transAxes, fontsize=8, color="#111827", bbox=dict(boxstyle="round,pad=0.2", facecolor="white", alpha=0.85), zorder=6)
            for point in projected_points:
                ax.scatter([point["x_proj"]], [point["y_proj"]], s=20, color="black", alpha=0.7, zorder=5)

            cbar = fig.colorbar(ax.images[0], ax=ax, pad=0.02)
            cbar.set_label("Beam coverage count")
            ax.text(0.02, 0.02, "Coverage count is geometric beam overlap, not scientific intensity", transform=ax.transAxes, fontsize=8, bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.85))
            fig.tight_layout()
            coverage_path = self.output_dir / "grid_coverage.png"
            fig.savefig(coverage_path, dpi=150, bbox_inches="tight")
            plt.close(fig)
            self.log(f"  Coverage plot saved: {coverage_path}")

        except ImportError:
            self.log("matplotlib or numpy not installed - skipping plot generation", "WARNING")
        except Exception as exc:
            self.log(f"Error generating plot: {exc}", "WARNING")

    def generate_grid_plan(
        self,
        center_ra: float,
        center_dec: float,
        width_deg: float,
        height_deg: float,
        num_points: Optional[int] = None,
    ) -> bool:
        self.log("=" * 80)
        self.log("GENERATING GRID OBSERVATION PLAN")
        self.log("=" * 80)

        if num_points is not None:
            self.log("Legacy --points value received; ignored in beam-based mode", "WARNING")

        points, metadata = build_spherical_grid(
            center_ra_hours=center_ra,
            center_dec_deg=center_dec,
            width_deg=width_deg,
            height_deg=height_deg,
            beam_fwhm_deg=self.beam_fwhm_deg,
            beam_sampling_fraction=self.beam_sampling_fraction,
        )

        nominal_spacing_deg = metadata["nominal_spacing_deg"]
        self.log(f"Center: RA={center_ra}h, DEC={center_dec} deg")
        self.log(f"Grid area: {width_deg} deg x {height_deg} deg")
        self.log(f"Beam FWHM: {self.beam_fwhm_deg:.6f} deg")
        self.log(f"Beam sampling fraction: {self.beam_sampling_fraction:.10f}")
        self.log(f"Nominal spacing: {nominal_spacing_deg:.6f} deg ({nominal_spacing_deg*60:.2f} arcmin)")
        self.log(f"Grid shape: {metadata['columns']} cols x {metadata['rows']} rows")
        self.log(f"Scan strategy: {metadata['scan_strategy']}")

        points_ra: List[float] = []
        points_dec: List[float] = []
        points_numbers: List[int] = []

        for point in points:
            scan_order = int(point["scan_order"])
            point_id = int(point["point_id"])
            ra_hours = float(point["target_ra_hours"])
            dec_deg = float(point["target_dec_degrees"])

            points_ra.append(ra_hours)
            points_dec.append(dec_deg)
            points_numbers.append(scan_order)

            point_data = {
                "point_number": scan_order,
                "point_id": point_id,
                "scan_order": scan_order,
                "grid_row": point["grid_row"],
                "grid_col": point["grid_col"],
                "row": point["row"],
                "column": point["column"],
                "ra": f"{ra_hours:.6f}",
                "dec": f"{dec_deg:.6f}",
                "target_ra_hours": f"{ra_hours:.6f}",
                "target_dec_degrees": f"{dec_deg:.6f}",
                "target_ra_hms": self._ra_to_hms(ra_hours),
                "target_dec_dms": self._dec_to_dms(dec_deg),
                "center_distance_deg": f"{point['center_distance_deg']:.6f}",
                "nominal_spacing_deg": f"{nominal_spacing_deg:.10f}",
                "capture_status": "planned",
                "start_time": "",
                "end_time": "",
                "duration": "",
                "error_message": "",
                "data_filename": f"{self.session_name}_{scan_order:04d}.dat",
                "session_name": self.session_name,
                "session_id": self.session_id,
            }
            self.append_point(point_data)

        self._write_metadata(metadata)
        self._write_plot_images(points, metadata)

        self.log("=" * 80)
        self.log("GRID PLAN GENERATED")
        self.log("=" * 80)
        self.log(f"Total points: {metadata['total_points']}")
        self.log(f"CSV file: {self.csv_filepath}")
        self.log(f"Metadata file: {self.metadata_filepath}")
        self.log(f"Plan plot file: {self.output_dir / 'grid_plan.png'}")
        self.log(f"Coverage plot file: {self.output_dir / 'grid_coverage.png'}")
        self.log("")
        self.log("Next steps:")
        self.log("  1. Review the CSV file")
        self.log("  2. Check the metadata JSON")
        self.log("  3. Use this CSV with your observation/capture script")
        self.log("")

        return True


def _load_beam_settings(config_path: Path) -> Tuple[float, float]:
    """Load beam parameters from observer config, with defaults."""
    default_beam = 20.0
    default_sampling = 0.3333333333

    if not config_path.exists():
        return default_beam, default_sampling

    with open(config_path, "r", encoding="utf-8") as infile:
        config = json.load(infile)

    defaults = config.get("observation_defaults", {})
    beam_fwhm_deg = float(defaults.get("beam_fwhm_deg", default_beam))
    beam_sampling_fraction = float(defaults.get("beam_sampling_fraction", default_sampling))
    return beam_fwhm_deg, beam_sampling_fraction


def main():
    parser = argparse.ArgumentParser(
        description="Grid Generator for Radio Astronomy - beam-based spherical plan (NO INDI needed)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --session cygnus_a --center-ra 19.99 --center-dec 40.73 --width 20 --height 20
  %(prog)s --session m51 --center-ra 13.5 --center-dec 47.2 --width 8.0 --height 6.0
  %(prog)s --session test --center-ra 23.9 --center-dec 0.0 --width 10 --height 10 --beam-fwhm 18 --beam-sampling 0.4

Spacing model:
  spacing_deg = beam_fwhm_deg * beam_sampling_fraction

Beam settings are loaded from observer_config.json by default and can be overridden
via CLI flags.
        """,
    )

    parser.add_argument("--session", required=True, help="Session name")
    parser.add_argument("--center-ra", type=float, required=True, help="Grid center RA in hours (0-24)")
    parser.add_argument("--center-dec", type=float, required=True, help="Grid center DEC in degrees (-90 to +90)")
    parser.add_argument("--width", type=float, required=True, help="Grid total width in degrees")
    parser.add_argument("--height", type=float, required=True, help="Grid total height in degrees")

    parser.add_argument("--points", type=int, default=None,
                        help="Legacy option (ignored): points are now derived from beam geometry")

    parser.add_argument("--config", default="observer_config.json", help="Path to observer config JSON")
    parser.add_argument("--beam-fwhm", type=float, default=None, help="Override beam FWHM in degrees")
    parser.add_argument("--beam-sampling", type=float, default=None, help="Override beam sampling fraction")

    parser.add_argument("--data-dir", default="./data/mosaic", help="Base directory for mosaic output")

    args = parser.parse_args()

    config_path = Path(args.config)
    beam_fwhm_deg, beam_sampling_fraction = _load_beam_settings(config_path)

    if args.beam_fwhm is not None:
        beam_fwhm_deg = args.beam_fwhm
    if args.beam_sampling is not None:
        beam_sampling_fraction = args.beam_sampling

    generator = GridGenerator(
        session_name=args.session,
        base_dir=args.data_dir,
        beam_fwhm_deg=beam_fwhm_deg,
        beam_sampling_fraction=beam_sampling_fraction,
    )

    try:
        success = generator.generate_grid_plan(
            center_ra=args.center_ra,
            center_dec=args.center_dec,
            width_deg=args.width,
            height_deg=args.height,
            num_points=args.points,
        )
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        generator.log("Operation cancelled by user", "WARNING")
        sys.exit(130)
    except Exception as exc:
        generator.log(f"Unexpected error: {exc}", "ERROR")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
