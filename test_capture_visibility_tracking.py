import asyncio,csv,json,re
from types import SimpleNamespace
import pytest
import capture
from capture import CaptureExecutor

FIELDS=["point_number","scan_order","target_ra_hours","target_dec_degrees","target_ra_hms","target_dec_dms","capture_status","start_time","end_time","duration","error_message","data_filename","session_name"]

class FakeSession:
    def __init__(self):self.actions=[]
    def create_session(self,**kwargs):self.actions.append("create");return "session"
    def update_session(self,*args,**kwargs):self.actions.append("update")
    def complete_session(self,*args):self.actions.append("complete")
    def pause_session(self,*args):self.actions.append("pause")

class FakeTelescope:
    device_name="fake"
    def __init__(self,goto=True,state="off",wait_on=True,wait_off=True,set_on=True,set_off=True,onstep_statuses=None,cached_onstep_status=None):
        self.goto_result=goto;self.state=state;self.wait_on=wait_on;self.wait_off=wait_off;self.set_on=set_on;self.set_off=set_off;self.events=[]
        self.onstep_statuses=list(onstep_statuses or [])
        self.cached_onstep_status=cached_onstep_status
        self.onstep_seq=0
    async def goto(self,*args):self.events.append("goto");return self.goto_result
    async def get_onstep_status(self,timeout=1):
        self.events.append("onstep_cached")
        return self.cached_onstep_status or {"state":"healthy","message":"None","is_error":False,"received_at":"2026-08-20T00:00:00+00:00","fresh":True,"hardware_fresh":False,"source":"indi_cached","update_seq":self.onstep_seq,"reason":None}
    async def wait_onstep_status_update(self,timeout=2.5):
        self.events.append("onstep")
        if self.onstep_statuses:return self.onstep_statuses.pop(0)
        self.onstep_seq+=1
        return {"state":"healthy","message":"None","is_error":False,"received_at":"2026-08-20T00:00:00+00:00","fresh":True,"hardware_fresh":True,"source":"indi_poll","update_seq":self.onstep_seq,"reason":None}
    async def get_tracking_state(self,timeout=1):self.events.append("get_tracking");return self.state
    async def set_tracking(self,enable):self.events.append("set_on" if enable else "set_off");return self.set_on if enable else self.set_off
    async def wait_tracking_state(self,expected_on,timeout=5):self.events.append("wait_on" if expected_on else "wait_off");return self.wait_on if expected_on else self.wait_off

class FakeSDR:
    fail_capture=False
    events=[]
    def __init__(self,**kwargs):type(self).events=[]
    async def connect(self):self.events.append("connect")
    async def configure(self,**kwargs):self.events.append("configure")
    async def flush_buffer(self):self.events.append("flush");return 0
    async def capture(self,**kwargs):
        self.events.append("capture")
        if self.fail_capture:raise RuntimeError("HDF5/SDR failure")
        callback=getattr(self,"timing_callback",None)
        if callback:
            callback("capture_end",{"duration":2.0,"samples_received":4_800_000})
            callback("disk_write_start",{"file":kwargs["output_file"]})
            callback("disk_write_end",{"duration":0.7,"file":kwargs["output_file"],"size_mb":5.2})
        return SimpleNamespace(capture_time=2.0,disk_write_time=0.7,throughput_mbps=4.8,total_samples=4_800_000)
    async def close(self):self.events.append("close")

def make_executor(tmp_path,count=3,min_altitude=30):
    tmp_path.mkdir(parents=True,exist_ok=True)
    plan=tmp_path/"plan.csv"
    with plan.open("w",newline="") as h:
        w=csv.DictWriter(h,fieldnames=FIELDS);w.writeheader()
        for n in range(1,count+1):w.writerow({"point_number":n,"scan_order":n,"target_ra_hours":n,"target_dec_degrees":-40,"target_ra_hms":str(n),"target_dec_dms":"-40","capture_status":"planned","data_filename":f"p{n}.dat","session_name":"test"})
    (tmp_path/"observer_config.json").write_text(json.dumps({"observer":{"latitude_deg":-33.4,"longitude_deg":-70.6,"elevation_m":550}}))
    ex=CaptureExecutor(str(plan),config_path="observer_config.json",min_altitude_deg=min_altitude);ex.session_manager=FakeSession();ex.hour_angle_for_point=lambda p,obstime=None:-1;assert ex.load_observation_plan();return ex

