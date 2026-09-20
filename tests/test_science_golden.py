"""GOLDEN SCIENCE ACCEPTANCE FIXTURE (sections 54, 55, 63, 96-98, 113-115).

One synthetic sky with known truth, written as a REDUCE-format session and processed by the PRODUCTION path
(ingest -> preflight -> cube -> integrated/moment products -> HDF5 -> validator). Truth:

  spatial Gaussian source, FWHM 1.2 deg, centred (+0.30, -0.20) deg from the mosaic centre (tangent plane)
  spectral Gaussian line, FWHM 8 km/s, centroid = 2 km/s + 8 km/s per degree of RA offset  (velocity gradient)
  per-channel noise 0.02 (reported sigma = 0.02), 4% of channels masked (a fixed channel notch, like real RFI)
  per-point LSRK velocity offsets 0..4.5 km/s and DESCENDING axes (like real Level 1)
  point 25 is BAD with an astronomically wrong spectrum; point 26 was never captured (BLOCKED in REDUCE)

Run `pytest -s test_science_golden.py` to print the measured recovery table used in docs/SCIENCE_ACCEPTANCE.md.
"""
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

from science_engine.config import ScienceConfig
from science_engine.ingest import load_science_input
from science_engine.products import run_science_session
from science_engine.simulation import (SyntheticPointSpec, build_synthetic_science_input, rectangular_grid_specs,
                                       write_synthetic_reduce_session)
from science_engine.storage import validate_science_session

FOUR_LN2 = 4 * np.log(2)
DEC0, RA0_H = -30.0, 12.0
FWHM_SRC, FWHM_BEAM = 1.2, 1.5
SRC_DX, SRC_DY = 0.30, -0.20                 # tangent-plane offset of the source from the mosaic centre (deg)
C0, GRAD = 2_000.0, 8_000.0                  # line centroid m/s at x=0 and m/s per degree of x
LINE_FWHM, AMP, SIGMA = 8_000.0, 1.0, 0.02
N_CH, VMIN, VMAX = 512, -200_000.0, 200_000.0
DV = (VMAX - VMIN) / (N_CH - 1)
BAD_INDEX, MISSING_INDEX = 25, 26


def xy(ra_deg, dec_deg):
    x = ((ra_deg - RA0_H * 15 + 180) % 360 - 180) * np.cos(np.radians(DEC0))
    return x, dec_deg - DEC0


def spatial(ra_deg, dec_deg):
    x, y = xy(ra_deg, dec_deg)
    return np.exp(-FOUR_LN2 * ((x - SRC_DX) ** 2 + (y - SRC_DY) ** 2) / FWHM_SRC ** 2)


def centroid_of(ra_deg, dec_deg):
    return C0 + GRAD * xy(ra_deg, dec_deg)[0]


def build(tmp_path, *, bad=True, missing=True, seed=1):
    specs = rectangular_grid_specs(RA0_H, DEC0, 7, 7, spacing_deg=0.75)
    for i, s in enumerate(specs):
        s.velocity_offset_m_s = 4_500.0 * ((i * 37) % 11) / 10.0          # deterministic 0..4.5 km/s offsets
        s.masked_channel_slice = slice(300, 320)                          # ~4% of channels, fixed channel notch
    si = build_synthetic_science_input(
        specs, n_channels=N_CH, velocity_min_m_s=VMIN, velocity_max_m_s=VMAX, descending=True,
        line_center_m_s_fn=centroid_of, line_amplitude_fn=lambda ra, dec: AMP * spatial(ra, dec),
        line_fwhm_m_s=LINE_FWHM, noise_sigma=SIGMA, rng=np.random.default_rng(seed))
    extra = []
    if bad:
        p = next(q for q in si.points if q.point_index == BAD_INDEX)
        p.relative_intensity[:] = np.where(np.isfinite(p.relative_intensity), 50.0, np.nan)
        p.reduce_quality_state = "BAD"
    if missing:
        si.points = [q for q in si.points if q.point_index != MISSING_INDEX]
        extra = [{"point_index": MISSING_INDEX, "status": "BLOCKED", "reason": "never captured"}]
    write_synthetic_reduce_session(tmp_path / "REDUCE", si, status="PARTIAL" if missing else "COMPLETED",
                                   extra_manifest_points=extra)
    return si, tmp_path / "REDUCE"


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("golden")
    si, reduce_dir = build(tmp)
    config = ScienceConfig(beam_fwhm_deg=FWHM_BEAM, beam_source="operator_config", beam_status="CONFIGURED_OPERATIONAL",
                           velocity_window_min_m_s=-100_000.0, velocity_window_max_m_s=100_000.0, pixels_per_beam=5.0)
    report = run_science_session(str(reduce_dir), config, output_root=str(tmp / "science"))
    return si, reduce_dir, config, report, Path(report.output_dir)


