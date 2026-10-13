"""Operating SDR configuration: one source, explicit tuning before every capture, honest evidence.

Single source of truth: observer_config.json -> observation_defaults (center_frequency_hz, sample_rate_hz,
gain_db). operating_config() is the only reader; there is no literal fallback anywhere - a missing or invalid
value raises instead of silently substituting a stale number (the 1420405000-vs-1420405752 incidents of
2026-09-28 and 2026-10-01 both came from fallbacks/defaults that drifted from this file).

Explicit tuning: rtl_tcp keeps whatever tuning the PREVIOUS client left, and its startup argv (-f/-s/-g) only
describes the state before any client retuned it. So every acquisition calls tune_explicitly() on its own
connection before capturing: it forces rtl_tcp commands 0x01 (frequency), 0x02 (sample rate), 0x03 (manual
gain mode), 0x04 (gain), then waits for rtl_tcp's own acknowledgement lines ("set freq N", "set sample rate N",
"set gain N") in its log, emitted after it received each command.

Evidence vocabulary (never "readback"): rtl_tcp has NO frequency readback, and it prints "set freq N" BEFORE
calling rtlsdr_set_center_freq() without reporting its return value. The strongest evidence available is
therefore RTL_TCP_SERVER_ACK: the server received and dispatched exactly these values on this connection.
A tuner PLL that failed to lock is reported by librtlsdr as "[R82XX] PLL not locked!" and is counted as a
caveat. If the acknowledgement is missing or differs from what was requested, TuningIncoherent is raised and
the caller must not capture.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "observer_config.json"
MAIN_RTL_TCP_UNIT = "rtl_tcp.service"
EVIDENCE_SERVER_ACK = "RTL_TCP_SERVER_ACK"
ACK_TIMEOUT_SECONDS = 4.0

_ACK_PATTERNS = {
    "center_frequency_hz": re.compile(r"\bset freq (\d+)\b"),
    "sample_rate_hz": re.compile(r"\bset sample rate (\d+)\b"),
    "gain_tenths_db": re.compile(r"\bset gain (\d+)\b"),
}
_PLL_NOT_LOCKED = re.compile(r"PLL not locked")
# librtlsdr failures rtl_tcp prints right AFTER its "set freq/sample rate/gain" lines when the dongle cannot be driven
# (e.g. -4 = LIBUSB_ERROR_NO_DEVICE: the USB device re-enumerated under a running rtl_tcp). rtl_tcp prints the "set"
# line before calling librtlsdr, so the acknowledgement alone does not prove the setting was applied.
_DEVICE_FAILURE = re.compile(r"failed with -\d+|failed=-\d+|Failed to submit transfer|No such device|"
                             r"usb_claim_interface error|Failed to open rtlsdr device")


class OperatingConfigError(RuntimeError):
    """observer_config.json does not define a usable operating SDR configuration."""


class TuningIncoherent(RuntimeError):
    """The SDR could not be shown to be tuned to what was requested: no capture may follow."""


def operating_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """The agreed operating configuration. Raises instead of falling back to any literal."""
    path = Path(path) if path is not None else CONFIG_PATH
    try:
        defaults = json.loads(path.read_text(encoding="utf-8"))["observation_defaults"]
    except (OSError, ValueError, KeyError) as exc:
        raise OperatingConfigError(f"{path}: no readable observation_defaults ({exc})") from exc
    out: Dict[str, Any] = {}
    for key, kind in (("center_frequency_hz", int), ("sample_rate_hz", int), ("gain_db", float)):
        value = defaults.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise OperatingConfigError(f"{path}: observation_defaults.{key} missing or invalid ({value!r})")
        if kind is int and float(value) != int(value):
            raise OperatingConfigError(f"{path}: observation_defaults.{key} must be a whole number of Hz ({value!r})")
        out[key] = kind(value)
    return out


def operating_center_frequency_hz(path: Optional[Path] = None) -> int:
    return operating_config(path)["center_frequency_hz"]


# ------------------------------------------------------------------ acknowledgement sources
class JournalAckSource:
    """rtl_tcp's stdout in the journal of a systemd unit. The unit must run rtl_tcp line-buffered
    (stdbuf -oL), otherwise the acknowledgement lines arrive minutes late and every tune times out."""

    def __init__(self, unit: str = MAIN_RTL_TCP_UNIT):
        self.unit = unit
        self.cursor: Optional[str] = None
        self.describe = f"journal of {unit}"

    def mark(self) -> None:
        out = subprocess.run(["journalctl", "-u", self.unit, "-n", "0", "--show-cursor", "--no-pager"],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"-- cursor: (\S+)", out)
        if not m:
            raise TuningIncoherent(f"cannot read the {self.unit} journal cursor - no acknowledgement source")
        self.cursor = m.group(1)

    def lines(self) -> List[str]:
        out = subprocess.run(["journalctl", "-u", self.unit, f"--after-cursor={self.cursor}", "-o", "cat",
                              "--no-pager"], capture_output=True, text=True, timeout=10).stdout
        return out.splitlines()


class LogFileAckSource:
    """rtl_tcp's stdout redirected to a file (RFI_REF's dedicated server)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.offset = 0
        self.describe = f"log file {self.path}"

    def mark(self) -> None:
        self.offset = self.path.stat().st_size if self.path.exists() else 0

    def lines(self) -> List[str]:
        if not self.path.exists():
            return []
        with open(self.path, "r", errors="replace") as handle:
            handle.seek(self.offset)
            return handle.read().splitlines()


