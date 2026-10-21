"""SESSIONS deletion (BL-093) on a fake data/ tree in tmp_path only: never the real data/. Every test checks what must
survive byte for byte, not only what goes."""
import json
import os
import threading
from pathlib import Path

import pytest

import session_catalog as sc
import session_deletion as sd
from tests import session_fixtures as fx

P = "ALMITA-OBSERVE-20261002-01:45:08"          # the preserved 400-point campaign (by name)
IDLE = lambda: []                                # noqa: E731 - no pipeline activity
BUSY = lambda: ["job REDUCE-20261021-000000-abcd (reduce) is running"]   # noqa: E731


@pytest.fixture()
def tree(tmp_path):
    paths = fx.build(tmp_path)
    for prof in (fx.PROFILE_A, fx.PROFILE_B):                   # profiles complete (.json + .npz): A loadable, B invalidated
        (tmp_path / prof).with_suffix(".npz").write_bytes(b"npz")
    fx._campaign(tmp_path, P, "2026-10-02T01:45:08.000Z", ["success", "success"], fx.PROFILE_A)
    preserved_run = fx._reduce(tmp_path, "ALMITA-OBSERVE", "REDUCE-20261002-213042-301170", P, points=(1, 2))
    fx._science(tmp_path, "ALMITA-OBSERVE", "SCIENCE_WEB-20261005-230737-640582", preserved_run)
    outside = tmp_path / "data" / "hardware_tests"
    outside.mkdir(parents=True)
    (outside / "precious.bin").write_bytes(b"keep me" * 100)
    e_iq = next((tmp_path / "data" / "mosaic" / fx.E / "data" / "iq").iterdir())
    os.link(outside / "precious.bin", e_iq / "hardlinked.bin")                   # an inode shared with a file outside
    os.symlink(outside / "precious.bin", tmp_path / "data" / "mosaic" / fx.E / "link_to_outside")
    roots = sc.Roots.under(tmp_path)
    sc.build_index(roots)
    return tmp_path, roots, {**paths, "preserved_run": preserved_run, "outside": outside}


def _keep_snapshot(base, *excluded_prefixes):
    snap = fx.snapshot(base, skip="data/sessions")
    return {k: v for k, v in snap.items() if not k.startswith(excluded_prefixes) and not k.endswith("sessions_deletion.lock")}


def _row(p, cid):
    return next(r for r in p["sessions"] if r["campaign_id"] == cid)


def test_preview_and_cancel_change_nothing(tree):
    base, roots, _ = tree
    before = fx.snapshot(base)
    p = sd.plan([fx.A, fx.B, fx.C], "sessions", None, roots, IDLE)
    assert p["deletable"] == 3 and p["confirm_label"] == "DELETE 3 SESSIONS — IRREVERSIBLE"
    assert p["space"]["recoverable_bytes"] > 0 and "estimate_note" in p["space"]
    assert _row(p, fx.C)["capture_state"] == "NOT_RUN" and _row(p, fx.C)["files"] > 0       # a plan without captures is deletable
    assert fx.snapshot(base) == before                     # previewing (and then cancelling: no call at all) touches nothing
    assert "_internal" not in sd.public_plan(p) and all("inventory" not in i for r in sd.public_plan(p)["sessions"] for i in r["items"])


