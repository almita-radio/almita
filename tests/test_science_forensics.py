"""SCIENCE FEATURE FORENSICS V1: synthetic acceptance (sky-fixed, receiver-fixed, time-drifting, spatial gradient, serpentine
confounding), measurement accuracy, numeric robustness, masks, negative-map audit, session integrity/replay, and the
Level1-only / no-RAW / no-network boundary. Real-data tests skip when the local REDUCE sessions are absent."""
import ast
import csv
import io
import json
import os
import re
import shutil
import socket
import sys
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pytest

import almita_science_forensics as cli
from science_forensics.measure import gaussian_fit, measure_point, scan_peaks
from science_forensics.models import ForensicsConfig, FeatureCandidate, canonical_json
from science_forensics.negative import (candidate_lsrk_range, cumulative_integral, empirical_noise, integrate_window,
                                        median_spectrum, negative_map_audit, select_control_windows)
from science_forensics.session import (compare_forensics_sessions, plan_forensics, run_forensics, validate_forensics_session)
from science_forensics.statistics import LABELS, analyze, ols
from science_forensics.synthetic import DF_HZ, DV_M_S, build_campaign, serpentine_positions, write_reduce_session_fixture

ROOT = Path(__file__).resolve().parent.parent
REAL_9 = ROOT / "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411"
CFG = ForensicsConfig(window_frame="channel", window_center=512, window_half_width=150)


def run_analysis(inp, cfg=CFG):
    ms = [measure_point(p, cfg) for p in inp.points]
    return ms, analyze(inp, ms, cfg)


def frames(a):
    return a["frame_coherence"]["frames"]


# ------------------------------------------------------------------ configuration validation

@pytest.mark.parametrize("kw", [dict(window_half_width=0.0), dict(window_half_width=-1.0), dict(window_half_width=float("nan")),
                                dict(window_center=float("inf")), dict(window_frame="wavelength"), dict(sign="both"),
                                dict(centroid_method="magic"), dict(min_usable_bins=3), dict(min_usable_points=2),
                                dict(sort_modes=("capture", "alphabetical")), dict(n_velocity_intervals=1),
                                dict(integration_window_min_m_s=5.0, integration_window_max_m_s=1.0),
                                dict(site_latitude_deg=10.0)])
def test_invalid_configuration_is_rejected(kw):
    base = dict(window_center=150000.0, window_half_width=5000.0)
    with pytest.raises(ValueError):
        ForensicsConfig(**{**base, **kw})


def test_config_hash_is_canonical():
    a = ForensicsConfig(window_center=1.0, window_half_width=2.0)
    b = ForensicsConfig(**dict(reversed(list(a.to_dict().items()))) | {"sort_modes": a.sort_modes})
    assert a.config_hash() == b.config_hash()
    assert a.config_hash() != ForensicsConfig(window_center=1.5, window_half_width=2.0).config_hash()


def test_candidate_status_vocabulary_is_enforced():
    c = FeatureCandidate("X", None, None, None, {}, "s", "m", status="UNCLASSIFIED")
    c.to_dict()
    c.status = "IS_RFI"
    with pytest.raises(ValueError):
        c.to_dict()


# ------------------------------------------------------------------ measurement accuracy

def test_gaussian_fit_recovers_centroid_with_calibrated_formal_error():
    """Monte Carlo: centroid z-scores against the FORMAL sigma must be ~N(0,1) when the noise model is right."""
    rng = np.random.default_rng(1)
    x = np.arange(60.0)
    z, hs = [], []
    for _ in range(300):
        y = 0.8 * np.exp(-0.5 * ((x - 30.37) / 4.0) ** 2) + 0.01 + rng.normal(0, 0.05, x.size)
        p, cov, chi, it, ok = gaussian_fit(x, y, np.full(x.size, 0.05), 0.8, 29.0, 3.0, 0.0)
        assert ok
        z.append((p[1] - 30.37) / np.sqrt(cov[1, 1]))
        hs.append(p[0])
    z = np.array(z)
    assert abs(z.mean()) < 0.2 and 0.85 < z.std() < 1.15
    assert abs(np.mean(hs) - 0.8) < 0.01


@pytest.mark.parametrize("sign,amp", [(+1, 1.0), (-1, -1.0)])
def test_signed_feature_is_measured_with_its_sign(sign, amp):
    inp, _ = build_campaign("receiver_fixed", seed=4, n_points=6, amplitude=amp)
    cfg = ForensicsConfig(window_frame="channel", window_center=512, window_half_width=100, sign="auto")
    for p in inp.points:
        m = measure_point(p, cfg)
        assert m.status == "OK" and m.sign == sign and np.sign(m.peak_height) == sign and np.sign(m.area_relative_m_s) == sign
        assert abs(abs(m.peak_height) - 1.0) < 0.15 and abs(m.fwhm_channels - 12.0) < 2.0
        assert abs(m.centroid_channel - 512.0) < 0.5


