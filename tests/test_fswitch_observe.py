"""Frequency switching in OBSERVE / REDUCE (BL-100; operator 2026-11-09: "descongela y aplica" - the lab copies'
changes moved into capture.py, observation_spec/plan/orchestrator and reduce_engine). A simulated SDR whose band
shape follows the tuning and whose sky line stays fixed in RF: the capture writes one valid HDF5 with the raw I/Q of
every segment, and REDUCE finds the line back at its velocity and height with the band shape gone. An unswitched
spec and capture stay exactly what they were."""
import asyncio

import numpy as np
import pytest

import fswitch_capture as FC
import observation_orchestrator as orch
import observation_plan
import observation_spec
from reduce_engine import fswitch_fold
from reduce_engine.config import ReduceConfig

RATE, F0, LINE_V_KMS, LINE_AMP = 2_400_000, 1_420_405_752, -20.0, 0.08


class FakeSDR:
    """configure/flush_buffer/capture_iq like SDRCapture in network mode; the spectrum it delivers = a band shape
    fixed in baseband x (1 + a Gaussian HI line fixed in RF), noise rms ~ 9 ADC codes."""
    mode, host, port = "network", "fake", 0

    def __init__(self, seed=5):
        self.f, self.rng, self.retune_settle_seconds, self.timing_callback, self.tuned = F0, np.random.default_rng(seed), 0.5, None, []

    async def configure(self, center_freq, sample_rate, gain):
        if center_freq != self.f:
            self.tuned.append(center_freq)
        self.f = center_freq

    async def flush_buffer(self):
        return 0

    async def capture_iq(self, duration, sample_rate):
        n, blk = int(duration * sample_rate), 1 << 16
        bb = np.fft.fftfreq(blk, 1 / sample_rate)
        shape = (1 + 0.12 * np.sin(2 * np.pi * bb / 900e3)) * np.exp(-0.5 * (bb / 1.1e6) ** 4)
        line_hz = F0 * (1 - LINE_V_KMS / 299792.458)
        sky = 1 + LINE_AMP * np.exp(-0.5 * ((self.f + bb - line_hz) / 40e3) ** 2)
        out = []
        for _ in range(int(np.ceil(n / blk))):
            w = (self.rng.normal(size=blk) + 1j * self.rng.normal(size=blk))
            out.append(np.fft.ifft(np.fft.fft(w) * np.sqrt(shape * sky)) * 9.0)
        z = np.concatenate(out)[:n]
        raw = np.empty(2 * n, np.uint8)
        raw[0::2] = np.clip(np.round(z.real + 127.5), 0, 255)
        raw[1::2] = np.clip(np.round(z.imag + 127.5), 0, 255)
        return raw


@pytest.fixture(scope="module")
def capture(tmp_path_factory):
    out = tmp_path_factory.mktemp("cap") / "point_001.h5"
    sdr = FakeSDR()
    m = asyncio.run(FC.capture_point(sdr, f0_hz=F0, sample_rate=RATE, gain_db=40.2, duration_s=8.0,
                                     output_file=str(out), metadata={"capture_started_at": "2026-11-12T03:00:00+00:00"}))
    return out, m, sdr


def test_the_capture_writes_one_valid_hdf5_with_every_segment(capture):
    from sdr_capture import validate_hdf5_capture
    from reduce_engine.ingest import read_capture
    path, m, sdr = capture
    assert validate_hdf5_capture(path)["capture_status"] == "success"
    iq, attrs = read_capture(path)
    seg = FC.read_segments(attrs)
    assert [s["lo_hz"] for s in seg] == [F0 - 600000, F0 + 600000, F0 - 600000]       # 2 cycles: A BB A
    assert sum(s["n_samples"] for s in seg) == iq.size // 2 == m.total_samples == 8 * RATE
    assert int(attrs["center_frequency_hz"]) == F0 and attrs["sdr_frequency_mode"] == "frequency_switched"
    assert sdr.tuned[-1] == F0 and sdr.retune_settle_seconds == 0.5                    # left as capture.py expects


