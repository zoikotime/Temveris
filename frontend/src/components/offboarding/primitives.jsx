/**
 * Offboarding shared UI primitives — admin-theme aligned.
 *
 * Every primitive consumes ThemeContext (`useTheme().C`) so they paint with the
 * exact same surface / brand / border tokens the rest of the admin uses
 * (Team Directory, Screenshots, Attendance, Reporting). No Tailwind, no
 * page-specific colours — only the global design system.
 */
import React from "react";
import {
  AlertTriangle, Shield, Clock, EyeOff, Hourglass, AlertCircle,
  CheckCircle2, CircleSlash,
} from "lucide-react";
import { CASE_STATE_LABEL, isRedacted } from "../../api/offboarding";
import { useTheme } from "../../context/ThemeContext";

// ── tone helpers ─────────────────────────────────────────────────────────────
// Map domain semantics to the theme palette so every chip in the module reads
// from the same source as the rest of the admin (success/warn/error/brand).

function caseStateTone(status, C) {
  const success    = { bg: C.greenBg,  fg: C.green,  bdr: C.greenBdr };
  const warn       = { bg: C.amberBg,  fg: C.amber,  bdr: C.amberBdr };
  const danger     = { bg: C.redBg,    fg: C.red,    bdr: C.redBdr };
  const brand      = { bg: C.brandBg,  fg: C.brand,  bdr: C.brandBdr };
  const neutral    = { bg: C.surfaceAlt, fg: C.textSub, bdr: C.border };

  switch (status) {
    case "draft":                     return neutral;
    case "pending_approval":          return warn;
    case "approved":                  return brand;
    case "scheduled":                 return brand;
    case "in_progress":               return brand;
    case "access_closure_pending":    return warn;
    case "access_revocation_failed":  return danger;
    case "final_review_pending":      return warn;
    case "payroll_closure_pending":   return warn;
    case "billing_closure_pending":   return warn;
    case "legal_hold_review_pending": return warn;
    case "former_access_pending":     return brand;
    case "blocked":                   return danger;
    case "completed":                 return success;
    case "reversed":                  return neutral;
    case "cancelled":                 return neutral;
    default:                          return neutral;
  }
}

function riskTone(risk, C) {
  switch (risk) {
    case "low":      return { bg: C.greenBg, fg: C.green,  bdr: C.greenBdr };
    case "standard": return { bg: C.surfaceAlt, fg: C.textSub, bdr: C.border };
    case "elevated": return { bg: C.amberBg, fg: C.amber,  bdr: C.amberBdr };
    case "high":     return { bg: C.warnBg,  fg: C.warn,   bdr: C.warnBorder };
    case "critical": return { bg: C.redBg,   fg: C.red,    bdr: C.redBdr };
    default:         return { bg: C.surfaceAlt, fg: C.textSub, bdr: C.border };
  }
}

const chipStyle = ({ bg, fg, bdr }) => ({
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  padding: "3px 10px",
  borderRadius: 999,
  fontSize: 11,
  fontWeight: 700,
  background: bg,
  color: fg,
  border: `1px solid ${bdr}`,
  lineHeight: 1.6,
  whiteSpace: "nowrap",
});

// ─────────────────────────────────────────────────────────────────────────────

export function StatusChip({ status, className = "" }) {
  const { C } = useTheme();
  const tone = caseStateTone(status, C);
  const label = CASE_STATE_LABEL[status] || status;
  return (
    <span
      role="status"
      aria-label={`Case status: ${label}`}
      className={className}
      style={chipStyle(tone)}
    >
      {label}
    </span>
  );
}

export function RiskBadge({ risk = "standard", className = "" }) {
  const { C } = useTheme();
  const tone = riskTone(risk, C);
  const label = risk[0].toUpperCase() + risk.slice(1);
  return (
    <span
      aria-label={`Risk level: ${label}`}
      className={className}
      style={chipStyle(tone)}
    >
      <Shield size={11} aria-hidden="true" /> {label}
    </span>
  );
}

export function BlockerChip({ count = 0, className = "" }) {
  const { C } = useTheme();
  if (!count) {
    return (
      <span
        className={className}
        style={chipStyle({ bg: C.greenBg, fg: C.green, bdr: C.greenBdr })}
      >
        <CheckCircle2 size={11} aria-hidden="true" /> No blockers
      </span>
    );
  }
  const tone = count >= 3
    ? { bg: C.redBg, fg: C.red, bdr: C.redBdr }
    : { bg: C.amberBg, fg: C.amber, bdr: C.amberBdr };
  return (
    <span
      aria-label={`${count} open blocker${count === 1 ? "" : "s"}`}
      className={className}
      style={chipStyle(tone)}
    >
      <AlertTriangle size={11} aria-hidden="true" /> {count} blocker{count === 1 ? "" : "s"}
    </span>
  );
}

export function SLAIndicator({ dueAt, label = "SLA", className = "" }) {
  const { C } = useTheme();
  if (!dueAt) {
    return (
      <span
        className={className}
        style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, color: C.textLight }}
      >
        <Hourglass size={11} aria-hidden="true" /> No SLA
      </span>
    );
  }
  const due = new Date(dueAt);
  const now = new Date();
  const diffMs = due - now;
  const diffH = Math.round(diffMs / 36e5);

  let tone, icon, text;
  if (diffMs < 0) {
    tone = { bg: C.redBg, fg: C.red, bdr: C.redBdr };
    icon = <AlertCircle size={11} aria-hidden="true" />;
    text = `${label} breached ${Math.abs(diffH)}h ago`;
  } else if (diffH <= 24) {
    tone = { bg: C.amberBg, fg: C.amber, bdr: C.amberBdr };
    icon = <Clock size={11} aria-hidden="true" />;
    text = `${label} in ${diffH}h`;
  } else {
    tone = { bg: C.surfaceAlt, fg: C.textSub, bdr: C.border };
    icon = <Clock size={11} aria-hidden="true" />;
    text = `${label} in ${Math.round(diffH / 24)}d`;
  }
  return (
    <span
      role="status"
      aria-label={text}
      className={className}
      style={chipStyle(tone)}
    >
      {icon} {text}
    </span>
  );
}

