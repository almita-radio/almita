"""GRIDDING TRUTH (sections 21-24, 28-32, 38-44, 96-102): geometry, orientation, normalisation and weighting
verified against expectations computed OUTSIDE the production gridding code (own haversine, own weighted-mean
loops). Synthetic Level 1 only - never RAW.
"""
import numpy as np
import pytest

from science_engine.beam import beam_weight
from science_engine.config import ScienceConfig
from science_engine.cube import build_cube
from science_engine.grid import build_beam_model, build_grid, pixel_centers_deg
from science_engine.gridding import GriddingAccumulator, point_passes_quality_policy
from science_engine.models import BeamModel, ScienceGrid
from science_engine.simulation import (SyntheticPointSpec, build_synthetic_science_input, rectangular_grid_specs)
from science_engine.spatial import angular_separation_deg, tangent_plane_offsets_deg, to_galactic

FOUR_LN2 = 4.0 * np.log(2.0)


def haversine_deg(ra1, dec1, ra2, dec2):
    """Independent great-circle separation (numerically stable haversine) - NOT the production/astropy path."""
    r1, d1, r2, d2 = map(np.radians, (ra1, dec1, ra2, dec2))
    a = np.sin((d2 - d1) / 2) ** 2 + np.cos(d1) * np.cos(d2) * np.sin((r2 - r1) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.minimum(1.0, np.sqrt(a))))


def run(specs, *, fwhm=1.5, grid=None, quality_policy="STANDARD", n_channels=16, cutoff=3.0, margin=1.5,
        pixels_per_beam=4.0, **synth):
    si = build_synthetic_science_input(specs, n_channels=n_channels, **synth)
    config = ScienceConfig(beam_fwhm_deg=fwhm, pixels_per_beam=pixels_per_beam, extent_margin_beams=margin,
                           beam_cutoff_n_fwhm=cutoff, quality_policy=quality_policy)
    beam = build_beam_model(config)
    grid = grid or build_grid(si, beam, config)
    return si, config, beam, grid, build_cube(si, grid, beam, config)


def mosaic(n=5, spacing=1.0, dec=-30.0, ra_h=12.0, **kw):
    return rectangular_grid_specs(ra_h, dec, n, n, spacing_deg=spacing, **kw)


# ---------------------------------------------------------------- 21-22: spherical geometry

@pytest.mark.parametrize("ra1,dec1,ra2,dec2", [
    (359.9, 0.0, 0.1, 0.0), (359.95, -30.0, 0.05, -30.0), (359.9, 45.0, 0.1, 45.2), (0.0, 89.0, 180.0, 89.0),
    (10.0, 80.0, 190.0, 80.0), (123.4, 85.0, 123.5, 89.0), (200.0, 0.0, 200.001, 0.001), (0.0, 89.99, 359.99, 89.99),
])
def test_angular_separation_matches_independent_haversine_and_astropy(ra1, dec1, ra2, dec2):
    ours = angular_separation_deg(np.array([ra1]), np.array([dec1]), np.array([ra2]), np.array([dec2]))[0]
    assert ours == pytest.approx(haversine_deg(ra1, dec1, ra2, dec2), abs=1e-9)
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    astropy_value = SkyCoord(ra1 * u.deg, dec1 * u.deg).separation(SkyCoord(ra2 * u.deg, dec2 * u.deg)).deg
    assert ours == pytest.approx(astropy_value, abs=1e-12)


def test_ra_wrap_separation_is_small_not_359():
    sep = angular_separation_deg(np.array([359.9]), np.array([0.0]), np.array([0.1]), np.array([0.0]))[0]
    assert sep == pytest.approx(0.2, abs=1e-9)


@pytest.mark.parametrize("dec", [-60.0, -30.0, 0.0, 45.0, 80.0, 85.0, 89.0])
def test_pixel_centres_round_trip_through_the_tangent_plane_at_any_declination(dec):
    grid = ScienceGrid(frame="icrs", center_ra_deg=359.95, center_dec_deg=dec, width_deg=2.0, height_deg=1.0,
                       pixel_scale_deg=0.1, nx=20, ny=10)
    ra, decs = pixel_centers_deg(grid)
    x, y = tangent_plane_offsets_deg(ra, decs, grid.center_ra_deg, grid.center_dec_deg)
    cx = (np.arange(20) + 0.5) * 0.1 - 1.0
    cy = (np.arange(10) + 0.5) * 0.1 - 0.5
    assert np.allclose(x, cx[None, :], atol=1e-9) and np.allclose(y, cy[:, None], atol=1e-9)
    assert np.all((ra >= 0) & (ra < 360))


