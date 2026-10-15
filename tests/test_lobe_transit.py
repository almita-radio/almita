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
import astropy.units as u  # noqa: E402

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


def test_scan_plan_blocks_exactly_when_a_held_position_crosses_the_meridian_limit():
    # start times every 30 s around the Sun's transit (16:27 UTC): with a 4 s hold per position only a few starts put
    # a position across the +12 min limit while it is held - the plan must block those, and only those
    held_h = (2 + 2 + 8) / 3600 * 1.0027379
    blocked = 0
    for k in range(0, 160):
        t = Time("2026-10-15T16:00:00", scale="utc") + k * 30 * u.s
        p = lt.plan("scan", t, LOC, 10, 1.5, 5, 2, 1.0, 2, min_elevation_deg=10)
        crosses = any(q["hour_angle_h"] < 0.2 <= q["hour_angle_h"] + held_h for q in p["positions"])
        flagged = any("meridian limit" in q for q in p["problems"])
        assert crosses == flagged, (t.isot, crosses, flagged)
        blocked += flagged
    assert blocked > 0


def test_scan_plan_with_the_sun_far_from_the_meridian_has_no_meridian_problem():
    p = lt.plan("scan", Time("2026-10-15T13:00:00", scale="utc"), LOC, 10, 1.0, 5, 1, 1.0, 2, min_elevation_deg=5)
    assert not any("meridian" in q for q in p["problems"])


def test_spectrum_view_marks_the_injected_narrow_peak():
    raw = _iq(1024 * 200, line_bin=-75, line_amp=1.5)                           # -75 x 9.375 kHz = -703 kHz from the centre
    sp = lt.capture_spectrum(raw)
    v = lt.spectrum_view(sp, 1420405752.0, 2_400_000)
    k = int(np.argmin(np.abs(np.array(v["freq_mhz"]) - v["peak"]["freq_mhz"])))
    assert v["peak"]["excess_db"] > 3 and abs(v["peak"]["offset_khz"] - (-703.125)) < 2.4
    assert v["usable"][k] and not v["usable"][512] and len(v["db"]) == lt.SPECTRUM_NFFT


