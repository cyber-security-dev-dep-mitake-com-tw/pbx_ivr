#!/usr/bin/env bash
# Force a single target IP over the USB Ethernet interface.
# Usage:
#   APPLY=1 ./patch_host_route.sh 192.168.0.160

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

TARGET="${1:-$HT812_WAN_IP}"

section "Host Route Patch"
cat <<EOF
Target:    $TARGET
Interface: $USB_IFACE
EOF

if [ "${APPLY:-0}" != "1" ]; then
  warn "Dry run only. Re-run with APPLY=1 to add the host route."
  exit 0
fi

require_sudo "APPLY=1 ./scripts/test_connections/patch_host_route.sh $TARGET"

sudo route -n delete -host "$TARGET" >/dev/null 2>&1 || true
run_show sudo route -n add -host "$TARGET" -interface "$USB_IFACE"
run_show route -n get "$TARGET"

section "Immediate Probe"
if curl_probe "http://$TARGET/" "$USB_IFACE" || curl_probe "https://$TARGET/" "$USB_IFACE"; then
  pass "$TARGET responded over $USB_IFACE"
else
  fail "$TARGET did not respond over $USB_IFACE"
fi

print_summary
