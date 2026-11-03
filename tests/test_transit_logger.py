"""TRANSIT logger + page: daily CSV record, the 180-min window for the page, SDR-busy skip, routes and navigation."""
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import transit_logger as tl
from tests.test_orchestrator_web import running_server

ROOT = Path(__file__).resolve().parent.parent


def _row(t, **k):
    return {"utc": t.isoformat(timespec="seconds").replace("+00:00", "Z"), "status": "VALID", "power_db": 40.0,
            "clipping_fraction": 0.0, "sun_alt_deg": 30.0, "sun_az_deg": 80.0, **k}


def test_rows_go_to_the_daily_csv_and_the_window_crosses_midnight(tmp_path):
    now = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
    for minutes in (200, 170, 61, 5):                                  # 200 min ago is outside the 180-min window
        tl.append_row(_row(now - timedelta(minutes=minutes), power_db=40.0 + minutes / 100), tmp_path)
    tl.append_row(_row(now, status="SDR_BUSY", power_db=None, error="skipped: capture.py is using the SDR"), tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["transit_20261102.csv", "transit_20261103.csv"]
    rows = tl.read_window(now, out_dir=tmp_path)
    assert [r["power_db"] for r in rows] == [41.7, 40.61, 40.05, None]
    assert rows[-1]["status"] == "SDR_BUSY"


def test_latest_json_is_what_the_page_reads(tmp_path):
    tl.write_latest([{"utc": "2026-11-03T12:00:00Z", "status": "VALID", "power_db": 41.0}], {"updated_utc": "2026-11-03T12:00:00Z", "every_s": 10}, tmp_path)
    doc = json.loads((tmp_path / tl.LATEST_NAME).read_text())
    assert doc["schema"] == "almita.transit_logger.v1" and doc["every_s"] == 10 and doc["rows"][0]["power_db"] == 41.0
    assert not list(tmp_path.glob("*.tmp"))


def test_another_sdr_user_skips_the_sample(monkeypatch):
    import subprocess
    calls = []

    def fake_run(argv, **k):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0 if "capture.py" in argv[-1] else 1)
    monkeypatch.setattr(tl.subprocess, "run", fake_run)
    assert tl.sdr_busy() == "capture.py"
    monkeypatch.setattr(tl.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1))
    import almita_web_ops
    monkeypatch.setattr(almita_web_ops, "running_jobs", lambda: [{"stage": "align", "state": "RUNNING"}])
    assert tl.sdr_busy() == "web job align"
    monkeypatch.setattr(almita_web_ops, "running_jobs", lambda: [])
    assert tl.sdr_busy() is None


def test_page_routes_and_navigation():
    with running_server() as base:
        for page in ("/transit.html", "/transit.js"):
            with urllib.request.urlopen(base + page, timeout=10) as r:
                assert r.status == 200
    assert 'key: "TRANSIT", href: "/transit.html"' in (ROOT / "console" / "common.js").read_text()
    assert 'href="/transit.html"' in (ROOT / "console" / "index.html").read_text()
    unit = (ROOT / "systemd" / "notebook" / "almita-transit-logger.service").read_text()
    assert "KillSignal=SIGINT" in unit and "transit_logger.py" in unit


def test_start_stop_go_through_the_user_unit(monkeypatch):
    import subprocess
    import pytest
    calls = []

    def fake(*args, timeout=60.0):
        calls.append(args)
        out = "active\n" if args[0] == "is-active" and ("start",) in [c[:1] for c in calls] else "inactive\n"
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(tl, "_systemctl", fake)
    assert tl.service_action("start")["active"] is True
    assert calls[0] == ("start", tl.UNIT)
    with pytest.raises(ValueError):
        tl.service_action("restart")
    monkeypatch.setattr(tl, "_systemctl", lambda *a, **k: subprocess.CompletedProcess(a, 5, "", "Unit not found."))
    with pytest.raises(RuntimeError, match="Unit not found"):
        tl.service_action("stop")


def test_start_route_needs_confirm_and_reports_a_failed_systemctl(monkeypatch):
    import subprocess
    import urllib.error
    monkeypatch.setattr(tl, "_systemctl", lambda *a, **k: subprocess.CompletedProcess(a, 5, "", "Unit not found."))
    with running_server() as base:
        for body, code in ((b"{}", 400), (b'{"confirm": true}', 409)):
            req = urllib.request.Request(base + "/api/transit/start", data=body, method="POST", headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                raise AssertionError("expected an error")
            except urllib.error.HTTPError as e:
                assert e.code == code
        with urllib.request.urlopen(base + "/api/transit/state", timeout=10) as r:
            assert json.loads(r.read())["data"]["unit"] == tl.UNIT