def test_galactic_conversion_matches_independent_rotation_and_sgr_a_star():
    ra = np.array([266.40499, 10.6847, 83.633, 0.0])
    dec = np.array([-28.93617, 41.2687, 22.0145, 90.0])
    l, b = to_galactic(ra, dec)
    # independent: standard equatorial->galactic rotation from the NGP (J2000): RA 192.85948, Dec 27.12825, l_NCP 122.93192
    a0, d0, l0 = map(np.radians, (192.85948, 27.12825, 122.93192))
    a, d = np.radians(ra), np.radians(dec)
    sin_b = np.sin(d0) * np.sin(d) + np.cos(d0) * np.cos(d) * np.cos(a - a0)
    y = np.cos(d) * np.sin(a - a0)
    x = np.cos(d0) * np.sin(d) - np.sin(d0) * np.cos(d) * np.cos(a - a0)
    l_manual = (l0 - np.arctan2(y, x)) % (2 * np.pi)
    assert np.allclose(b, np.degrees(np.arcsin(sin_b)), atol=1e-3)
    dl = (l - np.degrees(l_manual) + 180) % 360 - 180
    assert np.allclose(dl[:3], 0.0, atol=1e-3)                      # (l is undefined at the pole - excluded)
    assert abs(((l[0] + 180) % 360) - 180) < 0.01 and abs(b[0]) < 0.06        # Sgr A*: l~359.94, b~-0.05


def test_source_across_ra_zero_is_one_contiguous_structure_not_two_edge_sources():
    """Section 22: a Gaussian source centred on RA=0 must map to ONE blob in the middle of the grid."""
    specs = rectangular_grid_specs(0.0, -30.0, 5, 7, spacing_deg=1.0)      # 7 columns straddle RA = 0h
    ras = np.array([s.ra_hours * 15 for s in specs]) % 360
    assert (ras > 350).any() and (ras < 10).any()
    dec_c = -30.0

    def amp(ra_deg, dec_deg):
        d_ra = ((ra_deg + 180) % 360 - 180) * np.cos(np.radians(dec_c))
        return np.exp(-FOUR_LN2 * (d_ra ** 2 + (dec_deg - dec_c) ** 2) / 1.5 ** 2)

    # ScienceInputPoint hours may be negative for RA<0 in the builder: normalise to [0, 24)
    specs = [SyntheticPointSpec(s.point_index, (s.ra_hours) % 24.0, s.dec_degrees) for s in specs]
    si, config, beam, grid, cube = run(specs, line_amplitude_fn=amp, noise_sigma=1e-3, line_fwhm_m_s=20_000.0)
    ch = np.argmin(np.abs(cube.velocity_lsrk_m_s))
    image = cube.relative_intensity[ch]
    ok = cube.valid[ch]
    hot = ok & (image > 0.5 * np.nanmax(image))
    assert 0 < hot.sum()
    assert _n_components(hot) == 1
    iy, ix = np.unravel_index(np.nanargmax(np.where(ok, image, -np.inf)), image.shape)
    assert abs(ix - (grid.nx - 1) / 2) <= 1.5 and abs(iy - (grid.ny - 1) / 2) <= 1.5      # peak near the middle
    assert not hot[:, 0].any() and not hot[:, -1].any()                                    # nothing at the map edges


def _n_components(mask):
    seen, count = np.zeros_like(mask, dtype=bool), 0
    for start in zip(*np.nonzero(mask)):
        if seen[start]:
            continue
        count += 1
        stack = [start]
        while stack:
            y, x = stack.pop()
            if seen[y, x]:
                continue
            seen[y, x] = True
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and mask[ny, nx] and not seen[ny, nx]:
                    stack.append((ny, nx))
    return count


# ---------------------------------------------------------------- 23-24: orientation and axis order

