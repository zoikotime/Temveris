/**
 * NotificationsModule.jsx — worker-facing Notifications settings.
 *
 * One page, six sections:
 *   1. Setup Summary       — timezone, default channels, quiet hours, required alerts copy
 *   2. Posture selector    — Essential / Balanced / Detailed / Custom
 *   3. Notification catalog — 5 groups, each row collapsed in postures, expanded in Custom
 *   4. Channel preferences — In-app (locked) / Email / Push / SMS
 *   5. Quiet hours         — toggle, time range, critical-override copy
 *   6. Notification history (inline) — last N events with filters
 *
 * Key invariants enforced in this UI (per spec §11):
 *   • In-app delivery cannot be disabled for any notification.
 *   • Required notification rows are locked; their toggle is disabled and lock copy shown.
 *   • Custom posture exposes per-row controls; other postures collapse them.
 *   • Test sends only to the signed-in worker.
 *   • Every preference change saves immediately and writes to the audit trail
 *     (server-side enforcement; client just calls PATCH).
 *
 * Backend endpoints used (graceful 404 fallback so the page works pre-launch):
 *   GET    /notifications/preferences
 *   PATCH  /notifications/preferences
 *   GET    /notifications/history
 *   POST   /notifications/test
 */

import React, { useEffect, useMemo, useState } from "react";
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

const browserTZ = () => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; }
  catch { return "UTC"; }
};

