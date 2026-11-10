"""reduce_estimator_v2: opt-in REDUCE estimate (Hann 50 % overlap + mean of spectral-kurtosis-clean cells), with
reduce_engine (frozen) untouched."""
from types import SimpleNamespace

import numpy as np

import reduce_estimator_v2 as v2
from hi_spectral_metric import robust_psd_from_iq
from reduce_engine.config import ReduceConfig
from reduce_engine.models import MaskFlag

RATE, F0 = 2.4e6, 1.4204e9


def _iq(z: np.ndarray, rms: float = 12.0) -> np.ndarray:
    """complex samples -> interleaved uint8 like the RTL (rms counts per component)."""
    out = np.empty(2 * z.size, np.float64)
    out[0::2], out[1::2] = z.real * rms + 127.5, z.imag * rms + 127.5
    return np.clip(np.round(out), 0, 255).astype(np.uint8)


def _noise(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2)


def _smoothed_rel_noise(psd: np.ndarray, k: int = 32) -> float:
    n = psd.size
    x = psd[n // 4: 3 * n // 4]
    s = x[: x.size // k * k].reshape(-1, k).mean(axis=1)
    return float(np.std(s) / np.mean(s))


def test_sk_limits_have_a_tiny_false_alarm_on_noise():
    rng = np.random.default_rng(1)
    w = np.hanning(512)
    for m in (32, 64, 128):
        z = rng.standard_normal((40, m, 512)) + 1j * rng.standard_normal((40, m, 512))
        sk = np.concatenate([v2.spectral_kurtosis(np.abs(np.fft.fft(b * w, axis=1)) ** 2) for b in z])
        lo, hi = v2.sk_limits(m)
        assert abs(sk.mean() - 1.0) < 0.02
        assert np.mean((sk < lo) | (sk > hi)) < 0.002, m


def test_noise_level_matches_the_classic_median_and_noise_drops():
    nfft = 1024
    iq = _iq(_noise(nfft * 1024, 2))
    _, classic, _ = robust_psd_from_iq(iq, RATE, F0, fft_size=nfft, return_segments=True)
    r = v2.estimate(iq, RATE, F0, nfft)
    inner = slice(nfft // 4, 3 * nfft // 4)
    assert abs(np.median(r["psd"][inner]) / np.median(classic[inner]) - 1.0) < 0.03       # ln2 level match
    assert r["stats"]["cells_flagged_fraction"] < 0.003
    gain = _smoothed_rel_noise(classic) / _smoothed_rel_noise(r["psd"])
    assert gain > 1.45, gain                                                                # simulated ~1.7
    assert r["segments"].shape == (r["stats"]["blocks"], nfft)


def test_pulses_are_left_out_and_the_mean_does_not_blow_up():
    nfft = 1024
    z = _noise(nfft * 1024, 3)
    clean = v2.estimate(_iq(z), RATE, F0, nfft)
    for start in range(50_000, z.size, 3 * 131_072):             # short strong broadband pulses (~2 frames) in 1 block of 3
        z[start:start + 2 * nfft] *= 6.0
    r = v2.estimate(_iq(z), RATE, F0, nfft)
    assert r["stats"]["blocks_broadband_flagged"] == 3 and r["stats"]["channels_all_flagged"] == 0
    inner = slice(nfft // 4, 3 * nfft // 4)
    assert abs(np.median(r["psd"][inner]) / np.median(clean["psd"][inner]) - 1.0) < 0.05
    naive = lambda x: float(np.mean(np.abs(x) ** 2))
    assert naive(z) / naive(_noise(nfft * 1024, 3)) > 1.15                                   # what a plain mean would do


def test_a_carrier_is_cut_only_while_it_is_on_and_a_noise_like_line_is_kept():
    nfft, n = 1024, 1024 * 1024
    z = _noise(n, 4)
    t = np.arange(n)
    on = t < 3 * 131_072                                           # carrier on for the first 3 of 8 blocks
    z = z + on * 3.0 * np.exp(2j * np.pi * 100.5 / nfft * t)
    # an "HI line": extra stationary Gaussian noise in a band (filtered noise), the way the sky makes it
    line = np.fft.ifft(np.fft.fft(_noise(n, 5)) * (np.abs(np.fft.fftfreq(n) + 0.15) < 0.01))
    z = z + 3.0 * line
    r = v2.estimate(_iq(z), RATE, F0, nfft)
    ch = nfft // 2 + 100                                           # fftshifted bin of the carrier
    assert np.allclose(r["channel_flagged_fraction"][ch: ch + 2], 3 / 8, atol=0.01)   # its on-blocks left out...
    assert not r["rfi_channels"][ch: ch + 2].any()                                    # ...the clean ones used
    floor = np.median(r["psd"][nfft // 2 + 150: nfft // 2 + 250])
    assert np.all(r["psd"][ch: ch + 2] < 1.3 * floor)               # the carrier (+38 dB) is gone from the estimate
    line_ch = nfft // 2 - int(0.15 * nfft)
    assert not r["rfi_channels"][line_ch - 5: line_ch + 5].any()
    assert r["channel_flagged_fraction"][line_ch - 5: line_ch + 5].max() < 0.05


def test_too_short_a_capture_falls_back_to_the_classic_estimate():
    ev = v2.EstimatorV2()
    ev.read_capture(lambda p: ("iq", {}), "a.h5")
    called = {}

    def classic(*a, **k):
        called["yes"] = True
        return "CLASSIC"
    out = ev.estimate_spectrum(classic, _iq(_noise(8192 * 50, 6)), sample_rate_hz=RATE, center_frequency_hz=F0,
                               config=ReduceConfig())
    assert out == "CLASSIC" and called and ev.report()["estimates_classic_fallback"] == 1


def test_wrapper_returns_a_reduce_estimate_and_flags_rfi_channels_in_the_mask():
    ev = v2.EstimatorV2()
    ev.read_capture(lambda p: ("iq", {}), "a.h5")
    n = 8192 * 600
    t = np.arange(n)
    z = _noise(n, 7) + ((t // 600_000) % 2 == 0) * 4.0 * np.exp(2j * np.pi * 0.1 * t)
    est = ev.estimate_spectrum(lambda *a, **k: None, _iq(z), sample_rate_hz=RATE, center_frequency_hz=F0, config=ReduceConfig())
    assert est.psd.shape == (8192,) and est.fft_config["estimator"].startswith("v2")
    assert est.fft_config["n_segments"] == est.segments.shape[0] >= 4
    res = ev.build_mask(lambda f, p, **k: SimpleNamespace(mask=np.zeros(8192, np.int64)), est.frequency_hz, est.psd)
    assert (res.mask & MaskFlag.RFI.value).any()
    rep = ev.report()
    assert rep["estimates_v2"] == 1 and rep["per_point"][0]["estimates"][0]["rfi_channels_flagged_in_mask"] >= 1


def test_rfi_mask_steps_aside_for_pulses_on_top_of_v2():
    import reduce_rfi_mask as rrm
    est = SimpleNamespace(fft_config={"estimator": v2.ESTIMATOR}, segments=np.ones((5, 4)))
    mr = rrm.MaskedReduce(loader=lambda p: {"source": "sidecar", "sidecar": "x", "freq_hz": np.arange(4.0),
                                            "codes": np.zeros(4, np.int8), "frame_pulse": np.ones(9, bool)})
    mr.read_capture(lambda p: None, "a.h5")
    out = mr.estimate_spectrum(lambda *a, **k: est, b"", sample_rate_hz=1, center_frequency_hz=1, config=ReduceConfig())
    assert out is est and "ESTIMATOR v2" in mr.report()["per_point"][0]["pulse_note"]


def test_install_wraps_only_inside_and_restores_the_frozen_functions():
    from reduce_engine import fswitch_fold, masks, pipeline, spectral
    funcs = lambda: (pipeline.read_capture, spectral.estimate_spectrum, masks.build_mask, fswitch_fold.switched_spectrum)
    before = funcs()
    with v2.install():
        assert all(x is not y for x, y in zip(funcs(), before))
    assert funcs() == before


def test_web_reduce_passes_estimator_v2_only_when_asked(monkeypatch):
    import almita_web_ops as ops
    camp = ops.SERVE_ROOTS["mosaic"] / "__estimator_v2_test__"
    camp.mkdir(parents=True, exist_ok=True)
    try:
        rel = str(camp.relative_to(ops.ROOT))
        argv, meta = ops.build_command("reduce_plan", {"campaign_dir": rel}, "J1")
        assert "--estimator-v2" not in argv and "estimator_v2" not in meta
        argv, meta = ops.build_command("reduce_plan", {"campaign_dir": rel, "estimator_v2": True}, "J1")
        assert "--estimator-v2" not in argv and meta["estimator_v2"] is True
        monkeypatch.setattr(ops, "_require_fresh_plan", lambda *a, **k: None)
        argv, meta = ops.build_command("reduce", {"campaign_dir": rel, "estimator_v2": True, "rfi_mask": True}, "J1")
        assert argv[1] == "reduce_campaign_run.py" and "--estimator-v2" in argv and "--rfi-mask" in argv
    finally:
        camp.rmdir()


def test_reduce_page_has_the_checkbox():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / "console" / "reduce.html").read_text()
    js = (root / "console" / "reduce.js").read_text()
    assert 'id="in-estimator-v2"' in html and "estimator_v2" in js and "estimator_v2_report.json" in js


def test_folded_bins_touching_maps_a_baseband_channel_through_each_lo():
    bb = (np.arange(100) - 50) * 1000.0
    bad = np.zeros(100, bool)
    bad[70] = True                                           # baseband +20 kHz
    rf = 1e9 + bb
    out = v2.folded_bins_touching(rf, [1e9 - 10e3, 1e9 + 30e3], bb, bad)
    # rf - lo == +20 kHz  ->  rf = 1e9 + 10 kHz (bin 60) and 1e9 + 50 kHz (bin 100: outside)
    assert np.nonzero(out)[0].tolist() == [60]
    half = v2.folded_bins_touching(rf, [1e9 - 10.5e3], bb, bad)   # half a bin off: both neighbours interpolate it
    assert np.nonzero(half)[0].tolist() == [59, 60]


def test_a_switched_capture_keeps_the_kurtosis_flags_through_the_fold():
    """End to end through the frozen fold: an on/off carrier seen by both LOs ends up flagged RFI at its RF
    frequency in the folded spectrum (before 2026-11-10 the fold dropped v2's flags)."""
    import json
    from reduce_engine import fswitch_fold
    delta, n_lo = 600e3, 2 ** 21
    f_a, f_b = F0 - delta, F0 + delta
    fc = F0 + 100e3 + 0.3 * RATE / 8192                     # inside the folded band, between bins
    pieces = []
    for k, lo in enumerate((f_a, f_b)):
        t = np.arange(n_lo) / RATE
        z = _noise(n_lo, seed=20 + k)
        on = np.arange(n_lo) < int(0.7 * n_lo)                  # on 70 % of the time, then off
        z = z + on * 3.0 * np.exp(2j * np.pi * (fc - lo) * t)
        pieces.append(_iq(z))
    iq = np.concatenate(pieces)
    attrs = {"sample_rate_hz": RATE, "center_frequency_hz": F0, "fswitch_f_a_hz": f_a, "fswitch_f_b_hz": f_b,
             "fswitch_segments_json": json.dumps([{"lo_hz": f_a, "start_sample": 0, "n_samples": n_lo},
                                                  {"lo_hz": f_b, "start_sample": n_lo, "n_samples": n_lo}])}
    plain = fswitch_fold.switched_spectrum(iq, attrs, ReduceConfig())
    assert not (plain.mask & MaskFlag.RFI.value).any()       # the frozen fold alone flags nothing
    with v2.install() as ev:
        ev.current = "s.h5"
        sw = fswitch_fold.switched_spectrum(iq, attrs, ReduceConfig())
    flagged = np.nonzero(sw.mask & MaskFlag.RFI.value)[0]
    near = np.argsort(np.abs(sw.frequency_hz - fc))[:2]
    assert set(near.tolist()) <= set(flagged.tolist()) and flagged.size <= 12
    assert np.all(np.abs(sw.frequency_hz[flagged] - fc) < 5e3)
    rec = ev.points["s.h5"]
    assert rec["fswitch_folded_bins_flagged"] == flagged.size and ev.report()["fswitch_points"] == 1
