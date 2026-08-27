import inspect
import json
from pathlib import Path

import numpy as np
import pytest
import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time

import alignment


def test_import_runtime_and_safety_source():
    source = inspect.getsource(alignment)
    assert "socket.recv" not in source
    assert "gain='auto'" not in source and 'gain="auto"' not in source
    assert alignment.PROVISIONAL_BEAM_FWHM_DEG == 14.0


def test_ra_wrap_and_spherical_offset():
    center = SkyCoord(ra=359.5 * u.deg, dec=70 * u.deg)
    shifted = alignment.offset_coordinates(center, [2.0], [0.0])[0]
    assert 0 <= shifted.ra.deg < 360
    assert center.separation(shifted).deg == pytest.approx(2.0, abs=1e-8)
    assert shifted.ra.deg < 10.0


def test_template_convolution_constant_and_smoothing():
    catalog = SkyCoord(ra=[0, 2, 4] * u.deg, dec=[0, 0, 0] * u.deg)
    query = SkyCoord(ra=[1, 3] * u.deg, dec=[0, 0] * u.deg)
    constant = alignment.gaussian_convolved_template(query, catalog, np.ones(3) * 7)
    assert np.allclose(constant, 7)
    impulse = alignment.gaussian_convolved_template(query, catalog, np.array([0, 10, 0]), 14)
    assert np.all(impulse > 0) and np.all(impulse < 10)


@pytest.mark.parametrize("magnitude", [0.5, 1.0, 2.0, 3.0, 5.0])
@pytest.mark.parametrize("angle", [0, 45, 135, 225, 315])
def test_synthetic_offset_recovery(magnitude, angle):
    east = magnitude * np.cos(np.radians(angle))
    north = magnitude * np.sin(np.radians(angle))
    estimate, truth = alignment.simulate_offset_case(east, north, noise_fraction=.04,
                                                      seed=int(magnitude * 100 + angle))
    assert truth["error_angular_deg"] <= .25
    assert estimate.confidence >= .6


def test_low_confidence_flat_measurements():
    center = SkyCoord(ra=10 * u.deg, dec=-30 * u.deg)
    positions = alignment.multiscale_pattern(center)
    estimate = alignment.estimate_template_offset(
        positions, np.ones(len(positions)), center,
        lambda coords: alignment.synthetic_template(center, coords),
        search_levels=((1, .5),),
    )
    assert estimate.confidence == 0


@pytest.mark.parametrize("declination", [-70, 0, 65])
def test_offset_recovery_at_varied_declinations(declination):
    center = SkyCoord(ra=359 * u.deg, dec=declination * u.deg)
    estimate, truth = alignment.simulate_offset_case(1.4, -1.4, center=center,
                                                      noise_fraction=.03, seed=44)
    assert truth["error_angular_deg"] <= .25
    assert estimate.confidence >= .6


def test_hi_metric_uses_line_excess_and_reports_clipping():
    rate, size, segments = 2_400_000, 8192, 8
    rng = np.random.default_rng(7)
    noise = rng.normal(0, .05, size * segments) + 1j * rng.normal(0, .05, size * segments)
    tone = .25 * np.exp(2j * np.pi * np.arange(size * segments) * 40_000 / rate)
    iq = noise + tone
    raw = np.empty(iq.size * 2, dtype=np.uint8)
    raw[0::2] = np.clip(iq.real * 127.5 + 127.5, 0, 255).astype(np.uint8)
    raw[1::2] = np.clip(iq.imag * 127.5 + 127.5, 0, 255).astype(np.uint8)
    result = alignment.compute_hi_metric(raw, rate, alignment.HI_REST_HZ)
    assert result["metric"] > 1
    assert result["fft_segments"] == segments
    assert result["clipping_fraction"] == 0


def _runner_args(tmp_path, reference):
    catalog = tmp_path / "catalog.csv"
    catalog.write_text("point_id,ra_hours,dec_deg,tb_kelvin\n1,0,-30,10\n2,6,-30,20\n")
    config = tmp_path / "observer.json"
    config.write_text(json.dumps({"observer": {"latitude_deg": -33.4,
        "longitude_deg": -70.6, "elevation_m": 500}}))
    return alignment.parse_args(["--reference", reference, "--catalog", str(catalog),
                                 "--observer-config", str(config), "--output-dir", str(tmp_path / "out"),
                                 "--min-elevation", "-90"])


def test_sun_unavailable_and_auto_hi_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(alignment, "get_sun", lambda _: SkyCoord(ra=0 * u.deg, dec=90 * u.deg))
    with pytest.raises(RuntimeError, match="below horizon"):
        alignment.AlignmentRunner(_runner_args(tmp_path, "sun")).resolve_reference()
    reference, _, info = alignment.AlignmentRunner(_runner_args(tmp_path, "auto")).resolve_reference()
    assert reference == "hi" and info["auto_fallback_from_sun"]


def test_sun_eod_uses_apparent_equatorial_coordinates():
    obstime = Time("2026-08-26T16:55:00")
    sun = alignment.sun_eod(obstime)
    assert sun.frame.name == "cirs"
    assert sun.ra.hour == pytest.approx(10.3367, abs=.01)
    assert sun.dec.deg == pytest.approx(10.2305, abs=.05)


def test_sync_default_disabled_and_confidence_guard(tmp_path):
    args = _runner_args(tmp_path, "hi")
    assert not args.apply_sync
    assert not alignment.sync_allowed(False, .99)
    assert not alignment.sync_allowed(True, .64)
    assert alignment.sync_allowed(True, .65)


def test_json_and_png_generation(tmp_path):
    runner = alignment.AlignmentRunner(_runner_args(tmp_path, "hi"))
    center = SkyCoord(ra=1 * u.hourangle, dec=-30 * u.deg)
    runner.save("hi", center, [], None, {"selection_score": 1}, False, "PASS")
    result = json.loads((runner.output_dir / "alignment_result.json").read_text())
    assert result["beam_fwhm_deg"] == 14.0
    assert result["sync_applied"] is False
    assert (runner.output_dir / "alignment_diagnostic.png").is_file()
