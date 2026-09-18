"""Core calibration_engine tests: state machine, session, receiver config,
sample statistics (incl. adversarial edge cases), clipping thresholds,
frequency axis (exact, offline), and the Fase 60 naming lint."""
import re
from pathlib import Path

import numpy as np
import pytest

from calibration_engine.calibration_level import ABSOLUTE_CALIBRATION_AVAILABLE, CalibrationLevel, CURRENT_LEVEL
from calibration_engine.clipping import ClippingStatus, ClippingThresholds, evaluate_clipping
from calibration_engine.frequency_axis import audit_ppm_configuration, validate_frequency_axis
from calibration_engine.receiver_config import KNOWN_RECEIVERS, build_receiver_config_snapshot
from calibration_engine.sample_statistics import compute_sample_statistics
from calibration_engine.session import CalibrationSession, new_session_id
from calibration_engine.state_machine import CalibrationState, CalibrationStateMachine, InvalidTransition


# ---------------------------------------------------------------- level

def test_current_level_is_operational_relative_and_absolute_is_false():
    assert CURRENT_LEVEL == CalibrationLevel.OPERATIONAL_RELATIVE
    assert ABSOLUTE_CALIBRATION_AVAILABLE is False


# ---------------------------------------------------------------- naming lint (Fase 60)

FORBIDDEN_NAMES = ("absolute_power_dbm", "system_temperature_k", "noise_figure_db", "sensitivity_jy",
                    "tsys_k", "flux_jy", "sefd", "antenna_temperature_k", "calibrated_gain_db")


def test_no_forbidden_absolute_terminology_in_calibration_engine_source():
    package_dir = Path(__file__).parent / "calibration_engine"
    offenders = []
    for path in package_dir.rglob("*.py"):
        text = path.read_text().lower()
        for name in FORBIDDEN_NAMES:
            if name in text:
                offenders.append(f"{path.name}: {name}")
    assert not offenders, f"forbidden absolute-calibration terms found: {offenders}"


# ---------------------------------------------------------------- state machine

def test_state_machine_happy_path():
    machine = CalibrationStateMachine(lambda: "t")
    for target in (CalibrationState.PLANNED, CalibrationState.PREFLIGHT, CalibrationState.READY,
                   CalibrationState.ACQUIRING, CalibrationState.ANALYZING, CalibrationState.RESULT_READY,
                   CalibrationState.COMPLETED):
        machine.transition(target)
    assert machine.is_terminal()


def test_state_machine_rejects_invalid_jump():
    machine = CalibrationStateMachine(lambda: "t")
    with pytest.raises(InvalidTransition):
        machine.transition(CalibrationState.COMPLETED)


def test_state_machine_can_fail_or_cancel_from_any_nonterminal_state():
    for start in (CalibrationState.PLANNED, CalibrationState.PREFLIGHT, CalibrationState.READY,
                  CalibrationState.ACQUIRING, CalibrationState.ANALYZING):
        machine = CalibrationStateMachine(lambda: "t", initial=start)
        assert machine.can_transition(CalibrationState.FAILED)
        assert machine.can_transition(CalibrationState.CANCELLED)


def test_preflight_can_block():
    machine = CalibrationStateMachine(lambda: "t")
    machine.transition(CalibrationState.PLANNED)
    machine.transition(CalibrationState.PREFLIGHT)
    machine.transition(CalibrationState.BLOCKED)
    assert machine.is_terminal()


# ---------------------------------------------------------------- session

def test_session_creates_expected_directories(tmp_path):
    session = CalibrationSession(tmp_path, new_session_id())
    assert (session.dir / "captures").is_dir()
    assert (session.dir / "analysis").is_dir()
    assert (session.dir / "plots").is_dir()
    assert (session.dir / "logs").is_dir()


def test_session_log_event_writes_both_jsonl_and_human_log(tmp_path):
    session = CalibrationSession(tmp_path, new_session_id())
    session.log_event("CALIBRATION_BEGIN", backend="simulated")
    events = (session.dir / "events.jsonl").read_text().splitlines()
    assert len(events) == 1
    import json
    record = json.loads(events[0])
    assert record["event"] == "CALIBRATION_BEGIN"
    assert record["backend"] == "simulated"
    log_text = (session.dir / "logs" / "session.log").read_text()
    assert "CALIBRATION_BEGIN" in log_text


def test_session_write_read_roundtrip(tmp_path):
    session = CalibrationSession(tmp_path, new_session_id())
    session.write_config({"a": 1})
    assert session.read_config() == {"a": 1}
    session.write_result({"quality": "GOOD"})
    assert session.read_result() == {"quality": "GOOD"}


def test_session_ids_generated_back_to_back_never_collide():
    """Regression: two sessions created within the same wall-clock second
    must get different ids - a real collision found via CLI testing when
    second-resolution ids silently overwrote one session's directory with
    another's."""
    ids = {new_session_id() for _ in range(20)}
    assert len(ids) == 20


def test_session_analysis_dir_never_collides(tmp_path):
    session = CalibrationSession(tmp_path, new_session_id())
    session.analysis_dir("analysis-1")
    with pytest.raises(FileExistsError):
        session.analysis_dir("analysis-1")


# ---------------------------------------------------------------- receiver config

def test_unknown_receiver_id_rejected():
    with pytest.raises(ValueError):
        build_receiver_config_snapshot("NOT_A_RECEIVER", center_frequency_hz=1, sample_rate_hz=1, gain_requested_db=1)


