"""EXPERIMENT (2026-11-21): recipes, same-altitude OFFs, readiness, the verdicts on synthetic spectra, the runner with
OBSERVE/REDUCE faked, and SURVEY/EXPERIMENT never driving the mount together."""
from __future__ import annotations

import copy
import math
import types

import numpy as np
import pytest
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy import units as u
from astropy.time import Time

import almita_experiment
import experiment as X

LOC = EarthLocation(lat=-33.45 * u.deg, lon=-70.66 * u.deg, height=600 * u.m)


def recipes():
    return {r["id"]: r for r in X.load_recipes()}


# ------------------------------------------------------------------ recipes

def test_shipped_recipes_validate_and_cover_the_five_experiments():
    rs = recipes()
    assert {"CHAIN-CHECK", "VEL-AB", "SMC-ONOFF", "LMC-ONOFF", "ROTATION"} <= set(rs)
    assert [r["kind"] for r in X.load_recipes()][:2] == ["LINE_CHECK", "AB_VELOCITY"]      # cheap check first
    v = rs["VEL-AB"]
    # the decisive test: 12-11 mode, 19-11 mode, and the one that separates throw from centring
    assert (v["modes"]["A"]["offset_hz"], v["modes"]["A"]["center"]) == (None, None)
    assert (v["modes"]["B"]["offset_hz"], v["modes"]["B"]["center"]) == (300000.0, "auto")
    assert (v["modes"]["C"]["offset_hz"], v["modes"]["C"]["center"]) == (300000.0, 0.0)
    assert [s["mode"] for s in X.expand_steps(v)] == ["A", "B", "C", "A", "B", "C"]
    for cloud in ("SMC-ONOFF", "LMC-ONOFF"):
        r = rs[cloud]
        offs = [t for t in r["targets"] if "same_alt_as" in t]
        assert len(offs) == 2 and {math.copysign(1, t["daz_deg"]) for t in offs} == {1.0, -1.0}   # one each side
        assert r["cycles"] >= 6 and r["analysis"]["min_pairs"] >= 4


@pytest.mark.parametrize("mutate, msg", [
    (lambda r: r["targets"].append({"id": "X", "same_alt_as": "NOPE", "daz_deg": 30}), "same_alt_as"),
    (lambda r: r["sequence"].append({"mode": "Z", "targets": ["SMC"]}), "mode"),
    (lambda r: r["sequence"].append({"mode": "S", "targets": ["GHOST"]}), "targets"),
    (lambda r: r["analysis"].update(window_km_s=[200, 120]), "window_km_s"),
    (lambda r: r["targets"].append({"id": "Y", "l": 1, "b": 2, "ra_hours": 3, "dec_deg": 4}), "exactly one"),
    (lambda r: r["modes"]["S"].update(offset_hz=5), "offset"),
])
def test_bad_recipes_are_refused(mutate, msg):
    raw = copy.deepcopy(recipes()["SMC-ONOFF"])
    mutate(raw)
    with pytest.raises(X.RecipeError, match=msg):
        X.validate_recipe(raw)


# ------------------------------------------------------------------ sky geometry

def test_same_altitude_off_is_at_the_source_altitude_and_clears_the_rules():
    r = recipes()["SMC-ONOFF"]
    when = Time("2026-11-22T02:00:00")
    frame = AltAz(obstime=when, location=LOC)
    ra, dec = X.fixed_position(r["targets"][0])
    src = SkyCoord(ra=ra * u.hourangle, dec=dec * u.deg).transform_to(frame)
    for t in r["targets"][1:]:
        off = X.same_altitude_off(r, t, when, LOC)
        assert off["ok"], off
        o = SkyCoord(ra=off["ra_hours"] * u.hourangle, dec=off["dec_deg"] * u.deg).transform_to(frame)
        assert abs(o.alt.deg - src.alt.deg) < 0.1                         # the whole point: same altitude
        assert math.copysign(1, off["daz_deg"]) == math.copysign(1, t["daz_deg"])
        assert X._sep(off["ra_hours"], off["dec_deg"], ra, dec) >= 25
        assert X._gal_sep(off["l"], off["b"], 280.47, -32.89) >= 25          # not on the LMC
        assert abs(off["b"]) >= 20


def test_resolve_step_drops_what_is_low_and_choose_keeps_the_highest():
    r = recipes()["CHAIN-CHECK"]
    step = X.expand_steps(r)[0]
    res = X.resolve_step(r, step, LOC, now=Time("2026-11-21T16:00:00"))
    assert len(res["targets"]) == 1
    up = res["targets"][0]
    assert all(d["reason"] for d in res["dropped"])
    others = [d for d in res["dropped"] if d["reason"].startswith("not chosen")]
    assert others                                                       # several were up: the highest won
    assert up["alt_now"] >= r["min_altitude_deg"] + X.ALT_MARGIN_DEG


