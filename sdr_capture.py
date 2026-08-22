#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SDR Capture Module - RTL-SDR data capture with USB and network mode support
Optimized for NVMe disk I/O with detailed performance metrics
HDF5 output format with complete metadata for radio astronomy
"""

import asyncio
import json
import os
import time
import socket
import struct
import threading
import numpy as np
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

try:
    from rtlsdr import RtlSdr
    HAS_RTLSDR = True
except ImportError:
    HAS_RTLSDR = False

try:
    import h5py
    HAS_HDF5 = True
except ImportError:
    HAS_HDF5 = False
    print("WARNING: h5py not installed. Install with: pip install h5py")


@dataclass
class CaptureMetrics:
    """Metrics for SDR capture performance analysis"""
    usb_open_time: float = 0.0
    usb_config_time: float = 0.0
    network_connect_time: float = 0.0
    capture_time: float = 0.0
    disk_write_time: float = 0.0
    total_samples: int = 0
    sample_rate: int = 0
    throughput_mbps: float = 0.0
    bytes_received_total: int = 0
    bytes_discarded: int = 0
    bytes_kept: int = 0
    instantaneous_throughput: float = 0.0
    effective_capture_throughput: float = 0.0
    seconds_since_last_byte: float = 0.0
    consumer_state: str = "DISCONNECTED"
    generation: int = 0
    expected_bytes: int = 0
    received_capture_bytes: int = 0
    buffer_capacity: int = 0
    buffer_used: int = 0
    socket_reconnect_count: int = 0
    last_error: Optional[str] = None
    
    def __str__(self) -> str:
        lines = [
            "📊 SDR Capture Metrics:",
            f"  • USB Open:      {self.usb_open_time*1000:.2f}ms" if self.usb_open_time > 0 else None,
            f"  • USB Config:    {self.usb_config_time*1000:.2f}ms" if self.usb_config_time > 0 else None,
            f"  • Net Connect:   {self.network_connect_time*1000:.2f}ms" if self.network_connect_time > 0 else None,
            f"  • SDR Capture:   {self.capture_time*1000:.2f}ms",
            f"  • Disk Write:    {self.disk_write_time*1000:.2f}ms",
            f"  • Total Samples: {self.total_samples:,}",
            f"  • Sample Rate:   {self.sample_rate/1e6:.2f} MS/s",
            f"  • Throughput:    {self.throughput_mbps:.2f} MB/s",
        ]
        return "\n".join(line for line in lines if line is not None)


class SDRNetworkError(ConnectionError):
    """Typed rtl_tcp stream failure with a complete diagnostic snapshot."""

    code = "SDR_NETWORK_ERROR"

    def __init__(self, message: str, **details):
        self.details = details
        detail_text = " ".join(f"{key}={value}" for key, value in details.items())
        super().__init__(f"{self.code}: {message}" + (f" | {detail_text}" if detail_text else ""))


class SDRDisconnected(SDRNetworkError):
    code = "SDR_DISCONNECTED"


class SDRStallNoBytes(SDRNetworkError):
    code = "SDR_STALL_NO_BYTES"


class SDRThroughputTooLow(SDRNetworkError):
    code = "SDR_THROUGHPUT_TOO_LOW"


class SDRWallTimeout(SDRNetworkError):
    code = "SDR_WALL_TIMEOUT"


def _hdf5_attribute_value(value: Any) -> Any:
    """Convert capture metadata to values supported by HDF5 attributes."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    return value


