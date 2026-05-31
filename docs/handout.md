# HT812 + Asterisk IVR — Setup, Protocol Monitoring & Debugging Handout

A complete record of the work done to connect a factory-reset Grandstream HT812V2
ATA to a Dockerized Asterisk PBX, build a live protocol-monitoring dashboard, and
debug SIP registration. Read this end-to-end before touching the device — several
steps are order-sensitive and the HT812 has a punishing login lockout.

---

## 1. Topology & Network Layout

```
  ┌─────────────────────────────┐         ┌──────────────────────────┐
  │  MacBook (host)             │         │  Grandstream HT812V2     │
  │                             │         │  (direct-LAN mode)       │
  │  USB 10/100 LAN  en7        │  Cat5   │                          │
  │  192.168.2.2/24  ──────────────────────▶  NET2 (LAN) 192.168.2.1 │
  │                             │         │  NET1 (WAN) — unused     │
  │  Wi-Fi en0  192.168.0.252   │         │  FXS1 → analog phone     │
  │                             │         │  FXS2 → analog phone     │
  │  ┌───────────────────────┐  │         └──────────────────────────┘
  │  │ Docker Desktop        │  │
  │  │  asterisk   :5060/8088 │  │   host.docker.internal:18443
  │  │  ht812_api  :8000      │──┼──────────┐  (TCP proxy)
  │  │  ari_app               │  │          ▼
  │  │  web        :3000      │  │   ht812_proxy.py → 192.168.2.1:443
  │  └───────────────────────┘  │
  └─────────────────────────────┘
```

