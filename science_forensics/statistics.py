"""Campaign-level diagnostics of a feature track: frame coherence, simple OLS models, time/sky confounding, drift slopes,
separate (non-composite) metrics and rule-based evidence statements.

Regressions here are DIAGNOSTICS, not causal inference: with a raster/serpentine mosaic capture time, point order and sky
position are entangled, and the confounding block exists precisely to say so. Language is restricted to
OBSERVED / MEASURED / CONSISTENT WITH / INCONSISTENT WITH / UNRESOLVED.
"""
from __future__ import annotations

import numpy as np

from science_forensics.models import ForensicsConfig

LABELS = ("OBSERVED", "MEASURED", "CONSISTENT WITH", "INCONSISTENT WITH", "UNRESOLVED")


def _finite_pairs(*arrays):
    ok = np.ones(len(arrays[0]), dtype=bool)
    for a in arrays:
        ok &= np.isfinite(a)
    return ok


def ols(X: np.ndarray, y: np.ndarray) -> dict:
    """Ordinary least squares with intercept already in X. Reports R2, adjusted R2, residual RMS, AIC/AICc, SEs."""
    n, k = X.shape
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    rss = float(np.sum(resid ** 2))
    tss = float(np.sum((y - y.mean()) ** 2))
    dof = n - k
    r2 = 1.0 - rss / tss if tss > 0 else float("nan")
    sigma2 = rss / dof if dof > 0 else float("nan")
    try:
        cov = sigma2 * np.linalg.pinv(X.T @ X)
        se = np.sqrt(np.clip(np.diag(cov), 0, None))
    except np.linalg.LinAlgError:
        se = np.full(k, np.nan)
    per = max(rss / n, 1e-300)
    aic = n * np.log(per) + 2 * k
    aicc = aic + 2 * k * (k + 1) / (n - k - 1) if n - k - 1 > 0 else float("nan")
    return {"n": int(n), "k": int(k), "coefficients": beta.tolist(), "standard_errors": se.tolist(), "rss": rss, "r2": r2,
            "adjusted_r2": (1 - (1 - r2) * (n - 1) / dof) if dof > 0 and np.isfinite(r2) else float("nan"),
            "residual_rms": float(np.sqrt(rss / dof)) if dof > 0 else float("nan"),
            "residual_rms_biased": float(np.sqrt(rss / n)), "aic": float(aic), "aicc": float(aicc)}


def predictors(points, measurements, config: ForensicsConfig) -> dict:
    """Aligned predictor arrays for the points with a usable centroid (time in hours, order, sky offsets in degrees)."""
    used = [(p, m) for p, m in zip(points, measurements) if m.status == "OK" and np.isfinite(m.centroid_channel)]
    if not used:
        return {"used": []}
    t = np.array([p.t_mid_s for p, _ in used])
    ra = np.array([p.ra_deg for p, _ in used])
    dec = np.array([p.dec_deg for p, _ in used])
    ra_mean = np.degrees(np.arctan2(np.mean(np.sin(np.radians(ra))), np.mean(np.cos(np.radians(ra))))) % 360.0
    d_ra = (ra - ra_mean + 180.0) % 360.0 - 180.0
    ra_off = d_ra * np.cos(np.radians(dec.mean()))
    return {"used": used, "point_index": np.array([p.point_index for p, _ in used], dtype=float),
            "time_h": (t - t.min()) / 3600.0, "ra_off_deg": ra_off, "dec_off_deg": dec - dec.mean(),
            "ra_deg": ra, "dec_deg": dec}