def test_three_methods_agree_on_a_clean_feature_and_report_all():
    inp, _ = build_campaign("receiver_fixed", seed=5, n_points=4, noise_sigma=0.01)
    m = measure_point(inp.points[0], CFG)
    assert abs(m.centroid_channel_gaussian - m.centroid_channel_halfmax) < 0.3
    assert abs(m.centroid_channel_gaussian - m.centroid_channel_peak) < 0.6
    assert m.method_used == "gaussian" and m.fit_converged and 0.5 < m.fit_redchi2 < 2.0


def test_the_spectrum_is_never_rebaselined_by_the_measurement():
    inp, _ = build_campaign("receiver_fixed", seed=6, n_points=4)
    before = [p.value.copy() for p in inp.points]
    for p in inp.points:
        measure_point(p, CFG)
    assert all(np.array_equal(b, p.value, equal_nan=True) for b, p in zip(before, inp.points))


# ------------------------------------------------------------------ synthetic acceptance: the five scientific cases

def test_random_position_reference_and_detection_fraction_are_reported():
    inp, _ = build_campaign("receiver_fixed", seed=60, n_points=10)
    ms, a = run_analysis(inp)
    ref = a["frame_coherence"]["uniform_random_position_rms_channels"]
    assert ref == pytest.approx(301 / np.sqrt(12), rel=0.02)
    assert a["detection"]["n_detected"] == 10 and a["detection"]["fraction_detected"] == 1.0
    assert not any("random positions" in e["statement"] and e["label"] == "UNRESOLVED" for e in a["evidence"])   # stationary: far below random


def test_case_A_sky_fixed_line_is_stationary_in_lsrk_and_scattered_in_frequency():
    inp, truth = build_campaign("sky_fixed", seed=11)
    ms, a = run_analysis(inp)
    fc = frames(a)
    assert fc["lsrk_velocity_m_s"]["rms_channels"] < 0.3
    assert fc["topocentric_frequency_hz"]["rms_channels"] > 3.0
    assert fc["topocentric_frequency_hz"]["rms_channels"] / fc["lsrk_velocity_m_s"]["rms_channels"] > 10
    # the frequency scatter must equal the imposed LSRK-shift scatter (in channel widths)
    shifts = np.array(truth.parameters["shifts_m_s"])
    assert fc["topocentric_frequency_hz"]["rms_channels"] == pytest.approx(shifts.std(ddof=1) / DV_M_S, rel=0.12)
    labels = {(e["label"], e["basis"]) for e in a["evidence"]}
    assert ("CONSISTENT WITH", "frame_coherence") in labels
    assert any("more stationary in LSRK velocity" in e["statement"] for e in a["evidence"])


def test_case_B_receiver_fixed_line_is_stationary_in_frequency_with_apparent_lsrk_drift():
    inp, truth = build_campaign("receiver_fixed", seed=12)
    ms, a = run_analysis(inp)
    fc = frames(a)
    assert fc["topocentric_frequency_hz"]["rms_channels"] < 0.3
    assert fc["lsrk_velocity_m_s"]["rms_channels"] > 3.0
    assert fc["lsrk_velocity_m_s"]["rms_channels"] / fc["topocentric_frequency_hz"]["rms_channels"] > 10
    assert any("more stationary in topocentric frequency" in e["statement"] for e in a["evidence"])
    assert abs(a["drift"]["topocentric_frequency_hz"]["slope_z"]) < 3            # no time drift


def test_case_C_drifting_oscillator_slope_is_recovered_in_all_three_units(capsys):
    inp, truth = build_campaign("time_drift", seed=13, n_points=24, drift_hz_per_hour=60_000.0, dt_s=45.0)
    ms, a = run_analysis(inp)
    d = a["drift"]["topocentric_frequency_hz"]
    assert abs(d["hz_per_hour"] - 60_000.0) < 3 * d["slope_se"] and abs(d["hz_per_hour"] / 60_000.0 - 1) < 0.02
    assert d["slope_z"] > 100 and d["r2"] > 0.999
    ch = a["drift"]["channel_bin"]
    assert ch["channels_per_hour"] == pytest.approx(60_000.0 / DF_HZ, rel=0.02)
    per_point = d["hz_per_point"]
    assert per_point == pytest.approx(60_000.0 * 45.0 / 3600.0, rel=0.02)
    with capsys.disabled():
        print(f"\nDRIFT: recovered {d['hz_per_hour']:.0f} +/- {d['slope_se']:.0f} Hz/h (truth 60000); "
              f"{ch['channels_per_hour']:.1f} ch/h; per point {per_point:.1f} Hz")
    assert any("linear drift in topocentric frequency" in e["statement"] for e in a["evidence"])


