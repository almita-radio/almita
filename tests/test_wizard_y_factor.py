"""CALIBRATE receiver Y-factor (operator 2026-11-10: "el Y-factor tenemos que tenerlo calculado desde siempre"):
Y = P(50 ohm load) / P(cold sky HI BAJO), with the load temperature from its DS18 ('load') or declared, and only
from ONE rtl_tcp process. Plus the DS18 reader that now reaches the sensors on the capture Pi."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import almita_web_ops as ops
import calibrate_reference_wizard as wiz
import instrument_host
import temperature_sensors
from calibration_engine import y_factor as yf
from tests.test_calibrate_wizard_lifecycle import _state, session  # noqa: F401 - pytest fixture
from tests.test_web_frontend import run_page

PROC = {"main_pid": 704, "active_enter": "Wed 2026-11-05 22:00:00 UTC"}
TUNE = {"requested": {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain_db": 40.2}}


def noise(sigma, n=4096 * 40, seed=0):
    """Gaussian complex noise as the RTL's interleaved uint8 - power proportional to sigma^2."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0, sigma, 2 * n) + 127.5
    return np.clip(np.round(x), 0, 255).astype(np.uint8)


def compute(load_sigma=20.0, cold_sigma=10.0, **kw):
    args = dict(connection_point="LNA_INPUT", load_temperature_c=16.85, load_temperature_source="DS18 load sensor",
                load_tuning=TUNE, cold_tuning=TUNE, load_process=PROC, cold_process=dict(PROC))
    args.update(kw)
    return yf.compute_y_factor([noise(load_sigma, seed=1), noise(load_sigma, seed=2)],
                               [noise(cold_sigma, seed=3), noise(cold_sigma, seed=4)], **args)


def test_y_is_the_load_to_cold_sky_power_ratio_and_gives_tsys_and_the_antenna_temperature():
    r = compute()                                         # sigma ratio 2 -> power ratio 4 -> 6.02 dB
    assert r["verdict"] == "VALID" and abs(r["y_db"] - 6.02) < 0.1
    t_load = 16.85 + 273.15
    assert r["t_load_k"] == pytest.approx(t_load)
    assert r["tsys_sky_k"] == pytest.approx((t_load + 59.0) / r["y_linear"], abs=0.2)
    assert r["t_ant_at_lna_input_k"] == pytest.approx(r["tsys_sky_k"] - 59.0, abs=0.2)
    assert "NOT measured" in r["t_rx_assumption"] and [s["t_rx_k"] for s in r["spread_for_t_rx"]] == [35.0, 80.0]
    assert r["calibration_level"] == "OPERATIONAL_RELATIVE" and "no kelvin conversion" in r["not_applied"]
    assert all(c["status"] == "OK" for c in r["checks"])


def test_an_rtl_tcp_restart_between_load_and_sky_invalidates_y():
    r = compute(cold_process={"main_pid": 999, "active_enter": "Wed 2026-11-05 22:10:00 UTC"})
    assert r["verdict"] == "INVALID" and "same_rtl_tcp_process" in r["reason"] and "1.7 dB" in r["reason"]


def test_without_process_identity_y_is_kept_but_says_a_restart_cannot_be_ruled_out():
    r = compute(load_process=None, cold_process=None)
    assert r["verdict"] == "VALID" and "cannot be ruled out" in r["reason"]


@pytest.mark.parametrize("kw, why", [
    (dict(cold_tuning={"requested": {**TUNE["requested"], "gain_db": 30.0}}), "same_tuning"),
    (dict(cold_clipped=True), "no_clipping"),
    (dict(load_sigma=10.0, cold_sigma=12.0), "load_hotter_than_cold_sky"),
])
def test_other_failed_checks_make_y_invalid(kw, why):
    r = compute(**kw)
    assert r["verdict"] == "INVALID" and why in r["reason"]


def test_without_a_load_temperature_or_at_another_plane_only_y_is_given():
    r = compute(load_temperature_c=None, load_temperature_source=None)
    assert r["verdict"] == "Y_ONLY" and "tsys_sky_k" not in r and r["y_db"] > 5
    r = compute(connection_point="SDR_INPUT")
    assert r["verdict"] == "Y_ONLY" and "different reference planes" in r["reason"]


