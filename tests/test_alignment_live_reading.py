"""ALIGN live reading (operator, 2026-10-18): one display-only reading per point, written to alignment_live.json while
the run is still acquiring, so the ALIGN page can show under the log whether the reading changes where it should."""
import asyncio
import json

import h5py
import numpy as np
import pytest
import astropy.units as u
from astropy.coordinates import SkyCoord

import alignment
from tests.test_alignment_v2 import _runner_args

SR, CF = 2_400_000.0, 1_420_405_752.0


def _iq(amplitude=20.0, line_gain=1.0, n=600_000, seed=0):
    """White noise; line_gain > 1 raises the power within +/-260 kHz of the HI rest frequency (a broad line, as HI is)."""
    rng = np.random.default_rng(seed)
    z = amplitude * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    if line_gain != 1.0:
        spec = np.fft.fft(z)
        f = CF + np.fft.fftfreq(n, 1 / SR)
        spec[np.abs(f - alignment.HI_REST_HZ) <= 260_000] *= np.sqrt(line_gain)
        z = np.fft.ifft(spec)
    raw = np.empty(2 * n, dtype=np.uint8)
    raw[0::2] = np.clip(np.round(z.real + 127.5), 0, 255)
    raw[1::2] = np.clip(np.round(z.imag + 127.5), 0, 255)
    return raw


def test_live_reading_sees_band_power_and_an_hi_window_excess():
    quiet, loud = alignment.live_reading(_iq(10.0), SR, CF), alignment.live_reading(_iq(20.0), SR, CF)
    assert loud["power_db"] - quiet["power_db"] == pytest.approx(6.0, abs=0.3)      # amplitude x2 -> +6 dB
    assert abs(quiet["hi_excess_pct"]) < 5.0                                       # white noise: no line excess
    assert alignment.live_reading(_iq(10.0, line_gain=1.3), SR, CF)["hi_excess_pct"] == pytest.approx(30.0, abs=6.0)
    assert quiet["blocks"] == 128                                                  # bounded cost, any capture length


def test_live_reading_without_the_hi_window_in_band_reports_none_and_short_iq_is_refused():
    assert alignment.live_reading(_iq(10.0), SR, CF + 5e6)["hi_excess_pct"] is None
    with pytest.raises(ValueError):
        alignment.live_reading(_iq(10.0, n=4000), SR, CF)


class _Mount:
    def __init__(self, fail_at=()):
        self.fail_at, self.n = set(fail_at), 0

    async def goto(self, ra, dec):
        self.n += 1
        return self.n not in self.fail_at

    async def get_coordinates(self, force_refresh=False):
        return 1.0, -30.0


class _Sdr:
    async def capture(self, duration, path, sample_rate, attrs):
        with h5py.File(path, "w") as h:
            h["iq_data"] = _iq(15.0)
            h.attrs["file_state"] = "complete"


def _runner(tmp_path, monkeypatch):
    monkeypatch.setattr(alignment.sdr_tuning, "tuning_attrs", lambda tuning: {})
    runner = alignment.AlignmentRunner(_runner_args(tmp_path, "sun"))
    runner.args.settle = 0
    runner.tuning = {"applied": {"center_frequency_hz": CF, "sample_rate_hz": SR}}
    runner.sdr = _Sdr()
    return runner


def test_acquire_writes_the_pattern_first_then_one_reading_per_point_including_failures(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    runner.telescope = _Mount(fail_at={2})
    centre = SkyCoord(ra=1 * u.hourangle, dec=-30 * u.deg)
    positions = alignment.multiscale_pattern(centre, radii=(2.0,), ring_points=3)
    records = asyncio.run(runner.acquire("sun", positions, 10.0))
    live = json.loads((runner.output_dir / "alignment_live.json").read_text())
    assert live["reference"] == "sun" and live["planned_points"] == 4 and live["display_only"] is True
    assert [p["index"] for p in live["planned"]] == [1, 2, 3, 4]
    assert live["planned"][0]["east_deg"] == pytest.approx(0, abs=1e-9)
    assert np.hypot(live["planned"][1]["east_deg"], live["planned"][1]["north_deg"]) == pytest.approx(2.0, abs=1e-3)
    assert [p["status"] for p in live["points"]] == [r["status"] for r in records] == ["VALID", "CAPTURE_FAILED", "VALID", "VALID"]
    assert "power_db" not in live["points"][1] and live["points"][1]["error"] == "GOTO failed"
    assert all("power_db" in live["points"][i] for i in (0, 2, 3))


def test_a_live_file_that_cannot_be_written_never_breaks_the_acquisition(tmp_path, monkeypatch, capsys):
    runner = _runner(tmp_path, monkeypatch)
    runner.telescope = _Mount()
    monkeypatch.setattr(runner, "_flush_live", lambda live: (_ for _ in ()).throw(OSError("disk full")))
    centre = SkyCoord(ra=1 * u.hourangle, dec=-30 * u.deg)
    records = asyncio.run(runner.acquire("sun", alignment.multiscale_pattern(centre, radii=(2.0,), ring_points=3), 10.0))
    assert [r["status"] for r in records] == ["VALID"] * 4
    assert "live reading not written" in capsys.readouterr().out