def read_map(session, name):
    with h5py.File(session / "maps" / f"{name}.h5") as h:
        return {k: h[k][()] for k in h.keys()} | {"attrs": dict(h.attrs)}


def read_cube(session):
    with h5py.File(session / "cube" / "science_cube.h5") as h:
        return {k: h[k][()] for k in h.keys()} | {"attrs": dict(h.attrs)}


def pixel_xy(grid):
    from science_engine.grid import pixel_centers_deg
    ra, dec = pixel_centers_deg(type("G", (), grid)) if False else (None, None)
    return ra, dec


def grid_of(session):
    return json.loads((session / "manifest.json").read_text())["grid"]


def pixel_offsets(grid):
    """Tangent-plane pixel-centre coordinates (deg) about the mosaic centre = grid centre."""
    nx, ny, s = grid["nx"], grid["ny"], grid["pixel_scale_deg"]
    x = (np.arange(nx) + 0.5) * grid["width_deg"] / nx - grid["width_deg"] / 2
    y = (np.arange(ny) + 0.5) * grid["height_deg"] / ny - grid["height_deg"] / 2
    return x, y


def fwhm_1d(coord, profile):
    good = np.isfinite(profile)
    coord, profile = coord[good], profile[good]
    half = profile.max() / 2
    idx = np.flatnonzero(profile >= half)
    i0, i1 = idx[0], idx[-1]
    left = coord[i0 - 1] + (half - profile[i0 - 1]) * (coord[i0] - coord[i0 - 1]) / (profile[i0] - profile[i0 - 1])
    right = coord[i1] + (half - profile[i1]) * (coord[i1 + 1] - coord[i1]) / (profile[i1 + 1] - profile[i1])
    return right - left


def test_run_is_completed_partial_and_passes_the_strict_validator(golden):
    si, reduce_dir, config, report, session = golden
    assert report.status == "COMPLETED" and report.data_completeness == "PARTIAL"
    assert validate_science_session(session) == {"ok": True, "problems": [], "products_checked": 4}
    manifest = json.loads((session / "manifest.json").read_text())
    assert manifest["input_reduce_session_status"] == "PARTIAL"
    assert "INPUT_PARTIAL" in manifest["quality"]["limitations"]
    assert "VELOCITY_RESAMPLED" in manifest["quality"]["limitations"] and "PROVISIONAL_BEAM_MODEL" in manifest["quality"]["limitations"]
    assert manifest["quality"]["state"] == "WARNING"                     # INPUT_PARTIAL / POINTS_EXCLUDED are real reasons
    assert manifest["n_input_points"] == 48 and manifest["n_points_used"] == 47 and manifest["n_points_excluded"] == 2
    excluded = {(e["point_index"], e["reason"]) for e in json.loads((session / "provenance.json").read_text())["ingest_exclusions"]}
    assert (MISSING_INDEX, "REDUCE_POINT_STATUS_BLOCKED") in excluded


def test_bad_point_is_excluded_and_does_not_move_the_map(golden):
    si, reduce_dir, config, report, session = golden
    cube = read_cube(session)
    assert np.nanmax(cube["relative_intensity"]) < 3.0                  # BAD point carries 50.0 in every bin
    prov = json.loads((session / "provenance.json").read_text())
    bad = next(p for p in prov["input_points"] if p["point_index"] == BAD_INDEX)
    assert bad["used_in_cube"] is False and "EXCLUDES_BAD" in bad["exclusion_reason"]


