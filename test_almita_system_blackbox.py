import signal
import subprocess
import time
from pathlib import Path

import pytest

import almita_system_blackbox as bb
import wifi_health

ROOT = Path(__file__).parent.resolve()


# ---------------------------------------------------------------- individual readers tolerate missing sources


def test_read_uptime_missing_file_returns_none(tmp_path):
    assert bb.read_uptime_seconds(tmp_path / "missing") is None


def test_read_uptime_parses_real_shape(tmp_path):
    f = tmp_path / "uptime"
    f.write_text("16069.23 48000.11\n")
    assert bb.read_uptime_seconds(f) == pytest.approx(16069.23)


def test_read_loadavg_missing_file_returns_all_none(tmp_path):
    assert bb.read_loadavg(tmp_path / "missing") == (None, None, None, None)


def test_read_loadavg_parses_real_shape(tmp_path):
    f = tmp_path / "loadavg"
    f.write_text("0.16 0.29 0.33 1/442 13775\n")
    assert bb.read_loadavg(f) == (0.16, 0.29, 0.33, 442)


def test_read_meminfo_missing_file_returns_all_none(tmp_path):
    result = bb.read_meminfo(tmp_path / "missing")
    assert result == {"mem_avail_kb": None, "mem_used_pct": None, "swap_total_kb": None, "swap_used_kb": None}


