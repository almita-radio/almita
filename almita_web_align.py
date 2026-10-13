"""ALIGN web route handlers for almita_orchestrator_server.py.

Every handler calls almita_align.py's OWN functions (_engine_for, cmd_plan,
cmd_preflight, run_solar_simulated/run_hi_simulated) or alignment_engine
directly - never a reimplementation. No fit/offset/quality/sync-eligibility
math happens in this file or in any frontend JS; this module only builds
the args object the CLI would have built from argv, and shapes the result
as JSON.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

import almita_align
from almita_web_common import JOBS, envelope, get_sdr_resource_status, list_sessions, new_job_id, resolve_within_root
from alignment_engine.deployment_state import DEFAULT_STATE_PATH, read_current_deployment_state
from alignment_engine.hi.sync_policy import AlignmentPhase, evaluate_phase_gate
from alignment_engine.session import AlignmentSession

SESSION_ROOT = "data/alignment"


def _args(mode: str, session: Optional[str] = None, **overrides) -> SimpleNamespace:
    """Same defaults almita_align.py's own argparse would set for `plan`/
    `preflight`/`run` - kept in exactly one place (this function) so the
    web path can never silently drift from the CLI's own defaults."""
    defaults = dict(
        config=None, observer_config=None, json=True, min_altitude=None,
        catalog="data/hi_sky_catalog_2000pts.csv", session=session,
        simulate=True, dry_run=False, true_offset_east=0.0, true_offset_north=0.0,
        fwhm=20.0, gain=1.0, baseline=0.0, noise=0.02, seed=1,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def get_status() -> Dict[str, Any]:
    deployment = read_current_deployment_state(DEFAULT_STATE_PATH)
    resource = get_sdr_resource_status()
    hi_sync = evaluate_phase_gate(AlignmentPhase.FIRST_LIGHT_HI, "N/A (FIRST_LIGHT_HI never evaluates eligibility)")
    return envelope({
        "deployment_state": deployment.state.value if deployment else "UNKNOWN",
        "resource": resource.to_dict(),
        "hi": {"phase": "FIRST_LIGHT_HI", "sync": hi_sync.to_dict()},
        "solar": {"sync_authorized": False,
                  "reason": "real solar SYNC has not been separately authorized in this codebase"},
        "sessions": {"solar": list_sessions(SESSION_ROOT, limit=8, prefix="SOLAR-"),
                     "hi": list_sessions(SESSION_ROOT, limit=8, prefix="HI-")},
    })


def plan(mode: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if mode not in ("solar", "hi"):
        return envelope(blocked=True, reason=f"unknown mode: {mode}")
    args = _args(mode, min_altitude=body.get("min_altitude"))
    engine = almita_align._engine_for(mode, args, resume=False)
    engine.plan()
    return envelope({"session_id": engine.session.session_id, "state": engine.state_machine.state.value})


def _validate_session_id(session_id: Optional[str]) -> Optional[str]:
    """Fase 57: session_id flows into alignment_engine.session.AlignmentSession
    (via almita_align._engine_for), whose __init__ does mkdir(parents=True,
    exist_ok=True) on Path(output_root) / session_id - an unvalidated
    absolute path would let that mkdir land anywhere on disk, not just
    inside data/alignment. Returns the bare (validated) directory name, or
    None if it does not resolve inside SESSION_ROOT."""
    if not session_id:
        return None
    resolved = resolve_within_root(SESSION_ROOT, session_id)
    return resolved.name if resolved is not None else None


async def preflight(mode: str, body: Dict[str, Any]) -> Dict[str, Any]:
    session_id = _validate_session_id(body.get("session_id"))
    if not session_id:
        return envelope(blocked=True, reason="session_id is required and must resolve inside data/alignment")
    args = _args(mode, session=session_id, catalog=body.get("catalog", "data/hi_sky_catalog_2000pts.csv"))
    engine = almita_align._engine_for(mode, args, resume=True)
    provider = (almita_align.SolarTarget(engine.location) if mode == "solar"
                else almita_align.SyntheticHIReferenceProvider(args.catalog))
    checks = await engine.preflight(provider)
    ok = all(c.ok for c in checks)
    return envelope({"session_id": engine.session.session_id, "ok": ok,
                      "checks": [vars(c) for c in checks]}, blocked=not ok,
                     reason=None if ok else "one or more preflight checks failed - see checks[]")


def run_simulation(mode: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Starts a background simulated run (Fase 30: job id == session id,
    poll GET /api/align/session/{id}). Real hardware is never reachable
    from this endpoint - only run_*_simulated()."""
    if mode not in ("solar", "hi"):
        return envelope(blocked=True, reason=f"unknown mode: {mode}")
    session_id = _validate_session_id(body.get("session_id"))
    if not session_id:
        return envelope(blocked=True, reason="session_id is required (call plan first) and must resolve inside data/alignment")
    args = _args(
        mode, session=session_id, catalog=body.get("catalog", "data/hi_sky_catalog_2000pts.csv"),
        true_offset_east=float(body.get("true_offset_east", 0.0)), true_offset_north=float(body.get("true_offset_north", 0.0)),
        fwhm=float(body.get("fwhm", 20.0)), gain=float(body.get("gain", 1.0)),
        baseline=float(body.get("baseline", 0.0)), noise=float(body.get("noise", 0.02)),
        seed=int(body.get("seed", 1)),
    )

    if JOBS.is_alive(session_id):
        # double click / second tab: never start a second thread on the same session (backend is the authority, not the button state)
        return envelope(blocked=True, reason=f"a run for session {session_id} is already in progress")

    def _job() -> None:
        import asyncio
        try:
            asyncio.run(almita_align.cmd_run(mode, args))
        except Exception:
            pass  # the engine's own session.write_state()/on_event already records failure; nothing to add here

    JOBS.start(session_id, _job)
    return envelope({"session_id": session_id, "state": "STARTED"})


def get_session(session_id: str) -> Dict[str, Any]:
    from pathlib import Path
    root = Path(SESSION_ROOT).resolve()
    candidate = (root / session_id).resolve()
    if candidate != root and root not in candidate.parents:
        return envelope(blocked=True, reason="invalid session id")  # Fase 57: no path traversal
    if not candidate.is_dir():
        return envelope(blocked=True, reason=f"no session found: {session_id}")
    session = AlignmentSession(SESSION_ROOT, session_id)
    state = session.read_state()
    result = session.read_alignment_result()
    return envelope({"session_id": session_id, "state": state, "result": result,
                      "job_running": JOBS.is_alive(session_id)})


def replay(body: Dict[str, Any]) -> Dict[str, Any]:
    """HI only - matches alignment_engine/hi/replay.py's own scope."""
    from alignment_engine.hi.replay import replay_session
    session_dir = body.get("session_dir")
    if not session_dir:
        return envelope(blocked=True, reason="session_dir is required")
    resolved = resolve_within_root(SESSION_ROOT, session_dir)
    if resolved is None:
        return envelope(blocked=True, reason="session_dir must resolve inside data/alignment")  # Fase 57
    result = replay_session(str(resolved), bootstrap_iterations=int(body.get("bootstrap_iterations", 20)),
                             label=body.get("label"))
    return envelope(result.to_dict())


def compare(body: Dict[str, Any]) -> Dict[str, Any]:
    """HI only - matches alignment_engine/hi/repeatability.py's own scope."""
    from alignment_engine.hi.repeatability import AlignmentResultSummary, compare_alignment_results
    analysis_dir_a, analysis_dir_b = body.get("analysis_dir_a"), body.get("analysis_dir_b")
    if not analysis_dir_a or not analysis_dir_b:
        return envelope(blocked=True, reason="analysis_dir_a and analysis_dir_b are required")
    resolved_a = resolve_within_root(SESSION_ROOT, analysis_dir_a)
    resolved_b = resolve_within_root(SESSION_ROOT, analysis_dir_b)
    if resolved_a is None or resolved_b is None:
        return envelope(blocked=True, reason="analysis directories must resolve inside data/alignment")  # Fase 57
    try:
        summary_a = AlignmentResultSummary.from_analysis_dir(str(resolved_a), target_label=body.get("target_label_a"))
        summary_b = AlignmentResultSummary.from_analysis_dir(str(resolved_b), target_label=body.get("target_label_b"))
    except (FileNotFoundError, ValueError) as exc:
        return envelope(blocked=True, reason=str(exc))
    result = compare_alignment_results(summary_a, summary_b)
    return envelope(result.to_dict())


def sync_prepare(mode: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if mode == "hi":
        gate = evaluate_phase_gate(AlignmentPhase.FIRST_LIGHT_HI, "N/A")
        return envelope(blocked=True, reason=gate.reason)
    return envelope(blocked=True, reason="real solar SYNC has not been separately authorized in this codebase")


def sync_apply(mode: str, body: Dict[str, Any]) -> Dict[str, Any]:
    # Never reachable with a passing gate today - prepare() above already
    # blocks both modes unconditionally; apply() re-checks independently
    # rather than trusting that prepare() was actually called first.
    return sync_prepare(mode, body)
