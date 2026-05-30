#!/usr/bin/env bash
# Probe likely HT812 addresses over the selected USB interface.

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

CANDIDATES="${CANDIDATES:-192.168.2.1 192.168.0.160 192.168.2.100 192.168.100.100 192.168.100.1}"

section "Candidate Scan"
info "Interface: $USB_IFACE"
info "Candidates: $CANDIDATES"

for ip in $CANDIDATES; do
  printf '\n-- %s --\n' "$ip"
  iface="$(route_iface_for "$ip" || true)"
  flags="$(route_flags_for "$ip" || true)"
  info "route interface=${iface:-none} flags=${flags:-none}"

  if ping -c 1 -W 1000 "$ip" >/dev/null 2>&1; then
    pass "$ip responds to ICMP"
  else
    warn "$ip does not respond to ICMP"
  fi

  for scheme in http https; do
    url="$scheme://$ip/"
    if curl_probe "$url" "$USB_IFACE"; then
      pass "$url reachable over $USB_IFACE"
    else
      warn "$url not reachable over $USB_IFACE"
    fi
  done

  if entry="$(arp_entry_for "$ip")"; then
    info "$entry"
  else
    info "no ARP entry"
  fi
done

print_summary
