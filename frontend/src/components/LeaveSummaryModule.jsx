/**
 * LeaveSummaryModule.jsx — admin-facing Leave Summary command surface.
 *
 * Architecture (per the Detailed Wireframe Specification):
 *   1. ModuleHeader with scope filters (date / team / location / type / status / payroll)
 *   2. Executive metrics strip (Currently on leave · Upcoming 7 days · Pending · Coverage risk · Payroll blockers)
 *   3. Risk panels (coverage risk, approval aging, payroll cutoff, policy exceptions, sync errors)
 *   4. Calendar / coverage heatmap (month view with daily density)
 *   5. Pending approval queue (manager review · HR review · needs info · cutoff)
 *   6. Detailed records table (role-scoped, redacted sensitive fields)
 *   7. Pivots — by team, by leave type, by payroll readiness
 *   8. Employee detail side panel (slide-out)
 *
 * Backend endpoints used (all already shipped, no new server work needed):
 *   GET /leave/requests   list of all leave records with worker metadata
 *   GET /leave/types      catalog of leave types and policies
 *   GET /leave/policies   policy rules (notice, evidence gate, accrual)
 *   GET /employees        employee directory (id, name, department, team) —
 *                         the lightweight directory list. NOT /employees/team,
 *                         which recomputes live clock/break/idle status the
 *                         Leave Summary never reads and is far slower to load.
 *
 * Design intent: every aggregate is computed client-side so the page works
 * with the endpoints that already exist. Future server-side aggregation
 * (GET /admin/leave/summary, /calendar, /pending, /exceptions) can replace
 * the client compute layer without changing the UI.
 *
 * Privacy doctrine: by default the operational category is shown (e.g.
 * "Approved Leave", "Sick Leave"). Worker-submitted descriptions, attachments,
 * and AWOL/HR flags are gated to HR/Owner roles and recorded in the audit log
 * when accessed (server-side audit hook is a future drop-in — the UI already
 * marks the access intent via a "View sensitive details" button).
 */

import React, { useEffect, useMemo, useState, useCallback, useRef } from "react";
import { useTheme } from "../context/ThemeContext";
import { API_BASE } from "../config";

const TOKEN = () => localStorage.getItem("zt_token") || localStorage.getItem("token");

// Module-level context so sub-components get C without prop-drilling.
const CCtx = React.createContext(null);
const useC = () => React.useContext(CCtx);

// ─── helpers ────────────────────────────────────────────────────────────────
const fmtDate = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch { return "—"; }
};

const fmtDateShort = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
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

const dayKey = (d) => {
  if (!d) return "";
  const dt = (d instanceof Date) ? d : new Date(d);
  if (isNaN(dt)) return "";
  const y = dt.getFullYear(), m = String(dt.getMonth() + 1).padStart(2, "0"), day = String(dt.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
};

const startOfMonth = (d) => new Date(d.getFullYear(), d.getMonth(), 1);
const endOfMonth   = (d) => new Date(d.getFullYear(), d.getMonth() + 1, 0);
const addDays      = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };

const overlapsDay = (req, day) => {
  if (!req?.from_date || !req?.to_date) return false;
  const dk = dayKey(day);
  const fk = dayKey(req.from_date);
  const tk = dayKey(req.to_date);
  return dk >= fk && dk <= tk;
};

const inRange = (req, fromKey, toKey) => {
  if (!req?.from_date || !req?.to_date) return false;
  const fk = dayKey(req.from_date);
  const tk = dayKey(req.to_date);
  return !(tk < fromKey || fk > toKey);
};

const STATUS_LABELS = {
  submitted:          { label: "Submitted",       tone: "info"    },
  under_review:       { label: "Under Review",    tone: "warn"    },
  approved:           { label: "Approved",        tone: "success" },
  rejected:           { label: "Rejected",        tone: "danger"  },
  cancelled:          { label: "Cancelled",       tone: "neutral" },
  locked_for_payroll: { label: "Locked",          tone: "purple"  },
};

const SENSITIVE_TYPES = new Set([
  "Sick Leave", "Maternity Leave", "Paternity Leave",
  "Bereavement Leave", "Adoption Leave", "Medical Escort Leave",
]);

// Canonical team key: case-, whitespace-, connector- AND word-order-insensitive.
// Collapses split spellings of the same team into one dropdown entry and one
// filter bucket — "Creative & Design" / "Design and Creative" → "creative design",
// the three spellings of "Technology Infrastructure" → one. The dropdown option
// `value` and the record-filter comparison MUST both route through this so a
// selected team still matches records saved under either spelling.
const normTeam = (v) =>
  String(v == null ? "" : v)
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(w => w && w !== "and")
    .sort()
    .join(" ");

const loadXLSX = () => import("xlsx");

// Export to a real .xlsx workbook (not CSV). This avoids the two Excel defects
// the CSV export produced:
//   1. Encoding — Windows Excel opens a UTF-8 .csv as ANSI regardless of the
//      MIME charset, so special characters (e.g. the "—" redaction placeholder)
//      were mangled into "â€"". A true xlsx stores strings as UTF-8 XML, so
//      there's nothing for Excel to misdecode.
//   2. "######" — CSV can't carry column widths, so Excel clipped date/number
//      columns. Here we set auto-fit widths from the content length.
const downloadXLSX = async (filename, rows, headers) => {
  const XLSX = await loadXLSX();
  const aoa = [
    headers.map(h => h.label),
    ...rows.map(r => headers.map(h => (r[h.key] == null ? "" : r[h.key]))),
  ];
  const ws = XLSX.utils.aoa_to_sheet(aoa);
  // Auto-fit: each column as wide as its longest value (header included),
  // padded a little and clamped so a stray long field can't blow out the sheet.
  ws["!cols"] = headers.map(h => {
    const maxLen = [h.label, ...rows.map(r => (r[h.key] == null ? "" : String(r[h.key])))]
      .reduce((m, s) => Math.max(m, String(s).length), 0);
    return { wch: Math.min(48, Math.max(10, maxLen + 2)) };
  });
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "Leave Summary");
  XLSX.writeFile(wb, filename);
};

// ─── inline icon set ────────────────────────────────────────────────────────
const ICON = {
  briefcase: "M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2",
  calendar:  "M3 4h18v18H3z M16 2v4 M8 2v4 M3 10h18",
  users:     "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2 M9 11a4 4 0 100-8 4 4 0 000 8 M23 21v-2a4 4 0 00-3-3.87 M16 3.13a4 4 0 010 7.75",
  clock:     "M12 22a10 10 0 100-20 10 10 0 000 20z M12 6v6l4 2",
  warning:   "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
  alert:     "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
  shield:    "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  check:     "M20 6L9 17l-5-5",
  x:         "M18 6L6 18 M6 6l12 12",
  search:    "M11 19a8 8 0 100-16 8 8 0 000 16z M21 21l-4.35-4.35",
  download:  "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  refresh:   "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10 M1 14l4.64 4.36A9 9 0 0020.49 15",
  filter:    "M22 3H2l8 9.46V19l4 2v-8.54L22 3z",
  chevright: "M9 18l6-6-6-6",
  chevleft:  "M15 18l-6-6 6-6",
  chevdown:  "M6 9l6 6 6-6",
  user:      "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2 M12 11a4 4 0 100-8 4 4 0 000 8",
  lock:      "M19 11H5a2 2 0 00-2 2v7a2 2 0 002 2h14a2 2 0 002-2v-7a2 2 0 00-2-2z M7 11V7a5 5 0 0110 0v4",
  info:      "M12 22a10 10 0 100-20 10 10 0 000 20z M12 16v-4 M12 8h.01",
  dollar:    "M12 1v22 M17 5H9.5a3.5 3.5 0 100 7h5a3.5 3.5 0 110 7H6",
  flag:      "M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z M4 22V15",
  trending:  "M23 6l-9.5 9.5-5-5L1 18 M17 6h6v6",
  filetext:  "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8",
  external:  "M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6 M15 3h6v6 M10 14L21 3",
  map:       "M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z M12 13a3 3 0 100-6 3 3 0 000 6z",
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
          padding: "14px 20px", borderBottom: `1px solid ${C.borderLight}`,
          display: "flex", alignItems: "flex-start", gap: 10,
        }}>
          {icon && <span style={{ marginTop: 2 }}><Icon n={icon} s={18} c={C.brand} /></span>}
          <div style={{ flex: 1, minWidth: 0 }}>
            <h3 style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>{title}</h3>
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
      padding: "2px 10px", borderRadius: 999, fontSize: 11, fontWeight: 700,
      background: t.bg, color: t.fg, letterSpacing: 0.4,
      textTransform: "uppercase", whiteSpace: "nowrap",
    }}>
      {icon && <Icon n={icon} s={11} c={t.fg} w={2.5} />}
      {children}
    </span>
  );
};

