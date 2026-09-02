"""Tests for observation_preflight.py: PASS/WARNING/BLOCK aggregation, REQUIRED/OPTIONAL
criticality, and the individual check functions. All hardware access is mocked — no real
INDI/SDR connection is required.
"""
import asyncio
import json
from pathlib import Path

import pytest

import observation_preflight as pf


def test_overall_pass_when_all_pass():
    checks = [pf._check("A", "SYSTEM", pf.REQUIRED, pf.PASS, "ok"),
              pf._check("B", "RFI_REF", pf.OPTIONAL, pf.PASS, "ok")]
    assert pf._overall(checks) == pf.PASS


def test_overall_block_when_required_blocks():
    checks = [pf._check("A", "SYSTEM", pf.REQUIRED, pf.BLOCK, "bad"),
              pf._check("B", "RFI_REF", pf.OPTIONAL, pf.PASS, "ok")]
    assert pf._overall(checks) == pf.BLOCK


def test_overall_warning_when_optional_blocks_but_no_required_blocks():
    # An OPTIONAL check's own BLOCK-severity finding never escalates the
    # overall verdict past WARNING — only a REQUIRED check may BLOCK.
    checks = [pf._check("A", "RFI_REF", pf.OPTIONAL, pf.BLOCK, "port busy"),
              pf._check("B", "SYSTEM", pf.REQUIRED, pf.PASS, "ok")]
    assert pf._overall(checks) == pf.WARNING


def test_overall_warning_when_any_warning_and_no_block():
    checks = [pf._check("A", "SYSTEM", pf.REQUIRED, pf.WARNING, "meh"),
              pf._check("B", "SYSTEM", pf.REQUIRED, pf.PASS, "ok")]
    assert pf._overall(checks) == pf.WARNING


# --------------------------------------------------------------- RFI_REF / QUICKLOOK / CONSOLE

def test_rfi_ref_check_disabled_is_pass():
    result = pf._rfi_ref_check({"enabled": False}, 1235)
    assert result["status"] == pf.PASS
    assert result["criticality"] == pf.OPTIONAL


def test_rfi_ref_check_free_port_is_pass(monkeypatch):
    monkeypatch.setattr(pf.dual_sdr_benchmark, "listening_pid", lambda host, port: None)
    result = pf._rfi_ref_check({"enabled": True}, 1235)
    assert result["status"] == pf.PASS


def test_rfi_ref_check_foreign_pid_is_warning_never_block(monkeypatch):
    monkeypatch.setattr(pf.dual_sdr_benchmark, "listening_pid", lambda host, port: 4242)
    result = pf._rfi_ref_check({"enabled": True}, 1235)
    assert result["status"] == pf.WARNING
    assert result["criticality"] == pf.OPTIONAL
    assert "4242" in result["detail"]


def test_quicklook_check_disabled_is_pass():
    assert pf._quicklook_check({"enabled": False})["status"] == pf.PASS


def test_quicklook_check_valid_profile_is_pass(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"ok": True}))
    result = pf._quicklook_check({"enabled": True, "calibration_profile_path": str(profile)})
    assert result["status"] == pf.PASS


def test_quicklook_check_missing_profile_is_warning_not_block(tmp_path):
    result = pf._quicklook_check({"enabled": True, "calibration_profile_path": str(tmp_path / "missing.json")})
    assert result["status"] == pf.WARNING
    assert result["criticality"] == pf.OPTIONAL


def test_console_check_missing_web_root_is_warning(monkeypatch):
    monkeypatch.setattr(pf.Path, "exists", lambda self: False)
    result = pf._console_check()
    assert result["status"] == pf.WARNING
    assert result["criticality"] == pf.OPTIONAL


# --------------------------------------------------------------- OnStep clock check

def _fake_process(stdout_bytes: bytes):
    class _FakeProcess:
        async def communicate(self):
            return stdout_bytes, b""
    return _FakeProcess()


# --------------------------------------------------------------------------
# _onstep_time_snapshot_check: TIME_UTC.UTC is NOT a live clock (proven by
# read-only field investigation on 2026-09-02 — frozen across repeated
# reads and across fresh client reconnects; INDI's own timestamp= attribute
# tracks message-send time, not value-update time). Its apparent offset
# from host UTC is an indeterminate mix of real drift + unknown staleness,
# so this check is WARNING-only, OPTIONAL criticality, and NEVER BLOCKs —
# see the function's own docstring for the full evidence record.
# --------------------------------------------------------------------------

