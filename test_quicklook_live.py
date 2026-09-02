import csv
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

import quicklook_live as ql

PROFILE=Path("data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1.npz")


def capture(path, topology=True):
    with h5py.File(path,"w") as f:
        f.create_dataset("iq_data",data=np.zeros(32,dtype=np.uint8))
        f.attrs["center_frequency_hz"]=1420405752;f.attrs["sample_rate_hz"]=2400000;f.attrs["gain"]=40.2
        if topology:f.attrs["rf_input"]="ANTENNA_AT_LNA_INPUT_INDOOR"


def manifest(path,rows):
    fields=["point_id","status","source_hdf5","coordinate_source","ra_deg","dec_deg","environment"]
    with (path/"session.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)


def row(pid,status,source,ra="10",dec="20"):
    return {"point_id":pid,"status":status,"source_hdf5":source,"coordinate_source":"COMMANDED",
            "ra_deg":ra,"dec_deg":dec,"environment":"INDOOR_DEPARTMENT"}


def make_grid(session,rows,cols,width_deg=30.0,height_deg=30.0,coords=None):
    """Write grid_generator.py's own canonical artifacts (mosaic.csv,
    grid_metadata.json) directly into the session dir so
    find_grid_directory() locates them at level 0 - point_id is row-major
    (row*cols+col+1), independent of any capture/scan order."""
    (session/"grid_metadata.json").write_text(json.dumps({"grid":{"rows":rows,"columns":cols,
        "total_points":rows*cols,"width_deg":width_deg,"height_deg":height_deg}}))
    fields=["point_number","point_id","scan_order","grid_row","grid_col","row","column","ra","dec",
            "capture_status","visibility_deferred","session_name"]
    with (session/"mosaic.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in range(rows):
            for c in range(cols):
                pid=r*cols+c+1
                ra,dec=(coords[pid] if coords and pid in coords else (10.0+c,20.0+r))
                w.writerow({"point_number":pid,"point_id":pid,"scan_order":pid,"grid_row":r,"grid_col":c,
                            "row":r,"column":c,"ra":ra,"dec":dec,"capture_status":"planned",
                            "visibility_deferred":"false","session_name":"s"})


def fake_products(monkeypatch):
    def spectrum(source,profile,out):
        out=Path(out);out.mkdir(parents=True,exist_ok=True)
        if "broken" in str(source): raise RuntimeError("synthetic processor failure")
        (out/"quicklook_spectrum.json").write_text('{"calibration_level":"RELATIVE_INSTRUMENTAL","absolute_calibration":false}')
        (out/"quicklook_spectrum.png").write_bytes(b"png")
        (out/"quicklook_fractional_excess.png").write_bytes(b"png")
        f=np.linspace(1419e6,1422e6,16);v=np.linspace(0,.1,16);u=np.full(16,.01);m=np.ones(16,bool)
        return {},{"frequency_hz":f,"fractional_excess":v,"fractional_uncertainty":u,"valid_mask":m,
          "relative_psd_db":v}
    def waterfall(source,profile,out):
        out=Path(out);out.mkdir(parents=True,exist_ok=True)
        (out/"quicklook_waterfall.json").write_text('{"calibration_level":"RELATIVE_INSTRUMENTAL","absolute_calibration":false}')
        (out/"quicklook_waterfall.png").write_bytes(b"png");(out/"quicklook_waterfall.npz").write_bytes(b"npz")
        return {},{}
    monkeypatch.setattr(ql,"generate_quicklook",spectrum);monkeypatch.setattr(ql,"generate_waterfall",waterfall)


def test_empty_session(tmp_path):
    session=tmp_path/"s";session.mkdir();manifest(session,[])
    status=ql.QuicklookLive(session,PROFILE,tmp_path/"out").run(once=True)
    assert status["status"]=="IDLE" and status["points_seen"]==0


def test_success_latest_restart_and_source_immutability(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();make_grid(session,1,1)
    source=session/"one.h5";capture(source)
    before=source.read_bytes();manifest(session,[row("1","SUCCESS","one.h5")])
    out=tmp_path/"out";first=ql.QuicklookLive(session,PROFILE,out).run(True)
    assert first["points_processed"]==1 and (out/"latest_spectrum.json").exists()
    assert (out/"latest_waterfall.png").exists() and (out/"quicklook_map.json").exists()
    assert (out/"session_waterfall.png").exists() and (out/"session_waterfall.json").exists()
    doc=json.loads((out/"quicklook_map.json").read_text())
    assert doc["status"]=="NATIVE_GRID" and doc["map_mode"]=="NATIVE_GRID"
    assert doc["quicklook_metrics"]["total_cells"]==1 and doc["quicklook_metrics"]["observed_cells"]==1
    second=ql.QuicklookLive(session,PROFILE,out).run(True)
    assert second["points_processed"]==1 and second["performance"]["points"]==[]
    assert source.read_bytes()==before


def test_native_grid_observed_cells_grow_without_changing_mode(tmp_path,monkeypatch):
    """Replaces the old POINT_ONLY/LINE_ONLY/INTERPOLATED progression: the
    map mode is always NATIVE_GRID regardless of how many points have been
    observed - only observed_cells grows."""
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();make_grid(session,2,2);capture(session/"x.h5")
    out=tmp_path/"out";rows=[]
    for pid,expected_observed in [("1",1),("2",2),("3",3)]:
        rows.append(row(pid,"SUCCESS","x.h5"));manifest(session,rows)
        ql.QuicklookLive(session,PROFILE,out).run(True)
        doc=json.loads((out/"quicklook_map.json").read_text())
        assert doc["status"]=="NATIVE_GRID"
        assert doc["quicklook_metrics"]["total_cells"]==4
        assert doc["quicklook_metrics"]["observed_cells"]==expected_observed


def test_session_waterfall_accumulates_one_row_per_point(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();make_grid(session,2,2);capture(session/"x.h5")
    out=tmp_path/"out";rows=[]
    for pid in ("1","2","3"):
        rows.append(row(pid,"SUCCESS","x.h5"));manifest(session,rows)
        ql.QuicklookLive(session,PROFILE,out).run(True)
    document=json.loads((out/"session_waterfall.json").read_text())
    assert document["point_ids"]==["1","2","3"]
    assert document["point_count"]==3


def test_failed_deferred_part_ignored_then_final_processed(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();capture(session/"good.h5")
    (session/"later.h5.part").write_bytes(b"partial")
    rows=[row("f","FAILED","good.h5"),row("d","DEFERRED","good.h5"),row("p","SUCCESS","later.h5.part")]
    manifest(session,rows);out=tmp_path/"out";status=ql.QuicklookLive(session,PROFILE,out).run(True)
    assert status["points_processed"]==0
    capture(session/"later.h5");rows[-1]["source_hdf5"]="later.h5";manifest(session,rows)
    status=ql.QuicklookLive(session,PROFILE,out).run(True);assert status["points_processed"]==1


def test_unknown_and_incompatible_skipped(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();capture(session/"unknown.h5",False);capture(session/"bad.h5")
    with h5py.File(session/"bad.h5","a") as f:f.attrs["gain"]=20
    manifest(session,[row("u","SUCCESS","unknown.h5"),row("i","SUCCESS","bad.h5")])
    status=ql.QuicklookLive(session,PROFILE,tmp_path/"out").run(True)
    assert status["points_skipped"]==2 and status["status"]=="DEGRADED"


def test_invalid_point_isolated_and_next_processed(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();capture(session/"broken.h5");capture(session/"good.h5")
    manifest(session,[row("1","SUCCESS","broken.h5"),row("2","SUCCESS","good.h5")])
    status=ql.QuicklookLive(session,PROFILE,tmp_path/"out").run(True)
    assert len(status["errors"])==1 and status["points_processed"]==1


def test_duplicate_backlog_and_missing_position(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();capture(session/"x.h5")
    rows=[row("1","SUCCESS","x.h5","",""),row("1","SUCCESS","x.h5"),row("2","SUCCESS","x.h5")]
    manifest(session,rows);status=ql.QuicklookLive(session,PROFILE,tmp_path/"out").run(True)
    assert status["backlog_initial"]==2 and status["points_processed"]==2
    assert any(w.get("warning")=="DUPLICATE_POINT_ID" for w in status["warnings"])
    assert any(w.get("warning")=="MISSING_POSITION_METADATA" for w in status["warnings"])


def test_source_changed_not_reprocessed(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();capture(session/"x.h5");manifest(session,[row("1","SUCCESS","x.h5")])
    out=tmp_path/"out";ql.QuicklookLive(session,PROFILE,out).run(True)
    with h5py.File(session/"x.h5","a") as f:f.attrs["note"]="changed"
    status=ql.QuicklookLive(session,PROFILE,out).run(True)
    assert any(w.get("warning")=="SOURCE_CHANGED_AFTER_PROCESSING" for w in status["warnings"])


def test_atomic_helpers_and_stop(tmp_path):
    path=tmp_path/"x.json";ql.atomic_json(path,{"x":1});assert json.loads(path.read_text())=={"x":1}
    assert not (tmp_path/"x.json.tmp").exists();ql.request_stop();assert ql.STOP_REQUESTED


# ---------------------------------------------------------------- ANTENNA B occupancy map wiring


def mark_success(grid_dir,point_id,start,end):
    """Rewrite mosaic.csv's capture_status/start_time/end_time for one
    point, the same way capture.py's own update_point_status does - so the
    RFI occupancy map correlation has a real [start,end] window to use."""
    path=grid_dir/"mosaic.csv"
    with path.open(newline="") as f:
        reader=csv.DictReader(f);fieldnames=list(reader.fieldnames or []);rows=list(reader)
    for field in ("start_time","end_time"):
        if field not in fieldnames:fieldnames.append(field)
    for r in rows:
        if r["point_id"]==str(point_id):
            r["capture_status"]="SUCCESS";r["start_time"]=start;r["end_time"]=end
    with path.open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fieldnames);w.writeheader();w.writerows(rows)


def write_rfi_history(runtime_dir,session_id,samples):
    ql.atomic_json(runtime_dir/"rfi_ref_history.json",{"schema_version":1,"session_id":session_id,
        "device_serial":"00000002","samples":samples,"updated_utc":"2026-09-02T00:00:00+00:00"})


def test_rfi_occupancy_map_built_from_matching_session_history(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();make_grid(session,1,2)
    mark_success(session,1,"2026-09-02T00:00:00+00:00","2026-09-02T00:00:10+00:00")
    source=session/"one.h5";capture(source);manifest(session,[row("1","SUCCESS","one.h5")])
    runtime=tmp_path/"runtime";runtime.mkdir()
    write_rfi_history(runtime,"s",[{"utc":"2026-09-02T00:00:05+00:00","occupancy_fraction":0.12,
        "clipping_fraction":0.0,"peak_dbfs":-58.0}])
    out=tmp_path/"out"
    live=ql.QuicklookLive(session,PROFILE,out,runtime_dir=runtime)
    assert live.session_id=="s"
    live.run(True)
    doc=json.loads((out/"rfi_occupancy_map.json").read_text())
    assert doc["title"]=="ANTENNA B - RFI OCCUPANCY MAP"
    assert doc["grid"]["values"][0][0]==pytest.approx(0.12)
    assert doc["grid"]["values"][0][1] is None
    assert (out/"rfi_occupancy_map.png").exists()


def test_rfi_occupancy_map_absent_without_runtime_dir_never_fatal(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();make_grid(session,1,1)
    mark_success(session,1,"2026-09-02T00:00:00+00:00","2026-09-02T00:00:10+00:00")
    source=session/"one.h5";capture(source);manifest(session,[row("1","SUCCESS","one.h5")])
    out=tmp_path/"out"
    status=ql.QuicklookLive(session,PROFILE,out).run(True)  # no runtime_dir at all
    assert status["points_processed"]==1
    assert not (out/"rfi_occupancy_map.json").exists()
    assert (out/"quicklook_map.json").exists()  # Antenna A's own map is unaffected


def test_rfi_occupancy_map_session_mismatch_produces_nothing(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();make_grid(session,1,1)
    mark_success(session,1,"2026-09-02T00:00:00+00:00","2026-09-02T00:00:10+00:00")
    source=session/"one.h5";capture(source);manifest(session,[row("1","SUCCESS","one.h5")])
    runtime=tmp_path/"runtime";runtime.mkdir()
    write_rfi_history(runtime,"different-session",[{"utc":"2026-09-02T00:00:05+00:00",
        "occupancy_fraction":0.5,"clipping_fraction":0.0,"peak_dbfs":-40.0}])
    out=tmp_path/"out"
    ql.QuicklookLive(session,PROFILE,out,runtime_dir=runtime).run(True)
    assert not (out/"rfi_occupancy_map.json").exists()


def test_rfi_occupancy_map_missing_history_file_produces_nothing(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();make_grid(session,1,1)
    mark_success(session,1,"2026-09-02T00:00:00+00:00","2026-09-02T00:00:10+00:00")
    source=session/"one.h5";capture(source);manifest(session,[row("1","SUCCESS","one.h5")])
    runtime=tmp_path/"runtime";runtime.mkdir()  # no rfi_ref_history.json written at all
    out=tmp_path/"out"
    status=ql.QuicklookLive(session,PROFILE,out,runtime_dir=runtime).run(True)
    assert status["points_processed"]==1  # science quicklook unaffected
    assert not (out/"rfi_occupancy_map.json").exists()


# ---------------------------------------------------------------- ANTENNA A interpolated preview (optional)


def test_interpolated_preview_available_alongside_unmodified_native_grid(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();make_grid(session,1,3)
    for pid in (1,2,3):
        capture(session/f"p{pid}.h5")
    manifest(session,[row(str(pid),"SUCCESS",f"p{pid}.h5",ra=str(10.0+pid),dec="-30") for pid in (1,2,3)])
    out=tmp_path/"out"
    ql.QuicklookLive(session,PROFILE,out).run(True)
    native=json.loads((out/"quicklook_map.json").read_text())
    assert native["map_mode"]=="NATIVE_GRID"  # untouched by the preview feature
    preview=json.loads((out/"quicklook_map_interpolated.json").read_text())
    assert preview["available"] is True
    assert preview["interpolated"] is True
    assert preview["n_points_used"]==3
    assert (out/"quicklook_map_interpolated.png").exists()


def test_interpolated_preview_unavailable_with_few_points_native_grid_still_updates(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();make_grid(session,1,1)
    source=session/"one.h5";capture(source);manifest(session,[row("1","SUCCESS","one.h5")])
    out=tmp_path/"out"
    ql.QuicklookLive(session,PROFILE,out).run(True)
    assert (out/"quicklook_map.json").exists()  # native grid: 1 point is enough to update
    preview=json.loads((out/"quicklook_map_interpolated.json").read_text())
    assert preview["available"] is False  # fewer than 3 points: interpolation undefined
    assert not (out/"quicklook_map_interpolated.png").exists()


def test_interpolated_preview_failure_never_breaks_native_grid(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();make_grid(session,1,3)
    for pid in (1,2,3):
        capture(session/f"p{pid}.h5")
    manifest(session,[row(str(pid),"SUCCESS",f"p{pid}.h5",ra=str(10.0+pid),dec="-30") for pid in (1,2,3)])
    out=tmp_path/"out"

    def boom(*a,**k):
        raise RuntimeError("simulated interpolation failure")
    monkeypatch.setattr(ql,"build_interpolated_preview_document",boom)
    status=ql.QuicklookLive(session,PROFILE,out).run(True)
    assert status["points_processed"]==3
    native=json.loads((out/"quicklook_map.json").read_text())
    assert native["map_mode"]=="NATIVE_GRID"
    assert native["quicklook_metrics"]["observed_cells"]==3
    assert not (out/"quicklook_map_interpolated.json").exists()
