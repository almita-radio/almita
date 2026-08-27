import csv
import json
from pathlib import Path

import h5py
import numpy as np

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


def fake_products(monkeypatch):
    def spectrum(source,profile,out):
        out=Path(out);out.mkdir(parents=True,exist_ok=True)
        if "broken" in str(source): raise RuntimeError("synthetic processor failure")
        (out/"quicklook_spectrum.json").write_text('{"calibration_level":"RELATIVE_INSTRUMENTAL","absolute_calibration":false}')
        (out/"quicklook_spectrum.png").write_bytes(b"png")
        (out/"quicklook_fractional_excess.png").write_bytes(b"png")
        f=np.linspace(1419e6,1422e6,16);v=np.linspace(0,.1,16);u=np.full(16,.01);m=np.ones(16,bool)
        return {},{"frequency_hz":f,"fractional_excess":v,"fractional_uncertainty":u,"valid_mask":m}
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
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();source=session/"one.h5";capture(source)
    before=source.read_bytes();manifest(session,[row("1","SUCCESS","one.h5")])
    out=tmp_path/"out";first=ql.QuicklookLive(session,PROFILE,out).run(True)
    assert first["points_processed"]==1 and (out/"latest_spectrum.json").exists()
    assert (out/"latest_waterfall.png").exists() and (out/"quicklook_map.json").exists()
    assert json.loads((out/"quicklook_map.json").read_text())["status"]=="POINT_ONLY"
    second=ql.QuicklookLive(session,PROFILE,out).run(True)
    assert second["points_processed"]==1 and second["performance"]["points"]==[]
    assert source.read_bytes()==before


def test_one_two_three_point_map_modes(tmp_path,monkeypatch):
    fake_products(monkeypatch);session=tmp_path/"s";session.mkdir();capture(session/"x.h5")
    out=tmp_path/"out";rows=[]
    for pid,ra,dec,mode in [("1",10,20,"POINT_ONLY"),("2",11,20,"LINE_ONLY"),("3",10,21,"INTERPOLATED")]:
        rows.append(row(pid,"SUCCESS","x.h5",str(ra),str(dec)));manifest(session,rows)
        ql.QuicklookLive(session,PROFILE,out).run(True)
        assert json.loads((out/"quicklook_map.json").read_text())["status"]==mode


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
