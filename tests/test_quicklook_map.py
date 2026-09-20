import json
from pathlib import Path

import h5py
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
import astropy.units as u

from calibration_foundation import load_calibration_profile
from quicklook_map import (MapError, MapPoint, derive_point, generate, interpolate_visual,
                           map_metric, project_offsets, robust_spherical_center, validation_points)

PROFILE=Path("data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1.npz")


def test_map_metric_masks_and_uncertainty():
    f=np.arange(10.); v=np.arange(10.)/10; e=np.full(10,.02); mask=np.ones(10,bool); mask[5]=False
    result=map_metric(v,e,mask,f,2,7)
    assert result["map_value"] == pytest.approx(np.median([.2,.3,.4,.6,.7]))
    assert result["valid_fraction"] == pytest.approx(5/6)
    assert result["map_uncertainty"] > 0


def test_ra_wrap_and_east_sign():
    center=SkyCoord(359.9*u.deg, 10*u.deg)
    x,y=project_offsets([.1,359.7],[10,10],center)
    assert 0 < x[0] < 1 and -1 < x[1] < 0
    assert np.max(np.abs(y)) < .001


def test_high_dec_projection_and_north_sign():
    center=SkyCoord(120*u.deg, 85*u.deg)
    x,y=project_offsets([121,120],[85,85.2],center)
    assert 0 < x[0] < .2
    assert y[1] > 0


def test_robust_center_wrap():
    center=robust_spherical_center([359,0,1], [20,20,20])
    assert min(center.ra.deg,360-center.ra.deg) < .1


def test_regular_grid_feature_missing_point_and_no_extrapolation():
    points=validation_points(); points.pop(0)
    center=robust_spherical_center([p.ra_deg for p in points],[p.dec_deg for p in points])
    x,y=project_offsets([p.ra_deg for p in points],[p.dec_deg for p in points],center)
    for p,px,py in zip(points,x,y): p.x_offset_deg=px;p.y_offset_deg=py
    grid=interpolate_visual(points,64)
    assert grid["value"].shape == (64,64)
    assert np.isnan(grid["value"][0,0])  # missing corner outside convex hull
    assert grid["coverage_mask"].sum() < grid["coverage_mask"].size
    assert len(points)==8


def test_edge_maximum_preserved():
    points=[]
    for i,(x,y) in enumerate([(0,0),(1,0),(0,1),(1,1)]):
        points.append(MapPoint(str(i),"","VALIDATION_DATASET","SYNTHETIC_TEST",0,0,
                               x,y,10 if (x,y)==(1,1) else 0,.1,1,0))
    grid=interpolate_visual(points,32)
    assert np.nanmax(grid["value"]) == pytest.approx(10)


def test_part_and_unknown_compatibility(tmp_path):
    profile=load_calibration_profile(PROFILE)
    with pytest.raises(MapError): derive_point("x",tmp_path/"x.h5.part",0,0,"COMMANDED",profile,1,2)
    h=tmp_path/"x.h5"
    with h5py.File(h,"w") as f:
        f.create_dataset("iq_data",data=np.zeros(16,dtype=np.uint8))
        f.attrs["center_frequency_hz"]=1420405752; f.attrs["sample_rate_hz"]=2400000; f.attrs["gain"]=40.2
    point=derive_point("x",h,0,0,"COMMANDED",profile,1419e6,1421e6)
    assert point.status == "UNKNOWN" and point.coordinate_source == "COMMANDED"


def test_invalid_hdf5(tmp_path):
    profile=load_calibration_profile(PROFILE); bad=tmp_path/"bad.h5"; bad.write_text("bad")
    point=derive_point("x",bad,0,0,"ACTUAL",profile,1419e6,1421e6)
    assert point.status == "INCOMPATIBLE"


def test_generate_schema_png_null_grid_deterministic_geometry(tmp_path):
    before=PROFILE.read_bytes()
    result=generate(tmp_path,PROFILE,grid_size=40)
    doc=json.loads((tmp_path/"quicklook_map.json").read_text())
    assert doc["status"]=="SUCCESS" and doc["dataset_classification"]=="VALIDATION_DATASET"
    assert doc["absolute_calibration"] is False and "Kelvin" not in json.dumps(doc)
    assert len(doc["points"])==9 and len(doc["grid"]["values"])==40
    assert any(v is None for row in doc["grid"]["values"] for v in row)
    assert (tmp_path/"quicklook_map.png").stat().st_size>1000
    assert (tmp_path/"quicklook_map_points.csv").exists() and (tmp_path/"quicklook_map_grid.npz").exists()
    assert PROFILE.read_bytes()==before
    second=validation_points()
    assert [p.map_value for p in second]==[p.map_value for p in validation_points()]
    assert result["validation"]["profile_integrity"]["unchanged"]


def test_three_collinear_rejected():
    points=[MapPoint(str(i),"","VALIDATION_DATASET","SYNTHETIC_TEST",0,0,i,0,i,.1,1,0) for i in range(3)]
    with pytest.raises(MapError): interpolate_visual(points)
