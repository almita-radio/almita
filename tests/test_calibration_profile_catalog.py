"""OBSERVE calibration-profile selector (calibration_profile_catalog.py + /api/observe/calibration-profiles).

Everything runs on temporary directories: a fake repo root with data/calibration/ built in tmp_path. The HTTP
tests use make_server() on 127.0.0.1:0 with a tmp public_root and redirected roots - never main(), never the
real data/ or a real service."""
import contextlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pytest

import almita_orchestrator_server as server_mod
import almita_web_ops as ops
import calibration_profile_catalog as cat

MAIN = {"center_frequency_hz": 1420405752.0, "sample_rate": 2400000.0, "gain_db": 40.2}


def _profile(path, *, freq=1420405752, rate=2400000, gain=40.2, absolute=False, created="2026-10-02T01:40:00Z"):
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"absolute_calibration": absolute, "calibration_level": "RELATIVE_INSTRUMENTAL", "center_frequency_hz": freq,
            "sample_rate_hz": rate, "gain_db": gain, "fft_size": 8192, "created_utc": created, "reference_count": 5,
            "instrument_chain": "LNA_FILTER_CABLING_TO_RTL_SDR", "reference_topology": "AMBIENT_50R_AT_LNA_INPUT_WIZARD"}
    path.write_text(json.dumps(meta))
    np.savez(path.with_suffix(".npz"), reference=np.ones(8))
    return path


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    cal = root / "data" / "calibration"
    _profile(cal / "WIZARD-A" / "observe_profile" / "calibration_profile_v1.json")
    _profile(cal / "WIZARD-OLD" / "observe_profile" / "calibration_profile_v1.json", freq=1420405000,
             created="2026-09-28T02:38:00Z")
    _profile(cal / "BROKEN" / "calibration_profile_v1.json", absolute=True)       # rejected by the real loader
    (cal / "WIZARD-A" / "wizard_state.json").write_text(json.dumps({"step": "DONE"}))  # not a profile: never listed
    (root / "secret.json").write_text(json.dumps({"absolute_calibration": False, "center_frequency_hz": 1}))
    np.savez(root / "secret.npz", x=np.ones(2))
    return root, cal


def test_lists_only_profiles_under_the_calibration_root_with_compatibility(repo):
    root, cal = repo
    out = cat.list_profiles(cal, root, MAIN)
    paths = [p["path"] for p in out["profiles"]]
    assert out["root"] == "data/calibration" and out["disk"] == "server"
    assert "data/calibration/WIZARD-A/observe_profile/calibration_profile_v1.json" in paths
    assert not any("wizard_state" in p or "secret" in p for p in paths)
    by = {p["path"].split("/")[2]: p for p in out["profiles"]}
    assert by["WIZARD-A"]["compatibility"]["status"] == "COMPATIBLE"
    assert by["WIZARD-OLD"]["compatibility"]["status"] == "INCOMPATIBLE"
    assert "center frequency" in by["WIZARD-OLD"]["compatibility"]["reason"]
    assert by["BROKEN"]["valid"] is False and by["BROKEN"]["error"]
    assert paths[0].startswith("data/calibration/WIZARD-A")                     # newest first


def test_listing_without_observation_values_reports_unknown(repo):
    root, cal = repo
    out = cat.list_profiles(cal, root, None)
    assert {p["compatibility"]["status"] for p in out["profiles"] if p["valid"]} == {"UNKNOWN"}


@pytest.mark.parametrize("raw,msg", [
    ("", "empty path"),
    ("../secret.json", "only profiles under data/calibration/"),
    ("data/calibration/../../secret.json", "only profiles under data/calibration/"),
    ("/etc/passwd", "only profiles under data/calibration/"),
    ("data/calibration/NOPE/calibration_profile_v1.json", "does not exist on the ALMITA server"),
    ("C:\\Users\\op\\Desktop\\calibration_profile_v1.json", "path on the browser's computer"),
    ("C:\\fakepath\\calibration_profile_v1.json", "path on the browser's computer"),
    ("data/calibration/BROKEN/calibration_profile_v1.json", "not a valid calibration profile"),
    ("data/calibration/WIZARD-A/wizard_state.txt", "ends in .json"),
])
def test_validate_rejects_with_clear_messages(repo, raw, msg):
    root, cal = repo
    with pytest.raises(cat.ProfilePathError, match=msg.replace(".", r"\.").replace("/", "/")):
        cat.validate_profile_path(raw, cal, root, MAIN)