def test_products_only_keeps_captures_notes_and_what_is_not_regenerable(tree):
    base, roots, p = tree
    sc.annotate(fx.A, {"notes": "keep this note", "preferred_reduce": p["r1"].name}, roots)
    camp = base / "data" / "mosaic" / fx.A
    keep = _keep_snapshot(base, f"data/mosaic/{fx.A}/quicklook", f"data/reduced/ALMITA-OBSERVE/{p['r1'].name}",
                          f"data/reduced/ALMITA-OBSERVE/{p['r3'].name}", f"data/science/ALMITA-OBSERVE/{p['s1'].name}",
                          f"data/science/ALMITA-OBSERVE/{p['s2'].name}")
    plan = sd.plan([fx.A, fx.B], "products", ["QUICKLOOK", "REDUCE", "SCIENCE"], roots, IDLE)
    a = _row(plan, fx.A)
    paths = {i["path"] for i in a["items"]}
    assert f"data/mosaic/{fx.A}/quicklook" in paths and str(p["r1"].relative_to(base)) in paths and str(p["s1"].relative_to(base)) in paths
    kept = {k["path"]: k["why"] for k in a["kept"]}
    assert "INCONSISTENT" in kept[str(p["s3"].relative_to(base))]                        # ambiguous SCIENCE stays
    assert "input of SCIENCE" in kept[str(p["r2"].relative_to(base))]                    # ...and so does its REDUCE
    assert "manual" in kept[str(p["manual"].relative_to(base))]                          # manual analysis never regenerable
    b = _row(plan, fx.B)
    assert not b["items"] and any("not regenerable" in k["why"] and "INVALIDATED" in k["why"] for k in b["kept"])
    out = sd.execute([fx.A, fx.B], "products", ["QUICKLOOK", "REDUCE", "SCIENCE"], plan["token"], roots, IDLE)
    assert [s["status"] for s in out["sessions"]] == ["DELETED", "NOTHING TO DELETE"]
    assert not (camp / "quicklook").exists() and not p["r1"].exists() and not p["s1"].exists() and p["r2"].exists() and p["s3"].exists()
    assert (camp / "mosaic.csv").is_file() and any((camp / "data" / "iq").rglob("*.h5"))
    assert _keep_snapshot(base, f"data/mosaic/{fx.A}/quicklook", f"data/reduced/ALMITA-OBSERVE/{p['r1'].name}",
                          f"data/reduced/ALMITA-OBSERVE/{p['r3'].name}", f"data/science/ALMITA-OBSERVE/{p['s1'].name}",
                          f"data/science/ALMITA-OBSERVE/{p['s2'].name}") == keep
    ann = sc.load_annotations(roots)["sessions"][fx.A]
    assert ann["notes"] == "keep this note" and ann["preferred_reduce"] is None               # stale mark cleared, note kept
    evidence = list((base / out["audit_dir"]).rglob("config.json"))
    assert len(evidence) >= 3                                                                  # parameters to regenerate kept
    d = sc.session_detail(fx.A, roots)
    assert d["sections"]["quicklook"]["state"] == "NOT_RUN" and {r["run_id"] for r in d["sections"]["reduce"]["runs"]} == {p["r2"].name}


def test_whole_sessions_go_with_their_own_runs_and_leave_no_ghost(tree):
    base, roots, p = tree
    sc.annotate(fx.B, {"name": "simulator night", "tags": ["sim"]}, roots)
    profile = (base / fx.PROFILE_B).read_bytes()
    keep = _keep_snapshot(base, f"data/mosaic/{fx.B}", f"data/mosaic/{fx.C}")
    plan = sd.plan([fx.B, fx.C], "sessions", None, roots, IDLE)
    assert any("calibration profile" in k["why"] for k in plan["kept_resources"])
    out = sd.execute([fx.B, fx.C], "sessions", None, plan["token"], roots, IDLE)
    assert [s["status"] for s in out["sessions"]] == ["DELETED", "DELETED"]
    assert not (base / "data" / "mosaic" / fx.B).exists() and not (base / "data" / "mosaic" / fx.C).exists()
    assert (base / fx.PROFILE_B).read_bytes() == profile
    assert _keep_snapshot(base, f"data/mosaic/{fx.B}", f"data/mosaic/{fx.C}") == keep
    listing = sc.list_sessions({"archived": "all"}, roots)
    assert fx.B not in {s["campaign_id"] for s in listing["sessions"]} and listing["orphan_annotations"] == []
    assert fx.B not in sc.load_annotations(roots)["sessions"]
    gone = json.loads((roots.store / "deleted_sessions.json").read_text())
    assert gone[fx.B]["name"] == "simulator night" and gone[fx.B]["had_annotation"] and gone[fx.B]["batch_id"] == out["batch_id"]
    hist = sd.history(roots)
    assert hist[0]["batch_id"] == out["batch_id"] and hist[0]["mode"] == "sessions"
    assert out["free_bytes_before"] is not None and "free_bytes_change_observed" in out and out["bytes_removed_estimate"] > 0
    assert (base / out["audit_dir"] / "batch.json").is_file() and not str(out["audit_dir"]).startswith("data/mosaic")


def test_session_a_whole_keeps_the_ambiguous_science_its_reduce_and_the_manual_analysis(tree):
    base, roots, p = tree
    plan = sd.plan([fx.A], "sessions", None, roots, IDLE)
    out = sd.execute([fx.A], "sessions", None, plan["token"], roots, IDLE)
    assert out["sessions"][0]["status"] == "DELETED"
    assert not p["r1"].exists() and not p["r3"].exists() and not p["s1"].exists() and not p["s2"].exists()
    assert p["r2"].exists() and p["s3"].exists() and p["manual"].exists() and p["bad"].exists()


