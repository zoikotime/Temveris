/**
 * PlatformNotificationsTab.jsx — admin-facing org-wide notification configuration.
 *
 * Spec: Temveris™ — Notifications Tab Workspace Settings Implementation Wireframe.
 * Lives under Workspace Settings → Notifications, immediately below Leave Settings.
 *
 * Architecture:
 *   1. Notification Posture cards (Essential / Balanced / Detailed / Custom)
 *   2. Daily Digests (Morning Brief, Midday Pulse, End of Day Summary)
 *   3. Real-Time Alerts (cannot be fully disabled)
 *   4. Weekly / Monthly / Pay-Period Reports
 *   5. Delivery Channels (in-app always, email, push, SMS critical-only)
 *   6. Recipient Routing
 *   7. Quiet Hours
 *   8. Audit & Delivery Log (read-only inline list)
 *
 * Backend (graceful 404 fallback so the page works pre-launch):
 *   GET   /admin/notifications/settings
 *   PATCH /admin/notifications/settings
 *   POST  /admin/notifications/test
 *   GET   /admin/notifications/event-log
 *
 * Default posture: Balanced.
 */

import React, { useEffect, useMemo, useState, useCallback } from "react";
import { useTheme } from "../context/ThemeContext";
import { API_BASE } from "../config";

const TOKEN = () => localStorage.getItem("zt_token") || localStorage.getItem("token");

// ─── inline icon set (matches Payroll / MyData / Help modules) ──────────────
const ICON = {
  tune:      "M4 21v-7 M4 10V3 M12 21v-9 M12 8V3 M20 21v-5 M20 12V3 M1 14h6 M9 8h6 M17 16h6",
  filetext:  "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8",
  alert:     "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
  trending:  "M23 6l-9.5 9.5-5-5L1 18 M17 6h6v6",
  send:      "M22 2L11 13 M22 2l-7 20-4-9-9-4 20-7z",
  users:     "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2 M9 11a4 4 0 100-8 4 4 0 000 8 M23 21v-2a4 4 0 00-3-3.87 M16 3.13a4 4 0 010 7.75",
  moon:      "M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z",
  shield:    "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  refresh:   "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10 M1 14l4.64 4.36A9 9 0 0020.49 15",
  bell:      "M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9 M13.73 21a2 2 0 01-3.46 0",
  check:     "M20 6L9 17l-5-5",
  lock:      "M19 11H5a2 2 0 00-2 2v7a2 2 0 002 2h14a2 2 0 002-2v-7a2 2 0 00-2-2z M7 11V7a5 5 0 0110 0v4",
  inbox:     "M22 12h-6l-2 3h-4l-2-3H2 M5.45 5.11L2 12v6a2 2 0 002 2h16a2 2 0 002-2v-6l-3.45-6.89A2 2 0 0016.76 4H7.24a2 2 0 00-1.79 1.11z",
};

const Icon = ({ n, s = 16, c = "#475467", w = 2 }) => (
  <svg width={s} height={s} viewBox="0 0 24 24" fill="none"
    stroke={c} strokeWidth={w} strokeLinecap="round" strokeLinejoin="round"
    style={{ flexShrink: 0, display: "block" }}>
    {(ICON[n] || "").split(" M").map((seg, i) => (
      <path key={i} d={i === 0 ? seg : "M" + seg} />
    ))}
  </svg>
);

// ─── helpers ────────────────────────────────────────────────────────────────
const fmtRel = (iso) => {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const diff = Math.max(0, Date.now() - t);
  const sec = Math.floor(diff / 1000);
  if (sec < 60)         return "just now";
  if (sec < 3600)       return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400)      return `${Math.floor(sec / 3600)}h ago`;
  if (sec < 86400 * 7)  return `${Math.floor(sec / 86400)}d ago`;
  try { return new Date(iso).toLocaleDateString(); } catch { return "—"; }
};

// ─── catalog & posture defaults (per spec §6 / §7) ──────────────────────────
const POSTURES = ["essential", "balanced", "detailed", "custom"];

const POSTURE_DEFAULTS = {
  morning_brief:        { essential: false, balanced: true,  detailed: true  },
  midday_pulse:         { essential: false, balanced: false, detailed: true  },
  end_of_day:           { essential: false, balanced: true,  detailed: true  },
  weekly_performance:   { essential: false, balanced: true,  detailed: true  },
  compliance_snapshot:  { essential: false, balanced: true,  detailed: true  },
  payroll_readiness:    { essential: true,  balanced: true,  detailed: true  },
  monthly_executive:    { essential: false, balanced: false, detailed: true  },
  billing_usage:        { essential: true,  balanced: true,  detailed: true  },
  // Real-time alerts — cannot be fully disabled (all postures: on)
  alert_unplanned_absence:  { essential: true, balanced: true, detailed: true, locked: true },
  alert_policy_violation:   { essential: true, balanced: true, detailed: true, locked: true },
  alert_security:           { essential: true, balanced: true, detailed: true, locked: true },
  alert_payment_failed:     { essential: true, balanced: true, detailed: true, locked: true },
  alert_integration_failed: { essential: true, balanced: true, detailed: true, locked: true },
};

