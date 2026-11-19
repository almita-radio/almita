#!/usr/bin/env bash
# Notebook -> Pi 4 internet over the direct UTP link (operator 2026-11-19: the Pi 4 gets internet and NTP time through
# the cable, never through its own WiFi). Persistent across reboots. Run once:  sudo bash scripts/notebook_share_internet_to_pi4.sh
#
#  - IPv4 forwarding on (/etc/sysctl.d/90-almita-forward.conf)
#  - NAT (masquerade) for the cable subnet out of WHATEVER uplink the notebook has (home WiFi, phone tethering...),
#    as its own nftables table loaded by a oneshot unit (does not touch ufw's own rules)
#  - ufw: allow routed traffic coming in from the cable
# The cable addressing is untouched: notebook 192.168.50.253 on eno2, Pi 4 192.168.50.254.
set -euo pipefail
LAN_IF=eno2
LAN_NET=192.168.50.0/24

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/90-almita-forward.conf
sysctl -q --system >/dev/null
sysctl net.ipv4.ip_forward

cat > /etc/almita-pi4-nat.nft <<EOF
table ip almita_pi4_nat
delete table ip almita_pi4_nat
table ip almita_pi4_nat {
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr $LAN_NET oifname != "$LAN_IF" masquerade
  }
}
EOF

cat > /etc/systemd/system/almita-pi4-nat.service <<EOF
[Unit]
Description=ALMITA - share the notebook's internet with the Pi 4 over the direct cable (NAT for $LAN_NET)
After=network-pre.target ufw.service
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/almita-pi4-nat.nft
ExecStop=/usr/sbin/nft delete table ip almita_pi4_nat

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now almita-pi4-nat.service
systemctl restart almita-pi4-nat.service

if ufw status | grep -q "Status: active"; then
  ufw route allow in on "$LAN_IF" from "$LAN_NET" comment "ALMITA Pi 4 internet via notebook" >/dev/null
  echo "ufw: routed traffic from $LAN_IF allowed"
fi

nft list table ip almita_pi4_nat
echo "OK - notebook shares its internet with $LAN_NET (persistent)"
