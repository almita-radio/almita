"""local_rfi_check: the Pi's 357.14 kHz comb and narrow lines over the floor, and the NanoVNA on the USB (2026-10-29)."""
import numpy as np

import local_rfi_check as rc
import observation_preflight as pf

CENTER, RATE = 1_420_405_752.0, 2_400_000.0


def _iq(tone_hz=(), tone_amp=0.0, sigma=20.0, seconds=0.5, seed=0):
    rng = np.random.default_rng(seed)
    n = int(RATE * seconds)
    z = rng.normal(0, sigma, n) + 1j * rng.normal(0, sigma, n)
    t = np.arange(n) / RATE
    for f in tone_hz:
        z += tone_amp * np.exp(2j * np.pi * (f - CENTER) * t)
    iq = np.empty(2 * n, dtype=np.uint8)
    iq[0::2] = np.clip(np.round(127.5 + z.real), 0, 255)
    iq[1::2] = np.clip(np.round(127.5 + z.imag), 0, 255)
    return iq


def test_comb_teeth_are_the_harmonics_measured_on_2026_10_28():
    teeth = rc.comb_teeth_hz(CENTER, RATE)
    assert len(teeth) == 6
    # measured 1419.640 / 1420.711 MHz on the V3, ~3 kHz under the exact harmonics (~2 ppm): inside the search window
    assert abs(teeth[0] - 1_419_640_000) < rc.TOOTH_WINDOW_HZ and abs(teeth[3] - 1_420_711_000) < rc.TOOTH_WINDOW_HZ


def test_plain_noise_is_clean():
    r = rc.analyze(_iq(), RATE, CENTER)
    assert r["verdict"] == "CLEAN" and r["lines_over_threshold"] == 0
    assert abs(r["tooth_max_db"]) < 3.0


def test_the_comb_is_found_and_called_noisy():
    r = rc.analyze(_iq(rc.comb_teeth_hz(CENTER, RATE), tone_amp=4.0), RATE, CENTER)
    assert r["verdict"] == "NOISY"
    assert r["tooth_max_db"] >= rc.COMB_PRESENT_DB
    assert all(t["excess_db"] > 6 for t in r["teeth"])


def test_a_faint_comb_is_faint():
    r = rc.analyze(_iq(rc.comb_teeth_hz(CENTER, RATE)[:1], tone_amp=0.9), RATE, CENTER)
    assert r["verdict"] in ("FAINT", "NOISY") and r["tooth_max_db"] >= rc.COMB_FAINT_DB


def test_spectrum_payload_matches_the_fft():
    r = rc.analyze(_iq(), RATE, CENTER)
    assert len(r["spectrum"]["frequency_hz"]) == len(r["spectrum"]["excess_db"]) == rc.FFT_SIZE


def _usb(root, name, vid, pid, product=None):
    d = root / name
    d.mkdir()
    (d / "idVendor").write_text(vid + "\n")
    (d / "idProduct").write_text(pid + "\n")
    if product:
        (d / "product").write_text(product + "\n")


def test_nanovna_is_seen_by_its_usb_id_and_the_onstep_ch340_is_not(tmp_path):
    _usb(tmp_path, "1-1", "1a86", "7523", "USB Serial")              # OnStep's CH340
    assert not rc.nanovna_present(tmp_path)["present"]
    _usb(tmp_path, "1-2", "0483", "5740", "NanoVNA-H")
    found = rc.nanovna_present(tmp_path)
    assert found["present"] and found["devices"][0]["usb_id"] == "0483:5740"


def test_preflight_warns_when_the_nanovna_is_plugged(monkeypatch):
    monkeypatch.setattr(rc, "nanovna_present", lambda: {"present": True, "devices": [], "detail": "NanoVNA plugged"})
    assert pf._nanovna_check()["status"] == pf.WARNING
    monkeypatch.setattr(rc, "nanovna_present", lambda: {"present": False, "devices": [], "detail": "none"})
    assert pf._nanovna_check()["status"] == pf.PASS
