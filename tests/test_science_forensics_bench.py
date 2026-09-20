"""SCIENCE FORENSICS INDOOR BENCH: infrastructure validation with ZERO possibility of mount movement. Every effect is injected/mocked:
no test touches INDI, rtl_tcp, the mount, systemd or capture.py."""
import ast
import importlib.util
import json
import os
import shutil
import struct
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


F = sys.modules.get("science_forensics_field") or _load("science_forensics_field", "science_forensics_field.py")
B = _load("science_forensics_bench_mod", "science_forensics_bench.py")

NOW = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("INDOOR_MODE", raising=False)
    yield
    os.environ.pop("INDOOR_MODE", None)


class Runner:
    """Fake subprocess layer: git answers; anything mentioning capture.py (other than git) fails the test."""
    def __init__(self, dirty=False, observer_dirty=False, head_ts=None):
        self.calls, self.dirty, self.observer_dirty = [], dirty, observer_dirty
        self.head_ts = head_ts if head_ts is not None else str(int((NOW - timedelta(hours=3)).timestamp()))

    def __call__(self, argv, timeout=120, cwd=None):
        self.calls.append(list(argv))
        assert argv[0] == "git", f"bench must only call git through the runner, got {argv}"
        base = {"argv": argv, "stdout": "", "stderr": "", "returncode": 0, "started_utc": "", "ended_utc": ""}
        if argv[1] == "rev-parse":
            base["stdout"] = SHA + "\n" if argv[2] == "HEAD" else "science-forensics-v1\n"
        elif argv[1] == "diff":
            if "--quiet" in argv:
                base["returncode"] = 1 if self.observer_dirty else 0
            else:
                base["stdout"] = "reduce_engine/x.py\n" if self.dirty else ""
        elif argv[1] == "log":
            base["stdout"] = self.head_ts + "\n"
        return base


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "examples").mkdir(parents=True)
    shutil.copyfile(ROOT / "examples/science_forensics_experiment.yaml", root / "examples/science_forensics_experiment.yaml")
    shutil.copytree(ROOT / "examples/science_forensics_experiment", root / "examples/science_forensics_experiment")
    shutil.copyfile(ROOT / "observer_config.json", root / "observer_config.json")
    for f in ("capture.py", "indi_telescope_control.py", "sdr_capture.py"):
        shutil.copyfile(ROOT / f, root / f)
    (root / "data").mkdir()
    return root


INDI_XML = """<defSwitchVector device="LX200 OnStep" name="CONNECTION" state="Ok"><defSwitch name="CONNECT">On</defSwitch><defSwitch name="DISCONNECT">Off</defSwitch></defSwitchVector>
<defNumberVector device="LX200 OnStep" name="EQUATORIAL_EOD_COORD" state="Idle" timestamp="2026-09-21T14:00:00"><defNumber name="RA">11.83</defNumber><defNumber name="DEC">-33.449</defNumber></defNumberVector>
<defSwitchVector device="LX200 OnStep" name="TELESCOPE_TRACK_STATE" state="Ok"><defSwitch name="TRACK_ON">Off</defSwitch><defSwitch name="TRACK_OFF">On</defSwitch></defSwitchVector>
<defSwitchVector device="LX200 OnStep" name="TELESCOPE_PIER_SIDE" state="Ok"><defSwitch name="PIER_WEST">On</defSwitch><defSwitch name="PIER_EAST">Off</defSwitch></defSwitchVector>
<defSwitchVector device="LX200 OnStep" name="TELESCOPE_PARK" state="Ok"><defSwitch name="PARK">Off</defSwitch><defSwitch name="UNPARK">On</defSwitch></defSwitchVector>
<defTextVector device="LX200 OnStep" name="OnStep Status" state="Ok"><defText name="Error">None</defText></defTextVector>
<defNumberVector device="Other Device" name="EQUATORIAL_EOD_COORD" state="Idle"><defNumber name="RA">1</defNumber><defNumber name="DEC">2</defNumber></defNumberVector>
"""


class FakeTransport:
    def __init__(self, xml=INDI_XML, split=True):
        self.sent, self.closed = [], False
        data = xml.encode()
        self.chunks = [data[:170], data[170:]] if split else [data]      # a split inside a vector exercises the partial-tail handling

    def send(self, data):
        self.sent.append(data)

    def recv(self, n, timeout):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class FakeSocket:
    def __init__(self, generator):
        self.sent, self.gen = [], generator

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, t):
        pass

    def recv(self, n):
        time.sleep(0.004)
        return self.gen(n)

    def close(self):
        pass


def live_bytes(n):
    return np.random.default_rng(5).normal(127.5, 25, n).clip(1, 254).astype(np.uint8).tobytes()


