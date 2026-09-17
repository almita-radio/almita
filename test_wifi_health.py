import subprocess
import time
from pathlib import Path

import pytest

import wifi_health as wh


# ---------------------------------------------------------------- passive /sys, /proc readers


def test_wlan_present_true_when_dir_exists(tmp_path):
    (tmp_path / "wlan0").mkdir()
    assert wh.wlan_present("wlan0", sys_root=tmp_path) is True


def test_wlan_present_false_when_absent(tmp_path):
    assert wh.wlan_present("wlan0", sys_root=tmp_path) is False


def test_read_carrier_parses_value(tmp_path):
    iface = tmp_path / "wlan0"
    iface.mkdir()
    (iface / "carrier").write_text("1\n")
    assert wh.read_carrier("wlan0", sys_root=tmp_path) == 1


def test_read_carrier_missing_returns_none(tmp_path):
    assert wh.read_carrier("wlan0", sys_root=tmp_path) is None


def test_read_operstate_parses_value(tmp_path):
    iface = tmp_path / "wlan0"
    iface.mkdir()
    (iface / "operstate").write_text("down\n")
    assert wh.read_operstate("wlan0", sys_root=tmp_path) == "down"


def test_read_default_route_present_true(tmp_path):
    f = tmp_path / "route"
    f.write_text(
        "Iface\tDestination\tGateway\tFlags\n"
        "wlan0\t00000000\t0102A8C0\t0003\n"
    )
    assert wh.read_default_route_present(f) is True


def test_read_default_route_present_false_when_no_default(tmp_path):
    f = tmp_path / "route"
    f.write_text("Iface\tDestination\tGateway\tFlags\nwlan0\t0002A8C0\t00000000\t0001\n")
    assert wh.read_default_route_present(f) is False


def test_read_default_route_missing_file_returns_none(tmp_path):
    assert wh.read_default_route_present(tmp_path / "missing") is None


# ---------------------------------------------------------------- iw / nmcli, mocked subprocess


IW_LINK_CONNECTED = """Connected to 44:48:b9:49:bb:07 (on wlan0)
\tSSID: Marciano5
\tfreq: 5220
\tRX: 749496178 bytes (3275945 packets)
\tTX: 89118712 bytes (48331147 packets)
\tsignal: -53 dBm
\trx bitrate: 325.0 MBit/s
\ttx bitrate: 433.3 MBit/s

\tbss flags:\tshort-slot-time
\tdtim period:\t2
\tbeacon int:\t100
"""

IW_LINK_NOT_CONNECTED = "Not connected.\n"


def test_iw_link_info_parses_connected_output(monkeypatch):
    monkeypatch.setattr(wh, "_IW_PATH", "/usr/sbin/iw")

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["/usr/sbin/iw", "dev", "wlan0"]
        return subprocess.CompletedProcess(cmd, 0, stdout=IW_LINK_CONNECTED, stderr="")

    monkeypatch.setattr(wh.subprocess, "run", fake_run)
    info = wh.iw_link_info("wlan0")
    assert info == {
        "associated": True, "bssid": "44:48:b9:49:bb:07", "ssid": "Marciano5",
        "freq_mhz": 5220, "signal_dbm": -53, "tx_mbps": 433.3, "rx_mbps": 325.0,
    }


def test_iw_link_info_not_connected(monkeypatch):
    monkeypatch.setattr(wh, "_IW_PATH", "/usr/sbin/iw")
    monkeypatch.setattr(wh.subprocess, "run",
                         lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=IW_LINK_NOT_CONNECTED, stderr=""))
    info = wh.iw_link_info("wlan0")
    assert info["associated"] is False
    assert info["bssid"] is None


def test_iw_not_available_returns_none(monkeypatch):
    monkeypatch.setattr(wh, "_IW_PATH", None)
    assert wh.iw_link_info("wlan0") is None


def test_iw_timeout_returns_none(monkeypatch):
    monkeypatch.setattr(wh, "_IW_PATH", "/usr/sbin/iw")

    def raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1.5))

    monkeypatch.setattr(wh.subprocess, "run", raise_timeout)
    assert wh.iw_link_info("wlan0") is None


def test_iw_nonzero_exit_returns_none(monkeypatch):
    monkeypatch.setattr(wh, "_IW_PATH", "/usr/sbin/iw")
    monkeypatch.setattr(wh.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="err"))
    assert wh.iw_link_info("wlan0") is None


def test_nmcli_not_available_returns_none(monkeypatch):
    monkeypatch.setattr(wh, "_NMCLI_PATH", None)
    assert wh.nmcli_wlan_state("wlan0") is None


