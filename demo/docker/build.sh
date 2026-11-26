#!/bin/bash
# Snapshot of the working tree for the demo image: tracked code (no docs/tests) + the demo folder + the HI4PI
# column-density map. data/ starts EMPTY; observer_config.json is replaced by a city-level copy. Never the real data.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${1:-$HOME/almita-demo-build}
rm -rf "$OUT/app"; mkdir -p "$OUT/app"
cd "$REPO"
{ git ls-files; git ls-files --others --exclude-standard demo hi4pi_compare.py linefree_noise.py; } \
  | grep -v -E '^(docs|tests)/|^data/' | sort -u | while read -r f; do
    [ -f "$f" ] && mkdir -p "$OUT/app/$(dirname "$f")" && cp -p "$f" "$OUT/app/$f"
  done
cp -p demo/site/sky.json "$OUT/app/demo/site/sky.json"
mkdir -p "$OUT/app/data/reference/hi4pi" "$OUT/app/data/runtime"
cp -p data/reference/hi4pi/CAR_-600_600.fits "$OUT/app/data/reference/hi4pi/"
python3 - "$OUT/app/observer_config.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
o = d.get("observer", {})
o.update({"latitude_deg": -33.45, "longitude_deg": -70.67, "elevation_m": 570.0, "name": "ALMITA DEMO (SIMULATED)"})
d["observer"] = o
json.dump(d, open(p, "w"), indent=2)
PY
cp -p "$REPO/demo/docker/Dockerfile" "$REPO/demo/docker/entrypoint.sh" "$REPO/demo/docker/requirements-demo.txt" "$REPO/demo/docker/systemctl-shim.py" "$OUT/"
du -sh "$OUT/app"
