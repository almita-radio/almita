import csv, json
from collections import defaultdict
from astropy.time import Time
from capture import CaptureExecutor

FIELDS=["point_number","scan_order","target_ra_hours","target_dec_degrees","capture_status","start_time","end_time","duration","error_message","session_name","data_filename","target_ra_hms","target_dec_dms"]

def executor(tmp_path,statuses=None):
    statuses=statuses or ["planned"]*6;csv_path=tmp_path/"plan.csv"
    with csv_path.open("w",newline="") as h:
        w=csv.DictWriter(h,fieldnames=FIELDS);w.writeheader()
        for n,status in enumerate(statuses,1):w.writerow({"point_number":n,"scan_order":n,"target_ra_hours":n,"target_dec_degrees":-60,"capture_status":status,"session_name":"test","data_filename":f"p{n}.dat","target_ra_hms":str(n),"target_dec_dms":"-60"})
    (tmp_path/"observer_config.json").write_text(json.dumps({"observer":{"latitude_deg":-33.433,"longitude_deg":-70.666,"elevation_m":550}}))
    return CaptureExecutor(str(csv_path),config_path="observer_config.json")

def set_ha_map(ex,values): ex.hour_angle_for_point=lambda p,obstime=None:values[int(p["point_number"])]

def order(ex): return [int(p["point_number"]) for p in ex.iter_meridian_partitioned_points()]

def test_all_negative_preserves_scan_order(tmp_path):
    ex=executor(tmp_path);set_ha_map(ex,{n:-1 for n in range(1,7)});assert ex.load_observation_plan();assert order(ex)==[1,2,3,4,5,6];assert ex.meridian_partition_metadata["first_block"]=="negative"

def test_all_positive_preserves_scan_order(tmp_path):
    ex=executor(tmp_path);set_ha_map(ex,{n:1 for n in range(1,7)});assert ex.load_observation_plan();assert order(ex)==[1,2,3,4,5,6];assert ex.meridian_partition_metadata["first_block"]=="positive"

def test_mixed_blocks_keep_relative_serpentine_order(tmp_path):
    ex=executor(tmp_path);set_ha_map(ex,{1:-1,2:-1,3:1,4:-1,5:-1,6:1});assert ex.load_observation_plan();assert order(ex)==[1,2,4,5,3,6]

def test_first_pending_scan_order_selects_first_block(tmp_path):
    ex=executor(tmp_path);set_ha_map(ex,{1:1,2:-1,3:-1,4:1,5:-1,6:1});assert ex.load_observation_plan();assert ex.meridian_partition_metadata["first_block"]=="positive";assert order(ex)==[1,4,6,2,3,5]

def changing(ex,initial,changed,point_number):
    calls=defaultdict(int)
    def ha(point,obstime=None):
        n=int(point["point_number"]);calls[n]+=1
        return changed if n==point_number and calls[n]>1 else initial[n]
    ex.hour_angle_for_point=ha

def test_negative_to_positive_is_reclassified(tmp_path):
    ex=executor(tmp_path);changing(ex,{1:-1,2:-1,3:1,4:1,5:1,6:1},1,2);assert ex.load_observation_plan();points=list(ex.iter_meridian_partitioned_points());assert [int(p["point_number"]) for p in points]==[1,2,3,4,5,6];assert points[1]["_reclassified_due_to_ha_change"] is True

def test_positive_to_negative_is_reclassified(tmp_path):
    ex=executor(tmp_path);changing(ex,{1:1,2:1,3:-1,4:-1,5:-1,6:-1},-1,2);assert ex.load_observation_plan();points=list(ex.iter_meridian_partitioned_points());assert [int(p["point_number"]) for p in points]==[1,2,3,4,5,6];assert points[1]["_ha_block"]=="negative"

def test_resume_ignores_done_and_repartitions_pending(tmp_path):
    ex=executor(tmp_path,["DONE","planned","success","planned","failed","planned"]);set_ha_map(ex,{1:-1,2:1,3:-1,4:-1,5:1,6:1});assert ex.load_observation_plan(resume=True);assert order(ex)==[2,6,3,4]

def test_resume_uses_current_time(tmp_path):
    ex=executor(tmp_path);now=Time("2026-08-20T20:00:00");ex.time_provider=lambda:now;seen=[]
    def ha(point,obstime=None):seen.append(obstime);return -1
    ex.hour_angle_for_point=ha;assert ex.load_observation_plan(resume=True);assert all(t==now for t in seen);assert ex.meridian_partition_metadata["initial_time_utc"]==now.utc.isot

def test_planned_scan_order_is_unchanged(tmp_path):
    ex=executor(tmp_path);set_ha_map(ex,{1:-1,2:1,3:-1,4:1,5:-1,6:1});assert ex.load_observation_plan();list(ex.iter_meridian_partitioned_points());assert [int(p["scan_order"]) for p in ex.observation_points]==[1,2,3,4,5,6]

def test_actual_capture_order_is_persisted_without_hardware(tmp_path):
    ex=executor(tmp_path);set_ha_map(ex,{1:-1,2:1,3:-1,4:1,5:-1,6:1});assert ex.load_observation_plan()
    for actual,p in enumerate(ex.iter_meridian_partitioned_points(),1):ex.persist_selection_metadata(p,actual)
    rows=list(csv.DictReader(ex.csv_path.open()));actual={int(r["point_number"]):int(r["actual_capture_order"]) for r in rows};assert actual=={1:1,3:2,5:3,2:4,4:5,6:6};assert [int(r["scan_order"]) for r in rows]==[1,2,3,4,5,6]

def test_resume_discards_orders_without_valid_hdf5(tmp_path):
    ex=executor(tmp_path,["success","planned","success","planned","planned","planned"])
    rows=list(csv.DictReader(ex.csv_path.open()));fields=list(rows[0])+["actual_capture_order"]
    rows[0]["actual_capture_order"]="1";rows[2]["actual_capture_order"]="2"
    with ex.csv_path.open("w",newline="") as h:
        w=csv.DictWriter(h,fieldnames=fields);w.writeheader();w.writerows(rows)
    set_ha_map(ex,{1:-1,2:-1,3:-1,4:-1,5:-1,6:-1});assert ex.load_observation_plan(resume=True);assert ex.actual_capture_order_offset==0
    for idx,p in enumerate(ex.iter_meridian_partitioned_points(),1):ex.persist_selection_metadata(p,ex.actual_capture_order_offset+idx)
    final=list(csv.DictReader(ex.csv_path.open()));assert [int(r["actual_capture_order"]) for r in final]==[1,2,3,4,5,6]

def test_capture_source_does_not_use_slew_predictor_or_new_goto_logic():
    source=open("capture.py",encoding="utf-8").read();assert "predict_slew" not in source;assert source.count("self.telescope.goto(")==1
