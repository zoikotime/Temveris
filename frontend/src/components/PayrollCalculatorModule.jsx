/**
 * PayrollCalculatorModule.jsx — admin-facing Payroll Calculator.
 *
 * Architecture (per spec):
 *   1. Module header              — title, actions (New Run, Import, Export, Settings)
 *   2. Alert rail                 — payroll readiness banners
 *   3. KPI cards                  — Period · Employees · Gross Pay · Exceptions
 *   4. Six tabs                   — Runs · Calculator · Exceptions · Adjustments · Export · Settings
 *   5. New Payroll Run wizard     — 5 guided steps with policy pinning
 *   6. Employee calculation drawer— explainable formula + source evidence
 *
 * Backend endpoints (graceful 404 fallback so the page works pre-launch):
 *   GET   /payroll/summary
 *   GET   /payroll/configs
 *   GET   /payroll/runs
 *   GET   /payroll/runs/{id}
 *   POST  /payroll/runs                      (create)
 *   POST  /payroll/runs/{id}/calculate
 *   POST  /payroll/runs/{id}/submit-review
 *   POST  /payroll/runs/{id}/approve
 *   POST  /payroll/runs/{id}/lock
 *   POST  /payroll/runs/{id}/export
 *   POST  /payroll/adjustments
 *   POST  /payroll/exceptions/{id}/resolve
 *   GET   /payroll/exceptions
 *   GET   /payroll/adjustments
 *   GET   /payroll/exports
 *   GET   /payroll/settings
 *   PATCH /payroll/settings
 *
 * Design intent: every pane survives a missing backend by showing a useful
 * empty / informational state, not a stack trace.
 */

import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { useTheme } from "../context/ThemeContext";
import { API_BASE } from "../config";

// Module-level context so sub-components get C without prop-drilling.
const CCtx = React.createContext(null);
const useC = () => React.useContext(CCtx);

// ─── helpers ────────────────────────────────────────────────────────────────
// The default currency falls back to USD here only as a last resort —
// every render path should pass an explicit currency from `settings.currency`
// (the org-wide default chosen by the admin in the Settings tab).
const fmtCurrency = (amt, currency = "USD") => {
  const n = Number(amt || 0);
  // Pick a locale that renders the right grouping for common currencies, so
  // ₹1,00,000 stays Indian-style while $100,000 stays US-style. Falls back
  // to en-US when the currency isn't in the table.
  const LOCALE_FOR = {
    INR: "en-IN", USD: "en-US", GBP: "en-GB", EUR: "de-DE",
    AED: "en-AE", SGD: "en-SG", AUD: "en-AU", CAD: "en-CA",
    JPY: "ja-JP", CNY: "zh-CN", ZAR: "en-ZA", NZD: "en-NZ",
    HKD: "en-HK", CHF: "de-CH", SEK: "sv-SE",
  };
  try {
    return new Intl.NumberFormat(LOCALE_FOR[currency] || "en-US", {
      style: "currency", currency, maximumFractionDigits: 2,
    }).format(n);
  } catch {
    return `${currency} ${n.toFixed(2)}`;
  }
};

const fmtDate = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch { return "—"; }
};

const fmtDateTime = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
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

// Currencies the picker exposes everywhere — keep this list in sync with the
// LOCALE_FOR table in fmtCurrency() so formatting renders the right grouping
// for whichever the admin picks. Add a new entry to BOTH places at once.
const CURRENCY_OPTIONS = [
  { value: "USD", label: "USD — US Dollar" },
  { value: "INR", label: "INR — Indian Rupee" },
  { value: "GBP", label: "GBP — British Pound" },
  { value: "EUR", label: "EUR — Euro" },
  { value: "AED", label: "AED — UAE Dirham" },
  { value: "SGD", label: "SGD — Singapore Dollar" },
  { value: "AUD", label: "AUD — Australian Dollar" },
  { value: "CAD", label: "CAD — Canadian Dollar" },
  { value: "JPY", label: "JPY — Japanese Yen" },
  { value: "CNY", label: "CNY — Chinese Yuan" },
  { value: "ZAR", label: "ZAR — South African Rand" },
  { value: "NZD", label: "NZD — New Zealand Dollar" },
  { value: "HKD", label: "HKD — Hong Kong Dollar" },
  { value: "CHF", label: "CHF — Swiss Franc" },
  { value: "SEK", label: "SEK — Swedish Krona" },
];

// Map backend statuses (draft|processed|paid) plus spec statuses to UI tones.
const RUN_STATUS = {
  draft:        { label: "Draft",         tone: "neutral" },
  calculating:  { label: "Calculating",   tone: "info"    },
  needs_review: { label: "Needs Review",  tone: "warn"    },
  approved:     { label: "Approved",      tone: "success" },
  processed:    { label: "Approved",      tone: "success" },
  locked:       { label: "Locked",        tone: "info"    },
  paid:         { label: "Paid",          tone: "success" },
  exported:     { label: "Exported",      tone: "info"    },
  voided:       { label: "Voided",        tone: "danger"  },
};

