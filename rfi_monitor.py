#!/usr/bin/env python3
"""RFI_REF: optional, disposable RTL-SDR sidecar for auxiliary RFI monitoring.

Architecture rule (non-negotiable): MAIN is the sole authoritative science
receiver. RFI_REF exists only to help characterize RF interference in
parallel with MAIN; it is never allowed to block, pause, slow down, or
otherwise influence MAIN acquisition, gain, rtl_tcp.service, mount/GOTO
behavior, or the science HDF5. A failure/crash/backlog in RFI_REF degrades
auxiliary monitoring only - it must never abort a science session.

This launches its own disposable `rtl_tcp` subprocess (never rtl_tcp.service,
never MAIN's process), verifies by PID - not just "is the port open" - that
the socket it consumes actually belongs to the subprocess it launched (the
same ownership hardening validated in dual_sdr_benchmark.py, reused here
rather than reimplemented), and performs a lightweight FFT quicklook at
~5% duty (one full block out of every `quicklook_every`) with no full IQ
persisted. FFT work runs in a dedicated single-worker thread so it can never
block the asyncio event loop that MAIN's own SDR consumer shares; if RFI_REF
falls behind, it drops its own quicklook work (dropped_blocks) rather than
ever creating backpressure anyone else could observe.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import subprocess
import time
from typing import Optional

import numpy as np

from dual_sdr_benchmark import listening_pid
from runtime_state import atomic_write_json, utcnow

RTL_TCP_BIN = "rtl_tcp"
BYTES_PER_SAMPLE = 2  # 8-bit I + 8-bit Q, rtl_tcp default format
FULL_SCALE_AMPLITUDE = 127.5  # unsigned-8 IQ centered at 127/128

STATES = ("DISABLED", "STARTING", "RUNNING", "DEGRADED", "UNAVAILABLE", "FAILED", "STOPPED")

# Test-only escape hatch: lets a hardware validation harness (SATANIC test)
# simulate a backed-up FFT consumer without adding a production CLI flag.
# Unset/0 in all normal operation.
_TEST_FFT_DELAY_ENV = "ALMITA_RFI_TEST_FFT_DELAY_S"


def _quicklook_fft(chunk: bytes, test_delay_s: float = 0.0):
    """Runs in a worker thread; never touches the asyncio loop. Returns
    (peak_dbfs, clip_fraction, occupancy_fraction)."""
    if test_delay_s > 0:
        time.sleep(test_delay_s)  # SATANIC-test-only: simulate a slow consumer
    u8 = np.frombuffer(chunk, dtype=np.uint8)
    clip_fraction = float(np.mean((u8 == 0) | (u8 == 255)))
    iq = u8.astype(np.float32) - 127.5
    i = iq[0::2]
    q = iq[1::2]
    n = min(len(i), len(q))
    if n < 16:
        return -120.0, clip_fraction, 0.0
    complex_samples = i[:n] + 1j * q[:n]
    window = np.hanning(n)
    spectrum = np.fft.fftshift(np.fft.fft(complex_samples * window))
    # Normalize by N * full-scale amplitude so ~0 dBFS means a full-scale tone.
    norm_mag = np.abs(spectrum) / (n * FULL_SCALE_AMPLITUDE)
    power_dbfs = 20 * np.log10(norm_mag + 1e-12)
    peak_dbfs = float(np.max(power_dbfs))
    noise_floor = float(np.median(power_dbfs))
    occupancy = float(np.mean(power_dbfs > (noise_floor + 10.0)))
    return peak_dbfs, clip_fraction, occupancy


class RFIReferenceMonitor:
    """Optional, disposable RFI_REF sidecar. Capture controls lifecycle only
    (start/stop); this class never touches MAIN's rtl_tcp.service, MAIN's
    SDRCapture, mount/GOTO, or the science HDF5 path."""

    def __init__(self, *, enabled: bool, runtime_dir, session_id: Optional[str] = None,
                 device_serial: str = "00000002", host: str = "127.0.0.1", port: int = 1235,
                 center_frequency_hz: int = 1420405000, sample_rate: int = 2_400_000,
                 gain_db: float = 25.0, quicklook_every: int = 20, block_bytes: int = 131072,
                 bind_timeout: float = 5.0, connect_timeout: float = 5.0,
                 log=None):
        self.enabled = bool(enabled)
        self.runtime_dir = runtime_dir
        self.session_id = session_id
        self.device_serial = device_serial
        self.host = host
        self.port = port
        self.center_frequency_hz = int(center_frequency_hz)
        self.sample_rate = int(sample_rate)
        self.gain_db = float(gain_db)
        self.quicklook_every = max(1, int(quicklook_every))
        self.block_bytes = int(block_bytes)
        self.bind_timeout = float(bind_timeout)
        self.connect_timeout = float(connect_timeout)
        self._log = log or (lambda message: None)
        self._test_fft_delay_s = float(os.environ.get(_TEST_FFT_DELAY_ENV, "0") or 0.0)

        self.status = "DISABLED"
        self.last_error: Optional[str] = None
        self.processed_blocks = 0
        self.skipped_blocks = 0
        self.dropped_blocks = 0
        self.fft_duty_fraction = 1.0 / self.quicklook_every
        self.clipping_fraction: Optional[float] = None
        self.occupancy_fraction: Optional[float] = None
        self.peak_dbfs: Optional[float] = None

        self.proc: Optional[subprocess.Popen] = None
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="rfi-fft")
        self._fft_future: Optional[concurrent.futures.Future] = None

    # -- public lifecycle -------------------------------------------------

    async def start(self) -> None:
        """Best-effort, bounded-time launch check. Never raises: any failure
        is captured as UNAVAILABLE/FAILED status so the caller (Capture) can
        log it and continue MAIN unconditionally."""
        if not self.enabled:
            self.status = "DISABLED"
            self._write_status()
            return

        self.status = "STARTING"
        self.last_error = None
        self._write_status()

        pre_existing = listening_pid(self.host, self.port)
        if pre_existing is not None:
            self.status = "UNAVAILABLE"
            self.last_error = (f"port {self.host}:{self.port} already held by unrelated "
                                f"pid={pre_existing}; refusing to connect to or kill it")
            self._log(f"RFI_REF: {self.last_error}")
            self._write_status()
            return

        cmd = [
            RTL_TCP_BIN, "-d", self.device_serial, "-a", self.host, "-p", str(self.port),
            "-f", str(self.center_frequency_hz), "-s", str(self.sample_rate),
            "-g", str(self.gain_db),  # explicit manual gain => rtl_tcp's AGC/auto-gain is not used
        ]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError as exc:
            self.status = "UNAVAILABLE"
            self.last_error = f"failed to launch rtl_tcp for RFI_REF: {exc}"
            self._log(f"RFI_REF: {self.last_error}")
            self._write_status()
            return

        ok, reason = await self._wait_for_ownership()
        if not ok:
            self.status = "UNAVAILABLE" if "port bound by unexpected pid" in (reason or "") else "FAILED"
            self.last_error = reason
            self._log(f"RFI_REF: failed to start: {reason}")
            await self._cleanup_process_async()
            self._write_status()
            return

        self._task = asyncio.create_task(self._run_consumer())

    async def stop(self) -> None:
        """Ownership-safe, bounded teardown. Only ever terminates the
        subprocess this instance itself launched (self.proc); never signals
        any other PID, never touches rtl_tcp.service."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self._cleanup_process_async()
        if self.status in ("RUNNING", "STARTING", "DEGRADED"):
            self.status = "STOPPED"
        self._write_status()
        self._executor.shutdown(wait=False)

    # -- internal -----------------------------------------------------------

    async def _wait_for_ownership(self):
        deadline = time.monotonic() + self.bind_timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                tail = ""
                if self.proc.stdout:
                    with contextlib.suppress(Exception):
                        tail = self.proc.stdout.read()
                return False, f"rtl_tcp exited before binding (rc={self.proc.returncode}): {tail[-500:]}"
            owner = listening_pid(self.host, self.port)
            if owner == self.proc.pid:
                return True, None
            if owner is not None:
                return False, f"port bound by unexpected pid={owner}, not RFI_REF's own pid={self.proc.pid}"
            await asyncio.sleep(0.2)
        return False, f"did not confirm socket ownership within {self.bind_timeout}s"

    async def _run_consumer(self) -> None:
        reader = writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.connect_timeout
            )
            header = await asyncio.wait_for(reader.readexactly(12), timeout=self.connect_timeout)
            if header[0:4] != b"RTL0":
                raise ValueError(f"unexpected rtl_tcp header magic: {header[0:4]!r}")
        except Exception as exc:  # noqa: BLE001 - isolate RFI_REF, never propagate to MAIN
            self.status = "FAILED"
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._log(f"RFI_REF: connect failed: {self.last_error}")
            self._write_status()
            return

        self.status = "RUNNING"
        self.last_error = None
        self._write_status()

        loop = asyncio.get_running_loop()
        block_idx = 0
        consecutive_timeouts = 0
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = await asyncio.wait_for(reader.readexactly(self.block_bytes), timeout=5.0)
                    consecutive_timeouts = 0
                except asyncio.IncompleteReadError:
                    self.status = "FAILED"
                    self.last_error = "RFI_REF connection closed unexpectedly (short read)"
                    break
                except asyncio.TimeoutError:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= 3:
                        self.status = "DEGRADED"
                        self.last_error = "no data from RFI_REF for >=15s"
                        self._write_status()
                    continue
                except (ConnectionResetError, OSError) as exc:
                    self.status = "FAILED"
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    break

                block_idx += 1
                if block_idx % self.quicklook_every != 0:
                    self.skipped_blocks += 1
                    continue

                if self._fft_future is not None and not self._fft_future.done():
                    # Previous quicklook block still processing: drop this one
                    # rather than queueing - RFI_REF must never build backlog.
                    self.dropped_blocks += 1
                    continue

                if self._fft_future is not None and self._fft_future.done():
                    with contextlib.suppress(Exception):
                        peak, clip, occ = self._fft_future.result()
                        self.peak_dbfs, self.clipping_fraction, self.occupancy_fraction = peak, clip, occ
                        self.processed_blocks += 1

                self._fft_future = loop.run_in_executor(
                    self._executor, _quicklook_fft, chunk, self._test_fft_delay_s
                )
                self._write_status()
        except asyncio.CancelledError:
            if self.status == "RUNNING":
                self.status = "STOPPED"
            raise
        except Exception as exc:  # noqa: BLE001 - never let RFI_REF's loop escape uncaught
            self.status = "FAILED"
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._log(f"RFI_REF: consumer loop error: {self.last_error}")
        finally:
            if self._fft_future is not None and self._fft_future.done():
                with contextlib.suppress(Exception):
                    peak, clip, occ = self._fft_future.result()
                    self.peak_dbfs, self.clipping_fraction, self.occupancy_fraction = peak, clip, occ
                    self.processed_blocks += 1
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
            self._write_status()

    async def _cleanup_process_async(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._cleanup_process_sync)

    def _cleanup_process_sync(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        with contextlib.suppress(Exception):
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)

    def _write_status(self) -> None:
        if self.runtime_dir is None:
            return
        try:
            atomic_write_json(f"{self.runtime_dir}/rfi_ref_status.json", {
                "schema_version": 1,
                "session_id": self.session_id,
                "enabled": self.enabled,
                "status": self.status,
                "device_serial": self.device_serial,
                "center_frequency_hz": self.center_frequency_hz,
                "sample_rate": self.sample_rate,
                "gain_db": self.gain_db,
                "fft_duty_fraction": round(self.fft_duty_fraction, 4),
                "clipping_fraction": self.clipping_fraction,
                "occupancy_fraction": self.occupancy_fraction,
                "peak_dbfs": self.peak_dbfs,
                "processed_blocks": self.processed_blocks,
                "skipped_blocks": self.skipped_blocks,
                "dropped_blocks": self.dropped_blocks,
                "last_update_utc": utcnow(),
                "last_error": self.last_error,
            })
        except Exception:
            pass
