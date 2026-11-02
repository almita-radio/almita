#!/usr/bin/env bash
# ALMITA capture Pi (Pi 4 at the tripod): installs instrument_agent.py as a service there. Run it FROM THE NOTEBOOK:
#
#   scripts/install_capture_pi.sh [user@pi] [bind-address]
#       default: stellarmate@192.168.50.254, bind 192.168.50.254 (the agent answers on the direct cable only)
#
# It asks for the Pi user's password (ssh) and for sudo there. Repeatable. It touches nothing else on the Pi:
# rtl_tcp.service and the INDI server (KStars/Ekos) stay as they are.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-stellarmate@192.168.50.254}"
BIND="${2:-192.168.50.254}"
PI_USER="${TARGET%@*}"
AGENT_DIR="/home/$PI_USER/almita-agent"
UNIT=almita-instrument-agent.service
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

sed -e "s|@USER@|$PI_USER|g" -e "s|@AGENT_DIR@|$AGENT_DIR|g" -e "s|@BIND@|$BIND|g" \
    "$ROOT/systemd/capture-pi/$UNIT" > "$TMP/$UNIT"
cp "$ROOT/instrument_agent.py" "$TMP/"

echo "== copying the agent to $TARGET:$AGENT_DIR"
ssh -o ControlMaster=auto -o ControlPath="$TMP/cm" -o ControlPersist=60 "$TARGET" "mkdir -p '$AGENT_DIR'"
scp -o ControlPath="$TMP/cm" "$TMP/instrument_agent.py" "$TMP/$UNIT" "$TARGET:$AGENT_DIR/"
echo "== installing $UNIT (sudo on the Pi)"
ssh -t -o ControlPath="$TMP/cm" "$TARGET" "sudo install -m 644 '$AGENT_DIR/$UNIT' /etc/systemd/system/$UNIT && rm '$AGENT_DIR/$UNIT' \
  && sudo systemctl daemon-reload && sudo systemctl enable $UNIT && sudo systemctl restart $UNIT && sleep 2 \
  && systemctl is-active $UNIT"
ssh -O exit -o ControlPath="$TMP/cm" "$TARGET" 2>/dev/null || true

echo "== agent health from here"
curl -s --max-time 5 "http://$BIND:8099/health" && echo || echo "agent NOT answering on $BIND:8099"
