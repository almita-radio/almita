import asyncio
import csv
import inspect
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

import alignment


def make_args(tmp_path, source=None, minimum=4):
    catalog=tmp_path/"catalog.csv"
    catalog.write_text("point_id,ra_hours,dec_deg,tb_kelvin\n1,0,-30,10\n2,6,-30,20\n")
    config=tmp_path/"observer.json"
    config.write_text(json.dumps({"observer":{"latitude_deg":-33.4,"longitude_deg":-70.6,"elevation_m":500}}))
    argv=["--reference","hi","--catalog",str(catalog),"--observer-config",str(config),
          "--output-dir",str(tmp_path/"result"),"--minimum-valid-positions",str(minimum),"--no-sync"]
    if source: argv += ["--replay-dir",str(source)]
    return alignment.parse_args(argv)


def make_replay(tmp_path, count=8):
    source=tmp_path/"source";source.mkdir()
    records=[];rng=np.random.default_rng(12);n=8192*8
    for index in range(1,count+1):
        i=np.clip(127.5+rng.normal(0,4,n),0,255).astype(np.uint8)
        q=np.clip(127.5+rng.normal(0,4,n),0,255).astype(np.uint8)
        raw=np.empty(2*n,np.uint8);raw[0::2]=i;raw[1::2]=q
        with h5py.File(source/f"alignment_sample_{index:03d}.h5","w") as h:
            h.create_dataset("iq_data",data=raw)
            h.attrs.update(file_state="complete",sample_rate_hz=2_400_000,
                           center_frequency_hz=round(alignment.HI_REST_HZ),
                           duration_seconds=2.0,requested_capture_duration_sec=2.0,
                           target_ra_hours=23.9+(index-1)*.01,
                           target_dec_deg=-30+(index-1)*.1)
        records.append({"index":index,"commanded_ra_hours":23.9+(index-1)*.01,
                        "commanded_dec_deg":-30+(index-1)*.1,
                        "mount_ra_hours":23.9+(index-1)*.01,
                        "mount_dec_deg":-30+(index-1)*.1,
                        "temperatures_pre":{},"temperatures_post":{}})
    payload={"reference":"hi","center_coordinates":{"ra_hours":23.95,"dec_deg":-29.7},
             "measured_positions":records,"expected_template_metric":{"selection":{}}}
    (source/"alignment_result.json").write_text(json.dumps(payload))
    return source


def test_hi_uses_imported_v2_and_two_pass_source():
    source=inspect.getsource(alignment.AlignmentRunner.analyze_hi_ensemble)
    assert "compute_hi_metric_v2" in source
    assert "compute_hi_metric(" not in source
    assert "np.median(spectra" in source
    assert "usable.append((record, raw))" not in source
    assert "raw, _ = self._read_capture" in source
    assert alignment.HI_VELOCITY_WINDOW_KM_S==200


def test_integration_default_is_provisional_20_seconds(tmp_path):
    args=make_args(tmp_path)
    assert args.integration_seconds==20.0
    assert "OFFLINE EXTRAPOLATED" in alignment.HI_INTEGRATION_VALIDATION_STATUS
    compatible=alignment.parse_args(["--capture-time","7"])
    assert compatible.integration_seconds==7


def test_replay_loads_metadata_without_hardware(tmp_path,monkeypatch):
    source=make_replay(tmp_path);runner=alignment.AlignmentRunner(make_args(tmp_path,source))
    monkeypatch.setattr(alignment,"INDITelescopeControl",lambda *a,**k:pytest.fail("mount constructed"))
    monkeypatch.setattr(alignment,"SDRCapture",lambda *a,**k:pytest.fail("SDR constructed"))
    code=asyncio.run(runner.run())
    assert code==0
    result=json.loads((runner.output_dir/"alignment_result.json").read_text())
    assert result["metric_version"]=="2"
    assert result["ensemble_reference_method"]=="ensemble_reference_scaled_linear"
    assert result["velocity_frame"]=="TOPOCENTRIC"
    assert result["velocity_window_kms"]==200
    assert result["template_type"]=="SYNTHETIC"
    assert result["template_ground_truth"] is False
    assert result["sync_eligible"] is False and result["sync_applied"] is False
    assert result["replay_mode"] is True
    assert result["result_status"]=="NO DEFENDIBLE DIFFERENTIAL HI STRUCTURE"
    assert result["status"]==result["result_status"]
    assert result["ensemble_status"]=="PASS"
    assert all(r["status"]=="VALID" for r in result["measured_positions"])
    assert all("metric_uncertainty" in r and "metric_snr" in r for r in result["measured_positions"])


def test_replay_recovers_records_when_source_result_is_missing(tmp_path, monkeypatch):
    source = make_replay(tmp_path, count=4)
    (source / "alignment_result.json").unlink()
    runner = alignment.AlignmentRunner(make_args(tmp_path, source, minimum=4))
    monkeypatch.setattr(alignment, "INDITelescopeControl",
                        lambda *a, **k: pytest.fail("mount constructed"))
    monkeypatch.setattr(alignment, "SDRCapture",
                        lambda *a, **k: pytest.fail("SDR constructed"))
    code = asyncio.run(runner.run())
    result = json.loads((runner.output_dir / "alignment_result.json").read_text())
    assert code == 0
    assert len(result["measured_positions"]) == 4
    assert result["expected_template_metric"]["selection"]["recovered_without_source_result"] is True
    assert result["sync_applied"] is False


