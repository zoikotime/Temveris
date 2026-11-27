/**
 * HelpModule.jsx — worker-facing Help page.
 *
 * Architecture (spec §4):
 *   1. Kairos panel        — feature-flagged; hidden cleanly when not deployed
 *   2. Search              — articles, policy summaries, own tickets
 *   3. Recommended for You — max 4, state-driven, never judgmental
 *   4. Categories          — 5 worker-mental-model groups
 *   5. Quick actions       — correction / report issue / pay question / data question
 *   6. Contact Support     — guided routing drawer (3 steps)
 *   7. My Tickets          — table + detail view + escalation
 *   8. Policy context      — inline on relevant articles
 *
 * v1 storage strategy: tickets, messages, escalations, diagnostic consent,
 * search events, and article views all write to AuditLog with dedicated
 * entity/action tags. No new DB tables. Frontend article catalog is static
 * to avoid CMS infra in v1; swap to /worker/help/articles when ready.
 *
 * Endpoints used (graceful 404 fallback so the page works pre-launch):
 *   GET   /worker/help/home
 *   GET   /worker/help/tickets
 *   POST  /worker/help/tickets
 *   POST  /worker/help/tickets/{id}/messages
 *   POST  /worker/help/tickets/{id}/escalate
 *   POST  /worker/help/diagnostic-consent
 *   POST  /worker/help/search-events
 *   POST  /worker/help/article-views
 */

import React, { useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import API from "../api/axios";
import { useTheme } from "../context/ThemeContext";

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

const fmtRel = (iso) => {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const diff = Math.max(0, Date.now() - t);
  const sec = Math.floor(diff / 1000);
  if (sec < 60)        return "just now";
  if (sec < 3600)      return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400)     return `${Math.floor(sec / 3600)}h ago`;
  if (sec < 86400 * 7) return `${Math.floor(sec / 86400)}d ago`;
  try { return new Date(iso).toLocaleDateString(); } catch { return "—"; }
};

// ─── inline icon set ────────────────────────────────────────────────────────
const ICON = {
  help:       "M12 22a10 10 0 100-20 10 10 0 000 20z M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3 M12 17h.01",
  search:     "M11 19a8 8 0 100-16 8 8 0 000 16z M21 21l-4.35-4.35",
  bot:        "M12 8V4H8 M12 8h4v4 M5 12h14v8H5z M8 16h.01 M16 16h.01 M9 4h6",
  zap:        "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
  clock:      "M12 22a10 10 0 100-20 10 10 0 000 20z M12 6v6l4 2",
  calendar:   "M8 2v4 M16 2v4 M3 10h18 M5 4h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V6a2 2 0 012-2z",
  leaf:       "M17 8C8 10 5.9 16.17 3.82 22 M9.5 9.5c1 2.5 3.5 5 6.5 5.5 M21 3C6 5 5 13 5 20",
  dollar:     "M12 1v22 M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6",
  shield:     "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  briefcase:  "M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2",
  check:      "M20 6L9 17l-5-5",
  x:          "M18 6L6 18 M6 6l12 12",
  arrow:      "M5 12h14 M12 5l7 7-7 7",
  send:       "M22 2L11 13 M22 2l-7 20-4-9-9-4 20-7z",
  doc:        "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8",
  user:       "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2 M12 11a4 4 0 100-8 4 4 0 000 8",
  warning:    "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
  info:       "M12 22a10 10 0 100-20 10 10 0 000 20z M12 16v-4 M12 8h.01",
  inbox:      "M22 12h-6l-2 3h-4l-2-3H2 M5.45 5.11L2 12v6a2 2 0 002 2h16a2 2 0 002-2v-6l-3.45-6.89A2 2 0 0016.76 4H7.24a2 2 0 00-1.79 1.11z",
  lock:       "M19 11H5a2 2 0 00-2 2v7a2 2 0 002 2h14a2 2 0 002-2v-7a2 2 0 00-2-2z M7 11V7a5 5 0 0110 0v4",
  refresh:    "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10 M1 14l4.64 4.36A9 9 0 0020.49 15",
  upload:     "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M17 8l-5-5-5 5 M12 3v12",
  external:   "M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6 M15 3h6v6 M10 14L21 3",
  chevright:  "M9 18l6-6-6-6",
  chevdown:   "M6 9l6 6 6-6",
  chevup:     "M18 15l-6-6-6 6",
  flag:       "M4 22V4 M4 4l7 0 1 2h7v8h-7l-1-2H4",
  star:       "M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z",
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

const Button = ({ children, variant = "primary", onClick, disabled, type = "button", style, icon }) => {
  const C = useC();
  const variants = {
    primary:   { bg: disabled ? C.textLight : C.brand, color: "#fff", border: "none" },
    secondary: { bg: C.surface, color: C.text, border: `1px solid ${C.border}` },
    danger:    { bg: disabled ? "#fbb" : C.danger, color: "#fff", border: "none" },
    ghost:     { bg: "transparent", color: C.brand, border: "none" },
  };
  const v = variants[variant] || variants.primary;
  return (
    <button type={type} onClick={onClick} disabled={disabled}
      style={{
        padding: "9px 16px", borderRadius: C.rSm,
        background: v.bg, color: v.color, border: v.border,
        fontWeight: 600, fontSize: 13, cursor: disabled ? "not-allowed" : "pointer",
        fontFamily: "inherit", whiteSpace: "nowrap",
        display: "inline-flex", alignItems: "center", gap: 6, minHeight: 44,
        boxShadow: variant === "primary" && !disabled ? "0 1px 2px rgba(17,73,106,0.25)" : "none",
        opacity: disabled ? 0.7 : 1,
        ...style,
      }}>
      {icon && <Icon n={icon} s={14} c={v.color} />}
      {children}
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
        outline: "none", boxSizing: "border-box", minHeight: 44,
      }}
      onFocus={(e) => { if (!disabled) { e.target.style.borderColor = C.brand; e.target.style.background = C.surface; } }}
      onBlur={(e)  => { e.target.style.borderColor = C.border; e.target.style.background = disabled ? C.borderLight : C.bg; }}
    />
  );
};

const Textarea = ({ value, onChange, placeholder, rows = 4, disabled }) => {
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
      {body && <p style={{ margin: "6px auto 0", maxWidth: 420 }}>{body}</p>}
      {action && <div style={{ marginTop: 14 }}>{action}</div>}
    </div>
  );
};

// Modal — overlay click closes; ESC closes; body scroll locked.
const Modal = ({ open, onClose, title, children, footer, size = "medium", fullScreenMobile = false }) => {
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
    <div onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 9999,
        background: "rgba(16,24,40,0.55)", backdropFilter: "blur(2px)",
        display: "flex", alignItems: fullScreenMobile ? "stretch" : "center",
        justifyContent: "center", padding: fullScreenMobile ? 0 : 20,
      }}>
      <div onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%", maxWidth: widths[size] || widths.medium,
          maxHeight: "92vh", display: "flex", flexDirection: "column",
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
              width: 32, height: 32, borderRadius: 6,
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
            display: "flex", justifyContent: "flex-end", gap: 8, background: C.bg, flexWrap: "wrap",
          }}>{footer}</footer>
        )}
      </div>
    </div>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// STATIC HELP CATALOG  (frontend v1; replace with /worker/help/articles later)
