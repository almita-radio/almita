"""Fase 16: deployment interlock test matrix - the safety-critical gate
that must never let hardware movement proceed without an explicit,
persisted FIELD confirmation."""
import json

import pytest

from alignment_engine.deployment_state import (
    DeploymentRecord,
    DeploymentState,
    check_hardware_movement_allowed,
    read_current_deployment_state,
    write_deployment_state,
)


def test_missing_state_file_blocks(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    record = read_current_deployment_state(path)
    assert record is None
    result = check_hardware_movement_allowed("real GOTO", record)
    assert result.allowed is False
    assert result.observed_state == "MISSING"


def test_corrupt_state_file_blocks(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json")
    record = read_current_deployment_state(str(path))
    assert record is None
    result = check_hardware_movement_allowed("real GOTO", record)
    assert result.allowed is False
    assert result.observed_state == "MISSING"


def test_state_file_with_unrecognized_state_string_blocks(tmp_path):
    path = tmp_path / "bad_state.json"
    path.write_text(json.dumps({"state": "SOMEWHERE_WEIRD", "timestamp_utc": "now",
                                 "operator_action": "x", "hostname": "h"}))
    record = read_current_deployment_state(str(path))
    assert record is None


@pytest.mark.parametrize("state", [DeploymentState.UNKNOWN, DeploymentState.BENCH, DeploymentState.INDOOR])
def test_non_field_states_block_movement(tmp_path, state):
    path = str(tmp_path / "deployment.json")
    write_deployment_state(state, operator_action="set-test", path=path)
    record = read_current_deployment_state(path)
    result = check_hardware_movement_allowed("real GOTO", record)
    assert result.allowed is False
    assert result.observed_state == state.value


def test_field_state_allows_movement(tmp_path):
    path = str(tmp_path / "deployment.json")
    write_deployment_state(DeploymentState.FIELD, operator_action="set-field", reason="deployed on roof", path=path)
    record = read_current_deployment_state(path)
    result = check_hardware_movement_allowed("real GOTO", record)
    assert result.allowed is True
    assert result.observed_state == "FIELD"


def test_written_record_has_full_provenance(tmp_path):
    path = str(tmp_path / "deployment.json")
    record = write_deployment_state(DeploymentState.FIELD, operator_action="set-field", reason="test", path=path)
    assert record.timestamp_utc
    assert record.hostname
    assert record.operator_action == "set-field"
    assert record.reason == "test"
    on_disk = json.loads((tmp_path / "deployment.json").read_text())
    assert on_disk["state"] == "FIELD"
    assert on_disk["hostname"] == record.hostname


def test_transition_field_to_indoor_blocks_again(tmp_path):
    """No state is sticky in a way that could be exploited - moving FROM
    field back to indoor/bench/unknown must immediately re-block."""
    path = str(tmp_path / "deployment.json")
    write_deployment_state(DeploymentState.FIELD, operator_action="set-field", path=path)
    assert check_hardware_movement_allowed("real GOTO", read_current_deployment_state(path)).allowed is True
    write_deployment_state(DeploymentState.INDOOR, operator_action="set-indoor", reason="packed up", path=path)
    assert check_hardware_movement_allowed("real GOTO", read_current_deployment_state(path)).allowed is False


def test_missing_state_message_names_the_actual_path_checked_not_the_default(tmp_path):
    """Regression: an earlier version hardcoded DEFAULT_STATE_PATH in the
    message regardless of which path was actually checked, making the
    error misleading for any caller using a non-default path."""
    custom_path = str(tmp_path / "my_custom_deployment_file.json")
    result = check_hardware_movement_allowed("real GOTO", None, state_path=custom_path)
    assert custom_path in result.reason
    assert "data/deployment_state.json" not in result.reason


def test_default_state_is_not_field(tmp_path):
    """No state file at all (the out-of-the-box case) must never be
    silently treated as FIELD."""
    path = str(tmp_path / "never_written.json")
    result = check_hardware_movement_allowed("real GOTO", read_current_deployment_state(path))
    assert result.allowed is False


def test_movement_allowed_states_is_exactly_field():
    from alignment_engine.deployment_state import MOVEMENT_ALLOWED_STATES
    assert MOVEMENT_ALLOWED_STATES == frozenset({DeploymentState.FIELD})


def test_check_hardware_movement_allowed_has_no_override_parameter():
    import inspect
    sig = inspect.signature(check_hardware_movement_allowed)
    for name in sig.parameters:
        assert "override" not in name.lower()
        assert "force" not in name.lower()