def test_replay_recovers_coordinates_from_execution_log(tmp_path):
    source = make_replay(tmp_path, count=4)
    (source / "alignment_result.json").unlink()
    for path in source.glob("*.h5"):
        with h5py.File(path, "r+") as handle:
            del handle.attrs["target_ra_hours"]
            del handle.attrs["target_dec_deg"]
    center = alignment.load_hi_catalog(make_args(tmp_path).catalog)[0][0]
    positions = alignment.multiscale_pattern(center)[:4]
    lines = []
    for position in positions:
        lines.extend((f"    RA={position.ra.hour}", f"    DEC={position.dec.deg}",
                      "Capturing synthetic samples"))
    (source / "alignment_execution.log").write_text("\n".join(lines))
    runner = alignment.AlignmentRunner(make_args(tmp_path, source, minimum=4))
    reference, recovered_center, selection, records = runner.replay_records()
    assert reference == "hi" and len(records) == 4
    assert recovered_center.separation(center).deg < 1e-9
    assert selection["recovery_coordinate_method"].startswith("execution_log")
    assert records[0]["mount_ra_hours"] == pytest.approx(positions[0].ra.hour)


def test_sun_replay_uses_existing_captures_without_hardware(tmp_path, monkeypatch):
    source = make_replay(tmp_path, count=4)
    (source / "alignment_result.json").unlink()
    lines = []
    for path in sorted(source.glob("*.h5")):
        with h5py.File(path, "r+") as handle:
            handle.attrs["alignment_reference"] = "sun"
            handle.attrs["created_at"] = "2026-08-26T16:37:52+00:00"
            del handle.attrs["target_ra_hours"]
            del handle.attrs["target_dec_deg"]
        lines.extend(("    RA=16.88", "    DEC=-21.48", "Capturing synthetic samples"))
    (source / "alignment_execution.log").write_text("\n".join(lines))
    runner = alignment.AlignmentRunner(make_args(tmp_path, source, minimum=4))
    monkeypatch.setattr(alignment, "INDITelescopeControl",
                        lambda *a, **k: pytest.fail("mount constructed"))
    monkeypatch.setattr(alignment, "SDRCapture",
                        lambda *a, **k: pytest.fail("SDR constructed"))
    asyncio.run(runner.run())
    result = json.loads((runner.output_dir / "alignment_result.json").read_text())
    assert result["reference"] == "sun"
    assert len(result["measured_positions"]) == 4
    assert result["sync_applied"] is False


def test_partial_invalid_hdf5_continues_with_sufficient_positions(tmp_path):
    source=make_replay(tmp_path,count=6)
    (source/"alignment_sample_003.h5").unlink()
    runner=alignment.AlignmentRunner(make_args(tmp_path,source,minimum=4))
    _,_,_,records=runner.replay_records();_,info=runner.analyze_hi_ensemble(records)
    assert records[2]["status"]=="INVALID_HDF5"
    assert info["ensemble_positions_count"]==5
    assert info["status"]=="PASS"


def test_insufficient_valid_positions_prevents_offset(tmp_path):
    source=make_replay(tmp_path,count=5)
    (source/"alignment_sample_001.h5").unlink();(source/"alignment_sample_002.h5").unlink()
    runner=alignment.AlignmentRunner(make_args(tmp_path,source,minimum=4))
    code=asyncio.run(runner.run());result=json.loads((runner.output_dir/"alignment_result.json").read_text())
    assert code==2 and result["result_status"]=="INSUFFICIENT VALID POSITIONS"
    assert result["offsets"] is None and result["sync_eligible"] is False


def test_replay_comparison_reports_exact_match(tmp_path):
    source=make_replay(tmp_path,count=4);runner=alignment.AlignmentRunner(make_args(tmp_path,source,minimum=4))
    _,_,_,records=runner.replay_records();runner.analyze_hi_ensemble(records)
    analysis=source/"analysis_v2";analysis.mkdir()
    with (analysis/"hi_metric_v2_49.csv").open("w",newline="") as h:
        fields=["point","metric_v2","uncertainty","snr","valid_fraction"]
        w=csv.DictWriter(h,fieldnames=fields);w.writeheader()
        for r in records:w.writerow({"point":r["index"],"metric_v2":r["metric_value"],"uncertainty":r["metric_uncertainty"],"snr":r["metric_snr"],"valid_fraction":r["valid_fraction"]})
    comparison=runner.replay_comparison(records)
    assert comparison["status"]=="PASS" and comparison["mismatches"]==0
    assert comparison["max_abs_difference"]==0


def test_replay_forces_sync_off_and_preserves_ra_wrap(tmp_path):
    source=make_replay(tmp_path,count=4)
    args=alignment.parse_args(["--replay-dir",str(source),"--apply-sync"])
    assert args.apply_sync is False
    runner=alignment.AlignmentRunner(make_args(tmp_path,source,minimum=4));_,center,_,records=runner.replay_records()
    assert 0<=center.ra.hour<24 and all(0<=r["commanded_ra_hours"]<24 for r in records)
