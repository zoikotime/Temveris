/**
 * ActivityLog.jsx
 *
 * Unified per-user event timeline that surfaces every record the platform
 * captures about a user's day:
 *   • screenshot / app activity rows
 *   • browser tab visits
 *   • clock-in / clock-out events
 *   • break start / end events
 *
 * Backend: GET /api/activity-log/timeline   (cursor-paginated, newest-first)
 *          GET /api/activity-log/summary    (KPIs + hourly buckets + top lists)
 *
 * Role gating is enforced server-side: employees only ever see their own
 * data. Admins / HR can pass user_id to scope to a specific user, or omit
 * it to see the whole org.
 *
 * "Real-time" is implemented as low-frequency polling of the first page +
 * the summary every POLL_MS. WebSocket hookup can be layered on later by
 * subscribing to "new_screenshot" events and prepending them to the list.
 */

import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { API_BASE } from "../config";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, PieChart, Pie, Cell,
  AreaChart, Area,
} from "recharts";

// ─── Vibrant chart palette ────────────────────────────────────────────────
// Used by the new gradients + accent strokes. Kept here (not inline in JSX)
// so the same color list can drive both the SVG <defs> ids and the chart
// references — change a colour in one place and it propagates everywhere.
const CHART_GRADIENTS = {
  productive:  { from: "#22D3A6", to: "#0F9D6E" },   // emerald
  neutral:     { from: "#A5B4FC", to: "#6366F1" },   // indigo (was washed-out grey)
  distracting: { from: "#FB7185", to: "#E11D48" },   // rose
  idle:        { from: "#CBD5E1", to: "#94A3B8" },   // slate
  brand:       { from: "#38BDF8", to: "#0284C7" },   // sky
  amber:       { from: "#FCD34D", to: "#D97706" },   // amber (clicks)
  violet:      { from: "#C4B5FD", to: "#7C3AED" },   // violet (keystrokes)
  pink:        { from: "#F472B6", to: "#DB2777" },   // pink  (browser)
};

// Top-apps / top-domains rotating palette — gives each bar its own colour
// instead of grouping everything by productivity bucket. Keeps the chart
// visually lively even when all top entries are "productive".
const RAINBOW_BARS = [
  "#0EA5E9", "#22D3A6", "#A78BFA", "#F472B6",
  "#F59E0B", "#34D399", "#6366F1", "#EF4444",
  "#14B8A6", "#FB923C",
];

