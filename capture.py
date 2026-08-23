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
import importlib
import math
import shutil
import tempfile
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict
import astropy.units as u
from astropy.coordinates import EarthLocation
from astropy.coordinates import AltAz, ICRS, SkyCoord
from astropy.time import Time
from indi_telescope_control import INDITelescopeControl
from session_manager import SessionManager
from sdr_capture import SDRCapture, CaptureMetrics, validate_hdf5_capture


def should_execute_after_preflight(report: Dict, preflight_only: bool) -> bool:
    """Single gate between preflight and any physical observation execution."""
    return bool(report.get("success")) and not preflight_only


def format_duration_hms(seconds: float) -> str:
    """Format a non-negative duration as HH:MM:SS."""
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class CaptureExecutor:
    """
    Executes observation plans from CSV files with session management
    """

    def __init__(self, csv_path: str, host: str = "localhost", port: int = 7624,
                 device_name: Optional[str] = None, session_id: Optional[str] = None,
                 config_path: str = "observer_config.json",
                 verbose: bool = False, sdr_mode: str = "network",
                 sdr_host: str = "localhost", sdr_port: int = 1234,
                 sdr_freq: int = 1420405752, sdr_sample_rate: int = 2400000,
                 min_altitude_deg: Optional[float] = None,
                 tracking_timeout: float = 5.0):
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
        self.compact_console = False
        self.time_provider = Time.now
        self.meridian_partition_metadata = {}
        self.actual_capture_order_offset = 0
        self.grid_metadata = self._load_grid_metadata()
        self._live_timing_csv_path = None
        self._live_timing_rows = []
        self._live_session_clock = None
        self._live_previous_point_complete_clock = None
        
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
        
        self.observer_config_path = config_full_path
        self.observer_config_preexisting = config_full_path.is_file()
        self.observer_config_valid = False
        try:
            with open(config_full_path, 'r') as f:
                self.observer_config = json.load(f)
            self.observer_config_valid = True
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

        defaults = self.observer_config.get("observation_defaults", {}) if isinstance(self.observer_config, dict) else {}
        self.min_altitude_deg = float(
            min_altitude_deg if min_altitude_deg is not None else defaults.get("min_altitude_deg", 30.0)
        )
        self.tracking_timeout = float(tracking_timeout)
        self.visibility_deferred_count = 0

        if self.verbose:
            self.log(f"Capture Executor initialized")
            self.log(f"CSV plan: {self.csv_path}")

        # Verify CSV exists
        # Existence/readability is a critical preflight check. load_observation_plan
        # retains its existing fail-fast behavior for normal CLI use.

    def _load_grid_metadata(self) -> Dict:
        """Load generator-owned beam/grid parameters without hardcoded defaults."""
        metadata_path = self.csv_path.parent / "grid_metadata.json"
        try:
            with open(metadata_path, "r") as metadata_file:
                metadata = json.load(metadata_file)
            grid = metadata.get("grid", {})
            return grid if isinstance(grid, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    async def _optional_mount_coordinates(self):
        """Read mount coordinates for provenance without making capture depend on them."""
        try:
            if self.telescope and hasattr(self.telescope, "get_coordinates"):
                return await self.telescope.get_coordinates(force_refresh=True)
        except Exception as exc:
            self.log(f"Optional mount coordinate read failed: {exc}", "WARNING")
        return None, None

    def _session_capture_directories(self, session_name: str) -> List[Path]:
        root = self._capture_output_root()
        if not root.is_dir():
            return []
        prefix = f"{session_name}-"
        return [path for path in root.iterdir() if path.is_dir() and path.name.startswith(prefix)]

    @staticmethod
    def _quarantine_invalid_final(path: Path) -> Path:
        """Preserve an invalid final under an unambiguous diagnostic name."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = path.with_name(f"{path.name}.invalid-{stamp}")
        os.replace(path, quarantine)
        return quarantine

    def reconcile_resume(self, rows: List[Dict]) -> Dict:
        """Reconcile CSV state against identity-validated final HDF5 captures."""
        report = {
            "valid_successes_preserved": 0, "promoted": 0,
            "reset_to_planned": 0, "invalid_finals": 0,
            "partials_cleaned": 0, "failed_preserved": 0,
            "orphan_partials": 0, "next_actual_capture_order": 1,
        }
        valid_orders = []
        associated_parts = set()

        for row in rows:
            status = str(row.get("capture_status", "planned")).strip().lower()
            session_name = row.get("session_name", "unknown_session")
            final_name = f"{Path(row['data_filename']).stem}.h5"
            directories = self._session_capture_directories(session_name)
            finals = [directory / final_name for directory in directories if (directory / final_name).is_file()]
            parts = [directory / f"{final_name}.part" for directory in directories if (directory / f"{final_name}.part").is_file()]
            associated_parts.update(parts)
            identity = {
                "point_id": row.get("point_id") or row.get("point_number"),
                "session_id": self.session_id,
                "scan_order": row.get("scan_order"),
            }
            valid = []
            for final in finals:
                try:
                    attrs = validate_hdf5_capture(final, expected_identity=identity)
                    valid.append((final, attrs))
                except Exception as exc:
                    quarantine = self._quarantine_invalid_final(final)
                    report["invalid_finals"] += 1
                    self.log(f"Invalid HDF5 quarantined: {final} -> {quarantine} ({exc})", "WARNING", force=True)

            if valid:
                _, attrs = max(valid, key=lambda item: item[0].stat().st_mtime)
                stored_order = attrs.get("actual_capture_order")
                if stored_order is not None:
                    order = int(stored_order)
                    valid_orders.append(order)
                    row["actual_capture_order"] = str(order)
                row["capture_status"] = "success"
                row["resume_reconciled"] = "true"
                if status in {"success", "completed"}:
                    report["valid_successes_preserved"] += 1
                    row["resume_reconcile_reason"] = "valid_success_preserved"
                elif status == "failed":
                    report["promoted"] += 1
                    row["resume_reconcile_reason"] = "valid_hdf5_after_failed_status_update"
                else:
                    report["promoted"] += 1
                    row["resume_reconcile_reason"] = "valid_hdf5_after_interrupted_status_update"
            elif status == "failed":
                report["failed_preserved"] += 1
                row["resume_reconciled"] = "true"
                row["resume_reconcile_reason"] = "failed_preserved"
            elif status not in {"planned", "capturing", "success", "completed"}:
                row["resume_reconciled"] = "true"
                row["resume_reconcile_reason"] = "unknown_status_preserved"
                self.log(f"Unknown capture status preserved for point {row.get('point_number')}: {status}", "WARNING", force=True)
            else:
                if status in {"success", "completed"}:
                    reason = "success_without_valid_hdf5"
                elif status == "capturing":
                    reason = "interrupted_capture"
                else:
                    reason = "planned_without_valid_hdf5"
                if status != "planned" or finals or parts:
                    report["reset_to_planned"] += 1
                if status == "capturing":
                    row["capture_status"] = "failed"
                    row["failed_at"] = datetime.now(timezone.utc).isoformat()
                    row["error_code"] = "interrupted_capture"
                    row["error_detail"] = "capture interrupted before a valid final HDF5 existed"
                    row["error_message"] = row["error_detail"]
                    report["failed_preserved"] += 1
                else:
                    row["capture_status"] = "planned"
                    row["actual_capture_order"] = ""
                row["resume_reconciled"] = "true"
                row["resume_reconcile_reason"] = reason

            for part in parts:
                part.unlink()
                report["partials_cleaned"] += 1

        # Parts with no exact data_filename association are evidence, not garbage.
        session_names = {row.get("session_name", "unknown_session") for row in rows}
        all_parts = {
            part
            for name in session_names
            for directory in self._session_capture_directories(name)
            for part in directory.glob("*.h5.part")
        }
        orphan_parts = sorted(all_parts.difference(associated_parts))
        report["orphan_partials"] = len(orphan_parts)
        for part in orphan_parts:
            self.log(f"Orphan partial preserved for diagnosis: {part}", "WARNING", force=True)

        self.actual_capture_order_offset = max(valid_orders, default=0)
        report["next_actual_capture_order"] = self.actual_capture_order_offset + 1
        fieldnames = []
        for row in rows:
            for field in row.keys():
                if field not in fieldnames:
                    fieldnames.append(field)
        for field in ("resume_reconciled", "resume_reconcile_reason"):
            if field not in fieldnames:
                fieldnames.append(field)
        with open(self.csv_path, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        if self.session_id:
            successes = sum(row.get("capture_status") == "success" for row in rows)
            failures = sum(row.get("capture_status") == "failed" for row in rows)
            completed_points = [int(row["point_number"]) for row in rows if row.get("capture_status") == "success"]
            self.session_manager.update_session(
                self.session_id, status="active", points_completed=successes,
                points_failed=failures, last_point_completed=max(completed_points, default=0),
            )
        self.log(
            "Resume reconciliation:\n"
            f"- valid successes preserved: {report['valid_successes_preserved']}\n"
            f"- promoted from capturing/planned by valid HDF5: {report['promoted']}\n"
            f"- reset to planned: {report['reset_to_planned']}\n"
            f"- invalid finals: {report['invalid_finals']}\n"
            f"- partials cleaned: {report['partials_cleaned']}\n"
            f"- failed preserved: {report['failed_preserved']}\n"
            f"- next actual_capture_order: {report['next_actual_capture_order']}",
            force=True,
        )
        return report
    
    def log(self, message: str, level: str = "INFO", force: bool = False):
        """Print timestamped log message
        
        Args:
            message: Message to print
            level: Log level (INFO, WARNING, ERROR)
            force: Always print even in non-verbose mode
        """
        if self.compact_console and level not in ["WARNING", "ERROR"]:
            return
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        if self.verbose or force or level in ["WARNING", "ERROR"]:
            print(f"[{timestamp}] {message}", flush=True)
        sys.stdout.flush()

    @staticmethod
    def _live_utc() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _live_event(self, row: Dict, label: str, field: Optional[str] = None,
                    details: Optional[List[str]] = None) -> float:
        """Print and record a diagnostic event without affecting control flow."""
        now = time.perf_counter()
        elapsed = now - row["_point_clock"]
        utc = self._live_utc()
        if field:
            row[field] = utc
            row[f"_{field}_clock"] = now
        # Compact mode records the same telemetry, but phase rendering is done
        # by the single-line progress/status display at each call site.
        if self.compact_console:
            return now
        print(f"[+{elapsed:07.3f}] {label}", flush=True)
        for detail in details or []:
            print(f"           {detail}", flush=True)
        return now

    def _console_progress(self, label: str, fraction: float, details: str = "",
                          force: bool = False) -> None:
        """Render one in-place observational progress line."""
        now = time.perf_counter()
        progress_state = getattr(self, "_console_progress_state", {})
        previous = progress_state.get(label, 0.0)
        if not force and now - previous < 0.25:
            return
        progress_state[label] = now
        self._console_progress_state = progress_state
        fraction = max(0.0, min(1.0, fraction))
        width = 36
        filled = int(round(width * fraction))
        bar = "█" * filled + "░" * (width - filled)
        print(f"\r\033[2K{label:<10} [{bar}] {fraction * 100:5.1f}% {details}",
              end="", flush=True)

    def _console_progress_end(self) -> None:
        print(
            "\r\033[2K" + (" " * 160) + "\r\033[2K",
            end="",
            flush=True,
        )

    def _observe_goto_log(self, row: Dict, message: str) -> None:
        """Translate existing goto verbose logs into the operator timeline."""
        baseline = re.search(r"GOTO baseline_seq=(\d+)", message)
        if baseline:
            row["baseline_eod_seq"] = int(baseline.group(1))
            self._live_event(row, "EOD BASELINE RECORDED",
                             details=[f"baseline_eod_seq={baseline.group(1)}"])
            return

        command_sent = re.search(r"GOTO command_sent_seq=(\d+)", message)
        if command_sent:
            self._live_event(row, "GOTO COMMAND SENT", "goto_command_utc",
                             [f"baseline_eod_seq={command_sent.group(1)}"])
            row["_waiting_last_report_clock"] = time.perf_counter()
            row["_motion_initial_error"] = None
            return

        eod = re.search(
            r"EOD seq=(\d+) state=(\w+) RA=([-+0-9.]+) DEC=([-+0-9.]+) "
            r"error=([-+0-9.]+)", message)
        if eod:
            seq, state, ra, dec, error = eod.groups()
            seq_i = int(seq)
            row.setdefault("first_post_command_eod_seq", seq_i)
            row["_last_eod"] = {
                "seq": seq_i, "state": state, "ra": ra, "dec": dec,
                "error": float(error),
            }
            if row.get("_motion_initial_error") is None and float(error) > 0:
                row["_motion_initial_error"] = float(error)
            if state != "Busy" and not row.get("motion_start_utc"):
                now = time.perf_counter()
                initial = row.get("_motion_initial_error") or float(error) or 1.0
                self._console_progress(
                    "WAIT MOTION", 0.0,
                    f"elapsed={now - row['_goto_command_utc_clock']:.1f}s "
                    f"EOD={state} seq={seq} err={float(error):.3f}°",
                )
            if state == "Busy" and not row.get("motion_start_utc"):
                self._console_progress_end()
                motion_clock = self._live_event(
                    row, "MOUNT MOTION START", "motion_start_utc",
                    ["detection=EOD Busy", f"seq={seq}",
                     f"command_to_motion_start={time.perf_counter() - row['_goto_command_utc_clock']:.3f} s"],
                )
                row["_motion_last_report_clock"] = motion_clock
            elif state == "Busy" and row.get("motion_start_utc"):
                now = time.perf_counter()
                initial = row.get("_motion_initial_error") or float(error) or 1.0
                fraction = 1.0 - min(1.0, float(error) / initial)
                self._console_progress(
                    "MOUNT MOVING", fraction,
                    f"elapsed={now - row['_motion_start_utc_clock']:.1f}s "
                    f"RA={float(ra):.4f} DEC={float(dec):.3f} "
                    f"err={float(error):.3f}° seq={seq}",
                )
            elif state != "Busy" and row.get("motion_start_utc") and not row.get("motion_end_utc"):
                self._console_progress(
                    "MOUNT MOVING", 1.0,
                    f"RA={float(ra):.4f} DEC={float(dec):.3f} err={float(error):.3f}°",
                    force=True,
                )
                self._console_progress_end()
                now = self._live_event(
                    row, "MOUNT MOTION END", "motion_end_utc",
                    [f"EOD={state}", f"seq={seq}",
                     f"physical_motion_duration={time.perf_counter() - row['_motion_start_utc_clock']:.3f} s",
                     f"command_to_motion_end={time.perf_counter() - row['_goto_command_utc_clock']:.3f} s"],
                )
            if float(error) <= 0.25 and not row.get("first_convergence_utc"):
                self._live_event(
                    row, "FIRST CONVERGENCE", "first_convergence_utc",
                    [f"error={float(error):.6f} deg", f"eod_seq={seq}"],
                )
            return

        hit = re.search(r"convergence=True stable_hit=(\d+) seq=(\d+)", message)
        if hit:
            number, seq = map(int, hit.groups())
            if number == 1 and not row.get("hit1_utc"):
                row["hit1_seq"] = seq
                error = row.get("_last_eod", {}).get("error")
                self._live_event(row, "STABLE HIT 1", "hit1_utc",
                                 [f"error={error} deg", f"seq={seq}"])
            elif number == 2 and not row.get("hit2_utc"):
                row["hit2_seq"] = seq
                error = row.get("_last_eod", {}).get("error")
                self._live_event(row, "STABLE HIT 2", "hit2_utc",
                                 [f"error={error} deg", f"seq={seq}"])

    def _write_live_timing_csv(self) -> None:
        """Rewrite the small diagnostic timing CSV atomically after each point."""
        if not self._live_timing_csv_path or not self._live_timing_rows:
            return
        fields = [
            "point_id", "scan_order", "actual_capture_order", "point_start_utc",
            "pre_guard_start", "pre_guard_end", "goto_command_utc",
            "motion_start_utc", "motion_end_utc", "first_convergence_utc",
            "hit1_utc", "hit2_utc", "goto_return_utc", "post_guard_start",
            "post_guard_end", "tracking_start", "tracking_confirmed",
            "post_tracking_guard_start", "post_tracking_guard_end",
            "settle_start", "settle_end", "flush_start", "flush_end",
            "capture_start", "capture_end", "disk_write_start", "disk_write_end",
            "validation_start", "validation_end", "rename_start", "rename_end",
            "persist_start", "persist_end", "point_complete_utc",
            "next_point_start_utc", "baseline_eod_seq", "first_post_command_eod_seq",
            "hit1_seq", "hit2_seq", "command_to_motion_start_sec",
            "physical_motion_sec", "motion_end_to_goto_return_sec", "goto_total_sec",
            "pre_guard_sec", "post_guard_sec", "tracking_sec",
            "post_tracking_guard_sec", "settle_sec", "flush_sec", "capture_sec",
            "disk_hdf5_sec", "persist_sec", "inter_point_delay_sec", "other_sec",
            "point_total_sec", "capture_disk_combined",
        ]
        temp_path = self._live_timing_csv_path.with_suffix(".csv.tmp")
        with temp_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self._live_timing_rows)
        os.replace(temp_path, self._live_timing_csv_path)
    
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

            if resume:
                self.reconcile_resume(self.observation_points)

            if not resume:
                existing_actual_orders = [
                    int(point["actual_capture_order"])
                    for point in self.observation_points
                    if str(point.get("actual_capture_order", "")).strip()
                ]
                self.actual_capture_order_offset = max(existing_actual_orders, default=0)

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
                    point['actual_capture_order'] = ''
                self.actual_capture_order_offset = 0
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

            self.partition_pending_by_hour_angle()

            return True

        except Exception as e:
            self.log(f"Error loading CSV: {e}", "ERROR")
            return False

    def _observer_location(self) -> EarthLocation:
        """Build the same observer location used elsewhere in ALMITA."""
        observer = self.observer_config["observer"]
        return EarthLocation(
            lat=float(observer["latitude_deg"]) * u.deg,
            lon=float(observer["longitude_deg"]) * u.deg,
            height=float(observer.get("elevation_m", 0.0)) * u.m,
        )

    @staticmethod
    def _wrapped_hour_angle(lst_hours: float, ra_hours: float) -> float:
        return (lst_hours - ra_hours + 12.0) % 24.0 - 12.0

    def hour_angle_for_point(self, point: Dict, obstime: Optional[Time] = None) -> float:
        """Return current geometric HA in [-12, 12) hours."""
        when = obstime or self.time_provider()
        location = self._observer_location()
        lst = float(when.sidereal_time("apparent", longitude=location.lon).hour)
        return self._wrapped_hour_angle(lst, float(point["target_ra_hours"]))

    @staticmethod
    def _scan_order(point: Dict) -> int:
        """Grid Generator's authoritative serpentine order is scan_order."""
        return int(point["scan_order"])

    def partition_pending_by_hour_angle(self, obstime: Optional[Time] = None) -> None:
        """Partition pending points by HA sign without changing serpentine order."""
        when = obstime or self.time_provider()
        ordered = sorted(self.observation_points, key=self._scan_order)
        for point in ordered:
            ha = self.hour_angle_for_point(point, when)
            point["_initial_ha_hours"] = ha
            point["_ha_block"] = "negative" if ha < 0 else "positive"
            point["_reclassified_due_to_ha_change"] = False
        first_block = ordered[0]["_ha_block"] if ordered else None
        location = self._observer_location()
        initial_lst = float(when.sidereal_time("apparent", longitude=location.lon).hour)
        self.meridian_partition_metadata = {
            "meridian_partition_enabled": True,
            "initial_time_utc": when.utc.isot,
            "initial_lst_hours": initial_lst,
            "first_block": first_block,
        }
        self.observation_points = ordered
        self.log(
            f"Meridian partition enabled | initial_time={when.utc.isot} | "
            f"initial_lst={initial_lst:.6f}h | first_block={first_block}",
            force=True,
        )

    def iter_meridian_partitioned_points(self):
        """Yield targets blockwise, reclassifying a target if its HA sign changes."""
        queues = {"negative": [], "positive": []}
        for point in self.observation_points:
            queues[point["_ha_block"]].append(point)
        first = self.meridian_partition_metadata.get("first_block")
        active = first or "negative"
        other = "positive" if active == "negative" else "negative"
        while queues[active] or queues[other]:
            while queues[active]:
                point = queues[active].pop(0)
                ha = self.hour_angle_for_point(point)
                actual = "negative" if ha < 0 else "positive"
                if actual != active:
                    point["_ha_block"] = actual
                    point["_reclassified_due_to_ha_change"] = True
                    queues[actual].append(point)
                    queues[actual].sort(key=self._scan_order)
                    self.log(
                        f"Point #{point['point_number']} changed HA sign; moved to {actual} block",
                        force=True,
                    )
                    continue
                point["_ha_at_selection"] = ha
                yield point
            active, other = other, active

    def persist_selection_metadata(self, point: Dict, actual_capture_order: int) -> None:
        """Persist actual order and HA selection metadata without altering scan_order."""
        new_fields = ["actual_capture_order", "ha_at_selection", "ha_block", "reclassified_due_to_ha_change"]
        with open(self.csv_path, "r", newline="") as csvfile:
            reader = csv.DictReader(csvfile); fieldnames = list(reader.fieldnames or []); rows = list(reader)
        for field in new_fields:
            if field not in fieldnames: fieldnames.append(field)
        for row in rows:
            if int(row["point_number"]) == int(point["point_number"]):
                row.update({"actual_capture_order": str(actual_capture_order), "ha_at_selection": f"{float(point['_ha_at_selection']):.9f}", "ha_block": point["_ha_block"], "reclassified_due_to_ha_change": str(bool(point["_reclassified_due_to_ha_change"])).lower()})
                break
        with open(self.csv_path, "w", newline="") as csvfile:
            writer=csv.DictWriter(csvfile,fieldnames=fieldnames);writer.writeheader();writer.writerows(rows)

    def persist_runtime_metadata(self, point: Dict, values: Dict) -> None:
        """Add compatible per-point runtime fields while preserving capture_status."""
        with open(self.csv_path, "r", newline="") as csvfile:
            reader=csv.DictReader(csvfile);fieldnames=list(reader.fieldnames or []);rows=list(reader)
        for field in values:
            if field not in fieldnames:fieldnames.append(field)
        for row in rows:
            if int(row["point_number"])==int(point["point_number"]):
                row.update({key:str(value).lower() if isinstance(value,bool) else str(value) for key,value in values.items()});break
        with open(self.csv_path,"w",newline="") as csvfile:
            writer=csv.DictWriter(csvfile,fieldnames=fieldnames);writer.writeheader();writer.writerows(rows)

    def visibility_for_point(self, point: Dict, obstime: Optional[Time] = None) -> Dict:
        """Calculate target Alt/Az/HA at the actual selection time."""
        when=obstime or self.time_provider();location=self._observer_location()
        target=SkyCoord(ra=float(point["target_ra_hours"])*u.hourangle,
                        dec=float(point["target_dec_degrees"])*u.deg,frame=ICRS())
        local=target.transform_to(AltAz(obstime=when,location=location))
        return {"altitude_deg_at_goto":float(local.alt.deg),"azimuth_deg_at_goto":float(local.az.deg),
                "ha_hours_at_goto":self.hour_angle_for_point(point,when),"visibility_checked_at":when.utc.isot,
                "min_altitude_deg":self.min_altitude_deg}

    def iter_runtime_visible_points(self):
        """Yield each currently visible point once; deferred targets remain planned."""
        self.visibility_deferred_count=0
        for point in self.iter_meridian_partitioned_points():
            visibility=self.visibility_for_point(point);point.update({f"_{k}":v for k,v in visibility.items()})
            if visibility["altitude_deg_at_goto"] < self.min_altitude_deg:
                self.visibility_deferred_count+=1
                self.persist_runtime_metadata(point,{"visibility_deferred":True,"visibility_deferred_at":visibility["visibility_checked_at"],"altitude_deg":f"{visibility['altitude_deg_at_goto']:.9f}","min_altitude_deg":self.min_altitude_deg})
                self.log(f"Point #{point['point_number']} deferred_visibility: altitude={visibility['altitude_deg_at_goto']:.3f}° < {self.min_altitude_deg:.3f}°",force=True)
                continue
            self.persist_runtime_metadata(point,{**visibility,"visibility_deferred":False})
            yield point

    async def confirm_tracking_on(self) -> tuple[bool,str,bool]:
        """Ensure and verify tracking ON using only the controller's public API."""
        state=await self.telescope.get_tracking_state(timeout=1.0);requested=False
        if state=="alert":return False,state,requested
        if state=="on":return True,state,requested
        if state!="on":
            requested=True
            if not await self.telescope.set_tracking(True):return False,state,requested
        confirmed=await self.telescope.wait_tracking_state(expected_on=True,timeout=self.tracking_timeout)
        if not confirmed:
            state=await self.telescope.get_tracking_state(timeout=1.0)
            return False,state,requested
        return True,"on",requested

    async def read_onstep_status(self, *, retry_unknown: bool = False) -> Dict:
        """Wait for a hardware-polled OnStep status, optionally retrying unknown once."""
        status = await self.telescope.wait_onstep_status_update(timeout=self.tracking_timeout)
        if retry_unknown and status.get("state") == "unknown":
            await asyncio.sleep(0.1)
            status = await self.telescope.wait_onstep_status_update(timeout=self.tracking_timeout)
        if not status.get("hardware_fresh") and status.get("reason") == "timeout":
            status = dict(status)
            status["reason"] = "onstep_status_hardware_timeout"
        return status

    def persist_onstep_status(self, point: Dict, phase: str, status: Dict) -> None:
        """Persist the stable OnStep status fields needed for causal diagnosis."""
        values = {
            f"onstep_state_{phase}": status.get("state", "unknown"),
            f"onstep_message_{phase}": status.get("message") or "",
            f"onstep_received_at_{phase}": status.get("received_at") or "",
            f"onstep_fresh_{phase}": bool(status.get("fresh", False)),
            f"onstep_{phase}_hardware_fresh": bool(status.get("hardware_fresh", False)),
            f"onstep_source_{phase}": status.get("source") or "",
            f"onstep_{phase}_update_seq": status.get("update_seq") if status.get("update_seq") is not None else "",
            f"onstep_reason_{phase}": status.get("reason") or "",
        }
        self.persist_runtime_metadata(point, values)
        point[f"_onstep_{phase}"] = dict(status)
        self.log(
            f"OnStep {phase}: state={values[f'onstep_state_{phase}']} | "
            f"message={values[f'onstep_message_{phase}'] or '<empty>'} | "
            f"received_at={values[f'onstep_received_at_{phase}'] or '<none>'} | "
            f"hardware_fresh={values[f'onstep_{phase}_hardware_fresh']} | "
            f"source={values[f'onstep_source_{phase}'] or '<none>'} | "
            f"update_seq={values[f'onstep_{phase}_update_seq'] or '<none>'}",
            force=True,
        )

    @staticmethod
    def onstep_status_allows_operation(status: Dict) -> bool:
        """Only a hardware-fresh, explicitly healthy status permits operation."""
        return (bool(status.get("hardware_fresh"))
                and status.get("state") == "healthy"
                and status.get("is_error") is False)

    async def ensure_tracking_off(self) -> bool:
        """Best-effort final tracking shutdown; never masks an earlier failure."""
        if self.telescope is None:return True
        try:
            command_ok=await self.telescope.set_tracking(False)
            confirmed=command_ok and await self.telescope.wait_tracking_state(expected_on=False,timeout=self.tracking_timeout)
            if not confirmed:self.log("Tracking OFF could not be confirmed", "WARNING", force=True)
            return bool(confirmed)
        except Exception as exc:
            self.log(f"Tracking OFF best-effort failed: {type(exc).__name__}: {exc}","WARNING",force=True);return False

    def _capture_output_root(self) -> Path:
        base_dir = self.csv_path.parent.parent if self.csv_path.parent.name == 'data' else self.csv_path.parent
        return base_dir / 'data' / 'iq'

    async def _read_indi_preflight_properties(self) -> Dict[str, str]:
        """Read optional INDI safety properties without changing mount state."""
        queries = [
            f"{self.device_name}.EQUATORIAL_EOD_COORD._STATE",
            f"{self.device_name}.TELESCOPE_MOTION_NS.MOTION_NORTH",
            f"{self.device_name}.TELESCOPE_MOTION_NS.MOTION_SOUTH",
            f"{self.device_name}.TELESCOPE_MOTION_WE.MOTION_WEST",
            f"{self.device_name}.TELESCOPE_MOTION_WE.MOTION_EAST",
            f"{self.device_name}.TELESCOPE_HOME._STATE",
            f"{self.device_name}.TELESCOPE_HOME.GO",
        ]
        process = await asyncio.create_subprocess_exec(
            "indi_getprop", "-h", self.host, "-p", str(self.port), *queries,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        properties = {}
        for line in stdout.decode(errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                properties[key.split(f"{self.device_name}.", 1)[-1]] = value
        return properties

    async def run_preflight(self, capture_time: float, *, disk_safety_factor: float = 1.25,
                            sdr_factory=SDRCapture, h5py_loader=None, disk_usage=shutil.disk_usage,
                            indi_property_reader=None) -> Dict:
        """Validate session prerequisites without GOTO, tracking, or IQ capture."""
        checks = []
        def record(name, status, detail):
            checks.append({"name": name, "status": status, "detail": detail})

        required_fields = {
            "point_number", "scan_order", "target_ra_hours", "target_dec_degrees",
            "capture_status", "start_time", "end_time", "duration", "error_message",
            "data_filename", "session_name",
        }
        planned_points = []
        try:
            with open(self.csv_path, "r", newline="") as handle:
                reader = csv.DictReader(handle); fields = set(reader.fieldnames or []); rows = list(reader)
            missing = sorted(required_fields - fields)
            if missing: record("Grid", "FAIL", f"missing required fields: {missing}")
            else:
                planned_points = [row for row in rows if row.get("capture_status") == "planned"]
                valid_targets = bool(planned_points)
                try:
                    for row in planned_points:
                        ra = float(row["target_ra_hours"]); dec = float(row["target_dec_degrees"]); order = int(row["scan_order"])
                        if not all(math.isfinite(v) for v in (ra, dec)) or not 0 <= ra < 24 or not -90 <= dec <= 90 or order < 1:
                            valid_targets = False; break
                except (TypeError, ValueError): valid_targets = False
                record("Grid", "PASS" if valid_targets else "FAIL", f"{len(planned_points)} planned points with valid coordinates/order={valid_targets}")
        except Exception as exc:
            record("Grid", "FAIL", f"not readable: {type(exc).__name__}: {exc}")

        observer = self.observer_config.get("observer", {}) if isinstance(self.observer_config, dict) else {}
        observer_ok = self.observer_config_preexisting and self.observer_config_valid
        try:
            observer_values = [float(observer[k]) for k in ("latitude_deg", "longitude_deg")]
            observer_ok = observer_ok and all(math.isfinite(v) for v in observer_values)
        except (KeyError, TypeError, ValueError): observer_ok = False
        record("Observer config", "PASS" if observer_ok else "FAIL", str(self.observer_config_path))

        numeric_ok = capture_time > 0 and self.sdr_sample_rate > 0 and self.sdr_freq > 0 and 0 < self.sdr_port < 65536 and disk_safety_factor >= 1 and -90 <= self.min_altitude_deg <= 90 and self.tracking_timeout > 0
        record("Numeric parameters", "PASS" if numeric_ok else "FAIL", f"duration={capture_time}, rate={self.sdr_sample_rate}, frequency={self.sdr_freq}, margin={disk_safety_factor}, min_altitude={self.min_altitude_deg}, tracking_timeout={self.tracking_timeout}")

        output_root = self._capture_output_root(); hdf5_ok = False
        try:
            output_root.mkdir(parents=True, exist_ok=True)
            h5py = (h5py_loader or (lambda: importlib.import_module("h5py")))()
            descriptor, temp_name = tempfile.mkstemp(prefix=".almita-preflight-", suffix=".h5", dir=output_root)
            os.close(descriptor)
            try:
                with h5py.File(temp_name, "w") as handle: handle.create_dataset("probe", data=[0, 1])
            finally:
                Path(temp_name).unlink(missing_ok=True)
            hdf5_ok = True; record("HDF5/output", "PASS", f"temporary HDF5 created and removed in {output_root}")
        except Exception as exc:
            record("HDF5/output", "FAIL", f"{type(exc).__name__}: {exc}")

        try:
            control_dir = Path(self.session_manager.control_dir); control_dir.mkdir(parents=True, exist_ok=True)
            descriptor, session_probe = tempfile.mkstemp(prefix=".almita-session-preflight-", suffix=".csv", dir=control_dir)
            os.write(descriptor, b"preflight,ok\n"); os.close(descriptor); Path(session_probe).unlink()
            record("Session persistence", "PASS", f"write/delete succeeded in {control_dir}")
        except Exception as exc:
            record("Session persistence", "FAIL", f"{type(exc).__name__}: {exc}")

        bytes_per_complex_sample = 2  # canonical interleaved iq_data: uint8 I + uint8 Q
        estimated = int(len(planned_points) * capture_time * self.sdr_sample_rate * bytes_per_complex_sample)
        required = int(math.ceil(estimated * disk_safety_factor))
        try:
            free = int(disk_usage(output_root).free)
            record("Disk space", "PASS" if free >= required else "FAIL", f"free_bytes={free}, estimated_session_bytes={estimated}, safety_required_bytes={required}")
        except Exception as exc:
            record("Disk space", "FAIL", f"{type(exc).__name__}: {exc}")

        if self.sdr_mode == "usb":
            record("SDR USB", "FAIL", "USB capture path is not validated for field use")
        elif self.sdr_mode != "network":
            record("SDR network", "FAIL", f"unsupported mode {self.sdr_mode}")
        else:
            probe = None
            try:
                probe = sdr_factory(mode="network", host=self.sdr_host, port=self.sdr_port, verbose=False)
                await probe.connect(); await probe.configure(center_freq=self.sdr_freq, sample_rate=self.sdr_sample_rate, gain="auto")
                record("SDR network", "PASS", f"configured {self.sdr_host}:{self.sdr_port} at {self.sdr_freq} Hz/{self.sdr_sample_rate} Sps, gain=auto")
            except Exception as exc:
                record("SDR network", "FAIL", f"{type(exc).__name__}: {exc}")
            finally:
                if probe is not None:
                    try: await probe.close()
                    except Exception: pass

        if self.telescope is None:
            record("INDI/mount", "FAIL", "controller is not connected")
        else:
            try:
                ra, dec = await self.telescope.get_coordinates(force_refresh=True)
                if ra is None or dec is None or not math.isfinite(float(ra)) or not math.isfinite(float(dec)) or not -90 <= float(dec) <= 90:
                    raise ValueError(f"invalid coordinates RA={ra}, DEC={dec}")
                reader = indi_property_reader or self._read_indi_preflight_properties
                try:
                    properties = await reader()
                except Exception as exc:
                    properties = {}; record("INDI properties", "WARN", f"optional state properties unavailable: {type(exc).__name__}: {exc}")
                eod = properties.get("EQUATORIAL_EOD_COORD._STATE")
                active = [key for key in ("TELESCOPE_MOTION_NS.MOTION_NORTH", "TELESCOPE_MOTION_NS.MOTION_SOUTH", "TELESCOPE_MOTION_WE.MOTION_WEST", "TELESCOPE_MOTION_WE.MOTION_EAST") if properties.get(key) == "On"]
                if eod == "Alert" or active: raise RuntimeError(f"EOD={eod}, active_motion={active}")
                home = f"HOME={properties.get('TELESCOPE_HOME._STATE')}/GO={properties.get('TELESCOPE_HOME.GO')} (informational only)"
                record("INDI/mount", "PASS", f"valid coordinates RA={float(ra):.6f}, DEC={float(dec):.6f}; {home}")
            except Exception as exc:
                record("INDI/mount", "FAIL", f"{type(exc).__name__}: {exc}")

        errors = [c["detail"] for c in checks if c["status"] == "FAIL"]
        warnings = [c["detail"] for c in checks if c["status"] == "WARN"]
        return {"success": not errors, "checks": checks, "warnings": warnings, "errors": errors,
                "estimated_session_bytes": estimated, "safety_required_bytes": required}

    def log_preflight(self, report: Dict) -> None:
        for check in report["checks"]:
            self.log(f"[{check['status']}] {check['name']}: {check['detail']}", force=True)
        self.log(f"PREFLIGHT: {'PASS' if report['success'] else 'FAIL'}", force=True)
        if not report["success"]: self.log("No hardware movement executed.", force=True)
    
    def update_point_status(self, point_number: int, status: str,
                           start_time: Optional[str] = None,
                           end_time: Optional[str] = None,
                           duration: Optional[float] = None,
                           error_msg: Optional[str] = None,
                           error_code: Optional[str] = None,
                           error_detail: Optional[str] = None,
                           failed_at: Optional[str] = None):
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
                fieldnames = list(reader.fieldnames or [])
                all_rows = list(reader)
            for field in ("failed_at", "error_code", "error_detail"):
                if field not in fieldnames:
                    fieldnames.append(field)
            
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
                    if failed_at:
                        row['failed_at'] = failed_at
                    if error_code:
                        row['error_code'] = error_code
                    if error_detail:
                        row['error_detail'] = error_detail
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

        self._live_timing_csv_path = (
            self.csv_path.parent / f"capture_timing_{self.session_id}.csv"
        )
        self._live_session_clock = time.perf_counter()
        self._live_timing_rows = []
        live_totals = {key: 0.0 for key in (
            "command_delay", "physical_motion", "motion_tail", "goto", "guards",
            "tracking", "settle", "flush", "capture", "disk_hdf5", "persist",
            "inter_point", "other", "point_total",
        )}

        if not self.verbose:
            self.log("", force=True)

        successful = 0
        failed = 0
        safety_paused = False

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
            self.sdr.compact_console = self.compact_console
            
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
            await self.ensure_tracking_off()
            return False

        try:
            actual_session_count = 0
            timing_totals = {key: 0.0 for key in (
                "slew", "settle", "flush", "capture", "hdf5",
                "guards", "tracking", "persist", "other", "wall")}
            for idx, point in enumerate(self.iter_runtime_visible_points(), start=1):
                point_clock = time.perf_counter()
                point_start = datetime.now(timezone.utc)
                point_num = int(point['point_number'])
                ra_hours = float(point['target_ra_hours'])
                dec_deg = float(point['target_dec_degrees'])
                actual_session_count += 1
                actual_capture_order = self.actual_capture_order_offset + actual_session_count
                inter_point_delay_sec = (
                    0.0 if self._live_previous_point_complete_clock is None
                    else point_clock - self._live_previous_point_complete_clock
                )
                if idx > 1:
                    previous_total = self._live_timing_rows[-1].get("point_total_sec", 0.0)
                    if not self.compact_console:
                        print(
                            f"[{self._live_utc()}] NEXT POINT START\n"
                            f"      previous_point_total={float(previous_total):.3f} s\n"
                            f"      session_wall_so_far={point_clock - self._live_session_clock:.3f} s\n"
                            f"      inter_point_delay_sec={inter_point_delay_sec:.3f} s",
                            flush=True,
                        )
                    self._live_timing_rows[-1]["next_point_start_utc"] = self._live_utc()
                    self._write_live_timing_csv()
                persist_clock = time.perf_counter()
                self.persist_selection_metadata(point, actual_capture_order)
                persist_sec = time.perf_counter() - persist_clock

                # Header for this point
                if self.verbose:
                    self.log(f"--- Point {idx}/{len(self.observation_points)} (#{point_num}) ---")
                    self.log(f"Target: RA={ra_hours:.4f}h, DEC={dec_deg:.4f} deg")
                    self.log(f"       ({point['target_ra_hms']}, {point['target_dec_dms']})")
                else:
                    self.log("="*80, force=True)
                    self.log(f"📍 POINT {idx}/{len(self.observation_points)} (#{point_num}) | RA={point['target_ra_hms']} DEC={point['target_dec_dms']}", force=True)
                    self.log("="*80, force=True)

                previous_target = getattr(self, "_timing_previous_target", None)
                if previous_target:
                    previous_coord = SkyCoord(
                        ra=previous_target[0] * u.hourangle,
                        dec=previous_target[1] * u.deg,
                    )
                    target_coord = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg)
                    distance_from_previous = float(previous_coord.separation(target_coord).deg)
                else:
                    distance_from_previous = 0.0
                live_row = {
                    "point_id": point.get("point_id", point_num),
                    "scan_order": self._scan_order(point),
                    "actual_capture_order": actual_capture_order,
                    "point_start_utc": point_start.isoformat(timespec="milliseconds"),
                    "_point_clock": point_clock,
                    "inter_point_delay_sec": inter_point_delay_sec,
                }
                print(
                    f"\nPOINT {idx:02d}/{len(self.observation_points):02d}  "
                    f"RA={ra_hours:.4f}h DEC={dec_deg:+.3f}deg  "
                    f"HA={float(point['_ha_at_selection']):+.3f}h "
                    f"ALT={float(point['_altitude_deg_at_goto']):.1f}deg "
                    f"DIST={distance_from_previous:.1f}deg",
                    flush=True,
                )
                self._live_event(live_row, "POINT START")
                self.log(
                    f"TIMING POINT {idx:02d}/20 | point_id={point.get('point_id', point_num)} | "
                    f"scan={self._scan_order(point)} | actual={actual_capture_order} | "
                    f"RA={ra_hours:.6f}h DEC={dec_deg:.6f}° | "
                    f"HA={float(point['_ha_at_selection']):+.6f}h | "
                    f"Alt={float(point['_altitude_deg_at_goto']):.3f}° "
                    f"Az={float(point['_azimuth_deg_at_goto']):.3f}° | "
                    f"distance={distance_from_previous:.3f}°",
                    force=True,
                )

                # A fresh healthy OnStep status is the final gate before motion.
                guard_clock = time.perf_counter()
                self._live_event(live_row, "PRE-GOTO GUARD START", "pre_guard_start")
                self.log("PRE-GOTO GUARD START | waiting hardware-fresh", force=True)
                onstep_pre_goto = await self.read_onstep_status(retry_unknown=True)
                pre_goto_guard_sec = time.perf_counter() - guard_clock
                self._live_event(
                    live_row, "PRE-GOTO GUARD END", "pre_guard_end",
                    [f"duration={pre_goto_guard_sec:.3f} s",
                     f"hardware_fresh={onstep_pre_goto.get('hardware_fresh')}",
                     f"update_seq={onstep_pre_goto.get('update_seq')}"],
                )
                self.log(
                    f"PRE-GOTO GUARD END | hardware_fresh={onstep_pre_goto.get('hardware_fresh')} | "
                    f"update_seq={onstep_pre_goto.get('update_seq')} | duration={pre_goto_guard_sec:.3f}s",
                    force=True,
                )
                self.persist_onstep_status(point, "pre_goto", onstep_pre_goto)
                if not self.onstep_status_allows_operation(onstep_pre_goto):
                    safety_paused = True
                    message = onstep_pre_goto.get("message") or onstep_pre_goto.get("reason") or "unverifiable"
                    self.session_manager.update_session(self.session_id, status="paused")
                    self.log(
                        f"Safety pause before GOTO: OnStep state={onstep_pre_goto.get('state')} | message={message}; target remains planned",
                        "ERROR", force=True,
                    )
                    break

                # GOTO coordinates
                if self.verbose:
                    self.log(f"GOTO coordinates...")
                else:
                    self.log(f"🔭 Step 1/3: SLEWING to coordinates", force=True)

                mount_start_ra, mount_start_dec = await self._optional_mount_coordinates()
                slew_start = datetime.now(timezone.utc)
                slew_clock = time.perf_counter()
                self.log(f">>> GOTO START | distance={distance_from_previous:.3f}°", force=True)
                original_telescope_log = getattr(self.telescope, "log", None)
                original_telescope_log_verbose = getattr(self.telescope, "log_verbose", None)
                if original_telescope_log is not None:
                    def timeline_telescope_log(message, level="INFO", *args, **kwargs):
                        result = original_telescope_log(message, level, *args, **kwargs)
                        if ("GOTO baseline_seq=" in str(message)
                                or "GOTO command_sent_seq=" in str(message)):
                            self._observe_goto_log(live_row, str(message))
                        return result
                    self.telescope.log = timeline_telescope_log
                if original_telescope_log_verbose is not None:
                    def timeline_telescope_log_verbose(message, *args, **kwargs):
                        self._observe_goto_log(live_row, str(message))
                        return original_telescope_log_verbose(message, *args, **kwargs)
                    self.telescope.log_verbose = timeline_telescope_log_verbose
                try:
                    success = await self.telescope.goto(ra_hours, dec_deg)
                finally:
                    if original_telescope_log is not None:
                        self.telescope.log = original_telescope_log
                    if original_telescope_log_verbose is not None:
                        self.telescope.log_verbose = original_telescope_log_verbose
                slew_sec = time.perf_counter() - slew_clock
                slew_end = datetime.now(timezone.utc)
                slew_time = (slew_end - slew_start).total_seconds()
                if not live_row.get("motion_start_utc"):
                    print("MOUNT MOTION START: BUSY NOT OBSERVED", flush=True)
                goto_return_clock = time.perf_counter()
                live_row["goto_total_sec"] = slew_sec
                if live_row.get("motion_start_utc"):
                    live_row["command_to_motion_start_sec"] = (
                        live_row["_motion_start_utc_clock"] - live_row["_goto_command_utc_clock"]
                    )
                if live_row.get("motion_end_utc"):
                    live_row["physical_motion_sec"] = (
                        live_row["_motion_end_utc_clock"] - live_row["_motion_start_utc_clock"]
                    )
                    live_row["motion_end_to_goto_return_sec"] = (
                        goto_return_clock - live_row["_motion_end_utc_clock"]
                    )
                command_delay = float(live_row.get("command_to_motion_start_sec") or 0.0)
                physical_motion = float(live_row.get("physical_motion_sec") or 0.0)
                motion_tail = float(live_row.get("motion_end_to_goto_return_sec") or 0.0)
                residual_goto = slew_sec - command_delay - physical_motion - motion_tail
                self._live_event(
                    live_row, "GOTO RETURN", "goto_return_utc",
                    [f"goto_total={slew_sec:.3f} s",
                     f"command_to_motion_start={command_delay:.3f} s",
                     f"physical_motion={physical_motion:.3f} s",
                     f"motion_end_to_return={motion_tail:.3f} s",
                     f"residual_goto_time={residual_goto:.3f} s"],
                )
                self.log(f"<<< GOTO END | monotonic={slew_sec:.3f}s", force=True)
                if self.compact_console:
                    print(
                        f"GOTO       OK {slew_sec:.1f}s "
                        f"(motion={physical_motion:.1f}s, wait={command_delay + motion_tail + residual_goto:.1f}s)",
                        flush=True,
                    )

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

                guard_clock = time.perf_counter()
                self._live_event(live_row, "POST-GOTO GUARD START", "post_guard_start")
                self.log("POST-GOTO GUARD START", force=True)
                onstep_post_goto = await self.read_onstep_status(retry_unknown=True)
                post_goto_guard_sec = time.perf_counter() - guard_clock
                self._live_event(
                    live_row, "POST-GOTO GUARD END", "post_guard_end",
                    [f"duration={post_goto_guard_sec:.3f} s",
                     f"hardware_fresh={onstep_post_goto.get('hardware_fresh')}",
                     f"update_seq={onstep_post_goto.get('update_seq')}"],
                )
                self.log(
                    f"POST-GOTO GUARD END | hardware_fresh={onstep_post_goto.get('hardware_fresh')} | "
                    f"update_seq={onstep_post_goto.get('update_seq')} | duration={post_goto_guard_sec:.3f}s",
                    force=True,
                )
                self.persist_onstep_status(point, "post_goto", onstep_post_goto)
                if not self.onstep_status_allows_operation(onstep_post_goto):
                    safety_paused = True
                    message = onstep_post_goto.get("message") or onstep_post_goto.get("reason") or "unverifiable"
                    self.update_point_status(point_num, "failed", error_msg=f"OnStep post-GOTO {onstep_post_goto.get('state')}: {message}")
                    failed += 1
                    self.session_manager.update_session(self.session_id, points_failed=failed, status="paused")
                    self.log(
                        f"Safety pause after GOTO: OnStep state={onstep_post_goto.get('state')} | message={message}; tracking will not be attempted",
                        "ERROR", force=True,
                    )
                    break

                # Enable and verify real tracking state before scientific settle/capture.
                if self.verbose:self.log(f"Enabling and confirming tracking...")
                tracking_request_at = datetime.now(timezone.utc)
                tracking_clock = time.perf_counter()
                self._live_event(live_row, "TRACKING START", "tracking_start")
                self.log("TRACKING START | ON REQUEST", force=True)
                tracking_confirmed,tracking_state,tracking_requested=await self.confirm_tracking_on()
                tracking_enable_sec = time.perf_counter() - tracking_clock
                self._live_event(
                    live_row, "TRACKING ON CONFIRMED" if tracking_confirmed else "TRACKING NOT CONFIRMED",
                    "tracking_confirmed", [f"duration={tracking_enable_sec:.3f} s"],
                )
                self.log(
                    f"TRACKING {'CONFIRMED' if tracking_confirmed else 'NOT CONFIRMED'} | "
                    f"state={tracking_state} | duration={tracking_enable_sec:.3f}s",
                    force=True,
                )
                if self.compact_console:
                    print(
                        f"TRACK      {'OK' if tracking_confirmed else 'FAIL'} {tracking_enable_sec:.1f}s",
                        flush=True,
                    )
                tracking_result_at = datetime.now(timezone.utc)
                tracking_confirmed_at = tracking_result_at if tracking_confirmed else None
                guard_clock = time.perf_counter()
                self._live_event(live_row, "POST-TRACK GUARD START", "post_tracking_guard_start")
                self.log("POST-TRACKING GUARD START", force=True)
                onstep_post_tracking = await self.read_onstep_status()
                post_tracking_guard_sec = time.perf_counter() - guard_clock
                self._live_event(
                    live_row, "POST-TRACK GUARD END", "post_tracking_guard_end",
                    [f"duration={post_tracking_guard_sec:.3f} s",
                     f"hardware_fresh={onstep_post_tracking.get('hardware_fresh')}",
                     f"update_seq={onstep_post_tracking.get('update_seq')}"],
                )
                self.log(
                    f"POST-TRACKING GUARD END | hardware_fresh={onstep_post_tracking.get('hardware_fresh')} | "
                    f"update_seq={onstep_post_tracking.get('update_seq')} | duration={post_tracking_guard_sec:.3f}s",
                    force=True,
                )
                self.persist_onstep_status(point, "post_tracking", onstep_post_tracking)
                self.persist_runtime_metadata(point,{
                    "tracking_requested":tracking_requested,
                    "tracking_confirmed":tracking_confirmed,
                    "tracking_state_at_capture":tracking_state,
                    "goto_command_started_at":slew_start.isoformat(),
                    "goto_completed_at":slew_end.isoformat(),
                    "tracking_request_at":tracking_request_at.isoformat(),
                    "tracking_confirmation_at":tracking_result_at.isoformat() if tracking_confirmed else "",
                    "tracking_failure_at":tracking_result_at.isoformat() if not tracking_confirmed else "",
                })
                if not tracking_confirmed:
                    onstep_detail = onstep_post_tracking.get("message") or onstep_post_tracking.get("reason") or "unverifiable"
                    self.log(f"Tracking failed or was not confirmed (state={tracking_state}, OnStep={onstep_post_tracking.get('state')}: {onstep_detail}); point #{point_num} will not be captured","ERROR",force=True)
                    self.update_point_status(point_num,'failed',error_msg=f"tracking not confirmed: {tracking_state}; OnStep {onstep_post_tracking.get('state')}: {onstep_detail}")
                    failed+=1
                    safety_paused = True
                    self.session_manager.update_session(
                        self.session_id, points_failed=failed, status="paused"
                    )
                    self.log(
                        "Safety pause: tracking was not confirmed after GOTO; "
                        "no additional GOTO will be attempted",
                        "ERROR", force=True,
                    )
                    break

                if not self.onstep_status_allows_operation(onstep_post_tracking):
                    safety_paused = True
                    message = onstep_post_tracking.get("message") or onstep_post_tracking.get("reason") or "unverifiable"
                    self.update_point_status(point_num, "failed", error_msg=f"OnStep post-tracking {onstep_post_tracking.get('state')}: {message}")
                    failed += 1
                    self.session_manager.update_session(self.session_id, points_failed=failed, status="paused")
                    self.log(
                        f"Safety pause after tracking: OnStep state={onstep_post_tracking.get('state')} | message={message}",
                        "ERROR", force=True,
                    )
                    break

                mount_capture_ra, mount_capture_dec = await self._optional_mount_coordinates()

                # Wait for settle
                if self.verbose:
                    self.log(f"Settling for {settle_time}s...")
                else:
                    self.log(f"⏱️  Step 2/3: SETTLING (stabilizing telescope)", force=True)
                    self.log(f"   Settling time: {settle_time}s", force=True)
                
                settle_start = datetime.now(timezone.utc)
                settle_clock = time.perf_counter()
                self._live_event(live_row, "SETTLE START", "settle_start",
                                 [f"requested={settle_time:.3f} s"])
                self.log(f"SETTLE START ({settle_time:.3f}s)", force=True)
                settle_task = asyncio.create_task(asyncio.sleep(settle_time))
                while not settle_task.done():
                    elapsed_settle = time.perf_counter() - settle_clock
                    self._console_progress(
                        "SETTLE", elapsed_settle / settle_time if settle_time > 0 else 1.0,
                        f"elapsed={min(elapsed_settle, settle_time):.1f}s/{settle_time:.1f}s",
                    )
                    try:
                        await asyncio.wait_for(asyncio.shield(settle_task), timeout=0.1)
                    except asyncio.TimeoutError:
                        pass
                await settle_task
                self._console_progress("SETTLE", 1.0,
                                       f"elapsed={settle_time:.1f}s/{settle_time:.1f}s",
                                       force=True)
                self._console_progress_end()
                settle_sec = time.perf_counter() - settle_clock
                if self.compact_console:
                    print(f"SETTLE     OK {settle_sec:.1f}s", flush=True)
                settle_end = datetime.now(timezone.utc)
                actual_settle = (settle_end - settle_start).total_seconds()
                self._live_event(live_row, "SETTLE END", "settle_end",
                                 [f"actual={settle_sec:.3f} s"])
                self.log(f"SETTLE END | requested={settle_time:.3f}s | actual={settle_sec:.3f}s", force=True)
                
                if not self.verbose:
                    self.log(f"   ✓ Settling completed ({actual_settle:.1f}s)", force=True)
                    self.log("="*80, force=True)

                # FLUSH SDR BUFFER AFTER SETTLE - discard data accumulated during slew AND settle
                # This ensures we only capture data from the final stable position
                if self.sdr and hasattr(self.sdr, 'flush_buffer'):
                    flush_clock = time.perf_counter()
                    self._live_event(live_row, "FLUSH START", "flush_start")
                    self.log(">>> SDR FLUSH START", force=True)
                    previous_flush_callback = getattr(self.sdr, "flush_progress_callback", None)
                    def flush_progress_callback(total_bytes, elapsed, max_duration):
                        self._console_progress(
                            "FLUSH", elapsed / max_duration,
                            f"elapsed={elapsed:.1f}s bytes={total_bytes:,}",
                        )
                    self.sdr.flush_progress_callback = flush_progress_callback
                    try:
                        flushed_bytes = await self.sdr.flush_buffer()
                    finally:
                        self.sdr.flush_progress_callback = previous_flush_callback
                    flush_sec = time.perf_counter() - flush_clock
                    self._console_progress(
                        "FLUSH", 1.0,
                        f"{flushed_bytes // 2:,} samples  {flush_sec:.1f}s",
                        force=True,
                    )
                    self._console_progress_end()
                    print(
                        f"FLUSH      OK {flush_sec:.1f}s ({flushed_bytes // 2:,} samples)",
                        flush=True,
                    )
                    self._live_event(
                        live_row, "FLUSH END", "flush_end",
                        [f"flush_duration={flush_sec:.3f} s",
                         f"samples_flushed={flushed_bytes // 2:,}",
                         f"bytes_flushed={flushed_bytes:,}"],
                    )
                    self.log(
                        f"<<< SDR FLUSH END | samples={flushed_bytes//2:,} | "
                        f"bytes={flushed_bytes:,} | duration={flush_sec:.3f}s",
                        force=True,
                    )
                    if self.verbose and flushed_bytes > 0:
                        self.log(f"  Flushed {flushed_bytes//2:,} samples from SDR buffer")

                # Start capture
                capture_start = datetime.now(timezone.utc)
                start_time_iso = capture_start.isoformat()

                persist_clock = time.perf_counter()
                self.update_point_status(point_num, 'capturing', start_time=start_time_iso)
                persist_sec += time.perf_counter() - persist_clock

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
                        'point_id': point.get('point_id', point_num),
                        'scan_order': self._scan_order(point),
                        'planned_scan_order': self._scan_order(point),
                        'actual_capture_order': actual_capture_order,
                        'ha_at_selection': float(point['_ha_at_selection']),
                        'ha_block': point['_ha_block'],
                        'reclassified_due_to_ha_change': bool(point['_reclassified_due_to_ha_change']),
                        'meridian_partition_enabled': bool(self.meridian_partition_metadata.get('meridian_partition_enabled')),
                        'altitude_deg_at_goto': float(point['_altitude_deg_at_goto']),
                        'azimuth_deg_at_goto': float(point['_azimuth_deg_at_goto']),
                        'ha_hours_at_goto': float(point['_ha_hours_at_goto']),
                        'visibility_checked_at': point['_visibility_checked_at'],
                        'min_altitude_deg': self.min_altitude_deg,
                        'tracking_requested': tracking_requested,
                        'tracking_confirmed': tracking_confirmed,
                        'tracking_state_at_capture': tracking_state,
                        'onstep_state_pre_goto': onstep_pre_goto.get('state'),
                        'onstep_message_pre_goto': onstep_pre_goto.get('message'),
                        'onstep_received_at_pre_goto': onstep_pre_goto.get('received_at'),
                        'onstep_fresh_pre_goto': bool(onstep_pre_goto.get('fresh')),
                        'onstep_state_post_goto': onstep_post_goto.get('state'),
                        'onstep_message_post_goto': onstep_post_goto.get('message'),
                        'onstep_received_at_post_goto': onstep_post_goto.get('received_at'),
                        'onstep_fresh_post_goto': bool(onstep_post_goto.get('fresh')),
                        'onstep_state_post_tracking': onstep_post_tracking.get('state'),
                        'onstep_message_post_tracking': onstep_post_tracking.get('message'),
                        'onstep_received_at_post_tracking': onstep_post_tracking.get('received_at'),
                        'onstep_fresh_post_tracking': bool(onstep_post_tracking.get('fresh')),

                        # Grid provenance (owned by generator metadata/CSV).
                        'beam_fwhm_deg': self.grid_metadata.get('beam_fwhm_deg'),
                        'beam_sampling_fraction': self.grid_metadata.get('beam_sampling_fraction'),
                        'nominal_spacing_deg': point.get('nominal_spacing_deg') or self.grid_metadata.get('nominal_spacing_deg'),

                        # Actual mount coordinates; absent when an optional read failed.
                        'mount_start_ra_hours': mount_start_ra,
                        'mount_start_dec_deg': mount_start_dec,
                        'mount_capture_ra_hours': mount_capture_ra,
                        'mount_capture_dec_deg': mount_capture_dec,

                        # Event timestamps and measured/requested durations.
                        'goto_started_at': slew_start.isoformat(),
                        'goto_completed_at': slew_end.isoformat(),
                        'goto_command_started_at': slew_start.isoformat(),
                        'tracking_request_at': tracking_request_at.isoformat(),
                        'tracking_confirmed_at': tracking_confirmed_at.isoformat() if tracking_confirmed_at else None,
                        'tracking_confirmation_at': tracking_result_at.isoformat() if tracking_confirmed else None,
                        'tracking_failure_at': tracking_result_at.isoformat() if not tracking_confirmed else None,
                        'settle_started_at': settle_start.isoformat(),
                        'settle_duration_sec': actual_settle,
                        'requested_capture_duration_sec': capture_time,
                        'capture_started_at': start_time_iso,
                        
                        # Observation parameters
                        'settle_time': settle_time,
                        'slew_time': slew_time,
                        'tracking': True,
                        'telescope_name': self.telescope.device_name if hasattr(self.telescope, 'device_name') else 'Unknown',
                        
                        # SDR configuration
                        'center_freq': self.sdr_freq,
                        'center_frequency_hz': self.sdr_freq,
                        'sample_rate_hz': self.sdr_sample_rate,
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
                    capture_wall_clock = time.perf_counter()
                    self._live_event(
                        live_row, "CAPTURE START", "capture_start",
                        [f"requested_duration={capture_time:.3f} s",
                         f"requested_samples={int(capture_time * self.sdr_sample_rate):,}",
                         "implementation=SDRCapture.capture with observational timing callback"],
                    )
                    live_row["capture_disk_combined"] = False
                    self.log(
                        f">>> SDR CAPTURE START | requested_duration={capture_time:.3f}s | "
                        f"requested_samples={int(capture_time * self.sdr_sample_rate):,}",
                        force=True,
                    )
                    previous_timing_callback = getattr(self.sdr, "timing_callback", None)

                    def sdr_timing_callback(event, details):
                        if event == "capture_end":
                            if self.compact_console:
                                print(
                                    "\r\033[2K" + (" " * 160) + "\r\033[2K"
                                    f"CAPTURE    OK {float(details['duration']):.1f}s "
                                    f"({int(details['samples_received']):,} samples)",
                                    flush=True,
                                )
                            self._live_event(
                                live_row, "CAPTURE END", "capture_end",
                                [f"duration={float(details['duration']):.3f} s",
                                 f"samples_received={int(details['samples_received']):,}"],
                            )
                        elif event == "disk_write_start":
                            self._live_event(
                                live_row, "DISK WRITE START", "disk_write_start",
                                [f"file={details['file']}"],
                            )
                        elif event == "disk_write_end":
                            if self.compact_console:
                                print(
                                    f"WRITE      OK {float(details['duration']):.1f}s",
                                    flush=True,
                                )
                            self._live_event(
                                live_row, "DISK WRITE END", "disk_write_end",
                                [f"duration={float(details['duration']):.3f} s",
                                 f"file={details['file']}"],
                            )

                    self.sdr.timing_callback = sdr_timing_callback
                    try:
                        sdr_metrics = await self.sdr.capture(
                            duration=capture_time,
                            output_file=str(data_path),
                            sample_rate=self.sdr_sample_rate,
                            metadata=capture_metadata
                        )
                    finally:
                        self.sdr.timing_callback = previous_timing_callback
                    capture_hdf_wall_sec = time.perf_counter() - capture_wall_clock
                    capture_sec = float(sdr_metrics.capture_time)
                    hdf5_sec = float(sdr_metrics.disk_write_time)
                    actual_samples = getattr(sdr_metrics, 'total_samples', 0)
                    effective_rate = actual_samples / capture_sec if capture_sec > 0 else 0.0
                    combined_end = time.perf_counter()
                    live_row["validation_start"] = "N/D (inside SDRCapture.capture)"
                    live_row["validation_end"] = "N/D (inside SDRCapture.capture)"
                    live_row["rename_start"] = "N/D (inside SDRCapture.capture)"
                    live_row["rename_end"] = "N/D (inside SDRCapture.capture)"
                    self.log(
                        f"<<< SDR CAPTURE END | capture_wall={capture_sec:.3f}s | "
                        f"samples_received={actual_samples:,} | effective_rate={effective_rate:.1f}Sps",
                        force=True,
                    )
                    self.log(
                        f"HDF5 FINALIZED | write_time={hdf5_sec:.3f}s | valid=True | "
                        f"samples={actual_samples:,} | file={data_path} | combined_wall={capture_hdf_wall_sec:.3f}s",
                        force=True,
                    )
                    
                    if self.verbose:
                        self.log(f"SDR Metrics:")
                        self.log(f"  Capture time: {sdr_metrics.capture_time*1000:.2f}ms")
                        self.log(f"  Write time: {sdr_metrics.disk_write_time*1000:.2f}ms")
                        self.log(f"  Throughput: {sdr_metrics.throughput_mbps:.2f} MB/s")
                    
                except Exception as e:
                    self.log(f"SDR capture error: {e}", "ERROR", force=True)
                    failed_at = datetime.now(timezone.utc)
                    error_code = getattr(e, "code", type(e).__name__)
                    self.update_point_status(
                        point_num,
                        "failed",
                        start_time=start_time_iso,
                        end_time=failed_at.isoformat(),
                        duration=(failed_at - capture_start).total_seconds(),
                        error_msg=str(e),
                        error_code=error_code,
                        error_detail=str(e),
                        failed_at=failed_at.isoformat(),
                    )
                    raise

                # End capture
                capture_end = datetime.now(timezone.utc)
                end_time_iso = capture_end.isoformat()
                duration = (capture_end - capture_start).total_seconds()

                persist_clock = time.perf_counter()
                self._live_event(live_row, "PERSIST START", "persist_start")
                self.log("CSV/SESSION PERSIST START", force=True)
                self.update_point_status(point_num, 'success',
                                       start_time=start_time_iso,
                                       end_time=end_time_iso,
                                       duration=duration)
                persist_sec += time.perf_counter() - persist_clock
                self._live_event(
                    live_row, "PERSIST END", "persist_end",
                    [f"persist_duration={persist_sec:.3f} s", "capture_status=success"],
                )
                self.log(f"CSV/SESSION PERSIST END | cumulative_persist={persist_sec:.3f}s", force=True)

                # Calculate total time for this point
                point_end = datetime.now(timezone.utc)
                total_point_time = time.perf_counter() - point_clock
                guards_sec = pre_goto_guard_sec + post_goto_guard_sec + post_tracking_guard_sec
                known_sec = (slew_sec + settle_sec + flush_sec + capture_sec + hdf5_sec
                             + guards_sec + tracking_enable_sec + persist_sec)
                other_sec = total_point_time - known_sec
                live_row.update({
                    "pre_guard_sec": pre_goto_guard_sec,
                    "post_guard_sec": post_goto_guard_sec,
                    "tracking_sec": tracking_enable_sec,
                    "post_tracking_guard_sec": post_tracking_guard_sec,
                    "settle_sec": settle_sec, "flush_sec": flush_sec,
                    "capture_sec": capture_sec, "disk_hdf5_sec": hdf5_sec,
                    "persist_sec": persist_sec, "other_sec": other_sec,
                    "point_total_sec": total_point_time,
                })
                point_timing = {
                    "slew": slew_sec, "settle": settle_sec, "flush": flush_sec,
                    "capture": capture_sec, "hdf5": hdf5_sec, "guards": guards_sec,
                    "tracking": tracking_enable_sec, "persist": persist_sec,
                    "other": other_sec, "wall": total_point_time,
                }
                for key, value in point_timing.items(): timing_totals[key] += value
                self.persist_runtime_metadata(point, {
                    "timing_pre_goto_guard_sec": pre_goto_guard_sec,
                    "timing_slew_sec": slew_sec,
                    "timing_post_goto_guard_sec": post_goto_guard_sec,
                    "timing_tracking_enable_sec": tracking_enable_sec,
                    "timing_post_tracking_guard_sec": post_tracking_guard_sec,
                    "timing_settle_sec": settle_sec, "timing_flush_sec": flush_sec,
                    "timing_capture_sec": capture_sec, "timing_hdf5_finalize_sec": hdf5_sec,
                    "timing_persist_sec": persist_sec, "timing_other_sec": other_sec,
                    "timing_point_total_sec": total_point_time,
                    "timing_distance_from_previous_deg": distance_from_previous,
                })
                self._timing_previous_target = (ra_hours, dec_deg)
                self._live_event(live_row, "POINT COMPLETE", "point_complete_utc")
                self._live_previous_point_complete_clock = time.perf_counter()
                self._live_timing_rows.append(live_row)
                self._write_live_timing_csv()
                command_delay = float(live_row.get("command_to_motion_start_sec") or 0.0)
                physical_motion = float(live_row.get("physical_motion_sec") or 0.0)
                motion_tail = float(live_row.get("motion_end_to_goto_return_sec") or 0.0)
                for key, value in {
                    "command_delay": command_delay, "physical_motion": physical_motion,
                    "motion_tail": motion_tail, "goto": slew_sec, "guards": guards_sec,
                    "tracking": tracking_enable_sec, "settle": settle_sec,
                    "flush": flush_sec, "capture": capture_sec, "disk_hdf5": hdf5_sec,
                    "persist": persist_sec, "inter_point": inter_point_delay_sec,
                    "other": other_sec, "point_total": total_point_time,
                }.items():
                    live_totals[key] += value
                if self.compact_console:
                    completed_now = actual_session_count
                    wall_now = time.perf_counter() - self._live_session_clock
                    remaining_points = max(0, len(self.observation_points) - completed_now)
                    remaining_estimate = (
                        (wall_now / completed_now) * remaining_points
                        if completed_now else 0.0
                    )
                    print(
                        f"✓ POINT {idx:02d}/{len(self.observation_points):02d} "
                        f"goto={slew_sec:.1f}s motion={physical_motion:.1f}s "
                        f"settle={settle_sec:.1f}s flush={flush_sec:.1f}s "
                        f"capture={capture_sec:.1f}s disk={hdf5_sec:.1f}s "
                        f"total={total_point_time:.1f}s",
                        flush=True,
                    )
                    print(
                        f"  ELAPSED {format_duration_hms(wall_now)}  |  "
                        f"REMAINING ~{format_duration_hms(remaining_estimate)}",
                        flush=True,
                    )
                else:
                    print(f"\n---------------- POINT {idx:02d} METRICS ----------------", flush=True)
                if not self.compact_console:
                    print(f"Pre-GOTO guard:              {pre_goto_guard_sec:.3f} s", flush=True)
                    print(f"Command -> motion start:     {command_delay:.3f} s" if live_row.get("motion_start_utc") else "Command -> motion start:     N/D", flush=True)
                    print(f"Physical motion:             {physical_motion:.3f} s" if live_row.get("motion_end_utc") else "Physical motion:             N/D", flush=True)
                    print(f"Motion end -> GOTO return:    {motion_tail:.3f} s" if live_row.get("motion_end_utc") else "Motion end -> GOTO return:    N/D", flush=True)
                    print(f"GOTO TOTAL:                  {slew_sec:.3f} s", flush=True)
                    print(f"Post-GOTO guard:              {post_goto_guard_sec:.3f} s", flush=True)
                    print(f"Tracking:                     {tracking_enable_sec:.3f} s", flush=True)
                    print(f"Post-track guard:             {post_tracking_guard_sec:.3f} s", flush=True)
                    print(f"Settle:                       {settle_sec:.3f} s", flush=True)
                    print(f"FLUSH:                        {flush_sec:.3f} s", flush=True)
                    print(f"Capture:                      {capture_sec:.3f} s", flush=True)
                    print(f"Disk write:                   {hdf5_sec:.3f} s", flush=True)
                    print(f"Persist:                      {persist_sec:.3f} s", flush=True)
                    print(f"Other:                        {other_sec:.3f} s", flush=True)
                    print(f"POINT TOTAL:                  {total_point_time:.3f} s", flush=True)
                    print("------------------------------------------------------\n", flush=True)
                n = actual_session_count
                wall_elapsed = time.perf_counter() - self._live_session_clock
                if not self.compact_console:
                    print(f"SESSION RUNNING TOTAL {n:02d}/{len(self.observation_points):02d}", flush=True)
                    print(f"GOTO total={live_totals['goto']:.3f} s", flush=True)
                    print(f"command->motion total={live_totals['command_delay']:.3f} s", flush=True)
                    print(f"physical motion total={live_totals['physical_motion']:.3f} s", flush=True)
                    print(f"motion-end->return total={live_totals['motion_tail']:.3f} s", flush=True)
                    print(f"guards total={live_totals['guards']:.3f} s", flush=True)
                    print(f"tracking total={live_totals['tracking']:.3f} s", flush=True)
                    print(f"settle total={live_totals['settle']:.3f} s", flush=True)
                    print(f"flush total={live_totals['flush']:.3f} s", flush=True)
                    print(f"capture total={live_totals['capture']:.3f} s", flush=True)
                    print(f"disk total={live_totals['disk_hdf5']:.3f} s", flush=True)
                    print(f"other total={live_totals['other']:.3f} s", flush=True)
                    print(f"wall total={wall_elapsed:.3f} s", flush=True)
                self.log(
                    f"POINT COMPLETE | slew={slew_sec:.3f} guards={guards_sec:.3f} "
                    f"tracking={tracking_enable_sec:.3f} settle={settle_sec:.3f} "
                    f"flush={flush_sec:.3f} capture={capture_sec:.3f} hdf5={hdf5_sec:.3f} "
                    f"persist={persist_sec:.3f} other={other_sec:.3f} total={total_point_time:.3f}",
                    force=True,
                )
                self.log(
                    f"RUNNING TOTAL {actual_session_count:02d}/20 | slew={timing_totals['slew']:.3f} "
                    f"settle={timing_totals['settle']:.3f} flush={timing_totals['flush']:.3f} "
                    f"capture={timing_totals['capture']:.3f} hdf5={timing_totals['hdf5']:.3f} "
                    f"guards/tracking={timing_totals['guards']+timing_totals['tracking']:.3f} "
                    f"persist={timing_totals['persist']:.3f} other={timing_totals['other']:.3f} "
                    f"wall={timing_totals['wall']:.3f} | "
                    f"avg_point={timing_totals['wall']/actual_session_count:.3f} "
                    f"avg_slew={timing_totals['slew']/actual_session_count:.3f} "
                    f"avg_flush={timing_totals['flush']/actual_session_count:.3f}",
                    force=True,
                )

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

            # Deferred visibility remains planned for a future resume.
            if safety_paused:
                self.session_manager.pause_session(self.session_id)
                self.log(
                    "Session paused by mount safety policy; remaining planned targets were preserved",
                    "WARNING", force=True,
                )
            elif self.visibility_deferred_count:
                self.session_manager.pause_session(self.session_id)
                self.log(f"No more currently executable targets; pending_total={self.visibility_deferred_count}, deferred_visibility={self.visibility_deferred_count}",force=True)
            else:
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

            return not safety_paused and failed == 0 and self.visibility_deferred_count == 0

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
            # Never deliberately leave the mount tracking after any exit path.
            await self.ensure_tracking_off()
            if self.sdr:
                await self.sdr.close()
                if self.verbose:self.log("SDR closed")


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
    parser.add_argument('--preflight-only', action='store_true',
                        help='Run all preflight checks and exit without movement or capture')
    parser.add_argument('--disk-safety-factor', type=float, default=1.25,
                        help='Required disk-space multiplier for preflight (default: 1.25)')

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
    parser.add_argument('--min-altitude', type=float, default=None,
                        help='Minimum target altitude at GOTO; config or 30 degrees by default')
    parser.add_argument('--tracking-timeout', type=float, default=5.0,
                        help='Seconds to confirm real tracking state (default: 5)')

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
            sdr_sample_rate=args.sdr_rate,
            min_altitude_deg=args.min_altitude,
            tracking_timeout=args.tracking_timeout,
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
            sdr_sample_rate=args.sdr_rate,
            min_altitude_deg=args.min_altitude,
            tracking_timeout=args.tracking_timeout,
        )

        if not executor.load_observation_plan(resume=False, force=args.force):
            print("Failed to load observation plan")
            sys.exit(1)

    else:
        parser.print_help()
        print("\nERROR: Must specify either --csv or --resume")
        sys.exit(1)

    # Compact is the operator-facing mode. Detailed diagnostics remain in the
    # timing CSV and are still available with --debug.
    executor.compact_console = not executor.verbose

    # Connect to INDI server
    executor.telescope = INDITelescopeControl(
        host=executor.host,
        port=executor.port,
        device_name=executor.device_name,
        verbose=executor.verbose
    )
    executor.telescope.compact_console = executor.compact_console

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

    preflight = await executor.run_preflight(
        capture_time=args.capture,
        disk_safety_factor=args.disk_safety_factor,
    )
    executor.log_preflight(preflight)
    if executor.compact_console:
        print(f"PREFLIGHT  {'PASS' if preflight['success'] else 'FAIL'}", flush=True)
    if not should_execute_after_preflight(preflight, args.preflight_only):
        if executor.telescope and executor.telescope.writer:
            executor.telescope.writer.close()
            await executor.telescope.writer.wait_closed()
        sys.exit(0 if preflight["success"] else 1)

    # Execute observation plan
    executor.compact_console = True
    executor.telescope.compact_console = True
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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSTOPPED    session paused; tracking-off cleanup requested", flush=True)
