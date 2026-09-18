"""Fase 27's own success criteria, run as tests: plan -> preflight ->
simulated raster -> fit -> recover injected offset -> alignment_result.json
-> sync plan prepared (never applied), for both SOLAR and HI.

3rd pass: preflight()/run_solar_simulated()/run_hi_simulated() are now
`async def` (the async tracking contract) - plan()/cancel()/snapshot()
stay plain sync, unchanged."""
import asyncio
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


@pytest.mark.asyncio
async def test_solar_end_to_end_recovers_offset_and_persists_evidence(tmp_path):
    engine, mount, tracking = _solar_engine(tmp_path)
    engine.plan()
    checks = await engine.preflight(SolarTarget(LOCATION))
    assert all(c.ok for c in checks)

    sim = SolarBeamSimConfig(true_offset_east_deg=1.2, true_offset_north_deg=-0.7,
                              fwhm_deg=20.0, noise_fraction=0.02, seed=7)
    result = await engine.run_solar_simulated(sim)

    assert result.final_fit.estimate.offset_ra_deg == pytest.approx(1.2, abs=0.7)
    assert result.final_fit.estimate.offset_dec_deg == pytest.approx(-0.7, abs=0.7)
    assert engine.state_machine.state == AlignmentState.RESULT_READY
    # tracking was set to SOLAR during the scan and restored afterwards
    assert TrackingMode.SOLAR in tracking.commands_sent
    assert await tracking.get_tracking_mode() == TrackingMode.SIDEREAL
    # engine's own cached tracking mode (snapshot()'s source, item 7) agrees
    assert engine.snapshot().tracking_mode == "SIDEREAL"

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


@pytest.mark.asyncio
async def test_hi_end_to_end_recovers_offset_and_blocks_sync_as_non_observational(tmp_path):
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
    checks = await engine.preflight(provider)
    assert all(c.ok for c in checks)

    sim = HIMapSimConfig(true_offset_east_deg=-0.8, true_offset_north_deg=1.5,
                          gain_a=2.3, baseline_b=5.0, noise_fraction=0.02, seed=11)
    result = await engine.run_hi_simulated(provider, sim)

    assert result.final_fit.estimate.offset_ra_deg == pytest.approx(-0.8, abs=0.7)
    assert result.final_fit.estimate.offset_dec_deg == pytest.approx(1.5, abs=0.7)
    assert TrackingMode.SIDEREAL == await tracking.get_tracking_mode()  # never left SIDEREAL for HI

    persisted = engine.session.read_alignment_result()
    assert persisted["is_observational"] is False

    center, _ = provider.choose_target(LOCATION, Time.now(), config.hi.min_altitude_deg, 20.0)
    plan = engine.prepare_sync(result, center, is_observational=provider.is_observational)
    assert plan.eligible is False
    assert "not observational" in plan.eligibility_reason
    assert mount.synced_to is None


@pytest.mark.asyncio
async def test_preflight_failure_transitions_to_preflight_failed_not_a_crash(tmp_path):
    config = AlignmentConfig.load()
    config.global_.output_root = str(tmp_path)
    config.global_.orchestrator_runtime_dir = str(tmp_path / "orchestrator_runtime")
    config.solar.min_altitude_deg = 89.9  # nearly impossible to satisfy -> forces failure
    mount = SimulatedMountAdapter()
    tracking = SimulatedTrackingBackend(TrackingMode.SIDEREAL)
    engine = AlignmentEngine("solar", config, LOCATION, mount, tracking)
    engine.plan()
    checks = await engine.preflight(SolarTarget(LOCATION))
    assert not all(c.ok for c in checks)
    assert engine.state_machine.state == AlignmentState.PREFLIGHT_FAILED
    assert engine.state_machine.is_terminal()


@pytest.mark.asyncio
async def test_preflight_tracking_backend_timeout_fails_that_check_not_the_whole_preflight_call():
    """Item 4: a hung tracking backend must produce a structured failed
    check (and a real, bounded wait), never an infinite await."""
    class HangingTrackingBackend:
        async def get_tracking_mode(self):
            await asyncio.sleep(999)

        async def set_tracking_mode(self, mode):
            return True

    config = AlignmentConfig.load()
    config.global_.tracking_timeout_s = 0.05
    engine = AlignmentEngine("solar", config, LOCATION, SimulatedMountAdapter(), HangingTrackingBackend())
    engine.plan()
    checks = await engine.preflight(SolarTarget(LOCATION), timeout=0.05)
    tracking_check = next(c for c in checks if c.name == "tracking_backend_reachable")
    assert tracking_check.ok is False
    assert "timed out" in tracking_check.detail


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