def test_a_sky_pointing_brighter_than_the_ambient_load_is_flagged():
    """2026-11-05 real night: HI_ALTO read +4.6 dB above the load (interference)."""
    r = compute(other_sky={"HI_ALTO": [noise(40.0, seed=9)]})
    assert r["sky_minus_load_db"]["HI_ALTO"] > 5 and "ABOVE the ambient 50 ohm load" in r["warnings"][0]


def test_the_median_ignores_a_strong_narrow_line():
    clean = compute()["y_db"]
    n = 4096 * 40
    t = np.arange(n)
    tone = 60 * np.exp(2j * np.pi * 0.1 * t)
    base = noise(10.0, seed=3).astype(float) - 127.5
    z = base[0::2] + 1j * base[1::2] + tone
    cold = np.empty(2 * n)
    cold[0::2], cold[1::2] = z.real + 127.5, z.imag + 127.5
    cold = np.clip(np.round(cold), 0, 255).astype(np.uint8)
    r = yf.compute_y_factor([noise(20.0, seed=1)], [cold], connection_point="LNA_INPUT", load_temperature_c=None,
                            load_temperature_source=None)
    assert abs(r["y_db"] - clean) < 0.3


def test_load_temperature_record_never_invents_a_value():
    ok = lambda c: {"valid": True, "temperature_c": c}
    r = yf.load_temperature_record(ok(17.0), ok(17.5), "28-x")
    assert r["temperature_c"] == 17.25 and r["drift_c"] == 0.5 and r["source"] == "DS18 load sensor"
    r = yf.load_temperature_record({"valid": False, "error": "No such file"}, None, "28-x")
    assert r["temperature_c"] is None and "No such file" in r["reason"]
    assert "no 'load' sensor" in yf.load_temperature_record(None, None, None)["reason"]


# ------------------------------------------------------------------ the wizard flow

def _to_hi_bajo(d, monkeypatch):
    """50 ohm (simulated, HEALTHY) -> HI BAJO captured from a simulated, quieter sky."""
    assert wiz.cmd_capture_50r(SimpleNamespace(session_dir=str(d), simulate="HEALTHY")) in (0, 2)
    st = _state(d)
    assert st["load_temperature"]["temperature_c"] is None                       # simulated: no sensor read
    from tests.test_hi_plan_preview import PLAN
    st.update(step="READY_HI_BAJO", hi_plan=PLAN, hi_plan_approved_utc="2026-11-10T01:00:00+00:00")
    (d / "wizard_state.json").write_text(json.dumps(st))

    async def quiet_sky(ra, dec, gain, n, secs, rate, cf, cap_dir, scenario):
        paths = []
        for i in range(n):
            p = Path(cap_dir) / f"capture_{i:03d}.h5"
            import h5py
            with h5py.File(p, "w") as h:
                h.create_dataset("iq_data", data=noise(4.0, n=2 ** 17, seed=10 + i))
                h.attrs["capture_status"] = "success"
            paths.append(str(p))
        return {"capture_paths": paths, "simulated": True}
    monkeypatch.setattr(wiz, "_simulate_capture_n_at", quiet_sky)
    monkeypatch.setattr("calibration_engine.spectral_contrast.evaluate_hi_spectral_captures", lambda paths: {"stub": True})
    monkeypatch.setattr(wiz, "evaluate_hi_spectral_captures", lambda paths: {"stub": True})
    wiz.cmd_capture_hi(SimpleNamespace(session_dir=str(d), label="HI_BAJO", simulate="HEALTHY"))
    return _state(d)


def test_hi_bajo_computes_y_and_a_declared_load_temperature_gives_kelvin(session, monkeypatch, capsys):
    d = session()
    st = _to_hi_bajo(d, monkeypatch)
    assert st["step"] == "RESULT_HI_BAJO"
    y = st["y_factor"]
    assert y["verdict"] == "Y_ONLY" and y["y_db"] > 0 and "no load temperature" in y["reason"]
    wiz.cmd_set_load_temperature(SimpleNamespace(session_dir=str(d), celsius=18.0))
    st = _state(d)
    assert st["load_temperature"]["source"] == "declared by operator" and st["load_temperature"]["temperature_c"] == 18.0
    assert st["y_factor"]["verdict"] == "VALID" and st["y_factor"]["load_temperature_source"] == "declared by operator"
    with pytest.raises(SystemExit, match="plausible"):
        wiz.cmd_set_load_temperature(SimpleNamespace(session_dir=str(d), celsius=300.0))


