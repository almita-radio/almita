import numpy as np
import pytest

from hi_spectral_metric import (
    HI_REST_HZ, dc_mask, detect_fixed_spurs, expected_gaussian_integral,
    fit_polynomial_baseline, frequency_to_radio_velocity,
    inject_gaussian_psd, measure_dc_mask_half_width, metric_v2_from_psd,
    radio_velocity_to_frequency,
)


def grid(n=8192, rate=2_400_000):
    return HI_REST_HZ + np.fft.fftshift(np.fft.fftfreq(n, 1/rate))


def baseline(f):
    x=(f-HI_REST_HZ)/1.2e6
    return 10 + .8*x + .4*x*x


def test_velocity_roundtrip_and_sign():
    v=np.array([-200.,0.,175.]); assert np.allclose(frequency_to_radio_velocity(radio_velocity_to_frequency(v)),v)
    assert radio_velocity_to_frequency(100)<HI_REST_HZ


def test_dc_mask_width_and_edge():
    f=grid();m=dc_mask(f,HI_REST_HZ,5000);assert m.any();assert np.max(np.abs(f[m]-HI_REST_HZ))<=5000
    assert not m[0]


def test_measured_dc_mask_is_evidence_based_and_guarded():
    f=grid();p=np.ones(len(f));k=np.argmin(abs(f-HI_REST_HZ));p[k-1:k+2]=100
    info=measure_dc_mask_half_width(f,p,HI_REST_HZ)
    assert info["affected_bins_above_threshold"]==3
    assert info["half_width_bins"]==pytest.approx(5)


def test_robust_baseline_rejects_spike():
    f=grid();b=baseline(f);p=b.copy();p[1000]=1000
    model,_=fit_polynomial_baseline(f,p,np.ones(len(f),bool),degree=2)
    assert np.max(np.abs(model-b))<1e-6


def test_signed_no_signal_has_no_positive_clip_bias():
    f=grid();b=baseline(f);noise=.01*np.sin(np.arange(len(f)))
    r,_,_=metric_v2_from_psd(f,b*(1+noise),HI_REST_HZ)
    assert abs(r.metric_value)<.2


def test_negative_signal_stays_negative():
    f=grid();b=baseline(f);v=frequency_to_radio_velocity(f);p=b*(1-.05*np.exp(-.5*(v/30)**2))
    r,_,_=metric_v2_from_psd(f,p,HI_REST_HZ)
    assert r.metric_value<0


def test_strong_gaussian_recovery():
    f=grid();b=baseline(f);p=inject_gaussian_psd(b,b,f,80,25,.2)
    r,_,_=metric_v2_from_psd(f,p,HI_REST_HZ,velocity_window_km_s=150)
    assert r.metric_value==pytest.approx(expected_gaussian_integral(.2,25),rel=.05)


def test_dc_does_not_change_metric():
    f=grid();b=baseline(f);p=b.copy();p[np.argmin(abs(f-HI_REST_HZ))]=1e8
    r,_,_=metric_v2_from_psd(f,p,HI_REST_HZ,dc_mask_half_width_hz=5000)
    assert abs(r.metric_value)<1e-6


def test_spur_mask_removes_spur():
    f=grid();b=baseline(f);p=b.copy();k=np.argmin(abs(f-(HI_REST_HZ+100_000)));p[k]=1000
    lo,hi=f[k]-1000,f[k]+1000
    r,_,_=metric_v2_from_psd(f,p,HI_REST_HZ,spur_ranges_hz=[(lo,hi)])
    assert abs(r.metric_value)<1e-6 and r.spur_masked_fraction>0


def test_fixed_spur_detection_persistence():
    f=grid();b=baseline(f);s=np.tile(b,(20,1));k=1500;s[:,k]*=4
    mask,items=detect_fixed_spurs(f,s,z_threshold=6,persistence_threshold=.7)
    assert mask[k] and items and items[0]['persistence']>=.7


def test_nonpersistent_feature_not_fixed_spur():
    f=grid();b=baseline(f);s=np.tile(b,(20,1));s[0,1500]*=10
    mask,_=detect_fixed_spurs(f,s,z_threshold=6,persistence_threshold=.7)
    assert not mask[1500]


def test_masked_fraction_and_valid_fraction_sum():
    f=grid();b=baseline(f);r,_,_=metric_v2_from_psd(f,b,HI_REST_HZ,dc_mask_half_width_hz=5000)
    assert r.valid_fraction+r.masked_fraction==pytest.approx(1)


def test_window_selection_changes_coverage():
    f=grid();b=baseline(f);p=inject_gaussian_psd(b,b,f,120,20,.2)
    small,_,_=metric_v2_from_psd(f,p,HI_REST_HZ,velocity_window_km_s=50)
    wide,_,_=metric_v2_from_psd(f,p,HI_REST_HZ,velocity_window_km_s=150)
    assert wide.metric_value>small.metric_value


def test_uncertainty_positive():
    f=grid();b=baseline(f);p=b*(1+.01*np.random.default_rng(1).normal(size=len(f)))
    r,_,_=metric_v2_from_psd(f,p,HI_REST_HZ)
    assert np.isfinite(r.metric_uncertainty) and r.metric_uncertainty>0


def test_reference_bandpass_removes_common_ripple_and_recovers_line():
    f=grid();ref=baseline(f)*(1+.2*np.sin((f-HI_REST_HZ)/180000))
    observed=1.07*ref
    injected=inject_gaussian_psd(observed,1.07*ref,f,70,25,.1)
    r,_,_=metric_v2_from_psd(f,injected,HI_REST_HZ,reference_bandpass=ref)
    assert r.metric_value==pytest.approx(expected_gaussian_integral(.1,25),rel=.06)


def test_edge_of_band_window_rejected():
    f=HI_REST_HZ+1_050_000+np.arange(128)*100
    with pytest.raises(ValueError): metric_v2_from_psd(f,np.ones(128),HI_REST_HZ)