const DAILY_DIGESTS = [
  { key: "morning_brief", label: "Morning Brief",      time: "09:00",
    contents: "Today's scheduled shifts, approved leave, late starts, pending approvals, unresolved exceptions from yesterday." },
  { key: "midday_pulse",  label: "Midday Pulse",       time: "12:30",
    contents: "Attendance exceptions, unexpected idle concentration, high-risk policy signals, unresolved manager actions." },
  { key: "end_of_day",    label: "End of Day Summary", time: "17:30",
    contents: "Hours logged, attendance, top apps and sites, idle patterns, flagged exceptions, payroll-readiness indicators." },
];

const REALTIME_ALERTS = [
  { key: "alert_unplanned_absence",  label: "Unplanned absence",
    trigger: "Scheduled employee has not started after grace period and no leave record exists.",
    recipients: "All Admins" },
  { key: "alert_policy_violation",   label: "Material policy violation",
    trigger: "Workspace-defined threshold breached, based on configured policy severity.",
    recipients: "All Admins" },
  { key: "alert_security",           label: "Security event",
    trigger: "New admin added, MFA failure pattern, suspicious login, privilege escalation, or unusual access.",
    recipients: "Account Owner and Admins" },
  { key: "alert_payment_failed",     label: "Payment failed",
    trigger: "Latest payment attempt failed or payment method requires action.",
    recipients: "Account Owner" },
  { key: "alert_integration_failed", label: "Critical integration failure",
    trigger: "Payroll, SSO, identity provider, or time-capture integration offline.",
    recipients: "Account Owner and affected Admins" },
];

const REPORTS = [
  { key: "weekly_performance",  label: "Weekly Performance",     timing: "Monday, 09:00",
    contents: "Previous week's metrics, trend movements, team comparisons, productivity variances." },
  { key: "compliance_snapshot", label: "Compliance Snapshot",    timing: "Monday, 09:00",
    contents: "Policy adherence, audit-relevant events, retention status, unresolved compliance exceptions." },
  { key: "payroll_readiness",   label: "Payroll Readiness",      timing: "Last day of pay period, 16:00",
    contents: "Missing timesheets, unapproved hours, overtime exceptions, payroll export blockers." },
  { key: "monthly_executive",   label: "Monthly Executive Summary", timing: "1st day of month, 09:00",
    contents: "Workforce trends, cost analysis, productivity movement, exception themes, executive metrics." },
  { key: "billing_usage",       label: "Billing & Usage",        timing: "Invoice generation date",
    contents: "Invoice ready, seat usage, plan utilization, payment status, billing action required." },
];

const RECIPIENT_GROUPS = [
  { key: "operational_digests", label: "Operational digests",       defaults: "All Admins" },
  { key: "operational_alerts",  label: "Real-time operational alerts", defaults: "All Admins" },
  { key: "payroll_readiness",   label: "Payroll Readiness",         defaults: "Finance Admins, HR Admins" },
  { key: "billing_usage",       label: "Billing & Usage",           defaults: "Account Owner" },
  { key: "security_events",     label: "Security events",           defaults: "Account Owner and all Admins" },
  { key: "compliance_snapshot", label: "Compliance Snapshot",       defaults: "All Admins + designated compliance contact" },
  { key: "monthly_executive",   label: "Monthly Executive Summary", defaults: "Account Owner, Admins, exec recipient list" },
];

const DEFAULT_SETTINGS = {
  posture: "balanced",
  overrides: {},                    // { [key]: { enabled, channels, send_time, recipients } }
  channels: {                       // org-wide channel defaults
    in_app: true,                   // always on, non-negotiable
    email:  true,
    push:   false,
    sms:    false,
  },
  quiet_hours_enabled: true,
  quiet_hours_start:   "20:00",
  quiet_hours_end:     "07:00",
  quiet_hours_timezone: "workspace",
  quiet_hours_critical_bypass: true,
  custom_recipients: {},            // { [group_key]: [email,...] }
};

// ─── widgets ────────────────────────────────────────────────────────────────
function Section({ C, n, title, subtitle, icon, children }) {
  return (
    <section className="zt-pn-section" style={{
      background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r,
      boxShadow: "0 1px 2px rgba(16,24,40,0.04)", marginBottom: 16, overflow: "hidden",
    }}>
      <header style={{
        padding: "16px 24px", borderBottom: `1px solid ${C.borderLight}`,
        display: "flex", alignItems: "flex-start", gap: 12,
      }}>
        <span style={{
          width: 32, height: 32, borderRadius: 8, flexShrink: 0,
          background: C.brandBg, display: "inline-flex",
          alignItems: "center", justifyContent: "center",
        }}>
          <Icon n={icon} s={16} c={C.brand} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ fontSize: 10, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.6, margin: 0 }}>
            Section {n}
          </p>
          <h3 style={{ fontSize: 15, fontWeight: 700, color: C.text, margin: "2px 0 0" }}>{title}</h3>
          {subtitle && (
            <p style={{ fontSize: 12.5, color: C.textSub, margin: "4px 0 0", lineHeight: 1.5 }}>{subtitle}</p>
          )}
        </div>
      </header>
      <div style={{ padding: "18px 24px" }}>{children}</div>
    </section>
  );
}