def test_validate_rejects_symlink_escaping_the_root(repo, tmp_path):
    root, cal = repo
    link = cal / "LINK"
    link.mkdir()
    (link / "p.json").symlink_to(root / "secret.json")
    (link / "p.npz").symlink_to(root / "secret.npz")
    with pytest.raises(cat.ProfilePathError):
        cat.validate_profile_path("data/calibration/LINK/p.json", cal, root, MAIN)
    assert not any("LINK" in p["path"] for p in cat.list_profiles(cal, root, MAIN)["profiles"])


def test_validate_normalizes_npz_and_reports_compatibility(repo):
    root, cal = repo
    ok = cat.validate_profile_path("data/calibration/WIZARD-A/observe_profile/calibration_profile_v1.npz", cal, root, MAIN)
    assert ok["path"] == "data/calibration/WIZARD-A/observe_profile/calibration_profile_v1.json"
    assert ok["compatibility"]["status"] == "COMPATIBLE" and ok["disk"] == "server"
    old = cat.validate_profile_path("data/calibration/WIZARD-OLD/observe_profile/calibration_profile_v1.json", cal, root, MAIN)
    assert old["valid"] is True and old["compatibility"]["status"] == "INCOMPATIBLE"


def test_selector_and_preflight_reach_the_same_verdict(repo):
    """The selector's compatibility is the pre-RUN preflight's own decision function."""
    import calibration_foundation
    import observation_preflight
    root, cal = repo
    for d in ("WIZARD-A", "WIZARD-OLD"):
        path = cal / d / "observe_profile" / "calibration_profile_v1.json"
        profile = calibration_foundation.load_calibration_profile(path)
        expected = observation_preflight.planned_profile_compatibility(profile, MAIN)
        got = cat.validate_profile_path(str(path.relative_to(root)), cal, root, MAIN)["compatibility"]
        assert got == expected


# ------------------------------------------------------------------ HTTP endpoints (isolated server)
@contextlib.contextmanager
def _server(tmp_path, monkeypatch, repo):
    root, cal = repo
    monkeypatch.setitem(ops.SERVE_ROOTS, "calibration", cal)
    monkeypatch.setattr(ops, "ROOT", root)
    public = tmp_path / "public"
    public.mkdir()
    httpd = server_mod.make_server("127.0.0.1", 0, authenticator=None, public_root=public)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_list_and_validate(tmp_path, monkeypatch, repo):
    q = urllib.parse.urlencode({"center_frequency_hz": 1420405752, "sample_rate": 2400000, "gain_db": 40.2})
    with _server(tmp_path, monkeypatch, repo) as base:
        status, body = _get(f"{base}/api/observe/calibration-profiles?{q}")
        assert status == 200 and body["data"]["disk"] == "server" and len(body["data"]["profiles"]) == 3
        good = urllib.parse.quote("data/calibration/WIZARD-A/observe_profile/calibration_profile_v1.json")
        status, body = _get(f"{base}/api/observe/calibration-profiles/validate?path={good}&{q}")
        assert status == 200 and body["data"]["compatibility"]["status"] == "COMPATIBLE"
        status, body = _get(f"{base}/api/observe/calibration-profiles/validate?path={urllib.parse.quote('../secret.json')}&{q}")
        assert status == 400 and "only profiles under data/calibration/" in body["error"]
        status, body = _get(f"{base}/api/observe/calibration-profiles?center_frequency_hz=abc")
        assert status == 400


# ------------------------------------------------------------------ file-explorer view (one server directory at a time)

def _hashes(root):
    import hashlib
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_browse_root_shows_directories_with_breadcrumbs_and_nothing_outside(repo):
    root, cal = repo
    out = cat.browse_directory("", cal, root, MAIN)
    assert out["disk"] == "server" and out["dir"] == "data/calibration" and out["parent"] is None
    assert [d["name"] for d in out["dirs"]] == ["BROKEN", "WIZARD-A", "WIZARD-OLD"]
    assert out["files"] == [] and out["breadcrumbs"] == [{"name": "calibration", "path": "data/calibration"}]


