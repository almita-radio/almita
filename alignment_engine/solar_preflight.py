"""Solar GOTO preflight gate (item 12: define and implement the gate that
must pass before any FUTURE real solar GOTO - this pass does not execute a
GOTO, only builds and tests the gate).

Distinct from (and stricter than) AlignmentEngine.preflight()'s existing
single-instant altitude check (engine.py, `target_provider.altitude_deg`):
this module additionally samples across the whole intended test window (not
just "now") and enforces both a floor (operational altitude/horizon) and a
ceiling (the mount's own configured elevation limit - see the real
'Slew elevation Limit' INDI property this installation exposes, read-only
audited 2026-09-18: minAlt=0 maxAlt=90), so a target that is fine right now
but would fall out of bounds before the test finishes is still BLOCKED.

Pure computation - no I/O, no mount connection. A caller supplies an
`AltitudeAzimuthProvider` (SolarTarget already implements this shape) and
whatever limit numbers it already has (from GlobalConfig and/or a read-only
inspection of the real mount) - this module never fetches them itself.

No override switch exists anywhere in this module, and none is exposed by
any CLI wired to it (see almita_align.py's `solar goto-preflight`) - a
future decision to bypass this gate must be a separate, deliberate code
change, never a flag flip on this command.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol

import astropy.units as u
from astropy.time import Time

# Mirrors GlobalConfig.altitude_floor_deg / SolarScanConfig.min_altitude_deg
# - the same conservative science-driven floor already used elsewhere in
# this package, not a new, second number invented here.
DEFAULT_MIN_SOLAR_ALTITUDE_DEG = 20.0
# The real driver's own configured ceiling on this installation (see module
# docstring) - overridable by a caller that has read a different value.
DEFAULT_MAX_ALTITUDE_DEG = 90.0


class AltitudeAzimuthProvider(Protocol):
    def altitude_deg(self, obstime: Time) -> float: ...
    def azimuth_deg(self, obstime: Time) -> float: ...


@dataclass
class PreflightSample:
    offset_seconds: float
    altitude_deg: float
    azimuth_deg: float
    inside_limits: bool

    def to_dict(self) -> dict:
        return {
            "offset_seconds": self.offset_seconds,
            "altitude_deg": self.altitude_deg,
            "azimuth_deg": self.azimuth_deg,
            "inside_limits": self.inside_limits,
        }


@dataclass
class SolarPreflightResult:
    verdict: str  # "PASS" | "BLOCKED"
    reason: str
    altitude_now_deg: float
    azimuth_now_deg: float
    altitude_end_deg: float
    azimuth_end_deg: float
    min_altitude_deg: float
    max_altitude_deg: float
    window_seconds: float
    samples: List[PreflightSample] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "altitude_now_deg": self.altitude_now_deg,
            "azimuth_now_deg": self.azimuth_now_deg,
            "altitude_end_deg": self.altitude_end_deg,
            "azimuth_end_deg": self.azimuth_end_deg,
            "min_altitude_deg": self.min_altitude_deg,
            "max_altitude_deg": self.max_altitude_deg,
            "window_seconds": self.window_seconds,
            "samples": [s.to_dict() for s in self.samples],
        }


def check_solar_preflight(provider: AltitudeAzimuthProvider, obstime: Time, window_seconds: float,
                           min_altitude_deg: float = DEFAULT_MIN_SOLAR_ALTITUDE_DEG,
                           max_altitude_deg: float = DEFAULT_MAX_ALTITUDE_DEG,
                           sample_count: int = 5) -> SolarPreflightResult:
    """BLOCKED unless the target is inside [min_altitude_deg, max_altitude_deg]
    at every one of `sample_count` evenly spaced instants across
    [obstime, obstime + window_seconds] - not just the two endpoints, so a
    target that dips out of bounds in the middle of the window is still
    caught."""
    if window_seconds < 0:
        raise ValueError("window_seconds must be >= 0")
    if sample_count < 2:
        raise ValueError("sample_count must be >= 2 - must cover both the start and the end of the window")

    samples: List[PreflightSample] = []
    for i in range(sample_count):
        offset = window_seconds * i / (sample_count - 1)
        t = obstime + offset * u.s
        alt = float(provider.altitude_deg(t))
        az = float(provider.azimuth_deg(t)) % 360.0
        inside = min_altitude_deg <= alt <= max_altitude_deg
        samples.append(PreflightSample(offset_seconds=offset, altitude_deg=alt, azimuth_deg=az, inside_limits=inside))

    first, last = samples[0], samples[-1]
    common = dict(
        altitude_now_deg=first.altitude_deg, azimuth_now_deg=first.azimuth_deg,
        altitude_end_deg=last.altitude_deg, azimuth_end_deg=last.azimuth_deg,
        min_altitude_deg=min_altitude_deg, max_altitude_deg=max_altitude_deg,
        window_seconds=window_seconds, samples=samples,
    )

    if not first.inside_limits:
        if first.altitude_deg < 0:
            reason = f"Sun below operational altitude (below horizon, altitude={first.altitude_deg:.2f} deg)"
        elif first.altitude_deg < min_altitude_deg:
            reason = (f"Sun below operational altitude (above horizon at {first.altitude_deg:.2f} deg "
                       f"but below the configured floor of {min_altitude_deg:.1f} deg)")
        else:
            reason = (f"target outside mount limits (altitude {first.altitude_deg:.2f} deg is above "
                       f"the configured maximum of {max_altitude_deg:.1f} deg)")
        return SolarPreflightResult(verdict="BLOCKED", reason=reason, **common)

    failing = next((s for s in samples if not s.inside_limits), None)
    if failing is not None:
        if failing.altitude_deg < min_altitude_deg:
            reason = (f"insufficient visibility window (Sun falls to {failing.altitude_deg:.2f} deg, below "
                       f"the {min_altitude_deg:.1f} deg floor, at +{failing.offset_seconds:.0f}s into the "
                       f"{window_seconds:.0f}s test window)")
        else:
            reason = (f"insufficient visibility window (target rises to {failing.altitude_deg:.2f} deg, above "
                       f"the {max_altitude_deg:.1f} deg mount limit, at +{failing.offset_seconds:.0f}s into the "
                       f"{window_seconds:.0f}s test window)")
        return SolarPreflightResult(verdict="BLOCKED", reason=reason, **common)

    return SolarPreflightResult(
        verdict="PASS",
        reason=(f"Sun stays within [{min_altitude_deg:.1f}, {max_altitude_deg:.1f}] deg altitude for the "
                f"entire {window_seconds:.0f}s test window (now={first.altitude_deg:.2f} deg, "
                f"end={last.altitude_deg:.2f} deg)"),
        **common,
    )
