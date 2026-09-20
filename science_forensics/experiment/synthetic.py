"""Schedule-based synthetic Level 1 campaigns for the experiment power test. Unlike forensics.synthetic (uniform cadence, one
line) this takes the per-capture time and sky position from an experiment schedule and injects a candidate line, control lines
and a DC-like spur, each with its OWN frame behaviour, so that 'sky-fixed', 'receiver-fixed', temporal drift (whole spectrum or
candidate only), spatial gradient and mixtures are simulated as physically distinct things."""
from __future__ import annotations

from typing import Optional

import numpy as np

from science_forensics.models import ForensicsInput, ForensicsPoint
from science_forensics.synthetic import F_REST, DF_HZ, DV_M_S, T0_POSIX, C_LIGHT_M_S

SCENARIOS = ("sky_fixed", "time_drift", "receiver_fixed", "spatial_gradient", "spatial_gradient_ra", "time_plus_spatial",
             "whole_spectrum_drift", "candidate_only_drift")
TRUTH_CLASS = {"sky_fixed": "SKY_FIXED", "time_drift": "TIME_GLOBAL", "receiver_fixed": "RECEIVER_FIXED",
               "spatial_gradient": "SKY_GRADIENT", "spatial_gradient_ra": "SKY_GRADIENT", "time_plus_spatial": "MIXED_TIME_AND_SPATIAL",
               "whole_spectrum_drift": "TIME_GLOBAL", "candidate_only_drift": "TIME_CANDIDATE_ONLY"}
N_CH = 2048
CAND_CH = 1300.0
CONTROL_CH = (260.0, 420.0, 560.0, 1650.0, 1800.0, 1930.0)
SPUR_CH = 200.0


def build_experiment_campaign(caps: list, scenario: str, shift_model: dict, *, seed: int = 0, amplitude: float = 0.6, noise_sigma: float = 0.035,
                              fwhm_channels: float = 22.0, drift_hz_per_hour: float = -323_525.0, gradient_hz_per_deg: float = 2_500.0,
                              gradient_axis: str = "dec", control_amp: float = 0.5, capture_s: float = 10.0,
                              t0_posix: float = T0_POSIX, ref_label: str = "A") -> ForensicsInput:
    """caps: design.Capture list. shift_model: {label: (shift0_m_s, rate_m_s_per_h)} = the real LSRK shift and its slow time change.
    Frequencies are topocentric; the pointing-dependent LSRK shift enters ONLY through sky-frame lines, exactly as in Level 1."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    if scenario.endswith("_ra"):
        gradient_axis, scenario = "ra", scenario[:-3]
    rng = np.random.default_rng(seed)
    k = np.arange(N_CH, dtype=float)
    f = F_REST - (N_CH / 2 - k) * DF_HZ
    kk = F_REST / C_LIGHT_M_S
    ra0 = np.mean([c.ra_deg for c in caps])
    dec0 = np.mean([c.dec_deg for c in caps])
    s_ref = shift_model[ref_label][0]
    f_c0, f_ctl = f[int(CAND_CH)], [f[int(c)] for c in CONTROL_CH]
    t_first = caps[0].t_mid_s
    points = []
    for i, c in enumerate(caps):
        t_h = (c.t_mid_s - t_first) / 3600.0
        s0, rate = shift_model[c.label]
        shift = s0 + rate * t_h                                   # LSRK shift of THIS capture (m/s)
        sky_df = kk * (shift - (s_ref + shift_model[ref_label][1] * 0.0))   # topocentric offset of a sky-stationary line vs reference
        off = ((c.ra_deg - ra0 + 180.0) % 360.0 - 180.0) * np.cos(np.radians(dec0)) if gradient_axis == "ra" else (c.dec_deg - dec0)
        grad = gradient_hz_per_deg * off
        ramp = drift_hz_per_hour * t_h
        wave = 25_000.0 * np.sin(2 * np.pi * t_h / 0.9)         # whole-spectrum non-linear wander (~85 channels amplitude)
        if scenario == "sky_fixed":
            fc, ctl = f_c0 + sky_df, [x + sky_df for x in f_ctl]
        elif scenario == "receiver_fixed":
            fc, ctl = f_c0, list(f_ctl)
        elif scenario == "time_drift":                              # linear ramp of the whole spectral scale (sky lines included)
            fc, ctl = f_c0 + sky_df + ramp, [x + sky_df + ramp for x in f_ctl]
        elif scenario == "whole_spectrum_drift":
            fc, ctl = f_c0 + sky_df + wave + ramp * 0.5, [x + sky_df + wave + ramp * 0.5 for x in f_ctl]
        elif scenario == "candidate_only_drift":
            fc, ctl = f_c0 + sky_df + ramp, [x + sky_df for x in f_ctl]
        elif scenario == "spatial_gradient":
            fc, ctl = f_c0 + sky_df + grad, [x + sky_df for x in f_ctl]
        else:   # time_plus_spatial
            fc, ctl = f_c0 + sky_df + grad + ramp, [x + sky_df for x in f_ctl]
        y = rng.normal(0.0, noise_sigma, N_CH)
        s_c = fwhm_channels / 2.3548
        y += amplitude * np.exp(-0.5 * (k - (fc - f[0]) / DF_HZ) ** 2 / s_c ** 2)
        for x in ctl:
            y += control_amp * np.exp(-0.5 * (k - (x - f[0]) / DF_HZ) ** 2 / 2.0 ** 2)
        y += 0.8 * np.exp(-0.5 * (k - SPUR_CH) ** 2 / 1.0)                          # DC-like spur: always receiver fixed
        v_axis = C_LIGHT_M_S * (1.0 - f / F_REST) + shift
        t = t0_posix + (c.t_mid_s - t_first) - 0.5 * capture_s
        points.append(ForensicsPoint(point_index=i + 1, t_start_s=t, t_end_s=t + capture_s, ra_deg=float(c.ra_deg), dec_deg=float(c.dec_deg),
                                     frequency_hz=f.copy(), velocity_lsrk_m_s=v_axis, value=y, sigma=np.full(N_CH, noise_sigma),
                                     mask=np.zeros(N_CH, dtype=np.int64), lsrk_shift_m_s=float(shift), quality_state="GOOD",
                                     timestamp_utc="", receiver="SYNTHETIC", capture_ids=[f"SYN_{i + 1:04d}"],
                                     rfi_ref_available=False, source_h5_sha256=f"{i + 1:064x}", source_json_sha256=f"{i + 1:064x}"))
    return ForensicsInput(campaign_id=f"SYN-{scenario}", reduce_session_id="REDUCE-SYNTHETIC", reduce_session_dir="<synthetic>",
                          reduce_session_status="COMPLETED", reduce_manifest_sha256="0" * 64, rest_frequency_hz=F_REST, points=points,
                          unavailable={"temperature": "synthetic"})


from science_forensics.experiment.design import shift_model_from_expected  # noqa: E402,F401  (re-export)
