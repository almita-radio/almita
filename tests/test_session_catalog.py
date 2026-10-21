"""SESSIONS catalog + viewer (BL-081..BL-090) on a fake data/ tree in tmp_path: no hardware, the real data/ is never read
or written. The capture files must stay byte-identical through every operation; only data/sessions/ may change."""
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import session_catalog as sc
from tests import session_fixtures as fx

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def tree(tmp_path):
    paths = fx.build(tmp_path)
    return tmp_path, sc.Roots.under(tmp_path), paths


def _session(index, cid):
    return next(s for s in index["sessions"] if s["campaign_id"] == cid)


def test_indexing_twice_gives_the_same_sessions_and_never_touches_the_data(tree):
    base, roots, _ = tree
    before = fx.snapshot(base)
    first = sc.build_index(roots)
    second = sc.build_index(roots)
    for key in ("sessions", "reduce_runs", "science_runs", "pending_associations"):
        assert first[key] == second[key], key
    assert len(first["sessions"]) == len({s["campaign_id"] for s in first["sessions"]}) == 6
    assert fx.snapshot(base) == before
    assert sorted(p.name for p in roots.store.iterdir()) == ["catalog_index.json"]


def test_capture_states_kinds_and_conditions_come_only_from_recorded_evidence(tree):
    _, roots, _ = tree
    idx = sc.build_index(roots)
    a, b, c, d, t = (_session(idx, x) for x in (fx.A, fx.B, fx.C, fx.D, fx.T))
    assert (a["capture"]["state"], a["points_captured"], a["points_planned"]) == ("COMPLETED", 3, 3)
    assert b["capture"]["state"] == "STOPPED" and "stop_report.json" in b["capture"]["evidence"]
    assert c["capture"]["state"] == "NOT_RUN"
    assert d["capture"]["state"] == "UNKNOWN" and d["plan_state"] == "UNREADABLE"
    assert t["kind"] == "TEST" and a["kind"] == "OBSERVE"
    assert b["condition"]["value"] == "SIMULATOR"
    assert a["condition"]["value"] == "UNKNOWN" and "LX200" in a["condition"]["source"]    # a real mount is not "open sky"
    assert a["receiver"]["value"] is None and a["receiver"]["config_label"] == "RTL-SDR Blog V4"
    assert b["calibration"]["state"] == "INVALIDATED" and a["calibration"]["state"] == "OK"
    assert a["started_chile"] == "2026-10-18 18:27:05 (UTC-03)"


def test_associations_need_evidence_and_closeness_in_time_is_not_enough(tree):
    _, roots, p = tree
    idx = sc.build_index(roots)
    a, e = _session(idx, fx.A), _session(idx, fx.E)
    assert a["reduce"]["runs"] == [p["r1"].name, p["r2"].name, p["r3"].name]          # r3: no manifest, linked through its points
    r3 = next(r for r in idx["reduce_runs"] if r["run_id"] == p["r3"].name)
    assert r3["status"] == "INTERRUPTED" and "capture_refs" in r3["link_evidence"]
    assert e["reduce"]["runs"] == [] and e["science"]["runs"] == []                      # 3 min after A: nothing inherited
    sci = {s["run_id"]: s for s in idx["science_runs"]}
    assert sci[p["s1"].name]["link_state"] == "LINKED" and sci[p["s3"].name]["link_state"] == "INCONSISTENT"
    assert sci[p["bad"].name]["status"] == "INTERRUPTED" and sci[p["bad"].name]["link_state"] == "PENDING"
    assert set(a["science"]["runs"]) == {p["s1"].name, p["s2"].name, p["s3"].name}
    pending = {x["run_id"]: x for x in idx["pending_associations"]}
    assert pending[p["manual"].name]["candidates"] == [fx.A]                               # a candidate only, by its exact capture id
    assert pending[p["r4"].name]["kind"] == "REDUCE" and "not on disk" in pending[p["r4"].name]["reason"]
    assert p["manual"].name not in a["science"]["runs"]


def test_several_reduce_and_science_runs_stay_apart_and_preferred_marks_change_no_result_file(tree):
    base, roots, p = tree
    before = fx.snapshot(base)
    sc.annotate(fx.A, {"preferred_reduce": p["r2"].name, "preferred_science": {p["r1"].name: p["s2"].name}}, roots)
    d = sc.session_detail(fx.A, roots)
    runs = {r["run_id"]: r for r in d["sections"]["reduce"]["runs"]}
    assert runs[p["r2"].name]["preferred"] and not runs[p["r1"].name]["preferred"]
    assert sorted(runs[p["r1"].name]["science_runs"]) == sorted([p["s1"].name, p["s2"].name])
    assert runs[p["r2"].name]["science_runs"] == [p["s3"].name]
    sci = {s["run_id"]: s for s in d["sections"]["science"]["runs"]}
    assert sci[p["s2"].name]["preferred"] and not sci[p["s1"].name]["preferred"] and sci[p["s3"].name]["reduce_run_id"] == p["r2"].name
    with pytest.raises(ValueError):
        sc.annotate(fx.A, {"preferred_science": {p["r1"].name: p["s3"].name}}, roots)    # s3 belongs to r2
    with pytest.raises(ValueError):
        sc.annotate(fx.E, {"preferred_reduce": p["r1"].name}, roots)                     # not E's run
    assert fx.snapshot(base) == before


