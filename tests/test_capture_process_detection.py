"""'Is capture.py running?' must mean a process EXECUTING capture.py - never a command line that only mentions it
(pytest on tests/test_*capture*.py, sdr_capture.py, a shell -c line, grep). Those used to raise a false capture
conflict that blocked the SDR resource and the web's physical gates."""
import os

import pytest

import almita_console_watcher as watcher
from alignment_engine.capture_conflict import _scan_proc_for_cmdline_needle
from runtime_state import is_script_cmdline


def _cmd(*argv):
    return b"\0".join(a.encode() for a in argv) + b"\0"


@pytest.mark.parametrize("argv", [
    ("/home/stellarmate/almita/.venv/bin/python", "capture.py", "--sdr-freq", "1420405752"),
    ("python3", "-u", "/home/stellarmate/almita/capture.py", "--plan", "mosaic.csv"),
    ("/usr/bin/python3.11", "-B", "./capture.py"),
    ("/home/stellarmate/almita/capture.py", "--plan", "x.csv"),
])
def test_a_process_executing_capture_py_is_detected(argv):
    assert is_script_cmdline(_cmd(*argv))


@pytest.mark.parametrize("argv", [
    (".venv/bin/python", "-m", "pytest", "-q", "tests/test_capture_onstep_guard.py", "capture.py"),
    (".venv/bin/python", "-m", "pytest", "tests/test_sdr_capture.py"),
    ("python3", "sdr_capture.py"),
    ("python3", "tests/test_capture.py"),
    ("/bin/bash", "-c", "pgrep -af capture.py; ls capture.py"),
    ("grep", "-n", "capture.py", "notes.md"),
    ("python3", "-c", "import capture; print('capture.py')"),
    ("vim", "capture.py"),
])
def test_command_lines_that_only_mention_capture_py_are_not_a_capture(argv):
    assert not is_script_cmdline(_cmd(*argv))


def _fake_proc(tmp_path, argvs):
    for pid, argv in enumerate(argvs, start=100):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(_cmd(*argv))
    return tmp_path


def test_both_scanners_ignore_pytest_and_find_the_real_capture(tmp_path):
    proc = _fake_proc(tmp_path, [(".venv/bin/python", "-m", "pytest", "tests/test_sdr_capture.py", "-k", "capture.py")])
    assert _scan_proc_for_cmdline_needle("capture.py", proc_root=str(proc)) is None
    assert watcher.find_capture_process(proc) is False
    (proc / "200").mkdir()
    (proc / "200" / "cmdline").write_bytes(_cmd(".venv/bin/python", "capture.py", "--plan", "m.csv"))
    assert _scan_proc_for_cmdline_needle("capture.py", proc_root=str(proc)) == 200
    assert watcher.find_capture_process(proc) is True


def test_this_very_pytest_process_is_not_a_capture():
    with open(f"/proc/{os.getpid()}/cmdline", "rb") as fh:
        assert not is_script_cmdline(fh.read())
