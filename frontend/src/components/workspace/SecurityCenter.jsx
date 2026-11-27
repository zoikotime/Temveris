/**
 * Security Center — Workspace Settings → Security.
 *
 * Implements the seven-tab governance design from spec. Every value, toggle,
 * counter, and row is fed by the backend (`/api/workspace/security-center/*`)
 * — there is no static data in this component. Every write hits a real
 * endpoint and appends an immutable AuditLog row so the Audit & Compliance
 * page can prove who changed what.
 *
 * Endpoints:
 *   GET  /security-center/overview          – banner + 6 stat tiles
 *   GET  /security-center/password-policy
 *   PUT  /security-center/password-policy
 *   GET  /security-center/sessions-policy
 *   PUT  /security-center/sessions-policy
 *   GET  /security-center/sso
 *   PUT  /security-center/sso
 *   GET  /security-center/trusted-domains
 *   POST /security-center/trusted-domains
 *   POST /security-center/trusted-domains/{id}/verify
 *   DELETE /security-center/trusted-domains/{id}
 *   PUT  /security-center/invite-restrictions
 *   GET  /security-center/exceptions
 *   POST /security-center/exceptions
 *   DELETE /security-center/exceptions/{id}
 *   GET  /security-center/break-glass
 *   POST /security-center/break-glass
 *   DELETE /security-center/break-glass/{id}
 *   GET  /security-center/events
 *   GET  /security-center/export             – CSV evidence
 *   POST /security-center/secure             – mark workspace as reviewed
 *
 *   MFA enforcement tab reuses /access-governance/mfa-controls so the tier
 *   matrix stays the single source of truth across Roles & Permissions and
 *   Security pages.
 */
import { useEffect, useMemo, useState } from "react";
import { useTheme } from "../../context/ThemeContext";
import { wsGet, wsSend } from "./_api";
import { API_BASE } from "../../config";

const token = () => localStorage.getItem("zt_token") || localStorage.getItem("token");

// ──────────────────────────────────────────────────────────────────────
// Visual primitives
// ──────────────────────────────────────────────────────────────────────
const STATE_ACCENT = {
  enforced:        "#10B981",
  configured:      "#10B981",
  strong:          "#10B981",
  partial:         "#F59E0B",
  warning:         "#F59E0B",
  weak:            "#EF4444",
  moderate:        "#F59E0B",
  not_set:         "#94A3B8",
  not_configured:  "#F59E0B",
};

const TILE_LABELS = {
  mfa:             "MFA",
  sso:             "SSO",
  password:        "PASSWORDS",
  sessions:        "SESSIONS",
  trusted_domains: "TRUSTED DOMAINS",
  exceptions:      "EXCEPTIONS",
};

const TABS = [
  { k: "mfa",      label: "MFA enforcement"      },
  { k: "sso",      label: "SSO & identity"       },
  { k: "password", label: "Password policy"      },
  { k: "sessions", label: "Sessions & devices"   },
  { k: "domains",  label: "Trusted domains"      },
  { k: "exc",      label: "Exceptions & break-glass" },
  { k: "events",   label: "Security events"      },
];

const card = (C) => ({
  background: C.surface, border: `1px solid ${C.border}`,
  borderRadius: 12, boxSizing: "border-box",
});
const ghostBtn = (C) => ({
  padding: "9px 16px", borderRadius: 8, border: `1px solid ${C.border}`,
  background: C.surface, color: C.text, fontWeight: 600, fontSize: 13,
  cursor: "pointer", fontFamily: "inherit",
});
const primaryBtn = () => ({
  padding: "9px 16px", borderRadius: 8, border: "none",
  background: "#0F4C3A", color: "#fff", fontWeight: 700, fontSize: 13,
  cursor: "pointer", fontFamily: "inherit",
});

function Switch({ on, onChange, disabled, C }) {
  return (
    <button
      type="button"
      onClick={() => !disabled && onChange(!on)}
      disabled={disabled}
      style={{
        width: 38, height: 22, borderRadius: 999,
        background: on ? "#10B981" : (C.border || "#CBD5E1"),
        border: "none", padding: 0, position: "relative",
        cursor: disabled ? "default" : "pointer",
        opacity: disabled ? 0.6 : 1, transition: "background .15s",
      }}
      aria-pressed={on}
      aria-label="toggle"
    >
      <span style={{
        position: "absolute", top: 2, left: on ? 18 : 2,
        width: 18, height: 18, borderRadius: "50%", background: "#fff",
        transition: "left .15s", boxShadow: "0 1px 3px rgba(0,0,0,0.18)",
      }} />
    </button>
  );
}

function Pill({ children, tone, C }) {
  const palette = {
    success: { bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
    warning: { bg: "#FEF3C7", fg: "#92400E", bd: "#FDE68A" },
    danger:  { bg: "#FEE2E2", fg: "#B91C1C", bd: "#FECACA" },
    info:    { bg: "#EFF6FF", fg: "#1D4ED8", bd: "#BFDBFE" },
    neutral: { bg: C.bg, fg: C.textSub, bd: C.border },
  }[tone || "neutral"];
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", padding: "3px 10px",
      borderRadius: 999, fontSize: 11, fontWeight: 700, letterSpacing: 0.2,
      background: palette.bg, color: palette.fg, border: `1px solid ${palette.bd}`,
      whiteSpace: "nowrap",
    }}>{children}</span>
  );
}

function fmtDate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  } catch { return "—"; }
}
function fmtDateTime(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(); } catch { return "—"; }
}
function fmtRelative(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return "Just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `Today, ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  } catch { return "—"; }
}

