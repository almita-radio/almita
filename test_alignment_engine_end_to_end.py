"""Fase 27's own success criteria, run as tests: plan -> preflight ->
simulated raster -> fit -> recover injected offset -> alignment_result.json
-> sync plan prepared (never applied), for both SOLAR and HI."""
import json
import warnings

import pytest
import astropy.units as u
from astropy.coordinates import EarthLocation
from astropy.time import Time

warnings.filterwarnings("ignore", module="astropy")

from alignment_engine.config import AlignmentConfig
from alignment_engine.engine import AlignmentEngine
from alignment_engine.mount_adapter import SimulatedMountAdapter
from alignment_engine.simulation import HIMapSimConfig, SolarBeamSimConfig
from alignment_engine.state_machine import AlignmentState
from alignment_engine.targets.hi_reference import SyntheticHIReferenceProvider
from alignment_engine.targets.solar import SolarTarget
from alignment_engine.tracking import SimulatedTrackingBackend, TrackingMode

LOCATION = EarthLocation(lat=-33.4489 * u.deg, lon=-70.6693 * u.deg, height=570 * u.m)


def _solar_engine(tmp_path):
    config = AlignmentConfig.load()
    config.global_.output_root = str(tmp_path)
    # Isolate the preflight capture-conflict check from this machine's real
    # (possibly live) data/runtime/ - tests must never depend on whether a
    # real capture happens to be running on the host.
    config.global_.orchestrator_runtime_dir = str(tmp_path / "orchestrator_runtime")
    config.solar.coarse_span_deg, config.solar.coarse_spacing_deg = 14.0, 3.5
    config.solar.fine_span_deg, config.solar.fine_spacing_deg = 5.0, 1.5
    config.solar.min_altitude_deg = -90.0  # test independent of local time-of-day
    mount = SimulatedMountAdapter()
    tracking = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    return AlignmentEngine("solar", config, LOCATION, mount, tracking), mount, tracking


def test_solar_end_to_end_recovers_offset_and_persists_evidence(tmp_path):
    engine, mount, tracking = _solar_engine(tmp_path)
    engine.plan()
    checks = engine.preflight(SolarTarget(LOCATION))
    assert all(c.ok for c in checks)

    sim = SolarBeamSimConfig(true_offset_east_deg=1.2, true_offset_north_deg=-0.7,
                              fwhm_deg=20.0, noise_fraction=0.02, seed=7)
    result = engine.run_solar_simulated(sim)

    assert result.final_fit.estimate.offset_ra_deg == pytest.approx(1.2, abs=0.7)
    assert result.final_fit.estimate.offset_dec_deg == pytest.approx(-0.7, abs=0.7)
    assert engine.state_machine.state == AlignmentState.RESULT_READY
    # tracking was set to SOLAR during the scan and restored afterwards
    assert TrackingMode.SOLAR in tracking.commands_sent
    assert tracking.get_tracking_mode() == TrackingMode.SIDEREAL

    persisted = engine.session.read_alignment_result()
    assert persisted["mode"] == "SOLAR"
    assert persisted["applied_sync"] is False
    assert (engine.session.dir / "raw_grid.json").exists()
    assert (engine.session.dir / "fit_result.json").exists()

    center = SolarTarget(LOCATION).current_position(Time.now()).icrs
    plan = engine.prepare_sync(result, center, is_observational=True, confidence_threshold=0.5)
    assert plan.eligible is True
    assert engine.session.read_sync_plan() is not None
    # prepare_sync must never itself have touched the mount
    assert mount.synced_to is None


def test_hi_end_to_end_recovers_offset_and_blocks_sync_as_non_observational(tmp_path):
    config = AlignmentConfig.load()
    config.global_.output_root = str(tmp_path)
    config.global_.orchestrator_runtime_dir = str(tmp_path / "orchestrator_runtime")
    config.hi.raster_span_deg, config.hi.raster_spacing_deg = 12.0, 3.0
    config.hi.min_altitude_deg = -90.0
    mount = SimulatedMountAdapter()
    tracking = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    engine = AlignmentEngine("hi", config, LOCATION, mount, tracking)
    engine.plan()
    provider = SyntheticHIReferenceProvider()
    checks = engine.preflight(provider)
    assert all(c.ok for c in checks)

    sim = HIMapSimConfig(true_offset_east_deg=-0.8, true_offset_north_deg=1.5,
                          gain_a=2.3, baseline_b=5.0, noise_fraction=0.02, seed=11)
    result = engine.run_hi_simulated(provider, sim)

    assert result.final_fit.estimate.offset_ra_deg == pytest.approx(-0.8, abs=0.7)
    assert result.final_fit.estimate.offset_dec_deg == pytest.approx(1.5, abs=0.7)
    assert TrackingMode.SIDEREAL == tracking.get_tracking_mode()  # never left SIDEREAL for HI

    persisted = engine.session.read_alignment_result()
    assert persisted["is_observational"] is False

    center, _ = provider.choose_target(LOCATION, Time.now(), config.hi.min_altitude_deg, 20.0)
    plan = engine.prepare_sync(result, center, is_observational=provider.is_observational)
    assert plan.eligible is False
    assert "not observational" in plan.eligibility_reason
    assert mount.synced_to is None


def test_preflight_failure_transitions_to_preflight_failed_not_a_crash(tmp_path):
    config = AlignmentConfig.load()
    config.global_.output_root = str(tmp_path)
    config.global_.orchestrator_runtime_dir = str(tmp_path / "orchestrator_runtime")
    config.solar.min_altitude_deg = 89.9  # nearly impossible to satisfy -> forces failure
    mount = SimulatedMountAdapter()
    tracking = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    engine = AlignmentEngine("solar", config, LOCATION, mount, tracking)
    engine.plan()
    checks = engine.preflight(SolarTarget(LOCATION))
    assert not all(c.ok for c in checks)
    assert engine.state_machine.state == AlignmentState.PREFLIGHT_FAILED
    assert engine.state_machine.is_terminal()


def test_cancel_is_terminal_and_idempotent(tmp_path):
    config = AlignmentConfig.load()
    config.global_.output_root = str(tmp_path)
    config.global_.orchestrator_runtime_dir = str(tmp_path / "orchestrator_runtime")
    engine = AlignmentEngine("solar", config, LOCATION, SimulatedMountAdapter(),
                              SimulatedTrackingBackend())
    engine.plan()
    engine.cancel("operator requested stop")
    assert engine.state_machine.state == AlignmentState.CANCELLED
    engine.cancel("called again after already cancelled")  # must not raise
    assert engine.state_machine.state == AlignmentState.CANCELLED


def test_snapshot_is_json_serializable_and_reflects_current_state(tmp_path):
    config = AlignmentConfig.load()
    config.global_.output_root = str(tmp_path)
    config.global_.orchestrator_runtime_dir = str(tmp_path / "orchestrator_runtime")
    engine = AlignmentEngine("solar", config, LOCATION, SimulatedMountAdapter(),
                              SimulatedTrackingBackend())
    engine.plan()
    snapshot = engine.snapshot(point_index=5, point_total=20, latest_metric=42.0)
    payload = json.dumps(snapshot.to_dict())  # must not raise
    decoded = json.loads(payload)
    assert decoded["state"] == "PLANNED"
    assert decoded["point_index"] == 5
    assert decoded["point_total"] == 20
    assert decoded["latest_metric"] == 42.0