def test_nmcli_parses_state(monkeypatch):
    monkeypatch.setattr(wh, "_NMCLI_PATH", "/usr/bin/nmcli")
    monkeypatch.setattr(wh.subprocess, "run",
                         lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="GENERAL.STATE:100 (connected)\n", stderr=""))
    assert wh.nmcli_wlan_state("wlan0") == "100 (connected)"


def test_nmcli_failure_returns_none(monkeypatch):
    monkeypatch.setattr(wh, "_NMCLI_PATH", "/usr/bin/nmcli")

    def raise_err(cmd, **kwargs):
        raise OSError("no such command")

    monkeypatch.setattr(wh.subprocess, "run", raise_err)
    assert wh.nmcli_wlan_state("wlan0") is None


# ---------------------------------------------------------------- WifiErrorCounters (incremental journalctl)


def _journal_lines(*messages, iso="2026-09-15T12:46:20+0000"):
    return "\n".join(f"{iso} stellarmate kernel: {m}" for m in messages)


def test_counters_first_poll_uses_bounded_since_window(monkeypatch):
    monkeypatch.setattr(wh, "_JOURNALCTL_PATH", "/usr/bin/journalctl")
    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="-- cursor: s=abc\n", stderr="")

    monkeypatch.setattr(wh.subprocess, "run", fake_run)
    counters = wh.WifiErrorCounters()
    counters.poll(min_interval=0.0)
    assert "--since" in seen_cmds[0] and "-2min" in seen_cmds[0]
    assert not any(c.startswith("--after-cursor") for c in seen_cmds[0])


def test_counters_second_poll_uses_after_cursor(monkeypatch):
    monkeypatch.setattr(wh, "_JOURNALCTL_PATH", "/usr/bin/journalctl")
    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="-- cursor: s=xyz\n", stderr="")

    monkeypatch.setattr(wh.subprocess, "run", fake_run)
    counters = wh.WifiErrorCounters()
    counters.poll(min_interval=0.0)  # seeds cursor s=xyz via --since
    counters.poll(min_interval=0.0)  # must now use that cursor instead of --since
    assert "--since" not in seen_cmds[1]
    assert "--after-cursor=s=xyz" in seen_cmds[1]


def test_counters_throttled_returns_none_not_list(monkeypatch):
    monkeypatch.setattr(wh, "_JOURNALCTL_PATH", "/usr/bin/journalctl")
    monkeypatch.setattr(wh.subprocess, "run",
                         lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="-- cursor: s=abc\n", stderr=""))
    counters = wh.WifiErrorCounters()
    first = counters.poll(min_interval=60.0)
    second = counters.poll(min_interval=60.0)
    assert first == []  # ran, nothing new
    assert second is None  # throttled, did not even try


def test_counters_never_runs_full_journal_without_bound(monkeypatch):
    monkeypatch.setattr(wh, "_JOURNALCTL_PATH", "/usr/bin/journalctl")
    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="-- cursor: s=abc\n", stderr="")

    monkeypatch.setattr(wh.subprocess, "run", fake_run)
    wh.WifiErrorCounters().poll(min_interval=0.0)
    cmd = seen_cmds[0]
    assert "-b" not in cmd  # not an unbounded "whole current boot" read either
    assert "--since" in cmd and "-2min" in cmd


def test_counters_counts_each_pattern_and_records_last_error(monkeypatch):
    monkeypatch.setattr(wh, "_JOURNALCTL_PATH", "/usr/bin/journalctl")
    output = _journal_lines(
        "brcmfmac: brcmf_sdio_txfail: sdio error, abort command and terminate frame",
        "brcmfmac: brcmf_sdio_dpc: sdio ctrlframe tx failed err=-84",
        "brcmfmac: brcmf_sdio_dpc: failed backplane access over SDIO, halting operation",
    ) + "\n-- cursor: s=abc\n"
    monkeypatch.setattr(wh.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=output, stderr=""))
    counters = wh.WifiErrorCounters()
    matches = counters.poll(min_interval=0.0)
    assert counters.counts[wh.SDIO_TXFAIL] == 1
    assert counters.counts[wh.SDIO_CTRLFRAME_FAIL] == 1
    assert counters.counts[wh.SDIO_BACKPLANE_HALT] == 1
    assert counters.total() == 3
    assert len(matches) == 3
    assert counters.last_error_kind == wh.SDIO_BACKPLANE_HALT  # last line wins
    assert counters.last_error_utc == "2026-09-15T12:46:20+0000"


