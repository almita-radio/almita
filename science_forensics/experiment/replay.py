"""Read-only replay of the new diagnostics on an EXISTING REDUCE session (e.g. the old 9-point campaign). REDUCE Level 1 only."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np

from science_forensics.ingest import load_forensics_input
from science_forensics.models import ForensicsConfig
from science_forensics.measure import measure_point
from science_forensics.experiment import analysis as A
from science_forensics.experiment.design import CHANNEL_WIDTH_HZ, F_REST_HZ

MASK_NAMES = ("KNOWN_SPUR", "DC", "RFI", "EDGE")


def _level1_metrics(reduce_dir: Path, point_index: int) -> dict:
    p = reduce_dir / "points" / str(point_index) / "master_spectrum.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text()).get("quality", {}).get("metrics", {})


def _corr(a, b):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 4 or np.std(a[ok]) == 0 or np.std(b[ok]) == 0:
        return None
    return float(np.corrcoef(a[ok], b[ok])[0, 1])


def replay_campaign(reduce_dir, *, window_lsrk_center_m_s: float, window_half_width_m_s: float, labels: Optional[list] = None) -> dict:
    reduce_dir = Path(reduce_dir)
    inp = load_forensics_input(reduce_dir)
    pts = inp.points
    n = len(pts)
    f0 = inp.rest_frequency_hz
    cfg = ForensicsConfig(window_center=window_lsrk_center_m_s, window_half_width=window_half_width_m_s, window_frame="lsrk_velocity",
                          feature_id="REPLAY")
    first = measure_point(pts[0], cfg)
    start = first.centroid_channel if np.isfinite(first.centroid_channel) else float(np.mean([m for m in [first.window_first_channel, first.window_last_channel]]))
    ms = A.track_line(pts, start_channel=start, search_half_width=60, fit_half_width=40)
    ii, f, sf = A.centroid_hz(pts, ms)
    cand = np.full(n, np.nan)
    for i in ii:
        cand[i] = ms[i].centroid_channel
    labels = labels or [f"P{p.point_index}" for p in pts]
    times = np.array([p.t_mid_s for p in pts])
    t_h = (times - times.min()) / 3600.0
    shift = np.array([p.lsrk_shift_m_s for p in pts])
    out: dict = {"campaign_id": inp.campaign_id, "reduce_session_dir": str(reduce_dir), "n_points": n, "f0_hz": f0,
                 "span_minutes": float(np.ptp(times) / 60.0), "median_cadence_s": float(np.median(np.diff(times)))}
    # --- candidate track and design confounding of THIS campaign
    valid = np.isfinite(cand)
    fit = A.ols(np.column_stack([np.ones(valid.sum()), t_h[valid]]), cand[valid]) if valid.sum() >= 4 else None
    out["candidate"] = {"start_channel": float(start), "n_detected": int(valid.sum()), "channels": [None if not np.isfinite(c) else float(c) for c in cand],
                        "slope_channels_per_hour": fit["coefficients"][1] if fit else None, "slope_se": fit["standard_errors"][1] if fit else None,
                        "total_channels_over_span": (fit["coefficients"][1] * float(np.ptp(t_h[valid]))) if fit else None}
    # --- persistent masks = known spurs/DC as REDUCE flagged them
    mask_frac = np.mean([(p.mask != 0) | ~np.isfinite(p.value) for p in pts], axis=0)
    persistent = np.flatnonzero(mask_frac >= 0.8)
    from reduce_engine.models import MaskFlag
    comp = {}
    for name in MASK_NAMES:
        bit = MaskFlag[name].value
        comp[name] = {"channels_flagged_in_all_captures": int(np.sum(np.all([(p.mask & bit) != 0 for p in pts], axis=0))),
                      "mean_fraction_of_channels": float(np.mean([np.mean((p.mask & bit) != 0) for p in pts]))}
    static = bool(np.all(np.std(np.array([(p.mask != 0) for p in pts], dtype=float), axis=0) < 0.5))
    out["known_spurs"] = {"n_persistently_masked_channels": int(persistent.size), "persistently_masked_channel_ranges": _ranges(persistent),
                          "mask_composition": comp,
                          "note": "REDUCE masks known spurs/DC at FIXED channel positions in every capture (receiver-fixed by construction); they are excluded from the global shift and cannot be tracked as lines"}
    # --- global spectral shift across capture order (whole spectrum, candidate excluded at its own position in each capture)
    gs = A.global_shift_series(pts, labels, only_reference_position=False, exclude_channels=persistent, candidate_channels=cand,
                               candidate_half_width=60, max_lag=100)
    k = f0 / A.C_LIGHT_M_S
    sky_step = [0.0] + [k * (shift[i] - shift[i - 1]) / CHANNEL_WIDTH_HZ for i in range(1, n)]
    sky_cum = np.cumsum(sky_step)
    cand_cum = np.array([c - cand[valid][0] if np.isfinite(c) else np.nan for c in cand])
    g_cum = np.array(gs["cumulative_shift_channels"], dtype=float)
    se_final = gs["se_channels"][-1] if gs["se_channels"] else float("nan")
    g_range = float(np.ptp(g_cum)) if len(g_cum) else float("nan")
    g_moves = bool(np.isfinite(se_final) and g_range > 5.0 * max(se_final, 0.05))
    out["global_shift"] = {"chained_consecutive_captures": True, "cumulative_channels_by_point": g_cum.tolist(), "reliable_steps": gs["n_reliable_steps"],
                           "n_steps": gs["n_steps"], "se_final_channels": se_final, "range_channels": g_range,
                           "total_channels": float(g_cum[-1]) if len(g_cum) else None,
                           "sky_stationary_expectation_cumulative_channels": sky_cum.tolist(),
                           "sky_stationary_expectation_total_channels": float(sky_cum[-1]),
                           "candidate_cumulative_channels": [None if not np.isfinite(c) else float(c) for c in cand_cum],
                           "candidate_total_channels": float(cand_cum[np.isfinite(cand_cum)][-1]) if np.isfinite(cand_cum).any() else None,
                           "global_shift_significant": g_moves,
                           "correlation_global_vs_candidate": _corr(g_cum, cand_cum) if g_moves else None,
                           "correlation_global_vs_sky_expectation": _corr(g_cum, sky_cum) if g_moves else None,
                           "correlation_candidate_vs_sky_expectation": _corr(cand_cum, sky_cum),
                           "reading": ("the whole-spectrum structure does NOT move (range within 5 x its uncertainty) while the candidate moved "
                                       f"{cand_cum[np.isfinite(cand_cum)][-1]:.1f} channels: candidate-specific motion relative to that structure"
                                       if not g_moves else "the whole-spectrum structure moves; compare with the candidate"),
                           "caveat": ("the correlated structure is whatever Level 1 spectra share (calibration/baseline pattern, persistent lines); if it is "
                                      "attached to the digitised frequency axis it cannot reveal an RF-frequency scale drift - independent sky/RFI control lines are needed")}
    # --- control lines
    ex = np.unique(np.concatenate([persistent] + [A._window(c, 60, pts[0].value.size) for c in cand[valid]])) if valid.any() else persistent
    lines = A.find_control_lines(pts, exclude_channels=ex, n_lines=5)
    out["control_features"] = {"n_found": len(lines), "lines": A.control_line_tracks(pts, lines, f0, CHANNEL_WIDTH_HZ)} if lines else {"n_found": 0, "lines": [],
                              "note": "no narrow persistent line stands out in the median spectrum at z >= 8 outside the candidate"}
    # --- baseline metrics vs the negative offset (Level 1 quality.metrics; read-only)
    met = [_level1_metrics(reduce_dir, p.point_index) for p in pts]
    base = np.array([m.get("baseline_fit_quality_rms_fraction", np.nan) for m in met], dtype=float)
    med_level = np.array([float(np.nanmedian(np.where(np.isfinite(p.value) & (p.mask == 0), p.value, np.nan))) for p in pts])
    out["baseline_metrics"] = {"baseline_fit_quality_rms_fraction": base.tolist(), "per_capture_median_relative_intensity": med_level.tolist(),
                               "corr_baseline_rms_vs_median_level": _corr(base, med_level), "corr_baseline_rms_vs_time": _corr(base, t_h),
                               "corr_median_level_vs_time": _corr(med_level, t_h), "corr_median_level_vs_candidate_channel": _corr(med_level, cand),
                               "corr_baseline_rms_vs_candidate_channel": _corr(base, cand), "n": int(np.isfinite(base).sum())}
    return out


def _ranges(idx: np.ndarray) -> list:
    if idx.size == 0:
        return []
    out, start, prev = [], int(idx[0]), int(idx[0])
    for v in idx[1:]:
        if int(v) != prev + 1:
            out.append([start, prev]); start = int(v)
        prev = int(v)
    out.append([start, prev])
    return out[:40]