// ═════════════════════════════════════════════════════════════════════════════
const CATEGORIES = [
  { key: "getting_started", label: "Getting started", icon: "star",
    desc: "First-time setup, dashboard tour, basic actions, and mobile app basics." },
  { key: "my_day",          label: "My day at work", icon: "calendar",
    desc: "My Schedule, clock-in/out, breaks, missed entries, and activity timeline." },
  { key: "time_off",        label: "Time off", icon: "leaf",
    desc: "Leave requests, approvals, balances, holidays, and declined requests." },
  { key: "pay",             label: "Pay & documents", icon: "dollar",
    desc: "Payslips, payroll questions, tax documents, timesheets, and cutoff reminders." },
  { key: "data_privacy",    label: "My data & privacy", icon: "shield",
    desc: "What's recorded, who viewed your data, corrections, exports, and DSAR." },
];

// Articles are intentionally minimal v1 stubs. Title, snippet, category, read
// time and last_reviewed are required per spec §6.4. body shows on view.
const ARTICLES = [
  {
    slug: "starting-first-session", category: "getting_started",
    title: "How to start your first work session",
    snippet: "Open the dashboard, choose a task, and click Clock In. Your status changes to Working and tracking begins.",
    read_min: 2, last_reviewed: "2026-04-12",
    body: "When you start your first session, Temveris needs to know what you're working on. From the Today page, click Clock In and pick a task. Your sidebar status changes to Working and the timer starts. You can clock out at any time from the same button.",
    related_action: { label: "Open Today", tab: "my-work" },
  },
  {
    slug: "dashboard-tour", category: "getting_started",
    title: "Dashboard tour: where everything lives",
    snippet: "A quick map of the worker dashboard — Home, My Schedule, My Activity, My Pay, My Insights, and My Data.",
    read_min: 3, last_reviewed: "2026-04-10",
    body: "The left sidebar groups everything by what you do, not how the system is built. Home is your day. My Schedule is when. My Activity is what. My Pay is the money. My Insights is your patterns. My Data is what the system has on you. Account holds your profile, notifications, help, and settings.",
  },
  {
    slug: "missed-clock-in", category: "my_day",
    title: "What to do if you missed a clock-in",
    snippet: "If your shift started without a clock-in, the system flags it. You can request a correction from My Data → Corrections & Disputes.",
    read_min: 2, last_reviewed: "2026-04-15",
    body: "A missed clock-in shows up on your timeline as a gap. To fix it, go to My Data → Corrections & Disputes and submit a Time entry correction with the time you actually started. Your manager reviews it. If approved, your hours are restored.",
    related_action: { label: "Open Corrections", tab: "corrections" },
  },
  {
    slug: "breaks-explained", category: "my_day",
    title: "How breaks work",
    snippet: "Breaks pause your active timer. They're recorded separately from working time and don't count toward your daily target.",
    read_min: 2, last_reviewed: "2026-04-08",
    body: "Click Start Break to pause the active timer. Your status changes to On Break. Click Resume to continue. Break time is captured separately so your manager can see your actual working hours and your breaks.",
  },
  {
    slug: "schedule-changes", category: "my_day",
    title: "Why my schedule changed",
    snippet: "Schedule changes are made by your manager. Material changes (added/removed shifts, time changes) notify you immediately.",
    read_min: 2, last_reviewed: "2026-04-05",
    body: "Your manager owns your schedule. When they change it, you'll see a Schedule Changed notification. Material changes (a new shift, a removed shift, time changes) are sent immediately. Minor changes (color, label, manager note) are batched into a daily digest.",
    related_action: { label: "Open My Schedule", tab: "my-schedule" },
  },
  {
    slug: "request-time-off", category: "time_off",
    title: "How to request time off",
    snippet: "From My Time Off, click Request Leave, pick the dates, and submit. Your manager reviews it.",
    read_min: 2, last_reviewed: "2026-04-09",
    body: "Open My Time Off (Leave & Requests in the sidebar). Click Request Leave, select the leave type and dates, and submit. Your manager gets a notification and reviews. You'll be notified immediately when it's approved or declined. Declines always include a reason.",
    related_action: { label: "Request Leave", tab: "leave" },
  },
  {
    slug: "declined-time-off", category: "time_off",
    title: "Understanding declined time-off",
    snippet: "If your time-off was declined, the manager's reason is shown on the request. You can submit a new request or escalate.",
    read_min: 2, last_reviewed: "2026-04-11",
    body: "Every decline includes a reason — the system blocks silent declines. Read the reason on your request. If you disagree, you can submit a new request with different dates, or use Help → Contact Support to escalate to HR or your grievance contact.",
  },
  {
    slug: "reading-payslip", category: "pay",
    title: "Reading your payslip",
    snippet: "Your payslip breaks down gross pay, deductions, taxes, and net pay. Tax documents are filed separately under My Pay.",
    read_min: 4, last_reviewed: "2026-04-14",
    body: "Open My Pay → Payslips. Each payslip shows: gross pay (your earnings before deductions), deductions (taxes, benefits, other), and net pay (what hits your account). Tax documents (W-2, 1099, country equivalents) are under My Pay → Documents.",
  },
  {
    slug: "payroll-cutoff", category: "pay",
    title: "Payroll cutoff: what it is and what to do",
    snippet: "The cutoff is when payroll closes for the period. After cutoff, time corrections may not affect this period's pay.",
    read_min: 2, last_reviewed: "2026-04-07",
    body: "Your workspace has a payroll cutoff date and time. Before cutoff: any approved correction adjusts this period's pay. After cutoff: corrections still get made, but they roll into the next pay period. You'll get a 24-hour reminder before cutoff.",
  },
  {
    slug: "whats-recorded", category: "data_privacy",
    title: "What Temveris records about your work",
    snippet: "Your workspace decides what's captured: time, app usage, screenshots, location, keyboard, and mouse activity.",
    read_min: 4, last_reviewed: "2026-04-12",
    body: "Capture is workspace-controlled, not platform-controlled. Open My Data → What's Recorded to see exactly what's on for your account: time tracking, app/website usage, screenshots, location (if applicable), keyboard activity (presence only — never content), and mouse activity (presence only).",
    related_action: { label: "What's Recorded", tab: "whats-recorded" },
    related_policy_id: "workforce_monitoring",
  },
  {
    slug: "screenshots-explained", category: "data_privacy",
    title: "Why I can see screenshots in my activity",
    snippet: "Screenshots appear so you can see what's been recorded. Sensitive apps are blurred where workspace policy allows.",
    read_min: 3, last_reviewed: "2026-04-12",
    body: "If your workspace has screenshots enabled, you'll see them on your Screenshots page. The system blurs sensitive apps (password managers, banking, health apps) before storing them when blur rules are on. You can see exactly what was captured — there's nothing kept from you.",
    related_policy_id: "screenshot_policy",
  },
  {
    slug: "request-correction", category: "data_privacy",
    title: "How correction requests are reviewed",
    snippet: "Submit a correction from My Data → Corrections & Disputes. Time goes to your manager. Screenshots go to HR/privacy.",
    read_min: 3, last_reviewed: "2026-04-15",
    body: "Different correction types route to different reviewers: time entries → your manager; idle time → manager or ops; screenshots → HR/privacy; app classification → workspace admin. Status updates are immediate. Rejections always include a reason. You can escalate any rejection.",
    related_action: { label: "Open Corrections", tab: "corrections" },
  },
  {
    slug: "data-export", category: "data_privacy",
    title: "How to export your data",
    snippet: "From My Data → My Data Export, choose PDF/JSON/CSV and the records you want, then click Download.",
    read_min: 3, last_reviewed: "2026-04-13",
    body: "Standard export covers time records, activity, screenshots, correction requests, access log entries, privacy setting history, and policy acknowledgements. For a formal regulatory request, use Submit DSAR — the privacy team acknowledges within 72 hours and fulfills within 30 days for UK/EU regulatory settings.",
    related_action: { label: "My Data Export", tab: "data-export" },
  },
  {
    slug: "secure-account", category: "data_privacy",
    title: "Securing your Temveris account",
    snippet: "Use a unique password, enable MFA, review your active sessions, and check sign-in alerts.",
    read_min: 4, last_reviewed: "2026-04-16",
    body: "From Account → Profile → Security: change your password (this signs you out everywhere else), review active sessions and revoke any you don't recognize, and watch for new-device sign-in alerts. If a sign-in wasn't you, change your password immediately and contact Temveris support.",
    related_action: { label: "Open Profile", tab: "profile" },
  },
  {
    slug: "policy-acknowledgements", category: "data_privacy",
    title: "How policy acknowledgements work",
    snippet: "When workspace policy is updated, you may need to acknowledge that you've read it. Acknowledgement is not consent.",
    read_min: 2, last_reviewed: "2026-04-10",
    body: "Acknowledging a policy means you've read it. It doesn't constitute blanket consent — where consent is legally required for a specific form of processing, your workspace captures that separately. You can review and acknowledge from My Data → policy drawer, or from any article that links to a policy.",
  },
];

