"""Forensics sessions: orchestration, isolated persistence, manifest, validation and comparison.

Outputs live in their own directory (default data/science_forensics/CAMPAIGN/FORENSICS-...): forensics NEVER writes inside a REDUCE
or SCIENCE session, never opens RAW and never touches the network. A session is immutable once created (collision -> error).
"""
from __future__ import annotations

import csv
import hashlib
import json
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from science_forensics import FORENSICS_PIPELINE_VERSION, FORENSICS_SCHEMA_VERSION
from science_forensics.ingest import ForensicsInputError, load_forensics_input, local_sidereal_hours
from science_forensics.measure import MASK_FLAGS_REPORTED, measure_point, window_channels
from science_forensics.models import FeatureCandidate, ForensicsConfig, ForensicsInput, sanitize
from science_forensics.negative import (cumulative_integral, median_spectrum, negative_map_audit)
from science_forensics.statistics import analyze

NUMERIC_FILES = ("feature_track.csv", "feature_summary.json", "frame_coherence.json", "models.json", "negative_map_audit.json",
                 "median_spectrum.csv", "cumulative_integral.csv")
TRACK_COLUMNS = ("point_index", "timestamp_utc", "t_minutes", "t_hours", "lst_hours", "ra_deg", "dec_deg", "l_deg", "b_deg",
                 "lsrk_shift_m_s", "reduce_quality_state", "receiver", "rfi_ref_available", "status", "flags", "method_used",
                 "centroid_channel", "centroid_channel_sigma", "centroid_freq_hz", "centroid_freq_sigma_hz", "centroid_lsrk_m_s",
                 "centroid_lsrk_sigma_m_s", "centroid_channel_gaussian", "centroid_channel_halfmax", "centroid_channel_peak",
                 "peak_height", "peak_raw_value", "area_relative_m_s", "area_relative_hz", "fwhm_channels", "fwhm_hz", "fwhm_m_s",
                 "sigma_formal", "snr_formal", "fit_redchi2", "n_window_bins", "n_usable_bins", "mask_fraction",
                 *[f"mask_{n}" for n in MASK_FLAGS_REPORTED], "temperature", "gain", "source_h5_sha256")


def _reject_unsafe(name: str, field: str):
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"unsafe {field} for forensics storage: {name!r}")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _git_commit() -> Optional[str]:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(sanitize(obj), indent=1, sort_keys=True, allow_nan=False) + "\n")


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, (float, np.floating)):
        return repr(float(v)) if np.isfinite(v) else ""
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    return str(v)


def build_track(inp: ForensicsInput, measurements, config: ForensicsConfig) -> list:
    t0 = min(p.t_mid_s for p in inp.points)
    lst = None
    if config.site_longitude_deg is not None:
        lst = local_sidereal_hours(np.array([p.t_mid_s for p in inp.points]), config.site_longitude_deg)
    rows = []
    for i, (p, m) in enumerate(zip(inp.points, measurements)):
        r = {"point_index": p.point_index, "timestamp_utc": p.timestamp_utc, "t_minutes": (p.t_mid_s - t0) / 60.0,
             "t_hours": (p.t_mid_s - t0) / 3600.0, "lst_hours": None if lst is None else float(lst[i]), "ra_deg": p.ra_deg,
             "dec_deg": p.dec_deg, "l_deg": p.l_deg, "b_deg": p.b_deg, "lsrk_shift_m_s": p.lsrk_shift_m_s,
             "reduce_quality_state": p.quality_state, "receiver": p.receiver, "rfi_ref_available": p.rfi_ref_available,
             "temperature": None, "gain": None, "source_h5_sha256": p.source_h5_sha256, "flags": ";".join(m.flags)}
        d = m.to_dict()
        for k in ("status", "method_used", "centroid_channel", "centroid_channel_sigma", "centroid_freq_hz", "centroid_freq_sigma_hz",
                  "centroid_lsrk_m_s", "centroid_lsrk_sigma_m_s", "centroid_channel_gaussian", "centroid_channel_halfmax",
                  "centroid_channel_peak", "peak_height", "peak_raw_value", "area_relative_m_s", "area_relative_hz", "fwhm_channels",
                  "fwhm_hz", "fwhm_m_s", "sigma_formal", "snr_formal", "fit_redchi2", "n_window_bins", "n_usable_bins",
                  "mask_fraction"):
            r[k] = d[k]
        for n in MASK_FLAGS_REPORTED:
            r[f"mask_{n}"] = m.mask_flag_fractions.get(n)
        rows.append(r)
    return rows


