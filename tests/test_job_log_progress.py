"""REDUCE and SCIENCE runs print live progress lines on stderr for the web job log, and those lines never
break the web's parse of the final --json result (first '{' .. last '}' of the combined log)."""
import io
import json
import re

import numpy as np

import reduce_campaign_run as rcr


def _write_events(path, events):
    with path.open("a") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def test_reduce_follower_reports_each_processed_point_from_the_real_events_file(tmp_path):
    parent = tmp_path / "CAMP"
    (parent / "OLD-SESSION" / "logs").mkdir(parents=True)
    _write_events(parent / "OLD-SESSION" / "logs" / "events.jsonl",
                  [{"event": "POINT_PROCESSED", "point_index": 99, "status": "COMPLETED"}])
    out = io.StringIO()
    f = rcr._ProgressFollower(parent, total=3, stream=out, poll_s=0.01)
    logs = parent / "NEW-SESSION" / "logs"
    logs.mkdir(parents=True)
    ev = logs / "events.jsonl"
    _write_events(ev, [{"event": "REDUCE_CAMPAIGN_BEGIN", "points_discovered": 3},
                       {"event": "POINT_PROCESSED", "point_index": 1, "status": "COMPLETED", "reason": None,
                        "timing": {"ingest": 0.5, "spectral_estimate": 8.0}}])
    f._drain()
    with ev.open("a") as fh:                       # a half-written line must wait for its newline
        fh.write('{"event": "POINT_PROCESSED", "point_index": 2, "status": "FAI')
    f._drain()
    with ev.open("a") as fh:
        fh.write('LED", "reason": "bad {header}", "timing": {}}\n')
    f.finish()
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("[REDUCE] point 1 - 1/3 - COMPLETED (8.5 s)")
    assert "point 2 - 2/3 - FAILED" in lines[1] and "bad (header)" in lines[1]
    assert "point 99" not in out.getvalue()        # an older session's events are never reported
    assert lines[-1].startswith("[REDUCE] done: 2/3 points processed (COMPLETED 1, FAILED 1)")
    assert "{" not in out.getvalue() and "}" not in out.getvalue()


def test_reduce_follower_without_a_session_reports_nothing_processed(tmp_path):
    out = io.StringIO()
    f = rcr._ProgressFollower(tmp_path / "missing", total=5, stream=out, poll_s=0.01)
    f.start()
    f.finish()
    assert out.getvalue().strip() == "[REDUCE] done: 0/5 points processed (no points) in 00:00:00 - writing the result"


def test_science_progress_goes_to_stderr_without_braces(capsys):
    import science_web_bridge as swb
    from science_engine.simulation import SyntheticPointSpec, build_synthetic_science_input
    from tests.test_science_web_bridge import cfg_with
    specs = [SyntheticPointSpec(point_index=1 + r * 4 + c, ra_hours=(321.6 + (c - 1.5) / np.cos(np.radians(-33.4))) / 15.0,
                                dec_degrees=-33.4 + (r - 1.5), calibration_level="UNCALIBRATED")
             for r in range(4) for c in range(4)]
    swb.build_all_products(build_synthetic_science_input(specs, noise_sigma=0.02), cfg_with(beam_fwhm_deg=20.0))
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    for stage in ("building the board", "map B: ", "map B: done", "map C: done", "per-point integrated values",
                  "leave-one-out check", "B/C same-coordinate consistency check"):
        assert stage in err, stage
    assert all(re.match(r"\[SCIENCE \d\d:\d\d:\d\d\] ", line) for line in err.splitlines())
    assert "{" not in err and "}" not in err
