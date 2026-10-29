"""observation_gain_pilot.recommend_gain: one jump to the highest admitted gain the loudest pilot point can take,
with the quietest point checked against the 8-bit quantisation floor (operator, 2026-10-29: the SAWbird clips at 40.2)."""
import numpy as np

import observation_gain_pilot as gp
from calibration_engine.clipping import ClippingThresholds, evaluate_clipping
from calibration_engine.gain_sweep import CANDIDATE_GAIN_TABLE_DB
from calibration_engine.sample_statistics import compute_sample_statistics

CONFIG = gp.PilotConfig()


def _eval(sigma_codes: float, gain_db: float, seed: int = 1, n: int = 400_000) -> dict:
    """A pilot evaluation of Gaussian noise with true rms sigma_codes, clipped to uint8 like the real ADC."""
    rng = np.random.default_rng(seed)
    iq = np.clip(np.round(127.5 + rng.normal(0.0, sigma_codes, n)), 0, 255).astype(np.uint8)
    stats = compute_sample_statistics(iq)
    clip = evaluate_clipping(stats, ClippingThresholds(clipped_rail_hit_fraction=CONFIG.clipping_rail_hit_fraction))
    return {"sample_statistics": stats.to_dict(), "clipping": clip.to_dict(), "gain_db": gain_db}


def _verify_at(sigma_at_gain: float, gain_from: float, gain_to: float) -> dict:
    return _eval(sigma_at_gain * 10 ** ((gain_to - gain_from) / 20.0), gain_to, seed=9)


def test_heavily_clipped_high_jumps_down_in_one_step_and_the_new_gain_does_not_clip():
    # A ~40 dB LNA at 40.2: rms ~90 codes, a few % on the rails (as measured indoors on 2026-10-28).
    high = _eval(90.0, 40.2)
    assert high["clipping"]["status"] == "CLIPPED"
    rec = gp.recommend_gain({"HIGH": high}, 40.2, CONFIG)
    assert rec["changed"] and rec["recommended_gain_db"] <= 33.8     # several table steps at once, not 38.6
    assert rec["recommended_gain_db"] in CANDIDATE_GAIN_TABLE_DB
    assert _verify_at(90.0, 40.2, rec["recommended_gain_db"])["clipping"]["status"] == "OK"


def test_the_jump_lands_on_the_highest_admitted_value_that_still_has_the_comfortable_margin():
    rec = gp.recommend_gain({"HIGH": _eval(90.0, 40.2)}, 40.2, CONFIG)
    table = list(CANDIDATE_GAIN_TABLE_DB)
    one_up = table[table.index(rec["recommended_gain_db"]) + 1]
    assert one_up > rec["max_gain_db"]                               # the next value up is over the ceiling
    assert rec["recommended_gain_db"] <= rec["max_gain_db"]


def test_a_quiet_high_point_allows_a_jump_up():
    rec = gp.recommend_gain({"HIGH": _eval(4.0, 29.7)}, 29.7, CONFIG)
    assert rec["recommended_gain_db"] > 33.8
    assert _verify_at(4.0, 29.7, rec["recommended_gain_db"])["clipping"]["status"] == "OK"


def test_a_comfortable_point_keeps_its_gain():
    ev = _eval(25.0, 40.2)
    rec = gp.recommend_gain({"HIGH": ev}, 40.2, CONFIG)
    assert rec["recommended_gain_db"] == 40.2 and not rec["changed"]


def test_high_and_low_report_the_dynamic_range_and_a_healthy_floor():
    rec = gp.recommend_gain({"HIGH": _eval(60.0, 40.2, seed=2), "LOW": _eval(30.0, 40.2, seed=3)}, 40.2, CONFIG)
    assert rec["worst_pilot"] == "HIGH"
    assert abs(rec["dynamic_range_db"] - 6.0) < 0.5
    assert rec["quantisation_floor_ok"] and rec["low_sigma_codes_at_recommended"] >= gp.MIN_LOW_SIGMA_CODES


def test_a_dynamic_range_too_wide_to_fit_keeps_clipping_away_and_warns_about_the_quiet_end():
    rec = gp.recommend_gain({"HIGH": _eval(60.0, 40.2, seed=2), "LOW": _eval(1.2, 40.2, seed=3)}, 40.2, CONFIG)
    assert rec["recommended_gain_db"] <= rec["max_gain_db"]
    assert not rec["quantisation_floor_ok"] and "quantised" in rec["reason"]


def test_points_measured_at_different_gains_are_compared_at_the_same_gain():
    # VERIFY at 29.7 and LOW measured earlier at 40.2: the same sky as HIGH 60 / LOW 30 codes at 40.2.
    verify = _eval(60.0 * 10 ** ((29.7 - 40.2) / 20.0), 29.7, seed=4)
    rec = gp.recommend_gain({"VERIFY": verify, "LOW": _eval(30.0, 40.2, seed=5)}, 29.7, CONFIG)
    assert rec["worst_pilot"] == "VERIFY"
    assert abs(rec["dynamic_range_db"] - 6.0) < 0.5


def test_true_sigma_of_a_clipped_capture_is_recovered_from_the_rail_fraction():
    high = _eval(90.0, 40.2)
    assert high["sample_statistics"]["std_i"] < 85.0                # the clipped std under-reports
    assert abs(gp.estimate_true_sigma_codes(high["sample_statistics"]) - 90.0) < 5.0
