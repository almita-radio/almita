"""Tests for observation_spec.py: schema validation, ranges, enums, hash determinism."""
import copy

import pytest

import observation_spec as sp


def _valid_raw():
    return {
        "session": {"name": "ALMITA-TEST-01"},
        "grid": {
            "mode": "EQUATORIAL_RECT", "placement": "EASTMOST_SAFE",
            "center_ra_hours": None, "center_dec_deg": None,
            "width_deg": 30, "height_deg": 30, "rows": 20, "cols": 20,
            "min_altitude_deg": 10, "traversal": "SERPENTINE",
        },
        "capture": {"seconds": 10, "settle_seconds": 2},
        "main": {"center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 40.2, "bias_tee": True},
        "rfi_ref": {"enabled": True, "serial": "00000002", "gain_db": 25.0},
        "quicklook": {"enabled": True, "native_grid": True, "interpolated_preview": True,
                      "calibration_profile_path": "profile.json"},
        "console": {"enabled": True},
        "execution": {"unattended": True},
    }


def test_valid_config_passes():
    result = sp.validate_spec_dict(_valid_raw())
    assert result["schema_version"] == sp.SCHEMA_VERSION
    assert result["grid"]["rows"] == 20
    assert result["grid"]["nominal_spacing_deg"] == pytest.approx(30 / 19)


def test_missing_top_level_section_fails():
    raw = _valid_raw()
    del raw["rfi_ref"]
    with pytest.raises(sp.ObservationSpecError, match="missing top-level section"):
        sp.validate_spec_dict(raw)


def test_unknown_top_level_section_fails():
    raw = _valid_raw()
    raw["bogus"] = {}
    with pytest.raises(sp.ObservationSpecError, match="unknown top-level section"):
        sp.validate_spec_dict(raw)


def test_missing_required_field_fails():
    raw = _valid_raw()
    del raw["grid"]["width_deg"]
    with pytest.raises(sp.ObservationSpecError, match="width_deg"):
        sp.validate_spec_dict(raw)


@pytest.mark.parametrize("field,value", [
    ("width_deg", -1), ("height_deg", 0), ("rows", 1), ("cols", 1),
    ("min_altitude_deg", -5), ("min_altitude_deg", 90),
])
def test_invalid_range_fails(field, value):
    raw = _valid_raw()
    raw["grid"][field] = value
    with pytest.raises(sp.ObservationSpecError):
        sp.validate_spec_dict(raw)


def test_unknown_mode_enum_fails():
    raw = _valid_raw()
    raw["grid"]["mode"] = "GALACTIC_STRIP"
    with pytest.raises(sp.ObservationSpecError, match="not one of"):
        sp.validate_spec_dict(raw)


def test_reserved_placement_gives_not_implemented_message():
    raw = _valid_raw()
    raw["grid"]["placement"] = "CENTERED"
    with pytest.raises(sp.ObservationSpecError, match="reserved.*not yet implemented"):
        sp.validate_spec_dict(raw)


def test_fixed_center_requires_ra_dec():
    raw = _valid_raw()
    raw["grid"]["placement"] = "FIXED_CENTER"
    with pytest.raises(sp.ObservationSpecError, match="center_ra_hours"):
        sp.validate_spec_dict(raw)

    raw["grid"]["center_ra_hours"] = 6.0
    raw["grid"]["center_dec_deg"] = -33.0
    result = sp.validate_spec_dict(raw)
    assert result["grid"]["center_ra_hours"] == 6.0


def test_spacing_mismatch_rejected():
    raw = _valid_raw()
    raw["grid"]["rows"] = 19  # 30/19 != 30/18 -> inconsistent single-spacing model
    with pytest.raises(sp.ObservationSpecError, match="must match"):
        sp.validate_spec_dict(raw)


def test_bias_tee_false_rejected_with_honest_explanation():
    raw = _valid_raw()
    raw["main"]["bias_tee"] = False
    with pytest.raises(sp.ObservationSpecError, match="no CLI flag to disable bias-tee"):
        sp.validate_spec_dict(raw)


def test_rfi_ref_bias_tee_defaults_false_when_field_omitted():
    raw = _valid_raw()
    assert "bias_tee" not in raw["rfi_ref"]  # historical spec, predates this field
    result = sp.validate_spec_dict(raw)
    assert result["rfi_ref"]["bias_tee"] is False


def test_rfi_ref_bias_tee_true_accepted():
    raw = _valid_raw()
    raw["rfi_ref"]["bias_tee"] = True
    result = sp.validate_spec_dict(raw)
    assert result["rfi_ref"]["bias_tee"] is True


def test_rfi_ref_bias_tee_false_accepted_explicitly():
    raw = _valid_raw()
    raw["rfi_ref"]["bias_tee"] = False
    result = sp.validate_spec_dict(raw)
    assert result["rfi_ref"]["bias_tee"] is False


def test_quicklook_enabled_requires_calibration_profile():
    raw = _valid_raw()
    raw["quicklook"]["calibration_profile_path"] = None
    with pytest.raises(sp.ObservationSpecError, match="calibration_profile_path"):
        sp.validate_spec_dict(raw)


def test_session_name_pattern_enforced():
    raw = _valid_raw()
    raw["session"]["name"] = "bad name with spaces!"
    with pytest.raises(sp.ObservationSpecError):
        sp.validate_spec_dict(raw)


def test_unknown_nested_field_rejected():
    raw = _valid_raw()
    raw["main"]["extra_field"] = 1
    with pytest.raises(sp.ObservationSpecError, match="unknown field"):
        sp.validate_spec_dict(raw)


def test_hash_deterministic_for_identical_input():
    payload = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
    assert sp.compute_config_hash(payload) == sp.compute_config_hash(copy.deepcopy(payload))


def test_hash_key_order_independent():
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert sp.compute_config_hash(a) == sp.compute_config_hash(b)


def test_hash_changes_with_content():
    a = {"x": 1}
    b = {"x": 2}
    assert sp.compute_config_hash(a) != sp.compute_config_hash(b)


def test_load_and_validate_rejects_invalid_yaml(tmp_path):
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("session: [unterminated")
    with pytest.raises(sp.ObservationSpecError, match="invalid YAML"):
        sp.load_and_validate(str(bad_yaml))


def test_load_and_validate_round_trips_valid_yaml(tmp_path):
    import yaml
    path = tmp_path / "obs.yaml"
    path.write_text(yaml.safe_dump(_valid_raw()))
    result = sp.load_and_validate(str(path))
    assert result["session"]["name"] == "ALMITA-TEST-01"
