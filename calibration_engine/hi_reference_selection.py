"""HI ALTO / HI BAJO candidate selection for the CALIBRATE reference wizard's relative spectral-contrast
procedure. Proposes two observable sky zones - one where the real HI4PI map predicts a strong line, one where
it predicts a weak one - for the OPERATOR to approve before any real GOTO. Reuses the SAME real map, observer
location, and astronomical machinery ALIGN's own sky view and OBSERVE's gain-pilot stage already use:
hi4pi_map.py (HI4PI Collaboration 2016, A&A 594, A116 - see that module's own docstring for source/license) via
its already-public _load()/_smoothed() (the same functions observation_gain_pilot.py imports directly - not a
new pattern), observer_config.json for the real site location (the same file almita_web_ops.align_sky() reads),
and plain astropy AltAz transforms for the real, no-hardware altitude/temporal checks.

WHY MEAN, NOT CONTRAST: hi4pi_map.sky_grid_and_ranking() (ALIGN's own candidate ranking) scores candidates by
LOCAL CONTRAST (std x log1p(mean)) within a patch - the right criterion for finding sky structure a beam-fitting
alignment can lock onto. This module wants the opposite kind of candidate: a region whose BEAM-AVERAGED N_HI is
as high, or as low, as the real sky currently offers. hi4pi_map._smoothed(beam_fwhm_deg) already IS that
beam-averaged map (a Gaussian convolution at the beam's own FWHM) - a single pixel lookup in it, at a
candidate's projected position, is the beam-averaged value there; no second, differently-shaped (top-hat)
local average is needed on top of it. Candidate values are read via ONE vectorized wcs.all_world2pix() call
over the whole candidate array (the same vectorized-lookup pattern sky_grid_and_ranking() itself uses for its
raster), not a per-candidate scalar astropy call in a Python loop - the difference between ~1s and ~45s at a
realistic candidate count, confirmed while building this module.

HONESTY BOUNDARY (see calibration_engine/reference_wizard.py's module docstring for the rest of it): the HI4PI
column density predicts NEITHER the SDR/ADC power NOR the measured spectral line flux directly - it orients
WHICH sky zones to point at, nothing more. The actual measurement always comes from a real capture, analyzed by
alignment_engine.hi.spectral_pipeline's own real HI-line metric (reused, not reimplemented here either).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent.parent


class NoHIContrastAvailable(Exception):
    """The real sky right now offers no defensible HIGH-vs-LOW N_HI split (e.g. the map is unavailable, or
    every visible point has essentially the same value) - callers must say so plainly, never invent a split."""


def _observer_location():
    import json
    import astropy.units as u
    from astropy.coordinates import EarthLocation
    obs = json.loads((ROOT / "observer_config.json").read_text())["observer"]
    return EarthLocation(lat=obs["latitude_deg"] * u.deg, lon=obs["longitude_deg"] * u.deg, height=obs["elevation_m"] * u.m)


def point_stays_above_elevation(ra_hours: float, dec_deg: float, min_elevation_deg: float, hold_seconds: float,
                                location=None, obstime=None) -> Dict[str, Any]:
    """Real, no-hardware check: does this point's altitude stay >= min_elevation_deg from now through
    now+hold_seconds (the time it would actually be occupied for GOTO+settle+the reference's captures)? Checked
    at both ends of the window (short holds - a handful of minutes at most - are effectively monotonic over that
    span at any real observing latitude/declination, so the two endpoints are the genuine worst case, not an
    approximation that hides a mid-window dip)."""
    import astropy.units as u
    from astropy.coordinates import AltAz, ICRS, SkyCoord
    from astropy.time import Time, TimeDelta
    location = location if location is not None else _observer_location()
    obstime = obstime if obstime is not None else Time.now()
    coord = SkyCoord(ra=ra_hours * 15 * u.deg, dec=dec_deg * u.deg, frame=ICRS())
    end = obstime + TimeDelta(hold_seconds, format="sec")
    alt_now = float(coord.transform_to(AltAz(obstime=obstime, location=location)).alt.deg)
    alt_end = float(coord.transform_to(AltAz(obstime=end, location=location)).alt.deg)
    worst = min(alt_now, alt_end)
    return {"altitude_now_deg": alt_now, "altitude_at_end_deg": alt_end, "worst_case_altitude_deg": worst,
            "min_elevation_deg": min_elevation_deg, "margin_deg": worst - min_elevation_deg,
            "clears": worst >= min_elevation_deg, "hold_seconds": hold_seconds,
            "checked_utc": obstime.utc.isot + "Z"}


# OnStep stops tracking once a target is this far past the meridian ("Meridian Limit (W) Exceeded"): measured
# 2026-10-13 19:31:59, the HI ALTO target at hour angle +0.202 h (BL-060). The error then stays latched in OnStep
# and ALMITA refuses to move until it is cleared (tracking OFF then ON in Ekos).
ONSTEP_MERIDIAN_LIMIT_MIN = 12.0
# An approved plan is used later than it is proposed (2026-10-13: approved 18:53, captured 19:31): a zone that would
# reach the limit within this long after PROPOSE is not offered.
PLAN_MERIDIAN_LOOKAHEAD_S = 45 * 60
SIDEREAL_RATE = 1.0027379        # hour angle advances this many hours per hour


def hour_angle_hours(ra_hours, location, obstime):
    """Hour angle in hours, wrapped to [-12, 12): negative = east of the meridian (still rising), positive = past it."""
    lst = obstime.sidereal_time("apparent", longitude=location.lon).hour
    return (np.asarray(lst - np.asarray(ra_hours)) + 12.0) % 24.0 - 12.0


def meridian_limit_check(ra_hours: float, hold_seconds: float, location=None, obstime=None,
                         limit_min: float = ONSTEP_MERIDIAN_LIMIT_MIN) -> Dict[str, Any]:
    """Does this point cross OnStep's west meridian limit between now and now+hold_seconds? A point already past the
    limit at GOTO time is not a crossing (OnStep slews it on the other side of the pier); a point that reaches the
    limit WHILE it is being held stops the tracking mid-step - that is what failed on 2026-10-13."""
    from astropy.time import Time
    location = location if location is not None else _observer_location()
    obstime = obstime if obstime is not None else Time.now()
    ha0 = float(hour_angle_hours(ra_hours, location, obstime))
    ha1 = ha0 + hold_seconds / 3600.0 * SIDEREAL_RATE
    lim = limit_min / 60.0
    crosses = ha0 < lim <= ha1
    to_limit = (lim - ha0) * 60.0 / SIDEREAL_RATE if ha0 < lim else None
    return {"hour_angle_now_h": ha0, "hour_angle_at_end_h": ha1, "limit_min": limit_min, "hold_seconds": hold_seconds,
            "minutes_to_limit": to_limit, "crosses_limit": bool(crosses), "clears": not crosses,
            "checked_utc": obstime.utc.isot + "Z"}


@dataclass
class HICandidate:
    label: str                       # "HI_ALTO" | "HI_BAJO"
    ra_hours: float
    dec_deg: float
    alt_deg: float
    az_deg: float
    mean_n_hi_1e20cm2: float         # real HI4PI beam-averaged column density at this point - ORIENTATION ONLY
    reason: str
    altitude_check: Dict[str, Any]
    meridian_check: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "ra_hours": self.ra_hours, "dec_deg": self.dec_deg, "alt_deg": self.alt_deg,
                "az_deg": self.az_deg, "mean_n_hi_1e20cm2": self.mean_n_hi_1e20cm2, "reason": self.reason,
                "altitude_check": self.altitude_check, "meridian_check": self.meridian_check}


def select_hi_alto_bajo_candidates(min_elevation_deg: float, beam_fwhm_deg: float, hold_seconds: float,
                                   grid_n: int = 180, min_separation_beams: float = 2.0,
                                   fixed_high: Optional[Dict[str, Any]] = None,
                                   meridian_lookahead_s: float = PLAN_MERIDIAN_LOOKAHEAD_S) -> Dict[str, Any]:
    """Real, no-hardware computation. Rasters the currently-visible sky (same disk-projection convention as
    hi4pi_map.sky_grid_and_ranking()), reads the real HI4PI beam-smoothed map at each grid point (beam_fwhm_deg -
    "considera el haz ancho"), and proposes the highest-mean point as HI_ALTO and the lowest-mean (but still
    finite/real, never a no-data pixel) as HI_BAJO, each required to clear min_elevation_deg for the whole
    hold_seconds window ("considera... altitud, duracion") and to sit at least min_separation_beams beam widths
    apart from each other (so they are genuinely distinct sky regions, not the same patch twice - "considera...
    recorrido" is disclosed as the real angular separation/slew between them). Raises NoHIContrastAvailable if
    the map is unreadable or the visible sky right now offers no real split - never invents one.

    RFI is NOT predicted here: this project has no per-sky-position RFI forecast (RFI_REF is a separate,
    independent receiver, not a sky map). "considera RFI" is honored at the RESULT stage instead, via each real
    capture's own bandpass usable-band-fraction proxy (the same RFI proxy used everywhere else in this
    codebase) - never guessed in advance from the HI4PI column density, which has nothing to do with RFI."""
    import astropy.units as u
    from astropy.coordinates import AltAz, ICRS, SkyCoord
    from astropy.time import Time
    import hi4pi_map

    location = _observer_location()
    now = Time.now()
    try:
        data, wcs, _pix_scale_deg = hi4pi_map._load()
        smoothed = hi4pi_map._smoothed(beam_fwhm_deg)
    except hi4pi_map.HI4PIUnavailable as exc:
        raise NoHIContrastAvailable(f"real HI4PI map unavailable ({exc}) - manual candidate entry is not offered "
                                    "by this wizard; fix/download the map first (fetch_hi4pi_map.py)") from exc

    lin = np.linspace(-1, 1, grid_n)
    px, py = np.meshgrid(lin, lin)
    r = np.hypot(px, py)
    inside = r <= 1.0
    alt = 90.0 * (1.0 - r[inside])
    az = np.degrees(np.arctan2(px[inside], -py[inside])) % 360.0
    candidate_mask = alt >= min_elevation_deg
    if not candidate_mask.any():
        raise NoHIContrastAvailable(f"no point of the visible sky currently clears min_elevation_deg={min_elevation_deg:g}")
    alt_c, az_c = alt[candidate_mask], az[candidate_mask]
    altaz = SkyCoord(AltAz(az=az_c * u.deg, alt=alt_c * u.deg, obstime=now, location=location))
    icrs = altaz.transform_to(ICRS())
    ra_deg, dec_deg = icrs.ra.deg, icrs.dec.deg

    # Vectorized: ONE wcs.all_world2pix() call over the whole candidate array (not one scalar astropy call per
    # candidate in a Python loop - confirmed ~45x faster at a realistic ~15000-candidate count while building
    # this module), then a plain numpy index into the already beam-smoothed map - see module docstring.
    x_pix, y_pix = wcs.all_world2pix(ra_deg, dec_deg, 0)
    ny, nx = smoothed.shape
    xi = np.mod(np.round(x_pix).astype(int), nx)
    yi = np.round(y_pix).astype(int)
    valid = (yi >= 0) & (yi < ny)
    means = np.full(ra_deg.shape, np.nan)
    means[valid] = smoothed[yi[valid], xi[valid]]
    # BL-060: never offer a zone that reaches OnStep's meridian limit between PROPOSE and the end of its hold
    ha = hour_angle_hours(ra_deg / 15.0, location, now)
    lim = ONSTEP_MERIDIAN_LIMIT_MIN / 60.0
    meridian_bad = (ha < lim) & (ha + (meridian_lookahead_s + hold_seconds) / 3600.0 * SIDEREAL_RATE >= lim)
    finite = np.isfinite(means) & (means >= 0) & ~meridian_bad
    if finite.sum() < 2:
        raise NoHIContrastAvailable("fewer than 2 visible points had a real, finite HI4PI value - cannot propose a HIGH/LOW pair")
    order = np.argsort(np.where(finite, means, -np.inf))[::-1]        # descending mean, non-finite last

    min_sep_deg = min_separation_beams * beam_fwhm_deg
    high_i = int(order[0])
    high_coord = SkyCoord(ra=ra_deg[high_i] * u.deg, dec=dec_deg[high_i] * u.deg)
    if fixed_high is not None:          # RE-PROPOSE after HI ALTO was measured: only HI BAJO is chosen again, away from it
        high_coord = SkyCoord(ra=fixed_high["ra_hours"] * 15 * u.deg, dec=fixed_high["dec_deg"] * u.deg)
    # Vectorized separation from HI ALTO to every candidate at once (astropy SkyCoord array ops are numpy-
    # vectorized internally), instead of a per-candidate scalar .separation() call in a Python loop.
    all_coords = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    sep_from_high_deg = all_coords.separation(high_coord).deg
    far_enough = finite & (sep_from_high_deg >= min_sep_deg)
    if not far_enough.any():
        raise NoHIContrastAvailable(f"no visible point at least {min_sep_deg:g} deg ({min_separation_beams:g} beam widths) "
                                    "from the HI ALTO candidate has a real, finite HI4PI value - the currently visible sky "
                                    "is too small/uniform for a defensible HIGH/LOW split right now")
    low_i = int(np.argmin(np.where(far_enough, means, np.inf)))

    def _row(i: int, label: str, other_ra_dec: Optional[SkyCoord]) -> HICandidate:
        check = point_stays_above_elevation(ra_deg[i] / 15.0, dec_deg[i], min_elevation_deg, hold_seconds, location, now)
        reason = (f"real HI4PI beam-averaged N_HI = {means[i]:.3f} (x1e20 cm^-2, beam FWHM {beam_fwhm_deg:g} deg) - "
                  f"{'highest' if label == 'HI_ALTO' else 'lowest'} among {int(finite.sum())} visible points considered; "
                  f"altitude margin {check['margin_deg']:+.1f} deg over {hold_seconds:g} s hold")
        if other_ra_dec is not None:
            sep = SkyCoord(ra=ra_deg[i] * u.deg, dec=dec_deg[i] * u.deg).separation(other_ra_dec).deg
            reason += f"; {sep:.1f} deg from the other candidate ({sep / beam_fwhm_deg:.1f} beam widths - real slew distance)"
        mcheck = meridian_limit_check(ra_deg[i] / 15.0, hold_seconds, location, now)
        if mcheck["minutes_to_limit"] is not None and mcheck["minutes_to_limit"] < 6 * 60:
            reason += f"; reaches OnStep's meridian limit in {mcheck['minutes_to_limit']:.0f} min"
        return HICandidate(label=label, ra_hours=float(ra_deg[i] / 15.0), dec_deg=float(dec_deg[i]),
                           alt_deg=float(alt_c[i]), az_deg=float(az_c[i]), mean_n_hi_1e20cm2=float(means[i]),
                           reason=reason, altitude_check=check, meridian_check=mcheck)

    hi_alto = _row(high_i, "HI_ALTO", None)
    hi_bajo = _row(low_i, "HI_BAJO", high_coord)
    if fixed_high is not None:
        hi_alto = HICandidate(**{k: fixed_high[k] for k in ("label", "ra_hours", "dec_deg", "alt_deg", "az_deg", "mean_n_hi_1e20cm2",
                                                             "reason", "altitude_check")}, meridian_check=fixed_high.get("meridian_check"))
    if fixed_high is None and not hi_alto.altitude_check["clears"]:
        raise NoHIContrastAvailable(f"HI ALTO candidate would not stay above min_elevation_deg for the whole "
                                    f"{hold_seconds:g} s hold ({hi_alto.altitude_check})")
    if not hi_bajo.altitude_check["clears"]:
        raise NoHIContrastAvailable(f"HI BAJO candidate would not stay above min_elevation_deg for the whole "
                                    f"{hold_seconds:g} s hold ({hi_bajo.altitude_check})")

    return {"candidates": {"HI_ALTO": hi_alto.to_dict(), "HI_BAJO": hi_bajo.to_dict()},
            "generated_utc": now.utc.isot + "Z", "grid_points_considered": int(finite.sum()),
            "beam_fwhm_deg": beam_fwhm_deg, "min_elevation_deg": min_elevation_deg, "hold_seconds": hold_seconds,
            "min_separation_deg": min_sep_deg, "source": hi4pi_map.SOURCE,
            "meridian_limit_min": ONSTEP_MERIDIAN_LIMIT_MIN, "meridian_lookahead_s": meridian_lookahead_s,
            "meridian_excluded_points": int(meridian_bad.sum()), "hi_alto_kept_from_previous_plan": fixed_high is not None,
            "rfi_note": "RFI is not predicted here (no per-sky-position RFI forecast exists in this project) - "
                        "each real capture's own bandpass usable-band-fraction proxy reports RFI-proxy findings "
                        "after the fact, at the RESULT step."}
