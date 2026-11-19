#!/usr/bin/env python3
"""ALMITA SURVEY - the sky as a fixed set of tiles, observed again and again until every tile has its passes.

Operator 2026-11-19: "ojalá yo te pasara el control ... y yo fuera mirando en status cómo este lindo survey va
creciendo". A survey is defined once (centre, radius, spacing, passes, altitude floor, capture settings); the tiles
are fixed for its whole life; a state file records every pass. almita_survey.py is the pilot that picks the next
batch and drives OBSERVE + REDUCE; this module is pure bookkeeping and sky geometry (no hardware, no processes).

Layout: concentric rings around the centre, one tile every `spacing` degrees along each ring (ring k at k*spacing,
round(2*pi*k) tiles). Around the south celestial pole with 7.5 deg and a 30 deg radius: 1+6+13+19+25 = 64 tiles.

Files: data/surveys/<survey_id>/survey.json (definition + tiles + passes + batches), written atomically;
data/surveys/active.json names the survey the pilot runs.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import astropy_offline  # noqa: F401 - offline IERS policy, like the rest of the planning code

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, SkyOffsetFrame, get_sun
from astropy.time import Time

ROOT = Path(__file__).resolve().parent
SURVEYS_DIR = ROOT / "data" / "surveys"
ACTIVE_NAME = "active.json"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")

ALT_MARGIN_DEG = 2.0          # a tile must clear the floor by this much over the whole batch
POINT_MATCH_FRACTION = 0.5    # a reduced point belongs to the tile within half a spacing of it

DEFAULTS: Dict[str, Any] = {
    "centre_ra_hours": 0.0, "centre_dec_deg": -90.0, "radius_deg": 30.0, "spacing_deg": 7.5,
    "passes": 4, "min_altitude_deg": 25.0, "sun_avoid_deg": 35.0, "batch_points": 6,
    "check_centre_every_batch": True,
    "allow_simulator": False,     # a survey of the real sky waits while the header's mount selector is on SIM
    "site_lat_deg": None, "site_lon_deg": None,   # simulate/plan for another site (Petorca); None = observer_config
    "capture_seconds": 120.0, "settle_seconds": 2.0,
    "frequency_switching": True, "frequency_switching_offset_hz": None, "frequency_switching_center": None,
    # explicit galactic targets [{"id", "l", "b"}] instead of rings (2026-11-19: the Galactic Centre / rotation-curve
    # experiment). The FIRST target becomes the check tile "C" that opens every batch; spacing_deg is then only the
    # matching tolerance (a reduced point counts for a target within 0.5 x spacing).
    "targets": None,
}


class SurveyError(ValueError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ------------------------------------------------------------------------------------------------ definition / tiles

def ring_tiles(centre_ra_hours: float, centre_dec_deg: float, radius_deg: float, spacing_deg: float) -> List[Dict[str, Any]]:
    """Tiles on concentric rings around the centre, in the centre's spherical offset frame (exact on the sphere,
    fine across a pole). Each ring is turned half a step against the previous one so tiles do not line up radially."""
    if spacing_deg <= 0 or radius_deg < 0:
        raise SurveyError("spacing must be > 0 and radius >= 0")
    centre = SkyCoord(ra=centre_ra_hours * u.hourangle, dec=centre_dec_deg * u.deg, frame="icrs")
    frame = SkyOffsetFrame(origin=centre)
    rings = int(math.floor(radius_deg / spacing_deg + 1e-9))
    lon, lat, ids, ring_of = [], [], [], []
    for k in range(rings + 1):
        r = k * spacing_deg
        # ring circumference on the sphere is 2*pi*sin(r) (not 2*pi*r: that overcounts far from the centre)
        n = 1 if k == 0 else max(1, int(round(2 * math.pi * math.sin(math.radians(r)) / math.radians(spacing_deg))))
        for j in range(n):
            pa = 2 * math.pi * (j + 0.5 * (k % 2)) / n      # position angle, North through East
            # exact spherical point at distance r, angle pa, as a unit vector in the offset frame:
            # x toward the origin, y = offset east (lon), z = offset north (lat)
            x = math.cos(math.radians(r))
            y = math.sin(math.radians(r)) * math.sin(pa)
            z = math.sin(math.radians(r)) * math.cos(pa)
            lon.append(math.degrees(math.atan2(y, x)))
            lat.append(math.degrees(math.asin(max(-1.0, min(1.0, z)))))
            ids.append("C" if k == 0 else f"R{k}-{j + 1:02d}")
            ring_of.append(k)
    sky = SkyCoord(lon=np.array(lon) * u.deg, lat=np.array(lat) * u.deg, frame=frame).icrs
    gal = sky.galactic
    return [{"id": ids[i], "ring": ring_of[i],
             "ra_hours": round(float(sky.ra.hour[i]) % 24.0, 6), "dec_deg": round(float(sky.dec.deg[i]), 6),
             "l": round(float(gal.l.deg[i]), 3), "b": round(float(gal.b.deg[i]), 3),
             "passes_done": 0, "attempts": 0, "last_utc": None, "readings": []}
            for i in range(len(ids))]


def target_tiles(targets: Any) -> List[Dict[str, Any]]:
    """Tiles from explicit galactic targets; the first one is the check tile "C" (its own id kept as the label)."""
    if not isinstance(targets, list) or not 1 <= len(targets) <= 200:
        raise SurveyError("targets: a list of 1-200 {id, l, b}")
    out, seen = [], set()
    for i, t in enumerate(targets):
        if not isinstance(t, dict) or set(t) - {"id", "l", "b"} or not {"l", "b"} <= set(t):
            raise SurveyError(f"targets[{i}]: needs l and b (and an optional id), nothing else")
        l, b = t["l"], t["b"]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (l, b)) or not -90 <= b <= 90:
            raise SurveyError(f"targets[{i}]: l, b must be numbers, b within +/-90")
        label = str(t.get("id") or f"l{l:g}b{b:g}")[:24]
        tid = "C" if i == 0 else label
        if tid in seen or (i and label == "C"):
            raise SurveyError(f"targets[{i}]: duplicate id {tid!r}")
        seen.add(tid)
        c = SkyCoord(l=float(l) % 360.0 * u.deg, b=float(b) * u.deg, frame="galactic").icrs
        out.append({"id": tid, "label": label, "ring": 0 if i == 0 else 1,
                    "ra_hours": round(float(c.ra.hour) % 24.0, 6), "dec_deg": round(float(c.dec.deg), 6),
                    "l": round(float(l) % 360.0, 3), "b": round(float(b), 3),
                    "passes_done": 0, "attempts": 0, "last_utc": None, "readings": []})
    return out


def new_survey(survey_id: str, name: str, **params: Any) -> Dict[str, Any]:
    if not _ID_RE.match(survey_id or ""):
        raise SurveyError("survey_id: letters, digits, - and _ only (max 48)")
    nullable = ("frequency_switching_offset_hz", "frequency_switching_center", "site_lat_deg", "site_lon_deg", "targets")
    d = {**DEFAULTS, **{k: v for k, v in params.items() if v is not None or k in nullable}}
    unknown = set(params) - set(DEFAULTS)
    if unknown:
        raise SurveyError(f"unknown survey parameter(s): {sorted(unknown)}")
    checks = [("radius_deg", 0.0, 90.0), ("spacing_deg", 1.0, 30.0), ("passes", 1, 50), ("min_altitude_deg", 5.0, 80.0),
              ("sun_avoid_deg", 0.0, 120.0), ("batch_points", 1, 30), ("capture_seconds", 4.0, 1800.0),
              ("settle_seconds", 0.0, 60.0), ("centre_ra_hours", 0.0, 24.0), ("centre_dec_deg", -90.0, 90.0)]
    for key in ("check_centre_every_batch", "allow_simulator", "frequency_switching"):
        if not isinstance(d[key], bool):
            raise SurveyError(f"{key}: must be true or false")
    for key, lo, hi in (("site_lat_deg", -90.0, 90.0), ("site_lon_deg", -180.0, 180.0)):
        if d[key] is not None and not (isinstance(d[key], (int, float)) and lo <= d[key] <= hi):
            raise SurveyError(f"{key}: must be null or a number in [{lo}, {hi}]")
    for key, lo, hi in checks:
        v = d[key]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (lo <= v <= hi):
            raise SurveyError(f"{key}: must be a number in [{lo}, {hi}], got {v!r}")
    if d["targets"] is not None:
        tiles = target_tiles(d["targets"])
        d["centre_ra_hours"], d["centre_dec_deg"] = tiles[0]["ra_hours"], tiles[0]["dec_deg"]
    else:
        tiles = ring_tiles(d["centre_ra_hours"], d["centre_dec_deg"], d["radius_deg"], d["spacing_deg"])
    return {"schema": 1, "survey_id": survey_id, "name": str(name)[:80], "created_utc": utcnow(),
            "definition": d, "tiles": tiles, "batches": [],
            "pilot": {"state": "IDLE", "note": "created", "updated_utc": utcnow()}}


# ------------------------------------------------------------------------------------------------------ persistence

def survey_dir(survey_id: str, base: Path = SURVEYS_DIR) -> Path:
    if not _ID_RE.match(survey_id or ""):
        raise SurveyError("bad survey_id")
    return base / survey_id


def _write_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save(state: Dict[str, Any], base: Path = SURVEYS_DIR) -> None:
    _write_atomic(survey_dir(state["survey_id"], base) / "survey.json", state)


def load(survey_id: str, base: Path = SURVEYS_DIR) -> Dict[str, Any]:
    return json.loads((survey_dir(survey_id, base) / "survey.json").read_text(encoding="utf-8"))


def set_active(survey_id: Optional[str], base: Path = SURVEYS_DIR) -> None:
    _write_atomic(base / ACTIVE_NAME, {"survey_id": survey_id, "set_utc": utcnow()})


def active_id(base: Path = SURVEYS_DIR) -> Optional[str]:
    try:
        return json.loads((base / ACTIVE_NAME).read_text(encoding="utf-8")).get("survey_id")
    except (OSError, ValueError):
        return None


def list_surveys(base: Path = SURVEYS_DIR) -> List[Dict[str, Any]]:
    out = []
    for f in sorted(base.glob("*/survey.json")) if base.is_dir() else []:
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
            out.append({"survey_id": s["survey_id"], "name": s["name"], **progress(s)})
        except (OSError, ValueError, KeyError):
            continue
    return out


def set_pilot(state: Dict[str, Any], pilot_state: str, note: str, **extra: Any) -> None:
    state["pilot"] = {**{k: v for k, v in (state.get("pilot") or {}).items() if k in ("current_batch",)},
                      "state": pilot_state, "note": note, "updated_utc": utcnow(), **extra}


# ------------------------------------------------------------------------------------------------------ progress

def progress(state: Dict[str, Any]) -> Dict[str, Any]:
    passes = int(state["definition"]["passes"])
    tiles = state["tiles"]
    done_passes = sum(min(passes, t["passes_done"]) for t in tiles)
    total = passes * len(tiles)
    return {"tiles": len(tiles), "passes_per_tile": passes,
            "tiles_started": sum(1 for t in tiles if t["passes_done"] > 0),
            "tiles_complete": sum(1 for t in tiles if t["passes_done"] >= passes),
            "passes_done": done_passes, "passes_total": total,
            "fraction": round(done_passes / total, 4) if total else 0.0,
            "batches": len(state.get("batches") or []),
            "complete": bool(tiles) and all(t["passes_done"] >= passes for t in tiles)}


# ------------------------------------------------------------------------------------------------------ scheduling

def site_location(state: Dict[str, Any], observer_config_path: str = "observer_config.json",
                  allow_override: bool = True) -> EarthLocation:
    """Where the survey is planned: the definition's site override (e.g. Petorca, for simulations) or
    observer_config.json. The pilot passes allow_override=False: it must plan with the same site capture.py uses
    for its own altitude guard, so at a new site observer_config.json is what has to change."""
    d = state["definition"]
    if allow_override and d.get("site_lat_deg") is not None and d.get("site_lon_deg") is not None:
        return EarthLocation(lat=float(d["site_lat_deg"]) * u.deg, lon=float(d["site_lon_deg"]) * u.deg, height=0 * u.m)
    import observation_plan
    loc, _ = observation_plan._observer_location(observation_plan._load_observer_config(Path(observer_config_path)))
    return loc


def per_point_seconds(definition: Dict[str, Any]) -> float:
    import observation_plan
    d = observation_plan.estimate_duration(1, definition["capture_seconds"], definition["settle_seconds"],
                                           frequency_switching=bool(definition["frequency_switching"]))
    return float(d["per_point_planning_seconds"])


def _sep_deg(ra1_h, dec1, ra2_h, dec2) -> float:
    a1, d1, a2, d2 = (math.radians(ra1_h * 15), math.radians(dec1), math.radians(ra2_h * 15), math.radians(dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def visibility(tiles: List[Dict[str, Any]], location: EarthLocation, t0: Time, window_s: float) -> Dict[str, Dict[str, float]]:
    """Altitude of every tile now and at the end of the window, and its distance to the Sun."""
    if not tiles:
        return {}
    coords = SkyCoord(ra=[t["ra_hours"] * 15 for t in tiles] * u.deg, dec=[t["dec_deg"] for t in tiles] * u.deg)
    out = {}
    alts = []
    for when in (t0, t0 + timedelta(seconds=window_s)):
        alts.append(coords.transform_to(AltAz(obstime=when, location=location)).alt.deg)
    sun = get_sun(t0)
    sun_sep = coords.separation(SkyCoord(ra=sun.ra, dec=sun.dec)).deg
    # the Sun only matters while it is up (any part of the window): below the horizon it cannot reach the beam
    sun_alt = max(float(get_sun(w).transform_to(AltAz(obstime=w, location=location)).alt.deg)
                  for w in (t0, t0 + timedelta(seconds=window_s)))
    for i, t in enumerate(tiles):
        out[t["id"]] = {"alt_now": float(alts[0][i]), "alt_end": float(alts[1][i]),
                        "sun_sep": float(sun_sep[i]) if sun_alt > -2.0 else 180.0}
    return out


def choose_batch(state: Dict[str, Any], location: EarthLocation, now: Optional[datetime] = None,
                 last_pointing: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """The next batch: tiles still owing passes, visible over the whole batch (above floor + margin), far enough
    from the Sun. Passes are filled in waves (fewest passes first: every tile once before any tile twice). Inside
    the wave the path starts at the candidate nearest the mount's last pointing (or the highest one) and walks to
    the nearest neighbour each time. The centre tile opens every batch as the check tile when enabled. capture.py
    still applies its own meridian partition to this order. Returns {"tiles": [...], "reason": str}."""
    d = state["definition"]
    t0 = Time(now.astimezone(timezone.utc)) if now else Time.now()
    n = int(d["batch_points"])
    check = bool(d.get("check_centre_every_batch"))
    window = (n + (1 if check else 0)) * per_point_seconds(d)
    vis = visibility(state["tiles"], location, t0, window)
    floor = float(d["min_altitude_deg"]) + ALT_MARGIN_DEG

    def ok(t):
        v = vis[t["id"]]
        return min(v["alt_now"], v["alt_end"]) >= floor and v["sun_sep"] >= float(d["sun_avoid_deg"])

    passes = int(d["passes"])
    pending = [t for t in state["tiles"] if t["passes_done"] < passes and ok(t)]
    if not pending:
        owing = sum(1 for t in state["tiles"] if t["passes_done"] < passes)
        if owing == 0:
            return {"tiles": [], "reason": "survey complete"}
        return {"tiles": [], "reason": f"{owing} tile(s) still owe passes but none is above "
                                        f"{floor:.0f} deg and {d['sun_avoid_deg']:.0f} deg from the Sun right now"}
    pending.sort(key=lambda t: (t["passes_done"], -vis[t["id"]]["alt_now"]))
    wave = pending[0]["passes_done"]
    pool = [t for t in pending if t["passes_done"] == wave]
    if len(pool) < n:            # top up from the next wave rather than run a short batch
        pool += [t for t in pending if t["passes_done"] != wave][: n - len(pool)]
    if last_pointing:
        seed = min(pool, key=lambda t: _sep_deg(t["ra_hours"], t["dec_deg"], last_pointing["ra_hours"], last_pointing["dec_deg"]))
    else:
        seed = max(pool, key=lambda t: vis[t["id"]]["alt_now"])
    path, rest = [seed], [t for t in pool if t is not seed]
    while rest and len(path) < n:
        here = path[-1]
        nxt = min(rest, key=lambda t: _sep_deg(t["ra_hours"], t["dec_deg"], here["ra_hours"], here["dec_deg"]))
        path.append(nxt)
        rest.remove(nxt)
    if check:
        centre = next((t for t in state["tiles"] if t["id"] == "C"), None)
        if centre is not None and ok(centre):
            path = [centre] + [t for t in path if t is not centre]
    return {"tiles": [{"id": t["id"], "ra_hours": t["ra_hours"], "dec_deg": t["dec_deg"],
                       "alt_now": round(vis[t["id"]]["alt_now"], 1)} for t in path],
            "reason": f"wave {wave + 1} of {passes}: {len(path)} tile(s), lowest now "
                      f"{min(vis[t['id']]['alt_now'] for t in path):.0f} deg"}


# ------------------------------------------------------------------------------------------------------ OBSERVE spec

def batch_spec(state: Dict[str, Any], batch_tiles: List[Dict[str, Any]], gain_db: float) -> Dict[str, Any]:
    """The TARGET_LIST spec OBSERVE plans for one batch (observation_plan.plan_target_list)."""
    import sdr_tuning
    d = state["definition"]
    op = sdr_tuning.operating_config()
    capture: Dict[str, Any] = {"seconds": float(d["capture_seconds"]), "settle_seconds": float(d["settle_seconds"]),
                               "frequency_switching": bool(d["frequency_switching"])}
    if d["frequency_switching"]:
        if d.get("frequency_switching_offset_hz") is not None:
            capture["frequency_switching_offset_hz"] = d["frequency_switching_offset_hz"]
        if d.get("frequency_switching_center") is not None:
            capture["frequency_switching_center"] = d["frequency_switching_center"]
    return {
        "session": {"name": f"ALMITA-SURVEY-{state['survey_id']}"[:64]},
        "grid": {"mode": "TARGET_LIST", "min_altitude_deg": float(d["min_altitude_deg"]),
                 "nominal_spacing_deg": float(d["spacing_deg"]),
                 "targets": [{"ra_hours": t["ra_hours"], "dec_deg": t["dec_deg"], "label": t["id"]} for t in batch_tiles]},
        "capture": capture,
        "main": {"center_frequency_hz": op["center_frequency_hz"], "sample_rate": op["sample_rate_hz"],
                 "gain_db": float(gain_db), "bias_tee": True},
        "rfi_ref": {"enabled": False},
        "quicklook": {"enabled": False},
        "console": {"enabled": True},
        "execution": {"unattended": True},
    }


# ------------------------------------------------------------------------------------------------------ results

def match_tile(state: Dict[str, Any], ra_hours: float, dec_deg: float) -> Optional[Dict[str, Any]]:
    """The tile a reduced point belongs to: the nearest one, if within half a spacing."""
    best, best_sep = None, None
    for t in state["tiles"]:
        s = _sep_deg(t["ra_hours"], t["dec_deg"], ra_hours, dec_deg)
        if best_sep is None or s < best_sep:
            best, best_sep = t, s
    if best is not None and best_sep <= POINT_MATCH_FRACTION * float(state["definition"]["spacing_deg"]):
        return best
    return None


def record_batch(state: Dict[str, Any], batch: Dict[str, Any], points: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Credit a pass to each tile with an accepted reduced point. points: [{ra_hours, dec_deg, accepted: bool,
    reading: float|None, point_index}]. Every planned tile counts one attempt. Returns a summary."""
    planned = {t["id"] for t in batch.get("tiles", [])}
    by_id = {t["id"]: t for t in state["tiles"]}
    for tid in planned:
        if tid in by_id:
            by_id[tid]["attempts"] += 1
    credited, unmatched = [], 0
    for p in points:
        if not p.get("accepted"):
            continue
        t = match_tile(state, p["ra_hours"], p["dec_deg"])
        if t is None:
            unmatched += 1
            continue
        t["passes_done"] += 1
        t["last_utc"] = batch.get("ended_utc") or utcnow()
        t["readings"].append({"campaign_id": batch.get("campaign_id"), "reduce_run": batch.get("reduce_run"),
                              "point_index": p.get("point_index"), "reading": p.get("reading"),
                              "utc": t["last_utc"]})
        credited.append(t["id"])
    return {"credited": credited, "unmatched": unmatched, "missed": sorted(planned - set(credited))}