function Toggle({ C, on, onChange, disabled, locked }) {
  return (
    <button type="button"
      onClick={() => !disabled && !locked && onChange?.(!on)}
      disabled={disabled || locked}
      title={locked ? "Critical alert — cannot be fully disabled" : ""}
      className="zt-pn-toggle"
      data-on={on ? "true" : "false"}
      style={{
        width: 42, height: 24, borderRadius: 999, border: "none",
        cursor: (disabled || locked) ? "not-allowed" : "pointer",
        background: on ? C.brand : C.textLight, position: "relative",
        transition: "background .22s cubic-bezier(.4,0,.2,1), box-shadow .22s ease",
        flexShrink: 0, opacity: (disabled || locked) ? 0.55 : 1, padding: 0,
        boxShadow: on ? `0 0 0 4px ${C.brand}1A` : "inset 0 0 0 1px rgba(0,0,0,.04)",
      }}>
      <span style={{
        position: "absolute", top: 2, left: on ? 20 : 2,
        width: 20, height: 20, background: "#fff", borderRadius: "50%",
        transition: "left .22s cubic-bezier(.34,1.56,.64,1)",
        boxShadow: "0 1px 3px rgba(0,0,0,0.18), 0 1px 1px rgba(0,0,0,.06)",
      }} />
    </button>
  );
}

function Pill({ C, tone = "neutral", children }) {
  const tones = {
    neutral: { bg: C.borderLight, fg: C.textSub },
    info:    { bg: C.brandBg,     fg: C.brand   },
    success: { bg: C.greenBg,     fg: C.green   },
    warn:    { bg: C.amberBg,     fg: C.amber   },
    danger:  { bg: C.dangerBg,    fg: C.danger  },
  };
  const t = tones[tone] || tones.neutral;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", padding: "2px 10px",
      borderRadius: 999, fontSize: 10, fontWeight: 700, letterSpacing: 0.4,
      textTransform: "uppercase", background: t.bg, color: t.fg,
    }}>{children}</span>
  );
}

function PostureCard({ C, value, current, onSelect, label, badge, purpose, cadence }) {
  const active = current === value;
  return (
    <button type="button" onClick={() => onSelect(value)}
      className="zt-pn-posture"
      data-active={active ? "true" : "false"}
      style={{
        position: "relative",
        display: "block", textAlign: "left", padding: 16, borderRadius: 12,
        background: active ? C.brandBg : C.surface,
        border: `2px solid ${active ? C.brand : C.border}`,
        cursor: "pointer", fontFamily: "inherit", flex: 1, minWidth: 0,
        transition: "background .18s ease, border-color .18s ease, transform .2s cubic-bezier(.34,1.56,.64,1), box-shadow .2s ease",
        boxShadow: active ? `0 4px 14px ${C.brand}1F` : "0 1px 2px rgba(16,24,40,.04)",
      }}>
      {active && (
        <span style={{
          position: "absolute", top: 10, right: 10,
          width: 18, height: 18, borderRadius: "50%",
          background: C.brand, display: "inline-flex",
          alignItems: "center", justifyContent: "center",
        }}>
          <Icon n="check" s={11} c="#fff" w={3} />
        </span>
      )}
      {badge && (
        <span style={{
          position: "absolute",
          top: -10,
          left: 12,
          padding: "3px 10px",
          borderRadius: 999,
          fontSize: 9.5,
          fontWeight: 800,
          letterSpacing: 0.6,
          textTransform: "uppercase",
          background: C.green,
          color: "#fff",
          boxShadow: "0 2px 6px rgba(16,24,40,.15)",
          whiteSpace: "nowrap",
          lineHeight: 1.4,
        }}>{badge}</span>
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, paddingRight: active ? 26 : 0 }}>
        <span style={{ fontSize: 14, fontWeight: 800,
                       color: active ? C.brand : C.text }}>{label}</span>
      </div>
      <p style={{ fontSize: 12, color: C.textSub, margin: 0, lineHeight: 1.5 }}>{purpose}</p>
      <p style={{ fontSize: 11, color: C.textLight, margin: "8px 0 0" }}>{cadence}</p>
    </button>
  );
}

function Row({ C, title, subtitle, right, badges, last }) {
  return (
    <div className="zt-pn-row" style={{
      padding: "14px 12px",
      margin: "0 -12px",
      borderRadius: 8,
      borderBottom: last ? "none" : `1px solid ${C.borderLight}`,
      display: "flex", alignItems: "flex-start", gap: 14,
      transition: "background .15s ease",
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 4 }}>
          <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>{title}</p>
          {(badges || []).map((b, i) => <span key={i}>{b}</span>)}
        </div>
        {subtitle && (
          <p style={{ fontSize: 12.5, color: C.textSub, margin: 0, lineHeight: 1.5 }}>{subtitle}</p>
        )}
      </div>
      {right}
    </div>
  );
}

