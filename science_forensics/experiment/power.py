"""Synthetic POWER TEST: which designs, analysed how, identify which physical scenario? Reports counts (a confusion matrix), never a
single 'accuracy' number: wrong decisions and honest abstentions are separate columns."""
from __future__ import annotations

import time
from collections import Counter, OrderedDict

import numpy as np

from science_forensics.experiment import design as D
from science_forensics.experiment.analysis import analyze_experiment, regression_classifier, track_line
from science_forensics.experiment.synthetic import SCENARIOS, TRUTH_CLASS, build_experiment_campaign, shift_model_from_expected, SPUR_CH, CAND_CH

FAMILY = {"TIME_GLOBAL": "TIME", "TIME_CANDIDATE_ONLY": "TIME", "TIME_DRIFT": "TIME"}
ABSTAIN = ("UNRESOLVED", "STATIONARY_UNDISCRIMINATED")


def family(x: str) -> str:
    return FAMILY.get(x, x)


def _run_one(caps, positions, expected, scenario, seed, analysis, tol_s, ref_label):
    sm = shift_model_from_expected(expected)
    inp = build_experiment_campaign(caps, scenario, sm, seed=seed, ref_label=ref_label)
    labels = [c.label for c in caps]
    if analysis == "full":
        r = analyze_experiment(inp.points, labels, start_channel=CAND_CH, expected=expected, f0_hz=D.F_REST_HZ, tol_s=tol_s,
                               channel_width_hz=D.CHANNEL_WIDTH_HZ, exclude_channels=np.arange(int(SPUR_CH) - 6, int(SPUR_CH) + 7))
        return r["decision"]["decision"]
    ms = track_line(inp.points, start_channel=CAND_CH, search_half_width=60, fit_half_width=30)
    return regression_classifier(inp.points, ms, D.F_REST_HZ, D.CHANNEL_WIDTH_HZ)["decision"]


def run_power_test(designs: dict, scenarios=SCENARIOS, n_seeds: int = 10, progress=None) -> dict:
    """designs: name -> {caps, positions, expected, analysis ('full'|'regression'), tol_s, ref_label}.
    Returns per design: matrix[scenario][decision] counts, plus correct / abstain / wrong totals per scenario (family-level)."""
    out = OrderedDict()
    for name, d in designs.items():
        rows, t0 = OrderedDict(), time.time()
        for sc in scenarios:
            cnt = Counter()
            for seed in range(n_seeds):
                cnt[_run_one(d["caps"], d["positions"], d["expected"], sc, 1000 + seed, d["analysis"], d["tol_s"], d.get("ref_label", "A"))] += 1
            truth = TRUTH_CLASS[sc]
            correct = sum(v for k, v in cnt.items() if (k == truth if d["analysis"] == "full" else family(k) == family(truth)))
            if d["analysis"] == "full":
                correct = sum(v for k, v in cnt.items() if k == truth)
            abstain = sum(v for k, v in cnt.items() if k in ABSTAIN)
            rows[sc] = {"truth": truth, "counts": dict(cnt), "n": n_seeds, "correct": int(correct), "abstain": int(abstain),
                        "wrong": int(n_seeds - correct - abstain)}
        out[name] = {"analysis": d["analysis"], "scenarios": rows, "seconds": time.time() - t0}
        if progress:
            progress(name, out[name])
    return out