const Button = ({ children, variant = "primary", onClick, disabled, icon, size = "md", style }) => {
  const C = useC();
  const variants = {
    primary:   { bg: disabled ? C.textLight : C.brand, color: "#fff", border: "none" },
    secondary: { bg: C.surface, color: C.text, border: `1px solid ${C.border}` },
    ghost:     { bg: "transparent", color: C.brand, border: "none" },
    danger:    { bg: disabled ? "#fbb" : C.danger, color: "#fff", border: "none" },
    success:   { bg: disabled ? C.textLight : C.green, color: "#fff", border: "none" },
  };
  const v = variants[variant] || variants.primary;
  const pad = size === "sm" ? "6px 12px" : "9px 16px";
  const fs  = size === "sm" ? 12 : 13;
  const mh  = size === "sm" ? 32 : 40;
  return (
    <button type="button" onClick={onClick} disabled={disabled}
      style={{
        padding: pad, borderRadius: C.rSm,
        background: v.bg, color: v.color, border: v.border,
        fontWeight: 600, fontSize: fs, cursor: disabled ? "not-allowed" : "pointer",
        fontFamily: "inherit", whiteSpace: "nowrap",
        display: "inline-flex", alignItems: "center", gap: 6, minHeight: mh,
        boxShadow: variant === "primary" && !disabled ? "0 1px 2px rgba(17,73,106,0.25)" : "none",
        opacity: disabled ? 0.7 : 1,
        ...style,
      }}>
      {icon && <Icon n={icon} s={size === "sm" ? 12 : 14} c={v.color} />}
      {children}
    </button>
  );
};

const Select = ({ value, onChange, options, disabled, style }) => {
  const C = useC();
  return (
    <select value={value ?? ""} onChange={(e) => onChange?.(e.target.value)} disabled={disabled}
      style={{
        padding: "8px 12px", borderRadius: C.rSm,
        border: `1px solid ${C.border}`, background: C.bg,
        fontSize: 13, color: C.text, fontFamily: "inherit",
        outline: "none", minHeight: 36,
        ...style,
      }}>
      {options.map(o => typeof o === "string"
        ? <option key={o} value={o}>{o}</option>
        : <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
};

const EmptyState = ({ icon = "info", title, body, action }) => {
  const C = useC();
  return (
    <div style={{ padding: "40px 24px", textAlign: "center", color: C.textSub, fontSize: 13, lineHeight: 1.5 }}>
      <div style={{
        width: 44, height: 44, borderRadius: "50%",
        background: C.brandBg, display: "inline-flex",
        alignItems: "center", justifyContent: "center", marginBottom: 10,
      }}>
        <Icon n={icon} s={20} c={C.brand} />
      </div>
      <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>{title}</p>
      {body && <p style={{ margin: "6px auto 0", maxWidth: 460 }}>{body}</p>}
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
        animation: "zt-leave-spin 0.8s linear infinite",
      }} />
      <style>{`@keyframes zt-leave-spin{to{transform:rotate(360deg)}}`}</style>
    </div>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// MODULE HEADER + FILTERS
// ═════════════════════════════════════════════════════════════════════════════

const ModuleHeader = ({ filters, setFilters, onExport, onConfigurePolicy, onRefresh, summary, scopeLabel }) => {
  const C = useC();
  const set = (k, v) => setFilters(f => ({ ...f, [k]: v }));
  return (
    <div style={{
      background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r,
      boxShadow: "0 1px 2px rgba(16,24,40,0.04)", marginBottom: 16,
      padding: "18px 22px",
    }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
        <div style={{
          width: 48, height: 48, borderRadius: 12, flexShrink: 0,
          background: `linear-gradient(135deg, ${C.brand}, ${C.brandDark})`,
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <Icon n="briefcase" s={22} c="#fff" w={2.2} />
        </div>
        <div style={{ flex: 1, minWidth: 240 }}>
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      letterSpacing: 0.6, textTransform: "uppercase", margin: 0 }}>
            Admin · Workforce
          </p>
          <h1 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: "2px 0 0" }}>
            Leave Summary
          </h1>
          <p style={{ fontSize: 13, color: C.textSub, margin: "6px 0 0", lineHeight: 1.6, maxWidth: 760 }}>
            View workforce leave by team, location, policy status, and payroll readiness.
            {scopeLabel && <> Scope: <strong style={{ color: C.text }}>{scopeLabel}</strong></>}
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, flexShrink: 0, flexWrap: "wrap" }}>
          <Button variant="secondary" icon="refresh" onClick={onRefresh}>Refresh</Button>
          <Button variant="secondary" icon="download" onClick={onExport}>Export</Button>
          <Button icon="filetext" onClick={onConfigurePolicy}>Configure policy</Button>
        </div>
      </div>

      {/* Filter row */}
      <div style={{
        marginTop: 14, paddingTop: 14, borderTop: `1px solid ${C.borderLight}`,
        display: "grid", gap: 10,
        gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
      }}>
        <FilterField C={C} label="Date range">
          <Select value={filters.range} onChange={v => set("range", v)} options={[
            { value: "today",     label: "Today" },
            { value: "week",      label: "This week" },
            { value: "pay_period",label: "This pay period" },
            { value: "month",     label: "This month" },
            { value: "quarter",   label: "This quarter" },
            { value: "year",      label: "This year" },
          ]} />
        </FilterField>
        <FilterField C={C} label="Team / department">
          <Select value={filters.team} onChange={v => set("team", v)} options={filters._teamOptions || [{ value: "all", label: "All teams" }]} />
        </FilterField>
        <FilterField C={C} label="Location">
          <Select value={filters.location} onChange={v => set("location", v)} options={filters._locationOptions || [{ value: "all", label: "All locations" }]} />
        </FilterField>
        <FilterField C={C} label="Leave type">
          <Select value={filters.leaveType} onChange={v => set("leaveType", v)} options={filters._typeOptions || [{ value: "all", label: "All types" }]} />
        </FilterField>
        <FilterField C={C} label="Status">
          <Select value={filters.status} onChange={v => set("status", v)} options={[
            { value: "active",       label: "Active (default)" },
            { value: "all",          label: "All statuses" },
            { value: "submitted",    label: "Submitted" },
            { value: "under_review", label: "Under Review" },
            { value: "approved",     label: "Approved" },
            { value: "rejected",     label: "Rejected" },
            { value: "locked_for_payroll", label: "Locked for payroll" },
          ]} />
        </FilterField>
        <FilterField C={C} label="Payroll readiness">
          <Select value={filters.payroll} onChange={v => set("payroll", v)} options={[
            { value: "all",         label: "All" },
            { value: "ready",       label: "Ready" },
            { value: "not_synced",  label: "Not synced" },
            { value: "missing_code",label: "Missing code" },
            { value: "cutoff_risk", label: "Cutoff risk" },
            { value: "blocked",     label: "Blocked" },
          ]} />
        </FilterField>
      </div>
    </div>
  );
};

const FilterField = ({ C, label, children }) => (
  <div>
    <label style={{ display: "block", fontSize: 10, fontWeight: 700, color: C.textLight,
                    letterSpacing: 0.5, textTransform: "uppercase", marginBottom: 4 }}>
      {label}
    </label>
    {children}
  </div>
);

// ═════════════════════════════════════════════════════════════════════════════
// EXECUTIVE METRICS STRIP
// ═════════════════════════════════════════════════════════════════════════════

