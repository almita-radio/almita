"""3rd pass, item 6: "Solo el entrypoint CLI usa asyncio.run()." A static,
permanent regression guard - not a behavioral test, but exactly as
meaningful: a future edit that sneaks an asyncio.run() into the engine,
a backend, or TrackingSession (to "quickly" bridge some sync call) would
reintroduce the nested-event-loop anti-pattern this pass was explicitly
about removing, and would not necessarily be caught by any behavioral test
(a nested asyncio.run() often still "works" in isolation - it just breaks
composability with a future async web caller, which is exactly why this
architecture decision was made).

Uses `ast` (not a plain substring search) so mentioning "asyncio.run()" in
a docstring/comment - as several of this pass's own modules do, explaining
what they deliberately do NOT do - never counts as a real call.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).parent

# Every module whose async I/O this pass explicitly wired (or re-audited)
# to have NO internal asyncio.run() / new event loop of its own.
FORBIDDEN_MODULES = [
    "alignment_engine/engine.py",
    "alignment_engine/tracking.py",
    "alignment_engine/sync_flow.py",
    "alignment_engine/mount_adapter.py",
    "alignment_engine/capture_conflict.py",
    "alignment_engine/session.py",
    "alignment_engine/fitting.py",
    "alignment_engine/scan_planner.py",
    "alignment_engine/simulation.py",
]

FORBIDDEN_CALL_NAMES = {
    ("asyncio", "run"), ("asyncio", "new_event_loop"),
}


def _find_forbidden_calls(tree: ast.AST):
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            value = node.func.value
            if isinstance(value, ast.Name) and (value.id, node.func.attr) in FORBIDDEN_CALL_NAMES:
                found.append(f"{value.id}.{node.func.attr}() at line {node.lineno}")
            # get_event_loop().run_until_complete(...) - a chained call, func.value is itself a Call
            if (node.func.attr == "run_until_complete" and isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute) and value.func.attr == "get_event_loop"):
                found.append(f"get_event_loop().run_until_complete() at line {node.lineno}")
    return found


def test_no_module_under_the_engine_hides_its_own_event_loop():
    for relative_path in FORBIDDEN_MODULES:
        tree = ast.parse((ROOT / relative_path).read_text())
        forbidden = _find_forbidden_calls(tree)
        assert not forbidden, (
            f"{relative_path} contains {forbidden} - this pass's architecture decision "
            f"requires exactly one asyncio.run() boundary, in almita_align.py's main() only"
        )


def test_cli_has_exactly_one_asyncio_run_boundary():
    tree = ast.parse((ROOT / "almita_align.py").read_text())
    calls = _find_forbidden_calls(tree)
    run_calls = [c for c in calls if c.startswith("asyncio.run(")]
    assert len(run_calls) == 1, (
        f"almita_align.py must call asyncio.run() exactly once (inside main()) - found {run_calls}, "
        "meaning either a second boundary was added (nested event loops) or the single boundary was removed"
    )


def test_cli_asyncio_run_call_is_inside_main():
    tree = ast.parse((ROOT / "almita_align.py").read_text())
    main_def = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls_in_main = _find_forbidden_calls(main_def)
    assert any(c.startswith("asyncio.run(") for c in calls_in_main), (
        "asyncio.run() must be called from within main(), not a command handler"
    )
    # and it must NOT appear anywhere else in the module
    whole_module_calls = _find_forbidden_calls(tree)
    assert len(whole_module_calls) == len(calls_in_main)
