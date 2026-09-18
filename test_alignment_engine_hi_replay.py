"""Fase 17: replay mode - re-analysis of raw evidence without touching
mount/SDR, with proper analysis versioning and reproducibility.

Uses a controlled fixture: the spectral-reduction step
(_compute_spectrum_from_iq_file) is mocked to return a KNOWN, deterministic
(velocity, power) array keyed by point index - this tests replay_session()'s
OWN orchestration (reading session evidence, versioning, not touching
history, reproducibility) rather than re-testing FFT correctness (already
covered in test_alignment_engine_hi_acquisition_and_policy.py). Raw .h5
files are still created on disk (empty) so the "does this point have raw
data" check exercises the real file-existence logic.
"""
import json
from pathlib import Path
from unittest.mock import patch

import dataclasses

import numpy as np
import pytest
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
import astropy.units as u

from alignment_engine.hi.fits_reference import validate_reference
from alignment_engine.hi.reference_trust import ReferenceTrust, build_manifest_for_file
from alignment_engine.hi.spectral_pipeline import SpectralPipelineConfig
from alignment_engine.scan_planner import build_raster


def _write_fixture_reference(path, amplitude_scale=5.0):
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["GLON-CAR", "GLAT-CAR"]
    wcs.wcs.crpix = [90, 90]
    wcs.wcs.crval = [30.0, 0.0]
    wcs.wcs.cdelt = [-0.2, 0.2]
    ys, xs = np.mgrid[0:181, 0:181]
    lon, lat = wcs.wcs_pix2world(xs, ys, 0)
    data = amplitude_scale * np.exp(-0.5 * (((lon - 30 + 180) % 360 - 180) ** 2 + lat ** 2) / 3.0 ** 2) + 0.5
    header = wcs.to_header()
    header["BUNIT"] = "K"
    fits.PrimaryHDU(data=data.astype(np.float32), header=header).writeto(path, overwrite=True)


_REST_FREQ_HZ = 1_420_405_751.77


def _fake_spectrum_for_point(velocity_center=0.0, amplitude=10.0):
    """Returns (frequency_hz, power) - the SAME shape
    _compute_spectrum_from_iq_file() itself returns (replay.py converts
    frequency -> velocity internally; a mock returning velocity directly
    would silently mis-convert and starve the line window)."""
    velocity = np.linspace(-150, 150, 512)
    frequency_hz = _REST_FREQ_HZ * (1.0 - velocity / 299792.458)
    sigma = 20.0 / (2 * np.sqrt(2 * np.log(2)))
    power = amplitude * np.exp(-0.5 * ((velocity - velocity_center) / sigma) ** 2) + 0.1
    return frequency_hz, power


def _build_fixture_session(tmp_path, true_east=0.0, true_north=0.0, span_deg=6.0, spacing_deg=3.0,
                            missing_indices=()):
    fits_path = tmp_path / "ref.fits"
    _write_fixture_reference(str(fits_path))
    manifest = build_manifest_for_file(str(fits_path), survey="TEST", version="v1", source="unit test",
                                        coordinate_system="Galactic", spectral_axis="none", units="K",
                                        trust=ReferenceTrust.TEST_FIXTURE)
    validation = validate_reference(str(fits_path), manifest)
    assert validation.ok, validation.reason
    manifest = dataclasses.replace(manifest, trust=ReferenceTrust.REAL_VALIDATED)
    manifest_path = tmp_path / "ref.manifest.json"
    manifest.write(str(manifest_path))

    session_dir = tmp_path / "HI-fixture-session"
    (session_dir / "points").mkdir(parents=True)

    center = SkyCoord(l=30.0 * u.deg, b=0.0 * u.deg, frame="galactic").icrs
    points = build_raster(span_deg, spacing_deg)

    config = {"fits_path": str(fits_path), "manifest_path": str(manifest_path), "beam_fwhm_deg": 6.0,
              "pixel_catalog_stride": 3, "raster_span_deg": span_deg, "velocity_window_km_s": [-60.0, 60.0]}
    (session_dir / "alignment_config.json").write_text(json.dumps(config))
    (session_dir / "target.json").write_text(json.dumps({
        "icrs_ra_hours": float(center.ra.hour), "icrs_dec_deg": float(center.dec.deg)}))
    (session_dir / "raw_grid.json").write_text(json.dumps({
        "points": [{"east_deg": p.east_deg, "north_deg": p.north_deg, "row": p.row, "col": p.col} for p in points],
        "values": [1.0] * len(points),
    }))
    for i, p in enumerate(points):
        if i in missing_indices:
            continue
        (session_dir / "points" / f"point_{i:04d}.h5").write_bytes(b"")  # existence is all that's checked (mocked reader)
    return session_dir, points


@pytest.fixture
def fixture_session(tmp_path):
    return _build_fixture_session(tmp_path)


def _mock_reader_factory(amplitude_by_index, missing_indices=()):
    call_state = {"path_to_index": {}}

    def _mock(path):
        # infer index from the filename convention point_NNNN.h5
        index = int(Path(path).stem.split("_")[1])
        if index in missing_indices:
            raise ValueError("insufficient IQ samples")
        return _fake_spectrum_for_point(amplitude=amplitude_by_index[index])
    return _mock


