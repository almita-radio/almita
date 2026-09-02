#!/usr/bin/env python3
"""Dual RTL-SDR benchmark: validates that RFI_REF (secondary, RTL-SDR V3)
can run in parallel with Capture MAIN (RTL-SDR V4) without degrading it.

Never touches rtl_tcp.service (MAIN). The secondary is launched as a plain
subprocess and is fully disposable: any failure/stall on it is caught and
logged, and never blocks or delays the MAIN consumer loop.

Two phases are run back to back with identical duration:
  1. baseline  - consume MAIN only
  2. dual      - consume MAIN + RFI_REF concurrently, with a lightweight
                 RFI quicklook (FFT on ~5% of blocks, no IQ persisted)

Outputs (under --output-dir, default data/dual_sdr_bench/<run_id>/):
  baseline_main.csv, dual_main.csv, dual_rfi_ref.csv   - per-block metrics
  resources_baseline.csv, resources_dual.csv           - CPU/RAM/load/IO
  summary.json                                         - machine-readable
  report.md                                            - PASS/FAIL report
  run.log                                              - full log
"""

import argparse
import asyncio
import concurrent.futures
import contextlib
import csv
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

RTL_TCP_BIN = "rtl_tcp"
MAIN_SERVICE = "rtl_tcp.service"
BYTES_PER_SAMPLE = 2  # 8-bit I + 8-bit Q, rtl_tcp default format

log = logging.getLogger("dual_sdr_benchmark")


# --------------------------------------------------------------------------
# /proc-based resource sampling (no external dependency such as psutil)
# --------------------------------------------------------------------------

class ProcSampler:
    def __init__(self, pids: dict):
        self.pids = dict(pids)
        self.clk_tck = os.sysconf("SC_CLK_TCK")
        self._prev_wall = None
        self._prev_proc_jiffies = {}
        self._prev_sys_total = None
        self._prev_sys_idle = None
        self._prev_disk = None

    @staticmethod
    def _read_stat_jiffies(pid):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            # comm field may contain spaces/parens; split after last ')'
            rest = raw[raw.rfind(")") + 2:].split()
            utime, stime = int(rest[11]), int(rest[12])
            return utime + stime
        except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
            return None

    @staticmethod
    def _read_rss_kib(pid):
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except (FileNotFoundError, ProcessLookupError):
            return None
        return None

    @staticmethod
    def _read_io_bytes(pid):
        try:
            d = {}
            for line in Path(f"/proc/{pid}/io").read_text().splitlines():
                k, v = line.split(":")
                d[k.strip()] = int(v.strip())
            return d.get("read_bytes", 0), d.get("write_bytes", 0)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return None, None

    @staticmethod
    def _read_sys_cpu():
        line = Path("/proc/stat").read_text().splitlines()[0]
        parts = [int(x) for x in line.split()[1:]]
        idle = parts[3] + parts[4]
        return sum(parts), idle

    @staticmethod
    def _read_loadavg():
        return [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]

    @staticmethod
    def _read_mem_used_pct():
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":")
            meminfo[k.strip()] = int(v.strip().split()[0])
        total, avail = meminfo["MemTotal"], meminfo["MemAvailable"]
        return 100.0 * (1 - avail / total) if total else None

    @staticmethod
    def _read_disk_sectors():
        # aggregate across block devices from /proc/diskstats (fields 3 & 7, 1-indexed)
        read_sectors = write_sectors = 0
        for line in Path("/proc/diskstats").read_text().splitlines():
            f = line.split()
            if len(f) < 14:
                continue
            dev = f[2]
            if dev[-1:].isdigit() and (dev.startswith("loop") or dev.startswith("ram")):
                continue
            read_sectors += int(f[5])
            write_sectors += int(f[9])
        return read_sectors, write_sectors

    def sample(self, t_rel):
        now = time.monotonic()
        total, idle = self._read_sys_cpu()
        sys_cpu_pct = None
        if self._prev_sys_total is not None:
            dt_total = total - self._prev_sys_total
            dt_idle = idle - self._prev_sys_idle
            if dt_total > 0:
                sys_cpu_pct = 100.0 * (1 - dt_idle / dt_total)
        self._prev_sys_total, self._prev_sys_idle = total, idle

        load1, load5, load15 = self._read_loadavg()
        mem_used_pct = self._read_mem_used_pct()
        rsec, wsec = self._read_disk_sectors()
        disk_read_kibs = disk_write_kibs = None
        if self._prev_disk is not None and self._prev_wall is not None:
            dt_wall = now - self._prev_wall
            if dt_wall > 0:
                disk_read_kibs = (rsec - self._prev_disk[0]) * 0.5 / dt_wall
                disk_write_kibs = (wsec - self._prev_disk[1]) * 0.5 / dt_wall
        self._prev_disk = (rsec, wsec)

        row = {
            "t": round(t_rel, 3),
            "sys_cpu_pct": sys_cpu_pct,
            "load1": load1, "load5": load5, "load15": load15,
            "mem_used_pct": mem_used_pct,
            "disk_read_kibs": disk_read_kibs, "disk_write_kibs": disk_write_kibs,
        }

        dt_wall = None if self._prev_wall is None else now - self._prev_wall
        for name, pid in self.pids.items():
            cpu_pct = rss_kib = io_r = io_w = None
            if pid:
                jiffies = self._read_stat_jiffies(pid)
                rss_kib = self._read_rss_kib(pid)
                io_r, io_w = self._read_io_bytes(pid)
                if jiffies is not None and dt_wall and name in self._prev_proc_jiffies:
                    dj = jiffies - self._prev_proc_jiffies[name]
                    cpu_pct = 100.0 * (dj / self.clk_tck) / dt_wall
                if jiffies is not None:
                    self._prev_proc_jiffies[name] = jiffies
            row[f"{name}_cpu_pct"] = cpu_pct
            row[f"{name}_rss_kib"] = rss_kib
            row[f"{name}_io_read_bytes"] = io_r
            row[f"{name}_io_write_bytes"] = io_w

        self._prev_wall = now
        return row


