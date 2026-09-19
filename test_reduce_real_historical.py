"""Real historical dry run (docs/REDUCE_FIRST_RUNBOOK.md milestone 3):
runs the FULL REDUCE pipeline, unmodified, against one real, completed,
non-active OBSERVE campaign - read-only, offline, no hardware. This is
the same campaign used for the delivery report's section L.

Explicitly NOT the currently-active campaign - ALMITA-WEB-SMALL-RUN-01
completed on 2026-09-02 and is a small (9-point), safe, real fixture.

Module-scoped fixtures run the (real, non-trivial: 9 real FFTs + real
astropy frame conversions) campaign reduction exactly ONCE for the whole
file and share it across assertions, instead of once per test - the
pipeline itself is exercised in full either way.
"""
from pathlib import Path

import pytest

from reduce_engine.compare import compare_sessions
from reduce_engine.config import ReduceConfig
from reduce_engine.ingest import discover_campaign
from reduce_engine.pipeline import reduce_campaign
from reduce_engine.replay import replay_session
from reduce_engine.storage import ReduceSession, load_master_spectrum
from reduce_engine.validation import blocking_reason, run_preflight

REAL_CAMPAIGN_DIR = "data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16"
# A real, gain/frequency/topology-compatible calibration profile for this
# exact campaign (both were built at 40.2dB, 1420405752Hz, 2.4MHz on the
# same instrument chain) - using it also exercises the fast path in
# masks.build_mask (DC/spur mask reused from profile evidence, skipping
# the much more expensive per-capture measurement) and demonstrates the
# RELATIVE calibration_level end to end on real data.
REAL_CALIBRATION_PROFILE = "data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1"


def _skip_if_campaign_absent():
    if not Path(REAL_CAMPAIGN_DIR).is_dir():
        pytest.skip(f"real historical fixture campaign not present: {REAL_CAMPAIGN_DIR}")


def _calibration_profile_path_if_present():
    path = Path(REAL_CALIBRATION_PROFILE)
    return str(path) if path.with_suffix(".json").exists() and path.with_suffix(".npz").exists() else None


@pytest.fixture(scope="module")
def output_root(tmp_path_factory):
    return tmp_path_factory.mktemp("reduced")


@pytest.fixture(scope="module")
def real_manifest():
    _skip_if_campaign_absent()
    return discover_campaign(REAL_CAMPAIGN_DIR)


@pytest.fixture(scope="module")
def real_config():
    return ReduceConfig()  # the real V1 default config, unmodified


@pytest.fixture(scope="module")
def calibration_profile_path():
    return _calibration_profile_path_if_present()


@pytest.fixture(scope="module")
def first_run(real_manifest, real_config, output_root, calibration_profile_path):
    return reduce_campaign(real_manifest, real_config, output_root=str(output_root),
                           calibration_profile_path=calibration_profile_path)


@pytest.fixture(scope="module")
def replayed_run(first_run, output_root, calibration_profile_path):
    return replay_session(first_run.output_dir, output_root=str(output_root),
                          calibration_profile_path=calibration_profile_path)


def test_real_campaign_is_discovered_with_all_nine_points_accepted(real_manifest):
    assert real_manifest.campaign_id == "ALMITA-WEB-SMALL-RUN-01"
    assert len(real_manifest.points) == 9
    assert len(real_manifest.accepted_points()) == 9


def test_real_campaign_preflight_passes_offline(real_manifest, real_config, output_root):
    checks = run_preflight(real_manifest, real_config, output_root=str(output_root), calibration_profile_path=None)
    assert blocking_reason(checks) is None


def test_real_campaign_full_run_completes_and_produces_master_spectra(first_run):
    assert first_run.status in ("COMPLETED", "PARTIAL")
    assert first_run.points_completed >= 1
    assert first_run.points_discovered == 9

    metadata, arrays = load_master_spectrum(Path(first_run.output_dir) / "points" / "1")
    assert metadata["calibration_level"] in ("RELATIVE", "UNCALIBRATED")
    assert metadata["quality"]["state"] in ("GOOD", "WARNING", "BAD", "UNKNOWN")
    assert "frequency_hz" in arrays and "relative_intensity" in arrays and "mask" in arrays
    assert arrays["frequency_hz"].shape == arrays["relative_intensity"].shape


def test_replay_reprocesses_from_raw_into_a_new_session_deterministically(first_run, replayed_run):
    assert replayed_run.session_id != first_run.session_id
    assert replayed_run.status in ("COMPLETED", "PARTIAL")

    comparison = compare_sessions(first_run.output_dir, replayed_run.output_dir)
    assert comparison["same_source_campaign"]
    assert comparison["same_config"]
    for point_index, rms in comparison["per_point_rms_difference"].items():
        assert rms == pytest.approx(0.0, abs=1e-9), f"replay must be deterministic for point {point_index}"


def test_reduce_session_directory_is_immutable_against_accidental_reuse(first_run, output_root, real_manifest):
    with pytest.raises(FileExistsError):
        ReduceSession(output_root, real_manifest.campaign_id, session_id=first_run.session_id)
