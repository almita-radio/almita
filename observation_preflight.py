#!/usr/bin/env python3
"""Unified ALMITA Orchestrator preflight: PASS / WARNING / BLOCK, REQUIRED / OPTIONAL.

Two entry points with deliberately different depth:

- run_plan_preflight(): PLAN-time. Strictly read-only — never sends
  SET_FREQUENCY/SET_SAMPLE_RATE/SET_GAIN/Bias-T to MAIN's rtl_tcp, never
  calls SDRCapture.configure(). Reuses SDRCapture.connect()/close() (a
  connect-only handshake — rtl_tcp sends its 12-byte dongle-info header
  unconditionally, no client command involved, see sdr_capture.py:499-536)
  to confirm MAIN is alive without mutating it, and reuses
  CaptureExecutor._read_indi_preflight_properties()/INDITelescopeControl.
  get_coordinates() (pure reads) instead of the monolithic
  CaptureExecutor.run_preflight() (which also configures MAIN).

- run_execution_preflight(): RUN-time, after operator GO, right before
  spawning capture.py. Reuses CaptureExecutor.run_preflight() in full —
  the exact mount/SDR/disk/HDF5/session-persistence/grid/observer-config
  probes `capture.py --preflight-only` already performs, called in-process,
  in the same sequence capture.py's own main() uses. This IS allowed to
  configure MAIN's SDR (frequency/rate/gain) because RUN is the real
  operational path about to hand MAIN over to capture.py anyway.

Both layer on SYSTEM, OnStep-time-snapshot, RFI_REF-port, QUICKLOOK, CONSOLE,
and SOFTWARE checks that don't exist in capture.py today.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import capture as capture_module
import dual_sdr_benchmark
import observation_plan
import runtime_state
import temperature_sensors
from sdr_capture import SDRCapture

PASS, WARNING, BLOCK = "PASS", "WARNING", "BLOCK"
REQUIRED, OPTIONAL = "REQUIRED", "OPTIONAL"

DEFAULT_ONSTEP_TIME_SNAPSHOT_THRESHOLD_SEC = 60.0
DEFAULT_TRACKING_TIMEOUT_SEC = 5.0
DEFAULT_MAIN_SDR_HOST = "localhost"
DEFAULT_MAIN_SDR_PORT = 1234


def _check(name: str, category: str, criticality: str, status: str, detail: str) -> Dict[str, Any]:
    return {"name": name, "category": category, "criticality": criticality, "status": status, "detail": detail}


def _overall(checks: List[Dict[str, Any]]) -> str:
    if any(c["criticality"] == REQUIRED and c["status"] == BLOCK for c in checks):
        return BLOCK
    if any(c["status"] in (BLOCK, WARNING) for c in checks):
        return WARNING
    return PASS


def _system_checks(observer_config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    checks = []
    try:
        checks.append(_check("Hostname", "SYSTEM", OPTIONAL, PASS, socket.gethostname()))
    except Exception as exc:
        checks.append(_check("Hostname", "SYSTEM", OPTIONAL, WARNING, f"{type(exc).__name__}: {exc}"))

    now_local = datetime.now().astimezone()
    checks.append(_check(
        "Local time / UTC / timezone", "SYSTEM", OPTIONAL, PASS,
        f"local={now_local.isoformat()} utc={datetime.now(timezone.utc).isoformat()} tz={now_local.tzname()}",
    ))

    try:
        load1, load5, load15 = os.getloadavg()
        checks.append(_check("Load average", "SYSTEM", OPTIONAL, PASS, f"1m={load1:.2f} 5m={load5:.2f} 15m={load15:.2f}"))
    except Exception as exc:
        checks.append(_check("Load average", "SYSTEM", OPTIONAL, WARNING, f"unavailable: {type(exc).__name__}: {exc}"))

    sensor_ids = ((observer_config or {}).get("temperature_sensors") or {})
    if not sensor_ids:
        checks.append(_check("Temperatures", "SYSTEM", OPTIONAL, WARNING,
                              "no temperature_sensors configured in observer_config.json; skipped"))
    else:
        try:
            readings = temperature_sensors.DS18B20Reader(sensor_ids).read_all()
            checks.append(_check("Temperatures", "SYSTEM", OPTIONAL, PASS, temperature_sensors.format_temperatures(readings)))
        except Exception as exc:
            checks.append(_check("Temperatures", "SYSTEM", OPTIONAL, WARNING, f"unavailable: {type(exc).__name__}: {exc}"))

    try:
        out = subprocess.run(["dmesg", "--ctime", "--level=err,warn"], capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            checks.append(_check("Recent USB/kernel issues", "SYSTEM", OPTIONAL, WARNING,
                                  f"dmesg unavailable (exit {out.returncode}); skipped"))
        else:
            tail = "\n".join(out.stdout.strip().splitlines()[-5:])
            checks.append(_check("Recent USB/kernel issues", "SYSTEM", OPTIONAL, PASS,
                                  tail or "no recent err/warn kernel messages"))
    except Exception as exc:
        checks.append(_check("Recent USB/kernel issues", "SYSTEM", OPTIONAL, WARNING,
                              f"dmesg unavailable: {type(exc).__name__}: {exc}; skipped"))
    return checks


def _disk_check(output_root: Path, required_bytes: int) -> Dict[str, Any]:
    try:
        free = shutil.disk_usage(output_root if output_root.exists() else output_root.parent).free
        status = PASS if free >= required_bytes else BLOCK
        return _check("Disk space", "SYSTEM", REQUIRED, status,
                      f"free_bytes={free}, required_bytes={required_bytes}")
    except Exception as exc:
        return _check("Disk space", "SYSTEM", REQUIRED, BLOCK, f"{type(exc).__name__}: {exc}")


async def _onstep_time_snapshot_check(host: str, port: int, device_name: str,
                                       threshold_sec: float = DEFAULT_ONSTEP_TIME_SNAPSHOT_THRESHOLD_SEC
                                       ) -> Dict[str, Any]:
    """MOUNT / OnStep time snapshot — informational, WARNING-only (never BLOCK).

    LX200 OnStep.TIME_UTC.UTC is NOT a live-ticking INDI property. Read-only
    field investigation (2026-09-02) proved:
      - the value stays frozen across repeated reads seconds apart;
      - a brand-new client connection (the real production
        INDITelescopeControl.connect() path) does not refresh it either;
      - INDI's own per-message `timestamp=` XML attribute tracks when the
        *response* was generated, not when the *value* last changed — it
        cannot be used to measure property staleness;
      - the value does eventually change on the driver's own internal
        cycle, of unknown/unconfirmed cadence, never triggered by a client.
    So `abs(host_now - TIME_UTC.UTC)` is an indeterminate mix of real mount
    clock drift and unknown property staleness — it must never be reported
    as "clock drift", and must never hard-BLOCK on its own: there is no
    known threshold past which staleness can be ruled out. If a genuine
    live-clock read-only query is found in the future, a true CLOCK_DRIFT
    REQUIRED/BLOCK check can be reinstated — this is deliberately not that.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "indi_getprop", "-h", host, "-p", str(port), f"{device_name}.TIME_UTC.UTC",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        line = stdout.decode(errors="replace").strip()
        if "=" not in line:
            return _check("OnStep time snapshot", "MOUNT", OPTIONAL, WARNING,
                           "TIME_UTC property not exposed via INDI; skipped")
        _, raw_value = line.split("=", 1)
        snapshot_time = datetime.fromisoformat(raw_value.strip().replace("Z", "+00:00"))
        if snapshot_time.tzinfo is None:
            snapshot_time = snapshot_time.replace(tzinfo=timezone.utc)
        offset = abs((datetime.now(timezone.utc) - snapshot_time).total_seconds())
        if offset <= threshold_sec:
            return _check("OnStep time snapshot", "MOUNT", OPTIONAL, PASS,
                           f"TIME_UTC snapshot is within {offset:.1f}s of host UTC. "
                           "Property freshness is not guaranteed by this INDI driver.")
        return _check(
            "OnStep time snapshot", "MOUNT", OPTIONAL, WARNING,
            f"OnStep TIME_UTC snapshot differs from host UTC by {offset:.1f}s, but this INDI property "
            "has unknown freshness and has been observed remaining unchanged for minutes. Actual mount "
            "clock drift cannot be inferred from this value. Verify mount time in the INDI/OnStep "
            "operator interface before RUN.",
        )
    except Exception as exc:
        return _check("OnStep time snapshot", "MOUNT", OPTIONAL, WARNING,
                       f"TIME_UTC property not exposed via INDI; skipped ({type(exc).__name__}: {exc})")


