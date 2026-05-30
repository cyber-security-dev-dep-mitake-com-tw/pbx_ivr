import { useEffect, useMemo, useState } from "react";
import { Activity, Cable, CheckCircle2, ClipboardList, PhoneCall, Radio, RefreshCw, Server, TriangleAlert } from "lucide-react";
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
    manual_password_fields: string[];
  };
  ports: {
    port1: PortStatus;
    port2: PortStatus;
    raw: Record<string, string>;
  };
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

type Tab = "setup" | "timeline";

function App() {
  const [tab, setTab] = useState<Tab>("setup");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [events, setEvents] = useState<CommunicationEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [provisioning, setProvisioning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<"connecting" | "open" | "closed">("connecting");

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

  useEffect(() => {
    loadSummary();
  }, []);

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
        <button className={tab === "timeline" ? "active" : ""} onClick={() => setTab("timeline")}>
          <Activity size={18} />
          Timeline
        </button>
      </nav>

      {error && <div className="error"><TriangleAlert size={18} />{error}</div>}

      {tab === "setup" ? (
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
        </section>
      ) : (
        <section className="panel">
          <div className="panel-head">
            <div>
              <h2>Live Communication Process</h2>
              <p>ARI and provisioning events stream from the backend.</p>
            </div>
            <div className={`stream ${streamState}`}>
              <Radio size={17} />
              {streamState}
            </div>
          </div>
          <div className="timeline">
            {latestEvents.length === 0 ? (
              <div className="empty">No communication events yet.</div>
            ) : (
              latestEvents.map((event) => <EventRow key={event.id} event={event} />)
            )}
          </div>
        </section>
      )}
    </main>
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

function EventRow({ event }: { event: CommunicationEvent }) {
  const time = new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(event.created_at));

  return (
    <article className="event-row">
      <div className="event-icon"><Server size={17} /></div>
      <div className="event-main">
        <div className="event-meta">
          <span>{time}</span>
          <span>{event.source}</span>
          <span>{event.type}</span>
          {event.digit && <span>DTMF {event.digit}</span>}
        </div>
        <p>{event.message}</p>
        {(event.caller || event.channel_id) && (
          <div className="event-detail">
            {event.caller && <span>caller {event.caller}</span>}
            {event.line && <span>line {event.line}</span>}
            {event.channel_id && <span>channel {event.channel_id}</span>}
          </div>
        )}
      </div>
    </article>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
