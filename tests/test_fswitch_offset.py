"""Configurable frequency-switching throw (operator 2026-11-11: "quiero aprovechar la circunpolaridad de las nubes").

With the default +/-600 kHz offset (253 km/s throw) the Milky Way's local gas at ~0 km/s puts its negative,
half-height ghost at ~+253 km/s - right on the Large Magellanic Cloud (~+260 LSRK), and at b ~ -33 that local gas is
several times brighter than the Cloud in a 20 deg beam. A +/-300 kHz offset (127 km/s throw) moves the ghost to
~+127 and widens the folded window to +/-148 km/s. The planner records the local gas and its ghosts as mask windows
so REDUCE never fits its baseline on them, and flags a ghost that lands in the line window."""
import asyncio

import numpy as np
import pytest

import fswitch_capture as FC
import fswitch_center as fc
import observation_orchestrator as orch
import observation_spec
from reduce_engine import fswitch_fold
from reduce_engine.config import ReduceConfig
from tests.test_fswitch_center import BroadSDR, C, REST
from tests.test_fswitch_observe import F0, RATE, _validated

OBS = {"latitude_deg": -33.45, "longitude_deg": -70.66, "elevation_m": 570}
LMC = (5.39, -69.75)
SMC = (0.88, -72.8)
WHEN = "2026-11-13T04:00:00"          # 01:00 Chile, the LMC at ~48 deg


def test_offset_parsing_and_the_window_it_gives():
    assert fc.parse_offset_hz(None) is None and fc.parse_offset_hz("") is None
    assert fc.parse_offset_hz("300000") == 300e3
    for bad in (100e3, 900e3, "nan", "wide"):
        with pytest.raises((ValueError, TypeError)):
            fc.parse_offset_hz(bad)
    assert fc.half_window_km_s() == pytest.approx(fc.HALF_WINDOW_KM_S)           # default unchanged: 84.4
    assert fc.half_window_km_s(300e3) == pytest.approx(147.7, abs=0.2)
    assert fc.throw_km_s() == pytest.approx(253.3, abs=0.2) and fc.throw_km_s(300e3) == pytest.approx(126.6, abs=0.2)


def test_the_lmc_needs_the_small_throw_and_the_smc_does_not():
    big = fc.plan_point(260, *LMC, WHEN, OBS)["attrs"]
    small = fc.plan_point(260, *LMC, WHEN, OBS, offset_hz=300e3)["attrs"]
    assert big["fswitch_local_ghost_in_line_window"] is True                      # 600 kHz: the ghost sits on the LMC
    assert small["fswitch_local_ghost_in_line_window"] is False
    assert big["fswitch_planned_offset_hz"] == 600e3 and small["fswitch_planned_offset_hz"] == 300e3
    assert small["fswitch_half_window_km_s"] == pytest.approx(147.7, abs=0.2)
    assert len(small["fswitch_mask_windows_topo_km_s"]) == 3                      # local gas + its two ghosts
    # the SMC (~+90..+200 LSRK) is fine at 600 kHz centred at +145; at +160 the +/-80 window's top edge already
    # reaches the ghost (+228..+278), so the planner flags it
    smc = fc.plan_point(145, *SMC, "2026-11-13T01:00:00", OBS)["attrs"]
    assert smc["fswitch_local_ghost_in_line_window"] is False
    assert fc.plan_point(160, *SMC, "2026-11-13T01:00:00", OBS)["attrs"]["fswitch_local_ghost_in_line_window"] is True
    # the line window of a fixed centre is never wider than BL-100's +/-80, whatever the throw
    w = small["fswitch_line_window_lsrk_km_s"]
    assert w[1] - w[0] == pytest.approx(2 * (fc.HALF_WINDOW_KM_S - 4), abs=0.05)


def test_auto_and_the_default_plan_are_unchanged():
    auto = fc.plan_point("auto", 18.8, -5.0, "2026-11-12T20:00:00", OBS)["attrs"]
    assert auto["fswitch_mask_windows_topo_km_s"] == []                           # the line window holds the local gas
    assert auto["fswitch_local_ghost_in_line_window"] is False
    assert auto["fswitch_planned_offset_hz"] == 600e3


def _lmc_sky_and_fold(tmp_path, offset_hz, name):
    planned = fc.plan_point(260, *LMC, WHEN, OBS, offset_hz=offset_hz)
    a = planned["attrs"]
    shift = a["fswitch_lsrk_shift_km_s"]
    # LSRK -> topocentric: the LMC between +210 and +310, the local gas (3x brighter) within +/-12
    sdr = BroadSDR(210 - shift, 310 - shift, 0.02, seed=11, wing=(-12 - shift, 12 - shift, 3.0))
    out = tmp_path / name
    asyncio.run(FC.capture_point(sdr, f0_hz=planned["centre_frequency_hz"], sample_rate=RATE, gain_db=40.2,
                                 duration_s=8.0, output_file=str(out), offset_hz=offset_hz or FC.DEFAULT_OFFSET_HZ,
                                 metadata={"capture_started_at": WHEN + "+00:00"}, home_hz=F0, extra_attrs=a))
    from reduce_engine.ingest import read_capture
    iq, attrs = read_capture(out)
    sw = fswitch_fold.switched_spectrum(iq, attrs, ReduceConfig())
    v_lsrk = (1 - sw.frequency_hz / REST) * C + shift
    return sw, v_lsrk, sdr