async def resource_sampler_task(sampler: ProcSampler, csv_path: Path, stop_event: asyncio.Event, interval=1.0):
    t0 = time.monotonic()
    rows = []
    fieldnames = None
    while not stop_event.is_set():
        row = sampler.sample(time.monotonic() - t0)
        rows.append(row)
        if fieldnames is None:
            fieldnames = list(row.keys())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames or ["t"])
        w.writeheader()
        w.writerows(rows)
    return rows


# --------------------------------------------------------------------------
# rtl_tcp stream consumer
# --------------------------------------------------------------------------

class StreamResult:
    def __init__(self, name):
        self.name = name
        self.connected = False
        self.connect_error = None
        self.tuner_type = None
        self.gain_count = None
        self.bytes_total = 0
        self.blocks_total = 0
        self.short_reads = 0
        self.socket_errors = 0
        self.read_timeouts = 0
        self.discontinuities = []  # list of {t, gap_s}
        self.max_gap_s = 0.0
        self.inter_arrival = []  # seconds, sampled (thinned) for stats
        self.peak_db = []
        self.clip_fraction = []
        self.occupancy = []
        self.start_wall = None
        self.end_wall = None
        self.error = None

    def summary(self, expected_bytes_per_s):
        duration = (self.end_wall - self.start_wall) if (self.start_wall and self.end_wall) else 0.0
        achieved_bps = self.bytes_total / duration if duration > 0 else 0.0
        arr = np.array(self.inter_arrival) if self.inter_arrival else np.array([0.0])
        return {
            "name": self.name,
            "connected": self.connected,
            "connect_error": self.connect_error,
            "tuner_type": self.tuner_type,
            "duration_s": round(duration, 3),
            "bytes_total": self.bytes_total,
            "blocks_total": self.blocks_total,
            "expected_bytes_per_s": expected_bytes_per_s,
            "achieved_bytes_per_s": round(achieved_bps, 1),
            "throughput_ratio": round(achieved_bps / expected_bytes_per_s, 4) if expected_bytes_per_s else None,
            "short_reads": self.short_reads,
            "socket_errors": self.socket_errors,
            "read_timeouts": self.read_timeouts,
            "discontinuity_count": len(self.discontinuities),
            "max_gap_s": round(self.max_gap_s, 4),
            "inter_arrival_mean_ms": round(float(np.mean(arr)) * 1000, 3),
            "inter_arrival_p99_ms": round(float(np.percentile(arr, 99)) * 1000, 3),
            "quicklook_blocks": len(self.peak_db),
            "quicklook_peak_db_mean": round(float(np.mean(self.peak_db)), 2) if self.peak_db else None,
            "quicklook_peak_db_max": round(float(np.max(self.peak_db)), 2) if self.peak_db else None,
            "quicklook_clip_fraction_mean": round(float(np.mean(self.clip_fraction)), 6) if self.clip_fraction else None,
            "quicklook_occupancy_mean": round(float(np.mean(self.occupancy)), 4) if self.occupancy else None,
            "error": self.error,
        }


