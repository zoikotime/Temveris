/**
 * SettingsModule.jsx — worker-facing Account → Settings page.
 *
 * Four cards, instant save, no global Save button:
 *   1. Display & Accessibility — theme, text size, high contrast, time format
 *   2. Language & Region       — language, timezone, date/number format, first day
 *   3. Workday Preferences     — landing page, next-shift, weekly hours, calendar view, ? overlay
 *   4. App Behavior            — hide pay, presentation mode, confirm corrections, tips, announcements, calendars
 *
 * Reset to Defaults preserves Presentation Mode (spec §11) so we never
 * silently expose sensitive values during a screen share. Locked settings
 * must explain *why* (current value + policy ref + View policy link); never
 * just disable the control.
 *
 * Endpoints used (graceful 404 fallback so the page works pre-launch):
 *   GET   /worker/settings
 *   PATCH /worker/settings
 *   POST  /worker/settings/reset
 *   GET   /worker/settings/policy-locks
 *   GET   /worker/calendar-connections
 *   POST  /worker/calendar-connections
 *   DELETE /worker/calendar-connections/{id}
 *
 * Dashboard integration: the parent passes onPreferencesChange so theme,
 * text size, and presentation mode can take effect across the worker
 * dashboard immediately, without waiting for a route remount.
 */

import React, { useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import API from "../api/axios";
import { useTheme } from "../context/ThemeContext";

// Module-level context so sub-components get C without prop-drilling.
const CCtx = React.createContext(null);
const useC = () => React.useContext(CCtx);

// ─── inline icon set ────────────────────────────────────────────────────────
const ICON = {
  settings:   "M12 15a3 3 0 100-6 3 3 0 000 6z M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z",
  sun:        "M12 18a6 6 0 100-12 6 6 0 000 12z M12 2v4 M12 18v4 M4.22 4.22l2.83 2.83 M16.95 16.95l2.83 2.83 M2 12h4 M18 12h4 M4.22 19.78l2.83-2.83 M16.95 7.05l2.83-2.83",
  moon:       "M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z",
  monitor:    "M2 3h20a1 1 0 011 1v13a1 1 0 01-1 1H2a1 1 0 01-1-1V4a1 1 0 011-1z M8 21h8 M12 17v4",
  globe:      "M12 2a10 10 0 100 20A10 10 0 0012 2z M2 12h20 M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z",
  briefcase:  "M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2",
  zap:        "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
  eye:        "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z M12 15a3 3 0 100-6 3 3 0 000 6z",
  eyeoff:     "M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24 M1 1l22 22",
  lock:       "M19 11H5a2 2 0 00-2 2v7a2 2 0 002 2h14a2 2 0 002-2v-7a2 2 0 00-2-2z M7 11V7a5 5 0 0110 0v4",
  refresh:    "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10 M1 14l4.64 4.36A9 9 0 0020.49 15",
  check:      "M20 6L9 17l-5-5",
  x:          "M18 6L6 18 M6 6l12 12",
  external:   "M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6 M15 3h6v6 M10 14L21 3",
  calendar:   "M8 2v4 M16 2v4 M3 10h18 M5 4h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V6a2 2 0 012-2z",
  plus:       "M12 5v14 M5 12h14",
  trash:      "M3 6h18 M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2 M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6",
  user:       "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2 M12 11a4 4 0 100-8 4 4 0 000 8",
  bell:       "M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9 M13.73 21a2 2 0 01-3.46 0",
  shield:     "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  device:     "M5 2h14a2 2 0 012 2v16a2 2 0 01-2 2H5a2 2 0 01-2-2V4a2 2 0 012-2z M12 18h.01",
  database:   "M12 3a9 3 0 100 6 9 3 0 000-6z M3 5v6a9 3 0 0018 0V5 M3 11v6a9 3 0 0018 0v-6",
  chevright:  "M9 18l6-6-6-6",
  info:       "M12 22a10 10 0 100-20 10 10 0 000 20z M12 16v-4 M12 8h.01",
  warning:    "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
  keyboard:   "M2 5h20a1 1 0 011 1v12a1 1 0 01-1 1H2a1 1 0 01-1-1V6a1 1 0 011-1z M6 9h.01 M10 9h.01 M14 9h.01 M18 9h.01 M6 13h.01 M10 13h.01 M14 13h.01 M18 13h.01 M6 17h12",
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

// Modal — overlay-click + ESC close, body-scroll locked.
const Modal = ({ open, onClose, title, children, footer, size = "small" }) => {
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
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 20,
      }}>
      <div onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%", maxWidth: widths[size] || widths.small,
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
// CONSTANTS  (kept in lock-step with backend; see /worker/settings/policy-locks)
// ═════════════════════════════════════════════════════════════════════════════
const TIMEZONE_CHOICES = (() => {
  try {
    if (typeof Intl?.supportedValuesOf === "function") {
      return Intl.supportedValuesOf("timeZone");
    }
  } catch { /* fall through */ }
  return [
    "UTC", "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo",
    "Europe/London", "Europe/Paris", "Europe/Berlin",
    "America/New_York", "America/Chicago", "America/Los_Angeles",
    "Australia/Sydney",
  ];
})();

const LANGUAGES = [
  { value: "en-US",   label: "English (US)" },
  { value: "en-GB",   label: "English (UK)" },
  { value: "es",      label: "Español" },
  { value: "fr",      label: "Français" },
  { value: "de",      label: "Deutsch" },
  { value: "pt-BR",   label: "Português (Brasil)" },
  { value: "ja",      label: "日本語" },
  { value: "zh-Hans", label: "简体中文" },
];

const DATE_FORMATS = [
  { value: "MM/DD/YYYY", label: "04/28/2026 (MM/DD/YYYY)" },
  { value: "DD/MM/YYYY", label: "28/04/2026 (DD/MM/YYYY)" },
  { value: "YYYY-MM-DD", label: "2026-04-28 (YYYY-MM-DD)" },
  { value: "D MMM YYYY", label: "28 Apr 2026" },
];

const NUMBER_FORMATS = [
  { value: "1,234.56", label: "1,234.56 (US)" },
  { value: "1.234,56", label: "1.234,56 (EU)" },
  { value: "1 234,56", label: "1 234,56 (FR)" },
  { value: "1’234.56", label: "1’234.56 (CH)" },
];

const TEXT_SIZES = [
  { value: "compact",  label: "Compact"  },
  { value: "default",  label: "Default"  },
  { value: "large",    label: "Large"    },
  { value: "xlarge",   label: "Extra Large" },
];

const TIME_FORMATS = [
  { value: "12h",        label: "12-hour" },
  { value: "24h",        label: "24-hour" },
  { value: "workspace",  label: "Workspace default" },
];

const FIRST_DAYS = [
  { value: "sunday",    label: "Sunday" },
  { value: "monday",    label: "Monday" },
  { value: "workspace", label: "Workspace default" },
];

const LANDING_PAGES = [
  { value: "my-work",     label: "Home (Today)" },
  { value: "activity-log", label: "My Activity" },
  { value: "my-schedule",  label: "My Schedule" },
];

const CALENDAR_VIEWS = [
  { value: "day",   label: "Day"   },
  { value: "week",  label: "Week"  },
  { value: "month", label: "Month" },
];

const DEFAULTS = {
  theme:                            "system",
  text_size:                        "default",
  high_contrast:                    false,
  time_format:                      "workspace",
  language:                         "en-US",
  timezone:                         (() => {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; }
    catch { return "UTC"; }
  })(),
  date_format:                      "MM/DD/YYYY",
  number_format:                    "1,234.56",
  first_day_of_week:                "workspace",
  default_landing_page:             "my-work",
  show_next_shift:                  true,
  show_weekly_hours:                true,
  calendar_view:                    "week",
  show_keyboard_shortcuts:          false,
  hide_payroll_values:              true,
  presentation_mode:                false,
  correction_confirmation_enabled:  true,
  product_tips_enabled:             true,
  feature_announcements_enabled:    true,
};