def test_case_D_spatial_velocity_gradient_is_not_mistaken_for_time_drift():
    inp, truth = build_campaign("sky_gradient", seed=14, n_points=30, dt_s=40.0, gradient_m_s_per_deg=2500.0, shift_mode="zero")
    ms, a = run_analysis(inp)
    assert not a["confounding"]["confounded_time_sky"]                            # random positions: time and sky are independent
    m = a["models"]["lsrk_velocity_m_s"]
    assert m["sky"]["r2"] > 0.95 and m["time"]["r2"] < 0.3
    assert abs(a["drift"]["lsrk_velocity_m_s"]["m_s_per_hour"] / a["drift"]["lsrk_velocity_m_s"]["slope_se"]) < 3.5
    assert m["time_plus_sky"]["aicc"] < m["time"]["aicc"] - 20                    # sky explains, time adds nothing
    assert a["models"]["lsrk_velocity_m_s"]["partial_r2"]["sky_given_time"] > 0.9
    assert a["models"]["lsrk_velocity_m_s"]["partial_r2"]["time_given_sky"] < 0.5
    stm = " ".join(e["statement"] for e in a["evidence"])
    assert "dependence of the LSRK centroid on sky position" in stm
    assert "dependence on capture time" not in stm


@pytest.mark.parametrize("kind,extra", [("time_drift", dict(drift_hz_per_hour=25_000.0)),
                                        ("sky_gradient", dict(gradient_m_s_per_deg=3000.0, gradient_axis="dec"))])
def test_case_E_serpentine_mosaic_confounds_time_and_sky_and_forensics_says_so(kind, extra):
    """Same mosaic, opposite truths (a pure time drift vs a pure Dec velocity gradient): a naive time correlation is high in BOTH."""
    inp, truth = build_campaign(kind, seed=15, positions=serpentine_positions(5, 6), dt_s=40.0, shift_mode="zero", **extra)
    ms, a = run_analysis(inp)
    conf = a["confounding"]
    assert conf["confounded_time_sky"] and conf["max_abs_corr_time_vs_sky"] > 0.85
    assert conf["correlation_matrix"]["time_h"]["point_index"] > 0.999
    assert conf["vif"]["time_h"] > 5 or conf["vif"]["dec_off_deg"] > 5
    f_time = a["models"]["topocentric_frequency_hz"]["time"]["r2"]
    f_sky = a["models"]["topocentric_frequency_hz"]["sky"]["r2"]
    l_time = a["models"]["lsrk_velocity_m_s"]["time"]["r2"]
    assert max(f_time, l_time) > 0.9                                              # the naive correlation looks decisive...
    assert f_sky > 0.85 or l_time > 0.9                                          # ...and so does the sky-only fit
    unresolved = [e for e in a["evidence"] if e["label"] == "UNRESOLVED"]
    assert unresolved and "confounded" in unresolved[0]["statement"]
    stm = " ".join(e["statement"] for e in a["evidence"] if e["label"] == "CONSISTENT WITH")
    assert "capture time" not in stm and "sky position" not in stm              # neither cause is attributed


def test_multivariate_models_match_an_independent_numpy_fit():
    rng = np.random.default_rng(2)
    n = 40
    t, ra, dec = rng.uniform(0, 1, n), rng.normal(size=n), rng.normal(size=n)
    y = 3 + 2 * t - 1.5 * ra + 0.5 * dec + rng.normal(0, 0.1, n)
    X = np.column_stack([np.ones(n), t, ra, dec])
    fit = ols(X, y)
    beta = np.linalg.solve(X.T @ X, X.T @ y)
    assert np.allclose(fit["coefficients"], beta)
    rss = float(np.sum((y - X @ beta) ** 2))
    assert fit["r2"] == pytest.approx(1 - rss / np.sum((y - y.mean()) ** 2))
    assert fit["aic"] == pytest.approx(n * np.log(rss / n) + 2 * 4)
    assert fit["residual_rms"] == pytest.approx(np.sqrt(rss / (n - 4)))


# ------------------------------------------------------------------ masks, NaN and adversarial inputs

def test_masked_bins_are_missing_not_zero_and_lower_the_quality():
    inp, _ = build_campaign("receiver_fixed", seed=21, n_points=6, masked_slice=slice(505, 520))
    m = measure_point(inp.points[0], CFG)
    assert m.status == "OK" and m.mask_fraction > 0.03 and m.mask_flag_fractions["RFI"] == pytest.approx(m.mask_fraction)
    assert m.n_usable_bins == m.n_window_bins - 15
    inp2, _ = build_campaign("receiver_fixed", seed=21, n_points=6, masked_slice=slice(300, 900))      # window fully masked
    m2 = measure_point(inp2.points[0], CFG)
    assert m2.status == "NO_USABLE_BINS" and not np.isfinite(m2.centroid_channel)
    ms, a = run_analysis(inp2)
    assert a["status"] == "INSUFFICIENT_EVIDENCE" and a["evidence"][0]["label"] == "UNRESOLVED"


