"""SESSIONS: delete regenerable products or whole sessions to recover disk space (BL-093).

Two steps, always: plan() builds a preview (what goes, what stays and why, real recoverable space) and a token that
fingerprints the exact inventory; execute() rebuilds the plan under the deletion lock, refuses if the token no longer
matches (the inventory changed or a pipeline job started), and only then removes files. Nothing is moved to a trash on
the same card: the goal is real free space.

What a session owns (session_catalog associations, never paths read out of manifests and walked):
  - its campaign directory data/mosaic/<campaign_id>/ (captures, QUICKLOOK, plan, logs). capture.py writes the raw IQ
    in <plan dir>/data/iq/<session>/, so a campaign's IQ normally live inside it (and gain_pilot_captures/ too);
  - raw IQ directories outside it, in data/iq/<dir>/ (capture.py's place when the plan CSV sits in data/), only when
    ownership is proven: a readable session_identity.json whose session_root is that directory and whose session_id
    is claimed by this campaign alone (capture_timing_<id>.csv, data/IQ/session.csv) and cited by no other campaign;
  - the REDUCE runs linked to it by evidence whose every capture_ref is inside that campaign (or its own outside IQ);
  - the SCIENCE WEB runs LINKED to one of those REDUCE runs.
Never deleted here: calibration profiles (shared), manual analyses (may hold unique work), SCIENCE runs whose link is
inconsistent or pending (ambiguous), the temperature log, exports and the SESSIONS store itself.

IQ outside a campaign that are shared, ambiguous (no identity, inconsistent identity, unclaimed) or protected stay
and the preview says why. Products mode never touches an IQ file.

Protected and not releasable from this flow: PRESERVED_CAMPAIGNS and everything derived from them, IQ included.
Every path removed is computed by the server from validated ids, must lie inside data/mosaic, data/reduced or
data/science (or data/iq, for raw IQ directories only) with no symbolic link on the way, and is removed without ever
following a link. After a whole session goes, its rows leave the capture control index data/IQ/session.csv (a copy of
the index is kept in the audit record first); every other row stays byte for byte."""
from __future__ import annotations

import csv
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import session_catalog as sc

MODES = ("products", "sessions")
CATEGORIES = ("QUICKLOOK", "REDUCE", "SCIENCE")
# Protected by name; there is no switch in the UI or the API to release them. Empty since 2026-10-21: the operator
# released the 400-point campaign of 2026-10-02 (indoor-era V4 data; only outdoor sessions matter from now on).
PRESERVED_CAMPAIGNS: Dict[str, str] = {}
PIPELINE_SCRIPTS = ("capture.py", "quicklook_live.py", "alignment.py", "calibrate_reference_wizard.py", "lobe_transit.py",
                    "observation_gain_pilot.py", "reduce_single_capture.py", "almita_reduce.py", "almita_science.py",
                    "science_web_bridge.py", "almita_observe.py", "hw_hi_night_scan.py", "rf_line_bench.py", "sdr_health.py")
EVIDENCE_FILES = ("config.json", "provenance.json", "manifest.json")      # kept in the audit record when a run is removed
EVIDENCE_MAX_BYTES = 2_000_000
LOCK_TIMEOUT_S = 10.0
CITATION_GLOBS = ("docs/**/*.md", "examples/*", "scripts/**/*.py", "*.md")
IQ_FILE_RE = re.compile(r"\.(h5|hdf5)(\.part|\.tmp)?$", re.IGNORECASE)     # raw IQ, finished or interrupted
PARTIAL_RE = re.compile(r"\.(part|tmp)$", re.IGNORECASE)
CLASSES = ("IQ_RAW", "CAPTURE_OTHER", "PRODUCTS")       # preview breakdown; an inode is counted once, in the first class
CITE_MAX_BYTES = 20_000_000


class DeletionBlocked(Exception):
    """The batch cannot run now (a job is running, a deletion is in progress)."""


class StalePreview(Exception):
    """The inventory or the pipeline state changed since the preview: a new preview is needed."""


# ------------------------------------------------------------------ the lock shared with job starts
def _lock_path(roots: sc.Roots) -> Path:
    """ALMITA_SESSIONS_DELETION_LOCK_PATH moves it (tests), like mount_lock's ALMITA_MOUNT_LOCK_PATH."""
    return Path(os.environ.get("ALMITA_SESSIONS_DELETION_LOCK_PATH") or roots.base / "data" / "runtime" / "sessions_deletion.lock")


@contextmanager
def _flock(roots: sc.Roots, timeout: float) -> Iterator[None]:
    path = _lock_path(roots)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() >= deadline:
                    raise DeletionBlocked("a SESSIONS deletion or a job start holds the lock: try again") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


@contextmanager
def job_start_gate(roots: Optional[sc.Roots] = None) -> Iterator[None]:
    """Held by every web job start (almita_web_ops.start, OBSERVE START) around its checks and its launch: a deletion
    and a start never interleave. A start never waits for a deletion: it is refused while one runs."""
    try:
        with _flock(roots or sc.default_roots(), timeout=0.5):
            yield
    except DeletionBlocked:
        raise DeletionBlocked("a SESSIONS deletion is in progress: start the job again when it finishes") from None


# ------------------------------------------------------------------ is the pipeline busy?
def _pipeline_processes() -> List[str]:
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        names = {os.path.basename(a.decode(errors="replace")) for a in argv if a}
        hit = names & set(PIPELINE_SCRIPTS)
        if hit:
            out.append(f"pid {d.name}: {', '.join(sorted(hit))}")
    return sorted(out)


