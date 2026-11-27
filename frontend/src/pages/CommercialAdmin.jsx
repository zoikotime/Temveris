// Commercial Admin Console (Tier-0 §22).
// Routes: /admin/commercial
//
// Tabs:
//   Tenants  — search, view detail, suspend / reactivate / close / retry
//   Manual review — fraud-flagged signups awaiting human decision
//   Fraud queue — last 24h fraud signals
//   Events    — commercial event stream (signup.*, payment.*, etc.)
//   Funnel    — basic conversion metrics

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { API_BASE } from "../config";

function authHeaders() {
  const t = localStorage.getItem("token") || localStorage.getItem("zt_token");
  return t ? { Authorization: `Bearer ${t}`, "Content-Type": "application/json" }
           : { "Content-Type": "application/json" };
}

// Global search / correlation explorer — resolves any operator identifier to
// its entity + event timeline via GET /admin/commercial/search (audit §9).
function GlobalSearch() {
  const [q, setQ] = useState("");
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);

  const run = async (e) => {
    e?.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    try {
      const r = await fetch(
        `${API_BASE}/api/v1/admin/commercial/search?q=${encodeURIComponent(q.trim())}`,
        { headers: authHeaders() },
      );
      setRes(await r.json());
    } catch {
      setRes({ note: "Search failed. Please retry." });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <form onSubmit={run} style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="Search commercial records"
          placeholder="Org, workspace, signup, event, correlation ID, ZT- support ref, or email"
          style={{ flex: 1, padding: "10px 12px", border: "1px solid #cbd5e1", borderRadius: 8 }}
        />
        <button style={styles.tab} disabled={busy}>{busy ? "Searching…" : "Search"}</button>
      </form>

      {res && (
        <>
          <div style={{ marginBottom: 12, fontSize: 13, color: "#475569" }}>
            <strong>Kind:</strong> {res.kind || "—"}
            {res.correlation_id && <> · <strong>Correlation:</strong> <code>{res.correlation_id}</code></>}
            {res.note && <> · <em>{res.note}</em></>}
          </div>
          {res.entity && (
            <pre style={{ background: "#f8fafc", padding: 12, borderRadius: 8, fontSize: 12, overflow: "auto" }}>
              {JSON.stringify(res.entity, null, 2)}
            </pre>
          )}
          <h3 style={{ fontSize: 14, margin: "12px 0 6px" }}>Timeline ({res.timeline?.length || 0})</h3>
          <table style={styles.table}>
            <thead><tr><th>When</th><th>Event</th><th>State</th><th>Error</th><th>Service</th></tr></thead>
            <tbody>
              {(res.timeline || []).map((ev) => (
                <tr key={ev.event_id}>
                  <td>{ev.occurred_at ? ev.occurred_at.replace("T", " ").slice(0, 19) : ""}</td>
                  <td>{ev.event_type}</td>
                  <td>{ev.state_from && ev.state_to ? `${ev.state_from} → ${ev.state_to}` : ""}</td>
                  <td>{ev.error_code || ""}</td>
                  <td>{ev.source_service}</td>
                </tr>
              ))}
              {res.timeline && res.timeline.length === 0 && (
                <tr><td colSpan={5} style={{ color: "#94a3b8" }}>No events for this identifier.</td></tr>
              )}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

export default function CommercialAdmin() {
  const [tab, setTab] = useState("tenants");
  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <Link to="/admin" style={styles.brand}>← Admin</Link>
        <h1 style={styles.title}>Commercial Operations</h1>
        <span />
      </header>

      <nav style={styles.tabs}>
        {[
          ["search",  "Search"],
          ["tenants", "Tenants"],
          ["review",  "Manual review"],
          ["fraud",   "Fraud queue"],
          ["recovery","Payment recovery"],
          ["dlq",     "Webhook DLQ"],
          ["events",  "Events"],
          ["funnel",  "Funnel"],
        ].map(([k, label]) => (
          <button key={k} onClick={() => setTab(k)}
                  style={{ ...styles.tab,
                           background: tab === k ? "#11496A" : "transparent",
                           color: tab === k ? "#fff" : "#0F172A" }}>
            {label}
          </button>
        ))}
      </nav>

      <main style={styles.body}>
        {tab === "search"  && <GlobalSearch />}
        {tab === "tenants" && <Tenants />}
        {tab === "review"  && <ManualReview />}
        {tab === "fraud"   && <FraudQueue />}
        {tab === "recovery" && <PaymentRecovery />}
        {tab === "dlq"     && <WebhookDLQ />}
        {tab === "events"  && <Events />}
        {tab === "funnel"  && <Funnel />}
      </main>
    </div>
  );
}


function Tenants() {
  const [q, setQ] = useState("");
  const [state, setState] = useState("");
  const [list, setList] = useState({ total: 0, items: [] });
  const [picked, setPicked] = useState(null);
  const [detail, setDetail] = useState(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    setBusy(true);
    try {
      const params = new URLSearchParams();
      if (q) params.set("q", q);
      if (state) params.set("state", state);
      const res = await fetch(`${API_BASE}/api/v1/admin/commercial/tenants?${params}`,
                              { headers: authHeaders() });
      if (res.ok) setList(await res.json());
    } finally { setBusy(false); }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  async function pick(id) {
    setPicked(id); setDetail(null);
    const res = await fetch(`${API_BASE}/api/v1/admin/commercial/tenants/${id}`,
                            { headers: authHeaders() });
    if (res.ok) setDetail(await res.json());
  }

  async function act(path) {
    if (!picked) return;
    await fetch(`${API_BASE}/api/v1/admin/commercial/tenants/${picked}/${path}`,
                { method: "POST", headers: authHeaders() });
    pick(picked); load();
  }

  return (
    <div style={styles.split}>
      <div>
        <div style={styles.toolbar}>
          <input style={styles.input} placeholder="Search name / domain / id"
                 value={q} onChange={e => setQ(e.target.value)}
                 onKeyDown={e => e.key === "Enter" && load()} />
          <select style={{ ...styles.input, width: 180 }} value={state}
                  onChange={e => setState(e.target.value)}>
            <option value="">Any state</option>
            {["trial_active","trial_ending","trial_expired","active","past_due",
              "grace_period","suspended","cancelled","closed","demo","sandbox",
              "enterprise_pending","manual_review"].map(s =>
                <option key={s}>{s}</option>)}
          </select>
          <button style={styles.btn} disabled={busy} onClick={load}>Search</button>
        </div>
        <div style={styles.list}>
          {list.items.map(t => (
            <button key={t.organization_id} onClick={() => pick(t.organization_id)}
                    style={{ ...styles.listItem,
                             background: picked === t.organization_id ? "#F1F7FB" : "#fff" }}>
              <div style={{ fontWeight: 600 }}>{t.name}</div>
              <div style={{ fontSize: 12, color: "#475569" }}>
                {t.organization_id} · {t.country || "—"} · <Pill text={t.commercial_state} />
              </div>
            </button>
          ))}
          {list.items.length === 0 && <div style={styles.empty}>No tenants found.</div>}
        </div>
      </div>

      <div>
        {!detail ? (
          <div style={styles.empty}>Select a tenant to see details.</div>
        ) : (
          <div style={styles.panel}>
            <h3 style={{ margin: 0 }}>{detail.organization.name}</h3>
            <div style={{ fontSize: 13, color: "#475569" }}>
              {detail.organization.id} · {detail.organization.country}
            </div>
            <div style={{ marginTop: 10 }}>
              <Pill text={detail.organization.commercial_state} />
            </div>

            <h4 style={styles.h4}>Subscription</h4>
            {detail.subscription ? (
              <table style={styles.kv}>
                <tbody>
                  <Row k="Plan" v={detail.subscription.plan_code} />
                  <Row k="State" v={detail.subscription.state} />
                  <Row k="Cycle" v={detail.subscription.billing_cycle} />
                  <Row k="Seats" v={detail.subscription.seat_count} />
                  <Row k="Trial ends" v={detail.subscription.trial_ends_at || "—"} />
                </tbody>
              </table>
            ) : <div style={styles.empty}>No subscription</div>}

            <h4 style={styles.h4}>Entitlements</h4>
            <pre style={styles.pre}>{JSON.stringify(detail.entitlements?.features, null, 2)}</pre>

            <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
              <button style={styles.btn} onClick={() => act("retry-provisioning")}>Retry provisioning</button>
              <button style={styles.btn} onClick={() => act("recompute-entitlements")}>Recompute entitlements</button>
              <button style={styles.btnWarn} onClick={() => act("suspend")}>Suspend</button>
              <button style={styles.btn} onClick={() => act("reactivate")}>Reactivate</button>
              <button style={styles.btnDanger} onClick={() => act("close")}>Close</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}


function ManualReview() {
  const [items, setItems] = useState([]);
  async function load() {
    const r = await fetch(`${API_BASE}/api/v1/admin/commercial/manual-review?status=pending`,
                          { headers: authHeaders() });
    if (r.ok) setItems((await r.json()).items || []);
  }
  useEffect(() => { load(); }, []);

  async function resolve(case_id, outcome) {
    await fetch(`${API_BASE}/api/v1/admin/commercial/manual-review/${case_id}/resolve`,
                { method: "POST", headers: authHeaders(),
                  body: JSON.stringify({ outcome }) });
    load();
  }
  return (
    <div>
      {items.length === 0 && <div style={styles.empty}>Queue clear.</div>}
      {items.map(c => (
        <div key={c.case_id} style={styles.panel}>
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <div>
              <strong>{c.reason}</strong> · <Pill text={c.severity} />{" "}
              <span style={{ color: "#64748B" }}>score {c.fraud_score}</span>
            </div>
            <small style={{ color: "#64748B" }}>{c.created_at}</small>
          </div>
          <div style={{ fontSize: 12, color: "#475569", marginTop: 4 }}>
            signup_id: {c.signup_id}
          </div>
          <pre style={styles.pre}>{JSON.stringify(c.details, null, 2)}</pre>
          <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
            <button style={styles.btn} onClick={() => resolve(c.case_id, "approve")}>Approve</button>
            <button style={styles.btn} onClick={() => resolve(c.case_id, "approve_with_restrictions")}>Approve w/ restrictions</button>
            <button style={styles.btn} onClick={() => resolve(c.case_id, "route_to_sales")}>Route to sales</button>
            <button style={styles.btnWarn} onClick={() => resolve(c.case_id, "request_more_information")}>Request more info</button>
            <button style={styles.btnDanger} onClick={() => resolve(c.case_id, "block")}>Block</button>
          </div>
        </div>
      ))}
    </div>
  );
}


function FraudQueue() {
  const [rows, setRows] = useState([]);
  useEffect(() => {
    fetch(`${API_BASE}/api/v1/admin/commercial/fraud-signals?since_hours=24`,
          { headers: authHeaders() })
      .then(r => r.ok ? r.json() : { items: [] })
      .then(b => setRows(b.items || []));
  }, []);
  return (
    <table style={styles.table}>
      <thead>
        <tr><th>Type</th><th>Severity</th><th>Signup</th><th>Δ</th><th>Time</th></tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td>{r.signal_type}</td>
            <td><Pill text={r.severity} /></td>
            <td>{r.signup_id || "—"}</td>
            <td>{r.score_delta}</td>
            <td>{r.created_at}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}


function Events() {
  const [rows, setRows] = useState([]);
  const [evtType, setEvtType] = useState("");
  useEffect(() => {
    const params = new URLSearchParams();
    if (evtType) params.set("event_type", evtType);
    fetch(`${API_BASE}/api/v1/admin/commercial/events?${params}`,
          { headers: authHeaders() })
      .then(r => r.ok ? r.json() : { items: [] })
      .then(b => setRows(b.items || []));
  }, [evtType]);
  return (
    <div>
      <div style={styles.toolbar}>
        <input style={styles.input} placeholder="event_type filter (e.g. payment.succeeded)"
               value={evtType} onChange={e => setEvtType(e.target.value)} />
      </div>
      <table style={styles.table}>
        <thead><tr><th>Event</th><th>Org</th><th>From → To</th><th>Service</th><th>Time</th></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td>{r.event_type}</td>
              <td>{r.organization_id || "—"}</td>
              <td>{r.state_from ? `${r.state_from} → ${r.state_to}` : "—"}</td>
              <td>{r.source_service}</td>
              <td>{r.occurred_at}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


function Funnel() {
  const [m, setM] = useState(null);
  useEffect(() => {
    fetch(`${API_BASE}/api/v1/admin/commercial/metrics/funnel?since_hours=72`,
          { headers: authHeaders() })
      .then(r => r.ok ? r.json() : null).then(setM);
  }, []);
  if (!m) return <div style={styles.empty}>Loading…</div>;
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>Last 72 hours</h3>
      {m.stages.map(s => (
        <div key={s.name} style={{ display: "flex", justifyContent: "space-between",
                                   padding: "8px 0", borderBottom: "1px solid #E5E7EB" }}>
          <span>{s.name}</span>
          <strong>{s.count}</strong>
        </div>
      ))}
      <div style={{ marginTop: 14, color: "#991B1B" }}>
        Failures: <strong>{m.failures}</strong>
      </div>
    </div>
  );
}


function Pill({ text }) {
  const color = {
    trial_active: "#15803D", trial_ending: "#92400E", trial_expired: "#991B1B",
    active: "#15803D", past_due: "#92400E", suspended: "#991B1B",
    cancelled: "#475569", closed: "#475569", manual_review: "#92400E",
    high: "#991B1B", critical: "#991B1B", medium: "#92400E", low: "#475569",
  }[text] || "#475569";
  return <span style={{ background: "#F8FAFC", color, padding: "2px 8px",
                        borderRadius: 999, fontSize: 11, fontWeight: 700 }}>{text}</span>;
}

function Row({ k, v }) {
  return (<tr><td style={{ color: "#64748B", paddingRight: 10 }}>{k}</td><td>{v}</td></tr>);
}


// §6.2 Critical-failure recovery queue. Lists checkout sessions whose
// payment succeeded but downstream provisioning failed. Admin can retry
// (re-run entitlements compute) or queue a refund for finance.
function PaymentRecovery() {
  const [list, setList] = useState({ total: 0, items: [] });
  const [busy, setBusy] = useState(null);
  const [msg, setMsg]   = useState(null);

  const load = async () => {
    const r = await fetch(`${API_BASE}/api/v1/admin/commercial/payment-recovery`, { headers: authHeaders() });
    if (r.ok) setList(await r.json());
  };
  useEffect(() => { load(); }, []);

  const retry = async (id) => {
    setBusy(id); setMsg(null);
    try {
      const r = await fetch(`${API_BASE}/api/v1/admin/commercial/payment-recovery/${id}/retry`, {
        method: "POST", headers: authHeaders(),
      });
      const d = await r.json();
      setMsg(r.ok ? `Retried ${id}: ${d.recovered ? "RECOVERED" : "still pending"}` : `Retry failed: ${d.detail}`);
      load();
    } finally { setBusy(null); }
  };

  const refund = async (id) => {
    const reason = window.prompt("Refund reason?", "post_payment_provisioning_unrecoverable");
    if (!reason) return;
    setBusy(id); setMsg(null);
    try {
      const r = await fetch(`${API_BASE}/api/v1/admin/commercial/payment-recovery/${id}/refund`, {
        method: "POST", headers: authHeaders(), body: JSON.stringify({ reason }),
      });
      const d = await r.json();
      setMsg(r.ok ? `Refunded ${id}` : `Refund failed: ${d.detail}`);
      load();
    } finally { setBusy(null); }
  };

  return (
    <div>
      <h2 style={{ margin: "0 0 6px", color: "#0F172A" }}>Payment Recovery Queue</h2>
      <p style={{ color: "#64748B", marginTop: 0, fontSize: 13 }}>
        Sessions where payment succeeded but post-payment provisioning failed (§6.2). Retry runs entitlement compute again; refund opens a ledgered refund the finance team can process via the provider.
      </p>
      {msg && <div style={{ background: "#EFF6FF", padding: "8px 12px", borderRadius: 6, color: "#1D4ED8", marginBottom: 12 }}>{msg}</div>}
      <p style={{ color: "#64748B" }}>{list.total} in queue</p>
      <table style={styles.table}>
        <thead><tr>{["Session", "Org", "Plan", "Reason", "Attempts", "Updated", "Actions"].map(h => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>
          {list.items.map(s => (
            <tr key={s.checkout_session_id}>
              <td><code>{s.checkout_session_id}</code></td>
              <td><code>{s.organization_id}</code></td>
              <td>{s.plan} · {s.cycle} · {s.seats} seats</td>
              <td>{s.reason}</td>
              <td>{s.attempts}</td>
              <td>{s.updated_at?.slice(0, 16)?.replace("T", " ")}</td>
              <td>
                <button onClick={() => retry(s.checkout_session_id)} disabled={busy === s.checkout_session_id}
                  style={{ marginRight: 6, padding: "4px 10px", borderRadius: 4, border: "1px solid #11496A", background: "#11496A", color: "#fff", cursor: "pointer", fontSize: 12 }}>
                  Retry
                </button>
                <button onClick={() => refund(s.checkout_session_id)} disabled={busy === s.checkout_session_id}
                  style={{ padding: "4px 10px", borderRadius: 4, border: "1px solid #B91C1C", background: "#fff", color: "#B91C1C", cursor: "pointer", fontSize: 12 }}>
                  Refund
                </button>
              </td>
            </tr>
          ))}
          {list.items.length === 0 && (
            <tr><td colSpan={7} style={{ padding: 24, textAlign: "center", color: "#94A3B8" }}>No sessions in the recovery queue.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}


// §23 Webhook dead-letter queue. After 5 failed processing attempts a
// provider webhook is parked here. Admin can replay a single receipt
// (resets status to "received" so the next worker pass picks it up).
function WebhookDLQ() {
  const [list, setList] = useState({ total: 0, items: [] });
  const [busy, setBusy] = useState(null);
  const [msg, setMsg]   = useState(null);

  const load = async () => {
    const r = await fetch(`${API_BASE}/api/v1/admin/commercial/webhooks/dead-letter`, { headers: authHeaders() });
    if (r.ok) setList(await r.json());
  };
  useEffect(() => { load(); }, []);

  const replay = async (id) => {
    setBusy(id); setMsg(null);
    try {
      const r = await fetch(`${API_BASE}/api/v1/admin/commercial/webhooks/dead-letter/${id}/replay`, {
        method: "POST", headers: authHeaders(),
      });
      const d = await r.json();
      setMsg(r.ok ? `Receipt ${id} queued for replay` : `Replay failed: ${d.detail}`);
      load();
    } finally { setBusy(null); }
  };

  return (
    <div>
      <h2 style={{ margin: "0 0 6px", color: "#0F172A" }}>Webhook Dead-Letter Queue</h2>
      <p style={{ color: "#64748B", marginTop: 0, fontSize: 13 }}>
        Provider webhooks that failed processing 5+ times. Replay resets status so the next worker tick picks it up — idempotency on (provider, event_id) prevents duplicate work.
      </p>
      {msg && <div style={{ background: "#EFF6FF", padding: "8px 12px", borderRadius: 6, color: "#1D4ED8", marginBottom: 12 }}>{msg}</div>}
      <p style={{ color: "#64748B" }}>{list.total} parked</p>
      <table style={styles.table}>
        <thead><tr>{["ID", "Provider", "Event ID", "Type", "Attempts", "Last Error", "Received", ""].map(h => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>
          {list.items.map(r => (
            <tr key={r.id}>
              <td>{r.id}</td>
              <td>{r.provider}</td>
              <td><code style={{ fontSize: 11 }}>{r.provider_event_id}</code></td>
              <td>{r.event_type}</td>
              <td>{r.attempts}</td>
              <td title={r.last_error}><span style={{ color: "#B91C1C", fontSize: 11 }}>{(r.last_error || "").slice(0, 60)}{(r.last_error || "").length > 60 ? "…" : ""}</span></td>
              <td>{r.received_at?.slice(0, 16)?.replace("T", " ")}</td>
              <td>
                <button onClick={() => replay(r.id)} disabled={busy === r.id}
                  style={{ padding: "4px 10px", borderRadius: 4, border: "1px solid #11496A", background: "#11496A", color: "#fff", cursor: "pointer", fontSize: 12 }}>
                  Replay
                </button>
              </td>
            </tr>
          ))}
          {list.items.length === 0 && (
            <tr><td colSpan={8} style={{ padding: 24, textAlign: "center", color: "#94A3B8" }}>Dead-letter queue is empty.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}


const styles = {
  page: { minHeight: "100vh", background: "#F8FAFC", fontFamily: "'DM Sans', sans-serif" },
  header: { display: "grid", gridTemplateColumns: "120px 1fr 120px",
            alignItems: "center", padding: "16px 24px", background: "#fff",
            borderBottom: "1px solid #E5E7EB" },
  brand: { color: "#11496A", textDecoration: "none", fontWeight: 600 },
  title: { margin: 0, textAlign: "center", fontSize: 18, color: "#0F172A" },
  tabs: { display: "flex", gap: 6, padding: "10px 24px", background: "#fff",
          borderBottom: "1px solid #E5E7EB" },
  tab: { border: "none", padding: "8px 14px", borderRadius: 8, cursor: "pointer",
         fontWeight: 600, fontSize: 13 },
  body: { padding: 24 },
  split: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 },
  toolbar: { display: "flex", gap: 8, marginBottom: 12 },
  input: { flex: 1, padding: "8px 10px", border: "1px solid #CBD5E1",
           borderRadius: 8, fontSize: 13 },
  btn: { padding: "8px 12px", background: "#fff", border: "1px solid #CBD5E1",
         borderRadius: 8, cursor: "pointer", fontSize: 13, color: "#11496A" },
  btnWarn: { padding: "8px 12px", background: "#FEF3C7", border: "1px solid #FDE68A",
             borderRadius: 8, cursor: "pointer", fontSize: 13, color: "#92400E" },
  btnDanger: { padding: "8px 12px", background: "#FEF2F2", border: "1px solid #FECACA",
               borderRadius: 8, cursor: "pointer", fontSize: 13, color: "#991B1B" },
  list: { background: "#fff", borderRadius: 10, border: "1px solid #E5E7EB",
          maxHeight: "70vh", overflowY: "auto" },
  listItem: { display: "block", width: "100%", textAlign: "left",
              padding: "10px 12px", border: "none",
              borderBottom: "1px solid #F1F5F9", cursor: "pointer" },
  empty: { color: "#94A3B8", padding: 24, textAlign: "center" },
  panel: { background: "#fff", padding: 18, borderRadius: 12,
           border: "1px solid #E5E7EB", marginBottom: 14 },
  h4: { fontSize: 13, marginTop: 14, color: "#475569",
        textTransform: "uppercase", letterSpacing: 1 },
  kv: { fontSize: 13, color: "#0F172A" },
  pre: { background: "#0F172A", color: "#94A3B8", padding: 12, borderRadius: 8,
         fontSize: 11, maxHeight: 200, overflow: "auto", marginTop: 6 },
  table: { width: "100%", borderCollapse: "collapse", background: "#fff",
           border: "1px solid #E5E7EB", borderRadius: 10, fontSize: 13 },
};