def parse_acks(lines: List[str]) -> Dict[str, Any]:
    """The LAST acknowledged value of each setting, plus the PLL-not-locked count, from rtl_tcp output."""
    acks: Dict[str, Any] = {"pll_not_locked_count": 0, "device_failures": []}
    for line in lines:
        if _DEVICE_FAILURE.search(line):
            acks["device_failures"].append(line.strip())
        for key, pattern in _ACK_PATTERNS.items():
            m = pattern.search(line)
            if m:
                acks[key] = int(m.group(1))
        if _PLL_NOT_LOCKED.search(line):
            acks["pll_not_locked_count"] += 1
    return acks


# ------------------------------------------------------------------ explicit tuning
async def tune_explicitly(sdr: Any, center_frequency_hz: float, sample_rate_hz: float, gain_db: float, *,
                          ack_source: Any = None, ack_timeout: float = ACK_TIMEOUT_SECONDS,
                          operating: Optional[Dict[str, Any]] = None,
                          sleep: Callable[[float], Any] = asyncio.sleep) -> Dict[str, Any]:
    """Send the full configuration on this SDRCapture connection and prove the server received it.

    SDRCapture.configure() skips commands whose value it believes is already set, so its cached state is
    cleared first: all four commands go out every time, whatever the previous client left behind. Returns the
    tuning record (requested / applied / evidence); raises TuningIncoherent when rtl_tcp's acknowledgement is
    missing or differs."""
    requested = {"center_frequency_hz": int(round(center_frequency_hz)), "sample_rate_hz": int(round(sample_rate_hz)),
                 "gain_db": float(gain_db)}
    source = ack_source if ack_source is not None else JournalAckSource()
    source.mark()
    sdr.current_frequency = None
    sdr.current_sample_rate = None
    sdr.manual_gain_enabled = None
    sdr.current_gain = None
    await sdr.configure(center_freq=requested["center_frequency_hz"], sample_rate=requested["sample_rate_hz"],
                        gain=requested["gain_db"])

    return await verify_acknowledged(source, requested, operating=operating, ack_timeout=ack_timeout, sleep=sleep)


