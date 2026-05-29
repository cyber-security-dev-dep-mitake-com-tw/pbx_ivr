# Development Plans

## Current State (v1.0 — 2026-05-29)

All three Docker services are scaffolded and building. HT812V2 API client has been reverse-engineered from firmware 3.7.5 JavaScript bundle and updated to use the correct endpoints.

---

## Phase 1 — Foundation (complete)

- [x] Git repo + `.gitignore`
- [x] `.env` / `.env.example` with all credentials
- [x] `ht812_api/` — FastAPI service with correct CGI auth flow
  - Reverse-engineered real endpoints from firmware JS bundle
  - `POST /cgi-bin/dologin` (P2 password field, session_token response)
  - `GET /cgi-bin/download_cfg_xml` — full XML backup with timestamped file save
  - `GET /cgi-bin/api.values.get` — read P-values
  - `POST /cgi-bin/api.values.post` — write/apply P-values
  - `POST /cgi-bin/rs` — reboot
  - `POST /cgi-bin/unit_reset` — factory reset (types 0/1/2)
  - `GET /status/portStatus`, `/status/systemInfo`, `/status/netStatus`
- [x] `asterisk/` — Dockerfile + full config set
  - PJSIP endpoints for FXS port 1 (1001) and port 2 (1002)
  - DTMF IVR dialplan with menu options 0-9 and *
  - AGI script with time-of-day routing + VIP caller bypass
  - Voicemail for both extensions
  - ARI HTTP + credentials
- [x] `ari_app/` — async WebSocket ARI app
- [x] `docker-compose.yml`
- [x] `README.md` + `devplans.md`

---

## Phase 2 — Wire Up & Validate (next)

### 2.1 Live device testing

- [ ] Set correct `HT812_ADMIN_PASS` in `.env`
- [ ] `docker compose up --build` — confirm all 3 services start
- [ ] `curl http://localhost:8000/ht812/status/system` — confirm JSON response
- [ ] `curl http://localhost:8000/ht812/status/ports` — confirm FXS reg status
- [ ] `curl http://localhost:8000/ht812/config` — confirm XML dump + backup file

### 2.2 SIP registration

- [ ] Configure HT812V2 FXS port 1: server=`<docker-host>:5060`, user=`1001`
- [ ] Configure HT812V2 FXS port 2: server=`<docker-host>:5060`, user=`1002`
- [ ] `pjsip show endpoints` in Asterisk CLI — both show `Avail`

### 2.3 IVR smoke test

- [ ] Pick up analog phone → hear IVR prompt
- [ ] Press `0` → connects to operator
- [ ] Press `8` → voicemail check
- [ ] Press `9` → AGI script runs (check `asterisk -rvvv` output)
- [ ] Press `*` → ARI Stasis app receives `StasisStart` event

### 2.4 Voicemail test

- [ ] Call in → press voicemail option → leave message
- [ ] Confirm `.wav` file in `asterisk_spool` volume

---

## Phase 3 — Enhancements

### 3.1 Custom IVR audio

- Record custom prompts (WAV, 8kHz, mono, signed 16-bit PCM)
- Drop files into `asterisk/etc/asterisk/sounds/custom/`
- Update `extensions.conf` `Background()` calls to use custom sounds
- Test playback with `asterisk -rx "channel originate PJSIP/1001 application Playback custom/welcome"`

### 3.2 HT812 API enhancements

- [ ] `GET /ht812/backups` — list all saved backup files
- [ ] `POST /ht812/restore` — upload a saved XML config to the device
- [ ] `GET /ht812/status/sip-log` — proxy `/cgi-bin/api-get_sip` (live SIP trace)
- [ ] Auto-backup cron: call `/ht812/config` once daily, keep last 30

### 3.3 ARI app enhancements

- [ ] Multi-level IVR menus (sub-menus via nested event loops)
- [ ] TTS playback integration (pass text to a TTS service, play resulting audio)
- [ ] Call recording: start/stop via ARI `POST /channels/{id}/record`
- [ ] Bridge two channels for attended transfer

### 3.4 AGI enhancements

- [ ] SQLite/Redis lookup for caller-specific routing rules
- [ ] HTTP call to external API for dynamic routing decisions
- [ ] VIP caller list loaded from env/config file

---

## Phase 4 — Observability

- [ ] Structured JSON logging in `ht812_api` via `structlog`
- [ ] Prometheus metrics endpoint (`/metrics`) on `ht812_api`
  - API call latency, login failures, backup file count
- [ ] Asterisk AMI event streamer → push to log aggregator
- [ ] Docker Compose `healthcheck` for all three services

---

## Phase 5 — Security Hardening