export function RedactedField({ value, fallback = "—", className = "" }) {
  const { C } = useTheme();
  if (value == null || value === "") {
    return <span className={className} style={{ color: C.textLight }}>{fallback}</span>;
  }
  if (isRedacted(value)) {
    return (
      <span
        role="note"
        aria-label="Field withheld by role-based access control"
        className={className}
        style={chipStyle({ bg: C.surfaceAlt, fg: C.textSub, bdr: C.border })}
      >
        <EyeOff size={11} aria-hidden="true" /> Withheld
      </span>
    );
  }
  return <span className={className} style={{ color: C.text }}>{value}</span>;
}

export function SeverityDot({ severity = "low" }) {
  const { C } = useTheme();
  const colour = {
    critical: C.red,
    high:     C.warn,
    medium:   C.amber,
    low:      C.textLight,
  }[severity] || C.textLight;
  return (
    <span
      aria-label={`Severity ${severity}`}
      style={{ display: "inline-block", width: 9, height: 9, borderRadius: "50%", background: colour }}
    />
  );
}

/**
 * ActionButton — variants match the admin dashboard's button family:
 *   primary   → brand gradient pill (matches header CTAs)
 *   secondary → outlined surface
 *   subtle    → ghost / quiet
 *   danger    → red gradient
 */
export function ActionButton({
  variant = "primary",
  size = "md",
  className = "",
  style,
  children,
  ...rest
}) {
  const { C, isDark } = useTheme();
  const dims = size === "sm"
    ? { padding: "6px 12px", fontSize: 12 }
    : { padding: "9px 16px", fontSize: 13 };

  const base = {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    borderRadius: C.rSm,
    fontWeight: 700,
    cursor: rest.disabled ? "not-allowed" : "pointer",
    fontFamily: "inherit",
    transition: "all .18s ease",
    border: "none",
    opacity: rest.disabled ? 0.55 : 1,
    lineHeight: 1.4,
    ...dims,
  };

  const variants = {
    primary: {
      background: `linear-gradient(135deg, ${C.brand}, ${C.brandDark})`,
      color: "#fff",
      boxShadow: `0 2px 8px ${C.brandAccent}33`,
    },
    secondary: {
      background: C.surface,
      color: C.text,
      border: `1.5px solid ${C.border}`,
    },
    subtle: {
      background: "transparent",
      color: C.textSub,
      border: `1.5px solid ${C.border}`,
    },
    danger: {
      background: `linear-gradient(135deg, ${C.red}, ${isDark ? "#991B1B" : "#9F1A12"})`,
      color: "#fff",
      boxShadow: `0 2px 8px ${C.red}33`,
    },
  };

  return (
    <button
      className={className}
      style={{ ...base, ...variants[variant], ...(style || {}) }}
      {...rest}
    >
      {children}
    </button>
  );
}

export function EmptyState({ title, hint, icon }) {
  const { C } = useTheme();
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 8,
        padding: "40px 24px",
        borderRadius: C.r,
        border: `1px dashed ${C.border}`,
        background: C.surfaceAlt,
        textAlign: "center",
      }}
    >
      <div
        style={{
          width: 40, height: 40, borderRadius: 12,
          background: C.brandBg, border: `1px solid ${C.brandBdr}`,
          display: "flex", alignItems: "center", justifyContent: "center",
        }}
      >
        {icon || <CircleSlash size={20} color={C.brand} aria-hidden="true" />}
      </div>
      <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>{title}</div>
      {hint && <div style={{ fontSize: 12, color: C.textSub, maxWidth: 360 }}>{hint}</div>}
    </div>
  );
}

export function SectionHeading({ title, description, right }) {
  const { C } = useTheme();
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "flex-end",
        justifyContent: "space-between",
        gap: 12,
        marginBottom: 16,
      }}
    >
      <div>
        <h2 style={{ fontSize: 16, fontWeight: 800, color: C.text, margin: 0 }}>{title}</h2>
        {description && (
          <p style={{ fontSize: 12, color: C.textSub, marginTop: 4, margin: 0 }}>{description}</p>
        )}
      </div>
      {right && <div style={{ display: "flex", alignItems: "center", gap: 8 }}>{right}</div>}
    </div>
  );
}

export function DataRow({ label, children }) {
  const { C } = useTheme();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, padding: "6px 0" }}>
      <dt style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.6px", color: C.textLight }}>
        {label}
      </dt>
      <dd style={{ fontSize: 13, color: C.text, margin: 0 }}>{children}</dd>
    </div>
  );
}

/**
 * Surface — the standard admin card. Drop-in for what used to be
 * `rounded-xl border bg-white p-4 shadow-sm` in the offboarding tree.
 */
export function Surface({ children, style, padding = "22px 24px" }) {
  const { C } = useTheme();
  return (
    <div
      style={{
        background: C.surface,
        borderRadius: 16,
        border: `1px solid ${C.border}`,
        padding,
        boxShadow: "0 1px 2px rgba(16, 24, 40, 0.04)",
        ...(style || {}),
      }}
    >
      {children}
    </div>
  );
}
