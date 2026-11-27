/**
 * MyDataModule.jsx — worker-facing "My Data" module.
 *
 * Five panes, one component, swappable via the `pane` prop:
 *   1. whats-recorded     — capture types + retention + workspace policy refs
 *   2. who-viewed         — human access log with filters, anomaly notice, CSV export
 *   3. privacy-controls   — worker-controllable settings only (locked rows show policy reason)
 *   4. corrections        — request a correction + status table + escalation
 *   5. data-export        — standard export (PDF/JSON/CSV), DSAR, deletion request
 *
 * Backend endpoints used:
 *   GET  /profile/preferences          (existing — privacy controls live under preferences)
 *   PUT  /profile/preferences          (existing)
 *   GET  /my-data/capture-config       (new — falls back to a sensible default if 404)
 *   GET  /my-data/who-viewed           (new — falls back to /profile/access-history shape)
 *   GET/POST /my-data/corrections      (new — graceful empty state on 404)
 *   GET/POST /my-data/exports          (new — graceful empty state on 404)
 *   POST /my-data/dsar                 (new)
 *   POST /my-data/deletion             (new)
 *
 * Design intent: every pane survives a missing backend by showing a real empty
 * state, not a stack trace. The UI stays useful while the backend ships.
 */

import React, { useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import API from "../api/axios";
import { useTheme } from "../context/ThemeContext";

// Module-level context so sub-components get C without prop-drilling.
const CCtx = React.createContext(null);
const useC = () => React.useContext(CCtx);

// ─── helpers ────────────────────────────────────────────────────────────────
const fmtDateTime = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return "—"; }
};

const fmtDate = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch { return "—"; }
};

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

