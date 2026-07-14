#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Capture Script - Executes observation plan from grid_generator.py
Connects to telescope via INDI (XML/TCP direct) and captures data at each grid point

Reads CSV from grid_generator.py and executes the observation plan.
Supports resuming interrupted sessions via session_manager.py
"""

import sys
import os
import csv
import json
import asyncio
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict
from indi_telescope_control import INDITelescopeControl
from session_manager import SessionManager
from sdr_capture import SDRCapture, CaptureMetrics


class CaptureExecutor:
    """
    Executes observation plans from CSV files with session management
    """

    def __init__(self, csv_path: str, host: str = "localhost", port: int = 7624,
                 device_name: Optional[str] = None, session_id: Optional[str] = None,
                 config_path: str = "observer_config.json",
                 verbose: bool = False, sdr_mode: str = "network",
                 sdr_host: str = "localhost", sdr_port: int = 1234,
                 sdr_freq: int = 1420405752, sdr_sample_rate: int = 2400000):
        """
        Initialize capture executor

        Args:
            csv_path: Path to CSV file from grid_generator.py
            host: INDI server host
            port: INDI server port
            device_name: INDI device name (default: auto-detect)
            session_id: Existing session ID to resume (optional)
            config_path: Path to observer configuration JSON (default: observer_config.json)
            verbose: Enable verbose/debug output (default: False)
            sdr_mode: SDR capture mode 'usb' or 'network' (default: network)
            sdr_host: rtl_tcp server host (default: localhost)
            sdr_port: rtl_tcp server port (default: 1234)
            sdr_freq: Center frequency in Hz (default: 1420405752 for HI)
            sdr_sample_rate: Sample rate in Hz (default: 2400000)
        """
        self.csv_path = Path(csv_path)
        self.host = host
        self.port = port
        self.device_name = device_name or "Telescope Simulator"
        self.telescope = None
        self.observation_points = []
        self.session_manager = SessionManager()
        self.session_id = session_id
        self.current_session_data = None
        self.verbose = verbose
        
        # SDR configuration
        self.sdr_mode = sdr_mode
        self.sdr_host = sdr_host
        self.sdr_port = sdr_port
        self.sdr_freq = sdr_freq
        self.sdr_sample_rate = sdr_sample_rate
        self.sdr = None
        
        # Load observer configuration
        config_full_path = Path(config_path)
        if not config_full_path.is_absolute():
            config_full_path = self.csv_path.parent / config_path
        
        try:
            with open(config_full_path, 'r') as f:
                self.observer_config = json.load(f)
            if self.verbose:
                self.log(f"✓ Observer config loaded: {config_full_path}")
                self.log(f"  Location: {self.observer_config.get('observer', {}).get('name', 'Unknown')}")
        except FileNotFoundError:
            self.log(f"⚠️  Observer config not found: {config_full_path}", "WARNING", force=True)
            self.log(f"   Creating default config...", "WARNING", force=True)
            # Create default config with Santiago coordinates
            self.observer_config = {
                "observer": {
                    "name": "Default Observatory",
                    "latitude_deg": -33.4489,
                    "longitude_deg": -70.6693,
                    "elevation_m": 570,
                    "timezone": "America/Santiago"
                }
            }
            # Save default config
            try:
                with open(config_full_path, 'w') as f:
                    json.dump(self.observer_config, f, indent=2)
                self.log(f"   ✓ Default config created at: {config_full_path}", "INFO", force=True)
            except Exception as e:
                self.log(f"   Could not create config file: {e}", "WARNING")
        except json.JSONDecodeError as e:
            self.log(f"⚠️  Invalid JSON in observer config: {e}", "ERROR", force=True)
            self.observer_config = {}

        if self.verbose:
            self.log(f"Capture Executor initialized")
            self.log(f"CSV plan: {self.csv_path}")

        # Verify CSV exists
        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV file not found: {self.csv_path}")
    
    def log(self, message: str, level: str = "INFO", force: bool = False):
        """Print timestamped log message
        
        Args:
            message: Message to print
            level: Log level (INFO, WARNING, ERROR)
            force: Always print even in non-verbose mode
        """
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        if self.verbose or force or level in ["WARNING", "ERROR"]:
            print(f"[{timestamp}] {message}")
        sys.stdout.flush()
    
    def load_observation_plan(self, resume: bool = False, force: bool = False) -> bool:
        """
        Load observation plan from CSV

        Args:
            resume: If True, load only pending points (not completed)
            force: If True, reload ALL points regardless of status

        Returns:
            True if loaded successfully
        """
        try:
            self.log("📄 Reading observation plan from CSV...", force=True)

            with open(self.csv_path, 'r', newline='') as csvfile:
                reader = csv.DictReader(csvfile)
                self.observation_points = list(reader)

            # Validate CSV is not empty (has actual data rows)
            if len(self.observation_points) == 0:
                self.log("", "ERROR")
                self.log("=" * 80, "ERROR")
                self.log("CSV FILE IS EMPTY OR HAS NO DATA ROWS", "ERROR")
                self.log("=" * 80, "ERROR")
                self.log(f"CSV file: {self.csv_path}", "ERROR")
                self.log("", "ERROR")
                self.log("Possible causes:", "INFO")
                self.log("  1. Grid generation was interrupted before writing points", "INFO")
                self.log("  2. CSV file was corrupted or manually edited", "INFO")
                self.log("  3. Grid generation failed silently", "INFO")
                self.log("", "INFO")
                self.log("Solution: Regenerate the grid with grid_generator.py", "INFO")
                self.log("", "INFO")
                return False

            if force:
                # Force mode: load ALL points and reset status to 'planned'
                if self.verbose:
                    self.log(f"  FORCE mode: Resetting all points to 'planned'")
                for point in self.observation_points:
                    point['capture_status'] = 'planned'
                    point['start_time'] = ''
                    point['end_time'] = ''
                    point['duration'] = ''
                    point['error_message'] = ''
                self.log(f"   ✓ Total points to capture: {len(self.observation_points)}", force=True)
            elif resume:
                # When resuming, only load 'planned' points (not yet captured)
                pending_points = [p for p in self.observation_points 
                                if p['capture_status'] == 'planned']
                if self.verbose:
                    self.log(f"  Resuming session:")
                    self.log(f"    Total points in CSV: {len(self.observation_points)}")
                    self.log(f"    Pending points: {len(pending_points)}")
                self.log(f"   ✓ Resuming session: {len(pending_points)} pending points of {len(self.observation_points)} total", force=True)
                self.observation_points = pending_points
            else:
                # New session: filter only 'planned' points
                planned_points = [p for p in self.observation_points 
                                if p['capture_status'] == 'planned']
                if self.verbose:
                    self.log(f"  Total points in CSV: {len(self.observation_points)}")
                    self.log(f"  Points to capture: {len(planned_points)}")
                self.log(f"   ✓ Total points to capture: {len(planned_points)} of {len(self.observation_points)}", force=True)
                self.observation_points = planned_points

            if len(self.observation_points) == 0:
                self.log("No points to capture (all already done or failed)", "WARNING")
                self.log("Use --force to re-execute all points", "INFO")
                return False

            return True

        except Exception as e:
            self.log(f"Error loading CSV: {e}", "ERROR")
            return False
    
    def update_point_status(self, point_number: int, status: str, 
                           start_time: Optional[str] = None,
                           end_time: Optional[str] = None,
                           duration: Optional[float] = None,
                           error_msg: Optional[str] = None):
        """
        Update capture status in CSV file
        
        Args:
            point_number: Point number to update
            status: New status (capturing, success, failed)
            start_time: Start time (ISO format)
            end_time: End time (ISO format)
            duration: Duration in seconds
            error_msg: Error message if failed
        """
        try:
            # Read all rows
            all_rows = []
            with open(self.csv_path, 'r', newline='') as csvfile:
                reader = csv.DictReader(csvfile)
                fieldnames = reader.fieldnames
                all_rows = list(reader)
            
            # Update the specific point
            for row in all_rows:
                if int(row['point_number']) == point_number:
                    row['capture_status'] = status
                    if start_time:
                        row['start_time'] = start_time
                    if end_time:
                        row['end_time'] = end_time
                    if duration:
                        row['duration'] = f"{duration:.2f}"
                    if error_msg:
                        row['error_message'] = error_msg
                    break
            
            # Write back
            with open(self.csv_path, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_rows)
            
        except Exception as e:
            self.log(f"Error updating CSV: {e}", "WARNING")
    
    async def execute_observation_plan(self, 
                                      settle_time: float = 5.0,
                                      capture_time: float = 10.0) -> bool:
        """
        Execute the observation plan

        Args:
            settle_time: Time to wait for mount to settle (seconds)
            capture_time: Time for data capture at each point (seconds)

        Returns:
            True if all captures successful
        """
        if self.verbose:
            self.log("=" * 80)
            self.log("STARTING OBSERVATION EXECUTION")
            self.log("=" * 80)
            self.log(f"Total points to observe: {len(self.observation_points)}")
            self.log(f"Settle time: {settle_time}s")
            self.log(f"Capture time per point: {capture_time}s")
        else:
            self.log("="*80, force=True)
            self.log(f"🚀 STARTING CAPTURE - {len(self.observation_points)} points | Settle: {settle_time}s | Capture: {capture_time}s", force=True)
            self.log("="*80, force=True)

        # Get session name from CSV
        if self.observation_points:
            session_name = self.observation_points[0].get('session_name', 'unknown')
        else:
            session_name = 'unknown'

        # Create or resume session
        if self.session_id is None:
            self.session_id = self.session_manager.create_session(
                session_name=session_name,
                csv_plan_path=str(self.csv_path),
                total_points=len(self.observation_points),
                device_name=self.device_name or 'auto-detect'
            )
            self.log(f"📋 Session ID: {self.session_id}", force=True)
        else:
            self.log(f"📋 Resuming Session ID: {self.session_id}", force=True)

        if not self.verbose:
            self.log("", force=True)

        successful = 0
        failed = 0

        # Initialize SDR
        try:
            if self.verbose:
                self.log(f"Initializing SDR in {self.sdr_mode} mode...")
            
            self.sdr = SDRCapture(
                mode=self.sdr_mode,
                host=self.sdr_host,
                port=self.sdr_port,
                verbose=self.verbose
            )
            
            # Connect and configure SDR
            await self.sdr.connect()
            await self.sdr.configure(
                center_freq=self.sdr_freq,
                sample_rate=self.sdr_sample_rate,
                gain='auto'
            )
            
            if self.verbose:
                self.log(f"SDR initialized: {self.sdr_freq/1e6:.6f} MHz @ {self.sdr_sample_rate/1e6:.2f} MS/s")
            else:
                self.log(f"📻 SDR ready: {self.sdr_freq/1e6:.3f} MHz @ {self.sdr_sample_rate/1e6:.2f} MS/s (mode: {self.sdr_mode})", force=True)
                self.log("", force=True)
                
        except Exception as e:
            self.log(f"Failed to initialize SDR: {e}", "ERROR", force=True)
            return False

        try:
            for idx, point in enumerate(self.observation_points, start=1):
                point_start = datetime.now(timezone.utc)
                point_num = int(point['point_number'])
                ra_hours = float(point['target_ra_hours'])
                dec_deg = float(point['target_dec_degrees'])

                # Header for this point
                if self.verbose:
                    self.log(f"--- Point {idx}/{len(self.observation_points)} (#{point_num}) ---")
                    self.log(f"Target: RA={ra_hours:.4f}h, DEC={dec_deg:.4f} deg")
                    self.log(f"       ({point['target_ra_hms']}, {point['target_dec_dms']})")
                else:
                    self.log("="*80, force=True)
                    self.log(f"📍 POINT {idx}/{len(self.observation_points)} (#{point_num}) | RA={point['target_ra_hms']} DEC={point['target_dec_dms']}", force=True)
                    self.log("="*80, force=True)

                # GOTO coordinates
                if self.verbose:
                    self.log(f"GOTO coordinates...")
                else:
                    self.log(f"🔭 Step 1/3: SLEWING to coordinates", force=True)
                
                slew_start = datetime.now(timezone.utc)
                success = await self.telescope.goto(ra_hours, dec_deg)
                slew_end = datetime.now(timezone.utc)
                slew_time = (slew_end - slew_start).total_seconds()

                if not success:
                    self.log(f"   ❌ ERROR: SLEW failed!", "ERROR", force=True)
                    self.log("="*80, force=True)
                    self.update_point_status(point_num, 'failed', error_msg='GOTO failed')
                    failed += 1
                    # Update session stats
                    self.session_manager.update_session(
                        self.session_id,
                        points_failed=failed
                    )
                    continue

                if self.verbose:
                    self.log(f"  GOTO completed in {slew_time:.1f}s")
                else:
                    self.log(f"   ✓ SLEW completed in {slew_time:.1f}s", force=True)
                    self.log("="*80, force=True)

                # Enable tracking (ALWAYS ON during capture)
                if self.verbose:
                    self.log(f"Enabling tracking...")
                await self.telescope.set_tracking(True)

                # Wait for settle
                if self.verbose:
                    self.log(f"Settling for {settle_time}s...")
                else:
                    self.log(f"⏱️  Step 2/3: SETTLING (stabilizing telescope)", force=True)
                    self.log(f"   Settling time: {settle_time}s", force=True)
                
                settle_start = datetime.now(timezone.utc)
                await asyncio.sleep(settle_time)
                settle_end = datetime.now(timezone.utc)
                actual_settle = (settle_end - settle_start).total_seconds()
                
                if not self.verbose:
                    self.log(f"   ✓ Settling completed ({actual_settle:.1f}s)", force=True)
                    self.log("="*80, force=True)

                # FLUSH SDR BUFFER AFTER SETTLE - discard data accumulated during slew AND settle
                # This ensures we only capture data from the final stable position
                if self.sdr and hasattr(self.sdr, 'flush_buffer'):
                    flushed_bytes = await self.sdr.flush_buffer()
                    if self.verbose and flushed_bytes > 0:
                        self.log(f"  Flushed {flushed_bytes//2:,} samples from SDR buffer")

                # Start capture
                capture_start = datetime.now(timezone.utc)
                start_time_iso = capture_start.isoformat()

                self.update_point_status(point_num, 'capturing', start_time=start_time_iso)

                if self.verbose:
                    self.log(f"Capturing data for {capture_time}s...")
                    self.log(f"  Data file: {point['data_filename']}")
                else:
                    self.log(f"📡 Step 3/3: CAPTURING data", force=True)
                    self.log(f"   Capture time: {capture_time}s", force=True)
                    self.log(f"   File: {point['data_filename']}", force=True)

                # Real SDR capture
                try:
                    # Save HDF5 files in data/iq/<session_name>-<timestamp>/
                    # Format: data/iq/cygnus_hi_survey-2026-02-14-20:44:36/
                    
                    # Get session name from first point
                    session_name = point.get('session_name', 'unknown_session')
                    
                    # Create timestamp in format YYYYMMDD-HH:MM:SS
                    if not hasattr(self, '_capture_timestamp'):
                        self._capture_timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H:%M:%S')
                    
                    # Determine base directory
                    if self.csv_path.parent.name == 'data':
                        base_dir = self.csv_path.parent.parent
                    else:
                        base_dir = self.csv_path.parent
                    
                    output_dir = base_dir / 'data' / 'iq' / f"{session_name}-{self._capture_timestamp}"
                    output_dir.mkdir(parents=True, exist_ok=True)
                    
                    # Build HDF5 filename
                    base_filename = Path(point['data_filename']).stem  # Remove .dat extension
                    data_path = output_dir / f"{base_filename}.h5"
                    
                    # Prepare metadata for HDF5
                    capture_metadata = {
                        # Target coordinates
                        'ra_hours': ra_hours,
                        'dec_degrees': dec_deg,
                        'ra_hms': point['target_ra_hms'],
                        'dec_dms': point['target_dec_dms'],
                        'azimuth': point.get('azimuth', None),
                        'altitude': point.get('altitude', None),
                        'target_name': point.get('target_name', 'Unknown'),
                        'point_number': point_num,
                        
                        # Observation parameters
                        'settle_time': settle_time,
                        'slew_time': slew_time,
                        'tracking': True,
                        'telescope_name': self.telescope.device_name if hasattr(self.telescope, 'device_name') else 'Unknown',
                        
                        # SDR configuration
                        'center_freq': self.sdr_freq,
                        'gain': 'auto',
                        
                        # Observer location (critical for Doppler corrections)
                        'observer_latitude': self.observer_config.get('observer', {}).get('latitude_deg'),
                        'observer_longitude': self.observer_config.get('observer', {}).get('longitude_deg'),
                        'observer_elevation': self.observer_config.get('observer', {}).get('elevation_m'),
                        'observer_name': self.observer_config.get('observer', {}).get('name'),
                        'observer_location': self.observer_config.get('site_info', {}).get('location'),
                        
                        # Session info
                        'session_id': self.session_id,
                        'capture_start_iso': start_time_iso,
                    }
                    
                    # Capture with SDR
                    sdr_metrics = await self.sdr.capture(
                        duration=capture_time,
                        output_file=str(data_path),
                        sample_rate=self.sdr_sample_rate,
                        metadata=capture_metadata
                    )
                    
                    if self.verbose:
                        self.log(f"SDR Metrics:")
                        self.log(f"  Capture time: {sdr_metrics.capture_time*1000:.2f}ms")
                        self.log(f"  Write time: {sdr_metrics.disk_write_time*1000:.2f}ms")
                        self.log(f"  Throughput: {sdr_metrics.throughput_mbps:.2f} MB/s")
                    
                except Exception as e:
                    self.log(f"SDR capture error: {e}", "ERROR", force=True)
                    raise

                # End capture
                capture_end = datetime.now(timezone.utc)
                end_time_iso = capture_end.isoformat()
                duration = (capture_end - capture_start).total_seconds()

                self.update_point_status(point_num, 'success', 
                                       start_time=start_time_iso,
                                       end_time=end_time_iso,
                                       duration=duration)

                # Calculate total time for this point
                point_end = datetime.now(timezone.utc)
                total_point_time = (point_end - point_start).total_seconds()

                if self.verbose:
                    self.log(f"  Capture completed ({duration:.1f}s)")
                else:
                    self.log(f"   ✓ Capture completed ({duration:.1f}s)", force=True)
                    self.log("="*80, force=True)
                    self.log(f"📊 POINT SUMMARY #{point_num}", force=True)
                    self.log(f"   • SLEW time:    {slew_time:.1f}s", force=True)
                    self.log(f"   • SETTLE time:  {actual_settle:.1f}s", force=True)
                    self.log(f"   • CAPTURE time: {duration:.1f}s", force=True)
                    self.log(f"   • TOTAL TIME:   {total_point_time:.1f}s", force=True)
                
                successful += 1

                # Update session progress
                self.session_manager.update_session(
                    self.session_id,
                    last_point_completed=point_num,
                    points_completed=successful,
                    points_failed=failed
                )

                if not self.verbose and idx < len(self.observation_points):
                    self.log("="*80, force=True)
                    self.log("", force=True)
                elif self.verbose:
                    self.log("")

            # Mark session as completed
            self.session_manager.complete_session(self.session_id)

            # Summary
            if self.verbose:
                self.log("=" * 80)
                self.log("OBSERVATION EXECUTION COMPLETED")
                self.log("=" * 80)
                self.log(f"Total points: {len(self.observation_points)}")
                self.log(f"Successful: {successful}")
                self.log(f"Failed: {failed}")
                self.log(f"Session ID: {self.session_id}")
                self.log("")
            else:
                self.log("="*80, force=True)
                self.log(f"✅ OBSERVATION COMPLETED", force=True)
                self.log(f"   Total: {len(self.observation_points)} | Successful: {successful} | Failed: {failed}", force=True)
                self.log(f"   Session ID: {self.session_id}", force=True)
                self.log("="*80, force=True)

            return failed == 0

        except KeyboardInterrupt:
            # User interrupted - pause session
            self.log("", "WARNING", force=True)
            self.log("=" * 80, "WARNING", force=True)
            self.log("⚠️  OBSERVATION INTERRUPTED BY USER", "WARNING", force=True)
            self.log("=" * 80, "WARNING", force=True)
            self.session_manager.pause_session(self.session_id)
            self.log(f"📋 Session paused. Resume with: --resume {self.session_id}", "INFO", force=True)
            raise
        except Exception as e:
            # Unexpected error - mark as failed but save progress
            self.log(f"Observation execution error: {e}", "ERROR")
            self.session_manager.pause_session(self.session_id)
            import traceback
            self.log(traceback.format_exc(), "ERROR")
            return False
        finally:
            # Always close SDR on exit
            if self.sdr:
                await self.sdr.close()
                if self.verbose:
                    self.log("SDR closed")


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Capture Script - Executes observation plan from grid CSV (with resume support)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # New observation (MUST specify settle and capture times)
  %(prog)s --csv data/20260214_020754/orionidas.csv --settle 10 --capture 30

  # Force re-execute ALL points (even if already captured)
  %(prog)s --csv data/20260214_020754/orionidas.csv --settle 10 --capture 30 --force

  # With specific device
  %(prog)s --csv data/20260214_020754/orionidas.csv --device "Telescope Simulator" --settle 5 --capture 60

  # Resume interrupted session (uses same times)
  %(prog)s --resume 20260214_021530 --settle 10 --capture 30

  # List active sessions
  %(prog)s --list

This script reads the observation plan CSV from grid_generator.py and executes it.
Supports resuming interrupted sessions automatically.
Requires INDI server running with telescope connected.

NOTE: --settle and --capture are REQUIRED parameters (no defaults).
You must consciously choose appropriate times for your antenna/receiver setup.

FORCE MODE: Use --force to re-execute ALL points, ignoring their current status.
Useful for re-observations or after fixing equipment issues.
        """
    )

    # Arguments
    parser.add_argument('--csv', 
                        help='Path to CSV file from grid_generator.py')
    parser.add_argument('--resume', 
                        help='Resume session by ID (e.g., 20260214_021530)')
    parser.add_argument('--list', action='store_true',
                        help='List active/paused sessions and exit')
    parser.add_argument('--force', action='store_true',
                        help='Force re-execution of ALL points (ignores status)')

    # INDI connection
    parser.add_argument('--host', default='localhost',
                        help='INDI server address (default: localhost)')
    parser.add_argument('--port', type=int, default=7624,
                        help='INDI server port (default: 7624)')
    parser.add_argument('--device', default=None,
                        help='Telescope device name (default: auto-detect, e.g., "Telescope Simulator")')

    # Observation parameters (REQUIRED - no defaults for radio astronomy)
    parser.add_argument('--settle', type=float, required=True,
                        help='Mount/antenna settle time in seconds (REQUIRED)')
    parser.add_argument('--capture', type=float, required=True,
                        help='Capture duration per point in seconds (REQUIRED)')
    
    # Debug/verbose mode
    parser.add_argument('--debug', action='store_true',
                        help='Enable verbose debug output (default: concise output)')
    
    # SDR parameters
    parser.add_argument('--sdr-mode', default='network', choices=['usb', 'network'],
                        help='SDR capture mode: usb (direct) or network (rtl_tcp) (default: network)')
    parser.add_argument('--sdr-host', default='localhost',
                        help='rtl_tcp server host (default: localhost)')
    parser.add_argument('--sdr-port', type=int, default=1234,
                        help='rtl_tcp server port (default: 1234)')
    parser.add_argument('--sdr-freq', type=int, default=1420405752,
                        help='Center frequency in Hz (default: 1420405752 for HI line)')
    parser.add_argument('--sdr-rate', type=int, default=2400000,
                        help='Sample rate in Hz (default: 2400000)')

    args = parser.parse_args()

    # Initialize session manager
    session_mgr = SessionManager()

    # Handle --list
    if args.list:
        active_sessions = session_mgr.get_active_sessions()
        if not active_sessions:
            print("No active or paused sessions.")
        else:
            print("=" * 80)
            print("ACTIVE/PAUSED SESSIONS")
            print("=" * 80)
            for sess in active_sessions:
                print(f"\nSession ID: {sess['session_id']}")
                print(f"  Name: {sess['session_name']}")
                print(f"  Status: {sess['status']}")
                print(f"  Progress: {sess['points_completed']}/{sess['total_points']} completed, {sess['points_failed']} failed")
                print(f"  Last point: #{sess['last_point_completed']}")
                print(f"  CSV: {sess['csv_plan_path']}")
                print(f"  Device: {sess['device_name']}")
                print(f"  Started: {sess['start_time']}")
                print(f"  Updated: {sess['last_update_time']}")
        sys.exit(0)

    # Handle --resume
    if args.resume:
        session_id = args.resume
        session_data = session_mgr.get_session(session_id)

        if not session_data:
            print(f"ERROR: Session {session_id} not found")
            sys.exit(1)

        if session_data['status'] not in ['active', 'paused']:
            print(f"ERROR: Session {session_id} is {session_data['status']}, cannot resume")
            sys.exit(1)

        print(f"Resuming session: {session_id}")
        print(f"  Name: {session_data['session_name']}")
        print(f"  Progress: {session_data['points_completed']}/{session_data['total_points']} completed")
        print(f"  Last point: #{session_data['last_point_completed']}")
        print("")

        csv_path = session_data['csv_plan_path']
        device_name = args.device or session_data['device_name']

        executor = CaptureExecutor(
            csv_path=csv_path,
            host=args.host,
            port=args.port,
            device_name=device_name if device_name != 'auto-detect' else None,
            session_id=session_id,
            verbose=args.debug,
            sdr_mode=args.sdr_mode,
            sdr_host=args.sdr_host,
            sdr_port=args.sdr_port,
            sdr_freq=args.sdr_freq,
            sdr_sample_rate=args.sdr_rate
        )

        if not executor.load_observation_plan(resume=True, force=args.force):
            print("Failed to load observation plan")
            sys.exit(1)

    # Handle new session
    elif args.csv:
        executor = CaptureExecutor(
            csv_path=args.csv,
            host=args.host,
            port=args.port,
            device_name=args.device,
            verbose=args.debug,
            sdr_mode=args.sdr_mode,
            sdr_host=args.sdr_host,
            sdr_port=args.sdr_port,
            sdr_freq=args.sdr_freq,
            sdr_sample_rate=args.sdr_rate
        )

        if not executor.load_observation_plan(resume=False, force=args.force):
            print("Failed to load observation plan")
            sys.exit(1)

    else:
        parser.print_help()
        print("\nERROR: Must specify either --csv or --resume")
        sys.exit(1)

    # Connect to INDI server
    executor.telescope = INDITelescopeControl(
        host=executor.host,
        port=executor.port,
        device_name=executor.device_name,
        verbose=executor.verbose
    )

    if executor.verbose:
        executor.log("Connecting to INDI server...")
    else:
        executor.log(f"🔌 Connecting to INDI server at {executor.host}:{executor.port}...", force=True)
    
    if not await executor.telescope.connect():
        executor.log("❌ ERROR: Failed to connect to INDI server", "ERROR", force=True)
        sys.exit(1)

    if executor.verbose:
        executor.log(f"Connected to telescope: {executor.device_name}")
        executor.log("")
    else:
        executor.log(f"   ✓ Connected to telescope: {executor.device_name}", force=True)
        executor.log("", force=True)

    # Execute observation plan
    try:
        success = await executor.execute_observation_plan(
            settle_time=args.settle,
            capture_time=args.capture
        )

        if success:
            executor.log("All observations completed successfully!", "INFO")
            sys.exit(0)
        else:
            executor.log("Observation completed with some failures", "WARNING")
            sys.exit(1)

    except KeyboardInterrupt:
        executor.log("", "WARNING")
        executor.log("Observation interrupted by user. Session saved.", "WARNING")
        executor.log(f"Resume with: python capture.py --resume {executor.session_id}", "INFO")
        sys.exit(130)  # Standard exit code for Ctrl+C
    except Exception as e:
        executor.log(f"Unexpected error: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if executor.telescope and executor.telescope.writer:
            executor.telescope.writer.close()
            await executor.telescope.writer.wait_closed()
        executor.log("Disconnected from INDI server")


if __name__ == "__main__":
    asyncio.run(main())
