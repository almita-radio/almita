"""BL-050: one identity for an OBSERVE session - the campaign - reachable from any of its three identifiers."""
import session_identity as si


def _campaign(root):
    c = root / "ALMITA-OBSERVE-20261015-03:10:34"
    (c / "data" / "iq" / "ALMITA-OBSERVE-20261015-03:12:19").mkdir(parents=True)
    (c / "capture_timing_20261015_031145.csv").write_text("x\n")
    return c


def test_any_of_the_three_identifiers_leads_to_the_same_campaign(tmp_path):
    _campaign(tmp_path)
    want = {"campaign_id": "ALMITA-OBSERVE-20261015-03:10:34", "iq_session_dirs": ["ALMITA-OBSERVE-20261015-03:12:19"],
            "capture_session_ids": ["20261015_031145"]}
    for ident in ("ALMITA-OBSERVE-20261015-03:10:34", "ALMITA-OBSERVE-20261015-03:12:19", "20261015_031145"):
        got = si.resolve(ident, tmp_path)
        assert {k: got[k] for k in want} == want
    assert si.resolve("20990101_000000", tmp_path) is None and si.resolve("../etc", tmp_path) is None


def test_session_root_gives_campaign_and_iq_dir():
    r = si.from_session_root("data/mosaic/ALMITA-OBSERVE-20261015-03:10:34/data/iq/ALMITA-OBSERVE-20261015-03:12:19")
    assert r == {"campaign_id": "ALMITA-OBSERVE-20261015-03:10:34", "iq_session_dir": "ALMITA-OBSERVE-20261015-03:12:19"}
    assert si.from_session_root(None) == {"campaign_id": None, "iq_session_dir": None}