def test_counters_survive_journalctl_missing(monkeypatch):
    monkeypatch.setattr(wh, "_JOURNALCTL_PATH", None)
    assert wh.WifiErrorCounters().poll(min_interval=0.0) == []


def test_counters_survive_journalctl_timeout(monkeypatch):
    monkeypatch.setattr(wh, "_JOURNALCTL_PATH", "/usr/bin/journalctl")

    def raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 3))

    monkeypatch.setattr(wh.subprocess, "run", raise_timeout)
    assert wh.WifiErrorCounters().poll(min_interval=0.0) == []


def test_counters_survive_journalctl_nonzero_exit(monkeypatch):
    monkeypatch.setattr(wh, "_JOURNALCTL_PATH", "/usr/bin/journalctl")
    monkeypatch.setattr(wh.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="denied"))
    assert wh.WifiErrorCounters().poll(min_interval=0.0) == []


# ---------------------------------------------------------------- classify_wifi_health (pure)


def test_classify_wlan_absent_is_failed():
    assert wh.classify_wifi_health(present=False, carrier=None, operstate=None, associated=None,
                                    signal_dbm=None, backplane_halt_recent=False,
                                    txfail_isolated=False, txfail_persistent=False) == wh.WIFI_FAILED


def test_classify_backplane_halt_is_failed_even_if_associated():
    assert wh.classify_wifi_health(present=True, carrier=1, operstate="up", associated=True,
                                    signal_dbm=-40, backplane_halt_recent=True,
                                    txfail_isolated=False, txfail_persistent=False) == wh.WIFI_FAILED


def test_classify_persistent_txfail_is_failed():
    assert wh.classify_wifi_health(present=True, carrier=1, operstate="up", associated=True,
                                    signal_dbm=-40, backplane_halt_recent=False,
                                    txfail_isolated=False, txfail_persistent=True) == wh.WIFI_FAILED


def test_classify_not_associated_is_degraded():
    assert wh.classify_wifi_health(present=True, carrier=1, operstate="up", associated=False,
                                    signal_dbm=None, backplane_halt_recent=False,
                                    txfail_isolated=False, txfail_persistent=False) == wh.WIFI_DEGRADED


def test_classify_isolated_txfail_is_degraded():
    assert wh.classify_wifi_health(present=True, carrier=1, operstate="up", associated=True,
                                    signal_dbm=-40, backplane_halt_recent=False,
                                    txfail_isolated=True, txfail_persistent=False) == wh.WIFI_DEGRADED


def test_classify_weak_signal_is_degraded():
    assert wh.classify_wifi_health(present=True, carrier=1, operstate="up", associated=True,
                                    signal_dbm=-85, backplane_halt_recent=False,
                                    txfail_isolated=False, txfail_persistent=False) == wh.WIFI_DEGRADED


def test_classify_healthy_link_is_ok():
    assert wh.classify_wifi_health(present=True, carrier=1, operstate="up", associated=True,
                                    signal_dbm=-53, backplane_halt_recent=False,
                                    txfail_isolated=False, txfail_persistent=False) == wh.WIFI_OK


# ---------------------------------------------------------------- WifiEventLog: dedup / rate-limit / recovery


def _read_events(path):
    return path.read_text().splitlines() if path.exists() else []


def test_event_log_health_transition_emits_one_line(tmp_path):
    log = wh.WifiEventLog(tmp_path / "wifi_events.log")
    log.note_health("OK", "t0")
    log.note_health("DEGRADED", "t1")
    log.note_health("DEGRADED", "t2")  # no change -> no new line
    log.close()
    lines = _read_events(tmp_path / "wifi_events.log")
    assert sum("WIFI_STATE OK -> DEGRADED" in l for l in lines) == 1
    assert len(lines) == 1


def test_event_log_assoc_lost_and_restored(tmp_path):
    log = wh.WifiEventLog(tmp_path / "wifi_events.log")
    log.note_association(True, "t0")
    log.note_association(False, "t1")
    log.note_association(False, "t2")  # still lost -> no duplicate
    log.note_association(True, "t3")
    log.close()
    lines = _read_events(tmp_path / "wifi_events.log")
    assert sum("WIFI_ASSOC_LOST" in l for l in lines) == 1
    assert sum("WIFI_ASSOC_RESTORED" in l for l in lines) == 1


def test_event_log_backplane_halt_uses_dedicated_label(tmp_path):
    log = wh.WifiEventLog(tmp_path / "wifi_events.log")
    log.note_matches([{"kind": wh.SDIO_BACKPLANE_HALT, "utc": "t0", "message": "failed backplane access over SDIO"}], "t0")
    log.close()
    lines = _read_events(tmp_path / "wifi_events.log")
    assert any("WIFI_SDIO_BACKPLANE_HALTED" in l for l in lines)


