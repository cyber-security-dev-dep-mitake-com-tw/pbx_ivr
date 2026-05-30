#!/usr/bin/env bash
# Set the macOS USB Ethernet service to either the HT812 LAN-admin or WAN/SIP profile.
# Usage:
#   APPLY=1 ./patch_usb_profile.sh lan
#   APPLY=1 ./patch_usb_profile.sh wan

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

PROFILE="${1:-}"
if [ "$PROFILE" = "lan" ]; then
  IP="$HT812_LAN_LOCAL_IP"
  MASK="$HT812_LAN_MASK"
  TARGET="$HT812_LAN_IP"
elif [ "$PROFILE" = "wan" ]; then
  IP="$HT812_WAN_LOCAL_IP"
  MASK="$HT812_WAN_MASK"
  TARGET="$HT812_WAN_IP"
else
  echo "Usage: APPLY=1 $0 lan|wan" >&2
  exit 2
fi

section "USB Profile Patch"
cat <<EOF
Service: $USB_SERVICE
Profile: $PROFILE
Set IP:  $IP
Mask:    $MASK
Router:  0.0.0.0
Target:  $TARGET
EOF

if [ "${APPLY:-0}" != "1" ]; then
  warn "Dry run only. Re-run with APPLY=1 to change macOS network settings."
  exit 0
fi

if ! sudo -n true 2>/dev/null; then
  cat <<EOF
ERROR: sudo requires an interactive password.
Run this in a local Terminal:

  cd "$ROOT_DIR"
  APPLY=1 ./scripts/test_connections/patch_usb_profile.sh $PROFILE

EOF
  exit 1
fi

run_show sudo networksetup -setmanual "$USB_SERVICE" "$IP" "$MASK" "0.0.0.0"
run_show ifconfig "$USB_IFACE"
run_show route -n get "$TARGET"

section "Immediate Probe"
if curl_probe "http://$TARGET/" "$USB_IFACE" || curl_probe "https://$TARGET/" "$USB_IFACE"; then
  pass "$TARGET responded over $USB_IFACE"
else
  fail "$TARGET did not respond over $USB_IFACE"
fi

print_summary
