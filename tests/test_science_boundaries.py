"""BOUNDARIES & ROBUSTNESS (sections 5-6, 75-81, 93-95, 116-129): the input boundary, import boundary, no-network,
path safety, crash/interrupt/atomicity, validator corruption, false-good/false-bad, determinism, replay/compare.
Every test that needs a session builds a tiny synthetic REDUCE-format session (never RAW, never the real repo tree)
unless it says it uses a copy of a real REDUCE session.
"""
import ast
import io
import json
import os
import re
import shutil
import socket
import sys
from contextlib import redirect_stdout
from pathlib import Path

import h5py
import numpy as np
import pytest

import almita_science
from science_engine.compare import compare_sessions
from science_engine.config import ScienceConfig
from science_engine.ingest import ScienceContractError, load_science_input
from science_engine.products import run_science_session
from science_engine.simulation import (SyntheticPointSpec, build_synthetic_science_input, rectangular_grid_specs,
                                       write_synthetic_reduce_session)
from science_engine.storage import ScienceSession, arrays_sha256, validate_science_session
from science_engine.validation import SciencePreflightBlocked

ROOT = Path(__file__).resolve().parent.parent
REAL_SESSION = ROOT / "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411"


def tiny_si(seed=1, n=3, **kw):
    specs = rectangular_grid_specs(12.0, -30.0, n, n, spacing_deg=1.0)
    for i, s in enumerate(specs):
        s.velocity_offset_m_s = 300.0 * (i % 3)
    return build_synthetic_science_input(specs, n_channels=64, line_amplitude_fn=lambda r, d: 0.5, line_fwhm_m_s=60_000.0,
                                         noise_sigma=0.03, descending=True, rng=np.random.default_rng(seed), **kw)


def tiny_session(tmp, name="R", *, seed=1, si=None, **writer_kw):
    return write_synthetic_reduce_session(Path(tmp) / name, si or tiny_si(seed), **writer_kw)


def cfg(**kw):
    base = dict(beam_fwhm_deg=1.5, beam_source="operator_config", beam_status="CONFIGURED_OPERATIONAL",
                velocity_window_min_m_s=-1e5, velocity_window_max_m_s=1e5)
    return ScienceConfig(**{**base, **kw})


