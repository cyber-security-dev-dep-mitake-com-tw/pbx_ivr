#!/usr/bin/env bash
# Capture USB Ethernet ARP/HTTP traffic while probing the HT812.
# Usage:
#   ./scripts/test_connections/capture_usb_arp.sh 192.168.0.160
#   ./scripts/test_connections/capture_usb_arp.sh 192.168.2.1

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

TARGET="${1:-$HT812_WAN_IP}"
COUNT="${COUNT:-30}"

section "USB Ethernet Packet Capture"
cat <<EOF
Interface: $USB_IFACE
Target:    $TARGET
Packets:   $COUNT
EOF

require_sudo "./scripts/test_connections/capture_usb_arp.sh $TARGET"

section "Instructions"
cat <<EOF
This capture should show:
- ARP request: who-has $TARGET
- ARP reply from the HT812 MAC, if the device is answering
- TCP SYN packets to ports 80/443 during browser/curl probes

In another Terminal or browser, probe the same target while this runs.
EOF

sudo tcpdump -ni "$USB_IFACE" -c "$COUNT" "arp or host $TARGET"