const TICKET_TOPICS = [
  { value: "clock_issue",   label: "Clock-in or clock-out issue",
    routesTo: "Temveris Support · Workspace admin (if policy)", sla: "1 business day" },
  { value: "schedule",      label: "Schedule question",
    routesTo: "Your direct manager", sla: "1 business day" },
  { value: "time_off",      label: "Time-off question",
    routesTo: "Your manager / HR", sla: "1 business day" },
  { value: "pay",           label: "Pay or payslip question",
    routesTo: "Workspace payroll contact", sla: "2 business days", sensitiveOptional: true },
  { value: "privacy",       label: "Privacy or data question",
    routesTo: "Workspace data contact", sla: "3 business days", sensitiveOptional: true },
  { value: "technical",     label: "Technical issue",
    routesTo: "Temveris Support", sla: "1 business day", needsDiagnostics: true },
  { value: "account",       label: "Account access issue",
    routesTo: "Temveris Support", sla: "1 business day" },
  { value: "grievance",     label: "Grievance / harassment / discrimination",
    routesTo: "Designated grievance contact", sla: "Confidential — workspace SLA",
    forceSensitive: true },
  { value: "other",         label: "Other",
    routesTo: "General support queue", sla: "2 business days" },
];

const TICKET_STATUS = {
  submitted:    { label: "Submitted",      tone: "info"   },
  in_review:    { label: "In review",      tone: "info"   },
  waiting_you:  { label: "Waiting for you", tone: "locked" },
  responded:    { label: "Responded",      tone: "purple" },
  resolved:     { label: "Resolved",       tone: "on"     },
  closed:       { label: "Closed",         tone: "neutral" },
  escalated:    { label: "Escalated",      tone: "danger" },
};

const ESCALATION_ELIGIBLE_TOPICS = new Set(["pay", "privacy", "technical", "time_off"]);

