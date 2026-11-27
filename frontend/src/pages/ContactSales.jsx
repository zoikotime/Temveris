// Assisted / Enterprise lead capture page (Tier-0 §6.3 / §6.4).
// Routes: /contact-sales

import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { API_BASE } from "../config";

export default function ContactSales() {
  const [params] = useSearchParams();
  const initialIntent = params.get("route") === "enterprise" ? "enterprise" : "assisted";

  const [form, setForm] = useState({
    intent: initialIntent,
    first_name: "", last_name: "", work_email: "",
    company_name: "", country: "US", expected_users: 100,
    industry: "", deployment_pref: "SaaS",
    integrations: "", compliance_needs: "", notes: "",
  });
  const [busy, setBusy] = useState(false);
  const [ok, setOk] = useState(false);
  const [err, setErr] = useState(null);
  const update = (k, v) => setForm(f => ({ ...f, [k]: v }));

  async function submit(e) {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/leads`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...form,
          expected_users: parseInt(form.expected_users, 10) || 0,
          source: {
            utm_source: params.get("utm_source") || null,
            utm_medium: params.get("utm_medium") || null,
            utm_campaign: params.get("utm_campaign") || null,
            referrer: document.referrer || null,
          },
        }),
      });
      if (!res.ok) {
        const body = await res.json();
        setErr(body?.error?.message || "Couldn't submit your enquiry.");
        return;
      }
      setOk(true);
    } finally { setBusy(false); }
  }

  if (ok) {
    return (
      <div style={styles.page}>
        <div style={styles.card}>
          <h1 style={{ fontSize: 26, margin: 0 }}>Thanks — we've got it.</h1>
          <p style={{ color: "#475569", marginTop: 10 }}>
            A specialist will reach out within one business day. Meanwhile feel
            free to <Link to="/start">start a free trial</Link> while we
            scope your deployment.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div style={styles.page}>
      <form onSubmit={submit} style={styles.card}>
        <h1 style={{ fontSize: 24, margin: 0 }}>
          Talk to {form.intent === "enterprise" ? "Enterprise" : "Sales"}
        </h1>
        <p style={{ color: "#475569", marginTop: 6 }}>
          Tell us about your team. We'll route you to the right person.
        </p>

        <div style={styles.toggle}>
          {["assisted", "enterprise"].map(v => (
            <button type="button" key={v}
                    onClick={() => update("intent", v)}
                    style={{ ...styles.toggleBtn,
                             background: form.intent === v ? "#11496A" : "#fff",
                             color: form.intent === v ? "#fff" : "#11496A" }}>
              {v === "assisted" ? "Assisted (50–249 users)" : "Enterprise (250+ users)"}
            </button>
          ))}
        </div>

        <div style={styles.row}>
          <L label="First name"><input style={styles.input} required value={form.first_name}
                                       onChange={e => update("first_name", e.target.value)} /></L>
          <L label="Last name"><input style={styles.input} required value={form.last_name}
                                      onChange={e => update("last_name", e.target.value)} /></L>
        </div>
        <L label="Work email"><input style={styles.input} type="email" required value={form.work_email}
                                     onChange={e => update("work_email", e.target.value)} /></L>
        <L label="Company"><input style={styles.input} required value={form.company_name}
                                  onChange={e => update("company_name", e.target.value)} /></L>
        <div style={styles.row}>
          <L label="Country"><input style={styles.input} maxLength={2} value={form.country}
                                    onChange={e => update("country", e.target.value.toUpperCase())} /></L>
          <L label="Expected users"><input style={styles.input} type="number" min={1}
                                           value={form.expected_users}
                                           onChange={e => update("expected_users", e.target.value)} /></L>
        </div>
        <L label="Industry"><input style={styles.input} value={form.industry}
                                   onChange={e => update("industry", e.target.value)} /></L>
        {form.intent === "enterprise" && (
          <>
            <L label="Deployment preference">
              <select style={styles.input} value={form.deployment_pref}
                      onChange={e => update("deployment_pref", e.target.value)}>
                <option>SaaS</option><option>Private Cloud</option>
                <option>On-Premise</option><option>Not Sure</option>
              </select>
            </L>
            <L label="Required integrations">
              <textarea style={{ ...styles.input, minHeight: 60 }} value={form.integrations}
                        onChange={e => update("integrations", e.target.value)} />
            </L>
            <L label="Compliance requirements">
              <textarea style={{ ...styles.input, minHeight: 60 }} value={form.compliance_needs}
                        onChange={e => update("compliance_needs", e.target.value)} />
            </L>
          </>
        )}
        <L label="Anything else?"><textarea style={{ ...styles.input, minHeight: 80 }} value={form.notes}
                                            onChange={e => update("notes", e.target.value)} /></L>

        {err && <div style={styles.error}>{err}</div>}

        <button type="submit" disabled={busy} style={{
          ...styles.submit, opacity: busy ? 0.6 : 1, cursor: busy ? "not-allowed" : "pointer",
        }}>{busy ? "Sending…" : "Send enquiry"}</button>
      </form>
    </div>
  );
}

function L({ label, children }) {
  return (
    <label style={{ display: "block", marginBottom: 12 }}>
      <span style={{ display: "block", fontSize: 13, color: "#475569", marginBottom: 4 }}>{label}</span>
      {children}
    </label>
  );
}

const styles = {
  page: { minHeight: "100vh", padding: "40px 16px",
          background: "linear-gradient(160deg, #F1F7FB 0%, #fff 100%)",
          fontFamily: "'DM Sans', sans-serif" },
  card: { maxWidth: 620, margin: "0 auto", background: "#fff",
          padding: 32, borderRadius: 14, boxShadow: "0 4px 16px rgba(15,23,42,0.06)" },
  toggle: { display: "flex", gap: 8, marginTop: 18, marginBottom: 18 },
  toggleBtn: { flex: 1, padding: "10px 12px", border: "1px solid #11496A",
               borderRadius: 8, cursor: "pointer", fontWeight: 600, fontSize: 13 },
  row: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 },
  input: { width: "100%", padding: "10px 12px", border: "1px solid #CBD5E1",
           borderRadius: 8, fontSize: 14, fontFamily: "inherit" },
  submit: { width: "100%", marginTop: 8, padding: "12px 16px",
            background: "#11496A", color: "#fff", border: "none",
            borderRadius: 10, fontSize: 15, fontWeight: 600 },
  error: { background: "#FEF2F2", color: "#991B1B", padding: "10px 12px",
           borderRadius: 8, marginTop: 12, fontSize: 13 },
};
