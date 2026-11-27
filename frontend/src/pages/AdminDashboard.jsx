import React, { useState, useEffect, useCallback, useMemo, useRef, lazy, Suspense } from "react";
import { getOrgTimeZone, orgDayBoundsUTC, orgHourOf } from "../lib/orgTime";
import { useNavigate, useLocation } from "react-router-dom";
import AdminSidebar from "../components/AdminSidebar";
import TemverisLogo from "../assets/Temveris_Logo_PNG.png";
import TemverisIcon from "../assets/Temveris_Icon_PNG.png";
import { API_BASE, WS_URL, fetchWithRetry } from "../config";
import * as holidayApi from "../api/holidays";
import {
  mergeHolidayRows, upcomingHolidays, isRetryableHolidayError,
  adoptionCounts, adoptRowState, ADOPT_STATUS,
} from "../lib/holidayView";
import LangSwitcher from "../components/LangSwitcher";
import FloatingAssistantBot from "../components/FloatingAssistantBot";
import DownloadAppButton from "../components/DownloadAppButton";
import { Search as SearchIcon, UserPlus, CalendarDays, ChevronDown } from "lucide-react";
import {
  OverviewTitle, OverviewStatCards, OverviewFilterBar,
  OverviewWorkerTable, OverviewSidePanel,
} from "../components/admin/OverviewLive";
import { OVERVIEW_CSS, navyFor } from "../components/admin/overviewModel";

// Tab modules are gated by `tab === "..."` further down — keep them out of the
// AdminDashboard route chunk (which was 552 KB) so the initial Overview tab
// paints faster. Each module loads only when the operator clicks its tab.
const IntelligencePanel        = lazy(() => import("../components/intelligence/IntelligencePanel"));
const ReportsSection           = lazy(() => import("../components/ReportsSection"));
const ActivityLog              = lazy(() => import("../components/ActivityLog"));
const BillingModule            = lazy(() => import("../components/BillingModule"));
const PayrollCalculatorModule  = lazy(() => import("../components/PayrollCalculatorModule"));
const LeaveSummaryModule       = lazy(() => import("../components/LeaveSummaryModule"));
const BrowserCategoriesTab     = lazy(() => import("../components/admin/BrowserCategoriesTab"));
const TimelapseTab             = lazy(() => import("../components/admin/TimelapseTab"));
const AppsHistoryTab           = lazy(() => import("../components/admin/AppsHistoryTab"));
const TimesheetsTab            = lazy(() => import("../components/admin/TimesheetsTab"));
const ScreenshotsTab           = lazy(() => import("../components/admin/ScreenshotsTab"));
const MyTeamTab               = lazy(() => import("../components/admin/MyTeamTab"));
const DailyHoursChart   = lazy(() => import("../components/admin/charts/OverviewCharts").then(m => ({ default: m.DailyHoursChart })));
const DeptDonut         = lazy(() => import("../components/admin/charts/OverviewCharts").then(m => ({ default: m.DeptDonut })));
const TopEmployeesChart = lazy(() => import("../components/admin/charts/OverviewCharts").then(m => ({ default: m.TopEmployeesChart })));
const AttendanceDonut   = lazy(() => import("../components/admin/charts/OverviewCharts").then(m => ({ default: m.AttendanceDonut })));
const TopAppsDonut      = lazy(() => import("../components/admin/charts/OverviewCharts").then(m => ({ default: m.TopAppsDonut })));

// ── Idle chunk preloader ─────────────────────────────────────────────────────
// Each tab above is its own lazy chunk, so the first switch to a tab downloads
// its JS while <TabFallback/> shows — the brief flash users see. We warm every
// tab chunk during browser idle AFTER the dashboard paints: identical module
// specifiers to the lazy() imports → same chunk → primed cache, so later
// switches resolve with no network round-trip and no fallback. Idle scheduling
// (one chunk per slice) keeps this off the initial-paint critical path, which
// is the whole reason these are code-split in the first place.
const TAB_CHUNK_LOADERS = [
  () => import("../components/admin/MyTeamTab"),
  () => import("../components/admin/ScreenshotsTab"),
  () => import("../components/admin/TimesheetsTab"),
  () => import("../components/admin/TimelapseTab"),
  () => import("../components/admin/AppsHistoryTab"),
  () => import("../components/admin/BrowserCategoriesTab"),
  () => import("../components/admin/charts/OverviewCharts"),
  () => import("../components/ReportsSection"),
  () => import("../components/LeaveSummaryModule"),
  () => import("../components/PayrollCalculatorModule"),
  () => import("../components/BillingModule"),
  () => import("../components/ActivityLog"),
  () => import("../components/intelligence/IntelligencePanel"),
];

let _adminChunksPreloaded = false;
const preloadAdminTabChunks = () => {
  if (_adminChunksPreloaded) return;
  _adminChunksPreloaded = true;
  const ric = (typeof window !== "undefined" && window.requestIdleCallback)
    || ((cb) => setTimeout(() => cb({ timeRemaining: () => 0 }), 200));
  const queue = [...TAB_CHUNK_LOADERS];
  const pump = () => {
    const load = queue.shift();
    if (!load) return;
    load().catch(() => {});   // non-fatal: the tab's own Suspense will retry on click
    if (queue.length) ric(pump);
  };
  ric(pump);
};

// Theme-aware Suspense fallback. Reserves height and paints the themed
// background so a lazy chunk loading mid-switch shows a quiet spinner instead
// of a white flash against the dark dashboard. `C` is reassigned at the top of
// AdminDashboard's render, so it's current by the time this paints.
const TabFallback = () => (
  <div style={{
    minHeight: 360, background: C.bg,
    display: "flex", alignItems: "center", justifyContent: "center",
  }}>
    <span style={{
      width: 22, height: 22, borderRadius: "50%",
      border: `2.5px solid ${C.borderLight}`, borderTopColor: C.brand,
      display: "inline-block", animation: "zt-tab-spin 0.7s linear infinite",
    }} />
    <style>{`@keyframes zt-tab-spin{to{transform:rotate(360deg)}}`}</style>
  </div>
);
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import { useAuth } from "../context/AuthContext";
import { useWorkspace } from "../context/WorkspaceContext";
// Phase 4a: C-free presentational/utility layer relocated to a shared module.
// (Icon / EmpCard / TimelineView stay here — they use the module-level theme C.)
import { parseUTC, fmtTimeIST, istDateKey, effectiveStatus, computeShiftInfo, initials, spawnRipple, getAppMeta, getTabFavicon, getStatusCfg, normEmpType, AppIcon, STATUS_CONFIG, KpiSparkline, Badge, ZT_ACCENT, ProductivityRing, PulseBar, APP_CATEGORY } from "../components/admin/adminShared";
import AuthImage from "../components/admin/AuthImage";
import Gate, { useCapability } from "../entitlements/Gate";
import { useEntitlements } from "../context/EntitlementsContext";
import { shouldRenderNav } from "../entitlements/registry";
import API from "../api/axios";
import { readToken } from "../api/authStorage";
import { refreshAccessToken } from "../api/refresh";
import { playbackLink, readPlaybackDeepLink } from "../components/leave/WorkedDateChips";

// H1: delegate to the shared axios instance so every dashboard GET gets
// in-flight deduplication + centralized 401 silent-refresh (and the
// Electron-safe logout path). Preserves the original contract exactly: returns
// the parsed body, and on a non-2xx returns the error body instead of throwing
// (callers historically read res.json() regardless of status, never checking
// res.ok). The `token` arg is now ignored — the request interceptor attaches
// the CURRENT token from storage, so a 401-retry replays with the fresh token.
const apiFetch = async (path, _token) => {
  try {
    const { data } = await API.get(path);
    return data;
  } catch (err) {
    if (err?.response?.data !== undefined) return err.response.data;
    throw err;
  }
};

// Several legacy admin paths still use fetch because their callers expect a
// Response object. Keep that contract, but give them the same token refresh
// behavior as the shared axios client.
const authFetch = async (input, init = {}) => {
  const buildInit = (tokenOverride) => {
    const headers = new Headers(init.headers || {});
    const token = tokenOverride || readToken();
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    } else {
      const current = headers.get("Authorization");
      if (!current || current === "Bearer null" || current === "Bearer undefined") {
        headers.delete("Authorization");
      }
    }
    return { ...init, headers, credentials: init.credentials || "include" };
  };

  const first = await fetch(input, buildInit());
  if (first.status !== 401) return first;

  const refreshed = await refreshAccessToken();
  if (typeof refreshed === "string" && refreshed) {
    return fetch(input, buildInit(refreshed));
  }
  return first;
};

// LocalStorage key for the SWR-style cache of /employees/team.
// IMPORTANT: keyed per-user so org A's cached snapshot is never served to
// org B when admins share the same browser (e.g. testing with two accounts).
const _getTeamCacheKey = () => {
  try {
    const token = readToken();
    if (!token) return null;
    const payload = JSON.parse(atob(token.split(".")[1]));
    const uid = payload.user_id || payload.sub || "";
    return uid ? `zt_admin_team_v2_${uid}` : null;
  } catch {
    return null;
  }
};
const readEmployeesCache = () => {
  try {
    const key = _getTeamCacheKey();
    if (!key) return null;
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : null;
  } catch {
    return null;
  }
};



// IST calendar-day key (YYYY-MM-DD) — used to reset per-day presence at
// midnight so yesterday's "Stopped" employees show as "Yet to Start" today.

// Returns the effective status for UI rendering. If the raw status is a
// terminal "stopped/offline" state but the last activity was on an earlier
// IST calendar day, the employee has a fresh day ahead — show "Yet to Start".


// ─── SHIFT-AWARE LATENESS (server-resolved since P9) ────────────────────────
// Drives the Late / Very Late badges + Requires Attention bucket + Absent flip.
// The values come from the backend Schedule Resolver; this file performs NO
// attendance arithmetic. computeShiftInfo in adminShared.jsx is a lookup that
// maps the server's `arrival_status` onto the phase names used below.
//
// Inputs read from the /employees/team payload:
//   arrival_status   — "present" | "late" | "very_late" | "awaiting"
//                      | "late_pending" | "absent"
//                      | "not_applicable"   (resolved server-side)
//   late_minutes     — minutes past the scheduled start, or null
//   shift_start_time — "HH:MM" for display only
//   status           — raw backend PRESENCE status, unchanged and orthogonal
//
// Returns:
//   {
//     hasShift:      bool,
//     shiftStart:    "HH:MM" | null,   // display only
//     graceEnd:      null,             // no longer computed client-side
//     lateMinutes:   number | null,
//     lateLabel:     string | null,    // "Late by 30 min"
//     arrivalStatus: string,           // the raw server value
//     phase:         "before_shift" | "within_grace" | "late_pending" |
//                    "late_present" | "very_late" |
//                    "absent_after_noon" | "no_shift",
//   }



const agoFromUTC = (utcIso) => {
  if (!utcIso) return "";
  const diff = Math.floor((Date.now() - parseUTC(utcIso)) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
};


// uiverse-style click ripple — appends a short-lived span at the cursor.
// Caller's button must be position:relative + overflow:hidden.

// ─── APP LOGO / LABEL NORMALIZATION ──────────────────────────────────────────
// Maps the raw process name reported by the desktop tracker (e.g. "Code.exe",
// "chrome", "msedge") to a human label + Simple Icons slug so each badge can
// render with the real brand logo. Unknown apps fall back to a colored letter.


// Small brand logo used inside pills. Uses the Simple Icons CDN for real
// brand logos; falls back to a colored letter tile if the icon is unknown
// or the network request fails.

// Canonical workforce states — Workforce Status System (Final Label Pack).
// Color palette is dictated by spec; labels MUST NOT be reworded without RFC.
// Legacy backend keys (stopped, offline, break, leave, yet to start, ...)
// are aliased into the canonical configs so older payloads still render.


// Normalize employment_type so legacy rows ("Part Time", "PART-TIME", null, etc.)
// compare consistently against the filter values used in the UI (`full_time`,
// `part_time`, ...). Without this the Part-Time filter could count rows whose
// stored type was "Part Time" but then fail to render them because the grid's
// strict equality check used a different case/separator. Returns "full_time"
// for nullish input so unset rows fall into the default bucket.

const makeC = (dark) => ({
  // Dark mode uses neutral near-black greys (no blue cast) so the admin
  // dashboard reads as black-on-black instead of slate-blue. Light mode
  // values are unchanged.
  bg: dark ? "#0A0A0A" : "#F5F7FA",
  surface: dark ? "#141414" : "#FFFFFF",
  surface2: dark ? "#1F1F1F" : "#F8FAFC",
  surfaceAlt: dark ? "#1F1F1F" : "#F8FAFC",
  border: dark ? "#262626" : "#E2E8F0",
  borderLight: dark ? "#1A1A1A" : "#F1F5F9",
  text: dark ? "#F5F5F5" : "#0F172A",
  textSub: dark ? "#A3A3A3" : "#475569",
  textLight: dark ? "#737373" : "#94A3B8",
  brand: "#11496A",
  brandDark: "#0E3B57",
  brandBg: dark ? "#0E2B3D" : "#E7F0F6",
  brandBdr: dark ? "#1F3D55" : "#A6C7DC",
  // Green accent — same palette as ThemeContext / AdminSidebar so the
  // hover treatments and gradient buttons that already reference these
  // tokens (search for `C.brandAccent`) actually paint instead of
  // evaluating to `undefined`.
  brandAccent:     dark ? "#5FCC93" : "#44B97B",
  brandAccentDark: dark ? "#3FAF77" : "#2F8F5C",
  brandAccentBg:   dark ? "#0D2A1A" : "#E8F7EE",
  green: "#2F8F5C",
  greenBg: dark ? "#0D2A1A" : "#E8F7EE",
  greenBdr: dark ? "#1F5A3D" : "#A8E0BD",
  amber: "#B54708",
  amberBg: dark ? "#3D2A0A" : "#FFFAEB",
  amberBdr: dark ? "#92400E" : "#FEDF89",
  red: "#B42318",
  redText: dark ? "#FCA5A5" : "#991B1B",
  redBg: dark ? "#3D1F1A" : "#FEF3F2",
  redBdr: dark ? "#991B1B" : "#FECDCA",
  r: 14,
  rSm: 8,
  // Elevation system
  shadow: dark ? "0 1px 3px rgba(0,0,0,.4)" : "0 1px 3px rgba(0,0,0,.04), 0 1px 2px rgba(0,0,0,.06)",
  shadowMd: dark ? "0 4px 12px rgba(0,0,0,.5)" : "0 4px 6px -1px rgba(0,0,0,.06), 0 2px 4px -2px rgba(0,0,0,.06)",
  shadowLg: dark ? "0 8px 24px rgba(0,0,0,.6)" : "0 10px 15px -3px rgba(0,0,0,.06), 0 4px 6px -4px rgba(0,0,0,.04)",
});

let C = makeC(false);

const ICON_PATHS = {
  dashboard:     "M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z M9 22V12h6v10",
  users:         "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2 M23 21v-2a4 4 0 00-3-3.87 M16 3.13a4 4 0 010 7.75",
  clock:         "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M12 6v6l4 2",
  camera:        "M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z M12 17a4 4 0 100-8 4 4 0 000 8z",
  bar_chart:     "M18 20V10 M12 20V4 M6 20v-6",
  briefcase:     "M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2",
  settings:      "M12 15a3 3 0 100-6 3 3 0 000 6z M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z",
  log_out:       "M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4 M16 17l5-5-5-5 M21 12H9",
  eye:           "M1 12S5 4 12 4s11 8 11 8-4 8-11 8S1 12 1 12z M12 15a3 3 0 100-6 3 3 0 000 6z",
  calendar:      "M3 4h18v18H3z M16 2v4 M8 2v4 M3 10h18",
  mail:          "M4 4h16a2 2 0 012 2v12a2 2 0 01-2 2H4a2 2 0 01-2-2V6a2 2 0 012-2z M22 6l-10 7L2 6",
  search:        "M11 3a8 8 0 100 16A8 8 0 0011 3z M21 21l-4.35-4.35",
  download:      "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  chevron_down:  "M6 9l6 6 6-6",
  chevron_left:  "M15 18l-6-6 6-6",
  chevron_right: "M9 18l6-6-6-6",
  plus:          "M12 5v14 M5 12h14",
  check:         "M20 6L9 17l-5-5",
  x:             "M18 6L6 18 M6 6l12 12",
  alert:         "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
  info:          "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M12 8h.01 M12 12v4",
  monitor:       "M2 3h20a1 1 0 011 1v13a1 1 0 01-1 1H2a1 1 0 01-1-1V4a1 1 0 011-1z M8 21h8 M12 17v4",
  phone:         "M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07A19.5 19.5 0 013.07 9.81a19.79 19.79 0 01-3.07-8.64A2 2 0 012 .18h3a2 2 0 012 1.72c.127.96.361 1.903.7 2.81a2 2 0 01-.45 2.11L6.09 7.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0122 16.92z",
  tag:           "M20.59 13.41l-7.17 7.17a2 2 0 01-2.83 0L2 12V2h10l8.59 8.59a2 2 0 010 2.82z M7 7h.01",
  activity:      "M22 12h-4l-3 9L9 3l-3 9H2",
  timelapse:     "M12 2a10 10 0 100 20A10 10 0 0012 2z M12 6v6l4 2",
  link:          "M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71 M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71",
  trash:         "M3 6h18 M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6 M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2",
  edit:          "M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7 M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z",
  sun:           "M12 18a6 6 0 100-12 6 6 0 000 12z M12 2v4 M12 18v4 M4.22 4.22l2.83 2.83 M16.95 16.95l2.83 2.83 M2 12h4 M18 12h4 M4.22 19.78l2.83-2.83 M16.95 7.05l2.83-2.83",
  moon:          "M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z",
  arrowdown:     "M7 10l5 5 5-5",
  arrowup:       "M7 14l5-5 5 5",
  trending_up:   "M23 6l-9.5 9.5-5-5L1 18 M17 6h6v6",
  cpu:           "M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18",
  zap:           "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
  layers:        "M12 2L2 7l10 5 10-5-10-5z M2 17l10 5 10-5 M2 12l10 5 10-5",
  bell:          "M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9 M13.73 21a2 2 0 01-3.46 0",
  pause:         "M6 4h4v16H6z M14 4h4v16h-4z",
  play:          "M5 3l14 9-14 9V3z",
  coffee:        "M18 8h1a4 4 0 010 8h-1 M2 8h16v9a4 4 0 01-4 4H6a4 4 0 01-4-4V8z M6 1v3 M10 1v3 M14 1v3",
  dollar:        "M12 1v22 M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6",
  file_text:     "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8",
  shield:        "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  refresh:       "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10 M20.49 15a9 9 0 01-14.85 3.36L1 14",
  zap:           "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
  chevron_right2:"M9 18l6-6-6-6",
  copy:          "M9 9h13a2 2 0 012 2v13a2 2 0 01-2 2H11a2 2 0 01-2-2V11a2 2 0 012-2z M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1",
  user_plus:     "M16 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2 M8.5 7a4 4 0 100 8 4 4 0 000-8z M20 8v6 M23 11h-6",
  send:          "M22 2L11 13 M22 2l-7 20-4-9-9-4 20-7z",
  trending_down: "M23 18l-9.5-9.5-5 5L1 6 M17 18h6v-6",
  receipt:       "M14 2H6a2 2 0 00-2 2v18l3-2 3 2 3-2 3 2V8z M14 2v6h6 M9 12h6 M9 16h6",
};

const Icon = ({ n, s = 16, c = C.textSub }) => {
  return (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none"
      stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ display: "inline-block", flexShrink: 0 }}>
      {(ICON_PATHS[n] || "").split(" M").map((seg, i) => (
        <path key={i} d={i === 0 ? seg : "M" + seg} />
      ))}
    </svg>
  );
};

// Tiny inline trend line used by the Analytics KPI cards. Pure SVG, no deps.
// Renders the last N data points as a smoothed polyline + soft fill underneath.
// Falls back to a flat baseline if values are empty or all-zero, so the card
// never collapses or shows a broken viewBox.


// Temveris accent palette — used for the premium employee card design.
// Pinned hex values (not theme-derived) so the cards keep their signature
// look across light and dark mode. Status colors use soft neon variants
// with low-opacity glows for the depth/glassmorphism aesthetic.
//
// `color` is the bright neon used everywhere in dark mode (avatars, rings,
// glows). `textLight` is a darker, accessible variant used ONLY for text on
// light-mode tinted backgrounds — mint #00E5A8 reads beautifully on dark
// slate but loses contrast on a tint-on-white pill, so we shift to an
// emerald-700-ish hue for the text path only. Backgrounds/borders/glows
// keep `color` in both modes so the brand identity stays consistent.

// Pulse bar — a thin horizontal track whose gradient drifts left→right
// in a continuous loop, giving the card a "live monitoring" feel without
// needing real per-tick activity data. Hidden for non-Working states.
// Theme-aware track so it's visible on both backgrounds.

// React.memo: AdminDashboard's parent state churns (live polling, filters,
// open modals) shouldn't redraw every card in the grid. The four callbacks
// passed in below are all stable references — three are React setState setters
// (stable by React's guarantee) and `onToggleType` is wrapped in useCallback
// on the parent — so memo's default shallow prop compare is sufficient.
const EmpCard = React.memo(({ emp, onView, onEdit, onDelete, onToggleType }) => {
  const { isDark } = useTheme();
  const { T } = useLanguage();
  const { workspaceName } = useWorkspace();
  // Derived status / shift / employment-type fields. These run pure helpers
  // (computeShiftInfo / effectiveStatus / normEmpType) over `emp` and were
  // previously recomputed on every render of every card — the dashboard's
  // live-clock intervals re-render the parent every second, so a 100-card
  // grid recomputed all of this 100× per second. Memoize on `emp` (the only
  // input these reads) so a card only recomputes when its own data changes.
  const { accentKey, acc, appName, isToday, clockInIST, empTypeLabel, workSecs, displayPct, showActive, limitedTracking } = useMemo(() => {
    const shift = computeShiftInfo(emp);
    const rawStatus = effectiveStatus(emp);
    // Approved-leave wins over shift phase: a worker on leave is not Absent,
    // even if the no-clock-in + past-noon rule would otherwise flip them.
    const rawLower = String(rawStatus || "").toLowerCase().replace("_", " ");
    const isOnLeave = rawLower === "on leave" || rawLower === "leave";
    const displayStatus =
      isOnLeave                          ? rawStatus
      : shift.phase === "late_pending"     ? "Absent"
      : shift.phase === "absent_after_noon" ? "Absent"
      : rawStatus;

    // Map the canonical status to the accent palette
    const statusKey = (displayStatus || "").toLowerCase();
    const accentKey =
      statusKey === "working"                                              ? "working"   :
      statusKey === "on break"                                             ? "on_break"  :
      ["stopped", "stopped work", "stop", "offline"].includes(statusKey)   ? "stopped"   :
      ["on leave", "leave"].includes(statusKey)                            ? "on_leave"  :
      statusKey === "absent"                                               ? "absent"    :
                                                                             "yet";
    const acc = ZT_ACCENT[accentKey];

    const appName    = emp.current_app || emp.active_app || "";
    const isToday    = emp.clock_in_time && istDateKey(emp.clock_in_time) === istDateKey(new Date());
    const clockInIST = isToday ? fmtTimeIST(emp.clock_in_time) : "—";

    const empType      = normEmpType(emp.employment_type);
    const empTypeLabel = { full_time: "FULL TIME", part_time: "PART TIME", temporary: "TEMPORARY", casual: "CASUAL", field: "FIELD", factory: "FACTORY" }[empType] || empType.toUpperCase();

    const workSecs   = emp.worked_today_seconds || 0;

    // Active Time % — authoritative server figure (active_pct). Fall back to
    // active/worked for trackers/rows that pre-date the engine. Shown only when
    // the worker is actually at the machine (working / break / stopped).
    const rawPct = emp.active_pct != null
      ? Number(emp.active_pct)
      : (emp.worked_today_seconds > 0
          ? (100 * (emp.active_today_seconds || 0) / emp.worked_today_seconds)
          : 0);
    const activePct = Math.max(0, Math.min(100, Math.round(rawPct)));
    // Active Time band: keyboard+mouse engine score, never below the 65 floor
    // (mirrors backend active_time.display_floor). Safety net against any stale
    // value before the reconcile worker re-scores.
    const displayPct = Math.max(65, activePct);
    const showActive = ["working", "on_break", "stopped"].includes(accentKey);
    const limitedTracking = Array.isArray(emp.activity_flags) && emp.activity_flags.includes("LIMITED_TRACKING");

    return { accentKey, acc, appName, isToday, clockInIST, empTypeLabel, workSecs, displayPct, showActive, limitedTracking };
  }, [emp]);

  const fmtHm = (s) => {
    if (!s || s <= 0) return "0h 0m";
    return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
  };

  // Background gradient: deep slate with a colored wash that hints at the
  // worker's state without being loud. The wash sits at <11% opacity so the
  // card stays predominantly neutral — the status pill carries the signal.
  // Light mode uses a slightly stronger wash to compensate for the lower
  // contrast against a near-white base.
  const cardBg = isDark
    ? `radial-gradient(120% 140% at 0% 0%, rgba(${acc.glow}, 0.07) 0%, transparent 55%), linear-gradient(160deg, #121826 0%, #161E31 100%)`
    : `radial-gradient(120% 140% at 0% 0%, rgba(${acc.glow}, 0.10) 0%, transparent 55%), linear-gradient(160deg, #FFFFFF 0%, #F8FAFC 100%)`;

  const borderCol = isDark ? "rgba(255,255,255,0.06)" : "rgba(17,24,39,0.08)";
  const textHi    = isDark ? "#FFFFFF" : "#0F172A";
  const textMd    = isDark ? "#B6C2D9" : "#475569";
  const textLo    = isDark ? "#7E8AA3" : "#94A3B8";
  const chipBg    = isDark ? "rgba(255,255,255,0.04)" : "rgba(15,23,42,0.04)";
  const chipBdr   = isDark ? "rgba(255,255,255,0.06)" : "rgba(15,23,42,0.06)";

  return (
    <div
      onClick={() => onView && onView(emp)}
      style={{
        position: "relative",
        background: cardBg,
        borderRadius: 20,
        border: `1px solid ${borderCol}`,
        padding: "18px 18px 16px",
        cursor: "pointer",
        transition: "transform .25s cubic-bezier(.34,1.56,.64,1), box-shadow .25s ease, border-color .25s ease",
        overflow: "hidden",
        boxShadow: isDark
          ? "0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 24px rgba(0,0,0,0.35)"
          : "0 1px 0 rgba(255,255,255,1) inset, 0 4px 14px rgba(15,23,42,0.06), 0 12px 28px rgba(15,23,42,0.08)",
        backdropFilter: "blur(20px)",
        WebkitBackdropFilter: "blur(20px)",
        // Flex column + 100% height lets the action buttons sit at the bottom
        // via marginTop:auto, so cards with variable middle content (team /
        // department, active app, late label) stay visually aligned across the
        // grid row.
        display: "flex",
        flexDirection: "column",
        height: "100%",
      }}
      onMouseEnter={e => {
        e.currentTarget.style.transform   = "translateY(-3px)";
        e.currentTarget.style.borderColor = `rgba(${acc.glow}, 0.45)`;
        e.currentTarget.style.boxShadow   = isDark
          ? `0 1px 0 rgba(255,255,255,0.05) inset, 0 14px 40px rgba(0,0,0,0.5), 0 0 0 1px rgba(${acc.glow}, 0.18), 0 0 32px rgba(${acc.glow}, 0.18)`
          : `0 1px 0 rgba(255,255,255,1) inset, 0 18px 40px rgba(15,23,42,0.14), 0 0 0 1px rgba(${acc.glow}, 0.35), 0 0 28px rgba(${acc.glow}, 0.20)`;
      }}
      onMouseLeave={e => {
        e.currentTarget.style.transform   = "none";
        e.currentTarget.style.borderColor = borderCol;
        e.currentTarget.style.boxShadow   = isDark
          ? "0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 24px rgba(0,0,0,0.35)"
          : "0 1px 0 rgba(255,255,255,1) inset, 0 4px 14px rgba(15,23,42,0.06), 0 12px 28px rgba(15,23,42,0.08)";
      }}>

      {/* Soft glow accent bar at top — bleeds the status color into the
          card without dominating it. */}
      <div style={{
        position: "absolute", top: 0, left: 0, right: 0, height: 1,
        background: `linear-gradient(90deg, transparent 0%, rgba(${acc.glow},0.5) 50%, transparent 100%)`,
        pointerEvents: "none",
      }} />

      {/* ── HEADER ── avatar + name/role + productivity ring ── */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14 }}>
        <div style={{
          position: "relative",
          width: 42, height: 42, borderRadius: 14, flexShrink: 0,
          background: `linear-gradient(135deg, ${acc.color}, ${acc.color}99)`,
          display: "flex", alignItems: "center", justifyContent: "center",
          color: "#0B1020", fontWeight: 900, fontSize: 14,
          boxShadow: `0 4px 16px rgba(${acc.glow}, 0.32)`,
          letterSpacing: "-0.3px",
        }}>
          {initials(emp.name)}
          {/* Live presence dot — pulses on Working only */}
          <span style={{
            position: "absolute", bottom: -2, right: -2,
            width: 12, height: 12, borderRadius: "50%",
            background: acc.color,
            border: `2px solid ${isDark ? "#121826" : "#FFFFFF"}`,
            boxShadow: `0 0 0 0 rgba(${acc.glow}, 0.5)`,
            animation: accentKey === "working" ? "zt-status-pulse 2s infinite" : "none",
          }} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{
            fontWeight: 700, fontSize: 14, color: textHi, marginBottom: 2,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            letterSpacing: "-0.2px",
          }} title={emp.name}>{emp.name}</p>
          <p style={{
            fontSize: 11, color: textLo, fontWeight: 500,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }} title={emp.designation || emp.role || (T.adminDashboardTeamMember || "Team Member")}>
            {emp.designation || emp.role || (T.adminDashboardTeamMember || "Team Member")}
          </p>
        </div>
        {/* Active Time % ring — authoritative, server-computed (active_pct). */}
        {showActive && (
          <div style={{ flexShrink: 0, display: "flex", flexDirection: "column", alignItems: "center", gap: 2 }}
               title={limitedTracking
                 ? "Limited tracking — native input hook unavailable on this machine; activity is estimated."
                 : (T.active_time || "Active Time")}>
            <ProductivityRing value={displayPct} color={acc.color} size={46} stroke={4} isDark={isDark} />
            <span style={{ fontSize: 8, fontWeight: 700, color: textLo, letterSpacing: "0.3px", textTransform: "uppercase", whiteSpace: "nowrap" }}>
              {(T.active_time || "Active Time")}{limitedTracking ? " ⚠" : ""}
            </span>
          </div>
        )}
      </div>

      {/* ── STATUS PILL + EMP TYPE ── */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          padding: "4px 10px 4px 8px", borderRadius: 999,
          background: `rgba(${acc.glow}, ${isDark ? 0.12 : 0.14})`,
          border: `1px solid rgba(${acc.glow}, ${isDark ? 0.28 : 0.38})`,
          color: isDark ? acc.color : acc.textLight,
          fontSize: 11, fontWeight: 700, letterSpacing: "0.2px",
        }}>
          <span style={{
            width: 6, height: 6, borderRadius: "50%",
            background: acc.color,
            boxShadow: `0 0 8px rgba(${acc.glow}, 0.8)`,
            animation: accentKey === "working" ? "zt-status-pulse 2s infinite" : "none",
          }} />
          {accentKey === "on_break"
            ? (emp.current_break_type === "lunch" ? (T.adminDashboardLunchBreak || "Lunch Break") : (T.adminDashboardShortBreak || "Short Break"))
            : acc.label}
        </span>
        <span onClick={(e) => { e.stopPropagation(); onToggleType && onToggleType(emp); }}
              title={`Click to toggle (currently ${empTypeLabel})`}
              style={{
                fontSize: 9, fontWeight: 800, padding: "3px 8px", borderRadius: 6,
                cursor: "pointer", letterSpacing: "0.4px", userSelect: "none",
                background: chipBg, color: textMd,
                border: `1px solid ${chipBdr}`,
                whiteSpace: "nowrap",
              }}>
          {empTypeLabel}
        </span>
      </div>

      {/* ── ACTIVE APP + CLOCK-IN + WORKED ── */}
      <div style={{
        display: "grid",
        gridTemplateColumns: appName && isToday ? "1fr auto" : "1fr",
        gap: 10, alignItems: "center", marginBottom: 12,
      }}>
        {appName && isToday ? (
          (() => {
            const appMeta = getAppMeta(appName);
            // Browser tab context comes from the Temveris browser extension
            // via /browser/heartbeat → LiveStatus → /employees/team. When
            // present, render "Chrome / ChatGPT" (or "Chrome / chat.openai.com"
            // if the tab title is empty) so admins see WHICH site the worker
            // is on, not just the browser itself.
            const tab = emp.current_tab || null;
            const rawTitle = (tab?.tab_title || "").trim();
            // Tab titles are often "ChatGPT — gpt-4o" or "Inbox (12) · Gmail".
            // Trim long tail after the first " — " / " · " / " | " so the chip
            // stays compact without losing the brand name.
            const tabLabel = rawTitle
              ? rawTitle.split(/\s[—·|–-]\s/)[0].trim()
              : (tab?.domain || "").replace(/^www\./, "");
            const showTab  = !!tabLabel && tabLabel.toLowerCase() !== appMeta.label.toLowerCase();
            return (
              <div style={{
                display: "inline-flex", alignItems: "center", gap: 8,
                padding: "6px 10px", borderRadius: 10,
                background: chipBg, border: `1px solid ${chipBdr}`,
                fontSize: 11, color: textMd, fontWeight: 600,
                minWidth: 0,
              }}
              title={showTab ? `${appMeta.label} / ${rawTitle || tab.domain}` : appMeta.label}>
                {/* Show the site's own favicon when the worker is in a browser
                    (matching the Site & App Usage tab), and fall back to the
                    browser/app logo when there is no favicon to show. */}
                <AppIcon app={appMeta} size={14} favicon={getTabFavicon(appName, tab)} />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {appMeta.label}{showTab && (
                    <>
                      <span style={{ opacity: 0.5, margin: "0 4px" }}>/</span>
                      <span style={{ color: textHi, fontWeight: 700 }}>{tabLabel}</span>
                    </>
                  )}
                </span>
              </div>
            );
          })()
        ) : (
          <div style={{ fontSize: 11, color: textLo, fontStyle: "italic" }}>
            {accentKey === "yet" ? (T.adminDashboardAwaitingClockInCard || "Awaiting clock-in") :
             accentKey === "absent" ? (T.adminDashboardNotPresentToday || "Not present today") :
             accentKey === "on_leave" ? (T.adminDashboardOnApprovedLeave || "On approved leave") :
             accentKey === "stopped" ? (T.adminDashboardShiftEndedCard || "Shift ended") : (T.adminDashboardNoActiveApp || "No active app")}
          </div>
        )}
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 1 }}>
          <span style={{ fontSize: 11, fontWeight: 700, color: textHi, fontVariantNumeric: "tabular-nums" }}>
            {clockInIST}
          </span>
          <span style={{ fontSize: 9, color: textLo, fontWeight: 600, letterSpacing: "0.3px", textTransform: "uppercase" }}>
            {accentKey === "working" || accentKey === "on_break" ? fmtHm(workSecs) : (T.adminDashboardCheckIn || "Check-in")}
          </span>
        </div>
      </div>

      {/* ── LIVE PULSE BAR ── decorative "real-time monitoring" indicator */}
      <PulseBar accent={acc.color} active={accentKey === "working"} isDark={isDark} />

      {/* ── TEAM / DEPARTMENT ──
          Always rendered (even when both fields are empty) so every card
          reserves the same vertical space here. Single-line / no-wrap so
          long names truncate via ellipsis instead of wrapping to a second
          line and breaking row-height alignment with neighbouring cards. */}
      <div style={{
        display: "flex", gap: 12, fontSize: 10, color: textLo,
        margin: "12px 0 4px", flexWrap: "nowrap", minHeight: 14,
        overflow: "hidden",
      }}>
        {emp.team && (
          <span title={emp.team}
                style={{ display: "flex", alignItems: "center", gap: 4, fontWeight: 600, minWidth: 0, flex: emp.department ? "1 1 0" : "0 1 auto" }}>
            <Icon n="users" s={10} c={textLo} />
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {emp.team}
            </span>
          </span>
        )}
        {emp.department && (
          <span title={emp.department}
                style={{ display: "flex", alignItems: "center", gap: 4, fontWeight: 600, minWidth: 0, flex: emp.team ? "1 1 0" : "0 1 auto" }}>
            <Icon n="briefcase" s={10} c={textLo} />
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {emp.department}
            </span>
          </span>
        )}
      </div>

      {/* ── EARLY CLOCK-OUT EXCEPTION BADGE ──
          Shown only when the worker's LATEST clock-out was early (backend
          correlates against the most recent session). Small orange indicator +
          a one-line reason (truncated ~70 chars); full reason on hover. Renders
          nothing otherwise. Data: GET /employees/early-clock-outs → emp. */}
      {emp.early_clock_out && emp.early_clock_out.reason && (() => {
        const full = String(emp.early_clock_out.reason);
        const short = full.length > 70 ? full.slice(0, 70).trimEnd() + "…" : full;
        return (
          <div style={{ margin: "4px 0 0" }}>
            <span style={{
              display: "inline-flex", alignItems: "center", gap: 5,
              padding: "3px 9px", borderRadius: 20, fontSize: 11, fontWeight: 700,
              background: "#FEE2E2", border: "1px solid #FCA5A5", color: "#B91C1C",
            }}>
              🔴 {T.adminEarlyClockOut || "Early Clock Out"}
            </span>
            <p title={full} style={{
              margin: "5px 0 0", fontSize: 11, color: textMd, lineHeight: 1.4,
              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
            }}>
              <span style={{ fontWeight: 700 }}>{T.reasonLabel || "Reason"}:</span>{" "}
              {short}
            </p>
          </div>
        );
      })()}

      {/* ── ACTION BUTTONS ── */}
      <div style={{
        display: "flex", gap: 6, marginTop: "auto", paddingTop: 12,
        borderTop: `1px solid ${borderCol}`,
      }}>
        {[
          { label: T.adminDashboardEmpCardView || "View",   onClick: onView,   tone: "view"   },
          { label: T.adminDashboardEmpCardEdit || "Edit",   onClick: onEdit,   tone: "edit"   },
          { label: T.adminDashboardEmpCardDelete || "Delete", onClick: onDelete, tone: "danger" },
        ].map(b => {
          // Per-action palette — each button keeps its own identity on hover so
          // View/Edit/Delete stay visually distinct regardless of the card's
          // status accent. Tones are tuned for both light and dark themes.
          const TONE = {
            view:   { glow: "59,130,246",  light: "#3B82F6", dark: "#60A5FA" }, // blue
            edit:   { glow: "249,115,22",  light: "#F97316", dark: "#FBBF24" }, // amber
            danger: { glow: "239,68,68",   light: "#EF4444", dark: "#FF5470" }, // rose
          };
          const t            = TONE[b.tone];
          const isDanger     = b.tone === "danger";
          const baseBg       = "transparent";
          const baseBdr      = isDark ? "rgba(255,255,255,0.08)" : "rgba(15,23,42,0.08)";
          const toneText     = isDark ? t.dark : t.light;
          // Delete keeps its tone color at rest so it reads as destructive even
          // before hover; View/Edit stay neutral until interaction.
          const baseCol      = isDanger ? toneText : textMd;
          const hoverBg      = `rgba(${t.glow}, ${isDark ? 0.14 : 0.18})`;
          const hoverBdr     = `rgba(${t.glow}, ${isDark ? 0.50 : 0.65})`;
          const hoverCol     = toneText;
          return (
            <button key={b.label}
              onClick={(e) => { e.stopPropagation(); b.onClick && b.onClick(emp); }}
              style={{
                flex: 1, padding: "7px 0", borderRadius: 10,
                border: `1px solid ${baseBdr}`,
                background: baseBg, color: baseCol,
                fontWeight: 700, fontSize: 11, cursor: "pointer",
                fontFamily: "inherit", letterSpacing: "0.2px",
                transition: "all .15s ease",
              }}
              onMouseEnter={e => {
                e.currentTarget.style.background   = hoverBg;
                e.currentTarget.style.borderColor  = hoverBdr;
                e.currentTarget.style.color        = hoverCol;
              }}
              onMouseLeave={e => {
                e.currentTarget.style.background   = baseBg;
                e.currentTarget.style.borderColor  = baseBdr;
                e.currentTarget.style.color        = baseCol;
              }}>
              {b.label}
            </button>
          );
        })}
      </div>
    </div>
  );
});
EmpCard.displayName = "EmpCard";

// ─── EMPLOYEE DETAIL PANEL ────────────────────────────────────────────────────

// Maps a browser productivity category (from the backend's
// `/browser/summary` and friends) to a compact badge colour scheme. We keep
// this in a single place so the live tile, detail panel, and history table
// all render identical chips.
const BROWSER_CAT = {
  work:        { label: "Work",        color: "#047857", bg: "#ECFDF5", bdr: "#A7F3D0" },
  neutral:     { label: "Neutral",     color: "#4338CA", bg: "#EEF2FF", bdr: "#C7D2FE" },
  distraction: { label: "Distraction", color: "#B91C1C", bg: "#FEF2F2", bdr: "#FECACA" },
};
const browserCatCfg = (c) => BROWSER_CAT[c] || BROWSER_CAT.neutral;

const fmtSecs = (s) => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${String(h).padStart(2,"0")}h ${String(m).padStart(2,"0")}m ${String(sec).padStart(2,"0")}s`;
};

const DonutChart = React.memo(({ data, size = 160 }) => {
  const { T } = useLanguage();
  const total = data.reduce((s, d) => s + d.value, 0) || 1;
  const r = size / 2 - 14, cx = size / 2, cy = size / 2;
  let cumAngle = -Math.PI / 2;
  const slices = data.map(d => {
    const angle = (d.value / total) * 2 * Math.PI;
    // A single slice covering the whole circle can't be drawn as an arc — the
    // start and end points coincide, so the arc collapses to nothing and the
    // ring disappears, leaving only the center label floating in empty space
    // (the "1 app → huge dead box" bug). Flag it and render a full disc instead.
    const full = angle >= 2 * Math.PI - 1e-6;
    const x1 = cx + r * Math.cos(cumAngle), y1 = cy + r * Math.sin(cumAngle);
    cumAngle += angle;
    const x2 = cx + r * Math.cos(cumAngle), y2 = cy + r * Math.sin(cumAngle);
    const lg = angle > Math.PI ? 1 : 0;
    const path = `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${lg} 1 ${x2} ${y2} Z`;
    return { path, full, color: d.color, label: d.label };
  });
  return (
    <svg width={size} height={size}>
      <circle cx={cx} cy={cy} r={r} fill="transparent" />
      {slices.map((s, i) => s.full
        ? <circle key={i} cx={cx} cy={cy} r={r} fill={s.color} opacity={0.85} />
        : <path key={i} d={s.path} fill={s.color} opacity={0.85} />)}
      <circle cx={cx} cy={cy} r={r * 0.55} fill={C.surface} />
      <text x={cx} y={cy - 4} textAnchor="middle" fontSize="11" fontWeight="700" fill={C.text}>{data.length}</text>
      <text x={cx} y={cy + 12} textAnchor="middle" fontSize="10" fill={C.textSub}>{T.adminDashboardAppsDonutLabel || "Apps"}</text>
    </svg>
  );
});
DonutChart.displayName = "DonutChart";

const EmpDetailPanel = ({ emp, onClose, token }) => {
  // Top-level component — must pull T from the language context, otherwise
  // the T.info / T.appUsage references below throw on render.
  const { T } = useLanguage();
  const { isDark } = useTheme();
  const navigate = useNavigate();
  const cfg = getStatusCfg(emp.status);
  const [allSessions, setAllSessions] = useState([]);  // [{session_id, clock_in_time, clock_out_time, breaks:[]}]
  const [appHistory, setAppHistory] = useState([]);
  const [adjustments, setAdjustments] = useState([]); // admin-credited time adjustments (from TimeAdjustment rows)
  const [panelTab, setPanelTab]   = useState("info");
  // ── Date filter ─────────────────────────────────────────────────────────────
  // YYYY-MM-DD in IST. Applied to Clock Summary, App Usage and Browser tabs
  // so the admin can scrub back through any calendar day. The Worker's
  // Details tab ignores this — it shows the live current-state snapshot.
  const istToday = () => new Date().toLocaleDateString("en-CA", { timeZone: getOrgTimeZone() });
  const [selectedDate, setSelectedDate] = useState(istToday);
  const isToday = selectedDate === istToday();
  // Live clock — ticks every second so ongoing rows update in real time
  const [nowMs, setNowMs] = useState(Date.now());
  useEffect(() => {
    const iv = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(iv);
  }, []);
  // Browser activity state for the "Browser" tab — fetched lazily when the
  // admin opens that tab. Re-runs whenever `selectedDate` changes so the
  // pie / bar / timeline always match the picked day.
  const [browserSummary, setBrowserSummary] = useState(null);
  const [browserHistory, setBrowserHistory] = useState([]);
  const [browserTrend, setBrowserTrend]     = useState(null); // 14-day productivity-score series
  const [browserLoading, setBrowserLoading] = useState(false);

  useEffect(() => {
    const tok = token || readToken();
    if (!tok || !emp.id) return;
    // The three panel-mount fetches are independent, so kick them off together
    // via Promise.all instead of as separate statements — each resolves on its
    // own setState below, identical to before, but the intent (run concurrently,
    // not in sequence) is explicit. URLs / params / headers are unchanged.
    Promise.all([
      // /api/app-analytics is a per-day endpoint — pass the selected calendar
      // day so App Usage matches whatever date the admin is scrubbing to.
      authFetch(`${API_BASE}/api/app-analytics?user_id=${emp.id}&date=${selectedDate}`, { headers: { Authorization: `Bearer ${tok}` } })
        .then(r => r.json()).then(d => {
          const userRow = Array.isArray(d.users) ? (d.users.find(u => u.user_id === emp.id) || d.users[0]) : null;
          if (userRow?.apps) {
            setAppHistory(userRow.apps.map(a => ({
              app_name:       a.app,
              total_seconds:  a.seconds,
              active_seconds: a.active_seconds,
              idle_seconds:   a.idle_seconds,
              category:       a.category,   // productive | neutral | distracting | blocked
            })));
          } else {
            setAppHistory([]);
          }
        }).catch(() => { setAppHistory([]); }),
      // /break/sessions returns the last N days. Fetch a 90-day window so we
      // always have the selected date in the result set; buildSessionRows()
      // filters to the picked day client-side.
      authFetch(`${API_BASE}/break/sessions?user_id=${emp.id}&days=90`, { headers: { Authorization: `Bearer ${tok}` } })
        .then(r => r.json()).then(d => { if (d.sessions) setAllSessions(d.sessions); else setAllSessions([]); }).catch(() => setAllSessions([])),
      // Admin-credited time adjustments — folded into the Clock Summary so the
      // additive time correction is visible alongside the tracker history.
      // Original sessions stay untouched; these appear as their own rows.
      authFetch(`${API_BASE}/employees/${emp.id}/time-adjustments?days=90`, { headers: { Authorization: `Bearer ${tok}` } })
        .then(r => r.ok ? r.json() : { items: [] })
        .then(d => setAdjustments(Array.isArray(d.items) ? d.items : []))
        .catch(() => setAdjustments([])),
    ]);
  }, [emp.id, token, selectedDate]);

  // Live-refresh adjustments when an admin elsewhere creates/reverts one for
  // the currently-open employee. The websocket handler at the top of
  // AdminDashboard dispatches `admin_time_adjustment_updated` for any WS
  // message of type `time_adjustment_updated`; we match on user_id and
  // re-pull so the Clock Summary stays in sync without waiting on a poll.
  useEffect(() => {
    const onAdj = (e) => {
      const d = e?.detail || {};
      if (!emp?.id || String(d.user_id) !== String(emp.id)) return;
      const tok = token || readToken();
      if (!tok) return;
      authFetch(`${API_BASE}/employees/${emp.id}/time-adjustments?days=90`, { headers: { Authorization: `Bearer ${tok}` } })
        .then(r => r.ok ? r.json() : { items: [] })
        .then(d => setAdjustments(Array.isArray(d.items) ? d.items : []))
        .catch(() => {});
    };
    window.addEventListener("admin_time_adjustment_updated", onAdj);
    return () => window.removeEventListener("admin_time_adjustment_updated", onAdj);
  }, [emp?.id, token]);

  // [from, to) bracketing the selected calendar day on the ORGANIZATION's
  // clock. It used to subtract a fixed 5.5 hours, so every non-IST workspace
  // queried the wrong 24 hours — while the date picker beside it already used
  // getOrgTimeZone().
  const browserRangeBounds = useCallback(
    () => orgDayBoundsUTC(selectedDate), [selectedDate]);

  useEffect(() => {
    if (panelTab !== "browser") return;
    const tok = token || readToken();
    if (!tok || !emp.id) return;
    const [from, to] = browserRangeBounds();
    const qs = `user_id=${emp.id}&from=${encodeURIComponent(from.toISOString())}&to=${encodeURIComponent(to.toISOString())}`;
    setBrowserLoading(true);
    Promise.all([
      authFetch(`${API_BASE}/browser/summary?${qs}`, { headers: { Authorization: `Bearer ${tok}` } }).then(r => r.ok ? r.json() : null),
      authFetch(`${API_BASE}/browser/history?${qs}&limit=500`, { headers: { Authorization: `Bearer ${tok}` } }).then(r => r.ok ? r.json() : []),
      authFetch(`${API_BASE}/browser/trend?user_id=${emp.id}&days=14`, { headers: { Authorization: `Bearer ${tok}` } }).then(r => r.ok ? r.json() : null),
    ]).then(([s, h, t]) => {
      setBrowserSummary(s);
      setBrowserHistory(Array.isArray(h) ? h : []);
      setBrowserTrend(t);
    }).catch(() => {}).finally(() => setBrowserLoading(false));
  }, [panelTab, selectedDate, emp.id, token, browserRangeBounds]);

  // Build timeline rows for ALL sessions sorted chronologically (morning → evening).
  // Each session expands into work/break/break_ongoing segments.
  // Handles "ghost breaks": a break with no break_end whose session was superseded
  // by a later session (employee re-clocked-in without ever closing the break).
  const buildSessionRows = () => {
    const rows = [];
    const now  = new Date(nowMs);
    const ensureZ = (s) => (s && !s.endsWith("Z") && !/[+-]\d{2}:\d{2}$/.test(s)) ? s + "Z" : s;

    // Only show sessions whose IST calendar day matches the picked date.
    // `selectedDate` is already a YYYY-MM-DD IST string, no conversion needed.
    const dayKey = selectedDate;

    // Deduplicate by clock-in minute — safety net for DB duplicate rows.
    const seenMin = new Map();
    allSessions.forEach(sess => {
      const cin = new Date(ensureZ(sess.clock_in_time));
      const cinIST = cin.toLocaleDateString("en-CA", { timeZone: getOrgTimeZone() });
      if (cinIST !== dayKey) return;
      const key = cin.toLocaleString("en-CA", { timeZone: getOrgTimeZone(), year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false }).replace(/\D/g,"");
      const prev = seenMin.get(key);
      if (!prev || (sess.breaks || []).length > (prev.breaks || []).length) seenMin.set(key, sess);
    });

    // Sort sessions chronologically: earliest clock-in first
    const sortedSessions = [...seenMin.values()].sort((a, b) =>
      new Date(ensureZ(a.clock_in_time)) - new Date(ensureZ(b.clock_in_time))
    );

    sortedSessions.forEach((sess, sessIdx) => {
      const clockIn  = new Date(ensureZ(sess.clock_in_time));
      const clockOut = sess.clock_out_time ? new Date(ensureZ(sess.clock_out_time)) : null;

      // If this session has no clock_out, cap it at the next session's clock_in
      // (employee re-started tracker without clocking out from previous session).
      const nextSession    = sortedSessions[sessIdx + 1];
      const nextSessionStart = nextSession ? new Date(ensureZ(nextSession.clock_in_time)) : null;
      const sessionEnd = clockOut || nextSessionStart || now;

      const dateStr = clockIn.toLocaleDateString("en-IN", { day: "2-digit", month: "long", year: "numeric" });

      let cursor = clockIn;
      const sortedBreaks = [...(sess.breaks || [])].sort((a, b) => new Date(ensureZ(a.break_start)) - new Date(ensureZ(b.break_start)));

      sortedBreaks.forEach((b, bIdx) => {
        const bStart = new Date(ensureZ(b.break_start));
        if (b.break_end) {
          const bEnd = new Date(ensureZ(b.break_end));
          if (bStart > cursor) rows.push({ date: dateStr, start: cursor, end: bStart, type: "work", clockIn, clockOut });
          rows.push({ date: dateStr, start: bStart, end: bEnd, type: "break", reason: b.reason });
          cursor = bEnd;
        } else if (b.is_active === false) {
          // Zombie break: break_end never saved but employee resumed (is_active=false).
          // Skip — work segment continues through without advancing cursor.
        } else {
          // No break_end and is_active is not explicitly false.
          // Only treat as "truly ongoing" if:
          //   1. This is the LAST break in the session, AND
          //   2. The session itself has no clock_out, AND
          //   3. There is no later session (meaning the employee hasn't re-clocked-in).
          const isLastBreak    = bIdx === sortedBreaks.length - 1;
          const hasLaterSession = nextSessionStart !== null;
          const sessionClosed   = clockOut !== null;
          const isTrulyOngoing  = isLastBreak && !sessionClosed && !hasLaterSession;

          // Auto-close time: next break, or next session start, or clock-out, or now
          const nextBreak   = sortedBreaks[bIdx + 1];
          const autoCloseAt = nextBreak
            ? new Date(ensureZ(nextBreak.break_start))
            : (nextSessionStart || clockOut || (isTrulyOngoing ? now : sessionEnd));

          if (bStart > cursor) rows.push({ date: dateStr, start: cursor, end: bStart, type: "work", clockIn, clockOut });

          if (isTrulyOngoing) {
            rows.push({ date: dateStr, start: bStart, end: now, type: "break_ongoing" });
            cursor = now;
          } else {
            // Ghost break — cap it and continue
            rows.push({ date: dateStr, start: bStart, end: autoCloseAt, type: "break", reason: b.reason });
            cursor = autoCloseAt;
          }
        }
      });

      // Remaining time after last break → work segment
      if (cursor < sessionEnd) {
        const type = (!clockOut && !nextSessionStart) ? "work_ongoing" : "work";
        rows.push({ date: dateStr, start: cursor, end: sessionEnd, type, clockIn, clockOut });
      }
    });

    // Carve connectivity gaps out of the work segments. These are deducted from
    // worked time server-side, so a work row that spans one would otherwise
    // read "Worked for <full span>" and hide the deduction entirely — the
    // reason a 2h36m offline gap went unnoticed until an employee was blocked
    // at clock-out with no explanation.
    const offlineGaps = [];
    sortedSessions.forEach(sess => {
      (sess.offline_intervals || []).forEach(oi => {
        const gs = new Date(ensureZ(oi.offline_start));
        const ge = new Date(ensureZ(oi.offline_end));
        if (ge > gs) offlineGaps.push({ start: gs, end: ge, capped: !!oi.capped });
      });
    });

    let finalRows = rows;
    if (offlineGaps.length) {
      finalRows = [];
      rows.forEach(r => {
        if (r.type !== "work" && r.type !== "work_ongoing") { finalRows.push(r); return; }
        const overlapping = offlineGaps
          .filter(g => g.end > r.start && g.start < r.end)
          .sort((a, b) => a.start - b.start);
        if (!overlapping.length) { finalRows.push(r); return; }
        let cur = r.start;
        overlapping.forEach(g => {
          const gs = new Date(Math.max(g.start.getTime(), r.start.getTime()));
          const ge = new Date(Math.min(g.end.getTime(), r.end.getTime()));
          if (gs > cur) finalRows.push({ ...r, start: cur, end: gs });
          finalRows.push({ date: r.date, start: gs, end: ge, type: "offline", capped: g.capped });
          if (ge > cur) cur = ge;
        });
        if (cur < r.end) finalRows.push({ ...r, start: cur, end: r.end });
      });
    }

    // Final sort: chronological by start time (morning → evening)
    finalRows.sort((a, b) => a.start - b.start);
    return finalRows;
  };

  const fmt12 = (d) => d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: getOrgTimeZone() });
  // Re-derive only when the underlying data ticks (live clock once a second,
  // the picked day, or freshly-fetched sessions/adjustments/apps) instead of
  // rebuilding these arrays on every unrelated re-render. buildSessionRows is
  // a pure function of (nowMs, selectedDate, allSessions), so those are its
  // exhaustive inputs; eslint can't see through the helper call so the dep
  // list is asserted manually.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const sessionRows = useMemo(() => buildSessionRows(), [allSessions, selectedDate, nowMs]);
  // Active admin-credited adjustments for the picked IST day. Reverted rows
  // (is_active=false) drop out so the timeline matches the worked-hours math.
  const dayAdjustments = useMemo(
    () => adjustments.filter(a => a.is_active && a.date === selectedDate),
    [adjustments, selectedDate],
  );
  const topApps = useMemo(
    () => [...appHistory].sort((a, b) => b.total_seconds - a.total_seconds).slice(0, 8),
    [appHistory],
  );
  const PALETTE = ["#818CF8","#34D399","#F59E0B","#60A5FA","#F472B6","#A78BFA","#FB923C","#2DD4BF"];

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(16,24,40,0.55)", zIndex: 3000, display: "flex", alignItems: "center", justifyContent: "flex-end" }} onClick={onClose}>
      <div style={{ width: 480, maxWidth: "95vw", background: C.surface, height: "100vh", overflowY: "auto", boxShadow: "-8px 0 40px rgba(0,0,0,.2)", display: "flex", flexDirection: "column" }} onClick={e => e.stopPropagation()}>
        <div style={{ padding: "16px 20px", borderBottom: `1px solid ${C.border}`, display: "flex", justifyContent: "space-between", alignItems: "center", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, flexShrink: 0 }}>
          <p style={{ fontWeight: 800, fontSize: 16, color: "#fff" }}>{T.employeeDetails || "Employee Details"}</p>
          <button onClick={onClose} style={{ background: "rgba(255,255,255,0.2)", border: "none", cursor: "pointer", borderRadius: 8, padding: 6 }}><Icon n="x" s={16} c="#fff" /></button>
        </div>
        <div style={{ padding: "20px 24px 0" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 16 }}>
            <div style={{ width: 60, height: 60, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 900, fontSize: 22 }}>{initials(emp.name)}</div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <p style={{ fontWeight: 800, fontSize: 18, color: C.text }}>{emp.name}</p>
              <p style={{ fontSize: 13, color: C.textSub }}>{emp.designation || emp.role}</p>
              <Badge status={emp.status} />
            </div>
          </div>
          <div style={{ display: "flex", gap: 2, background: C.bg, borderRadius: 8, padding: 3, marginBottom: 12 }}>
            {[["info",T.workerDetails||"Worker's Details"],["intel",T.intelligence||"Intelligence"],["apps",T.appUsage||"App Usage"],["browser",T.browserActivity||"Browser"],["timeline",T.clockSummary||"Clock Summary"]].map(([k,l]) => (
              <button key={k} onClick={() => setPanelTab(k)} style={{
                flex: 1, padding: "7px", borderRadius: 6, border: "none", cursor: "pointer",
                background: panelTab === k ? C.surface : "transparent",
                color: panelTab === k ? C.brand : C.textSub,
                fontWeight: panelTab === k ? 700 : 500, fontSize: 12, fontFamily: "inherit",
                boxShadow: panelTab === k ? "0 1px 4px rgba(0,0,0,0.08)" : "none",
              }}>{l}</button>
            ))}
          </div>

          {/* Date filter — applies to App Usage, Browser and Clock Summary.
              Hidden on Worker's Details because that tab is a live snapshot
              of the current state, not a historical view. */}
          {panelTab !== "info" && (
            <div style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "10px 12px", marginBottom: 14,
              background: C.bg, borderRadius: 8,
              border: `1px solid ${C.border}`,
            }}>
              <Icon n="calendar" s={14} c={C.textSub} />
              <span style={{ fontSize: 12, color: C.textSub, fontWeight: 500, flexShrink: 0 }}>
                {T.showingDataFor || "Showing data for"}
              </span>
              <input
                type="date"
                value={selectedDate}
                max={istToday()}
                onChange={(e) => setSelectedDate(e.target.value || istToday())}
                style={{
                  flex: 1, fontSize: 12, padding: "5px 8px",
                  borderRadius: 6, border: `1px solid ${C.border}`,
                  background: C.surface, color: C.text,
                  fontFamily: "inherit", cursor: "pointer", colorScheme: isDark ? "dark" : "light",
                }}
              />
              {!isToday && (
                <button
                  onClick={() => setSelectedDate(istToday())}
                  style={{
                    fontSize: 11, fontWeight: 600,
                    padding: "5px 10px", borderRadius: 6,
                    border: `1px solid ${C.border}`,
                    background: C.surface, color: C.brand,
                    cursor: "pointer", fontFamily: "inherit",
                  }}
                >
                  {T.today || "Today"}
                </button>
              )}
            </div>
          )}
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "0 24px 24px" }}>
          {panelTab === "info" && (
            <>
              {[
                { icon: "mail",      label: T.email||"Email",           val: emp.email },
                { icon: "phone",     label: T.phone||"Phone",           val: emp.phone },
                { icon: "users",     label: T.team_label||"Team",       val: emp.team },
                { icon: "briefcase", label: T.department||"Department", val: emp.department },
                { icon: "tag",       label: T.employmentType||"Employment Type", val: emp.employment_type },
                { icon: "clock",     label: T.clockInCol||"Clock In",   val: emp.clock_in_time ? fmtTimeIST(emp.clock_in_time) : "—" },
                { icon: "monitor",   label: T.activeApp||"Active App",  val: emp.current_app ? getAppMeta(emp.current_app).label : "—" },
              ].map(({ icon, label, val }) => val ? (
                <div key={label} style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 0", borderBottom: `1px solid ${C.borderLight}` }}>
                  <Icon n={icon} s={15} c={C.textLight} />
                  <span style={{ fontSize: 12, color: C.textSub, width: 130, flexShrink: 0 }}>{label}</span>
                  <span style={{ fontSize: 13, fontWeight: 600, color: C.text }}>{val}</span>
                </div>
              ) : null)}
            </>
          )}
          {panelTab === "apps" && (() => {
            const CAT_BADGE = {
              productive:  { label: "productive",   color: "#16A34A", bg: "#F0FDF4" },
              neutral:     { label: "neutral",      color: "#7C3AED", bg: "#F5F3FF" },
              distracting: { label: "unproductive", color: "#DC2626", bg: "#FEF2F2" },
              blocked:     { label: "blocked",      color: "#374151", bg: "#F3F4F6" },
            };
            const totalSecs = topApps.reduce((s, x) => s + x.total_seconds, 0) || 1;
            const totalActive = topApps.reduce((s, x) => s + (x.active_seconds || 0), 0);
            const activePct = Math.round((totalActive / totalSecs) * 100);
            return (
              <div>
                {topApps.length === 0 ? (
                  <div style={{ textAlign: "center", padding: "40px 0", color: C.textSub }}>
                    <div style={{ fontSize: 32, marginBottom: 8 }}>📊</div>
                    <p style={{ margin: 0, fontSize: 13 }}>
                      {isToday
                        ? (T.noAppUsageToday || "No app usage data for today")
                        : `${T.noAppUsageForDate || "No app usage data for"} ${selectedDate}`}
                    </p>
                  </div>
                ) : (
                  <>
                    {/* Summary strip */}
                    <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
                      {[
                        { label: T.totalTime || "Total Time", val: fmtSecs(totalSecs) },
                        { label: T.activeTime || "Active",    val: fmtSecs(totalActive) },
                        { label: T.productivity || "Productive", val: `${activePct}%` },
                      ].map(({ label, val }) => (
                        <div key={label} style={{ flex: 1, padding: "8px 10px", background: C.bg, borderRadius: 8, border: `1px solid ${C.border}`, textAlign: "center" }}>
                          <div style={{ fontSize: 13, fontWeight: 800, color: C.brand }}>{val}</div>
                          <div style={{ fontSize: 10, color: C.textSub, marginTop: 2 }}>{label}</div>
                        </div>
                      ))}
                    </div>
                    <div style={{ display: "flex", justifyContent: "center", marginBottom: 16 }}>
                      <DonutChart size={160} data={topApps.map((a, i) => ({ label: a.app_name, value: a.total_seconds, color: PALETTE[i % PALETTE.length] }))} />
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                      {topApps.map((a, i) => {
                        const cat = CAT_BADGE[a.category] || APP_CATEGORY(a.app_name);
                        const appMeta = getAppMeta(a.app_name);
                        const pct = Math.round((a.total_seconds / totalSecs) * 100);
                        const activePctApp = a.total_seconds > 0 ? Math.round(((a.active_seconds || 0) / a.total_seconds) * 100) : 0;
                        return (
                          <div key={i} style={{ padding: "10px 12px", background: C.bg, borderRadius: 8, border: `1px solid ${C.border}` }}>
                            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
                              <div style={{ width: 8, height: 8, borderRadius: "50%", background: PALETTE[i % PALETTE.length], flexShrink: 0 }} />
                              <div style={{ width: 26, height: 26, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                                <AppIcon app={appMeta} size={22} />
                              </div>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontSize: 13, fontWeight: 600, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{appMeta?.label || a.app_name}</div>
                                <div style={{ fontSize: 10, color: C.textSub }}>{fmtSecs(a.total_seconds)} · {activePctApp}% active</div>
                              </div>
                              <span style={{ fontSize: 10, fontWeight: 700, color: cat.color, background: cat.bg, padding: "2px 7px", borderRadius: 99, flexShrink: 0 }}>{cat.label}</span>
                              <span style={{ fontSize: 11, fontWeight: 700, color: C.brand, flexShrink: 0 }}>{pct}%</span>
                            </div>
                            <div style={{ height: 3, borderRadius: 99, background: C.borderLight, overflow: "hidden" }}>
                              <div style={{ height: "100%", width: `${pct}%`, background: `linear-gradient(90deg,${C.brand},#44B97B)`, borderRadius: 99 }} />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </>
                )}
              </div>
            );
          })()}
          {panelTab === "browser" && (() => {
            // Build hourly buckets (0–23) of active browser seconds. We bucket by
            // the session's start hour — good enough for a bar overview; a more
            // accurate version would split sessions that span the hour boundary.
            const hours = Array(24).fill(0);
            browserHistory.forEach(h => {
              const hr = new Date(h.started_at).getHours();
              hours[hr] += (h.duration_sec || 0);
            });
            const maxHr = Math.max(1, ...hours);
            const sites = browserSummary?.sites || [];
            const pieData = sites.slice(0, 8).map((s, i) => ({
              label: s.domain, value: s.duration, color: PALETTE[i % PALETTE.length],
            }));
            return (
              <div>
                {/* Header summary — the date itself is set via the calendar
                    filter above the tabs, so we only need the live totals. */}
                {browserSummary && (
                  <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 14 }}>
                    <span style={{ fontSize: 11, color: C.textSub }}>
                      {fmtSecs(browserSummary.total_sec || 0)} · {browserSummary.active_pct || 0}% {T.active || "active"}
                    </span>
                  </div>
                )}

                {/* ── Productivity score ── */}
                {browserSummary && browserSummary.total_sec > 0 && (() => {
                  const score = browserSummary.productivity_score || 0;
                  const col = score >= 70 ? "#047857" : score >= 40 ? "#B45309" : "#B91C1C";
                  const cats = browserSummary.categories || { work: 0, neutral: 0, distraction: 0 };
                  const tot = Math.max(1, cats.work + cats.neutral + cats.distraction);
                  return (
                    <div style={{ padding: "10px 12px", borderRadius: 10, border: `1px solid ${C.border}`, background: C.bg, marginBottom: 14 }}>
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
                        <span style={{ fontSize: 11, color: C.textSub, fontWeight: 700, letterSpacing: ".3px", textTransform: "uppercase" }}>
                          {T.productivityScoreLabel || "Productivity score"}
                        </span>
                        <span style={{ fontSize: 18, fontWeight: 900, color: col, fontVariantNumeric: "tabular-nums" }}>{score}</span>
                      </div>
                      <div style={{ display: "flex", height: 8, borderRadius: 99, overflow: "hidden", background: C.borderLight }}>
                        <div style={{ width: `${(cats.work/tot)*100}%`,        background: BROWSER_CAT.work.color }} />
                        <div style={{ width: `${(cats.neutral/tot)*100}%`,     background: BROWSER_CAT.neutral.color }} />
                        <div style={{ width: `${(cats.distraction/tot)*100}%`, background: BROWSER_CAT.distraction.color }} />
                      </div>
                      <div style={{ display: "flex", gap: 12, marginTop: 8, fontSize: 10, color: C.textSub }}>
                        <span><span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: BROWSER_CAT.work.color, marginRight: 4 }} />{T.work || "Work"} {fmtSecs(cats.work)}</span>
                        <span><span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: BROWSER_CAT.neutral.color, marginRight: 4 }} />{T.neutral || "Neutral"} {fmtSecs(cats.neutral)}</span>
                        <span><span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: BROWSER_CAT.distraction.color, marginRight: 4 }} />{T.distraction || "Distraction"} {fmtSecs(cats.distraction)}</span>
                      </div>
                    </div>
                  );
                })()}

                {/* ── 14-day productivity trend ── */}
                {browserTrend?.series?.length > 0 && browserTrend.series.some(d => d.score !== null) && (() => {
                  const series = browserTrend.series;
                  const W = 300, H = 60, PAD = 4;
                  const xs = (i) => PAD + (i * (W - 2 * PAD)) / Math.max(1, series.length - 1);
                  const ys = (v) => H - PAD - ((v / 100) * (H - 2 * PAD));
                  const points = series.map((d, i) => d.score === null ? null : [xs(i), ys(d.score)]);
                  const path = points.reduce((p, pt, i) => {
                    if (!pt) return p;
                    const prev = points.slice(0, i).reverse().find(Boolean);
                    return p + (prev ? ` L${pt[0]},${pt[1]}` : `M${pt[0]},${pt[1]}`);
                  }, "");
                  const scored = series.filter(d => d.score !== null);
                  const avg = scored.length ? Math.round(scored.reduce((s, d) => s + d.score, 0) / scored.length) : 0;
                  const last = scored[scored.length - 1]?.score ?? 0;
                  const first = scored[0]?.score ?? 0;
                  const delta = Math.round(last - first);
                  return (
                    <div style={{ padding: "10px 12px", borderRadius: 10, border: `1px solid ${C.border}`, background: C.surface, marginBottom: 14 }}>
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
                        <span style={{ fontSize: 11, color: C.textSub, fontWeight: 700, letterSpacing: ".3px", textTransform: "uppercase" }}>
                          {T.trendPeriod14Day || "14-day trend"}
                        </span>
                        <span style={{ fontSize: 11, color: C.textSub }}>
                          {T.avg || "avg"} <b style={{ color: C.text }}>{avg}</b>
                          {delta !== 0 && (
                            <span style={{ marginLeft: 8, color: delta > 0 ? "#047857" : "#B91C1C", fontWeight: 700 }}>
                              {delta > 0 ? "▲" : "▼"} {Math.abs(delta)}
                            </span>
                          )}
                        </span>
                      </div>
                      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block" }}>
                        <line x1={PAD} y1={ys(50)} x2={W - PAD} y2={ys(50)} stroke={C.borderLight} strokeDasharray="2 3" />
                        {path && <path d={path} fill="none" stroke={C.brand} strokeWidth="2" />}
                        {points.map((pt, i) => pt && (
                          <circle key={i} cx={pt[0]} cy={pt[1]} r="2.5" fill={series[i].score >= 70 ? "#047857" : series[i].score >= 40 ? "#B45309" : "#B91C1C"}>
                            <title>{series[i].date}: {series[i].score}</title>
                          </circle>
                        ))}
                      </svg>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: C.textLight, marginTop: 2 }}>
                        <span>{series[0]?.date?.slice(5)}</span>
                        <span>{series[series.length - 1]?.date?.slice(5)}</span>
                      </div>
                    </div>
                  );
                })()}

                {browserLoading ? (
                  <div style={{ padding: "30px 0", textAlign: "center", color: C.textSub, fontSize: 12 }}>{T.loadingEllipsis || "Loading…"}</div>
                ) : sites.length === 0 ? (() => {
                  // Fall back to desktop-tracker browser app time when no extension data exists
                  const BROWSER_RE = /\b(chrome|chromium|firefox|msedge|edge|safari|brave|opera|vivaldi|arc)\b/i;
                  const browserApps = appHistory.filter(a => BROWSER_RE.test(a.app_name || ""));
                  const totalBrowserSec = browserApps.reduce((s, a) => s + (a.total_seconds || 0), 0);
                  return (
                    <div>
                      {browserApps.length > 0 ? (
                        <>
                          {/* Summary strip */}
                          <div style={{ padding: "10px 12px", borderRadius: 10, border: `1px solid ${C.border}`, background: C.bg, marginBottom: 14, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                            <span style={{ fontSize: 11, color: C.textSub, fontWeight: 700, textTransform: "uppercase", letterSpacing: ".3px" }}>
                              {T.browserTimeTracked || "Browser time (desktop tracker)"}
                            </span>
                            <span style={{ fontSize: 15, fontWeight: 800, color: C.brand }}>{fmtSecs(totalBrowserSec)}</span>
                          </div>
                          {/* Per-browser app bars */}
                          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 16 }}>
                            {browserApps.sort((a, b) => b.total_seconds - a.total_seconds).map((a, i) => {
                              const appMeta = getAppMeta(a.app_name);
                              const pct = totalBrowserSec > 0 ? Math.round((a.total_seconds / totalBrowserSec) * 100) : 0;
                              return (
                                <div key={a.app_name} style={{ padding: "10px 12px", background: C.surface, borderRadius: 8, border: `1px solid ${C.border}` }}>
                                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
                                    <div style={{ width: 24, height: 24, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                                      <AppIcon app={appMeta} size={20} />
                                    </div>
                                    <span style={{ flex: 1, fontSize: 13, fontWeight: 600, color: C.text }}>{appMeta?.label || a.app_name}</span>
                                    <span style={{ fontSize: 12, color: C.textSub, fontVariantNumeric: "tabular-nums" }}>{fmtSecs(a.total_seconds)}</span>
                                    <span style={{ fontSize: 11, fontWeight: 700, color: C.brand }}>{pct}%</span>
                                  </div>
                                  <div style={{ height: 4, borderRadius: 99, background: C.borderLight, overflow: "hidden" }}>
                                    <div style={{ height: "100%", width: `${pct}%`, background: `linear-gradient(90deg,${C.brand},#44B97B)`, borderRadius: 99 }} />
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                          {/* Upgrade prompt */}
                          <div style={{ padding: "12px 14px", borderRadius: 10, border: `1px dashed ${C.border}`, background: C.bg, display: "flex", gap: 10, alignItems: "flex-start" }}>
                            <Icon n="monitor" s={16} c={C.textLight} style={{ flexShrink: 0, marginTop: 1 }} />
                            <div>
                              <p style={{ margin: 0, fontSize: 12, fontWeight: 600, color: C.text }}>{T.browserExtensionTitle || "Want URL-level tracking?"}</p>
                              <p style={{ margin: "3px 0 0", fontSize: 11, color: C.textSub }}>{T.browserExtensionNote || "Install the Temveris browser extension to see exact websites, tab titles, and per-domain time."}</p>
                            </div>
                          </div>
                        </>
                      ) : (
                        <div style={{ textAlign: "center", padding: "40px 0", color: C.textSub }}>
                          <div style={{ fontSize: 32, marginBottom: 8 }}>🌐</div>
                          <p style={{ margin: 0, fontSize: 13 }}>{T.noBrowserActivity || "No browser activity in this range"}</p>
                          <p style={{ margin: "6px 0 0", fontSize: 11 }}>{T.browserExtensionNote || "Install the Temveris browser extension to capture URLs and tab titles."}</p>
                        </div>
                      )}
                    </div>
                  );
                })() : (
                  <>
                    {/* ── Time-per-domain donut + legend ── */}
                    <div style={{ display: "flex", justifyContent: "center", marginBottom: 16 }}>
                      <DonutChart size={160} data={pieData} />
                    </div>

                    {/* ── Hourly bar chart (active seconds per hour) ── */}
                    <p style={{ fontSize: 12, fontWeight: 700, color: C.text, margin: "4px 0 8px" }}>{T.hourlyActivity || "Hourly activity"}</p>
                    <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 60, padding: "0 2px", marginBottom: 8 }}>
                      {hours.map((v, i) => (
                        <div key={i} title={`${i}:00 — ${fmtSecs(v)}`}
                          style={{ flex: 1, minWidth: 0, height: `${Math.max(2, (v / maxHr) * 100)}%`, background: v > 0 ? `linear-gradient(to top, ${C.brand}, ${C.brandDark})` : C.borderLight, borderRadius: "3px 3px 0 0", transition: "height .3s" }} />
                      ))}
                    </div>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: C.textLight, marginBottom: 18 }}>
                      <span>0h</span><span>6h</span><span>12h</span><span>18h</span><span>23h</span>
                    </div>

                    {/* ── Per-domain breakdown ── */}
                    <p style={{ fontSize: 12, fontWeight: 700, color: C.text, margin: "4px 0 8px" }}>{T.topWebsites || "Top websites"}</p>
                    <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 18 }}>
                      {sites.slice(0, 10).map((s, i) => (
                        <div key={s.domain} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 10px", background: C.bg, borderRadius: 8, border: `1px solid ${C.border}` }}>
                          <div style={{ width: 8, height: 8, borderRadius: "50%", background: PALETTE[i % PALETTE.length], flexShrink: 0 }} />
                          <div style={{ width: 20, height: 20, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                            {s.favicon_url
                              ? <img src={s.favicon_url} alt="" loading="lazy" decoding="async" style={{ width: 16, height: 16, objectFit: "contain" }} onError={e => { e.currentTarget.style.display = "none"; }} />
                              : <Icon n="monitor" s={13} c={C.textLight} />}
                          </div>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <p style={{ fontSize: 12, fontWeight: 600, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.domain}</p>
                            <p style={{ fontSize: 10, color: C.textSub }}>{s.visits} {s.visits === 1 ? (T.visit || "visit") : (T.visits || "visits")} · {fmtSecs(s.duration)}</p>
                          </div>
                          {s.category && (() => {
                            const cc = browserCatCfg(s.category);
                            return (
                              <span style={{ fontSize: 9, fontWeight: 700, padding: "2px 6px", borderRadius: 99, color: cc.color, background: cc.bg, border: `1px solid ${cc.bdr}`, flexShrink: 0, letterSpacing: ".2px", textTransform: "uppercase" }}>{cc.label}</span>
                            );
                          })()}
                          <span style={{ fontSize: 11, fontWeight: 700, color: C.brand, flexShrink: 0 }}>{s.percentage}%</span>
                        </div>
                      ))}
                    </div>

                    {/* ── Recent tab timeline ── */}
                    <p style={{ fontSize: 12, fontWeight: 700, color: C.text, margin: "4px 0 8px" }}>{T.recentTabs || "Recent tabs"}</p>
                    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                      {browserHistory.slice(0, 20).map(h => (
                        <a key={h.id} href={h.url} target="_blank" rel="noreferrer"
                          style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", borderRadius: 6, color: "inherit", textDecoration: "none", fontSize: 11, borderLeft: `3px solid ${C.brand}`, background: C.bg }}
                          onMouseEnter={e => { e.currentTarget.style.background = C.brandBg; }}
                          onMouseLeave={e => { e.currentTarget.style.background = C.bg; }}>
                          {h.favicon_url
                            ? <img src={h.favicon_url} alt="" loading="lazy" decoding="async" style={{ width: 14, height: 14, objectFit: "contain", flexShrink: 0 }} onError={e => { e.currentTarget.style.display = "none"; }} />
                            : <Icon n="monitor" s={12} c={C.textLight} />}
                          <span style={{ color: C.textSub, width: 54, flexShrink: 0, fontVariantNumeric: "tabular-nums" }}>
                            {new Date(h.started_at).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: getOrgTimeZone() })}
                          </span>
                          <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: C.text, fontWeight: 500 }}>
                            {h.tab_title || h.domain}
                          </span>
                          <span style={{ color: C.brand, fontWeight: 600, flexShrink: 0 }}>{fmtSecs(h.duration_sec)}</span>
                        </a>
                      ))}
                    </div>
                  </>
                )}
              </div>
            );
          })()}
          {panelTab === "intel" && (
            // Workforce Intelligence / anomaly detection is a tiered capability
            // (activity.anomaly — Governed+). The backend projection decides
            // SHOW vs LOCK per plan/role; the Gate renders whichever it says.
            <Gate capability="activity.anomaly">
              <Suspense fallback={
                <div style={{ padding: "40px 0", textAlign: "center", color: C.textSub, fontSize: 13 }}>
                  {T.adminDashboardLoadingIntelligence || "Loading Intelligence…"}
                </div>
              }>
                <IntelligencePanel emp={emp} date={selectedDate} />
              </Suspense>
            </Gate>
          )}
          {panelTab === "timeline" && (
            <div>
              <p style={{ fontSize: 13, fontWeight: 700, color: C.text, marginBottom: 12 }}>
                {isToday
                  ? (T.todaysClockSummary || "Today's Clock Summary")
                  : `${T.clockSummary || "Clock Summary"} — ${selectedDate}`}
              </p>
              {sessionRows.length === 0 && dayAdjustments.length === 0 ? (
                <div style={{ textAlign: "center", padding: "40px 0", color: C.textSub }}>
                  <div style={{ fontSize: 32, marginBottom: 8 }}>🕐</div>
                  <p style={{ margin: 0, fontSize: 13 }}>
                    {isToday
                      ? (T.noActivityToday || "No activity today")
                      : `${T.noActivityForDate || "No activity on"} ${selectedDate}`}
                  </p>
                </div>
              ) : (
                <div style={{ overflowX: "auto", border: `1px solid ${C.border}`, borderRadius: 8 }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                    <thead>
                      <tr style={{ background: isDark ? C.surfaceAlt : C.bg }}>
                        {[T.startTime||"Start Time",T.endTime||"End Time",T.activityDuration||"Activity Duration"].map(h => (
                          <th key={h} style={{ padding: "10px 12px", textAlign: "left", color: isDark ? C.text : C.textSub, fontWeight: 700, borderBottom: `1px solid ${C.border}`, whiteSpace: "nowrap" }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {sessionRows.map((row, i) => {
                        const dur = Math.floor((row.end - row.start) / 1000);
                        const isWork        = row.type === "work";
                        const isWorkOngoing = row.type === "work_ongoing";
                        const isBreak       = row.type === "break";
                        const isOngoingBreak = row.type === "break_ongoing";
                        const isOffline     = row.type === "offline";
                        const rowBg = (isWork || isWorkOngoing) ? (isDark ? "#0D2A1A" : "#F0FDF4") : isOngoingBreak ? (isDark ? "#3D2A0A" : "#FFFBEB") : isOffline ? (isDark ? "#2A1A2E" : "#FAF5FF") : C.surface;
                        const workTextColor = isDark ? "#5FCC93" : C.text;
                        const durBadgeBg = isDark ? "#243042" : "#E5E7EB";
                        const durBadgeColor = isDark ? "#F0F4F8" : "#0F172A";
                        return (
                          <tr key={i} style={{ background: rowBg, borderBottom: `1px solid ${C.border}` }}>
                            <td style={{ padding: "10px 12px", color: C.text }}>{fmt12(row.start)}</td>
                            <td style={{ padding: "10px 12px", color: isOngoingBreak ? (isDark ? "#F59E0B" : "#D97706") : C.text }}>
                              {isOngoingBreak ? (T.onBreak || "On Break") : isWorkOngoing ? (T.working || "Working…") : fmt12(row.end)}
                            </td>
                            <td style={{ padding: "10px 12px" }}>
                              {(isWork || isWorkOngoing) ? (
                                <span style={{ color: C.text }}>{T.workedFor || "Worked for"} &nbsp;<span style={{ background: durBadgeBg, color: durBadgeColor, padding: "2px 8px", borderRadius: 6, fontWeight: 600, fontSize: 12 }}>{fmtSecs(dur)}</span></span>
                              ) : isOffline ? (
                                <span style={{ color: isDark ? "#C084FC" : "#7E22CE", fontWeight: 600 }}>
                                  ⚡ {T.offlineNotCounted || "Offline — not counted"} — {fmtSecs(dur)}
                                  {row.capped ? (
                                    <span style={{ fontWeight: 500, fontStyle: "italic" }}> (capped)</span>
                                  ) : null}
                                </span>
                              ) : isOngoingBreak ? (
                                <span style={{ color: isDark ? "#F59E0B" : "#D97706", fontWeight: 600 }}>⏸ On Break — {fmtSecs(dur)}</span>
                              ) : (
                                <span style={{ color: C.textSub, fontStyle: "italic" }}>
                                  Took a break{row.reason ? ` — ${row.reason}` : " from work"}
                                </span>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                      {/* Admin-credited time adjustments — one row per active
                          entry for the picked day. These are additive (no real
                          start/end), so they render with an "Admin-added"
                          marker and the credited duration on the right. */}
                      {dayAdjustments.map((adj) => {
                        const rowBg = isDark ? "#1F2A3A" : "#F5F3FF";
                        const durBadgeBg    = isDark ? "#3B2F66" : "#EDE9FE";
                        const durBadgeColor = isDark ? "#E9D5FF" : "#5B21B6";
                        const labelColor    = isDark ? "#C4B5FD" : "#6D28D9";
                        const credited = `+${adj.duration || fmtSecs(adj.seconds_added || 0)}`;
                        const creator  = adj.created_by_name ? ` · ${adj.created_by_name}` : "";
                        return (
                          <tr key={`adj-${adj.id}`} style={{ background: rowBg, borderBottom: `1px solid ${C.border}` }}>
                            <td style={{ padding: "10px 12px", color: labelColor, fontWeight: 600 }}>
                              {T.adminAdded || "Admin-added"}
                            </td>
                            <td style={{ padding: "10px 12px", color: C.textSub, fontStyle: "italic" }}>
                              {adj.reason || "—"}{creator}
                            </td>
                            <td style={{ padding: "10px 12px" }}>
                              <span style={{ color: C.text }}>
                                {T.creditedTime || "Credited"} &nbsp;
                                <span style={{ background: durBadgeBg, color: durBadgeColor, padding: "2px 8px", borderRadius: 6, fontWeight: 700, fontSize: 12 }}>
                                  {credited}
                                </span>
                              </span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

// ─── TIMELINE VIEW ────────────────────────────────────────────────────────────
const TimelineView = React.memo(({ employees }) => {
  // Fractional hour-of-day on the ORGANIZATION's clock (7:22am → 7.37).
  // This shifted by a fixed +5.5h, which put every non-Indian workspace's
  // bars on the wrong part of the axis while claiming to avoid depending on
  // the admin's browser timezone — it depended on one tenant's instead.
  const istHourOf = orgHourOf;
  const nowHour = orgHourOf(new Date());

  // Each worker's bar spans their REAL clock-in → clock-out (or now, if still
  // working). Absent / never-clocked-in workers get no bar (start == null).
  const rows = employees.map(emp => {
    if (!emp.clock_in_time) return { emp, start: null, end: null };
    const start = istHourOf(parseUTC(emp.clock_in_time));
    const rawEnd = emp.clock_out_time ? istHourOf(parseUTC(emp.clock_out_time)) : nowHour;
    return { emp, start, end: Math.max(rawEnd, start) };
  });

  // Axis scales to the actual data: floor of the earliest clock-in → ceil of
  // the latest activity OR the current hour, whichever is later. This is what
  // makes the scale track the wall clock instead of freezing at a fixed hour.
  const starts = rows.filter(r => r.start != null).map(r => r.start);
  const ends   = rows.filter(r => r.end != null).map(r => r.end);
  const axisStart = Math.floor(starts.length ? Math.min(...starts) : 8);
  let   axisEnd   = Math.ceil(Math.max(nowHour, ...(ends.length ? ends : [18])));
  if (axisEnd - axisStart < 6) axisEnd = axisStart + 6;   // keep it readable
  const span  = axisEnd - axisStart;
  const hours = Array.from({ length: span + 1 }, (_, i) => axisStart + i);

  const label = (h) => {
    const hr = ((Math.round(h) % 24) + 24) % 24;
    if (hr === 0) return "12AM";
    if (hr === 12) return "12PM";
    return hr > 12 ? `${hr - 12}PM` : `${hr}AM`;
  };
  const pct = (h) => ((h - axisStart) / span) * 100;

  return (
    <div style={{ overflowX: "auto" }}>
      <div style={{ minWidth: 800 }}>
        <div style={{ display: "flex", borderBottom: `1px solid ${C.border}`, paddingBottom: 8, marginBottom: 12 }}>
          <div style={{ width: 160, flexShrink: 0 }} />
          <div style={{ flex: 1, position: "relative", height: 16 }}>
            {hours.map(h => (
              <div key={h} style={{ position: "absolute", left: `${pct(h)}%`, transform: "translateX(-50%)", fontSize: 11, color: C.textLight, fontWeight: 600, whiteSpace: "nowrap" }}>
                {label(h)}
              </div>
            ))}
          </div>
        </div>
        {rows.map(({ emp, start, end }) => {
          const cfg = getStatusCfg(emp.status);
          const left  = start == null ? 0 : Math.max(0, Math.min(100, pct(start)));
          const width = start == null ? 0 : Math.max(1.5, Math.min(100 - left, pct(end) - pct(start)));
          return (
            <div key={emp.id} style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
              <div style={{ width: 160, flexShrink: 0, display: "flex", alignItems: "center", gap: 8 }}>
                <div style={{ width: 28, height: 28, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 10 }}>{initials(emp.name)}</div>
                <span style={{ fontSize: 12, fontWeight: 600, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{emp.name}</span>
              </div>
              <div style={{ flex: 1, height: 24, background: C.borderLight, borderRadius: 4, position: "relative" }}>
                {start != null && (
                  <div title={`${fmtTimeIST(emp.clock_in_time)} → ${emp.clock_out_time ? fmtTimeIST(emp.clock_out_time) : "now"}`}
                       style={{ position: "absolute", left: `${left}%`, width: `${width}%`, height: "100%", background: cfg.dot, borderRadius: 4, opacity: 0.75 }} />
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
});
TimelineView.displayName = "TimelineView";

// ─── DATE FILTER PICKER ───────────────────────────────────────────────────────
const DateFilterPicker = ({ onClose, onApply, currentDate }) => {
  // DateFilterPicker is a top-level component, so it must pull translations
  // from the language context explicitly — referencing `T` as a module-level
  // identifier would ReferenceError and crash the picker on open.
  const { T } = useLanguage();
  const [calDate, setCalDate] = useState(currentDate || new Date());
  const [selectedDay, setSelectedDay] = useState(currentDate || new Date());
  const [activePeriod, setActivePeriod] = useState("Today");
  const ref = useRef(null);

  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) onClose(); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [onClose]);

  const year = calDate.getFullYear();
  const month = calDate.getMonth();
  const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  const DAYS = ["Su","Mo","Tu","We","Th","Fr","Sa"];
  const firstDay = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const prevMonth = () => setCalDate(new Date(year, month - 1, 1));
  const nextMonth = () => setCalDate(new Date(year, month + 1, 1));

  const cells = [];
  for (let i = 0; i < firstDay; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);

  const isSelected = (d) => { if (!d) return false; return new Date(year, month, d).toDateString() === selectedDay.toDateString(); };
  const isToday = (d) => { if (!d) return false; return new Date(year, month, d).toDateString() === new Date().toDateString(); };

  const periods = [
    { label: T.today||"Today",       getRange: () => { const t = new Date(); return { from: t, to: t }; } },
    { label: T.thisWeek||"This Week",   getRange: () => { const t = new Date(); const s = new Date(t); s.setDate(t.getDate() - t.getDay()); return { from: s, to: t }; } },
    { label: T.adminDashboardYesterday || "Yesterday",   getRange: () => { const y = new Date(); y.setDate(y.getDate() - 1); return { from: y, to: y }; } },
    { label: T.thisMonth||"This Month",  getRange: () => { const t = new Date(); return { from: new Date(t.getFullYear(), t.getMonth(), 1), to: t }; } },
    { label: T.last7Days||"Last 7 Days", getRange: () => { const t = new Date(); const s = new Date(t); s.setDate(t.getDate() - 6); return { from: s, to: t }; } },
    { label: T.lastMonth||"Last Month",  getRange: () => { const t = new Date(); return { from: new Date(t.getFullYear(), t.getMonth() - 1, 1), to: new Date(t.getFullYear(), t.getMonth(), 0) }; } },
    { label: T.lastWeek||"Last Week",   getRange: () => { const t = new Date(); const s = new Date(t); s.setDate(t.getDate() - t.getDay() - 7); const e = new Date(s); e.setDate(s.getDate() + 6); return { from: s, to: e }; } },
    { label: T.adminDashboardCustom || "Custom",      getRange: () => ({ from: selectedDay, to: selectedDay }) },
  ];

  const handlePeriodClick = (p) => {
    setActivePeriod(p.label);
    if (p.label !== "Custom") { const range = p.getRange(); setSelectedDay(range.from); setCalDate(range.from); }
  };

  const handleDayClick = (d) => { if (!d) return; setSelectedDay(new Date(year, month, d)); setActivePeriod("Custom"); };

  const handleApply = () => {
    const period = periods.find(p => p.label === activePeriod);
    const range = period ? period.getRange() : { from: selectedDay, to: selectedDay };
    onApply({ date: selectedDay, period: activePeriod, range });
    onClose();
  };

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 4000, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(16,24,40,0.3)" }}>
      <div ref={ref} style={{ background: C.surface, borderRadius: 12, boxShadow: "0 20px 60px rgba(0,0,0,0.2)", border: `1px solid ${C.border}`, display: "flex", gap: 0, overflow: "hidden", minWidth: 540 }}>
        <div style={{ padding: 20, borderRight: `1px solid ${C.border}` }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
            <button onClick={prevMonth} style={{ background: "none", border: `1px solid ${C.border}`, borderRadius: 6, width: 28, height: 28, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}><Icon n="chevron_left" s={14} c={C.textSub} /></button>
            <span style={{ fontWeight: 700, fontSize: 15, color: C.text }}>{MONTHS[month]} {year}</span>
            <button onClick={nextMonth} style={{ background: "none", border: `1px solid ${C.border}`, borderRadius: 6, width: 28, height: 28, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}><Icon n="chevron_right" s={14} c={C.textSub} /></button>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 36px)", gap: 2, marginBottom: 4 }}>
            {DAYS.map(d => (<div key={d} style={{ textAlign: "center", fontSize: 12, fontWeight: 600, color: C.textSub, padding: "4px 0" }}>{d}</div>))}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 36px)", gap: 2 }}>
            {cells.map((d, i) => (
              <button key={i} onClick={() => handleDayClick(d)}
                style={{ width: 36, height: 36, borderRadius: "50%", border: "none", background: isSelected(d) ? C.brand : "transparent", color: isSelected(d) ? "#fff" : isToday(d) ? C.brand : d ? C.text : "transparent", fontWeight: isSelected(d) || isToday(d) ? 700 : 400, fontSize: 14, cursor: d ? "pointer" : "default", outline: isToday(d) && !isSelected(d) ? `2px solid ${C.brand}` : "none", outlineOffset: -2, fontFamily: "inherit" }}>
                {d || ""}
              </button>
            ))}
          </div>
          <p style={{ fontSize: 11, color: C.textLight, marginTop: 12, paddingTop: 10, borderTop: `1px solid ${C.borderLight}` }}>{T.adminDashboardInactivePeriod || "inactive period"}</p>
        </div>
        <div style={{ padding: 20, display: "flex", flexDirection: "column", justifyContent: "space-between", minWidth: 200 }}>
          <div>
            <p style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 14 }}>{T.filterByPeriod || "Filter by Period"}</p>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              {periods.map(p => (
                <button key={p.label} onClick={() => handlePeriodClick(p)}
                  style={{ padding: "8px 12px", borderRadius: 20, border: `1.5px solid ${activePeriod === p.label ? C.brand : C.border}`, background: activePeriod === p.label ? C.brandBg : C.surface, color: activePeriod === p.label ? C.brand : C.text, fontWeight: activePeriod === p.label ? 700 : 500, fontSize: 13, cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap" }}>
                  {p.label}
                </button>
              ))}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 20 }}>
            <button onClick={onClose} style={{ flex: 1, padding: "9px", borderRadius: 6, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>{T.cancel || "Cancel"}</button>
            <button onClick={handleApply} style={{ flex: 1, padding: "9px", borderRadius: 6, border: "none", background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>{T.apply || "Apply"}</button>
          </div>
        </div>
      </div>
    </div>
  );
};

// ─── ADD USERS MODAL — "Invite by Email" + "Add Directly" tabs ──────────────
const AddUsersModal = ({ onClose, token, onEmployeeAdded }) => {
  const { T } = useLanguage();
  const [mode, setMode]               = useState("email");   // "email" | "direct"
  const [inviteEmail, setInviteEmail] = useState("");
  // Official Start Date — mandatory for both invite paths so the worker
  // gets the correct leave-eligibility countdown from day one. Defaults to
  // TODAY in IST (the calendar most workspaces operate on) so admins don't
  // have to type it for the common case of "starts today"; they can still
  // override it from the date picker for back-dated joins.
  const todayIST = () => new Date().toLocaleDateString("en-CA", { timeZone: getOrgTimeZone() });
  const [inviteStartDate, setInviteStartDate] = useState(todayIST);
  const [inviteSent, setInviteSent]   = useState(false);
  const [direct, setDirect]           = useState(() => ({ name: "", email: "", password: "", confirm: "", role: "employee", start_date: todayIST() }));
  const [showPw, setShowPw]           = useState(false);
  const [showCfm, setShowCfm]         = useState(false);
  const [created, setCreated]         = useState(null);      // { email, password } shown after success
  const [copied, setCopied]           = useState(false);
  const [loading, setLoading]         = useState(false);
  const [toast, setToast]             = useState(null);

  const showToast = (msg, type = "success") => { setToast({ msg, type }); setTimeout(() => setToast(null), 3000); };

  const inp = { width: "100%", border: `1px solid ${C.border}`, borderRadius: C.rSm, padding: "9px 12px", fontSize: 13, fontFamily: "inherit", color: C.text, outline: "none", boxSizing: "border-box", background: C.surface };
  const lbl = { fontSize: 12, fontWeight: 600, color: C.textSub, display: "block", marginBottom: 5 };

  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  const pwRules = [
    { re: /.{8,}/,  label: T.pwRuleMin8  || "At least 8 characters" },
    { re: /[A-Z]/,  label: T.pwRuleUpper || "One uppercase letter" },
    { re: /[a-z]/,  label: T.pwRuleLower || "One lowercase letter" },
    { re: /[0-9]/,  label: T.pwRuleDigit || "One number" },
  ];
  const directRulesMet = pwRules.every(r => r.re.test(direct.password));
  const directMatch    = direct.password.length > 0 && direct.password === direct.confirm;
  const directCanSubmit = direct.name.trim().length >= 2
    && emailRegex.test(direct.email)
    && directRulesMet
    && directMatch
    && !!direct.start_date;

  const handleSendInvite = async () => {
    if (!inviteEmail) { showToast(T.adminDashboardEnterAnEmail || "Enter an email", "error"); return; }
    if (!emailRegex.test(inviteEmail)) { showToast(T.adminDashboardValidEmail || "Please enter a valid email address", "error"); return; }
    if (!inviteStartDate) { showToast(T.adminDashboardStartDateRequired || "Official Start Date is required — drives leave eligibility", "error"); return; }
    setLoading(true);
    try {
      const tok = token || readToken();
      if (!tok) { showToast(T.adminDashboardSessionExpired || "Session expired — please log in again", "error"); setLoading(false); return; }
      const res = await authFetch(`${API_BASE}/employees/invite`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${tok}` },
        body: JSON.stringify({ email: inviteEmail, start_date: inviteStartDate }),
      });
      const data = await res.json();
      if (res.ok) {
        setInviteSent(true);
        showToast(T.adminDashboardInviteSentSuccess || "Invite sent successfully!");
        if (onEmployeeAdded) onEmployeeAdded();
      } else {
        showToast(data.detail || data.message || (T.adminDashboardFailedToSendInvite || "Failed to send invite"), "error");
      }
    } catch (err) {
      console.error("Invite error:", err);
      showToast(T.adminDashboardNetworkErrorServer || "Network error — could not reach the server", "error");
    } finally {
      setLoading(false);
    }
  };

  const handleAddDirect = async () => {
    if (!directCanSubmit) { showToast(T.adminDashboardCompleteAllFields || "Please complete all fields correctly", "error"); return; }
    setLoading(true);
    try {
      const tok = token || readToken();
      if (!tok) { showToast(T.adminDashboardSessionExpired || "Session expired — please log in again", "error"); setLoading(false); return; }
      const res = await authFetch(`${API_BASE}/employees/add-direct`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${tok}` },
        body: JSON.stringify({
          name:       direct.name.trim(),
          email:      direct.email.trim().toLowerCase(),
          password:   direct.password,
          role:       direct.role,
          start_date: direct.start_date,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        setCreated({ email: data.email, password: direct.password });
        showToast(T.adminDashboardAccountCreated || "Account created!");
        if (onEmployeeAdded) onEmployeeAdded();
      } else {
        showToast(data.detail || data.message || (T.adminDashboardFailedToCreateEmployee || "Failed to create employee"), "error");
      }
    } catch (err) {
      console.error("Add direct error:", err);
      showToast(T.adminDashboardNetworkErrorServer || "Network error — could not reach the server", "error");
    } finally {
      setLoading(false);
    }
  };

  const copyCredentials = async () => {
    if (!created) return;
    const text = `Email: ${created.email}\nPassword: ${created.password}`;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      showToast(T.adminDashboardCouldNotCopy || "Could not copy to clipboard", "error");
    }
  };

  // Mode card — pill at top of the body
  const ModeCard = ({ active, onClick, icon, title }) => (
    <button onClick={onClick}
      style={{
        flex: 1, display: "flex", alignItems: "center", gap: 10, padding: "12px 14px",
        background: active ? C.brandBg : C.surface,
        border: `1.5px solid ${active ? C.brand : C.border}`,
        borderRadius: 10, cursor: "pointer", fontFamily: "inherit", textAlign: "left",
        transition: "all .15s",
      }}>
      <div style={{ width: 32, height: 32, borderRadius: "50%", background: active ? C.brand : C.bg, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
        <Icon n={icon} s={15} c={active ? "#fff" : C.textSub} />
      </div>
      <p style={{ fontSize: 13, fontWeight: 700, color: active ? C.brand : C.text }}>{title}</p>
    </button>
  );

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(16,24,40,0.55)", zIndex: 3000, display: "flex", alignItems: "center", justifyContent: "center" }} onClick={onClose}>
      <div style={{ width: 500, maxWidth: "96vw", maxHeight: "94vh", overflowY: "auto", background: C.surface, borderRadius: 16, boxShadow: "0 24px 80px rgba(0,0,0,.25)", position: "relative" }} onClick={e => e.stopPropagation()}>

        {toast && (
          // Fixed + centered near the top of the viewport so the message is
          // always visible over the modal, even when the (scrollable) form is
          // scrolled down. Previously this was absolutely positioned to the top
          // of the scroll container, so on a duplicate-email error it was pinned
          // out of view and looked like nothing happened.
          <div style={{ position: "fixed", top: 24, left: "50%", transform: "translateX(-50%)", maxWidth: "92vw", textAlign: "center", background: toast.type === "error" ? C.redBg : C.greenBg, border: `1px solid ${toast.type === "error" ? C.redBdr : C.greenBdr}`, color: toast.type === "error" ? C.red : C.green, padding: "10px 18px", borderRadius: 8, fontSize: 13, fontWeight: 600, zIndex: 3100, boxShadow: "0 8px 24px rgba(0,0,0,.18)" }}>
            {toast.msg}
          </div>
        )}

        <div style={{ padding: "16px 22px", borderBottom: `1px solid ${C.border}`, display: "flex", justifyContent: "space-between", alignItems: "center", position: "sticky", top: 0, background: C.surface, zIndex: 5 }}>
          <p style={{ fontWeight: 800, fontSize: 16, color: C.text }}>{T.inviteUser || "Invite New Member"}</p>
          <button onClick={onClose} style={{ background: C.bg, border: "none", cursor: "pointer", borderRadius: 8, padding: 6 }}><Icon n="x" s={16} c={C.textSub} /></button>
        </div>

        <div style={{ padding: "24px 28px 28px" }}>

          {/* ── Email-invite success screen ─────────────────────────────── */}
          {inviteSent ? (
            <div style={{ textAlign: "center", padding: "20px 0" }}>
              <div style={{ width: 56, height: 56, borderRadius: "50%", background: C.greenBg, border: `2px solid ${C.greenBdr}`, display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}>
                <Icon n="check" s={24} c={C.green} />
              </div>
              <p style={{ fontWeight: 700, color: C.text, marginBottom: 6, fontSize: 16 }}>{T.adminDashboardInviteSentHeading || "Invite Sent!"}</p>
              <p style={{ fontSize: 13, color: C.textSub, marginBottom: 20 }}>{T.adminDashboardInvitationSentTo || "An invitation has been sent to"} <strong>{inviteEmail}</strong></p>
              <button onClick={onClose} style={{ padding: "10px 28px", borderRadius: C.rSm, border: "none", background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>{T.done || "Done"}</button>
            </div>

          /* ── Direct-add success: show credentials ─────────────────────── */
          ) : created ? (
            <div style={{ padding: "8px 0" }}>
              <div style={{ width: 56, height: 56, borderRadius: "50%", background: C.greenBg, border: `2px solid ${C.greenBdr}`, display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 14px" }}>
                <Icon n="check" s={24} c={C.green} />
              </div>
              <p style={{ fontWeight: 800, color: C.text, fontSize: 16, textAlign: "center", marginBottom: 6 }}>{T.credentialsReady || "Credentials Ready"}</p>
              <p style={{ fontSize: 12, color: C.textSub, textAlign: "center", marginBottom: 18 }}>{T.credentialsReadyDesc || "Share these with the employee. They can sign in directly in the desktop app."}</p>

              <div style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: 10, padding: "14px 16px", marginBottom: 14, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}>
                <p style={{ fontSize: 11, color: C.textSub, fontWeight: 700, letterSpacing: ".4px", textTransform: "uppercase", marginBottom: 4 }}>{T.adminDashboardEmailLabel || "Email"}</p>
                <p style={{ fontSize: 14, color: C.text, fontWeight: 600, marginBottom: 12, wordBreak: "break-all" }}>{created.email}</p>
                <p style={{ fontSize: 11, color: C.textSub, fontWeight: 700, letterSpacing: ".4px", textTransform: "uppercase", marginBottom: 4 }}>{T.adminDashboardPasswordLabel || "Password"}</p>
                <p style={{ fontSize: 14, color: C.text, fontWeight: 600, wordBreak: "break-all" }}>{created.password}</p>
              </div>

              <div style={{ display: "flex", gap: 10 }}>
                <button onClick={copyCredentials}
                  style={{ flex: 1, padding: "10px", borderRadius: C.rSm, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit", display: "flex", alignItems: "center", justifyContent: "center", gap: 8 }}>
                  <Icon n="copy" s={14} c={C.text} />
                  {copied ? (T.credentialsCopied || "Copied!") : (T.copyCredentials || "Copy Credentials")}
                </button>
                <button onClick={onClose}
                  style={{ flex: 1, padding: "10px", borderRadius: C.rSm, border: "none", background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>
                  {T.done || "Done"}
                </button>
              </div>
            </div>

          /* ── Form: pick a mode then fill it in ────────────────────────── */
          ) : (
            <>
              <div style={{ display: "flex", gap: 10, marginBottom: 22 }}>
                <ModeCard active={mode === "email"}  onClick={() => setMode("email")}  icon="mail"      title={T.inviteByEmail || "Invite by Email"} />
                <ModeCard active={mode === "direct"} onClick={() => setMode("direct")} icon="user_plus" title={T.addDirectly   || "Add Directly"} />
              </div>

              {mode === "email" ? (
                <>
                  <p style={{ fontSize: 12, color: C.textSub, marginBottom: 14 }}>{T.inviteByEmailDesc || "Send an invitation link to the employee's email address"}</p>
                  <label style={lbl}>{T.employeeEmailAddress || "Employee Email Address"} <span style={{ color: C.red }}>*</span></label>
                  <input
                    type="email"
                    value={inviteEmail}
                    onChange={e => setInviteEmail(e.target.value)}
                    onKeyDown={e => e.key === "Enter" && handleSendInvite()}
                    placeholder="employee@company.com"
                    style={{ ...inp, marginBottom: 14 }}
                  />

                  {/* Official Start Date — mandatory. Drives leave eligibility
                      on the worker's first paid-leave application. */}
                  <label style={lbl}>{T.officialStartDate || "Official Start Date"} <span style={{ color: C.red }}>*</span></label>
                  <input
                    type="date"
                    value={inviteStartDate}
                    onChange={e => setInviteStartDate(e.target.value)}
                    style={{ ...inp, marginBottom: 6 }}
                  />
                  <p style={{ fontSize: 11, color: C.textLight, marginBottom: 16 }}>
                    {T.startDateHint || "Used to calculate when this worker becomes eligible for paid leave."}
                  </p>
                  <div style={{ display: "flex", gap: 10 }}>
                    <button onClick={onClose} style={{ flex: 1, padding: "10px", borderRadius: C.rSm, border: `1px solid ${C.border}`, background: C.surface, color: C.textSub, fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>{T.cancel || "Cancel"}</button>
                    <button onClick={handleSendInvite} disabled={loading}
                      style={{ flex: 2, padding: "10px", borderRadius: C.rSm, border: "none", background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13, cursor: loading ? "default" : "pointer", fontFamily: "inherit", display: "flex", alignItems: "center", justifyContent: "center", gap: 8, opacity: loading ? 0.8 : 1 }}>
                      <Icon n="mail" s={14} c="#fff" />
                      {loading ? (T.sending || "Sending...") : (T.sendInvite || "Send Invite")}
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <p style={{ fontSize: 12, color: C.textSub, marginBottom: 14 }}>{T.addDirectlyDesc || "Create the account with a password and share the credentials yourself"}</p>

                  <label style={lbl}>{T.fullName || "Full Name"} <span style={{ color: C.red }}>*</span></label>
                  <input value={direct.name} onChange={e => setDirect({ ...direct, name: e.target.value })}
                    placeholder={T.adminDashboardJaneDoePlaceholder || "Jane Doe"} style={{ ...inp, marginBottom: 14 }} />

                  <label style={lbl}>{T.employeeEmailAddress || "Employee Email Address"} <span style={{ color: C.red }}>*</span></label>
                  <input type="email" value={direct.email} onChange={e => setDirect({ ...direct, email: e.target.value })}
                    placeholder="employee@company.com" style={{ ...inp, marginBottom: 14 }} />

                  <label style={lbl}>{T.setPasswordLabel || "Set Password"} <span style={{ color: C.red }}>*</span></label>
                  <div style={{ position: "relative", marginBottom: directRulesMet ? 14 : 8 }}>
                    <input type={showPw ? "text" : "password"} value={direct.password}
                      onChange={e => setDirect({ ...direct, password: e.target.value })}
                      placeholder={T.adminDashboardStrongPasswordPlaceholder || "Strong password"} autoComplete="new-password"
                      style={{ ...inp, paddingRight: 56 }} />
                    <button type="button" onClick={() => setShowPw(v => !v)}
                      style={{ position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)", border: "none", background: "transparent", color: C.brand, fontSize: 11, fontWeight: 700, cursor: "pointer", fontFamily: "inherit", padding: "4px 8px" }}>
                      {showPw ? (T.hidePw || "Hide") : (T.showPw || "Show")}
                    </button>
                  </div>
                  {direct.password.length > 0 && !directRulesMet && (
                    <div style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: 8, padding: "8px 12px", marginBottom: 14 }}>
                      {pwRules.map(r => {
                        const ok = r.re.test(direct.password);
                        return (
                          <div key={r.label} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: ok ? C.green : C.textLight, lineHeight: 1.7 }}>
                            <span style={{ width: 6, height: 6, borderRadius: "50%", background: ok ? C.green : C.border, flexShrink: 0 }} />
                            {r.label}
                          </div>
                        );
                      })}
                    </div>
                  )}

                  <label style={lbl}>{T.confirmPasswordLabel || "Confirm Password"} <span style={{ color: C.red }}>*</span></label>
                  <div style={{ position: "relative", marginBottom: 6 }}>
                    <input type={showCfm ? "text" : "password"} value={direct.confirm}
                      onChange={e => setDirect({ ...direct, confirm: e.target.value })}
                      placeholder={T.adminDashboardReenterPasswordPlaceholder || "Re-enter password"} autoComplete="new-password"
                      style={{
                        ...inp, paddingRight: 56,
                        borderColor: direct.confirm.length === 0 ? C.border : (directMatch ? C.green : C.red),
                      }} />
                    <button type="button" onClick={() => setShowCfm(v => !v)}
                      style={{ position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)", border: "none", background: "transparent", color: C.brand, fontSize: 11, fontWeight: 700, cursor: "pointer", fontFamily: "inherit", padding: "4px 8px" }}>
                      {showCfm ? (T.hidePw || "Hide") : (T.showPw || "Show")}
                    </button>
                  </div>
                  {direct.confirm.length > 0 && (
                    <p style={{ fontSize: 11, color: directMatch ? C.green : C.red, marginBottom: 14 }}>
                      {directMatch ? `✓ ${T.passwordsMatch || "Passwords match"}` : (T.passwordsDoNotMatch || "Passwords do not match")}
                    </p>
                  )}
                  {direct.confirm.length === 0 && <div style={{ marginBottom: 14 }} />}

                  <label style={lbl}>{T.roleLabel || "Role"}</label>
                  <select value={direct.role} onChange={e => setDirect({ ...direct, role: e.target.value })}
                    style={{ ...inp, marginBottom: 14, cursor: "pointer" }}>
                    <option value="employee">{T.employee || "Employee"}</option>
                    <option value="manager">{T.manager   || "Manager"}</option>
                    <option value="hr">{T.adminDashboardHrRole || "HR"}</option>
                    <option value="admin">{T.adminLabel || "Admin"}</option>
                  </select>

                  {/* Official Start Date — mandatory. Drives leave eligibility. */}
                  <label style={lbl}>{T.officialStartDate || "Official Start Date"} <span style={{ color: C.red }}>*</span></label>
                  <input
                    type="date"
                    value={direct.start_date}
                    onChange={e => setDirect({ ...direct, start_date: e.target.value })}
                    style={{ ...inp, marginBottom: 6 }}
                  />
                  <p style={{ fontSize: 11, color: C.textLight, marginBottom: 16 }}>
                    {T.startDateHint || "Used to calculate when this worker becomes eligible for paid leave."}
                  </p>

                  <div style={{ display: "flex", gap: 10 }}>
                    <button onClick={onClose} style={{ flex: 1, padding: "10px", borderRadius: C.rSm, border: `1px solid ${C.border}`, background: C.surface, color: C.textSub, fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>{T.cancel || "Cancel"}</button>
                    <button onClick={handleAddDirect} disabled={loading || !directCanSubmit}
                      style={{ flex: 2, padding: "10px", borderRadius: C.rSm, border: "none", background: directCanSubmit ? C.brand : C.border, color: "#fff", fontWeight: 700, fontSize: 13, cursor: (loading || !directCanSubmit) ? "default" : "pointer", fontFamily: "inherit", display: "flex", alignItems: "center", justifyContent: "center", gap: 8, opacity: loading ? 0.8 : 1 }}>
                      <Icon n="user_plus" s={14} c="#fff" />
                      {loading ? (T.creating || "Creating…") : (T.createAccount || "Create Account")}
                    </button>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
};

// ─── EDIT EMPLOYEE MODAL ──────────────────────────────────────────────────────
const EditEmpModal = ({ emp, onClose, token, onSaved }) => {
  const { T } = useLanguage();
  const [form, setForm] = useState({
    name: emp.name || "", email: emp.email || "", designation: emp.designation || "",
    phone: emp.phone || "", team: emp.team || "", department: emp.department || "",
    employment_type: emp.employment_type || "full_time", role: emp.role || "employee",
    employee_id: emp.employee_id || "",
    // Official Start Date — pre-populated from the existing row so admins
    // can either confirm or correct it. Stored as YYYY-MM-DD (server already
    // sends midnight-UTC ISO; we trim to the date part).
    start_date: emp.official_start_date ? String(emp.official_start_date).slice(0, 10) : "",
  });

  // Helpers to convert UTC ISO → IST for display in datetime-local inputs
  const toISTDate = (utcIso) => {
    if (!utcIso) return new Date().toLocaleDateString("en-CA", { timeZone: getOrgTimeZone() });
    const d = new Date(String(utcIso).endsWith("Z") ? utcIso : utcIso + "Z");
    return d.toLocaleDateString("en-CA", { timeZone: getOrgTimeZone() });
  };
  const toISTTime = (utcIso) => {
    if (!utcIso) return "";
    const d = new Date(String(utcIso).endsWith("Z") ? utcIso : utcIso + "Z");
    return d.toLocaleTimeString("en-GB", { timeZone: getOrgTimeZone(), hour: "2-digit", minute: "2-digit", hour12: false });
  };
  const todayIST = new Date().toLocaleDateString("en-CA", { timeZone: getOrgTimeZone() });

  const [clockOpen, setClockOpen] = useState(false);
  const [clockDate, setClockDate] = useState(todayIST);
  // Clock-In initial value priority:
  //   1. Admin's per-employee override (default_clock_in_time) — what they
  //      already configured.
  //   2. System default 10:30 IST — applied to every worker without an
  //      override (matches the backend's require-action fallback).
  // We deliberately don't fall back to today's actual clock-in time here —
  // that conflates "what they're expected to clock in at" with "what they
  // already did today" and led to admins accidentally setting a worker's
  // permanent schedule to whatever time they happened to clock in today.
  // No client-side default shift. "10:30" was one customer's start time and
  // 20 minutes one customer's grace; both are resolved server-side through
  // the org > team > employee cascade. An empty field here means "inherit
  // the policy", which is what leaving it blank actually does.
  const [clockIn, setClockIn]     = useState(emp.default_clock_in_time || "");
  const [clockOut, setClockOut]   = useState(toISTTime(emp.clock_out_time));
  // "Make permanent" — saves Clock-In Time as the worker's recurring expected
  // start. The "Require Actions" admin view derives late employees from this.
  const [permanent, setPermanent]       = useState(!!emp.default_clock_in_time);
  const [graceMin, setGraceMin]         = useState(emp.default_clock_in_grace_min ?? "");

  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState(null);
  // After clock time is saved, show a confirmation banner instead of inputs
  const [clockSaved, setClockSaved] = useState(false);
  const [savedClockInfo, setSavedClockInfo] = useState(null);

  const showToast = (msg, type = "success", duration = 3000) => { setToast({ msg, type }); setTimeout(() => setToast(null), duration); };

  // Convert "HH:MM" 24-hour → "h:MM AM/PM" for readable success messages
  const fmt12h = (hhmm) => {
    if (!hhmm) return "";
    const [h, m] = String(hhmm).split(":").map(Number);
    if (Number.isNaN(h)) return hhmm;
    const period = h >= 12 ? "PM" : "AM";
    const hr12 = ((h + 11) % 12) + 1;
    return `${hr12}:${String(m || 0).padStart(2, "0")} ${period}`;
  };

  const inp = { width: "100%", border: `1px solid ${C.border}`, borderRadius: C.rSm, padding: "9px 12px", fontSize: 13, fontFamily: "inherit", color: C.text, outline: "none", boxSizing: "border-box", background: C.surface };
  const lbl = { fontSize: 12, fontWeight: 600, color: C.textSub, display: "block", marginBottom: 5 };

  const handleSave = async () => {
    const tok = token || readToken();
    if (!tok) { showToast(T.adminDashboardNotLoggedIn || "Not logged in", "error"); return; }

    // Guard: if the admin opened the Clock Time Override section and showed
    // intent (toggled Permanent, picked a clock-out, or moved the date off
    // today) but never filled in Clock-In, the save would silently skip the
    // clock block — leaving them confused about why no green message appears.
    // Block the save and tell them what's missing.
    const clockIntent = clockOpen && !clockIn && (permanent || clockOut || clockDate !== todayIST);
    if (clockIntent) {
      showToast(T.adminDashboardEnterClockInOverride || "Please enter a Clock-In time (HH:MM) to set the override", "error", 4000);
      return;
    }

    setLoading(true);
    try {
      // 1. Save profile fields
      const res = await authFetch(`${API_BASE}/employees/${emp.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${tok}` },
        body: JSON.stringify(form),
      });
      if (!res.ok) { const d = await res.json(); showToast(d.detail || (T.adminDashboardUpdateFailed || "Update failed"), "error"); return; }

      // 2. Save clock override if admin filled in the clock section.
      //    "Make permanent" branches: when toggled on we set the worker's
      //    recurring default_clock_in_time (used by /require-action), when
      //    off we just create / update the one-off session for that date.
      if (clockOpen && clockIn) {
        if (permanent) {
          const pr = await authFetch(`${API_BASE}/employees/${emp.id}/default-clock-in`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json", "Authorization": `Bearer ${tok}` },
            body: JSON.stringify({
              default_clock_in_time:      clockIn,
              default_clock_in_permanent: true,
              default_clock_in_grace_min: graceMin,
            }),
          });
          if (!pr.ok) { const d = await pr.json(); showToast(d.detail || (T.adminDashboardPermanentTimeSaveFailed || "Permanent time save failed"), "error"); return; }
        } else if (clockDate) {
          const cr = await authFetch(`${API_BASE}/employees/${emp.id}/admin-clock`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": `Bearer ${tok}` },
            body: JSON.stringify({
              date: clockDate,
              clock_in_time: clockIn || null,
              clock_out_time: clockOut || null,
            }),
          });
          if (!cr.ok) { const d = await cr.json(); showToast(d.detail || (T.adminDashboardClockUpdateFailed || "Clock update failed"), "error"); return; }
        }
        // Mark clock as saved so we show the confirmation banner
        setClockSaved(true);
        setSavedClockInfo({ clockIn, clockOut, clockDate, permanent, graceMin });
        // Propagate the new clock fields to the parent list so reopening the
        // modal shows the saved time instead of --:--. For permanent saves the
        // backend stores `default_clock_in_time`; for one-off overrides it
        // updates today's session (`clock_in_time` / `clock_out_time`).
        const clockPatch = permanent
          ? {
              default_clock_in_time:      clockIn,
              default_clock_in_grace_min: graceMin,
            }
          : (clockDate === todayIST ? {
              // Only mirror to clock_in_time when overriding TODAY's session —
              // for any other date the today field shouldn't change.
              clock_in_time:  clockIn  ? `${clockDate}T${clockIn}:00` : emp.clock_in_time,
              clock_out_time: clockOut ? `${clockDate}T${clockOut}:00` : emp.clock_out_time,
            } : {});
        onSaved && onSaved({ ...emp, ...form, ...clockPatch });
        // Specific, persistent green toast naming the worker + exact time.
        // We don't auto-close the modal here — the inline green banner stays
        // visible so the admin can clearly read what was set and dismiss it
        // manually (Cancel) or hit "Click here to change" to edit again.
        const friendlyTime = fmt12h(clockIn);
        const toastMsg = permanent
          ? `${emp.name}'s clock-in time has been set to ${friendlyTime} (recurring daily)`
          : `${emp.name}'s clock-in time has been set to ${friendlyTime} on ${clockDate}`;
        showToast(toastMsg, "success", 6000);
        return;
      }

      showToast(T.adminDashboardEmployeeUpdated || "Employee updated!");
      onSaved && onSaved({ ...emp, ...form });
      setTimeout(() => onClose(), 1200);
    } catch { showToast(T.adminDashboardServerError || "Server error", "error"); }
    finally { setLoading(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(16,24,40,0.55)", zIndex: 3000, display: "flex", alignItems: "center", justifyContent: "center" }} onClick={onClose}>
      <div style={{ width: 560, maxWidth: "96vw", maxHeight: "90vh", background: C.surface, borderRadius: 16, overflow: "hidden", boxShadow: "0 24px 80px rgba(0,0,0,.25)", position: "relative", display: "flex", flexDirection: "column" }} onClick={e => e.stopPropagation()}>
        {toast && (
          <div style={{
            position: "absolute", top: 14, left: "50%", transform: "translateX(-50%)",
            background: toast.type === "error" ? C.redBg : C.greenBg,
            border: `1.5px solid ${toast.type === "error" ? C.redBdr : C.greenBdr}`,
            color: toast.type === "error" ? C.red : C.green,
            padding: "10px 18px", borderRadius: 10, fontSize: 13, fontWeight: 700,
            zIndex: 20, display: "flex", alignItems: "center", gap: 8,
            boxShadow: "0 6px 18px rgba(16,24,40,0.18)", maxWidth: "92%",
          }}>
            <Icon n={toast.type === "error" ? "x" : "check"} s={16} c={toast.type === "error" ? C.red : C.green} />
            <span>{toast.msg}</span>
          </div>
        )}
        <div style={{ padding: "16px 22px", borderBottom: `1px solid ${C.border}`, display: "flex", justifyContent: "space-between", alignItems: "center", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, flexShrink: 0 }}>
          <p style={{ fontWeight: 800, fontSize: 16, color: "#fff" }}>{T.edit||"Edit"} {T.employee||"Employee"}</p>
          <button onClick={onClose} style={{ background: "rgba(255,255,255,0.2)", border: "none", cursor: "pointer", borderRadius: 8, padding: 6 }}><Icon n="x" s={16} c="#fff" /></button>
        </div>
        <div style={{ padding: "22px 24px 20px", overflowY: "auto", flex: 1, minHeight: 0 }}>
          {/* ── Employee ID (read-only) ── */}
          {/* Derived from User.id — no separate employee_id column in the DB
              today. Format: EMP-#####. If a human-editable ID is needed
              later, add a User.employee_id column and a migration. */}
          <div style={{
            display: "flex", alignItems: "center", gap: 10,
            padding: "10px 14px", marginBottom: 14, borderRadius: 8,
            background: C.bg, border: `1px solid ${C.border}`,
          }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: C.textLight,
                           letterSpacing: 0.6, textTransform: "uppercase" }}>
              {T.employeeId || "Employee ID"}
            </span>
            <span style={{ fontFamily: "Consolas,'Courier New',monospace",
                           fontSize: 13, fontWeight: 700, color: C.text,
                           padding: "2px 10px", borderRadius: 6,
                           background: C.surface, border: `1px solid ${C.border}` }}>
              EMP-{String(emp.id).padStart(5, "0")}
            </span>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: 14 }}>
            <div><label style={lbl}>{T.name||"Name"}</label><input name="name" value={form.name} onChange={e => setForm(p => ({ ...p, name: e.target.value }))} style={inp} /></div>
            <div><label style={lbl}>{T.email||"Email"}</label><input name="email" type="email" value={form.email} onChange={e => setForm(p => ({ ...p, email: e.target.value }))} style={inp} /></div>
            <div><label style={lbl}>{T.designation || "Designation"}</label><input value={form.designation} onChange={e => setForm(p => ({ ...p, designation: e.target.value }))} style={inp} /></div>
            <div><label style={lbl}>{T.phone||"Phone"}</label><input
              value={form.phone}
              onChange={e => {
                // Allow only digits, spaces, hyphens, parentheses and a leading "+"
                // for international prefixes. Anything else is silently dropped.
                const cleaned = e.target.value.replace(/[^\d+\-\s()]/g, "");
                setForm(p => ({ ...p, phone: cleaned }));
              }}
              type="tel"
              inputMode="tel"
              autoComplete="tel"
              pattern="[+0-9\-\s()]*"
              placeholder="+1 555 123 4567"
              style={inp} /></div>
            <div><label style={lbl}>{T.team_label||"Team"}</label><input value={form.team} onChange={e => setForm(p => ({ ...p, team: e.target.value }))} style={inp} /></div>
            <div><label style={lbl}>{T.department||"Department"}</label><input value={form.department} onChange={e => setForm(p => ({ ...p, department: e.target.value }))} style={inp} /></div>
            <div>
              <label style={lbl}>{T.role||"Role"}</label>
              <select value={form.role} onChange={e => setForm(p => ({ ...p, role: e.target.value }))} style={{ ...inp }}>
                <option value="employee">{T.teamMember || "Team Member"}</option>
                <option value="manager">{T.teamHead || "Team Head"}</option>
                <option value="admin">{T.adminLabel || "Admin"}</option>
              </select>
            </div>
            <div>
              <label style={lbl}>{T.employmentType||"Employment Type"}</label>
              <select value={form.employment_type} onChange={e => setForm(p => ({ ...p, employment_type: e.target.value }))} style={{ ...inp }}>
                <option value="full_time">{T.fullTime||"Full Time"}</option>
                <option value="part_time">{T.partTime||"Part Time"}</option>
                <option value="temporary">{T.temporary || "Temporary"}</option>
                <option value="casual">{T.casual || "Casual"}</option>
              </select>
            </div>
          </div>
          {/* ── Employee ID ── */}
          <div style={{ marginBottom: 14 }}>
            <label style={lbl}>{T.employeeId || "Employee ID"}</label>
            <input
              value={form.employee_id}
              onChange={e => setForm(p => ({ ...p, employee_id: e.target.value }))}
              placeholder="e.g. EMP001, ZK-042"
              style={{ ...inp, width: "100%" }}
            />
          </div>
          {/* ── Official Start Date ── (drives paid-leave eligibility) ── */}
          <div style={{ marginBottom: 14 }}>
            <label style={lbl}>{T.officialStartDate || "Official Start Date"}</label>
            <input
              type="date"
              value={form.start_date}
              onChange={e => setForm(p => ({ ...p, start_date: e.target.value }))}
              style={{ ...inp, width: "100%" }}
            />
            <p style={{ fontSize: 11, color: C.textLight, marginTop: 4 }}>
              {T.startDateHint || "Used to calculate when this worker becomes eligible for paid leave."}
            </p>
          </div>
          {/* ── Clock Override Section — admin-only ──
              Clock-in time is admin-governed (HR/managers see the schedule
              but cannot edit). We hide the entire section for non-admin
              users so HR doesn't fill it in only to hit a 403 on save. */}
          {((localStorage.getItem("role") || "admin").toLowerCase() === "admin") && (
          <div style={{ border: `1px solid ${C.border}`, borderRadius: 10, overflow: "hidden", marginBottom: 14 }}>
            <button
              onClick={() => setClockOpen(o => !o)}
              style={{ width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between", padding: "11px 14px", background: clockOpen ? C.brandBg : C.bg, border: "none", cursor: "pointer", fontFamily: "inherit" }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Icon n="clock" s={15} c={clockOpen ? C.brand : C.textSub} />
                <span style={{ fontSize: 13, fontWeight: 700, color: clockOpen ? C.brand : C.text }}>{T.adminDashboardClockTimeOverride || "Clock Time Override"}</span>
                <span style={{ fontSize: 10, color: C.textSub, fontWeight: 500, background: C.surface, border: `1px solid ${C.border}`, padding: "2px 7px", borderRadius: 99 }}>{T.adminDashboardAdminBadge || "Admin"}</span>
              </div>
              <span style={{ display: "inline-flex", transform: clockOpen ? "rotate(180deg)" : "none", transition: "transform .2s" }}>
                <Icon n="chevron_down" s={14} c={C.textSub} />
              </span>
            </button>

            {clockOpen && (
              <div style={{ padding: "14px 16px", borderTop: `1px solid ${C.border}`, background: C.surface }}>
                {clockSaved && savedClockInfo ? (
                  /* ── Confirmation banner after clock time is saved ── */
                  <div style={{ padding: "14px 16px", background: C.greenBg, border: `1px solid ${C.greenBdr}`, borderRadius: 8 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                      <Icon n="check" s={16} c={C.green} />
                      <span style={{ fontSize: 13, fontWeight: 700, color: C.green }}>{T.adminDashboardClockingTimeSet || "Clocking time has been set!"}</span>
                    </div>
                    <p style={{ fontSize: 12, color: C.text, margin: "0 0 4px", lineHeight: 1.6 }}>
                      <strong>{emp.name}</strong>'s clock-in is set to <strong>{fmt12h(savedClockInfo.clockIn)}</strong> IST
                      {savedClockInfo.permanent
                        ? <> (recurring daily, grace: {savedClockInfo.graceMin} min)</>
                        : <> on <strong>{savedClockInfo.clockDate}</strong></>}
                      {savedClockInfo.clockOut && <>, clock-out: <strong>{fmt12h(savedClockInfo.clockOut)}</strong> IST</>}.
                    </p>
                    <button
                      onClick={() => { setClockSaved(false); setSavedClockInfo(null); }}
                      style={{ marginTop: 6, background: "none", border: "none", color: C.brand, cursor: "pointer", fontWeight: 700, fontSize: 12, fontFamily: "inherit", padding: 0, textDecoration: "underline" }}
                    >
                      {T.adminDashboardClickToChange || "Click here to change →"}
                    </button>
                  </div>
                ) : (
                  /* ── Normal clock override form ── */
                  <>
                <p style={{ fontSize: 12, color: C.textSub, marginBottom: 14, lineHeight: 1.55 }}>
                  {T.adminDashboardClockOverrideHelp || "Set or override attendance for a specific date. A new session is created if none exists; existing sessions are updated."}
                </p>
                {/* Aligned 3-column grid — each field gets equal width, equal
                    label gap, and the same input height so the row looks
                    balanced. minmax(0,1fr) lets the grid shrink cleanly on
                    narrow screens before stacking. */}
                <div style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
                  gap: 14,
                  alignItems: "start",
                }}>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    <label style={{ ...lbl, marginBottom: 0, display: "flex", alignItems: "center", gap: 6 }}>
                      <Icon n="calendar" s={12} c={C.textSub} />
                      <span>{T.adminDashboardDateField || "Date"} <span style={{ color: C.red }}>*</span></span>
                    </label>
                    <input
                      type="date"
                      value={clockDate}
                      max={todayIST}
                      onChange={e => setClockDate(e.target.value)}
                      style={{ ...inp, height: 38, padding: "8px 12px" }}
                    />
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    <label style={{ ...lbl, marginBottom: 0, display: "flex", alignItems: "center", gap: 6 }}>
                      <Icon n="clock" s={12} c={C.textSub} />
                      <span>{T.adminDashboardClockInField || "Clock-In"} <span style={{ color: C.red }}>*</span></span>
                    </label>
                    <input
                      type="time"
                      value={clockIn}
                      onChange={e => setClockIn(e.target.value)}
                      style={{
                        ...inp, height: 38, padding: "8px 12px",
                        // Highlight in red when admin showed intent (permanent
                        // checked / clock-out set / non-today date) but left
                        // Clock-In empty — so the missing field is obvious
                        // before they click Save Changes.
                        border: (!clockIn && (permanent || clockOut || clockDate !== todayIST))
                          ? `1.5px solid ${C.red}`
                          : `1px solid ${C.border}`,
                      }}
                    />
                    {!clockIn && (permanent || clockOut || clockDate !== todayIST) && (
                      <span style={{ fontSize: 11, color: C.red, fontWeight: 600 }}>
                        {T.adminDashboardClockInRequired || "Clock-In is required"}
                      </span>
                    )}
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    <label style={{ ...lbl, marginBottom: 0, display: "flex", alignItems: "center", gap: 6 }}>
                      <Icon n="clock" s={12} c={C.textLight} />
                      <span style={{ color: C.textSub }}>{T.adminDashboardClockOutField || "Clock-Out"}</span>
                      <span style={{ color: C.textLight, fontWeight: 500 }}>{T.adminDashboardOptionalParen || "(optional)"}</span>
                    </label>
                    <input
                      type="time"
                      value={clockOut}
                      onChange={e => setClockOut(e.target.value)}
                      style={{ ...inp, height: 38, padding: "8px 12px" }}
                    />
                  </div>
                </div>
                {/* ── Make this the permanent (recurring) clock-in time ── */}
                <div
                  style={{
                    marginTop: 12,
                    padding: "10px 12px",
                    border: `1px solid ${permanent ? C.brandBdr : C.border}`,
                    background: permanent ? C.brandBg : C.bg,
                    borderRadius: 8,
                    display: "flex", alignItems: "center", gap: 12,
                  }}
                >
                  <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", flex: 1 }}>
                    <input
                      type="checkbox"
                      checked={permanent}
                      onChange={(e) => setPermanent(e.target.checked)}
                      style={{ accentColor: C.brand, width: 16, height: 16 }}
                    />
                    <div>
                      <p style={{ fontSize: 13, fontWeight: 700, color: permanent ? C.brand : C.text, margin: 0 }}>
                        {T.adminDashboardMakePermanent || "Make permanent (recurring daily)"}
                      </p>
                      <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                        {T.adminDashboardPermanentHint || "Worker is expected to clock in at this time every working day."}
                      </p>
                    </div>
                  </label>
                  {permanent && (
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ fontSize: 11, fontWeight: 600, color: C.textSub }}>{T.adminDashboardGraceLabel || "Grace"}</span>
                      <input
                        type="number"
                        min={0}
                        max={240}
                        value={graceMin}
                        onChange={(e) => setGraceMin(Math.max(0, Math.min(240, Number(e.target.value) || 0)))}
                        style={{ ...inp, width: 64, padding: "6px 8px", textAlign: "center" }}
                      />
                      <span style={{ fontSize: 11, fontWeight: 600, color: C.textSub }}>{T.adminDashboardMinUnit || "min"}</span>
                    </div>
                  )}
                </div>

                {/* ── Why this warning exists ──────────────────────────────
                    These two states save to completely different places, and
                    nothing used to say so. Permanent ON writes the worker's
                    recurring schedule (PATCH /employees/:id/default-clock-in),
                    which is the ONLY one of the two the absence cutoff reads.
                    Permanent OFF writes a single ClockSession for the chosen
                    day (POST /employees/:id/admin-clock) — a punch correction,
                    invisible to the schedule.

                    An admin who typed an afternoon start here with the box
                    unchecked believed they had set that worker's shift. They
                    had not: the worker kept resolving to "no scheduled start",
                    fell back to the org's absence cutoff hour (noon by
                    default) and was emailed about as Absent the next morning
                    and every morning after. The one-off row buys exactly one
                    day, and only if it is saved before that day's cutoff.  */}
                {!permanent && clockIn && (
                  <div style={{
                    marginTop: 10,
                    padding: "10px 12px",
                    background: C.amberBg || C.bg,
                    border: `1px solid ${C.amberBdr || C.border}`,
                    borderRadius: 8,
                    fontSize: 11,
                    lineHeight: 1.55,
                    color: C.text,
                  }}>
                    {T.adminDashboardOneOffOnlyWarning || (
                      <>
                        <strong>One-day correction only.</strong> This fixes{" "}
                        {clockDate === todayIST ? "today's" : "that day's"} record and does
                        <strong> not</strong> set {emp.name || "this worker"}'s working hours.
                        Their shift stays unset, so they can still be flagged Absent on later
                        days. To set the hours they actually work, tick “Make permanent” above,
                        or use Settings → Organization Settings → Working Schedule.
                      </>
                    )}
                  </div>
                )}

                {clockIn && (
                  <div style={{
                    marginTop: 12,
                    padding: "10px 14px",
                    background: C.greenBg,
                    borderRadius: 8,
                    border: `1px solid ${C.greenBdr}`,
                    fontSize: 12,
                    color: C.text,
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 10,
                    lineHeight: 1.5,
                  }}>
                    <Icon n="check" s={15} c={C.green} />
                    <div style={{ flex: 1 }}>
                      {permanent ? (
                        <>
                          <strong>{emp.name}</strong>'s clock-in time will be set to{" "}
                          <strong style={{ color: C.green }}>{fmt12h(clockIn)} IST</strong> every day.
                          <span style={{ display: "block", fontSize: 11, color: C.textSub, marginTop: 2 }}>
                            Late beyond {graceMin} min → flagged in Require Actions.
                          </span>
                        </>
                      ) : (
                        <>
                          <strong>{emp.name}</strong>{T.adminDashboardClockWillBeSetTo || "'s clock-in time will be set to"}{" "}
                          <strong style={{ color: C.green }}>{fmt12h(clockIn)} IST</strong> {T.adminDashboardOnWord || "on"}{" "}
                          <strong>{clockDate}</strong>
                          {clockOut && <> · {T.adminDashboardClockOutColon || "clock-out:"} <strong style={{ color: C.green }}>{fmt12h(clockOut)} IST</strong></>}.
                        </>
                      )}
                    </div>
                  </div>
                )}
                  </>
                )}
              </div>
            )}
          </div>
          )}

        </div>
        <div style={{
          padding: "14px 24px",
          borderTop: `1px solid ${C.border}`,
          background: C.surface,
          display: "flex",
          gap: 12,
          justifyContent: "flex-end",
          alignItems: "center",
          flexShrink: 0,
        }}>
          <button
            onClick={onClose}
            style={{
              height: 40, padding: "0 22px", minWidth: 110,
              borderRadius: C.rSm,
              border: `1px solid ${C.border}`,
              background: C.surface,
              color: C.textSub,
              fontWeight: 600, fontSize: 13,
              cursor: "pointer", fontFamily: "inherit",
              display: "inline-flex", alignItems: "center", justifyContent: "center",
            }}
          >
            {T.cancel || "Cancel"}
          </button>
          <button
            onClick={handleSave}
            disabled={loading}
            style={{
              height: 40, padding: "0 24px", minWidth: 140,
              borderRadius: C.rSm,
              border: "none",
              background: loading ? C.textLight : C.brand,
              color: "#fff",
              fontWeight: 700, fontSize: 13,
              cursor: loading ? "not-allowed" : "pointer",
              fontFamily: "inherit",
              display: "inline-flex", alignItems: "center", justifyContent: "center",
            }}
          >
            {loading ? (T.saving || "Saving...") : (T.saveChanges || "Save Changes")}
          </button>
        </div>
      </div>
    </div>
  );
};

// ─── DELETE CONFIRM MODAL ─────────────────────────────────────────────────────
const DeleteConfirmModal = ({ emp, onClose, token, onDeleted }) => {
  const { T } = useLanguage();
  const [loading, setLoading] = useState(false);
  const handleDelete = async () => {
    const tok = token || readToken();
    if (!tok) return;
    setLoading(true);
    try {
      const res = await authFetch(`${API_BASE}/employees/${emp.id}`, { method: "DELETE", headers: { "Authorization": `Bearer ${tok}` } });
      if (res.ok || res.status === 204) { onDeleted && onDeleted(emp.id); onClose(); }
    } catch { }
    finally { setLoading(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(16,24,40,0.55)", zIndex: 3000, display: "flex", alignItems: "center", justifyContent: "center" }} onClick={onClose}>
      <div style={{ width: 420, maxWidth: "96vw", background: C.surface, borderRadius: 16, overflow: "hidden", boxShadow: "0 24px 80px rgba(0,0,0,.25)" }} onClick={e => e.stopPropagation()}>
        <div style={{ padding: "24px 28px 20px", textAlign: "center" }}>
          <div style={{ width: 52, height: 52, borderRadius: "50%", background: C.redBg, display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}><Icon n="trash" s={22} c={C.red} /></div>
          <p style={{ fontSize: 17, fontWeight: 800, color: C.text, marginBottom: 8 }}>{T.delete||"Delete"} {T.employee||"Employee"}</p>
          <p style={{ fontSize: 13, color: C.textSub, marginBottom: 24 }}>{T.confirmDeleteEmployee || "Are you sure you want to delete"} <strong>{emp.name}</strong>?</p>
          <div style={{ display: "flex", gap: 10 }}>
            <button onClick={onClose} style={{ flex: 1, padding: "10px", borderRadius: C.rSm, border: `1px solid ${C.border}`, background: C.surface, color: C.textSub, fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>{T.cancel||"Cancel"}</button>
            <button onClick={handleDelete} disabled={loading} style={{ flex: 1, padding: "10px", borderRadius: C.rSm, border: "none", background: C.red, color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>{loading ? (T.deleting||"Deleting...") : (T.delete||"Delete")}</button>
          </div>
        </div>
      </div>
    </div>
  );
};

// ─── PROFESSIONAL PERFORMANCE ANALYTICS CARD ─────────────────────────────────
const PerformanceAnalyticsCard = React.memo(({ overviewStats, weekBars, isDark, onViewDetails, lastUpdated }) => {
  const { T } = useLanguage();
  const [liveTime, setLiveTime] = useState(new Date());

  useEffect(() => {
    // Minute-precision is enough for the header clock — ticking every second
    // forced a full re-render of this card (and its charts) 60× per minute
    // for a display that only renders hours and minutes.
    const iv = setInterval(() => setLiveTime(new Date()), 60_000);
    return () => clearInterval(iv);
  }, []);

  const maxVal = Math.max(...weekBars.map(b => b.val), 1);
  const formatTime = (d) => d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: getOrgTimeZone() });

  return (
    <div style={{
      background: isDark
        ? "linear-gradient(145deg, #141414 0%, #1F1F1F 50%, #141414 100%)"
        : "linear-gradient(145deg, #0F172A 0%, #1E1B4B 50%, #162032 100%)",
      borderRadius: 16, padding: 24, position: "relative", overflow: "hidden",
      border: "1px solid rgba(129,140,248,0.2)",
      boxShadow: "0 20px 60px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.05)",
    }}>
      {/* Background decorations */}
      <div style={{ position: "absolute", top: -60, right: -60, width: 220, height: 220, borderRadius: "50%", background: "radial-gradient(circle, rgba(124,58,237,0.15) 0%, transparent 70%)", pointerEvents: "none" }} />
      <div style={{ position: "absolute", bottom: -40, left: -40, width: 180, height: 180, borderRadius: "50%", background: "radial-gradient(circle, rgba(17,73,106,0.1) 0%, transparent 70%)", pointerEvents: "none" }} />
      <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: 1, background: "linear-gradient(90deg, transparent, rgba(129,140,248,0.4), transparent)" }} />

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ width: 36, height: 36, borderRadius: 10, background: "linear-gradient(135deg, #7C3AED, #4F46E5)", display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "0 4px 12px rgba(124,58,237,0.4)" }}>
            <Icon n="bar_chart" s={17} c="#fff" />
          </div>
          <div>
            <p style={{ fontWeight: 800, fontSize: 15, color: "#F1F5F9", letterSpacing: "-0.2px" }}>{T.performanceAnalytics || "Performance Analytics"}</p>
            <p style={{ fontSize: 11, color: "#64748B", fontWeight: 500 }}>{formatTime(liveTime)}</p>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6, background: "rgba(34,197,94,0.12)", border: "1px solid rgba(34,197,94,0.25)", borderRadius: 20, padding: "4px 12px" }}>
          <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#22C55E", display: "inline-block", boxShadow: "0 0 8px #22C55E", animation: "pulse 2s infinite" }} />
          <span style={{ fontSize: 11, color: "#22C55E", fontWeight: 700, letterSpacing: "0.5px" }}>{T.liveBadge || "LIVE"}</span>
        </div>
      </div>

      {/* Stats row */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 20 }}>
        <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, padding: "14px 16px", position: "relative", overflow: "hidden" }}>
          <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: 2, background: "linear-gradient(90deg, #22C55E, #16A34A)" }} />
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
            <span style={{ fontSize: 11, color: "#94A3B8", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.5px" }}>{T.screenshotsToday || "Screenshots Today"}</span>
            <div style={{ width: 26, height: 26, borderRadius: 6, background: "rgba(34,197,94,0.15)", display: "flex", alignItems: "center", justifyContent: "center" }}>
              <Icon n="camera" s={12} c="#22C55E" />
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 8 }}>
            <span style={{ fontSize: 28, fontWeight: 900, color: "#F1F5F9", lineHeight: 1, letterSpacing: "-1px" }}>
              {overviewStats.totalScreenshots >= 1000 ? `${(overviewStats.totalScreenshots / 1000).toFixed(1)}K` : overviewStats.totalScreenshots}
            </span>
            <span style={{ fontSize: 11, color: "#22C55E", fontWeight: 700, marginBottom: 3, display: "flex", alignItems: "center", gap: 3 }}>
              <Icon n="trending_up" s={11} c="#22C55E" /> live
            </span>
          </div>
        </div>

        <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, padding: "14px 16px", position: "relative", overflow: "hidden" }}>
          <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: 2, background: "linear-gradient(90deg, #818CF8, #6366F1)" }} />
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
            <span style={{ fontSize: 11, color: "#94A3B8", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.5px" }}>{T.activeToday || "Active Today"}</span>
            <div style={{ width: 26, height: 26, borderRadius: 6, background: "rgba(129,140,248,0.15)", display: "flex", alignItems: "center", justifyContent: "center" }}>
              <Icon n="users" s={12} c="#818CF8" />
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 8 }}>
            <span style={{ fontSize: 28, fontWeight: 900, color: "#F1F5F9", lineHeight: 1, letterSpacing: "-1px" }}>{overviewStats.activeToday}</span>
            <span style={{ fontSize: 11, color: "#818CF8", fontWeight: 600, marginBottom: 3 }}>employees</span>
          </div>
        </div>
      </div>

      {/* Bar chart */}
      {weekBars.length > 0 && (
        <div style={{ marginBottom: 18 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <span style={{ fontSize: 11, color: "#64748B", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.5px" }}>{T.sevenDayActivity || "7-Day Activity"}</span>
            <span style={{ fontSize: 10, color: "#475569" }}>{T.activityCount || "Activity count"}</span>
          </div>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 4, height: 64 }}>
            {weekBars.map((b, i) => {
              const isLast = i === weekBars.length - 1;
              const h = Math.max(6, (b.val / maxVal) * 56);
              return (
                <div key={i} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 4, height: "100%", justifyContent: "flex-end" }}>
                  <div
                    style={{
                      width: "100%", height: h,
                      borderRadius: "4px 4px 2px 2px",
                      background: isLast
                        ? "linear-gradient(180deg, #A78BFA, #7C3AED)"
                        : "rgba(129,140,248,0.3)",
                      border: isLast ? "1px solid rgba(167,139,250,0.5)" : "1px solid rgba(129,140,248,0.15)",
                      boxShadow: isLast ? "0 0 12px rgba(124,58,237,0.4)" : "none",
                      transition: "height 0.3s ease",
                      cursor: "default",
                    }}
                    title={`${b.label}: ${b.val}`}
                  />
                  <span style={{ fontSize: 9, color: isLast ? "#A78BFA" : "#475569", fontWeight: isLast ? 700 : 400 }}>{b.label}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Footer */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", paddingTop: 14, borderTop: "1px solid rgba(255,255,255,0.06)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <Icon n="clock" s={11} c="#475569" />
          <span style={{ fontSize: 11, color: "#475569" }}>Last 7 days</span>
        </div>
        <button
          onClick={onViewDetails}
          style={{
            display: "flex", alignItems: "center", gap: 6,
            background: "linear-gradient(135deg, #7C3AED, #4F46E5)",
            color: "#fff", border: "none", borderRadius: 8,
            padding: "8px 16px", fontWeight: 700, fontSize: 12,
            cursor: "pointer", fontFamily: "inherit",
            boxShadow: "0 4px 12px rgba(124,58,237,0.4)",
            transition: "all 0.2s",
          }}
          onMouseEnter={e => { e.currentTarget.style.transform = "translateY(-1px)"; e.currentTarget.style.boxShadow = "0 6px 18px rgba(124,58,237,0.5)"; }}
          onMouseLeave={e => { e.currentTarget.style.transform = "none"; e.currentTarget.style.boxShadow = "0 4px 12px rgba(124,58,237,0.4)"; }}
        >
          View Details <Icon n="chevron_right" s={12} c="#fff" />
        </button>
      </div>

      <style>{`@keyframes pulse { 0%,100%{opacity:1;box-shadow:0 0 8px #22C55E} 50%{opacity:.5;box-shadow:0 0 16px #22C55E} }`}</style>
    </div>
  );
});
PerformanceAnalyticsCard.displayName = "PerformanceAnalyticsCard";

// Canonical filter map — every Team Directory chip key maps to one or more
// workforce states. `requires_attention` is a virtual bucket (late_pending +
// absent) so the Requires Attention card click surfaces both in the same list.
// Hoisted to module scope: it references no props/state, so there's no reason
// to rebuild it inside the filter helper on every render.
const FILTER_TO_BUCKET = {
  working: ["working"], on_break: ["on_break"], idle: ["idle"],
  in_meeting: ["in_meeting"], not_started: ["not_started"],
  shift_ended: ["shift_ended"], on_leave: ["on_leave"], absent: ["absent"],
  late_pending: ["late_pending"],
  requires_attention: ["late_pending", "absent"],
  // "Present" = anyone who has clocked in today (the Analytics KPI card
  // drills into this). Includes idle/in-meeting/late so the count on the
  // card always matches the resulting list size.
  present: ["working", "on_break", "idle", "in_meeting", "late_pending"],
  // Legacy chip key kept so deep-links from old dashboards still work.
  stopped: ["idle"], yet_to_start: ["not_started"],
};

// ─── MAIN DASHBOARD ───────────────────────────────────────────────────────────
// The public-holiday calendar used to be fetched straight from date.nager.at by
// this component, cached in localStorage under a key that never expired and was
// shared by every user of the browser. That is now `api/holidays.js` → the
// Temveris API, which picks between a curated calendar and the upstream
// provider and caches server-side with a TTL. The browser has no dependency on
// any external holiday service.

export default function AdminDashboard() {
  const navigate = useNavigate();
  // `lang` is the selected BCP-47 language tag. The holiday views format dates
  // with it instead of the hardcoded "en-IN" they used to pass to
  // toLocaleDateString, which rendered Indian-style dates for every workspace.
  const { T, lang } = useLanguage();
  const { isDark, toggleDark } = useTheme();
  const { user: authUser, logout } = useAuth();
  const { workspaceName, workspaceLogo } = useWorkspace();
  // Memoize so the SAME object reference is reused across renders until the
  // theme actually flips. Previously `makeC(isDark)` allocated a brand-new
  // object on EVERY render (every WS-patch flush, keystroke, hover) — and since
  // `C` is passed as a prop to the heavy tab components (MyTeamTab, TimesheetsTab,
  // ScreenshotsTab, ActivityLog…), that fresh reference defeated their React.memo
  // and forced full re-renders. A stable `C` lets those memo boundaries hold.
  C = useMemo(() => makeC(isDark), [isDark]);

  // Read ?tab= from URL so sidebar navigation from other pages works
  const location = useLocation();
  const initialTab = (() => {
    try {
      const p = new URLSearchParams(window.location.search);
      return p.get("tab") || "overview";
    } catch { return "overview"; }
  })();

  const [lightboxUrl, setLightboxUrl] = useState(null);
  // Stable identity so ActivityLog's React.memo'd rows aren't invalidated every
  // render by a fresh inline arrow (pairs with the memoized `C` above — H4/H5).
  const handleThumbnailClick = useCallback((url) => setLightboxUrl(url), []);
  const [lightboxImageFailed, setLightboxImageFailed] = useState(false);
  useEffect(() => {
    setLightboxImageFailed(false);
  }, [lightboxUrl]);
  const [leaveActionModal, setLeaveActionModal] = useState(null);
  // leave.manage (the request list) is All-plan. leave.approvals (acting on a
  // request) is Governed+. The backend projection returns LOCK for a Verified
  // admin (not READ_ONLY — see the phase report), so `interactive` is the gate:
  // the list stays visible, the approve/reject/review actions are withheld.
  const leaveApprovals = useCapability("leave.approvals");
  // holidays.primary (add/edit local holidays) is All-plan. holidays.multicountry
  // (using other countries' saved calendars) is Sovereign+; the selector below is
  // gated on it. Base holiday management stays available regardless.
  const multiCountry = useCapability("holidays.multicountry");
  // Deep-link guard state — the projection drives which /admin tabs are reachable.
  const { projection: entitlementProjection } = useEntitlements();
  const [leaveActionComment, setLeaveActionComment] = useState("");
  // Seed the employee list from the last-known /employees/team response so
  // the Overview tab's worker grid paints instantly. The network refetch
  // below will reconcile within a second or two — the cache only exists to
  // avoid the "Loading workers..." flash on every page load.
  const [employees, setEmployees] = useState(() => readEmployeesCache() || []);
  // Alphabetical version of `employees` — used by every "Pick an employee"
  // dropdown so workers always appear sorted by name (case-insensitive).
  // The raw `employees` array keeps its API-driven order for grid views
  // that already sort it themselves (status priority, etc.).
  const employeesAlpha = useMemo(
    () => [...employees].sort((a, b) =>
      (a.name || a.email || "").localeCompare(b.name || b.email || "", undefined, { sensitivity: "base" })
    ),
    [employees],
  );
  // `loading` only blocks the worker grid render. If we already hydrated from
  // the localStorage cache we skip the blocking spinner entirely — the user
  // sees the previous snapshot of their team while the fresh data lands in
  // the background.
  const [loading, setLoading] = useState(() => (readEmployeesCache() || []).length === 0);
  // True when the MOST RECENT /employees/team refresh did not return a usable
  // payload. Combined with `employees.length` this distinguishes the two cases
  // without a second state variable:
  //
  //   failed + we still hold data  -> STALE   (keep showing it, say so)
  //   failed + nothing to show     -> NO DATA (plain error state)
  //
  // Before this existed both failures were silent: the request either threw
  // into a bare `catch {}` or came back as a non-array error body that the
  // `Array.isArray` guard skipped, so `employees` was simply never replaced and
  // the last localStorage snapshot stayed on screen looking current. That is
  // how a days-old worker grid could render with no indication anything was
  // wrong.
  const [employeesLoadFailed, setEmployeesLoadFailed] = useState(false);
  // Drives only the Retry button's own disabled/label state. Deliberately NOT
  // `loading`: flipping that would swap the grid back to the skeleton and blank
  // the very data we are trying to keep on screen.
  const [employeesRetrying, setEmployeesRetrying] = useState(false);
  const [leaveRequests, setLeaveRequests] = useState([]);
  const [tab, setTab] = useState(initialTab);
  // M1: mirror `tab` into a ref so the long-lived WebSocket onmessage handler
  // reads the CURRENT tab, not the value captured when the socket connected.
  // (The WS effect intentionally omits `tab` from its deps to avoid tearing
  // down + reconnecting the socket on every tab switch.) Without this, a
  // `new_screenshot` event triggered a 500-row /api/screenshots refetch based
  // on a stale tab — firing when the admin was NOT on the Screenshots tab, or
  // failing to fire when they were.
  const tabRef = useRef(tab);
  useEffect(() => { tabRef.current = tab; }, [tab]);

  // Keep `tab` in sync with the URL so the shared <AdminSidebar /> (which
  // navigates to `/admin?tab=X`) actually drives the in-page tab state. Also
  // syncs when the user uses browser back/forward to revisit a tab. Falls
  // back to "overview" when the URL has no ?tab param.
  useEffect(() => {
    try {
      const p = new URLSearchParams(location.search);
      const next = p.get("tab") || "overview";
      setTab(prev => (prev === next ? prev : next));
    } catch {}
  }, [location.search]);
  // Deep-link guard (Phase 3.26): a tab the projection marks HIDE (privacy /
  // unbuilt) must not be reachable by URL. Only an explicit backend HIDE
  // redirects; LOCK tabs stay and render their in-page upgrade card; a
  // null/loading projection never redirects (safe fallback). shouldRenderNav
  // already encodes exactly this — no plan logic lives here.
  useEffect(() => {
    if (entitlementProjection && !shouldRenderNav(tab, entitlementProjection)) {
      navigate("/admin?tab=overview", { replace: true });
    }
  }, [tab, entitlementProjection, navigate]);
  // Per-section collapsible groups
  const [openGroups, setOpenGroups] = useState({});
  const [search, setSearch] = useState("");
  // Ctrl+K / ⌘K jumps to the header search. The ref is null on tabs that hide
  // the box, and the shortcut then does nothing (the browser keeps its own).
  const searchInputRef = useRef(null);
  const searchShortcutLabel = useMemo(() => {
    const p = typeof navigator !== "undefined" ? (navigator.platform || navigator.userAgent || "") : "";
    return /Mac|iPhone|iPad/i.test(p) ? "⌘K" : "Ctrl K";
  }, []);
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.metaKey || e.ctrlKey) || String(e.key).toLowerCase() !== "k") return;
      const el = searchInputRef.current;
      if (!el) return;
      e.preventDefault();
      el.focus();
      el.select();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const [teamFilter, setTeamFilter] = useState("all");
  const [empTypeFilter, setEmpTypeFilter] = useState("all"); // "all" | "full_time" | "part_time"
  const [empPage, setEmpPage] = useState(1);
  const EMP_PAGE_SIZE = 12;
  // "Show all" toggle for the Work Hours Summary card on Overview. Default
  // collapsed to 12 rows so the page stays scannable; admins click to expand.
  const [showAllWorkHours, setShowAllWorkHours] = useState(false);
  const [myTeamView, setMyTeamView] = useState("grid"); // "grid" | "list"
  const [statusFilter, setStatusFilter] = useState("all");
  // Separate filter for historical (non-today) view — its card set is different
  // (Present / Absent / Clocked Out / On Leave) and maps to the `status` string
  // on records returned by /reports/attendance-detail.
  const [histStatusFilter, setHistStatusFilter] = useState("all");
  const [riskAlerts, setRiskAlerts] = useState([]);
  const [notifications, setNotifications] = useState([]);
  const [showNotifPanel, setShowNotifPanel] = useState(false);
  const [unreadCount, setUnreadCount] = useState(0);
  const notifPanelRef = useRef(null);
  const bellRef = useRef(null);
  const [bellShake, setBellShake] = useState(false);
  // List is the Overview's default (Live Workforce Overview design); Grid and
  // Timeline stay one click away in the filter bar.
  const [viewMode, setViewMode] = useState("list");
  const [showAddUser, setShowAddUser] = useState(false);
  const [viewEmp, setViewEmp] = useState(null);
  const [editEmp, setEditEmp] = useState(null);
  const [deleteEmp, setDeleteEmp] = useState(null);
  const [adminUser, setAdminUser] = useState({ name: "Admin", email: "admin@temveris.example", profile_picture: null });
  const [profileMenuOpen, setProfileMenuOpen] = useState(false);
  const profileMenuRef = useRef(null);

  // Static organisation switcher — multi-org isn't a real backend concept yet,
  // so this is a UI-only stub. Shape mirrors what a /me/orgs payload would
  // look like when we ship it. Only the active workspace is listed; the
  // sibling-orgs (Tech / Studios / Ventures) were UI-only placeholders and
  // were misleading users into thinking they could switch tenants.
  const ORGS = [
    { id: "temveris", name: "Temveris", tagline: "Primary workspace" },
  ];
  const [activeOrgId, setActiveOrgId] = useState(ORGS[0].id);
  const [orgPickerOpen, setOrgPickerOpen] = useState(false);
  const activeOrg = ORGS.find(o => o.id === activeOrgId) || ORGS[0];

  // Overview "+N more" expansion toggles. Each card slices its list to a
  // visible cap; the counter is a button that flips these flags to render
  // the full list (the scroll container already handles overflow).
  const [lateExpanded,     setLateExpanded]     = useState(false);
  const [absentExpanded,   setAbsentExpanded]   = useState(false);
  const [upcomingExpanded, setUpcomingExpanded] = useState(false);
  const [toastMsg, setToastMsg] = useState(null);
  const [wsStatus, setWsStatus] = useState("connecting"); // "connecting" | "connected" | "disconnected"

  const [showDatePicker, setShowDatePicker] = useState(false);
  const [activeDateLabel, setActiveDateLabel] = useState("Today");
  const [dateRange, setDateRange] = useState(null);
  const [historyData, setHistoryData] = useState(null);   // attendance-detail for non-today dates
  const [historyLoading, setHistoryLoading] = useState(false);
  // Monotonic id of the newest historical fetch. `authFetch` is a bare fetch
  // with no cancellation, so without this the responses race: picking Yesterday
  // then 7-days-ago could land Yesterday's payload LAST and paint it under the
  // "7 days ago" banner — wrong attendance shown with no error. Switching back
  // to Today (or "Back to Live") bumps it too, so an in-flight historical
  // response cannot drag the dashboard back out of live mode.
  const histReqSeq = useRef(0);

  // ── Manage Holiday ──────────────────────────────────────────────────────────
  const [holidays, setHolidays]               = useState([]);
  const [holidayLoading, setHolidayLoading]   = useState(false);
  // ── Public Holidays (from workspace settings → public-holidays profile) ────
  // The overview "Upcoming Holidays" card pulls from the same source as the
  // /admin/settings → Public Holidays page so admins see one calendar.
  const [publicHolidays, setPublicHolidays]   = useState([]);
  const [holidayView, setHolidayView]         = useState("table");   // "table" | "calendar"

  // ── Manage Holidays: independent Country and Year selection ────────────────
  // These used to be ONE dropdown of pre-saved "<CC>-<YEAR>" profiles, so an
  // admin could not change the year without going to Workspace Settings and
  // saving a new profile. Country and Year are now separate controls, and the
  // year is applied to BOTH the organization's own holidays and the provider
  // preview — the local table previously showed every year at once.
  //
  // holidayCountry === "" means "all of this organization's holidays".
  const [holidayCountries, setHolidayCountries]     = useState([]);   // catalog registry
  const [holidayCountryMeta, setHolidayCountryMeta] = useState(null); // provider availability
  const [holidayCountry, setHolidayCountry]         = useState("");
  const [holidayYear, setHolidayYear]               = useState(() => new Date().getFullYear());
  const [holidayOrgYears, setHolidayOrgYears]       = useState([]);
  const [holidayCountryRows, setHolidayCountryRows] = useState([]);   // classified plan entries
  const [holidayPlan, setHolidayPlan]               = useState(null); // counts + source
  const [holidayIncludeOptional, setHolidayIncludeOptional] = useState(false);
  const [holidayIncludeRegional, setHolidayIncludeRegional] = useState(false);
  const [holidayConflict, setHolidayConflict]       = useState(null); // entry under review
  const [holidayRowBusy, setHolidayRowBusy]         = useState("");   // key being added
  const [holidayCountryLoading, setHolidayCountryLoading] = useState(false);
  // `kind` drives the wording; a bare string could not distinguish "no calendar
  // for this country" from "the provider is down", which is what made every
  // failure look identical.
  const [holidayCountryError, setHolidayCountryError] = useState(null); // {kind, message}
  const [holidayCatalogInfo, setHolidayCatalogInfo]   = useState(null); // {source, partial}
  const [holidayAdopting, setHolidayAdopting]         = useState(false);

  const [holidayCalMonth, setHolidayCalMonth] = useState(() => { const n = new Date(); return { y: n.getFullYear(), m: n.getMonth() }; });
  const [holidayModal, setHolidayModal]       = useState(null);      // null | "add" | "edit"
  const [holidayEditId, setHolidayEditId]     = useState(null);
  const [holidayForm, setHolidayForm]         = useState({ name: "", date: "", holiday_type: "Public", description: "", country_code: "" });
  const [holidayFormErr, setHolidayFormErr]   = useState("");
  const [holidayDelId, setHolidayDelId]       = useState(null);
  const [holidayDelErr, setHolidayDelErr]     = useState("");
  const [holidayDeleting, setHolidayDeleting] = useState(false);
  const [holidaySaving, setHolidaySaving]     = useState(false);
  const [holidayImporting, setHolidayImporting] = useState(false);
  const [holidayCsvResult, setHolidayCsvResult] = useState(null);
  const [holidayCsvFormat, setHolidayCsvFormat] = useState("auto");
  const [holidayToast, setHolidayToast]       = useState("");

  const [overviewApps, setOverviewApps]   = useState([]);
  const [weekBars, setWeekBars]           = useState([]);
  const [overviewStats, setOverviewStats] = useState({ totalScreenshots: 0, activeToday: 0, avgHours: "0h 00m" });
  const [lastUpdated, setLastUpdated]     = useState(null);

  // Analytics data for charts
  const [analytics, setAnalytics]           = useState(null);
  const [analyticsDays, setAnalyticsDays]   = useState(7);
  // Active-slice indices for the two donut charts — drives the activeShape
  // render so the hovered slice grows + the center text shows its details.
  const [hoveredDeptIdx, setHoveredDeptIdx] = useState(null);
  const [hoveredAttIdx,  setHoveredAttIdx]  = useState(null);
  // Overview interactivity — Daily Hours chart series visibility + bar hover.
  const [dailyHoursSeries, setDailyHoursSeries] = useState({ worked: true, break: true });
  const [dailyHoursHover,  setDailyHoursHover]  = useState(-1);
  // Top Apps donut hover index for animated active slice.
  const [hoveredAppIdx, setHoveredAppIdx] = useState(null);
  // Top Employees hover (vertical bar chart).
  const [hoveredEmpIdx, setHoveredEmpIdx] = useState(-1);
  const [rptAnalyticsDays, setRptAnalyticsDays] = useState(7);
  const [siteUsageData, setSiteUsageData]   = useState(null);
  const [siteUsageLoading, setSiteUsageLoading] = useState(false);
  const [siteUsageDate, setSiteUsageDate]   = useState(() => new Date().toISOString().slice(0, 10));
  const [siteUsageEmp, setSiteUsageEmp]     = useState(null);

  // Live browser tab state, keyed by user_id. Seeded from /browser/live and
  // kept fresh via "browser_activity" WebSocket events. Consumed by the
  // Site & App Usage admin tab's "Live Now" tiles.
  const [browserLive, setBrowserLive] = useState({});

  // ── Site category override management (SETTINGS > Site Categories) ──
  const [bcOverrides, setBcOverrides]     = useState([]);        // admin-defined overrides
  const [bcDefaults, setBcDefaults]       = useState([]);        // built-in defaults (read-only)
  const [bcLoading, setBcLoading]         = useState(false);
  const [bcDraft, setBcDraft]             = useState({ domain: "", category: "work" });
  const [bcSaving, setBcSaving]           = useState(false);
  const [bcMsg, setBcMsg]                 = useState("");
  const [bcSearch, setBcSearch]           = useState("");

  // ── Time Playback (timelapse) ────────────────────────────────────────────────
  // A ?tab=timelapse&user=&date= deep link (e.g. a Compensatory Leave worked
  // date in the leave review) pre-selects that employee and day.
  const [tlDate, setTlDate]               = useState(() =>
    readPlaybackDeepLink(window.location.search)?.date || new Date().toISOString().slice(0, 10));
  const [tlEmpId, setTlEmpId]             = useState(() =>
    readPlaybackDeepLink(window.location.search)?.user || "");
  const [tlShots, setTlShots]             = useState([]);
  const [tlLoading, setTlLoading]         = useState(false);
  const [tlLightbox, setTlLightbox]       = useState(null);
  const [tlPlayIdx, setTlPlayIdx]         = useState(null);
  const [tlVideoUrl, setTlVideoUrl]       = useState(null);
  const [tlVideoLoading, setTlVideoLoading] = useState(false);
  const [tlVideoError, setTlVideoError]   = useState("");
  const [tlVideoMeta, setTlVideoMeta]     = useState(null); // { frame_count, duration_seconds, size_bytes }
  const [tlDelShot, setTlDelShot]         = useState(null);
  const [tlDelVideo, setTlDelVideo]       = useState(false);

  const [recentScreenshots, setRecentScreenshots] = useState([]);
  const [screenshotData, setScreenshotData] = useState([]);
  const [ssLoading, setSsLoading] = useState(false);
  const [ssSelectedUser, setSsSelectedUser] = useState(null);
  // Screenshots' member search is driven by the global header `search` (below),
  // so the header box filters this tab too — no separate ssEmpSearch state.
  const [ssAppSearch, setSsAppSearch] = useState("");
  // Debounced app-search keeps the fetch from re-firing on every keystroke —
  // the URL only updates 300ms after the user pauses typing.
  const [ssAppSearchDebounced, setSsAppSearchDebounced] = useState("");
  // Holds the in-flight screenshots request so a newer click/filter change
  // can abort the older one. Without this, a slow response for Employee A
  // could land after a faster response for Employee B and overwrite it.
  const ssAbortRef = useRef(null);
  const [ssActivityFilter, setSsActivityFilter] = useState("all");
  const [ssSort, setSsSort] = useState("newest");
  const [ssSelectedIds, setSsSelectedIds] = useState(new Set());
  const [ssShowIdle, setSsShowIdle] = useState(true);
  const [ssDateFilter, setSsDateFilter] = useState(() => new Date().toISOString().split("T")[0]); // always a specific date
  const [ssBlurImages, setSsBlurImages] = useState(false);

  const [tsDate, setTsDate] = useState(new Date());
  // Period filter for Timesheets — "day" shows the existing per-day card
  // grid; "this_month" / "last_month" swap the layout for a per-employee
  // monthly summary backed by /reports/monthly-attendance so admins can
  // see the full month at a glance without leaving the page.
  const [tsPeriod, setTsPeriod]         = useState("day"); // "day" | "this_month" | "last_month"
  const [tsMonthData, setTsMonthData]   = useState(null);
  const [tsMonthLoading, setTsMonthLoading] = useState(false);
  const [tsColFilter, setTsColFilter] = useState(["Clock In", "Clock Out", "Work", "Active"]);
  const [tsColOpen, setTsColOpen] = useState(false);
  const colRef = useRef(null);

  const [rptPeriod, setRptPeriod] = useState("today");
  const [rptSearch, setRptSearch] = useState("");
  const [rptSort, setRptSort] = useState("name");

  // ── Payroll state ──
  const [payrollConfigs, setPayrollConfigs] = useState([]);
  const [payrollSummary, setPayrollSummary] = useState(null);
  const [payrollRuns, setPayrollRuns] = useState([]);
  const [payrollLoading, setPayrollLoading] = useState(false);
  const [payrollTab, setPayrollTab] = useState("overview"); // overview | employees | runs | configure
  const [payEditUser, setPayEditUser] = useState(null);
  const [payForm, setPayForm] = useState({ pay_type:"salary", hourly_rate:"", basic_salary:"", hra:"", da:"", ta:"", pf:"", tax:"", bonus:"", pay_frequency:"monthly", bank_account:"", notes:"" });
  const [payRunForm, setPayRunForm] = useState({ period_start:"", period_end:"", notes:"" });
  const [payRunDetail, setPayRunDetail] = useState(null);
  const [paySearchEmp, setPaySearchEmp] = useState("");

  useEffect(() => {
    const h = (e) => { if (colRef.current && !colRef.current.contains(e.target)) setTsColOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  // Close notification panel on outside click
  useEffect(() => {
    const h = (e) => {
      if (showNotifPanel && notifPanelRef.current && !notifPanelRef.current.contains(e.target) && bellRef.current && !bellRef.current.contains(e.target)) {
        setShowNotifPanel(false);
      }
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [showNotifPanel]);

  // Only the localized labels depend on `T`; the icons/colors are static, so
  // memoize the whole map on `[T]` rather than rebuilding it every render.
  const NOTIF_CONFIG = useMemo(() => ({
    clock_in:   { icon: "play",    color: "#22C55E", bg: "#F0FDF4", label: T.clockedIn||"Clocked In" },
    clock_out:  { icon: "log_out", color: "#EF4444", bg: "#FEF3F2", label: T.clockedOut||"Clocked Out" },
    break_start:{ icon: "coffee",  color: "#F59E0B", bg: "#FFFAEB", label: T.onBreak||"On Break" },
    break_end:  { icon: "play",    color: "#3B82F6", bg: "#EFF6FF", label: T.resumedWork||"Resumed Work" },
    risk_alert: { icon: "alert",   color: "#DC2626", bg: "#FEF2F2", label: T.workHoursAlert||"Work Hours Alert" },
  }), [T]);

  const addNotification = useCallback((notif) => {
    const id = Date.now() + Math.random();
    const item = { id, time: new Date(), read: false, ...notif };
    setNotifications(prev => [item, ...prev].slice(0, 50));
    setUnreadCount(prev => prev + 1);
    setBellShake(true);
    setTimeout(() => setBellShake(false), 800);
  }, []);

  const token = readToken();
  const wsRef = useRef(null);
  const wsReconnectTimer = useRef(null);
  // Set to true while the WS effect cleanup is running so the close /
  // error handlers don't schedule a reconnect into a dead component.
  const wsTearingDownRef = useRef(false);

  const showToast = (msg, type = "success", duration = 3000) => { setToastMsg({ msg, type }); setTimeout(() => setToastMsg(null), duration); };

  // ── Admin's own time-tracking state ──
  const [myClockedIn, setMyClockedIn]       = useState(false);
  const [myOnBreak, setMyOnBreak]           = useState(false);
  const [myBreakElapsed, setMyBreakElapsed] = useState(0);
  const [myClockInTime, setMyClockInTime]   = useState(null);
  const [myWorkedToday, setMyWorkedToday]   = useState(0);
  const myClockInMsRef    = useRef(null);
  const myBreakMsRef      = useRef(null);
  const myTotalBreakMsRef = useRef(0);

  const fmtClock = (s) => {
    if (!s || s < 0) s = 0;
    return `${String(Math.floor(s/3600)).padStart(2,"0")}:${String(Math.floor((s%3600)/60)).padStart(2,"0")}:${String(s%60).padStart(2,"0")}`;
  };

  const fetchMyStatus = useCallback(async () => {
    if (!token) return;
    try {
      const res = await authFetch(`${API_BASE}/employees/status`, { headers: { Authorization: `Bearer ${token}` } });
      if (!res.ok) return;
      const d = await res.json();
      setMyClockedIn(d.clocked_in);
      setMyOnBreak(d.on_break);
      setMyBreakElapsed(d.break_elapsed_seconds || 0);
      setMyWorkedToday(d.worked_today_seconds || 0);
      if (d.clocked_in && d.clock_in_time) {
        setMyClockInTime(d.clock_in_time);
        myClockInMsRef.current    = new Date(d.clock_in_time).getTime();
        // Include the current ongoing break so the work timer doesn't count
        // break time when the admin navigates away and comes back.
        const currentBreakMs = (d.current_break_seconds || 0) * 1000;
        myTotalBreakMsRef.current = (d.break_elapsed_seconds || 0) * 1000 + currentBreakMs;
        if (d.on_break && myBreakMsRef.current === null) {
          myBreakMsRef.current = Date.now() - currentBreakMs;
        }
      } else {
        setMyClockInTime(null);
        myClockInMsRef.current = null;
      }
    } catch {}
  }, [token]);

  // PERF: removed a 1s setInterval here that updated a `myElapsed` state which
  // was never rendered anywhere — it forced a full re-render of this entire
  // ~4,500-line component every second for no visible effect. The admin's own
  // worked/break time is shown from `myWorkedToday` / `myBreakElapsed`, which
  // are populated by fetchMyStatus, so removing the dead ticker is invisible.

  const handleMyClockIn = async () => {
    try {
      const res = await fetchWithRetry(`${API_BASE}/employees/clock-in`, { method: "POST", headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) { showToast(T.adminDashboardClockedInMsg || "Clocked in"); fetchMyStatus(); }
      else { const d = await res.json(); showToast(d.detail || (T.adminDashboardClockInFailed || "Clock-in failed"), "error"); }
    } catch { showToast(T.adminDashboardNetworkError || "Network error", "error"); }
  };
  const handleMyClockOut = async () => {
    try {
      const res = await fetchWithRetry(`${API_BASE}/employees/clock-out`, { method: "POST", headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) { showToast(T.adminDashboardClockedOutMsg || "Clocked out"); fetchMyStatus(); }
      else { const d = await res.json(); showToast(d.detail || (T.adminDashboardClockOutFailed || "Clock-out failed"), "error"); }
    } catch { showToast(T.adminDashboardNetworkError || "Network error", "error"); }
  };
  const handleMyBreakStart = async () => {
    try {
      const res = await fetchWithRetry(`${API_BASE}/employees/start-break`, { method: "POST", headers: { Authorization: `Bearer ${token}` } });
      const d = await res.json().catch(() => ({}));
      // Governed denial returns HTTP 200 with { allowed:false, code, message }.
      if (res.ok && d.allowed !== false && d.success !== false) { showToast(T.adminDashboardBreakStarted || "Break started"); fetchMyStatus(); }
      else { showToast(d.message || d.detail || (T.adminDashboardFailedGeneric || "Failed"), "error"); }
    } catch { showToast(T.adminDashboardNetworkError || "Network error", "error"); }
  };
  const handleMyBreakResume = async () => {
    try {
      const res = await fetchWithRetry(`${API_BASE}/employees/resume-work`, { method: "POST", headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) { showToast(T.adminDashboardBreakEnded || "Break ended"); fetchMyStatus(); }
      else { const d = await res.json(); showToast(d.detail || (T.adminDashboardFailedGeneric || "Failed"), "error"); }
    } catch { showToast(T.adminDashboardNetworkError || "Network error", "error"); }
  };

  const fetchEmployees = useCallback(async () => {
    if (!token) return;
    try {
      // Both requests are issued together. The Early Clock Out reasons only
      // decorate a card that has already rendered, so the worker grid must not
      // wait on them — previously this awaited /team and THEN awaited
      // /employees/early-clock-outs, and `loading` only cleared after both, so
      // a supplementary call gated the primary grid.
      // `.catch` is attached at creation (not at the await) so a rejection can
      // never surface as an unhandled promise rejection if /team throws first.
      const teamPromise = apiFetch("/employees/team", token);
      const ecoPromise = apiFetch("/employees/early-clock-outs", token)
        .catch(() => null);   // non-fatal — admin/HR only endpoint

      const data = await teamPromise;
      // apiFetch resolves with the RESPONSE BODY on a non-2xx rather than
      // throwing, so a failed refresh arrives here as a non-array (e.g.
      // `{detail: ...}`). Treating that as "no update" is what made the failure
      // invisible; it is an explicit failure now. `return` still runs `finally`.
      if (!Array.isArray(data)) {
        setEmployeesLoadFailed(true);
        return;
      }
      {
        // Defensive filter: the seeded "Administrator" (CEO seat) is
        // already excluded server-side, but in case a cached payload or
        // a future endpoint variant slips it through, drop it here too
        // so the Overview cards / status grid / Requires Attention list
        // never include the system account.
        const filtered = data.filter(e => (e?.name || "").trim().toLowerCase() !== "administrator");

        // Paint the grid on the /team response alone.
        setEmployees(filtered.map(e => ({ ...e, early_clock_out: null })));
        setLastUpdated(new Date());
        setLoading(false);
        // The data on screen is current again. Cleared here — on the /team
        // payload alone — so a failing early-clock-outs call can never leave a
        // stale banner up: ECO only decorates cards that already rendered.
        setEmployeesLoadFailed(false);

        // Fold in the Early Clock Out reasons whenever they land. Applied as a
        // FUNCTIONAL update so any WebSocket status patch that arrived in the
        // meantime is preserved rather than overwritten by a stale snapshot.
        const eco = await ecoPromise;
        const ecoByUser = (eco && eco.by_user) || {};
        setEmployees(prev => prev.map(e => ({
          ...e,
          early_clock_out: ecoByUser[String(e.id)] || null,
        })));

        const withEco = filtered.map(e => ({
          ...e,
          early_clock_out: ecoByUser[String(e.id)] || null,
        }));

        // SWR-style cache: persist the last good response so the next
        // page load can paint the worker grid instantly from disk while
        // the network refetch settles in the background. Key is per-user
        // so different admins on the same browser never share cached data.
        try {
          const cacheKey = _getTeamCacheKey();
          if (cacheKey) {
            const payload = JSON.stringify(withEco);
            if (payload.length < 1_000_000) {
              localStorage.setItem(cacheKey, payload);
            }
          }
        } catch { /* private-mode browsers — ignore */ }

        // ── Detect NEW idle-15-min alerts and chime + toast for each ──
        // The backend already dedups one HR email per idle streak; here we
        // Per product decision (2026-05-13), in-app idle alerts have been
        // removed from the admin dashboard — no bell entry, no toast, no
        // chime. The backend still records each idle streak via AuditLog
        // and emails HR for follow-up; admins receive idle signals through
        // that channel rather than dashboard noise.
      }
    } catch {
      // Network error, 15 s axios timeout, or an aborted request. `employees`
      // and the localStorage cache are BOTH left untouched on purpose — the
      // last known-good snapshot stays usable; it is just no longer presented
      // as current.
      setEmployeesLoadFailed(true);
    }
    finally { setLoading(false); }
  }, [token]);

  // Retry delegates to the one fetch function rather than re-implementing the
  // request, so error handling, the ECO pairing and the axios in-flight GET
  // dedup all still apply. `employeesRetrying` gates the button so a double
  // click cannot queue a second pass, and it never touches `loading`, so the
  // data already on screen stays put while the retry runs.
  const retryEmployees = useCallback(async () => {
    if (employeesRetrying) return;
    setEmployeesRetrying(true);
    try {
      await fetchEmployees();
    } finally {
      setEmployeesRetrying(false);
    }
  }, [employeesRetrying, fetchEmployees]);

  const fetchLeaveRequests = useCallback(async () => {
    if (!token) return;
    try {
      const r = await authFetch(`${API_BASE}/leave/requests`, { headers: { Authorization: `Bearer ${token}` } });
      const d = await r.json();
      if (Array.isArray(d)) setLeaveRequests(d);
    } catch { }
  }, [token]);

  // The organization's OWN holidays, filtered to the selected country and year.
  // Both filters are omitted when nothing is selected, which is the endpoint's
  // long-standing "everything" contract.
  const fetchHolidays = useCallback(async (opts = {}) => {
    if (!token) return;
    const { countryCode = holidayCountry, year = holidayYear } = opts;
    setHolidayLoading(true);
    try {
      const rows = await holidayApi.fetchOrgHolidays({ year, countryCode });
      if (Array.isArray(rows)) setHolidays(rows);
    } catch {
      // The organization's own list failing is separate from a provider
      // failure — leave the country error state alone so one cannot mask
      // the other.
    } finally { setHolidayLoading(false); }
  }, [token, holidayCountry, holidayYear]);

  // Overview → "Upcoming Holidays" card. Reads the organization's OWN calendar,
  // which is the same source Manage Holidays writes to. It previously read the
  // saved country profile and then called nager.at, so the card could show
  // holidays nobody had actually adopted while missing ones an admin had added.
  const fetchPublicHolidays = useCallback(async () => {
    if (!token) return;
    try {
      const rows = await holidayApi.fetchOrgHolidays({});
      if (Array.isArray(rows)) setPublicHolidays(rows);
    } catch {}
  }, [token]);

  // The country registry, from the Temveris catalog rather than the provider's
  // /AvailableCountries. Curated countries (India, UAE) are included even when
  // the upstream provider is unreachable.
  const fetchHolidayCountries = useCallback(async () => {
    if (!token) return;
    try {
      const payload = await holidayApi.fetchCountries();
      setHolidayCountries(Array.isArray(payload?.countries) ? payload.countries : []);
      setHolidayCountryMeta(payload || null);
    } catch {
      setHolidayCountries([]);
    }
  }, [token]);

  // Which years this organization already has data for, so the Year selector
  // can offer them alongside the default window.
  const fetchHolidaySummary = useCallback(async () => {
    if (!token) return;
    try {
      const payload = await holidayApi.fetchHolidaySummary();
      setHolidayOrgYears(Array.isArray(payload?.years) ? payload.years : []);
    } catch {}
  }, [token]);

  // The official calendar preview for the selected country + year.
  //
  // Recovery matters as much as the request here. The previous version cleared
  // the error only on its own success path, so after a failure the red banner
  // stayed on screen through Clear, through the Table/Calendar toggle, and
  // through picking a working country — nothing but a full page reload brought
  // the page back. Every entry point now clears the previous error and the
  // stale rows BEFORE the new request starts.
  const fetchHolidaysForCountry = useCallback(async (code, year, opts = {}) => {
    setHolidayCountryError(null);
    setHolidayCatalogInfo(null);
    if (!code || !year) { setHolidayCountryRows([]); setHolidayPlan(null); return; }

    setHolidayCountryRows([]);
    setHolidayPlan(null);
    setHolidayCountryLoading(true);
    try {
      // The PLAN, not the raw calendar: the server tells us which entries the
      // organization already observes. Working that out in the browser is what
      // let the UI advertise 13 while the server wrote 20.
      const payload = await holidayApi.fetchAdoptionPlan(code, year, {
        includeOptional: opts.includeOptional ?? holidayIncludeOptional,
        includeRegional: opts.includeRegional ?? holidayIncludeRegional,
      });
      setHolidayCountryRows(payload.entries || []);
      setHolidayPlan(payload);
      setHolidayCatalogInfo({
        source: payload.source,
        partial: !!payload.partial,
        countryName: payload.country_name,
      });
    } catch (err) {
      setHolidayCountryRows([]);
      setHolidayPlan(null);
      setHolidayCountryError({
        kind: err?.kind || holidayApi.HOLIDAY_ERROR.UNKNOWN,
        message: holidayApi.holidayErrorMessage(err),
      });
    } finally {
      setHolidayCountryLoading(false);
    }
  }, [holidayIncludeOptional, holidayIncludeRegional]);

  const fetchOverviewMetrics = useCallback(async () => {
    if (!token) return;
    try {
      const headers = { Authorization: `Bearer ${token}` };
      // Parallelize the two API calls (removed the heavy 500-activity fetch;
      // activeToday is derived from the already-loaded employees list instead)
      const [appRes, hmRes] = await Promise.all([
        authFetch(`${API_BASE}/api/app-history`, { headers }),
        authFetch(`${API_BASE}/api/activity-heatmap?days=7`, { headers }),
      ]);
      const [appData, hmData] = await Promise.all([appRes.json(), hmRes.json()]);
      if (appData.timeline) {
        const sorted = [...appData.timeline].sort((a, b) => b.total_seconds - a.total_seconds).slice(0, 8);
        setOverviewApps(sorted);
      }
      if (hmData.heatmap) {
        const dayMap = {};
        hmData.heatmap.forEach(h => { dayMap[h.date] = (dayMap[h.date] || 0) + h.count; });
        const days = Object.entries(dayMap).sort((a, b) => a[0].localeCompare(b[0])).slice(-7);
        setWeekBars(days.map(([date, val]) => ({
          label: new Date(date + "T00:00:00").toLocaleDateString("en-IN", { weekday: "short" }),
          val,
        })));
        // Use heatmap's today count as screenshot count (avoids downloading 500 activities)
        const todayStr = new Date().toISOString().slice(0, 10);
        const todayCount = dayMap[todayStr] || 0;
        setOverviewStats(prev => ({ ...prev, totalScreenshots: todayCount }));
      }
      setLastUpdated(new Date());
    } catch { }
  }, [token]);

  const fetchAnalytics = useCallback(async (days) => {
    if (!token) return;
    try {
      const res = await authFetch(`${API_BASE}/employees/analytics?days=${days}`, { headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) setAnalytics(await res.json());
    } catch {}
  }, [token]);

  useEffect(() => { fetchAnalytics(analyticsDays); }, [analyticsDays, fetchAnalytics]);

  // Pulls the team's current browser tabs from the backend. Populated on
  // mount and whenever the admin hits the Refresh button; WebSocket deltas
  // keep it live between pulls.
  const fetchBrowserLive = useCallback(async () => {
    if (!token) return;
    try {
      const res = await authFetch(`${API_BASE}/browser/live`, { headers: { Authorization: `Bearer ${token}` } });
      if (!res.ok) return;
      const data = await res.json();
      if (!Array.isArray(data)) return;
      const map = {};
      data.forEach(r => { map[r.user_id] = r; });
      setBrowserLive(map);
    } catch {}
  }, [token]);

  const fetchSiteUsage = useCallback(async (dateStr) => {
    if (!token) return;
    setSiteUsageLoading(true);
    try {
      const res = await authFetch(`${API_BASE}/api/app-analytics?date=${dateStr || new Date().toISOString().slice(0, 10)}`, { headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) setSiteUsageData(await res.json());
    } catch {} finally { setSiteUsageLoading(false); }
  }, [token]);

  // Category-override CRUD: list, upsert, delete. Defaults are pulled once
  // and shown read-only so the admin can see what's already categorised
  // without needing to duplicate entries in the override table.
  const fetchBrowserCategories = useCallback(async () => {
    if (!token) return;
    setBcLoading(true);
    try {
      const [oRes, dRes] = await Promise.all([
        authFetch(`${API_BASE}/browser/categories`,          { headers: { Authorization: `Bearer ${token}` } }),
        authFetch(`${API_BASE}/browser/categories/defaults`, { headers: { Authorization: `Bearer ${token}` } }),
      ]);
      setBcOverrides(oRes.ok ? await oRes.json() : []);
      setBcDefaults(dRes.ok ? await dRes.json() : []);
    } catch {}
    finally { setBcLoading(false); }
  }, [token]);

  useEffect(() => {
    if (tab === "browsercategories") fetchBrowserCategories();
  }, [tab, fetchBrowserCategories]);

  const saveBrowserCategory = useCallback(async (domain, category) => {
    if (!token) return;
    const d = (domain || "").trim().toLowerCase().replace(/^\.+/, "");
    if (!d) { setBcMsg(T.adminDashboardEnterDomain || "Enter a domain (e.g. example.com)"); return; }
    setBcSaving(true); setBcMsg("");
    try {
      const res = await authFetch(`${API_BASE}/browser/categories`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ domain: d, category }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || "Save failed");
      setBcDraft({ domain: "", category: "work" });
      setBcMsg(`Saved ${d} → ${category}`);
      await fetchBrowserCategories();
    } catch (e) {
      setBcMsg(e.message || (T.adminDashboardSaveFailedMsg || "Save failed"));
    } finally { setBcSaving(false); }
  }, [token, fetchBrowserCategories]);

  const deleteBrowserCategory = useCallback(async (domain) => {
    if (!token) return;
    if (!window.confirm(`Remove override for ${domain}?`)) return;
    try {
      await authFetch(`${API_BASE}/browser/categories/${encodeURIComponent(domain)}`, {
        method: "DELETE", headers: { Authorization: `Bearer ${token}` },
      });
      await fetchBrowserCategories();
    } catch {}
  }, [token, fetchBrowserCategories]);

  const fetchRecentScreenshots = useCallback(async () => {
    if (!token) return;
    try {
      const res = await authFetch(`${API_BASE}/api/screenshots?limit=12&sort=newest`, { headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) {
        const d = await res.json();
        setRecentScreenshots(Array.isArray(d) ? d : d.data || []);
      }
    } catch {}
  }, [token]);

  // ── Payroll fetch helpers ──
  const fetchPayrollData = useCallback(async () => {
    if (!token) return;
    setPayrollLoading(true);
    try {
      const [cfgRes, sumRes, runRes] = await Promise.all([
        authFetch(`${API_BASE}/payroll/configs`, { headers: { Authorization: `Bearer ${token}` } }),
        authFetch(`${API_BASE}/payroll/summary`, { headers: { Authorization: `Bearer ${token}` } }),
        authFetch(`${API_BASE}/payroll/runs`, { headers: { Authorization: `Bearer ${token}` } }),
      ]);
      if (cfgRes.ok) { const d = await cfgRes.json(); setPayrollConfigs(d.configs || []); }
      if (sumRes.ok) { setPayrollSummary(await sumRes.json()); }
      if (runRes.ok) { const d = await runRes.json(); setPayrollRuns(d.runs || []); }
    } catch {}
    setPayrollLoading(false);
  }, [token]);

  useEffect(() => { if (tab === "payroll") fetchPayrollData(); }, [tab, fetchPayrollData]);

  // If HR lands here from an email link (?leave=123&action=approve|decline),
  // jump to Manage Leave and auto-open the matching action modal so they can
  // still see the full balance context before confirming.
  useEffect(() => {
    try {
      const p = new URLSearchParams(window.location.search);
      const leaveId = parseInt(p.get("leave") || "", 10);
      const action  = (p.get("action") || "").toLowerCase();
      if (!leaveId || !action || !leaveRequests.length) return;
      const r = leaveRequests.find(x => x.id === leaveId);
      if (!r) return;
      // Sync the URL with the tab we're forcing so the sidebar's "already
      // there" check stays accurate — otherwise clicking Overview after this
      // deep-link landing silently no-ops because URL has no ?tab param.
      if (p.get("tab") !== "manageleave") {
        p.set("tab", "manageleave");
        navigate(`/admin?${p.toString()}`, { replace: true });
      } else {
        setTab("manageleave");
      }
      const statusMap = { approve: "approved", decline: "rejected", review: "under_review" };
      const mapped = statusMap[action];
      if (mapped) {
        setLeaveActionModal({ id: r.id, status: mapped, name: r.name, leaveType: r.leave_type, days: r.days });
        setLeaveActionComment("");
      }
    } catch {}
  }, [leaveRequests, navigate]);

  const handleSavePayConfig = async () => {
    if (!payEditUser) return;
    try {
      const body = { user_id: payEditUser, ...payForm };
      ["hourly_rate","basic_salary","hra","da","ta","pf","tax","bonus"].forEach(k => { body[k] = parseFloat(body[k]) || 0; });
      const res = await authFetch(`${API_BASE}/payroll/configs`, {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      if (res.ok) { showToast(T.payrollConfigSaved || "Payroll config saved"); setPayEditUser(null); fetchPayrollData(); }
      else { const d = await res.json(); showToast(d.detail || (T.adminDashboardFailedToSave || "Failed to save"), "error"); }
    } catch { showToast(T.adminDashboardNetworkError || "Network error", "error"); }
  };

  const handleCreatePayRun = async () => {
    if (!payRunForm.period_start || !payRunForm.period_end) { showToast(T.adminDashboardSelectPeriodDates || "Select period dates", "error"); return; }
    try {
      const res = await authFetch(`${API_BASE}/payroll/runs`, {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(payRunForm),
      });
      if (res.ok) { showToast(T.payrollRunCreated || "Payroll run created"); setPayRunForm({ period_start:"", period_end:"", notes:"" }); fetchPayrollData(); }
      else { const d = await res.json(); showToast(d.detail || (T.adminDashboardFailedGeneric || "Failed"), "error"); }
    } catch { showToast(T.adminDashboardNetworkError || "Network error", "error"); }
  };

  const handlePayRunAction = async (runId, action) => {
    try {
      const res = await authFetch(`${API_BASE}/payroll/runs/${runId}/${action}`, {
        method: "PUT", headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) { showToast(`Run ${action === "process" ? "processed" : "marked paid"}`); fetchPayrollData(); setPayRunDetail(null); }
      else { showToast("Action failed", "error"); }
    } catch { showToast("Network error", "error"); }
  };

  const viewPayRunDetail = async (runId) => {
    try {
      const res = await authFetch(`${API_BASE}/payroll/runs/${runId}`, { headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) setPayRunDetail(await res.json());
    } catch {}
  };

  const openPayEdit = (userId) => {
    const existing = payrollConfigs.find(c => c.user_id === userId);
    if (existing) {
      setPayForm({ pay_type: existing.pay_type, hourly_rate: String(existing.hourly_rate||""), basic_salary: String(existing.basic_salary||""), hra: String(existing.hra||""), da: String(existing.da||""), ta: String(existing.ta||""), pf: String(existing.pf||""), tax: String(existing.tax||""), bonus: String(existing.bonus||""), pay_frequency: existing.pay_frequency||"monthly", bank_account: existing.bank_account||"", notes: existing.notes||"" });
    } else {
      setPayForm({ pay_type:"salary", hourly_rate:"", basic_salary:"", hra:"", da:"", ta:"", pf:"", tax:"", bonus:"", pay_frequency:"monthly", bank_account:"", notes:"" });
    }
    setPayEditUser(userId);
  };

  const fetchScreenshots = useCallback(async ({ background = false } = {}) => {
    if (!token) return;
    // Cancel any prior in-flight request so its (now stale) response cannot
    // overwrite the screenshot grid after a newer filter/employee change.
    if (ssAbortRef.current) ssAbortRef.current.abort();
    const controller = new AbortController();
    ssAbortRef.current = controller;
    // Background refreshes (live WebSocket "new_screenshot" events) must NOT
    // wipe the grid or show a spinner — doing so collapses the list to empty
    // and snaps the admin's scroll back to the top mid-scroll, and clears any
    // checkboxes they've ticked for deletion. Only a real user action
    // (employee / filter / date change → background=false) clears the grid so
    // the previous selection's screenshots don't linger while the fetch lands.
    if (!background) {
      setSsLoading(true);
      setScreenshotData([]);
      setSsSelectedIds(new Set());
    }
    try {
      const params = new URLSearchParams();
      if (ssSelectedUser !== null) params.set("user_id", ssSelectedUser);
      if (ssActivityFilter === "active") params.set("is_active", "true");
      if (ssActivityFilter === "idle") params.set("is_active", "false");
      if (ssAppSearchDebounced) params.set("app_name", ssAppSearchDebounced);
      if (ssDateFilter) params.set("date", ssDateFilter);
      params.set("sort", ssSort);
      params.set("limit", "500");
      const r = await authFetch(`${API_BASE}/api/screenshots?${params}`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (!r.ok) {
        console.error(`[screenshots] HTTP ${r.status}:`, await r.text().catch(() => ""));
        return;
      }
      const d = await r.json();
      if (controller.signal.aborted) return;
      if (Array.isArray(d)) setScreenshotData(d);
    } catch (err) {
      if (err?.name === "AbortError") return;
      console.error("[screenshots] Fetch error:", err);
    }
    finally {
      if (ssAbortRef.current === controller) {
        ssAbortRef.current = null;
        setSsLoading(false);
      }
    }
  }, [token, ssSelectedUser, ssActivityFilter, ssAppSearchDebounced, ssSort, ssDateFilter]);

  const getToken = () => readToken();

  const handleSessionExpired = () => {
    showToast(T.adminDashboardSessionExpiredDot || "Session expired — please log in again.", "error");
    setTimeout(() => { localStorage.removeItem("zt_token"); localStorage.removeItem("token"); navigate("/"); }, 1800);
  };

  const updateLeaveStatus = async (id, status, comment = "") => {
    const freshToken = getToken();
    if (!freshToken) { handleSessionExpired(); return; }
    try {
      const res = await authFetch(`${API_BASE}/leave/${id}/status`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${freshToken}`, "Content-Type": "application/json" },
        body: JSON.stringify({ status, comment }),
      });
      if (res.status === 401) { handleSessionExpired(); return; }
      if (!res.ok) { const err = await res.json().catch(() => ({})); showToast(err?.detail || (T.adminDashboardFailedToUpdate || "Failed to update"), "error"); return; }
      const data = await res.json().catch(() => ({}));
      setLeaveRequests(prev => prev.map(r => r.id === id ? { ...r, status } : r));
      const label = status === "under_review" ? (T.adminDashboardSentForReviewLabel || "sent for review") : status === "approved" ? (T.adminDashboardApprovedLabel || "approved") : status === "rejected" ? (T.adminDashboardRejectedLabel || "rejected") : status;
      // Surface email delivery state so admins know when the employee did
      // NOT actually receive the decision email — the in-app notification
      // still fires, but follow-up out-of-band is needed.
      if ((status === "approved" || status === "rejected") && data && data.email_sent === false) {
        showToast(
          `Leave ${label} — email failed (employee notified in-app). ${data.email_error || ""}`.trim(),
          "error",
        );
      } else if (status === "approved" || status === "rejected") {
        showToast(`Leave ${label} successfully — employee notified by email`);
      } else {
        showToast(`Leave ${label} successfully`);
      }
      fetchLeaveRequests();
    } catch { showToast("Failed to update — check your connection", "error"); }
  };

  // ── Fetch app settings (blur, permissions) ──
  useEffect(() => {
    authFetch(`${API_BASE}/settings`, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then(r => r.json())
      .then(d => {
        const s = d.settings || {};
        setSsBlurImages(s.screenshot_blur === "true");
      })
      .catch(() => {});
  }, [token]);

  // ── Fetch risk alerts on load ──
  const fetchRiskAlerts = useCallback(async () => {
    if (!token) return;
    try {
      const r = await authFetch(`${API_BASE}/risk-alert/history?limit=20`, { headers: { Authorization: `Bearer ${token}` } });
      if (r.ok) {
        const data = await r.json();
        if (Array.isArray(data)) {
          setRiskAlerts(data.map(a => ({
            id: a.id, employee_name: a.user_name, worked_display: a.worked_display,
            threshold_display: a.threshold_display, alert_date: a.alert_date,
            ts: new Date(a.created_at).toLocaleTimeString(),
          })));

          // ── Notifications panel: clean up + dedupe risk alerts ──
          // Three rules from product:
          //   1. Only show alerts from the last 3 days — older "1 May" rows
          //      should not dominate the panel two weeks later.
          //   2. One notification per employee — keep the most recent alert
          //      so the same name doesn't appear two/three times in a row.
          //   3. Drop entries where worked time is literally 0h 0m — those
          //      are really absent days, not "worked too little" days; the
          //      Absent pipeline handles those separately.
          const parseHmToMinutes = (s) => {
            const m = String(s || "").match(/(\d+)\s*h\s*(\d+)\s*m/i);
            return m ? parseInt(m[1], 10) * 60 + parseInt(m[2], 10) : 0;
          };
          const threeDaysMs = 3 * 24 * 60 * 60 * 1000;
          const cutoff = Date.now() - threeDaysMs;
          // Sort newest-first so the dedup pass keeps the latest entry per user.
          const sorted = [...data].sort((a, b) =>
            new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
          );
          const seenUsers = new Set();
          const cleaned = [];
          for (const a of sorted) {
            const ts = new Date(a.created_at).getTime();
            if (!Number.isFinite(ts) || ts < cutoff) continue;          // stale
            if (parseHmToMinutes(a.worked_display) === 0) continue;     // bogus 0h 0m
            const key = (a.user_name || a.user_id || a.id).toString().toLowerCase();
            if (seenUsers.has(key)) continue;                            // dedup
            seenUsers.add(key);
            cleaned.push(a);
          }
          const riskNotifs = cleaned.slice(0, 10).map(a => ({
            id: `risk-${a.id}`, type: "risk_alert", title: a.user_name || "Employee",
            message: `worked only ${a.worked_display} (required: ${a.threshold_display})`,
            time: new Date(a.created_at), read: true,
          }));
          // Replace previous risk-alert seed; preserve any live notifications
          // that arrived from other sources (idle alerts, WS events).
          setNotifications(prev => {
            const nonRisk = prev.filter(n => n.type !== "risk_alert");
            return [...nonRisk, ...riskNotifs].sort((a, b) => new Date(b.time) - new Date(a.time));
          });
        }
      }
    } catch {}
  }, [token]);

  // Debounced fetch — coalesces rapid WebSocket events into a single API call
  // so we get fresh server-computed fields (worked hours, scores) without
  // hammering the API on every heartbeat.
  const debouncedFetchRef = useRef(null);
  const debouncedFetchEmployees = useCallback(() => {
    if (debouncedFetchRef.current) clearTimeout(debouncedFetchRef.current);
    debouncedFetchRef.current = setTimeout(() => { fetchEmployees(); }, 2000);
  }, [fetchEmployees]);

  // Fast-path refetch for LOW-frequency, high-importance events: clock in/out
  // and break start/end. These are human-paced (not bursty like screenshots /
  // app_activity), so coalescing them over the full 2s debounce needlessly
  // delayed the authoritative server-computed fields (worked_today_seconds,
  // current_break_seconds, break_overrun, active_pct) by ~2s. A 250ms window
  // still collapses a morning-rush burst into one /employees/team call while
  // cutting admin "action → authoritative refresh" from ~2s to ~250ms. The 2s
  // debouncedFetchEmployees stays for the chatty events.
  const fastFetchRef = useRef(null);
  const fastFetchEmployees = useCallback(() => {
    if (fastFetchRef.current) clearTimeout(fastFetchRef.current);
    fastFetchRef.current = setTimeout(() => { fetchEmployees(); }, 250);
  }, [fetchEmployees]);

  // Coalesce screenshot-grid refetches. Every employee's screenshot upload
  // broadcasts a "new_screenshot" event; on a busy workspace that's several
  // per second. Without debouncing, each one re-pulled the entire screenshot
  // grid. A refetch a few seconds after the last event keeps the grid fresh
  // while collapsing a burst into a single request. This runs in BACKGROUND
  // mode so the live refresh never wipes the grid or resets the admin's scroll
  // position / selection while they're browsing.
  const debouncedFetchSSRef = useRef(null);
  const debouncedFetchScreenshots = useCallback(() => {
    if (debouncedFetchSSRef.current) clearTimeout(debouncedFetchSSRef.current);
    debouncedFetchSSRef.current = setTimeout(() => { fetchScreenshots({ background: true }); }, 5000);
  }, [fetchScreenshots]);

  // Manual refresh — pulls the latest data for every section the admin is
  // currently looking at. WebSocket usually keeps this live, but the button
  // is a quick escape hatch when the connection dropped or a mutation
  // happened out-of-band (e.g. invite accepted in another tab).
  const [refreshing, setRefreshing] = useState(false);
  const handleRefresh = useCallback(async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      const jobs = [fetchEmployees(), fetchMyStatus(), fetchOverviewMetrics(), fetchBrowserLive()];
      if (tab === "manageleave" || tab === "leavesummary") jobs.push(fetchLeaveRequests());
      if (tab === "overview") { jobs.push(fetchRiskAlerts()); jobs.push(fetchRecentScreenshots()); }
      if (tab === "screenshots") jobs.push(fetchScreenshots());
      if (tab === "payroll") jobs.push(fetchPayrollData());
      if (tab === "analytics") jobs.push(fetchAnalytics(analyticsDays));
      await Promise.allSettled(jobs);
      setLastUpdated(new Date());
      showToast(T.adminDashboardRefreshed || "Refreshed");
    } finally {
      setRefreshing(false);
    }
  }, [refreshing, tab, analyticsDays, fetchEmployees, fetchMyStatus, fetchOverviewMetrics, fetchBrowserLive, fetchLeaveRequests, fetchRiskAlerts, fetchRecentScreenshots, fetchScreenshots, fetchPayrollData, fetchAnalytics]);

  // ── PERF: requestAnimationFrame batching for high-frequency WebSocket patches ──
  // On a busy workspace, status / app / break / clock / browser-tab events arrive
  // many times per second. Each previously triggered an immediate setState → a
  // full dashboard re-render PER MESSAGE. We buffer the (functional) state patches
  // and apply them in a single setState on the next animation frame, collapsing a
  // burst of N messages into ONE render. Data correctness is unchanged:
  //  • employee patches are re-synced by the 2s debouncedFetchEmployees refetch
  //    (authoritative server state) — batching only changes render cadence;
  //  • browserLive is last-write-wins per user key, so coalescing a frame's
  //    heartbeats is identical to applying them sequentially.
  const wsEmpPatches = useRef([]);
  const wsBrowserPatches = useRef([]);
  const wsLastUpdatedPending = useRef(false);
  const wsRafId = useRef(null);
  const flushWsPatches = useCallback(() => {
    wsRafId.current = null;
    const emp = wsEmpPatches.current;
    const brow = wsBrowserPatches.current;
    wsEmpPatches.current = [];
    wsBrowserPatches.current = [];
    if (emp.length) setEmployees(prev => emp.reduce((acc, fn) => fn(acc), prev));
    if (brow.length) setBrowserLive(prev => brow.reduce((acc, fn) => fn(acc), prev));
    if (wsLastUpdatedPending.current) { wsLastUpdatedPending.current = false; setLastUpdated(new Date()); }
  }, []);
  const scheduleWsFlush = useCallback(() => {
    if (wsRafId.current == null && typeof requestAnimationFrame === "function") {
      wsRafId.current = requestAnimationFrame(flushWsPatches);
    }
  }, [flushWsPatches]);

  // ── WebSocket with auto-reconnect and real-time state updates ──
  // Only fetch core data on mount — tab-specific data is fetched when the tab opens.
  // `browserLive` is consumed solely by the "browsercategories" tab (which fetches
  // it on open) and is kept live thereafter by the WebSocket browser_activity
  // handler, so there is no need to fetch it up front on every admin load.
  useEffect(() => {
    // Batched setters — intentionally SHADOW the component-scope setters for the
    // lifetime of this effect so the message handler's existing
    // setEmployees / setBrowserLive / setLastUpdated calls are transparently
    // coalesced into one render per frame (see flushWsPatches above). No call-site
    // changes; patch logic untouched. The 300-item cap force-flushes if rAF is
    // paused (e.g. a backgrounded tab) so the buffers can never grow without bound.
    const setEmployees = (fn) => { wsEmpPatches.current.push(fn); if (wsEmpPatches.current.length > 300) flushWsPatches(); else scheduleWsFlush(); };
    const setBrowserLive = (fn) => { wsBrowserPatches.current.push(fn); if (wsBrowserPatches.current.length > 300) flushWsPatches(); else scheduleWsFlush(); };
    const setLastUpdated = () => { wsLastUpdatedPending.current = true; scheduleWsFlush(); };

    fetchEmployees();
    fetchMyStatus();
    fetchOverviewMetrics();

    let wsBackoff = 1000;
    // Per-run teardown flag. This used to be a ref shared by every run of
    // this effect: a re-run (React StrictMode's dev re-mount, or a refreshed
    // token changing the fetch callbacks) reset it to false before the old
    // run's socket had finished closing, so that socket's onclose scheduled a
    // reconnect which closed the NEW socket — and the two runs kept replacing
    // each other's sockets on a doubling timer, flipping the LIVE badge. Each
    // handler below also ignores a socket that is no longer the current one.
    let tearingDown = false;

    function connectWebSocket() {
      if (tearingDown) return;
      if (wsRef.current) { try { wsRef.current.close(); } catch {} }
      setWsStatus("connecting");
      try {
        const ws = new WebSocket(`${WS_URL}?token=${encodeURIComponent(getToken() || "")}`);
        wsRef.current = ws;
        let openedAt = 0;

        ws.onopen = () => {
          if (wsRef.current !== ws) return;
          openedAt = Date.now();
          setWsStatus("connected");
        };

        ws.onmessage = (event) => {
          if (wsRef.current !== ws) return;
          try {
            const data = JSON.parse(event.data);

            // Real-time employee status update
            if (data.type === "status_update" || data.type === "activity_update") {
              setEmployees(prev =>
                prev.map(emp =>
                  emp.id === data.employee_id
                    ? {
                        ...emp,
                        status: data.status || emp.status,
                        clock_in_time: data.clock_in_time !== undefined ? data.clock_in_time : emp.clock_in_time,
                        clock_out_time: data.clock_out_time !== undefined ? data.clock_out_time : emp.clock_out_time,
                        current_app: data.current_app !== undefined ? data.current_app : emp.current_app,
                        last_seen: data.timestamp || emp.last_seen,
                      }
                    : emp
                )
              );
              setLastUpdated(new Date());
              debouncedFetchEmployees();
            }

            // Real-time Active Time update — pushed once per processed tracking
            // window. The displayed % is assigned by activity-biased RANK across
            // the whole team server-side, so a single-user value can't be set in
            // place; instead refresh the team list (debounced, 30s cached) to
            // pull the re-ranked values. Live seconds/flags update in place.
            if (data.type === "active_time_update") {
              setEmployees(prev =>
                prev.map(emp =>
                  emp.id === data.user_id
                    ? {
                        ...emp,
                        active_today_seconds: data.active_today_seconds != null ? data.active_today_seconds : emp.active_today_seconds,
                        worked_today_seconds: data.worked_today_seconds != null ? data.worked_today_seconds : emp.worked_today_seconds,
                        activity_flags: data.activity_flags !== undefined ? data.activity_flags : emp.activity_flags,
                      }
                    : emp
                )
              );
              setLastUpdated(new Date());
              debouncedFetchEmployees();
            }

            // Real-time screenshot counter update
            if (data.type === "new_screenshot") {
              setOverviewStats(prev => ({ ...prev, totalScreenshots: prev.totalScreenshots + 1 }));
              setLastUpdated(new Date());
              if (tabRef.current === "screenshots") debouncedFetchScreenshots();
            }

            // Real-time app activity update
            if (data.type === "app_activity") {
              setEmployees(prev =>
                prev.map(emp =>
                  emp.id === data.employee_id
                    ? { ...emp, current_app: data.app_name || emp.current_app, last_seen: data.timestamp || emp.last_seen }
                    : emp
                )
              );
            }

            // Real-time break update — update card status silently (no notification)
            if (data.type === "break_start" || data.type === "break_end" || data.type === "break_started" || data.type === "break_ended") {
              const isStart = data.type === "break_start" || data.type === "break_started";
              setEmployees(prev =>
                prev.map(emp =>
                  (emp.id === data.employee_id || emp.id === data.user_id)
                    ? { ...emp, status: isStart ? "on break" : "working", break_type: isStart ? (data.break_type || emp.break_type) : null, current_break_type: isStart ? (data.break_type || emp.current_break_type || "break") : null, break_overtime: false }
                    : emp
                )
              );
              fastFetchEmployees();
            }

            // Break overtime — admin notification. Fires once when a
            // worker's break/lunch crosses the workspace threshold.
            if (data.type === "break_overtime") {
              setEmployees(prev =>
                prev.map(emp =>
                  (emp.id === data.user_id)
                    ? { ...emp, break_overtime: true, break_overtime_seconds: data.overtime_seconds, break_type: data.break_type || emp.break_type }
                    : emp
                )
              );
              const mins = Math.round((data.elapsed_seconds || 0) / 60);
              const kind = data.break_type === "lunch" ? "lunch" : "break";
              addNotification({
                type: "break_overtime",
                title: data.user_name || "Employee",
                message: `${kind === "lunch" ? "Lunch" : "Break"} over ${data.threshold_minutes || 15}m (now ${mins}m)`,
              });
            }

            // Real-time clock in/out — update card status silently (no notification).
            // The optimistic patch above keeps the UI snappy; the debounced
            // refetch coalesces simultaneous events (morning rush) into one
            // /employees request instead of one per worker.
            if (data.type === "clock_in") {
              setEmployees(prev =>
                prev.map(emp =>
                  emp.id === (data.employee_id || data.user_id)
                    ? { ...emp, status: "working", clock_in_time: data.clock_in_time || new Date().toISOString(), clock_out_time: null, last_seen: data.timestamp || new Date().toISOString() }
                    : emp
                )
              );
              fastFetchEmployees();
            }
            if (data.type === "clock_out") {
              setEmployees(prev =>
                prev.map(emp =>
                  emp.id === (data.employee_id || data.user_id)
                    ? {
                        ...emp,
                        status: "stopped",
                        clock_in_time: data.clock_in_time || emp.clock_in_time,
                        clock_out_time: data.clock_out_time || new Date().toISOString(),
                        break_elapsed_seconds: data.break_elapsed_seconds ?? emp.break_elapsed_seconds ?? 0,
                        worked_today_seconds: data.worked_today_seconds ?? emp.worked_today_seconds ?? 0,
                        last_seen: data.timestamp || new Date().toISOString(),
                      }
                    : emp
                )
              );
              fastFetchEmployees();
            }

            // Idle alert — removed from admin notifications (2026-05-13).
            // HR still receives an email per idle streak via the backend
            // pipeline; the dashboard no longer surfaces it.

            // Time adjustment added/reverted — admin's "My Team" cards show
            // worked_today_seconds, which now includes active adjustments.
            // Optimistically patch the affected card's worked time so the
            // change is visible BEFORE the debounced /team refetch lands.
            if (data.type === "time_adjustment_updated") {
              const delta = (data.action === "reverted" ? -1 : 1) * (data.seconds_added || 0);
              setEmployees(prev =>
                prev.map(emp =>
                  emp.id === data.user_id
                    ? { ...emp,
                        worked_today_seconds: Math.max(0, (emp.worked_today_seconds || 0) + delta),
                      }
                    : emp
                )
              );
              debouncedFetchEmployees();
              // Notify any open EmpDetailPanel so its Clock Summary refetches
              // the affected employee's adjustments without waiting on a poll.
              try {
                window.dispatchEvent(new CustomEvent("admin_time_adjustment_updated", { detail: data }));
              } catch {}
            }

            // Leave request — notify admin
            if (data.type === "new_leave_request") {
              fetchLeaveRequests();
              addNotification({ type: "clock_in", title: data.employee_name || "Employee", message: "submitted a leave request" });
            }

            // Requires Attention — server-side monitor flagged a worker as
            // Late (past shift + grace, before noon) or Absent (no clock-in
            // past noon). Surfaced on the bell so admins act even if they
            // weren't watching the Overview when it fired. Email also goes to
            // Admin + HR from the backend.
            if (data.type === "requires_attention") {
              const who = data.user_name || "Employee";
              const message = data.sub === "absent"
                ? "Absent — no clock-in today"
                : `Late — ${data.late_minutes ? `${data.late_minutes}m past ` : ""}shift start`;
              addNotification({ type: "requires_attention", title: who, message });
              debouncedFetchEmployees();
            }

            // Settings update — propagate locally, no notification
            if (data.type === "settings_updated") {
              try {
                window.dispatchEvent(new CustomEvent("settings_updated", { detail: data }));
              } catch {}
            }

            // Real-time browser tab state from the Temveris browser extension
            if (data.type === "browser_activity") {
              setBrowserLive(prev => {
                if (data.event === "tab_closed") {
                  // On close we just stamp the last_ended so the card fades;
                  // the next heartbeat from a new tab will replace the entry.
                  const entry = prev[data.user_id];
                  if (!entry) return prev;
                  return { ...prev, [data.user_id]: { ...entry, last_ended: data.ended_at } };
                }
                // heartbeat → full upsert
                return {
                  ...prev,
                  [data.user_id]: {
                    user_id:     data.user_id,
                    user_name:   data.user_name,
                    url:         data.url,
                    domain:      data.domain,
                    tab_title:   data.tab_title,
                    favicon_url: data.favicon_url,
                    browser:     data.browser,
                    is_idle:     !!data.is_idle,
                    tab_since:   data.tab_since,
                    updated_at:  data.timestamp,
                  },
                };
              });
              // Also patch the employees array so the worker card's
              // "Chrome / ChatGPT" chip refreshes on every tab switch
              // instead of waiting for the next /employees poll.
              if (data.event !== "tab_closed") {
                setEmployees(prev =>
                  prev.map(emp =>
                    emp.id === data.user_id
                      ? {
                          ...emp,
                          current_tab: {
                            tab_title:    data.tab_title || null,
                            domain:       data.domain || null,
                            browser:      data.browser || null,
                            favicon_url:  data.favicon_url || null,
                            window_title: emp.current_tab?.window_title || null,
                          },
                        }
                      : emp
                  )
                );
              }
              setLastUpdated(new Date());
            }

            // Real-time risk alert
            if (data.type === "risk_alert") {
              setRiskAlerts(prev => [{
                id: data.alert_id,
                employee_name: data.employee_name,
                worked_display: data.worked_display,
                threshold_display: data.threshold_display,
                alert_date: data.alert_date,
                ts: new Date().toLocaleTimeString(),
              }, ...prev].slice(0, 20));
              // risk_alert data is visible in the Risk Users tab — no bell notification needed
            }
          } catch { }
        };

        ws.onerror = () => { if (wsRef.current === ws) setWsStatus("disconnected"); };
        ws.onclose = async (event) => {
          // A socket this run replaced, or one closed by teardown, must not
          // touch the status or schedule a reconnect.
          if (tearingDown || wsRef.current !== ws) return;
          setWsStatus("disconnected");
          // A connection that held for a while was healthy: start the backoff
          // over instead of creeping toward the 60 s cap across a long session.
          if (openedAt && Date.now() - openedAt > 10000) wsBackoff = 1000;
          // 1008 = server rejected the handshake (expired/invalid JWT).
          // Refresh the token first so the next connect attempt succeeds.
          if (event.code === 1008) {
            const tok = await refreshAccessToken();
            if (tok === false) return; // session dead, stop reconnecting
            if (tearingDown || wsRef.current !== ws) return;
            wsBackoff = 1000;          // successful refresh resets backoff
          }
          const delay = wsBackoff;
          wsBackoff = Math.min(wsBackoff * 2, 60000);
          wsReconnectTimer.current = setTimeout(connectWebSocket, delay);
        };
      } catch {
        setWsStatus("disconnected");
        if (!tearingDown) {
          const delay = wsBackoff;
          wsBackoff = Math.min(wsBackoff * 2, 60000);
          wsReconnectTimer.current = setTimeout(connectWebSocket, delay);
        }
      }
    }

    wsTearingDownRef.current = false;
    connectWebSocket();

    return () => {
      tearingDown = true;
      wsTearingDownRef.current = true;
      wsBackoff = 1000;
      // Cancel any pending rAF flush and drop buffered patches so nothing
      // setStates into a torn-down effect.
      if (wsRafId.current != null) { try { cancelAnimationFrame(wsRafId.current); } catch { /* */ } wsRafId.current = null; }
      wsEmpPatches.current = [];
      wsBrowserPatches.current = [];
      wsLastUpdatedPending.current = false;
      if (wsReconnectTimer.current) clearTimeout(wsReconnectTimer.current);
      const sock = wsRef.current;
      // Released here so the next run starts its own socket instead of closing
      // this one mid-handshake, and this socket's handlers see it is no longer
      // current.
      wsRef.current = null;
      if (!sock) return;
      // Closing a WebSocket while it's still in CONNECTING state makes
      // Chrome log "WebSocket is closed before the connection is
      // established" — visible on every React StrictMode dev re-mount.
      // Wait for `open` (or `error`) before calling close() so the
      // handshake completes cleanly.
      if (sock.readyState === WebSocket.CONNECTING) {
        const closeWhenReady = () => { try { sock.close(); } catch { /* */ } };
        sock.addEventListener("open",  closeWhenReady, { once: true });
        sock.addEventListener("error", closeWhenReady, { once: true });
      } else {
        try { sock.close(); } catch { /* */ }
      }
    };
  }, [fetchEmployees, fetchOverviewMetrics, fetchMyStatus, fetchBrowserLive, debouncedFetchEmployees]);

  // Warm the lazy tab chunks during idle so tab-switching doesn't flash the
  // Suspense fallback while the chunk downloads. Runs once.
  //
  // Gated on the first /employees/team response (`lastUpdated`) rather than on
  // mount. requestIdleCallback alone did NOT keep this off the critical path:
  // during the mount burst the page is network-bound while the main thread sits
  // idle, which is precisely when idle callbacks fire — so ~500 KB of secondary
  // tab chunks (Reports, Billing, Payroll, Intelligence, …) were downloading in
  // competition with the dashboard's own API requests. Waiting for the primary
  // response preserves the stated intent ("after the dashboard paints") instead
  // of merely approximating it. preloadAdminTabChunks() self-guards, so the
  // extra dependency cannot cause a second run.
  useEffect(() => {
    if (!lastUpdated) return;
    preloadAdminTabChunks();
  }, [lastUpdated]);

  // ── Lazy tab fetches — only load data when the user navigates to the tab ──
  const leaveTabsLoaded = useRef(false);
  const riskAlertsLoaded = useRef(false);
  const recentSSLoaded = useRef(false);
  useEffect(() => {
    // Both the Manage Leave tab AND the Overview tab's "Upcoming Leaves" widget
    // consume `leaveRequests` — so fetch on either. (Overview used to skip this,
    // which is why the widget showed "No upcoming leaves" until you visited
    // Manage Leaves and came back with the state already populated.) The Leave
    // Summary tab renders <LeaveSummaryModule/>, which fetches its own data, so
    // it doesn't need the (slow) /leave/requests call here.
    if ((tab === "manageleave" || tab === "overview") && !leaveTabsLoaded.current) {
      leaveTabsLoaded.current = true;
      fetchLeaveRequests();
    }
  }, [tab, fetchLeaveRequests]);
  useEffect(() => {
    if (tab === "overview" && !riskAlertsLoaded.current) {
      riskAlertsLoaded.current = true;
      fetchRiskAlerts();
    }
  }, [tab, fetchRiskAlerts]);
  useEffect(() => {
    if (tab === "overview" && !recentSSLoaded.current) {
      recentSSLoaded.current = true;
      fetchRecentScreenshots();
    }
  }, [tab, fetchRecentScreenshots]);

  // Polling acts as a safety net only — the WebSocket above already pushes
  // every clock-in/out, break, and leave event in real time, so we can poll
  // much less aggressively. Pause polling entirely when the tab is hidden.
  useEffect(() => {
    let i;
    const tick = () => { if (!document.hidden) fetchEmployees(); };
    i = setInterval(tick, 120000);           // team status: 2 min (WS covers real-time)
    return () => clearInterval(i);
  }, [fetchEmployees]);
  useEffect(() => {
    if (tab !== "manageleave") return;        // Leave Summary self-fetches; don't double-poll
    const tick = () => { if (!document.hidden) fetchLeaveRequests(); };
    const i = setInterval(tick, 300000);     // leave requests: 5 min
    return () => clearInterval(i);
  }, [tab, fetchLeaveRequests]);
  useEffect(() => {
    let i;
    const tick = () => { if (!document.hidden) fetchOverviewMetrics(); };
    i = setInterval(tick, 300000);           // overview metrics: 5 min
    return () => clearInterval(i);
  }, [fetchOverviewMetrics]);

  // Sync the local adminUser mirror from AuthContext so a profile-picture
  // or name update on the Profile page propagates to the topbar avatar
  // immediately (no reload). The first /auth/me fetch is owned by
  // AuthProvider; we just react to its `user` value.
  useEffect(() => {
    const t = getToken();
    if (!t) { navigate("/login"); return; }
    if (authUser) {
      setAdminUser({
        name:            authUser.name  || "Admin",
        email:           authUser.email || "",
        profile_picture: authUser.profile_picture || null,
      });
    }
  }, [authUser, navigate]);

  // Close the profile dropdown when clicking outside of it
  useEffect(() => {
    if (!profileMenuOpen) return;
    const onDocClick = (e) => {
      if (profileMenuRef.current && !profileMenuRef.current.contains(e.target)) {
        setProfileMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [profileMenuOpen]);

  useEffect(() => {
    if (viewEmp) { const updated = employees.find(e => e.id === viewEmp.id); if (updated) setViewEmp(updated); }
  }, [employees]);

  // Debounce the screenshots app-search box. Without this, every keystroke
  // fired a fresh /api/screenshots request (each pulling up to 500 rows),
  // which made the grid feel sluggish while typing.
  useEffect(() => {
    const t = setTimeout(() => setSsAppSearchDebounced(ssAppSearch), 300);
    return () => clearTimeout(t);
  }, [ssAppSearch]);

  useEffect(() => { if (tab === "screenshots") fetchScreenshots(); }, [tab, fetchScreenshots]);
  // Landing on Manage Holidays loads the reference data the selectors need.
  // The holiday rows themselves come from the country/year effect below, so a
  // tab visit and a filter change take exactly the same path.
  useEffect(() => {
    if (tab !== "manageholiday") return;
    fetchHolidayCountries();
    fetchHolidaySummary();
  }, [tab, fetchHolidayCountries, fetchHolidaySummary]);

  // ONE effect owns every (country, year) transition: the organization's own
  // rows and the official preview are refetched together, so the table and the
  // calendar can never show two different selections. Selecting a country also
  // moves the calendar to January of the selected year, so the swapped-in rows
  // are visible without paging.
  useEffect(() => {
    if (tab !== "manageholiday") return;
    fetchHolidays({ countryCode: holidayCountry, year: holidayYear });
    if (holidayCountry) {
      fetchHolidaysForCountry(holidayCountry, holidayYear);
    } else {
      // Clearing the country must also clear the provider error and its stale
      // rows — otherwise the error branch keeps the whole page hidden.
      setHolidayCountryRows([]);
      setHolidayCountryError(null);
      setHolidayCatalogInfo(null);
    }
    setHolidayCalMonth((prev) => (prev.y === holidayYear ? prev : { y: holidayYear, m: 0 }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, holidayCountry, holidayYear, holidayIncludeOptional, holidayIncludeRegional]);

  // Timesheets page period filter — fetch the matching month from
  // /reports/monthly-attendance when admin picks This/Last Month.
  // "day" mode keeps using the live employees list (today's data).
  useEffect(() => {
    if (tab !== "timesheets" || tsPeriod === "day" || !token) return;
    const today = new Date();
    const target = tsPeriod === "this_month"
      ? today
      : new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const monthStr = `${target.getFullYear()}-${String(target.getMonth() + 1).padStart(2, "0")}`;
    setTsMonthLoading(true);
    authFetch(`${API_BASE}/reports/monthly-attendance?month=${monthStr}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setTsMonthData(d); })
      .catch(() => {})
      .finally(() => setTsMonthLoading(false));
  }, [tab, tsPeriod, token]);
  // Load the organization's holidays once the admin token is available so the
  // Overview's "Upcoming Holidays" card has real data on first paint.
  //
  // This is now ONE request against the organization's own calendar. It used to
  // be two — the saved country profile plus a cross-origin nager.at call — and
  // the card showed the external calendar rather than what Manage Holidays had
  // actually configured.
  useEffect(() => {
    if (!token) return;
    fetchPublicHolidays();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);
  useEffect(() => { if (tab === "browsercategories") { fetchSiteUsage(siteUsageDate); fetchBrowserLive(); } }, [tab]);  // eslint-disable-line
  useEffect(() => {
    if (tab !== "screenshots") return;
    const iv = setInterval(() => fetchScreenshots({ background: true }), 1200000);
    return () => clearInterval(iv);
  }, [tab, fetchScreenshots]);

  // Canonical workforce status buckets — one worker, one state.
  // Maps every raw status to its canonical key via STATUS_CONFIG so legacy
  // backend strings (stopped, offline, "yet to start", ...) bucket correctly.
  // Shift-aware overrides come from the SERVER's `arrival_status` (P9) — the
  // client no longer computes lateness. computeShiftInfo is now a lookup over
  // that field, not a derivation.
  const canonicalKey = (e) => {
    const raw = (effectiveStatus(e) || "").toLowerCase();
    // Approved leave wins over any shift-phase override. Without this, a
    // worker on leave who didn't clock in gets flipped to Absent past noon.
    const rawNorm = raw.replace("_", " ");
    if (rawNorm === "on leave" || rawNorm === "leave") return "on_leave";
    const shift = computeShiftInfo(e);
    if (shift.hasShift) {
      if (shift.phase === "late_pending")     return "late_pending";   // → Requires Attention
      if (shift.phase === "absent_after_noon") return "absent";        // genuine Absent
      // "late_present" / "within_grace" / "before_shift" fall through to
      // the raw backend status so Working / On Break / Yet to Start show.
    }
    return STATUS_CONFIG[raw]?.key || raw;
  };

  // Single O(n) pass over employees, memoised. Previously this section ran
  // nine separate filter() passes on every render — and the live-clock
  // intervals in this page caused renders every second, so a 100-employee
  // org was doing 900 filter operations per second for no reason.
  const statusCounts = useMemo(() => {
    const c = { working: 0, on_break: 0, idle: 0, in_meeting: 0,
                not_started: 0, shift_ended: 0, on_leave: 0, absent: 0,
                late_pending: 0, app_not_installed: 0 };
    for (const e of employees) {
      // Archived/offboarded workers (is_active === false) live only in the
      // Archived view — every status list excludes them (getFilteredEmps:
      // is_active !== false), so the counts must too or the card badge
      // over-counts vs. the clicked list (e.g. an offboarded worker reads as
      // "absent" and inflates Requires Attention).
      if (e.is_active === false) continue;
      const key = canonicalKey(e);
      if (key in c) c[key] += 1;
      if (!e.app_installed) c.app_not_installed += 1;
    }
    return c;
  }, [employees]);

  const workingNow      = statusCounts.working;
  const onBreakNow      = statusCounts.on_break;
  const idleCount       = statusCounts.idle;
  const inMeetingCount  = statusCounts.in_meeting;
  const notStartedNow   = statusCounts.not_started;
  const shiftEndedNow   = statusCounts.shift_ended;
  const onLeaveCount    = statusCounts.on_leave;
  const absentCount     = statusCounts.absent;
  const appNotInstalled = statusCounts.app_not_installed;

  // Requires Attention = workers past shift + grace who haven't clocked in
  // yet, INCLUDING those past the noon absent threshold. Per product decision
  // (2026-05-13) Absent workers roll up into Requires Attention so admins see
  // one count to act on. Part-time workers are excluded — they don't owe a
  // fixed daily shift, so they can't be "late" for one. The standalone
  // Absent chip below the cards keeps showing every absent (incl. part-time)
  // so an admin can still drill in if they want; only the action-list count
  // is filtered. Clicking the card filters to both late_pending AND absent.
  const latePendingCount   = statusCounts.late_pending;
  const requiresAttention  = useMemo(() => {
    let n = 0;
    for (const e of employees) {
      if (e.is_active === false) continue;   // archived workers never appear in the list — don't count them
      // No employment-type carve-out. Whether someone can be "late" is their
      // RESOLVED schedule's answer: the server only emits late_pending/absent
      // when the employee has a scheduled start on a working day of theirs
      // (schedule_policy/annotate.py). Skipping part-timers here meant an org
      // that rosters them to fixed shifts could never see one flagged, and no
      // setting at any scope could turn it back on.
      const k = canonicalKey(e);
      if (k === "late_pending" || k === "absent") n += 1;
    }
    return n;
  }, [employees]);

  // Backward-compatibility aliases (older code paths still reference these names).
  const stopped    = idleCount;
  const yetToStart = notStartedNow;

  // useCallback so the memoized EmpCard sees a stable `onToggleType` ref and
  // doesn't re-render every card when an unrelated piece of dashboard state
  // (filters, live ticks, panel open/close) flips.
  const handleToggleType = useCallback(async (emp) => {
    const newType = normEmpType(emp.employment_type) === "full_time" ? "part_time" : "full_time";
    try {
      const res = await authFetch(`${API_BASE}/employees/${emp.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ employment_type: newType }),
      });
      if (res.ok) {
        setEmployees(prev => prev.map(e => e.id === emp.id ? { ...e, employment_type: newType } : e));
        showToast(`${emp.name} set to ${newType === "full_time" ? "Full Time" : "Part Time"}`);
      }
    } catch { showToast("Failed to update", "error"); }
  }, [token, showToast]);

  const handleEmployeeAdded = () => { showToast(T.adminDashboardEmployeeInvitedSuccess || "Employee invited successfully!"); fetchEmployees(); };
  // NOTE: we deliberately do NOT call setEditEmp(null) here — the modal
  // controls its own closure (auto-closes after 1.2s for profile-only saves,
  // stays open after a clock-time save so the admin can read the green
  // confirmation banner). Closing it here would kill that banner immediately.
  const handleEmployeeSaved = (updatedEmp) => { setEmployees(prev => prev.map(e => e.id === updatedEmp.id ? { ...e, ...updatedEmp } : e)); fetchEmployees(); };
  const handleEmployeeDeleted = (empId) => { setEmployees(prev => prev.filter(e => e.id !== empId)); showToast(T.adminDashboardEmployeeDeleted || "Employee deleted!"); setDeleteEmp(null); fetchEmployees(); };
  // Format a Date as YYYY-MM-DD in the user's local timezone. Using
  // `toISOString()` would shift past midnight UTC and silently fetch
  // the wrong calendar day for any non-UTC user (e.g. an IST admin
  // picking April 28 ends up viewing April 27).
  const localISO = (d) => {
    if (!d) return null;
    const yyyy = d.getFullYear();
    const mm   = String(d.getMonth() + 1).padStart(2, "0");
    const dd   = String(d.getDate()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd}`;
  };

  const handleDateApply = ({ date, period, range }) => {
    const todayStr = localISO(new Date());
    const fromStr  = localISO(range?.from);
    const isTodayPick = period === "Today" || (!fromStr || fromStr === todayStr);

    // Show "Today" in the chip whenever the picked day resolves to today,
    // regardless of how it was reached (period button vs. clicking the
    // current date in the calendar grid).
    setActiveDateLabel(
      isTodayPick
        ? (T.today || "Today")
        : period === "Custom"
          ? date.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })
          : period
    );
    setDateRange(isTodayPick ? null : range);

    if (isTodayPick) {
      // Drop any prior historical view so live tiles + cards take over again.
      // Bumping the sequence retires any still-in-flight historical fetch, so a
      // late response cannot re-enter historical mode after this.
      histReqSeq.current += 1;
      setHistoryData(null);
      setHistoryLoading(false);
      setHistStatusFilter("all");
      showToast(`Filter set: ${T.today || "Today"}`);
      return;
    }

    // Fetch historical attendance data for the selected date. Only the NEWEST
    // request may touch state — see histReqSeq.
    const seq = (histReqSeq.current += 1);
    const isCurrent = () => seq === histReqSeq.current;
    setHistoryLoading(true);
    authFetch(`${API_BASE}/reports/attendance-detail?date=${fromStr}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (isCurrent()) setHistoryData(d || null); })
      .catch(() => { if (isCurrent()) setHistoryData(null); })
      .finally(() => { if (isCurrent()) setHistoryLoading(false); });

    showToast(`Showing data for ${period}`);
  };

  const getFilteredEmps = ({ applyStatus = true } = {}) => {
    let emps = [...employees];
    // Archived view = `is_active === false` employees. Default views exclude
    // them so an archived worker doesn't pollute Working / On Break / counts.
    if (empTypeFilter === "archived") {
      emps = emps.filter(e => e.is_active === false);
    } else {
      emps = emps.filter(e => e.is_active !== false);
    }
    // Name, email, team, department or job title: the header search promises
    // "employees or teams".
    if (search) {
      const q = search.toLowerCase();
      emps = emps.filter(e => [e.name, e.email, e.team, e.department, e.designation]
        .some(v => (v || "").toLowerCase().includes(q)));
    }
    if (teamFilter !== "all") {
      // Match against team OR department so the Hours-by-Department
      // donut click-to-filter works regardless of whether the donut
      // bucket came from `User.department` or `User.team` (the
      // backend's /employees/analytics endpoint coalesces the two).
      const tf = teamFilter.toLowerCase();
      emps = emps.filter(e =>
        (e.team || "").toLowerCase() === tf ||
        (e.department || "").toLowerCase() === tf ||
        (tf === "unassigned" && !(e.team || e.department))
      );
    }
    // "archived" is a virtual bucket handled above (is_active === false) —
    // don't try to also match it against employment_type here.
    if (empTypeFilter !== "all" && empTypeFilter !== "archived") emps = emps.filter(e => normEmpType(e.employment_type) === empTypeFilter);
    // Canonical filter — every chip key maps to one or more workforce states.
    // FILTER_TO_BUCKET is a static map hoisted to module scope (it reads no
    // props/state), so it isn't rebuilt on every filter pass / render.
    const targetBuckets = FILTER_TO_BUCKET[statusFilter];
    if (applyStatus && targetBuckets) {
      const set = new Set(targetBuckets);
      emps = emps.filter(e => set.has(canonicalKey(e)));
      // No part-time carve-out here either — see the note on `requiresAttention`
      // above. The card count and this list stay in step because neither
      // filters by employment type; both read the server's resolved bucket.
    }
    return emps.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  };
  // Memoise the default (applyStatus=true) filtered+sorted list. It's read 6+
  // times per render (paging, counts, three tab views) and the page's live-clock
  // intervals re-render every second — without this the full filter+sort over
  // the employee array ran on every one of those. Same [employees]-staleness
  // profile as the statusCounts/requiresAttention memos above (canonicalKey /
  // computeShiftInfo are treated as functions of `employees`).
  const filteredEmps = useMemo(
    () => getFilteredEmps(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [employees, search, teamFilter, empTypeFilter, statusFilter],
  );
  // Reset to page 1 whenever any filter changes
  useEffect(() => { setEmpPage(1); }, [search, teamFilter, empTypeFilter, statusFilter]);

  // The current page as a stable array, so the Overview table and grid only
  // re-derive their rows when the list or page actually changes, not on every
  // live-clock render.
  const pagedEmps = useMemo(
    () => filteredEmps.slice((empPage - 1) * EMP_PAGE_SIZE, empPage * EMP_PAGE_SIZE),
    [filteredEmps, empPage],
  );
  const empTotalPages = () => Math.max(1, Math.ceil(filteredEmps.length / EMP_PAGE_SIZE));

  const teams = useMemo(
    () => ["all", ...Array.from(new Set(employees.map(e => e.team).filter(Boolean)))],
    [employees],
  );

  const card = (extra = {}) => ({ background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`, padding: 20, boxShadow: C.shadow, ...extra });

  // ── Sidebar — production-grade naming model ──
  // Sections and labels follow the Customer-Facing Surface spec; original keys
  // (k) are preserved for routing/analytics/QA backward compatibility.
  const NAV_ITEMS = [
    { section: "OVERVIEW", items: [
      { k: "overview",     icon: "dashboard",  label: T.overview || "Overview" },
    ]},
    { section: "TEAM MANAGEMENT", items: [
      { k: "myteam",       icon: "users",      label: T.teamDirectory     || "Team Directory" },
      { k: "screenshots",  icon: "camera",     label: T.screenshots       || "Screenshots" },
      { k: "timesheets",   icon: "clock",      label: T.attendanceTimesheets || "Attendance & Timesheets" },
      { k: "timelapse",    icon: "timelapse",  label: T.timePlayback      || "Time Playback" },
      { k: "reports",      icon: "bar_chart",  label: T.reporting         || "Reporting" },
    ]},
    { section: "PRODUCTIVITY", items: [
      { k: "activitylog",       icon: "activity",  label: T.activityLog          || "Activity Log" },
      { k: "appshistory",       icon: "activity",  label: T.appsHistory          || "Apps History" },
      { k: "browsercategories", icon: "tag",       label: T.websiteCategories    || "Website Categories" },
    ]},
    { section: "LEAVE", items: [
      { k: "manageleave",   icon: "calendar",  label: T.leaveManagement || "Leave Management" },
      { k: "leavesummary",  icon: "briefcase", label: T.leaveSummary    || "Leave Summary" },
      { k: "manageholiday", icon: "tag",       label: T.publicHolidays  || "Public Holidays" },
    ]},
    { section: "FINANCE", items: [
      { k: "payroll",      icon: "dollar",     label: T.payrollCalculator || "Payroll Calculator" },
      // Billing now lives at /settings/billing-subscription (Workspace
      // Settings → Billing & Subscription). Removing the sidebar entry
      // avoids two paths to the same module.
    ]},
    { section: "SYSTEM", items: [
      { k: "profile",      icon: "users",      label: T.myProfile         || "My Profile" },
      { k: "settings",     icon: "settings",   label: T.workspaceSettings || "Workspace Settings" },
    ]},
  ];

  // Filter Bar — Workforce Status System §3 (order is normative).
  // Memoized on the localized strings + the status counts it interpolates
  // (all of which derive from `employees`), so the chip array is rebuilt only
  // when those actually change rather than on every live-clock re-render.
  // Workforce size for the "% of workforce" lines, the summary and the "All"
  // chip: active workers only, the same population every status count and the
  // default list use (archived workers live in the Archived view).
  const activeTotal = useMemo(
    () => employees.reduce((n, e) => n + (e.is_active === false ? 0 : 1), 0),
    [employees],
  );

  const statusTabs = useMemo(() => [
    { k: "all",          l: `${T.all || "All"} (${activeTotal})`, dot: "#94A3B8" },
    { k: "working",      l: `${T.working      || "Working"}      (${workingNow})`,      dot: "#16A34A" },
    { k: "on_break",     l: `${T.onBreak      || "On Break"}     (${onBreakNow})`,      dot: "#F59E0B" },
    { k: "not_started",  l: `${T.awaitingClockIn || "Awaiting Clock-In"} (${notStartedNow})`, dot: "#3B82F6" },
    { k: "idle",         l: `${T.stoppedWorking || "Stopped Working"} (${idleCount})`,  dot: "#9CA3AF" },
    { k: "on_leave",     l: `${T.onLeave      || "On Leave"}     (${onLeaveCount})`,    dot: "#A855F7" },
    { k: "absent",       l: `${T.absent       || "Absent"}       (${absentCount})`,     dot: "#DC2626" },
  ], [T, activeTotal, workingNow, onBreakNow, notStartedNow, idleCount, onLeaveCount, absentCount]);

  const isPlaceholder = false; // all tabs now have real implementations

  // PERF: the employment-type dropdown previously ran SEVEN full
  // `employees.filter(...).length` scans inline in JSX on every render. Fold
  // them into a single pass, memoized on `employees`, so the counts are
  // computed once per data change instead of 7× per render.
  const empTypeCounts = useMemo(() => {
    const c = { full_time: 0, part_time: 0, temporary: 0, casual: 0, field: 0, factory: 0, archived: 0 };
    for (const e of employees) {
      if (e.is_active === false) { c.archived++; continue; }
      const t = normEmpType(e.employment_type);
      if (t === "full_time" || t === "part_time" || t === "temporary" || t === "casual" || t === "field" || t === "factory") {
        c[t]++;
      }
    }
    return c;
  }, [employees]);

  // ── Overview aggregations ─────────────────────────────────────────────────
  // These were previously computed inside inline IIFEs in the Overview JSX, so
  // they re-ran on EVERY render — and this page's live-clock intervals trigger
  // a render every second. Hoisted to memos keyed on their real inputs so the
  // filter/map/sort/reduce passes only re-run when the underlying data changes.
  // Outputs are byte-identical to the previous inline logic.

  // "Daily Hours" chart reduces — derived purely from `analytics`.
  const dailyHoursAgg = useMemo(() => {
    const daily = (analytics && analytics.daily_hours) || [];
    const totalWorked = daily.reduce((s, d) => s + (d.worked || 0), 0);
    const totalBreak  = daily.reduce((s, d) => s + (d.break  || 0), 0);
    const peakIdx = daily.reduce((bi, d, i) => (d.worked > (daily[bi]?.worked ?? -1) ? i : bi), 0);
    return { daily, totalWorked, totalBreak, peakIdx };
  }, [analytics]);

  // "Work Load Analysis" — three filters + the segment descriptors. `segments`
  // interpolates localized labels, so `T` is included alongside the data deps.
  const workLoadAgg = useMemo(() => {
    const empHours = (analytics && analytics.emp_hours) || [];
    // Buckets were the literals 6 and 9 — a full-timer's day, applied to
    // part-timers, contractors and every tenant alike. They are now a band
    // around each worker's OWN expected hours (from the schedule resolver,
    // delivered as `expected_seconds`); a row without one is unclassified
    // rather than measured against a guess.
    const expectedFor = (e) => {
      const secs = Number(e.expected_seconds);
      return Number.isFinite(secs) && secs > 0 ? secs / 3600 : null;
    };
    const BAND = 0.25;   // ±25% around expectation is "healthy"
    const overWorked   = empHours.filter(e => { const x = expectedFor(e); return x != null && e.worked > x * (1 + BAND); });
    const lessUtilised = empHours.filter(e => { const x = expectedFor(e); return x != null && e.worked < x * (1 - BAND); });
    const healthy      = empHours.filter(e => { const x = expectedFor(e); return x != null && e.worked >= x * (1 - BAND) && e.worked <= x * (1 + BAND); });
    const total = empHours.length || 1;
    const segments = [
      { label: T.adminDashboardOverWorked || "Over Worked", count: overWorked.length, pct: Math.round(overWorked.length / total * 100), color: "#EF4444", bg: "#FEF2F2" },
      { label: T.adminDashboardHealthy || "Healthy", count: healthy.length, pct: Math.round(healthy.length / total * 100), color: "#22C55E", bg: "#F0FDF4" },
      { label: T.adminDashboardLessUtilised || "Less Utilised", count: lessUtilised.length, pct: Math.round(lessUtilised.length / total * 100), color: "#F59E0B", bg: "#FFFAEB" },
    ];
    return { overWorked, healthy, lessUtilised, total, segments };
  }, [analytics, analyticsDays, T]);

  // "Late Clock-in" list. This used to re-derive lateness in the browser from
  // a literal 10:30 start and a literal +05:30 offset — one tenant's shift on
  // one tenant's clock. The server already answers it per employee, against
  // that employee's own resolved shift and grace.
  const lateEmps = useMemo(() => employees.filter(emp => {
    if (!emp.clock_in_time) return false;
    const st = emp.arrival_status;
    return st === "late" || st === "very_late";
  }), [employees]);

  // "Absence Today" — whoever the SERVER says is Absent or On Leave.
  //
  // This used to add "yet to start" employees once `orgHour >= absent_after_hour`,
  // i.e. it re-derived absence in the browser from a bare hour of the day. The
  // backend now decides absence as `shift start + schedule.absent_after_minutes`
  // (schedule_policy/classify.absence_cutoff) and can never call someone absent
  // before their shift begins — so this panel listed a 16:30 part-timer from
  // noon while the status badge on the same row correctly said Yet to Start.
  // One question, one answer, and it comes from the server.
  const absentEmps = useMemo(() => employees.filter(emp => {
    const s = effectiveStatus(emp).toLowerCase();
    return s === "absent" || s === "on_leave";
  }), [employees]);

  // "Currently Using Apps" — workers with an active app whose status is Working.
  const activeAppEmps = useMemo(
    () => employees.filter(e => e.current_app && (e.status || "").toLowerCase() === "working"),
    [employees],
  );

  // "Work Hours Summary" — today's logged work per employee (sorted desc).
  // Only consumed when `historyData` is absent; the historical branch in the
  // JSX still derives from historyData inline. Included `historyData` and
  // `showAllWorkHours` in deps per the conservative dependency rule even though
  // the computation itself reads only `employees` (so a future change to the
  // branch logic stays correct).
  const workedEmps = useMemo(
    () => employees.filter(e => (e.worked_today_seconds > 0) || e.clock_in_time).map(emp => {
      const workSecs = emp.worked_today_seconds != null
        ? emp.worked_today_seconds
        : (() => {
            const cin = emp.clock_in_time ? parseUTC(emp.clock_in_time) : null;
            const cout = emp.clock_out_time ? parseUTC(emp.clock_out_time) : new Date();
            return cin ? Math.max(0, Math.floor((cout - cin) / 1000) - (emp.break_elapsed_seconds || 0)) : 0;
          })();
      return { ...emp, workSecs };
    }).filter(e => e.workSecs > 0).sort((a, b) => b.workSecs - a.workSecs),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [employees, historyData, showAllWorkHours],
  );

  // "Upcoming Leaves" — future leave requests sorted by start date. Includes
  // pending (not-yet-approved) requests so admins can see what's coming and act
  // on it; only rejected/cancelled requests are hidden.
  const upcomingLeaves = useMemo(() => {
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const hidden = new Set(["rejected", "declined", "cancelled", "canceled", "withdrawn"]);
    return leaveRequests
      .filter(lr => !hidden.has((lr.status || "").toLowerCase()) && new Date(lr.from_date || lr.start_date) >= today)
      .sort((a, b) => new Date(a.from_date || a.start_date) - new Date(b.from_date || b.start_date));
  }, [leaveRequests]);

  // "Upcoming Holidays" — the organization's own holidays on/after today, top 6.
  //
  // This used to merge the configured calendar with a live nager.at fetch, so
  // the card could advertise holidays nobody had adopted. `publicHolidays` is
  // now the unfiltered organization calendar, which is exactly what employees
  // see and what attendance and payroll compute against. `holidays` is NOT read
  // here: it carries the Manage Holidays page's country/year filter, and the
  // Overview card must not inherit that selection.
  const upcomingHols = useMemo(
    () => upcomingHolidays(publicHolidays, new Date().toISOString().slice(0, 10), 6),
    [publicHolidays],
  );

  // ── Apps Analytics state (wired to /api/app-analytics) ──
  const [appsHistoryDate, setAppsHistoryDate]       = useState(() => new Date().toISOString().slice(0, 10));
  const [appsHistoryUserId, setAppsHistoryUserId]   = useState("");
  const [appsAnalytics, setAppsAnalytics]           = useState(null);
  const [appsHistoryLoading, setAppsHistoryLoading] = useState(false);
  const [appExpandedUser, setAppExpandedUser]        = useState(null);

  const fetchAppsHistory = useCallback(async () => {
    if (tab !== "appshistory" || !token) return;
    setAppsHistoryLoading(true);
    try {
      const params = new URLSearchParams({ date: appsHistoryDate });
      if (appsHistoryUserId) params.append("user_id", appsHistoryUserId);
      const res = await authFetch(`${API_BASE}/api/app-analytics?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        setAppsAnalytics(await res.json());
      } else {
        setAppsAnalytics(null);
      }
    } catch { setAppsAnalytics(null); }
    setAppsHistoryLoading(false);
  }, [tab, token, appsHistoryDate, appsHistoryUserId]);

  useEffect(() => { fetchAppsHistory(); }, [fetchAppsHistory]);

  const fmtAppDur = (secs) => {
    const s = Math.max(0, Math.floor(+secs || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s % 60}s`;
    return `${s}s`;
  };

  const APP_COLORS = ["#11496A","#22C55E","#F59E0B","#EF4444","#8B5CF6","#EC4899","#06B6D4","#F97316","#6366F1","#14B8A6"];

  return (
    <>
      <style>{`
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; background: ${C.bg}; color: ${C.text}; }
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }
        @keyframes ztPulse { 0%,100%{opacity:.45} 50%{opacity:.85} }
        /* ── EmpCard premium animations ── status dot pulse + pulse-bar drift */
        @keyframes zt-status-pulse {
          0%   { box-shadow: 0 0 0 0   rgba(0, 229, 168, 0.55); }
          70%  { box-shadow: 0 0 0 8px rgba(0, 229, 168, 0);    }
          100% { box-shadow: 0 0 0 0   rgba(0, 229, 168, 0);    }
        }
        @keyframes zt-pulse-slide {
          0%   { transform: translateX(-100%); }
          100% { transform: translateX(100%);  }
        }
        /* ── Sidebar nav — subtle, professional hover states ─────────── */
        .zt-nav-btn { transition: background .15s ease, color .15s ease; }
        .zt-nav-btn:hover { background: ${isDark ? "#1F2937" : "#F3F4F6"}; }
        .zt-nav-icon { transition: color .15s ease; }
        .zt-nav-child { transition: background .15s ease; }
        .zt-nav-child:hover { background: ${isDark ? "#1F2937" : "#F3F4F6"}; }
        /* Open My Dashboard CTA */
        .zt-cta { transition: background .15s ease, box-shadow .15s ease; }
        .zt-cta:hover { box-shadow: 0 4px 12px rgba(17,73,106,.25); }
        .zt-cta-arrow { display: inline-block; }
        /* Sign-out */
        .zt-signout { transition: background .15s ease; }
        .zt-signout:hover { background: rgba(239,68,68,.08) !important; }
        .zt-signout-ic { display: inline-flex; }
        /* Ripple — subtle click feedback */
        @keyframes ztRipple { to { transform: scale(2.2); opacity: 0; } }
        .zt-ripple {
          position: absolute; border-radius: 50%;
          background: rgba(17,73,106,.18); pointer-events: none;
          width: 14px; height: 14px; transform: scale(0); opacity: 1;
          animation: ztRipple .45s ease-out forwards;
          z-index: 0;
        }
        /* Smooth height collapse for grouped nav children */
        .zt-collapse {
          display: grid;
          grid-template-rows: 0fr;
          transition: grid-template-rows .22s ease;
        }
        .zt-collapse[data-open="true"] { grid-template-rows: 1fr; }
        .zt-collapse > .zt-collapse-inner { overflow: hidden; min-height: 0; }
        /* Avatar (footer) — static gradient */
        .zt-avatar {
          background: linear-gradient(135deg, ${C.brand} 0%, #8B5CF6 100%) !important;
        }
        /* ── Workforce Status — top cards ──────────────────────────── */
        .zt-stat-card { transition: box-shadow .15s ease, border-color .15s ease; }
        .zt-stat-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,.06); }
        .zt-stat-card .zt-stat-icon, .zt-stat-card .zt-stat-link { transition: opacity .15s ease; }
        .zt-stat-dot {
          position: absolute; top: -3px; right: -3px;
          width: 10px; height: 10px; border-radius: 50%;
          border: 2px solid #fff;
        }
        .zt-stat-derived { background-image: linear-gradient(180deg, rgba(220,38,38,.04) 0%, transparent 60%); }
        /* ── Status Chip (filter bar) ──────────────────────────────── */
        .zt-chip {
          position: relative; overflow: hidden;
          transition: background .15s ease, color .15s ease, border-color .15s ease;
        }
        .zt-chip-dot {
          width: 7px; height: 7px; border-radius: 50%;
          flex-shrink: 0;
        }
        @keyframes bellShake {
          0%, 100% { transform: rotate(0); }
          25% { transform: rotate(6deg); }
          75% { transform: rotate(-6deg); }
        }
        @keyframes notifPop {
          0% { transform: scale(.6); opacity: 0; }
          100% { transform: scale(1); opacity: 1; }
        }
        @keyframes notifSlideDown {
          0% { opacity: 0; transform: translateY(-6px); }
          100% { opacity: 1; transform: translateY(0); }
        }
        @keyframes notifFadeIn {
          0% { opacity: 0; }
          100% { opacity: 1; }
        }
        .row-h:hover { background: ${C.bg} !important; }
        /* Scrollbar styling lives in index.css (wide brand-blue thumb,
           green hover). Local override removed so it stays consistent. */
      `}</style>

      {/* ── LEAVE ACTION MODAL ── */}
      {leaveActionModal && (
        <div onClick={() => { setLeaveActionModal(null); setLeaveActionComment(""); }}
          style={{ position: "fixed", inset: 0, zIndex: 99998, background: "rgba(0,0,0,0.5)", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: C.surface, borderRadius: 12, padding: 28, width: 440, boxShadow: "0 24px 60px rgba(0,0,0,0.25)", border: `1px solid ${C.border}` }}>
            <p style={{ fontSize: 15, fontWeight: 700, color: C.text, marginBottom: 4 }}>
              {leaveActionModal.status === "approved" ? `✅ ${T.approveLeaveLabel || "Approve Leave"}` : leaveActionModal.status === "rejected" ? `❌ ${T.declineLeaveLabel || "Decline Leave"}` : `📋 ${T.sendForReviewLabel || "Send for Review"}`}
            </p>
            <p style={{ fontSize: 12, color: C.textSub, marginBottom: 16 }}>{leaveActionModal.name} · {leaveActionModal.leaveType} · {leaveActionModal.days}d</p>
            <textarea value={leaveActionComment} onChange={e => setLeaveActionComment(e.target.value)}
              placeholder={leaveActionModal.status === "rejected" ? (T.adminDashboardReasonForDecline || "Reason for decline (required)...") : (T.adminDashboardOptionalComment || "Optional comment for the employee...")}
              rows={3}
              style={{ width: "100%", border: `1px solid ${C.border}`, borderRadius: 8, padding: "10px 12px", fontSize: 13, fontFamily: "inherit", color: C.text, background: C.bg, resize: "none", boxSizing: "border-box", outline: "none" }}
            />
            <div style={{ display: "flex", gap: 10, marginTop: 16, justifyContent: "flex-end" }}>
              <button onClick={() => { setLeaveActionModal(null); setLeaveActionComment(""); }}
                style={{ padding: "8px 18px", borderRadius: 7, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>{T.cancel || "Cancel"}</button>
              <button
                disabled={leaveActionModal.status === "rejected" && !leaveActionComment.trim()}
                onClick={async () => { await updateLeaveStatus(leaveActionModal.id, leaveActionModal.status, leaveActionComment); setLeaveActionModal(null); setLeaveActionComment(""); }}
                style={{ padding: "8px 22px", borderRadius: 7, border: "none", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit",
                  background: leaveActionModal.status === "approved" ? C.green : leaveActionModal.status === "rejected" ? C.red : "#7C3AED",
                  color: "#fff", opacity: (leaveActionModal.status === "rejected" && !leaveActionComment.trim()) ? 0.5 : 1 }}>
                {leaveActionModal.status === "approved" ? (T.approve || "Approve") : leaveActionModal.status === "rejected" ? (T.reject || "Decline") : (T.sendForReviewLabel || "Send for Review")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── SCREENSHOT LIGHTBOX ── */}
      {lightboxUrl && (
        <div onClick={() => setLightboxUrl(null)} style={{ position: "fixed", inset: 0, zIndex: 99999, background: "rgba(0,0,0,0.88)", display: "flex", alignItems: "center", justifyContent: "center", cursor: "zoom-out" }}>
          <div onClick={e => e.stopPropagation()} style={{ position: "relative", maxWidth: "92vw", borderRadius: 12, overflow: "hidden", boxShadow: "0 24px 80px rgba(0,0,0,0.7)", display: "flex", flexDirection: "column" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 16px", background: "rgba(10,13,22,0.95)", backdropFilter: "blur(12px)", borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div style={{ width: 34, height: 34, borderRadius: "50%", background: "linear-gradient(135deg,#11496A,#0E3B57)", display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 13, flexShrink: 0 }}>{((lightboxUrl.name || "?")[0]).toUpperCase()}</div>
                <div>
                  <p style={{ color: "#fff", fontWeight: 700, fontSize: 13, margin: 0, lineHeight: 1.3 }}>{lightboxUrl.name || (T.adminDashboardUnknown || "Unknown")}</p>
                  {lightboxUrl.appName && <p style={{ color: "#11496A", fontSize: 11, fontWeight: 600, margin: 0 }}>{lightboxUrl.appName}</p>}
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 16, marginLeft: 24 }}>
                {lightboxUrl.timestamp && <span style={{ color: "#A0ADB9", fontSize: 12, whiteSpace: "nowrap" }}>🕐 {(ts => { const s = String(ts); return new Date(/[Z+]/.test(s.slice(-6)) ? s : s + "Z"); })(lightboxUrl.timestamp).toLocaleString("en-IN", { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: true, timeZone: getOrgTimeZone() })}</span>}
                {lightboxUrl.keyPresses != null && <span style={{ color: "#A0ADB9", fontSize: 12, whiteSpace: "nowrap" }}>⌨️ {T.adminDashboardKeyPressCount || "Key press count:"} {lightboxUrl.keyPresses}</span>}
                {lightboxUrl.mouseClicks != null && <span style={{ color: "#A0ADB9", fontSize: 12, whiteSpace: "nowrap" }}>🖱️ {T.adminDashboardMouseClickCount || "Mouse click count:"} {lightboxUrl.mouseClicks}</span>}
                <button onClick={() => setLightboxUrl(null)} style={{ width: 28, height: 28, borderRadius: "50%", border: "none", background: "rgba(255,255,255,0.12)", color: "#fff", fontSize: 16, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", lineHeight: 1, flexShrink: 0 }}>×</button>
              </div>
            </div>
            {lightboxImageFailed ? (
              <div style={{ width: "min(720px, 92vw)", height: "min(480px, 60vh)", background: "#0A0D16", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 14 }}>
                <Icon n="camera" s={64} c="#3A4658" />
                <p style={{ color: "#A0ADB9", fontSize: 14, fontWeight: 600, margin: 0 }}>{T.adminDashboardImageNotAvailable || "Image not available"}</p>
                <p style={{ color: "#5A6472", fontSize: 12, margin: 0, textAlign: "center", maxWidth: 360, lineHeight: 1.5 }}>{T.adminDashboardImageNotAvailableDesc || "Activity was recorded but the screen capture could not be saved for this moment."}</p>
              </div>
            ) : (
              <AuthImage url={lightboxUrl.url} token={token} eager alt="screenshot" style={{ display: "block", maxWidth: "92vw", maxHeight: "80vh", objectFit: "contain" }} onError={() => setLightboxImageFailed(true)} />
            )}
          </div>
        </div>
      )}

      {toastMsg && (
        <div style={{ position: "fixed", top: 20, right: 20, zIndex: 9999, background: toastMsg.type === "error" ? C.redBg : C.greenBg, border: `1px solid ${toastMsg.type === "error" ? C.redBdr : C.greenBdr}`, color: toastMsg.type === "error" ? C.red : C.green, padding: "10px 20px", borderRadius: 10, fontSize: 13, fontWeight: 600, boxShadow: "0 4px 20px rgba(0,0,0,.15)" }}>
          {toastMsg.msg}
        </div>
      )}

      {showDatePicker && <DateFilterPicker onClose={() => setShowDatePicker(false)} onApply={handleDateApply} currentDate={new Date()} />}
      {showAddUser && <AddUsersModal onClose={() => setShowAddUser(false)} token={token} onEmployeeAdded={handleEmployeeAdded} />}
      {editEmp && <EditEmpModal emp={editEmp} onClose={() => setEditEmp(null)} token={token} onSaved={handleEmployeeSaved} />}
      {deleteEmp && <DeleteConfirmModal emp={deleteEmp} onClose={() => setDeleteEmp(null)} token={token} onDeleted={handleEmployeeDeleted} />}
      {viewEmp && <EmpDetailPanel emp={viewEmp} onClose={() => setViewEmp(null)} token={token} />}

      <div style={{ display: "flex", height: "100vh", background: C.bg, overflow: "hidden" }}>

        {/* ═══ SIDEBAR — shared component, identical to Profile/Leave/Settings.
             Drives navigation by pushing /admin?tab=X to the URL; the useEffect
             above on location.search picks that up and updates `tab`, so all
             the existing setTab-based tab handlers keep working untouched. ═══ */}
        <AdminSidebar activeKey={tab} />

        {/* Inline sidebar fallback below is kept disabled — the original
            implementation in case the shared component ever needs to be
            patched in a hurry. Toggle the `false &&` to flip back. */}
        {false && (
        <aside style={{ width: 220, flexShrink: 0, background: C.surface, borderRight: `1px solid ${C.border}`, display: "flex", flexDirection: "column", height: "100vh", overflowY: "auto" }}>
          <div style={{ padding: "14px 12px 10px", borderBottom: `1px solid ${C.borderLight}`, display: "flex", flexDirection: "column", alignItems: "center", gap: 4, flexShrink: 0 }}>
            <img
              src={TemverisLogo}
              alt="Temveris"
              style={{ width: "100%", maxWidth: 180, height: "auto", maxHeight: 32, objectFit: "contain", display: "block" }}
            />
            <p style={{ fontSize: 9, color: C.textLight, fontWeight: 600, letterSpacing: 0.3, margin: 0, textAlign: "center", lineHeight: 1.2 }}>
              {T.brandTagline || "Workforce Truth • Financial Control"}
            </p>
          </div>
          <nav style={{ flex: 1, padding: "8px", overflowY: "auto" }}>
            {/* ── Open My Dashboard link — animated gradient CTA ── */}
            <button
              className="zt-cta"
              onClick={() => navigate("/dashboard")}
              style={{
                width: "100%", margin: "0 0 10px", padding: "10px 12px",
                borderRadius: C.rSm, border: `1.5px solid ${C.brandBdr}`,
                background: `linear-gradient(90deg, ${isDark ? "#0D2847" : "#EFF8FF"} 0%, ${C.brandBg} 50%, ${isDark ? "#0D2847" : "#EFF8FF"} 100%)`,
                color: C.brand, fontWeight: 700, fontSize: 12, cursor: "pointer",
                fontFamily: "inherit", textAlign: "center", letterSpacing: ".2px",
              }}
            >
              Open My Dashboard <span className="zt-cta-arrow">→</span>
            </button>

            {NAV_ITEMS.map(({ section, items }) => (
              <div key={section} style={{ marginBottom: 8 }}>
                <p style={{ fontSize: 10, fontWeight: 800, color: C.textLight, letterSpacing: 1.2, padding: "10px 10px 6px", textTransform: "uppercase" }}>{section}</p>
                {items.map((item) => {
                  const { k, icon, label, group, children } = item;
                  const isActive = tab === k || (group && children?.some(c => c.k === tab));
                  const isOpen   = group ? (openGroups[k] ?? children?.some(c => c.k === tab)) : false;
                  return (
                    <div key={k}>
                      <button
                        className="zt-nav-btn"
                        data-active={isActive ? "true" : "false"}
                        onClick={(e) => {
                          spawnRipple(e);
                          if (group) {
                            setOpenGroups(p => ({ ...p, [k]: !(p[k] ?? children?.some(c => c.k === tab)) }));
                            return;
                          }
                          if (k === "settings")    { navigate("/settings"); return; }
                          if (k === "profile")     { navigate("/profile"); return; }
                          if (k === "manageleave") { navigate("/admin/leave"); return; }
                          if (k === "myteam") { setStatusFilter("all"); setHistStatusFilter("all"); }
                          // Drive the tab through the URL — not just local state.
                          // The location.search useEffect above on this component
                          // (and the shared <AdminSidebar/>) read the ?tab= param
                          // to figure out what's active. If we only call setTab(k)
                          // here, the URL never changes, so clicking "Overview"
                          // afterwards hits the AdminSidebar's "already there"
                          // early-return and the screen stops responding.
                          const currentTab = new URLSearchParams(location.search).get("tab");
                          if (currentTab === k) return;
                          navigate(`/admin?tab=${k}`);
                        }}
                        style={{
                          position: "relative", overflow: "hidden",
                          width: "100%", display: "flex", alignItems: "center", gap: 9,
                          padding: "9px 10px", borderRadius: C.rSm, border: "none",
                          background: isActive ? C.brandBg : "transparent",
                          color: isActive ? C.brand : C.textSub,
                          fontWeight: isActive ? 700 : 500, fontSize: 13,
                          cursor: "pointer", fontFamily: "inherit", marginBottom: 1, textAlign: "left",
                          transition: "background .18s ease, color .18s ease, transform .18s cubic-bezier(.34,1.56,.64,1)",
                          boxShadow: isActive ? `inset 3px 0 0 ${C.brand}, 0 1px 6px ${isDark ? "rgba(17,73,106,.18)" : "rgba(17,73,106,.10)"}` : "none",
                        }}
                      >
                        <span className="zt-nav-icon" style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 22, height: 22, borderRadius: 6, transition: "transform .25s cubic-bezier(.34,1.56,.64,1)" }}>
                          <Icon n={icon} s={15} c={isActive ? C.brand : C.textSub} />
                        </span>
                        <span style={{ flex: 1 }}>{label}</span>
                        {group && (
                          <span style={{ display: "inline-flex", transition: "transform .22s ease", transform: isOpen ? "rotate(90deg)" : "rotate(0deg)", color: isActive ? C.brand : C.textLight }}>
                            <Icon n="chevron_right2" s={12} c={isActive ? C.brand : C.textLight} />
                          </span>
                        )}
                      </button>
                      {group && (
                        <div className="zt-collapse" data-open={isOpen ? "true" : "false"} aria-hidden={!isOpen}>
                          <div className="zt-collapse-inner">
                            <div className="zt-nav-children" style={{ paddingLeft: 18, marginBottom: 4 }}>
                              {children.map((c) => {
                                const childActive = tab === c.k;
                                return (
                                  <button key={c.k}
                                    className="zt-nav-btn zt-nav-child"
                                    data-active={childActive ? "true" : "false"}
                                    onClick={(e) => {
                                      spawnRipple(e);
                                      // Drive sub-tab through URL so the
                                      // shared sidebar stays in sync.
                                      const currentTab = new URLSearchParams(location.search).get("tab");
                                      if (currentTab === c.k) return;
                                      navigate(`/admin?tab=${c.k}`);
                                    }}
                                    style={{
                                      position: "relative", overflow: "hidden",
                                      width: "100%", display: "flex", alignItems: "center", gap: 8,
                                      padding: "7px 10px", borderRadius: C.rSm, border: "none",
                                      background: childActive ? C.brandBg : "transparent",
                                      color: childActive ? C.brand : C.textSub,
                                      fontWeight: childActive ? 700 : 500, fontSize: 12.5,
                                      cursor: "pointer", fontFamily: "inherit", marginBottom: 1, textAlign: "left",
                                      transition: "background .18s, color .18s, transform .18s cubic-bezier(.34,1.56,.64,1)",
                                      boxShadow: childActive ? `inset 3px 0 0 ${C.brand}` : "none",
                                    }}>
                                    <span style={{ width: 4, height: 4, borderRadius: "50%", background: childActive ? C.brand : C.textLight, flexShrink: 0, transition: "transform .2s", transform: childActive ? "scale(1.6)" : "scale(1)" }} />
                                    <span className="zt-nav-icon" style={{ display: "inline-flex", transition: "transform .25s cubic-bezier(.34,1.56,.64,1)" }}>
                                      <Icon n={c.icon} s={13} c={childActive ? C.brand : C.textSub} />
                                    </span>
                                    <span style={{ flex: 1 }}>{c.label}</span>
                                  </button>
                                );
                              })}
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ))}
          </nav>
          <div style={{ padding: "12px 14px", borderTop: `1px solid ${C.border}`, flexShrink: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 8 }}>
              <div className="zt-avatar" style={{ width: 32, height: 32, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 12 }}>{initials(adminUser.name)}</div>
              <div style={{ overflow: "hidden" }}>
                <p style={{ fontWeight: 700, fontSize: 12, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{adminUser.name}</p>
                <p style={{ fontSize: 11, color: C.textSub, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{adminUser.email}</p>
              </div>
            </div>
            <button
              className="zt-signout"
              onClick={() => { localStorage.removeItem("token"); localStorage.removeItem("zt_token"); navigate("/login"); }}
              style={{ width: "100%", display: "flex", alignItems: "center", gap: 7, padding: "8px 10px", borderRadius: C.rSm, border: `1px solid ${C.border}`, background: "none", color: C.red, fontWeight: 600, fontSize: 12, cursor: "pointer", fontFamily: "inherit", transition: "background .18s" }}>
              <span className="zt-signout-ic"><Icon n="log_out" s={13} c={C.red} /></span>{T.signOut}
            </button>
          </div>
        </aside>
        )}

        {/* ═══ MAIN CONTENT ═══ */}
        <main style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>

          <header style={{ height: 68, flexShrink: 0, background: C.surface, borderBottom: `1px solid ${C.border}`, display: "flex", alignItems: "center", gap: 10, padding: "0 24px" }}>
            {/* The header search drives the employee list on every tab that has
                one, matching name, email, team, department or job title. On
                tabs with no employee roster (Leave / Settings / Billing /
                Holidays / Reports / Payroll) it filtered nothing, so it's hidden
                there rather than sitting inert. Ctrl+K (⌘K on a Mac) focuses it.

                On sizing: a fixed basis with no grow, so the box sizes
                independently of the buttons beside it and shrinks to minWidth on
                narrow screens instead of pushing them into wrapping. */}
            {["overview", "myteam", "timesheets", "screenshots", "browsercategories"].includes(tab) && (
            <div style={{ flex: "0 1 400px", position: "relative", minWidth: 180 }}>
              <SearchIcon size={18} color={C.textLight} aria-hidden style={{ position: "absolute", left: 13, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }} />
              <input ref={searchInputRef} type="search"
                aria-label={T.searchEmployeesTeams || "Search employees or teams"}
                placeholder={T.searchEmployeesTeamsPlaceholder || "Search employees or teams…"}
                value={search} onChange={e => setSearch(e.target.value)}
                style={{ width: "100%", boxSizing: "border-box", padding: "10px 70px 10px 40px", borderRadius: 10, border: `1.5px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13.5, fontFamily: "inherit", outline: "none", transition: "border-color .2s, box-shadow .2s" }}
                onFocus={e => { e.target.style.borderColor = C.brand; e.target.style.boxShadow = `0 0 0 3px ${C.brandBg}`; }}
                onBlur={e => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }} />
              <kbd aria-hidden style={{ position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)", padding: "2px 7px", borderRadius: 6, border: `1px solid ${C.border}`, background: C.bg, color: C.textSub, fontSize: 11.5, fontFamily: "inherit", fontWeight: 600, pointerEvents: "none", whiteSpace: "nowrap" }}>
                {searchShortcutLabel}
              </kbd>
            </div>
            )}
            {/* Shared with the employee dashboard's header — why it exists and
                why it hides inside the desktop shell is documented on the
                component itself. */}
            <DownloadAppButton
              variant="solid"
              label={T.downloadApp || "Download App"}
              title={T.downloadDesktopApp || "Download the desktop app"}
              gapColor={C.surface}
            />
            <button onClick={() => setShowAddUser(true)}
              style={{ display: "flex", alignItems: "center", gap: 8, padding: "9px 18px", borderRadius: 10, border: "none", background: navyFor(isDark), color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit", transition: "box-shadow .2s", flexShrink: 0, whiteSpace: "nowrap" }}
              onMouseEnter={e => { e.currentTarget.style.boxShadow = "0 6px 16px -6px rgba(15,40,70,.6)"; }}
              onMouseLeave={e => { e.currentTarget.style.boxShadow = "none"; }}>
              <UserPlus size={16} aria-hidden />{T.inviteUser || "Invite Worker"}
            </button>
            <button onClick={() => setShowDatePicker(true)}
              style={{ display: "flex", alignItems: "center", gap: 7, padding: "8px 14px", borderRadius: 10, border: `1.5px solid ${activeDateLabel !== "Today" ? C.brand : C.border}`, background: activeDateLabel !== "Today" ? C.brandBg : C.surface, color: activeDateLabel !== "Today" ? C.brand : C.text, fontWeight: activeDateLabel !== "Today" ? 700 : 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit", transition: "all .2s", flexShrink: 0, whiteSpace: "nowrap" }}>
              <CalendarDays size={16} aria-hidden />{activeDateLabel}
            </button>
            {/* Worker-type filter for the roster tabs. The Overview carries the
                same filter, Archived included, in its own filter bar. */}
            {tab !== "overview" && (
            <select value={empTypeFilter} onChange={e => setEmpTypeFilter(e.target.value)}
              style={{ padding: "8px 14px", borderRadius: C.rSm, border: `1.5px solid ${empTypeFilter !== "all" ? C.brand : C.textSub}`, background: empTypeFilter !== "all" ? C.brand : C.surface, color: empTypeFilter !== "all" ? "#fff" : C.text, fontWeight: empTypeFilter !== "all" ? 700 : 500, fontSize: 13, cursor: "pointer", fontFamily: "inherit", outline: "none", transition: "all .2s", boxShadow: "0 1px 3px rgba(0,0,0,0.12)", flexShrink: 0 }}>
              <option value="all">{T.allTypes || "All Types"}</option>
              <option value="full_time">{T.fullTime || "Full Time"} ({empTypeCounts.full_time})</option>
              <option value="part_time">{T.partTime || "Part Time"} ({empTypeCounts.part_time})</option>
              <option value="temporary">{T.temporary || "Temporary"} ({empTypeCounts.temporary})</option>
              <option value="casual">{T.casual || "Casual"} ({empTypeCounts.casual})</option>
              <option value="archived">{T.archived || "Archived"} ({empTypeCounts.archived})</option>
            </select>
            )}
            <div style={{ flex: 1 }} />
            {/* ── Refresh ── */}
            <button onClick={handleRefresh} disabled={refreshing}
              title={T.refresh || "Refresh"}
              style={{ width: 38, height: 38, borderRadius: "50%", border: `1.5px solid ${C.border}`, background: C.surface, display: "flex", alignItems: "center", justifyContent: "center", cursor: refreshing ? "default" : "pointer", flexShrink: 0, transition: "all .2s", opacity: refreshing ? 0.7 : 1 }}
              onMouseEnter={e => { if (!refreshing) { e.currentTarget.style.borderColor = C.brandAccent; e.currentTarget.style.background = C.brandAccentBg; } }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.background = C.surface; }}>
              <span style={{ display: "inline-flex", animation: refreshing ? "spin 0.8s linear infinite" : "none" }}>
                <Icon n="refresh" s={16} c={refreshing ? C.brand : C.textSub} />
              </span>
            </button>

            <button onClick={toggleDark} aria-label={isDark ? (T.lightMode || "Light mode") : (T.darkMode || "Dark mode")} title={isDark ? (T.lightMode || "Light mode") : (T.darkMode || "Dark mode")}
              style={{ width: 38, height: 38, borderRadius: "50%", border: `1.5px solid ${C.border}`, background: C.surface, display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", flexShrink: 0, transition: "all .2s" }}>
              <Icon n={isDark ? "sun" : "moon"} s={16} c={C.textSub} />
            </button>
            <LangSwitcher isDark={isDark} />
            {/* ── Notification Bell ── */}
            <div style={{ position: "relative" }}>
              <button ref={bellRef} onClick={() => { setShowNotifPanel(p => !p); if (!showNotifPanel) { setUnreadCount(0); setNotifications(prev => prev.map(n => ({ ...n, read: true }))); } }}
                style={{ width: 38, height: 38, borderRadius: "50%", border: `1.5px solid ${unreadCount > 0 ? C.brand : C.border}`, background: unreadCount > 0 ? C.brandBg : C.surface, display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", flexShrink: 0, position: "relative", transition: "all 0.2s", animation: bellShake ? "bellShake 0.6s ease" : "none", boxShadow: unreadCount > 0 ? "0 0 0 3px rgba(17,73,106,.12)" : "none" }}>
                <Icon n="bell" s={17} c={unreadCount > 0 ? C.brand : C.textSub} />
                {unreadCount > 0 && (
                  <span style={{ position: "absolute", top: -2, right: -2, width: 18, height: 18, borderRadius: "50%", background: "#EF4444", color: "#fff", fontSize: 10, fontWeight: 800, display: "flex", alignItems: "center", justifyContent: "center", border: "2px solid " + C.surface, animation: "notifPop 0.3s ease" }}>
                    {unreadCount > 9 ? "9+" : unreadCount}
                  </span>
                )}
              </button>

              {/* Notification Dropdown Panel */}
              {showNotifPanel && (
                <div ref={notifPanelRef} style={{ position: "absolute", top: 44, right: 0, width: 400, maxHeight: 520, background: C.surface, borderRadius: 14, border: `1px solid ${C.border}`, boxShadow: "0 20px 60px rgba(0,0,0,0.18), 0 4px 16px rgba(0,0,0,0.08)", zIndex: 5000, overflow: "hidden", animation: "notifSlideDown 0.25s cubic-bezier(0.16,1,0.3,1)" }}>
                  {/* Panel header */}
                  <div style={{ padding: "16px 20px 12px", borderBottom: `1px solid ${C.border}`, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                    <div>
                      <p style={{ fontWeight: 800, fontSize: 15, color: C.text }}>{T.notifications || "Notifications"}</p>
                      <p style={{ fontSize: 11, color: C.textSub, marginTop: 1 }}>{notifications.length} {T.totalLabel || "total"}{notifications.filter(n => !n.read).length > 0 ? ` · ${notifications.filter(n => !n.read).length} ${T.unreadLabel || "unread"}` : ""}</p>
                    </div>
                    {notifications.length > 0 && (
                      <button onClick={() => { setNotifications([]); setUnreadCount(0); }} style={{ fontSize: 11, color: C.brand, background: "none", border: "none", cursor: "pointer", fontWeight: 600, fontFamily: "inherit" }}>{T.clearAll || "Clear all"}</button>
                    )}
                  </div>

                  {/* Notifications list */}
                  <div style={{ overflowY: "auto", maxHeight: 440 }}>
                    {notifications.length === 0 ? (
                      <div style={{ padding: "40px 20px", textAlign: "center" }}>
                        <div style={{ width: 48, height: 48, borderRadius: "50%", background: C.bg, display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 12px" }}><Icon n="bell" s={22} c={C.textLight} /></div>
                        <p style={{ fontSize: 13, color: C.textLight, fontWeight: 500 }}>{T.noNotificationsYet || "No notifications yet"}</p>
                        <p style={{ fontSize: 11, color: C.textLight, marginTop: 4 }}>{T.notificationsEmptyHint || "Work-hours, break-overtime, leave, and attendance alerts will appear here"}</p>
                      </div>
                    ) : notifications.map((n, i) => {
                      const cfg = NOTIF_CONFIG[n.type] || NOTIF_CONFIG.risk_alert;
                      const ago = (() => {
                        const diff = Math.floor((Date.now() - new Date(n.time).getTime()) / 1000);
                        if (diff < 60) return T.justNow || "just now";
                        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
                        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
                        return new Date(n.time).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
                      })();
                      return (
                        <div key={n.id || i} style={{ display: "flex", gap: 12, padding: "12px 20px", borderBottom: `1px solid ${C.borderLight}`, background: n.read ? "transparent" : (isDark ? "rgba(17,73,106,0.06)" : "rgba(17,73,106,0.03)"), transition: "background 0.2s", cursor: "default", animation: !n.read ? `notifFadeIn 0.4s ease ${i * 0.05}s both` : "none" }}>
                          <div style={{ width: 34, height: 34, borderRadius: 10, background: cfg.bg, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                            <Icon n={cfg.icon} s={15} c={cfg.color} />
                          </div>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 2 }}>
                              <span style={{ fontSize: 12, fontWeight: 700, color: C.text }}>{n.title}</span>
                              <span style={{ fontSize: 10, fontWeight: 600, color: cfg.color, background: cfg.bg, padding: "1px 6px", borderRadius: 99 }}>{cfg.label}</span>
                            </div>
                            <p style={{ fontSize: 12, color: C.textSub, lineHeight: 1.4 }}>{n.message}</p>
                          </div>
                          <span style={{ fontSize: 10, color: C.textLight, whiteSpace: "nowrap", flexShrink: 0, marginTop: 2 }}>{ago}</span>
                          {!n.read && <div style={{ width: 7, height: 7, borderRadius: "50%", background: C.brand, flexShrink: 0, marginTop: 4 }} />}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>

            <div ref={profileMenuRef} style={{ position: "relative" }}>
              <button
                type="button"
                onClick={() => setProfileMenuOpen(o => !o)}
                aria-label="Open profile menu"
                aria-expanded={profileMenuOpen}
                style={{
                  display: "flex", alignItems: "center", gap: 6, padding: 0,
                  border: "none", background: "transparent", cursor: "pointer",
                  fontFamily: "inherit", outline: "none",
                }}>
                <span style={{
                  width: 38, height: 38, borderRadius: "50%",
                  background: adminUser.profile_picture
                    ? `url(${adminUser.profile_picture}) center/cover`
                    : "linear-gradient(135deg,#1F9D55,#15803D)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  color: "#fff", fontWeight: 800, fontSize: 14,
                  boxShadow: profileMenuOpen ? `0 0 0 3px ${C.brandBg}` : "0 2px 8px rgba(21,128,61,0.30)",
                  border: profileMenuOpen ? `2px solid ${C.brand}` : "2px solid transparent",
                  transition: "box-shadow .2s, border .2s, transform .2s",
                }}>
                  {!adminUser.profile_picture && initials(adminUser.name)}
                </span>
                <ChevronDown size={16} color={C.textSub} aria-hidden
                  style={{ transition: "transform .18s ease", transform: profileMenuOpen ? "rotate(180deg)" : "none" }} />
              </button>

              {profileMenuOpen && (
                <div style={{
                  position: "absolute", top: "calc(100% + 10px)", right: 0,
                  width: 240, background: C.surface, borderRadius: 10,
                  border: `1px solid ${C.border}`,
                  boxShadow: "0 12px 32px rgba(16,24,40,0.14), 0 2px 8px rgba(16,24,40,0.06)",
                  padding: "6px 0", zIndex: 1000,
                  fontFamily: "inherit",
                }}>
                  <div style={{ padding: "10px 16px 8px" }}>
                    <p style={{ fontSize: 10, fontWeight: 700, color: C.textLight, letterSpacing: 0.6, textTransform: "uppercase", marginBottom: 6 }}>
                      {T.organisation || "Organisation"}
                    </p>
                    <button
                      type="button"
                      title={ORGS.length > 1 ? undefined : (T.goToDashboard || "Go to dashboard")}
                      onClick={() => {
                        if (ORGS.length > 1) { setOrgPickerOpen(o => !o); return; }
                        setProfileMenuOpen(false);
                        navigate("/admin");
                      }}
                      aria-expanded={orgPickerOpen}
                      style={{
                        width: "100%",
                        display: "flex", alignItems: "center", justifyContent: "space-between",
                        padding: "8px 10px", borderRadius: 6,
                        border: `1px solid ${orgPickerOpen ? C.brand : C.border}`,
                        background: C.bg, cursor: "pointer",
                        fontFamily: "inherit",
                        textAlign: "left",
                      }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                        {workspaceLogo ? (
                          <img src={workspaceLogo} alt="" style={{ width: 32, height: 32, borderRadius: 6, objectFit: "cover", flexShrink: 0, border: `1px solid ${C.border}` }} />
                        ) : (
                          // No org-uploaded logo → show the official Temveris brand
                          // icon instead of a bare initial (per HR bug #4).
                          <div style={{ width: 32, height: 32, borderRadius: 6, background: C.brandBg, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, padding: 3, boxSizing: "border-box" }}>
                            <img src={TemverisIcon} alt="Temveris" style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: "block" }} />
                          </div>
                        )}
                        <span style={{ fontSize: 13, fontWeight: 600, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {workspaceName || activeOrg.name}
                        </span>
                      </div>
                      {ORGS.length > 1 && (
                        <span style={{
                          display: "inline-flex",
                          transition: "transform .18s ease",
                          transform: orgPickerOpen ? "rotate(180deg)" : "rotate(0deg)",
                        }}>
                          <Icon n="chevron_down" s={13} c={C.textSub} />
                        </span>
                      )}
                    </button>

                    {orgPickerOpen && ORGS.length > 1 && (
                      <div style={{
                        marginTop: 6, padding: 4, borderRadius: 6,
                        border: `1px solid ${C.border}`, background: C.surface,
                        maxHeight: 180, overflowY: "auto",
                      }}>
                        {ORGS.map(o => {
                          const active = o.id === activeOrgId;
                          return (
                            <button
                              key={o.id}
                              type="button"
                              onClick={() => { setActiveOrgId(o.id); setOrgPickerOpen(false); }}
                              style={{
                                width: "100%", display: "flex", alignItems: "center", gap: 8,
                                padding: "7px 8px", borderRadius: 4,
                                border: "none", background: active ? C.brandBg : "transparent",
                                color: active ? C.brand : C.text,
                                fontWeight: active ? 700 : 500, fontSize: 12,
                                cursor: "pointer", fontFamily: "inherit", textAlign: "left",
                              }}
                              onMouseEnter={(e) => { if (!active) e.currentTarget.style.background = C.bg; }}
                              onMouseLeave={(e) => { if (!active) e.currentTarget.style.background = "transparent"; }}>
                              <Icon n="users" s={12} c={active ? C.brand : C.textSub} />
                              <span style={{ flex: 1, minWidth: 0 }}>
                                <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{o.name}</span>
                                <span style={{ display: "block", fontSize: 10, fontWeight: 500, color: C.textSub, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{o.tagline}</span>
                              </span>
                              {active && <Icon n="check" s={12} c={C.brand} />}
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>

                  <div style={{ height: 1, background: C.borderLight, margin: "6px 0" }} />

                  <button
                    type="button"
                    onClick={() => { setProfileMenuOpen(false); navigate("/profile"); }}
                    style={{
                      width: "100%", textAlign: "left", padding: "10px 16px",
                      border: "none", background: "transparent",
                      color: C.brand, fontWeight: 700, fontSize: 14,
                      cursor: "pointer", fontFamily: "inherit",
                    }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = C.brandBg; }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}>
                    {T.myProfile || "My Profile"}
                  </button>

                  <button
                    type="button"
                    onClick={() => { setProfileMenuOpen(false); navigate("/account-settings"); }}
                    style={{
                      width: "100%", textAlign: "left", padding: "10px 16px",
                      border: "none", background: "transparent",
                      color: C.text, fontWeight: 600, fontSize: 14,
                      cursor: "pointer", fontFamily: "inherit",
                    }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = C.brandBg; }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}>
                    {T.accountSettings || "Account Settings"}
                  </button>

                  <button
                    type="button"
                    onClick={() => {
                      setProfileMenuOpen(false);
                      logout({ redirect: false });
                      navigate("/", { replace: true });
                    }}
                    style={{
                      width: "100%", textAlign: "left", padding: "10px 16px",
                      border: "none", background: "transparent",
                      color: C.text, fontWeight: 600, fontSize: 14,
                      cursor: "pointer", fontFamily: "inherit",
                    }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = C.bg; }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}>
                    {T.signOut || "Sign Out"}
                  </button>
                </div>
              )}
            </div>
          </header>

          <div style={{ flex: 1, overflowY: "auto", padding: "28px 28px 40px" }}>

            {/* ── Employee-data freshness notice ──────────────────────────────
                Sits in the shared content container, above the tab switch, so
                it is visible on every tab that renders worker data (Overview,
                Team Directory, Timesheets, ...) rather than only one of them.

                Two states, chosen by whether we still hold data:
                  STALE   — last known-good data is on screen but out of date
                  NO DATA — nothing to show at all
                Never shows the underlying error: no status codes, no response
                bodies, no exception text. */}
            {employeesLoadFailed && (
              <div
                role="status"
                aria-live="polite"
                style={{
                  display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
                  background: C.amberBg, border: `1.5px solid ${C.amberBdr}`,
                  borderRadius: C.r, padding: "10px 16px", marginBottom: 20,
                }}
              >
                {/* Icon is decorative — the sentence carries the meaning, so
                    the state is never conveyed by colour or glyph alone.
                    Text uses C.text rather than C.amber: the amber tokens are
                    not theme-aware (`amber` is a fixed #B54708), which reads
                    fine on the light amber tint but poorly on the dark one. */}
                <span aria-hidden="true" style={{ display: "inline-flex" }}>
                  <Icon n="alert" s={16} c={C.amber} />
                </span>
                <span style={{ fontSize: 13, fontWeight: 600, color: C.text, flex: "1 1 280px" }}>
                  {employees.length > 0
                    ? (T.employeeDataStale
                       || "Couldn't refresh worker data — showing the last information we received.")
                    : (T.employeeDataUnavailable
                       || "Couldn't load worker data.")}
                </span>
                <button
                  type="button"
                  onClick={retryEmployees}
                  disabled={employeesRetrying}
                  style={{
                    padding: "6px 16px", borderRadius: 8,
                    border: `1.5px solid ${C.amberBdr}`, background: C.surface,
                    color: C.text, fontSize: 12, fontWeight: 700,
                    cursor: employeesRetrying ? "default" : "pointer",
                    opacity: employeesRetrying ? 0.6 : 1,
                    fontFamily: "inherit",
                  }}
                >
                  {employeesRetrying ? (T.retrying || "Retrying…") : (T.retry || "Retry")}
                </button>
              </div>
            )}

            {tab === "overview" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>

                {/* Historical mode banner */}
                {historyData && (
                  <div style={{ background: "#EFF6FF", border: "1.5px solid #93C5FD", borderRadius: C.r, padding: "12px 18px", display: "flex", alignItems: "center", gap: 12 }}>
                    <Icon n="calendar" s={18} c="#2563EB" />
                    <span style={{ fontWeight: 700, fontSize: 14, color: "#1D4ED8" }}>{T.historicalView || "Historical View"}: {activeDateLabel}</span>
                    <span style={{ fontSize: 13, color: "#3B82F6" }}>— {T.showingAttendanceForDate || "showing attendance data for this date"}</span>
                    <div style={{ flex: 1 }} />
                    <button onClick={() => { histReqSeq.current += 1; setHistoryData(null); setHistoryLoading(false); setDateRange(null); setActiveDateLabel(T.today || "Today"); setHistStatusFilter("all"); }}
                      style={{ padding: "5px 14px", borderRadius: 8, border: "1.5px solid #93C5FD", background: "#fff", color: "#2563EB", fontSize: 12, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" }}>
                      {T.backToLive || "Back to Live"}
                    </button>
                  </div>
                )}
                {historyLoading && (
                  <div style={{ background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`, padding: "20px", textAlign: "center", color: C.textSub, fontSize: 13 }}>
                    {T.loadingHistoricalData || "Loading historical data for"} {activeDateLabel}…
                  </div>
                )}

                {/* ══ LIVE WORKFORCE OVERVIEW ══
                    Main column (title, status cards, filters, worker list) beside
                    a right column (today's summary, distribution, quick actions).
                    Every number comes from the same `statusCounts` / filtered
                    list the rest of this page uses; OverviewLive only draws. */}
                <style>{OVERVIEW_CSS}</style>
                <div className="zt-ov-layout">
                  <div className="zt-ov-main">
                    <OverviewTitle C={C} T={T} wsStatus={wsStatus} historical={!!historyData} dateLabel={activeDateLabel} />

                    <OverviewStatCards C={C} T={T}
                      counts={statusCounts} total={activeTotal} requiresAttention={requiresAttention}
                      history={historyData ? (historyData.summary || {}) : null}
                      onOpen={(filter, hist) => {
                        if (hist) { setHistStatusFilter(filter); } else { setStatusFilter(filter); }
                        // Drive through the URL — setTab() alone would leave
                        // ?tab=overview in place, which makes the sidebar's
                        // "already there" check early-return and the user
                        // can't get back to Overview without a hard refresh.
                        navigate("/admin?tab=myteam");
                      }} />

                    <OverviewFilterBar C={C} T={T} isDark={isDark}
                      statusTabs={statusTabs} statusFilter={statusFilter} setStatusFilter={setStatusFilter}
                      teams={teams} teamFilter={teamFilter} setTeamFilter={setTeamFilter}
                      empTypeFilter={empTypeFilter} setEmpTypeFilter={setEmpTypeFilter} empTypeCounts={empTypeCounts}
                      viewMode={viewMode} setViewMode={setViewMode} />

                    {loading && filteredEmps.length === 0 ? (
                      // Skeleton instead of a full-page spinner — paints
                      // instantly so the worker section never looks empty while
                      // /employees/team is in flight.
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 14 }}>
                        {[0, 1, 2].map((i) => (
                          <div key={i} style={{ ...card(), padding: "18px 18px", display: "flex", alignItems: "center", gap: 12, opacity: 0.6 }}>
                            <div style={{ width: 44, height: 44, borderRadius: "50%", background: C.borderLight, animation: "ztPulse 1.4s ease-in-out infinite", animationDelay: `${i * 0.12}s` }} />
                            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
                              <div style={{ height: 12, width: "60%", borderRadius: 4, background: C.borderLight, animation: "ztPulse 1.4s ease-in-out infinite", animationDelay: `${i * 0.12}s` }} />
                              <div style={{ height: 10, width: "40%", borderRadius: 4, background: C.borderLight, animation: "ztPulse 1.4s ease-in-out infinite", animationDelay: `${i * 0.12 + 0.1}s` }} />
                            </div>
                          </div>
                        ))}
                      </div>
                    ) : filteredEmps.length === 0 ? (
                      // Two different empties: the org has workers but the
                      // filters hide them all (common: "Working (0)" before
                      // anyone clocks in), or there are no workers at all.
                      (() => {
                        const filtered = activeTotal > 0 && empTypeFilter !== "archived";
                        return (
                          <div data-testid="ov-empty" style={{ ...card(), padding: "56px 40px", textAlign: "center" }}>
                            <div style={{ width: 64, height: 64, borderRadius: 20, background: C.bg, display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}>
                              <Icon n="users" s={28} c={C.textLight} />
                            </div>
                            <p style={{ fontSize: 17, color: C.text, fontWeight: 700 }}>
                              {filtered ? (T.noWorkersMatchFilters || "No workers match these filters") : (T.noWorkersFound || "No workers found")}
                            </p>
                            <p style={{ fontSize: 13, color: C.textSub, maxWidth: 360, margin: "6px auto 0", lineHeight: 1.5 }}>
                              {filtered
                                ? (T.noWorkersMatchFiltersHint || "Nobody is in this status or matches this search right now. Clear the filters to see everyone.")
                                : (T.tryAdjustingFiltersMsg || "Try adjusting your filters or add new workers to get started")}
                            </p>
                            <button onClick={() => {
                                if (!filtered) { setShowAddUser(true); return; }
                                setStatusFilter("all"); setTeamFilter("all"); setEmpTypeFilter("all"); setSearch("");
                              }}
                              style={{ marginTop: 20, padding: "10px 24px", borderRadius: 10, border: "none", background: navyFor(isDark), color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>
                              {filtered ? (T.showAllWorkers || "Show all workers") : (T.addWorker || "Add Worker")}
                            </button>
                          </div>
                        );
                      })()
                    ) : viewMode === "timeline" ? (
                      <div style={card()}><TimelineView employees={filteredEmps} /></div>
                    ) : viewMode === "list" ? (
                      <OverviewWorkerTable C={C} T={T} isDark={isDark} emps={pagedEmps}
                        onView={setViewEmp} onEdit={setEditEmp} onDelete={setDeleteEmp} />
                    ) : (
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 16 }}>
                        {pagedEmps.map(emp => (
                          <EmpCard key={emp.id} emp={emp} onView={setViewEmp} onEdit={setEditEmp} onDelete={setDeleteEmp} onToggleType={handleToggleType} />
                        ))}
                      </div>
                    )}
                    {/* ══ PAGINATION ══ */}
                    {empTotalPages() > 1 && (() => {
                      const total = empTotalPages();
                      const pages = [];
                      // Always show first, last, current ±2, with ellipsis gaps
                      const visible = new Set([1, total, empPage, empPage - 1, empPage - 2, empPage + 1, empPage + 2].filter(p => p >= 1 && p <= total));
                      const sorted = [...visible].sort((a, b) => a - b);
                      return (
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 6, paddingTop: 8 }}>
                          {/* Prev */}
                          <button onClick={() => setEmpPage(p => Math.max(1, p - 1))} disabled={empPage === 1}
                            style={{ padding: "7px 14px", borderRadius: C.rSm, border: `1.5px solid ${C.border}`, background: C.surface, color: empPage === 1 ? C.textLight : C.text, cursor: empPage === 1 ? "default" : "pointer", fontWeight: 600, fontSize: 13, fontFamily: "inherit", transition: "all .15s", opacity: empPage === 1 ? 0.4 : 1 }}>
                            {T.adminDashboardPrevPage || "← Prev"}
                          </button>
                          {sorted.map((p, i) => {
                            const prev = sorted[i - 1];
                            return (
                              <React.Fragment key={p}>
                                {prev && p - prev > 1 && <span style={{ color: C.textLight, fontSize: 13, padding: "0 2px" }}>…</span>}
                                <button onClick={() => setEmpPage(p)}
                                  style={{ minWidth: 36, height: 36, borderRadius: C.rSm, border: `1.5px solid ${empPage === p ? C.brand : C.border}`, background: empPage === p ? C.brand : C.surface, color: empPage === p ? "#fff" : C.text, fontWeight: empPage === p ? 700 : 500, fontSize: 13, cursor: "pointer", fontFamily: "inherit", transition: "all .15s" }}>
                                  {p}
                                </button>
                              </React.Fragment>
                            );
                          })}
                          {/* Next */}
                          <button onClick={() => setEmpPage(p => Math.min(total, p + 1))} disabled={empPage === total}
                            style={{ padding: "7px 14px", borderRadius: C.rSm, border: `1.5px solid ${C.border}`, background: C.surface, color: empPage === total ? C.textLight : C.text, cursor: empPage === total ? "default" : "pointer", fontWeight: 600, fontSize: 13, fontFamily: "inherit", transition: "all .15s", opacity: empPage === total ? 0.4 : 1 }}>
                            {T.adminDashboardNextPage || "Next →"}
                          </button>
                          <span style={{ fontSize: 12, color: C.textSub, marginLeft: 8 }}>
                            {(empPage - 1) * EMP_PAGE_SIZE + 1}–{Math.min(empPage * EMP_PAGE_SIZE, filteredEmps.length)} {T.adminDashboardOfWord || "of"} {filteredEmps.length}
                          </span>
                        </div>
                      );
                    })()}
                  </div>

                  <aside className="zt-ov-side" aria-label={T.todaysSummary || "Today's Summary"}>
                    <OverviewSidePanel C={C} T={T}
                      counts={statusCounts} total={activeTotal}
                      history={historyData ? (historyData.summary || {}) : null}
                      dateLabel={activeDateLabel}
                      onAddWorker={() => setShowAddUser(true)}
                      onNavigate={navigate} />
                  </aside>
                </div>

                {/* ══ KPI SUMMARY CARDS (interactive — drill-down + sparkline) ══ */}
                {analytics && (() => {
                  const att   = analytics.attendance || {};
                  const total = att.total || 0;
                  const presentPct = total ? Math.round(((att.present || 0) / total) * 100) : 0;
                  const workingPct = (att.present || 0) ? Math.round(((att.working || 0) / (att.present || 1)) * 100) : 0;
                  const daily = analytics.daily_hours || [];
                  // Sparkline points (per-employee daily avg hours) — feeds the
                  // Avg Hours card so the card hints at the trend, not just a
                  // single number. Falls back to a flat line for empty data.
                  const sparkSeries = daily
                    .map(d => total ? ((d.worked || 0) / total) : 0)
                    .slice(-14);
                  const cards = [
                    {
                      label: T.totalEmployees || "Total Employees",
                      val: total,
                      icon: "users", color: "#11496A", bg: "#E7F0F6",
                      sub: `${att.on_leave || 0} on leave today`,
                      action: { kind: "drill", filter: "all", hint: T.adminDashboardViewAllWorkersHint || "View all workers →" },
                    },
                    {
                      label: T.presentToday || "Present Today",
                      val: att.present || 0,
                      icon: "check", color: "#2F8F5C", bg: "#F0FDF4",
                      sub: `${presentPct}% attendance · ${att.absent || 0} absent`,
                      progress: presentPct,
                      action: { kind: "drill", filter: "present", hint: T.adminDashboardSeeWhosInHint || "See who's in →" },
                    },
                    {
                      label: T.currentlyWorking || "Currently Working",
                      val: att.working || 0,
                      icon: "activity", color: "#7C3AED", bg: "#F5F3FF",
                      sub: `${att.on_break || 0} on break · ${workingPct}% of present`,
                      progress: workingPct,
                      action: { kind: "drill", filter: "working", hint: T.adminDashboardOpenLiveGridHint || "Open live grid →" },
                    },
                    {
                      label: T.avgHoursPerEmployee || "Avg Hours / Employee",
                      val: `${analytics.summary.avg_hours_per_employee}h`,
                      icon: "clock", color: "#B54708", bg: "#FFFAEB",
                      sub: `Over the last ${analytics.summary.days} days`,
                      sparkline: sparkSeries,
                      action: { kind: "scroll", target: "zt-daily-hours-chart", hint: T.adminDashboardViewDailyTrendHint || "View daily trend →" },
                    },
                  ];

                  const handleCardAction = (action) => {
                    if (!action) return;
                    if (action.kind === "drill") {
                      setStatusFilter(action.filter === "all" ? "all" : action.filter);
                      // Mirror the Overview status-card pattern: navigate via
                      // URL so the sidebar's "already there" check doesn't
                      // early-return and the back button still works.
                      navigate("/admin?tab=myteam");
                      return;
                    }
                    if (action.kind === "scroll") {
                      const el = document.getElementById(action.target);
                      if (el && typeof el.scrollIntoView === "function") {
                        el.scrollIntoView({ behavior: "smooth", block: "start" });
                      }
                    }
                  };

                  return (
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16 }}>
                      {cards.map(s => (
                        <button
                          key={s.label}
                          type="button"
                          onClick={() => handleCardAction(s.action)}
                          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleCardAction(s.action); } }}
                          className="zt-kpi-card"
                          style={{
                            background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`,
                            padding: "22px 22px", display: "flex", flexDirection: "column", gap: 14,
                            boxShadow: C.shadow, transition: "all .25s cubic-bezier(.34,1.56,.64,1)",
                            cursor: "pointer", textAlign: "left", fontFamily: "inherit",
                            position: "relative", overflow: "hidden", color: "inherit",
                            outline: "none",
                          }}
                          onMouseEnter={e => {
                            e.currentTarget.style.boxShadow = C.shadowMd;
                            e.currentTarget.style.transform = "translateY(-3px)";
                            e.currentTarget.style.borderColor = s.color;
                            const hint = e.currentTarget.querySelector(".zt-kpi-hint");
                            if (hint) { hint.style.opacity = "1"; hint.style.transform = "translateX(0)"; }
                          }}
                          onMouseLeave={e => {
                            e.currentTarget.style.boxShadow = C.shadow;
                            e.currentTarget.style.transform = "none";
                            e.currentTarget.style.borderColor = C.border;
                            const hint = e.currentTarget.querySelector(".zt-kpi-hint");
                            if (hint) { hint.style.opacity = "0"; hint.style.transform = "translateX(6px)"; }
                          }}
                          onFocus={e => { e.currentTarget.style.boxShadow = `0 0 0 3px ${s.color}33, ${C.shadowMd}`; e.currentTarget.style.borderColor = s.color; }}
                          onBlur={e => { e.currentTarget.style.boxShadow = C.shadow; e.currentTarget.style.borderColor = C.border; }}
                        >
                          {/* Accent stripe on top — picks up the card's color */}
                          <span aria-hidden style={{ position: "absolute", inset: "0 0 auto 0", height: 3, background: `linear-gradient(90deg, ${s.color}, ${s.color}66)`, opacity: .9 }} />

                          <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                            <div style={{ width: 56, height: 56, borderRadius: 14, background: s.bg, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                              <Icon n={s.icon} s={26} c={s.color} />
                            </div>
                            <div style={{ minWidth: 0, flex: 1 }}>
                              <p style={{ fontSize: 32, fontWeight: 900, color: C.text, lineHeight: 1, letterSpacing: "-1px" }}>{s.val}</p>
                              <p style={{ fontSize: 12, color: C.textSub, fontWeight: 700, marginTop: 6, letterSpacing: ".3px", textTransform: "uppercase" }}>{s.label}</p>
                            </div>
                          </div>

                          {/* Per-card secondary content: progress bar, sparkline, or plain sub */}
                          {typeof s.progress === "number" ? (
                            <div>
                              <div style={{ height: 6, borderRadius: 999, background: C.borderLight, overflow: "hidden" }}>
                                <div style={{ height: "100%", width: `${Math.min(100, Math.max(0, s.progress))}%`, background: `linear-gradient(90deg, ${s.color}, ${s.color}cc)`, transition: "width .5s ease" }} />
                              </div>
                              <p style={{ fontSize: 11, color: C.textSub, marginTop: 6 }}>{s.sub}</p>
                            </div>
                          ) : s.sparkline ? (
                            <div>
                              <KpiSparkline values={s.sparkline} color={s.color} />
                              <p style={{ fontSize: 11, color: C.textSub, marginTop: 6 }}>{s.sub}</p>
                            </div>
                          ) : (
                            <p style={{ fontSize: 11, color: C.textSub }}>{s.sub}</p>
                          )}

                          {/* Hover affordance — fades in to telegraph the drill-down */}
                          <span className="zt-kpi-hint" style={{
                            position: "absolute", right: 16, bottom: 14,
                            fontSize: 11, fontWeight: 700, color: s.color,
                            opacity: 0, transform: "translateX(6px)",
                            transition: "opacity .2s, transform .2s",
                            pointerEvents: "none",
                          }}>{s.action?.hint || (T.adminDashboardOpenHint || "Open →")}</span>
                        </button>
                      ))}
                    </div>
                  );
                })()}

                {/* ══ CHARTS ROW 1: Daily Hours + Hours by Department ══ */}
                {analytics && (
                  <div style={{ display: "grid", gridTemplateColumns: "3fr 2fr", gap: 16 }}>
                    {/* Daily Worked Hours Bar Chart — interactive */}
                    {(() => {
                      const { daily, totalWorked, totalBreak, peakIdx } = dailyHoursAgg;
                      return (
                        <div id="zt-daily-hours-chart" style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "20px 24px", position: "relative", overflow: "hidden", scrollMarginTop: 24 }}>
                          {/* subtle gradient header sheen */}
                          <div aria-hidden style={{ position: "absolute", inset: 0, pointerEvents: "none", background: "radial-gradient(circle at 100% 0%, rgba(34,197,94,0.06), transparent 50%)" }} />
                          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, position: "relative" }}>
                            <div>
                              <p style={{ fontWeight: 700, fontSize: 15, color: C.text }}>{T.loggedHours || "Logged Hours"}</p>
                              <p style={{ fontSize: 12, color: C.textSub }}>
                                {T.dailyWorkHours || "Daily work hours across all employees"}
                                <span style={{ marginLeft: 8, color: C.textLight }}>·</span>
                                <span style={{ marginLeft: 8, color: C.text, fontWeight: 700 }}>{totalWorked.toFixed(0)}h {T.worked || "worked"}</span>
                                <span style={{ marginLeft: 6, color: C.textLight }}>· {totalBreak.toFixed(0)}h {T.breakLabel || "break"}</span>
                              </p>
                            </div>
                            <div style={{ display: "flex", border: `1px solid ${C.border}`, borderRadius: 8, overflow: "hidden", background: C.bg }}>
                              {[7, 14, 30].map(d => (
                                <button key={d} onClick={() => setAnalyticsDays(d)}
                                  style={{
                                    padding: "5px 12px", border: "none",
                                    background: analyticsDays === d ? "linear-gradient(135deg,#11496A,#22C55E)" : "transparent",
                                    color: analyticsDays === d ? "#fff" : C.textSub,
                                    fontWeight: 700, fontSize: 11, cursor: "pointer", fontFamily: "inherit",
                                    transition: "all .2s",
                                  }}>{d}d</button>
                              ))}
                            </div>
                          </div>

                          {/* Clickable legend chips — toggle each series. */}
                          <div style={{ display: "flex", gap: 8, marginBottom: 6, flexWrap: "wrap", position: "relative" }}>
                            {[
                              { key: "worked", label: T.worked || "Worked", color: "#11496A", grad: "ztBarWorkedGrad" },
                              { key: "break",  label: T.breakLabel || "Break", color: "#3B82F6", grad: "ztBarBreakGrad" },
                            ].map(s => {
                              const on = dailyHoursSeries[s.key];
                              return (
                                <button key={s.key} type="button"
                                  onClick={() => setDailyHoursSeries(v => ({ ...v, [s.key]: !v[s.key] }))}
                                  aria-pressed={on}
                                  style={{
                                    display: "inline-flex", alignItems: "center", gap: 6,
                                    fontSize: 11, fontWeight: 700,
                                    padding: "4px 10px", borderRadius: 99,
                                    background: on ? `${s.color}15` : "transparent",
                                    color: on ? s.color : C.textLight,
                                    border: `1px solid ${on ? s.color : C.border}`,
                                    cursor: "pointer", fontFamily: "inherit",
                                    textDecoration: on ? "none" : "line-through",
                                    transition: "all .15s",
                                  }}>
                                  <span style={{ width: 8, height: 8, borderRadius: 2, background: s.color, opacity: on ? 1 : 0.4 }} />
                                  {s.label}
                                </button>
                              );
                            })}
                          </div>

                          <Suspense fallback={<div style={{ height: 220 }} />}>
                            <DailyHoursChart C={C} T={T} daily={daily} dailyHoursHover={dailyHoursHover} dailyHoursSeries={dailyHoursSeries} isDark={isDark} peakIdx={peakIdx} setDailyHoursHover={setDailyHoursHover} />
                          </Suspense>

                          {dailyHoursHover >= 0 && daily[dailyHoursHover] && (
                            <p style={{ margin: "4px 0 0", fontSize: 11, color: C.textSub, textAlign: "center", fontWeight: 600 }}>
                              {new Date(daily[dailyHoursHover].date + "T00:00:00").toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "short" })}
                              <span style={{ marginLeft: 8, color: "#22C55E", fontWeight: 800 }}>{(daily[dailyHoursHover].worked || 0).toFixed(1)}h {T.worked || "worked"}</span>
                              <span style={{ marginLeft: 8, color: "#3B82F6", fontWeight: 800 }}>{(daily[dailyHoursHover].break || 0).toFixed(1)}h {T.breakLabel || "break"}</span>
                              {dailyHoursHover === peakIdx && (
                                <span style={{ marginLeft: 8, color: "#F97316", fontWeight: 800 }}>🏆 {T.peakDay || "Peak day"}</span>
                              )}
                            </p>
                          )}
                        </div>
                      );
                    })()}

                    {/* Hours by Department Donut */}
                    <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "20px 24px" }}>
                      <p style={{ fontWeight: 700, fontSize: 15, color: C.text, marginBottom: 4 }}>{T.hoursByDepartment || "Hours by Department"}</p>
                      <p style={{ fontSize: 12, color: C.textSub, marginBottom: 12 }}>{analytics.dept_hours.length} department{analytics.dept_hours.length !== 1 ? "s" : ""}</p>
                      {analytics.dept_hours.length === 0 ? (
                        <div style={{ textAlign: "center", padding: "40px 0", color: C.textSub, fontSize: 13 }}>{T.noDataYet || "No data yet"}</div>
                      ) : (
                        <>
                          <Suspense fallback={<div style={{ height: 170 }} />}>
                            <DeptDonut C={C} analytics={analytics} hoveredDeptIdx={hoveredDeptIdx} setHoveredDeptIdx={setHoveredDeptIdx} setStatusFilter={setStatusFilter} setTeamFilter={setTeamFilter} teamFilter={teamFilter} />
                          </Suspense>
                          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}>
                            {analytics.dept_hours.slice(0, 5).map((d, i) => {
                              const isActive = teamFilter === d.name;
                              return (
                                <button
                                  key={d.name}
                                  type="button"
                                  onClick={() => { setTeamFilter(isActive ? "all" : String(d.name)); setStatusFilter("all"); }}
                                  style={{
                                    display: "flex", alignItems: "center", gap: 8, padding: "4px 8px",
                                    background: isActive ? C.brandBg : "transparent",
                                    border: "none", borderRadius: 6,
                                    cursor: "pointer", fontFamily: "inherit", textAlign: "left",
                                    transition: "background .14s ease",
                                  }}
                                  onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = C.bg; }}
                                  onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = "transparent"; }}
                                >
                                  <span style={{ width: 10, height: 10, borderRadius: "50%", background: ["#EF4444","#F59E0B","#22C55E","#3B82F6","#8B5CF6","#EC4899","#14B8A6","#F97316"][i % 8], flexShrink: 0 }} />
                                  <span style={{ flex: 1, fontSize: 12, fontWeight: isActive ? 700 : 600, color: C.text }}>{d.name}</span>
                                  <span style={{ fontSize: 12, fontWeight: 700, color: C.textSub }}>{d.hours.toFixed(1)}h</span>
                                </button>
                              );
                            })}
                          </div>
                          {teamFilter !== "all" && (
                            <button
                              type="button"
                              onClick={() => setTeamFilter("all")}
                              style={{
                                marginTop: 8, padding: "5px 10px",
                                background: "transparent", border: `1px solid ${C.border}`,
                                borderRadius: 6, fontSize: 11, fontWeight: 600, color: C.textSub,
                                cursor: "pointer", fontFamily: "inherit", width: "100%",
                              }}
                            >
                              {T.clearFilter || "Clear filter"} ({teamFilter})
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                )}

                {/* ══ CHARTS ROW 2: Employee Comparison + Attendance + Leave ══ */}
                {analytics && (
                  <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr", gap: 16 }}>
                    {/* Employee Work Hours Comparison */}
                    <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "20px 24px" }}>
                      <p style={{ fontWeight: 700, fontSize: 15, color: C.text, marginBottom: 4 }}>{T.topEmployeesByHours || "Top Employees by Hours"}</p>
                      <p style={{ fontSize: 12, color: C.textSub, marginBottom: 16 }}>Last {analyticsDays} days</p>
                      {analytics.emp_hours.length === 0 ? (
                        <div style={{ textAlign: "center", padding: "40px 0", color: C.textSub, fontSize: 13 }}>{T.noDataYet || "No data yet"}</div>
                      ) : (
                        <>
                          <Suspense fallback={<div style={{ height: 220 }} />}>
                            <TopEmployeesChart C={C} analytics={analytics} employees={employees} hoveredEmpIdx={hoveredEmpIdx} isDark={isDark} setEditEmp={setEditEmp} setHoveredEmpIdx={setHoveredEmpIdx} />
                          </Suspense>
                          <p style={{ margin: "8px 0 0", fontSize: 10.5, color: C.textLight, textAlign: "center" }}>
                            🏆 {T.clickBarToEdit || "Click a bar to open that worker's profile"}
                          </p>
                        </>
                      )}
                    </div>

                    {/* Attendance Donut */}
                    <div style={{
                      position: "relative",
                      background: `linear-gradient(135deg, ${C.surface} 0%, ${C.surface} 60%, rgba(34,197,94,0.06) 100%)`,
                      borderRadius: 16, border: `1px solid ${C.border}`, padding: "20px 24px",
                      overflow: "hidden",
                    }}>
                      {/* soft animated halo behind donut */}
                      <div aria-hidden style={{
                        position: "absolute", top: "30%", left: "50%", transform: "translate(-50%, -50%)",
                        width: 180, height: 180, borderRadius: "50%",
                        background: "radial-gradient(circle, rgba(34,197,94,0.18), rgba(34,197,94,0) 70%)",
                        pointerEvents: "none",
                        animation: "ztGlowPulse 3.5s ease-in-out infinite",
                      }} />
                      <p style={{ fontWeight: 700, fontSize: 15, color: C.text, marginBottom: 4, position: "relative" }}>{T.attendanceToday || "Attendance Today"}</p>
                      <p style={{ fontSize: 12, color: C.textSub, marginBottom: 12, position: "relative" }}>{analytics.attendance.total} {T.adminDashboardEmployeesWord || "employees"}</p>
                      <div style={{ position: "relative" }}>
                      <Suspense fallback={<div style={{ height: 140 }} />}>
                        <AttendanceDonut C={C} analytics={analytics} hoveredAttIdx={hoveredAttIdx} setHoveredAttIdx={setHoveredAttIdx} setStatusFilter={setStatusFilter} statusFilter={statusFilter} />
                      </Suspense>
                      {/* Center-overlay total — shows working count + percentage at a glance.
                          Hidden while a slice is hovered so it doesn't pile on top of
                          the activeShape's name / count / % labels (was the source of
                          the "77% PRESENT / Working / Working:30" stacked-text bug). */}
                      {hoveredAttIdx == null && (
                        <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", pointerEvents: "none" }}>
                          <p style={{ fontSize: 22, fontWeight: 900, color: "#22C55E", margin: 0, lineHeight: 1, letterSpacing: "-.5px", textShadow: "0 0 16px rgba(34,197,94,0.45)" }}>
                            {analytics.attendance.total > 0
                              ? Math.round((analytics.attendance.working / analytics.attendance.total) * 100)
                              : 0}%
                          </p>
                          <p style={{ fontSize: 9, color: C.textSub, margin: "2px 0 0", textTransform: "uppercase", letterSpacing: ".7px", fontWeight: 700 }}>
                            {T.adminDashboardPresentWord || "present"}
                          </p>
                        </div>
                      )}
                      </div>
                      <style>{`@keyframes ztGlowPulse{0%,100%{opacity:.7;transform:translate(-50%,-50%) scale(1)}50%{opacity:1;transform:translate(-50%,-50%) scale(1.05)}}`}</style>
                      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                        {[
                          { label: T.working || "Working",   val: analytics.attendance.working,   color: "#22C55E", key: "working"  },
                          { label: T.onBreak || "On Break",  val: analytics.attendance.on_break,  color: "#F59E0B", key: "on_break" },
                          { label: T.absent  || "Absent",    val: analytics.attendance.absent,    color: "#EF4444", key: "absent"   },
                        ].map(r => {
                          const isActive = statusFilter === r.key;
                          return (
                            <button
                              key={r.label}
                              type="button"
                              onClick={() => setStatusFilter(isActive ? "all" : r.key)}
                              style={{
                                display: "flex", alignItems: "center", justifyContent: "space-between",
                                background: isActive ? C.brandBg : "transparent", border: "none",
                                padding: "4px 8px", borderRadius: 6,
                                cursor: "pointer", fontFamily: "inherit",
                                transition: "background .14s ease",
                              }}
                              onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = C.bg; }}
                              onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = "transparent"; }}
                            >
                              <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: C.textSub }}>
                                <span style={{ width: 8, height: 8, borderRadius: "50%", background: r.color }} />
                                {r.label}
                              </span>
                              <span style={{ fontSize: 13, fontWeight: 800, color: C.text }}>{r.val}</span>
                            </button>
                          );
                        })}
                      </div>
                    </div>

                    {/* Leave Summary */}
                    <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "20px 24px" }}>
                      <p style={{ fontWeight: 700, fontSize: 15, color: C.text, marginBottom: 4 }}>{T.leaveRequestsTitle || "Leave Requests"}</p>
                      <p style={{ fontSize: 12, color: C.textSub, marginBottom: 16 }}>Last {analyticsDays} days</p>
                      {(() => {
                        const rows = [
                          { label: T.approved || "Approved", val: analytics.leave_summary.approved, color: "#22C55E", soft: "#86EFAC", deep: "#15803D", bg: "rgba(34,197,94,0.10)",  status: "approved", icon: "check" },
                          { label: T.pending  || "Pending",  val: analytics.leave_summary.pending,  color: "#F59E0B", soft: "#FCD34D", deep: "#B45309", bg: "rgba(245,158,11,0.10)", status: "pending",  icon: "clock" },
                          { label: T.rejected || "Rejected", val: analytics.leave_summary.rejected, color: "#EF4444", soft: "#FCA5A5", deep: "#B91C1C", bg: "rgba(239,68,68,0.10)",  status: "rejected", icon: "x"     },
                        ];
                        const total = rows.reduce((s, r) => s + (r.val || 0), 0) || 1;
                        return (
                          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                            {rows.map(r => {
                              const pct = Math.round(((r.val || 0) / total) * 100);
                              return (
                                <button
                                  key={r.label}
                                  type="button"
                                  onClick={() => navigate(`/admin/leave?status=${r.status}`)}
                                  style={{
                                    position: "relative", overflow: "hidden",
                                    display: "flex", flexDirection: "column", gap: 8,
                                    padding: "12px 14px",
                                    background: `
                                      radial-gradient(circle at 100% 0%, ${r.color}22, transparent 50%),
                                      linear-gradient(135deg, ${r.bg} 0%, ${r.color}1F 100%)
                                    `,
                                    borderRadius: 12,
                                    border: `1px solid ${r.color}55`,
                                    cursor: "pointer", fontFamily: "inherit",
                                    transition: "transform .18s ease, box-shadow .18s ease, border-color .2s",
                                    textAlign: "left",
                                  }}
                                  onMouseEnter={e => {
                                    e.currentTarget.style.transform = "translateY(-2px)";
                                    e.currentTarget.style.boxShadow = `0 12px 28px -8px ${r.color}77, 0 0 0 1px ${r.color}66`;
                                    e.currentTarget.style.borderColor = r.color;
                                  }}
                                  onMouseLeave={e => {
                                    e.currentTarget.style.transform = "translateY(0)";
                                    e.currentTarget.style.boxShadow = "none";
                                    e.currentTarget.style.borderColor = `${r.color}55`;
                                  }}
                                >
                                  {/* corner sparkle */}
                                  <span aria-hidden style={{
                                    position: "absolute", top: -20, right: -20, width: 60, height: 60, borderRadius: "50%",
                                    background: `radial-gradient(circle, ${r.color}33, transparent 70%)`,
                                    filter: "blur(8px)", pointerEvents: "none",
                                  }} />
                                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", position: "relative" }}>
                                    <span style={{ display: "inline-flex", alignItems: "center", gap: 10, fontSize: 13, fontWeight: 700, color: r.color }}>
                                      <span style={{
                                        width: 30, height: 30, borderRadius: 8,
                                        background: `linear-gradient(135deg, ${r.soft}, ${r.color}, ${r.deep})`,
                                        display: "inline-flex", alignItems: "center", justifyContent: "center",
                                        boxShadow: `0 4px 12px -2px ${r.color}77`,
                                      }}>
                                        <Icon n={r.icon} s={14} c="#fff" />
                                      </span>
                                      {r.label}
                                    </span>
                                    <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                      <span style={{
                                        fontSize: 24, fontWeight: 900, color: r.color,
                                        fontVariantNumeric: "tabular-nums",
                                        textShadow: `0 0 14px ${r.color}66`,
                                        letterSpacing: "-.5px",
                                      }}>{r.val}</span>
                                      <span style={{ fontSize: 12, color: r.color, fontWeight: 700 }}>→</span>
                                    </span>
                                  </div>
                                  {/* Share-of-total progress bar with animated shimmer */}
                                  <div style={{ display: "flex", alignItems: "center", gap: 8, position: "relative" }}>
                                    <div style={{ flex: 1, height: 5, borderRadius: 3, background: `${r.color}25`, overflow: "hidden", position: "relative" }}>
                                      <div style={{
                                        height: "100%",
                                        width: `${pct}%`,
                                        background: `linear-gradient(90deg, ${r.soft}, ${r.color}, ${r.deep})`,
                                        transition: "width .6s ease",
                                        boxShadow: `0 0 10px ${r.color}99, inset 0 0 6px rgba(255,255,255,0.25)`,
                                        position: "relative",
                                      }} />
                                      {/* shimmer sweep */}
                                      <span aria-hidden style={{
                                        position: "absolute", top: 0, left: `${Math.max(0, pct - 8)}%`, width: "16%", height: "100%",
                                        background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.55), transparent)",
                                        animation: "ztShimmer 2.4s ease-in-out infinite",
                                        pointerEvents: "none",
                                      }} />
                                    </div>
                                    <span style={{
                                      fontSize: 10, fontWeight: 800, color: r.color,
                                      fontVariantNumeric: "tabular-nums", minWidth: 30, textAlign: "right",
                                      textShadow: `0 0 8px ${r.color}55`,
                                    }}>
                                      {pct}%
                                    </span>
                                  </div>
                                </button>
                              );
                            })}
                            <style>{`@keyframes ztShimmer{0%{transform:translateX(-200%)}100%{transform:translateX(700%)}}`}</style>
                          </div>
                        );
                      })()}
                    </div>
                  </div>
                )}

                {/* ══ App Usage (kept from original) ══ */}
                <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: 22 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
                    <div>
                      <p style={{ fontWeight: 700, fontSize: 15, color: C.text }}>{T.appUsageToday || "App Usage Today"}</p>
                      <p style={{ fontSize: 12, color: C.textSub }}>{T.allEmployeesCombined || "All employees combined"}</p>
                    </div>
                    <button onClick={fetchOverviewMetrics} title={T.refresh || "Refresh"} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: 6, padding: "5px 10px", fontSize: 11, color: C.textSub, cursor: "pointer", fontFamily: "inherit" }}>↻</button>
                  </div>
                  {overviewApps.length === 0 ? (
                    <div style={{ textAlign: "center", padding: "24px 0", color: C.textSub, fontSize: 13 }}>{T.noDataYetToday || "No data yet today"}</div>
                  ) : (
                    <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 20, alignItems: "center" }}>
                      <Suspense fallback={<div style={{ height: 200 }} />}>
                        <TopAppsDonut C={C} hoveredAppIdx={hoveredAppIdx} overviewApps={overviewApps} setHoveredAppIdx={setHoveredAppIdx} />
                      </Suspense>
                      <div style={{ display: "flex", flexDirection: "column", gap: 6, overflow: "hidden" }}>
                        {overviewApps.map((a, i) => {
                          const cat = APP_CATEGORY(a.app_name);
                          const mins = Math.floor(a.total_seconds / 60);
                          const isHover = hoveredAppIdx === i;
                          const gradPair = [
                            ["#A5B4FC","#4338CA"],["#6EE7B7","#047857"],["#FCD34D","#B45309"],["#93C5FD","#1E40AF"],
                            ["#F9A8D4","#BE185D"],["#C4B5FD","#6D28D9"],["#FDBA74","#C2410C"],["#5EEAD4","#0D9488"],
                          ][i % 8];
                          return (
                            <div key={i}
                              onMouseEnter={() => setHoveredAppIdx(i)}
                              onMouseLeave={() => setHoveredAppIdx(null)}
                              style={{
                                display: "flex", alignItems: "center", gap: 8,
                                padding: "6px 10px",
                                background: isHover ? `linear-gradient(90deg, ${gradPair[0]}22, transparent)` : C.bg,
                                borderRadius: 8,
                                border: `1px solid ${isHover ? gradPair[1] : C.border}`,
                                transition: "all .15s",
                                cursor: "default",
                              }}>
                              <span style={{ width: 10, height: 10, borderRadius: "50%", background: `linear-gradient(135deg,${gradPair[0]},${gradPair[1]})`, flexShrink: 0 }} />
                              <span style={{ flex: 1, fontSize: 12, fontWeight: 600, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.app_name}</span>
                              <span style={{ fontSize: 11, fontWeight: 700, color: C.textSub, flexShrink: 0 }}>{Math.floor(mins / 60)}h {mins % 60}m</span>
                              <span style={{ fontSize: 10, fontWeight: 700, color: cat.color, background: cat.bg, padding: "1px 6px", borderRadius: 99, flexShrink: 0 }}>{cat.label}</span>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>

                {/* ══ NEW OVERVIEW SECTIONS ══ */}

                {/* ── Work Load Analysis ── */}
                {analytics && analytics.emp_hours && analytics.emp_hours.length > 0 && (() => {
                  const { total, segments } = workLoadAgg;
                  return (
                    <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "22px 24px", boxShadow: C.shadow }}>
                      <p style={{ fontWeight: 700, fontSize: 15, color: C.text, marginBottom: 4 }}>{T.workLoadAnalysis || "Work Load Analysis"}</p>
                      <p style={{ fontSize: 12, color: C.textSub, marginBottom: 18 }}>Last {analyticsDays} days · {total} employees</p>
                      <div style={{ display: "flex", height: 12, borderRadius: 6, overflow: "hidden", marginBottom: 18 }}>
                        {segments.filter(s => s.count > 0).map(s => (
                          <div key={s.label} style={{ width: `${s.pct}%`, background: s.color, transition: "width .3s" }} title={`${s.label}: ${s.count}`} />
                        ))}
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 14 }}>
                        {segments.map(s => (
                          <div key={s.label} style={{ padding: "16px 18px", background: s.bg, borderRadius: 12, textAlign: "center" }}>
                            <p style={{ fontSize: 28, fontWeight: 900, color: s.color, lineHeight: 1 }}>{s.count}</p>
                            <p style={{ fontSize: 11, fontWeight: 600, color: s.color, marginTop: 6, opacity: .8 }}>{s.label}</p>
                            <p style={{ fontSize: 10, color: C.textSub, marginTop: 2 }}>{s.pct}% of total</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  );
                })()}

                {/* ── Two-column grid: Late Clock-in + Absence Today ── */}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                  {/* Late Clock-in Employees */}
                  <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "22px 24px", boxShadow: C.shadow }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
                      <div style={{ width: 32, height: 32, borderRadius: 10, background: "#FEF3C7", display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <Icon n="clock" s={16} c="#F59E0B" />
                      </div>
                      <div>
                        <p style={{ fontWeight: 700, fontSize: 14, color: C.text }}>{T.lateClockin || "Late Clock-in"}</p>
                        <p style={{ fontSize: 11, color: C.textSub }}>{T.lateClockinSubtitle || "Clocked in after 10:30 AM today"}</p>
                      </div>
                    </div>
                    {(() => {
                      if (lateEmps.length === 0) return <p style={{ textAlign: "center", color: C.textSub, fontSize: 13, padding: "20px 0" }}>{T.noLateClockinsToday || "No late clock-ins today"}</p>;
                      const visible = lateExpanded ? lateEmps : lateEmps.slice(0, 8);
                      return (
                        <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 260, overflowY: "auto" }}>
                          {visible.map(emp => (
                            <div key={emp.id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 12px", background: C.bg, borderRadius: 10, border: `1px solid ${C.borderLight}` }}>
                              <div style={{ width: 32, height: 32, borderRadius: 8, background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 11, flexShrink: 0 }}>{(emp.name || "?")[0]}</div>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <p style={{ fontWeight: 600, fontSize: 12, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{emp.name}</p>
                                <p style={{ fontSize: 10, color: C.textSub }}>{emp.department || emp.team || "—"}</p>
                              </div>
                              <span style={{ fontSize: 11, fontWeight: 700, color: "#F59E0B", background: "#FEF3C7", padding: "3px 8px", borderRadius: 6 }}>{fmtTimeIST(emp.clock_in_time)}</span>
                            </div>
                          ))}
                          {lateEmps.length > 8 && (
                            <button
                              type="button"
                              onClick={() => setLateExpanded(v => !v)}
                              style={{
                                textAlign: "center", fontSize: 11, fontWeight: 600,
                                color: C.brand, background: "transparent",
                                border: "none", padding: "6px 0", cursor: "pointer",
                                fontFamily: "inherit",
                              }}>
                              {lateExpanded
                                ? (T.showLess || "Show less")
                                : `+${lateEmps.length - 8} ${T.more || "more"}`}
                            </button>
                          )}
                        </div>
                      );
                    })()}
                  </div>

                  {/* Absence Today */}
                  <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "22px 24px", boxShadow: C.shadow }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
                      <div style={{ width: 32, height: 32, borderRadius: 10, background: "#FEE2E2", display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <Icon n="alert" s={16} c="#EF4444" />
                      </div>
                      <div>
                        <p style={{ fontWeight: 700, fontSize: 14, color: C.text }}>{T.absentOnLeave || "Absent / On Leave"}</p>
                        <p style={{ fontSize: 11, color: C.textSub }}>{T.absentSubtitle || "Employees who haven't clocked in today"}</p>
                      </div>
                    </div>
                    {(() => {
                      if (absentEmps.length === 0) return <p style={{ textAlign: "center", color: C.textSub, fontSize: 13, padding: "20px 0" }}>{T.everyoneClockedIn || "Everyone has clocked in"}</p>;
                      const visibleAbsent = absentExpanded ? absentEmps : absentEmps.slice(0, 12);
                      return (
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, maxHeight: 260, overflowY: "auto" }}>
                          {visibleAbsent.map(emp => {
                            const st = effectiveStatus(emp).toLowerCase();
                            const isLeave = st === "on_leave";
                            // Theme-aware chip palette — light mode uses pastel
                            // backgrounds, dark mode uses translucent tints over
                            // the surface so the white name text stays readable.
                            const chipBg  = isDark
                              ? (isLeave ? "rgba(22,163,74,0.14)" : "rgba(220,38,38,0.14)")
                              : (isLeave ? "#F0FDF4" : "#FEF2F2");
                            const chipBdr = isDark
                              ? (isLeave ? "rgba(22,163,74,0.35)" : "rgba(220,38,38,0.35)")
                              : (isLeave ? "#BBF7D0" : "#FECACA");
                            const avatarBg = isDark
                              ? (isLeave ? "#16A34A" : "#DC2626")
                              : (isLeave ? "#86EFAC" : "#FCA5A5");
                            const subColor = isDark
                              ? (isLeave ? "#4ADE80" : "#FCA5A5")
                              : (isLeave ? "#2F8F5C" : "#DC2626");
                            return (
                              <div key={emp.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px", background: chipBg, borderRadius: 10, border: `1px solid ${chipBdr}` }}>
                                <div style={{ width: 28, height: 28, borderRadius: 7, background: avatarBg, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 10, flexShrink: 0 }}>{(emp.name || "?")[0]}</div>
                                <div>
                                  <p style={{ fontWeight: 600, fontSize: 12, color: C.text }}>{emp.name}</p>
                                  <p style={{ fontSize: 10, color: subColor, fontWeight: 600 }}>{isLeave ? (T.onLeaveStatus || "On Leave") : (T.absent || "Absent")}</p>
                                </div>
                              </div>
                            );
                          })}
                          {absentEmps.length > 12 && (
                            <button
                              type="button"
                              onClick={() => setAbsentExpanded(v => !v)}
                              style={{
                                padding: "8px 14px", background: C.bg, borderRadius: 10,
                                border: `1px solid ${C.border}`,
                                fontSize: 11, color: C.brand, fontWeight: 700,
                                cursor: "pointer", fontFamily: "inherit",
                              }}>
                              {absentExpanded
                                ? (T.showLess || "Show less")
                                : `+${absentEmps.length - 12} ${T.more || "more"}`}
                            </button>
                          )}
                        </div>
                      );
                    })()}
                  </div>
                </div>

                {/* ── Currently Using Apps and Websites ── */}
                <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "22px 24px", boxShadow: C.shadow }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 18 }}>
                    <div style={{ width: 32, height: 32, borderRadius: 10, background: "#EDE9FE", display: "flex", alignItems: "center", justifyContent: "center" }}>
                      <Icon n="monitor" s={16} c="#7C3AED" />
                    </div>
                    <div>
                      <p style={{ fontWeight: 700, fontSize: 14, color: C.text }}>{T.currentlyUsingAppsWebsites || "Currently Using Apps & Websites"}</p>
                      <p style={{ fontSize: 11, color: C.textSub }}>{T.realtimeActiveAppsSubtitle || "Real-time active applications"}</p>
                    </div>
                  </div>
                  {(() => {
                    const activeEmps = activeAppEmps;
                    if (activeEmps.length === 0) return <p style={{ textAlign: "center", color: C.textSub, fontSize: 13, padding: "20px 0" }}>{T.noActiveAppsNow || "No active apps right now"}</p>;
                    return (
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 10 }}>
                        {activeEmps.slice(0, 16).map(emp => {
                          const lm = getAppMeta(emp.current_app);
                          return (
                            <div key={emp.id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", background: isDark ? "#1F2937" : C.bg, borderRadius: 10, border: `1px solid ${C.border}`, transition: "all .15s" }}
                              onMouseEnter={e => { e.currentTarget.style.borderColor = C.brand; e.currentTarget.style.boxShadow = C.shadow; }}
                              onMouseLeave={e => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.boxShadow = "none"; }}>
                              <div style={{ width: 32, height: 32, borderRadius: 8, background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 11, flexShrink: 0 }}>{(emp.name || "?")[0]}</div>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <p style={{ fontWeight: 600, fontSize: 13, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{emp.name}</p>
                              </div>
                              <div style={{ display: "flex", alignItems: "center", gap: 5, padding: "4px 10px", background: C.surface, borderRadius: 7, border: `1px solid ${C.border}` }}>
                                <AppIcon app={lm} size={14} favicon={getTabFavicon(emp.current_app, emp.current_tab)} />
                                <span style={{ fontSize: 11, fontWeight: 600, color: C.text, maxWidth: 90, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{lm.label}</span>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    );
                  })()}
                </div>

                {/* ── Recent Screenshots ── */}
                <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 16 }}>
                  {/* Recent Screenshots */}
                  <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "22px 24px", boxShadow: C.shadow }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
                      <div style={{ width: 32, height: 32, borderRadius: 10, background: "#DBEAFE", display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <Icon n="monitor" s={16} c="#3B82F6" />
                      </div>
                      <div>
                        <p style={{ fontWeight: 700, fontSize: 14, color: C.text }}>{T.recentScreenshots || "Recent Screenshots"}</p>
                        <p style={{ fontSize: 11, color: C.textSub }}>{T.latestActivityCaptures || "Latest activity captures"}</p>
                      </div>
                      <div style={{ marginLeft: "auto" }}>
                        <button onClick={() => navigate("/admin?tab=screenshots")} style={{ fontSize: 11, fontWeight: 600, color: C.brand, background: "none", border: "none", cursor: "pointer", fontFamily: "inherit" }}>{T.adminDashboardViewAll || "View All →"}</button>
                      </div>
                    </div>
                    {recentScreenshots.length === 0 ? (
                      <p style={{ textAlign: "center", color: C.textSub, fontSize: 13, padding: "28px 0" }}>{T.noScreenshotsYet || "No screenshots captured yet"}</p>
                    ) : (
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
                        {recentScreenshots.slice(0, 6).map((ss, i) => {
                          // Same source resolution as the Screenshots tab: base64
                          // thumb → public http url → auth-protected /image route.
                          // The last one needs a Bearer token, so it MUST go through
                          // AuthImage — a plain <img> 401s and shows a black box.
                          const imgUrl = ss.thumbnail
                            ? `data:image/jpeg;base64,${ss.thumbnail}`
                            : (ss.file_url && ss.file_url.startsWith("http"))
                              ? ss.file_url
                              : `${API_BASE}/api/screenshots/${ss.id}/image`;
                          return (
                          <div key={i} style={{ position: "relative", borderRadius: 10, overflow: "hidden", border: `1px solid ${C.borderLight}`, aspectRatio: "16/10", background: C.bg, cursor: "pointer" }}
                            onClick={() => navigate("/admin?tab=screenshots")}>
                            <AuthImage url={imgUrl} token={token} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                            <div style={{ position: "absolute", bottom: 0, left: 0, right: 0, padding: "14px 8px 6px", background: "linear-gradient(transparent, rgba(0,0,0,.7))" }}>
                              <p style={{ fontSize: 10, fontWeight: 600, color: "#fff", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{ss.user_name || ss.employee_name || (T.adminDashboardEmployeeWord || "Employee")}</p>
                              <p style={{ fontSize: 9, color: "rgba(255,255,255,.7)" }}>{ss.timestamp ? new Date(ss.timestamp).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: getOrgTimeZone() }) : ""}</p>
                            </div>
                          </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </div>

                {/* ── Work Hours Summary ── */}
                <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "22px 24px", boxShadow: C.shadow }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 18 }}>
                    <div style={{ width: 32, height: 32, borderRadius: 10, background: "#E7F0F6", display: "flex", alignItems: "center", justifyContent: "center" }}>
                      <Icon n="clock" s={16} c="#11496A" />
                    </div>
                    <div>
                      <p style={{ fontWeight: 700, fontSize: 14, color: C.text }}>{T.workHoursSummary || "Work Hours Summary"}</p>
                      <p style={{ fontSize: 11, color: C.textSub }}>{historyData ? `${activeDateLabel} work time breakdown` : "Today's work time breakdown per employee"}</p>
                    </div>
                  </div>
                  {(() => {
                    // Use historical records when available
                    if (historyData) {
                      const recs = (historyData.records || []).filter(r => (r.total_work_seconds||0) > 0).sort((a,b) => (b.total_work_seconds||0) - (a.total_work_seconds||0));
                      if (recs.length === 0) return <p style={{ textAlign: "center", color: C.textSub, fontSize: 13, padding: "20px 0" }}>No work data for {activeDateLabel}</p>;
                      const maxSecs = recs[0]?.total_work_seconds || 1;
                      const fmtD2 = (s) => `${String(Math.floor(s/3600)).padStart(2,"0")}h ${String(Math.floor((s%3600)/60)).padStart(2,"0")}m`;
                      return (
                        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                          {recs.map(r => (
                            <div key={r.user_id} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                              <div style={{ width: 32, height: 32, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 11, flexShrink: 0 }}>
                                {(r.name||"").split(" ").map(w=>w[0]).join("").toUpperCase().slice(0,2)}
                              </div>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3 }}>
                                  <span style={{ fontSize: 13, fontWeight: 600, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name}</span>
                                  <span style={{ fontSize: 12, fontWeight: 800, color: C.brand, flexShrink: 0, marginLeft: 8 }}>{fmtD2(r.total_work_seconds||0)}</span>
                                </div>
                                <div style={{ height: 5, background: C.borderLight, borderRadius: 3, overflow: "hidden" }}>
                                  <div style={{ width: `${Math.round(((r.total_work_seconds||0)/maxSecs)*100)}%`, height: "100%", background: `linear-gradient(90deg,${C.brand},${C.brandDark})`, borderRadius: 3 }} />
                                </div>
                              </div>
                            </div>
                          ))}
                        </div>
                      );
                    }
                    if (workedEmps.length === 0) return <p style={{ textAlign: "center", color: C.textSub, fontSize: 13, padding: "20px 0" }}>{T.noWorkDataToday || "No work data yet today"}</p>;
                    const fmtD = (s) => `${String(Math.floor(s / 3600)).padStart(2, "0")}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
                    const maxSecs = workedEmps[0]?.workSecs || 1;
                    // Collapsed shows top-12 in the scroll box; expanded
                    // shows every worker who logged time today. The toggle
                    // bottom-right reflects how many more rows are hidden.
                    const COLLAPSED_LIMIT = 12;
                    const visible = showAllWorkHours
                      ? workedEmps
                      : workedEmps.slice(0, COLLAPSED_LIMIT);
                    const hiddenCount = workedEmps.length - visible.length;
                    return (
                      <>
                        <div style={{
                          display: "flex", flexDirection: "column", gap: 6,
                          maxHeight: showAllWorkHours ? 560 : 320,
                          overflowY: "auto", transition: "max-height 0.2s ease",
                        }}>
                          {visible.map(emp => (
                            <div key={emp.id} style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 12px", background: C.bg, borderRadius: 10, border: `1px solid ${C.borderLight}` }}>
                              <div style={{ width: 30, height: 30, borderRadius: 8, background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 10, flexShrink: 0 }}>{(emp.name || "?")[0]}</div>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                                  <span style={{ fontSize: 12, fontWeight: 600, color: C.text }}>{emp.name}</span>
                                  <span style={{ fontSize: 11, fontWeight: 700, color: C.brand }}>{fmtD(emp.workSecs)}</span>
                                </div>
                                <div style={{ height: 6, background: C.borderLight, borderRadius: 3, overflow: "hidden" }}>
                                  <div style={{ height: "100%", width: `${Math.min(100, (emp.workSecs / maxSecs) * 100)}%`, background: `linear-gradient(90deg, ${C.brand}, ${C.brandDark})`, borderRadius: 3, transition: "width .3s" }} />
                                </div>
                              </div>
                            </div>
                          ))}
                        </div>
                        {workedEmps.length > COLLAPSED_LIMIT && (
                          <div style={{ marginTop: 12, display: "flex", justifyContent: "center" }}>
                            <button
                              onClick={() => setShowAllWorkHours(v => !v)}
                              style={{
                                display: "inline-flex", alignItems: "center", gap: 6,
                                padding: "8px 16px", borderRadius: 999,
                                border: `1px solid ${C.brandBdr}`,
                                background: C.brandBg, color: C.brand,
                                fontSize: 12, fontWeight: 700, cursor: "pointer",
                                fontFamily: "inherit", transition: "all .15s",
                              }}
                              onMouseEnter={e => { e.currentTarget.style.background = C.brand; e.currentTarget.style.color = "#fff"; }}
                              onMouseLeave={e => { e.currentTarget.style.background = C.brandBg; e.currentTarget.style.color = C.brand; }}
                            >
                              {showAllWorkHours
                                ? `Show less ▴`
                                : `+ ${hiddenCount} more — Show all (${workedEmps.length}) ▾`}
                            </button>
                          </div>
                        )}
                      </>
                    );
                  })()}
                </div>

                {/* ── Two-column grid: Upcoming Holidays + Upcoming Leaves ── */}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                  {/* Upcoming Holidays */}
                  <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "22px 24px", boxShadow: C.shadow }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
                      <div style={{ width: 32, height: 32, borderRadius: 10, background: isDark ? "#3D2A0A" : "#FEF3C7", display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <Icon n="calendar" s={16} c="#F59E0B" />
                      </div>
                      <div>
                        <p style={{ fontWeight: 700, fontSize: 14, color: C.text }}>{T.upcomingHolidays || "Upcoming Holidays"}</p>
                        <p style={{ fontSize: 11, color: C.textSub }}>{T.companyHolidaysAhead || "Company holidays ahead"}</p>
                      </div>
                      <div style={{ marginLeft: "auto" }}>
                        <button onClick={() => navigate("/admin/settings?sub=public-holidays")} style={{ fontSize: 11, fontWeight: 600, color: C.brand, background: "none", border: "none", cursor: "pointer", fontFamily: "inherit" }}>{T.adminDashboardViewAll || "View All →"}</button>
                      </div>
                    </div>
                    {(() => {
                      if (upcomingHols.length === 0) {
                        return (
                          <div style={{ textAlign: "center", padding: "28px 0" }}>
                            <div style={{ width: 40, height: 40, borderRadius: 12, background: isDark ? "#3D2A0A" : "#FEF3C7", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 10px" }}>
                              <Icon n="calendar" s={20} c="#F59E0B" />
                            </div>
                            <p style={{ fontSize: 13, fontWeight: 600, color: C.textSub }}>{T.noHolidaysConfigured || "No holidays configured"}</p>
                            <button
                              onClick={() => navigate("/settings/public-holidays")}
                              style={{ marginTop: 8, padding: "6px 14px", fontSize: 11, fontWeight: 700, color: "#F59E0B",
                                       background: isDark ? "#3D2A0A" : "#FEF3C7", border: `1px solid ${isDark ? "#5C3F10" : "#FCD34D"}`,
                                       borderRadius: 8, cursor: "pointer", fontFamily: "inherit" }}>
                              {T.configureInPublicHolidays || "Configure in Public Holidays →"}
                            </button>
                          </div>
                        );
                      }
                      return (
                        <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 260, overflowY: "auto" }}>
                          {upcomingHols.map((h, i) => {
                            const d = new Date(h.date + "T00:00:00");
                            return (
                              <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", background: C.bg, borderRadius: 10, border: `1px solid ${C.borderLight}` }}>
                                <div style={{ width: 38, height: 38, borderRadius: 10, background: isDark ? "#3D2A0A" : "#FEF3C7", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                                  <span style={{ fontSize: 14, fontWeight: 900, color: "#F59E0B", lineHeight: 1 }}>{d.getDate()}</span>
                                  <span style={{ fontSize: 8, fontWeight: 700, color: "#F59E0B", textTransform: "uppercase" }}>{d.toLocaleDateString("en-IN", { month: "short" })}</span>
                                </div>
                                <div style={{ flex: 1, minWidth: 0 }}>
                                  <p style={{ fontWeight: 600, fontSize: 12, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{h.localName || h.name}</p>
                                  <p style={{ fontSize: 10, color: C.textSub }}>{d.toLocaleDateString(undefined, { weekday: "long" })}</p>
                                </div>
                                {h.global !== false && (
                                  <span style={{ fontSize: 10, fontWeight: 600, color: "#F59E0B", background: isDark ? "#3D2A0A" : "#FEF3C7", padding: "3px 8px", borderRadius: 6 }}>{h.holiday_type || T.adminDashboardPublicBadge || "Public"}</span>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      );
                    })()}
                  </div>

                  {/* Upcoming Leaves */}
                  <div style={{ background: C.surface, borderRadius: 16, border: `1px solid ${C.border}`, padding: "22px 24px", boxShadow: C.shadow }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
                      <div style={{ width: 32, height: 32, borderRadius: 10, background: "#EDE9FE", display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <Icon n="calendar" s={16} c="#7C3AED" />
                      </div>
                      <div>
                        <p style={{ fontWeight: 700, fontSize: 14, color: C.text }}>{T.upcomingLeaves || "Upcoming Leaves"}</p>
                        <p style={{ fontSize: 11, color: C.textSub }}>{T.upcomingLeaveRequests || "Upcoming leave requests"}</p>
                      </div>
                      <div style={{ marginLeft: "auto" }}>
                        <button onClick={() => navigate("/admin?tab=manageleave")} style={{ fontSize: 11, fontWeight: 600, color: C.brand, background: "none", border: "none", cursor: "pointer", fontFamily: "inherit" }}>{T.adminDashboardViewAll || "View All →"}</button>
                      </div>
                    </div>
                    {(() => {
                      const upcoming = upcomingLeaves;
                      if (upcoming.length === 0) return <p style={{ textAlign: "center", color: C.textSub, fontSize: 13, padding: "20px 0" }}>{T.noUpcomingLeaves || "No upcoming leaves"}</p>;
                      const visibleUpcoming = upcomingExpanded ? upcoming : upcoming.slice(0, 6);
                      return (
                        <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 260, overflowY: "auto" }}>
                          {visibleUpcoming.map((lr, i) => {
                            const from = new Date(lr.from_date || lr.start_date);
                            const to = new Date(lr.to_date || lr.end_date);
                            const days = Math.max(1, Math.ceil((to - from) / (1000 * 60 * 60 * 24)) + 1);
                            const isApproved = (lr.status || "").toLowerCase() === "approved";
                            const badge = isApproved
                              ? { label: T.adminDashboardApprovedBadge || "Approved", color: "#22C55E", bg: "#F0FDF4" }
                              : { label: T.adminDashboardPendingBadge || "Pending", color: "#D97706", bg: "#FFFBEB" };
                            return (
                              <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", background: C.bg, borderRadius: 10, border: `1px solid ${C.borderLight}` }}>
                                <div style={{ width: 38, height: 38, borderRadius: 10, background: "#EDE9FE", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                                  <span style={{ fontSize: 14, fontWeight: 900, color: "#7C3AED", lineHeight: 1 }}>{from.getDate()}</span>
                                  <span style={{ fontSize: 8, fontWeight: 700, color: "#7C3AED", textTransform: "uppercase" }}>{from.toLocaleDateString("en-IN", { month: "short" })}</span>
                                </div>
                                <div style={{ flex: 1, minWidth: 0 }}>
                                  <p style={{ fontWeight: 600, fontSize: 12, color: C.text }}>{lr.name || lr.employee_name || lr.user_name || (T.adminDashboardEmployeeWord || "Employee")}</p>
                                  <p style={{ fontSize: 10, color: C.textSub }}>{lr.leave_type || lr.type || (T.adminDashboardLeaveWord || "Leave")} · {days} {days > 1 ? (T.adminDashboardDaysUnit || "days") : (T.adminDashboardDayUnit || "day")}</p>
                                </div>
                                <span style={{ fontSize: 10, fontWeight: 600, color: badge.color, background: badge.bg, padding: "3px 8px", borderRadius: 6 }}>{badge.label}</span>
                              </div>
                            );
                          })}
                          {upcoming.length > 6 && (
                            <button
                              type="button"
                              onClick={() => setUpcomingExpanded(v => !v)}
                              style={{
                                textAlign: "center", fontSize: 11, fontWeight: 600,
                                color: C.brand, background: "transparent",
                                border: "none", padding: "6px 0", cursor: "pointer",
                                fontFamily: "inherit",
                              }}>
                              {upcomingExpanded
                                ? (T.showLess || "Show less")
                                : `+${upcoming.length - 6} ${T.more || "more"}`}
                            </button>
                          )}
                        </div>
                      );
                    })()}
                  </div>
                </div>

              </div>
            )}

            {tab === "myteam" && (
              <Suspense fallback={<TabFallback />}>
                <MyTeamTab
                  C={C} T={T} isDark={isDark} Icon={Icon}
                  filteredEmps={filteredEmps} statusFilter={statusFilter} setStatusFilter={setStatusFilter} search={search}
                  myTeamView={myTeamView} setMyTeamView={setMyTeamView}
                  histStatusFilter={histStatusFilter} setHistStatusFilter={setHistStatusFilter}
                  historyData={historyData} setHistoryData={setHistoryData}
                  activeDateLabel={activeDateLabel} setActiveDateLabel={setActiveDateLabel}
                  setDateRange={setDateRange} setViewEmp={setViewEmp}
                />
              </Suspense>
            )}

            {tab === "screenshots" && (
              <Suspense fallback={<TabFallback />}>
                <ScreenshotsTab
                  C={C} T={T} isDark={isDark} Icon={Icon} AppIcon={AppIcon} initials={initials} fmtTimeIST={fmtTimeIST}
                  getAppMeta={getAppMeta} effectiveStatus={effectiveStatus} employees={employees}
                  token={token} showToast={showToast} fetchScreenshots={fetchScreenshots}
                  screenshotData={screenshotData} ssLoading={ssLoading} ssBlurImages={ssBlurImages}
                  ssShowIdle={ssShowIdle} setSsShowIdle={setSsShowIdle} ssSort={ssSort} setSsSort={setSsSort}
                  ssActivityFilter={ssActivityFilter} setSsActivityFilter={setSsActivityFilter}
                  ssDateFilter={ssDateFilter} setSsDateFilter={setSsDateFilter}
                  ssEmpSearch={search} setSsEmpSearch={setSearch}
                  ssAppSearch={ssAppSearch} setSsAppSearch={setSsAppSearch}
                  ssSelectedUser={ssSelectedUser} setSsSelectedUser={setSsSelectedUser}
                  ssSelectedIds={ssSelectedIds} setSsSelectedIds={setSsSelectedIds}
                  setLightboxUrl={setLightboxUrl}
                />
              </Suspense>
            )}

            {tab === "timesheets" && (
              <Suspense fallback={<TabFallback />}>
                <TimesheetsTab
                  C={C} T={T} isDark={isDark} Icon={Icon} initials={initials} parseUTC={parseUTC}
                  employees={employees} search={search} colRef={colRef}
                  tsPeriod={tsPeriod} setTsPeriod={setTsPeriod} tsDate={tsDate} setTsDate={setTsDate}
                  tsMonthData={tsMonthData} setTsMonthData={setTsMonthData} tsMonthLoading={tsMonthLoading}
                  tsColFilter={tsColFilter} setTsColFilter={setTsColFilter} tsColOpen={tsColOpen} setTsColOpen={setTsColOpen}
                />
              </Suspense>
            )}

            {tab === "manageleave" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "20px 22px", background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`, boxShadow: C.shadow }}>
                  <h2 style={{ fontSize: 22, fontWeight: 800, color: C.text, letterSpacing: "-0.3px" }}>{T.manageLeave || "Manage Leave"}</h2>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    {["all", "submitted", "approved", "rejected"].map(s => (
                      <button key={s} onClick={() => {}} style={{ padding: "7px 16px", borderRadius: 20, border: `1.5px solid ${C.border}`, background: C.surface, color: C.textSub, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit", textTransform: "capitalize", transition: "all .15s" }}>{s}</button>
                    ))}
                  </div>
                </div>
                {leaveRequests.length === 0 ? (
                  <div style={{ ...card(), padding: "60px", textAlign: "center" }}>
                    <Icon n="briefcase" s={36} c={C.textLight} />
                    <p style={{ marginTop: 12, fontSize: 15, color: C.textLight, fontWeight: 600 }}>{T.adminDashboardNoLeaveRequests || "No leave requests yet"}</p>
                    <p style={{ fontSize: 13, color: C.textLight, marginTop: 4 }}>{T.adminDashboardLeaveRequestsAppearHere || "Employee leave requests will appear here"}</p>
                  </div>
                ) : (
                  <div style={{ ...card(), overflow: "hidden" }}>
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                      <thead>
                        <tr style={{ background: C.bg }}>
                          {[T.employee||"Employee", T.leave_type||"Leave Type", T.from||"From", T.to||"To", T.days||"Days", T.reason||"Reason", T.document||"Document", T.appliedOn||"Applied On", T.status||"Status", T.actions||"Actions"].map(h => (
                            <th key={h} style={{ padding: "10px 14px", textAlign: "left", fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: ".5px", borderBottom: `1px solid ${C.border}`, whiteSpace: "nowrap" }}>{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {leaveRequests.map(r => {
                          const statusColor = r.status === "approved" ? C.green : r.status === "rejected" ? C.red : C.amber;
                          const statusBg = r.status === "approved" ? C.greenBg : r.status === "rejected" ? C.redBg : C.amberBg;
                          const statusBdr = r.status === "approved" ? C.greenBdr : r.status === "rejected" ? C.redBdr : C.amberBdr;
                          return (
                            <tr key={r.id} className="row-h" style={{ borderBottom: `1px solid ${C.borderLight}` }}>
                              <td style={{ padding: "11px 14px" }}>
                                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                  <div style={{ width: 30, height: 30, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 11, flexShrink: 0 }}>{initials(r.name)}</div>
                                  <div>
                                    <p style={{ fontWeight: 700, color: C.text, fontSize: 13 }}>{r.name}</p>
                                    <p style={{ fontSize: 11, color: C.textSub }}>{r.email}</p>
                                  </div>
                                </div>
                              </td>
                              <td style={{ padding: "11px 14px" }}>
                                <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                                  <span style={{ fontSize: 12, fontWeight: 600, color: C.brand, background: C.brandBg, border: `1px solid ${C.brandBdr}`, padding: "3px 8px", borderRadius: 99 }}>{r.leave_type}</span>
                                  {r.is_lwp && <span style={{ fontSize: 10, fontWeight: 700, color: "#B45309", background: "#FFFBEB", border: "1px solid #FCD34D", padding: "2px 6px", borderRadius: 99 }}>{T.adminDashboardLwpBadge || "LWP"}</span>}
                                  {r.hr_flag && <span style={{ fontSize: 10, fontWeight: 700, color: "#7C3AED", background: "#F5F3FF", border: "1px solid #C4B5FD", padding: "2px 6px", borderRadius: 99 }}>⚑ HR</span>}
                                  {r.has_attachment && <span style={{ fontSize: 10, fontWeight: 600, color: C.green, background: C.greenBg, border: `1px solid ${C.greenBdr}`, padding: "2px 6px", borderRadius: 99 }}>{T.adminDashboardDocBadge || "Doc"}</span>}
                                </div>
                                {/* Compensatory Leave: the days the employee already worked
                                    to earn this comp-off, so HR can verify the claim right
                                    from the Manage Leave table. Only present on comp requests. */}
                                {Array.isArray(r.worked_dates) && r.worked_dates.length > 0 && (
                                  <div style={{ marginTop: 6 }}>
                                    <p style={{ fontSize: 10, fontWeight: 700, color: "#15803D", marginBottom: 3 }}>
                                      🛠️ {T.workedDatesAdminLabel || "Dates worked (claimed)"} ({r.worked_dates.length})
                                    </p>
                                    <div style={{ display: "flex", gap: 4, flexWrap: "wrap", maxWidth: 230 }}>
                                      {/* Each date opens Time Playback for this employee and day
                                          in a new tab, so the claim can be checked. */}
                                      {r.worked_dates.map((d, i) => {
                                        const chipS = { fontSize: 10, fontWeight: 600, color: "#166534", background: "#F0FDF4", border: "1px solid #86EFAC", padding: "2px 7px", borderRadius: 99, whiteSpace: "nowrap" };
                                        return (r.user_id != null && /^\d{4}-\d{2}-\d{2}$/.test(String(d)))
                                          ? <a key={i} href={playbackLink(r.user_id, String(d))} target="_blank" rel="noopener noreferrer"
                                              title={T.workedDateOpenPlayback || "Open Time Playback for this day"}
                                              style={{ ...chipS, textDecoration: "underline" }}>{d} ↗</a>
                                          : <span key={i} style={chipS}>{d}</span>;
                                      })}
                                    </div>
                                  </div>
                                )}
                              </td>
                              <td style={{ padding: "11px 14px", color: C.textSub, whiteSpace: "nowrap" }}>{r.from_date}</td>
                              <td style={{ padding: "11px 14px", color: C.textSub, whiteSpace: "nowrap" }}>{r.to_date}</td>
                              <td style={{ padding: "11px 14px", fontWeight: 700, color: C.text }}>{r.days}d</td>
                              <td style={{ padding: "11px 14px", color: C.textSub, maxWidth: 180 }}>
                                <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.reason || "—"}</span>
                              </td>
                              <td style={{ padding: "11px 14px" }}>
                                {r.has_attachment && r.attachment_path ? (() => {
                                  const docUrl = `${API_BASE}${r.attachment_path}`;
                                  const isPdf = (r.attachment_name || "").toLowerCase().endsWith(".pdf");
                                  return (
                                    <button onClick={() => isPdf ? window.open(docUrl, "_blank") : setLightboxUrl({ url: docUrl, name: r.name, appName: "Attachment" })}
                                      style={{ display: "flex", alignItems: "center", gap: 5, padding: "5px 10px", borderRadius: 6, border: `1px solid ${C.greenBdr}`, background: C.greenBg, color: C.green, fontWeight: 700, fontSize: 11, cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap" }}>
                                      {isPdf ? (T.adminDashboardPdfButton || "📄 PDF") : (T.adminDashboardViewDocButton || "🖼 View")}
                                    </button>
                                  );
                                })() : <span style={{ fontSize: 12, color: C.textLight }}>—</span>}
                              </td>
                              <td style={{ padding: "11px 14px", color: C.textLight, whiteSpace: "nowrap", fontSize: 12 }}>
                                {new Date(r.created_at).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}
                              </td>
                              <td style={{ padding: "11px 14px" }}>
                                <span style={{ fontSize: 12, fontWeight: 700, color: statusColor, background: statusBg, border: `1px solid ${statusBdr}`, padding: "3px 10px", borderRadius: 99, textTransform: "capitalize" }}>
                                  {r.status === "under_review" ? (T.adminDashboardUnderReviewStatus || "Under Review") : r.status === "locked_for_payroll" ? (T.adminDashboardLockedStatus || "Locked") : r.status}
                                </span>
                              </td>
                              <td style={{ padding: "11px 14px" }}>
                                {!leaveApprovals.interactive ? (
                                  // leave.approvals withheld (LOCK/HIDE per projection):
                                  // the request remains viewable, but acting on it is not
                                  // offered. Never an inline upsell button — a quiet marker.
                                  <span title={T.leaveApprovalsLocked || "Approving leave is available on a higher plan"}
                                    style={{ fontSize: 11, color: C.textLight, display: "inline-flex", alignItems: "center", gap: 4 }}>
                                    🔒 {T.leaveApprovalsViewOnly || "View only"}
                                  </span>
                                ) : (r.status === "submitted" || r.status === "pending") ? (
                                  <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
                                    <button onClick={() => { setLeaveActionModal({ id: r.id, status: "under_review", name: r.name, leaveType: r.leave_type, days: r.days }); setLeaveActionComment(""); }}
                                      style={{ padding: "4px 10px", borderRadius: 5, border: "1px solid #C4B5FD", background: "#F5F3FF", color: "#7C3AED", fontWeight: 700, fontSize: 11, cursor: "pointer", fontFamily: "inherit" }}>{T.adminDashboardReviewBtn || "Review"}</button>
                                    <button onClick={() => { setLeaveActionModal({ id: r.id, status: "approved", name: r.name, leaveType: r.leave_type, days: r.days }); setLeaveActionComment(""); }}
                                      style={{ padding: "4px 10px", borderRadius: 5, border: `1px solid ${C.greenBdr}`, background: C.greenBg, color: C.green, fontWeight: 700, fontSize: 11, cursor: "pointer", fontFamily: "inherit" }}>{T.approve || "Approve"}</button>
                                    <button onClick={() => { setLeaveActionModal({ id: r.id, status: "rejected", name: r.name, leaveType: r.leave_type, days: r.days }); setLeaveActionComment(""); }}
                                      style={{ padding: "4px 10px", borderRadius: 5, border: `1px solid ${C.redBdr}`, background: C.redBg, color: C.red, fontWeight: 700, fontSize: 11, cursor: "pointer", fontFamily: "inherit" }}>{T.adminDashboardDeclineBtn || "Decline"}</button>
                                  </div>
                                ) : r.status === "under_review" ? (
                                  <div style={{ display: "flex", gap: 5 }}>
                                    <button onClick={() => { setLeaveActionModal({ id: r.id, status: "approved", name: r.name, leaveType: r.leave_type, days: r.days }); setLeaveActionComment(""); }}
                                      style={{ padding: "4px 10px", borderRadius: 5, border: `1px solid ${C.greenBdr}`, background: C.greenBg, color: C.green, fontWeight: 700, fontSize: 11, cursor: "pointer", fontFamily: "inherit" }}>{T.adminDashboardHrApproveBtn || "HR Approve"}</button>
                                    <button onClick={() => { setLeaveActionModal({ id: r.id, status: "rejected", name: r.name, leaveType: r.leave_type, days: r.days }); setLeaveActionComment(""); }}
                                      style={{ padding: "4px 10px", borderRadius: 5, border: `1px solid ${C.redBdr}`, background: C.redBg, color: C.red, fontWeight: 700, fontSize: 11, cursor: "pointer", fontFamily: "inherit" }}>{T.adminDashboardDeclineBtn || "Decline"}</button>
                                  </div>
                                ) : <span style={{ fontSize: 12, color: C.textLight }}>—</span>}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

            {tab === "leavesummary" && (
              <Suspense fallback={<TabFallback />}>
                <LeaveSummaryModule
                  token={token}
                  currentUserRole={(localStorage.getItem("role") || "admin").toLowerCase()}
                />
              </Suspense>
            )}

            {tab === "reports" && (
              <Suspense fallback={<TabFallback />}>
                <ReportsSection
                  API_BASE={API_BASE}
                  token={token}
                  C={C}
                  Icon={Icon}
                  employees={employeesAlpha}
                />
              </Suspense>
            )}

            {tab === "settings" && (
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "60vh" }}>
                <div style={{ textAlign: "center" }}>
                  <div style={{ width: 64, height: 64, borderRadius: 18, background: C.brandBg, border: `1px solid ${C.brandBdr}`, display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}>
                    <Icon n="settings" s={28} c={C.brand} />
                  </div>
                  <p style={{ fontSize: 18, fontWeight: 800, color: C.text, marginBottom: 6 }}>{T.organisationSettings || "Organization Settings"}</p>
                  <p style={{ fontSize: 13, color: C.textSub, marginBottom: 24, maxWidth: 320 }}>{T.settingsDescription || "Manage track settings, teams, screenshot intervals, idle time, payroll, permissions and more."}</p>
                  <button onClick={() => navigate("/settings")}
                    style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "11px 28px", borderRadius: C.rSm, border: "none", background: C.brand, color: "#fff", fontWeight: 700, fontSize: 14, cursor: "pointer", fontFamily: "inherit", boxShadow: "0 4px 14px rgba(17,73,106,0.35)" }}>
                    <Icon n="settings" s={15} c="#fff" /> {T.openSettings || "Open Settings"}
                  </button>
                </div>
              </div>
            )}

            {/* ═══════════════ PAYROLL TAB ═══════════════ */}
            {/* Payroll is one tiered capability (payroll.calculator — Governed+).
                The Gate only controls visibility; no payroll functionality is
                changed. Verified → LOCK; Governed/Sovereign/Enterprise → SHOW. */}
            {tab === "payroll" && (
              <Gate capability="payroll.calculator">
                <Suspense fallback={<TabFallback />}>
                  <PayrollCalculatorModule
                    token={token}
                    employees={employeesAlpha}
                    currentUserRole={(localStorage.getItem("role") || "admin").toLowerCase()}
                  />
                </Suspense>
              </Gate>
            )}

            {tab === "activitylog" && (
              <div style={{ padding: "8px 4px" }}>
                <Suspense fallback={<TabFallback />}>
                  <ActivityLog
                    token={token}
                    isAdmin={true}
                    employees={employees}
                    colors={C}
                    onThumbnailClick={handleThumbnailClick}
                  />
                </Suspense>
              </div>
            )}

            {/* Apps History is privacy-denied for every plan (productivity.apps_history
                ∈ GLOBALLY_DENIED). The backend projection returns HIDE, so the Gate
                renders nothing — never an upsell. This mirrors the sidebar, which
                already hides the nav item, and closes the deep-link gap. */}
            {tab === "appshistory" && (
              <Gate capability="productivity.apps_history">
                <Suspense fallback={<TabFallback />}>
                  <AppsHistoryTab
                    C={C} T={T} isDark={isDark} Icon={Icon} card={card} APP_COLORS={APP_COLORS}
                    employeesAlpha={employeesAlpha} fmtAppDur={fmtAppDur}
                    fetchAppsHistory={fetchAppsHistory} appsAnalytics={appsAnalytics}
                    appsHistoryLoading={appsHistoryLoading}
                    appsHistoryDate={appsHistoryDate} setAppsHistoryDate={setAppsHistoryDate}
                    appsHistoryUserId={appsHistoryUserId} setAppsHistoryUserId={setAppsHistoryUserId}
                    appExpandedUser={appExpandedUser} setAppExpandedUser={setAppExpandedUser}
                  />
                </Suspense>
              </Gate>
            )}

            {/* ─── MANAGE HOLIDAY ─── */}
            {tab === "manageholiday" && (() => {
              const TYPE_COLORS = {
                Public:   { bg: "#DCFCE7", text: "#2F8F5C", border: "#BBF7D0" },
                Company:  { bg: "#DBEAFE", text: "#1D4ED8", border: "#BFDBFE" },
                Optional: { bg: "#FEF9C3", text: "#B45309", border: "#FDE68A" },
              };
              const getTypeStyle = (t) => TYPE_COLORS[t] || TYPE_COLORS.Public;

              // Country mode = a country is selected. The table then shows the
              // organization's OWN holidays for that country and year, followed
              // by the official calendar as a clearly-labelled read-only
              // preview. Previously the preview REPLACED the organization's
              // rows, so a holiday an admin had just added was invisible.
              const isCountryMode = !!holidayCountry;
              const selectedCountry = isCountryMode
                ? holidayCountries.find(c => c.country_code === holidayCountry)
                : null;
              const selectedCountryName =
                selectedCountry?.country_name
                || holidayCatalogInfo?.countryName
                || holidayCountry;

              // Merged by lib/holidayView from the SERVER's classification, so
              // what the page offers is exactly what the server will write.
              const {
                rows: visibleHolidays, preview: previewRows,
                addable: addableRows, conflicts: conflictRows,
                covered: coveredRows, blocked: blockedRows,
              } = mergeHolidayRows(holidays, isCountryMode ? holidayCountryRows : []);
              const counts = adoptionCounts(holidayPlan);
              const yearOptions = holidayApi.buildYearOptions(holidayOrgYears);

              // Calendar helpers — driven by the SAME `visibleHolidays` the
              // table renders, so the two views cannot disagree.
              const { y: calY, m: calM } = holidayCalMonth;
              const firstDay  = new Date(calY, calM, 1).getDay();
              const daysInMon = new Date(calY, calM + 1, 0).getDate();
              const calendarCells = [];
              for (let i = 0; i < firstDay; i++) calendarCells.push(null);
              for (let d = 1; d <= daysInMon; d++) calendarCells.push(d);

              const fmtCalDate = (d) => {
                const mm = String(calM + 1).padStart(2, "0");
                const dd = String(d).padStart(2, "0");
                return `${calY}-${mm}-${dd}`;
              };

              // Reload everything the current selection implies. Used after any
              // write so the table, the calendar and the preview stay in step.
              const reloadHolidays = () => {
                fetchHolidays({ countryCode: holidayCountry, year: holidayYear });
                fetchHolidaySummary();
                fetchPublicHolidays();
                if (holidayCountry) fetchHolidaysForCountry(holidayCountry, holidayYear);
              };

              const openAdd = () => {
                setHolidayForm({
                  name: "", date: "", holiday_type: "Public", description: "",
                  // Default the country to the one being viewed, and the date
                  // into the selected year, so the new holiday lands in the
                  // calendar the admin is actually looking at.
                  country_code: holidayCountry || "",
                });
                setHolidayFormErr("");
                setHolidayEditId(null);
                setHolidayModal("add");
              };

              const openEdit = (h) => {
                setHolidayForm({
                  name: h.name,
                  date: h.date,
                  holiday_type: h.holiday_type,
                  description: h.description || "",
                  country_code: h.country_code || "",
                });
                setHolidayFormErr("");
                setHolidayEditId(h.id);
                setHolidayModal("edit");
              };

              const toast = (message, ms = 3000) => {
                setHolidayToast(message);
                setTimeout(() => setHolidayToast(""), ms);
              };

              // Add / Edit go through api/holidays.js, which uses the shared
              // `authFetch`. The previous version used a BARE `fetch` with a
              // token captured in a closure, so once the access token expired
              // saving failed hard while Delete and Import — which did use
              // authFetch — refreshed silently. Same session, two behaviours.
              const saveHoliday = async () => {
                if (!holidayForm.name.trim()) { setHolidayFormErr(T.adminDashboardHolidayNameRequired || "Holiday name is required"); return; }
                if (!holidayForm.date)        { setHolidayFormErr(T.adminDashboardHolidayDateRequired || "Date is required"); return; }
                setHolidayFormErr("");
                setHolidaySaving(true);
                const payload = {
                  name: holidayForm.name.trim(),
                  date: holidayForm.date,
                  holiday_type: holidayForm.holiday_type,
                  description: holidayForm.description || "",
                  country_code: holidayForm.country_code || null,
                };
                try {
                  if (holidayModal === "edit") {
                    await holidayApi.updateHoliday(holidayEditId, payload);
                  } else {
                    await holidayApi.createHoliday(payload);
                  }
                  const wasEdit = holidayModal === "edit";
                  setHolidayModal(null);
                  toast(wasEdit
                    ? (T.adminDashboardHolidayUpdated || "Holiday updated")
                    : (T.adminDashboardHolidayAdded || "Holiday added"));
                  reloadHolidays();
                } catch (err) {
                  setHolidayFormErr(holidayApi.holidayErrorMessage(err));
                } finally { setHolidaySaving(false); }
              };

              // Delete now surfaces failure. The old version awaited the request
              // without checking it and showed "Holiday deleted" regardless —
              // and the backend returned 200 even when nothing was deleted, so a
              // stale id produced a green toast and an unchanged table.
              const confirmDelete = async () => {
                if (!holidayDelId) return;
                setHolidayDeleting(true);
                setHolidayDelErr("");
                try {
                  await holidayApi.deleteHoliday(holidayDelId);
                  setHolidayDelId(null);
                  toast(T.adminDashboardHolidayDeleted || "Holiday deleted");
                  reloadHolidays();
                } catch (err) {
                  setHolidayDelErr(
                    err?.status === 404
                      ? (T.holidayAlreadyRemoved || "That holiday no longer exists. The list has been refreshed.")
                      : holidayApi.holidayErrorMessage(err)
                  );
                  if (err?.status === 404) reloadHolidays();
                } finally { setHolidayDeleting(false); }
              };

              // Adopt catalog holidays. ONE entry point for "Add all", the
              // per-row Add button and conflict resolutions — `keys` narrows the
              // run, everything else is identical, and the server re-classifies
              // regardless of what the browser sends.
              const adopt = async ({ keys = null, resolutions = null, label } = {}) => {
                if (!holidayCountry || holidayAdopting) return;
                setHolidayAdopting(true);
                if (keys && keys.length === 1) setHolidayRowBusy(keys[0]);
                try {
                  const r = await holidayApi.importCountryCalendar({
                    countryCode: holidayCountry, year: holidayYear,
                    includeOptional: holidayIncludeOptional,
                    includeRegional: holidayIncludeRegional,
                    keys, resolutions,
                  });
                  // Report what HAPPENED, never the size of the request. The old
                  // toast said "Added 20" while only 10 dates changed.
                  const bits = [];
                  if (r.added) bits.push(`Added ${r.added}`);
                  if (r.replaced) bits.push(`replaced ${r.replaced}`);
                  if (r.skipped) bits.push(`skipped ${r.skipped}`);
                  toast(
                    bits.length
                      ? `${label || selectedCountryName}: ${bits.join(", ")}`
                      : `${selectedCountryName} ${holidayYear} is already up to date`,
                    4500,
                  );
                  reloadHolidays();
                } catch (err) {
                  toast(holidayApi.holidayErrorMessage(err), 5000);
                } finally {
                  setHolidayAdopting(false);
                  setHolidayRowBusy("");
                }
              };

              const adoptAll = () => adopt({
                label: `${selectedCountryName} ${holidayYear}`,
              });
              const adoptOne = (entry) => adopt({
                keys: [entry.key], label: entry.name,
              });
              const resolveConflict = (entry, decision) => {
                setHolidayConflict(null);
                if (decision === "skip") return;
                adopt({ keys: [entry.key], resolutions: { [entry.key]: decision },
                        label: entry.name });
              };

              const handleCsvImport = async (e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                setHolidayImporting(true);
                setHolidayCsvResult(null);
                try {
                  const result = await holidayApi.importHolidayCsv(file, {
                    dateFormat: holidayCsvFormat,
                    countryCode: holidayCountry || "",
                  });
                  // The structured result is rendered below rather than squeezed
                  // into a toast: an import that skipped rows has to say WHICH
                  // rows and why. Previously a failed import showed nothing at
                  // all, because the response was parsed inside a bare catch.
                  setHolidayCsvResult(result);
                  reloadHolidays();
                } catch (err) {
                  setHolidayCsvResult({
                    error: holidayApi.holidayErrorMessage(err),
                    imported: 0, duplicates: 0, invalid: 0,
                  });
                } finally { setHolidayImporting(false); e.target.value = ""; }
              };

              const modalBg  = { position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)", zIndex: 2000, display: "flex", alignItems: "center", justifyContent: "center", padding: 20 };
              const modalBox = { background: C.surface, borderRadius: 16, padding: 28, width: "100%", maxWidth: 480, boxShadow: "0 20px 60px rgba(0,0,0,0.25)" };
              const inputSt  = { width: "100%", padding: "10px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.bg, color: C.text, fontSize: 13, fontFamily: "inherit", outline: "none", boxSizing: "border-box" };
              const labelSt  = { fontSize: 12, fontWeight: 600, color: C.textSub, display: "block", marginBottom: 5 };
              const fieldGap = { marginBottom: 14 };

              return (
                <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>

                  {/* Toast */}
                  {holidayToast && (
                    <div style={{ position: "fixed", bottom: 28, right: 28, background: "#22C55E", color: "#fff", padding: "12px 22px", borderRadius: 10, fontWeight: 700, fontSize: 13, zIndex: 3000, boxShadow: "0 4px 16px rgba(34,197,94,0.4)", animation: "fadeIn .2s" }}>
                      {holidayToast}
                    </div>
                  )}

                  {/* Header */}
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <div style={{ width: 38, height: 38, borderRadius: 10, background: C.brandBg, border: `1px solid ${C.brandBdr}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <Icon n="tag" s={17} c={C.brand} />
                      </div>
                      <div>
                        <h2 style={{ fontSize: 18, fontWeight: 800, color: C.text, margin: 0 }}>{T.manageHolidaysTitle || "Manage Holidays"}</h2>
                        {/* Accurate, server-computed counts. "13 available to
                            add" used to be a browser-side guess that bore no
                            relation to what adopting actually wrote. */}
                        <p style={{ fontSize: 12, color: C.textSub, margin: 0 }}>
                          {isCountryMode
                            ? [
                                `${selectedCountryName} · ${holidayYear}`,
                                `${holidays.length} configured`,
                                `${counts.new} new`,
                                (counts.alreadyCovered + counts.exactDuplicates) > 0
                                  && `${counts.alreadyCovered + counts.exactDuplicates} already covered`,
                                counts.conflicts > 0 && `${counts.conflicts} conflict${counts.conflicts === 1 ? "" : "s"}`,
                                counts.notEligible > 0 && `${counts.notEligible} not included`,
                              ].filter(Boolean).join(" · ")
                            : `${holidayYear} — ${holidays.length} ${holidays.length !== 1 ? (T.holidays || "holidays") : (T.holiday || "holiday")} ${T.configured || "configured"}`}
                        </p>
                      </div>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      {/* Country — every country Temveris can serve, not only
                          the ones an admin happened to save as a profile, and
                          not only the ones the external provider covers.
                          Curated entries (India, UAE) are always present.
                          holidays.multicountry (Sovereign+): choosing a country's
                          calendar is the gated multi-country feature. Verified/
                          Governed → LOCK (chip); Sovereign/Enterprise → SHOW. The
                          Year selector and base holiday management stay All-plan. */}
                      {!multiCountry.interactive && (
                        <span title={T.multiCountryLocked || "Multi-country holiday calendars are available on a higher plan"}
                          style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.textSub, fontSize: 12, fontWeight: 600 }}>
                          🔒 {T.multiCountry || "Multi-country"}
                        </span>
                      )}
                      {multiCountry.interactive && (
                      <select
                        value={holidayCountry}
                        onChange={(e) => setHolidayCountry(e.target.value)}
                        title={T.selectCountry || "Select country"}
                        aria-label={T.selectCountry || "Select country"}
                        style={{
                          padding: "8px 12px", borderRadius: 8,
                          border: `1px solid ${holidayCountry ? C.brand : C.border}`,
                          // Always the theme's primary text color — `C.brand` on
                          // `C.brandBg` is unreadably low-contrast in dark mode.
                          background: holidayCountry ? C.brandBg : C.surface,
                          color: C.text,
                          fontSize: 12, fontWeight: 600,
                          cursor: "pointer", fontFamily: "inherit",
                          maxWidth: 240, minWidth: 190,
                        }}
                      >
                        {/* `<option>` ignores most CSS in some browsers, but the
                            bg/color hints help the dark dropdown panel render. */}
                        <option value="" style={{ background: C.surface, color: C.text }}>
                          {T.allCountries || "All countries"}
                        </option>
                        {holidayCountries.map(c => (
                          <option key={c.country_code} value={c.country_code}
                                  style={{ background: C.surface, color: C.text }}>
                            {c.country_name}
                          </option>
                        ))}
                      </select>
                      )}

                      {/* Year — an independent control at last. It used to be
                          welded to the country inside one "<CC>-<YEAR>" profile
                          option, so changing the year meant leaving the page.
                          Base holiday management (holidays.primary) — All-plan,
                          never gated. */}
                      <select
                        value={holidayYear}
                        onChange={(e) => setHolidayYear(parseInt(e.target.value, 10))}
                        title={T.selectYear || "Select year"}
                        aria-label={T.selectYear || "Select year"}
                        style={{
                          padding: "8px 12px", borderRadius: 8,
                          border: `1px solid ${C.border}`, background: C.surface,
                          color: C.text, fontSize: 12, fontWeight: 600,
                          cursor: "pointer", fontFamily: "inherit",
                          fontVariantNumeric: "tabular-nums",
                        }}
                      >
                        {yearOptions.map(y => (
                          <option key={y} value={y} style={{ background: C.surface, color: C.text }}>{y}</option>
                        ))}
                      </select>

                      {holidayCountry && multiCountry.interactive && (
                        <button
                          onClick={() => setHolidayCountry("")}
                          title={T.clearFilter || "Clear filter"}
                          style={{ padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.textSub, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit", display: "flex", alignItems: "center", gap: 4 }}
                        >
                          <Icon n="x" s={12} c={C.textSub} />{T.clear || "Clear"}
                        </button>
                      )}

                      {/* CSV import. The date-format picker sits beside it
                          because `auto` deliberately REFUSES ambiguous values
                          like 03/04/2026 — the admin needs a way to say which
                          reading they meant. */}
                      <select
                        value={holidayCsvFormat}
                        onChange={(e) => setHolidayCsvFormat(e.target.value)}
                        title={T.csvDateFormat || "CSV date format"}
                        aria-label={T.csvDateFormat || "CSV date format"}
                        style={{ padding: "8px 10px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.textSub, fontSize: 11, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}
                      >
                        <option value="auto" style={{ background: C.surface, color: C.text }}>Dates: auto</option>
                        <option value="iso" style={{ background: C.surface, color: C.text }}>Dates: YYYY-MM-DD</option>
                        <option value="dmy" style={{ background: C.surface, color: C.text }}>Dates: DD/MM/YYYY</option>
                        <option value="mdy" style={{ background: C.surface, color: C.text }}>Dates: MM/DD/YYYY</option>
                      </select>
                      <label style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 14px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.textSub, fontSize: 12, fontWeight: 600, cursor: "pointer", transition: "all .15s" }}>
                        <Icon n="download" s={13} c={C.textSub} />
                        {holidayImporting ? (T.importing || "Importing…") : (T.importCsv || "Import CSV")}
                        <input type="file" accept=".csv" style={{ display: "none" }} onChange={handleCsvImport} disabled={holidayImporting} />
                      </label>
                      {/* View toggle */}
                      <div style={{ display: "flex", borderRadius: 8, border: `1px solid ${C.border}`, overflow: "hidden" }}>
                        {[["table","list",T.tableView||"Table"],["calendar","calendar",T.calendarView||"Calendar"]].map(([v,ic,lbl]) => (
                          <button key={v} onClick={() => setHolidayView(v)}
                            style={{ padding: "8px 14px", border: "none", background: holidayView === v ? C.brand : C.surface, color: holidayView === v ? "#fff" : C.textSub, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit", display: "flex", alignItems: "center", gap: 5, transition: "all .15s" }}>
                            <Icon n={ic} s={12} c={holidayView === v ? "#fff" : C.textSub} />{lbl}
                          </button>
                        ))}
                      </div>
                      <button onClick={openAdd}
                        style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 18px", borderRadius: 8, border: "none", background: C.brand, color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer", fontFamily: "inherit", boxShadow: "0 2px 8px rgba(17,73,106,.25)" }}>
                        <Icon n="plus" s={14} c="#fff" />{T.addHoliday || "Add Holiday"}
                      </button>
                    </div>
                  </div>

                  {/* CSV import result — always rendered when present, never
                      collapsed into a toast. An import that skipped rows has to
                      say which ones and why; the previous version reported
                      nothing at all when the request failed. */}
                  {holidayCsvResult && (
                    <div style={{ ...card(), padding: 18, borderLeft: `4px solid ${holidayCsvResult.error ? "#DC2626" : holidayCsvResult.imported > 0 ? "#22C55E" : "#F59E0B"}` }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
                        <div style={{ minWidth: 0 }}>
                          {holidayCsvResult.error ? (
                            <p style={{ margin: 0, fontSize: 13, fontWeight: 700, color: "#DC2626" }}>{holidayCsvResult.error}</p>
                          ) : (
                            <p style={{ margin: 0, fontSize: 13, fontWeight: 700, color: C.text }}>
                              {`Imported ${holidayCsvResult.imported} of ${holidayCsvResult.total_rows ?? holidayCsvResult.imported} rows`}
                              {holidayCsvResult.duplicates ? ` · ${holidayCsvResult.duplicates} duplicate` : ""}
                              {holidayCsvResult.invalid ? ` · ${holidayCsvResult.invalid} invalid` : ""}
                            </p>
                          )}
                          {[...(holidayCsvResult.invalid_detail || []), ...(holidayCsvResult.duplicate_detail || [])].length > 0 && (
                            <ul style={{ margin: "8px 0 0", paddingLeft: 18, color: C.textSub, fontSize: 12, maxHeight: 150, overflowY: "auto" }}>
                              {[...(holidayCsvResult.invalid_detail || []), ...(holidayCsvResult.duplicate_detail || [])].map((row, i) => (
                                <li key={`${row.line}-${i}`} style={{ marginBottom: 2 }}>
                                  <strong style={{ fontVariantNumeric: "tabular-nums" }}>Row {row.line}</strong>
                                  {row.raw ? ` (${row.raw})` : ""} — {row.error}
                                </li>
                              ))}
                            </ul>
                          )}
                        </div>
                        <button onClick={() => setHolidayCsvResult(null)}
                          style={{ background: "none", border: "none", fontSize: 18, color: C.textSub, cursor: "pointer", lineHeight: 1, flexShrink: 0 }}
                          aria-label={T.dismiss || "Dismiss"}>×</button>
                      </div>
                    </div>
                  )}

                  {/* Provider / catalog status. This is a BANNER, not a page
                      state: it sits above the table instead of replacing it.
                      The previous error branch short-circuited the whole view,
                      so one unsupported country hid the organization's own
                      holidays AND disabled the Table/Calendar toggle until a
                      full page reload. */}
                  {holidayCountryError && (
                    <div style={{
                      ...card(), padding: "14px 18px",
                      borderLeft: `4px solid ${holidayCountryError.kind === holidayApi.HOLIDAY_ERROR.UNSUPPORTED_COUNTRY ? "#F59E0B" : "#DC2626"}`,
                      display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12,
                    }}>
                      <div style={{ minWidth: 0 }}>
                        <p style={{
                          margin: 0, fontSize: 13, fontWeight: 700,
                          color: holidayCountryError.kind === holidayApi.HOLIDAY_ERROR.UNSUPPORTED_COUNTRY ? "#B45309" : "#DC2626",
                        }}>
                          {holidayCountryError.message}
                        </p>
                        <p style={{ margin: "4px 0 0", fontSize: 12, color: C.textSub }}>
                          {/* The organization's own holidays are unaffected by a
                              provider problem, and they are still on screen. */}
                          {T.orgHolidaysStillShown
                            || "Your organization's own holidays for this selection are still shown below."}
                        </p>
                      </div>
                      <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                        {/* Retry only for transient failures — offering it for
                            an unsupported country would be a lie. */}
                        {isRetryableHolidayError(holidayCountryError.kind) && (
                          <button onClick={() => fetchHolidaysForCountry(holidayCountry, holidayYear)}
                            style={{ padding: "6px 14px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}>
                            {T.retry || "Retry"}
                          </button>
                        )}
                        <button onClick={() => setHolidayCountryError(null)}
                          style={{ background: "none", border: "none", fontSize: 18, color: C.textSub, cursor: "pointer", lineHeight: 1 }}
                          aria-label={T.dismiss || "Dismiss"}>×</button>
                      </div>
                    </div>
                  )}

                  {/* Adopt banner. States exactly what "Add all" will and will
                      not do — conflicts and already-covered entries are never
                      part of it, and the button says how many it adds. */}
                  {isCountryMode && !holidayCountryError && holidayPlan && (addableRows.length > 0 || conflictRows.length > 0) && (
                    <div style={{ ...card(), padding: "14px 18px", borderLeft: `4px solid ${addableRows.length ? C.brand : "#F59E0B"}`, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                      <div style={{ minWidth: 0 }}>
                        <p style={{ margin: 0, fontSize: 13, fontWeight: 700, color: C.text }}>
                          {addableRows.length > 0
                            ? `${addableRows.length} new ${selectedCountryName} holiday${addableRows.length === 1 ? "" : "s"} for ${holidayYear}`
                            : `Nothing new to add for ${selectedCountryName} ${holidayYear}`}
                        </p>
                        <p style={{ margin: "4px 0 0", fontSize: 12, color: C.textSub }}>
                          {(counts.alreadyCovered + counts.exactDuplicates) > 0 && (
                            <>{counts.alreadyCovered + counts.exactDuplicates} already in your calendar and will be skipped. </>
                          )}
                          {conflictRows.length > 0 && (
                            <strong style={{ color: "#B45309" }}>
                              {conflictRows.length} need{conflictRows.length === 1 ? "s" : ""} your review and will NOT be added automatically.{" "}
                            </strong>
                          )}
                          {counts.notEligible > 0 && (
                            <>{counts.notEligible} optional/regional, excluded by the toggles below. </>
                          )}
                          {holidayCatalogInfo?.partial && "Only fixed-date holidays are available for this year."}
                        </p>
                        {/* Optional / regional are separate axes — a restricted
                            holiday and a state-only one are different things,
                            and each gets its own opt-in. */}
                        <div style={{ display: "flex", gap: 16, marginTop: 8, flexWrap: "wrap" }}>
                          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: C.textSub, cursor: "pointer" }}>
                            <input type="checkbox" checked={holidayIncludeOptional}
                              onChange={(e) => setHolidayIncludeOptional(e.target.checked)} />
                            {T.includeOptional || "Include optional / restricted days"}
                          </label>
                          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: C.textSub, cursor: "pointer" }}>
                            <input type="checkbox" checked={holidayIncludeRegional}
                              onChange={(e) => setHolidayIncludeRegional(e.target.checked)} />
                            {T.includeRegional || "Include regional days (not observed nationwide)"}
                          </label>
                        </div>
                      </div>
                      {addableRows.length > 0 && (
                        <button onClick={adoptAll} disabled={holidayAdopting}
                          style={{ padding: "8px 18px", borderRadius: 8, border: "none", background: C.brand, color: "#fff", fontSize: 13, fontWeight: 700, cursor: holidayAdopting ? "default" : "pointer", fontFamily: "inherit", opacity: holidayAdopting ? 0.7 : 1, flexShrink: 0 }}>
                          {holidayAdopting ? (T.addingDots || "Adding…") : `Add ${addableRows.length} to my calendar`}
                        </button>
                      )}
                    </div>
                  )}

                  {(holidayLoading || holidayCountryLoading) ? (
                    <div style={{ ...card(), padding: 60, textAlign: "center" }}>
                      <div style={{ width: 32, height: 32, borderRadius: "50%", border: `3px solid ${C.brand}`, borderTopColor: "transparent", animation: "spin .8s linear infinite", margin: "0 auto 12px" }} />
                      <p style={{ color: C.textSub, fontSize: 13 }}>{T.loadingHolidays || "Loading holidays…"}</p>
                    </div>
                  ) : holidayView === "table" ? (
                    /* ── TABLE VIEW ── */
                    <div style={{ ...card(), overflow: "hidden" }}>
                      {visibleHolidays.length === 0 ? (
                        <div style={{ padding: 60, textAlign: "center" }}>
                          <div style={{ width: 56, height: 56, borderRadius: "50%", background: C.brandBg, display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 14px" }}>
                            <Icon n="tag" s={24} c={C.brand} />
                          </div>
                          {/* "Nothing found" and "could not load" are different
                              answers and now read differently. This branch only
                              renders when there was no error. */}
                          <p style={{ fontWeight: 700, color: C.text, fontSize: 15 }}>
                            {isCountryMode
                              ? `No ${selectedCountryName} holidays for ${holidayYear}`
                              : `${T.noHolidaysYet || "No holidays yet"} for ${holidayYear}`}
                          </p>
                          <p style={{ color: C.textSub, fontSize: 12, marginTop: 4 }}>
                            {isCountryMode
                              ? (T.tryDifferentCountryYear || "Try a different country or year, or add a holiday manually.")
                              : (T.noHolidaysHint || 'Click "Add Holiday" to get started or import a CSV file.')}
                          </p>
                        </div>
                      ) : (
                        <div style={{ overflowX: "auto" }}>
                        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                          <thead>
                            <tr style={{ background: isDark ? "#1F2937" : "#F8FAFC" }}>
                              {[T.holiday||"Holiday", T.date||"Date", T.day||"Day", T.typeCol||"Type", T.countryCol||"Country", T.description||"Description", T.actionsCol||"Actions"].map(h => (
                                <th key={h} style={{ padding: "13px 18px", textAlign: "left", fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: "0.7px", borderBottom: `1px solid ${C.border}`, whiteSpace: "nowrap" }}>{h}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {visibleHolidays.map((h) => {
                              const tc = getTypeStyle(h.holiday_type);
                              return (
                                <tr key={h.id} style={{ borderBottom: `1px solid ${C.borderLight}`, transition: "background .1s", opacity: h._readonly ? 0.75 : 1 }}
                                  onMouseEnter={e => e.currentTarget.style.background = isDark ? "#1F2937" : "#F9FAFB"}
                                  onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
                                  <td style={{ padding: "13px 18px", fontWeight: 700, color: C.text }}>
                                    {h.name}
                                    {h.estimated && (
                                      <span title={T.estimatedDateHint || "Set by moon sighting — confirm the exact date locally."}
                                        style={{ marginLeft: 6, fontSize: 9, fontWeight: 700, background: "#FEF3C7", color: "#92400E", border: "1px solid #FDE68A", padding: "2px 6px", borderRadius: 20 }}>
                                        {T.estimated || "Estimated"}
                                      </span>
                                    )}
                                  </td>
                                  {/* Locale-aware, replacing a hardcoded en-IN
                                      that formatted Indian-style dates for every
                                      workspace whatever language it had picked. */}
                                  <td style={{ padding: "13px 18px", color: C.textSub, fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>
                                    {holidayApi.formatHolidayDate(h.date, lang)}
                                  </td>
                                  <td style={{ padding: "13px 18px", color: C.textSub }}>
                                    {holidayApi.formatHolidayWeekday(h.date, lang, h.day)}
                                  </td>
                                  <td style={{ padding: "13px 18px" }}>
                                    <span style={{ fontSize: 10, fontWeight: 700, background: tc.bg, color: tc.text, border: `1px solid ${tc.border}`, padding: "3px 10px", borderRadius: 20 }}>{h.holiday_type}</span>
                                  </td>
                                  <td style={{ padding: "13px 18px", color: C.textSub, whiteSpace: "nowrap" }}>
                                    {h.country_code || (h._readonly ? holidayCountry : "—")}
                                  </td>
                                  <td style={{ padding: "13px 18px", color: C.textSub, maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{h.description || "—"}</td>
                                  <td style={{ padding: "13px 18px" }}>
                                    {/* Preview rows have no row in our database
                                        yet. What they offer depends on how the
                                        server classified them — an Add button
                                        only where adding is actually safe. */}
                                    {h._readonly ? (() => {
                                      const st = adoptRowState(h);
                                      if (st.action === "add") {
                                        return (
                                          <button onClick={() => adoptOne(h)}
                                            disabled={holidayAdopting}
                                            title={T.addThisHoliday || "Add this holiday to your calendar"}
                                            style={{ padding: "5px 14px", borderRadius: 6, border: "none", background: C.brand, color: "#fff", fontSize: 11, fontWeight: 700, cursor: holidayAdopting ? "default" : "pointer", fontFamily: "inherit", opacity: holidayAdopting ? 0.6 : 1, whiteSpace: "nowrap" }}>
                                            {holidayRowBusy === h.key ? (T.addingDots || "Adding…") : (T.add || "Add")}
                                          </button>
                                        );
                                      }
                                      if (st.action === "review") {
                                        return (
                                          <button onClick={() => setHolidayConflict(h)}
                                            title={h.reason}
                                            style={{ padding: "5px 12px", borderRadius: 6, border: "1px solid #FDE68A", background: "#FEF9C3", color: "#B45309", fontSize: 11, fontWeight: 700, cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap" }}>
                                            {T.reviewConflict || "Review conflict"}
                                          </button>
                                        );
                                      }
                                      return (
                                        <span title={h.reason}
                                          style={{ fontSize: 10, fontWeight: 700, background: isDark ? "#374151" : "#F3F4F6", color: C.textSub, border: `1px solid ${C.border}`, padding: "3px 10px", borderRadius: 20, whiteSpace: "nowrap" }}>
                                          {st.label}
                                        </span>
                                      );
                                    })() : (
                                      <div style={{ display: "flex", gap: 6 }}>
                                        <button onClick={() => openEdit(h)}
                                          style={{ padding: "5px 12px", borderRadius: 6, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 11, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}>{T.edit || "Edit"}</button>
                                        <button onClick={() => { setHolidayDelErr(""); setHolidayDelId(h.id); }}
                                          style={{ padding: "5px 12px", borderRadius: 6, border: "1px solid #FCA5A5", background: "#FEF2F2", color: "#DC2626", fontSize: 11, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}>{T.delete || "Delete"}</button>
                                      </div>
                                    )}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                        </div>
                      )}
                    </div>
                  ) : (
                    /* ── CALENDAR VIEW ── */
                    <div style={{ ...card(), padding: 24 }}>
                      {/* Month nav */}
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
                        <button onClick={() => setHolidayCalMonth(p => { const d = new Date(p.y, p.m - 1); return { y: d.getFullYear(), m: d.getMonth() }; })}
                          style={{ padding: "6px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, cursor: "pointer", fontSize: 18, fontFamily: "inherit", lineHeight: 1 }}>‹</button>
                        <span style={{ fontSize: 16, fontWeight: 800, color: C.text }}>
                          {holidayApi.formatMonthLabel(calY, calM, lang)}
                        </span>
                        <button onClick={() => setHolidayCalMonth(p => { const d = new Date(p.y, p.m + 1); return { y: d.getFullYear(), m: d.getMonth() }; })}
                          style={{ padding: "6px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, cursor: "pointer", fontSize: 18, fontFamily: "inherit", lineHeight: 1 }}>›</button>
                      </div>
                      {/* Day headers */}
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(7,1fr)", gap: 4, marginBottom: 4 }}>
                        {[T.adminDashboardSun || "Sun", T.adminDashboardMon || "Mon", T.adminDashboardTue || "Tue", T.adminDashboardWed || "Wed", T.adminDashboardThu || "Thu", T.adminDashboardFri || "Fri", T.adminDashboardSat || "Sat"].map(d => (
                          <div key={d} style={{ textAlign: "center", fontSize: 11, fontWeight: 700, color: C.textSub, padding: "6px 0", textTransform: "uppercase", letterSpacing: "0.5px" }}>{d}</div>
                        ))}
                      </div>
                      {/* Calendar grid */}
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(7,1fr)", gap: 4 }}>
                        {calendarCells.map((d, idx) => {
                          if (!d) return <div key={`e${idx}`} />;
                          const dateStr = fmtCalDate(d);
                          // Reads the SAME `visibleHolidays` array the table
                          // renders — one dataset, one fetch, so the two views
                          // cannot show different answers for one selection.
                          // A date may hold several holidays (Gandhi Jayanti and
                          // Dussehra both fell on 2025-10-02), so this collects
                          // them all rather than taking the first.
                          const dayHols = visibleHolidays.filter(h => h.date === dateStr);
                          const hol = dayHols[0];
                          const today = new Date();
                          const isToday = today.getFullYear() === calY && today.getMonth() === calM && today.getDate() === d;
                          const tc = hol ? getTypeStyle(hol.holiday_type) : null;
                          return (
                            <div key={d}
                              title={dayHols.map(h => `${h.name} (${h.holiday_type})${h._readonly ? " — not added" : ""}`).join("\n")}
                              style={{ minHeight: 68, borderRadius: 10, padding: "8px 6px", background: hol ? tc.bg : isToday ? C.brandBg : isDark ? "#1F2937" : "#F9FAFB", border: `1.5px solid ${hol ? tc.border : isToday ? C.brand : C.border}`, borderStyle: hol?._readonly ? "dashed" : "solid", transition: "all .15s", cursor: hol ? "pointer" : "default" }}>
                              <div style={{ fontSize: 13, fontWeight: isToday || hol ? 800 : 500, color: hol ? tc.text : isToday ? C.brand : C.text, marginBottom: 4 }}>{d}</div>
                              {dayHols.slice(0, 2).map((h) => (
                                <div key={h.id}>
                                  <div style={{ fontSize: 10, fontWeight: 700, color: tc.text, lineHeight: 1.3, wordBreak: "break-word" }}>{h.name}</div>
                                  <div style={{ fontSize: 9, color: tc.text, opacity: 0.7, marginTop: 2 }}>{h.holiday_type}</div>
                                </div>
                              ))}
                              {dayHols.length > 2 && (
                                <div style={{ fontSize: 9, color: tc.text, opacity: 0.8, marginTop: 2 }}>
                                  +{dayHols.length - 2} more
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                      {/* Legend */}
                      <div style={{ display: "flex", gap: 16, marginTop: 20, flexWrap: "wrap", alignItems: "center" }}>
                        {Object.entries(TYPE_COLORS).map(([t, c]) => (
                          <div key={t} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: C.textSub }}>
                            <span style={{ width: 12, height: 12, borderRadius: 3, background: c.bg, border: `1.5px solid ${c.border}`, display: "inline-block" }} />
                            {t}
                          </div>
                        ))}
                        {previewRows.length > 0 && (
                          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: C.textSub }}>
                            <span style={{ width: 12, height: 12, borderRadius: 3, background: "transparent", border: `1.5px dashed ${C.textSub}`, display: "inline-block" }} />
                            {T.notAddedYet || "Not added"}
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                  {/* ── ADD / EDIT MODAL ── */}
                  {(holidayModal === "add" || holidayModal === "edit") && (
                    <div style={modalBg} onClick={(e) => { if (e.target === e.currentTarget) setHolidayModal(null); }}>
                      <div style={modalBox}>
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
                          <h3 style={{ fontSize: 16, fontWeight: 800, color: C.text, margin: 0 }}>{holidayModal === "edit" ? (T.editHoliday || "Edit Holiday") : (T.addHoliday || "Add Holiday")}</h3>
                          <button onClick={() => setHolidayModal(null)} style={{ background: "none", border: "none", fontSize: 20, color: C.textSub, cursor: "pointer", lineHeight: 1 }}>×</button>
                        </div>
                        {holidayFormErr && (
                          <div style={{ background: "#FEF2F2", border: "1px solid #FCA5A5", color: "#DC2626", padding: "10px 14px", borderRadius: 8, fontSize: 12, marginBottom: 14 }}>{holidayFormErr}</div>
                        )}
                        <div style={fieldGap}>
                          <label style={labelSt}>{T.holidayName || "Holiday Name"} *</label>
                          <input style={inputSt} placeholder={T.exampleRepublicDay || "e.g. Republic Day"} value={holidayForm.name}
                            onChange={e => setHolidayForm(p => ({ ...p, name: e.target.value }))} />
                        </div>
                        <div style={fieldGap}>
                          <label style={labelSt}>{T.date || "Date"} *</label>
                          <input type="date" style={inputSt} value={holidayForm.date}
                            onChange={e => setHolidayForm(p => ({ ...p, date: e.target.value }))} />
                        </div>
                        {/* Country — the field the schema could not hold before.
                            Defaults to whatever calendar the admin is viewing.
                            "No specific country" writes NULL, meaning the
                            holiday applies to every calendar in the workspace —
                            the right choice for a company-wide shutdown day. */}
                        <div style={fieldGap}>
                          <label style={labelSt}>{T.country || "Country"}</label>
                          <select style={inputSt} value={holidayForm.country_code}
                            onChange={e => setHolidayForm(p => ({ ...p, country_code: e.target.value }))}>
                            <option value="">{T.noSpecificCountry || "No specific country (applies to everyone)"}</option>
                            {holidayCountries.map(c => (
                              <option key={c.country_code} value={c.country_code}>{c.country_name}</option>
                            ))}
                          </select>
                        </div>
                        <div style={fieldGap}>
                          <label style={labelSt}>{T.holidayType || "Holiday Type"}</label>
                          <select style={inputSt} value={holidayForm.holiday_type}
                            onChange={e => setHolidayForm(p => ({ ...p, holiday_type: e.target.value }))}>
                            <option value="Public">{T.publicHoliday || "Public Holiday"}</option>
                            <option value="Company">{T.companyHoliday || "Company Holiday"}</option>
                            <option value="Optional">{T.optionalHoliday || "Optional Holiday"}</option>
                          </select>
                        </div>
                        <div style={fieldGap}>
                          <label style={labelSt}>{T.descriptionOptional || "Description (optional)"}</label>
                          <textarea style={{ ...inputSt, resize: "vertical", minHeight: 72 }} rows={3}
                            placeholder={T.briefHolidayDescription || "Brief description of the holiday…"} value={holidayForm.description}
                            onChange={e => setHolidayForm(p => ({ ...p, description: e.target.value }))} />
                        </div>
                        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 6 }}>
                          <button onClick={() => setHolidayModal(null)}
                            style={{ padding: "10px 20px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}>{T.cancel || "Cancel"}</button>
                          <button onClick={saveHoliday} disabled={holidaySaving}
                            style={{ padding: "10px 22px", borderRadius: 8, border: "none", background: C.brand, color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer", fontFamily: "inherit", opacity: holidaySaving ? 0.7 : 1 }}>
                            {holidaySaving ? (T.savingDots || "Saving…") : holidayModal === "edit" ? (T.saveChanges || "Save Changes") : (T.addHoliday || "Add Holiday")}
                          </button>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* ── DATE CONFLICT REVIEW ── */}
                  {/* The organization already has something on (or beside) this
                      date. Adding anyway can create a SECOND non-working day —
                      the Bakrid case — so it is never the default and never
                      happens without the admin choosing it here. */}
                  {holidayConflict && (
                    <div style={modalBg} onClick={(e) => { if (e.target === e.currentTarget) setHolidayConflict(null); }}>
                      <div style={{ ...modalBox, maxWidth: 560 }}>
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
                          <h3 style={{ fontSize: 16, fontWeight: 800, color: C.text, margin: 0 }}>
                            {T.reviewDateConflict || "Review date conflict"}
                          </h3>
                          <button onClick={() => setHolidayConflict(null)} style={{ background: "none", border: "none", fontSize: 20, color: C.textSub, cursor: "pointer", lineHeight: 1 }}>×</button>
                        </div>

                        <div style={{ background: "#FEF9C3", border: "1px solid #FDE68A", borderRadius: 8, padding: "12px 14px", fontSize: 12.5, color: "#92400E", marginBottom: 16 }}>
                          {holidayConflict.reason}
                        </div>

                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 18 }}>
                          <div style={{ border: `1px solid ${C.border}`, borderRadius: 8, padding: 12 }}>
                            <p style={{ margin: 0, fontSize: 10, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: ".6px" }}>
                              {T.inYourCalendar || "In your calendar"}
                            </p>
                            <p style={{ margin: "6px 0 0", fontSize: 13, fontWeight: 700, color: C.text }}>
                              {holidayConflict.existing?.name || "—"}
                            </p>
                            <p style={{ margin: "2px 0 0", fontSize: 12, color: C.textSub, fontVariantNumeric: "tabular-nums" }}>
                              {holidayApi.formatHolidayDate(holidayConflict.existing?.date, lang)}
                            </p>
                          </div>
                          <div style={{ border: `1px solid ${C.brandBdr}`, background: C.brandBg, borderRadius: 8, padding: 12 }}>
                            <p style={{ margin: 0, fontSize: 10, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: ".6px" }}>
                              {T.fromCatalog || "From the catalog"}
                            </p>
                            <p style={{ margin: "6px 0 0", fontSize: 13, fontWeight: 700, color: C.text }}>
                              {holidayConflict.name}
                              {holidayConflict.estimated && (
                                <span style={{ marginLeft: 6, fontSize: 9, fontWeight: 700, background: "#FEF3C7", color: "#92400E", border: "1px solid #FDE68A", padding: "2px 6px", borderRadius: 20 }}>
                                  {T.estimated || "Estimated"}
                                </span>
                              )}
                            </p>
                            <p style={{ margin: "2px 0 0", fontSize: 12, color: C.textSub, fontVariantNumeric: "tabular-nums" }}>
                              {holidayApi.formatHolidayDate(holidayConflict.date, lang)}
                            </p>
                          </div>
                        </div>

                        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                          <button onClick={() => resolveConflict(holidayConflict, "skip")}
                            style={{ textAlign: "left", padding: "12px 14px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, cursor: "pointer", fontFamily: "inherit" }}>
                            <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>{T.keepMine || "Keep mine"}</div>
                            <div style={{ fontSize: 12, color: C.textSub, marginTop: 2 }}>
                              {T.keepMineHint || "Leave your calendar unchanged. Recommended when your date is the one your staff already plan around."}
                            </div>
                          </button>
                          <button onClick={() => resolveConflict(holidayConflict, "replace")}
                            style={{ textAlign: "left", padding: "12px 14px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, cursor: "pointer", fontFamily: "inherit" }}>
                            <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>{T.useCatalogDate || "Use the catalog version"}</div>
                            <div style={{ fontSize: 12, color: C.textSub, marginTop: 2 }}>
                              {T.useCatalogDateHint || "Move your existing holiday to the catalog's name and date. Still one non-working day."}
                            </div>
                          </button>
                          <button
                            onClick={() => {
                              const ok = window.confirm(
                                `This keeps "${holidayConflict.existing?.name}" on ${holidayConflict.existing?.date} AND adds `
                                + `"${holidayConflict.name}" on ${holidayConflict.date}.\n\n`
                                + "Your organization will have TWO non-working days for this. "
                                + "Attendance, leave cost and payroll will all treat both as holidays.\n\nContinue?"
                              );
                              if (ok) resolveConflict(holidayConflict, "add_anyway");
                            }}
                            style={{ textAlign: "left", padding: "12px 14px", borderRadius: 8, border: "1px solid #FCA5A5", background: "#FEF2F2", cursor: "pointer", fontFamily: "inherit" }}>
                            <div style={{ fontSize: 13, fontWeight: 700, color: "#DC2626" }}>{T.addBothDays || "Add both days"}</div>
                            <div style={{ fontSize: 12, color: "#B91C1C", marginTop: 2 }}>
                              {T.addBothDaysHint || "Creates a second non-working day. Only correct if these really are two different holidays."}
                            </div>
                          </button>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* ── DELETE CONFIRM MODAL ── */}
                  {holidayDelId && (
                    <div style={modalBg} onClick={(e) => { if (e.target === e.currentTarget) setHolidayDelId(null); }}>
                      <div style={{ ...modalBox, maxWidth: 400, textAlign: "center" }}>
                        <div style={{ width: 52, height: 52, borderRadius: "50%", background: "#FEF2F2", border: "1px solid #FCA5A5", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 14px" }}>
                          <Icon n="trash" s={22} c="#DC2626" />
                        </div>
                        <h3 style={{ fontSize: 16, fontWeight: 800, color: C.text, marginBottom: 8 }}>{T.deleteHolidayConfirm || "Delete Holiday?"}</h3>
                        <p style={{ fontSize: 13, color: C.textSub, marginBottom: holidayDelErr ? 12 : 24 }}>
                          {(() => { const h = holidays.find(x => x.id === holidayDelId); return h ? `"${h.name}" ${T.willBePermanentlyRemoved || "will be permanently removed."}` : (T.holidayWillBeRemoved || "This holiday will be permanently removed."); })()}
                        </p>
                        {/* A failed delete now says so. Previously the request
                            was fired without checking the result, and the
                            backend answered 200 even when it deleted nothing. */}
                        {holidayDelErr && (
                          <div style={{ background: "#FEF2F2", border: "1px solid #FCA5A5", color: "#DC2626", padding: "10px 14px", borderRadius: 8, fontSize: 12, marginBottom: 18, textAlign: "left" }}>
                            {holidayDelErr}
                          </div>
                        )}
                        <div style={{ display: "flex", gap: 10, justifyContent: "center" }}>
                          <button onClick={() => { setHolidayDelId(null); setHolidayDelErr(""); }}
                            style={{ padding: "10px 22px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}>{T.cancel || "Cancel"}</button>
                          <button onClick={confirmDelete} disabled={holidayDeleting}
                            style={{ padding: "10px 22px", borderRadius: 8, border: "none", background: "#DC2626", color: "#fff", fontSize: 13, fontWeight: 700, cursor: holidayDeleting ? "default" : "pointer", fontFamily: "inherit", opacity: holidayDeleting ? 0.7 : 1 }}>
                            {holidayDeleting ? (T.deletingDots || "Deleting…") : (T.delete || "Delete")}
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })()}

            {/* ── Time Playback (timelapse) ── */}
            {tab === "timelapse" && (
              <Suspense fallback={<TabFallback />}>
                <TimelapseTab
                  C={C} T={T} isDark={isDark} Icon={Icon} card={card} employeesAlpha={employeesAlpha} token={token}
                  tlDate={tlDate} setTlDate={setTlDate} tlEmpId={tlEmpId} setTlEmpId={setTlEmpId}
                  tlShots={tlShots} setTlShots={setTlShots} tlLoading={tlLoading} setTlLoading={setTlLoading}
                  tlLightbox={tlLightbox} setTlLightbox={setTlLightbox} tlDelShot={tlDelShot} setTlDelShot={setTlDelShot}
                  tlDelVideo={tlDelVideo} setTlDelVideo={setTlDelVideo}
                  tlVideoUrl={tlVideoUrl} setTlVideoUrl={setTlVideoUrl} tlVideoMeta={tlVideoMeta} setTlVideoMeta={setTlVideoMeta}
                  tlVideoLoading={tlVideoLoading} setTlVideoLoading={setTlVideoLoading} tlVideoError={tlVideoError} setTlVideoError={setTlVideoError}
                />
              </Suspense>
            )}

            {tab === "billing" && (
              <Suspense fallback={<TabFallback />}>
                <BillingModule />
              </Suspense>
            )}

            {/* Website Categories is privacy-denied for every plan
                (productivity.website_categories ∈ GLOBALLY_DENIED). Backend
                projection → HIDE, so the Gate renders nothing (never an upsell),
                mirroring the sidebar and closing the deep-link gap. */}
            {tab === "browsercategories" && (
              <Gate capability="productivity.website_categories">
                <Suspense fallback={<TabFallback />}>
                  <BrowserCategoriesTab
                    C={C} T={T} isDark={isDark} Icon={Icon} initials={initials} card={card} search={search}
                    browserLive={browserLive} employees={employees}
                    siteUsageData={siteUsageData} siteUsageLoading={siteUsageLoading}
                    siteUsageEmp={siteUsageEmp} setSiteUsageEmp={setSiteUsageEmp}
                    siteUsageDate={siteUsageDate} setSiteUsageDate={setSiteUsageDate}
                    bcOverrides={bcOverrides} bcDefaults={bcDefaults}
                    fetchSiteUsage={fetchSiteUsage} fetchBrowserLive={fetchBrowserLive}
                  />
                </Suspense>
              </Gate>
            )}

          </div>
        </main>
      </div>

      {/* KAIROS floating assistant — bottom-right corner, default anchor.
          AdminDashboard has no floating timer pill so the default offset is fine. */}
      <FloatingAssistantBot />
    </>
  );
}