def visibility_map(ex,altitudes):
    def value(point,obstime=None):
        n=int(point["point_number"]);return {"altitude_deg_at_goto":altitudes[n],"azimuth_deg_at_goto":100+n,"ha_hours_at_goto":-1,"visibility_checked_at":"2026-08-20T00:00:00.000","min_altitude_deg":ex.min_altitude_deg}
    ex.visibility_for_point=value

def csv_rows(ex):return list(csv.DictReader(ex.csv_path.open()))

@pytest.mark.asyncio
async def test_compact_console_operational_output(monkeypatch,tmp_path,capsys):
    class WallTimedFakeSDR(FakeSDR):
        async def capture(self,**kwargs):
            # Match the timing telemetry emitted by FakeSDR: 2.0 s receiving
            # plus 0.7 s writing.  Wall-clock assertions must measure reality,
            # not merely trust the reported phase metrics.
            await asyncio.sleep(2.7)
            return await super().capture(**kwargs)
    monkeypatch.setattr(capture,"SDRCapture",WallTimedFakeSDR)
    ex=make_executor(tmp_path,count=1);visibility_map(ex,{1:45});ex.telescope=FakeTelescope();ex.compact_console=True
    ex.print_console_header(0,2)
    assert await ex.execute_observation_plan(0,2)
    output=capsys.readouterr().out
    for expected in [
        "ALMITA CAPTURE","Session","Targets","SDR endpoint","POINT 001/001",
        "TARGET RA=","MOUNT    GOTO","MOUNT    TRACK","CAPTURE  SETTLE",
        "SDR      FLUSH","SDR      CAPTURE","DISK     WRITE","✓ POINT 001/001",
        "SESSION  elapsed=","SESSION COMPLETE","Success      1",".part        0",
    ]: assert expected in output
    assert "RUNNING TOTAL" not in output
    assert "POINT SUMMARY" not in output
    assert "TARGET RA=01:00:00.0  DEC=-40:00:00" in output
    assert "MOUNT    MOTION" not in output
    point_total=re.search(r"✓ POINT 001/001\s+total=([0-9.]+)s",output)
    assert point_total and float(point_total.group(1))>=2.6
    measured_point_wall=ex._live_timing_rows[0]["point_total_sec"]
    assert measured_point_wall>=2.6
    assert measured_point_wall>=2.0
    session_elapsed=re.search(r"SESSION\s+elapsed=(\d\d):(\d\d):(\d\d)",output)
    assert session_elapsed
    session_seconds=sum(value*factor for value,factor in zip(map(int,session_elapsed.groups()),(3600,60,1)))
    assert session_seconds>=round(measured_point_wall)
    assert "SESSION  elapsed=00:00:00" not in output
    final_elapsed=re.search(r"Elapsed\s+(\d\d:\d\d:\d\d)",output)
    assert final_elapsed and final_elapsed.group(1)==":".join(session_elapsed.groups())
    assert "OnStep       N/D" in output

def test_above_and_equal_minimum_are_executable(tmp_path):
    ex=make_executor(tmp_path,count=2);visibility_map(ex,{1:31,2:30});assert [int(p["point_number"]) for p in ex.iter_runtime_visible_points()]==[1,2]

def test_below_minimum_is_deferred_and_stays_planned(tmp_path):
    ex=make_executor(tmp_path,count=1);visibility_map(ex,{1:29});assert list(ex.iter_runtime_visible_points())==[];row=csv_rows(ex)[0];assert row["capture_status"]=="planned";assert row["visibility_deferred"]=="true";assert not row.get("actual_capture_order")

