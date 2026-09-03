"""Tests for the ALMITA Observe web API's RFI_REF/bias_tee handling.

_handle_plan() in almita_orchestrator_server.py does nothing beyond
json.loads(body) -> observation_spec.validate_spec_dict() -> observation_plan
.plan_observation() -> json response; there is no server-side logic of its
own for rfi_ref/bias_tee. So the meaningful, cheap thing to test here is that
a JSON body shaped exactly like console/observe.js's readSpec() output
survives validate_spec_dict() with rfi_ref.bias_tee intact - this is exactly
what a real browser POST to /api/observe/plan would send.

Deliberately does NOT call observation_plan.plan_observation() here: this
Pi runs with ~0 free swap, and stacking more than the one full
plan_observation() call already budgeted in test_observation_plan.py (real
grid_generator.py matplotlib rendering) risks an OOM kill. The resolved-plan
and START-side passthrough of bias_tee are covered there and in
test_observation_orchestrator.py instead.
"""
import observation_spec as sp


def _web_payload(*, rfi_ref_enabled=True, bias_tee=True):
    """Mirrors console/observe.js's readSpec() output byte-for-byte for the
    fields relevant here."""
    return {
        "session": {"name": "ALMITA-OBSERVE"},
        "grid": {
            "mode": "EQUATORIAL_RECT", "placement": "EASTMOST_SAFE",
            "center_ra_hours": None, "center_dec_deg": None,
            "width_deg": 30, "height_deg": 30, "rows": 20, "cols": 20,
            "min_altitude_deg": 10, "traversal": "SERPENTINE",
        },
        "capture": {"seconds": 10, "settle_seconds": 2},
        "main": {"center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 40.2, "bias_tee": True},
        "rfi_ref": {"enabled": rfi_ref_enabled, "serial": "00000002", "gain_db": 25.0, "bias_tee": bias_tee},
        "quicklook": {"enabled": False, "native_grid": True, "interpolated_preview": False,
                      "calibration_profile_path": None},
        "console": {"enabled": True},
        "execution": {"unattended": True},
    }


def test_web_payload_bias_tee_true_survives_validation():
    spec = sp.validate_spec_dict(_web_payload(bias_tee=True))
    assert spec["rfi_ref"]["bias_tee"] is True
    assert spec["rfi_ref"]["enabled"] is True


def test_web_payload_bias_tee_false_survives_validation():
    spec = sp.validate_spec_dict(_web_payload(bias_tee=False))
    assert spec["rfi_ref"]["bias_tee"] is False


def test_web_payload_rfi_ref_disabled_still_records_bias_tee_choice():
    """enabled=false + bias_tee=true is a valid, meaningful combination (the
    operator toggled RFI_REF off but left Bias-T checked): the schema must
    not silently force bias_tee to false just because enabled is false -
    the console-side toggle only affects the form's visual state."""
    spec = sp.validate_spec_dict(_web_payload(rfi_ref_enabled=False, bias_tee=True))
    assert spec["rfi_ref"]["enabled"] is False
    assert spec["rfi_ref"]["bias_tee"] is True
