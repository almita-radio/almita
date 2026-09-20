"""Fase 46: small synthetic FITS fixtures - never a real survey product -
exercising FITSMomentMapProvider's parsing, coordinates, and target
selection end to end.
"""
import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import EarthLocation
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS

from alignment_engine.hi.fits_reference import (
    FITSMomentMapProvider,
    inspect_fits_moment_map,
    validate_reference,
)
from alignment_engine.hi.reference_trust import ReferenceTrust, build_manifest_for_file

LOCATION = EarthLocation(lat=-33.4331 * u.deg, lon=289.3336 * u.deg, height=550 * u.m)
OBSTIME = Time("2026-09-18T03:00:00")


def _write_galactic_gaussian_fixture(path, center_l=30.0, center_b=0.0, amplitude=5.0, sigma_deg=3.0,
                                      size=181, pixscale_deg=0.2, all_nan=False):
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["GLON-CAR", "GLAT-CAR"]
    wcs.wcs.crpix = [size / 2, size / 2]
    wcs.wcs.crval = [center_l, center_b]
    wcs.wcs.cdelt = [-pixscale_deg, pixscale_deg]
    ys, xs = np.mgrid[0:size, 0:size]
    lon, lat = wcs.wcs_pix2world(xs, ys, 0)
    data = amplitude * np.exp(-0.5 * (((lon - center_l + 180) % 360 - 180) ** 2 + (lat - center_b) ** 2) / sigma_deg ** 2)
    data += 0.05  # small nonzero floor, still physically plausible
    if all_nan:
        data[:] = np.nan
    header = wcs.to_header()
    header["BUNIT"] = "K"
    fits.PrimaryHDU(data=data.astype(np.float32), header=header).writeto(path, overwrite=True)
    return wcs, data


@pytest.fixture
def fixture_fits(tmp_path):
    path = tmp_path / "synthetic_hi_fixture.fits"
    _write_galactic_gaussian_fixture(str(path))
    return str(path)


def test_inspect_reports_real_structure(fixture_fits):
    info = inspect_fits_moment_map(fixture_fits)
    assert info["shape"] == (181, 181)
    assert info["wcs_is_celestial"] is True
    assert info["nan_fraction"] == 0.0
    assert info["finite_max"] > info["finite_min"]


def test_validate_reference_passes_for_a_clean_fixture(tmp_path, fixture_fits):
    manifest = build_manifest_for_file(fixture_fits, survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K",
                                        trust=ReferenceTrust.TEST_FIXTURE)
    result = validate_reference(fixture_fits, manifest)
    assert result.ok, result.reason