def test_shared_inputs_hard_links_and_symlinks_survive(tree):
    base, roots, p = tree
    # a REDUCE run of A that also read E's captures: shared
    run = fx._reduce(base, "ALMITA-OBSERVE", "REDUCE-20261019-000000-000009", fx.A, points=(1,))
    fx._w(run / "points" / "2" / "master_spectrum.json", {"capture_refs": [{"source_path": f"data/mosaic/{fx.E}/data/iq/x/y.h5"}]})
    pa = sd.plan([fx.A], "sessions", None, roots, IDLE)
    assert any(k["path"] == str(run.relative_to(base)) and "shared" in k["why"] for k in _row(pa, fx.A)["kept"])
    pe = sd.plan([fx.E], "sessions", None, roots, IDLE)
    assert _row(pe, fx.E)["blocked"].startswith("its captures are inputs of REDUCE runs of other sessions")
    run.joinpath("points", "2", "master_spectrum.json").unlink()
    pe = sd.plan([fx.E], "sessions", None, roots, IDLE)
    assert pe["space"]["kept_alive_by_outside_hard_links"] == 1
    out = sd.execute([fx.E], "sessions", None, pe["token"], roots, IDLE)
    assert out["sessions"][0]["status"] == "DELETED"
    assert (p["outside"] / "precious.bin").read_bytes() == b"keep me" * 100 and os.stat(p["outside"] / "precious.bin").st_nlink == 1


def test_busy_pipeline_and_preserved_sessions_are_blocked(tree):
    base, roots, p = tree
    before = _keep_snapshot(base)
    busy = sd.plan([fx.A, fx.C], "sessions", None, roots, BUSY)
    assert busy["deletable"] == 0 and all("pipeline active" in b["why"] for b in busy["blocked"])
    pres = sd.plan([P, fx.C], "products", ["QUICKLOOK", "REDUCE", "SCIENCE"], roots, IDLE)
    assert _row(pres, P)["blocked"].startswith("PRESERVED") and _row(pres, P)["items"] == []
    whole = sd.plan([P], "sessions", None, roots, IDLE)
    assert whole["deletable"] == 0
    with pytest.raises(sd.StalePreview):
        sd.execute([fx.C], "sessions", None, busy["token"], roots, BUSY)
    assert _keep_snapshot(base) == before                     # only the (empty) lock file may have appeared
    assert p["preserved_run"].exists()


def test_invalid_requests_and_symbolic_links_are_refused(tree, tmp_path_factory):
    base, roots, _ = tree
    for ids in (["../etc"], ["a/b"], "ALMITA", [], ["x\x00y"]):
        with pytest.raises(ValueError):
            sd.plan(ids, "sessions", None, roots, IDLE)
    with pytest.raises(ValueError):
        sd.plan([fx.A], "everything", None, roots, IDLE)
    with pytest.raises(ValueError):
        sd.plan([fx.A], "products", ["IQ"], roots, IDLE)
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "mosaic.csv").write_text("point_id\n")
    (elsewhere / "victim.txt").write_text("never deleted")
    os.symlink(elsewhere, base / "data" / "mosaic" / "LINKED-20261019-00:00:00")
    sc.build_index(roots)
    pl = sd.plan(["LINKED-20261019-00:00:00"], "sessions", None, roots, IDLE)
    assert pl["deletable"] == 0 and "symbolic link" in pl["blocked"][0]["why"]
    ql = base / "data" / "mosaic" / fx.A / "quicklook"
    import shutil
    shutil.rmtree(ql)
    os.symlink(elsewhere, ql)
    pq = sd.plan([fx.A], "products", ["QUICKLOOK"], roots, IDLE)
    assert not _row(pq, fx.A)["items"] and any("symbolic link" in k["why"] for k in _row(pq, fx.A)["kept"])
    with pytest.raises(ValueError):
        sd.execute([fx.A], "sessions", None, "not-a-token", roots, IDLE)
    assert (elsewhere / "victim.txt").read_text() == "never deleted"


