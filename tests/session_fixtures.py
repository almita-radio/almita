"""A small fake data/ tree for the SESSIONS tests, shaped like the real files (BL-080 contracts,
docs/SESSIONS_FILE_CONTRACTS.md). Built in tmp_path: no test reads or writes the real data/."""
import csv
import hashlib
import json
from pathlib import Path

MOSAIC_FIELDS = ["point_number", "point_id", "scan_order", "grid_row", "grid_col", "ra", "dec", "capture_status", "start_time",
                 "end_time", "error_message", "data_filename", "altitude_deg_at_goto", "azimuth_deg_at_goto", "visibility_deferred",
                 "error_code", "failed_at"]

A = "ALMITA-OBSERVE-20261018-21:27:05"        # completed, 3 points, two finished REDUCE + one interrupted, SCIENCE runs
B = "ALMITA-OBSERVE-20261017-07:31:11"        # stopped by the operator, simulator mount, invalidated profile
C = "ALMITA-OBSERVE-20261015-09:50:15"        # planned only
D = "ALMITA-OBSERVE-20261014-12:06:42"        # corrupt plan, no mosaic.csv
E = "ALMITA-OBSERVE-20261018-21:30:00"        # 3 minutes after A, never REDUCEd: must not inherit A's runs
T = "TEST-STOP-20261013-17:47:25"             # operator's test run (by name)
PROFILE_A = "data/calibration/WIZARD-20261018-205845-078465/observe_profile/calibration_profile_v1.json"
PROFILE_B = "data/calibration/WIZARD-20261017-014447-688120/observe_profile/calibration_profile_v1.json"


def _w(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, (dict, list)):
        content = json.dumps(content, indent=1)
    path.write_bytes(content if isinstance(content, bytes) else str(content).encode())
    return path


def _plan(name: str, stamp: str, profile: str, gain: float = 40.2, rows: int = 1, cols: int = 3) -> dict:
    return {"schema_version": 1, "observation_name": name, "planning_timestamp_utc": stamp,
            "observer_config": {"observer": {"latitude": -33.4, "longitude": -70.6, "elevation_m": 570},
                                "site_info": {"location": "Santiago, Chile"}, "hardware": {"sdr_device": "RTL-SDR Blog V4"}},
            "requested": {"session": {"name": name}, "grid": {"rows": rows, "cols": cols, "spacing_deg": 5.0, "rotation_deg": 0},
                          "capture": {"seconds": 20, "settle_seconds": 2}, "main": {"center_frequency_hz": 1420405752, "sample_rate": 2400000,
                                                                                     "gain_db": gain, "bias_tee": True}},
            "resolved": {"rows": rows, "cols": cols, "point_count": rows * cols, "spacing_deg": 5.0, "rotation_deg": 0.0,
                         "center_ra_hours": 18.1, "center_dec_deg": -20.3, "frame": "icrs", "traversal": "SERPENTINE"},
            "main": {"center_frequency_hz": 1420405752, "sample_rate": 2400000, "gain_db": gain, "bias_tee": True},
            "rfi_ref": {"enabled": False}, "quicklook": {"enabled": True, "calibration_profile_path": profile},
            "visibility": "PASS", "observation_config_sha256": "ab" * 32}