def _parse_rtl_tcp_header(header: bytes):
    magic = header[0:4]
    tuner_type = int.from_bytes(header[4:8], "big")
    gain_count = int.from_bytes(header[8:12], "big")
    return magic, tuner_type, gain_count


async def consume_stream(name, host, port, duration, samplerate, block_bytes,
                          csv_path: Path, result: StreamResult,
                          quicklook=False, quicklook_every=20,
                          executor: concurrent.futures.Executor = None,
                          connect_timeout=5.0):
    """Read one rtl_tcp stream for `duration` seconds. Never raises: all
    errors are captured on `result` so a failure here cannot propagate to
    (and cancel) a sibling task consuming another stream."""
    expected_block_interval = block_bytes / (samplerate * BYTES_PER_SAMPLE)
    gap_threshold = max(expected_block_interval * 4, 0.25)

    rows = []
    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=connect_timeout
        )
        header = await asyncio.wait_for(reader.readexactly(12), timeout=connect_timeout)
        magic, tuner_type, gain_count = _parse_rtl_tcp_header(header)
        if magic != b"RTL0":
            raise ValueError(f"unexpected rtl_tcp header magic: {magic!r}")
        result.connected = True
        result.tuner_type = tuner_type
        result.gain_count = gain_count
        log.info("[%s] connected %s:%d tuner_type=%d gain_count=%d",
                  name, host, port, tuner_type, gain_count)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, isolate stream
        result.connect_error = f"{type(exc).__name__}: {exc}"
        result.error = result.connect_error
        log.error("[%s] connect failed: %s", name, result.connect_error)
        return result

    loop = asyncio.get_running_loop()
    result.start_wall = time.monotonic()
    deadline = result.start_wall + duration
    last_ts = result.start_wall
    block_idx = 0
    try:
        while True:
            now_check = time.monotonic()
            if now_check >= deadline:
                break
            try:
                chunk = await asyncio.wait_for(reader.readexactly(block_bytes), timeout=5.0)
            except asyncio.IncompleteReadError as exc:
                result.short_reads += 1
                result.bytes_total += len(exc.partial)
                log.warning("[%s] short read / connection closed after %d bytes",
                            name, len(exc.partial))
                break
            except asyncio.TimeoutError:
                result.read_timeouts += 1
                log.warning("[%s] read timeout (no data for 5s)", name)
                continue
            except (ConnectionResetError, OSError) as exc:
                result.socket_errors += 1
                result.error = f"{type(exc).__name__}: {exc}"
                log.error("[%s] socket error: %s", name, result.error)
                break

            now = time.monotonic()
            dt = now - last_ts
            result.inter_arrival.append(dt)
            gap_flag = 0
            if dt > gap_threshold:
                result.discontinuities.append({"t": round(now - result.start_wall, 3), "gap_s": round(dt, 4)})
                result.max_gap_s = max(result.max_gap_s, dt)
                gap_flag = 1
            last_ts = now
            result.bytes_total += len(chunk)
            result.blocks_total += 1
            block_idx += 1

            peak_db = clip_frac = occ = None
            if quicklook and (block_idx % quicklook_every == 0):
                peak_db, clip_frac, occ = await loop.run_in_executor(
                    executor, _quicklook_fft, chunk
                )
                result.peak_db.append(peak_db)
                result.clip_fraction.append(clip_frac)
                result.occupancy.append(occ)

            if block_idx <= 20000:  # cap in-memory row growth for pathological runs
                rows.append({
                    "t": round(now - result.start_wall, 4),
                    "bytes": len(chunk),
                    "dt_s": round(dt, 5),
                    "gap_flag": gap_flag,
                    "peak_db": peak_db,
                    "clip_fraction": clip_frac,
                    "occupancy": occ,
                })
    except Exception as exc:  # noqa: BLE001 - never let one stream kill the run
        result.error = f"{type(exc).__name__}: {exc}"
        log.exception("[%s] unexpected error in consumer loop", name)
    finally:
        result.end_wall = time.monotonic()
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)

    with csv_path.open("w", newline="") as fh:
        fieldnames = ["t", "bytes", "dt_s", "gap_flag", "peak_db", "clip_fraction", "occupancy"]
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    log.info("[%s] done: blocks=%d bytes=%d short_reads=%d socket_errors=%d discontinuities=%d",
              name, result.blocks_total, result.bytes_total, result.short_reads,
              result.socket_errors, len(result.discontinuities))
    return result


