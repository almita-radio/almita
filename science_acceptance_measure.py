#!/usr/bin/env python3
"""SCIENCE acceptance MEASUREMENTS (docs/SCIENCE_ACCEPTANCE.md): memory model, larger-grid stress, float32 precision,
HDF5 layout/compression, beam-cutoff sensitivity. Each mode prints one JSON line so numbers are copied, never
re-typed. Modes that allocate a large cube run in their own process (peak RSS = ru_maxrss of that process) and
are always preceded by the preflight estimate. Synthetic Level 1 or a real REDUCE session only - never RAW.

  science_acceptance_measure.py mem NX NY NV          build+persist a synthetic cube, report estimate vs peak RSS
  science_acceptance_measure.py precision SCIENCE_SESSION_DIR
  science_acceptance_measure.py io SCIENCE_SESSION_DIR [--scratch DIR]
  science_acceptance_measure.py cutoff REDUCE_SESSION_DIR BEAM_FWHM_DEG [--scratch DIR]
"""
import argparse
import json
import resource
import sys
import time
from pathlib import Path

import h5py
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def mode_mem(args) -> dict:
    from science_engine.config import ScienceConfig
    from science_engine.cube import build_cube
    from science_engine.integration import integrated_map
    from science_engine.models import BeamModel, ScienceGrid
    from science_engine.moments import moment1_like, moment2_like
    from science_engine.simulation import build_synthetic_science_input, rectangular_grid_specs
    from science_engine.storage import ScienceSession
    from science_engine.validation import estimate_output_bytes, estimate_peak_memory_bytes
    import tempfile

    nx, ny, nv = args.nx, args.ny, args.nv
    specs = rectangular_grid_specs(12.0, -30.0, 3, 3, spacing_deg=1.0)
    for i, s in enumerate(specs):
        s.velocity_offset_m_s = 61.8 * (i % 5) * 2.3        # forces the resample path like real data
    si = build_synthetic_science_input(specs, n_channels=nv, line_amplitude_fn=lambda r, d: 0.3, line_fwhm_m_s=20000.0,
                                       noise_sigma=0.04, descending=True, velocity_min_m_s=-272904.0, velocity_max_m_s=233581.0)
    scale = 6.0 / max(nx, ny)
    grid = ScienceGrid(frame="icrs", center_ra_deg=180.0, center_dec_deg=-30.0, width_deg=nx * scale, height_deg=ny * scale,
                       pixel_scale_deg=scale, nx=nx, ny=ny)
    config = ScienceConfig(beam_fwhm_deg=1.5, velocity_window_min_m_s=-1e5, velocity_window_max_m_s=1e5)
    beam = BeamModel(fwhm_deg=1.5)
    estimate_peak, estimate_out = estimate_peak_memory_bytes(grid, nv), estimate_output_bytes(grid, nv)
    baseline_rss = peak_rss_mb()
    stages = {}
    t = time.perf_counter()
    cube = build_cube(si, grid, beam, config)
    stages["cube"] = time.perf_counter() - t
    rss_after_cube = peak_rss_mb()
    t = time.perf_counter()
    m0 = integrated_map(cube, config)
    m1 = moment1_like(cube, config, m0)
    m2 = moment2_like(cube, config, m1)
    stages["maps"] = time.perf_counter() - t
    rss_after_maps = peak_rss_mb()
    with tempfile.TemporaryDirectory() as tmp:
        session = ScienceSession(tmp, "MEM", session_id="S")
        t = time.perf_counter()
        beam_dict = beam.to_dict()
        path = session.write_cube(cube, campaign_id="MEM", reduce_session_id="R", beam=beam_dict, config_hash="h",
                                  quality_state="GOOD", data_completeness="COMPLETE")
        stages["write_cube"] = time.perf_counter() - t
        size = path.stat().st_size
    return {"mode": "mem", "grid": [ny, nx], "n_channels": nv, "voxels": nx * ny * nv,
            "estimated_peak_ram_mb": estimate_peak / 1024 ** 2, "estimated_output_mb": estimate_out / 1024 ** 2,
            "baseline_rss_mb": baseline_rss, "peak_rss_after_cube_mb": rss_after_cube,
            "peak_rss_after_maps_mb": rss_after_maps, "peak_rss_final_mb": peak_rss_mb(),
            "actual_cube_file_mb": size / 1024 ** 2, "stage_seconds": stages,
            "bytes_per_voxel_peak_measured": (peak_rss_mb() - baseline_rss) * 1024 ** 2 / (nx * ny * nv)}