def test_annotations_survive_rebuilding_the_index_and_orphans_are_kept(tree):
    base, roots, _ = tree
    sc.annotate(fx.A, {"name": "Vía Láctea 18 oct", "notes": "first V3 night", "tags": ["V3", "night"], "favorite": True,
                       "condition": {"value": "INDOOR", "note": "antenna on the balcony floor"}, "receiver": {"value": "RTL-SDR Blog V3", "note": "serial 00000001"}}, roots)
    (roots.store / "catalog_index.json").unlink()
    sc.build_index(roots)
    listing = sc.list_sessions({"q": "láctea"}, roots)
    assert [s["campaign_id"] for s in listing["sessions"]] == [fx.A]
    row = listing["sessions"][0]
    assert row["display_name"] == "Vía Láctea 18 oct" and row["condition"]["value"] == "INDOOR" and row["condition"]["documented_by"] == "operator"
    assert row["receiver"]["value"] == "RTL-SDR Blog V3" and listing["tags"] == ["V3", "night"]
    log = (roots.store / "annotations_log.jsonl").read_text().splitlines()
    assert len(log) == 1 and json.loads(log[0])["campaign_id"] == fx.A
    import shutil
    shutil.rmtree(base / "data" / "mosaic" / fx.A)
    sc.build_index(roots)
    assert sc.list_sessions({}, roots)["orphan_annotations"] == [fx.A]
    assert fx.A in sc.load_annotations(roots)["sessions"]


def test_annotation_rules(tree):
    _, roots, _ = tree
    for bad in ({"discarded": {"reason": "  "}}, {"condition": {"value": "CLOUDY"}}, {"tags": ["<b>"]}, {"name": "x" * 121},
                {"archived": "yes"}, {"unknown_field": 1}, {}):
        with pytest.raises(ValueError):
            sc.annotate(fx.A, bad, roots)
    e = sc.annotate(fx.A, {"discarded": {"reason": "LNA connector intermittent"}, "archived": True}, roots)
    assert e["discarded"]["reason"] == "LNA connector intermittent"
    assert sc.list_sessions({}, roots)["total"] == 5                       # archived hidden by default
    only = sc.list_sessions({"archived": "only"}, roots)["sessions"]
    assert [s["campaign_id"] for s in only] == [fx.A] and only[0]["annotation"]["discarded"]
    assert sc.annotate(fx.A, {"discarded": None}, roots)["discarded"] is None
    with pytest.raises(FileNotFoundError):
        sc.annotate("NOPE-20260101-00:00:00", {"favorite": True}, roots)
    with pytest.raises(ValueError):
        sc.annotate("../etc", {"favorite": True}, roots)


def test_listing_filters_sort_and_pages(tree):
    _, roots, _ = tree
    sc.build_index(roots)
    assert [s["campaign_id"] for s in sc.list_sessions({"state": "STOPPED"}, roots)["sessions"]] == [fx.B]
    assert [s["campaign_id"] for s in sc.list_sessions({"kind": "TEST"}, roots)["sessions"]] == [fx.T]
    assert [s["campaign_id"] for s in sc.list_sessions({"condition": "SIMULATOR"}, roots)["sessions"]] == [fx.B]
    assert [s["campaign_id"] for s in sc.list_sessions({"reduce": "COMPLETED"}, roots)["sessions"]] == [fx.A]
    invalid = sc.list_sessions({"calibration": "INVALIDATED"}, roots)["sessions"]
    assert fx.B in [s["campaign_id"] for s in invalid] and fx.A not in [s["campaign_id"] for s in invalid]
    assert all(s["calibration"]["state"] == "INVALIDATED" for s in invalid)
    newest = sc.list_sessions({"sort": "started"}, roots)["sessions"]
    assert newest[0]["campaign_id"] == fx.E and newest[-1]["campaign_id"] in (fx.D, fx.T)
    p1 = sc.list_sessions({"per_page": "5", "page": "1"}, roots)
    p2 = sc.list_sessions({"per_page": "5", "page": "2"}, roots)
    assert p1["pages"] == 2 and len(p1["sessions"]) == 5 and len(p2["sessions"]) == 1
    assert not {s["campaign_id"] for s in p1["sessions"]} & {s["campaign_id"] for s in p2["sessions"]}
    with pytest.raises(ValueError):
        sc.list_sessions({"page": "x"}, roots)


