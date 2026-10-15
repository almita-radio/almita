"""BL-050: one process moves the mount at a time; stopping never needs the lock."""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import mount_lock
from indi_telescope_control import INDITelescopeControl

ROOT = Path(__file__).resolve().parents[1]


def _holder_process(lock_path):
    code = "import mount_lock, time; mount_lock.claim('GOTO'); print('held', flush=True); time.sleep(30)"
    env = {**os.environ, "ALMITA_MOUNT_LOCK_PATH": str(lock_path)}
    p = subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT), env=env, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_a_second_process_cannot_move_while_the_first_holds_the_mount(tmp_path):
    p = _holder_process(tmp_path / "mount.lock")
    try:
        assert mount_lock.holder()["pid"] == p.pid
        with pytest.raises(mount_lock.MountBusy, match=f"pid {p.pid}"):
            mount_lock.claim("GOTO")
    finally:
        p.kill(); p.wait()
    deadline = time.time() + 5
    while mount_lock.holder() is not None and time.time() < deadline:      # the kernel released it with the process
        time.sleep(0.05)
    mount_lock.claim("GOTO")
    assert mount_lock.holder()["this_process"] is True
    mount_lock.claim("SYNC")                                               # re-entrant inside the same process


def test_movement_commands_claim_but_tracking_off_never_does(tmp_path, monkeypatch):
    p = _holder_process(tmp_path / "mount.lock")
    try:
        t = INDITelescopeControl("localhost", 7624, "LX200 OnStep", False)
        sent = []

        async def fake_send(xml, timing_trace=None):
            sent.append(xml)
        monkeypatch.setattr(t, "_send_command", fake_send)
        monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: asyncio.Future() if False else _done())
        assert asyncio.run(t.set_tracking(False)) is True and sent              # a stop goes through
        for coro in (lambda: t.goto(10.0, -20.0), lambda: t.sync(10.0, -20.0), lambda: t.set_tracking(True),
                     lambda: t.set_track_mode("solar")):
            with pytest.raises(mount_lock.MountBusy):
                asyncio.run(coro())
    finally:
        p.kill(); p.wait()


async def _done():
    return None


def test_the_lock_can_be_switched_off_for_an_emergency(tmp_path, monkeypatch):
    p = _holder_process(tmp_path / "mount.lock")
    try:
        monkeypatch.setenv("ALMITA_MOUNT_LOCK", "off")
        mount_lock.claim("GOTO")                                           # no exception
    finally:
        p.kill(); p.wait()


def test_web_refuses_a_physical_job_while_another_process_holds_the_mount(tmp_path):
    import almita_web_ops as ops
    assert ops.mount_lock_problem() is None
    p = _holder_process(tmp_path / "mount.lock")
    try:
        assert f"pid {p.pid}" in ops.mount_lock_problem()
    finally:
        p.kill(); p.wait()
