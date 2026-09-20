"""Gain sweep: plan construction, synthetic sweep with KNOWN ground truth
(Fase 72 - low/mid/near-clip/clipped), linearity analysis, and the
recommendation engine's explicit "not just highest gain" requirement
(Fase 41)."""
import pytest

from calibration_engine.clipping import ClippingStatus
from calibration_engine.gain_sweep import (
    CANDIDATE_GAIN_TABLE_DB, build_gain_sweep_plan, compute_gain_step_result,
    analyze_gain_linearity, recommend_operational_gain,
)
from calibration_engine.simulation import InstrumentSimulationConfig, simulate_capture_iq


def test_gain_table_is_the_real_documented_r820t2_table_size():
    # 32 discrete steps - the actual published table size, not a guess.
    assert len(CANDIDATE_GAIN_TABLE_DB) == 32
    assert CANDIDATE_GAIN_TABLE_DB[0] == 0.0
    assert CANDIDATE_GAIN_TABLE_DB[-1] == 49.6


def test_build_gain_sweep_plan_always_includes_the_closest_table_entry_to_nominal():
    plan = build_gain_sweep_plan(40.2, 5)
    assert 40.2 in plan


def test_build_gain_sweep_plan_spans_low_to_high():
    plan = build_gain_sweep_plan(40.2, 6)
    assert plan[0] < 10.0
    assert plan[-1] > 45.0
    assert plan == sorted(plan)


@pytest.mark.parametrize("n_steps", [3, 4, 8, 20])
def test_build_gain_sweep_plan_respects_requested_step_count_bounds(n_steps):
    plan = build_gain_sweep_plan(40.2, n_steps)
    assert 3 <= len(plan) <= len(CANDIDATE_GAIN_TABLE_DB)


# ---------------------------------------------------------------- Fase 72: synthetic sweep, known ground truth

@pytest.fixture
def known_gain_sweep():
    """low: safe but weak; mid: healthy; high: near rail; max: clipped -
    the recommendation engine must find this WITHOUT being told the answer."""
    scenarios = [
        ("low", 3.0, {}),
        ("mid", 15.0, {}),
        ("high_near_rail", 35.0, {}),
        ("max_clipped", 60.0, {"force_clip_fraction": 0.02}),
    ]
    steps = []
    for label, gain_linear, extra in scenarios:
        config = InstrumentSimulationConfig(seed=7, gain_linear=gain_linear, **extra)
        iq = simulate_capture_iq(config)
        gain_db_label = {"low": 8.7, "mid": 20.7, "high_near_rail": 40.2, "max_clipped": 49.6}[label]
        steps.append((label, compute_gain_step_result(iq, gain_db_label)))
    return steps


def test_known_sweep_max_gain_is_clipped(known_gain_sweep):
    label, step = known_gain_sweep[-1]
    assert label == "max_clipped"
    assert step.clipping.status == ClippingStatus.CLIPPED


def test_known_sweep_low_gain_is_ok_but_weak(known_gain_sweep):
    label, step = known_gain_sweep[0]
    assert step.clipping.status == ClippingStatus.OK
    assert step.relative_digital_power < known_gain_sweep[1][1].relative_digital_power


def test_recommendation_prefers_high_healthy_gain_never_the_clipped_max(known_gain_sweep):
    steps = [s for _, s in known_gain_sweep]
    recommendation = recommend_operational_gain(steps)
    assert recommendation.status == "OK"
    assert recommendation.recommended_gain_db != 49.6   # never the clipped one
    assert recommendation.recommended_gain_db in (8.7, 20.7, 40.2)
    rejected_gains = {r["gain_db"] for r in recommendation.rejected}
    assert 49.6 in rejected_gains


def test_recommendation_does_not_simply_pick_the_lowest_gain_either(known_gain_sweep):
    """Fase 41: must actively prefer sufficient signal + headroom, not
    default to the safest-looking (lowest) option out of excess caution."""
    steps = [s for _, s in known_gain_sweep]
    recommendation = recommend_operational_gain(steps)
    assert recommendation.recommended_gain_db != 8.7


def test_linearity_detects_clipping_onset_between_last_two_steps(known_gain_sweep):
    steps = [s for _, s in known_gain_sweep]
    result = analyze_gain_linearity(steps)
    kinds = [f["kind"] for f in result["findings"]]
    assert "clipping_onset" in kinds


def test_linearity_monotonic_true_for_well_behaved_sweep():
    steps = []
    for gain_linear, gain_db in ((2.0, 8.7), (8.0, 20.7), (16.0, 29.7), (25.0, 37.2)):
        config = InstrumentSimulationConfig(seed=9, gain_linear=gain_linear)
        iq = simulate_capture_iq(config)
        steps.append(compute_gain_step_result(iq, gain_db))
    result = analyze_gain_linearity(steps)
    assert result["monotonic"] is True


def test_linearity_flags_reversal():
    steps = []
    for gain_linear, gain_db in ((20.0, 8.7), (5.0, 20.7)):   # power goes DOWN as gain goes up - a real anomaly
        config = InstrumentSimulationConfig(seed=11, gain_linear=gain_linear)
        iq = simulate_capture_iq(config)
        steps.append(compute_gain_step_result(iq, gain_db))
    result = analyze_gain_linearity(steps)
    kinds = [f["kind"] for f in result["findings"]]
    assert "reversal" in kinds
    assert result["monotonic"] is False


def test_recommendation_inconclusive_with_fewer_than_two_steps():
    config = InstrumentSimulationConfig(seed=1, gain_linear=15.0)
    iq = simulate_capture_iq(config)
    step = compute_gain_step_result(iq, 40.2)
    result = recommend_operational_gain([step])
    assert result.status == "INCONCLUSIVE"


def test_recommendation_inconclusive_when_every_gain_clips():
    steps = []
    for gain_linear, gain_db in ((60.0, 40.2), (80.0, 49.6)):
        config = InstrumentSimulationConfig(seed=3, gain_linear=gain_linear, force_clip_fraction=0.02)
        iq = simulate_capture_iq(config)
        steps.append(compute_gain_step_result(iq, gain_db))
    result = recommend_operational_gain(steps)
    assert result.status == "INCONCLUSIVE"
    assert result.recommended_gain_db is None
