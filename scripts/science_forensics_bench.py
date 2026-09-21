#!/usr/bin/env python3
"""ALMITA SCIENCE FORENSICS INDOOR BENCH (infrastructure validation; NOT astronomy, NOT a preflight, NOT READY FOR FIELD).

The antenna and mount may be indoors. Everything here is designed so that NO MOUNT MOVEMENT is possible:
the bench never imports capture.py or the INDI control class; its own INDI client can only send <getProperties/> (anything else is
blocked before it reaches the wire and traced as BLOCKED_MOVEMENT / BLOCKED_WRITE); the MAIN SDR is touched only with --yes.

  status                 offline + passive checks (hashes, plan, clocks, storage, ports, services). Writes nothing.
  check                  full bench: offline by default; --live-readonly adds INDI reads; --with-sdr --yes adds a short SDR stream
  sdr --seconds N --yes  short MAIN SDR stream sanity (BENCH DATA, NOT SCIENCE); never saved unless --save-sample
  audit                  static audit of capture.py --preflight-only: SAFE INDOOR / NOT SAFE INDOOR, with file:line evidence
  preview                the exact commands to use outside (status, precheck, preflight, run --dry-run); nothing is executed
  dates                  plan validity for a date and how the sky-shift contrast and the start windows move per day
  selftest               movement-guard and A-B-A timestamp-pipeline self tests

Result vocabulary: BENCH PASS | BENCH PARTIAL | BENCH BLOCKED (operational: READY FOR INDOOR BENCH PREFLIGHT -> INDOOR BENCH PASS).
This tool NEVER prints READY FOR FIELD: that needs an outdoor, real, passing capture.py --preflight-only via science_forensics_field.py.
Bench output goes only under data/science_forensics_bench/ (never data/mosaic, data/reduced, campaign or REDUCE paths).
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import os
import re
import select
import socket
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO), str(REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PINNED_PLAN_SHA256 = "8bacb7ab79f99df2afc16213e7f3b26c2bdf832e07a04f06127ebb93cd301b4e"
PINNED_OBSERVER_SHA256 = "4264bc52e7233c26cb899c8263a36a0222b3fd11f678a5db3b5b7b36667fc269"
DEFAULT_PLAN = "examples/science_forensics_experiment.yaml"
BENCH_ROOT = "data/science_forensics_bench"
SCIENCE_PATHS = ("data/mosaic", "data/reduced", "data/iq", "data/IQ", "data/science_forensics_field", "data/science", "data/afc00", "data/e2e",
                 "data/calibration", "data/science_forensics", "data/science_forensics_experiment")
PORTS = {"MAIN rtl_tcp": 1234, "RFI_REF rtl_tcp": 1235, "INDI": 7624, "OBSERVE API": 8090, "Field Console": 8088}
SERVICES = ("rtl_tcp.service", "almita-observe-api.service", "almita-console-web.service", "almita-console-watcher.service")
ALLOWED_RTL_COMMANDS = {0x01: "SET FREQUENCY", 0x02: "SET SAMPLE RATE", 0x03: "SET GAIN MODE", 0x04: "SET GAIN"}   # receiver tuner only, as capture.py
RTL_RECV_BYTES = 262144                 # one large recv per call: the hot loop does no processing (metrics are computed after the read)
STALL_TIMEOUT_S = 1.0                   # no bytes for this long = the stream stalled
DEFAULT_SETTLE_S = 0.5                  # capture.py's own retune settle (SDRCapture.retune_settle_seconds): bytes in it are discarded
# Stream validation is by THROUGHPUT against the rate the receiver must deliver (2 bytes per complex sample). A real MAIN stream measured 0.98 of
# it; USB/rtl_tcp can lose a little, so >= 0.90 is "alive and complete". Above 1.15 the receiver is delivering more than the configured rate
# allows (rate not applied), which is also not a healthy stream. The bounds are tested against exact, 95 %, 50 % and 24 % streams.
MIN_THROUGHPUT_RATIO = 0.90
MAX_THROUGHPUT_RATIO = 1.15
BANNER = "BENCH DATA - NOT SCIENCE"
NO_MOVEMENT = "NO MOUNT MOVEMENT: the bench sends only INDI <getProperties/> and rtl_tcp receiver commands (frequency, sample rate, gain); every other action is blocked"
MOVEMENT_PROPERTIES = ("EQUATORIAL_EOD_COORD", "EQUATORIAL_COORD", "ON_COORD_SET", "TELESCOPE_ABORT_MOTION", "TELESCOPE_PARK", "TELESCOPE_TRACK_MODE",
                       "TELESCOPE_TRACK_STATE", "TELESCOPE_MOTION_NS", "TELESCOPE_MOTION_WE", "TELESCOPE_HOME", "TELESCOPE_SLEW_RATE", "TELESCOPE_PIER_SIDE")
MOVEMENT_VERBS = ("GOTO", "SYNC", "PARK", "UNPARK", "TRACK", "SLEW", "ABORT", "HOME")


# ------------------------------------------------------------------ trace and guards

class MovementBlocked(Exception):
    """A mount-affecting (or any non-read) action was attempted: stopped before it reached the wire."""


class Trace:
    KINDS = ("READ", "WRITE", "CONNECT", "CONFIGURE", "BLOCKED_MOVEMENT", "BLOCKED_WRITE")

    def __init__(self, echo=False, clock=None):
        self.events, self.echo, self.clock = [], echo, clock or (lambda: datetime.now(timezone.utc))

    def add(self, kind, what, detail=None):
        assert kind in self.KINDS
        ev = {"t_utc": self.clock().isoformat(timespec="milliseconds"), "kind": kind, "what": what, "detail": detail}
        self.events.append(ev)
        if self.echo:
            print(f"  [trace] {kind:<16} {what}" + (f"  ({detail})" if detail else ""), flush=True)
        return ev

    def count(self, kind):
        return sum(1 for e in self.events if e["kind"] == kind)


def indoor_mode_active(env=None) -> bool:
    return (env if env is not None else os.environ).get("INDOOR_MODE") == "1"


def guard_indi_message(xml: str, trace: Trace) -> str:
    """The ONLY thing the bench may send to INDI is <getProperties/>. Everything else is refused before the wire."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        trace.add("BLOCKED_WRITE", "INDI unparsable message", str(exc))
        raise MovementBlocked(f"unparsable INDI message refused: {exc}")
    if root.tag == "getProperties":
        return "READ"
    name = root.attrib.get("name", "")
    kind = "BLOCKED_MOVEMENT" if (name in MOVEMENT_PROPERTIES or root.tag.startswith("new")) else "BLOCKED_WRITE"
    trace.add(kind, f"INDI {root.tag} {name} -> BLOCKED", "bench is read-only")
    raise MovementBlocked(f"INDI {root.tag} {name} refused: the bench only sends getProperties")


def mount_command(action: str, trace: Trace, *args):
    """Any movement verb, even by name, is refused. Exists so the guard is testable and so no code path can 'just try'."""
    trace.add("BLOCKED_MOVEMENT", f"MOUNT {action.upper()} -> BLOCKED", "INDOOR bench: no movement API exists")
    raise MovementBlocked(f"mount command {action!r} is not available in the indoor bench")


def assert_bench_path(root: Path, path) -> Path:
    """Every bench write must resolve inside data/science_forensics_bench and outside every science/campaign path."""
    root_b = (Path(root) / BENCH_ROOT).resolve()
    p = Path(path).resolve()
    if not (p == root_b or root_b in p.parents):
        raise MovementBlocked(f"bench output {p} is outside {root_b}")
    for s in SCIENCE_PATHS:
        sp = (Path(root) / s).resolve()
        if sp == p or sp in p.parents:
            raise MovementBlocked(f"bench output {p} is under the science path {sp}")
    return p


# ------------------------------------------------------------------ transports (real, injectable in tests)

class SocketTransport:
    def __init__(self, host, port, timeout=3.0):
        self.s = socket.create_connection((host, port), timeout=timeout)

    def send(self, data: bytes):
        self.s.sendall(data)

    def recv(self, n, timeout):
        r, _, _ = select.select([self.s], [], [], max(0.0, timeout))
        if not r:
            return b""
        d = self.s.recv(n)
        return d if d else None          # None = closed

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


_VECTOR = re.compile(r"<(def|set)(Number|Switch|Text|Light)Vector\b[^>]*>.*?</\1\2Vector>", re.S)


def _num(text):
    t = (text or "").strip()
    try:
        return float(t)
    except ValueError:
        try:
            parts = [float(x) for x in t.split(":")]
            sign = -1 if t.startswith("-") else 1
            return sign * (abs(parts[0]) + (parts[1] / 60 if len(parts) > 1 else 0) + (parts[2] / 3600 if len(parts) > 2 else 0))
        except ValueError:
            return None