def test_invisible_then_visible_preserves_remaining_order(tmp_path):
    ex=make_executor(tmp_path);visibility_map(ex,{1:20,2:40,3:41});assert [int(p["point_number"]) for p in ex.iter_runtime_visible_points()]==[2,3]

def test_all_invisible_finishes_single_pass(tmp_path):
    ex=make_executor(tmp_path);visibility_map(ex,{1:1,2:2,3:3});assert list(ex.iter_runtime_visible_points())==[];assert ex.visibility_deferred_count==3

def test_actual_order_excludes_deferred(tmp_path):
    ex=make_executor(tmp_path);visibility_map(ex,{1:20,2:40,3:41})
    for actual,p in enumerate(ex.iter_runtime_visible_points(),1):ex.persist_selection_metadata(p,actual)
    rows=csv_rows(ex);assert [r.get("actual_capture_order","") for r in rows]==["","1","2"]

def test_resume_recalculates_visibility(tmp_path):
    ex=make_executor(tmp_path,count=1);calls=[]
    def current(point,obstime=None):calls.append(point["point_number"]);return {"altitude_deg_at_goto":40,"azimuth_deg_at_goto":1,"ha_hours_at_goto":-1,"visibility_checked_at":"now","min_altitude_deg":30}
    ex.visibility_for_point=current;assert [p["point_number"] for p in ex.iter_runtime_visible_points()]==["1"];assert calls==["1"]

def test_ha_reclassification_precedes_visibility(tmp_path):
    ex=make_executor(tmp_path,count=1);calls=0
    def changing(point,obstime=None):
        nonlocal calls;calls+=1;return -1 if calls==1 else 1
    ex.hour_angle_for_point=changing;ex.partition_pending_by_hour_angle()
    def inspect(point,obstime=None):
        assert point["_ha_block"]=="positive";return {"altitude_deg_at_goto":40,"azimuth_deg_at_goto":1,"ha_hours_at_goto":1,"visibility_checked_at":"now","min_altitude_deg":30}
    ex.visibility_for_point=inspect;assert len(list(ex.iter_runtime_visible_points()))==1

@pytest.mark.asyncio
async def test_tracking_confirmed_allows_capture_and_precedes_flush(monkeypatch,tmp_path):
    monkeypatch.setattr(capture,"SDRCapture",FakeSDR);ex=make_executor(tmp_path,count=1);visibility_map(ex,{1:40});ex.telescope=FakeTelescope();assert await ex.execute_observation_plan(0,0);assert "wait_on" in ex.telescope.events;assert FakeSDR.events.index("flush")<FakeSDR.events.index("capture")

@pytest.mark.asyncio
async def test_set_tracking_failure_prevents_capture(monkeypatch,tmp_path):
    monkeypatch.setattr(capture,"SDRCapture",FakeSDR);ex=make_executor(tmp_path,count=1);visibility_map(ex,{1:40});ex.telescope=FakeTelescope(set_on=False);assert not await ex.execute_observation_plan(0,0);assert "capture" not in FakeSDR.events

@pytest.mark.asyncio
async def test_wait_tracking_failure_or_alert_prevents_capture(monkeypatch,tmp_path):
    monkeypatch.setattr(capture,"SDRCapture",FakeSDR)
    for state,wait in [("off",False),("alert",True)]:
        ex=make_executor(tmp_path/str(state),count=1);visibility_map(ex,{1:40});ex.telescope=FakeTelescope(state=state,wait_on=wait);assert not await ex.execute_observation_plan(0,0);assert "capture" not in FakeSDR.events

