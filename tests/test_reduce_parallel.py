"""reduce_parallel: REDUCE's points on several cores, with the frozen reduce_engine untouched - the parallel RUN must
give byte-identical spectra to the sequential one, with and without ESTIMATOR v2, and complete reports."""
import csv
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

import reduce_campaign_run
import reduce_parallel as rp

RATE, F0 = 2_400_000, 1_420_405_752


def _campaign(root: Path, n_points: int = 4, seconds: float = 2.0) -> Path:
    camp = root / "CAMP-PAR"
    iq_dir = camp / "data" / "iq" / "20261110_120000"
    iq_dir.mkdir(parents=True)
    (camp / "grid_metadata.json").write_text(json.dumps({"session_name": "PAR", "grid": {}}))
    (camp / "observer_config.json").write_text("{}")
    rng = np.random.default_rng(11)
    rows = []
    for i in range(1, n_points + 1):
        n = int(seconds * RATE)
        raw = np.clip(np.round(rng.normal(127.5, 10.0, 2 * n)), 0, 255).astype(np.uint8)
        with h5py.File(iq_dir / f"p{i}.h5", "w") as f:
            f.create_dataset("iq_data", data=raw)
            f.attrs.update({"sample_rate_hz": RATE, "center_frequency_hz": F0, "capture_status": "success",
                            "capture_start_utc": f"2026-11-10T12:0{i}:00+00:00", "duration_seconds": seconds})
        rows.append({"point_number": str(i), "capture_status": "success", "data_filename": f"p{i}.dat",
                     "target_ra_hours": str(18.0 + 0.1 * i), "target_dec_degrees": "-20.0"})
    with (camp / "mosaic.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return camp


def _run(camp: Path, out: Path, capsys, *extra) -> dict:
    rc = reduce_campaign_run.main(["run", str(camp), "--output-root", str(out), "--velocity-frame", "topocentric", "--json", *extra])
    assert rc == 0
    text = capsys.readouterr().out
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def _spectra(output_dir: str) -> dict:
    out = {}
    for h5 in sorted(Path(output_dir).glob("points/*/master_spectrum.h5")):
        with h5py.File(h5) as f:
            out[h5.parent.name] = {k: f[k][:] for k in f.keys()}
    return out


def _same(a: dict, b: dict) -> bool:
    return a.keys() == b.keys() and all(
        a[p].keys() == b[p].keys() and all(np.array_equal(a[p][k], b[p][k], equal_nan=True) for k in a[p]) for p in a)


def test_auto_workers_keeps_a_core_free_and_fits_the_ram():
    gb = 1e9
    w, why = rp.auto_workers([73e6] * 25, False, cpus=8, available=17.3 * gb, sdr_busy=None)
    assert w == 4 and "classic" in why                       # 0.7*17.3 GB / (0.2 + 37*0.073) GB = 4.2
    assert rp.auto_workers([73e6] * 25, True, cpus=8, available=17.3 * gb, sdr_busy=None)[0] == 7      # v2: limited by cores - 1
    assert rp.auto_workers([73e6] * 3, True, cpus=8, available=17.3 * gb, sdr_busy=None)[0] == 3       # never more than points
    assert rp.auto_workers([73e6] * 25, False, cpus=8, available=1 * gb, sdr_busy=None)[0] == 1        # tight RAM -> sequential
    assert rp.auto_workers([73e6], True, cpus=8, available=17.3 * gb, sdr_busy=None)[0] == 1
    w, why = rp.auto_workers([73e6] * 25, True, cpus=8, available=17.3 * gb, sdr_busy="capture.py")
    assert w == 3 and "capture.py is running" in why          # a capture on this machine keeps half the cores


def test_use_prefetched_answers_from_the_cache_falls_back_and_restores():
    from types import SimpleNamespace
    from reduce_engine import pipeline
    before = pipeline.reduce_point
    with rp.use_prefetched({1: "OUT1"}) as used:
        assert pipeline.reduce_point(SimpleNamespace(point_index=1)) == "OUT1"
        assert pipeline.reduce_point is not before
    assert pipeline.reduce_point is before and used == {"prefetched": 1, "computed_here": 0}


@pytest.mark.parametrize("extra", [(), ("--estimator-v2",)], ids=["classic", "estimator_v2"])
def test_parallel_run_is_byte_identical_to_the_sequential_one(tmp_path, capsys, extra):
    camp = _campaign(tmp_path)
    seq = _run(camp, tmp_path / "seq", capsys, "--workers", "1", *extra)
    par = _run(camp, tmp_path / "par", capsys, "--workers", "2", *extra)
    assert seq["parallel"]["workers"] == 1 and "used" not in seq["parallel"]
    assert par["parallel"]["workers"] == 2 and par["parallel"]["used"] == {"prefetched": 4, "computed_here": 0}
    assert par["parallel"]["worker_failures"] == []
    assert seq["status"] == par["status"] == "COMPLETED" and par["points_completed"] == 4
    assert _same(_spectra(seq["output_dir"]), _spectra(par["output_dir"]))
    if extra:                                                  # the v2 report gathered every worker's records
        assert seq["estimator_v2"]["estimates_v2"] == par["estimator_v2"]["estimates_v2"] == 4
        report = json.loads(Path(par["estimator_v2"]["report_path"]).read_text())
        assert len(report["per_point"]) == 4


def test_a_point_a_worker_cannot_hand_back_is_reduced_here(tmp_path, capsys, monkeypatch):
    camp = _campaign(tmp_path, n_points=3)
    real = rp.prefetch

    def lossy(*a, **k):
        outcomes, records, facts = real(*a, **k)
        outcomes.pop(2)                                         # as if that worker had died
        facts["worker_failures"] = ["point 2: simulated"]
        return outcomes, records, facts
    monkeypatch.setattr(rp, "prefetch", lossy)
    par = _run(camp, tmp_path / "par", capsys, "--workers", "2")
    seq = _run(camp, tmp_path / "seq", capsys, "--workers", "1")
    assert par["parallel"]["used"] == {"prefetched": 2, "computed_here": 1} and par["points_completed"] == 3
    assert _same(_spectra(seq["output_dir"]), _spectra(par["output_dir"]))
