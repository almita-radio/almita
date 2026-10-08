import numpy as np

from sun_detectability_test import summarize_gain


def records(sun, off, clipping=0.0):
    rows=[]
    for index,(name,value) in enumerate(zip(("SUN","OFF","SUN","OFF","SUN"),
                                            (sun[0],off[0],sun[1],off[1],sun[2])),1):
        rows.append({"position":name,"status":"VALID","broadband_power":value,
                     "clipping_fraction":clipping,"p001":20,"p999":235,
                     "subband_power":[value]*(16)})
    return rows


def test_detectability_passes_repeatable_broadband_difference():
    result=summarize_gain(records([110,111,109],[100,101]))
    assert result["status"]=="PASS" and result["snr"]>=5


def test_detectability_rejects_clipping_and_narrow_effect():
    clipped=summarize_gain(records([110,111,109],[100,101],.01))
    assert clipped["status"]=="FAIL"
    rows=records([110,111,109],[100,101])
    for row in rows:
        if row["position"]=="SUN": row["subband_power"]=[110]+[100]*15
        else: row["subband_power"]=[100]*16
    assert summarize_gain(rows)["status"]=="FAIL"
