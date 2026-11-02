"""Capture Pi split: instrument_host (where), instrument_agent (fixed read-only commands), instrument_forwarder (sockets)."""
import asyncio
import json
import os
import socket
import subprocess
import urllib.error
import threading
from http.server import ThreadingHTTPServer

import pytest

import instrument_agent as agent
import instrument_forwarder as fwd
import instrument_host as ih


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "almita" / "instrument.json"
    monkeypatch.setenv(ih.INSTRUMENT_FILE_ENV, str(path))
    return path


@pytest.fixture
def live_agent(monkeypatch):
    server = agent.Server(("127.0.0.1", 0), agent.Handler)
    monkeypatch.setattr(ih, "AGENT_PORT", server.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def test_the_test_suite_never_reaches_a_real_instrument_file():
    assert os.environ[ih.INSTRUMENT_FILE_ENV] == os.devnull and ih.remote_host() is None


@pytest.mark.parametrize("content", [None, "", "not json", "[]", '{"host": ""}', '{"host": "  "}', '{"host": 5}', "{}",
                                     '{"host": "bad host;rm -rf"}'])
def test_missing_or_unusable_config_means_this_machine(config, content):
    if content is not None:
        config.parent.mkdir(parents=True)
        config.write_text(content)
    assert ih.remote_host() is None and ih.where() == "this machine" and ih.describe()["remote"] is False


@pytest.mark.parametrize("host", ["192.168.50.254", "almita-pi4", "stellarmate.local", "fe80::1"])
def test_set_host_stores_an_ip_or_hostname_and_empty_goes_back_to_local(config, host):
    assert ih.set_host(host)["host"] == host and ih.remote_host() == host
    assert json.loads(config.read_text()) == {"host": host}
    assert ih.set_host("")["remote"] is False and ih.remote_host() is None


@pytest.mark.parametrize("host", ["a b", "x;reboot", "-oProxyCommand=x", "http://pi", "a..b", "x" * 300])
def test_set_host_refuses_what_is_not_a_host(config, host):
    with pytest.raises(ValueError):
        ih.set_host(host)
    assert not config.exists()


def test_run_is_plain_subprocess_locally(config):
    out = ih.run(["echo", "hi"], text=True)
    assert out.returncode == 0 and out.stdout == "hi\n"


@pytest.mark.parametrize("argv", [
    ["journalctl", "-u", "rtl_tcp.service", "-n", "0", "--show-cursor", "--no-pager"],
    ["journalctl", "-u", "rtl_tcp.service", "--after-cursor=s=ab12;i=3f;b=cd;m=1;t=2;x=ef", "-o", "cat", "--no-pager"],
    ["journalctl", "-u", "rtl_tcp.service", "--since", "Sun 2026-11-01 22:09:42 UTC", "-o", "short-iso", "--no-pager"],
    ["journalctl", "-k", "--since", "Sun 2026-11-01 22:09:42 UTC", "-o", "short-iso", "--no-pager"],
    ["systemctl", "is-active", "rtl_tcp.service"],
    ["systemctl", "show", "-p", "ExecStart", "--value", "rtl_tcp.service"],
    ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", "rtl_tcp.service"],
    ["systemctl", "show", "-p", "MainPID", "--value", "rtl_tcp.service"],
    ["ps", "-eo", "pid,args"],
    ["cat", "/proc/2658/cmdline"], ["cat", "/proc/2658/environ"],
    ["cat", "/sys/bus/w1/devices/28-0123456789ab/w1_slave"]])
def test_agent_allows_exactly_what_the_rtl_tcp_evidence_and_sensors_need(argv):
    assert agent.allowed(argv)


@pytest.mark.parametrize("argv", [
    [], ["sh", "-c", "id"], ["systemctl", "restart", "rtl_tcp.service"], ["systemctl", "stop", "rtl_tcp.service"],
    ["systemctl", "is-active", "ssh.service"], ["systemctl", "show", "-p", "Environment", "--value", "rtl_tcp.service"],
    ["journalctl", "-u", "ssh.service"], ["journalctl", "--no-pager"], ["journalctl", "-u", "rtl_tcp.service", "--vacuum-size=1"],
    ["journalctl", "-u", "rtl_tcp.service", "--since", "$(reboot)"], ["cat", "/etc/shadow"], ["cat", "/proc/1/../../etc/passwd"],
    ["cat", "/proc/self/environ"], ["ps", "aux"], ["sudo", "-n", "systemctl", "restart", "rtl_tcp.service"], [1, 2]])
def test_agent_refuses_everything_else(argv):
    assert not agent.allowed(argv)


def test_run_goes_through_the_agent_with_the_same_result_shape(config, live_agent):
    ih.set_host("127.0.0.1")
    out = ih.run(["ps", "-eo", "pid,args"], text=True)
    assert out.returncode == 0 and "PID" in out.stdout.splitlines()[0]
    with pytest.raises(subprocess.SubprocessError, match="refused"):
        ih.run(["sh", "-c", "id"])
    health = ih.agent_health()
    assert health["ok"] is True and health["agent"] == "almita-instrument-agent" and "rtlsdr_serials" in health


def test_unreachable_agent_is_an_oserror_the_callers_already_handle(config, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        monkeypatch.setattr(ih, "AGENT_PORT", s.getsockname()[1])          # bound, never listening: refused
    ih.set_host("127.0.0.1")
    with pytest.raises(OSError, match="unreachable"):
        ih.run(["systemctl", "is-active", "rtl_tcp.service"])
    assert ih.agent_health()["ok"] is False


def test_sdr_tuning_reads_go_through_instrument_host_by_default():
    import inspect
    import sdr_tuning
    for fn in (sdr_tuning.main_service_has_bias_tee_flag, sdr_tuning.main_bias_tee_state):   # (the re-attach check is
        # stubbed by conftest for every test; its signature is the same and test_sdr_tuning covers it with real_tuning)
        assert inspect.signature(fn).parameters["run"].default is ih.run


def test_forwarder_carries_bytes_both_ways_and_refuses_without_a_host(config):
    async def scenario():
        async def echo(r, w):
            w.write(b"RTL0" + await r.read(5))
            await w.drain()
            w.close()
        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]
        local = await asyncio.start_server(fwd._handler(port), "127.0.0.1", 0)
        lport = local.sockets[0].getsockname()[1]

        async def roundtrip():
            r, w = await asyncio.open_connection("127.0.0.1", lport)
            w.write(b"hello")
            await w.drain()
            try:
                data = await asyncio.wait_for(r.read(100), 3)
            except ConnectionResetError:                                  # refused: closed before any byte
                data = b""
            w.close()
            return data
        no_host = await roundtrip()
        ih.set_host("127.0.0.1")
        with_host = await roundtrip()
        upstream.close()
        local.close()
        return no_host, with_host
    no_host, with_host = asyncio.run(scenario())
    assert no_host == b"" and with_host == b"RTL0hello"


def test_web_changes_the_capture_pi_only_when_nothing_uses_the_sdr_or_the_mount(config, monkeypatch):
    import almita_web_ops as ops
    import observation_orchestrator
    monkeypatch.setattr(observation_orchestrator, "get_status", lambda: {"orchestrator": {}})
    monkeypatch.setattr(observation_orchestrator, "active_campaign_reason", lambda o: None)
    monkeypatch.setattr(ops, "busy_reason", lambda kinds: "a CALIBRATE job holds the SDR")
    with pytest.raises(ops.OpsBlocked, match="cannot change the capture Pi now"):
        ops.set_instrument_host("192.168.50.254")
    assert ih.remote_host() is None
    monkeypatch.setattr(ops, "busy_reason", lambda kinds: None)
    with pytest.raises(ValueError):
        ops.set_instrument_host("x;reboot")
    out = ops.set_instrument_host("192.168.50.254")
    assert out["host"] == "192.168.50.254" and ih.remote_host() == "192.168.50.254" and "agent" in out and "indi_port" in out
    assert ops.set_instrument_host("")["remote"] is False


def test_server_routes_get_and_post_of_the_capture_pi(config, monkeypatch):
    import json as _json
    import urllib.request
    import almita_orchestrator_server as srv
    import almita_web_ops as ops
    monkeypatch.setattr(ops, "set_instrument_host", lambda host: {"host": host, "set": True})
    server = srv.make_server("127.0.0.1", 0, authenticator=None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        got = _json.loads(urllib.request.urlopen(base + "/api/instrument", timeout=10).read())
        assert got["ok"] and got["data"]["remote"] is False
        req = urllib.request.Request(base + "/api/instrument", data=_json.dumps({"host": "pi4", "confirm": True}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        assert _json.loads(urllib.request.urlopen(req, timeout=10).read())["data"] == {"host": "pi4", "set": True}
        bad = urllib.request.Request(base + "/api/instrument", data=b'{"host": "pi4"}', method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad, timeout=10)
    finally:
        server.shutdown()


def test_agent_survives_a_burst_of_parallel_requests(config, live_agent):
    from concurrent.futures import ThreadPoolExecutor
    ih.set_host("127.0.0.1")
    with ThreadPoolExecutor(20) as ex:
        results = list(ex.map(lambda i: ih.run(["ps", "-eo", "pid,args"], text=True).returncode, range(100)))
    assert results == [0] * 100
