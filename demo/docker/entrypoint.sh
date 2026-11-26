#!/bin/sh
# ALMITA DEMO entrypoint: INDI Telescope Simulator, the SIMULATED instrument, then the real ALMITA web on 8088.
set -e
cd /app
# a simulated winter night (the Galactic Centre high at 22:00 Chile), the clock running from there: every process
# below sees it through libfaketime
export LD_PRELOAD=$(find /usr/lib -name libfaketime.so.1 | head -n 1) FAKETIME="$DEMO_CLOCK" FAKETIME_DONT_RESET=1
date
indiserver -v indi_simulator_telescope > /app/data/runtime/indiserver.log 2>&1 &
sleep 2
python3 demo/fake_instrument.py > /app/data/runtime/fake_instrument.log 2>&1 &
sleep 2
systemctl --user start almita-console-watcher
systemctl --user start almita-temperature-logger
python3 - <<'PY'
import almita_web_auth, os
almita_web_auth.write_credentials(almita_web_auth.auth_file_path(), "demo", os.environ.get("ALMITA_DEMO_PASSWORD", "almita-demo"))
PY
exec python3 almita_orchestrator_server.py --host 0.0.0.0 --port 8088
