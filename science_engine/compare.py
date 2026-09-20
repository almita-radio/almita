"""COMPARE two SCIENCE sessions: identity, configuration, and per-artifact numeric equivalence.

No astrophysical interpretation - purely "are these the same numbers, and if not, is the difference explained by
a different input/config?". Artifact classes (canonical scientific ARRAYS only; attributes such as the session id
never count):

  BYTE IDENTICAL          arrays_sha256 equal
  NUMERICALLY IDENTICAL   same dtype-independent values, NaN positions and shapes (hash differs only by dtype/layout)
  NUMERICALLY EQUIVALENT  same shape and NaN positions, max |a-b| <= rtol * max|a|
  EXPECTED DIFFERENCE     differs AND the configuration or input identity differs (so a difference is the point)
  UNEXPECTED DIFFERENCE   differs although input identity and config_hash are equal  -> a determinism bug
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from science_engine.storage import arrays_sha256

RTOL_EQUIVALENT = 1e-9


def _load(session_dir: Path) -> tuple[dict, dict]:
    return (json.loads((session_dir / "manifest.json").read_text()),
            json.loads((session_dir / "config.json").read_text()))


def _dataset_diff(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    if a.shape != b.shape:
        return {"shape_a": list(a.shape), "shape_b": list(b.shape), "comparable": False}
    if a.dtype == bool or b.dtype == bool:
        n = int(np.sum(a != b))
        return {"comparable": True, "n_differing": n, "max_abs_diff": float(n > 0), "nan_mismatch": 0}
    a, b = a.astype(np.float64), b.astype(np.float64)
    nan_mismatch = int(np.sum(np.isnan(a) != np.isnan(b)))
    both = np.isfinite(a) & np.isfinite(b)
    diff = np.abs(a - b)[both]
    scale = float(np.max(np.abs(a[both]))) if both.any() else 0.0
    max_abs = float(diff.max()) if diff.size else 0.0
    return {"comparable": True, "nan_mismatch": nan_mismatch, "max_abs_diff": max_abs,
            "max_rel_diff": (max_abs / scale) if scale > 0 else (0.0 if max_abs == 0 else float("inf")),
            "n_differing": int(np.sum(diff > 0))}


def _compare_h5(path_a: Path, path_b: Path) -> dict[str, Any]:
    if arrays_sha256(path_a) == arrays_sha256(path_b):
        return {"level": "BYTE IDENTICAL", "datasets": {}}
    out: dict[str, Any] = {}
    with h5py.File(path_a, "r") as ha, h5py.File(path_b, "r") as hb:
        for name in sorted(set(ha.keys()) | set(hb.keys())):
            if name not in ha or name not in hb:
                out[name] = {"comparable": False, "only_in": "a" if name in ha else "b"}
                continue
            out[name] = _dataset_diff(ha[name][()], hb[name][()])
    if any(not d["comparable"] for d in out.values()):
        return {"level": "DIFFERENT", "datasets": out}
    if all(d["nan_mismatch"] == 0 and d["max_abs_diff"] == 0 for d in out.values()):
        return {"level": "NUMERICALLY IDENTICAL", "datasets": out}
    if all(d["nan_mismatch"] == 0 and d.get("max_rel_diff", 0.0) <= RTOL_EQUIVALENT for d in out.values()):
        return {"level": "NUMERICALLY EQUIVALENT", "datasets": out}
    return {"level": "DIFFERENT", "datasets": out}


def compare_sessions(session_a: str | Path, session_b: str | Path) -> dict[str, Any]:
    a_dir, b_dir = Path(session_a), Path(session_b)
    ma, ca = _load(a_dir)
    mb, cb = _load(b_dir)

    identity = {
        "input_reduce_session_id": [ma.get("input_reduce_session_id"), mb.get("input_reduce_session_id")],
        "input_reduce_manifest_sha256": [ma.get("input_reduce_manifest_sha256"), mb.get("input_reduce_manifest_sha256")],
        "config_hash": [ma.get("config_hash"), mb.get("config_hash")],
        "campaign_id": [ma.get("campaign_id"), mb.get("campaign_id")],
    }
    same_input = (identity["input_reduce_manifest_sha256"][0] == identity["input_reduce_manifest_sha256"][1]
                  and identity["input_reduce_session_id"][0] == identity["input_reduce_session_id"][1])
    same_config = identity["config_hash"][0] == identity["config_hash"][1]

    config_diff = {k: [ca.get(k), cb.get(k)] for k in sorted(set(ca) | set(cb)) if ca.get(k) != cb.get(k)}
    beam_diff = {k: [ma["beam"].get(k), mb["beam"].get(k)] for k in ma["beam"] if ma["beam"].get(k) != mb["beam"].get(k)}
    grid_diff = {k: [ma["grid"].get(k), mb["grid"].get(k)] for k in ma["grid"] if ma["grid"].get(k) != mb["grid"].get(k)}
    window_diff = ([ma.get("integrated_window_m_s"), mb.get("integrated_window_m_s")]
                   if ma.get("integrated_window_m_s") != mb.get("integrated_window_m_s") else None)
    shape = {"cube_shape": [_product(ma, "science_cube"), _product(mb, "science_cube")]}
    shape["cube_shape"] = [p["shape"] if p else None for p in shape["cube_shape"]]

    products: dict[str, Any] = {}
    explained = (not same_input) or (not same_config)
    for pid in sorted({p["id"] for p in ma.get("products", [])} | {p["id"] for p in mb.get("products", [])}):
        pa, pb = _product(ma, pid), _product(mb, pid)
        if pa is None or pb is None:
            products[pid] = {"level": "EXPECTED DIFFERENCE" if explained else "UNEXPECTED DIFFERENCE",
                             "detail": "product present in only one session"}
            continue
        result = _compare_h5(a_dir / pa["path"], b_dir / pb["path"])
        result["sha256_equal"] = pa.get("sha256") == pb.get("sha256")
        result["arrays_sha256_equal"] = pa.get("arrays_sha256") == pb.get("arrays_sha256")
        if result["level"] == "DIFFERENT":
            result["level"] = "EXPECTED DIFFERENCE" if explained else "UNEXPECTED DIFFERENCE"
        products[pid] = result

    levels = {p["level"] for p in products.values()}
    equivalent = levels <= {"BYTE IDENTICAL", "NUMERICALLY IDENTICAL", "NUMERICALLY EQUIVALENT"} and not (
        config_diff or beam_diff or grid_diff or window_diff) and same_input
    return {"verdict": "EQUIVALENT" if equivalent else "DIFFERENT", "same_input": same_input, "same_config": same_config,
            "identity": identity, "config_diff": config_diff, "beam_diff": beam_diff, "grid_diff": grid_diff,
            "velocity_window_diff": window_diff, **shape, "products": products,
            "quality_a": ma.get("quality", {}).get("state"), "quality_b": mb.get("quality", {}).get("state")}


def _product(manifest: dict, product_id: str):
    return next((p for p in manifest.get("products", []) if p["id"] == product_id), None)
