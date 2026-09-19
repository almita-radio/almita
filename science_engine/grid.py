"""ScienceGrid construction (sections 15-16): a local tangent-plane grid
sized to the actual input points' extent plus a beam-derived margin -
never a hardcoded shape (section 15).
"""
from __future__ import annotations

import numpy as np

from science_engine.config import ScienceConfig
from science_engine.models import BeamModel, ScienceGrid, ScienceInput
from science_engine.spatial import angular_separation_deg, tangent_plane_offsets_deg


def build_beam_model(config: ScienceConfig) -> BeamModel:
    return BeamModel(fwhm_deg=config.beam_fwhm_deg, source=config.beam_source,
                     status=config.beam_status, cutoff_n_fwhm=config.beam_cutoff_n_fwhm)


def build_grid(science_input: ScienceInput, beam: BeamModel, config: ScienceConfig) -> ScienceGrid:
    if not science_input.points:
        raise ValueError("cannot build a grid from zero input points")

    ra_deg = np.array([p.ra_deg for p in science_input.points])
    dec_deg = np.array([p.dec_degrees for p in science_input.points])

    # Center: mean position via a real vector mean on the sphere (not a
    # naive arithmetic mean of RA, which breaks near the wrap) - cheap and
    # exact enough for a field-of-view center.
    center_ra_deg = float(np.degrees(np.arctan2(
        np.mean(np.sin(np.radians(ra_deg))), np.mean(np.cos(np.radians(ra_deg))))) % 360.0)
    center_dec_deg = float(np.mean(dec_deg))

    x, y = tangent_plane_offsets_deg(ra_deg, dec_deg, center_ra_deg, center_dec_deg)
    margin_deg = config.extent_margin_beams * beam.fwhm_deg
    half_width = max(float(np.max(np.abs(x))), 1e-6) + margin_deg
    half_height = max(float(np.max(np.abs(y))), 1e-6) + margin_deg
    width_deg, height_deg = 2 * half_width, 2 * half_height

    pixel_scale_deg = config.pixel_scale_deg or (beam.fwhm_deg / config.pixels_per_beam)
    nx = max(int(np.ceil(width_deg / pixel_scale_deg)), 1)
    ny = max(int(np.ceil(height_deg / pixel_scale_deg)), 1)

    return ScienceGrid(frame="icrs", center_ra_deg=center_ra_deg, center_dec_deg=center_dec_deg,
                       width_deg=width_deg, height_deg=height_deg, pixel_scale_deg=pixel_scale_deg,
                       nx=nx, ny=ny, projection="tangent-plane")


def pixel_centers_deg(grid: ScienceGrid) -> tuple[np.ndarray, np.ndarray]:
    """Returns (ra_deg, dec_deg) 2D arrays, shape (ny, nx), of each pixel
    center's real sky coordinates (tangent-plane offset undone)."""
    x_edges = np.linspace(-grid.width_deg / 2, grid.width_deg / 2, grid.nx + 1)
    y_edges = np.linspace(-grid.height_deg / 2, grid.height_deg / 2, grid.ny + 1)
    x_centers = (x_edges[:-1] + x_edges[1:]) / 2
    y_centers = (y_edges[:-1] + y_edges[1:]) / 2
    xx, yy = np.meshgrid(x_centers, y_centers)  # (ny, nx)
    cos_dec = np.cos(np.radians(grid.center_dec_deg))
    ra_deg = grid.center_ra_deg + xx / cos_dec
    dec_deg = grid.center_dec_deg + yy
    return np.mod(ra_deg, 360.0), dec_deg


def point_to_pixel_separations_deg(grid: ScienceGrid, point_ra_deg: float, point_dec_deg: float) -> np.ndarray:
    """Real great-circle separation (section 14) from one input point to
    every pixel center - shape (ny, nx)."""
    pixel_ra, pixel_dec = pixel_centers_deg(grid)
    point_ra_arr = np.full(pixel_ra.shape, point_ra_deg)
    point_dec_arr = np.full(pixel_dec.shape, point_dec_deg)
    return angular_separation_deg(point_ra_arr, point_dec_arr, pixel_ra, pixel_dec)
