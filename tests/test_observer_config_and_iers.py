import json
from pathlib import Path

import pytest

from capture import CaptureExecutor
from grid_generator import GridGenerator

REAL_CONFIG = {
    "observer": {"name": "Santiago Observatory", "latitude_deg": -33.4489,
                 "longitude_deg": -70.6693, "elevation_m": 570},
    "hardware": {"telescope": "LX200 OnStep", "sdr_device": "RTL-SDR Blog V4", "antenna_type": "Dipole"},
    "observation_defaults": {"beam_fwhm_deg": 20.0, "beam_sampling_fraction": 0.3333333333},
}


def make_grid(tmp_path, config=None):
    config = config if config is not None else REAL_CONFIG
    config_path = tmp_path / "observer_config.json"
    config_path.write_text(json.dumps(config, indent=2))
    gen = GridGenerator(session_name="TEST-FINAL-100", base_dir=str(tmp_path / "mosaic"),
                         beam_fwhm_deg=20.0, beam_sampling_fraction=1.0 / 6.0)
    gen.persist_observer_config(config_path)
    gen.generate_grid_plan(center_ra=15.0, center_dec=-33.4489, width_deg=30.0, height_deg=30.0)
    return gen, config_path


# ---------------------------------------------------------------- §7: grid geometry unchanged

def test_grid_geometry_unchanged_100_points_10x10_30x30(tmp_path):
    gen, _ = make_grid(tmp_path)
    metadata = json.loads((gen.output_dir / "grid_metadata.json").read_text())["grid"]
    assert metadata["total_points"] == 100
    assert metadata["rows"] == 10 and metadata["columns"] == 10
    assert metadata["width_deg"] == 30.0 and metadata["height_deg"] == 30.0
    assert metadata["beam_fwhm_deg"] == 20.0
    assert abs(metadata["beam_sampling_fraction"] - 1.0 / 6.0) < 1e-9


# ---------------------------------------------------------------- §1/§5: observer config persistence

def test_grid_generator_persists_exact_observer_config_copy(tmp_path):
    gen, config_path = make_grid(tmp_path)
    persisted = gen.output_dir / "observer_config.json"
    assert persisted.is_file()
    assert persisted.read_bytes() == config_path.read_bytes()  # byte-exact, no re-serialization


def test_grid_generator_reports_none_when_config_missing(tmp_path):
    gen = GridGenerator(session_name="NOCFG", base_dir=str(tmp_path / "mosaic"))
    result = gen.persist_observer_config(tmp_path / "does_not_exist.json")
    assert result is None
    assert not (gen.output_dir / "observer_config.json").exists()


def test_capture_resolves_exact_persisted_config_from_a_different_cwd(tmp_path, monkeypatch):
    gen, _ = make_grid(tmp_path)
    other_cwd = tmp_path / "somewhere_else"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    ex = CaptureExecutor(str(gen.csv_filepath))  # absolute csv path, unrelated cwd
    assert ex.observer_config_valid is True
    assert ex.observer_config_preexisting is True
    assert ex.observer_config_generated_by_capture is False
    assert ex.observer_config["hardware"]["telescope"] == "LX200 OnStep"
    assert ex.device_name == "LX200 OnStep"  # no --device needed
    assert ex.observer_config["observer"]["latitude_deg"] == -33.4489


def test_capture_never_creates_default_config_for_a_real_campaign(tmp_path):
    gen, _ = make_grid(tmp_path)
    # Simulate the exact reported scenario: CSV present, but its colocated
    # observer_config.json is absent (e.g. an operator manually deletes it,
    # or a grid was produced before this fix).
    (gen.output_dir / "observer_config.json").unlink()
    ex = CaptureExecutor(str(gen.csv_filepath))
    assert ex.observer_config_valid is False
    assert not (gen.output_dir / "observer_config.json").exists()
    ok = ex.load_observation_plan(resume=False)
    assert ok is False  # fail closed, clear error - not a silent default


def test_capture_fails_closed_on_corrupted_persisted_config(tmp_path):
    gen, _ = make_grid(tmp_path)
    (gen.output_dir / "observer_config.json").write_text("{not valid json")
    ex = CaptureExecutor(str(gen.csv_filepath))
    assert ex.observer_config_valid is False
    ok = ex.load_observation_plan(resume=False)
    assert ok is False


def test_root_observer_config_has_real_onstep_mount():
    """The Almita root observer_config.json (used as the --config source for
    new grids) must already reflect the real physical mount."""
    cfg = json.loads(Path("observer_config.json").read_text())
    assert cfg["hardware"]["telescope"] == "LX200 OnStep"
    assert "latitude_deg" in cfg["observer"] and "longitude_deg" in cfg["observer"]


# ---------------------------------------------------------------- §3/§6: offline IERS policy

def test_astropy_offline_disables_both_download_and_max_age():
    from astropy_offline import configure_astropy_offline
    configure_astropy_offline()
    from astropy.utils import iers
    assert iers.conf.auto_download is False
    assert iers.conf.auto_max_age is None


def test_iers_policy_reproduces_and_then_resolves_the_reported_error(monkeypatch):
    """Proves the failure mode is real (fails when auto_max_age is at its
    default) and that Almita's actual configured policy (auto_max_age=None)
    resolves it - using the exact same real-time call path that broke in
    the field (Time.now().sidereal_time(...))."""
    from astropy.utils import iers
    import astropy.units as u
    from astropy.coordinates import EarthLocation
    from astropy.time import Time

    location = EarthLocation(lat=-33.4489 * u.deg, lon=-70.6693 * u.deg, height=570 * u.m)

    monkeypatch.setattr(iers.conf, "auto_max_age", 30.0)
    with pytest.raises(ValueError, match="predictive values"):
        Time.now().sidereal_time("apparent", longitude=location.lon)

    monkeypatch.setattr(iers.conf, "auto_max_age", None)
    lst = Time.now().sidereal_time("apparent", longitude=location.lon)
    assert lst is not None


def test_capture_plan_loading_survives_iers_offline_policy(tmp_path):
    """The exact real path that broke in the field: load_observation_plan()
    -> partition_pending_by_hour_angle() -> hour_angle_for_point() using
    Time.now(), with a fully valid, self-contained session."""
    gen, _ = make_grid(tmp_path)
    ex = CaptureExecutor(str(gen.csv_filepath))
    assert ex.load_observation_plan(resume=False) is True
    assert len(ex.observation_points) == 100


def test_no_network_module_used_by_astropy_offline_or_capture_iers_path():
    for path in ("astropy_offline.py",):
        source = Path(path).read_text()
        assert "requests" not in source
        assert "urllib" not in source
