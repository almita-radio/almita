"""BL-105 (operator 2026-11-10): frequency switching with a configurable centre velocity.

fswitch_center plans each point (a flat-rotation-curve model through the beam, the terminal side by quadrant, LSRK ->
topocentric with REDUCE's own shift, velocity -> sky frequency); capture.py hands the centre to fswitch_capture,
which tunes around it, returns the SDR to the operating tuning and records the line window; REDUCE's fold fits its
baseline where the line is not - before the fold when the baseband has room, after it when a broad inner-Galaxy
line fills the baseband. A capture without a centre is folded exactly as before."""
import asyncio
import math

import numpy as np
import pytest

import fswitch_capture as FC
import fswitch_center as fc
import observation_orchestrator as orch
import observation_spec
from reduce_engine import fswitch_fold
from reduce_engine.config import ReduceConfig
from tests.test_fswitch_observe import FakeSDR, RATE, F0, _validated

C = 299792.458
REST = ReduceConfig().hi_rest_frequency_hz


# ------------------------------------------------------------------ planning
def test_the_model_puts_the_terminal_edge_where_the_rotation_curve_says():
    for l in (30.0, 45.0, 60.0):
        tangent = fc.V0_KM_S * (1 - math.sin(math.radians(l)))
        vmin, vmax = fc.model_line_window_lsrk(l, 0.0, beam_fwhm_deg=1.0)       # a pencil beam: no smearing
        assert vmax == pytest.approx(tangent + fc.DISPERSION_KM_S, abs=3)
        assert vmin < 0                                                           # gas beyond the solar circle
    lo4, hi4 = fc.model_line_window_lsrk(330.0, 0.0, beam_fwhm_deg=1.0)          # 4th quadrant: the mirror image
    assert lo4 == pytest.approx(-(fc.V0_KM_S * (1 - math.sin(math.radians(30))) + fc.DISPERSION_KM_S), abs=3)
    assert fc.model_line_window_lsrk(40.0, 60.0) == (-fc.LOCAL_KM_S, fc.LOCAL_KM_S) or \
        max(map(abs, fc.model_line_window_lsrk(40.0, 60.0))) < 60                 # high latitude: local gas only


def test_the_centre_goes_to_the_terminal_side_and_stays_at_zero_when_the_line_fits():
    H, M = fc.HALF_WINDOW_KM_S, fc.EDGE_MARGIN_KM_S
    assert fc.choose_centre_lsrk((-25.0, 25.0), 40.0) == 0.0
    c = fc.choose_centre_lsrk((-120.0, 165.0), 30.0)                              # first quadrant: positive edge
    assert c == pytest.approx(165 + M - H) and c + H == pytest.approx(165 + M)
    c4 = fc.choose_centre_lsrk((-165.0, 120.0), 330.0)                            # fourth quadrant: negative edge
    assert c4 == pytest.approx(-165 - M + H)
    assert fc.choose_centre_lsrk((-140.0, 70.0), 60.0) > 0                        # QI keeps the terminal (+) side


def test_parse_and_the_doppler_direction():
    assert fc.parse_centre(None) is None and fc.parse_centre("") is None and fc.parse_centre(" AUTO ") == "auto"
    assert fc.parse_centre("270") == 270.0
    for bad in ("fast", "nan", 900):
        with pytest.raises((ValueError, TypeError)):
            fc.parse_centre(bad)
    assert fc.velocity_to_frequency_hz(100.0) < REST < fc.velocity_to_frequency_hz(-100.0)   # receding = lower
    assert fc.velocity_to_frequency_hz(0.0) == pytest.approx(REST)


