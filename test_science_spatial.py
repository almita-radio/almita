"""RA wrap and spherical geometry (sections 13-14)."""
import numpy as np

from science_engine.spatial import (angular_separation_deg, normalize_ra_deg, ra_hours_to_deg,
                                    tangent_plane_offsets_deg, to_galactic)


def test_ra_hours_to_deg():
    assert np.isclose(ra_hours_to_deg(12.0), 180.0)
    assert np.isclose(ra_hours_to_deg(0.0), 0.0)


def test_normalize_ra_deg_wraps():
    assert np.isclose(normalize_ra_deg(370.0), 10.0)
    assert np.isclose(normalize_ra_deg(-10.0), 350.0)


def test_ra_wrap_points_near_zero_are_close_not_359_degrees_apart():
    """Section 13: RA 23h59m and RA 00h01m must be recognized as nearly
    adjacent, not ~359 degrees apart."""
    ra1_deg = ra_hours_to_deg(23.0 + 59.0 / 60.0)   # 23h59m
    ra2_deg = ra_hours_to_deg(0.0 + 1.0 / 60.0)     # 00h01m
    sep = angular_separation_deg(np.array([ra1_deg]), np.array([0.0]), np.array([ra2_deg]), np.array([0.0]))
    assert sep[0] < 1.0  # ~0.5 deg apart, never ~359


def test_angular_separation_zero_for_identical_points():
    sep = angular_separation_deg(np.array([100.0]), np.array([-30.0]), np.array([100.0]), np.array([-30.0]))
    assert np.isclose(sep[0], 0.0, atol=1e-9)


def test_angular_separation_matches_known_value_on_equator():
    # two points on the celestial equator, 10 degrees apart in RA -> exactly 10 deg separation
    sep = angular_separation_deg(np.array([0.0]), np.array([0.0]), np.array([10.0]), np.array([0.0]))
    assert np.isclose(sep[0], 10.0, atol=1e-6)


def test_angular_separation_near_pole_is_not_naive_cartesian():
    """Near dec=+89, a naive sqrt((dRA)^2+(dDEC)^2) wildly overestimates
    separation because RA circles shrink toward the pole - real angular
    separation for a large RA difference at high dec is small."""
    sep = angular_separation_deg(np.array([0.0]), np.array([89.0]), np.array([180.0]), np.array([89.0]))
    naive = np.sqrt((180.0) ** 2 + 0.0 ** 2)
    assert sep[0] < 5.0  # real separation is small (twice the polar cap radius-ish)
    assert sep[0] < naive / 10  # naive cartesian would be wildly wrong (180 deg)


def test_tangent_plane_offset_is_wrap_safe():
    """A point at RA=0h01m relative to a center at RA=23h59m must offset
    as a SMALL positive x, not wrap around as ~+359 deg."""
    x, y = tangent_plane_offsets_deg(
        ra_deg=np.array([ra_hours_to_deg(0.0 + 1.0 / 60.0)]), dec_deg=np.array([0.0]),
        center_ra_deg=float(ra_hours_to_deg(23.0 + 59.0 / 60.0)), center_dec_deg=0.0)
    assert abs(x[0]) < 1.0


def test_tangent_plane_offset_zero_at_center():
    x, y = tangent_plane_offsets_deg(np.array([123.4]), np.array([-45.6]), 123.4, -45.6)
    assert np.isclose(x[0], 0.0, atol=1e-9)
    assert np.isclose(y[0], 0.0, atol=1e-9)


def test_to_galactic_is_a_real_astropy_transform_not_identity():
    l, b = to_galactic(np.array([266.4]), np.array([-28.9]))  # near the Galactic center in ICRS
    assert abs(l[0]) < 2.0  # galactic center: l~0, b~0
    assert abs(b[0]) < 2.0
