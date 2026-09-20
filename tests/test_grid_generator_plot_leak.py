#!/usr/bin/env python3
"""Regression tests for two related matplotlib bugs in
GridGenerator._write_plot_images() found and fixed on 2026-09-02.

BUG A — figure leak: fig.savefig() and plt.close(fig) used to be sequential
statements inside one try/except shared by both the grid-plan and
grid-coverage figures. Any exception raised by savefig() skipped the
close() call, permanently registering that Figure in pyplot's global figure
manager. Harmless for a short-lived CLI process; a real per-request leak in
the resident almita-observe-api.service, which accumulated to 5.4 GiB RSS
after 4 PLAN requests and was killed by the kernel OOM-killer
(2026-09-02T15:25:44Z, PID 229658, anon-rss 5658176kB).
Fix: each figure now gets its own try/finally with plt.close(fig)
unconditional in finally.

BUG B — oversized tight-bbox canvas: _draw_equatorial_overlay() draws RA/DEC
grid lines that sweep a broad sky swath (parallels cover the full 360 deg of
RA) and picked an arbitrary point along each line for its label, with no
regard for whether that point falls inside the actual plotting field. The
line/label data was still visually clipped correctly by the axes viewport,
but matplotlib's bbox_inches="tight" sizing (used by savefig) includes each
artist's FULL, unclipped data extent — so a label or line point ~180 deg
away from the field center could make savefig() compute a canvas hundreds
of inches across. For a real 3x3/1.5deg-spacing FIXED_CENTER plan this
exceeded matplotlib's 65536px format limit outright, and while allocating
the (failed) raster buffer drove RSS to 5.3 GiB in a SINGLE request,
independent of and worse than BUG A — the kernel OOM-killed the resident
API service on the very first PLAN after a fresh restart
(2026-09-02T15:51:07Z, PID 240423, anon-rss 5315940kB).
Fix: _draw_equatorial_overlay() now accepts an optional view_bounds
(x_min, x_max, y_min, y_max) and crops each line's points (and the point
used for its label) to that box (with a small margin) before plotting —
identical visible render (anything outside view_bounds was never drawn
anyway), but a bounded tight-bbox.

None of these tests render a real oversized canvas — BUG A's tests mock
savefig() entirely; BUG B's tests either inspect geometry/tight-bbox
directly (cheap, vector-only) or run the real (now-bounded) pipeline.
"""
import tempfile
import unittest
from unittest.mock import patch

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from grid_generator import GridGenerator, build_spherical_grid

# Same center/spacing as the real ALMITA-WEB-SMALL-RUN-01 validation —
# the exact geometry that triggered BUG B in production.
_SPEC = dict(center_ra=11.830, center_dec=-33.4489, width_deg=3.0, height_deg=3.0)


def _new_generator(tmpdir, name="LEAK-TEST"):
    return GridGenerator(
        session_name=name,
        base_dir=tmpdir,
        beam_fwhm_deg=1.5,
        beam_sampling_fraction=1.0,
    )


