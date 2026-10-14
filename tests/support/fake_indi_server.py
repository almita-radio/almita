"""A small INDI server with one simulated mount that really keeps moving and tracking - for stop tests.

It speaks enough INDI XML for INDITelescopeControl and indi_getprop: getProperties, CONNECTION, ON_COORD_SET,
EQUATORIAL_EOD_COORD (a slew that takes real time, Busy while it lasts), TELESCOPE_TRACK_STATE,
TELESCOPE_ABORT_MOTION, TELESCOPE_PARK, the motion and home vectors. The mount model is what makes the tests
meaningful - it is NOT replaced by the code under test:

    - not tracking and not slewing -> RA advances at the sidereal rate (a stopped mount, as seen from the sky);
    - tracking                      -> RA and Dec stay put;
    - slewing                       -> coordinates travel to the target; EQUATORIAL_EOD_COORD is Busy;
    - when a slew ends (completed OR aborted) the mount goes back to the tracking state it had when the slew
      started - the OnStep behaviour behind the 2026-10-13 finding: a tracking OFF sent during the slew is undone.

Faults are switched on with flags. Every command received is appended to a JSON-lines journal so a test can prove
what was (and was not) sent - e.g. that no park, home or new GOTO followed a STOP.

    python tests/support/fake_indi_server.py --port 0 --journal /tmp/x/indi.jsonl [--no-abort] [--slew-seconds 8] ...
prints "LISTENING <port>" on stdout once it accepts connections.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

SIDEREAL = 1.0027379 / 3600.0      # hours of RA per second for a mount that is not tracking


class Mount:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.device = a.device
        self.connected = True
        self.parked = False
        self.tracking = a.start_tracking
        self.tracking_before_slew = self.tracking
        self.ra, self.dec = a.start_ra, a.start_dec
        self.slew = None                    # (ra0, dec0, ra1, dec1, t0, duration)
        self.last = time.monotonic()
        self.coord_set = "TRACK"
        self.frozen = False                 # --freeze-after-stop: stop answering
        self.stop_seen_at = None

    # ---- model
    def advance(self) -> None:
        now = time.monotonic()
        dt, self.last = now - self.last, now
        if self.slew:
            ra0, dec0, ra1, dec1, t0, dur = self.slew
            f = min(1.0, (now - t0) / dur)
            self.ra = ra0 + (((ra1 - ra0 + 12) % 24) - 12) * f
            self.dec = dec0 + (dec1 - dec0) * f
            if f >= 1.0:
                self.end_slew()
        elif not self.tracking and not self.a.pinned:
            self.ra = (self.ra + SIDEREAL * dt) % 24.0

    def end_slew(self) -> None:
        self.slew = None
        if not self.a.no_track_restore:
            self.tracking = self.tracking_before_slew      # OnStep: back to the pre-slew tracking state

    def start_slew(self, ra: float, dec: float) -> None:
        self.advance()
        self.tracking_before_slew = self.tracking
        self.slew = (self.ra, self.dec, ra % 24.0, dec, time.monotonic(), self.a.slew_seconds)

    def abort(self) -> None:
        self.advance()
        if self.slew:
            self.end_slew()

    # ---- vectors
    def state_of_eod(self) -> str:
        return "Busy" if self.slew else ("Ok" if self.tracking else "Idle")

    def vector(self, name: str, tag_prefix: str = "def") -> str | None:
        self.advance()
        d, ts = self.device, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        sw = lambda b: "On" if b else "Off"                                      # noqa: E731
        if name == "CONNECTION":
            return (f'<{tag_prefix}SwitchVector device="{d}" name="CONNECTION" state="Ok" perm="rw" rule="OneOfMany" timestamp="{ts}">'
                    f'<{tag_prefix}Switch name="CONNECT">{sw(self.connected)}</{tag_prefix}Switch>'
                    f'<{tag_prefix}Switch name="DISCONNECT">{sw(not self.connected)}</{tag_prefix}Switch></{tag_prefix}SwitchVector>')
        if name == "EQUATORIAL_EOD_COORD":
            return (f'<{tag_prefix}NumberVector device="{d}" name="EQUATORIAL_EOD_COORD" state="{self.state_of_eod()}" perm="rw" timestamp="{ts}">'
                    f'<{tag_prefix}Number name="RA" format="%10.6m">{self.ra:.10f}</{tag_prefix}Number>'
                    f'<{tag_prefix}Number name="DEC" format="%10.6m">{self.dec:.10f}</{tag_prefix}Number></{tag_prefix}NumberVector>')
        if name == "TELESCOPE_TRACK_STATE":
            shown = self.tracking
            if self.a.lie_track_off_after_stop and self.stop_seen_at is not None:
                shown = False                                                    # the switch says OFF, the mount still tracks
            return (f'<{tag_prefix}SwitchVector device="{d}" name="TELESCOPE_TRACK_STATE" state="{"Busy" if self.tracking else "Idle"}" perm="rw" rule="OneOfMany" timestamp="{ts}">'
                    f'<{tag_prefix}Switch name="TRACK_ON">{sw(shown)}</{tag_prefix}Switch>'
                    f'<{tag_prefix}Switch name="TRACK_OFF">{sw(not shown)}</{tag_prefix}Switch></{tag_prefix}SwitchVector>')
        if name == "TELESCOPE_ABORT_MOTION" and not self.a.no_abort:
            return (f'<{tag_prefix}SwitchVector device="{d}" name="TELESCOPE_ABORT_MOTION" state="Idle" perm="rw" rule="AtMostOne" timestamp="{ts}">'
                    f'<{tag_prefix}Switch name="ABORT">Off</{tag_prefix}Switch></{tag_prefix}SwitchVector>')
        if name == "ON_COORD_SET":
            return (f'<{tag_prefix}SwitchVector device="{d}" name="ON_COORD_SET" state="Idle" perm="rw" rule="OneOfMany" timestamp="{ts}">'
                    + "".join(f'<{tag_prefix}Switch name="{m}">{sw(self.coord_set == m)}</{tag_prefix}Switch>' for m in ("TRACK", "SLEW", "SYNC"))
                    + f'</{tag_prefix}SwitchVector>')
        if name == "TELESCOPE_PARK":
            return (f'<{tag_prefix}SwitchVector device="{d}" name="TELESCOPE_PARK" state="Ok" perm="rw" rule="OneOfMany" timestamp="{ts}">'
                    f'<{tag_prefix}Switch name="PARK">{sw(self.parked)}</{tag_prefix}Switch>'
                    f'<{tag_prefix}Switch name="UNPARK">{sw(not self.parked)}</{tag_prefix}Switch></{tag_prefix}SwitchVector>')
        if name in ("TELESCOPE_MOTION_NS", "TELESCOPE_MOTION_WE"):
            a, b = ("MOTION_NORTH", "MOTION_SOUTH") if name.endswith("NS") else ("MOTION_WEST", "MOTION_EAST")
            return (f'<{tag_prefix}SwitchVector device="{d}" name="{name}" state="Idle" perm="rw" rule="AtMostOne" timestamp="{ts}">'
                    f'<{tag_prefix}Switch name="{a}">Off</{tag_prefix}Switch><{tag_prefix}Switch name="{b}">Off</{tag_prefix}Switch></{tag_prefix}SwitchVector>')
        if name == "OnStep Status":
            kind = "defText" if tag_prefix == "def" else "oneText"
            texts = {":GU# return": "nNpz/EW280", "Tracking": "Tracking" if self.tracking else "Idle", "Park": "UnParked",
                     "Error": self.a.onstep_error}
            return (f'<{tag_prefix}TextVector device="{d}" name="OnStep Status" state="Ok" perm="ro" timestamp="{ts}">'
                    + "".join(f'<{kind} name="{k}">{v}</{kind}>' for k, v in texts.items()) + f'</{tag_prefix}TextVector>')
        if name == "TELESCOPE_HOME":
            return (f'<{tag_prefix}SwitchVector device="{d}" name="TELESCOPE_HOME" state="Idle" perm="rw" rule="AtMostOne" timestamp="{ts}">'
                    f'<{tag_prefix}Switch name="GO">Off</{tag_prefix}Switch></{tag_prefix}SwitchVector>')
        return None

    NAMES = ("CONNECTION", "EQUATORIAL_EOD_COORD", "TELESCOPE_TRACK_STATE", "TELESCOPE_ABORT_MOTION", "ON_COORD_SET",
             "TELESCOPE_PARK", "TELESCOPE_MOTION_NS", "TELESCOPE_MOTION_WE", "TELESCOPE_HOME", "OnStep Status")


class Server:
    def __init__(self, a: argparse.Namespace):
        self.a, self.mount = a, Mount(a)
        self.journal = open(a.journal, "a", buffering=1) if a.journal else None
        self.writers: set = set()

    def note(self, kind: str, **fields) -> None:
        if self.journal:
            self.journal.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **fields}) + "\n")

    async def send(self, writer, xml: str | None) -> None:
        if xml is None or self.mount.frozen:
            return
        try:
            writer.write((xml + "\n").encode())
            await writer.drain()
        except (ConnectionError, RuntimeError):
            pass

    def stop_seen(self) -> None:
        m = self.mount
        if m.stop_seen_at is None:
            m.stop_seen_at = time.monotonic()
        if self.a.freeze_after_stop:
            m.frozen = True                    # INDI stops answering once a stop command has arrived
        if self.a.drop_after_stop:
            for w in list(self.writers):
                w.close()

    async def handle_element(self, writer, root: ET.Element) -> None:
        m, tag, name = self.mount, root.tag, root.attrib.get("name")
        if tag == "getProperties":
            self.note("get", name=name)
            for n in ([name] if name else m.NAMES):
                await self.send(writer, m.vector(n, "def"))
            return
        values = {c.attrib.get("name"): (c.text or "").strip() for c in root}
        self.note("new", name=name, values=values)
        if name == "CONNECTION":
            m.connected = values.get("CONNECT") == "On"
        elif name == "ON_COORD_SET":
            m.coord_set = next((k for k, v in values.items() if v == "On"), m.coord_set)
        elif name == "EQUATORIAL_EOD_COORD":
            if m.coord_set == "SYNC":
                m.ra, m.dec = float(values["RA"]), float(values["DEC"])
            else:
                m.start_slew(float(values["RA"]), float(values["DEC"]))
        elif name == "TELESCOPE_TRACK_STATE":
            want = values.get("TRACK_ON") == "On"
            if not want:
                self.stop_seen()
            if not ((self.a.ignore_track_off or self.a.lie_track_off_after_stop) and not want):
                m.advance()
                m.tracking = want
        elif name == "TELESCOPE_ABORT_MOTION" and not self.a.no_abort:
            self.stop_seen()
            if not self.a.abort_ignored:
                m.abort()
        elif name == "TELESCOPE_PARK":
            m.parked = values.get("PARK") == "On"
        if name:
            await self.send(writer, m.vector(name, "set"))

    async def client(self, reader, writer) -> None:
        self.writers.add(writer)
        buf = ""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                buf += data.decode("utf8", "replace")
                while True:
                    match = re.search(r"<(getProperties|new\w+Vector|enableBLOB)\b[^>]*?(/>|>.*?</\1>)", buf, re.S)
                    if not match:
                        break
                    buf = buf[match.end():]
                    try:
                        await self.handle_element(writer, ET.fromstring(match.group(0)))
                    except ET.ParseError:
                        continue
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.writers.discard(writer)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def ticker(self) -> None:
        """Pushes EOD updates while slewing and when a slew ends, like a driver polling its mount."""
        was_slewing = False
        ticks = 0
        while True:
            await asyncio.sleep(0.2)
            m = self.mount
            m.advance()
            ticks += 1
            if ticks % 4 == 0:                         # the driver's own status poll (OnStep :GU#), about once a second
                for w in list(self.writers):
                    await self.send(w, m.vector("OnStep Status", "set"))
            if m.slew or was_slewing:
                for w in list(self.writers):
                    await self.send(w, m.vector("EQUATORIAL_EOD_COORD", "set"))
                    if not m.slew:
                        await self.send(w, m.vector("TELESCOPE_TRACK_STATE", "set"))
            was_slewing = bool(m.slew)


async def serve(a: argparse.Namespace) -> None:
    srv = Server(a)
    server = await asyncio.start_server(srv.client, "127.0.0.1", a.port)
    print(f"LISTENING {server.sockets[0].getsockname()[1]}", flush=True)
    asyncio.create_task(srv.ticker())
    async with server:
        await server.serve_forever()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--device", default="Telescope Simulator")
    p.add_argument("--journal", default=None)
    p.add_argument("--slew-seconds", type=float, default=6.0)
    p.add_argument("--start-ra", type=float, default=5.0)
    p.add_argument("--start-dec", type=float, default=-30.0)
    p.add_argument("--start-tracking", action="store_true")
    p.add_argument("--no-abort", action="store_true", help="the driver exposes no TELESCOPE_ABORT_MOTION")
    p.add_argument("--abort-ignored", action="store_true", help="ABORT is accepted but the slew goes on")
    p.add_argument("--no-track-restore", action="store_true", help="a finished slew does NOT restore the pre-slew tracking")
    p.add_argument("--ignore-track-off", action="store_true", help="tracking OFF commands have no effect")
    p.add_argument("--lie-track-off-after-stop", action="store_true", help="the switch reads OFF but the mount keeps tracking")
    p.add_argument("--freeze-after-stop", action="store_true", help="stop answering once a stop command arrives")
    p.add_argument("--drop-after-stop", action="store_true", help="close every connection once a stop command arrives")
    p.add_argument("--onstep-error", default="None", help="text of the OnStep Status Error field")
    p.add_argument("--pinned", action="store_true", help="a stopped mount shows constant RA (e.g. at the pole)")
    return p


if __name__ == "__main__":
    try:
        asyncio.run(serve(parser().parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
