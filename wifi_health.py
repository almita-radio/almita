#!/usr/bin/env python3
"""Passive Wi-Fi/SDIO health probe - read-only, low-cost, no active traffic.

Motivation (2026-09-17 incident): the onboard Broadcom BCM4345/6 SDIO
Wi-Fi chip went into a persistent

    brcmfmac: brcmf_sdio_txfail: sdio error, abort command and terminate frame
    brcmfmac: brcmf_sdio_dpc: sdio ctrlframe tx failed err=-84
    brcmfmac: brcmf_sdio_dpc: failed backplane access over SDIO, halting operation

loop for more than two days while CPU/RAM/temperature/fds/ALMITA ports
stayed perfectly nominal the whole time - almita-system-blackbox.log never
flagged anything wrong, because it never looked at the network stack at
all. This module exists so that a future occurrence shows up as
wifi_health=DEGRADED/FAILED on both the low-level heartbeat log
(almita_system_blackbox.py) and the live console (almita_status.json via
almita_console_watcher.py), instead of silence next to an otherwise-green
board.

Design constraints, all deliberate:
  - Every reader is independently fault-tolerant (returns None/NA rather
    than raising), mirroring almita_system_blackbox.py's read_*()
    functions - one bad read must never take the caller's whole sample
    down.
  - No active network traffic of any kind. `iw dev <if> link` only reads
    the *current* association (never `iw dev <if> scan`); no ICMP/UDP/TCP
    packet is ever sent. A "ping the gateway" check was considered and
    deliberately left out: this repo's other host-diagnostics module
    (almita_system_blackbox.py) already states as a hard invariant that it
    "never opens a socket"; a periodic active probe would be a new kind of
    side effect on the same radio medium this project does RF science on,
    for a marginal gain over the passive signals already collected here
    (carrier/operstate/association/default-route/SDIO error counters). If
    wanted later, it is a small, separate addition behind its own flag -
    not bundled into a passive-monitoring change.
  - The only two subprocesses this module ever runs (`iw`, `nmcli`) and its
    one journalctl read are all bounded by a timeout and throttled to at
    most once every MIN_SUBPROCESS_POLL_INTERVAL_S seconds *inside this
    module*, regardless of how often the caller invokes sample() - a
    watcher polling every 2s and a blackbox polling every 10s both get the
    same, flat overhead.
  - The journalctl read is never a full-journal scan. The first poll seeds
    a cursor from a short, bounded `--since` window (not the full current
    boot - which, mid-incident, could already be hundreds of thousands of
    lines); every poll after that uses `--after-cursor=<cursor>`, so cost
    stays flat and independent of total journal/incident size, and it
    naturally tolerates journald's own size-based rotation (a rotated-away
    cursor position simply yields no new matches, it does not error).
"""
from __future__ import annotations

import logging
import logging.handlers
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_INTERFACE = "wlan0"

# How often the throttled subprocess/journalctl reads are allowed to
# actually run, regardless of caller polling frequency. 30s is short enough
# to catch a burst well within the "degraded" window before it can turn
# into hours of silence, and long enough that even a caller polling every
# 2s (almita_console_watcher.py) adds no meaningful subprocess overhead.
MIN_SUBPROCESS_POLL_INTERVAL_S = 30.0

# How long a single observed backplane halt keeps the derived health at
# FAILED even if the next poll(s) see nothing new - long enough to not
# flap OK/FAILED between throttled polls, short enough that a real
# recovery (chip re-associates, no further errors) clears within a couple
# of minutes rather than being stuck FAILED forever.
BACKPLANE_HALT_STICKY_S = 90.0

# How many consecutive *executed* (non-throttled) polls with a new
# txfail/ctrlframe match in a row before classifying the condition as
# "persistent" (FAILED) rather than "isolated" (DEGRADED). At the
# MIN_SUBPROCESS_POLL_INTERVAL_S cadence, 3 means the errors kept
# recurring for at least ~60-90s of real time, not a single blip.
PERSISTENT_ERROR_POLL_THRESHOLD = 3