def test_readiness_ready_later_and_coverage():
    r = recipes()["SMC-ONOFF"]
    high = X.readiness(r, LOC, now=Time("2026-11-22T02:00:00"))         # 23:00 Chile: SMC high
    assert high["state"] == "READY" and high["coverage"] == 1.0
    low = X.readiness(r, LOC, now=Time("2026-11-21T16:00:00"))          # 13:00 Chile: SMC low in the south
    assert low["state"] == "LATER" and X._parse_utc(low["start_utc"]) > X._parse_utc("2026-11-21T16:00:00Z")
    assert low["duration_s"] == pytest.approx(X.total_seconds(r), abs=1)


# ------------------------------------------------------------------ verdicts on synthetic spectra

V = np.arange(-200, 300.01, 1.29)


def profile(shift=0.0, amp=1.0):
    """flat-topped, double-humped like HI4PI at l = 5-20 (the shape that fooled the peak comparison)"""
    g = lambda c, w, a: a * np.exp(-0.5 * ((V - c - shift) / w) ** 2)
    return amp * (g(5, 9, 12) + g(28, 12, 10) + g(70, 18, 3))


def spec(y, rng=None, noise=0.15, base=(0.0, 0.0)):
    rng = rng or np.random.default_rng(1)
    return {"v": V.copy(), "y": y + base[0] + base[1] * V / 100 + rng.normal(0, noise, V.size)}


def test_fit_lag_recovers_a_whole_profile_shift_with_baseline_and_scale():
    rng = np.random.default_rng(7)
    ref = X.on_grid(spec(profile(), rng), X.GRID)
    y = X.on_grid(spec(profile(-5.3, 0.8), rng, base=(0.4, -0.2)), X.GRID)
    f = X.fit_lag(X.GRID, ref, y)
    assert f["ok"] and f["lag_km_s"] == pytest.approx(-5.3, abs=0.3) and f["scale"] == pytest.approx(0.8, rel=0.1)


def _run(recipe, **kw):
    return X.new_run(recipe, open_sky_confirmed=True, **kw)


def test_velocity_verdict_names_the_mode_that_shifts():
    rng = np.random.default_rng(3)
    r = recipes()["VEL-AB"]
    pts = []
    for c in range(2):
        pts += [{"mode": "A", "target_id": "l20", "cycle": c, "spec": spec(profile(0.0), rng)},
                {"mode": "B", "target_id": "l20", "cycle": c, "spec": spec(profile(-5.5), rng)},
                {"mode": "C", "target_id": "l20", "cycle": c, "spec": spec(profile(0.2), rng)}]
    a = X.analyse_velocity(_run(r), pts)
    assert a["per_mode"]["B"]["code"] == "SHIFTED" and a["per_mode"]["B"]["lag_km_s"] == pytest.approx(-5.5, abs=0.4)
    assert a["per_mode"]["C"]["code"] == "SAME"
    assert a["verdict"]["code"] == "MODE_SHIFT" and "B" in a["verdict"]["title"]
    assert "warnings" not in a["verdict"]


def test_velocity_verdict_warns_when_one_mode_jumps_between_passes():
    # 19-11: same mode, B1-B3 at -8.3 and B4-B5 at -1 km/s - the shift changes with time
    rng = np.random.default_rng(4)
    r = recipes()["VEL-AB"]
    pts = []
    for c, sb in enumerate((-8.3, -1.0, -8.3)):
        pts += [{"mode": "A", "target_id": "l20", "cycle": c, "spec": spec(profile(0.0), rng)},
                {"mode": "B", "target_id": "l20", "cycle": c, "spec": spec(profile(sb), rng)},
                {"mode": "C", "target_id": "l20", "cycle": c, "spec": spec(profile(0.0), rng)}]
    a = X.analyse_velocity(_run(r), pts)
    assert a["verdict"].get("warnings") and "changes with time" in a["verdict"]["warnings"][0]


def test_velocity_needs_passes_in_every_mode():
    r = recipes()["VEL-AB"]
    a = X.analyse_velocity(_run(r), [{"mode": "A", "target_id": "l20", "cycle": 0, "spec": spec(profile())}])
    assert a["verdict"]["code"] == "INCONCLUSIVE"