class FakeSDR:
    """Same surface capture.py's preflight uses: connect / configure / close; configure sends the 4 rtl_tcp commands over the socket."""
    def __init__(self, generator=live_bytes, extra_cmd=None, mode=None, host=None, port=None, verbose=False):
        self.generator, self.extra_cmd, self.socket, self.closed = generator, extra_cmd, None, False

    async def connect(self):
        self.socket = FakeSocket(self.generator)

    async def configure(self, center_freq, sample_rate, gain):
        for cmd, val in ((1, center_freq), (2, sample_rate), (3, 1), (4, round(float(gain) * 10))):
            self.socket.sendall(struct.pack(">BI", cmd, int(val)))
        if self.extra_cmd is not None:
            self.socket.sendall(struct.pack(">BI", self.extra_cmd, 1))

    async def close(self):
        self.closed = True


def make_bench(repo, now=NOW, runner=None, **kw):
    runner = runner or Runner()
    ctx = F.Ctx(root=repo, now=now, run=runner, listeners=lambda: {1234, 7624, 8090, 8088}, service_active=lambda u: "active",
                capture_procs=lambda: [], disk_free=lambda p: 10 ** 12)
    defaults = dict(listeners=lambda: {1234, 7624, 8090, 8088}, established=lambda: set(), service_active=lambda u: "active",
                    usb=lambda: [{"product": "Blog V4", "serial": "00000001"}], meminfo=lambda: {"MemAvailable": 4 * 2 ** 30, "SwapFree": 1 << 20},
                    timedatectl=lambda: {"Timezone": "Etc/UTC", "NTPSynchronized": "yes"},
                    execstart=lambda: "/usr/bin/rtl_tcp -d 00000001 -a 127.0.0.1 -p 1234 -f 1420405000 -s 2400000 -g 40.2 -T",
                    h5_samples=lambda: [(12_000_000, 10.0)] * 5, disk_free=lambda p: 10 ** 12, indi_factory=lambda h, p, t: FakeTransport(),
                    sdr_factory=lambda **k: FakeSDR())
    defaults.update(kw)
    b = B.Bench(root=repo, ctx=ctx, **defaults)
    b.runner = runner
    return b


def tree(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in Path(root).rglob("*") if p.is_file())


def result(b, **kw):
    return B.run_checks(b, B.load_plan(b), **kw)


def status_of(res, name_part):
    return next(c["status"] for c in res["checks"] if name_part in c["name"])


# ------------------------------------------------------------------ zero movement

@pytest.mark.parametrize("verb", list(B.MOVEMENT_VERBS))
def test_every_mount_verb_is_blocked_and_traced(verb):
    tr = B.Trace()
    with pytest.raises(B.MovementBlocked):
        B.mount_command(verb, tr)
    assert tr.events[-1]["kind"] == "BLOCKED_MOVEMENT" and "-> BLOCKED" in tr.events[-1]["what"] and verb in tr.events[-1]["what"]


@pytest.mark.parametrize("name,element", [("EQUATORIAL_EOD_COORD", "RA"), ("TELESCOPE_PARK", "PARK"), ("TELESCOPE_PARK", "UNPARK"), ("ON_COORD_SET", "SYNC"),
                                          ("TELESCOPE_TRACK_STATE", "TRACK_ON"), ("TELESCOPE_ABORT_MOTION", "ABORT"), ("TELESCOPE_MOTION_NS", "MOTION_NORTH")])
def test_indi_movement_writes_never_reach_the_wire(name, element):
    tr, transport = B.Trace(), FakeTransport()
    cli = B.IndiReadOnly("localhost", 7624, "LX200 OnStep", tr, transport_factory=lambda h, p, t: transport)
    xml = f'<newSwitchVector device="LX200 OnStep" name="{name}"><oneSwitch name="{element}">On</oneSwitch></newSwitchVector>'
    with pytest.raises(B.MovementBlocked):
        cli._send(transport, xml)
    assert transport.sent == [] and cli.wire_log == [] and tr.count("BLOCKED_MOVEMENT") == 1


def test_connection_switch_is_also_refused_by_the_bench():
    tr, transport = B.Trace(), FakeTransport()
    cli = B.IndiReadOnly("localhost", 7624, "d", tr, transport_factory=lambda h, p, t: transport)
    with pytest.raises(B.MovementBlocked):
        cli._send(transport, '<newSwitchVector device="d" name="CONNECTION"><oneSwitch name="CONNECT">On</oneSwitch></newSwitchVector>')
    assert transport.sent == [] and tr.count("BLOCKED_WRITE") + tr.count("BLOCKED_MOVEMENT") == 1