def mode_precision(args) -> dict:
    from science_engine.config import ScienceConfig
    from science_engine.integration import integrated_map
    from science_engine.models import ScienceCube, ScienceGrid
    session = Path(args.session)
    manifest = json.loads((session / "manifest.json").read_text())
    with h5py.File(session / "cube" / "science_cube.h5") as h:
        v, ri, un = h["velocity_lsrk_m_s"][()], h["relative_intensity"][()], h["uncertainty"][()]
        ws, npt, valid = h["weight_sum"][()], h["n_pointings"][()], h["valid"][()]
    grid = ScienceGrid(**{k: manifest["grid"][k] for k in ("frame", "center_ra_deg", "center_dec_deg", "width_deg",
                                                             "height_deg", "pixel_scale_deg", "nx", "ny")})
    cfg = ScienceConfig(beam_fwhm_deg=manifest["beam"]["fwhm_deg"],
                        velocity_window_min_m_s=manifest["integrated_window_m_s"][0],
                        velocity_window_max_m_s=manifest["integrated_window_m_s"][1])
    ref = integrated_map(ScienceCube(grid, v, ri, un, ws, npt, valid), cfg)
    f32 = lambda a: a.astype(np.float32).astype(np.float64)
    low = integrated_map(ScienceCube(grid, f32(v), f32(ri), f32(un), ws, npt, valid), cfg)
    rel = lambda a, b: float(np.nanmax(np.abs(a - b) / np.maximum(np.abs(a), 1e-300)))
    scale = float(np.nanmax(np.abs(ri)))
    d_int = np.abs(f32(ri) - ri)[valid]
    return {"mode": "precision", "n_voxels": int(ri.size),
            "intensity_max_abs_err_float32": float(d_int.max()), "intensity_max_rel_err_of_peak": float(d_int.max() / scale),
            "uncertainty_max_rel_err_float32": rel(un[valid], f32(un)[valid]),
            "velocity_axis_max_abs_err_m_s": float(np.abs(f32(v) - v).max()),
            "velocity_axis_max_abs_err_channels": float(np.abs(f32(v) - v).max() / np.median(np.abs(np.diff(v)))),
            "integrated_map_max_abs_err": float(np.nanmax(np.abs(ref.value - low.value))),
            "integrated_map_max_rel_err_of_peak": float(np.nanmax(np.abs(ref.value - low.value)) / np.nanmax(np.abs(ref.value))),
            "integrated_uncertainty_max_rel_err": rel(ref.uncertainty, low.uncertainty),
            "median_uncertainty": float(np.nanmedian(un))}


def mode_io(args) -> dict:
    import shutil
    with h5py.File(Path(args.session) / "cube" / "science_cube.h5") as h:
        arrays = {k: h[k][()] for k in ("relative_intensity", "uncertainty", "weight_sum", "n_pointings", "valid")}
    nv, ny, nx = arrays["relative_intensity"].shape
    scratch = Path(args.scratch or "/tmp")
    scratch.mkdir(parents=True, exist_ok=True)
    layouts = {"contiguous_none": dict(), "contiguous_lzf": dict(compression="lzf"),
               "contiguous_gzip1": dict(compression="gzip", compression_opts=1),
               "chunked_64ch_none": dict(chunks=(64, ny, nx)), "chunked_64ch_lzf": dict(chunks=(64, ny, nx), compression="lzf"),
               "chunked_64ch_gzip1": dict(chunks=(64, ny, nx), compression="gzip", compression_opts=1),
               "chunked_spectrum_gzip1": dict(chunks=(nv, 1, 1), compression="gzip", compression_opts=1)}
    out = {}
    for name, kw in layouts.items():
        path = scratch / f"io_{name}.h5"
        t = time.perf_counter()
        with h5py.File(path, "w") as h:
            for k, a in arrays.items():
                h.create_dataset(k, data=a, **({**kw} if a.ndim == 3 and not (kw.get("chunks") and False) else {}))
        write_s = time.perf_counter() - t
        with h5py.File(path, "r") as h:
            d = h["relative_intensity"]
            def timeit(fn, n=5):
                t0 = time.perf_counter()
                for _ in range(n):
                    fn()
                return (time.perf_counter() - t0) / n * 1000
            out[name] = {"file_mb": path.stat().st_size / 1024 ** 2, "write_s": write_s,
                         "read_channel_ms": timeit(lambda: d[nv // 2]), "read_spectrum_ms": timeit(lambda: d[:, ny // 2, nx // 2]),
                         "read_full_cube_ms": timeit(lambda: d[()], 2)}
        path.unlink()
    per_array = {k: a.nbytes / 1024 ** 2 for k, a in arrays.items()}
    return {"mode": "io", "shape": [nv, ny, nx], "raw_array_mb": per_array, "layouts": out}


def mode_cutoff(args) -> dict:
    from science_engine.config import ScienceConfig
    from science_engine.products import run_science_session
    import tempfile
    rows = {}
    with tempfile.TemporaryDirectory(dir=args.scratch) as tmp:
        for n in (1.0, 1.5, 2.0, 3.0):
            cfg = ScienceConfig(beam_fwhm_deg=args.fwhm, beam_source="operator_config", beam_status="CONFIGURED_OPERATIONAL",
                                beam_cutoff_n_fwhm=n)
            t = time.perf_counter()
            report = run_science_session(args.reduce_session, cfg, output_root=tmp)
            runtime = time.perf_counter() - t
            s = Path(report.output_dir)
            with h5py.File(s / "maps" / "integrated_relative_intensity.h5") as h:
                val, unc, valid = h["value"][()], h["uncertainty"][()], h["valid"][()]
            with h5py.File(s / "cube" / "science_cube.h5") as h:
                voxel_valid = float(np.mean(h["valid"][()]))
            rows[str(n)] = {"runtime_s": runtime, "valid_voxel_fraction": voxel_valid, "valid_map_fraction": float(valid.mean()),
                            "integrated_flux_proxy_sum": float(np.nansum(val)), "median_integrated_sigma": float(np.nanmedian(unc)),
                            "n_valid_pixels": int(valid.sum())}
    return {"mode": "cutoff", "fwhm_deg": args.fwhm, "cutoffs_n_fwhm": rows}


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="mode", required=True)
    m = sub.add_parser("mem"); m.add_argument("nx", type=int); m.add_argument("ny", type=int); m.add_argument("nv", type=int)
    for name in ("precision", "io"):
        q = sub.add_parser(name); q.add_argument("session"); q.add_argument("--scratch", default=None)
    c = sub.add_parser("cutoff"); c.add_argument("reduce_session"); c.add_argument("fwhm", type=float); c.add_argument("--scratch", default=None)
    args = p.parse_args()
    result = {"mem": mode_mem, "precision": mode_precision, "io": mode_io, "cutoff": mode_cutoff}[args.mode](args)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
