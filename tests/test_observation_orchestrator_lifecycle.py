"""Web API lifecycle != Observation lifecycle — regression tests for Blocker 2.

Proves (without hardware, without capture.py, without INDI) that a child
launched via the exact production observation_orchestrator._popen_detached()
mechanism survives a `systemctl restart` of its parent service, PROVIDED the
unit declares KillMode=process — and dies under systemd's default
KillMode=control-group despite start_new_session=True. This is the same
empirical result manually verified during the forensic review (see
systemd/almita-observe-api.service's KillMode=process comment).

The live systemd-run test spins up transient --user units (no root, no
persistent unit files, auto-cleaned) — it's skipped gracefully if this
environment has no usable systemd --user session (e.g. some CI sandboxes).
"""
import subprocess
import time
from pathlib import Path

import pytest

import observation_orchestrator as orch

UNIT_FILE = Path(__file__).parent.parent / "systemd" / "almita-observe-api.service"


def _systemd_user_available() -> bool:
    try:
        out = subprocess.run(["systemctl", "--user", "status"], capture_output=True, timeout=5)
        return out.returncode == 0
    except Exception:
        return False


def test_production_unit_declares_killmode_process():
    """Regression guard: nobody should remove this directive without noticing
    — it's the entire fix for Blocker 2, not an incidental setting."""
    text = UNIT_FILE.read_text()
    assert "KillMode=process" in text


def _run_transient_dummy(unit_name: str, kill_mode: str, harness_script: Path, status_file: Path):
    subprocess.run(["systemctl", "--user", "reset-failed", unit_name], capture_output=True)
    cmd = ["systemd-run", "--user", f"--unit={unit_name}", "--collect"]
    if kill_mode:
        cmd += ["-p", f"KillMode={kill_mode}"]
    cmd += ["/usr/bin/env", "python3", str(harness_script)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=10)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not status_file.exists():
        time.sleep(0.2)
    assert status_file.exists(), "harness did not report its status in time"
    fields = dict(line.split("=", 1) for line in status_file.read_text().splitlines() if "=" in line)
    return int(fields["dummy_pid"])


def _pid_alive(pid: int) -> bool:
    try:
        import os
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.mark.skipif(not _systemd_user_available(), reason="no usable systemd --user session in this environment")
def test_detached_child_dies_under_default_killmode_but_survives_killmode_process(tmp_path):
    """The A/B proof: same _popen_detached() launch mechanism, same restart
    trigger — only KillMode differs."""
    status_file = tmp_path / "status.txt"
    harness = tmp_path / "harness.py"
    harness.write_text(f"""
import sys, os, time
sys.path.insert(0, {str(Path(__file__).parent.parent)!r})
import observation_orchestrator as orch
with open({str(tmp_path / "dummy.log")!r}, "wb") as log:
    proc = orch._popen_detached(["sleep", "60"], log)
with open({str(status_file)!r}, "w") as f:
    f.write(f"harness_pid={{os.getpid()}}\\ndummy_pid={{proc.pid}}\\n")
while True:
    time.sleep(1)
""")

    default_unit = "almita-lifecycle-test-default"
    process_unit = "almita-lifecycle-test-process"
    try:
        # 1) Default KillMode (control-group, systemd's implicit default —
        #    what almita-observe-api.service would have without the fix).
        dummy_pid_default = _run_transient_dummy(default_unit, "", harness, status_file)
        assert _pid_alive(dummy_pid_default)
        subprocess.run(["systemctl", "--user", "restart", f"{default_unit}.service"], check=True, timeout=10)
        time.sleep(2)
        assert not _pid_alive(dummy_pid_default), (
            "expected the dummy child to be killed under default KillMode=control-group "
            "(this is the exact failure Blocker 2 identified)"
        )
    finally:
        subprocess.run(["systemctl", "--user", "stop", f"{default_unit}.service"], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "reset-failed", default_unit], capture_output=True)
        if _pid_alive(dummy_pid_default):
            subprocess.run(["kill", str(dummy_pid_default)], capture_output=True)

    status_file.unlink(missing_ok=True)
    dummy_pid_fixed = None
    try:
        # 2) KillMode=process (the fix): the child must survive a restart.
        dummy_pid_fixed = _run_transient_dummy(process_unit, "process", harness, status_file)
        assert _pid_alive(dummy_pid_fixed)
        subprocess.run(["systemctl", "--user", "restart", f"{process_unit}.service"], check=True, timeout=10)
        time.sleep(2)
        assert _pid_alive(dummy_pid_fixed), (
            "expected the dummy child to SURVIVE a restart under KillMode=process — "
            "this is the property Blocker 2 requires"
        )
    finally:
        # The restart's whole point is that it spawns a *second* orphaned
        # dummy too (the new harness instance's own child) — both the
        # pre-restart survivor and this new one need explicit cleanup,
        # since neither is killed by `systemctl stop` under KillMode=process.
        pids_to_kill = set()
        if dummy_pid_fixed is not None:
            pids_to_kill.add(dummy_pid_fixed)
        if status_file.exists():
            fields = dict(line.split("=", 1) for line in status_file.read_text().splitlines() if "=" in line)
            if "dummy_pid" in fields:
                pids_to_kill.add(int(fields["dummy_pid"]))
        subprocess.run(["systemctl", "--user", "stop", f"{process_unit}.service"], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "reset-failed", process_unit], capture_output=True)
        for pid in pids_to_kill:
            if _pid_alive(pid):
                subprocess.run(["kill", str(pid)], capture_output=True)