def test_a_real_50r_step_records_the_load_ds18_and_the_rtl_tcp_process(session, monkeypatch):
    d = session()
    readings = iter([{"valid": True, "temperature_c": 17.0, "sensor_id": "28-load"},
                     {"valid": True, "temperature_c": 17.4, "sensor_id": "28-load"}])
    monkeypatch.setattr(wiz, "_read_load_temperature", lambda: next(readings))
    monkeypatch.setattr(wiz, "_load_sensor_id", lambda: "28-load")
    monkeypatch.setattr(wiz, "_rtl_tcp_process", lambda: dict(PROC))
    monkeypatch.setattr(wiz, "_main_device_check_or_raise", lambda: None)
    monkeypatch.setattr(wiz, "bias_t_load_refusal", lambda *a, **k: None)
    from calibration_engine import acquisition

    class Backend(acquisition.SimulatedCalibrationAcquisitionBackend):
        tuning = TUNE

        def __init__(self, *a, **k):
            super().__init__(simulation_config_factory=lambda i: __import__("calibration_engine.simulation", fromlist=["x"]).flat_noisy_config(seed=i))

        async def connect(self):
            return None

        async def tune(self, *a):
            return None

        async def close(self):
            return None

        async def capture(self, **kw):
            kw.pop("metadata", None)
            return await super().capture(**kw, metadata={}, index=0)
    monkeypatch.setattr(acquisition, "RealCalibrationAcquisitionBackend", Backend)
    wiz.cmd_capture_50r(SimpleNamespace(session_dir=str(d), simulate=None))
    st = _state(d)
    assert st["fifty_ohm_result"]["rtl_tcp_process"] == PROC
    assert st["load_temperature"] == {"temperature_c": pytest.approx(17.2), "source": "DS18 load sensor",
                                      "sensor_id": "28-load", "drift_c": pytest.approx(0.4)}


def test_old_sessions_get_their_y_recomputed_from_disk(session, monkeypatch, capsys):
    d = session()
    _to_hi_bajo(d, monkeypatch)
    st = _state(d)
    st.pop("y_factor")
    (d / "wizard_state.json").write_text(json.dumps(st))
    wiz.main(["y-factor", "--session-dir", str(d)])
    assert _state(d)["y_factor"]["y_db"] > 0


def test_the_web_routes_the_load_temperature_as_a_state_only_action(tmp_path, monkeypatch):
    cal = tmp_path / "data" / "calibration"
    (cal / "WIZ-1").mkdir(parents=True)
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    monkeypatch.setitem(ops.SERVE_ROOTS, "calibration", cal)
    argv, meta = ops.build_command("calibrate_wizard_state", {"action": "set_load_temperature", "session_dir": "data/calibration/WIZ-1",
                                                              "celsius": 17.5}, "J")
    assert argv[1:] == ["calibrate_reference_wizard.py", "set-load-temperature", "--session-dir", "data/calibration/WIZ-1", "--celsius", "17.5"]
    with pytest.raises(ValueError, match="celsius"):
        ops.build_command("calibrate_wizard_state", {"action": "set_load_temperature", "session_dir": "data/calibration/WIZ-1"}, "J")


# ------------------------------------------------------------------ DS18 on the capture Pi

W1 = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"


def test_the_ds18_reader_reads_the_capture_pis_sensors_through_its_agent(monkeypatch):
    asked = []
    monkeypatch.setattr(instrument_host, "remote_host", lambda: "192.168.50.254")
    monkeypatch.setattr(instrument_host, "read_bytes", lambda path, timeout=10.0: asked.append(str(path)) or W1.encode())
    r = temperature_sensors.DS18B20Reader({"load": "28-0000006f80a5"}).read("load")
    assert r["valid"] and r["temperature_c"] == 23.125
    assert asked == ["/sys/bus/w1/devices/28-0000006f80a5/w1_slave"]


def test_a_refused_or_absent_remote_sensor_is_missing_never_a_crash(monkeypatch):
    monkeypatch.setattr(instrument_host, "remote_host", lambda: "192.168.50.254")

    def refused(path, timeout=10.0):
        raise subprocess.SubprocessError("instrument agent refused cat")
    monkeypatch.setattr(instrument_host, "read_bytes", refused)
    r = temperature_sensors.DS18B20Reader({"load": "28-x"}, warning=lambda m: None).read("load")
    assert not r["valid"] and r["error_kind"] == "missing" and r["attempts"] == 1


