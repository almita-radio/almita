"""Read-only real-hardware inspection (Fase precheck: "distinguir
CONFIGURED/EXPECTED de VERIFIED BY DEVICE READBACK"). Every function here
either reads already-public OS state (a process's own command line) or
reads rtl_tcp's own connection handshake (server-initiated data) - NONE
of them ever sends a byte that could change tuner state. This is the
"read-only hardware inspection" this project's own policy explicitly
allows even when everything else about real hardware is off-limits.

Three honest verification tiers, never conflated:
- CONFIGURED_EXPECTED: only from this codebase's own written defaults/docs
- VERIFIED_BY_SERVICE_COMMAND_LINE: read from the actual running rtl_tcp
  process's argv via `ps` - real, but reports what rtl_tcp was TOLD to do
  at startup, not confirmation the tuner is presently obeying it.
- VERIFIED_BY_DEVICE_READBACK: from rtl_tcp's own protocol handshake -
  the only genuine device-side confirmation this protocol offers at all
  (tuner type + gain count; NOT frequency/sample-rate/gain, which rtl_tcp
  never reports back over the wire).
"""
from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional

VERIFICATION_CONFIGURED_EXPECTED = "CONFIGURED_EXPECTED"
VERIFICATION_SERVICE_COMMAND_LINE = "VERIFIED_BY_SERVICE_COMMAND_LINE"
VERIFICATION_DEVICE_READBACK = "VERIFIED_BY_DEVICE_READBACK"

# rtl_tcp's own dongle-info magic -> tuner name mapping (from librtlsdr's
# rtl_tcp.c enum rtlsdr_tuner - not this project's invention).
_TUNER_TYPE_BY_MAGIC_SUFFIX = {
    1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D",
}


@dataclass
class RtlTcpHandshakeInfo:
    reachable: bool
    magic: Optional[str]
    tuner_type: Optional[str]
    gain_count: Optional[int]
    verification: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"reachable": self.reachable, "magic": self.magic, "tuner_type": self.tuner_type,
                "gain_count": self.gain_count, "verification": self.verification, "detail": self.detail}


def probe_rtl_tcp_handshake(host: str, port: int, timeout: float = 3.0) -> RtlTcpHandshakeInfo:
    """Connects, reads the 12-byte dongle-info handshake rtl_tcp sends
    unsolicited on every new connection, then closes - NEVER sends a
    single byte. Safe to call at any time, including immediately before a
    real capture, without affecting the tuner or any other connected
    client."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            payload = bytearray()
            while len(payload) < 12:
                chunk = sock.recv(12 - len(payload))
                if not chunk:
                    return RtlTcpHandshakeInfo(False, None, None, None, VERIFICATION_DEVICE_READBACK,
                                                "connection closed during handshake")
                payload.extend(chunk)
    except (OSError, socket.timeout) as exc:
        return RtlTcpHandshakeInfo(False, None, None, None, VERIFICATION_DEVICE_READBACK,
                                    f"{type(exc).__name__}: {exc}")
    magic = payload[0:4].decode("ascii", errors="replace")
    tuner_code = int.from_bytes(payload[4:8], byteorder="big")
    gain_count = int.from_bytes(payload[8:12], byteorder="big")
    tuner_type = _TUNER_TYPE_BY_MAGIC_SUFFIX.get(tuner_code, f"UNKNOWN_CODE_{tuner_code}")
    return RtlTcpHandshakeInfo(True, magic, tuner_type, gain_count, VERIFICATION_DEVICE_READBACK,
                                "handshake read successfully - no bytes written")


@dataclass
class ServiceCommandLineInfo:
    found: bool
    pid: Optional[int]
    command_line: Optional[str]
    parsed: Dict[str, Any]
    verification: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"found": self.found, "pid": self.pid, "command_line": self.command_line,
                "parsed": self.parsed, "verification": self.verification, "detail": self.detail}


def inspect_rtl_tcp_service_command_line(port: int) -> ServiceCommandLineInfo:
    """Reads `ps -eo pid,args` (read-only, no hardware access at all - a
    plain OS process listing) looking for the rtl_tcp process bound to
    `port`, and parses its OWN argv for -d/-f/-s/-g/-T. This reports what
    the service was TOLD at startup, not a live device confirmation -
    labeled VERIFIED_BY_SERVICE_COMMAND_LINE, distinct from and weaker
    than a true protocol readback."""
    try:
        result = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return ServiceCommandLineInfo(False, None, None, {}, VERIFICATION_SERVICE_COMMAND_LINE,
                                       f"{type(exc).__name__}: {exc}")
    port_flag = f"-p {port}" if f"-p {port}" in result.stdout else f"-p{port}"
    for line in result.stdout.splitlines():
        if "rtl_tcp" in line and (f"-p {port}" in line or f"-p{port}" in line):
            parts = line.strip().split(None, 1)
            pid = int(parts[0]) if parts and parts[0].isdigit() else None
            command_line = parts[1] if len(parts) > 1 else line.strip()
            tokens = command_line.split()
            parsed: Dict[str, Any] = {}
            for i, token in enumerate(tokens):
                if token == "-d" and i + 1 < len(tokens):
                    parsed["serial"] = tokens[i + 1]
                elif token == "-f" and i + 1 < len(tokens):
                    parsed["center_frequency_hz"] = float(tokens[i + 1])
                elif token == "-s" and i + 1 < len(tokens):
                    parsed["sample_rate_hz"] = float(tokens[i + 1])
                elif token == "-g" and i + 1 < len(tokens):
                    parsed["gain_db"] = float(tokens[i + 1])
                elif token == "-a" and i + 1 < len(tokens):
                    parsed["bind_address"] = tokens[i + 1]
            parsed["bias_t_enabled"] = "-T" in tokens
            return ServiceCommandLineInfo(True, pid, command_line, parsed, VERIFICATION_SERVICE_COMMAND_LINE,
                                           "parsed from the live process's own argv via ps - not a device readback")
    return ServiceCommandLineInfo(False, None, None, {}, VERIFICATION_SERVICE_COMMAND_LINE,
                                   f"no rtl_tcp process found bound to port {port}")
