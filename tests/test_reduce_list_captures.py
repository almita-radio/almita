"""REDUCE's single-capture list (2026-10-17): capture.py writes <campaign>/data/iq/<session>/<name>.h5 and the old
"*/data/iq/*.h5" pattern listed none of them - only CALIBRATE captures showed. Files only, in tmp_path."""
import os

import almita_web_ops as ops


def test_observe_captures_in_their_session_directory_are_listed(tmp_path, monkeypatch):
    mosaic, cal = tmp_path / "data" / "mosaic", tmp_path / "data" / "calibration"
    nested = mosaic / "ALMITA-OBSERVE-20261017-07:36:37" / "data" / "iq" / "ALMITA-OBSERVE-20261017-07:41:00"
    nested.mkdir(parents=True)
    (nested / "ALMITA-OBSERVE_0001.h5").write_bytes(b"x")
    (nested / "ALMITA-OBSERVE_0002.h5.part").write_bytes(b"x")            # still being written: never listed
    flat = mosaic / "OLD" / "data" / "iq"; flat.mkdir(parents=True)
    (flat / "old_0001.h5").write_bytes(b"x")
    wiz = cal / "WIZARD-1" / "captures" / "AMBIENT_50R"; wiz.mkdir(parents=True)
    (wiz / "capture_000.h5").write_bytes(b"x")
    os.utime(wiz / "capture_000.h5", (1, 1))                               # oldest: listed last
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    monkeypatch.setitem(ops.SERVE_ROOTS, "mosaic", mosaic)
    monkeypatch.setitem(ops.SERVE_ROOTS, "calibration", cal)
    paths = [r["path"] for r in ops.reduce_list_captures()]
    assert "data/mosaic/ALMITA-OBSERVE-20261017-07:36:37/data/iq/ALMITA-OBSERVE-20261017-07:41:00/ALMITA-OBSERVE_0001.h5" in paths
    assert "data/mosaic/OLD/data/iq/old_0001.h5" in paths
    assert not any(p.endswith(".part") for p in paths)
    assert paths[-1] == "data/calibration/WIZARD-1/captures/AMBIENT_50R/capture_000.h5" and len(paths) == 3


def test_each_listed_capture_says_its_campaign_date_grid_and_point(tmp_path, monkeypatch):
    import json
    mosaic, cal = tmp_path / "data" / "mosaic", tmp_path / "data" / "calibration"
    camp = mosaic / "ALMITA-OBSERVE-20261017-07:36:37"
    (camp / "data" / "iq" / "S").mkdir(parents=True)
    (camp / "data" / "iq" / "S" / "ALMITA-OBSERVE_0009.h5").write_bytes(b"x")
    (camp / "observation_resolved.json").write_text(json.dumps({"observation_name": "ALMITA-OBSERVE", "planning_timestamp_utc": "2026-10-17T07:36:37.076Z",
        "resolved": {"rows": 3, "cols": 3, "point_count": 9, "footprint_width_deg": 5.0, "footprint_height_deg": 5.0, "spacing_deg": 2.5}}))
    bare = mosaic / "via_lactea-20261015-20:55:13" / "data" / "iq"; bare.mkdir(parents=True)          # no resolved plan on disk
    (bare / "via_lactea_0024.h5").write_bytes(b"x")
    wiz = cal / "WIZARD-20261013-193915-787403" / "captures" / "AMBIENT_50R"; wiz.mkdir(parents=True)
    (wiz / "capture_003.h5").write_bytes(b"x")
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    monkeypatch.setitem(ops.SERVE_ROOTS, "mosaic", mosaic)
    monkeypatch.setitem(ops.SERVE_ROOTS, "calibration", cal)
    rows = {r["name"]: r for r in ops.reduce_list_captures()}
    o = rows["ALMITA-OBSERVE_0009.h5"]
    assert (o["kind"], o["campaign"], o["item"], o["campaign_started_utc"]) == ("OBSERVE", "ALMITA-OBSERVE", "point 0009", "2026-10-17T07:36:37.076Z")
    assert o["grid"]["rows"] == 3 and o["grid"]["footprint_width_deg"] == 5.0
    b = rows["via_lactea_0024.h5"]
    assert b["grid"] is None and b["campaign_started_utc"].startswith("2026-10-15T20:55:13")    # date from the directory name
    c = rows["capture_003.h5"]
    assert (c["kind"], c["item"], c["campaign_started_utc"]) == ("CALIBRATION", "AMBIENT_50R / capture_003", "2026-10-13T19:39:15+00:00")
