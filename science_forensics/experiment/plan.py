"""Machine-readable experiment plan (science_forensics_experiment.yaml) and the capture.py-compatible CSV emitter.

Nothing here runs hardware. The CSV follows the real mosaic.csv schema (same required fields capture.py checks), one row per
capture, `scan_order` = the planned order; repeated coordinates are distinct rows with distinct point_number / data_filename, which
is exactly what REDUCE (`point_number`/`data_filename` identity) and SCIENCE (`point_index`, capture sha256) key on."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from science_forensics.experiment import EXPERIMENT_SCHEMA_VERSION
from science_forensics.experiment import design as D

MOSAIC_COLUMNS = ["point_number", "point_id", "scan_order", "grid_row", "grid_col", "row", "column", "ra", "dec", "target_ra_hours",
                  "target_dec_degrees", "target_ra_hms", "target_dec_dms", "center_distance_deg", "nominal_spacing_deg", "capture_status",
                  "start_time", "end_time", "duration", "error_message", "data_filename", "session_name", "session_id",
                  "experiment_label", "experiment_visit"]


def _hms(hours: float) -> str:
    h = int(hours)
    m = int((hours - h) * 60)
    s = ((hours - h) * 60 - m) * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _dms(deg: float) -> str:
    sgn = "-" if deg < 0 else "+"
    d = abs(deg)
    dd = int(d)
    m = int((d - dd) * 60)
    s = ((d - dd) * 60 - m) * 60
    return f"{sgn}{dd:02d}:{m:02d}:{s:05.2f}"


def emit_capture_csv(path, caps: list, session_name: str, positions: dict, *, spacing_deg: float = 0.0) -> dict:
    """Write a capture.py plan. Returns {"path", "sha256", "n_rows", "labels"}."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    visit: dict = {}
    order = {lab: i for i, lab in enumerate(sorted(positions))}
    rows = []
    for c in caps:
        visit[c.label] = visit.get(c.label, 0) + 1
        ra_h = c.ra_deg / 15.0
        n = c.order
        rows.append({"point_number": n, "point_id": n, "scan_order": n, "grid_row": order[c.label], "grid_col": visit[c.label] - 1,
                     "row": order[c.label], "column": visit[c.label] - 1, "ra": f"{ra_h:.6f}", "dec": f"{c.dec_deg:.6f}",
                     "target_ra_hours": f"{ra_h:.6f}", "target_dec_degrees": f"{c.dec_deg:.6f}", "target_ra_hms": _hms(ra_h),
                     "target_dec_dms": _dms(c.dec_deg), "center_distance_deg": f"{D.angular_separation_deg(positions['A'], D.Position(c.label, c.ra_deg, c.dec_deg)):.6f}",
                     "nominal_spacing_deg": f"{spacing_deg:.10f}", "capture_status": "planned", "start_time": "", "end_time": "",
                     "duration": "", "error_message": "", "data_filename": f"{session_name}_{n:04d}.dat", "session_name": session_name,
                     "session_id": "", "experiment_label": c.label, "experiment_visit": visit[c.label]})
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=MOSAIC_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "n_rows": len(rows), "labels": [c.label for c in caps]}


def validate_capture_csv(path) -> dict:
    """The checks capture.py's own preflight applies to the plan (required fields, finite in-range coordinates, order >= 1,
    all rows 'planned') plus the identity checks REDUCE needs (unique point_number and data_filename)."""
    required = {"point_number", "scan_order", "target_ra_hours", "target_dec_degrees", "capture_status", "start_time", "end_time",
                "duration", "error_message", "data_filename", "session_name"}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        rows = list(reader)
    problems = []
    missing = sorted(required - fields)
    if missing:
        problems.append(f"missing required fields: {missing}")
    seen_p, seen_f, seen_o = set(), set(), set()
    for r in rows:
        try:
            ra, dec, order = float(r["target_ra_hours"]), float(r["target_dec_degrees"]), int(r["scan_order"])
            if not (np.isfinite(ra) and np.isfinite(dec) and 0 <= ra < 24 and -90 <= dec <= 90 and order >= 1):
                problems.append(f"row {r.get('point_number')}: coordinates/order out of range")
        except (TypeError, ValueError, KeyError):
            problems.append(f"row {r.get('point_number')}: unparsable coordinates/order")
            continue
        if r["capture_status"] != "planned":
            problems.append(f"row {r['point_number']}: status {r['capture_status']!r} is not 'planned'")
        for val, seen, what in ((r["point_number"], seen_p, "point_number"), (r["data_filename"], seen_f, "data_filename"), (order, seen_o, "scan_order")):
            if val in seen:
                problems.append(f"duplicate {what}: {val}")
            seen.add(val)
    return {"ok": not problems, "n_rows": len(rows), "problems": problems,
            "distinct_coordinates": len({(r["target_ra_hours"], r["target_dec_degrees"]) for r in rows})}