def test_sample_with_fake_hardware_writes_csv_spectrum_and_survives_a_failed_capture(tmp_path):
    """sample() end to end without hardware: a good capture and a failed one (2026-10-15: a misplaced log line made
    every GOOD sample raise NameError - only a run with fakes catches that)."""
    import asyncio, csv as _csv, types
    from sdr_capture import write_hdf5_atomic

    class FakeSDR:
        def __init__(self):
            self.calls = 0

        async def capture(self, seconds, path, rate, meta):
            self.calls += 1
            if self.calls == 2:
                raise OSError("simulated capture failure")
            iq = _iq(int(rate * seconds) // 2 // 256 * 256)
            write_hdf5_atomic(path, iq, {"center_frequency_hz": 1420405752, "sample_rate_hz": rate, "num_samples": len(iq) // 2,
                                         "capture_status": "complete", "final_filename": Path(path).name, "created_at": "x"},
                              expected_samples=len(iq) // 2)

    class FakeScope:
        async def get_coordinates(self, force_refresh=False):
            return 13.0, -8.5

    args = types.SimpleNamespace(output_dir=str(tmp_path / "run"), observer_config="observer_config.json", capture_seconds=0.2,
                                 sample_rate=2_400_000, gain=40.2, mode="scan", max_clipping=0.001)
    run = lt.LobeRun(args)
    run.temps = None
    run.sdr, run.telescope = FakeSDR(), FakeScope()
    run.tuning = {"requested": {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain_db": 40.2},
                  "applied": {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain_db": 40.2},
                  "evidence": "TEST", "evidence_detail": "", "pll_not_locked_count": 0, "tuned_utc": "x", "rtl_agc": "off: test"}
    with open(run.csv_path, "w", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=lt.CSV_FIELDS); w.writeheader()
        good = asyncio.run(run.sample(w, fh, "on"))
        bad = asyncio.run(run.sample(w, fh, "on"))
    assert good["status"] == "VALID" and good["power_lin"] > 0 and bad["status"] == "CAPTURE_FAILED"
    spectra = np.fromfile(tmp_path / "run" / "lobe_spectra.f32", dtype=np.float32).reshape(-1, lt.SPECTRUM_NFFT)
    assert spectra.shape[0] == 2 and np.isfinite(spectra[0]).all() and np.isnan(spectra[1]).all()
    assert [r["status"] for r in lt.read_samples(run.csv_path)] == ["VALID", "CAPTURE_FAILED"]


class _FakeMount:
    def __init__(self):
        self.calls, self.tracking = [], False

    async def goto(self, ra, dec):
        self.calls.append(("goto", round(ra, 3), round(dec, 3))); self.ra, self.dec = ra, dec
        return True

    async def set_track_mode(self, mode):
        self.calls.append(("mode", mode)); return True

    async def wait_track_mode(self, mode, timeout=5.0):
        return True

    async def set_tracking(self, on):
        self.calls.append(("tracking", on)); self.tracking = on; return True

    async def wait_tracking_state(self, on, timeout=5.0):
        return self.tracking == on

    async def get_tracking_state(self, timeout=1.0):
        return "on" if self.tracking else "off"

    async def get_coordinates(self, force_refresh=False):
        return self.ra, self.dec

    async def disconnect(self):
        self.calls.append(("disconnect",))


def _fake_run(tmp_path, monkeypatch, mode, plan_out):
    import types
    from sdr_capture import write_hdf5_atomic

    class FakeSDR:
        async def capture(self, seconds, path, rate, meta):
            iq = _iq(int(rate * seconds) // 2 // 256 * 256)
            write_hdf5_atomic(path, iq, {"center_frequency_hz": 1420405752, "sample_rate_hz": rate, "num_samples": len(iq) // 2,
                                         "capture_status": "complete", "final_filename": Path(path).name, "created_at": "x"},
                              expected_samples=len(iq) // 2)

        async def close(self):
            pass

    args = lt.parse_args(["--mode", mode, "--output-dir", str(tmp_path / mode), "--capture-seconds", "0.2", "--every", "1",
                          "--settle", "0"])
    run = lt.LobeRun(args)
    run.temps = None
    mount = _FakeMount()

    async def fake_connect():
        run.telescope, run.sdr = mount, FakeSDR()
        run.tuning = {"requested": {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain_db": 40.2},
                      "applied": {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain_db": 40.2},
                      "evidence": "TEST", "evidence_detail": "", "pll_not_locked_count": 0, "tuned_utc": "x", "rtl_agc": "off: test"}
    monkeypatch.setattr(run, "connect", fake_connect)
    monkeypatch.setattr(lt, "plan", lambda *a, **k: plan_out)
    import asyncio
    rc = asyncio.run(run.run())
    return rc, mount, json.loads((tmp_path / mode / "lobe_result.json").read_text()), lt.read_samples(run.csv_path)


def test_a_full_scan_run_with_fake_hardware_completes_and_leaves_tracking_off(tmp_path, monkeypatch):
    sun = lt.sun_eod(Time.now())
    positions = [{"offset_deg": x, "ra_hours": 0, "dec_deg": float(sun.dec.deg), "alt_deg": 40, "az_deg": 90} for x in (-2.0, 0.0, 2.0)]
    p = {"mode": "scan", "half_range_deg": 2.0, "duration_s": 30, "point_alt_deg": 40.0, "positions": positions, "problems": []}
    rc, mount, res, rows = _fake_run(tmp_path, monkeypatch, "scan", p)
    assert rc == 0 and res["status"] == "COMPLETE" and [r["status"] for r in rows] == ["VALID"] * 3
    assert ("mode", "solar") in mount.calls and sum(c[0] == "goto" for c in mount.calls) == 3
    assert mount.calls[-2:] == [("tracking", False), ("disconnect",)]


def test_a_drift_run_with_fake_hardware_samples_until_the_sun_passes_and_stops_at_the_time_limit(tmp_path, monkeypatch):
    sun = lt.sun_eod(Time.now())
    # pointing far east of the Sun so the offset never reaches the half range: the time limit ends it
    p = {"mode": "drift", "half_range_deg": 30.0, "duration_s": 1.0, "point_alt_deg": 40.0, "point_ra_hours": (float(sun.ra.hour) + 1) % 24,
         "point_dec_deg": float(sun.dec.deg), "crossing_utc": "x", "problems": []}
    monkeypatch.setattr(lt.time, "monotonic", _clock())
    rc, mount, res, rows = _fake_run(tmp_path, monkeypatch, "drift", p)
    assert res["status"] == "COMPLETE" and rows and all(r["status"] == "VALID" for r in rows)
    assert ("tracking", False) in mount.calls and not any(c == ("tracking", True) for c in mount.calls)
    assert any("time limit" in n for n in res["notes"])


def _clock():
    t = {"v": 0.0}

    def mono():
        t["v"] += 20.0                      # every call jumps 20 s: the drift loop reaches its deadline after a few samples
        return t["v"]
    return mono
