"""Physical-motion detection for hardware tracking tests.

Distinguishes:
  A. apparent coordinate drift caused purely by the passage of time
     (sidereal/solar rate) - expected, benign.
  B. real physical motion of the mount (a slew, a meridian flip, a runaway
     tracking rate) - the thing that actually matters for safety.

Physical basis (measured, not assumed - see
data/alignment/HW-TRACKING-20260918-004605/motion_analysis.json for the
first empirical confirmation, and motion_reinterpretation_v2.json in the
same directory for the corrected version using this module): for a German
equatorial mount with TELESCOPE_TRACK_STATE=OFF (RA-axis motor unpowered),
the *mechanical* pointing direction is fixed. Hour Angle (HA = LST - RA) is
exactly the RA axis' rotation angle from the meridian, so a motionless axis
has CONSTANT hour angle and DEC, while its of-date RA necessarily increases
with LST as time passes - a coordinate-frame fact, not evidence of
commanded movement. This is why a raw RA/DEC delta is the wrong invariant
with tracking off, and Hour Angle + DEC is the right one.

Real INDI read-only audit (2026-09-18, live indiserver, device "LX200
OnStep") found no raw encoder / axis-position property on this
installation ("TMC Axis1" under "OnStep Status" reports "TMC Reporting not
detected") - the user's stated preference order's item 1 (encoder/raw axis)
is therefore confirmed UNAVAILABLE, not merely unchecked, and this module
implements item 2 (Hour Angle + DEC) instead.

Scope limit, stated honestly rather than silently extrapolated: this
module's PASS/FAIL invariant (HA and DEC constant) has only been validated
against real hardware for TELESCOPE_TRACK_STATE=OFF throughout. It does not
yet assume the mirror-image relationship for TRACK_STATE=ON (where RA,
not HA, should be the invariant during sidereal tracking) - that case
returns INCONCLUSIVE until it too has real evidence behind it.

"Cerca del polo" (item 3): a raw HA delta corresponds to less true angular
motion the closer DEC is to +/-90 deg. Rather than a small-angle cos(DEC)
correction, `_angular_separation_arcsec` below uses the exact spherical law
of cosines, which is valid at any declination including at the pole.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# Mean solar day / sidereal day ratio (constant across this codebase - see
# hardware evidence in motion_analysis.json which independently confirmed
# this rate against a real, idle mount's apparent RA drift).
SIDEREAL_RATE_RATIO = 1.0027379093

# Empirically derived (2026-09-18, real hardware): the real LX200 OnStep
# driver was observed to report EQUATORIAL_EOD_COORD.RA at 1 whole
# RA-second resolution (13:22:26 -> 13:22:30 for a 3.786s real interval
# where 3.797 RA-seconds of sidereal drift was expected) - a 1 RA-second =
# 15 arcsec quantization step. Two independent readings can each be rounded
# by up to +/-0.5 RA-second, so the worst-case quantization artifact in a
# *difference* of two readings is one full RA-second (15").
RA_QUANTIZATION_ARCSEC = 15.0

# 2x the raw quantization step: absorbs both endpoints rounding in opposite
# directions plus ordinary reader/network jitter, while staying far below
# the scale of any real slew (arcminutes to tens of degrees) - so it cannot
# hide genuine motion. Not tuned after the fact to make any test result
# look good - fixed here, before re-running the historical evidence through
# it, from first-principles reasoning about the driver's own resolution.
DEFAULT_MOTION_TOLERANCE_ARCSEC = 2.0 * RA_QUANTIZATION_ARCSEC


@dataclass
class MotionCheckResult:
    verdict: str  # "PASS" | "FAIL" | "INCONCLUSIVE"
    reason: str
    ra_before_hours: Optional[float]
    ra_after_hours: Optional[float]
    dec_before_deg: Optional[float]
    dec_after_deg: Optional[float]
    lst_before_hours: Optional[float]
    lst_after_hours: Optional[float]
    ha_before_hours: Optional[float]
    ha_after_hours: Optional[float]
    delta_ha_arcsec: Optional[float]
    delta_dec_arcsec: Optional[float]
    elapsed_s: Optional[float]
    expected_ra_drift_hours: Optional[float]
    observed_ra_drift_hours: Optional[float]
    tolerance_arcsec: float
    angular_separation_arcsec: Optional[float] = None
    track_state_before: Optional[str] = None
    track_state_after: Optional[str] = None
    site_lat_deg: Optional[float] = None
    site_lon_deg: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "ra_before": self.ra_before_hours,
            "ra_after": self.ra_after_hours,
            "dec_before": self.dec_before_deg,
            "dec_after": self.dec_after_deg,
            "lst_before": self.lst_before_hours,
            "lst_after": self.lst_after_hours,
            "ha_before": self.ha_before_hours,
            "ha_after": self.ha_after_hours,
            "delta_ha_arcsec": self.delta_ha_arcsec,
            "delta_dec_arcsec": self.delta_dec_arcsec,
            "elapsed_s": self.elapsed_s,
            "expected_ra_drift": self.expected_ra_drift_hours,
            "observed_ra_drift": self.observed_ra_drift_hours,
            "tolerance_arcsec": self.tolerance_arcsec,
            "angular_separation_arcsec": self.angular_separation_arcsec,
            "track_state_before": self.track_state_before,
            "track_state_after": self.track_state_after,
            "site_lat_deg": self.site_lat_deg,
            "site_lon_deg": self.site_lon_deg,
        }


def track_state_from_switch_vector(vector: Optional[dict]) -> Optional[str]:
    """'ON' | 'OFF' | None (missing/ambiguous - never guessed) from a
    TELESCOPE_TRACK_STATE switch vector dict such as
    {'TRACK_ON': 'Off', 'TRACK_OFF': 'On'}."""
    if not vector:
        return None
    on = [k for k, v in vector.items() if v == "On"]
    if len(on) != 1:
        return None
    if on[0] == "TRACK_ON":
        return "ON"
    if on[0] == "TRACK_OFF":
        return "OFF"
    return None


def _wrap_hours_signed(hours: float) -> float:
    """Wrap into [-12, 12) - the standard hour-angle convention, and also
    the right wrap for a small RA delta that happens to straddle 24h/0h."""
    return ((hours + 12.0) % 24.0) - 12.0


def _angular_separation_arcsec(ha1_hours: float, dec1_deg: float,
                                ha2_hours: float, dec2_deg: float) -> float:
    """Exact great-circle separation between two (HA, DEC) directions via
    the spherical law of cosines - not a small-angle cos(DEC)
    approximation, so it stays correct arbitrarily close to the pole."""
    ha1 = math.radians(ha1_hours * 15.0)
    ha2 = math.radians(ha2_hours * 15.0)
    dec1 = math.radians(dec1_deg)
    dec2 = math.radians(dec2_deg)
    cos_sep = (math.sin(dec1) * math.sin(dec2)
               + math.cos(dec1) * math.cos(dec2) * math.cos(ha1 - ha2))
    cos_sep = max(-1.0, min(1.0, cos_sep))  # guard fp overshoot at/near 0 or 180 deg
    return math.degrees(math.acos(cos_sep)) * 3600.0


def _lst_hours(ts_utc: datetime, lat_deg: float, lon_deg: float) -> float:
    from astropy.coordinates import EarthLocation
    from astropy.time import Time
    import astropy.units as u

    naive_utc = ts_utc.astimezone(timezone.utc).replace(tzinfo=None)
    location = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg)
    t = Time(naive_utc, location=location)
    return float(t.sidereal_time("apparent").hour)


def classify_motion(*, ra_before_hours: Optional[float], dec_before_deg: Optional[float],
                     ra_after_hours: Optional[float], dec_after_deg: Optional[float],
                     ts_before_utc: Optional[datetime], ts_after_utc: Optional[datetime],
                     track_state_before: Optional[str], track_state_after: Optional[str],
                     site_lat_deg: Optional[float], site_lon_deg: Optional[float],
                     tolerance_arcsec: float = DEFAULT_MOTION_TOLERANCE_ARCSEC) -> MotionCheckResult:
    """PASS / FAIL / INCONCLUSIVE - never a false FAIL from ordinary
    sidereal drift, and never a silent PASS when there isn't enough
    evidence to say so (missing data, or a TRACK_STATE this module's model
    hasn't been validated against - see module docstring)."""
    required = (ra_before_hours, dec_before_deg, ra_after_hours, dec_after_deg,
                ts_before_utc, ts_after_utc, site_lat_deg, site_lon_deg)
    elapsed_s = ((ts_after_utc - ts_before_utc).total_seconds()
                 if (ts_before_utc is not None and ts_after_utc is not None) else None)
    if any(v is None for v in required):
        return MotionCheckResult(
            verdict="INCONCLUSIVE",
            reason="insufficient data to compute a physical hour-angle motion check "
                   "(missing coordinate, timestamp, or site location) - never guessed",
            ra_before_hours=ra_before_hours, ra_after_hours=ra_after_hours,
            dec_before_deg=dec_before_deg, dec_after_deg=dec_after_deg,
            lst_before_hours=None, lst_after_hours=None, ha_before_hours=None, ha_after_hours=None,
            delta_ha_arcsec=None, delta_dec_arcsec=None, elapsed_s=elapsed_s,
            expected_ra_drift_hours=None, observed_ra_drift_hours=None,
            tolerance_arcsec=tolerance_arcsec, angular_separation_arcsec=None,
            track_state_before=track_state_before, track_state_after=track_state_after,
            site_lat_deg=site_lat_deg, site_lon_deg=site_lon_deg,
        )

    lst_before = _lst_hours(ts_before_utc, site_lat_deg, site_lon_deg)
    lst_after = _lst_hours(ts_after_utc, site_lat_deg, site_lon_deg)
    ha_before = _wrap_hours_signed(lst_before - ra_before_hours)
    ha_after = _wrap_hours_signed(lst_after - ra_after_hours)
    delta_ha_arcsec = _wrap_hours_signed(ha_after - ha_before) * 15.0 * 3600.0
    delta_dec_arcsec = (dec_after_deg - dec_before_deg) * 3600.0
    angular_separation_arcsec = _angular_separation_arcsec(ha_before, dec_before_deg, ha_after, dec_after_deg)
    observed_ra_drift_hours = _wrap_hours_signed(ra_after_hours - ra_before_hours)
    expected_ra_drift_hours = (elapsed_s * SIDEREAL_RATE_RATIO / 3600.0
                                if track_state_before == "OFF" else None)

    base_kwargs = dict(
        ra_before_hours=ra_before_hours, ra_after_hours=ra_after_hours,
        dec_before_deg=dec_before_deg, dec_after_deg=dec_after_deg,
        lst_before_hours=lst_before, lst_after_hours=lst_after,
        ha_before_hours=ha_before, ha_after_hours=ha_after,
        delta_ha_arcsec=delta_ha_arcsec, delta_dec_arcsec=delta_dec_arcsec,
        elapsed_s=elapsed_s, expected_ra_drift_hours=expected_ra_drift_hours,
        observed_ra_drift_hours=observed_ra_drift_hours, tolerance_arcsec=tolerance_arcsec,
        angular_separation_arcsec=angular_separation_arcsec,
        track_state_before=track_state_before, track_state_after=track_state_after,
        site_lat_deg=site_lat_deg, site_lon_deg=site_lon_deg,
    )

    if track_state_before != "OFF" or track_state_after != "OFF":
        return MotionCheckResult(
            verdict="INCONCLUSIVE",
            reason=(f"physical-motion model is only validated for TELESCOPE_TRACK_STATE=OFF "
                    f"throughout (real hardware evidence collected only for that case so far); "
                    f"observed track_state_before={track_state_before!r} "
                    f"track_state_after={track_state_after!r} - HA/DEC numbers are still reported "
                    f"below for future analysis, but no PASS/FAIL claim is made on them"),
            **base_kwargs,
        )

    if angular_separation_arcsec > tolerance_arcsec:
        return MotionCheckResult(
            verdict="FAIL",
            reason=(f'angular separation {angular_separation_arcsec:.2f}" exceeds the '
                    f'{tolerance_arcsec:.2f}" tolerance for a mount with TRACK_STATE=OFF throughout - '
                    f"real physical motion, not explainable by sidereal drift or driver quantization"),
            **base_kwargs,
        )

    return MotionCheckResult(
        verdict="PASS",
        reason=(f'angular separation {angular_separation_arcsec:.3f}" is within the '
                f'{tolerance_arcsec:.1f}" tolerance derived from the driver\'s observed ~15" '
                f"(1 RA-second) coordinate-reporting resolution - consistent with a mechanically "
                f"idle mount, no real motion detected"),
        **base_kwargs,
    )