def _quicklook_fft(chunk: bytes):
    """Runs in a worker thread so FFT work never blocks the asyncio loop
    that is draining the MAIN socket."""
    u8 = np.frombuffer(chunk, dtype=np.uint8)
    clip_fraction = float(np.mean((u8 == 0) | (u8 == 255)))
    iq = (u8.astype(np.float32) - 127.5)
    i = iq[0::2]
    q = iq[1::2]
    n = min(len(i), len(q))
    if n < 16:
        return -120.0, clip_fraction, 0.0
    complex_samples = i[:n] + 1j * q[:n]
    window = np.hanning(n)
    spectrum = np.fft.fftshift(np.fft.fft(complex_samples * window))
    power_db = 20 * np.log10(np.abs(spectrum) + 1e-6)
    peak_db = float(np.max(power_db))
    noise_floor = float(np.median(power_db))
    occupancy = float(np.mean(power_db > (noise_floor + 10.0)))
    return peak_db, clip_fraction, occupancy


# --------------------------------------------------------------------------
# process / systemd helpers
# --------------------------------------------------------------------------

def get_service_main_pid(service: str):
    try:
        out = subprocess.check_output(
            ["systemctl", "show", service, "-p", "MainPID", "--value"], text=True
        ).strip()
        pid = int(out)
        return pid if pid > 0 else None
    except Exception:
        return None


def tcp_port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def listening_pid(host, port):
    """PID of the process currently LISTENing on host:port, or None. Used so
    we never silently consume a stream from some other, unmanaged process
    that happens to already occupy the port."""
    try:
        out = subprocess.check_output(["ss", "-lntp"], text=True)
    except Exception:
        return None
    needle = f"{host}:{port} "
    for line in out.splitlines():
        if needle in line and "LISTEN" in line:
            m = line.rfind("pid=")
            if m == -1:
                continue
            digits = ""
            for ch in line[m + 4:]:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                return int(digits)
    return None