def test_nan_and_inf_never_contribute_and_never_fake_success():
    inp, _ = build_campaign("receiver_fixed", seed=22, n_points=6)
    clean = measure_point(inp.points[1], CFG)
    p = inp.points[1]
    p.value[520:524] = np.nan
    p.value[500] = np.inf
    p.sigma[490] = np.nan
    p.sigma[491] = np.inf
    m = measure_point(p, CFG)
    assert m.status == "OK" and np.isfinite(m.centroid_channel) and abs(m.centroid_channel - clean.centroid_channel) < 0.5
    assert m.nonfinite_fraction > 0
    for q in inp.points:                                    # everything non-finite in the window
        q.value[:] = np.nan
    ms, a = run_analysis(inp)
    assert all(x.status == "NO_USABLE_BINS" for x in ms) and a["status"] == "INSUFFICIENT_EVIDENCE"


def test_single_usable_point_makes_no_campaign_claim():
    inp, _ = build_campaign("receiver_fixed", seed=23, n_points=6)
    for p in inp.points[1:]:
        p.mask[:] = 4
    ms, a = run_analysis(inp)
    assert sum(m.status == "OK" for m in ms) == 1 and a["status"] == "INSUFFICIENT_EVIDENCE" and "frame_coherence" not in a


def test_feature_outside_the_spectral_range_is_reported_not_measured():
    inp, _ = build_campaign("receiver_fixed", seed=24, n_points=5)
    cfg = ForensicsConfig(window_frame="lsrk_velocity", window_center=900_000.0, window_half_width=5_000.0)
    ms = [measure_point(p, cfg) for p in inp.points]
    assert all(m.status == "WINDOW_OUTSIDE_SPECTRUM" for m in ms)
    partial = ForensicsConfig(window_frame="channel", window_center=2, window_half_width=20)
    m = measure_point(inp.points[0], partial)
    assert "WINDOW_TRUNCATED" in m.flags or m.status != "OK"


def test_edge_feature_and_very_broad_feature_are_flagged():
    inp, _ = build_campaign("receiver_fixed", seed=25, n_points=5, feature_channel=512)
    edge = ForensicsConfig(window_frame="channel", window_center=512 + 60, window_half_width=60)      # feature on the window's edge
    m = measure_point(inp.points[0], edge)
    assert m.status != "OK" or "EDGE_OF_WINDOW" in m.flags or abs(m.centroid_channel - 512) < 1
    broad, _ = build_campaign("receiver_fixed", seed=26, n_points=5, fwhm_channels=180.0)
    mb = measure_point(broad.points[0], ForensicsConfig(window_frame="channel", window_center=512, window_half_width=100))
    assert mb.status != "OK" or "BROAD_VS_WINDOW" in mb.flags or "GAUSSIAN_FIT_REJECTED" in mb.flags or mb.method_used != "gaussian"


def test_no_feature_signal_is_not_a_detection():
    """A window with only noise must not produce a 'centroid scatter' story: points fall below the detection gate."""
    inp, _ = build_campaign("receiver_fixed", seed=27, n_points=12, amplitude=0.0)
    ms = [measure_point(p, CFG) for p in inp.points]
    assert all(m.status in ("BELOW_DETECTION_THRESHOLD", "NO_FEATURE_SIGNAL") for m in ms)
    a = analyze(inp, ms, CFG)
    assert a["status"] == "INSUFFICIENT_EVIDENCE" and a["evidence"][0]["label"] == "UNRESOLVED"
    assert "BELOW_DETECTION_THRESHOLD" in a["reason"] or "NO_FEATURE_SIGNAL" in a["reason"]
    assert "frame_coherence" not in a
    strong, _ = build_campaign("receiver_fixed", seed=27, n_points=12, amplitude=1.0)
    assert all(measure_point(p, CFG).status == "OK" for p in strong.points)
    with pytest.raises(ValueError):
        ForensicsConfig(window_center=1.0, window_half_width=1.0, min_snr_formal=float("nan"))
    gate = ForensicsConfig(window_frame="channel", window_center=512, window_half_width=150, min_snr_formal=1e6)
    assert all(measure_point(p, gate).status == "BELOW_DETECTION_THRESHOLD" for p in strong.points)


def test_evidence_language_is_restricted_and_never_names_the_nature():
    for kind in ("sky_fixed", "receiver_fixed", "time_drift", "sky_gradient"):
        inp, _ = build_campaign(kind, seed=30)
        ms, a = run_analysis(inp)
        for e in a["evidence"]:
            assert e["label"] in LABELS and e["basis"] and e["statement"]
            assert not re.search(r"\b(is|are) (RFI|Galactic HI|celestial|astronomical|interference)\b", e["statement"], re.I)
            assert not re.search(r"kelvin|brightness temperature|absolute calibrat", e["statement"], re.I)


