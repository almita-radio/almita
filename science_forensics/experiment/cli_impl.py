"""Implementation of the experiment CLI subcommands (kept out of the top-level script so it can be imported and tested)."""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from science_forensics.models import sanitize
from science_forensics.experiment import design as D
from science_forensics.experiment import plan as P

REPO = Path(__file__).resolve().parents[2]
DEFAULT_EPOCH = "2026-09-21T14:45:00+00:00"
A_POSITION = D.Position("A", 177.45, -33.449)          # centre of the feature-campaign grid
N_A, N_B_BLOCKS, N_A_IN_AB, SEED_B = 60, 16, 40, 3     # AB = A x 20, B, A x 20 (time-balanced A)
POWER_RESULTS = REPO / "science_forensics" / "experiment" / "power_test_results.json"
OLD_REDUCE = REPO / "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411"


def load_site(path) -> dict:
    return json.loads(Path(path).read_text())["observer"]


def epoch_posix(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def make_designs(site: dict, epoch_iso: str, timing: D.TimingModel, capture_s: float) -> dict:
    t0 = epoch_posix(epoch_iso)
    ch = D.choose_positions(A_POSITION, site, t0)
    positions = {k: ch[k] for k in ("A", "B", "C")}
    expected = D.expected_signatures(positions, site, t0, 3600.0)
    sm = D.shift_model_from_expected(expected)
    seqs = {"A": D.seq_fixed("A", N_A), "B": D.seq_random_bracketed(N_B_BLOCKS, seed=SEED_B)}
    seqs["AB"] = D.seq_fixed("A", N_A_IN_AB // 2) + seqs["B"] + D.seq_fixed("A", N_A_IN_AB // 2)
    out = {"positions": positions, "expected": expected, "shift_model": sm, "min_pairwise_delta_shift_m_s": ch["min_pairwise_delta_shift_m_s"]}
    for name, seq in seqs.items():
        caps = D.build_schedule(positions, seq, capture_s, timing)
        diag = D.design_diagnostics(caps, shift_model=sm)
        q = {}
        for qn in ("median", "p75", "p90"):
            cq = D.build_schedule(positions, seq, capture_s, timing, quantile=qn)
            q[qn] = D.schedule_duration_s(cq, capture_s) / 60.0
        out[name] = {"sequence": seq, "caps": caps, "diagnostics": diag, "duration": q, "median_cadence_s": diag["median_cadence_s"],
                     "pair_time_tolerance_s": diag["pair_time_tolerance_s"]}
    out["timing_quantiles"] = {n: out[n]["duration"] for n in ("A", "B", "AB")}
    return out


def old_reference() -> dict | None:
    """Confounding of the REAL old raster, computed from its Level 1 times/coordinates (not from a model)."""
    if not OLD_REDUCE.is_dir():
        return None
    from science_forensics.ingest import load_forensics_input
    from science_forensics.experiment.design import Capture
    inp = load_forensics_input(OLD_REDUCE)
    t0 = inp.points[0].t_mid_s
    caps = [Capture(i + 1, f"P{p.point_index}", p.ra_deg, p.dec_deg, p.t_start_s - t0, p.t_mid_s - t0, 0.0, 0.0) for i, p in enumerate(inp.points)]
    sm = {f"P{p.point_index}": (p.lsrk_shift_m_s, 0.0) for p in inp.points}
    d = D.design_diagnostics(caps, shift_model=sm)
    return {"campaign": inp.campaign_id, "n_captures": d["n_captures"], "duration_min": d["duration_min"], "corr_time_ra": d["corr_time_ra"],
            "corr_time_dec": d["corr_time_dec"], "vif": d["vif"], "standardized_condition_number": d["standardized_condition_number"],
            "shift_regression": d["shift_regression"], "joint_model_standard_errors": d["joint_model_standard_errors"], "source": "REDUCE Level 1 real times/coordinates"}


def cmd_design(args) -> dict:
    timing = D.TimingModel()
    instrument = dict(P.DEFAULT_INSTRUMENT)
    resolved = REPO / "data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16/observation_resolved.json"
    if resolved.is_file():
        instrument = P.instrument_from_resolved(resolved)
        instrument["source"] = str(resolved.relative_to(REPO))
    des = make_designs(load_site(args.observer_config), args.epoch, timing, instrument["capture_seconds"])
    sess = "SCIENCE-FORENSICS-EXP-HI148-01"
    emit = Path(args.emit_dir)
    files = {}
    for name in ("A", "B", "AB"):
        d = emit / name
        info = P.emit_capture_csv(d / "mosaic.csv", des[name]["caps"], f"{sess}-{name}", des["positions"])
        v = P.validate_capture_csv(d / "mosaic.csv")
        if not v["ok"]:
            raise SystemExit(f"emitted CSV invalid: {v['problems']}")
        files[name] = {"path": str(d / "mosaic.csv"), "sha256": info["sha256"], "n_rows": info["n_rows"]}
        if args.copy_observer_config:
            shutil.copyfile(args.observer_config, d / "observer_config.json")
    diagnostics = {n: des[n]["diagnostics"] for n in ("A", "B", "AB")}
    power = json.loads(POWER_RESULTS.read_text()) if POWER_RESULTS.is_file() else None
    sigma = {"median_hz": 30.4, "max_hz": 37.7, "source": "feature_summary/feature_track of the 9-point real campaign (formal, per capture)"}
    readiness = readiness_block()
    plan = P.build_plan(positions=des["positions"], expected=des["expected"], designs=des, diagnostics=diagnostics, instrument=instrument, timing=timing,
                        epoch_utc=args.epoch, old_reference=old_reference(), power=power_summary(power), centroid_sigma_hz=sigma,
                        files=files, readiness=readiness)
    plan["capture_command_template"] = {n: P.capture_command(files[n]["path"], instrument) for n in ("A", "B", "AB")}
    P.write_yaml(args.out, plan)
    return {"plan": str(args.out), "csv": files, "durations_min": des["timing_quantiles"], "positions": {k: (v.ra_deg, v.dec_deg) for k, v in des["positions"].items()}}


def power_summary(power: dict | None) -> dict | None:
    if not power:
        return None
    out = {"n_seeds": power.get("n_seeds"), "generated_by": "almita_science_forensics_experiment.py simulate", "designs": {}}
    for name, d in power["designs"].items():
        out["designs"][name] = {"analysis": d["analysis"], "scenarios": {s: {"truth": v["truth"], "correct": v["correct"], "abstain": v["abstain"],
                                                                           "wrong": v["wrong"], "n": v["n"], "decisions": v["counts"]} for s, v in d["scenarios"].items()}}
    return out


def readiness_block() -> dict:
    return {
        "verdict": "READY FOR FIELD (plan and analysis); execution is a manual field action",
        "software": ["plan, CSVs and analysis validated on synthetic and replayed real data", "capture.py --csv accepts explicit ordered lists with repeated coordinates (code audit; CSV validated against its own preflight rules)",
                     "REDUCE/SCIENCE identity (point_number/data_filename, capture sha256) keeps repeated coordinates distinct - verified by test"],
        "operator_prerequisites": ["mount/antenna deployed outdoors (indoors: no real GOTO); no software authorization changes that",
                                   "manual `capture.py --csv ...` (OBSERVE spec/orchestrator cannot express an explicit sequence)",
                                   "start inside a `windows` result so that A, B and C stay on one hour-angle side above min altitude for the whole run",
                                   "run capture.py --preflight-only on the emitted CSV before the first real run"],
        "known_limits": ["the feature may be absent on the new day: that is a result, not a failure (runbook section 9)",
                         "bracket interpolation is exact for linear drift; curvature over a 65 s bracket adds error (quantified in the power test)",
                         "OBSERVE orchestrator not modified (minimal future change: accept an explicit `sequence:` list in the spec)"]}


def cmd_windows(args) -> list:
    site = load_site(args.observer_config)
    t0 = epoch_posix(args.epoch)
    pos = D.choose_positions(A_POSITION, site, t0)
    p3 = {k: pos[k] for k in ("A", "B", "C")}
    return D.find_start_windows(p3, site, args.date, args.days, args.minutes * 60.0, min_alt_deg=args.min_alt, ha_margin_h=args.ha_margin, step_min=args.step_min)


def _power_designs(timing, site, epoch_iso, capture_s) -> dict:
    des = make_designs(site, epoch_iso, timing, capture_s)
    pos, exp = des["positions"], des["expected"]
    from science_forensics.experiment.design import Position
    t0 = epoch_posix(epoch_iso)
    old_pts = [(175.6207, -34.9357), (177.45, -34.9489), (179.2792, -34.9357), (179.2475, -33.4359), (177.45, -33.4489), (175.6524, -33.4359),
               (175.6829, -31.9361), (177.45, -31.9489), (179.217, -31.9361)]
    old = {f"P{i + 1}": Position(f"P{i + 1}", ra, dec) for i, (ra, dec) in enumerate(old_pts)}
    exp_old = D.expected_signatures(old, site, t0, 300.0)

    def mk(seq, p, e, analysis, ref):
        caps = D.build_schedule(p, seq, capture_s, timing)
        return {"caps": caps, "positions": p, "expected": e, "analysis": analysis, "tol_s": D.design_diagnostics(caps)["pair_time_tolerance_s"], "ref_label": ref}
    return {"old_raster_9pt__regression": mk([f"P{k}" for k in range(1, 10)], old, exp_old, "regression", "P1"),
            "expA_fixed_sky__full": mk(des["A"]["sequence"], {"A": pos["A"]}, exp, "full", "A"),
            "expB_random_bracketed__regression": mk(des["B"]["sequence"], pos, exp, "regression", "A"),
            "expB_random_bracketed__full": mk(des["B"]["sequence"], pos, exp, "full", "A"),
            "expAB_combined__full": mk(des["AB"]["sequence"], pos, exp, "full", "A")}


def cmd_simulate(args) -> dict:
    from science_forensics.experiment.power import run_power_test
    from science_forensics.experiment.synthetic import SCENARIOS
    site = load_site(args.observer_config)
    designs = _power_designs(D.TimingModel(), site, args.epoch, 10.0)
    spatial = {"spatial_gradient", "spatial_gradient_ra", "time_plus_spatial"}
    res = {}
    for name, d in designs.items():
        scen = tuple(s for s in SCENARIOS if not (name == "expA_fixed_sky__full" and s in spatial))
        one = run_power_test({name: d}, scenarios=scen, n_seeds=args.seeds, progress=lambda n, r: print(f"  {n}: {r['seconds']:.0f} s", flush=True))
        res.update(one)
    out = {"n_seeds": args.seeds, "epoch": args.epoch, "designs": res,
           "scoring": "correct = decision equals the injected truth (regression analyses score the TIME family as one class); abstain = UNRESOLVED / STATIONARY_UNDISCRIMINATED; wrong = anything else"}
    Path(args.out).write_text(json.dumps(sanitize(out), indent=1, sort_keys=True))
    return out


def format_power(res: dict) -> list:
    lines = []
    for name, d in res["designs"].items():
        lines.append(f"{name}  [{d['analysis']}]")
        for sc, v in d["scenarios"].items():
            lines.append(f"  {sc:<22} truth={v['truth']:<24} correct {v['correct']:>3}/{v['n']}  abstain {v['abstain']:>3}  wrong {v['wrong']:>3}   {v['counts']}")
    return lines


def cmd_replay(args) -> dict:
    from science_forensics.experiment.replay import replay_campaign
    return replay_campaign(args.reduce_session_dir, window_lsrk_center_m_s=args.center, window_half_width_m_s=args.half_width)


def cmd_analyze(args) -> dict:
    """Analyse a REDUCE session of an experiment run (Level 1 only). Labels come from the plan CSV (experiment_label per point_number)."""
    from science_forensics.ingest import load_forensics_input
    from science_forensics.models import ForensicsConfig
    from science_forensics.measure import measure_point
    from science_forensics.experiment import analysis as A
    inp = load_forensics_input(args.reduce_session_dir)
    pts = inp.points
    plan_rows = {int(r["point_number"]): r for r in csv.DictReader(open(args.plan_csv, newline=""))}
    missing = [p.point_index for p in pts if p.point_index not in plan_rows]
    if missing:
        raise SystemExit(f"points {missing} are not in the plan CSV: refusing to label by guesswork")
    labels = [plan_rows[p.point_index]["experiment_label"] for p in pts]
    drift = [p.point_index for p, q in zip(pts[1:], pts[:-1]) if p.t_mid_s < q.t_mid_s]
    order_ok = not drift
    cfg = ForensicsConfig(window_center=args.center, window_half_width=args.half_width, window_frame="lsrk_velocity", feature_id="EXP")
    first_a = next(p for p, lb in zip(pts, labels) if lb == "A")
    m0 = measure_point(first_a, cfg)
    start = m0.centroid_channel if np.isfinite(m0.centroid_channel) else 0.5 * (m0.window_first_channel + m0.window_last_channel)
    exp = A.expected_from_points(pts, labels, inp.rest_frequency_hz)
    times = np.array([p.t_mid_s for p in pts])
    tol = 1.5 * float(np.median(np.diff(times)))
    from reduce_engine.models import MaskFlag
    persistent = A.spur_and_masked_channels(pts)
    res = A.analyze_experiment(pts, labels, start_channel=float(start), expected=exp, f0_hz=inp.rest_frequency_hz, tol_s=tol,
                               channel_width_hz=D.CHANNEL_WIDTH_HZ, exclude_channels=persistent, search_half_width=75)
    res["capture_order_matches_time"] = order_ok
    res["campaign_id"] = inp.campaign_id
    res["reduce_session_id"] = inp.reduce_session_id
    res["plan_csv_sha256"] = hashlib.sha256(Path(args.plan_csv).read_bytes()).hexdigest()
    lines = A.find_control_lines(pts, exclude_channels=persistent, n_lines=5)
    res["control_features"] = A.control_line_tracks(pts, lines, inp.rest_frequency_hz, D.CHANNEL_WIDTH_HZ) if lines else []
    ms = [type("M", (), m) for m in res["measurements"]]
    res["morphology_nonparametric"] = [A.robust_morphology(p, m["centroid_channel"]) if np.isfinite(m["centroid_channel"]) else {"status": "NO_CENTROID"}
                                       for p, m in zip(pts, res["measurements"])]
    res["expected"] = exp
    return res


def analysis_markdown(res: dict) -> str:
    d = res["decision"]
    lines = [f"# Experiment analysis: {res.get('campaign_id', '')}", "",
             f"decision: **{d['decision']}**  ({d.get('reason', '')})", "",
             f"- captures {res['n_captures']}, detections {res['n_detected']}",
             f"- H0_time rejected: {d['H0_time_rejected']} (excess drift {d['excess_drift_hz_per_hour']:.0f} Hz/h, z={d['excess_drift_z']:.1f}, "
             f"{d['excess_total_over_span_channels']:.1f} ch over the span; stationary chi2/dof {d['stationary_chi2_per_dof']})",
             f"- H0_sky consistent: {d['H0_sky_consistent']} (chi2/dof {d['chi2_sky_per_dof']})",
             f"- H0_frequency consistent: {d['H0_frequency_consistent']} (chi2/dof {d['chi2_zero_per_dof']})",
             f"- global shift tracks candidate: {d['global_shift_tracks_candidate']}; global flat: {d['global_shift_flat']}"]
    return "\n".join(lines) + "\n"