// ──────────────────────────────────────────────────────────────────────
// Top stat tile
// ──────────────────────────────────────────────────────────────────────
function Tile({ tileKey, data, C }) {
  const accent = STATE_ACCENT[data?.state] || C.border;
  return (
    <div style={{
      ...card(C), padding: "14px 16px", borderLeft: `3px solid ${accent}`,
      minWidth: 0,
    }}>
      <p style={{
        margin: 0, fontSize: 11, color: C.textSub, fontWeight: 800,
        letterSpacing: 0.5, textTransform: "uppercase",
      }}>
        {TILE_LABELS[tileKey]}
      </p>
      <p style={{ margin: "8px 0 4px", fontSize: 22, fontWeight: 800, color: C.text }}>
        {data?.value || "—"}
      </p>
      <p style={{ margin: 0, fontSize: 12, color: C.textSub, lineHeight: 1.4 }}>
        {data?.caption || ""}
      </p>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Top-level component
// ──────────────────────────────────────────────────────────────────────
export function SecurityCenter({ isAdmin, showToast }) {
  const { C, isDark } = useTheme();

  const [tab, setTab] = useState("mfa");
  const [overview, setOverview] = useState(null);
  const [loadingOverview, setLoadingOverview] = useState(true);

  const refreshOverview = async () => {
    setLoadingOverview(true);
    try {
      setOverview(await wsGet("/api/workspace/security-center/overview"));
    } catch (e) {
      showToast?.(e?.message || "Failed to load security overview", "error");
    } finally {
      setLoadingOverview(false);
    }
  };

  useEffect(() => { refreshOverview(); }, []);

  const banner = overview?.banner;
  const tiles  = overview?.tiles || {};

  const exportEvidence = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/workspace/security-center/export?since_days=30`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`Export failed (${res.status})`);
      const blob = await res.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `security-evidence-${new Date().toISOString().slice(0,10)}.csv`;
      document.body.appendChild(link); link.click(); link.remove();
      URL.revokeObjectURL(link.href);
      showToast?.("Evidence exported");
    } catch (e) { showToast?.(e?.message || "Failed to export evidence", "error"); }
  };

  const secureWorkspace = async () => {
    try {
      await wsSend("POST", "/api/workspace/security-center/secure", { note: "marked secure from header CTA" });
      showToast?.("Workspace marked as secured");
      refreshOverview();
    } catch (e) { showToast?.(e?.message || "Failed to mark workspace secured", "error"); }
  };

  return (
    <div style={{ padding: "28px 36px 96px", maxWidth: 1240 }}>
      {/* ─── Header ──────────────────────────────────────────────── */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 18, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 14, alignItems: "flex-start", minWidth: 0 }}>
          <div style={{
            width: 38, height: 38, borderRadius: 10,
            background: isDark ? "#0D2A1A" : "#ECFDF5",
            display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
          }}>
            <span style={{ width: 14, height: 14, borderRadius: 4, background: "#10B981" }} />
          </div>
          <div style={{ minWidth: 0 }}>
            <h2 style={{ margin: 0, fontSize: 22, fontWeight: 800, color: C.text }}>Security</h2>
            <p style={{ margin: "4px 0 0", fontSize: 13, color: C.textSub, lineHeight: 1.5, maxWidth: 640 }}>
              Protect workspace access, enforce identity controls, manage sessions, and keep security changes audit-ready.
            </p>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button onClick={exportEvidence} style={ghostBtn(C)}>Export evidence</button>
          <button onClick={() => { setTab("events"); }} style={ghostBtn(C)}>View audit trail</button>
          <button onClick={secureWorkspace} style={primaryBtn()} disabled={!isAdmin}>Secure workspace</button>
        </div>
      </div>

      {/* ─── Banner ──────────────────────────────────────────────── */}
      {banner && (
        <div style={{
          background: banner.state === "ok" ? (isDark ? "#0D2A1A" : "#ECFDF5") : (isDark ? "#3D2A0A" : "#FFFBEB"),
          border:     `1px solid ${banner.state === "ok" ? "#A7F3D0" : "#FDE68A"}`,
          borderRadius: 12, padding: "14px 18px", marginBottom: 18,
          display: "flex", justifyContent: "space-between", gap: 16, alignItems: "flex-start", flexWrap: "wrap",
        }}>
          <div style={{ display: "flex", gap: 14, alignItems: "flex-start", minWidth: 0 }}>
            <div style={{
              width: 38, height: 38, borderRadius: 10,
              background: banner.state === "ok" ? "#D1FAE5" : "#FDE68A",
              color: banner.state === "ok" ? "#047857" : "#92400E",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontWeight: 800, fontSize: 16, flexShrink: 0,
            }}>{banner.state === "ok" ? "✓" : "阮"}</div>
            <div style={{ minWidth: 0 }}>
              <p style={{ margin: 0, fontSize: 14, fontWeight: 800, color: banner.state === "ok" ? "#047857" : "#92400E" }}>
                {banner.title}
              </p>
              <p style={{ margin: "4px 0 0", fontSize: 13, color: C.text, lineHeight: 1.5 }}>
                {banner.summary}
              </p>
              <p style={{ margin: "6px 0 0", fontSize: 11.5, color: C.textSub }}>
                Last reviewed: {banner.last_reviewed ? fmtRelative(banner.last_reviewed) : "—"}
                {" · "}Policy version: {banner.policy_version || "—"}
                {banner.next_review ? <> · Next recommended review: {fmtDate(banner.next_review)}</> : null}
              </p>
            </div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "flex-end" }}>
            <Pill tone={banner.state === "ok" ? "success" : "warning"} C={C}>
              {banner.state === "ok" ? "Healthy" : "Review needed"}
            </Pill>
            {banner.state !== "ok" && (
              <button onClick={() => setTab("mfa")} style={ghostBtn(C)}>Resolve risks</button>
            )}
          </div>
        </div>
      )}

      {/* ─── 6 Stat tiles ───────────────────────────────────────── */}
      <div style={{
        display: "grid", gridTemplateColumns: "repeat(6, minmax(0, 1fr))",
        gap: 12, marginBottom: 22,
      }}>
        {loadingOverview && Array.from({ length: 6 }).map((_, i) => (
          <div key={i} style={{ ...card(C), padding: "14px 16px", minHeight: 92 }}>
            <p style={{ margin: 0, fontSize: 11, color: C.textSub, fontWeight: 800 }}>—</p>
            <p style={{ margin: "8px 0 4px", fontSize: 22, color: C.textSub }}>…</p>
          </div>
        ))}
        {!loadingOverview && Object.keys(TILE_LABELS).map((k) => (
          <Tile key={k} tileKey={k} data={tiles[k]} C={C} />
        ))}
      </div>

      {/* ─── Tabs ───────────────────────────────────────────────── */}
      <div style={{
        display: "flex", gap: 4, borderBottom: `1px solid ${C.border}`,
        marginBottom: 18, overflowX: "auto",
      }}>
        {TABS.map((t) => {
          const on = tab === t.k;
          return (
            <button key={t.k} onClick={() => setTab(t.k)}
              style={{
                padding: "12px 18px", border: "none", background: "transparent",
                color: on ? C.brand : C.textSub,
                fontWeight: on ? 700 : 600, fontSize: 13.5,
                cursor: "pointer", fontFamily: "inherit",
                borderBottom: on ? `2px solid ${C.brand}` : "2px solid transparent",
                marginBottom: -1, whiteSpace: "nowrap",
              }}>{t.label}</button>
          );
        })}
      </div>

      {tab === "mfa"      && <MfaEnforcement      C={C} isAdmin={isAdmin} showToast={showToast} onChange={refreshOverview} />}
      {tab === "sso"      && <SsoIdentity         C={C} isAdmin={isAdmin} showToast={showToast} onChange={refreshOverview} />}
      {tab === "password" && <PasswordPolicyTab   C={C} isAdmin={isAdmin} showToast={showToast} onChange={refreshOverview} />}
      {tab === "sessions" && <SessionsTab         C={C} isAdmin={isAdmin} showToast={showToast} onChange={refreshOverview} />}
      {tab === "domains"  && <TrustedDomainsTab   C={C} isAdmin={isAdmin} showToast={showToast} onChange={refreshOverview} />}
      {tab === "exc"      && <ExceptionsTab       C={C} isAdmin={isAdmin} showToast={showToast} onChange={refreshOverview} />}
      {tab === "events"   && <EventsTab           C={C} />}

      {/* ─── Sticky footer ──────────────────────────────────────── */}
      <div style={{
        position: "sticky", bottom: 0, marginTop: 22, background: C.surface,
        border: `1px solid ${C.border}`, borderRadius: 10, padding: "12px 16px",
        display: "flex", justifyContent: "space-between", alignItems: "center",
        gap: 12, flexWrap: "wrap", boxShadow: "0 -4px 16px rgba(0,0,0,.04)",
      }}>
        <span style={{ color: C.textSub, fontSize: 12, fontWeight: 600 }}>
          Security settings updated. Changes are logged in Audit & Compliance.
        </span>
        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={refreshOverview} style={ghostBtn(C)}>Refresh</button>
          <button onClick={exportEvidence} style={ghostBtn(C)}>Export evidence</button>
          <button onClick={secureWorkspace} style={primaryBtn()} disabled={!isAdmin}>Save changes</button>
        </div>
      </div>
    </div>
  );
}

// Export with the legacy name so existing imports in SettingsPage.jsx keep working.
export default SecurityCenter;


// ──────────────────────────────────────────────────────────────────────
// 1. MFA enforcement tab — reuses access-governance/mfa-controls so the
//    tier matrix is the single source of truth.
// ──────────────────────────────────────────────────────────────────────
function MfaEnforcement({ C, isAdmin, showToast, onChange }) {
  const [cfg, setCfg] = useState(null);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    try { setCfg(await wsGet("/api/workspace/access-governance/mfa-controls")); }
    catch (e) { showToast?.(e?.message || "Failed to load MFA controls", "error"); }
  };
  useEffect(() => { load(); }, []);

  const save = async (patch) => {
    setSaving(true);
    try {
      const next = await wsSend("PUT", "/api/workspace/access-governance/mfa-controls", patch);
      setCfg(next);
      showToast?.("MFA enforcement saved");
      onChange?.();
    } catch (e) { showToast?.(e?.message || "Failed to save MFA controls", "error"); }
    finally { setSaving(false); }
  };

  if (!cfg) return <p style={{ color: C.textSub }}>Loading…</p>;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ ...card(C), padding: "18px 20px" }}>
        <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>MFA enforcement by tier</h3>
        <p style={{ margin: "4px 0 14px", fontSize: 12.5, color: C.textSub }}>
          Users on any selected tier must enroll a verified factor before logging in.
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {(cfg.tiers || []).map((t) => {
            const active = (cfg.required_tiers || []).includes(t.code);
            return (
              <button key={t.code}
                onClick={() => {
                  if (!isAdmin) return;
                  const next = new Set(cfg.required_tiers || []);
                  if (active) next.delete(t.code); else next.add(t.code);
                  save({ required_tiers: Array.from(next) });
                }}
                disabled={!isAdmin || saving}
                style={{
                  padding: "8px 14px", borderRadius: 999,
                  border: `1px solid ${active ? "#10B981" : C.border}`,
                  background: active ? "#ECFDF5" : C.surface,
                  color: active ? "#047857" : C.text,
                  fontWeight: 700, fontSize: 12.5, cursor: isAdmin ? "pointer" : "default",
                  fontFamily: "inherit",
                }}>{t.label}</button>
            );
          })}
        </div>
      </div>

      <div style={{ ...card(C), padding: "18px 20px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <div>
          <label style={{ fontSize: 12.5, fontWeight: 700, color: C.textSub, display: "block", marginBottom: 6 }}>
            Enrollment grace period (days)
          </label>
          <input type="number" min={0} max={90}
            value={cfg.enforce_grace_days}
            onChange={(e) => setCfg({ ...cfg, enforce_grace_days: parseInt(e.target.value || "0", 10) })}
            onBlur={() => save({ enforce_grace_days: cfg.enforce_grace_days })}
            disabled={!isAdmin || saving}
            style={{
              width: "100%", padding: "9px 12px", borderRadius: 8,
              border: `1px solid ${C.border}`, background: C.surface, color: C.text,
              fontSize: 13, fontFamily: "inherit",
            }} />
          <p style={{ margin: "4px 0 0", fontSize: 11, color: C.textLight }}>
            Days a new user has to enroll before access is blocked.
          </p>
        </div>
        <div>
          <label style={{ fontSize: 12.5, fontWeight: 700, color: C.textSub, display: "block", marginBottom: 6 }}>
            Allow admin-granted MFA exceptions
          </label>
          <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "9px 0" }}>
            <Switch C={C} on={!!cfg.allow_exceptions} disabled={!isAdmin || saving}
              onChange={(v) => save({ allow_exceptions: v })} />
            <span style={{ color: C.text, fontSize: 13, fontWeight: 600 }}>
              {cfg.allow_exceptions ? "Exceptions allowed" : "Strict — no exceptions"}
            </span>
          </div>
          <p style={{ margin: "4px 0 0", fontSize: 11, color: C.textLight }}>
            Every exception is recorded in the audit trail.
          </p>
        </div>
      </div>
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────
// 2. SSO & identity tab
// ──────────────────────────────────────────────────────────────────────
function SsoIdentity({ C, isAdmin, showToast, onChange }) {
  const [cfg, setCfg] = useState(null);

  const load = async () => {
    try { setCfg(await wsGet("/api/workspace/security-center/sso")); }
    catch (e) { showToast?.(e?.message || "Failed to load SSO config", "error"); }
  };
  useEffect(() => { load(); }, []);

  const save = async (patch) => {
    try {
      const next = await wsSend("PUT", "/api/workspace/security-center/sso", patch);
      setCfg(next);
      showToast?.("SSO settings saved");
      onChange?.();
    } catch (e) { showToast?.(e?.message || "Failed to save SSO", "error"); }
  };

  if (!cfg) return <p style={{ color: C.textSub }}>Loading…</p>;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      <div style={{ ...card(C), padding: "18px 20px" }}>
        <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>Identity provider</h3>
        <p style={{ margin: "4px 0 14px", fontSize: 12.5, color: C.textSub }}>
          Pick the IdP this workspace federates with.
        </p>
        <div style={{ display: "grid", gap: 10 }}>
          {[
            { code: "",          label: "None — local password login only" },
            { code: "google",    label: "Google Workspace" },
            { code: "microsoft", label: "Microsoft Entra ID" },
            { code: "okta",      label: "Okta" },
            { code: "saml",      label: "Generic SAML 2.0" },
          ].map((p) => {
            const on = (cfg.provider || "") === p.code;
            return (
              <label key={p.code || "none"} style={{
                display: "flex", gap: 10, alignItems: "center",
                padding: "9px 12px", borderRadius: 8,
                border: `1px solid ${on ? "#10B981" : C.border}`,
                background: on ? "#ECFDF5" : "transparent",
                cursor: isAdmin ? "pointer" : "default",
              }}>
                <input type="radio" name="idp" checked={on} disabled={!isAdmin}
                  onChange={() => save({ provider: p.code })} />
                <span style={{ color: C.text, fontWeight: 600, fontSize: 13 }}>{p.label}</span>
              </label>
            );
          })}
        </div>
      </div>

      <div style={{ ...card(C), padding: "18px 20px" }}>
        <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>Federation policy</h3>
        <p style={{ margin: "4px 0 14px", fontSize: 12.5, color: C.textSub }}>
          How federated identity is enforced.
        </p>
        <Row label="Require SSO for admins"
             hint="Admin/Owner/HR can only sign in via the chosen IdP."
             C={C}>
          <Switch C={C} on={!!cfg.enforce_for_admins} disabled={!isAdmin || !cfg.provider}
            onChange={(v) => save({ enforce_for_admins: v })} />
        </Row>
        <Row label="Auto-provision new users"
             hint="Create a worker account on first successful SSO login."
             C={C}>
          <Switch C={C} on={!!cfg.auto_provision} disabled={!isAdmin || !cfg.provider}
            onChange={(v) => save({ auto_provision: v })} />
        </Row>
        <div style={{ marginTop: 14 }}>
          <label style={{ fontSize: 12.5, fontWeight: 700, color: C.textSub, display: "block", marginBottom: 6 }}>
            Default role for JIT-provisioned users
          </label>
          <select value={cfg.jit_default_role || "employee"}
            disabled={!isAdmin}
            onChange={(e) => save({ jit_default_role: e.target.value })}
            style={{
              width: "100%", padding: "9px 12px", borderRadius: 8,
              border: `1px solid ${C.border}`, background: C.surface, color: C.text,
              fontSize: 13, fontFamily: "inherit",
            }}>
            <option value="employee">Worker</option>
            <option value="people_manager">People Manager</option>
            <option value="hr">HR Administrator</option>
            <option value="admin">Administrator</option>
          </select>
        </div>
      </div>
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────
// 3. Password policy tab
// ──────────────────────────────────────────────────────────────────────
function PasswordPolicyTab({ C, isAdmin, showToast, onChange }) {
  const [cfg, setCfg] = useState(null);

  const load = async () => {
    try { setCfg(await wsGet("/api/workspace/security-center/password-policy")); }
    catch (e) { showToast?.(e?.message || "Failed to load password policy", "error"); }
  };
  useEffect(() => { load(); }, []);

  const patch = async (delta) => {
    const optimistic = { ...cfg, ...delta };
    setCfg(optimistic);
    try {
      const next = await wsSend("PUT", "/api/workspace/security-center/password-policy", delta);
      setCfg(next);
      showToast?.("Password policy updated");
      onChange?.();
    } catch (e) {
      showToast?.(e?.message || "Failed to save", "error");
      load();
    }
  };

  if (!cfg) return <p style={{ color: C.textSub }}>Loading…</p>;

  const strengthBadge = cfg.min_length >= 12 && cfg.require_uppercase && cfg.require_number && cfg.require_special && cfg.block_breached
    ? <Pill C={C} tone="success">Strong</Pill>
    : cfg.min_length >= 8 && cfg.require_uppercase && cfg.require_number
      ? <Pill C={C} tone="warning">Moderate</Pill>
      : <Pill C={C} tone="danger">Weak</Pill>;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      <div style={{ ...card(C), padding: "18px 20px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>Password policy</h3>
          {strengthBadge}
        </div>
        <div style={{ marginTop: 14, display: "grid", gap: 12 }}>
          <Row label="Minimum length" C={C}>
            <input type="number" min={6} max={64} value={cfg.min_length}
              onChange={(e) => setCfg({ ...cfg, min_length: parseInt(e.target.value || "0", 10) })}
              onBlur={() => patch({ min_length: cfg.min_length })}
              disabled={!isAdmin}
              style={{ width: 70, padding: "6px 10px", borderRadius: 8, border: `1px solid ${C.border}`, fontFamily: "inherit", background: C.surface, color: C.text, fontSize: 13, textAlign: "center" }} />
            <span style={{ color: C.textSub, fontSize: 12.5 }}>characters</span>
          </Row>
          <Row label="Require uppercase letter" C={C}>
            <Switch C={C} on={cfg.require_uppercase} disabled={!isAdmin}
              onChange={(v) => patch({ require_uppercase: v })} />
          </Row>
          <Row label="Require number" C={C}>
            <Switch C={C} on={cfg.require_number} disabled={!isAdmin}
              onChange={(v) => patch({ require_number: v })} />
          </Row>
          <Row label="Require special character" C={C}>
            <Switch C={C} on={cfg.require_special} disabled={!isAdmin}
              onChange={(v) => patch({ require_special: v })} />
          </Row>
          <Row label="Password history" C={C}>
            <input type="number" min={0} max={24} value={cfg.history_count}
              onChange={(e) => setCfg({ ...cfg, history_count: parseInt(e.target.value || "0", 10) })}
              onBlur={() => patch({ history_count: cfg.history_count })}
              disabled={!isAdmin}
              style={{ width: 70, padding: "6px 10px", borderRadius: 8, border: `1px solid ${C.border}`, fontFamily: "inherit", background: C.surface, color: C.text, fontSize: 13, textAlign: "center" }} />
            <span style={{ color: C.textSub, fontSize: 12.5 }}>last passwords</span>
          </Row>
          <Row label="Forced rotation" C={C}>
            {cfg.force_rotation_days > 0 ? (
              <button onClick={() => patch({ force_rotation_days: 0 })} disabled={!isAdmin} style={ghostBtn(C)}>Disable</button>
            ) : (
              <button onClick={() => patch({ force_rotation_days: 90 })} disabled={!isAdmin}
                style={{ ...primaryBtn(), background: "#0F4C3A" }}>Enable (90 days)</button>
            )}
          </Row>
        </div>
      </div>

      <div style={{ ...card(C), padding: "18px 20px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>Breach detection</h3>
          <Pill C={C} tone={cfg.block_breached ? "success" : "warning"}>{cfg.block_breached ? "Active" : "Off"}</Pill>
        </div>
        <div style={{ marginTop: 14, display: "grid", gap: 12 }}>
          <Row label="Block breached passwords" hint="Checked against known breach databases (HaveIBeenPwned)." C={C}>
            <Switch C={C} on={cfg.block_breached} disabled={!isAdmin} onChange={(v) => patch({ block_breached: v })} />
          </Row>
          <Row label="Block common / dictionary passwords" hint='Rejects "password123", "qwerty", and similar.' C={C}>
            <Switch C={C} on={cfg.block_common} disabled={!isAdmin} onChange={(v) => patch({ block_common: v })} />
          </Row>
          <Row label="Warn on weak password at login" hint="Prompt users to change weak existing passwords." C={C}>
            <Switch C={C} on={cfg.warn_on_weak} disabled={!isAdmin} onChange={(v) => patch({ warn_on_weak: v })} />
          </Row>
          <Row label="Force reset on confirmed breach" hint="Immediately lock account on breach match." C={C}>
            <Switch C={C} on={cfg.force_reset_on_breach} disabled={!isAdmin} onChange={(v) => patch({ force_reset_on_breach: v })} />
          </Row>
        </div>

        <div style={{ ...card(C), marginTop: 14, padding: 14, background: "#ECFDF5", borderColor: "#A7F3D0" }}>
          <p style={{ margin: 0, fontSize: 12.5, fontWeight: 700, color: "#047857" }}>Breach warning copy</p>
          <textarea
            value={cfg.breach_warning_copy || ""}
            onChange={(e) => setCfg({ ...cfg, breach_warning_copy: e.target.value })}
            onBlur={() => patch({ breach_warning_copy: cfg.breach_warning_copy })}
            disabled={!isAdmin}
            rows={3}
            style={{
              width: "100%", marginTop: 8, padding: "9px 12px", borderRadius: 8,
              border: `1px solid #A7F3D0`, background: "#fff", color: "#065F46",
              fontSize: 12.5, fontFamily: "inherit", resize: "vertical",
            }} />
        </div>
      </div>
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────
// 4. Sessions & devices tab
// ──────────────────────────────────────────────────────────────────────
function SessionsTab({ C, isAdmin, showToast, onChange }) {
  const [cfg, setCfg] = useState(null);
  const [sessions, setSessions] = useState([]);

  const load = async () => {
    try {
      const [p, s] = await Promise.all([
        wsGet("/api/workspace/security-center/sessions-policy"),
        wsGet("/api/workspace/security/sessions"),
      ]);
      setCfg(p);
      setSessions(s.items || []);
    } catch (e) { showToast?.(e?.message || "Failed to load sessions", "error"); }
  };
  useEffect(() => { load(); }, []);

  const patch = async (delta) => {
    setCfg({ ...cfg, ...delta });
    try {
      const next = await wsSend("PUT", "/api/workspace/security-center/sessions-policy", delta);
      setCfg(next);
      showToast?.("Sessions policy updated");
      onChange?.();
    } catch (e) {
      showToast?.(e?.message || "Failed to save", "error");
      load();
    }
  };

  const revoke = async (id) => {
    try {
      await wsSend("POST", `/api/workspace/security/sessions/${id}/revoke`);
      showToast?.("Session revoked");
      load();
    } catch (e) { showToast?.(e?.message || "Failed to revoke session", "error"); }
  };

  if (!cfg) return <p style={{ color: C.textSub }}>Loading…</p>;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ ...card(C), padding: "18px 20px" }}>
        <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>Sessions & devices</h3>
        <p style={{ margin: "4px 0 14px", fontSize: 12.5, color: C.textSub }}>
          {cfg.active_sessions} active session{cfg.active_sessions === 1 ? "" : "s"} across the workspace.
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          <Row label="Idle timeout (minutes)"
               hint="Sign out after this many minutes of inactivity. 0 = disabled."
               C={C}>
            <input type="number" min={0} max={2880} value={cfg.idle_minutes}
              onChange={(e) => setCfg({ ...cfg, idle_minutes: parseInt(e.target.value || "0", 10) })}
              onBlur={() => patch({ idle_minutes: cfg.idle_minutes })}
              disabled={!isAdmin}
              style={{ width: 90, padding: "6px 10px", borderRadius: 8, border: `1px solid ${C.border}`, fontFamily: "inherit", background: C.surface, color: C.text, fontSize: 13, textAlign: "center" }} />
          </Row>
          <Row label="Absolute timeout (hours)"
               hint="Force re-auth after this many hours, regardless of activity."
               C={C}>
            <input type="number" min={0} max={720} value={cfg.absolute_hours}
              onChange={(e) => setCfg({ ...cfg, absolute_hours: parseInt(e.target.value || "0", 10) })}
              onBlur={() => patch({ absolute_hours: cfg.absolute_hours })}
              disabled={!isAdmin}
              style={{ width: 90, padding: "6px 10px", borderRadius: 8, border: `1px solid ${C.border}`, fontFamily: "inherit", background: C.surface, color: C.text, fontSize: 13, textAlign: "center" }} />
          </Row>
          <Row label='Allow "Remember this device"'
               hint="Workers can stay signed in across browser restarts."
               C={C}>
            <Switch C={C} on={cfg.allow_remember_device} disabled={!isAdmin}
              onChange={(v) => patch({ allow_remember_device: v })} />
          </Row>
          <Row label="Revoke sessions on role change"
               hint="Force re-login when a worker's role is changed."
               C={C}>
            <Switch C={C} on={cfg.revoke_on_role_change} disabled={!isAdmin}
              onChange={(v) => patch({ revoke_on_role_change: v })} />
          </Row>
        </div>
      </div>

      <div style={{ ...card(C), padding: 0, overflow: "hidden" }}>
        <div style={{ padding: "14px 18px", borderBottom: `1px solid ${C.border}` }}>
          <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: C.text }}>Active sessions</h3>
        </div>
        {sessions.length === 0 ? (
          <p style={{ padding: 24, color: C.textSub, textAlign: "center" }}>No active sessions.</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr>
                {["User", "Device", "Location / IP", "Last active", "Started", "Action"].map((h) => (
                  <th key={h} style={th(C)}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id} style={{ borderTop: `1px solid ${C.borderLight || C.border}` }}>
                  <td style={td(C)}>
                    <p style={{ margin: 0, color: C.text, fontWeight: 700 }}>{s.user_name || s.user_email}</p>
                    <p style={{ margin: "2px 0 0", color: C.textSub, fontSize: 12 }}>{s.user_email}</p>
                  </td>
                  <td style={td(C)}>{s.device_name || s.os || "—"}</td>
                  <td style={td(C)}>{s.location || s.ip_address || "—"}</td>
                  <td style={td(C)}>{fmtRelative(s.last_active_at)}</td>
                  <td style={td(C)}>{fmtDateTime(s.created_at)}</td>
                  <td style={td(C)}>
                    <button disabled={!isAdmin} onClick={() => revoke(s.id)} style={ghostBtn(C)}>Revoke</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────
// 5. Trusted domains tab
// ──────────────────────────────────────────────────────────────────────
function TrustedDomainsTab({ C, isAdmin, showToast, onChange }) {
  const [data, setData] = useState(null);
  const [draft, setDraft] = useState("");

  const load = async () => {
    try { setData(await wsGet("/api/workspace/security-center/trusted-domains")); }
    catch (e) { showToast?.(e?.message || "Failed to load trusted domains", "error"); }
  };
  useEffect(() => { load(); }, []);

  const add = async () => {
    const domain = draft.trim().toLowerCase().replace(/^@/, "");
    if (!domain) return;
    try {
      await wsSend("POST", "/api/workspace/security-center/trusted-domains", { domain });
      showToast?.("Domain added · DNS verification pending");
      setDraft("");
      load();
      onChange?.();
    } catch (e) { showToast?.(e?.message || "Failed to add domain", "error"); }
  };

  const verify = async (id) => {
    try {
      await wsSend("POST", `/api/workspace/security-center/trusted-domains/${id}/verify`);
      showToast?.("Domain verified");
      load();
      onChange?.();
    } catch (e) { showToast?.(e?.message || "Verification failed", "error"); }
  };

  const remove = async (id) => {
    if (!window.confirm("Remove this trusted domain? Existing invites and workers are unaffected.")) return;
    try {
      await wsSend("DELETE", `/api/workspace/security-center/trusted-domains/${id}`);
      showToast?.("Domain removed");
      load();
      onChange?.();
    } catch (e) { showToast?.(e?.message || "Failed to remove", "error"); }
  };

  const setInvite = async (delta) => {
    try {
      const next = await wsSend("PUT", "/api/workspace/security-center/invite-restrictions", delta);
      setData(next);
      showToast?.("Invite restrictions updated");
      onChange?.();
    } catch (e) { showToast?.(e?.message || "Failed to save", "error"); }
  };

  if (!data) return <p style={{ color: C.textSub }}>Loading…</p>;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      <div style={{ ...card(C), padding: "18px 20px" }}>
        <p style={{ margin: 0, fontSize: 11, color: C.textSub, fontWeight: 800, letterSpacing: 0.4, textTransform: "uppercase" }}>
          Verified trusted domains
        </p>
        <div style={{ marginTop: 14, padding: 14, border: `1px solid ${C.border}`, borderRadius: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <p style={{ margin: 0, color: C.text, fontWeight: 700, fontSize: 14 }}>Allowed email domains</p>
            <Pill C={C} tone={data.domains.filter((d) => d.status === "verified").length ? "success" : "neutral"}>
              {data.domains.filter((d) => d.status === "verified").length} verified
            </Pill>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {data.domains.map((d) => (
              <span key={d.id}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 6,
                  padding: "5px 10px 5px 12px", borderRadius: 999,
                  border: `1px solid ${d.status === "verified" ? "#10B981" : C.border}`,
                  background: d.status === "verified" ? "#ECFDF5" : C.bg,
                  color: d.status === "verified" ? "#047857" : C.textSub,
                  fontSize: 12.5, fontWeight: 700,
                }}>
                {d.domain}
                {d.status !== "verified" && <span style={{ fontSize: 11, fontWeight: 600, opacity: 0.7 }}>(pending)</span>}
                {isAdmin && (
                  <button onClick={() => remove(d.id)}
                    title="Remove"
                    style={{ background: "transparent", border: "none", color: "inherit", cursor: "pointer", padding: 0, fontSize: 14, lineHeight: 1, marginLeft: 4 }}>×</button>
                )}
              </span>
            ))}
            {data.domains.length === 0 && (
              <span style={{ color: C.textSub, fontSize: 12.5 }}>No domains added yet.</span>
            )}
          </div>
          {isAdmin && (
            <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
              <input value={draft} onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && add()}
                placeholder="acme.com" style={{
                  flex: 1, padding: "9px 12px", borderRadius: 8,
                  border: `1px solid ${C.border}`, background: C.surface, color: C.text,
                  fontSize: 13, fontFamily: "inherit",
                }} />
              <button onClick={add} style={ghostBtn(C)}>Add domain</button>
            </div>
          )}
        </div>

        <div style={{ marginTop: 16 }}>
          <p style={{ margin: 0, fontSize: 11, color: C.textSub, fontWeight: 800, letterSpacing: 0.4, textTransform: "uppercase" }}>
            Invite restrictions
          </p>
          <div style={{ marginTop: 12, display: "grid", gap: 10 }}>
            <Row label="Restrict invites to trusted domains only"
                 hint="Blocks worker invitations to personal email addresses." C={C}>
              <Switch C={C} on={data.invite_restrictions.trusted_only} disabled={!isAdmin}
                onChange={(v) => setInvite({ trusted_only: v })} />
            </Row>
            <Row label="Warn on outside-domain invites"
                 hint="Show confirmation step but allow if permitted." C={C}>
              <Switch C={C} on={data.invite_restrictions.warn_outside} disabled={!isAdmin}
                onChange={(v) => setInvite({ warn_outside: v })} />
            </Row>
            <Row label="Allow personal email for contractors"
                 hint="Exemption for non-employee worker types only." C={C}>
              <Switch C={C} on={data.invite_restrictions.allow_personal_for_contractors} disabled={!isAdmin}
                onChange={(v) => setInvite({ allow_personal_for_contractors: v })} />
            </Row>
          </div>
        </div>
      </div>

      <div style={{ ...card(C), padding: "18px 20px" }}>
        <p style={{ margin: 0, fontSize: 11, color: C.textSub, fontWeight: 800, letterSpacing: 0.4, textTransform: "uppercase" }}>
          Domain verification status
        </p>
        {data.domains.length === 0 ? (
          <p style={{ color: C.textSub, marginTop: 18 }}>Add a domain to start verifying.</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, marginTop: 10 }}>
            <thead>
              <tr>
                {["Domain", "Status", "Verified", "Action"].map((h) => <th key={h} style={th(C)}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {data.domains.map((d) => (
                <tr key={d.id} style={{ borderTop: `1px solid ${C.borderLight || C.border}` }}>
                  <td style={td(C)}><span style={{ color: C.text, fontWeight: 700 }}>{d.domain}</span></td>
                  <td style={td(C)}>
                    {d.status === "verified"
                      ? <Pill C={C} tone="success">Verified</Pill>
                      : <Pill C={C} tone="warning">Pending DNS</Pill>}
                  </td>
                  <td style={td(C)}>{d.verified_at ? fmtDate(d.verified_at) : "—"}</td>
                  <td style={td(C)}>
                    {d.status === "verified" ? (
                      <button onClick={() => remove(d.id)} disabled={!isAdmin} style={ghostBtn(C)}>Manage</button>
                    ) : (
                      <button onClick={() => verify(d.id)} disabled={!isAdmin} style={ghostBtn(C)}>Verify</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div style={{ marginTop: 14, padding: "10px 14px", background: "#ECFDF5", border: "1px solid #A7F3D0", borderRadius: 10, color: "#065F46", fontSize: 12, lineHeight: 1.55 }}>
          Domain verification uses a DNS TXT record. Changes take effect after DNS propagation (up to 48 hours). Trusted domain policy prevents uncontrolled workspace expansion.
        </div>
      </div>
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────
// 6. Exceptions & break-glass tab
// ──────────────────────────────────────────────────────────────────────
function ExceptionsTab({ C, isAdmin, showToast, onChange }) {
  const [exceptions, setExceptions] = useState([]);
  const [breakGlass, setBreakGlass] = useState([]);
  const [showExceptionModal, setShowExceptionModal] = useState(false);
  const [showBreakGlassModal, setShowBreakGlassModal] = useState(false);

  const load = async () => {
    try {
      const [exc, bg] = await Promise.all([
        wsGet("/api/workspace/security-center/exceptions"),
        wsGet("/api/workspace/security-center/break-glass"),
      ]);
      setExceptions(exc.items || []);
      setBreakGlass(bg.items || []);
    } catch (e) { showToast?.(e?.message || "Failed to load exceptions", "error"); }
  };
  useEffect(() => { load(); }, []);

  const release = async (id) => {
    if (!window.confirm("Release this exception? Standard controls will re-apply.")) return;
    try {
      await wsSend("DELETE", `/api/workspace/security-center/exceptions/${id}`);
      showToast?.("Exception released");
      load(); onChange?.();
    } catch (e) { showToast?.(e?.message || "Failed to release", "error"); }
  };

  const revokeBg = async (id) => {
    if (!window.confirm("Revoke this break-glass account? It can no longer be used to recover the workspace.")) return;
    try {
      await wsSend("DELETE", `/api/workspace/security-center/break-glass/${id}`);
      showToast?.("Account revoked");
      load(); onChange?.();
    } catch (e) { showToast?.(e?.message || "Failed to revoke", "error"); }
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ background: "#FFFBEB", border: "1px solid #FDE68A", borderRadius: 10, padding: "10px 14px", color: "#92400E", fontSize: 12.5, lineHeight: 1.5 }}>
        Exceptions reduce workspace protection. Use only with a named owner, clear reason, expiry date, and approval evidence. Every exception creates an immutable audit event.
      </div>

      <div style={{ ...card(C), padding: 0, overflow: "hidden" }}>
        <div style={{ padding: "14px 18px", borderBottom: `1px solid ${C.border}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <p style={{ margin: 0, fontSize: 11, color: C.textSub, fontWeight: 800, letterSpacing: 0.4, textTransform: "uppercase" }}>Active exceptions</p>
          <button onClick={() => setShowExceptionModal(true)} disabled={!isAdmin} style={ghostBtn(C)}>Create exception</button>
        </div>
        {exceptions.filter((x) => x.status !== "released").length === 0 ? (
          <p style={{ padding: 24, color: C.textSub, textAlign: "center" }}>No active exceptions.</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr>
                {["Control", "Role scope", "Reason", "Owner", "Expiry", "Status", "Last used", "Action"].map((h) => (
                  <th key={h} style={th(C)}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {exceptions.filter((x) => x.status !== "released").map((x) => (
                <tr key={x.id} style={{ borderTop: `1px solid ${C.borderLight || C.border}` }}>
                  <td style={td(C)}><span style={{ color: C.text, fontWeight: 700 }}>{x.control}</span></td>
                  <td style={td(C)}>{x.role_scope ? <Pill C={C} tone="neutral">{x.role_scope}</Pill> : "—"}</td>
                  <td style={{ ...td(C), maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={x.reason}>{x.reason}</td>
                  <td style={td(C)}>{x.owner || "—"}</td>
                  <td style={td(C)}>
                    {x.expires_at ? (
                      <span style={{ color: x.status === "expiring_soon" || x.status === "expired" ? "#B91C1C" : C.textSub, fontWeight: 600 }}>
                        {fmtDate(x.expires_at)}{x.status === "expiring_soon" ? " ⚠" : ""}
                      </span>
                    ) : "—"}
                  </td>
                  <td style={td(C)}>
                    {x.status === "expiring_soon"
                      ? <Pill C={C} tone="warning">Expiring soon</Pill>
                      : x.status === "expired"
                        ? <Pill C={C} tone="danger">Expired</Pill>
                        : <Pill C={C} tone="success">Active</Pill>}
                  </td>
                  <td style={td(C)}>{x.last_used_at ? fmtRelative(x.last_used_at) : "—"}</td>
                  <td style={td(C)}>
                    <button onClick={() => release(x.id)} disabled={!isAdmin} style={{ ...ghostBtn(C), color: "#B91C1C" }}>Release</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div>
        <p style={{ margin: "0 0 10px", fontSize: 11, color: C.textSub, fontWeight: 800, letterSpacing: 0.4, textTransform: "uppercase" }}>Break-glass accounts</p>
        <div style={{ ...card(C), padding: "18px 20px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: C.text }}>Emergency recovery accounts</h3>
            <Pill C={C} tone="neutral">{breakGlass.filter((b) => b.active).length} configured</Pill>
          </div>
          <div style={{ marginTop: 12, background: "#ECFDF5", border: "1px solid #A7F3D0", borderRadius: 10, padding: "10px 14px", color: "#065F46", fontSize: 12.5, lineHeight: 1.55 }}>
            Break-glass accounts must be explicitly named, limited in scope, excluded from SSO dependency, and reviewed on a regular cadence. Every use creates an immediate owner alert and audit event.
          </div>
          <div style={{ marginTop: 14, display: "flex", gap: 10 }}>
            <button onClick={() => setShowBreakGlassModal(true)} disabled={!isAdmin}
              style={{ ...primaryBtn(), background: "#0F4C3A" }}>Create break-glass account</button>
            <button onClick={() => window.open("https://docs.temveris.example/security/recovery", "_blank")}
              style={ghostBtn(C)}>View recovery policy</button>
          </div>
          {breakGlass.length > 0 && (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, marginTop: 14 }}>
              <thead>
                <tr>
                  {["Name", "Email", "Scope", "Last used", "Status", "Action"].map((h) => <th key={h} style={th(C)}>{h}</th>)}
                </tr>
              </thead>
              <tbody>
                {breakGlass.map((b) => (
                  <tr key={b.id} style={{ borderTop: `1px solid ${C.borderLight || C.border}` }}>
                    <td style={td(C)}><span style={{ color: C.text, fontWeight: 700 }}>{b.name}</span></td>
                    <td style={td(C)}>{b.email}</td>
                    <td style={td(C)}>{b.scope || "—"}</td>
                    <td style={td(C)}>{b.last_used_at ? fmtRelative(b.last_used_at) : "Never"}</td>
                    <td style={td(C)}>{b.active ? <Pill C={C} tone="success">Active</Pill> : <Pill C={C} tone="neutral">Revoked</Pill>}</td>
                    <td style={td(C)}>
                      {b.active && <button onClick={() => revokeBg(b.id)} disabled={!isAdmin} style={{ ...ghostBtn(C), color: "#B91C1C" }}>Revoke</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {showExceptionModal && (
        <ExceptionModal C={C} onClose={() => setShowExceptionModal(false)} onSubmit={async (form) => {
          try {
            await wsSend("POST", "/api/workspace/security-center/exceptions", form);
            showToast?.("Exception created");
            setShowExceptionModal(false);
            load(); onChange?.();
          } catch (e) { showToast?.(e?.message || "Failed to create", "error"); }
        }} />
      )}
      {showBreakGlassModal && (
        <BreakGlassModal C={C} onClose={() => setShowBreakGlassModal(false)} onSubmit={async (form) => {
          try {
            await wsSend("POST", "/api/workspace/security-center/break-glass", form);
            showToast?.("Break-glass account created");
            setShowBreakGlassModal(false);
            load(); onChange?.();
          } catch (e) { showToast?.(e?.message || "Failed to create", "error"); }
        }} />
      )}
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────
// 7. Security events tab
// ──────────────────────────────────────────────────────────────────────
function EventsTab({ C }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [since, setSince] = useState(7);
  const [eventType, setEventType] = useState("all");

  const load = async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({ since_days: String(since) });
      if (eventType !== "all") qs.set("event_type", eventType);
      const d = await wsGet(`/api/workspace/security-center/events?${qs.toString()}`);
      setItems(d.items || []);
    } catch (_e) {
      setItems([]);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [since, eventType]);

  const riskTone = (r) => r === "High" || r === "Critical" ? "danger" : r === "Medium" ? "warning" : "info";

  return (
    <div style={{ ...card(C), padding: 0, overflow: "hidden" }}>
      <div style={{ padding: "14px 18px", borderBottom: `1px solid ${C.border}`, display: "flex", justifyContent: "space-between", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <div>
          <p style={{ margin: 0, fontSize: 11, color: C.textSub, fontWeight: 800, letterSpacing: 0.4, textTransform: "uppercase" }}>Recent security events</p>
          <p style={{ margin: "4px 0 0", fontSize: 12.5, color: C.textSub }}>Preview of high-risk events. Full immutable log lives in Audit & Compliance.</p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <select value={eventType} onChange={(e) => setEventType(e.target.value)}
            style={{ padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, fontFamily: "inherit" }}>
            <option value="all">All event types</option>
            <option value="Auth">Auth</option>
            <option value="MFA">MFA</option>
            <option value="Policy">Policy</option>
            <option value="TrustedDomain">Trusted domain</option>
            <option value="Exception">Exception</option>
            <option value="BreakGlass">Break-glass</option>
          </select>
          <select value={since} onChange={(e) => setSince(parseInt(e.target.value, 10))}
            style={{ padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, fontFamily: "inherit" }}>
            <option value={1}>Last 24 hours</option>
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
          <button onClick={() => window.location.assign("/settings/audit-compliance")} style={ghostBtn(C)}>Open Audit & Compliance</button>
        </div>
      </div>
      {loading ? (
        <p style={{ padding: 28, color: C.textSub, textAlign: "center" }}>Loading…</p>
      ) : items.length === 0 ? (
        <p style={{ padding: 28, color: C.textSub, textAlign: "center" }}>No security events in this window.</p>
      ) : (
        <div>
          {items.map((ev) => (
            <div key={ev.id} style={{
              display: "flex", gap: 14, padding: "14px 18px",
              borderTop: `1px solid ${C.borderLight || C.border}`,
              alignItems: "flex-start",
            }}>
              <span style={{
                width: 9, height: 9, borderRadius: "50%", marginTop: 6,
                background: ev.risk === "High" ? "#EF4444" : ev.risk === "Medium" ? "#F59E0B" : "#3B82F6",
                flexShrink: 0,
              }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <p style={{ margin: 0, color: C.text, fontWeight: 600, fontSize: 13.5 }}>
                  <strong style={{ fontWeight: 800 }}>{ev.actor || "System"}</strong> — {ev.action}
                </p>
                <p style={{ margin: "3px 0 0", color: C.textSub, fontSize: 11.5 }}>
                  {ev.entity}
                  {ev.ip_address ? ` · IP ${ev.ip_address}` : ""}
                  {ev.payload && Object.keys(ev.payload).length > 0
                    ? ` · ${Object.entries(ev.payload).slice(0, 3).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join(" · ")}`
                    : ""}
                </p>
              </div>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4, flexShrink: 0 }}>
                <Pill C={C} tone={riskTone(ev.risk)}>{ev.risk}</Pill>
                <span style={{ color: C.textSub, fontSize: 11.5 }}>{fmtRelative(ev.occurred_at)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────
// Helpers — Row, table cells, modals
// ──────────────────────────────────────────────────────────────────────
function Row({ label, hint, children, C }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16, padding: "8px 0" }}>
      <div style={{ minWidth: 0, flex: 1 }}>
        <p style={{ margin: 0, color: C.text, fontWeight: 600, fontSize: 13.5 }}>{label}</p>
        {hint && <p style={{ margin: "2px 0 0", color: C.textSub, fontSize: 11.5, lineHeight: 1.45 }}>{hint}</p>}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>{children}</div>
    </div>
  );
}

const th = (C) => ({
  padding: "10px 14px", textAlign: "left",
  fontSize: 11, fontWeight: 700, color: C.textSub,
  textTransform: "uppercase", letterSpacing: 0.4,
  borderBottom: `1px solid ${C.border}`,
});
const td = (C) => ({
  padding: "12px 14px", fontSize: 13, color: C.text, verticalAlign: "middle",
});


function Modal({ title, onClose, children, C, width = 540 }) {
  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, zIndex: 9000, background: "rgba(15,23,42,0.55)",
      display: "flex", alignItems: "center", justifyContent: "center", padding: 16,
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        ...card(C), width, maxWidth: "100%", maxHeight: "90vh", overflowY: "auto",
        boxShadow: "0 22px 60px rgba(0,0,0,0.35)", padding: 22,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
          <h3 style={{ margin: 0, color: C.text, fontSize: 17, fontWeight: 700 }}>{title}</h3>
          <button onClick={onClose} style={{ background: "transparent", border: "none", color: C.textSub, fontSize: 22, cursor: "pointer", lineHeight: 1, fontFamily: "inherit" }}>×</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function ExceptionModal({ C, onClose, onSubmit }) {
  const [form, setForm] = useState({ control: "MFA exemption", role_scope: "", reason: "", expires_at: "" });
  const submit = () => {
    if (!form.control.trim() || form.reason.trim().length < 4) return;
    onSubmit({
      control: form.control.trim(),
      role_scope: form.role_scope.trim() || null,
      reason: form.reason.trim(),
      expires_at: form.expires_at ? new Date(form.expires_at).toISOString() : null,
    });
  };
  return (
    <Modal title="Create exception" onClose={onClose} C={C}>
      <div style={{ display: "grid", gap: 12 }}>
        <Field label="Control" C={C}>
          <select value={form.control} onChange={(e) => setForm({ ...form, control: e.target.value })} style={inp(C)}>
            <option>MFA exemption</option>
            <option>SSO exemption</option>
            <option>Password policy exemption</option>
            <option>IP allowlist exemption</option>
            <option>Session timeout exemption</option>
          </select>
        </Field>
        <Field label="Role scope (optional)" C={C}>
          <input value={form.role_scope} onChange={(e) => setForm({ ...form, role_scope: e.target.value })}
            placeholder="e.g. HR Admin" style={inp(C)} />
        </Field>
        <Field label="Reason" C={C}>
          <textarea rows={3} value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })}
            placeholder="Why is this exception required?" style={{ ...inp(C), resize: "vertical" }} />
        </Field>
        <Field label="Expires at" C={C}>
          <input type="date" value={form.expires_at}
            onChange={(e) => setForm({ ...form, expires_at: e.target.value })} style={inp(C)} />
        </Field>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button onClick={onClose} style={ghostBtn(C)}>Cancel</button>
          <button onClick={submit} style={{ ...primaryBtn(), background: "#0F4C3A" }}>Create exception</button>
        </div>
      </div>
    </Modal>
  );
}

function BreakGlassModal({ C, onClose, onSubmit }) {
  const [form, setForm] = useState({ name: "", email: "", scope: "emergency" });
  const ok = form.name.trim().length >= 2 && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(form.email.trim());
  return (
    <Modal title="Create break-glass account" onClose={onClose} C={C}>
      <div style={{ background: "#FFFBEB", border: "1px solid #FDE68A", borderRadius: 10, padding: "10px 14px", color: "#92400E", fontSize: 12.5, marginBottom: 12 }}>
        Break-glass accounts bypass SSO and MFA. Use only for documented disaster recovery scenarios.
      </div>
      <div style={{ display: "grid", gap: 12 }}>
        <Field label="Account name" C={C}>
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="e.g. Workspace recovery — primary" style={inp(C)} />
        </Field>
        <Field label="Recovery email" C={C}>
          <input value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })}
            placeholder="recovery@yourcompany.com" style={inp(C)} />
        </Field>
        <Field label="Scope" C={C}>
          <input value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}
            placeholder="e.g. emergency · workspace owner" style={inp(C)} />
        </Field>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button onClick={onClose} style={ghostBtn(C)}>Cancel</button>
          <button onClick={() => ok && onSubmit(form)} disabled={!ok}
            style={{ ...primaryBtn(), background: "#0F4C3A", opacity: ok ? 1 : 0.5 }}>Create account</button>
        </div>
      </div>
    </Modal>
  );
}

function Field({ label, children, C }) {
  return (
    <div>
      <label style={{ display: "block", fontSize: 12, fontWeight: 700, color: C.textSub, marginBottom: 5 }}>{label}</label>
      {children}
    </div>
  );
}

const inp = (C) => ({
  width: "100%", padding: "9px 12px", borderRadius: 8,
  border: `1px solid ${C.border}`, background: C.surface, color: C.text,
  fontSize: 13, fontFamily: "inherit", outline: "none", boxSizing: "border-box",
});
