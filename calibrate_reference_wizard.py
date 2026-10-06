#!/usr/bin/env python3
"""ALMITA CALIBRATE reference wizard - stateful CLI, one subcommand per discrete web-driven step: 50 ohm
(physical, hand-connected) -> HI ALTO -> HI BAJO (real sky zones, real GOTO). See
calibration_engine/reference_wizard.py's module docstring for the full scientific rationale and the safety
order (never move while the 50 ohm terminator could still be connected).

Real captures only in `capture-50r`/`capture-hi` without --simulate; a real GOTO happens ONLY in `capture-hi`
(never anywhere else in this script). Each web click is exactly one invocation of this script against an
existing session directory - the operator's explicit confirmation IS the corresponding subcommand call;
nothing here advances on a timer, and nothing here decides physical-movement policy (typed MOVE + real
preflight are enforced by almita_web_ops.start()'s existing physical-stage gate, reused unmodified, on the
`calibrate_wizard_move` stage this script's `capture-hi` action is dispatched through).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_state import atomic_write_json, read_json_safe

from calibration_engine.reference_wizard import (
    WIZARD_REFERENCE_ORDER, PhysicalReferenceConfig, WizardConfig, WizardStep,
    bias_t_known_facts, evaluate_reference_captures,
)
from calibration_engine.spectral_contrast import compute_spectral_contrast, evaluate_hi_spectral_captures
import sdr_tuning

SESSION_PREFIX = "WIZARD"
HI_KINDS = ("HI_ALTO", "HI_BAJO")


def _new_session_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{SESSION_PREFIX}-{now.strftime('%Y%m%d-%H%M%S-%f')}"


def _state_path(session_dir: Path) -> Path:
    return session_dir / "wizard_state.json"


def _load_state(session_dir: Path) -> Dict[str, Any]:
    state = read_json_safe(_state_path(session_dir))
    if state is None:
        raise SystemExit(f"no wizard_state.json in {session_dir}")
    return state


def _save_state(session_dir: Path, state: Dict[str, Any]) -> None:
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(_state_path(session_dir), state)


# Same convergence tolerance INDITelescopeControl.goto() requires before it reports success; re-checked after
# the settle sleep so a capture never starts with the mount anywhere else.
ARRIVAL_TOLERANCE_DEG = 0.25


def _progress(line: str) -> None:
    """Human progress for the web job log. stderr, and never braces: the job parser takes the one JSON blob."""
    print(f"[capture-hi] {line}".replace("{", "(").replace("}", ")"), file=sys.stderr, flush=True)


def _emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _preserve_previous_attempt(cap_dir: Path, state: Dict[str, Any], label: str) -> None:
    """Files left in `cap_dir` by an earlier, interrupted attempt of this same step are MOVED to
    cap_dir/interrupted-<UTC>/ (never overwritten by the retry's capture_000.h5...) and listed in
    state["interrupted_attempts"]."""
    old = sorted(p for p in cap_dir.glob("capture_*.h5") if p.is_file())
    if not old:
        return
    dest = cap_dir / f"interrupted-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    dest.mkdir()
    for f in old:
        f.rename(dest / f.name)
    state.setdefault("interrupted_attempts", []).append(
        {"step": label, "moved_utc": datetime.now(timezone.utc).isoformat(),
         "kept_in": str(dest), "files": [f.name for f in old]})


def _record_interruption(session_dir: Path, label: str, exc: BaseException, cap_dir: Path) -> None:
    """A capture step that stops early (operator STOP = SIGINT, a hardware error, a lost connection) leaves the
    step where it was - so it can be retried or the wizard aborted - and says so in the state, with the files
    that did reach the disk (kept; a retry moves them aside, see _preserve_previous_attempt)."""
    try:
        state = _load_state(session_dir)
    except SystemExit:
        return
    kind = "STOPPED_BY_OPERATOR" if isinstance(exc, KeyboardInterrupt) else "FAILED"
    state["last_interruption"] = {
        "step": label, "kind": kind, "utc": datetime.now(timezone.utc).isoformat(),
        "error": "" if isinstance(exc, KeyboardInterrupt) else f"{type(exc).__name__}: {exc}",
        "files_on_disk": sorted(p.name for p in cap_dir.glob("capture_*.h5")),
    }
    _save_state(session_dir, state)


def _save_result_unless_aborted(session_dir: Path, state: Dict[str, Any]) -> None:
    """A capture ends by saving its result - unless the session was ABORTED meanwhile (abort from another
    process). Then the ABORTED state stays as it is: the capture files remain on disk, the result is not
    written over the abort."""
    current = _load_state(session_dir)
    if current.get("step") == WizardStep.ABORTED.value:
        raise SystemExit("the session was ABORTED while this capture ran - its files are kept on disk, the result "
                         "is not recorded")
    state.pop("last_interruption", None)
    _save_state(session_dir, state)


def _require_step(state: Dict[str, Any], *expected: WizardStep) -> None:
    values = {e.value for e in expected}
    if state["step"] not in values:
        raise SystemExit(f"wizard is at step {state['step']}, not one of {sorted(values)} - refusing")


def cmd_start(args) -> int:
    from calibration_engine.hardware_inspection import inspect_rtl_tcp_service_command_line, probe_rtl_tcp_handshake
    session_id = _new_session_id()
    session_dir = Path(args.session_root) / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    (session_dir / "captures").mkdir(exist_ok=True)
    config = WizardConfig(n_captures=args.n_captures, capture_seconds=args.capture_seconds,
                          stabilize_seconds=args.stabilize_seconds, hi_settle_seconds=args.hi_settle_seconds,
                          center_frequency_hz=args.center_freq, sample_rate_hz=args.sample_rate, gain_db=args.gain,
                          clipping_rail_hit_fraction=args.clipping_threshold,
                          stability_rms_fraction_threshold=args.stability_threshold,
                          rfi_min_usable_band_fraction=args.rfi_threshold, min_elevation_deg=args.min_elevation,
                          beam_fwhm_deg=args.beam_fwhm, contrast_significance_threshold=args.significance_threshold)
    bias_t = bias_t_known_facts(port=1234)
    # Real, read-only inspection only (handshake + argv) - never a config write, matching
    # calibration_operational_realtest.py's own established precheck pattern.
    handshake = probe_rtl_tcp_handshake("localhost", 1234, timeout=2.0)
    cmdline = inspect_rtl_tcp_service_command_line(1234)
    state = {
        "schema_version": 2, "session_id": session_id, "config": config.to_dict(),
        "step": WizardStep.PREPARE_50R.value,
        "fifty_ohm": None, "fifty_ohm_result": None,
        "reconnect_antenna_confirmed_utc": None,
        "hi_plan": None, "hi_plan_approved_utc": None,
        "hi_references": {},          # "HI_ALTO"/"HI_BAJO" -> {candidate, mount_readback, quality_result, spectral_result, capture_files, confirmed_utc, simulated}
        "spectral_contrast": None, "profile_path": None,
        "bias_t_facts": bias_t,
        "receiver_snapshot": {"handshake": handshake.to_dict(), "service_command_line": cmdline.to_dict()},
        "created_utc": datetime.now(timezone.utc).isoformat(), "aborted": False,
    }
    _save_state(session_dir, state)
    _emit({"session_id": session_id, "session_dir": str(session_dir), "state": state})
    return 0


# ------------------------------------------------------------------ 50 OHM (physical, hand-connected)
def cmd_set_reference(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.PREPARE_50R)
    ref = PhysicalReferenceConfig(kind="AMBIENT_50R", connection_point=args.connection_point,
                                  confirmed_utc=datetime.now(timezone.utc).isoformat())
    state["fifty_ohm"] = {**ref.to_dict(), "status": "PENDING_CAPTURE"}
    state["step"] = WizardStep.STABILIZE_50R.value
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def cmd_skip_reference(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.PREPARE_50R)
    state["fifty_ohm"] = {"kind": "AMBIENT_50R", "status": "SKIPPED", "reason": args.reason or "not available",
                          "confirmed_utc": datetime.now(timezone.utc).isoformat()}
    state["step"] = WizardStep.RECONNECT_ANTENNA.value
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


async def _do_50r_capture(session_dir: Path, state: Dict[str, Any], simulate: Optional[str]) -> Dict[str, Any]:
    from calibration_engine.acquisition import (
        RealCalibrationAcquisitionBackend, SimulatedCalibrationAcquisitionBackend, read_capture_iq,
    )
    cfg = WizardConfig.from_dict(state["config"])
    cap_dir = session_dir / "captures" / "AMBIENT_50R"
    cap_dir.mkdir(parents=True, exist_ok=True)
    backend: Any
    if simulate:
        from calibration_engine.simulation import NAMED_SCENARIOS, flat_noisy_config
        scenario_fn = NAMED_SCENARIOS.get(simulate, flat_noisy_config)
        backend = SimulatedCalibrationAcquisitionBackend(
            simulation_config_factory=lambda i, _fn=scenario_fn: _fn(seed=(abs(hash(("AMBIENT_50R", i))) % 10_000)))
    else:
        backend = RealCalibrationAcquisitionBackend(host="localhost", port=1234)
        await backend.connect()
        try:
            await backend.tune(cfg.center_frequency_hz, cfg.sample_rate_hz, cfg.gain_db)
        except Exception:
            await backend.close()
            raise
    iq_arrays = []
    try:
        for i in range(cfg.n_captures):
            path = cap_dir / f"capture_{i:03d}.h5"
            kwargs = dict(duration_seconds=cfg.capture_seconds, output_path=str(path),
                         center_frequency_hz=cfg.center_frequency_hz, sample_rate_hz=cfg.sample_rate_hz,
                         gain_db=cfg.gain_db)
            if simulate:
                await backend.capture(**kwargs, metadata={"reference_kind": "AMBIENT_50R", "simulated_scenario": simulate}, index=i)
            else:
                # The backend writes requested/applied/evidence attrs from its own tune() record.
                await backend.capture(**kwargs, metadata={"reference_kind": "AMBIENT_50R"})
            iq, _ = read_capture_iq(str(path))
            iq_arrays.append(iq)
    finally:
        if not simulate:
            await backend.close()
    result = evaluate_reference_captures(iq_arrays, cfg.sample_rate_hz, cfg.center_frequency_hz, cfg)
    if not simulate:
        result["tuning"] = backend.tuning
    return result


def cmd_capture_50r(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.STABILIZE_50R)
    cap_dir = session_dir / "captures" / "AMBIENT_50R"
    cap_dir.mkdir(parents=True, exist_ok=True)
    _preserve_previous_attempt(cap_dir, state, "AMBIENT_50R")
    _save_state(session_dir, state)
    try:
        result = asyncio.run(_do_50r_capture(session_dir, state, args.simulate))
    except BaseException as exc:          # STOP (KeyboardInterrupt) or failure: record, keep files, re-raise
        _record_interruption(session_dir, "AMBIENT_50R", exc, cap_dir)
        raise
    state["fifty_ohm"]["status"] = "DONE"
    state["fifty_ohm_result"] = result
    state["step"] = WizardStep.RESULT_50R.value
    _save_result_unless_aborted(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0 if result["verdict"] != "FAIL" else 2


# ------------------------------------------------------------------ transition helper (shared "move on" click)
def cmd_next(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    if state["step"] == WizardStep.RESULT_50R.value:
        state["step"] = WizardStep.RECONNECT_ANTENNA.value
    elif state["step"] == WizardStep.RESULT_HI_ALTO.value:
        state["step"] = WizardStep.READY_HI_BAJO.value
    else:
        raise SystemExit(f"wizard is at step {state['step']} - nothing to move on from with 'next'")
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


# ------------------------------------------------------------------ ANTENNA RECONNECT (safety gate before any HI step)
def cmd_confirm_antenna(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.RECONNECT_ANTENNA)
    state["reconnect_antenna_confirmed_utc"] = datetime.now(timezone.utc).isoformat()
    state["step"] = WizardStep.PLAN_HI.value
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


# ------------------------------------------------------------------ HI ALTO / HI BAJO candidate selection + approval
def cmd_plan_hi(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.PLAN_HI)
    if state.get("reconnect_antenna_confirmed_utc") is None:
        raise SystemExit("antenna reconnect/connected was never confirmed - refusing to plan a real sky pointing")
    from calibration_engine.hi_reference_selection import NoHIContrastAvailable, select_hi_alto_bajo_candidates
    cfg = WizardConfig.from_dict(state["config"])
    try:
        plan = select_hi_alto_bajo_candidates(min_elevation_deg=cfg.min_elevation_deg, beam_fwhm_deg=cfg.beam_fwhm_deg,
                                              hold_seconds=cfg.hi_hold_seconds)
    except NoHIContrastAvailable as exc:
        raise SystemExit(f"could not propose HI ALTO/HI BAJO candidates: {exc}")
    state["hi_plan"] = plan
    state["hi_plan_approved_utc"] = None      # a fresh plan always needs a fresh approval
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def cmd_approve_hi_plan(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.PLAN_HI)
    if not state.get("hi_plan"):
        raise SystemExit("no HI plan on record yet - run plan-hi first")
    state["hi_plan_approved_utc"] = datetime.now(timezone.utc).isoformat()
    state["step"] = WizardStep.READY_HI_ALTO.value
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


# ------------------------------------------------------------------ real GOTO + capture (the ONLY place this script moves the mount)
async def _capture_n_at(ra_hours: float, dec_deg: float, gain_db: float, n_captures: int, capture_seconds: float,
                        settle_seconds: float, sample_rate_hz: float, center_frequency_hz: float,
                        cap_dir: Path) -> Dict[str, Any]:
    """Real GOTO ONCE, held there for n_captures back-to-back captures (no further GOTO in between) - mirrors
    observation_gain_pilot._capture_at()'s own connect/goto/capture pattern (same classes, not reimplemented),
    extended to repeat the capture step without re-issuing GOTO, since the point does not move between captures."""
    from indi_telescope_control import INDITelescopeControl
    from sdr_capture import SDRCapture
    telescope = INDITelescopeControl("localhost", 7624, "LX200 OnStep", False)
    sdr = SDRCapture("network", "localhost", 1234, verbose=False)
    paths: List[Path] = []
    tracking_restore = False
    try:
        if not await telescope.connect():
            raise RuntimeError("INDI connection failed")
        await sdr.connect()
        try:
            tuning = await sdr_tuning.tune_explicitly(sdr, center_frequency_hz, sample_rate_hz, gain_db,
                                                      operating=sdr_tuning.operating_config())
        except sdr_tuning.TuningIncoherent as exc:
            raise RuntimeError(str(exc)) from exc
        _progress(f"SDR tuned: requested {tuning['requested']['center_frequency_hz']} Hz, "
                  f"rtl_tcp acknowledged {tuning['applied']['center_frequency_hz']} Hz")
        start_ra, start_dec = await telescope.get_coordinates(force_refresh=True)
        _progress(f"mount before GOTO: RA {start_ra} h  Dec {start_dec} deg")
        _progress(f"GOTO target: RA {ra_hours:.5f} h  Dec {dec_deg:.4f} deg")
        if not await telescope.goto(ra_hours, dec_deg):
            raise RuntimeError(f"GOTO to RA {ra_hours:.5f} h Dec {dec_deg:.4f} deg failed (mount Alert, timeout or "
                               f"no convergence) - mount last read at RA {start_ra} h Dec {start_dec} deg; NO capture taken")
        # Celestial reference: hold it with sidereal tracking during the whole hold. OnStep leaves tracking OFF
        # after a GOTO (found 2026-10-01: the readback RA drifted at the sidereal rate during the captures).
        tracking_before = await telescope.get_tracking_state(timeout=2.0)
        if not await telescope.set_track_mode("sidereal") or not await telescope.wait_track_mode("sidereal", timeout=10.0):
            raise RuntimeError("SIDEREAL track mode not confirmed by the mount - NO capture taken")
        if tracking_before != "on":
            tracking_restore = True
            if not await telescope.set_tracking(True) or not await telescope.wait_tracking_state(expected_on=True, timeout=10.0):
                raise RuntimeError("tracking ON not confirmed by the mount after GOTO - NO capture taken")
        _progress(f"GOTO converged; sidereal tracking ON confirmed (was {tracking_before}); "
                  f"settling {settle_seconds:g} s before capturing")
        await asyncio.sleep(settle_seconds)
        mount_ra, mount_dec = await telescope.get_coordinates(force_refresh=True)
        arrival_error_deg = (None if mount_ra is None or mount_dec is None
                             else telescope._angular_distance_deg(mount_ra, mount_dec, ra_hours, dec_deg))
        if arrival_error_deg is None or arrival_error_deg > ARRIVAL_TOLERANCE_DEG:
            raise RuntimeError(f"mount is not at the target after settling: readback RA {mount_ra} h Dec {mount_dec} deg, "
                               f"error {arrival_error_deg} deg > {ARRIVAL_TOLERANCE_DEG} deg - NO capture taken")
        slew_deg = (None if start_ra is None or start_dec is None
                    else telescope._angular_distance_deg(start_ra, start_dec, mount_ra, mount_dec))
        _progress(f"mount at target after settle: RA {mount_ra} h  Dec {mount_dec} deg  error {arrival_error_deg:.4f} deg  "
                  f"slewed {slew_deg} deg; capturing")
        hold: List[Dict[str, Any]] = []

        async def _hold_sample(phase: str) -> float:
            ra, dec = await telescope.get_coordinates(force_refresh=True)
            state = await telescope.get_tracking_state(timeout=2.0)
            err = None if ra is None or dec is None else telescope._angular_distance_deg(ra, dec, ra_hours, dec_deg)
            hold.append({"phase": phase, "utc": datetime.now(timezone.utc).isoformat(), "ra_hours": ra,
                         "dec_deg": dec, "error_deg": err, "tracking": state})
            if err is None or err > ARRIVAL_TOLERANCE_DEG or state != "on":
                raise RuntimeError(f"target NOT held at {phase}: error {err} deg (tolerance {ARRIVAL_TOLERANCE_DEG}), "
                                   f"tracking {state} - captures stopped after {len(paths)} of {n_captures}")
            return err

        for i in range(n_captures):
            await _hold_sample(f"before capture {i}")
            path = cap_dir / f"capture_{i:03d}.h5"
            await sdr.capture(capture_seconds, str(path), int(sample_rate_hz),
                              {**sdr_tuning.tuning_attrs(tuning), "gain_requested_db": gain_db,
                               "purpose": "calibrate_wizard_hi", "target_ra_hours": ra_hours, "target_dec_deg": dec_deg,
                               "tracking": "sidereal_on_confirmed"})
            paths.append(path)
            err = await _hold_sample(f"after capture {i}")
            _progress(f"capture {i} done; target held, error {err:.4f} deg")
        end_ra, end_dec = hold[-1]["ra_hours"], hold[-1]["dec_deg"]
        return {"commanded_ra_hours": ra_hours, "commanded_dec_deg": dec_deg,
               "mount_ra_hours": mount_ra, "mount_dec_deg": mount_dec, "gain_db": gain_db,
               "pre_goto_ra_hours": start_ra, "pre_goto_dec_deg": start_dec, "slew_distance_deg": slew_deg,
               "arrival_error_deg": arrival_error_deg, "arrival_tolerance_deg": ARRIVAL_TOLERANCE_DEG,
               "settle_seconds": settle_seconds, "goto_busy_seconds": telescope.last_slew_busy_duration_sec,
               "tracking": {"mode": "sidereal", "state_before_goto_hold": tracking_before,
                            "max_hold_error_deg": max(h["error_deg"] for h in hold), "samples": hold},
               "post_capture_ra_hours": end_ra, "post_capture_dec_deg": end_dec,
               "capture_seconds": capture_seconds, "sample_rate_hz": sample_rate_hz,
               "center_frequency_hz": center_frequency_hz, "captured_utc": datetime.now(timezone.utc).isoformat(),
               "capture_paths": [str(p) for p in paths], "simulated": False, "tuning": tuning}
    finally:
        await sdr.close()
        if tracking_restore:     # leave tracking as it was before this hold (OFF): best effort, reported
            try:
                if not (await telescope.set_tracking(False) and await telescope.wait_tracking_state(expected_on=False, timeout=10.0)):
                    _progress("WARNING: could not confirm tracking OFF after the hold")
            except Exception as exc:  # noqa: BLE001 - never mask the original outcome
                _progress(f"WARNING: tracking OFF after the hold failed: {type(exc).__name__}: {exc}")
        await telescope.disconnect()


async def _simulate_capture_n_at(ra_hours: float, dec_deg: float, gain_db: float, n_captures: int,
                                 capture_seconds: float, sample_rate_hz: float, center_frequency_hz: float,
                                 cap_dir: Path, scenario: str) -> Dict[str, Any]:
    """Offline-testing only, real hardware never touched: writes real HDF5 files (same iq_data/attrs shape a
    real capture has) via calibration_engine.simulation - the SAME instrument-quality scenarios (HEALTHY/
    CLIPPED/THERMAL_DRIFT/RFI_CONTAMINATED) the 50 ohm step already uses. None of these scenarios contains a
    synthetic HI line (they model instrument noise characteristics, not astrophysics) - a simulated HI ALTO/HI
    BAJO run therefore always and correctly reports INCONCLUSIVE spectral contrast; this is the honest behavior
    to test (the wizard never claims a contrast that is not really in the data)."""
    from calibration_engine.simulation import NAMED_SCENARIOS, flat_noisy_config, simulate_capture_iq
    import h5py
    scenario_fn = NAMED_SCENARIOS.get(scenario, flat_noisy_config)
    paths: List[Path] = []
    for i in range(n_captures):
        cfg = scenario_fn(seed=abs(hash((ra_hours, dec_deg, i))) % 10_000)
        iq = simulate_capture_iq(cfg)
        path = cap_dir / f"capture_{i:03d}.h5"
        with h5py.File(path, "w") as handle:
            handle.create_dataset("iq_data", data=iq, compression="gzip", compression_opts=4)
            handle.attrs.update(capture_status="success", simulated=True, simulated_scenario=scenario,
                                center_frequency_hz=center_frequency_hz, sample_rate_hz=sample_rate_hz,
                                gain_requested_db=gain_db, duration_seconds=capture_seconds,
                                created_at=datetime.now(timezone.utc).isoformat())
        paths.append(path)
    return {"commanded_ra_hours": ra_hours, "commanded_dec_deg": dec_deg, "mount_ra_hours": ra_hours,
           "mount_dec_deg": dec_deg, "gain_db": gain_db, "capture_seconds": capture_seconds,
           "sample_rate_hz": sample_rate_hz, "center_frequency_hz": center_frequency_hz,
           "captured_utc": datetime.now(timezone.utc).isoformat(), "capture_paths": [str(p) for p in paths],
           "simulated": True, "simulated_scenario": scenario}


def cmd_capture_hi(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    label = args.label
    if label not in HI_KINDS:
        raise SystemExit(f"--label must be one of {HI_KINDS}")
    expected_step = WizardStep.READY_HI_ALTO if label == "HI_ALTO" else WizardStep.READY_HI_BAJO
    _require_step(state, expected_step)
    if not state.get("hi_plan_approved_utc"):
        raise SystemExit("the HI plan was never approved - refusing to move")
    cand = state["hi_plan"]["candidates"][label]
    cfg = WizardConfig.from_dict(state["config"])

    if not args.simulate:
        # PLAN vigente: re-check, right now, that this real point still clears min_elevation for the whole
        # hold - never trust a candidate computed earlier without re-verifying it still holds at GOTO time
        # (the same discipline ALIGN's own RUN and OBSERVE's gain-pilot capture already apply before moving).
        from calibration_engine.hi_reference_selection import point_stays_above_elevation
        fresh = point_stays_above_elevation(cand["ra_hours"], cand["dec_deg"], cfg.min_elevation_deg, cfg.hi_hold_seconds)
        if not fresh["clears"]:
            raise SystemExit(f"{label} PLAN is no longer valid: real altitude check now gives margin "
                             f"{fresh['margin_deg']:+.1f} deg over the {cfg.hi_hold_seconds:g} s hold - "
                             f"PLAN the HI candidates again ({fresh})")
    else:
        fresh = {"skipped": True, "reason": "altitude re-check skipped for --simulate (no real sky/mount involved)"}

    cap_dir = session_dir / "captures" / label
    cap_dir.mkdir(parents=True, exist_ok=True)
    _preserve_previous_attempt(cap_dir, state, label)
    _save_state(session_dir, state)
    try:
        if args.simulate:
            capture = asyncio.run(_simulate_capture_n_at(
                cand["ra_hours"], cand["dec_deg"], cfg.gain_db, cfg.n_captures, cfg.capture_seconds,
                cfg.sample_rate_hz, cfg.center_frequency_hz, cap_dir, args.simulate))
        else:
            capture = asyncio.run(_capture_n_at(
                cand["ra_hours"], cand["dec_deg"], cfg.gain_db, cfg.n_captures, cfg.capture_seconds,
                cfg.hi_settle_seconds, cfg.sample_rate_hz, cfg.center_frequency_hz, cap_dir))
    except RuntimeError as exc:
        # The step is left at READY_HI_*: the operator sees why and can retry (files that were written are kept).
        _record_interruption(session_dir, label, exc, cap_dir)
        raise SystemExit(f"{label} movement FAILED: {exc}")
    except BaseException as exc:          # STOP (KeyboardInterrupt) or any other failure: record, keep files
        _record_interruption(session_dir, label, exc, cap_dir)
        raise

    from calibration_engine.acquisition import read_capture_iq
    paths = [Path(p) for p in capture["capture_paths"]]
    iq_arrays = [read_capture_iq(str(p))[0] for p in paths]
    quality_result = evaluate_reference_captures(iq_arrays, cfg.sample_rate_hz, cfg.center_frequency_hz, cfg)
    spectral_result = evaluate_hi_spectral_captures(paths)

    state["hi_references"][label] = {
        "candidate": cand, "capture": capture, "quality_result": quality_result, "spectral_result": spectral_result,
        "pre_goto_altitude_check": fresh, "confirmed_utc": datetime.now(timezone.utc).isoformat(),
    }
    state["step"] = (WizardStep.RESULT_HI_ALTO if label == "HI_ALTO" else WizardStep.RESULT_HI_BAJO).value
    _save_result_unless_aborted(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0 if quality_result["verdict"] != "FAIL" else 2


def cmd_abort(args) -> int:
    """Ends the session from any step. Everything already on disk (captures, results, interrupted attempts) is
    kept; nothing is deleted. A new session can be started right after. Aborting a DONE session is refused (its
    profile is already built) and aborting twice is a no-op."""
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    if state["step"] == WizardStep.DONE.value:
        raise SystemExit("this session is already DONE (profile built) - nothing to abort; start a new session")
    if state["step"] != WizardStep.ABORTED.value:
        state["aborted_from_step"] = state["step"]
        state["aborted_utc"] = datetime.now(timezone.utc).isoformat()
    state["step"] = WizardStep.ABORTED.value
    state["aborted"] = True
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def _build_and_write_profile(session_dir: Path, state: Dict[str, Any]):
    from calibration_engine.profile import OperationalEnvelope, build_draft_profile, write_profile
    from calibration_engine.receiver_config import KNOWN_RECEIVERS
    cfg = state["config"]
    quality_results = [state["fifty_ohm_result"]] if state["fifty_ohm_result"] else []
    for label in HI_KINDS:
        ref = state["hi_references"].get(label)
        if ref:
            quality_results.append(ref["quality_result"])
    if not quality_results:
        return None
    worst_clip = "OK"
    usable_fracs = []
    for r in quality_results:
        inputs = r["quality"]["inputs"]
        worst_clip = max(worst_clip, inputs["clipping_status"], key=lambda s: ["OK", "WARNING", "CLIPPED", "UNKNOWN"].index(s))
        if inputs.get("usable_band_fraction") is not None:
            usable_fracs.append(inputs["usable_band_fraction"])
    envelope = OperationalEnvelope(
        gain_range_tested_db=[cfg["gain_db"]], temperature_range_observed_c=None,
        clipping_maximum_observed=worst_clip,
        stability_rms_fraction_expected_max=cfg["stability_rms_fraction_threshold"],
        usable_band_fraction=(min(usable_fracs) if usable_fracs else 0.0), known_artifacts=[])
    limitations = [
        "DRAFT - not reviewed (profile activation is a separate, future, explicit step)",
        "REDUCE may use ONLY this profile's recommended_gain_db and receiver identity",
        "calibration_level stays OPERATIONAL_RELATIVE - this profile carries NO Y-factor, noise temperature, "
        "Kelvin or absolute-gain figure of any kind",
    ]
    if state["fifty_ohm"] and state["fifty_ohm"]["status"] == "SKIPPED":
        limitations.append("50 ohm reference not tested (skipped)")
    contrast = state.get("spectral_contrast")
    if contrast:
        if contrast["verdict"] == "DEFENSIBLE_CONTRAST":
            limitations.append(f"HI ALTO vs HI BAJO RELATIVE spectral contrast: DEFENSIBLE ({contrast['reason']})")
        else:
            limitations.append(f"HI ALTO vs HI BAJO spectral contrast: INCONCLUSIVE ({contrast['reason']})")
    profile = build_draft_profile(
        receiver_id="MAIN", receiver_serial=KNOWN_RECEIVERS["MAIN"].serial,
        source_calibration_sessions=[state["session_id"]],
        conditions={"references_tested": ([state["fifty_ohm"]["kind"]] if state["fifty_ohm"] and state["fifty_ohm"]["status"] == "DONE" else [])
                                        + [k for k in HI_KINDS if k in state["hi_references"]],
                   "bias_t_facts": state["bias_t_facts"], "spectral_contrast": contrast},
        recommended_gain_db=cfg["gain_db"], recommended_usable_band=None, known_masks=[],
        warmup_recommendation={"stabilize_seconds": cfg["stabilize_seconds"]}, envelope=envelope,
        limitations=limitations)
    return write_profile(profile, session_dir.parent / "profiles")


def _build_observe_profile(session_dir: Path) -> Dict[str, Any]:
    """Automatically builds and verifies the OBSERVE/REDUCE-consumable RELATIVE profile (.json+.npz) from
    THIS session's real 50 ohm captures - calibration_engine.wizard_profile_converter does the real work
    (validation, DSP, the write-then-reload-through-OBSERVE's-own-loader check with cleanup on failure);
    nothing is reimplemented here. Deliberately separate from _build_and_write_profile's own operational
    DRAFT profile above (a diagnostic summary only - no .npz, not directly loadable by
    calibration_foundation.load_calibration_profile - see its own "DRAFT" limitations entry): the two must
    never be confused for each other, which is exactly why the web UI shows them as two distinct results.
    Returns {"status": "READY", ...path/report...} or {"status": "UNAVAILABLE", "reason": <exact cause>} -
    FINISH must show this honestly either way, never an ambiguous success."""
    from calibration_engine.wizard_profile_converter import WizardProfileError, build_profile_from_wizard
    stem = session_dir / "observe_profile" / "calibration_profile_v1"
    try:
        built = build_profile_from_wizard(session_dir, stem, fft_size=8192)
    except WizardProfileError as exc:
        return {"status": "UNAVAILABLE", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never let an unexpected error look like ambiguous success
        return {"status": "UNAVAILABLE", "reason": f"unexpected error building the OBSERVE profile: {type(exc).__name__}: {exc}"}
    return {
        "status": "READY",
        "profile_json_path": str(stem.with_suffix(".json")),
        "profile_npz_path": str(stem.with_suffix(".npz")),
        "profile_path_for_observe": str(stem.with_suffix(".json")),
        "verified_by_observe_loader": True,       # build_profile_from_wizard already re-loaded it through
                                                   # calibration_foundation.load_calibration_profile() - the
                                                   # exact function OBSERVE's own quicklook_spectrum.py calls
                                                   # first - and deletes the pair instead of returning if that fails.
        "report": built["report"],
    }


def cmd_finish(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _require_step(state, WizardStep.RESULT_HI_BAJO)
    cfg = WizardConfig.from_dict(state["config"])
    alto = state["hi_references"]["HI_ALTO"]["spectral_result"]
    bajo = state["hi_references"]["HI_BAJO"]["spectral_result"]
    contrast = compute_spectral_contrast(alto, bajo, significance_threshold=cfg.contrast_significance_threshold,
                                         min_usable_fraction=cfg.rfi_min_usable_band_fraction)
    state["spectral_contrast"] = contrast.to_dict()
    profile_path = _build_and_write_profile(session_dir, state)
    state["profile_path"] = str(profile_path) if profile_path else None
    state["observe_profile"] = _build_observe_profile(session_dir)
    state["step"] = WizardStep.DONE.value
    _save_state(session_dir, state)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def cmd_status(args) -> int:
    session_dir = Path(args.session_dir)
    state = _load_state(session_dir)
    _emit({"session_dir": str(session_dir), "state": state})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("--session-root", default="data/calibration")
    s.add_argument("--n-captures", type=int, default=5)
    s.add_argument("--capture-seconds", type=float, default=2.0)
    s.add_argument("--stabilize-seconds", type=float, default=20.0)
    s.add_argument("--hi-settle-seconds", type=float, default=2.0)
    s.add_argument("--center-freq", type=float, default=float(sdr_tuning.operating_center_frequency_hz()))
    s.add_argument("--sample-rate", type=float, default=2_400_000.0)
    s.add_argument("--gain", type=float, default=40.2)
    s.add_argument("--clipping-threshold", type=float, default=1.0e-4)
    s.add_argument("--stability-threshold", type=float, default=0.10)
    s.add_argument("--rfi-threshold", type=float, default=0.5)
    s.add_argument("--min-elevation", type=float, default=20.0)
    s.add_argument("--beam-fwhm", type=float, default=20.0)
    s.add_argument("--significance-threshold", type=float, default=3.0)
    s.set_defaults(func=cmd_start)

    sr = sub.add_parser("set-reference")
    sr.add_argument("--session-dir", required=True)
    sr.add_argument("--connection-point", required=True, choices=["LNA_INPUT", "LNA_OUTPUT", "SDR_INPUT"])
    sr.set_defaults(func=cmd_set_reference)

    sk = sub.add_parser("skip-reference")
    sk.add_argument("--session-dir", required=True)
    sk.add_argument("--reason", default=None)
    sk.set_defaults(func=cmd_skip_reference)

    c50 = sub.add_parser("capture-50r")
    c50.add_argument("--session-dir", required=True)
    c50.add_argument("--simulate", default=None,
                     help="offline-testing only: HEALTHY|CLIPPED|THERMAL_DRIFT|RFI_CONTAMINATED - never used for a real reference")
    c50.set_defaults(func=cmd_capture_50r)

    n = sub.add_parser("next")
    n.add_argument("--session-dir", required=True)
    n.set_defaults(func=cmd_next)

    ca = sub.add_parser("confirm-antenna")
    ca.add_argument("--session-dir", required=True)
    ca.set_defaults(func=cmd_confirm_antenna)

    ph = sub.add_parser("plan-hi")
    ph.add_argument("--session-dir", required=True)
    ph.set_defaults(func=cmd_plan_hi)

    ap = sub.add_parser("approve-hi-plan")
    ap.add_argument("--session-dir", required=True)
    ap.set_defaults(func=cmd_approve_hi_plan)

    ch = sub.add_parser("capture-hi")
    ch.add_argument("--session-dir", required=True)
    ch.add_argument("--label", required=True, choices=list(HI_KINDS))
    ch.add_argument("--simulate", default=None,
                    help="offline-testing only: HEALTHY|CLIPPED|THERMAL_DRIFT|RFI_CONTAMINATED - skips the real GOTO/altitude "
                         "re-check entirely; never used for a real HI ALTO/HI BAJO measurement")
    ch.set_defaults(func=cmd_capture_hi)

    a = sub.add_parser("abort")
    a.add_argument("--session-dir", required=True)
    a.set_defaults(func=cmd_abort)

    f = sub.add_parser("finish")
    f.add_argument("--session-dir", required=True)
    f.set_defaults(func=cmd_finish)

    st = sub.add_parser("status")
    st.add_argument("--session-dir", required=True)
    st.set_defaults(func=cmd_status)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