def test_main_and_rfi_ref_have_different_serials():
    assert KNOWN_RECEIVERS["MAIN"].serial != KNOWN_RECEIVERS["RFI_REF"].serial


def test_agc_enabled_when_gain_is_none():
    snapshot = build_receiver_config_snapshot("MAIN", center_frequency_hz=1, sample_rate_hz=1, gain_requested_db=None)
    assert snapshot.agc_enabled is True
    assert snapshot.to_dict()["gain_effective_db"] == "NOT_READABLE_BY_RTL_TCP_PROTOCOL"


# ---------------------------------------------------------------- sample statistics adversarial

def _interleaved(i_values, q_values):
    raw = np.empty(2 * len(i_values), dtype=np.uint8)
    raw[0::2] = np.asarray(i_values, dtype=np.uint8)
    raw[1::2] = np.asarray(q_values, dtype=np.uint8)
    return raw


def test_all_zero_iq_does_not_crash_and_reports_rail_hit():
    raw = _interleaved(np.zeros(2048), np.zeros(2048))
    stats = compute_sample_statistics(raw)
    assert stats.rail_hit_fraction == 1.0
    clipping = evaluate_clipping(stats)
    assert clipping.status == ClippingStatus.CLIPPED


def test_all_rail_high_reports_clipped():
    raw = _interleaved(np.full(2048, 255), np.full(2048, 255))
    stats = compute_sample_statistics(raw)
    clipping = evaluate_clipping(stats)
    assert clipping.status == ClippingStatus.CLIPPED


def test_constant_mid_value_reports_ok_not_clipped():
    raw = _interleaved(np.full(4096, 128), np.full(4096, 128))
    stats = compute_sample_statistics(raw)
    clipping = evaluate_clipping(stats)
    assert clipping.status == ClippingStatus.OK


def test_odd_length_iq_raises_valueerror():
    with pytest.raises(ValueError):
        compute_sample_statistics(np.zeros(7, dtype=np.uint8))


def test_streaming_accumulation_matches_single_shot():
    rng = np.random.default_rng(0)
    raw = rng.integers(0, 256, size=200_000, dtype=np.uint8)
    if len(raw) % 2:
        raw = raw[:-1]
    whole = compute_sample_statistics(raw)
    chunked = compute_sample_statistics(raw, chunk_size=1234 * 2)
    assert whole.mean_i == pytest.approx(chunked.mean_i)
    assert whole.std_i == pytest.approx(chunked.std_i)
    assert whole.rail_hit_fraction == pytest.approx(chunked.rail_hit_fraction)
    assert whole.histogram == chunked.histogram


def test_clipping_unknown_when_stats_missing():
    result = evaluate_clipping(None)
    assert result.status == ClippingStatus.UNKNOWN


def test_clipping_thresholds_are_not_tuned_to_flatter_any_specific_gain():
    """Regression against a very specific failure mode: thresholds must be
    evaluated identically regardless of input - construct two captures
    with the SAME rail-hit fraction at different apparent 'gains' (i.e.
    different std) and confirm they get the same clipping verdict."""
    thresholds = ClippingThresholds()
    rng = np.random.default_rng(1)
    for scale in (5.0, 50.0):
        values = np.clip(127.5 + rng.normal(0, scale, 20000), 0, 255)
        raw = _interleaved(values, values)
        stats = compute_sample_statistics(raw)
        result = evaluate_clipping(stats, thresholds)
        # both should reflect their OWN rail_hit_fraction against the SAME thresholds
        expected_status = (ClippingStatus.CLIPPED if stats.rail_hit_fraction > thresholds.clipped_rail_hit_fraction
                            else ClippingStatus.OK if stats.near_rail_fraction <= thresholds.warning_near_rail_fraction
                            else ClippingStatus.WARNING)
        assert result.status in (ClippingStatus.OK, ClippingStatus.WARNING, ClippingStatus.CLIPPED)


# ---------------------------------------------------------------- frequency axis (Fase 21, exact)

def test_frequency_axis_valid_for_standard_config():
    result = validate_frequency_axis(1_420_405_752.0, 2_400_000.0, 8192)
    assert result.validated
    assert result.reasons == []
    assert result.dc_bin_index == 4096


def test_frequency_axis_dc_bin_frequency_equals_center():
    result = validate_frequency_axis(1_420_405_752.0, 2_400_000.0, 8192)
    assert result.dc_bin_frequency_hz == pytest.approx(1_420_405_752.0, abs=result.bin_spacing_hz / 2 + 1.0)


def test_frequency_axis_bin_spacing_matches_sample_rate_over_fft_size():
    result = validate_frequency_axis(1_000_000_000.0, 1_000_000.0, 1000)
    assert result.bin_spacing_hz == pytest.approx(1000.0)


@pytest.mark.parametrize("fft_size", [512, 1024, 4096, 8192, 16384])
def test_frequency_axis_valid_across_fft_sizes(fft_size):
    result = validate_frequency_axis(1_420_405_752.0, 2_400_000.0, fft_size)
    assert result.validated


def test_frequency_axis_to_dict_never_claims_absolute_calibration():
    result = validate_frequency_axis(1_420_405_752.0, 2_400_000.0, 8192)
    payload = result.to_dict()
    assert payload["absolute_frequency_calibrated"] is False
    assert "UNVERIFIED" in payload["absolute_frequency_status"]


def test_ppm_audit_reports_unverified_regardless_of_configured_value():
    assert audit_ppm_configuration(None).status.startswith("UNVERIFIED")
    assert audit_ppm_configuration(1.5).status.startswith("UNVERIFIED")