# Minimum gap between two reminder lines for the *same* ongoing error kind
# in the event log, so a chip stuck erroring every ~6s does not flood
# wifi_events.log with one line per occurrence.
DEFAULT_REMINDER_INTERVAL_S = 300.0

_IW_PATH = shutil.which("iw")
_NMCLI_PATH = shutil.which("nmcli")
_JOURNALCTL_PATH = shutil.which("journalctl")

SDIO_TXFAIL = "sdio_txfail"
SDIO_CTRLFRAME_FAIL = "sdio_ctrlframe_fail"
SDIO_BACKPLANE_HALT = "sdio_backplane_halt"
FIRMWARE_HALTED = "firmware_halted"

# Order matters: more specific/severe patterns are checked in this order,
# but each kernel line only ever increments one counter (first match wins)
# so a single "failed backplane access" line is never double-counted.
_KERNEL_PATTERNS = (
    (SDIO_BACKPLANE_HALT, re.compile(r"failed backplane access over SDIO")),
    (FIRMWARE_HALTED, re.compile(r"brcmf_sdio_hostmail:.*firmware halted", re.IGNORECASE)),
    (SDIO_CTRLFRAME_FAIL, re.compile(r"sdio ctrlframe tx failed")),
    (SDIO_TXFAIL, re.compile(r"brcmf_sdio_txfail")),
)

