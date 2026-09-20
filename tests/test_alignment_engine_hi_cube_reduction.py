"""Fase 24: cube reduction + trust promotion, against a small synthetic 3D
cube fixture (never the real, untracked-in-git HI4PI download - these
tests must pass in a fresh clone with no reference data present)."""
import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from alignment_engine.hi.cube_reduction import promote_to_validated, reduce_cube_to_moment_map
from alignment_engine.hi.reference_trust import ReferenceTrust, build_manifest_for_file


def _write_synthetic_cube(path, n_vel=200, size=41, increasing=True, specsys="LSRK", ctype3="VRAD"):
    wcs = WCS(naxis=3)
    wcs.wcs.ctype = ["GLON-CAR", "GLAT-CAR", ctype3]
    wcs.wcs.crpix = [size / 2, size / 2, n_vel / 2]
    wcs.wcs.crval = [10.0, 0.0, 0.0]
    wcs.wcs.cdelt = [-0.5, 0.5, 1000.0 if increasing else -1000.0]  # m/s
    header = wcs.to_header()
    header["BUNIT"] = "K"
    header["SPECSYS"] = specsys
    header["NAXIS"] = 3
    header["NAXIS1"] = size
    header["NAXIS2"] = size
    header["NAXIS3"] = n_vel

    vel_channel = np.arange(n_vel)
    vel_km_s = (vel_channel - n_vel / 2) * (1000.0 if increasing else -1000.0) / 1000.0
    spatial = np.ones((size, size)) * 2.0
    spatial[size // 2, size // 2] = 10.0  # a peak
    data = np.zeros((n_vel, size, size), dtype=np.float32)
    for i, v in enumerate(vel_km_s):
        line_shape = np.exp(-0.5 * (v / 20.0) ** 2)
        data[i] = spatial * line_shape
    fits.PrimaryHDU(data=data, header=header).writeto(path, overwrite=True)
    return vel_km_s


@pytest.fixture
def cube_fixture(tmp_path):
    path = tmp_path / "synthetic_cube.fits"
    vel_km_s = _write_synthetic_cube(str(path))
    return str(path), vel_km_s


def test_reduce_cube_to_moment_map_produces_2d_output(tmp_path, cube_fixture):
    fits_path, _ = cube_fixture
    out_path = str(tmp_path / "moment.fits")
    result = reduce_cube_to_moment_map(fits_path, out_path, velocity_window_km_s=(-50.0, 50.0))
    with fits.open(out_path) as hdul:
        assert hdul[0].header["NAXIS"] == 2
        assert hdul[0].data.shape == (41, 41)
    assert result.n_channels_used > 0
    assert 0.0 < result.signal_fraction_captured <= 1.0


def test_reduce_cube_narrow_window_captures_less_signal_than_wide_window(tmp_path, cube_fixture):
    fits_path, _ = cube_fixture
    r_narrow = reduce_cube_to_moment_map(fits_path, str(tmp_path / "narrow.fits"), velocity_window_km_s=(-10, 10))
    r_wide = reduce_cube_to_moment_map(fits_path, str(tmp_path / "wide.fits"), velocity_window_km_s=(-100, 100))
    assert r_narrow.signal_fraction_captured < r_wide.signal_fraction_captured


def test_reduce_cube_handles_decreasing_velocity_axis(tmp_path):
    path = tmp_path / "decreasing.fits"
    _write_synthetic_cube(str(path), increasing=False)
    result = reduce_cube_to_moment_map(str(path), str(tmp_path / "moment_dec.fits"),
                                        velocity_window_km_s=(-50.0, 50.0))
    assert result.velocity_axis_increasing is False
    assert result.n_channels_used > 0


def test_reduce_cube_out_of_range_window_raises(tmp_path, cube_fixture):
    fits_path, _ = cube_fixture
    with pytest.raises(ValueError):
        reduce_cube_to_moment_map(fits_path, str(tmp_path / "bad.fits"), velocity_window_km_s=(900.0, 950.0))


def test_reduce_cube_excludes_nan_never_zeros_them(tmp_path, cube_fixture):
    fits_path, _ = cube_fixture
    with fits.open(fits_path) as hdul:
        data = hdul[0].data
        data[:, 0, 0] = np.nan  # one pixel entirely NaN across all channels
        hdul.writeto(fits_path, overwrite=True)
    out_path = str(tmp_path / "moment_nan.fits")
    reduce_cube_to_moment_map(fits_path, out_path, velocity_window_km_s=(-50, 50))
    with fits.open(out_path) as hdul:
        assert np.isnan(hdul[0].data[0, 0])  # stayed NaN, was not fabricated into 0


def test_promote_to_validated_passes_for_a_correct_cube(tmp_path, cube_fixture):
    fits_path, _ = cube_fixture
    manifest = build_manifest_for_file(fits_path, survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="velocity_lsrk_km_s",
                                        units="K", trust=ReferenceTrust.REAL_UNVERIFIED)
    validated, checks = promote_to_validated(fits_path, manifest)
    assert validated is not None
    assert validated.trust == ReferenceTrust.REAL_VALIDATED
    assert all(c["ok"] for c in checks)


def test_promote_to_validated_rejects_wrong_spectral_axis_type(tmp_path):
    path = tmp_path / "wrong_ctype.fits"
    _write_synthetic_cube(str(path), ctype3="FREQ")
    manifest = build_manifest_for_file(str(path), survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="unknown", units="K")
    validated, checks = promote_to_validated(str(path), manifest)
    assert validated is None
    assert any(not c["ok"] and c["check"] == "spectral_axis_type_known" for c in checks)


def test_promote_to_validated_rejects_unknown_specsys(tmp_path):
    path = tmp_path / "wrong_specsys.fits"
    _write_synthetic_cube(str(path), specsys="TOPOCENT")
    manifest = build_manifest_for_file(str(path), survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="unknown", units="K")
    validated, checks = promote_to_validated(str(path), manifest)
    assert validated is None
    assert any(not c["ok"] and c["check"] == "spectral_frame_known" for c in checks)


def test_promote_to_validated_rejects_checksum_mismatch(tmp_path, cube_fixture):
    fits_path, _ = cube_fixture
    manifest = build_manifest_for_file(fits_path, survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="velocity_lsrk_km_s", units="K")
    manifest.sha256 = "0" * 64
    validated, checks = promote_to_validated(fits_path, manifest)
    assert validated is None
    assert any(not c["ok"] and c["check"] == "checksum_matches_manifest" for c in checks)


def test_promote_to_validated_never_has_a_force_parameter():
    import inspect
    sig = inspect.signature(promote_to_validated)
    for name in sig.parameters:
        assert "force" not in name.lower()
        assert "trust" != name.lower()
        assert "validated" not in name.lower() or name == "expected_spectral_axis"