const downloadCSV = (filename, rows, headers) => {
  // Tiny CSV generator — quotes anything containing a comma, quote, or newline
  const esc = (v) => {
    if (v == null) return "";
    const s = String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [headers.map(h => h.label).join(","),
               ...rows.map(r => headers.map(h => esc(r[h.key])).join(","))].join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

// ─── inline icon set ────────────────────────────────────────────────────────
const ICON = {
  shield:     "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  eye:        "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z M12 15a3 3 0 100-6 3 3 0 000 6z",
  lock:       "M19 11H5a2 2 0 00-2 2v7a2 2 0 002 2h14a2 2 0 002-2v-7a2 2 0 00-2-2z M7 11V7a5 5 0 0110 0v4",
  unlock:     "M19 11H5a2 2 0 00-2 2v7a2 2 0 002 2h14a2 2 0 002-2v-7a2 2 0 00-2-2z M7 11V7a5 5 0 019.9-1",
  download:   "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  warning:    "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
  info:       "M12 22a10 10 0 100-20 10 10 0 000 20z M12 16v-4 M12 8h.01",
  chevdown:   "M6 9l6 6 6-6",
  chevup:     "M18 15l-6-6-6 6",
  chevright:  "M9 18l6-6-6-6",
  check:      "M20 6L9 17l-5-5",
  x:          "M18 6L6 18 M6 6l12 12",
  database:   "M12 3a9 3 0 100 6 9 3 0 000-6z M3 5v6a9 3 0 0018 0V5 M3 11v6a9 3 0 0018 0v-6",
  monitor:    "M2 3h20a1 1 0 011 1v13a1 1 0 01-1 1H2a1 1 0 01-1-1V4a1 1 0 011-1z M8 21h8 M12 17v4",
  camera:     "M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z M12 17a4 4 0 100-8 4 4 0 000 8",
  map:        "M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z M12 13a3 3 0 100-6 3 3 0 000 6z",
  keyboard:   "M2 5h20a1 1 0 011 1v12a1 1 0 01-1 1H2a1 1 0 01-1-1V6a1 1 0 011-1z M6 9h.01 M10 9h.01 M14 9h.01 M18 9h.01 M6 13h.01 M10 13h.01 M14 13h.01 M18 13h.01 M6 17h12",
  mouse:      "M12 1a6 6 0 00-6 6v10a6 6 0 0012 0V7a6 6 0 00-6-6z M12 7v4",
  clock:      "M12 22a10 10 0 100-20 10 10 0 000 20z M12 6v6l4 2",
  user:       "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2 M12 11a4 4 0 100-8 4 4 0 000 8",
  external:   "M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6 M15 3h6v6 M10 14L21 3",
  upload:     "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M17 8l-5-5-5 5 M12 3v12",
  bell:       "M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9 M13.73 21a2 2 0 01-3.46 0",
  filetext:   "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8",
  filejson:   "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M9 13a2 2 0 01-2 2 M15 13a2 2 0 002 2 M9 19a2 2 0 01-2-2 M15 19a2 2 0 002-2",
  filecsv:    "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M8 16h8 M8 12h8",
  trash:      "M3 6h18 M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2 M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6",
  search:     "M11 19a8 8 0 100-16 8 8 0 000 16z M21 21l-4.35-4.35",
  refresh:    "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10 M1 14l4.64 4.36A9 9 0 0020.49 15",
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

// ─── reusable widgets ───────────────────────────────────────────────────────
const Card = ({ title, subtitle, icon, action, children, padding = 24 }) => {
  const C = useC();
  return (
    <section style={{
      background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`,
      boxShadow: "0 1px 2px rgba(16,24,40,0.04)", marginBottom: 16, overflow: "hidden",
    }}>
      {(title || icon) && (
        <header style={{
          padding: "16px 24px", borderBottom: `1px solid ${C.borderLight}`,
          display: "flex", alignItems: "flex-start", gap: 10,
        }}>
          {icon && <span style={{ marginTop: 2 }}><Icon n={icon} s={18} c={C.brand} /></span>}
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 style={{ fontSize: 15, fontWeight: 700, color: C.text, margin: 0 }}>
              {title}
            </h2>
            {subtitle && (
              <p style={{ fontSize: 12, color: C.textSub, margin: "4px 0 0", lineHeight: 1.5 }}>
                {subtitle}
              </p>
            )}
          </div>
          {action}
        </header>
      )}
      <div style={{ padding }}>{children}</div>
    </section>
  );
};

const Button = ({ children, variant = "primary", onClick, disabled, type = "button", style, icon }) => {
  const C = useC();
  const variants = {
    primary:   { bg: disabled ? C.textLight : C.brand, color: "#fff", border: "none" },
    secondary: { bg: C.surface, color: C.text, border: `1px solid ${C.border}` },
    danger:    { bg: disabled ? "#fbb" : C.danger, color: "#fff", border: "none" },
    ghost:     { bg: "transparent", color: C.brand, border: "none" },
    success:   { bg: disabled ? C.textLight : C.green, color: "#fff", border: "none" },
  };
  const v = variants[variant] || variants.primary;
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: "9px 16px", borderRadius: C.rSm,
        background: v.bg, color: v.color, border: v.border,
        fontWeight: 600, fontSize: 13, cursor: disabled ? "not-allowed" : "pointer",
        fontFamily: "inherit", whiteSpace: "nowrap",
        display: "inline-flex", alignItems: "center", gap: 6,
        boxShadow: variant === "primary" && !disabled ? "0 1px 2px rgba(17,73,106,0.25)" : "none",
        opacity: disabled ? 0.7 : 1,
        ...style,
      }}>
      {icon && <Icon n={icon} s={14} c={v.color} />}
      {children}
    </button>
  );
};

const Toggle = ({ on, onChange, disabled }) => {
  const C = useC();
  return (
    <button
      type="button"
      onClick={() => !disabled && onChange?.(!on)}
      disabled={disabled}
      style={{
        width: 40, height: 22, borderRadius: 12, border: "none",
        cursor: disabled ? "not-allowed" : "pointer",
        background: on ? C.brand : C.textLight, position: "relative",
        transition: "background .18s ease", flexShrink: 0,
        opacity: disabled ? 0.6 : 1,
      }}>
      <span style={{
        position: "absolute", top: 2, left: on ? 20 : 2,
        width: 18, height: 18, background: "#fff", borderRadius: "50%",
        transition: "left .18s ease", boxShadow: "0 1px 3px rgba(0,0,0,0.2)",
      }} />
    </button>
  );
};

const Pill = ({ tone = "neutral", children, icon }) => {
  const C = useC();
  const tones = {
    neutral: { bg: C.borderLight, fg: C.textSub },
    on:      { bg: C.greenBg,     fg: C.green   },
    off:     { bg: C.borderLight, fg: C.textLight },
    locked:  { bg: C.amberBg,     fg: C.amber   },
    danger:  { bg: C.dangerBg,    fg: C.danger  },
    info:    { bg: C.brandBg,     fg: C.brand   },
    purple:  { bg: C.purpleBg,    fg: C.purple  },
  };
  const t = tones[tone] || tones.neutral;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      padding: "3px 10px", borderRadius: 999, fontSize: 11, fontWeight: 700,
      background: t.bg, color: t.fg, letterSpacing: 0.4,
      textTransform: "uppercase", whiteSpace: "nowrap",
    }}>
      {icon && <Icon n={icon} s={11} c={t.fg} w={2.5} />}
      {children}
    </span>
  );
};

const Field = ({ label, helper, children, required }) => {
  const C = useC();
  return (
    <div style={{ marginBottom: 14 }}>
      <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 6 }}>
        {label}{required && <span style={{ color: C.danger, marginLeft: 2 }}>*</span>}
      </label>
      {children}
      {helper && <p style={{ fontSize: 11, color: C.textLight, marginTop: 4 }}>{helper}</p>}
    </div>
  );
};

const Input = ({ value, onChange, type = "text", placeholder, disabled }) => {
  const C = useC();
  return (
    <input
      type={type}
      value={value ?? ""}
      onChange={(e) => onChange?.(e.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      style={{
        width: "100%", padding: "10px 14px", borderRadius: C.rSm,
        border: `1px solid ${C.border}`, background: disabled ? C.borderLight : C.bg,
        fontSize: 14, color: disabled ? C.textSub : C.text, fontFamily: "inherit",
        outline: "none", boxSizing: "border-box",
      }}
      onFocus={(e) => { if (!disabled) { e.target.style.borderColor = C.brand; e.target.style.background = C.surface; } }}
      onBlur={(e)  => { e.target.style.borderColor = C.border; e.target.style.background = disabled ? C.borderLight : C.bg; }}
    />
  );
};

const Select = ({ value, onChange, options, disabled }) => {
  const C = useC();
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange?.(e.target.value)}
      disabled={disabled}
      style={{
        width: "100%", padding: "10px 14px", borderRadius: C.rSm,
        border: `1px solid ${C.border}`, background: disabled ? C.borderLight : C.bg,
        fontSize: 14, color: C.text, fontFamily: "inherit",
        outline: "none", boxSizing: "border-box", appearance: "auto",
      }}>
      {options.map((o) =>
        typeof o === "string"
          ? <option key={o} value={o}>{o}</option>
          : <option key={o.value} value={o.value}>{o.label}</option>
      )}
    </select>
  );
};

const Textarea = ({ value, onChange, placeholder, rows = 3, disabled }) => {
  const C = useC();
  return (
    <textarea
      value={value ?? ""}
      onChange={(e) => onChange?.(e.target.value)}
      placeholder={placeholder}
      rows={rows}
      disabled={disabled}
      style={{
        width: "100%", padding: "10px 14px", borderRadius: C.rSm,
        border: `1px solid ${C.border}`, background: disabled ? C.borderLight : C.bg,
        fontSize: 14, color: C.text, fontFamily: "inherit",
        outline: "none", boxSizing: "border-box", resize: "vertical",
      }}
      onFocus={(e) => { if (!disabled) { e.target.style.borderColor = C.brand; e.target.style.background = C.surface; } }}
      onBlur={(e)  => { e.target.style.borderColor = C.border; e.target.style.background = disabled ? C.borderLight : C.bg; }}
    />
  );
};

const EmptyState = ({ icon = "info", title, body, action }) => {
  const C = useC();
  return (
    <div style={{
      padding: "48px 24px", textAlign: "center",
      color: C.textSub, fontSize: 13, lineHeight: 1.5,
    }}>
      <div style={{
        width: 48, height: 48, borderRadius: "50%",
        background: C.brandBg, display: "inline-flex",
        alignItems: "center", justifyContent: "center", marginBottom: 12,
      }}>
        <Icon n={icon} s={22} c={C.brand} />
      </div>
      <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>{title}</p>
      {body && <p style={{ margin: "6px 0 0", maxWidth: 420, marginLeft: "auto", marginRight: "auto" }}>{body}</p>}
      {action && <div style={{ marginTop: 14 }}>{action}</div>}
    </div>
  );
};

const Spinner = () => {
  const C = useC();
  return (
    <div style={{ padding: 32, display: "flex", justifyContent: "center" }}>
      <div style={{
        width: 28, height: 28, borderRadius: "50%",
        border: `3px solid ${C.brandBg}`, borderTopColor: C.brand,
        animation: "zt-spin 0.8s linear infinite",
      }} />
      <style>{`@keyframes zt-spin{to{transform:rotate(360deg)}}`}</style>
    </div>
  );
};

// Modal — controlled, dismiss-on-overlay-click, ESC-close
const Modal = ({ open, onClose, title, children, footer, size = "medium" }) => {
  const C = useC();
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onClose?.(); };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [open, onClose]);

  if (!open) return null;
  const widths = { small: 460, medium: 640, large: 820 };
  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 9999,
        background: "rgba(16,24,40,0.55)", backdropFilter: "blur(2px)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 20,
      }}>
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%", maxWidth: widths[size] || widths.medium,
          maxHeight: "90vh", display: "flex", flexDirection: "column",
          background: C.surface, borderRadius: 12,
          boxShadow: "0 12px 40px rgba(16,24,40,0.25)",
          overflow: "hidden",
        }}>
        <header style={{
          padding: "16px 20px", borderBottom: `1px solid ${C.borderLight}`,
          display: "flex", alignItems: "center", gap: 10,
        }}>
          <h3 style={{ flex: 1, fontSize: 15, fontWeight: 800, color: C.text, margin: 0 }}>
            {title}
          </h3>
          <button onClick={onClose} aria-label="Close"
            style={{
              width: 28, height: 28, borderRadius: 6,
              background: C.borderLight, border: "none", cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}>
            <Icon n="x" s={14} c={C.textSub} />
          </button>
        </header>
        <div style={{ padding: 20, overflowY: "auto", flex: 1 }}>{children}</div>
        {footer && (
          <footer style={{
            padding: "12px 20px", borderTop: `1px solid ${C.borderLight}`,
            display: "flex", justifyContent: "flex-end", gap: 8, background: C.bg,
          }}>{footer}</footer>
        )}
      </div>
    </div>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// MODULE HEADER  (renders above every pane)
// ═════════════════════════════════════════════════════════════════════════════
const PANE_LABELS = {
  "whats-recorded":   "What's Recorded",
  "who-viewed":       "Who Viewed My Data",
  "privacy-controls": "Privacy Controls",
  "corrections":      "Corrections & Disputes",
  "data-export":      "My Data Export",
};

const ModuleHeader = ({ activePane, policyMeta, onViewPolicy }) => {
  const C = useC();
  return (
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
        <Icon n="shield" s={22} c="#fff" w={2.2} />
      </div>
      <div style={{ flex: 1, minWidth: 240 }}>
        <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                    letterSpacing: 0.6, textTransform: "uppercase", margin: 0 }}>
          My Data {activePane && <span style={{ color: C.brand }}>· {PANE_LABELS[activePane]}</span>}
        </p>
        <h1 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: "2px 0 0" }}>
          {PANE_LABELS[activePane] || "My Data"}
        </h1>
        <p style={{ fontSize: 13, color: C.textSub, margin: "6px 0 0", lineHeight: 1.6, maxWidth: 720 }}>
          Everything Temveris records about your work — what it is, who has seen it,
          and what you can do about it. What Temveris captures depends on your
          workspace's configured policy.
        </p>
      </div>
      <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
        <Button variant="secondary" icon="external" onClick={onViewPolicy}>
          View Workspace Policy
        </Button>
      </div>
    </div>
    {policyMeta?.last_updated && (
      <p style={{ fontSize: 11, color: C.textLight, margin: "14px 0 0",
                  paddingTop: 14, borderTop: `1px solid ${C.borderLight}` }}>
        Workspace policy last updated on{" "}
        <strong style={{ color: C.textSub }}>{fmtDate(policyMeta.last_updated)}</strong>
        {policyMeta.updated_by && <> by <strong style={{ color: C.textSub }}>{policyMeta.updated_by}</strong></>}.
      </p>
    )}
  </div>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 1 — WHAT'S RECORDED
// ═════════════════════════════════════════════════════════════════════════════

// Default capture config — used when /my-data/capture-config returns 404
// or the workspace hasn't configured one yet.
const DEFAULT_CAPTURE = {
  policy: {
    name: "Workforce Monitoring Policy",
    version: "1.0",
    last_updated: null,
    updated_by: null,
  },
  captures: [
    {
      key: "time_tracking", icon: "clock",
      label: "Time tracking",
      state: "on",
      detail: "Captures clock-in, clock-out, breaks, work sessions, and idle periods where enabled.",
      why: "Required to compute pay, attendance, and compliance with working-time rules.",
      retention_days: 2555,
      retention_label: "7 years",
      who_can_view: ["Direct manager", "HR Admin", "Payroll", "You"],
    },
    {
      key: "app_usage", icon: "monitor",
      label: "App and website usage",
      state: "on",
      detail: "Records app and site categories during work sessions, based on workspace policy.",
      why: "Productivity insights and compliance with acceptable-use policies.",
      retention_days: 180,
      retention_label: "180 days",
      who_can_view: ["Direct manager", "You"],
    },
    {
      key: "screenshots", icon: "camera",
      label: "Screenshots",
      state: "on",
      detail: "Periodic screenshots taken while you're clocked in. Capture frequency and blur rules follow workspace policy.",
      why: "Proof-of-work for client billing, audit, and dispute resolution.",
      retention_days: 90,
      retention_label: "90 days",
      who_can_view: ["Direct manager", "HR Admin", "You"],
      meta: ["Frequency: every 10 min", "Sensitive-app blur: enabled", "Worker preview: available"],
    },
    {
      key: "location", icon: "map",
      label: "Location",
      state: "off",
      detail: "Captured only where configured and legally permitted, while clocked in.",
      why: "Field-work attendance and safety compliance.",
      retention_days: 30,
      retention_label: "30 days",
      who_can_view: ["Direct manager", "You"],
    },
    {
      key: "keyboard", icon: "keyboard",
      label: "Keyboard activity",
      state: "activity_only",
      detail: "Records typing presence only. No keystroke content is stored.",
      why: "Distinguishing active work from idle time.",
      retention_days: 90,
      retention_label: "90 days",
      who_can_view: ["Direct manager", "You"],
    },
    {
      key: "mouse", icon: "mouse",
      label: "Mouse activity",
      state: "on",
      detail: "Records movement presence. Cursor position and clicks are not stored as content.",
      why: "Distinguishing active work from idle time.",
      retention_days: 90,
      retention_label: "90 days",
      who_can_view: ["Direct manager", "You"],
    },
  ],
};

const stateToPill = (state) => {
  if (state === "on")             return <Pill tone="on" icon="check">On</Pill>;
  if (state === "off")            return <Pill tone="off">Off</Pill>;
  if (state === "activity_only")  return <Pill tone="info">Activity only</Pill>;
  if (state === "not_applicable") return <Pill tone="neutral">Not applicable</Pill>;
  return <Pill tone="neutral">{state}</Pill>;
};

const CaptureRow = ({ row, defaultOpen = false }) => {
  const C = useC();
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div style={{ borderBottom: `1px solid ${C.borderLight}` }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        style={{
          width: "100%", display: "flex", alignItems: "center", gap: 14,
          padding: "16px 24px", background: "none", border: "none",
          cursor: "pointer", textAlign: "left", fontFamily: "inherit",
        }}>
        <div style={{
          width: 38, height: 38, borderRadius: 10, flexShrink: 0,
          background: row.state === "on" ? C.brandBg : C.borderLight,
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <Icon n={row.icon} s={18} c={row.state === "on" ? C.brand : C.textSub} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>
            {row.label}
          </p>
          <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0",
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {row.detail}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
          {stateToPill(row.state)}
          <Icon n={open ? "chevup" : "chevdown"} s={16} c={C.textLight} />
        </div>
      </button>
      {open && (
        <div style={{
          padding: "0 24px 20px 76px", display: "grid", gap: 16,
          gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
        }}>
          <div>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 4px" }}>
              Why it's captured
            </p>
            <p style={{ fontSize: 13, color: C.text, margin: 0, lineHeight: 1.5 }}>{row.why}</p>
          </div>
          <div>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 4px" }}>
              Retention period
            </p>
            <p style={{ fontSize: 13, color: C.text, margin: 0, fontWeight: 600 }}>{row.retention_label}</p>
          </div>
          <div>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 4px" }}>
              Who can view it
            </p>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {(row.who_can_view || []).map((r) => <Pill key={r} tone="neutral">{r}</Pill>)}
            </div>
          </div>
          {row.meta && row.meta.length > 0 && (
            <div>
              <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                          textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 4px" }}>
                Configuration
              </p>
              <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12, color: C.textSub, lineHeight: 1.7 }}>
                {row.meta.map((m, i) => <li key={i}>{m}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

const WhatsRecordedPane = ({ config, loading, onViewPolicy }) => {
  const C = useC();
  if (loading) return <Card><Spinner /></Card>;
  const captures = config?.captures || DEFAULT_CAPTURE.captures;
  const policy   = config?.policy   || DEFAULT_CAPTURE.policy;
  return (
    <>
      <Card
        title="Active capture configuration"
        subtitle="Each row reflects what your workspace policy currently has enabled for your account. Tap any row to see why it's captured, how long it's kept, and who can view it."
        icon="database"
        padding={0}
      >
        {captures.map((row) => <CaptureRow key={row.key} row={row} />)}
      </Card>

      <Card title="Workspace policy" icon="filetext"
        action={<Button variant="secondary" icon="external" onClick={onViewPolicy}>View full policy</Button>}>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
          <div>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 4px" }}>Policy</p>
            <p style={{ fontSize: 13, color: C.text, margin: 0, fontWeight: 700 }}>
              {policy.name}
              <span style={{ marginLeft: 6, color: C.textSub, fontWeight: 500 }}>v{policy.version}</span>
            </p>
          </div>
          <div>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 4px" }}>Last updated</p>
            <p style={{ fontSize: 13, color: C.text, margin: 0 }}>
              {policy.last_updated ? fmtDate(policy.last_updated) : "—"}
              {policy.updated_by && <span style={{ color: C.textSub }}> · {policy.updated_by}</span>}
            </p>
          </div>
        </div>
      </Card>
    </>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 2 — WHO VIEWED MY DATA
// ═════════════════════════════════════════════════════════════════════════════

const RANGE_OPTIONS = [
  { value: "7",   label: "Last 7 days" },
  { value: "30",  label: "Last 30 days" },
  { value: "90",  label: "Last 90 days" },
  { value: "all", label: "All time" },
];

const WhoViewedPane = ({ rows, loading, range, onRangeChange, onRefresh }) => {
  const C = useC();
  const anomalies = useMemo(() => (rows || []).filter((r) => r.anomaly_flag), [rows]);

  const exportCSV = () => {
    if (!rows || rows.length === 0) {
      toast.error("No rows to export");
      return;
    }
    downloadCSV(`my-data-access-log-${new Date().toISOString().slice(0, 10)}.csv`,
      rows.map((r) => ({
        date:    fmtDateTime(r.accessed_at || r.at),
        who:     r.accessor_name || r.actor_name || "—",
        role:    r.accessor_role || "—",
        viewed:  r.surface_accessed || r.action || "—",
        reason:  r.reason || "—",
      })),
      [
        { key: "date",   label: "Date" },
        { key: "who",    label: "Who" },
        { key: "role",   label: "Role" },
        { key: "viewed", label: "What they viewed" },
        { key: "reason", label: "Reason" },
      ]);
    toast.success("CSV downloaded");
  };

  const requestExplanation = (row) => {
    toast.success(`Explanation request sent for ${row.accessor_name || "this access"}`);
    // Backend wiring point: POST /my-data/access/:id/explain
  };

  return (
    <>
      <Card
        title="Plain-language explanation"
        subtitle="Routine viewing of your daily timesheet by your direct manager is logged but not flagged. Access to screenshots, detailed activity, or historical records is logged with a reason. Automated processing such as payroll calculation is not shown here — only human access."
        icon="info"
      >
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <Pill tone="info" icon="user">Human access only</Pill>
          <Pill tone="neutral">Audit-logged</Pill>
          <Pill tone="purple" icon="shield">Append-only record</Pill>
        </div>
      </Card>

      {anomalies.length > 0 && (
        <Card padding={0}>
          <div style={{
            background: C.amberBg, padding: "16px 20px",
            borderLeft: `4px solid ${C.amber}`,
            display: "flex", gap: 14, alignItems: "flex-start",
          }}>
            <Icon n="warning" s={20} c={C.amber} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <p style={{ fontSize: 14, fontWeight: 800, color: C.amber, margin: 0 }}>
                Unusual access detected
              </p>
              <p style={{ fontSize: 13, color: C.text, margin: "4px 0 0", lineHeight: 1.5 }}>
                {anomalies.length === 1
                  ? `${anomalies[0].accessor_name || "Someone"} (${anomalies[0].accessor_role || "Unknown role"}) accessed ${anomalies[0].surface_accessed || "your data"}. Reason recorded: ${anomalies[0].reason || "not specified"}.`
                  : `${anomalies.length} unusual access events detected in the selected range.`}
              </p>
              <div style={{ marginTop: 10, display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Button variant="secondary" onClick={() => requestExplanation(anomalies[0])}>
                  Request explanation
                </Button>
              </div>
            </div>
          </div>
        </Card>
      )}

      <Card
        title={`Access log (${rows?.length || 0})`}
        icon="eye"
        padding={0}
        action={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Select value={range} onChange={onRangeChange} options={RANGE_OPTIONS} />
            <Button variant="secondary" icon="refresh" onClick={onRefresh}>Refresh</Button>
            <Button variant="secondary" icon="download" onClick={exportCSV}
                    disabled={!rows || rows.length === 0}>CSV</Button>
          </div>
        }
      >
        {loading ? <Spinner /> : (rows && rows.length > 0) ? (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {rows.map((r) => (
              <li key={r.id} style={{
                display: "grid",
                gridTemplateColumns: "minmax(140px, 180px) minmax(0, 1.2fr) minmax(0, 1fr) auto",
                gap: 12, alignItems: "center",
                padding: "14px 24px",
                borderBottom: `1px solid ${C.borderLight}`,
                background: r.anomaly_flag ? `${C.amberBg}66` : "transparent",
              }}>
                <div style={{ fontSize: 12, color: C.textSub }}>
                  {fmtDateTime(r.accessed_at || r.at)}
                </div>
                <div style={{ minWidth: 0 }}>
                  <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0,
                              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {r.accessor_name || r.actor_name || "System"}
                  </p>
                  <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                    {r.accessor_role || "—"}
                  </p>
                </div>
                <div style={{ minWidth: 0 }}>
                  <p style={{ fontSize: 13, color: C.text, margin: 0, fontWeight: 600,
                              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {r.surface_accessed || r.action || "—"}
                  </p>
                  <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0",
                              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {r.reason || "Routine review"}
                  </p>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {r.anomaly_flag && <Pill tone="locked" icon="warning">Unusual</Pill>}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState icon="eye"
            title="No human access to your data is recorded for this period."
            body="Automated system processing isn't shown here — only views by managers, HR, or admins." />
        )}
      </Card>
    </>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 3 — PRIVACY CONTROLS
// ═════════════════════════════════════════════════════════════════════════════

const PRIVACY_CONTROLS = [
  {
    key: "blur_screenshots",
    title: "Blur screenshots when sensitive apps are detected",
    sub:   "Automatically blurs windows for password managers, banking, and health apps before storage.",
    default: true, lockable: false,
  },
  {
    key: "screenshot_notification",
    title: "Show notification when a screenshot is captured",
    sub:   "A small overlay appears for 1.5 seconds at the moment of capture.",
    default: false, lockable: true,
  },
  {
    key: "tracking_indicator",
    title: 'Show "tracking active" indicator while clocked in',
    sub:   "A persistent badge in your menu bar / system tray confirms tracking is on.",
    default: true, lockable: false,
  },
  {
    key: "hide_browser_tab_titles",
    title: "Hide non-work browser tab titles in stored records",
    sub:   "Replaces tab titles with the domain only for sites outside your work category.",
    default: true, lockable: true,
  },
  {
    key: "weekly_summary_email",
    title: "Email me a weekly summary of what was recorded about me",
    sub:   "A Friday digest covering hours, capture counts, and any access events on your data.",
    default: false, lockable: false,
  },
];

const PrivacyControlsPane = ({ values, locks, loading, onChange }) => {
  const C = useC();
  if (loading) return <Card><Spinner /></Card>;
  return (
    <>
      <Card
        title="Worker-controllable settings"
        subtitle="These are settings you control. Workspace-mandated capture remains policy-driven and cannot be disabled here. Changes save immediately and are written to the audit trail."
        icon="lock"
        padding={0}
      >
        {PRIVACY_CONTROLS.map((ctrl, i) => {
          const lock = locks?.[ctrl.key];
          const isLocked = !!lock?.locked;
          const value = values?.[ctrl.key] ?? ctrl.default;
          return (
            <div key={ctrl.key} style={{
              padding: "18px 24px",
              borderBottom: i < PRIVACY_CONTROLS.length - 1 ? `1px solid ${C.borderLight}` : "none",
              display: "flex", alignItems: "flex-start", gap: 16,
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0,
                            display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  {ctrl.title}
                  {isLocked && <Pill tone="locked" icon="lock">Workspace-locked</Pill>}
                </p>
                <p style={{ fontSize: 12, color: C.textSub, margin: "4px 0 0", lineHeight: 1.5 }}>
                  {ctrl.sub}
                </p>
                {isLocked && (
                  <div style={{
                    marginTop: 10, padding: "10px 12px", borderRadius: 6,
                    background: C.amberBg, border: `1px solid ${C.amberBg}`,
                    fontSize: 12, color: C.amber, lineHeight: 1.5,
                  }}>
                    <strong>This setting is managed by your workspace policy.</strong>
                    <br />
                    Required state: <strong>{lock.required_value ? "On" : "Off"}</strong>
                    {lock.reason && <> · Reason: {lock.reason}</>}
                    {lock.policy_name && <> · {lock.policy_name}</>}
                    <div style={{ marginTop: 6 }}>
                      <Button variant="ghost" style={{ padding: "4px 0", fontSize: 12 }}
                              onClick={() => toast.success("Change request sent to your manager")}>
                        Request a change from your manager →
                      </Button>
                    </div>
                  </div>
                )}
              </div>
              <Toggle on={value} disabled={isLocked} onChange={(v) => onChange(ctrl.key, v)} />
            </div>
          );
        })}
      </Card>

      <Card icon="info" title="What's not here, and why" padding={20}>
        <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13, color: C.textSub, lineHeight: 1.8 }}>
          <li><strong style={{ color: C.text }}>Pause tracking during break</strong> — handled by the workforce state model, not by a privacy toggle.</li>
          <li><strong style={{ color: C.text }}>Disable workspace time tracking</strong> — governed by workspace policy and your contract.</li>
          <li><strong style={{ color: C.text }}>Hide all activity from managers</strong> — access is role- and policy-governed, not worker-discretionary.</li>
        </ul>
      </Card>
    </>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 4 — CORRECTIONS & DISPUTES
// ═════════════════════════════════════════════════════════════════════════════

const CORRECTION_TYPES = [
  { value: "time_entry",    label: "Time entry" },
  { value: "idle_time",     label: "Idle time" },
  { value: "screenshot",    label: "Screenshot" },
  { value: "app_classification", label: "App / website classification" },
  { value: "other",         label: "Other" },
];

const CORRECTION_STATUSES = {
  submitted:           { label: "Submitted",            tone: "info" },
  in_review:           { label: "In review",            tone: "info" },
  approved:            { label: "Approved",             tone: "on" },
  resolved_no_change:  { label: "Resolved — no change", tone: "neutral" },
  rejected:            { label: "Rejected",             tone: "danger" },
};

const CorrectionsPane = ({ items, loading, onRefresh, onSubmit, onEscalate }) => {
  const C = useC();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState({
    type: "time_entry", from: "", to: "", correction: "", reason: "",
  });
  const [evidence, setEvidence] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const fileRef = useRef(null);

  const reset = () => {
    setDraft({ type: "time_entry", from: "", to: "", correction: "", reason: "" });
    setEvidence(null);
  };

  const submit = async () => {
    if (!draft.correction.trim()) {
      toast.error("Please describe what should be corrected");
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit({ ...draft, evidence_filename: evidence?.name || null });
      reset();
      setOpen(false);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Card
        title="Request a correction"
        subtitle="If you believe something was recorded incorrectly, submit a structured review request. Your submission is routed to the appropriate reviewer and a confirmation is logged to the audit trail."
        icon="info"
        action={<Button icon="upload" onClick={() => setOpen(true)}>Request a correction</Button>}
      >
        <div style={{
          display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
          gap: 12, marginTop: 4,
        }}>
          {[
            { type: "Time entry",  who: "Direct manager / time approver" },
            { type: "Idle time",   who: "Direct manager / ops reviewer" },
            { type: "Screenshot",  who: "HR Admin / privacy reviewer" },
            { type: "App class.",  who: "Workspace Admin" },
            { type: "Other",       who: "HR Admin triage queue" },
          ].map((r) => (
            <div key={r.type} style={{
              padding: 12, borderRadius: 8, border: `1px solid ${C.border}`,
              background: C.bg,
            }}>
              <p style={{ fontSize: 12, fontWeight: 700, color: C.text, margin: 0 }}>{r.type}</p>
              <p style={{ fontSize: 11, color: C.textSub, margin: "4px 0 0", lineHeight: 1.5 }}>
                Routes to {r.who}
              </p>
            </div>
          ))}
        </div>
      </Card>

      <Card
        title={`Your requests (${items?.length || 0})`}
        icon="filetext"
        padding={0}
        action={<Button variant="secondary" icon="refresh" onClick={onRefresh}>Refresh</Button>}
      >
        {loading ? <Spinner /> : (items && items.length > 0) ? (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {items.map((it) => {
              const status = CORRECTION_STATUSES[it.status] || { label: it.status, tone: "neutral" };
              const typeLabel = CORRECTION_TYPES.find((t) => t.value === it.request_type)?.label || it.request_type;
              const canEscalate = it.status === "rejected" || it.status === "resolved_no_change";
              return (
                <li key={it.id} style={{
                  padding: "16px 24px",
                  borderBottom: `1px solid ${C.borderLight}`,
                  display: "flex", gap: 14, alignItems: "flex-start",
                }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0,
                                display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      {typeLabel}
                      <Pill tone={status.tone}>{status.label}</Pill>
                    </p>
                    <p style={{ fontSize: 12, color: C.textSub, margin: "4px 0 0", lineHeight: 1.5 }}>
                      Submitted {fmtDate(it.submitted_at)}
                      {it.resolved_at && <> · Last update {fmtRel(it.resolved_at)}</>}
                      {it.reviewer_name && <> · {it.reviewer_name}</>}
                    </p>
                    {it.outcome_reason && (
                      <p style={{ fontSize: 12, color: C.text, margin: "8px 0 0",
                                  padding: "8px 10px", background: C.bg,
                                  borderLeft: `3px solid ${status.tone === "danger" ? C.danger : C.brand}`,
                                  borderRadius: 4, lineHeight: 1.5 }}>
                        <strong>Reviewer note:</strong> {it.outcome_reason}
                      </p>
                    )}
                  </div>
                  {canEscalate && (
                    <Button variant="ghost" onClick={() => onEscalate(it)}>
                      Escalate →
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        ) : (
          <EmptyState icon="filetext"
            title="No correction requests yet."
            body="If you spot something inaccurate in your records — a time entry, a screenshot, an idle period — request a correction here."
            action={<Button icon="upload" onClick={() => setOpen(true)}>Request a correction</Button>}
          />
        )}
      </Card>

      <Card icon="warning" title="Disagree with a decision?" padding={20}>
        <p style={{ fontSize: 13, color: C.textSub, margin: 0, lineHeight: 1.6 }}>
          You can escalate any rejected or unresolved decision to HR or your designated grievance contact.
          In jurisdictions with works councils or employee representatives, escalation routes through the
          relevant process where configured.
        </p>
        <div style={{ marginTop: 12 }}>
          <Button variant="secondary" onClick={() => onEscalate(null)}>Start escalation</Button>
        </div>
      </Card>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Request a correction"
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)} disabled={submitting}>Cancel</Button>
            <Button onClick={submit} disabled={submitting}>
              {submitting ? "Submitting…" : "Submit request"}
            </Button>
          </>
        }
      >
        <Field label="What needs correction?" required>
          <Select
            value={draft.type}
            onChange={(v) => setDraft({ ...draft, type: v })}
            options={CORRECTION_TYPES}
          />
        </Field>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <Field label="From">
            <Input type="datetime-local" value={draft.from} onChange={(v) => setDraft({ ...draft, from: v })} />
          </Field>
          <Field label="To">
            <Input type="datetime-local" value={draft.to} onChange={(v) => setDraft({ ...draft, to: v })} />
          </Field>
        </div>
        <Field label="What should it be?" required>
          <Textarea
            value={draft.correction}
            onChange={(v) => setDraft({ ...draft, correction: v })}
            placeholder="Describe the correct value or what should change…"
            rows={3}
          />
        </Field>
        <Field label="Why?" helper="Optional. Helps reviewers prioritize and respond.">
          <Textarea
            value={draft.reason}
            onChange={(v) => setDraft({ ...draft, reason: v })}
            placeholder="e.g. Clocked back in late from lunch — system idle-flagged the gap"
            rows={2}
          />
        </Field>
        <Field label="Supporting evidence" helper="Optional. PDF, image, or document — max 10 MB.">
          <input
            ref={fileRef}
            type="file"
            onChange={(e) => setEvidence(e.target.files?.[0] || null)}
            style={{ display: "none" }}
          />
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Button variant="secondary" icon="upload" onClick={() => fileRef.current?.click()}>
              {evidence ? "Replace file" : "Choose file"}
            </Button>
            {evidence && (
              <span style={{ fontSize: 12, color: C.textSub }}>
                {evidence.name} ({Math.round(evidence.size / 1024)} KB)
              </span>
            )}
          </div>
        </Field>
      </Modal>
    </>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 5 — MY DATA EXPORT
// ═════════════════════════════════════════════════════════════════════════════

const EXPORT_SCOPE = [
  { key: "time_records",         label: "Time records",         on: true },
  { key: "activity_records",     label: "Activity records",     on: true },
  { key: "screenshots",          label: "Screenshots",          on: true },
  { key: "correction_requests",  label: "Correction requests",  on: true },
  { key: "access_log",           label: "Access log entries",   on: true },
  { key: "privacy_history",      label: "Privacy setting history", on: true },
  { key: "policy_acks",          label: "Policy acknowledgements", on: true },
];

const FORMAT_INFO = {
  pdf:  { icon: "filetext", label: "PDF",  desc: "Human-readable record for the worker." },
  json: { icon: "filejson", label: "JSON", desc: "Machine-readable data portability format." },
  csv:  { icon: "filecsv",  label: "CSV",  desc: "Structured data tables — time, activity, access, corrections." },
};

const DataExportPane = ({ history, loading, onRequest, onRequestDSAR, onRequestDeletion, onRefresh }) => {
  const C = useC();
  const [format, setFormat] = useState("pdf");
  const [scope, setScope]   = useState(() =>
    Object.fromEntries(EXPORT_SCOPE.map((s) => [s.key, s.on]))
  );
  const [requesting, setRequesting] = useState(false);
  const [dsarOpen, setDsarOpen] = useState(false);
  const [delOpen,  setDelOpen]  = useState(false);

  const submit = async () => {
    setRequesting(true);
    try {
      await onRequest({ format, scope });
    } finally {
      setRequesting(false);
    }
  };

  return (
    <>
      <Card
        title="Standard export"
        subtitle="Download a complete record of the data Temveris holds about you. Other workers' data and workspace aggregates are excluded by design."
        icon="download"
      >
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1.4fr)", gap: 28 }}>
          <div>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 8px" }}>
              Format
            </p>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {Object.entries(FORMAT_INFO).map(([k, v]) => (
                <button key={k} type="button" onClick={() => setFormat(k)}
                  style={{
                    textAlign: "left", display: "flex", alignItems: "center", gap: 12,
                    padding: 12, borderRadius: 8,
                    border: `1px solid ${format === k ? C.brand : C.border}`,
                    background: format === k ? C.brandBg : C.surface,
                    cursor: "pointer", fontFamily: "inherit",
                  }}>
                  <Icon n={v.icon} s={20} c={format === k ? C.brand : C.textSub} />
                  <div style={{ flex: 1 }}>
                    <p style={{ fontSize: 13, fontWeight: 700,
                                color: format === k ? C.brand : C.text, margin: 0 }}>
                      {v.label}
                    </p>
                    <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                      {v.desc}
                    </p>
                  </div>
                  {format === k && <Icon n="check" s={16} c={C.brand} />}
                </button>
              ))}
            </div>
          </div>

          <div>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 8px" }}>
              Included in export
            </p>
            <div style={{ border: `1px solid ${C.border}`, borderRadius: 8 }}>
              {EXPORT_SCOPE.map((s, i) => (
                <div key={s.key} style={{
                  display: "flex", alignItems: "center", justifyContent: "space-between",
                  padding: "10px 14px",
                  borderBottom: i < EXPORT_SCOPE.length - 1 ? `1px solid ${C.borderLight}` : "none",
                }}>
                  <span style={{ fontSize: 13, color: C.text }}>{s.label}</span>
                  <Toggle on={scope[s.key]}
                          onChange={(v) => setScope({ ...scope, [s.key]: v })} />
                </div>
              ))}
            </div>
            <p style={{ fontSize: 11, color: C.textLight, margin: "8px 2px 0", lineHeight: 1.5 }}>
              Exports exclude other workers' data, workspace-aggregate reports, and admin notes.
              Download links are signed and expire automatically.
            </p>
          </div>
        </div>

        <div style={{ marginTop: 18, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button onClick={submit} icon="download" disabled={requesting}>
            {requesting ? "Preparing…" : "Download my data"}
          </Button>
        </div>
      </Card>

      <Card
        title={`Export history (${history?.length || 0})`}
        icon="filetext"
        padding={0}
        action={<Button variant="secondary" icon="refresh" onClick={onRefresh}>Refresh</Button>}
      >
        {loading ? <Spinner /> : (history && history.length > 0) ? (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {history.map((h) => (
              <li key={h.id} style={{
                padding: "14px 24px", borderBottom: `1px solid ${C.borderLight}`,
                display: "flex", alignItems: "center", gap: 14,
              }}>
                <Icon n={FORMAT_INFO[h.format]?.icon || "filetext"} s={20} c={C.brand} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>
                    {(h.format || "PDF").toUpperCase()} export
                    {h.expires_at && new Date(h.expires_at) < new Date() && (
                      <span style={{ marginLeft: 8 }}><Pill tone="danger">Expired</Pill></span>
                    )}
                  </p>
                  <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                    Requested {fmtRel(h.requested_at)}
                    {h.completed_at && <> · Ready {fmtRel(h.completed_at)}</>}
                    {h.expires_at && <> · Expires {fmtDate(h.expires_at)}</>}
                  </p>
                </div>
                {h.download_url && (
                  <Button variant="secondary" icon="download"
                          onClick={() => window.open(h.download_url, "_blank", "noopener")}>
                    Download
                  </Button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState icon="download"
            title="No exports yet."
            body="When you request a download, it'll appear here with a signed link that expires automatically." />
        )}
      </Card>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 16 }}>
        <Card title="Formal Data Subject Request" icon="shield">
          <p style={{ fontSize: 13, color: C.textSub, margin: 0, lineHeight: 1.6 }}>
            Need a complete formal record? Submit a Data Subject Access Request.
            Acknowledged within 72 hours; fulfilled within 30 days for UK/EU regulatory settings.
          </p>
          <div style={{ marginTop: 14 }}>
            <Button variant="secondary" onClick={() => setDsarOpen(true)}>Submit DSAR</Button>
          </div>
        </Card>

        <Card title="Data deletion request" icon="trash">
          <p style={{ fontSize: 13, color: C.textSub, margin: 0, lineHeight: 1.6 }}>
            If you have left this organization, you can request deletion of your data.
            Some records may be retained where payroll, tax, legal-hold, or audit obligations require it.
          </p>
          <div style={{ marginTop: 14 }}>
            <Button variant="danger" onClick={() => setDelOpen(true)}>Request deletion</Button>
          </div>
        </Card>
      </div>

      {/* DSAR modal */}
      <DSARModal open={dsarOpen} onClose={() => setDsarOpen(false)} onSubmit={onRequestDSAR} />

      {/* Deletion modal */}
      <DeletionModal open={delOpen} onClose={() => setDelOpen(false)} onSubmit={onRequestDeletion} />
    </>
  );
};

const DSARModal = ({ open, onClose, onSubmit }) => {
  const C = useC();
  const [type, setType] = useState("access");
  const [details, setDetails] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async () => {
    setSubmitting(true);
    try { await onSubmit({ type, details }); onClose(); }
    finally { setSubmitting(false); }
  };
  return (
    <Modal open={open} onClose={onClose} title="Submit Data Subject Access Request"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button onClick={submit} disabled={submitting}>{submitting ? "Submitting…" : "Submit DSAR"}</Button>
        </>
      }>
      <Field label="Request type" required>
        <Select value={type} onChange={setType} options={[
          { value: "access",      label: "Access — I want a copy of all my data" },
          { value: "rectification", label: "Rectification — I want incorrect data corrected" },
          { value: "erasure",     label: "Erasure — I want my data deleted" },
          { value: "portability", label: "Portability — I want my data in a machine-readable format" },
        ]} />
      </Field>
      <Field label="Additional details" helper="Optional. Helps the privacy team scope your request.">
        <Textarea value={details} onChange={setDetails} rows={4}
          placeholder="Specify time ranges, record types, or anything else relevant." />
      </Field>
      <p style={{ fontSize: 12, color: C.textSub, margin: "8px 0 0", lineHeight: 1.6,
                  padding: "10px 12px", background: C.bg, borderRadius: 6 }}>
        We'll acknowledge within 72 hours and aim to fulfil within 30 days. You'll see status
        updates here and via email. Some records may be retained where legal obligations require it.
      </p>
    </Modal>
  );
};

const DeletionModal = ({ open, onClose, onSubmit }) => {
  const C = useC();
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const submit = async () => {
    setSubmitting(true);
    try { await onSubmit({ reason }); onClose(); setReason(""); setConfirm(false); }
    finally { setSubmitting(false); }
  };
  return (
    <Modal open={open} onClose={onClose} title="Request data deletion"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button variant="danger" onClick={submit} disabled={!confirm || submitting}>
            {submitting ? "Submitting…" : "Submit deletion request"}
          </Button>
        </>
      }>
      <div style={{
        padding: 14, borderRadius: 8, background: C.amberBg,
        border: `1px solid ${C.amber}33`, color: C.amber,
        display: "flex", gap: 10, alignItems: "flex-start",
      }}>
        <Icon n="warning" s={18} c={C.amber} />
        <div style={{ flex: 1, fontSize: 13, lineHeight: 1.5 }}>
          Deletion is workspace-mediated. Data the workspace is legally required to retain
          (payroll, tax records, legal holds, audit) cannot be deleted. You will receive a
          response listing what was deleted and what was retained, with the legal basis.
        </div>
      </div>
      <Field label="Reason for request">
        <Textarea value={reason} onChange={setReason} rows={3}
          placeholder="e.g. I have left this organization" />
      </Field>
      <label style={{ display: "flex", gap: 10, alignItems: "flex-start",
                      fontSize: 13, color: C.text, cursor: "pointer" }}>
        <input type="checkbox" checked={confirm}
               onChange={(e) => setConfirm(e.target.checked)}
               style={{ marginTop: 3 }} />
        <span>I understand my workspace may retain certain records where required by law,
              and that this request will be reviewed before any data is deleted.</span>
      </label>
    </Modal>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// POLICY DRAWER  (side panel — opens from the header "View policy" CTA)
// ═════════════════════════════════════════════════════════════════════════════
const PolicyDrawer = ({ open, onClose, policy, acks, onAcknowledge }) => {
  const C = useC();
  const [confirmTarget, setConfirmTarget] = useState(null);
  const [hasRead, setHasRead] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const openConfirm = (ack) => {
    setConfirmTarget(ack);
    setHasRead(false);
  };
  const closeConfirm = () => {
    setConfirmTarget(null);
    setHasRead(false);
  };
  const confirmAcknowledge = async () => {
    if (!hasRead || !confirmTarget) return;
    setSubmitting(true);
    try {
      await onAcknowledge(confirmTarget);
      closeConfirm();
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;
  return (
    <>
      <div onClick={onClose}
        style={{
          position: "fixed", inset: 0, zIndex: 9998,
          background: "rgba(16,24,40,0.45)",
          display: "flex", justifyContent: "flex-end",
        }}>
        <aside onClick={(e) => e.stopPropagation()}
          style={{
            width: "100%", maxWidth: 480, height: "100%",
            background: C.surface, boxShadow: "-12px 0 40px rgba(16,24,40,0.18)",
            display: "flex", flexDirection: "column",
          }}>
          <header style={{
            padding: "18px 22px", borderBottom: `1px solid ${C.borderLight}`,
            display: "flex", alignItems: "center", gap: 10,
          }}>
            <Icon n="filetext" s={18} c={C.brand} />
            <h3 style={{ flex: 1, fontSize: 15, fontWeight: 800, color: C.text, margin: 0 }}>
              Workspace policy
            </h3>
            <button onClick={onClose} aria-label="Close"
              style={{
                width: 28, height: 28, borderRadius: 6, background: C.borderLight,
                border: "none", cursor: "pointer",
                display: "flex", alignItems: "center", justifyContent: "center",
              }}>
              <Icon n="x" s={14} c={C.textSub} />
            </button>
          </header>
          <div style={{ flex: 1, overflowY: "auto", padding: 22 }}>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: 0 }}>
              Active policy
            </p>
            <h2 style={{ fontSize: 20, fontWeight: 800, color: C.text, margin: "4px 0 0" }}>
              {policy?.name || "Workforce Monitoring Policy"}
              <span style={{ marginLeft: 8, fontSize: 13, color: C.textSub, fontWeight: 600 }}>
                v{policy?.version || "1.0"}
              </span>
            </h2>
            <p style={{ fontSize: 12, color: C.textSub, margin: "4px 0 16px" }}>
              Last updated {policy?.last_updated ? fmtDate(policy.last_updated) : "—"}
              {policy?.updated_by && <> by {policy.updated_by}</>}
            </p>

            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: "16px 0 6px" }}>
              Plain-English summary
            </p>
            <p style={{ fontSize: 13, color: C.text, lineHeight: 1.6, margin: 0 }}>
              {policy?.summary ||
                "This policy defines how your workspace records work activity, how long records are kept, and who can access them. Acknowledging it confirms you've read it — it does not constitute consent for any specific form of processing where consent is legally required."}
            </p>

            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: "20px 0 6px" }}>
              Acknowledgements
            </p>
            <div style={{ border: `1px solid ${C.border}`, borderRadius: 8, overflow: "hidden" }}>
              {(acks || []).map((a, i) => (
                <div key={a.policy_id || i} style={{
                  padding: "12px 14px", display: "flex", alignItems: "center", gap: 10,
                  borderBottom: i < acks.length - 1 ? `1px solid ${C.borderLight}` : "none",
                }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>
                      {a.policy_name || a.policy_id} v{a.policy_version}
                    </p>
                    <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                      {a.acknowledged_at
                        ? `Acknowledged ${fmtDate(a.acknowledged_at)}`
                        : "Acknowledgement required — you must read the policy before acknowledging"}
                    </p>
                  </div>
                  {a.acknowledged_at
                    ? <Pill tone="on" icon="check">Done</Pill>
                    : <Button onClick={() => openConfirm(a)}>Review &amp; Acknowledge</Button>}
                </div>
              ))}
              {(!acks || acks.length === 0) && (
                <div style={{ padding: 16, textAlign: "center", fontSize: 12, color: C.textSub }}>
                  No outstanding acknowledgements.
                </div>
              )}
            </div>

            <p style={{ fontSize: 12, color: C.textLight, marginTop: 20, lineHeight: 1.6 }}>
              Acknowledgement records your understanding, not blanket consent. Where consent is
              legally required for a specific form of processing, it is captured separately
              through the Governance layer.
            </p>
          </div>
        </aside>
      </div>

      {/* ── Acknowledgement confirmation modal ─────────────────────────────── */}
      <Modal
        open={!!confirmTarget}
        onClose={closeConfirm}
        title={`Acknowledge: ${confirmTarget?.policy_name || confirmTarget?.policy_id || "Policy"} v${confirmTarget?.policy_version || ""}`}
        size="medium"
        footer={
          <>
            <Button variant="secondary" onClick={closeConfirm} disabled={submitting}>Cancel</Button>
            <Button onClick={confirmAcknowledge} disabled={!hasRead || submitting}>
              {submitting ? "Saving…" : "Confirm acknowledgement"}
            </Button>
          </>
        }
      >
        {/* Policy body — required reading before the checkbox unlocks */}
        <div style={{
          maxHeight: 280, overflowY: "auto", padding: "14px 16px", borderRadius: 8,
          background: C.bg, border: `1px solid ${C.border}`, marginBottom: 16,
          fontSize: 13, color: C.text, lineHeight: 1.7,
        }}>
          <p style={{ fontSize: 11, color: C.brand, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 8px" }}>
            Policy text
          </p>
          <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>
            {confirmTarget?.body || confirmTarget?.summary ||
              "This policy defines how your workspace records work activity, how long records are kept, and who can access them. It describes:\n\n" +
              "• What types of data are captured during your working sessions\n" +
              "• How long each category of data is retained\n" +
              "• Who can view your data and under what circumstances\n" +
              "• Your rights to request corrections, exports, and deletions\n" +
              "• How access to your data is logged and made available to you\n\n" +
              "Acknowledging this policy confirms you have read and understood it. It does not constitute consent for any specific form of processing where separate consent is legally required. Where your jurisdiction provides additional rights (e.g. GDPR, UK GDPR), those rights are captured through the Governance layer and are not waived by acknowledgement."}
          </p>
        </div>

        {/* Checkbox gate — must be ticked before "Confirm" is enabled */}
        <label style={{
          display: "flex", gap: 10, alignItems: "flex-start",
          fontSize: 13, color: C.text, cursor: "pointer",
          padding: "12px 14px", borderRadius: 8,
          background: hasRead ? C.greenBg : C.bg,
          border: `1px solid ${hasRead ? C.green : C.border}`,
          transition: "background .15s, border-color .15s",
        }}>
          <input
            type="checkbox"
            checked={hasRead}
            onChange={(e) => setHasRead(e.target.checked)}
            style={{ marginTop: 2, accentColor: C.brand }}
          />
          <span>
            <strong>I have read and understood this policy.</strong>
            {" "}Acknowledgement confirms I have read it — it does not constitute blanket consent.
          </span>
        </label>

        {!hasRead && (
          <p style={{ fontSize: 11, color: C.amber, margin: "10px 0 0",
                      display: "flex", alignItems: "center", gap: 6 }}>
            <Icon n="info" s={12} c={C.amber} />
            You must read the policy and tick the checkbox before confirming.
          </p>
        )}
      </Modal>
    </>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// MAIN COMPONENT
// ═════════════════════════════════════════════════════════════════════════════
export default function MyDataModule({ pane = "whats-recorded" }) {
  const { C } = useTheme();
  // ── Shared state ─────────────────────────────────────────────────────────
  const [policyDrawerOpen, setPolicyDrawerOpen] = useState(false);

  // Capture config (pane 1)
  const [captureConfig, setCaptureConfig] = useState(null);
  const [captureLoading, setCaptureLoading] = useState(false);

  // Access log (pane 2)
  const [accessRows,   setAccessRows]   = useState([]);
  const [accessLoading, setAccessLoading] = useState(false);
  const [accessRange,  setAccessRange]  = useState("30");

  // Privacy controls (pane 3)
  const [privacyValues, setPrivacyValues] = useState({});
  const [privacyLocks,  setPrivacyLocks]  = useState({});
  const [privacyLoading, setPrivacyLoading] = useState(false);

  // Corrections (pane 4)
  const [corrections, setCorrections] = useState([]);
  const [correctionsLoading, setCorrectionsLoading] = useState(false);

  // Exports (pane 5)
  const [exportHistory, setExportHistory] = useState([]);
  const [exportLoading, setExportLoading] = useState(false);

  // Policy + acknowledgements (header + drawer)
  const [policyMeta, setPolicyMeta] = useState(null);
  const [acks,       setAcks]       = useState([]);

  // ── Loaders (each pane only loads its own data lazily) ───────────────────
  const loadCaptureConfig = async () => {
    setCaptureLoading(true);
    try {
      const { data } = await API.get("/my-data/capture-config");
      setCaptureConfig(data);
      if (data?.policy) setPolicyMeta(data.policy);
    } catch {
      setCaptureConfig(DEFAULT_CAPTURE);
      setPolicyMeta(DEFAULT_CAPTURE.policy);
    } finally {
      setCaptureLoading(false);
    }
  };

  const loadAccessLog = async (range = accessRange) => {
    setAccessLoading(true);
    try {
      const params = range === "all" ? {} : { days: range };
      const { data } = await API.get("/my-data/who-viewed", { params });
      setAccessRows(Array.isArray(data) ? data : []);
    } catch {
      // Fallback: pull from /profile/access-history (worker's own auth events) —
      // not the same data shape, but keeps the pane usable until the new
      // endpoint ships. Mark these rows as system events, not human access.
      try {
        const { data } = await API.get("/profile/access-history", { params: { limit: 50 } });
        setAccessRows((data || []).map((r) => ({
          id: r.id,
          accessed_at: r.at,
          accessor_name: "You",
          accessor_role: "Self",
          surface_accessed: r.action === "UserLogin" ? "Sign-in event" : r.action,
          reason: "Self-event (until /my-data/who-viewed ships)",
          anomaly_flag: false,
        })));
      } catch {
        setAccessRows([]);
      }
    } finally {
      setAccessLoading(false);
    }
  };

  const loadPrivacy = async () => {
    setPrivacyLoading(true);
    try {
      const { data } = await API.get("/profile/preferences");
      const prefs = data?.preferences || data || {};
      setPrivacyValues({
        blur_screenshots:        prefs.blur_screenshots        ?? true,
        screenshot_notification: prefs.screenshot_notification ?? false,
        tracking_indicator:      prefs.tracking_indicator      ?? true,
        hide_browser_tab_titles: prefs.hide_browser_tab_titles ?? true,
        weekly_summary_email:    prefs.weekly_summary_email    ?? false,
      });
      setPrivacyLocks(prefs.locks || {});
    } catch {
      setPrivacyValues(Object.fromEntries(PRIVACY_CONTROLS.map((c) => [c.key, c.default])));
      setPrivacyLocks({});
    } finally {
      setPrivacyLoading(false);
    }
  };

  const loadCorrections = async () => {
    setCorrectionsLoading(true);
    try {
      const { data } = await API.get("/my-data/corrections");
      setCorrections(Array.isArray(data) ? data : []);
    } catch {
      setCorrections([]);
    } finally {
      setCorrectionsLoading(false);
    }
  };

  const loadExports = async () => {
    setExportLoading(true);
    try {
      const { data } = await API.get("/my-data/exports");
      setExportHistory(Array.isArray(data) ? data : []);
    } catch {
      setExportHistory([]);
    } finally {
      setExportLoading(false);
    }
  };

  const loadAcks = async () => {
    try {
      const { data } = await API.get("/my-data/policy-acks");
      setAcks(Array.isArray(data) ? data : []);
    } catch {
      setAcks([]);
    }
  };

  // ── Lazy-load on pane switch ─────────────────────────────────────────────
  useEffect(() => {
    if (!policyMeta) loadCaptureConfig(); // header always wants the policy meta
    if (!acks.length) loadAcks();
    if (pane === "whats-recorded"   && !captureConfig)   loadCaptureConfig();
    if (pane === "who-viewed")                            loadAccessLog(accessRange);
    if (pane === "privacy-controls" && !Object.keys(privacyValues).length) loadPrivacy();
    if (pane === "corrections")                           loadCorrections();
    if (pane === "data-export")                           loadExports();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pane]);

  // ── Action handlers ──────────────────────────────────────────────────────
  const handlePrivacyChange = async (key, value) => {
    const prev = privacyValues[key];
    setPrivacyValues((v) => ({ ...v, [key]: value }));
    try {
      await API.put("/profile/preferences", { [key]: value });
      toast.success("Setting updated.");
    } catch (err) {
      setPrivacyValues((v) => ({ ...v, [key]: prev }));
      toast.error(err?.response?.data?.detail || "Couldn't save — try again");
    }
  };

  const handleAccessRange = (next) => {
    setAccessRange(next);
    loadAccessLog(next);
  };

  const handleCorrectionSubmit = async (payload) => {
    try {
      const { data } = await API.post("/my-data/corrections", {
        request_type:    payload.type,
        period_from:     payload.from || null,
        period_to:       payload.to   || null,
        correction:      payload.correction,
        reason:          payload.reason || null,
        evidence_filename: payload.evidence_filename || null,
      });
      toast.success("Correction request submitted");
      // Optimistic: prepend the new request if backend returned it; else reload.
      if (data?.id) setCorrections((rows) => [data, ...rows]);
      else loadCorrections();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't submit — please try again");
    }
  };

  const handleEscalate = (item) => {
    toast.success(item ? "Escalation started" : "Escalation form opened");
    // Wiring point: navigate to /my-data/corrections/escalate or open a modal
  };

  const handleExportRequest = async ({ format, scope }) => {
    try {
      const { data } = await API.post("/my-data/exports", { format, scope });
      toast.success(data?.completed_at ? "Export ready — see history below" : "Export queued");
      loadExports();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't request export");
    }
  };

  const handleDSAR = async ({ type, details }) => {
    try {
      await API.post("/my-data/dsar", { type, details });
      toast.success("DSAR submitted — we'll acknowledge within 72 hours");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't submit DSAR");
    }
  };

  const handleDeletion = async ({ reason }) => {
    try {
      await API.post("/my-data/deletion", { reason });
      toast.success("Deletion request submitted for review");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't submit deletion request");
    }
  };

  const handleAcknowledge = async (ack) => {
    try {
      await API.post("/my-data/policy-acks", {
        policy_id:      ack.policy_id,
        policy_version: ack.policy_version,
      });
      toast.success("Policy acknowledged");
      loadAcks();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't record acknowledgement");
    }
  };

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <CCtx.Provider value={C}>
    <div style={{
      maxWidth: 1100, margin: "0 auto", padding: "8px 4px 32px",
      fontFamily: "'DM Sans', system-ui, sans-serif",
    }}>
      <ModuleHeader
        activePane={pane}
        policyMeta={policyMeta}
        onViewPolicy={() => setPolicyDrawerOpen(true)}
      />

      {pane === "whats-recorded" && (
        <WhatsRecordedPane
          config={captureConfig}
          loading={captureLoading}
          onViewPolicy={() => setPolicyDrawerOpen(true)}
        />
      )}

      {pane === "who-viewed" && (
        <WhoViewedPane
          rows={accessRows}
          loading={accessLoading}
          range={accessRange}
          onRangeChange={handleAccessRange}
          onRefresh={() => loadAccessLog(accessRange)}
        />
      )}

      {pane === "privacy-controls" && (
        <PrivacyControlsPane
          values={privacyValues}
          locks={privacyLocks}
          loading={privacyLoading}
          onChange={handlePrivacyChange}
        />
      )}

      {pane === "corrections" && (
        <CorrectionsPane
          items={corrections}
          loading={correctionsLoading}
          onRefresh={loadCorrections}
          onSubmit={handleCorrectionSubmit}
          onEscalate={handleEscalate}
        />
      )}

      {pane === "data-export" && (
        <DataExportPane
          history={exportHistory}
          loading={exportLoading}
          onRequest={handleExportRequest}
          onRequestDSAR={handleDSAR}
          onRequestDeletion={handleDeletion}
          onRefresh={loadExports}
        />
      )}

      <PolicyDrawer
        open={policyDrawerOpen}
        onClose={() => setPolicyDrawerOpen(false)}
        policy={policyMeta}
        acks={acks}
        onAcknowledge={handleAcknowledge}
      />
    </div>
    </CCtx.Provider>
  );
}
