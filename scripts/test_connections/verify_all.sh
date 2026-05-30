#!/usr/bin/env bash
# Verify Mac network paths, HT812 reachability, Docker services, and SIP endpoint state.

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

init_logs
save_cmd ifconfig_"$USB_IFACE".txt ifconfig "$USB_IFACE"
save_cmd netstat_inet.txt netstat -rn -f inet
save_cmd arp_before.txt arp -an

section "Expected Addresses"
CURRENT_PROFILE="$(expected_profile_for_current_ip)"
cat <<EOF
USB_IFACE=$USB_IFACE
USB_SERVICE=$USB_SERVICE
Current USB IPv4:       $(iface_ipv4_for "$USB_IFACE" || true)
Current profile:        $CURRENT_PROFILE
HT812 LAN/admin target: $HT812_LAN_IP from local $HT812_LAN_LOCAL_IP/$HT812_LAN_MASK
HT812 WAN target:       $HT812_WAN_IP from local $HT812_WAN_LOCAL_IP/$HT812_WAN_MASK
Asterisk SIP host:      $ASTERISK_HOST:5060
HT812 API URL:          $HT812_API_URL
Web URL:                $WEB_URL
EOF

if [ "$CURRENT_PROFILE" = "wan" ]; then
  info "Current USB profile is WAN/SIP. LAN/admin checks for $HT812_LAN_IP are expected to fail unless you switch to the lan profile."
elif [ "$CURRENT_PROFILE" = "lan" ]; then
  info "Current USB profile is LAN/admin. WAN checks for $HT812_WAN_IP are expected to fail unless you switch to the wan profile."
else
  warn "USB IPv4 does not match known lan/wan profile; patch with patch_usb_profile.sh lan|wan."
fi

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
      warn "Run: APPLY=1 ./scripts/test_connections/clear_target_cache.sh $target"
    fi
  fi
done

section "ARP State"
ARP_TARGETS=()
if [ "$CURRENT_PROFILE" = "lan" ]; then
  ARP_TARGETS=("$HT812_LAN_IP")
elif [ "$CURRENT_PROFILE" = "wan" ]; then
  ARP_TARGETS=("$HT812_WAN_IP")
else
  ARP_TARGETS=("$HT812_LAN_IP" "$HT812_WAN_IP")
fi

for target in "${ARP_TARGETS[@]}"; do
  if entry="$(arp_entry_for "$target")"; then
    info "$entry"
    if arp_remote_resolved_for "$target" "$USB_IFACE"; then
      remote_mac="$(arp_remote_mac_for "$target" "$USB_IFACE" | head -n 1)"
      pass "$target has a resolved remote ARP entry (${remote_mac:-unknown-mac})"
    else
      fail "$target has no resolved remote ARP entry on $USB_IFACE"
    fi
  else
    warn "No ARP entry for $target yet"
  fi
done

if [ "$CURRENT_PROFILE" = "wan" ]; then
  warn "Skipped LAN ARP verdict for $HT812_LAN_IP because current profile is wan"
elif [ "$CURRENT_PROFILE" = "lan" ]; then
  warn "Skipped WAN ARP verdict for $HT812_WAN_IP because current profile is lan"
fi

section "HT812 HTTP/HTTPS Probes"
URLS=()
if [ "$CURRENT_PROFILE" = "lan" ]; then
  URLS=("http://$HT812_LAN_IP/" "https://$HT812_LAN_IP/")
elif [ "$CURRENT_PROFILE" = "wan" ]; then
  URLS=("http://$HT812_WAN_IP/" "https://$HT812_WAN_IP/")
else
  URLS=("http://$HT812_LAN_IP/" "https://$HT812_LAN_IP/" "http://$HT812_WAN_IP/" "https://$HT812_WAN_IP/")
fi

for url in "${URLS[@]}"; do
  log_name="$(printf '%s' "$url" | sed 's#[/:]#_#g').log"
  if curl_probe_verbose "$url" "$LOG_DIR/$log_name" "$USB_IFACE"; then
    pass "$url reachable over $USB_IFACE"
    continue
  fi
  tail -n 12 "$LOG_DIR/$log_name"
  info "curl log: $LOG_DIR/$log_name"
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
  if docker compose -f "$ROOT_DIR/docker-compose.yml" ps >"$LOG_DIR/docker_compose_ps.txt" 2>&1; then
    cat "$LOG_DIR/docker_compose_ps.txt"
    pass "docker compose ps succeeded"
  else
    cat "$LOG_DIR/docker_compose_ps.txt"
    warn "docker compose ps failed"
  fi

  if docker exec asterisk asterisk -rx "pjsip show endpoints" >"$LOG_DIR/pjsip_show_endpoints.txt" 2>&1; then
    cat "$LOG_DIR/pjsip_show_endpoints.txt"
    if grep -q "Endpoint:  1001.*Avail" "$LOG_DIR/pjsip_show_endpoints.txt" && grep -q "Endpoint:  1002.*Avail" "$LOG_DIR/pjsip_show_endpoints.txt"; then
      pass "Asterisk endpoints 1001 and 1002 are available"
    else
      warn "Asterisk endpoints are not both available"
    fi
  else
    cat "$LOG_DIR/pjsip_show_endpoints.txt"
    warn "Could not query Asterisk PJSIP endpoints"
  fi
else
  warn "docker command not found"
fi

save_cmd arp_after.txt arp -an

print_summary