def onoff_pts(on_level, off_level=0.0, n=8, noise=0.3, seed=5):
    rng = np.random.default_rng(seed)
    pts = []
    for c in range(n):
        win = (V >= 120) & (V <= 200)
        for tid, lvl in (("SMC", on_level), ("OFF-E", off_level), ("OFF-W", off_level)):
            y = np.where(win, lvl, 0.0) + rng.normal(0, noise, V.size)
            pts.append({"mode": "S", "target_id": tid, "cycle": c, "spec": {"v": V.copy(), "y": y}})
    return pts


@pytest.mark.parametrize("on, code", [(1.0, "DETECTED"), (-4.6, "BASELINE"), (0.0, "NOT_DETECTED")])
def test_onoff_verdicts(on, code):
    r = recipes()["SMC-ONOFF"]
    a = X.analyse_onoff(_run(r), onoff_pts(on, noise=0.3 if on else 0.05))
    assert a["verdict"]["code"] == code, a["verdict"]
    assert len(a["pairs"]) == 8


def test_onoff_inconclusive_says_how_many_pairs_are_missing():
    r = recipes()["SMC-ONOFF"]
    a = X.analyse_onoff(_run(r), onoff_pts(0.7, n=6, noise=6.0))
    assert a["verdict"]["code"] == "INCONCLUSIVE" and "pairs" in a["verdict"]["text"]


def test_onoff_with_too_few_pairs_does_not_opine():
    r = recipes()["SMC-ONOFF"]
    assert X.analyse_onoff(_run(r), onoff_pts(1.0, n=2))["verdict"]["code"] == "INCONCLUSIVE"


def test_line_check():
    r = recipes()["CHAIN-CHECK"]
    ok = X.analyse_line_check(_run(r), [{"mode": "A", "target_id": "l20", "cycle": 0, "spec": spec(profile())}])
    assert ok["verdict"]["code"] == "HI_OK"
    rng = np.random.default_rng(9)
    mute = X.analyse_line_check(_run(r), [{"mode": "A", "target_id": "l20", "cycle": 0, "spec": {"v": V.copy(), "y": rng.normal(0, 0.3, V.size)}}])
    assert mute["verdict"]["code"] == "NO_LINE"


# ------------------------------------------------------------------ the runner, OBSERVE/REDUCE faked

@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "RUNS_BASE", tmp_path)
    for fn in ("save_run", "load_run", "set_active", "active_id"):
        orig = getattr(X, fn)
        monkeypatch.setattr(X, fn, (lambda o: (lambda *a, **k: o(*a, **{**k, "base": tmp_path})))(orig))
    w = types.SimpleNamespace(accept=True, runs=0, plans=[], visible=True, analysed=0)
    monkeypatch.setattr(almita_experiment, "gate_reason", lambda run: None)
    monkeypatch.setattr(almita_experiment, "_observation_active", lambda: False)
    monkeypatch.setattr(almita_experiment, "_sleep", lambda s: None)
    monkeypatch.setattr(almita_experiment, "_stop", False)
    monkeypatch.setattr(X, "site_location", lambda *a: LOC)

    def fake_resolve(recipe, step, location, now=None):
        if not w.visible:
            return {"targets": [], "dropped": [{"id": t, "reason": "below 32 deg"} for t in step["planned_targets"]]}
        return {"targets": [{"id": t, "ra_hours": 1.0 + i, "dec_deg": -70.0} for i, t in enumerate(step["planned_targets"])], "dropped": []}

    import observation_orchestrator
    import observation_plan
    import almita_survey

    def fake_plan(spec, data_dir=None, **k):
        w.plans.append(spec)
        d = tmp_path / f"camp{len(w.plans)}"
        d.mkdir()
        return {"grid_session_dir": str(d), "_resolved_plan_path": str(d / "observation_resolved.json")}

    def fake_run(path, yes, device_name):
        w.runs += 1

    monkeypatch.setattr(X, "resolve_step", fake_resolve)
    monkeypatch.setattr(observation_plan, "plan_target_list", fake_plan)
    monkeypatch.setattr(observation_orchestrator, "run_observation", fake_run)
    monkeypatch.setattr(almita_survey, "run_reduce", lambda d: {"output_dir": d + "/REDUCE-1", "status": "COMPLETED"})
    monkeypatch.setattr(almita_survey, "reduced_points", lambda out: [
        {"point_index": i + 1, "ra_hours": t["ra_hours"], "dec_deg": t["dec_deg"], "accepted": w.accept}
        for i, t in enumerate(w.plans[int(out.split("camp")[1].split("/")[0]) - 1]["grid"]["targets"])])

    def fake_analyse(run):
        w.analysed += 1
        return {"verdict": {"code": "INCONCLUSIVE", "title": "t", "text": ""}}

    monkeypatch.setattr(X, "analyse", fake_analyse)
    import mount_device
    monkeypatch.setattr(mount_device, "current", lambda *a: "LX200 OnStep")
    import sdr_tuning
    monkeypatch.setattr(sdr_tuning, "operating_config", lambda *a: {"gain_db": 40.2, "center_frequency_hz": 1420405752, "sample_rate_hz": 2400000})
    return w