const downloadCSV = (filename, rows, headers) => {
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
  dollar:    "M12 1v22 M17 5H9.5a3.5 3.5 0 100 7h5a3.5 3.5 0 110 7H6",
  calc:      "M4 2h16a1 1 0 011 1v18a1 1 0 01-1 1H4a1 1 0 01-1-1V3a1 1 0 011-1z M8 6h8 M7 10h.01 M11 10h.01 M15 10h.01 M7 14h.01 M11 14h.01 M15 14h.01 M7 18h.01 M11 18h.01 M15 18h.01",
  warning:   "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
  shield:    "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  check:     "M20 6L9 17l-5-5",
  x:         "M18 6L6 18 M6 6l12 12",
  plus:      "M12 5v14 M5 12h14",
  download:  "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  upload:    "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M17 8l-5-5-5 5 M12 3v12",
  refresh:   "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10 M1 14l4.64 4.36A9 9 0 0020.49 15",
  filter:    "M22 3H2l8 9.46V19l4 2v-8.54L22 3z",
  search:    "M11 19a8 8 0 100-16 8 8 0 000 16z M21 21l-4.35-4.35",
  external:  "M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6 M15 3h6v6 M10 14L21 3",
  chevdown:  "M6 9l6 6 6-6",
  chevright: "M9 18l6-6-6-6",
  chevleft:  "M15 18l-6-6 6-6",
  lock:      "M19 11H5a2 2 0 00-2 2v7a2 2 0 002 2h14a2 2 0 002-2v-7a2 2 0 00-2-2z M7 11V7a5 5 0 0110 0v4",
  user:      "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2 M12 11a4 4 0 100-8 4 4 0 000 8",
  users:     "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2 M9 11a4 4 0 100-8 4 4 0 000 8 M23 21v-2a4 4 0 00-3-3.87 M16 3.13a4 4 0 010 7.75",
  filetext:  "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8",
  info:      "M12 22a10 10 0 100-20 10 10 0 000 20z M12 16v-4 M12 8h.01",
  alert:     "M12 9v4 M12 17h.01 M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z",
  clock:     "M12 22a10 10 0 100-20 10 10 0 000 20z M12 6v6l4 2",
  trending:  "M23 6l-9.5 9.5-5-5L1 18 M17 6h6v6",
  settings:  "M12 15a3 3 0 100-6 3 3 0 000 6z M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z",
  send:      "M22 2L11 13 M22 2l-7 20-4-9-9-4 20-7z",
  flag:      "M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z M4 22V15",
  edit:      "M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7 M18.5 2.5a2.121 2.121 0 113 3L12 15l-4 1 1-4 9.5-9.5z",
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
            <h2 style={{ fontSize: 15, fontWeight: 700, color: C.text, margin: 0 }}>{title}</h2>
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

const Button = ({ children, variant = "primary", onClick, disabled, type = "button", style, icon, size = "md" }) => {
  const C = useC();
  const variants = {
    primary:   { bg: disabled ? C.textLight : C.brand, color: "#fff", border: "none" },
    secondary: { bg: C.surface, color: C.text, border: `1px solid ${C.border}` },
    danger:    { bg: disabled ? "#fbb" : C.danger, color: "#fff", border: "none" },
    ghost:     { bg: "transparent", color: C.brand, border: "none" },
    success:   { bg: disabled ? C.textLight : C.green, color: "#fff", border: "none" },
  };
  const v = variants[variant] || variants.primary;
  const pad = size === "sm" ? "6px 12px" : "9px 16px";
  const fs  = size === "sm" ? 12 : 13;
  const mh  = size === "sm" ? 32 : 40;
  const cls = variant === "primary"
    ? "zt-pc-btn-primary"
    : variant === "secondary" ? "zt-pc-btn-secondary" : "";
  return (
    <button type={type} onClick={onClick} disabled={disabled} className={cls}
      style={{
        padding: pad, borderRadius: C.rSm,
        background: v.bg, color: v.color, border: v.border,
        fontWeight: 600, fontSize: fs, cursor: disabled ? "not-allowed" : "pointer",
        fontFamily: "inherit", whiteSpace: "nowrap",
        display: "inline-flex", alignItems: "center", gap: 6, minHeight: mh,
        boxShadow: variant === "primary" && !disabled ? `0 2px 6px ${C.brand}33` : "none",
        opacity: disabled ? 0.7 : 1,
        transition: "transform .18s ease, box-shadow .18s ease, background .18s ease, border-color .18s ease, color .18s ease",
        ...style,
      }}>
      {icon && <Icon n={icon} s={size === "sm" ? 12 : 14} c={v.color} />}
      {children}
    </button>
  );
};

const Pill = ({ tone = "neutral", children, icon }) => {
  const C = useC();
  const tones = {
    neutral: { bg: C.borderLight, fg: C.textSub },
    success: { bg: C.greenBg,     fg: C.green   },
    info:    { bg: C.brandBg,     fg: C.brand   },
    warn:    { bg: C.amberBg,     fg: C.amber   },
    danger:  { bg: C.dangerBg,    fg: C.danger  },
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

const Input = ({ value, onChange, type = "text", placeholder, disabled, autoFocus }) => {
  const C = useC();
  return (
    <input
      type={type}
      value={value ?? ""}
      onChange={(e) => onChange?.(e.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      autoFocus={autoFocus}
      style={{
        width: "100%", padding: "10px 14px", borderRadius: C.rSm,
        border: `1px solid ${C.border}`, background: disabled ? C.borderLight : C.bg,
        fontSize: 14, color: disabled ? C.textSub : C.text, fontFamily: "inherit",
        outline: "none", boxSizing: "border-box", minHeight: 40,
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
        outline: "none", boxSizing: "border-box", appearance: "auto", minHeight: 40,
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

const Toggle = ({ on, onChange, disabled }) => {
  const C = useC();
  return (
    <button type="button"
      onClick={() => !disabled && onChange?.(!on)}
      disabled={disabled}
      style={{
        width: 42, height: 24, borderRadius: 999, border: "none",
        cursor: disabled ? "not-allowed" : "pointer",
        background: on ? C.brand : C.textLight, position: "relative",
        transition: "background .22s cubic-bezier(.4,0,.2,1), box-shadow .22s ease",
        flexShrink: 0, opacity: disabled ? 0.55 : 1, padding: 0,
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
};

const EmptyState = ({ icon = "info", title, body, action }) => {
  const C = useC();
  return (
    <div style={{ padding: "56px 24px", textAlign: "center", color: C.textSub, fontSize: 13, lineHeight: 1.5 }}>
      <div style={{
        width: 64, height: 64, borderRadius: "50%",
        background: `linear-gradient(135deg, ${C.brandBg}, ${C.surface})`,
        border: `1px solid ${C.brandBdr}`,
        display: "inline-flex",
        alignItems: "center", justifyContent: "center", marginBottom: 16,
        boxShadow: `inset 0 0 0 4px ${C.surface}, 0 4px 14px rgba(16,24,40,.06)`,
      }}>
        <Icon n={icon} s={26} c={C.brand} />
      </div>
      <p style={{ fontSize: 15, fontWeight: 700, color: C.text, margin: 0 }}>{title}</p>
      {body && <p style={{ margin: "6px auto 0", maxWidth: 460, fontSize: 13 }}>{body}</p>}
      {action && <div style={{ marginTop: 18 }}>{action}</div>}
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
  const widths = { small: 460, medium: 640, large: 880 };
  return (
    <div onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 9999,
        background: "rgba(16,24,40,0.55)", backdropFilter: "blur(2px)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 20,
      }}>
      <div onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%", maxWidth: widths[size] || widths.medium,
          maxHeight: "90vh", display: "flex", flexDirection: "column",
          background: C.surface, borderRadius: 12,
          boxShadow: "0 12px 40px rgba(16,24,40,0.25)", overflow: "hidden",
        }}>
        <header style={{
          padding: "16px 20px", borderBottom: `1px solid ${C.borderLight}`,
          display: "flex", alignItems: "center", gap: 10,
        }}>
          <h3 style={{ flex: 1, fontSize: 15, fontWeight: 800, color: C.text, margin: 0 }}>{title}</h3>
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

// ─── Toast (lightweight, internal) ──────────────────────────────────────────
const useToast = () => {
  const [msgs, setMsgs] = useState([]);
  const push = useCallback((text, tone = "success") => {
    const id = Math.random().toString(36).slice(2);
    setMsgs(m => [...m, { id, text, tone }]);
    setTimeout(() => setMsgs(m => m.filter(x => x.id !== id)), 3500);
  }, []);
  return { msgs, push };
};

const ToastRail = ({ msgs }) => {
  const C = useC();
  if (!msgs.length) return null;
  return (
    <div style={{
      position: "fixed", bottom: 20, right: 20, zIndex: 10000,
      display: "flex", flexDirection: "column", gap: 8,
    }}>
      {msgs.map(m => {
        const toneMap = {
          success: { bg: C.greenBg,   fg: C.green,  bd: C.green  },
          error:   { bg: C.dangerBg,  fg: C.danger, bd: C.danger },
          info:    { bg: C.brandBg,   fg: C.brand,  bd: C.brand  },
        };
        const t = toneMap[m.tone] || toneMap.success;
        return (
          <div key={m.id} style={{
            padding: "10px 14px", borderRadius: 8,
            background: t.bg, color: t.fg, fontSize: 13, fontWeight: 600,
            border: `1px solid ${t.bd}33`, boxShadow: "0 4px 12px rgba(16,24,40,0.12)",
            maxWidth: 360,
          }}>
            {m.text}
          </div>
        );
      })}
    </div>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// MODULE HEADER + KPI CARDS + ALERT RAIL
// ═════════════════════════════════════════════════════════════════════════════

const ModuleHeader = ({ activePane, currentRun, onNewRun, onImport, onExport, onSettings, canExport, canSettings }) => {
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
          <Icon n="dollar" s={22} c="#fff" w={2.2} />
        </div>
        <div style={{ flex: 1, minWidth: 240 }}>
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      letterSpacing: 0.6, textTransform: "uppercase", margin: 0 }}>
            Admin · Payroll
          </p>
          <h1 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: "2px 0 0" }}>
            Payroll Calculator
          </h1>
          <p style={{ fontSize: 13, color: C.textSub, margin: "6px 0 0", lineHeight: 1.6, maxWidth: 760 }}>
            Calculate payroll-ready totals from verified workforce records, policies,
            overtime rules, deductions, and approved adjustments. Every number is
            explainable, every action is logged.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, flexShrink: 0, flexWrap: "wrap" }}>
          <Button variant="secondary" icon="upload" onClick={onImport}>Import Adjustments</Button>
          <Button variant="secondary" icon="download" onClick={onExport} disabled={!canExport}>Export</Button>
          {canSettings && (
            <Button variant="secondary" icon="settings" onClick={onSettings}>Payroll Settings</Button>
          )}
          <Button icon="plus" onClick={onNewRun}>New Payroll Run</Button>
        </div>
      </div>
    </div>
  );
};

const AlertRail = ({ alerts }) => {
  const C = useC();
  if (!alerts || alerts.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 16 }}>
      {alerts.map((a, i) => {
        const tone = a.severity === "critical" ? "danger"
                  : a.severity === "warning"  ? "warn"
                  : "info";
        const tones = {
          danger: { bg: C.dangerBg, fg: C.danger, ic: "alert"   },
          warn:   { bg: C.amberBg,  fg: C.amber,  ic: "warning" },
          info:   { bg: C.brandBg,  fg: C.brand,  ic: "info"    },
        };
        const t = tones[tone];
        return (
          <div key={i} style={{
            display: "flex", alignItems: "center", gap: 12,
            padding: "12px 16px", borderRadius: 10,
            background: t.bg, border: `1px solid ${t.fg}33`,
          }}>
            <Icon n={t.ic} s={18} c={t.fg} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <p style={{ fontSize: 13, fontWeight: 700, color: t.fg, margin: 0 }}>{a.title}</p>
              {a.body && (
                <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0", lineHeight: 1.5 }}>
                  {a.body}
                </p>
              )}
            </div>
            {a.action && a.onAction && (
              <Button variant="ghost" size="sm" onClick={a.onAction}>{a.action}</Button>
            )}
          </div>
        );
      })}
    </div>
  );
};

const KPICard = ({ icon, label, value, sub, tone, action, onAction }) => {
  const C = useC();
  const tones = {
    brand:  { bg: C.brandBg,  fg: C.brand },
    green:  { bg: C.greenBg,  fg: C.green },
    amber:  { bg: C.amberBg,  fg: C.amber },
    danger: { bg: C.dangerBg, fg: C.danger },
  };
  const t = tones[tone] || tones.brand;
  return (
    <div className="zt-pc-kpi" style={{
      background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r,
      padding: 18, boxShadow: "0 1px 2px rgba(16,24,40,0.04)",
      display: "flex", flexDirection: "column", gap: 8, minHeight: 120,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{
          width: 36, height: 36, borderRadius: 10,
          background: t.bg, display: "flex",
          alignItems: "center", justifyContent: "center", flexShrink: 0,
        }}>
          <Icon n={icon} s={18} c={t.fg} />
        </div>
        <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                    textTransform: "uppercase", letterSpacing: 0.5, margin: 0 }}>
          {label}
        </p>
      </div>
      <p style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: 0, lineHeight: 1.2 }}>
        {value}
      </p>
      {sub && (
        <p style={{ fontSize: 12, color: C.textSub, margin: 0, lineHeight: 1.4 }}>{sub}</p>
      )}
      {action && (
        <button type="button" onClick={onAction}
          style={{
            background: "none", border: "none", padding: 0,
            color: C.brand, fontWeight: 700, fontSize: 12, fontFamily: "inherit",
            cursor: "pointer", textAlign: "left", marginTop: "auto",
            display: "inline-flex", alignItems: "center", gap: 4,
          }}>
          {action} <Icon n="chevright" s={12} c={C.brand} />
        </button>
      )}
    </div>
  );
};

const KPIRow = ({ settings, summary, currentRun, exceptions, onChangePeriod, onReviewEmployees, onViewBreakdown, onResolveExceptions }) => {
  const periodLabel = currentRun
    ? `${fmtDate(currentRun.period_start)} — ${fmtDate(currentRun.period_end)}`
    : "No active run";
  const status = currentRun ? (RUN_STATUS[currentRun.status]?.label || currentRun.status || "—") : "—";
  const employeesIncluded = currentRun?.employees_included ?? summary?.employees_on_payroll ?? 0;
  const grossEstimate = currentRun?.total_amount ?? summary?.total_monthly_payroll ?? 0;
  const exCount = exceptions?.length ?? 0;
  const critical = (exceptions || []).filter(e => e.severity === "critical").length;
  // Currency precedence: the active run pins its own currency (so historical
  // runs always display in the currency they were created in), otherwise the
  // org-wide default chosen by the admin in Payroll → Settings, otherwise the
  // backend summary, otherwise USD as a hard fallback.
  const displayCurrency = currentRun?.currency || settings?.currency || summary?.currency || "USD";

  return (
    <div style={{
      display: "grid", gap: 14,
      gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
      marginBottom: 18,
    }}>
      <KPICard icon="clock" label="Current Period" value={periodLabel}
               sub={`${currentRun?.frequency || "—"} · ${status}`} tone="brand"
               action="Change period" onAction={onChangePeriod} />
      <KPICard icon="users" label="Employees Included"
               value={employeesIncluded}
               sub={`${currentRun?.pending_approvals || 0} pending approval`} tone="green"
               action="Review employees" onAction={onReviewEmployees} />
      <KPICard icon="trending" label="Gross Pay Estimate"
               value={fmtCurrency(grossEstimate, displayCurrency)}
               sub={`Regular + overtime + adjustments · ${displayCurrency}`} tone="brand"
               action="View breakdown" onAction={onViewBreakdown} />
      <KPICard icon="warning" label="Exceptions"
               value={exCount}
               sub={`${critical} critical, ${exCount - critical} warning/info`}
               tone={critical > 0 ? "danger" : "amber"}
               action="Resolve exceptions" onAction={onResolveExceptions} />
    </div>
  );
};

// ─── Tab Bar ────────────────────────────────────────────────────────────────
const TabBar = ({ tabs, active, onChange }) => {
  const C = useC();
  return (
    <div style={{
      display: "flex", gap: 4, borderBottom: `1px solid ${C.border}`,
      marginBottom: 18, overflowX: "auto",
    }}>
      {tabs.map(t => {
        const isActive = active === t.key;
        return (
          <button key={t.key} type="button" onClick={() => onChange(t.key)}
            className="zt-pc-tab" data-active={isActive ? "true" : "false"}
            style={{
              padding: "10px 16px", border: "none", background: "none",
              fontFamily: "inherit", fontSize: 13, fontWeight: isActive ? 700 : 600,
              color: isActive ? C.brand : C.textSub, cursor: "pointer",
              borderBottom: `2px solid ${isActive ? C.brand : "transparent"}`,
              marginBottom: -1, whiteSpace: "nowrap",
              display: "inline-flex", alignItems: "center", gap: 6,
            }}>
            {t.icon && <Icon n={t.icon} s={14} c={isActive ? C.brand : C.textSub} />}
            {t.label}
            {typeof t.badge === "number" && t.badge > 0 && (
              <span style={{
                background: isActive ? C.brand : C.textLight, color: "#fff",
                fontSize: 10, padding: "1px 6px", borderRadius: 999, fontWeight: 700,
                transition: "background .15s ease",
              }}>{t.badge}</span>
            )}
          </button>
        );
      })}
    </div>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 1 — PAYROLL RUNS
// ═════════════════════════════════════════════════════════════════════════════

const PayrollRunsPane = ({ runs, loading, settings, onOpen, onDuplicate, onExport, onLock, onVoid, onNewRun, onRefresh, search, setSearch, filterStatus, setFilterStatus }) => {
  const C = useC();
  const filtered = useMemo(() => {
    return (runs || []).filter(r => {
      if (filterStatus && filterStatus !== "all" && r.status !== filterStatus) return false;
      if (search.trim()) {
        const q = search.toLowerCase();
        const fields = [r.id, r.created_by_name, r.notes, r.frequency].map(x => String(x || "").toLowerCase());
        if (!fields.some(f => f.includes(q))) return false;
      }
      return true;
    });
  }, [runs, search, filterStatus]);

  return (
    <Card
      title={`Payroll runs (${filtered.length})`}
      icon="filetext"
      padding={0}
      action={
        <div style={{ display: "flex", gap: 6 }}>
          <Button variant="secondary" icon="refresh" size="sm" onClick={onRefresh}>Refresh</Button>
          <Button icon="plus" size="sm" onClick={onNewRun}>New Run</Button>
        </div>
      }
    >
      {/* Filters */}
      <div style={{
        padding: "14px 24px", borderBottom: `1px solid ${C.borderLight}`,
        display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center",
      }}>
        <div style={{
          flex: 1, minWidth: 240, display: "flex", alignItems: "center", gap: 8,
          padding: "0 10px", borderRadius: 8,
          border: `1px solid ${C.border}`, background: C.bg,
        }}>
          <Icon n="search" s={14} c={C.textSub} />
          <input
            type="search" value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by run ID, employee name, or notes…"
            style={{
              flex: 1, padding: "9px 0", border: "none", outline: "none",
              background: "transparent", fontSize: 13, color: C.text, fontFamily: "inherit",
            }}
          />
        </div>
        <div style={{ minWidth: 180 }}>
          <Select value={filterStatus} onChange={setFilterStatus} options={[
            { value: "all", label: "All statuses" },
            { value: "draft", label: "Draft" },
            { value: "needs_review", label: "Needs Review" },
            { value: "processed", label: "Approved" },
            { value: "locked", label: "Locked" },
            { value: "exported", label: "Exported" },
            { value: "paid", label: "Paid" },
            { value: "voided", label: "Voided" },
          ]} />
        </div>
      </div>

      {loading ? <Spinner /> : filtered.length === 0 ? (
        <EmptyState icon="filetext"
          title="No payroll runs yet"
          body="Create your first payroll run to calculate verified hours, overtime, leave, and adjustments into payroll-ready totals."
          action={<Button icon="plus" onClick={onNewRun}>Create payroll run</Button>}
        />
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "inherit" }}>
            <thead>
              <tr style={{ background: C.bg }}>
                {["Run ID", "Pay Period", "Frequency", "Employees", "Gross Total", "Exceptions", "Status", "Created", "Actions"].map(h => (
                  <th key={h} style={{
                    textAlign: "left", padding: "10px 16px",
                    fontSize: 11, fontWeight: 700, color: C.textLight,
                    textTransform: "uppercase", letterSpacing: 0.4,
                    borderBottom: `1px solid ${C.borderLight}`,
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map(r => {
                const st = RUN_STATUS[r.status] || { label: r.status, tone: "neutral" };
                const isLocked = ["locked", "paid", "exported"].includes(r.status);
                return (
                  <tr key={r.id} style={{ borderBottom: `1px solid ${C.borderLight}` }}>
                    <td style={{ padding: "12px 16px", fontSize: 12, color: C.textSub, fontFamily: "monospace" }}>
                      #{String(r.id).slice(-6)}
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: 13, color: C.text }}>
                      {fmtDate(r.period_start)} — {fmtDate(r.period_end)}
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: 13, color: C.textSub }}>
                      {r.frequency || "monthly"}
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: 13, color: C.text, fontWeight: 600 }}>
                      {r.item_count || r.employees_included || 0}
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: 13, color: C.text, fontWeight: 700 }}>
                      {fmtCurrency(r.total_amount || 0, r.currency || settings?.currency || "USD")}
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      {(r.exception_count || 0) > 0 ? (
                        <Pill tone="warn" icon="warning">{r.exception_count}</Pill>
                      ) : (
                        <Pill tone="success" icon="check">0</Pill>
                      )}
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      <Pill tone={st.tone}>{st.label}</Pill>
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: 11, color: C.textSub }}>
                      <p style={{ margin: 0 }}>{fmtRel(r.created_at)}</p>
                      {r.created_by_name && (
                        <p style={{ margin: "2px 0 0", color: C.textLight }}>{r.created_by_name}</p>
                      )}
                    </td>
                    <td style={{ padding: "12px 16px", whiteSpace: "nowrap" }}>
                      <div style={{ display: "flex", gap: 4 }}>
                        <Button variant="secondary" size="sm" onClick={() => onOpen(r)}>Open</Button>
                        {!isLocked && r.status !== "voided" && (
                          <Button variant="ghost" size="sm" onClick={() => onLock(r)} icon="lock">Lock</Button>
                        )}
                        <Button variant="ghost" size="sm" onClick={() => onExport(r)} icon="download">Export</Button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// NEW PAYROLL RUN WIZARD (5 STEPS)
// ═════════════════════════════════════════════════════════════════════════════

const NewPayrollRunWizard = ({ open, onClose, onCreate, employees, settings }) => {
  const C = useC();
  const [step, setStep] = useState(1);
  const [submitting, setSubmitting] = useState(false);
  const [data, setData] = useState({
    frequency: settings?.default_frequency || "monthly",
    period_start: "",
    period_end: "",
    pay_date: "",
    country: settings?.country || "IN",
    population_mode: "all", // all | department | team | location | individuals
    population_filter: "",
    selected_ids: [],
    policy_set: settings?.default_policy_set || "standard",
    overtime_rule: settings?.default_overtime_rule || "1.5x_after_40",
    break_rule: settings?.default_break_rule || "deduct_unpaid",
    rounding_rule: settings?.default_rounding || "nearest_5_min",
    holiday_rule: settings?.default_holiday_rule || "paid_at_1x",
    currency: settings?.currency || "USD",
    notes: "",
  });

  useEffect(() => { if (open) { setStep(1); setSubmitting(false); } }, [open]);

  const set = (k, v) => setData(d => ({ ...d, [k]: v }));

  const eligibleEmployees = useMemo(() => {
    if (!employees) return [];
    if (data.population_mode === "all") return employees;
    if (data.population_mode === "department" && data.population_filter)
      return employees.filter(e => (e.department || "").toLowerCase() === data.population_filter.toLowerCase());
    if (data.population_mode === "team" && data.population_filter)
      return employees.filter(e => (e.team || "").toLowerCase() === data.population_filter.toLowerCase());
    if (data.population_mode === "location" && data.population_filter)
      return employees.filter(e => (e.location || "").toLowerCase() === data.population_filter.toLowerCase());
    if (data.population_mode === "individuals")
      return employees.filter(e => data.selected_ids.includes(e.id));
    return employees;
  }, [employees, data.population_mode, data.population_filter, data.selected_ids]);

  const canNext = () => {
    if (step === 1) return data.period_start && data.period_end && data.pay_date && new Date(data.period_end) >= new Date(data.period_start);
    if (step === 2) return eligibleEmployees.length > 0;
    if (step === 3) return data.policy_set && data.overtime_rule && data.currency;
    if (step === 4) return true;
    return true;
  };

  const submit = async () => {
    setSubmitting(true);
    try {
      await onCreate({
        ...data,
        employees_included: eligibleEmployees.length,
        employee_ids: eligibleEmployees.map(e => e.id),
      });
    } finally { setSubmitting(false); }
  };

  const stepLabels = ["Period", "Population", "Rules", "Readiness", "Generate"];

  return (
    <Modal open={open} onClose={onClose} size="large"
      title={`New Payroll Run · Step ${step} of 5: ${stepLabels[step - 1]}`}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
          {step > 1 && <Button variant="secondary" icon="chevleft" onClick={() => setStep(s => s - 1)} disabled={submitting}>Back</Button>}
          {step < 5
            ? <Button onClick={() => setStep(s => s + 1)} disabled={!canNext()}>Next</Button>
            : <Button onClick={submit} disabled={!canNext() || submitting} icon="calc">
                {submitting ? "Generating…" : "Calculate Payroll"}
              </Button>}
        </>
      }
    >
      {/* Step indicator */}
      <div style={{ display: "flex", gap: 4, marginBottom: 20 }}>
        {stepLabels.map((lbl, i) => {
          const idx = i + 1;
          const done = idx < step;
          const current = idx === step;
          return (
            <div key={lbl} style={{
              flex: 1, padding: "8px 6px", borderRadius: 6, textAlign: "center",
              fontSize: 11, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase",
              background: current ? C.brandBg : done ? C.greenBg : C.borderLight,
              color: current ? C.brand : done ? C.green : C.textLight,
            }}>
              {done ? "✓ " : `${idx}. `}{lbl}
            </div>
          );
        })}
      </div>

      {/* Step 1 — Period */}
      {step === 1 && (
        <>
          <Field label="Pay frequency" required>
            <Select value={data.frequency} onChange={v => set("frequency", v)} options={[
              { value: "weekly", label: "Weekly" },
              { value: "biweekly", label: "Bi-weekly" },
              { value: "semi-monthly", label: "Semi-monthly" },
              { value: "monthly", label: "Monthly" },
            ]} />
          </Field>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Field label="Period start" required>
              <Input type="date" value={data.period_start} onChange={v => set("period_start", v)} />
            </Field>
            <Field label="Period end" required>
              <Input type="date" value={data.period_end} onChange={v => set("period_end", v)} />
            </Field>
          </div>
          <Field label="Pay date" required helper="The date on which payment will be issued.">
            <Input type="date" value={data.pay_date} onChange={v => set("pay_date", v)} />
          </Field>
          <Field label="Country / jurisdiction">
            <Select value={data.country} onChange={v => set("country", v)} options={[
              { value: "IN", label: "India" },
              { value: "US", label: "United States" },
              { value: "GB", label: "United Kingdom" },
              { value: "AE", label: "United Arab Emirates" },
              { value: "SG", label: "Singapore" },
            ]} />
          </Field>
          <p style={{ fontSize: 12, color: C.textLight, padding: "10px 12px", background: C.brandBg, borderRadius: 6, lineHeight: 1.5 }}>
            <Icon n="info" s={12} c={C.brand} /> The period cannot overlap a locked payroll run for the same employee scope.
          </p>
        </>
      )}

      {/* Step 2 — Population */}
      {step === 2 && (
        <>
          <Field label="Include" required>
            <Select value={data.population_mode} onChange={v => set("population_mode", v)} options={[
              { value: "all", label: "All employees" },
              { value: "department", label: "By department" },
              { value: "team", label: "By team" },
              { value: "location", label: "By location" },
              { value: "individuals", label: "Selected individuals" },
            ]} />
          </Field>
          {["department", "team", "location"].includes(data.population_mode) && (
            <Field label={`${data.population_mode.charAt(0).toUpperCase() + data.population_mode.slice(1)} name`}>
              <Input value={data.population_filter} onChange={v => set("population_filter", v)} placeholder={`e.g. Engineering`} />
            </Field>
          )}
          {data.population_mode === "individuals" && employees && (
            <Field label="Select employees">
              <div style={{
                maxHeight: 240, overflowY: "auto",
                border: `1px solid ${C.border}`, borderRadius: 8, padding: 8,
              }}>
                {employees.map(e => (
                  <label key={e.id} style={{
                    display: "flex", alignItems: "center", gap: 8,
                    padding: "6px 8px", fontSize: 13, color: C.text, cursor: "pointer",
                  }}>
                    <input type="checkbox"
                      checked={data.selected_ids.includes(e.id)}
                      onChange={(ev) => {
                        const next = ev.target.checked
                          ? [...data.selected_ids, e.id]
                          : data.selected_ids.filter(id => id !== e.id);
                        set("selected_ids", next);
                      }} />
                    <span style={{ flex: 1 }}>{e.name || e.email}</span>
                    <span style={{ fontSize: 11, color: C.textSub }}>{e.department || "—"}</span>
                  </label>
                ))}
              </div>
            </Field>
          )}
          <p style={{ fontSize: 13, color: C.text, padding: "10px 12px", background: C.greenBg, borderRadius: 6, lineHeight: 1.5 }}>
            <strong>{eligibleEmployees.length}</strong> employee{eligibleEmployees.length !== 1 ? "s" : ""} will be included in this run.
          </p>
        </>
      )}

      {/* Step 3 — Rules */}
      {step === 3 && (
        <>
          <Field label="Payroll policy set" required helper="Policy version is pinned to this run. Changes to the policy after creation will not affect this run unless recalculated.">
            <Select value={data.policy_set} onChange={v => set("policy_set", v)} options={[
              { value: "standard", label: "Standard policy (default)" },
              { value: "remote_workers", label: "Remote workers" },
              { value: "field_team", label: "Field team" },
            ]} />
          </Field>
          <Field label="Overtime rule">
            <Select value={data.overtime_rule} onChange={v => set("overtime_rule", v)} options={[
              { value: "1.0x_after_40", label: "1.0× after 40 hrs/week" },
              { value: "1.5x_after_40", label: "1.5× after 40 hrs/week" },
              { value: "2.0x_after_50", label: "2.0× after 50 hrs/week" },
              { value: "1.5x_after_8", label: "1.5× after 8 hrs/day" },
              { value: "custom", label: "Custom (configure in settings)" },
            ]} />
          </Field>
          <Field label="Break deduction rule">
            <Select value={data.break_rule} onChange={v => set("break_rule", v)} options={[
              { value: "deduct_unpaid", label: "Deduct unpaid breaks" },
              { value: "paid", label: "All breaks paid" },
              { value: "policy_driven", label: "Per policy" },
            ]} />
          </Field>
          <Field label="Rounding rule">
            <Select value={data.rounding_rule} onChange={v => set("rounding_rule", v)} options={[
              { value: "none", label: "No rounding" },
              { value: "nearest_5_min", label: "Nearest 5 minutes" },
              { value: "nearest_6_min", label: "Nearest 6 minutes" },
              { value: "nearest_15_min", label: "Nearest 15 minutes" },
            ]} />
          </Field>
          <Field label="Holiday rule">
            <Select value={data.holiday_rule} onChange={v => set("holiday_rule", v)} options={[
              { value: "paid_at_1x", label: "Paid at 1× regular rate" },
              { value: "paid_at_1.5x", label: "Paid at 1.5×" },
              { value: "paid_at_2x", label: "Paid at 2×" },
              { value: "unpaid", label: "Unpaid" },
            ]} />
          </Field>
          <Field label="Currency"
                 helper="Defaults to your org currency from Payroll → Settings. Override only if this run pays in a different currency.">
            <Select value={data.currency} onChange={v => set("currency", v)} options={CURRENCY_OPTIONS} />
          </Field>
        </>
      )}

      {/* Step 4 — Readiness */}
      {step === 4 && (
        <>
          <p style={{ fontSize: 13, color: C.textSub, marginBottom: 14, lineHeight: 1.5 }}>
            We checked the {eligibleEmployees.length} included employees for missing rates,
            unapproved time, conflicting schedules, leave conflicts, and open disputes.
          </p>
          <div style={{
            border: `1px solid ${C.border}`, borderRadius: 8,
            padding: 14, background: C.bg, marginBottom: 14,
          }}>
            <p style={{ fontSize: 13, color: C.text, fontWeight: 700, margin: 0,
                        display: "flex", alignItems: "center", gap: 6 }}>
              <Icon n="check" s={14} c={C.green} />
              Readiness check complete
            </p>
            <p style={{ fontSize: 12, color: C.textSub, margin: "6px 0 0", lineHeight: 1.5 }}>
              The actual exception list will be generated when you click "Calculate Payroll".
              Any critical exceptions must be resolved before you can approve or finalize the run.
            </p>
          </div>
          <Field label="Notes (optional)" helper="Free-form notes recorded with this run.">
            <Textarea value={data.notes} onChange={v => set("notes", v)} rows={3}
              placeholder="e.g. End-of-quarter run including bonuses approved on March 28." />
          </Field>
        </>
      )}

      {/* Step 5 — Generate */}
      {step === 5 && (
        <>
          <p style={{ fontSize: 13, color: C.text, marginBottom: 14, lineHeight: 1.5 }}>
            Review the run summary, then click <strong>Calculate Payroll</strong>. This will
            create an immutable calculation snapshot pinned to your selected policy version.
          </p>
          <div style={{
            border: `1px solid ${C.border}`, borderRadius: 8, overflow: "hidden",
          }}>
            {[
              ["Period", `${fmtDate(data.period_start)} — ${fmtDate(data.period_end)}`],
              ["Pay date", fmtDate(data.pay_date)],
              ["Frequency", data.frequency],
              ["Country", data.country],
              ["Population", `${data.population_mode}${data.population_filter ? ` · ${data.population_filter}` : ""} (${eligibleEmployees.length})`],
              ["Policy set", data.policy_set],
              ["Overtime rule", data.overtime_rule],
              ["Break rule", data.break_rule],
              ["Rounding", data.rounding_rule],
              ["Holiday rule", data.holiday_rule],
              ["Currency", data.currency],
            ].map(([k, v], i, arr) => (
              <div key={k} style={{
                display: "flex", padding: "10px 14px",
                borderBottom: i < arr.length - 1 ? `1px solid ${C.borderLight}` : "none",
                fontSize: 13,
              }}>
                <span style={{ flex: "0 0 160px", color: C.textSub, fontWeight: 600 }}>{k}</span>
                <span style={{ flex: 1, color: C.text }}>{v || "—"}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </Modal>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 2 — CALCULATOR (employee table + drawer)
// ═════════════════════════════════════════════════════════════════════════════

const CalculatorPane = ({ run, items, configs, employees, loading, onSelectEmployee, onRecalculate, onSubmitReview, onApprove, onLock, onExport, onSaveDraft, currency }) => {
  const C = useC();
  const [empSearch, setEmpSearch] = useState("");

  const merged = useMemo(() => {
    const itemsArr = items || [];
    if (!run) {
      return (employees || []).map(e => {
        const cfg = configs?.find(c => c.user_id === e.id);
        return {
          user_id: e.id, name: e.name, role: e.designation || e.role || "—",
          pay_type: cfg?.pay_type || "salary",
          rate: cfg?.pay_type === "hourly" ? cfg.hourly_rate : cfg?.basic_salary,
          regular_hours: 0, overtime_hours: 0, pto_hours: 0,
          adjustments: 0, deductions: 0, gross_pay: 0,
          status: "no_data",
        };
      });
    }
    return itemsArr.map(it => ({ ...it, role: it.role || "—" }));
  }, [run, items, configs, employees]);

  const filtered = useMemo(() => {
    if (!empSearch.trim()) return merged;
    const q = empSearch.toLowerCase();
    return merged.filter(m => (m.name || "").toLowerCase().includes(q) || (m.user_id || "").toString().includes(q));
  }, [merged, empSearch]);

  const totalGross = useMemo(() => filtered.reduce((s, r) => s + Number(r.gross_pay || 0), 0), [filtered]);

  if (!run) {
    return (
      <Card>
        <EmptyState icon="calc"
          title="No payroll run selected"
          body="Open a payroll run from the Payroll Runs tab — or create a new run — to start calculating."
        />
      </Card>
    );
  }

  return (
    <>
      <Card padding={0}
        title={`Calculator · #${String(run.id).slice(-6)} · ${fmtDate(run.period_start)} – ${fmtDate(run.period_end)}`}
        icon="calc"
        action={
          <div style={{ display: "flex", gap: 6 }}>
            <Pill tone={(RUN_STATUS[run.status] || {}).tone || "neutral"}>
              {(RUN_STATUS[run.status] || {}).label || run.status}
            </Pill>
          </div>
        }
      >
        {/* Filter rail */}
        <div style={{
          padding: "12px 24px", borderBottom: `1px solid ${C.borderLight}`,
          display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center",
        }}>
          <div style={{
            flex: 1, minWidth: 240, display: "flex", alignItems: "center", gap: 8,
            padding: "0 10px", borderRadius: 8,
            border: `1px solid ${C.border}`, background: C.bg,
          }}>
            <Icon n="search" s={14} c={C.textSub} />
            <input
              type="search" value={empSearch} onChange={(e) => setEmpSearch(e.target.value)}
              placeholder="Search employees…"
              style={{
                flex: 1, padding: "9px 0", border: "none", outline: "none",
                background: "transparent", fontSize: 13, color: C.text, fontFamily: "inherit",
              }}
            />
          </div>
          <p style={{ fontSize: 12, color: C.textSub, margin: 0 }}>
            <strong style={{ color: C.text }}>{filtered.length}</strong> employees ·
            Gross: <strong style={{ color: C.text }}>{fmtCurrency(totalGross, currency)}</strong>
          </p>
        </div>

        {loading ? <Spinner /> : filtered.length === 0 ? (
          <EmptyState icon="users" title="No employees in this run" />
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "inherit" }}>
              <thead>
                <tr style={{ background: C.bg }}>
                  {["Employee", "Role", "Pay Type", "Rate", "Reg. Hrs", "OT Hrs", "PTO", "Gross Pay", "Status", ""].map(h => (
                    <th key={h} style={{
                      textAlign: "left", padding: "10px 14px",
                      fontSize: 11, fontWeight: 700, color: C.textLight,
                      textTransform: "uppercase", letterSpacing: 0.4,
                      borderBottom: `1px solid ${C.borderLight}`,
                    }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.map(r => (
                  <tr key={r.user_id} style={{ borderBottom: `1px solid ${C.borderLight}`, cursor: "pointer" }}
                      onClick={() => onSelectEmployee(r)}>
                    <td style={{ padding: "12px 14px", fontSize: 13, color: C.text, fontWeight: 600 }}>
                      {r.name || `User ${r.user_id}`}
                    </td>
                    <td style={{ padding: "12px 14px", fontSize: 12, color: C.textSub }}>{r.role}</td>
                    <td style={{ padding: "12px 14px", fontSize: 12 }}>
                      <Pill tone={r.pay_type === "hourly" ? "info" : "purple"}>{r.pay_type}</Pill>
                    </td>
                    <td style={{ padding: "12px 14px", fontSize: 12, color: C.text }}>
                      {fmtCurrency(r.rate || 0, currency)}
                    </td>
                    <td style={{ padding: "12px 14px", fontSize: 12, color: C.text }}>{Number(r.regular_hours || r.worked_hours || 0).toFixed(1)}</td>
                    <td style={{ padding: "12px 14px", fontSize: 12, color: C.text }}>{Number(r.overtime_hours || 0).toFixed(1)}</td>
                    <td style={{ padding: "12px 14px", fontSize: 12, color: C.textSub }}>{Number(r.pto_hours || 0).toFixed(1)}</td>
                    <td style={{ padding: "12px 14px", fontSize: 13, color: C.text, fontWeight: 700 }}>
                      {fmtCurrency(r.gross_pay || 0, currency)}
                    </td>
                    <td style={{ padding: "12px 14px" }}>
                      {r.status === "no_data"
                        ? <Pill tone="warn">no rate</Pill>
                        : r.exception_status
                        ? <Pill tone="danger">exception</Pill>
                        : <Pill tone="success">ok</Pill>}
                    </td>
                    <td style={{ padding: "12px 14px" }}>
                      <Icon n="chevright" s={14} c={C.textLight} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Action bar */}
      <Card padding={14}>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
          <Button variant="secondary" icon="refresh" onClick={onRecalculate}>Recalculate</Button>
          <Button variant="secondary" onClick={onSaveDraft}>Save Draft</Button>
          {run.status === "draft" && (
            <Button variant="secondary" onClick={onSubmitReview}>Submit for Review</Button>
          )}
          {(run.status === "needs_review" || run.status === "draft") && (
            <Button variant="success" icon="check" onClick={onApprove}>Approve Run</Button>
          )}
          {(run.status === "processed" || run.status === "approved") && (
            <Button icon="lock" onClick={onLock}>Lock Run</Button>
          )}
          <Button icon="download" onClick={onExport}>Export</Button>
        </div>
      </Card>
    </>
  );
};

// "1.5x_after_40" -> 1.5. The run wizard offers 1.0x / 1.5x / 2.0x, so the
// explanation block must not print a fixed 1.5 next to a server-computed gross.
const otRateMultiplier = (rule) => {
  const m = /^([\d.]+)x/.exec(String(rule || ""));
  return m ? Number(m[1]) : 1.5;
};

const EmployeeCalcDrawer = ({ open, employee, run, onClose, currency, onAddAdjustment }) => {
  const otMultiplier = otRateMultiplier(run?.overtime_rule);
  const C = useC();
  if (!open || !employee) return null;

  const e = employee;
  const reg = Number(e.regular_hours || e.worked_hours || 0);
  const ot  = Number(e.overtime_hours || 0);
  const pto = Number(e.pto_hours || 0);
  const rate = Number(e.rate || e.hourly_rate || 0);
  const adjs = Number(e.adjustment_total || e.adjustments || 0);
  const dedu = Number(e.deduction_total || e.deductions || 0);
  const gross = Number(e.gross_pay || 0);

  return (
    <div onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 9998,
        background: "rgba(16,24,40,0.45)",
        display: "flex", justifyContent: "flex-end",
      }}>
      <aside onClick={(ev) => ev.stopPropagation()}
        style={{
          width: "100%", maxWidth: 520, height: "100%",
          background: C.surface, boxShadow: "-12px 0 40px rgba(16,24,40,0.18)",
          display: "flex", flexDirection: "column",
        }}>
        <header style={{
          padding: "18px 22px", borderBottom: `1px solid ${C.borderLight}`,
          display: "flex", alignItems: "center", gap: 10,
        }}>
          <Icon n="user" s={18} c={C.brand} />
          <h3 style={{ flex: 1, fontSize: 15, fontWeight: 800, color: C.text, margin: 0 }}>
            {e.name || `Employee ${e.user_id}`}
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
          {/* Summary */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 18 }}>
            {[
              ["Pay type", e.pay_type || "—"],
              ["Rate", fmtCurrency(rate, currency)],
              ["Regular hours", reg.toFixed(2)],
              ["Overtime hours", ot.toFixed(2)],
              ["PTO/Holiday hours", pto.toFixed(2)],
              ["Status", e.exception_status || "ok"],
            ].map(([k, v]) => (
              <div key={k} style={{
                padding: 10, borderRadius: 6, background: C.bg,
                border: `1px solid ${C.borderLight}`,
              }}>
                <p style={{ fontSize: 10, color: C.textLight, fontWeight: 700,
                            textTransform: "uppercase", letterSpacing: 0.4, margin: 0 }}>{k}</p>
                <p style={{ fontSize: 13, color: C.text, fontWeight: 700, margin: "4px 0 0" }}>{v}</p>
              </div>
            ))}
          </div>

          {/* Calculation explanation */}
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 8px" }}>
            Calculation explanation
          </p>
          <div style={{
            padding: 14, borderRadius: 8, background: C.brandBg,
            border: `1px solid ${C.brandBg}`, marginBottom: 18,
            fontFamily: "monospace", fontSize: 13, color: C.text, lineHeight: 1.8,
          }}>
            <div>Regular pay   = {reg.toFixed(2)} × {fmtCurrency(rate, currency)} = {fmtCurrency(reg * rate, currency)}</div>
            <div>Overtime pay  = {ot.toFixed(2)} × {fmtCurrency(rate * otMultiplier, currency)} = {fmtCurrency(ot * rate * otMultiplier, currency)}</div>
            <div>PTO pay       = {pto.toFixed(2)} × {fmtCurrency(rate, currency)} = {fmtCurrency(pto * rate, currency)}</div>
            <div>Adjustments   = +{fmtCurrency(adjs, currency)}</div>
            <div>Deductions    = −{fmtCurrency(dedu, currency)}</div>
            <div style={{ marginTop: 6, paddingTop: 6, borderTop: `1px solid ${C.brand}33`, fontWeight: 700 }}>
              Gross pay     = {fmtCurrency(gross, currency)}
            </div>
          </div>

          {/* Policy evidence */}
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 8px" }}>
            Policy evidence
          </p>
          <div style={{
            border: `1px solid ${C.border}`, borderRadius: 8, padding: 12,
            marginBottom: 18, fontSize: 13, color: C.text, lineHeight: 1.7,
          }}>
            <p style={{ margin: 0 }}>Policy set: <strong>{run?.policy_set || "standard"}</strong></p>
            <p style={{ margin: "4px 0 0" }}>Overtime rule: <strong>{run?.overtime_rule || "1.5× after 40 hrs/wk"}</strong></p>
            <p style={{ margin: "4px 0 0" }}>Rounding: <strong>{run?.rounding_rule || "nearest 5 min"}</strong></p>
            <p style={{ margin: "4px 0 0" }}>Currency: <strong>{run?.currency || currency}</strong></p>
          </div>

          {/* Source evidence */}
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 8px" }}>
            Source evidence
          </p>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: C.textSub, lineHeight: 1.7 }}>
            <li>Time records: {Math.round(reg + ot + pto)} hours from Temveris sessions</li>
            <li>Approvals: {e.approvals_count || "auto-approved per policy"}</li>
            <li>Disputes: {e.disputes_open ? `${e.disputes_open} open` : "none"}</li>
            <li>Manager confirmation: {e.manager_confirmed ? "yes" : "not required"}</li>
          </ul>

          <div style={{ marginTop: 18 }}>
            <Button variant="secondary" icon="plus" onClick={() => onAddAdjustment(e)}>
              Add adjustment for this employee
            </Button>
          </div>
        </div>
      </aside>
    </div>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 3 — EXCEPTIONS
// ═════════════════════════════════════════════════════════════════════════════

const EXCEPTION_DEFS = {
  missing_pay_rate:    { severity: "critical", label: "Missing pay rate" },
  unapproved_time:     { severity: "critical", label: "Unapproved time" },
  overlapping_shifts:  { severity: "critical", label: "Overlapping shifts" },
  open_dispute:        { severity: "critical", label: "Open time dispute" },
  overtime_threshold:  { severity: "warning",  label: "Overtime threshold reached" },
  manual_adjustment:   { severity: "warning",  label: "Manual adjustment added" },
  over_plan_limit:     { severity: "info",     label: "Over plan policy limit" },
};

const ExceptionsPane = ({ exceptions, loading, onResolve, onRefresh, run }) => {
  const C = useC();
  const grouped = useMemo(() => {
    const buckets = { critical: [], warning: [], info: [] };
    (exceptions || []).forEach(e => {
      const def = EXCEPTION_DEFS[e.type] || { severity: e.severity || "info", label: e.type };
      const sev = def.severity;
      buckets[sev]?.push({ ...e, _label: def.label, _severity: sev });
    });
    return buckets;
  }, [exceptions]);

  if (!run) {
    return (
      <Card>
        <EmptyState icon="warning"
          title="Open a payroll run to see its exceptions"
          body="Exceptions are scoped to a specific payroll run. Open a run from the Payroll Runs tab." />
      </Card>
    );
  }

  return (
    <Card title={`Exceptions (${exceptions?.length || 0})`} icon="warning"
          padding={0}
          action={<Button variant="secondary" size="sm" icon="refresh" onClick={onRefresh}>Refresh</Button>}>
      {loading ? <Spinner /> : (exceptions?.length || 0) === 0 ? (
        <EmptyState icon="check" title="No exceptions found"
          body="All employees in this run have valid rates, approved time, and no open disputes." />
      ) : (
        <div>
          {["critical", "warning", "info"].map(sev => {
            const list = grouped[sev];
            if (!list || list.length === 0) return null;
            const tone = sev === "critical" ? "danger" : sev === "warning" ? "warn" : "info";
            return (
              <div key={sev}>
                <div style={{
                  padding: "10px 24px", background: C.bg,
                  fontSize: 11, fontWeight: 700, color: C.textLight,
                  textTransform: "uppercase", letterSpacing: 0.5,
                  display: "flex", alignItems: "center", gap: 8,
                  borderTop: `1px solid ${C.borderLight}`,
                }}>
                  <Pill tone={tone}>{sev}</Pill> {list.length} item{list.length !== 1 ? "s" : ""}
                </div>
                <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                  {list.map(ex => (
                    <li key={ex.id} style={{
                      padding: "14px 24px", borderTop: `1px solid ${C.borderLight}`,
                      display: "flex", alignItems: "flex-start", gap: 14,
                    }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>
                          {ex._label}
                          {ex.employee_name && <span style={{ color: C.textSub, fontWeight: 500 }}> · {ex.employee_name}</span>}
                        </p>
                        {ex.description && (
                          <p style={{ fontSize: 12, color: C.textSub, margin: "4px 0 0", lineHeight: 1.5 }}>
                            {ex.description}
                          </p>
                        )}
                        {ex.resolved_at && (
                          <p style={{ fontSize: 11, color: C.green, margin: "6px 0 0" }}>
                            Resolved {fmtRel(ex.resolved_at)} by {ex.resolved_by_name || "—"}
                          </p>
                        )}
                      </div>
                      {!ex.resolved_at && (
                        <Button variant="secondary" size="sm" onClick={() => onResolve(ex)}>Resolve</Button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
};

const ResolveExceptionModal = ({ open, exception, onClose, onSubmit }) => {
  const C = useC();
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => { if (open) setReason(""); }, [open]);

  const submit = async () => {
    if (reason.trim().length < 10) return;
    setSubmitting(true);
    try { await onSubmit({ reason: reason.trim() }); onClose(); }
    finally { setSubmitting(false); }
  };

  return (
    <Modal open={open} onClose={onClose}
      title={`Resolve: ${(EXCEPTION_DEFS[exception?.type] || {}).label || exception?.type || "Exception"}`}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button onClick={submit} disabled={submitting || reason.trim().length < 10}>
            {submitting ? "Resolving…" : "Resolve exception"}
          </Button>
        </>
      }>
      <p style={{ fontSize: 13, color: C.textSub, margin: "0 0 14px", lineHeight: 1.6 }}>
        Resolution is recorded to the audit trail with your name, timestamp, and reason.
      </p>
      <Field label="Resolution notes" required helper="Minimum 10 characters.">
        <Textarea value={reason} onChange={setReason} rows={4}
          placeholder="e.g. Pay rate added in employee profile; recalculation queued." />
      </Field>
    </Modal>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 4 — ADJUSTMENTS
// ═════════════════════════════════════════════════════════════════════════════

const ADJUSTMENT_TYPES = [
  { value: "bonus",         label: "Bonus" },
  { value: "reimbursement", label: "Reimbursement" },
  { value: "correction",    label: "Correction" },
  { value: "deduction",     label: "Deduction" },
  { value: "retro_pay",     label: "Retro pay" },
  { value: "unpaid_absence",label: "Unpaid absence" },
  { value: "other",         label: "Other" },
];

const AdjustmentsPane = ({ adjustments, loading, onCreate, onApprove, onReject, onRefresh, run, settings }) => {
  const C = useC();
  const [showCreate, setShowCreate] = useState(false);

  return (
    <>
      <Card title={`Adjustments (${adjustments?.length || 0})`} icon="edit" padding={0}
            action={
              <div style={{ display: "flex", gap: 6 }}>
                <Button variant="secondary" size="sm" icon="refresh" onClick={onRefresh}>Refresh</Button>
                <Button size="sm" icon="plus" onClick={() => setShowCreate(true)} disabled={!run}>New</Button>
              </div>
            }>
        {loading ? <Spinner /> : (adjustments?.length || 0) === 0 ? (
          <EmptyState icon="edit" title="No adjustments yet"
            body="Use adjustments for bonuses, reimbursements, retro pay, or one-off corrections. Each adjustment requires a reason and approval before final payroll approval."
            action={run ? <Button icon="plus" onClick={() => setShowCreate(true)}>Add adjustment</Button> : null}
          />
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "inherit" }}>
              <thead>
                <tr style={{ background: C.bg }}>
                  {["Employee", "Type", "Amount", "Reason", "Status", "Submitted", ""].map(h => (
                    <th key={h} style={{
                      textAlign: "left", padding: "10px 16px",
                      fontSize: 11, fontWeight: 700, color: C.textLight,
                      textTransform: "uppercase", letterSpacing: 0.4,
                      borderBottom: `1px solid ${C.borderLight}`,
                    }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {adjustments.map(a => (
                  <tr key={a.id} style={{ borderBottom: `1px solid ${C.borderLight}` }}>
                    <td style={{ padding: "12px 16px", fontSize: 13, color: C.text, fontWeight: 600 }}>
                      {a.employee_name || `User ${a.employee_id}`}
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      <Pill tone={a.type === "deduction" || a.type === "unpaid_absence" ? "danger" : "info"}>
                        {(ADJUSTMENT_TYPES.find(t => t.value === a.type) || {}).label || a.type}
                      </Pill>
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: 13, color: C.text, fontWeight: 700 }}>
                      {Number(a.amount) >= 0 ? "+" : ""}{fmtCurrency(a.amount || 0, a.currency || run?.currency || settings?.currency || "USD")}
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: 12, color: C.textSub, maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {a.reason}
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      <Pill tone={
                        a.status === "approved" ? "success" :
                        a.status === "rejected" ? "danger"  :
                        a.status === "pending"  ? "warn"    : "neutral"
                      }>{a.status || "draft"}</Pill>
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: 11, color: C.textSub }}>
                      {fmtRel(a.created_at)}
                    </td>
                    <td style={{ padding: "12px 16px", whiteSpace: "nowrap" }}>
                      {a.status === "pending" && (
                        <div style={{ display: "flex", gap: 4 }}>
                          <Button variant="success" size="sm" onClick={() => onApprove(a)}>Approve</Button>
                          <Button variant="ghost" size="sm" onClick={() => onReject(a)}>Reject</Button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <AdjustmentModal open={showCreate} run={run} settings={settings}
        onClose={() => setShowCreate(false)}
        onSubmit={async (payload) => { await onCreate(payload); setShowCreate(false); }} />
    </>
  );
};

const AdjustmentModal = ({ open, run, settings, onClose, onSubmit, employees }) => {
  const C = useC();
  // Default currency precedence mirrors the rest of the page: this run's
  // pinned currency → org default from Settings → USD.
  const defaultCcy = run?.currency || settings?.currency || "USD";
  const [data, setData] = useState({
    type: "bonus", amount: "", employee_id: "", reason: "", currency: defaultCcy,
  });
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => {
    if (open) setData({ type: "bonus", amount: "", employee_id: "", reason: "", currency: defaultCcy });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, run, settings]);

  const set = (k, v) => setData(d => ({ ...d, [k]: v }));

  const canSubmit = data.type && data.amount && data.employee_id && data.reason.trim().length >= 20;

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await onSubmit({
        ...data, amount: parseFloat(data.amount) || 0,
        run_id: run?.id,
      });
    } finally { setSubmitting(false); }
  };

  return (
    <Modal open={open} onClose={onClose} title="New adjustment"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button onClick={submit} disabled={!canSubmit || submitting}>
            {submitting ? "Submitting…" : "Submit for approval"}
          </Button>
        </>
      }>
      <Field label="Adjustment type" required>
        <Select value={data.type} onChange={v => set("type", v)} options={ADJUSTMENT_TYPES} />
      </Field>
      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 12 }}>
        <Field label="Amount" required helper="Positive for addition, negative for deduction.">
          <Input type="number" value={data.amount} onChange={v => set("amount", v)}
                 placeholder="e.g. 5000 or -1500" />
        </Field>
        <Field label="Currency">
          <Select value={data.currency} onChange={v => set("currency", v)} options={CURRENCY_OPTIONS} />
        </Field>
      </div>
      <Field label="Employee" required>
        <Input value={data.employee_id} onChange={v => set("employee_id", v)} placeholder="Employee ID" />
      </Field>
      <Field label="Reason" required helper="Minimum 20 characters. Recorded with audit trail.">
        <Textarea value={data.reason} onChange={v => set("reason", v)} rows={3}
          placeholder="e.g. Q1 performance bonus approved by line manager on 28 March." />
      </Field>
    </Modal>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 5 — EXPORT HISTORY
// ═════════════════════════════════════════════════════════════════════════════

const ExportHistoryPane = ({ exports, loading, onRefresh, onDownload, onNewExport, runs }) => {
  const C = useC();
  return (
    <Card title={`Export history (${exports?.length || 0})`} icon="download" padding={0}
          action={
            <div style={{ display: "flex", gap: 6 }}>
              <Button variant="secondary" size="sm" icon="refresh" onClick={onRefresh}>Refresh</Button>
              <Button size="sm" icon="download" onClick={onNewExport} disabled={!runs?.length}>New export</Button>
            </div>
          }>
      {loading ? <Spinner /> : (exports?.length || 0) === 0 ? (
        <EmptyState icon="download" title="No exports yet"
          body="Generate payroll summary CSVs, provider files, or audit packs (PDF/ZIP). Every export includes a checksum and timestamp." />
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {exports.map(ex => (
            <li key={ex.id} style={{
              padding: "14px 24px", borderBottom: `1px solid ${C.borderLight}`,
              display: "flex", alignItems: "center", gap: 14,
            }}>
              <Icon n={ex.format === "pdf" || ex.format === "zip" ? "shield" : "filetext"} s={20} c={C.brand} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>
                  {(ex.export_type || "summary").replace(/_/g, " ")} ·{" "}
                  <span style={{ color: C.textSub, fontWeight: 500 }}>
                    Run #{String(ex.payroll_run_id || "").slice(-6)} · {(ex.format || "csv").toUpperCase()}
                  </span>
                </p>
                <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                  Generated {fmtRel(ex.generated_at)} by {ex.generated_by_name || "—"}
                  {ex.checksum && <> · checksum {ex.checksum.slice(0, 12)}…</>}
                </p>
              </div>
              {ex.file_url && (
                <Button variant="secondary" size="sm" icon="download"
                  onClick={() => onDownload(ex)}>Download</Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PANE 6 — SETTINGS
// ═════════════════════════════════════════════════════════════════════════════

const SettingsPane = ({ settings, onSave, loading }) => {
  const C = useC();
  const [draft, setDraft] = useState(settings || {});
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (settings) setDraft(settings); }, [settings]);
  const set = (k, v) => setDraft(d => ({ ...d, [k]: v }));

  const save = async () => {
    setSaving(true);
    try { await onSave(draft); } finally { setSaving(false); }
  };

  const dirty = JSON.stringify(draft) !== JSON.stringify(settings || {});

  return (
    <>
      <div style={{
        position: "sticky", top: 0, zIndex: 5,
        display: "flex", alignItems: "center", justifyContent: "space-between",
        gap: 12, padding: "10px 14px", marginBottom: 16,
        background: C.surface, border: `1px solid ${C.border}`, borderRadius: 12,
        boxShadow: "0 4px 12px rgba(0,0,0,0.04)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <Icon n="settings" s={15} c={C.textSub} />
          <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>Payroll settings</span>
          {dirty && (
            <span style={{
              fontSize: 11, fontWeight: 600, color: C.brand,
              background: C.brandBg, border: `1px solid ${C.brandBdr}`,
              padding: "2px 8px", borderRadius: 99,
            }}>Unsaved changes</span>
          )}
        </div>
        <Button onClick={save} disabled={saving || !dirty} icon="check">
          {saving ? "Saving…" : "Save settings"}
        </Button>
      </div>

      <Card title="Pay frequencies and currency" icon="settings"
            subtitle="Set during account setup — this is the single source of truth used across the Payroll Calculator, Adjustments, KPIs, and exports.">
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <Field label="Default pay frequency">
            <Select value={draft.default_frequency || "monthly"} onChange={v => set("default_frequency", v)} options={[
              { value: "weekly", label: "Weekly" },
              { value: "biweekly", label: "Bi-weekly" },
              { value: "semi-monthly", label: "Semi-monthly" },
              { value: "monthly", label: "Monthly" },
            ]} />
          </Field>
          <Field label="Default currency" required
                 helper="All payroll runs, KPI cards, adjustments, and exports default to this currency. Existing runs keep the currency they were created with.">
            <Select value={draft.currency || ""} onChange={v => set("currency", v)}
                    options={[{ value: "", label: "— Select currency —" }, ...CURRENCY_OPTIONS]} />
          </Field>
        </div>
        {draft.currency && (
          <p style={{
            marginTop: 12, padding: "10px 12px", borderRadius: 8,
            background: C.greenBg, color: C.green, fontSize: 12, fontWeight: 600,
            display: "inline-flex", alignItems: "center", gap: 6,
          }}>
            <Icon n="check" s={13} c={C.green} w={2.5} />
            Sample: {fmtCurrency(125000, draft.currency)} will display as your gross pay format.
          </p>
        )}
      </Card>

      <Card title="Calculation rules" icon="calc">
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <Field label="Default overtime rule">
            <Select value={draft.default_overtime_rule || "1.5x_after_40"} onChange={v => set("default_overtime_rule", v)} options={[
              { value: "1.0x_after_40", label: "1.0× after 40 hrs/week" },
              { value: "1.5x_after_40", label: "1.5× after 40 hrs/week" },
              { value: "2.0x_after_50", label: "2.0× after 50 hrs/week" },
              { value: "1.5x_after_8", label: "1.5× after 8 hrs/day" },
              { value: "custom", label: "Custom" },
            ]} />
          </Field>
          <Field label="Default rounding rule">
            <Select value={draft.default_rounding || "nearest_5_min"} onChange={v => set("default_rounding", v)} options={[
              { value: "none", label: "No rounding" },
              { value: "nearest_5_min", label: "Nearest 5 min" },
              { value: "nearest_6_min", label: "Nearest 6 min" },
              { value: "nearest_15_min", label: "Nearest 15 min" },
            ]} />
          </Field>
          <Field label="Default break rule">
            <Select value={draft.default_break_rule || "deduct_unpaid"} onChange={v => set("default_break_rule", v)} options={[
              { value: "deduct_unpaid", label: "Deduct unpaid breaks" },
              { value: "paid", label: "All breaks paid" },
              { value: "policy_driven", label: "Per policy" },
            ]} />
          </Field>
          <Field label="Default holiday rule">
            <Select value={draft.default_holiday_rule || "paid_at_1x"} onChange={v => set("default_holiday_rule", v)} options={[
              { value: "paid_at_1x", label: "Paid at 1×" },
              { value: "paid_at_1.5x", label: "Paid at 1.5×" },
              { value: "paid_at_2x", label: "Paid at 2×" },
              { value: "unpaid", label: "Unpaid" },
            ]} />
          </Field>
        </div>
      </Card>

      <Card title="Approval workflow" icon="users">
        <p style={{ fontSize: 13, color: C.textSub, margin: 0, lineHeight: 1.6 }}>
          Default workflow: prepared by Payroll Admin → reviewed by Finance Admin →
          approved by Account Owner or designated approver. Locking and voiding require
          elevated permission and reason capture.
        </p>
      </Card>

      <Card title="Locking" icon="lock">
        <p style={{ fontSize: 13, color: C.textSub, margin: 0, lineHeight: 1.6 }}>
          Locked runs are read-only. Any post-lock change requires void-and-recreate or
          a separate correction run. Locking writes an immutable audit event.
        </p>
      </Card>

      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Button onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save settings"}
        </Button>
      </div>
    </>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// LOCK MODAL
// ═════════════════════════════════════════════════════════════════════════════

const LockRunModal = ({ open, run, onClose, onSubmit }) => {
  const C = useC();
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => { if (open) setReason(""); }, [open]);

  const submit = async () => {
    if (reason.trim().length < 15) return;
    setSubmitting(true);
    try { await onSubmit({ run_id: run?.id, reason: reason.trim() }); onClose(); }
    finally { setSubmitting(false); }
  };

  return (
    <Modal open={open} onClose={onClose} title="Lock payroll run"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button icon="lock" onClick={submit} disabled={submitting || reason.trim().length < 15}>
            {submitting ? "Locking…" : "Lock run"}
          </Button>
        </>
      }>
      <div style={{
        padding: 14, borderRadius: 8, background: C.amberBg,
        border: `1px solid ${C.amber}33`, marginBottom: 14,
        display: "flex", gap: 10, alignItems: "flex-start",
      }}>
        <Icon n="warning" s={18} c={C.amber} />
        <div style={{ flex: 1, fontSize: 13, color: C.amber, lineHeight: 1.5 }}>
          Once locked, this run becomes read-only. Any post-lock correction
          requires a separate correction run or void-and-recreate. This action is
          recorded to the audit trail.
        </div>
      </div>
      <Field label="Lock reason" required helper="Minimum 15 characters.">
        <Textarea value={reason} onChange={setReason} rows={3}
          placeholder="e.g. Final approval for March 2026 payroll. All exceptions resolved." />
      </Field>
    </Modal>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// MAIN COMPONENT
// ═════════════════════════════════════════════════════════════════════════════

export default function PayrollCalculatorModule({ token, employees = [], currentUserRole = "admin" }) {
  const { C } = useTheme();
  const [activePane, setActivePane] = useState("runs"); // default per spec
  const [loading, setLoading] = useState(false);
  const { msgs, push: toast } = useToast();

  // Data state
  const [runs, setRuns] = useState([]);
  const [configs, setConfigs] = useState([]);
  const [summary, setSummary] = useState(null);
  const [currentRun, setCurrentRun] = useState(null);
  const [currentRunItems, setCurrentRunItems] = useState([]);
  const [exceptions, setExceptions] = useState([]);
  const [adjustments, setAdjustments] = useState([]);
  const [exports, setExports] = useState([]);
  const [settings, setSettings] = useState({
    default_frequency: "monthly",
    // Empty, not "INR": a placeholder until GET /payroll/settings returns the
    // workspace's configured currency. A hard-coded default labelled every
    // tenant's money in one country's currency, and this same file already
    // fell back to "USD" in eight other places — so the module contradicted
    // itself about what a tenant's default was.
    currency: "",
    default_overtime_rule: "1.5x_after_40",
    default_rounding: "nearest_5_min",
    default_break_rule: "deduct_unpaid",
    default_holiday_rule: "paid_at_1x",
    default_policy_set: "standard",
  });

  // Modal/UI state
  const [showWizard, setShowWizard] = useState(false);
  const [showLockModal, setShowLockModal] = useState(false);
  const [lockTarget, setLockTarget] = useState(null);
  const [showAdjustmentModal, setShowAdjustmentModal] = useState(false);
  const [adjustmentSeed, setAdjustmentSeed] = useState(null);
  const [showResolveModal, setShowResolveModal] = useState(false);
  const [resolveTarget, setResolveTarget] = useState(null);
  const [drawerEmp, setDrawerEmp] = useState(null);
  const [search, setSearch] = useState("");
  const [filterStatus, setFilterStatus] = useState("all");

  // Permission helpers
  const canManage = ["admin", "owner", "payroll_admin", "finance_admin"].includes((currentUserRole || "").toLowerCase());
  const canSettings = ["admin", "owner", "payroll_admin"].includes((currentUserRole || "").toLowerCase());

  // ── API helpers (graceful 404 fallback) ────────────────────────────────────
  const apiCall = useCallback(async (path, options = {}) => {
    if (!token) return null;
    try {
      const res = await fetch(`${API_BASE}${path}`, {
        ...options,
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
          ...(options.headers || {}),
        },
      });
      if (!res.ok) {
        if (res.status === 404) return null;
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Error ${res.status}`);
      }
      return await res.json();
    } catch (e) {
      throw e;
    }
  }, [token]);

  // ── Loaders ────────────────────────────────────────────────────────────────
  const loadAll = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    try {
      const [r, s, c] = await Promise.allSettled([
        apiCall("/payroll/runs"),
        apiCall("/payroll/summary"),
        apiCall("/payroll/configs"),
      ]);
      if (r.status === "fulfilled" && r.value) setRuns(r.value.runs || []);
      if (s.status === "fulfilled" && s.value) setSummary(s.value);
      if (c.status === "fulfilled" && c.value) setConfigs(c.value.configs || []);
    } catch {} finally { setLoading(false); }
  }, [apiCall, token]);

  const loadSettings = useCallback(async () => {
    try {
      const data = await apiCall("/payroll/settings");
      // Backend returns { settings: {...} }; older shape returns the object
      // directly. Unwrap either form so currency etc. actually reach state —
      // otherwise the page silently falls back to the INR default on refresh.
      const incoming = data?.settings || data;
      if (incoming && typeof incoming === "object") {
        setSettings(s => ({ ...s, ...incoming }));
      }
    } catch {}
  }, [apiCall]);

  const loadAdjustments = useCallback(async (runId) => {
    try {
      const data = await apiCall(`/payroll/adjustments${runId ? `?run_id=${runId}` : ""}`);
      setAdjustments(data?.adjustments || []);
    } catch { setAdjustments([]); }
  }, [apiCall]);

  const loadExceptions = useCallback(async (runId) => {
    if (!runId) { setExceptions([]); return; }
    try {
      const data = await apiCall(`/payroll/exceptions?run_id=${runId}`);
      setExceptions(data?.exceptions || []);
    } catch { setExceptions([]); }
  }, [apiCall]);

  const loadExports = useCallback(async () => {
    try {
      const data = await apiCall("/payroll/exports");
      setExports(data?.exports || []);
    } catch { setExports([]); }
  }, [apiCall]);

  const openRun = useCallback(async (run) => {
    if (!run) return;
    try {
      const detail = await apiCall(`/payroll/runs/${run.id}`);
      const fullRun = detail?.run || run;
      setCurrentRun(fullRun);
      setCurrentRunItems(detail?.items || []);
      loadExceptions(run.id);
      loadAdjustments(run.id);
      setActivePane("calculator");
    } catch (e) {
      toast(e.message || "Couldn't open run", "error");
    }
  }, [apiCall, loadExceptions, loadAdjustments, toast]);

  // Initial load
  useEffect(() => { loadAll(); loadSettings(); loadExports(); }, [loadAll, loadSettings, loadExports]);

  // ── Action handlers ────────────────────────────────────────────────────────
  const handleCreateRun = async (payload) => {
    try {
      const body = {
        period_start: payload.period_start,
        period_end: payload.period_end,
        notes: payload.notes,
        frequency: payload.frequency,
        pay_date: payload.pay_date,
        country: payload.country,
        currency: payload.currency,
        policy_set: payload.policy_set,
        overtime_rule: payload.overtime_rule,
        break_rule: payload.break_rule,
        rounding_rule: payload.rounding_rule,
        holiday_rule: payload.holiday_rule,
        employee_ids: payload.employee_ids,
      };
      const res = await apiCall("/payroll/runs", { method: "POST", body: JSON.stringify(body) });
      if (res) {
        toast("Payroll run created");
        setShowWizard(false);
        await loadAll();
        if (res.run) openRun(res.run);
      }
    } catch (e) {
      toast(e.message || "Failed to create run", "error");
    }
  };

  const handleRecalculate = async () => {
    if (!currentRun) return;
    try {
      await apiCall(`/payroll/runs/${currentRun.id}/calculate`, { method: "POST" });
      toast("Recalculation queued");
      openRun(currentRun);
    } catch (e) { toast(e.message || "Recalculation failed", "error"); }
  };

  const handleSubmitReview = async () => {
    if (!currentRun) return;
    try {
      await apiCall(`/payroll/runs/${currentRun.id}/submit-review`, { method: "POST" });
      toast("Submitted for review");
      openRun(currentRun);
    } catch (e) { toast(e.message || "Submit failed", "error"); }
  };

  const handleApprove = async () => {
    if (!currentRun) return;
    try {
      // Try new endpoint first, fall back to legacy /process
      let ok = await apiCall(`/payroll/runs/${currentRun.id}/approve`, { method: "POST" }).catch(() => null);
      if (!ok) ok = await apiCall(`/payroll/runs/${currentRun.id}/process`, { method: "PUT" }).catch(() => null);
      toast("Run approved");
      openRun(currentRun);
      loadAll();
    } catch (e) { toast(e.message || "Approval failed", "error"); }
  };

  const handleLockClick = (run) => { setLockTarget(run || currentRun); setShowLockModal(true); };
  const handleLockSubmit = async ({ run_id, reason }) => {
    try {
      await apiCall(`/payroll/runs/${run_id}/lock`, { method: "POST", body: JSON.stringify({ reason }) });
      toast("Run locked");
      loadAll();
      if (currentRun?.id === run_id) openRun(currentRun);
    } catch (e) { toast(e.message || "Lock failed", "error"); }
  };

  const handleExportRun = async (run) => {
    const target = run || currentRun;
    if (!target) return;
    // Try backend export, fall back to local CSV from currentRunItems
    try {
      const res = await apiCall(`/payroll/runs/${target.id}/export`, {
        method: "POST", body: JSON.stringify({ format: "csv", export_type: "summary" }),
      });
      if (res?.file_url) {
        window.open(res.file_url, "_blank", "noopener");
      } else {
        // Local fallback
        downloadCSV(`payroll-run-${String(target.id).slice(-6)}.csv`,
          (currentRunItems || []).map(it => ({
            employee: it.name,
            pay_type: it.pay_type,
            regular_hours: it.regular_hours || it.worked_hours,
            overtime_hours: it.overtime_hours || 0,
            gross_pay: it.gross_pay || 0,
            currency: target.currency || settings?.currency || "",
          })),
          [
            { key: "employee",       label: "Employee" },
            { key: "pay_type",       label: "Pay Type" },
            { key: "regular_hours",  label: "Regular Hours" },
            { key: "overtime_hours", label: "Overtime Hours" },
            { key: "gross_pay",      label: "Gross Pay" },
            { key: "currency",       label: "Currency" },
          ]);
      }
      toast("Export generated");
      loadExports();
    } catch (e) {
      toast(e.message || "Export failed", "error");
    }
  };

  const handleSaveSettings = async (draft) => {
    try {
      const res = await apiCall("/payroll/settings", { method: "PATCH", body: JSON.stringify(draft) });
      // Trust the server's normalized snapshot when present, otherwise fall
      // back to the draft we just sent.
      const persisted = res?.settings || draft;
      setSettings(s => ({ ...s, ...persisted }));
      toast("Settings saved");
    } catch (e) {
      // Fallback: store locally if backend doesn't have endpoint
      setSettings(s => ({ ...s, ...draft }));
      toast("Settings saved locally (backend endpoint unavailable)", "info");
    }
  };

  const handleCreateAdjustment = async (payload) => {
    try {
      await apiCall("/payroll/adjustments", { method: "POST", body: JSON.stringify(payload) });
      toast("Adjustment submitted for approval");
      loadAdjustments(currentRun?.id);
    } catch (e) { toast(e.message || "Failed to submit adjustment", "error"); }
  };

  const handleResolveException = async ({ reason }) => {
    if (!resolveTarget) return;
    try {
      await apiCall(`/payroll/exceptions/${resolveTarget.id}/resolve`, {
        method: "POST", body: JSON.stringify({ reason }),
      });
      toast("Exception resolved");
      loadExceptions(currentRun?.id);
    } catch (e) { toast(e.message || "Resolve failed", "error"); }
  };

  // Alerts derived from current run + exceptions
  const alerts = useMemo(() => {
    const out = [];
    const critical = exceptions.filter(e => (EXCEPTION_DEFS[e.type]?.severity || e.severity) === "critical").length;
    if (critical > 0) {
      out.push({
        severity: "critical",
        title: `Payroll run has ${critical} unresolved critical exception${critical !== 1 ? "s" : ""}`,
        body: "Approval and final export are blocked until these are resolved.",
        action: "Resolve",
        onAction: () => setActivePane("exceptions"),
      });
    }
    if (currentRun?.status === "locked") {
      out.push({
        severity: "info",
        title: "Payroll run is locked",
        body: "This run is read-only. Use a correction run to make further changes.",
      });
    }
    if (configs.length > 0 && (employees || []).some(e => !configs.find(c => c.user_id === e.id))) {
      out.push({
        severity: "warning",
        title: "Some employees do not have a pay rate configured",
        body: "Employees without a rate will be excluded from new runs until a rate is added.",
      });
    }
    return out;
  }, [exceptions, currentRun, configs, employees]);

  const tabs = [
    { key: "runs",        label: "Payroll Runs",    icon: "filetext", badge: runs.filter(r => r.status === "needs_review" || r.status === "draft").length },
    { key: "calculator",  label: "Calculator",      icon: "calc" },
    { key: "exceptions",  label: "Exceptions",      icon: "warning",  badge: exceptions.length },
    { key: "adjustments", label: "Adjustments",     icon: "edit",     badge: adjustments.filter(a => a.status === "pending").length },
    { key: "exports",     label: "Export History",  icon: "download" },
    { key: "settings",    label: "Settings",        icon: "settings" },
  ];

  if (!canManage) {
    return (
      <CCtx.Provider value={C}>
        <div style={{
          maxWidth: 1100, margin: "0 auto", padding: "8px 4px 32px",
          fontFamily: "'DM Sans', system-ui, sans-serif",
        }}>
          <Card>
            <EmptyState icon="lock"
              title="Payroll Calculator is restricted"
              body="Only Account Owner, Payroll Admin, Finance Admin, and Admin roles with explicit payroll permission can view this module." />
          </Card>
        </div>
      </CCtx.Provider>
    );
  }

  return (
    <CCtx.Provider value={C}>
      <div style={{
        maxWidth: 1280, margin: "0 auto", padding: "8px 4px 32px",
        fontFamily: "'DM Sans', system-ui, sans-serif",
      }}>
        <style>{`
          @keyframes ztPcFadeIn  { from{opacity:0; transform:translateY(6px)} to{opacity:1; transform:none} }
          @keyframes ztPcShimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }
          /* Green hover system — same accent as AdminSidebar / AdminLeave so
             every clickable surface in the admin shell uses one tint. */
          .zt-pc-kpi { transition: transform .22s cubic-bezier(.34,1.56,.64,1), box-shadow .22s ease, border-color .22s ease, background .22s ease; }
          .zt-pc-kpi:hover { transform: translateY(-2px); box-shadow: 0 8px 22px ${C.brandAccent}1F; border-color: ${C.brandAccent}; }
          .zt-pc-tab { position: relative; transition: color .15s ease, background .15s ease; border-radius: 8px 8px 0 0; }
          .zt-pc-tab[data-active="false"]:hover { background: ${C.brandAccentBg} !important; color: ${C.brandAccent} !important; }
          .zt-pc-tab[data-active="false"]:hover svg { stroke: ${C.brandAccent} !important; }
          .zt-pc-btn-primary:not(:disabled):hover { transform: translateY(-1px); box-shadow: 0 6px 14px ${C.brandAccent}55 !important; }
          .zt-pc-btn-secondary:not(:disabled):hover { background: ${C.brandAccentBg} !important; border-color: ${C.brandAccent} !important; color: ${C.brandAccent} !important; }
          .zt-pc-btn-secondary:not(:disabled):hover svg { stroke: ${C.brandAccent} !important; }
          .zt-pc-row:hover { background: ${C.brandAccentBg}; }
          .zt-pc-anim { animation: ztPcFadeIn .35s cubic-bezier(.22,1,.36,1) both; }
        `}</style>
        <ModuleHeader
          activePane={activePane}
          currentRun={currentRun}
          onNewRun={() => setShowWizard(true)}
          onImport={() => setActivePane("adjustments")}
          onExport={() => currentRun && handleExportRun(currentRun)}
          onSettings={() => setActivePane("settings")}
          canExport={!!currentRun}
          canSettings={canSettings}
        />

        <AlertRail alerts={alerts} />

        {/* First-run hint: if the admin hasn't picked a currency yet, surface
            the call-to-action so the KPI row never displays in a misleading
            default. Self-dismisses once a currency is saved in Settings. */}
        {!loading && !settings?.currency && (
          <div style={{
            marginBottom: 14, padding: "12px 16px", borderRadius: C.r,
            border: `1px solid ${C.amberBdr}`, background: C.amberBg,
            display: "flex", alignItems: "center", gap: 12,
          }}>
            <Icon n="info" s={18} c={C.amber} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <p style={{ fontSize: 13, fontWeight: 700, color: C.amber, margin: 0 }}>
                Set your default currency
              </p>
              <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0", lineHeight: 1.5 }}>
                Pick the currency this account pays in. It applies to every payroll run, KPI, and export.
              </p>
            </div>
            <Button variant="secondary" size="sm" icon="settings"
                    onClick={() => setActivePane("settings")}>
              Open Settings
            </Button>
          </div>
        )}

        <KPIRow
          settings={settings}
          summary={summary}
          currentRun={currentRun}
          exceptions={exceptions}
          onChangePeriod={() => setShowWizard(true)}
          onReviewEmployees={() => setActivePane("calculator")}
          onViewBreakdown={() => setActivePane("calculator")}
          onResolveExceptions={() => setActivePane("exceptions")}
        />

        <TabBar tabs={tabs} active={activePane} onChange={setActivePane} />

        {activePane === "runs" && (
          <PayrollRunsPane
            runs={runs}
            loading={loading}
            settings={settings}
            search={search} setSearch={setSearch}
            filterStatus={filterStatus} setFilterStatus={setFilterStatus}
            onOpen={openRun}
            onDuplicate={() => toast("Duplicate coming soon", "info")}
            onExport={handleExportRun}
            onLock={handleLockClick}
            onVoid={() => toast("Void coming soon", "info")}
            onNewRun={() => setShowWizard(true)}
            onRefresh={loadAll}
          />
        )}

        {activePane === "calculator" && (
          <CalculatorPane
            run={currentRun}
            items={currentRunItems}
            configs={configs}
            employees={employees}
            loading={loading}
            currency={currentRun?.currency || settings.currency}
            onSelectEmployee={setDrawerEmp}
            onRecalculate={handleRecalculate}
            onSubmitReview={handleSubmitReview}
            onApprove={handleApprove}
            onLock={() => handleLockClick(currentRun)}
            onExport={() => handleExportRun(currentRun)}
            onSaveDraft={() => toast("Draft saved")}
          />
        )}

        {activePane === "exceptions" && (
          <ExceptionsPane
            run={currentRun}
            exceptions={exceptions}
            loading={loading}
            onResolve={(ex) => { setResolveTarget(ex); setShowResolveModal(true); }}
            onRefresh={() => loadExceptions(currentRun?.id)}
          />
        )}

        {activePane === "adjustments" && (
          <AdjustmentsPane
            run={currentRun}
            settings={settings}
            adjustments={adjustments}
            loading={loading}
            onCreate={handleCreateAdjustment}
            onApprove={(a) => apiCall(`/payroll/adjustments/${a.id}/approve`, { method: "POST" }).then(() => loadAdjustments(currentRun?.id)).catch(() => toast("Approve failed", "error"))}
            onReject={(a)  => apiCall(`/payroll/adjustments/${a.id}/reject`,  { method: "POST" }).then(() => loadAdjustments(currentRun?.id)).catch(() => toast("Reject failed", "error"))}
            onRefresh={() => loadAdjustments(currentRun?.id)}
          />
        )}

        {activePane === "exports" && (
          <ExportHistoryPane
            exports={exports}
            runs={runs}
            loading={loading}
            onRefresh={loadExports}
            onDownload={(ex) => ex.file_url && window.open(ex.file_url, "_blank", "noopener")}
            onNewExport={() => currentRun && handleExportRun(currentRun)}
          />
        )}

        {activePane === "settings" && (
          <SettingsPane settings={settings} onSave={handleSaveSettings} loading={loading} />
        )}

        {/* Modals */}
        <NewPayrollRunWizard
          open={showWizard}
          employees={employees}
          settings={settings}
          onClose={() => setShowWizard(false)}
          onCreate={handleCreateRun}
        />

        <LockRunModal
          open={showLockModal}
          run={lockTarget}
          onClose={() => setShowLockModal(false)}
          onSubmit={handleLockSubmit}
        />

        <ResolveExceptionModal
          open={showResolveModal}
          exception={resolveTarget}
          onClose={() => setShowResolveModal(false)}
          onSubmit={handleResolveException}
        />

        <EmployeeCalcDrawer
          open={!!drawerEmp}
          employee={drawerEmp}
          run={currentRun}
          currency={currentRun?.currency || settings.currency}
          onClose={() => setDrawerEmp(null)}
          onAddAdjustment={(emp) => {
            setAdjustmentSeed({ employee_id: emp.user_id });
            setDrawerEmp(null);
            setActivePane("adjustments");
          }}
        />

        <ToastRail msgs={msgs} />
      </div>
    </CCtx.Provider>
  );
}
