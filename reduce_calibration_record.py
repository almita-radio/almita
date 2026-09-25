#!/usr/bin/env python3
"""ALMITA REDUCE - shared helper for a VERIFIABLE, RUN-time calibration-compatibility record.

reduce_engine's own persistence layer (reduce_engine/storage.py, frozen) does not write
CalibrationOutcome's compatibility_status/compatibility_reason into master_spectrum.json - only
calibration_level, calibration_profile_id and calibration_profile_hash survive (confirmed by reading
reduce_engine/calibration.py and reduce_engine/storage.py; see the REDUCE web console's own commit
history for this finding). Rather than edit that frozen module, this file gives the web bridge
scripts (reduce_campaign_run.py, reduce_single_capture.py) a small, additive way to record - as its
own separate, real artifact, never altering reduce_engine's own output - exactly what
calibration_foundation.check_calibration_compatibility() found for every point, at the one moment
that matters: immediately before reduce_engine.pipeline.reduce_campaign() actually processes them.

This is NOT the same as re-deriving compatibility after the fact from whatever the HDF5 files happen
to contain right now (which could differ from what RUN actually saw, if a file was edited or replaced
mid-run) - it is captured once, right before the real frozen reduction call, in the same process, so
the gap between "what this records" and "what reduce_engine.calibration.apply_calibration() itself
independently computed a moment later, internally, using the exact same function" is as small as a
single Python process can make it without modifying reduce_engine itself.

File identity ("did this exact file change since an earlier PLAN/preview") is checked elsewhere
(almita_web_ops.py's _require_fresh_plan, server-side, before a RUN job is even started) using a
cheap (size, mtime_ns) signature - the same shape returned here per point - never a full-file hash
(hashing every raw IQ capture, which can be 10+ MB each, on every PLAN/RUN check would be needlessly
slow for a large campaign).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def file_signature(path: Path) -> Dict[str, Any]:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def compute_record(profile_path: Optional[str], points: Sequence[Tuple[int, Optional[Path]]]) -> Optional[Dict[str, Any]]:
    """points: (point_index, resolved_path-or-None) for every ACCEPTED point about to be reduced.
    Returns None if no profile was requested (no compatibility question to record - the whole run is
    UNCALIBRATED by choice, already unambiguous without a per-point record)."""
    if not profile_path:
        return None
    from calibration_foundation import check_calibration_compatibility, load_calibration_profile
    profile = load_calibration_profile(profile_path)
    pp = Path(profile_path)
    profile_sig = None
    try:
        profile_sig = {"json": file_signature(pp.with_suffix(".json")), "npz": file_signature(pp.with_suffix(".npz"))}
    except OSError:
        pass
    points_record: List[Dict[str, Any]] = []
    for point_index, resolved_path in points:
        if resolved_path is None:
            points_record.append({"point_index": point_index, "status": "UNKNOWN",
                                  "reason": "no resolved capture file for this point", "signature": None})
            continue
        try:
            result = check_calibration_compatibility(profile, resolved_path)
            status, reason = result["status"], result["reason"]
        except (OSError, ValueError, KeyError) as exc:
            status, reason = "UNKNOWN", str(exc)
        try:
            sig = file_signature(resolved_path)
        except OSError:
            sig = None
        points_record.append({"point_index": point_index, "status": status, "reason": reason, "signature": sig})
    return {
        "checked_immediately_before_reduce_campaign_utc": datetime.now(timezone.utc).isoformat(),
        "profile": str(profile_path), "profile_signature": profile_sig,
        "points": points_record,
        "note": "status/reason captured right before reduce_engine.pipeline.reduce_campaign() processed these "
               "points, using the same calibration_foundation.check_calibration_compatibility() the frozen "
               "engine's own calibration stage calls internally - this is what RUN actually saw, not a "
               "pre-RUN preview and not a post-hoc re-check of whatever the files contain now.",
    }


def write_record(output_dir: Optional[str], record: Optional[Dict[str, Any]]) -> Optional[str]:
    if record is None or not output_dir:
        return None
    out = Path(output_dir) / "calibration_compatibility_record.json"
    out.write_text(json.dumps(record, indent=2, default=str))
    return str(out)
