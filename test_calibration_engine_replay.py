"""Calibration replay (Fase 37): re-analyze existing captures without
touching SDR/mount, source immutable, new analysis version each call."""
import asyncio
from pathlib import Path

import h5py
import numpy as np
import pytest

from calibration_engine.acquisition import SimulatedCalibrationAcquisitionBackend
from calibration_engine.replay import replay_calibration_session
from calibration_engine.simulation import InstrumentSimulationConfig


def _build_session_with_captures(tmp_path, n=5, seed_base=0):
    session_dir = tmp_path / "CAL-fixture"
    captures_dir = session_dir / "captures"
    captures_dir.mkdir(parents=True)
    backend = SimulatedCalibrationAcquisitionBackend(
        simulation_config_factory=lambda i: InstrumentSimulationConfig(seed=seed_base + i, gain_linear=15.0))

    async def _fill():
        for i in range(n):
            path = captures_dir / f"capture_{i:03d}.h5"
            await backend.capture(duration_seconds=1.0, output_path=str(path), center_frequency_hz=1_420_405_752.0,
                                   sample_rate_hz=2_400_000.0, gain_db=40.2, metadata={"index": i}, index=i)
    asyncio.run(_fill())
    return session_dir


def test_replay_never_imports_sdr_or_mount_modules():
    import ast
    import calibration_engine.replay as replay_module
    tree = ast.parse(Path(replay_module.__file__).read_text())
    forbidden = {"sdr_capture", "indi_telescope_control", "alignment_engine.tracking", "alignment_engine.mount_adapter"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not (imported & forbidden)


def test_replay_creates_new_analysis_dir_and_leaves_captures_untouched(tmp_path):
    session_dir = _build_session_with_captures(tmp_path)
    capture_files_before = sorted(p.name for p in (session_dir / "captures").iterdir())
    result = replay_calibration_session(str(session_dir))
    assert Path(result.analysis_dir).exists()
    assert Path(result.analysis_dir).parent.name == "analysis"
    capture_files_after = sorted(p.name for p in (session_dir / "captures").iterdir())
    assert capture_files_before == capture_files_after
    assert result.n_captures_found == 5
    assert result.n_captures_valid == 5


def test_replay_twice_produces_two_separately_versioned_analyses(tmp_path):
    session_dir = _build_session_with_captures(tmp_path)
    result_a = replay_calibration_session(str(session_dir), label="run1")
    result_b = replay_calibration_session(str(session_dir), label="run2")
    assert result_a.analysis_dir != result_b.analysis_dir
    assert Path(result_a.analysis_dir).exists() and Path(result_b.analysis_dir).exists()


def test_replay_is_deterministic_given_the_same_raw_captures(tmp_path):
    session_dir = _build_session_with_captures(tmp_path)
    result_a = replay_calibration_session(str(session_dir), label="a")
    result_b = replay_calibration_session(str(session_dir), label="b")
    stats_a = [c["sample_statistics"]["rms"] for c in result_a.per_capture]
    stats_b = [c["sample_statistics"]["rms"] for c in result_b.per_capture]
    assert stats_a == pytest.approx(stats_b)


def test_replay_marks_corrupted_capture_invalid_not_fabricated(tmp_path):
    session_dir = _build_session_with_captures(tmp_path, n=3)
    bad_path = session_dir / "captures" / "capture_001.h5"
    bad_path.unlink()
    bad_path.write_bytes(b"not a valid hdf5 file")
    result = replay_calibration_session(str(session_dir))
    assert result.n_captures_valid == 2
    bad_record = next(r for r in result.per_capture if "capture_001" in r["source_file"])
    assert bad_record["valid"] is False
    assert "reason" in bad_record


def test_replay_marks_partial_capture_invalid(tmp_path):
    session_dir = _build_session_with_captures(tmp_path, n=3)
    partial_path = session_dir / "captures" / "capture_001.h5"
    partial_path.unlink()
    with h5py.File(partial_path, "w") as handle:
        handle.create_dataset("iq_data", data=np.zeros(100, dtype=np.uint8))
        handle.attrs["capture_status"] = "in_progress"
    result = replay_calibration_session(str(session_dir))
    bad_record = next(r for r in result.per_capture if "capture_001" in r["source_file"])
    assert bad_record["valid"] is False


def test_replay_with_no_captures_directory_reports_zero_found(tmp_path):
    session_dir = tmp_path / "CAL-empty"
    session_dir.mkdir()
    result = replay_calibration_session(str(session_dir))
    assert result.n_captures_found == 0
    assert result.n_captures_valid == 0


def test_replay_computes_stability_when_enough_captures_have_timestamps(tmp_path):
    session_dir = _build_session_with_captures(tmp_path, n=5)
    result = replay_calibration_session(str(session_dir))
    assert result.stability is not None
    assert result.stability["n_samples"] == 5


def test_replay_skips_stability_with_too_few_captures(tmp_path):
    session_dir = _build_session_with_captures(tmp_path, n=2)
    result = replay_calibration_session(str(session_dir))
    assert result.stability is None