// Reset scope per spec §14: only personal preferences. Presentation Mode is
// EXCLUDED so it stays on if active during a screen share.
const RESET_KEYS = [
  "theme", "text_size", "high_contrast", "time_format",
  "language", "timezone", "date_format", "number_format", "first_day_of_week",
  "default_landing_page", "show_next_shift", "show_weekly_hours",
  "calendar_view", "show_keyboard_shortcuts",
  "hide_payroll_values",
  "correction_confirmation_enabled",
  "product_tips_enabled", "feature_announcements_enabled",
];

// ═════════════════════════════════════════════════════════════════════════════
// MAIN COMPONENT
// ═════════════════════════════════════════════════════════════════════════════
export default function SettingsModule({ onJumpToTab, onPreferencesChange }) {
  const { C, setDark } = useTheme();
  const [loading, setLoading] = useState(true);
  const [values,  setValues]  = useState(DEFAULTS);
  const [locks,   setLocks]   = useState({});      // { field: { current, policy_name, policy_version, policy_url } }
  const [calendars, setCalendars] = useState([]);
  const [showReset,    setShowReset]    = useState(false);
  const [showCalendar, setShowCalendar] = useState(false);
  const [showShortcuts, setShowShortcuts] = useState(false);

  // ── Load on mount ────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, l, c] = await Promise.allSettled([
          API.get("/worker/settings"),
          API.get("/worker/settings/policy-locks"),
          API.get("/worker/calendar-connections"),
        ]);
        if (cancelled) return;
        if (s.status === "fulfilled" && s.value?.data) {
          setValues(v => ({ ...v, ...s.value.data }));
        }
        if (l.status === "fulfilled" && l.value?.data) {
          setLocks(l.value.data || {});
        }
        if (c.status === "fulfilled" && c.value?.data) {
          setCalendars(Array.isArray(c.value.data) ? c.value.data : []);
        }
      } catch { /* keep defaults silently */ }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  }, []);

  // ── Notify parent so the Dashboard can apply preferences across its
  //    surface immediately (theme, sizing, formatting, locale). Each field
  //    listed here is consumed somewhere outside the Settings page, so any
  //    change must flow back to the parent without waiting for a remount.
  useEffect(() => {
    onPreferencesChange?.({
      theme:                  values.theme,
      text_size:              values.text_size,
      high_contrast:          values.high_contrast,
      presentation_mode:      values.presentation_mode,
      hide_payroll_values:    values.hide_payroll_values,
      show_keyboard_shortcuts: values.show_keyboard_shortcuts,
      time_format:            values.time_format,
      date_format:            values.date_format,
      number_format:          values.number_format,
      timezone:               values.timezone,
      language:               values.language,
      first_day_of_week:      values.first_day_of_week,
    });
  }, [
    values.theme, values.text_size, values.high_contrast,
    values.presentation_mode, values.hide_payroll_values,
    values.show_keyboard_shortcuts,
    values.time_format, values.date_format, values.number_format,
    values.timezone, values.language, values.first_day_of_week,
    onPreferencesChange,
  ]);

  // ── Global "?" shortcut overlay (spec §9 + §22) ─────────────────────────
  useEffect(() => {
    if (!values.show_keyboard_shortcuts) return;
    const onKey = (e) => {
      // Don't hijack the key if the user is typing in a field.
      const tag = (e.target?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || e.target?.isContentEditable) return;
      if (e.key === "?" || (e.shiftKey && e.key === "/")) {
        e.preventDefault();
        setShowShortcuts(s => !s);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [values.show_keyboard_shortcuts]);

  // ── Save helper — optimistic with rollback on lock-rejection ────────────
  const patchOne = async (key, value) => {
    const prev = values[key];
    if (prev === value) return;
    setValues(v => ({ ...v, [key]: value }));

    // Apply theme immediately without waiting for the API round-trip.
    if (key === "theme") {
      if (value === "dark")   setDark(true);
      else if (value === "light") setDark(false);
      else setDark(window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false);
    }

    try {
      await API.patch("/worker/settings", { [key]: value });
      toast.success("Updated");
    } catch (err) {
      const data = err?.response?.data;
      if (data?.error === "SETTING_LOCKED_BY_WORKSPACE" && data?.field === key) {
        // Re-apply the workspace's current value and surface the policy lock.
        setValues(v => ({ ...v, [key]: data.current_value ?? prev }));
        setLocks(l => ({
          ...l,
          [key]: {
            current_value:  data.current_value,
            policy_name:    data.policy_ref || l[key]?.policy_name || "Workspace policy",
            policy_version: data.policy_version || l[key]?.policy_version,
            policy_url:     data.policy_url || l[key]?.policy_url,
          },
        }));
        toast.error("This setting is managed by your workspace.");
      } else {
        setValues(v => ({ ...v, [key]: prev }));
        toast.error(data?.detail || data?.message || "Couldn't save — try again");
      }
    }
  };

  // ── Reset to defaults (preserves Presentation Mode) ─────────────────────
  const doReset = async () => {
    const presPrev = values.presentation_mode;
    const next = { ...values };
    RESET_KEYS.forEach(k => { next[k] = DEFAULTS[k]; });
    next.presentation_mode = presPrev;     // §14: never silently disable
    setValues(next);
    try {
      await API.post("/worker/settings/reset");
      toast.success("Settings reset to defaults");
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't reset");
    } finally {
      setShowReset(false);
    }
  };

  // ── Calendar connection actions (stubs — no real OAuth in v1) ───────────
  const connectCalendar = async (provider) => {
    try {
      const { data } = await API.post("/worker/calendar-connections", { provider });
      if (data) {
        setCalendars(rows => [data, ...rows]);
        toast.success(`${provider} calendar connected`);
      }
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Couldn't connect calendar");
    }
  };

  const disconnectCalendar = async (id) => {
    if (!window.confirm(
      "Disconnect this calendar? Meeting time may be classified as idle until you reconnect."
    )) return;
    const prev = calendars;
    setCalendars(rows => rows.filter(c => c.id !== id));
    try {
      await API.delete(`/worker/calendar-connections/${id}`);
      toast.success("Calendar disconnected");
    } catch (err) {
      setCalendars(prev);
      toast.error(err?.response?.data?.detail || "Couldn't disconnect");
    }
  };

  // ── Live preview helpers ────────────────────────────────────────────────
  const previewDate = useMemo(() => formatDatePreview(values.date_format), [values.date_format]);
  const previewNumber = useMemo(() => formatNumberPreview(values.number_format), [values.number_format]);

  if (loading) {
    return (
      <CCtx.Provider value={C}>
        <div style={{ maxWidth: 1100, margin: "0 auto", padding: "40px 24px" }}>
          <Spinner />
        </div>
      </CCtx.Provider>
    );
  }

  // ─── Render ─────────────────────────────────────────────────────────────
  return (
    <CCtx.Provider value={C}>
    <div style={{
      maxWidth: 1100, margin: "0 auto", padding: "8px 4px 32px",
      fontFamily: "'DM Sans', system-ui, sans-serif",
    }}>
      {/* ── Header ───────────────────────────────────────────────────── */}
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
            <Icon n="settings" s={22} c="#fff" w={2.2} />
          </div>
          <div style={{ flex: 1, minWidth: 240 }}>
            <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700,
                        letterSpacing: 0.6, textTransform: "uppercase", margin: 0 }}>
              Account · Settings
            </p>
            <h1 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: "2px 0 0" }}>
              Settings
            </h1>
            <p style={{ fontSize: 13, color: C.textSub, margin: "6px 0 0", lineHeight: 1.6, maxWidth: 720 }}>
              Personal preferences for how Temveris looks and behaves for you. Workspace-controlled
              settings are visible but locked — your admin can change them through workspace policy.
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, flexShrink: 0, flexWrap: "wrap" }}>
            <Button variant="secondary" icon="refresh" onClick={() => setShowReset(true)}>
              Reset to defaults
            </Button>
          </div>
        </div>
      </div>

      {/* ── Presentation Mode banner (sticky-feel; only when active) ── */}
      {values.presentation_mode && (
        <div style={{
          padding: "12px 18px", borderRadius: 10, marginBottom: 16,
          background: C.amberBg, border: `1px solid ${C.amber}33`,
          display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
        }}>
          <Icon n="eyeoff" s={20} c={C.amber} />
          <div style={{ flex: 1, minWidth: 200 }}>
            <p style={{ fontSize: 13, fontWeight: 800, color: C.amber, margin: 0 }}>
              Presentation mode is on
            </p>
            <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0", lineHeight: 1.5 }}>
              Sensitive details — pay values, contact info, activity identifiers — are hidden across
              the worker dashboard.
            </p>
          </div>
          <Button variant="secondary" onClick={() => patchOne("presentation_mode", false)}>
            Turn off
          </Button>
        </div>
      )}

      {/* ── Section 1 — Display & Accessibility ──────────────────────── */}
      {/* Text size and High contrast were removed: the OS already handles
          both at a system level (Windows zoom / macOS Display preferences /
          browser Ctrl+/Ctrl-) and exposing duplicate toggles in-app caused
          inconsistent states across pages. Temveris still honours the
          device-level accessibility settings automatically. */}
      <Card title="Display & Accessibility" icon="sun">
        <Row label="Theme" hint={null}>
          <Segmented
            value={values.theme}
            onChange={(v) => patchOne("theme", v)}
            options={[
              { value: "light",  label: "Light",  icon: "sun" },
              { value: "dark",   label: "Dark",   icon: "moon" },
              { value: "system", label: "System", icon: "monitor" },
            ]}
            disabled={isLocked(locks, "theme")}
          />
          {renderLock(locks.theme, "theme", C)}
        </Row>
        <Row label="Time format">
          <Segmented
            value={values.time_format}
            onChange={(v) => patchOne("time_format", v)}
            options={TIME_FORMATS}
            disabled={isLocked(locks, "time_format")}
          />
          {renderLock(locks.time_format, "time_format", C)}
        </Row>
        <p style={{ fontSize: 11, color: C.textLight, margin: "4px 0 0", lineHeight: 1.6 }}>
          Temveris respects your device accessibility settings, including text size,
          high contrast, reduced motion, and screen reader preferences.
        </p>
      </Card>

      {/* ── Section 2 — Language & Region ────────────────────────────── */}
      <Card title="Language & Region" icon="globe">
        <Row label="Language">
          <Select value={values.language}
                  onChange={(v) => patchOne("language", v)}
                  options={LANGUAGES}
                  disabled={isLocked(locks, "language")} />
          {renderLock(locks.language, "language", C)}
        </Row>
        <Row label="Time zone"
             helper="Used for schedules, reports, and notifications. May be locked by workspace policy.">
          {isLocked(locks, "timezone") ? (
            <LockedDisplay value={values.timezone} lock={locks.timezone} field="timezone" />
          ) : (
            <Select value={values.timezone}
                    onChange={(v) => patchOne("timezone", v)}
                    options={TIMEZONE_CHOICES.includes(values.timezone)
                      ? TIMEZONE_CHOICES
                      : [values.timezone, ...TIMEZONE_CHOICES]} />
          )}
        </Row>
        <Row label="Date format" helper={`Preview: ${previewDate}`}>
          <Select value={values.date_format}
                  onChange={(v) => patchOne("date_format", v)}
                  options={DATE_FORMATS}
                  disabled={isLocked(locks, "date_format")} />
          {renderLock(locks.date_format, "date_format", C)}
        </Row>
        <Row label="Number format" helper={`Preview: ${previewNumber}`}>
          <Select value={values.number_format}
                  onChange={(v) => patchOne("number_format", v)}
                  options={NUMBER_FORMATS}
                  disabled={isLocked(locks, "number_format")} />
          {renderLock(locks.number_format, "number_format", C)}
        </Row>
        <Row label="First day of week">
          <Select value={values.first_day_of_week}
                  onChange={(v) => patchOne("first_day_of_week", v)}
                  options={FIRST_DAYS}
                  disabled={isLocked(locks, "first_day_of_week")} />
          {renderLock(locks.first_day_of_week, "first_day_of_week", C)}
        </Row>
      </Card>

      {/* ── Section 3 — Workday Preferences ──────────────────────────── */}
      <Card title="Workday Preferences" icon="briefcase"
            subtitle="These preferences change how Temveris appears for you. They don't change your schedule, breaks, or workspace rules.">
        <Row label="Default landing page"
             helper="Applied at next sign-in or new session.">
          <Select value={values.default_landing_page}
                  onChange={(v) => patchOne("default_landing_page", v)}
                  options={LANDING_PAGES} />
        </Row>
        <Row label="Show next shift on Home"
             helper="Pulls from My Schedule and respects schedule visibility policy.">
          <Toggle on={values.show_next_shift}
                  onChange={(v) => patchOne("show_next_shift", v)}
                  ariaLabel="Show next shift" />
        </Row>
        <Row label="Show weekly hours summary"
             helper="Visibility preference only — your recorded hours don't change.">
          <Toggle on={values.show_weekly_hours}
                  onChange={(v) => patchOne("show_weekly_hours", v)}
                  ariaLabel="Show weekly hours" />
        </Row>
        <Row label="Calendar view">
          <Segmented value={values.calendar_view}
                     onChange={(v) => patchOne("calendar_view", v)}
                     options={CALENDAR_VIEWS} />
        </Row>
        <Row label="Show keyboard shortcuts overlay"
             helper="Press ? from any worker page to open the shortcuts overlay.">
          <Toggle on={values.show_keyboard_shortcuts}
                  onChange={(v) => patchOne("show_keyboard_shortcuts", v)}
                  ariaLabel="Keyboard shortcuts overlay" />
        </Row>
      </Card>

      {/* ── Section 4 — App Behavior ─────────────────────────────────── */}
      <Card title="App Behavior" icon="zap">
        <Row label="Hide payroll values by default"
             helper="Shows masked values until clicked in My Pay. Applies across all pay-related surfaces.">
          <Toggle on={values.hide_payroll_values}
                  onChange={(v) => patchOne("hide_payroll_values", v)}
                  ariaLabel="Hide payroll values" />
        </Row>
        <Row label="Presentation mode"
             helper="One-click privacy state for screen sharing, demos, or public work. Hides pay, contact details, and activity identifiers.">
          <Toggle on={values.presentation_mode}
                  onChange={(v) => patchOne("presentation_mode", v)}
                  ariaLabel="Presentation mode" />
        </Row>
        <Row label="Confirm before submitting correction requests"
             helper="Prevents accidental correction submissions.">
          <Toggle on={values.correction_confirmation_enabled}
                  onChange={(v) => patchOne("correction_confirmation_enabled", v)}
                  ariaLabel="Confirm correction submissions" />
        </Row>
        <Row label="Product tips"
             helper="Short educational prompts. No productivity coaching.">
          <Toggle on={values.product_tips_enabled}
                  onChange={(v) => patchOne("product_tips_enabled", v)}
                  ariaLabel="Product tips" />
        </Row>
        <Row label="Feature announcements"
             helper="New Temveris updates and changes.">
          <Toggle on={values.feature_announcements_enabled}
                  onChange={(v) => patchOne("feature_announcements_enabled", v)}
                  ariaLabel="Feature announcements" />
        </Row>
        <Row label="Connected calendars"
             helper="Temveris uses your calendar to detect when you're in a meeting, so meeting time isn't recorded as idle. You control which calendars are connected.">
          <Button variant="secondary" icon="calendar" onClick={() => setShowCalendar(true)}>
            Manage ({calendars.length})
          </Button>
        </Row>
      </Card>

      {/* ── Boundary Notice (cross-links) ────────────────────────────── */}
      <Card title="Looking for something else?" icon="info">
        <div style={{ display: "grid", gap: 10,
                       gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
          <CrossLink icon="user"     label="Profile"
            sub="Edit your name, photo, and contact details."
            onClick={() => onJumpToTab?.("profile")} />
          <CrossLink icon="bell"     label="Notifications"
            sub="Choose how Temveris informs you."
            onClick={() => onJumpToTab?.("notifications")} />
          <CrossLink icon="device"   label="Connected Devices"
            sub="Manage your sessions and signed-in devices."
            onClick={() => onJumpToTab?.("profile")} />
          <CrossLink icon="database" label="My Data"
            sub="See what's recorded, request corrections, or export your data."
            onClick={() => onJumpToTab?.("whats-recorded")} />
        </div>
      </Card>

      {/* ── Modals ───────────────────────────────────────────────────── */}
      <Modal open={showReset} onClose={() => setShowReset(false)}
        title="Reset settings to defaults?"
        footer={
          <>
            <Button variant="secondary" onClick={() => setShowReset(false)}>Cancel</Button>
            <Button onClick={doReset}>Reset</Button>
          </>
        }>
        <p style={{ fontSize: 13, color: C.text, margin: 0, lineHeight: 1.6 }}>
          This resets your <strong>display</strong>, <strong>language</strong>, <strong>workday</strong>,
          and <strong>app behavior</strong> preferences. It does not affect:
        </p>
        <ul style={{ fontSize: 13, color: C.textSub, marginTop: 12, paddingLeft: 22, lineHeight: 1.7 }}>
          <li>Your profile, contact details, or password</li>
          <li>Notification preferences</li>
          <li>Connected devices, sessions, or sign-ins</li>
          <li>My Data settings, DSAR, exports, or privacy controls</li>
          <li>Calendar connections</li>
          <li>Workspace policies (theme/timezone locks remain)</li>
        </ul>
        {values.presentation_mode && (
          <p style={{ fontSize: 12, color: C.amber, margin: "12px 0 0", lineHeight: 1.5,
                       padding: "10px 12px", background: C.amberBg, borderRadius: 6 }}>
            <strong>Presentation mode stays on.</strong> Your settings reset, but sensitive details
            remain hidden so nothing leaks during a screen share.
          </p>
        )}
      </Modal>

      <CalendarPanel
        open={showCalendar}
        onClose={() => setShowCalendar(false)}
        calendars={calendars}
        onConnect={connectCalendar}
        onDisconnect={disconnectCalendar}
      />

      <ShortcutsOverlay open={showShortcuts} onClose={() => setShowShortcuts(false)} />
    </div>
    </CCtx.Provider>
  );
}

