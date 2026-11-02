#!/usr/bin/env bash
# ALMITA capture Pi (Pi 4 at the tripod): installs instrument_agent.py and the INDI server as services there and
# enables rtl_tcp.service (already installed on the Pi), so a power-on brings everything up. Run it FROM THE NOTEBOOK:
#
#   scripts/install_capture_pi.sh [user@pi] [bind-address]
#       default: stellarmate@192.168.50.254, bind 192.168.50.254 (the agent answers on the direct cable only)
#
# It asks for the Pi user's password (ssh) and for sudo there. Repeatable. almita-indiserver runs the Telescope
# Simulator and connects it (KStars only autostarts paused and does NOT start INDI); add indi_lx200_OnStep to its
# ExecStart when the OnStep is connected.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-stellarmate@192.168.50.254}"
BIND="${2:-192.168.50.254}"
PI_USER="${TARGET%@*}"
AGENT_DIR="/home/$PI_USER/almita-agent"
UNIT=almita-instrument-agent.service
INDI_UNIT=almita-indiserver.service
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

sed -e "s|@USER@|$PI_USER|g" -e "s|@AGENT_DIR@|$AGENT_DIR|g" -e "s|@BIND@|$BIND|g" \
    "$ROOT/systemd/capture-pi/$UNIT" > "$TMP/$UNIT"
sed -e "s|@USER@|$PI_USER|g" "$ROOT/systemd/capture-pi/$INDI_UNIT" > "$TMP/$INDI_UNIT"
cp "$ROOT/instrument_agent.py" "$TMP/"

echo "== copying the agent to $TARGET:$AGENT_DIR"
ssh -o ControlMaster=auto -o ControlPath="$TMP/cm" -o ControlPersist=60 "$TARGET" "mkdir -p '$AGENT_DIR'"
scp -o ControlPath="$TMP/cm" "$TMP/instrument_agent.py" "$TMP/$UNIT" "$TMP/$INDI_UNIT" "$TARGET:$AGENT_DIR/"
echo "== installing $UNIT + $INDI_UNIT, enabling rtl_tcp.service (sudo on the Pi)"
ssh -t -o ControlPath="$TMP/cm" "$TARGET" "for u in $UNIT $INDI_UNIT; do sudo install -m 644 '$AGENT_DIR/'\$u /etc/systemd/system/\$u && rm '$AGENT_DIR/'\$u; done \
  && sudo systemctl daemon-reload && sudo systemctl enable rtl_tcp.service $UNIT $INDI_UNIT \
  && sudo systemctl restart $UNIT && (pgrep -x indiserver >/dev/null && echo 'an INDI server already runs: $INDI_UNIT not restarted' || sudo systemctl restart $INDI_UNIT) \
  && sleep 3 && systemctl is-active rtl_tcp.service $UNIT $INDI_UNIT"
ssh -O exit -o ControlPath="$TMP/cm" "$TARGET" 2>/dev/null || true

echo "== agent health from here"
curl -s --max-time 5 "http://$BIND:8099/health" && echo || echo "agent NOT answering on $BIND:8099"