def validate_hdf5_capture(path: Path, expected_samples: Optional[int] = None,
                          expected_identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate capture structure and, when supplied, point/session identity."""
    essential = {
        "center_frequency_hz", "sample_rate_hz", "num_samples",
        "capture_status", "final_filename", "created_at",
    }
    with h5py.File(path, "r") as capture_file:
        if "iq_data" not in capture_file:
            raise ValueError("HDF5 validation failed: iq_data dataset is missing")
        stored_samples = int(capture_file.attrs.get("num_samples", -1))
        coherent_samples = expected_samples if expected_samples is not None else stored_samples
        if coherent_samples < 0 or capture_file["iq_data"].shape != (coherent_samples * 2,):
            raise ValueError("HDF5 validation failed: incoherent IQ sample count")
        if expected_samples is not None and stored_samples != expected_samples:
            raise ValueError("HDF5 validation failed: num_samples is incoherent")
        missing = essential.difference(capture_file.attrs.keys())
        if missing:
            raise ValueError(f"HDF5 validation failed: missing metadata {sorted(missing)}")
        attributes = {key: capture_file.attrs[key] for key in capture_file.attrs.keys()}
        for key, expected in (expected_identity or {}).items():
            if expected is None:
                continue
            if key not in attributes or str(attributes[key]) != str(expected):
                raise ValueError(f"HDF5 validation failed: identity mismatch for {key}")
        return attributes


def _validate_hdf5_capture(path: Path, expected_samples: int) -> None:
    """Compatibility wrapper used by the atomic writer and fault-injection tests."""
    validate_hdf5_capture(path, expected_samples=expected_samples)


def write_hdf5_atomic(output_file: str, iq_samples: np.ndarray,
                      capture_metadata: Dict[str, Any], expected_samples: int) -> Path:
    """Write, sync, validate and atomically promote one HDF5 capture."""
    final_path = Path(output_file)
    part_path = final_path.with_name(f"{final_path.name}.part")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with h5py.File(part_path, "w") as capture_file:
            dset = capture_file.create_dataset(
                "iq_data", data=iq_samples, compression="gzip",
                compression_opts=4, chunks=True,
            )
            dset.attrs["description"] = "Interleaved I/Q samples (uint8)"
            dset.attrs["format"] = "I[0], Q[0], I[1], Q[1], ..."
            capture_file.create_dataset(
                "i_samples", data=iq_samples[0::2], compression="gzip", compression_opts=4
            ).attrs["description"] = "In-phase component (I)"
            capture_file.create_dataset(
                "q_samples", data=iq_samples[1::2], compression="gzip", compression_opts=4
            ).attrs["description"] = "Quadrature component (Q)"
            for key, value in capture_metadata.items():
                if value is not None:
                    capture_file.attrs[key] = _hdf5_attribute_value(value)
            capture_file.attrs["hdf5_completed_at"] = datetime.now(timezone.utc).isoformat()
            capture_file.attrs["software"] = "INDIpy SDR Capture"
            capture_file.attrs["file_format_version"] = "1.1"
            capture_file.attrs["created_by"] = "sdr_capture.py"
            capture_file.flush()
            try:
                handle = capture_file.id.get_vfd_handle()
                if isinstance(handle, int):
                    os.fsync(handle)
            except (AttributeError, OSError, TypeError):
                pass

        _validate_hdf5_capture(part_path, expected_samples)
        fd = os.open(part_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(part_path, final_path)
        try:
            directory_fd = os.open(final_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        return final_path
    except Exception:
        try:
            part_path.unlink()
        except FileNotFoundError:
            pass
        raise


class SDRCapture:
    """
    RTL-SDR capture with USB and network modes
    Optimized for low-latency and high-throughput I/O
    """
    
    def __init__(self, mode: str = "usb", host: str = "localhost", port: int = 1234,
                 device_index: int = 0, verbose: bool = False):
        """
        Initialize SDR capture

        Args:
            mode: "usb" for local USB or "network" for rtl_tcp
            host: rtl_tcp server host (only for network mode)
            port: rtl_tcp server port (only for network mode)
            device_index: RTL-SDR device index (only for USB mode)
            verbose: Enable detailed logging
        """
        self.mode = mode.lower()
        self.host = host
        self.port = port
        self.device_index = device_index
        self.verbose = verbose

        self.sdr = None  # USB mode: RtlSdr object
        self.socket = None  # Network mode: socket connection
        self.metrics = CaptureMetrics()
        self.timing_callback = None
        self.flush_progress_callback = None
        self.compact_console = False
        self.consumer_state = "DISCONNECTED"
        self.bytes_received_total = 0
        self.bytes_discarded = 0
        self.bytes_kept = 0
        self.instantaneous_throughput = 0.0
        self.last_byte_timestamp = 0.0
        self.socket_reconnect_count = 0
        self.last_error = None
        self.generation = 0
        self.stall_timeout = 5.0
        self.throughput_grace = 5.0
        self.throughput_ratio = 0.50
        self.max_capture_wall_override = None
        self._consumer_thread = None
        self._consumer_stop = threading.Event()
        self._consumer_condition = threading.Condition()
        self._active_capture = None
        self._flush_marker = 0

        if self.mode not in ["usb", "network"]:
            raise ValueError(f"Invalid mode: {mode}. Must be 'usb' or 'network'")
        
        if self.mode == "usb" and not HAS_RTLSDR:
            raise ImportError("pyrtlsdr not installed. Run: pip install pyrtlsdr")
    
    def log(self, message: str):
        """Log message if verbose enabled"""
        if self.verbose:
            timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{timestamp}] {message}")

    def _emit_timing_event(self, event: str, **details) -> None:
        """Emit optional live timing telemetry without changing capture flow."""
        callback = self.timing_callback
        if callback is not None:
            callback(event, details)

    def _network_error(self, error_type, message: str, request=None):
        now = time.monotonic()
        request = request or self._active_capture or {}
        started = request.get("started", now)
        received = request.get("received", 0)
        expected = request.get("expected", 0)
        elapsed = max(0.0, now - started)
        last_age = max(0.0, now - self.last_byte_timestamp) if self.last_byte_timestamp else elapsed
        effective = received / elapsed if elapsed > 0 else 0.0
        return error_type(
            message,
            received_bytes=received,
            expected_bytes=expected,
            elapsed=f"{elapsed:.6f}",
            effective_throughput=f"{effective:.3f}",
            seconds_since_last_byte=f"{last_age:.6f}",
            consumer_state=self.consumer_state,
            generation=request.get("generation", self.generation),
        )

    def get_network_telemetry(self) -> Dict[str, Any]:
        """Return an atomic snapshot of continuous-consumer telemetry."""
        with self._consumer_condition:
            request = self._active_capture or {}
            now = time.monotonic()
            return {
                "bytes_received_total": self.bytes_received_total,
                "bytes_discarded": self.bytes_discarded,
                "bytes_kept": self.bytes_kept,
                "instantaneous_throughput": self.instantaneous_throughput,
                "seconds_since_last_byte": (
                    max(0.0, now - self.last_byte_timestamp)
                    if self.last_byte_timestamp else float("inf")
                ),
                "consumer_state": self.consumer_state,
                "generation": request.get("generation", self.generation),
                "expected_bytes": request.get("expected", 0),
                "received_capture_bytes": request.get("received", 0),
                "buffer_capacity": request.get("expected", 0),
                "buffer_used": request.get("received", 0),
                "socket_reconnect_count": self.socket_reconnect_count,
                "last_error": self.last_error,
            }

    def _ensure_consumer_started(self) -> None:
        if self.mode != "network" or self.socket is None:
            raise SDRDisconnected("rtl_tcp socket is not connected")
        if self._consumer_thread and self._consumer_thread.is_alive():
            return
        self._consumer_stop.clear()
        self.consumer_state = "DISCARD"
        self.socket.settimeout(0.25)
        self._consumer_thread = threading.Thread(
            target=self._network_consumer_loop,
            name="rtl_tcp-continuous-consumer",
            daemon=True,
        )
        self._consumer_thread.start()

    def _fail_consumer(self, error: SDRNetworkError) -> None:
        with self._consumer_condition:
            self.last_error = str(error)
            self.consumer_state = "FAILED"
            request = self._active_capture
            if request and request.get("error") is None:
                request["error"] = error
                request["done"] = True
            self._consumer_condition.notify_all()
        if self.socket is not None:
            failed_socket = self.socket
            try:
                failed_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            failed_socket.close()
            self.socket = None

    def _network_consumer_loop(self) -> None:
        """The sole owner of streaming recv(); always drains rtl_tcp."""
        last_measurement = time.monotonic()
        measured_bytes = 0
        while not self._consumer_stop.is_set():
            try:
                chunk = self.socket.recv(524288)
            except socket.timeout:
                with self._consumer_condition:
                    request = self._active_capture
                    if request and not request.get("done"):
                        age = time.monotonic() - request.get("last_byte", request["started"])
                        if age >= self.stall_timeout:
                            self._fail_consumer(self._network_error(
                                SDRStallNoBytes, "rtl_tcp delivered no bytes", request
                            ))
                            return
                continue
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError) as exc:
                if not self._consumer_stop.is_set():
                    self._fail_consumer(self._network_error(
                        SDRDisconnected, f"rtl_tcp socket error: {exc}"
                    ))
                return
            if not chunk:
                if not self._consumer_stop.is_set():
                    self._fail_consumer(self._network_error(
                        SDRDisconnected, "rtl_tcp closed the stream"
                    ))
                return

            now = time.monotonic()
            size = len(chunk)
            with self._consumer_condition:
                self.bytes_received_total += size
                self.last_byte_timestamp = now
                measured_bytes += size
                interval = now - last_measurement
                if interval >= 0.25:
                    self.instantaneous_throughput = measured_bytes / interval
                    measured_bytes = 0
                    last_measurement = now

                request = self._active_capture
                if request and not request.get("done"):
                    remaining = request["expected"] - request["received"]
                    kept = min(remaining, size)
                    start = request["received"]
                    request["buffer"][start:start + kept] = chunk[:kept]
                    request["received"] += kept
                    request["last_byte"] = now
                    self.bytes_kept += kept
                    if kept < size:
                        self.bytes_discarded += size - kept
                    if request["received"] == request["expected"]:
                        request["done"] = True
                        request["completed"] = now
                        self._active_capture = None
                        self.consumer_state = "DISCARD"
                        self._consumer_condition.notify_all()
                else:
                    self.bytes_discarded += size
    
    def _print_progress_bar(self, current: float, total: float, label: str, 
                           start_time: float, width: int = 40):
        """
        Print progress bar with percentage and timing
        
        Args:
            current: Current progress value
            total: Total value
            label: Label for the operation
            start_time: Start time (from time.perf_counter())
            width: Width of progress bar in characters
        """
        percent = min(100, (current / total) * 100)
        filled = int(width * current / total)
        bar = '█' * filled + '░' * (width - filled)
        elapsed = time.perf_counter() - start_time
        
        # Estimate remaining time
        if current > 0:
            eta = (elapsed / current) * (total - current)
            eta_str = f"Remaining: {eta:.1f}s"
        else:
            eta_str = "Remaining: --s"
        
        print(f"\r\033[2K{label.upper():<10} [{bar}] {percent:5.1f}% elapsed={elapsed:.1f}s eta={eta:.1f}s" if current > 0 else
              f"\r\033[2K{label.upper():<10} [{bar}] {percent:5.1f}% elapsed={elapsed:.1f}s",
              end='', flush=True)
    
    async def connect(self) -> CaptureMetrics:
        """
        Connect to SDR (USB or network)
        
        Returns:
            Metrics with connection timing
        """
        if self.mode == "usb":
            return await self._connect_usb()
        else:
            return await self._connect_network()
    
    async def _connect_usb(self) -> CaptureMetrics:
        """Connect to local RTL-SDR via USB"""
        metrics = CaptureMetrics()
        
        self.log(f"Opening RTL-SDR device {self.device_index} via USB...")
        start = time.perf_counter()
        
        # Open device
        self.sdr = RtlSdr(device_index=self.device_index)
        metrics.usb_open_time = time.perf_counter() - start
        
        self.log(f"RTL-SDR opened in {metrics.usb_open_time*1000:.2f}ms")
        
        return metrics
    
    async def _connect_network(self) -> CaptureMetrics:
        """Connect to rtl_tcp server"""
        metrics = CaptureMetrics()
        
        self.log(f"Connecting to rtl_tcp at {self.host}:{self.port}...")
        start = time.perf_counter()
        
        # Create socket connection
        self.consumer_state = "CONNECTING"
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(5.0)
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.socket.connect, (self.host, self.port))
        
        # Read dongle info (12 bytes) that rtl_tcp sends on connect
        # Format: "RTL0" magic (4 bytes) + tuner type (4 bytes) + gain count (4 bytes)
        def read_dongle_info():
            payload = bytearray()
            while len(payload) < 12:
                chunk = self.socket.recv(12 - len(payload))
                if not chunk:
                    raise SDRDisconnected("rtl_tcp closed during dongle handshake")
                payload.extend(chunk)
            return bytes(payload)

        dongle_info = await loop.run_in_executor(None, read_dongle_info)
        if len(dongle_info) == 12:
            magic = dongle_info[0:4].decode('ascii', errors='ignore')
            self.log(f"Received dongle info: {magic}")
        
        metrics.network_connect_time = time.perf_counter() - start
        self.socket_reconnect_count += 1
        
        self.log(f"Connected to rtl_tcp in {metrics.network_connect_time*1000:.2f}ms")
        
        return metrics
    
    async def configure(self, center_freq: int = 1420405752,
                       sample_rate: int = 2400000,
                       gain: str = 'auto') -> CaptureMetrics:
        """
        Configure SDR parameters
        
        Args:
            center_freq: Center frequency in Hz (default: 1420.405752 MHz for HI line)
            sample_rate: Sample rate in Hz
            gain: Gain setting ('auto' or numeric value)
        
        Returns:
            Metrics with configuration timing
        """
        metrics = CaptureMetrics()
        metrics.sample_rate = sample_rate
        
        if self.mode == "usb":
            return await self._configure_usb(center_freq, sample_rate, gain, metrics)
        else:
            return await self._configure_network(center_freq, sample_rate, gain, metrics)
    
    async def _configure_usb(self, center_freq: int, sample_rate: int,
                            gain: str, metrics: CaptureMetrics) -> CaptureMetrics:
        """Configure USB RTL-SDR"""
        self.log(f"Configuring RTL-SDR: {center_freq/1e6:.6f} MHz @ {sample_rate/1e6:.2f} MS/s")
        start = time.perf_counter()
        
        self.sdr.sample_rate = sample_rate
        self.sdr.center_freq = center_freq
        
        if gain == 'auto':
            self.sdr.gain = 'auto'
        else:
            self.sdr.gain = float(gain)
        
        metrics.usb_config_time = time.perf_counter() - start
        
        self.log(f"RTL-SDR configured in {metrics.usb_config_time*1000:.2f}ms")
        
        return metrics
    
    async def _configure_network(self, center_freq: int, sample_rate: int,
                                 gain: str, metrics: CaptureMetrics) -> CaptureMetrics:
        """Configure rtl_tcp server"""
        self.log(f"Configuring rtl_tcp: {center_freq/1e6:.6f} MHz @ {sample_rate/1e6:.2f} MS/s")
        start = time.perf_counter()
        
        # rtl_tcp command format: [CMD: 1 byte][ARG: 4 bytes big-endian]
        # CMD 0x01: Set frequency, CMD 0x02: Set sample rate, CMD 0x03: Set gain mode, CMD 0x04: Set gain
        
        loop = asyncio.get_event_loop()
        
        # Set sample rate FIRST (most important for data integrity)
        cmd = struct.pack('>BI', 0x02, sample_rate)
        await loop.run_in_executor(None, self.socket.send, cmd)
        
        # Set frequency
        cmd = struct.pack('>BI', 0x01, center_freq)
        await loop.run_in_executor(None, self.socket.send, cmd)
        
        # Set gain mode (0 = manual, 1 = auto)
        if gain == 'auto':
            cmd = struct.pack('>BI', 0x03, 1)
        else:
            cmd = struct.pack('>BI', 0x03, 0)
            # Set gain value (tenths of dB)
            cmd = struct.pack('>BI', 0x04, int(float(gain) * 10))
        
        await loop.run_in_executor(None, self.socket.send, cmd)
        
        # Wait for rtl_tcp to apply settings.  Streaming data is subsequently
        # owned exclusively by the permanent consumer.
        await asyncio.sleep(0.2)
        self._ensure_consumer_started()
        
        metrics.usb_config_time = time.perf_counter() - start
        
        self.log(f"rtl_tcp configured in {metrics.usb_config_time*1000:.2f}ms")
        
        return metrics
    
    async def capture(self, duration: float, output_file: str,
                     sample_rate: int = 2400000, 
                     metadata: Optional[Dict[str, Any]] = None) -> CaptureMetrics:
        """
        Capture IQ samples and write to HDF5 file with complete metadata
        
        Args:
            duration: Capture duration in seconds
            output_file: Output file path (.h5 extension recommended)
            sample_rate: Sample rate in Hz
            metadata: Additional observation metadata (coordinates, timestamps, etc.)
        
        Returns:
            Complete metrics including capture and I/O timing
        """
        if self.mode == "usb":
            return await self._capture_usb(duration, output_file, sample_rate, metadata)
        else:
            return await self._capture_network(duration, output_file, sample_rate, metadata)
    
    async def _capture_usb(self, duration: float, output_file: str,
                          sample_rate: int) -> CaptureMetrics:
        """Capture from USB RTL-SDR - read_samples() captures in real time"""
        num_samples = int(duration * sample_rate)
        
        if not self.verbose:
            print(f"   📻 Capturing during {duration:.1f}s real time ({num_samples:,} samples @ {sample_rate/1e6:.1f}M S/s)...", flush=True)
        else:
            self.log(f"Capturing {num_samples:,} samples ({duration}s real time) via USB...")
        
        # Capture timing - read_samples() blocks until it gets all samples in REAL TIME
        capture_start = time.perf_counter()
        
        if not self.verbose:
            # Show spinning indicator during blocking capture
            import sys
            spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
            
            async def show_spinner():
                idx = 0
                start = time.perf_counter()
                while True:
                    elapsed = time.perf_counter() - start
                    remaining = duration - elapsed
                    print(f"\r   Capturing... {spinner[idx % len(spinner)]} Elapsed: {elapsed:.1f}s | Remaining: {max(0, remaining):.1f}s", 
                          end='', flush=True)
                    await asyncio.sleep(0.1)
                    idx += 1
            
            # Start spinner task
            spinner_task = asyncio.create_task(show_spinner())
            
            try:
                samples = await asyncio.get_event_loop().run_in_executor(
                    None, self.sdr.read_samples, num_samples
                )
            finally:
                spinner_task.cancel()
                try:
                    await spinner_task
                except asyncio.CancelledError:
                    pass
                print("\r" + " " * 80 + "\r", end='', flush=True)  # Clear spinner line
        else:
            samples = await asyncio.get_event_loop().run_in_executor(
                None, self.sdr.read_samples, num_samples
            )
        
        capture_time = time.perf_counter() - capture_start
        
        # Disk write timing with optimized buffering and progress bar
        data_mb = len(samples) * 8 / 1024 / 1024
        if not self.verbose:
            print(f"   💾 Escribiendo {data_mb:.1f} MB a disco...", flush=True)
        else:
            self.log(f"Writing {data_mb:.2f} MB to {output_file}...")
        
        write_start = time.perf_counter()
        
        # Convert complex samples to interleaved I/Q bytes (uint8)
        import numpy as np
        
        # RTL-SDR gives float complex in range [-1, 1], convert to uint8 [0, 255]
        iq_data = np.empty(len(samples) * 2, dtype=np.uint8)
        iq_data[0::2] = ((samples.real + 1) * 127.5).astype(np.uint8)  # I
        iq_data[1::2] = ((samples.imag + 1) * 127.5).astype(np.uint8)  # Q
        
        # Write with large buffer for NVMe optimization - with progress bar
        total_bytes = len(iq_data)
        chunk_size = 1024 * 1024  # 1MB chunks for progress updates
        written = 0
        
        with open(output_file, 'wb', buffering=8*1024*1024) as f:
            while written < total_bytes:
                chunk_end = min(written + chunk_size, total_bytes)
                f.write(iq_data[written:chunk_end].tobytes())
                written = chunk_end
                
                # Update progress bar (not in verbose mode)
                if not self.verbose:
                    self._print_progress_bar(written, total_bytes, "Saving", 
                                            write_start, width=40)
                
                # Small yield to update display
                await asyncio.sleep(0)
            
            f.flush()
            await asyncio.get_event_loop().run_in_executor(None, f.fileno)  # Ensure fsync
        
        write_time = time.perf_counter() - write_start
        
        if not self.verbose:
            self._print_progress_bar(expected_bytes, expected_bytes, "CAPTURE", capture_start, width=40)
            # Clear the in-place bar before the permanent phase summary.  The
            # spaces also work on terminals that ignore ANSI erase-line.
            print(
                "\r\033[2K" + (" " * 160) + "\r\033[2K",
                end="",
                flush=True,
            )
            print(f"   ✓ Real-time capture of {duration:.1f}s completed (SDR read: {capture_time:.2f}s, write: {write_time:.2f}s)", flush=True)
        
        # Calculate metrics
        total_bytes = len(samples) * 8  # complex64 = 8 bytes per sample
        throughput_mbps = total_bytes / (capture_time + write_time) / 1024 / 1024
        
        metrics = CaptureMetrics(
            capture_time=capture_time,
            disk_write_time=write_time,
            total_samples=len(samples),
            sample_rate=sample_rate,
            throughput_mbps=throughput_mbps
        )
        
        if self.verbose:
            self.log(f"Capture complete: {capture_time*1000:.2f}ms capture + {write_time*1000:.2f}ms write")
            self.log(f"Throughput: {throughput_mbps:.2f} MB/s")
        
        return metrics
    
    async def _capture_network(self, duration: float, output_file: str,
                              sample_rate: int, metadata: Optional[Dict[str, Any]] = None) -> CaptureMetrics:
        """Capture from rtl_tcp server and save to HDF5 with complete metadata"""
        if not HAS_HDF5:
            raise ImportError("h5py not installed. Install with: pip install h5py")
        
        expected_samples = int(duration * sample_rate)
        expected_bytes = expected_samples * 2  # I+Q bytes (uint8 each)
        
        if not self.verbose and not self.compact_console:
            print(f"   📻 Capturing {expected_samples:,} samples ({duration:.1f}s @ {sample_rate/1e6:.1f}M S/s)...", flush=True)
        else:
            self.log(f"Capturing {expected_samples:,} samples ({duration}s) via network...")
        
        capture_start = time.monotonic()
        max_capture_wall = (
            self.max_capture_wall_override
            if self.max_capture_wall_override is not None
            else max(duration * 2.0, duration + 15.0)
        )
        try:
            iq_data = bytearray(expected_bytes)
        except (MemoryError, OverflowError) as exc:
            raise MemoryError(f"cannot reserve bounded SDR buffer of {expected_bytes} bytes") from exc

        self._ensure_consumer_started()
        with self._consumer_condition:
            if self.consumer_state == "FAILED":
                raise SDRDisconnected(self.last_error or "rtl_tcp consumer failed")
            if self._active_capture is not None:
                raise RuntimeError("an SDR capture is already active")
            self.generation += 1
            request = {
                "generation": self.generation,
                "expected": expected_bytes,
                "received": 0,
                "buffer": iq_data,
                "started": capture_start,
                "last_byte": capture_start,
                "done": False,
                "error": None,
            }
            self._active_capture = request
            self.consumer_state = "CAPTURE"

        try:
            while True:
                await asyncio.sleep(0.02)
                with self._consumer_condition:
                    done = request["done"]
                    error = request["error"]
                    bytes_received = request["received"]
                if error is not None:
                    raise error
                if done:
                    break
                now = time.monotonic()
                elapsed = now - capture_start
                if elapsed >= max_capture_wall:
                    raise self._network_error(
                        SDRWallTimeout, "capture exceeded wall deadline", request
                    )
                if elapsed >= self.throughput_grace:
                    effective = bytes_received / elapsed
                    required = sample_rate * 2 * self.throughput_ratio
                    if effective < required:
                        raise self._network_error(
                            SDRThroughputTooLow,
                            f"effective throughput {effective:.1f} B/s below {required:.1f} B/s",
                            request,
                        )
        except asyncio.CancelledError:
            with self._consumer_condition:
                if self._active_capture is request:
                    self._active_capture = None
                    self.consumer_state = "DISCARD"
                    request["done"] = True
                    request["error"] = asyncio.CancelledError()
                    self._consumer_condition.notify_all()
            raise
        except Exception as exc:
            with self._consumer_condition:
                if self._active_capture is request:
                    self._active_capture = None
                    if self.consumer_state != "FAILED":
                        self.consumer_state = "DISCARD"
                    request["done"] = True
                    request["error"] = exc
            failure_time = time.monotonic() - capture_start
            if not self.verbose and not self.compact_console:
                print("\r\033[2K", end="", flush=True)
            print(f"CAPTURE    FAIL after {failure_time:.1f}s", flush=True)
            code = getattr(exc, "code", "SDR_NETWORK_ERROR")
            print(f"reason={code}", flush=True)
            part_path = Path(output_file).with_name(f"{Path(output_file).name}.part")
            try:
                part_path.unlink()
            except FileNotFoundError:
                pass
            raise
        
        capture_time = time.monotonic() - capture_start
        
        if not self.verbose and not self.compact_console:
            print()  # Newline after progress bar
        
        # Convert to numpy array (interleaved I/Q uint8)
        iq_samples = np.frombuffer(iq_data, dtype=np.uint8)
        actual_samples = len(iq_samples) // 2
        self._emit_timing_event(
            "capture_end", duration=capture_time, samples_received=actual_samples
        )
        
        # Write to HDF5 with complete metadata
        if not self.verbose and not self.compact_console:
            print(f"   💾 Saving to HDF5 with metadata...", flush=True)
        
        write_start = time.perf_counter()
        self._emit_timing_event("disk_write_start", file=str(output_file))
        
        # Ensure .h5 or .hdf5 extension
        output_path = Path(output_file)
        if output_path.suffix not in ['.h5', '.hdf5']:
            output_file = str(output_path.with_suffix('.h5'))
        
        now_utc = datetime.now(timezone.utc)
        # Prepare complete metadata
        capture_metadata = {
            # Time information
            'capture_start_utc': (metadata or {}).get('capture_started_at', now_utc.isoformat()),
            'capture_completed_at': now_utc.isoformat(),
            'unix_timestamp': now_utc.timestamp(),
            'created_at': now_utc.isoformat(),
            
            # SDR Configuration
            'center_frequency_hz': metadata.get('center_freq', 1420405752) if metadata else 1420405752,
            'sample_rate_hz': sample_rate,
            'gain': metadata.get('gain', 'auto') if metadata else 'auto',
            'sdr_mode': self.mode,
            'sdr_host': self.host if self.mode == 'network' else 'USB',
            'sdr_port': self.port if self.mode == 'network' else 0,
            
            # Capture parameters
            'duration_seconds': duration,
            'requested_capture_duration_sec': duration,
            'actual_capture_duration_sec': capture_time,
            'num_samples': actual_samples,
            'capture_time_seconds': capture_time,
            'throughput_mbps': bytes_received / capture_time / 1024 / 1024,
            
            # Data format
            'data_type': 'IQ_uint8',
            'sample_format': 'interleaved',  # I,Q,I,Q,I,Q...
            'bits_per_sample': 8,
            'capture_status': 'success',
            'file_state': 'complete',
            'final_filename': Path(output_file).name,
        }
        
        # Add observation metadata if provided
        if metadata:
            capture_metadata.update(metadata)
            capture_metadata.update({
                'target_ra_hours': metadata.get('ra_hours', None),
                'target_dec_deg': metadata.get('dec_degrees', None),
                'target_dec_degrees': metadata.get('dec_degrees', None),
                'target_ra_hms': metadata.get('ra_hms', None),
                'target_dec_dms': metadata.get('dec_dms', None),
                'target_azimuth': metadata.get('azimuth', None),
                'target_altitude': metadata.get('altitude', None),
                'target_name': metadata.get('target_name', None),
                'point_number': metadata.get('point_number', None),
                'settle_time_seconds': metadata.get('settle_time', None),
                'tracking_enabled': metadata.get('tracking', None),
                'telescope_name': metadata.get('telescope_name', None),
                # Observer location (critical for Doppler corrections)
                'observer_latitude_deg': metadata.get('observer_latitude', None),
                'observer_longitude_deg': metadata.get('observer_longitude', None),
                'observer_elevation_m': metadata.get('observer_elevation', None),
                'observer_name': metadata.get('observer_name', None),
                'observer_location': metadata.get('observer_location', None),
            })
        
        # The final name becomes visible only after close, sync and validation.
        # Keep the event loop free while the dedicated consumer continues to
        # drain and discard rtl_tcp data during compression/fsync/rename.
        output_path = await asyncio.get_running_loop().run_in_executor(
            None, write_hdf5_atomic,
            output_file, iq_samples, capture_metadata, actual_samples,
        )
        
        write_time = time.perf_counter() - write_start
        self._emit_timing_event(
            "disk_write_end", duration=write_time, file=str(output_path)
        )
        
        # Calculate file size
        file_size_mb = Path(output_file).stat().st_size / 1024 / 1024
        
        throughput_mbps = bytes_received / capture_time / 1024 / 1024
        
        telemetry = self.get_network_telemetry()
        metrics = CaptureMetrics(
            capture_time=capture_time,
            disk_write_time=write_time,
            total_samples=actual_samples,
            sample_rate=sample_rate,
            throughput_mbps=throughput_mbps,
            bytes_received_total=telemetry["bytes_received_total"],
            bytes_discarded=telemetry["bytes_discarded"],
            bytes_kept=telemetry["bytes_kept"],
            instantaneous_throughput=telemetry["instantaneous_throughput"],
            effective_capture_throughput=bytes_received / capture_time,
            seconds_since_last_byte=telemetry["seconds_since_last_byte"],
            consumer_state=telemetry["consumer_state"],
            generation=request["generation"],
            expected_bytes=expected_bytes,
            received_capture_bytes=bytes_received,
            buffer_capacity=expected_bytes,
            buffer_used=bytes_received,
            socket_reconnect_count=telemetry["socket_reconnect_count"],
            last_error=telemetry["last_error"],
        )
        
        if not self.verbose and not self.compact_console:
            print(f"   ✓ {actual_samples:,} samples captured = {duration:.1f}s of data @ {sample_rate/1e6:.1f} MS/s", flush=True)
            print(f"     HDF5 file: {file_size_mb:.1f} MB (compressed)", flush=True)
            print(f"     Time: capture {capture_time:.2f}s + write {write_time:.2f}s = {capture_time + write_time:.2f}s total", flush=True)
        else:
            self.log(f"Capture complete: {capture_time:.2f}s transfer + {write_time:.2f}s write")
            self.log(f"HDF5 file: {file_size_mb:.1f} MB")
            self.log(f"Received {actual_samples:,} samples = {duration:.1f}s of data")
        
        return metrics
    
    async def flush_buffer(self) -> int:
        """
        Flush buffered data from rtl_tcp to discard contaminated samples
        Call this AFTER telescope SLEW+SETTLE to discard all accumulated data
        
        ADAPTIVE FLUSH: Drain until buffer is empty (consecutive timeouts)
        More efficient than fixed duration - adapts to actual buffer size
        
        Returns:
            Number of bytes flushed
        """
        if self.mode != "network" or not self.socket:
            return 0
        self._ensure_consumer_started()
        await asyncio.sleep(0)
        with self._consumer_condition:
            if self.consumer_state == "FAILED":
                raise SDRDisconnected(self.last_error or "rtl_tcp consumer failed")
            total_flushed = self.bytes_discarded - self._flush_marker
            self._flush_marker = self.bytes_discarded
        flush_duration = 0.0
        callback = self.flush_progress_callback
        if callback is not None:
            callback(total_flushed, flush_duration, 1.0)

        if not self.verbose and not self.compact_console and total_flushed > 0:
            flushed_samples = total_flushed // 2
            print(f"   🗑️  Buffer flushed: {flushed_samples:,} samples discarded ({total_flushed/1024/1024:.1f} MB in {flush_duration:.1f}s)", flush=True)

        self.log(f"Flushed {total_flushed} bytes ({total_flushed//2} samples) from buffer in {flush_duration:.1f}s")

        return total_flushed
    
    async def close(self):
        """Close SDR connection"""
        if self.mode == "usb" and self.sdr:
            self.sdr.close()
            self.log("RTL-SDR closed")
        elif self.mode == "network":
            self._consumer_stop.set()
            network_socket = self.socket
            if network_socket:
                try:
                    network_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                network_socket.close()
            if self._consumer_thread and self._consumer_thread.is_alive():
                await asyncio.get_running_loop().run_in_executor(
                    None, self._consumer_thread.join, 2.0
                )
            self.socket = None
            self.consumer_state = "CLOSED"
            self.log("rtl_tcp connection closed")


async def test_sdr_modes():
    """Test both USB and network modes with timing comparison"""
    print("="*80)
    print("SDR CAPTURE MODE COMPARISON TEST")
    print("="*80)
    print()
    
    test_duration = 1.0  # 1 second test
    sample_rate = 2400000  # 2.4 MS/s
    center_freq = 1420405752  # HI line
    
    results = {}
    
    # Test USB mode
    print("🔌 Testing USB Mode...")
    print("-"*80)
    try:
        sdr_usb = SDRCapture(mode="usb", verbose=True)
        
        conn_metrics = await sdr_usb.connect()
        config_metrics = await sdr_usb.configure(center_freq, sample_rate)
        capture_metrics = await sdr_usb.capture(test_duration, "/tmp/test_usb.dat", sample_rate)
        
        await sdr_usb.close()
        
        total_time = (conn_metrics.usb_open_time + config_metrics.usb_config_time + 
                     capture_metrics.capture_time + capture_metrics.disk_write_time)
        
        results['usb'] = {
            'total_time': total_time,
            'throughput': capture_metrics.throughput_mbps,
            'metrics': capture_metrics
        }
        
        print()
        print(capture_metrics)
        print(f"\n⏱️  Total USB Time: {total_time*1000:.2f}ms")
        
    except Exception as e:
        print(f"❌ USB mode failed: {e}")
        results['usb'] = None
    
    print()
    print("="*80)
    print()
    
    # Test Network mode (requires rtl_tcp server running)
    print("🌐 Testing Network Mode (requires rtl_tcp server)...")
    print("-"*80)
    try:
        sdr_net = SDRCapture(mode="network", host="localhost", port=1234, verbose=True)
        
        conn_metrics = await sdr_net.connect()
        config_metrics = await sdr_net.configure(center_freq, sample_rate)
        capture_metrics = await sdr_net.capture(test_duration, "/tmp/test_network.dat", sample_rate)
        
        await sdr_net.close()
        
        total_time = (conn_metrics.network_connect_time + config_metrics.usb_config_time + 
                     capture_metrics.capture_time)
        
        results['network'] = {
            'total_time': total_time,
            'throughput': capture_metrics.throughput_mbps,
            'metrics': capture_metrics
        }
        
        print()
        print(capture_metrics)
        print(f"\n⏱️  Total Network Time: {total_time*1000:.2f}ms")
        
    except Exception as e:
        print(f"❌ Network mode failed: {e}")
        print("   (Hint: Start rtl_tcp server with: rtl_tcp -a 127.0.0.1 -p 1234)")
        results['network'] = None
    
    print()
    print("="*80)
    print("📊 COMPARISON SUMMARY")
    print("="*80)
    
    if results.get('usb') and results.get('network'):
        usb_time = results['usb']['total_time']
        net_time = results['network']['total_time']
        
        faster = "USB" if usb_time < net_time else "Network"
        diff_ms = abs(usb_time - net_time) * 1000
        diff_pct = (abs(usb_time - net_time) / max(usb_time, net_time)) * 100
        
        print(f"USB Total Time:     {usb_time*1000:.2f}ms")
        print(f"Network Total Time: {net_time*1000:.2f}ms")
        print()
        print(f"⚡ {faster} is FASTER by {diff_ms:.2f}ms ({diff_pct:.1f}%)")
        print()
        print(f"USB Throughput:     {results['usb']['throughput']:.2f} MB/s")
        print(f"Network Throughput: {results['network']['throughput']:.2f} MB/s")
    
    print("="*80)


if __name__ == "__main__":
    asyncio.run(test_sdr_modes())