def _onstep_offset_seconds(seconds_ago: float, monkeypatch):
    import datetime
    snapshot = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)).isoformat()

    async def fake_exec(*args, **kwargs):
        return _fake_process(f"Mount.TIME_UTC.UTC={snapshot}\n".encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return asyncio.run(pf._onstep_time_snapshot_check("localhost", 7624, "Mount"))


def test_onstep_time_snapshot_offset_20s_pass(monkeypatch):
    result = _onstep_offset_seconds(20, monkeypatch)
    assert result["status"] == pf.PASS
    assert result["criticality"] == pf.OPTIONAL
    assert "within" in result["detail"]


def test_onstep_time_snapshot_offset_59s_pass(monkeypatch):
    result = _onstep_offset_seconds(59, monkeypatch)
    assert result["status"] == pf.PASS


def test_onstep_time_snapshot_offset_61s_warning_never_block(monkeypatch):
    result = _onstep_offset_seconds(61, monkeypatch)
    assert result["status"] == pf.WARNING
    assert result["status"] != pf.BLOCK
    assert "clock drift cannot be inferred" in result["detail"]


def test_onstep_time_snapshot_offset_218s_warning_never_block(monkeypatch):
    """The real value observed during field validation (2026-09-02)."""
    result = _onstep_offset_seconds(218.1, monkeypatch)
    assert result["status"] == pf.WARNING
    assert result["status"] != pf.BLOCK


def test_onstep_time_snapshot_offset_3377s_warning_never_block(monkeypatch):
    """Also a real value observed the same day, at its most extreme (~56
    min). Even this large an apparent offset must not BLOCK: there is no
    evidence-backed threshold past which staleness can be ruled out."""
    result = _onstep_offset_seconds(3377, monkeypatch)
    assert result["status"] == pf.WARNING
    assert result["status"] != pf.BLOCK
    assert result["criticality"] == pf.OPTIONAL


def test_onstep_time_snapshot_frozen_value_never_reported_as_live_drift(monkeypatch):
    """A snapshot that is identical across repeated reads (the empirically
    observed real behavior) must never be labeled "clock drift" in the
    check's own output — only "snapshot"/"offset" language."""
    result = _onstep_offset_seconds(90, monkeypatch)
    assert "clock drift" not in result["name"].lower()
    assert "drift" not in result["detail"] or "cannot be inferred" in result["detail"]
    assert result["name"] == "OnStep time snapshot"


def test_onstep_time_snapshot_timezone_parsing_still_correct(monkeypatch):
    """Z-suffixed and naive-but-UTC timestamps must both parse correctly —
    unchanged behavior from before this fix."""
    async def fake_exec(*args, **kwargs):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        return _fake_process(f"Mount.TIME_UTC.UTC={now.isoformat().replace('+00:00', 'Z')}\n".encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(pf._onstep_time_snapshot_check("localhost", 7624, "Mount"))
    assert result["status"] == pf.PASS


def test_onstep_time_snapshot_property_missing_is_warning_not_block(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _fake_process(b"")  # no "=" line: property not returned by indi_getprop

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(pf._onstep_time_snapshot_check("localhost", 7624, "Mount"))
    assert result["status"] == pf.WARNING
    assert result["status"] != pf.BLOCK
    assert "not exposed" in result["detail"]
    assert result["criticality"] == pf.OPTIONAL


# --------------------------------------------------------------- software / hash check

def _fixture_resolved_plan(mosaic_csv_path):
    return {
        "schema_version": 1,
        "resolved": {"mode": "EQUATORIAL_RECT", "placement": "FIXED_CENTER", "center_ra_hours": 6.0,
                     "center_dec_deg": -30.0, "rows": 3, "cols": 3, "spacing_deg": 2.0, "traversal": "SERPENTINE"},
        "requested": {
            "grid": {"min_altitude_deg": 10}, "capture": {"seconds": 1, "settle_seconds": 0.5},
            "main": {"center_frequency_hz": 1420405000, "sample_rate": 2400000, "gain_db": 40.2, "bias_tee": True},
            "rfi_ref": {"enabled": False, "serial": "00000002", "gain_db": 25.0},
            "quicklook": {"enabled": False, "native_grid": True, "interpolated_preview": False,
                          "calibration_profile_path": None},
        },
        "mosaic_csv_path": str(mosaic_csv_path),
    }


def test_software_check_pass_when_hash_matches_and_csv_exists(tmp_path):
    import observation_plan
    mosaic = tmp_path / "mosaic.csv"
    mosaic.write_text("point_number\n1\n")
    plan = _fixture_resolved_plan(mosaic)
    plan["observation_config_sha256"] = observation_plan.recompute_config_hash(plan)
    checks = pf._software_check(plan)
    statuses = {c["name"]: c["status"] for c in checks}
    assert statuses["Software / config hash"] == pf.PASS
    assert statuses["Software / mosaic.csv present"] == pf.PASS
    assert all(c["criticality"] == pf.REQUIRED for c in checks)


def test_software_check_blocks_on_hash_mismatch(tmp_path):
    mosaic = tmp_path / "mosaic.csv"
    mosaic.write_text("point_number\n1\n")
    plan = _fixture_resolved_plan(mosaic)
    plan["observation_config_sha256"] = "deadbeef" * 8
    checks = pf._software_check(plan)
    statuses = {c["name"]: c["status"] for c in checks}
    assert statuses["Software / config hash"] == pf.BLOCK


def _fixture_full_plan(tmp_path):
    import observation_plan
    mosaic = tmp_path / "mosaic.csv"
    mosaic.write_text("point_number\n1\n")
    plan = _fixture_resolved_plan(mosaic)
    plan["grid_session_dir"] = str(tmp_path)
    plan["storage"] = {"required_bytes": 0, "disk_safety_factor": 1.25}
    plan["observer_config"] = {}
    plan["rfi_ref"] = plan["requested"]["rfi_ref"]
    plan["quicklook"] = plan["requested"]["quicklook"]
    plan["main"] = plan["requested"]["main"]
    plan["observation_config_sha256"] = observation_plan.recompute_config_hash(plan)
    return plan


@pytest.mark.parametrize("entry_point", ["run_plan_preflight", "run_execution_preflight"])
def test_config_path_is_bare_filename_in_both_entry_points(tmp_path, monkeypatch, entry_point):
    """Regression test: CaptureExecutor resolves a non-absolute config_path
    relative to the CSV's own directory (capture.py:189-231). Passing an
    already session-dir-prefixed path here double-joins it and capture.py
    fails closed with "OBSERVER CONFIG MISSING OR INVALID" — this was caught
    during real-hardware validation of almita observe run. Both PLAN's and
    RUN's preflight construct CaptureExecutor the same way, so both must
    pass the bare filename."""
    plan = _fixture_full_plan(tmp_path)
    captured = {}

    class _FakeExecutor:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.device_name = "Mount"

        def load_observation_plan(self, resume, force):
            return False  # short-circuit before any INDI/SDR touch

    monkeypatch.setattr(pf.capture_module, "CaptureExecutor", _FakeExecutor)
    kwargs = {"runtime_dir": None} if entry_point == "run_execution_preflight" else {}
    asyncio.run(getattr(pf, entry_point)(plan, **kwargs))
    assert captured["config_path"] == "observer_config.json"


def test_software_check_blocks_on_missing_mosaic_csv(tmp_path):
    import observation_plan
    plan = _fixture_resolved_plan(tmp_path / "does_not_exist.csv")
    plan["observation_config_sha256"] = observation_plan.recompute_config_hash(plan)
    checks = pf._software_check(plan)
    statuses = {c["name"]: c["status"] for c in checks}
    assert statuses["Software / mosaic.csv present"] == pf.BLOCK


# --------------------------------------------------------------------------
# Blocker 1: PLAN must be strictly read-only — never SDRCapture.configure(),
# never SET_FREQUENCY/SET_SAMPLE_RATE/SET_GAIN/Bias-T. RUN's preflight is
# allowed to (and must continue to) configure the real MAIN SDR.
# --------------------------------------------------------------------------

class _FakeTelescope:
    writer = None

    async def connect(self):
        return True

    async def get_coordinates(self, force_refresh=True):
        return 6.0, -30.0


def _fake_executor_factory(captured):
    class _FakeExecutor:
        def __init__(self, **kwargs):
            captured.setdefault("constructions", []).append(kwargs)
            self.device_name = "Mount"
            self.telescope = None

        def load_observation_plan(self, resume, force):
            return True

        async def _read_indi_preflight_properties(self):
            return {}

        async def run_preflight(self, **kwargs):
            # This is the ONLY place real CaptureExecutor.run_preflight()
            # would call SDRCapture.configure() — recorded so the
            # run_execution_preflight test can assert it WAS reached.
            captured["run_preflight_called"] = True
            return {"checks": [], "success": True, "warnings": [], "errors": []}

    return _FakeExecutor


@pytest.fixture(autouse=True)
def _no_real_asyncio_subprocess_for_onstep_clock(monkeypatch):
    """Every test in this section constructs a full resolved plan and drives
    a real preflight function; keep the OnStep-clock indi_getprop subprocess
    call from ever actually spawning a process (it would just hang/fail
    without a real INDI server, and these tests target the SDR question,
    not the clock check)."""
    async def fake_exec(*args, **kwargs):
        class _P:
            async def communicate(self):
                return b"", b""
        return _P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def test_plan_preflight_never_calls_sdrcapture_configure(tmp_path, monkeypatch):
    """The core Blocker 1 proof: PLAN's preflight must never call
    SDRCapture.configure() — not even indirectly through CaptureExecutor."""
    plan = _fixture_full_plan(tmp_path)
    captured = {}
    monkeypatch.setattr(pf.capture_module, "CaptureExecutor", _fake_executor_factory(captured))
    monkeypatch.setattr(pf.capture_module, "INDITelescopeControl", lambda **kwargs: _FakeTelescope())

    def boom_configure(self, *args, **kwargs):
        raise AssertionError("PLAN preflight must never call SDRCapture.configure()")

    monkeypatch.setattr(pf.SDRCapture, "configure", boom_configure)

    async def fake_main_sdr_check(host, port):
        return pf._check("MAIN / SDR presence", "MAIN", pf.REQUIRED, pf.PASS, "connect-only handshake OK (mocked)")

    monkeypatch.setattr(pf, "_main_sdr_presence_check", fake_main_sdr_check)
    monkeypatch.setattr(pf, "_rtl_tcp_service_check", lambda: pf._check("x", "MAIN", pf.REQUIRED, pf.PASS, "mocked"))
    monkeypatch.setattr(pf, "_main_port_check", lambda h, p: pf._check("x", "MAIN", pf.REQUIRED, pf.PASS, "mocked"))

    report = asyncio.run(pf.run_plan_preflight(plan))
    assert report["overall"] in (pf.PASS, pf.WARNING)  # never raised AssertionError above
    assert "run_preflight_called" not in captured  # the mutating CaptureExecutor.run_preflight() path was never reached


def test_plan_preflight_uses_connect_only_never_the_configuring_run_preflight(tmp_path, monkeypatch):
    """PLAN must not call the real CaptureExecutor.run_preflight() at all
    (that method is what internally configures MAIN) — only the granular
    read-only pieces (_indi_read_only_check, _main_sdr_presence_check)."""
    plan = _fixture_full_plan(tmp_path)
    captured = {}
    monkeypatch.setattr(pf.capture_module, "CaptureExecutor", _fake_executor_factory(captured))
    monkeypatch.setattr(pf.capture_module, "INDITelescopeControl", lambda **kwargs: _FakeTelescope())

    async def fake_sdr_connect_only(host, port):
        return pf._check("MAIN / SDR presence", "MAIN", pf.REQUIRED, pf.PASS, "mocked connect-only")

    monkeypatch.setattr(pf, "_main_sdr_presence_check", fake_sdr_connect_only)
    asyncio.run(pf.run_plan_preflight(plan))
    assert captured.get("run_preflight_called") is not True


def test_execution_preflight_still_calls_real_capture_run_preflight(tmp_path, monkeypatch):
    """RUN's preflight (after operator GO) must continue to reuse the real
    CaptureExecutor.run_preflight() in full — this is the method that
    configures MAIN, appropriately so here since RUN is about to hand MAIN
    to capture.py regardless."""
    plan = _fixture_full_plan(tmp_path)
    captured = {}
    monkeypatch.setattr(pf.capture_module, "CaptureExecutor", _fake_executor_factory(captured))
    monkeypatch.setattr(pf.capture_module, "INDITelescopeControl", lambda **kwargs: _FakeTelescope())

    asyncio.run(pf.run_execution_preflight(plan, runtime_dir=None))
    assert captured.get("run_preflight_called") is True


def test_plan_preflight_detects_main_absent_via_read_only_checks(monkeypatch):
    """PLAN must still be able to report MAIN as unavailable — via read-only
    port/service checks, never by attempting to configure it."""
    monkeypatch.setattr(pf.dual_sdr_benchmark, "listening_pid", lambda host, port: None)
    result = pf._main_port_check("localhost", 1234)
    assert result["status"] == pf.BLOCK
    assert result["criticality"] == pf.REQUIRED


# --------------------------------------------------------------------------
# _main_port_check: semantic host resolution (regression for the real
# false-positive found during field validation — "localhost" was compared
# as a literal string against ss -lntp's numeric "127.0.0.1:1234" output).
# --------------------------------------------------------------------------

def _fake_getaddrinfo(records):
    """records: list of (family, ip) tuples to hand back from getaddrinfo()."""
    def fake(host, port, proto=None):
        return [(family, pf.socket.SOCK_STREAM, 6, "", (ip, port)) for family, ip in records]
    return fake


def test_main_port_check_localhost_resolves_to_ipv4_listener_pass(monkeypatch):
    """Scenario 1: host="localhost", ss shows 127.0.0.1:1234 (the real
    observed case — rtl_tcp bound with -a 127.0.0.1)."""
    monkeypatch.setattr(pf.socket, "getaddrinfo", _fake_getaddrinfo([(pf.socket.AF_INET, "127.0.0.1")]))

    def fake_listening_pid(host, port):
        return 3763 if (host, port) == ("127.0.0.1", 1234) else None

    monkeypatch.setattr(pf.dual_sdr_benchmark, "listening_pid", fake_listening_pid)
    result = pf._main_port_check("localhost", 1234)
    assert result["status"] == pf.PASS
    assert "127.0.0.1:1234" in result["detail"]
    assert "pid=3763" in result["detail"]


def test_main_port_check_localhost_resolves_to_ipv6_listener_pass(monkeypatch):
    """Scenario 2: host="localhost" resolves to both families, but only the
    IPv6 loopback is actually listening — must still PASS, matched via the
    bracketed [::1] form ss would print."""
    monkeypatch.setattr(pf.socket, "getaddrinfo",
                         _fake_getaddrinfo([(pf.socket.AF_INET, "127.0.0.1"), (pf.socket.AF_INET6, "::1")]))

    def fake_listening_pid(host, port):
        return 4242 if (host, port) == ("[::1]", 1234) else None

    monkeypatch.setattr(pf.dual_sdr_benchmark, "listening_pid", fake_listening_pid)
    result = pf._main_port_check("localhost", 1234)
    assert result["status"] == pf.PASS
    assert "[::1]:1234" in result["detail"]


def test_main_port_check_literal_ip_direct_pass(monkeypatch):
    """Scenario 3: host="127.0.0.1" passed directly (no hostname resolution
    ambiguity at all) must still PASS via the same code path."""
    monkeypatch.setattr(pf.socket, "getaddrinfo", _fake_getaddrinfo([(pf.socket.AF_INET, "127.0.0.1")]))
    monkeypatch.setattr(pf.dual_sdr_benchmark, "listening_pid",
                         lambda host, port: 3763 if (host, port) == ("127.0.0.1", 1234) else None)
    result = pf._main_port_check("127.0.0.1", 1234)
    assert result["status"] == pf.PASS


def test_main_port_check_no_listener_at_all_blocks(monkeypatch):
    """Scenario 4: host resolves fine, but nothing is actually listening on
    any resolved address — must BLOCK, and list what was checked."""
    monkeypatch.setattr(pf.socket, "getaddrinfo", _fake_getaddrinfo([(pf.socket.AF_INET, "127.0.0.1")]))
    monkeypatch.setattr(pf.dual_sdr_benchmark, "listening_pid", lambda host, port: None)
    result = pf._main_port_check("localhost", 1234)
    assert result["status"] == pf.BLOCK
    assert result["criticality"] == pf.REQUIRED
    assert "127.0.0.1" in result["detail"]


def test_main_port_check_wrong_port_blocks(monkeypatch):
    """Scenario 5: something is listening on the resolved address, but on a
    different port — must not be mistaken for a match."""
    monkeypatch.setattr(pf.socket, "getaddrinfo", _fake_getaddrinfo([(pf.socket.AF_INET, "127.0.0.1")]))
    monkeypatch.setattr(pf.dual_sdr_benchmark, "listening_pid",
                         lambda host, port: 9999 if (host, port) == ("127.0.0.1", 9999) else None)
    result = pf._main_port_check("127.0.0.1", 1234)
    assert result["status"] == pf.BLOCK


def test_main_port_check_never_touches_sdrcapture(monkeypatch):
    """Scenario 6: _main_port_check is pure ss/getaddrinfo bookkeeping — it
    must never reference SDRCapture.configure() at all (structural
    guarantee, not just behavioral)."""
    def boom_configure(self, *args, **kwargs):
        raise AssertionError("_main_port_check must never touch SDRCapture.configure()")

    monkeypatch.setattr(pf.SDRCapture, "configure", boom_configure)
    monkeypatch.setattr(pf.socket, "getaddrinfo", _fake_getaddrinfo([(pf.socket.AF_INET, "127.0.0.1")]))
    monkeypatch.setattr(pf.dual_sdr_benchmark, "listening_pid",
                         lambda host, port: 3763 if (host, port) == ("127.0.0.1", 1234) else None)
    result = pf._main_port_check("localhost", 1234)
    assert result["status"] == pf.PASS  # never raised AssertionError above


def test_main_port_check_unresolvable_host_blocks_gracefully_gaierror(monkeypatch):
    def raising_getaddrinfo(host, port, proto=None):
        raise pf.socket.gaierror("Name or service not known")

    monkeypatch.setattr(pf.socket, "getaddrinfo", raising_getaddrinfo)
    result = pf._main_port_check("no-such-host.invalid", 1234)
    assert result["status"] == pf.BLOCK


def test_main_port_check_unresolvable_host_blocks_gracefully_plain_oserror(monkeypatch):
    """Regression test: empirically, getaddrinfo() on this system's real NSS
    chain (hosts: files mdns4_minimal [NOTFOUND=return] dns) raises a plain
    OSError (observed: errno 16, "Device or resource busy" via the
    mdns4_minimal plugin) for a name that fails to resolve — NOT
    socket.gaierror. Catching only gaierror would let this propagate
    uncaught. Confirmed live on the real host, not just theorized:
        >>> socket.getaddrinfo('no-such-host.invalid.', 1234, proto=socket.IPPROTO_TCP)
        OSError: [Errno 16] Device or resource busy
    """
    def raising_getaddrinfo(host, port, proto=None):
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(pf.socket, "getaddrinfo", raising_getaddrinfo)
    result = pf._main_port_check("no-such-host.invalid", 1234)
    assert result["status"] == pf.BLOCK
    assert "did not resolve" in result["detail"]


def test_main_sdr_presence_check_never_configures(monkeypatch):
    """_main_sdr_presence_check itself: connect+close only, .configure()
    must never be called, even on a real SDRCapture instance."""
    def boom_configure(self, *args, **kwargs):
        raise AssertionError("must never call configure()")

    connect_calls = []

    async def fake_connect(self):
        connect_calls.append(True)

    async def fake_close(self):
        pass

    monkeypatch.setattr(pf.SDRCapture, "configure", boom_configure)
    monkeypatch.setattr(pf.SDRCapture, "connect", fake_connect)
    monkeypatch.setattr(pf.SDRCapture, "close", fake_close)

    result = asyncio.run(pf._main_sdr_presence_check("localhost", 1234))
    assert result["status"] == pf.PASS
    assert connect_calls == [True]


def test_main_sdr_presence_check_blocks_when_connect_fails(monkeypatch):
    async def fake_connect(self):
        raise ConnectionRefusedError("nothing listening")

    monkeypatch.setattr(pf.SDRCapture, "connect", fake_connect)
    result = asyncio.run(pf._main_sdr_presence_check("localhost", 1234))
    assert result["status"] == pf.BLOCK
    assert result["criticality"] == pf.REQUIRED


def test_rtl_tcp_service_check_blocks_when_inactive(monkeypatch):
    class _Result:
        returncode = 3
        stdout = "inactive\n"
        stderr = ""

    monkeypatch.setattr(pf.subprocess, "run", lambda *a, **k: _Result())
    result = pf._rtl_tcp_service_check()
    assert result["status"] == pf.BLOCK
    assert result["criticality"] == pf.REQUIRED


# --------------------------------------------------------------------------
# Aggregation: OnStep time snapshot WARNING must never hide/downgrade a real
# REQUIRED BLOCK elsewhere, and other REQUIRED checks must keep BLOCKing
# exactly as before this fix.
# --------------------------------------------------------------------------

def test_overall_warning_when_only_time_snapshot_warns_main_and_visibility_pass():
    """Item 9: MAIN PASS + mount PASS + time snapshot WARNING -> overall
    WARNING, never BLOCK."""
    checks = [
        pf._check("MAIN / rtl_tcp.service", "MAIN", pf.REQUIRED, pf.PASS, "active"),
        pf._check("MAIN / port listening", "MAIN", pf.REQUIRED, pf.PASS, "127.0.0.1:1234 owned by pid=3763"),
        pf._check("MAIN / SDR presence", "MAIN", pf.REQUIRED, pf.PASS, "handshake OK"),
        pf._check("INDI/mount", "MOUNT", pf.REQUIRED, pf.PASS, "valid coordinates"),
        pf._check("OnStep time snapshot", "MOUNT", pf.OPTIONAL, pf.WARNING, "differs by 218.1s, freshness unknown"),
    ]
    assert pf._overall(checks) == pf.WARNING


def test_real_required_block_is_never_hidden_by_onstep_time_snapshot_warning():
    """Adversarial check: does the new OPTIONAL/WARNING OnStep check
    accidentally swallow a genuine REQUIRED BLOCK elsewhere (e.g. disk)?
    _overall() checks REQUIRED+BLOCK first, unconditionally — must stay BLOCK."""
    checks = [
        pf._check("Disk space", "SYSTEM", pf.REQUIRED, pf.BLOCK, "free_bytes=0, required_bytes=999999999"),
        pf._check("OnStep time snapshot", "MOUNT", pf.OPTIONAL, pf.WARNING, "differs by 3377.0s, freshness unknown"),
    ]
    assert pf._overall(checks) == pf.BLOCK


def test_main_port_block_unaffected_by_onstep_fix(monkeypatch):
    """Item 11: MAIN port BLOCK still BLOCKs (independent check, untouched by this fix)."""
    monkeypatch.setattr(pf.socket, "getaddrinfo", _fake_getaddrinfo([(pf.socket.AF_INET, "127.0.0.1")]))
    monkeypatch.setattr(pf.dual_sdr_benchmark, "listening_pid", lambda host, port: None)
    result = pf._main_port_check("localhost", 1234)
    assert result["status"] == pf.BLOCK
    checks = [result, pf._check("OnStep time snapshot", "MOUNT", pf.OPTIONAL, pf.WARNING, "differs by 90.0s")]
    assert pf._overall(checks) == pf.BLOCK


def test_disk_block_unaffected_by_onstep_fix():
    """Item 12: disk BLOCK still BLOCKs."""
    result = pf._disk_check(Path("/"), required_bytes=10 ** 30)  # absurdly large requirement -> guaranteed BLOCK
    assert result["status"] == pf.BLOCK
    checks = [result, pf._check("OnStep time snapshot", "MOUNT", pf.OPTIONAL, pf.WARNING, "differs by 90.0s")]
    assert pf._overall(checks) == pf.BLOCK


def test_mount_state_block_unaffected_by_onstep_fix():
    """Item 13: mount state (INDI/mount) BLOCK still BLOCKs — independent of
    the OnStep time snapshot check, which is a separate MOUNT-category entry."""
    checks = [
        pf._check("INDI/mount", "MOUNT", pf.REQUIRED, pf.BLOCK, "EOD=Alert"),
        pf._check("OnStep time snapshot", "MOUNT", pf.OPTIONAL, pf.WARNING, "differs by 90.0s, freshness unknown"),
    ]
    assert pf._overall(checks) == pf.BLOCK


def test_no_stray_clock_drift_terminology_in_source():
    """Terminology guard: the check must never be described as measuring
    "clock drift" as a positive claim — only as something explicitly
    disclaimed. Guards against regressing the honest naming this fix
    introduced."""
    source = open("observation_preflight.py").read()
    assert '"OnStep clock"' not in source
    assert "_onstep_clock_check" not in source
    assert "DEFAULT_CLOCK_DRIFT_THRESHOLD_SEC" not in source
