"""MONITOR's SKY - CURRENT RUN: the running campaign's points with their state (no hardware, temporary files)."""
import csv
import json
from pathlib import Path

import almita_web_ops as ops

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("point_number", "point_id", "scan_order", "target_ra_hours", "target_dec_degrees", "capture_status")


def _campaign(tmp_path, statuses, running=True, current="3"):
    mosaic = tmp_path / "mosaic"
    camp = mosaic / "ALMITA-OBSERVE-TEST"
    (camp / "data" / "iq" / "ALMITA-OBSERVE-RUN").mkdir(parents=True)
    with open(camp / "mosaic.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS); w.writeheader()
        for i, st in enumerate(statuses, 1):
            w.writerow({"point_number": i, "point_id": i, "scan_order": i, "target_ra_hours": 6 + i * 0.05,
                        "target_dec_degrees": -30, "capture_status": st})
    (camp / "observation_resolved.json").write_text(json.dumps({"requested": {"grid": {"min_altitude_deg": 25}}}))
    rt = tmp_path / "runtime"; rt.mkdir()
    (rt / "current_session.json").write_text(json.dumps({"state": "RUNNING" if running else "ABORTED", "current_point_id": current,
        "session_root": "data/mosaic/ALMITA-OBSERVE-TEST/data/iq/ALMITA-OBSERVE-RUN"}))
    (rt / "last_session.json").write_text(json.dumps({"campaign_id": "ALMITA-OBSERVE-TEST", "final_state": "ABORTED"}))
    return rt, mosaic


def test_states_follow_capture_rows_and_the_current_point(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "sky_layer_now", lambda *a, **k: {})
    ops._RUN_SKY_CACHE.clear()
    rt, mosaic = _campaign(tmp_path, ["success", "success", "capturing", "planned", "failed"])
    r = ops.observe_sky_progress(rt, mosaic)
    assert r["available"] and r["running"] and r["min_altitude_deg"] == 25
    assert [p["state"] for p in r["points"]] == ["done", "done", "current", "pending", "failed"]
    assert r["counts"] == {"done": 2, "current": 1, "failed": 1, "pending": 1}


def test_a_stopped_run_has_no_current_point(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "sky_layer_now", lambda *a, **k: {})
    ops._RUN_SKY_CACHE.clear()
    rt, mosaic = _campaign(tmp_path, ["success", "capturing", "planned"], running=False)
    r = ops.observe_sky_progress(rt, mosaic)
    assert not r["running"] and "current" not in [p["state"] for p in r["points"]]


def test_no_campaign_is_an_answer_not_an_error(tmp_path):
    (tmp_path / "rt").mkdir()
    assert ops.observe_sky_progress(tmp_path / "rt", tmp_path / "mosaic")["available"] is False


def test_monitor_has_the_panel_and_the_map_draws_states_and_the_red_x():
    html = (ROOT / "console" / "index.html").read_text()
    app = (ROOT / "console" / "app.js").read_text()
    common = (ROOT / "console" / "common.js").read_text()
    assert 'id="run-sky"' in html and "initRunSky()" in app and '"/api/observe/sky"' in app
    assert 'p.state === "done"' in common and '"#ff2d2d"' in common
