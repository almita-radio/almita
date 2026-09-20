import json, pytest
from astropy.coordinates import EarthLocation
from astropy.time import Time
import astropy.units as u
from mount_slew_model import SlewModel,angular_distance_deg,predict_slew

LOCATION=EarthLocation(lat=-33.4330556*u.deg,lon=-70.6663889*u.deg,height=550*u.m)
TIME=Time("2026-08-20T15:30:00")

def test_loads_model04():
    m=SlewModel.load();assert m.parameters["sample_count"]==132;assert m.parameters["model_version"]=="almita-slew-4.0"

def test_ra_wrap_uses_short_separation(): assert angular_distance_deg(23.9,0,.1,0)==pytest.approx(3)

@pytest.mark.parametrize("args",[(float("nan"),0,1,0),(0,91,1,0),(0,0,1,-91)])
def test_invalid_inputs_raise(args):
    with pytest.raises(ValueError):angular_distance_deg(*args)

def test_predict_returns_regime_and_safe_values():
    r=predict_slew(9,-70,10,-70,TIME,LOCATION);assert r["model_regime"] in {"normal","ha_zero_crossing"};assert r["predicted_slew_safe_sec"]>=r["predicted_slew_sec"]

def test_crossing_regime_is_detected_geometrically():
    lst=float(TIME.sidereal_time("apparent",longitude=LOCATION.lon).hour)
    r=predict_slew((lst+.5)%24,-60,(lst-.5)%24,-60,TIME,LOCATION);assert r["ha_zero_crossing"] is True;assert r["model_regime"]=="ha_zero_crossing"

def test_no_crossing_is_detected_geometrically():
    lst=float(TIME.sidereal_time("apparent",longitude=LOCATION.lon).hour)
    r=predict_slew((lst+2)%24,-60,(lst+1)%24,-61,TIME,LOCATION);assert r["ha_zero_crossing"] is False;assert r["model_regime"]=="normal"

def test_axis_max_uses_wrapped_ra_and_dec():
    lst=float(TIME.sidereal_time("apparent",longitude=LOCATION.lon).hour)
    r=predict_slew((lst+2)%24,-60,(lst+2.2)%24,-65,TIME,LOCATION);assert r["axis_max_deg"]==pytest.approx(5.0)

def test_prediction_is_deterministic():
    args=(9,-60,10,-61,TIME,LOCATION);assert predict_slew(*args)==predict_slew(*args)

def test_normal_regime_has_lower_short_safe_prediction():
    m=SlewModel.load();normal=m.predict_features(3,5,False);cross=m.predict_features(3,5,True);assert normal["predicted_slew_safe_sec"]<cross["predicted_slew_safe_sec"]

def test_mapping_location_supported():
    r=predict_slew(9,-60,9.2,-61,TIME,{"latitude_deg":-33.4330556,"longitude_deg":-70.6663889,"elevation_m":550});assert r["predicted_slew_sec"]>0

def test_extrapolation_warning_for_outside_training_domain():
    r=SlewModel.load().predict_features(150,180,False);assert r["extrapolation_warning"] is True

def test_in_domain_prediction_has_no_extrapolation_warning():
    r=SlewModel.load().predict_features(20,25,False);assert r["extrapolation_warning"] is False

def test_rejects_invalid_artifact(tmp_path):
    p=tmp_path/"bad.json";p.write_text(json.dumps({"model_version":"bad"}))
    with pytest.raises(ValueError):SlewModel.load(p)

def test_model_module_has_no_hardware_dependency():
    source=open("mount_slew_model.py",encoding="utf-8").read().lower();assert "indi" not in source;assert "socket" not in source;assert "asyncio" not in source