// ═════════════════════════════════════════════════════════════════════════════
// MAIN COMPONENT
// ═════════════════════════════════════════════════════════════════════════════
export default function HelpModule({ onJumpToTab }) {
  const { C } = useTheme();
  const [home, setHome] = useState({
    kairos_enabled: false,    // Phase 3 not deployed by default — panel hidden
    recommendations: [],      // server-driven; falls back to client logic
    workspace_policy_meta: null,
  });
  const [tickets, setTickets] = useState([]);
  const [ticketsLoading, setTicketsLoading] = useState(false);
  const [openArticle, setOpenArticle] = useState(null);   // article object
  const [openCategory, setOpenCategory] = useState(null); // category key
  const [contactOpen, setContactOpen] = useState(false);
  const [contactSeed, setContactSeed] = useState(null);   // { topic, subject } prefill
  const [ticketDetail, setTicketDetail] = useState(null);
  const [searchQ, setSearchQ] = useState("");
  const [searchActive, setSearchActive] = useState(false);

  // ── Load home + tickets on mount ─────────────────────────────────────────
  useEffect(() => {
    (async () => {
      try {
        const { data } = await API.get("/worker/help/home");
        if (data) setHome(prev => ({ ...prev, ...data }));
      } catch { /* keep defaults — Kairos hidden, no recs */ }
    })();
    loadTickets();
  }, []);

  const loadTickets = async () => {
    setTicketsLoading(true);
    try {
      const { data } = await API.get("/worker/help/tickets");
      setTickets(Array.isArray(data) ? data : []);
    } catch {
      setTickets([]);
    } finally {
      setTicketsLoading(false);
    }
  };

  // ── Search (client-side over the static catalog) ─────────────────────────
  const searchResults = useMemo(() => {
    const q = searchQ.trim().toLowerCase();
    if (!q) return [];
    const articles = ARTICLES.filter(a =>
      a.title.toLowerCase().includes(q) ||
      a.snippet.toLowerCase().includes(q) ||
      (a.body || "").toLowerCase().includes(q)
    );
    const ticketHits = tickets.filter(t =>
      (t.subject || "").toLowerCase().includes(q) ||
      (t.description || "").toLowerCase().includes(q)
    );
    // Rank: keep order from catalog (which is already loosely curated by category)
    return { articles, tickets: ticketHits };
  }, [searchQ, tickets]);

  const onSearchSubmit = async () => {
    if (!searchQ.trim()) return;
    // Best-effort log; never blocks the UI
    try {
      await API.post("/worker/help/search-events", {
        query: searchQ.trim(),
        result_count: (searchResults.articles?.length || 0) + (searchResults.tickets?.length || 0),
      });
    } catch { /* non-fatal */ }
  };

  // ── Recommended for You — client fallback when server has none ──────────
  const recs = useMemo(() => {
    if (home.recommendations?.length) return home.recommendations.slice(0, 4);
    // Fallback: simple client-side seeding
    const out = [];
    if (tickets.length === 0) {
      out.push({ slug: "starting-first-session", reason: "First time using Help" });
    }
    out.push({ slug: "missed-clock-in",    reason: "Common question" });
    out.push({ slug: "request-correction", reason: "Common question" });
    out.push({ slug: "secure-account",     reason: "Account safety" });
    return out.slice(0, 4);
  }, [home.recommendations, tickets.length]);

  // ── Article view → log + show ───────────────────────────────────────────
  const viewArticle = async (article) => {
    setOpenArticle(article);
    try {
      await API.post("/worker/help/article-views", {
        slug: article.slug,
        related_policy_id: article.related_policy_id || null,
      });
    } catch { /* non-fatal */ }
  };

  // ── Quick actions ────────────────────────────────────────────────────────
  const onQuickAction = (kind) => {
    if (kind === "correction") {
      onJumpToTab?.("corrections");
      return;
    }
    if (kind === "report")   setContactSeed({ topic: "technical" });
    if (kind === "pay")      setContactSeed({ topic: "pay" });
    if (kind === "data")     setContactSeed({ topic: "privacy" });
    setContactOpen(true);
  };

  // ── Ticket detail / escalation ──────────────────────────────────────────
  const reloadOneTicket = async (id) => {
    try {
      const { data } = await API.get(`/worker/help/tickets/${id}`);
      if (data) {
        setTicketDetail(data);
        setTickets(rows => rows.map(r => r.id === id ? { ...r, ...data } : r));
      }
    } catch { /* non-fatal */ }
  };

  // ─── Render ─────────────────────────────────────────────────────────────
  return (
    <CCtx.Provider value={C}>
    <div style={{
      maxWidth: 1100, margin: "0 auto", padding: "8px 4px 32px",
      fontFamily: "'DM Sans', system-ui, sans-serif",
    }}>
      <Header
        onContact={() => { setContactSeed(null); setContactOpen(true); }}
        onMyTickets={() => {
          // Smooth scroll to the tickets card (rendered below).
          document.getElementById("zt-help-tickets")?.scrollIntoView({ behavior: "smooth" });
        }}
      />

      {home.kairos_enabled && <KairosPanel />}

      <SearchBar
        value={searchQ}
        onChange={setSearchQ}
        onFocus={() => setSearchActive(true)}
        onBlur={() => setTimeout(() => setSearchActive(false), 150)}
        onSubmit={onSearchSubmit}
      />

      {searchQ.trim() && searchActive ? (
        <SearchResults
          q={searchQ}
          results={searchResults}
          onView={(a) => { setSearchActive(false); viewArticle(a); }}
          onAskKairos={home.kairos_enabled ? () => toast("Kairos panel above") : null}
          onContact={() => { setSearchActive(false); setContactOpen(true); }}
        />
      ) : (
        <>
          <Recommendations
            recs={recs}
            onView={(slug) => {
              const a = ARTICLES.find(x => x.slug === slug);
              if (a) viewArticle(a);
            }}
          />

          <Categories
            onOpen={(key) => setOpenCategory(key)}
          />

          <QuickActions onAction={onQuickAction} />

          <TicketsCard
            tickets={tickets}
            loading={ticketsLoading}
            onRefresh={loadTickets}
            onOpen={async (t) => { setTicketDetail(t); reloadOneTicket(t.id); }}
            onContact={() => setContactOpen(true)}
          />
        </>
      )}

      {/* ─── Modals ────────────────────────────────────────────────────── */}
      {openArticle && (
        <ArticleModal
          article={openArticle}
          onClose={() => setOpenArticle(null)}
          onContact={() => { setOpenArticle(null); setContactOpen(true); }}
          onJumpToTab={onJumpToTab}
        />
      )}
      {openCategory && (
        <CategoryModal
          categoryKey={openCategory}
          onClose={() => setOpenCategory(null)}
          onView={(a) => { setOpenCategory(null); viewArticle(a); }}
        />
      )}
      {contactOpen && (
        <ContactDrawer
          seed={contactSeed}
          onClose={() => { setContactOpen(false); setContactSeed(null); }}
          onSubmitted={async () => {
            setContactOpen(false);
            setContactSeed(null);
            await loadTickets();
            toast.success("Your request has been submitted");
          }}
        />
      )}
      {ticketDetail && (
        <TicketDetailModal
          ticket={ticketDetail}
          onClose={() => setTicketDetail(null)}
          onReload={() => reloadOneTicket(ticketDetail.id).finally(loadTickets)}
        />
      )}
    </div>
    </CCtx.Provider>
  );
}

// ═════════════════════════════════════════════════════════════════════════════
// SUB-COMPONENTS
// ═════════════════════════════════════════════════════════════════════════════

function Header({ onContact, onMyTickets }) {
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
          <Icon n="help" s={22} c="#fff" w={2.2} />
        </div>
        <div style={{ flex: 1, minWidth: 240 }}>
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      letterSpacing: 0.6, textTransform: "uppercase", margin: 0 }}>
            Account · Help
          </p>
          <h1 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: "2px 0 0" }}>
            Help
          </h1>
          <p style={{ fontSize: 13, color: C.textSub, margin: "6px 0 0", lineHeight: 1.6, maxWidth: 720 }}>
            Get answers, contact support, or ask Kairos. Help is a trust surface — clear, honest,
            and routed to the people who can actually help.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, flexShrink: 0, flexWrap: "wrap" }}>
          <Button variant="secondary" icon="send" onClick={onContact}>
            Contact Support
          </Button>
          <Button variant="secondary" icon="inbox" onClick={onMyTickets}>
            My Tickets
          </Button>
        </div>
      </div>
    </div>
  );
}