const MetricCard = ({ label, value, sub, tone = "brand", icon, onClick }) => {
  const C = useC();
  const tones = {
    brand:  { bg: C.brandBg,  fg: C.brand  },
    green:  { bg: C.greenBg,  fg: C.green  },
    amber:  { bg: C.amberBg,  fg: C.amber  },
    danger: { bg: C.dangerBg, fg: C.danger },
    purple: { bg: C.purpleBg, fg: C.purple },
  };
  const t = tones[tone] || tones.brand;
  return (
    <button type="button" onClick={onClick}
      style={{
        background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r,
        boxShadow: "0 1px 2px rgba(16,24,40,0.04)",
        padding: 16, textAlign: "left", cursor: onClick ? "pointer" : "default",
        fontFamily: "inherit", display: "flex", flexDirection: "column", gap: 6,
        minHeight: 110, transition: "transform .15s, box-shadow .15s",
      }}
      onMouseEnter={(e) => { if (onClick) { e.currentTarget.style.transform = "translateY(-1px)"; e.currentTarget.style.boxShadow = "0 4px 12px rgba(16,24,40,0.08)"; } }}
      onMouseLeave={(e) => { e.currentTarget.style.transform = "translateY(0)"; e.currentTarget.style.boxShadow = "0 1px 2px rgba(16,24,40,0.04)"; }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <div style={{
          width: 30, height: 30, borderRadius: 8, flexShrink: 0,
          background: t.bg, display: "flex",
          alignItems: "center", justifyContent: "center",
        }}>
          <Icon n={icon} s={16} c={t.fg} />
        </div>
        <p style={{ fontSize: 10, color: C.textLight, fontWeight: 700,
                    textTransform: "uppercase", letterSpacing: 0.5, margin: 0 }}>
          {label}
        </p>
      </div>
      <p style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: "4px 0 0", lineHeight: 1.2 }}>
        {value}
      </p>
      {sub && <p style={{ fontSize: 11, color: C.textSub, margin: 0, lineHeight: 1.4 }}>{sub}</p>}
    </button>
  );
};

const MetricsStrip = ({ metrics, onJumpToPane }) => (
  <div style={{
    display: "grid", gap: 12,
    gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
    marginBottom: 16,
  }}>
    <MetricCard icon="users"    label="Currently on leave" value={metrics.onLeaveToday}
      sub={`${metrics.onLeaveTeams} teams affected`} tone="brand"
      onClick={() => onJumpToPane("table")} />
    <MetricCard icon="calendar" label="Upcoming 7 days"     value={metrics.upcoming7d}
      sub={`${metrics.upcomingTeams} teams`} tone="purple"
      onClick={() => onJumpToPane("calendar")} />
    <MetricCard icon="clock"    label="Pending approval"    value={metrics.pending}
      sub={metrics.pendingAging > 0 ? `${metrics.pendingAging} aging > 2d` : "Within SLA"}
      tone={metrics.pendingAging > 0 ? "amber" : "green"}
      onClick={() => onJumpToPane("queue")} />
    <MetricCard icon="warning"  label="Coverage risk"       value={metrics.coverageRisk}
      sub={`${metrics.coverageRiskTeams} teams flagged`}
      tone={metrics.coverageRisk > 0 ? "danger" : "green"}
      onClick={() => onJumpToPane("risks")} />
    <MetricCard icon="dollar"   label="Payroll blockers"    value={metrics.payrollBlockers}
      sub={metrics.payrollBlockers > 0 ? "Cutoff approaching" : "All clear"}
      tone={metrics.payrollBlockers > 0 ? "danger" : "green"}
      onClick={() => onJumpToPane("payroll")} />
  </div>
);

// ═════════════════════════════════════════════════════════════════════════════
// RISK & EXCEPTION PANELS
// ═════════════════════════════════════════════════════════════════════════════