def _start(recipe_id):
    run = X.new_run(recipes()[recipe_id], open_sky_confirmed=True)
    X.save_run(run)
    X.set_active(run["run_id"])
    return run["run_id"]


def test_runner_runs_every_batch_in_its_mode_and_writes_the_verdict(world):
    rid = _start("VEL-AB")
    assert almita_experiment.runner() == 0
    run = X.load_run(rid)
    assert run["status"] == "DONE" and world.runs == 6 and world.analysed == 1
    caps = [p["capture"] for p in world.plans]
    assert [c.get("frequency_switching_offset_hz") for c in caps] == [None, 300000.0, 300000.0] * 2
    assert [c.get("frequency_switching_center") for c in caps] == [None, "auto", 0.0] * 2
    assert all(p["target_id"] == "l20" and p["accepted"] for s in run["steps"] for p in s["points"])
    assert all(p["rfi_ref"] == {"enabled": False} and p["execution"]["unattended"] for p in world.plans)


def test_runner_skips_a_batch_whose_targets_are_down(world):
    rid = _start("CHAIN-CHECK")
    world.visible = False
    assert almita_experiment.runner() == 0
    run = X.load_run(rid)
    assert run["steps"][0]["status"] == "SKIPPED" and "below" in run["steps"][0]["skipped_reason"] and world.runs == 0


def test_runner_halts_after_two_batches_with_nothing_accepted(world):
    rid = _start("SMC-ONOFF")
    world.accept = False
    assert almita_experiment.runner() == 2
    run = X.load_run(rid)
    assert run["status"] == "HALTED" and world.runs == 2 and "no accepted point" in run["pilot"]["note"]
    assert world.analysed == 1                     # a halted run still gets its (partial) verdict


def test_runner_stop_leaves_the_batch_captured_and_resume_reduces_it(world, monkeypatch):
    rid = _start("VEL-AB")

    def run_then_stop(path, yes, device_name):
        world.runs += 1
        monkeypatch.setattr(almita_experiment, "_stop", True)

    import observation_orchestrator
    monkeypatch.setattr(observation_orchestrator, "run_observation", run_then_stop)
    assert almita_experiment.runner() == 0
    run = X.load_run(rid)
    assert run["status"] == "STOPPED" and run["steps"][0]["status"] == "CAPTURED"
    monkeypatch.setattr(almita_experiment, "_stop", False)
    monkeypatch.setattr(observation_orchestrator, "run_observation", lambda *a, **k: None)
    (X.Path(run["steps"][0]["campaign_dir"]) / "p.h5").write_bytes(b"")
    assert almita_experiment.runner() == 0
    run = X.load_run(rid)
    assert run["status"] == "DONE" and all(s["status"] == "DONE" for s in run["steps"])


# ------------------------------------------------------------------ one driver of the mount at a time

def test_experiment_refuses_to_start_while_the_survey_pilot_runs(monkeypatch, tmp_path):
    import survey
    monkeypatch.setattr(X, "service_state", lambda: {"active": False, "state": "inactive"})
    monkeypatch.setattr(survey, "service_state", lambda: {"active": True, "state": "active"})
    with pytest.raises(RuntimeError, match="SURVEY"):
        X.start_run("CHAIN-CHECK", open_sky_confirmed=True, base=tmp_path)


def test_experiment_refuses_to_start_without_open_sky(monkeypatch, tmp_path):
    import survey
    monkeypatch.setattr(X, "service_state", lambda: {"active": False, "state": "inactive"})
    monkeypatch.setattr(survey, "service_state", lambda: {"active": False, "state": "inactive"})
    with pytest.raises(RuntimeError, match="open sky"):
        X.start_run("CHAIN-CHECK", open_sky_confirmed=False, base=tmp_path)


def test_survey_refuses_to_start_and_waits_while_an_experiment_runs(monkeypatch):
    import almita_survey
    import survey
    monkeypatch.setattr(X, "service_state", lambda: {"active": True, "state": "active"})
    monkeypatch.setattr(survey, "active_id", lambda *a, **k: "T")
    with pytest.raises(RuntimeError, match="EXPERIMENT"):
        survey.service_action("start")
    assert "EXPERIMENT" in almita_survey.gate_reason({"definition": {"allow_simulator": True}})