def test_reduce_finds_the_line_at_its_velocity_and_height(capture):
    from reduce_engine.ingest import read_capture
    iq, attrs = read_capture(capture[0])
    sw = fswitch_fold.switched_spectrum(iq, attrs, ReduceConfig())
    v = (1 - sw.frequency_hz / F0) * 299792.458
    ok = np.isfinite(sw.fractional)
    assert ok.mean() > 0.95 and abs(v[ok]).max() < 86                                  # the +/-0.4 MHz folded band
    win = ok & (np.abs(v - LINE_V_KMS) < 25)                      # centroid of a broad (sigma 8 km/s) line
    w = np.clip(sw.fractional[win], 0, None)
    assert float(np.sum(w * v[win]) / np.sum(w)) == pytest.approx(LINE_V_KMS, abs=2)
    # the line's AREA over +/-25 km/s (radiometer noise of 4 s per LO: ~3.6 % per bin after the fold, ~0.13 % on this
    # mean; a single +/-3 km/s window is noise-dominated): Gaussian sigma 40 kHz = 8.44 km/s
    sig = 40e3 / F0 * 299792.458
    from math import erf, pi, sqrt
    # B sees the line as 1/(1+a) - 1, so the fold gives a(1 + 1/(1+a))/2. Checked unbiased over 6 seeds (mean 3.263 %
    # vs 3.251 %, scatter 0.185 % = the predicted radiometer noise): 3 sigma tolerance
    peak = LINE_AMP * (1 + 1 / (1 + LINE_AMP)) / 2
    expected = peak * sig * sqrt(2 * pi) / 50.0 * erf(25 / (sig * sqrt(2)))
    assert np.mean(sw.fractional[ok & (np.abs(v - LINE_V_KMS) < 25)]) == pytest.approx(expected, abs=0.0056)
    off = ok & (np.abs(v - LINE_V_KMS) > 40)
    assert np.std(np.convolve(sw.fractional[off], np.ones(31) / 31, "valid")) < 0.01    # the 12 % band shape is gone
    assert abs(np.median(sw.fractional[off])) < 0.003


def test_reduce_routes_a_switched_capture_and_the_rfi_mask_steps_aside(capture):
    """reduce_point sends a capture with a segment table to the fold; the opt-in RFI MASK does not try to map its
    single-LO mask onto it."""
    import reduce_rfi_mask
    from reduce_engine.ingest import read_capture
    mr = reduce_rfi_mask.MaskedReduce(loader=lambda p: pytest.fail("no mask should be loaded"))
    iq, attrs = mr.read_capture(read_capture, capture[0])
    assert fswitch_fold.is_switched(attrs) and mr.current is None
    assert mr.points[str(capture[0])]["applied"] is False


def _raw():
    from tests.test_observation_spec import _valid_raw
    return _valid_raw()


def _validated(**capture):
    raw = _raw()
    raw["capture"].update(capture)
    return observation_spec.validate_spec_dict(raw)


def test_the_spec_takes_frequency_switching_only_when_asked():
    off = _validated()
    assert "frequency_switching" not in off["capture"]                      # unswitched: resolves (and hashes) as before
    assert _validated(frequency_switching=False)["capture"] == off["capture"]
    assert _validated(frequency_switching=True)["capture"]["frequency_switching"] is True
    with pytest.raises(observation_spec.ObservationSpecError, match="at least 4 s"):
        _validated(frequency_switching=True, seconds=3)


def test_the_orchestrator_passes_fswitch_and_the_plan_counts_its_pauses():
    plan = {"mosaic_csv_path": "m.csv", "requested": {"capture": {"settle_seconds": 2, "seconds": 8}, "grid": {"min_altitude_deg": 10}},
            "main": {"center_frequency_hz": F0, "sample_rate": RATE, "gain_db": 40.2}, "rfi_ref": {"enabled": False}}
    assert "--fswitch" not in orch._capture_args(plan, "rt")
    plan["requested"]["capture"]["frequency_switching"] = True
    assert "--fswitch" in orch._capture_args(plan, "rt")
    a = observation_plan.estimate_duration(100, 8.0, 2.0)
    b = observation_plan.estimate_duration(100, 8.0, 2.0, frequency_switching=True)
    assert b["estimated_seconds"] - a["estimated_seconds"] == pytest.approx(100 * 4 * 0.16)   # 2 cycles: 3 LO changes + back to f0


