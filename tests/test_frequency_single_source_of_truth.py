"""Single source of truth for the real operating frequency (and sample rate/gain) across CALIBRATION,
OBSERVE PLAN/RUN and QUICKLOOK - the real incident this exists to prevent: console/calibrate.html hardcoded
CENTER FREQUENCY to a stale 1420405000 Hz (never synced to observer_config.json), a WIZARD session run without
editing it built a calibration profile at that stale value, while observer_config.json's real
center_frequency_hz (1420405752 Hz) is what a real OBSERVE session actually captured at - Quicklook then
silently skipped all 625/625 real points as INCOMPATIBLE, discovered only after a ~4h20m real capture finished.

These tests are read-only / pure-function: no hardware, no INDI, no SDR, no real capture.py or PLAN/RUN. Where
a real fixture already on disk from that real incident is the most convincing evidence (the WIZARD's own
profile, and one of the real 625 MAIN captures), it is read, never written to or modified."""
import json

import pytest

import almita_web_ops as ops
import calibration_foundation as cf
import capture as capture_module
import observation_orchestrator as orch
import observation_preflight as pf

REAL_PROFILE_PATH = "data/calibration/WIZARD-20260928-022756-139526/observe_profile/calibration_profile_v1"
REAL_MAIN_CAPTURE = ("data/mosaic/ALMITA-OBSERVE-20260928-02:45:30/data/iq/"
                     "ALMITA-OBSERVE-20260928-02:47:24/ALMITA-OBSERVE_0001.h5")


# ------------------------------------------------------------------ 1. defaults synced (single source of truth)

def test_calibrate_wizard_defaults_matches_observe_defaults_source():
    """Both now read the SAME observation_defaults() - not two independent config reads that can drift apart."""
    wiz = ops.calibrate_wizard_defaults()
    obs = ops.observation_defaults()
    assert wiz["center_frequency_hz"] == obs["center_frequency_hz"]
    assert wiz["sample_rate_hz"] == obs["sample_rate_hz"]


def test_calibrate_wizard_defaults_reflects_a_changed_observer_config(tmp_path, monkeypatch):
    """Not just coincidentally equal today: changing observer_config.json changes BOTH the wizard's and
    OBSERVE's suggested frequency identically, proving they share one real source, not two literals that
    happen to agree right now."""
    cfg = {"observation_defaults": {"center_frequency_hz": 1420999999, "sample_rate_hz": 2400000, "gain_db": 40.2}}
    (tmp_path / "observer_config.json").write_text(json.dumps(cfg))
    import sdr_tuning
    monkeypatch.setattr(sdr_tuning, "CONFIG_PATH", tmp_path / "observer_config.json")
    assert ops.observation_defaults()["center_frequency_hz"] == 1420999999
    assert ops.calibrate_wizard_defaults()["center_frequency_hz"] == 1420999999


def test_calibrate_wizard_start_fallback_no_longer_a_bare_literal(monkeypatch, tmp_path):
    """build_command()'s calibrate_wizard "start" fallback (only used if the request omits the field) now comes
    from observation_defaults() too - this used to be the literal 1_420_405_000.0, independent of
    observer_config.json (the actual root cause of the real incident: the wizard form's own default disagreed
    with observer_config.json's real 1420405752 Hz)."""
    cfg = {"observation_defaults": {"center_frequency_hz": 1420111111, "sample_rate_hz": 2400000, "gain_db": 40.2}}
    (tmp_path / "observer_config.json").write_text(json.dumps(cfg))
    import sdr_tuning
    monkeypatch.setattr(sdr_tuning, "CONFIG_PATH", tmp_path / "observer_config.json")
    argv, meta = ops.build_command("calibrate_wizard", {"action": "start"}, "job-1")
    assert "--center-freq" in argv
    assert argv[argv.index("--center-freq") + 1] == "1420111111.0"


# ------------------------------------------------------------------ 2. PLAN -> RUN: no silent change

def test_capture_args_use_the_frozen_resolved_plan_value_verbatim():
    """_capture_args() (what RUN actually launches capture.py with) must read the RESOLVED plan's own
    main.center_frequency_hz - never re-derive or re-fetch it - so whatever PLAN showed the operator is
    EXACTLY what gets captured, regardless of what observer_config.json says by the time RUN actually fires."""
    plan = {
        "mosaic_csv_path": "data/mosaic/FAKE/mosaic.csv",
        "requested": {"capture": {"settle_seconds": 1.0, "seconds": 5.0}, "grid": {"min_altitude_deg": 10.0}},
        "main": {"center_frequency_hz": 1420405752, "sample_rate": 2400000, "gain_db": 40.2},
        "rfi_ref": {"enabled": False},
    }
    argv = orch._capture_args(plan, "data/runtime")
    assert argv[argv.index("--sdr-freq") + 1] == "1420405752"
    # a DIFFERENT plan value must produce a DIFFERENT argv - proving this is read from the plan, not a constant
    plan2 = {**plan, "main": {**plan["main"], "center_frequency_hz": 1420405000}}
    argv2 = orch._capture_args(plan2, "data/runtime")
    assert argv2[argv2.index("--sdr-freq") + 1] == "1420405000"


