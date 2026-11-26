#!/opt/venv/bin/python
"""ALMITA DEMO: `systemctl --user` for the container (no systemd). Units are the operator's user units, mapped to the
same commands; start = a detached process with a pidfile, stop = SIGINT (never SIGTERM - capture.py must finish its
point), is-active / show MainPID / show ActiveState answered from the pidfile. Anything else: exit 0, no output."""
import os, signal, subprocess, sys
from pathlib import Path

RUN = Path("/app/data/runtime/units")
UNITS = {"almita-experiment": ["python3", "almita_experiment.py", "run"],
         "almita-survey": ["python3", "almita_survey.py", "run"],
         "almita-transit-logger": ["python3", "transit_logger.py"],
         "almita-sdr-audio": ["python3", "sdr_audio.py"],
         "almita-temperature-logger": ["python3", "temperature_logger.py", "--interval", "20"],
         "almita-console-watcher": ["python3", "almita_console_watcher.py", "--interval", "2"]}


def pid_of(unit):
    try:
        pid = int((RUN / f"{unit}.pid").read_text())
        os.kill(pid, 0)
        # an exited child of PID 1 (the web server does not reap) stays a zombie: that is not running
        if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return None
        return pid
    except (OSError, ValueError):
        return None


def main(argv):
    args = [a for a in argv if a not in ("--user", "--no-block", "--quiet", "-q", "--no-pager")]
    if not args:
        return 0
    action, rest = args[0], args[1:]
    unit = (rest[-1] if rest else "").removesuffix(".service")
    RUN.mkdir(parents=True, exist_ok=True)
    if action == "start" and unit in UNITS:
        if pid_of(unit):
            return 0
        log = open(RUN / f"{unit}.log", "ab")
        p = subprocess.Popen(UNITS[unit], cwd="/app", stdout=log, stderr=log, start_new_session=True)
        (RUN / f"{unit}.pid").write_text(str(p.pid))
        return 0
    if action in ("stop", "kill") and unit in UNITS:
        pid = pid_of(unit)
        if pid:
            os.kill(pid, signal.SIGINT)
        return 0
    if action == "is-active":
        active = unit in UNITS and pid_of(unit)
        print("active" if active else "inactive")
        return 0 if active else 3
    if action == "show":
        pid = pid_of(unit) if unit in UNITS else None
        prop = rest[rest.index("-p") + 1] if "-p" in rest else ""
        if prop == "MainPID":
            print(pid or 0)
        elif prop == "ActiveState":
            print("active" if pid else "inactive")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
