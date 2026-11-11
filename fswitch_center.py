"""BL-105 (operator 2026-11-10: "comienza de inmediato ... tenemos que hacerlo"): frequency switching with a
configurable CENTRE VELOCITY.

Plain frequency switching (fswitch_capture.py, BL-100) straddles the operating tuning f0 = 1420.405752 MHz: the folded
spectrum covers f0 +/- 0.4 MHz = +/-84 km/s topocentric around 0. That misses the terminal velocities of the inner
Milky Way (up to ~150-200 km/s in a 20 deg beam) and the Magellanic Clouds (+160 / +270 km/s). This module chooses,
per point, where to put that +/-84 km/s window (+/-148 with a +/-300 kHz offset) and turns it into the SDR tuning:

  - centre velocity, LSRK: a fixed number (e.g. +270 for the LMC), or "auto" - from a flat-rotation-curve model of
    the Milky Way's HI seen through the beam, the window is moved toward the extreme (terminal) side so it holds the
    line's edge plus a line-free margin beyond it (that margin is where the folded baseline is measured);
  - LSRK -> topocentric with the SAME astropy shift REDUCE applies afterwards (alignment_engine.hi.velocity.
    frame_shift_km_s), so the window lands where REDUCE will look;
  - topocentric velocity -> sky frequency with the radio convention REDUCE uses (hi_spectral_metric):
    f = f_rest * (1 - v / c).

It also returns the expected LINE WINDOW (topocentric, km/s): REDUCE's fold must not fit its baseline where the line
is (reduce_engine/fswitch_fold.py reads it from the capture's attributes).

Pure planning: nothing here touches the SDR. The tuning itself still goes through capture.py -> fswitch_capture ->
SDRCapture.configure, with every LO change confirmed in rtl_tcp's own log (the one-frequency-source rule)."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

C_KM_S = 299792.458
HI_REST_HZ = 1420405751.77          # REDUCE's hi_rest_frequency_hz (reduce_engine.config)
SWITCH_OFFSET_HZ = 600e3            # fswitch_capture.DEFAULT_OFFSET_HZ
USABLE_HZ = 1.0e6                   # reduce_engine.fswitch_fold.USABLE_HZ
HALF_WINDOW_KM_S = (USABLE_HZ - SWITCH_OFFSET_HZ) / HI_REST_HZ * C_KM_S       # 84.4 km/s: the folded half-width
# Configurable throw (operator 2026-11-11, for the Magellanic Clouds): with the default +/-600 kHz (253 km/s) the
# Milky Way's local gas at ~0 km/s puts its negative ghost at ~+253 km/s - on the LMC. +/-300 kHz (127 km/s) moves it
# to ~+127 and widens the folded window to +/-148 km/s. Below ~250 kHz a line's own ghosts crowd it; above ~700 kHz
# the folded window gets too narrow to hold any line plus a baseline.
MIN_OFFSET_HZ, MAX_OFFSET_HZ = 250e3, 700e3
EDGE_MARGIN_KM_S = 40.0             # line-free velocities kept beyond the predicted edge (folded baseline lives there)
DISPERSION_KM_S = 20.0              # added on both sides of the model's velocities (cloud motions, ~7-10 km/s rms x2)
LOCAL_KM_S = 25.0                   # local gas: always assumed present within +/- this
R0_KPC, V0_KM_S = 8.2, 220.0        # Sun's galactocentric radius and circular speed (flat curve)
RMAX_KPC, ZMAX_KPC = 20.0, 0.5      # HI disc: out to 20 kpc, within 0.5 kpc of the plane


def gal_from_equatorial(ra_hours: float, dec_deg: float) -> Tuple[float, float]:
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    g = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg, frame="icrs").galactic
    return float(g.l.deg), float(g.b.deg)


def _los_velocities(l_deg: float, b_deg: float, n: int = 400) -> np.ndarray:
    """LSR radial velocities of gas in circular rotation (flat curve) along one line of sight, inside the HI disc."""
    l, b = math.radians(l_deg), math.radians(b_deg)
    dmax = 40.0
    d = np.linspace(0.0, dmax, n)
    R = np.sqrt(R0_KPC ** 2 + (d * math.cos(b)) ** 2 - 2 * R0_KPC * d * math.cos(b) * math.cos(l))
    ok = (R <= RMAX_KPC) & (np.abs(d * math.sin(b)) <= ZMAX_KPC) & (R > 0.3)
    R = R[ok]
    return V0_KM_S * (R0_KPC / R - 1.0) * math.sin(l) * math.cos(b)


def model_line_window_lsrk(l_deg: float, b_deg: float, beam_fwhm_deg: float = 20.0,
                           step_deg: float = 2.0) -> Tuple[float, float]:
    """(vmin, vmax) LSRK km/s where HI is expected inside the beam (out to its half power radius): flat rotation
    curve + dispersion, local gas always included. A planning model, deliberately generous - never a measurement."""
    vmin, vmax = -LOCAL_KM_S, LOCAL_KM_S
    r = beam_fwhm_deg / 2.0
    k = int(math.floor(r / step_deg))
    offsets = [i * step_deg for i in range(-k, k + 1)] + [-r, r]        # always the centre, always the half-power ring
    for dl in offsets:
        for db in offsets:
            if dl * dl + db * db > r * r:
                continue
            bb = b_deg + db
            if abs(bb) > 89.0:
                continue
            v = _los_velocities((l_deg + dl / max(math.cos(math.radians(bb)), 0.05)) % 360.0, bb)
            if v.size:
                vmin, vmax = min(vmin, float(v.min()) - DISPERSION_KM_S), max(vmax, float(v.max()) + DISPERSION_KM_S)
    return vmin, vmax


def choose_centre_lsrk(window: Tuple[float, float], l_deg: Optional[float] = None, half_km_s: float = HALF_WINDOW_KM_S,
                       margin_km_s: float = EDGE_MARGIN_KM_S) -> float:
    """Where to centre the +/-half window: 0 when the whole line (plus margin) already fits - the plain BL-100 set-up;
    otherwise toward the TERMINAL side - positive in the first quadrant (0 < l < 90), negative in the fourth
    (270 < l < 360), where the tangent-point gas of the rotation curve is - or, elsewhere, the larger extreme;
    keeping `margin` of line-free velocities beyond that edge."""
    vmin, vmax = window
    if vmin - margin_km_s >= -half_km_s and vmax + margin_km_s <= half_km_s:
        return 0.0
    if l_deg is not None and 0.0 < l_deg % 360.0 < 90.0:
        positive = True
    elif l_deg is not None and 270.0 < l_deg % 360.0 < 360.0:
        positive = False
    else:
        positive = abs(vmax) >= abs(vmin)
    return vmax + margin_km_s - half_km_s if positive else vmin - margin_km_s + half_km_s


def parse_offset_hz(value: Any) -> Optional[float]:
    """None (the default +/-600 kHz) or the switch offset in Hz (LOs at centre -/+ offset). Never guesses."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    v = float(value)
    if not math.isfinite(v) or not MIN_OFFSET_HZ <= v <= MAX_OFFSET_HZ:
        raise ValueError(f"frequency-switching offset {value!r} Hz: must be within {MIN_OFFSET_HZ / 1e3:.0f}-"
                         f"{MAX_OFFSET_HZ / 1e3:.0f} kHz (LOs at centre -/+ offset)")
    return v


