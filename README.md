# pbx_ivr — HT812V2 + Asterisk IVR Integration

A self-hosted PBX/IVR stack that integrates a **Grandstream HT812 V2** analog telephone adapter with a Dockerized **Asterisk** server and a Python **FastAPI** control plane.

---

## Architecture

```
Analog phone(s)
    │ FXS port 1 (ext 1001)
    │ FXS port 2 (ext 1002)
    ▼
Grandstream HT812V2 ──SIP──▶ Asterisk (Docker)
   192.168.0.160                │  pjsip.conf, extensions.conf
                                │  voicemail.conf, ari.conf
                                │
                         ┌──────┴──────────────────────┐
                         │ IVR Layers                   │
                         │  1. DTMF dialplan            │
                         │  2. AGI script (Python)      │
                         │  3. ARI app (asyncio WS)     │
                         │  4. Voicemail                │
                         └──────────────────────────────┘

ht812_api (FastAPI :8000) ──HTTPS──▶ HT812V2 CGI API
   Config snapshot/patch, reboot, factory-reset, SIP status
```

---

## Services

| Service | Port | Description |
|---------|------|-------------|
| `asterisk` | 5060/udp, 8088, 10000-10100/udp | Asterisk PBX |
| `ht812_api` | 8000 | FastAPI control plane for HT812V2 |
| `ari_app` | — | ARI WebSocket IVR app (internal) |

---

## Quick Start

### 1. Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:

```env
HT812_HOST=https://192.168.0.160
HT812_ADMIN_USER=admin
HT812_ADMIN_PASS=<your-ht812-admin-password>

SIP_1001_PASS=<choose-a-password-for-ext-1001>
SIP_1002_PASS=<choose-a-password-for-ext-1002>

ARI_USER=ari-user
ARI_PASS=<choose-an-ari-password>
```

### 2. Start everything

```bash
docker compose up --build
```

### 3. Configure HT812V2

Open `https://192.168.0.160` → log in → configure each FXS port:

| Field | Port 1 | Port 2 |
|-------|--------|--------|
| SIP Server | `<your-docker-host-ip>` | `<your-docker-host-ip>` |
| SIP Server Port | `5060` | `5060` |
| SIP User ID | `1001` | `1002` |
| SIP Auth ID | `1001` | `1002` |
| SIP Auth Password | value of `SIP_1001_PASS` | value of `SIP_1002_PASS` |

Click **Apply** then **Reboot**.

### 4. Verify SIP registration

```bash
docker exec -it asterisk asterisk -rvvv
# In the Asterisk CLI:
pjsip show endpoints
pjsip show registrations
```

Both 1001 and 1002 should show `Avail`.

---

## HT812V2 API

Base URL: `http://localhost:8000`

Interactive docs: `http://localhost:8000/docs`

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/ht812/config` | Export full config as XML (saves timestamped backup to `./backups/`) |
| `GET` | `/ht812/values?keys=P47,P48` | Read specific P-value settings |
| `PATCH` | `/ht812/config` | Write P-value settings |
| `POST` | `/ht812/reboot` | Reboot device (~30s downtime) |
| `POST` | `/ht812/factory-reset?reset_type=2` | Factory reset (0=ISP, 1=VoIP, 2=full) |
| `GET` | `/ht812/status/ports` | FXS port SIP registration status |
| `GET` | `/ht812/status/system` | Firmware, MAC, model, uptime |
| `GET` | `/ht812/status/network` | IP, DNS, gateway info |
| `GET` | `/health` | Service health check |

### Examples

```bash
# Export config
curl http://localhost:8000/ht812/config > ht812_backup.xml

# Read SIP server setting for port 1
curl "http://localhost:8000/ht812/values?keys=P47,P48"

# Change SIP server for port 1 (P47) and port 2 (P2312)
curl -X PATCH http://localhost:8000/ht812/config \
  -H "Content-Type: application/json" \
  -d '{"params": {"P47": "192.168.0.100", "P2312": "192.168.0.100"}}'

# Check port registration status
curl http://localhost:8000/ht812/status/ports

# Reboot
curl -X POST http://localhost:8000/ht812/reboot

# Full factory reset (destructive!)
curl -X POST "http://localhost:8000/ht812/factory-reset?reset_type=2"
```

---

## IVR Dialplan

### Call flow (from analog phone)

```
Pick up phone → HT812V2 FXS → SIP → Asterisk [from-ht812]
    → [ivr-main]
       1 → Sales
       2 → Support
       0 → Operator (ext 1001)
       8 → Voicemail check
       9 → AGI dynamic IVR (time-of-day routing, VIP bypass)
       * → ARI Stasis app (full programmatic control)
