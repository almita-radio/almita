"""VELOCITY FRAME stage: reuses alignment_engine.hi.velocity verbatim (the
already-validated, astropy-backed topocentric->LSRK/heliocentric/
barycentric conversion). Requires observation time, observer location, and
sky direction; if any is missing, velocity_frame is reported UNAVAILABLE -
REDUCE never invents a Doppler correction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from alignment_engine.hi.velocity import SUPPORTED_FRAMES, frame_shift_km_s
from hi_spectral_metric import frequency_to_radio_velocity

# REDUCE must be 100% offline (no hardware, no network) - astropy's default
# IERS-A behavior otherwise attempts a network download of Earth-orientation
# bulletins on first use of Time/EarthLocation frame transforms, and retries
# with a network timeout on every call while offline. Force it to use only
# the bundled, built-in low-precision tables instead. This was found for
# real: without it, a 9-point real historical campaign's velocity stage
# alone took >200s; with it, the whole campaign completes in seconds.
from astropy.utils import iers as _astropy_iers
_astropy_iers.conf.auto_download = False
# auto_download=False alone still raises once the bundled IERS table is
# more than 30 days old relative to wall-clock "now" (a near-certainty on
# a machine that is deliberately offline for months) - None disables that
# staleness check too, accepting the small, bounded precision loss
# (arcsecond-level polar motion) instead of refusing to run at all.
_astropy_iers.conf.auto_max_age = None


@dataclass
class VelocityResult:
    frame: str  # one of SUPPORTED_FRAMES, or "UNAVAILABLE"
    velocity_m_s: Optional[np.ndarray]
    reason: str


def compute_velocity_axis(frequency_hz: np.ndarray, *, rest_frequency_hz: float, frame: str,
                          timestamp_utc: Optional[str], observer_latitude_deg: Any,
                          observer_longitude_deg: Any, observer_elevation_m: Any,
                          ra_hours: Any, dec_degrees: Any) -> VelocityResult:
    missing = [name for name, value in (
        ("timestamp_utc", timestamp_utc), ("observer_latitude_deg", observer_latitude_deg),
        ("observer_longitude_deg", observer_longitude_deg), ("observer_elevation_m", observer_elevation_m),
        ("ra_hours", ra_hours), ("dec_degrees", dec_degrees),
    ) if value in (None, "UNKNOWN", "")]
    if missing:
        return VelocityResult(frame="UNAVAILABLE", velocity_m_s=None,
                              reason=f"missing required metadata for velocity frame: {missing}")
    if frame not in SUPPORTED_FRAMES:
        return VelocityResult(frame="UNAVAILABLE", velocity_m_s=None,
                              reason=f"unsupported frame {frame!r} (fail-safe: refusing to guess)")
    import astropy.units as u
    from astropy.coordinates import EarthLocation, SkyCoord
    from astropy.time import Time

    # astropy's Time() 'isot' auto-detection does not accept a "+00:00" UTC
    # offset suffix (real capture metadata carries one, e.g.
    # "2026-09-02T16:18:20.126808+00:00") - parse with datetime first,
    # which does accept it, then hand Time() an unambiguous datetime object.
    from datetime import datetime, timezone
    parsed = datetime.fromisoformat(timestamp_utc)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    obstime = Time(parsed, scale="utc")
    location = EarthLocation(lat=float(observer_latitude_deg) * u.deg,
                             lon=float(observer_longitude_deg) * u.deg,
                             height=float(observer_elevation_m) * u.m)
    target = SkyCoord(ra=float(ra_hours) * u.hourangle, dec=float(dec_degrees) * u.deg, frame="icrs")
    # The IAU radio-velocity convention is affine in frequency, and a frame
    # change (topocentric -> LSRK/etc) is a single additive velocity offset
    # independent of frequency (alignment_engine.hi.velocity.frame_shift_km_s's
    # own documented contract) - so the shift is computed ONCE via astropy,
    # then applied to the whole (vectorized) topocentric velocity axis,
    # rather than re-invoking astropy's frame transform per bin.
    shift_km_s = frame_shift_km_s(frame=frame, obstime=obstime, location=location, target=target)
    velocity_km_s = frequency_to_radio_velocity(frequency_hz, rest_hz=rest_frequency_hz) + shift_km_s
    return VelocityResult(frame=frame, velocity_m_s=velocity_km_s * 1000.0, reason="computed")