function SkeletonRow({ C, w = "60%" }) {
  return (
    <div style={{
      height: 12, width: w, borderRadius: 6,
      background: `linear-gradient(90deg, ${C.borderLight} 0%, ${C.border} 50%, ${C.borderLight} 100%)`,
      backgroundSize: "200% 100%",
      animation: "ztPnShimmer 1.4s linear infinite",
    }} />
  );
}

// ═════════════════════════════════════════════════════════════════════════════
// MAIN
// ═════════════════════════════════════════════════════════════════════════════

export default function PlatformNotificationsTab({ isAdmin, showToast }) {
  const { C } = useTheme();
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [original, setOriginal] = useState(DEFAULT_SETTINGS);
  const [loading,  setLoading]  = useState(true);
  const [saving,   setSaving]   = useState(false);
  const [eventLog, setEventLog] = useState([]);
  const [showEventLog, setShowEventLog] = useState(false);

  // ── API helpers (graceful 404 fallback) ────────────────────────────────────
  const apiCall = useCallback(async (path, options = {}) => {
    try {
      const res = await fetch(`${API_BASE}${path}`, {
        ...options,
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${TOKEN()}`,
          ...(options.headers || {}),
        },
      });
      if (!res.ok) {
        if (res.status === 404) return { __notFound: true };
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Error ${res.status}`);
      }
      return await res.json();
    } catch (e) {
      throw e;
    }
  }, []);

  // ── Initial load ───────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiCall("/admin/notifications/settings");
        if (cancelled) return;
        if (data && !data.__notFound) {
          const merged = { ...DEFAULT_SETTINGS, ...data };
          setSettings(merged);
          setOriginal(merged);
        } else {
          // Try localStorage fallback for pre-backend persistence
          try {
            const raw = localStorage.getItem("zt_admin_notif_settings");
            if (raw) {
              const parsed = JSON.parse(raw);
              const merged = { ...DEFAULT_SETTINGS, ...parsed };
              setSettings(merged);
              setOriginal(merged);
            }
          } catch {}
        }
      } catch {} finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [apiCall]);

  // ── Dirty / save ──────────────────────────────────────────────────────────
  const dirty = useMemo(
    () => JSON.stringify(settings) !== JSON.stringify(original),
    [settings, original]
  );

  const handleSave = async () => {
    if (!isAdmin) { showToast?.("You don't have permission to save notification settings", "error"); return; }
    setSaving(true);
    try {
      const res = await apiCall("/admin/notifications/settings", {
        method: "PATCH",
        body: JSON.stringify(settings),
      });
      if (res?.__notFound) {
        // Fall back to localStorage so the user's choices aren't lost while
        // the backend endpoint is being implemented.
        localStorage.setItem("zt_admin_notif_settings", JSON.stringify(settings));
        showToast?.("Saved locally — backend endpoint will pick this up when available.");
      } else {
        showToast?.("Notification settings saved");
      }
      setOriginal(settings);
    } catch (e) {
      showToast?.(e.message || "Failed to save", "error");
    } finally { setSaving(false); }
  };

  const handleDiscard = () => setSettings(original);

  const handleReset = () => {
    if (!window.confirm("Reset all overrides to posture defaults? This cannot be undone.")) return;
    setSettings(s => ({ ...s, overrides: {} }));
    showToast?.("Overrides cleared. Save to persist.");
  };

  const handleSendTest = async () => {
    try {
      const res = await apiCall("/admin/notifications/test", {
        method: "POST", body: JSON.stringify({ channels: settings.channels }),
      });
      if (res?.__notFound) {
        showToast?.("Test endpoint not yet available — coming with backend rollout.", "info");
      } else {
        showToast?.("Test notification sent to your account");
      }
    } catch (e) { showToast?.(e.message || "Couldn't send test", "error"); }
  };

  const loadEventLog = async () => {
    try {
      const res = await apiCall("/admin/notifications/event-log?limit=50");
      if (res?.__notFound) {
        setEventLog([]);
      } else {
        setEventLog(res?.events || []);
      }
      setShowEventLog(true);
    } catch { setEventLog([]); setShowEventLog(true); }
  };

  // ── Compute effective enabled state (posture default + override) ──────────
  const effective = useCallback((key) => {
    const def = POSTURE_DEFAULTS[key];
    const baseEnabled = def?.[settings.posture] ?? false;
    if (settings.posture !== "custom") return baseEnabled;
    const ov = settings.overrides[key];
    if (ov && typeof ov.enabled === "boolean") return ov.enabled;
    return def?.balanced ?? false;  // sensible custom default
  }, [settings]);

  const isLocked = (key) => !!POSTURE_DEFAULTS[key]?.locked;

  const setOverride = (key, patch) => {
    setSettings(s => {
      // Toggling any individual control implies the admin is moving off
      // a preset posture into Custom. Without this auto-switch the
      // override stored here would be ignored by `effective()` (which
      // only reads overrides when posture === "custom"), making the
      // toggles look frozen.
      const nextPosture = s.posture === "custom" ? "custom" : "custom";
      return {
        ...s,
        posture: nextPosture,
        overrides: { ...s.overrides, [key]: { ...(s.overrides[key] || {}), ...patch } },
      };
    });
  };

  const setChannel = (k, v) => setSettings(s => ({ ...s, channels: { ...s.channels, [k]: v } }));

  // ── Loading state ─────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{ padding: "24px 40px", maxWidth: 920,
                    fontFamily: "'DM Sans', system-ui, sans-serif" }}>
        <style>{`@keyframes ztPnShimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}`}</style>
        <div style={{
          background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r,
          padding: 22, marginBottom: 16, display: "flex", gap: 16, alignItems: "center",
        }}>
          <div style={{
            width: 48, height: 48, borderRadius: 12,
            background: `linear-gradient(90deg, ${C.borderLight} 0%, ${C.border} 50%, ${C.borderLight} 100%)`,
            backgroundSize: "200% 100%", animation: "ztPnShimmer 1.4s linear infinite",
          }} />
          <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
            <SkeletonRow C={C} w="40%" />
            <SkeletonRow C={C} w="70%" />
          </div>
        </div>
        {[1, 2, 3].map(i => (
          <div key={i} style={{
            background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r,
            padding: 22, marginBottom: 16, display: "flex", flexDirection: "column", gap: 12,
          }}>
            <SkeletonRow C={C} w="30%" />
            <SkeletonRow C={C} w="80%" />
            <SkeletonRow C={C} w="65%" />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div style={{
      padding: "24px 40px 96px", maxWidth: 920,
      fontFamily: "'DM Sans', system-ui, sans-serif",
    }}>
      <style>{`
        @keyframes ztPnShimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }
        @keyframes ztPnFadeIn  { from{opacity:0; transform:translateY(6px)} to{opacity:1; transform:none} }
        @keyframes ztPnSlideUp { from{opacity:0; transform:translateY(20px)} to{opacity:1; transform:none} }
        .zt-pn-section { animation: ztPnFadeIn .35s cubic-bezier(.22,1,.36,1) both; }
        .zt-pn-section:nth-child(2) { animation-delay: .04s; }
        .zt-pn-section:nth-child(3) { animation-delay: .08s; }
        .zt-pn-section:nth-child(4) { animation-delay: .12s; }
        .zt-pn-section:nth-child(5) { animation-delay: .16s; }
        .zt-pn-section:nth-child(6) { animation-delay: .20s; }
        .zt-pn-section:nth-child(7) { animation-delay: .24s; }
        .zt-pn-section:nth-child(8) { animation-delay: .28s; }
        .zt-pn-section:nth-child(9) { animation-delay: .32s; }
        /* Green hover — same accent as AdminSidebar / AdminLeave / Payroll. */
        .zt-pn-posture:hover { transform: translateY(-2px); box-shadow: 0 6px 18px ${C.brandAccent}1F; }
        .zt-pn-posture[data-active="false"]:hover { border-color: ${C.brandAccent} !important; }
        .zt-pn-row:hover { background: ${C.brandAccentBg}; }
        .zt-pn-toggle:not(:disabled):hover { filter: brightness(1.05); }
        .zt-pn-action:hover { background: ${C.brandAccentBg} !important; border-color: ${C.brandAccent} !important; color: ${C.brandAccent} !important; }
        .zt-pn-action:hover svg { stroke: ${C.brandAccent} !important; }
        .zt-pn-save-bar { animation: ztPnSlideUp .28s cubic-bezier(.22,1,.36,1) both; }
        .zt-pn-primary:not(:disabled):hover { transform: translateY(-1px); box-shadow: 0 6px 14px ${C.brandAccent}55 !important; }
      `}</style>
      {/* Header — matches Payroll Calculator hero treatment */}
      <div style={{
        background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r,
        boxShadow: "0 1px 2px rgba(16,24,40,0.04)", marginBottom: 18,
        padding: "20px 24px",
      }}>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
          <div style={{
            width: 48, height: 48, borderRadius: 12, flexShrink: 0,
            background: `linear-gradient(135deg, ${C.brand}, ${C.brandDark})`,
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            <Icon n="bell" s={22} c="#fff" w={2.2} />
          </div>
          <div style={{ flex: 1, minWidth: 240 }}>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        letterSpacing: 0.6, textTransform: "uppercase", margin: 0 }}>
              Workspace Settings · Notifications
            </p>
            <h1 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: "2px 0 0" }}>
              Notifications
            </h1>
            <p style={{ fontSize: 13, color: C.textSub, margin: "6px 0 0", lineHeight: 1.6, maxWidth: 720 }}>
              Choose what Temveris tells you about — and when.
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, flexShrink: 0, flexWrap: "wrap" }}>
            <button onClick={handleSendTest} className="zt-pn-action"
              style={{ padding: "9px 16px", borderRadius: C.rSm, border: `1px solid ${C.border}`,
                       background: C.surface, color: C.text, fontWeight: 600, fontSize: 13,
                       cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap",
                       transition: "background .18s ease, border-color .18s ease, color .18s ease",
                       display: "inline-flex", alignItems: "center", gap: 6, minHeight: 40 }}>
              <Icon n="send" s={14} c={C.text} /> Send Test
            </button>
            <button onClick={handleReset} disabled={!isAdmin} className="zt-pn-action"
              style={{ padding: "9px 16px", borderRadius: C.rSm, border: `1px solid ${C.border}`,
                       background: C.surface, color: C.text, fontWeight: 600, fontSize: 13,
                       cursor: isAdmin ? "pointer" : "not-allowed", fontFamily: "inherit", whiteSpace: "nowrap",
                       transition: "background .18s ease, border-color .18s ease, color .18s ease",
                       display: "inline-flex", alignItems: "center", gap: 6, minHeight: 40,
                       opacity: isAdmin ? 1 : 0.5 }}>
              <Icon n="refresh" s={14} c={C.text} /> Reset to Posture Defaults
            </button>
          </div>
        </div>
      </div>

      {/* ─── Section 1: Notification Posture ──────────────────────────────── */}
      <Section C={C} n={1} icon="tune" title="Notification Posture"
               subtitle="Pick the cadence that suits your team. You can switch any time.">
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <PostureCard C={C} value="essential" current={settings.posture}
            onSelect={(v) => setSettings(s => ({ ...s, posture: v }))}
            label="Essential" purpose="Critical alerts only. Minimal traffic for owners."
            cadence="≈ 2–4 / week + critical events" />
          <PostureCard C={C} value="balanced" current={settings.posture}
            onSelect={(v) => setSettings(s => ({ ...s, posture: v }))}
            label="Balanced" badge="Recommended"
            purpose="Two daily digests, critical alerts, payroll readiness, weekly intelligence."
            cadence="≈ 2 / day + critical events" />
          <PostureCard C={C} value="detailed" current={settings.posture}
            onSelect={(v) => setSettings(s => ({ ...s, posture: v }))}
            label="Detailed"
            purpose="Morning, midday, end-of-day rhythm + monthly, payroll, compliance."
            cadence="≈ 3 / day + scheduled reports" />
          <PostureCard C={C} value="custom" current={settings.posture}
            onSelect={(v) => setSettings(s => ({ ...s, posture: v }))}
            label="Custom"
            purpose="Unlocks individual controls for every notification type."
            cadence="For mature teams with defined ownership" />
        </div>
        <p style={{ fontSize: 12, color: C.textLight, margin: "14px 0 0", lineHeight: 1.5 }}>
          Default: <strong style={{ color: C.text }}>Balanced</strong>. Custom mode unlocks per-notification overrides below.
        </p>
      </Section>

      {/* ─── Section 2: Daily Digests ─────────────────────────────────────── */}
      <Section C={C} n={2} icon="filetext" title="Daily Digests"
               subtitle="Scheduled summaries delivered at fixed times in workspace timezone.">
        {DAILY_DIGESTS.map((d, i) => {
          const on = effective(d.key);
          const ov = settings.overrides[d.key] || {};
          return (
            <Row key={d.key} C={C} last={i === DAILY_DIGESTS.length - 1}
              title={d.label}
              subtitle={d.contents}
              badges={[
                <Pill C={C} tone="info">{ov.send_time || d.time}</Pill>,
              ]}
              right={
                <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6 }}>
                  <Toggle C={C} on={on}
                    onChange={(v) => setOverride(d.key, { enabled: v })}
                  />
                  <input type="time" value={ov.send_time || d.time}
                    onChange={(e) => setOverride(d.key, { send_time: e.target.value })}
                    style={{
                      padding: "4px 8px", fontSize: 11, fontFamily: "inherit",
                      border: `1px solid ${C.border}`, borderRadius: 6,
                      background: C.bg, color: C.text,
                    }} />
                </div>
              }
            />
          );
        })}
      </Section>

      {/* ─── Section 3: Real-Time Alerts ──────────────────────────────────── */}
      <Section C={C} n={3} icon="alert" title="Real-Time Alerts"
               subtitle="Critical events. These cannot be fully disabled and always appear in the in-app inbox.">
        {REALTIME_ALERTS.map((a, i) => (
          <Row key={a.key} C={C} last={i === REALTIME_ALERTS.length - 1}
            title={a.label}
            subtitle={a.trigger}
            badges={[
              <Pill C={C} tone="danger">Locked</Pill>,
              <Pill C={C} tone="neutral">Routes to {a.recipients}</Pill>,
            ]}
            right={<Toggle C={C} on={true} locked />}
          />
        ))}
      </Section>

      {/* ─── Section 4: Weekly / Monthly / Pay-Period Reports ─────────────── */}
      <Section C={C} n={4} icon="trending" title="Weekly, Monthly & Pay-Period Reports"
               subtitle="Scheduled intelligence delivered at the right cadence.">
        {REPORTS.map((r, i) => {
          const on = effective(r.key);
          return (
            <Row key={r.key} C={C} last={i === REPORTS.length - 1}
              title={r.label}
              subtitle={r.contents}
              badges={[<Pill C={C} tone="info">{r.timing}</Pill>]}
              right={
                <Toggle C={C} on={on}
                  onChange={(v) => setOverride(r.key, { enabled: v })}
                />
              }
            />
          );
        })}
      </Section>

      {/* ─── Section 5: Delivery Channels ─────────────────────────────────── */}
      <Section C={C} n={5} icon="send" title="Delivery Channels"
               subtitle="Which channels are used to deliver each notification type.">
        <Row C={C} title="In-app inbox"
          subtitle="Always on. Every notification is delivered to the in-app inbox regardless of other channel preferences."
          badges={[<Pill C={C} tone="success">Always on</Pill>]}
          right={<Toggle C={C} on={true} locked />} />
        <Row C={C} title="Email"
          subtitle="Primary launch channel for digests, reports, and critical alerts."
          right={<Toggle C={C} on={settings.channels.email}
                          onChange={(v) => setChannel("email", v)} />} />
        <Row C={C} title="Mobile push"
          subtitle="Useful for immediate action — especially real-time alerts. Off by default for digests."
          right={<Toggle C={C} on={settings.channels.push}
                          onChange={(v) => setChannel("push", v)} />} />
        <Row C={C} title="SMS" last
          subtitle="Critical only, opt-in. Used for security, payment, unplanned absence, and integration failures."
          badges={[<Pill C={C} tone="warn">Critical-only</Pill>]}
          right={<Toggle C={C} on={settings.channels.sms}
                          onChange={(v) => setChannel("sms", v)} />} />
      </Section>

      {/* ─── Section 6: Recipient Routing ─────────────────────────────────── */}
      <Section C={C} n={6} icon="users" title="Recipient Routing"
               subtitle="Who receives each notification group. Custom recipient lists appear only in Custom mode.">
        {RECIPIENT_GROUPS.map((g, i) => (
          <Row key={g.key} C={C} last={i === RECIPIENT_GROUPS.length - 1}
            title={g.label}
            subtitle={`Default: ${g.defaults}`}
            right={
              settings.posture === "custom" ? (
                <input type="text"
                  placeholder="Add custom recipients (emails)…"
                  value={(settings.custom_recipients[g.key] || []).join(", ")}
                  onChange={(e) =>
                    setSettings(s => ({
                      ...s,
                      custom_recipients: {
                        ...s.custom_recipients,
                        [g.key]: e.target.value.split(",").map(x => x.trim()).filter(Boolean),
                      },
                    }))
                  }
                  style={{
                    width: 320, padding: "8px 12px", borderRadius: 8,
                    border: `1px solid ${C.border}`, background: C.bg,
                    fontSize: 12, color: C.text, fontFamily: "inherit", outline: "none",
                  }}
                />
              ) : (
                <Pill C={C} tone="neutral">Defaults applied</Pill>
              )
            }
          />
        ))}
      </Section>

      {/* ─── Section 7: Quiet Hours ───────────────────────────────────────── */}
      <Section C={C} n={7} icon="moon" title="Quiet Hours"
               subtitle="Suppress non-critical notifications during quiet hours. Critical alerts may still be delivered.">
        <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16 }}>
          <Toggle C={C} on={settings.quiet_hours_enabled}
            onChange={(v) => setSettings(s => ({ ...s, quiet_hours_enabled: v }))} />
          <span style={{ fontSize: 14, fontWeight: 600, color: C.text }}>
            {settings.quiet_hours_enabled ? "Enabled" : "Disabled"}
          </span>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.textSub, marginBottom: 6 }}>
              Start
            </label>
            <input type="time" value={settings.quiet_hours_start}
              disabled={!settings.quiet_hours_enabled}
              onChange={(e) => setSettings(s => ({ ...s, quiet_hours_start: e.target.value }))}
              style={{
                width: "100%", padding: "9px 12px", borderRadius: 8,
                border: `1px solid ${C.border}`, background: C.bg, fontSize: 13,
                color: C.text, fontFamily: "inherit", outline: "none",
                opacity: settings.quiet_hours_enabled ? 1 : 0.5,
              }}
            />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.textSub, marginBottom: 6 }}>
              End
            </label>
            <input type="time" value={settings.quiet_hours_end}
              disabled={!settings.quiet_hours_enabled}
              onChange={(e) => setSettings(s => ({ ...s, quiet_hours_end: e.target.value }))}
              style={{
                width: "100%", padding: "9px 12px", borderRadius: 8,
                border: `1px solid ${C.border}`, background: C.bg, fontSize: 13,
                color: C.text, fontFamily: "inherit", outline: "none",
                opacity: settings.quiet_hours_enabled ? 1 : 0.5,
              }}
            />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.textSub, marginBottom: 6 }}>
              Timezone
            </label>
            <select value={settings.quiet_hours_timezone}
              disabled={!settings.quiet_hours_enabled}
              onChange={(e) => setSettings(s => ({ ...s, quiet_hours_timezone: e.target.value }))}
              style={{
                width: "100%", padding: "9px 12px", borderRadius: 8,
                border: `1px solid ${C.border}`, background: C.bg, fontSize: 13,
                color: C.text, fontFamily: "inherit", outline: "none",
                opacity: settings.quiet_hours_enabled ? 1 : 0.5,
              }}>
              <option value="workspace">Workspace timezone</option>
              <option value="utc">UTC</option>
              <option value="ist">IST (Asia/Kolkata)</option>
              <option value="est">EST (America/New_York)</option>
              <option value="pst">PST (America/Los_Angeles)</option>
              <option value="gmt">GMT (Europe/London)</option>
            </select>
          </div>
        </div>
        <label style={{ display: "flex", gap: 10, alignItems: "flex-start",
                         fontSize: 13, color: C.text, cursor: "pointer", marginTop: 14,
                         padding: 10, borderRadius: 8, background: C.bg,
                         border: `1px solid ${C.border}` }}>
          <input type="checkbox" checked={settings.quiet_hours_critical_bypass}
            onChange={(e) => setSettings(s => ({ ...s, quiet_hours_critical_bypass: e.target.checked }))}
            style={{ marginTop: 2 }} />
          <span>
            <strong>Allow critical alerts to bypass quiet hours.</strong>
            {" "}Payment failures, security events, integration failures, and unplanned absences
            for critical roles will still be delivered immediately.
          </span>
        </label>
      </Section>

      {/* ─── Section 8: Audit & Delivery Log ──────────────────────────────── */}
      <Section C={C} n={8} icon="shield" title="Audit & Delivery Log"
               subtitle="Recent notification deliveries. Every send, failure, and consolidation is recorded for audit.">
        {!showEventLog ? (
          <button onClick={loadEventLog}
            style={{ padding: "9px 16px", borderRadius: C.rSm, border: `1px solid ${C.border}`,
                     background: C.surface, color: C.brand, fontWeight: 600, fontSize: 13,
                     cursor: "pointer", fontFamily: "inherit",
                     display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Icon n="inbox" s={14} c={C.brand} /> View delivery log
          </button>
        ) : eventLog.length === 0 ? (
          <p style={{ fontSize: 13, color: C.textSub, margin: 0,
                      padding: "14px 18px", background: C.bg, borderRadius: 8 }}>
            No delivery events recorded yet. Once notifications start firing, they'll appear here.
          </p>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {eventLog.slice(0, 20).map((ev, i) => (
              <li key={ev.id || i} style={{
                padding: "10px 0", borderBottom: i < Math.min(eventLog.length, 20) - 1 ? `1px solid ${C.borderLight}` : "none",
                display: "flex", alignItems: "center", gap: 12,
              }}>
                <span style={{ fontSize: 13, color: C.text, fontWeight: 600, flex: 1 }}>
                  {ev.notification_type || "—"}
                </span>
                <Pill C={C} tone={ev.delivery_status === "delivered" ? "success" :
                                   ev.delivery_status === "failed" ? "danger" : "info"}>
                  {ev.delivery_status || "queued"}
                </Pill>
                <span style={{ fontSize: 11, color: C.textSub }}>{ev.channel}</span>
                <span style={{ fontSize: 11, color: C.textLight }}>{fmtRel(ev.sent_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </Section>

      {/* Sticky bottom save bar (visible when dirty) */}
      {dirty && (
        <div className="zt-pn-save-bar" style={{
          position: "sticky", bottom: 0, marginTop: 24,
          padding: "14px 20px", borderRadius: 14,
          background: C.surface, border: `1px solid ${C.brand}`,
          boxShadow: `0 14px 36px rgba(16,24,40,.18), 0 0 0 4px ${C.brand}14`,
          display: "flex", alignItems: "center", gap: 12,
          backdropFilter: "blur(8px)",
        }}>
          <span style={{
            width: 8, height: 8, borderRadius: "50%", background: C.brand,
            boxShadow: `0 0 0 4px ${C.brand}26`, flexShrink: 0,
          }} />
          <span style={{ fontSize: 13, color: C.text, fontWeight: 600, flex: 1 }}>
            You have unsaved notification changes
          </span>
          <button onClick={handleDiscard} disabled={saving} className="zt-pn-action"
            style={{ padding: "8px 14px", borderRadius: 8, border: `1px solid ${C.border}`,
                     background: C.surface, color: C.text, fontWeight: 600, fontSize: 13,
                     cursor: "pointer", fontFamily: "inherit",
                     transition: "background .18s ease, border-color .18s ease, color .18s ease" }}>
            Discard
          </button>
          <button onClick={handleSave} disabled={saving || !isAdmin} className="zt-pn-primary"
            style={{ padding: "9px 20px", borderRadius: C.rSm, border: "none",
                     background: saving ? C.textLight : C.brand, color: "#fff",
                     fontWeight: 700, fontSize: 13, cursor: saving ? "wait" : "pointer",
                     fontFamily: "inherit", boxShadow: !saving ? `0 2px 8px ${C.brand}33` : "none",
                     transition: "transform .18s ease, box-shadow .18s ease",
                     display: "inline-flex", alignItems: "center", gap: 6, minHeight: 40 }}>
            {!saving && <Icon n="check" s={14} c="#fff" />}
            {saving ? "Saving…" : "Save Changes"}
          </button>
        </div>
      )}
    </div>
  );
}