def test_indi_reads_are_allowed_and_only_getProperties_is_sent(repo):
    b = make_bench(repo)
    snap = B.indi_bench(b, B.load_plan(b), b.trace)
    assert snap["reachable"] and snap["device_present"] and snap["connected"]
    assert snap["ra_hours"] == 11.83 and snap["dec_deg"] == -33.449 and snap["coordinates_valid"]
    assert snap["tracking"] == "off" and snap["pier_side"] == "west" and snap["park"] == "unparked" and snap["onstep_status"]["error"] == "None"
    assert snap["mount_state"] == "idle" and "Other Device" in snap["devices_seen"]
    assert snap["messages_sent"] == ["getProperties"] and snap["only_getProperties_sent"]
    kinds = [e["kind"] for e in b.trace.events]
    assert kinds.count("CONNECT") == 1 and "READ" in kinds and "WRITE" not in kinds


def test_indi_unreachable_and_bad_coordinates_are_reported_not_faked(repo):
    def refuse(h, p, t):
        raise ConnectionRefusedError("no server")
    b = make_bench(repo, indi_factory=refuse)
    snap = B.indi_bench(b, B.load_plan(b), b.trace)
    assert snap["reachable"] is False and "ConnectionRefusedError" in snap["error"]
    bad = INDI_XML.replace(">11.83<", ">99.0<")
    b2 = make_bench(repo, indi_factory=lambda h, p, t: FakeTransport(bad))
    assert B.indi_bench(b2, B.load_plan(b2), b2.trace)["coordinates_valid"] is False