def test_plan_point_uses_reduces_own_lsrk_shift():
    obs = {"latitude_deg": -33.45, "longitude_deg": -70.66, "elevation_m": 570}
    p = fc.plan_point(270, 5.39, -69.75, "2026-11-12T23:00:00", obs)             # the LMC, fixed centre
    a = p["attrs"]
    assert a["fswitch_centre_mode"] == "fixed" and a["fswitch_centre_lsrk_km_s"] == 270
    assert a["fswitch_centre_topo_km_s"] == pytest.approx(270 - a["fswitch_lsrk_shift_km_s"], abs=1e-3)
    assert p["centre_frequency_hz"] == pytest.approx(REST * (1 - a["fswitch_centre_topo_km_s"] / C), abs=5)   # 1 m/s rounding
    w = a["fswitch_line_window_topo_km_s"]
    assert w[0] < a["fswitch_centre_topo_km_s"] < w[1]
    auto = fc.plan_point("auto", 18.8, -5.0, "2026-11-12T20:00:00", obs)        # the plane at l ~ 28
    assert auto["attrs"]["fswitch_centre_mode"] == "auto" and auto["attrs"]["fswitch_centre_lsrk_km_s"] > 80
    assert 27 < auto["attrs"]["galactic_l_deg"] < 29


# ------------------------------------------------------------------ capture + fold
class BroadSDR(FakeSDR):
    """The sky: a line with soft edges between v_lo and v_hi (topocentric), amplitude `amp`, fixed in RF."""
    def __init__(self, v_lo, v_hi, amp, seed=7, gauss=None, wing=None):
        super().__init__(seed)
        self.v_lo, self.v_hi, self.amp, self.gauss, self.wing = v_lo, v_hi, amp, gauss, wing

    async def capture_iq(self, duration, sample_rate):
        n, blk = int(duration * sample_rate), 1 << 16
        bb = np.fft.fftfreq(blk, 1 / sample_rate)
        shape = (1 + 0.12 * np.sin(2 * np.pi * bb / 900e3)) * np.exp(-0.5 * (bb / 1.1e6) ** 4)
        v = (1 - (self.f + bb) / REST) * C
        if self.gauss:
            prof = np.exp(-0.5 * ((v - self.gauss[0]) / self.gauss[1]) ** 2)
        else:
            edge = 6.0
            prof = 1 / (1 + np.exp(-(v - self.v_lo) / edge)) / (1 + np.exp((v - self.v_hi) / edge))
            if self.wing:                                     # weak far-disc gas: (v_from, v_to, relative height)
                w0, w1, h = self.wing
                prof = prof + h / (1 + np.exp(-(v - w0) / edge)) / (1 + np.exp((v - w1) / edge))
        sky = 1 + self.amp * prof
        out = []
        for _ in range(int(np.ceil(n / blk))):
            w = (self.rng.normal(size=blk) + 1j * self.rng.normal(size=blk))
            out.append(np.fft.ifft(np.fft.fft(w) * np.sqrt(shape * sky)) * 9.0)
        z = np.concatenate(out)[:n]
        raw = np.empty(2 * n, np.uint8)
        raw[0::2] = np.clip(np.round(z.real + 127.5), 0, 255)
        raw[1::2] = np.clip(np.round(z.imag + 127.5), 0, 255)
        return raw


def _capture(tmp_path, sdr, centre_topo, window_topo, name):
    f_c = int(round(REST * (1 - centre_topo / C)))
    extra = {"fswitch_centre_mode": "test", "fswitch_centre_topo_km_s": centre_topo,
             "fswitch_line_window_topo_km_s": list(window_topo)}
    out = tmp_path / name
    asyncio.run(FC.capture_point(sdr, f0_hz=f_c, sample_rate=RATE, gain_db=40.2, duration_s=8.0, output_file=str(out),
                                 metadata={"capture_started_at": "2026-11-12T03:00:00+00:00"},
                                 home_hz=F0, extra_attrs=extra))
    from reduce_engine.ingest import read_capture
    iq, attrs = read_capture(out)
    return fswitch_fold.switched_spectrum(iq, attrs, ReduceConfig()), attrs, f_c