// ═════════════════════════════════════════════════════════════════════════════
// SUB-COMPONENTS
// ═════════════════════════════════════════════════════════════════════════════
function Row({ label, helper, children }) {
  const C = useC();
  return (
    <div style={{
      display: "flex", alignItems: "flex-start", gap: 16,
      padding: "14px 0", borderBottom: `1px solid ${C.borderLight}`, flexWrap: "wrap",
    }}>
      <div style={{ flex: 1, minWidth: 220 }}>
        <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>{label}</p>
        {helper && <p style={{ fontSize: 11, color: C.textSub, margin: "4px 0 0", lineHeight: 1.5 }}>{helper}</p>}
      </div>
      <div style={{ flexShrink: 0 }}>{children}</div>
    </div>
  );
}

function Select({ value, onChange, options, disabled }) {
  const C = useC();
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange?.(e.target.value)}
      disabled={disabled}
      style={{
        minWidth: 220, padding: "9px 12px", borderRadius: C.rSm,
        border: `1px solid ${C.border}`, background: disabled ? C.borderLight : C.bg,
        fontSize: 13, color: disabled ? C.textSub : C.text, fontFamily: "inherit",
        outline: "none", minHeight: 44,
      }}>
      {options.map(o => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}

function Segmented({ value, onChange, options, disabled }) {
  const C = useC();
  return (
    <div role="radiogroup"
      style={{
        display: "inline-flex", gap: 2, padding: 3,
        background: C.bg, borderRadius: 8,
        border: `1px solid ${C.border}`, opacity: disabled ? 0.5 : 1,
      }}>
      {options.map(o => {
        const active = value === o.value;
        return (
          <button key={o.value} type="button" role="radio" aria-checked={active}
            disabled={disabled}
            onClick={() => onChange?.(o.value)}
            style={{
              padding: "7px 12px", borderRadius: 6, border: "none",
              background: active ? C.brand : "transparent",
              color: active ? "#fff" : C.textSub,
              fontWeight: active ? 700 : 600, fontSize: 12,
              cursor: disabled ? "not-allowed" : "pointer",
              fontFamily: "inherit", minHeight: 36,
              display: "inline-flex", alignItems: "center", gap: 6,
            }}>
            {o.icon && <Icon n={o.icon} s={12} c={active ? "#fff" : C.textSub} />}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

function CrossLink({ icon, label, sub, onClick }) {
  const C = useC();
  return (
    <button type="button" onClick={onClick}
      style={{
        textAlign: "left", display: "flex", alignItems: "center", gap: 12,
        padding: 14, borderRadius: 10,
        border: `1px solid ${C.border}`, background: C.surface,
        cursor: "pointer", fontFamily: "inherit", minHeight: 56,
      }}>
      <span style={{
        width: 36, height: 36, borderRadius: 8,
        background: C.brandBg,
        display: "flex", alignItems: "center", justifyContent: "center",
      }}>
        <Icon n={icon} s={18} c={C.brand} />
      </span>
      <span style={{ flex: 1 }}>
        <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>{label}</p>
        <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0", lineHeight: 1.5 }}>{sub}</p>
      </span>
      <Icon n="chevright" s={14} c={C.textLight} />
    </button>
  );
}

function isLocked(locks, key) {
  return !!locks?.[key];
}

function renderLock(lock, key, C) {
  if (!lock || !C) return null;
  return (
    <div style={{
      marginTop: 8, padding: "8px 10px", borderRadius: 6,
      background: C.amberBg, fontSize: 11, color: C.amber, lineHeight: 1.5,
      maxWidth: 320,
    }} role="note" aria-describedby={`lock-${key}`}>
      <span id={`lock-${key}`}>
        <strong>Managed by workspace.</strong>
        {lock.current_value !== undefined && <> Current: <strong>{String(lock.current_value)}</strong>.</>}
        {lock.policy_name && <> {lock.policy_name}{lock.policy_version && ` v${lock.policy_version}`}.</>}
      </span>
      {lock.policy_url && (
        <a href={lock.policy_url} target="_blank" rel="noopener noreferrer"
          style={{ marginLeft: 6, color: C.amber, fontWeight: 700 }}>
          View policy
        </a>
      )}
    </div>
  );
}

function LockedDisplay({ value, lock, field }) {
  const C = useC();
  return (
    <div>
      <div style={{
        minWidth: 220, padding: "9px 12px", borderRadius: C.rSm,
        border: `1px solid ${C.border}`, background: C.borderLight,
        fontSize: 13, color: C.textSub, fontFamily: "inherit",
        display: "flex", alignItems: "center", gap: 8, minHeight: 44,
      }}>
        <Icon n="lock" s={14} c={C.amber} />
        <span style={{ flex: 1 }}>{value}</span>
        <Pill tone="locked">Locked</Pill>
      </div>
      {renderLock(lock, field, C)}
    </div>
  );
}

// ─── Calendar panel (modal) ─────────────────────────────────────────────────
function CalendarPanel({ open, onClose, calendars, onConnect, onDisconnect }) {
  const C = useC();
  return (
    <Modal open={open} onClose={onClose} title="Connected calendars" size="medium"
      footer={<Button variant="secondary" onClick={onClose}>Close</Button>}>
      <p style={{ fontSize: 13, color: C.textSub, margin: "0 0 16px", lineHeight: 1.6 }}>
        Temveris uses your connected calendar only to detect when you're in a meeting, so
        meeting time isn't recorded as idle or unexplained. You can disconnect any calendar
        at any time.
      </p>

      {calendars.length > 0 && (
        <ul style={{ listStyle: "none", padding: 0, margin: "0 0 16px" }}>
          {calendars.map(c => (
            <li key={c.id} style={{
              padding: 14, borderRadius: 8,
              border: `1px solid ${C.border}`, background: C.surface, marginBottom: 8,
              display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
            }}>
              <div style={{
                width: 36, height: 36, borderRadius: 8,
                background: C.brandBg,
                display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
              }}>
                <Icon n="calendar" s={18} c={C.brand} />
              </div>
              <div style={{ flex: 1, minWidth: 200 }}>
                <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>
                  {c.provider === "google" ? "Google Calendar" : c.provider === "outlook" ? "Outlook Calendar" : c.provider}
                </p>
                <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                  {c.account_email || "—"} ·{" "}
                  <Pill tone={c.connection_status === "connected" ? "on" : c.connection_status === "attention" ? "locked" : "off"}>
                    {c.connection_status || "connected"}
                  </Pill>
                  {c.last_synced_at && <> · last sync {new Date(c.last_synced_at).toLocaleString()}</>}
                </p>
                {c.scopes_granted?.length > 0 && (
                  <p style={{ fontSize: 11, color: C.textLight, margin: "4px 0 0" }}>
                    Read-only access to free/busy and meeting times.
                  </p>
                )}
              </div>
              <Button variant="ghost" icon="trash" onClick={() => onDisconnect(c.id)}>
                Disconnect
              </Button>
            </li>
          ))}
        </ul>
      )}

      <p style={{ fontSize: 12, color: C.textLight, fontWeight: 700,
                  textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 8px" }}>
        Add a calendar
      </p>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <Button variant="secondary" icon="plus" onClick={() => onConnect("google")}>
          Connect Google Calendar
        </Button>
        <Button variant="secondary" icon="plus" onClick={() => onConnect("outlook")}>
          Connect Outlook Calendar
        </Button>
      </div>
      <p style={{ fontSize: 11, color: C.textLight, margin: "12px 0 0", lineHeight: 1.5,
                   padding: "8px 10px", borderRadius: 6, background: C.bg }}>
        OAuth flows are part of the integrations rollout. Connecting now records your intent;
        full sync activates once the OAuth handshake is enabled for your workspace.
      </p>
    </Modal>
  );
}

// ─── Shortcuts overlay (only when toggle is on; opened by "?") ──────────────
function ShortcutsOverlay({ open, onClose }) {
  const C = useC();
  return (
    <Modal open={open} onClose={onClose} title="Keyboard shortcuts" size="medium"
      footer={<Button variant="secondary" onClick={onClose}>Close</Button>}>
      <p style={{ fontSize: 12, color: C.textSub, margin: "0 0 14px", lineHeight: 1.5 }}>
        Press these from any worker page. The <kbd style={kbd(C)}>?</kbd> overlay must be
        enabled in Settings → Workday Preferences.
      </p>
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {[
          { keys: ["?"],          label: "Open this shortcuts overlay" },
          { keys: ["g", "h"],     label: "Go to Home" },
          { keys: ["g", "s"],     label: "Go to My Schedule" },
          { keys: ["g", "a"],     label: "Go to My Activity" },
          { keys: ["g", "p"],     label: "Go to My Pay" },
          { keys: ["g", "d"],     label: "Go to My Data" },
          { keys: ["c"],          label: "Clock in / clock out" },
          { keys: ["b"],          label: "Start / end break" },
          { keys: ["/"],          label: "Focus search (where available)" },
          { keys: ["esc"],        label: "Close current dialog or panel" },
        ].map((s, i) => (
          <li key={i} style={{
            display: "flex", alignItems: "center", gap: 12,
            padding: "10px 0", borderBottom: `1px solid ${C.borderLight}`,
          }}>
            <span style={{ display: "flex", gap: 4 }}>
              {s.keys.map((k, j) => (
                <kbd key={j} style={kbd(C)}>{k}</kbd>
              ))}
            </span>
            <span style={{ fontSize: 13, color: C.text }}>{s.label}</span>
          </li>
        ))}
      </ul>
      <p style={{ fontSize: 11, color: C.textLight, margin: "12px 0 0" }}>
        Note: shortcut targets that aren't yet wired into your build will show "not available"
        — they'll activate as those pages are released.
      </p>
    </Modal>
  );
}

function kbd(C) {
  return {
    display: "inline-flex", alignItems: "center", justifyContent: "center",
    minWidth: 26, height: 24, padding: "0 6px",
    borderRadius: 5, border: `1px solid ${C?.border ?? "#E4E7EC"}`,
    background: C?.surface ?? "#fff", fontSize: 12, fontWeight: 700, color: C?.text ?? "#101828",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  };
}

// ═════════════════════════════════════════════════════════════════════════════
// PREVIEW HELPERS
// ═════════════════════════════════════════════════════════════════════════════
function formatDatePreview(fmt) {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm   = String(d.getMonth() + 1).padStart(2, "0");
  const dd   = String(d.getDate()).padStart(2, "0");
  const mmm  = d.toLocaleDateString("en-US", { month: "short" });
  switch (fmt) {
    case "DD/MM/YYYY": return `${dd}/${mm}/${yyyy}`;
    case "YYYY-MM-DD": return `${yyyy}-${mm}-${dd}`;
    case "D MMM YYYY": return `${parseInt(dd, 10)} ${mmm} ${yyyy}`;
    case "MM/DD/YYYY":
    default:           return `${mm}/${dd}/${yyyy}`;
  }
}

function formatNumberPreview(fmt) {
  const n = 1234567.89;
  switch (fmt) {
    case "1.234,56":
      return n.toLocaleString("de-DE", { minimumFractionDigits: 2 });
    case "1 234,56":
      return n.toLocaleString("fr-FR", { minimumFractionDigits: 2 });
    case "1’234.56":
      return n.toLocaleString("de-CH", { minimumFractionDigits: 2 });
    case "1,234.56":
    default:
      return n.toLocaleString("en-US", { minimumFractionDigits: 2 });
  }
}