def test_incomplete_and_broken_sessions_open_and_say_what_is_missing(tree):
    base, roots, _ = tree
    sc.build_index(roots)
    (base / fx.PROFILE_A).unlink()                                         # the profile a session points to disappears
    (base / "data" / "mosaic" / fx.A / "quicklook" / "quicklook_map.png").unlink()
    d = sc.session_detail(fx.D, roots)
    assert d["sections"]["observation"]["plan_state"] == "UNREADABLE" and d["points_state"] == "MISSING"
    assert d["sections"]["quicklook"]["state"] == "NOT_RUN" and d["sections"]["reduce"]["runs"] == []
    a = sc.session_detail(fx.A, roots)
    assert a["sections"]["calibration"]["profile"]["state"] == "MISSING"
    products = {p["label"]: p["state"] for p in a["sections"]["quicklook"]["products"]}
    assert products["native grid map"] == "MISSING" and products["session waterfall"] == "OK"
    b = sc.session_detail(fx.B, roots)
    kinds = [i["kind"] for i in b["sections"]["summary"]["incidents"]]
    assert "STOP" in kinds and "PROFILE" in kinds
    t = sc.session_detail(fx.T, roots)
    assert any(i["kind"] == "POINT" and "boom" in i["detail"] for i in t["sections"]["summary"]["incidents"])
    with pytest.raises(FileNotFoundError):
        sc.session_detail("NOPE-20260101-00:00:00", roots)


def test_points_temperatures_and_files(tree):
    _, roots, _ = tree
    sc.build_index(roots)
    pts = sc.session_points(fx.A, roots)
    assert [p["capture_status"] for p in pts["points"]] == ["success"] * 3
    pd = sc.point_detail(fx.A, "1", roots)
    assert pd["capture"]["state"] == "OK" and pd["capture"]["path"].endswith("_0001.h5")
    assert [r["state"] for r in pd["reduce_points"]] == ["OK", "OK", "OK"]
    assert sc.point_detail(fx.A, "2", roots)["reduce_points"][2]["state"] == "MISSING"   # r3 stopped after point 1
    assert sc.point_detail(fx.B, "4", roots)["capture"]["state"] == "NOT_RUN"
    t = sc.session_temperatures(fx.A, roots)
    assert t["state"] == "OK" and t["stats"]["sdr"]["valid"] > 0 and t["stats"]["lna"]["valid"] == 0
    assert 0 < t["coverage"]["fraction"] <= 1 and "temperature_logger" in t["source"]
    assert sc.session_temperatures(fx.B, roots)["state"] == "MISSING"                  # no log on 2026-10-17 in the fixture
    assert sc.session_temperatures(fx.C, roots)["state"] == "NOT_RUN"
    f = sc.session_files(fx.A, "", roots)
    assert {d["name"] for d in f["dirs"]} == {"data", "quicklook"} and len(f["products"]) == 6
    deep = sc.session_files(fx.A, "data/iq/" + fx.A + "x", roots)
    h5 = [x for x in deep["files"] if x["name"].endswith(".h5")]
    assert h5 and not any(x["downloadable"] for x in h5)
    with pytest.raises(ValueError):
        sc.session_files(fx.A, "../../..", roots)


def test_disk_usage_counts_a_hard_link_once_and_never_follows_symlinks(tmp_path):
    (tmp_path / "d").mkdir()
    f = tmp_path / "d" / "a.bin"
    f.write_bytes(b"x" * 10000)
    (tmp_path / "d" / "b.bin").hardlink_to(f)
    (tmp_path / "d" / "link").symlink_to("/usr")
    u = sc.disk_usage(tmp_path / "d")
    assert u["files"] == 1 and u["hardlinks_counted_once"] == 1 and u["symlinks"] == 1 and u["apparent_bytes"] == 10000


