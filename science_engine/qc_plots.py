"""QC/presentation PNGs (sections 65-72, 100) - NEVER a primary science
source (section 65): every array here is read back from an already-
persisted SCIENCE session's own HDF5 products. matplotlib is imported
lazily inside each function, kept OUT of science_engine.products'
dependency chain entirely - mirrors reduce_engine's own precedent of
keeping its core `run` pipeline plotting-free (data-level QC only) and
matches section 61's dependency-minimalism instruction: a `plot` command
that isn't invoked never imports matplotlib.

Invalid/no-coverage pixels render fully transparent, never black-zero
(section 67: 0 is a legitimate relative_intensity value).
"""
from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np


def _masked(value: np.ndarray, valid: np.ndarray) -> np.ma.MaskedArray:
    return np.ma.masked_where(~valid, value)


def _save_map(fig, ax, im, out_path: Path, title: str, colorbar_label: str) -> None:
    import matplotlib.pyplot as plt
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label(colorbar_label)
    ax.set_title(title)
    ax.set_xlabel("x (pixel)")
    ax.set_ylabel("y (pixel)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def _resolution_caption(grid_json: dict, beam_json: dict | None) -> str:
    """Section 72: every map declares pixel scale + beam FWHM so a
    reader never mistakes pixel count for spatial resolution."""
    fwhm = beam_json.get("fwhm_deg") if beam_json else "?"
    return f"pixel scale={grid_json['pixel_scale_deg']:.3f} deg, beam FWHM={fwhm} deg (effective resolution)"


def plot_science_map(science_session_dir: str | Path, map_name: str, out_path: str | Path) -> Path:
    """`map_name` matches a file under <session>/maps/, e.g.
    'integrated_relative_intensity', 'moment1_like_velocity_centroid'."""
    import matplotlib.pyplot as plt
    session_dir = Path(science_session_dir)
    manifest = json.loads((session_dir / "manifest.json").read_text())
    with h5py.File(session_dir / "maps" / f"{map_name}.h5", "r") as handle:
        value = handle["value"][:]
        valid = handle["valid"][:]
        units = handle.attrs["units"]
        grid_json = json.loads(handle.attrs["grid_json"])

    masked = _masked(value, valid)
    cmap = plt.get_cmap("viridis" if "intensity" in map_name else "coolwarm").copy()
    cmap.set_bad(alpha=0.0)  # section 67: invalid = transparent, never black/zero
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(masked, origin="lower", cmap=cmap)
    caption = _resolution_caption(grid_json, manifest.get("beam"))
    _save_map(fig, ax, im, Path(out_path), f"{map_name} ({units})\n{caption}", units)
    return Path(out_path)


def plot_uncertainty_map(science_session_dir: str | Path, map_name: str, out_path: str | Path) -> Path:
    import matplotlib.pyplot as plt
    session_dir = Path(science_session_dir)
    with h5py.File(session_dir / "maps" / f"{map_name}.h5", "r") as handle:
        if "uncertainty" not in handle:
            raise ValueError(f"{map_name} has no uncertainty array")
        uncertainty = handle["uncertainty"][:]
        valid = handle["valid"][:]
        grid_json = json.loads(handle.attrs["grid_json"])

    masked = _masked(uncertainty, valid)
    cmap = plt.get_cmap("magma").copy()
    cmap.set_bad(alpha=0.0)
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(masked, origin="lower", cmap=cmap)
    _save_map(fig, ax, im, Path(out_path), f"{map_name} uncertainty\n{_resolution_caption(grid_json, None)}",
             "uncertainty")
    return Path(out_path)


def plot_coverage_map(science_session_dir: str | Path, out_path: str | Path) -> Path:
    """Section 69: n_contributing, from the integrated map (a well-defined
    2D product) rather than the full cube."""
    import matplotlib.pyplot as plt
    session_dir = Path(science_session_dir)
    with h5py.File(session_dir / "maps" / "integrated_relative_intensity.h5", "r") as handle:
        n_contributing = handle["n_contributing"][:]
        valid = handle["valid"][:]
        grid_json = json.loads(handle.attrs["grid_json"])

    masked = _masked(n_contributing.astype(float), n_contributing > 0)
    cmap = plt.get_cmap("cividis").copy()
    cmap.set_bad(alpha=0.0)
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(masked, origin="lower", cmap=cmap)
    _save_map(fig, ax, im, Path(out_path), f"coverage (n contributing points)\n{_resolution_caption(grid_json, None)}",
             "n_contributing")
    return Path(out_path)


def plot_channel_map(science_session_dir: str | Path, channel_index: int, out_path: str | Path) -> Path:
    """A raw cube slice at one channel index - a QC convenience, not a
    replacement for science_engine.integration.channel_map's own
    coverage-aware windowed average."""
    import matplotlib.pyplot as plt
    session_dir = Path(science_session_dir)
    with h5py.File(session_dir / "cube" / "science_cube.h5", "r") as handle:
        value = handle["relative_intensity"][channel_index]
        valid = handle["valid"][channel_index]
        velocity = handle["velocity_lsrk_m_s"][channel_index]
        grid_json = json.loads(handle.attrs["grid_json"])

    masked = _masked(value, valid)
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad(alpha=0.0)
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(masked, origin="lower", cmap=cmap)
    _save_map(fig, ax, im, Path(out_path),
             f"channel {channel_index} (v={velocity:.0f} m/s)\n{_resolution_caption(grid_json, None)}",
             "relative_intensity")
    return Path(out_path)


def plot_representative_spectrum(reduce_session_dir: str | Path, point_index: int, out_path: str | Path) -> Path:
    """Reads one REDUCE point's own master_spectrum.h5 directly - this is
    SCIENCE consuming REDUCE's frozen LEVEL 1 contract for a QC plot, not
    reopening RAW (section 51's point inspector, minimal core version)."""
    import matplotlib.pyplot as plt
    from reduce_engine.models import MaskFlag
    point_dir = Path(reduce_session_dir) / "points" / str(point_index)
    with h5py.File(point_dir / "master_spectrum.h5", "r") as handle:
        velocity = handle["velocity_lsrk_m_s"][:]
        value = handle["relative_intensity"][:]
        mask = handle["mask"][:]
        quality_state = handle.attrs["quality_state"]

    good = mask == MaskFlag.GOOD.value
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(velocity, np.where(good, value, np.nan), color="tab:blue", label="GOOD")
    ax.plot(velocity, np.where(~good, value, np.nan), color="tab:red", lw=0.8, alpha=0.6, label="masked")
    ax.set_xlabel("velocity_lsrk (m/s)")
    ax.set_ylabel("relative_intensity")
    ax.set_title(f"point {point_index} (REDUCE quality={quality_state})")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return Path(out_path)
