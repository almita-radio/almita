"""STATUS · HYDROGEN LINE (operator 2026-11-09: "incluye la opción de ver el rf status con switching"): fswitch.py runs
as a web job holding MAIN only; the page draws the newest folded spectrum and starts a run with one confirmation."""
import json
import os

import pytest

import almita_web_ops as ops
import transit_logger
from tests.test_web_frontend import run_page

LAST = {"v_kms": [-150 + i for i in range(301)], "folded_pct": [0.01 * ((i % 7) - 3) for i in range(301)],
        "line_window_kms": [-30, 40], "dir": "data/fswitch/WEB-20261109T120000Z", "png": None,
        "summary": {"dwell_s": 2.0, "f_a_hz": 1419805752, "f_b_hz": 1421005752, "gain_db": 40.2, "adc_rms_range": [3.9, 4.2],
                    "started_utc": "2026-11-09T12:00:00+00:00", "lo_acknowledged": {"f_a": True, "f_b": True},
                    "efficiency": {"fraction": 0.953},
                    "result": {"ripple_band_shape_pct": 14.63, "off_line_rms_pct": 0.1177, "line_excess_pct": 0.0888,
                               "n_a": 24, "n_b": 24}}}

ROUTES = """
window.__routes["GET /api/sdr/fswitch"] = { body: { ok: true, data: { last: %s, cycles: [6, 12, 24, 48], default_cycles: 24, seconds_per_cycle: 4.2 } } };
window.__routes["GET /api/ops/jobs"] = { body: { ok: true, data: [] } };
window.__routes["POST /api/ops/start/fswitch"] = (b) => ({ body: { ok: true, data: { job_id: "FSWITCH-20261109-120000-abcd" } } });
window.__routes["GET /api/ops/job/FSWITCH-20261109-120000-abcd"] = { body: { ok: true, data: { job_id: "FSWITCH-20261109-120000-abcd",
  stage: "fswitch", state: "RUNNING", verdict: "RUNNING", exit_code: null, elapsed_s: 3, detail: "", facts: {}, artifacts: [],
  log_tail: "FSWITCH 24 cycles x 2 x 2.0 s\\nsegment 4/25 recorded" } } };
"""

DRIVER = r"""
await until(() => $("hi-detail").textContent.indexOf("folded") >= 0 && $("hi-cycles").options.length === 4, 6000);
out.badge = $("hi-badge").textContent; out.detail = $("hi-detail").textContent;
out.options = [...$("hi-cycles").options].map((o) => o.textContent); out.selected = $("hi-cycles").value;
$("hi-cycles").value = "12";
$("btn-hi").click();
await until(() => $("hi-job").textContent.indexOf("segment 4/25") >= 0, 6000);
out.posted = window.__calls.filter((c) => c.key === "POST /api/ops/start/fswitch").map((c) => c.body);
out.confirm = window.__confirmText;
out.stopShown = $("hi-job").textContent.indexOf("STOP SWITCHING") >= 0;
"""


def test_status_shows_the_last_switching_run_and_starts_one_as_a_web_job(tmp_path):
    out = run_page(tmp_path, "status", ROUTES % json.dumps(LAST), DRIVER, budget=12000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    assert out["badge"] == "PASS"
    assert "band shape ±14.6 % → folded ±0.12 % off the line" in out["detail"] and "both acknowledged" in out["detail"]
    assert "95 % of the time recording" in out["detail"]
    assert out["options"][2] == "24 cycles · ~101 s" and out["selected"] == "24"
    assert out["posted"] == [{"params": {"cycles": 12}, "confirm": None}]
    assert "The mount is not moved" in out["confirm"] and out["stopShown"] is True


def test_status_without_any_run_says_so(tmp_path):
    driver = r"""
await until(() => $("hi-badge").textContent !== "—", 6000);
out.badge = $("hi-badge").textContent; out.detail = $("hi-detail").textContent;
"""
    out = run_page(tmp_path, "status", ROUTES % "null", driver, budget=8000)
    assert out["badge"] == "NO RUN YET" and "No frequency-switching run" in out["detail"]


def test_the_web_job_holds_main_only_and_takes_admitted_lengths(monkeypatch):
    assert ops.STAGES["fswitch"] == {"physical": False, "resources": ("sdr",)}
    monkeypatch.setattr(transit_logger, "sdr_process_busy", lambda: None)
    argv, meta = ops.build_command("fswitch", {"cycles": 12}, "FSWITCH-X")
    assert argv[1:4] == ["fswitch.py", "--cycles", "12"] and argv[-2:] == ["--web-job", "FSWITCH-X"]
    assert meta["output_dir"].startswith("data/fswitch/WEB-")
    with pytest.raises(ValueError):
        ops.build_command("fswitch", {"cycles": 7}, "FSWITCH-X")
    monkeypatch.setattr(transit_logger, "sdr_process_busy", lambda: "capture.py")
    with pytest.raises(ops.OpsBlocked, match="capture.py"):
        ops.build_command("fswitch", {"cycles": 12}, "FSWITCH-X")


def test_state_reads_the_newest_result_never_a_settling_measurement(tmp_path, monkeypatch):
    root = tmp_path / "data" / "fswitch"
    for name, t in (("20261109T090000Z", 1), ("WEB-20261109T100000Z", 2), ("settle_20261109T110000Z", 3)):
        d = root / name
        d.mkdir(parents=True)
        (d / "folded.json").write_text(json.dumps({"v_kms": [0], "folded_pct": [0], "summary": {"name": name}}))
        os.utime(d / "folded.json", (t, t))
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    monkeypatch.setitem(ops.SERVE_ROOTS, "fswitch", root)
    last = ops.fswitch_state()["last"]
    assert last["summary"]["name"] == "WEB-20261109T100000Z" and last["dir"] == "data/fswitch/WEB-20261109T100000Z"


def test_a_switching_run_is_not_its_own_competitor(monkeypatch):
    """fswitch.py asks sdr_busy() about itself: its own process and its own web job never count."""
    import subprocess
    me = str(os.getpid())
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=me + "\n", stderr=""))
    assert transit_logger.sdr_process_busy() is None
    monkeypatch.setattr(ops, "running_jobs", lambda: [{"job_id": "FSWITCH-X", "stage": "fswitch"}])
    assert transit_logger.sdr_busy(ignore_job_id="FSWITCH-X") is None
    assert transit_logger.sdr_busy() == "web job fswitch"


def test_the_hydrogen_chart_fits_a_phone_like_the_rfi_chart(tmp_path):
    """Operator 2026-11-09 (phone screenshot): the chart was drawn at device-pixel size, 2-3x wider than the screen."""
    from tests.test_web_frontend import chromium, page_harness, result_of
    driver = r"""
await until(() => $("hi-detail").textContent.indexOf("folded") >= 0, 6000);
const box = (id) => { const r = $(id).getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; };
out.hi = box("hi-chart"); out.rfi = box("rfi-chart"); out.page = document.documentElement.clientWidth;
"""
    out = result_of(chromium(page_harness(tmp_path, "status", ROUTES % json.dumps(LAST), driver), 8000, width=390))
    assert "driver_error" not in out, out.get("driver_error")
    assert out["hi"] == out["rfi"] and out["hi"][0] <= out["page"] and out["hi"][1] == 260