def test_event_log_does_not_flood_on_repeated_identical_errors(tmp_path):
    """A chip erroring every ~6s must not produce a line per occurrence -
    only one first_seen line, no reminder until reminder_interval_s passes."""
    log = wh.WifiEventLog(tmp_path / "wifi_events.log", reminder_interval_s=9999)
    for i in range(50):
        log.note_matches([{"kind": wh.SDIO_TXFAIL, "utc": f"t{i}", "message": "brcmf_sdio_txfail"}], f"t{i}")
    log.close()
    lines = _read_events(tmp_path / "wifi_events.log")
    assert len(lines) == 1  # only the first_seen line, no reminder fired yet


def test_event_log_reminder_fires_after_interval_not_before(tmp_path, monkeypatch):
    log = wh.WifiEventLog(tmp_path / "wifi_events.log", reminder_interval_s=10.0)
    t = [1000.0]
    monkeypatch.setattr(wh.time, "monotonic", lambda: t[0])
    log.note_matches([{"kind": wh.SDIO_TXFAIL, "utc": "t0", "message": "m"}], "t0")
    t[0] += 5.0  # before the interval
    log.note_matches([{"kind": wh.SDIO_TXFAIL, "utc": "t1", "message": "m"}], "t1")
    t[0] += 6.0  # now past 10s total since first_seen's reminder anchor
    log.note_matches([{"kind": wh.SDIO_TXFAIL, "utc": "t2", "message": "m"}], "t2")
    log.close()
    lines = _read_events(tmp_path / "wifi_events.log")
    assert len(lines) == 2  # first_seen + exactly one reminder
    assert "ongoing count=3" in lines[1]


def test_event_log_recovery_emits_line_and_resets_condition(tmp_path):
    log = wh.WifiEventLog(tmp_path / "wifi_events.log")
    log.note_matches([{"kind": wh.SDIO_TXFAIL, "utc": "t0", "message": "m"}], "t0")
    log.note_recovery_if_clear(wh.WIFI_OK, "t1")
    log.close()
    lines = _read_events(tmp_path / "wifi_events.log")
    assert any("WIFI_SDIO_RECOVERED" in l and "total_count=1" in l for l in lines)


def test_event_log_rotates_like_blackbox_log(tmp_path):
    log = wh.WifiEventLog(tmp_path / "wifi_events.log", max_bytes=500, backup_count=1)
    for i in range(200):
        log.note_health("OK" if i % 2 else "DEGRADED", f"t{i}")
    log.close()
    assert (tmp_path / "wifi_events.log").exists()
    assert (tmp_path / "wifi_events.log.1").exists()


# ---------------------------------------------------------------- WifiMonitor integration


def test_monitor_reports_failed_when_interface_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(wh, "wlan_present", lambda interface=wh.DEFAULT_INTERFACE, sys_root=Path("/sys/class/net"): False)
    monitor = wh.WifiMonitor()
    sample = monitor.sample()
    assert sample["wlan_present"] is False
    assert sample["wifi_health"] == wh.WIFI_FAILED


def test_monitor_healthy_link_end_to_end(monkeypatch):
    monkeypatch.setattr(wh, "wlan_present", lambda interface=wh.DEFAULT_INTERFACE, sys_root=Path("/sys/class/net"): True)
    monkeypatch.setattr(wh, "read_carrier", lambda interface=wh.DEFAULT_INTERFACE, sys_root=Path("/sys/class/net"): 1)
    monkeypatch.setattr(wh, "read_operstate", lambda interface=wh.DEFAULT_INTERFACE, sys_root=Path("/sys/class/net"): "up")
    monkeypatch.setattr(wh, "read_default_route_present", lambda path=Path("/proc/net/route"): True)
    monkeypatch.setattr(wh, "iw_link_info", lambda interface=wh.DEFAULT_INTERFACE, timeout=1.5: {
        "associated": True, "bssid": "aa:bb", "ssid": "Marciano5", "freq_mhz": 5220,
        "signal_dbm": -53, "tx_mbps": 433.3, "rx_mbps": 325.0})
    monkeypatch.setattr(wh, "nmcli_wlan_state", lambda interface=wh.DEFAULT_INTERFACE, timeout=1.5: "100 (connected)")
    monkeypatch.setattr(wh, "_JOURNALCTL_PATH", None)  # no new errors this run
    monitor = wh.WifiMonitor()
    sample = monitor.sample()
    assert sample["wifi_health"] == wh.WIFI_OK
    assert sample["wlan_ssid"] == "Marciano5"
    assert sample["sdio_error_total"] == 0


