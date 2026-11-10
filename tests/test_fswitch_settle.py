"""fswitch_settle.py (operator 2026-11-09): the retune pause for frequency switching is measured from the stream itself.
A synthetic stream with a known latency and a known settling glitch must give those numbers back; a control (same
frequency again) must give nothing."""
import numpy as np
import pytest

import fswitch_settle as S

RATE = 2.4e6


def _stream(latency_s=0.020, glitch_s=0.006, seconds=1.0, cmd_s=0.4, switch=True, seed=2):
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    z = (rng.normal(0, 8, n) + 1j * rng.normal(0, 8, n))
    k_new = int((cmd_s + latency_s) * RATE)
    tone_old = 6 * np.exp(2j * np.pi * 200e3 * t)
    tone_new = 6 * np.exp(2j * np.pi * -300e3 * t)
    if switch:
        z[:k_new] += tone_old[:k_new]
        z[k_new:] = z[k_new:] * 1.06 + tone_new[k_new:]
        g = slice(k_new, k_new + int(glitch_s * RATE))
        z[g] *= 0.3                                       # the PLL relock dip
    else:
        z += tone_old
    raw = np.empty(2 * n, np.uint8)
    raw[0::2] = np.clip(np.round(z.real + 127.5), 0, 255)
    raw[1::2] = np.clip(np.round(z.imag + 127.5), 0, 255)
    return raw, int(cmd_s * RATE) * 2


def test_known_latency_comes_back_as_the_step():
    raw, cmd = _stream(latency_s=0.084, glitch_s=0.0)
    p = S.transition_profile(raw, cmd, RATE)
    assert p["step_ms"] == pytest.approx(84.0, abs=1.5)


def test_a_relock_dip_is_inside_the_time_to_settled_and_shows_in_the_aligned_average():
    trials = [S.transition_profile(*_stream(latency_s=0.06 + 0.004 * s, glitch_s=0.010, seed=s), RATE) for s in range(12)]
    assert all(t["step_ms"] == pytest.approx(70.0 + 4 * s, abs=2.0) for s, t in enumerate(trials))   # after the dip
    tail = S.aligned_tail(trials)
    assert tail["n"] == 12 and tail["settled_after_step_ms"] <= 4.0
    assert 8.0 <= tail["early_start_ms"] <= 14.0                      # the 10 ms dip, seen just before the step
    clean = [S.transition_profile(*_stream(latency_s=0.07, glitch_s=0.0, seed=s), RATE) for s in range(12)]
    t2 = S.aligned_tail(clean)
    assert t2["settled_after_step_ms"] <= 4.0 and t2["early_start_ms"] <= 4.0


def test_a_control_shows_nothing():
    raw, cmd = _stream(switch=False)
    p = S.transition_profile(raw, cmd, RATE, control=True)
    assert p["step_ms"] is None and p["control_max_dev_sigma"] < 6.0


def test_recommendation_has_margin_and_floor():
    assert S.recommend([10.0, 26.0, 22.0]) == pytest.approx(0.05)    # 39 ms -> floor 50 ms
    assert S.recommend([80.0, 120.0]) == pytest.approx(0.18)         # 120 x 1.5