def instrument_from_resolved(path) -> dict:
    """Audit the REAL configuration of the feature campaign from its observation_resolved.json (never guessed)."""
    p = Path(path)
    raw = p.read_bytes()
    d = json.loads(raw)
    cap = d["requested"]["capture"]
    out = {"source": str(p), "source_sha256": hashlib.sha256(raw).hexdigest(), "campaign": d["observation_name"],
           "capture_seconds": cap["seconds"], "settle_seconds": cap["settle_seconds"], "sdr_center_frequency_hz": d["main"]["center_frequency_hz"],
           "sdr_sample_rate_hz": d["main"]["sample_rate"], "sdr_gain_db": d["main"]["gain_db"], "main_bias_tee": d["main"].get("bias_tee"),
           "input_topology": "antenna (orchestrator always passes --input-topology antenna)",
           "rfi_ref": {"enabled": d["rfi_ref"]["enabled"], "serial": d["rfi_ref"].get("serial"), "gain_db": d["rfi_ref"].get("gain_db"),
                       "bias_tee": d["rfi_ref"].get("bias_tee", False)},
           "min_altitude_deg": d["requested"]["grid"]["min_altitude_deg"], "fft_size": 8192,
           "software_versions": d.get("software_versions"), "calibration_profile": d["requested"]["quicklook"].get("calibration_profile_path")}
    return out


DEFAULT_INSTRUMENT = {  # audited 2026-09-20 from data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16/observation_resolved.json
    "source": "data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16/observation_resolved.json", "campaign": "ALMITA-WEB-SMALL-RUN-01",
    "capture_seconds": 10.0, "settle_seconds": 2.0, "sdr_center_frequency_hz": 1420405752, "sdr_sample_rate_hz": 2400000, "sdr_gain_db": 40.2,
    "main_bias_tee": True, "input_topology": "antenna (orchestrator always passes --input-topology antenna)",
    "rfi_ref": {"enabled": True, "serial": "00000002", "gain_db": 25.0, "bias_tee": False}, "min_altitude_deg": 10.0, "fft_size": 8192,
}


def capture_command(csv_path: str, instrument: dict, runtime_dir: str = "<RUNTIME_DIR>") -> list:
    cmd = ["python3", "capture.py", "--csv", csv_path, "--settle", str(instrument["settle_seconds"]), "--capture", str(instrument["capture_seconds"]),
           "--sdr-freq", str(instrument["sdr_center_frequency_hz"]), "--sdr-rate", str(instrument["sdr_sample_rate_hz"]),
           "--sdr-gain", str(instrument["sdr_gain_db"]), "--input-topology", "antenna", "--min-altitude", str(instrument["min_altitude_deg"])]
    r = instrument["rfi_ref"]
    if r["enabled"]:
        cmd += ["--rfi-ref-enabled", "--rfi-ref-gain-db", str(r["gain_db"]), "--rfi-ref-serial", str(r["serial"])]
        if r.get("bias_tee"):
            cmd.append("--rfi-ref-bias-t")
    return cmd


def position_rationale(positions: dict, expected: dict) -> dict:
    pa = positions["A"]
    seps = {f"{a}{b}": round(D.angular_separation_deg(positions[a], positions[b]), 2) for a, b in (("A", "B"), ("A", "C"), ("B", "C"))}
    moves = {f"{a}{b}": round(D.move_axis_deg(positions[a], positions[b]), 2) for a, b in (("A", "B"), ("A", "C"), ("B", "C"))}
    out = {"A": "centre of the feature-campaign grid (RA 11.83 h, Dec -33.449): the reference sky where the feature was seen",
           "selection_rule": ("B and C are chosen (deterministically, from 24 candidate directions x 2 distances around A) to MAXIMISE the smallest pairwise LSRK-shift "
                              "difference among A, B, C, subject to: every pair >= 4.5 deg apart, every move <= 7 deg (inside the timing evidence)"),
           "angular_separation_deg": seps, "move_size_axis_deg": moves,
           "lsrk_shift_difference_m_s": {k: v["delta_lsrk_shift_m_s"] for k, v in expected["pairwise"].items()},
           "lsrk_shift_difference_channels": {k: v.get("delta_lsrk_shift_channels") for k, v in expected["pairwise"].items()},
           "ha_side": "all positions and all times on ONE side of the meridian (capture.py re-blocks by hour-angle sign; a crossing = 64-85 s slews): check with `windows`",
           "beam_note": "pairs are > 3 beam widths apart if the (provisional, operator-provided) beam is <= 1.5 deg FWHM"}
    return out


