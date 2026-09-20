#!/usr/bin/env python3
"""Tests for spherical beam-based grid generation."""

import math
import tempfile
import unittest
from pathlib import Path

import astropy.units as u
from astropy.coordinates import SkyCoord

from grid_generator import (
    GridGenerator,
    build_spherical_grid,
    calculate_grid_shape,
    compute_nominal_spacing_deg,
)


def _coord_from_point(point):
    return SkyCoord(
        ra=float(point["target_ra_hours"]) * u.hourangle,
        dec=float(point["target_dec_degrees"]) * u.deg,
        frame="icrs",
    )


def _row_col_map(points):
    return {(int(p["row"]), int(p["column"])): p for p in points}


def _angular_sep_deg(p1, p2):
    return _coord_from_point(p1).separation(_coord_from_point(p2)).deg


class TestGridDistribution(unittest.TestCase):

    def test_nominal_spacing_from_beam(self):
        spacing = compute_nominal_spacing_deg(20.0, 0.3333333333)
        self.assertAlmostEqual(spacing, 6.666666666, places=6)

    def test_grid_shape_from_geometry(self):
        rows, cols = calculate_grid_shape(20.0, 10.0, 6.666666666)
        self.assertEqual(rows, 3)
        self.assertEqual(cols, 4)

    def test_center_near_equator(self):
        points, metadata = build_spherical_grid(
            center_ra_hours=10.0,
            center_dec_deg=0.0,
            width_deg=20.0,
            height_deg=20.0,
            beam_fwhm_deg=20.0,
            beam_sampling_fraction=0.3333333333,
        )
        self.assertGreater(len(points), 0)
        self.assertEqual(metadata["scan_strategy"], "serpentine")

        point_ids = [int(p["point_id"]) for p in points]
        self.assertEqual(len(point_ids), len(set(point_ids)))

    def test_center_high_declination(self):
        points, metadata = build_spherical_grid(
            center_ra_hours=5.0,
            center_dec_deg=70.0,
            width_deg=20.0,
            height_deg=20.0,
            beam_fwhm_deg=20.0,
            beam_sampling_fraction=0.3333333333,
        )
        self.assertEqual(len(points), metadata["total_points"])
        self.assertTrue(all(-90.0 <= float(p["target_dec_degrees"]) <= 90.0 for p in points))

    def test_ra_wrap_crossing(self):
        points, _ = build_spherical_grid(
            center_ra_hours=23.95,
            center_dec_deg=0.0,
            width_deg=30.0,
            height_deg=10.0,
            beam_fwhm_deg=20.0,
            beam_sampling_fraction=0.3333333333,
        )
        ras = [float(p["target_ra_hours"]) for p in points]
        self.assertTrue(any(ra < 1.0 for ra in ras))
        self.assertTrue(any(ra > 23.0 for ra in ras))

    def test_serpentine_order(self):
        points, metadata = build_spherical_grid(
            center_ra_hours=8.0,
            center_dec_deg=10.0,
            width_deg=20.0,
            height_deg=20.0,
            beam_fwhm_deg=20.0,
            beam_sampling_fraction=0.3333333333,
        )
        cols = metadata["columns"]

        first_row = [p for p in points if int(p["row"]) == 0]
        second_row = [p for p in points if int(p["row"]) == 1]

        self.assertEqual([int(p["column"]) for p in first_row], list(range(cols)))
        self.assertEqual([int(p["column"]) for p in second_row], list(range(cols - 1, -1, -1)))

        scan_orders = [int(p["scan_order"]) for p in points]
        self.assertEqual(scan_orders, list(range(1, len(points) + 1)))

    def test_stable_point_ids(self):
        points, metadata = build_spherical_grid(
            center_ra_hours=9.0,
            center_dec_deg=5.0,
            width_deg=20.0,
            height_deg=20.0,
            beam_fwhm_deg=20.0,
            beam_sampling_fraction=0.3333333333,
        )
        row_col_to_id = {
            (int(p["row"]), int(p["column"])): int(p["point_id"]) for p in points
        }
        cols = metadata["columns"]

        for row in range(metadata["rows"]):
            for col in range(cols):
                expected = row * cols + col + 1
                self.assertEqual(row_col_to_id[(row, col)], expected)

    def test_no_duplicate_points(self):
        points, _ = build_spherical_grid(
            center_ra_hours=12.0,
            center_dec_deg=30.0,
            width_deg=20.0,
            height_deg=20.0,
            beam_fwhm_deg=20.0,
            beam_sampling_fraction=0.3333333333,
        )

        seen = set()
        for p in points:
            key = (round(float(p["target_ra_hours"]), 10), round(float(p["target_dec_degrees"]), 10))
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_neighbor_separation_reasonable(self):
        points, metadata = build_spherical_grid(
            center_ra_hours=10.0,
            center_dec_deg=20.0,
            width_deg=20.0,
            height_deg=20.0,
            beam_fwhm_deg=20.0,
            beam_sampling_fraction=0.3333333333,
        )

        rowcol = _row_col_map(points)
        rows = metadata["rows"]
        cols = metadata["columns"]
        target = metadata["nominal_spacing_deg"]

        horizontal = []
        vertical = []
        for row in range(rows):
            for col in range(cols):
                if col + 1 < cols:
                    horizontal.append(_angular_sep_deg(rowcol[(row, col)], rowcol[(row, col + 1)]))
                if row + 1 < rows:
                    vertical.append(_angular_sep_deg(rowcol[(row, col)], rowcol[(row + 1, col)]))

        self.assertTrue(horizontal and vertical)
        mean_h = sum(horizontal) / len(horizontal)
        mean_v = sum(vertical) / len(vertical)

        self.assertLess(abs(mean_h - target), 2.0)
        self.assertLess(abs(mean_v - target), 2.0)

    def test_grid_plan_pngs_are_generated_and_non_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generator = GridGenerator(
                session_name="plot-test",
                base_dir=tmpdir,
                beam_fwhm_deg=20.0,
                beam_sampling_fraction=0.3333333333,
            )
            success = generator.generate_grid_plan(
                center_ra=10.0,
                center_dec=0.0,
                width_deg=20.0,
                height_deg=20.0,
            )

            self.assertTrue(success)
            plan_path = Path(generator.output_dir) / "grid_plan.png"
            coverage_path = Path(generator.output_dir) / "grid_coverage.png"
            self.assertTrue(plan_path.exists())
            self.assertTrue(coverage_path.exists())
            self.assertGreater(plan_path.stat().st_size, 0)
            self.assertGreater(coverage_path.stat().st_size, 0)

    def test_plotting_order_matches_serpentine_scan_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generator = GridGenerator(
                session_name="plot-order",
                base_dir=tmpdir,
                beam_fwhm_deg=20.0,
                beam_sampling_fraction=0.3333333333,
            )
            points, metadata = generator.build_plot_data(
                center_ra=8.0,
                center_dec=10.0,
                width_deg=20.0,
                height_deg=20.0,
            )

            scan_orders = [int(p["scan_order"]) for p in points]
            self.assertEqual(scan_orders, list(range(1, len(points) + 1)))
            self.assertEqual(len(points), metadata["total_points"])

    def test_high_declination_plotting_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generator = GridGenerator(
                session_name="plot-high-dec",
                base_dir=tmpdir,
                beam_fwhm_deg=20.0,
                beam_sampling_fraction=0.3333333333,
            )
            points, metadata = generator.build_plot_data(
                center_ra=5.0,
                center_dec=70.0,
                width_deg=20.0,
                height_deg=20.0,
            )
            self.assertGreater(len(points), 0)
            self.assertEqual(metadata["scan_strategy"], "serpentine")

    def test_scp_projection_uses_degree_scale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generator = GridGenerator(
                session_name="plot-scp",
                base_dir=tmpdir,
                beam_fwhm_deg=20.0,
                beam_sampling_fraction=0.3333333333,
            )
            points, metadata = generator.build_plot_data(
                center_ra=0.0,
                center_dec=-90.0,
                width_deg=40.0,
                height_deg=40.0,
            )
            plot_data = generator._build_plot_geometry(points, metadata)
            projected_points = plot_data["projected_points"]
            x_vals = [p["x_proj"] for p in projected_points]
            y_vals = [p["y_proj"] for p in projected_points]

            center_point = next(p for p in projected_points if int(p["row"]) == 3 and int(p["column"]) == 3)
            self.assertAlmostEqual(center_point["x_proj"], 0.0, delta=1.0)
            self.assertAlmostEqual(center_point["y_proj"], 0.0, delta=1.0)
            self.assertLess(abs(min(x_vals) + 20.0), 5.0)
            self.assertGreater(abs(max(x_vals)), 15.0)
            self.assertLess(abs(min(y_vals) + 20.0), 5.0)
            self.assertGreater(abs(max(y_vals)), 15.0)

            coverage = []
            for point in projected_points:
                coverage.append((point["x_proj"], point["y_proj"], point["beam_footprint"]))

            max_count = 0
            for point in projected_points:
                x0 = point["x_proj"]
                y0 = point["y_proj"]
                radius = 10.0
                count = 0
                for other in projected_points:
                    dx = other["x_proj"] - x0
                    dy = other["y_proj"] - y0
                    if dx * dx + dy * dy <= radius * radius:
                        count += 1
                max_count = max(max_count, count)

            self.assertLess(max_count, 20)


if __name__ == "__main__":
    unittest.main()