def half_window_km_s(offset_hz: Optional[float] = None) -> float:
    """Half-width of the folded spectrum (km/s) for a switch offset: the usable baseband minus the offset."""
    return (USABLE_HZ - float(offset_hz or SWITCH_OFFSET_HZ)) / HI_REST_HZ * C_KM_S


def throw_km_s(offset_hz: Optional[float] = None) -> float:
    """The switch throw (2 x offset) in velocity: where a line's two negative half-height ghosts land, +/- this."""
    return 2.0 * float(offset_hz or SWITCH_OFFSET_HZ) / HI_REST_HZ * C_KM_S


def local_gas_ghosts_lsrk(offset_hz: Optional[float] = None) -> List[Tuple[float, float]]:
    """Where the Milky Way's local gas (always present, +/-LOCAL_KM_S LSRK) puts its ghosts in the folded spectrum."""
    t = throw_km_s(offset_hz)
    return [(-LOCAL_KM_S - t, LOCAL_KM_S - t), (-LOCAL_KM_S + t, LOCAL_KM_S + t)]


def _overlaps(a: Tuple[float, float], b: Tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def lsrk_shift_km_s(ra_hours: float, dec_deg: float, when_utc, lat_deg: float, lon_deg: float,
                    elev_m: float = 0.0) -> float:
    """v_LSRK - v_topo for this direction, instant and place: the same astropy shift REDUCE adds to its
    topocentric velocity axis (reduce_engine.velocity -> alignment_engine.hi.velocity.frame_shift_km_s)."""
    from astropy import units as u
    from astropy.coordinates import EarthLocation, SkyCoord
    from astropy.time import Time
    import reduce_engine.velocity  # noqa: F401  - the same offline IERS settings REDUCE runs with
    from alignment_engine.hi.velocity import frame_shift_km_s
    when = Time(when_utc, scale="utc") if not isinstance(when_utc, Time) else when_utc
    loc = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elev_m * u.m)
    target = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg, frame="icrs")
    return float(frame_shift_km_s(frame="lsrk", obstime=when, location=loc, target=target))