# ------------------------------------------------------------------ scan helper and negative-map audit

def test_scan_peaks_finds_the_drifting_feature_per_point():
    inp, truth = build_campaign("time_drift", seed=31, n_points=8)
    rows = scan_peaks(inp.points, inp.points[0].velocity_lsrk_m_s.min() + 5000, inp.points[0].velocity_lsrk_m_s.max() - 5000)
    ch = np.array([r["channel"] for r in rows])
    assert np.all(np.abs(ch - np.array(truth.parameters["true_centre_channels"])) < 3)


def _flat_campaign(offset=-0.01, seed=40, n_points=12, per_point_offset_sd=0.0):
    inp, truth = build_campaign("receiver_fixed", seed=seed, n_points=n_points, amplitude=1.0, noise_sigma=0.03, shift_mode="zero")
    rng = np.random.default_rng(seed + 1)
    for p in inp.points:
        p.value += offset + rng.normal(0, per_point_offset_sd)
    return inp


def test_negative_audit_measures_a_known_baseline_offset_and_it_is_not_the_candidate():
    inp = _flat_campaign(offset=-0.01)
    cfg = ForensicsConfig(window_frame="channel", window_center=512, window_half_width=100,
                          integration_window_min_m_s=-45_000.0, integration_window_max_m_s=5_000.0)
    audit = negative_map_audit(inp, cfg)
    w = audit["windows"]
    width = w["integration_window"]["width_m_s"]
    # internal consistency, point by point: integral(window) = integral(candidate) + integral(window without candidate)
    excl_pp = audit["integration_window_excluding_candidate_relative_m_s"]
    per_point = audit["per_point"]
    total = np.array([r["integration_window_integral"] for r in per_point])
    cand = np.array([r["candidate_integral"] for r in per_point])
    assert excl_pp["median"] == pytest.approx(np.median(total - cand), rel=1e-9)
    assert w["control_left"]["mean_relative_intensity"]["median"] == pytest.approx(-0.01, abs=0.003)
    assert w["control_right"]["mean_relative_intensity"]["median"] == pytest.approx(-0.01, abs=0.003)
    excl = audit["integration_window_excluding_candidate_relative_m_s"]["median"]
    assert excl == pytest.approx(-0.01 * (width - w["candidate"]["width_m_s"]), rel=0.15)
    assert w["control_left"]["integral_relative_m_s"]["fraction_negative"] == 1.0
    ints = [r for r in audit["interval_decomposition"]["intervals"] if r["status"] == "OK"]
    assert len(audit["interval_decomposition"]["intervals"]) == 8 and all(r["fraction_points_negative"] > 0.5 for r in ints if r["lsrk_range_m_s"][1] < w["candidate"]["lsrk_range_m_s"][0] or r["lsrk_range_m_s"][0] > w["candidate"]["lsrk_range_m_s"][1])


def test_control_windows_never_overlap_the_candidate_and_are_chosen_for_validity_not_sign():
    inp = _flat_campaign(offset=+0.02)
    cfg = ForensicsConfig(window_frame="channel", window_center=512, window_half_width=100)
    ctl = select_control_windows(inp, cfg)
    a, b = ctl["candidate_lsrk_range_m_s"]
    for side, blk in ctl["windows"].items():
        assert blk["status"] == "OK" and blk["attempts"][-1]["accepted"]
        lo, hi = blk["lsrk_range_m_s"]
        assert hi <= a - ctl["guard_m_s"] + 1e-6 or lo >= b + ctl["guard_m_s"] - 1e-6
        assert hi - lo == pytest.approx(ctl["width_m_s"])
    # a masked control region is rejected and the window is moved outward (attempts are persisted)
    for p in inp.points:
        p.mask[150:400] = 4
        p.value[150:400] = np.nan
    ctl2 = select_control_windows(inp, cfg)
    # velocity DEcreases with channel, so masking channels 150-400 empties the RIGHT (higher-velocity) control window
    assert ctl2["windows"]["right"]["status"] == "UNAVAILABLE" and len(ctl2["windows"]["right"]["attempts"]) >= 2
    assert ctl2["windows"]["left"]["status"] == "OK"


def test_cumulative_integral_final_value_equals_the_direct_sum():
    inp = _flat_campaign(offset=-0.01, seed=41)
    vmin, vmax = -40_000.0, 0.0
    cum = cumulative_integral(inp, vmin, vmax)
    direct = [integrate_window(p, vmin, vmax)["integral"] for p in inp.points]
    assert cum["final_value_median"] == pytest.approx(np.median(direct), rel=0.03)
    assert np.all(np.diff(cum["grid_lsrk_m_s"]) > 0)


