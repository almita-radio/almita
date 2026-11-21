#!/usr/bin/env python3
"""ALMITA EXPERIMENT - ready-made observing recipes, each with its verdict written down BEFORE the data exist.

Operator 2026-11-21: an EXPERIMENT mode with ready-made pilots for the LMC, the SMC, the Galactic Centre, the velocity,
etc., that can be started at any moment -> "go all in, its own page".

What the last month taught, built into the recipes (docs/BITACORA.md):
  - the frequency-switching baseline depends on WHERE the dish points and repeats (LMC sweep 12-11, SMC 19-11:
    -4.6 % toward the SMC, ~0 on the OFFs): an OFF must sit at the SAME ALTITUDE as the source, and ON/OFF must
    alternate many times - a few long passes say nothing;
  - comparing the MAXIMUM of a flat-topped profile invented a +3..+10 km/s offset (retracted 2026-11-21): velocity is
    compared by fitting the WHOLE profile (scale + linear baseline + shift);
  - the 19-11 FS 300 kHz centred data sit -5..-6.5 km/s low: the decisive test is the same tile, back to back, in
    each switching mode;
  - an SDR can come back mute after a restart: every batch is reduced at once and two batches with no accepted point
    stop the run.

Pure module: recipes (experiments/*.json, tracked in git), sky geometry (same-altitude OFFs, when a recipe can run),
run bookkeeping (data/experiments/runs/<run_id>/run.json, written atomically) and the analyses with their verdicts.
almita_experiment.py is the runner (one batch at a time through OBSERVE + REDUCE, the same calls as the SURVEY
pilot); no hardware, no processes here except systemctl for the runner's unit.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import astropy_offline  # noqa: F401 - offline IERS policy, like the rest of the planning code

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_sun
from astropy.time import Time

ROOT = Path(__file__).resolve().parent
RECIPES_DIR = ROOT / "experiments"
RUNS_BASE = ROOT / "data" / "experiments"
ACTIVE_NAME = "active.json"
UNIT = "almita-experiment.service"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")

KINDS = ("AB_VELOCITY", "ONOFF", "TANGENT", "LINE_CHECK")
ALT_MARGIN_DEG = 2.0           # a target must clear the floor by this much over the whole batch
BATCH_OVERHEAD_S = 75.0        # PLAN + capture.py start + first slew (19-11: ~1 min)
REDUCE_S_PER_BATCH = 60.0      # REDUCE of a short batch on the notebook (19-11: 41-45 s per point, in parallel)
POINT_MATCH_DEG = 5.0          # a reduced point belongs to the planned target within this
READY_SCAN_HOURS = 24.0
READY_STEP_MIN = 10.0


class RecipeError(ValueError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _finite(x) -> Optional[float]:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(x, nd=2) -> Optional[float]:
    x = _finite(x)
    return None if x is None else round(x, nd)


# ================================================================================================== recipes

def _num(d: Dict[str, Any], key: str, where: str, lo: float, hi: float, default=None) -> float:
    v = d.get(key, default)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not lo <= v <= hi:
        raise RecipeError(f"{where}.{key}: must be a number in [{lo}, {hi}], got {v!r}")
    return float(v)


def validate_recipe(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A recipe, checked field by field (never a free-form pass-through). Returns a normalised copy."""
    if not isinstance(raw, dict):
        raise RecipeError("recipe: must be a mapping")
    r = copy.deepcopy(raw)
    rid = r.get("id")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        raise RecipeError("id: letters, digits, - and _ only (max 48)")
    w = rid
    if r.get("kind") not in KINDS:
        raise RecipeError(f"{w}.kind: one of {KINDS}")
    for key in ("name", "question"):
        if not isinstance(r.get(key), str) or not r[key].strip():
            raise RecipeError(f"{w}.{key}: required text")
    r.setdefault("why", "")
    r.setdefault("after", [])
    r["capture_seconds"] = _num(r, "capture_seconds", w, 4.0, 1800.0, 120.0)
    r["settle_seconds"] = _num(r, "settle_seconds", w, 0.0, 60.0, 2.0)
    r["min_altitude_deg"] = _num(r, "min_altitude_deg", w, 5.0, 80.0, 25.0)
    r["sun_avoid_deg"] = _num(r, "sun_avoid_deg", w, 0.0, 120.0, 35.0)
    r["cycles"] = int(_num(r, "cycles", w, 1, 50, 1))
    r["allow_simulator"] = bool(r.get("allow_simulator", False))
    r["min_coverage"] = _num(r, "min_coverage", w, 0.1, 1.0, 1.0)
    # targets
    targets = r.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 30:
        raise RecipeError(f"{w}.targets: a list of 1-30 targets")
    ids = set()
    for i, t in enumerate(targets):
        tw = f"{w}.targets[{i}]"
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not _ID_RE.match(t["id"]):
            raise RecipeError(f"{tw}.id: required, letters/digits/-/_")
        if t["id"] in ids:
            raise RecipeError(f"{tw}.id: duplicate {t['id']!r}")
        ids.add(t["id"])
        kinds = [k for k in (("l", "b"), ("ra_hours", "dec_deg"), ("same_alt_as", "daz_deg")) if all(x in t for x in k)]
        if len(kinds) != 1:
            raise RecipeError(f"{tw}: give exactly one of (l, b), (ra_hours, dec_deg) or (same_alt_as, daz_deg)")
        if kinds[0] == ("l", "b"):
            _num(t, "l", tw, -360.0, 360.0)
            _num(t, "b", tw, -90.0, 90.0)
        elif kinds[0] == ("ra_hours", "dec_deg"):
            _num(t, "ra_hours", tw, 0.0, 24.0)
            _num(t, "dec_deg", tw, -90.0, 90.0)
        else:
            _num(t, "daz_deg", tw, -170.0, 170.0)
            if abs(t["daz_deg"]) < 5:
                raise RecipeError(f"{tw}.daz_deg: at least 5 deg away from the source")
    for t in targets:
        if "same_alt_as" in t:
            ref = next((x for x in targets if x["id"] == t["same_alt_as"]), None)
            if ref is None or "same_alt_as" in ref:
                raise RecipeError(f"{w}.targets[{t['id']}].same_alt_as: must name a fixed target of this recipe")
    off = r.setdefault("off_rules", {})
    off.setdefault("min_sep_deg", 25.0)
    off.setdefault("min_abs_b_deg", 0.0)
    off.setdefault("avoid", [])
    for a in off["avoid"]:
        if not all(k in a for k in ("l", "b", "r_deg")):
            raise RecipeError(f"{w}.off_rules.avoid: each needs l, b, r_deg")
    # modes
    modes = r.get("modes")
    if not isinstance(modes, dict) or not modes:
        raise RecipeError(f"{w}.modes: at least one mode")
    import fswitch_center
    for name, m in modes.items():
        mw = f"{w}.modes.{name}"
        if not _ID_RE.match(name) or not isinstance(m, dict):
            raise RecipeError(f"{mw}: bad mode")
        m.setdefault("label", name)
        m["frequency_switching"] = bool(m.get("frequency_switching", True))
        if m["frequency_switching"]:
            try:
                m["offset_hz"] = fswitch_center.parse_offset_hz(m.get("offset_hz"))
                m["center"] = fswitch_center.parse_centre(m.get("center"))
            except (TypeError, ValueError) as exc:
                raise RecipeError(f"{mw}: {exc}") from None
        else:
            if m.get("offset_hz") is not None or m.get("center") is not None:
                raise RecipeError(f"{mw}: offset_hz/center need frequency_switching")
            m["offset_hz"], m["center"] = None, None
    # sequence: one cycle = these batches in order
    seq = r.get("sequence")
    if not isinstance(seq, list) or not 1 <= len(seq) <= 20:
        raise RecipeError(f"{w}.sequence: 1-20 batches")
    for i, s in enumerate(seq):
        sw = f"{w}.sequence[{i}]"
        if not isinstance(s, dict) or s.get("mode") not in modes:
            raise RecipeError(f"{sw}.mode: one of {sorted(modes)}")
        if not isinstance(s.get("targets"), list) or not s["targets"] or any(x not in ids for x in s["targets"]):
            raise RecipeError(f"{sw}.targets: ids of this recipe's targets")
        s["choose"] = int(s.get("choose") or 0)
        if s["choose"] < 0 or s["choose"] > len(s["targets"]):
            raise RecipeError(f"{sw}.choose: 0 (all) .. {len(s['targets'])}")
    a = r.setdefault("analysis", {})
    k = r["kind"]
    if k == "AB_VELOCITY":
        if a.get("reference_mode") not in modes:
            raise RecipeError(f"{w}.analysis.reference_mode: one of the modes")
        a.setdefault("tolerance_km_s", 1.5)
        a.setdefault("min_passes", 2)
        a.setdefault("reference_reduce", None)
    elif k == "ONOFF":
        if a.get("on") not in ids:
            raise RecipeError(f"{w}.analysis.on: the source target id")
        win = a.get("window_km_s")
        if not (isinstance(win, list) and len(win) == 2 and win[0] < win[1]):
            raise RecipeError(f"{w}.analysis.window_km_s: [low, high]")
        exp = a.get("expected_pct")
        if not (isinstance(exp, list) and len(exp) == 2 and 0 < exp[0] <= exp[1]):
            raise RecipeError(f"{w}.analysis.expected_pct: [low, high] > 0")
        a.setdefault("min_pairs", 4)
        a.setdefault("max_off_abs_pct", 1.5)
    elif k == "TANGENT":
        a.setdefault("edge_fraction", 0.03)
        a.setdefault("nsigma", 4.0)
        a.setdefault("min_points", 3)
    elif k == "LINE_CHECK":
        a.setdefault("snr_ok", 20.0)
        a.setdefault("snr_weak", 5.0)
    return r


