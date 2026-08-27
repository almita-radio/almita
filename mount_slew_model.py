#!/usr/bin/env python3
"""Hardware-independent MODEL-04 inference for ALMITA slew duration."""
from __future__ import annotations
import json, math
from pathlib import Path
from typing import Any, Mapping
from astropy_offline import configure_astropy_offline
configure_astropy_offline()
import astropy.units as u
from astropy.coordinates import EarthLocation
from astropy.time import Time

DEFAULT_MODEL_PATH=Path(__file__).with_name("mount_slew_model.json")

def _number(name,value):
    if isinstance(value,bool): raise ValueError(f"{name} must be a finite number")
    try: result=float(value)
    except (TypeError,ValueError) as exc: raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result): raise ValueError(f"{name} must be a finite number")
    return result

def wrapped_delta_ra_hours(start,target): return (target-start+12)%24-12

def angular_distance_deg(start_ra_hours,start_dec_deg,target_ra_hours,target_dec_deg):
    ra1=math.radians(_number("start_ra_hours",start_ra_hours)*15);ra2=math.radians(_number("target_ra_hours",target_ra_hours)*15)
    dec1=_number("start_dec_deg",start_dec_deg);dec2=_number("target_dec_deg",target_dec_deg)
    if not -90<=dec1<=90 or not -90<=dec2<=90: raise ValueError("declination must be within [-90, 90] degrees")
    dec1,dec2=math.radians(dec1),math.radians(dec2);cosd=math.sin(dec1)*math.sin(dec2)+math.cos(dec1)*math.cos(dec2)*math.cos(ra2-ra1)
    return math.degrees(math.acos(max(-1,min(1,cosd))))

def hour_angles(start_ra_hours,target_ra_hours,obstime,location):
    when=obstime if isinstance(obstime,Time) else Time(obstime)
    if isinstance(location,EarthLocation): loc=location
    elif isinstance(location,Mapping): loc=EarthLocation(lat=_number("latitude_deg",location["latitude_deg"])*u.deg,lon=_number("longitude_deg",location["longitude_deg"])*u.deg,height=_number("elevation_m",location.get("elevation_m",0))*u.m)
    else: raise ValueError("location must be EarthLocation or observer mapping")
    lst=float(when.sidereal_time("apparent",longitude=loc.lon).hour)
    return wrapped_delta_ra_hours(_number("start_ra_hours",start_ra_hours),lst),wrapped_delta_ra_hours(_number("target_ra_hours",target_ra_hours),lst)

class SlewModel:
    def __init__(self,parameters:Mapping[str,Any]):
        required={"model_version","normal_model","ha_zero_crossing_model","safety_model"};missing=required.difference(parameters)
        if missing: raise ValueError(f"model artifact missing fields: {sorted(missing)}")
        self.parameters=dict(parameters);normal=parameters["normal_model"];cross=parameters["ha_zero_crossing_model"];safe=parameters["safety_model"]
        self.normal_intercept=_number("normal intercept",normal["intercept"]);self.normal_axis_coefficient=_number("axis coefficient",normal["axis_max_coefficient"])
        self.cross_intercept=_number("crossing intercept",cross["intercept"]);self.cross_distance_coefficient=_number("crossing distance coefficient",cross.get("distance_coefficient",0))
        self.normal_margin=_number("normal safety margin",safe["normal_residual_margin_sec"]);self.cross_margin=_number("crossing safety margin",safe["ha_zero_crossing_residual_margin_sec"])
        domain=parameters["training_domain"]
        self.axis_range=tuple(map(float,domain["axis_max_deg_range"]));self.distance_range=tuple(map(float,domain["angular_distance_deg_range"]));self.ha_range=tuple(map(float,domain["ha_hours_range"]));self.cross_safe=_number("crossing safe",cross["safe_sec"])
    @classmethod
    def load(cls,path=DEFAULT_MODEL_PATH):
        with Path(path).open(encoding="utf-8") as h:return cls(json.load(h))
    def predict_features(self,distance_deg,axis_max_deg,ha_zero_crossing,*,start_ha_hours=None,target_ha_hours=None):
        distance=_number("distance_deg",distance_deg);axis=_number("axis_max_deg",axis_max_deg)
        if not 0<=distance<=180 or axis<0: raise ValueError("distance must be within [0,180] and axis_max non-negative")
        crossing=bool(ha_zero_crossing)
        central=self.cross_intercept+self.cross_distance_coefficient*distance if crossing else self.normal_intercept+self.normal_axis_coefficient*axis
        safe=self.cross_safe if crossing else central+self.normal_margin
        outside=not self.distance_range[0]<=distance<=self.distance_range[1] or not self.axis_range[0]<=axis<=self.axis_range[1]
        for value in (start_ha_hours,target_ha_hours):
            if value is not None and not self.ha_range[0]<=float(value)<=self.ha_range[1]: outside=True
        return {"predicted_slew_sec":max(.001,central),"predicted_slew_safe_sec":max(max(.001,central),safe),"axis_max_deg":axis,"ha_zero_crossing":crossing,"model_regime":"ha_zero_crossing" if crossing else "normal","extrapolation_warning":outside}
    def predict(self,start_ra_hours,start_dec_deg,target_ra_hours,target_dec_deg,obstime,location):
        values=[_number("start_ra_hours",start_ra_hours),_number("start_dec_deg",start_dec_deg),_number("target_ra_hours",target_ra_hours),_number("target_dec_deg",target_dec_deg)]
        distance=angular_distance_deg(*values);dra=abs(wrapped_delta_ra_hours(values[0],values[2])*15);ddec=abs(values[3]-values[1]);start_ha,target_ha=hour_angles(values[0],values[2],obstime,location)
        result=self.predict_features(distance,max(dra,ddec),start_ha*target_ha<0,start_ha_hours=start_ha,target_ha_hours=target_ha);result.update({"angular_distance_deg":distance,"start_ha_hours":start_ha,"target_ha_hours":target_ha});return result

def predict_slew(start_ra,start_dec,target_ra,target_dec,obstime,location,model_path=DEFAULT_MODEL_PATH):
    return SlewModel.load(model_path).predict(start_ra,start_dec,target_ra,target_dec,obstime,location)