@pytest.mark.asyncio
async def test_tracking_failure_after_goto_pauses_without_next_goto(monkeypatch,tmp_path):
    monkeypatch.setattr(capture,"SDRCapture",FakeSDR)
    ex=make_executor(tmp_path,count=2);visibility_map(ex,{1:40,2:40})
    ex.telescope=FakeTelescope(state="off",wait_on=False)
    assert not await ex.execute_observation_plan(0,0)
    rows=csv_rows(ex)
    assert [row["capture_status"] for row in rows]==["failed","planned"]
    assert rows[0]["error_message"]=="tracking not confirmed: off; OnStep healthy: None"
    assert ex.telescope.events.count("goto")==1
    assert "pause" in ex.session_manager.actions

@pytest.mark.asyncio
async def test_safety_paused_plan_can_resume_preserving_failed_and_planned(monkeypatch,tmp_path):
    monkeypatch.setattr(capture,"SDRCapture",FakeSDR)
    ex=make_executor(tmp_path,count=3);visibility_map(ex,{1:40,2:40,3:40})
    ex.telescope=FakeTelescope(state="off",wait_on=False)
    assert not await ex.execute_observation_plan(0,0)

    resumed=CaptureExecutor(str(ex.csv_path),session_id="session",config_path="observer_config.json")
    resumed.session_manager=FakeSession();resumed.hour_angle_for_point=lambda p,obstime=None:-1
    assert resumed.load_observation_plan(resume=True)
    rows=csv_rows(resumed)
    assert [row["capture_status"] for row in rows]==["failed","planned","planned"]
    assert [int(point["point_number"]) for point in resumed.observation_points]==[2,3]

@pytest.mark.asyncio
async def test_goto_failure_never_enables_tracking_or_captures(monkeypatch,tmp_path):
    monkeypatch.setattr(capture,"SDRCapture",FakeSDR);ex=make_executor(tmp_path,count=1);visibility_map(ex,{1:40});ex.telescope=FakeTelescope(goto=False);assert not await ex.execute_observation_plan(0,0);assert "set_on" not in ex.telescope.events;assert "capture" not in FakeSDR.events

@pytest.mark.asyncio
async def test_normal_finish_turns_tracking_off(monkeypatch,tmp_path):
    monkeypatch.setattr(capture,"SDRCapture",FakeSDR);ex=make_executor(tmp_path,count=1);visibility_map(ex,{1:40});ex.telescope=FakeTelescope();await ex.execute_observation_plan(0,0);assert ex.telescope.events[-2:]==["set_off","wait_off"]

@pytest.mark.asyncio
async def test_capture_exception_still_turns_tracking_off(monkeypatch,tmp_path):
    class Broken(FakeSDR):fail_capture=True
    monkeypatch.setattr(capture,"SDRCapture",Broken);ex=make_executor(tmp_path,count=1);visibility_map(ex,{1:40});ex.telescope=FakeTelescope();assert not await ex.execute_observation_plan(0,0);row=csv_rows(ex)[0];assert row["capture_status"]=="failed";assert row["failed_at"];assert row["error_code"]=="RuntimeError";assert "HDF5/SDR failure" in row["error_detail"];assert ex.telescope.events[-2:]==["set_off","wait_off"]

@pytest.mark.asyncio
async def test_no_visible_targets_pauses_and_turns_tracking_off(monkeypatch,tmp_path):
    monkeypatch.setattr(capture,"SDRCapture",FakeSDR);ex=make_executor(tmp_path,count=2);visibility_map(ex,{1:1,2:2});ex.telescope=FakeTelescope();assert not await ex.execute_observation_plan(0,0);assert "pause" in ex.session_manager.actions;assert ex.telescope.events[-2:]==["set_off","wait_off"]

@pytest.mark.asyncio
async def test_tracking_off_failure_is_best_effort(monkeypatch,tmp_path,capsys):
    class Broken(FakeSDR):fail_capture=True
    monkeypatch.setattr(capture,"SDRCapture",Broken);ex=make_executor(tmp_path,count=1);visibility_map(ex,{1:40});ex.telescope=FakeTelescope(set_off=False);assert not await ex.execute_observation_plan(0,0);assert "Tracking OFF could not be confirmed" in capsys.readouterr().out
