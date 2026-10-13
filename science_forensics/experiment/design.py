"""Experiment DESIGN: positions, observation sequences, timing from real evidence, confounding diagnostics, visibility and
expected-signature predictions. Pure planning: no hardware, no network, nothing is executed.

The old 9-point raster had corr(time, Dec) = 0.95 and VIF 11: time, Dec and point order were one variable. The remedy is a smarter
OBSERVATION: (1) the same sky is revisited at many times (fixed-sky series), (2) different sky is visited at almost the same time
(alternating positions bracketed by a reference position, so the reference series can be interpolated to the time of each
other-position capture), with an order that is balanced but not periodic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from science_forensics.statistics import confounding_block, ols
from science_forensics.models import ForensicsConfig

C_LIGHT_M_S = 299_792_458.0
F_REST_HZ = 1_420_405_751.77
CHANNEL_WIDTH_HZ = 2_400_000.0 / 8192.0
TIMING_PATH = Path(__file__).resolve().parent.parent / "timing_evidence.json"


@dataclass(frozen=True)
class Position:
    label: str
    ra_deg: float
    dec_deg: float


@dataclass
class Capture:
    order: int
    label: str
    ra_deg: float
    dec_deg: float
    t_start_s: float
    t_mid_s: float
    move_axis_deg: float
    noncapture_s: float


def move_axis_deg(a: Position, b: Position) -> float:
    """Size of a move as used by the timing evidence: max(|dRA*cos(dec)|, |dDec|) in degrees."""
    dra = ((b.ra_deg - a.ra_deg + 180.0) % 360.0 - 180.0) * np.cos(np.radians(b.dec_deg))
    return float(max(abs(dra), abs(b.dec_deg - a.dec_deg)))


def angular_separation_deg(a: Position, b: Position) -> float:
    r1, d1, r2, d2 = map(np.radians, (a.ra_deg, a.dec_deg, b.ra_deg, b.dec_deg))
    s = np.sin((d2 - d1) / 2) ** 2 + np.cos(d1) * np.cos(d2) * np.sin((r2 - r1) / 2) ** 2
    return float(np.degrees(2 * np.arcsin(min(1.0, np.sqrt(s)))))


class TimingModel:
    """Non-capture seconds per point vs move size, from real capture_timing CSVs (see timing_evidence.json)."""

    def __init__(self, evidence: Optional[dict] = None):
        self.evidence = evidence or json.loads(TIMING_PATH.read_text())
        self.table = self.evidence["table"]

    def noncapture_s(self, axis_deg: float, quantile: str = "median") -> float:
        key = {"median": "median_s", "p75": "p75_s", "p90": "p90_s"}[quantile]
        for row in self.table:
            if row["axis_deg_min"] <= axis_deg < row["axis_deg_max"]:
                return float(row[key])
        return float(self.table[-1][key])

    def to_dict(self) -> dict:
        return {"n_intervals": self.evidence["n_intervals"], "n_campaigns": self.evidence["n_campaigns"], "source": self.evidence["source"]}


# ---------------------------------------------------------------- sequences (lists of position labels)

def seq_fixed(label: str, n: int) -> list:
    return [label] * n


def seq_abac(n_blocks: int) -> list:
    return ["A", "B", "A", "C"] * n_blocks


def seq_latin_example(n_blocks: int) -> list:
    """The conceptual example from the brief: [A B C A], [C A B A], [B A C A], cycled."""
    blocks = (["A", "B", "C", "A"], ["C", "A", "B", "A"], ["B", "A", "C", "A"])
    return [x for i in range(n_blocks) for x in blocks[i % 3]]


def seq_random_bracketed(n_blocks: int, seed: int = 0) -> list:
    """A x A y A ...: the reference A alternates with the other positions, whose order inside each block is a balanced random
    permutation of (B, C) (each block's first element alternates, shuffled in pairs) - no fixed period to alias with an instrumental
    rhythm, and every B/C capture is bracketed by A captures one step before and after."""
    rng = np.random.default_rng(seed)
    firsts = []
    for _ in range((n_blocks + 1) // 2):
        pair = ["B", "C"]
        rng.shuffle(pair)
        firsts += pair
    seq = ["A"]
    for i in range(n_blocks):
        x, y = ("B", "C") if firsts[i] == "B" else ("C", "B")
        seq += [x, "A", y, "A"]
    return seq


def seq_random_pure(n: int, seed: int = 0, labels=("A", "B", "C")) -> list:
    rng = np.random.default_rng(seed)
    seq = [labels[int(rng.integers(len(labels)))]]
    while len(seq) < n:
        c = labels[int(rng.integers(len(labels)))]
        seq.append(c)
    return seq


def seq_raster_reference(n_rows: int = 3, n_cols: int = 3) -> list:
    """Label-level analogue of the old campaign: a monotone serpentine visiting every cell once."""
    out = []
    for r in range(n_rows):
        cols = range(n_cols) if r % 2 == 0 else range(n_cols - 1, -1, -1)
        out += [f"P{r}{c}" for c in cols]
    return out


# ---------------------------------------------------------------- schedule

def build_schedule(positions: dict, sequence: list, capture_s: float, timing: TimingModel, *, quantile: str = "median",
                   initial_slew_s: float = 60.0, t0_s: float = 0.0) -> list:
    caps, t, prev = [], float(t0_s), None
    for k, label in enumerate(sequence):
        pos = positions[label]
        axis = move_axis_deg(prev, pos) if prev is not None else 5.0
        nc = timing.noncapture_s(axis, quantile) + (initial_slew_s if prev is None else 0.0)
        # the non-capture part happens before and after the capture; the capture midpoint is used as the observation time
        t_start = t + 0.5 * nc
        caps.append(Capture(k + 1, label, pos.ra_deg, pos.dec_deg, t_start, t_start + 0.5 * capture_s, axis, nc))
        t = t + nc + capture_s
        prev = pos
    return caps


def schedule_duration_s(caps: list, capture_s: float) -> float:
    return caps[-1].t_mid_s + 0.5 * capture_s + 0.5 * caps[-1].noncapture_s - caps[0].t_start_s + 0.5 * caps[0].noncapture_s


# ---------------------------------------------------------------- diagnostics

def _pred(caps: list) -> dict:
    t = np.array([c.t_mid_s for c in caps]) / 3600.0
    ra = np.array([c.ra_deg for c in caps])
    dec = np.array([c.dec_deg for c in caps])
    ra_mean = np.degrees(np.arctan2(np.mean(np.sin(np.radians(ra))), np.mean(np.cos(np.radians(ra))))) % 360.0
    ra_off = ((ra - ra_mean + 180.0) % 360.0 - 180.0) * np.cos(np.radians(dec.mean()))
    return {"time_h": t - t.min(), "point_index": np.arange(1, len(caps) + 1, dtype=float), "ra_off_deg": ra_off, "dec_off_deg": dec - dec.mean()}


def design_diagnostics(caps: list, *, sigma_hz: tuple = (30.5, 2446.0), bracket_tolerance_factor: float = 1.5,
                       shift_model: Optional[dict] = None) -> dict:
    """Concrete diagnostics of an observation schedule (no composite score): time-sky correlations, VIF, condition number,
    per-position time balance, bracketing by the reference position, and the standard errors of the joint regression
    centroid ~ 1 + time + RA + Dec (in Hz/hour and Hz/degree) for the given per-capture centroid noise levels."""
    pred = _pred(caps)
    labels = [c.label for c in caps]
    conf = confounding_block(pred, ForensicsConfig(window_center=1.0, window_half_width=1.0))
    n = len(caps)
    X = np.column_stack([np.ones(n), pred["time_h"], pred["ra_off_deg"], pred["dec_off_deg"]])
    rank = int(np.linalg.matrix_rank(X))
    se = {}
    if rank == 4:
        inv = np.linalg.inv(X.T @ X)
        for s in sigma_hz:
            se[f"sigma_{s:g}_hz"] = {"time_coefficient_se_hz_per_hour": float(s * np.sqrt(inv[1, 1])),
                                     "ra_coefficient_se_hz_per_deg": float(s * np.sqrt(inv[2, 2])),
                                     "dec_coefficient_se_hz_per_deg": float(s * np.sqrt(inv[3, 3]))}
    shift_block = None
    if shift_model is not None:
        # physically parameterised regression  f = a + b t + gamma * (f0/c) * (LSRK shift - mean):  b = excess time drift (Hz/h),
        # gamma = 1 stationary sky, 0 receiver-fixed. corr(t, shift) is THE confounding that matters for this question.
        t_h = pred["time_h"]
        sh = np.array([shift_model[c.label][0] + shift_model[c.label][1] * (c.t_mid_s - caps[0].t_mid_s) / 3600.0 for c in caps])
        xg = (F_REST_HZ / C_LIGHT_M_S) * (sh - sh.mean())
        Xg = np.column_stack([np.ones(n), t_h, xg])
        if np.linalg.matrix_rank(Xg) == 3 and float(np.std(xg)) > 0:
            invg = np.linalg.inv(Xg.T @ Xg)
            corr = float(np.corrcoef(t_h, xg)[0, 1])
            shift_block = {"corr_time_lsrk_shift": corr, "vif_time_shift": float(1.0 / (1.0 - corr ** 2)) if abs(corr) < 1 else float("inf"),
                           "lsrk_shift_range_m_s": float(np.ptp(sh)), "gamma_se_per_unit_sigma_hz": float(np.sqrt(invg[2, 2])),
                           "time_se_per_unit_sigma_hz_per_hour": float(np.sqrt(invg[1, 1])),
                           "sky_vs_receiver_separation_in_sigma_at_30hz": float(1.0 / (30.5 * np.sqrt(invg[2, 2])))}
        else:
            shift_block = {"corr_time_lsrk_shift": float("nan"), "note": "no shift variation (one sky position): sky vs receiver-fixed not testable"}
    times = np.array([c.t_mid_s for c in caps])
    cadence = float(np.median(np.diff(times))) if n > 1 else float("nan")
    tolerance = bracket_tolerance_factor * cadence
    bracketed, others = 0, 0
    for i, lab in enumerate(labels):
        if lab == "A":
            continue
        others += 1
        before = any(labels[j] == "A" and 0 < times[i] - times[j] <= tolerance for j in range(max(0, i - 3), i))
        after = any(labels[j] == "A" and 0 < times[j] - times[i] <= tolerance for j in range(i + 1, min(n, i + 4)))
        bracketed += int(before and after)
    distinct = sorted(set(labels))
    mean_t = {lab: float(np.mean([t for t, l in zip(times, labels) if l == lab])) for lab in distinct}
    span = float(times.max() - times.min())
    balance = max(abs(m - times.mean()) for m in mean_t.values()) / span if span > 0 else float("nan")
    return {"n_captures": n, "counts": {lab: labels.count(lab) for lab in distinct}, "duration_min": span / 60.0,
            "median_cadence_s": cadence, "pair_time_tolerance_s": tolerance,
            "corr_time_ra": conf["correlation_matrix"]["time_h"]["ra_off_deg"], "corr_time_dec": conf["correlation_matrix"]["time_h"]["dec_off_deg"],
            "corr_time_point_index": conf["correlation_matrix"]["time_h"]["point_index"],
            "max_abs_corr_time_sky": conf["max_abs_corr_time_vs_sky"], "vif": conf["vif"],
            "standardized_condition_number": conf["standardized_condition_number"], "design_rank": rank,
            "confounded_time_sky": conf["confounded_time_sky"], "position_time_balance_fraction_of_span": float(balance),
            "mean_time_min_by_position": {k: v / 60.0 for k, v in mean_t.items()},
            "bracketed_fraction_of_non_reference_captures": (bracketed / others) if others else float("nan"),
            "joint_model_standard_errors": se, "shift_regression": shift_block}


def min_detectable_drift_hz_per_hour(diag: dict, sigma_key: str, z: float = 5.0) -> float:
    return z * diag["joint_model_standard_errors"][sigma_key]["time_coefficient_se_hz_per_hour"]


def captures_needed_for_rate(rate_hz_per_hour: float, sigma_hz: float, cadence_s: float, z: float = 5.0) -> int:
    """Equispaced captures at ONE sky position: SE(slope) = sigma / (cadence_h * sqrt(N (N^2-1) / 12)); smallest N with rate/SE >= z."""
    cad_h = cadence_s / 3600.0
    for n in range(3, 100_000):
        se = sigma_hz / (cad_h * np.sqrt(n * (n * n - 1) / 12.0))
        if abs(rate_hz_per_hour) / se >= z:
            return n
    return -1


# ---------------------------------------------------------------- geometry (offline)

def lst_hours(t_unix: np.ndarray, longitude_deg: float) -> np.ndarray:
    from science_forensics.ingest import local_sidereal_hours
    return local_sidereal_hours(np.asarray(t_unix, dtype=float), longitude_deg)


def hour_angle_hours(lst_h: np.ndarray, ra_deg: float) -> np.ndarray:
    return (np.asarray(lst_h) - ra_deg / 15.0 + 12.0) % 24.0 - 12.0


def altitude_deg(lat_deg: float, dec_deg: float, ha_hours: np.ndarray) -> np.ndarray:
    ha = np.radians(np.asarray(ha_hours) * 15.0)
    return np.degrees(np.arcsin(np.sin(np.radians(lat_deg)) * np.sin(np.radians(dec_deg)) +
                                np.cos(np.radians(lat_deg)) * np.cos(np.radians(dec_deg)) * np.cos(ha)))


def visibility_ok(positions: dict, site: dict, t_unix: np.ndarray, *, min_alt_deg: float, ha_side: str, ha_margin_h: float) -> dict:
    """All positions, at all times: altitude >= min and hour angle strictly on ONE side of the meridian with a margin (a sign
    change would move a point to the other block in capture.py and would cost ~64 s slews)."""
    lst = lst_hours(t_unix, site["longitude_deg"])
    worst_alt, worst_ha = 90.0, {}
    for lab, p in positions.items():
        ha = hour_angle_hours(lst, p.ra_deg)
        alt = altitude_deg(site["latitude_deg"], p.dec_deg, ha)
        worst_alt = min(worst_alt, float(alt.min()))
        worst_ha[lab] = [float(ha.min()), float(ha.max())]
    side_ok = all((hi <= -ha_margin_h) if ha_side == "negative" else (lo >= ha_margin_h) for lo, hi in worst_ha.values())
    return {"ok": bool(worst_alt >= min_alt_deg and side_ok), "min_altitude_deg": worst_alt, "hour_angle_range_h": worst_ha,
            "side": ha_side, "ha_margin_h": ha_margin_h}


def find_start_windows(positions: dict, site: dict, first_day_utc: str, days: int, duration_s: float, *, min_alt_deg: float = 20.0,
                       ha_margin_h: float = 0.25, step_min: int = 10) -> list:
    """Start times (UTC) from which the WHOLE session keeps every position on one side of the meridian, above min altitude."""
    from datetime import datetime, timedelta, timezone
    day0 = datetime.fromisoformat(first_day_utc).replace(tzinfo=timezone.utc)
    out = []
    for d in range(days):
        starts = [day0 + timedelta(days=d, minutes=m) for m in range(0, 24 * 60, step_min)]
        run = None
        for s in starts:
            t = np.array([s.timestamp(), s.timestamp() + duration_s])
            t = np.linspace(t[0], t[1], 7)
            ok = None
            for side in ("negative", "positive"):
                v = visibility_ok(positions, site, t, min_alt_deg=min_alt_deg, ha_side=side, ha_margin_h=ha_margin_h)
                if v["ok"]:
                    ok = side
                    break
            if ok and (run is None or run["side"] != ok):
                if run:
                    out.append(run)
                run = {"first_start_utc": s.isoformat(), "last_start_utc": s.isoformat(), "side": ok}
            elif ok:
                run["last_start_utc"] = s.isoformat()
            elif run:
                out.append(run)
                run = None
        if run:
            out.append(run)
    return out


def lsrk_shift_m_s(position: Position, t_unix: float, site: dict) -> float:
    """LSRK shift (m/s) with the SAME frozen helper REDUCE uses (validated: 0.0 m/s difference on the 9 real points)."""
    import astropy_offline
    astropy_offline.configure_astropy_offline()
    import astropy.units as u
    from astropy.coordinates import EarthLocation, SkyCoord
    from astropy.time import Time
    from alignment_engine.hi.velocity import frame_shift_km_s
    loc = EarthLocation(lat=site["latitude_deg"] * u.deg, lon=site["longitude_deg"] * u.deg, height=site.get("elevation_m", 0.0) * u.m)
    return float(frame_shift_km_s(frame="lsrk", obstime=Time(t_unix, format="unix", scale="utc"), location=loc,
                                  target=SkyCoord(position.ra_deg * u.deg, position.dec_deg * u.deg, frame="icrs")) * 1000.0)


def expected_signatures(positions: dict, site: dict, t_unix: float, duration_s: float) -> dict:
    """Quantitative predictions under 'stationary in the sky' (constant LSRK velocity) and 'stationary in the receiver':
    the topocentric-frequency offset between positions and the slow topocentric drift a sky-fixed line has at one position."""
    k = F_REST_HZ / C_LIGHT_M_S
    shifts = {lab: lsrk_shift_m_s(p, t_unix, site) for lab, p in positions.items()}
    later = {lab: lsrk_shift_m_s(p, t_unix + 3600.0, site) for lab, p in positions.items()}
    labels = sorted(positions)
    pairs = {}
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            ds = shifts[b] - shifts[a]
            pairs[f"{b}-{a}"] = {"delta_lsrk_shift_m_s": ds, "delta_lsrk_shift_channels": ds / (C_LIGHT_M_S * CHANNEL_WIDTH_HZ / F_REST_HZ),
                                 "sky_stationary_predicts_delta_topocentric_frequency_hz": k * ds,
                                 "receiver_fixed_predicts_delta_topocentric_frequency_hz": 0.0}
    drift = {lab: {"lsrk_shift_change_m_s_per_hour": later[lab] - shifts[lab],
                   "sky_stationary_predicts_topocentric_drift_hz_per_hour": k * (later[lab] - shifts[lab])} for lab in labels}
    return {"lsrk_shift_m_s": shifts, "pairwise": pairs, "fixed_sky_expected_drift": drift, "frequency_per_velocity_hz_per_m_s": k}


def choose_positions(anchor: Position, site: dict, t_unix: float, *, separation_deg: float = 4.5, max_move_deg: float = 7.0,
                     min_alt_deg: float = 20.0) -> dict:
    """B and C are picked from directions around the anchor A so that the LSRK-shift differences between positions are as large as
    possible (that is what lets one observation separate 'sky-fixed' from 'receiver-fixed'), with every pair >= separation apart
    (> 3 beam widths for a 1.5 deg beam), moves within the timing evidence, and altitude above the minimum."""
    cands = []
    for ang in range(0, 360, 30):
        for sep in (separation_deg, 1.5 * separation_deg):
            dec = anchor.dec_deg + sep * np.sin(np.radians(ang))
            ra = anchor.ra_deg + sep * np.cos(np.radians(ang)) / np.cos(np.radians(dec))
            cands.append(Position("?", float(ra % 360.0), float(dec)))
    shifts = {id(c): lsrk_shift_m_s(c, t_unix, site) for c in cands}
    s_a = lsrk_shift_m_s(anchor, t_unix, site)
    best, best_score = None, -1.0
    for i, b in enumerate(cands):
        for c in cands[i + 1:]:
            if min(angular_separation_deg(anchor, b), angular_separation_deg(anchor, c), angular_separation_deg(b, c)) < separation_deg - 1e-9:
                continue
            if max(move_axis_deg(anchor, b), move_axis_deg(anchor, c), move_axis_deg(b, c)) > max_move_deg:
                continue
            score = min(abs(shifts[id(b)] - s_a), abs(shifts[id(c)] - s_a), abs(shifts[id(b)] - shifts[id(c)]))
            if score > best_score:
                best, best_score = (b, c), score
    if best is None:
        raise ValueError("no B/C pair satisfies the separation / move constraints")
    return {"A": anchor, "B": Position("B", best[0].ra_deg, best[0].dec_deg), "C": Position("C", best[1].ra_deg, best[1].dec_deg),
            "min_pairwise_delta_shift_m_s": float(best_score)}


def shift_model_from_expected(expected: dict) -> dict:
    """{label: (LSRK shift m/s at t0, change per hour)} from expected_signatures."""
    return {lab: (expected["lsrk_shift_m_s"][lab], expected["fixed_sky_expected_drift"][lab]["lsrk_shift_change_m_s_per_hour"])
            for lab in expected["lsrk_shift_m_s"]}
