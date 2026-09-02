#!/usr/bin/env python3
"""ANTENNA A session-wide waterfall: one row per grid point.

quicklook_waterfall.py's own product shows the fine time structure
*inside* a single point's capture and is overwritten every point - it
never accumulates across a session. This module keeps one decimated
row per point instead, so an operator can see at which point in the
session a signal appeared. Row count is naturally bounded by the
plan's point count (still capped defensively); each row reuses the
spectrum quicklook_live.py already computed for that point (no second
FFT) and is frequency-decimated with quicklook_waterfall.py's own
dashboard decimation, keeping the file small regardless of session
length.

The PNG is rendered at a fixed figsize with a plain (non-"tight")
bbox, so - unlike the bbox_inches="tight" incident in
grid_generator.py - its canvas size never depends on the data: adding
more rows only changes what is mapped into that fixed-size image.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from quicklook_waterfall import decimate_dashboard
from runtime_state import atomic_write_json, read_json_safe, utcnow

SCHEMA_VERSION = 1
DEFAULT_FREQUENCY_BINS = 256
DEFAULT_MAX_ROWS = 5000


def _finite_list(values: np.ndarray) -> list[float | None]:
    return [float(value) if np.isfinite(value) else None for value in values]


def _state_path(output_dir: Path) -> Path:
    return output_dir / "session_waterfall_state.json"


def _load_state(output_dir: Path, session_id: str) -> dict[str, Any]:
    state = read_json_safe(_state_path(output_dir))
    if state and state.get("session_id") == session_id:
        return state
    return {"schema_version": SCHEMA_VERSION, "session_id": session_id, "rows": []}


def _render_png(output_path: Path, frequency_mhz: np.ndarray, rows: list[dict]) -> None:
    matrix = np.array(
        [[np.nan if value is None else value for value in row["relative_db"]] for row in rows],
        dtype=float,
    )
    masked = np.ma.masked_invalid(matrix)
    figure, axis = plt.subplots(figsize=(12, 6))
    try:
        image = axis.imshow(
            masked, origin="lower", aspect="auto", interpolation="nearest",
            extent=[frequency_mhz[0], frequency_mhz[-1], 0, len(rows)],
            cmap="viridis",
        )
        axis.set(
            xlabel="Frequency (MHz)", ylabel="Point index (session order)",
            title="ALMITA — Quicklook Session Waterfall",
        )
        figure.colorbar(image, ax=axis, label="Relative PSD (dB)")
        figure.tight_layout()
        figure.savefig(output_path, dpi=150)
    finally:
        plt.close(figure)


def update_session_waterfall(
    output_dir: str | Path, *, session_id: str, point_id: str,
    frequency_hz: np.ndarray, relative_db: np.ndarray, valid_mask: np.ndarray,
    frequency_bins: int = DEFAULT_FREQUENCY_BINS, max_rows: int = DEFAULT_MAX_ROWS,
) -> None:
    """Add/replace `point_id`'s row and re-render the session waterfall PNG."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    frequency_hz = np.asarray(frequency_hz)
    dashboard_frequency, dashboard_values, _ = decimate_dashboard(
        frequency_hz, np.asarray(relative_db)[None, :], np.asarray(valid_mask),
        frequency_bins=frequency_bins,
    )
    new_row = {"point_id": point_id, "utc": utcnow(), "relative_db": _finite_list(dashboard_values[0])}

    state = _load_state(output_dir, session_id)
    rows = [row for row in state["rows"] if row.get("point_id") != point_id]
    rows.append(new_row)
    state["rows"] = rows[-max_rows:]
    state["frequency_mhz"] = (dashboard_frequency / 1e6).tolist()
    state["updated_utc"] = utcnow()
    atomic_write_json(_state_path(output_dir), state)

    _render_png(output_dir / "session_waterfall.png", np.asarray(state["frequency_mhz"]), state["rows"])
    atomic_write_json(output_dir / "session_waterfall.json", {
        "schema_version": SCHEMA_VERSION, "session_id": session_id,
        "point_count": len(state["rows"]), "frequency_mhz": state["frequency_mhz"],
        "point_ids": [row["point_id"] for row in state["rows"]],
        "updated_utc": state["updated_utc"],
    })
