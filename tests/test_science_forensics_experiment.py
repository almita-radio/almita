"""SCIENCE FORENSICS EXPERIMENT DESIGN: schedule balance, repeated-position representation and identity, timing-driven schedules,
design confounding (old serpentine vs new alternating designs), matched pairs, global cross-correlation shift, candidate-only vs
whole-spectrum drift, sky-fixed and receiver-fixed classification, synthetic power test (small), plan/CSV validity, and the
no-hardware / frozen-module boundary. Real-data tests skip when the local sessions are absent."""
import ast
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from science_forensics.experiment import analysis as A
from science_forensics.experiment import design as D
from science_forensics.experiment import plan as P
from science_forensics.experiment.synthetic import SCENARIOS, TRUTH_CLASS, build_experiment_campaign, CAND_CH, SPUR_CH

ROOT = Path(__file__).resolve().parent.parent
REAL_9 = ROOT / "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411"
SITE = json.loads((ROOT / "observer_config.json").read_text())["observer"]
EPOCH = datetime(2026, 9, 21, 14, 45, tzinfo=timezone.utc).timestamp()
_CH = D.choose_positions(D.Position("A", 177.45, -33.449), SITE, EPOCH)          # the same choice the committed plan makes
POS = {k: _CH[k] for k in "ABC"}
EXP = D.expected_signatures(POS, SITE, EPOCH, 3600.0)
SM = D.shift_model_from_expected(EXP)
TM = D.TimingModel()


def schedule(seq, positions=POS):
    return D.build_schedule(positions, seq, 10.0, TM)


def analyse(caps, scenario, seed=1):
    inp = build_experiment_campaign(caps, scenario, SM, seed=seed)
    labels = [c.label for c in caps]
    tol = D.design_diagnostics(caps)["pair_time_tolerance_s"]
    return A.analyze_experiment(inp.points, labels, start_channel=CAND_CH, expected=EXP, f0_hz=D.F_REST_HZ, tol_s=tol,
                                channel_width_hz=D.CHANNEL_WIDTH_HZ, exclude_channels=np.arange(int(SPUR_CH) - 6, int(SPUR_CH) + 7))


@pytest.fixture(scope="module")
def caps_b():
    return schedule(D.seq_random_bracketed(16, seed=3))


# ---------------------------------------------------------------- schedule balance / representation

def test_schedule_time_balance_per_position(caps_b):
    d = D.design_diagnostics(caps_b)
    assert d["position_time_balance_fraction_of_span"] < 0.05           # mean time of each position within 5 % of the span of the overall mean
    assert d["counts"]["A"] == 33 and d["counts"]["B"] == 16 and d["counts"]["C"] == 16
    assert d["bracketed_fraction_of_non_reference_captures"] >= 0.9


def test_random_bracketed_every_other_capture_is_reference_and_orders_vary():
    seq = D.seq_random_bracketed(16, seed=3)
    assert seq[0] == "A" and seq[-1] == "A" and all(seq[i] == "A" for i in range(0, len(seq), 2))
    assert seq != D.seq_random_bracketed(16, seed=4)                     # non-periodic, seed dependent
    assert D.seq_random_bracketed(16, seed=3) == seq                     # deterministic


def test_latin_example_and_abac_are_balanced():
    for seq in (D.seq_abac(16), D.seq_latin_example(16)):
        assert seq.count("B") == seq.count("C") and seq.count("A") == 2 * seq.count("B")


def test_repeated_position_is_repeated_coordinates_with_distinct_captures(caps_b, tmp_path):
    info = P.emit_capture_csv(tmp_path / "mosaic.csv", caps_b, "S", POS)
    rows = list(csv.DictReader(open(tmp_path / "mosaic.csv")))
    assert len(rows) == len(caps_b)
    assert len({(r["target_ra_hours"], r["target_dec_degrees"]) for r in rows}) == 3      # 3 distinct coordinates ...
    assert len({r["point_number"] for r in rows}) == len(rows)                            # ... but every capture has its own identity
    assert len({r["data_filename"] for r in rows}) == len(rows)
    assert [int(r["scan_order"]) for r in rows] == list(range(1, len(rows) + 1))          # order preserved exactly
    assert [r["experiment_label"] for r in rows] == [c.label for c in caps_b]
    assert P.validate_capture_csv(tmp_path / "mosaic.csv")["ok"]