# ------------------------------------------------------------------------------------------------------ the pilot's unit

UNIT = "almita-survey.service"


def _systemctl(*args: str, timeout: float = 60.0):
    import subprocess
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)


def service_state() -> Dict[str, Any]:
    import subprocess
    try:
        r = _systemctl("is-active", UNIT, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"unit": UNIT, "active": None, "state": "unknown", "error": str(exc)}
    state = (r.stdout or "").strip() or "unknown"
    return {"unit": UNIT, "active": state == "active", "state": state}


def service_action(action: str) -> Dict[str, Any]:
    """START / STOP from STATUS. STOP is the unit's KillSignal=SIGINT: capture.py ends its point cleanly, the pilot
    waits for it and saves; the captured batch is reduced on the next START."""
    import subprocess
    if action not in ("start", "stop"):
        raise ValueError("action must be start or stop")
    if action == "start" and not active_id():
        raise RuntimeError("no active survey - create one and activate it first (almita_survey.py create/activate)")
    try:
        # STOP does not wait: the unit's SIGINT lets capture.py finish its point (up to ~2-3 min), longer than any
        # web request should hang - the panel's polling shows deactivating -> inactive (2026-11-19 review)
        r = _systemctl(*(("stop", "--no-block") if action == "stop" else ("start",)), UNIT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"systemctl --user {action} {UNIT} did not answer in 60 s")
    if r.returncode != 0:
        raise RuntimeError(f"systemctl --user {action} {UNIT} failed: {(r.stderr or r.stdout).strip()[:300]} "
                           "(installed by scripts/install_notebook.sh)")
    return {"action": action, **service_state()}