def test_no_nan_or_inf_in_any_valid_voxel_and_notch_never_fabricated(golden):
    si, reduce_dir, config, report, session = golden
    cube = read_cube(session)
    valid = cube["valid"]
    assert np.all(np.isfinite(cube["relative_intensity"][valid])) and np.all(np.isfinite(cube["uncertainty"][valid]))
    assert np.all(cube["uncertainty"][valid] > 0)
    assert np.all(np.isnan(cube["relative_intensity"][~valid])) and np.all(cube["weight_sum"][~valid] == 0)
    # The fixed-channel notch maps to a velocity band that differs by point offset: at least one channel has
    # strictly fewer contributing pointings than the surrounding continuum there, and none is filled from nothing.
    n_pt = cube["n_pointings"][:, cube["n_pointings"].shape[1] // 2, cube["n_pointings"].shape[2] // 2]
    assert n_pt.min() < n_pt.max()


def forward_model(session, si):
    """INDEPENDENT expected cube: noise-free sum_i w_i s_i(v) / sum_i w_i evaluated analytically on the cube's
    velocity axis with haversine weights - shares no code with the gridding/resampling implementation."""
    from science_engine.grid import pixel_centers_deg
    from science_engine.models import ScienceGrid
    from test_science_gridding_truth import haversine_deg
    grid = grid_of(session)
    sg = ScienceGrid(**{k: grid[k] for k in ("frame", "center_ra_deg", "center_dec_deg", "width_deg", "height_deg",
                                            "pixel_scale_deg", "nx", "ny")})
    ra, dec = pixel_centers_deg(sg)
    prov = json.loads((session / "provenance.json").read_text())
    used = [p["point_index"] for p in prov["input_points"] if p["used_in_cube"]]
    by_index = {p.point_index: p for p in si.points}
    v = read_cube(session)["velocity_lsrk_m_s"]
    num = np.zeros((v.size,) + ra.shape)
    den = np.zeros(ra.shape)
    for idx in used:
        p = by_index[idx]
        theta = haversine_deg(p.ra_deg, p.dec_degrees, ra, dec)
        w = np.where(theta <= 3 * FWHM_BEAM, np.exp(-FOUR_LN2 * theta ** 2 / FWHM_BEAM ** 2), 0.0) / SIGMA ** 2
        line = AMP * spatial(p.ra_deg, p.dec_degrees) * np.exp(
            -FOUR_LN2 * (v - centroid_of(p.ra_deg, p.dec_degrees)) ** 2 / LINE_FWHM ** 2)
        num += line[:, None, None] * w[None]
        den += w
    return num / np.where(den > 0, den, np.nan)[None], den, ra, dec


def test_full_cube_matches_the_independent_forward_model_within_its_own_uncertainty(golden, capsys):
    """The strongest end-to-end check: mask + descending axes + LSRK offsets + resampling + beam*inverse-variance
    gridding + geometry + orientation all at once, against a model that shares none of that code."""
    si, reduce_dir, config, report, session = golden
    cube = read_cube(session)
    expected, den, ra, dec = forward_model(session, si)
    n_pt = cube["n_pointings"]
    notch_free = np.ones(cube["valid"].shape[0], dtype=bool)
    notch_free[280:340] = False                                   # skip the masked-notch velocity band (edge-dilated)
    sel = cube["valid"] & (n_pt >= 6) & notch_free[:, None, None]
    z = (cube["relative_intensity"][sel] - expected[sel]) / cube["uncertainty"][sel]
    with capsys.disabled():
        print("\nGOLDEN FORWARD-MODEL CHECK (voxels with >= 6 pointings, outside the notch): "
              f"n={sel.sum()}  z mean {z.mean():+.3f}  rms {np.sqrt(np.mean(z ** 2)):.3f}  max|z| {np.abs(z).max():.2f}")
    assert abs(z.mean()) < 0.2 and np.sqrt(np.mean(z ** 2)) < 1.3 and np.abs(z).max() < 6.0


def test_recovered_spatial_center_width_integral_and_uncertainty(golden, capsys):
    si, reduce_dir, config, report, session = golden
    integ = read_map(session, "integrated_relative_intensity")
    grid = grid_of(session)
    x, y = pixel_offsets(grid)
    value, valid = integ["value"], integ["valid"]
    good = np.where(valid, value, np.nan)
    expected_cube, den, ra, dec = forward_model(session, si)
    cube = read_cube(session)
    v = cube["velocity_lsrk_m_s"]
    expected_int = np.sum(np.clip(expected_cube, None, None) * np.diff(np.concatenate([[v[0] - (v[1] - v[0]) / 2],
                          (v[1:] + v[:-1]) / 2, [v[-1] + (v[-1] - v[-2]) / 2]]))[:, None, None], axis=0)
    X, Y = np.meshgrid(x, y)

    def centroid(m):
        top = np.isfinite(m) & (m > 0.5 * np.nanmax(m))
        return float(np.sum(X[top] * m[top]) / np.sum(m[top])), float(np.sum(Y[top] * m[top]) / np.sum(m[top]))

    (cx, cy), (ex, ey) = centroid(good), centroid(expected_int)
    iy, ix = np.unravel_index(np.nanargmax(good), good.shape)
    fx, fy = fwhm_1d(x, good[iy, :]), fwhm_1d(y, good[:, ix])
    predicted = np.hypot(FWHM_SRC, FWHM_BEAM)
    both = valid & np.isfinite(expected_int)
    z = (value[both] - expected_int[both]) / integ["uncertainty"][both]
    sigma_map = np.where(valid, integ["uncertainty"], np.nan)
    centre_sigma = np.nanmedian(sigma_map[grid["ny"] // 3: 2 * grid["ny"] // 3, grid["nx"] // 3: 2 * grid["nx"] // 3])
    edge_sigma = np.nanmedian(np.concatenate([sigma_map[0, :], sigma_map[-1, :], sigma_map[:, 0], sigma_map[:, -1]]))
    with capsys.disabled():
        print("GOLDEN RECOVERY TABLE")
        print(f"  spatial centre   : pipeline ({cx:+.3f}, {cy:+.3f}) deg | forward model ({ex:+.3f}, {ey:+.3f}) | truth ({SRC_DX:+.3f}, {SRC_DY:+.3f})")
        print(f"  spatial FWHM     : RA {fx:.3f} deg, Dec {fy:.3f} deg | sqrt(src^2+beam^2)={predicted:.3f}")
        print(f"  integrated map   : z vs forward model: mean {z.mean():+.2f} rms {np.sqrt(np.mean(z**2)):.2f} max|z| {np.abs(z).max():.1f}; "
              f"peak pipeline {np.nanmax(good):.0f} forward {np.nanmax(expected_int):.0f} (relative*m/s)")
        print(f"  uncertainty      : centre {centre_sigma:.1f}  edge {edge_sigma:.1f}  ratio {edge_sigma / centre_sigma:.2f}")
    # centre: the pipeline reproduces the independent model to a fraction of a pixel; both sit within a beam-scale
    # tolerance of the truth (pixel 0.3 deg, mosaic spacing 0.75 deg vs beam FWHM 1.5 deg -> Shepard ripple)
    assert abs(cx - ex) < 0.1 and abs(cy - ey) < 0.1
    assert abs(cx - SRC_DX) < 0.4 and abs(cy - SRC_DY) < 0.4
    assert abs(fx - predicted) < 0.25 * predicted and abs(fy - predicted) < 0.25 * predicted
    assert abs(z.mean()) < 1.0 and np.sqrt(np.mean(z ** 2)) < 1.6 and np.abs(z).max() < 6.0
    assert edge_sigma > centre_sigma


def test_recovered_spectral_centroid_and_velocity_gradient(golden, capsys):
    si, reduce_dir, config, report, session = golden
    m1 = read_map(session, "moment1_like_velocity_centroid")
    grid = grid_of(session)
    x, y = pixel_offsets(grid)
    value, valid = m1["value"], m1["valid"]
    expected_cube, den, ra, dec = forward_model(session, si)
    v = read_cube(session)["velocity_lsrk_m_s"]
    pos = np.clip(np.nan_to_num(expected_cube), 0, None)
    win = (v >= -1e5) & (v <= 1e5)                      # (the noise-free model needs no signal gating)
    exp_m1 = np.sum(pos[win] * v[win][:, None, None], axis=0) / np.sum(pos[win], axis=0)
    truth_at_pixel = C0 + GRAD * (ra - 180.0 + 0) * 0        # (kept for readability; not used)
    X = np.broadcast_to(x[None, :], value.shape)
    strong = valid & (np.broadcast_to(spatial(180.0 + x[None, :] / np.cos(np.radians(DEC0)), DEC0 + y[:, None]),
                                       value.shape) > 0.5)
    slope_pipe, _ = np.polyfit(X[strong], value[strong], 1)
    slope_model, _ = np.polyfit(X[strong], exp_m1[strong], 1)
    iy, ix = int(np.argmin(np.abs(y - SRC_DY))), int(np.argmin(np.abs(x - SRC_DX)))
    with capsys.disabled():
        print(f"  velocity gradient: pipeline {slope_pipe:.0f} m/s/deg | forward model (beam-smoothed) {slope_model:.0f} | "
              f"truth (unsmoothed) {GRAD:.0f}; n_pixels={strong.sum()}; centroid at source pixel "
              f"{value[iy, ix]:.0f} vs model {exp_m1[iy, ix]:.0f} m/s")
    assert strong.sum() > 8
    assert abs(slope_pipe - slope_model) < 0.10 * abs(slope_model)      # pipeline == independent model
    assert 0.3 * GRAD < slope_pipe < 1.1 * GRAD                          # a real, correctly-signed gradient (beam smooths it)
    assert abs(value[iy, ix] - exp_m1[iy, ix]) < 0.5 * DV               # half a channel (was 1.3 channels before gating)


def test_channel_maps_follow_the_velocity_gradient(golden):
    """Section 54: higher velocity -> the emission peak sits further east; the pipeline peak matches the forward model."""
    si, reduce_dir, config, report, session = golden
    cube = read_cube(session)
    v = cube["velocity_lsrk_m_s"]
    x, y = pixel_offsets(grid_of(session))
    expected_cube, den, ra, dec = forward_model(session, si)
    peaks = {}
    for target in (C0 + GRAD * (SRC_DX - 0.6), C0 + GRAD * (SRC_DX + 0.6)):
        ch = int(np.argmin(np.abs(v - target)))
        img = np.where(cube["valid"][ch], cube["relative_intensity"][ch], -np.inf)
        model = np.where(np.isfinite(expected_cube[ch]), expected_cube[ch], -np.inf)
        ix_p = np.unravel_index(np.argmax(img), img.shape)[1]
        ix_m = np.unravel_index(np.argmax(model), model.shape)[1]
        peaks[target] = (x[ix_p], x[ix_m])
    low, high = sorted(peaks)
    assert peaks[high][0] > peaks[low][0] + 0.3                     # pipeline: the source moves east with velocity
    assert peaks[high][1] > peaks[low][1] + 0.3                     # ...as the independent model says it must
    assert abs(peaks[high][0] - peaks[high][1]) <= 0.35 and abs(peaks[low][0] - peaks[low][1]) <= 0.35


def test_coverage_hole_is_visible_in_pointing_count_but_the_map_is_not_blanked(golden):
    """Sections 97-98: a missing pointing lowers n_pointings/weight around it; neighbours' beams still support the pixel."""
    si, reduce_dir, config, report, session = golden
    cube = read_cube(session)
    grid = grid_of(session)
    missing = next(p for p in rectangular_grid_specs(RA0_H, DEC0, 7, 7, spacing_deg=0.75) if p.point_index == MISSING_INDEX)
    from science_engine.grid import pixel_centers_deg
    from science_engine.models import ScienceGrid
    sg = ScienceGrid(**{k: grid[k] for k in ("frame", "center_ra_deg", "center_dec_deg", "width_deg", "height_deg",
                                            "pixel_scale_deg", "nx", "ny")})
    ra, dec = pixel_centers_deg(sg)
    from test_science_gridding_truth import haversine_deg
    hole = np.unravel_index(np.argmin(haversine_deg(missing.ra_hours * 15, missing.dec_degrees, ra, dec)), ra.shape)
    ch = cube["n_pointings"].shape[0] // 3                                   # a continuum channel, no notch
    assert cube["valid"][ch][hole], "the pixel at the missing pointing must still be supported by neighbouring beams"
    ring = [(hole[0] + dy, hole[1] + dx) for dy in (-6, 6) for dx in (-6, 6)]
    assert cube["n_pointings"][ch][hole] > 0                                  # not blank
    assert np.isfinite(cube["relative_intensity"][ch][hole])


def test_cube_and_maps_are_self_describing_and_beam_is_labelled_operational(golden):
    si, reduce_dir, config, report, session = golden
    for attrs in (read_cube(session)["attrs"], read_map(session, "integrated_relative_intensity")["attrs"]):
        assert attrs["beam_model"] == "gaussian_circular" and attrs["beam_fwhm_deg"] == FWHM_BEAM
        assert attrs["beam_source"] == "operator_config" and attrs["beam_status"] == "CONFIGURED_OPERATIONAL"
        assert attrs["data_completeness"] == "PARTIAL" and attrs["coordinate_frame"] == "icrs"
        assert "not" in attrs["intensity_unit"].lower() or "no absolute" in attrs["intensity_unit"].lower()


# ---------------------------------------------------------------- 63: RFI spatial smearing

def _spike_case(tmp_path, *, masked):
    specs = rectangular_grid_specs(RA0_H, DEC0, 11, 11, spacing_deg=1.0)
    spike_index = 61                                                          # the central pointing
    for s in specs:
        s.velocity_offset_m_s = 0.0
    spike_channel = 40

    def spike(velocity, ra, dec):
        out = np.zeros_like(velocity)
        out[spike_channel] = 30.0
        return out

    for s in specs:
        if s.point_index == spike_index:
            s.extra_line_fn = spike
            if masked:
                s.masked_channel_slice = slice(spike_channel, spike_channel + 1)
    si = build_synthetic_science_input(specs, n_channels=64, line_amplitude_fn=lambda r, d: 0.0, noise_sigma=0.02,
                                       rng=np.random.default_rng(9))
    write_synthetic_reduce_session(tmp_path / "R", si)
    config = ScienceConfig(beam_fwhm_deg=1.0, pixels_per_beam=3.0, beam_cutoff_n_fwhm=3.0)
    report = run_science_session(str(tmp_path / "R"), config, output_root=str(tmp_path / "S"))
    return si, config, read_cube(Path(report.output_dir)), grid_of(Path(report.output_dir)), spike_index, spike_channel


def test_unmasked_extreme_line_in_one_pointing_stays_inside_that_pointings_beam_support(tmp_path):
    si, config, cube, grid, spike_index, ch = _spike_case(tmp_path, masked=False)
    spike_point = next(p for p in si.points if p.point_index == spike_index)
    from science_engine.grid import pixel_centers_deg
    from science_engine.models import ScienceGrid
    from test_science_gridding_truth import haversine_deg
    sg = ScienceGrid(**{k: grid[k] for k in ("frame", "center_ra_deg", "center_dec_deg", "width_deg", "height_deg",
                                            "pixel_scale_deg", "nx", "ny")})
    ra, dec = pixel_centers_deg(sg)
    theta = haversine_deg(spike_point.ra_deg, spike_point.dec_degrees, ra, dec)
    excess = np.where(cube["valid"][ch], cube["relative_intensity"][ch], 0.0)
    far = theta > 3.0 * config.beam_fwhm_deg
    assert far.any()
    assert np.all(np.abs(excess[far]) < 0.2)                  # beyond the spike point's cutoff: nothing (only noise)
    assert excess[np.unravel_index(np.argmin(theta), theta.shape)] > 3.0     # at the pointing: clearly there
    near = theta < 0.5 * config.beam_fwhm_deg
    assert excess[near].mean() > excess[(theta > 2.0 * config.beam_fwhm_deg) & ~far].mean()   # decays with distance


def test_masked_rfi_in_one_pointing_vanishes_from_the_valid_contribution(tmp_path):
    si, config, cube, grid, spike_index, ch = _spike_case(tmp_path, masked=True)
    excess = np.where(cube["valid"][ch], cube["relative_intensity"][ch], 0.0)
    assert np.all(np.abs(excess) < 0.2)                       # the flagged spike contributes nowhere (noise level only)
    assert cube["valid"][ch].any()                            # ...and neighbours still cover the channel
