"""NEGATIVE INTEGRATED MAP AUDIT: why are integrated relative intensities negative? Purely diagnostic - the canonical SCIENCE
product is never recomputed or altered, no window is optimised to make anything positive, nothing is baseline-corrected.

Everything is computed from Level 1 point spectra (least transformed valid product); an existing SCIENCE integrated map is
only read (optional) to describe its distribution. Per-point integrals use the midpoint rule with the LOCAL channel width
(boundary quantisation <= 1 channel), which is a diagnostic approximation of, not a replacement for, SCIENCE's bin-overlap rule.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import h5py
import numpy as np

from reduce_engine.models import MaskFlag
from science_forensics.models import C_LIGHT_M_S, ForensicsConfig

PERCENTILES = (1, 5, 25, 50, 75, 95, 99)


def _usable(p) -> np.ndarray:
    return (p.mask == MaskFlag.GOOD.value) & np.isfinite(p.value) & np.isfinite(p.sigma) & (p.sigma > 0)


def candidate_lsrk_range(inp, config: ForensicsConfig) -> tuple:
    """The candidate window as an LSRK-velocity range (m/s), whatever frame the operator used."""
    c, h = config.window_center, config.window_half_width
    if config.window_frame == "lsrk_velocity":
        return c - h, c + h
    ref = inp.points[len(inp.points) // 2]
    shift = float(np.median([p.lsrk_shift_m_s for p in inp.points]))
    if config.window_frame == "frequency":
        v = [C_LIGHT_M_S * (1 - f / inp.rest_frequency_hz) + shift for f in (c - h, c + h)]
    else:
        n = np.arange(ref.value.shape[0], dtype=float)
        v = [float(np.interp(x, n, ref.velocity_lsrk_m_s)) for x in (c - h, c + h)]
    return min(v), max(v)


def integrate_window(p, vmin: float, vmax: float) -> dict:
    v = p.velocity_lsrk_m_s
    inside = (v >= vmin) & (v <= vmax)
    n_in = int(inside.sum())
    if n_in == 0:
        return {"integral": float("nan"), "coverage": float("nan"), "mean_value": float("nan"), "n_bins": 0, "n_valid": 0}
    dv = np.abs(np.gradient(v))
    ok = _usable(p) & inside
    total_w = float(dv[inside].sum())
    valid_w = float(dv[ok].sum())
    integral = float(np.sum(p.value[ok] * dv[ok])) if ok.any() else float("nan")
    return {"integral": integral, "coverage": valid_w / total_w if total_w > 0 else float("nan"),
            "mean_value": float(np.sum(p.value[ok] * dv[ok]) / valid_w) if valid_w > 0 else float("nan"),
            "n_bins": n_in, "n_valid": int(ok.sum())}


def _summ(values) -> dict:
    a = np.asarray(values, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0}
    out = {"n": int(a.size), "median": float(np.median(a)), "mean": float(a.mean()), "std": float(a.std(ddof=1)) if a.size > 1 else 0.0,
           "min": float(a.min()), "max": float(a.max()), "fraction_negative": float(np.mean(a < 0)),
           "fraction_positive": float(np.mean(a > 0))}
    out["percentiles"] = {str(q): float(np.percentile(a, q)) for q in PERCENTILES}
    return out


def select_control_windows(inp, config: ForensicsConfig) -> dict:
    """Left/right control windows of the candidate's width, separated by a guard gap, chosen for spectral validity (not overlapping
    the candidate, inside every point's velocity coverage, well populated) - never for their sign. Exact definitions and every
    attempt are persisted."""
    a, b = candidate_lsrk_range(inp, config)
    w = b - a
    guard = config.control_guard_fraction * w
    cover_lo = max(float(p.velocity_lsrk_m_s.min()) for p in inp.points)
    cover_hi = min(float(p.velocity_lsrk_m_s.max()) for p in inp.points)
    out = {"candidate_lsrk_range_m_s": [a, b], "width_m_s": w, "guard_m_s": guard, "min_valid_fraction": config.control_min_valid_fraction,
           "campaign_common_lsrk_coverage_m_s": [cover_lo, cover_hi], "windows": {}}
    for side, sign in (("left", -1), ("right", +1)):
        attempts = []
        chosen = None
        for step in range(0, 7):
            offset = guard + step * 0.5 * w
            lo, hi = (a - offset - w, a - offset) if sign < 0 else (b + offset, b + offset + w)
            inside_cov = lo >= cover_lo and hi <= cover_hi
            if inside_cov:
                covs = [integrate_window(p, lo, hi)["coverage"] for p in inp.points]
                frac_ok = float(np.mean([c >= config.control_min_valid_fraction for c in covs]))
                valid = frac_ok >= 0.8
            else:
                frac_ok, valid = float("nan"), False
            attempts.append({"lsrk_range_m_s": [lo, hi], "inside_common_coverage": bool(inside_cov),
                             "fraction_points_with_valid_coverage": frac_ok, "accepted": bool(valid)})
            if valid:
                chosen = [lo, hi]
                break
        out["windows"][side] = {"lsrk_range_m_s": chosen, "status": "OK" if chosen else "UNAVAILABLE", "attempts": attempts}
    return out


def window_table(inp, windows: dict) -> dict:
    """Per-point integral / mean value / coverage for named LSRK windows, plus campaign summaries."""
    per_point = {name: [integrate_window(p, lo, hi) for p in inp.points] for name, (lo, hi) in windows.items() if lo is not None}
    out = {}
    for name, rows in per_point.items():
        lo, hi = windows[name]
        out[name] = {"lsrk_range_m_s": [lo, hi], "width_m_s": hi - lo,
                     "integral_relative_m_s": _summ([r["integral"] for r in rows]),
                     "mean_relative_intensity": _summ([r["mean_value"] for r in rows]),
                     "coverage": _summ([r["coverage"] for r in rows]),
                     "per_point": [{"point_index": p.point_index, **r} for p, r in zip(inp.points, rows)]}
    return out


def interval_decomposition(inp, vmin: float, vmax: float, k: int) -> dict:
    edges = np.linspace(vmin, vmax, k + 1)
    rows = []
    for i in range(k):
        lo, hi = float(edges[i]), float(edges[i + 1])
        vals = [integrate_window(p, lo, hi) for p in inp.points]
        means = np.array([v["mean_value"] for v in vals])
        if not np.any(np.isfinite(means)):
            rows.append({"interval": i, "lsrk_range_m_s": [lo, hi], "status": "NO_COVERAGE", "median_mean_relative_intensity": None,
                         "p16": None, "p84": None, "fraction_points_negative": None, "median_coverage": None,
                         "median_integral_relative_m_s": None})
            continue
        rows.append({"interval": i, "status": "OK", "lsrk_range_m_s": [lo, hi], "median_mean_relative_intensity": float(np.nanmedian(means)),
                     "p16": float(np.nanpercentile(means, 16)), "p84": float(np.nanpercentile(means, 84)),
                     "fraction_points_negative": float(np.nanmean(means < 0)),
                     "median_coverage": float(np.nanmedian([v["coverage"] for v in vals])),
                     "median_integral_relative_m_s": float(np.nanmedian([v["integral"] for v in vals]))})
    return {"window_lsrk_m_s": [vmin, vmax], "n_intervals": k, "intervals": rows,
            "sum_of_interval_medians_relative_m_s": float(np.nansum([r["median_integral_relative_m_s"] for r in rows if r["median_integral_relative_m_s"] is not None]))}


def cumulative_integral(inp, vmin: float, vmax: float) -> dict:
    """Cumulative integral of the usable relative intensity along ascending LSRK velocity (masked bins contribute nothing and are
    counted in coverage). Evaluated on a common LSRK grid by interpolating each point's own cumulative curve."""
    dv_ref = float(np.median([abs(np.median(np.diff(p.velocity_lsrk_m_s))) for p in inp.points]))
    grid = np.arange(vmin, vmax + 0.5 * dv_ref, dv_ref)
    curves = []
    for p in inp.points:
        order = np.argsort(p.velocity_lsrk_m_s)
        v = p.velocity_lsrk_m_s[order]
        dv = np.abs(np.gradient(v))
        contrib = np.where(_usable(p)[order], p.value[order], 0.0) * dv
        cum = np.cumsum(contrib)
        inside = (v >= vmin) & (v <= vmax)
        if not inside.any():
            curves.append(np.full(grid.shape, np.nan))
            continue
        base = cum[np.flatnonzero(inside)[0]] - contrib[np.flatnonzero(inside)[0]]
        curves.append(np.interp(grid, v, cum - base, left=np.nan, right=np.nan))
    C = np.array(curves)
    n = len(inp.points)
    rep_idx = sorted({0, n // 2, n - 1})
    # the common grid can extend past a point's own coverage (NaN there): summaries use only columns finite for EVERY point
    full = np.flatnonzero(np.all(np.isfinite(C), axis=0))
    last = int(full[-1]) if full.size else None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med, p16, p84 = np.nanmedian(C, axis=0), np.nanpercentile(C, 16, axis=0), np.nanpercentile(C, 84, axis=0)
    return {"grid_lsrk_m_s": grid, "median": med, "p16": p16, "p84": p84,
            "representative": {str(inp.points[i].point_index): C[i] for i in rep_idx},
            "final_grid_lsrk_m_s": float(grid[last]) if last is not None else float("nan"),
            "final_value_median": float(np.median(C[:, last])) if last is not None else float("nan"),
            "final_value_p16": float(np.percentile(C[:, last], 16)) if last is not None else float("nan"),
            "final_value_p84": float(np.percentile(C[:, last], 84)) if last is not None else float("nan"), "grid_step_m_s": dv_ref}


def median_spectrum(inp, config: ForensicsConfig) -> dict:
    freqs = [p.frequency_hz for p in inp.points]
    common = all(np.array_equal(freqs[0], f) for f in freqs[1:])
    if not common:
        return {"status": "FREQUENCY_AXES_DIFFER", "reason": "points do not share one topocentric frequency axis"}
    V = np.array([np.where(_usable(p), p.value, np.nan) for p in inp.points])
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # channels masked in every point are all-NaN by construction
        med = np.nanmedian(V, axis=0)
        mad = 1.4826 * np.nanmedian(np.abs(V - med), axis=0)
    n_used = np.sum(np.isfinite(V), axis=0)
    mean_lsrk = np.mean([p.velocity_lsrk_m_s for p in inp.points], axis=0)
    imin, imax = config.integration_window_min_m_s, config.integration_window_max_m_s
    inside = (mean_lsrk >= imin) & (mean_lsrk <= imax) & np.isfinite(med)
    return {"status": "OK", "frequency_hz": freqs[0], "mean_lsrk_m_s": mean_lsrk, "median": med, "robust_spread": mad, "n_points": n_used,
            "summary": {"median_of_median_spectrum_in_window": float(np.median(med[inside])) if inside.any() else float("nan"),
                        "mean_of_median_spectrum_in_window": float(np.mean(med[inside])) if inside.any() else float("nan"),
                        "fraction_channels_negative_in_window": float(np.mean(med[inside] < 0)) if inside.any() else float("nan"),
                        "window_lsrk_m_s": [imin, imax]}}


def empirical_noise(inp, control: dict) -> dict:
    """Across-point scatter of the SAME channel in feature-free control windows vs the Level 1 reported sigma (diagnostic; the
    empirical value includes real point-to-point differences, not just thermal noise)."""
    out = {}
    ref = inp.points[len(inp.points) // 2]
    shift_med = float(np.median([p.lsrk_shift_m_s for p in inp.points]))
    for side, blk in control["windows"].items():
        if blk["lsrk_range_m_s"] is None:
            out[side] = {"status": "UNAVAILABLE"}
            continue
        lo, hi = blk["lsrk_range_m_s"]
        chans = np.flatnonzero((ref.velocity_lsrk_m_s >= lo) & (ref.velocity_lsrk_m_s <= hi))
        V = np.array([np.where(_usable(p)[chans], p.value[chans], np.nan) for p in inp.points])
        S = np.array([np.where(_usable(p)[chans], p.sigma[chans], np.nan) for p in inp.points])
        n = np.sum(np.isfinite(V), axis=0)
        ok = n >= 4
        if not ok.any():
            out[side] = {"status": "INSUFFICIENT_POINTS"}
            continue
        with np.errstate(all="ignore"):
            emp = np.nanstd(V[:, ok], axis=0, ddof=1)
            rep = np.nanmedian(S[:, ok], axis=0)
        out[side] = {"status": "OK", "n_channels": int(ok.sum()), "median_empirical_scatter": float(np.nanmedian(emp)),
                     "median_reported_sigma": float(np.nanmedian(rep)), "ratio_empirical_to_reported": float(np.nanmedian(emp / rep))}
    ratios = [v["ratio_empirical_to_reported"] for v in out.values() if v.get("status") == "OK"]
    out["combined_ratio_empirical_to_reported"] = float(np.mean(ratios)) if ratios else float("nan")
    return out


def science_map_audit(science_session_dir: str | Path, inp) -> dict:
    """Read-only description of an existing SCIENCE integrated map (never modified)."""
    d = Path(science_session_dir)
    manifest = json.loads((d / "manifest.json").read_text())
    if manifest.get("input_reduce_session_id") != inp.reduce_session_id:
        return {"status": "BLOCKED", "reason": f"science session was built from {manifest.get('input_reduce_session_id')!r}, "
                                                 f"not from {inp.reduce_session_id!r}"}
    with h5py.File(d / "maps" / "integrated_relative_intensity.h5", "r") as h:
        value, valid = h["value"][()], h["valid"][()]
        coverage = h["spectral_coverage_fraction"][()] if "spectral_coverage_fraction" in h else None
        npt = h["n_pointings"][()]
        weight = h["weight_sum"][()]
        units = str(h.attrs.get("units", ""))
    v = value[valid & np.isfinite(value)]
    out = {"status": "OK", "science_session_id": manifest.get("science_session_id"),
           "science_manifest_sha256": __import__("hashlib").sha256((d / "manifest.json").read_bytes()).hexdigest(),
           "units": units, "integrated_window_m_s": manifest.get("integrated_window_m_s"),
           "quality_state": manifest.get("quality", {}).get("state"),
           "input_quality_counts": manifest.get("quality", {}).get("metrics", {}).get("input_quality_counts"),
           "n_pixels": int(value.size), "n_valid_pixels": int(v.size), "distribution": _summ(v)}
    if v.size:
        terc = np.quantile(npt[valid], [1 / 3, 2 / 3])
        for name, sel in (("low_pointing_count", npt <= terc[0]), ("mid_pointing_count", (npt > terc[0]) & (npt <= terc[1])),
                          ("high_pointing_count", npt > terc[1])):
            s = valid & sel & np.isfinite(value)
            out.setdefault("by_pointing_count_tercile", {})[name] = _summ(value[s]) if s.any() else {"n": 0}
        if coverage is not None:
            out["spectral_coverage"] = _summ(coverage[valid])
    return out


def negative_map_audit(inp, config: ForensicsConfig, science_session_dir=None) -> dict:
    imin, imax = config.integration_window_min_m_s, config.integration_window_max_m_s
    a, b = candidate_lsrk_range(inp, config)
    control = select_control_windows(inp, config)
    windows = {"candidate": (a, b), "control_left": tuple(control["windows"]["left"]["lsrk_range_m_s"] or (None, None)),
               "control_right": tuple(control["windows"]["right"]["lsrk_range_m_s"] or (None, None)),
               "integration_window": (imin, imax)}
    table = window_table(inp, windows)
    rows_total = table["integration_window"]["per_point"]
    rows_cand = table["candidate"]["per_point"]
    excl = [t["integral"] - c["integral"] if (imin <= a and b <= imax) else t["integral"] for t, c in zip(rows_total, rows_cand)]
    decomposition = interval_decomposition(inp, imin, imax, config.n_velocity_intervals)
    by_quality = {}
    for q in sorted({p.quality_state for p in inp.points}):
        by_quality[q] = _summ([r["integral"] for p, r in zip(inp.points, rows_total) if p.quality_state == q])
    audit = {"status": "OK", "definition": "per-point Level 1 integral of relative_intensity over the given LSRK window "
             "(midpoint rule, masked bins skipped, units relative_intensity*m/s); diagnostic only",
             "integration_window_m_s": [imin, imax], "windows": {k: {kk: vv for kk, vv in v.items() if kk != "per_point"} for k, v in table.items()},
             "per_point": [{"point_index": p.point_index, **{f"{name}_{key}": table[name]["per_point"][i][key]
                                                              for name in table for key in ("integral", "mean_value", "coverage")}}
                           for i, p in enumerate(inp.points)],
             "integration_window_excluding_candidate_relative_m_s": _summ(excl),
             "control_windows": control, "interval_decomposition": decomposition, "by_reduce_quality_state": by_quality,
             "empirical_noise": empirical_noise(inp, control)}
    if science_session_dir is not None:
        audit["science_integrated_map"] = science_map_audit(science_session_dir, inp)
    return audit
