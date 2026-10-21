"""SESSIONS: catalog and viewer of stored observations (BL-079..BL-089, docs/SESSIONS_FILE_CONTRACTS.md).

Read-only over everything an observation, a REDUCE or a SCIENCE run wrote: nothing under data/mosaic, data/reduced,
data/science or data/calibration is ever moved, renamed, rewritten or deleted here, no HDF5 IQ array is ever read,
and nothing touches the mount, the SDRs, rtl_tcp or INDI. The only files this module writes live under data/sessions/:
  catalog_index.json   the catalog, rebuilt from the data at any time (disposable)
  annotations.json     the operator's names, notes, tags, marks and confirmed associations (never rebuilt)
  annotations_log.jsonl every annotation change, appended
  exports/<id>-<utc>/  exported reports (report.md + manifest.json + figures, and a .zip of them)

The canonical identity is session_identity's: campaign_id = the data/mosaic/<campaign_id>/ directory name.
Every association carries the file and field it comes from; one without such evidence stays PENDING - closeness in
time is never enough. Four states are kept apart everywhere: NOT_RUN, FAILED, MISSING (an expected file is absent)
and UNKNOWN (the metadata was never recorded)."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
INDEX_SCHEMA = 1
ANNOTATION_SCHEMA = 1
CHILE_TZ_NAME = "America/Santiago"
NOT_RUN, FAILED, MISSING, UNKNOWN = "NOT_RUN", "FAILED", "MISSING", "UNKNOWN"
CONDITIONS = ("OPEN_SKY", "INDOOR", "SIMULATOR", "UNKNOWN")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.+ -]{1,40}$")
_INDEX_LOCK = threading.Lock()
_ANNOTATION_LOCK = threading.Lock()
MAX_FILES_LISTED = 500
TEMPERATURE_MAX_POINTS = 600
# Files an export never carries, whatever is asked: authentication, keys, tokens, environment files.
_SECRET_NAME_RE = re.compile(r"(auth|passw|secret|token|credential|\.key$|\.pem$|\.env$|id_rsa|htpasswd)", re.I)


@dataclass(frozen=True)
class Roots:
    base: Path
    mosaic: Path
    reduced: Path
    science: Path
    calibration: Path
    temperature: Path
    store: Path

    @classmethod
    def under(cls, base: Path) -> "Roots":
        base = Path(base)
        data = base / "data"
        return cls(base, data / "mosaic", data / "reduced", data / "science", data / "calibration",
                   data / "runtime" / "temperature", data / "sessions")


def default_roots() -> Roots:
    return Roots.under(ROOT)


# ------------------------------------------------------------------ small readers (never raise on bad data files)
def _rel(path: Path, roots: Roots) -> str:
    try:
        return str(Path(path).relative_to(roots.base))
    except ValueError:
        return str(path)


def _read_json(path: Path, max_bytes: int = 20_000_000) -> Tuple[Any, str]:
    """(content, state): state is OK, MISSING (no file) or UNREADABLE (empty, corrupt, too big)."""
    try:
        if not path.is_file():
            return None, MISSING
        if path.stat().st_size > max_bytes:
            return None, "UNREADABLE"
        return json.loads(path.read_text()), "OK"
    except (OSError, ValueError):
        return None, "UNREADABLE"


def _sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _parse_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def chile_time(utc_iso: Optional[str]) -> Optional[str]:
    """The same instant in Chile's civil time (America/Santiago, with its DST), or None when the zone data is absent."""
    t = _parse_utc(utc_iso)
    if t is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        local = t.astimezone(ZoneInfo(CHILE_TZ_NAME))
    except Exception:  # noqa: BLE001 - no tz database: say nothing rather than guess an offset
        return None
    return local.strftime("%Y-%m-%d %H:%M:%S") + f" (UTC{local.strftime('%z')[:3]})"


def disk_usage(path: Path, seen: Optional[set] = None) -> Dict[str, Any]:
    """Space a directory really takes: allocated blocks (st_blocks x 512, not apparent sizes), each inode counted once
    (hard links), symbolic links never followed (counted apart, 0 bytes). `seen` lets several calls share one inode set."""
    seen = set() if seen is None else seen
    out = {"bytes_on_disk": 0, "apparent_bytes": 0, "files": 0, "symlinks": 0, "hardlinks_counted_once": 0}
    stack = [Path(path)]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    out["symlinks"] += 1
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if e.is_dir(follow_symlinks=False):
                stack.append(Path(e.path))
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                out["hardlinks_counted_once"] += 1
                continue
            seen.add(key)
            out["files"] += 1
            out["apparent_bytes"] += st.st_size
            out["bytes_on_disk"] += getattr(st, "st_blocks", 0) * 512 or st.st_size
    return out


SIZE_METHOD = ("allocated disk blocks (st_blocks x 512), each file counted once by inode (hard links), symbolic links "
               "not followed; the campaign directory only - its REDUCE and SCIENCE runs are summed apart, and calibration "
               "profiles (shared by several sessions) are listed but never added to any session")


# ------------------------------------------------------------------ campaign files
def _mosaic_rows(camp: Path) -> Tuple[List[Dict[str, str]], str]:
    path = camp / "mosaic.csv"
    if not path.is_file():
        return [], MISSING
    try:
        with path.open(newline="") as fh:
            return list(csv.DictReader(fh)), "OK"
    except (OSError, csv.Error, UnicodeDecodeError):
        return [], "UNREADABLE"