@pytest.mark.asyncio
async def test_solar_run_cancellation_transitions_to_cancelled_not_stuck(tmp_path):
    """Item 5: a cancellation raised mid-scan must not leave the state
    machine stuck in TRACKING_CONFIGURED/SCANNING forever."""
    from alignment_engine import scan_planner

    engine, mount, tracking = _solar_engine(tmp_path)
    engine.plan()
    await engine.preflight(SolarTarget(LOCATION))

    def _boom(*_a, **_k):
        raise asyncio.CancelledError()

    import alignment_engine.engine as engine_module
    original = engine_module.scan_planner.build_raster
    engine_module.scan_planner.build_raster = _boom
    try:
        sim = SolarBeamSimConfig(true_offset_east_deg=0.0, true_offset_north_deg=0.0, fwhm_deg=20.0)
        with pytest.raises(asyncio.CancelledError):
            await engine.run_solar_simulated(sim)
    finally:
        engine_module.scan_planner.build_raster = original

    assert engine.state_machine.state == AlignmentState.CANCELLED
    assert engine.state_machine.is_terminal()
    # tracking was restored even though the body raised
    assert await tracking.get_tracking_mode() == TrackingMode.SIDEREAL
    # item 5/16: state persistence - the ON-DISK state.json must agree,
    # not just the in-memory state machine (a future web/API reader only
    # ever sees the persisted file).
    persisted_state = engine.session.read_state()
    assert persisted_state["state"] == "CANCELLED"


@pytest.mark.asyncio
async def test_solar_run_generic_exception_transitions_to_failed_not_cancelled(tmp_path):
    engine, mount, tracking = _solar_engine(tmp_path)
    engine.plan()
    await engine.preflight(SolarTarget(LOCATION))

    import alignment_engine.engine as engine_module

    def _boom(*_a, **_k):
        raise ValueError("synthetic failure")

    original = engine_module.scan_planner.build_raster
    engine_module.scan_planner.build_raster = _boom
    try:
        sim = SolarBeamSimConfig(true_offset_east_deg=0.0, true_offset_north_deg=0.0, fwhm_deg=20.0)
        with pytest.raises(ValueError):
            await engine.run_solar_simulated(sim)
    finally:
        engine_module.scan_planner.build_raster = original

    assert engine.state_machine.state == AlignmentState.FAILED
    assert await tracking.get_tracking_mode() == TrackingMode.SIDEREAL


@pytest.mark.asyncio
async def test_progress_events_are_emitted_in_order_and_json_friendly(tmp_path):
    """Item 8: engine -> progress event -> (here) a plain list-collecting
    sink, same contract the CLI's formatter uses."""
    import json as _json

    events = []
    config = AlignmentConfig.load()
    config.global_.output_root = str(tmp_path)
    config.global_.orchestrator_runtime_dir = str(tmp_path / "orchestrator_runtime")
    config.solar.min_altitude_deg = -90.0
    engine = AlignmentEngine("solar", config, LOCATION, SimulatedMountAdapter(),
                              SimulatedTrackingBackend(), on_event=events.append)
    engine.plan()
    await engine.preflight(SolarTarget(LOCATION))
    sim = SolarBeamSimConfig(true_offset_east_deg=0.5, true_offset_north_deg=-0.3, fwhm_deg=20.0, noise_fraction=0.0)
    await engine.run_solar_simulated(sim)

    assert _json.dumps(events)  # every event must be JSON-serializable
    state_sequence = [e["state"] for e in events if e["type"] == "state"]
    assert state_sequence == [
        "PLANNED", "PREFLIGHT_OK", "TRACKING_CONFIGURED", "SCANNING", "FITTING", "SCANNING", "FITTING", "RESULT_READY",
    ]
    fit_events = [e for e in events if e["type"] == "fit"]
    assert [e["stage"] for e in fit_events] == ["coarse", "fine"]