def _corr(a, b):
    if np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def confounding_block(pred: dict, config: ForensicsConfig) -> dict:
    t, order, ra, dec = pred["time_h"], pred["point_index"], pred["ra_off_deg"], pred["dec_off_deg"]
    names = ["time_h", "point_index", "ra_off_deg", "dec_off_deg"]
    arrays = [t, order, ra, dec]
    corr = {a: {b: _corr(x, y) for b, y in zip(names, arrays)} for a, x in zip(names, arrays)}
    vif = {}
    core = {"time_h": t, "ra_off_deg": ra, "dec_off_deg": dec}
    for name, x in core.items():
        others = np.column_stack([np.ones_like(x)] + [v for k, v in core.items() if k != name])
        if np.std(x) == 0:
            vif[name] = float("nan")
            continue
        fit = ols(others, x)
        vif[name] = float(1.0 / max(1.0 - fit["r2"], 1e-12)) if np.isfinite(fit["r2"]) else float("nan")
    Z = np.column_stack([(v - v.mean()) / v.std() if v.std() > 0 else np.zeros_like(v) for v in core.values()])
    cond = float(np.linalg.cond(Z)) if Z.shape[0] > Z.shape[1] else float("nan")
    pairs = [abs(corr["time_h"]["ra_off_deg"]), abs(corr["time_h"]["dec_off_deg"])]
    max_corr = float(np.nanmax(pairs)) if np.any(np.isfinite(pairs)) else float("nan")
    confounded = bool(np.isfinite(max_corr) and max_corr >= config.confounding_corr_threshold) or any(
        np.isfinite(v) and v >= 5.0 for v in vif.values())
    return {"predictors": names, "correlation_matrix": corr, "vif": vif, "standardized_condition_number": cond,
            "max_abs_corr_time_vs_sky": max_corr, "corr_threshold": config.confounding_corr_threshold,
            "time_span_hours": float(t.max() - t.min()), "confounded_time_sky": confounded,
            "note": "time, point order, RA and Dec are entangled by the mosaic scan pattern; a regression cannot separate "
                    "them when confounded_time_sky is true"}


def models_block(y: np.ndarray, pred: dict) -> dict:
    n = y.shape[0]
    one = np.ones(n)
    t, order, ra, dec = pred["time_h"], pred["point_index"], pred["ra_off_deg"], pred["dec_off_deg"]
    designs = {"null": (["intercept"], np.column_stack([one])),
               "time": (["intercept", "time_h"], np.column_stack([one, t])),
               "point_order": (["intercept", "point_index"], np.column_stack([one, order])),
               "sky": (["intercept", "ra_off_deg", "dec_off_deg"], np.column_stack([one, ra, dec])),
               "time_plus_sky": (["intercept", "time_h", "ra_off_deg", "dec_off_deg"], np.column_stack([one, t, ra, dec]))}
    out = {}
    for name, (terms, X) in designs.items():
        if X.shape[1] >= n:
            out[name] = {"terms": terms, "status": "INSUFFICIENT_POINTS"}
            continue
        fit = ols(X, y)
        fit["terms"] = terms
        fit["status"] = "OK"
        out[name] = fit
    if out["time"]["status"] == "OK" and out["sky"]["status"] == "OK" and out["time_plus_sky"]["status"] == "OK":
        rt, rs, rb = out["time"]["rss"], out["sky"]["rss"], out["time_plus_sky"]["rss"]
        out["partial_r2"] = {"sky_given_time": float((rt - rb) / rt) if rt > 0 else float("nan"),
                             "time_given_sky": float((rs - rb) / rs) if rs > 0 else float("nan")}
        out["delta_aicc_vs_best"] = {k: float(v["aicc"]) for k, v in out.items() if isinstance(v, dict) and v.get("status") == "OK"}
    return out


def _series_stats(name, values, pred_arrays):
    v = np.asarray(values, dtype=float)
    ok = np.isfinite(v)
    if ok.sum() < 3:
        return {"name": name, "status": "INSUFFICIENT_POINTS", "n": int(ok.sum())}
    t = pred_arrays["time_h"][ok]
    fit = ols(np.column_stack([np.ones(ok.sum()), t]), v[ok]) if np.std(t) > 0 else None
    return {"name": name, "status": "OK", "n": int(ok.sum()), "mean": float(v[ok].mean()), "std": float(v[ok].std(ddof=1)),
            "first": float(v[ok][0]), "last": float(v[ok][-1]),
            "slope_per_hour": fit["coefficients"][1] if fit else float("nan"),
            "slope_se": fit["standard_errors"][1] if fit else float("nan"),
            "slope_z": (fit["coefficients"][1] / fit["standard_errors"][1]) if fit and fit["standard_errors"][1] > 0 else float("nan"),
            "corr_time": _corr(v[ok], t), "corr_ra": _corr(v[ok], pred_arrays["ra_off_deg"][ok]),
            "corr_dec": _corr(v[ok], pred_arrays["dec_off_deg"][ok])}


