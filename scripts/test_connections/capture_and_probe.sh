#!/usr/bin/env bash
# Run tcpdump and active probes together, saving detailed logs.
# Usage:
#   ./scripts/test_connections/capture_and_probe.sh 192.168.0.160
#   ./scripts/test_connections/capture_and_probe.sh 192.168.2.1

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

TARGET="${1:-$HT812_WAN_IP}"
COUNT="${COUNT:-80}"

init_logs

section "Capture And Probe"
cat <<EOF
Interface: $USB_IFACE
Target:    $TARGET
Logs:      $LOG_DIR
Filter:    $(tcpdump_filter_for "$TARGET")
EOF

require_sudo "./scripts/test_connections/capture_and_probe.sh $TARGET"

TCPDUMP_LOG="$LOG_DIR/tcpdump_${TARGET}.log"
ARP_BEFORE="$LOG_DIR/arp_before_${TARGET}.txt"
ARP_AFTER="$LOG_DIR/arp_after_${TARGET}.txt"
ROUTE_BEFORE="$LOG_DIR/route_before_${TARGET}.txt"
ROUTE_AFTER_CLEAR="$LOG_DIR/route_after_clear_${TARGET}.txt"

arp -an >"$ARP_BEFORE" 2>&1 || true
route -n get "$TARGET" >"$ROUTE_BEFORE" 2>&1 || true
clear_target_cache "$TARGET"
route -n get "$TARGET" >"$ROUTE_AFTER_CLEAR" 2>&1 || true

sudo tcpdump -U -l -e -vv -ni "$USB_IFACE" -c "$COUNT" "$(tcpdump_filter_for "$TARGET")" >"$TCPDUMP_LOG" 2>&1 &
TCPDUMP_PID=$!
sleep 1

for url in "http://$TARGET/" "https://$TARGET/"; do
  name="$(printf '%s' "$url" | sed 's#[/:]#_#g')"
  info "probing $url"
  curl_probe_verbose "$url" "$LOG_DIR/${name}.log" "$USB_IFACE" || true
done

ping -c 2 -W 1000 "$TARGET" >"$LOG_DIR/ping_${TARGET}.log" 2>&1 || true

sleep 2
if kill -0 "$TCPDUMP_PID" 2>/dev/null; then
  sudo kill "$TCPDUMP_PID" >/dev/null 2>&1 || true
fi
wait "$TCPDUMP_PID" >/dev/null 2>&1 || true

arp -an >"$ARP_AFTER" 2>&1 || true

section "Tcpdump"
cat "$TCPDUMP_LOG"

section "Route State"
printf '\n-- before clear --\n'
cat "$ROUTE_BEFORE"
printf '\n-- after clear --\n'
cat "$ROUTE_AFTER_CLEAR"

section "Probe Logs"
for f in "$LOG_DIR"/*"$TARGET"*.log; do
  printf '\n-- %s --\n' "$f"
  tail -n 30 "$f"
done

section "Verdict"
if grep -q "Reply" "$TCPDUMP_LOG" || grep -qi "is-at" "$TCPDUMP_LOG"; then
  pass "A device replied on the wire for $TARGET"
else
  fail "No ARP reply observed for $TARGET on $USB_IFACE"
fi

print_summary
