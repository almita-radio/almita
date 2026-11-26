"""ALMITA DEMO: a recording (demo/record/record.mjs) -> the static site's /rec/ (manifest + bodies) next to sw.js.

    python demo/static/build_static.py <site dir> <recording dir>[:<cut at mark>] [<recording dir>[:<cut>] ...]

Several recordings of one container session (the recorder restarted between parts) are laid end to end on one
timeline; ":label" drops everything a part recorded from its mark "label" on (a step re-done in the next part).

Every recorded request becomes a manifest entry {t, method, key (normalised URL), post, status, ctype, body}. POSTs
whose URL is in ACTIONS are replay actions (they move the clock); "pausing" ones stop the clock at their time when an
earlier action runs into them. Bodies are content-addressed (sha256 prefix), deduplicated by the recorder."""
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ACTIONS = ("/api/observe/start", "/api/observe/stop", "/api/ops/start/", "/api/experiment/start", "/api/experiment/stop",
           "/api/survey/start", "/api/survey/stop")
DROP_QUERY = {"v", "t", "_", "ts", "nocache"}


def norm(u: str) -> str:
    s = urlsplit(u)
    q = sorted((k, v) for k, v in parse_qsl(s.query, keep_blank_values=True) if k not in DROP_QUERY)
    return s.path + ("?" + "&".join(f"{k}={v}" for k, v in q) if q else "")


GAP_MS = 2000


def load_parts(parts):
    rows, marks, offset, dirs = [], [], 0.0, {}
    for spec in parts:
        d, _, cut = spec.partition(":")
        d = Path(d)
        r = [json.loads(l) for l in (d / "index.jsonl").read_text().splitlines() if l.strip()]
        m = [json.loads(l) for l in (d / "marks.jsonl").read_text().splitlines() if l.strip()] if (d / "marks.jsonl").exists() else []
        end = next((x["t"] for x in m if x["label"] == cut), None) if cut else None
        if end is not None:
            r = [x for x in r if x["t"] < end]
            m = [x for x in m if x["t"] < end]
        for x in r:
            x["t"] += offset
            dirs[x["body"]] = d
        for x in m:
            x["t"] += offset
        rows += r
        marks += m
        offset = max([x["t"] for x in r] + [offset]) + GAP_MS
    return rows, marks, dirs


def main(site: str, *parts: str) -> None:
    site = Path(site)
    rows, marks, dirs = load_parts(parts)
    entries, actions = [], []
    for r in sorted(rows, key=lambda r: r["t"]):
        if r.get("status") is None:
            continue
        e = {"t": r["t"], "method": r["method"], "key": norm(r["url"]), "post": r.get("post"),
             "status": r["status"], "ctype": r.get("ctype") or r.get("mime"), "body": r["body"]}
        entries.append(e)
        if r["method"] == "POST" and any(e["key"].startswith(a) for a in ACTIONS) and r["status"] == 200:
            actions.append({"t": e["t"], "key": e["key"], "post": e["post"], "pausing": True})
    start = next((m["t"] for m in marks if m["label"] == "start"), entries[0]["t"] if entries else 0)
    clk = next((m for m in marks if m["label"] == "clock"), {})
    out = {"schema": "almita.demo_recording.v1", "SIMULATED": True, "start_t": start,
           "fake_epoch_ms": clk.get("fake_epoch_ms"), "clock_t": clk.get("t", 0),
           "end_t": entries[-1]["t"] if entries else 0, "actions": actions, "marks": marks, "entries": entries}
    (site / "rec" / "bodies").mkdir(parents=True, exist_ok=True)
    used = {e["body"] for e in entries}
    for h in used:
        shutil.copyfile(dirs[h] / "bodies" / h, site / "rec" / "bodies" / h)
    (site / "rec" / "manifest.json").write_text(json.dumps(out, separators=(",", ":")))
    print(f"{len(entries)} responses, {len(actions)} actions, {len(used)} bodies, "
          f"{sum((site / 'rec' / 'bodies' / h).stat().st_size for h in used) / 1e6:.1f} MB")


if __name__ == "__main__":
    main(*sys.argv[1:])
