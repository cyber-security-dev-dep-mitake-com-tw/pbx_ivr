import { useEffect, useMemo, useState } from "react";
import {
  Activity, Archive, ArrowRight, Cable, CheckCircle2, ClipboardList,
  DatabaseBackup, Hash, Phone, PhoneCall, PhoneOff, Radio, RefreshCw,
  RotateCcw, Server, Settings2, TriangleAlert, Zap,
} from "lucide-react";
import { createRoot } from "react-dom/client";
import "./styles.css";

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
    write_only_password_fields?: string[];
    manual_password_fields?: string[];
    password_env_available?: Record<string, boolean>;
  };
  ports: {
    port1: PortStatus;
    port2: PortStatus;
    raw: Record<string, string>;
  };
  diagnostics?: Record<string, unknown>;
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

type BackupFile = {
  filename: string;
  size_bytes: number;
  created_at: string;
  path: string;
};

type ForceRegisterResponse = {
  success: boolean;
  message: string;
  sip_server: string;
  sip_port: string;
  transport: string;
  params_written: Record<string, string>;
  readback: Record<string, string>;
  diagnostics?: {
    debug_log_path?: string;
    action?: {
      password_fields_attempted?: string[];
    };
    comparison?: {
      summary?: {
        mismatch_count?: number;
      };
    };
  };
};