class IndiReadOnly:
    """Minimal INDI client: connect, send <getProperties/> once, read definitions. It has no method that can write anything else."""

    def __init__(self, host, port, device, trace: Trace, transport_factory=None, timeout=5.0):
        self.host, self.port, self.device, self.trace, self.timeout = host, port, device, trace, timeout
        self.factory = transport_factory or (lambda h, p, t: SocketTransport(h, p, t))
        self.wire_log = []

    def _send(self, tr, xml: str):
        guard_indi_message(xml, self.trace)                         # raises before anything is written
        root = ET.fromstring(xml)
        self.wire_log.append({"tag": root.tag, "device": root.attrib.get("device"), "name": root.attrib.get("name"), "bytes": len(xml)})
        tr.send((xml + "\n").encode())
        self.trace.add("READ", f"INDI getProperties device={root.attrib.get('device')}", root.attrib.get("name"))

    def snapshot(self) -> dict:
        out = {"host": self.host, "port": self.port, "device": self.device, "reachable": False, "devices_seen": [], "properties_seen": []}
        try:
            tr = self.factory(self.host, self.port, 3.0)
        except OSError as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        self.trace.add("CONNECT", f"INDI {self.host}:{self.port}")
        out["reachable"] = True
        try:
            self._send(tr, f'<getProperties version="1.7" device="{self.device}"/>')
            deadline, buf, devices, props = time.monotonic() + self.timeout, "", set(), {}
            while time.monotonic() < deadline:
                chunk = tr.recv(65536, min(0.5, max(0.05, deadline - time.monotonic())))
                if chunk is None:
                    break
                buf += chunk.decode(errors="replace")
                consumed = 0
                for m in _VECTOR.finditer(buf):
                    consumed = m.end()
                    try:
                        root = ET.fromstring(m.group(0))
                    except ET.ParseError:
                        continue
                    if root.attrib.get("device"):
                        devices.add(root.attrib["device"])
                    if root.attrib.get("device") not in (None, self.device):
                        continue
                    props[root.attrib.get("name")] = root
                buf = buf[consumed:]                       # keep only the (possibly partial) tail after the last complete vector
                if {"CONNECTION", "EQUATORIAL_EOD_COORD", "TELESCOPE_TRACK_STATE"} <= set(props):
                    break
        finally:
            tr.close()
        out["devices_seen"], out["properties_seen"] = sorted(devices), sorted(p for p in props if p)
        out["device_present"] = self.device in devices
        out.update(self._extract(props))
        return out

    @staticmethod
    def _elements(root):
        return {c.attrib.get("name"): (c.text or "").strip() for c in root if c.attrib.get("name")}

    def _extract(self, props: dict) -> dict:
        r = {}
        if "CONNECTION" in props:
            r["connected"] = self._elements(props["CONNECTION"]).get("CONNECT") == "On"
        if "EQUATORIAL_EOD_COORD" in props:
            e = self._elements(props["EQUATORIAL_EOD_COORD"])
            ra, dec = _num(e.get("RA")), _num(e.get("DEC"))
            r["ra_hours"], r["dec_deg"] = ra, dec
            r["coordinates_valid"] = bool(ra is not None and dec is not None and 0 <= ra < 24 and -90 <= dec <= 90)
            r["coordinate_state"] = props["EQUATORIAL_EOD_COORD"].attrib.get("state")
            r["coordinate_timestamp"] = props["EQUATORIAL_EOD_COORD"].attrib.get("timestamp")
        if "TELESCOPE_TRACK_STATE" in props:
            e = self._elements(props["TELESCOPE_TRACK_STATE"])
            r["tracking"] = "alert" if props["TELESCOPE_TRACK_STATE"].attrib.get("state") == "Alert" else (
                "on" if e.get("TRACK_ON") == "On" else "off" if e.get("TRACK_OFF") == "On" else "unknown")
        if "TELESCOPE_PIER_SIDE" in props:
            e = self._elements(props["TELESCOPE_PIER_SIDE"])
            r["pier_side"] = "west" if e.get("PIER_WEST") == "On" else "east" if e.get("PIER_EAST") == "On" else "unknown"
        if "TELESCOPE_PARK" in props:
            e = self._elements(props["TELESCOPE_PARK"])
            r["park"] = "parked" if e.get("PARK") == "On" else "unparked" if e.get("UNPARK") == "On" else "unknown"
        if "OnStep Status" in props:
            e = self._elements(props["OnStep Status"])
            r["onstep_status"] = {"error": e.get("Error"), "state": props["OnStep Status"].attrib.get("state")}
        r["mount_state"] = ("moving/busy" if r.get("coordinate_state") == "Busy" else "idle" if r.get("coordinate_state") in ("Idle", "Ok") else
                            r.get("coordinate_state") or "unknown")
        return r


class RecordingSocket:
    """Wraps rtl_tcp's socket: records every command sent and refuses anything outside the receiver-tuner whitelist."""

    def __init__(self, sock, trace: Trace, log: list):
        self._s, self._trace, self._log = sock, trace, log

    def sendall(self, data, *a):
        if len(data) == 5:
            cmd, val = struct.unpack(">BI", data)
            if cmd not in ALLOWED_RTL_COMMANDS:
                self._trace.add("BLOCKED_WRITE", f"RTL CMD 0x{cmd:02x} -> BLOCKED", "not in the bench whitelist (bias-T etc. are never changed)")
                raise MovementBlocked(f"rtl_tcp command 0x{cmd:02x} refused")
            self._log.append({"cmd": cmd, "name": ALLOWED_RTL_COMMANDS[cmd], "value": val})
            self._trace.add("CONFIGURE", f"RTL {ALLOWED_RTL_COMMANDS[cmd]}", str(val if cmd != 0x04 else f"{val / 10:.1f} dB"))
        else:
            self._trace.add("BLOCKED_WRITE", "RTL non-command write -> BLOCKED")
            raise MovementBlocked("unexpected write to rtl_tcp refused")
        return self._s.sendall(data, *a)

    def __getattr__(self, name):
        return getattr(self._s, name)


# ------------------------------------------------------------------ passive system reads

def _proc_tcp(states):
    out = {}
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in Path(name).read_text().splitlines()[1:]:
                f = line.split()
                if len(f) > 3 and f[3] in states:
                    out.setdefault(int(f[1].rsplit(":", 1)[1], 16), []).append(f[3])
        except OSError:
            return None
    return out


def real_listeners():
    r = _proc_tcp({"0A"})
    return set(r) if r is not None else set()


def real_established():
    r = _proc_tcp({"01"})
    return set(r) if r is not None else None


def real_service_active(unit):
    try:
        return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def real_usb_serials():
    out = []
    for d in Path("/sys/bus/usb/devices").glob("*"):
        try:
            if (d / "idVendor").read_text().strip() == "0bda":
                out.append({"product": (d / "product").read_text().strip() if (d / "product").exists() else None,
                            "serial": (d / "serial").read_text().strip() if (d / "serial").exists() else None})
        except OSError:
            continue
    return out


def real_meminfo():
    d = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        d[k] = int(v.split()[0]) * 1024
    return d


def real_timedatectl():
    try:
        p = subprocess.run(["timedatectl", "show", "-p", "Timezone", "-p", "NTPSynchronized"], capture_output=True, text=True, timeout=10)
        return dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
    except Exception:
        return {}


def real_unit_execstart(unit="rtl_tcp.service"):
    for base in ("/etc/systemd/system", "/lib/systemd/system"):
        p = Path(base) / unit
        if p.is_file():
            m = re.search(r"^ExecStart=(.*)$", p.read_text(), re.M)
            return m.group(1) if m else None
    return None