_EMPTY_LINK: Dict[str, object] = {
    "associated": None, "bssid": None, "ssid": None,
    "freq_mhz": None, "signal_dbm": None, "tx_mbps": None, "rx_mbps": None,
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Cheap, every-cycle, /sys and /proc readers - no subprocess, no throttling.
# --------------------------------------------------------------------------

def wlan_present(interface: str = DEFAULT_INTERFACE, sys_root: Path = Path("/sys/class/net")) -> bool:
    return (sys_root / interface).exists()


def read_carrier(interface: str = DEFAULT_INTERFACE, sys_root: Path = Path("/sys/class/net")) -> Optional[int]:
    """1 = link carrier detected, 0 = not detected. None if unreadable (some
    drivers raise EINVAL on this file while the interface is fully down -
    that is itself informative and is treated the same as "0" by callers
    that care, via read_operstate() instead)."""
    try:
        return int((sys_root / interface / "carrier").read_text().strip())
    except (OSError, ValueError):
        return None


def read_operstate(interface: str = DEFAULT_INTERFACE, sys_root: Path = Path("/sys/class/net")) -> Optional[str]:
    try:
        return (sys_root / interface / "operstate").read_text().strip()
    except OSError:
        return None


def read_default_route_present(path: Path = Path("/proc/net/route")) -> Optional[bool]:
    """True if /proc/net/route has an entry whose destination is 0.0.0.0
    (any interface) - pure /proc parsing, no `ip route` subprocess."""
    try:
        lines = path.read_text().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "00000000":
            return True
    return False


# --------------------------------------------------------------------------
# Throttled, subprocess-based readers - association detail and NM state.
# --------------------------------------------------------------------------

_IW_LINE_PATTERNS = {
    "bssid": re.compile(r"^Connected to ([0-9a-fA-F:]{17})"),
    "ssid": re.compile(r"^\s*SSID:\s*(.+)$"),
    "freq": re.compile(r"^\s*freq:\s*(\d+)"),
    "signal": re.compile(r"^\s*signal:\s*(-?\d+)\s*dBm"),
    "tx_bitrate": re.compile(r"^\s*tx bitrate:\s*([\d.]+)\s*MBit/s"),
    "rx_bitrate": re.compile(r"^\s*rx bitrate:\s*([\d.]+)\s*MBit/s"),
}


def iw_link_info(interface: str = DEFAULT_INTERFACE, timeout: float = 1.5) -> Optional[Dict[str, object]]:
    """One-shot `iw dev <if> link` - current association only, never a scan.

    Returns a dict with keys associated/bssid/ssid/freq_mhz/signal_dbm/
    tx_mbps/rx_mbps, or None if `iw` is not installed or the call fails or
    times out (both treated as "unknown", never raised)."""
    if not _IW_PATH:
        return None
    try:
        result = subprocess.run(
            [_IW_PATH, "dev", interface, "link"], capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stdout or ""
    if result.returncode != 0:
        return None
    if text.strip().startswith("Not connected"):
        return {**_EMPTY_LINK, "associated": False}
    info: Dict[str, object] = {**_EMPTY_LINK, "associated": True}
    for line in text.splitlines():
        for key, pattern in _IW_LINE_PATTERNS.items():
            match = pattern.match(line)
            if not match:
                continue
            if key == "bssid":
                info["bssid"] = match.group(1)
            elif key == "ssid":
                info["ssid"] = match.group(1).strip()
            elif key == "freq":
                info["freq_mhz"] = int(match.group(1))
            elif key == "signal":
                info["signal_dbm"] = int(match.group(1))
            elif key == "tx_bitrate":
                info["tx_mbps"] = float(match.group(1))
            elif key == "rx_bitrate":
                info["rx_mbps"] = float(match.group(1))
            break
    return info


def nmcli_wlan_state(interface: str = DEFAULT_INTERFACE, timeout: float = 1.5) -> Optional[str]:
    """`nmcli -t -f GENERAL.STATE device show <if>`'s value (e.g.
    "100 (connected)"). None if nmcli is not installed or the call fails -
    NetworkManager state is a convenience field, not load-bearing for the
    OK/DEGRADED/FAILED classification below."""
    if not _NMCLI_PATH:
        return None
    try:
        result = subprocess.run(
            [_NMCLI_PATH, "-t", "-f", "GENERAL.STATE", "device", "show", interface],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    line = (result.stdout or "").strip()
    return line.split(":", 1)[1] if ":" in line else (line or None)


# --------------------------------------------------------------------------
# Cumulative brcmfmac/SDIO error counters, via an incremental journalctl
# cursor - never a full journal read.
# --------------------------------------------------------------------------

@dataclass
class WifiErrorCounters:
    """Cumulative brcmfmac/SDIO error counts since this object was created
    (i.e. since the owning process started - not necessarily since kernel
    boot, if the service restarted independently of the Pi; that is an
    honest, documented limit, not a bug)."""

    counts: Dict[str, int] = field(default_factory=lambda: {k: 0 for k, _ in _KERNEL_PATTERNS})
    last_error_kind: Optional[str] = None
    last_error_message: Optional[str] = None
    last_error_utc: Optional[str] = None
    _cursor: Optional[str] = field(default=None, repr=False)
    _last_poll_monotonic: Optional[float] = field(default=None, repr=False)

    def total(self) -> int:
        return sum(self.counts.values())

    def poll(self, timeout: float = 3.0, min_interval: float = MIN_SUBPROCESS_POLL_INTERVAL_S) -> Optional[List[Dict[str, str]]]:
        """Run at most once every `min_interval` seconds of real time,
        regardless of call frequency. Returns None if this call was
        throttled (no attempt made) or journalctl is unavailable/failing;
        returns the (possibly empty) list of newly-seen matches if a real
        attempt was made - the empty-list-vs-None distinction lets a caller
        tell "checked, found nothing" apart from "didn't check this time"."""
        now = time.monotonic()
        if self._last_poll_monotonic is not None and (now - self._last_poll_monotonic) < min_interval:
            return None
        self._last_poll_monotonic = now
        if not _JOURNALCTL_PATH:
            return []

        cmd = [_JOURNALCTL_PATH, "-k", "-q", "--no-pager", "--output=short-iso", "--show-cursor"]
        if self._cursor:
            cmd.append(f"--after-cursor={self._cursor}")
        else:
            # First poll ever: seed from a short, bounded recent window -
            # deliberately NOT the full current boot, which mid-incident
            # could already be hundreds of thousands of lines. Errors from
            # before this process started are not retroactively counted;
            # only what happens from here on is.
            cmd += ["--since", "-2min"]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []  # ran, but journalctl itself failed - treated as "checked, nothing"

        new_matches: List[Dict[str, str]] = []
        for line in (result.stdout or "").splitlines():
            if line.startswith("-- cursor:"):
                self._cursor = line.split(":", 1)[1].strip()
                continue
            for kind, pattern in _KERNEL_PATTERNS:
                if pattern.search(line):
                    self.counts[kind] += 1
                    ts = line.split(" ", 1)[0] if " " in line else None
                    entry = {"kind": kind, "utc": ts, "message": line.strip()[:200]}
                    new_matches.append(entry)
                    self.last_error_kind = kind
                    self.last_error_message = entry["message"]
                    self.last_error_utc = ts
                    break
        return new_matches


# --------------------------------------------------------------------------
# Derived health state - pure function, no I/O, trivially testable.
# --------------------------------------------------------------------------

WIFI_OK = "OK"
WIFI_DEGRADED = "DEGRADED"
WIFI_FAILED = "FAILED"


def classify_wifi_health(
    *,
    present: Optional[bool],
    carrier: Optional[int],
    operstate: Optional[str],
    associated: Optional[bool],
    signal_dbm: Optional[int],
    backplane_halt_recent: bool,
    txfail_isolated: bool,
    txfail_persistent: bool,
    weak_signal_dbm: int = -80,
) -> str:
    """OK / DEGRADED / FAILED from passive signals only.

    Priority is FAILED > DEGRADED > OK. A single backplane halt is what the
    driver itself calls "halting operation", so that alone is FAILED
    regardless of anything else looking fine - this is exactly the shape
    of the 2026-09-17 incident: CPU/RAM/temp/ports all nominal, Wi-Fi
    silently dead.
    """
    if not present:
        return WIFI_FAILED
    if backplane_halt_recent or txfail_persistent:
        return WIFI_FAILED
    if associated is False:
        return WIFI_DEGRADED
    if operstate is not None and operstate not in ("up", "unknown"):
        return WIFI_DEGRADED
    if carrier == 0:
        return WIFI_DEGRADED
    if txfail_isolated:
        return WIFI_DEGRADED
    if signal_dbm is not None and signal_dbm <= weak_signal_dbm:
        return WIFI_DEGRADED
    return WIFI_OK


# --------------------------------------------------------------------------
# Event log - deduplicated, rate-limited, append-only.
# --------------------------------------------------------------------------

class WifiEventLog:
    """Appends WIFI_* event lines to `path`, never one line per occurrence.

    An ongoing condition gets exactly one line when it starts, one
    low-frequency reminder line (every `reminder_interval_s`) while it
    persists, and one line when it clears. A health-state change
    (OK/DEGRADED/FAILED) always gets its own line immediately - that
    transition itself is already low-frequency by construction."""

    def __init__(self, path: Path, reminder_interval_s: float = DEFAULT_REMINDER_INTERVAL_S,
                 max_bytes: int = 1_000_000, backup_count: int = 3):
        self.path = Path(path)
        self.reminder_interval_s = reminder_interval_s
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(f"wifi_events.{self.path}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        for existing in list(self._logger.handlers):
            self._logger.removeHandler(existing)
            existing.close()
        handler = logging.handlers.RotatingFileHandler(
            str(self.path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)

        self._last_health: Optional[str] = None
        self._condition_first_seen_utc: Dict[str, str] = {}
        self._condition_count: Dict[str, int] = {}
        self._condition_last_reminder_monotonic: Dict[str, float] = {}
        self._associated_last: Optional[bool] = None

    def _emit(self, text: str, at_utc: str) -> None:
        self._logger.info("%s %s", at_utc, text)

    def note_health(self, new_health: str, at_utc: str) -> None:
        if self._last_health is not None and new_health != self._last_health:
            self._emit(f"WIFI_STATE {self._last_health} -> {new_health}", at_utc)
        self._last_health = new_health

    def note_association(self, associated: Optional[bool], at_utc: str) -> None:
        if associated is None:
            return
        if self._associated_last is True and associated is False:
            self._emit("WIFI_ASSOC_LOST", at_utc)
        elif self._associated_last is False and associated is True:
            self._emit("WIFI_ASSOC_RESTORED", at_utc)
        self._associated_last = associated

    def note_matches(self, matches: List[Dict[str, str]], at_utc: str) -> None:
        """One entry per NEWLY-seen kernel error kind this poll. Repeats of
        an already-open condition are deduplicated into a periodic
        reminder rather than a line each; a distinct label
        (WIFI_SDIO_BACKPLANE_HALTED) is used for that specific condition
        since it is the direct signal for FAILED, not a generic SDIO_ERROR."""
        now = time.monotonic()
        seen_kinds = set()
        for match in matches:
            kind = match["kind"]
            seen_kinds.add(kind)
            self._condition_count[kind] = self._condition_count.get(kind, 0) + 1
            if kind not in self._condition_first_seen_utc:
                self._condition_first_seen_utc[kind] = at_utc
                self._condition_last_reminder_monotonic[kind] = now
                label = "WIFI_SDIO_BACKPLANE_HALTED" if kind == SDIO_BACKPLANE_HALT else f"WIFI_SDIO_ERROR {kind}"
                self._emit(f"{label} first_seen={at_utc} message=\"{match['message']}\"", at_utc)
        for kind in seen_kinds:
            last_reminder = self._condition_last_reminder_monotonic.get(kind, 0.0)
            if (now - last_reminder) >= self.reminder_interval_s and self._condition_count[kind] > 1:
                self._emit(
                    f"WIFI_SDIO_ERROR {kind} ongoing count={self._condition_count[kind]} "
                    f"since={self._condition_first_seen_utc[kind]}",
                    at_utc,
                )
                self._condition_last_reminder_monotonic[kind] = now

    def note_recovery_if_clear(self, health: str, at_utc: str) -> None:
        """Once health is back to OK, close out any open error conditions
        so a later recurrence is reported as a fresh "first_seen" event
        rather than silently folded into stale counters."""
        if health != WIFI_OK or not self._condition_first_seen_utc:
            return
        for kind, count in self._condition_count.items():
            self._emit(f"WIFI_SDIO_RECOVERED {kind} total_count={count}", at_utc)
        self._condition_first_seen_utc.clear()
        self._condition_count.clear()
        self._condition_last_reminder_monotonic.clear()

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)


# --------------------------------------------------------------------------
# Top-level convenience: one object, one sample() call, used identically by
# almita_system_blackbox.py (heartbeat log line) and
# almita_console_watcher.py (almita_status.json "wifi" section) - a single
# shared implementation rather than two independent readers of the same
# thing (the DS18B20 dual-reader duplication in this repo is the known
# cautionary example this deliberately avoids).
# --------------------------------------------------------------------------

class WifiMonitor:
    def __init__(self, interface: str = DEFAULT_INTERFACE, events_log_path: Optional[Path] = None,
                 min_subprocess_poll_interval: float = MIN_SUBPROCESS_POLL_INTERVAL_S,
                 reminder_interval_s: float = DEFAULT_REMINDER_INTERVAL_S):
        self.interface = interface
        self._min_interval = min_subprocess_poll_interval
        self._counters = WifiErrorCounters()
        self._last_iw_poll_monotonic: Optional[float] = None
        self._cached_link: Dict[str, object] = dict(_EMPTY_LINK)
        self._cached_nm_state: Optional[str] = None
        self._events = (
            WifiEventLog(Path(events_log_path), reminder_interval_s=reminder_interval_s)
            if events_log_path is not None else None
        )
        self._backplane_halt_until_monotonic: float = 0.0
        self._consecutive_txfail_polls = 0

    def close(self) -> None:
        if self._events is not None:
            self._events.close()

    def sample(self, now_utc: Optional[str] = None) -> Dict[str, object]:
        now_utc = now_utc or utcnow_iso()
        present = wlan_present(self.interface)
        carrier = read_carrier(self.interface) if present else None
        operstate = read_operstate(self.interface) if present else None
        default_route = read_default_route_present()

        now = time.monotonic()
        if present and (
            self._last_iw_poll_monotonic is None
            or (now - self._last_iw_poll_monotonic) >= self._min_interval
        ):
            self._cached_link = iw_link_info(self.interface) or dict(_EMPTY_LINK)
            self._cached_nm_state = nmcli_wlan_state(self.interface)
            self._last_iw_poll_monotonic = now
        link = self._cached_link

        poll_result = self._counters.poll(min_interval=self._min_interval) if present else []
        poll_ran = poll_result is not None
        new_matches = poll_result or []

        error_matches = [m for m in new_matches if m["kind"] in (SDIO_TXFAIL, SDIO_CTRLFRAME_FAIL)]
        if any(m["kind"] == SDIO_BACKPLANE_HALT for m in new_matches):
            self._backplane_halt_until_monotonic = now + BACKPLANE_HALT_STICKY_S
        if error_matches:
            self._consecutive_txfail_polls += 1
        elif poll_ran:  # a real (non-throttled) poll just ran clean
            self._consecutive_txfail_polls = 0
        backplane_halt_recent = now < self._backplane_halt_until_monotonic
        txfail_persistent = self._consecutive_txfail_polls >= PERSISTENT_ERROR_POLL_THRESHOLD
        txfail_isolated = 0 < self._consecutive_txfail_polls < PERSISTENT_ERROR_POLL_THRESHOLD

        health = classify_wifi_health(
            present=present, carrier=carrier, operstate=operstate,
            associated=link.get("associated"), signal_dbm=link.get("signal_dbm"),
            backplane_halt_recent=backplane_halt_recent,
            txfail_isolated=txfail_isolated, txfail_persistent=txfail_persistent,
        )

        if self._events is not None:
            self._events.note_association(link.get("associated"), now_utc)
            self._events.note_matches(new_matches, now_utc)
            self._events.note_health(health, now_utc)
            self._events.note_recovery_if_clear(health, now_utc)

        return {
            "interface": self.interface,
            "wlan_present": present,
            "wlan_carrier": carrier,
            "wlan_operstate": operstate,
            "wlan_associated": link.get("associated"),
            "wlan_ssid": link.get("ssid"),
            "wlan_bssid": link.get("bssid"),
            "wlan_freq_mhz": link.get("freq_mhz"),
            "wlan_signal_dbm": link.get("signal_dbm"),
            "wlan_tx_mbps": link.get("tx_mbps"),
            "wlan_rx_mbps": link.get("rx_mbps"),
            "networkmanager_state": self._cached_nm_state,
            "default_route_present": default_route,
            "sdio_txfail_total": self._counters.counts[SDIO_TXFAIL],
            "sdio_ctrlframe_fail_total": self._counters.counts[SDIO_CTRLFRAME_FAIL],
            "sdio_backplane_halt_total": self._counters.counts[SDIO_BACKPLANE_HALT],
            "sdio_error_total": self._counters.total(),
            "wifi_health": health,
            "last_wifi_error_kind": self._counters.last_error_kind,
            "last_wifi_error_message": self._counters.last_error_message,
            "last_wifi_error_utc": self._counters.last_error_utc,
        }