def test_read_meminfo_parses_real_shape(tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal:        8000000 kB\nMemAvailable:    6000000 kB\nSwapTotal:        466936 kB\nSwapFree:         466936 kB\n")
    result = bb.read_meminfo(f)
    assert result["mem_avail_kb"] == 6000000
    assert result["mem_used_pct"] == pytest.approx(25.0)
    assert result["swap_total_kb"] == 466936
    assert result["swap_used_kb"] == 0


def test_read_meminfo_missing_mandatory_key_falls_back_to_none(tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal:        8000000 kB\n")  # no MemAvailable
    result = bb.read_meminfo(f)
    assert result["mem_avail_kb"] is None and result["mem_used_pct"] is None


def test_read_cpu_stat_missing_file_returns_none(tmp_path):
    assert bb.read_cpu_stat(tmp_path / "missing") is None


def test_cpu_percent_since_computes_delta_not_absolute():
    prev = (1000, 800)  # total, idle
    curr = (1100, 850)  # +100 total, +50 idle -> 50% busy over the window
    assert bb.cpu_percent_since(prev, curr) == pytest.approx(50.0)


def test_cpu_percent_since_none_inputs_yield_none():
    assert bb.cpu_percent_since(None, (10, 5)) is None
    assert bb.cpu_percent_since((10, 5), None) is None


def test_cpu_percent_since_non_positive_delta_yields_none():
    # A counter reset (e.g. across a reboot between samples) must not raise
    # or produce a nonsensical negative/huge percentage.
    assert bb.cpu_percent_since((1000, 800), (900, 700)) is None


def test_read_process_count_counts_only_numeric_entries(tmp_path):
    for name in ("1", "42", "999"):
        (tmp_path / name).mkdir()
    (tmp_path / "self").mkdir()  # /proc/self is not a PID, must not be counted
    (tmp_path / "loadavg").write_text("0.1 0.1 0.1 1/1 1\n")
    assert bb.read_process_count(tmp_path) == 3


def test_read_open_fds_parses_file_nr(tmp_path):
    f = tmp_path / "file-nr"
    f.write_text("4992\t0\t9223372036854775807\n")
    assert bb.read_open_fds(f) == 4992


def test_read_tcp_sockets_counts_and_flags_almita_ports(tmp_path):
    proc = tmp_path
    (proc / "net").mkdir()
    # header + one LISTEN entry on port 8088 (0x1F98) + one non-listen entry
    header = "  sl  local_address rem_address   st\n"
    listen_8088 = "   0: 00000000:1F98 00000000:0000 0A\n"
    established = "   1: 0100007F:04D2 0100007F:9999 01\n"
    (proc / "net" / "tcp").write_text(header + listen_8088 + established)
    (proc / "net" / "tcp6").write_text(header)
    total, listening = bb.read_tcp_sockets(proc)
    assert total == 2
    assert listening[8088] is True
    assert listening[1234] is False
    assert listening[8090] is False


def test_read_tcp_sockets_missing_tables_returns_none_total(tmp_path):
    total, listening = bb.read_tcp_sockets(tmp_path)
    assert total is None
    assert listening == {1234: False, 8088: False, 8090: False}


def test_read_psi_missing_returns_none(tmp_path):
    assert bb.read_psi("cpu", tmp_path) is None  # this Pi's kernel has no /proc/pressure at all


def test_read_psi_parses_real_shape(tmp_path):
    (tmp_path / "cpu").write_text("some avg10=1.23 avg60=0.50 avg300=0.10 total=999\n")
    assert bb.read_psi("cpu", tmp_path) == pytest.approx(1.23)


def test_read_cpu_temp_missing_returns_none(tmp_path):
    assert bb.read_cpu_temp_c(tmp_path / "missing") is None


def test_read_cpu_temp_parses_millidegrees(tmp_path):
    f = tmp_path / "temp"
    f.write_text("55100\n")
    assert bb.read_cpu_temp_c(f) == pytest.approx(55.1)


def test_read_nvme_temp_finds_hwmon_named_nvme(tmp_path):
    (tmp_path / "hwmon0").mkdir()
    (tmp_path / "hwmon0" / "name").write_text("cpu_thermal\n")
    (tmp_path / "hwmon1").mkdir()
    (tmp_path / "hwmon1" / "name").write_text("nvme\n")
    (tmp_path / "hwmon1" / "temp1_input").write_text("48850\n")
    assert bb.read_nvme_temp_c(tmp_path) == pytest.approx(48.85)


def test_read_nvme_temp_absent_returns_none(tmp_path):
    (tmp_path / "hwmon0").mkdir()
    (tmp_path / "hwmon0" / "name").write_text("cpu_thermal\n")
    assert bb.read_nvme_temp_c(tmp_path) is None


def test_read_rootfs_free_gb_real_root_is_positive():
    assert bb.read_rootfs_free_gb("/") > 0


def test_read_throttled_no_vcgencmd_binary_returns_none(monkeypatch):
    monkeypatch.setattr(bb, "_VCGENCMD_PATH", None)
    assert bb.read_throttled() is None


# ---------------------------------------------------------------- build_sample: formatting and tolerance


def test_build_sample_line_has_expected_keys_and_is_single_line(monkeypatch):
    _stub_all_readers(monkeypatch)
    _, line, _ = bb.build_sample(prev_cpu_stat=None, sample_index=0, cached_throttled=None)
    assert "\n" not in line
    fields = dict(tok.split("=", 1) for tok in line.split()[1:])
    for key in ("uptime_s", "load1", "cpu_pct", "threads_total", "procs", "mem_avail_kb",
                "swap_used_kb", "cpu_temp_c", "nvme_temp_c", "throttled",
                "psi_cpu_avg10", "fds_open", "tcp_sockets", "rootfs_free_gb",
                "port_1234", "port_8088", "port_8090"):
        assert key in fields, f"missing field {key} in line: {line}"


def test_build_sample_all_readers_failing_still_produces_line_of_nas(monkeypatch):
    monkeypatch.setattr(bb, "read_uptime_seconds", lambda: None)
    monkeypatch.setattr(bb, "read_loadavg", lambda: (None, None, None, None))
    monkeypatch.setattr(bb, "read_cpu_stat", lambda: None)
    monkeypatch.setattr(bb, "read_meminfo", lambda: {"mem_avail_kb": None, "mem_used_pct": None, "swap_total_kb": None, "swap_used_kb": None})
    monkeypatch.setattr(bb, "read_cpu_temp_c", lambda: None)
    monkeypatch.setattr(bb, "read_nvme_temp_c", lambda: None)
    monkeypatch.setattr(bb, "read_psi", lambda resource: None)
    monkeypatch.setattr(bb, "read_process_count", lambda: None)
    monkeypatch.setattr(bb, "read_open_fds", lambda: None)
    monkeypatch.setattr(bb, "read_tcp_sockets", lambda: (None, {1234: False, 8088: False, 8090: False}))
    monkeypatch.setattr(bb, "read_rootfs_free_gb", lambda: None)
    monkeypatch.setattr(bb, "read_throttled", lambda: None)
    _, line, _ = bb.build_sample(prev_cpu_stat=None, sample_index=0, cached_throttled=None)
    assert "\n" not in line
    assert line.startswith(bb.utcnow_iso()[:10])  # still starts with a real ISO date, not NA
    fields = dict(tok.split("=", 1) for tok in line.split()[1:])
    assert fields["uptime_s"] == "NA"
    assert fields["throttled"] == "NA"
    assert fields["port_1234"] == "DOWN"


def test_build_sample_vcgencmd_only_polled_on_slow_cadence(monkeypatch):
    calls = []
    monkeypatch.setattr(bb, "read_throttled", lambda: calls.append(1) or "0x0")
    _stub_all_readers(monkeypatch, skip=("read_throttled",))
    # sample_index 0 and SLOW_POLL_EVERY_N_SAMPLES both poll; in between reuse cache
    bb.build_sample(None, 0, cached_throttled=None)
    for i in range(1, bb.SLOW_POLL_EVERY_N_SAMPLES):
        bb.build_sample(None, i, cached_throttled="0x0")
    bb.build_sample(None, bb.SLOW_POLL_EVERY_N_SAMPLES, cached_throttled="0x0")
    assert len(calls) == 2


def _stub_all_readers(monkeypatch, skip=()):
    stubs = {
        "read_uptime_seconds": lambda: 123.4,
        "read_loadavg": lambda: (0.1, 0.2, 0.3, 400),
        "read_cpu_stat": lambda: (1000, 800),
        "read_meminfo": lambda: {"mem_avail_kb": 6000000, "mem_used_pct": 25.0, "swap_total_kb": 466936, "swap_used_kb": 0},
        "read_cpu_temp_c": lambda: 55.1,
        "read_nvme_temp_c": lambda: 48.85,
        "read_psi": lambda resource: None,
        "read_process_count": lambda: 227,
        "read_open_fds": lambda: 4992,
        "read_tcp_sockets": lambda: (42, {1234: True, 8088: True, 8090: False}),
        "read_rootfs_free_gb": lambda: 380.6,
        "read_throttled": lambda: "0x0",
    }
    for name, fn in stubs.items():
        if name not in skip:
            monkeypatch.setattr(bb, name, fn)


# ---------------------------------------------------------------- rotation and fsync cadence


def test_rotating_handler_rotates_past_max_bytes_and_bounds_backup_count(tmp_path):
    logger = bb.build_logger(tmp_path, max_bytes=500, backup_count=2)
    try:
        for i in range(200):
            logger.info(f"line {i} " + "x" * 40)
    finally:
        for h in logger.handlers:
            h.close()
    files = sorted(tmp_path.glob("blackbox.log*"))
    names = {f.name for f in files}
    assert "blackbox.log" in names
    # backupCount=2 -> at most blackbox.log, .log.1, .log.2
    assert len(files) <= 3
    for f in files:
        assert f.stat().st_size <= 500 * 3  # generous slack; must not be unbounded


def test_rotating_handler_fsyncs_only_every_nth_emit(tmp_path, monkeypatch):
    fsync_calls = []
    monkeypatch.setattr(bb.os, "fsync", lambda fd: fsync_calls.append(fd))
    handler = bb._RotatingBlackboxHandler(str(tmp_path / "b.log"), maxBytes=10**9, backupCount=1, fsync_every=3)
    try:
        for i in range(7):
            handler.emit(_fake_record(f"sample {i}"))
    finally:
        handler.close()
    assert len(fsync_calls) == 2  # emits 3 and 6 (1-indexed multiples of 3) out of 7


def _fake_record(message):
    import logging
    return logging.LogRecord(name="bb", level=logging.INFO, pathname=__file__, lineno=0, msg=message, args=None, exc_info=None)


def test_emit_survives_write_failure_and_self_heals(tmp_path, capsys):
    # A temporarily read-only filesystem (or any other transient write
    # failure) must never crash the resident loop and must recover on its
    # own once writes succeed again - the main loop has no bespoke
    # try/except around logger.info() because logging.StreamHandler.emit()
    # already swallows a write() OSError internally (handleError(), which
    # prints to stderr and returns normally rather than raising).
    handler = bb._RotatingBlackboxHandler(str(tmp_path / "b.log"), maxBytes=10**9, backupCount=1)
    handler.setFormatter(__import__("logging").Formatter("%(message)s"))

    class BoomStream:
        def seek(self, *a, **k):
            return 0

        def tell(self):
            return 0

        def write(self, *_a, **_k):
            raise OSError(30, "Read-only file system")

        def flush(self):
            pass

    handler.stream = BoomStream()
    handler.emit(_fake_record("during read-only window"))  # must not raise
    assert "Read-only file system" in capsys.readouterr().err

    handler.stream = open(tmp_path / "b.log", "a")
    handler.emit(_fake_record("after recovery"))
    handler.close()
    assert "after recovery" in (tmp_path / "b.log").read_text()


# ---------------------------------------------------------------- short real subprocess run (CLI wiring + clean shutdown)


def test_cli_short_real_run_produces_samples_and_clean_shutdown(tmp_path):
    process = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "almita_system_blackbox.py", "--interval", "1", "--log-dir", str(tmp_path)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert "ALMITA SYSTEM BLACKBOX START" in process.stdout.readline()
    time.sleep(3.5)
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=5) == 0
    remaining_stdout = process.stdout.read()
    assert "ALMITA SYSTEM BLACKBOX STOP" in remaining_stdout
    log_file = tmp_path / "blackbox.log"
    assert log_file.exists()
    lines = [l for l in log_file.read_text().splitlines() if l.strip()]
    assert 2 <= len(lines) <= 6
    for line in lines:
        assert line.count(" ") > 10  # sanity: many key=value fields present
        assert "port_1234=" in line and "port_8088=" in line and "port_8090=" in line


# ---------------------------------------------------------------- Wi-Fi/SDIO integration


def test_build_sample_without_wifi_monitor_renders_na_for_wifi_fields(monkeypatch):
    _stub_all_readers(monkeypatch)
    _, line, _ = bb.build_sample(prev_cpu_stat=None, sample_index=0, cached_throttled=None, wifi_monitor=None)
    fields = dict(tok.split("=", 1) for tok in line.split()[1:])
    for key in ("wlan_present", "wlan_operstate", "wlan_assoc", "wifi_health"):
        assert fields[key] == "NA"


def test_build_sample_with_wifi_monitor_includes_its_fields(monkeypatch):
    _stub_all_readers(monkeypatch)

    class FakeMonitor:
        def sample(self, ts):
            return {
                "wlan_present": True, "wlan_operstate": "up", "wlan_carrier": 1,
                "wlan_associated": True, "wlan_signal_dbm": -53,
                "default_route_present": True, "sdio_txfail_total": 2,
                "sdio_ctrlframe_fail_total": 1, "sdio_backplane_halt_total": 0,
                "wifi_health": "DEGRADED",
            }

    _, line, _ = bb.build_sample(prev_cpu_stat=None, sample_index=0, cached_throttled=None,
                                  wifi_monitor=FakeMonitor())
    fields = dict(tok.split("=", 1) for tok in line.split()[1:])
    assert fields["wlan_present"] == "1"
    assert fields["wlan_assoc"] == "1"
    assert fields["sdio_txfail_total"] == "2"
    assert fields["wifi_health"] == "DEGRADED"


def test_build_sample_wifi_monitor_exception_never_takes_down_the_line(monkeypatch):
    _stub_all_readers(monkeypatch)

    class ExplodingMonitor:
        def sample(self, ts):
            raise RuntimeError("boom")

    _, line, _ = bb.build_sample(prev_cpu_stat=None, sample_index=0, cached_throttled=None,
                                  wifi_monitor=ExplodingMonitor())
    assert "\n" not in line
    fields = dict(tok.split("=", 1) for tok in line.split()[1:])
    assert fields["wifi_health"] == "NA"


def test_cli_real_run_logs_wifi_fields(tmp_path):
    process = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "almita_system_blackbox.py", "--interval", "1", "--log-dir", str(tmp_path)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert "ALMITA SYSTEM BLACKBOX START" in process.stdout.readline()
    time.sleep(3.5)
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=5) == 0
    log_file = tmp_path / "blackbox.log"
    lines = [l for l in log_file.read_text().splitlines() if l.strip()]
    assert lines, "expected at least one sample"
    for line in lines:
        assert "wifi_health=" in line and "sdio_backplane_halt_total=" in line


def test_cli_no_wifi_monitor_flag_disables_it(tmp_path):
    process = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "almita_system_blackbox.py", "--interval", "1",
         "--log-dir", str(tmp_path), "--no-wifi-monitor"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert "ALMITA SYSTEM BLACKBOX START" in process.stdout.readline()
    time.sleep(2.2)
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=5) == 0
    log_file = tmp_path / "blackbox.log"
    lines = [l for l in log_file.read_text().splitlines() if l.strip()]
    assert lines
    for line in lines:
        assert "wifi_health=NA" in line


def test_cli_sigterm_also_shuts_down_cleanly(tmp_path):
    process = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "almita_system_blackbox.py", "--interval", "1", "--log-dir", str(tmp_path)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert "ALMITA SYSTEM BLACKBOX START" in process.stdout.readline()
    time.sleep(0.3)
    process.terminate()
    assert process.wait(timeout=5) == 0
    assert "ALMITA SYSTEM BLACKBOX STOP" in process.stdout.read()
