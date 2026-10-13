"""Server-side catalog and validation of calibration profiles for OBSERVE's "Calibration profile path".

Everything here is about the SERVER's disk: the path OBSERVE stores in quicklook.calibration_profile_path is
opened by the backend (observation_preflight, quicklook_live) relative to the repo root, so the selector only
offers and accepts files that exist there. A path typed in the browser that does not exist on the server is
rejected with a clear message; nothing from the browser's own disk is ever accepted as a path.

Scope is deliberately narrow: only regular files under the calibration root (data/calibration), never through a
symlink or `..` that leaves it, only `<stem>.json` + `<stem>.npz` pairs that calibration_foundation's own loader
accepts. The listing returns profile metadata only (no file contents, no other directories).

Validity = calibration_foundation.load_calibration_profile() succeeds (the loader Quicklook uses).
Compatibility = observation_preflight.planned_profile_compatibility() against the OBSERVE form's main section
(the same decision the pre-RUN preflight and Quicklook make).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_JSON_BYTES = 2_000_000
MAX_PROFILES = 500
SUMMARY_FIELDS = ("center_frequency_hz", "sample_rate_hz", "gain_db", "fft_size", "calibration_level",
                  "created_utc", "reference_count", "instrument_chain", "reference_topology")


class ProfilePathError(ValueError):
    """A profile path the server cannot accept (outside the calibration root, missing, not a profile)."""


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _summary(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {k: metadata.get(k) for k in SUMMARY_FIELDS}


def _compatibility(profile: Dict[str, Any], main: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not main or not any(main.get(k) is not None for k in ("center_frequency_hz", "sample_rate", "gain_db")):
        return {"status": "UNKNOWN", "reason": "no OBSERVE frequency / sample rate / gain given to compare against"}
    import observation_preflight
    return observation_preflight.planned_profile_compatibility(profile, main)


def _load(json_path: Path) -> Dict[str, Any]:
    import calibration_foundation
    if json_path.stat().st_size > MAX_JSON_BYTES:
        raise ProfilePathError("file too large to be a calibration profile")
    return calibration_foundation.load_calibration_profile(json_path)


def _looks_like_profile(json_path: Path) -> bool:
    """Cheap pre-filter before the real loader: a calibration profile JSON with its .npz sibling."""
    if json_path.is_symlink() or not json_path.with_suffix(".npz").is_file() or json_path.with_suffix(".npz").is_symlink():
        return False
    try:
        if json_path.stat().st_size > MAX_JSON_BYTES:
            return False
        head = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(head, dict) and "absolute_calibration" in head and "center_frequency_hz" in head


def list_profiles(root: Path, repo_root: Path, main: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Every loadable profile under `root`, newest first, with its compatibility against `main`."""
    entries: List[Dict[str, Any]] = []
    root = Path(root)
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) / d).is_symlink())
            for name in sorted(filenames):
                if not name.endswith(".json"):
                    continue
                path = Path(dirpath) / name
                if not _within(path, root) or not _looks_like_profile(path):
                    continue
                rel = str(path.resolve().relative_to(Path(repo_root).resolve()))
                try:
                    profile = _load(path)
                except Exception as exc:  # noqa: BLE001 - reported per file, never fatal for the listing
                    entries.append({"path": rel, "valid": False, "error": f"{type(exc).__name__}: {exc}",
                                    "summary": None, "compatibility": None})
                    continue
                entries.append({"path": rel, "valid": True, "error": None, "summary": _summary(profile["metadata"]),
                                "compatibility": _compatibility(profile, main)})
                if len(entries) >= MAX_PROFILES:
                    break
    entries.sort(key=lambda e: ((e.get("summary") or {}).get("created_utc") or "", e["path"]), reverse=True)
    return {"root": str(Path(root).resolve().relative_to(Path(repo_root).resolve())), "disk": "server",
            "profiles": entries, "compared_against": main}