def _broad(tmp_path, v_lo, v_hi, name, seed=7, wing=None):
    amp = 0.06
    window = (min(v_lo, wing[0] if wing else v_lo) - 20, v_hi + 20)
    centre = window[1] + fc.EDGE_MARGIN_KM_S - fc.HALF_WINDOW_KM_S                # what fswitch_center would pick
    sdr = BroadSDR(v_lo, v_hi, amp, seed=seed, wing=wing)
    sw, attrs, f_c = _capture(tmp_path, sdr, centre, window, name)
    assert {f_c - 600_000, f_c + 600_000} <= set(sdr.tuned)                      # LO A and B straddle the centre
    assert sdr.tuned[-1] == F0 and sdr.f == F0                                    # ... and back home afterwards
    assert attrs["center_frequency_hz"] == f_c and "fswitch_line_window_topo_km_s" in attrs
    v = (1 - sw.frequency_hz / REST) * C
    ok = np.isfinite(sw.fractional)
    assert v[ok].min() == pytest.approx(centre - fc.HALF_WINDOW_KM_S, abs=2)
    assert v[ok].max() == pytest.approx(centre + fc.HALF_WINDOW_KM_S, abs=2)
    peak = amp * (1 + 1 / (1 + amp)) / 2
    inside = ok & (v > centre - 70) & (v < v_hi - 20)
    beyond = ok & (v > v_hi + 25)
    assert np.mean(sw.fractional[inside]) == pytest.approx(peak, abs=0.006)       # the line's height is kept
    assert abs(np.mean(sw.fractional[beyond])) < 0.004                            # flat beyond the edge
    sm = np.convolve(sw.fractional[ok], np.ones(15) / 15, "same")
    edge_at = v[ok][np.argmin(np.abs(sm - peak / 2) + 10 * (v[ok] < v_hi - 40))]
    assert edge_at == pytest.approx(v_hi, abs=8)                                  # the terminal edge where it is
    return sw


def test_a_broad_line_with_room_on_both_sides_keeps_the_baseline_before_the_fold(tmp_path):
    sw = _broad(tmp_path, 0.0, 180.0, "broad.h5")                                 # ~ l = 30: room left in the baseband
    assert sw.info["baseline_model"] == "switched_S_polynomial_degree_1_outside_both_line_window_images"


def test_a_line_filling_the_baseband_gets_its_baseline_after_the_fold(tmp_path):
    # ~ l = 20: strong gas 0..+180 plus the weak far disc (10 %) down to -80: together wider than the 253 km/s throw
    sw = _broad(tmp_path, 0.0, 180.0, "fill.h5", seed=9, wing=(-80.0, 0.0, 0.1))
    assert sw.info["baseline_model"].startswith("folded_polynomial_degree_")
    assert 0 < sw.info["ghost_overlap_fraction"] < 1                              # reported, never silent


def test_a_narrow_line_at_a_fixed_centre_uses_the_ordinary_baseline(tmp_path):
    sdr = BroadSDR(0, 0, 0.08, seed=11, gauss=(270.0, 8.0))                        # an LMC-like line at +270
    sw, attrs, f_c = _capture(tmp_path, sdr, 270.0, (190.0, 350.0), "narrow.h5")
    assert sw.info["baseline_model"] == "switched_S_polynomial_degree_1_outside_both_line_window_images"
    v = (1 - sw.frequency_hz / REST) * C
    ok = np.isfinite(sw.fractional)
    win = ok & (np.abs(v - 270) < 25)
    w = np.clip(sw.fractional[win], 0, None)
    assert float(np.sum(w * v[win]) / np.sum(w)) == pytest.approx(270.0, abs=2)
    assert abs(np.median(sw.fractional[ok & (np.abs(v - 270) > 40)])) < 0.003