- [ ] TLS on `ht812_api` (nginx reverse proxy with Let's Encrypt or self-signed)
- [ ] API key authentication on `ht812_api` (Bearer token middleware)
- [ ] Asterisk AMI restricted to 127.0.0.1 (not exposed to host)
- [ ] `.env` secrets migrated to Docker secrets or Vault
- [ ] RTP port range locked down to minimum needed (`10000-10020` for 2 concurrent calls)
- [ ] Asterisk `pjsip.conf` — `allow_unauthenticated_options=no`

---

## Phase 6 — Multi-ATA / Multi-Site

- [ ] Parameterize `ht812_client.py` to support multiple HT812 devices
- [ ] `GET /ht812/{device_id}/config` — per-device config management
- [ ] Asterisk PJSIP wizard config for easier multi-endpoint scaling
- [ ] Config template system: push standard P-value profiles to multiple devices

---

## Known Issues / Technical Debt

| Issue | Priority | Notes |
|-------|----------|-------|
| P34/P734 SIP auth password cannot be written via CGI API | **Critical** | API returns `success` but silently ignores password fields. Must set via web UI (https://192.168.0.160 → Account → FXS Port 1/2 → SIP Auth Password). One-time manual step. |
| `/status/portStatus` response schema unknown | High | Need to log real response once auth is working |
| `ari_app` has no retry on ARI HTTP errors | Medium | Add exponential backoff on `_call()` |
| AGI `wait_digit()` ASCI code parsing fragile | Low | Asterisk returns raw ASCII; add proper parser |
| `pjsip.conf` passwords baked in | Low | Migrate to env-based injection |

---

## HT812V2 Firmware API Reference (3.7.5)

Reverse-engineered from `/assets/js/app.1770202008296.js` on 2026-05-29:

### Auth

```
POST /cgi-bin/dologin
Content-Type: application/x-www-form-urlencoded

username=admin&P2=<password>

→ {"response":"success","body":{"role":"admin","session_token":"<token>","default_auth":"false","oem_id":"0"}}
→ {"response":"error","body":"remain<N>"}   (wrong password, N attempts left before lockout)
```

### Session token

Appended to every request:
- POST body: `&session_token=<token>`
- GET query: `?session_token=<token>&_nocache_=<epoch_ms>`

### Endpoints

| Method | Path | Params | Response |
|--------|------|--------|----------|
| POST | `/cgi-bin/dologout` | — | `{response,body}` |
| POST | `/cgi-bin/api.values.get` | `request=P47,P48,...` | `{response,body:{P47:...}}` |
| POST | `/cgi-bin/api.values.post` | `P47=val&update=1` or `apply=1` | `{response,body}` |
| GET | `/cgi-bin/download_cfg_xml` | — | XML config file |
| GET | `/cgi-bin/export_cfg` | — | Binary config |
| GET | `/cgi-bin/download_cfg` | — | Binary config (alt) |
| POST | `/cgi-bin/rs` | — | `{response,body}` — reboot |
| POST | `/cgi-bin/unit_reset` | `reset_type=0\|1\|2` | `{response,body}` |
| GET | `/status/portStatus` | — | JSON SIP port status |
| GET | `/status/systemInfo` | — | JSON system info |
| GET | `/status/netStatus` | — | JSON network status |
| POST | `/cgi-bin/api-get_apply_status` | — | Whether pending apply needed |
| POST | `/cgi-bin/api-get_system_base_info` | — | Minimal system info |
| POST | `/cgi-bin/api-get_sessioninfo` | — | Current session details |
| POST | `/cgi-bin/api-phone_operation` | `cmd=extend&arg=` | Extend session |
| GET | `/cgi-bin/api-get_sip` | — | Live SIP log |
| GET | `/cgi-bin/download_sip` | — | SIP log file download |

### Factory reset types

| `reset_type` | Effect |
|-------------|--------|
| `0` | ISP data reset |
| `1` | VoIP data reset |
| `2` | Full factory reset |

### Key P-values (verified against firmware 3.7.5 via JS bundle + live device)

**FXS Port 1 SIP (Port 2 = add 700 to P-number, or use P2312/P2313 for server):**

| P-value | Description | Values |
|---------|-------------|--------|
| P47 | SIP server hostname/IP | e.g. `192.168.0.100` |
| P48 | SIP server port | `5060` |
| P35 | SIP User ID (From header) | `1001` |
| P36 | SIP Auth ID (credential username) | `1001` |
| P34 | SIP Auth Password | **Write-only. API returns success but ignores value. Set via web UI only.** |
| P130 | SIP Transport | `0`=UDP, `1`=TCP, `2`=TLS (per JS bundle `opts` — confirmed) |
| P46 | SIP Registration Expiry (s) | `60`–`3600` |
| P40 | Local SIP listen port | `5060` |
| P52 | NAT Traversal | `0`=No, `1`=STUN, `2`=Keep-Alive, `3`=UPnP, `4`=Auto, `5`=VPN |
| P4901 | Hook state | `On Hook` / `Off Hook` |
| P4921 | SIP Registration state | `Registered` / `Not Registered` |

**FXS Port 2 equivalents:** P735/P736/P734/P830/P746/P740/P4902/P4922 / server: P2312/P2313

**Session token rotation:** every `apply=1` response includes `body.token` = new session token. Must capture and use it for subsequent calls or next request returns "invalid session".

**`api.values.get` quirk:** only the LAST `request=` field is processed when multiple are sent. Call once per key and merge results.

**`noInit:1` password fields** (P34, P734, P8536, etc.): web UI never pre-populates them; submitting empty skips the field. API writes return success but value is not stored — firmware enforces write-only password handling at the CGI level.

**P5001/P5002/P5003:** UCM (Grandstream UCM6xxx) auto-provisioning settings. `P5001=1` enables UCM discovery; does NOT affect SIP server used for registration (P47/P2312 still apply). Safe to leave as-is.

**P6767:** Firmware upgrade protocol — `0`=TFTP, `1`=HTTP, `2`=HTTPS (default), `3`=FTP, `4`=FTPS. Not related to SIP transport.

---

## Useful Asterisk CLI Commands

```bash
# Check PJSIP endpoints
pjsip show endpoints
pjsip show aors
pjsip show auths

# Dialplan
dialplan show ivr-main
dialplan show from-ht812

# Make a test call
channel originate PJSIP/1001 extension s@ivr-main

# Voicemail
voicemail show mailboxes default

# Reload without restart
core reload
pjsip reload
dialplan reload
module reload app_voicemail.so
```