def _rfi_ref_check(rfi_ref_cfg: Dict[str, Any], port: int) -> Dict[str, Any]:
    if not rfi_ref_cfg.get("enabled"):
        return _check("RFI_REF", "RFI_REF", OPTIONAL, PASS, "disabled by request; not checked")
    pid = dual_sdr_benchmark.listening_pid("127.0.0.1", port)
    if pid is None:
        return _check("RFI_REF", "RFI_REF", OPTIONAL, PASS, f"port {port} free")
    return _check("RFI_REF", "RFI_REF", OPTIONAL, WARNING,
                  f"port {port} already held by unrelated pid={pid}; RFI_REF will report UNAVAILABLE "
                  "without touching it (MAIN unaffected)")


def _quicklook_check(quicklook_cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not quicklook_cfg.get("enabled"):
        return _check("Quicklook", "QUICKLOOK", OPTIONAL, PASS, "disabled by request; not checked")
    profile_path = quicklook_cfg.get("calibration_profile_path")
    try:
        with open(profile_path, "r", encoding="utf-8") as handle:
            json.load(handle)
        return _check("Quicklook", "QUICKLOOK", OPTIONAL, PASS, f"calibration profile OK: {profile_path}")
    except Exception as exc:
        return _check("Quicklook", "QUICKLOOK", OPTIONAL, WARNING,
                       f"calibration profile unavailable ({profile_path}): {type(exc).__name__}: {exc}")


def _console_check() -> Dict[str, Any]:
    web_root = Path("data/console_web")
    if not web_root.exists():
        return _check("Console", "CONSOLE", OPTIONAL, WARNING, f"{web_root} not present")
    try:
        out = subprocess.run(
            ["systemctl", "is-active", "almita-console-watcher.service", "almita-console-web.service"],
            capture_output=True, text=True, timeout=5,
        )
        detail = out.stdout.strip().replace("\n", ", ") or out.stderr.strip()
        status = PASS if out.returncode == 0 else WARNING
        return _check("Console", "CONSOLE", OPTIONAL, status, detail or "systemctl reported no output")
    except Exception as exc:
        return _check("Console", "CONSOLE", OPTIONAL, WARNING,
                       f"systemctl unavailable: {type(exc).__name__}: {exc}; skipped")


def _software_check(resolved_plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    checks = []
    try:
        recomputed = observation_plan.recompute_config_hash(resolved_plan)
        match = recomputed == resolved_plan.get("observation_config_sha256")
        checks.append(_check("Software / config hash", "SOFTWARE", REQUIRED, PASS if match else BLOCK,
                              f"recomputed={recomputed[:16]}..., stored={resolved_plan.get('observation_config_sha256', '')[:16]}..."))
    except Exception as exc:
        checks.append(_check("Software / config hash", "SOFTWARE", REQUIRED, BLOCK, f"{type(exc).__name__}: {exc}"))

    mosaic_csv = Path(resolved_plan.get("mosaic_csv_path", ""))
    checks.append(_check("Software / mosaic.csv present", "SOFTWARE", REQUIRED,
                          PASS if mosaic_csv.exists() else BLOCK, str(mosaic_csv)))
    return checks


# --------------------------------------------------------------------------
# MAIN (rtl_tcp) — read-only checks only. Never .configure(). See module
# docstring: rtl_tcp's dongle-info handshake on connect() requires zero
# client commands (sdr_capture.py:499-536), so a bare connect()+close() is
# a legitimate "is MAIN alive and responding" probe with zero mutation.
# --------------------------------------------------------------------------

def _rtl_tcp_service_check() -> Dict[str, Any]:
    try:
        out = subprocess.run(["systemctl", "is-active", "rtl_tcp.service"],
                              capture_output=True, text=True, timeout=5)
        detail = out.stdout.strip() or out.stderr.strip()
        status = PASS if out.returncode == 0 else BLOCK
        return _check("MAIN / rtl_tcp.service", "MAIN", REQUIRED, status, detail or "systemctl reported no output")
    except Exception as exc:
        return _check("MAIN / rtl_tcp.service", "MAIN", REQUIRED, WARNING,
                       f"systemctl unavailable: {type(exc).__name__}: {exc}; skipped (informational only)")


def _listening_address_candidates(host: str, port: int) -> List[str]:
    """Resolve host to the literal address form(s) `ss -lntp` actually prints
    (e.g. "localhost" -> "127.0.0.1", and "[::1]" for IPv6) — read-only DNS/
    hosts-file resolution via the stdlib, no network I/O to the SDR itself.
    Lets dual_sdr_benchmark.listening_pid()'s exact-substring match (which we
    do not modify — it's an existing, tested, reused function) be checked
    against what the system will really show, instead of the raw hostname
    the caller happened to pass in."""
    candidates: List[str] = []
    seen = set()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        # Not just socket.gaierror: empirically, a name that fails to
        # resolve on this system's NSS chain (hosts: files mdns4_minimal
        # dns) can raise a plain OSError (observed: errno 16, "Device or
        # resource busy", via the mdns4_minimal NSS plugin) rather than the
        # "textbook" gaierror. gaierror is itself an OSError subclass, so
        # this still catches that case too — strictly broader, not looser.
        return candidates
    for family, _socktype, _proto, _canonname, sockaddr in infos:
        addr = sockaddr[0]
        if addr in seen:
            continue
        seen.add(addr)
        candidates.append(f"[{addr}]" if family == socket.AF_INET6 else addr)
    return candidates


def _main_port_check(host: str, port: int) -> Dict[str, Any]:
    candidates = _listening_address_candidates(host, port)
    for addr in candidates:
        pid = dual_sdr_benchmark.listening_pid(addr, port)
        if pid is not None:
            return _check("MAIN / port listening", "MAIN", REQUIRED, PASS,
                          f"{addr}:{port} (resolved from {host!r}) owned by pid={pid}")
    tried = ", ".join(candidates) or "(host did not resolve)"
    return _check("MAIN / port listening", "MAIN", REQUIRED, BLOCK,
                  f"nothing listening on {host}:{port} — checked resolved address(es): {tried}")


async def _main_sdr_presence_check(host: str, port: int) -> Dict[str, Any]:
    """Connect-only presence probe: opens the TCP socket, reads the 12-byte
    dongle-info handshake rtl_tcp sends unconditionally, then closes.
    Never calls SDRCapture.configure() — no SET_FREQUENCY/SET_SAMPLE_RATE/
    SET_GAIN/Bias-T command is ever sent."""
    probe = None
    try:
        probe = SDRCapture(mode="network", host=host, port=port, verbose=False)
        await probe.connect()
        return _check("MAIN / SDR presence", "MAIN", REQUIRED, PASS,
                      f"rtl_tcp dongle-info handshake OK at {host}:{port} (read-only, not configured)")
    except Exception as exc:
        return _check("MAIN / SDR presence", "MAIN", REQUIRED, BLOCK, f"{type(exc).__name__}: {exc}")
    finally:
        if probe is not None:
            try:
                await probe.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# INDI — read-only checks only. Mirrors (does not call) the pass/fail
# criteria capture.py's own run_preflight() applies to the same two reads
# (get_coordinates + _read_indi_preflight_properties), kept in sync
# manually since capture.py is never modified. The underlying INDI
# protocol/property-reading code itself is 100% reused, not reimplemented.
# --------------------------------------------------------------------------

async def _indi_read_only_check(executor: "capture_module.CaptureExecutor") -> Dict[str, Any]:
    if executor.telescope is None:
        return _check("INDI/mount", "MOUNT", REQUIRED, BLOCK, "controller is not connected")
    try:
        ra, dec = await executor.telescope.get_coordinates(force_refresh=True)
        if ra is None or dec is None or not math.isfinite(float(ra)) or not math.isfinite(float(dec)) \
                or not -90 <= float(dec) <= 90:
            raise ValueError(f"invalid coordinates RA={ra}, DEC={dec}")
        try:
            properties = await executor._read_indi_preflight_properties()
        except Exception as exc:
            return _check("INDI/mount", "MOUNT", REQUIRED, WARNING,
                           f"coordinates OK but optional state properties unavailable: {type(exc).__name__}: {exc}")
        eod = properties.get("EQUATORIAL_EOD_COORD._STATE")
        active = [key for key in ("TELESCOPE_MOTION_NS.MOTION_NORTH", "TELESCOPE_MOTION_NS.MOTION_SOUTH",
                                   "TELESCOPE_MOTION_WE.MOTION_WEST", "TELESCOPE_MOTION_WE.MOTION_EAST")
                  if properties.get(key) == "On"]
        if eod == "Alert" or active:
            raise RuntimeError(f"EOD={eod}, active_motion={active}")
        home = f"HOME={properties.get('TELESCOPE_HOME._STATE')}/GO={properties.get('TELESCOPE_HOME.GO')} (informational only)"
        return _check("INDI/mount", "MOUNT", REQUIRED, PASS,
                       f"valid coordinates RA={float(ra):.6f}, DEC={float(dec):.6f}; {home}")
    except Exception as exc:
        return _check("INDI/mount", "MOUNT", REQUIRED, BLOCK, f"{type(exc).__name__}: {exc}")


async def run_plan_preflight(resolved_plan: Dict[str, Any], *, host: str = "localhost", port: int = 7624,
                              device_name: Optional[str] = None,
                              sdr_host: str = DEFAULT_MAIN_SDR_HOST, sdr_port: int = DEFAULT_MAIN_SDR_PORT,
                              onstep_time_snapshot_threshold_sec: float =
                              DEFAULT_ONSTEP_TIME_SNAPSHOT_THRESHOLD_SEC) -> Dict[str, Any]:
    """PLAN-time preflight. Strictly read-only: zero hardware mutation.

    Never calls SDRCapture.configure() (no SET_FREQUENCY/SET_SAMPLE_RATE/
    SET_GAIN/Bias-T), never GOTOs, never touches tracking. Connects to INDI
    and to MAIN's rtl_tcp only to read state, then disconnects both.
    """
    checks: List[Dict[str, Any]] = []
    checks.extend(_system_checks(resolved_plan.get("observer_config")))
    checks.append(_disk_check(Path(resolved_plan["grid_session_dir"]), resolved_plan["storage"]["required_bytes"]))
    checks.append(_rtl_tcp_service_check())
    checks.append(_main_port_check(sdr_host, sdr_port))
    checks.append(await _main_sdr_presence_check(sdr_host, sdr_port))
    checks.append(_rfi_ref_check(resolved_plan["rfi_ref"], resolved_plan["rfi_ref"].get("port", 1235)))
    checks.append(_quicklook_check(resolved_plan["quicklook"]))
    checks.append(_console_check())
    checks.extend(_software_check(resolved_plan))

    executor = capture_module.CaptureExecutor(
        csv_path=resolved_plan["mosaic_csv_path"],
        host=host, port=port, device_name=device_name,
        config_path="observer_config.json",
        min_altitude_deg=resolved_plan["requested"]["grid"]["min_altitude_deg"],
        tracking_timeout=DEFAULT_TRACKING_TIMEOUT_SEC,
        runtime_dir=None,
    )
    if not executor.load_observation_plan(resume=False, force=False):
        checks.append(_check("Grid", "SOFTWARE", REQUIRED, BLOCK, "failed to load observation plan CSV"))
        return {"checks": checks, "overall": _overall(checks), "generated_utc": runtime_state.utcnow()}

    telescope = capture_module.INDITelescopeControl(host=host, port=port, device_name=executor.device_name, verbose=False)
    executor.telescope = telescope
    try:
        connected = await telescope.connect()
        if not connected:
            checks.append(_check("INDI/mount", "MOUNT", REQUIRED, BLOCK, "failed to connect to INDI server"))
        else:
            checks.append(await _indi_read_only_check(executor))
            checks.append(await _onstep_time_snapshot_check(
                host, port, executor.device_name, onstep_time_snapshot_threshold_sec))
    finally:
        if telescope.writer:
            telescope.writer.close()
            await telescope.writer.wait_closed()

    return {"checks": checks, "overall": _overall(checks), "generated_utc": runtime_state.utcnow()}


async def run_execution_preflight(resolved_plan: Dict[str, Any], *, host: str = "localhost", port: int = 7624,
                                   device_name: Optional[str] = None, runtime_dir: Optional[str] = None,
                                   onstep_time_snapshot_threshold_sec: float =
                                   DEFAULT_ONSTEP_TIME_SNAPSHOT_THRESHOLD_SEC) -> Dict[str, Any]:
    """RUN-time preflight (after operator GO, before spawning capture.py).

    Reuses CaptureExecutor.run_preflight() in full — including the real
    MAIN SDR connect+configure probe — because RUN is the real operational
    path about to hand MAIN over to capture.py regardless.
    """
    checks: List[Dict[str, Any]] = []
    checks.extend(_system_checks(resolved_plan.get("observer_config")))
    checks.append(_disk_check(Path(resolved_plan["grid_session_dir"]), resolved_plan["storage"]["required_bytes"]))
    checks.append(_rfi_ref_check(resolved_plan["rfi_ref"], resolved_plan["rfi_ref"].get("port", 1235)))
    checks.append(_quicklook_check(resolved_plan["quicklook"]))
    checks.append(_console_check())
    checks.extend(_software_check(resolved_plan))

    executor = capture_module.CaptureExecutor(
        csv_path=resolved_plan["mosaic_csv_path"],
        host=host, port=port, device_name=device_name,
        config_path="observer_config.json",
        sdr_mode="network", sdr_host="localhost", sdr_port=1234,
        sdr_freq=resolved_plan["main"]["center_frequency_hz"],
        sdr_sample_rate=resolved_plan["main"]["sample_rate"],
        sdr_gain_db=resolved_plan["main"]["gain_db"],
        input_topology=capture_module.INPUT_TOPOLOGIES["antenna"],
        min_altitude_deg=resolved_plan["requested"]["grid"]["min_altitude_deg"],
        tracking_timeout=DEFAULT_TRACKING_TIMEOUT_SEC,
        runtime_dir=runtime_dir,
        rfi_ref_enabled=resolved_plan["rfi_ref"]["enabled"],
        rfi_ref_gain_db=resolved_plan["rfi_ref"]["gain_db"],
        rfi_ref_serial=resolved_plan["rfi_ref"]["serial"],
    )
    if not executor.load_observation_plan(resume=False, force=False):
        checks.append(_check("Grid", "SOFTWARE", REQUIRED, BLOCK, "failed to load observation plan CSV"))
        return {"checks": checks, "overall": _overall(checks), "generated_utc": runtime_state.utcnow()}

    telescope = capture_module.INDITelescopeControl(
        host=host, port=port, device_name=executor.device_name, verbose=False,
    )
    executor.telescope = telescope
    try:
        connected = await telescope.connect()
        if not connected:
            checks.append(_check("INDI/mount", "MOUNT", REQUIRED, BLOCK, "failed to connect to INDI server"))
        else:
            capture_report = await executor.run_preflight(
                capture_time=resolved_plan["requested"]["capture"]["seconds"],
                disk_safety_factor=resolved_plan["storage"]["disk_safety_factor"],
            )
            status_map = {"PASS": PASS, "WARN": WARNING, "FAIL": BLOCK}
            for c in capture_report["checks"]:
                category = "MOUNT" if "INDI" in c["name"] else (
                    "MAIN" if "SDR" in c["name"] else (
                        "SOFTWARE" if c["name"] in ("Grid", "Observer config", "Numeric parameters") else "SYSTEM"
                    )
                )
                checks.append(_check(c["name"], category, REQUIRED, status_map.get(c["status"], BLOCK), c["detail"]))
            checks.append(await _onstep_time_snapshot_check(
                host, port, executor.device_name, onstep_time_snapshot_threshold_sec))
    finally:
        if telescope.writer:
            telescope.writer.close()
            await telescope.writer.wait_closed()

    return {"checks": checks, "overall": _overall(checks), "generated_utc": runtime_state.utcnow()}