def write_track_csv(path: Path, rows: list) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(TRACK_COLUMNS)
        for r in rows:
            w.writerow([_fmt(r.get(c)) for c in TRACK_COLUMNS])


def _candidate(inp, measurements, config, analysis) -> FeatureCandidate:
    ok = [m for m in measurements if m.status == "OK" and np.isfinite(m.centroid_channel)]
    med = (lambda vals: float(np.median(vals)) if vals else None)
    status = "UNCLASSIFIED" if analysis.get("status") == "OK" else "INSUFFICIENT_EVIDENCE"
    return FeatureCandidate(
        id=config.feature_id, velocity_lsrk_center=med([m.centroid_lsrk_m_s for m in ok]),
        velocity_width=med([m.fwhm_m_s for m in ok if np.isfinite(m.fwhm_m_s)]),
        frequency_equivalent=med([m.centroid_freq_hz for m in ok]),
        search_window={"frame": config.window_frame, "center": config.window_center, "half_width": config.window_half_width,
                       "unit": {"lsrk_velocity": "m/s", "frequency": "Hz (topocentric)", "channel": "channel index"}[config.window_frame]},
        source="operator-specified window on REDUCE Level 1 point spectra", detection_method=config.detection_method, status=status)


def plan_forensics(reduce_session_dir, config: ForensicsConfig, science_session_dir=None) -> dict:
    """No writes. What would be analysed and why it might be blocked."""
    blocked = []
    try:
        inp = load_forensics_input(reduce_session_dir)
    except ForensicsInputError as error:
        return {"blocked": True, "blocked_reasons": [str(error)], "input_reduce_session": str(reduce_session_dir)}
    counts = [int(window_channels(p, config).size) for p in inp.points]
    usable_points = sum(1 for c in counts if c >= config.min_usable_bins)
    if usable_points < config.min_usable_points:
        blocked.append(f"only {usable_points} points have >= {config.min_usable_bins} channels inside the window "
                       f"(need {config.min_usable_points})")
    if usable_points == 0:
        blocked.append("the search window does not overlap the spectrum of any point")
    if science_session_dir is not None:
        try:
            m = json.loads((Path(science_session_dir) / "manifest.json").read_text())
            if m.get("input_reduce_session_id") != inp.reduce_session_id:
                blocked.append("science session was built from a different REDUCE session")
        except (OSError, ValueError) as error:
            blocked.append(f"science session unreadable: {error}")
    times = np.array([p.t_mid_s for p in inp.points])
    return {"blocked": bool(blocked), "blocked_reasons": blocked, "input_reduce_session": str(reduce_session_dir),
            "campaign_id": inp.campaign_id, "reduce_session_id": inp.reduce_session_id, "reduce_session_status": inp.reduce_session_status,
            "n_points_available": len(inp.points), "n_points_excluded_at_ingest": len(inp.exclusions),
            "candidate_window": config.to_dict() | {"unit": {"lsrk_velocity": "m/s", "frequency": "Hz", "channel": "channel"}[config.window_frame]},
            "window_channels_per_point": {"min": min(counts), "median": float(np.median(counts)), "max": max(counts)},
            "points_with_enough_window_channels": usable_points,
            "spectral_frames_available": {"lsrk_velocity": True, "topocentric_frequency": True, "channel_bin": True,
                                          "lsrk_relation": "v_lsrk = c(1 - f/f_rest) + shift verified for every point"},
            "time_span_minutes": float((times.max() - times.min()) / 60.0), "sky_coordinates": True,
            "sidereal_time": "available" if config.site_longitude_deg is not None else "unavailable (no --site-from-observer-config)",
            "metadata_unavailable_in_level1": inp.unavailable,
            "science_session": str(science_session_dir) if science_session_dir else None,
            "expected_outputs": ["manifest.json", "config.json", "provenance.json", *NUMERIC_FILES, "centroid_vs_time.png",
                                 "centroid_vs_point.png", "sky_position_diagnostic.png", "point_velocity_waterfall.png",
                                 "point_frequency_waterfall.png", "negative_map_windows.png", "median_spectrum.png", "summary.md"]}