def test_validate_reference_fails_checksum_mismatch(tmp_path, fixture_fits):
    manifest = build_manifest_for_file(fixture_fits, survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K")
    # Corrupt the manifest's own recorded hash - simulates a manifest that
    # no longer matches the file it claims to describe.
    manifest.sha256 = "0" * 64
    result = validate_reference(fixture_fits, manifest)
    assert result.ok is False
    assert "checksum" in result.reason


def test_validate_reference_rejects_all_nan_data(tmp_path):
    path = tmp_path / "all_nan.fits"
    _write_galactic_gaussian_fixture(str(path), all_nan=True)
    manifest = build_manifest_for_file(str(path), survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K")
    result = validate_reference(str(path), manifest)
    assert result.ok is False


def test_validate_reference_rejects_implausible_values(tmp_path):
    path = tmp_path / "implausible.fits"
    wcs, data = _write_galactic_gaussian_fixture(str(path))
    with fits.open(str(path)) as hdul:
        hdul[0].data[:] = 1e9  # absurd for a brightness-temperature map
        hdul.writeto(str(path), overwrite=True)
    manifest = build_manifest_for_file(str(path), survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K")
    result = validate_reference(str(path), manifest)
    assert result.ok is False


def test_provider_refuses_to_load_unvalidated_reference_by_default(fixture_fits):
    manifest = build_manifest_for_file(fixture_fits, survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K",
                                        trust=ReferenceTrust.REAL_UNVERIFIED)
    with pytest.raises(ValueError):
        FITSMomentMapProvider(fixture_fits, manifest, require_validated=True)


def test_provider_loads_with_explicit_override_for_testing(fixture_fits):
    manifest = build_manifest_for_file(fixture_fits, survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K",
                                        trust=ReferenceTrust.TEST_FIXTURE)
    provider = FITSMomentMapProvider(fixture_fits, manifest, require_validated=False)
    assert provider.is_observational is False  # TEST_FIXTURE is not observational


def test_provider_choose_target_finds_the_injected_gaussian_peak(fixture_fits):
    manifest = build_manifest_for_file(fixture_fits, survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K",
                                        trust=ReferenceTrust.TEST_FIXTURE)
    provider = FITSMomentMapProvider(fixture_fits, manifest, require_validated=False)
    center, info = provider.choose_target(LOCATION, OBSTIME, min_altitude_deg=-90.0, beam_fwhm_deg=6.0)
    galactic = center.galactic
    assert abs(galactic.l.deg - 30.0) < 4.0
    assert abs(galactic.b.deg - 0.0) < 4.0
    assert info["accepted"] is True


def test_template_for_grid_cache_matches_exact_convolution(fixture_fits):
    """template_for()'s default cached (LocalSphericalTemplate-backed)
    path must agree with the exact, uncached convolution to well within
    the tolerance this package's fits use - the grid is a performance
    optimization, not an approximation that should change results."""
    from astropy.coordinates import SkyCoord

    manifest = build_manifest_for_file(fixture_fits, survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K",
                                        trust=ReferenceTrust.TEST_FIXTURE)
    provider = FITSMomentMapProvider(fixture_fits, manifest, require_validated=False)
    center = SkyCoord(l=30.0, b=0.0, unit="deg", frame="galactic").icrs
    exact = provider.template_for(center, beam_fwhm_deg=6.0, use_grid_cache=False)
    cached = provider.template_for(center, beam_fwhm_deg=6.0, use_grid_cache=True, extent_deg=8.0)

    query = SkyCoord(l=[30.0, 31.0, 29.0], b=[0.0, 1.0, -1.0], unit="deg", frame="galactic").icrs
    exact_values = exact(query)
    cached_values = cached(query)
    assert np.allclose(exact_values, cached_values, rtol=0.02)


def test_grid_cache_calls_exact_convolution_exactly_once_regardless_of_query_count(fixture_fits):
    """Fase 19 performance-regression guard: LocalSphericalTemplate's whole
    point is ONE batched exact-convolution call at grid-build time, then
    O(1) bilinear lookups per query forever after (this is what took the
    real-FITS fit from ~16.8s to ~0.31s). This is deliberately a
    STRUCTURAL/call-counting test, not a wall-clock timing test (timing
    is flaky in CI and on this Pi under load) - it catches the actual
    regression class of interest: someone re-introducing a call to the
    exact convolution inside __call__/template evaluation, which would
    silently reintroduce the O(n_queries) cost this cache exists to kill,
    without necessarily changing the numeric result at all."""
    import alignment
    from astropy.coordinates import SkyCoord

    manifest = build_manifest_for_file(fixture_fits, survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K",
                                        trust=ReferenceTrust.TEST_FIXTURE)
    provider = FITSMomentMapProvider(fixture_fits, manifest, require_validated=False)
    center = SkyCoord(l=30.0, b=0.0, unit="deg", frame="galactic").icrs

    call_count = {"n": 0}
    real_fn = alignment.gaussian_convolved_template

    def _counting_wrapper(*args, **kwargs):
        call_count["n"] += 1
        return real_fn(*args, **kwargs)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(alignment, "gaussian_convolved_template", _counting_wrapper)
        cached = provider.template_for(center, beam_fwhm_deg=6.0, use_grid_cache=True, extent_deg=8.0)
        assert call_count["n"] == 1, \
            "building the cached template must call the exact convolution exactly once (grid build)"

        # Many separate evaluations, including single-point and batched
        # queries, and repeated re-evaluation - none of this may trigger
        # another exact-convolution call.
        for _ in range(25):
            query = SkyCoord(l=[30.0, 31.0, 29.0], b=[0.0, 1.0, -1.0], unit="deg", frame="galactic").icrs
            cached(query)
        single = SkyCoord(l=30.5, b=0.2, unit="deg", frame="galactic").icrs
        cached(single)

    assert call_count["n"] == 1, \
        "template evaluation (__call__) must never call the exact convolution again after grid build - " \
        "each evaluation must be an O(1) bilinear lookup on the pre-built grid"


def test_provider_template_for_returns_finite_values_near_peak(fixture_fits):
    from astropy.coordinates import SkyCoord
    manifest = build_manifest_for_file(fixture_fits, survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K",
                                        trust=ReferenceTrust.TEST_FIXTURE)
    provider = FITSMomentMapProvider(fixture_fits, manifest, require_validated=False)
    center = SkyCoord(l=30.0, b=0.0, unit="deg", frame="galactic").icrs
    template = provider.template_for(center, beam_fwhm_deg=6.0)
    values = template(SkyCoord([center]))
    assert np.all(np.isfinite(values))
    assert values[0] > 0.0
