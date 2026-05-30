#!/usr/bin/env bash

set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

USB_IFACE="${USB_IFACE:-en7}"
USB_SERVICE="${USB_SERVICE:-USB 10/100 LAN}"

HT812_LAN_IP="${HT812_LAN_IP:-192.168.2.1}"
HT812_LAN_LOCAL_IP="${HT812_LAN_LOCAL_IP:-192.168.2.10}"
HT812_LAN_RESET_LOCAL_IP="${HT812_LAN_RESET_LOCAL_IP:-192.168.2.2}"
HT812_LAN_MASK="${HT812_LAN_MASK:-255.255.255.0}"

HT812_WAN_IP="${HT812_WAN_IP:-192.168.0.160}"
HT812_WAN_LOCAL_IP="${HT812_WAN_LOCAL_IP:-192.168.0.100}"
HT812_WAN_MASK="${HT812_WAN_MASK:-255.255.0.0}"

ASTERISK_HOST="${ASTERISK_HOST:-192.168.0.100}"
HT812_API_URL="${HT812_API_URL:-http://localhost:8000}"
WEB_URL="${WEB_URL:-http://localhost:3000}"

LOG_ROOT="${LOG_ROOT:-$ROOT_DIR/scripts/test_connections/logs}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
LOG_DIR="${LOG_DIR:-$LOG_ROOT/$RUN_ID}"

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

init_logs() {
  mkdir -p "$LOG_DIR"
  info "debug logs: $LOG_DIR"
}

save_cmd() {
  local name="$1"
  shift
  mkdir -p "$LOG_DIR"
  {
    printf '+ %s\n' "$*"
    "$@"
    printf '\nexit=%s\n' "$?"
  } >"$LOG_DIR/$name" 2>&1
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

iface_ipv4_for() {
  ifconfig "$1" 2>/dev/null | awk '/inet / {print $2; exit}'
}

iface_netmask_hex_for() {
  ifconfig "$1" 2>/dev/null | awk '/inet / {print $4; exit}'
}

wait_for_iface_ipv4() {
  local iface="$1"
  local expected_ip="$2"
  local attempts="${3:-10}"
  local current_ip
  local i
  for i in $(seq 1 "$attempts"); do
    current_ip="$(iface_ipv4_for "$iface")"
    if [ "$current_ip" = "$expected_ip" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_iface_ipv4_prefix() {
  local iface="$1"
  local prefix="$2"
  local attempts="${3:-20}"
  local current_ip
  local i
  for i in $(seq 1 "$attempts"); do
    current_ip="$(iface_ipv4_for "$iface")"
    case "$current_ip" in
      "$prefix"*) return 0 ;;
    esac
    sleep 1
  done
  return 1
}

clear_iface_ipv4() {
  local iface="$1"
  sudo ifconfig "$iface" inet 0.0.0.0 >/dev/null 2>&1 || true
}

expected_profile_for_current_ip() {
  local ip
  ip="$(iface_ipv4_for "$USB_IFACE")"
  if [ "$ip" = "$HT812_LAN_LOCAL_IP" ]; then
    printf 'lan'
  elif [ "$ip" = "$HT812_WAN_LOCAL_IP" ]; then
    printf 'wan'
  else
    printf 'unknown'
  fi
}

normalize_mac() {
  awk -F: '{
    out = ""
    for (i = 1; i <= NF; i++) {
      oct = tolower($i)
      if (length(oct) == 1) oct = "0" oct
      out = out (i == 1 ? "" : ":") oct
    }
    print out
  }'
}

arp_remote_resolved_for() {
  local ip="$1"
  local iface="$2"
  local local_mac
  local_mac="$(iface_mac_for "$iface" | normalize_mac)"
  arp_entry_for "$ip" | awk -v local_mac="$local_mac" '
    /\(incomplete\)/ {next}
    {
      mac = tolower($4)
      split(mac, parts, ":")
      normalized = ""
      for (i = 1; i <= length(parts); i++) {
        oct = parts[i]
        if (length(oct) == 1) oct = "0" oct
        normalized = normalized (i == 1 ? "" : ":") oct
      }
    }
    local_mac != "" && normalized == local_mac {next}
    {found=1}
    END {exit found ? 0 : 1}
  '
}

arp_remote_mac_for() {
  local ip="$1"
  local iface="$2"
  local local_mac
  local_mac="$(iface_mac_for "$iface" | normalize_mac)"
  arp_entry_for "$ip" | awk -v local_mac="$local_mac" '
    /\(incomplete\)/ {next}
    {
      mac = tolower($4)
      split(mac, parts, ":")
      normalized = ""
      for (i = 1; i <= length(parts); i++) {
        oct = parts[i]
        if (length(oct) == 1) oct = "0" oct
        normalized = normalized (i == 1 ? "" : ":") oct
      }
      if (local_mac == "" || normalized != local_mac) print normalized
    }
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

curl_probe_verbose() {
  local url="$1"
  local outfile="$2"
  local iface="${3:-}"
  mkdir -p "$(dirname "$outfile")"
  if [ -n "$iface" ]; then
    curl -vk --max-time 6 --interface "$iface" "$url" >"$outfile" 2>&1
  else
    curl -vk --max-time 6 "$url" >"$outfile" 2>&1
  fi
}

tcpdump_filter_for() {
  local target="$1"
  printf 'arp or host %s or ether host %s' "$target" "$(iface_mac_for "$USB_IFACE")"
}

clear_target_cache() {
  local target="$1"
  info "clearing stale route/ARP cache for $target"
  sudo route -n delete -host "$target" >/dev/null 2>&1 || true
  sudo route -n delete "$target" >/dev/null 2>&1 || true
  sudo arp -d "$target" >/dev/null 2>&1 || true
}

print_summary() {
  section "Summary"
  printf 'PASS=%s WARN=%s FAIL=%s\n' "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"
  if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
  fi
}
