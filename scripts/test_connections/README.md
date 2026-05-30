# HT812 Connection Test Scripts

These scripts diagnose and patch the HT812 network paths used by this repo.

Defaults match the saved backups:

- HT812 WAN/static IP: `192.168.0.160`
- HT812 LAN/admin IP: `192.168.2.1`
- Mac USB Ethernet for WAN/SIP: `192.168.0.100/16`
- Mac USB Ethernet for LAN/admin: `192.168.2.10/24`
- USB interface: `en7`
- USB macOS service name: `USB 10/100 LAN`

## Diagnose Everything

```bash
./scripts/test_connections/verify_all.sh
```

This checks:

- USB Ethernet link state
- macOS route selection
- ARP state
- HTTP/HTTPS reachability for `192.168.2.1` and `192.168.0.160`
- local web/API services
- Docker Compose service state
- Asterisk PJSIP endpoint state

Each run writes detailed artifacts to:

```text
scripts/test_connections/logs/<timestamp>/
```

## Scan Likely HT812 Addresses

```bash
./scripts/test_connections/scan_candidates.sh
```

Override candidates:

```bash
CANDIDATES="192.168.2.1 192.168.0.160 192.168.100.100" ./scripts/test_connections/scan_candidates.sh
```

## Patch USB Ethernet Profile

Dry run:

```bash
./scripts/test_connections/patch_usb_profile.sh lan
./scripts/test_connections/patch_usb_profile.sh wan
```

Apply LAN/admin profile:

```bash
APPLY=1 ./scripts/test_connections/patch_usb_profile.sh lan
```

Apply WAN/SIP profile:

```bash
APPLY=1 ./scripts/test_connections/patch_usb_profile.sh wan
```

## Force Host Route

Useful when Wi-Fi or another interface steals `192.168.0.160`.

Dry run:

```bash
./scripts/test_connections/patch_host_route.sh 192.168.0.160
```

Apply:

```bash
APPLY=1 ./scripts/test_connections/patch_host_route.sh 192.168.0.160
```

## Capture and Probe Automatically

Use this when a route is correct but the HT812 still times out:

```bash
./scripts/test_connections/capture_and_probe.sh 192.168.0.160
```

or:

```bash
./scripts/test_connections/capture_and_probe.sh 192.168.2.1
```

The script starts `tcpdump`, runs HTTP/HTTPS and ping probes, then saves the
packet trace and probe logs under `scripts/test_connections/logs/<timestamp>/`.

## Override Defaults

All scripts accept environment overrides:

```bash
USB_IFACE=en7 \
USB_SERVICE="USB 10/100 LAN" \
HT812_LAN_IP=192.168.2.1 \
HT812_LAN_LOCAL_IP=192.168.2.10 \
HT812_WAN_IP=192.168.0.160 \
HT812_WAN_LOCAL_IP=192.168.0.100 \
./scripts/test_connections/verify_all.sh
```