def test_end_to_end_the_lmc_survives_only_with_the_small_throw(tmp_path):
    amp = 0.02
    peak = amp * (1 + 1 / (1 + amp)) / 2
    sw, v, sdr = _lmc_sky_and_fold(tmp_path, 300e3, "lmc_300.h5")
    assert {round(f) for f in sdr.tuned} >= {round(sdr.tuned[0])} and sdr.tuned[-1] == F0
    ok = np.isfinite(sw.fractional)
    assert v[ok].min() == pytest.approx(260 - 147.7, abs=3) and v[ok].max() == pytest.approx(260 + 147.7, abs=3)
    core = ok & (v > 230) & (v < 290)
    assert np.mean(sw.fractional[core]) == pytest.approx(peak, abs=0.004)         # the Cloud's height is kept
    ghost = ok & (v > 118) & (v < 136)                                            # local gas ghost: -1/2 x 0.06
    assert np.mean(sw.fractional[ghost]) < -0.015
    assert sw.info["mask_windows_topo_km_s"] and sw.info["local_ghost_in_line_window"] is False

    sw6, v6, _ = _lmc_sky_and_fold(tmp_path, None, "lmc_600.h5")                  # the default throw
    ok6 = np.isfinite(sw6.fractional)
    hit = ok6 & (v6 > 245) & (v6 < 262)
    assert np.mean(sw6.fractional[hit]) < 0                                       # the ghost ate the Cloud
    assert sw6.info["local_ghost_in_line_window"] is True


def test_the_spec_takes_an_offset_only_with_switching_and_keeps_old_specs_identical():
    plain = _validated(frequency_switching=True)
    assert "frequency_switching_offset_hz" not in plain["capture"]
    assert _validated(frequency_switching=True, frequency_switching_offset_hz=None)["capture"] == plain["capture"]
    cap = _validated(frequency_switching=True, frequency_switching_center=260, frequency_switching_offset_hz=300000)["capture"]
    assert cap["frequency_switching_offset_hz"] == 300e3
    with pytest.raises(observation_spec.ObservationSpecError, match="needs frequency_switching"):
        _validated(frequency_switching_offset_hz=300000)
    with pytest.raises(observation_spec.ObservationSpecError, match="frequency_switching_offset_hz"):
        _validated(frequency_switching=True, frequency_switching_offset_hz=50000)


def test_the_orchestrator_passes_the_offset_to_capture_py():
    plan = {"mosaic_csv_path": "m.csv", "requested": {"capture": {"settle_seconds": 2, "seconds": 8, "frequency_switching": True},
                                                      "grid": {"min_altitude_deg": 10}},
            "main": {"center_frequency_hz": F0, "sample_rate": RATE, "gain_db": 40.2}, "rfi_ref": {"enabled": False}}
    assert "--fswitch-offset-hz" not in orch._capture_args(plan, "rt")
    plan["requested"]["capture"]["frequency_switching_offset_hz"] = 300e3
    args = orch._capture_args(plan, "rt")
    assert float(args[args.index("--fswitch-offset-hz") + 1]) == 300e3


def test_capture_py_wires_the_offset():
    import capture
    src = open(capture.__file__).read()
    assert "'--fswitch-offset-hz'" in src and "fswitch_offset_hz=args.fswitch_offset_hz" in src
    assert "offset_hz=self.fswitch_offset_hz" in src and '"offset_hz": self.fswitch_offset_hz' in src


def test_the_observe_page_sends_the_offset_only_when_chosen(tmp_path):
    from tests.test_web_frontend import OBSERVE_ROUTES, run_page
    driver = r"""
await until(() => $("hdr-status").textContent.indexOf("PLANNED") >= 0, 4000);
out.disabled_at_start = $("f-fswitch-offset").disabled;
$("f-fswitch").checked = true; $("f-fswitch").dispatchEvent(new Event("change"));
out.enabled_when_ticked = !$("f-fswitch-offset").disabled;
const plan = async (n) => { $("observe-form").requestSubmit(); await until(() => window.__count("POST /api/observe/plan") === n, 4000); await sleep(300); };
await plan(1);
$("f-fswitch-center").value = "260"; $("f-fswitch-offset").value = "300000"; await plan(2);
out.caps = window.__calls.filter((c) => c.key === "POST /api/observe/plan").map((c) => c.body.capture);
"""
    out = run_page(tmp_path, "observe", OBSERVE_ROUTES, driver, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["disabled_at_start"] is True and out["enabled_when_ticked"] is True
    assert "frequency_switching_offset_hz" not in out["caps"][0]
    assert out["caps"][1]["frequency_switching_offset_hz"] == 300000
    assert out["caps"][1]["frequency_switching_center"] == 260
