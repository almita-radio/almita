"""The SURVEY pilot loop with OBSERVE/REDUCE/gates faked: credits, HALT on empty batches, stop, recovery."""
from __future__ import annotations

import types

import pytest

import almita_survey
import observation_orchestrator
import observation_plan
import survey


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(survey, "SURVEYS_DIR", tmp_path)
    for fn in ("save", "load", "set_active", "active_id"):
        orig = getattr(survey, fn)
        monkeypatch.setattr(survey, fn, (lambda o: (lambda *a, **k: o(*a, **{**k, "base": tmp_path})))(orig))
    st = survey.new_survey("T", "t", passes=1, batch_points=4, site_lat_deg=-32.25, site_lon_deg=-70.93)
    survey.save(st)
    survey.set_active("T")
    w = types.SimpleNamespace(accept=True, runs=0, plans=[])
    monkeypatch.setattr(almita_survey, "gate_reason", lambda state: None)
    monkeypatch.setattr(almita_survey, "_observation_active", lambda: False)
    monkeypatch.setattr(almita_survey, "_sleep", lambda s: None)
    monkeypatch.setattr(almita_survey, "_stop", False)

    def fake_plan(spec, data_dir=None, **k):
        w.plans.append(spec["grid"]["targets"])
        d = tmp_path / f"camp{len(w.plans)}"
        d.mkdir()
        return {"grid_session_dir": str(d), "_resolved_plan_path": str(d / "observation_resolved.json")}

    def fake_run(path, yes, device_name):
        w.runs += 1
        return {"orchestrator_state": "RUNNING"}

    def fake_reduce(campaign_dir):
        return {"output_dir": campaign_dir + "/REDUCE-1", "status": "COMPLETED"}

    def fake_points(output_dir):
        n = int(output_dir.split("camp")[1].split("/")[0])
        return [{"point_index": i + 1, "ra_hours": t["ra_hours"], "dec_deg": t["dec_deg"], "accepted": w.accept,
                 "reading": 10.0 if w.accept else None} for i, t in enumerate(w.plans[n - 1])]

    monkeypatch.setattr(observation_plan, "plan_target_list", fake_plan)
    monkeypatch.setattr(observation_orchestrator, "run_observation", fake_run)
    monkeypatch.setattr(almita_survey, "run_reduce", fake_reduce)
    monkeypatch.setattr(almita_survey, "reduced_points", fake_points)
    import mount_device
    monkeypatch.setattr(mount_device, "current", lambda *a: "LX200 OnStep")
    import sdr_tuning
    monkeypatch.setattr(sdr_tuning, "operating_config", lambda *a: {"gain_db": 40.2, "center_frequency_hz": 1420405752,
                                                                     "sample_rate_hz": 2400000})
    import astropy.time
    fixed = astropy.time.Time("2026-12-07T03:00:00")
    monkeypatch.setattr(survey.Time, "now", staticmethod(lambda: fixed))
    return w


def test_pilot_runs_until_every_visible_tile_is_credited(world, monkeypatch):
    # stop as soon as no tile is left to choose (the rest are below the floor at this frozen instant)
    real_choose = survey.choose_batch

    def choose(*a, **k):
        r = real_choose(*a, **k)
        if not r["tiles"]:
            monkeypatch.setattr(almita_survey, "_stop", True)
        return r
    monkeypatch.setattr(survey, "choose_batch", choose)
    assert almita_survey.pilot() == 0
    st = survey.load("T")
    assert world.runs >= 2
    done = [b for b in st["batches"] if b["status"] == "DONE"]
    assert done and all(b["credited"] for b in done)
    assert st["pilot"]["state"] == "STOPPED"
    assert survey.progress(st)["passes_done"] > 0


def test_pilot_halts_after_batches_with_no_accepted_point(world):
    world.accept = False
    assert almita_survey.pilot() == 2
    st = survey.load("T")
    assert st["pilot"]["state"] == "HALTED"
    assert world.runs == almita_survey.MAX_EMPTY_BATCHES
    assert survey.progress(st)["passes_done"] == 0


def test_pilot_halts_after_repeated_run_failures(world, monkeypatch):
    def boom(*a, **k):
        raise observation_orchestrator.OrchestratorError("preflight BLOCKED: mount")
    monkeypatch.setattr(observation_orchestrator, "run_observation", boom)
    assert almita_survey.pilot() == 2
    st = survey.load("T")
    assert st["pilot"]["state"] == "HALTED" and "preflight BLOCKED" in st["pilot"]["note"]
    assert [b["status"] for b in st["batches"]] == ["RUN_FAILED"] * almita_survey.MAX_FAILED_BATCHES


def test_pilot_reduces_a_captured_batch_left_from_a_stop(world, monkeypatch):
    st = survey.load("T")
    tile = st["tiles"][0]
    d = survey.SURVEYS_DIR / "camp1"
    d.mkdir()
    world.plans.append([{"ra_hours": tile["ra_hours"], "dec_deg": tile["dec_deg"], "label": tile["id"]}])
    st["batches"].append({"batch_id": "B0001", "tiles": [{"id": tile["id"]}], "campaign_dir": str(d),
                          "campaign_id": "camp1", "status": "CAPTURED"})
    survey.save(st)
    monkeypatch.setattr(almita_survey, "gate_reason", lambda state: (monkeypatch.setattr(almita_survey, "_stop", True), "stop")[1])
    almita_survey.pilot()
    st = survey.load("T")
    assert st["batches"][0]["status"] == "DONE"
    assert next(t for t in st["tiles"] if t["id"] == tile["id"])["passes_done"] == 1
