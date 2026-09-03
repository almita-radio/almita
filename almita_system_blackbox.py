#!/usr/bin/env python3
"""Resident lightweight system heartbeat log for the Raspberry Pi host.

Purely a host-diagnostics side-channel, unrelated to ALMITA science. It
never imports capture.py, quicklook_live.py, the SDR/rtl_tcp control code,
OnStep/GOTO, or runtime_state.py/telemetry_summary.py - those modules stay
free to change for science reasons without this file (or this file's
failures) touching them, and vice versa. It never opens a socket, spawns a
persistent child process, sets a gain, or moves anything. It only reads
/proc, /sys and (at a reduced cadence, see SLOW_POLL_EVERY_N_SAMPLES)
`vcgencmd get_throttled`, and appends one compact line per sample to a
rotating log under data/runtime/diagnostics/ (generated/non-scientific,
already excluded from version control the same way data/runtime/ is).

Motivation: after a campaign that finished cleanly (625/625, no stalls, no
disconnects), the Pi went completely unresponsive hours later for a cause
that remains INDETERMINATE - the prior boot's logs show no OOM, thermal,
undervoltage, kernel panic/lockup/RCU-stall, NVMe/PCIe error, or USB
timeout. This log exists so that if it happens again, the last ~heartbeats
before the hang are on disk instead of nothing.

Each sample is independently fault-tolerant: any single metric that can't
be read renders as NA rather than aborting the line, and one bad sample
never stops the loop (see main()'s per-cycle try/except). Cheap /proc and
/sys reads only; no external command runs every cycle (vcgencmd is the one
exception, throttled back to roughly once a minute - see
SLOW_POLL_EVERY_N_SAMPLES and read_throttled()).
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_DIR = ROOT / "data" / "runtime" / "diagnostics"
DEFAULT_INTERVAL_S = 10.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 6
# Cadence (in samples) for the two things deliberately *not* done every
# ~10s cycle: the vcgencmd subprocess call, and fsync() of the log file.
# ~6 samples at the default 10s interval is ~once a minute.
SLOW_POLL_EVERY_N_SAMPLES = 6
ALMITA_PORTS = (1234, 8088, 8090)
NA = "NA"

_VCGENCMD_PATH = shutil.which("vcgencmd")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt(value, digits: Optional[int] = None) -> str:
    if value is None:
        return NA
    if digits is not None and isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def read_uptime_seconds(path: Path = Path("/proc/uptime")) -> Optional[float]:
    try:
        return float(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_loadavg(path: Path = Path("/proc/loadavg")) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[int]]:
    """(load1, load5, load15, kernel_scheduling_entities).

    The fourth value is /proc/loadavg's own "runnable/existing" field's
    denominator - the kernel's live count of scheduling entities (threads +
    processes), already being read for load average, so reporting it as an
    approximate system-wide thread count costs nothing extra.
    """
    try:
        parts = path.read_text().split()
        load1, load5, load15 = float(parts[0]), float(parts[1]), float(parts[2])
        existing = int(parts[3].split("/")[1])
        return load1, load5, load15, existing
    except (OSError, ValueError, IndexError):
        return None, None, None, None


def read_meminfo(path: Path = Path("/proc/meminfo")) -> Dict[str, Optional[float]]:
    empty = {"mem_avail_kb": None, "mem_used_pct": None, "swap_total_kb": None, "swap_used_kb": None}
    try:
        values = {}
        for line in path.read_text().splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            parts = rest.split()
            if parts:
                values[key] = int(parts[0])  # kB
        total = values["MemTotal"]
        avail = values["MemAvailable"]
        swap_total = values.get("SwapTotal", 0)
        swap_free = values.get("SwapFree", 0)
        return {
            "mem_avail_kb": avail,
            "mem_used_pct": (total - avail) / total * 100 if total else None,
            "swap_total_kb": swap_total,
            "swap_used_kb": swap_total - swap_free,
        }
    except (OSError, ValueError, KeyError):
        return empty


def read_cpu_stat(path: Path = Path("/proc/stat")) -> Optional[Tuple[int, int]]:
    """(total_jiffies, idle_jiffies) from /proc/stat's aggregate 'cpu' line."""
    try:
        parts = path.read_text().splitlines()[0].split()
        if parts[0] != "cpu":
            return None
        values = [int(v) for v in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle
    except (OSError, ValueError, IndexError):
        return None


def cpu_percent_since(prev: Optional[Tuple[int, int]], curr: Optional[Tuple[int, int]]) -> Optional[float]:
    """CPU used over [prev sample, curr sample] - i.e. over one ~interval-second
    window, computed from two /proc/stat snapshots already taken one loop
    apart. No extra sleep()/read() needed versus a dedicated measurement."""
    if prev is None or curr is None:
        return None
    total_delta = curr[0] - prev[0]
    idle_delta = curr[1] - prev[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, (total_delta - idle_delta) / total_delta * 100))