const RiskPanels = ({ exceptions, onResolve }) => {
  const C = useC();
  const buckets = useMemo(() => {
    const b = { urgent: [], attention: [], info: [] };
    (exceptions || []).forEach(e => { (b[e.severity] || b.info).push(e); });
    return b;
  }, [exceptions]);

  return (
    <Card title="Risk and exception panels" icon="alert"
          subtitle="Operational and compliance risks. Neutral language; no misconduct implied.">
      {(exceptions?.length || 0) === 0 ? (
        <EmptyState icon="check" title="No active risks"
          body="No coverage gaps, approval aging, payroll cutoff, or policy exceptions in the current scope." />
      ) : (
        <div>
          {[
            { key: "urgent",    label: "Urgent",    tone: "danger" },
            { key: "attention", label: "Attention", tone: "warn"   },
            { key: "info",      label: "Info",      tone: "info"   },
          ].map(({ key, label, tone }) => {
            const list = buckets[key] || [];
            if (list.length === 0) return null;
            return (
              <div key={key} style={{ marginBottom: 10 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                  <Pill tone={tone}>{label}</Pill>
                  <span style={{ fontSize: 11, color: C.textLight }}>{list.length} item{list.length !== 1 ? "s" : ""}</span>
                </div>
                <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                  {list.map((ex, i) => (
                    <li key={i} style={{
                      padding: "10px 12px", borderRadius: 8,
                      background: C.bg, border: `1px solid ${C.borderLight}`,
                      marginBottom: 6,
                      display: "flex", alignItems: "center", gap: 12,
                    }}>
                      <Icon n={ex.icon || "warning"} s={16} c={tone === "danger" ? C.danger : tone === "warn" ? C.amber : C.brand} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>{ex.title}</p>
                        {ex.body && <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0", lineHeight: 1.5 }}>{ex.body}</p>}
                      </div>
                      {ex.action && (
                        <Button size="sm" variant="secondary" onClick={() => onResolve?.(ex)}>{ex.action}</Button>
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

// ═════════════════════════════════════════════════════════════════════════════
// CALENDAR / COVERAGE HEATMAP
// ═════════════════════════════════════════════════════════════════════════════

const CalendarView = ({ records, employeesById, onDayClick }) => {
  const C = useC();
  const [cursor, setCursor] = useState(() => startOfMonth(new Date()));
  const [mode, setMode]     = useState("month");  // month | week | heatmap

  const days = useMemo(() => {
    const start = startOfMonth(cursor);
    const end   = endOfMonth(cursor);
    const offset = (start.getDay() + 6) % 7; // Monday-first
    const cells = [];
    for (let i = 0; i < offset; i++) cells.push(null);
    for (let d = 1; d <= end.getDate(); d++) {
      cells.push(new Date(cursor.getFullYear(), cursor.getMonth(), d));
    }
    return cells;
  }, [cursor]);

  const dayBuckets = useMemo(() => {
    const buckets = {};
    (records || []).forEach(r => {
      if (r.status === "rejected" || r.status === "cancelled") return;
      const start = new Date(r.from_date);
      const end   = new Date(r.to_date);
      let cur = new Date(start.getFullYear(), start.getMonth(), start.getDate());
      const last = new Date(end.getFullYear(), end.getMonth(), end.getDate());
      while (cur <= last) {
        const k = dayKey(cur);
        if (!buckets[k]) buckets[k] = { approved: [], pending: [] };
        const target = (r.status === "approved" || r.status === "locked_for_payroll") ? "approved" : "pending";
        buckets[k][target].push(r);
        cur = addDays(cur, 1);
      }
    });
    return buckets;
  }, [records]);

  const monthLabel = cursor.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  const weekdays = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];

  // Density tones for heatmap based on combined leave count.
  const densityColor = (count) => {
    if (!count)        return { bg: C.bg,        fg: C.textLight };
    if (count <= 1)    return { bg: C.brandBg,   fg: C.brand     };
    if (count <= 3)    return { bg: C.amberBg,   fg: C.amber     };
    return                  { bg: C.dangerBg,  fg: C.danger    };
  };

  return (
    <Card title="Leave calendar and coverage" icon="calendar"
          subtitle="Absence density and coverage status across days. Click a day for full record details."
          padding={0}
          action={
            <div style={{ display: "flex", gap: 6 }}>
              <Button size="sm" variant="secondary" icon="chevleft"
                      onClick={() => setCursor(d => new Date(d.getFullYear(), d.getMonth() - 1, 1))} />
              <Button size="sm" variant="secondary" onClick={() => setCursor(startOfMonth(new Date()))}>Today</Button>
              <Button size="sm" variant="secondary" icon="chevright"
                      onClick={() => setCursor(d => new Date(d.getFullYear(), d.getMonth() + 1, 1))} />
              <Select value={mode} onChange={setMode} options={[
                { value: "month",   label: "Month" },
                { value: "heatmap", label: "Heatmap" },
              ]} style={{ minHeight: 32, padding: "5px 10px", fontSize: 12 }} />
            </div>
          }>
      <div style={{ padding: "12px 20px 6px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>{monthLabel}</p>
        <div style={{ display: "flex", gap: 8, fontSize: 11, color: C.textSub }}>
          <span><span style={{ display:"inline-block", width:10, height:10, borderRadius:2, background:C.brandBg, marginRight:4 }} /> Light</span>
          <span><span style={{ display:"inline-block", width:10, height:10, borderRadius:2, background:C.amberBg, marginRight:4 }} /> Moderate</span>
          <span><span style={{ display:"inline-block", width:10, height:10, borderRadius:2, background:C.dangerBg, marginRight:4 }} /> Heavy</span>
        </div>
      </div>
      <div style={{ padding: "0 20px 20px" }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 4, marginBottom: 6 }}>
          {weekdays.map(d => (
            <div key={d} style={{ fontSize: 10, fontWeight: 700, color: C.textLight,
                                  textTransform: "uppercase", letterSpacing: 0.5, textAlign: "center", padding: 4 }}>
              {d}
            </div>
          ))}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 4 }}>
          {days.map((d, i) => {
            if (!d) return <div key={i} />;
            const k = dayKey(d);
            const b = dayBuckets[k] || { approved: [], pending: [] };
            const total = b.approved.length + b.pending.length;
            const isToday = dayKey(new Date()) === k;
            const tones = densityColor(total);
            return (
              <button key={i} type="button" onClick={() => onDayClick?.(k, b)}
                style={{
                  minHeight: mode === "heatmap" ? 56 : 72,
                  padding: 6, borderRadius: 6,
                  background: tones.bg,
                  border: `1px solid ${isToday ? C.brand : C.borderLight}`,
                  cursor: "pointer", fontFamily: "inherit", textAlign: "left",
                  display: "flex", flexDirection: "column", gap: 4,
                  transition: "transform .15s",
                }}
                onMouseEnter={(e) => { e.currentTarget.style.transform = "scale(1.02)"; }}
                onMouseLeave={(e) => { e.currentTarget.style.transform = "scale(1)"; }}>
                <span style={{ fontSize: 11, fontWeight: isToday ? 800 : 600,
                               color: isToday ? C.brand : C.text }}>
                  {d.getDate()}
                </span>
                {mode !== "heatmap" && total > 0 && (
                  <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                    {b.approved.length > 0 && (
                      <span style={{ fontSize: 10, fontWeight: 700, color: C.green,
                                     background: "rgba(255,255,255,0.7)", padding: "1px 6px", borderRadius: 4 }}>
                        ✓ {b.approved.length}
                      </span>
                    )}
                    {b.pending.length > 0 && (
                      <span style={{ fontSize: 10, fontWeight: 700, color: C.amber,
                                     background: "rgba(255,255,255,0.7)", padding: "1px 6px", borderRadius: 4 }}>
                        ⏳ {b.pending.length}
                      </span>
                    )}
                  </div>
                )}
                {mode === "heatmap" && total > 0 && (
                  <span style={{ fontSize: 14, fontWeight: 800, color: tones.fg, marginTop: "auto" }}>
                    {total}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>
    </Card>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PENDING QUEUE
// ═════════════════════════════════════════════════════════════════════════════

const PendingQueue = ({ records, onOpen, onNudge, sensitiveOK }) => {
  const C = useC();
  const queues = useMemo(() => {
    const now = Date.now();
    const ageDays = (iso) => iso ? Math.floor((now - new Date(iso).getTime()) / 86400000) : 0;
    const pending = (records || []).filter(r =>
      r.status === "submitted" || r.status === "under_review"
    );
    const needManager  = pending.filter(r => r.status === "submitted").sort((a,b) => ageDays(b.created_at) - ageDays(a.created_at));
    const needHR       = pending.filter(r => r.status === "under_review" || r.hr_flag).sort((a,b) => ageDays(b.created_at) - ageDays(a.created_at));
    const needInfo     = pending.filter(r => r.doc_status === "pending");
    const cutoffSoon   = pending.filter(r => {
      const days = ageDays(r.from_date);
      return days < 0 && days > -3; // starts in 0-3 days
    });
    return { needManager, needHR, needInfo, cutoffSoon };
  }, [records]);

  const Section = ({ title, list, action }) => (
    <div style={{ marginBottom: 12 }}>
      <p style={{ fontSize: 11, fontWeight: 700, color: C.textLight, letterSpacing: 0.5,
                  textTransform: "uppercase", margin: "0 0 6px" }}>
        {title} <span style={{ color: C.textSub, fontWeight: 500 }}>· {list.length}</span>
      </p>
      {list.length === 0 ? (
        <p style={{ fontSize: 12, color: C.textLight, margin: "0 0 10px", padding: "8px 10px",
                    background: C.bg, borderRadius: 6 }}>
          No items in this queue.
        </p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {list.slice(0, 5).map(r => (
            <li key={r.id} style={{
              padding: "10px 12px", borderRadius: 8, background: C.bg,
              border: `1px solid ${C.borderLight}`, marginBottom: 6,
              display: "flex", alignItems: "center", gap: 10,
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0,
                            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {r.name || `User ${r.user_id}`} · {SENSITIVE_TYPES.has(r.leave_type) && !sensitiveOK ? "Protected Leave" : r.leave_type}
                </p>
                <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                  {fmtDateShort(r.from_date)} – {fmtDateShort(r.to_date)} · {r.days || r.leave_days || 1} day(s) · submitted {fmtRel(r.created_at)}
                </p>
              </div>
              <Button size="sm" variant="secondary" onClick={() => onOpen?.(r)}>{action || "Review"}</Button>
            </li>
          ))}
          {list.length > 5 && (
            <li style={{ padding: "6px 10px", fontSize: 11, color: C.textSub }}>
              + {list.length - 5} more — see records table
            </li>
          )}
        </ul>
      )}
    </div>
  );

  return (
    <Card title="Pending and approval queue" icon="clock"
          subtitle="Routed by ownership. Each item has one obvious owner and one next-best action.">
      <Section title="Needs manager review"   list={queues.needManager} action="Review" />
      <Section title="Needs HR review"        list={queues.needHR}      action="Open HR review" />
      <Section title="Needs worker information" list={queues.needInfo}  action="Request info" />
      <Section title="Approaching cutoff"     list={queues.cutoffSoon}  action="Resolve" />
    </Card>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// LEAVE RECORDS TABLE
// ═════════════════════════════════════════════════════════════════════════════

const RecordsTable = ({ records, onOpen, onExport, sensitiveOK }) => {
  const C = useC();
  const [search, setSearch] = useState("");
  const [sortBy, setSortBy] = useState("created_at");
  const [sortDir, setSortDir] = useState("desc");

  const visible = useMemo(() => {
    let rows = records || [];
    if (search.trim()) {
      const q = search.toLowerCase();
      rows = rows.filter(r =>
        (r.name || "").toLowerCase().includes(q) ||
        (r.email || "").toLowerCase().includes(q) ||
        (r.leave_type || "").toLowerCase().includes(q) ||
        (r.department || "").toLowerCase().includes(q)
      );
    }
    rows = [...rows].sort((a, b) => {
      const av = a[sortBy] ?? ""; const bv = b[sortBy] ?? "";
      if (av < bv) return sortDir === "asc" ? -1 : 1;
      if (av > bv) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
    return rows;
  }, [records, search, sortBy, sortDir]);

  const flipSort = (key) => {
    if (sortBy === key) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortBy(key); setSortDir("asc"); }
  };

  const Header = ({ k, children }) => (
    <th onClick={() => flipSort(k)} style={{
      textAlign: "left", padding: "10px 14px",
      fontSize: 11, fontWeight: 700, color: C.textLight,
      textTransform: "uppercase", letterSpacing: 0.4,
      borderBottom: `1px solid ${C.borderLight}`, cursor: "pointer", whiteSpace: "nowrap",
    }}>
      {children} {sortBy === k && (sortDir === "asc" ? "▲" : "▼")}
    </th>
  );

  return (
    <Card title={`Leave records (${visible.length})`} icon="filetext" padding={0}
          subtitle="Role-scoped columns. Sensitive fields are redacted by default and audited on access."
          action={
            <div style={{ display: "flex", gap: 6 }}>
              <Button size="sm" variant="secondary" icon="download" onClick={onExport}>Export</Button>
            </div>
          }>
      {/* Search */}
      <div style={{ padding: "12px 20px", borderBottom: `1px solid ${C.borderLight}` }}>
        <div style={{
          display: "flex", alignItems: "center", gap: 8,
          padding: "0 10px", borderRadius: 8,
          border: `1px solid ${C.border}`, background: C.bg,
        }}>
          <Icon n="search" s={14} c={C.textSub} />
          <input type="search" value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by name, email, leave type, or department…"
            style={{
              flex: 1, padding: "9px 0", border: "none", outline: "none",
              background: "transparent", fontSize: 13, color: C.text, fontFamily: "inherit",
            }} />
          {search && (
            <button type="button" onClick={() => setSearch("")}
              style={{ background:"none", border:"none", cursor:"pointer", padding: 4 }}>
              <Icon n="x" s={12} c={C.textSub} />
            </button>
          )}
        </div>
      </div>

      {visible.length === 0 ? (
        <EmptyState icon="filetext" title="No leave records match the selected filters."
          body="Adjust the date range, status filter, or team scope above to see records." />
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "inherit" }}>
            <thead>
              <tr style={{ background: C.bg }}>
                <Header k="name">Worker</Header>
                <Header k="leave_type">Leave type</Header>
                <Header k="from_date">Dates</Header>
                <Header k="leave_days">Days</Header>
                <Header k="status">Status</Header>
                <th style={{ padding:"10px 14px", fontSize:11, fontWeight:700, color:C.textLight,
                             textTransform:"uppercase", letterSpacing:0.4,
                             borderBottom:`1px solid ${C.borderLight}` }}>Coverage</th>
                <th style={{ padding:"10px 14px", fontSize:11, fontWeight:700, color:C.textLight,
                             textTransform:"uppercase", letterSpacing:0.4,
                             borderBottom:`1px solid ${C.borderLight}` }}>Policy</th>
                <th style={{ padding:"10px 14px", fontSize:11, fontWeight:700, color:C.textLight,
                             textTransform:"uppercase", letterSpacing:0.4,
                             borderBottom:`1px solid ${C.borderLight}` }}>Payroll</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {visible.slice(0, 100).map(r => {
                const st = STATUS_LABELS[r.status] || { label: r.status, tone: "neutral" };
                const isSensitive = SENSITIVE_TYPES.has(r.leave_type);
                const displayType = isSensitive && !sensitiveOK ? "Protected Leave" : r.leave_type;
                const policyStatus = r.is_lwp ? { label: "LWP", tone: "warn" }
                                  : r.hr_flag ? { label: "HR Review", tone: "warn" }
                                  : r.doc_status === "pending" ? { label: "Doc pending", tone: "warn" }
                                  : { label: "Clear", tone: "success" };
                const payrollState = r.status === "locked_for_payroll" ? { label: "Locked", tone: "purple" }
                                   : r.status === "approved" && !r.is_lwp ? { label: "Ready", tone: "success" }
                                   : r.is_lwp ? { label: "Unpaid", tone: "warn" }
                                   : { label: "—", tone: "neutral" };
                return (
                  <tr key={r.id} style={{ borderBottom: `1px solid ${C.borderLight}`, cursor: "pointer" }}
                      onClick={() => onOpen(r)}>
                    <td style={{ padding: "12px 14px", fontSize: 13 }}>
                      <p style={{ fontWeight: 700, color: C.text, margin: 0 }}>{r.name || `User ${r.user_id}`}</p>
                      <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>{r.department || r.team || "—"}</p>
                    </td>
                    <td style={{ padding: "12px 14px", fontSize: 13, color: C.text }}>
                      {displayType}
                      {isSensitive && !sensitiveOK && (
                        <span title="Sensitive details restricted by privacy policy" style={{ marginLeft: 6 }}>
                          <Icon n="lock" s={11} c={C.textLight} />
                        </span>
                      )}
                    </td>
                    <td style={{ padding: "12px 14px", fontSize: 12, color: C.textSub }}>
                      {fmtDateShort(r.from_date)} – {fmtDateShort(r.to_date)}
                    </td>
                    <td style={{ padding: "12px 14px", fontSize: 13, color: C.text, fontWeight: 600 }}>
                      {r.leave_days || r.days || 1}
                      {r.is_half_day && <span style={{ color: C.textLight, fontWeight: 400 }}> · ½</span>}
                    </td>
                    <td style={{ padding: "12px 14px" }}>
                      <Pill tone={st.tone}>{st.label}</Pill>
                    </td>
                    <td style={{ padding: "12px 14px" }}>
                      <Pill tone="neutral">—</Pill>
                    </td>
                    <td style={{ padding: "12px 14px" }}>
                      <Pill tone={policyStatus.tone}>{policyStatus.label}</Pill>
                    </td>
                    <td style={{ padding: "12px 14px" }}>
                      <Pill tone={payrollState.tone}>{payrollState.label}</Pill>
                    </td>
                    <td style={{ padding: "12px 14px" }}>
                      <Icon n="chevright" s={14} c={C.textLight} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {visible.length > 100 && (
            <div style={{ padding: "10px 20px", fontSize: 12, color: C.textSub,
                          borderTop: `1px solid ${C.borderLight}` }}>
              Showing 100 of {visible.length} records — refine filters or export to see the rest.
            </div>
          )}
        </div>
      )}
    </Card>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// PIVOT VIEWS — by team, by leave type, by payroll readiness
// ═════════════════════════════════════════════════════════════════════════════

const PivotsRow = ({ records }) => {
  const C = useC();
  const byTeam = useMemo(() => {
    const map = {};
    (records || []).forEach(r => {
      if (r.status === "rejected" || r.status === "cancelled") return;
      const k = r.department || r.team || "Unassigned";
      if (!map[k]) map[k] = { team: k, total: 0, approved: 0, pending: 0 };
      map[k].total += 1;
      if (r.status === "approved" || r.status === "locked_for_payroll") map[k].approved += 1;
      else map[k].pending += 1;
    });
    return Object.values(map).sort((a, b) => b.total - a.total).slice(0, 8);
  }, [records]);

  const byType = useMemo(() => {
    const map = {};
    (records || []).forEach(r => {
      if (r.status === "rejected" || r.status === "cancelled") return;
      const k = r.leave_type || "Other";
      if (!map[k]) map[k] = { type: k, count: 0, days: 0 };
      map[k].count += 1;
      map[k].days  += Number(r.leave_days || r.days || 1);
    });
    return Object.values(map).sort((a, b) => b.days - a.days).slice(0, 8);
  }, [records]);

  const byReadiness = useMemo(() => {
    let ready = 0, blocked = 0, lwp = 0, locked = 0, na = 0;
    (records || []).forEach(r => {
      if (r.status === "rejected" || r.status === "cancelled") return;
      if (r.status === "locked_for_payroll") locked += 1;
      else if (r.is_lwp) lwp += 1;
      else if (r.status === "approved") ready += 1;
      else if (r.hr_flag || r.doc_status === "pending") blocked += 1;
      else na += 1;
    });
    return { ready, blocked, lwp, locked, na };
  }, [records]);

  return (
    <div style={{
      display: "grid", gap: 12,
      gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
      marginBottom: 16,
    }}>
      <Card title="By team / department" icon="users" padding={16}>
        {byTeam.length === 0
          ? <p style={{ fontSize: 12, color: C.textSub, margin: 0 }}>No data in scope.</p>
          : <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {byTeam.map(t => (
                <li key={t.team} style={{ padding: "8px 0", borderBottom: `1px solid ${C.borderLight}` }}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                    <span style={{ fontSize: 13, fontWeight: 600, color: C.text }}>{t.team}</span>
                    <span style={{ fontSize: 12, color: C.textSub }}>{t.total} record{t.total !== 1 ? "s" : ""}</span>
                  </div>
                  <div style={{ display: "flex", gap: 4, height: 6, borderRadius: 3, overflow: "hidden", background: C.borderLight }}>
                    <div style={{ flex: t.approved, background: C.green }} />
                    <div style={{ flex: t.pending,  background: C.amber }} />
                  </div>
                </li>
              ))}
            </ul>}
      </Card>

      <Card title="By leave type" icon="filetext" padding={16}>
        {byType.length === 0
          ? <p style={{ fontSize: 12, color: C.textSub, margin: 0 }}>No data in scope.</p>
          : <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {byType.map(t => (
                <li key={t.type} style={{ padding: "8px 0", borderBottom: `1px solid ${C.borderLight}`,
                                          display: "flex", justifyContent: "space-between" }}>
                  <span style={{ fontSize: 13, color: C.text }}>{t.type}</span>
                  <span style={{ fontSize: 12, color: C.textSub }}>
                    <strong style={{ color: C.text }}>{t.days.toFixed(1)}</strong> days · {t.count} req
                  </span>
                </li>
              ))}
            </ul>}
      </Card>

      <Card title="Payroll readiness" icon="dollar" padding={16}>
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {[
            { label: "Ready",   count: byReadiness.ready,   tone: "success" },
            { label: "Locked",  count: byReadiness.locked,  tone: "purple"  },
            { label: "LWP",     count: byReadiness.lwp,     tone: "warn"    },
            { label: "Blocked", count: byReadiness.blocked, tone: "danger"  },
            { label: "N/A",     count: byReadiness.na,      tone: "neutral" },
          ].map(row => (
            <li key={row.label} style={{ padding: "8px 0", borderBottom: `1px solid ${C.borderLight}`,
                                         display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <Pill tone={row.tone}>{row.label}</Pill>
              <span style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{row.count}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// EMPLOYEE / RECORD SIDE PANEL
// ═════════════════════════════════════════════════════════════════════════════

const SidePanel = ({ open, record, onClose, sensitiveOK, onRequestSensitive }) => {
  const C = useC();
  if (!open || !record) return null;
  const r = record;
  const st = STATUS_LABELS[r.status] || { label: r.status, tone: "neutral" };
  const isSensitive = SENSITIVE_TYPES.has(r.leave_type);
  const displayType = isSensitive && !sensitiveOK ? "Protected Leave" : r.leave_type;
  const showDescription = sensitiveOK || !isSensitive;

  return (
    <div onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 9998,
        background: "rgba(16,24,40,0.45)",
        display: "flex", justifyContent: "flex-end",
      }}>
      <aside onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%", maxWidth: 540, height: "100%",
          background: C.surface, boxShadow: "-12px 0 40px rgba(16,24,40,0.18)",
          display: "flex", flexDirection: "column",
          fontFamily: "'DM Sans', system-ui, sans-serif",
        }}>
        <header style={{
          padding: "18px 22px", borderBottom: `1px solid ${C.borderLight}`,
          display: "flex", alignItems: "center", gap: 10,
        }}>
          <Icon n="user" s={18} c={C.brand} />
          <h3 style={{ flex: 1, fontSize: 15, fontWeight: 800, color: C.text, margin: 0 }}>
            {r.name || `Worker ${r.user_id}`}
          </h3>
          <button onClick={onClose} aria-label="Close"
            style={{ width: 28, height: 28, borderRadius: 6, background: C.borderLight,
                     border: "none", cursor: "pointer",
                     display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Icon n="x" s={14} c={C.textSub} />
          </button>
        </header>

        <div style={{ flex: 1, overflowY: "auto", padding: 22 }}>
          {/* Worker summary */}
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 6px" }}>
            Worker summary
          </p>
          <div style={{
            padding: "12px 14px", borderRadius: 8, background: C.bg,
            border: `1px solid ${C.borderLight}`, marginBottom: 18,
          }}>
            <p style={{ fontSize: 13, color: C.text, margin: 0, fontWeight: 700 }}>{r.name || "—"}</p>
            <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}>
              {r.email || "—"} · {r.department || r.team || "—"}
            </p>
          </div>

          {/* Leave request details */}
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 6px" }}>
            Leave request
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 18 }}>
            <Field label="Type" value={displayType} C={C} />
            <Field label="Status" value={<Pill tone={st.tone}>{st.label}</Pill>} C={C} />
            <Field label="From" value={fmtDate(r.from_date)} C={C} />
            <Field label="To"   value={fmtDate(r.to_date)} C={C} />
            <Field label="Days" value={`${r.leave_days || r.days || 1}${r.is_half_day ? " (½ day)" : ""}`} C={C} />
            <Field label="Submitted" value={fmtRel(r.created_at)} C={C} />
          </div>

          {/* Sensitive description */}
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 6px" }}>
            Worker description
          </p>
          {showDescription ? (
            <div style={{
              padding: "12px 14px", borderRadius: 8, background: C.bg,
              border: `1px solid ${C.borderLight}`, marginBottom: 18,
              fontSize: 13, color: C.text, lineHeight: 1.6, whiteSpace: "pre-wrap",
            }}>
              {r.description || r.reason || "—"}
            </div>
          ) : (
            <div style={{
              padding: "12px 14px", borderRadius: 8, background: C.amberBg,
              border: `1px solid ${C.amber}33`, marginBottom: 18,
              display: "flex", gap: 10, alignItems: "flex-start",
            }}>
              <Icon n="lock" s={16} c={C.amber} />
              <div style={{ flex: 1, fontSize: 12, color: C.amber, lineHeight: 1.5 }}>
                <strong>Sensitive details restricted.</strong> This record relates to a protected
                leave category. Access requires authorization and is logged to the compliance audit.
                <div style={{ marginTop: 8 }}>
                  <Button size="sm" variant="secondary" onClick={() => onRequestSensitive?.(r)}>
                    Request access
                  </Button>
                </div>
              </div>
            </div>
          )}

          {/* Policy explanation */}
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 6px" }}>
            Policy explanation
          </p>
          <div style={{
            padding: "12px 14px", borderRadius: 8, background: C.brandBg,
            border: `1px solid ${C.brandBg}`, marginBottom: 18,
            fontSize: 13, color: C.text, lineHeight: 1.6,
          }}>
            {r.is_lwp
              ? "This record was auto-converted to Unpaid Leave because the requested days exceeded the worker's available balance, or because required documentation is missing."
              : r.hr_flag
              ? "Flagged for HR review. Manager approval is not sufficient — HR must validate this request."
              : r.doc_status === "pending"
              ? "Required supporting document has not been uploaded. Review request will resume after the worker provides it."
              : r.status === "approved"
              ? "Approved per policy. No outstanding actions."
              : "Operational review only — no policy exception detected."}
          </div>

          {/* Coverage & payroll */}
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 6px" }}>
            Coverage and payroll
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 18 }}>
            <Field label="Coverage impact" value="To be computed by scheduling module" C={C} />
            <Field label="Payroll state"
                   value={r.status === "locked_for_payroll" ? "Locked" : r.is_lwp ? "Unpaid (LWP)" : r.status === "approved" ? "Ready" : "—"}
                   C={C} />
            <Field label="Doc status" value={r.doc_status || "—"} C={C} />
            <Field label="HR flag"   value={r.hr_flag ? "Yes" : "No"} C={C} />
          </div>

          {/* Audit timeline */}
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 6px" }}>
            Timeline
          </p>
          <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: 12, color: C.textSub }}>
            <li style={{ padding: "6px 0" }}>Submitted {fmtRel(r.created_at)}</li>
            {r.reviewed_at && <li style={{ padding: "6px 0" }}>Reviewed {fmtRel(r.reviewed_at)}</li>}
            {r.review_comment && <li style={{ padding: "6px 0", color: C.text }}>Note: "{r.review_comment}"</li>}
          </ul>
        </div>
      </aside>
    </div>
  );
};

const Field = ({ label, value, C }) => (
  <div style={{
    padding: 10, borderRadius: 6, background: C.bg,
    border: `1px solid ${C.borderLight}`,
  }}>
    <p style={{ fontSize: 10, color: C.textLight, fontWeight: 700,
                textTransform: "uppercase", letterSpacing: 0.4, margin: 0 }}>{label}</p>
    <p style={{ fontSize: 13, color: C.text, fontWeight: 600, margin: "3px 0 0" }}>{value}</p>
  </div>
);

// ═════════════════════════════════════════════════════════════════════════════
// MAIN MODULE
// ═════════════════════════════════════════════════════════════════════════════

export default function LeaveSummaryModule({ token, currentUserRole = "admin" }) {
  const { C } = useTheme();
  const [loading, setLoading] = useState(true);
  const [records, setRecords] = useState([]);
  const [employees, setEmployees] = useState([]);
  const [types, setTypes]         = useState([]);
  const [error, setError]         = useState(null);
  const [drawerRow, setDrawerRow] = useState(null);
  const [toast, setToast]         = useState(null);
  // Calendar → table drill-down: when set to a "YYYY-MM-DD" key, the records
  // table shows only leaves that cover that day (clicking a calendar cell).
  const [calDay, setCalDay]       = useState(null);
  const recordsTableRef           = useRef(null);
  const risksRef                  = useRef(null);
  const calendarRef               = useRef(null);
  const queueRef                  = useRef(null);
  const payrollRef                = useRef(null);

  // Filters — defaults per spec §6.
  const [filters, setFilters] = useState({
    range: "month",
    team: "all",
    location: "all",
    leaveType: "all",
    status: "active",
    payroll: "all",
  });

  const role = (currentUserRole || "").toLowerCase();
  const sensitiveOK = ["owner", "hr", "hr_admin"].includes(role);
  const canConfigure = ["owner", "admin", "hr"].includes(role);

  const showToast = (msg, tone = "success") => {
    setToast({ msg, tone });
    setTimeout(() => setToast(null), 3500);
  };

  const apiCall = useCallback(async (path) => {
    const tok = token || TOKEN();
    if (!tok) throw new Error("Not authenticated");
    const res = await fetch(`${API_BASE}${path}`, { headers: { Authorization: `Bearer ${tok}` } });
    if (!res.ok) throw new Error(`Error ${res.status}`);
    return res.json();
  }, [token]);

  const loadAll = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [recs, emps, tys] = await Promise.allSettled([
        apiCall("/leave/requests"),
        // Lightweight directory (id/name/department/team). The Leave Summary
        // only needs these fields — using the heavy /employees/team endpoint
        // here recomputed live clock/break/idle status on every load and was
        // the main cause of the slow page load.
        apiCall("/employees"),
        apiCall("/leave/types"),
      ]);
      if (recs.status === "fulfilled") setRecords(Array.isArray(recs.value) ? recs.value : []);
      if (emps.status === "fulfilled") {
        const list = Array.isArray(emps.value) ? emps.value
                   : Array.isArray(emps.value?.employees) ? emps.value.employees
                   : [];
        setEmployees(list);
      }
      if (tys.status === "fulfilled") setTypes(Array.isArray(tys.value) ? tys.value : []);
    } catch (e) {
      setError(e.message || "Failed to load leave data");
    } finally {
      setLoading(false);
    }
  }, [apiCall]);

  useEffect(() => { loadAll(); }, [loadAll]);

  // Derive filter dropdown options from the current data.
  const teamOptions = useMemo(() => {
    // Dedupe on the canonical team key (case/whitespace/connector/word-order
    // insensitive) so casing variants ("AI Development" vs "Ai Development"),
    // the 3 spellings of "Technology Infrastructure", AND reversed names
    // ("Creative & Design" vs "Design and Creative") all collapse to a single
    // entry. Keyed by normTeam(); the first-seen spelling is the display label.
    const byKey = new Map();  // normKey → display label
    const add = (v) => {
      const label = (v == null ? "" : String(v)).trim();
      if (!label) return;
      const key = normTeam(label);
      if (!key) return;
      if (!byKey.has(key)) byKey.set(key, label);
    };
    employees.forEach(e => { add(e.department); add(e.team); });
    records.forEach(r => { add(r.department); add(r.team); });
    const opts = [...byKey.entries()]
      .sort((a, b) => a[1].localeCompare(b[1]))
      .map(([key, label]) => ({ value: key, label }));
    return [{ value: "all", label: "All teams" }, ...opts];
  }, [employees, records]);

  const locationOptions = useMemo(() => {
    const set = new Set();
    employees.forEach(e => { if (e.location) set.add(e.location); });
    return [{ value: "all", label: "All locations" }, ...[...set].sort().map(l => ({ value: l, label: l }))];
  }, [employees]);

  // Index employees by id once, so joining a leave record to its employee is
  // an O(1) Map lookup instead of an employees.find() linear scan repeated for
  // every record on every filter/keystroke change.
  const employeesById = useMemo(() => {
    const m = new Map();
    employees.forEach(e => m.set(e.id, e));
    return m;
  }, [employees]);

  const typeOptions = useMemo(() => {
    const set = new Set();
    types.forEach(t => { if (t.leave_type) set.add(t.leave_type); });
    records.forEach(r => { if (r.leave_type) set.add(r.leave_type); });
    return [{ value: "all", label: "All types" }, ...[...set].sort().map(t => ({ value: t, label: t }))];
  }, [types, records]);

  // Inject derived options into filters object so ModuleHeader can read them.
  const filtersWithOptions = { ...filters,
    _teamOptions: teamOptions, _locationOptions: locationOptions, _typeOptions: typeOptions,
  };

  // Date range → from/to keys.
  const { fromKey, toKey } = useMemo(() => {
    const today = new Date();
    let from = today, to = today;
    switch (filters.range) {
      case "today":      from = today;                                                to = today; break;
      case "week":       from = addDays(today, -((today.getDay() + 6) % 7));          to = addDays(from, 6); break;
      case "pay_period": from = new Date(today.getFullYear(), today.getMonth(), 1);   to = endOfMonth(today); break;
      case "month":      from = startOfMonth(today);                                  to = endOfMonth(today); break;
      case "quarter": {
        const q = Math.floor(today.getMonth() / 3);
        from = new Date(today.getFullYear(), q * 3, 1);
        to   = new Date(today.getFullYear(), q * 3 + 3, 0);
        break;
      }
      case "year":       from = new Date(today.getFullYear(), 0, 1);                  to = new Date(today.getFullYear(), 11, 31); break;
      default:           from = startOfMonth(today);                                  to = endOfMonth(today);
    }
    return { fromKey: dayKey(from), toKey: dayKey(to) };
  }, [filters.range]);

  // Filtered records — applies all scope filters before everything else.
  const filteredRecords = useMemo(() => {
    return (records || []).filter(r => {
      if (!inRange(r, fromKey, toKey)) return false;
      // Team option values are the canonical team key (see normTeam) so every
      // split spelling of a team — casing AND reversed names — matches the one
      // dropdown entry, and a leave record saved under either spelling is found.
      if (filters.team !== "all" && normTeam(r.department || r.team) !== filters.team) return false;
      if (filters.leaveType !== "all" && r.leave_type !== filters.leaveType) return false;
      if (filters.status === "active") {
        if (r.status === "rejected" || r.status === "cancelled") return false;
      } else if (filters.status !== "all" && r.status !== filters.status) {
        return false;
      }
      if (filters.location !== "all") {
        const emp = employeesById.get(r.user_id);
        if (!emp || emp.location !== filters.location) return false;
      }
      if (filters.payroll !== "all") {
        const isReady   = r.status === "approved" && !r.is_lwp;
        const isBlocked = r.hr_flag || r.doc_status === "pending";
        const isLocked  = r.status === "locked_for_payroll";
        if (filters.payroll === "ready"        && !isReady)   return false;
        if (filters.payroll === "blocked"      && !isBlocked) return false;
        if (filters.payroll === "missing_code" && !r.is_lwp)  return false;
      }
      return true;
    });
  }, [records, filters, fromKey, toKey, employeesById]);

  // Records shown in the table. When a calendar day is selected, narrow to
  // leaves that cover that day (from_date ≤ day ≤ to_date); otherwise show the
  // full filtered set.
  const tableRecords = useMemo(() => {
    if (!calDay) return filteredRecords;
    return filteredRecords.filter(r => {
      const from = dayKey(r.from_date);
      const to   = dayKey(r.to_date) || from;
      return from && from <= calDay && calDay <= to;
    });
  }, [filteredRecords, calDay]);

  // Metrics for the executive strip.
  const metrics = useMemo(() => {
    const today = dayKey(new Date());
    const onLeaveTodayList = filteredRecords.filter(r =>
      (r.status === "approved" || r.status === "locked_for_payroll") && overlapsDay(r, new Date())
    );
    const upcoming7 = filteredRecords.filter(r => {
      if (r.status !== "approved" && r.status !== "submitted" && r.status !== "under_review") return false;
      const fk = dayKey(r.from_date);
      const fromDate = new Date();
      const toDate = addDays(new Date(), 7);
      return fk >= dayKey(fromDate) && fk <= dayKey(toDate);
    });
    const pending = filteredRecords.filter(r => r.status === "submitted" || r.status === "under_review");
    const pendingAging = pending.filter(r => {
      const days = (Date.now() - new Date(r.created_at).getTime()) / 86400000;
      return days > 2;
    }).length;

    // Coverage risk — naive: any team where >= 30% of its members are on leave today.
    const teamMembersToday = {};
    employees.forEach(e => {
      const k = e.department || e.team || "Unassigned";
      if (!teamMembersToday[k]) teamMembersToday[k] = { total: 0, off: 0 };
      teamMembersToday[k].total += 1;
    });
    onLeaveTodayList.forEach(r => {
      const k = r.department || r.team || "Unassigned";
      if (!teamMembersToday[k]) teamMembersToday[k] = { total: 0, off: 0 };
      teamMembersToday[k].off += 1;
    });
    const coverageRiskTeamsList = Object.entries(teamMembersToday)
      .filter(([_, v]) => v.total > 0 && v.off / v.total >= 0.3 && v.off >= 2)
      .map(([k]) => k);
    const coverageRisk = coverageRiskTeamsList.length;

    // Payroll blockers — approved but is_lwp, or doc_status pending, or hr_flag.
    const payrollBlockers = filteredRecords.filter(r =>
      (r.status === "approved" && (r.hr_flag || r.doc_status === "pending")) || r.is_lwp
    ).length;

    return {
      onLeaveToday: onLeaveTodayList.length,
      onLeaveTeams: new Set(onLeaveTodayList.map(r => r.department || r.team || "Unassigned")).size,
      upcoming7d: upcoming7.length,
      upcomingTeams: new Set(upcoming7.map(r => r.department || r.team || "Unassigned")).size,
      pending: pending.length,
      pendingAging,
      coverageRisk,
      coverageRiskTeams: coverageRisk,
      coverageRiskTeamsList,
      payrollBlockers,
    };
  }, [filteredRecords, employees]);

  // Risks/exceptions for the panel.
  const exceptions = useMemo(() => {
    const out = [];
    if (metrics.coverageRiskTeamsList?.length) {
      out.push({
        severity: "urgent",
        icon: "warning",
        title: `Coverage risk on ${metrics.coverageRiskTeamsList.length} team(s)`,
        body:  `Teams with ≥30% of members on leave today: ${metrics.coverageRiskTeamsList.join(", ")}.`,
      });
    }
    if (metrics.pendingAging > 0) {
      out.push({
        severity: "attention",
        icon: "clock",
        title: `${metrics.pendingAging} request(s) waiting > 2 days`,
        body: "Manager review SLA exceeded. Nudge or reassign to prevent blocking workers.",
      });
    }
    const docPending = filteredRecords.filter(r => r.doc_status === "pending").length;
    if (docPending > 0) {
      out.push({
        severity: "attention",
        icon: "filetext",
        title: `${docPending} record(s) awaiting documentation`,
        body: "Worker has not provided required evidence yet. Review will resume after upload.",
      });
    }
    const hrFlagged = filteredRecords.filter(r => r.hr_flag).length;
    if (hrFlagged > 0) {
      out.push({
        severity: "attention",
        icon: "flag",
        title: `${hrFlagged} record(s) flagged for HR review`,
        body: "Manager approval is not sufficient — HR must validate before payroll.",
      });
    }
    const lwpCount = filteredRecords.filter(r => r.is_lwp).length;
    if (lwpCount > 0) {
      out.push({
        severity: "info",
        icon: "info",
        title: `${lwpCount} record(s) auto-converted to Unpaid Leave`,
        body: "Balance was insufficient or required documentation missing. Confirm pay treatment is correct.",
      });
    }
    return out;
  }, [metrics, filteredRecords]);

  const onExport = async () => {
    if (!filteredRecords.length) {
      showToast("Nothing to export in current scope", "error");
      return;
    }
    try {
    await downloadXLSX(`leave-summary-${dayKey(new Date())}.xlsx`,
      filteredRecords.map(r => ({
        worker:      r.name || `User ${r.user_id}`,
        email:       sensitiveOK ? r.email : "—",
        department:  r.department || r.team || "—",
        leave_type:  SENSITIVE_TYPES.has(r.leave_type) && !sensitiveOK ? "Protected Leave" : r.leave_type,
        from_date:   dayKey(r.from_date),
        to_date:     dayKey(r.to_date),
        days:        r.leave_days || r.days || 1,
        status:      r.status,
        is_lwp:      r.is_lwp ? "yes" : "no",
        hr_flag:     r.hr_flag ? "yes" : "no",
        doc_status:  r.doc_status || "—",
        // Human-readable date+time instead of the raw ISO timestamp
        // (e.g. "08 Jul 2026, 07:28 AM"); falls back to "—" when missing.
        submitted:   r.created_at
          ? new Date(r.created_at).toLocaleString(undefined, {
              day: "2-digit", month: "short", year: "numeric",
              hour: "2-digit", minute: "2-digit",
            })
          : "—",
      })),
      [
        { key: "worker",     label: "Worker" },
        { key: "email",      label: "Email" },
        { key: "department", label: "Department" },
        { key: "leave_type", label: "Leave Type" },
        { key: "from_date",  label: "From" },
        { key: "to_date",    label: "To" },
        { key: "days",       label: "Days" },
        { key: "status",     label: "Status" },
        { key: "is_lwp",     label: "LWP" },
        { key: "hr_flag",    label: "HR Flag" },
        { key: "doc_status", label: "Doc Status" },
        { key: "submitted",  label: "Submitted At" },
      ]);
      showToast("Export generated");
    } catch (e) {
      showToast("Export failed — please try again", "error");
    }
  };

  const onConfigurePolicy = () => {
    if (!canConfigure) {
      showToast("Only Account Owner / Admin / HR can configure leave policy", "error");
      return;
    }
    showToast("Opening leave policy settings…", "info");
  };

  // Executive-strip cards are links: each metric card calls jumpToPane(<pane>)
  // and we smooth-scroll that section into view. Previously onJumpToPane was a
  // no-op stub, so the cards looked clickable but went nowhere.
  const jumpToPane = useCallback((pane) => {
    const ref = {
      table:    recordsTableRef,
      calendar: calendarRef,
      queue:    queueRef,
      risks:    risksRef,
      payroll:  payrollRef,
    }[pane];
    ref?.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  const scopeLabel = (() => {
    const labels = {
      today: "Today", week: "This week", pay_period: "Current pay period",
      month: "This month", quarter: "This quarter", year: "This year",
    };
    let s = labels[filters.range] || filters.range;
    if (filters.team !== "all") {
      // filters.team is the normalized key — show the human-cased label.
      const teamLabel = (teamOptions.find(o => o.value === filters.team) || {}).label || filters.team;
      s += ` · ${teamLabel}`;
    }
    if (filters.location !== "all") s += ` · ${filters.location}`;
    return s;
  })();

  if (loading) {
    return (
      <CCtx.Provider value={C}>
        <div style={{ maxWidth: 1280, margin: "0 auto", padding: "8px 4px 32px",
                      fontFamily: "'DM Sans', system-ui, sans-serif" }}>
          <Card><Spinner /></Card>
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
        <ModuleHeader
          filters={filtersWithOptions}
          setFilters={setFilters}
          onExport={onExport}
          onConfigurePolicy={onConfigurePolicy}
          onRefresh={loadAll}
          summary={metrics}
          scopeLabel={scopeLabel}
        />

        {error && (
          <Card>
            <EmptyState icon="warning" title="Couldn't load leave data"
              body={error}
              action={<Button icon="refresh" onClick={loadAll}>Retry</Button>} />
          </Card>
        )}

        {!error && (
          <>
            <MetricsStrip metrics={metrics} onJumpToPane={jumpToPane} />

            <div ref={risksRef}>
              <RiskPanels exceptions={exceptions} />
            </div>

            <div ref={calendarRef}>
              <CalendarView records={filteredRecords} employeesById={{}}
                            onDayClick={(k, b) => {
                              const all = [...b.approved, ...b.pending];
                              if (all.length === 0) return;
                              // Filter the records table to this day and bring it
                              // into view, so the admin doesn't hunt through the
                              // full list for that date's entries.
                              setCalDay(k);
                              requestAnimationFrame(() => {
                                recordsTableRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
                              });
                            }} />
            </div>

            <div ref={queueRef}>
              <PendingQueue records={filteredRecords}
                            onOpen={(r) => setDrawerRow(r)}
                            sensitiveOK={sensitiveOK} />
            </div>

            <div ref={payrollRef}>
              <PivotsRow records={filteredRecords} />
            </div>

            <div ref={recordsTableRef}>
              {calDay && (
                <div style={{
                  display: "flex", alignItems: "center", justifyContent: "space-between",
                  gap: 12, padding: "10px 14px", marginBottom: 10, borderRadius: 8,
                  background: C.brandBg, border: `1px solid ${C.brand}`,
                }}>
                  <span style={{ fontSize: 13, fontWeight: 600, color: C.brand }}>
                    Showing {tableRecords.length} record(s) for {calDay}
                  </span>
                  <Button size="sm" variant="secondary" icon="x" onClick={() => setCalDay(null)}>
                    Clear filter
                  </Button>
                </div>
              )}
              <RecordsTable records={tableRecords}
                            onOpen={(r) => setDrawerRow(r)}
                            onExport={onExport}
                            sensitiveOK={sensitiveOK} />
            </div>
          </>
        )}

        <SidePanel open={!!drawerRow} record={drawerRow}
                   onClose={() => setDrawerRow(null)}
                   sensitiveOK={sensitiveOK}
                   onRequestSensitive={() => showToast("Sensitive-access request logged. Compliance review required.", "info")} />

        {/* Toast */}
        {toast && (
          <div style={{
            position: "fixed", bottom: 20, right: 20, zIndex: 10000,
            padding: "10px 14px", borderRadius: 8,
            background: toast.tone === "error" ? C.dangerBg : toast.tone === "info" ? C.brandBg : C.greenBg,
            color: toast.tone === "error" ? C.danger : toast.tone === "info" ? C.brand : C.green,
            fontSize: 13, fontWeight: 600,
            border: `1px solid ${(toast.tone === "error" ? C.danger : toast.tone === "info" ? C.brand : C.green)}33`,
            boxShadow: "0 4px 12px rgba(16,24,40,0.12)", maxWidth: 360,
          }}>
            {toast.msg}
          </div>
        )}
      </div>
    </CCtx.Provider>
  );
}