def test_csv_validator_rejects_bad_plans(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("point_number,scan_order,target_ra_hours,target_dec_degrees,capture_status,start_time,end_time,duration,error_message,data_filename,session_name\n"
                 "1,1,25.0,-33,planned,,,,,a.dat,S\n1,1,12.0,-33,planned,,,,,a.dat,S\n")
    v = P.validate_capture_csv(p)
    assert not v["ok"] and any("duplicate" in x for x in v["problems"]) and any("out of range" in x for x in v["problems"])


def test_reduce_discovery_keeps_repeated_coordinates_as_distinct_points(caps_b, tmp_path):
    from reduce_engine.ingest import discover_campaign
    root = tmp_path / "camp"
    P.emit_capture_csv(root / "mosaic.csv", caps_b[:12], "CAMP", POS)
    rows = list(csv.DictReader(open(root / "mosaic.csv")))
    for r in rows:
        r["capture_status"] = "success"
        (root / "data" / "iq").mkdir(parents=True, exist_ok=True)
        (root / "data" / "iq" / (Path(r["data_filename"]).stem + ".h5")).write_bytes(b"")
    with open(root / "mosaic.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    m = discover_campaign(root)
    assert len(m.points) == 12 and all(p.accepted for p in m.points)
    assert len({p.point_index for p in m.points}) == 12
    assert len({(p.ra_hours, p.dec_degrees) for p in m.points}) == 3


def test_no_ha_crossing_in_planned_windows():
    v = D.visibility_ok(POS, SITE, np.linspace(EPOCH - 1500, EPOCH + 2400, 9), min_alt_deg=30, ha_side="negative", ha_margin_h=0.1)
    assert v["ok"], v
    # the same positions two hours later cross the meridian: must be flagged, not silently accepted
    late = D.visibility_ok(POS, SITE, np.linspace(EPOCH + 3600, EPOCH + 3 * 3600 + 2400, 9), min_alt_deg=30, ha_side="negative", ha_margin_h=0.1)
    assert not late["ok"]


# ---------------------------------------------------------------- timing and diagnostics

def test_timing_model_is_data_derived_and_move_size_monotone_enough():
    assert TM.evidence["n_intervals"] > 1000 and TM.evidence["n_campaigns"] >= 30
    assert 15.0 <= TM.noncapture_s(0.0) <= 21.0 and 17.0 <= TM.noncapture_s(6.75) <= 30.0
    assert TM.noncapture_s(3.0, "p90") >= TM.noncapture_s(3.0, "median")


def test_schedule_cadence_reflects_real_evidence(caps_b):
    cad = D.design_diagnostics(caps_b)["median_cadence_s"]
    assert 28.0 < cad < 40.0                                             # capture 10 s + ~19-25 s of real overhead


def test_new_design_breaks_time_sky_confounding_vs_serpentine():
    old = {f"P{i}": D.Position(f"P{i}", ra, dec) for i, (ra, dec) in enumerate(
        [(175.62, -34.94), (177.45, -34.95), (179.28, -34.94), (179.25, -33.44), (177.45, -33.45), (175.65, -33.44), (175.68, -31.94), (177.45, -31.95), (179.22, -31.94)], 1)}
    d_old = D.design_diagnostics(schedule([f"P{i}" for i in range(1, 10)], old))
    d_new = D.design_diagnostics(schedule(D.seq_random_bracketed(16, seed=3)))
    assert abs(d_old["corr_time_dec"]) > 0.9 and d_old["vif"]["time_h"] > 8 and d_old["confounded_time_sky"]
    assert abs(d_new["corr_time_dec"]) < 0.1 and abs(d_new["corr_time_ra"]) < 0.1 and d_new["vif"]["time_h"] < 1.2
    assert d_new["standardized_condition_number"] < d_old["standardized_condition_number"]


def test_serpentine_stays_confounded_for_any_shape():
    seq = D.seq_raster_reference(3, 3)
    pos = {l: D.Position(l, 175.6 + 1.8 * int(l[2]), -35.0 + 1.5 * int(l[1])) for l in seq}
    d = D.design_diagnostics(schedule(seq, pos))
    assert d["max_abs_corr_time_sky"] > 0.9


def test_fixed_sky_has_no_sky_variation_and_says_so():
    d = D.design_diagnostics(schedule(D.seq_fixed("A", 30), {"A": POS["A"]}), shift_model=SM)
    assert d["shift_regression"]["corr_time_lsrk_shift"] != d["shift_regression"]["corr_time_lsrk_shift"] or "note" in d["shift_regression"]


def test_sky_vs_receiver_separation_is_huge_in_new_design(caps_b):
    d = D.design_diagnostics(caps_b, shift_model=SM)
    assert abs(d["shift_regression"]["corr_time_lsrk_shift"]) < 0.1
    assert d["shift_regression"]["sky_vs_receiver_separation_in_sigma_at_30hz"] > 100


def test_repetition_calculator_matches_statistics():
    n_fast = D.captures_needed_for_rate(3.2e5, 100.0, 27.7)
    n_slow = D.captures_needed_for_rate(317.5, 100.0, 27.7)
    assert n_fast <= 5 and 40 <= n_slow <= 120


def test_predicted_sky_signature_is_parameter_free_and_consistent():
    e = D.expected_signatures(POS, SITE, EPOCH, 2400)
    k = D.F_REST_HZ / D.C_LIGHT_M_S
    b = e["pairwise"]["B-A"]
    assert abs(b["sky_stationary_predicts_delta_topocentric_frequency_hz"] - k * b["delta_lsrk_shift_m_s"]) < 1e-6
    assert abs(b["delta_lsrk_shift_channels"]) > 10                       # positions separate by >10 channels
    slow = e["fixed_sky_expected_drift"]["A"]["sky_stationary_predicts_topocentric_drift_hz_per_hour"]
    assert abs(slow) < 1000 and abs(slow) < 0.01 * 3.2e5                   # sky-null drift is ~1000x smaller than the observed drift


# ---------------------------------------------------------------- analysis on synthetic campaigns

@pytest.mark.parametrize("scenario,expected", [("sky_fixed", "SKY_FIXED"), ("receiver_fixed", "RECEIVER_FIXED"), ("time_drift", "TIME_GLOBAL"),
                                               ("candidate_only_drift", "TIME_CANDIDATE_ONLY"), ("spatial_gradient", "SKY_GRADIENT")])
def test_scenario_decisions_with_bracketed_design(caps_b, scenario, expected):
    r = analyse(caps_b, scenario)
    assert r["decision"]["decision"] == expected, r["decision"]


def test_whole_spectrum_drift_is_global_and_candidate_only_is_not(caps_b):
    g = analyse(caps_b, "time_drift")["global_vs_candidate"]
    c = analyse(caps_b, "candidate_only_drift")["global_vs_candidate"]
    assert g["tracks_candidate"] is True and abs(g["candidate_vs_global_slope"] - 1.0) < 0.2
    assert c["tracks_candidate"] is False and c["flat"] is True


def test_global_cross_correlation_recovers_known_shift():
    caps = schedule(D.seq_fixed("A", 2), {"A": POS["A"]})
    inp = build_experiment_campaign(caps, "time_drift", SM, seed=5)
    a, b = inp.points
    true_shift = -323525.0 * (b.t_mid_s - a.t_mid_s) / 3600.0 / D.CHANNEL_WIDTH_HZ
    g = A.global_shift(a, b, exclude_channels=np.arange(int(CAND_CH) - 120, int(CAND_CH) + 40))
    assert g["reliable"] and abs(g["shift_channels"] - true_shift) < 0.3


def test_global_shift_excludes_the_candidate_itself():
    caps = schedule(D.seq_fixed("A", 2), {"A": POS["A"]})
    inp = build_experiment_campaign(caps, "candidate_only_drift", SM, seed=6)      # only the candidate moves
    a, b = inp.points
    g = A.global_shift(a, b, exclude_channels=np.concatenate([np.arange(int(CAND_CH) - 120, int(CAND_CH) + 40), np.arange(int(SPUR_CH) - 6, int(SPUR_CH) + 7)]))
    assert abs(g["shift_channels"]) < 0.3                                          # controls did not move
    g2 = A.global_shift(a, b)                                                       # without exclusion the moving feature would matter
    assert g2["peak_snr"] > 0


def test_local_candidate_drift_measured_in_reference_series(caps_b):
    r = analyse(caps_b, "candidate_only_drift")["reference_series"]
    assert abs(r["drift_hz_per_hour"] - (-323525.0)) < 5 * max(r["drift_se_hz_per_hour"], 100.0)
    assert r["drift_se_hz_per_hour"] < 2000


def test_sky_fixed_reference_series_matches_sky_null_not_instrumental(caps_b):
    r = analyse(caps_b, "sky_fixed")
    rs = r["reference_series"]
    assert abs(rs["drift_hz_per_hour"] - rs["sky_null_drift_hz_per_hour"]) < 5 * max(rs["drift_se_hz_per_hour"], 30.0)
    off = r["matched_pairs"]["position_offsets"]
    for lab, pred in r["predicted_position_offsets_hz"].items():
        assert abs(off[lab]["mean_delta_hz"] - pred) < 5 * off[lab]["se_used_hz"] + 50.0


def test_matched_pairs_bracket_within_tolerance(caps_b):
    inp = build_experiment_campaign(caps_b, "sky_fixed", SM, seed=2)
    labels = [c.label for c in caps_b]
    ms = A.track_line(inp.points, start_channel=CAND_CH, search_half_width=75, fit_half_width=30)
    ii, f, sf = A.centroid_hz(inp.points, ms)
    tol = D.design_diagnostics(caps_b)["pair_time_tolerance_s"]
    mp = A.matched_pairs(np.array([p.t_mid_s for p in inp.points]), labels, f, sf, ii, tol_s=tol)
    assert len(mp["cross_position_pairs"]) >= 25
    assert all(c["gap_s"] <= 2 * tol + 1e-6 for c in mp["cross_position_pairs"])
    assert all(p["dt_s"] <= 2 * tol + 1e-6 for p in mp["same_position_pairs"])


def test_shift_and_difference_collapses_when_prediction_is_right(caps_b):
    inp = build_experiment_campaign(caps_b, "sky_fixed", SM, seed=3)
    a = inp.points[0]                                    # A
    b = next(p for p, c in zip(inp.points, caps_b) if c.label == "B")
    pred = EXP["pairwise"]["B-A"]["sky_stationary_predicts_delta_topocentric_frequency_hz"]
    sd = A.shift_and_difference(a, b, pred, (1200, 1400))
    assert sd["diagnostic_only"] and sd["shift_and_difference_rms"] < 0.5 * sd["plain_difference_rms"]


def test_shift_and_difference_does_not_help_receiver_fixed(caps_b):
    inp = build_experiment_campaign(caps_b, "receiver_fixed", SM, seed=3)
    a = inp.points[0]
    b = next(p for p, c in zip(inp.points, caps_b) if c.label == "B")
    sd = A.shift_and_difference(a, b, EXP["pairwise"]["B-A"]["sky_stationary_predicts_delta_topocentric_frequency_hz"], (1200, 1400))
    assert sd["shift_and_difference_rms"] > 1.5 * sd["plain_difference_rms"] and sd["plain_difference_rms"] < 0.1


def test_robust_morphology_agrees_with_gaussian_on_a_gaussian(caps_b):
    inp = build_experiment_campaign(caps_b[:3], "receiver_fixed", SM, seed=4)
    m = A.robust_morphology(inp.points[0], CAND_CH, 40)
    assert m["status"] == "OK" and abs(m["halfmax_centre_channel"] - CAND_CH) < 1.0 and 18 < m["fwhm_channels"] < 27 and abs(m["skewness"]) < 0.4


def test_position_labels_keep_captures_distinct():
    inp = build_experiment_campaign(schedule(D.seq_random_bracketed(3, seed=1)), "sky_fixed", SM, seed=1)
    labels = A.assign_position_labels(inp.points)
    assert set(labels) == {"A", "B", "C"} and labels[0] == "A"
    assert len({p.point_index for p in inp.points}) == len(inp.points)


def test_decide_is_conservative_with_too_few_points():
    d = A.decide(n_time_points=3, drift_hz_per_hour=1e5, drift_se=10, span_h=0.1, sky_null_drift_hz_per_hour=-300, position_offsets={}, predicted_offsets={})
    assert d["decision"] == "UNRESOLVED"


def test_fixed_sky_alone_cannot_split_sky_from_receiver(caps_b):
    caps = schedule(D.seq_fixed("A", 48), {"A": POS["A"]})
    for sc in ("sky_fixed", "receiver_fixed"):
        assert analyse(caps, sc)["decision"]["decision"] == "STATIONARY_UNDISCRIMINATED"
    assert analyse(caps, "time_drift")["decision"]["decision"] == "TIME_GLOBAL"


def test_old_serpentine_regression_confuses_dec_gradient_with_sky_fixed():
    """The point of the whole exercise: on the old raster time, Dec and shift are entangled, a Dec gradient is not identified."""
    from science_forensics.experiment.power import run_power_test
    old = {f"P{i}": D.Position(f"P{i}", ra, dec) for i, (ra, dec) in enumerate(
        [(175.6207, -34.9357), (177.45, -34.9489), (179.2792, -34.9357), (179.2475, -33.4359), (177.45, -33.4489), (175.6524, -33.4359),
         (175.6829, -31.9361), (177.45, -31.9489), (179.217, -31.9361)], 1)}
    e_old = D.expected_signatures(old, SITE, EPOCH, 300)
    caps = schedule([f"P{i}" for i in range(1, 10)], old)
    res_old = run_power_test({"old": {"caps": caps, "positions": old, "expected": e_old, "analysis": "regression", "tol_s": 60.0, "ref_label": "P1"}},
                             scenarios=("spatial_gradient",), n_seeds=3)
    assert res_old["old"]["scenarios"]["spatial_gradient"]["correct"] == 0
    cb = schedule(D.seq_random_bracketed(16, seed=3))
    res_new = run_power_test({"new": {"caps": cb, "positions": POS, "expected": EXP, "analysis": "full", "tol_s": D.design_diagnostics(cb)["pair_time_tolerance_s"], "ref_label": "A"}},
                             scenarios=("spatial_gradient",), n_seeds=3)
    assert res_new["new"]["scenarios"]["spatial_gradient"]["correct"] == 3


# ---------------------------------------------------------------- plan and boundaries

def test_capture_command_uses_the_audited_instrument_config(tmp_path):
    cmd = P.capture_command("x.csv", P.DEFAULT_INSTRUMENT)
    assert cmd[cmd.index("--capture") + 1] == "10.0" and cmd[cmd.index("--settle") + 1] == "2.0"
    assert cmd[cmd.index("--sdr-gain") + 1] == "40.2" and "--rfi-ref-enabled" in cmd and "--input-topology" in cmd


def test_instrument_audit_reads_the_real_resolved_plan_when_present():
    p = ROOT / "data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16/observation_resolved.json"
    if not p.is_file():
        pytest.skip("real resolved plan not present")
    got = P.instrument_from_resolved(p)
    for k in ("capture_seconds", "settle_seconds", "sdr_center_frequency_hz", "sdr_sample_rate_hz", "sdr_gain_db"):
        assert got[k] == P.DEFAULT_INSTRUMENT[k]


def test_committed_plan_is_consistent_with_its_csvs():
    import yaml
    plan_path = ROOT / "examples/science_forensics_experiment.yaml"
    if not plan_path.is_file():
        pytest.skip("plan not generated")
    plan = yaml.safe_load(plan_path.read_text())
    assert plan["executed_by_this_plan"] is False and plan["schema"] == "science_forensics_experiment"
    for name in ("A", "B", "AB"):
        f = plan["experiments"][name]["csv"]
        path = ROOT / f["path"]
        assert P.validate_capture_csv(path)["ok"]
        import hashlib
        assert hashlib.sha256(path.read_bytes()).hexdigest() == f["sha256"]
        assert plan["experiments"][name]["n_captures"] == f["n_rows"]
    assert plan["readiness"]["verdict"].startswith(("READY FOR FIELD", "BLOCKED"))
    assert set(plan["null_hypotheses"]) >= {"H0_time", "H0_sky", "H0_frequency"}


def test_experiment_modules_do_not_import_hardware_network_or_raw():
    banned = {"socket", "requests", "urllib", "http", "serial", "indi", "PyIndi", "rtlsdr", "h5py", "subprocess"}
    for f in (ROOT / "science_forensics" / "experiment").glob("*.py"):
        tree = ast.parse(f.read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        assert not (mods & banned), (f.name, mods & banned)


def test_frozen_modules_untouched():
    frozen = {"reduce_engine": "2afc4c5", "science_engine": "f5e2f65"}
    for path, commit in frozen.items():
        out = subprocess.run(["git", "diff", "--name-only", commit, "--", path], cwd=ROOT, capture_output=True, text=True)
        if out.returncode != 0:
            pytest.skip("git history unavailable")
        assert out.stdout.strip() == "", out.stdout
    out = subprocess.run(["git", "diff", "--name-only", "8f4b40a", "--", "science_forensics/models.py", "science_forensics/measure.py", "science_forensics/statistics.py",
                          "science_forensics/negative.py", "science_forensics/ingest.py", "science_forensics/session.py", "science_forensics/plots.py",
                          "science_forensics/synthetic.py", "almita_science_forensics.py", "capture.py", "observation_orchestrator.py"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.strip() == "", out.stdout


@pytest.mark.skipif(not REAL_9.is_dir(), reason="real 9-point REDUCE session absent")
def test_replay_on_real_old_campaign_reports_candidate_moves_while_global_structure_does_not():
    from science_forensics.experiment.replay import replay_campaign
    r = replay_campaign(REAL_9, window_lsrk_center_m_s=150857.0, window_half_width_m_s=5607.0)
    assert r["n_points"] == 9 and r["candidate"]["total_channels_over_span"] < -70
    gs = r["global_shift"]
    assert gs["reliable_steps"] >= 6
    assert gs["global_shift_significant"] is False and abs(gs["total_channels"]) < 1.0
    assert r["control_features"]["n_found"] == 0 or all("start_channel" in l for l in r["control_features"]["lines"])
    assert "corr_baseline_rms_vs_time" in r["baseline_metrics"]


@pytest.mark.skipif(not REAL_9.is_dir(), reason="real 9-point REDUCE session absent")
def test_global_shift_recovers_injected_shifts_on_real_level1_spectra():
    """Sensitivity on REAL structure: roll a real Level 1 spectrum by a known number of channels and recover it (0 shift stays 0)."""
    import dataclasses
    from science_forensics.ingest import load_forensics_input
    inp = load_forensics_input(REAL_9)
    p1, p2 = inp.points[0], inp.points[1]
    ex = A.spur_and_masked_channels(inp.points)
    for sh in (0, 3, 7, -12):
        q = dataclasses.replace(p2, value=np.roll(p2.value, sh), mask=np.roll(p2.mask, sh), sigma=np.roll(p2.sigma, sh))
        g = A.global_shift(p1, q, exclude_channels=ex, max_lag=100)
        assert g["reliable"] and abs(g["shift_channels"] - sh) < 0.1