def test_bench_has_no_movement_api_and_imports_no_mount_control():
    src = (ROOT / "scripts" / "science_forensics_bench.py").read_text()
    tree_ = ast.parse(src)
    imported = set()
    for n in ast.walk(tree_):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert not (imported & {"indi_telescope_control", "capture", "observation_orchestrator", "PyIndi"})
    # sdr_capture (receiver only) is imported lazily, inside sdr_bench only
    top = {n.module for n in tree_.body if isinstance(n, ast.ImportFrom) and n.module}
    assert "sdr_capture" not in top
    # every INDI 'new*Vector' literal lives in the guard self-test (refusal fixtures) or the guard itself
    allowed = {"guard_selftest", "guard_indi_message", "audit_preflight"}
    for fn in [n for n in ast.walk(tree_) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for c in ast.walk(fn):
            if isinstance(c, ast.Constant) and isinstance(c.value, str) and "<new" in c.value:
                assert fn.name in allowed, (fn.name, c.value[:40])
    names = {n.name for n in ast.walk(tree_) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not [n for n in names if n.lower() in ("goto", "sync", "park", "unpark", "set_tracking", "slew")]
    assert not __import__("re").search(r"signal\.SIG(KILL|TERM)|os\.kill|subprocess\.(Popen|call)", src)


def test_full_bench_records_zero_movement_from_the_actual_wire(repo):
    b = make_bench(repo)
    res = result(b, live_indi=True, with_sdr=True, yes=True, sdr_seconds=0.2)
    assert res["movement_commands_sent"] is False and res["science_capture"] is False
    assert res["details"]["indi"]["only_getProperties_sent"] and res["mount_movement_apis_available_to_bench"] is False
    assert status_of(res, "zero INDI writes") == "PASS"


def test_movement_seen_on_the_wire_blocks_the_bench(repo, monkeypatch):
    b = make_bench(repo)
    real = B.indi_bench

    def tainted(bench, plan, trace):
        s = real(bench, plan, trace)
        s["only_getProperties_sent"], s["messages_sent"] = False, ["getProperties", "newNumberVector"]
        return s
    monkeypatch.setattr(B, "indi_bench", tainted)
    res = result(b, live_indi=True)
    assert res["result"] == "BENCH BLOCKED" and res["movement_commands_sent"] is True and "zero INDI writes (only getProperties on the wire)" in res["hard_failures"]


# ------------------------------------------------------------------ SDR

def test_rtl_connect_and_plan_configuration_allowed_with_trace(repo):
    b = make_bench(repo)
    plan = B.load_plan(b)
    r = B.sdr_bench(b, plan, 0.2, b.trace)
    assert r["executed"] and r["banner"] == "BENCH DATA - NOT SCIENCE"
    cmds = {c["name"]: c["value"] for c in r["commands_sent"]}
    assert cmds == {"SET FREQUENCY": 1420405752, "SET SAMPLE RATE": 2400000, "SET GAIN MODE": 1, "SET GAIN": 402}
    kinds = [(e["kind"], e["what"]) for e in b.trace.events]
    assert ("CONNECT", "RTL MAIN localhost:1234") in kinds and ("CONFIGURE", "RTL SET FREQUENCY") in kinds and ("CONFIGURE", "RTL SET GAIN") in kinds
    m = r["metrics"]
    assert m["stream_alive_and_sane"] and m["rms_finite"] and m["clipping_fraction"] < 0.05 and m["zero_or_constant_blocks"] == 0
    assert "NOT assessed" in m["not_reported"] and "hydrogen" not in json.dumps(m).lower() and "detected" not in json.dumps(m).lower()


def test_bias_t_or_any_other_rtl_command_is_blocked(repo):
    b = make_bench(repo, sdr_factory=lambda **k: FakeSDR(extra_cmd=0x0e))            # 0x0e = bias-T on some rtl_tcp builds
    with pytest.raises(B.MovementBlocked):
        B.sdr_bench(b, B.load_plan(b), 0.2, b.trace)
    assert b.trace.count("BLOCKED_WRITE") == 1


def test_dead_or_clipped_streams_are_not_declared_sane():
    n = 2 * 2_400_000
    dead = B.sdr_stream_metrics([bytes([128]) * n], 2_400_000, 1.0, 1.0, 0)
    assert not dead["stream_alive_and_sane"] and dead["zero_or_constant_blocks"] > 0
    clipped = B.sdr_stream_metrics([bytes([0, 255]) * (n // 2)], 2_400_000, 1.0, 1.0, 0)
    assert not clipped["stream_alive_and_sane"] and clipped["clipping_fraction"] > 0.9
    short = B.sdr_stream_metrics([live_bytes(1000)], 2_400_000, 1.0, 1.0, 0)
    assert not short["stream_alive_and_sane"]
    assert B.sdr_stream_metrics([], 2_400_000, 1.0, 1.0, 0)["samples_received_bytes"] == 0


def test_sdr_stream_length_is_limited(repo):
    b = make_bench(repo)
    for s in (0, -1, 5.5):
        with pytest.raises(ValueError):
            B.sdr_bench(b, B.load_plan(b), s, b.trace)


def test_busy_main_sdr_is_never_touched(repo):
    touched = []
    b = make_bench(repo, established=lambda: {1234}, sdr_factory=lambda **k: touched.append(1) or FakeSDR())
    r = B.sdr_bench(b, B.load_plan(b), 0.2, b.trace)
    assert r["executed"] is False and r["main_rtl_tcp_state"]["state"] == "BUSY" and not touched


def test_rtl_states_available_busy_unknown(repo):
    plan = B.load_plan(make_bench(repo))
    assert B.ports_and_services(make_bench(repo), plan)["main_rtl_tcp"]["state"] == "AVAILABLE"
    assert B.ports_and_services(make_bench(repo, established=lambda: {1234}), plan)["main_rtl_tcp"]["state"] == "BUSY"
    assert B.ports_and_services(make_bench(repo, listeners=lambda: set()), plan)["main_rtl_tcp"]["state"] == "UNKNOWN"
    assert B.ports_and_services(make_bench(repo, established=lambda: None), plan)["main_rtl_tcp"]["state"] == "UNKNOWN"


def test_sdr_sample_is_bench_marked_and_stays_inside_the_bench_dir(repo):
    b = make_bench(repo)
    r = B.sdr_bench(b, B.load_plan(b), 0.2, b.trace, save_sample=True, bench_id="BENCH-T")
    p = Path(r["sample_file"])
    assert p.is_file() and "BENCH_NOT_SCIENCE" in p.name and (repo / B.BENCH_ROOT).resolve() in p.resolve().parents
    side = json.loads(p.with_suffix(".json").read_text())
    assert side["banner"] == "BENCH DATA - NOT SCIENCE" and "never input to REDUCE" in side["note"]
    assert not any((repo / "data" / d).exists() for d in ("mosaic", "reduced", "iq", "science_forensics_field"))


def test_sdr_config_report_compares_with_plan(repo):
    b = make_bench(repo)
    rep = B.sdr_config_report(b, B.load_plan(b))
    assert rep["plan_center_frequency_hz"] == 1420405752 and rep["plan_sample_rate_hz"] == 2400000 and rep["plan_gain_db"] == 40.2 and rep["rtl_tcp_port"] == 1234
    assert rep["service"]["serial"] == "00000001" and rep["service"]["bias_tee_flag_-T"] is True
    assert rep["service_vs_plan"]["gain_db_equal"] and rep["service_vs_plan"]["bias_tee_matches_plan"] and rep["service_vs_plan"]["frequency_equal"] is False


def test_yes_is_required_for_anything_that_touches_the_sdr(repo, capsys):
    b = make_bench(repo)
    assert B.main(["check", "--with-sdr"], bench=b) == 2
    assert B.main(["sdr", "--seconds", "0.2"], bench=b) == 2
    assert "--yes" in capsys.readouterr().err
    assert not [e for e in b.trace.events if e["kind"] in ("CONNECT", "CONFIGURE")]


# ------------------------------------------------------------------ hashes and provenance

def test_bench_passes_offline_status_as_ready_for_indoor_bench_and_writes_nothing(repo, capsys):
    b = make_bench(repo)
    before = tree(repo)
    rc = B.main(["status"], bench=b)
    out = capsys.readouterr().out
    assert rc == 0 and "READY FOR INDOOR BENCH PREFLIGHT" in out and "BENCH PARTIAL" in out
    assert "READY FOR FIELD" not in out.replace("never yields READY FOR FIELD", "")
    assert tree(repo) == before and not (repo / B.BENCH_ROOT).exists()


def test_plan_hash_is_enforced(repo):
    p = repo / "examples/science_forensics_experiment.yaml"
    p.write_text(p.read_text().replace("READY FOR LIVE PREFLIGHT", "READY FOR LIVE PREFLIGHT."))
    res = result(make_bench(repo))
    assert res["result"] == "BENCH BLOCKED" and "plan sha256 (pinned)" in res["hard_failures"] and res["operational_state"] == "BENCH BLOCKED"


def test_non_default_plan_needs_an_explicit_expected_sha(repo):
    b = make_bench(repo, expected_plan_sha256=None)
    b.expected_plan_sha256 = None
    assert result(b)["result"] == "BENCH BLOCKED"
    b.expected_plan_sha256 = F.sha256_file(b.ctx.plan_path)
    assert "plan sha256 (pinned)" not in result(b)["hard_failures"]


@pytest.mark.parametrize("exp", ["A", "B", "AB"])
def test_csv_hash_is_enforced_per_experiment(repo, exp):
    p = repo / "examples/science_forensics_experiment" / exp / "mosaic.csv"
    p.write_text(p.read_text() + " ")
    res = result(make_bench(repo))
    assert res["result"] == "BENCH BLOCKED" and f"CSV {exp} sha256 (full, from plan)" in res["hard_failures"]


def test_observer_config_is_read_only_and_differences_are_reported(repo):
    oc = repo / "observer_config.json"
    before = F.sha256_file(oc)
    res = result(make_bench(repo, runner=Runner(observer_dirty=True)))
    assert "observer_config.json unmodified vs HEAD (read-only)" in res["hard_failures"]
    assert F.sha256_file(oc) == before                                              # the bench never writes it
    oc.write_text(oc.read_text().replace("-33.4489", "-30.0"))
    res2 = result(make_bench(repo))
    assert status_of(res2, "equals the one the plan was designed with") == "WARN" and "site/hardware entries changed" in next(c["detail"] for c in res2["checks"] if "designed with" in c["name"])


def test_frozen_module_change_blocks(repo):
    assert result(make_bench(repo, runner=Runner(dirty=True)))["result"] == "BENCH BLOCKED"


# ------------------------------------------------------------------ clock

def test_clock_checks(repo):
    assert status_of(result(make_bench(repo)), "UTC timezone-aware") == "PASS"
    naive = datetime(2026, 9, 21, 14, 0)
    assert "UTC timezone-aware" in result(make_bench(repo, now=naive))["hard_failures"]
    seq = iter([1.0, 1.05, 1.1, 1.0, 1.2, 1.25])                                       # goes backwards
    res = result(make_bench(repo, mono=lambda: next(seq), sleep=lambda s: None))
    assert "monotonic clock sane" in res["hard_failures"]
    old = result(make_bench(repo, runner=Runner(head_ts=str(int((NOW + timedelta(days=30)).timestamp())))))
    assert "system clock sane (not before the HEAD commit)" in old["hard_failures"]


def test_timestamp_pipeline_uses_actual_uneven_times():
    r = B.timestamp_pipeline_selftest()
    assert r["ok"] and abs(r["recovered_offset_hz"] - 6919.0) < 1e-6 and r["bracket"] == [0, 2] and r["gap_s"] == 70.0
    assert abs(r["planned_schedule_would_give_hz"] - r["true_offset_hz"]) > 100


# ------------------------------------------------------------------ storage, states, outcome logic

def test_storage_estimate_uses_real_sizes_and_the_capture_bound(repo):
    b = make_bench(repo)
    st = B.storage_estimate(b, B.load_plan(b))
    assert st["bytes_per_capture_empirical"] == 12_000_000 and st["bytes_per_capture_uncompressed_bound"] == 48_000_000
    assert st["experiments"]["B"]["n_captures"] == 65 and st["experiments"]["B"]["estimated_bytes"] == 65 * 12_000_000
    assert st["experiments"]["AB"]["with_1.25_safety"] == int(105 * 48_000_000 * 1.25) and st["experiments"]["A"]["ok"]
    low = make_bench(repo, disk_free=lambda p: 10 ** 9)
    assert "storage for A/B/AB (real historical capture sizes)" in result(low)["hard_failures"]


def test_full_pass_only_when_everything_was_executed_and_never_ready_for_field(repo, capsys):
    b = make_bench(repo)
    rc = B.main(["check", "--live-readonly", "--with-sdr", "--yes", "--seconds", "0.2"], bench=b)
    out = capsys.readouterr().out
    assert rc == 0 and "BENCH PASS" in out and "INDOOR BENCH PASS" in out
    assert "READY FOR FIELD" not in out.replace("never yields READY FOR FIELD", "")


def test_partial_when_sdr_unavailable_and_blocked_never_hidden(repo):
    b = make_bench(repo, listeners=lambda: {7624}, usb=lambda: [])
    res = result(b, live_indi=True, with_sdr=True, yes=True, sdr_seconds=0.2)
    assert res["result"] == "BENCH PARTIAL" and res["operational_state"] == "INDOOR BENCH PARTIAL" and not res["hard_failures"]
    assert status_of(res, "MAIN SDR short stream") == "FAIL"


def test_rfi_ref_absence_never_blocks(repo):
    res = result(make_bench(repo, usb=lambda: [{"product": "Blog V4", "serial": "00000001"}]))
    rfi = next(c for c in res["checks"] if c["name"].startswith("RFI_REF"))
    assert rfi["status"] == "WARN" and rfi["level"] == "info" and res["result"] != "BENCH BLOCKED"


def test_indi_down_gives_partial_not_pass(repo):
    def refuse(h, p, t):
        raise ConnectionRefusedError("x")
    res = result(make_bench(repo, indi_factory=refuse), live_indi=True)
    assert res["result"] == "BENCH PARTIAL" and status_of(res, "INDI reachable") == "FAIL"


def test_manifest_fields_and_location(repo):
    b = make_bench(repo)
    start = b.now()
    res = result(b, live_indi=True, bench_id="BENCH-TEST")
    p = B.write_manifest(b, B.load_plan(b), res, start, b.now())
    man = json.loads(p.read_text())
    for k in ("bench_id", "git_commit", "branch", "plan_sha256", "observer_config_sha256", "csv_sha256", "start_utc", "end_utc", "rtl_tcp_reachable", "indi_reachable",
              "mount_readable", "movement_commands_sent", "science_capture"):
        assert k in man
    assert man["movement_commands_sent"] is False and man["science_capture"] is False and man["indi_reachable"] and man["mount_readable"]
    assert man["plan_sha256"] == B.PINNED_PLAN_SHA256 and set(man["csv_sha256"]) == {"A", "B", "AB"} and man["git_commit"] == SHA
    assert (repo / B.BENCH_ROOT / "BENCH-TEST").resolve() == p.parent.resolve()
    assert all(str(Path(w)).startswith(str((repo / B.BENCH_ROOT).resolve())) for w in b.written)


def test_live_readonly_persists_a_manifest_but_status_does_not(repo, capsys):
    b = make_bench(repo)
    B.main(["check", "--live-readonly"], bench=b)
    assert list((repo / B.BENCH_ROOT).glob("BENCH-*/manifest.json"))
    b2 = make_bench(repo, now=NOW + timedelta(hours=1))
    n = len(list((repo / B.BENCH_ROOT).glob("BENCH-*")))
    B.main(["status"], bench=b2)
    assert len(list((repo / B.BENCH_ROOT).glob("BENCH-*"))) == n


@pytest.mark.parametrize("sub", ["data/mosaic/X", "data/reduced/X", "data/iq/X", "data/IQ/x", "data/science_forensics_field/CID", "data/science_forensics_bench/../mosaic/X", "elsewhere"])
def test_bench_output_can_never_enter_science_paths(repo, sub):
    with pytest.raises(B.MovementBlocked):
        B.assert_bench_path(repo, repo / sub)
    assert B.assert_bench_path(repo, repo / B.BENCH_ROOT / "BENCH-1" / "x.json")


# ------------------------------------------------------------------ audit of the REAL preflight path

def test_real_capture_preflight_path_is_safe_indoor():
    r = B.audit_preflight(ROOT)
    assert r["verdict"] == "SAFE INDOOR", [e for e in r["evidence"] if not e["ok"]]
    claims = {e["claim"][:30]: e for e in r["evidence"]}
    assert all(e["ok"] for e in r["evidence"])
    gate = r["evidence"][0]
    assert gate["gate_line"] < min(gate["execute_call_lines"])
    indi = next(e for e in r["evidence"] if e["claim"].startswith("INDI writes"))
    assert [w["property"] for w in indi["writes"]] == ["CONNECTION"] and indi["guarded_by_CONNECT_state_check"]
    sdr = next(e for e in r["evidence"] if e["claim"].startswith("SDR commands"))
    assert sdr["commands"] == ["0x01 SET FREQUENCY", "0x02 SET SAMPLE RATE", "0x03 SET GAIN MODE", "0x04 SET GAIN"]
    assert next(e for e in r["evidence"] if e["claim"].startswith("no movement primitive"))["found"] == []


@pytest.mark.parametrize("mutation", ["goto_in_preflight", "tracking_in_preflight", "gate_removed", "bias_command", "indi_write_on_coordinates"])
def test_audit_detects_a_preflight_that_could_move_or_change_the_mount(repo, mutation):
    cap = (repo / "capture.py").read_text()
    indi = (repo / "indi_telescope_control.py").read_text()
    sdr = (repo / "sdr_capture.py").read_text()
    if mutation == "goto_in_preflight":
        cap = cap.replace("            try:\n                ra, dec = await self.telescope.get_coordinates(force_refresh=True)", "            try:\n                await self.telescope.goto(1.0, -30.0)\n                ra, dec = await self.telescope.get_coordinates(force_refresh=True)", 1)
    elif mutation == "tracking_in_preflight":
        cap = cap.replace("            try:\n                ra, dec = await self.telescope.get_coordinates(force_refresh=True)", "            try:\n                await self.ensure_tracking_off()\n                ra, dec = await self.telescope.get_coordinates(force_refresh=True)", 1)
    elif mutation == "gate_removed":
        cap = cap.replace("if not should_execute_after_preflight(preflight, args.preflight_only):", "if False:", 1)
    elif mutation == "bias_command":
        sdr = sdr.replace("await send(0x04, round(gain_value * 10))", "await send(0x04, round(gain_value * 10))\n            await send(0x0e, 1)", 1)
    elif mutation == "indi_write_on_coordinates":
        indi = indi.replace("get_coords = f'<getProperties device=\"{self.device_name}\" name=\"EQUATORIAL_EOD_COORD\" version=\"1.7\"/>'",
                            "get_coords = f'<newNumberVector device=\"{self.device_name}\" name=\"EQUATORIAL_EOD_COORD\"/>'", 1)
    (repo / "capture.py").write_text(cap)
    (repo / "indi_telescope_control.py").write_text(indi)
    (repo / "sdr_capture.py").write_text(sdr)
    assert cap != (ROOT / "capture.py").read_text() or indi != (ROOT / "indi_telescope_control.py").read_text() or sdr != (ROOT / "sdr_capture.py").read_text(), "mutation did not apply"
    r = B.audit_preflight(repo)
    assert r["verdict"] == "NOT SAFE INDOOR", mutation


def test_audit_never_executes_capture_py(repo):
    (repo / "capture.py").write_text((repo / "capture.py").read_text() + "\nraise SystemExit('executed!')\n")
    assert B.audit_preflight(repo)["verdict"] in ("SAFE INDOOR", "NOT SAFE INDOOR")      # static only: importing it would have exited


# ------------------------------------------------------------------ previews, dates, windows

def test_field_command_preview_is_exact():
    exp = ['S="./.venv/bin/python scripts/science_forensics_field.py"']
    for e in ("A", "B", "AB"):
        exp += [f"$S status {e}", f"$S precheck {e}", f"$S preflight {e}", f"$S run {e} --dry-run"]
    assert B.field_command_preview() == exp
    assert not [l for l in exp if l.endswith(" run A") or l.endswith(" run B") or l.endswith(" run AB")]       # `run` is never previewed without --dry-run
    text = (ROOT / "docs" / "SCIENCE_FORENSICS_FIELD_RUNBOOK.md").read_text()
    for line in exp[1:]:
        assert line in text


def test_plan_validity_and_date_sensitivity(repo):
    b = make_bench(repo)
    plan = B.load_plan(b)
    ok = B.plan_validity(b, plan, datetime.fromisoformat(plan["design_epoch_utc"]))
    assert ok["status"] == "VALID" and all(v["status"] == "VALID" for v in ok["by_experiment"].values()) and ok["basis"].startswith("CALCULATED ONLY")
    later = B.plan_validity(b, plan, datetime.fromisoformat(plan["design_epoch_utc"]) + timedelta(days=180))
    assert later["status"] in ("VALID", "REGENERATE RECOMMENDED", "INVALID")
    if later["status"] != "VALID":
        assert later["by_experiment"]["B"]["regenerate_command"] and "design --epoch" in later["by_experiment"]["B"]["regenerate_command"]
    sens = B.date_sensitivity(b, plan, days=(0, 30), scan_days=[0, 30, 60, 90])
    plan_pair = plan["predicted_lsrk_shift"]["pairwise"]["B-A"]["delta_lsrk_shift_channels"]
    assert abs(sens["by_day"][0]["B-A"] - plan_pair) < 0.5 and set(sens["delta_channels_per_day_mean"]) == {"B-A", "C-A"}
    assert sens["basis"] == "CALCULATED ONLY - NOT FIELD VALIDATED" and sens["same_local_sidereal_time"] is True


def test_plan_validity_flags_a_collapsed_contrast(repo, monkeypatch):
    b = make_bench(repo)
    plan = B.load_plan(b)
    monkeypatch.setattr(F, "check_epoch", lambda ctx, p, exp, when: {"status": "BLOCKED FOR PLANNED GEOMETRY", "reason": "contrast < 10 channels", "days_from_plan_epoch": 200.0,
                                                                     "regenerate_command": "design --epoch X", "pairwise_shift_channels": {}} if exp != "A" else
                        {"status": "OK", "days_from_plan_epoch": 200.0, "regenerate_command": "x"})
    r = B.plan_validity(b, plan, NOW)
    assert r["status"] == "INVALID" and r["by_experiment"]["A"]["status"] == "VALID" and r["by_experiment"]["B"]["reason"] == "contrast < 10 channels"


def test_window_sensitivity_is_calculated_only(repo):
    b = make_bench(repo)
    plan = B.load_plan(b)

    def finder(pos, site, day, days, dur, **kw):
        d0 = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        shift = timedelta(minutes=-3.934 * (d0 - datetime(2026, 9, 21, tzinfo=timezone.utc)).days)
        return [{"side": "negative", "first_start_utc": (d0 + timedelta(hours=12) + shift).isoformat(), "last_start_utc": ""},
                {"side": "positive", "first_start_utc": (d0 + timedelta(hours=17) + shift).isoformat(), "last_start_utc": ""}]
    r = B.window_sensitivity(b, plan, "B", offset_days=7, finder=finder, start_date="2026-09-21")
    assert r["basis"].startswith("CALCULATED ONLY") and abs(r["sides"]["negative"]["shift_min_per_day"] - (-3.934)) < 0.01
    assert r["expected_sidereal_shift_min_per_day"] == -3.934


def test_geometry_never_blocks_the_bench(repo):
    night = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)                # planned geometry NOT valid at this time
    res = result(make_bench(repo, now=night, runner=Runner(head_ts=str(int((night - timedelta(days=1)).timestamp())))))
    assert res["result"] != "BENCH BLOCKED"


# ------------------------------------------------------------------ wrapper INDOOR_MODE guard (same movement-safety rule, field pack side)

def _field_ctx(repo, now=NOW):
    return F.Ctx(root=repo, now=now, run=Runner(), listeners=lambda: {1234, 7624}, service_active=lambda u: "active", capture_procs=lambda: [],
                 spawn=lambda *a, **k: pytest.fail("a live run must not start with INDOOR_MODE=1"), disk_free=lambda p: 10 ** 12)


def test_wrapper_refuses_preflight_and_run_when_indoor_mode_is_on(repo, monkeypatch):
    monkeypatch.setenv("INDOOR_MODE", "1")
    ctx = _field_ctx(repo)
    plan = F.load_plan(ctx)
    r = F.do_preflight(ctx, plan, "B")
    assert r["status"] == "BLOCKED" and "INDOOR_MODE=1" in r["reasons"][0]
    assert not (repo / F.FIELD_ROOT).exists()
    d = F.run_experiment(ctx, plan, "B", dry_run=True, yes=True)
    assert d["status"] == "DRY_RUN_WOULD_BLOCK" and any("INDOOR_MODE=1" in b for b in d["blockers"])
    live = F.run_experiment(ctx, plan, "B", dry_run=False, yes=True)
    assert live["status"] == "BLOCKED"
    assert F.do_preflight(ctx, plan, "B", print_only=True)["printed_only"]              # printing is still allowed


def test_indoor_bench_never_writes_the_wrapper_preflight_record(repo):
    b = make_bench(repo)
    result(b, live_indi=True, with_sdr=True, yes=True, sdr_seconds=0.2)
    assert not (repo / "data" / "science_forensics_field").exists()                       # nothing that could unlock READY FOR FIELD


def test_bench_main_sets_indoor_mode_for_its_own_process(repo, monkeypatch):
    monkeypatch.delenv("INDOOR_MODE", raising=False)
    B.main(["selftest"], bench=make_bench(repo))
    assert os.environ.get("INDOOR_MODE") == "1"


def test_every_live_command_states_no_mount_movement(repo, capsys):
    b = make_bench(repo)
    B.main(["check", "--live-readonly"], bench=b)
    assert "NO MOUNT MOVEMENT" in capsys.readouterr().out
    B.main(["sdr", "--seconds", "0.2", "--yes"], bench=make_bench(repo, now=NOW + timedelta(hours=2)))
    assert "NO MOUNT MOVEMENT" in capsys.readouterr().out
    B.main(["status"], bench=make_bench(repo, now=NOW + timedelta(hours=3)))
    assert "READY FOR FIELD" not in capsys.readouterr().out.replace("never yields READY FOR FIELD", "")