def read_process_count(proc: Path = Path("/proc")) -> Optional[int]:
    try:
        return sum(1 for entry in proc.iterdir() if entry.name.isdigit())
    except OSError:
        return None


def read_open_fds(path: Path = Path("/proc/sys/fs/file-nr")) -> Optional[int]:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_tcp_sockets(proc: Path = Path("/proc")) -> Tuple[Optional[int], Dict[int, bool]]:
    """(total_tcp_socket_count, {almita_port: is_listening}) from /proc/net/tcp[6].

    Pure /proc parsing - never opens a real connection, never makes an HTTP
    request against the ALMITA services it is reporting on.
    """
    wanted = {f"{p:04X}": p for p in ALMITA_PORTS}
    listening = {p: False for p in ALMITA_PORTS}
    total = 0
    seen_any_table = False
    for name in ("net/tcp", "net/tcp6"):
        try:
            lines = (proc / name).read_text().splitlines()[1:]
        except OSError:
            continue
        seen_any_table = True
        total += len(lines)
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                continue
            try:
                _, port_hex = fields[1].split(":")
            except ValueError:
                continue
            if fields[3] == "0A" and port_hex.upper() in wanted:
                listening[wanted[port_hex.upper()]] = True
    return (total if seen_any_table else None), listening


def read_psi(resource: str, root: Path = Path("/proc/pressure")) -> Optional[float]:
    """avg10 from /proc/pressure/<resource>'s 'some' line. None (-> NA) on
    kernels without CONFIG_PSI / cgroup v2 PSI mounted - this repo's Pi
    kernel does not currently expose /proc/pressure at all."""
    try:
        first_line = (root / resource).read_text().splitlines()[0]
        for token in first_line.split():
            if token.startswith("avg10="):
                return float(token.split("=", 1)[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def read_cpu_temp_c(path: Path = Path("/sys/class/thermal/thermal_zone0/temp")) -> Optional[float]:
    try:
        return int(path.read_text().strip()) / 1000
    except (OSError, ValueError):
        return None


def read_nvme_temp_c(hwmon_root: Path = Path("/sys/class/hwmon")) -> Optional[float]:
    """First hwmon device named 'nvme's temp1_input, in Celsius. Device-number
    agnostic (hwmonN numbering isn't stable across boots)."""
    try:
        for name_file in sorted(hwmon_root.glob("hwmon*/name")):
            try:
                if name_file.read_text().strip() != "nvme":
                    continue
                return int((name_file.parent / "temp1_input").read_text().strip()) / 1000
            except (OSError, ValueError):
                continue
    except OSError:
        return None
    return None


def read_rootfs_free_gb(path: str = "/") -> Optional[float]:
    try:
        return shutil.disk_usage(path).free / (1024**3)
    except OSError:
        return None


def read_throttled(timeout: float = 2.0) -> Optional[str]:
    """`vcgencmd get_throttled`'s sticky undervoltage/throttle bitmask (e.g.
    "0x0"). Raspberry Pi firmware exposes this only via the VideoCore
    mailbox - there is no /proc or /sys equivalent - so this is the one
    metric here that spawns a subprocess. Bounded by `timeout` and by the
    caller polling it at SLOW_POLL_EVERY_N_SAMPLES rather than every cycle;
    the bits are sticky (persist until cleared) so infrequent polling still
    catches an event."""
    if not _VCGENCMD_PATH:
        return None
    try:
        result = subprocess.run([_VCGENCMD_PATH, "get_throttled"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text.split("=", 1)[1] if "=" in text else (text or None)


def build_sample(
    prev_cpu_stat: Optional[Tuple[int, int]],
    sample_index: int,
    cached_throttled: Optional[str],
) -> Tuple[Optional[Tuple[int, int]], str, Optional[str]]:
    """One heartbeat line. Returns (cpu_stat_for_next_call, line, throttled_for_next_call)."""
    ts = utcnow_iso()
    uptime = read_uptime_seconds()
    load1, load5, load15, threads_total = read_loadavg()
    curr_cpu_stat = read_cpu_stat()
    cpu_pct = cpu_percent_since(prev_cpu_stat, curr_cpu_stat)
    mem = read_meminfo()
    cpu_temp = read_cpu_temp_c()
    nvme_temp = read_nvme_temp_c()
    psi_cpu = read_psi("cpu")
    psi_mem = read_psi("memory")
    psi_io = read_psi("io")
    procs = read_process_count()
    fds = read_open_fds()
    tcp_total, tcp_listening = read_tcp_sockets()
    rootfs_free = read_rootfs_free_gb()
    throttled = read_throttled() if sample_index % SLOW_POLL_EVERY_N_SAMPLES == 0 else cached_throttled

    fields = [
        ts,
        f"uptime_s={_fmt(uptime, 1)}",
        f"load1={_fmt(load1, 2)}",
        f"load5={_fmt(load5, 2)}",
        f"load15={_fmt(load15, 2)}",
        f"cpu_pct={_fmt(cpu_pct, 1)}",
        f"threads_total={_fmt(threads_total)}",
        f"procs={_fmt(procs)}",
        f"mem_avail_kb={_fmt(mem['mem_avail_kb'])}",
        f"mem_used_pct={_fmt(mem['mem_used_pct'], 1)}",
        f"swap_used_kb={_fmt(mem['swap_used_kb'])}",
        f"swap_total_kb={_fmt(mem['swap_total_kb'])}",
        f"cpu_temp_c={_fmt(cpu_temp, 1)}",
        f"nvme_temp_c={_fmt(nvme_temp, 1)}",
        f"throttled={_fmt(throttled)}",
        f"psi_cpu_avg10={_fmt(psi_cpu, 2)}",
        f"psi_mem_avg10={_fmt(psi_mem, 2)}",
        f"psi_io_avg10={_fmt(psi_io, 2)}",
        f"fds_open={_fmt(fds)}",
        f"tcp_sockets={_fmt(tcp_total)}",
        f"rootfs_free_gb={_fmt(rootfs_free, 1)}",
    ] + [f"port_{p}={'LISTEN' if tcp_listening.get(p) else 'DOWN'}" for p in ALMITA_PORTS]

    return curr_cpu_stat, " ".join(fields), throttled


class _RotatingBlackboxHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that fsyncs the file only every `fsync_every` emits.

    logging.StreamHandler.emit() already flush()es on every write, which is
    enough to survive this process's own crash (the line leaves Python's
    buffers into the OS page cache immediately, where a plain restart or
    OOM-kill of just this process can't lose it). Only a true hard hang or
    power loss can still lose whatever the kernel hasn't written back yet;
    periodic (not per-sample) fsync bounds that window without adding a
    physical NVMe write-and-sync for a ~250-byte line every ~10s, all day,
    for the life of the service.
    """

    def __init__(self, *args, fsync_every: int = SLOW_POLL_EVERY_N_SAMPLES, **kwargs):
        super().__init__(*args, **kwargs)
        self._fsync_every = max(1, fsync_every)
        self._emit_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self._emit_count += 1
        if self.stream is not None and self._emit_count % self._fsync_every == 0:
            try:
                os.fsync(self.stream.fileno())
            except OSError:
                pass


def build_logger(log_dir: Path, max_bytes: int, backup_count: int) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("almita_system_blackbox")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    handler = _RotatingBlackboxHandler(
        str(log_dir / "blackbox.log"), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="seconds between samples")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="rotate blackbox.log past this size")
    parser.add_argument("--backup-count", type=int, default=DEFAULT_BACKUP_COUNT, help="rotated files to retain")
    args = parser.parse_args()

    logger = build_logger(Path(args.log_dir), args.max_bytes, args.backup_count)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"ALMITA SYSTEM BLACKBOX START interval={args.interval}s log_dir={args.log_dir}", flush=True)
    prev_cpu_stat: Optional[Tuple[int, int]] = None
    cached_throttled: Optional[str] = None
    sample_index = 0
    try:
        while True:
            cycle_started = time.monotonic()
            try:
                prev_cpu_stat, line, cached_throttled = build_sample(prev_cpu_stat, sample_index, cached_throttled)
                logger.info(line)
            except Exception as exc:  # a resident heartbeat must never die on one bad sample
                print(f"ALMITA SYSTEM BLACKBOX sample error: {exc!r}", file=sys.stderr, flush=True)
            sample_index += 1
            remaining = args.interval - (time.monotonic() - cycle_started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        pass
    finally:
        for handler in logger.handlers:
            handler.close()
        print("ALMITA SYSTEM BLACKBOX STOP", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