def test_a_changed_inventory_or_a_job_starting_after_the_preview_stops_the_batch(tree):
    base, roots, _ = tree
    plan = sd.plan([fx.B], "sessions", None, roots, IDLE)
    (base / "data" / "mosaic" / fx.B / "new_capture.h5").write_bytes(b"arrived after the preview")
    with pytest.raises(sd.StalePreview, match="changed"):
        sd.execute([fx.B], "sessions", None, plan["token"], roots, IDLE)
    assert (base / "data" / "mosaic" / fx.B / "new_capture.h5").is_file()
    plan = sd.plan([fx.B], "sessions", None, roots, IDLE)
    with pytest.raises(sd.StalePreview, match="pipeline became active"):
        sd.execute([fx.B], "sessions", None, plan["token"], roots, BUSY)
    assert (base / "data" / "mosaic" / fx.B).is_dir()


def test_deletion_and_job_starts_exclude_each_other(tree, monkeypatch):
    base, roots, _ = tree
    monkeypatch.setattr(sd, "LOCK_TIMEOUT_S", 0.3)
    plan = sd.plan([fx.B], "sessions", None, roots, IDLE)
    with sd.job_start_gate(roots):                                   # a job start in progress
        with pytest.raises(sd.DeletionBlocked):
            sd.execute([fx.B], "sessions", None, plan["token"], roots, IDLE)
    assert (base / "data" / "mosaic" / fx.B).is_dir()
    entered, release, refused = threading.Event(), threading.Event(), []

    def slow_probe():
        entered.set()
        release.wait(5)
        return []
    t = threading.Thread(target=lambda: sd.execute([fx.B], "sessions", None, plan["token"], roots, slow_probe))
    t.start()
    assert entered.wait(5)
    try:
        with sd.job_start_gate(roots):
            pass
    except sd.DeletionBlocked as exc:
        refused.append(str(exc))
    release.set()
    t.join(10)
    assert refused and "deletion is in progress" in refused[0]
    assert not (base / "data" / "mosaic" / fx.B).exists()


def test_a_partial_failure_is_reported_and_the_catalog_keeps_what_is_left(tree):
    base, roots, _ = tree
    sc.annotate(fx.B, {"notes": "still here"}, roots)
    victim = next((base / "data" / "mosaic" / fx.B / "data" / "iq").rglob("*_0002.h5"))

    def remover(path):
        if path == str(victim):
            raise PermissionError(13, "Permission denied")
        os.unlink(path)
    plan = sd.plan([fx.B], "sessions", None, roots, IDLE)
    out = sd.execute([fx.B], "sessions", None, plan["token"], roots, IDLE, remover=remover)
    r = out["sessions"][0]
    assert r["status"] == "PARTIAL" and r["error_count"] == 1 and "Permission denied" in r["errors"][0]["error"]
    assert victim.is_file() and r["files_removed"] > 0
    listing = sc.list_sessions({}, roots)
    assert fx.B in {s["campaign_id"] for s in listing["sessions"]}                 # what is left is still in the catalog
    assert sc.load_annotations(roots)["sessions"][fx.B]["notes"] == "still here"     # not a completed deletion: note kept
    assert not (roots.store / "deleted_sessions.json").exists() or fx.B not in json.loads((roots.store / "deleted_sessions.json").read_text())
    assert sd.history(roots)[0]["sessions"][0]["status"] == "PARTIAL"


def test_repeating_a_confirmed_request_touches_nothing_else(tree):
    base, roots, _ = tree
    plan = sd.plan([fx.C], "sessions", None, roots, IDLE)
    sd.execute([fx.C], "sessions", None, plan["token"], roots, IDLE)
    after_first = fx.snapshot(base)
    with pytest.raises(sd.StalePreview):
        sd.execute([fx.C], "sessions", None, plan["token"], roots, IDLE)
    assert fx.snapshot(base) == after_first
    again = sd.plan([fx.C], "sessions", None, roots, IDLE)
    assert again["deletable"] == 0 and "unknown session" in again["blocked"][0]["why"]


def test_select_all_filtered_returns_every_match_beyond_the_page(tree):
    _, roots, _ = tree
    page = sc.list_sessions({"per_page": "5", "page": "1", "ids": "1"}, roots)
    assert len(page["sessions"]) == 5 and len(page["selection"]) == page["total"] == 7
    sims = sc.list_sessions({"condition": "SIMULATOR", "ids": "1"}, roots)["selection"]
    assert [s["campaign_id"] for s in sims] == [fx.B] and sims[0]["bytes_on_disk"] > 0
    assert "selection" not in sc.list_sessions({}, roots)
