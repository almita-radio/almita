#!/usr/bin/env python3
"""Observation Specification: declarative YAML input for the ALMITA Orchestrator.

Loads and validates an operator-authored observation.yaml into a normalized,
typed dict (never a bare pass-through of untrusted input) and computes the
deterministic `observation_config_sha256` used for provenance/tamper checks.

This module never touches hardware, INDI, SDR, or the filesystem beyond
reading the YAML/JSON files it is given.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict

import yaml

SCHEMA_VERSION = 1

GRID_MODES = ("EQUATORIAL_RECT",)
# Only EASTMOST_SAFE and FIXED_CENTER are implemented in V1. The others are
# reserved so a spec author gets a clear "not yet implemented" error instead
# of an "unknown enum" error (see FUTURE PLACEMENT EXTENSIBILITY).
PLACEMENT_STRATEGIES_IMPLEMENTED = ("FIXED_CENTER", "EASTMOST_SAFE")
PLACEMENT_STRATEGIES_RESERVED = ("CENTERED", "GALACTIC_TARGET", "TRANSIT_WINDOW")
TRAVERSAL_MODES = ("SERPENTINE",)

_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_ALLOWED_TOP_LEVEL = {
    "session", "grid", "capture", "main", "rfi_ref", "quicklook", "console", "execution",
}


class ObservationSpecError(ValueError):
    """Raised for any validation failure of an observation specification."""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ObservationSpecError(message)


def _require_keys(d: Any, keys, where: str) -> None:
    _require(isinstance(d, dict), f"{where}: must be a mapping")
    unknown = set(d.keys()) - set(keys)
    _require(not unknown, f"{where}: unknown field(s) {sorted(unknown)}")


def _num(d: dict, key: str, where: str, *, required: bool = True, default=None,
         min_value=None, max_value=None, kind=float):
    if key not in d or d[key] is None:
        if required:
            raise ObservationSpecError(f"{where}.{key}: required field is missing")
        return default
    value = d[key]
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
              f"{where}.{key}: must be numeric, got {value!r}")
    value = kind(value)
    if min_value is not None:
        _require(value >= min_value, f"{where}.{key}: must be >= {min_value}, got {value}")
    if max_value is not None:
        _require(value <= max_value, f"{where}.{key}: must be <= {max_value}, got {value}")
    return value


def _bool(d: dict, key: str, where: str, *, required: bool = True, default=None):
    if key not in d or d[key] is None:
        if required:
            raise ObservationSpecError(f"{where}.{key}: required field is missing")
        return default
    value = d[key]
    _require(isinstance(value, bool), f"{where}.{key}: must be a boolean, got {value!r}")
    return value


def _str(d: dict, key: str, where: str, *, required: bool = True, default=None):
    if key not in d or d[key] is None:
        if required:
            raise ObservationSpecError(f"{where}.{key}: required field is missing")
        return default
    value = d[key]
    _require(isinstance(value, str) and value.strip(), f"{where}.{key}: must be a non-empty string")
    return value


def _enum(d: dict, key: str, where: str, allowed, *, reserved=(), required: bool = True, default=None):
    value = _str(d, key, where, required=required, default=default)
    if value is None:
        return None
    if value in reserved:
        raise ObservationSpecError(
            f"{where}.{key}: '{value}' is a reserved placement/mode name not yet implemented in V1"
        )
    _require(value in allowed, f"{where}.{key}: '{value}' is not one of {list(allowed)}")
    return value


def _validate_session(raw: dict) -> dict:
    where = "session"
    _require_keys(raw, ("name",), where)
    name = _str(raw, "name", where)
    _require(bool(_SESSION_NAME_RE.match(name)),
              f"{where}.name: must match {_SESSION_NAME_RE.pattern} (safe for use as a directory name)")
    return {"name": name}


def _validate_grid(raw: dict) -> dict:
    where = "grid"
    allowed = ("mode", "placement", "center_ra_hours", "center_dec_deg", "width_deg",
               "height_deg", "rows", "cols", "min_altitude_deg", "traversal")
    _require_keys(raw, allowed, where)

    mode = _enum(raw, "mode", where, GRID_MODES)
    placement = _enum(raw, "placement", where, PLACEMENT_STRATEGIES_IMPLEMENTED,
                       reserved=PLACEMENT_STRATEGIES_RESERVED)
    traversal = _enum(raw, "traversal", where, TRAVERSAL_MODES, required=False, default="SERPENTINE")

    width_deg = _num(raw, "width_deg", where, min_value=0.01, max_value=180.0)
    height_deg = _num(raw, "height_deg", where, min_value=0.01, max_value=180.0)
    rows = int(_num(raw, "rows", where, min_value=2, max_value=1000, kind=int))
    cols = int(_num(raw, "cols", where, min_value=2, max_value=1000, kind=int))
    min_altitude_deg = _num(raw, "min_altitude_deg", where, min_value=0.0, max_value=89.0)

    center_ra_hours = _num(raw, "center_ra_hours", where, required=False, default=None,
                            min_value=0.0, max_value=24.0)
    center_dec_deg = _num(raw, "center_dec_deg", where, required=False, default=None,
                           min_value=-90.0, max_value=90.0)

    if placement == "FIXED_CENTER":
        _require(center_ra_hours is not None, f"{where}.center_ra_hours: required when placement=FIXED_CENTER")
        _require(center_dec_deg is not None, f"{where}.center_dec_deg: required when placement=FIXED_CENTER")

    # Consistency check: a single nominal spacing must serve both axes,
    # matching how grid_generator.build_spherical_grid derives rows/cols
    # from one spacing value. Reject width/height/rows/cols combinations
    # that don't imply (nearly) the same spacing on both axes rather than
    # silently deviating from the requested grid shape.
    spacing_w = width_deg / (cols - 1)
    spacing_h = height_deg / (rows - 1)
    _require(
        abs(spacing_w - spacing_h) <= 1e-6 * max(spacing_w, spacing_h, 1.0) or
        abs(spacing_w - spacing_h) < 1e-3,
        f"{where}: width_deg/(cols-1)={spacing_w:.6f} and height_deg/(rows-1)={spacing_h:.6f} "
        "must match (within 1e-3 deg) — adjust width_deg/height_deg/rows/cols to a consistent spacing",
    )

    return {
        "mode": mode,
        "placement": placement,
        "center_ra_hours": center_ra_hours,
        "center_dec_deg": center_dec_deg,
        "width_deg": width_deg,
        "height_deg": height_deg,
        "rows": rows,
        "cols": cols,
        "min_altitude_deg": min_altitude_deg,
        "traversal": traversal,
        "nominal_spacing_deg": spacing_w,
    }


def _validate_capture(raw: dict) -> dict:
    where = "capture"
    _require_keys(raw, ("seconds", "settle_seconds"), where)
    return {
        "seconds": _num(raw, "seconds", where, min_value=0.1),
        "settle_seconds": _num(raw, "settle_seconds", where, min_value=0.0),
    }


def _validate_main(raw: dict) -> dict:
    where = "main"
    _require_keys(raw, ("center_frequency_hz", "sample_rate", "gain_db", "bias_tee"), where)
    bias_tee = _bool(raw, "bias_tee", where, required=False, default=True)
    # capture.py has no CLI flag to disable bias-tee: it is unconditionally
    # enabled in every production invocation today (CaptureExecutor's
    # bias_tee_enabled default is never overridden by main()'s argparse).
    # Rather than silently ignore an operator's explicit "false", fail
    # closed with an honest explanation.
    _require(bias_tee is True,
              f"{where}.bias_tee: false is not supported — capture.py has no CLI flag to disable "
              "bias-tee (it is always enabled in production); omit this field or set it to true")
    return {
        "center_frequency_hz": int(_num(raw, "center_frequency_hz", where, min_value=1, kind=int)),
        "sample_rate": int(_num(raw, "sample_rate", where, min_value=1, kind=int)),
        "gain_db": _num(raw, "gain_db", where),
        "bias_tee": bias_tee,
    }


def _validate_rfi_ref(raw: dict) -> dict:
    where = "rfi_ref"
    _require_keys(raw, ("enabled", "serial", "gain_db", "bias_tee"), where)
    enabled = _bool(raw, "enabled", where, required=False, default=False)
    serial = _str(raw, "serial", where, required=False, default="00000002")
    gain_db = _num(raw, "gain_db", where, required=False, default=25.0)
    # Unlike main.bias_tee (always-on, no CLI path to disable), RFI_REF's
    # bias-tee is genuinely optional per-field hardware (whether an LNA is
    # wired into antenna B's chain varies by deployment). Default false:
    # an older spec/archived resolved-plan with no opinion on this field
    # must revalidate to the same behavior rtl_tcp has always run with for
    # RFI_REF (no -T) rather than silently start energizing hardware that
    # wasn't there when the config was written.
    bias_tee = _bool(raw, "bias_tee", where, required=False, default=False)
    return {"enabled": enabled, "serial": serial, "gain_db": gain_db, "bias_tee": bias_tee}


def _validate_quicklook(raw: dict) -> dict:
    where = "quicklook"
    allowed = ("enabled", "native_grid", "interpolated_preview", "calibration_profile_path")
    _require_keys(raw, allowed, where)
    enabled = _bool(raw, "enabled", where, required=False, default=False)
    native_grid = _bool(raw, "native_grid", where, required=False, default=True)
    interpolated_preview = _bool(raw, "interpolated_preview", where, required=False, default=False)
    calibration_profile_path = _str(raw, "calibration_profile_path", where, required=False, default=None)
    if enabled:
        _require(calibration_profile_path is not None,
                  f"{where}.calibration_profile_path: required when quicklook.enabled=true")
    return {
        "enabled": enabled,
        "native_grid": native_grid,
        "interpolated_preview": interpolated_preview,
        "calibration_profile_path": calibration_profile_path,
    }


def _validate_console(raw: dict) -> dict:
    where = "console"
    _require_keys(raw, ("enabled",), where)
    return {"enabled": _bool(raw, "enabled", where, required=False, default=True)}


def _validate_execution(raw: dict) -> dict:
    where = "execution"
    _require_keys(raw, ("unattended",), where)
    return {"unattended": _bool(raw, "unattended", where, required=False, default=False)}


def validate_spec_dict(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a parsed observation-spec dict; return a normalized copy.

    Raises ObservationSpecError with a precise field path on any problem.
    Never raises on hardware/filesystem grounds — this is pure validation.
    """
    _require(isinstance(raw, dict), "observation spec: top level must be a mapping")
    unknown = set(raw.keys()) - _ALLOWED_TOP_LEVEL
    _require(not unknown, f"observation spec: unknown top-level section(s) {sorted(unknown)}")
    missing = _ALLOWED_TOP_LEVEL - set(raw.keys())
    _require(not missing, f"observation spec: missing top-level section(s) {sorted(missing)}")

    return {
        "schema_version": SCHEMA_VERSION,
        "session": _validate_session(raw["session"]),
        "grid": _validate_grid(raw["grid"]),
        "capture": _validate_capture(raw["capture"]),
        "main": _validate_main(raw["main"]),
        "rfi_ref": _validate_rfi_ref(raw["rfi_ref"]),
        "quicklook": _validate_quicklook(raw["quicklook"]),
        "console": _validate_console(raw["console"]),
        "execution": _validate_execution(raw["execution"]),
    }


def load_and_validate(path: str) -> Dict[str, Any]:
    """Load a YAML observation spec from disk and validate it."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ObservationSpecError(f"{path}: invalid YAML: {exc}") from exc
    return validate_spec_dict(raw)


def canonical_json(value: Any) -> str:
    """Deterministic JSON serialization: sorted keys, no whitespace ambiguity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_config_hash(resolved_config: Dict[str, Any]) -> str:
    """sha256 over the canonical JSON of the resolved (non-time-derived) config.

    Callers must exclude timestamps, duration estimates, and other
    time-varying fields before calling this — the hash identifies *what*
    was scheduled to run, not *when* it was planned.
    """
    return hashlib.sha256(canonical_json(resolved_config).encode("utf-8")).hexdigest()