```

### Key files

| File | Purpose |
|------|---------|
| `asterisk/etc/asterisk/pjsip.conf` | SIP endpoints for HT812V2 FXS ports |
| `asterisk/etc/asterisk/extensions.conf` | IVR dialplan (DTMF menus, AGI, ARI hooks) |
| `asterisk/etc/asterisk/voicemail.conf` | Voicemail boxes for 1001/1002 |
| `asterisk/etc/asterisk/agi-bin/ivr_dynamic.py` | AGI script (time-of-day routing) |
| `ari_app/ivr_ari.py` | ARI WebSocket app |

### Custom IVR prompts

Place custom `.wav` or `.gsm` files in `asterisk/etc/asterisk/sounds/` and reference them in `extensions.conf`:

```
; Replace tt-weasels with your recording:
same => n,Background(custom/welcome)
```

### Voicemail

Default mailbox PIN is `1234` for both extensions. Change in `voicemail.conf`:

```
1001 => 9999,Extension 1001,user@example.com
```

Check voicemail: dial `8` from IVR menu, or dial `*97` internally.

---

## Project Structure

```
pbx_ivr/
├── docker-compose.yml
├── .env                    ← credentials (gitignored)
├── .env.example            ← template
├── backups/                ← HT812V2 config snapshots (gitignored)
├── asterisk/
│   ├── Dockerfile
│   ├── docker-entrypoint.sh
│   └── etc/asterisk/
│       ├── asterisk.conf
│       ├── pjsip.conf
│       ├── extensions.conf
│       ├── voicemail.conf
│       ├── http.conf
│       ├── ari.conf
│       ├── modules.conf
│       └── agi-bin/
│           └── ivr_dynamic.py
├── ht812_api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py
│   ├── ht812_client.py
│   ├── router.py
│   └── models.py
└── ari_app/
    ├── Dockerfile
    ├── requirements.txt
    └── ivr_ari.py
```

---

## HT812V2 API Notes

This device runs firmware 3.7.5 with a Vue.js SPA frontend (not the older CGI-only API documented in Grandstream's older guides).

**Auth**: `POST /cgi-bin/dologin` with `username=admin&P2=<password>` (URL-encoded). Returns a `session_token` appended to all subsequent requests.

**Config P-values**: Settings are referenced by P-number (e.g., `P47` = SIP server for port 1, `P2312` = SIP server for port 2). See the [Grandstream HT812 Admin Guide](https://documentation.grandstream.com/knowledge-base/ht812-ht814-administration-guide/) for the full P-value reference.

Common P-values:
| P-value | Setting |
|---------|---------|
| `P47` | SIP Server (port 1) |
| `P48` | SIP Server Port (port 1) |
| `P35` | SIP User ID (port 1) |
| `P36` | Authenticate ID (port 1) |
| `P34` | Authenticate Password (port 1) |
| `P2312` | SIP Server (port 2) |
| `P2313` | SIP Server Port (port 2) |
| `P2300` | SIP User ID (port 2) |

---

## Development

### Run ht812_api locally (without Docker)

```bash
cd ht812_api
pip install -r requirements.txt
HT812_HOST=https://192.168.0.160 \
HT812_ADMIN_USER=admin \
HT812_ADMIN_PASS=yourpassword \
uvicorn main:app --reload --port 8000
```

### Run Asterisk locally for testing

```bash
docker compose up asterisk
# Tail logs:
docker logs -f asterisk
# Drop into CLI:
docker exec -it asterisk asterisk -rvvv
```

### Reload Asterisk config without restart

```bash
docker exec -it asterisk asterisk -rx "core reload"
docker exec -it asterisk asterisk -rx "dialplan reload"
docker exec -it asterisk asterisk -rx "pjsip reload"
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| HT812 API returns `401 Unauthorized` | Check `HT812_ADMIN_PASS` in `.env`; reset lockout by waiting 5 min |
| SIP endpoints show `Unavail` | Verify SIP server IP/port in HT812V2 web UI matches docker host |
| No audio on calls | Open RTP ports `10000-10100/udp` on host firewall |
| AGI script errors | Check `docker logs asterisk`; ensure Python3 in container |
| ARI app disconnects | Check `ARI_PASS` matches value in `ari.conf`; verify port 8088 is reachable |