def load_recipes(base: Path = RECIPES_DIR) -> List[Dict[str, Any]]:
    out = []
    for f in sorted(base.glob("*.json")) if base.is_dir() else []:
        raw = json.loads(f.read_text(encoding="utf-8"))
        out.append(validate_recipe(raw))
    out.sort(key=lambda r: (r.get("order", 99), r["id"]))
    return out


def get_recipe(recipe_id: str, base: Path = RECIPES_DIR) -> Dict[str, Any]:
    for r in load_recipes(base):
        if r["id"] == recipe_id:
            return r
    raise RecipeError(f"no recipe {recipe_id!r}")


def expand_steps(recipe: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The recipe's batches in order: the sequence repeated `cycles` times."""
    steps = []
    for c in range(recipe["cycles"]):
        for j, s in enumerate(recipe["sequence"]):
            steps.append({"index": len(steps), "cycle": c, "slot": j, "mode": s["mode"], "planned_targets": list(s["targets"]),
                          "choose": s["choose"], "status": "PENDING"})
    return steps


def per_point_seconds(recipe: Dict[str, Any], mode: str) -> float:
    import survey
    return survey.per_point_seconds({"capture_seconds": recipe["capture_seconds"], "settle_seconds": recipe["settle_seconds"],
                                     "frequency_switching": bool(recipe["modes"][mode]["frequency_switching"])})


def step_seconds(recipe: Dict[str, Any], step: Dict[str, Any], n_points: Optional[int] = None) -> float:
    n = n_points if n_points is not None else (step["choose"] or len(step["planned_targets"]))
    return n * per_point_seconds(recipe, step["mode"]) + BATCH_OVERHEAD_S + REDUCE_S_PER_BATCH


def total_seconds(recipe: Dict[str, Any]) -> float:
    return sum(step_seconds(recipe, s) for s in expand_steps(recipe))


# ================================================================================================== sky geometry

def site_location(observer_config_path: str = "observer_config.json") -> EarthLocation:
    """The site capture.py's own altitude guard uses (observer_config.json)."""
    import observation_plan
    loc, _ = observation_plan._observer_location(observation_plan._load_observer_config(Path(observer_config_path)))
    return loc


def _gal_to_icrs(l: float, b: float) -> Tuple[float, float]:
    c = SkyCoord(l=float(l) % 360.0 * u.deg, b=float(b) * u.deg, frame="galactic").icrs
    return float(c.ra.hour) % 24.0, float(c.dec.deg)


def fixed_position(t: Dict[str, Any]) -> Tuple[float, float]:
    if "l" in t:
        return _gal_to_icrs(t["l"], t["b"])
    return float(t["ra_hours"]) % 24.0, float(t["dec_deg"])


def _icrs_to_gal(ra_h: float, dec: float) -> Tuple[float, float]:
    g = SkyCoord(ra=ra_h * u.hourangle, dec=dec * u.deg).galactic
    return float(g.l.deg), float(g.b.deg)


def _sep(ra1, d1, ra2, d2) -> float:
    a1, b1, a2, b2 = (math.radians(ra1 * 15), math.radians(d1), math.radians(ra2 * 15), math.radians(d2))
    c = math.sin(b1) * math.sin(b2) + math.cos(b1) * math.cos(b2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _gal_sep(l1, b1, l2, b2) -> float:
    a1, c1, a2, c2 = map(math.radians, (l1, b1, l2, b2))
    c = math.sin(c1) * math.sin(c2) + math.cos(c1) * math.cos(c2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def same_altitude_off(recipe: Dict[str, Any], t: Dict[str, Any], when: Time, location: EarthLocation) -> Dict[str, Any]:
    """An OFF at the source's altitude, daz_deg away in azimuth (then 10, 20, 30, 40 deg further out on the same side
    until it clears the recipe's off_rules: distance to the source, |b|, avoided regions such as the other Cloud or
    the Magellanic Bridge). Resolved for `when` (the batch's middle); the mount then tracks that RA/Dec."""
    ref = next(x for x in recipe["targets"] if x["id"] == t["same_alt_as"])
    rra, rdec = fixed_position(ref)
    frame = AltAz(obstime=when, location=location)
    aa = SkyCoord(ra=rra * u.hourangle, dec=rdec * u.deg).transform_to(frame)
    rules = recipe["off_rules"]
    sign = 1.0 if t["daz_deg"] > 0 else -1.0
    tried = []
    for extra in (0, 10, 20, 30, 40):
        daz = sign * (abs(t["daz_deg"]) + extra)
        cand = SkyCoord(alt=aa.alt, az=aa.az + daz * u.deg, frame=frame).icrs
        ra, dec = float(cand.ra.hour) % 24.0, float(cand.dec.deg)
        l, b = _icrs_to_gal(ra, dec)
        why = []
        if _sep(ra, dec, rra, rdec) < rules["min_sep_deg"]:
            why.append(f"{_sep(ra, dec, rra, rdec):.0f} deg from {ref['id']}")
        if abs(b) < rules["min_abs_b_deg"]:
            why.append(f"|b| {abs(b):.0f} deg")
        for a in rules["avoid"]:
            if _gal_sep(l, b, a["l"], a["b"]) < a["r_deg"]:
                why.append(f"inside {a.get('id', 'avoid')}")
        tried.append({"daz_deg": daz, "why": why})
        if not why:
            return {"ok": True, "ra_hours": round(ra, 6), "dec_deg": round(dec, 6), "l": round(l % 360, 2), "b": round(b, 2),
                    "daz_deg": daz, "alt_deg": round(float(aa.alt.deg), 1)}
    return {"ok": False, "reason": "no same-altitude OFF clears the rules: " + "; ".join(
        f"daz {x['daz_deg']:+.0f}: {', '.join(x['why'])}" for x in tried)}


def _altitudes(coords: SkyCoord, location: EarthLocation, times: List[Time]) -> np.ndarray:
    """alt[target, time]"""
    out = np.empty((len(coords), len(times)))
    for j, tm in enumerate(times):
        out[:, j] = coords.transform_to(AltAz(obstime=tm, location=location)).alt.deg
    return out


def _sun(times: List[Time], location: EarthLocation):
    res = []
    for tm in times:
        s = get_sun(tm)
        res.append((SkyCoord(ra=s.ra, dec=s.dec), float(s.transform_to(AltAz(obstime=tm, location=location)).alt.deg)))
    return res


def resolve_step(recipe: Dict[str, Any], step: Dict[str, Any], location: EarthLocation, now: Optional[Time] = None) -> Dict[str, Any]:
    """The pointings of one batch, for a start now: fixed targets as they are, same-altitude OFFs resolved for the
    batch's middle; drops what is below the floor (+margin) at any time of the batch or too near the Sun while it is
    up; `choose` keeps the highest N. Returns {"targets": [...], "dropped": [{"id", "reason"}]}."""
    t0 = now or Time.now()
    dur = step_seconds(recipe, step)
    mid = t0 + timedelta(seconds=dur / 2)
    by_id = {t["id"]: t for t in recipe["targets"]}
    resolved, dropped = [], []
    for tid in step["planned_targets"]:
        t = by_id[tid]
        if "same_alt_as" in t:
            off = same_altitude_off(recipe, t, mid, location)
            if not off["ok"]:
                dropped.append({"id": tid, "reason": off["reason"]})
                continue
            resolved.append({"id": tid, "ra_hours": off["ra_hours"], "dec_deg": off["dec_deg"], "l": off["l"], "b": off["b"],
                             "daz_deg": off["daz_deg"]})
        else:
            ra, dec = fixed_position(t)
            l, b = _icrs_to_gal(ra, dec)
            resolved.append({"id": tid, "ra_hours": round(ra, 6), "dec_deg": round(dec, 6), "l": round(l % 360, 2), "b": round(b, 2)})
    if not resolved:
        return {"targets": [], "dropped": dropped}
    times = [t0, mid, t0 + timedelta(seconds=dur)]
    coords = SkyCoord(ra=[x["ra_hours"] * 15 for x in resolved] * u.deg, dec=[x["dec_deg"] for x in resolved] * u.deg)
    alts = _altitudes(coords, location, times)
    sun = _sun(times, location)
    sun_up = max(s[1] for s in sun) > -2.0
    sun_sep = coords.separation(sun[0][0]).deg
    floor = recipe["min_altitude_deg"] + ALT_MARGIN_DEG
    keep = []
    for i, x in enumerate(resolved):
        x["alt_now"] = round(float(alts[i, 0]), 1)
        if alts[i].min() < floor:
            dropped.append({"id": x["id"], "reason": f"below {floor:.0f} deg during the batch (lowest {alts[i].min():.0f})"})
        elif sun_up and sun_sep[i] < recipe["sun_avoid_deg"]:
            dropped.append({"id": x["id"], "reason": f"{sun_sep[i]:.0f} deg from the Sun"})
        else:
            keep.append(x)
    if step.get("choose"):
        keep.sort(key=lambda x: -x["alt_now"])
        for x in keep[step["choose"]:]:
            dropped.append({"id": x["id"], "reason": "not chosen (a higher one was)"})
        keep = keep[: step["choose"]]
    return {"targets": keep, "dropped": dropped}


def readiness(recipe: Dict[str, Any], location: EarthLocation, now: Optional[Time] = None) -> Dict[str, Any]:
    """Can the recipe run if it starts now - and if not, the first start within 24 h when it can. The run is
    simulated batch by batch on a 10-min grid: a batch's FIXED targets must be above the floor (+margin) during that
    batch and away from the Sun while the Sun is up (same-altitude OFFs follow their source; a `choose` batch needs
    its N). Coverage = observable points / planned points must reach the recipe's min_coverage (1.0 unless the
    recipe says otherwise). READY / LATER / NOT_TODAY."""
    t0 = now or Time.now()
    steps = expand_steps(recipe)
    D = sum(step_seconds(recipe, s) for s in steps)
    need = float(recipe.get("min_coverage", 1.0))
    fixed = [t for t in recipe["targets"] if "same_alt_as" not in t]
    fixed_ids = [t["id"] for t in fixed]
    alias = {t["id"]: (t["same_alt_as"] if "same_alt_as" in t else t["id"]) for t in recipe["targets"]}
    pos = [fixed_position(t) for t in fixed]
    coords = SkyCoord(ra=[p[0] * 15 for p in pos] * u.deg, dec=[p[1] for p in pos] * u.deg)
    dt = READY_STEP_MIN * 60.0
    n = int(READY_SCAN_HOURS * 3600 / dt) + 1
    span = int(math.ceil(D / dt)) + 1
    times = [t0 + timedelta(seconds=dt * k) for k in range(n + span)]
    alts = _altitudes(coords, location, times)
    floor = recipe["min_altitude_deg"] + ALT_MARGIN_DEG
    good = alts >= floor
    for j, (sc, salt) in enumerate(_sun(times, location)):
        if salt > -2.0:
            good[:, j] &= coords.separation(sc).deg >= recipe["sun_avoid_deg"]
    offs, acc = [], 0.0
    for s in steps:
        d = step_seconds(recipe, s)
        offs.append((int(acc // dt), int(math.ceil((acc + d) / dt))))
        acc += d
    planned = sum(s["choose"] or len(s["planned_targets"]) for s in steps)

    def coverage(k: int) -> float:
        seen = 0
        for s, (a, b) in zip(steps, offs):
            ok = [good[fixed_ids.index(alias[x]), k + a: k + b + 1].all() for x in s["planned_targets"]]
            seen += min(sum(ok), s["choose"]) if s["choose"] else sum(ok)
        return seen / planned if planned else 0.0

    covs = [coverage(k) for k in range(n)]
    base = {"duration_s": round(D), "min_coverage": need,
            "alt_now": {fixed_ids[i]: round(float(alts[i, 0]), 1) for i in range(len(fixed_ids))}}
    for k in range(n):
        if covs[k] >= need - 1e-9:
            last = k
            while last + 1 < n and covs[last + 1] >= need - 1e-9:
                last += 1
            return {**base, "state": "READY" if k == 0 else "LATER", "start_utc": times[k].utc.isot[:19] + "Z",
                    "until_utc": times[last].utc.isot[:19] + "Z", "coverage": round(covs[k], 2),
                    "coverage_now": round(covs[0], 2)}
    best = int(np.argmax(covs))
    return {**base, "state": "NOT_TODAY", "coverage_now": round(covs[0], 2), "best_coverage": round(covs[best], 2),
            "best_start_utc": times[best].utc.isot[:19] + "Z",
            "reason": f"no start in the next {READY_SCAN_HOURS:.0f} h sees {need:.0%} of the planned points above "
                      f"{floor:.0f} deg (best {covs[best]:.0%})"}


# ================================================================================================== OBSERVE spec

def batch_spec(recipe: Dict[str, Any], run_id: str, step: Dict[str, Any], targets: List[Dict[str, Any]], gain_db: float) -> Dict[str, Any]:
    """The TARGET_LIST spec OBSERVE plans for one batch (observation_plan.plan_target_list), in this step's mode."""
    import sdr_tuning
    op = sdr_tuning.operating_config()
    m = recipe["modes"][step["mode"]]
    capture: Dict[str, Any] = {"seconds": recipe["capture_seconds"], "settle_seconds": recipe["settle_seconds"],
                               "frequency_switching": bool(m["frequency_switching"])}
    if m["frequency_switching"]:
        if m.get("offset_hz") is not None:
            capture["frequency_switching_offset_hz"] = m["offset_hz"]
        if m.get("center") is not None:
            capture["frequency_switching_center"] = m["center"]
    return {
        "session": {"name": f"ALMITA-EXP-{recipe['id']}-{step['mode']}"[:64]},
        "grid": {"mode": "TARGET_LIST", "min_altitude_deg": recipe["min_altitude_deg"], "nominal_spacing_deg": POINT_MATCH_DEG * 2,
                 "targets": [{"ra_hours": t["ra_hours"], "dec_deg": t["dec_deg"], "label": t["id"]} for t in targets]},
        "capture": capture,
        "main": {"center_frequency_hz": op["center_frequency_hz"], "sample_rate": op["sample_rate_hz"],
                 "gain_db": float(gain_db), "bias_tee": True},
        "rfi_ref": {"enabled": False},
        "quicklook": {"enabled": False},
        "console": {"enabled": True},
        "execution": {"unattended": True},
    }


def match_points(step: Dict[str, Any], points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Give every reduced point the id of the batch target it belongs to (nearest, within POINT_MATCH_DEG)."""
    out = []
    for p in points:
        best, bs = None, None
        for t in step.get("targets") or []:
            s = _sep(t["ra_hours"], t["dec_deg"], p["ra_hours"], p["dec_deg"])
            if bs is None or s < bs:
                best, bs = t, s
        out.append({**p, "target_id": best["id"] if best is not None and bs <= POINT_MATCH_DEG else None})
    return out


# ================================================================================================== runs

def runs_dir(base: Path = RUNS_BASE) -> Path:
    return base / "runs"


def _write_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def new_run(recipe: Dict[str, Any], *, open_sky_confirmed: bool, note: str = "", replay_of: Optional[str] = None,
            now: Optional[datetime] = None) -> Dict[str, Any]:
    t = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    run_id = f"{recipe['id']}-{t.strftime('%Y%m%d-%H%M%S')}"
    return {"schema": 1, "run_id": run_id, "recipe_id": recipe["id"], "recipe": recipe, "created_utc": utcnow(),
            "open_sky_confirmed": bool(open_sky_confirmed), "note": str(note)[:300], "replay_of": replay_of,
            "status": "REPLAY" if replay_of else "PENDING", "steps": [] if replay_of else expand_steps(recipe),
            "pilot": {"state": "IDLE", "note": "created", "updated_utc": utcnow()}, "analysis": None}


def save_run(run: Dict[str, Any], base: Path = RUNS_BASE) -> None:
    if not re.match(r"^[A-Za-z0-9_-]{1,80}$", run["run_id"]):
        raise RecipeError("bad run id")
    _write_atomic(runs_dir(base) / run["run_id"] / "run.json", run)


def load_run(run_id: str, base: Path = RUNS_BASE) -> Dict[str, Any]:
    if not re.match(r"^[A-Za-z0-9_-]{1,80}$", run_id or ""):
        raise RecipeError("bad run id")
    return json.loads((runs_dir(base) / run_id / "run.json").read_text(encoding="utf-8"))


def list_runs(base: Path = RUNS_BASE) -> List[Dict[str, Any]]:
    out = []
    d = runs_dir(base)
    for f in sorted(d.glob("*/run.json")) if d.is_dir() else []:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        v = (r.get("analysis") or {}).get("verdict") or {}
        done = sum(1 for s in r.get("steps") or [] if s.get("status") in ("DONE", "SKIPPED"))
        out.append({"run_id": r["run_id"], "recipe_id": r["recipe_id"], "name": r["recipe"]["name"], "created_utc": r["created_utc"],
                    "status": r["status"], "replay_of": r.get("replay_of"), "steps_done": done, "steps": len(r.get("steps") or []),
                    "verdict": {"code": v.get("code"), "title": v.get("title")} if v else None})
    out.sort(key=lambda x: x["created_utc"], reverse=True)
    return out


def set_active(run_id: Optional[str], base: Path = RUNS_BASE) -> None:
    _write_atomic(base / ACTIVE_NAME, {"run_id": run_id, "set_utc": utcnow()})


def active_id(base: Path = RUNS_BASE) -> Optional[str]:
    try:
        return json.loads((base / ACTIVE_NAME).read_text(encoding="utf-8")).get("run_id")
    except (OSError, ValueError):
        return None


def set_pilot(run: Dict[str, Any], state: str, note: str, **extra: Any) -> None:
    run["pilot"] = {"state": state, "note": str(note)[:500], "updated_utc": utcnow(), **extra}


# ================================================================================================== spectra

def load_spectrum(output_dir: str, point_index: int) -> Optional[Dict[str, Any]]:
    """One REDUCE master spectrum: velocity (km/s LSRK), relative intensity in %, masked channels as NaN."""
    import h5py
    base = Path(output_dir)
    if not base.is_absolute():
        base = ROOT / base
    js = base / "points" / str(point_index) / "master_spectrum.json"
    if not js.exists():
        hits = [p for p in (base / "points").glob("*/master_spectrum.json") if json.loads(p.read_text()).get("point_index") == point_index]
        if not hits:
            return None
        js = hits[0]
    meta = json.loads(js.read_text())
    try:
        with h5py.File(js.with_suffix(".h5"), "r") as f:
            v = f["velocity_lsrk_m_s"][:] / 1e3
            y = f["relative_intensity"][:].astype(float) * 100.0
            m = f["mask"][:]
    except (OSError, KeyError):
        return None
    y[(m != 0) | ~np.isfinite(y) | ~np.isfinite(v)] = np.nan
    o = np.argsort(v)
    q = (meta.get("quality") or {}).get("metrics") or {}
    return {"v": v[o], "y": y[o], "utc": meta.get("timestamp_start_utc"), "ra_hours": meta.get("ra_hours"),
            "dec_deg": meta.get("dec_degrees"), "offset_hz": q.get("fswitch_offset_hz")}


def on_grid(spec: Dict[str, Any], grid: np.ndarray) -> np.ndarray:
    v, y = spec["v"], spec["y"]
    ok = np.isfinite(y)
    if ok.sum() < 10:
        return np.full(grid.shape, np.nan)
    out = np.interp(grid, v[ok], y[ok], left=np.nan, right=np.nan)
    # do not bridge masked stretches wider than 2 km/s
    gaps = np.interp(grid, v, (~ok).astype(float), left=1.0, right=1.0)
    out[gaps > 0.5] = np.nan
    return out


def collect(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every accepted reduced point of the run with its spectrum: [{mode, target_id, cycle, step, spec}]."""
    out = []
    for s in run.get("steps") or []:
        if s.get("status") != "DONE" or not s.get("output_dir"):
            continue
        for p in s.get("points") or []:
            if not p.get("accepted") or not p.get("target_id"):
                continue
            sp = load_spectrum(s["output_dir"], int(p["point_index"]))
            if sp is not None:
                out.append({"mode": s["mode"], "target_id": p["target_id"], "cycle": s.get("cycle", 0), "step": s["index"], "spec": sp})
    return out


GRID = np.arange(-400.0, 500.0001, 0.5)


def _series(name: str, grid: np.ndarray, y: np.ndarray, step_km_s: float = 1.0, smooth_km_s: float = 3.0) -> Dict[str, Any]:
    """A plot series for the page: smoothed, resampled, NaN-trimmed."""
    k = max(1, int(round(smooth_km_s / 0.5)))
    good = np.isfinite(y)
    num = np.convolve(np.where(good, y, 0.0), np.ones(k), "same")
    den = np.convolve(good.astype(float), np.ones(k), "same")
    ys = np.where(den > 0.5 * k, num / np.where(den > 0, den, 1), np.nan)
    stride = max(1, int(round(step_km_s / 0.5)))
    xs, vs = grid[::stride], ys[::stride]
    ok = np.isfinite(vs)
    if not ok.any():
        return {"name": name, "x": [], "y": []}
    i0, i1 = np.argmax(ok), len(ok) - np.argmax(ok[::-1])
    return {"name": name, "x": [round(float(x), 1) for x in xs[i0:i1]],
            "y": [round(float(x), 3) if math.isfinite(x) else None for x in vs[i0:i1]]}


# ================================================================================================== velocity

def fit_lag(grid: np.ndarray, ref: np.ndarray, y: np.ndarray, *, max_lag: float = 15.0, step: float = 0.05,
            line_fraction: float = 0.05, pad_km_s: float = 20.0) -> Dict[str, Any]:
    """Shift of `y` against `ref` by fitting the WHOLE profile: y(v) ~ s * ref(v - d) + c0 + c1 v, d on a fine grid,
    s/c0/c1 by least squares, over the line region of `ref` (>= line_fraction of its peak, +/- pad) where both exist.
    d > 0 = y sits at higher velocity. Never the position of a maximum (2026-11-21 lesson)."""
    fr = np.isfinite(ref)
    if fr.sum() < 40:
        return {"ok": False, "reason": "reference too short"}
    peak = np.nanmax(ref)
    if not peak > 0:
        return {"ok": False, "reason": "no line in the reference"}
    line = fr & (ref >= line_fraction * peak)
    if line.sum() < 10:
        return {"ok": False, "reason": "no line in the reference"}
    lo, hi = grid[line].min() - pad_km_s, grid[line].max() + pad_km_s
    region = (grid >= lo) & (grid <= hi) & np.isfinite(y)
    vr, rr = grid[fr], ref[fr]
    lags = np.arange(-max_lag, max_lag + step / 2, step)
    rss = np.full(lags.shape, np.inf)
    coefs = [None] * len(lags)
    for i, d in enumerate(lags):
        sh = np.interp(grid - d, vr, rr, left=np.nan, right=np.nan)
        ok = region & np.isfinite(sh)
        if ok.sum() < 40:
            continue
        X = np.c_[sh[ok], np.ones(ok.sum()), grid[ok] / 100.0]
        co, *_ = np.linalg.lstsq(X, y[ok], rcond=None)
        rss[i] = float(((X @ co - y[ok]) ** 2).sum() / ok.sum())
        coefs[i] = co
    if not np.isfinite(rss).any():
        return {"ok": False, "reason": "the two spectra do not overlap on the line"}
    i = int(np.nanargmin(rss))
    d = float(lags[i])
    if 0 < i < len(lags) - 1 and np.isfinite(rss[i - 1]) and np.isfinite(rss[i + 1]):
        den = rss[i - 1] - 2 * rss[i] + rss[i + 1]
        if den > 0:
            d += 0.5 * step * (rss[i - 1] - rss[i + 1]) / den
    at_edge = i in (0, len(lags) - 1)
    ok = region & np.isfinite(np.interp(grid - d, vr, rr, left=np.nan, right=np.nan))
    return {"ok": not at_edge and coefs[i] is not None and coefs[i][0] > 0, "lag_km_s": d, "scale": float(coefs[i][0]) if coefs[i] is not None else None,
            "fit_from_km_s": float(grid[ok].min()) if ok.any() else None, "fit_to_km_s": float(grid[ok].max()) if ok.any() else None,
            "reason": "shift at the edge of the search" if at_edge else ("negative scale" if coefs[i] is not None and coefs[i][0] <= 0 else None)}


def _mean(arrs: List[np.ndarray]) -> np.ndarray:
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return np.nanmean(np.vstack(arrs), axis=0)


def reference_spectrum(ref: Optional[Dict[str, Any]], target: Dict[str, Any]) -> Optional[Tuple[np.ndarray, str]]:
    """An external ALMITA reference (e.g. the 12-11 REDUCE, whose whole-profile shift against HI4PI is +0.06 km/s):
    the reduced point nearest the target, if within 3 deg."""
    if not ref or not ref.get("reduce_dir"):
        return None
    base = ROOT / ref["reduce_dir"]
    ra, dec = fixed_position(target) if "same_alt_as" not in target else (None, None)
    if ra is None:
        return None
    best, bs = None, None
    for js in (base / "points").glob("*/master_spectrum.json"):
        m = json.loads(js.read_text())
        s = _sep(m["ra_hours"], m["dec_degrees"], ra, dec)
        if bs is None or s < bs:
            best, bs = m, s
    if best is None or bs > 3.0:
        return None
    sp = load_spectrum(str(base), int(best["point_index"]))
    return (on_grid(sp, GRID), f"{ref.get('label', base.name)} point {best['point_index']} ({bs:.1f} deg away)") if sp else None


def analyse_velocity(run: Dict[str, Any], pts: List[Dict[str, Any]]) -> Dict[str, Any]:
    rc = run["recipe"]
    a = rc["analysis"]
    refm, tol, minp = a["reference_mode"], float(a["tolerance_km_s"]), int(a["min_passes"])
    modes = list(rc["modes"])
    by_t: Dict[str, Dict[str, List[np.ndarray]]] = {}
    for p in pts:
        by_t.setdefault(p["target_id"], {}).setdefault(p["mode"], []).append(on_grid(p["spec"], GRID))
    targets, plots = [], []
    mode_lags: Dict[str, List[Tuple[float, float, int]]] = {}
    for tid, per in by_t.items():
        row: Dict[str, Any] = {"target_id": tid, "passes": {m: len(per.get(m, [])) for m in modes}, "modes": {}}
        series = []
        if per.get(refm):
            A = _mean(per[refm])
            for m in modes:
                if not per.get(m):
                    continue
                M = _mean(per[m])
                series.append(_series(f"{m} · {rc['modes'][m]['label']} (n={len(per[m])})", GRID, M))
                if m == refm:
                    # the reference mode's own scatter: each pass against the mean of the others
                    ls = []
                    for i in range(len(per[m])):
                        others = [x for j, x in enumerate(per[m]) if j != i]
                        if others:
                            f = fit_lag(GRID, _mean(others), per[m][i])
                            if f["ok"]:
                                ls.append(f["lag_km_s"])
                    sd = float(np.std(ls, ddof=1)) if len(ls) >= 2 else None
                    row["modes"][m] = {"lag_km_s": 0.0, "pass_lags": [round(x, 2) for x in ls], "pass_sd_km_s": _r(sd)}
                    continue
                f = fit_lag(GRID, A, M)
                ls = [g["lag_km_s"] for g in (fit_lag(GRID, A, x) for x in per[m]) if g["ok"]]
                sd = float(np.std(ls, ddof=1)) if len(ls) >= 2 else None
                sem = sd / math.sqrt(len(ls)) if sd is not None else None
                row["modes"][m] = {"lag_km_s": _r(f.get("lag_km_s")) if f["ok"] else None, "fit_ok": f["ok"], "reason": f.get("reason"),
                                   "fit_from_km_s": _r(f.get("fit_from_km_s"), 0), "fit_to_km_s": _r(f.get("fit_to_km_s"), 0),
                                   "pass_lags": [round(x, 2) for x in ls], "pass_sd_km_s": _r(sd), "sem_km_s": _r(sem)}
                if f["ok"]:
                    mode_lags.setdefault(m, []).append((f["lag_km_s"], sem if sem is not None else float("nan"), len(per[m])))
            ext = reference_spectrum(a.get("reference_reduce"), next(t for t in rc["targets"] if t["id"] == tid))
            if ext is not None:
                E, label = ext
                row["external"] = {"label": label, "lags_km_s": {}}
                series.append(_series(f"REF · {label}", GRID, E))
                for m in modes:
                    if per.get(m):
                        g = fit_lag(GRID, E, _mean(per[m]))
                        row["external"]["lags_km_s"][m] = _r(g["lag_km_s"]) if g["ok"] else None
        targets.append(row)
        plots.append({"title": f"{tid}: each mode's mean spectrum", "x_label": "velocity LSRK (km/s)", "y_label": "% over Tsys",
                      "series": series, "xlim": [-120, 200]})
    # verdict per mode
    per_mode = {}
    for m in modes:
        if m == refm:
            continue
        rows = mode_lags.get(m) or []
        npass = sum(r[2] for r in rows)
        if not rows or npass < minp or not by_t or min(len(per.get(refm, [])) for per in by_t.values()) < minp:
            per_mode[m] = {"code": "INCONCLUSIVE", "why": f"needs >= {minp} passes in {refm} and {m}"}
            continue
        lag = float(np.mean([r[0] for r in rows]))
        sems = [r[1] for r in rows if math.isfinite(r[1])]
        sem = float(math.sqrt(sum(s * s for s in sems)) / len(sems)) if sems else float("nan")
        if abs(lag) > tol and (not math.isfinite(sem) or abs(lag) > 3 * sem):
            code = "SHIFTED"
        elif abs(lag) <= tol and (math.isfinite(sem) and 2 * sem <= tol):
            code = "SAME"
        else:
            code = "INCONCLUSIVE"
        per_mode[m] = {"code": code, "lag_km_s": _r(lag), "sem_km_s": _r(sem), "passes": npass}
    verdict = _velocity_story(rc, refm, per_mode, tol)
    # the same mode jumping between passes = the shift changes with TIME (19-11: B1-B3 -8.3, B4-B5 -1 km/s, same mode,
    # smooth tuning) - the alternation is what exposes it; say so instead of averaging it away
    jumps = []
    for row in targets:
        for m, mv in row["modes"].items():
            sd = mv.get("pass_sd_km_s")
            if sd is not None and sd > 2 * tol:
                jumps.append(f"{row['target_id']} {m}: passes {', '.join(f'{x:+.1f}' for x in mv['pass_lags'])} (sd {sd:.1f})")
    if jumps:
        verdict["warnings"] = ["Passes of the same mode jump by more than ±%g km/s: the shift changes with time, "
                               "not only with the mode. " % (2 * tol) + "; ".join(jumps)]
    return {"kind": "AB_VELOCITY", "reference_mode": refm, "tolerance_km_s": tol, "targets": targets, "per_mode": per_mode,
            "verdict": verdict, "plots": plots}


def _velocity_story(rc: Dict[str, Any], refm: str, per_mode: Dict[str, Any], tol: float) -> Dict[str, Any]:
    lab = {m: rc["modes"][m]["label"] for m in rc["modes"]}
    if not per_mode or all(v["code"] == "INCONCLUSIVE" for v in per_mode.values()):
        return {"code": "INCONCLUSIVE", "title": "No verdict yet",
                "text": "Some mode is missing passes: " + "; ".join(f"{m}: {v.get('why', v['code'])}" for m, v in per_mode.items())}
    bits = []
    for m, v in per_mode.items():
        if v["code"] == "INCONCLUSIVE" and v.get("lag_km_s") is None:
            bits.append(f"{m} ({lab[m]}): {v.get('why')}")
        else:
            bits.append(f"{m} ({lab[m]}) − {refm}: {v['lag_km_s']:+.2f} ± {v['sem_km_s'] if v['sem_km_s'] is not None else float('nan'):.2f} km/s → {v['code']}")
    shifted = [m for m, v in per_mode.items() if v["code"] == "SHIFTED"]
    same = [m for m, v in per_mode.items() if v["code"] == "SAME"]
    if shifted and not [m for m, v in per_mode.items() if v["code"] == "INCONCLUSIVE"]:
        if same:
            title = f"Mode {', '.join(shifted)} shifts the axis; {', '.join(same)} does not"
            text = (f"The shift follows {', '.join(lab[m] for m in shifted)} and does not appear in {', '.join(lab[m] for m in same)}: "
                    "it lies in what tells those modes apart (throw or centring), not in the night or the instrument.")
        else:
            title = "Every mode is shifted from the reference"
            text = "Each mode differs from the reference mode: check the reference itself against HI4PI."
        code = "MODE_SHIFT"
    elif same and len(same) == len(per_mode):
        title = "The modes agree"
        text = (f"Every mode agrees with {refm} within ±{tol:g} km/s: the 19-11 shift is not made by the switching "
                "mode (it was the night or something common to all; see the comparison with the external reference).")
        code = "MODES_AGREE"
    else:
        title = "Mixed result"
        text = "Some mode is still undecided: more passes."
        code = "INCONCLUSIVE"
    return {"code": code, "title": title, "text": text + " " + " · ".join(bits)}


# ================================================================================================== ON/OFF

def _window_mean(spec: Dict[str, Any], w0: float, w1: float) -> Optional[float]:
    v, y = spec["v"], spec["y"]
    sel = (v >= w0) & (v <= w1)
    if sel.sum() < 5 or np.isfinite(y[sel]).mean() < 0.5:
        return None
    return float(np.nanmean(y[sel]))


def analyse_onoff(run: Dict[str, Any], pts: List[Dict[str, Any]]) -> Dict[str, Any]:
    rc = run["recipe"]
    a = rc["analysis"]
    on, (w0, w1) = a["on"], a["window_km_s"]
    exp_lo, exp_hi = a["expected_pct"]
    cycles: Dict[Any, Dict[str, List[float]]] = {}
    spectra: Dict[str, List[np.ndarray]] = {}
    for p in pts:
        role = "ON" if p["target_id"] == on else "OFF"
        val = _window_mean(p["spec"], w0, w1)
        key = (p.get("cycle"), p.get("step")) if run.get("replay_of") else p.get("cycle")
        c = cycles.setdefault(key, {"ON": [], "OFF": [], "off_ids": []})
        if val is not None:
            c[role].append(val)
            if role == "OFF":
                c["off_ids"].append(p["target_id"])
        spectra.setdefault(role if role == "ON" else f"OFF {p['target_id']}", []).append(on_grid(p["spec"], GRID))
    pairs = []
    for k, c in sorted(cycles.items(), key=lambda kv: str(kv[0])):
        if c["ON"] and c["OFF"]:
            pairs.append({"cycle": k if not isinstance(k, tuple) else k[0], "on_pct": round(float(np.mean(c["ON"])), 3),
                          "off_pct": round(float(np.mean(c["OFF"])), 3), "diff_pct": round(float(np.mean(c["ON"]) - np.mean(c["OFF"])), 3),
                          "off_ids": sorted(set(c["off_ids"]))})
    off_all = [x for c in cycles.values() for x in c["OFF"]]
    on_all = [x for c in cycles.values() for x in c["ON"]]
    n = len(pairs)
    res: Dict[str, Any] = {"kind": "ONOFF", "on": on, "window_km_s": [w0, w1], "expected_pct": [exp_lo, exp_hi], "pairs": pairs,
                           "on_mean_pct": _r(np.mean(on_all), 3) if on_all else None, "off_mean_pct": _r(np.mean(off_all), 3) if off_all else None}
    d = np.array([p["diff_pct"] for p in pairs])
    if n:
        res["diff_mean_pct"] = _r(d.mean(), 3)
        res["diff_sem_pct"] = _r(d.std(ddof=1) / math.sqrt(n), 3) if n >= 2 else None
        res["positive_fraction"] = _r((d > 0).mean(), 2)
    if n < int(a["min_pairs"]) or n < 2:
        v = {"code": "INCONCLUSIVE", "title": "Too few ON/OFF pairs",
             "text": f"{n} complete pair(s); the recipe needs at least {a['min_pairs']} to decide."}
    else:
        m, e = float(d.mean()), float(d.std(ddof=1) / math.sqrt(n))
        off_ok = abs(float(np.mean(off_all))) <= a["max_off_abs_pct"] if off_all else False
        need = int(math.ceil(n * (3 * e / exp_lo) ** 2)) if e > 0 else n
        if m >= 3 * e and m >= 0.5 * exp_lo and (d > 0).mean() >= 0.75 and off_ok:
            v = {"code": "DETECTED", "title": f"DETECTED: ON − OFF = {m:+.2f} ± {e:.2f} %",
                 "text": f"{(d > 0).sum()}/{n} pairs positive, {m / e:.1f}σ, expected {exp_lo:g}–{exp_hi:g} %, OFFs at {np.mean(off_all):+.2f} %."}
        elif m <= -3 * e:
            v = {"code": "BASELINE", "title": f"NOT DETECTED: ON − OFF negative and repeatable ({m:+.2f} ± {e:.2f} %)",
                 "text": "An ON below its OFFs, pass after pass, is not gas: it is the switching baseline toward the source "
                         "(the 19-11 lesson). With this result the recipe cannot see the expected signal "
                         f"({exp_lo:g}–{exp_hi:g} %)."}
        elif m + 2 * e < exp_lo:
            v = {"code": "NOT_DETECTED", "title": f"NOT DETECTED: ON − OFF < {m + 2 * e:.2f} % (2σ)",
                 "text": f"The upper limit is below the expected signal ({exp_lo:g}–{exp_hi:g} %): at this noise the source would have been seen."}
        else:
            v = {"code": "INCONCLUSIVE", "title": f"Undecided: ON − OFF = {m:+.2f} ± {e:.2f} %",
                 "text": f"The noise per pair is not yet low enough for the expected signal ({exp_lo:g} %): about {need} pairs "
                         f"in total are needed for 3σ (there are {n})." + ("" if off_ok else f" Note: the OFFs average {np.mean(off_all):+.2f} %, "
                                                                      f"above the ±{a['max_off_abs_pct']:g} % maximum.")}
    res["verdict"] = v
    series = [_series(f"{k} (n={len(vv)})", GRID, _mean(vv)) for k, vv in sorted(spectra.items(), key=lambda kv: kv[0] != "ON")]
    res["plots"] = [{"title": "Mean spectra (the window is shaded)", "x_label": "velocity LSRK (km/s)", "y_label": "% over Tsys",
                     "series": series, "band": [w0, w1], "xlim": [w0 - 150, w1 + 150]},
                    {"title": "ON − OFF per pair, in the window", "x_label": "pair", "y_label": "%", "kind": "bars",
                     "series": [{"name": "ON − OFF", "x": list(range(1, n + 1)), "y": [p["diff_pct"] for p in pairs]}],
                     "band_y": [exp_lo, exp_hi]}]
    return res


# ================================================================================================== tangent / line check

def analyse_tangent(run: Dict[str, Any], pts: List[Dict[str, Any]]) -> Dict[str, Any]:
    import rotation_curve as rcurve
    rc = run["recipe"]
    a = rc["analysis"]
    by_t: Dict[str, List[np.ndarray]] = {}
    for p in pts:
        by_t.setdefault(p["target_id"], []).append(on_grid(p["spec"], GRID))
    rows, series = [], []
    for tid, arrs in by_t.items():
        t = next(x for x in rc["targets"] if x["id"] == tid)
        ra, dec = fixed_position(t)
        l, b = _icrs_to_gal(ra, dec)
        M = _mean(arrs)
        series.append(_series(f"{tid} (n={len(arrs)})", GRID, M))
        sign = rcurve.quadrant_sign(l)
        row = {"target_id": tid, "l_deg": round(l % 360, 1), "b_deg": round(b, 1), "passes": len(arrs)}
        if sign == 0:
            row.update(used=False, reason="not quadrant I/IV")
        else:
            ok = np.isfinite(M)
            e = rcurve.terminal_velocity(GRID[ok], M[ok], sign, edge_fraction=a["edge_fraction"], nsigma=a["nsigma"])
            if e.get("ok"):
                tp = rcurve.tangent_point(l, e["terminal_km_s"], e["terminal_err_km_s"])
                row.update(used=True, terminal_km_s=_r(e["terminal_km_s"], 1), terminal_err_km_s=_r(e["terminal_err_km_s"], 1),
                           R_kpc=_r(tp["R_kpc"]), V_km_s=_r(tp["V_km_s"], 1))
            else:
                row.update(used=False, reason=e.get("reason"))
        rows.append(row)
    used = [r for r in rows if r.get("used")]
    if len(used) >= int(a["min_points"]):
        med = float(np.median([r["V_km_s"] for r in used]))
        v = {"code": "MEASURED", "title": f"Rotation curve: {len(used)} tangent points, median {med:.0f} km/s",
             "text": "V(R) = |v_t| + V0·|sin l| with R0 8.2 kpc and V0 220 km/s; the edge is a fraction of the spectrum's own peak "
                     f"({a['edge_fraction']:g}) and the 20° beam lowers it somewhat (see rotation_curve.py). " +
                     " · ".join(f"l={r['l_deg']:g}: v_t {r['terminal_km_s']:+.0f}, R {r['R_kpc']:.1f} kpc, V {r['V_km_s']:.0f}" for r in used)}
    else:
        v = {"code": "INCONCLUSIVE", "title": f"{len(used)} point(s) with a terminal edge; the recipe needs {a['min_points']}",
             "text": " · ".join(f"{r['target_id']}: {r.get('reason')}" for r in rows if not r.get("used"))}
    plots = [{"title": "Mean spectrum per longitude", "x_label": "velocity LSRK (km/s)", "y_label": "% over Tsys", "series": series,
              "xlim": [-200, 200]}]
    if used:
        plots.append({"title": "Rotation curve (tangent points)", "x_label": "R (kpc)", "y_label": "V (km/s)", "kind": "points",
                      "series": [{"name": "ALMITA", "x": [r["R_kpc"] for r in used], "y": [r["V_km_s"] for r in used]}],
                      "hline": 220})
    return {"kind": "TANGENT", "points": rows, "verdict": v, "plots": plots}


def analyse_line_check(run: Dict[str, Any], pts: List[Dict[str, Any]]) -> Dict[str, Any]:
    a = run["recipe"]["analysis"]
    if not pts:
        return {"kind": "LINE_CHECK", "verdict": {"code": "NO_DATA", "title": "No accepted spectra",
                                                  "text": "REDUCE accepted no point: SDR mute, mount, RFI?"}, "plots": []}
    M = _mean([on_grid(p["spec"], GRID) for p in pts])
    ok = np.isfinite(M)
    k = 8
    sm = np.convolve(np.where(ok, M, 0), np.ones(k) / k, "same")
    sm[~ok] = np.nan
    # noise on the NATIVE channels of each spectrum (the 0.5 km/s grid interpolates them and would hide it), for the
    # mean of the N spectra smoothed over k grid points = 4 km/s
    sig2, widths = [], []
    for p in pts:
        y = p["spec"]["y"][np.isfinite(p["spec"]["y"])]
        if y.size > 20:
            d = np.diff(y)
            sig2.append((1.4826 * float(np.median(np.abs(d - np.median(d)))) / math.sqrt(2)) ** 2)
            widths.append(float(np.median(np.abs(np.diff(p["spec"]["v"])))))
    k_native = max(1.0, (k * 0.5) / (float(np.median(widths)) if widths else 1.0))
    noise = math.sqrt(sum(sig2) / len(sig2) ** 2 / k_native) if sig2 else float("nan")
    core = ok & (GRID > -150) & (GRID < 200)
    i = int(np.nanargmax(np.where(core, sm, -np.inf)))
    peak, vpk = float(sm[i]), float(GRID[i])
    snr = peak / noise if noise > 0 else float("inf")
    if snr >= a["snr_ok"]:
        v = {"code": "HI_OK", "title": f"Hydrogen in sight: peak {peak:.1f} % at {vpk:+.0f} km/s (S/N {snr:.0f})",
             "text": "The chain antenna → LNA → SDR → REDUCE works. The long experiments can go ahead."}
    elif snr >= a["snr_weak"]:
        v = {"code": "HI_WEAK", "title": f"Weak line: S/N {snr:.0f}", "text": "Something shows, but less than usual: check the LNA, the feed connector and the temperatures."}
    else:
        v = {"code": "NO_LINE", "title": f"No line (S/N {snr:.1f})", "text": "No hydrogen where there always is: SDR mute, LNA off or the feed. Do not go on with the long ones."}
    return {"kind": "LINE_CHECK", "peak_pct": _r(peak), "peak_km_s": _r(vpk, 1), "noise_pct": _r(noise, 3), "snr": _r(snr, 1),
            "verdict": v, "plots": [{"title": "Mean spectrum", "x_label": "velocity LSRK (km/s)", "y_label": "% over Tsys",
                                     "series": [_series(f"{len(pts)} pass(es)", GRID, M)], "xlim": [-200, 250]}]}


ANALYSES = {"AB_VELOCITY": analyse_velocity, "ONOFF": analyse_onoff, "TANGENT": analyse_tangent, "LINE_CHECK": analyse_line_check}


def analyse(run: Dict[str, Any]) -> Dict[str, Any]:
    pts = collect(run)
    res = ANALYSES[run["recipe"]["kind"]](run, pts)
    res.update({"analysed_utc": utcnow(), "spectra_used": len(pts),
                "caveat": None if run.get("open_sky_confirmed") or run.get("replay_of") else
                "the operator did not confirm open sky: an instrument check, not a sky result"})
    return res


# ================================================================================================== replay

def replay_from_survey(recipe: Dict[str, Any], survey_state: Dict[str, Any], *, match_deg: float = 6.0) -> Dict[str, Any]:
    """Build a REPLAY run from the batches a SURVEY already captured and reduced (e.g. the 19-11 SMC ON/OFF) so the
    recipe's analysis and verdict can be tried on real data. Points map to the recipe's fixed targets by position;
    for ON/OFF any other survey tile is an OFF; the mode is the recipe mode with the same switching settings."""
    run = new_run(recipe, open_sky_confirmed=True, replay_of=survey_state["survey_id"],
                  note=f"replay of SURVEY {survey_state['survey_id']} (its own order and OFFs, not this recipe's)")
    fixed = [(t["id"], *fixed_position(t)) for t in recipe["targets"] if "same_alt_as" not in t]
    tiles = {t["id"]: t for t in survey_state["tiles"]}
    for k, b in enumerate(survey_state.get("batches") or []):
        if b.get("status") != "DONE" or not (b.get("reduce") or {}).get("output_dir"):
            continue
        try:
            cap = json.loads(Path(b["resolved_plan"]).read_text())["requested"]["capture"]
        except (OSError, ValueError, KeyError):
            continue
        mode = None
        for name, m in recipe["modes"].items():
            if bool(cap.get("frequency_switching")) == m["frequency_switching"] and \
               (cap.get("frequency_switching_offset_hz") or 600000.0) == (m.get("offset_hz") or 600000.0) and \
               cap.get("frequency_switching_center") == m.get("center"):
                mode = name
        if mode is None:
            continue
        step = {"index": len(run["steps"]), "cycle": k, "slot": 0, "mode": mode, "status": "DONE",
                "campaign_id": b.get("campaign_id"), "output_dir": b["reduce"]["output_dir"], "reduce_run": b.get("reduce_run"),
                "ended_utc": b.get("ended_utc"), "targets": [], "points": []}
        for p in b.get("points") or []:
            tid = None
            for fid, ra, dec in fixed:
                if _sep(ra, dec, p["ra_hours"], p["dec_deg"]) <= match_deg:
                    tid = fid
            if tid is None and recipe["kind"] == "ONOFF":
                near = min(tiles.values(), key=lambda t: _sep(t["ra_hours"], t["dec_deg"], p["ra_hours"], p["dec_deg"]))
                tid = "SURVEY-" + near.get("label", near["id"])
            step["points"].append({**p, "target_id": tid})
        run["steps"].append(step)
    return run


# ================================================================================================== the runner's unit + page

def _systemctl(*args: str, timeout: float = 60.0):
    import subprocess
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)


def service_state() -> Dict[str, Any]:
    import subprocess
    try:
        r = _systemctl("is-active", UNIT, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"unit": UNIT, "active": None, "state": "unknown", "error": str(exc)}
    st = (r.stdout or "").strip() or "unknown"
    return {"unit": UNIT, "active": st in ("active", "activating", "deactivating"), "state": st}


def start_run(recipe_id: str, *, open_sky_confirmed: bool, note: str = "", base: Path = RUNS_BASE) -> Dict[str, Any]:
    """START from the EXPERIMENT page: a new run of the recipe, made active, and the runner's unit started.
    Refuses while the runner or the SURVEY pilot is already going (one driver of the mount at a time)."""
    import survey
    if service_state().get("active"):
        raise RuntimeError("an experiment is already running - STOP it first")
    if survey.service_state().get("active"):
        raise RuntimeError("the SURVEY pilot is running - stop it first (one driver of the mount at a time)")
    if not open_sky_confirmed:
        raise RuntimeError("confirm that the antenna is under open sky (indoors an experiment is not a sky result)")
    recipe = get_recipe(recipe_id)
    run = new_run(recipe, open_sky_confirmed=True, note=note)
    save_run(run, base)
    set_active(run["run_id"], base)
    _unit("start")
    return {"run_id": run["run_id"], **service_state()}


def resume_run(run_id: str, base: Path = RUNS_BASE) -> Dict[str, Any]:
    import survey
    if service_state().get("active") or survey.service_state().get("active"):
        raise RuntimeError("the experiment runner or the SURVEY pilot is already running")
    run = load_run(run_id, base)
    if run["status"] in ("DONE", "REPLAY"):
        raise RuntimeError(f"run {run_id} is {run['status']} - start a new run instead")
    set_active(run_id, base)
    _unit("start")
    return {"run_id": run_id, **service_state()}


def stop_runner() -> Dict[str, Any]:
    # no wait: the unit's SIGINT lets capture.py finish its point (minutes) - the page polls the state
    _unit("stop", "--no-block")
    return service_state()


def _unit(action: str, *flags: str) -> None:
    import subprocess
    try:
        r = _systemctl(action, *flags, UNIT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"systemctl --user {action} {UNIT} did not answer in 60 s")
    if r.returncode != 0:
        raise RuntimeError(f"systemctl --user {action} {UNIT} failed: {(r.stderr or r.stdout).strip()[:300]} "
                           "(installed by scripts/install_notebook.sh)")


_READY_CACHE: Dict[str, Any] = {"t": 0.0, "data": None}


def recipes_with_readiness(base: Path = RECIPES_DIR, cache_s: float = 60.0) -> List[Dict[str, Any]]:
    import time
    if _READY_CACHE["data"] is not None and time.monotonic() - _READY_CACHE["t"] < cache_s:
        return _READY_CACHE["data"]
    loc = site_location()
    out = []
    runs = list_runs()
    for r in load_recipes(base):
        try:
            rd = readiness(r, loc)
        except Exception as exc:  # noqa: BLE001 - one bad recipe must not hide the others
            rd = {"state": "ERROR", "reason": f"{type(exc).__name__}: {exc}"}
        last = next((x for x in runs if x["recipe_id"] == r["id"] and not x.get("replay_of")), None)
        after = []
        for dep in r.get("after") or []:
            done = next((x for x in runs if x["recipe_id"] == dep and x["status"] == "DONE" and not x.get("replay_of")), None)
            if done is None:
                after.append(dep)
        out.append({"id": r["id"], "name": r["name"], "kind": r["kind"], "question": r["question"], "why": r["why"],
                    "targets": r["targets"], "modes": {k: v["label"] for k, v in r["modes"].items()},
                    "cycles": r["cycles"], "steps": len(r["sequence"]) * r["cycles"], "capture_seconds": r["capture_seconds"],
                    "min_altitude_deg": r["min_altitude_deg"], "analysis": r["analysis"], "readiness": rd,
                    "pending_after": after, "last_run": last})
    _READY_CACHE.update(t=time.monotonic(), data=out)
    return out


def run_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    steps = run.get("steps") or []
    cur = next((s for s in steps if s.get("status") in ("PLANNED", "CAPTURED")), None)
    return {"run_id": run["run_id"], "recipe_id": run["recipe_id"], "name": run["recipe"]["name"], "status": run["status"],
            "created_utc": run["created_utc"], "pilot": run.get("pilot"), "replay_of": run.get("replay_of"), "note": run.get("note"),
            "steps": [{k: s.get(k) for k in ("index", "cycle", "mode", "status", "planned_targets", "ended_utc", "skipped_reason", "error")}
                      | {"targets": [t["id"] for t in s.get("targets") or []],
                         "accepted": sum(1 for p in s.get("points") or [] if p.get("accepted"))} for s in steps],
            "current_step": cur["index"] if cur else None,
            "steps_done": sum(1 for s in steps if s.get("status") in ("DONE", "SKIPPED"))}


def state_snapshot(base: Path = RUNS_BASE) -> Dict[str, Any]:
    out: Dict[str, Any] = {"service": service_state(), "active_run_id": active_id(base), "runs": list_runs(base)}
    try:
        import survey
        out["survey_service"] = survey.service_state()
    except Exception:  # noqa: BLE001
        out["survey_service"] = None
    try:
        out["recipes"] = recipes_with_readiness()
    except Exception as exc:  # noqa: BLE001
        out["recipes"] = []
        out["recipes_error"] = f"{type(exc).__name__}: {exc}"
    aid = out["active_run_id"]
    if aid:
        try:
            out["active_run"] = run_summary(load_run(aid, base))
        except (OSError, ValueError, RecipeError):
            out["active_run"] = None
    return out


def run_detail(run_id: str, base: Path = RUNS_BASE) -> Dict[str, Any]:
    run = load_run(run_id, base)
    return {**run_summary(run), "recipe": run["recipe"], "analysis": run.get("analysis"), "open_sky_confirmed": run.get("open_sky_confirmed")}
