"""rfi_mask: comb, narrow lines and pulses are flagged; broad (HI-like) structure and the raw capture are not touched."""
import hashlib

import h5py
import numpy as np

import rfi_mask as rm

CENTER, RATE = 1_420_405_752.0, 2_400_000.0


def _write(path, z):
    iq = np.empty(2 * len(z), np.uint8)
    iq[0::2] = np.clip(np.round(127.5 + z.real), 0, 255)
    iq[1::2] = np.clip(np.round(127.5 + z.imag), 0, 255)
    with h5py.File(path, "w") as f:
        f.create_dataset("iq_data", data=iq)
        f.attrs.update({"capture_status": "success", "center_frequency_hz": CENTER, "sample_rate_hz": RATE,
                        "applied_gain_db": 40.2})
    return path


def _noise(seconds=1.0, sigma=15.0, seed=0):
    rng = np.random.default_rng(seed)
    n = int(RATE * seconds)
    return rng.normal(0, sigma, n) + 1j * rng.normal(0, sigma, n)


def _tones(z, freqs_hz, amp):
    t = np.arange(len(z)) / RATE
    for f in freqs_hz:
        z = z + amp * np.exp(2j * np.pi * (f - CENTER) * t)
    return z


def _real_lines(r):
    return [ln for ln in r["lines"] if not ln.get("dc")]


def test_plain_noise_masks_only_the_dc_spike(tmp_path):
    r = rm.analyze_capture(_write(tmp_path / "n.h5", _noise()))
    assert r["comb_channels"] == 0 and not _real_lines(r) and r["pulse_fraction"] < 0.01
    assert r["line_channels"] == 2 * rm.DC_HALF_WIDTH_BINS + 1 and r["lines"][0]["dc"]


def test_comb_teeth_are_masked_and_nothing_else(tmp_path):
    teeth = rm.comb_teeth_hz(CENTER, RATE)
    r = rm.analyze_capture(_write(tmp_path / "c.h5", _tones(_noise(), teeth, 3.0)))
    assert all(t["masked"] for t in r["teeth"]) and not _real_lines(r)
    assert r["masked_channel_fraction"] < 0.02


def test_a_narrow_line_is_masked_but_a_broad_hi_like_bump_is_not(tmp_path):
    z = _tones(_noise(), [CENTER + 123_456.0], 3.0)
    rng = np.random.default_rng(5)                                   # a 100 kHz-wide +20 % bump at the HI frequency
    bump = np.fft.ifft(np.fft.fft(rng.normal(0, 7, len(z)) + 1j * rng.normal(0, 7, len(z)))
                       * (np.abs(np.fft.fftfreq(len(z), 1 / RATE)) < 50e3))
    r = rm.analyze_capture(_write(tmp_path / "l.h5", z + bump))
    lines = _real_lines(r)
    assert len(lines) == 1 and abs(lines[0]["frequency_hz"] - (CENTER + 123_456.0)) < 1e3
    codes, f = r["arrays"]["codes"], r["arrays"]["freq_hz"]
    dc = np.abs(f - CENTER) <= (rm.DC_HALF_WIDTH_BINS + 0.5) * (f[1] - f[0])
    assert not codes[(np.abs(f - CENTER) < 40e3) & ~dc].any()         # the bump itself: never masked (only the DC spike)
    assert (codes != 0).sum() <= 2 * rm.LINE_MAX_HALF_WIDTH_BINS + 1 + 2 * rm.DC_HALF_WIDTH_BINS + 1


def test_a_burst_is_cut_and_the_clean_mean_drops(tmp_path):
    z = _noise()
    z[200_000:230_000] *= 4.0                                        # 12.5 ms burst, 16x the power
    r = rm.analyze_capture(_write(tmp_path / "p.h5", z))
    assert 0 < r["pulse_frames"] <= 6
    a = r["arrays"]
    assert np.median(a["mean_db_clean"]) < np.median(a["mean_db_raw"]) - 0.5


def test_known_lines_are_masked_from_a_lower_bar():
    freq = CENTER + np.fft.fftshift(np.fft.fftfreq(rm.FFT_SIZE, 1 / RATE))
    th = rm.thresholds(2929)
    i = int(np.argmin(np.abs(freq - (CENTER - 300_000.0))))
    db = np.zeros(rm.FFT_SIZE)
    db[i] = (th["known_db"] + th["line_db"]) / 2                     # under the new-line bar, over the known-line bar
    assert not [ln for ln in rm.find_channel_mask(freq, db, CENTER, RATE)["lines"] if not ln.get("dc")]
    found = rm.find_channel_mask(freq, db, CENTER, RATE, known_lines_hz=[freq[i]])["lines"]
    assert [ln for ln in found if not ln.get("dc")][0]["known"]


def test_thresholds_follow_the_channel_noise():
    long, short = rm.thresholds(2929), rm.thresholds(292)               # 10 s and 1 s of frames
    assert long["line_db"] == rm.LINE_DB_FLOOR and short["line_db"] > long["line_db"]


def test_the_raw_capture_is_never_modified_and_the_sidecar_has_everything(tmp_path):
    path = _write(tmp_path / "s.h5", _tones(_noise(), rm.comb_teeth_hz(CENTER, RATE), 3.0))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    out = rm.write_sidecar(rm.analyze_capture(path), tmp_path / "masks", "s")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with np.load(out["npz"]) as z:
        assert {"freq_hz", "codes", "mean_db_raw", "mean_db_clean", "block_codes"} <= set(z.files)


def test_display_payloads_are_decimated(tmp_path):
    r = rm.analyze_capture(_write(tmp_path / "d.h5", _noise(0.5)))
    d = rm.display_spectrum(r, 1024)
    assert len(d["frequency_hz"]) == len(d["power_db"]) == len(d["code"]) == 1024
    assert len(rm.display_block_rows(r, 256)[0]) == 256
