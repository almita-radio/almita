#!/bin/bash
# The Hugging Face static Space (~/almita-demo-space): landing + service worker + recording + the sky simulator.
#   demo/static/assemble_space.sh <built site dir with rec/>
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SITE=$1
SPACE=${2:-$HOME/almita-demo-space}
cd "$SPACE"
git rm -q -r --cached . >/dev/null 2>&1 || true
find . -mindepth 1 -maxdepth 1 ! -name .git ! -name .gitattributes -exec rm -rf {} +
cp "$HERE/index.html" "$HERE/sw.js" "$HERE/about-demo.html" "$HERE/home.html" "$HERE/console-tour.html" "$HERE/demo.html" .
cp -r "$HERE/project" project
sed 's#<script src="app.js"></script>#<script src="sim.js"></script>#' "$HERE/../site/index.html" > simulator.html
cp "$HERE/../site/app.js" sim.js
cp "$HERE/../site/sky.json" sky.json
cp -r "$SITE/rec" rec
cat > README.md <<'MD'
---
title: ALMITA demo
emoji: 📡
colorFrom: indigo
colorTo: gray
sdk: static
app_file: home.html
pinned: false
short_description: ALMITA's real console replaying a SIMULATED telescope
---

ALMITA demo - SIMULATED. The real web console of ALMITA (a home-built 21 cm hydrogen-line radio telescope) replaying a
recording of its own software driving a simulated telescope and SDR. The sky follows the HI4PI survey (HI4PI
Collaboration 2016, A&A 594, A116); receiver and noise are models. Nothing here is a real observation.
Source: https://github.com/almita-radio/almita
MD
git add -A
du -sh --exclude=.git .