def _mosaic(camp: Path, statuses, t0: str = "2026-10-18T21:28:", name: str = "ALMITA-OBSERVE") -> None:
    with (camp / "mosaic.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=MOSAIC_FIELDS)
        w.writeheader()
        for i, st in enumerate(statuses, start=1):
            w.writerow({"point_number": i, "point_id": i, "scan_order": i, "grid_row": 0, "grid_col": i - 1, "ra": 18.0, "dec": -20.0,
                        "capture_status": st, "start_time": f"{t0}{10 + i * 5:02d}+00:00" if st != "planned" else "",
                        "end_time": f"{t0}{12 + i * 5:02d}+00:00" if st == "success" else "", "error_message": "boom" if st == "failed" else "",
                        "data_filename": f"{name}_{i:04d}.dat", "altitude_deg_at_goto": 40.0, "azimuth_deg_at_goto": 120.0,
                        "visibility_deferred": "false", "error_code": "", "failed_at": ""})


def _campaign(root: Path, cid: str, stamp: str, statuses, profile: str, mount: str = "LX200 OnStep", iq: bool = True, gain: float = 40.2) -> Path:
    camp = root / "data" / "mosaic" / cid
    name = cid.rsplit("-", 2)[0]
    _w(camp / "observation_resolved.json", _plan(name, stamp, profile, gain=gain, cols=len(statuses)))
    _w(camp / "grid_metadata.json", {"session_name": name, "session_id": cid[len(name) + 1:], "grid": {"total_points": len(statuses)}})
    _w(camp / "grid_plan.png", b"\x89PNG\r\n\x1a\nplan")
    _mosaic(camp, statuses, t0=stamp[:17], name=name)
    if iq:
        sid = stamp[:10].replace("-", "") + "_" + stamp[11:19].replace(":", "")
        _w(camp / f"capture_timing_{sid}.csv", "point_id\n1\n")
        _w(camp / "orchestrator_capture.log", f"ALMITA\n  Mount              {mount}\n[2026-10-18 21:28:00.000] started\n")
        iqd = camp / "data" / "iq" / (cid + "x")
        _w(iqd / "session_identity.json", {"schema_version": 1, "session_id": sid, "session_root": str(iqd)})
        for i, st in enumerate(statuses, start=1):
            if st == "success":
                _w(iqd / f"{name}_{i:04d}.h5", b"IQ-NOT-HDF5-" * 100)       # never opened by the catalog
        ql = camp / "quicklook"
        _w(ql / "session_waterfall.png", b"\x89PNG\r\n\x1a\nwaterfall")
        _w(ql / "quicklook_map.png", b"\x89PNG\r\n\x1a\nmap")
        _w(ql / "session_waterfall_state.json", {"session_id": sid, "updated_utc": stamp, "frequency_mhz": [1420.0, 1420.4, 1420.8],
                                                 "rows": [{"point_id": str(i), "utc": stamp, "relative_db": [0.0, 0.1, 0.0]}
                                                          for i, st in enumerate(statuses, start=1) if st == "success"]})
        _w(ql / "points" / "1" / "spectrum" / "spectrum.json", {"x": 1})
    return camp


def _reduce(root: Path, group: str, run_id: str, campaign: str, status: str = "COMPLETED", manifest: bool = True,
            frame: str = "lsrk", profile: str = PROFILE_A, points=(1, 2, 3), commit: str = "cd35eed") -> Path:
    run = root / "data" / "reduced" / group / run_id
    _w(run / "config.json", {"velocity_frame": frame, "calibration_profile_path": profile, "fft_size": 8192})
    _w(run / "logs" / "events.jsonl", json.dumps({"event": "REDUCE_CAMPAIGN_BEGIN", "utc": "2026-10-18T21:58:34+00:00", "campaign_id": group}) + "\n" +
       (json.dumps({"event": "REDUCE_CAMPAIGN_END", "utc": "2026-10-18T22:07:39+00:00", "status": status}) + "\n" if manifest else ""))
    for p in points:
        _w(run / "points" / str(p) / "master_spectrum.json", {"campaign_id": group, "point_index": p, "capture_refs": [
            {"source_path": f"data/mosaic/{campaign}/data/iq/{campaign}x/{group}_{p:04d}.h5"}]})
    if manifest:
        _w(run / "manifest.json", {"campaign_id": group, "reduce_session_id": run_id, "status": status, "points_discovered": len(points),
                                   "points_completed": len(points), "quality_counts": {"GOOD": len(points)}, "velocity_frame_counts": {frame: len(points)},
                                   "source_campaign_root": f"data/mosaic/{campaign}"})
        _w(run / "provenance.json", {"git_commit": commit, "pipeline_version": "reduce-v1", "config_hash": run_id[-6:] * 4,
                                     "started_utc": "2026-10-18T21:58:34+00:00", "ended_utc": "2026-10-18T22:07:39+00:00"})
        _w(run / "qc" / "quality_summary.json", {"GOOD": len(points)})
    return run


def _science(root: Path, group: str, run_id: str, reduce_run: Path, sha: str = None) -> Path:
    run = root / "data" / "science" / group / run_id
    rel = str(reduce_run.relative_to(root))
    digest = sha or hashlib.sha256((reduce_run / "manifest.json").read_bytes()).hexdigest()
    _w(run / "provenance.json", {"input_reduce_session_dir": rel, "input_reduce_manifest_sha256": digest, "git_commit": "cd35eed",
                                 "config_hash": "c" * 64, "science_web_pipeline_version": "science-web-v1.3"})
    _w(run / "manifest.json", {"reduce_session_dir": rel, "input_reduce_manifest_sha256": digest, "data_completeness": "COMPLETE"})
    _w(run / "config.json", {"interp_factor_b": 3, "beam_fwhm_deg": 20.0})
    _w(run / "maps" / "map_abc_combined.png", b"\x89PNG\r\n\x1a\nabc")
    _w(run / "maps" / "secret_token.png", b"\x89PNG\r\n\x1a\nnever-exported")
    _w(run / "maps" / "NOTES.md", "notes")
    return run


def build(root: Path) -> dict:
    """The whole fake tree; returns the paths the tests use."""
    root = Path(root)
    a = _campaign(root, A, "2026-10-18T21:27:05.000Z", ["success", "success", "success"], PROFILE_A)
    b = _campaign(root, B, "2026-10-17T07:31:11.000Z", ["success", "success", "capturing", "planned"], PROFILE_B, mount="Telescope Simulator", gain=30.0)
    _w(b / "stop_report.json", {"schema_version": 1, "stop_requested_utc": "2026-10-17T07:33:00+00:00", "recorded_utc": "2026-10-17T07:33:10+00:00",
                                "phase_at_stop": "CAPTURE", "interrupted_point": 3, "interrupted_point_outcome": "no file",
                                "mount_stop": {"confirmed": True}})
    _campaign(root, C, "2026-10-15T09:50:15.000Z", ["planned", "planned", "planned"], PROFILE_A, iq=False)
    d = root / "data" / "mosaic" / D
    _w(d / "observation_resolved.json", "{not json")
    _campaign(root, E, "2026-10-18T21:30:00.000Z", ["success"], PROFILE_A)
    _campaign(root, T, "2026-10-13T17:47:25.000Z", ["success", "failed"], PROFILE_A)
    for prof, gain in ((PROFILE_A, 40.2), (PROFILE_B, 30.0)):
        _w(root / prof, {"created_utc": "2026-10-17T00:00:00+00:00", "center_frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain_db": gain})
    _w(root / PROFILE_B.replace(".json", ".INVALIDATED.json"), {"reason": "50 ohm taken with the antenna connected", "by": "operator",
                                                                "invalidated_utc": "2026-10-17T08:00:00+00:00"})
    r1 = _reduce(root, "ALMITA-OBSERVE", "REDUCE-20261018-215834-947591", A)
    r2 = _reduce(root, "ALMITA-OBSERVE", "REDUCE-20261018-230000-000001", A, frame="topocentric", commit="aaaaaaa")
    r3 = _reduce(root, "ALMITA-OBSERVE", "REDUCE-20261018-231500-000002", A, manifest=False, points=(1,))
    r4 = _reduce(root, "ALMITA-OBSERVE", "REDUCE-20261018-232000-000003", "GONE-20261001-00:00:00", points=())
    s1 = _science(root, "ALMITA-OBSERVE", "SCIENCE_WEB-20261018-220835-410607", r1)
    s2 = _science(root, "ALMITA-OBSERVE", "SCIENCE_WEB-20261018-221000-000001", r1)
    s3 = _science(root, "ALMITA-OBSERVE", "SCIENCE_WEB-20261018-231000-000002", r2, sha="0" * 64)
    bad = root / "data" / "science" / "ALMITA-OBSERVE" / "SCIENCE_WEB-20261018-232340-610800"
    _w(bad / "provenance.json", "")
    manual = root / "data" / "science" / "ALMITA-OBSERVE" / "HI_MANUAL-20261018_212705"
    _w(manual / "mapa.png", b"\x89PNG\r\n\x1a\nmanual")
    temps = root / "data" / "runtime" / "temperature" / "20261018.jsonl"
    lines = [json.dumps({"utc": f"2026-10-18T21:{m:02d}:00+00:00", "readings": {"sdr": {"c": 30.0 + m / 100}, "lna": {"c": None, "error": "crc"}}})
             for m in range(20, 40)]
    _w(temps, "\n".join(lines) + "\n")
    _w(root / "data" / "runtime" / "current_session.json", {"session_id": "LIVE-SESSION", "state": "RUNNING"})
    return {"a": a, "b": b, "r1": r1, "r2": r2, "r3": r3, "r4": r4, "s1": s1, "s2": s2, "s3": s3, "bad": bad, "manual": manual}


def snapshot(root: Path, skip: str = "data/sessions") -> dict:
    """path -> (size, mtime_ns, sha256) of every file under data/ except the SESSIONS store."""
    out = {}
    for p in sorted((Path(root) / "data").rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_file() and not rel.startswith(skip):
            st = p.stat()
            out[rel] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out