class TestPlotFigureCleanup(unittest.TestCase):

    def setUp(self):
        self._baseline = set(plt.get_fignums())

    def tearDown(self):
        for n in self._leaked():
            plt.close(n)

    def _leaked(self):
        return set(plt.get_fignums()) - self._baseline

    def test_success_closes_both_figures(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(Figure, "savefig"):
            gen = _new_generator(tmpdir)
            gen.generate_grid_plan(**_SPEC)
        self.assertEqual(self._leaked(), set(),
                          "successful PLAN generation must leave zero open figures")

    def test_savefig_failure_still_closes_figure(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(Figure, "savefig", side_effect=RuntimeError("boom")):
            gen = _new_generator(tmpdir)
            # Existing behavior: the method swallows the exception and logs
            # a WARNING — it must not raise.
            gen.generate_grid_plan(**_SPEC)
        self.assertEqual(self._leaked(), set(),
                          "a failed savefig() must not leave an orphaned figure")

    def test_repeated_success_does_not_accumulate_figures(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(Figure, "savefig"):
            for i in range(5):
                gen = _new_generator(tmpdir, name=f"LEAK-OK-{i}")
                gen.generate_grid_plan(**_SPEC)
                self.assertEqual(self._leaked(), set(),
                                  f"figure count grew after success iteration {i}")

    def test_repeated_failures_do_not_accumulate_figures(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(Figure, "savefig", side_effect=RuntimeError("boom")):
            for i in range(5):
                gen = _new_generator(tmpdir, name=f"LEAK-FAIL-{i}")
                gen.generate_grid_plan(**_SPEC)
                self.assertEqual(self._leaked(), set(),
                                  f"figure count grew after failing iteration {i}")

    def test_plan_and_coverage_figures_each_independently_closed(self):
        """Fail only the SECOND savefig call (the coverage plot) — proves
        each figure has its own try/finally rather than one shared one
        (which would either leak the first figure too, or mask the bug by
        never reaching the second figure's close on some other path)."""
        calls = {"n": 0}

        def flaky(self_fig, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom on coverage plot only")

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(Figure, "savefig", flaky):
            gen = _new_generator(tmpdir)
            gen.generate_grid_plan(**_SPEC)
        self.assertEqual(calls["n"], 2, "expected exactly one savefig call per figure")
        self.assertEqual(self._leaked(), set(),
                          "coverage-plot failure must not leak the plan-plot figure or itself")

    def test_realistic_oversized_canvas_exception_without_allocating_it(self):
        """Simulates the exact production failure (matplotlib's own error
        for a canvas exceeding 2**16 px) via a mock — never actually renders
        a monster image, so this test itself can't pressure memory."""
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            Figure, "savefig",
            side_effect=ValueError(
                "Image size of 72316x18508 pixels is too large. "
                "It must be less than 2^16 in each direction."
            ),
        ):
            gen = _new_generator(tmpdir)
            gen.generate_grid_plan(**_SPEC)
        self.assertEqual(self._leaked(), set(),
                          "the real production exception message must not leak a figure either")


# A generously large "field of view" used only to prove the *unclipped*
# legacy behavior in test_without_view_bounds_preserves_legacy_behavior —
# real callers always pass the actual small plotting field.
_TINY_VIEW = (-2.16, 2.16, -2.16, 2.16)  # the real small-run's own x/y bounds


class TestEqualatorialOverlayBoundedTightBbox(unittest.TestCase):
    """BUG B: overlay lines/labels used to carry their full, unclipped
    (up to ~180 deg away) data extent into savefig's bbox_inches="tight"
    sizing. These tests cover the view_bounds cropping fix directly."""

    def setUp(self):
        self.gen = GridGenerator(session_name="BBOX-TEST", base_dir=tempfile.mkdtemp(),
                                  beam_fwhm_deg=1.5, beam_sampling_fraction=1.0)
        self._baseline = set(plt.get_fignums())

    def tearDown(self):
        for n in set(plt.get_fignums()) - self._baseline:
            plt.close(n)

    def test_view_bounds_crops_every_line_and_label_to_the_field(self):
        fig, ax = plt.subplots(figsize=(4, 4))
        x_lo, x_hi, y_lo, y_hi = _TINY_VIEW
        pad_x, pad_y = (x_hi - x_lo) * 0.05, (y_hi - y_lo) * 0.05
        self.gen._draw_equatorial_overlay(
            ax, center_ra_deg=11.830 * 15.0, center_dec_deg=-33.4489, span_deg=3.3,
            show_labels=True, view_bounds=_TINY_VIEW,
        )
        for t in ax.texts:
            x, y = t.get_position()
            self.assertTrue(x_lo - pad_x <= x <= x_hi + pad_x, f"label {t.get_text()!r} x={x} outside view_bounds")
            self.assertTrue(y_lo - pad_y <= y <= y_hi + pad_y, f"label {t.get_text()!r} y={y} outside view_bounds")
        for ln in ax.lines:
            xdata, ydata = ln.get_xdata(), ln.get_ydata()
            if len(xdata) == 0:
                continue
            self.assertTrue(max(xdata) <= x_hi + pad_x and min(xdata) >= x_lo - pad_x,
                             f"line x-range {min(xdata)}..{max(xdata)} exceeds view_bounds")
            self.assertTrue(max(ydata) <= y_hi + pad_y and min(ydata) >= y_lo - pad_y,
                             f"line y-range {min(ydata)}..{max(ydata)} exceeds view_bounds")
        plt.close(fig)

    def test_without_view_bounds_preserves_legacy_unclipped_behavior(self):
        """Backward compatibility: any caller that doesn't pass view_bounds
        (view_bounds=None, the default) must still get the pre-fix,
        unclipped behavior — proves the fix is opt-in via the parameter,
        not a silent global behavior change."""
        fig, ax = plt.subplots(figsize=(4, 4))
        self.gen._draw_equatorial_overlay(
            ax, center_ra_deg=11.830 * 15.0, center_dec_deg=-33.4489, span_deg=3.3,
            show_labels=True, view_bounds=None,
        )
        far_points = [t.get_position() for t in ax.texts if abs(t.get_position()[0]) > 50 or abs(t.get_position()[1]) > 50]
        self.assertTrue(far_points, "expected at least one far-away (unclipped) label when view_bounds is None")
        plt.close(fig)

    def test_known_trigger_geometry_produces_bounded_tight_bbox(self):
        """Direct regression test for the exact production incident: the
        real ALMITA-WEB-SMALL-RUN-01 geometry (RA=11.83h, DEC=-33.4489,
        3x3, spacing 1.5deg) used to produce a ~482x123 inch tight bbox
        (72287x18444 px at dpi=150). Reproduces the coverage-plot path up
        to (not including) savefig — get_tightbbox() is cheap vector
        geometry, no raster buffer is ever allocated here."""
        points, metadata = build_spherical_grid(
            center_ra_hours=_SPEC["center_ra"], center_dec_deg=_SPEC["center_dec"],
            width_deg=_SPEC["width_deg"], height_deg=_SPEC["height_deg"],
            beam_fwhm_deg=1.5, beam_sampling_fraction=1.0,
        )
        plot_data = self.gen._build_plot_geometry(points, metadata)
        x_min, x_max = plot_data["x_min"], plot_data["x_max"]
        y_min, y_max = plot_data["y_min"], plot_data["y_max"]

        fig, ax = plt.subplots(figsize=(12, 10))
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlim(x_min, x_max)
        ax.set_ylim(y_min, y_max)
        self.gen._draw_equatorial_overlay(
            ax, plot_data["center_ra_deg"], plot_data["center_dec_deg"], plot_data["span_deg"],
            show_labels=True, view_bounds=(x_min, x_max, y_min, y_max),
        )
        fig.canvas.draw()
        tbb = fig.get_tightbbox()
        plt.close(fig)

        # Pre-fix this was ~482 x 123 inches (72287 x 18444 px @ dpi=150).
        # A generous 40in ceiling is still ~10x the nominal 12x10in figure —
        # comfortably below matplotlib's 65536px/dpi=150 => ~437in limit —
        # while leaving headroom for legitimate legend/label growth.
        self.assertLess(tbb.width, 40.0, f"tight bbox width {tbb.width:.1f}in — BUG B may have regressed")
        self.assertLess(tbb.height, 40.0, f"tight bbox height {tbb.height:.1f}in — BUG B may have regressed")

    def test_known_trigger_geometry_real_savefig_succeeds_with_no_warning(self):
        """End-to-end: run the REAL pipeline (real savefig, not mocked) for
        the exact production-incident geometry and confirm both PNGs are
        written with sane dimensions and matplotlib raises no
        'Tight layout not applied' warning (the visible symptom that always
        accompanied BUG B, including in the successful-but-ugly 400-point
        run). Now that the bbox is bounded this is safe to actually render."""
        import warnings
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmpdir:
            gen = GridGenerator(session_name="BBOX-E2E", base_dir=tmpdir,
                                 beam_fwhm_deg=1.5, beam_sampling_fraction=1.0)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                gen.generate_grid_plan(**_SPEC)
            tight_layout_warnings = [w for w in caught if "Tight layout not applied" in str(w.message)]
            self.assertEqual(tight_layout_warnings, [],
                              "the known BUG B trigger geometry must no longer produce this warning")

            plan_png = gen.output_dir / "grid_plan.png"
            coverage_png = gen.output_dir / "grid_coverage.png"
            self.assertTrue(plan_png.exists() and coverage_png.exists())
            for png in (plan_png, coverage_png):
                w, h = Image.open(png).size
                self.assertLess(w, 6000, f"{png.name} width {w}px — unexpectedly large")
                self.assertLess(h, 6000, f"{png.name} height {h}px — unexpectedly large")
        self.assertEqual(set(plt.get_fignums()) - self._baseline, set())


if __name__ == "__main__":
    unittest.main()