def default_busy_probe() -> List[str]:
    """Reasons the pipeline is active now (any of them blocks every deletion): a running web job, an active
    observation, or a pipeline script running outside the web."""
    reasons: List[str] = []
    try:
        import almita_web_ops
        for j in almita_web_ops.running_jobs():
            reasons.append(f"job {j.get('job_id')} ({j.get('stage')}) is running")
    except Exception as exc:  # noqa: BLE001 - unknown state blocks, never allows
        reasons.append(f"could not read the job list ({type(exc).__name__})")
    try:
        import observation_orchestrator
        held = observation_orchestrator.active_campaign_reason(observation_orchestrator.get_status().get("orchestrator") or {})
        if held:
            reasons.append(f"an observation is active: {held}")
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"could not read the observation state ({type(exc).__name__})")
    reasons += [f"pipeline process {p}" for p in _pipeline_processes()]
    return reasons


# ------------------------------------------------------------------ safe paths and inventory
def _iq_root(roots: sc.Roots) -> Path:
    """capture.py's data/iq/ at the repository level: admitted for raw IQ directories only."""
    return roots.base / "data" / "iq"


def _capture_index_path(roots: sc.Roots) -> Path:
    """session_manager's control index of capture sessions (no IQ in it)."""
    return roots.base / "data" / "IQ" / "session.csv"


def _allowed_roots(roots: sc.Roots, kind: str) -> Tuple[Path, ...]:
    return (_iq_root(roots),) if kind == "IQ" else (roots.mosaic, roots.reduced, roots.science)


def _validate_target(path: Path, roots: sc.Roots, kind: str = "") -> Optional[str]:
    """None if `path` may be removed: a real directory inside an allowed root for its kind, with no symbolic link from
    the root down."""
    for root in _allowed_roots(roots, kind):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if not rel.parts or any(p in ("..", ".", "") for p in rel.parts):
            return "not a directory below an allowed data root"
        if root.is_symlink():
            return f"{sc._rel(root, roots)} is a symbolic link"
        cur = root
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                return f"{sc._rel(cur, roots)} is a symbolic link: never followed, never removed through"
        if not cur.is_dir():
            return "not an existing directory"
        if root.resolve() not in cur.resolve().parents:
            return "resolves outside its data root"
        return None
    return "outside data/iq" if kind == "IQ" else "outside data/mosaic, data/reduced and data/science"


def _file_class(kind: str, item_path: Path, file_path: str) -> str:
    """IQ_RAW: .h5/.hdf5 (and their .part/.tmp) of a capture area; PRODUCTS: QUICKLOOK, REDUCE, SCIENCE; the rest of a
    capture (plan, logs, identity, timing, session.csv...) is CAPTURE_OTHER."""
    if kind in ("REDUCE", "SCIENCE"):                    # their .h5 are reduced spectra, not captures
        return "PRODUCTS"
    rel = Path(file_path).relative_to(item_path)
    if IQ_FILE_RE.search(rel.name):
        return "IQ_RAW"
    return "PRODUCTS" if kind == "QUICKLOOK" or (kind == "CAMPAIGN" and rel.parts[0] == "quicklook") else "CAPTURE_OTHER"


