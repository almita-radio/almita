"""rfi_mask_live: follows only sessions announced after it starts, writes side-cars and the monitor's runtime files."""
import csv
import json
from datetime import datetime, timedelta, timezone

import numpy as np

import rfi_mask_live as live
from tests.test_rfi_mask import CENTER, RATE, _noise, _tones, _write
import rfi_mask as rm


def _session(tmp_path, started_utc, n=2):
    root = tmp_path / "mosaic" / "G" / "data" / "iq" / "S1"
    root.mkdir(parents=True)
    rows = []
    for k in range(n):
        _write(root / f"ALMITA-OBSERVE_{k + 1:04d}.h5", _tones(_noise(0.5, seed=k), rm.comb_teeth_hz(CENTER, RATE), 3.0))
        rows.append({"point_id": str(k + 1), "status": "SUCCESS", "source_hdf5": f"ALMITA-OBSERVE_{k + 1:04d}.h5"})
    with (root / "session.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["point_id", "status", "source_hdf5"])
        w.writeheader()
        w.writerows(rows)
    rt = tmp_path / "runtime"
    rt.mkdir()
    (rt / "current_session.json").write_text(json.dumps({"session_id": "S1", "session_root": str(root),
                                                          "started_utc": started_utc}))
    return root, rt


def test_an_older_session_is_never_processed(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    root, rt = _session(tmp_path, old)
    assert live.MaskLive(rt).scan_once() == 0
    assert not (rt / live.LATEST).exists() and not live.sidecar_dir(root).exists()


def test_a_new_session_is_masked_next_to_its_data(tmp_path):
    m = live.MaskLive(tmp_path / "runtime_unused")
    root, rt = _session(tmp_path, (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
    m.runtime_dir = rt
    assert m.scan_once() == 2 and m.scan_once() == 0                 # each capture once
    side = live.sidecar_dir(root)
    assert side == tmp_path / "mosaic" / "G" / "data" / "rfi_mask" / "S1"
    assert len(list(side.glob("*.rfimask.npz"))) == 2
    latest = json.loads((rt / live.LATEST).read_text())
    assert latest["session_id"] == "S1" and latest["comb_channels"] > 0
    assert len(latest["spectrum"]["power_db"]) == len(latest["spectrum"]["code"])
    sess = json.loads((rt / live.SESSION).read_text())
    assert [r["point_id"] for r in sess["rows"]] == ["1", "2"]


def test_a_line_is_known_only_after_two_strong_captures(tmp_path):
    m = live.MaskLive(tmp_path)
    strong = [{"frequency_hz": 1_420_100_000.0, "excess_db": 5.0, "known": False}]
    m._count_hits(strong, 1.0)
    assert m.known_lines == []
    m._count_hits([{**strong[0], "frequency_hz": 1_420_100_300.0}], 1.0)          # same line, next capture
    assert m.known_lines == [1_420_100_000.0]
    m._count_hits([{"frequency_hz": 1_420_500_000.0, "excess_db": 0.8, "known": False}], 1.0)   # weak: never counted
    assert len(m.line_hits) == 1