def test_empirical_noise_ratio_is_one_for_iid_noise_and_flags_point_to_point_offsets():
    iid = _flat_campaign(offset=0.0, seed=42, n_points=20)
    cfg = ForensicsConfig(window_frame="channel", window_center=512, window_half_width=100)
    r_iid = empirical_noise(iid, select_control_windows(iid, cfg))["combined_ratio_empirical_to_reported"]
    assert r_iid == pytest.approx(1.0, abs=0.08)
    off = _flat_campaign(offset=0.0, seed=42, n_points=20, per_point_offset_sd=0.06)
    r_off = empirical_noise(off, select_control_windows(off, cfg))["combined_ratio_empirical_to_reported"]
    assert r_off > 1.8                                       # systematics between points inflate the empirical scatter


def test_median_spectrum_is_a_diagnostic_on_the_common_axis():
    inp = _flat_campaign(offset=-0.02, seed=43)
    ms = median_spectrum(inp, ForensicsConfig(window_frame="channel", window_center=512, window_half_width=100,
                                              integration_window_min_m_s=-45_000.0, integration_window_max_m_s=5_000.0))
    assert ms["status"] == "OK" and ms["summary"]["median_of_median_spectrum_in_window"] == pytest.approx(-0.02, abs=0.01)
    inp.points[0].frequency_hz = inp.points[0].frequency_hz + 1.0
    assert median_spectrum(inp, CFG)["status"] == "FREQUENCY_AXES_DIFFER"


# ------------------------------------------------------------------ sessions, CLI, integrity, determinism

