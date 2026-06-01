import { useEffect, useMemo, useState } from "react";
import {
  Activity, ArrowRight, Cable, CheckCircle2, ClipboardList,
  Hash, Phone, PhoneCall, PhoneOff, Radio, RefreshCw,
  Server, Settings2, TriangleAlert, Zap,
} from "lucide-react";
import { createRoot } from "react-dom/client";
// Ignore missing type declarations for CSS side-effect import
// @ts-ignore
import "./styles.css";

// Provide ImportMeta typing for Vite env to satisfy TypeScript
declare global {
  interface ImportMetaEnv {
    readonly VITE_API_BASE_URL?: string;
  }

  interface ImportMeta {
    readonly env: ImportMetaEnv;
  }
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

type PortStatus = {
  port: number;
  hook: string;
  registered: boolean;
  user_id: string;
  sip_server: string;
  sip_port: string;
};

type Summary = {
  expected: {
    transport: string;
    sip_port: string;
    extensions: string[];
    manual_password_fields: string[];
  };
  ports: {
    port1: PortStatus;
    port2: PortStatus;
    raw: Record<string, string>;
  };
};

type AsteriskEndpoint = {
  state: string;
  registered: boolean;
  channel_count: number;
  port: number;
};

type RegistrationAudit = {
  verdict: string;
  snapshot_source?: string;
  device_offline?: boolean;
  device: {
    registered: boolean;
    fxs1_registration_raw?: string | null;
    fxs2_registration_raw?: string | null;
    sip_trace_state?: string;
  };
  asterisk: {
    reachable: boolean;
    both_registered: boolean;
    endpoints: Record<string, AsteriskEndpoint>;
    error?: string | null;
  };
  sip_log: {
    found: boolean;
    offline: boolean;
    empty?: boolean | null;
    raw?: string;
  };
};

type AuditResponse = {
  audit: RegistrationAudit;
  offline?: boolean;
  snapshot_source?: string;
  live_error?: { message: string } | null;
};

type CommunicationEvent = {
  id: string;
  created_at: string;
  source: string;
  type: string;
  message: string;
  channel_id?: string | null;
  caller?: string | null;
  line?: string | null;
  digit?: string | null;
  data: Record<string, unknown>;
};

type Tab = "setup" | "protocol" | "timeline";
type LineFilter = "all" | "1" | "2";

const DTMF_KEYS = [
  ["1", "2", "3"],
  ["4", "5", "6"],
  ["7", "8", "9"],
  ["*", "0", "#"],
] as const;

function normalizeLineNum(line: string | null | undefined): "1" | "2" | null {
  if (!line) return null;
  if (line === "1" || line === "1001") return "1";
  if (line === "2" || line === "1002") return "2";
  return null;
}

function App() {
  const [tab, setTab] = useState<Tab>("setup");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [events, setEvents] = useState<CommunicationEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [provisioning, setProvisioning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<"connecting" | "open" | "closed">("connecting");
  const [lineFilter, setLineFilter] = useState<LineFilter>("all");
  const [audit, setAudit] = useState<AuditResponse | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [forcing, setForcing] = useState(false);

  const allRegistered = Boolean(summary?.ports.port1.registered && summary?.ports.port2.registered);

  async function loadSummary() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/ht812/status/summary`);
      if (!res.ok) throw new Error(await res.text());
      setSummary(await res.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load status");
    } finally {
      setLoading(false);
    }
  }

  // load backup
  async function loadBackup(){}

  async function loadAudit() {
    setAuditLoading(true);
    try {
      const res = await fetch(`${API_BASE_URL}/ht812/status/audit?transport=tcp&live=true`);
      if (!res.ok) throw new Error(await res.text());
      setAudit(await res.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load registration audit");
    } finally {
      setAuditLoading(false);
    }
  }

  async function forceRegister() {
    setForcing(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/ht812/force-register?transport=tcp`, { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      await Promise.all([loadSummary(), loadAudit()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Force-register failed");
    } finally {
      setForcing(false);
    }
  }

  async function provisionTwoLine() {
    setProvisioning(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/ht812/provision/two-line`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transport: "tcp", sip_port: "5060" }),
      });
      if (!res.ok) throw new Error(await res.text());
      await loadSummary();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to provision HT812");
    } finally {
      setProvisioning(false);
    }
  }

  useEffect(() => { loadSummary(); loadAudit(); }, []);

  useEffect(() => {
    const source = new EventSource(`${API_BASE_URL}/events/stream`);
    source.onopen = () => setStreamState("open");
    source.onerror = () => setStreamState("closed");
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as CommunicationEvent;
      setEvents((current) => {
        const next = [...current.filter((item) => item.id !== event.id), event];
        return next.slice(-120);
      });
    };
    return () => source.close();
  }, []);

  const latestEvents = useMemo(() => [...events].reverse(), [events]);

  // Per-line protocol state derived from the live event stream
  const lineProtocol = useMemo(() => {
    const blank = () => ({
      hookLabel: "unknown", hookState: "", dtmfSeq: [] as string[], registered: false,
      lastForwarded: null as string | null,
      lastLiveDigit: null as string | null, lastLiveAt: null as string | null,
      liveCount: 0,
    });
    const state: Record<"1" | "2", LineProtocol> = { "1": blank(), "2": blank() };

    for (const ev of events) {
      const ln = normalizeLineNum(ev.line);
      if (!ln) continue;
      if (ev.type === "fxs_hook") {
        state[ln].hookLabel = (ev.data.hook_label as string) || "unknown";
        state[ln].hookState = (ev.data.hook_state as string) || "";
      }
      if (ev.type === "dtmf" && ev.digit) {
        state[ln].dtmfSeq = [...state[ln].dtmfSeq, ev.digit].slice(-30);
        if (ln === "2" && ev.data.forwarded_from === "1") {
          state["2"].lastForwarded = ev.digit;
        }
        // A LIVE digit is a real one from the telephony path (ari_app), not a
        // web simulation. This is the evidence that the line actually keyed in.
        const isLive = ev.source === "ari_app" && ev.data.simulated !== true;
        if (isLive) {
          state[ln].lastLiveDigit = ev.digit;
          state[ln].lastLiveAt = ev.created_at;
          state[ln].liveCount += 1;
        }
      }
    }

    if (summary) {
      state["1"].registered = summary.ports.port1.registered;
      state["2"].registered = summary.ports.port2.registered;
    }
    return state;
  }, [events, summary]);

  const filteredEvents = useMemo(() => {
    if (lineFilter === "all") return latestEvents;
    return latestEvents.filter((ev) => normalizeLineNum(ev.line) === lineFilter);
  }, [latestEvents, lineFilter]);

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">HT812 + Asterisk IVR</p>
          <h1>PBX Communication Monitor</h1>
        </div>
        <div className={`status-pill ${allRegistered ? "ok" : "warn"}`}>
          {allRegistered ? <CheckCircle2 size={18} /> : <TriangleAlert size={18} />}
          {allRegistered ? "Both lines registered" : "Registration pending"}
        </div>
      </header>

      <nav className="tabs" aria-label="Views">
        <button className={tab === "setup" ? "active" : ""} onClick={() => setTab("setup")}>
          <ClipboardList size={18} />
          Setup
        </button>
        <button className={tab === "protocol" ? "active" : ""} onClick={() => setTab("protocol")}>
          <Hash size={18} />
          Protocol
        </button>
        <button className={tab === "timeline" ? "active" : ""} onClick={() => setTab("timeline")}>
          <Activity size={18} />
          Timeline
        </button>
      </nav>

      {error && <div className="error"><TriangleAlert size={18} />{error}</div>}

      {tab === "setup" && (
        <section className="layout">
          <div className="panel">
            <div className="panel-head">
              <div>
                <h2>Line Registration</h2>
                <p>FXS 1 and FXS 2 register to Asterisk over SIP/TCP.</p>
              </div>
              <button className="icon-button" onClick={loadSummary} disabled={loading} title="Refresh status">
                <RefreshCw size={18} className={loading ? "spin" : ""} />
              </button>
            </div>
            <div className="line-grid">
              <LinePanel port={summary?.ports.port1} label="FXS Port 1" expected="1001" />
              <LinePanel port={summary?.ports.port2} label="FXS Port 2" expected="1002" />
            </div>
          </div>

          <div className="panel side">
            <h2>Provisioning</h2>
            <dl className="kv">
              <div><dt>SIP transport</dt><dd>TCP</dd></div>
              <div><dt>SIP port</dt><dd>5060</dd></div>
              <div><dt>Password fields</dt><dd>P34, P734</dd></div>
              <div><dt>API</dt><dd>{API_BASE_URL}</dd></div>
            </dl>
            <button className="primary" onClick={provisionTwoLine} disabled={provisioning}>
              <Cable size={18} />
              {provisioning ? "Applying..." : "Apply two-line settings"}
            </button>
            <p className="note">
              SIP auth passwords must still be entered in the HT812 web UI for both FXS ports.
            </p>
          </div>

          <AuditPanel
            audit={audit}
            loading={auditLoading}
            forcing={forcing}
            onRefresh={loadAudit}
            onForce={forceRegister}
          />
        </section>
      )}

      {tab === "protocol" && (
        <section className="protocol-layout">
          <LineDTMFPanel lineNum="1" protocol={lineProtocol["1"]} portStatus={summary?.ports.port1} />
          <LineDTMFPanel lineNum="2" protocol={lineProtocol["2"]} portStatus={summary?.ports.port2} />
        </section>
      )}

      {tab === "timeline" && (
        <section className="panel">
          <div className="panel-head">
            <div>
              <h2>Live Communication Process</h2>
              <p>DTMF digits, FXS hook changes, SIP routing, and provisioning events.</p>
            </div>
            <div className={`stream ${streamState}`}>
              <Radio size={17} />
              {streamState}
            </div>
          </div>
          <div className="filter-bar">
            <span>Line:</span>
            {(["all", "1", "2"] as LineFilter[]).map((f) => (
              <button
                key={f}
                className={`filter-btn ${lineFilter === f ? "active" : ""}`}
                onClick={() => setLineFilter(f)}
              >
                {f === "all" ? "All" : `Line ${f}`}
              </button>
            ))}
          </div>
          <div className="timeline">
            {filteredEvents.length === 0 ? (
              <div className="empty">No communication events yet.</div>
            ) : (
              filteredEvents.map((event) => <EventRow key={event.id} event={event} />)
            )}
          </div>
        </section>
      )}
    </main>
  );
}

const VERDICT_TONE: Record<string, "ok" | "warn" | "bad"> = {
  registered_confirmed_both_sides: "ok",
  registered: "ok",
  asterisk_online_but_device_flag_stale: "warn",
  device_says_registered_but_asterisk_has_no_contact: "bad",
  sip_trace_present_but_not_registered: "bad",
  configured_but_neither_side_registered: "bad",
  neither_side_registered: "bad",
  configured_but_no_register_observed: "warn",
  configured: "warn",
  no_force_register_audit_found: "warn",
};

function AuditPanel({
  audit, loading, forcing, onRefresh, onForce,
}: {
  audit: AuditResponse | null;
  loading: boolean;
  forcing: boolean;
  onRefresh: () => void;
  onForce: () => void;
}) {
  const a = audit?.audit;
  const verdict = a?.verdict ?? "unknown";
  const tone = VERDICT_TONE[verdict] ?? "warn";
  const ast = a?.asterisk;
  const sipRaw = a?.sip_log?.raw?.trim();

  return (
    <div className="panel side">
      <div className="panel-head">
        <div>
          <h2>Registration Audit</h2>
          <p>Three-way: device flag · Asterisk contact · written config.</p>
        </div>
        <button className="icon-button" onClick={onRefresh} disabled={loading} title="Re-run audit">
          <RefreshCw size={18} className={loading ? "spin" : ""} />
        </button>
      </div>

      <div className={`reg ${tone === "ok" ? "ok" : "warn"}`} style={{ marginBottom: 10 }}>
        {verdict.replace(/_/g, " ")}
      </div>

      <dl className="kv compact">
        <div><dt>Snapshot</dt><dd>{audit?.snapshot_source ?? "-"}{audit?.offline ? " (device offline)" : ""}</dd></div>
        <div><dt>Device flags</dt><dd>FXS1={a?.device?.fxs1_registration_raw ?? "?"} · FXS2={a?.device?.fxs2_registration_raw ?? "?"}</dd></div>
        <div><dt>Asterisk</dt><dd>{ast ? (ast.reachable ? (ast.both_registered ? "both online" : "not both online") : "unreachable") : "-"}</dd></div>
        {ast?.endpoints && Object.entries(ast.endpoints).map(([ext, ep]) => (
          <div key={ext}><dt>PJSIP/{ext}</dt><dd>{ep.state} ({ep.channel_count} ch)</dd></div>
        ))}
        {ast?.error && <div><dt>ARI error</dt><dd>{ast.error}</dd></div>}
        <div><dt>SIP trace</dt><dd>{a?.device?.sip_trace_state ?? "-"}</dd></div>
      </dl>

      {sipRaw && (
        <details style={{ marginTop: 8 }}>
          <summary className="section-label">Device SIP trace</summary>
          <pre className="sip-trace">{sipRaw.slice(-4000)}</pre>
        </details>
      )}

      <button className="primary" onClick={onForce} disabled={forcing} style={{ marginTop: 12 }}>
        <Zap size={18} />
        {forcing ? "Force-registering (TCP)…" : "Force-register over TCP"}
      </button>
    </div>
  );
}

function LinePanel({ port, label, expected }: { port?: PortStatus; label: string; expected: string }) {
  const registered = Boolean(port?.registered);
  return (
    <article className="line-panel">
      <div className="line-title">
        <PhoneCall size={20} />
        <div>
          <h3>{label}</h3>
          <p>Extension {expected}</p>
        </div>
      </div>
      <div className={`reg ${registered ? "ok" : "warn"}`}>{registered ? "Registered" : "Not registered"}</div>
      <dl className="kv compact">
        <div><dt>User</dt><dd>{port?.user_id || "-"}</dd></div>
        <div><dt>Server</dt><dd>{port?.sip_server || "-"}</dd></div>
        <div><dt>Port</dt><dd>{port?.sip_port || "-"}</dd></div>
        <div><dt>Hook</dt><dd>{port?.hook || "-"}</dd></div>
      </dl>
    </article>
  );
}

type LineProtocol = {
  hookLabel: string;
  hookState: string;
  dtmfSeq: string[];
  registered: boolean;
  lastForwarded: string | null;
  lastLiveDigit: string | null;
  lastLiveAt: string | null;
  liveCount: number;
};

function LineDTMFPanel({
  lineNum,
  protocol,
  portStatus,
}: {
  lineNum: "1" | "2";
  protocol: LineProtocol;
  portStatus?: PortStatus;
}) {
  const recentDigits = new Set(protocol.dtmfSeq.slice(-5));
  const isOffHook = protocol.hookState === "1" || protocol.hookLabel === "off-hook";
  const hookKnown = protocol.hookLabel !== "unknown";
  const hasLiveEvidence = protocol.liveCount > 0;

  const [mode, setMode] = useState<"simulate" | "live">("simulate");
  const [sending, setSending] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<string | null>(null);

  async function pressKey(digit: string) {
    setSending(digit);
    setLastResult(null);
    try {
      const ep = mode === "simulate" ? "simulate" : "send";
      const res = await fetch(
        `${API_BASE_URL}/dtmf/${ep}?line=${lineNum}&digit=${encodeURIComponent(digit)}`,
        { method: "POST" },
      );
      const body = await res.json();
      if (body.ok) {
        setLastResult(mode === "simulate" ? `simulated ${digit}` : `sent ${digit} → live channel`);
      } else {
        setLastResult(body.reason || body.error || "no active call");
      }
    } catch (err) {
      setLastResult(err instanceof Error ? err.message : "request failed");
    } finally {
      setSending(null);
    }
  }

  return (
    <div className="panel dtmf-panel">
      <div className="panel-head">
        <div>
          <h2>FXS Line {lineNum}</h2>
          <p>Extension 100{lineNum} · {portStatus?.sip_server || "—"} · TCP</p>
        </div>
        <div className="badge-group">
          {hasLiveEvidence && (
            <div
              className="live-evidence-badge"
              title={[
                `LIVE evidence: ${protocol.liveCount} event${protocol.liveCount === 1 ? "" : "s"}`,
                protocol.lastLiveDigit ? `last digit ${protocol.lastLiveDigit}` : null,
                protocol.lastLiveAt ? `last at ${new Date(protocol.lastLiveAt).toLocaleTimeString()}` : null,
              ].filter(Boolean).join(" · ")}
            >
              <Activity size={16} />
              <div className="live-evidence-copy">
                <span>LIVE evidence</span>
                <strong>
                  {protocol.liveCount} event{protocol.liveCount === 1 ? "" : "s"}
                  {protocol.lastLiveDigit ? ` · last ${protocol.lastLiveDigit}` : ""}
                </strong>
              </div>
            </div>
          )}
          <div className={`status-pill ${protocol.registered ? "ok" : "warn"}`}>
            {protocol.registered ? <CheckCircle2 size={16} /> : <TriangleAlert size={16} />}
            {protocol.registered ? "Registered" : "Unregistered"}
          </div>
          {hookKnown && (
            <div className={`status-pill ${isOffHook ? "ok" : ""}`}>
              <Phone size={16} />
              {isOffHook ? "Off-hook" : "On-hook"}
            </div>
          )}
          {lineNum === "2" && protocol.lastForwarded && (
            <div className="status-pill ok" title="Last digit forwarded from Line 1">
              <ArrowRight size={16} />
              {`L1→L2: ${protocol.lastForwarded}`}
            </div>
          )}
        </div>
      </div>

      <div className="protocol-body">
        <div className="keypad-section">
          <div className="keypad-head">
            <p className="section-label">DTMF Keypad</p>
            <div className="mode-toggle" role="group" aria-label="DTMF send mode">
              <button
                className={mode === "simulate" ? "active" : ""}
                onClick={() => setMode("simulate")}
                title="Emit a simulated keypress (no call needed)"
              >Simulate</button>
              <button
                className={mode === "live" ? "active" : ""}
                onClick={() => setMode("live")}
                title="Send a real DTMF digit into the live call (needs an active call)"
              >Live</button>
            </div>
          </div>
          <p className="key-legend">
            {mode === "simulate"
              ? "Click a key to simulate transmitting it"
              : "Click sends a real digit into the active call"}
            {" · last 5 highlighted"}
          </p>
          <div className="keypad">
            {DTMF_KEYS.map((row, ri) => (
              <div key={ri} className="keypad-row">
                {row.map((key) => (
                  <button
                    key={key}
                    className={`keypad-key ${recentDigits.has(key) ? "pressed" : ""} ${sending === key ? "sending" : ""}`}
                    onClick={() => pressKey(key)}
                    disabled={sending !== null}
                  >
                    {key}
                  </button>
                ))}
              </div>
            ))}
          </div>
          {lastResult && <p className={`keypad-result ${mode}`}>{lastResult}</p>}
        </div>

        <div className="seq-section">
          <div className="seq-block">
            <p className="section-label">DTMF Sequence</p>
            <div className="dtmf-seq">
              {protocol.dtmfSeq.length > 0
                ? protocol.dtmfSeq.map((d, i) => (
                    <span
                      key={i}
                      className={`dtmf-digit ${i >= protocol.dtmfSeq.length - 5 ? "recent" : ""}`}
                    >
                      {d}
                    </span>
                  ))
                : <span className="placeholder">No digits received yet</span>
              }
            </div>
          </div>

          <div className="fxs-block">
            <p className="section-label">FXS Hook State</p>
            <div className="fxs-state-row">
              <div className={`fxs-indicator ${isOffHook ? "active" : hookKnown ? "idle" : "unknown"}`} />
              <span className="fxs-label">
                {!hookKnown
                  ? "Awaiting first hook event…"
                  : isOffHook
                  ? "Off-hook — line active"
                  : "On-hook — line idle"}
              </span>
            </div>
            <div className="fxs-codes">
              <div className="fxs-code-row">
                <span className="code-badge">P490{lineNum}</span>
                <span>Hook: <strong>{(portStatus?.hook ?? protocol.hookState) || "—"}</strong></span>
                <span className="code-note">0=on-hook · 1=off-hook</span>
              </div>
              <div className="fxs-code-row">
                <span className="code-badge">P130</span>
                <span>Transport: <strong>1 (TCP)</strong></span>
              </div>
              <div className="fxs-code-row">
                <span className="code-badge">P492{lineNum}</span>
                <span>Registered: <strong>{protocol.registered ? "1 (yes)" : "0 (no)"}</strong></span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function eventIcon(type: string) {
  switch (type) {
    case "dtmf":             return <Hash size={17} />;
    case "fxs_hook":         return <Phone size={17} />;
    case "route":            return <ArrowRight size={17} />;
    case "call_start":
    case "stasis_start":     return <PhoneCall size={17} />;
    case "hangup":
    case "channel_destroyed":return <PhoneOff size={17} />;
    case "provision":        return <Settings2 size={17} />;
    case "bridge":           return <Zap size={17} />;
    case "error":            return <TriangleAlert size={17} />;
    default:                 return <Server size={17} />;
  }
}

function eventIconClass(type: string): string {
  switch (type) {
    case "dtmf":              return "icon-dtmf";
    case "fxs_hook":          return "icon-fxs";
    case "call_start":
    case "stasis_start":      return "icon-call";
    case "hangup":
    case "channel_destroyed": return "icon-hangup";
    case "provision":         return "icon-provision";
    case "bridge":            return "icon-bridge";
    case "route":             return "icon-route";
    default:                  return "";
  }
}

function EventRow({ event }: { event: CommunicationEvent }) {
  const time = new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(event.created_at));

  const lineNum = normalizeLineNum(event.line);

  return (
    <article className={`event-row ${event.type === "fxs_hook" ? "ev-fxs" : ""}`}>
      <div className={`event-icon ${eventIconClass(event.type)}`}>{eventIcon(event.type)}</div>
      <div className="event-main">
        <div className="event-meta">
          <span>{time}</span>
          <span>{event.source}</span>
          <span className={`type-badge type-${event.type.replace(/_/g, "-")}`}>{event.type}</span>
          {event.digit && <span className="dtmf-badge">DTMF <strong>{event.digit}</strong></span>}
          {lineNum && <span>Line {lineNum}</span>}
        </div>
        <p>{event.message}</p>
        {(event.caller || event.channel_id) && (
          <div className="event-detail">
            {event.caller && <span>caller {event.caller}</span>}
            {event.channel_id && <span>ch {event.channel_id.slice(0, 20)}…</span>}
          </div>
        )}
      </div>
    </article>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