def test_capture_py_accepts_fswitch_and_keeps_the_ordinary_capture_otherwise():
    import capture
    src = open(capture.__file__).read()
    assert "'--fswitch'" in src and "frequency_switching=args.fswitch" in src
    ex = capture.CaptureExecutor.__init__.__defaults__
    assert False in ex                                                       # frequency_switching defaults to off


def test_quicklook_folds_a_switched_capture_without_any_profile(capture, tmp_path):
    import quicklook_spectrum as Q
    assert Q.is_switched_capture(capture[0])
    doc, arrays = Q.generate_quicklook_switched(capture[0], tmp_path / "spectrum")
    assert doc["frequency_switched"] is True and doc["calibration_profile"] is None
    assert doc["compatibility"]["status"] == "NOT_APPLICABLE_FREQUENCY_SWITCHED" and doc["integrity"]["unchanged"] is True
    f = arrays["frequency_hz"]
    assert abs(f - F0).max() <= 0.4e6 + 1e3 and arrays["valid_mask"].mean() > 0.95
    for png in ("quicklook_spectrum.png", "quicklook_fractional_excess.png", "quicklook_spectrum.json"):
        assert (tmp_path / "spectrum" / png).stat().st_size > 0


def test_with_switching_quicklook_needs_no_profile_anywhere():
    import observation_preflight as pf
    raw = _raw()
    raw["quicklook"]["calibration_profile_path"] = None
    with pytest.raises(observation_spec.ObservationSpecError, match="calibration_profile_path"):
        observation_spec.validate_spec_dict(raw)                              # unswitched: still required
    raw["capture"]["frequency_switching"] = True
    spec = observation_spec.validate_spec_dict(raw)
    assert spec["quicklook"]["enabled"] is True and spec["quicklook"]["calibration_profile_path"] is None
    assert pf._quicklook_check(spec["quicklook"], True)["status"] == pf.PASS
    assert pf._quicklook_calibration_match_check(spec["quicklook"], spec["main"], True)["status"] == pf.PASS
    args = orch._quicklook_args({"quicklook": spec["quicklook"]}, "s", "o", "r")
    assert "--calibration-profile" not in args
    assert orch._quicklook_args({"quicklook": {"calibration_profile_path": "p.json"}}, "s", "o", "r")[4:6] == ["--calibration-profile", "p.json"]


def test_the_observe_page_sends_frequency_switching_only_when_ticked_and_then_no_profile(tmp_path):
    from tests.test_web_frontend import OBSERVE_ROUTES, run_page
    driver = r"""
await until(() => $("hdr-status").textContent.indexOf("PLANNED") >= 0, 4000);
$("f-ql-enabled").checked = true; $("f-ql-cal").value = "data/calibration/x.json";
$("observe-form").requestSubmit();
await until(() => window.__count("POST /api/observe/plan") === 1, 4000);
$("f-fswitch").checked = true;
await sleep(400);
$("observe-form").requestSubmit();
await until(() => window.__count("POST /api/observe/plan") === 2, 4000);
const posts = window.__calls.filter((c) => c.key === "POST /api/observe/plan").map((c) => c.body);
out.off = [posts[0].capture, posts[0].quicklook.calibration_profile_path];
out.on = [posts[1].capture, posts[1].quicklook.calibration_profile_path];
"""
    out = run_page(tmp_path, "observe", OBSERVE_ROUTES, driver, budget=15000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["off"][0] == {"seconds": 10, "settle_seconds": 2} and out["off"][1] == "data/calibration/x.json"
    assert out["on"][0] == {"seconds": 10, "settle_seconds": 2, "frequency_switching": True} and out["on"][1] is None
