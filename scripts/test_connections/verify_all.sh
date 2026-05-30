#!/usr/bin/env bash
# Verify Mac network paths, HT812 reachability, Docker services, and SIP endpoint state.

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

section "Expected Addresses"
cat <<EOF
USB_IFACE=$USB_IFACE
USB_SERVICE=$USB_SERVICE
HT812 LAN/admin target: $HT812_LAN_IP from local $HT812_LAN_LOCAL_IP/$HT812_LAN_MASK
HT812 WAN target:       $HT812_WAN_IP from local $HT812_WAN_LOCAL_IP/$HT812_WAN_MASK
Asterisk SIP host:      $ASTERISK_HOST:5060
HT812 API URL:          $HT812_API_URL
Web URL:                $WEB_URL
EOF

section "USB Ethernet Interface"
if ifconfig "$USB_IFACE" >/tmp/ht812_ifconfig.txt 2>&1; then
  cat /tmp/ht812_ifconfig.txt
  if grep -q "status: active" /tmp/ht812_ifconfig.txt; then
    pass "$USB_IFACE link is active"
  else
    fail "$USB_IFACE link is not active"
  fi
else
  cat /tmp/ht812_ifconfig.txt
  fail "Cannot read interface $USB_IFACE"
fi

section "Route Selection"
for target in "$HT812_LAN_IP" "$HT812_WAN_IP"; do
  iface="$(route_iface_for "$target" || true)"
  flags="$(route_flags_for "$target" || true)"
  if [ -z "$iface" ]; then
    fail "No route found for $target"
  else
    info "$target route interface=$iface flags=$flags"
    if [ "$iface" = "$USB_IFACE" ]; then
      pass "$target routes over $USB_IFACE"
    else
      warn "$target routes over $iface instead of $USB_IFACE"
    fi
    if printf '%s' "$flags" | grep -q 'REJECT'; then
      warn "$target has a REJECT route; ARP likely failed recently"
    fi
  fi
done

section "ARP State"
for target in "$HT812_LAN_IP" "$HT812_WAN_IP"; do
  if entry="$(arp_entry_for "$target")"; then
    info "$entry"
    if printf '%s' "$entry" | grep -q '(incomplete)'; then
      fail "$target has incomplete ARP entry"
    else
      pass "$target has a resolved ARP entry"
    fi
  else
    warn "No ARP entry for $target yet"
  fi
done

section "HT812 HTTP/HTTPS Probes"
for url in "http://$HT812_LAN_IP/" "https://$HT812_LAN_IP/" "http://$HT812_WAN_IP/" "https://$HT812_WAN_IP/"; do
  if curl_probe "$url" "$USB_IFACE"; then
    pass "$url reachable over $USB_IFACE"
  else
    fail "$url not reachable over $USB_IFACE"
  fi
done

section "Local PBX Services"
if curl_probe "$WEB_URL"; then
  pass "Web dashboard reachable at $WEB_URL"
else
  warn "Web dashboard not reachable at $WEB_URL"
fi

if curl_probe "$HT812_API_URL/health"; then
  pass "HT812 API health reachable"
else
  warn "HT812 API health not reachable"
fi

if have_cmd docker; then
  if docker compose -f "$ROOT_DIR/docker-compose.yml" ps >/tmp/ht812_compose_ps.txt 2>&1; then
    cat /tmp/ht812_compose_ps.txt
    pass "docker compose ps succeeded"
  else
    cat /tmp/ht812_compose_ps.txt
    warn "docker compose ps failed"
  fi

  if docker exec asterisk asterisk -rx "pjsip show endpoints" >/tmp/ht812_pjsip.txt 2>&1; then
    cat /tmp/ht812_pjsip.txt
    if grep -q "Endpoint:  1001.*Avail" /tmp/ht812_pjsip.txt && grep -q "Endpoint:  1002.*Avail" /tmp/ht812_pjsip.txt; then
      pass "Asterisk endpoints 1001 and 1002 are available"
    else
      warn "Asterisk endpoints are not both available"
    fi
  else
    cat /tmp/ht812_pjsip.txt
    warn "Could not query Asterisk PJSIP endpoints"
  fi
else
  warn "docker command not found"
fi

print_summary
