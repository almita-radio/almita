import csv, json
from types import SimpleNamespace
import pytest
from capture import CaptureExecutor, should_execute_after_preflight

FIELDS=["point_number","scan_order","target_ra_hours","target_dec_degrees","capture_status","start_time","end_time","duration","error_message","data_filename","session_name"]

class FakeH5File:
    def __init__(self,*args,**kwargs):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def create_dataset(self,*args,**kwargs):return None

class FakeH5py: File=FakeH5File

class FakeSDR:
    fail=False
    def __init__(self,**kwargs):self.kwargs=kwargs
    async def connect(self):
        if self.fail:raise ConnectionError("rtl_tcp unavailable")
    async def configure(self,**kwargs):self.config=kwargs
    async def close(self):pass

class FakeTelescope:
    def __init__(self,coordinates=(5.0,-40.0)):self.coordinates=coordinates;self.goto_calls=0
    async def get_coordinates(self,force_refresh=False):return self.coordinates
    async def goto(self,*args):self.goto_calls+=1;return True

async def safe_properties(home="Idle"):
    return {"EQUATORIAL_EOD_COORD._STATE":"Idle","TELESCOPE_MOTION_NS.MOTION_NORTH":"Off","TELESCOPE_MOTION_NS.MOTION_SOUTH":"Off","TELESCOPE_MOTION_WE.MOTION_WEST":"Off","TELESCOPE_MOTION_WE.MOTION_EAST":"Off","TELESCOPE_HOME._STATE":home,"TELESCOPE_HOME.GO":"On" if home=="Busy" else "Off"}

def make_executor(tmp_path,statuses=("planned",),mode="network",csv_exists=True):
    plan=tmp_path/"plan.csv"
    if csv_exists:
        with plan.open("w",newline="") as h:
            w=csv.DictWriter(h,fieldnames=FIELDS);w.writeheader()
            for n,status in enumerate(statuses,1):w.writerow({"point_number":n,"scan_order":n,"target_ra_hours":n,"target_dec_degrees":-40,"capture_status":status,"data_filename":f"p{n}.dat","session_name":"test"})
    (tmp_path/"observer_config.json").write_text(json.dumps({"observer":{"latitude_deg":-33.4,"longitude_deg":-70.6,"elevation_m":550}}))
    ex=CaptureExecutor(str(plan),config_path="observer_config.json",sdr_mode=mode);ex.telescope=FakeTelescope();return ex

async def run(ex,**kwargs):
    return await ex.run_preflight(10,sdr_factory=kwargs.pop("sdr_factory",FakeSDR),h5py_loader=kwargs.pop("h5py_loader",lambda:FakeH5py),disk_usage=kwargs.pop("disk_usage",lambda p:SimpleNamespace(free=10**12)),indi_property_reader=kwargs.pop("indi_property_reader",safe_properties),**kwargs)

def status(report,name):return next(c["status"] for c in report["checks"] if c["name"]==name)

@pytest.mark.asyncio
async def test_complete_preflight_pass(tmp_path):
    report=await run(make_executor(tmp_path,statuses=("planned","planned")));assert report["success"];assert all(c["status"] in {"PASS","WARN"} for c in report["checks"])

@pytest.mark.asyncio
async def test_generated_valid_observer_config_passes_preflight(tmp_path):
    plan=tmp_path/"plan.csv"
    with plan.open("w",newline="") as h:
        w=csv.DictWriter(h,fieldnames=FIELDS);w.writeheader()
        w.writerow({"point_number":1,"scan_order":1,"target_ra_hours":1,"target_dec_degrees":-40,"capture_status":"planned","data_filename":"p1.dat","session_name":"test"})
    config=tmp_path/"observer_config.json"
    assert not config.exists()
    ex=CaptureExecutor(str(plan),config_path="observer_config.json",sdr_mode="network")
    ex.telescope=FakeTelescope()
    assert config.is_file()
    report=await run(ex)
    assert status(report,"Observer config")=="PASS"
    assert report["success"]

@pytest.mark.asyncio
async def test_invalid_observer_config_json_fails_preflight(tmp_path):
    ex=make_executor(tmp_path)
    (tmp_path/"observer_config.json").write_text("{")
    ex=CaptureExecutor(str(ex.csv_path),config_path="observer_config.json",sdr_mode="network")
    ex.telescope=FakeTelescope()
    report=await run(ex)
    assert status(report,"Observer config")=="FAIL"
    assert not report["success"]

@pytest.mark.asyncio
async def test_observer_config_without_required_coordinates_fails_preflight(tmp_path):
    ex=make_executor(tmp_path)
    (tmp_path/"observer_config.json").write_text(json.dumps({"observer":{"latitude_deg":-33.4}}))
    ex=CaptureExecutor(str(ex.csv_path),config_path="observer_config.json",sdr_mode="network")
    ex.telescope=FakeTelescope()
    report=await run(ex)
    assert status(report,"Observer config")=="FAIL"
    assert not report["success"]