# ------------------------------------------------------------------ 3 & 4. compatible profile accepted / mismatched
# frequency rejected BEFORE capture.py launches - using the REAL profile and a REAL captured file from the
# actual incident, so this is not a synthetic reproduction: it is the literal case that happened.

def test_compatible_capture_is_accepted_by_the_same_function_quicklook_uses():
    profile = cf.load_calibration_profile(REAL_PROFILE_PATH)
    result = cf.check_calibration_compatibility_values(
        profile, center_frequency_hz=1420405000.0, sample_rate_hz=2400000.0, gain_db=40.2,
        topology=capture_module.INPUT_TOPOLOGIES["antenna"],
    )
    assert result["status"] == "COMPATIBLE"


def test_mismatched_frequency_is_rejected_by_the_same_function_quicklook_uses():
    """The exact real incident's numbers: the real MAIN capture's own recorded frequency (1420405752, read
    straight from the real HDF5 file) against the real WIZARD profile (1420405000)."""
    profile = cf.load_calibration_profile(REAL_PROFILE_PATH)
    real_capture_result = cf.check_calibration_compatibility(profile, REAL_MAIN_CAPTURE)
    assert real_capture_result == {"status": "INCOMPATIBLE", "reason": "center frequency: 1420405752 != 1420405000.0"}


def test_preflight_calibration_match_check_passes_for_a_compatible_plan():
    quicklook_cfg = {"enabled": True, "calibration_profile_path": REAL_PROFILE_PATH}
    main_cfg = {"center_frequency_hz": 1420405000.0, "sample_rate": 2400000.0, "gain_db": 40.2}
    check = pf._quicklook_calibration_match_check(quicklook_cfg, main_cfg)
    assert check["status"] == "PASS" and check["criticality"] == "REQUIRED"


def test_preflight_calibration_match_check_blocks_before_capture_launches_for_the_real_incident_values():
    """The real incident, reproduced exactly: an OBSERVE plan at observer_config.json's real 1420405752 Hz
    against the real WIZARD profile built at 1420405000 Hz - must BLOCK, with both real values named, and this
    result must make the overall preflight verdict BLOCK (which is what actually stops run_observation() from
    ever calling Popen() on capture.py - see observation_orchestrator._run_observation_locked)."""
    quicklook_cfg = {"enabled": True, "calibration_profile_path": REAL_PROFILE_PATH}
    main_cfg = {"center_frequency_hz": 1420405752, "sample_rate": 2400000, "gain_db": 40.2}
    check = pf._quicklook_calibration_match_check(quicklook_cfg, main_cfg)
    assert check["status"] == "BLOCK" and check["criticality"] == "REQUIRED"
    assert "1420405752" in check["detail"] and "1420405000" in check["detail"]
    assert pf._overall([check]) == "BLOCK"


def test_preflight_calibration_match_check_ignored_when_quicklook_disabled():
    check = pf._quicklook_calibration_match_check({"enabled": False}, {"center_frequency_hz": 1420405752})
    assert check["status"] == "PASS" and check["criticality"] == "OPTIONAL"


def test_preflight_calibration_match_check_does_not_double_report_a_missing_profile():
    """_quicklook_check() (unchanged) already WARNs on a genuinely unavailable profile file - this check must
    stay silent (PASS) for that same case rather than raising a second, differently-worded verdict about it."""
    check = pf._quicklook_calibration_match_check(
        {"enabled": True, "calibration_profile_path": "data/calibration/does/not/exist"}, {"center_frequency_hz": 1})
    assert check["status"] == "PASS"


# ------------------------------------------------------------------ compatibility decision function itself: basic
# correctness independent of the real-incident fixtures above

def _fake_profile(**over):
    metadata = {"center_frequency_hz": 1420405000.0, "sample_rate_hz": 2400000.0, "gain_db": 40.2,
                "fft_size": 8192, "instrument_chain": "LNA_FILTER_CABLING_TO_RTL_SDR",
                "reference_topology": "AMBIENT_50R_AT_LNA_INPUT_WIZARD"}
    metadata.update(over)
    return {"metadata": metadata}


@pytest.mark.parametrize("field,value,expect_reason_prefix", [
    ("center_frequency_hz", 1420405001.0, "center frequency"),
    ("sample_rate_hz", 2400001.0, "sample rate"),
    ("gain_db", 40.3, "gain"),
])
def test_check_values_rejects_each_mismatched_field_independently(field, value, expect_reason_prefix):
    base = dict(center_frequency_hz=1420405000.0, sample_rate_hz=2400000.0, gain_db=40.2,
                topology="ANTENNA_TO_LNA_FILTER_CABLING_TO_RTL_SDR")
    base[field] = value
    result = cf.check_calibration_compatibility_values(_fake_profile(), **base)
    assert result["status"] == "INCOMPATIBLE" and result["reason"].startswith(expect_reason_prefix)


def test_check_values_missing_field_is_unknown_not_silently_compatible():
    result = cf.check_calibration_compatibility_values(
        _fake_profile(), center_frequency_hz=None, sample_rate_hz=2400000.0, gain_db=40.2, topology="x")
    assert result["status"] == "UNKNOWN"