def velocity_to_frequency_hz(v_topo_km_s: float) -> float:
    return HI_REST_HZ * (1.0 - v_topo_km_s / C_KM_S)


def parse_centre(value: Any) -> Optional[Any]:
    """None (plain BL-100), "auto", or a float (km/s LSRK). Raises on anything else - never guesses."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    v = float(value)
    if not math.isfinite(v) or abs(v) > 600.0:
        raise ValueError(f"frequency-switching centre velocity {value!r}: must be 'auto' or a number within +/-600 km/s")
    return v


def plan_point(centre: Any, ra_hours: float, dec_deg: float, when_utc, observer: Dict[str, Any],
               beam_fwhm_deg: float = 20.0, offset_hz: Optional[float] = None) -> Dict[str, Any]:
    """Everything one switched point needs: the sky frequency to straddle, and what to record with the capture.

    offset_hz: the switch offset (None = the default +/-600 kHz). It sets the folded half-width and where the local
    gas's ghosts land; when the target is not the local gas itself, those ghosts and the local gas are recorded as
    MASK windows (fswitch_mask_windows_topo_km_s) so REDUCE never fits its baseline on them, and a ghost landing in
    the expected line window is flagged (fswitch_local_ghost_in_line_window) - the reason the LMC needs +/-300 kHz."""
    offset = parse_offset_hz(offset_hz) or SWITCH_OFFSET_HZ
    half = half_window_km_s(offset)
    mode = parse_centre(centre)
    if mode is None:
        raise ValueError("plan_point needs a centre: 'auto' or a velocity in km/s (LSRK)")
    shift = lsrk_shift_km_s(ra_hours, dec_deg, when_utc, float(observer["latitude_deg"]),
                            float(observer["longitude_deg"]), float(observer.get("elevation_m", 0.0)))
    l, b = gal_from_equatorial(ra_hours, dec_deg)
    if mode == "auto":
        window = model_line_window_lsrk(l, b, beam_fwhm_deg)
        c_lsrk = choose_centre_lsrk(window, l, half_km_s=half)
        line_source = "flat-rotation-curve model through the beam (fswitch_center.model_line_window_lsrk)"
    else:
        c_lsrk = float(mode)
        # BL-100's own assumption (+/-80 km/s), never wider than the folded window minus 4 km/s
        w = min(HALF_WINDOW_KM_S, half) - 4.0
        window = (c_lsrk - w, c_lsrk + w)
        line_source = f"fixed centre: line assumed within +/-{w:.0f} km/s of it (as plain BL-100 does around 0)"
    # the local gas and its two ghosts: never baseline there, unless the line window already holds the local gas
    local = (-LOCAL_KM_S, LOCAL_KM_S)
    masks: List[Tuple[float, float]] = []
    ghost_in_line = False
    if not (window[0] <= local[0] and local[1] <= window[1]):
        masks = [local] + local_gas_ghosts_lsrk(offset)
        ghost_in_line = any(_overlaps(g, window) for g in local_gas_ghosts_lsrk(offset))
    v_topo_centre = c_lsrk - shift
    f_c = int(round(velocity_to_frequency_hz(v_topo_centre)))
    return {
        "centre_frequency_hz": f_c,
        "attrs": {
            "fswitch_centre_mode": "auto" if mode == "auto" else "fixed",
            "fswitch_centre_lsrk_km_s": round(c_lsrk, 3),
            "fswitch_centre_topo_km_s": round(v_topo_centre, 3),
            "fswitch_lsrk_shift_km_s": round(shift, 3),
            "fswitch_line_window_lsrk_km_s": [round(window[0], 2), round(window[1], 2)],
            "fswitch_line_window_topo_km_s": [round(window[0] - shift, 2), round(window[1] - shift, 2)],
            "fswitch_line_window_source": line_source,
            "fswitch_rest_frequency_hz": HI_REST_HZ,
            "fswitch_planned_offset_hz": float(offset),
            "fswitch_half_window_km_s": round(half, 2),
            "fswitch_mask_windows_topo_km_s": [[round(m[0] - shift, 2), round(m[1] - shift, 2)] for m in masks],
            "fswitch_local_ghost_in_line_window": bool(ghost_in_line),
            "galactic_l_deg": round(l, 3), "galactic_b_deg": round(b, 3),
        },
    }