@pytest.mark.asyncio
async def test_out_of_range_observer_coordinates_fail_preflight(tmp_path):
    ex=make_executor(tmp_path)
    (tmp_path/"observer_config.json").write_text(json.dumps({"observer":{"latitude_deg":-91,"longitude_deg":-70.6}}))
    ex=CaptureExecutor(str(ex.csv_path),config_path="observer_config.json",sdr_mode="network")
    ex.telescope=FakeTelescope()
    report=await run(ex)
    assert status(report,"Observer config")=="FAIL"
    assert not report["success"]

@pytest.mark.asyncio
async def test_temporary_observer_config_path_is_not_trusted_or_generated(tmp_path):
    ex=make_executor(tmp_path)
    temporary=tmp_path/"observer_config.json.part"
    ex=CaptureExecutor(str(ex.csv_path),config_path=temporary.name,sdr_mode="network")
    ex.telescope=FakeTelescope()
    report=await run(ex)
    assert not temporary.exists()
    assert status(report,"Observer config")=="FAIL"
    assert not report["success"]

@pytest.mark.asyncio
async def test_missing_grid_fails(tmp_path):
    report=await run(make_executor(tmp_path,csv_exists=False));assert not report["success"];assert status(report,"Grid")=="FAIL"

@pytest.mark.asyncio
async def test_grid_without_planned_fails(tmp_path):
    report=await run(make_executor(tmp_path,statuses=("success","failed")));assert not report["success"];assert status(report,"Grid")=="FAIL"

@pytest.mark.asyncio
async def test_grid_with_invalid_target_fails(tmp_path):
    ex=make_executor(tmp_path);rows=list(csv.DictReader(ex.csv_path.open()));rows[0]["target_dec_degrees"]="nan"
    with ex.csv_path.open("w",newline="") as h:
        w=csv.DictWriter(h,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
    report=await run(ex);assert not report["success"];assert status(report,"Grid")=="FAIL"

@pytest.mark.asyncio
async def test_h5py_unavailable_fails(tmp_path):
    def missing():raise ImportError("no h5py")
    report=await run(make_executor(tmp_path),h5py_loader=missing);assert not report["success"];assert status(report,"HDF5/output")=="FAIL"

@pytest.mark.asyncio
async def test_output_not_writable_fails(tmp_path):
    def denied():raise PermissionError("read-only filesystem")
    report=await run(make_executor(tmp_path),h5py_loader=denied);assert not report["success"]

@pytest.mark.asyncio
async def test_insufficient_disk_fails(tmp_path):
    report=await run(make_executor(tmp_path),disk_usage=lambda p:SimpleNamespace(free=1));assert not report["success"];assert status(report,"Disk space")=="FAIL"

@pytest.mark.asyncio
async def test_network_sdr_unavailable_fails(tmp_path):
    class BrokenSDR(FakeSDR):fail=True
    report=await run(make_executor(tmp_path),sdr_factory=BrokenSDR);assert not report["success"];assert status(report,"SDR network")=="FAIL"

@pytest.mark.asyncio
async def test_usb_is_explicitly_unsupported(tmp_path):
    report=await run(make_executor(tmp_path,mode="usb"));assert not report["success"];assert status(report,"SDR USB")=="FAIL"

@pytest.mark.asyncio
async def test_indi_unavailable_fails(tmp_path):
    ex=make_executor(tmp_path);ex.telescope=None;report=await run(ex);assert not report["success"];assert status(report,"INDI/mount")=="FAIL"

@pytest.mark.asyncio
async def test_invalid_mount_coordinates_fail(tmp_path):
    ex=make_executor(tmp_path);ex.telescope=FakeTelescope((float("nan"),100));report=await run(ex);assert not report["success"]

@pytest.mark.asyncio
async def test_stale_home_busy_alone_does_not_fail(tmp_path):
    async def stale():return await safe_properties("Busy")
    report=await run(make_executor(tmp_path),indi_property_reader=stale);assert report["success"];assert status(report,"INDI/mount")=="PASS"

@pytest.mark.asyncio
async def test_failed_preflight_never_calls_goto(tmp_path):
    ex=make_executor(tmp_path);report=await run(ex,disk_usage=lambda p:SimpleNamespace(free=1));assert not should_execute_after_preflight(report,False);assert ex.telescope.goto_calls==0

@pytest.mark.asyncio
async def test_preflight_does_not_change_capture_status(tmp_path):
    ex=make_executor(tmp_path,statuses=("planned","planned"));before=ex.csv_path.read_text();await run(ex);assert ex.csv_path.read_text()==before

@pytest.mark.asyncio
async def test_preflight_only_gate_never_executes(tmp_path):
    ex=make_executor(tmp_path);report=await run(ex);assert report["success"];assert not should_execute_after_preflight(report,True);assert ex.telescope.goto_calls==0