@pytest.fixture(scope="module")
def drift_fixture(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("forensics")
    inp, truth = build_campaign("time_drift", seed=50, n_points=10, drift_hz_per_hour=50_000.0, dt_s=45.0)
    reduce_dir = write_reduce_session_fixture(tmp / "REDUCE", inp)
    return tmp, reduce_dir, inp


def cfg_for(inp):
    return ForensicsConfig(window_frame="channel", window_center=512, window_half_width=100, sort_modes=("capture",))


def test_full_session_validates_and_replays_to_identical_numbers(drift_fixture):
    tmp, reduce_dir, inp = drift_fixture
    a = run_forensics(str(reduce_dir), cfg_for(inp), output_root=str(tmp / "out"))
    b = run_forensics(str(reduce_dir), cfg_for(inp), output_root=str(tmp / "out"))
    assert a["status"] == "COMPLETED" and validate_forensics_session(a["session_dir"]) == {"ok": True, "problems": [], "products_checked": pytest.approx(0) if False else validate_forensics_session(a["session_dir"])["products_checked"]}
    assert validate_forensics_session(b["session_dir"])["ok"]
    assert a["manifest"]["numeric_sha256"] == b["manifest"]["numeric_sha256"]
    cmp = compare_forensics_sessions(a["session_dir"], b["session_dir"])
    assert cmp["verdict"] == "EQUIVALENT" and cmp["same_input"] and cmp["same_config"]
    m = a["manifest"]
    for k in ("input_reduce_manifest_sha256", "config_hash", "candidate", "input_reduce_session_id", "campaign_id", "created_utc"):
        assert m[k]
    prov = json.loads((Path(a["session_dir"]) / "provenance.json").read_text())
    assert prov["raw_access"] == "none" and prov["network_access"] == "none" and len(prov["input_points"]) == 10
    assert all(len(x["master_spectrum_h5_sha256"]) == 64 for x in prov["input_points"])
    rows = list(csv.DictReader((Path(a["session_dir"]) / "feature_track.csv").open()))
    assert len(rows) == 10 and rows[0]["temperature"] == "" and rows[0]["gain"] == "" and rows[0]["receiver"] == "SYNTHETIC" or rows[0]["receiver"] == ""
    summary = (Path(a["session_dir"]) / "summary.md").read_text()
    assert "Diagnostics only" in summary and "UNCLASSIFIED" in summary
    assert not re.search(r"\bis RFI\b|\bis Galactic\b", summary)


def test_validator_catches_tampering(drift_fixture, tmp_path):
    tmp, reduce_dir, inp = drift_fixture
    a = run_forensics(str(reduce_dir), cfg_for(inp), output_root=str(tmp_path / "o"))
    d = Path(a["session_dir"])
    (d / "models.json").write_text((d / "models.json").read_text() + " ")
    res = validate_forensics_session(d)
    assert not res["ok"] and any("sha256 mismatch" in p for p in res["problems"])
    (d / "provenance.json").unlink()
    assert not validate_forensics_session(d)["ok"]
    d2 = Path(run_forensics(str(reduce_dir), cfg_for(inp), output_root=str(tmp_path / "o2"))["session_dir"])
    m = json.loads((d2 / "manifest.json").read_text())
    m["forensics_schema_version"] = "9.9"
    (d2 / "manifest.json").write_text(json.dumps(m))
    assert "unknown forensics_schema_version" in validate_forensics_session(d2)["problems"][0]


def test_output_isolation_collision_and_path_safety(drift_fixture, tmp_path):
    tmp, reduce_dir, inp = drift_fixture
    before = sorted(str(p.relative_to(reduce_dir)) for p in Path(reduce_dir).rglob("*"))
    r = run_forensics(str(reduce_dir), cfg_for(inp), output_root=str(tmp_path / "o"), session_id="FIXED")
    assert sorted(str(p.relative_to(reduce_dir)) for p in Path(reduce_dir).rglob("*")) == before        # REDUCE session untouched
    with pytest.raises(FileExistsError):
        run_forensics(str(reduce_dir), cfg_for(inp), output_root=str(tmp_path / "o"), session_id="FIXED")
    with pytest.raises(ValueError):
        run_forensics(str(reduce_dir), cfg_for(inp), output_root=str(tmp_path / "o"), session_id="..")
    with pytest.raises(ValueError, match="never writes inside"):
        run_forensics(str(reduce_dir), cfg_for(inp), output_root=str(reduce_dir))


def test_failure_marks_the_session_failed_never_completed(drift_fixture, tmp_path, monkeypatch):
    tmp, reduce_dir, inp = drift_fixture
    import science_forensics.session as S
    monkeypatch.setattr(S, "analyze", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    with pytest.raises(RuntimeError):
        run_forensics(str(reduce_dir), cfg_for(inp), output_root=str(tmp_path / "o"), session_id="BOOM")
    m = json.loads((tmp_path / "o" / inp.campaign_id / "BOOM" / "manifest.json").read_text())
    assert m["status"] == "FAILED" and "injected" in m["error"]
    assert not validate_forensics_session(tmp_path / "o" / inp.campaign_id / "BOOM")["ok"]


def _cli(args):
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(args)
    return code, out.getvalue()


def test_cli_plan_run_validate_compare_and_blocked_cases(drift_fixture, tmp_path):
    tmp, reduce_dir, inp = drift_fixture
    common = [str(reduce_dir), "--window-frame", "channel", "--center", "512", "--half-width", "100", "--sort-modes", "capture"]
    code, text = _cli(["plan", *common, "--json"])
    plan = json.loads(text)
    assert code == 0 and not plan["blocked"] and plan["n_points_available"] == 10 and "manifest.json" in plan["expected_outputs"]
    assert plan["metadata_unavailable_in_level1"]["temperature"]
    code, text = _cli(["run", *common, "--output-root", str(tmp_path / "o"), "--json"])
    assert code == 0
    session = json.loads(text)["session_dir"]
    assert _cli(["validate", session])[0] == 0
    code, text = _cli(["run", *common, "--output-root", str(tmp_path / "o"), "--json"])
    other = json.loads(text)["session_dir"]
    code, text = _cli(["compare", session, other])
    assert code == 0 and "EQUIVALENT" in text
    # blocked: window outside the spectrum -> no writes, exit 1
    bad = [str(reduce_dir), "--window-frame", "lsrk_velocity", "--center", "900000", "--half-width", "1000"]
    assert _cli(["plan", *bad])[0] == 1
    assert cli.main(["run", *bad, "--output-root", str(tmp_path / "nowrite")]) == 1 and not (tmp_path / "nowrite").exists()
    assert cli.main(["plan", *common[:1], "--center", "1", "--half-width", "0"]) == 2                     # zero-width window
    code, text = _cli(["inspect", str(reduce_dir), "--scan-v-min", "-100000", "--scan-v-max", "100000"])
    assert code == 0 and "suggested window" in text


# ------------------------------------------------------------------ boundaries: Level 1 only, no RAW, no network

_AUDIT = {"active": False, "opened": [], "network": []}


def _hook(event, args):
    if not _AUDIT["active"]:
        return
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        _AUDIT["opened"].append(os.fsdecode(args[0]))
    elif event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname"):
        _AUDIT["network"].append(event)


sys.addaudithook(_hook)


def test_runs_from_an_isolated_copy_without_raw_hardware_or_network(drift_fixture, tmp_path, monkeypatch):
    tmp, reduce_dir, inp = drift_fixture
    copy = tmp_path / "COPY"
    shutil.copytree(reduce_dir, copy)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    def boom(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    _AUDIT.update(active=True, opened=[], network=[])
    try:
        r = run_forensics(str(copy), cfg_for(inp), output_root=str(tmp_path / "out"))
    finally:
        _AUDIT["active"] = False
    assert r["status"] == "COMPLETED" and not _AUDIT["network"]
    assert not [p for p in _AUDIT["opened"] if "data/mosaic" in p or "/mosaic/" in p]
    data_files = [p for p in _AUDIT["opened"] if p.endswith((".h5", ".hdf5")) and "site-packages" not in p]
    assert all(str(tmp_path) in p for p in data_files)


ALLOWED_FROZEN_IMPORTS = {"reduce_engine.models", "science_engine.ingest", "science_engine.spatial"}
FORBIDDEN_ROOTS = {"capture", "sdr_capture", "rtl_tcp", "SoapySDR", "requests", "urllib", "http", "socket", "scipy", "observation_orchestrator",
                   "indi_telescope_control", "alignment_engine", "calibration_engine", "quicklook_live"}


def test_import_boundary_only_consumes_frozen_level1_and_science_helpers():
    seen = set()
    for f in sorted((ROOT / "science_forensics").glob("*.py")) + [ROOT / "almita_science_forensics.py"]:
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module]
            for mod in mods:
                assert mod.split(".")[0] not in FORBIDDEN_ROOTS, f"{f.name}: {mod}"
                if mod.split(".")[0] in ("reduce_engine", "science_engine"):
                    seen.add(mod)
    synthetic_only = {"science_engine.models", "science_engine.simulation"}     # fixture writer, test support only
    assert seen - synthetic_only <= ALLOWED_FROZEN_IMPORTS, seen


def _code_string_constants(path):
    """String constants that are CODE (not docstrings): what the module could actually open, run or print."""
    tree = ast.parse(path.read_text())
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                doc_ids.add(id(first.value))
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids]


def test_code_never_references_raw_trees_hardware_or_absolute_calibration_terms():
    for f in sorted((ROOT / "science_forensics").glob("*.py")) + [ROOT / "almita_science_forensics.py"]:
        for text in _code_string_constants(f):
            assert "data/mosaic" not in text and "rtl_tcp" not in text.lower(), (f.name, text[:60])
            assert not re.search(r"\b(Kelvin|brightness temperature|SEFD|Tsys)\b", text), (f.name, text[:60])


# ------------------------------------------------------------------ real data (skipped when absent)

@pytest.mark.skipif(not REAL_9.is_dir(), reason="real REDUCE session not present")
def test_real_nine_point_feature_is_traced_with_all_frames_and_declared_confounded(tmp_path):
    cfg = ForensicsConfig(window_center=150_857.0, window_half_width=5_607.0, sort_modes=("capture",))
    r = run_forensics(str(REAL_9), cfg, output_root=str(tmp_path))
    a = r["analysis"]
    assert a["status"] == "OK" and a["n_points_measured"] == 9
    fc = a["frame_coherence"]["frames"]
    assert set(fc) == {"lsrk_velocity_m_s", "topocentric_frequency_hz", "channel_bin"}
    assert fc["topocentric_frequency_hz"]["range_channels"] > 50 and fc["lsrk_velocity_m_s"]["range_channels"] > 50
    assert a["confounding"]["confounded_time_sky"]                      # 3x3 raster over 4.3 minutes
    assert a["drift"]["topocentric_frequency_hz"]["hz_per_hour"] < 0
    assert any(e["label"] == "INCONSISTENT WITH" for e in a["evidence"])
    assert r["analysis"]["axes"]["time_span_hours"] * 60 == pytest.approx(4.3, abs=0.3)
    assert validate_forensics_session(r["session_dir"])["ok"]
    assert r["candidate"]["status"] == "UNCLASSIFIED"


@pytest.mark.skipif(not REAL_9.is_dir(), reason="real REDUCE session not present")
def test_real_session_copy_runs_without_raw_or_network_from_an_unrelated_cwd(tmp_path, monkeypatch):
    """SECTION 54: forensics from a COPY of a real Level 1 session alone; every open() is audited; the source_campaign_root
    recorded inside the REDUCE manifest (a data/mosaic path) must never be followed."""
    copy = tmp_path / "REDUCE_COPY"
    shutil.copytree(REAL_9, copy)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    def boom(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    cfg = ForensicsConfig(window_center=150_857.0, window_half_width=5_607.0, sort_modes=("capture",))
    _AUDIT.update(active=True, opened=[], network=[])
    try:
        r = run_forensics(str(copy), cfg, output_root=str(tmp_path / "out"))
    finally:
        _AUDIT["active"] = False
    assert r["status"] == "COMPLETED" and not _AUDIT["network"]
    assert not [p for p in _AUDIT["opened"] if "data/mosaic" in p or "/mosaic/" in p]
    data_files = [p for p in _AUDIT["opened"] if p.endswith((".h5", ".hdf5", ".json", ".csv", ".npz")) and "site-packages" not in p
                  and "matplotlib" not in p and "astropy" not in p and "/proc/" not in p]
    assert data_files and all(str(tmp_path) in p for p in data_files), [p for p in data_files if str(tmp_path) not in p][:3]
