#!/usr/bin/env bash

set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

USB_IFACE="${USB_IFACE:-en7}"
USB_SERVICE="${USB_SERVICE:-USB 10/100 LAN}"

HT812_LAN_IP="${HT812_LAN_IP:-192.168.2.1}"
HT812_LAN_LOCAL_IP="${HT812_LAN_LOCAL_IP:-192.168.2.10}"
HT812_LAN_MASK="${HT812_LAN_MASK:-255.255.255.0}"

HT812_WAN_IP="${HT812_WAN_IP:-192.168.0.160}"
HT812_WAN_LOCAL_IP="${HT812_WAN_LOCAL_IP:-192.168.0.100}"
HT812_WAN_MASK="${HT812_WAN_MASK:-255.255.0.0}"

ASTERISK_HOST="${ASTERISK_HOST:-192.168.0.100}"
HT812_API_URL="${HT812_API_URL:-http://localhost:8000}"
WEB_URL="${WEB_URL:-http://localhost:3000}"

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

section() {
  printf '\n== %s ==\n' "$1"
}

info() {
  printf 'INFO: %s\n' "$1"
}

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  printf 'PASS: %s\n' "$1"
}

warn() {
  WARN_COUNT=$((WARN_COUNT + 1))
  printf 'WARN: %s\n' "$1"
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf 'FAIL: %s\n' "$1"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

run_show() {
  printf '+ %s\n' "$*"
  "$@"
}

route_iface_for() {
  route -n get "$1" 2>/dev/null | awk '/interface:/ {print $2; exit}'
}

route_flags_for() {
  route -n get "$1" 2>/dev/null | awk '/flags:/ {$1=""; sub(/^ /, ""); print; exit}'
}

arp_entry_for() {
  arp -an 2>/dev/null | awk -v ip="$1" '
    $0 ~ "^\\? \\(" ip "\\) " {print; found=1}
    END {if (!found) exit 1}
  '
}

iface_mac_for() {
  ifconfig "$1" 2>/dev/null | awk '/ether / {print tolower($2); exit}'
}

arp_remote_resolved_for() {
  local ip="$1"
  local iface="$2"
  local local_mac
  local_mac="$(iface_mac_for "$iface")"
  arp_entry_for "$ip" | awk -v local_mac="$local_mac" '
    /\(incomplete\)/ {next}
    local_mac != "" && tolower($4) == local_mac {next}
    {found=1}
    END {exit found ? 0 : 1}
  '
}

require_sudo() {
  local command_hint="$1"
  if sudo -n true 2>/dev/null; then
    return 0
  fi
  if [ "${NONINTERACTIVE:-0}" = "1" ]; then
    cat <<EOF
ERROR: sudo requires a password and NONINTERACTIVE=1 is set.
Run this in a local Terminal:

  cd "$ROOT_DIR"
  $command_hint

EOF
    return 1
  fi
  info "sudo permission is required; macOS may prompt for your password now."
  sudo -v
}

curl_probe() {
  local url="$1"
  local iface="${2:-}"
  if [ -n "$iface" ]; then
    curl -kfsS --max-time 4 --interface "$iface" "$url" >/dev/null
  else
    curl -kfsS --max-time 4 "$url" >/dev/null
  fi
}

print_summary() {
  section "Summary"
  printf 'PASS=%s WARN=%s FAIL=%s\n' "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"
  if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
  fi
}
