"""SESSIONS page API (BL-079..BL-090): thin routing over session_catalog.

Every GET is read-only over the data and never starts REDUCE, SCIENCE, QUICKLOOK or a physical job. The three POSTs
write only under data/sessions/: reindex (catalog_index.json), annotate (annotations.json + its log) and export
(exports/<id>-<utc>/ and its .zip). Nothing here imports the hardware layers."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import session_catalog as catalog

GET_ROUTES = ("/api/sessions/list", "/api/sessions/detail", "/api/sessions/points", "/api/sessions/point",
              "/api/sessions/capture_attributes", "/api/sessions/temperatures", "/api/sessions/files", "/api/sessions/pending",
              "/api/sessions/exports", "/api/sessions/compare", "/api/sessions/compare_runs", "/api/sessions/deletions")
POST_ROUTES = ("/api/sessions/reindex", "/api/sessions/annotate", "/api/sessions/export", "/api/sessions/delete/preview",
               "/api/sessions/delete/execute")
EXPORT_FILE_ROUTE = "/api/sessions/export_file"
ROOTS: Optional[catalog.Roots] = None        # None = the real data/ tree; tests point it at a temporary one
BUSY_PROBE = None                            # None = session_deletion.default_busy_probe; tests replace it


def _mount_readback(h5: Optional[Path]) -> Dict[str, Any]:
    """Where the mount really was during the capture (capture.py's mount_capture_* attributes), not the plan's target.
    Attributes only: the IQ samples are never read. None when there is no capture file or the attribute is absent."""
    out: Dict[str, Any] = {"mount_ra_hours": None, "mount_dec_deg": None}
    if h5 is None:
        return out
    try:
        import h5py
        with h5py.File(h5, "r") as f:
            ra, dec = f.attrs.get("mount_capture_ra_hours"), f.attrs.get("mount_capture_dec_deg")
        if ra is not None and dec is not None:
            out.update(mount_ra_hours=float(ra), mount_dec_deg=float(dec))
    except (ImportError, OSError, ValueError, TypeError):
        pass
    return out


def _one(q: Dict[str, Any], key: str, default: str = "") -> str:
    v = q.get(key)
    if isinstance(v, list):
        v = v[0] if v else default
    return default if v is None else str(v)


def handle_get(path: str, query: Dict[str, Any], roots: Optional[catalog.Roots] = None) -> Dict[str, Any]:
    """The payload of one GET route. ValueError = bad request (400), FileNotFoundError = unknown session/point (404)."""
    roots = roots or ROOTS
    sid = _one(query, "id")
    if path == "/api/sessions/list":
        return catalog.list_sessions({k: _one(query, k) for k in query}, roots)
    if path == "/api/sessions/detail":
        return catalog.session_detail(sid, roots)
    if path == "/api/sessions/points":
        # the sky map needs where the mount really was: read here, not in the catalog (which never loads h5py)
        d = catalog.session_points(sid, roots)
        camp = catalog._campaign_dir(sid, roots or catalog.default_roots())
        for p in d["points"]:
            p.update(_mount_readback(catalog._h5_for_point(camp, p)))
        return d
    if path == "/api/sessions/point":
        return catalog.point_detail(sid, _one(query, "point"), roots)
    if path == "/api/sessions/capture_attributes":
        # Explicit request only (a button): the HDF5 attributes and array shape of ONE capture, through REDUCE's own
        # read-only metadata preview (reduce_single_capture.inspect_capture_metadata) - never the IQ samples.
        p = catalog.point_detail(sid, _one(query, "point"), roots)
        if p["capture"]["state"] != "OK":
            raise FileNotFoundError("this point has no capture file")
        import almita_web_ops
        return {"path": p["capture"]["path"], "attributes": almita_web_ops.reduce_inspect_capture(p["capture"]["path"])}
    if path == "/api/sessions/temperatures":
        return catalog.session_temperatures(sid, roots)
    if path == "/api/sessions/files":
        return catalog.session_files(sid, _one(query, "dir"), roots)
    if path == "/api/sessions/pending":
        return {"pending": catalog.load_index(roots).get("pending_associations", [])}
    if path == "/api/sessions/exports":
        return {"exports": catalog.list_exports(sid, roots)}
    if path == "/api/sessions/compare":
        return catalog.compare_sessions(_one(query, "a"), _one(query, "b"), roots)
    if path == "/api/sessions/compare_runs":
        return catalog.compare_runs(sid, _one(query, "a"), _one(query, "b"), roots)
    if path == "/api/sessions/deletions":
        import session_deletion
        return {"batches": session_deletion.history(roots)}
    raise FileNotFoundError("not found")


def handle_post(path: str, body: Dict[str, Any], roots: Optional[catalog.Roots] = None) -> Dict[str, Any]:
    roots = roots or ROOTS
    if path == "/api/sessions/reindex":
        idx = catalog.build_index(roots)
        return {"generated_utc": idx["generated_utc"], "build_seconds": idx["build_seconds"], "sessions": len(idx["sessions"]),
                "pending_associations": len(idx["pending_associations"])}
    if path == "/api/sessions/annotate":
        return {"annotation": catalog.annotate(str(body.get("id") or ""), body.get("patch") or {}, roots)}
    if path in ("/api/sessions/delete/preview", "/api/sessions/delete/execute"):
        # BL-093. Only ids, a mode and categories come from the browser: every path is computed by the server.
        import session_deletion
        args = (body.get("ids"), body.get("mode"), body.get("categories"))
        if path.endswith("preview"):
            return session_deletion.public_plan(session_deletion.plan(*args, roots=roots, busy_probe=BUSY_PROBE))
        if body.get("confirm") != "DELETE":
            raise ValueError('confirm: "DELETE" is required - deletion never proceeds implicitly')
        return session_deletion.execute(*args, body.get("token"), roots=roots, busy_probe=BUSY_PROBE)
    if path == "/api/sessions/export":
        if body.get("include_iq"):
            raise ValueError("exporting IQ is not available: the report is meant to travel without the captures")
        return catalog.export_report(str(body.get("id") or ""), roots)
    raise FileNotFoundError("not found")
