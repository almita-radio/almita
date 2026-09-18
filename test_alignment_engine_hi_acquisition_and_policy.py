"""Fase 24: acquisition interface + FIRST_LIGHT sync policy."""
import asyncio

import pytest
from astropy.coordinates import SkyCoord

from alignment_engine.hi.acquisition import (
    RealHIAcquisitionBackend,
    SimulatedHIAcquisitionBackend,
    acquire_and_reduce_point,
)
from alignment_engine.hi.sync_policy import (
    AlignmentPhase,
    PastSolution,
    check_repeatability,
    evaluate_phase_gate,
)

POINT = SkyCoord(ra=180.0, dec=-40.0, unit="deg")


def test_real_backend_refuses_construction():
    with pytest.raises(NotImplementedError):
        RealHIAcquisitionBackend()


def test_simulated_backend_produces_a_valid_metric_for_a_real_signal():
    backend = SimulatedHIAcquisitionBackend(expected_amplitude_by_index=[5.0], noise_std=0.02)
    metric = asyncio.run(acquire_and_reduce_point(
        backend, POINT, center_frequency_hz=1_420_405_751.77, sample_rate_hz=2_400_000.0,
        gain_db=40.2, integration_seconds=5.0))
    assert metric is not None
    assert metric > 0


def test_simulated_backend_advances_index_per_call():
    backend = SimulatedHIAcquisitionBackend(expected_amplitude_by_index=[1.0, 5.0, 1.0], noise_std=0.01)
    metrics = [asyncio.run(acquire_and_reduce_point(
        backend, POINT, center_frequency_hz=1_420_405_751.77, sample_rate_hz=2_400_000.0,
        gain_db=40.2, integration_seconds=5.0)) for _ in range(3)]
    assert all(m is not None for m in metrics)
    assert metrics[1] > metrics[0]
    assert metrics[1] > metrics[2]


def test_missing_channels_can_make_metric_none_not_a_fabricated_zero():
    backend = SimulatedHIAcquisitionBackend(expected_amplitude_by_index=[1.0], noise_std=0.02,
                                             missing_channel_fraction=0.98)
    metric = asyncio.run(acquire_and_reduce_point(
        backend, POINT, center_frequency_hz=1_420_405_751.77, sample_rate_hz=2_400_000.0,
        gain_db=40.2, integration_seconds=5.0))
    assert metric is None


def test_first_light_phase_always_blocks_sync_even_when_eligible_and_repeatable():
    repeatability = check_repeatability(
        PastSolution("s2", "target", 1.0, 1.0, 0.1, 0.1),
        [PastSolution("s1", "target", 1.05, 0.95, 0.1, 0.1)])
    assert repeatability.satisfied
    result = evaluate_phase_gate(AlignmentPhase.FIRST_LIGHT_HI, "ELIGIBLE", repeatability)
    assert result.sync_allowed is False
    assert "FIRST_LIGHT_HI" in result.reason


def test_established_phase_without_repeatability_evidence_blocks_sync():
    result = evaluate_phase_gate(AlignmentPhase.ESTABLISHED_HI, "ELIGIBLE", repeatability=None)
    assert result.sync_allowed is False


def test_established_phase_with_eligible_and_repeatable_allows_sync():
    repeatability = check_repeatability(
        PastSolution("s2", "target", 1.0, 1.0, 0.1, 0.1),
        [PastSolution("s1", "target", 1.05, 0.95, 0.1, 0.1)])
    result = evaluate_phase_gate(AlignmentPhase.ESTABLISHED_HI, "ELIGIBLE", repeatability)
    assert result.sync_allowed is True


def test_repeatability_fails_with_only_one_solution():
    result = check_repeatability(PastSolution("s1", "target", 1.0, 1.0, 0.1, 0.1), [])
    assert result.satisfied is False


def test_repeatability_fails_when_solutions_disagree():
    result = check_repeatability(
        PastSolution("s2", "target", 5.0, 5.0, 0.1, 0.1),
        [PastSolution("s1", "target", 0.0, 0.0, 0.1, 0.1)])
    assert result.satisfied is False


def test_evaluate_phase_gate_has_no_override_parameter():
    import inspect
    sig = inspect.signature(evaluate_phase_gate)
    for name in sig.parameters:
        assert "override" not in name.lower()
        assert "force" not in name.lower()