async def verify_acknowledged(source: Any, requested: Dict[str, Any], *, operating: Optional[Dict[str, Any]] = None,
                              ack_timeout: float = ACK_TIMEOUT_SECONDS,
                              sleep: Callable[[float], Any] = asyncio.sleep) -> Dict[str, Any]:
    """The evidence half of tune_explicitly(), for a caller that sent the four rtl_tcp commands itself (the bench,
    which must stay the only reader of its socket). `source.mark()` must have been called BEFORE the commands
    were sent. Returns the tuning record; raises TuningIncoherent when the acknowledgement is missing/differs."""
    requested = {"center_frequency_hz": int(round(requested["center_frequency_hz"])),
                 "sample_rate_hz": int(round(requested["sample_rate_hz"])), "gain_db": float(requested["gain_db"])}
    want = {"center_frequency_hz": requested["center_frequency_hz"], "sample_rate_hz": requested["sample_rate_hz"],
            "gain_tenths_db": int(round(requested["gain_db"] * 10))}
    deadline = time.monotonic() + ack_timeout
    acks: Dict[str, Any] = {}
    while True:
        acks = parse_acks(source.lines())
        if all(acks.get(k) == v for k, v in want.items()) or time.monotonic() > deadline:
            break
        await sleep(0.2)

    applied = {"center_frequency_hz": acks.get("center_frequency_hz"), "sample_rate_hz": acks.get("sample_rate_hz"),
               "gain_db": (acks["gain_tenths_db"] / 10.0) if acks.get("gain_tenths_db") is not None else None}
    record = {
        "requested": requested,
        "applied": applied,
        "evidence": EVIDENCE_SERVER_ACK,
        "evidence_detail": (f"rtl_tcp acknowledged the commands sent on this connection ({source.describe}); "
                            "rtl_tcp has no frequency readback and does not report librtlsdr's return value"),
        "pll_not_locked_count": acks.get("pll_not_locked_count", 0),
        "tuned_utc": datetime.now(timezone.utc).isoformat(),
    }
    if operating is not None:
        record["operating_center_frequency_hz"] = operating["center_frequency_hz"]
        record["matches_operating"] = requested["center_frequency_hz"] == operating["center_frequency_hz"]
    failures = acks.get("device_failures") or []
    if failures:
        record["evidence"] = "NONE"
        record["device_failures"] = failures[:8]
        raise TuningIncoherent(f"rtl_tcp received the settings but librtlsdr FAILED to apply them ({len(failures)} error "
                               f"line(s), first: {failures[0]!r}) via {source.describe} - the MAIN dongle cannot be "
                               f"driven by this rtl_tcp (USB device lost or re-enumerated?): restart rtl_tcp.service - "
                               f"NO capture taken")
    mismatched = [k for k, v in want.items() if acks.get(k) != v]
    if mismatched:
        record["evidence"] = "NONE"
        raise TuningIncoherent(f"rtl_tcp did not acknowledge the requested {', '.join(mismatched)} within "
                               f"{ack_timeout:g} s (requested {requested}, acknowledged {applied}) via "
                               f"{source.describe} - NO capture taken")
    return record


def main_device_reenumerated_since_rtl_tcp_start(serial: str = "00000001", unit: str = MAIN_RTL_TCP_UNIT,
                                                run: Callable[..., Any] = subprocess.run) -> Optional[str]:
    """Reason when the MAIN dongle (USB serial `serial`) was (re)attached by the kernel AFTER the running rtl_tcp
    process started - rtl_tcp then holds a handle to a device that no longer exists and every command fails (-4).
    None when not detected. Read-only: the unit's start time and the kernel journal."""
    try:
        started = run(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", unit], capture_output=True,
                      text=True, timeout=10).stdout.strip()
        if not started:
            return None
        kernel = run(["journalctl", "-k", "--since", started, "-o", "short-iso", "--no-pager"], capture_output=True,
                     text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    hits = [line for line in kernel.splitlines() if f"SerialNumber: {serial}" in line]
    if not hits:
        return None
    return (f"the MAIN SDR (USB serial {serial}) was re-attached by the kernel at {hits[-1].split()[0]}, after "
            f"{unit} started ({started}): rtl_tcp still holds the previous device handle and cannot drive the dongle - "
            f"restart {unit} (no capture may be running) before capturing")


def tuning_attrs(record: Dict[str, Any]) -> Dict[str, Any]:
    """Flat HDF5-attr form of a tuning record: requested and applied are separate keys, never merged.
    center_frequency_hz itself is the applied (acknowledged) value."""
    req, app = record["requested"], record["applied"]
    return {
        "center_frequency_hz": app["center_frequency_hz"],
        "gain": app["gain_db"],
        "requested_center_frequency_hz": req["center_frequency_hz"],
        "applied_center_frequency_hz": app["center_frequency_hz"],
        "requested_sample_rate_hz": req["sample_rate_hz"],
        "applied_sample_rate_hz": app["sample_rate_hz"],
        "requested_gain_db": req["gain_db"],
        "applied_gain_db": app["gain_db"],
        "tuning_evidence": record["evidence"],
        "tuning_evidence_detail": record["evidence_detail"],
        "tuning_pll_not_locked_count": record["pll_not_locked_count"],
        "tuned_utc": record["tuned_utc"],
    }
