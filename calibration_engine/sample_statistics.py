"""Raw IQ sample statistics (Fase 7), computed WITHOUT modifying the data.

Format audited first (Fase 7's explicit instruction): rtl_tcp/RTL-SDR
network captures are interleaved UNSIGNED uint8 I/Q, nominally centered
near 127.5, not a signed ADC and not exactly centered (confirmed by
reading sdr_capture.py, hi_spectral_metric.robust_psd_from_iq, and
calibration_foundation._adc_statistics - all three already treat the raw
array as np.uint8). This module does not assume 127.5 is the true center;
it measures the real mean and reports both.

Streaming/chunked (Fase 58): because the raw alphabet is exactly 256
values (uint8), a running histogram is an EXACT sufficient statistic for
every metric here (mean, std, percentiles, rail fractions) with O(256)
memory regardless of capture length - so this never needs the whole IQ
array resident at once. accumulate_from_iterable() is the streaming entry
point; compute_sample_statistics() is the convenience non-streaming path
for an array already in memory (what most of this repo's existing HDF5
reads already produce - a single h5py dataset read).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator

import numpy as np

RAIL_LOW = 0
RAIL_HIGH = 255
# "Near-rail" is a headroom margin, not a clipping declaration by itself -
# see clipping.py for the ClippingStatus decision that uses these together
# with the exact rail-hit fraction. 2 codes out of 256 (~0.8% of full
# scale) is a deliberately small, documented margin - wide enough to catch
# a signal riding right at the rail without a single 0/255 sample yet,
# narrow enough not to flag ordinary Gaussian tails at any sane gain.
NEAR_RAIL_MARGIN_CODES = 2


def _histogram_to_percentile(histogram: np.ndarray, fraction: float) -> float:
    total = histogram.sum()
    if total == 0:
        return float("nan")
    cumulative = np.cumsum(histogram)
    target = fraction * total
    index = int(np.searchsorted(cumulative, target))
    return float(min(index, len(histogram) - 1))


@dataclass
class RawSampleStatistics:
    n_samples: int
    mean_i: float
    mean_q: float
    std_i: float
    std_q: float
    rms: float
    minimum: int
    maximum: int
    percentiles: Dict[str, float]     # "p0_1", "p1", "p50", "p99", "p99_9"
    dc_offset_i: float                 # mean_i - nominal_center
    dc_offset_q: float
    near_rail_fraction: float
    rail_hit_fraction: float
    histogram: list                    # 256 bins, I and Q combined

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_samples": self.n_samples, "mean_i": self.mean_i, "mean_q": self.mean_q,
            "std_i": self.std_i, "std_q": self.std_q, "rms": self.rms,
            "minimum": self.minimum, "maximum": self.maximum, "percentiles": self.percentiles,
            "dc_offset_i": self.dc_offset_i, "dc_offset_q": self.dc_offset_q,
            "near_rail_fraction": self.near_rail_fraction, "rail_hit_fraction": self.rail_hit_fraction,
            "histogram": self.histogram,
        }


class _StreamingAccumulator:
    """O(256) state regardless of how many chunks are fed in - Fase 58."""

    def __init__(self, nominal_center: float = 127.5):
        self.nominal_center = nominal_center
        self.histogram_i = np.zeros(256, dtype=np.int64)
        self.histogram_q = np.zeros(256, dtype=np.int64)
        self.sum_i = 0.0
        self.sum_q = 0.0
        self.sumsq_i = 0.0
        self.sumsq_q = 0.0
        self.n = 0

    def feed(self, iq_chunk: np.ndarray) -> None:
        raw = np.asarray(iq_chunk, dtype=np.uint8)
        if raw.ndim != 1 or raw.size % 2:
            raise ValueError("IQ chunk must be an even-length interleaved uint8 vector")
        i = raw[0::2]
        q = raw[1::2]
        self.histogram_i += np.bincount(i, minlength=256)
        self.histogram_q += np.bincount(q, minlength=256)
        i_f = i.astype(np.float64)
        q_f = q.astype(np.float64)
        self.sum_i += float(i_f.sum())
        self.sum_q += float(q_f.sum())
        self.sumsq_i += float(np.sum(i_f * i_f))
        self.sumsq_q += float(np.sum(q_f * q_f))
        self.n += i.size

    def finalize(self) -> RawSampleStatistics:
        if self.n == 0:
            raise ValueError("no samples accumulated - cannot compute statistics")
        mean_i = self.sum_i / self.n
        mean_q = self.sum_q / self.n
        var_i = max(self.sumsq_i / self.n - mean_i ** 2, 0.0)
        var_q = max(self.sumsq_q / self.n - mean_q ** 2, 0.0)
        std_i, std_q = float(np.sqrt(var_i)), float(np.sqrt(var_q))
        rms = float(np.sqrt((self.sumsq_i + self.sumsq_q) / (2 * self.n)))
        combined = self.histogram_i + self.histogram_q
        nonzero = np.nonzero(combined)[0]
        minimum = int(nonzero.min()) if nonzero.size else 0
        maximum = int(nonzero.max()) if nonzero.size else 0
        percentiles = {
            "p0_1": _histogram_to_percentile(combined, 0.001), "p1": _histogram_to_percentile(combined, 0.01),
            "p50": _histogram_to_percentile(combined, 0.50), "p99": _histogram_to_percentile(combined, 0.99),
            "p99_9": _histogram_to_percentile(combined, 0.999),
        }
        near_rail = int(np.sum(combined[:NEAR_RAIL_MARGIN_CODES])) + int(np.sum(combined[-NEAR_RAIL_MARGIN_CODES:]))
        rail_hit = int(combined[RAIL_LOW] + combined[RAIL_HIGH])
        total = int(combined.sum())
        return RawSampleStatistics(
            n_samples=self.n, mean_i=mean_i, mean_q=mean_q, std_i=std_i, std_q=std_q, rms=rms,
            minimum=minimum, maximum=maximum, percentiles=percentiles,
            dc_offset_i=mean_i - self.nominal_center, dc_offset_q=mean_q - self.nominal_center,
            near_rail_fraction=near_rail / total, rail_hit_fraction=rail_hit / total,
            histogram=combined.astype(int).tolist(),
        )


def accumulate_from_iterable(chunks: Iterable[np.ndarray], nominal_center: float = 127.5) -> RawSampleStatistics:
    accumulator = _StreamingAccumulator(nominal_center)
    for chunk in chunks:
        accumulator.feed(chunk)
    return accumulator.finalize()


def compute_sample_statistics(iq: np.ndarray, chunk_size: int = 1_000_000) -> RawSampleStatistics:
    """Convenience path for an array already in memory - still processed in
    chunks internally so this is a drop-in even when `iq` is an h5py
    dataset (sliceable, not necessarily loaded) rather than a plain
    ndarray."""
    def _chunks() -> Iterator[np.ndarray]:
        n = len(iq)
        step = chunk_size - (chunk_size % 2)
        for start in range(0, n, step):
            yield np.asarray(iq[start:start + step])
    return accumulate_from_iterable(_chunks())
