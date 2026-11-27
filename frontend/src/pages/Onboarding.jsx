// Onboarding checklist screen (Tier-0 §19).

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { API_BASE } from "../config";

const STEPS = [
  { key: "email_confirmed",         label: "Confirm your email",       hint: "Unlocks invites + export" },
  { key: "first_policy_configured", label: "Configure first workforce policy", hint: "Establishes governance baseline" },
  { key: "first_invite_sent",       label: "Invite first teammate",    hint: "Drives team activation" },
  { key: "dashboard_reviewed",      label: "Review your dashboard",    hint: "Shows where the value is" },
  { key: "tools_connected",         label: "Install / connect tools",  hint: "Enables workforce data" },
  { key: "billing_reviewed",        label: "Review billing / trial",   hint: "Reduces conversion friction" },
];

function authHeaders() {
  const t = localStorage.getItem("token") || localStorage.getItem("zt_token");
  return t ? { Authorization: `Bearer ${t}`, "Content-Type": "application/json" }
           : { "Content-Type": "application/json" };
}

export default function Onboarding() {
  const [loading, setLoading] = useState(true);
  const [data, setData]       = useState(null);
  const [ent, setEnt]         = useState(null);

  async function load() {
    setLoading(true);
    try {
      const [a, b] = await Promise.all([
        fetch(`${API_BASE}/api/v1/onboarding/checklist`, { headers: authHeaders() }),
        fetch(`${API_BASE}/api/v1/entitlements/current`, { headers: authHeaders() }),
      ]);
      const av = a.ok ? await a.json() : null;
      const bv = b.ok ? await b.json() : null;
      setData(av);
      setEnt(bv);
    } finally { setLoading(false); }
  }

  useEffect(() => { load(); }, []);

  async function markMilestone(stepKey) {
    const map = {
      first_policy_configured: "first_policy_configured",
      first_invite_sent:       "first_invite_sent",
      dashboard_reviewed:      "dashboard_reviewed",
      tools_connected:         "tools_connected",
      billing_reviewed:        "billing_reviewed",
    };
    const target = map[stepKey];
    if (!target) return;
    try {
      await fetch(`${API_BASE}/api/v1/onboarding/milestone/${target}`, {
        method: "POST", headers: authHeaders(),
      });
      load();
    } catch (_e) { /* swallow */ }
  }

  if (loading) return <div style={styles.page}><div>Loading…</div></div>;

  const cl = data?.checklist || {};
  const emailVerified = !ent?.restrictions?.email_unverified;
  const trial = ent?.commercial_state?.startsWith("trial");
  const completedCount = STEPS.filter(s => cl[s.key]).length;
  const pct = Math.round((completedCount / STEPS.length) * 100);

  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <Link to="/dashboard" style={styles.brand}>Temveris</Link>
        <Link to="/dashboard" style={styles.skip}>Skip onboarding →</Link>
      </header>

      <main style={styles.main}>
        <h1 style={styles.h1}>
          Welcome — let's set up your Temveris workspace.
        </h1>
        {trial && (
          <div style={styles.banner}>
            You're on a 30-day trial. Plan: <strong>{ent.plan}</strong>.{" "}
            {!emailVerified && (
              <>Verify your email to unlock invites and data export. </>
            )}
            <Link to="/billing" style={styles.link}>Add a payment method →</Link>
          </div>
        )}

        <div style={styles.progressWrap}>
          <div style={{ ...styles.progressBar, width: `${pct}%` }} />
        </div>
        <div style={styles.progressLabel}>
          {completedCount} of {STEPS.length} steps complete
        </div>

        <ul style={styles.list}>
          {STEPS.map(step => {
            const done = !!cl[step.key];
            const locked = step.key !== "email_confirmed" && !emailVerified;
            return (
              <li key={step.key} style={styles.item}>
                <div aria-checked={done}
                     style={{ ...styles.tick, background: done ? "#22C55E" : "#fff",
                              borderColor: done ? "#22C55E" : "#CBD5E1" }}>
                  {done && <span style={{ color: "#fff", fontSize: 14 }}>✓</span>}
                </div>
                <div style={{ flex: 1 }}>
                  <strong style={{ color: locked ? "#94A3B8" : "#0F172A" }}>
                    {step.label}
                  </strong>
                  <div style={{ fontSize: 12, color: "#64748B" }}>{step.hint}</div>
                </div>
                {step.key === "email_confirmed"
                  ? (emailVerified ? null : <span style={styles.smallBadge}>Awaiting verification</span>)
                  : (
                    !done && (
                      <button style={styles.markBtn} disabled={locked}
                              onClick={() => markMilestone(step.key)}>
                        Mark done
                      </button>
                    )
                  )}
              </li>
            );
          })}
        </ul>

        <div style={{ marginTop: 24, display: "flex", gap: 12 }}>
          <Link to="/dashboard" style={styles.primary}>Open dashboard</Link>
          <Link to="/admin/leave" style={styles.secondary}>Configure policy</Link>
        </div>
      </main>
    </div>
  );
}

const styles = {
  page: { minHeight: "100vh", background: "#F8FAFC", fontFamily: "'DM Sans', sans-serif" },
  header: { display: "flex", justifyContent: "space-between", padding: "20px 32px",
            background: "#fff", borderBottom: "1px solid #E5E7EB" },
  brand: { color: "#11496A", fontWeight: 700, fontSize: 20, textDecoration: "none" },
  skip:  { color: "#64748B", textDecoration: "none", fontSize: 14 },
  main:  { maxWidth: 720, margin: "32px auto", padding: 32, background: "#fff",
           borderRadius: 14, boxShadow: "0 1px 4px rgba(15,23,42,0.05)" },
  h1: { fontSize: 24, color: "#0F172A", margin: 0 },
  banner: { background: "#F1F7FB", border: "1px solid #DBEAFE", borderRadius: 10,
            padding: "10px 14px", marginTop: 14, color: "#0F172A", fontSize: 14 },
  link: { color: "#11496A", fontWeight: 600 },
  progressWrap: { height: 8, background: "#E5E7EB", borderRadius: 6, marginTop: 18 },
  progressBar:  { height: "100%", background: "#11496A", borderRadius: 6, transition: "width 0.3s" },
  progressLabel:{ fontSize: 12, color: "#64748B", marginTop: 4 },
  list: { listStyle: "none", padding: 0, marginTop: 22 },
  item: { display: "flex", alignItems: "center", gap: 14, padding: "14px 12px",
          border: "1px solid #E5E7EB", borderRadius: 10, marginBottom: 8 },
  tick: { width: 24, height: 24, border: "2px solid #CBD5E1", borderRadius: 999,
          display: "grid", placeItems: "center", flexShrink: 0 },
  smallBadge: { background: "#FEF3C7", color: "#92400E",
                padding: "2px 8px", borderRadius: 999, fontSize: 11, fontWeight: 600 },
  markBtn: { background: "#fff", border: "1px solid #11496A", color: "#11496A",
             padding: "6px 12px", borderRadius: 8, cursor: "pointer", fontSize: 13 },
  primary: { padding: "10px 16px", background: "#11496A", color: "#fff",
             borderRadius: 8, textDecoration: "none", fontSize: 14 },
  secondary: { padding: "10px 16px", background: "#fff",
               border: "1px solid #CBD5E1", color: "#11496A",
               borderRadius: 8, textDecoration: "none", fontSize: 14 },
};
