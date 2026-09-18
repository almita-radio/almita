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