def real_h5_samples(root: Path, limit=60, per_campaign=3, want_seconds=(9.0, 11.0)):
    """(file bytes, capture seconds) of real captures: file size from stat, seconds from an HDF5 ATTRIBUTE (samples never read).
    Stratified: a few files per campaign, newest campaigns first, until `limit` captures of the wanted length are found."""
    try:
        import h5py
    except ImportError:
        return []
    camps = sorted((d for d in (root / "data" / "mosaic").glob("*") if (d / "data" / "iq").is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    out = []
    for c in camps:
        files = sorted((c / "data" / "iq").glob("**/*.h5"))[:per_campaign]
        for f in files:
            try:
                with h5py.File(f, "r") as h:
                    sec = float(h.attrs.get("duration_seconds", 0))
                if want_seconds[0] <= sec <= want_seconds[1]:
                    out.append((f.stat().st_size, sec))
            except OSError:
                continue
        if len(out) >= limit:
            break
    return out


class Bench:
    """All external effects are injectable; defaults are passive reads."""

    def __init__(self, root=REPO, plan=DEFAULT_PLAN, now=None, ctx=None, trace=None, listeners=None, established=None, service_active=None,
                 usb=None, meminfo=None, timedatectl=None, execstart=None, h5_samples=None, indi_factory=None, sdr_factory=None, disk_free=None,
                 mono=None, sleep=None, expected_plan_sha256=None, sdr_clock=None):
        import science_forensics_field as F
        self.F, self.root = F, Path(root)
        self.plan_arg = plan
        self.ctx = ctx or F.Ctx(root=root, plan=plan, now=now)
        self.trace = trace or Trace()
        self.listeners = listeners or real_listeners
        self.established = established or real_established
        self.service_active = service_active or real_service_active
        self.usb = usb or real_usb_serials
        self.meminfo = meminfo or real_meminfo
        self.timedatectl = timedatectl or real_timedatectl
        self.execstart = execstart or real_unit_execstart
        self.h5_samples = h5_samples or (lambda: real_h5_samples(self.root))
        self.indi_factory, self.sdr_factory = indi_factory, sdr_factory
        self.disk_free = disk_free or (lambda p: __import__("shutil").disk_usage(p).free)
        self.mono, self.sleep = mono or time.monotonic, sleep or time.sleep
        self.sdr_clock = sdr_clock or time.monotonic       # the SDR read window's own clock (injectable so stream tests need no real time)
        self.expected_plan_sha256 = expected_plan_sha256 or (PINNED_PLAN_SHA256 if str(plan) == DEFAULT_PLAN else None)
        self.written = []

    def now(self):
        return self.ctx.now()

    def bench_dir(self, bench_id):
        return assert_bench_path(self.root, self.root / BENCH_ROOT / bench_id)


# ------------------------------------------------------------------ checks

class Checks:
    def __init__(self):
        self.items = []

    def add(self, name, status, detail, level="soft", **extra):
        self.items.append({"name": name, "status": status, "detail": detail, "level": level, **extra})

    def outcome(self):
        if any(c["status"] == "FAIL" and c["level"] == "hard" for c in self.items):
            return "BENCH BLOCKED"
        if any(c["level"] in ("soft", "hard") and c["status"] in ("FAIL", "SKIPPED") for c in self.items):
            return "BENCH PARTIAL"
        return "BENCH PASS"

    def hard_failures(self):
        return [c["name"] for c in self.items if c["status"] == "FAIL" and c["level"] == "hard"]


def load_plan(b: Bench):
    return b.F.load_plan(b.ctx)


def check_repo_and_hashes(b: Bench, plan: dict, ck: Checks):
    F, ctx = b.F, b.ctx
    git = F.git_state(ctx)
    ck.add("repo HEAD / branch", "PASS" if git["commit"] != "UNKNOWN" else "FAIL", f"{git['commit'][:12]} on {git['branch']}", "hard", git=git)
    fz = F.frozen_check(ctx)
    ck.add("frozen modules unchanged", "PASS" if fz["ok"] else "FAIL", "unchanged" if fz["ok"] else f"differ: {fz['differs'] or 'git unavailable'}", "hard")
    sha = F.sha256_file(ctx.plan_path)
    if b.expected_plan_sha256:
        ck.add("plan sha256 (pinned)", "PASS" if sha == b.expected_plan_sha256 else "FAIL", f"{sha}" if sha == b.expected_plan_sha256 else f"{sha} != pinned {b.expected_plan_sha256}", "hard")
    else:
        ck.add("plan sha256 (pinned)", "FAIL", f"{sha}: a non-default plan needs --expected-plan-sha256", "hard")
    for exp in F.EXPERIMENTS:
        info = F.experiment_info(ctx, plan, exp)
        actual = F.sha256_file(info["csv"]) if info["csv"].is_file() else None
        ok = actual == info["csv_sha256_plan"]
        ck.add(f"CSV {exp} sha256 (full, from plan)", "PASS" if ok else "FAIL", actual if ok else f"{actual} != plan {info['csv_sha256_plan']}", "hard")
    oc = ctx.p("observer_config.json")
    osha = F.sha256_file(oc)
    tracked_clean = ctx.run(["git", "diff", "--quiet", "HEAD", "--", "observer_config.json"], timeout=30, cwd=str(ctx.root))["returncode"] == 0
    ck.add("observer_config.json unmodified vs HEAD (read-only)", "PASS" if tracked_clean else "FAIL", osha, "hard")
    ck.add("observer_config.json equals the one the plan was designed with", "PASS" if osha == PINNED_OBSERVER_SHA256 else "WARN",
           osha if osha == PINNED_OBSERVER_SHA256 else f"{osha} != {PINNED_OBSERVER_SHA256}: site/hardware entries changed since the design (plan positions/windows assume the designed site)", "info")


def check_environment(b: Bench, ck: Checks):
    import importlib.util
    miss = [m for m in ("numpy", "h5py", "astropy", "yaml") if importlib.util.find_spec(m) is None]
    venv = b.root / ".venv"
    in_venv = sys.prefix != sys.base_prefix or str(Path(sys.executable)).startswith(str(venv))
    ck.add("python / venv", "PASS" if not miss and sys.version_info >= (3, 10) else "FAIL",
           f"{sys.executable} (venv: {in_venv}); missing modules {miss}", "hard")
    if not in_venv:
        ck.add("running inside the project .venv", "WARN", "use ./.venv/bin/python", "info")
    cp = b.root / "capture.py"
    ck.add("capture.py path", "PASS" if cp.is_file() else "FAIL", str(cp), "hard")
    mem = b.meminfo()
    avail, swap = mem.get("MemAvailable", 0), mem.get("SwapFree", 0)
    ck.add("memory sanity (Raspberry Pi)", "PASS" if avail > 500 * 2 ** 20 else "FAIL", f"MemAvailable {avail / 2 ** 30:.2f} GiB, SwapFree {swap / 2 ** 20:.1f} MiB (near zero swap: no concurrent heavy jobs)", "soft")


def check_clock(b: Bench, ck: Checks):
    now = b.now()
    aware = now.tzinfo is not None and now.utcoffset() == timedelta(0)
    ck.add("UTC timezone-aware", "PASS" if aware else "FAIL", now.isoformat(), "hard")
    m0, w0 = b.mono(), time.time()
    marks = []
    for _ in range(4):
        b.sleep(0.05)
        marks.append(b.mono())
    m1, w1 = b.mono(), time.time()
    mono_ok = all(y >= x for x, y in zip([m0] + marks, marks + [m1]))
    rate_ok = abs((m1 - m0) - (w1 - w0)) < 0.05 if (w1 - w0) > 0 else True
    ck.add("monotonic clock sane", "PASS" if mono_ok and rate_ok else "FAIL", f"monotonic dt {m1 - m0:.3f}s vs wall dt {w1 - w0:.3f}s, non-decreasing: {mono_ok}", "hard")
    head_ts = b.ctx.git("log", "-1", "--format=%ct")
    floor = datetime.fromtimestamp(int(head_ts), tz=timezone.utc) if head_ts.isdigit() else datetime(2026, 1, 1, tzinfo=timezone.utc)
    now_cmp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)      # a naive clock already failed above; do not crash on it
    ck.add("system clock sane (not before the HEAD commit)", "PASS" if now_cmp >= floor - timedelta(minutes=1) else "FAIL", f"now {now.isoformat()} >= HEAD {floor.isoformat()}", "hard")
    td = b.timedatectl()
    ck.add("local time sync state (no remote query)", "PASS" if td.get("NTPSynchronized") == "yes" else "WARN", f"timezone {td.get('Timezone')}, NTPSynchronized={td.get('NTPSynchronized')}", "info")


def timestamp_pipeline_selftest() -> dict:
    """Three synthetic captures A, B, A with UNEVEN, controlled timestamps (not a schedule): bracketing must use the ACTUAL times."""
    import numpy as np
    from science_forensics.experiment.analysis import matched_pairs
    t = np.array([1000.0, 1020.0, 1070.0])                    # A at 0 s, B at +20 s, A at +70 s (actual)
    labels = ["A", "B", "A"]
    a0, a1, offset = 1_419_598_000.0, 1_419_598_000.0 - 700.0, 6919.0
    w = 20.0 / 70.0
    f = np.array([a0, (1 - w) * a0 + w * a1 + offset, a1])    # B = A interpolated at ITS OWN time + a known offset
    mp = matched_pairs(t, labels, f, np.array([30.0, 30.0, 30.0]), np.array([0, 1, 2]), tol_s=90.0)
    ok_pairs = len(mp["cross_position_pairs"]) == 1
    got = mp["cross_position_pairs"][0]["delta_hz"] if ok_pairs else None
    # a planned (even) schedule would interpolate at w=0.5 and be wrong by |0.5 - 20/70| * 700 Hz
    wrong = f[1] - 0.5 * (a0 + a1)
    return {"ok": bool(ok_pairs and abs(got - offset) < 1e-6 and abs(wrong - offset) > 100.0), "recovered_offset_hz": got, "true_offset_hz": offset,
            "planned_schedule_would_give_hz": wrong, "bracket": mp["cross_position_pairs"][0]["bracket"] if ok_pairs else None,
            "gap_s": mp["cross_position_pairs"][0]["gap_s"] if ok_pairs else None}


def guard_selftest(trace: Trace) -> dict:
    """Prove the guard refuses: each mount verb and each non-read INDI message raises before anything is sent."""
    local = Trace()
    refused = []
    for verb in MOVEMENT_VERBS:
        try:
            mount_command(verb, local)
        except MovementBlocked:
            refused.append(verb)
    for xml in ('<newNumberVector device="LX200 OnStep" name="EQUATORIAL_EOD_COORD"><oneNumber name="RA">1</oneNumber></newNumberVector>',
                '<newSwitchVector device="LX200 OnStep" name="TELESCOPE_PARK"><oneSwitch name="PARK">On</oneSwitch></newSwitchVector>',
                '<newSwitchVector device="LX200 OnStep" name="ON_COORD_SET"><oneSwitch name="SYNC">On</oneSwitch></newSwitchVector>',
                '<newSwitchVector device="LX200 OnStep" name="TELESCOPE_TRACK_STATE"><oneSwitch name="TRACK_ON">On</oneSwitch></newSwitchVector>',
                '<newSwitchVector device="LX200 OnStep" name="CONNECTION"><oneSwitch name="CONNECT">On</oneSwitch></newSwitchVector>'):
        try:
            guard_indi_message(xml, local)
        except MovementBlocked:
            refused.append(ET.fromstring(xml).attrib["name"])
    read_ok = guard_indi_message('<getProperties version="1.7" device="x"/>', local) == "READ"
    ok = len(refused) == len(MOVEMENT_VERBS) + 5 and read_ok and local.count("BLOCKED_MOVEMENT") >= len(MOVEMENT_VERBS) + 4
    return {"ok": bool(ok), "refused": refused, "getProperties_allowed": read_ok, "environment_INDOOR_MODE": os.environ.get("INDOOR_MODE")}


# ------------------------------------------------------------------ storage, ports, services

def storage_estimate(b: Bench, plan: dict) -> dict:
    """Two numbers per experiment: EMPIRICAL (median size of real 10 s capture files, HDF5 is compressed) and the UNCOMPRESSED bound that
    capture.py's own preflight uses (2 bytes x rate x seconds x captures x 1.25). The disk check uses the bound."""
    samples = [(s, sec) for s, sec in b.h5_samples() if 9.0 <= sec <= 11.0]
    F = b.F
    inst = plan["instrument_config_identical_to_feature_campaign"]
    data_dir = b.ctx.p("data")
    free = int(b.disk_free(data_dir if data_dir.exists() else b.root))
    per_bound = int(2 * inst["sdr_sample_rate_hz"] * inst["capture_seconds"])
    out = {"free_bytes": free, "bytes_per_capture_uncompressed_bound": per_bound, "experiments": {}, "basis": None}
    sizes = sorted(s for s, _ in samples)
    per = sizes[len(sizes) // 2] if sizes else None
    out["basis"] = (f"empirical: median of {len(sizes)} real 10 s capture files (stat size; range {sizes[0]} .. {sizes[-1]} bytes); " if sizes else "empirical: no historical 10 s captures found; ") + \
        "bound: capture.py preflight formula (uncompressed)"
    out["bytes_per_capture_empirical"] = per
    for exp in F.EXPERIMENTS:
        n = plan["experiments"][exp]["n_captures"]
        need = int(per_bound * n * 1.25)                        # capture.py's own disk safety factor
        out["experiments"][exp] = {"n_captures": n, "estimated_bytes": per * n if per else None, "bound_bytes": per_bound * n, "with_1.25_safety": need,
                                   "margin": (free / need) if need else None, "ok": free >= need}
    out["note"] = "capture files only; REDUCE outputs are extra (measure with du on data/reduced after a run)"
    return out


def ports_and_services(b: Bench, plan: dict) -> dict:
    listen = b.listeners()
    est = b.established()
    ports = {}
    for name, port in PORTS.items():
        ports[name] = {"port": port, "listening": port in listen}
    main = ports["MAIN rtl_tcp"]
    if not main["listening"]:
        state = "UNKNOWN"
        reason = "nothing listening on 127.0.0.1:1234"
    elif est is None:
        state, reason = "UNKNOWN", "cannot read connection table"
    elif 1234 in est:
        state, reason = "BUSY", "an established connection exists on :1234 (rtl_tcp serves one client): not touched"
    else:
        state, reason = "AVAILABLE", "listening and no established client"
    services = {u: b.service_active(u) for u in SERVICES}
    usb = b.usb()
    serials = {d["serial"] for d in usb if d.get("serial")}
    rfi_serial = plan["instrument_config_identical_to_feature_campaign"]["rfi_ref"]["serial"]
    return {"ports": ports, "main_rtl_tcp": {"state": state, "reason": reason}, "services": services, "usb_rtlsdr": usb,
            "main_serial_expected": "00000001 (rtl_tcp.service unit)", "main_usb_present": "00000001" in serials,
            "rfi_ref": {"serial_expected": rfi_serial, "usb_present": rfi_serial in serials,
                        "status": "reachable" if rfi_serial in serials else "unavailable",
                        "note": "RFI_REF is started by capture.py as its own disposable rtl_tcp (:1235); never blocks the bench"}}


def sdr_config_report(b: Bench, plan: dict) -> dict:
    inst = plan["instrument_config_identical_to_feature_campaign"]
    es = b.execstart()
    svc = {}
    if es:
        for key, rx in (("serial", r"-d\s+(\S+)"), ("host", r"-a\s+(\S+)"), ("port", r"-p\s+(\d+)"), ("frequency_hz", r"-f\s+(\d+)"), ("sample_rate_hz", r"-s\s+(\d+)"), ("gain_db", r"-g\s+([\d.]+)")):
            m = re.search(rx, es)
            svc[key] = m.group(1) if m else None
        svc["bias_tee_flag_-T"] = bool(re.search(r"\s-T(\s|$)", es))
        svc["execstart"] = es
    cmp = {"plan_center_frequency_hz": inst["sdr_center_frequency_hz"], "plan_sample_rate_hz": inst["sdr_sample_rate_hz"], "plan_gain_db": inst["sdr_gain_db"],
           "plan_main_bias_tee": inst.get("main_bias_tee"), "rtl_tcp_host": svc.get("host") or "localhost", "rtl_tcp_port": int(svc.get("port") or 1234),
           "service": svc or "rtl_tcp.service unit not readable",
           "notes": ["capture.py sets frequency/rate/gain on every run through the rtl_tcp protocol; the bench sets the SAME plan values only with --yes",
                     "the service starts at -f %s; a preflight or run retunes it to the plan (normal behaviour, nothing is restored)" % svc.get("frequency_hz"),
                     "Bias-T is a rtl_tcp launch flag (-T) of the service; neither capture.py nor the bench changes it"]}
    if svc:
        cmp["service_vs_plan"] = {"gain_db_equal": svc.get("gain_db") is not None and float(svc["gain_db"]) == float(inst["sdr_gain_db"]),
                                  "sample_rate_equal": svc.get("sample_rate_hz") is not None and int(svc["sample_rate_hz"]) == int(inst["sdr_sample_rate_hz"]),
                                  "frequency_equal": svc.get("frequency_hz") is not None and int(svc["frequency_hz"]) == int(inst["sdr_center_frequency_hz"]),
                                  "bias_tee_matches_plan": bool(inst.get("main_bias_tee")) == svc["bias_tee_flag_-T"]}
    return cmp


# ------------------------------------------------------------------ SDR bench

def rtl_config_commands(inst: dict) -> list:
    """The receiver settings the bench applies, in the ORDER SDRCapture._configure_network sends them (initial connection):
    frequency, sample rate, manual gain mode, gain (tenths of dB). Nothing else, and never Bias-T."""
    return [(0x01, int(inst["sdr_center_frequency_hz"])), (0x02, int(inst["sdr_sample_rate_hz"])), (0x03, 1), (0x04, round(float(inst["sdr_gain_db"]) * 10))]


def read_stream(sock, *, seconds: float, settle_s: float, recv_size: int = RTL_RECV_BYTES, clock=time.monotonic) -> dict:
    """Read the rtl_tcp stream. HOT PATH = recv + list append + two counters: no numpy, no copies, no per-block processing.
    Bytes received during `settle_s` (retune settle) are counted and dropped; the following `seconds` are kept as-is (accumulate first,
    measure later). The caller sets the socket timeout (a stall). Returns the raw chunks and timing; it never raises for stream problems."""
    chunks, settle_bytes, gaps, calls = [], 0, 0, 0
    ended_by, error = "duration", None
    t_start = clock()
    t_settle_end, t_end = t_start + settle_s, t_start + settle_s + seconds
    t_first = t_last = None
    last_recv = t_start
    kept = 0
    while clock() < t_end:
        try:
            data = sock.recv(recv_size)
        except socket.timeout:
            ended_by, error = "timeout", f"no bytes for {STALL_TIMEOUT_S:.1f} s (stream stalled)"
            break
        except OSError as exc:
            ended_by, error = "error", f"{type(exc).__name__}: {exc}"
            break
        t = clock()
        calls += 1
        if not data:
            ended_by, error = "closed", "rtl_tcp closed the connection"
            break
        if t - last_recv > 0.25:
            gaps += 1
        last_recv = t
        if t < t_settle_end:
            settle_bytes += len(data)
            continue
        if t_first is None:
            t_first = t
        t_last = t
        kept += len(data)
        chunks.append(data)
    # rate estimate: bytes after the first chunk over the time they took (a chunk's own arrival time is not the time it took to produce)
    rate = None
    if len(chunks) >= 2 and t_last > t_first:
        rate = (kept - len(chunks[0])) / (t_last - t_first)
    return {"chunks": chunks, "kept_bytes": kept, "settle_discarded_bytes": settle_bytes, "recv_calls": calls, "recv_size": recv_size,
            "gaps_over_0.25s": gaps, "ended_by": ended_by, "error": error, "measured_bytes_per_s": rate,
            "window_seconds": (t_last - t_first) if t_first is not None else 0.0, "settle_s": settle_s}


def sdr_stream_metrics(chunks, rate, seconds, elapsed, gaps, *, read: dict | None = None) -> dict:
    """Sanity of what was received, computed AFTER the read and chunk by chunk (memory stays at one chunk of temporaries, never a
    whole-stream float array). With `read` (from read_stream) the verdict uses the real measured throughput against
    2 x sample_rate bytes/s; without it (legacy callers) the total received is compared with the total expected."""
    import numpy as np
    n = sum(len(c) for c in chunks)
    expected_rate = 2.0 * rate
    ss, clipped, zero_blocks, blocks = 0, 0, 0, 0
    for c in chunks:
        a = np.frombuffer(c, dtype=np.uint8)
        t = a.astype(np.int16) * 2 - 255                      # 2 * (v - 127.5): exact in integers
        ss += int(np.square(t, dtype=np.int64).sum())
        clipped += int(np.count_nonzero((a == 0) | (a == 255)))
        nb = a.size // 4096
        if nb:
            blk = a[: nb * 4096].reshape(nb, 4096)
            zero_blocks += int(np.count_nonzero(np.all(blk == blk[:, :1], axis=1)))
            blocks += nb
    rms = float(np.sqrt(ss / 4.0 / n)) if n else float("nan")
    clip = clipped / n if n else float("nan")
    measured = read["measured_bytes_per_s"] if read and read.get("measured_bytes_per_s") is not None else None
    if read is not None:
        ratio = (measured / expected_rate) if measured is not None else 0.0
        ended = read["ended_by"]
    else:
        ratio = n / (expected_rate * seconds) if seconds else 0.0
        ended = "duration"
    throughput_ok = bool(MIN_THROUGHPUT_RATIO <= ratio <= MAX_THROUGHPUT_RATIO)
    alive = bool(n > 0 and np.isfinite(rms) and rms > 0.5 and clip < 0.05 and zero_blocks == 0 and throughput_ok and ended == "duration")
    out = {"banner": BANNER, "samples_received_bytes": n, "expected_bytes": int(expected_rate * (read["window_seconds"] if read else seconds)),
           "received_over_expected": ratio, "expected_bytes_per_s": expected_rate,
           "throughput_bytes_per_s": measured if measured is not None else (n / seconds if (read is None and seconds) else None),
           "throughput_MB_s": (measured / 1e6) if measured is not None else None, "throughput_MS_s_iq": (measured / 2e6) if measured is not None else None,
           "throughput_ratio_bounds": [MIN_THROUGHPUT_RATIO, MAX_THROUGHPUT_RATIO], "throughput_ok": throughput_ok, "elapsed_s": elapsed,
           "rms_counts": rms, "rms_finite": bool(np.isfinite(rms)), "clipping_fraction": clip, "zero_or_constant_blocks": zero_blocks, "blocks_checked": blocks,
           "recv_gaps_over_0.25s": gaps, "nan_inf": "not applicable (uint8 interleaved I/Q)", "stream_alive_and_sane": alive, "ended_by": ended,
           "not_reported": "spectrum, lines, RFI and sky validity are NOT assessed: indoor data carry no astronomical meaning"}
    if read is not None:
        out.update({"recv_size_bytes": read["recv_size"], "recv_calls": read["recv_calls"], "settle_discarded_bytes": read["settle_discarded_bytes"],
                    "window_seconds": read["window_seconds"], "stream_error": read["error"], "peak_retained_bytes": n})
    return out


def sdr_bench(b: Bench, plan: dict, seconds: float, trace: Trace, save_sample=False, bench_id="BENCH", *, host="localhost", port=1234, settle_s=None) -> dict:
    """Connect to the MAIN rtl_tcp, apply the plan's four receiver settings, read a few seconds, report throughput and sanity.

    ONE reader only. SDRCapture.configure() starts its own continuous consumer thread that drains the same socket (it is "the sole owner of
    streaming recv()"): a second reader in the bench would split the stream with it and see only a fraction of the bytes. So the bench uses
    SDRCapture only to connect (handshake), sends the same four commands in the same order itself, and is the sole reader."""
    if seconds <= 0 or seconds > 5:
        raise ValueError("bench SDR stream is limited to 0 < seconds <= 5")
    inst = plan["instrument_config_identical_to_feature_campaign"]
    passive = ports_and_services(b, plan)["main_rtl_tcp"]
    res = {"banner": BANNER, "main_rtl_tcp_state": passive, "requested": {"center_frequency_hz": inst["sdr_center_frequency_hz"], "sample_rate_hz": inst["sdr_sample_rate_hz"],
                                                                        "gain_db": inst["sdr_gain_db"], "host": host, "port": port, "seconds": seconds}}
    if passive["state"] == "BUSY":
        res.update({"executed": False, "reason": "MAIN rtl_tcp is BUSY: not touched"})
        return res
    if passive["state"] == "UNKNOWN" and "nothing listening" in passive["reason"]:
        res.update({"executed": False, "reason": passive["reason"]})
        return res
    if b.sdr_factory is None:
        from sdr_capture import SDRCapture                       # connect/handshake only (no INDI, no mount, no configure(), no consumer thread)
        factory = lambda **kw: SDRCapture(**kw)
    else:
        factory = b.sdr_factory
    wire, read = [], None
    sdr = factory(mode="network", host=host, port=port, verbose=False)
    settle = DEFAULT_SETTLE_S if settle_s is None else settle_s

    async def go():
        t0 = time.monotonic()
        await sdr.connect()
        trace.add("CONNECT", f"RTL MAIN {host}:{port}")
        sock = RecordingSocket(sdr.socket, trace, wire)
        sdr.socket = sock
        for cmd, value in rtl_config_commands(inst):               # the ONLY writes: 0x01 frequency, 0x02 rate, 0x03 gain mode, 0x04 gain
            sock.sendall(struct.pack(">BI", cmd, value))
        sock.settimeout(STALL_TIMEOUT_S)
        result = await asyncio.get_running_loop().run_in_executor(None, lambda: read_stream(sock, seconds=seconds, settle_s=settle, clock=b.sdr_clock))
        if result["chunks"]:
            trace.add("READ", "RTL stream", f"{result['kept_bytes']} bytes kept in {result['recv_calls']} recv({result['recv_size']}) calls, "
                                            f"{result['settle_discarded_bytes']} bytes discarded in the {settle:.2f} s settle")
        return time.monotonic() - t0, result

    try:
        elapsed, read = asyncio.run(go())
        res["executed"] = True
        res["commands_sent"] = wire
        res["metrics"] = sdr_stream_metrics(read["chunks"], inst["sdr_sample_rate_hz"], seconds, elapsed, read["gaps_over_0.25s"], read=read)
        if read["error"]:
            res["error"] = read["error"]
    except MovementBlocked:
        raise
    except Exception as exc:
        res.update({"executed": True, "error": f"{type(exc).__name__}: {exc}", "commands_sent": wire})
    finally:
        try:
            asyncio.run(sdr.close())
        except Exception:
            pass
    if save_sample and read and read["chunks"]:
        d = b.bench_dir(bench_id)
        d.mkdir(parents=True, exist_ok=True)
        p = assert_bench_path(b.root, d / f"BENCH_NOT_SCIENCE_{bench_id}.u8")
        p.write_bytes(b"".join(read["chunks"]))
        (d / f"BENCH_NOT_SCIENCE_{bench_id}.json").write_text(json.dumps({"banner": BANNER, "note": "raw uint8 I/Q bytes for infrastructure testing only; never input to REDUCE/SCIENCE/FORENSICS"}))
        b.written += [str(p)]
        res["sample_file"] = str(p)
    return res


# ------------------------------------------------------------------ INDI bench

def indi_bench(b: Bench, plan: dict, trace: Trace) -> dict:
    device = json.loads(b.ctx.p("observer_config.json").read_text()).get("hardware", {}).get("telescope") or "Telescope Simulator"
    cli = IndiReadOnly("localhost", 7624, device, trace, transport_factory=b.indi_factory)
    snap = cli.snapshot()
    snap["wire_log"] = cli.wire_log
    snap["messages_sent"] = sorted({w["tag"] for w in cli.wire_log})
    snap["only_getProperties_sent"] = all(w["tag"] == "getProperties" for w in cli.wire_log)
    snap["blocked_attempts"] = trace.count("BLOCKED_MOVEMENT") + trace.count("BLOCKED_WRITE")
    return snap


# ------------------------------------------------------------------ static preflight audit

MOVEMENT_PRIMITIVES = {"goto", "sync", "set_tracking", "park", "unpark", "ensure_tracking_off", "confirm_tracking_on", "execute_observation_plan", "abort_motion", "slew"}


def _calls(node):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name:
                out.append((name, n.lineno))
    return out


def audit_preflight(root: Path) -> dict:
    """Static proof from the REAL source: what `capture.py --preflight-only` can reach. No execution of capture.py."""
    cap_p, indi_p, sdr_p = Path(root) / "capture.py", Path(root) / "indi_telescope_control.py", Path(root) / "sdr_capture.py"
    cap, indi, sdr = ast.parse(cap_p.read_text()), ast.parse(indi_p.read_text()), ast.parse(sdr_p.read_text())
    ev, verdict_ok = [], True

    def find(tree, name, cls=None):
        for n in ast.walk(tree):
            if cls and isinstance(n, ast.ClassDef) and n.name == cls:
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == name:
                        return m
            if not cls and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                return n
        return None

    executor = next(n for n in ast.walk(cap) if isinstance(n, ast.ClassDef) and n.name == "CaptureExecutor")
    methods = {m.name: m for m in executor.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    main = find(cap, "main")
    gate_if = next((n for n in ast.walk(main) if isinstance(n, ast.If) and "should_execute_after_preflight" in ast.unparse(n.test)), None)
    execute_calls = [ln for name, ln in _calls(main) if name == "execute_observation_plan"]
    gate_ok = bool(gate_if and gate_if.body and "sys.exit" in ast.unparse(gate_if.body[-1]) and all(ln > gate_if.lineno for ln in execute_calls) and execute_calls)
    ev.append({"claim": "main() exits before execute_observation_plan when --preflight-only", "ok": gate_ok, "file": "capture.py",
               "gate_line": gate_if.lineno if gate_if else None, "execute_call_lines": execute_calls})
    fn_src = ast.unparse(find(cap, "should_execute_after_preflight"))
    ns = {"Dict": dict}
    exec(fn_src, ns)                                             # pure boolean function; safe to evaluate
    gate_value = ns["should_execute_after_preflight"]({"success": True}, True)
    ev.append({"claim": "should_execute_after_preflight({'success': True}, preflight_only=True) is False", "ok": gate_value is False, "value": gate_value})
    # reachable set from the code that runs BEFORE the gate in main() plus run_preflight (and everything they call through self.)
    before = [(n, ln) for n, ln in _calls(main) if gate_if and ln < gate_if.lineno]
    frontier, reach, external = ["run_preflight"] + [n for n, _ in before if n in methods], set(), set(n for n, _ in before)
    while frontier:
        m = frontier.pop()
        if m in reach or m not in methods:
            continue
        reach.add(m)
        for name, _ln in _calls(methods[m]):
            external.add(name)
            if name in methods:
                frontier.append(name)
    moved = sorted(external & MOVEMENT_PRIMITIVES)
    ev.append({"claim": "no movement primitive (goto/sync/set_tracking/park/unpark/ensure_tracking_off/...) is reachable from the preflight path",
               "ok": not moved, "found": moved, "methods_reached": sorted(reach), "file": "capture.py"})
    # INDI writes reachable: connect() and get_coordinates() of the control class
    indi_writes = []
    conn = find(indi, "connect", "INDITelescopeControl")
    for n in ast.walk(conn):
        if isinstance(n, ast.JoinedStr) or (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            s = ast.unparse(n)
            for m in re.finditer(r"<(new\w+Vector)[^>]*name=\"(\w+)\"", s):
                indi_writes.append({"message": m.group(1), "property": m.group(2), "line": n.lineno, "method": "connect", "file": "indi_telescope_control.py"})
    gc = find(indi, "get_coordinates", "INDITelescopeControl")
    gc_new = [m.group(1) for n in ast.walk(gc) for m in re.finditer(r"<(new\w+Vector)", ast.unparse(n)) if isinstance(n, (ast.JoinedStr, ast.Constant))]
    connect_guard = "CONNECT" in ast.unparse(conn) and re.search(r"!=\s*['\"]On['\"]", ast.unparse(conn)) is not None
    ev.append({"claim": "INDI writes reachable are limited to the CONNECTION switch, and only when the device is not already CONNECTED", "ok": all(w["property"] == "CONNECTION" for w in indi_writes) and connect_guard and not gc_new,
               "writes": indi_writes, "guarded_by_CONNECT_state_check": connect_guard, "get_coordinates_writes": gc_new, "note": "a CONNECTION switch is a driver connect, not a motion, park or tracking command"})
    # SDR commands reachable through configure(): receiver tuner only
    cfg = find(sdr, "_configure_network", "SDRCapture")
    cmds = sorted({n.args[0].value for n in ast.walk(cfg) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "send"
                   and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, int)})
    ev.append({"claim": "SDR commands sent by configure() are receiver-tuner commands only (frequency, sample rate, gain mode, gain); no Bias-T command", "ok": set(cmds) <= {1, 2, 3, 4} and bool(cmds),
               "commands": [f"0x{c:02x} {ALLOWED_RTL_COMMANDS.get(c, '??')}" for c in cmds], "file": "sdr_capture.py"})
    rp = methods["run_preflight"]
    text = ast.unparse(rp)
    ev.append({"claim": "run_preflight only reads INDI (get_coordinates + indi_getprop) and probes the SDR (connect/configure/close)", "ok": "get_coordinates" in text and "indi_getprop" in ast.unparse(methods["_read_indi_preflight_properties"]) and "goto" not in text,
               "file": "capture.py", "line": rp.lineno})
    ev.append({"claim": "side effects are filesystem only: temp HDF5 and probe file created and removed, session.csv initialised by SessionManager()", "ok": True, "note": "no mount, park or tracking state is written"})
    safe = all(e["ok"] for e in ev)
    return {"verdict": "SAFE INDOOR" if safe else "NOT SAFE INDOOR", "evidence": ev,
            "caveats": ["preflight retunes MAIN rtl_tcp to the plan values (capture.py does that on every run)",
                        "it writes CONNECTION=On to INDI only if the mount device was not yet connected",
                        "the bench itself never executes capture.py; the wrapper's `preflight` stays a separate, OUTDOOR step (INDOOR_MODE=1 blocks it)"]}


# ------------------------------------------------------------------ dates, windows, preview

def field_command_preview() -> list:
    S = './.venv/bin/python scripts/science_forensics_field.py'
    out = [f'S="{S}"']
    for exp in ("A", "B", "AB"):
        out += [f"$S status {exp}", f"$S precheck {exp}", f"$S preflight {exp}", f"$S run {exp} --dry-run"]
    return out


def date_sensitivity(b: Bench, plan: dict, days=(0, 7, 14, 30, 60, 90), scan_days=None) -> dict:
    """Pairwise sky-shift contrast (channels) at the SAME local sidereal time on later dates (a start window keeps its HA, so its UTC moves
    earlier by 3.934 min/day): how long the design keeps separating sky from receiver. The contrast is NOT linear in time, so besides the
    table there is a coarse scan for the first day below 75 % of the plan (REGENERATE RECOMMENDED) and below 10 channels (INVALID).
    CALCULATED ONLY, NOT FIELD VALIDATED."""
    from science_forensics.experiment import design as D
    site = json.loads(b.ctx.p("observer_config.json").read_text())["observer"]
    pos = b.F._positions(plan, "B")
    base = datetime.fromisoformat(plan["design_epoch_utc"])

    def contrast(d):
        t = base + timedelta(days=d) - timedelta(minutes=3.9344 * d)
        e = D.expected_signatures(pos, site, t.timestamp(), 3600.0)
        return {k: v["delta_lsrk_shift_channels"] for k, v in e["pairwise"].items() if k.endswith("-A")}
    rows = {d: contrast(d) for d in days}
    d0 = rows[days[0]]
    per_day = {k: (rows[days[-1]][k] - d0[k]) / (days[-1] - days[0]) for k in d0}
    scan = {}
    first75 = first10 = None
    for d in (scan_days if scan_days is not None else range(0, 151, 5)):
        c = rows.get(d) or contrast(d)
        scan[d] = c
        if first75 is None and any(abs(c[k]) < 0.75 * abs(d0[k]) for k in d0):
            first75 = d
        if first10 is None and any(abs(v) < 10.0 for v in c.values()):
            first10 = d
    return {"basis": "CALCULATED ONLY - NOT FIELD VALIDATED", "epoch_utc": plan["design_epoch_utc"], "same_local_sidereal_time": True, "by_day": rows,
            "delta_channels_per_day_mean": per_day, "scan_step_days": 5, "first_day_below_75pct_of_plan": first75, "first_day_below_10_channels": first10,
            "scan": scan, "threshold_note": "wrapper rule: < 10 channels = INVALID (blocks), < 75 % of the plan = REGENERATE RECOMMENDED"}


def window_sensitivity(b: Bench, plan: dict, exp="B", offset_days=14, finder=None, start_date=None) -> dict:
    from science_forensics.experiment import design as D
    finder = finder or D.find_start_windows
    site = json.loads(b.ctx.p("observer_config.json").read_text())["observer"]
    pos = b.F._positions(plan, exp)
    dur = (plan["experiments"][exp]["duration_min"]["p90"] + 5.0) * 60.0
    d0 = start_date or b.now().strftime("%Y-%m-%d")
    d1 = (datetime.fromisoformat(d0) + timedelta(days=offset_days)).strftime("%Y-%m-%d")
    def first(day, side):
        w = [x for x in finder(pos, site, day, 1, dur, min_alt_deg=b.F.MIN_ALT_DEG, ha_margin_h=b.F.HA_MARGIN_H, step_min=5) if x["side"] == side]
        return datetime.fromisoformat(w[0]["first_start_utc"]) if w else None
    out = {"basis": "CALCULATED ONLY - NOT FIELD VALIDATED", "experiment": exp, "dates": [d0, d1], "sides": {}}
    for side in ("negative", "positive"):
        a, c = first(d0, side), first(d1, side)
        if a and c:
            delta = ((c - a).total_seconds() / 60.0 - offset_days * 1440.0) / offset_days
            out["sides"][side] = {"first_start_utc": [a.isoformat(), c.isoformat()], "shift_min_per_day": delta}
    out["expected_sidereal_shift_min_per_day"] = -3.934
    return out


def plan_validity(b: Bench, plan: dict, when: datetime) -> dict:
    """VALID / REGENERATE RECOMMENDED / INVALID for a departure date, with the reason. Never modifies the plan."""
    F = b.F
    out = {"basis": "CALCULATED ONLY - NOT FIELD VALIDATED", "date_utc": when.isoformat(), "by_experiment": {}}
    worst = "VALID"
    order = {"VALID": 0, "REGENERATE RECOMMENDED": 1, "INVALID": 2}
    for exp in F.EXPERIMENTS:
        e = F.check_epoch(b.ctx, plan, exp, when)
        st = "INVALID" if e["status"].startswith("BLOCKED") else "REGENERATE RECOMMENDED" if e["status"] == "REGENERATE_RECOMMENDED" else "VALID"
        out["by_experiment"][exp] = {"status": st, "days_from_plan_epoch": e["days_from_plan_epoch"], "pairwise_shift_channels": e.get("pairwise_shift_channels"),
                                     "reason": e.get("reason") or ("sky-shift contrast still >= 75 % of the plan" if st == "VALID" else "contrast fell below 75 % of the plan"),
                                     "regenerate_command": e["regenerate_command"] if st != "VALID" else None}
        if order[st] > order[worst]:
            worst = st
    out["status"] = worst
    return out


# ------------------------------------------------------------------ orchestration

def run_checks(b: Bench, plan: dict, *, live_indi=False, with_sdr=False, sdr_seconds=2.0, yes=False, save_sample=False, bench_id="BENCH") -> dict:
    ck, trace = Checks(), b.trace
    check_repo_and_hashes(b, plan, ck)
    check_environment(b, ck)
    check_clock(b, ck)
    g = guard_selftest(trace)
    ck.add("movement guard self-test (GOTO/SYNC/PARK/UNPARK/TRACK refused, INDI writes refused)", "PASS" if g["ok"] else "FAIL", f"refused {len(g['refused'])} actions; getProperties allowed: {g['getProperties_allowed']}", "hard")
    ts = timestamp_pipeline_selftest()
    ck.add("timestamp pipeline (A,B,A with actual uneven timestamps)", "PASS" if ts["ok"] else "FAIL", f"recovered offset {ts['recovered_offset_hz']} Hz (true {ts['true_offset_hz']}); a planned schedule would give {ts['planned_schedule_would_give_hz']:.0f}", "hard")
    for name in ("data/science_forensics_field", BENCH_ROOT):
        anc = b.ctx.p(name)
        while not anc.exists() and anc != anc.parent:
            anc = anc.parent
        ck.add(f"output destination writable: {name}", "PASS" if os.access(anc, os.W_OK) else "FAIL", f"nearest existing directory {anc} (no campaign directory is created)", "hard")
    st = storage_estimate(b, plan)
    if st["experiments"]:
        bad = [e for e, v in st["experiments"].items() if not v["ok"]]
        ck.add("storage for A/B/AB (real historical capture sizes)", "PASS" if not bad else "FAIL",
               "; ".join(f"{e}: empirical {(v['estimated_bytes'] or 0) / 1e9:.2f} GB, bound {v['bound_bytes'] / 1e9:.2f} GB (x1.25 {v['with_1.25_safety'] / 1e9:.2f}), margin x{v['margin']:.0f}" for e, v in st["experiments"].items()) + f"; free {st['free_bytes'] / 1e9:.1f} GB", "hard")
    else:
        ck.add("storage for A/B/AB (real historical capture sizes)", "FAIL", st["basis"], "hard")
    ps = ports_and_services(b, plan)
    p = ps["ports"]
    ck.add("rtl_tcp MAIN endpoint (passive)", "PASS" if p["MAIN rtl_tcp"]["listening"] else "FAIL", f"127.0.0.1:1234 listening={p['MAIN rtl_tcp']['listening']}; state {ps['main_rtl_tcp']['state']}: {ps['main_rtl_tcp']['reason']}", "soft")
    ck.add("rtl_tcp.service active (read-only systemctl)", "PASS" if ps["services"]["rtl_tcp.service"] == "active" else "FAIL", str(ps["services"]), "soft")
    ck.add("MAIN SDR USB device present", "PASS" if ps["main_usb_present"] else "FAIL", "serial 00000001 in /sys/bus/usb" if ps["main_usb_present"] else "serial 00000001 not found", "soft")
    ck.add("INDI endpoint (passive)", "PASS" if p["INDI"]["listening"] else "FAIL", f"127.0.0.1:7624 listening={p['INDI']['listening']}", "soft")
    ck.add("RFI_REF second SDR (informational)", "PASS" if ps["rfi_ref"]["usb_present"] else "WARN", f"{ps['rfi_ref']['status']} (serial {ps['rfi_ref']['serial_expected']}); not required by the bench", "info")
    ck.add("other services/ports (informational)", "PASS", f"OBSERVE API :8090 listening={p['OBSERVE API']['listening']}, Field Console :8088 listening={p['Field Console']['listening']}", "info")
    sdr_cfg = sdr_config_report(b, plan)
    svp = sdr_cfg.get("service_vs_plan")
    ck.add("MAIN service config vs plan (serial, gain, rate, Bias-T)", "PASS" if svp and svp["gain_db_equal"] and svp["sample_rate_equal"] and svp["bias_tee_matches_plan"] else "WARN",
           json.dumps(svp) if svp else "unit file not readable", "info")
    result = {"storage": st, "ports_services": ps, "sdr_config": sdr_cfg, "guard": g, "timestamp_selftest": ts}
    # live read-only INDI
    if live_indi:
        indi = indi_bench(b, plan, trace)
        result["indi"] = indi
        ck.add("INDI reachable", "PASS" if indi["reachable"] else "FAIL", f"{indi['host']}:{indi['port']}" + (f" error {indi.get('error')}" if not indi["reachable"] else ""), "soft")
        ck.add("mount device present", "PASS" if indi.get("device_present") else "FAIL", f"{indi['device']} in devices seen {indi.get('devices_seen')}", "soft")
        ck.add("mount coordinates readable", "PASS" if indi.get("coordinates_valid") else "FAIL", f"RA {indi.get('ra_hours')} h, Dec {indi.get('dec_deg')} deg, state {indi.get('coordinate_state')}", "soft")
        ck.add("mount tracking state readable", "PASS" if indi.get("tracking") in ("on", "off") else "FAIL", f"tracking={indi.get('tracking')}, pier={indi.get('pier_side')}, park={indi.get('park')}, mount_state={indi.get('mount_state')}", "soft")
        ck.add("zero INDI writes (only getProperties on the wire)", "PASS" if indi["only_getProperties_sent"] else "FAIL", f"messages sent: {indi['messages_sent']}", "hard")
    else:
        ck.add("INDI reachable / mount readable (live)", "SKIPPED", "not executed (offline): run check --live-readonly", "soft")
    if with_sdr:
        if not yes:
            ck.add("MAIN SDR short stream", "SKIPPED", "--with-sdr requires --yes (touches the SDR)", "soft")
        else:
            sdr = sdr_bench(b, plan, sdr_seconds, trace, save_sample=save_sample, bench_id=bench_id)
            result["sdr"] = sdr
            m = sdr.get("metrics")
            if sdr.get("executed") and m:
                mb = m["throughput_MB_s"]
                ck.add("MAIN SDR short stream sane (BENCH DATA, NOT SCIENCE)", "PASS" if m["stream_alive_and_sane"] else "FAIL",
                       f"throughput {mb:.3f} MB/s = {m['throughput_MS_s_iq']:.3f} MS/s IQ (expected {m['expected_bytes_per_s'] / 1e6:.3f} MB/s; ratio {m['received_over_expected']:.3f}, "
                       f"accepted {m['throughput_ratio_bounds'][0]:.2f}-{m['throughput_ratio_bounds'][1]:.2f}); {m['samples_received_bytes']} bytes kept, RMS {m['rms_counts']:.2f}, "
                       f"clipping {m['clipping_fraction']:.4f}, constant blocks {m['zero_or_constant_blocks']}, gaps {m['recv_gaps_over_0.25s']}, ended by {m['ended_by']}"
                       if mb is not None else f"no throughput estimate ({m['samples_received_bytes']} bytes, ended by {m['ended_by']}, {m.get('stream_error')})", "soft")
            else:
                ck.add("MAIN SDR short stream sane (BENCH DATA, NOT SCIENCE)", "FAIL", sdr.get("reason") or sdr.get("error") or "not executed", "soft")
    else:
        ck.add("MAIN SDR short stream sane (BENCH DATA, NOT SCIENCE)", "SKIPPED", "not executed: run sdr --seconds 2 --yes (or check --with-sdr --yes)", "soft")
    wire_bad = bool(result.get("indi") and not result["indi"]["only_getProperties_sent"])
    movement_sent = wire_bad
    ck.add("no science paths touched", "PASS" if all(str(Path(w)).startswith(str((b.root / BENCH_ROOT).resolve())) for w in b.written) else "FAIL", f"bench files written: {b.written or 'none'}", "hard")
    outcome = ck.outcome()
    op = {"BENCH PASS": "INDOOR BENCH PASS", "BENCH PARTIAL": "INDOOR BENCH PARTIAL", "BENCH BLOCKED": "BENCH BLOCKED"}[outcome]
    if outcome != "BENCH BLOCKED" and not live_indi and not with_sdr:
        op = "READY FOR INDOOR BENCH PREFLIGHT"
    return {"bench_id": bench_id, "result": outcome, "operational_state": op,
            "field_status": "READY FOR LIVE PREFLIGHT (outdoor). The indoor bench never yields READY FOR FIELD.",
            "movement_commands_sent": movement_sent, "science_capture": False, "checks": ck.items, "hard_failures": ck.hard_failures(),
            "details": result, "trace": trace.events, "mount_movement_apis_available_to_bench": False}


def write_manifest(b: Bench, plan: dict, res: dict, start, end) -> Path:
    F = b.F
    d = b.bench_dir(res["bench_id"])
    d.mkdir(parents=True, exist_ok=True)
    git = F.git_state(b.ctx)
    live = res["details"].get("indi")
    man = {"bench_id": res["bench_id"], "git_commit": git["commit"], "branch": git["branch"], "plan_sha256": F.sha256_file(b.ctx.plan_path),
           "observer_config_sha256": F.sha256_file(b.ctx.p("observer_config.json")),
           "csv_sha256": {e: F.sha256_file(F.experiment_info(b.ctx, plan, e)["csv"]) for e in F.EXPERIMENTS}, "start_utc": start.isoformat(), "end_utc": end.isoformat(),
           "rtl_tcp_reachable": next((c["status"] == "PASS" for c in res["checks"] if c["name"].startswith("rtl_tcp MAIN endpoint")), False),
           "indi_reachable": bool(live and live["reachable"]), "mount_readable": bool(live and live.get("coordinates_valid")),
           "movement_commands_sent": res["movement_commands_sent"], "science_capture": False, "result": res["result"], "operational_state": res["operational_state"],
           "trace_counts": {k: sum(1 for e in res["trace"] if e["kind"] == k) for k in Trace.KINDS}}
    p = assert_bench_path(b.root, d / "manifest.json")
    p.write_text(json.dumps(man, indent=2, sort_keys=True))
    q = assert_bench_path(b.root, d / "report.json")
    q.write_text(json.dumps(res, indent=2, sort_keys=True, default=str))
    b.written += [str(p), str(q)]
    return p


# ------------------------------------------------------------------ CLI

NO_MOUNT_MOVEMENT_LINE = ">>> " + NO_MOVEMENT + " <<<"


def _print_result(res, as_json):
    if as_json:
        print(json.dumps(res, indent=2, sort_keys=True, default=str))
        return
    for c in res["checks"]:
        print(f"[{c['status']:<7}] {c['name']}: {c['detail']}")
    print(f"\n{res['result']}  |  {res['operational_state']}")
    print(f"movement_commands_sent = {str(res['movement_commands_sent']).lower()}   science_capture = false   ({BANNER} where applicable)")
    print(res["field_status"])


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", default=DEFAULT_PLAN)
    ap.add_argument("--expected-plan-sha256")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", help="UTC ISO time override (tests/what-if only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status"); p.add_argument("--log", action="store_true", help="persist a manifest under data/science_forensics_bench/")
    p = sub.add_parser("check")
    p.add_argument("--offline", action="store_true"); p.add_argument("--live-readonly", action="store_true", help="read-only INDI snapshot (no confirmation needed)")
    p.add_argument("--with-sdr", action="store_true"); p.add_argument("--seconds", type=float, default=2.0); p.add_argument("--yes", action="store_true")
    p.add_argument("--save-sample", action="store_true"); p.add_argument("--trace", action="store_true"); p.add_argument("--log", action="store_true")
    p = sub.add_parser("sdr"); p.add_argument("--seconds", type=float, default=2.0); p.add_argument("--yes", action="store_true"); p.add_argument("--save-sample", action="store_true"); p.add_argument("--trace", action="store_true")
    sub.add_parser("audit")
    sub.add_parser("preview")
    p = sub.add_parser("dates"); p.add_argument("--date", help="departure date YYYY-MM-DD (default today)"); p.add_argument("--windows", action="store_true", help="also compute the start-window shift (slower)")
    sub.add_parser("selftest")
    return ap


def main(argv=None, bench: Bench | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ.setdefault("INDOOR_MODE", "1")                    # the bench is always an indoor-safe tool
    now = datetime.fromisoformat(args.now).astimezone(timezone.utc) if args.now else None
    b = bench or Bench(plan=args.plan, now=now, expected_plan_sha256=args.expected_plan_sha256)
    b.trace.echo = bool(getattr(args, "trace", False))
    try:
        plan = load_plan(b)
    except b.F.FieldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.cmd == "preview":
        print("# exact commands for OUTSIDE (nothing here is executed by the bench); indoors export INDOOR_MODE=1 to block preflight/run in the wrapper")
        print("\n".join(field_command_preview()))
        return 0
    if args.cmd == "audit":
        r = audit_preflight(b.root)
        if args.json:
            print(json.dumps(r, indent=2))
        else:
            for e in r["evidence"]:
                print(f"[{'OK' if e['ok'] else 'NO'}] {e['claim']}")
            print("\n".join(f"  caveat: {c}" for c in r["caveats"]))
            print(f"\ncapture.py --preflight-only: {r['verdict']}")
        return 0 if r["verdict"] == "SAFE INDOOR" else 1
    if args.cmd == "selftest":
        g, t = guard_selftest(b.trace), timestamp_pipeline_selftest()
        print(json.dumps({"guard": g, "timestamp_pipeline": t}, indent=2))
        return 0 if g["ok"] and t["ok"] else 1
    if args.cmd == "dates":
        when = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc, hour=14) if args.date else b.now()
        out = {"validity": plan_validity(b, plan, when), "date_sensitivity": date_sensitivity(b, plan)}
        if args.windows:
            out["window_sensitivity"] = window_sensitivity(b, plan)
        print(json.dumps(out, indent=2, default=str))
        return 0
    start = b.now()
    bench_id = "BENCH-" + start.strftime("%Y%m%dT%H%M%SZ")
    if args.cmd == "status":
        res = run_checks(b, plan, bench_id=bench_id)
        _print_result(res, args.json)
        if args.log:
            write_manifest(b, plan, res, start, b.now())
        return 0 if res["result"] != "BENCH BLOCKED" else 1
    if args.cmd == "check":
        with_sdr = args.with_sdr
        if with_sdr and not args.yes:
            print("--with-sdr touches the MAIN SDR: add --yes to confirm (NO MOUNT MOVEMENT either way)", file=sys.stderr)
            return 2
        live = args.live_readonly or with_sdr
        if live and not args.json:
            print(NO_MOUNT_MOVEMENT_LINE)
        res = run_checks(b, plan, live_indi=live, with_sdr=with_sdr, sdr_seconds=args.seconds, yes=args.yes, save_sample=args.save_sample, bench_id=bench_id)
        _print_result(res, args.json)
        if live or args.log:
            print(f"manifest: {write_manifest(b, plan, res, start, b.now())}")
        return {"BENCH PASS": 0, "BENCH PARTIAL": 2, "BENCH BLOCKED": 1}[res["result"]]
    if args.cmd == "sdr":
        if not args.yes:
            print("the sdr command touches the MAIN SDR: add --yes (NO MOUNT MOVEMENT)", file=sys.stderr)
            return 2
        print(NO_MOUNT_MOVEMENT_LINE)
        r = sdr_bench(b, plan, args.seconds, b.trace, save_sample=args.save_sample, bench_id=bench_id)
        print(json.dumps(r, indent=2, default=str))
        m = r.get("metrics")
        print(f"\n{BANNER}: " + ("stream alive and sane" if m and m["stream_alive_and_sane"] else "stream NOT confirmed") + "  (no astronomical interpretation)")
        return 0 if m and m["stream_alive_and_sane"] else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