def analyze(inp, measurements, config: ForensicsConfig) -> dict:
    """Everything derived from the per-point measurements. Returns plain dicts (JSON-ready after sanitize)."""
    pred = predictors(inp.points, measurements, config)
    n_used = len(pred["used"])
    ok_all = [m for m in measurements if m.status == "OK"]
    result = {"n_points_input": len(inp.points), "n_points_measured": n_used,
              "status_counts": {s: sum(1 for m in measurements if m.status == s) for s in sorted({m.status for m in measurements})}}
    if n_used < config.min_usable_points:
        result["status"] = "INSUFFICIENT_EVIDENCE"
        result["reason"] = (f"only {n_used} of {len(inp.points)} points carry a measurable feature above the detection gate "
                             f"(< min_usable_points={config.min_usable_points}); per-point status: {result['status_counts']}")
        result["evidence"] = [{"label": "UNRESOLVED", "statement": result["reason"] + "; no campaign-level statistic is claimed",
                               "basis": "n_points_measured"}]
        return result
    result["status"] = "OK"
    used = pred["used"]
    ms = [m for _, m in used]
    lsrk = np.array([m.centroid_lsrk_m_s for m in ms])
    freq = np.array([m.centroid_freq_hz for m in ms])
    chan = np.array([m.centroid_channel for m in ms])
    sig_ch = np.array([m.centroid_channel_sigma for m in ms])
    ps = [p for p, _ in used]
    dv_ref = float(np.median([abs(np.median(np.diff(p.velocity_lsrk_m_s))) for p in ps]))
    df_ref = float(np.median([abs(np.median(np.diff(p.frequency_hz))) for p in ps]))
    result["axes"] = {"channel_width_m_s": dv_ref, "channel_width_hz": df_ref,
                      "time_span_hours": float(pred["time_h"].max()), "n_time_distinct": int(np.unique(pred["time_h"]).size)}

    # ---- FRAME COHERENCE (core output): same centroids expressed in each frame
    frames = {"lsrk_velocity_m_s": (lsrk, dv_ref), "topocentric_frequency_hz": (freq, df_ref), "channel_bin": (chan, 1.0)}
    coherence = {}
    for name, (arr, unit_per_channel) in frames.items():
        fit = ols(np.column_stack([np.ones(n_used), pred["time_h"]]), arr) if np.std(pred["time_h"]) > 0 else None
        mad = float(np.median(np.abs(arr - np.median(arr))))
        coherence[name] = {"n": n_used, "mean": float(arr.mean()), "rms": float(arr.std(ddof=1)),
                           "robust_rms": float(1.4826 * mad), "range": float(arr.max() - arr.min()),
                           "rms_channels": float(arr.std(ddof=1) / unit_per_channel),
                           "range_channels": float((arr.max() - arr.min()) / unit_per_channel),
                           "detrended_time_rms_channels": float(fit["residual_rms"] / unit_per_channel) if fit else float("nan")}
    med_sig = float(np.nanmedian(sig_ch)) if np.any(np.isfinite(sig_ch)) else float("nan")
    window_bins = float(np.median([m.n_window_bins for m in ms]))
    result["detection"] = {"n_points": len(measurements), "n_detected": n_used, "fraction_detected": n_used / max(len(measurements), 1),
                           "min_snr_formal": config.min_snr_formal,
                           "formal_snr_median": float(np.nanmedian([m.snr_formal for m in ms])),
                           "fwhm_channels_median": float(np.nanmedian([m.fwhm_channels for m in ms]))}
    result["frame_coherence"] = {"frames": coherence, "median_formal_centroid_sigma_channels": med_sig,
                                 "uniform_random_position_rms_channels": window_bins / np.sqrt(12.0),
                                 "note": "channel_bin and topocentric_frequency_hz carry the same information (one common "
                                         "frequency axis); LSRK adds the per-point LSRK shift"}

    # ---- CONFOUNDING + MODELS (per frame)
    conf = confounding_block(pred, config)
    result["confounding"] = conf
    models = {}
    for name, arr in (("lsrk_velocity_m_s", lsrk), ("topocentric_frequency_hz", freq)):
        models[name] = models_block(arr, pred)
    result["models"] = models
    result["models_note"] = "diagnostics, not proof of causality; see confounding"

    # ---- DRIFT from the time-only and point-order fits
    drift = {}
    span = float(pred["time_h"].max())
    for label, arr, unit in (("lsrk_velocity_m_s", lsrk, "m_s"), ("topocentric_frequency_hz", freq, "hz"), ("channel_bin", chan, "channels")):
        blk = {}
        if np.std(pred["time_h"]) > 0:
            f = ols(np.column_stack([np.ones(n_used), pred["time_h"]]), arr)
            slope, se = f["coefficients"][1], f["standard_errors"][1]
            blk.update({f"{unit}_per_hour": slope, "slope_se": se, "slope_z": (slope / se) if se > 0 else float("nan"),
                        "r2": f["r2"], "total_over_campaign": slope * span})
        fo = ols(np.column_stack([np.ones(n_used), pred["point_index"]]), arr)
        blk.update({f"{unit}_per_point": fo["coefficients"][1], "per_point_se": fo["standard_errors"][1], "per_point_r2": fo["r2"]})
        drift[label] = blk
    result["drift"] = drift

    # ---- amplitude / width / area evolution
    result["evolution"] = {
        "peak_height": _series_stats("peak_height", [m.peak_height for m in ms], pred),
        "fwhm_channels": _series_stats("fwhm_channels", [m.fwhm_channels for m in ms], pred),
        "area_relative_m_s": _series_stats("area_relative_m_s", [m.area_relative_m_s for m in ms], pred),
        "snr_formal": _series_stats("snr_formal", [m.snr_formal for m in ms], pred),
        "mask_fraction": _series_stats("mask_fraction", [m.mask_fraction for m in ms], pred)}

    # ---- separate, dimensionally honest metrics (no composite "astronomy score")
    sky_r2 = models["lsrk_velocity_m_s"]["sky"].get("r2", float("nan")) if models["lsrk_velocity_m_s"]["sky"]["status"] == "OK" else float("nan")
    result["metrics"] = {
        "LSRK_stability_rms_channels": coherence["lsrk_velocity_m_s"]["rms_channels"],
        "frequency_stability_rms_channels": coherence["topocentric_frequency_hz"]["rms_channels"],
        "spatial_coherence_r2_sky_only_lsrk": sky_r2,
        "time_drift_total_channels": drift["channel_bin"].get("channels_per_hour", float("nan")) * span,
        "time_drift_slope_z_frequency": drift["topocentric_frequency_hz"].get("slope_z", float("nan")),
        "mask_overlap_median_fraction": float(np.median([m.mask_fraction for m in ms])),
        "formal_snr_median": float(np.nanmedian([m.snr_formal for m in ms])),
        "median_formal_centroid_sigma_channels": med_sig}
    result["evidence"] = evidence_statements(result, config)
    return result


