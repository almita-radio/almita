import contextlib
import json
import os
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

ROOT=Path(__file__).parent.resolve(); DASH=ROOT/"dashboard"


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self,*args): pass


@contextlib.contextmanager
def server(root):
    handler=lambda *a,**k:Quiet(*a,directory=str(root),**k)
    http=ThreadingHTTPServer(("127.0.0.1",0),handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    try: yield f"http://127.0.0.1:{http.server_port}"
    finally:http.shutdown();thread.join()


def status(kind="OK",updated="2099-01-01T00:00:00Z"):
    return {"schema_version":"1.0","status":kind,"session_id":"fixture-session","updated_utc":updated,
      "calibration_profile":"profile_v1","calibration_level":"RELATIVE_INSTRUMENTAL","absolute_calibration":False,
      "environment":"INDOOR_DEPARTMENT","points_seen":4,"points_success":3,"points_processed":2,"points_skipped":1,
      "latest_point_id":"p002","backlog_count":0,"errors":[],"warnings":[],
      "session_progress":{"total_planned":None,"success":3,"failed":1,"deferred":0,"pending":None,"processed":2},
      "performance":{"discovery_seconds":.01,"state_write_seconds":.01,"peak_rss_mib":100,
                     "approx_sustainable_points_per_minute":10,"point_history":[]}}


def fixture(tmp_path,kind="OK",map_state="POINT_ONLY",products=True,updated="2099-01-01T00:00:00Z",telemetry=None):
    shutil.copytree(DASH,tmp_path/"dashboard");data=tmp_path/"data";data.mkdir();(data/"quicklook_live_status.json").write_text(json.dumps(status(kind,updated)))
    if products:
        spectrum={"calibration_level":"RELATIVE_INSTRUMENTAL","absolute_calibration":False,"gain_db":40.2,
          "center_frequency_hz":1420405752,"sample_rate_hz":2400000,"capture_duration_seconds":2,
          "compatibility":{"status":"COMPATIBLE"},"quicklook_metrics":{"valid_fraction":.95,"masked_fraction":.05,
          "median_fractional_excess":.1,"robust_sigma_fractional_excess":.2,"max_positive_fractional_excess":1,"min_fractional_excess":-.5}}
        water={"time_bins":64,"frequency_bins_native":8192,"frequency_bins_dashboard":512,"capture_duration_seconds":2,
          "quicklook_metrics":{"temporal_rms_of_row_medians":.01,"maximum_excursion":2,"maximum_excursion_label":"FEATURE / OUTLIER"}}
        mapdoc={"status":map_state,"environment":"INDOOR_DEPARTMENT","points":[{"ra_deg":1,"dec_deg":2,"coordinate_source":"COMMANDED"}],
          "coordinate_convention":"x positive East; y positive North","map_center":{"ra_deg":1,"dec_deg":2}}
        for name,value in (("latest_spectrum.json",spectrum),("latest_waterfall.json",water),("quicklook_map.json",mapdoc)):(data/name).write_text(json.dumps(value))
    if telemetry is not None:
        td=tmp_path/"telemetry";td.mkdir();(td/"telemetry_summary.json").write_text(json.dumps(telemetry))
    return tmp_path


def dom(root,query="/dashboard/?root=/data"):
    with server(root) as base:
        run=subprocess.run(["chromium","--headless","--no-sandbox","--disable-gpu","--disable-dev-shm-usage",
          "--virtual-time-budget=1200","--dump-dom",base+query],capture_output=True,text=True,timeout=20)
    assert run.returncode==0,run.stderr
    return run.stdout


@pytest.mark.parametrize("kind",["OK","DEGRADED","ERROR"])
def test_render_global_states(tmp_path,kind):
    html=dom(fixture(tmp_path,kind))
    assert f'>{kind}<' in html and "RELATIVE_INSTRUMENTAL" in html and "INDOOR VALIDATION DATA" in html


@pytest.mark.parametrize("mode",["POINT_ONLY","LINE_ONLY","INTERPOLATED"])
def test_map_states(tmp_path,mode):
    assert mode in dom(fixture(tmp_path,"OK",mode))


def test_empty_missing_products(tmp_path):
    html=dom(fixture(tmp_path,"OK",products=False))
    assert html.count("NOT AVAILABLE")>=3 and "fixture-session" in html


def test_stale(tmp_path):
    html=dom(fixture(tmp_path,"OK",updated="2000-01-01T00:00:00Z"))
    assert ">STALE<" in html


def test_network_failure_preserves_page(tmp_path):
    shutil.copytree(DASH,tmp_path/"dashboard")
    html=dom(tmp_path,"/dashboard/?root=/missing")
    assert "DATA CONNECTION DEGRADED" in html and "waiting for derivatives" in html


def test_contract_polling_cache_and_no_science():
    js=(DASH/"app.js").read_text();html=(DASH/"index.html").read_text()
    assert "status.updated_utc===lastVersion" in js and "?v=${imageVersion(version)}" in js
    assert "setInterval(poll,CONFIG.pollMs)" in js and "pollMs:2000" in js
    assert all(token not in js.lower() for token in ("hdf5","numpy","scipy","astropy","fft("))
    assert all(token not in html for token in ("Kelvin"," Jy","GOTO","Start Capture","SYNC"))

def telemetry(status="OK",created="2099-01-01T00:00:00Z"):
    return {"status":status,"created_utc":created,"uptime_seconds":3600,"system":{"cpu_percent":12,"memory_percent":34},
      "storage":{"percent":56},"network":{"interface":"eth0","rx_bytes":100,"tx_bytes":200},
      "temperatures":{"sdr_c":25.5,"sdr_status":"OK","lna_c":26.5,"lna_status":"OK"},
      "sdr":{"rtl_tcp_process_detected":True,"rtl_tcp_port_listening":True,"expected_frequency_hz":1420405752,
      "expected_sample_rate_hz":2400000,"expected_gain_db":40.2,"bias_tee_expected":True},
      "mount":{"status":"NOT_EXPOSED","tracking":None,"ra":None,"dec":None}}

def test_telemetry_ok_and_system_render(tmp_path):
    html=dom(fixture(tmp_path,telemetry=telemetry()),"/dashboard/?root=/data&telemetry=/telemetry&snapshot=1")
    for text in ("TELEMETRY LIVE","25.5 °C","26.5 °C","DETECTED","LISTENING","NOT_EXPOSED","eth0"):assert text in html

def test_telemetry_degraded_stale_and_missing(tmp_path):
    html=dom(fixture(tmp_path,telemetry=telemetry("DEGRADED","2000-01-01T00:00:00Z")),"/dashboard/?root=/data&telemetry=/telemetry&snapshot=1")
    assert "TELEMETRY STALE" in html and "DEGRADED" in html
    missing=dom(fixture(tmp_path/"m"),"/dashboard/?root=/data&telemetry=/telemetry&snapshot=1")
    assert "TELEMETRY NOT AVAILABLE" in missing and "fixture-session" in missing


def test_build(tmp_path):
    run=subprocess.run([str(ROOT/".venv/bin/python"),str(DASH/"build_dashboard.py")],capture_output=True,text=True)
    assert run.returncode==0 and (DASH/"dist/index.html").exists()