def _inventory(path: Path, kind: str = "") -> Dict[str, Any]:
    """Every entry under `path` by lstat (links never followed): files (with inode data and class), symlinks, directories."""
    files, links, dirs = [], [], [str(path)]
    for top, dnames, fnames in os.walk(path, topdown=True, followlinks=False):
        for name in list(dnames):
            p = os.path.join(top, name)
            if os.path.islink(p):
                links.append(p)
                dnames.remove(name)
            else:
                dirs.append(p)
        for name in fnames:
            p = os.path.join(top, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if os.path.islink(p):
                links.append(p)
                continue
            files.append({"path": p, "size": st.st_size, "blocks": getattr(st, "st_blocks", 0) * 512 or st.st_size,
                          "dev": st.st_dev, "ino": st.st_ino, "nlink": st.st_nlink, "mtime_ns": st.st_mtime_ns,
                          "cls": _file_class(kind, path, p)})
    return {"files": files, "links": links, "dirs": dirs}


def _space(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Recoverable bytes over the whole batch: an inode frees its blocks only when every one of its hard links is in
    the batch; otherwise it stays alive and counts as kept."""
    seen: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for it in items:
        for f in it["inventory"]["files"]:
            k = (f["dev"], f["ino"])
            s = seen.setdefault(k, {"blocks": f["blocks"], "nlink": f["nlink"], "count": 0})
            s["count"] += 1
    recoverable = sum(s["blocks"] for s in seen.values() if s["count"] >= s["nlink"])
    kept = [s for s in seen.values() if s["count"] < s["nlink"]]
    return {"recoverable_bytes": recoverable, "unique_files": len(seen),
            "kept_alive_by_outside_hard_links": len(kept), "kept_alive_bytes": sum(s["blocks"] for s in kept),
            "estimate_note": "allocated blocks of each unique inode; the real change of free space is measured after "
                             "deletion and can differ (filesystem metadata, files written meanwhile)"}


def _breakdown(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Raw IQ, other capture files and products, each unique inode counted once (in the first class of CLASSES that
    holds it), with what each class really frees given the hard links of the whole batch."""
    seen: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for it in items:
        for f in it["inventory"]["files"]:
            s = seen.setdefault((f["dev"], f["ino"]), {"blocks": f["blocks"], "nlink": f["nlink"], "count": 0, "cls": f["cls"],
                                                       "partial": False})
            s["count"] += 1
            s["cls"] = min(s["cls"], f["cls"], key=CLASSES.index)
            s["partial"] = s["partial"] or bool(f["cls"] == "IQ_RAW" and PARTIAL_RE.search(f["path"]))
    out = {c: {"files": 0, "bytes_on_disk": 0, "recoverable_bytes": 0, "partial_files": 0} for c in CLASSES}
    for s in seen.values():
        o = out[s["cls"]]
        o["files"] += 1
        o["bytes_on_disk"] += s["blocks"]
        o["recoverable_bytes"] += s["blocks"] if s["count"] >= s["nlink"] else 0
        o["partial_files"] += int(s["partial"])
    return out


def _iq_locations(items: List[Dict[str, Any]], roots: sc.Roots) -> List[Dict[str, Any]]:
    """Where the raw IQ of the items are: one line per directory holding them, inside or outside the campaign."""
    groups: Dict[str, Dict[str, Any]] = {}
    for it in items:
        for f in it["inventory"]["files"]:
            if f["cls"] != "IQ_RAW":
                continue
            d = sc._rel(Path(f["path"]).parent, roots)
            g = groups.setdefault(d, {"path": d, "where": "outside the campaign (data/iq)" if it["kind"] == "IQ" else "inside the campaign",
                                      "files": 0, "partial_files": 0, "bytes_on_disk": 0})
            g["files"] += 1
            g["partial_files"] += int(bool(PARTIAL_RE.search(f["path"])))
            g["bytes_on_disk"] += f["blocks"]
    return sorted(groups.values(), key=lambda g: g["path"])


# ------------------------------------------------------------------ ownership
def _profile_loadable(roots: sc.Roots, profile_rel: Optional[str]) -> Tuple[bool, str]:
    if not profile_rel:
        return False, "no calibration profile recorded"
    info = sc._profile_info(roots, profile_rel)
    p = roots.base / profile_rel
    stem = p.with_suffix("") if p.suffix in (".json", ".npz") else p
    if info["state"] != "OK":
        return False, f"its calibration profile is {info['state']}"
    if not stem.with_suffix(".npz").is_file():
        return False, "its calibration profile has no .npz"
    return True, "profile loadable"


def _external_iq(roots: sc.Roots, index: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Every directory directly under data/iq/ with what links it to campaigns: the session_id of its identity
    (claimed by a campaign's capture_timing_<id>.csv or by a row of data/IQ/session.csv whose plan is in it) and the
    campaigns whose top-level files cite its path. `owner` is set only when ownership is proven."""
    root = _iq_root(roots)
    try:
        dirs = sorted(d for d in root.iterdir() if d.is_dir() or d.is_symlink()) if root.is_dir() and not root.is_symlink() else []
    except OSError:
        dirs = []
    if not dirs:
        return {}
    by_sid: Dict[str, set] = {}
    for s in index["sessions"]:
        for sid in s.get("capture_session_ids") or []:
            by_sid.setdefault(sid, set()).add(s["dir"])
    try:
        with _capture_index_path(roots).open(newline="") as fh:
            for row in csv.DictReader(fh):
                m = re.match(r"^(?:\./)?(data/mosaic/[^/]+)/", str(row.get("csv_plan_path") or ""))
                if m and row.get("session_id"):
                    by_sid.setdefault(row["session_id"], set()).add(m.group(1))
    except (OSError, csv.Error):
        pass
    patterns = {d.name: re.compile(r"(?:^|[\s\"'=:(,])(?:\./)?(?:" + re.escape(str(root)) + "|data/iq)/" + re.escape(d.name) + r"(?:/|[\"'\s,)]|$)")
                for d in dirs}
    cited: Dict[str, set] = {d.name: set() for d in dirs}
    for s in index["sessions"]:
        camp = roots.base / s["dir"]
        try:
            files = [f for f in camp.iterdir() if f.is_file() and not f.is_symlink() and f.stat().st_size <= CITE_MAX_BYTES]
        except OSError:
            continue
        for f in files:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for name, pat in patterns.items():
                if name in text and pat.search(text):
                    cited[name].add(s["dir"])
    out = {}
    for d in dirs:
        rel = sc._rel(d, roots)
        info: Dict[str, Any] = {"path": rel, "session_id": None, "claimants": set(), "cited_by": cited[d.name], "owner": None, "why": None}
        if d.is_symlink():
            info["why"] = "a symbolic link: never followed, never removed through"
        else:
            ident, state = sc._read_json(d / "session_identity.json")
            ident = ident if isinstance(ident, dict) else {}
            sid, sr = ident.get("session_id"), str(ident.get("session_root") or "")
            info["session_id"] = sid
            info["claimants"] = set(by_sid.get(sid, set())) if sid else set()
            srp = Path(sr) if Path(sr).is_absolute() else roots.base / sr
            linked = info["claimants"] | info["cited_by"]
            if state != "OK" or not sid:
                info["why"] = f"session_identity.json {state if state != 'OK' else 'without session_id'}: ownership not proven"
            elif not sr or os.path.normpath(srp) != os.path.normpath(d):
                info["why"] = f"its session_identity.json names another session_root ({sr or 'none'}): ownership not proven"
            elif not info["claimants"]:
                info["why"] = f"no campaign claims its session_id {sid}: ownership not proven"
            elif len(linked) > 1:
                info["why"] = "shared: claimed or cited by " + ", ".join(sorted(linked))
            else:
                info["owner"] = next(iter(info["claimants"]))
        out[rel] = info
    return out


def _run_campaigns(run_dir: Path, roots: sc.Roots, ext_owner: Optional[Dict[str, str]] = None) -> set:
    """Every campaign the points of a REDUCE run read (all points, not a sample). A capture in data/iq/<dir>/ counts
    as its proven owner campaign, or as that directory itself when ownership is not proven."""
    ext_owner = ext_owner or {}
    out = set()
    try:
        points = [p for p in (run_dir / "points").iterdir() if p.is_dir()]
    except OSError:
        return out
    for p in points:
        ms, state = sc._read_json(p / "master_spectrum.json", 5_000_000)
        for ref in ((ms or {}).get("capture_refs") or []) if state == "OK" else []:
            src = str(ref.get("source_path") or "")
            m = re.match(r"^(data/mosaic/[^/]+)/", src)
            x = re.match(r"^(data/iq/[^/]+)/", src)
            out.add(m.group(1) if m else ext_owner.get(x.group(1), x.group(1)) if x else "<unparsed capture_ref>")
    return out


def _citations(roots: sc.Roots, needles: List[str]) -> Dict[str, List[str]]:
    """Tracked text files of the repository that name a campaign or run (evidence cited in docs): a warning, not a block."""
    hits: Dict[str, List[str]] = {n: [] for n in needles}
    if not needles:
        return hits
    seen = set()
    for pattern in CITATION_GLOBS:
        for f in roots.base.glob(pattern):
            if not f.is_file() or f in seen or f.stat().st_size > 3_000_000:
                continue
            seen.add(f)
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for n in needles:
                if n in text:
                    hits[n].append(sc._rel(f, roots))
    return hits


def _session_iq(s: Dict[str, Any], ext: Dict[str, Dict[str, Any]], index: Dict[str, Any],
                roots: sc.Roots) -> Tuple[Optional[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Whole-session mode: (block reason, raw IQ directories outside the campaign to remove, IQ kept and why).
    A REDUCE run of another session that reads these IQ already blocks the session (they count as its captures)."""
    camp = s["dir"]
    others = {sid: x["dir"] for x in index["sessions"] if x["dir"] != camp for sid in x.get("capture_session_ids") or []}
    for name in s.get("iq_session_dirs") or []:          # inside the campaign: an IQ session claimed elsewhere is shared
        ident, _ = sc._read_json(roots.base / camp / "data" / "iq" / name / "session_identity.json")
        sid = ident.get("session_id") if isinstance(ident, dict) else None
        if sid and sid in others:
            return f"its raw IQ data/iq/{name} (session_id {sid}) is also claimed by {others[sid]}: shared", [], []
    preserved = {x["dir"] for x in index["sessions"] if x["campaign_id"] in PRESERVED_CAMPAIGNS}
    items, kept = [], []
    for rel, info in sorted(ext.items()):
        linked = info["claimants"] | info["cited_by"]
        if camp not in linked:
            continue
        if linked & preserved:
            kept.append({"kind": "IQ", "path": rel, "why": "PRESERVED: raw IQ linked to a preserved session"})
        elif info["owner"] != camp:
            kept.append({"kind": "IQ", "path": rel, "why": info["why"] or "ownership not proven"})
        else:
            items.append({"kind": "IQ", "category": "IQ", "path": rel,
                          "why": f"raw IQ of this session outside its campaign (session_id {info['session_id']}: identity, claim and citations agree)"})
    return None, items, kept


def _session_items(s: Dict[str, Any], mode: str, categories: List[str], index: Dict[str, Any], roots: sc.Roots,
                   all_run_campaigns: Dict[str, set], ext: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """What would be removed for one session, what is kept and why."""
    cid, camp = s["campaign_id"], roots.base / s["dir"]
    reduce_runs = [r for r in index["reduce_runs"] if r["run_id"] in (s.get("reduce") or {}).get("runs", [])]
    science_runs = [x for x in index["science_runs"] if x.get("reduce_dir") in {r["dir"] for r in reduce_runs}]
    items, kept = [], []
    plan_profile = (sc._read_json(camp / "observation_resolved.json")[0] or {}).get("quicklook", {}) or {}
    plan_profile = plan_profile.get("calibration_profile_path") if isinstance(plan_profile, dict) else None
    own_reduce = []
    for r in reduce_runs:
        used = all_run_campaigns.get(r["dir"], set())
        if used and used != {s["dir"]}:
            kept.append({"kind": "REDUCE", "path": r["dir"], "why": f"shared: its points also read {', '.join(sorted(used - {s['dir']}))}"})
        else:
            own_reduce.append(r)
    own_dirs = {r["dir"] for r in own_reduce}
    for x in science_runs:
        if x.get("link_state") != "LINKED" or x.get("reduce_dir") not in own_dirs:
            kept.append({"kind": "SCIENCE", "path": x["dir"], "why": f"link {x.get('link_state')}: association ambiguous"})
    own_science = [x for x in science_runs if x.get("link_state") == "LINKED" and x.get("reduce_dir") in own_dirs]
    # other campaigns' runs that read this campaign's captures: its captures are inputs of a kept result
    foreign = sorted(d for d, used in all_run_campaigns.items() if s["dir"] in used and d not in own_dirs)
    if mode == "sessions":
        if foreign:
            return {"blocked": f"its captures are inputs of REDUCE runs of other sessions: {', '.join(foreign)}", "items": [], "kept": kept}
        iq_block, iq_items, iq_kept = _session_iq(s, ext, index, roots)
        kept += iq_kept
        if iq_block:
            return {"blocked": iq_block, "items": [], "kept": kept}
        items.append({"kind": "CAMPAIGN", "category": "SESSION", "path": s["dir"], "why": "campaign directory (raw IQ, other capture files, QUICKLOOK, plan, logs)"})
        items += iq_items
        items += [{"kind": "REDUCE", "category": "REDUCE", "path": r["dir"], "why": "REDUCE run of this campaign only"} for r in own_reduce]
        items += [{"kind": "SCIENCE", "category": "SCIENCE", "path": x["dir"], "why": "SCIENCE run on one of those REDUCE runs"} for x in own_science]
    else:
        captures_present = bool(s.get("iq_session_dirs"))
        if "QUICKLOOK" in categories:
            ql = camp / "quicklook"
            ok, why = _profile_loadable(roots, plan_profile)
            if not ql.is_dir():
                pass
            elif not captures_present:
                kept.append({"kind": "QUICKLOOK", "path": sc._rel(ql, roots), "why": "no captures to regenerate it from"})
            elif not ok:
                kept.append({"kind": "QUICKLOOK", "path": sc._rel(ql, roots), "why": f"not regenerable: {why}"})
            else:
                items.append({"kind": "QUICKLOOK", "category": "QUICKLOOK", "path": sc._rel(ql, roots),
                              "why": f"regenerable: quicklook_live.py --once on the captures with {plan_profile}"})
        if "REDUCE" in categories:
            for r in own_reduce:
                ok, why = _profile_loadable(roots, r.get("calibration_profile_path"))
                if not captures_present:
                    kept.append({"kind": "REDUCE", "path": r["dir"], "why": "not regenerable: the captures are gone"})
                elif not ok:
                    kept.append({"kind": "REDUCE", "path": r["dir"], "why": f"not regenerable: {why}"})
                else:
                    items.append({"kind": "REDUCE", "category": "REDUCE", "path": r["dir"],
                                  "why": f"regenerable from the captures + {r.get('calibration_profile_path')} (config kept in the audit record; current code may differ from git {str(r.get('git_commit') or '?')[:10]})"})
        if "SCIENCE" in categories:
            items += [{"kind": "SCIENCE", "category": "SCIENCE", "path": x["dir"],
                       "why": "regenerable from its REDUCE run (config kept in the audit record)"} for x in own_science]
    # a SCIENCE that stays (kept, ambiguous, or not selected) while its REDUCE goes would lose its input: keep the REDUCE
    removing = {i["path"] for i in items}
    for x in science_runs:
        if x["dir"] not in removing and x.get("reduce_dir") in removing:
            items = [i for i in items if i["path"] != x["reduce_dir"]]
            removing.discard(x["reduce_dir"])
            kept.append({"kind": "REDUCE", "path": x["reduce_dir"], "why": f"input of SCIENCE {x['run_id']}, which is kept"})
    for p in index.get("pending_associations", []):
        if cid in p.get("candidates", []) or p["dir"] in {c["dir"] for c in (s.get("annotation") or {}).get("confirmed_links") or []}:
            kept.append({"kind": "MANUAL" if p["kind"] == "SCIENCE" else p["kind"], "path": p["dir"],
                         "why": "manual or unlinked analysis: may hold unique scripts, figures or conclusions; never deleted here"})
    return {"blocked": None, "items": items, "kept": kept}


# ------------------------------------------------------------------ preview
def _validate_request(ids: Any, mode: Any, categories: Any) -> Tuple[List[str], str, List[str]]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if not isinstance(ids, list) or not ids or len(ids) > 500:
        raise ValueError("ids must be a list of 1..500 session ids")
    clean = []
    for i in ids:
        if not isinstance(i, str) or not sc._ID_RE.match(i) or i in (".", ".."):
            raise ValueError(f"invalid session id: {str(i)[:80]!r}")
        if i not in clean:
            clean.append(i)
    cats = []
    if mode == "products":
        if not isinstance(categories, list) or not categories or any(c not in CATEGORIES for c in categories):
            raise ValueError(f"categories must be a non-empty subset of {', '.join(CATEGORIES)}")
        cats = [c for c in CATEGORIES if c in categories]
    return clean, mode, cats


def plan(ids: Any, mode: Any, categories: Any = None, roots: Optional[sc.Roots] = None,
         busy_probe: Optional[Callable[[], List[str]]] = None) -> Dict[str, Any]:
    """The preview. Reads only (it refreshes catalog_index.json). The token fingerprints mode, categories, ids, every
    file to remove (path, size, inode, mtime) and whether the pipeline is busy."""
    roots = roots or sc.default_roots()
    ids, mode, cats = _validate_request(ids, mode, categories)
    busy = (busy_probe or default_busy_probe)()
    index = sc.build_index(roots)
    ann = sc.load_annotations(roots)["sessions"]
    by_id = {s["campaign_id"]: sc._merged(s, ann) for s in index["sessions"]}
    ext = _external_iq(roots, index)
    ext_owner = {rel: i["owner"] for rel, i in ext.items() if i["owner"]}
    all_run_campaigns = {r["dir"]: _run_campaigns(roots.base / r["dir"], roots, ext_owner) for r in index["reduce_runs"]}
    preserved_dirs = set()
    for pid in PRESERVED_CAMPAIGNS:
        if pid in by_id:
            for r in index["reduce_runs"]:
                if r["run_id"] in by_id[pid]["reduce"]["runs"]:
                    preserved_dirs.add(r["dir"])
            preserved_dirs |= {x["dir"] for x in index["science_runs"] if x.get("reduce_dir") in preserved_dirs}
            preserved_dirs |= {rel for rel, i in ext.items() if by_id[pid]["dir"] in i["claimants"] | i["cited_by"]}
    sessions, all_items = [], []
    for cid in ids:
        s = by_id.get(cid)
        row: Dict[str, Any] = {"campaign_id": cid, "dir": (s or {}).get("dir"), "name": (s or {}).get("display_name"), "started_utc": (s or {}).get("started_utc"),
                               "started_chile": (s or {}).get("started_chile"), "capture_state": ((s or {}).get("capture") or {}).get("state"),
                               "points_captured": (s or {}).get("points_captured"), "blocked": None, "items": [], "kept": [], "warnings": []}
        if s is None:
            row["blocked"] = "unknown session (not on disk, or already deleted)"
        elif cid in PRESERVED_CAMPAIGNS:
            row["blocked"] = "PRESERVED: " + PRESERVED_CAMPAIGNS[cid]
        elif busy:
            row["blocked"] = "pipeline active: " + "; ".join(busy)
        else:
            r = _session_items(s, mode, cats, index, roots, all_run_campaigns, ext)
            row["kept"] = r["kept"]
            if r["blocked"]:
                row["blocked"] = r["blocked"]
            else:
                for it in r["items"]:
                    target = roots.base / it["path"]
                    if it["path"] in preserved_dirs:
                        row["kept"].append({"kind": it["kind"], "path": it["path"], "why": "PRESERVED (derived from a preserved session)"})
                        continue
                    bad = _validate_target(target, roots, it["kind"])
                    if bad:
                        row["kept"].append({"kind": it["kind"], "path": it["path"], "why": f"refused: {bad}"})
                        continue
                    it = dict(it, inventory=_inventory(target, it["kind"]))
                    if mode == "products" and any(f["cls"] != "PRODUCTS" for f in it["inventory"]["files"]):
                        row["kept"].append({"kind": it["kind"], "path": it["path"], "why": "refused: holds capture files, products mode never removes them"})
                        continue
                    row["items"].append(it)
                if mode == "sessions" and not any(i["kind"] == "CAMPAIGN" for i in row["items"]):
                    row["blocked"] = "its campaign directory cannot be removed safely: " + "; ".join(k["why"] for k in row["kept"] if k["kind"] == "CAMPAIGN")
                    row["items"] = []
        if row["blocked"]:
            row["items"] = []
        row["space"] = _space(row["items"])
        row["breakdown"] = _breakdown(row["items"])
        row["iq"] = _iq_locations(row["items"], roots)
        row["files"] = sum(len(i["inventory"]["files"]) for i in row["items"])
        sessions.append(row)
        all_items += row["items"]
    needles = [r["campaign_id"] for r in sessions if r["items"]] + [os.path.basename(i["path"]) for i in all_items if i["kind"] in ("REDUCE", "SCIENCE")]
    cites = _citations(roots, needles)
    for r in sessions:
        for n in [r["campaign_id"]] + [os.path.basename(i["path"]) for i in r["items"] if i["kind"] in ("REDUCE", "SCIENCE")]:
            if r["items"] and cites.get(n):
                r["warnings"].append(f"{n} is cited as evidence in {', '.join(cites[n][:4])}: those references will point to nothing")
    shared = {}
    for r in sessions:
        for k in r["kept"]:
            shared.setdefault(k["path"], k)
    profiles = {}
    for r in sessions:
        s = by_id.get(r["campaign_id"]) or {}
        prof = (s.get("calibration") or {}).get("profile_path")
        if prof and r["items"]:
            users = [x["campaign_id"] for x in index["sessions"] if (x.get("calibration") or {}).get("profile_path") == prof]
            profiles[prof] = {"path": prof, "why": f"calibration profile, never deleted here (used by {len(users)} session(s))"}
    fingerprint = {"mode": mode, "categories": cats, "ids": ids, "busy": bool(busy),
                   "items": sorted((i["path"], tuple(sorted((f["path"], f["size"], f["ino"], f["mtime_ns"]) for f in i["inventory"]["files"])),
                                    tuple(sorted(i["inventory"]["links"])), tuple(sorted(i["inventory"]["dirs"]))) for i in all_items)}
    token = hashlib.sha256(json.dumps(fingerprint, default=str).encode()).hexdigest()
    deletable = [r for r in sessions if r["items"]]
    space = _space(all_items)
    public = []
    for r in sessions:      # the preview the browser sees: no inode details, paths relative
        public.append({**{k: v for k, v in r.items() if k != "items"},
                       "items": [{"kind": i["kind"], "category": i["category"], "path": i["path"], "why": i["why"],
                                  "files": len(i["inventory"]["files"]), "symlinks_removed_as_links": len(i["inventory"]["links"]),
                                  "bytes_on_disk": sum(f["blocks"] for f in i["inventory"]["files"]),
                                  "iq_files": sum(f["cls"] == "IQ_RAW" for f in i["inventory"]["files"])} for i in r["items"]]})
    label = (f"DELETE {len(deletable)} SESSION{'S' if len(deletable) != 1 else ''} — IRREVERSIBLE" if mode == "sessions"
             else f"DELETE {'/'.join(cats)} OF {len(deletable)} SESSION{'S' if len(deletable) != 1 else ''} — IRREVERSIBLE")
    return {"token": token, "mode": mode, "categories": cats, "ids": ids, "sessions": public, "deletable": len(deletable),
            "blocked": [{"campaign_id": r["campaign_id"], "why": r["blocked"]} for r in sessions if r["blocked"]],
            "files": sum(r["files"] for r in sessions), "space": space, "breakdown": _breakdown(all_items),
            "kept_resources": list(shared.values()) + list(profiles.values()),
            "busy": busy, "confirm_label": label, "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "_internal": {"sessions": sessions}}


def public_plan(p: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in p.items() if k != "_internal"}


# ------------------------------------------------------------------ execute
def _audit_dir(roots: sc.Roots) -> Path:
    return roots.store / "deletions"


def _save_evidence(item: Dict[str, Any], dest: Path, roots: sc.Roots) -> List[str]:
    saved = []
    src = roots.base / item["path"]
    sub = dest / re.sub(r"[^A-Za-z0-9_.-]", "_", item["path"])
    names = EVIDENCE_FILES if item["kind"] in ("REDUCE", "SCIENCE") else ("observation_resolved.json", "grid_metadata.json", "stop_report.json") \
        if item["kind"] == "CAMPAIGN" else ("session_identity.json", "session.csv") if item["kind"] == "IQ" else ()
    for n in names:
        f = src / n
        if f.is_file() and not f.is_symlink() and f.stat().st_size <= EVIDENCE_MAX_BYTES:
            sub.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, sub / n)
            saved.append(sc._rel(sub / n, roots))
    return saved


def _remove_item(item: Dict[str, Any], remover: Callable[[str], None]) -> Dict[str, Any]:
    """Unlink exactly the inventoried files (same inode as previewed) and links, then the directories bottom-up when
    empty. Anything else found stays and is reported."""
    errors, removed, freed = [], 0, 0
    for f in item["inventory"]["files"]:
        try:
            st = os.lstat(f["path"])
            if os.path.islink(f["path"]) or st.st_ino != f["ino"] or st.st_dev != f["dev"]:
                errors.append({"path": f["path"], "error": "changed since the preview: kept"})
                continue
            remover(f["path"])
            removed += 1
            freed += f["blocks"]
        except OSError as exc:
            errors.append({"path": f["path"], "error": f"{type(exc).__name__}: {exc.strerror or exc}"})
    for link in item["inventory"]["links"]:
        try:
            if os.path.islink(link):
                os.unlink(link)                 # the link itself, never its target
        except OSError as exc:
            errors.append({"path": link, "error": f"{type(exc).__name__}: {exc.strerror or exc}"})
    for d in sorted(item["inventory"]["dirs"], key=lambda p: -p.count(os.sep)):
        try:
            if os.path.isdir(d) and not os.path.islink(d):
                os.rmdir(d)
        except OSError as exc:
            if exc.errno == errno.ENOTEMPTY and errors:
                continue                        # left over because a file inside could not be removed: already reported
            errors.append({"path": d, "error": f"{type(exc).__name__}: {exc.strerror or exc}"})
    return {"files_removed": removed, "bytes_removed_estimate": freed, "errors": errors, "gone": not os.path.lexists(item["inventory"]["dirs"][0])}


def _free_bytes(roots: sc.Roots) -> Optional[int]:
    try:
        st = os.statvfs(roots.base / "data")
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


def execute(ids: Any, mode: Any, categories: Any, token: Any, roots: Optional[sc.Roots] = None,
            busy_probe: Optional[Callable[[], List[str]]] = None, remover: Callable[[str], None] = os.unlink,
            by: str = "operator") -> Dict[str, Any]:
    """Re-plan under the lock; refuse on any change since the preview; delete; record the batch; rebuild the catalog."""
    roots = roots or sc.default_roots()
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("a preview token is required: preview first")
    with _flock(roots, timeout=LOCK_TIMEOUT_S):
        p = plan(ids, mode, categories, roots, busy_probe)
        if p["busy"]:
            raise StalePreview("the pipeline became active since the preview: " + "; ".join(p["busy"]) + " - nothing was deleted")
        if p["token"] != token:
            raise StalePreview("the files or the selection changed since the preview: nothing was deleted, preview again")
        batch_id = f"DEL-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
        audit = _audit_dir(roots) / batch_id
        audit.mkdir(parents=True, exist_ok=False)
        free_before = _free_bytes(roots)
        results = []
        for row in p["_internal"]["sessions"]:
            res: Dict[str, Any] = {"campaign_id": row["campaign_id"], "name": row["name"], "started_utc": row["started_utc"],
                                   "blocked": row["blocked"], "items": [], "files_removed": 0, "bytes_removed_estimate": 0, "errors": []}
            if row["blocked"] or not row["items"]:
                res["status"] = "BLOCKED" if row["blocked"] else "NOTHING TO DELETE"
                results.append(res)
                continue
            for it in row["items"]:
                saved = _save_evidence(it, audit, roots)
                out = _remove_item(it, remover)
                res["items"].append({"kind": it["kind"], "path": it["path"], "evidence_saved": saved, **out})
                res["files_removed"] += out["files_removed"]
                res["bytes_removed_estimate"] += out["bytes_removed_estimate"]
                res["errors"] += out["errors"]
            res["status"] = "DELETED" if not res["errors"] and all(i["gone"] for i in res["items"]) else "PARTIAL"
            results.append(res)
        free_after = _free_bytes(roots)
        _settle_annotations(results, mode, roots, batch_id)
        capture_index = _settle_capture_index(results, mode, roots, audit, {r["campaign_id"]: r["dir"] for r in p["_internal"]["sessions"] if r.get("dir")})
        index = sc.build_index(roots)
        summary = {"batch_id": batch_id, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": by, "mode": p["mode"],
                   "categories": p["categories"], "token": token,
                   "sessions": [{k: r[k] for k in ("campaign_id", "name", "started_utc", "status", "files_removed", "bytes_removed_estimate", "blocked")}
                                | {"errors": r["errors"][:50], "error_count": len(r["errors"]), "paths": [i["path"] for i in r["items"]]} for r in results],
                   "bytes_removed_estimate": sum(r["bytes_removed_estimate"] for r in results),
                   "free_bytes_before": free_before, "free_bytes_after": free_after,
                   "free_bytes_change_observed": (free_after - free_before) if free_before is not None and free_after is not None else None,
                   "capture_index": capture_index, "catalog_sessions_after": len(index["sessions"])}
        sc._write_atomic(audit / "batch.json", summary)
        with (roots.store / "deletions.jsonl").open("a") as fh:
            fh.write(json.dumps({k: summary[k] for k in ("batch_id", "utc", "by", "mode", "categories", "bytes_removed_estimate",
                                                          "free_bytes_change_observed")} |
                                {"sessions": [{k: s[k] for k in ("campaign_id", "status", "files_removed", "error_count")} for s in summary["sessions"]]},
                                default=str) + "\n")
        return {**summary, "audit_dir": sc._rel(audit, roots), "results": results}


def _settle_annotations(results: List[Dict[str, Any]], mode: str, roots: sc.Roots, batch_id: str) -> None:
    """Whole sessions gone: their active annotation leaves annotations.json (no ghost rows) and a minimal record stays
    in deleted_sessions.json. Products gone: preferred marks pointing to removed runs are cleared."""
    with sc._ANNOTATION_LOCK:
        data = sc.load_annotations(roots)
        changed = False
        gone_record_path = roots.store / "deleted_sessions.json"
        gone, _ = sc._read_json(gone_record_path)
        gone = gone if isinstance(gone, dict) else {}
        for r in results:
            cid = r["campaign_id"]
            removed_runs = {os.path.basename(i["path"]) for i in r["items"] if i["gone"] and i["kind"] in ("REDUCE", "SCIENCE")}
            entry = data["sessions"].get(cid)
            if mode == "sessions" and r["status"] == "DELETED":
                gone[cid] = {"deleted_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "batch_id": batch_id,
                             "name": r["name"], "started_utc": r["started_utc"], "files_removed": r["files_removed"],
                             "had_annotation": entry is not None}
                if entry is not None:
                    del data["sessions"][cid]
                    changed = True
            elif entry and removed_runs:
                if entry.get("preferred_reduce") in removed_runs:
                    entry["preferred_reduce"] = None
                    changed = True
                ps = entry.get("preferred_science") or {}
                for k in list(ps):
                    if k in removed_runs or ps[k] in removed_runs:
                        ps.pop(k)
                        changed = True
        if changed:
            sc._backup_annotations(roots)
            sc._write_atomic(sc._annotations_path(roots), data)
        if gone:
            sc._write_atomic(gone_record_path, gone)


def _settle_capture_index(results: List[Dict[str, Any]], mode: str, roots: sc.Roots, audit: Path,
                          dirs: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Whole sessions gone (DELETED only): their rows leave data/IQ/session.csv, matched by a csv_plan_path inside
    their campaign directory. The index is copied to the audit record first; every other line is written back as it
    was. A line that cannot be parsed alone leaves the index untouched (reported)."""
    gone = [dirs[r["campaign_id"]] for r in results if mode == "sessions" and r.get("status") == "DELETED" and dirs.get(r["campaign_id"])]
    path = _capture_index_path(roots)
    if not gone or not path.is_file() or path.is_symlink():
        return None
    try:
        lines = path.read_text().splitlines(keepends=True)
        header = next(csv.reader([lines[0]]))
        col = header.index("csv_plan_path")
        keep, removed = [lines[0]], []
        for line in lines[1:]:
            fields = next(csv.reader([line]), [])
            if len(fields) != len(header):
                return {"index": sc._rel(path, roots), "rows_removed": 0, "untouched": "a row spans several lines or has the wrong width: update it by hand"}
            plan_path = fields[col][2:] if fields[col].startswith("./") else fields[col]
            (removed if any(plan_path.startswith(d + "/") for d in gone) else keep).append(line)
    except (OSError, ValueError, IndexError, StopIteration, csv.Error) as exc:
        return {"index": sc._rel(path, roots), "rows_removed": 0, "untouched": f"unreadable ({type(exc).__name__})"}
    if not removed:
        return {"index": sc._rel(path, roots), "rows_removed": 0}
    backup = audit / "capture_index_session.csv.before"
    shutil.copyfile(path, backup)
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:6]}")
    with tmp.open("w") as fh:
        fh.write("".join(keep))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return {"index": sc._rel(path, roots), "rows_removed": len(removed), "backup": sc._rel(backup, roots)}


def history(roots: Optional[sc.Roots] = None, limit: int = 30) -> List[Dict[str, Any]]:
    roots = roots or sc.default_roots()
    try:
        lines = (roots.store / "deletions.jsonl").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
