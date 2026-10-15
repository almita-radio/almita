"""LOBE (lobe_transit.py): geometry of the drift/scan plan, the band-power metric and the transit fit. No hardware."""
import csv
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lobe_transit as lt  # noqa: E402
from astropy.time import Time  # noqa: E402

CFG = json.loads((Path(lt.ROOT) / "observer_config.json").read_text())
LOC = lt.observer_location(CFG)
NOON = Time("2026-10-15T16:30:00", scale="utc")          # ~13:30 Chile: the Sun is high


def test_offset_sign_goes_from_negative_to_positive_as_the_sun_drifts_west():
    # fixed pointing; the Sun's RA is larger (east) before the crossing and smaller after it
    assert lt.offset_deg(13.10, -9.0, 13.00) < 0
    assert lt.offset_deg(12.90, -9.0, 13.00) > 0
    assert lt.offset_deg(13.0, -9.0, 13.0) == pytest.approx(0.0)
    # cos(dec) applied: 1 h of RA at dec -60 is 7.5 deg of sky
    assert lt.offset_deg(12.0, -60.0, 13.0) == pytest.approx(7.5, abs=1e-9)
    # wraps across 0h
    assert lt.offset_deg(23.95, 0.0, 0.05) == pytest.approx(1.5, abs=1e-6)


def test_drift_plan_points_west_of_the_sun_and_lasts_twice_the_half_range():
    p = lt.plan("drift", NOON, LOC, expected_fwhm_deg=20, half_range_fwhm=1.5, every_s=5, capture_s=1,
                scan_step_deg=1, settle_s=2, min_elevation_deg=10)
    assert p["half_range_deg"] == pytest.approx(30.0)
    start_offset = lt.offset_deg(p["sun_ra_hours"], p["sun_dec_deg"], p["point_ra_hours"])
    assert start_offset == pytest.approx(-30.0, abs=1e-6)                 # the Sun starts 30 deg east of the beam
    assert p["duration_s"] / 3600 == pytest.approx(2 * 30 / math.cos(math.radians(p["sun_dec_deg"])) / 15, rel=1e-6)
    assert p["point_dec_deg"] == pytest.approx(p["sun_dec_deg"])
    assert p["problems"] == []


def test_drift_plan_blocks_a_low_pointing():
    late = Time("2026-10-15T21:30:00", scale="utc")                       # 18:30 Chile: low Sun
    p = lt.plan("drift", late, LOC, 20, 1.5, 5, 1, 1, 2, min_elevation_deg=10)
    assert p["problems"], "a pointing/crossing under the minimum elevation must be reported"


def test_half_range_is_capped():
    assert lt.half_range_deg(90, 4) == lt.MAX_HALF_RANGE_DEG


def test_scan_plan_steps_across_the_sun():
    p = lt.plan("scan", NOON, LOC, 10, 1.5, 5, 1, scan_step_deg=1.0, settle_s=2, min_elevation_deg=10)
    offs = [q["offset_deg"] for q in p["positions"]]
    assert offs[0] == pytest.approx(-15) and offs[-1] == pytest.approx(15) and len(offs) == 31
    assert p["samples_expected"] == 31


def _iq(n_complex, noise=8.0, line_bin=None, line_amp=0.0, seed=0):
    rng = np.random.default_rng(seed)
    z = noise * (rng.standard_normal(n_complex) + 1j * rng.standard_normal(n_complex))
    if line_bin is not None:
        k = np.arange(n_complex)
        z += line_amp * np.exp(2j * np.pi * (line_bin / lt.NFFT) * k)
    raw = np.empty(2 * n_complex, dtype=np.uint8)
    raw[0::2] = np.clip(np.round(z.real + 127.4), 0, 255)
    raw[1::2] = np.clip(np.round(z.imag + 127.4), 0, 255)
    return raw


def test_band_power_ignores_a_narrow_line_and_scales_with_noise_power():
    base = lt.band_power(_iq(256 * 400))
    with_line = lt.band_power(_iq(256 * 400, line_bin=-70, line_amp=6.0))     # strong narrow line (like BL-072's)
    assert with_line["power_lin"] == pytest.approx(base["power_lin"], rel=0.03)
    double = lt.band_power(_iq(256 * 400, noise=8.0 * math.sqrt(2)))
    assert double["power_lin"] / base["power_lin"] == pytest.approx(2.0, rel=0.05)
    assert base["clipping_fraction"] == 0.0


def test_band_power_reports_clipping():
    m = lt.band_power(_iq(256 * 50, noise=200.0))
    assert m["clipping_fraction"] > 0.3


def _transit(x0=1.5, fwhm=18.0, amp=0.08, b0=1.0, b1=0.0005, half=30, n=400, noise=0.004, seed=3):
    rng = np.random.default_rng(seed)
    x = np.linspace(-half, half, n)
    y = b0 + b1 * x + amp * np.exp(-4 * math.log(2) * (x - x0) ** 2 / fwhm ** 2) + noise * rng.standard_normal(n)
    return x, y


def test_fit_recovers_width_and_centre_on_a_sloped_background():
    x, y = _transit()
    f = lt.fit_transit(x, y)
    assert f["status"] == "FIT", f.get("flags")
    assert f["fwhm_deg"] == pytest.approx(18.0, rel=0.05)
    assert f["x0_deg"] == pytest.approx(1.5, abs=0.4)
    assert f["background_slope_per_deg"] == pytest.approx(0.0005, abs=2e-4)
    assert f["snr"] > 10


def test_fit_flags_a_range_that_is_too_short_for_the_beam():
    x, y = _transit(fwhm=40.0, half=12)
    f = lt.fit_transit(x, y)
    assert any("RANGE TOO SHORT" in w for w in f["flags"])


def test_fit_flags_no_transit_in_pure_noise():
    x, y = _transit(amp=0.0)
    f = lt.fit_transit(x, y)
    assert any("NO CLEAR TRANSIT" in w for w in f["flags"])


def test_fit_samples_skips_clipped_and_failed_rows(tmp_path):
    x, y = _transit(n=60)
    path = tmp_path / "lobe_samples.csv"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=lt.CSV_FIELDS)
        w.writeheader()
        for i, (xi, yi) in enumerate(zip(x, y)):
            status = "CLIPPED" if i == 10 else ("CAPTURE_FAILED" if i == 11 else "VALID")
            w.writerow({"index": i, "offset_deg": xi, "power_lin": yi * (50 if i == 10 else 1), "clipping_fraction": 0.0,
                        "status": status})
    rows = lt.read_samples(path)
    assert len(rows) == 60
    f = lt.fit_samples(rows, max_clipping=0.001)
    assert f["n"] == 58
