"""System health / version glue for the :8090 web app (almita_orchestrator_server.py).

READ-ONLY and PASSIVE: reads /proc/net/tcp (listening ports, no connection), sysfs USB serials, the watcher's almita_status.json,
the orchestrator's own runtime files and this process's job table. It never connects to rtl_tcp or INDI, never moves the mount and
never touches an SDR. It only REPORTS what the existing components already know; anything the read-only stack does not expose
(the mount state, for one) is reported as NOT_EXPOSED instead of being invented.

Three layers are kept apart on purpose (a running API does not mean an operational instrument):
  services      the processes that serve the web (observe_api, field_console, console_watcher)
  dependencies  what they need (INDI server, mount, MAIN SDR, RFI SDR)
  operational   READY / NOT_READY, derived only from the two layers above, with the reasons
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from runtime_state import read_json_safe

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = ROOT / "data" / "runtime"
PORT_FIELD_CONSOLE, PORT_MAIN_RTL, PORT_INDI, PORT_OBSERVE_API = 8088, 1234, 7624, 8090
WATCHER_STALE_SECONDS = 10.0
HEALTH_CACHE_SECONDS = 2.0
STARTED_UTC = datetime.now(timezone.utc).isoformat(timespec="seconds")
PROJECT_URL = "https://github.com/almita-radio/almita"
AUTHOR = "Felipe Fridman"

_version_lock = threading.Lock()
_version_cache: Dict[str, Any] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def git_short_sha(root: Path = ROOT) -> str:
    """argv, no shell, short timeout; 'unknown' when git or the repository is unavailable."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def version_info(component: str = "observe_api") -> Dict[str, Any]:
    """Build/runtime identification for the footer/status pages. No secrets, no paths, no e-mail."""
    with _version_lock:
        if "git" not in _version_cache:
            _version_cache["git"] = git_short_sha()
        git = _version_cache["git"]
    return {"project": "ALMITA", "author": AUTHOR, "project_url": PROJECT_URL, "component": component, "git_short_sha": git,
            "started_utc": STARTED_UTC, "hostname": socket.gethostname(), "transport": "HTTP (LAN, no TLS, no authentication)"}


def listening_ports() -> Set[int]:
    """TCP ports in LISTEN state, from /proc (passive; opens no connection)."""
    ports: Set[int] = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in Path(name).read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[3] == "0A":
                    ports.add(int(fields[1].rsplit(":", 1)[1], 16))
        except OSError:
            continue
    return ports


def usb_rtlsdr_serials() -> Set[str]:
    """Serial numbers of attached Realtek (0bda) dongles, read from sysfs. Passive."""
    serials: Set[str] = set()
    try:
        for dev in Path("/sys/bus/usb/devices").glob("*"):
            try:
                if (dev / "idVendor").read_text().strip() == "0bda" and (dev / "serial").exists():
                    serials.add((dev / "serial").read_text().strip())
            except OSError:
                continue
    except OSError:
        pass
    return serials


def _age_seconds(iso: Optional[str], now: datetime) -> Optional[float]:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return None


def liveness() -> Dict[str, Any]:
    """Process alive ONLY. Says nothing about INDI, the mount or the SDRs (see collect_health)."""
    return {"ok": True, "status": "ALIVE", "message": "process alive; dependencies are reported by /api/system/health",
            "data": {"alive": True, "component": "observe_api", "utc": _utc_now().isoformat(timespec="seconds")}}


def _default_status_reader(runtime_dir: Path) -> Dict[str, Any]:
    return read_json_safe(Path(runtime_dir) / "almita_status.json") or {}


def _default_orchestrator() -> Dict[str, Any]:
    import observation_orchestrator
    return observation_orchestrator.get_status()


def _default_resource(calibration_active: bool) -> Dict[str, Any]:
    from almita_web_common import get_sdr_resource_status
    return get_sdr_resource_status(calibration_active).to_dict()


