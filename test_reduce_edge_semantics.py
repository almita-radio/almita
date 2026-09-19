"""EDGE SEMANTICS (2nd-pass sections 31-35): single-capture point,
mixed-configuration point, duplicate capture, out-of-order input, and
filesystem determinism.

Scope note made explicit here (see docs/REDUCE_V1_FREEZE.md's known
limitations): REDUCE V1's real INGEST model is 1 capture per point,
matching every real OBSERVE campaign audited (mosaic.csv's point_number
is unique per row in every campaign inspected). Sections 32/33
("mixed configuration point", "duplicate capture") therefore cannot be
exercised end-to-end through pipeline.reduce_point today - they are
exercised at the AVERAGING primitive itself (reduce_engine.averaging.
stack_spectra), which IS the reusable function a future multi-capture-
per-point INGEST would call, and which is where these gaps genuinely
live.
"""
import csv
import json

import numpy as np
import pytest

from reduce_engine.averaging import stack_spectra
from reduce_engine.ingest import discover_campaign
from reduce_engine.models import MaskFlag

GOOD = MaskFlag.GOOD.value


# ---------------------------------------------------------------- 31. single-capture point

def test_single_capture_point_n_contributing_is_honestly_one():
    v = np.array([1.0, 2.0, 3.0])
    u = np.array([0.1, 0.1, 0.1])
    m = np.array([GOOD, GOOD, GOOD])
    result = stack_spectra([v], [u], [m])
    assert np.all(result.n_contributing == 1)
    # uncertainty must be the single capture's own uncertainty (inverse
    # variance of ONE term is just that term) - never artificially
    # shrunk by pretending averaging happened.
    np.testing.assert_allclose(result.uncertainty, u)


# ---------------------------------------------------------------- 32. mixed configuration ("same point")

def test_stack_spectra_has_no_built_in_compatibility_gate_documented_gap():
    """Real, documented gap: stack_spectra combines whatever arrays it is
    given - it does not know or check whether they came from captures
    with different gain/sample_rate/center_frequency. Two very
    different-amplitude "captures" (as if from mismatched gain) get
    blindly averaged today. This is acceptable ONLY because no real
    ingest path currently feeds multi-capture-per-point data into it;
    the moment one does, a compatibility gate must be added upstream of
    this call (see docs/REDUCE_V1_FREEZE.md known limitations) - this
    test exists so that gap stays visible instead of being silently
    forgotten.
    """
    normal = np.full(10, 1.0)
    mismatched_gain = np.full(10, 100.0)   # as if captured with wildly different gain
    u = np.full(10, 0.01)
    m = np.full(10, GOOD, dtype=np.int64)
    result = stack_spectra([normal, mismatched_gain], [u, u], [m, m])
    # Confirms the CURRENT (gap) behavior precisely, so a future fix is a
    # deliberate, visible change to this test - not a silent regression.
    assert np.all(result.n_contributing == 2)
    assert np.all(result.value > 1.0)  # the mismatched capture DID pull the average up


# ---------------------------------------------------------------- 33. duplicate capture

def test_stack_spectra_has_no_duplicate_capture_protection_documented_gap():
    """Real, documented gap: feeding the SAME array/uncertainty pair
    twice inflates n_contributing and shrinks uncertainty exactly as if
    two independent captures existed - stack_spectra has no identity
    awareness (it only sees arrays, never a CaptureRef/sha256).
    Deduplication by CaptureRef.sha256 is therefore a responsibility of
    whatever assembles the per-point capture list BEFORE calling
    stack_spectra - documented in docs/REDUCE_V1_FREEZE.md, not
    implemented here since no real ingest path can produce a duplicate
    today (each point maps to exactly one resolved file).
    """
    v = np.full(10, 1.0)
    u = np.full(10, 0.1)
    m = np.full(10, GOOD, dtype=np.int64)
    once = stack_spectra([v], [u], [m])
    duplicated = stack_spectra([v, v], [u, u], [m, m])
    assert np.all(duplicated.n_contributing == 2)
    assert np.all(duplicated.uncertainty < once.uncertainty), \
        "duplicating a capture measurably (and wrongly, if unnoticed) shrinks reported uncertainty"


# ---------------------------------------------------------------- 34/35. ordering and filesystem determinism

def test_out_of_order_captures_stack_to_the_same_result_regardless_of_order():
    rng = np.random.default_rng(9)
    values = [1.0 + 0.1 * rng.standard_normal(20) for _ in range(5)]
    uncertainties = [np.full(20, 0.1)] * 5
    masks = [np.full(20, GOOD, dtype=np.int64)] * 5
    forward = stack_spectra(values, uncertainties, masks)
    backward = stack_spectra(list(reversed(values)), list(reversed(uncertainties)), list(reversed(masks)))
    np.testing.assert_allclose(forward.value, backward.value)
    np.testing.assert_allclose(forward.uncertainty, backward.uncertainty)


def test_ingest_point_order_follows_mosaic_csv_not_filesystem_listing(tmp_path):
    """discover_campaign must never depend on os.listdir()'s arbitrary
    order - it iterates mosaic.csv's own row order (already covered by
    test_reduce_adversarial.py's out-of-order-timestamps test at the CSV
    level); this test additionally confirms repeated discovery calls
    against the SAME real campaign are 100% stable."""
    small = "data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16"
    import pathlib
    if not pathlib.Path(small).is_dir():
        pytest.skip("real fixture campaign not present")
    orders = [[p.point_index for p in discover_campaign(small).points] for _ in range(5)]
    assert all(order == orders[0] for order in orders)