def test_pixel_orientation_x_is_ra_offset_y_is_dec_and_row0_is_south():
    grid = ScienceGrid(frame="icrs", center_ra_deg=100.0, center_dec_deg=-20.0, width_deg=4.0, height_deg=2.0,
                       pixel_scale_deg=0.5, nx=8, ny=4)
    ra, dec = pixel_centers_deg(grid)
    assert ra.shape == dec.shape == (4, 8)
    assert np.all(np.diff(ra, axis=1) > 0)          # x increases -> RA increases (east)
    assert np.all(np.diff(dec, axis=0) > 0)         # y increases -> Dec increases (north); row 0 = southernmost
    assert np.allclose(dec[:, 0], dec[:, -1]) and np.allclose(ra[0, :], ra[-1, :])      # no accidental transpose/skew


@pytest.mark.parametrize("offset_ra_deg,offset_dec_deg,expect", [
    (+1.0, 0.0, ("x", +1)), (-1.0, 0.0, ("x", -1)), (0.0, +1.0, ("y", +1)), (0.0, -1.0, ("y", -1))])
def test_a_source_at_a_known_offset_lands_on_the_expected_side(offset_ra_deg, offset_dec_deg, expect):
    """East/West/North/South: only the offset pointing is bright; the map maximum must sit on that side."""
    dec = -30.0
    base_specs = rectangular_grid_specs(12.0, dec, 3, 3, spacing_deg=1.0)
    bright = min(base_specs, key=lambda s: (abs(((s.ra_hours * 15) - (12.0 * 15 + offset_ra_deg / np.cos(np.radians(dec)))))
                                            + abs(s.dec_degrees - (dec + offset_dec_deg))))
    for s in base_specs:
        s.amplitude_scale = 1.0 if s is bright else 0.0
    si, config, beam, grid, cube = run(base_specs, fwhm=0.8, line_amplitude_fn=lambda r, d: 1.0, noise_sigma=1e-4,
                                       cutoff=1.5)
    ch = np.argmin(np.abs(cube.velocity_lsrk_m_s))
    image = np.where(cube.valid[ch], cube.relative_intensity[ch], -np.inf)
    iy, ix = np.unravel_index(np.argmax(image), image.shape)
    cy, cx = (grid.ny - 1) / 2, (grid.nx - 1) / 2
    axis, sign = expect
    if axis == "x":
        assert np.sign(ix - cx) == sign and abs(iy - cy) <= 1.5
    else:
        assert np.sign(iy - cy) == sign and abs(ix - cx) <= 1.5


def test_cube_axis_order_channel_slice_and_integrated_axis_are_correct(tmp_path):
    """Section 24: [velocity, y, x] with all three lengths different so any transpose fails the shape check."""
    from science_engine.integration import channel_map_nearest, integrated_map
    specs = rectangular_grid_specs(12.0, -30.0, 2, 4, spacing_deg=1.0)          # wider than tall
    si, config, beam, grid, cube = run(specs, n_channels=40, line_amplitude_fn=lambda r, d: 1.0, noise_sigma=1e-3,
                                       line_fwhm_m_s=30_000.0)
    assert grid.nx != grid.ny
    assert cube.relative_intensity.shape == (40, grid.ny, grid.nx)
    assert np.all(np.diff(cube.velocity_lsrk_m_s) > 0)                          # canonical: strictly ascending
    idx = 17
    ch = channel_map_nearest(cube, cube.velocity_lsrk_m_s[idx])
    assert ch.value.shape == (grid.ny, grid.nx)
    assert np.array_equal(ch.value, cube.relative_intensity[idx], equal_nan=True)
    integ = integrated_map(cube, ScienceConfig(velocity_window_min_m_s=-2e5, velocity_window_max_m_s=2e5))
    assert integ.value.shape == (grid.ny, grid.nx)                              # velocity axis was the one collapsed