def test_browse_a_profile_directory_shows_name_dir_values_and_why_one_is_rejected(repo):
    root, cal = repo
    good = cat.browse_directory("data/calibration/WIZARD-A/observe_profile", cal, root, MAIN)
    assert good["parent"] == "data/calibration/WIZARD-A"
    [f] = good["files"]
    assert (f["name"], f["dir"], f["selectable"]) == ("calibration_profile_v1.json", "data/calibration/WIZARD-A/observe_profile", True)
    assert (f["summary"]["center_frequency_hz"], f["summary"]["sample_rate_hz"], f["summary"]["gain_db"]) == (1420405752, 2400000, 40.2)
    old = cat.browse_directory("data/calibration/WIZARD-OLD/observe_profile", cal, root, MAIN)["files"][0]
    assert old["selectable"] is False and old["compatibility"]["status"] == "INCOMPATIBLE"
    assert "center frequency" in old["compatibility"]["reason"]
    broken = cat.browse_directory("data/calibration/BROKEN", cal, root, MAIN)["files"][0]
    assert broken["valid"] is False and broken["selectable"] is False and broken["error"]
    state = cat.browse_directory("data/calibration/WIZARD-A", cal, root, MAIN)
    assert [d["name"] for d in state["dirs"]] == ["observe_profile"]
    assert state["files"][0]["type"] == "other" and "no .npz" in state["files"][0]["error"]   # wizard_state.json


@pytest.mark.parametrize("bad", ["data", "data/calibration/../..", "/etc", "data\\calibration", "data/calibration/NOPE"])
def test_browse_refuses_anything_outside_or_missing(repo, bad):
    root, cal = repo
    with pytest.raises(cat.ProfilePathError):
        cat.browse_directory(bad, cal, root, MAIN)


def test_browse_list_and_validate_never_write_to_the_profiles(repo):
    root, cal = repo
    before = _hashes(cal)
    for d in ("", "data/calibration/WIZARD-A", "data/calibration/WIZARD-A/observe_profile", "data/calibration/BROKEN"):
        cat.browse_directory(d, cal, root, MAIN)
    cat.list_profiles(cal, root, MAIN)
    cat.validate_profile_path("data/calibration/WIZARD-A/observe_profile/calibration_profile_v1.json", cal, root, MAIN)
    assert _hashes(cal) == before


def test_http_browse(tmp_path, monkeypatch, repo):
    with _server(tmp_path, monkeypatch, repo) as base:
        q = urllib.parse.urlencode({"dir": "data/calibration/WIZARD-OLD/observe_profile", **MAIN})
        status, body = _get(f"{base}/api/observe/calibration-profiles/browse?{q}")
        assert status == 200 and body["data"]["files"][0]["compatibility"]["status"] == "INCOMPATIBLE"
        status, body = _get(f"{base}/api/observe/calibration-profiles/browse?dir=..%2F..")
        assert status == 400 and body["disk"] == "server"


# ------------------------------------------------------------------ OBSERVE preflight uses the same validation

def test_preflight_blocks_an_unusable_quicklook_profile_and_passes_the_selected_one(repo, monkeypatch):
    import observation_preflight as pf
    root, cal = repo
    monkeypatch.setattr(pf, "CALIBRATION_ROOT", cal)
    good = pf._quicklook_check({"enabled": True, "calibration_profile_path": "data/calibration/WIZARD-A/observe_profile/calibration_profile_v1.json"})
    assert good["status"] == pf.PASS and "1420405752 Hz, 2400000 sps, 40.2 dB" in good["detail"]
    for bad, why in (("data/calibration/NOPE/calibration_profile_v1.json", "does not exist"),
                     ("data/calibration/BROKEN/calibration_profile_v1.json", "not a valid calibration profile"),
                     ("../secret.json", "only profiles under")):
        res = pf._quicklook_check({"enabled": True, "calibration_profile_path": bad})
        assert (res["status"], res["criticality"]) == (pf.BLOCK, pf.REQUIRED) and why in res["detail"], res
