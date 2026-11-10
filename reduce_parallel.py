"""REDUCE on several cores (operator 2026-11-10: "podemos hacer que reduce ... sean multicore?" ... "dale").

reduce_engine.pipeline.reduce_campaign() (FROZEN) reduces the points one after the other, each with
reduce_point(point, manifest, config, calibration_profile, calibration_profile_path) - and every point is
independent of the others. This module computes those SAME reduce_point() calls in worker processes BEFORE
reduce_campaign() runs, then installs a lookup in place of pipeline.reduce_point so the frozen loop picks up each
precomputed outcome instead of computing it again. reduce_campaign() itself still does everything else exactly as
before (order, session files, events, aggregation, report). No reduce_engine file changes; nothing is re-pinned.

  - Workers run the real reduce_point() with the same opt-in wrappers the RUN asked for (reduce_rfi_mask,
    reduce_estimator_v2), installed in each worker; their per-point report records come back with the outcome and
    are merged into the main process's wrappers, so rfi_mask_report.json / estimator_v2_report.json are complete.
  - Worker count from the RAM actually available: peak memory per point measured 2026-11-10 on a 20 s capture
    (73 MB file) = 2.75 GB with the classic median estimate (~37x the file) and 0.40 GB with ESTIMATOR v2 (~5.5x).
  - Processes are started with "spawn" (no fork of a process that has threads); each worker uses one BLAS thread
    and runs at nice +10, so a capture, the web and the mount always come first. While an SDR program is running
    (capture.py, fswitch...: transit_logger.sdr_process_busy) auto uses at most half the cores.
  - Any point a worker could not hand back (a crash, a killed worker) is simply computed in the main process by
    the frozen loop as before - never lost, never invented.
"""
from __future__ import annotations

import contextlib
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

PEAK_FACTOR_CLASSIC = 37.0      # peak RSS / capture file size, classic median estimate (measured 2026-11-10)
PEAK_FACTOR_V2 = 5.5            # same, ESTIMATOR v2
WORKER_BASE_BYTES = 200e6       # interpreter + numpy/astropy imports per worker
RAM_USE_FRACTION = 0.70         # of MemAvailable


def mem_available_bytes() -> Optional[float]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return float(line.split()[1]) * 1024.0
    except OSError:
        return None
    return None


def _sdr_in_use() -> Optional[str]:
    try:
        import transit_logger
        return transit_logger.sdr_process_busy()
    except Exception:  # noqa: BLE001 - unknown = assume nothing; the workers still run at nice +10
        return None


