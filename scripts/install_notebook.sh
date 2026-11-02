#!/usr/bin/env bash
# ALMITA on the notebook: web + capture + processing here, rtl_tcp / INDI / sensors on the capture Pi (Pi 4).
#
#   scripts/install_notebook.sh [capture-pi-host]        (default 192.168.50.254, the direct cable)
#
# Run it as your own user from the repo. No sudo: the units are systemd USER units with linger on, so they start at
# boot without a login (installed this way on 2026-11-02).
# What it does, in order - every step can be repeated safely:
#   1. ~/.config/almita/instrument.json -> {"host": "<capture-pi-host>"}   (the web's STATUS > CAPTURE PI edits it later)
#   2. web credentials ~/.config/almita/web_auth.json, only if missing (asks the password; stored as a hash)
#   3. installs systemd/notebook/*.service as user units (~/.config/systemd/user) for this checkout, enables and
#      (re)starts them, and turns linger on:
#      almita-instrument-forwarder  127.0.0.1:1234 + :7624 -> capture Pi
#      almita-observe-api           the web on :8088
#      almita-console-watcher       field console status
#      almita-system-blackbox       host heartbeat log
#      almita-rfi-mask-live         live RFI masks for the monitor (RFI MASK panel, UNKNOWN PLEASURES)
#   4. checks: forwarder up, rtl_tcp handshake through it, INDI port, the Pi's agent, the web answering.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${1:-192.168.50.254}"
ME="$(id -un)"
PY="$ROOT/.venv/bin/python"
UNITS=(almita-instrument-forwarder almita-observe-api almita-console-watcher almita-system-blackbox almita-rfi-mask-live)

cd "$ROOT"
[[ -x "$PY" ]] || { echo "no $PY - create the venv first (python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)"; exit 1; }
[[ "$ME" != root ]] || { echo "run it as your own user, not with sudo"; exit 1; }

echo "== 1. capture Pi = $HOST"
"$PY" - "$HOST" <<'EOF'
import sys
sys.path.insert(0, ".")
import instrument_host
print(instrument_host.set_host(sys.argv[1]))
EOF

echo "== 2. web credentials"
if "$PY" -c "import almita_web_auth as a; import sys; sys.exit(0 if a.Authenticator().configured() else 1)" 2>/dev/null; then
    echo "   already configured ($HOME/.config/almita/web_auth.json) - left as it is"
else
    read -r -p "   web user name: " WEBUSER
    "$PY" "$ROOT/almita_web_auth.py" set-password --user "$WEBUSER"
fi

echo "== 3. systemd user units"
mkdir -p "$HOME/.config/systemd/user"
for u in "${UNITS[@]}"; do
    sed -e "/^User=@USER@$/d" -e "s|@ROOT@|$ROOT|g" -e "s|^WantedBy=multi-user.target|WantedBy=default.target|" \
        -e "s|^After=network-online.target|After=network.target|" -e "/^Wants=network-online.target/d" \
        "$ROOT/systemd/notebook/$u.service" > "$HOME/.config/systemd/user/$u.service"
done
systemctl --user daemon-reload
systemctl --user enable "${UNITS[@]/%/.service}" >/dev/null
systemctl --user restart "${UNITS[@]/%/.service}"
loginctl enable-linger "$ME" || echo "   linger NOT enabled: the services stop when you log out (sudo loginctl enable-linger $ME)"
sleep 3

echo "== 4. checks"
for u in "${UNITS[@]}"; do printf "   %-30s %s\n" "$u" "$(systemctl --user is-active "$u")"; done
"$PY" - <<'EOF'
import json, socket, sys, urllib.request
sys.path.insert(0, ".")
import instrument_host as ih
def tcp(host, port):
    try:
        with socket.create_connection((host, port), timeout=3) as s:
            return s
    except OSError as exc:
        return exc
print("   agent on the capture Pi:", json.dumps(ih.agent_health()))
s = None
try:
    s = socket.create_connection(("127.0.0.1", 1234), timeout=5)
    s.settimeout(5)
    head = s.recv(12)
    print("   rtl_tcp through the forwarder:", "OK (RTL0 handshake)" if head[:4] == b"RTL0" else f"unexpected {head!r}")
except OSError as exc:
    print("   rtl_tcp through the forwarder: FAILED", exc)
finally:
    if s: s.close()
r = tcp("127.0.0.1", 7624)
print("   INDI through the forwarder:", "OK" if not isinstance(r, Exception) else f"FAILED {r}")
import urllib.error
try:
    print("   web :8088:", urllib.request.urlopen("http://127.0.0.1:8088/healthz", timeout=5).status)
except urllib.error.HTTPError as exc:
    print("   web :8088: answers", exc.code, "(login required)" if exc.code == 401 else "")
except Exception as exc:
    print("   web :8088: FAILED", exc)
EOF
echo "Open http://localhost:8088/status.html  (CAPTURE PI panel at the top)"
