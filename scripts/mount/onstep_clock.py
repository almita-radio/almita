#!/usr/bin/env python3
"""Measure, and if needed correct, the OnStep mount clock against the host's NTP-synchronized clock.

Why a reconnect: LX200 OnStep's TIME_UTC INDI property is a snapshot the driver reads from the mount when it
CONNECTS (:GC#/:GL#/:GG#); it does not tick and a new client connection does not refresh it (see
observation_preflight._onstep_time_snapshot_check). Comparing it with "now" therefore measures the snapshot's
age, not the mount clock. The only clean measurement is to make the driver re-read the mount: CONNECTION off/on,
then compare the fresh TIME_UTC with the host time at the instant the driver published it.

`measure`  reconnect the driver and report the mount clock offset (no write to the mount clock).
`set`      write TIME_UTC = host UTC now (driver sends it to OnStep), then `measure` again to verify.

Neither moves the mount: CONNECTION and TIME_UTC only. Refuses unless the mount is at rest (Idle/Tracking
status, coordinates not Busy, unparked or parked is fine). Exit 0 when |offset| <= --tolerance after the action.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

DEVICE = "LX200 OnStep"


class Indi:
    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), 3.0)
        self.sock.settimeout(0.2)
        self.buf = ""

    def send(self, xml: str) -> None:
        self.sock.sendall(xml.encode())

    def wait(self, name: str, timeout: float, predicate=lambda vec: True) -> Optional[Tuple[float, Dict[str, str], str]]:
        """(host epoch when received, elements, state) of the next def/set vector `name` that satisfies predicate.
        Vectors are consumed strictly in arrival order, so a burst (e.g. a reconnect) can never hide one."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data = self.sock.recv(65536)
                if not data:
                    return None
                self.buf += data.decode("utf8", "replace")
            except socket.timeout:
                pass
            received = time.time()
            while True:
                m = re.search(r"<(def|set)(Text|Switch|Number|Light|BLOB)Vector\b([^>]*)>(.*?)</\1\2Vector>", self.buf, re.S)
                if not m:
                    break
                self.buf = self.buf[m.end():]
                head, body = m.group(3), m.group(4)
                if f'device="{DEVICE}"' not in head or f'name="{name}"' not in head:
                    continue
                els = {e.group(1): e.group(2).strip()
                       for e in re.finditer(r'<(?:def|one)\w+\s+name="([^"]+)"[^>]*>(.*?)</', body, re.S)}
                state = (re.search(r'state="(\w+)"', head) or [None, None])[1]
                vec = (received, els, state)
                if predicate(vec):
                    return vec
        return None

    def get(self, name: str, timeout: float = 3.0):
        self.send(f'<getProperties version="1.7" device="{DEVICE}" name="{name}"/>')
        return self.wait(name, timeout)

    def close(self) -> None:
        self.sock.close()


def _at_rest(indi: Indi) -> str:
    eod, status = indi.get("EQUATORIAL_EOD_COORD"), indi.get("OnStep Status")
    if not eod or not status:
        return "mount status not readable"
    if eod[2] == "Busy":
        return "mount coordinates Busy (moving)"
    if status[1].get("Tracking") not in ("Idle", "Tracking"):
        return f"OnStep status {status[1].get('Tracking')!r} is not at rest"
    return ""


def measure(indi: Indi) -> Dict[str, object]:
    """Reconnect the driver and compare its freshly read TIME_UTC with the host time it was published at."""
    # indiserver forwards to a client only what it asked for: subscribe to the whole device first, then drain.
    indi.send(f'<getProperties version="1.7" device="{DEVICE}"/>')
    previous = indi.wait("TIME_UTC", 5.0)
    stale = previous[1].get("UTC") if previous else None
    indi.wait("__drain__", 1.0)
    indi.send(f'<newSwitchVector device="{DEVICE}" name="CONNECTION"><oneSwitch name="CONNECT">Off</oneSwitch>'
              f'<oneSwitch name="DISCONNECT">On</oneSwitch></newSwitchVector>')
    indi.wait("CONNECTION", 10, lambda v: v[1].get("DISCONNECT") == "On")
    time.sleep(1.0)
    indi.send(f'<newSwitchVector device="{DEVICE}" name="CONNECTION"><oneSwitch name="CONNECT">On</oneSwitch>'
              f'<oneSwitch name="DISCONNECT">Off</oneSwitch></newSwitchVector>')
    # On reconnect the driver first re-publishes its cached snapshot; the fresh mount read is the first value
    # that differs from the pre-reconnect one.
    vec = indi.wait("TIME_UTC", 30, lambda v: v[1].get("UTC") not in (None, stale))
    if not vec:
        raise SystemExit("driver did not publish a fresh TIME_UTC within 30 s of reconnecting")
    host_epoch, els, _ = vec
    mount_utc = datetime.fromisoformat(els["UTC"]).replace(tzinfo=timezone.utc)
    host_utc = datetime.fromtimestamp(host_epoch, timezone.utc)
    offset = (mount_utc - host_utc).total_seconds()
    indi.wait("CONNECTION", 5, lambda v: v[1].get("CONNECT") == "On")
    return {"mount_utc": mount_utc.isoformat(), "mount_utc_offset_hours": els.get("OFFSET"),
            "host_utc_at_publish": host_utc.isoformat(timespec="milliseconds"),
            "offset_seconds": round(offset, 1),
            "resolution": "OnStep reports whole seconds; host time is when the driver published the value "
                          "(after it read the mount), so the true offset is within about -2..+1 s of this"}


def set_time(indi: Indi, utc_offset_hours: float) -> Dict[str, object]:
    now = datetime.now(timezone.utc) + timedelta(milliseconds=500)   # rounds to the second OnStep will hold
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    indi.send(f'<newTextVector device="{DEVICE}" name="TIME_UTC"><oneText name="UTC">{stamp}</oneText>'
              f'<oneText name="OFFSET">{utc_offset_hours:.2f}</oneText></newTextVector>')
    vec = indi.wait("TIME_UTC", 15, lambda v: v[2] in ("Ok", "Alert"))
    return {"written_utc": stamp, "offset_hours": utc_offset_hours, "driver_state": vec[2] if vec else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("measure", "set"))
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7624)
    parser.add_argument("--utc-offset-hours", type=float, default=-3.0,
                        help="local-time offset OnStep stores alongside UTC (America/Santiago: -3 in October)")
    parser.add_argument("--tolerance", type=float, default=2.0, help="seconds")
    parser.add_argument("--record", help="append the JSON result to this file")
    args = parser.parse_args()

    indi = Indi(args.host, args.port)
    try:
        blocked = _at_rest(indi)
        if blocked:
            raise SystemExit(f"refusing: {blocked}")
        result: Dict[str, object] = {"action": args.action, "utc": datetime.now(timezone.utc).isoformat()}
        result["before"] = measure(indi)
        if args.action == "set":
            result["write"] = set_time(indi, args.utc_offset_hours)
            time.sleep(2.0)
            result["after"] = measure(indi)
        final = result.get("after") or result["before"]
        result["within_tolerance"] = abs(final["offset_seconds"]) <= args.tolerance
    finally:
        indi.close()
    print(json.dumps(result, indent=2))
    if args.record:
        with open(args.record, "a") as handle:
            handle.write(json.dumps(result) + "\n")
    return 0 if result["within_tolerance"] else 1


if __name__ == "__main__":
    sys.exit(main())