def test_monitor_backplane_halt_makes_health_sticky_failed(monkeypatch):
    monkeypatch.setattr(wh, "wlan_present", lambda interface=wh.DEFAULT_INTERFACE, sys_root=Path("/sys/class/net"): True)
    monkeypatch.setattr(wh, "read_carrier", lambda interface=wh.DEFAULT_INTERFACE, sys_root=Path("/sys/class/net"): 1)
    monkeypatch.setattr(wh, "read_operstate", lambda interface=wh.DEFAULT_INTERFACE, sys_root=Path("/sys/class/net"): "up")
    monkeypatch.setattr(wh, "read_default_route_present", lambda path=Path("/proc/net/route"): True)
    monkeypatch.setattr(wh, "iw_link_info", lambda interface=wh.DEFAULT_INTERFACE, timeout=1.5: {
        "associated": True, "bssid": "aa:bb", "ssid": "Marciano5", "freq_mhz": 5220,
        "signal_dbm": -53, "tx_mbps": 433.3, "rx_mbps": 325.0})
    monkeypatch.setattr(wh, "nmcli_wlan_state", lambda interface=wh.DEFAULT_INTERFACE, timeout=1.5: None)

    calls = {"n": 0}

    def fake_poll(self, timeout=3.0, min_interval=0.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"kind": wh.SDIO_BACKPLANE_HALT, "utc": "t0", "message": "failed backplane access over SDIO"}]
        return []  # subsequent polls see nothing new, but should stay FAILED (sticky)

    monkeypatch.setattr(wh.WifiErrorCounters, "poll", fake_poll)
    monitor = wh.WifiMonitor(min_subprocess_poll_interval=0.0)
    monitor._counters.counts[wh.SDIO_BACKPLANE_HALT] = 0
    first = monitor.sample()
    second = monitor.sample()
    assert first["wifi_health"] == wh.WIFI_FAILED
    assert second["wifi_health"] == wh.WIFI_FAILED  # still within the sticky window


def test_monitor_recovers_ok_after_sticky_window_expires(monkeypatch):
    monkeypatch.setattr(wh, "wlan_present", lambda interface=wh.DEFAULT_INTERFACE, sys_root=Path("/sys/class/net"): True)
    monkeypatch.setattr(wh, "read_carrier", lambda interface=wh.DEFAULT_INTERFACE, sys_root=Path("/sys/class/net"): 1)
    monkeypatch.setattr(wh, "read_operstate", lambda interface=wh.DEFAULT_INTERFACE, sys_root=Path("/sys/class/net"): "up")
    monkeypatch.setattr(wh, "read_default_route_present", lambda path=Path("/proc/net/route"): True)
    monkeypatch.setattr(wh, "iw_link_info", lambda interface=wh.DEFAULT_INTERFACE, timeout=1.5: {
        "associated": True, "bssid": "aa:bb", "ssid": "Marciano5", "freq_mhz": 5220,
        "signal_dbm": -53, "tx_mbps": 433.3, "rx_mbps": 325.0})
    monkeypatch.setattr(wh, "nmcli_wlan_state", lambda interface=wh.DEFAULT_INTERFACE, timeout=1.5: None)

    calls = {"n": 0}

    def fake_poll(self, timeout=3.0, min_interval=0.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"kind": wh.SDIO_BACKPLANE_HALT, "utc": "t0", "message": "failed backplane access over SDIO"}]
        return []

    monkeypatch.setattr(wh.WifiErrorCounters, "poll", fake_poll)
    t = [0.0]
    monkeypatch.setattr(wh.time, "monotonic", lambda: t[0])
    monitor = wh.WifiMonitor(min_subprocess_poll_interval=0.0)
    first = monitor.sample()
    assert first["wifi_health"] == wh.WIFI_FAILED
    t[0] += wh.BACKPLANE_HALT_STICKY_S + 1.0
    recovered = monitor.sample()
    assert recovered["wifi_health"] == wh.WIFI_OK


def test_monitor_never_raises_when_journalctl_and_iw_both_missing(monkeypatch):
    monkeypatch.setattr(wh, "_IW_PATH", None)
    monkeypatch.setattr(wh, "_NMCLI_PATH", None)
    monkeypatch.setattr(wh, "_JOURNALCTL_PATH", None)
    monitor = wh.WifiMonitor()
    sample = monitor.sample()  # must not raise
    assert sample["wifi_health"] in (wh.WIFI_OK, wh.WIFI_DEGRADED, wh.WIFI_FAILED)
