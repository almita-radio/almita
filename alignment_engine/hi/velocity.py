"""Frequency <-> radio velocity conversion (Fase 6 - "MUY IMPORTANTE").

ALMITA's SDR measures TOPOCENTRIC frequency (the frequency actually
arriving at the antenna, Doppler-shifted by Earth's rotation and orbital
motion, and by the source's own motion). HI surveys typically publish
their spectral axis in a KINEMATIC reference frame (commonly LSRK -
velocity relative to the Local Standard of Rest, kinematic definition) or
occasionally heliocentric/barycentric. These are NOT the same axis, and
treating them as equivalent would misalign every spectral bin comparison.

This module does NOT reinvent Doppler physics: the frame correction
(topocentric -> heliocentric/barycentric/LSRK) is delegated entirely to
astropy's own SpectralCoord machinery
(with_observer_stationary_relative_to), which explicitly documents this as
its purpose ("transformation from telescope-centric... frames to e.g.
Barycentric or Local Standard of Rest... velocity frames"). The only
formula implemented directly here is the IAU "radio velocity convention"
itself (v = c*(f0-f)/f0) - a DEFINITION of how radio astronomy maps
frequency to a velocity-like axis, not a physical effect requiring
astropy's help, and already exactly what astropy's own
`doppler_convention="radio"` argument implements (used here, not
reimplemented by hand).

FAIL SAFE (Fase 6's explicit requirement): an unrecognized/unsupported
spectral frame name raises ValueError immediately - this module never
guesses or silently falls back to topocentric.
"""
from __future__ import annotations

import warnings
from typing import Optional

import astropy.units as u
from astropy.coordinates import EarthLocation, SkyCoord, SpectralCoord
from astropy.time import Time

REST_FREQUENCY_HI_HZ = 1_420_405_751.77  # 1420.40575177 MHz, per project spec

SUPPORTED_FRAMES = frozenset({"topocentric", "heliocentric", "barycentric", "lsrk"})


def observed_frequency_to_velocity(freq_hz: float, *, rest_freq_hz: float,
                                    frame: str, obstime: Time, location: EarthLocation,
                                    target: SkyCoord) -> float:
    """Radio velocity (km/s, IAU radio convention) of `freq_hz` (topocentric,
    as actually measured) relative to `rest_freq_hz`, expressed in
    `frame`. `target` must be a real sky direction (ICRS or convertible)
    - used only to compute the correct projection of the frame-change
    velocity onto this specific line of sight, per astropy's own
    documented mechanism.

    Raises ValueError for any frame not in SUPPORTED_FRAMES - never a
    silent topocentric fallback.
    """
    if frame not in SUPPORTED_FRAMES:
        raise ValueError(f"unsupported spectral frame {frame!r} - supported: {sorted(SUPPORTED_FRAMES)} "
                          f"(fail-safe: refusing to guess)")

    observer = location.get_itrs(obstime=obstime)
    target_icrs = target.icrs if hasattr(target, "icrs") else SkyCoord(target).icrs
    with warnings.catch_warnings():
        # astropy's own NoVelocityWarning here is a documented false alarm
        # for an ITRS-with-obstime observer (see astropy issue #11805) -
        # the frame transform itself still correctly includes Earth's
        # rotation/orbital motion; only the *warning text* is misleading.
        warnings.simplefilter("ignore")
        spectral = SpectralCoord(freq_hz * u.Hz, observer=observer, target=target_icrs)
        if frame != "topocentric":
            # ICRS is centered at the solar-system barycenter (NOT GCRS,
            # which is geocentric - de-rotated but still co-orbiting with
            # Earth, and NOT what "barycentric" means here). Verified
            # against the independent, dedicated
            # SkyCoord.radial_velocity_correction(kind=...) API - see
            # test_alignment_engine_hi_velocity.py.
            target_frame = {"barycentric": "icrs", "heliocentric": "hcrs", "lsrk": "lsrk"}[frame]
            spectral = spectral.with_observer_stationary_relative_to(target_frame)
        velocity = spectral.to(u.km / u.s, doppler_convention="radio", doppler_rest=rest_freq_hz * u.Hz)
    return float(velocity.value)


def frame_shift_km_s(*, frame: str, obstime: Time, location: EarthLocation, target: SkyCoord) -> float:
    """The velocity shift alone (topocentric -> `frame`), independent of
    any particular observed frequency - useful for reporting/QA (e.g.
    "how big a correction did we apply"). topocentric always returns 0.0.
    """
    if frame not in SUPPORTED_FRAMES:
        raise ValueError(f"unsupported spectral frame {frame!r} - supported: {sorted(SUPPORTED_FRAMES)} "
                          f"(fail-safe: refusing to guess)")
    if frame == "topocentric":
        return 0.0
    probe_freq = REST_FREQUENCY_HI_HZ
    topo = observed_frequency_to_velocity(probe_freq, rest_freq_hz=probe_freq, frame="topocentric",
                                           obstime=obstime, location=location, target=target)
    other = observed_frequency_to_velocity(probe_freq, rest_freq_hz=probe_freq, frame=frame,
                                            obstime=obstime, location=location, target=target)
    return other - topo