def test_a_capture_without_a_centre_is_folded_exactly_as_before(tmp_path):
    sdr = FakeSDR(seed=5)
    out = tmp_path / "plain.h5"
    asyncio.run(FC.capture_point(sdr, f0_hz=F0, sample_rate=RATE, gain_db=40.2, duration_s=8.0, output_file=str(out),
                                 metadata={"capture_started_at": "2026-11-12T03:00:00+00:00"}))
    from reduce_engine.ingest import read_capture
    iq, attrs = read_capture(out)
    assert "fswitch_line_window_topo_km_s" not in attrs
    sw = fswitch_fold.switched_spectrum(iq, attrs, ReduceConfig())
    assert sw.info["baseline_model"] == "switched_S_polynomial_degree_1_outside_both_images"
    assert "line_window_topo_km_s" not in sw.info


# ------------------------------------------------------------------ spec / orchestrator / capture.py
def test_the_spec_takes_a_centre_only_with_switching_and_keeps_old_specs_identical():
    plain = _validated(frequency_switching=True)
    assert "frequency_switching_center" not in plain["capture"]
    assert _validated(frequency_switching=True, frequency_switching_center=None)["capture"] == plain["capture"]
    assert _validated(frequency_switching=True, frequency_switching_center="auto")["capture"]["frequency_switching_center"] == "auto"
    assert _validated(frequency_switching=True, frequency_switching_center=270)["capture"]["frequency_switching_center"] == 270.0
    with pytest.raises(observation_spec.ObservationSpecError, match="needs frequency_switching"):
        _validated(frequency_switching_center="auto")
    with pytest.raises(observation_spec.ObservationSpecError, match="frequency_switching_center"):
        _validated(frequency_switching=True, frequency_switching_center="sideways")


def test_the_orchestrator_passes_the_centre_to_capture_py():
    plan = {"mosaic_csv_path": "m.csv", "requested": {"capture": {"settle_seconds": 2, "seconds": 8, "frequency_switching": True},
                                                      "grid": {"min_altitude_deg": 10}},
            "main": {"center_frequency_hz": F0, "sample_rate": RATE, "gain_db": 40.2}, "rfi_ref": {"enabled": False}}
    args = orch._capture_args(plan, "rt")
    assert "--fswitch" in args and "--fswitch-center" not in args
    plan["requested"]["capture"]["frequency_switching_center"] = "auto"
    args = orch._capture_args(plan, "rt")
    assert args[args.index("--fswitch-center") + 1] == "auto"


def test_capture_py_wires_the_centre_and_refuses_it_without_switching():
    import capture
    src = open(capture.__file__).read()
    assert "'--fswitch-center'" in src and "fswitch_centre=args.fswitch_center" in src
    assert "home_hz=int(self.sdr_freq)" in src and "extra_attrs=extra" in src


def test_the_observe_page_sends_the_centre_only_with_switching_and_a_value(tmp_path):
    from tests.test_web_frontend import OBSERVE_ROUTES, run_page
    driver = r"""
await until(() => $("hdr-status").textContent.indexOf("PLANNED") >= 0, 4000);
out.disabled_at_start = $("f-fswitch-center").disabled;
$("f-fswitch").checked = true; $("f-fswitch").dispatchEvent(new Event("change"));
out.enabled_when_ticked = !$("f-fswitch-center").disabled;
const plan = async (n) => { $("observe-form").requestSubmit(); await until(() => window.__count("POST /api/observe/plan") === n, 4000); await sleep(300); };
await plan(1);                                                   // ticked, centre empty: exactly BL-100
$("f-fswitch-center").value = "auto"; await plan(2);
$("f-fswitch-center").value = " 270 "; await plan(3);
const posts = window.__calls.filter((c) => c.key === "POST /api/observe/plan").map((c) => c.body.capture);
out.caps = posts;
"""
    out = run_page(tmp_path, "observe", OBSERVE_ROUTES, driver, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["disabled_at_start"] is True and out["enabled_when_ticked"] is True
    assert out["caps"][0] == {"seconds": 10, "settle_seconds": 2, "frequency_switching": True}
    assert out["caps"][1]["frequency_switching_center"] == "auto"
    assert out["caps"][2]["frequency_switching_center"] == 270