def build_plan(*, positions: dict, expected: dict, designs: dict, diagnostics: dict, instrument: dict, timing: D.TimingModel,
               epoch_utc: str, old_reference: Optional[dict], power: Optional[dict], centroid_sigma_hz: dict, files: dict,
               readiness: dict) -> dict:
    rate_sky_null = expected["fixed_sky_expected_drift"]["A"]["sky_stationary_predicts_topocentric_drift_hz_per_hour"]
    feat_rate = -323_525.47                                       # measured on the 9-point campaign (Hz/h, topocentric)
    cadence = designs["B"]["median_cadence_s"]
    cad_a = designs["A"]["median_cadence_s"]
    reps = {f"sigma_{s:g}_hz": {"drift_3.2e5_hz_per_hour": D.captures_needed_for_rate(feat_rate, s, cad_a),
                                "sky_null_vs_zero_317_hz_per_hour": D.captures_needed_for_rate(rate_sky_null, s, cad_a)}
            for s in (30.5, 50.0, 100.0)}
    return {
        "schema": "science_forensics_experiment", "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": "SCIENCE-FORENSICS-EXP-HI148-01",
        "generated_by": "almita_science_forensics_experiment.py design",
        "design_epoch_utc": epoch_utc,
        "purpose": ("Break the time/sky confounding of the +148-153 km/s LSRK feature: measure the same sky at many times (Experiment A) and "
                    "different sky at nearly the same time (Experiment B, reference position A bracketing every B/C capture)."),
        "philosophy": "The next improvement is not a smarter regression. It is a smarter observation.",
        "executed_by_this_plan": False, "hardware_authorization": "none: plan only; execution is a manual operator action in the field",
        "instrument_config_identical_to_feature_campaign": instrument,
        "positions": {lab: {"ra_deg": p.ra_deg, "dec_deg": p.dec_deg, "ra_hours": p.ra_deg / 15.0} for lab, p in positions.items()},
        "position_rationale": position_rationale(positions, expected),
        "predicted_lsrk_shift": {"epoch_utc": epoch_utc, "shift_m_s": expected["lsrk_shift_m_s"], "pairwise": expected["pairwise"],
                                 "fixed_sky_expected_drift": expected["fixed_sky_expected_drift"], "source": "alignment_engine.hi.velocity.frame_shift_km_s (the helper REDUCE uses; 0.0 m/s difference on the 9 real points)"},
        "experiments": {
            "A": {"name": "A-REPEAT (fixed sky, repeated time)", "sequence": designs["A"]["sequence"], "n_captures": len(designs["A"]["sequence"]),
                  "csv": files["A"], "duration_min": designs["A"]["duration"], "diagnostics": diagnostics["A"],
                  "answers": "does the candidate drift in time at a FIXED sky position? (sky-null expectation: {:.0f} Hz/h; measured earlier {:.0f} Hz/h)".format(rate_sky_null, feat_rate)},
            "B": {"name": "B-ALTERNATE (rapid alternating sky, A bracketing)", "sequence": designs["B"]["sequence"], "n_captures": len(designs["B"]["sequence"]),
                  "csv": files["B"], "duration_min": designs["B"]["duration"], "diagnostics": diagnostics["B"],
                  "answers": "at (almost) the same time, does the candidate move between sky positions by the stationary-sky prediction, by zero, or by something else?"},
            "AB": {"name": "A then B in one continuous session (recommended)", "sequence_note": "20 x A, then the Experiment B sequence, then 20 x A (A before and after: time-balanced, and the A series spans the whole session)",
                   "n_captures": len(designs["AB"]["sequence"]), "csv": files["AB"], "duration_min": designs["AB"]["duration"], "diagnostics": diagnostics["AB"]}},
        "capture_length_and_repetitions": {
            "capture_seconds": instrument["capture_seconds"], "why_unchanged": "instrument configuration is kept identical to the feature campaign",
            "measured_snr_formal": {"median": 17.8, "min": 14.9, "max": 22.2, "source": "feature_summary.json of the 9-point real campaign"},
            "measured_centroid_sigma_hz": centroid_sigma_hz, "measured_drift_hz_per_hour": feat_rate,
            "min_captures_at_one_position_for_5_sigma": reps,
            "chosen": {"A": len(designs["A"]["sequence"]), "B": len(designs["B"]["sequence"])},
            "reading": "3-4 captures already prove a 3.2e5 Hz/h drift; ~38-84 captures are needed to tell the stationary-sky drift (-317 Hz/h) from zero at one position, which is why A has 60 captures and why B (position offsets of 5-11 kHz) is the decisive test."},
        "timing": {"model": timing.to_dict(), "quantiles": designs["timing_quantiles"],
                   "slew_note": "per-point non-capture overhead is 18-25 s nearly independent of move size (<=7.5 deg): alternating sky costs ~+4 s per capture versus repeating; hour-angle-crossing slews cost 64-85 s and are excluded by design"},
        "null_hypotheses": {
            "H0_time": {"statement": "the candidate does not drift with time beyond the stationary-sky prediction",
                        "rejected_when": "|excess drift|/SE >= 5 and total excess >= 2 channels over the span, OR stationary chi2/dof >= 4 with range >= 2 channels",
                        "effect_size": "excess drift (Hz/h) and total channels over the span, with SE"},
            "H0_sky": {"statement": "position offsets equal the parameter-free stationary-sky prediction (f0/c) x d(LSRK shift)",
                       "consistent_when": "chi2/dof < 4 over the bracketed B/C offsets", "effect_size": "measured minus predicted offset (Hz) with SE"},
            "H0_frequency": {"statement": "position offsets are zero (receiver / topocentric-frequency fixed)",
                             "consistent_when": "chi2/dof < 4 against zero", "effect_size": "offset (Hz) with SE"},
            "global_shift": "whole-spectrum cross-correlation shift (candidate and known spurs excluded) vs candidate motion: slope ~1 with R2>=0.8 -> the candidate moves with everything (TIME_GLOBAL); flat global with moving candidate -> TIME_CANDIDATE_ONLY"},
        "decision_labels": ["SKY_FIXED", "SKY_GRADIENT", "RECEIVER_FIXED", "TIME_GLOBAL", "TIME_CANDIDATE_ONLY", "MIXED_TIME_AND_SPATIAL",
                            "STATIONARY_UNDISCRIMINATED", "UNRESOLVED"],
        "analysis": {"tool": "python3 almita_science_forensics_experiment.py analyze", "matched_pair_tolerance_s": round(designs["B"]["pair_time_tolerance_s"], 1),
                     "tolerance_rule": "1.5 x median real cadence", "tracking_search_half_width_channels": 75, "fit_half_width_channels": 30,
                     "controls": "control lines from the median spectrum, tracked in every capture; known spurs/DC from REDUCE masks; candidate excluded from the global shift",
                     "diagnostic_only": ["difference spectra", "shift-and-difference"], "reduce_modified": False},
        "logging_requirements": {
            "rfi_ref": "capture.py --rfi-ref-enabled exactly as in the feature campaign; note the serial 00000002, gain 25 dB; confirm rfi_ref files exist for the session",
            "temperature": "keep the SDR and LNA DS18B20 sensors (28-082471f4e41b, 28-2c5acd1e64ff) logging; record the ambient temperature at start and end by hand",
            "clock": "record the Pi clock source and offset (chronyc tracking / timedatectl) at start and end; all timestamps are timezone-aware UTC; the OnStep clock drift finding is documented and not corrected here",
            "sdr_stream_start": "write down the time the SDR network stream is first opened relative to the first capture (warm-up transient is NOT assumed)",
            "pointing": "for each repeated visit note the mount-reported RA/Dec after GOTO (capture log) to quantify pointing repeatability",
            "reduce": "REDUCE V1 is not patched: per-capture rfi_ref association, temperature and gain remain in RAW metadata"},
        "expected_signatures": {"note": "recompute for the real start with: almita_science_forensics_experiment.py windows / design --epoch"},
        "old_campaign_reference": old_reference,
        "synthetic_power_test": power,
        "negative_background_followup": {
            "why": "integrated SCIENCE maps are strongly negative (REDUCE baseline offset); the fixed-position series doubles as the test",
            "checks": ["per-capture median level of the calibrated spectrum vs time (Experiment A, fixed sky)", "per-capture median level vs position (Experiment B)",
                       "baseline_fit_quality_rms_fraction (Level 1 quality.metrics) vs the negative offset per capture",
                       "the same control windows as the negative-map audit, before and after the candidate window"],
            "interpretation": "level stationary at a position and equal across positions -> negativity is a fixed REDUCE baseline offset; level drifting with time -> instrument gain/temperature; level tied to position -> sky/ground pickup"},
        "readiness": readiness,
    }


def write_yaml(path, obj) -> None:
    import yaml
    from science_forensics.models import sanitize
    Path(path).write_text(yaml.safe_dump(sanitize(obj), sort_keys=False, width=140, allow_unicode=True))