def test_a_test_tree_is_still_read_locally_even_with_a_remote_instrument(tmp_path, monkeypatch):
    monkeypatch.setattr(instrument_host, "remote_host", lambda: "192.168.50.254")
    monkeypatch.setattr(instrument_host, "read_bytes", lambda *a, **k: pytest.fail("must not go remote"))
    (tmp_path / "28-a").mkdir()
    (tmp_path / "28-a" / "w1_slave").write_text(W1)
    assert temperature_sensors.DS18B20Reader({"sdr": "28-a"}, tmp_path).read("sdr")["temperature_c"] == 23.125


# ------------------------------------------------------------------ CALIBRATE page

def test_the_calibrate_page_shows_the_y_factor_and_sends_a_declared_load_temperature(tmp_path):
    y = {"verdict": "VALID", "y_db": 1.53, "y_linear": 1.4213, "reason": "Y = 1.421 (1.53 dB) with T_load 290.1 K",
         "tsys_sky_k": 245.7, "t_ant_at_lna_input_k": 186.7, "t_rx_assumption": "T_rx 59 K is the LNA datasheet figure, NOT measured",
         "spread_for_t_rx": [{"t_rx_k": 35, "tsys_sky_k": 228.8}, {"t_rx_k": 80, "tsys_sky_k": 260.4}],
         "y_db_per_channel_p10_p50_p90": [1.07, 1.56, 1.83],
         "checks": [{"name": "same_rtl_tcp_process", "status": "OK", "detail": "MainPID 704 for both"}],
         "warnings": ["HI_ALTO read +4.6 dB ABOVE the ambient 50 ohm load"]}
    state = {"session_id": "WIZARD-X", "step": "RESULT_HI_BAJO", "config": {"n_captures": 5, "stabilize_seconds": 20},
             "fifty_ohm": {"status": "DONE"}, "load_temperature": {"temperature_c": 17.0, "source": "DS18 load sensor", "drift_c": 0.1},
             "hi_references": {"HI_ALTO": {}, "HI_BAJO": {"quality_result": {"verdict": "PASS", "verdict_reasons": [], "quality": {"verdict": "GOOD"}},
                                                         "spectral_result": {}}},
             "y_factor": y, "bias_t_facts": None}
    routes = """
window.__routes["GET /api/ops/jobs"] = { body: { ok: true, data: [{ job_id: "CW-1", stage: "calibrate_wizard_state" }] } };
window.__routes["GET /api/ops/job/CW-1"] = { body: { ok: true, data: { job_id: "CW-1", state: "DONE", output_dir: "data/calibration/WIZARD-X", facts: %s } } };
window.__routes["POST /api/ops/start/calibrate_wizard"] = (b) => ({ body: { ok: true, data: { job_id: "CW-2" } } });
window.__routes["GET /api/ops/job/CW-2"] = { body: { ok: true, data: { job_id: "CW-2", state: "DONE", exit_code: 0, facts: %s } } };
""" % (json.dumps(state), json.dumps(state))
    driver = r"""
await until(() => !$("wz-yfactor-panel").hidden && $("wz-yfactor-banner").textContent.indexOf("dB") >= 0, 6000);
out.banner = $("wz-yfactor-banner").textContent; out.load = $("wz-yfactor-load").textContent;
out.detail = $("wz-yfactor-detail").textContent; out.checks = $("wz-yfactor-checks").textContent;
$("wz-load-temp").value = "18.5";
$("wz-set-load-temp").click();
await until(() => window.__calls.some((c) => c.key === "POST /api/ops/start/calibrate_wizard"), 6000);
out.posted = window.__calls.filter((c) => c.key === "POST /api/ops/start/calibrate_wizard").map((c) => c.body.params);
"""
    out = run_page(tmp_path, "calibrate", routes, driver, budget=12000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["banner"].startswith("Y = 1.53 dB") and "VALID" in out["banner"]
    assert "17.0 °C (DS18 load sensor" in out["load"]
    assert "Tsys on the cold sky ≈ 246 K" in out["detail"] and "5.5 dB" in out["detail"]
    assert "same_rtl_tcp_process" in out["checks"] and "ABOVE the ambient" in out["checks"]
    assert out["posted"] == [{"action": "set_load_temperature", "session_dir": "data/calibration/WIZARD-X", "celsius": 18.5}]