def kernel_usb_events_since(since_dt: datetime):
    """Returns (total_usb_lines, reset_lines) from the kernel ring since `since_dt`."""
    since_str = since_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        out = subprocess.check_output(
            ["journalctl", "-k", "--since", since_str, "--no-pager", "-o", "cat"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("journalctl unavailable for USB check: %s", exc)
        return None, None
    lines = [ln for ln in out.splitlines() if "usb" in ln.lower()]
    reset_lines = [ln for ln in lines if "reset" in ln.lower()]
    return lines, reset_lines


@contextlib.contextmanager
def launch_secondary_rtl_tcp(device_serial, host, port, freq, samplerate, gain, bind_timeout=10.0):
    """Launches a disposable rtl_tcp for RFI_REF as a plain subprocess
    (never systemd). Guarantees termination on exit, even on error.

    Refuses to proceed if the port is already held by some other, unmanaged
    process, and verifies after spawning that the socket is actually bound
    by *our* subprocess -- otherwise a stale/foreign rtl_tcp could be
    silently consumed instead of the one we think we configured (wrong
    frequency/gain, and not ours to guarantee is disposable)."""
    pre_existing_pid = listening_pid(host, port)
    if pre_existing_pid is not None:
        msg = (f"port {host}:{port} already held by pid={pre_existing_pid} "
               f"(not started by this benchmark) -- refusing to launch or consume it")
        log.error(msg)
        yield None, False, msg
        return

    cmd = [
        RTL_TCP_BIN, "-d", device_serial, "-a", host, "-p", str(port),
        "-f", str(freq), "-s", str(samplerate), "-g", str(gain),
    ]
    log.info("launching secondary rtl_tcp: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ready = False
    startup_log = []
    deadline = time.monotonic() + bind_timeout
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                remaining = proc.stdout.read() if proc.stdout else ""
                startup_log.append(remaining)
                break
            owner = listening_pid(host, port)
            if owner == proc.pid:
                ready = True
                break
            elif owner is not None:
                startup_log.append(f"port bound by unexpected pid={owner}, not ours (pid={proc.pid})")
                break
            time.sleep(0.2)
        if not ready and proc.poll() is None:
            log.error("secondary rtl_tcp (pid=%d) did not confirm ownership of %s:%d within %.1fs",
                       proc.pid, host, port, bind_timeout)
        yield proc, ready, "".join(startup_log)
    finally:
        if proc.poll() is None:
            log.info("terminating secondary rtl_tcp (pid=%d)", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                log.warning("secondary rtl_tcp did not exit on SIGTERM, killing")
                proc.kill()
                proc.wait(timeout=5.0)


# --------------------------------------------------------------------------
# phases
# --------------------------------------------------------------------------

async def run_baseline(args, out_dir: Path, executor):
    log.info("=== PHASE: baseline (MAIN only) ===")
    main_pid = get_service_main_pid(MAIN_SERVICE)
    since = datetime.now(timezone.utc)
    stop_event = asyncio.Event()
    sampler = ProcSampler({"main": main_pid, "bench": os.getpid()})
    res_task = asyncio.create_task(
        resource_sampler_task(sampler, out_dir / "resources_baseline.csv", stop_event)
    )
    main_result = StreamResult("MAIN")
    await consume_stream(
        "MAIN", args.main_host, args.main_port, args.duration, args.main_samplerate,
        args.block_bytes, out_dir / "baseline_main.csv", main_result,
        quicklook=False, executor=executor,
    )
    stop_event.set()
    resource_rows = await res_task
    usb_lines, usb_resets = kernel_usb_events_since(since)
    return {
        "main_pid": main_pid,
        "since_utc": since.isoformat(),
        "main": main_result.summary(args.main_samplerate * BYTES_PER_SAMPLE),
        "resource_rows": len(resource_rows),
        "usb_event_lines": len(usb_lines) if usb_lines is not None else None,
        "usb_reset_lines": len(usb_resets) if usb_resets is not None else None,
        "usb_reset_detail": usb_resets[:20] if usb_resets else [],
    }


async def run_dual(args, out_dir: Path, executor):
    log.info("=== PHASE: dual (MAIN + RFI_REF) ===")
    main_pid = get_service_main_pid(MAIN_SERVICE)

    with launch_secondary_rtl_tcp(
        args.secondary_serial, args.secondary_host, args.secondary_port,
        args.secondary_freq, args.secondary_samplerate, args.secondary_gain,
        bind_timeout=args.secondary_bind_timeout,
    ) as (sec_proc, ready, startup_log):
        since = datetime.now(timezone.utc)
        secondary_launch_ok = ready
        secondary_pid = sec_proc.pid if (sec_proc is not None and sec_proc.poll() is None) else None

        stop_event = asyncio.Event()
        sampler = ProcSampler({"main": main_pid, "secondary": secondary_pid, "bench": os.getpid()})
        res_task = asyncio.create_task(
            resource_sampler_task(sampler, out_dir / "resources_dual.csv", stop_event)
        )

        main_result = StreamResult("MAIN")
        rfi_result = StreamResult("RFI_REF")

        main_coro = consume_stream(
            "MAIN", args.main_host, args.main_port, args.duration, args.main_samplerate,
            args.block_bytes, out_dir / "dual_main.csv", main_result,
            quicklook=False, executor=executor,
        )

        if secondary_launch_ok:
            rfi_coro = consume_stream(
                "RFI_REF", args.secondary_host, args.secondary_port, args.duration,
                args.secondary_samplerate, args.block_bytes, out_dir / "dual_rfi_ref.csv",
                rfi_result, quicklook=True, quicklook_every=args.quicklook_every,
                executor=executor,
            )
        else:
            rfi_result.connect_error = "secondary rtl_tcp failed to bind before start"
            rfi_result.error = rfi_result.connect_error

            async def _noop():
                return rfi_result
            rfi_coro = _noop()

        # return_exceptions=True: a crash in either coroutine cannot cancel
        # or delay the other. MAIN's result is authoritative regardless of
        # what happens to RFI_REF.
        gathered = await asyncio.gather(main_coro, rfi_coro, return_exceptions=True)
        for r in gathered:
            if isinstance(r, Exception):
                log.error("stream task raised: %s", r)

        stop_event.set()
        resource_rows = await res_task
        usb_lines, usb_resets = kernel_usb_events_since(since)

        return {
            "main_pid": main_pid,
            "secondary_pid": secondary_pid,
            "secondary_launch_ok": secondary_launch_ok,
            "secondary_startup_log_tail": startup_log[-2000:],
            "since_utc": since.isoformat(),
            "main": main_result.summary(args.main_samplerate * BYTES_PER_SAMPLE),
            "rfi_ref": rfi_result.summary(args.secondary_samplerate * BYTES_PER_SAMPLE),
            "resource_rows": len(resource_rows),
            "usb_event_lines": len(usb_lines) if usb_lines is not None else None,
            "usb_reset_lines": len(usb_resets) if usb_resets is not None else None,
            "usb_reset_detail": usb_resets[:20] if usb_resets else [],
        }


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------

def evaluate(baseline, dual, args):
    checks = []

    def add(name, passed, detail):
        checks.append({"check": name, "pass": bool(passed), "detail": detail})

    b_main = baseline["main"]
    d_main = dual["main"]
    d_rfi = dual["rfi_ref"]

    add("main_connected_both_phases",
        b_main["connected"] and d_main["connected"],
        f"baseline_connected={b_main['connected']} dual_connected={d_main['connected']}")

    ratio = None
    if b_main.get("achieved_bytes_per_s") and d_main.get("achieved_bytes_per_s"):
        ratio = d_main["achieved_bytes_per_s"] / b_main["achieved_bytes_per_s"]
    add("main_throughput_preserved",
        ratio is not None and ratio >= args.throughput_ratio_min,
        f"dual/baseline throughput ratio={ratio!r} (min {args.throughput_ratio_min}); "
        f"baseline={b_main.get('achieved_bytes_per_s')} B/s dual={d_main.get('achieved_bytes_per_s')} B/s "
        f"expected={b_main.get('expected_bytes_per_s')} B/s")

    add("main_no_short_reads",
        d_main["short_reads"] == 0,
        f"dual short_reads={d_main['short_reads']} (baseline={b_main['short_reads']})")

    add("main_no_socket_errors",
        d_main["socket_errors"] == 0,
        f"dual socket_errors={d_main['socket_errors']} (baseline={b_main['socket_errors']})")

    tolerance = args.discontinuity_tolerance
    add("main_discontinuities_not_worse",
        d_main["discontinuity_count"] <= b_main["discontinuity_count"] + tolerance,
        f"baseline={b_main['discontinuity_count']} dual={d_main['discontinuity_count']} "
        f"(tolerance +{tolerance})")

    usb_resets = dual.get("usb_reset_lines")
    add("zero_usb_resets_dual",
        usb_resets == 0,
        f"usb reset-matching kernel lines during dual phase={usb_resets!r}")

    dual_cpu = None
    with contextlib.suppress(Exception):
        import statistics
        rows = list(csv.DictReader((Path(args.output_dir_resolved) / "resources_dual.csv").open()))
        vals = [float(r["sys_cpu_pct"]) for r in rows if r["sys_cpu_pct"] not in ("", "None")]
        if vals:
            dual_cpu = statistics.mean(vals)
    add("system_cpu_reasonable",
        dual_cpu is None or dual_cpu <= args.max_avg_cpu_pct,
        f"mean system CPU during dual={dual_cpu!r}% (max allowed {args.max_avg_cpu_pct}%)")

    dual_mem = None
    with contextlib.suppress(Exception):
        import statistics
        rows = list(csv.DictReader((Path(args.output_dir_resolved) / "resources_dual.csv").open()))
        vals = [float(r["mem_used_pct"]) for r in rows if r["mem_used_pct"] not in ("", "None")]
        if vals:
            dual_mem = statistics.mean(vals)
    add("ram_reasonable",
        dual_mem is None or dual_mem <= args.max_avg_mem_pct,
        f"mean RAM used during dual={dual_mem!r}% (max allowed {args.max_avg_mem_pct}%)")

    add("rfi_ref_secondary_ran",
        dual.get("secondary_launch_ok", False) and d_rfi.get("connected", False),
        f"secondary_launch_ok={dual.get('secondary_launch_ok')} rfi_connected={d_rfi.get('connected')}")

    add("rfi_ref_quicklook_ran_at_target_duty",
        d_rfi.get("quicklook_blocks", 0) > 0,
        f"quicklook_blocks={d_rfi.get('quicklook_blocks')} of blocks_total={d_rfi.get('blocks_total')} "
        f"(target 1/{args.quicklook_every})")

    add("rfi_ref_disposable_did_not_affect_main",
        checks[1]["pass"] and checks[2]["pass"] and checks[3]["pass"] and checks[4]["pass"],
        "derived from throughput/short-reads/socket-errors/discontinuity checks above")

    overall = all(c["pass"] for c in checks)
    return overall, checks


def write_report(out_dir: Path, args, baseline, dual, overall, checks):
    lines = []
    lines.append("# Dual RTL-SDR Benchmark Report")
    lines.append("")
    lines.append(f"Run ID: {out_dir.name}")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Duration per phase: {args.duration}s")
    lines.append("")
    lines.append(f"## Verdict: {'PASS' if overall else 'FAIL'}")
    lines.append("")
    lines.append("| Check | Result | Detail |")
    lines.append("|---|---|---|")
    for c in checks:
        lines.append(f"| {c['check']} | {'PASS' if c['pass'] else 'FAIL'} | {c['detail']} |")
    lines.append("")
    lines.append("## MAIN — baseline vs dual")
    lines.append("")
    lines.append("| Metric | Baseline | Dual |")
    lines.append("|---|---|---|")
    bm, dm = baseline["main"], dual["main"]
    for key in ["achieved_bytes_per_s", "throughput_ratio", "blocks_total", "short_reads",
                "socket_errors", "read_timeouts", "discontinuity_count", "max_gap_s",
                "inter_arrival_mean_ms", "inter_arrival_p99_ms"]:
        lines.append(f"| {key} | {bm.get(key)} | {dm.get(key)} |")
    lines.append("")
    lines.append("## RFI_REF (dual phase only, quicklook mode)")
    lines.append("")
    dr = dual["rfi_ref"]
    for key in ["connected", "achieved_bytes_per_s", "throughput_ratio", "blocks_total",
                "short_reads", "socket_errors", "discontinuity_count",
                "quicklook_blocks", "quicklook_peak_db_mean", "quicklook_peak_db_max",
                "quicklook_clip_fraction_mean", "quicklook_occupancy_mean"]:
        lines.append(f"- {key}: {dr.get(key)}")
    lines.append("")
    lines.append("## USB / kernel")
    lines.append("")
    lines.append(f"- baseline usb-matching kernel lines: {baseline.get('usb_event_lines')}, "
                  f"reset-matching: {baseline.get('usb_reset_lines')}")
    lines.append(f"- dual usb-matching kernel lines: {dual.get('usb_event_lines')}, "
                  f"reset-matching: {dual.get('usb_reset_lines')}")
    if dual.get("usb_reset_detail"):
        lines.append("")
        lines.append("Reset-matching lines (dual phase):")
        for ln in dual["usb_reset_detail"]:
            lines.append(f"    {ln}")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    lines.append(f"- MAIN: {args.main_host}:{args.main_port}, "
                  f"samplerate={args.main_samplerate}, expected={args.main_samplerate * BYTES_PER_SAMPLE} B/s "
                  f"(rtl_tcp.service untouched, MainPID={dual.get('main_pid')})")
    lines.append(f"- RFI_REF: {args.secondary_host}:{args.secondary_port}, device={args.secondary_serial}, "
                  f"freq={args.secondary_freq}, samplerate={args.secondary_samplerate}, "
                  f"gain={args.secondary_gain}, launched as disposable subprocess "
                  f"(pid={dual.get('secondary_pid')}, launch_ok={dual.get('secondary_launch_ok')})")
    lines.append(f"- quicklook duty: 1/{args.quicklook_every} blocks (~{100.0 / args.quicklook_every:.1f}%), "
                  "FFT only, no IQ persisted")
    lines.append("")
    lines.append("## Recommendation for first field session")
    lines.append("")
    if overall:
        lines.append(
            "RFI_REF puede operar en paralelo con MAIN sin degradar Capture bajo esta carga "
            f"(duración {args.duration}s, quicklook {100.0/args.quicklook_every:.0f}% duty). "
            "Mantener RFI_REF como proceso desechable e independiente de rtl_tcp.service "
            "(subprocess propio, sin systemd), con timeouts de conexión cortos y sin política de "
            "reintento agresiva, para que cualquier falla en el secundario nunca bloquee MAIN."
        )
    else:
        failed = [c["check"] for c in checks if not c["pass"]]
        lines.append(
            "NO se recomienda operar RFI_REF en paralelo con la configuración probada hasta resolver: "
            + ", ".join(failed) + ". Revisar detalle de cada check arriba antes de la salida de campo."
        )
    lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duration", type=float, default=180.0, help="seconds per phase (default 180)")
    p.add_argument("--output-dir", default=None, help="output dir (default data/dual_sdr_bench/<run_id>)")
    p.add_argument("--block-bytes", type=int, default=131072, help="read block size in bytes")
    p.add_argument("--quicklook-every", type=int, default=20, help="FFT one block out of N on RFI_REF (~5%%)")

    p.add_argument("--main-host", default="127.0.0.1")
    p.add_argument("--main-port", type=int, default=1234)
    p.add_argument("--main-samplerate", type=int, default=2_400_000)

    p.add_argument("--secondary-host", default="127.0.0.1")
    p.add_argument("--secondary-port", type=int, default=1235)
    p.add_argument("--secondary-serial", default="00000002")
    p.add_argument("--secondary-freq", type=int, default=1_420_405_000)
    p.add_argument("--secondary-samplerate", type=int, default=2_400_000)
    p.add_argument("--secondary-gain", default="25")
    p.add_argument("--secondary-bind-timeout", type=float, default=10.0)

    p.add_argument("--throughput-ratio-min", type=float, default=0.99)
    p.add_argument("--discontinuity-tolerance", type=int, default=0)
    p.add_argument("--max-avg-cpu-pct", type=float, default=85.0)
    p.add_argument("--max-avg-mem-pct", type=float, default=90.0)

    p.add_argument("--phase", choices=["both", "baseline", "dual"], default="both")
    return p


async def async_main(args):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.output_dir) if args.output_dir else Path("data/dual_sdr_bench") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir_resolved = str(out_dir)

    file_handler = logging.FileHandler(out_dir / "run.log")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(file_handler)

    log.info("run_id=%s output_dir=%s", run_id, out_dir)
    log.info("args: %s", vars(args))

    if get_service_main_pid(MAIN_SERVICE) is None:
        log.error("rtl_tcp.service is not active (MainPID unavailable) — aborting. "
                   "This benchmark refuses to start/restart it.")
        return 2
    if not tcp_port_open(args.main_host, args.main_port, timeout=2.0):
        log.error("MAIN rtl_tcp not reachable at %s:%d — aborting.", args.main_host, args.main_port)
        return 2

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="quicklook-fft")

    baseline = dual = None
    try:
        if args.phase in ("both", "baseline"):
            baseline = await run_baseline(args, out_dir, executor)
            (out_dir / "baseline_summary.json").write_text(json.dumps(baseline, indent=2, sort_keys=True, default=str))
        if args.phase in ("both", "dual"):
            dual = await run_dual(args, out_dir, executor)
            (out_dir / "dual_summary.json").write_text(json.dumps(dual, indent=2, sort_keys=True, default=str))
    finally:
        executor.shutdown(wait=False)

    if baseline and dual:
        overall, checks = evaluate(baseline, dual, args)
        summary = {
            "run_id": run_id, "duration_s": args.duration, "overall_pass": overall,
            "checks": checks, "baseline": baseline, "dual": dual,
            "args": {k: v for k, v in vars(args).items() if k != "output_dir_resolved"},
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
        write_report(out_dir, args, baseline, dual, overall, checks)
        log.info("VERDICT: %s", "PASS" if overall else "FAIL")
        print(f"\n{'PASS' if overall else 'FAIL'} — report: {out_dir / 'report.md'}")
        return 0 if overall else 1

    log.info("single-phase run complete (no verdict computed)")
    return 0


def main():
    args = build_arg_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                         stream=sys.stdout)
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(Exception):
            signal.signal(sig, signal.default_int_handler)
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
