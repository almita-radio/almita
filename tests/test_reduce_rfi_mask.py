"""reduce_rfi_mask: opt-in RFI masks in REDUCE without changing reduce_engine (frozen)."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import rfi_mask
import reduce_rfi_mask as rrm
from reduce_engine.config import ReduceConfig
from reduce_engine.models import MaskFlag
from reduce_engine.spectral import SpectralEstimate

N, FRAMES = 64, 10


def _estimate(segments):
    freq = 1.42e9 + np.arange(N) * 300.0
    return SpectralEstimate(frequency_hz=freq, psd=np.median(segments, axis=0), segments=segments,
                            clipping_fraction=0.0, fft_config={"n_segments": segments.shape[0]})


def _mask(pulses, codes):
    return {"source": "sidecar", "sidecar": "x.rfimask.npz", "freq_hz": 1.42e9 + np.arange(N) * 300.0,
            "codes": codes, "frame_pulse": pulses}


def test_sidecar_path_matches_rfi_mask_live_layout():
    import rfi_mask_live
    cap = Path("data/mosaic/G/data/iq/20261102_162601/ALMITA_p1.h5")
    assert rrm.sidecar_path(cap).parent == rfi_mask_live.sidecar_dir(cap.parent)
    assert rrm.sidecar_path(cap).name == "ALMITA_p1.rfimask.npz"


def test_pulse_frames_leave_the_estimate_and_comb_line_channels_are_flagged():
    seg = np.ones((FRAMES, N))
    seg[[2, 3, 4, 5, 6, 7]] = 50.0                       # pulses dominate: the median of all frames is 50
    pulses = np.zeros(FRAMES, bool)
    pulses[[2, 3, 4, 5, 6, 7]] = True
    codes = np.zeros(N, np.int8)
    codes[10], codes[20], codes[30] = rfi_mask.CODE_COMB, rfi_mask.CODE_LINE, rfi_mask.CODE_PULSE
    mr = rrm.MaskedReduce(loader=lambda path: _mask(pulses, codes))
    mr.read_capture(lambda p: ("iq", {}), "a.h5")
    cfg = ReduceConfig(fft_size=rfi_mask.FFT_SIZE)
    est = mr.estimate_spectrum(lambda *a, **k: _estimate(seg), b"", sample_rate_hz=1, center_frequency_hz=1, config=cfg)
    assert est.segments.shape[0] == 4 and np.allclose(est.psd, 1.0)
    assert est.fft_config["rfi_mask_pulse_frames_removed"] == 6
    res = mr.build_mask(lambda f, p, **k: SimpleNamespace(mask=np.zeros(N, np.int64)), est.frequency_hz, est.psd)
    flagged = np.nonzero(res.mask & MaskFlag.RFI.value)[0].tolist()
    assert flagged == [10, 20]                          # comb + line; a PULSE code in the channel row is not a channel mask
    rep = mr.report()
    assert rep["points_masked"] == 1 and rep["pulse_frames_cut"] == 6 and rep["from_sidecar"] == 1


def test_frame_count_mismatch_keeps_the_estimate_and_says_why():
    seg = np.ones((FRAMES, N))
    mr = rrm.MaskedReduce(loader=lambda path: _mask(np.ones(FRAMES + 1, bool), np.zeros(N, np.int8)))
    mr.read_capture(lambda p: None, "a.h5")
    est0 = _estimate(seg)
    est = mr.estimate_spectrum(lambda *a, **k: est0, b"", sample_rate_hz=1, center_frequency_hz=1,
                               config=ReduceConfig(fft_size=rfi_mask.FFT_SIZE))
    assert est is est0 and "not cut" in mr.report()["per_point"][0]["pulse_note"]


def test_a_point_whose_mask_cannot_be_read_is_reduced_unmasked_and_reported():
    def boom(path):
        raise ValueError("no frames")
    mr = rrm.MaskedReduce(loader=boom)
    mr.read_capture(lambda p: None, "a.h5")
    est0 = _estimate(np.ones((FRAMES, N)))
    assert mr.estimate_spectrum(lambda *a, **k: est0, b"", sample_rate_hz=1, center_frequency_hz=1,
                                config=ReduceConfig()) is est0
    res = mr.build_mask(lambda f, p, **k: SimpleNamespace(mask=np.zeros(N, np.int64)), est0.frequency_hz, est0.psd)
    assert not res.mask.any()
    rep = mr.report()
    assert rep["points_masked"] == 0 and rep["per_point"][0]["applied"] is False and "no frames" in rep["per_point"][0]["reason"]


def test_install_wraps_only_inside_and_restores_the_frozen_functions():
    from reduce_engine import masks, pipeline, spectral
    before = (pipeline.read_capture, spectral.estimate_spectrum, masks.build_mask)
    with rrm.install(loader=lambda p: None):
        assert (pipeline.read_capture, spectral.estimate_spectrum, masks.build_mask) != before
    assert (pipeline.read_capture, spectral.estimate_spectrum, masks.build_mask) == before


def test_web_reduce_passes_rfi_mask_only_when_asked(monkeypatch, tmp_path):
    import almita_web_ops as ops
    camp = ops.SERVE_ROOTS["mosaic"] / "__rfi_mask_test__"
    camp.mkdir(parents=True, exist_ok=True)
    try:
        argv, meta = ops.build_command("reduce_plan", {"campaign_dir": str(camp.relative_to(ops.ROOT))}, "J1")
        assert "--rfi-mask" not in argv and "rfi_mask" not in meta
        argv, meta = ops.build_command("reduce_plan", {"campaign_dir": str(camp.relative_to(ops.ROOT)), "rfi_mask": True}, "J1")
        assert "--rfi-mask" not in argv and meta["rfi_mask"] is True        # PLAN records it; RUN applies it
        monkeypatch.setattr(ops, "_require_fresh_plan", lambda *a, **k: None)
        argv, meta = ops.build_command("reduce", {"campaign_dir": str(camp.relative_to(ops.ROOT)), "rfi_mask": True}, "J1")
        assert argv[1] == "reduce_campaign_run.py" and "--rfi-mask" in argv and meta["rfi_mask"] is True
    finally:
        camp.rmdir()
