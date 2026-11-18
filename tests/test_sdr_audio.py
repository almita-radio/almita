"""AUDIO (operator 2026-11-17): the idle reader (sdr_audio.py), its web routes, the header button and the browser's
analysis (console/sdr_audio.js) - the window must follow the hydrogen, and more hydrogen must mean more sound."""
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

import sdr_audio as sa
from tests.test_orchestrator_web import running_server

ROOT = Path(__file__).resolve().parent.parent
CENTER, FS = 1420405752.0, 2.4e6


def test_reduce_spectrum_keeps_the_axis_blanks_dc_and_flags_edges():
    n = 8192
    spec = np.ones(n)
    spec[n // 2 + 1000] = 50.0                                   # a line at +1000 input bins
    out = sa.reduce_spectrum(spec, CENTER, FS)
    f = np.array(out["frequency_hz"])
    assert len(f) == 2048 and len(out["power_db"]) == 2048 and len(out["inner"]) == 2048
    k = int(np.argmax([(-1e9 if v is None else v) for v in out["power_db"]]))
    assert abs(f[k] - (CENTER + 1000 * FS / n)) < 2 * FS / 2048          # the line lands where it is
    assert all(out["power_db"][i] is None for i in np.flatnonzero(np.abs(f - CENTER) < 3000))
    assert not out["inner"][0] and not out["inner"][-1] and out["inner"][1024]
    with pytest.raises(ValueError):
        sa.reduce_spectrum(np.ones(1000), CENTER, FS)


def _run(monkeypatch, tmp_path, busy, measure=None):
    import transit_logger
    sa.touch_heartbeat(tmp_path)
    calls = []

    def in_use():
        calls.append("busy?")
        if len(calls) > 1:                                       # second loop: no page listens any more -> it stops
            (tmp_path / sa.HEARTBEAT_NAME).unlink()
        return busy
    monkeypatch.setattr(sa, "sdr_in_use", in_use)
    if measure:
        monkeypatch.setattr(transit_logger, "measure", measure)
    assert sa.main(["--every", "0.01", "--runtime-dir", str(tmp_path)]) == 0
    return json.loads((tmp_path / sa.LATEST_NAME).read_text())


def test_another_sdr_user_means_no_sample(monkeypatch, tmp_path):
    def never(*a, **k):
        raise AssertionError("the SDR was touched while busy")
    doc = _run(monkeypatch, tmp_path, "capture.py", never)
    assert doc["status"] == "sdr_busy" and doc["busy"] == "capture.py" and "spectrum" not in doc


def test_a_free_sdr_gives_a_spectrum(monkeypatch, tmp_path):
    async def fake(host, port, gain, seconds):
        return {"gain_db": gain, "center_frequency_hz": CENTER, "sample_rate_hz": FS, "clipping_fraction": 0.0,
                "_spectrum": np.ones(8192, np.float32)}
    doc = _run(monkeypatch, tmp_path, None, fake)
    assert doc["status"] == "ok" and doc["source"] == "idle" and len(doc["spectrum"]["frequency_hz"]) == 2048


def test_without_a_listening_page_it_stops_at_once(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, "sdr_in_use", lambda: (_ for _ in ()).throw(AssertionError("should not even look")))
    assert sa.main(["--every", "0.01", "--runtime-dir", str(tmp_path)]) == 0
    assert not (tmp_path / sa.LATEST_NAME).exists()


def test_start_writes_the_heartbeat_and_goes_through_the_user_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, "RUNTIME_DIR", tmp_path)
    calls = []

    def fake(*args, timeout=60.0):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "active\n" if args[0] == "is-active" else "", "")
    monkeypatch.setattr(sa, "_systemctl", fake)
    assert sa.service_action("start")["active"] is True
    assert calls[0] == ("start", sa.UNIT) and sa.heartbeat_age_s(tmp_path) < 5
    with pytest.raises(ValueError):
        sa.service_action("restart")
    unit = (ROOT / "systemd" / "notebook" / "almita-sdr-audio.service").read_text()
    assert "KillSignal=SIGINT" in unit and "sdr_audio.py" in unit
    assert "almita-sdr-audio" in (ROOT / "scripts" / "install_notebook.sh").read_text().split("ON_DEMAND=", 1)[1].split("\n")[0]


def test_routes_need_confirm_and_state_is_the_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(sa, "_systemctl", lambda *a, **k: subprocess.CompletedProcess(a, 5, "", "Unit not found."))
    with running_server() as base:
        for body, code in ((b"{}", 400), (b'{"confirm": true}', 409)):
            req = urllib.request.Request(base + "/api/audio/start", data=body, method="POST", headers={"Content-Type": "application/json"})
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=10)
            assert e.value.code == code
        (tmp_path / sa.HEARTBEAT_NAME).unlink(missing_ok=True)
        with urllib.request.urlopen(base + "/api/audio/state", timeout=10) as r:
            assert json.loads(r.read())["data"]["unit"] == sa.UNIT
        assert not (tmp_path / sa.HEARTBEAT_NAME).exists()                # a plain look is not a heartbeat
        with urllib.request.urlopen(base + "/api/audio/state?listening=1", timeout=10) as r:
            assert r.status == 200
        assert sa.heartbeat_age_s(tmp_path) < 5
        with urllib.request.urlopen(base + "/sdr_audio.js", timeout=10) as r:
            assert b"AlmitaAudio" in r.read()