def test_export_has_no_iq_no_secret_and_traces_every_figure(tree, monkeypatch):
    base, roots, p = tree
    fake_home = base / "home"
    auth = fake_home / ".config" / "almita" / "web_auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"user": "felipe", "salt": "SALT-123", "hash": "HASH-456"}))
    monkeypatch.setenv("HOME", str(fake_home))
    sc.build_index(roots)
    before = fx.snapshot(base)
    with pytest.raises(ValueError):
        sc.export_report(fx.A, roots, include_iq=True)
    out = sc.export_report(fx.A, roots)
    assert fx.snapshot(base) == before                                      # only data/sessions/exports changed
    with zipfile.ZipFile(base / out["zip"]) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)
    assert not [n for n in names if n.endswith(".h5")] and not [n for n in names if "secret" in n or "auth" in n]
    assert b"SALT-123" not in blob and b"HASH-456" not in blob and b"IQ-NOT-HDF5" not in blob
    manifest = json.loads((base / out["manifest"]).read_text())
    assert manifest["includes_iq"] is False and manifest["campaign_id"] == fx.A
    assert all(f["sha256"] and f["source"].startswith("data/") for f in manifest["figures"])
    assert {r["run_id"] for r in manifest["reduce_runs"]} == {p["r1"].name, p["r2"].name, p["r3"].name}
    assert any("Sky condition not recorded" in w for w in manifest["warnings"])
    report = (base / out["report"]).read_text()
    assert "IQ captures are not included" in report and fx.A in report
    assert sc.list_exports(fx.A, roots)[0]["zip"] == out["zip"]
    path, ctype = sc.resolve_export(out["zip"], roots)
    assert ctype == "application/zip"
    for bad in ("data/mosaic/" + fx.A + "/mosaic.csv", "../etc/passwd", out["dir"] + "/../../annotations.json"):
        with pytest.raises((FileNotFoundError, ValueError)):
            sc.resolve_export(bad, roots)


def test_compare_flags_what_blocks_a_direct_comparison(tree):
    _, roots, p = tree
    sc.build_index(roots)
    c = sc.compare_sessions(fx.A, fx.B, roots)
    assert not c["direct_comparison"] and any(b.startswith("gain") for b in c["blockers"])
    assert any(b.startswith("calibration profile") for b in c["blockers"])
    assert "receiver" in c["unknown"] and not next(f for f in c["fields"] if f["field"] == "receiver")["same"]   # unknown != same
    same = sc.compare_sessions(fx.A, fx.E, roots)
    assert not any(b.startswith("gain") for b in same["blockers"])
    r = sc.compare_runs(fx.A, p["r1"].name, p["r2"].name, roots)
    assert r["kind"] == "REDUCE" and "velocity_frame" in r["blockers"]
    assert {d["key"] for d in r["config_diff"]} == {"velocity_frame"}
    assert r["common_points"] == [1, 2, 3]
    assert c["a"]["science"]["maps"] == ["map_abc_combined.png", "secret_token.png"] and c["b"]["science"] is None
    sr = sc.compare_runs(fx.A, p["s1"].name, p["s2"].name, roots)
    assert sr["kind"] == "SCIENCE" and sr["maps"]["a"] and sr["common_points"] == []
    with pytest.raises(ValueError):
        sc.compare_runs(fx.A, p["r1"].name, p["s1"].name, roots)
    with pytest.raises(ValueError):
        sc.compare_runs(fx.E, p["r1"].name, p["r2"].name, roots)


def test_opening_a_session_keeps_the_live_session_untouched_and_loads_no_hardware_module(tree):
    base, roots, _ = tree
    code = (
        "import sys, json, session_catalog as sc\n"
        f"r = sc.Roots.under({str(base)!r})\n"
        "sc.build_index(r); sc.session_detail(%r, r); sc.session_points(%r, r); sc.point_detail(%r, '1', r)\n"
        "sc.session_temperatures(%r, r); sc.session_files(%r, '', r); sc.list_sessions({}, r)\n"
        "bad = [m for m in ('indi_telescope_control', 'sdr_capture', 'capture', 'mount_control', 'mount_stop', 'h5py', 'rtl_tcp_client') if m in sys.modules]\n"
        "print(json.dumps(bad))\n") % ((fx.A,) * 5)
    live = (base / "data" / "runtime" / "current_session.json").read_bytes()
    run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout.strip().splitlines()[-1]) == []
    assert (base / "data" / "runtime" / "current_session.json").read_bytes() == live


def test_every_annotation_change_keeps_a_copy_of_the_previous_file(tree, monkeypatch):
    _, roots, _ = tree
    monkeypatch.setattr(sc, "ANNOTATION_BACKUPS", 3)
    sc.annotate(fx.A, {"name": "first"}, roots)                            # nothing to copy yet
    assert not (roots.store / "backups").exists()
    for i in range(5):
        sc.annotate(fx.A, {"name": f"v{i}"}, roots)
    copies = sorted((roots.store / "backups").glob("annotations-*.json"))
    assert len(copies) == 3
    assert json.loads(copies[-1].read_text())["sessions"][fx.A]["name"] == "v3"     # the state just before the last change
    assert sc.load_annotations(roots)["sessions"][fx.A]["name"] == "v4"


def test_an_unreadable_annotations_file_is_never_overwritten(tree):
    _, roots, _ = tree
    roots.store.mkdir(parents=True)
    (roots.store / "annotations.json").write_text("{broken")
    with pytest.raises(ValueError, match="unreadable"):
        sc.annotate(fx.A, {"favorite": True}, roots)
    assert (roots.store / "annotations.json").read_text() == "{broken"