@pytest.fixture(scope="module")
def good_session(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("good")
    reduce_dir = tiny_session(tmp)
    report = run_science_session(str(reduce_dir), cfg(), output_root=str(tmp / "out"))
    return tmp, reduce_dir, Path(report.output_dir), report


def copy_session(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst


# ---------------------------------------------------------------- 5, 75, 76: input boundary, no RAW, no network

_AUDIT = {"active": False, "opened": [], "network": [], "subprocess": []}


def _audit_hook(event, args):
    if not _AUDIT["active"]:
        return
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        _AUDIT["opened"].append(os.fsdecode(args[0]))
    elif event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.bind"):
        _AUDIT["network"].append((event, args))
    elif event == "subprocess.Popen":
        _AUDIT["subprocess"].append(args)


sys.addaudithook(_audit_hook)


def _run_audited(fn):
    _AUDIT.update(active=True, opened=[], network=[], subprocess=[])
    try:
        return fn()
    finally:
        _AUDIT["active"] = False


@pytest.mark.skipif(not REAL_SESSION.is_dir(), reason="real REDUCE session not present")
def test_full_science_run_from_an_isolated_copy_of_a_real_reduce_session_never_touches_raw_or_network(tmp_path, monkeypatch):
    """Section 5: copy a REAL REDUCE session elsewhere, run from an unrelated cwd (so the repo's data/mosaic cannot even
    be reached by a relative path), audit every open()/socket/subprocess event. PASS = SCIENCE completed and no path
    under data/mosaic (or any IQ/capture HDF5) was opened, no socket touched."""
    isolated = copy_session(REAL_SESSION, tmp_path / "REDUCE_COPY")
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    def boom(*a, **k):
        raise AssertionError("network access attempted")
    for name in ("connect", "getaddrinfo", "gethostbyname", "create_connection"):
        monkeypatch.setattr(socket, name, boom, raising=False) if name != "connect" else monkeypatch.setattr(
            socket.socket, "connect", boom)

    report = _run_audited(lambda: run_science_session(str(isolated), cfg(beam_fwhm_deg=1.5), output_root=str(tmp_path / "out")))
    assert report.status == "COMPLETED"
    opened = _AUDIT["opened"]
    assert not [p for p in opened if "data/mosaic" in p or "/mosaic/" in p]
    data_files = [p for p in opened if p.endswith((".h5", ".hdf5", ".json", ".csv", ".npy")) and "site-packages" not in p
                  and "/proc/" not in p and "matplotlib" not in p and "astropy" not in p]
    for p in data_files:
        assert str(tmp_path) in p, f"SCIENCE opened a data file outside its isolated input/output dirs: {p}"
    assert not _AUDIT["network"]
    assert all(cmd and "git" in str(cmd[0]) for cmd in _AUDIT["subprocess"])      # only provenance's `git rev-parse HEAD`


def test_synthetic_run_also_needs_no_network(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    reduce_dir = tiny_session(tmp_path)
    report = run_science_session(str(reduce_dir), cfg(), output_root=str(tmp_path / "out"))
    assert report.status == "COMPLETED"


# ---------------------------------------------------------------- 6: import boundary

ALLOWED_REDUCE_MODULES = {"reduce_engine.models", "reduce_engine.science_contract"}
FORBIDDEN_IMPORT_ROOTS = {"capture", "rtl_tcp", "SoapySDR", "requests", "urllib", "http", "socket", "scipy", "pyrtlsdr",
                          "rtlsdr", "alignment_engine", "calibration_engine", "observe", "spectral"}


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


def test_import_boundary_science_never_imports_reduce_algorithms_hardware_or_network():
    files = sorted((ROOT / "science_engine").glob("*.py")) + [ROOT / "almita_science.py"]
    reduce_imports = set()
    for f in files:
        for mod in _imports(f):
            root = mod.split(".")[0]
            assert root not in FORBIDDEN_IMPORT_ROOTS, f"{f.name} imports {mod}"
            if root == "reduce_engine":
                reduce_imports.add(mod)
    assert reduce_imports <= ALLOWED_REDUCE_MODULES, reduce_imports - ALLOWED_REDUCE_MODULES


def test_no_spectral_processing_baseline_fitting_or_doppler_code_in_science():
    banned = re.compile(r"\b(fft|rfft|welch|periodogram|polyfit|savgol|EarthLocation|radial_velocity|get_body|"
                        r"barycorr|SoapySDR|rtl_tcp|iers)\b", re.I)
    for f in sorted((ROOT / "science_engine").glob("*.py")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#")[0]
            assert not banned.search(code), f"{f.name}:{n}: {line.strip()}"


# ---------------------------------------------------------------- 127-130: terminology and labels

BANNED_TERMS = re.compile(r"kelvin|\bK km/s|\bJy\b|brightness temperature|column density|\bTsys\b|\bTant\b|\bSEFD\b|\bN_HI\b|"
                          r"measured beam|true beam|instrument resolution|effective resolution|RFI spike", re.I)


def test_no_absolute_calibration_or_overclaiming_terminology_in_source_or_outputs(good_session):
    for f in sorted((ROOT / "science_engine").glob("*.py")) + [ROOT / "almita_science.py"]:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            assert not BANNED_TERMS.search(line), f"{f.name}:{n}: {line.strip()}"
    _, _, session, report = good_session
    blobs = [json.dumps(json.loads((session / n).read_text())) for n in ("manifest.json", "config.json", "provenance.json")]
    blobs += ["\n".join(report.human_summary_lines())]
    with h5py.File(session / "cube" / "science_cube.h5") as h:
        blobs += [str(v) for v in h.attrs.values()]
    for blob in blobs:
        assert not BANNED_TERMS.search(blob), BANNED_TERMS.search(blob).group(0)


def test_png_labels_say_relative_and_carry_the_configured_beam_wording(good_session, monkeypatch, tmp_path):
    import science_engine.qc_plots as qc
    captured = []
    monkeypatch.setattr(qc, "_save_map", lambda fig, ax, im, out, title, label: captured.append((title, label)))
    _, reduce_dir, session, _ = good_session
    qc.plot_science_map(session, "integrated_relative_intensity", tmp_path / "a.png")
    qc.plot_uncertainty_map(session, "integrated_relative_intensity", tmp_path / "b.png")
    qc.plot_coverage_map(session, tmp_path / "c.png")
    qc.plot_channel_map(session, 10, tmp_path / "d.png")
    assert captured
    for title, label in captured:
        assert not BANNED_TERMS.search(title + " " + label)
        assert "intensity" not in label.lower().replace("relative_intensity", "") or "relative" in label.lower()
    assert any("relative" in label.lower() for _, label in captured)
    assert len(captured) == 4
    for title, _ in captured:      # regression: uncertainty/coverage/channel plots used to print "FWHM=?"
        assert "configured beam FWHM=1.5 deg" in title and "?" not in title and "unknown" not in title
    assert any("n_pointings" in title and "not coverage" in title for title, _ in captured)


# ---------------------------------------------------------------- 77-78: path safety and collisions

def test_path_safety_and_collision_fail_closed(tmp_path):
    for bad in ("..", "a/b", "/abs", "x\\y", "", ".", "nul\x00"):
        with pytest.raises(ValueError):
            ScienceSession(tmp_path, "CAMP", session_id=bad)
        with pytest.raises(ValueError):
            ScienceSession(tmp_path, bad)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "CAMP").symlink_to(outside, target_is_directory=True)          # campaign dir is a symlink out of the root
    with pytest.raises(ValueError, match="escapes"):
        ScienceSession(root, "CAMP", session_id="S1")
    assert not any(outside.iterdir())
    ScienceSession(tmp_path / "ok", "CAMP", session_id="S1")
    with pytest.raises(FileExistsError):
        ScienceSession(tmp_path / "ok", "CAMP", session_id="S1")


def test_existing_session_id_collision_blocks_before_any_computation(tmp_path, monkeypatch):
    reduce_dir = tiny_session(tmp_path)
    monkeypatch.setattr("science_engine.storage.new_science_session_id", lambda now=None: "SCIENCE-FIXED")
    monkeypatch.setattr("science_engine.storage.ScienceSession.__init__.__defaults__", ("SCIENCE-FIXED",), raising=False)
    import science_engine.products as products
    calls = {"cube": 0}
    real = products.build_cube
    monkeypatch.setattr(products, "build_cube", lambda *a, **k: (calls.__setitem__("cube", calls["cube"] + 1), real(*a, **k))[1])
    root = tmp_path / "out"
    (root / "SYNTHETIC-GOLDEN" / "SCIENCE-FIXED").mkdir(parents=True)
    monkeypatch.setattr("science_engine.products.ScienceSession",
                        lambda output_root, campaign_id: ScienceSession(output_root, campaign_id, session_id="SCIENCE-FIXED"))
    with pytest.raises(FileExistsError):
        run_science_session(str(reduce_dir), cfg(), output_root=str(root))
    assert calls["cube"] == 0                                      # failed closed BEFORE the expensive step


# ---------------------------------------------------------------- 79-81: crash, interrupt, atomic HDF5

def _only_session(root: Path) -> Path:
    sessions = [p for c in root.iterdir() for p in c.iterdir()]
    assert len(sessions) == 1
    return sessions[0]


def test_crash_mid_science_leaves_a_failed_session_never_completed(tmp_path, monkeypatch):
    import science_engine.products as products
    monkeypatch.setattr(products, "integrated_map", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected crash")))
    reduce_dir = tiny_session(tmp_path)
    with pytest.raises(RuntimeError, match="injected crash"):
        run_science_session(str(reduce_dir), cfg(), output_root=str(tmp_path / "out"))
    session = _only_session(tmp_path / "out")
    manifest = json.loads((session / "manifest.json").read_text())
    assert manifest["status"] == "FAILED" and "injected crash" in manifest["error"]
    assert not list((session / "cube").glob("*.h5")) and not list((session / "cube").glob("*.tmp"))   # no half-written cube
    result = validate_science_session(session)
    assert not result["ok"] and any("FAILED" in p for p in result["problems"])


def test_crash_while_persisting_leaves_no_half_written_canonical_file(tmp_path, monkeypatch):
    reduce_dir = tiny_session(tmp_path)
    calls = {"n": 0}
    real = ScienceSession.write_map

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (injected)")
        return real(self, *a, **k)

    monkeypatch.setattr(ScienceSession, "write_map", flaky)
    with pytest.raises(OSError):
        run_science_session(str(reduce_dir), cfg(), output_root=str(tmp_path / "out"))
    session = _only_session(tmp_path / "out")
    assert json.loads((session / "manifest.json").read_text())["status"] == "FAILED"
    assert not list(session.rglob("*.tmp"))
    for h5 in session.rglob("*.h5"):
        with h5py.File(h5) as f:                                    # anything that exists at a canonical path is complete
            assert "science_schema_version" in f.attrs
    assert not validate_science_session(session)["ok"]


def test_keyboard_interrupt_marks_the_session_cancelled_not_completed(tmp_path, monkeypatch):
    import science_engine.products as products
    monkeypatch.setattr(products, "build_cube", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    reduce_dir = tiny_session(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_science_session(str(reduce_dir), cfg(), output_root=str(tmp_path / "out"))
    manifest = json.loads((_only_session(tmp_path / "out") / "manifest.json").read_text())
    assert manifest["status"] == "CANCELLED"


def test_cli_maps_keyboard_interrupt_to_exit_130_without_a_traceback(tmp_path, monkeypatch, capsys):
    import science_engine.products as products
    monkeypatch.setattr(products, "build_cube", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    reduce_dir = tiny_session(tmp_path)
    code = almita_science.main(["run", str(reduce_dir), "--beam-fwhm-deg", "1.5", "--output-root", str(tmp_path / "o")])
    assert code == 130 and "CANCELLED" in capsys.readouterr().err


def test_hdf5_is_finalised_by_rename_a_failed_replace_leaves_nothing_at_the_canonical_path(tmp_path, monkeypatch):
    import science_engine.storage as storage
    from science_engine.models import ScienceCube, ScienceGrid
    target = tmp_path / "x.h5"

    def bad_replace(src, dst):
        raise OSError("rename failed (injected)")
    monkeypatch.setattr(storage.os, "replace", bad_replace)
    with pytest.raises(OSError):
        with storage.atomic_h5(target) as h:
            h.create_dataset("a", data=np.arange(10))
    assert not target.exists() and not (tmp_path / "x.h5.tmp").exists()


def test_preflight_blocks_a_huge_grid_before_any_allocation_and_before_a_session_exists(tmp_path, monkeypatch):
    import science_engine.gridding as gridding
    monkeypatch.setattr(gridding.GriddingAccumulator, "__init__",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("allocated a cube despite preflight block")))
    reduce_dir = tiny_session(tmp_path)
    with pytest.raises(SciencePreflightBlocked, match="memory_estimate_within_budget"):
        run_science_session(str(reduce_dir), cfg(pixel_scale_deg=0.001), output_root=str(tmp_path / "out"))
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").rglob("manifest.json"))


# ---------------------------------------------------------------- 118-121: invalid configuration, hash determinism

@pytest.mark.parametrize("kw", [
    dict(pixel_scale_deg=0.0), dict(pixel_scale_deg=-0.1), dict(pixel_scale_deg=float("nan")), dict(pixel_scale_deg=float("inf")),
    dict(pixels_per_beam=0.0), dict(pixels_per_beam=float("nan")), dict(extent_margin_beams=-1.0),
    dict(beam_cutoff_n_fwhm=0.0), dict(beam_cutoff_n_fwhm=float("nan")),
    dict(velocity_window_min_m_s=100.0, velocity_window_max_m_s=-100.0), dict(velocity_window_min_m_s=5.0, velocity_window_max_m_s=5.0),
    dict(velocity_window_min_m_s=float("nan")), dict(velocity_window_max_m_s=float("inf")),
    dict(min_spectral_coverage_fraction=1.5), dict(min_spectral_coverage_fraction=float("nan")),
    dict(quality_policy="LAX"), dict(beam_status="MEASURED_ISH"), dict(moment_min_snr=float("nan")),
    dict(uncertainty_floor_relative=float("nan")),
])
def test_invalid_configuration_is_rejected_explicitly(kw):
    with pytest.raises(ValueError):
        cfg(**kw)


def test_config_hash_is_canonical_and_order_independent():
    a = cfg()
    d = a.to_dict()
    reordered = ScienceConfig(**dict(reversed(list(d.items()))))
    assert reordered.config_hash() == a.config_hash()
    assert cfg(beam_fwhm_deg=1.5).config_hash() == cfg(beam_fwhm_deg=1.50).config_hash()
    assert cfg(beam_fwhm_deg=1.5).config_hash() != cfg(beam_fwhm_deg=1.6).config_hash()
    assert cfg(quality_policy="STRICT").config_hash() != cfg().config_hash()


# ---------------------------------------------------------------- 93-95: determinism and ordering

def test_products_are_independent_of_input_listing_order_and_repeat_bit_for_bit(tmp_path):
    si = tiny_si(seed=4)
    fwd = write_synthetic_reduce_session(tmp_path / "fwd", si)
    rev = write_synthetic_reduce_session(tmp_path / "rev", si, write_order_reversed=True)
    m = json.loads((rev / "manifest.json").read_text())
    assert [p["point_index"] for p in m["points"]] == sorted((p["point_index"] for p in m["points"]), reverse=True)
    a = run_science_session(str(fwd), cfg(), output_root=str(tmp_path / "o1"))
    b = run_science_session(str(rev), cfg(), output_root=str(tmp_path / "o2"))
    c = run_science_session(str(fwd), cfg(), output_root=str(tmp_path / "o3"))
    for other in (b, c):
        for prod in ("cube/science_cube.h5", "maps/integrated_relative_intensity.h5",
                     "maps/moment1_like_velocity_centroid.h5", "maps/moment2_like_velocity_dispersion.h5"):
            assert arrays_sha256(Path(a.output_dir) / prod) == arrays_sha256(Path(other.output_dir) / prod)


# ---------------------------------------------------------------- 66-67, 126: partial semantics, schema, FAILED input

def test_partial_reduce_session_completes_execution_but_is_marked_partial_and_warning(tmp_path):
    si = tiny_si()
    reduce_dir = write_synthetic_reduce_session(
        tmp_path / "R", si, status="PARTIAL",
        extra_manifest_points=[{"point_index": 10, "status": "BLOCKED", "reason": "x"},
                               {"point_index": 11, "status": "FAILED", "reason": "y"}])
    report = run_science_session(str(reduce_dir), cfg(), output_root=str(tmp_path / "out"))
    manifest = json.loads((Path(report.output_dir) / "manifest.json").read_text())
    assert manifest["status"] == "COMPLETED"                                  # pipeline execution axis
    assert manifest["data_completeness"] == "PARTIAL"                          # data axis - never the same word
    assert manifest["input_reduce_session_status"] == "PARTIAL"
    assert manifest["quality"]["state"] == "WARNING" and "INPUT_PARTIAL" in manifest["quality"]["limitations"]
    assert manifest["n_points_in_reduce_manifest"] == 11 and manifest["n_input_points"] == 9
    with h5py.File(Path(report.output_dir) / "cube" / "science_cube.h5") as h:
        assert h.attrs["data_completeness"] == "PARTIAL"
    assert validate_science_session(report.output_dir)["ok"]


def test_unknown_or_failed_reduce_input_is_refused(tmp_path):
    si = tiny_si()
    bad_schema = write_synthetic_reduce_session(tmp_path / "S", si, reduce_schema_version="9.9")
    with pytest.raises(ScienceContractError):
        load_science_input(bad_schema)
    failed = write_synthetic_reduce_session(tmp_path / "F", si, status="FAILED")
    with pytest.raises(ScienceContractError, match="FAILED"):
        load_science_input(failed)


def test_ingest_records_identity_hashes_and_capture_refs_without_touching_raw(tmp_path):
    reduce_dir = tiny_session(tmp_path)
    loaded = load_science_input(reduce_dir)
    p = loaded.points[0]
    assert len(p.source_h5_sha256) == 64 and len(p.source_json_sha256) == 64
    assert p.capture_refs and p.capture_refs[0]["sha256"] == f"{p.point_index:064x}"
    assert [q.point_index for q in loaded.points] == sorted(q.point_index for q in loaded.points)


def test_ingest_excludes_and_records_nonfinite_or_missing_coordinates_and_wrong_frame(tmp_path):
    si = tiny_si()
    si.points[1].ra_hours = float("nan")
    si.points[2].dec_degrees = 95.0
    si.points[3].velocity_frame = "topocentric"
    reduce_dir = write_synthetic_reduce_session(tmp_path / "R", si)
    loaded = load_science_input(reduce_dir)
    reasons = {e["point_index"]: e["reason"] for e in loaded.exclusions}
    assert reasons[si.points[1].point_index].startswith("INVALID_COORDINATES")
    assert reasons[si.points[2].point_index].startswith("INVALID_COORDINATES")
    assert reasons[si.points[3].point_index].startswith("VELOCITY_FRAME_NOT_LSRK")
    assert len(loaded.points) == 6
    report = run_science_session(str(reduce_dir), cfg(), output_root=str(tmp_path / "out"))
    assert report.data_completeness == "PARTIAL" and report.quality_state == "WARNING"


# ---------------------------------------------------------------- 116-117: false-good / false-bad matrix

def _scenario_all_bad(tmp):
    si = tiny_si()
    for p in si.points:
        p.reduce_quality_state = "BAD"
    return si, cfg()


def _scenario_all_masked(tmp):
    si = tiny_si()
    for p in si.points:
        p.mask[:] = 2
        p.relative_intensity[:] = np.nan
        p.uncertainty[:] = np.nan
    return si, cfg()


def _scenario_no_beam_support(tmp):
    return tiny_si(), cfg(beam_cutoff_n_fwhm=1e-9)


def _scenario_extreme_uncertainty(tmp):
    si = tiny_si()
    for p in si.points:
        p.uncertainty[:] = 50.0
    return si, cfg()


def _scenario_overflow_uncertainty(tmp):
    si = tiny_si()
    for p in si.points:
        p.uncertainty[:] = 1e300
    return si, cfg()


def _scenario_all_nan_coordinates(tmp):
    si = tiny_si()
    for p in si.points:
        p.ra_hours = float("nan")
    return si, cfg()


def _scenario_window_outside_cube(tmp):
    return tiny_si(), cfg(velocity_window_min_m_s=5e5, velocity_window_max_m_s=6e5)


def _scenario_no_velocity(tmp):
    si = tiny_si()
    for p in si.points:
        p.velocity_lsrk_m_s = None
    return si, cfg()


FALSE_GOOD_SCENARIOS = {
    "all_inputs_BAD": _scenario_all_bad, "all_bins_masked": _scenario_all_masked,
    "no_beam_support_anywhere": _scenario_no_beam_support, "extreme_uncertainty_50": _scenario_extreme_uncertainty,
    "overflow_uncertainty_1e300": _scenario_overflow_uncertainty, "all_coordinates_NaN": _scenario_all_nan_coordinates,
    "window_outside_cube": _scenario_window_outside_cube, "no_velocity_axis": _scenario_no_velocity,
}


@pytest.mark.parametrize("name", list(FALSE_GOOD_SCENARIOS))
def test_no_adversarial_scenario_yields_a_good_or_valid_product(name, tmp_path):
    si, config = FALSE_GOOD_SCENARIOS[name](tmp_path)
    reduce_dir = write_synthetic_reduce_session(tmp_path / "R", si)
    try:
        report = run_science_session(str(reduce_dir), config, output_root=str(tmp_path / "out"))
    except Exception:
        return                                                                 # failing closed is the best outcome
    manifest = json.loads((Path(report.output_dir) / "manifest.json").read_text())
    assert manifest["quality"]["state"] not in ("GOOD",), name
    assert all(p["status"] != "VALID" for p in manifest["products"]), (name, [p["status"] for p in manifest["products"]])


def test_false_good_and_false_bad_counts(tmp_path, capsys):
    """Section 117: healthy fixtures must be GOOD/VALID (false-bad = 0); adversarial ones never (false-good = 0)."""
    outcomes = {"false_good": 0, "false_bad": 0, "adversarial_total": len(FALSE_GOOD_SCENARIOS), "healthy_total": 0}
    for name, make in FALSE_GOOD_SCENARIOS.items():
        d = tmp_path / name
        d.mkdir()
        si, config = make(d)
        reduce_dir = write_synthetic_reduce_session(d / "R", si)
        try:
            report = run_science_session(str(reduce_dir), config, output_root=str(d / "out"))
        except Exception:
            continue
        m = json.loads((Path(report.output_dir) / "manifest.json").read_text())
        if m["quality"]["state"] == "GOOD" or any(p["status"] == "VALID" for p in m["products"]):
            outcomes["false_good"] += 1
    for seed in (1, 2, 3):
        d = tmp_path / f"healthy{seed}"
        d.mkdir()
        reduce_dir = write_synthetic_reduce_session(d / "R", tiny_si(seed))
        report = run_science_session(str(reduce_dir), cfg(), output_root=str(d / "out"))
        m = json.loads((Path(report.output_dir) / "manifest.json").read_text())
        outcomes["healthy_total"] += 1
        # STANDARD-policy healthy sessions: GOOD state; every product VALID or (moment maps with no signal) BLOCKED
        if m["quality"]["state"] != "GOOD" or m["products"][0]["status"] != "VALID" or m["products"][1]["status"] != "VALID":
            outcomes["false_bad"] += 1
    with capsys.disabled():
        print(f"\nFALSE-GOOD/FALSE-BAD: adversarial scenarios={outcomes['adversarial_total']} false_good={outcomes['false_good']}; "
              f"healthy scenarios={outcomes['healthy_total']} false_bad={outcomes['false_bad']}")
    assert outcomes["false_good"] == 0 and outcomes["false_bad"] == 0


# ---------------------------------------------------------------- 123-125: self-validator and corruption

def _fresh_copy(good_session, tmp_path):
    _, _, session, _ = good_session
    return copy_session(session, tmp_path / "copy")


def test_validator_passes_a_pristine_session_and_checks_the_index(good_session):
    _, _, session, _ = good_session
    result = validate_science_session(session)
    assert result == {"ok": True, "problems": [], "products_checked": 4}
    manifest = json.loads((session / "manifest.json").read_text())
    for key in ("science_schema_version", "science_session_id", "input_reduce_session_id", "input_reduce_schema_version",
                "campaign_id", "config_hash", "status", "data_completeness", "beam", "grid", "velocity", "products",
                "quality", "input_reduce_manifest_sha256"):
        assert key in manifest, key
    for product in manifest["products"]:
        for key in ("id", "kind", "path", "shape", "unit", "status", "reasons", "sha256", "arrays_sha256"):
            assert key in product, (product["id"], key)


def _rewrite_h5_without(path: Path, drop: str):
    tmp = path.with_suffix(".x")
    with h5py.File(path) as src, h5py.File(tmp, "w") as dst:
        for k in src.keys():
            if k != drop:
                src.copy(k, dst)
        for k, v in src.attrs.items():
            dst.attrs[k] = v
    tmp.replace(path)


def _set_manifest(session: Path, mutate):
    m = json.loads((session / "manifest.json").read_text())
    mutate(m)
    (session / "manifest.json").write_text(json.dumps(m))


def test_validator_catches_every_kind_of_corruption(good_session, tmp_path):
    _, _, pristine, _ = good_session
    cases = {}

    def case(name, mutate, expect):
        d = tmp_path / name
        shutil.copytree(pristine, d)
        mutate(d)
        result = validate_science_session(d)
        assert not result["ok"], name
        assert any(expect in p for p in result["problems"]), (name, result["problems"])

    case("deleted_cube_dataset", lambda d: _rewrite_h5_without(d / "cube" / "science_cube.h5", "uncertainty"), "sha256 mismatch")
    def truncate(d):
        p = d / "maps" / "integrated_relative_intensity.h5"
        p.write_bytes(p.read_bytes()[: p.stat().st_size // 2])
    case("truncated_map", truncate, "sha256 mismatch")
    case("deleted_map", lambda d: (d / "maps" / "integrated_relative_intensity.h5").unlink(), "dangling reference")
    case("index_shape_mismatch", lambda d: _set_manifest(d, lambda m: m["products"][1].__setitem__("shape", [1, 1])),
         "shape attribute != index shape")
    case("index_status_invalid", lambda d: _set_manifest(d, lambda m: m["products"][0].__setitem__("status", "GREAT")), "invalid status")
    case("unknown_schema", lambda d: _set_manifest(d, lambda m: m.__setitem__("science_schema_version", "2.0")), "unknown science_schema_version")
    case("config_tampered", lambda d: (d / "config.json").write_text(json.dumps({**json.loads((d / "config.json").read_text()), "beam_fwhm_deg": 9.0})),
         "does not hash")
    case("status_running", lambda d: _set_manifest(d, lambda m: m.__setitem__("status", "RUNNING")), "not a finished science product")
    case("bad_beam", lambda d: _set_manifest(d, lambda m: m["beam"].__setitem__("fwhm_deg", -1.0)), "invalid beam fwhm_deg")
    case("unindexed_file", lambda d: shutil.copy(d / "maps" / "integrated_relative_intensity.h5", d / "maps" / "stray.h5"),
         "not in the manifest product index")
    case("leftover_tmp", lambda d: (d / "cube" / "science_cube.h5.tmp").write_bytes(b"x"), "leftover temporary file")
    case("completeness_lie", lambda d: _set_manifest(d, lambda m: m.update(input_reduce_session_status="PARTIAL", data_completeness="COMPLETE")),
         "data_completeness COMPLETE but")
    case("missing_provenance", lambda d: (d / "provenance.json").unlink(), "missing provenance.json")


# ---------------------------------------------------------------- 111, 89-92: CLI, QC summary, replay, compare

def test_cli_requires_an_explicit_beam_and_records_where_it_came_from(tmp_path, capsys):
    reduce_dir = tiny_session(tmp_path)
    assert almita_science.main(["plan", str(reduce_dir), "--output-root", str(tmp_path / "o")]) == 2
    assert "no beam specified" in capsys.readouterr().err
    assert almita_science.main(["plan", str(reduce_dir), "--beam-fwhm-deg", "1.5", "--beam-from-observer-config",
                                "--output-root", str(tmp_path / "o")]) == 2
    observer = tmp_path / "observer_config.json"
    observer.write_text(json.dumps({"observation_defaults": {"beam_fwhm_deg": 1.5}}))
    out = io.StringIO()
    with redirect_stdout(out):
        code = almita_science.main(["run", str(reduce_dir), "--beam-from-observer-config", str(observer),
                                    "--output-root", str(tmp_path / "o"), "--json"])
    assert code == 0
    session = Path(json.loads(out.getvalue())["output_dir"])
    beam = json.loads((session / "manifest.json").read_text())["beam"]
    import hashlib
    assert beam["source_path"] == str(observer.resolve()) and beam["source_field"] == "observation_defaults.beam_fwhm_deg"
    assert beam["source_sha256"] == hashlib.sha256(observer.read_bytes()).hexdigest() and beam["status"] == "CONFIGURED_OPERATIONAL"
    with h5py.File(session / "cube" / "science_cube.h5") as h:
        assert h.attrs["beam_source"].startswith("observer_config.json") and h.attrs["beam_status"] == "CONFIGURED_OPERATIONAL"
    # explicit CLI value: source recorded as operator_config
    out = io.StringIO()
    with redirect_stdout(out):
        almita_science.main(["run", str(reduce_dir), "--beam-fwhm-deg", "1.5", "--output-root", str(tmp_path / "o"), "--json"])
    m2 = json.loads((Path(json.loads(out.getvalue())["output_dir"]) / "manifest.json").read_text())
    assert m2["beam"]["source"] == "operator_config" and m2["beam"]["source_path"] is None


def test_cli_invalid_values_exit_2_with_a_clear_message(tmp_path, capsys):
    reduce_dir = tiny_session(tmp_path)
    for flag, value in (("--beam-fwhm-deg", "0"), ("--beam-fwhm-deg", "nan"), ("--beam-fwhm-deg", "-2"),
                        ("--beam-fwhm-deg", "200"), ("--pixel-scale-deg", "0")):
        args = ["plan", str(reduce_dir), "--output-root", str(tmp_path / "o")]
        args += [flag, value] if flag == "--beam-fwhm-deg" else ["--beam-fwhm-deg", "1.5", flag, value]
        assert almita_science.main(args) == 2, (flag, value)
        assert "ValueError" in capsys.readouterr().err


def test_run_prints_the_required_qc_summary(tmp_path):
    reduce_dir = tiny_session(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        assert almita_science.main(["run", str(reduce_dir), "--beam-fwhm-deg", "1.5", "--output-root", str(tmp_path / "o")]) == 0
    text = out.getvalue()
    for label in ("SCIENCE COMPLETED", "Input REDUCE:", "Campaign:", "Input points:", "Used points:", "Excluded points:", "Beam:",
                  "Grid:", "Cube:", "Velocity range:", "Velocity resampled:", "Integrated window:", "Valid map fraction:",
                  "Median uncertainty:", "Runtime:", "Peak RSS:", "Output:", "Data completeness:"):
        assert label in text, label


def _run_cli_json(args):
    out = io.StringIO()
    with redirect_stdout(out):
        code = almita_science.main([*args, "--json"])
    return code, json.loads(out.getvalue())


def test_replay_and_compare_matrix(tmp_path):
    """Section 92 A-F."""
    si = tiny_si(seed=7)
    si.points[2].reduce_quality_state = "WARNING"
    reduce_a = write_synthetic_reduce_session(tmp_path / "RA", si)
    out = str(tmp_path / "out")
    base = ["--beam-fwhm-deg", "1.5", "--output-root", out]
    _, first = _run_cli_json(["run", str(reduce_a), *base])
    original = first["output_dir"]

    # A: original vs replay -> equivalent (byte identical arrays)
    code, replay = _run_cli_json(["replay", original])
    assert code == 0 and replay["compare"]["verdict"] == "EQUIVALENT"
    assert {p["level"] for p in replay["compare"]["products"].values()} == {"BYTE IDENTICAL"}
    assert replay["replay_session_dir"] != original and Path(original).is_dir()          # never overwritten

    def compared(extra, reduce_dir=reduce_a):
        _, run = _run_cli_json(["run", str(reduce_dir), *base, *extra])
        code, result = _run_cli_json(["compare", original, run["output_dir"]])
        return code, result

    # B: beam FWHM changed -> detected
    code, r = compared(["--beam-fwhm-deg", "1.8"]) if False else (None, None)
    _, run_b = _run_cli_json(["run", str(reduce_a), "--beam-fwhm-deg", "1.8", "--output-root", out])
    code, r = _run_cli_json(["compare", original, run_b["output_dir"]])
    assert code == 1 and r["verdict"] == "DIFFERENT" and "fwhm_deg" in r["beam_diff"] and "beam_fwhm_deg" in r["config_diff"]
    assert any(p["level"] == "EXPECTED DIFFERENCE" for p in r["products"].values())

    # C: velocity window changed -> cube arrays identical, integrated product changed
    code, r = compared(["--velocity-window-min-m-s", "-50000", "--velocity-window-max-m-s", "50000"])
    assert r["velocity_window_diff"] and r["products"]["science_cube"]["level"] == "BYTE IDENTICAL"
    assert r["products"]["integrated_relative_intensity"]["level"] == "EXPECTED DIFFERENCE"

    # D: grid spacing changed -> detected (grid + shape)
    code, r = compared(["--pixel-scale-deg", "0.5"])
    assert "pixel_scale_deg" in r["grid_diff"] and r["cube_shape"][0] != r["cube_shape"][1]
    assert r["products"]["science_cube"]["level"] == "EXPECTED DIFFERENCE"

    # E: quality policy changed -> detected (STRICT drops the WARNING point)
    code, r = compared(["--quality-policy", "STRICT"])
    assert "quality_policy" in r["config_diff"] and r["products"]["science_cube"]["level"] == "EXPECTED DIFFERENCE"

    # F: different REDUCE session (same config, different data) -> detected via input identity
    reduce_b = write_synthetic_reduce_session(tmp_path / "RB", tiny_si(seed=8))
    m = json.loads((reduce_b / "manifest.json").read_text())
    m["reduce_session_id"] = "REDUCE-OTHER"
    (reduce_b / "manifest.json").write_text(json.dumps(m))
    code, r = compared([], reduce_dir=reduce_b)
    assert r["same_input"] is False and r["same_config"] is True and r["verdict"] == "DIFFERENT"
    assert r["products"]["science_cube"]["level"] == "EXPECTED DIFFERENCE"


def test_replay_refuses_when_the_reduce_input_changed(tmp_path, capsys):
    reduce_dir = tiny_session(tmp_path)
    _, first = _run_cli_json(["run", str(reduce_dir), "--beam-fwhm-deg", "1.5", "--output-root", str(tmp_path / "o")])
    m = json.loads((reduce_dir / "manifest.json").read_text())
    m["median_usable_fraction"] = 0.5
    (reduce_dir / "manifest.json").write_text(json.dumps(m))
    assert almita_science.main(["replay", first["output_dir"]]) == 1
    assert "changed since the original run" in capsys.readouterr().err


def test_compare_flags_an_unexpected_difference_when_input_and_config_are_identical(tmp_path):
    """A determinism bug must not hide behind 'expected difference'."""
    reduce_dir = tiny_session(tmp_path)
    _, a = _run_cli_json(["run", str(reduce_dir), "--beam-fwhm-deg", "1.5", "--output-root", str(tmp_path / "o")])
    _, b = _run_cli_json(["run", str(reduce_dir), "--beam-fwhm-deg", "1.5", "--output-root", str(tmp_path / "o")])
    with h5py.File(Path(b["output_dir"]) / "cube" / "science_cube.h5", "r+") as h:
        h["relative_intensity"][0, 0, 0] = 123.0                              # tamper with one voxel
    r = compare_sessions(a["output_dir"], b["output_dir"])
    assert r["products"]["science_cube"]["level"] == "UNEXPECTED DIFFERENCE"


def test_manifest_and_products_report_the_two_status_axes_and_no_ambiguous_status(good_session):
    _, _, session, report = good_session
    m = json.loads((session / "manifest.json").read_text())
    assert m["status"] == "COMPLETED" and m["data_completeness"] == "COMPLETE"
    assert report.status == "COMPLETED" and report.data_completeness == "COMPLETE"
    assert "Data completeness:" in "\n".join(report.human_summary_lines())
