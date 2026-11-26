#!/bin/bash
# Fresh demo container: empty data/ (HI4PI reference kept, simulator mount kept), the simulated clock back at 22:00.
set -euo pipefail
D=${1:-$HOME/almita-demo-build/data_run}
sg docker -c "docker rm -f almita-demo-live" >/dev/null 2>&1 || true
sg docker -c "docker run --rm --entrypoint sh -v $D:/d almita-demo-live -c 'cp /d/runtime/mount_device.json /tmp/ 2>/dev/null; find /d -mindepth 1 -maxdepth 1 ! -name reference -exec rm -rf {} +; mkdir -p /d/runtime; cp /tmp/mount_device.json /d/runtime/ 2>/dev/null; chmod -R a+rwX /d'"
sg docker -c "docker run -d --name almita-demo-live -p 127.0.0.1:18088:8088 -v $D:/app/data almita-demo-live" >/dev/null
for i in $(seq 1 40); do
  if curl -s -u demo:almita-demo http://127.0.0.1:18088/api/system/health 2>/dev/null | grep -q '"AVAILABLE"'; then
    # the sessions catalog exists from the start, so STATUS's sky map shows the empty sky instead of an error
    curl -s -u demo:almita-demo -H Content-Type:application/json -X POST --data '{}' http://127.0.0.1:18088/api/sessions/reindex >/dev/null
    echo "demo container ready"; exit 0
  fi
  sleep 2
done
echo "demo container NOT ready"; exit 1