def collect_health(*, runtime_dir: Path = DEFAULT_RUNTIME_DIR, now: Optional[datetime] = None,
                   ports: Optional[Callable[[], Set[int]]] = None, usb: Optional[Callable[[], Set[str]]] = None,
                   status_reader: Optional[Callable[[Path], Dict[str, Any]]] = None,
                   orchestrator: Optional[Callable[[], Dict[str, Any]]] = None,
                   resource: Optional[Callable[[bool], Dict[str, Any]]] = None, jobs: Any = None) -> Dict[str, Any]:
    now = now or _utc_now()
    listening = (ports or listening_ports)()
    serials = (usb or usb_rtlsdr_serials)()
    status = (status_reader or _default_status_reader)(Path(runtime_dir)) or {}
    if jobs is None:
        from almita_web_common import JOBS as jobs
    problems: Dict[str, str] = {}

    # ---- services
    services: Dict[str, Dict[str, Any]] = {"observe_api": {"state": "UP", "detail": f"answering this request (port {PORT_OBSERVE_API})"}}
    services["field_console"] = ({"state": "UP", "detail": f"port {PORT_FIELD_CONSOLE} listening"} if PORT_FIELD_CONSOLE in listening
                                 else {"state": "DOWN", "detail": f"nothing listening on port {PORT_FIELD_CONSOLE}"})
    age = _age_seconds(status.get("updated_utc"), now)
    if not status:
        services["console_watcher"] = {"state": "DOWN", "detail": "no almita_status.json (watcher not running or never wrote)"}
    elif age is None or age > WATCHER_STALE_SECONDS:
        services["console_watcher"] = {"state": "STALE", "detail": f"almita_status.json is {age:.0f} s old" if age is not None else "almita_status.json has no timestamp",
                                       "age_seconds": age}
    else:
        services["console_watcher"] = {"state": "UP", "detail": f"status {age:.0f} s old", "age_seconds": age}

    # ---- workflows (what THIS instrument is doing)
    try:
        orch = (orchestrator or _default_orchestrator)() or {}
    except Exception as exc:  # noqa: BLE001 - a broken runtime file must not take the health view down
        orch = {}
        problems["observation"] = f"{type(exc).__name__}: {exc}"
    orch_state = ((orch.get("orchestrator") or {}).get("orchestrator_state")) or "UNKNOWN"
    acq_state = (status.get("acquisition") or {}).get("state")
    workflows = {
        "observation": {"state": orch_state, "detail": f"acquisition: {acq_state or 'IDLE'}", "session_id": (orch.get("orchestrator") or {}).get("session_id")},
        "calibration": {"state": "RUNNING" if jobs.any_alive(prefix="CAL-") else "IDLE", "detail": "simulation jobs of this web app"},
        "alignment": {"state": "RUNNING" if (jobs.any_alive(prefix="SOLAR-") or jobs.any_alive(prefix="HI-")) else "IDLE", "detail": "simulation jobs of this web app"},
    }
    if "observation" in problems:
        workflows["observation"]["detail"] = f"runtime state unreadable: {problems['observation']}"

    # ---- dependencies
    deps: Dict[str, Dict[str, Any]] = {}
    deps["indi"] = ({"state": "UP", "detail": f"server port {PORT_INDI} listening (not connected to by the web)"} if PORT_INDI in listening
                    else {"state": "DOWN", "detail": f"nothing listening on port {PORT_INDI}"})
    deps["mount"] = {"state": "NOT_EXPOSED", "detail": "the read-only web stack does not connect to the mount; use the bench or the INDI client to read it"}
    try:
        res = (resource or _default_resource)(workflows["calibration"]["state"] == "RUNNING")
    except Exception as exc:  # noqa: BLE001
        res = {"status": "UNKNOWN", "detail": f"resource check failed: {exc}"}
    r_status = res.get("status")
    if PORT_MAIN_RTL not in listening:
        deps["main_sdr"] = {"state": "DOWN", "detail": f"rtl_tcp not listening on port {PORT_MAIN_RTL}"}
    elif r_status == "FREE":
        deps["main_sdr"] = {"state": "AVAILABLE", "detail": res.get("detail") or "no conflicting acquisition found"}
    elif r_status and r_status.startswith("CLAIMED"):
        deps["main_sdr"] = {"state": "BUSY", "detail": f"{r_status}: {res.get('detail')}"}
    else:
        deps["main_sdr"] = {"state": "UNKNOWN", "detail": res.get("detail") or "could not determine"}
    deps["main_sdr"]["usb_serial_00000001_present"] = "00000001" in serials
    rfi = (status.get("rfi_ref") or {}).get("status")
    rfi_present = "00000002" in serials
    if rfi in ("DISABLED", None):
        deps["rfi_sdr"] = {"state": "AVAILABLE" if rfi_present else "DOWN", "detail": "USB serial 00000002 present" if rfi_present else "USB serial 00000002 not attached (optional)",
                           "session_status": rfi or "DISABLED"}
    elif rfi in ("FAILED", "UNAVAILABLE"):
        deps["rfi_sdr"] = {"state": "DOWN", "detail": f"RFI_REF {rfi}", "session_status": rfi}
    else:
        deps["rfi_sdr"] = {"state": "AVAILABLE", "detail": f"RFI_REF {rfi}", "session_status": rfi}
    deps["rfi_sdr"]["optional"] = True

    # ---- operational readiness: derived only from the layers above
    reasons = []
    if services["observe_api"]["state"] != "UP":
        reasons.append("observe API not up")
    if deps["indi"]["state"] != "UP":
        reasons.append("INDI server not listening")
    if deps["main_sdr"]["state"] == "DOWN":
        reasons.append("MAIN SDR (rtl_tcp) not listening")
    elif deps["main_sdr"]["state"] == "BUSY":
        reasons.append("MAIN SDR busy: " + deps["main_sdr"]["detail"])
    elif deps["main_sdr"]["state"] == "UNKNOWN":
        reasons.append("MAIN SDR state unknown")
    if orch_state in ("RUNNING", "STOPPING", "PREFLIGHT"):
        reasons.append(f"an observation is {orch_state}")
    soft = []
    if services["console_watcher"]["state"] != "UP":
        soft.append(f"console watcher {services['console_watcher']['state']}")
    if services["field_console"]["state"] != "UP":
        soft.append("field console down")
    level = "NOT_READY" if reasons else ("DEGRADED" if soft else "READY")
    return {"generated_utc": now.isoformat(timespec="seconds"), "services": services, "dependencies": deps, "workflows": workflows,
            "operational": {"level": level, "ready": level == "READY", "reasons": reasons, "degraded_by": soft,
                            "note": "service UP is not operational READY: the dependencies decide"},
            "version": version_info()}


_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"t": 0.0, "value": None}


def cached_health(**kw) -> Dict[str, Any]:
    """Short cache so several open pages do not multiply the /proc + sysfs reads."""
    with _cache_lock:
        if kw or _cache["value"] is None or time.monotonic() - _cache["t"] > HEALTH_CACHE_SECONDS:
            value = collect_health(**kw)
            if not kw:
                _cache.update(t=time.monotonic(), value=value)
            return value
        return _cache["value"]
