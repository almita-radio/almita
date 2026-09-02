#!/usr/bin/env python3
"""Regression tests for the matplotlib figure-leak fix in
GridGenerator._write_plot_images() (2026-09-02).

Root cause: fig.savefig() and plt.close(fig) used to be sequential
statements inside one try/except shared by both the grid-plan and
grid-coverage figures. Any exception raised by savefig() — confirmed in
production to happen when the coverage plot's bbox_inches="tight"
computation blows past matplotlib's 2**16 px canvas limit — skipped the
close() call, permanently registering that Figure in pyplot's global figure
manager. Harmless for a short-lived CLI process; a real per-request leak in
the resident almita-observe-api.service, which accumulated to 5.4 GiB RSS
after 4 PLAN requests and was killed by the kernel OOM-killer
(2026-09-02T15:25:44Z, PID 229658, anon-rss 5658176kB).

Fix: each figure now gets its own try/finally with plt.close(fig)
unconditional in finally. None of these tests render a real (potentially
oversized) canvas — savefig() is always mocked, so a failure-path test can
never itself cause memory pressure.
"""
import tempfile
import unittest
from unittest.mock import patch

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from grid_generator import GridGenerator

# Same center/spacing as the real ALMITA-WEB-SMALL-RUN-01 validation.
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


if __name__ == "__main__":
    unittest.main()