def _point_counts(rows: List[Dict[str, str]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in rows:
        status = (r.get("capture_status") or "unknown").strip() or "unknown"
        if str(r.get("visibility_deferred", "")).lower() == "true" and status != "success":
            status = "deferred"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _iq_dirs(camp: Path) -> List[Path]:
    iq = camp / "data" / "iq"
    try:
        return sorted(p for p in iq.iterdir() if p.is_dir()) if iq.is_dir() else []
    except OSError:
        return []


def _capture_state(rows_state: str, counts: Dict[str, int], planned: Optional[int], has_iq: bool,
                   stop_report: Optional[Dict[str, Any]], started: bool = False) -> Dict[str, Any]:
    """From mosaic.csv's own per-point status (declared by the orchestrator; HDF5 files are not opened here) and
    stop_report.json. observation_runtime.json is not used: it stays RUNNING by design (BL-016)."""
    success = counts.get("success", 0)
    failed = sum(v for k, v in counts.items() if k in ("failed", "error"))
    deferred = counts.get("deferred", 0)
    evidence = ["mosaic.csv capture_status"]
    if rows_state != "OK":
        return {"state": UNKNOWN, "detail": f"mosaic.csv {rows_state.lower()}", "evidence": evidence}
    if stop_report is not None:
        evidence.append("stop_report.json")
        state = "STOPPED"
        detail = f"stopped by the operator during {stop_report.get('phase_at_stop') or '?'}"
    elif not has_iq and success == 0 and started:
        evidence.append("orchestrator_capture.log")
        state, detail = "NO_CAPTURE", "the run started (orchestrator_capture.log exists) but no point was captured"
    elif not has_iq and success == 0:
        state, detail = NOT_RUN, "planned only: no capture directory was ever created"
    elif planned and success + deferred >= planned and failed == 0:
        state, detail = "COMPLETED", "every planned point captured (or deferred by the plan itself)"
    elif success == 0 and failed:
        state, detail = FAILED, "no point captured"
    else:
        state, detail = "INCOMPLETE", "the session ended before every planned point (no stop report)"
    return {"state": state, "detail": detail, "evidence": evidence}


def _mount_from_log(camp: Path) -> Optional[str]:
    """The INDI device the orchestrator logged ('Mount  LX200 OnStep' / 'Mount  Telescope Simulator')."""
    path = camp / "orchestrator_capture.log"
    try:
        with path.open(errors="replace") as fh:
            for i, line in enumerate(fh):
                m = re.match(r"^\s*Mount\s{2,}(.+?)\s*$", line)
                if m:
                    return m.group(1)
                if i > 400:
                    break
    except OSError:
        return None
    return None


def _condition(mount: Optional[str]) -> Dict[str, Any]:
    """Only what is recorded. A real mount does not mean open sky (it is used indoors too); only the simulator is a
    recorded condition. Everything else is UNKNOWN until the operator documents it (an annotation, labelled as such)."""
    if mount and "simulator" in mount.lower():
        return {"value": "SIMULATOR", "source": "orchestrator_capture.log: Mount = " + mount, "documented_by": "log"}
    return {"value": "UNKNOWN", "source": ("mount " + mount + " recorded; open sky or indoor is not recorded") if mount
            else "not recorded", "documented_by": None}


def _profile_info(roots: Roots, profile_rel: Optional[str]) -> Dict[str, Any]:
    if not profile_rel:
        return {"path": None, "state": NOT_RUN, "detail": "no calibration profile was set for this session"}
    p = Path(profile_rel)
    p = p if p.is_absolute() else roots.base / p
    stem = p.with_suffix("") if p.suffix in (".json", ".npz") else p
    marker = Path(f"{stem}.INVALIDATED.json")            # calibration_foundation.invalidation_marker()'s own name
    info: Dict[str, Any] = {"path": profile_rel, "state": "OK", "detail": None, "invalidated": None}
    if not stem.with_suffix(".json").is_file():
        info.update(state=MISSING, detail="the profile file this session points to is not on disk")
    if marker.is_file():
        m, _ = _read_json(marker)
        info["invalidated"] = m if isinstance(m, dict) else {"reason": "marker unreadable"}
        info.update(state="INVALIDATED", detail=(m or {}).get("reason") if isinstance(m, dict) else "marker present")
    return info


# ------------------------------------------------------------------ REDUCE and SCIENCE runs
def _events_bounds(path: Path) -> Dict[str, Any]:
    """First and last event of a REDUCE events.jsonl (begin/end status) without loading the whole file."""
    out: Dict[str, Any] = {"begin": None, "end": None}
    try:
        with path.open(errors="replace") as fh:
            first = fh.readline()
            last = first
            for line in fh:
                if line.strip():
                    last = line
        out["begin"] = json.loads(first) if first.strip() else None
        tail = json.loads(last) if last.strip() else None
        out["end"] = tail if tail and tail.get("event") == "REDUCE_CAMPAIGN_END" else None
        out["last"] = tail
    except (OSError, ValueError):
        pass
    return out


def _campaign_from_points(run: Path) -> Optional[str]:
    """An interrupted REDUCE has no manifest: the campaign is then read from the capture_refs of the points it wrote."""
    points = run / "points"
    try:
        dirs = sorted((p for p in points.iterdir() if p.is_dir()), key=lambda p: (len(p.name), p.name))
    except OSError:
        return None
    for d in dirs[:3]:
        ms, state = _read_json(d / "master_spectrum.json", 2_000_000)
        refs = (ms or {}).get("capture_refs") if state == "OK" else None
        for ref in refs or []:
            m = re.match(r"^(data/mosaic/[^/]+)/", str(ref.get("source_path") or ""))
            if m:
                return m.group(1)
    return None


def scan_reduce_runs(roots: Roots) -> List[Dict[str, Any]]:
    runs = []
    if not roots.reduced.is_dir():
        return runs
    for run in sorted(roots.reduced.glob("*/REDUCE-*")):
        if not run.is_dir():
            continue
        manifest, m_state = _read_json(run / "manifest.json")
        prov, p_state = _read_json(run / "provenance.json")
        config, c_state = _read_json(run / "config.json")
        ev = _events_bounds(run / "logs" / "events.jsonl")
        manifest = manifest if isinstance(manifest, dict) else {}
        prov = prov if isinstance(prov, dict) else {}
        config = config if isinstance(config, dict) else {}
        if manifest.get("source_campaign_root"):
            campaign_dir, link = manifest["source_campaign_root"], "manifest.json: source_campaign_root"
        else:
            campaign_dir = _campaign_from_points(run)
            link = "points/*/master_spectrum.json: capture_refs[].source_path" if campaign_dir else None
        if m_state == "OK":
            status = manifest.get("status") or UNKNOWN
        elif ev["end"]:
            status = ev["end"].get("status") or UNKNOWN
        elif ev["begin"]:
            status = "INTERRUPTED"
        else:
            status = UNKNOWN
        try:
            n_points = sum(1 for p in (run / "points").iterdir() if p.is_dir())
        except OSError:
            n_points = 0
        runs.append({
            "kind": "REDUCE", "run_id": run.name, "dir": _rel(run, roots), "status": status,
            "manifest": m_state, "provenance": p_state, "config": c_state,
            "campaign_dir": campaign_dir, "link_evidence": link,
            "campaign_name": manifest.get("campaign_id") or (ev["begin"] or {}).get("campaign_id"),
            "started_utc": prov.get("started_utc") or (ev["begin"] or {}).get("utc"),
            "ended_utc": prov.get("ended_utc") or (ev["end"] or {}).get("utc"),
            "git_commit": prov.get("git_commit"), "pipeline_version": prov.get("pipeline_version"),
            "config_hash": prov.get("config_hash"), "calibration_profile_hash": prov.get("calibration_profile_hash"),
            "calibration_profile_path": config.get("calibration_profile_path"), "velocity_frame": config.get("velocity_frame"),
            "points_written": n_points,
            "counts": {k: manifest.get(k) for k in ("points_discovered", "points_accepted", "points_rejected", "points_completed",
                                                     "points_blocked", "points_failed")} if manifest else None,
            "quality_counts": manifest.get("quality_counts"), "velocity_frame_counts": manifest.get("velocity_frame_counts"),
            "calibration_level_counts": manifest.get("calibration_level_counts"),
            "median_usable_fraction": manifest.get("median_usable_fraction"),
            "median_baseline_rms_fraction": manifest.get("median_baseline_rms_fraction"),
            "manifest_sha256": _sha256(run / "manifest.json") if m_state == "OK" else None,
        })
    return runs


def scan_science_runs(roots: Roots, reduce_runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_dir = {r["dir"]: r for r in reduce_runs}
    runs = []
    if not roots.science.is_dir():
        return runs
    for run in sorted(roots.science.glob("*/*")):
        if not run.is_dir():
            continue
        manifest, m_state = _read_json(run / "manifest.json")
        prov, p_state = _read_json(run / "provenance.json")
        manifest = manifest if isinstance(manifest, dict) else {}
        prov = prov if isinstance(prov, dict) else {}
        web = run.name.startswith("SCIENCE_WEB-")
        reduce_dir = prov.get("input_reduce_session_dir") or manifest.get("reduce_session_dir")
        link_state, link_detail = "PENDING", None
        if not web:
            link_detail = "not a SCIENCE WEB run (no provenance.json/manifest.json contract): association left to the operator"
        elif not reduce_dir:
            link_detail = f"provenance.json {p_state.lower()} and manifest.json {m_state.lower()}: input REDUCE not recorded"
        elif reduce_dir not in by_dir:
            link_state, link_detail = MISSING, f"input REDUCE {reduce_dir} is not on disk"
        else:
            expected = manifest.get("input_reduce_manifest_sha256") or prov.get("input_reduce_manifest_sha256")
            actual = by_dir[reduce_dir].get("manifest_sha256")
            if expected and actual and expected == actual:
                link_state, link_detail = "LINKED", "provenance.json input_reduce_session_dir + manifest sha256 match"
            elif expected and actual:
                link_state, link_detail = "INCONSISTENT", "the REDUCE manifest changed since this SCIENCE read it (sha256 differs)"
            else:
                link_state, link_detail = "LINKED", "provenance.json input_reduce_session_dir (no sha256 to cross-check)"
        maps = sorted(p.name for p in (run / "maps").glob("*.png")) if (run / "maps").is_dir() else []
        if web and m_state == "OK":
            status = "COMPLETED"
        elif web and m_state == MISSING and p_state != "OK":
            status = "INTERRUPTED"
        elif web:
            status = UNKNOWN
        else:
            status = "MANUAL"
        runs.append({
            "kind": "SCIENCE", "run_id": run.name, "dir": _rel(run, roots), "web": web, "status": status,
            "manifest": m_state, "provenance": p_state, "reduce_dir": reduce_dir if link_state != "PENDING" else None,
            "link_state": link_state, "link_detail": link_detail,
            "git_commit": prov.get("git_commit"), "pipeline_version": prov.get("science_web_pipeline_version"),
            "config_hash": prov.get("config_hash") or manifest.get("config_hash"),
            "started_utc": prov.get("started_utc"), "ended_utc": prov.get("ended_utc"),
            "data_completeness": manifest.get("data_completeness"), "maps": maps,
            "group": run.parent.name,
        })
    return runs


def _name_candidates(science_run: Dict[str, Any], campaigns: Dict[str, Dict[str, Any]]) -> List[str]:
    """A manual analysis whose folder name contains a campaign's exact capture session id (e.g. HI_MANUAL-20261018_212734)
    is a CANDIDATE only - the operator confirms it; nothing is associated from it on its own."""
    out = []
    for cid, c in campaigns.items():
        for sid in c.get("capture_session_ids") or []:
            if sid and sid in science_run["run_id"]:
                out.append(cid)
    return sorted(set(out))


# ------------------------------------------------------------------ one campaign
def campaign_record(camp: Path, roots: Roots, reduce_runs: List[Dict[str, Any]], science_runs: List[Dict[str, Any]],
                    seen_inodes: set) -> Dict[str, Any]:
    plan, plan_state = _read_json(camp / "observation_resolved.json")
    plan = plan if isinstance(plan, dict) else {}
    grid_meta, _ = _read_json(camp / "grid_metadata.json")
    grid_meta = grid_meta if isinstance(grid_meta, dict) else {}
    stop_report, stop_state = _read_json(camp / "stop_report.json")
    rows, rows_state = _mosaic_rows(camp)
    counts = _point_counts(rows)
    resolved = plan.get("resolved") or {}
    planned = resolved.get("point_count") or (grid_meta.get("grid") or {}).get("total_points") or (len(rows) or None)
    iq_dirs = _iq_dirs(camp)
    capture_ids = sorted(m.group(1) for p in camp.glob("capture_timing_*.csv")
                         if (m := re.match(r"^capture_timing_(\d{8}_\d{6})\.csv$", p.name)))
    started = plan.get("planning_timestamp_utc")
    if not started:
        m = re.search(r"(\d{8}-\d{2}:\d{2}:\d{2})$", camp.name)
        started = datetime.strptime(m.group(1), "%Y%m%d-%H:%M:%S").replace(tzinfo=timezone.utc).isoformat() if m else None
    times = [t for r in rows for t in (_parse_utc(r.get("start_time")), _parse_utc(r.get("end_time"))) if t]
    first, last = (min(times), max(times)) if times else (None, None)
    rel_dir = _rel(camp, roots)
    my_reduce = [r for r in reduce_runs if r.get("campaign_dir") == rel_dir]
    my_reduce_dirs = {r["dir"] for r in my_reduce}
    my_science = [s for s in science_runs if s.get("reduce_dir") in my_reduce_dirs]
    mount = _mount_from_log(camp)
    main = plan.get("main") or {}
    oc = plan.get("observer_config") or {}
    usage = disk_usage(camp, seen_inodes)
    product_usage = {"bytes_on_disk": 0, "files": 0}
    for r in my_reduce + my_science:
        u = disk_usage(roots.base / r["dir"], seen_inodes)
        product_usage["bytes_on_disk"] += u["bytes_on_disk"]
        product_usage["files"] += u["files"]
    reduce_state = (NOT_RUN if not my_reduce else
                    "COMPLETED" if any(r["status"] == "COMPLETED" for r in my_reduce) else
                    "PARTIAL" if any(r["status"] == "PARTIAL" for r in my_reduce) else FAILED)
    science_state = (NOT_RUN if not my_science else
                     "COMPLETED" if any(s["status"] == "COMPLETED" for s in my_science) else FAILED)
    profile = _profile_info(roots, (plan.get("quicklook") or {}).get("calibration_profile_path"))
    return {
        "campaign_id": camp.name, "dir": rel_dir,
        "observation_name": plan.get("observation_name") or grid_meta.get("session_name"),
        "kind": "TEST" if camp.name.upper().startswith("TEST") else "OBSERVE",
        "kind_evidence": "directory name starts with TEST" if camp.name.upper().startswith("TEST") else None,
        "plan_state": plan_state, "stop_report": stop_state,
        "started_utc": started, "started_chile": chile_time(started),
        "first_capture_utc": first.isoformat() if first else None, "last_capture_utc": last.isoformat() if last else None,
        "duration_s": (last - first).total_seconds() if first and last else None,
        "capture": _capture_state(rows_state, counts, planned, bool(iq_dirs), stop_report if stop_state == "OK" else None,
                                  (camp / "orchestrator_capture.log").is_file()),
        "points_planned": planned, "points_captured": counts.get("success", 0), "point_counts": counts,
        "iq_session_dirs": [p.name for p in iq_dirs], "capture_session_ids": capture_ids,
        "calibration": {"state": profile["state"], "profile_path": profile["path"]},
        "reduce": {"state": reduce_state, "runs": [r["run_id"] for r in my_reduce]},
        "science": {"state": science_state, "runs": [s["run_id"] for s in my_science]},
        "receiver": {"value": None, "documented_by": None,
                     "config_label": (oc.get("hardware") or {}).get("sdr_device"),
                     "config_label_note": "observer_config.json hardware.sdr_device is a fixed configuration label, not a reading "
                                          "of the connected dongle (it said RTL-SDR Blog V4 after the 2026-10-17 change to the V3)"},
        "mount": mount, "condition": _condition(mount),
        "main": {k: main.get(k) for k in ("center_frequency_hz", "sample_rate", "gain_db", "bias_tee")} if main else None,
        "grid": {k: resolved.get(k) for k in ("rows", "cols", "point_count", "spacing_deg", "rotation_deg", "center_ra_hours", "center_dec_deg")}
        if resolved else None,
        "size": {"campaign": usage, "products": product_usage},
    }


# ------------------------------------------------------------------ the index (disposable, rebuilt from the data)
def _index_path(roots: Roots) -> Path:
    return roots.store / "catalog_index.json"


def _write_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=1, default=str, allow_nan=False))
    os.replace(tmp, path)


def build_index(roots: Optional[Roots] = None, write: bool = True) -> Dict[str, Any]:
    """Discover every campaign, REDUCE and SCIENCE run. Deterministic (sorted) and idempotent: building twice over the
    same data gives the same sessions; only generated_utc changes. Reads the data, writes only catalog_index.json."""
    roots = roots or default_roots()
    with _INDEX_LOCK:
        started = datetime.now(timezone.utc)
        reduce_runs = scan_reduce_runs(roots)
        science_runs = scan_science_runs(roots, reduce_runs)
        seen: set = set()
        sessions: List[Dict[str, Any]] = []
        if roots.mosaic.is_dir():
            for camp in sorted(p for p in roots.mosaic.iterdir() if p.is_dir()):
                try:
                    sessions.append(campaign_record(camp, roots, reduce_runs, science_runs, seen))
                except Exception as exc:  # noqa: BLE001 - one unreadable campaign never hides the others
                    sessions.append({"campaign_id": camp.name, "dir": _rel(camp, roots), "error": f"{type(exc).__name__}: {exc}",
                                     "capture": {"state": UNKNOWN, "detail": "could not be read", "evidence": []}})
        by_id = {s["campaign_id"]: s for s in sessions}
        known_dirs = {s["dir"] for s in sessions}
        pending = []
        for r in reduce_runs:
            if not r.get("campaign_dir") or r["campaign_dir"] not in known_dirs:
                pending.append({"kind": "REDUCE", "dir": r["dir"], "run_id": r["run_id"], "status": r["status"],
                                "reason": "no campaign recorded" if not r.get("campaign_dir") else f"campaign {r['campaign_dir']} not on disk",
                                "candidates": []})
        for s in science_runs:
            if s["link_state"] != "LINKED" and s["link_state"] != "INCONSISTENT":
                pending.append({"kind": "SCIENCE", "dir": s["dir"], "run_id": s["run_id"], "status": s["status"],
                                "reason": s["link_detail"], "candidates": _name_candidates(s, by_id)})
        index = {"schema_version": INDEX_SCHEMA, "generated_utc": started.isoformat(timespec="seconds"),
                 "build_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
                 "size_method": SIZE_METHOD, "sessions": sessions, "reduce_runs": reduce_runs, "science_runs": science_runs,
                 "pending_associations": pending}
        if write:
            _write_atomic(_index_path(roots), index)
        return index


def load_index(roots: Optional[Roots] = None) -> Dict[str, Any]:
    roots = roots or default_roots()
    idx, state = _read_json(_index_path(roots), 200_000_000)
    if state == "OK" and isinstance(idx, dict) and idx.get("schema_version") == INDEX_SCHEMA:
        return idx
    return build_index(roots)


# ------------------------------------------------------------------ annotations (the operator's; never rebuilt)
def _annotations_path(roots: Roots) -> Path:
    return roots.store / "annotations.json"


def load_annotations(roots: Optional[Roots] = None) -> Dict[str, Any]:
    roots = roots or default_roots()
    a, state = _read_json(_annotations_path(roots))
    if state == "OK" and isinstance(a, dict):
        a.setdefault("sessions", {})
        return a
    if state == "UNREADABLE":
        raise ValueError("annotations.json is unreadable: refusing to start a new one over it (fix or move it by hand)")
    return {"schema_version": ANNOTATION_SCHEMA, "sessions": {}}


def _clean_text(v: Any, field: str, limit: int) -> str:
    if not isinstance(v, str):
        raise ValueError(f"{field} must be text")
    v = v.replace("\x00", "").strip()
    if len(v) > limit:
        raise ValueError(f"{field} is longer than {limit} characters")
    return v


def _validate_patch(patch: Dict[str, Any], session: Optional[Dict[str, Any]], index: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("nothing to change")
    out: Dict[str, Any] = {}
    for key, v in patch.items():
        if key in ("name", "notes"):
            out[key] = _clean_text(v, key, 120 if key == "name" else 5000)
        elif key == "tags":
            if not isinstance(v, list) or len(v) > 20:
                raise ValueError("tags must be a list of at most 20")
            tags = []
            for t in v:
                t = _clean_text(t, "tag", 40)
                if not _TAG_RE.match(t):
                    raise ValueError(f"tag {t!r}: letters, digits, space and _ . + - only")
                if t not in tags:
                    tags.append(t)
            out[key] = tags
        elif key in ("favorite", "reference", "archived"):
            if not isinstance(v, bool):
                raise ValueError(f"{key} must be true or false")
            out[key] = v
        elif key == "discarded":
            if v is None or v is False:
                out[key] = None
            else:
                reason = _clean_text((v or {}).get("reason") if isinstance(v, dict) else v, "discard reason", 1000)
                if not reason:
                    raise ValueError("discarding a session for analysis needs a reason")
                out[key] = {"reason": reason, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        elif key in ("condition", "receiver"):
            if v is None:
                out[key] = None
                continue
            if not isinstance(v, dict):
                raise ValueError(f"{key} must be an object with value and note")
            value = _clean_text(v.get("value", ""), key, 80)
            if key == "condition" and value not in CONDITIONS:
                raise ValueError(f"condition must be one of {', '.join(CONDITIONS)}")
            if not value:
                raise ValueError(f"{key} value is required")
            out[key] = {"value": value, "note": _clean_text(v.get("note", ""), f"{key} note", 1000),
                        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        elif key == "preferred_reduce":
            if v is not None and (session is None or v not in session.get("reduce", {}).get("runs", [])):
                raise ValueError("preferred_reduce must be one of this session's own REDUCE runs")
            out[key] = v
        elif key == "preferred_science":
            if not isinstance(v, dict):
                raise ValueError("preferred_science must map a REDUCE run to one of its SCIENCE runs")
            sci = {s["run_id"]: s for s in index.get("science_runs", [])}
            red = {r["run_id"]: r for r in index.get("reduce_runs", [])}
            for rid, sid in v.items():
                if session is None or rid not in session.get("reduce", {}).get("runs", []):
                    raise ValueError(f"{rid} is not one of this session's REDUCE runs")
                if sid is not None and (sid not in sci or sci[sid].get("reduce_dir") != red[rid]["dir"]):
                    raise ValueError(f"{sid} is not a SCIENCE run of {rid}")
            out[key] = dict(v)
        elif key == "confirmed_links":
            if not isinstance(v, list) or len(v) > 50:
                raise ValueError("confirmed_links must be a list")
            known = {p["dir"] for p in index.get("pending_associations", [])}
            links = []
            for item in v:
                d = _clean_text((item or {}).get("dir", ""), "link dir", 300)
                if d not in known:
                    raise ValueError(f"{d} is not a pending association")
                links.append({"dir": d, "note": _clean_text((item or {}).get("note", ""), "link note", 500)})
            out[key] = links
        else:
            raise ValueError(f"unknown field {key}")
    return out


def annotate(campaign_id: str, patch: Dict[str, Any], roots: Optional[Roots] = None, by: str = "operator") -> Dict[str, Any]:
    """Merge the operator's changes into annotations.json (atomic) and append them to annotations_log.jsonl. Capture
    and product files are never touched. An annotation may name a campaign that is no longer on disk (kept, orphaned)."""
    roots = roots or default_roots()
    if not isinstance(campaign_id, str) or not _ID_RE.match(campaign_id):
        raise ValueError("invalid session id")
    index = load_index(roots)
    session = next((s for s in index["sessions"] if s["campaign_id"] == campaign_id), None)
    if session is None:
        raise FileNotFoundError("unknown session")
    clean = _validate_patch(patch, session, index)
    with _ANNOTATION_LOCK:
        data = load_annotations(roots)
        entry = dict(data["sessions"].get(campaign_id) or {})
        before = {k: entry.get(k) for k in clean}
        entry.update(clean)
        entry["updated_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data["sessions"][campaign_id] = entry
        data["schema_version"] = ANNOTATION_SCHEMA
        _write_atomic(_annotations_path(roots), data)
        roots.store.mkdir(parents=True, exist_ok=True)
        with (roots.store / "annotations_log.jsonl").open("a") as fh:
            fh.write(json.dumps({"utc": entry["updated_utc"], "by": by, "campaign_id": campaign_id, "before": before, "after": clean},
                                default=str) + "\n")
    return entry


# ------------------------------------------------------------------ listing (catalog page)
def _merged(session: Dict[str, Any], ann: Dict[str, Any]) -> Dict[str, Any]:
    a = ann.get(session["campaign_id"]) or {}
    row = dict(session)
    row["annotation"] = a
    row["display_name"] = a.get("name") or session.get("observation_name") or session["campaign_id"]
    cond = a.get("condition")
    if cond:
        row["condition"] = {"value": cond["value"], "source": "operator note" + (f": {cond['note']}" if cond.get("note") else ""),
                            "documented_by": "operator", "recorded": session.get("condition")}
    rec = a.get("receiver")
    if rec:
        row["receiver"] = {**(session.get("receiver") or {}), "value": rec["value"], "documented_by": "operator", "note": rec.get("note")}
    return row


SORTS = {"started": lambda r: r.get("started_utc") or "", "name": lambda r: str(r.get("display_name") or "").lower(),
         "points": lambda r: r.get("points_captured") or 0,
         "size": lambda r: ((r.get("size") or {}).get("campaign") or {}).get("bytes_on_disk", 0)}


def list_sessions(params: Dict[str, str], roots: Optional[Roots] = None) -> Dict[str, Any]:
    roots = roots or default_roots()
    index = load_index(roots)
    ann = load_annotations(roots)["sessions"]
    rows = [_merged(s, ann) for s in index["sessions"]]
    q = (params.get("q") or "").strip().lower()
    if q:
        def hay(r):
            a = r.get("annotation") or {}
            return " ".join(str(x) for x in (r["campaign_id"], r.get("display_name"), r.get("observation_name"), a.get("notes"),
                                             " ".join(a.get("tags") or []), " ".join(r.get("capture_session_ids") or []))).lower()
        rows = [r for r in rows if q in hay(r)]
    for key, getter in (("state", lambda r: (r.get("capture") or {}).get("state")),
                        ("condition", lambda r: (r.get("condition") or {}).get("value")),
                        ("kind", lambda r: r.get("kind")),
                        ("reduce", lambda r: (r.get("reduce") or {}).get("state")),
                        ("science", lambda r: (r.get("science") or {}).get("state"))):
        want = params.get(key)
        if want:
            rows = [r for r in rows if getter(r) == want]
    tag = params.get("tag")
    if tag:
        rows = [r for r in rows if tag in ((r.get("annotation") or {}).get("tags") or [])]
    if params.get("favorite") == "1":
        rows = [r for r in rows if (r.get("annotation") or {}).get("favorite")]
    archived = params.get("archived", "0")
    if archived == "0":
        rows = [r for r in rows if not (r.get("annotation") or {}).get("archived")]
    elif archived == "only":
        rows = [r for r in rows if (r.get("annotation") or {}).get("archived")]
    discarded = params.get("discarded", "all")
    if discarded == "0":
        rows = [r for r in rows if not (r.get("annotation") or {}).get("discarded")]
    elif discarded == "only":
        rows = [r for r in rows if (r.get("annotation") or {}).get("discarded")]
    sort = params.get("sort", "started")
    rows.sort(key=SORTS.get(sort, SORTS["started"]), reverse=params.get("order", "desc") != "asc")
    try:
        per_page = max(5, min(100, int(params.get("per_page", "25"))))
        page = max(1, int(params.get("page", "1")))
    except ValueError:
        raise ValueError("page and per_page must be integers") from None
    total = len(rows)
    tags = sorted({t for a in ann.values() for t in (a.get("tags") or [])})
    orphans = sorted(set(ann) - {s["campaign_id"] for s in index["sessions"]})
    return {"generated_utc": index["generated_utc"], "build_seconds": index.get("build_seconds"), "size_method": index["size_method"],
            "total": total, "page": page, "per_page": per_page, "pages": max(1, -(-total // per_page)),
            "sessions": rows[(page - 1) * per_page: page * per_page], "tags": tags, "orphan_annotations": orphans,
            "pending_associations": len(index.get("pending_associations", [])), "all_sessions": len(index["sessions"])}


# ------------------------------------------------------------------ one session in full (viewer)
def _campaign_dir(campaign_id: str, roots: Roots) -> Path:
    if not isinstance(campaign_id, str) or not _ID_RE.match(campaign_id) or campaign_id in (".", ".."):
        raise ValueError("invalid session id")
    camp = roots.mosaic / campaign_id
    if not camp.is_dir():
        raise FileNotFoundError("unknown session")
    return camp


def _file_state(path: Path, roots: Roots) -> Dict[str, Any]:
    try:
        st = path.stat()
        return {"path": _rel(path, roots), "state": "OK", "bytes": st.st_size}
    except OSError:
        return {"path": _rel(path, roots), "state": MISSING, "bytes": None}


QUICKLOOK_PRODUCTS = (("session_waterfall.png", "session waterfall"), ("quicklook_map.png", "native grid map"),
                      ("quicklook_map_interpolated.png", "interpolated preview"), ("latest_spectrum.png", "last spectrum"),
                      ("latest_waterfall.png", "last waterfall"), ("latest_fractional_excess.png", "last fractional excess"))


def _incidents(camp: Path, rows: List[Dict[str, str]], stop_report: Any, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    if isinstance(stop_report, dict):
        ms = stop_report.get("mount_stop") or {}
        out.append({"utc": stop_report.get("stop_requested_utc"), "kind": "STOP", "source": "stop_report.json",
                    "detail": f"phase {stop_report.get('phase_at_stop')}, point {stop_report.get('interrupted_point')}: "
                              f"{stop_report.get('interrupted_point_outcome')}; mount stop "
                              f"{'confirmed' if ms.get('confirmed') else 'NOT confirmed'}"})
    for r in rows:
        status = (r.get("capture_status") or "").strip()
        if r.get("error_message") or r.get("error_code") or status in ("failed", "error"):
            out.append({"utc": r.get("failed_at") or r.get("end_time") or r.get("start_time"), "kind": "POINT",
                        "source": "mosaic.csv", "detail": f"point {r.get('point_id')}: {status} {r.get('error_code') or ''} "
                                                          f"{r.get('error_message') or r.get('error_detail') or ''}".strip()})
    if profile.get("state") == "INVALIDATED":
        inv = profile.get("invalidated") or {}
        out.append({"utc": inv.get("invalidated_utc"), "kind": "PROFILE", "source": "<profile>.INVALIDATED.json",
                    "detail": f"calibration profile invalidated: {inv.get('reason')}"})
    try:
        with (camp / "orchestrator_capture.log").open(errors="replace") as fh:
            for line in fh:
                if re.search(r"\b(ERROR|CRITICAL|paused by|safety policy|ABORT)", line) and len(out) < 200:
                    m = re.match(r"^\[([0-9-]+ [0-9:.]+)\]", line)
                    out.append({"utc": m.group(1) + " (log clock)" if m else None, "kind": "LOG", "source": "orchestrator_capture.log",
                                "detail": line.strip()[:300]})
    except OSError:
        pass
    return out


def session_detail(campaign_id: str, roots: Optional[Roots] = None) -> Dict[str, Any]:
    """Everything stored about one session, read now from its own files (the index only supplies the run links).
    Never starts anything; a missing or unreadable file is reported as such, never filled in."""
    roots = roots or default_roots()
    camp = _campaign_dir(campaign_id, roots)
    index = load_index(roots)
    ann = load_annotations(roots)["sessions"]
    rec = next((s for s in index["sessions"] if s["campaign_id"] == campaign_id), None)
    if rec is None:          # created after the last index build: describe it now, without writing the index
        rec = campaign_record(camp, roots, index["reduce_runs"], index["science_runs"], set())
    row = _merged(rec, ann)
    plan, plan_state = _read_json(camp / "observation_resolved.json")
    plan = plan if isinstance(plan, dict) else {}
    grid_meta, gm_state = _read_json(camp / "grid_metadata.json")
    stop_report, _ = _read_json(camp / "stop_report.json")
    rows, rows_state = _mosaic_rows(camp)
    requested = plan.get("requested") or {}
    profile = _profile_info(roots, (plan.get("quicklook") or {}).get("calibration_profile_path"))
    profile_meta = None
    if profile["state"] in ("OK", "INVALIDATED"):
        p = roots.base / profile["path"]
        meta, _ = _read_json(p.with_suffix(".json") if p.suffix != ".json" else p, 5_000_000)
        if isinstance(meta, dict):
            profile_meta = {k: meta.get(k) for k in ("created_utc", "center_frequency_hz", "sample_rate_hz", "gain_db", "profile_id",
                                                     "reference_kind", "connection_point", "calibration_level", "absolute_calibration")}
            main = plan.get("main") or {}
            profile_meta["compatibility"] = {
                "center_frequency": None if meta.get("center_frequency_hz") is None or main.get("center_frequency_hz") is None
                else float(meta["center_frequency_hz"]) == float(main["center_frequency_hz"]),
                "sample_rate": None if meta.get("sample_rate_hz") is None or main.get("sample_rate") is None
                else float(meta["sample_rate_hz"]) == float(main["sample_rate"]),
                "gain": None if meta.get("gain_db") is None or main.get("gain_db") is None
                else float(meta["gain_db"]) == float(main["gain_db"]),
            }
    reduce_runs = [r for r in index["reduce_runs"] if r["run_id"] in rec.get("reduce", {}).get("runs", [])]
    reduce_dirs = {r["dir"] for r in reduce_runs}
    science_runs = [s for s in index["science_runs"] if s.get("reduce_dir") in reduce_dirs]
    a = row.get("annotation") or {}
    for r in reduce_runs:
        r["preferred"] = a.get("preferred_reduce") == r["run_id"]
        qc = {}
        for name in ("quality_summary.json", "velocity_summary.json", "calibration_summary.json", "mask_occupancy.json"):
            v, st = _read_json(roots.base / r["dir"] / "qc" / name, 5_000_000)
            qc[name] = v if st == "OK" else st
        r["qc"] = qc
        r["compatibility_record"] = _file_state(roots.base / r["dir"] / "calibration_compatibility_record.json", roots)
        r["science_runs"] = [s["run_id"] for s in science_runs if s["reduce_dir"] == r["dir"]]
    pref_sci = a.get("preferred_science") or {}
    for s in science_runs:
        rid = next((r["run_id"] for r in reduce_runs if r["dir"] == s["reduce_dir"]), None)
        s["reduce_run_id"] = rid
        s["preferred"] = pref_sci.get(rid) == s["run_id"]
        s["notes_file"] = _file_state(roots.base / s["dir"] / "maps" / "NOTES.md", roots)
    confirmed = {c["dir"] for c in a.get("confirmed_links") or []}
    pending = [dict(p, confirmed=p["dir"] in confirmed) for p in index.get("pending_associations", [])
               if campaign_id in p.get("candidates", []) or p["dir"] in confirmed]
    ql = camp / "quicklook"
    quicklook = {"state": "OK" if ql.is_dir() else NOT_RUN,
                 "products": [dict(_file_state(ql / n, roots), label=label) for n, label in QUICKLOOK_PRODUCTS] if ql.is_dir() else [],
                 "stack3d": _file_state(ql / "session_waterfall_state.json", roots) if ql.is_dir() else None,
                 "validation": _read_json(ql / "quicklook_live_validation.json")[0] if ql.is_dir() else None,
                 "note": "stored as generated at capture time; views produced before BL-063/BL-067 (2026-10-14) keep their old look"}
    return {
        **row,
        "sections": {
            "summary": {"objective": requested.get("session"), "notes": a.get("notes"), "condition": row.get("condition"),
                        "duration_s": rec.get("duration_s"), "stages": {"capture": rec.get("capture"), "calibration": rec.get("calibration"),
                                                                       "reduce": rec.get("reduce"), "science": rec.get("science")},
                        "incidents": _incidents(camp, rows, stop_report, profile)},
            "observation": {"plan_state": plan_state, "requested": requested, "effective": {
                "main": plan.get("main"), "rfi_ref": plan.get("rfi_ref"), "resolved": plan.get("resolved"),
                "duration": plan.get("duration"), "visibility": plan.get("visibility"), "safety_policy": plan.get("safety_policy"),
                "software_versions": plan.get("software_versions"), "observation_config_sha256": plan.get("observation_config_sha256")},
                "site": (plan.get("observer_config") or {}).get("observer") or None,
                "site_info": (plan.get("observer_config") or {}).get("site_info"),
                "grid_metadata": grid_meta if gm_state == "OK" else gm_state, "mount": rec.get("mount"),
                "rfi_ref_dir": _file_state(camp / "rfi_ref", roots), "gain_pilot": _read_json(camp / "gain_pilot_state.json")[0],
                "note": "requested = what the operator asked; effective = what the plan resolved and ran with"},
            "calibration": {"profile": profile, "profile_metadata": profile_meta},
            "quicklook": quicklook,
            "reduce": {"runs": reduce_runs},
            "science": {"runs": science_runs, "pending_associations": pending},
            "instrument": {"temperature_source": _rel(roots.temperature, roots) + "/YYYYMMDD.jsonl (temperature_logger.py, "
                                                 "external to the session; per-capture reads live in each HDF5's attributes)",
                           "receiver": row.get("receiver")},
            "files": {"size": rec.get("size"), "size_method": index["size_method"]},
        },
        "points_state": rows_state,
    }


def session_points(campaign_id: str, roots: Optional[Roots] = None) -> Dict[str, Any]:
    roots = roots or default_roots()
    camp = _campaign_dir(campaign_id, roots)
    rows, state = _mosaic_rows(camp)
    keep = ("point_id", "scan_order", "grid_row", "grid_col", "ra", "dec", "capture_status", "start_time", "end_time",
            "data_filename", "altitude_deg_at_goto", "azimuth_deg_at_goto", "visibility_deferred", "error_message")
    points = [{k: r.get(k) for k in keep} for r in rows]
    return {"state": state, "points": points}


def _h5_for_point(camp: Path, point_id: str) -> Optional[Path]:
    try:
        n = int(point_id)
    except (TypeError, ValueError):
        return None
    for d in _iq_dirs(camp):
        for f in sorted(d.glob(f"*_{n:04d}.h5")):
            return f
    return None


def point_detail(campaign_id: str, point_id: str, roots: Optional[Roots] = None) -> Dict[str, Any]:
    """One point: its mosaic.csv row, the products stored for it, and which REDUCE runs wrote it. The capture's HDF5
    is located but not opened (its attributes are read only on an explicit request: capture_attributes())."""
    roots = roots or default_roots()
    camp = _campaign_dir(campaign_id, roots)
    rows, state = _mosaic_rows(camp)
    row = next((r for r in rows if str(r.get("point_id")) == str(point_id)), None)
    if row is None:
        raise FileNotFoundError("unknown point")
    h5 = _h5_for_point(camp, point_id)
    qp = camp / "quicklook" / "points" / str(point_id)
    products = sorted(_rel(p, roots) for p in qp.rglob("*") if p.is_file())[:50] if qp.is_dir() else []
    index = load_index(roots)
    rec = next((s for s in index["sessions"] if s["campaign_id"] == campaign_id), {})
    reduce_points = []
    for r in index["reduce_runs"]:
        if r["run_id"] in (rec.get("reduce") or {}).get("runs", []):
            d = roots.base / r["dir"] / "points" / str(int(point_id))
            reduce_points.append({"run_id": r["run_id"], "dir": r["dir"], "state": "OK" if (d / "master_spectrum.json").is_file() else MISSING})
    return {"row": row, "capture": _file_state(h5, roots) if h5 else {"path": None, "state": MISSING if row.get("capture_status") == "success"
                                                                     else NOT_RUN, "bytes": None},
            "quicklook_products": products, "reduce_points": reduce_points}


# ------------------------------------------------------------------ temperatures (external log, shown with its coverage)
def session_temperatures(campaign_id: str, roots: Optional[Roots] = None, pad_minutes: float = 10.0) -> Dict[str, Any]:
    roots = roots or default_roots()
    camp = _campaign_dir(campaign_id, roots)
    rows, _ = _mosaic_rows(camp)
    times = [t for r in rows for t in (_parse_utc(r.get("start_time")), _parse_utc(r.get("end_time"))) if t]
    stop, _ = _read_json(camp / "stop_report.json")
    if isinstance(stop, dict) and _parse_utc(stop.get("recorded_utc")):
        times.append(_parse_utc(stop["recorded_utc"]))
    source = _rel(roots.temperature, roots) + "/YYYYMMDD.jsonl (temperature_logger.py)"
    if not times:
        return {"state": NOT_RUN, "detail": "no capture times recorded: no window to look up", "source": source, "series": {}}
    t0, t1 = min(times), max(times)
    lo, hi = t0 - timedelta(minutes=pad_minutes), t1 + timedelta(minutes=pad_minutes)
    series: Dict[str, List[Tuple[str, Optional[float]]]] = {}
    samples = 0
    in_window = 0
    day = lo.date()
    while day <= hi.date():
        path = roots.temperature / f"{day.strftime('%Y%m%d')}.jsonl"
        try:
            with path.open(errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    t = _parse_utc(rec.get("utc"))
                    if t is None or t < lo or t > hi:
                        continue
                    samples += 1
                    if t0 - timedelta(seconds=30) <= t <= t1 + timedelta(seconds=30):   # the logger samples once a minute
                        in_window += 1
                    for role, r in (rec.get("readings") or {}).items():
                        series.setdefault(role, []).append((t.isoformat(timespec="seconds"), (r or {}).get("c")))
        except OSError:
            pass
        day += timedelta(days=1)
    for role, pts in series.items():
        if len(pts) > TEMPERATURE_MAX_POINTS:
            step = -(-len(pts) // TEMPERATURE_MAX_POINTS)
            series[role] = pts[::step]
    expected = max(1, -(-int((t1 - t0).total_seconds() + 60) // 60))      # one a minute over the same ±30 s window
    stats = {}
    for role, pts in series.items():
        vals = [v for _, v in pts if isinstance(v, (int, float))]
        stats[role] = {"min": min(vals) if vals else None, "max": max(vals) if vals else None,
                       "first": vals[0] if vals else None, "last": vals[-1] if vals else None,
                       "valid": len(vals), "samples": len(pts)}
    return {"state": "OK" if samples else MISSING, "source": source,
            "detail": None if samples else "the temperature log has no samples for this session (the logger started 2026-10-17 "
                                           "and keeps 60 days)",
            "window": {"session_start_utc": t0.isoformat(), "session_end_utc": t1.isoformat(), "pad_minutes": pad_minutes},
            "coverage": {"samples_in_session": in_window, "expected_one_per_minute": expected, "edge_tolerance_s": 30,
                         "fraction": round(min(1.0, in_window / expected), 3)},
            "series": series, "stats": stats}


# ------------------------------------------------------------------ files
def session_files(campaign_id: str, sub: str = "", roots: Optional[Roots] = None) -> Dict[str, Any]:
    """One directory level of the campaign: files (size, servable or not) and sub-directories (count + size)."""
    roots = roots or default_roots()
    camp = _campaign_dir(campaign_id, roots)
    target = (camp / sub).resolve() if sub else camp.resolve()
    if target != camp.resolve() and camp.resolve() not in target.parents:
        raise ValueError("path outside the session")
    if not target.is_dir():
        raise FileNotFoundError("no such directory")
    files, dirs = [], []
    for e in sorted(os.scandir(target), key=lambda e: e.name):
        if e.is_dir(follow_symlinks=False):
            u = disk_usage(Path(e.path))
            dirs.append({"name": e.name, "files": u["files"], "bytes_on_disk": u["bytes_on_disk"]})
        elif e.is_file(follow_symlinks=False) and len(files) < MAX_FILES_LISTED:
            st = e.stat(follow_symlinks=False)
            files.append({"name": e.name, "path": _rel(Path(e.path), roots), "bytes": st.st_size,
                          "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
                          "downloadable": Path(e.name).suffix.lower() in DOWNLOADABLE and st.st_size <= 25 * 1024 * 1024})
    rel = str(target.relative_to(camp.resolve())) if target != camp.resolve() else ""
    return {"dir": rel, "files": files, "dirs": dirs, "truncated": len(files) >= MAX_FILES_LISTED,
            "products": [{"kind": r["kind"], "run_id": r["run_id"], "dir": r["dir"], **disk_usage(roots.base / r["dir"])}
                         for r in _session_runs(campaign_id, roots)] if not rel else []}


DOWNLOADABLE = {".png", ".json", ".csv", ".txt", ".log", ".md", ".svg", ".pdf"}   # almita_web_ops.SERVE_SUFFIXES: never .h5 (IQ)


def _session_runs(campaign_id: str, roots: Roots) -> List[Dict[str, Any]]:
    index = load_index(roots)
    rec = next((s for s in index["sessions"] if s["campaign_id"] == campaign_id), {})
    reduce_ids = set((rec.get("reduce") or {}).get("runs", []))
    science_ids = set((rec.get("science") or {}).get("runs", []))
    return [r for r in index["reduce_runs"] if r["run_id"] in reduce_ids] + [s for s in index["science_runs"] if s["run_id"] in science_ids]


# ------------------------------------------------------------------ export (BL-088)
def _exports_root(roots: Roots) -> Path:
    return roots.store / "exports"


def _fmt(v: Any) -> str:
    return "—" if v is None or v == "" else str(v)


def export_report(campaign_id: str, roots: Optional[Roots] = None, include_iq: bool = False) -> Dict[str, Any]:
    """A readable report.md + a structured manifest.json + the stored figures, in data/sessions/exports/<id>-<utc>/ and
    one .zip of them. Never IQ (include_iq is refused in this version), never anything under an authentication,
    key or token name, never a file outside the session's own campaign and runs. Nothing is published or uploaded."""
    roots = roots or default_roots()
    if include_iq:
        raise ValueError("exporting IQ is not available: the report is meant to travel without the captures")
    d = session_detail(campaign_id, roots)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", campaign_id)
    out = _exports_root(roots) / f"{safe_id}-{stamp}"
    (out / "figures").mkdir(parents=True, exist_ok=False)
    figures: List[Dict[str, Any]] = []

    def add_figure(src: Path, label: str, origin: str) -> None:
        if not src.is_file() or _SECRET_NAME_RE.search(src.name) or src.suffix.lower() not in (".png", ".svg"):
            return
        if src.stat().st_size > 10 * 1024 * 1024:
            return
        name = f"{len(figures) + 1:02d}_{re.sub(r'[^A-Za-z0-9_.-]', '_', src.name)}"
        shutil.copyfile(src, out / "figures" / name)
        figures.append({"file": f"figures/{name}", "label": label, "source": _rel(src, roots), "origin": origin,
                        "sha256": _sha256(src)})

    camp = roots.base / d["dir"]
    for prod in d["sections"]["quicklook"]["products"]:
        if prod["state"] == "OK":
            add_figure(roots.base / prod["path"], prod["label"], "QUICKLOOK")
    add_figure(camp / "grid_plan.png", "grid plan", "OBSERVE plan")
    sci = d["sections"]["science"]["runs"]
    chosen = [s for s in sci if s.get("preferred")] or sci[-1:]
    for s in chosen:
        for m in ("map_abc_combined.png", "map_coverage.png", "map_snr.png"):
            add_figure(roots.base / s["dir"] / "maps" / m, f"SCIENCE {s['run_id']} {m}", "SCIENCE")
    sec = d["sections"]
    eff = sec["observation"]["effective"]
    main = eff.get("main") or {}
    warnings = []
    if d.get("condition", {}).get("value") == "UNKNOWN":
        warnings.append("Sky condition not recorded: open sky or indoor is unknown.")
    if not (d.get("receiver") or {}).get("value"):
        warnings.append("Receiver not documented by the operator; observer_config's sdr_device is only a configuration label.")
    if sec["calibration"]["profile"]["state"] != "OK":
        warnings.append(f"Calibration profile: {sec['calibration']['profile']['state']} - {sec['calibration']['profile'].get('detail')}")
    for s in sci:
        if s.get("link_state") == "INCONSISTENT":
            warnings.append(f"SCIENCE {s['run_id']}: {s['link_detail']}")
    if (d.get("annotation") or {}).get("discarded"):
        warnings.append("Discarded for analysis by the operator: " + d["annotation"]["discarded"]["reason"])
    lines = [f"# ALMITA session {campaign_id}", "",
             f"Exported {datetime.now(timezone.utc).isoformat(timespec='seconds')} from the stored files; nothing was recomputed.",
             "IQ captures are not included.", "",
             "## Identity", "",
             f"- Name: {_fmt(d.get('display_name'))}", f"- Campaign directory: `{d['dir']}`",
             f"- Capture session ids: {_fmt(', '.join(d.get('capture_session_ids') or []))}",
             f"- Started: {_fmt(d.get('started_utc'))} (Chile: {_fmt(d.get('started_chile'))})",
             f"- Condition: {_fmt((d.get('condition') or {}).get('value'))} ({_fmt((d.get('condition') or {}).get('source'))})",
             f"- Receiver: {_fmt((d.get('receiver') or {}).get('value'))}", f"- Mount: {_fmt(d.get('mount'))}", "",
             "## Observation", "",
             f"- Capture: {_fmt(d['capture']['state'])} - {_fmt(d['capture'].get('detail'))}",
             f"- Points captured / planned: {_fmt(d.get('points_captured'))} / {_fmt(d.get('points_planned'))}",
             f"- Centre frequency: {_fmt(main.get('center_frequency_hz'))} Hz; sample rate {_fmt(main.get('sample_rate'))} sps; "
             f"gain {_fmt(main.get('gain_db'))} dB; Bias-T {_fmt(main.get('bias_tee'))}",
             f"- Grid: {_fmt(json.dumps(d.get('grid')))}", "",
             "## Calibration", "", f"- Profile: `{_fmt(sec['calibration']['profile']['path'])}` ({sec['calibration']['profile']['state']})", "",
             "## REDUCE runs", ""]
    for r in sec["reduce"]["runs"]:
        lines.append(f"- {r['run_id']}{' (preferred)' if r.get('preferred') else ''}: {r['status']}, frame {_fmt(r.get('velocity_frame'))}, "
                     f"git {_fmt(r.get('git_commit'))}, config {_fmt((r.get('config_hash') or '')[:12])}")
    if not sec["reduce"]["runs"]:
        lines.append("- none (NOT_RUN)")
    lines += ["", "## SCIENCE runs", ""]
    for s in sci:
        lines.append(f"- {s['run_id']}{' (preferred)' if s.get('preferred') else ''} on {_fmt(s.get('reduce_run_id'))}: {s['status']}, "
                     f"completeness {_fmt(s.get('data_completeness'))}, link {s['link_state']}")
    if not sci:
        lines.append("- none (NOT_RUN)")
    lines += ["", "## Incidents", ""] + ([f"- {_fmt(i.get('utc'))} [{i['kind']}] {i['detail']}" for i in sec["summary"]["incidents"][:60]]
                                         or ["- none recorded in the session's files"])
    lines += ["", "## Notes", "", _fmt((d.get("annotation") or {}).get("notes")), "", "## Warnings", ""]
    lines += [f"- {w}" for w in warnings] or ["- none"]
    lines += ["", "## Figures", ""] + [f"- `{f['file']}`: {f['label']} (from `{f['source']}`)" for f in figures]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    manifest = {"schema_version": 1, "kind": "ALMITA_SESSION_EXPORT", "campaign_id": campaign_id,
                "exported_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "includes_iq": False,
                "identity": {k: d.get(k) for k in ("display_name", "dir", "observation_name", "capture_session_ids", "iq_session_dirs",
                                                   "started_utc", "started_chile", "kind")},
                "condition": d.get("condition"), "receiver": d.get("receiver"), "mount": d.get("mount"),
                "configuration": {"requested": sec["observation"]["requested"], "effective": eff},
                "calibration": sec["calibration"], "capture": d["capture"], "point_counts": d.get("point_counts"),
                "reduce_runs": [{k: r.get(k) for k in ("run_id", "dir", "status", "git_commit", "config_hash", "calibration_profile_hash",
                                                       "velocity_frame", "counts", "quality_counts", "preferred")} for r in sec["reduce"]["runs"]],
                "science_runs": [{k: s.get(k) for k in ("run_id", "dir", "status", "reduce_run_id", "git_commit", "config_hash",
                                                        "data_completeness", "link_state", "link_detail", "preferred")} for s in sci],
                "annotation": {k: (d.get("annotation") or {}).get(k) for k in ("name", "notes", "tags", "discarded", "reference")},
                "incidents": sec["summary"]["incidents"], "warnings": warnings, "figures": figures}
    _write_atomic(out / "manifest.json", manifest)
    zpath = out.with_suffix(".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, f"{out.name}/{p.relative_to(out)}")
    return {"dir": _rel(out, roots), "zip": _rel(zpath, roots), "report": _rel(out / "report.md", roots),
            "manifest": _rel(out / "manifest.json", roots), "figures": len(figures), "warnings": warnings,
            "bytes": zpath.stat().st_size}


def list_exports(campaign_id: str, roots: Optional[Roots] = None) -> List[Dict[str, Any]]:
    roots = roots or default_roots()
    root = _exports_root(roots)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", campaign_id)
    if not root.is_dir():
        return []
    return [{"zip": _rel(z, roots), "bytes": z.stat().st_size, "name": z.name}
            for z in sorted(root.glob(f"{glob_escape(safe_id)}-*.zip"), reverse=True)]


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)


def resolve_export(rel: str, roots: Optional[Roots] = None) -> Tuple[Path, str]:
    roots = roots or default_roots()
    if not isinstance(rel, str) or "\x00" in rel:
        raise ValueError("invalid path")
    p = (roots.base / rel).resolve()
    root = _exports_root(roots).resolve()
    if root not in p.parents or not p.is_file():
        raise FileNotFoundError(rel)
    types = {".zip": "application/zip", ".md": "text/markdown; charset=utf-8", ".json": "application/json",
             ".png": "image/png", ".svg": "image/svg+xml"}
    if p.suffix.lower() not in types:
        raise ValueError("file type not served")
    return p, types[p.suffix.lower()]


# ------------------------------------------------------------------ comparison (BL-090)
COMPARE_FIELDS = (("center_frequency_hz", "centre frequency (Hz)"), ("sample_rate", "sample rate (sps)"), ("gain_db", "gain (dB)"),
                  ("bias_tee", "Bias-T"))


def compare_sessions(a_id: str, b_id: str, roots: Optional[Roots] = None) -> Dict[str, Any]:
    """Side by side: configuration, condition, receiver, profile, grid, REDUCE frame and temperatures. Differences that
    stop a direct comparison are listed first; nothing is rescaled."""
    roots = roots or default_roots()
    a, b = session_detail(a_id, roots), session_detail(b_id, roots)
    blockers, rows, unknown = [], [], []

    def put(label: str, va: Any, vb: Any, blocking: bool) -> None:
        if va is None or vb is None or va == "UNKNOWN" or vb == "UNKNOWN":   # not recorded is never "the same"
            rows.append({"field": label, "a": va, "b": vb, "same": None, "blocking": False})
            unknown.append(label)
            return
        same = va == vb
        rows.append({"field": label, "a": va, "b": vb, "same": same, "blocking": blocking and not same})
        if blocking and not same:
            blockers.append(f"{label}: {va} vs {vb}")

    ma = (a["sections"]["observation"]["effective"].get("main") or {})
    mb = (b["sections"]["observation"]["effective"].get("main") or {})
    for key, label in COMPARE_FIELDS:
        put(label, ma.get(key), mb.get(key), key != "bias_tee")
    put("calibration profile", a["calibration"]["profile_path"], b["calibration"]["profile_path"], True)
    put("receiver", (a.get("receiver") or {}).get("value"), (b.get("receiver") or {}).get("value"), True)
    put("condition", (a.get("condition") or {}).get("value"), (b.get("condition") or {}).get("value"), False)
    put("mount", a.get("mount"), b.get("mount"), False)
    ga, gb = a.get("grid") or {}, b.get("grid") or {}
    for key in ("rows", "cols", "spacing_deg", "rotation_deg", "center_ra_hours", "center_dec_deg"):
        put(f"grid {key}", ga.get(key), gb.get(key), key in ("spacing_deg", "rotation_deg"))

    def frame(d):
        runs = d["sections"]["reduce"]["runs"]
        pref = next((r for r in runs if r.get("preferred")), runs[-1] if runs else None)
        return (pref or {}).get("velocity_frame"), (pref or {}).get("run_id")
    fa, ra = frame(a)
    fb, rb = frame(b)

    def science(d):
        runs = d["sections"]["science"]["runs"]
        pref = next((s for s in runs if s.get("preferred")), runs[-1] if runs else None)
        return {"run_id": pref["run_id"], "dir": pref["dir"], "maps": pref.get("maps") or []} if pref else None
    put("REDUCE velocity frame (preferred or newest run)", fa, fb, True)
    ta, tb = session_temperatures(a_id, roots), session_temperatures(b_id, roots)
    temps = {role: {"a": (ta.get("stats") or {}).get(role), "b": (tb.get("stats") or {}).get(role)}
             for role in sorted(set(ta.get("stats") or {}) | set(tb.get("stats") or {}))}
    return {"a": {"campaign_id": a_id, "name": a.get("display_name"), "reduce_run": ra, "science": science(a)},
            "b": {"campaign_id": b_id, "name": b.get("display_name"), "reduce_run": rb, "science": science(b)},
            "fields": rows, "blockers": blockers, "unknown": unknown, "direct_comparison": not blockers,
            "temperatures": temps, "temperature_coverage": {"a": ta.get("coverage"), "b": tb.get("coverage")},
            "note": "nothing is rescaled or recomputed; maps and spectra keep each run's own stored scale"}


def compare_runs(campaign_id: str, run_a: str, run_b: str, roots: Optional[Roots] = None) -> Dict[str, Any]:
    """Two REDUCE runs, or two SCIENCE runs, of one session: their recorded parameters side by side."""
    roots = roots or default_roots()
    d = session_detail(campaign_id, roots)
    runs = {r["run_id"]: r for r in d["sections"]["reduce"]["runs"] + d["sections"]["science"]["runs"]}
    if run_a not in runs or run_b not in runs:
        raise ValueError("both runs must belong to this session")
    ra, rb = runs[run_a], runs[run_b]
    if ra["kind"] != rb["kind"]:
        raise ValueError("compare two REDUCE runs or two SCIENCE runs, not one of each")
    keys = ("status", "git_commit", "pipeline_version", "config_hash", "calibration_profile_path", "calibration_profile_hash",
            "velocity_frame", "data_completeness", "reduce_run_id", "started_utc", "ended_utc", "points_written")
    fields = [{"field": k, "a": ra.get(k), "b": rb.get(k), "same": ra.get(k) == rb.get(k)} for k in keys if k in ra or k in rb]
    config_diff = []
    ca, _ = _read_json(roots.base / ra["dir"] / "config.json")
    cb, _ = _read_json(roots.base / rb["dir"] / "config.json")
    if isinstance(ca, dict) and isinstance(cb, dict):
        for k in sorted(set(ca) | set(cb)):
            if ca.get(k) != cb.get(k):
                config_diff.append({"key": k, "a": ca.get(k), "b": cb.get(k)})
    blockers = [f["field"] for f in fields if f["field"] in ("velocity_frame", "calibration_profile_hash") and not f["same"]]
    common: List[int] = []
    if ra["kind"] == "REDUCE":
        def pts(r):
            try:
                return {int(p.name) for p in (roots.base / r["dir"] / "points").iterdir() if p.name.isdigit() and (p / "master_spectrum.json").is_file()}
            except OSError:
                return set()
        common = sorted(pts(ra) & pts(rb))
    return {"kind": ra["kind"], "a": run_a, "b": run_b, "fields": fields, "config_diff": config_diff, "blockers": blockers,
            "dirs": {"a": ra["dir"], "b": rb["dir"]}, "common_points": common,
            "maps": {"a": ra.get("maps"), "b": rb.get("maps")} if ra["kind"] == "SCIENCE" else None}