def status_snapshot(base: Path = SURVEYS_DIR) -> Dict[str, Any]:
    """What STATUS shows: the active survey, its progress, the pilot, the unit, and every tile (position, passes,
    mean reading) - no spectra, small enough to poll."""
    sid = active_id(base)
    out: Dict[str, Any] = {"active_id": sid, "service": service_state(), "surveys": list_surveys(base)}
    if not sid:
        return out
    try:
        st = load(sid, base)
    except (OSError, ValueError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    d = st["definition"]
    # distance to the Sun right now: tiles inside sun_avoid are only above the horizon by day, so the pilot cannot
    # reach them this season (December: the Galactic Centre side) - STATUS draws them apart instead of as "owed"
    try:
        sun = get_sun(Time.now())
        coords = SkyCoord(ra=[t["ra_hours"] * 15 for t in st["tiles"]] * u.deg, dec=[t["dec_deg"] for t in st["tiles"]] * u.deg)
        sun_sep = [float(x) for x in coords.separation(SkyCoord(ra=sun.ra, dec=sun.dec)).deg]
    except Exception:  # noqa: BLE001 - display only; never break STATUS over it
        sun_sep = [None] * len(st["tiles"])
    tiles = []
    for t, sep in zip(st["tiles"], sun_sep):
        vals = [r["reading"] for r in t.get("readings") or [] if isinstance(r.get("reading"), (int, float))]
        tiles.append({"id": t["id"], "ra_hours": t["ra_hours"], "dec_deg": t["dec_deg"], "l": t["l"], "b": t["b"],
                      "passes_done": t["passes_done"], "attempts": t["attempts"], "last_utc": t["last_utc"],
                      "reading_mean": round(sum(vals) / len(vals), 2) if vals else None,
                      "sun_sep_deg": round(sep, 1) if sep is not None else None,
                      "behind_sun": sep is not None and sep < float(d["sun_avoid_deg"])})
    batches = st.get("batches") or []
    last = batches[-1] if batches else None
    out.update({
        "survey_id": sid, "name": st["name"], "created_utc": st["created_utc"],
        "definition": {k: d[k] for k in ("centre_ra_hours", "centre_dec_deg", "radius_deg", "spacing_deg", "passes",
                                          "min_altitude_deg", "sun_avoid_deg", "batch_points", "capture_seconds",
                                          "allow_simulator")},
        "progress": {**progress(st), "behind_sun": sum(1 for t in tiles if t["behind_sun"] and t["passes_done"] < int(d["passes"]))},
        "pilot": st.get("pilot"), "tiles": tiles,
        "last_batch": {k: last.get(k) for k in ("batch_id", "status", "planned_utc", "ended_utc", "campaign_id",
                                                 "credited", "missed", "reason", "error", "mount")} if last else None,
    })
    return out
