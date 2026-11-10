"""Frequency switching (fswitch.py, operator 2026-11-09): the receiver's band shape, fixed in baseband, cancels in
(A-B)/B; the 21 cm line, fixed in RF, survives and folds back once at its real frequency; a slow drift cancels in
ABBA order; the SDR is refused while another ALMITA process uses it."""
import numpy as np
import pytest

import fswitch as F

RATE, D = 2.4e6, 600e3
BB = F.baseband_hz(F.NFFT, RATE)
SHAPE = 1 + 0.05 * np.sin(2 * np.pi * BB / 700e3) + 0.03 * np.cos(2 * np.pi * BB / 230e3)   # +/-6 % in baseband
V_LINE_HZ = 30e3                                                                             # line at f0 + 30 kHz


def _line(rf_hz, amp=0.04):
    return amp * np.exp(-0.5 * ((rf_hz - F.HI_REST_HZ - V_LINE_HZ) / 60e3) ** 2)


def _run(cycles=20, noise=0.02, amp=0.04, drift=0.0, seed=1):
    rng = np.random.default_rng(seed)
    specs, los, t = [], [], 0
    for c in range(cycles):
        for lo in ((F.HI_REST_HZ - D, F.HI_REST_HZ + D) if c % 2 == 0 else (F.HI_REST_HZ + D, F.HI_REST_HZ - D)):
            gain = 1 + drift * t
            specs.append(gain * SHAPE * (1 + _line(lo + BB, amp)) * (1 + rng.normal(0, noise, BB.size)))
            los.append(lo)
            t += 1
    return F.analyse(specs, los, RATE, D)


def test_band_shape_cancels_and_the_line_folds_back_at_its_frequency():
    r = _run()
    assert r["ripple_band_shape_pct"] > 3.0                  # the receiver shape is there...
    assert r["ripple_switched_pct"] < 0.3                    # ...and gone in (A-B)/B
    v, t = r["v_kms"], r["folded_baselined_pct"]
    k = np.nanargmax(np.where(np.abs(v) < 90, t, np.nan))
    assert t[k] == pytest.approx(4.0, abs=0.3)               # the 4 % line, once, at full height
    assert v[k] == pytest.approx(-V_LINE_HZ / F.HI_REST_HZ * F.C_KMS, abs=3.0)
    assert r["line_excess_pct"] > 1.0 and r["off_line_rms_pct"] < 0.1


def test_no_line_gives_a_flat_folded_spectrum_at_the_noise_level():
    r = _run(amp=0.0)
    assert abs(r["line_excess_pct"]) < 0.05 and r["off_line_rms_pct"] < 0.1


def test_noise_falls_as_one_over_root_time():
    short, long_ = _run(cycles=6, amp=0.0, noise=0.05, seed=3), _run(cycles=24, amp=0.0, noise=0.05, seed=3)
    assert long_["off_line_rms_pct"] == pytest.approx(short["off_line_rms_pct"] / 2, rel=0.35)


def test_abba_order_cancels_a_slow_gain_drift():
    r = _run(amp=0.0, drift=2e-3)                          # +8 % gain over the run, linear
    assert abs(r["line_excess_pct"]) < 0.1 and r["ripple_switched_pct"] < 0.5


def test_fold_puts_the_two_images_back_on_one_rf_axis():
    s = np.zeros_like(BB)
    s[np.argmin(np.abs(BB - D))] = 1.0                      # +image (line seen by LO A at +delta)
    s[np.argmin(np.abs(BB + D))] = -1.0                     # -image (seen by LO B at -delta)
    rf = np.array([F.HI_REST_HZ])
    assert F.fold(s, BB, F.HI_REST_HZ - D, F.HI_REST_HZ + D, rf)[0] == pytest.approx(1.0, abs=0.05)


def test_the_sdr_is_refused_while_another_almita_process_uses_it(monkeypatch, capsys):
    import transit_logger
    monkeypatch.setattr(transit_logger, "sdr_busy", lambda **_: "capture.py")
    assert F.main(["--cycles", "1"]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_repeated_los_at_cycle_boundaries_become_one_segment():
    assert F.lo_segments(1) == [(0, 1), (1, 1)]
    assert F.lo_segments(4) == [(0, 1), (1, 2), (0, 2), (1, 2), (0, 1)]       # A BB AA BB A: 5 retunes, not 8
    segs = F.lo_segments(20)
    assert sum(n for _, n in segs) == 40 and len(segs) == 21
    assert sum(n for lo, n in segs if lo == 0) == sum(n for lo, n in segs if lo == 1) == 20


def _raw(n, rms=8.0, head_n=0, head_gain=1.0, seed=4):
    rng = np.random.default_rng(seed)
    z = rng.normal(0, rms, 2 * n)
    z[: 2 * head_n] *= head_gain
    return np.clip(np.round(z + 127.5), 0, 255).astype(np.uint8)


def test_old_lo_samples_at_a_segment_head_are_found_and_cut():
    rate = 2.4e6
    assert F.head_trim(_raw(int(rate)), rate) == 0                              # a clean head stays whole
    cut = F.head_trim(_raw(int(rate), head_n=int(0.05 * rate), head_gain=np.sqrt(1.11)), rate)
    assert 0.05 * rate <= cut <= 0.07 * rate                                    # 50 ms 11 % off -> cut to the block


def test_a_merged_segment_gives_one_spectrum_per_dwell():
    raw = _raw(4 * F.NFFT * 10)
    parts = F.segment_spectra(raw, 2, drop_samples=F.NFFT)
    assert len(parts) == 2 and all(p.shape == (F.NFFT,) for p in parts)


def test_capture_iq_returns_exactly_the_requested_samples_in_memory_and_fails_on_disconnect():
    """SDRCapture.capture_iq (2026-11-09, for fswitch): same consumer and guards as capture(), no HDF5."""
    import asyncio
    from sdr_capture import SDRCapture, SDRDisconnected
    from tests.test_sdr_network_failfast import FakeNetworkSocket
    counter = iter(range(10 ** 6))
    cap = SDRCapture(mode="network")
    cap.socket = FakeNetworkSocket(lambda size: bytes((next(counter) % 256,)) * min(size, 7))
    raw = asyncio.run(cap.capture_iq(0.5, sample_rate=100))
    assert raw.dtype == np.uint8 and raw.size == 100                 # 50 samples x I/Q, not one byte more
    dead = SDRCapture(mode="network")
    dead.socket = FakeNetworkSocket(lambda _size: b"")
    with pytest.raises(SDRDisconnected):
        asyncio.run(dead.capture_iq(5.0, sample_rate=100))