def evidence_statements(res: dict, config: ForensicsConfig) -> list:
    """Rule-based, deliberately conservative statements. Every one carries the label and the numbers it rests on."""
    ev = []
    fc = res["frame_coherence"]["frames"]
    r_l, r_f = fc["lsrk_velocity_m_s"]["rms_channels"], fc["topocentric_frequency_hz"]["rms_channels"]
    med_sig = res["frame_coherence"]["median_formal_centroid_sigma_channels"]
    floor = max(3.0 * med_sig, 0.5) if np.isfinite(med_sig) else 0.5
    ev.append({"label": "MEASURED", "basis": "frame_coherence",
               "statement": f"centroid scatter (RMS, channel widths): LSRK velocity {r_l:.2f}, topocentric frequency {r_f:.2f}, "
                            f"over {res['n_points_measured']} points; median formal centroid sigma {med_sig:.3f} channels"})
    ref = res["frame_coherence"]["uniform_random_position_rms_channels"]
    det = res["detection"]
    ev.append({"label": "MEASURED", "basis": "detection",
               "statement": f"{det['n_detected']} of {det['n_points']} points exceed the formal-SNR gate of {det['min_snr_formal']:g} "
                            f"(median formal SNR {det['formal_snr_median']:.1f}, median FWHM {det['fwhm_channels_median']:.1f} channels)"})
    ev.append({"label": "MEASURED", "basis": "frame_coherence",
               "statement": f"centroid scatter relative to positions drawn uniformly at random inside the window ({ref:.1f} ch RMS): "
                            f"LSRK {r_l / ref:.2f}, frequency {r_f / ref:.2f}"})
    if min(r_l, r_f) / ref >= 0.8:
        ev.append({"label": "UNRESOLVED", "basis": "frame_coherence",
                   "statement": "the centroid scatter is comparable to that of random positions inside the window: the measured "
                                "'centroids' may not localise one persistent feature"})
    thr = config.frame_ratio_threshold
    if r_l <= floor and r_f <= floor:
        ev.append({"label": "UNRESOLVED", "basis": "frame_coherence",
                   "statement": f"the centroid is stable in both frames within {floor:.2f} channels (3x formal sigma or 0.5 ch)"})
    elif r_f > 0 and r_l / r_f >= thr:
        ev.append({"label": "CONSISTENT WITH", "basis": "frame_coherence",
                   "statement": f"a feature that is more stationary in topocentric frequency than in LSRK velocity "
                                f"(LSRK scatter is {r_l / r_f:.1f}x the frequency scatter)"})
    elif r_l > 0 and r_f / r_l >= thr:
        ev.append({"label": "CONSISTENT WITH", "basis": "frame_coherence",
                   "statement": f"a feature that is more stationary in LSRK velocity than in topocentric frequency "
                                f"(frequency scatter is {r_f / r_l:.1f}x the LSRK scatter)"})
    else:
        ev.append({"label": "INCONSISTENT WITH", "basis": "frame_coherence",
                   "statement": f"a feature stationary in either frame: the centroid moves by comparable amounts in LSRK velocity "
                                f"({r_l:.2f} ch) and topocentric frequency ({r_f:.2f} ch), both above {floor:.2f} ch"})
    drift = res["drift"]["topocentric_frequency_hz"]
    if "hz_per_hour" in drift:
        ev.append({"label": "MEASURED", "basis": "drift",
                   "statement": f"linear drift in topocentric frequency {drift['hz_per_hour']:.4g} Hz/hour "
                                f"(+/-{drift['slope_se']:.2g}, z={drift['slope_z']:.1f}, R2={drift['r2']:.3f}) over "
                                f"{res['axes']['time_span_hours'] * 60:.1f} min; per point {drift['hz_per_point']:.4g} Hz"})
    conf = res["confounding"]
    ev.append({"label": "OBSERVED", "basis": "confounding",
               "statement": f"corr(time, RA offset)={conf['correlation_matrix']['time_h']['ra_off_deg']:.2f}, "
                            f"corr(time, Dec offset)={conf['correlation_matrix']['time_h']['dec_off_deg']:.2f}, "
                            f"corr(time, point index)={conf['correlation_matrix']['time_h']['point_index']:.2f}; "
                            f"VIF time/RA/Dec = {conf['vif']['time_h']:.1f}/{conf['vif']['ra_off_deg']:.1f}/{conf['vif']['dec_off_deg']:.1f}"})
    mods = res["models"]["lsrk_velocity_m_s"]
    modf = res["models"]["topocentric_frequency_hz"]
    for tag, mm in (("LSRK velocity", mods), ("topocentric frequency", modf)):
        if all(mm[k]["status"] == "OK" for k in ("time", "sky", "time_plus_sky")):
            ev.append({"label": "MEASURED", "basis": "models",
                       "statement": f"{tag}: R2 time-only {mm['time']['r2']:.3f}, sky-only {mm['sky']['r2']:.3f}, "
                                    f"time+sky {mm['time_plus_sky']['r2']:.3f}"})
    if conf["confounded_time_sky"]:
        ev.append({"label": "UNRESOLVED", "basis": "confounding",
                   "statement": "capture time and sky position are confounded in this campaign (|r| >= "
                                f"{conf['corr_threshold']} or VIF >= 5): the regressions cannot separate a temporal from a spatial "
                                "dependence, so neither is attributed"})
    else:
        z = drift.get("slope_z", float("nan"))
        t_r2 = modf["time"].get("r2", float("nan")) if modf["time"]["status"] == "OK" else float("nan")
        s_r2 = mods["sky"].get("r2", float("nan")) if mods["sky"]["status"] == "OK" else float("nan")
        if np.isfinite(z) and abs(z) >= 3 and np.isfinite(t_r2) and t_r2 >= 0.7:
            ev.append({"label": "CONSISTENT WITH", "basis": "models",
                       "statement": f"a dependence on capture time in the topocentric-frequency centroid (z={z:.1f}, R2={t_r2:.2f}) "
                                    "that is not shadowed by sky position in this campaign"})
        if np.isfinite(s_r2) and s_r2 >= 0.7 and not (np.isfinite(t_r2) and t_r2 >= 0.7):
            ev.append({"label": "CONSISTENT WITH", "basis": "models",
                       "statement": f"a dependence of the LSRK centroid on sky position (R2 sky-only {s_r2:.2f}) rather than on time"})
    mf = res["metrics"]["mask_overlap_median_fraction"]
    ev.append({"label": "OBSERVED", "basis": "masks",
               "statement": f"median fraction of window bins carrying a non-GOOD Level 1 mask: {mf:.3f}"})
    return ev