// Shared <defs> block — recharts requires gradients to be declared inside
// the chart's <svg>, so each chart re-emits this. Extracted to a component
// for DRY-ness and to keep gradient IDs centralised.
function ChartGradients() {
  return (
    <defs>
      {Object.entries(CHART_GRADIENTS).map(([key, { from, to }]) => (
        <linearGradient key={key} id={`grad-${key}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"   stopColor={from} stopOpacity={0.9} />
          <stop offset="100%" stopColor={to}   stopOpacity={0.6} />
        </linearGradient>
      ))}
      {/* Horizontal variant for vertical bar charts */}
      {Object.entries(CHART_GRADIENTS).map(([key, { from, to }]) => (
        <linearGradient key={`h-${key}`} id={`gradH-${key}`} x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%"   stopColor={from} stopOpacity={0.7} />
          <stop offset="100%" stopColor={to}   stopOpacity={1} />
        </linearGradient>
      ))}
    </defs>
  );
}

const POLL_MS = 20000; // 20s polling for first-page + summary refresh

// ─── Productivity vocabulary (mirrors backend) ─────────────────────────────
const P_PRODUCTIVE  = "productive";
const P_NEUTRAL     = "neutral";
const P_DISTRACTING = "distracting";

const PROD_LABEL = {
  [P_PRODUCTIVE]:  "Productive",
  [P_NEUTRAL]:     "Neutral",
  [P_DISTRACTING]: "Distracting",
};

const PROD_COLOR = {
  [P_PRODUCTIVE]:  "#10B981",
  [P_NEUTRAL]:     "#6366F1",
  [P_DISTRACTING]: "#EF4444",
};


// ─── Event-type metadata (icon + display name) ─────────────────────────────
const TYPE_META = {
  screenshot:    { label: "Screenshot",     icon: "camera",   color: "#0EA5E9" },
  app:           { label: "App Activity",   icon: "monitor",  color: "#0EA5E9" },
  browser:       { label: "Website Visit",  icon: "globe",    color: "#8B5CF6" },
  session_start: { label: "Clock In",       icon: "play",     color: "#16A34A" },
  session_end:   { label: "Clock Out",      icon: "stop",     color: "#64748B" },
  break_start:   { label: "Break Started",  icon: "pause",    color: "#F59E0B" },
  break_end:     { label: "Break Ended",    icon: "resume",   color: "#F59E0B" },
};

const ALL_TYPES = Object.keys(TYPE_META);

// ─── Range chips ───────────────────────────────────────────────────────────
function rangeFromChip(chip) {
  const end = new Date();
  const start = new Date();
  if (chip === "today") {
    start.setHours(0, 0, 0, 0);
  } else if (chip === "yesterday") {
    start.setDate(start.getDate() - 1);
    start.setHours(0, 0, 0, 0);
    end.setDate(end.getDate() - 1);
    end.setHours(23, 59, 59, 999);
  } else if (chip === "7d") {
    start.setDate(start.getDate() - 6);
    start.setHours(0, 0, 0, 0);
  } else if (chip === "30d") {
    start.setDate(start.getDate() - 29);
    start.setHours(0, 0, 0, 0);
  }
  return { start: start.toISOString(), end: end.toISOString() };
}

// ─── Tiny icon library (inline SVG, no external dep) ───────────────────────
function Icon({ name, size = 16, color = "currentColor" }) {
  const s = size;
  const stroke = { stroke: color, strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round", fill: "none" };
  switch (name) {
    case "camera":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><rect x="2.5" y="6.5" width="19" height="13" rx="2"/><circle cx="12" cy="13" r="3.5"/><path d="M8 6.5L9.5 4h5L16 6.5"/></svg>);
    case "monitor":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><rect x="2.5" y="3.5" width="19" height="13" rx="2"/><path d="M8 21h8M12 16.5V21"/></svg>);
    case "globe":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"/></svg>);
    case "play":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><path d="M7 5l12 7-12 7z" fill={color}/></svg>);
    case "stop":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><rect x="6" y="6" width="12" height="12" rx="1" fill={color}/></svg>);
    case "pause":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><rect x="7" y="5" width="3.5" height="14" rx="1" fill={color}/><rect x="13.5" y="5" width="3.5" height="14" rx="1" fill={color}/></svg>);
    case "resume":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><path d="M7 5l12 7-12 7z" fill={color}/></svg>);
    case "search":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>);
    case "filter":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><path d="M3 5h18l-7 9v6l-4-2v-4z"/></svg>);
    case "clock":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>);
    case "mouse":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><rect x="6.5" y="2.5" width="11" height="19" rx="5.5"/><path d="M12 6v5"/></svg>);
    case "key":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><circle cx="8" cy="13" r="4"/><path d="M12 13h9M17 13v3"/></svg>);
    case "user":
      return (<svg width={s} height={s} viewBox="0 0 24 24" {...stroke}><circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4.5-6 8-6s7 2 8 6"/></svg>);
    default:
      return null;
  }
}

// ─── Format helpers ────────────────────────────────────────────────────────
function fmtSecs(sec) {
  if (!sec || sec <= 0) return "0s";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch { return iso; }
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const today = new Date();
    const isToday = d.toDateString() === today.toDateString();
    if (isToday) return fmtTime(iso);
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch { return iso; }
}

function fmtDate(d) {
  return new Date(d).toISOString().slice(0, 10);
}

// Backend aggregates `hourly` into 24 UTC buckets. Most users sit in non-UTC
// timezones, so a chart labelled "0..23 UTC" lands their workday at strange
// hours. Rotate the array client-side so position `i` represents local hour
// `i`. Half-hour offsets (IST etc.) are rounded to the nearest whole hour —
// acceptable distortion for an at-a-glance chart.
function localizeHourly(hourly) {
  if (!Array.isArray(hourly) || hourly.length !== 24) return hourly || [];
  const shift = Math.round(new Date().getTimezoneOffset() / -60); // signed local-from-UTC hours
  return Array.from({ length: 24 }, (_, localHr) => {
    const utcHr = ((localHr - shift) % 24 + 24) % 24;
    return { ...hourly[utcHr], hour: localHr };
  });
}

// ─── Animated count-up ────────────────────────────────────────────────────
// Smoothly tweens the displayed number from its prior value to the new value
// whenever `target` changes. Keeps the dashboard feeling "live" when the
// 20-second poll lands new data — a static number that just snaps from 0 to
// 4,242 reads as a page refresh, but a 600ms tween reads as a count-up.
function useCountUp(target, durationMs = 600) {
  const [display, setDisplay] = useState(target || 0);
  const fromRef = useRef(target || 0);
  const startedRef = useRef(0);
  const rafRef = useRef(null);

  useEffect(() => {
    cancelAnimationFrame(rafRef.current);
    const from = fromRef.current;
    const to = Number(target) || 0;
    if (from === to) {
      setDisplay(to);
      return;
    }
    startedRef.current = performance.now();
    const tick = (now) => {
      const t = Math.min(1, (now - startedRef.current) / durationMs);
      // ease-out cubic — feels snappier than linear
      const eased = 1 - Math.pow(1 - t, 3);
      const val = from + (to - from) * eased;
      setDisplay(val);
      if (t < 1) {
        rafRef.current = requestAnimationFrame(tick);
      } else {
        fromRef.current = to;
      }
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [target, durationMs]);

  return display;
}

// Tile icons for the KPI row — small monoline glyphs that match Icon().
function KpiGlyph({ name, color }) {
  const stroke = { stroke: color, strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round", fill: "none" };
  switch (name) {
    case "active":
      return (<svg width="18" height="18" viewBox="0 0 24 24" {...stroke}><path d="M13 3L4 14h7l-1 7 9-11h-7z" fill={color} fillOpacity="0.25"/></svg>);
    case "idle":
      return (<svg width="18" height="18" viewBox="0 0 24 24" {...stroke}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>);
    case "productive":
      return (<svg width="18" height="18" viewBox="0 0 24 24" {...stroke}><path d="M3 17l5-5 4 4 7-9" /><path d="M14 7h5v5" /></svg>);
    case "shots":
      return (<svg width="18" height="18" viewBox="0 0 24 24" {...stroke}><rect x="2.5" y="6.5" width="19" height="13" rx="2"/><circle cx="12" cy="13" r="3.5"/><path d="M8 6.5L9.5 4h5L16 6.5"/></svg>);
    case "click":
      return (<svg width="18" height="18" viewBox="0 0 24 24" {...stroke}><path d="M8 3v6M3 8h6"/><path d="M13 13l8 3-3 1-1 3z" fill={color} fillOpacity="0.25"/></svg>);
    case "key":
      return (<svg width="18" height="18" viewBox="0 0 24 24" {...stroke}><rect x="3" y="7" width="18" height="11" rx="2"/><path d="M7 11h.01M11 11h.01M15 11h.01M7 14.5h10"/></svg>);
    case "browser":
      return (<svg width="18" height="18" viewBox="0 0 24 24" {...stroke}><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"/></svg>);
    default:
      return null;
  }
}

// Interactive KPI tile — gradient halo, animated count, hover lift.
function KpiTile({ label, value, sub, format = (v) => v, glyph, gradient, accent, onClick, active }) {
  const animated = useCountUp(typeof value === "number" ? value : 0);
  const displayValue = typeof value === "number" ? format(animated) : value;
  const grad = CHART_GRADIENTS[gradient] || CHART_GRADIENTS.brand;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!onClick}
      style={{
        flex: "1 1 0", minWidth: 130,
        background: `linear-gradient(135deg, ${grad.from}10 0%, ${grad.to}06 100%), var(--kpi-surface, #FFFFFF)`,
        border: `1px solid ${active ? grad.to : "var(--kpi-border, #E2E8F0)"}`,
        borderRadius: 14, padding: "14px 14px 12px",
        display: "flex", flexDirection: "column", gap: 6,
        cursor: onClick ? "pointer" : "default", textAlign: "left",
        position: "relative", overflow: "hidden",
        boxShadow: active
          ? `0 0 0 3px ${grad.to}22, 0 8px 22px -10px ${grad.to}55`
          : "0 1px 2px rgba(15,23,42,0.04)",
        transition: "transform .15s ease, box-shadow .2s ease, border-color .2s ease",
        fontFamily: "inherit",
      }}
      onMouseEnter={(e) => { e.currentTarget.style.transform = "translateY(-2px)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.transform = "translateY(0)"; }}
    >
      {/* Accent stripe */}
      <span style={{
        position: "absolute", top: 0, left: 0, right: 0, height: 3,
        background: `linear-gradient(90deg, ${grad.from}, ${grad.to})`,
      }} />
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span style={{
          fontSize: 10, fontWeight: 700, color: accent || grad.to,
          letterSpacing: 0.6, textTransform: "uppercase",
        }}>{label}</span>
        <span style={{
          width: 28, height: 28, borderRadius: 8,
          background: `${grad.from}22`,
          display: "inline-flex", alignItems: "center", justifyContent: "center",
        }}>
          <KpiGlyph name={glyph} color={grad.to} />
        </span>
      </div>
      <div style={{
        fontSize: 22, fontWeight: 800, fontVariantNumeric: "tabular-nums",
        background: `linear-gradient(135deg, ${grad.from}, ${grad.to})`,
        WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
        backgroundClip: "text", color: grad.to,
        lineHeight: 1.1,
      }}>
        {displayValue}
      </div>
      {sub && (
        <span style={{ fontSize: 11, color: "var(--kpi-sub, #94A3B8)", fontWeight: 500 }}>{sub}</span>
      )}
    </button>
  );
}

// Format an integer count with thousands separators (kept locale-free so
// dev/prod render identically regardless of browser language settings).
function fmtCount(n) {
  const v = Math.round(Number(n) || 0);
  return v.toLocaleString("en-US");
}

// ─── Theme defaults (overridable via `colors` prop) ────────────────────────
const DEFAULT_C = {
  bg: "#F8FAFC", surface: "#FFFFFF", surface2: "#F1F5F9",
  border: "#E2E8F0", borderLight: "#F1F5F9",
  text: "#0F172A", textSub: "#475569", textLight: "#94A3B8",
  brand: "#0EA5E9", brandBg: "#E0F2FE", brandBdr: "#7DD3FC",
};

// ────────────────────────────────────────────────────────────────────────────
// Component
// ────────────────────────────────────────────────────────────────────────────

/**
 * Props:
 *   token       - JWT (required)
 *   isAdmin     - bool: caller is admin/HR (controls user-picker visibility)
 *   employees   - [{ id, name }] (optional; admin user picker)
 *   colors      - theme palette (optional; falls back to DEFAULT_C)
 *   compact     - render in a tighter layout (used inside the employee dashboard)
 *   onThumbnailClick - (url) => void; opens the image lightbox if provided
 */
export default function ActivityLog({
  token,
  isAdmin = false,
  employees = [],
  colors,
  compact = false,
  onThumbnailClick,
}) {
  const C = colors || DEFAULT_C;

  // ── Filters ────────────────────────────────────────────────────────────
  const [rangeChip, setRangeChip] = useState("today");
  const [{ start, end }, setRange] = useState(() => rangeFromChip("today"));
  const [userFilter, setUserFilter] = useState("");           // "" = all (admin only)
  const [typeFilter, setTypeFilter] = useState(new Set());    // empty = all
  const [prodFilter, setProdFilter] = useState(new Set());    // empty = all
  const [search, setSearch] = useState("");
  const [searchDebounced, setSearchDebounced] = useState("");

  // Debounce free-text search so we don't refetch on every keystroke.
  useEffect(() => {
    const t = setTimeout(() => setSearchDebounced(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  // ── Data ───────────────────────────────────────────────────────────────
  const [events, setEvents] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(null);

  const [summary, setSummary] = useState(null);

  // Track the latest filter signature so a stale poll doesn't clobber a
  // newer manual query.
  const reqRef = useRef(0);

  const filtersKey = useMemo(
    () => JSON.stringify({
      start, end, userFilter,
      types: Array.from(typeFilter).sort(),
      prods: Array.from(prodFilter).sort(),
      search: searchDebounced,
    }),
    [start, end, userFilter, typeFilter, prodFilter, searchDebounced]
  );

  const buildQuery = useCallback((cursorVal) => {
    const p = new URLSearchParams();
    if (start) p.append("start", start);
    if (end)   p.append("end", end);
    if (userFilter) p.append("user_id", userFilter);
    if (typeFilter.size > 0) p.append("types", Array.from(typeFilter).join(","));
    if (prodFilter.size > 0) p.append("productivity", Array.from(prodFilter).join(","));
    if (searchDebounced)     p.append("search", searchDebounced);
    if (cursorVal)           p.append("cursor", cursorVal);
    // Employee dashboard view forces self-scoping so an admin browsing
    // their personal Activity Log doesn't accidentally see org-wide totals.
    if (!isAdmin)            p.append("scope", "self");
    p.append("limit", "50");
    return p.toString();
  }, [start, end, userFilter, typeFilter, prodFilter, searchDebounced, isAdmin]);

  const fetchPage = useCallback(async (cursorVal, opts = {}) => {
    const reqId = ++reqRef.current;
    if (cursorVal) setLoadingMore(true); else setLoading(true);
    setError(null);
    try {
      const q = buildQuery(cursorVal);
      const res = await fetch(`${API_BASE}/api/activity-log/timeline?${q}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        // 503 from the DB layer means a source query hit the
        // statement-timeout — usually a wide range with no user_id and
        // App Activity selected. Show an actionable hint instead of the
        // raw "HTTP 503" string the user reported.
        if (res.status === 503) {
          throw new Error(
            "The activity log query timed out. Try a shorter date range or pick a specific employee."
          );
        }
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      // Drop the response if a newer request superseded it.
      if (reqId !== reqRef.current && !opts.allowStale) return;

      if (cursorVal) {
        setEvents(prev => {
          // De-dupe in case the cursor boundary repeats (rare with the
          // strict-less-than predicate, but cheap to guard against).
          const seen = new Set(prev.map(e => e.id));
          const fresh = data.events.filter(e => !seen.has(e.id));
          return [...prev, ...fresh];
        });
      } else {
        setEvents(data.events || []);
      }
      setCursor(data.next_cursor || null);
      setHasMore(!!data.has_more);
      // When the backend served a partial result (one source hit the
      // per-request statement timeout) surface a non-blocking hint so the
      // user understands the timeline isn't necessarily empty — they just
      // need to narrow the filter.
      if (data.partial) {
        setError(
          "Some events couldn't be loaded for this range. Narrow the date range or pick a specific employee for the full timeline."
        );
      }
    } catch (e) {
      setError(e.message || "Failed to load activity");
    } finally {
      if (cursorVal) setLoadingMore(false); else setLoading(false);
    }
  }, [buildQuery, token]);

  const fetchSummary = useCallback(async () => {
    try {
      const p = new URLSearchParams();
      if (start) p.append("start", start);
      if (end)   p.append("end", end);
      if (userFilter) p.append("user_id", userFilter);
      if (!isAdmin)   p.append("scope", "self");
      const res = await fetch(`${API_BASE}/api/activity-log/summary?${p.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return;
      const data = await res.json();
      setSummary(data);
    } catch { /* summary is best-effort — don't surface errors */ }
  }, [start, end, userFilter, token, isAdmin]);

  // Initial + filter-change fetch
  useEffect(() => {
    fetchPage(null);
    fetchSummary();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey]);

  // Polling — refetch first page + summary every POLL_MS so newly-uploaded
  // screenshots/heartbeats appear without a manual reload. Pauses while the
  // tab is hidden (battery + backend load), and does an immediate catch-up
  // refresh on visibility regain so users see fresh data the moment they
  // come back instead of waiting up to a full poll interval.
  useEffect(() => {
    let id = null;
    const tick = () => {
      fetchPage(null, { allowStale: false });
      fetchSummary();
    };
    const start = () => {
      if (id == null) id = setInterval(tick, POLL_MS);
    };
    const stop = () => {
      if (id != null) { clearInterval(id); id = null; }
    };
    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        tick();
        start();
      }
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey]);

  // ── Filter helpers ─────────────────────────────────────────────────────
  const applyChip = (chip) => {
    setRangeChip(chip);
    setRange(rangeFromChip(chip));
  };

  const toggleSet = (set, key, setter) => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key); else next.add(key);
    setter(next);
  };

  // ── Derived metrics for KPI tiles ──────────────────────────────────────
  const activeS = summary?.totals?.active_seconds || 0;
  const idleS   = summary?.totals?.idle_seconds   || 0;
  const totalProd = summary
    ? (summary.productivity.productive + summary.productivity.neutral + summary.productivity.distracting)
    : 0;
  const productivePct = totalProd > 0
    ? Math.round((summary.productivity.productive / totalProd) * 100)
    : 0;
  const screenshotsCount = summary?.totals?.screenshots || 0;
  const sessionsCount    = summary?.totals?.sessions    || 0;
  const breaksCount      = summary?.totals?.breaks      || 0;
  const clicksCount      = summary?.totals?.mouse_clicks   || 0;
  const keysCount        = summary?.totals?.key_presses    || 0;
  const browserVisits    = summary?.totals?.browser_visits || 0;

  // ── Memoized chart data transforms ─────────────────────────────────────
  // These run on every render today (including each 20s poll, even when the
  // underlying summary is unchanged), handing recharts a brand-new `data`
  // array reference each time and forcing it to re-diff/re-animate. Memoizing
  // on `[summary]` stabilizes the array identity so charts only update when
  // the summary payload actually changes.
  //
  // `hourlyData` additionally depends on the device timezone offset (read via
  // `new Date().getTimezoneOffset()` inside localizeHourly). That offset is
  // effectively constant for the lifetime of the mount, so `[summary]` is a
  // conservative-but-correct key.
  const hourlyData = useMemo(
    () => localizeHourly(summary?.hourly),
    [summary]
  );

  const topAppsData = useMemo(
    () => (summary?.top_apps || []).slice(0, 6).map((a, i) => ({
      name: a.app_name, seconds: a.seconds, color: RAINBOW_BARS[i % RAINBOW_BARS.length],
    })),
    [summary]
  );

  const topDomainsData = useMemo(
    () => (summary?.top_domains || []).slice(0, 6).map((d, i) => ({
      name: d.domain, seconds: d.active_seconds, visits: d.visits,
      color: RAINBOW_BARS[(i + 3) % RAINBOW_BARS.length],
    })),
    [summary]
  );

  // ── Render styles (kept inline so we don't rely on global CSS) ─────────
  const styles = {
    page: {
      display: "flex", flexDirection: "column", gap: 14,
      fontFamily: "'DM Sans','Inter','Segoe UI',sans-serif",
      color: C.text, padding: compact ? 0 : 6,
    },
    card: {
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 12, padding: 14,
    },
    chip: (active) => ({
      padding: "5px 12px", borderRadius: 999,
      background: active ? C.brandBg : "transparent",
      color: active ? C.brand : C.textSub,
      border: `1px solid ${active ? C.brandBdr : C.border}`,
      fontSize: 12, fontWeight: 600, cursor: "pointer",
      fontFamily: "inherit", transition: "all .12s",
    }),
    input: {
      padding: "7px 10px", borderRadius: 8,
      border: `1px solid ${C.border}`, background: C.surface,
      color: C.text, fontSize: 13, fontFamily: "inherit",
      outline: "none", minWidth: 0,
    },
  };

  return (
    <div style={styles.page}>

      {/* ─── Filters ──────────────────────────────────────────────────── */}
      <div style={styles.card}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          {/* Date range chips */}
          {[
            ["today", "Today"], ["yesterday", "Yesterday"],
            ["7d", "Last 7 days"], ["30d", "Last 30 days"],
          ].map(([k, label]) => (
            <button key={k} onClick={() => applyChip(k)} style={styles.chip(rangeChip === k)}>
              {label}
            </button>
          ))}

          {/* Custom date inputs */}
          <span style={{ marginLeft: 6, color: C.textLight, fontSize: 12 }}>or</span>
          <input
            type="date"
            value={fmtDate(start)}
            onChange={(e) => {
              setRangeChip("custom");
              const d = new Date(e.target.value);
              d.setHours(0, 0, 0, 0);
              setRange((r) => ({ ...r, start: d.toISOString() }));
            }}
            style={styles.input}
          />
          <span style={{ color: C.textLight, fontSize: 12 }}>→</span>
          <input
            type="date"
            value={fmtDate(end)}
            onChange={(e) => {
              setRangeChip("custom");
              const d = new Date(e.target.value);
              d.setHours(23, 59, 59, 999);
              setRange((r) => ({ ...r, end: d.toISOString() }));
            }}
            style={styles.input}
          />

          {/* User picker (admin only) */}
          {isAdmin && (
            <select
              value={userFilter}
              onChange={(e) => setUserFilter(e.target.value)}
              style={{ ...styles.input, minWidth: 160 }}
            >
              <option value="">All employees</option>
              {employees.map(emp => (
                <option key={emp.id} value={emp.id}>{emp.name}</option>
              ))}
            </select>
          )}

          {/* Search */}
          <div style={{ flex: 1, minWidth: 180, display: "flex", alignItems: "center", gap: 6,
                        padding: "6px 10px", borderRadius: 8, border: `1px solid ${C.border}`,
                        background: C.surface }}>
            <Icon name="search" size={14} color={C.textLight} />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search apps, websites, window titles…"
              style={{ flex: 1, border: "none", outline: "none", background: "transparent",
                       color: C.text, fontSize: 13, fontFamily: "inherit", minWidth: 0 }}
            />
          </div>
        </div>

        {/* Type chips */}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 10, alignItems: "center" }}>
          <span style={{ fontSize: 11, fontWeight: 700, color: C.textLight, marginRight: 4 }}>TYPE</span>
          {ALL_TYPES.map(t => {
            const meta = TYPE_META[t];
            const active = typeFilter.has(t);
            return (
              <button key={t} onClick={() => toggleSet(typeFilter, t, setTypeFilter)} style={styles.chip(active)}>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                  <Icon name={meta.icon} size={12} color={active ? C.brand : meta.color} />
                  {meta.label}
                </span>
              </button>
            );
          })}
          {typeFilter.size > 0 && (
            <button onClick={() => setTypeFilter(new Set())}
                    style={{ ...styles.chip(false), color: C.textLight, borderStyle: "dashed" }}>
              Clear
            </button>
          )}
        </div>

        {/* Productivity chips */}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8, alignItems: "center" }}>
          <span style={{ fontSize: 11, fontWeight: 700, color: C.textLight, marginRight: 4 }}>PRODUCTIVITY</span>
          {[P_PRODUCTIVE, P_NEUTRAL, P_DISTRACTING].map(p => {
            const active = prodFilter.has(p);
            return (
              <button key={p} onClick={() => toggleSet(prodFilter, p, setProdFilter)} style={styles.chip(active)}>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <span style={{ width: 8, height: 8, borderRadius: "50%", background: PROD_COLOR[p] }}/>
                  {PROD_LABEL[p]}
                </span>
              </button>
            );
          })}
          {prodFilter.size > 0 && (
            <button onClick={() => setProdFilter(new Set())}
                    style={{ ...styles.chip(false), color: C.textLight, borderStyle: "dashed" }}>
              Clear
            </button>
          )}
        </div>
      </div>

      {/* ─── KPI tiles (interactive, gradient, animated) ─────────────── */}
      {/* Per request: the KPI summary tiles are hidden in the employee
          dashboard view (isAdmin === false). Admins still see the full row. */}
      {isAdmin && (
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
          gap: 10,
          // CSS variables let the gradient tiles inherit the current theme's
          // surface/border/sub-text colors without us having to pass `C`
          // through every nested style object.
          "--kpi-surface": C.surface,
          "--kpi-border": C.border,
          "--kpi-sub": C.textLight,
        }}
      >
        <KpiTile
          label="Active Time"
          value={activeS}
          format={fmtSecs}
          sub="tracker ticks marked active"
          glyph="active"
          gradient="productive"
        />
        <KpiTile
          label="Idle Time"
          value={idleS}
          format={fmtSecs}
          sub="tracker ticks marked idle"
          glyph="idle"
          gradient="idle"
        />
        <KpiTile
          label="Productive %"
          value={productivePct}
          format={(v) => `${Math.round(v)}%`}
          sub="of categorised time"
          glyph="productive"
          gradient="brand"
          active={prodFilter.has(P_PRODUCTIVE)}
          onClick={() => toggleSet(prodFilter, P_PRODUCTIVE, setProdFilter)}
        />
        <KpiTile
          label="Screenshots"
          value={screenshotsCount}
          format={fmtCount}
          sub={`${sessionsCount} sessions · ${breaksCount} breaks`}
          glyph="shots"
          gradient="violet"
          active={typeFilter.has("screenshot")}
          onClick={() => toggleSet(typeFilter, "screenshot", setTypeFilter)}
        />
        <KpiTile
          label="Mouse Clicks"
          value={clicksCount}
          format={fmtCount}
          sub="total clicks in window"
          glyph="click"
          gradient="amber"
        />
        <KpiTile
          label="Keystrokes"
          value={keysCount}
          format={fmtCount}
          sub="total keys in window"
          glyph="key"
          gradient="distracting"
        />
        <KpiTile
          label="Browser Visits"
          value={browserVisits}
          format={fmtCount}
          sub="tab activations tracked"
          glyph="browser"
          gradient="pink"
          active={typeFilter.has("browser")}
          onClick={() => toggleSet(typeFilter, "browser", setTypeFilter)}
        />
      </div>
      )}

      {/* ─── Charts row: hourly + productivity gauge ─────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: compact ? "1fr" : "2fr 1fr", gap: 10 }}>
        {/* Hourly stacked area — gradient fills + smoothing */}
        <div style={{ ...styles.card, minHeight: 240 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
            <h4 style={{ margin: 0, fontSize: 12, fontWeight: 700, color: C.textSub, letterSpacing: 0.4, textTransform: "uppercase" }}>
              Hourly activity
            </h4>
            <div style={{ display: "inline-flex", alignItems: "center", gap: 10, fontSize: 10, color: C.textLight, fontWeight: 600 }}>
              {[
                ["Productive",  CHART_GRADIENTS.productive.to],
                ["Neutral",     CHART_GRADIENTS.neutral.to],
                ["Distracting", CHART_GRADIENTS.distracting.to],
                ["Idle",        CHART_GRADIENTS.idle.to],
              ].map(([label, color]) => (
                <span key={label} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                  <span style={{ width: 8, height: 8, borderRadius: 2, background: color }} />
                  {label}
                </span>
              ))}
            </div>
          </div>
          <div style={{ width: "100%", height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={hourlyData} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
                <ChartGradients />
                <CartesianGrid strokeDasharray="3 3" stroke={C.borderLight} vertical={false} />
                <XAxis dataKey="hour" tick={{ fontSize: 10, fill: C.textLight }}
                       tickFormatter={(h) => String(h).padStart(2, "0")} />
                <YAxis tick={{ fontSize: 10, fill: C.textLight }}
                       tickFormatter={(v) => v >= 3600 ? Math.round(v/3600) + "h" : Math.round(v / 60) + "m"} />
                <Tooltip
                  formatter={(v) => fmtSecs(Number(v))}
                  contentStyle={{ fontSize: 11, fontFamily: "inherit", borderRadius: 10, background: C.surface, border: `1px solid ${C.border}`, color: C.text, boxShadow: "0 6px 24px -8px rgba(15,23,42,0.25)" }}
                  itemStyle={{ color: C.text }}
                  labelStyle={{ color: C.textSub, fontWeight: 700 }}
                  labelFormatter={(h) => `${String(h).padStart(2, "0")}:00 — ${String(h).padStart(2, "0")}:59 (local)`}
                />
                <Area type="monotone" dataKey="productive"  stackId="1" stroke={CHART_GRADIENTS.productive.to}  fill="url(#grad-productive)"  strokeWidth={1.5} name="Productive"
                      activeDot={{ r: 4, fill: CHART_GRADIENTS.productive.to, stroke: "#fff", strokeWidth: 2 }}/>
                <Area type="monotone" dataKey="neutral"     stackId="1" stroke={CHART_GRADIENTS.neutral.to}     fill="url(#grad-neutral)"     strokeWidth={1.5} name="Neutral"
                      activeDot={{ r: 4, fill: CHART_GRADIENTS.neutral.to, stroke: "#fff", strokeWidth: 2 }}/>
                <Area type="monotone" dataKey="distracting" stackId="1" stroke={CHART_GRADIENTS.distracting.to} fill="url(#grad-distracting)" strokeWidth={1.5} name="Distracting"
                      activeDot={{ r: 4, fill: CHART_GRADIENTS.distracting.to, stroke: "#fff", strokeWidth: 2 }}/>
                <Area type="monotone" dataKey="idle"        stackId="1" stroke={CHART_GRADIENTS.idle.to}        fill="url(#grad-idle)"        strokeWidth={1.5} name="Idle"
                      activeDot={{ r: 4, fill: CHART_GRADIENTS.idle.to, stroke: "#fff", strokeWidth: 2 }}/>
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Productivity radial gauge — center number = productive % */}
        <div style={{ ...styles.card, minHeight: 240, position: "relative" }}>
          <h4 style={{ margin: "0 0 8px", fontSize: 12, fontWeight: 700, color: C.textSub, letterSpacing: 0.4, textTransform: "uppercase" }}>
            Productivity mix
          </h4>
          <div style={{ position: "relative", width: "100%", height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <ChartGradients />
                <Pie
                  data={[
                    { name: "Productive",  value: summary?.productivity?.productive  || 0, fill: "url(#grad-productive)"  },
                    { name: "Neutral",     value: summary?.productivity?.neutral     || 0, fill: "url(#grad-neutral)"     },
                    { name: "Distracting", value: summary?.productivity?.distracting || 0, fill: "url(#grad-distracting)" },
                  ]}
                  dataKey="value" nameKey="name"
                  innerRadius={56} outerRadius={84}
                  paddingAngle={3}
                  cornerRadius={6}
                  startAngle={90} endAngle={-270}
                  stroke="none"
                  isAnimationActive
                />
                <Tooltip
                  formatter={(v, n) => [fmtSecs(Number(v)), n]}
                  contentStyle={{ fontSize: 11, fontFamily: "inherit", borderRadius: 10, background: C.surface, border: `1px solid ${C.border}`, color: C.text, boxShadow: "0 6px 24px -8px rgba(15,23,42,0.25)" }}
                  itemStyle={{ color: C.text }}
                  labelStyle={{ color: C.textSub, fontWeight: 700 }}
                />
              </PieChart>
            </ResponsiveContainer>
            {/* Center label — absolutely positioned over the donut */}
            <div style={{
              position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
              display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
              pointerEvents: "none",
            }}>
              <span style={{
                fontSize: 28, fontWeight: 800, fontVariantNumeric: "tabular-nums",
                background: `linear-gradient(135deg, ${CHART_GRADIENTS.productive.from}, ${CHART_GRADIENTS.productive.to})`,
                WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
                backgroundClip: "text", lineHeight: 1,
              }}>{productivePct}%</span>
              <span style={{ fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: 0.5, textTransform: "uppercase", marginTop: 2 }}>
                Productive
              </span>
            </div>
          </div>
          {/* Inline legend with second-totals */}
          <div style={{ display: "flex", justifyContent: "center", gap: 12, marginTop: 6, flexWrap: "wrap" }}>
            {[
              ["Productive",  summary?.productivity?.productive  || 0, CHART_GRADIENTS.productive.to],
              ["Neutral",     summary?.productivity?.neutral     || 0, CHART_GRADIENTS.neutral.to],
              ["Distracting", summary?.productivity?.distracting || 0, CHART_GRADIENTS.distracting.to],
            ].map(([label, v, color]) => (
              <span key={label} style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11, color: C.textSub, fontWeight: 600 }}>
                <span style={{ width: 10, height: 10, borderRadius: 3, background: color }} />
                {label} <span style={{ color: C.textLight, fontWeight: 500 }}>· {fmtSecs(v)}</span>
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* ─── Charts row: top apps + top websites ─────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: compact ? "1fr" : "1fr 1fr", gap: 10 }}>
        {/* Top apps — rainbow gradient bars */}
        <div style={{ ...styles.card, minHeight: 240 }}>
          <h4 style={{ margin: "0 0 8px", fontSize: 12, fontWeight: 700, color: C.textSub, letterSpacing: 0.4, textTransform: "uppercase" }}>
            Top apps
          </h4>
          {(summary?.top_apps || []).length === 0 ? (
            <div style={{ padding: 24, textAlign: "center", color: C.textLight, fontSize: 12 }}>
              No app activity in this window.
            </div>
          ) : (
            <div style={{ width: "100%", height: 200 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  layout="vertical"
                  data={topAppsData}
                  margin={{ top: 4, right: 16, left: 0, bottom: 0 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke={C.borderLight} horizontal={false} />
                  <XAxis type="number" hide tickFormatter={(v) => fmtSecs(v)} />
                  <YAxis type="category" dataKey="name" width={100}
                         tick={{ fontSize: 11, fill: C.textSub, fontWeight: 600 }} />
                  <Tooltip formatter={(v) => fmtSecs(Number(v))}
                           contentStyle={{ fontSize: 11, fontFamily: "inherit", borderRadius: 10, background: C.surface, border: `1px solid ${C.border}`, color: C.text, boxShadow: "0 6px 24px -8px rgba(15,23,42,0.25)" }}
                           itemStyle={{ color: C.text }}
                           labelStyle={{ color: C.textSub, fontWeight: 700 }}
                           cursor={{ fill: C.surface2 || "rgba(99,102,241,0.06)" }}/>
                  <Bar dataKey="seconds" radius={[6, 6, 6, 6]} barSize={16} isAnimationActive>
                    {(summary?.top_apps || []).slice(0, 6).map((a, i) => (
                      <Cell key={i} fill={RAINBOW_BARS[i % RAINBOW_BARS.length]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>

        {/* Top websites — wired to summary.top_domains (was unused) */}
        <div style={{ ...styles.card, minHeight: 240 }}>
          <h4 style={{ margin: "0 0 8px", fontSize: 12, fontWeight: 700, color: C.textSub, letterSpacing: 0.4, textTransform: "uppercase" }}>
            Top websites
          </h4>
          {(summary?.top_domains || []).length === 0 ? (
            <div style={{ padding: 24, textAlign: "center", color: C.textLight, fontSize: 12 }}>
              No browser activity captured for this window.
            </div>
          ) : (
            <div style={{ width: "100%", height: 200 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  layout="vertical"
                  data={topDomainsData}
                  margin={{ top: 4, right: 16, left: 0, bottom: 0 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke={C.borderLight} horizontal={false} />
                  <XAxis type="number" hide tickFormatter={(v) => fmtSecs(v)} />
                  <YAxis type="category" dataKey="name" width={120}
                         tick={{ fontSize: 11, fill: C.textSub, fontWeight: 600 }} />
                  <Tooltip
                    contentStyle={{ fontSize: 11, fontFamily: "inherit", borderRadius: 10, background: C.surface, border: `1px solid ${C.border}`, color: C.text, boxShadow: "0 6px 24px -8px rgba(15,23,42,0.25)" }}
                    itemStyle={{ color: C.text }}
                    labelStyle={{ color: C.textSub, fontWeight: 700 }}
                    cursor={{ fill: C.surface2 || "rgba(244,114,182,0.06)" }}
                    formatter={(value, _name, props) => [
                      `${fmtSecs(Number(value))} · ${props.payload.visits} visits`,
                      "Active",
                    ]}
                  />
                  <Bar dataKey="seconds" radius={[6, 6, 6, 6]} barSize={16} isAnimationActive>
                    {(summary?.top_domains || []).slice(0, 6).map((_d, i) => (
                      <Cell key={i} fill={RAINBOW_BARS[(i + 3) % RAINBOW_BARS.length]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>
      </div>

      {/* ─── Timeline list ───────────────────────────────────────────── */}
      <div style={styles.card}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <h3 style={{ margin: 0, fontSize: 14, fontWeight: 800, color: C.text }}>Activity Timeline</h3>
          <span style={{ fontSize: 11, color: C.textLight }}>
            {events.length} events {hasMore ? "(more available)" : ""} · auto-refresh every {Math.round(POLL_MS / 1000)}s
          </span>
        </div>

        {error && (
          <div style={{ padding: 10, background: C.redBg || "#FEE2E2", color: C.redText || "#991B1B",
                        border: `1px solid ${C.redBdr || "#FCA5A5"}`,
                        borderRadius: 8, fontSize: 12, fontWeight: 600, marginBottom: 8 }}>
            {error}
          </div>
        )}

        {loading && events.length === 0 ? (
          <div style={{ padding: 24, textAlign: "center", color: C.textLight, fontSize: 13 }}>
            Loading activity…
          </div>
        ) : events.length === 0 ? (
          <div style={{ padding: 24, textAlign: "center", color: C.textLight, fontSize: 13 }}>
            No activity in this window.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {events.map((ev) => (
              <ActivityRow
                key={ev.id} event={ev} colors={C}
                isAdmin={isAdmin}
                onThumbnailClick={onThumbnailClick}
              />
            ))}
          </div>
        )}

        {/* Load more */}
        {hasMore && (
          <div style={{ display: "flex", justifyContent: "center", marginTop: 12 }}>
            <button
              onClick={() => fetchPage(cursor)}
              disabled={loadingMore}
              style={{
                padding: "8px 18px", borderRadius: 8, border: `1px solid ${C.brandBdr}`,
                background: loadingMore ? C.borderLight : C.brandBg,
                color: C.brand, fontSize: 13, fontWeight: 700,
                cursor: loadingMore ? "wait" : "pointer", fontFamily: "inherit",
              }}
            >
              {loadingMore ? "Loading…" : "Load more"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Single timeline row
// ────────────────────────────────────────────────────────────────────────────
const ActivityRow = React.memo(function ActivityRow({ event, colors, isAdmin, onThumbnailClick }) {
  const C = colors || DEFAULT_C;
  const meta = TYPE_META[event.type] || TYPE_META.app;
  const prodColor = event.productivity ? PROD_COLOR[event.productivity] : null;
  // Accent stripe colour follows productivity when classified, otherwise
  // falls back to the event-type colour (sessions/breaks aren't bucketed).
  const accentColor = prodColor || meta.color;
  const [hover, setHover] = useState(false);

  const onThumb = () => {
    if (event.thumbnail_url && onThumbnailClick) onThumbnailClick(event.thumbnail_url);
  };

  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "flex", alignItems: "center", gap: 12,
        padding: "10px 12px 10px 14px", borderRadius: 10,
        position: "relative", overflow: "hidden",
        // C.surface2 was undefined in the global palette, so every row
        // fell through to the hard-coded #F8FAFC light grey. In dark mode
        // that washed every event out into an unreadable beige stripe.
        // surfaceAlt is the theme-aware "alt background" that already
        // exists on both light and dark palettes.
        background: hover
          ? `linear-gradient(90deg, ${accentColor}10 0%, ${C.surfaceAlt || C.surface} 30%)`
          : (C.surfaceAlt || C.surface),
        border: `1px solid ${hover ? accentColor + "55" : C.borderLight}`,
        boxShadow: hover ? `0 4px 14px -8px ${accentColor}66` : "none",
        transform: hover ? "translateX(2px)" : "translateX(0)",
        transition: "transform .15s ease, box-shadow .2s ease, border-color .2s ease, background .2s ease",
      }}
    >
      {/* Category-colored accent stripe on the left edge */}
      <span style={{
        position: "absolute", top: 0, bottom: 0, left: 0, width: 3,
        background: `linear-gradient(180deg, ${accentColor}, ${accentColor}88)`,
      }} />
      {/* Type icon */}
      <div style={{
        width: 32, height: 32, borderRadius: 8, flexShrink: 0,
        background: `linear-gradient(135deg, ${meta.color}33, ${meta.color}11)`,
        border: `1px solid ${meta.color}33`,
        display: "flex", alignItems: "center", justifyContent: "center",
      }}>
        <Icon name={meta.icon} size={16} color={meta.color} />
      </div>

      {/* Main content */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
            {meta.label}
          </span>
          {event.productivity && (
            <span style={{
              fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 999,
              background: prodColor + "22", color: prodColor,
              textTransform: "uppercase", letterSpacing: 0.4,
            }}>
              {PROD_LABEL[event.productivity]}
            </span>
          )}
          {isAdmin && event.user_name && (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4,
                           fontSize: 11, color: C.textSub, fontWeight: 600 }}>
              <Icon name="user" size={10} color={C.textLight} />
              {event.user_name}
            </span>
          )}
        </div>
        <div style={{ fontSize: 12, color: C.textSub, marginTop: 2,
                       overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
             title={event.label || ""}
        >
          {event.label}
          {event.url && (
            <span style={{ color: C.textLight }}> · {event.domain}</span>
          )}
        </div>
        {/* Stats line */}
        {(event.mouse_clicks != null || event.key_presses != null || event.duration_seconds != null) && (
          <div style={{ display: "flex", gap: 12, marginTop: 4, fontSize: 11, color: C.textLight }}>
            {event.duration_seconds != null && event.duration_seconds > 0 && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                <Icon name="clock" size={11} color={C.textLight} /> {fmtSecs(event.duration_seconds)}
              </span>
            )}
            {event.mouse_clicks != null && event.mouse_clicks > 0 && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                <Icon name="mouse" size={11} color={C.textLight} /> {event.mouse_clicks}
              </span>
            )}
            {event.key_presses != null && event.key_presses > 0 && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                <Icon name="key" size={11} color={C.textLight} /> {event.key_presses}
              </span>
            )}
            {event.idle_seconds != null && event.idle_seconds > 0 && (
              <span style={{ color: "#F59E0B" }}>idle {fmtSecs(event.idle_seconds)}</span>
            )}
          </div>
        )}
      </div>

      {/* Thumbnail (screenshot only) */}
      {event.thumbnail_url && (
        <button
          onClick={onThumb}
          style={{
            width: 64, height: 40, borderRadius: 6, flexShrink: 0,
            border: `1px solid ${C.border}`, padding: 0,
            background: `url(${API_BASE}${event.thumbnail_url}) center/cover no-repeat, ${C.borderLight}`,
            cursor: onThumbnailClick ? "pointer" : "default",
          }}
          title="Open screenshot"
          aria-label="Open screenshot"
        />
      )}

      {/* Timestamp */}
      <div style={{ flexShrink: 0, fontSize: 11, color: C.textLight, fontVariantNumeric: "tabular-nums",
                    textAlign: "right", minWidth: 70 }}>
        {fmtDateTime(event.timestamp)}
      </div>
    </div>
  );
});