def summary_markdown(manifest, analysis, audit, candidate) -> str:
    L = []
    L.append(f"# Feature forensics: {candidate['id']} ({manifest['campaign_id']})\n")
    L.append("Diagnostics only. Every statement below is labelled OBSERVED / MEASURED / CONSISTENT WITH / INCONSISTENT WITH / "
             "UNRESOLVED; none names the physical nature of the feature.\n")
    L.append("## Input\n")
    L.append(f"- REDUCE session `{manifest['input_reduce_session_id']}` (status {manifest['input_reduce_session_status']}), "
             f"manifest sha256 `{manifest['input_reduce_manifest_sha256'][:16]}...`")
    L.append(f"- Points: {manifest['n_points_input']} ingested, {manifest['n_points_measured']} measured; window "
             f"`{candidate['search_window']}`; config hash `{manifest['config_hash'][:16]}...`")
    if manifest.get("input_science_session"):
        L.append(f"- SCIENCE session read (read-only): `{manifest['input_science_session'].get('science_session_id')}`")
    L.append(f"- Candidate status: **{candidate['status']}**\n")
    if analysis.get("status") == "OK":
        fc = analysis["frame_coherence"]["frames"]
        L.append("## Frame coherence (centroid scatter of the same feature in each frame)\n")
        L.append("| frame | RMS | RMS (channel widths) | range (channels) | RMS after linear time fit (channels) |")
        L.append("|---|---|---|---|---|")
        for name, v in fc.items():
            L.append(f"| {name} | {v['rms']:.6g} | {v['rms_channels']:.3f} | {v['range_channels']:.2f} | {v['detrended_time_rms_channels']:.3f} |")
        L.append("")
        L.append("## Evidence\n")
        for e in analysis["evidence"]:
            L.append(f"- **{e['label']}**: {e['statement']}")
        L.append("")
        c = analysis["confounding"]
        L.append(f"## Confounding\n\nmax |corr(time, sky offset)| = {c['max_abs_corr_time_vs_sky']:.2f}; confounded_time_sky = "
                 f"**{c['confounded_time_sky']}**. Time, point order and sky position are entangled by the scan pattern; the "
                 "regressions are diagnostics, not causal inference.\n")
    else:
        L.append(f"## Result\n\n**UNRESOLVED**: {analysis.get('reason')}\n")
    if audit:
        w = audit["windows"]
        L.append("## Negative integrated map audit (Level 1, diagnostic)\n")
        for name in ("candidate", "control_left", "control_right", "integration_window"):
            if name in w:
                s = w[name]
                L.append(f"- MEASURED {name} {s['lsrk_range_m_s']}: median per-point integral "
                         f"{s['integral_relative_m_s']['median']:.4g} relative*m/s, fraction of points negative "
                         f"{s['integral_relative_m_s']['fraction_negative']:.2f}, median coverage {s['coverage']['median']:.3f}")
        L.append(f"- MEASURED integration window excluding the candidate window: median "
                 f"{audit['integration_window_excluding_candidate_relative_m_s']['median']:.4g} relative*m/s")
        L.append("")
    L.append("## Unavailable in Level 1 (not inferred, RAW never opened)\n")
    for k, v in manifest["metadata_unavailable_in_level1"].items():
        L.append(f"- {k}: {v}")
    return "\n".join(L) + "\n"