def auto_workers(capture_bytes: List[int], estimator_v2: bool, *, cpus: Optional[int] = None,
                 available: Optional[float] = None, sdr_busy: Optional[str] = "check") -> Tuple[int, str]:
    """How many points to reduce at once: one core left for the web and the rest of the machine, and no more
    workers than fit in RAM_USE_FRACTION of the available memory at their measured peak."""
    cpus = cpus if cpus is not None else (os.cpu_count() or 1)
    available = available if available is not None else mem_available_bytes()
    n = len(capture_bytes)
    if n <= 1:
        return 1, "one point"
    by_cpu = max(1, cpus - 1)
    busy = _sdr_in_use() if sdr_busy == "check" else sdr_busy
    if busy:
        by_cpu = max(1, (cpus - 1) // 2)
    factor = PEAK_FACTOR_V2 if estimator_v2 else PEAK_FACTOR_CLASSIC
    per_worker = WORKER_BASE_BYTES + factor * max(capture_bytes)
    by_ram = max(1, int((available or 0) * RAM_USE_FRACTION // per_worker)) if available else 1
    w = max(1, min(by_cpu, by_ram, n))
    why = (f"{w} worker(s): {cpus} cores (1 kept free), ~{per_worker / 1e9:.2f} GB per point "
           f"({'ESTIMATOR v2' if estimator_v2 else 'classic'}), {((available or 0) / 1e9):.1f} GB available"
           + (f", half the cores because {busy} is running" if busy else ""))
    return w, why


# ------------------------------------------------------------------ worker side
_W: Dict[str, Any] = {}


def _worker_init(rfi_mask: bool, estimator_v2: bool, calibration_profile_path: Optional[str]) -> None:
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    try:
        os.nice(10)                                     # a capture / the web / the mount come first
    except OSError:
        pass
    from reduce_engine import calibration as calibration_mod, pipeline
    stack = contextlib.ExitStack()
    if estimator_v2:                                   # same order as reduce_campaign_run: v2 inside, RFI MASK around it
        import reduce_estimator_v2
        _W["v2"] = stack.enter_context(reduce_estimator_v2.install())
    if rfi_mask:
        import reduce_rfi_mask
        _W["mask"] = stack.enter_context(reduce_rfi_mask.install())
    _W["stack"] = stack                                # kept open for the worker's life
    _W["reduce_point"] = pipeline.reduce_point
    _W["profile"] = calibration_mod.load_profile(calibration_profile_path)
    _W["profile_path"] = calibration_profile_path


def _worker_point(point, manifest, config) -> Tuple[Any, Dict[str, Any]]:
    for key in ("v2", "mask"):
        if key in _W:
            _W[key].points.clear()
    out = _W["reduce_point"](point, manifest, config, _W["profile"], _W["profile_path"])
    records = {key: dict(_W[key].points) for key in ("v2", "mask") if key in _W}
    return out, records


# ------------------------------------------------------------------ main side
def _emit(text: str, stream=None) -> None:
    print(f"[REDUCE] {text.replace('{', '(').replace('}', ')')}", file=stream or sys.stderr, flush=True)


def prefetch(manifest, config, *, calibration_profile_path: Optional[str], workers: int, rfi_mask: bool = False,
             estimator_v2: bool = False, stream=None) -> Tuple[Dict[int, Any], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """reduce_point() for every point of the manifest in `workers` processes. Returns (outcomes by point_index,
    wrapper records {"v2": {...}, "mask": {...}}, facts)."""
    from reduce_campaign_run import _hms
    points = list(manifest.points)
    outcomes: Dict[int, Any] = {}
    records: Dict[str, Dict[str, Any]] = {"v2": {}, "mask": {}}
    started = time.monotonic()
    failures: List[str] = []
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_worker_init,
                             initargs=(rfi_mask, estimator_v2, calibration_profile_path)) as pool:
        futs = {pool.submit(_worker_point, p, manifest, config): p for p in points}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                out, recs = fut.result()
            except Exception as exc:  # noqa: BLE001 - that point is left to the frozen loop in this process
                failures.append(f"point {p.point_index}: {type(exc).__name__}: {exc}")
                _emit(f"point {p.point_index} - worker failed ({type(exc).__name__}) - it will be reduced here, sequentially", stream)
                continue
            outcomes[p.point_index] = out
            for key, rec in recs.items():
                records[key].update(rec)
            done = len(outcomes) + len(failures)
            elapsed = time.monotonic() - started
            eta = elapsed / done * (len(points) - done)
            status = str(getattr(out, "status", "?")).split(".")[-1]
            took = sum(v for v in (getattr(out, "timing_seconds", None) or {}).values() if isinstance(v, (int, float)))
            _emit(f"point {p.point_index} - {done}/{len(points)} - {status} ({took:.1f} s) - elapsed {_hms(elapsed)}"
                  f" - remaining ~{_hms(eta)}", stream)
    facts = {"workers": workers, "points_prefetched": len(outcomes), "worker_failures": failures,
             "prefetch_seconds": round(time.monotonic() - started, 2)}
    return outcomes, records, facts


@contextlib.contextmanager
def use_prefetched(outcomes: Dict[int, Any]) -> Iterator[Dict[str, int]]:
    """pipeline.reduce_point answers from `outcomes`; a point not there is computed by the real function."""
    from reduce_engine import pipeline
    original = pipeline.reduce_point
    used = {"prefetched": 0, "computed_here": 0}

    def lookup(point, *a, **k):
        out = outcomes.pop(point.point_index, None)
        if out is not None:
            used["prefetched"] += 1
            return out
        used["computed_here"] += 1
        return original(point, *a, **k)

    pipeline.reduce_point = lookup
    try:
        yield used
    finally:
        pipeline.reduce_point = original
