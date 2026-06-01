#!/bin/bash
# Self-healing alias: ensures the Mac carries 192.168.100.2 on whichever
# interface is connected to the HT812 (the one already holding 192.168.2.2),
# so the device (WAN 192.168.100.100) and Asterisk stay on the same subnet.
#
# Why this exists: the USB 10/100 LAN adapter can be unplugged or re-enumerate
# under a different interface name (en7 → en8 …). Hardcoding the name breaks.
# This finds the interface by its base IP (192.168.2.2, set manually on the
# "USB 10/100 LAN" service) and adds the alias idempotently. Safe to run on a
# timer — it does nothing if the alias is already present or the adapter is gone.
#
# Install as a LaunchDaemon: see scripts/com.pbx.en7alias.plist

BASE_IP="192.168.2.2"
ALIAS_IP="192.168.100.2"
ALIAS_MASK="255.255.255.0"

# Find the interface that currently has the base IP (the HT812 link).
IFACE=$(ifconfig 2>/dev/null | awk -v ip="$BASE_IP" '
  /^[a-z]/ { cur=$1; sub(":","",cur) }
  $1=="inet" && $2==ip { print cur; exit }
')

if [ -z "$IFACE" ]; then
  echo "$(date '+%FT%T') no interface holding $BASE_IP — HT812 adapter not connected; nothing to do"
  exit 0
fi

if ifconfig "$IFACE" 2>/dev/null | grep -q "inet $ALIAS_IP"; then
  echo "$(date '+%FT%T') alias $ALIAS_IP already present on $IFACE"
  exit 0
fi

/sbin/ifconfig "$IFACE" alias "$ALIAS_IP" "$ALIAS_MASK"
echo "$(date '+%FT%T') added alias $ALIAS_IP on $IFACE"