def run_forensics(reduce_session_dir, config: ForensicsConfig, *, output_root, science_session_dir=None,
                  session_id: Optional[str] = None) -> dict:
    from science_forensics import plots
    t0 = time.monotonic()
    inp = load_forensics_input(reduce_session_dir)
    _reject_unsafe(inp.campaign_id, "campaign_id")
    sid = session_id or f"FORENSICS-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}"
    _reject_unsafe(sid, "session_id")
    root = Path(output_root).resolve()
    out = (root / inp.campaign_id / sid).resolve()
    if root not in out.parents:
        raise ValueError(f"resolved forensics path escapes output_root: {out}")
    for protected in (Path(reduce_session_dir).resolve(), Path(science_session_dir).resolve() if science_session_dir else None):
        if protected is not None and (out == protected or protected in out.parents):
            raise ValueError("forensics never writes inside a REDUCE/SCIENCE session directory")
    if out.exists():
        raise FileExistsError(f"forensics session already exists (immutable): {out}")
    out.mkdir(parents=True)

    science_ref = None
    if science_session_dir is not None:
        sm = json.loads((Path(science_session_dir) / "manifest.json").read_text())
        science_ref = {"dir": str(science_session_dir), "science_session_id": sm.get("science_session_id"),
                       "input_reduce_session_id": sm.get("input_reduce_session_id"),
                       "manifest_sha256": _sha256_file(Path(science_session_dir) / "manifest.json"),
                       "config_hash": sm.get("config_hash")}
    base = {"forensics_schema_version": FORENSICS_SCHEMA_VERSION, "forensics_pipeline_version": FORENSICS_PIPELINE_VERSION,
            "forensics_session_id": sid, "campaign_id": inp.campaign_id, "input_reduce_session_id": inp.reduce_session_id,
            "input_reduce_session_dir": str(reduce_session_dir), "input_reduce_session_status": inp.reduce_session_status,
            "input_reduce_manifest_sha256": inp.reduce_manifest_sha256, "input_science_session": science_ref,
            "config_hash": config.config_hash(), "metadata_unavailable_in_level1": inp.unavailable,
            "created_utc": datetime.now(timezone.utc).isoformat()}
    _write_json(out / "config.json", config.to_dict())
    _write_json(out / "manifest.json", {**base, "status": "RUNNING", "products": []})
    try:
        measurements = [measure_point(p, config) for p in inp.points]
        analysis = analyze(inp, measurements, config)
        track = build_track(inp, measurements, config)
        candidate = _candidate(inp, measurements, config, analysis)
        audit = negative_map_audit(inp, config, science_session_dir)
        cum = cumulative_integral(inp, config.integration_window_min_m_s, config.integration_window_max_m_s)
        ms = median_spectrum(inp, config)

        write_track_csv(out / "feature_track.csv", track)
        _write_json(out / "feature_summary.json", {"candidate": candidate.to_dict(), "analysis_status": analysis.get("status"),
                    "metrics": analysis.get("metrics"), "detection": analysis.get("detection"), "evidence": analysis.get("evidence"), "drift": analysis.get("drift"),
                    "evolution": analysis.get("evolution"), "axes": analysis.get("axes"), "status_counts": analysis.get("status_counts"),
                    "reason": analysis.get("reason"), "per_point_flags": {str(m.point_index): m.flags for m in measurements if m.flags},
                    "empirical_noise": audit.get("empirical_noise"), "limitations": [
                        "regressions are diagnostics, not causal inference", "Level 1 sigma is statistical only",
                        "temperature, gain and RFI_REF association are not available in Level 1"]})
        _write_json(out / "frame_coherence.json", analysis.get("frame_coherence", {}))
        _write_json(out / "models.json", {"models": analysis.get("models"), "confounding": analysis.get("confounding"),
                                          "note": analysis.get("models_note")})
        _write_json(out / "negative_map_audit.json", audit)
        if ms.get("status") == "OK":
            with (out / "median_spectrum.csv").open("w", newline="") as fh:
                w = csv.writer(fh, lineterminator="\n")
                w.writerow(["channel", "frequency_hz", "mean_lsrk_m_s", "median_relative_intensity", "robust_spread", "n_points"])
                for k in range(ms["median"].shape[0]):
                    w.writerow([k, _fmt(ms["frequency_hz"][k]), _fmt(ms["mean_lsrk_m_s"][k]), _fmt(ms["median"][k]),
                                _fmt(ms["robust_spread"][k]), int(ms["n_points"][k])])
        else:
            (out / "median_spectrum.csv").write_text("# unavailable: " + str(ms.get("reason")) + "\n")
        with (out / "cumulative_integral.csv").open("w", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            reps = list(cum["representative"])
            w.writerow(["lsrk_m_s", "median", "p16", "p84", *[f"point_{k}" for k in reps]])
            for k in range(cum["grid_lsrk_m_s"].shape[0]):
                w.writerow([_fmt(cum["grid_lsrk_m_s"][k]), _fmt(cum["median"][k]), _fmt(cum["p16"][k]), _fmt(cum["p84"][k]),
                            *[_fmt(cum["representative"][r][k]) for r in reps]])

        title = f"{inp.campaign_id} / {candidate.id} / {config.window_frame} {config.window_center:g} +/- {config.window_half_width:g}"
        if analysis.get("status") == "OK":
            plots.plot_centroid_vs(track, "t_minutes", "capture time (minutes since first point)", title, out / "centroid_vs_time.png")
            plots.plot_centroid_vs(track, "point_index", "point index (capture order)", title, out / "centroid_vs_point.png")
            plots.plot_sky(track, out / "sky_position_diagnostic.png", title)
        for frame, fname in (("lsrk", "point_velocity_waterfall.png"), ("frequency", "point_frequency_waterfall.png")):
            plots.plot_waterfall(inp.points, track, config, frame, "capture", out / fname)
        for mode in config.sort_modes:
            if mode == "capture":
                continue
            for frame in ("lsrk", "frequency"):
                plots.plot_waterfall(inp.points, track, config, frame, mode, out / f"waterfall_{frame}_sorted_{mode}.png")
        plots.plot_negative_map(audit, cum, out / "negative_map_windows.png", title)
        plots.plot_median_spectrum(ms, audit, out / "median_spectrum.png", title)

        prov = {"forensics_schema_version": FORENSICS_SCHEMA_VERSION, "git_commit": _git_commit(), "config": config.to_dict(),
                "config_hash": config.config_hash(), "input_reduce_manifest_sha256": inp.reduce_manifest_sha256,
                "input_science_session": science_ref, "software_versions": {"python": sys.version.split()[0], "numpy": np.__version__},
                "input_points": [{"point_index": p.point_index, "master_spectrum_h5_sha256": p.source_h5_sha256,
                                  "master_spectrum_json_sha256": p.source_json_sha256, "capture_ids": p.capture_ids,
                                  "receiver": p.receiver, "timestamp_utc": p.timestamp_utc} for p in inp.points],
                "ingest_exclusions": inp.exclusions, "hostname_recorded": False, "network_access": "none", "raw_access": "none"}
        _write_json(out / "provenance.json", prov)

        products = []
        for f in sorted(out.iterdir()):
            if f.name in ("manifest.json", "summary.md"):
                continue
            products.append({"id": f.name, "path": f.name, "kind": f.suffix.lstrip("."), "bytes": f.stat().st_size,
                             "sha256": _sha256_file(f), "numeric": f.name in NUMERIC_FILES})
        numeric = hashlib.sha256()
        for name in NUMERIC_FILES:
            numeric.update(name.encode())
            numeric.update((out / name).read_bytes())
        manifest = {**base, "status": "COMPLETED", "analysis_status": analysis.get("status"), "candidate": candidate.to_dict(),
                    "n_points_input": len(inp.points), "n_points_measured": analysis.get("n_points_measured", 0),
                    "products": products, "numeric_sha256": numeric.hexdigest(), "numeric_files": list(NUMERIC_FILES),
                    "runtime": {"seconds": time.monotonic() - t0, "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0}}
        (out / "summary.md").write_text(summary_markdown(manifest, analysis, audit, candidate.to_dict()))
        _write_json(out / "manifest.json", manifest)
    except BaseException as error:
        _write_json(out / "manifest.json", {**base, "status": "CANCELLED" if isinstance(error, KeyboardInterrupt) else "FAILED",
                                            "products": [], "error": f"{type(error).__name__}: {error}"})
        raise
    return {"session_dir": str(out), "status": "COMPLETED", "analysis_status": analysis.get("status"), "candidate": candidate.to_dict(),
            "analysis": analysis, "audit": audit, "track": track, "manifest": manifest}


def validate_forensics_session(session_dir) -> dict:
    d = Path(session_dir)
    problems = []
    for req in ("manifest.json", "config.json", "provenance.json"):
        if not (d / req).exists():
            problems.append(f"missing {req}")
    if problems:
        return {"ok": False, "problems": problems}
    try:
        m = json.loads((d / "manifest.json").read_text())
        cfg = json.loads((d / "config.json").read_text())
        prov = json.loads((d / "provenance.json").read_text())
    except ValueError as error:
        return {"ok": False, "problems": [f"unreadable JSON: {error}"]}
    if m.get("forensics_schema_version") != FORENSICS_SCHEMA_VERSION:
        return {"ok": False, "problems": [f"unknown forensics_schema_version {m.get('forensics_schema_version')!r}"]}
    if m.get("status") != "COMPLETED":
        problems.append(f"session status is {m.get('status')}")
    try:
        if ForensicsConfig(**{**cfg, "sort_modes": tuple(cfg.get("sort_modes", ()))}).config_hash() != m.get("config_hash"):
            problems.append("config.json does not hash to manifest.config_hash")
    except (TypeError, ValueError) as error:
        problems.append(f"config.json invalid: {error}")
    for k in ("input_reduce_manifest_sha256", "input_reduce_session_id", "candidate", "config_hash"):
        if not m.get(k):
            problems.append(f"manifest missing {k}")
    if not prov.get("input_points") or not all(p.get("master_spectrum_h5_sha256") for p in prov["input_points"]):
        problems.append("provenance lacks per-point input hashes")
    if prov.get("raw_access") != "none" or prov.get("network_access") != "none":
        problems.append("provenance does not declare raw_access/network_access none")
    for prod in m.get("products", []):
        p = d / prod["path"]
        if not p.is_file():
            problems.append(f"product {prod['id']} missing")
        elif _sha256_file(p) != prod["sha256"]:
            problems.append(f"product {prod['id']} sha256 mismatch")
    try:
        h = hashlib.sha256()
        for name in m.get("numeric_files", []):
            h.update(name.encode())
            h.update((d / name).read_bytes())
        if h.hexdigest() != m.get("numeric_sha256"):
            problems.append("numeric_sha256 mismatch")
    except OSError as error:
        problems.append(f"numeric files unreadable: {error}")
    for name in ("feature_summary.json", "frame_coherence.json", "models.json", "negative_map_audit.json"):
        try:
            json.loads((d / name).read_text(), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
        except (OSError, ValueError) as error:
            problems.append(f"{name} invalid or contains non-finite literal: {error}")
    try:
        rows = list(csv.DictReader((d / "feature_track.csv").open()))
        if len(rows) != m.get("n_points_input"):
            problems.append(f"feature_track rows {len(rows)} != n_points_input {m.get('n_points_input')}")
        if tuple(rows[0].keys()) != TRACK_COLUMNS:
            problems.append("feature_track.csv columns differ from schema")
    except (OSError, IndexError) as error:
        problems.append(f"feature_track.csv unreadable: {error}")
    if m.get("candidate", {}).get("status") not in ("UNCLASSIFIED", "INSUFFICIENT_EVIDENCE", "SKY_COHERENT", "TIME_COHERENT",
                                                    "INSTRUMENT_COHERENT", "RFI_SUSPECT"):
        problems.append("invalid candidate status")
    return {"ok": not problems, "problems": problems, "products_checked": len(m.get("products", []))}


def compare_forensics_sessions(a_dir, b_dir) -> dict:
    a, b = (json.loads((Path(x) / "manifest.json").read_text()) for x in (a_dir, b_dir))
    ca, cb = (json.loads((Path(x) / "config.json").read_text()) for x in (a_dir, b_dir))
    same_input = a["input_reduce_manifest_sha256"] == b["input_reduce_manifest_sha256"]
    same_config = a["config_hash"] == b["config_hash"]
    fa, fb = (json.loads((Path(x) / "frame_coherence.json").read_text()) for x in (a_dir, b_dir))
    metrics = {}
    for frame in sorted(set(fa.get("frames", {})) | set(fb.get("frames", {}))):
        metrics[frame] = {"rms_channels": [fa.get("frames", {}).get(frame, {}).get("rms_channels"), fb.get("frames", {}).get(frame, {}).get("rms_channels")]}
    if same_input and same_config:
        verdict = "EQUIVALENT" if a["numeric_sha256"] == b["numeric_sha256"] else "UNEXPECTED DIFFERENCE"
    else:
        verdict = "EQUIVALENT" if a["numeric_sha256"] == b["numeric_sha256"] else "EXPECTED DIFFERENCE"
    return {"verdict": verdict, "same_input": same_input, "same_config": same_config,
            "config_diff": {k: [ca.get(k), cb.get(k)] for k in sorted(set(ca) | set(cb)) if ca.get(k) != cb.get(k)},
            "campaigns": [a["campaign_id"], b["campaign_id"]], "numeric_sha256": [a["numeric_sha256"], b["numeric_sha256"]],
            "candidate": [a["candidate"], b["candidate"]], "frame_rms_channels": metrics}