def test_replay_creates_new_analysis_directory_and_leaves_history_untouched(fixture_session, tmp_path):
    session_dir, points = fixture_session
    original_raw_grid = (session_dir / "raw_grid.json").read_text()
    amplitude = [5.0] * len(points)
    with patch("alignment_engine.hi.replay._compute_spectrum_from_iq_file", side_effect=_mock_reader_factory(amplitude)):
        from alignment_engine.hi.replay import replay_session
        result = replay_session(str(session_dir), bootstrap_iterations=5)

    assert Path(result.analysis_dir).exists()
    assert Path(result.analysis_dir).parent.name == "analyses"
    assert (session_dir / "raw_grid.json").read_text() == original_raw_grid  # untouched
    assert (Path(result.analysis_dir) / "analysis_result.json").exists()
    assert (Path(result.analysis_dir) / "source_session.json").exists()


def test_replay_never_imports_mount_or_sdr_modules():
    """Structural guarantee, not just behavioral - mirrors
    test_async_architecture_boundary.py's own AST-based style."""
    import ast
    import alignment_engine.hi.replay as replay_module
    source = Path(replay_module.__file__).read_text()
    tree = ast.parse(source)
    forbidden = {"sdr_capture", "indi_telescope_control", "alignment_engine.tracking", "alignment_engine.mount_adapter"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not (imported & forbidden), f"replay.py must never import: {imported & forbidden}"


def test_replay_is_deterministic_with_same_config_and_seed(fixture_session):
    session_dir, points = fixture_session
    amplitude = [5.0 + 3.0 * np.exp(-((p.east_deg - 1.0) ** 2 + (p.north_deg + 0.5) ** 2) / 4.0) for p in points]
    from alignment_engine.hi.replay import replay_session

    with patch("alignment_engine.hi.replay._compute_spectrum_from_iq_file", side_effect=_mock_reader_factory(amplitude)):
        result_a = replay_session(str(session_dir), bootstrap_iterations=5, label="run1")
    with patch("alignment_engine.hi.replay._compute_spectrum_from_iq_file", side_effect=_mock_reader_factory(amplitude)):
        result_b = replay_session(str(session_dir), bootstrap_iterations=5, label="run2")

    assert result_a.fit["estimate"]["offset_ra_deg"] == pytest.approx(result_b.fit["estimate"]["offset_ra_deg"], abs=1e-9)
    assert result_a.fit["estimate"]["offset_dec_deg"] == pytest.approx(result_b.fit["estimate"]["offset_dec_deg"], abs=1e-9)
    assert result_a.analysis_dir != result_b.analysis_dir  # separately versioned


def test_replay_with_different_config_produces_a_separately_versioned_result(fixture_session):
    session_dir, points = fixture_session
    amplitude = [5.0] * len(points)
    from alignment_engine.hi.replay import replay_session

    narrow = SpectralPipelineConfig(line_window_km_s=(-20.0, 20.0), baseline_exclusion_km_s=(-20.0, 20.0))
    wide = SpectralPipelineConfig(line_window_km_s=(-100.0, 100.0), baseline_exclusion_km_s=(-100.0, 100.0))

    with patch("alignment_engine.hi.replay._compute_spectrum_from_iq_file", side_effect=_mock_reader_factory(amplitude)):
        result_narrow = replay_session(str(session_dir), pipeline_config=narrow, bootstrap_iterations=5, label="narrow")
    with patch("alignment_engine.hi.replay._compute_spectrum_from_iq_file", side_effect=_mock_reader_factory(amplitude)):
        result_wide = replay_session(str(session_dir), pipeline_config=wide, bootstrap_iterations=5, label="wide")

    assert result_narrow.analysis_dir != result_wide.analysis_dir
    narrow_config = json.loads((Path(result_narrow.analysis_dir) / "analysis_config.json").read_text())
    wide_config = json.loads((Path(result_wide.analysis_dir) / "analysis_config.json").read_text())
    assert narrow_config["spectral_pipeline"]["line_window_km_s"] != wide_config["spectral_pipeline"]["line_window_km_s"]


def test_replay_missing_raw_file_marks_point_invalid_not_fabricated(tmp_path):
    session_dir, points = _build_fixture_session(tmp_path, missing_indices={2, 5})
    amplitude = [5.0] * len(points)
    from alignment_engine.hi.replay import replay_session

    with patch("alignment_engine.hi.replay._compute_spectrum_from_iq_file", side_effect=_mock_reader_factory(amplitude)):
        result = replay_session(str(session_dir), bootstrap_iterations=5)

    details = json.loads((Path(result.analysis_dir) / "reduction_details.json").read_text())
    assert details["values"][2] is None
    assert details["values"][5] is None
    assert details["points"][2]["reason"].startswith("no raw IQ")


def test_replay_failed_reduction_leaves_no_analysis_directory_and_history_intact(fixture_session):
    """A failure during fit/quality (AFTER enough points reduced OK, but
    BEFORE the analysis directory is created - see replay.py's ordering:
    fit_raster/evaluate_quality_v2 both run before analysis_dir.mkdir())
    must not leave a partial analysis directory, and must never touch the
    source session's own evidence files.

    Per-point reduction failures are a DIFFERENT, non-fatal case already
    covered by test_replay_missing_raw_file_marks_point_invalid_not_fabricated
    - compute_spectral_metric errors are caught per-point and recorded, by
    design, since one bad point should not lose the rest of the raster."""
    session_dir, points = fixture_session
    original_target = (session_dir / "target.json").read_text()
    amplitude = [5.0] * len(points)
    from alignment_engine.hi.replay import replay_session

    with patch("alignment_engine.hi.replay._compute_spectrum_from_iq_file", side_effect=_mock_reader_factory(amplitude)):
        with patch("alignment_engine.hi.replay.fit_raster", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                replay_session(str(session_dir), bootstrap_iterations=5)

    assert (session_dir / "target.json").read_text() == original_target
    analyses_dir = session_dir / "analyses"
    assert not analyses_dir.exists() or list(analyses_dir.iterdir()) == []