// ─── inline icon set ────────────────────────────────────────────────────────
const ICON = {
  bell:       "M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9 M13.73 21a2 2 0 01-3.46 0",
  shield:     "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  lock:       "M19 11H5a2 2 0 00-2 2v7a2 2 0 002 2h14a2 2 0 002-2v-7a2 2 0 00-2-2z M7 11V7a5 5 0 0110 0v4",
  moon:       "M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z",
  sun:        "M12 18a6 6 0 100-12 6 6 0 000 12z M12 2v4 M12 18v4 M4.22 4.22l2.83 2.83 M16.95 16.95l2.83 2.83 M2 12h4 M18 12h4 M4.22 19.78l2.83-2.83 M16.95 7.05l2.83-2.83",
  clock:      "M12 22a10 10 0 100-20 10 10 0 000 20z M12 6v6l4 2",
  calendar:   "M8 2v4 M16 2v4 M3 10h18 M5 4h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V6a2 2 0 012-2z",
  briefcase:  "M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2",
  leaf:       "M17 8C8 10 5.9 16.17 3.82 22 M9.5 9.5c1 2.5 3.5 5 6.5 5.5 M21 3C6 5 5 13 5 20",
  dollar:     "M12 1v22 M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6",
  message:    "M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z",
  smartphone: "M5 2h14a2 2 0 012 2v16a2 2 0 01-2 2H5a2 2 0 01-2-2V4a2 2 0 012-2z M12 18h.01",
  monitor:    "M2 3h20a1 1 0 011 1v13a1 1 0 01-1 1H2a1 1 0 01-1-1V4a1 1 0 011-1z M8 21h8 M12 17v4",
  mail:       "M4 4h16a2 2 0 012 2v12a2 2 0 01-2 2H4a2 2 0 01-2-2V6a2 2 0 012-2z M22 6l-10 7L2 6",
  phone:      "M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6 19.79 19.79 0 01-3.07-8.67A2 2 0 014.11 2h3a2 2 0 012 1.72c.13.96.37 1.9.72 2.81a2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45c.91.35 1.85.59 2.81.72A2 2 0 0122 16.92z",
  check:      "M20 6L9 17l-5-5",
  x:          "M18 6L6 18 M6 6l12 12",
  refresh:    "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10 M1 14l4.64 4.36A9 9 0 0020.49 15",
  send:       "M22 2L11 13 M22 2l-7 20-4-9-9-4 20-7z",
  chevdown:   "M6 9l6 6 6-6",
  chevup:     "M18 15l-6-6-6 6",
  info:       "M12 22a10 10 0 100-20 10 10 0 000 20z M12 16v-4 M12 8h.01",
  zap:        "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
  inbox:      "M22 12h-6l-2 3h-4l-2-3H2 M5.45 5.11L2 12v6a2 2 0 002 2h16a2 2 0 002-2v-6l-3.45-6.89A2 2 0 0016.76 4H7.24a2 2 0 00-1.79 1.11z",
  filter:     "M22 3H2l8 9.46V19l4 2v-8.54L22 3z",
  search:     "M11 19a8 8 0 100-16 8 8 0 000 16z M21 21l-4.35-4.35",
  doc:        "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8",
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

const Toggle = ({ on, onChange, disabled, ariaLabel }) => {
  const C = useC();
  return (
    <button type="button"
      onClick={() => !disabled && onChange?.(!on)}
      disabled={disabled}
      aria-label={ariaLabel}
      aria-pressed={!!on}
      style={{
        width: 40, height: 22, borderRadius: 12, border: "none",
        cursor: disabled ? "not-allowed" : "pointer",
        background: on ? C.brand : C.textLight, position: "relative",
        transition: "background .18s ease", flexShrink: 0,
        opacity: disabled ? 0.55 : 1,
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

const EmptyState = ({ icon = "info", title, body }) => {
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
    </div>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// NOTIFICATION CATALOG  (spec §6)
// ═════════════════════════════════════════════════════════════════════════════
//
// Each row has:
//   key            — server-side notification_type
//   title / sub    — worker-facing copy
//   timing         — when it fires
//   defaults       — per posture (essential | balanced | detailed)
//   locked         — true if worker cannot disable (spec lists §7.5 + critical)
//   channels       — default channels array
//
// Custom posture exposes individual toggles. Other postures collapse the group
// into a one-line summary saying "Following [posture] defaults".

const CATALOG = [
  {
    group: "schedule", icon: "calendar",
    label: "Schedule & Workday",
    sub:   "Shifts, schedule changes, missed clock-ins, and end-of-shift prompts.",
    items: [
      { key: "todays_schedule",   title: "Today's schedule",         timing: "30 min before first shift",
        defaults: { essential: false, balanced: true,  detailed: true  } },
      { key: "shift_starting",    title: "Shift starting soon",      timing: "10 min before shift",
        defaults: { essential: false, balanced: true,  detailed: true  } },
      { key: "schedule_changed",  title: "Schedule changed",         timing: "Immediate (material) · daily digest (minor)",
        defaults: { essential: true,  balanced: true,  detailed: true  } },
      { key: "missed_clock_in",   title: "Missed clock-in",          timing: "5 min after scheduled start",
        defaults: { essential: true,  balanced: true,  detailed: true  } },
      { key: "end_of_shift",      title: "End-of-shift reminder",    timing: "15 min before scheduled end",
        defaults: { essential: false, balanced: true,  detailed: true  } },
    ],
  },
  {
    group: "time", icon: "clock",
    label: "Time & Corrections",
    sub:   "Missing entries, idle-time review, correction requests, and dispute outcomes.",
    items: [
      { key: "missing_time_entry",      title: "Missing time entry",     timing: "End of day",
        defaults: { essential: true,  balanced: true,  detailed: true  } },
      { key: "idle_review_needed",      title: "Idle-time review needed", timing: "When raised by a reviewer",
        defaults: { essential: true,  balanced: true,  detailed: true  } },
      { key: "correction_update",       title: "Correction request update", timing: "Immediately on status change",
        defaults: { essential: true,  balanced: true,  detailed: true  } },
      { key: "dispute_outcome",         title: "Dispute outcome",        timing: "Immediately",
        defaults: { essential: true,  balanced: true,  detailed: true  } },
    ],
  },
  {
    group: "time_off", icon: "leaf",
    label: "Time Off & Approvals",
    sub:   "Time-off requests, approvals, declines, upcoming leave, and holidays.",
    items: [
      { key: "time_off_submitted", title: "Time-off request submitted", timing: "Immediately",
        defaults: { essential: false, balanced: true,  detailed: true  } },
      { key: "time_off_approved",  title: "Time-off approved",          timing: "Immediately",
        defaults: { essential: true,  balanced: true,  detailed: true  } },
      { key: "time_off_declined",  title: "Time-off declined",          timing: "Immediately (reason required)",
        defaults: { essential: true,  balanced: true,  detailed: true  } },
      { key: "upcoming_leave",     title: "Upcoming approved leave",    timing: "1 day before",
        defaults: { essential: false, balanced: true,  detailed: true  } },
      { key: "public_holiday",     title: "Public holiday reminder",    timing: "1 day before",
        defaults: { essential: false, balanced: true,  detailed: true  } },
      { key: "leave_balance",      title: "Leave balance changed",      timing: "Weekly digest",
        defaults: { essential: false, balanced: false, detailed: true  } },
    ],
  },
  {
    group: "pay", icon: "dollar",
    label: "Pay & Documents",
    sub:   "Payroll cut-offs, timesheet approvals, payslips, payroll issues, and tax documents.",
    items: [
      { key: "payroll_cutoff",       title: "Payroll cut-off reminder", timing: "24 hours before cut-off",
        defaults: { essential: true,  balanced: true,  detailed: true  } },
      { key: "timesheet_approval",   title: "Timesheet approval needed", timing: "When approval is needed",
        defaults: { essential: true,  balanced: true,  detailed: true  } },
      { key: "payslip_available",    title: "Payslip available",        timing: "When published",
        defaults: { essential: true,  balanced: true,  detailed: true  },
        locked: { reason: "In-app delivery is required by workspace policy.", channelLock: ["in_app"] } },
      { key: "payroll_issue",        title: "Payroll issue",            timing: "Immediately (overrides quiet hours)",
        defaults: { essential: true,  balanced: true,  detailed: true  },
        locked: { reason: "Critical alert — cannot be disabled." }, critical: true },
      { key: "tax_document",         title: "Tax document available",   timing: "When published",
        defaults: { essential: true,  balanced: true,  detailed: true  },
        locked: { reason: "In-app delivery is required by workspace policy.", channelLock: ["in_app"] } },
    ],
  },
  {
    group: "security", icon: "shield",
    label: "Account & Data Security",
    sub:   "Sign-ins, password and MFA changes, policy acknowledgements, and unusual data access.",
    items: [
      { key: "new_signin",           title: "New sign-in from new device", timing: "Immediately (overrides quiet hours)",
        defaults: { essential: true,  balanced: true,  detailed: true  },
        locked: { reason: "Required security alert." }, critical: true },
      { key: "password_changed",     title: "Password changed",         timing: "Immediately (overrides quiet hours)",
        defaults: { essential: true,  balanced: true,  detailed: true  },
        locked: { reason: "Required security alert." }, critical: true },
      { key: "mfa_changed",          title: "MFA changed",              timing: "Immediately (overrides quiet hours)",
        defaults: { essential: true,  balanced: true,  detailed: true  },
        locked: { reason: "Required security alert." }, critical: true },
      { key: "policy_ack_required",  title: "Policy acknowledgement required", timing: "Immediately + daily until complete",
        defaults: { essential: true,  balanced: true,  detailed: true  },
        locked: { reason: "Required policy notice." }, critical: true },
      { key: "data_export_ready",    title: "Data export ready",        timing: "When ready",
        defaults: { essential: true,  balanced: true,  detailed: true  },
        locked: { reason: "Required so you can collect your export before the link expires." } },
      { key: "dsar_status",          title: "DSAR status update",       timing: "Immediately on status change",
        defaults: { essential: true,  balanced: true,  detailed: true  },
        locked: { reason: "Required regulatory notice." } },
      { key: "unusual_data_access",  title: "Unusual data access detected", timing: "Immediately (overrides quiet hours)",
        defaults: { essential: true,  balanced: true,  detailed: true  },
        locked: { reason: "Required security alert." }, critical: true },
    ],
  },
];

const FLAT_ITEMS = CATALOG.flatMap(g => g.items.map(it => ({ ...it, group: g.group })));

const DEFAULT_CHANNELS = {
  in_app: true,    // locked — always on
  email:  true,
  push:   true,
  sms:    false,   // opt-in only
};

const POSTURES = [
  { value: "essential", label: "Required alerts only",
    desc: "Only required security, payroll, policy, account, critical schedule, and required document notices.",
    icon: "shield" },
  { value: "balanced",  label: "Workday reminders + required alerts",
    desc: "Practical schedule, approval, time-off and payroll reminders. Recommended.",
    icon: "zap", recommended: true },
  { value: "detailed",  label: "More frequent reminders + required alerts",
    desc: "Stronger prompting with extra workday reminders and digest emails.",
    icon: "bell" },
  { value: "custom",    label: "Configure each notification individually",
    desc: "Per-category controls and channel overrides where allowed.",
    icon: "filter" },
];

const HISTORY_STATUS = {
  delivered: { label: "Delivered", tone: "info"   },
  read:      { label: "Read",      tone: "on"     },
  failed:    { label: "Failed",    tone: "danger" },
  suppressed:{ label: "Suppressed", tone: "locked" },
  required:  { label: "Required",  tone: "purple" },
};

const HISTORY_FILTERS = {
  ranges: [
    { value: "7",  label: "Last 7 days" },
    { value: "30", label: "Last 30 days" },
    { value: "90", label: "Last 90 days" },
  ],
  categories: ["all", "schedule", "time", "time_off", "pay", "security", "policy", "data"],
  statuses:   ["all", "delivered", "read", "failed", "suppressed", "required"],
};

// Returns the effective enabled-state for an item, given posture + custom overrides.
function effectiveEnabled(item, posture, overrides) {
  if (item.locked) return true;                    // required notices always on
  if (posture === "custom") {
    const o = overrides?.[item.key];
    if (o && typeof o.enabled === "boolean") return o.enabled;
    return item.defaults.balanced;                  // custom inherits balanced as base
  }
  return item.defaults[posture] ?? false;
}

// ═════════════════════════════════════════════════════════════════════════════
// MAIN COMPONENT
// ═════════════════════════════════════════════════════════════════════════════
export default function NotificationsModule() {
  const { C } = useTheme();
  const [loading, setLoading]   = useState(true);
  const [saving, setSaving]     = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  // Preference state — fetched and persisted via /notifications/preferences.
  const [posture,    setPosture]    = useState("balanced");
  const [channels,   setChannels]   = useState(DEFAULT_CHANNELS);
  const [quietOn,    setQuietOn]    = useState(true);
  const [quietStart, setQuietStart] = useState("20:00");
  const [quietEnd,   setQuietEnd]   = useState("07:00");
  const [tz,         setTz]         = useState(browserTZ());
  const [overrides,  setOverrides]  = useState({});
  const [openGroups, setOpenGroups] = useState(() =>
    Object.fromEntries(CATALOG.map(g => [g.group, true]))
  );

  // ── Load on mount ─────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await API.get("/notifications/preferences");
        if (cancelled) return;
        if (data?.posture) setPosture(data.posture);
        if (data?.channels) setChannels({ ...DEFAULT_CHANNELS, ...data.channels });
        if (typeof data?.quiet_hours_enabled === "boolean") setQuietOn(data.quiet_hours_enabled);
        if (data?.quiet_hours_start) setQuietStart(data.quiet_hours_start);
        if (data?.quiet_hours_end)   setQuietEnd(data.quiet_hours_end);
        if (data?.timezone) setTz(data.timezone);
        if (data?.overrides) setOverrides(data.overrides);
      } catch {
        // pre-launch: backend may not be live yet — keep defaults silently
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // ── Save helper ───────────────────────────────────────────────────────────
  const patch = async (delta) => {
    setSaving(true);
    try {
      await API.patch("/notifications/preferences", delta);
      toast.success("Updated");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't save — try again");
      throw err;
    } finally {
      setSaving(false);
    }
  };

  // ── Action handlers ───────────────────────────────────────────────────────
  const onPostureChange = async (next) => {
    if (next === posture) return;
    const prev = posture;
    setPosture(next);
    try { await patch({ posture: next }); } catch { setPosture(prev); }
  };

  const onChannelChange = async (key, value) => {
    if (key === "in_app") return;                    // locked
    const prev = channels;
    const next = { ...channels, [key]: value };
    setChannels(next);
    try { await patch({ channels: next }); } catch { setChannels(prev); }
  };

  const onQuietToggle = async (value) => {
    const prev = quietOn;
    setQuietOn(value);
    try { await patch({ quiet_hours_enabled: value }); } catch { setQuietOn(prev); }
  };

  const onQuietRange = async (which, value) => {
    if (which === "start") setQuietStart(value); else setQuietEnd(value);
    try {
      await patch({
        quiet_hours_start: which === "start" ? value : quietStart,
        quiet_hours_end:   which === "end"   ? value : quietEnd,
      });
    } catch {
      if (which === "start") setQuietStart(quietStart); else setQuietEnd(quietEnd);
    }
  };

  const onItemToggle = async (item, value) => {
    if (item.locked || posture !== "custom") return;
    const prev = overrides;
    const next = { ...overrides, [item.key]: { ...overrides[item.key], enabled: value } };
    setOverrides(next);
    try { await patch({ overrides: next }); } catch { setOverrides(prev); }
  };

  const onResetCustom = async () => {
    if (!Object.keys(overrides).length) return;
    if (!window.confirm("Reset all custom overrides back to posture defaults?")) return;
    const prev = overrides;
    setOverrides({});
    try { await patch({ overrides: {} }); } catch { setOverrides(prev); }
  };

  const onSendTest = async () => {
    try {
      await API.post("/notifications/test");
      toast.success("Test notification sent — check your inbox");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't send test");
    }
  };

  // ── Derived ──────────────────────────────────────────────────────────────
  const customOverrideCount = useMemo(
    () => Object.values(overrides).filter(o => typeof o?.enabled === "boolean").length,
    [overrides]
  );

  const summaryChannels = useMemo(() => {
    const out = ["In-app"];
    if (channels.email) out.push("Email");
    if (channels.push)  out.push("Push");
    if (channels.sms)   out.push("SMS");
    return out.join(" · ");
  }, [channels]);

  const enabledCounts = useMemo(() => {
    const counts = {};
    CATALOG.forEach(g => {
      const enabled = g.items.filter(it => effectiveEnabled(it, posture, overrides)).length;
      counts[g.group] = { enabled, total: g.items.length };
    });
    return counts;
  }, [posture, overrides]);

  // ─── Render ──────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <CCtx.Provider value={C}>
        <div style={{ maxWidth: 1100, margin: "0 auto", padding: "40px 24px" }}>
          <Spinner />
        </div>
      </CCtx.Provider>
    );
  }

  return (
    <CCtx.Provider value={C}>
    <div style={{
      maxWidth: 1100, margin: "0 auto", padding: "8px 4px 32px",
      fontFamily: "'DM Sans', system-ui, sans-serif",
    }}>
      {/* ─── Header ────────────────────────────────────────────────────── */}
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
              Account · Notifications
            </p>
            <h1 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: "2px 0 0" }}>
              Notifications
            </h1>
            <p style={{ fontSize: 13, color: C.textSub, margin: "6px 0 0", lineHeight: 1.6, maxWidth: 720 }}>
              Choose how Temveris keeps you informed. Some notifications are required by your
              workspace and cannot be turned off — including workspace policies, account security,
              payroll, and critical schedule changes.
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, flexShrink: 0, flexWrap: "wrap" }}>
            <Button variant="secondary" icon="inbox"
                    onClick={() => setShowHistory(s => !s)}>
              {showHistory ? "Hide history" : "View history"}
            </Button>
            <Button variant="secondary" icon="send" onClick={onSendTest} disabled={saving}>
              Send test
            </Button>
            {posture === "custom" && customOverrideCount > 0 && (
              <Button variant="ghost" icon="refresh" onClick={onResetCustom}>
                Reset custom settings
              </Button>
            )}
          </div>
        </div>
      </div>

      {/* ─── Setup Summary ─────────────────────────────────────────────── */}
      <Card title="Your setup" icon="info" subtitle="A snapshot of how you'll be notified right now.">
        <div style={{
          display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 16,
        }}>
          {[
            { label: "Time zone",       value: tz,                                icon: "clock" },
            { label: "Default channels", value: summaryChannels,                  icon: "mail"  },
            { label: "Quiet hours",      value: quietOn ? `${quietStart} – ${quietEnd}` : "Off", icon: "moon" },
            { label: "Required alerts",  value: "Always on",                       icon: "shield" },
          ].map((row) => (
            <div key={row.label} style={{
              padding: "12px 14px", borderRadius: 8,
              border: `1px solid ${C.borderLight}`, background: C.bg,
              display: "flex", alignItems: "flex-start", gap: 10,
            }}>
              <Icon n={row.icon} s={16} c={C.brand} />
              <div style={{ minWidth: 0, flex: 1 }}>
                <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                            textTransform: "uppercase", letterSpacing: 0.5, margin: 0 }}>
                  {row.label}
                </p>
                <p style={{ fontSize: 13, color: C.text, margin: "2px 0 0", fontWeight: 600,
                            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {row.value}
                </p>
              </div>
            </div>
          ))}
        </div>
      </Card>

      {/* ─── Posture selector ──────────────────────────────────────────── */}
      <Card title="How much do you want to be notified?" icon="zap">
        <div role="radiogroup" aria-label="Notification posture"
             style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
          {POSTURES.map(p => {
            const active = posture === p.value;
            return (
              <button key={p.value}
                role="radio"
                aria-checked={active}
                onClick={() => onPostureChange(p.value)}
                disabled={saving}
                style={{
                  textAlign: "left", display: "flex", flexDirection: "column", gap: 8,
                  padding: 14, borderRadius: 10,
                  border: `2px solid ${active ? C.brand : C.border}`,
                  background: active ? C.brandBg : C.surface,
                  cursor: "pointer", fontFamily: "inherit",
                  transition: "border-color .18s, background .18s",
                }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Icon n={p.icon} s={16} c={active ? C.brand : C.textSub} />
                  <p style={{ flex: 1, fontSize: 13, fontWeight: 800,
                              color: active ? C.brand : C.text, margin: 0 }}>
                    {p.label}
                  </p>
                  {p.recommended && <Pill tone="on">Recommended</Pill>}
                  {active && <Icon n="check" s={16} c={C.brand} />}
                </div>
                <p style={{ fontSize: 12, color: C.textSub, margin: 0, lineHeight: 1.5 }}>
                  {p.desc}
                </p>
              </button>
            );
          })}
        </div>
      </Card>

      {/* ─── Notification catalog ─────────────────────────────────────── */}
      {CATALOG.map(group => {
        const open = openGroups[group.group];
        const counts = enabledCounts[group.group];
        return (
          <Card
            key={group.group}
            title={group.label}
            icon={group.icon}
            subtitle={group.sub}
            action={
              <button type="button"
                onClick={() => setOpenGroups(g => ({ ...g, [group.group]: !g[group.group] }))}
                aria-label={open ? "Collapse" : "Expand"}
                style={{
                  display: "flex", alignItems: "center", gap: 8,
                  padding: "6px 10px", borderRadius: 6, border: "none", background: "transparent",
                  fontFamily: "inherit", fontSize: 12, color: C.textSub, cursor: "pointer",
                }}>
                <span style={{ fontWeight: 700, color: C.brand }}>
                  {counts.enabled}/{counts.total} on
                </span>
                <Icon n={open ? "chevup" : "chevdown"} s={14} c={C.textSub} />
              </button>
            }
            padding={open ? 0 : 18}
          >
            {!open ? (
              <p style={{ fontSize: 12, color: C.textSub, margin: 0 }}>
                {posture === "custom"
                  ? `${counts.enabled} of ${counts.total} notifications on. Click to manage.`
                  : `Following ${posture} defaults — ${counts.enabled} of ${counts.total} on.`}
              </p>
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
                {group.items.map((it, i) => {
                  const enabled = effectiveEnabled(it, posture, overrides);
                  const locked = !!it.locked;
                  const editable = posture === "custom" && !locked;
                  return (
                    <li key={it.key} style={{
                      display: "flex", alignItems: "flex-start", gap: 14,
                      padding: "14px 24px",
                      borderBottom: i < group.items.length - 1 ? `1px solid ${C.borderLight}` : "none",
                    }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0,
                                    display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                          {it.title}
                          {it.critical && <Pill tone="danger" icon="zap">Critical</Pill>}
                          {locked && <Pill tone="locked" icon="lock">Required</Pill>}
                          {!locked && enabled && <Pill tone="on" icon="check">On</Pill>}
                          {!locked && !enabled && <Pill tone="off">Off</Pill>}
                        </p>
                        <p style={{ fontSize: 12, color: C.textSub, margin: "4px 0 0", lineHeight: 1.5 }}>
                          {it.timing}
                        </p>
                        {locked && (
                          <p style={{
                            fontSize: 11, color: C.amber, margin: "8px 0 0",
                            padding: "8px 10px", borderRadius: 6,
                            background: C.amberBg, lineHeight: 1.5,
                          }} aria-describedby={`lock-${it.key}`}>
                            <span id={`lock-${it.key}`}>{it.locked.reason}</span>
                          </p>
                        )}
                      </div>
                      <Toggle
                        on={enabled}
                        disabled={!editable}
                        onChange={(v) => onItemToggle(it, v)}
                        ariaLabel={`Toggle ${it.title}`}
                      />
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>
        );
      })}

      {/* ─── Channel preferences ───────────────────────────────────────── */}
      <Card title="Where you receive notifications" icon="mail"
            subtitle="In-app delivery cannot be turned off — it's your permanent notification record. Other channels are optional and may still send required notices where workspace policy requires.">
        <div style={{ display: "grid", gap: 10 }}>
          {[
            { key: "in_app", icon: "monitor",    label: "In-app",
              sub: "Always on. Every notification is delivered here." },
            { key: "email",  icon: "mail",       label: "Email",
              sub: "Email summaries and links to action items." },
            { key: "push",   icon: "smartphone", label: "Mobile push",
              sub: "Schedule, approvals, mentions, and DMs." },
            { key: "sms",    icon: "phone",      label: "SMS",
              sub: "Reserved for critical alerts. Requires a verified phone number." },
          ].map((ch) => {
            const isLocked = ch.key === "in_app";
            const value = !!channels[ch.key];
            return (
              <div key={ch.key} style={{
                display: "flex", alignItems: "center", gap: 14,
                padding: "14px 16px", borderRadius: 8,
                border: `1px solid ${C.borderLight}`,
                background: isLocked ? C.bg : C.surface,
              }}>
                <div style={{
                  width: 36, height: 36, borderRadius: 8,
                  background: value ? C.brandBg : C.borderLight,
                  display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
                }}>
                  <Icon n={ch.icon} s={18} c={value ? C.brand : C.textSub} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0,
                              display: "flex", alignItems: "center", gap: 8 }}>
                    {ch.label}
                    {isLocked && <Pill tone="locked" icon="lock">Always on</Pill>}
                  </p>
                  <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}>{ch.sub}</p>
                </div>
                <Toggle on={value} disabled={isLocked || saving}
                        onChange={(v) => onChannelChange(ch.key, v)}
                        ariaLabel={`Toggle ${ch.label} channel`} />
              </div>
            );
          })}
        </div>
      </Card>

      {/* ─── Quiet hours ───────────────────────────────────────────────── */}
      <Card title="Quiet hours" icon="moon"
            subtitle="During quiet hours, optional notifications are suppressed. Critical alerts — security, payroll issues, urgent schedule changes, required policy notices, DSAR updates, and unusual data access — are always delivered.">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                       gap: 16, marginBottom: 12 }}>
          <div>
            <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>
              Enable quiet hours
            </p>
            <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}>
              Suppressed notifications still appear in history with status “Suppressed (quiet hours)”.
            </p>
          </div>
          <Toggle on={quietOn} disabled={saving} onChange={onQuietToggle}
                  ariaLabel="Enable quiet hours" />
        </div>
        {quietOn && (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
            <label style={{ display: "block" }}>
              <span style={{ display: "block", fontSize: 12, fontWeight: 600,
                              color: C.textSub, marginBottom: 6 }}>Start</span>
              <input type="time" value={quietStart}
                     onChange={(e) => setQuietStart(e.target.value)}
                     onBlur={() => onQuietRange("start", quietStart)}
                     style={{
                       width: "100%", padding: "9px 12px", borderRadius: C.rSm,
                       border: `1px solid ${C.border}`, background: C.bg,
                       fontSize: 14, fontFamily: "inherit",
                     }} />
            </label>
            <label style={{ display: "block" }}>
              <span style={{ display: "block", fontSize: 12, fontWeight: 600,
                              color: C.textSub, marginBottom: 6 }}>End</span>
              <input type="time" value={quietEnd}
                     onChange={(e) => setQuietEnd(e.target.value)}
                     onBlur={() => onQuietRange("end", quietEnd)}
                     style={{
                       width: "100%", padding: "9px 12px", borderRadius: C.rSm,
                       border: `1px solid ${C.border}`, background: C.bg,
                       fontSize: 14, fontFamily: "inherit",
                     }} />
            </label>
          </div>
        )}
      </Card>

      {/* ─── History panel (inline) ────────────────────────────────────── */}
      {showHistory && <HistoryPanel onClose={() => setShowHistory(false)} />}
    </div>
    </CCtx.Provider>
  );
}

// ═════════════════════════════════════════════════════════════════════════════
// HISTORY PANEL  (spec §9)
// ═════════════════════════════════════════════════════════════════════════════
function HistoryPanel({ onClose }) {
  const C = useC();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [range,  setRange]    = useState("30");
  const [cat,    setCat]      = useState("all");
  const [status, setStatus]   = useState("all");
  const [q,      setQ]        = useState("");
  const [expanded, setExpanded] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const params = { days: range };
      if (cat    !== "all") params.category = cat;
      if (status !== "all") params.status   = status;
      if (q.trim()) params.q = q.trim();
      const { data } = await API.get("/notifications/history", { params });
      setRows(Array.isArray(data) ? data : []);
    } catch {
      setRows([]);
    } finally {
      setLoading(false);
    }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [range, cat, status]);

  return (
    <Card title="Notification history" icon="inbox"
          subtitle="The complete record of notifications delivered to you. Required notifications are retained per workspace policy; optional history is kept for 12 months."
          padding={0}
          action={<Button variant="ghost" icon="x" onClick={onClose}>Close</Button>}>
      <div style={{
        padding: "12px 24px", borderBottom: `1px solid ${C.borderLight}`,
        display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center",
      }}>
        <select value={range} onChange={(e) => setRange(e.target.value)}
          style={inputStyle(C)} aria-label="Date range">
          {HISTORY_FILTERS.ranges.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
        </select>
        <select value={cat} onChange={(e) => setCat(e.target.value)}
          style={inputStyle(C)} aria-label="Category">
          {HISTORY_FILTERS.categories.map(c => (
            <option key={c} value={c}>{c === "all" ? "All categories" : c.replace("_", " ")}</option>
          ))}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)}
          style={inputStyle(C)} aria-label="Status">
          {HISTORY_FILTERS.statuses.map(s => (
            <option key={s} value={s}>{s === "all" ? "All statuses" : (HISTORY_STATUS[s]?.label || s)}</option>
          ))}
        </select>
        <input type="search" value={q} placeholder="Search notifications…"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") load(); }}
          style={{ ...inputStyle(C), flex: 1, minWidth: 180 }}
          aria-label="Search" />
        <Button variant="secondary" icon="refresh" onClick={load} disabled={loading}>
          Refresh
        </Button>
      </div>

      {loading ? <Spinner /> : rows.length === 0 ? (
        <EmptyState icon="inbox"
          title="Nothing here yet"
          body="Notifications you receive will show up here with full content, channel, and delivery status." />
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {rows.map((r) => {
            const isOpen = expanded === r.id;
            const st = HISTORY_STATUS[r.delivery_status] || HISTORY_STATUS.delivered;
            return (
              <li key={r.id} style={{ borderBottom: `1px solid ${C.borderLight}` }}>
                <button type="button"
                  onClick={() => setExpanded(isOpen ? null : r.id)}
                  aria-expanded={isOpen}
                  style={{
                    width: "100%", display: "grid",
                    gridTemplateColumns: "minmax(140px, 170px) minmax(0, 2fr) minmax(0, 1fr) auto auto",
                    gap: 12, alignItems: "center",
                    padding: "14px 24px",
                    background: "none", border: "none", textAlign: "left",
                    cursor: "pointer", fontFamily: "inherit",
                  }}>
                  <span style={{ fontSize: 12, color: C.textSub }}>
                    {fmtDateTime(r.sent_at)}
                  </span>
                  <span style={{ minWidth: 0 }}>
                    <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0,
                                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {r.title || "(no title)"}
                    </p>
                    <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                      {(r.category || "").replace("_", " ")}
                    </p>
                  </span>
                  <span style={{ fontSize: 11, color: C.textSub,
                                 overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {(r.channels || [r.channel]).filter(Boolean).join(" · ") || "in-app"}
                  </span>
                  <Pill tone={st.tone}>{st.label}</Pill>
                  <Icon n={isOpen ? "chevup" : "chevdown"} s={14} c={C.textSub} />
                </button>
                {isOpen && (
                  <div style={{
                    padding: "0 24px 18px 24px", color: C.text,
                    fontSize: 13, lineHeight: 1.6,
                    background: C.bg,
                  }}>
                    {r.body && (
                      <div style={{ padding: "12px 14px", background: C.surface,
                                    border: `1px solid ${C.borderLight}`,
                                    borderRadius: 8, marginBottom: 10 }}>
                        {r.body}
                      </div>
                    )}
                    <div style={{ display: "flex", gap: 16, fontSize: 11, color: C.textSub, flexWrap: "wrap" }}>
                      {r.read_at && <span>Read {fmtDateTime(r.read_at)}</span>}
                      {r.suppressed_reason && <span>Suppressed: {r.suppressed_reason}</span>}
                      {r.action_url && (
                        <a href={r.action_url} target="_blank" rel="noopener noreferrer"
                           style={{ color: C.brand, fontWeight: 700 }}>
                          Open action ↗
                        </a>
                      )}
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function inputStyle(C) {
  return {
    padding: "8px 12px", borderRadius: C?.rSm ?? 6,
    border: `1px solid ${C?.border ?? "#E4E7EC"}`, background: C?.bg ?? "#F5F7FA",
    fontSize: 13, color: C?.text ?? "#101828", fontFamily: "inherit",
    outline: "none",
  };
}
