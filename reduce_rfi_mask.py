"""REDUCE with the RFI masks (operator 2026-11-02: "dale con la mascara en reduce") - OPT-IN, off by default.

What it does, per point, with the capture's rfi_mask.py result (rfi_mask.py's module docstring explains the masks):
  - PULSE: the FFT frames rfi_mask flagged as pulses are left out of REDUCE's spectral estimate. Both cut the capture
    into the same consecutive 8192-sample frames from its first sample (rfi_mask.read_capture_frames and
    hi_spectral_metric.robust_psd_from_iq), so frame k is the same samples in both; the PSD is recombined from the
    remaining frames with REDUCE's own combine mode.
  - COMB / LINE: those channels get MaskFlag.RFI in REDUCE's mask - flagged, never filled (NATIVE_GRID). The baseline
    fit and the stacking already leave out any bin with a flag.
The mask comes from the side-car rfi_mask_live.py wrote during the run (data/mosaic/<grid>/data/rfi_mask/<session>/
<stem>.rfimask.npz); a capture without one is analysed now with rfi_mask.analyze_capture (nothing is written, and
lines "known" from earlier captures of the session are not available then - the report says which source was used).

reduce_engine is FROZEN (scripts/science_forensics_field.py pins it for SCIENCE V1): no file of it is changed. Inside
the REDUCE RUN process only (reduce_campaign_run.py --rfi-mask), install() wraps three of its functions - the capture
read (to know which file a point is), the spectral estimate and the mask - and leaves them as they were on exit.
Without --rfi-mask nothing here runs and REDUCE gives exactly what it gave before.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import numpy as np

import rfi_mask

REPORT_NAME = "rfi_mask_report.json"


def sidecar_path(capture: Path) -> Path:
    """data/mosaic/<grid>/data/iq/<session>/<stem>.h5 -> data/mosaic/<grid>/data/rfi_mask/<session>/<stem>.rfimask.npz
    (the layout rfi_mask_live.sidecar_dir() writes)."""
    capture = Path(capture)
    return capture.parent.parent.parent / "rfi_mask" / capture.parent.name / f"{capture.stem}.rfimask.npz"


def load_mask(capture: Path) -> Dict[str, Any]:
    """The capture's mask arrays: its side-car when there is one, otherwise analysed now (read-only)."""
    sc = sidecar_path(capture)
    if sc.is_file():
        with np.load(sc) as z:
            return {"source": "sidecar", "sidecar": str(sc), "freq_hz": z["freq_hz"], "codes": z["codes"],
                    "frame_pulse": z["frame_pulse"].astype(bool)}
    a = rfi_mask.analyze_capture(Path(capture))["arrays"]
    return {"source": "computed_at_reduce", "sidecar": None, "freq_hz": a["freq_hz"], "codes": a["codes"],
            "frame_pulse": np.asarray(a["frame_pulse"], bool)}


