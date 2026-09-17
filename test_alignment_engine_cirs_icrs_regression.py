"""Pre-hardware pass, item 3: regression test + technical documentation for
the CIRS/SkyOffsetFrame geometry finding.

ROOT CAUSE (precisely reproduced here, not assumed - see each test below
for the exact numbers): astropy.coordinates.SkyOffsetFrame's `origin` and
`obstime` constructor parameters are INDEPENDENT. When you build
`SkyOffsetFrame(origin=some_cirs_coord)` without ALSO passing
`obstime=...`, the new offset frame's own `obstime` attribute silently
defaults to the class default (J2000.000) - it does NOT inherit
`some_cirs_coord.obstime`. Since CIRS<->ICRS is itself an obstime-dependent
transform (precession/nutation/frame bias accumulate over time), building
the offset frame at the WRONG obstime and then converting to ICRS
introduces a real geometric error whose size scales with how far the real
observation time is from J2000 (confirmed: for 2026, off by ~0.15deg;
sanity-checks against the ~50 arcsec/year precession rate x ~26 years =
~0.36deg order of magnitude).

This is demonstrated two independent ways below, both converging to
agreement at ~1e-13deg:
  (1) pass `obstime=` explicitly to SkyOffsetFrame, or
  (2) convert the origin to ICRS BEFORE building the offset frame (ICRS
      has no obstime attribute, so the ambiguity cannot arise) - this is
      the path alignment_engine already uses everywhere (targets/solar.py,
      engine.py's reference_center), so production code was never exposed
      to this even before this investigation.

VERDICT: this is NOT a generic "Astropy bug" (SkyOffsetFrame's decoupled
origin/obstime parameters are a reasonable, documented API choice) - it is
a specific frame/obstime COMBINATION PITFALL when constructing an offset
frame from an already obstime-dependent frame (CIRS) without threading
obstime through. Calling it "the Astropy bug" without this precision would
itself be exactly the kind of unverified claim this pass's own instruction
("no afirmes bug de Astropy salvo que puedas demostrarlo bien") warns
against - which is also why a prior verbal description of this finding
(quoting an ~80deg discrepancy) is corrected here to the actual, carefully
reproduced number: ~0.15deg for the present epoch, not ~80deg. The
underlying conclusion (never build a SkyOffsetFrame directly from a CIRS
origin; always use ICRS for tangent-plane geometry) is unchanged - only
the magnitude claim is corrected, with fresh evidence.

IMPACT ON alignment.py V2 (existing, not modified by this pass):
alignment.py's own multiscale_pattern()/offset_coordinates() take whatever
`center` SkyCoord is handed to them. Its Sun alignment path
(search for "sun_eod" in alignment.py) already happens to convert through
`.icrs` at the relevant call site in the same way this package does, which
is presumably why its own field-hardware tests (data/alignment/
ALMITA-ALIGNMENT-V2-SUN-HARDWARE-TEST-01/02) never tripped over this - but
that was not independently re-audited end-to-end as part of this pass
(alignment.py is explicitly not touched here); it is flagged as an open
question in the report rather than asserted either way.
"""
import warnings

import astropy.units as u
from astropy.coordinates import SkyCoord, SkyOffsetFrame
from astropy.time import Time

warnings.filterwarnings("ignore", module="astropy")

from alignment import sun_eod, offset_coordinates
from alignment_engine.targets.solar import SolarTarget


def test_cirs_origin_without_explicit_obstime_has_a_real_nonzero_bias():
    """The failure mode, precisely reproduced: a ZERO offset from a CIRS
    origin should land exactly on the origin (0deg separation) - it does
    not, when the offset frame is built without threading obstime through."""
    obstime = Time.now()
    cirs_center = sun_eod(obstime)
    frame_default_obstime = SkyOffsetFrame(origin=cirs_center)  # obstime NOT passed
    zero_offset = SkyCoord(lon=0 * u.deg, lat=0 * u.deg, frame=frame_default_obstime).icrs
    bias_deg = cirs_center.separation(zero_offset).deg
    # Real, reproducible, and non-trivial relative to this package's own
    # 0.1deg precision goal - not astropy floating-point noise (~1e-13deg).
    assert bias_deg > 0.05
    assert bias_deg < 1.0  # sanity ceiling - not the wild ~80deg figure once assumed


def test_explicit_obstime_on_the_offset_frame_eliminates_the_bias():
    """Fix #1: thread obstime through explicitly."""
    obstime = Time.now()
    cirs_center = sun_eod(obstime)
    frame_with_obstime = SkyOffsetFrame(origin=cirs_center, obstime=obstime)
    zero_offset = SkyCoord(lon=0 * u.deg, lat=0 * u.deg, frame=frame_with_obstime).icrs
    assert cirs_center.separation(zero_offset).deg < 1e-9

    five_deg_offset = SkyCoord(lon=5 * u.deg, lat=0 * u.deg, frame=frame_with_obstime).icrs
    assert abs(cirs_center.separation(five_deg_offset).deg - 5.0) < 1e-9


def test_converting_origin_to_icrs_first_also_eliminates_the_bias():
    """Fix #2 (the one this package actually uses everywhere): sidestep
    the whole obstime-inheritance question by converting to a frame
    (ICRS) that has no obstime attribute at all before building any
    SkyOffsetFrame - this is exactly what offset_coordinates() combined
    with an ICRS `center` argument does."""
    obstime = Time.now()
    cirs_center = sun_eod(obstime)
    icrs_center = cirs_center.icrs

    zero_offset = offset_coordinates(icrs_center, [0.0], [0.0])[0]
    assert icrs_center.separation(zero_offset).deg < 1e-9

    five_deg_offset = offset_coordinates(icrs_center, [5.0], [0.0])[0]
    assert abs(icrs_center.separation(five_deg_offset).deg - 5.0) < 1e-9


def test_solar_target_resolve_offset_uses_the_safe_icrs_path():
    """Regression guard on the actual production code path
    (alignment_engine/targets/solar.py): SolarTarget.resolve_offset must
    never regress to building a SkyOffsetFrame directly from the raw CIRS
    current_position() - it must convert to ICRS first, exactly as its
    own docstring documents."""
    import astropy.units as u
    from astropy.coordinates import EarthLocation

    location = EarthLocation(lat=-33.4489 * u.deg, lon=-70.6693 * u.deg, height=570 * u.m)
    target = SolarTarget(location)
    obstime = Time.now()
    raw_cirs_center = target.current_position(obstime)
    resolved = target.resolve_offset(5.0, 0.0, obstime)
    icrs_center = raw_cirs_center.icrs
    # Must match the known-safe path to within floating point, not the
    # ~0.15deg CIRS-without-obstime bias this test file demonstrates above.
    safe_reference = offset_coordinates(icrs_center, [5.0], [0.0])[0]
    assert resolved.separation(safe_reference).deg < 1e-6
