#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SDR Capture Module - RTL-SDR data capture with USB and network mode support
Optimized for NVMe disk I/O with detailed performance metrics
HDF5 output format with complete metadata for radio astronomy
"""

import asyncio
import time
import socket
import struct
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
        
        if self.mode not in ["usb", "network"]:
            raise ValueError(f"Invalid mode: {mode}. Must be 'usb' or 'network'")
        
        if self.mode == "usb" and not HAS_RTLSDR:
            raise ImportError("pyrtlsdr not installed. Run: pip install pyrtlsdr")
    
    def log(self, message: str):
        """Log message if verbose enabled"""
        if self.verbose:
            timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{timestamp}] {message}")
    
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
        
        print(f"\r   {label}: [{bar}] {percent:.1f}% | Elapsed: {elapsed:.1f}s | {eta_str}", 
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
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(5.0)
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.socket.connect, (self.host, self.port))
        
        # Read dongle info (12 bytes) that rtl_tcp sends on connect
        # Format: "RTL0" magic (4 bytes) + tuner type (4 bytes) + gain count (4 bytes)
        dongle_info = await loop.run_in_executor(None, self.socket.recv, 12)
        if len(dongle_info) == 12:
            magic = dongle_info[0:4].decode('ascii', errors='ignore')
            self.log(f"Received dongle info: {magic}")
        
        metrics.network_connect_time = time.perf_counter() - start
        
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
        
        # Wait for rtl_tcp to apply settings (critical for sample rate change)
        await asyncio.sleep(0.2)
        
        # Flush buffer to discard data with old settings
        self.socket.settimeout(0.01)
        flushed = 0
        try:
            while True:
                chunk = await loop.run_in_executor(None, self.socket.recv, 65536)
                if not chunk:
                    break
                flushed += len(chunk)
        except socket.timeout:
            pass
        
        if flushed > 0:
            self.log(f"Flushed {flushed/1024:.1f} KB of buffer after config")
        
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
            print()  # Newline after progress bar
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
        
        if not self.verbose:
            print(f"   📻 Capturing {expected_samples:,} samples ({duration:.1f}s @ {sample_rate/1e6:.1f}M S/s)...", flush=True)
        else:
            self.log(f"Capturing {expected_samples:,} samples ({duration}s) via network...")
        
        # CAPTURE EXACT NUMBER OF SAMPLES
        capture_start = time.perf_counter()
        last_progress_update = capture_start
        
        bytes_received = 0
        chunk_size = 16384
        iq_data = bytearray()  # Accumulate data in memory
        
        loop = asyncio.get_event_loop()
        
        # Capture all data
        while bytes_received < expected_bytes:
            remaining = expected_bytes - bytes_received
            read_size = min(chunk_size, remaining)
            
            self.socket.settimeout(5.0)
            chunk = await loop.run_in_executor(None, self.socket.recv, read_size)
            if not chunk:
                raise ConnectionError("rtl_tcp connection closed unexpectedly")
            
            iq_data.extend(chunk)
            bytes_received += len(chunk)
            
            # Update progress bar
            current_time = time.perf_counter()
            if not self.verbose and (current_time - last_progress_update) >= 0.1:
                self._print_progress_bar(bytes_received, expected_bytes, "Capturing", 
                                        capture_start, width=40)
                last_progress_update = current_time
        
        capture_time = time.perf_counter() - capture_start
        
        if not self.verbose:
            print()  # Newline after progress bar
        
        # Convert to numpy array (interleaved I/Q uint8)
        iq_samples = np.frombuffer(iq_data, dtype=np.uint8)
        actual_samples = len(iq_samples) // 2
        
        # Write to HDF5 with complete metadata
        if not self.verbose:
            print(f"   💾 Saving to HDF5 with metadata...", flush=True)
        
        write_start = time.perf_counter()
        
        # Ensure .h5 or .hdf5 extension
        output_path = Path(output_file)
        if output_path.suffix not in ['.h5', '.hdf5']:
            output_file = str(output_path.with_suffix('.h5'))
        
        # Prepare complete metadata
        capture_metadata = {
            # Time information
            'capture_start_utc': datetime.now(timezone.utc).isoformat(),
            'unix_timestamp': time.time(),
            
            # SDR Configuration
            'center_frequency_hz': metadata.get('center_freq', 1420405752) if metadata else 1420405752,
            'sample_rate_hz': sample_rate,
            'gain': metadata.get('gain', 'auto') if metadata else 'auto',
            'sdr_mode': self.mode,
            'sdr_host': self.host if self.mode == 'network' else 'USB',
            'sdr_port': self.port if self.mode == 'network' else 0,
            
            # Capture parameters
            'duration_seconds': duration,
            'num_samples': actual_samples,
            'capture_time_seconds': capture_time,
            'throughput_mbps': bytes_received / capture_time / 1024 / 1024,
            
            # Data format
            'data_type': 'IQ_uint8',
            'sample_format': 'interleaved',  # I,Q,I,Q,I,Q...
            'bits_per_sample': 8,
        }
        
        # Add observation metadata if provided
        if metadata:
            capture_metadata.update({
                'target_ra_hours': metadata.get('ra_hours', None),
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
        
        # Save to HDF5
        with h5py.File(output_file, 'w') as f:
            # Main data dataset with compression
            dset = f.create_dataset(
                'iq_data',
                data=iq_samples,
                compression='gzip',
                compression_opts=4,  # Balance between speed and size
                chunks=True
            )
            dset.attrs['description'] = 'Interleaved I/Q samples (uint8)'
            dset.attrs['format'] = 'I[0], Q[0], I[1], Q[1], ...'
            
            # Separate I and Q for convenience (virtual datasets or links)
            f.create_dataset('i_samples', data=iq_samples[0::2], compression='gzip', compression_opts=4)
            f['i_samples'].attrs['description'] = 'In-phase component (I)'
            
            f.create_dataset('q_samples', data=iq_samples[1::2], compression='gzip', compression_opts=4)
            f['q_samples'].attrs['description'] = 'Quadrature component (Q)'
            
            # Store all metadata as attributes
            for key, value in capture_metadata.items():
                if value is not None:
                    f.attrs[key] = value
            
            # Software version info
            f.attrs['software'] = 'INDIpy SDR Capture'
            f.attrs['file_format_version'] = '1.0'
            f.attrs['created_by'] = 'sdr_capture.py'
        
        write_time = time.perf_counter() - write_start
        
        # Calculate file size
        file_size_mb = Path(output_file).stat().st_size / 1024 / 1024
        
        throughput_mbps = bytes_received / capture_time / 1024 / 1024
        
        metrics = CaptureMetrics(
            capture_time=capture_time,
            disk_write_time=write_time,
            total_samples=actual_samples,
            sample_rate=sample_rate,
            throughput_mbps=throughput_mbps
        )
        
        if not self.verbose:
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
        
        loop = asyncio.get_event_loop()
        total_flushed = 0
        
        # Set socket to non-blocking with minimal timeout
        self.socket.settimeout(0.01)  # 10ms timeout
        
        # Adaptive flush: drain until buffer empty
        chunk_size = 524288  # 512KB chunks
        max_duration = 5.0  # Safety timeout (5s max)
        empty_threshold = 5  # Number of consecutive timeouts = buffer empty
        
        start_time = time.time()
        empty_count = 0
        
        while empty_count < empty_threshold:
            # Safety check: don't flush forever
            if (time.time() - start_time) > max_duration:
                self.log(f"Flush safety timeout after {max_duration}s")
                break
            
            try:
                chunk = await loop.run_in_executor(None, self.socket.recv, chunk_size)
                if chunk and len(chunk) > 0:
                    total_flushed += len(chunk)
                    empty_count = 0  # Reset counter - still draining
                else:
                    empty_count += 1  # No data received
            except socket.timeout:
                # Timeout = no data available
                empty_count += 1
            except Exception as e:
                self.log(f"Flush exception: {e}")
                break
        
        flush_duration = time.time() - start_time
        
        if not self.verbose and total_flushed > 0:
            flushed_samples = total_flushed // 2
            print(f"   🗑️  Buffer flushed: {flushed_samples:,} samples discarded ({total_flushed/1024/1024:.1f} MB in {flush_duration:.1f}s)", flush=True)
        
        self.log(f"Flushed {total_flushed} bytes ({total_flushed//2} samples) from buffer in {flush_duration:.1f}s")
        
        return total_flushed
    
    async def close(self):
        """Close SDR connection"""
        if self.mode == "usb" and self.sdr:
            self.sdr.close()
            self.log("RTL-SDR closed")
        elif self.mode == "network" and self.socket:
            self.socket.close()
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