// ─── Kairos panel (only renders when feature-flagged on) ─────────────────────
function KairosPanel() {
  const C = useC();
  const [q, setQ] = useState("");
  const [reply, setReply] = useState(null);
  const [pending, setPending] = useState(false);
  const [introShown, setIntroShown] = useState(false);

  const ask = async () => {
    if (!q.trim()) return;
    setPending(true);
    try {
      const { data } = await API.post("/worker/help/kairos/ask", { question: q.trim() });
      setReply(data);
      if (!introShown) setIntroShown(true);
    } catch {
      setReply({
        is_ai: true,
        answer: "Kairos is unavailable right now. Try contacting support or browsing Help below.",
        sources: [],
      });
    } finally {
      setPending(false);
    }
  };

  return (
    <Card icon="bot" title="Kairos"
          subtitle="Ask anything about Temveris, your schedule, your data, or your account. Kairos can explain how things work — for decisions only your manager or HR can make, it'll route you to the right person.">
      <div style={{
        display: "flex", gap: 8, alignItems: "stretch", flexWrap: "wrap",
        padding: "8px 0",
      }}>
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }}
          placeholder="Ask Kairos a question…"
          style={{
            flex: 1, minWidth: 240, minHeight: 44,
            padding: "10px 14px", borderRadius: C.rSm,
            border: `1px solid ${C.border}`, background: C.bg,
            fontSize: 14, color: C.text, fontFamily: "inherit", outline: "none",
          }}
        />
        <Button onClick={ask} disabled={!q.trim() || pending} icon="send">
          {pending ? "Asking…" : "Ask Kairos"}
        </Button>
      </div>

      {!introShown && (
        <p style={{ fontSize: 11, color: C.textLight, margin: "10px 0 0",
                    display: "flex", alignItems: "center", gap: 6 }}>
          <Icon n="info" s={12} c={C.textLight} />
          Kairos is an AI assistant. It answers from your workspace's approved sources only.
        </p>
      )}

      {reply && (
        <div style={{
          marginTop: 14, padding: 14, borderRadius: 8,
          background: C.brandBg, border: `1px solid ${C.brandBg}`,
        }}>
          <p style={{ fontSize: 11, color: C.brand, fontWeight: 800,
                      letterSpacing: 0.5, textTransform: "uppercase", margin: 0,
                      display: "flex", alignItems: "center", gap: 6 }}>
            <Icon n="bot" s={12} c={C.brand} /> Kairos · AI
          </p>
          <p style={{ fontSize: 13, color: C.text, margin: "8px 0 0", lineHeight: 1.6,
                      whiteSpace: "pre-wrap" }}>
            {reply.answer}
          </p>
          {reply.sources?.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <p style={{ fontSize: 10, color: C.textSub, fontWeight: 700,
                          textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 4px" }}>
                Sources
              </p>
              {reply.sources.map((s, i) => (
                <span key={i} style={{ marginRight: 6 }}>
                  <Pill tone="info" icon="doc">{s.title || s}</Pill>
                </span>
              ))}
            </div>
          )}
          {reply.escalation && (
            <p style={{ fontSize: 12, color: C.amber, margin: "10px 0 0",
                        padding: "8px 10px", borderRadius: 6, background: C.amberBg }}>
              This needs a human. {reply.escalation.message}
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

// ─── Search bar ─────────────────────────────────────────────────────────────
function SearchBar({ value, onChange, onFocus, onBlur, onSubmit }) {
  const C = useC();
  return (
    <Card padding={18}>
      <div style={{ display: "flex", alignItems: "center", gap: 10,
                     padding: "0 6px",
                     border: `1px solid ${C.border}`, borderRadius: 8,
                     background: C.bg }}>
        <Icon n="search" s={16} c={C.textSub} />
        <input
          type="search"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onFocus={onFocus}
          onBlur={onBlur}
          onKeyDown={(e) => { if (e.key === "Enter") onSubmit?.(); }}
          placeholder="Search help articles, policies, and answers…"
          style={{
            flex: 1, padding: "11px 6px", border: "none",
            background: "transparent", outline: "none", fontSize: 14,
            color: C.text, fontFamily: "inherit", minHeight: 44,
          }}
          aria-label="Search help"
        />
        {value && (
          <button type="button" onClick={() => onChange("")} aria-label="Clear search"
            style={{ background: "none", border: "none", cursor: "pointer", padding: 6 }}>
            <Icon n="x" s={14} c={C.textSub} />
          </button>
        )}
      </div>
    </Card>
  );
}

function SearchResults({ q, results, onView, onAskKairos, onContact }) {
  const C = useC();
  const total = results.articles.length + results.tickets.length;
  if (total === 0) {
    return (
      <Card>
        <EmptyState icon="search"
          title={`No matches for "${q}"`}
          body="Try different words, ask Kairos, or contact support."
          action={
            <div style={{ display: "flex", gap: 8, justifyContent: "center", flexWrap: "wrap" }}>
              {onAskKairos && <Button icon="bot" onClick={onAskKairos}>Ask Kairos</Button>}
              <Button variant="secondary" icon="send" onClick={onContact}>Contact Support</Button>
            </div>
          }
        />
      </Card>
    );
  }
  return (
    <Card title={`Search results · ${total}`} icon="search">
      {results.articles.length > 0 && (
        <>
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 8px" }}>
            Articles
          </p>
          <ul style={{ listStyle: "none", padding: 0, margin: "0 0 16px" }}>
            {results.articles.map(a => <ArticleResultRow key={a.slug} article={a} onView={onView} />)}
          </ul>
        </>
      )}
      {results.tickets.length > 0 && (
        <>
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 8px" }}>
            Your tickets
          </p>
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {results.tickets.map(t => (
              <li key={t.id} style={{ padding: "8px 0", borderTop: `1px solid ${C.borderLight}` }}>
                <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>
                  {t.subject || "(no subject)"}
                </p>
                <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                  {t.topic} · {fmtRel(t.created_at)} · {(TICKET_STATUS[t.status] || {}).label || t.status}
                </p>
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

function ArticleResultRow({ article, onView }) {
  const C = useC();
  const cat = CATEGORIES.find(c => c.key === article.category);
  return (
    <li style={{ borderTop: `1px solid ${C.borderLight}` }}>
      <button type="button" onClick={() => onView(article)}
        style={{
          width: "100%", textAlign: "left", padding: "12px 0",
          background: "none", border: "none", cursor: "pointer", fontFamily: "inherit",
        }}>
        <p style={{ fontSize: 14, fontWeight: 700, color: C.brand, margin: 0 }}>
          {article.title}
        </p>
        <p style={{ fontSize: 13, color: C.textSub, margin: "4px 0 0", lineHeight: 1.5 }}>
          {article.snippet}
        </p>
        <p style={{ fontSize: 11, color: C.textLight, margin: "6px 0 0",
                    display: "flex", gap: 8, flexWrap: "wrap" }}>
          <span>{cat?.label || article.category}</span>
          <span>·</span>
          <span>{article.read_min} min</span>
          <span>·</span>
          <span>Last reviewed {fmtDate(article.last_reviewed)}</span>
        </p>
      </button>
    </li>
  );
}

// ─── Recommended for You ─────────────────────────────────────────────────────
function Recommendations({ recs, onView }) {
  const C = useC();
  if (!recs?.length) return null;
  return (
    <Card title="Recommended for you" icon="zap"
          subtitle="Based on your current state. We never use this for productivity scoring or comparisons.">
      <div style={{
        display: "grid", gap: 10,
        gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
      }}>
        {recs.map((rec) => {
          const a = ARTICLES.find(x => x.slug === rec.slug);
          if (!a) return null;
          return (
            <button key={rec.slug} type="button" onClick={() => onView(rec.slug)}
              style={{
                textAlign: "left", padding: 14, borderRadius: 10,
                border: `1px solid ${C.border}`, background: C.surface,
                cursor: "pointer", fontFamily: "inherit",
                display: "flex", flexDirection: "column", gap: 8, minHeight: 96,
              }}>
              <p style={{ fontSize: 11, color: C.brand, fontWeight: 700,
                          textTransform: "uppercase", letterSpacing: 0.5, margin: 0 }}>
                {rec.reason || "Suggested"}
              </p>
              <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0, lineHeight: 1.4 }}>
                {a.title}
              </p>
              <p style={{ fontSize: 11, color: C.textSub, margin: "auto 0 0",
                          display: "flex", alignItems: "center", gap: 4 }}>
                <Icon n="arrow" s={12} c={C.textSub} /> Read · {a.read_min} min
              </p>
            </button>
          );
        })}
      </div>
    </Card>
  );
}

// ─── Categories ──────────────────────────────────────────────────────────────
function Categories({ onOpen }) {
  const C = useC();
  const visible = CATEGORIES.filter(cat =>
    ARTICLES.some(a => a.category === cat.key)
  );
  if (!visible.length) return null;
  return (
    <Card title="Browse by topic" icon="doc">
      <div style={{
        display: "grid", gap: 12,
        gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
      }}>
        {visible.map((cat) => {
          const inCat = ARTICLES.filter(a => a.category === cat.key);
          const top3  = inCat.slice(0, 3);
          return (
            <div key={cat.key} style={{
              padding: 16, borderRadius: 10,
              border: `1px solid ${C.border}`, background: C.surface,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
                <Icon n={cat.icon} s={18} c={C.brand} />
                <p style={{ fontSize: 14, fontWeight: 800, color: C.text, margin: 0 }}>
                  {cat.label}
                </p>
              </div>
              <p style={{ fontSize: 12, color: C.textSub, margin: "0 0 10px", lineHeight: 1.5 }}>
                {cat.desc}
              </p>
              <ul style={{ listStyle: "none", padding: 0, margin: "0 0 8px" }}>
                {top3.map(a => (
                  <li key={a.slug} style={{ padding: "4px 0" }}>
                    <span style={{ fontSize: 13, color: C.text, fontWeight: 600 }}>· {a.title}</span>
                  </li>
                ))}
              </ul>
              <Button variant="ghost" onClick={() => onOpen(cat.key)}
                      style={{ padding: "6px 0", minHeight: 32 }}>
                Browse all {inCat.length} articles →
              </Button>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function CategoryModal({ categoryKey, onClose, onView }) {
  const C = useC();
  const cat = CATEGORIES.find(c => c.key === categoryKey);
  const articles = ARTICLES.filter(a => a.category === categoryKey);
  return (
    <Modal open={true} onClose={onClose} title={cat?.label || "Category"}
      footer={<Button variant="secondary" onClick={onClose}>Close</Button>}>
      <p style={{ fontSize: 13, color: C.textSub, margin: "0 0 16px", lineHeight: 1.6 }}>
        {cat?.desc}
      </p>
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {articles.map(a => <ArticleResultRow key={a.slug} article={a} onView={onView} />)}
      </ul>
    </Modal>
  );
}

// ─── Quick Actions ───────────────────────────────────────────────────────────
function QuickActions({ onAction }) {
  const C = useC();
  const items = [
    { key: "correction", label: "Request a correction", icon: "doc",  tone: C.brand  },
    { key: "report",     label: "Report an issue",      icon: "warning", tone: C.amber  },
    { key: "pay",        label: "Ask about my pay",     icon: "dollar", tone: C.green  },
    { key: "data",       label: "Ask about my data",    icon: "shield", tone: C.purple },
  ];
  return (
    <Card title="Quick actions" icon="zap">
      <div style={{
        display: "grid", gap: 10,
        gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
      }}>
        {items.map(it => (
          <button key={it.key} type="button" onClick={() => onAction(it.key)}
            style={{
              padding: "14px", borderRadius: 10,
              border: `1px solid ${C.border}`, background: C.surface,
              cursor: "pointer", fontFamily: "inherit", fontWeight: 600,
              fontSize: 13, color: C.text, textAlign: "left",
              display: "flex", alignItems: "center", gap: 10, minHeight: 56,
            }}>
            <span style={{
              width: 36, height: 36, borderRadius: 8,
              background: `${it.tone}15`,
              display: "flex", alignItems: "center", justifyContent: "center",
            }}>
              <Icon n={it.icon} s={18} c={it.tone} />
            </span>
            <span style={{ flex: 1 }}>{it.label}</span>
            <Icon n="chevright" s={14} c={C.textLight} />
          </button>
        ))}
      </div>
    </Card>
  );
}

// ─── Tickets list ────────────────────────────────────────────────────────────
function TicketsCard({ tickets, loading, onRefresh, onOpen, onContact }) {
  const C = useC();
  return (
    <div id="zt-help-tickets">
      <Card
        title={`My tickets (${tickets.length})`}
        icon="inbox"
        padding={0}
        action={
          <div style={{ display: "flex", gap: 6 }}>
            <Button variant="secondary" icon="refresh" onClick={onRefresh}>Refresh</Button>
            <Button icon="send" onClick={onContact}>New request</Button>
          </div>
        }
      >
        {loading ? <Spinner /> : tickets.length === 0 ? (
          <EmptyState icon="inbox"
            title="No support requests yet."
            body="If you need help, contact support or browse Help articles below."
            action={<Button icon="send" onClick={onContact}>Contact Support</Button>}
          />
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {tickets.map(t => {
              const st = TICKET_STATUS[t.status] || { label: t.status || "—", tone: "neutral" };
              return (
                <li key={t.id} style={{ borderBottom: `1px solid ${C.borderLight}` }}>
                  <button type="button" onClick={() => onOpen(t)}
                    style={{
                      width: "100%", textAlign: "left",
                      padding: "14px 24px", background: "none", border: "none",
                      cursor: "pointer", fontFamily: "inherit",
                      display: "grid",
                      gridTemplateColumns: "minmax(120px, 150px) minmax(0, 2fr) minmax(0, 1.2fr) auto auto",
                      gap: 12, alignItems: "center",
                    }}>
                    <span style={{ fontSize: 12, color: C.textSub }}>
                      {fmtDate(t.created_at)}
                    </span>
                    <span style={{ minWidth: 0 }}>
                      <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0,
                                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {t.subject || "(no subject)"}
                        {t.sensitive && <span style={{ marginLeft: 8 }}><Pill tone="locked" icon="lock">Confidential</Pill></span>}
                      </p>
                      <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                        {t.topic_label || t.topic}
                      </p>
                    </span>
                    <span style={{ fontSize: 12, color: C.textSub, minWidth: 0,
                                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      Routed to <strong style={{ color: C.text }}>{t.routed_to_name || "—"}</strong>
                    </span>
                    <Pill tone={st.tone}>{st.label}</Pill>
                    <Icon n="chevright" s={14} c={C.textLight} />
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </div>
  );
}

// ─── Article modal ───────────────────────────────────────────────────────────
function ArticleModal({ article, onClose, onContact, onJumpToTab }) {
  const C = useC();
  const cat = CATEGORIES.find(c => c.key === article.category);
  return (
    <Modal open={true} onClose={onClose} title={article.title} size="large"
      footer={
        <div style={{ display: "flex", gap: 8, alignItems: "center", flex: 1, flexWrap: "wrap" }}>
          <span style={{ fontSize: 11, color: C.textLight, marginRight: "auto" }}>
            Last reviewed {fmtDate(article.last_reviewed)} · v1
          </span>
          {article.related_action && (
            <Button variant="secondary" icon="external"
              onClick={() => { onJumpToTab?.(article.related_action.tab); onClose(); }}>
              {article.related_action.label}
            </Button>
          )}
          <Button variant="ghost" icon="send" onClick={onContact}>
            Still need help?
          </Button>
        </div>
      }>
      <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                  textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 8px" }}>
        {cat?.label} · {article.read_min} min
      </p>
      <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0, lineHeight: 1.5 }}>
        {article.snippet}
      </p>
      <p style={{ fontSize: 14, color: C.text, margin: "16px 0 0", lineHeight: 1.7,
                  whiteSpace: "pre-wrap" }}>
        {article.body}
      </p>
      {article.related_policy_id && (
        <div style={{
          marginTop: 18, padding: 14, borderRadius: 8,
          background: C.brandBg, border: `1px solid ${C.brandBg}`,
        }}>
          <p style={{ fontSize: 11, color: C.brand, fontWeight: 800,
                      textTransform: "uppercase", letterSpacing: 0.5, margin: 0 }}>
            Related policy
          </p>
          <p style={{ fontSize: 13, color: C.text, margin: "6px 0 0", lineHeight: 1.6 }}>
            This article references your workspace's <strong>{article.related_policy_id.replace(/_/g, " ")}</strong>.
            Open My Data to view the full policy and your acknowledgement status.
          </p>
        </div>
      )}
    </Modal>
  );
}

// ─── Contact Support drawer (3 steps) ────────────────────────────────────────
function ContactDrawer({ seed, onClose, onSubmitted }) {
  const C = useC();
  const [step,         setStep]        = useState(1);
  const [topic,        setTopic]       = useState(seed?.topic || "");
  const [subject,      setSubject]     = useState(seed?.subject || "");
  const [description,  setDescription] = useState("");
  const [whenStr,      setWhenStr]     = useState("");
  const [confidential, setConfidential]= useState(false);
  const [diagConsent,  setDiagConsent] = useState(false);
  const [submitting,   setSubmitting]  = useState(false);
  const [confirmation, setConfirmation]= useState(null);

  const topicMeta = useMemo(() =>
    TICKET_TOPICS.find(t => t.value === topic) || null,
    [topic]);

  const isSensitive = !!topicMeta?.forceSensitive || (topicMeta?.sensitiveOptional && confidential);
  const requiresWhen = ["clock_issue", "schedule", "pay", "technical"].includes(topic);
  const requiresDiag = !!topicMeta?.needsDiagnostics;

  const canSubmit =
    !!topic && subject.trim().length > 0 &&
    description.trim().length >= 50 &&
    (!requiresWhen || whenStr.trim().length > 0) &&
    (!requiresDiag || diagConsent);

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const { data } = await API.post("/worker/help/tickets", {
        topic,
        topic_label: topicMeta?.label,
        subject:     subject.trim(),
        description: description.trim(),
        when_str:    whenStr.trim() || null,
        sensitive:   isSensitive,
        confidentiality_flag: confidential,
        diagnostic_consent:   diagConsent,
      });
      setConfirmation(data || { id: "ZT-pending", routed_to_name: topicMeta?.routesTo, sla: topicMeta?.sla });
      setStep(3);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't submit — please try again");
    } finally {
      setSubmitting(false);
    }
  };

  const next = () => {
    if (step === 1 && !topic) {
      toast.error("Please choose a topic first");
      return;
    }
    setStep(s => Math.min(3, s + 1));
  };
  const back = () => setStep(s => Math.max(1, s - 1));

  const footer =
    step === 3 ? (
      <Button onClick={() => onSubmitted()}>View my tickets</Button>
    ) : (
      <>
        <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
        {step > 1 && <Button variant="secondary" onClick={back} disabled={submitting}>Back</Button>}
        {step < 2 && <Button onClick={next} disabled={!topic}>Next</Button>}
        {step === 2 && (
          <Button onClick={submit} disabled={!canSubmit || submitting}>
            {submitting ? "Submitting…" : "Submit request"}
          </Button>
        )}
      </>
    );

  return (
    <Modal open={true} onClose={onClose}
      title={
        step === 1 ? "Contact Support · What is this about?" :
        step === 2 ? "Contact Support · Tell us more" :
                     "Request submitted"
      }
      size={step === 3 ? "small" : "medium"}
      fullScreenMobile={true}
      footer={footer}>

      {/* Step 1 — topic */}
      {step === 1 && (
        <>
          <p style={{ fontSize: 13, color: C.textSub, margin: "0 0 14px", lineHeight: 1.5 }}>
            Choose what your request is about. We'll show you who it goes to and the expected
            response time before you submit.
          </p>
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {TICKET_TOPICS.map(t => {
              const active = topic === t.value;
              return (
                <li key={t.value} style={{ marginBottom: 8 }}>
                  <button type="button" onClick={() => setTopic(t.value)}
                    style={{
                      width: "100%", textAlign: "left", padding: "12px 14px",
                      borderRadius: 8, fontFamily: "inherit", cursor: "pointer", minHeight: 56,
                      border: `2px solid ${active ? C.brand : C.border}`,
                      background: active ? C.brandBg : C.surface,
                    }}>
                    <p style={{ fontSize: 13, fontWeight: 800,
                                color: active ? C.brand : C.text, margin: 0 }}>
                      {t.label}
                      {t.forceSensitive && <span style={{ marginLeft: 8 }}><Pill tone="locked" icon="lock">Confidential</Pill></span>}
                    </p>
                    <p style={{ fontSize: 11, color: C.textSub, margin: "4px 0 0" }}>
                      Routes to <strong style={{ color: C.text }}>{t.routesTo}</strong> · Expected: {t.sla}
                    </p>
                  </button>
                </li>
              );
            })}
          </ul>
        </>
      )}

      {/* Step 2 — details */}
      {step === 2 && topicMeta && (
        <>
          <div style={{
            padding: 12, borderRadius: 8, background: C.brandBg,
            border: `1px solid ${C.brandBg}`, marginBottom: 14,
          }}>
            <p style={{ fontSize: 12, color: C.brand, fontWeight: 700, margin: 0,
                        textTransform: "uppercase", letterSpacing: 0.5 }}>
              This will go to
            </p>
            <p style={{ fontSize: 13, color: C.text, margin: "4px 0 0", fontWeight: 700 }}>
              {topicMeta.routesTo}
            </p>
            <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}>
              Expected response: {topicMeta.sla}
            </p>
          </div>

          <Field label="Subject" required>
            <Input value={subject} onChange={setSubject} autoFocus
                   placeholder="Short summary of what's happening" />
          </Field>
          <Field label="Description" required helper="Minimum 50 characters.">
            <Textarea value={description} onChange={setDescription} rows={5}
              placeholder="Explain what happened, when it started, and anything you've already tried." />
          </Field>
          {requiresWhen && (
            <Field label="When did this happen?" required>
              <Input value={whenStr} onChange={setWhenStr}
                placeholder="e.g. Today around 9:30 AM, or Monday afternoon" />
            </Field>
          )}

          {topicMeta.sensitiveOptional && (
            <label style={{ display: "flex", gap: 10, alignItems: "flex-start",
                             fontSize: 13, color: C.text, cursor: "pointer", marginBottom: 12,
                             padding: 10, borderRadius: 6, background: C.bg,
                             border: `1px solid ${C.border}` }}>
              <input type="checkbox" checked={confidential}
                onChange={(e) => setConfidential(e.target.checked)} style={{ marginTop: 3 }} />
              <span>
                <strong>Mark this as confidential.</strong> Visibility will be restricted to your
                routed handler and the workspace's designated grievance/data contact.
              </span>
            </label>
          )}

          {requiresDiag && (
            <label style={{ display: "flex", gap: 10, alignItems: "flex-start",
                             fontSize: 13, color: C.text, cursor: "pointer", marginBottom: 8,
                             padding: 10, borderRadius: 6, background: C.amberBg,
                             border: `1px solid ${C.amber}33` }}>
              <input type="checkbox" checked={diagConsent}
                onChange={(e) => setDiagConsent(e.target.checked)} style={{ marginTop: 3 }} />
              <span>
                <strong>Allow diagnostic data.</strong> Include device and app diagnostics with
                this ticket so support can troubleshoot faster. No screen content, captured
                screenshots, keystrokes, or personal data is included.
              </span>
            </label>
          )}

          {topicMeta.forceSensitive && (
            <p style={{
              fontSize: 12, color: C.amber, padding: "10px 12px", borderRadius: 6,
              background: C.amberBg, lineHeight: 1.5, marginTop: 8,
            }}>
              <strong>This is treated as a sensitive request.</strong> Visibility is limited to
              the designated grievance contact and you. Your manager won't see it unless they're
              part of the explicit visibility set.
            </p>
          )}
        </>
      )}

      {/* Step 3 — confirmation */}
      {step === 3 && confirmation && (
        <>
          <div style={{
            display: "flex", flexDirection: "column", alignItems: "center",
            textAlign: "center", padding: "16px 0",
          }}>
            <div style={{
              width: 56, height: 56, borderRadius: "50%",
              background: C.greenBg, display: "flex",
              alignItems: "center", justifyContent: "center", marginBottom: 12,
            }}>
              <Icon n="check" s={28} c={C.green} w={2.5} />
            </div>
            <p style={{ fontSize: 16, fontWeight: 800, color: C.text, margin: 0 }}>
              Your request has been submitted.
            </p>
            <p style={{ fontSize: 13, color: C.textSub, margin: "8px 0 0", lineHeight: 1.5 }}>
              Ticket <strong style={{ color: C.text }}>{confirmation.id || "—"}</strong> · routed to{" "}
              <strong style={{ color: C.text }}>{confirmation.routed_to_name || topicMeta?.routesTo}</strong>
            </p>
            <p style={{ fontSize: 13, color: C.textSub, margin: "4px 0 0" }}>
              Expected response: {confirmation.sla || topicMeta?.sla}
            </p>
          </div>
        </>
      )}
    </Modal>
  );
}

// ─── Ticket detail modal (conversation, status, escalate, reopen) ────────────
function TicketDetailModal({ ticket, onClose, onReload }) {
  const C = useC();
  const [reply,     setReply]   = useState("");
  const [sending,   setSending] = useState(false);
  const [showEscalate, setShowEscalate] = useState(false);
  const st = TICKET_STATUS[ticket.status] || { label: ticket.status, tone: "neutral" };
  const messages = ticket.messages || [];
  const canEscalate = ESCALATION_ELIGIBLE_TOPICS.has(ticket.topic) && ticket.status !== "resolved";
  const canReopen = ticket.status === "resolved" && (() => {
    if (!ticket.resolved_at) return false;
    const days = (Date.now() - new Date(ticket.resolved_at).getTime()) / 86400000;
    return days <= 14;
  })();

  const sendMessage = async () => {
    if (!reply.trim()) return;
    setSending(true);
    try {
      await API.post(`/worker/help/tickets/${ticket.id}/messages`, { message_body: reply.trim() });
      setReply("");
      toast.success("Message sent");
      onReload?.();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't send");
    } finally {
      setSending(false);
    }
  };

  const reopen = async () => {
    try {
      await API.post(`/worker/help/tickets/${ticket.id}/messages`, {
        message_body: "Reopening this ticket — the issue isn't resolved yet.",
        reopen: true,
      });
      toast.success("Ticket reopened");
      onReload?.();
    } catch {
      toast.error("Couldn't reopen");
    }
  };

  return (
    <>
      <Modal open={true} onClose={onClose} title={ticket.subject || "Ticket"} size="large"
        footer={
          <div style={{ display: "flex", gap: 8, alignItems: "center", flex: 1, flexWrap: "wrap" }}>
            <span style={{ fontSize: 11, color: C.textLight, marginRight: "auto" }}>
              Ticket {ticket.id} · {fmtRel(ticket.created_at)}
            </span>
            {canReopen && <Button variant="secondary" onClick={reopen}>Reopen</Button>}
            {canEscalate && (
              <Button variant="secondary" icon="flag" onClick={() => setShowEscalate(true)}>
                Escalate
              </Button>
            )}
            <Button variant="secondary" onClick={onClose}>Close</Button>
          </div>
        }>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
          <Pill tone={st.tone}>{st.label}</Pill>
          {ticket.sensitive && <Pill tone="locked" icon="lock">Confidential</Pill>}
          <span style={{ fontSize: 12, color: C.textSub }}>
            Routed to <strong style={{ color: C.text }}>{ticket.routed_to_name || "—"}</strong>
          </span>
          {ticket.expected_response_at && (
            <span style={{ fontSize: 12, color: C.textSub }}>
              Expected by {fmtDate(ticket.expected_response_at)}
            </span>
          )}
        </div>

        <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                    textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 6px" }}>
          Original request
        </p>
        <div style={{
          padding: 12, borderRadius: 8, background: C.bg,
          border: `1px solid ${C.borderLight}`, marginBottom: 16,
          fontSize: 13, color: C.text, lineHeight: 1.6, whiteSpace: "pre-wrap",
        }}>
          {ticket.description || "—"}
        </div>

        {messages.length > 0 && (
          <>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 6px" }}>
              Conversation
            </p>
            <ul style={{ listStyle: "none", padding: 0, margin: "0 0 16px" }}>
              {messages.map((m, i) => (
                <li key={m.id || i} style={{
                  padding: 12, borderRadius: 8,
                  background: m.sender_role === "worker" ? C.brandBg : C.bg,
                  border: `1px solid ${C.borderLight}`, marginBottom: 6,
                }}>
                  <p style={{ fontSize: 11, color: C.textSub, fontWeight: 700, margin: 0 }}>
                    {m.sender_name || (m.sender_role === "worker" ? "You" : "Handler")} · {fmtRel(m.created_at)}
                  </p>
                  <p style={{ fontSize: 13, color: C.text, margin: "4px 0 0", lineHeight: 1.6,
                              whiteSpace: "pre-wrap" }}>
                    {m.message_body}
                  </p>
                </li>
              ))}
            </ul>
          </>
        )}

        {ticket.status !== "closed" && ticket.status !== "resolved" && (
          <Field label="Reply">
            <Textarea value={reply} onChange={setReply} rows={3}
              placeholder="Type your reply…" />
            <div style={{ marginTop: 8, display: "flex", justifyContent: "flex-end" }}>
              <Button icon="send" onClick={sendMessage} disabled={!reply.trim() || sending}>
                {sending ? "Sending…" : "Send"}
              </Button>
            </div>
          </Field>
        )}
      </Modal>

      {showEscalate && (
        <EscalationModal
          ticket={ticket}
          onClose={() => setShowEscalate(false)}
          onSubmitted={() => { setShowEscalate(false); onReload?.(); }}
        />
      )}
    </>
  );
}

function EscalationModal({ ticket, onClose, onSubmitted }) {
  const C = useC();
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async () => {
    if (reason.trim().length < 30) {
      toast.error("Please provide at least 30 characters of context");
      return;
    }
    setSubmitting(true);
    try {
      await API.post(`/worker/help/tickets/${ticket.id}/escalate`, { reason: reason.trim() });
      toast.success("Escalation submitted");
      onSubmitted();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't escalate");
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <Modal open={true} onClose={onClose} title="Escalate this ticket" size="small"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button variant="danger" icon="flag" onClick={submit}
                  disabled={submitting || reason.trim().length < 30}>
            {submitting ? "Submitting…" : "Submit escalation"}
          </Button>
        </>
      }>
      <p style={{ fontSize: 13, color: C.textSub, margin: "0 0 12px", lineHeight: 1.5 }}>
        Escalation routes per your workspace policy — typically HR, ombudsperson, or a designated
        grievance contact. Every escalation is logged to the audit trail.
      </p>
      <Field label="Why does this need escalation?" required
             helper="Minimum 30 characters. Be specific — what's not working with the current response?">
        <Textarea value={reason} onChange={setReason} rows={4}
          placeholder="e.g. The pay correction was rejected without addressing my evidence." />
      </Field>
    </Modal>
  );
}
