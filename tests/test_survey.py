"""SURVEY (2026-11-19): tiles, batch choice, pass bookkeeping, TARGET_LIST planning. No hardware, no processes."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

import pytest

import almita_survey
import observation_plan
import survey

PETORCA = dict(site_lat_deg=-32.25, site_lon_deg=-70.93)
NIGHT = datetime(2026, 12, 7, 3, 0, tzinfo=timezone.utc)     # 00:00 Chile, Petorca trip


def _sep(a, b):
    return survey._sep_deg(a["ra_hours"], a["dec_deg"], b["ra_hours"], b["dec_deg"])


def test_ring_tiles_around_the_pole_are_evenly_spaced():
    tiles = survey.ring_tiles(0.0, -90.0, 30.0, 7.5)
    assert len(tiles) == 1 + 6 + 13 + 19 + 25
    assert sorted({round(90 + t["dec_deg"], 3) for t in tiles}) == [0.0, 7.5, 15.0, 22.5, 30.0]
    nn = [min(_sep(a, b) for b in tiles if b is not a) for a in tiles]
    assert min(nn) > 6.5 and max(nn) <= 7.5 + 1e-6
    assert len({t["id"] for t in tiles}) == len(tiles)


def test_ring_tiles_off_the_pole_keep_their_distance_to_the_centre():
    tiles = survey.ring_tiles(5.4, -69.8, 15.0, 5.0)        # around the LMC
    centre = {"ra_hours": 5.4, "dec_deg": -69.8}
    for t in tiles:
        assert abs(_sep(t, centre) - 5.0 * t["ring"]) < 1e-4


def test_new_survey_rejects_bad_parameters():
    with pytest.raises(survey.SurveyError):
        survey.new_survey("bad id!", "x")
    with pytest.raises(survey.SurveyError):
        survey.new_survey("OK", "x", spacing_deg=0.0)
    with pytest.raises(survey.SurveyError):
        survey.new_survey("OK", "x", bogus=1)
    with pytest.raises(survey.SurveyError):
        survey.new_survey("OK", "x", allow_simulator="yes")


def test_choose_batch_respects_floor_sun_and_waves():
    st = survey.new_survey("T", "t", **PETORCA)
    loc = survey.site_location(st)
    b = survey.choose_batch(st, loc, now=NIGHT)
    assert b["tiles"][0]["id"] == "C"                      # the check tile opens the batch
    assert len(b["tiles"]) == st["definition"]["batch_points"] + 1
    assert all(t["alt_now"] >= st["definition"]["min_altitude_deg"] + survey.ALT_MARGIN_DEG for t in b["tiles"])
    # complete wave 1 for every tile but one: that one must come first in the next batch (after the check tile)
    for t in st["tiles"]:
        t["passes_done"] = 1
    lagging = next(t for t in st["tiles"] if t["id"] == "R1-01")
    lagging["passes_done"] = 0
    b2 = survey.choose_batch(st, loc, now=NIGHT)
    assert "R1-01" in [t["id"] for t in b2["tiles"]]


def test_complete_survey_has_nothing_to_do():
    st = survey.new_survey("T", "t", passes=1, **PETORCA)
    for t in st["tiles"]:
        t["passes_done"] = 1
    assert survey.progress(st)["complete"]
    assert survey.choose_batch(st, survey.site_location(st), now=NIGHT)["reason"] == "survey complete"


def test_record_batch_credits_only_accepted_points_near_their_tile():
    st = survey.new_survey("T", "t", **PETORCA)
    a, b = st["tiles"][1], st["tiles"][2]
    batch = {"tiles": [{"id": a["id"]}, {"id": b["id"]}], "campaign_id": "X", "ended_utc": "2026-12-07T03:10:00Z"}
    pts = [{"ra_hours": a["ra_hours"], "dec_deg": a["dec_deg"] + 0.5, "accepted": True, "reading": 12.0, "point_index": 1},
           {"ra_hours": b["ra_hours"], "dec_deg": b["dec_deg"], "accepted": False, "reading": None, "point_index": 2},
           {"ra_hours": 12.0, "dec_deg": 40.0, "accepted": True, "reading": 1.0, "point_index": 3}]
    r = survey.record_batch(st, batch, pts)
    assert r["credited"] == [a["id"]] and r["missed"] == [b["id"]] and r["unmatched"] == 1
    assert a["passes_done"] == 1 and a["attempts"] == 1 and b["passes_done"] == 0 and b["attempts"] == 1
    assert a["readings"][0]["reading"] == 12.0


def test_save_load_and_active(tmp_path):
    st = survey.new_survey("SCP", "polo", **PETORCA)
    survey.save(st, base=tmp_path)
    survey.set_active("SCP", base=tmp_path)
    assert survey.active_id(base=tmp_path) == "SCP"
    assert survey.load("SCP", base=tmp_path)["tiles"] == st["tiles"]
    assert survey.list_surveys(base=tmp_path)[0]["tiles"] == 64


def test_simulated_petorca_survey_completes_in_about_a_day():
    st = survey.new_survey("SIM", "x", **PETORCA)
    r = almita_survey.simulate(st, datetime(2026, 12, 7, 0, 0, tzinfo=timezone.utc), 72)
    assert r["progress"]["complete"]
    assert 12 <= r["hours_to_complete"] <= 36


def test_target_list_plan_writes_csv_metadata_and_a_checkable_hash(tmp_path):
    st = survey.new_survey("T", "t")
    tiles = [{"id": t["id"], "ra_hours": t["ra_hours"], "dec_deg": t["dec_deg"]} for t in st["tiles"][:4]]
    spec = survey.batch_spec(st, tiles, gain_db=40.2)
    # plan at a time when the pole cap is above the floor at the configured site (or skip if it never is there)
    try:
        plan = observation_plan.plan_target_list(spec, data_dir=str(tmp_path))
    except observation_plan.ObservationPlanError:
        pytest.skip("pole cap below the floor at observer_config's site")
    assert plan["resolved"]["mode"] == "TARGET_LIST" and plan["resolved"]["point_count"] == 4
    assert observation_plan.recompute_config_hash(plan) == plan["observation_config_sha256"]
    rows = list(csv.DictReader(open(plan["mosaic_csv_path"])))
    assert [round(float(r["target_dec_degrees"]), 4) for r in rows] == [round(t["dec_deg"], 4) for t in tiles]
    meta = json.loads((tmp_path / plan["grid_session_dir"].split("/")[-1] / "grid_metadata.json").read_text())
    assert meta["grid"]["mode"] == "TARGET_LIST" and meta["grid"]["targets"] == [t["id"] for t in tiles]
    tampered = json.loads(json.dumps(plan))
    tampered["resolved"]["targets"][0]["dec_deg"] += 1.0
    assert observation_plan.recompute_config_hash(tampered) != plan["observation_config_sha256"]


def test_target_list_spec_validation_is_strict():
    st = survey.new_survey("T", "t")
    spec = survey.batch_spec(st, [{"id": "C", "ra_hours": 0.0, "dec_deg": -90.0}], gain_db=40.2)
    spec["grid"]["targets"][0]["dec_deg"] = -95.0
    with pytest.raises(Exception):
        observation_plan.validate_target_list_spec(spec)
    spec = survey.batch_spec(st, [{"id": "C", "ra_hours": 0.0, "dec_deg": -90.0}], gain_db=40.2)
    spec["grid"]["extra"] = 1
    with pytest.raises(Exception):
        observation_plan.validate_target_list_spec(spec)