class MaskedReduce:
    """Wraps the frozen pipeline's read/estimate/mask for one RUN and keeps a per-point report."""

    def __init__(self, loader=load_mask):
        self.loader = loader
        self.current: Optional[Path] = None
        self.current_mask: Optional[Dict[str, Any]] = None
        self.points: Dict[str, Dict[str, Any]] = {}

    def _mask_for_current(self) -> Optional[Dict[str, Any]]:
        if self.current is None:
            return None
        if self.current_mask is None:
            key = str(self.current)
            try:
                self.current_mask = self.loader(self.current)
            except Exception as exc:  # noqa: BLE001 - a point without a usable mask is reduced unmasked, and said so
                self.points[key] = {"capture": key, "applied": False, "reason": f"{type(exc).__name__}: {exc}"}
                self.current_mask = {"failed": True}
            else:
                self.points[key] = {"capture": key, "applied": True, "source": self.current_mask["source"],
                                    "sidecar": self.current_mask["sidecar"]}
        return None if self.current_mask.get("failed") else self.current_mask

    def read_capture(self, original, path, *a, **k):
        self.current, self.current_mask = Path(path), None
        return original(path, *a, **k)

    def estimate_spectrum(self, original, iq_bytes, *, sample_rate_hz, center_frequency_hz, config):
        est = original(iq_bytes, sample_rate_hz=sample_rate_hz, center_frequency_hz=center_frequency_hz, config=config)
        m = self._mask_for_current()
        if m is None:
            return est
        rec = self.points[str(self.current)]
        pulses, seg = m["frame_pulse"], est.segments
        if config.fft_size != rfi_mask.FFT_SIZE or len(pulses) != seg.shape[0]:
            rec.update(pulse_frames_cut=0, pulse_note=f"not cut: mask has {len(pulses)} frames of {rfi_mask.FFT_SIZE}, "
                                                       f"REDUCE {seg.shape[0]} of {config.fft_size}")
            return est
        keep = ~pulses
        rec.update(frames=int(seg.shape[0]), pulse_frames_cut=int(pulses.sum()))
        if not pulses.any():
            return est
        if keep.sum() < 4:
            rec.update(pulse_frames_cut=0, pulse_note="not cut: fewer than 4 frames would remain")
            return est
        kept = seg[keep]
        psd = np.median(kept, axis=0) if config.combine == "median" else np.mean(kept, axis=0)
        fft_config = {**est.fft_config, "n_segments": int(kept.shape[0]), "rfi_mask_pulse_frames_removed": int(pulses.sum())}
        return dataclasses.replace(est, psd=psd, segments=kept, fft_config=fft_config)

    def build_mask(self, original, frequency_hz, psd, **k):
        from reduce_engine.models import MaskFlag
        result = original(frequency_hz, psd, **k)
        m = self._mask_for_current()
        if m is None:
            return result
        rec = self.points[str(self.current)]
        codes, freq = np.asarray(m["codes"]), np.asarray(m["freq_hz"], float)
        bad = (codes == rfi_mask.CODE_COMB) | (codes == rfi_mask.CODE_LINE)
        if freq.shape != frequency_hz.shape or not np.allclose(freq, frequency_hz, rtol=0, atol=1.0):
            # another channel grid: flag every REDUCE bin whose frequency falls in a masked channel of the mask
            width = float(np.median(np.diff(freq)))
            idx = np.clip(np.round((frequency_hz - freq[0]) / width).astype(int), 0, len(freq) - 1)
            bad = bad[idx]
            rec["channel_mapping"] = "nearest mask channel"
        new = bad & ((result.mask & MaskFlag.RFI.value) == 0)
        result.mask[bad] |= MaskFlag.RFI.value
        rec.update(rfi_channels_flagged=int(bad.sum()), rfi_channels_newly_flagged=int(new.sum()),
                   comb_channels=int((codes == rfi_mask.CODE_COMB).sum()), line_channels=int((codes == rfi_mask.CODE_LINE).sum()),
                   fraction_of_band=round(float(bad.mean()), 5))
        return result

    def report(self) -> Dict[str, Any]:
        pts = list(self.points.values())
        applied = [p for p in pts if p.get("applied")]
        return {"schema": "almita.reduce_rfi_mask.v1", "enabled": True,
                "what": "PULSE frames left out of the spectral estimate; COMB and LINE channels flagged RFI (never filled)",
                "points": len(pts), "points_masked": len(applied),
                "from_sidecar": sum(1 for p in applied if p.get("source") == "sidecar"),
                "computed_at_reduce": sum(1 for p in applied if p.get("source") == "computed_at_reduce"),
                "median_fraction_of_band": (float(np.median([p["fraction_of_band"] for p in applied if "fraction_of_band" in p]))
                                            if any("fraction_of_band" in p for p in applied) else None),
                "pulse_frames_cut": int(sum(p.get("pulse_frames_cut", 0) for p in applied)),
                "per_point": pts}


@contextlib.contextmanager
def install(loader=load_mask) -> Iterator[MaskedReduce]:
    """Wrap the frozen pipeline's three functions for the duration of one RUN in this process; always restored."""
    from reduce_engine import masks as masks_mod, pipeline, spectral as spectral_mod
    mr = MaskedReduce(loader)
    saved = (pipeline.read_capture, spectral_mod.estimate_spectrum, masks_mod.build_mask)
    orig_read, orig_est, orig_mask = saved
    pipeline.read_capture = lambda path, *a, **k: mr.read_capture(orig_read, path, *a, **k)
    spectral_mod.estimate_spectrum = lambda *a, **k: mr.estimate_spectrum(orig_est, *a, **k)
    masks_mod.build_mask = lambda *a, **k: mr.build_mask(orig_mask, *a, **k)
    try:
        yield mr
    finally:
        pipeline.read_capture, spectral_mod.estimate_spectrum, masks_mod.build_mask = saved


def write_report(output_dir: str, report: Dict[str, Any]) -> str:
    path = Path(output_dir) / REPORT_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2, default=float))
    tmp.replace(path)
    return str(path)
