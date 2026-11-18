"""One OBSERVE point captured with frequency switching (BL-100; capture.py --fswitch, operator 2026-11-09: "descongela
y aplica" - first proven on a working copy of capture.py, then moved into it).

The point's capture time is split into ABBA dwells (fswitch.lo_segments): LO A = f0 - delta, LO B = f0 + delta,
repeated LOs recorded as one segment, the measured retune pause (fswitch.SETTLE_S) after each change and every
segment's head checked for old-LO samples (fswitch.head_trim). The raw I/Q of every segment is kept - nothing is
reduced here - in ONE HDF5 written exactly like SDRCapture.capture() writes its own (write_hdf5_atomic: same iq_data
dataset, same attributes, same validation and atomic rename), plus:

  fswitch = "ABBA", fswitch_f_a_hz, fswitch_f_b_hz, fswitch_offset_hz, fswitch_settle_s, fswitch_dwell_s
  fswitch_segments_json = [{"lo_hz", "start_sample", "n_samples", "trimmed_samples"}, ...] (offsets into iq_data)
  center_frequency_hz = f0 (the nominal centre the LOs straddle), sdr_frequency_mode = "frequency_switched"

so a plain reader sees a valid capture; REDUCE (reduce_engine.fswitch_fold, reduce-v1.1) recognises the segment
table and folds it - an older REDUCE would read it as one long capture at f0, which is wrong. After the point the SDR is put
back on f0, the state the rest of capture.py expects.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

import fswitch as F

DEFAULT_OFFSET_HZ = 600e3
DEFAULT_DWELL_S = 2.0


def plan_cycles(duration_s: float, dwell_s: float = DEFAULT_DWELL_S) -> tuple:
    """(cycles, dwell) so that 2 x cycles x dwell == duration exactly, dwell as close to the requested as possible."""
    cycles = max(1, int(round(duration_s / (2.0 * dwell_s))))
    return cycles, duration_s / (2.0 * cycles)


def _metadata(metadata: Optional[Dict[str, Any]], *, f0: int, rate: int, duration: float, capture_time: float,
              samples: int, iq_bytes: int, sdr: Any) -> Dict[str, Any]:
    """The same attribute set SDRCapture._capture_network writes, so downstream readers find what they expect."""
    now = datetime.now(timezone.utc)
    md = metadata or {}
    out = {
        "capture_start_utc": md.get("capture_started_at", now.isoformat()), "capture_completed_at": now.isoformat(),
        "unix_timestamp": now.timestamp(), "created_at": now.isoformat(),
        "center_frequency_hz": md.get("center_freq", f0), "sample_rate_hz": rate, "gain": md.get("gain", "auto"),
        "sdr_mode": getattr(sdr, "mode", "network"), "sdr_host": getattr(sdr, "host", None), "sdr_port": getattr(sdr, "port", None),
        "duration_seconds": duration, "requested_capture_duration_sec": duration,
        "actual_capture_duration_sec": capture_time, "num_samples": samples, "raw_iq_bytes": iq_bytes,
        "bytes_per_complex_sample": 2, "capture_time_seconds": capture_time,
        "data_type": "IQ_uint8", "sample_format": "interleaved", "bits_per_sample": 8,
        "capture_status": "success", "file_state": "complete",
    }
    if md:
        out.update(md)
        out.update({
            "target_ra_hours": md.get("ra_hours"), "target_dec_deg": md.get("dec_degrees"),
            "target_dec_degrees": md.get("dec_degrees"), "target_ra_hms": md.get("ra_hms"),
            "target_dec_dms": md.get("dec_dms"), "target_azimuth": md.get("azimuth"),
            "target_altitude": md.get("altitude"), "target_name": md.get("target_name"),
            "point_number": md.get("point_number"), "settle_time_seconds": md.get("settle_time"),
            "tracking_enabled": md.get("tracking"), "telescope_name": md.get("telescope_name"),
            "observer_latitude_deg": md.get("observer_latitude"), "observer_longitude_deg": md.get("observer_longitude"),
            "observer_elevation_m": md.get("observer_elevation"), "observer_name": md.get("observer_name"),
            "observer_location": md.get("observer_location"),
        })
    return out


async def capture_point(sdr: Any, *, f0_hz: int, sample_rate: int, gain_db: float, duration_s: float,
                        output_file: str, metadata: Optional[Dict[str, Any]] = None,
                        offset_hz: float = DEFAULT_OFFSET_HZ, dwell_s: float = DEFAULT_DWELL_S,
                        settle_s: float = F.SETTLE_S, log=None, home_hz: Optional[int] = None,
                        extra_attrs: Optional[Dict[str, Any]] = None):
    """Capture one point frequency-switched into output_file; returns a sdr_capture.CaptureMetrics.

    BL-105: f0_hz may be a centre other than the operating tuning (fswitch_center.plan_point); `home_hz` is where
    the SDR is put back afterwards (default f0_hz, as before) and `extra_attrs` (the centre-velocity record) are
    written into the capture's attributes, lists as JSON."""
    from sdr_capture import CaptureMetrics, adc_level_summary, write_hdf5_atomic
    log = log or (lambda m: None)
    cycles, dwell = plan_cycles(duration_s, dwell_s)
    f_a, f_b = int(round(f0_hz - offset_hz)), int(round(f0_hz + offset_hz))
    previous_settle = sdr.retune_settle_seconds
    sdr.retune_settle_seconds = settle_s
    callback = getattr(sdr, "timing_callback", None)
    chunks: List[np.ndarray] = []
    table: List[Dict[str, Any]] = []
    t0 = time.perf_counter()
    try:
        position = 0
        for li, parts in F.lo_segments(cycles):
            lo = (f_a, f_b)[li]
            await sdr.configure(center_freq=lo, sample_rate=sample_rate, gain=str(gain_db))
            await sdr.flush_buffer()
            raw = await sdr.capture_iq(parts * dwell, sample_rate)
            drop = F.head_trim(raw, sample_rate)
            kept = raw[2 * drop:]
            chunks.append(kept)
            table.append({"lo_hz": lo, "start_sample": position, "n_samples": int(kept.size // 2),
                          "trimmed_samples": int(drop), "dwells": int(parts)})
            position += int(kept.size // 2)
            if drop:
                log(f"FSWITCH segment at {lo / 1e6:.4f} MHz: head trimmed {drop / sample_rate * 1e3:.0f} ms")
    finally:
        # back on the nominal tuning whatever happened: the rest of capture.py (and the next point) expects it
        try:
            await sdr.configure(center_freq=int(home_hz if home_hz is not None else f0_hz), sample_rate=sample_rate,
                                gain=str(gain_db))
        finally:
            sdr.retune_settle_seconds = previous_settle
    capture_time = time.perf_counter() - t0
    iq = np.concatenate(chunks) if chunks else np.zeros(0, np.uint8)
    samples = int(iq.size // 2)
    if callback:
        callback("capture_end", {"duration": capture_time, "samples_received": samples})
    md = _metadata(metadata, f0=int(f0_hz), rate=sample_rate, duration=duration_s, capture_time=capture_time,
                   samples=samples, iq_bytes=int(iq.size), sdr=sdr)
    md["center_frequency_hz"] = int(f0_hz)
    md.update(adc_level_summary(iq))
    md.update({"sdr_frequency_mode": "frequency_switched", "fswitch": "ABBA", "fswitch_f_a_hz": f_a, "fswitch_f_b_hz": f_b,
               "fswitch_offset_hz": float(offset_hz), "fswitch_settle_s": float(settle_s), "fswitch_dwell_s": float(dwell),
               "fswitch_cycles": int(cycles), "fswitch_segments_json": json.dumps(table),
               "fswitch_head_trimmed_samples": int(sum(t["trimmed_samples"] for t in table))})
    for k, v in (extra_attrs or {}).items():
        md[k] = json.dumps(v) if isinstance(v, (list, tuple, dict)) else v
    from pathlib import Path
    md["final_filename"] = Path(output_file).name
    if callback:
        callback("disk_write_start", {"file": str(output_file)})
    w0 = time.perf_counter()
    path = await asyncio.get_running_loop().run_in_executor(None, write_hdf5_atomic, output_file, iq, md, samples)
    write_time = time.perf_counter() - w0
    size_mb = Path(path).stat().st_size / 1024 / 1024
    if callback:
        callback("disk_write_end", {"duration": write_time, "file": str(path), "size_mb": size_mb})
    return CaptureMetrics(capture_time=capture_time, disk_write_time=write_time, total_samples=samples,
                          sample_rate=sample_rate, throughput_mbps=iq.size / max(capture_time, 1e-9) / 1024 / 1024,
                          adc_rms_codes=md.get("adc_rms_codes"), adc_rail_hit_fraction=md.get("adc_rail_hit_fraction"))


def read_segments(attrs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The segment table of a frequency-switched capture ([] for an ordinary one)."""
    raw = attrs.get("fswitch_segments_json")
    if raw is None:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(str(raw))
