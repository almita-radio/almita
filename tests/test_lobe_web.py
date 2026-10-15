"""LOBE in the web: the solar-ALIGN gate, the job command, the routes and the page wiring. No hardware: the mount
read and the plan are real functions on fixed inputs; nothing is started."""
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import almita_web_ops as ops
import lobe_transit as lt
from tests.test_orchestrator_web import _get, running_server

ROOT = Path(ops.ROOT)


def _align(root: Path, name: str, status: str, age_h: float, sync: bool = False) -> None:
    d = root / name
    d.mkdir(parents=True)
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat()
    (d / "alignment_result.json").write_text(json.dumps({
        "timestamp_utc": ts, "reference": "sun", "result_status": status, "sync_applied": sync, "confidence": 0.8,
        "offsets": {"offset_ra_deg": 0.4, "offset_dec_deg": -0.3, "separation_deg": 0.5}}))


def test_alignment_gate_needs_a_recent_real_solar_pass(tmp_path):
    assert ops.lobe_alignment(tmp_path)["verdict"] == "NOT ALIGNED"
    _align(tmp_path, "WEBPLAN-SUN-20261015T120000Z", "PASS", 0.1)                    # a PLAN never counts
    assert not ops.lobe_alignment(tmp_path)["ok"]
    _align(tmp_path, "WEB-SUN-20261015T120000Z", "LOW CONFIDENCE", 0.1)
    assert ops.lobe_alignment(tmp_path)["ok"] is False
    _align(tmp_path, "WEB-SUN-20261015T130000Z", "PASS", 13.0)
    assert ops.lobe_alignment(tmp_path)["verdict"] == "ALIGN TOO OLD"
    _align(tmp_path, "WEB-SUN-20261015T140000Z", "PASS", 0.5)
    a = ops.lobe_alignment(tmp_path)
    assert a["ok"] and a["verdict"] == "ALIGNED (OFFSET KNOWN, NOT SYNCED)" and a["separation_deg"] == 0.5
    _align(tmp_path, "WEB-SUN-20261015T150000Z", "PASS", 0.2, sync=True)
    assert ops.lobe_alignment(tmp_path)["verdict"] == "ALIGNED (SYNCED)"


def test_lobe_stage_is_physical_and_claims_mount_and_sdr():
    assert ops.STAGES["lobe"] == {"physical": True, "resources": ("mount", "sdr")}


def test_build_command_refuses_without_alignment(monkeypatch):
    monkeypatch.setattr(ops, "lobe_alignment", lambda: {"ok": False, "detail": "no completed real solar ALIGN"})
    with pytest.raises(ValueError, match="valid real solar ALIGN"):
        ops.build_command("lobe", {"mode": "drift"}, "JOB-1")


def test_build_command_passes_bounded_parameters(monkeypatch):
    monkeypatch.setattr(ops, "lobe_alignment", lambda: {"ok": True, "detail": "ok"})
    argv, meta = ops.build_command("lobe", {"mode": "scan", "expected_fwhm": 25, "half_range_fwhm": 2, "scan_step": 0.5, "gain": 30}, "JOB-1")
    assert argv[1] == "lobe_transit.py" and argv[argv.index("--mode") + 1] == "scan"
    assert argv[argv.index("--expected-fwhm") + 1] == "25.0" and argv[argv.index("--gain") + 1] == "30.0"
    assert meta["output_dir"].startswith("data/lobe/WEB-LOBE-SCAN-")
    with pytest.raises(ValueError):
        ops.build_command("lobe", {"mode": "drift", "gain": 99}, "JOB-1")
    with pytest.raises(ValueError):
        ops.build_command("lobe", {"mode": "spin"}, "JOB-1")


def test_status_combines_sun_mount_alignment_and_plan(monkeypatch):
    monkeypatch.setattr(ops, "read_mount", lambda: {"ra_h": 13.2, "dec_deg": -8.5, "tracking": "on", "mount_state": "Tracking"})
    monkeypatch.setattr(ops, "lobe_alignment", lambda: {"ok": False, "verdict": "NOT ALIGNED", "detail": "none"})
    s = ops.lobe_status({"mode": "drift", "expected_fwhm": "20", "half_range_fwhm": "1.5"})
    assert set(s) >= {"sun", "mount", "alignment", "plan", "params"}
    assert s["plan"]["half_range_deg"] == pytest.approx(30.0)
    assert "alt_deg" in s["mount"] and "sun_separation_deg" in s["mount"]
    with pytest.raises(ValueError):
        ops.lobe_status({"mode": "spin"})


def test_samples_route_reads_the_csv_and_fits(tmp_path, monkeypatch):
    monkeypatch.setitem(ops.SERVE_ROOTS, "lobe", tmp_path)
    d = tmp_path / "WEB-LOBE-DRIFT-X"
    d.mkdir()
    import math
    with open(d / "lobe_samples.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=lt.CSV_FIELDS)
        w.writeheader()
        for i in range(80):
            x = -30 + 60 * i / 79
            w.writerow({"index": i, "elapsed_s": 5 * i, "offset_deg": x, "status": "VALID", "clipping_fraction": 0,
                        "power_lin": 1 + 0.1 * math.exp(-4 * math.log(2) * x ** 2 / 15 ** 2)})
    out = ops.lobe_samples(str(d))
    assert len(out["samples"]) == 80 and out["fit"]["fwhm_deg"] == pytest.approx(15, rel=0.05)
    with pytest.raises(ValueError):
        ops.lobe_samples("/etc")


def test_page_routes_and_navigation(monkeypatch):
    monkeypatch.setattr(ops, "read_mount", lambda: {"error": "INDI not reachable (test)"})
    with running_server() as base:
        import urllib.request
        for page in ("/lobe.html", "/lobe.js"):
            with urllib.request.urlopen(base + page, timeout=10) as r:
                assert r.status == 200
        code, body = _get(base + "/api/ops/lobe/status?mode=drift")
        assert code == 200 and body["data"]["mount"]["error"]
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/ops/lobe/status?mode=spin", timeout=10)
        assert e.value.code == 400
    assert 'key: "LOBE", href: "/lobe.html"' in (ROOT / "console" / "common.js").read_text()