type BackupListResponse = {
  count: number;
  backups: BackupFile[];
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
  const [backups, setBackups] = useState<BackupFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [backupLoading, setBackupLoading] = useState(false);
  const [provisioning, setProvisioning] = useState(false);
  const [snapshotSaving, setSnapshotSaving] = useState(false);
  const [forceRegistering, setForceRegistering] = useState(false);
  const [registerDebug, setRegisterDebug] = useState<ForceRegisterResponse | null>(null);
  const [regTransport, setRegTransport] = useState<"udp" | "tcp" | "tls">("udp");
  const [sipServer, setSipServer] = useState("192.168.0.252");
  const [error, setError] = useState<string | null>(null);
  const [backupMessage, setBackupMessage] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<"connecting" | "open" | "closed">("connecting");
  const [lineFilter, setLineFilter] = useState<LineFilter>("all");

  const allRegistered = Boolean(summary?.ports.port1.registered && summary?.ports.port2.registered);
  const selectedSipPort = regTransport === "tls" ? "5061" : "5060";
  const liveTransport = summary?.ports.raw?.P130 === "0"
    ? "UDP"
    : summary?.ports.raw?.P130 === "1"
    ? "TCP"
    : summary?.ports.raw?.P130 === "2"
    ? "TLS"
    : "Unknown";

  async function loadSummary() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/ht812/status/summary`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      console.log("[PBX] summary loaded:", data);
      setSummary(data);
    } catch (err) {
      console.error("[PBX] summary error:", err);
      setError(err instanceof Error ? err.message : "Failed to load status");
    } finally {
      setLoading(false);
    }
  }

  async function loadBackups() {
    setBackupLoading(true);
    try {
      const res = await fetch(`${API_BASE_URL}/ht812/backups`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json() as BackupListResponse;
      setBackups(data.backups);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load snapshots");
    } finally {
      setBackupLoading(false);
    }
  }

  async function createSnapshotBackup() {
    setSnapshotSaving(true);
    setBackupMessage(null);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/ht812/snapshot-backup`, { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      const saved = await res.json() as BackupFile;
      setBackupMessage(`Saved ${saved.filename}`);
      await loadBackups();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save snapshot");
    } finally {
      setSnapshotSaving(false);
    }
  }

  async function provisionTwoLine() {
    setProvisioning(true);
    setError(null);
    try {
      console.log("[PBX] provision: calling /ht812/provision/two-line");
      const res = await fetch(`${API_BASE_URL}/ht812/provision/two-line`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transport: "tcp", sip_port: "5060" }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      console.log("[PBX] provision response:", data);
      await loadSummary();
    } catch (err) {
      console.error("[PBX] provision error:", err);
      setError(err instanceof Error ? err.message : "Failed to provision HT812");
    } finally {
      setProvisioning(false);
    }
  }

  async function forceRegister() {
    setForceRegistering(true);
    setRegisterDebug(null);
    setError(null);
    try {
      console.log(`[PBX] force-register: transport=${regTransport}`);
      const params = new URLSearchParams({
        transport: regTransport,
        sip_server: sipServer.trim(),
        sip_port: selectedSipPort,
        write_passwords: "true",
      });
      const res = await fetch(
        `${API_BASE_URL}/ht812/force-register?${params.toString()}`,
        { method: "POST" },
      );
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json() as ForceRegisterResponse;
      console.log("[PBX] force-register response:", data);
      console.table(data.readback);
      console.log("[PBX] P4921 (FXS1 reg):", data.readback["P4921"]);
      console.log("[PBX] P4922 (FXS2 reg):", data.readback["P4922"]);
      console.log("[PBX] P47  (FXS1 server):", data.readback["P47"]);
      console.log("[PBX] P35  (FXS1 user):", data.readback["P35"]);
      console.log("[PBX] P4060 (profile user):", data.readback["P4060"]);
      console.log("[PBX] P4669 (profile server):", data.readback["P4669"]);
      console.log("[PBX] P8   (device mode):", data.readback["P8"]);
      setRegisterDebug(data);
      await loadSummary();
    } catch (err) {
      console.error("[PBX] force-register error:", err);
      setError(err instanceof Error ? err.message : "Force register failed");
    } finally {
      setForceRegistering(false);
    }
  }

  useEffect(() => {
    loadSummary();
    loadBackups();
  }, []);

  useEffect(() => {
    const source = new EventSource(`${API_BASE_URL}/events/stream`);
    source.onopen = () => setStreamState("open");
    source.onerror = () => setStreamState("closed");
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as CommunicationEvent;
      console.log(`[PBX] event: ${event.type} | ${event.source} | ${event.message}`, event);
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
    const state: Record<"1" | "2", {
      hookLabel: string;
      hookState: string;
      dtmfSeq: string[];
      registered: boolean;
    }> = {
      "1": { hookLabel: "unknown", hookState: "", dtmfSeq: [], registered: false },
      "2": { hookLabel: "unknown", hookState: "", dtmfSeq: [], registered: false },
    };

    for (const ev of events) {
      const ln = normalizeLineNum(ev.line);
      if (!ln) continue;
      if (ev.type === "fxs_hook") {
        state[ln].hookLabel = (ev.data.hook_label as string) || "unknown";
        state[ln].hookState = (ev.data.hook_state as string) || "";
      }
      if (ev.type === "dtmf" && ev.digit) {
        state[ln].dtmfSeq = [...state[ln].dtmfSeq, ev.digit].slice(-30);
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
                <p>FXS 1 and FXS 2 register to Asterisk over the selected SIP transport.</p>
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

          <div className="side-stack">
            <div className="panel side">
              <h2>Provisioning</h2>
              <dl className="kv">
                <div><dt>Live transport</dt><dd>{liveTransport}</dd></div>
                <div><dt>Live server</dt><dd>{summary?.ports.port1.sip_server || "—"}</dd></div>
                <div><dt>Live port</dt><dd>{summary?.ports.port1.sip_port || "—"}</dd></div>
                <div><dt>Force target</dt><dd>{sipServer.trim() || "—"}:{selectedSipPort}</dd></div>
                <div><dt>Passwords</dt><dd>{summary?.expected.password_env_available?.SIP_1001_PASS && summary?.expected.password_env_available?.SIP_1002_PASS ? "env ready" : "check env"}</dd></div>
                <div><dt>API</dt><dd>{API_BASE_URL}</dd></div>
              </dl>
              <label className="field-label" htmlFor="sip-server">
                SIP server
                <input
                  id="sip-server"
                  value={sipServer}
                  onChange={(event) => setSipServer(event.target.value)}
                  placeholder="192.168.0.252"
                />
              </label>
              <button className="primary" onClick={provisionTwoLine} disabled={provisioning}>
                <Cable size={18} />
                {provisioning ? "Applying..." : "Apply two-line settings"}
              </button>
              <div className="transport-picker">
                <span>Force transport:</span>
                {(["udp", "tcp", "tls"] as const).map((t) => (
                  <button
                    key={t}
                    className={`filter-btn ${regTransport === t ? "active" : ""}`}
                    onClick={() => setRegTransport(t)}
                  >
                    {t.toUpperCase()}
                  </button>
                ))}
              </div>
              <button className="primary force-reg-btn" onClick={forceRegister} disabled={forceRegistering}>
                <RotateCcw size={18} />
                {forceRegistering ? "Forcing..." : `Force Register (${regTransport.toUpperCase()})`}
              </button>
              <p className="note">
                Force-register blind-writes write-only auth fields P34/P734/P4120/P4121 from API env vars; the HT812 cannot read them back.
              </p>
              {registerDebug && (
                <div className="debug-panel">
                  <p className="debug-title">Last force-register readback</p>
                  <p className="debug-message">{registerDebug.message}</p>
                  {registerDebug.diagnostics?.action?.password_fields_attempted?.length ? (
                    <div className="debug-log-path">
                      Password fields attempted: {registerDebug.diagnostics.action.password_fields_attempted.join(", ")}
                    </div>
                  ) : null}
                  {registerDebug.diagnostics?.debug_log_path && (
                    <div className="debug-log-path">
                      {registerDebug.diagnostics.debug_log_path}
                    </div>
                  )}
                  <table className="debug-table">
                    <tbody>
                      {Object.entries(registerDebug.readback)
                        .filter(([, v]) => v !== undefined)
                        .map(([k, v]) => (
                          <tr key={k} className={k.startsWith("P492") ? "debug-reg-row" : ""}>
                            <td className="debug-key">{k}</td>
                            <td className="debug-val">{v || <em>empty</em>}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            <div className="panel side">
              <div className="panel-head compact-head">
                <div>
                  <h2>Snapshots</h2>
                  <p>{backups.length} saved XML backups</p>
                </div>
                <button className="icon-button" onClick={loadBackups} disabled={backupLoading} title="Refresh snapshots">
                  <RefreshCw size={18} className={backupLoading ? "spin" : ""} />
                </button>
              </div>
              <button className="primary" onClick={createSnapshotBackup} disabled={snapshotSaving}>
                <DatabaseBackup size={18} />
                {snapshotSaving ? "Saving..." : "Save snapshot"}
              </button>
              {backupMessage && <p className="snapshot-message">{backupMessage}</p>}
              <div className="snapshot-list">
                {backups.length === 0 ? (
                  <div className="snapshot-empty">
                    <Archive size={17} />
                    No snapshots found
                  </div>
                ) : (
                  backups.slice(0, 8).map((backup) => <SnapshotRow key={backup.filename} backup={backup} />)
                )}
              </div>
            </div>
          </div>
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

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function SnapshotRow({ backup }: { backup: BackupFile }) {
  const created = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(backup.created_at));

  return (
    <article className="snapshot-row" title={backup.path}>
      <Archive size={17} />
      <div>
        <strong>{backup.filename}</strong>
        <span>{created} · {formatBytes(backup.size_bytes)}</span>
      </div>
    </article>
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

  return (
    <div className="panel dtmf-panel">
      <div className="panel-head">
        <div>
          <h2>FXS Line {lineNum}</h2>
          <p>Extension 100{lineNum} · {portStatus?.sip_server || "—"} · TCP</p>
        </div>
        <div className="badge-group">
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
        </div>
      </div>

      <div className="protocol-body">
        <div className="keypad-section">
          <p className="section-label">DTMF Keypad</p>
          <p className="key-legend">Last 5 keys highlighted</p>
          <div className="keypad">
            {DTMF_KEYS.map((row, ri) => (
              <div key={ri} className="keypad-row">
                {row.map((key) => (
                  <div key={key} className={`keypad-key ${recentDigits.has(key) ? "pressed" : ""}`}>
                    {key}
                  </div>
                ))}
              </div>
            ))}
          </div>
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