def validate_profile_path(raw: Any, root: Path, repo_root: Path, main: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalize and validate a path for quicklook.calibration_profile_path. Raises ProfilePathError with a
    message meant for the operator; returns the normalized server path (relative to the repo root)."""
    if not isinstance(raw, str) or not raw.strip():
        raise ProfilePathError("empty path")
    text = raw.strip()
    if "\x00" in text:
        raise ProfilePathError("invalid path")
    if re.match(r"^[A-Za-z]:[\\/]", text) or "\\" in text or "fakepath" in text.lower() or text.lower().startswith("file:"):
        raise ProfilePathError("this looks like a path on the browser's computer - the ALMITA server cannot read it; "
                               "pick a profile that exists on the server (BROWSE ALMITA SERVER)")
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = Path(repo_root) / candidate
    if candidate.suffix in (".npz", ""):
        candidate = candidate.with_suffix(".json")
    if candidate.suffix != ".json":
        raise ProfilePathError("a calibration profile path ends in .json (its .npz sibling is found automatically)")
    if not _within(candidate, root):
        raise ProfilePathError(f"only profiles under {Path(root).resolve().relative_to(Path(repo_root).resolve())}/ "
                               "on the ALMITA server can be used")
    if candidate.is_symlink() or candidate.with_suffix(".npz").is_symlink():
        raise ProfilePathError("symbolic links are not accepted")
    if not candidate.is_file():
        raise ProfilePathError("file does not exist on the ALMITA server (a path from this browser's own disk "
                               "cannot be used: pick a server profile)")
    if not candidate.with_suffix(".npz").is_file():
        raise ProfilePathError("the profile's .npz arrays are missing next to the .json")
    try:
        profile = _load(candidate)
    except ProfilePathError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProfilePathError(f"not a valid calibration profile: {type(exc).__name__}: {exc}") from exc
    rel = str(candidate.resolve().relative_to(Path(repo_root).resolve()))
    return {"path": rel, "disk": "server", "valid": True, "summary": _summary(profile["metadata"]),
            "compatibility": _compatibility(profile, main)}


MAX_BROWSE_ENTRIES = 300


def _profile_entry(path: Path, repo_root: Path, main: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    rel = path.resolve().relative_to(Path(repo_root).resolve())
    entry: Dict[str, Any] = {"type": "profile", "name": path.name, "dir": str(rel.parent), "path": str(rel)}
    try:
        profile = _load(path)
    except Exception as exc:  # noqa: BLE001 - a broken file is listed with its reason, never fatal
        entry.update(valid=False, selectable=False, error=f"{type(exc).__name__}: {exc}", summary=None, compatibility=None)
        return entry
    compat = _compatibility(profile, main)
    entry.update(valid=True, error=None, summary=_summary(profile["metadata"]), compatibility=compat,
                 selectable=compat["status"] != "INCOMPATIBLE")
    return entry


def browse_directory(rel_dir: Any, root: Path, repo_root: Path, main: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One directory of the calibration root on the SERVER, for a file-explorer view: its sub-directories and
    every .json in it - profiles with their frequency / sample rate / gain and compatibility against `main`
    (`selectable` is False for an invalid or INCOMPATIBLE one, with the reason), other .json files with why they
    are not a profile. Read-only: nothing is written, copied or overwritten. Raises ProfilePathError for a
    directory outside the root, a symlink or a missing one."""
    root_r, repo_r = Path(root).resolve(), Path(repo_root).resolve()
    text = (rel_dir or "").strip() if isinstance(rel_dir, str) else ""
    if "\x00" in text or "\\" in text:
        raise ProfilePathError("invalid directory")
    target = root_r if not text else (repo_r / text)
    if target.is_symlink() or not _within(target, root_r):
        raise ProfilePathError(f"only directories under {root_r.relative_to(repo_r)}/ on the ALMITA server can be browsed")
    target = target.resolve()
    if not target.is_dir():
        raise ProfilePathError("directory does not exist on the ALMITA server")
    dirs: List[Dict[str, Any]] = []
    files: List[Dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda p: p.name):
        if len(dirs) + len(files) >= MAX_BROWSE_ENTRIES:
            break
        if child.is_symlink() or child.name.startswith("."):
            continue
        rel = str(child.relative_to(repo_r))
        if child.is_dir():
            dirs.append({"type": "dir", "name": child.name, "path": rel})
        elif child.suffix == ".json":
            if _looks_like_profile(child):
                files.append(_profile_entry(child, repo_r, main))
            else:
                reason = ("no .npz arrays next to it" if not child.with_suffix(".npz").is_file()
                          else "not a calibration profile (missing center_frequency_hz / absolute_calibration)")
                files.append({"type": "other", "name": child.name, "dir": str(child.parent.relative_to(repo_r)),
                              "path": rel, "valid": False, "selectable": False, "error": reason,
                              "summary": None, "compatibility": None})
    crumbs, here = [], target
    while True:
        crumbs.append({"name": here.name, "path": str(here.relative_to(repo_r))})
        if here == root_r:
            break
        here = here.parent
    return {"disk": "server", "root": str(root_r.relative_to(repo_r)), "dir": str(target.relative_to(repo_r)),
            "parent": None if target == root_r else str(target.parent.relative_to(repo_r)),
            "breadcrumbs": list(reversed(crumbs)), "dirs": dirs, "files": files, "compared_against": main}