def test_canonical_velocity_axis_is_ascending_whatever_the_input_order():
    specs = mosaic(2, 1.0)
    _, _, _, _, cube_asc = run(specs, line_amplitude_fn=lambda r, d: 1.0, descending=False, noise_sigma=1e-3)
    _, _, _, _, cube_desc = run(specs, line_amplitude_fn=lambda r, d: 1.0, descending=True, noise_sigma=1e-3)
    assert np.all(np.diff(cube_asc.velocity_lsrk_m_s) > 0) and np.all(np.diff(cube_desc.velocity_lsrk_m_s) > 0)
    assert np.array_equal(cube_asc.velocity_lsrk_m_s, cube_desc.velocity_lsrk_m_s)
    assert cube_desc.build_info["resample"]["n_points_resampled"] == 0          # a pure reversal is a permutation, not an interpolation


# ---------------------------------------------------------------- 38-44: normalisation and null tests

def test_constant_sky_stays_constant_everywhere_it_is_covered(capsys):
    """Section 38/100: identical spectra at every point -> every covered voxel equals it, independent of how many
    beams overlap there (bias ~ machine precision); uncertainty (not value) carries the coverage difference."""
    v0 = 0.37
    si, config, beam, grid, cube = run(mosaic(5, 1.0), n_channels=8, line_amplitude_fn=lambda r, d: v0,
                                       line_fwhm_m_s=1e9, noise_sigma=0.0)
    for p in si.points:
        p.uncertainty[:] = 0.02
    cube = build_cube(si, grid, build_beam_model(config), config)
    v = cube.relative_intensity[cube.valid]
    truth = np.exp(-FOUR_LN2 * cube.velocity_lsrk_m_s[:, None, None] ** 2 / 1e18) * v0 * np.ones_like(cube.relative_intensity)
    bias = np.abs(cube.relative_intensity - truth)[cube.valid]
    counts = cube.n_pointings[cube.valid]
    assert counts.min() >= 1 and counts.max() >= 9                      # 1 contributor at edges .. many in the middle
    assert bias.max() < 1e-13
    centre = cube.uncertainty[0, grid.ny // 2, grid.nx // 2]
    edge = cube.uncertainty[0, 0, 0]
    assert centre < edge                                                # coverage difference lives in sigma, not value
    with capsys.disabled():
        print(f"\nCONSTANT SKY: max|bias|={bias.max():.2e}  RMS={np.sqrt(np.mean(bias**2)):.2e}  "
              f"n_pointings range [{counts.min()}, {counts.max()}]  sigma centre/edge = {centre/edge:.3f}")


def test_zero_sky_is_exactly_zero_where_covered_and_invalid_elsewhere():
    si, config, beam, grid, cube = run(mosaic(3, 1.0), fwhm=1.0, cutoff=1.0, margin=1.5, n_channels=8,
                                       line_amplitude_fn=lambda r, d: 0.0, noise_sigma=0.0)
    for p in si.points:
        p.uncertainty[:] = 0.02
    cube = build_cube(si, grid, build_beam_model(config), config)
    assert cube.valid.any() and (~cube.valid).any()                     # both covered and uncovered pixels exist
    assert np.all(cube.relative_intensity[cube.valid] == 0.0)           # exactly zero, no tiny offset structure
    assert np.all(np.isnan(cube.relative_intensity[~cube.valid]))       # uncovered = invalid, never 0
    assert np.all(cube.weight_sum[~cube.valid] == 0)


def test_noise_only_mean_is_zero_within_uncertainty_and_unstructured(capsys):
    """Section 40: 400 noise realisations through the PRODUCTION accumulator. Mean map / (sigma/sqrt(N)) must be
    ~N(0,1) per voxel and show no spatial trend against coverage."""
    rng = np.random.default_rng(2026)
    specs = mosaic(4, 1.0)
    si, config, beam, grid, _ = run(specs, n_channels=4, line_amplitude_fn=lambda r, d: 0.0, noise_sigma=0.05)
    from science_engine.gridding import spatial_weight_for_point
    weights = [spatial_weight_for_point(grid, p.ra_deg, p.dec_degrees, beam) for p in si.points]
    trials, sigma = 400, 0.05
    stack = np.empty((trials, 4, grid.ny, grid.nx))
    rep = None
    for t in range(trials):
        acc = GriddingAccumulator((4, grid.ny, grid.nx))
        for w in weights:
            acc.add_point(w, rng.normal(0, sigma, 4), np.full(4, sigma), np.ones(4, dtype=bool))
        v, u, _, _ = acc.finalize()
        stack[t], rep = v, u
    valid = np.isfinite(rep)
    z = stack.mean(axis=0)[valid] / (rep[valid] / np.sqrt(trials))
    weight_map = np.broadcast_to(acc.finalize()[2], stack.shape[1:])[valid]
    corr = np.corrcoef(z, weight_map)[0, 1]
    with capsys.disabled():
        print(f"\nNOISE-ONLY: z mean={z.mean():+.3f} std={z.std():.3f}  corr(z, coverage)={corr:+.3f}  "
              f"max|mean|/sigma={np.abs(z).max() / np.sqrt(trials):.3f}")
    assert abs(z.mean()) < 0.15 and 0.85 < z.std() < 1.15
    assert abs(corr) < 0.25                                             # no systematic pattern with coverage


def test_single_point_shows_the_beam_footprint_not_a_filled_rectangle():
    """Section 41: one MasterSpectrum -> support is exactly the disc within the cutoff radius."""
    spec = [SyntheticPointSpec(1, 12.0, -30.0)]
    fwhm = 1.0
    si, config, beam, grid, cube = run(spec, fwhm=fwhm, cutoff=1.0, margin=1.5, n_channels=4,
                                       line_amplitude_fn=lambda r, d: 1.0, noise_sigma=0.01)
    ra, dec = pixel_centers_deg(grid)
    sep = haversine_deg(180.0, -30.0, ra, dec)
    expected_support = sep <= 1.0 * fwhm
    assert np.array_equal(cube.valid[0], expected_support)
    assert (~cube.valid[0]).any() and cube.valid[0].sum() < grid.nx * grid.ny * 0.8      # corners invalid: not a filled box
    assert np.all(np.isnan(cube.relative_intensity[0][~expected_support]))
    # weight profile follows exp(-4 ln2 theta^2/FWHM^2)/sigma^2 exactly
    w_expected = np.exp(-FOUR_LN2 * sep ** 2 / fwhm ** 2)[expected_support] / 0.01 ** 2
    assert np.allclose(cube.weight_sum[0][expected_support], w_expected, rtol=1e-6)
    # the value is the point's own value everywhere in the footprint (one contributor -> its own measurement)
    assert np.allclose(cube.relative_intensity[0][expected_support], si.points[0].relative_intensity[0], rtol=1e-12)


def test_two_identical_points_do_not_double_the_signal_and_improve_sigma_by_root_two():
    """Sections 42/101: equal beam weight at the mid-line pixel -> value unchanged, sigma / sqrt(2), exactly."""
    dec, sigma = -30.0, 0.1
    ra_h = 12.0
    d_ra = 0.6 / np.cos(np.radians(dec)) / 15.0
    specs = [SyntheticPointSpec(1, ra_h - d_ra, dec), SyntheticPointSpec(2, ra_h + d_ra, dec)]
    grid = ScienceGrid(frame="icrs", center_ra_deg=180.0, center_dec_deg=dec, width_deg=3.15, height_deg=3.15,
                       pixel_scale_deg=0.15, nx=21, ny=21)          # odd -> a pixel centre exactly on the symmetry line
    si, config, beam, grid, cube = run(specs, fwhm=1.5, grid=grid, n_channels=4, line_amplitude_fn=lambda r, d: 0.0,
                                       noise_sigma=0.0)
    for p in si.points:
        p.relative_intensity[:] = 1.0
        p.uncertainty[:] = sigma
    cube = build_cube(si, grid, beam, config)
    c = cube.relative_intensity[0, 10, 10]
    assert c == pytest.approx(1.0, abs=1e-14)                        # NOT 2.0
    assert cube.uncertainty[0, 10, 10] == pytest.approx(sigma / np.sqrt(2), rel=1e-9)
    assert cube.n_pointings[0, 10, 10] == 2
    # over the whole grid, more overlap never raises the value and never raises sigma above the single-point sigma
    both = cube.n_pointings[0] == 2
    assert np.allclose(cube.relative_intensity[0][cube.valid[0]], 1.0, atol=1e-13)
    assert np.all(cube.uncertainty[0][both] <= sigma * (1 + 1e-12))


def test_duplicate_measurement_is_detected_and_cannot_fake_integration(tmp_path):
    """Section 43: (a) the same point_index twice in a REDUCE manifest is refused; (b) two points that reference the
    SAME RAW capture sha256 are a duplicate measurement: the second is excluded (and recorded)."""
    import json
    from science_engine.ingest import ScienceContractError, load_science_input
    from science_engine.simulation import write_synthetic_reduce_session
    specs = mosaic(2, 1.0, line_amplitude_fn=None) if False else mosaic(2, 1.0)
    si = build_synthetic_science_input(specs, n_channels=8, line_amplitude_fn=lambda r, d: 1.0, noise_sigma=0.01)
    d = write_synthetic_reduce_session(tmp_path / "s", si)
    m = json.loads((d / "manifest.json").read_text())
    m["points"].append(dict(m["points"][0]))
    (d / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ScienceContractError, match="more than once"):
        load_science_input(d)

    d2 = write_synthetic_reduce_session(tmp_path / "t", si)
    j1 = json.loads((d2 / "points" / "1" / "master_spectrum.json").read_text())
    j2 = json.loads((d2 / "points" / "2" / "master_spectrum.json").read_text())
    j2["capture_refs"] = j1["capture_refs"]                          # point 2 claims point 1's RAW capture
    (d2 / "points" / "2" / "master_spectrum.json").write_text(json.dumps(j2))
    loaded = load_science_input(d2)
    assert [p.point_index for p in loaded.points] == [1, 3, 4]
    assert {"point_index": 2, "reason": "DUPLICATE_CAPTURE_OF_POINT_1"} in loaded.exclusions


def test_extreme_but_valid_good_point_is_gridded_faithfully_not_silently_clipped():
    """Section 44: V1 has NO spatial sigma-clipping (documented limitation): a GOOD point with an extreme value
    contributes through its beam*inverse-variance weight exactly as the formula says."""
    specs = mosaic(3, 1.0)
    si, config, beam, grid, _ = run(specs, n_channels=4, line_amplitude_fn=lambda r, d: 0.0, noise_sigma=0.01)
    outlier = si.points[4]
    for p in si.points:
        p.relative_intensity[:] = 0.0
        p.uncertainty[:] = 0.01
    outlier.relative_intensity[:] = 1e6
    cube = build_cube(si, grid, beam, config)
    ra, dec = pixel_centers_deg(grid)
    def beam_w(p):     # documented cutoff: weight is exactly 0 beyond cutoff_n_fwhm * FWHM
        theta = haversine_deg(p.ra_deg, p.dec_degrees, ra, dec)
        return np.where(theta <= beam.cutoff_n_fwhm * beam.fwhm_deg, np.exp(-FOUR_LN2 * theta ** 2 / beam.fwhm_deg ** 2), 0.0)
    w = [beam_w(p) for p in si.points]
    expected = 1e6 * w[4] / sum(w)
    assert np.allclose(cube.relative_intensity[0], expected, rtol=1e-9, atol=1e-6)
    assert cube.build_info["n_nonfinite_rejected"] == 0 and cube.build_info["n_points_excluded"] == 0


# ---------------------------------------------------------------- 28-32, 99, 102: weighting equation

def test_weight_equation_matches_a_manual_evaluation_outside_the_production_code():
    """Sections 28/99/102: W = beam(theta) * (1/sigma^2); y = sum(W v)/sum(W); var = sum(W^2 sigma^2)/(sum W)^2.
    3 points, distinct sigma and value, evaluated with plain Python floats at every pixel."""
    rng = np.random.default_rng(5)
    specs = [SyntheticPointSpec(1, 12.00, -30.0), SyntheticPointSpec(2, 12.05, -29.5), SyntheticPointSpec(3, 11.96, -30.4)]
    sigmas, values = [0.02, 0.2, 0.05], [1.0, -0.5, 0.25]
    grid = ScienceGrid(frame="icrs", center_ra_deg=180.0, center_dec_deg=-30.0, width_deg=2.0, height_deg=2.0,
                       pixel_scale_deg=0.5, nx=4, ny=4)
    si, config, beam, grid, _ = run(specs, grid=grid, fwhm=1.2, n_channels=3, line_amplitude_fn=lambda r, d: 0.0,
                                    noise_sigma=0.0)
    for p, s, v in zip(si.points, sigmas, values):
        p.relative_intensity[:], p.uncertainty[:] = v, s
    cube = build_cube(si, grid, beam, config)
    ra, dec = pixel_centers_deg(grid)
    for iy in range(4):
        for ix in range(4):
            num = den = wsq = 0.0
            for p, s, v in zip(si.points, sigmas, values):
                theta = haversine_deg(p.ra_deg, p.dec_degrees, ra[iy, ix], dec[iy, ix])
                b = float(np.exp(-FOUR_LN2 * theta ** 2 / 1.2 ** 2)) if theta <= 3.0 * 1.2 else 0.0
                w = b / s ** 2
                num, den, wsq = num + w * v, den + w, wsq + w * w * s ** 2
            assert cube.relative_intensity[1, iy, ix] == pytest.approx(num / den, rel=1e-12)
            assert cube.uncertainty[1, iy, ix] == pytest.approx(np.sqrt(wsq) / den, rel=1e-12)
            assert cube.weight_sum[1, iy, ix] == pytest.approx(den, rel=1e-12)


def test_inverse_variance_sigma_1_2_10_exact_value_and_uncertainty():
    """Section 29: equal beam weight (c), sigma = 1, 2, 10."""
    acc = GriddingAccumulator((1, 1))
    truth_meas, sigmas, c = [10.0, 11.0, 20.0], [1.0, 2.0, 10.0], 0.37
    for m, s in zip(truth_meas, sigmas):
        acc.add_point(np.array([[c]]), m, s, True)
    v, u, w, n = acc.finalize()
    inv = [1 / s ** 2 for s in sigmas]
    assert v[0, 0] == pytest.approx(sum(m * i for m, i in zip(truth_meas, inv)) / sum(inv), rel=1e-14)
    assert u[0, 0] == pytest.approx(1 / np.sqrt(sum(inv)), rel=1e-14)          # equal beam weight -> the closed form
    assert w[0, 0] == pytest.approx(c * sum(inv), rel=1e-14) and n[0, 0] == 3


@pytest.mark.parametrize("n_overlap", [1, 2, 4, 9])
def test_constant_field_is_independent_of_the_number_of_overlapping_pointings(n_overlap):
    """Section 100."""
    acc = GriddingAccumulator((1, 1))
    rng = np.random.default_rng(n_overlap)
    for _ in range(n_overlap):
        acc.add_point(np.array([[rng.uniform(0.05, 1.0)]]), 0.123456789, rng.uniform(0.01, 0.5), True)
    assert acc.finalize()[0][0, 0] == pytest.approx(0.123456789, rel=1e-14)


def test_near_point_noisy_versus_far_point_precise_interaction():
    """Section 102: the closer-but-noisier point wins only as much as beam*inverse-variance says."""
    acc = GriddingAccumulator((1, 1))
    near = dict(beam=0.9, value=2.0, sigma=0.5)      # closer, noisy
    far = dict(beam=0.3, value=1.0, sigma=0.05)      # farther, precise
    for d in (near, far):
        acc.add_point(np.array([[d["beam"]]]), d["value"], d["sigma"], True)
    wn, wf = 0.9 / 0.25, 0.3 / 0.0025
    v, u, _, _ = acc.finalize()
    assert v[0, 0] == pytest.approx((wn * 2.0 + wf * 1.0) / (wn + wf), rel=1e-14)
    assert u[0, 0] == pytest.approx(np.sqrt(wn ** 2 * 0.25 + wf ** 2 * 0.0025) / (wn + wf), rel=1e-14)
    assert v[0, 0] < 1.1                            # the precise far point dominates despite the smaller beam factor


# ---------------------------------------------------------------- 31-32: quality policy

QUALITY_TABLE = {   # policy -> which REDUCE quality states contribute (the documented table)
    "STRICT": {"GOOD": True, "WARNING": False, "BAD": False, "UNKNOWN": False},
    "STANDARD": {"GOOD": True, "WARNING": True, "BAD": False, "UNKNOWN": False},
    "PERMISSIVE": {"GOOD": True, "WARNING": True, "BAD": False, "UNKNOWN": True},
}


@pytest.mark.parametrize("policy", list(QUALITY_TABLE))
@pytest.mark.parametrize("state", ["GOOD", "WARNING", "BAD", "UNKNOWN"])
def test_quality_policy_table(policy, state):
    assert point_passes_quality_policy(state, policy) is QUALITY_TABLE[policy][state]


def test_extreme_bad_point_never_moves_the_map_under_any_policy_and_policies_differ_as_documented(capsys):
    specs = mosaic(3, 1.0)
    states = {1: "GOOD", 2: "WARNING", 3: "BAD", 4: "UNKNOWN"}
    results = {}
    for policy in QUALITY_TABLE:
        si = build_synthetic_science_input(specs, n_channels=4, line_amplitude_fn=lambda r, d: 0.0, noise_sigma=0.0)
        for p in si.points:
            p.relative_intensity[:], p.uncertainty[:] = 1.0, 0.1
            p.reduce_quality_state = states.get(p.point_index, "GOOD")
        si.points[2].relative_intensity[:] = 1e9                     # the BAD point is astronomically wrong
        si.points[3].relative_intensity[:] = 50.0                    # the UNKNOWN point is merely different
        config = ScienceConfig(beam_fwhm_deg=1.5, quality_policy=policy)
        beam = build_beam_model(config)
        grid = build_grid(si, beam, config)
        results[policy] = build_cube(si, grid, beam, config)
    for policy, cube in results.items():
        assert np.nanmax(cube.relative_intensity) < 1e8              # BAD (1e9) never contributes, in ANY policy
    assert np.nanmax(results["STANDARD"].relative_intensity) < 2.0   # UNKNOWN excluded by STANDARD
    assert np.nanmax(results["PERMISSIVE"].relative_intensity) > 5.0 # ...but trusted by PERMISSIVE (documented)
    assert results["STRICT"].build_info["n_points_used"] == 6 and results["STANDARD"].build_info["n_points_used"] == 7
    assert results["PERMISSIVE"].build_info["n_points_used"] == 8
    reasons = {e["reason"] for e in results["STRICT"].build_info["excluded"]}
    assert "QUALITY_POLICY_STRICT_EXCLUDES_BAD" in reasons and "QUALITY_POLICY_STRICT_EXCLUDES_WARNING" in reasons
    with capsys.disabled():
        print("\nQUALITY POLICY used points (9 pts: 6 GOOD, 1 WARNING, 1 BAD, 1 UNKNOWN): "
              + ", ".join(f"{k}={v.build_info['n_points_used']}" for k, v in results.items()))


# ---------------------------------------------------------------- 20: pixel scale is sampling, not resolution

def test_output_pixel_scale_changes_sampling_not_the_underlying_map():
    """Section 20: coarse pixel centres are a subset of fine pixel centres -> identical values there, exactly;
    the beam (not the pixel) sets the resolution."""
    specs = mosaic(3, 1.0)
    fwhm = 1.5
    base = dict(frame="icrs", center_ra_deg=180.0, center_dec_deg=-30.0)
    fine = ScienceGrid(**base, width_deg=41 * 0.25, height_deg=41 * 0.25, pixel_scale_deg=0.25, nx=41, ny=41)
    coarse = ScienceGrid(**base, width_deg=21 * 0.5, height_deg=21 * 0.5, pixel_scale_deg=0.5, nx=21, ny=21)
    amp = lambda r, d: np.exp(-FOUR_LN2 * (((r - 180.0) * np.cos(np.radians(-30))) ** 2 + (d + 30.0) ** 2) / 1.0 ** 2)
    _, _, _, _, cf = run(specs, fwhm=fwhm, grid=fine, n_channels=4, line_amplitude_fn=amp, noise_sigma=1e-3)
    _, _, _, _, cc = run(specs, fwhm=fwhm, grid=coarse, n_channels=4, line_amplitude_fn=amp, noise_sigma=1e-3)
    sub = cf.relative_intensity[:, ::2, ::2]
    both = cf.valid[:, ::2, ::2] & cc.valid
    assert both.sum() > 0
    assert np.allclose(sub[both], cc.relative_intensity[both], rtol=1e-9, atol=1e-12)