# ---------------------------------------------------------------- the browser's analysis, in node
JS = r"""
global.window = {}; global.location = { search: "" };
require(process.argv[2]);
const A = window.AlmitaAudio, C = 299792.458, REST = 1420405751.77, F0 = 1420405752, FS = 2.4e6, N = 2048;
const f = Array.from({ length: N }, (_, i) => F0 + (i - N / 2 + 0.5) * FS / N);
const bandpass = (x) => 50 - 6 * ((x - F0) / 1.2e6) ** 2;                      // RTL-like roll-off, dB
let seed = 1; const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
function spectrum(amp, vKmS, sigmaKmS) {
  const fc = REST * (1 - vKmS / C), sg = sigmaKmS / C * REST;
  return { frequency_hz: f, inner: f.map((_, i) => i > 100 && i < N - 100),
           power_db: f.map((x) => (Math.abs(x - F0) < 3000 ? null :
             bandpass(x) + 10 * Math.log10(1 + amp * Math.exp(-0.5 * ((x - fc) / sg) ** 2) + 0.004 * (rnd() - 0.5)))) };
}
const out = {};
const st = {};
out.first = A.analyse(spectrum(0.02, 0, 15), st, F0);                          // the first spectrum IS the floor
for (let k = 0; k < 3; k++) A.analyse(spectrum(0.02, 0, 15), st, F0);
out.weak = A.analyse(spectrum(0.06, -40, 15), st, F0);
out.strong = A.analyse(spectrum(0.20, -40, 15), st, F0);
out.moved = A.analyse(spectrum(0.20, +60, 25), st, F0);
console.log(JSON.stringify(out));
"""


def test_the_window_follows_the_hydrogen_and_more_hydrogen_is_louder(tmp_path):
    drv = tmp_path / "drv.js"
    drv.write_text(JS)
    r = subprocess.run(["node", str(drv), str(ROOT / "console" / "sdr_audio.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["first"]["found"] is False                                    # nothing above the floor yet: only the quiet bed
    w, s, m = o["weak"], o["strong"], o["moved"]
    assert w["found"] and s["found"] and m["found"]
    assert abs(s["velocityKmS"] - (-40)) < 8 and abs(m["velocityKmS"] - 60) < 10          # it follows the line
    assert s["meanExcess"] > 2 * w["meanExcess"] and s["peak"] > w["peak"]                 # more hydrogen, more sound
    assert 200e3 <= s["widthHz"] <= 1e6 and s["lowHz"] < 1420405751.77 * (1 + 40 / 299792.458) < s["highHz"]
    assert len(s["bands"]) == 16 and max(s["bands"]) > 0


JS_NOISE = r"""
global.window = {}; global.location = { search: "" };
require(process.argv[2]);
const A = window.AlmitaAudio, REST = 1420405751.77, F0 = 1420405752, FS = 2.4e6, N = 2048;
const f = Array.from({ length: N }, (_, i) => F0 + (i - N / 2 + 0.5) * FS / N);
let seed = 7; const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
const gauss = () => Math.sqrt(-2 * Math.log(rnd() || 1e-12)) * Math.cos(2 * Math.PI * rnd());
// 0.5 s of IQ, 8192-point frames folded to 2048 bins: ~4 % noise per bin (the idle reader's real spectra)
const spectrum = (amp) => ({ frequency_hz: f, inner: f.map((_, i) => i > 100 && i < N - 100),
  power_db: f.map((x) => (Math.abs(x - F0) < 3000 ? null : 50 - 6 * ((x - F0) / 1.2e6) ** 2 +
    10 * Math.log10((1 + amp * Math.exp(-0.5 * ((x - REST) / 120e3) ** 2)) * Math.max(0.5, 1 + 0.041 * gauss())))) });
const st = {}; let noiseFound = 0, lineFound = 0, lineVel = [];
for (let k = 0; k < 100; k++) if (A.analyse(spectrum(0), st, F0).found) noiseFound++;
for (let k = 0; k < 20; k++) { const a = A.analyse(spectrum(0.15), st, F0); if (a.found) { lineFound++; lineVel.push(a.velocityKmS); } }
console.log(JSON.stringify({ noiseFound, lineFound, lineVel }));
"""


def test_pure_noise_stays_quiet_and_a_real_line_still_sounds(tmp_path):
    """Operator 2026-11-18: SDR alone (no LNA, no antenna) swung in volume - the floor is a running minimum, which noise
    always undercuts, so noise read as a 3-4 % "excess". The band's outer parts now set that bias aside."""
    drv = tmp_path / "noise.js"
    drv.write_text(JS_NOISE)
    r = subprocess.run(["node", str(drv), str(ROOT / "console" / "sdr_audio.js")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["noiseFound"] <= 2                                            # 100 noise-only spectra: (almost) silent
    assert o["lineFound"] >= 18                                            # a 15 % line over the same noise: heard
    assert all(abs(v) < 15 for v in o["lineVel"])


def test_header_has_audio_between_mount_and_day_night():
    js = (ROOT / "console" / "common.js").read_text()
    i, j, k = js.index("U.mountDeviceToggle(nav);\n    U.audioToggle(nav);"), js.index("U.audioToggle(nav);\n    U.themeToggle(nav);"), 0
    assert i < j and '"AUDIO ON"' in js and '"AUDIO OFF"' in js and 'label: "DAY"' in js and 'label: "NIGHT"' in js
    assert "DÍA\"" not in js and "NOCHE\"" not in js