- **Mac USB LAN adapter (en7):** static `192.168.2.2 / 255.255.255.0`
- **HT812 LAN (NET2):** `192.168.2.1` (its default DHCP/router address)
- **WAN (NET1):** intentionally unconnected during configuration
- **No internet** while in this direct-LAN mode (so Docker image pulls and `brew`
  will fail — that's expected).

---

## 2. Initial Problem: "This site can't be reached" (192.168.2.1)

**Symptom:** After factory-resetting the HT812, the browser timed out
(`ERR_CONNECTION_TIMED_OUT`) reaching `192.168.2.1`.

**Root cause:** The Mac's USB LAN adapter had a **self-assigned IP**
(`169.254.48.188`) — DHCP from the HT812 never completed, so the Mac and device
were on different subnets.

**Fix:** Set a manual static IP on the USB LAN adapter:

- System Settings → Network → USB 10/100 LAN → Details → TCP/IP
- Configure IPv4: **Manually**
- IP `192.168.2.2`, Mask `255.255.255.0`, Router `192.168.2.1`

Verify: `ping -c 4 192.168.2.1` (note: the HT812 often **blocks ICMP** even when
healthy — prefer `curl -sk https://192.168.2.1/ -o /dev/null -w "%{http_code}"`,
which should print `200`).

> A `169.254.x.x` address always means "DHCP failed — no server answered."

---

## 3. What Was Built

### 3.1 Backend — `ht812_api/` (FastAPI)

| File | Purpose |
|------|---------|
| `ht812_client.py` | Async HTTP client for the HT812 CGI API (login, P-value get/set, reboot, config XML, snapshot). |
| `fxs_poller.py` | **New.** Polls FXS hook/registration state every 2 s, emits `fxs_hook` events on transitions, with exponential backoff on errors. |
| `router.py` | REST endpoints (status, provision, snapshot, force-register, reboot, factory-reset). |
| `main.py` | App wiring, scheduler, CORS, global exception handler. |
| `events.py` / `events_router.py` | In-memory event store + SSE stream (`/events/stream`). |
| `models.py` | Pydantic request/response models. |

**Key endpoints**

- `GET  /ht812/status/summary` — combined FXS1/FXS2 status for the dashboard.
- `POST /ht812/provision/two-line` — write standard SIP P-values (UDP/TCP/TLS).
- `POST /ht812/snapshot-backup` — save a timestamped XML config snapshot.
- `POST /ht812/force-register?transport=udp|tcp|tls` — **debug tool.** Writes
  *every* SIP-related P-value (both legacy direct system **and** the firmware-3.7.5
  profile system) then reads them all back so you can see exactly what stuck.
- `GET  /events/stream` — Server-Sent Events feed of DTMF, hook, route, provision.

### 3.2 Frontend — `web/src/App.tsx` (React + Vite)

Three tabs:

1. **Setup** — line-registration status cards, Provisioning panel (with a
   **UDP / TCP / TLS transport picker** and a **Force Register (debug)** button
   that renders a full P-value readback table), and Snapshots list.
2. **Protocol** — per-line DTMF keypad (last 5 keys highlighted live), DTMF
   sequence, and FXS hook-state indicator with the underlying P-value codes.
3. **Timeline** — live SSE event feed with per-line filter and type-colored icons.

Every API call and SSE event is `console.log`'d with a `[PBX]` prefix
(`console.table` for the force-register readback) so the browser console is a
live debug log.

### 3.3 Test scripts — `scripts/test_protocol_simulation/`

| Script | What it does |
|--------|--------------|
| `provision_ht812.py` | Writes two-line SIP/TCP config to the HT812 + verifies readback. |
| `fxs_monitor.py` | Real-time FXS hook/reg monitor with ASCII phone diagram; auto re-logins on session expiry. |
| `watch_events.py` | Tails the `/events/stream` SSE feed with colored, icon-tagged output. |
| `send_dtmf_to_ivr.py` | Injects DTMF digits into a live ARI channel. |
| `simulate_call_flow.py` | Originates a test call into the IVR and walks a DTMF scenario (support/sales/operator/main). |
| `_env.py` | Auto-loads project `.env` into `os.environ` for all scripts. |
| `setup_venv.sh` | Creates `.venv` and installs `httpx` + `websockets`. |

### 3.4 Proxy — `scripts/ht812_proxy.py`

A raw TCP passthrough: `host:18443 → 192.168.2.1:443`. **Required** because
Docker Desktop on macOS cannot route to the `192.168.2.x` USB-LAN segment — only
the host can. Containers reach the device via `host.docker.internal:18443`.

---

## 4. The HT812 CGI API (Reverse-Engineered, Firmware 3.7.5)

Vue SPA frontend over `lighttpd`. Verified live against device `EC74D7F6602A`.

**Auth flow**

```
POST /cgi-bin/dologin   username=admin&P2=<base64(password)>
  → { response:"success", body:{ session_token, ... } }
```

- Password is **base64-encoded** (plaintext = `Mitake123`).
- `session_token` appended to every call: POST `&session_token=<tok>`,
  GET `?session_token=<tok>&_nocache_=<epoch_ms>`.
- The token **rotates on every `apply=1` commit** — capture the new one from the
  response `body.token` or you'll get "invalid session".

**Core endpoints**

| Endpoint | Purpose |
|----------|---------|
| `POST /cgi-bin/api.values.get`  `request=P47&session_token=…` | Read one P-value (only the **last** `request=` is honored — read one at a time). |
| `POST /cgi-bin/api.values.post` `P47=val&…&apply=1` | Write P-values (`apply=1` commits, `update=1` stages). |
| `GET  /cgi-bin/download_cfg_xml` | Full XML config (~1880 P-values). |
| `POST /cgi-bin/upload_cfg`  (multipart `cfg_file`) | Import `.txt`/`.xml` config (accepts both). |
| `POST /cgi-bin/restore_cfg` (multipart `cfg_file`) | Restore `.xml` only — **stricter, rejected our file as "invalid file"; use `upload_cfg`.** |
| `POST /cgi-bin/rs` | Reboot (~30 s downtime). |
| `POST /cgi-bin/unit_reset` `reset_type=0\|1\|2` | Factory reset (0=ISP, 1=VoIP, 2=full). |
| `GET  /cgi-bin/api-get_sip` | Live SIP trace log (empty if device sent no SIP). |

### 4.1 ⚠️ The LOGIN LOCKOUT — THE #1 RECURRING PROBLEM (read this twice)

After **5 failed logins**, the device locks (`remain<N>` counts down attempts
left). First lock is **5 minutes**; repeated triggering **escalates to 15
minutes**.

**This was the actual root cause of nearly every "device unreachable" symptom
in this project.** The password (`Mitake123`) was correct the whole time —
connections failed because the device was *locked out*, not misconfigured.

**Why it kept happening — the HT812 allows only ONE session** and counts
failed/competing logins aggressively. We had **multiple clients hitting the
login endpoint at once**:

1. The `fxs_poller` in the `ht812_api` container (re-logins on session expiry)
2. Manual Python debug scripts (each opens its own login)
3. `fxs_monitor.py`
4. The browser login

When several sessions compete, logins fail → counter climbs → lockout. A locked
device returns `200` on `GET /` (login page loads) but **every `dologin` returns
`remain<N>`**, which looks exactly like a wrong password or a dead device.

**Architectural fix applied — "single session owner" pattern:**

`ht812_api` is now the **sole** authenticator. Everything else reads through it,
so there is only ever one device session:

1. **Serialized, single-flight auth in `HT812Client`.** One `asyncio.Lock`
   wraps the entire auth+request cycle. The poller and API handlers can no longer
   race into two concurrent `dologin` calls (the actual cause of failed logins).
   At most one login is ever in flight.
2. **Lazy auth + retry-once.** The client no longer validates the session on
   every call (that was an extra request each time). It uses the token
   optimistically and re-logs-in exactly once on an "invalid session" reply
   (e.g. if the browser evicted it). Fewer logins = fewer chances to thrash.
3. **`fxs_monitor.py` reads through the API** (`GET /ht812/status/ports`) instead
   of logging into the device. Run as many monitors as you want — none touch the
   device login. A `--direct` flag still exists for bring-up before Docker is up.
4. **`fxs_poller.py`** keeps its exponential backoff (2 s → … → 120 s cap).

**Which tools touch the device login (after the fix):**

| Tool | Authenticates to device? |
|------|--------------------------|
| `ht812_api` (poller + handlers) | **Yes — the one owner**, serialized |
| `fxs_monitor.py` (default) | No — reads the API |
| `watch_events.py` | No — reads the API SSE stream |
| `send_dtmf_to_ivr.py`, `simulate_call_flow.py` | No — talk to **Asterisk ARI** |
| `provision_ht812.py` | Yes — but a deliberate one-shot bootstrap tool |
| Browser web UI | Yes — only when setting passwords; close it after |

**Remaining human rules:**

- The browser web UI is still a separate session (needed for write-only
  passwords). When you must use it, do it briefly and **close the tab after** so
  it doesn't sit competing with the API's session.
- For direct device debugging (`fxs_monitor.py --direct`, `provision_ht812.py`),
  **stop the container first**: `docker compose stop ht812_api`.
- If you ever see `remain<N>`, **STOP all access immediately** and wait out the
  full lock (5 or 15 min). Every further attempt resets the clock.
- The TCP proxy (`ht812_proxy.py`) is safe to leave running — it never logs in.

### 4.2 ⚠️ Write-only password fields

SIP auth passwords are **write-only at the firmware level**:

- Legacy direct system: **P34** (FXS1), **P734** (FXS2)
- Profile system: **P4120** (FXS1), **P4121** (FXS2)

The API write returns `success` but the value is **never readable back** and is
**not in the XML backup**. They effectively **must be set in the HT812 web UI**
(or written blind via API and trusted). `noInit:1` in the JS bundle confirms this.

### 4.3 Human-readable vs numeric P-values

Firmware 3.7.5 returns **strings** for status P-values, not `0`/`1`:

- `P4901`/`P4902` (hook): `"On Hook"` / `"Off Hook"`
- `P4921`/`P4922` (reg):  `"Not Registered"` / `"Registered"`

**Fix applied:** `_norm_hook()` / `_norm_reg()` normalizers in `ht812_client.py`,
`fxs_poller.py`, and `fxs_monitor.py` map both formats to `"0"`/`"1"`.

---

## 5. P-Value Reference

### 5.1 Legacy "direct" SIP account system

| P-value | Meaning | FXS1 | FXS2 |
|---------|---------|------|------|
| SIP User ID | extension | `P35` | `P735` |
| SIP Authenticate ID | | `P36` | `P736` |
| SIP Auth Password (write-only) | | `P34` | `P734` |
| SIP Server | IP/host | `P47` | `P2312` |
| SIP Server Port | | `P48` | `P2313` |
| Transport (0=UDP,1=TCP,2=TLS) | | `P130` | `P830` |
| Registration expiry (s) | | `P46` | `P746` |
| NAT traversal | | `P52` | — |

### 5.2 Firmware-3.7.5 "profile" system (the FXSPort table)

Discovered by decompiling `/assets/js/PortSetting.*.js`. The FXSPort table in the
web UI writes to a **completely different** set of P-values:

```js
// FXS1 = profile row 0, FXS2 = profile row 1
{ sipId:"P4060", auId:"P4090", auPw:"P4120", name:"P4180",
  profileId:"P4150", group:"P4300", url:"P4669", port:"P4595" }
{ sipId:"P4061", auId:"P4091", auPw:"P4121", name:"P4181",
  profileId:"P4151", group:"P4301", url:"P4670", port:"P4596" }
```

| Field | FXS1 | FXS2 |
|-------|------|------|
| SIP User ID | `P4060` | `P4061` |
| Authenticate ID | `P4090` | `P4091` |
| Auth Password (write-only) | `P4120` | `P4121` |
| SIP Server URL | `P4669` | `P4670` |
| Profile / enable | `P4150` | `P4151` |
| Group (FXS binding) | `P4300` | `P4301` |

### 5.3 Status (read-only)

| P-value | Meaning |
|---------|---------|
| `P4901` / `P4902` | Hook state (FXS1/FXS2): `On Hook` / `Off Hook` |
| `P4921` / `P4922` | Registration (FXS1/FXS2): `Not Registered` / `Registered` |
| `P8` | Device mode: `0`=Bridge, `1`=NAT Router |

The `force-register` endpoint writes **both** systems at once to remove ambiguity.
It keeps `P52=2` for NAT keep-alive; transport selection is handled by
`P130`/`P830`.

---

## 6. Asterisk (PJSIP) Configuration

`asterisk/etc/asterisk/pjsip.conf` defines endpoints `1001` and `1002`:

- Both transports listen: `transport-tcp` and `transport-udp` on `0.0.0.0:5060`.
- Auth passwords were set to **`Mitake123`** (replacing the
  `CHANGEME_SIP_*_PASS` placeholders) to match the HT812 side.
- Reload without restart: `docker exec asterisk asterisk -rx "module reload res_pjsip.so"`.

Useful checks:

```bash
docker exec asterisk asterisk -rx "pjsip show endpoints"   # 1001/1002 state
docker exec asterisk asterisk -rx "pjsip show contacts"    # registered contacts
docker exec asterisk asterisk -rx "pjsip set logger on"    # live SIP trace
```

`extensions.conf` routes inbound FXS calls into `ivr-main` (DTMF menu), with
`9`→AGI IVR and `*`→ARI Stasis app (`ivr-app`).

---

## 7. SIP Registration Debugging Journey (chronological)

The lines would not register. Each hypothesis tested and the result:

1. **Self-signed IP / wrong subnet** → fixed with static `192.168.2.2`. ✅
2. **API 500 on `/status/summary`** → `httpx.ConnectTimeout`: Docker couldn't
   reach `192.168.2.1`. → Built `ht812_proxy.py` and pointed `HT812_HOST` at
   `host.docker.internal:18443`. ✅ (status endpoint works)
3. **Login lockout (`remain4`)** → fxs_poller hammering re-login → added
   exponential backoff. ✅
4. **Hook showing `?='On Hook'`** → firmware returns strings → added normalizers. ✅
5. **Passwords set in UI but no SIP** → wrong P-values: the FXSPort UI table uses
   the **profile system** (P4060/P4090/P4669), not the legacy P35/P47. → Wrote
   both via force-register. ⚠️ still no SIP traffic.
6. **Device mode** → checked `P8`; toggled Bridge(0)↔NAT(1). Working backup had
   `P8=1`. ⚠️ still no SIP.
7. **`host.docker.internal` written as SIP server** → when the dashboard "Apply"
   button ran, the backend default `ASTERISK_SIP_HOST` wrote
   `host.docker.internal` (unresolvable by the HT812) into `P47`. → Added
   `ASTERISK_SIP_HOST=192.168.2.2` to `.env`; re-provisioned `P47=192.168.2.2`. ✅
   (server corrected) — this was a genuine bug, but registration still pending.
8. **HT812 SIP log empty** (`api-get_sip` → `exist:false`) the whole time → the
   device sent **zero** SIP packets, confirming the blocker was always device-side
   config, not the network or Asterisk.
9. **Config restore** → patched a known-good backup XML (`P47`/`P2312` →
   `192.168.2.2`) and uploaded via `upload_cfg` → `success, needreboot:1` → reboot.
10. **Device went physically unreachable** — ARP for `192.168.2.1` shows
    `(incomplete)`: no L2 reply. **Open item — requires physical power-cycle.**

### Current open item

The HT812 stopped answering even ARP after the last reboot. This is physical-layer:

- Confirm Power LED solid; confirm link light on the **NET2 (LAN)** port.
- 30-second power cycle (unplug power, wait 30 s, replug).
- When back: `curl -sk https://192.168.2.1/ -o /dev/null -w "%{http_code}"` → `200`.
- Then in the dashboard: **Force Register (TCP)** and read the P4921/P4922 rows in
  the debug table.

---

## 8. Bug Fixes Applied to the Code

| Area | Bug | Fix |
|------|-----|-----|
| `fxs_poller.py` | Re-login storm tripped the 5-attempt lockout | Exponential backoff (2→120 s); ignore empty/error reads |
| `ht812_client.py` `_norm_*` | Firmware returns "On Hook"/"Registered" strings | Normalizers → `"0"`/`"1"` |
| `ht812_client.py` `_login()` | `ConnectError` bubbled as 500 | Wrapped in `HT812Error` (→ clean 502) |
| `main.py` | 500 responses had **no CORS header** → browser saw opaque CORS error | Global `@app.exception_handler(Exception)` returns JSON 502 **with** `Access-Control-Allow-Origin` |
| `.env` | `host.docker.internal` written into the device's SIP server field | Added `ASTERISK_SIP_HOST=192.168.2.2` |
| `router.py` / `main.py` | Stale `get_config_xml` name | Renamed to `save_config_snapshot` everywhere |
| `force-register` | Transport hardcoded to UDP | `?transport=udp\|tcp\|tls` query param + UI picker |

---

## 9. Operational Runbook

### Bring everything up (in this order)

```bash
# 1. HT812 reachable? (USB LAN = 192.168.2.2, device = 192.168.2.1)
curl -sk https://192.168.2.1/ -o /dev/null -w "%{http_code}\n"   # want 200

# 2. Start the TCP proxy (REQUIRED before Docker can reach the device)
#    If "address already in use":  lsof -ti :18443 | xargs kill -9
python3 scripts/ht812_proxy.py &

# 3. Bring up the stack (recreate, not just restart, to re-read .env)
docker compose up -d

# 4. Confirm the API can reach the device
curl -s http://localhost:8000/ht812/status/summary | python3 -m json.tool
```

> **Gotcha:** `docker compose restart` does **not** re-read `.env`. After editing
> `.env`, use `docker compose up -d <service>` to recreate the container. When
> the image can't be rebuilt (no internet in direct-LAN mode), hot-patch files
> with `docker cp ht812_api/<file>.py ht812_api:/app/<file>.py && docker restart ht812_api`.

### Run the monitoring scripts

```bash
source scripts/test_protocol_simulation/.venv/bin/activate    # one-time: bash .../setup_venv.sh
python scripts/test_protocol_simulation/fxs_monitor.py        # hook/reg monitor
python scripts/test_protocol_simulation/watch_events.py       # SSE event tail
python scripts/test_protocol_simulation/simulate_call_flow.py --scenario support
```

### Set SIP passwords (manual, required)

1. Browse to `https://192.168.2.1` → login `admin` / `Mitake123`.
2. FXS Port 1 → **SIP Authentication Password** → `Mitake123` → Save & Apply.
3. FXS Port 2 → same → `Mitake123` → Save & Apply.

### Confirm registration

```bash
docker exec asterisk asterisk -rx "pjsip show contacts"
# Want: 1001/sip:… Reachable  and  1002/sip:… Reachable
```

---

## 10. Credentials & Key Facts (quick reference)

| Item | Value |
|------|-------|
| HT812 admin login | `admin` / `Mitake123` |
| HT812 LAN IP | `192.168.2.1` (HTTPS, self-signed cert) |
| Mac USB LAN IP | `192.168.2.2 / 255.255.255.0` |
| HT812 password as base64 (P2) | `TWl0YWtlMTIz` |
| Device MAC | `EC74D7F6602A` |
| Proxy | `host:18443 → 192.168.2.1:443` |
| `HT812_HOST` (.env) | `https://host.docker.internal:18443` |
| `ASTERISK_SIP_HOST` (.env) | `192.168.2.2` |
| SIP extensions | `1001` (FXS1), `1002` (FXS2), pass `Mitake123` |
| Lockout | 5 failed logins → 5-minute lock |
| Dashboard | `http://localhost:3000` · API `http://localhost:8000` |

---

## 11. Known Gotchas (memorize these)

1. **Proxy must run before Docker** can reach the device; it dies silently —
   re-check with `lsof -i :18443`.
2. **`docker compose restart` ≠ reload `.env`.** Use `up -d` to recreate.
3. **Login lockout is brutal** — never poll-login in a tight loop.
4. **Passwords are write-only** — can't verify via API or XML; set in the web UI.
5. **Status P-values are strings**, not 0/1 — always normalize.
6. **Session token rotates on every `apply=1`** — re-capture it.
7. **Two parallel SIP config systems** (legacy P35/P47 vs profile P4060/P4669) —
   the UI table uses the profile system; write both to be safe.
8. **HT812 blocks ICMP** — use `curl`, not `ping`, to test reachability.
9. **No internet in direct-LAN mode** — image pulls/`brew` will fail; hot-patch
   containers with `docker cp` instead of rebuilding.
10. **`restore_cfg` is stricter than `upload_cfg`** — use `upload_cfg` for XML.
