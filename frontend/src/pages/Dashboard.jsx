import React, {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  lazy,
  Suspense,
} from "react";
import { getOrgTimeZone } from "../lib/orgTime";
import { useNavigate } from "react-router-dom";
import TemverisLogo from "../assets/Temveris_Logo_PNG.png";
import TemverisLogoWhite from "../assets/Temveris_Logo_White.svg";
import { useWorkspace } from "../context/WorkspaceContext";
import EarlyClockOutModal, {
  EARLY_CLOCK_OUT_ENABLED,
  EARLY_REASON_MIN,
  EARLY_REASON_MAX,
} from "../components/EarlyClockOutModal";
import {
  API_BASE,
  WS_URL,
  WEB_ATTENDANCE_ACTIONS_ENABLED,
  PUBLIC_ORIGIN,
  fetchWithRetry,
} from "../config";
import { getBreakThresholds, getMe, invalidateMe } from "../api/session";
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import { useAuth } from "../context/AuthContext";
import LangSwitcherComponent from "../components/LangSwitcher";
import FloatingAssistantBot from "../components/FloatingAssistantBot";
import DownloadAppButton from "../components/DownloadAppButton";
import { refreshAccessToken } from "../api/refresh";
import AuthImage from "../components/admin/AuthImage";

// Tab modules are conditionally rendered by `tab === "..."` further down.
// Code-split each one so the Dashboard route chunk (413 KB before this) only
// has to ship the default "today" tab on first paint.
const LeavePage = lazy(() => import("./LeavePage"));
const ActivityLog = lazy(() => import("../components/ActivityLog"));
const MyScheduleTab = lazy(() => import("../components/MyScheduleTab"));
// `ProfileContent` is a named export; React.lazy only supports default exports
// so we adapt the shape here. Same trick is used elsewhere when chunks need to
// re-export a non-default member.
const ProfileContent = lazy(() =>
  import("./Profile").then((m) => ({ default: m.ProfileContent })),
);
const MyDataModule = lazy(() => import("../components/MyDataModule"));
const NotificationsModule = lazy(
  () => import("../components/NotificationsModule"),
);
const HelpModule = lazy(() => import("../components/HelpModule"));
const SettingsModule = lazy(() => import("../components/SettingsModule"));
// recharts (~321KB vendor-charts) is only needed on the AI Daily Summary tab
// (`tab === "ai-summary"`), which is NOT the default view. Code-split the whole
// view (charts + its leaf cards) so the Dashboard route chunk no longer eagerly
// downloads recharts on first paint.
const DailySummaryView = lazy(
  () => import("../components/DashboardSummaryView"),
);

// Shown instead of the Clock In button when the backend reports
// `desktop_clock_in_required` for this worker. Mirrors USER_MESSAGE in
// backend/app/attendance_desktop_policy.py — the server sends its own copy of
// the text, so this is only the fallback when a response has no message.
const DESKTOP_APP_CLOCK_IN_MSG =
  "Please clock in using the Temveris Desktop App.";

const TabFallback = () => (
  <div style={{ padding: 24, color: "#11496A", fontSize: 13, fontWeight: 600 }}>
    Loading…
  </div>
);

// ─── HELPERS ────────────────────────────────────────────────────────────────────
const fmtClock = (s) => {
  if (!s || s < 0) s = 0;
  return `${String(Math.floor(s / 3600)).padStart(2, "0")}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
const fmtHM = (s) => {
  if (!s || s < 0) s = 0;
  const h = Math.floor(s / 3600),
    m = Math.floor((s % 3600) / 60);
  return `${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m`;
};

// ─── DESKTOP-TOOL CLASSIFICATION ──────────────────────────────────────────────
// The tracker reports the foreground process name (Activity.app_name). Websites
// are already shown by the browser/domain views, so the "Top Tools" widget
// excludes browsers and pure-noise processes and shows the desktop applications
// the user actually works in (IDEs, Office, design tools, etc.).
const BROWSER_PROCS = new Set([
  "chrome", "google chrome", "msedge", "microsoft edge", "edge", "brave",
  "brave browser", "firefox", "mozilla firefox", "opera", "opera gx", "safari",
  "iexplore", "internet explorer", "vivaldi", "chromium", "arc",
]);
const NON_TOOL_PROCS = new Set([
  "idle", "desktop", "lockapp", "lock screen", "searchhost",
  "startmenuexperiencehost", "shellexperiencehost", "applicationframehost",
  "unknown", "",
  // OS shell / host / background noise.
  "pickerhost", "textinputhost", "systemsettings", "dwm", "sihost", "ctfmon",
  "runtimebroker", "fontdrvhost", "dllhost", "smartscreen",
  "securityhealthsystray", "mcuicnt",
]);
// Friendly display names for common process names. Eclipse on Windows fronts as
// the JVM (javaw), so map the JVM family onto Eclipse.
const TOOL_LABELS = {
  javaw: "Eclipse / Java", java: "Eclipse / Java", eclipse: "Eclipse",
  code: "VS Code", "code - insiders": "VS Code", cursor: "Cursor",
  devenv: "Visual Studio", idea64: "IntelliJ IDEA", idea: "IntelliJ IDEA",
  pycharm64: "PyCharm", pycharm: "PyCharm", webstorm64: "WebStorm",
  studio64: "Android Studio", opencode: "OpenCode", antigravity: "Antigravity",
  "antigravity ide": "Antigravity", electron: "Electron",
  winword: "Word", "microsoft word": "Word", excel: "Excel",
  powerpnt: "PowerPoint", outlook: "Outlook", onenote: "OneNote",
  photoshop: "Photoshop", illustrator: "Illustrator", figma: "Figma",
  postman: "Postman", slack: "Slack", teams: "Microsoft Teams",
  "ms-teams": "Microsoft Teams", zoom: "Zoom", explorer: "File Explorer",
  "whatsapp.root": "WhatsApp", terminal: "Terminal", windowsterminal: "Terminal",
};
const normalizeProc = (raw) =>
  (raw || "").trim().toLowerCase().replace(/\.exe$/, "");
const prettyApp = (raw) => {
  const key = normalizeProc(raw);
  return TOOL_LABELS[key] || (raw || "").trim() || "Unknown";
};
const isDesktopTool = (raw) => {
  const key = normalizeProc(raw);
  return key && !BROWSER_PROCS.has(key) && !NON_TOOL_PROCS.has(key);
};

// Live work / break timer (seconds granularity). Isolated as its own memoized
// leaf so the per-second tick re-renders ONLY this tiny component instead of the
// entire Dashboard tree. It derives the value from the parent's STABLE timing
// refs (refs never change identity, so props don't churn), using the exact same
// math the parent interval used — identical numbers, ~1/60th of the render work.
const LiveWorkedClock = React.memo(function LiveWorkedClock({
  mode, // "worked" | "break"
  clockInMsRef,
  totalBreakMsRef,
  breakStartMsRef,
  prevWorkedSecsRef,
}) {
  // Compute inside the interval (NOT during render) and store the result in
  // state, so render stays pure — same approach ElectronDashboard's clock uses.
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    const compute = () => {
      const ms = Date.now();
      const brk =
        totalBreakMsRef.current +
        (breakStartMsRef.current !== null ? ms - breakStartMsRef.current : 0);
      if (mode === "break") {
        setSecs(Math.floor(brk / 1000));
        return;
      }
      const total = ms - (clockInMsRef.current || ms);
      let sessionSecs = Math.floor(Math.max(0, total - brk) / 1000);
      const MAX_SESSION_SECS = 14 * 3600; // mirror the parent's sanity cap
      if (sessionSecs > MAX_SESSION_SECS) sessionSecs = MAX_SESSION_SECS;
      setSecs(prevWorkedSecsRef.current + sessionSecs);
    };
    compute(); // immediate paint on mount / mode change
    const id = setInterval(compute, 1000);
    return () => clearInterval(id);
    // Timing refs are stable identities; only `mode` changes behaviour.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);
  return <>{fmtClock(secs)}</>;
});
// Defensive against legacy "+00:00Z" timestamps (invalid ISO 8601 — would
// produce Invalid Date). Strips the redundant trailing 'Z' before parsing.
const safeDate = (utcIso) => {
  let v = String(utcIso);
  if (v.endsWith("+00:00Z")) v = v.slice(0, -1);
  return new Date(v);
};

const fmtTimeIST = (utcIso) => {
  if (!utcIso) return "—";
  try {
    return safeDate(utcIso).toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
      timeZone: getOrgTimeZone(),
    });
  } catch {
    return "—";
  }
};

// Reads from both storage tiers so sessions work regardless of whether
// "Remember for 30 days" was checked at login (localStorage) or not (sessionStorage).
const getToken = () =>
  localStorage.getItem("zt_token") ||
  sessionStorage.getItem("zt_token") ||
  localStorage.getItem("token") ||
  sessionStorage.getItem("token") ||
  null;

// When a break passes its allowance the worker gets a blocking modal. If they
// dismiss it with "Later" while still over, it re-appears after this interval
// so an over-limit break can never be silently ignored.
const OVERRUN_NAG_MS_WEB = 2 * 60 * 1000;

// ─── ICON PATHS ─────────────────────────────────────────────────────────────────
const PATHS = {
  home: "M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z M9 22V12h6v10",
  camera:
    "M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z M12 17a4 4 0 100-8 4 4 0 000 8",
  video:
    "M15 10l4.553-2.276A1 1 0 0121 8.723v6.554a1 1 0 01-1.447.894L15 14 M3 8a2 2 0 012-2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z",
  calendar:
    "M8 2v4 M16 2v4 M3 10h18 M5 4h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V6a2 2 0 012-2z",
  dollar: "M12 1v22 M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6",
  leaf: "M17 8C8 10 5.9 16.17 3.82 22 M9.5 9.5c1 2.5 3.5 5 6.5 5.5 M21 3C6 5 5 13 5 20",
  logout: "M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4 M16 17l5-5-5-5 M21 12H9",
  user: "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2 M12 11a4 4 0 100-8 4 4 0 000 8",
  check: "M20 6L9 17l-5-5",
  briefcase:
    "M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2",
  play: "M5 3l14 9-14 9V3z",
  x: "M18 6L6 18 M6 6l12 12",
  sun: "M12 18a6 6 0 100-12 6 6 0 000 12z M12 2v4 M12 18v4 M4.22 4.22l2.83 2.83 M16.95 16.95l2.83 2.83 M2 12h4 M18 12h4 M4.22 19.78l2.83-2.83 M16.95 7.05l2.83-2.83",
  moon: "M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z",
  info: "M12 22a10 10 0 100-20 10 10 0 000 20z M12 8h.01 M12 12v4",
  download: "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  // Desktop monitor — used by the "clock in from the desktop app" notice.
  monitor: "M4 4h16a1 1 0 011 1v10a1 1 0 01-1 1H4a1 1 0 01-1-1V5a1 1 0 011-1z M8 20h8 M12 16v4",
  filter: "M22 3H2l8 9.46V19l4 2v-8.54L22 3z",
  arrowdown: "M7 10l5 5 5-5",
  arrowup: "M7 14l5-5 5 5",
  chevleft: "M15 18l-6-6 6-6",
  chevright: "M9 18l6-6-6-6",
  users:
    "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2 M23 21v-2a4 4 0 00-3-3.87 M16 3.13a4 4 0 010 7.75",
  star: "M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z",
  export: "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M17 8l-5-5-5 5 M12 3v12",
  settings:
    "M12 15a3 3 0 100-6 3 3 0 000 6z M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z",
  globe:
    "M12 2a10 10 0 100 20A10 10 0 0012 2z M2 12h20 M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z",
  refresh:
    "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10 M1 14l4.64 4.36A9 9 0 0020.49 15",
  clock: "M12 2a10 10 0 100 20A10 10 0 0012 2z M12 6v6l4 2",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z M9 12l2 2 4-4",
  // ── Employee Sidebar additions (Workforce Surface spec) ────────────
  activity: "M22 12h-4l-3 9L9 3l-3 9H2",
  timeline:
    "M12 2v4 M12 18v4 M2 12h4 M18 12h4 M5 5l3 3 M16 16l3 3 M5 19l3-3 M16 8l3-3 M12 7a5 5 0 100 10 5 5 0 000-10z",
  message: "M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z",
  hash: "M4 9h16 M4 15h16 M10 3L8 21 M16 3l-2 18",
  videocall:
    "M23 7l-7 5 7 5V7z M14 5H3a2 2 0 00-2 2v10a2 2 0 002 2h11a2 2 0 002-2V7a2 2 0 00-2-2z",
  connect: "M22 11.08V12a10 10 0 11-5.93-9.14 M9 11l3 3L22 4",
  receipt:
    "M14 2H6a2 2 0 00-2 2v18l3-2 3 2 3-2 3 2V8z M14 2v6h6 M9 12h6 M9 16h6",
  trending_up: "M23 6l-9.5 9.5-5-5L1 18 M17 6h6v6",
  insights: "M3 3v18h18 M9 17v-5 M13 17v-9 M17 17v-3",
  database:
    "M12 3a9 3 0 100 6 9 3 0 000-6z M3 5v6a9 3 0 0018 0V5 M3 11v6a9 3 0 0018 0v-6",
  history: "M3 3v6h6 M3.51 9a9 9 0 1 0 2.13-9.36L1 9 M12 7v5l4 2",
  privacy: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z M12 8v4 M12 16h.01",
  bell: "M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9 M13.73 21a2 2 0 01-3.46 0",
  help: "M12 22a10 10 0 100-20 10 10 0 000 20z M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3 M12 17h.01",
  chevron_right2: "M9 18l6-6-6-6",
  panel_close: "M18 6L6 18 M6 6l12 12",
  send: "M22 2L11 13 M22 2l-7 20-4-9-9-4 20-7z",
  eye: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z M12 15a3 3 0 100-6 3 3 0 000 6z",
  eye_off:
    "M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24 M1 1l22 22",
};

// uiverse-style click ripple — appends a short-lived span at the cursor.
// Caller's button must be position:relative + overflow:hidden.
const spawnRipple = (e) => {
  try {
    const btn = e.currentTarget;
    if (!btn) return;
    const rect = btn.getBoundingClientRect();
    const span = document.createElement("span");
    span.className = "zte-ripple";
    span.style.left = e.clientX - rect.left - 7 + "px";
    span.style.top = e.clientY - rect.top - 7 + "px";
    btn.appendChild(span);
    setTimeout(() => span.remove(), 600);
  } catch {
    /* no-op */
  }
};

// Exported so the lazy-split DashboardSummaryView chunk can reuse this shared
// icon helper (and its PATHS map) without duplicating it.
export const Ico = ({ n, s = 16, c = "#98A2B3", fw = "2" }) => {
  return (
    <svg
      width={s}
      height={s}
      viewBox="0 0 24 24"
      fill="none"
      stroke={c}
      strokeWidth={fw}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ flexShrink: 0, display: "block" }}
    >
      {(PATHS[n] || PATHS.home)
        .split("M")
        .filter(Boolean)
        .map((seg, i) => (
          <path key={i} d={`M${seg}`} />
        ))}
    </svg>
  );
};

const getColors = (isDark) => ({
  // Dark mode uses neutral near-black greys (no blue cast) — matches the
  // admin dashboard and the global ThemeContext palette so the two pages
  // feel like one product.
  bg: isDark ? "#0A0A0A" : "#F0F2F5",
  surface: isDark ? "#141414" : "#FFFFFF",
  border: isDark ? "#262626" : "#E4E7EC",
  borderLight: isDark ? "#1A1A1A" : "#F2F4F7",
  text: isDark ? "#F5F5F5" : "#101828",
  textSub: isDark ? "#A3A3A3" : "#475467",
  textLight: isDark ? "#737373" : "#98A2B3",
  brand: "#11496A",
  brandDark: "#0E3B57",
  brandBg: isDark ? "#0E2B3D" : "#E7F0F6",
  brandBdr: "#A6C7DC",
  // Green accent — same palette as ThemeContext / AdminSidebar so the
  // sidebar hover system (zte-nav rules below) actually paints. Without
  // these the CSS evaluates to `background: undefined !important` and
  // the green hover state silently disappears.
  brandAccent: isDark ? "#5FCC93" : "#44B97B",
  brandAccentDark: isDark ? "#3FAF77" : "#2F8F5C",
  brandAccentBg: isDark ? "#0D2A1A" : "#E8F7EE",
  green: "#2F8F5C",
  greenBg: isDark ? "#0D2A1A" : "#E8F7EE",
  greenBdr: "#A8E0BD",
  amber: "#B54708",
  amberBg: isDark ? "#3D2A0A" : "#FFFAEB",
  amberBdr: "#FEDF89",
  red: "#B42318",
  redBg: isDark ? "#3D1F1A" : "#FEF3F2",
  redBdr: "#FECDCA",
  r: 10,
  rSm: 6,
});

// ─── LEGACY POPUP SYSTEM REMOVED ─────────────────────────────────────────────
// The old rectangular timer popups (ClockInPopup / ActivePopup / StoppedPopup)
// and their shared chrome (DesktopWindow, PopupHeader) used to render on top
// of a dark backdrop with their own clock-in / break / clock-out controls.
// They became unreachable dead code once the Electron shell moved to /electron
// (the legacy-Electron migration redirect above sends those clients there
// before any popup can render). Removing them eliminates the floating timer +
// dark overlay the dashboard was inheriting and shrinks the bundle. See git
// history if the previous bodies are ever needed.

// ─── MAIN ─────────────────────────────────────────────────────────────────────
// When running inside the Temveris desktop (Electron) shell, the window only
// shows the slim clock-in / break / today view. Anything heavier — full
// reports, leave management, settings, admin — belongs on the website. These
// helpers detect the desktop shell and route those clicks to the user's real
// browser (Electron's setWindowOpenHandler intercepts window.open and forwards
// to shell.openExternal, so this stays a one-liner at the call site).
const IS_DESKTOP_APP =
  typeof window !== "undefined" && window.isDesktopApp === true;
const WEBSITE_ORIGIN = PUBLIC_ORIGIN;
const openOnWebsite = (path) => {
  const url = WEBSITE_ORIGIN + (path.startsWith("/") ? path : "/" + path);
  window.open(url, "_blank", "noopener,noreferrer");
};

export default function EmployeeDashboard() {
  const navigate = useNavigate();
  // `lang` is the selected BCP-47 tag. The holiday views format dates with it
  // rather than the hardcoded "en-IN" they used to pass, which showed
  // Indian-style dates to every employee whatever language they had chosen.
  const { T, lang } = useLanguage();
  const { isDark, toggleDark } = useTheme();
  const { user: authUser, logout } = useAuth();
  const { workspaceLogo } = useWorkspace();
  const C = useMemo(() => getColors(isDark), [isDark]);
  // Prevents React StrictMode's double-invocation from firing the session-
  // restore fetch twice, which caused the loading screen to flash and then
  // immediately reset — making "Open My Dashboard" appear to not respond.
  const sessionRestoreDone = useRef(false);
  // Resolved logo: workspace-uploaded branding first, Temveris default fallback.
  // WorkspaceContext is org-scoped and skips for super_admin, so there is no
  // cross-tenant logo leakage.
  const activeLogo = workspaceLogo || TemverisLogo;

  // Migrate any client running inside the Electron shell to the slim
  // /electron view. Older installed Electron builds (≤1.0.4) hard-coded
  // loadURL(".../dashboard"); the new ElectronDashboard lives at /electron.
  // Detect both the new preload flag and the older user-agent so users
  // don't need to reinstall to land on the right page.
  useEffect(() => {
    const ua =
      typeof navigator !== "undefined" ? navigator.userAgent || "" : "";
    const inElectron =
      (typeof window !== "undefined" && window.isDesktopApp === true) ||
      /temveris-desktop\//i.test(ua);
    if (inElectron) {
      navigate("/electron", { replace: true });
    }
  }, [navigate]);

  const [acts, setActs] = useState([]);
  // Initial tab honours ?tab=... so a deep link from the desktop app
  // (e.g. /dashboard?tab=leave) lands the user directly on that section.
  const [tab, setTab] = useState(() => {
    try {
      const q = new URLSearchParams(window.location.search).get("tab");
      return q || "my-work";
    } catch {
      return "my-work";
    }
  });

  // ─── Worker preferences ────────────────────────────────────────────────────
  // Mirrors /worker/settings so the dashboard renders dates/times/numbers in
  // the format the worker picked in Account → Settings. SettingsModule pushes
  // updates here via onPreferencesChange so changes take effect immediately
  // without a tab remount.
  const [workerPrefs, setWorkerPrefs] = useState({
    text_size: "default",
    high_contrast: false,
    presentation_mode: false,
    time_format: "workspace", // "12h" | "24h" | "workspace"
    date_format: "MM/DD/YYYY",
    number_format: "1,234.56",
    timezone: (() => {
      try {
        return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
      } catch {
        return "UTC";
      }
    })(),
    language: "en-US",
    hide_payroll_values: true,
  });

  // Initial fetch — runs once on mount so preferences are already applied
  // when the worker first lands on any tab (no need to visit Settings first).
  //
  // Also honours `default_landing_page` from the saved preferences: if the
  // user picked "My Activity" as their landing tab, we redirect there as soon
  // as the prefs load — but only when the URL didn't carry an explicit
  // ?tab=… (deep links always win) and we're still on the fallback "my-work"
  // tab so we don't yank a user who already clicked somewhere else during the
  // brief load window.
  useEffect(() => {
    const token = getToken();
    if (!token) return;
    let cancelled = false;
    const urlHasExplicitTab = (() => {
      try {
        return !!new URLSearchParams(window.location.search).get("tab");
      } catch {
        return false;
      }
    })();
    fetch(`${API_BASE}/worker/settings`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (cancelled || !d) return;
        setWorkerPrefs((prev) => ({ ...prev, ...d }));
        const landing = d.default_landing_page;
        const LANDING_TO_TAB = {
          "my-work": "my-work",
          "activity-log": "activity-log",
          "my-schedule": "my-schedule",
        };
        const target = LANDING_TO_TAB[landing];
        if (target && !urlHasExplicitTab) {
          // Only override the bootstrap "my-work" default; don't yank a user
          // who already navigated during the (very brief) load window.
          setTab((curr) => (curr === "my-work" ? target : curr));
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Apply text_size and high_contrast globally. Text size scales the root
  // font-size so every rem-based value in the dashboard responds. High
  // contrast adds a data attribute so any component can opt into the
  // alternate palette via a CSS selector.
  useEffect(() => {
    const SIZE_PX = { compact: 14, default: 16, large: 18, xlarge: 20 };
    document.documentElement.style.fontSize = `${SIZE_PX[workerPrefs.text_size] || 16}px`;
    document.documentElement.dataset.contrast = workerPrefs.high_contrast
      ? "high"
      : "normal";
    document.documentElement.dataset.presentationMode =
      workerPrefs.presentation_mode ? "on" : "off";
    return () => {
      // Don't reset on unmount — preferences should persist while the worker
      // is logged in. Only the next preference change writes here again.
    };
  }, [
    workerPrefs.text_size,
    workerPrefs.high_contrast,
    workerPrefs.presentation_mode,
  ]);

  // ─── Preference-aware formatters ───────────────────────────────────────
  // Used wherever the dashboard renders a time or date — picks 12h/24h,
  // the worker's date layout, their timezone, and their locale. Falls back
  // to safe defaults when the value isn't set yet.
  const fmtTimePref = useCallback(
    (utcIso) => {
      if (!utcIso) return "—";
      try {
        const d = safeDate(utcIso);
        const hour12 =
          workerPrefs.time_format === "24h"
            ? false
            : workerPrefs.time_format === "12h"
              ? true
              : true; // "workspace" defaults to 12h until a workspace-level toggle exists
        return d.toLocaleTimeString(workerPrefs.language || "en-US", {
          hour: "2-digit",
          minute: "2-digit",
          hour12,
          timeZone: workerPrefs.timezone || undefined,
        });
      } catch {
        return "—";
      }
    },
    [workerPrefs.time_format, workerPrefs.timezone, workerPrefs.language],
  );

  const fmtDatePref = useCallback(
    (utcIso) => {
      if (!utcIso) return "—";
      try {
        const d = safeDate(utcIso);
        const map = {
          "MM/DD/YYYY": { month: "2-digit", day: "2-digit", year: "numeric" },
          "DD/MM/YYYY": { day: "2-digit", month: "2-digit", year: "numeric" },
          "YYYY-MM-DD": { year: "numeric", month: "2-digit", day: "2-digit" },
          "D MMM YYYY": { day: "numeric", month: "short", year: "numeric" },
        };
        const opts = map[workerPrefs.date_format] || map["MM/DD/YYYY"];
        let out = d.toLocaleDateString(workerPrefs.language || "en-US", {
          ...opts,
          timeZone: workerPrefs.timezone || undefined,
        });
        // YYYY-MM-DD requires dash-joined ISO; toLocaleDateString won't reliably
        // produce that on every browser, so we rebuild it manually.
        if (workerPrefs.date_format === "YYYY-MM-DD") {
          const y = d.getFullYear();
          const m = String(d.getMonth() + 1).padStart(2, "0");
          const dd = String(d.getDate()).padStart(2, "0");
          out = `${y}-${m}-${dd}`;
        }
        return out;
      } catch {
        return "—";
      }
    },
    [workerPrefs.date_format, workerPrefs.timezone, workerPrefs.language],
  );

  // Number formatter — mirrors the user's number_format choice. The four
  // shapes map to distinct BCP-47 locales whose default thousands/decimal
  // separators match what the SettingsModule preview shows.
  const fmtNumberPref = useCallback(
    (n, fractionDigits = 2) => {
      const num = Number(n);
      if (!isFinite(num)) return "—";
      const fmt = workerPrefs.number_format || "1,234.56";
      const localeByFormat = {
        "1,234.56": "en-US",
        "1.234,56": "de-DE",
        "1 234,56": "fr-FR",
        "1’234.56": "de-CH",
      };
      const locale = localeByFormat[fmt] || "en-US";
      try {
        return num.toLocaleString(locale, {
          minimumFractionDigits: fractionDigits,
          maximumFractionDigits: fractionDigits,
        });
      } catch {
        return num.toFixed(fractionDigits);
      }
    },
    [workerPrefs.number_format],
  );
  // Sidebar group expand state
  const [openGroups, setOpenGroups] = useState({
    "my-pay": true,
  });
  const [now, setNow] = useState(new Date());

  // Employee info — initial seed from localStorage, then kept in sync with
  // AuthContext so a profile edit (name / picture) propagates here without a
  // refresh. The /employees/me effect below also writes these.
  const [profileName, setProfileName] = useState(
    localStorage.getItem("name") ||
      localStorage.getItem("full_name") ||
      "Employee",
  );
  const [profileEmail, setProfileEmail] = useState(
    localStorage.getItem("email") || "",
  );

  // Live sync from AuthContext — Profile editor calls refreshUser() on save,
  // so authUser changes immediately when name or avatar updates.
  useEffect(() => {
    if (authUser?.name) setProfileName(authUser.name);
    if (authUser?.email) setProfileEmail(authUser.email);
  }, [authUser?.name, authUser?.email]);

  const [clockedIn, setClockedIn] = useState(false);
  const [onBreak, setOnBreak] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [breakElapsed, setBreakElapsed] = useState(0);
  const [dailyWorkedSecs, setDailyWorkedSecs] = useState(0);

  // Today's activity summary — drives the "Active Time / Idle Time % /
  // Productive Apps %" KPI tiles on the Today tab. Backed by the same
  // /api/activity-log/summary endpoint the Activity Log page uses, so the
  // numbers match across both views (previously these tiles showed a
  // hard-coded "0%" because no fetch was wired up).
  const [todayActivity, setTodayActivity] = useState({
    activeSecs: 0,
    idleSecs: 0,
    productivePct: 0,
  });
  const prevWorkedSecsRef = useRef(0);
  const clockInMsRef = useRef(null);
  const breakStartMsRef = useRef(null);
  const totalBreakMsRef = useRef(0);
  // C4: `elapsedRef` mirrors the live session seconds every tick (accurate to
  // the second) so the clock-out fallback reads it instead of the now
  // minute-gated `elapsed` state. `lastFlushMinRef` tracks the last minute we
  // pushed to React state (see startTick).
  const elapsedRef = useRef(0);
  const lastFlushMinRef = useRef({ w: -1, b: -1 });
  const timerRef = useRef(null);
  const [clockInTimeUTC, setClockInTimeUTC] = useState(null);
  const [clockOutTimeUTC, setClockOutTimeUTC] = useState(null);
  const [clockOutMsgIdx, setClockOutMsgIdx] = useState(null);
  const [clockingIn, setClockingIn] = useState(false);
  const [workedSeconds, setWorkedSeconds] = useState(0);
  const [error, setError] = useState(null);

  // Deep Work Alert — continuous work >= 4 hours without a break
  const lastBreakEndMsRef = useRef(null);
  const [showDeepWorkAlert, setShowDeepWorkAlert] = useState(false);
  const deepWorkDismissedRef = useRef(false);
  // Server-driven: `break.deep_work_minutes`, delivered by /break/thresholds
  // alongside the break allowances. It was a literal 4 hours here — a break
  // policy decided in the browser, for every tenant at once. null until the
  // fetch lands, and the nudge simply does not fire before then.
  // A ref, not state: it is read inside the 1-second tick, which is created by
  // a useCallback that must not re-subscribe when this value arrives.
  const deepWorkMsRef = useRef(null);

  // Break-type picker — opens before /start-break so the worker explicitly
  // chooses "short" (15m overtime threshold) or "long/lunch" (60m). Backend
  // uses this to pick the right overtime alert window.
  const [showBreakPicker, setShowBreakPicker] = useState(false);
  const [breakThresholds, setBreakThresholds] = useState({
    short_minutes: 15,
    lunch_minutes: 60,
  });
  // Governance: part-time / contractor workers are not entitled to a lunch
  // break, so the Lunch option is hidden for them (server also enforces).
  const [lunchAllowed, setLunchAllowed] = useState(true);
  // Blocking break-overrun modal. Driven by polling /break/active while on a
  // break; when the break crosses its allowance the worker gets a center-screen
  // alert they can't miss. Admin + HR are notified server-side (the same
  // /break/active call also triggers the admin break_overtime broadcast).
  const [overrunInfo, setOverrunInfo] = useState(null);
  const [showOverrunModal, setShowOverrunModal] = useState(false);
  // Controlled Clock-Out (Phase 2): populated with the backend's
  // EARLY_CLOCK_OUT_REASON_REQUIRED response when a reason is needed.
  const [earlyClockOut, setEarlyClockOut] = useState(null);
  const [earlyReason, setEarlyReason] = useState("");
  const [earlyError, setEarlyError] = useState("");
  const [earlySubmitting, setEarlySubmitting] = useState(false);
  // Employment type of the logged-in employee (from /employees/me) — drives the
  // rating threshold so part-timers are judged against 4h, not 8h45m.
  const [empType, setEmpType] = useState("full_time");
  // Required worked seconds for THIS worker, resolved server-side and delivered by
  // GET /employees/me. P9 removed the client-side 8h/4h table that used to derive
  // it. Null until the first fetch lands.
  const [expectedWorkSecs, setExpectedWorkSecs] = useState(null);
  // Desktop-only clock-in policy (backend: app/attendance_desktop_policy.py).
  // Server-supplied via /employees/me so this page never needs to know WHICH
  // orgs are covered — flip the backend env var and the UI follows. When true
  // the Clock In button is replaced by a "use the desktop app" message.
  // This page is web-only: any Electron client is redirected to /electron on
  // mount, so this branch can never hide the button inside the desktop app.
  const [desktopClockInRequired, setDesktopClockInRequired] = useState(false);
  // Attendance ACTIONS are desktop-app only for every role and every tenant
  // (WEB_ATTENDANCE_ACTIONS_ENABLED in config.js — false today). This is the one
  // predicate every Clock In / Clock Out / Break / Resume control on this page
  // is gated on. It covers, and is broader than, the per-org
  // `desktop_clock_in_required` employee policy, which is kept intact so the
  // employee experience is unchanged when the global flag is lifted.
  //
  // ACTIONS only: everything read-only on this page (worked hours, active time,
  // break time, activity, screenshots, timesheets, reports) is untouched.
  const attendanceActionsAllowed =
    WEB_ATTENDANCE_ACTIONS_ENABLED && !desktopClockInRequired;
  // Double-click guard for Clock Out (website). Ref = synchronous guard against
  // rapid double-clicks; state drives the button's disabled/loading UI.
  const clockOutBusyRef = useRef(false);
  const [clockOutBusy, setClockOutBusy] = useState(false);
  const overrunSnoozeUntilRef = useRef(0);

  // Single boolean replacing the old multi-state `screen` machine. The
  // legacy popup screens (clock-in / active-popup / stopped-popup) were
  // removed — see "LEGACY POPUP SYSTEM REMOVED" above. Now we only gate
  // the initial loading spinner.
  const [loading, setLoading] = useState(true);

  const [pay, setPay] = useState({
    basic: "",
    hra: "",
    da: "",
    ta: "",
    pf: "",
    tax: "",
  });
  const [payTab, setPayTab] = useState("time-entries");
  const [tsDateOffset, setTsDateOffset] = useState(0);
  // Historical day-summary fetched from /employees/me/day-summary when
  // tsDateOffset !== 0. Today (offset 0) uses live state instead so the
  // counters keep ticking in real time.
  const [tsHistory, setTsHistory] = useState(null);
  const [tsHistoryLoading, setTsHistoryLoading] = useState(false);
  // Per-session cache so paging back to a day already visited renders
  // instantly without a Loading flash or another round-trip. Historical
  // days don't mutate after the IST day ends so this is safe to keep for
  // the lifetime of the dashboard mount.
  const tsHistoryCacheRef = useRef(new Map());
  const [empScreenshots, setEmpScreenshots] = useState([]);
  const [empSsLoading, setEmpSsLoading] = useState(false);
  const [empSsSort, setEmpSsSort] = useState("newest");
  const [empLightbox, setEmpLightbox] = useState(null);
  const [empSsFailed, setEmpSsFailed] = useState(() => new Set());
  const [lightboxImageFailed, setLightboxImageFailed] = useState(false);
  const [empSsDate, setEmpSsDate] = useState(
    () => new Date().toISOString().split("T")[0],
  );

  // Holidays (fetched from API)
  const [holidays, setHolidays] = useState([]);

  // AI Daily Summary
  const [aiSummary, setAiSummary] = useState(null);
  const [aiWeekly, setAiWeekly] = useState(null);
  const [aiLoading, setAiLoading] = useState(false);
  // Date picker for AI Daily Summary — empty string means "today" (server
  // default). Changing this triggers a fresh fetch from /ai/summary/{uid}?date=...
  const [aiSummaryDate, setAiSummaryDate] = useState("");
  // Manual refresh nonce — increment to force a re-fetch (e.g. clicking the
  // refresh icon) without changing the date.
  const [aiRefreshNonce, setAiRefreshNonce] = useState(0);
  const [ssRefreshTick, setSsRefreshTick] = useState(0);

  // ── Earnings Estimate (employee self-serve payroll) ────────────────────────
  const [earningsData, setEarningsData] = useState(null);
  const [earningsLoading, setEarningsLoading] = useState(false);
  // Session-level reveal flag for masked payroll values. Default OFF so the
  // workerPrefs.hide_payroll_values setting takes effect on every page load;
  // users opt-in to show real numbers via the Reveal eye toggle on the Earnings
  // tab. Not persisted: clears on reload by design.
  const [payrollRevealed, setPayrollRevealed] = useState(false);
  const [earningsBreakFilter, setEarningsBreakFilter] = useState("all");
  const [earningsQOpen, setEarningsQOpen] = useState(false);
  const [earningsQCategory, setEarningsQCategory] = useState("");
  const [earningsQText, setEarningsQText] = useState("");
  const [earningsQDate, setEarningsQDate] = useState("");
  const [earningsQSubmitting, setEarningsQSubmitting] = useState(false);
  const [earningsQSent, setEarningsQSent] = useState(false);

  // ── Profile dropdown ───────────────────────────────────────────────────────
  const [profileMenuOpen, setProfileMenuOpen] = useState(false);
  const profileMenuRef = useRef(null);

  const wsRef = useRef(null);
  const wsReconnectTimer = useRef(null);
  // Guards the unmount → close() → onclose → reconnect leak: once teardown
  // begins we must NOT schedule another reconnect. Mirrors AdminDashboard.
  const wsTearingDownRef = useRef(false);

  // ── App settings (permissions + screenshot config) ─────────────────────────
  const [appSettings, setAppSettings] = useState({
    perm_emp_see_screenshot: true,
    perm_emp_see_timesheet: true,
    perm_emp_access_website: true,
    screenshot_blur: false,
    payroll_enabled: true,
  });

  useEffect(() => {
    const tok =
      localStorage.getItem("zt_token") || localStorage.getItem("token");
    fetch(`${API_BASE}/settings`, {
      headers: tok ? { Authorization: `Bearer ${tok}` } : {},
    })
      .then((r) => r.json())
      .then((d) => {
        const s = d.settings || {};
        const b = (v, def = true) => (v === undefined ? def : v !== "false");
        setAppSettings({
          perm_emp_see_screenshot: b(s.perm_emp_see_screenshot),
          perm_emp_see_timesheet: b(s.perm_emp_see_timesheet),
          perm_emp_access_website: b(s.perm_emp_access_website),
          screenshot_blur: s.screenshot_blur === "true",
          payroll_enabled: b(s.payroll_enabled),
        });
      })
      .catch(() => {});
  }, []);

  // Load workspace break overtime thresholds so the picker copy ("15m / 60m")
  // matches whatever the admin has configured.
  useEffect(() => {
    const tok =
      localStorage.getItem("zt_token") || localStorage.getItem("token");
    if (!tok) return;
    getBreakThresholds()
      .then((d) => {
        if (d && (d.short_minutes || d.lunch_minutes)) {
          setBreakThresholds({
            short_minutes: Number(d.short_minutes) || 15,
            lunch_minutes: Number(d.lunch_minutes) || 60,
          });
          const dw = Number(d.deep_work_minutes);
          if (Number.isFinite(dw) && dw > 0) deepWorkMsRef.current = dw * 60 * 1000;
        }
      })
      .catch(() => {});
    // Per-workday entitlements drive whether Lunch is offered at all.
    fetch(`${API_BASE}/break/entitlements`, {
      headers: { Authorization: `Bearer ${tok}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && typeof d.lunch_allowed === "boolean")
          setLunchAllowed(d.lunch_allowed);
      })
      .catch(() => {});
  }, []);

  const name = profileName;
  const email = profileEmail;
  const init =
    (name || "E")
      .split(" ")
      .map((w) => w[0])
      .join("")
      .toUpperCase()
      .slice(0, 2) || "E";

  const showError = (msg) => {
    setError(msg);
    setTimeout(() => setError(null), 5000);
  };

  // `now` is only consumed by date-formatted strings in this component
  // (weekday/month/day labels), so updating once a minute is plenty and saves
  // ~60 re-renders per minute on the entire dashboard tree.
  useEffect(() => {
    const i = setInterval(() => setNow(new Date()), 60000);
    return () => clearInterval(i);
  }, []);

  // Tracks the last tick timestamp so we can detect device sleep — if the
  // setInterval fires after a gap much larger than 1s, the OS suspended us.
  const lastTickMsRef = useRef(Date.now());

  const stopTick = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // Single source of truth for re-syncing local UI with the backend session
  // state. Called from:
  //   1. Sleep-gap detection inside the tick (laptop just woke up)
  //   2. The "online" event (network restored)
  //   3. A periodic poll every 60s while clocked in (backend may have
  //      auto-closed the session due to missed heartbeats during sleep)
  // If the backend reports the session was auto-closed, we force the UI
  // into a clocked-out state so the timer doesn't keep ticking.
  const syncStatusFromServer = useCallback(async () => {
    const tok = getToken();
    if (!tok) return;
    let d;
    try {
      d = await getMe();
    } catch {
      return;
    }
    if (!d) return;

    // Backend says the session is closed but our local UI thinks it's open —
    // this is the "laptop went to sleep, backend auto-closed" case. Force
    // the local UI to a clocked-out state.
    if (clockedIn && !d.clocked_in) {
      stopTick();
      setClockedIn(false);
      setOnBreak(false);
      breakStartMsRef.current = null;
      clockInMsRef.current = null;
      totalBreakMsRef.current = 0;
      lastBreakEndMsRef.current = null;
      deepWorkDismissedRef.current = false;
      setShowDeepWorkAlert(false);
      prevWorkedSecsRef.current = d.worked_today_seconds || 0;
      setDailyWorkedSecs(d.worked_today_seconds || 0);
      setElapsed(0);
      setBreakElapsed(0);
      if (d.clock_out_time) setClockOutTimeUTC(d.clock_out_time);
      window.electronTracking?.updateStatus("stopped");
      showError(
        "Session was auto-closed (device sleep / inactivity). Clock in again to resume.",
      );
      return;
    }

    // Otherwise — still clocked in — re-sync timing data from the server
    // so the local timer matches the server's authoritative numbers.
    if (d.clocked_in) {
      const startMs = safeDate(d.clock_in_time).getTime();
      clockInMsRef.current = startMs;
      // The backend's `break_elapsed_seconds` already includes the running
      // break. The local tick adds (now - breakStartMsRef) on top while on
      // break, so mirroring the full backend total here would double-count
      // and the worked timer would tick BACKWARDS during a break.
      // Fix: keep only completed-break time in totalBreakMsRef while on
      // break — the running portion is represented by breakStartMsRef.
      const totalBreakSecs = d.break_elapsed_seconds || 0;
      const currentBreakSecs = d.current_break_seconds || 0;
      prevWorkedSecsRef.current = d.worked_today_seconds || 0;
      if (d.on_break) {
        totalBreakMsRef.current =
          Math.max(0, totalBreakSecs - currentBreakSecs) * 1000;
        if (breakStartMsRef.current === null) {
          breakStartMsRef.current = Date.now() - currentBreakSecs * 1000;
          setOnBreak(true);
        }
      } else {
        totalBreakMsRef.current = totalBreakSecs * 1000;
        if (breakStartMsRef.current !== null) {
          // Backend says we're working but local thinks we're on break —
          // server-side resume happened (e.g. via desktop tracker). Sync.
          breakStartMsRef.current = null;
          setOnBreak(false);
          lastBreakEndMsRef.current = Date.now();
        }
      }
    }
  }, [clockedIn, stopTick]);

  const startTick = useCallback(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    const MAX_SESSION_SECS = 14 * 3600; // sanity cap — forgotten clock-out won't inflate the UI
    const SLEEP_GAP_MS = 30 * 1000; // tick gap larger than this ⇒ device slept
    lastTickMsRef.current = Date.now();
    lastFlushMinRef.current = { w: -1, b: -1 }; // force a state flush on the first tick of this session
    timerRef.current = setInterval(() => {
      // Pause the display timer when the device is offline so the counter
      // doesn't inflate beyond what the server will record.
      if (!navigator.onLine) {
        lastTickMsRef.current = Date.now();
        return;
      }
      const ms = Date.now();
      // Sleep detection — a gap larger than SLEEP_GAP_MS between ticks means
      // the OS suspended us. Resync with the server, which may have
      // auto-closed the session in the meantime.
      if (ms - lastTickMsRef.current > SLEEP_GAP_MS) {
        lastTickMsRef.current = ms;
        syncStatusFromServer();
        return;
      }
      lastTickMsRef.current = ms;
      const total = ms - (clockInMsRef.current || ms);
      const brk =
        totalBreakMsRef.current +
        (breakStartMsRef.current !== null ? ms - breakStartMsRef.current : 0);
      let sessionSecs = Math.floor(Math.max(0, total - brk) / 1000);
      if (sessionSecs > MAX_SESSION_SECS) sessionSecs = MAX_SESSION_SECS;
      // C4: keep an accurate-to-the-second mirror every tick (drives the
      // clock-out fallback + the deep-work check below), but only re-render the
      // Dashboard when a MINUTE-granularity readout actually changes. Every value
      // rendered from these three states uses fmtHM (H:M) or the hourly Activities
      // chart, so a per-second re-render produced identical pixels ~59 ticks out
      // of 60. The two seconds-granularity readouts now tick inside
      // <LiveWorkedClock>. Net: ~60 full-tree re-renders/min → ≤2/min.
      const newBreakSecs = Math.floor(brk / 1000);
      const newDailySecs = prevWorkedSecsRef.current + sessionSecs;
      elapsedRef.current = sessionSecs;
      const wMin = Math.floor(newDailySecs / 60);
      const bMin = Math.floor(newBreakSecs / 60);
      if (
        wMin !== lastFlushMinRef.current.w ||
        bMin !== lastFlushMinRef.current.b
      ) {
        lastFlushMinRef.current = { w: wMin, b: bMin };
        setElapsed(sessionSecs);
        setBreakElapsed(newBreakSecs);
        setDailyWorkedSecs(newDailySecs);
      }
      // Deep Work Alert: check continuous work since last break (or clock-in)
      if (
        lastBreakEndMsRef.current &&
        breakStartMsRef.current === null &&
        !deepWorkDismissedRef.current
      ) {
        if (deepWorkMsRef.current != null &&
            ms - lastBreakEndMsRef.current >= deepWorkMsRef.current) {
          setShowDeepWorkAlert(true);
        }
      }
    }, 1000);
  }, [syncStatusFromServer]);

  useEffect(() => () => stopTick(), [stopTick]);

  // Re-sync from server when network comes back online.
  useEffect(() => {
    const handleOnline = () => {
      if (clockedIn) syncStatusFromServer();
    };
    window.addEventListener("online", handleOnline);
    return () => window.removeEventListener("online", handleOnline);
  }, [clockedIn, syncStatusFromServer]);

  // Safety-net poll: every 20 s while clocked in, ask the backend for the
  // authoritative session state. Tighter than the original 60 s so a break
  // started in the desktop tracker (or vice-versa) shows up here within a
  // few seconds instead of up to a minute. Also refetch immediately on tab
  // focus / visibility change so switching back from the Electron shell
  // feels instant.
  useEffect(() => {
    if (!clockedIn) return;
    // PERF: increased from 20s to 30s poll cadence to reduce load while
    // WebSocket + focus/visibility handlers remain primary freshness sources.
    // The 5s cache on /me also dedupes bursts. Tab hidden still skips the poll.
    const id = setInterval(() => {
      if (!document.hidden) syncStatusFromServer();
    }, 30 * 1000);
    const onFocus = () => syncStatusFromServer();
    const onVis = () => {
      if (!document.hidden) syncStatusFromServer();
    };
    // Admin added/reverted a time adjustment for this worker — re-pull /me
    // so `prevWorkedSecsRef` picks up the new `worked_today_seconds` and the
    // "Total Worked" tile (which is `dailyWorkedSecs = prev + sessionSecs`)
    // updates without waiting for the 30s sync poll. The event is dispatched
    // by the websocket handler below when `time_adjustment_updated` matches
    // this user's id.
    const onAdjustment = () => syncStatusFromServer();
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("ts_adjustment_changed", onAdjustment);
    return () => {
      clearInterval(id);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("ts_adjustment_changed", onAdjustment);
    };
  }, [clockedIn, syncStatusFromServer]);

  // WebSocket reconnect
  useEffect(() => {
    let wsBackoff = 1000;
    const connect = () => {
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {}
      }
      try {
        // Authenticate the WS handshake — backend rejects (1008) without a
        // valid JWT. Reconnects re-read the current token via getToken().
        const ws = new WebSocket(
          `${WS_URL}?token=${encodeURIComponent(getToken() || "")}`,
        );
        wsRef.current = ws;
        ws.onmessage = (evt) => {
          try {
            const data = JSON.parse(evt.data);
            if (data.type === "new_screenshot") {
              const uid = localStorage.getItem("user_id");
              if (!uid || String(data.user_id) === String(uid)) {
                setSsRefreshTick((t) => t + 1);
              }
            }
            // Live settings update — admin changed screenshot interval or
            // similar; rebroadcast as a window event so any listening
            // component (incl. SettingsPage if open) can react immediately.
            if (data.type === "settings_updated") {
              try {
                window.dispatchEvent(
                  new CustomEvent("settings_updated", { detail: data }),
                );
              } catch {}
            }
            // Admin added/reverted a time adjustment for THIS user — refresh
            // the day-summary so worked hours reflect the change instantly
            // instead of waiting up to 60s for the next poll. Gated on
            // user_id so other employees' adjustments don't trigger a
            // re-fetch storm on every connected client.
            if (data.type === "time_adjustment_updated") {
              const uid = localStorage.getItem("user_id");
              if (uid && String(data.user_id) === String(uid)) {
                try {
                  window.dispatchEvent(
                    new CustomEvent("ts_adjustment_changed", { detail: data }),
                  );
                } catch {}
              }
            }
          } catch {}
        };
        ws.onclose = async (event) => {
          if (wsTearingDownRef.current) return;
          if (event.code === 1008) {
            const tok = await refreshAccessToken();
            if (tok === false) return;
            wsBackoff = 1000;
          }
          const delay = wsBackoff;
          wsBackoff = Math.min(wsBackoff * 2, 60000);
          wsReconnectTimer.current = setTimeout(connect, delay);
        };
        ws.onerror = () => {};
      } catch {
        if (!wsTearingDownRef.current) {
          const delay = wsBackoff;
          wsBackoff = Math.min(wsBackoff * 2, 60000);
          wsReconnectTimer.current = setTimeout(connect, delay);
        }
      }
    };
    wsTearingDownRef.current = false;
    connect();
    return () => {
      wsTearingDownRef.current = true;
      wsBackoff = 1000;
      if (wsReconnectTimer.current) clearTimeout(wsReconnectTimer.current);
      const sock = wsRef.current;
      if (!sock) return;
      // Closing a socket mid-handshake logs a benign Chrome warning; if it's
      // still CONNECTING, defer close() until it opens/errors. Mirrors AdminDashboard.
      if (sock.readyState === WebSocket.CONNECTING) {
        const closeWhenReady = () => {
          try {
            sock.close();
          } catch {}
        };
        sock.addEventListener("open", closeWhenReady, { once: true });
        sock.addEventListener("error", closeWhenReady, { once: true });
      } else {
        try {
          sock.close();
        } catch {}
      }
    };
  }, []);

  // Push auth token to local desktop tracker on port 8765.
  // This lets employees who use a regular browser (not the Electron built-in
  // window) have their token picked up by the running desktop app so that
  // screenshots and heartbeats are authenticated correctly.
  useEffect(() => {
    const tok = getToken();
    if (!tok) return;
    const push = () => {
      // Skip the convenience push while the tab is hidden — the desktop tracker
      // only needs a fresh token while the user is actively on the page, and
      // avoiding background POSTs saves a needless request every 60s.
      if (typeof document !== "undefined" && document.hidden) return;
      fetch("http://127.0.0.1:8765/set-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token: tok,
          name: localStorage.getItem("name") || "",
          email: localStorage.getItem("email") || "",
          role: localStorage.getItem("role") || "employee",
          user_id: localStorage.getItem("user_id") || "",
        }),
      }).catch(() => {});
    };
    push();
    const iv = setInterval(push, 60000);
    return () => clearInterval(iv);
  }, []);

  // Session restore on mount — guarded with a ref so React StrictMode's
  // intentional double-invocation in dev doesn't fire two simultaneous
  // /employees/me fetches, which caused a loading-screen flicker that made
  // the "Open My Dashboard" button appear unresponsive for 4-5 clicks.
  useEffect(() => {
    if (sessionRestoreDone.current) return;
    sessionRestoreDone.current = true;
    const tok = getToken();
    if (!tok) {
      navigate("/login");
      return;
    }
    // Deep links from the desktop app arrive with ?tab=... — they want the
    // full dashboard directly, not the clock-in/stopped popup overlay. Skip
    // the popup for any URL that names a target tab.
    let hasTabDeepLink = false;
    try {
      hasTabDeepLink = !!new URLSearchParams(window.location.search).get("tab");
    } catch {}
    fetch(`${API_BASE}/employees/me`, {
      headers: { Authorization: `Bearer ${tok}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d) {
          setLoading(false);
          return;
        }
        // Update name/email from API if better than localStorage
        const apiName = d.name || d.full_name || d.username;
        if (apiName && apiName !== "Employee") {
          setProfileName(apiName);
          localStorage.setItem("name", apiName);
        }
        if (d.email) {
          setProfileEmail(d.email);
          localStorage.setItem("email", d.email);
        }
        if (d.employment_type) setEmpType(d.employment_type);
        if (Number.isFinite(Number(d.expected_work_seconds))) {
          setExpectedWorkSecs(Number(d.expected_work_seconds));
        }
        setDesktopClockInRequired(!!d.desktop_clock_in_required);
        // Set accumulated previous sessions' work for daily total
        prevWorkedSecsRef.current = d.worked_today_seconds || 0;
        if (d.clocked_in) {
          const startMs = safeDate(d.clock_in_time).getTime();
          clockInMsRef.current = startMs;
          // See pollStatus comment: backend's break_elapsed_seconds already
          // includes the running break, so we must subtract current_break
          // here to avoid double-counting in the local tick.
          const totalBreakSecs = d.break_elapsed_seconds || 0;
          const currentBreakSecs = d.current_break_seconds || 0;
          totalBreakMsRef.current =
            (d.on_break
              ? Math.max(0, totalBreakSecs - currentBreakSecs)
              : totalBreakSecs) * 1000;
          setClockInTimeUTC(d.clock_in_time);
          setClockedIn(true);
          if (d.on_break) {
            setOnBreak(true);
            // Back-date breakStartMsRef so the break timer resumes from where
            // it actually started, not from the moment of this page load.
            breakStartMsRef.current = Date.now() - currentBreakSecs * 1000;
          } else if (!lastBreakEndMsRef.current) {
            // Initial fallback for the deep-work alert anchor. If the worker
            // has already taken break time today, reset the anchor to "now"
            // so the alert doesn't count earlier break time as continuous
            // work after a page reload. Without backend support for the
            // exact last-break-end timestamp, this is the safe approximation.
            lastBreakEndMsRef.current =
              (d.break_elapsed_seconds || 0) > 0 ? Date.now() : startMs;
          }
          setLoading(false);
          startTick();
        } else {
          setDailyWorkedSecs(prevWorkedSecsRef.current);
          setLoading(false);
        }
      })
      .catch(() => setLoading(false));
  }, [startTick, navigate]);

  // Midnight reset — detect date change and re-fetch state so yesterday's
  // session doesn't linger on screen the next morning.
  useEffect(() => {
    let lastDate = new Date().toDateString();
    const iv = setInterval(() => {
      const now = new Date().toDateString();
      if (now !== lastDate) {
        lastDate = now;
        // Reset local accumulators
        prevWorkedSecsRef.current = 0;
        setDailyWorkedSecs(0);
        setWorkedSeconds(0);
        // Re-fetch from backend — the /me endpoint auto-closes stale sessions
        const tok = getToken();
        if (!tok) return;
        fetch(`${API_BASE}/employees/me`, {
          headers: { Authorization: `Bearer ${tok}` },
        })
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => {
            if (d) setDesktopClockInRequired(!!d.desktop_clock_in_required);
            if (!d || !d.clocked_in) {
              stopTick();
              setClockedIn(false);
              setOnBreak(false);
              setLoading(false);
            }
          })
          .catch(() => {});
      }
    }, 30_000); // check every 30s
    return () => clearInterval(iv);
  }, [stopTick]);

  // Screenshots fetch
  useEffect(() => {
    if (tab !== "screenshots") return;
    const tok = getToken();
    if (!tok) return;
    const uid = localStorage.getItem("user_id");
    const doFetch = () => {
      const params = new URLSearchParams();
      if (uid) params.set("user_id", String(parseInt(uid)));
      params.set("limit", "500");
      params.set("sort", empSsSort);
      if (empSsDate) params.set("date", empSsDate);
      fetch(`${API_BASE}/api/screenshots?${params}`, {
        headers: { Authorization: `Bearer ${tok}` },
      })
        .then((r) => {
          if (!r.ok) {
            console.error(`[screenshots] HTTP ${r.status}`);
            return [];
          }
          return r.json();
        })
        .then((d) => {
          if (Array.isArray(d)) setEmpScreenshots(d);
        })
        .catch((err) => {
          console.error("[screenshots] Fetch error:", err);
        })
        .finally(() => setEmpSsLoading(false));
    };
    setEmpSsLoading(true);
    doFetch();
    // PERF: don't re-pull the screenshot grid (up to 500 rows) while the tab
    // is hidden; the initial doFetch() above still runs immediately on mount.
    const iv = setInterval(() => {
      if (!document.hidden) doFetch();
    }, 30000);
    return () => clearInterval(iv);
  }, [tab, empSsSort, empSsDate, ssRefreshTick]);

  // Today's activity summary — keeps the four KPI tiles (Total Worked /
  // Active Time / Idle Time % / Productive Apps %) on the Today tab in lock
  // step with the Activity Log page. We fetch only when this tab is visible
  // to avoid pinging the summary endpoint while the user is on a different
  // tab; refresh every 30 s while visible so the tiles tick toward the
  // Activity Log's numbers.
  useEffect(() => {
    if (tab !== "today") return;
    const tok = getToken();
    if (!tok) return;

    let cancelled = false;

    const isoStartOfToday = () => {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      return d.toISOString();
    };
    const isoEndOfToday = () => {
      const d = new Date();
      d.setHours(23, 59, 59, 999);
      return d.toISOString();
    };

    const load = async () => {
      try {
        const url =
          `${API_BASE}/api/activity-log/summary` +
          `?scope=self` +
          `&start=${encodeURIComponent(isoStartOfToday())}` +
          `&end=${encodeURIComponent(isoEndOfToday())}`;
        const res = await fetch(url, {
          headers: { Authorization: `Bearer ${tok}` },
        });
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const t = data?.totals || {};
        const p = data?.productivity || {};
        const totalCategorised =
          (p.productive || 0) + (p.neutral || 0) + (p.distracting || 0);
        setTodayActivity({
          activeSecs: t.active_seconds || 0,
          idleSecs: t.idle_seconds || 0,
          productivePct:
            totalCategorised > 0
              ? Math.round(((p.productive || 0) / totalCategorised) * 100)
              : 0,
        });
      } catch {
        /* best-effort — keep last known good values */
      }
    };

    load();
    const iv = setInterval(() => {
      if (!document.hidden) load();
    }, 30000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [tab]);

  // Holidays fetch — needed both for the dedicated "Public Holidays" tab and
  // for the "Upcoming Public Holidays" card on the My Work home view.
  useEffect(() => {
    if (tab !== "holidays" && tab !== "my-work") return;
    const tok = getToken();
    if (!tok) return;
    fetch(`${API_BASE}/holidays`, {
      headers: { Authorization: `Bearer ${tok}` },
    })
      .then((r) => (r.ok ? r.json() : []))
      .then((d) => {
        if (Array.isArray(d)) setHolidays(d);
      })
      .catch(() => {});
  }, [tab]);

  // Day-summary fetch for the My Timesheet view. Runs for any selected
  // day (including today) so `active_seconds` is sourced from the backend's
  // accurate per-tick math instead of being faked from `dailyWorkedSecs`
  // (which conflated "worked" with "active" — BUG-007). Today re-fetches
  // every 60s so the active counter stays current. Historical days are
  // immutable for the day so the in-memory cache is enough.
  useEffect(() => {
    if (tab !== "timesheet") {
      setTsHistory(null);
      return;
    }
    const tok = getToken();
    if (!tok) return;
    const d = new Date();
    d.setDate(d.getDate() + tsDateOffset);
    // IST calendar date — same logic the visible label uses.
    const istDate = new Date(
      d.toLocaleString("en-US", { timeZone: getOrgTimeZone() }),
    );
    const dateStr = `${istDate.getFullYear()}-${String(istDate.getMonth() + 1).padStart(2, "0")}-${String(istDate.getDate()).padStart(2, "0")}`;
    const isToday = tsDateOffset === 0;
    const cached = !isToday ? tsHistoryCacheRef.current.get(dateStr) : null;
    if (cached) {
      setTsHistory(cached);
      setTsHistoryLoading(false);
      return;
    }
    let cancelled = false;
    setTsHistoryLoading(true);
    const fetchOnce = () =>
      fetch(`${API_BASE}/employees/me/day-summary?date=${dateStr}`, {
        headers: { Authorization: `Bearer ${tok}` },
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (cancelled) return;
          if (data && !isToday) tsHistoryCacheRef.current.set(dateStr, data);
          setTsHistory(data);
        })
        .catch(() => {
          if (!cancelled) setTsHistory(null);
        })
        .finally(() => {
          if (!cancelled) setTsHistoryLoading(false);
        });
    fetchOnce();
    // Poll every 60s while viewing today so active_seconds keeps up with
    // newly-written tracker activity. Historical days don't need polling.
    let iv = null;
    if (isToday) iv = setInterval(fetchOnce, 60_000);
    // Live push: admin added/reverted a time adjustment for this user. Bypass
    // the 60s poll wait and refetch immediately so the "Total Worked Hours"
    // tile reflects the change. The window event is dispatched by the
    // websocket onmessage handler above when it sees `time_adjustment_updated`
    // for this user_id. We refetch even for non-today dates because the
    // adjustment may have been against a past day the user is currently viewing.
    const onAdjustmentChange = () => {
      fetchOnce();
    };
    window.addEventListener("ts_adjustment_changed", onAdjustmentChange);
    return () => {
      cancelled = true;
      if (iv) clearInterval(iv);
      window.removeEventListener("ts_adjustment_changed", onAdjustmentChange);
    };
  }, [tab, tsDateOffset]);

  // AI Daily Summary fetch — also triggered for "my-work" so the Top Apps
  // pie chart on the My Work tab is populated from real backend data.
  // Re-fires when the user changes the date or hits the manual refresh button.
  useEffect(() => {
    if (tab !== "ai-summary" && tab !== "my-work") return;
    const tok = getToken();
    if (!tok) return;
    const uid = localStorage.getItem("user_id");
    if (!uid) return;
    setAiLoading(true);
    const dailyUrl = aiSummaryDate
      ? `${API_BASE}/ai/summary/${uid}?date=${encodeURIComponent(aiSummaryDate)}`
      : `${API_BASE}/ai/summary/${uid}`;
    Promise.all([
      fetch(dailyUrl, {
        headers: { Authorization: `Bearer ${tok}` },
      }).then((r) => (r.ok ? r.json() : null)),
      fetch(`${API_BASE}/ai/summary-weekly/${uid}`, {
        headers: { Authorization: `Bearer ${tok}` },
      }).then((r) => (r.ok ? r.json() : null)),
    ])
      .then(([daily, weekly]) => {
        setAiSummary(daily);
        setAiWeekly(weekly);
      })
      .catch(() => {})
      .finally(() => setAiLoading(false));
  }, [tab, aiSummaryDate, aiRefreshNonce]);

  // ── Earnings data fetch ────────────────────────────────────────────────────
  useEffect(() => {
    if (tab !== "earnings") return;
    const tok = getToken();
    if (!tok) return;
    setEarningsLoading(true);
    fetch(`${API_BASE}/payroll/my-config`, {
      headers: { Authorization: `Bearer ${tok}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setEarningsData(d && d.config ? d.config : null))
      .catch(() => setEarningsData(null))
      .finally(() => setEarningsLoading(false));
  }, [tab]);

  // ── Close profile menu on outside click ───────────────────────────────────
  useEffect(() => {
    if (!profileMenuOpen) return;
    const handler = (e) => {
      if (
        profileMenuRef.current &&
        !profileMenuRef.current.contains(e.target)
      ) {
        setProfileMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [profileMenuOpen]);

  // Clock in
  const handleClockIn = async () => {
    // Desktop-app-only attendance. The button is hidden, so this is only
    // reachable from a stale tab or a keyboard path; the server rejects the
    // call regardless. Belt-and-braces so no website control can ever mutate
    // attendance state.
    if (!attendanceActionsAllowed) return;
    const tok = getToken();
    if (!tok) {
      navigate("/login");
      return;
    }
    setClockingIn(true);
    try {
      const res = await fetchWithRetry(`${API_BASE}/employees/clock-in`, {
        method: "POST",
        headers: { Authorization: `Bearer ${tok}` },
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        // Desktop-only clock-in policy. Reached only by a tab whose
        // /employees/me flag is stale (the button is already hidden when the
        // flag is set) or by a direct API call. Latch the flag so the button
        // swaps to the message instead of showing "[object Object]".
        if (res.status === 403 && e.detail?.code === "DESKTOP_APP_REQUIRED") {
          setDesktopClockInRequired(true);
          showError(e.detail.message || DESKTOP_APP_CLOCK_IN_MSG);
          return;
        }
        throw new Error(e.detail || `Error ${res.status}`);
      }
      const d = await res.json();
      if (d.success === false) {
        const me = await fetch(`${API_BASE}/employees/me`, {
          headers: { Authorization: `Bearer ${tok}` },
        }).then((r) => r.json());
        if (me.clocked_in) {
          prevWorkedSecsRef.current = me.worked_today_seconds || 0;
          clockInMsRef.current = safeDate(me.clock_in_time).getTime();
          // See pollStatus comment: subtract running break from total to
          // prevent the worked-time counter from ticking backwards.
          const totalBreakSecs = me.break_elapsed_seconds || 0;
          const currentBreakSecs = me.current_break_seconds || 0;
          totalBreakMsRef.current =
            (me.on_break
              ? Math.max(0, totalBreakSecs - currentBreakSecs)
              : totalBreakSecs) * 1000;
          setClockInTimeUTC(me.clock_in_time);
          setClockedIn(true);
          if (me.on_break) {
            setOnBreak(true);
            breakStartMsRef.current = Date.now() - currentBreakSecs * 1000;
          } else if (!lastBreakEndMsRef.current) {
            // Same approximation as the primary load path — if any break time
            // has been recorded today, reset the deep-work anchor to "now"
            // rather than clock-in, so the alert doesn't fire based on
            // wall-clock time that includes break minutes.
            lastBreakEndMsRef.current =
              (me.break_elapsed_seconds || 0) > 0
                ? Date.now()
                : safeDate(me.clock_in_time).getTime();
          }
          setLoading(false);
          startTick();
        }
        return;
      }
      // Update UI immediately — don't wait for /me
      const startMs = d.clock_in_time
        ? safeDate(d.clock_in_time).getTime()
        : Date.now();
      clockInMsRef.current = startMs;
      totalBreakMsRef.current = 0;
      breakStartMsRef.current = null;
      lastBreakEndMsRef.current = startMs;
      deepWorkDismissedRef.current = false;
      setShowDeepWorkAlert(false);
      setClockInTimeUTC(d.clock_in_time || new Date(startMs).toISOString());
      setClockOutTimeUTC(null);
      setClockOutMsgIdx(null);
      setClockedIn(true);
      setOnBreak(false);
      window.electronTracking?.updateStatus("working");
      setElapsed(0);
      setBreakElapsed(0);
      setLoading(false);
      startTick();
      // Invalidate cache so next /me read gets fresh state with updated worked_today_seconds
      invalidateMe();
    } catch (e) {
      showError(`Clock-in failed: ${e.message}`);
    } finally {
      setClockingIn(false);
    }
  };

  // Clock out
  // POST /employees/clock-out. When `reasonText` is provided (Early Clock Out
  // re-submit) it is sent in the JSON body. Returns the parsed response.
  const postClockOut = async (reasonText) => {
    const tok = getToken();
    if (!tok) throw new Error("Not authenticated");
    const opts = {
      method: "POST",
      headers: { Authorization: `Bearer ${tok}` },
    };
    if (reasonText != null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify({ reason: reasonText });
    }
    const res = await fetchWithRetry(`${API_BASE}/employees/clock-out`, opts);
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.detail || `Error ${res.status}`);
    }
    return await res.json();
  };

  // Apply a successful clock-out response to local UI state. Unchanged from the
  // original handler — extracted so both the direct and Early Clock Out paths
  // reuse the exact same completion logic.
  const finalizeClockOut = (d) => {
    const worked =
      d.worked_seconds != null ? d.worked_seconds : elapsedRef.current;
    prevWorkedSecsRef.current += worked;
    setDailyWorkedSecs(prevWorkedSecsRef.current);
    setWorkedSeconds(prevWorkedSecsRef.current);
    setClockOutTimeUTC(d.clock_out_time || new Date().toISOString());
    setClockOutMsgIdx(Math.floor(Math.random() * 5));
    setClockedIn(false);
    setOnBreak(false);
    window.electronTracking?.updateStatus("not_clocked_in");
    clockInMsRef.current = null;
    breakStartMsRef.current = null;
    totalBreakMsRef.current = 0;
    lastBreakEndMsRef.current = null;
    setShowDeepWorkAlert(false);
    deepWorkDismissedRef.current = false;
    setLoading(false);
    // Invalidate cache so next /me read gets fresh state
    invalidateMe();
  };

  const handleClockOut = async () => {
    // Desktop-app-only attendance — see handleClockIn.
    if (!attendanceActionsAllowed) return;
    const tok = getToken();
    if (!tok) return;
    if (clockOutBusyRef.current) return;   // ignore double-clicks while in flight
    clockOutBusyRef.current = true;
    setClockOutBusy(true);
    stopTick();
    try {
      const d = await postClockOut(null);
      // TEMPORARILY DISABLED: Early Clock Out is inactive, so this branch never
      // runs (the backend gate is off and no longer returns the code). Kept for
      // future re-enable — see EARLY_CLOCK_OUT_ENABLED in EarlyClockOutModal.
      if (EARLY_CLOCK_OUT_ENABLED && d && d.code === "EARLY_CLOCK_OUT_REASON_REQUIRED") {
        // Below required hours on a non-flexible day — collect a typed reason.
        // The employee is still clocked in, so keep the timer running.
        setEarlyClockOut(d);
        setEarlyReason("");
        setEarlyError("");
        startTick();
        return;
      }
      finalizeClockOut(d);
    } catch (e) {
      showError(`Clock-out failed: ${e.message}`);
      startTick();
    } finally {
      clockOutBusyRef.current = false;
      setClockOutBusy(false);
    }
  };

  // Early Clock Out modal — submit the typed reason and complete the clock-out.
  const submitEarlyClockOut = async () => {
    const reason = (earlyReason || "").trim();
    if (reason.length < EARLY_REASON_MIN) {
      setEarlyError(`Please enter at least ${EARLY_REASON_MIN} characters.`);
      return;
    }
    if (reason.length > EARLY_REASON_MAX) {
      setEarlyError(`Reason must be ${EARLY_REASON_MAX} characters or fewer.`);
      return;
    }
    setEarlySubmitting(true);
    setEarlyError("");
    try {
      const d = await postClockOut(reason);
      if (d && d.code === "EARLY_CLOCK_OUT_REASON_REQUIRED") {
        // Server rejected the reason (still too short / invalid).
        setEarlyError(d.error || "Please provide a valid reason.");
        setEarlySubmitting(false);
        return;
      }
      setEarlySubmitting(false);
      setEarlyClockOut(null);
      setEarlyReason("");
      stopTick();
      finalizeClockOut(d);
    } catch (e) {
      setEarlySubmitting(false);
      setEarlyError(e.message || "Clock-out failed");
    }
  };

  const cancelEarlyClockOut = () => {
    // Stay clocked in; the timer is already running.
    setEarlyClockOut(null);
    setEarlyReason("");
    setEarlyError("");
  };

  // Break toggle — start break immediately, show ProHance modal on resume.
  // `breakType` is "break" (short, ~15m) or "lunch" (long, ~60m); the backend
  // uses it to pick the right overtime threshold for the alert popup.
  const handleBreakToggle = async (breakType = "break") => {
    // Desktop-app-only attendance — see handleClockIn. Covers both directions
    // (start break and resume work) and every caller, including the break
    // picker and the break-overrun modal's Resume Work button.
    if (!attendanceActionsAllowed) return;
    const tok = getToken();
    if (!tok) return;
    if (!onBreak) {
      try {
        const res = await fetchWithRetry(`${API_BASE}/employees/start-break`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${tok}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            break_type: breakType === "lunch" ? "lunch" : "break",
          }),
        });
        if (!res || !res.ok) {
          const errData = await res?.json().catch(() => ({}));
          const errMsg = errData?.detail || errData?.message || "";
          // Backend says we're already on break — sync frontend state so the
          // button correctly shows "Resume Work" instead of "Take a Break".
          if (
            /already.*(on\s*break|break)/i.test(errMsg) ||
            /on\s*break/i.test(errMsg)
          ) {
            setOnBreak(true);
            showError(
              'You are already on a break. Use "Resume Work" to end it first.',
            );
          } else {
            showError(errMsg || "Could not start break. Please try again.");
          }
          return;
        }
        const data = await res.json().catch(() => ({}));
        if (!data.success) {
          const msg = data.message || data.detail || "";
          // "Already on break" response with success:false — same state sync.
          if (
            /already.*(on\s*break|break)/i.test(msg) ||
            /on\s*break/i.test(msg)
          ) {
            setOnBreak(true);
            showError(
              'You are already on a break. Use "Resume Work" to end it first.',
            );
          } else {
            // Session may have been auto-closed — refresh status so the employee
            // sees they are no longer clocked in and can re-clock in if needed.
            showError(msg || "Your session has ended. Please clock in again.");
            try {
              const me = await fetch(`${API_BASE}/employees/me`, {
                headers: { Authorization: `Bearer ${tok}` },
              });
              if (me.ok) {
                const meData = await me.json();
                if (!meData.clocked_in) {
                  setClockedIn(false);
                  setOnBreak(false);
                }
              }
            } catch {}
          }
          return;
        }
      } catch {
        showError("Network error. Please try again.");
        return;
      }
      breakStartMsRef.current = Date.now();
      setOnBreak(true);
      window.electronTracking?.updateStatus("on_break");
      setShowDeepWorkAlert(false);
      // Invalidate cache so next /me read gets fresh state
      invalidateMe();
    } else {
      const tok = getToken();
      if (!tok) return;
      try {
        const resumeRes = await fetchWithRetry(`${API_BASE}/break/resume`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${tok}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            reason: "Resumed",
            activity: "Resumed",
          }),
        });
        if (!resumeRes || !resumeRes.ok) {
          const d = await resumeRes?.json().catch(() => ({}));
          showError(
            d?.detail ||
              d?.message ||
              "Could not resume work. Please try again.",
          );
          return;
        }
      } catch {
        showError("Network error resuming work. Please try again.");
        return;
      }
      if (breakStartMsRef.current !== null) {
        totalBreakMsRef.current += Date.now() - breakStartMsRef.current;
        breakStartMsRef.current = null;
      }
      lastBreakEndMsRef.current = Date.now();
      deepWorkDismissedRef.current = false;
      setShowDeepWorkAlert(false);
      setOnBreak(false);
      window.electronTracking?.updateStatus("working");
      // Invalidate cache so next /me read gets fresh state
      invalidateMe();
    }
  };

  // ── Break-overrun blocking modal control ──────────────────────────────────
  // While on a break, poll /break/active every 15s. When the break is over its
  // allowance, surface the modal unless the worker snoozed it. Hitting the
  // endpoint also makes the backend emit the break_overtime broadcast, so the
  // admin bell + HR escalation fire promptly. Clears when the break ends.
  useEffect(() => {
    if (!onBreak) {
      setOverrunInfo(null);
      setShowOverrunModal(false);
      overrunSnoozeUntilRef.current = 0;
      return;
    }
    let cancelled = false;
    const poll = async () => {
      const tok = getToken();
      if (!tok) return;
      try {
        const r = await fetch(`${API_BASE}/break/active`, {
          headers: { Authorization: `Bearer ${tok}` },
        });
        if (!r.ok) return;
        const d = await r.json();
        if (cancelled) return;
        if (
          d &&
          d.on_break &&
          (d.notification_due ?? (d.is_overtime || d.exceeded))
        ) {
          setOverrunInfo(d);
          if (Date.now() >= overrunSnoozeUntilRef.current)
            setShowOverrunModal(true);
        } else {
          setOverrunInfo(null);
          setShowOverrunModal(false);
        }
      } catch {}
    };
    poll();
    // PERF: increased from 15s to 30s poll cadence to reduce load. Break overrun
    // state is reconciled from /employees/me governance view on the regular 30s poll.
    // Tab hidden still skips the poll for efficiency.
    const iv = setInterval(() => {
      if (!document.hidden) poll();
    }, 30_000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [onBreak]);

  const snoozeOverrun = () => {
    overrunSnoozeUntilRef.current = Date.now() + OVERRUN_NAG_MS_WEB;
    setShowOverrunModal(false);
  };
  const resumeFromOverrun = () => {
    overrunSnoozeUntilRef.current = 0;
    setShowOverrunModal(false);
    handleBreakToggle(); // no arg → resume (ends the active break)
  };

  const clockInStr = fmtTimeIST(clockInTimeUTC);
  const clockOutStr = fmtTimeIST(clockOutTimeUTC);
  const gross =
    (+pay.basic || 0) + (+pay.hra || 0) + (+pay.da || 0) + (+pay.ta || 0);
  const deduct = (+pay.pf || 0) + (+pay.tax || 0);
  const net = gross - deduct;

  // ── Employee Sidebar — Workforce Surface Spec (locked structure) ────
  // Existing tab IDs (my-work, screenshots, timesheet, leave,
  // holidays, payroll, ai-summary) are intentionally
  // preserved so route handlers, deep-links, and analytics keep working —
  // only the user-facing labels and section headings change.
  // Items marked `placeholder: true` render the new "Coming Soon" surface.
  const SECTIONS = useMemo(
    () => [
      {
        key: "HOME",
        label: T.sectionHome || "Home",
        items: [
          { id: "my-work", icon: "home", label: T.today || "Today" },
          {
            id: "my-schedule",
            icon: "calendar",
            label: T.mySchedule || "My Schedule",
          },
        ],
      },
      {
        key: "WORK_ACTIVITY",
        label: T.sectionWorkActivity || "Work & Activity",
        items: [
          {
            id: "activity-log",
            icon: "activity",
            label: T.activityLog || "Activity Log",
          },
          ...(appSettings.perm_emp_see_screenshot
            ? [
                {
                  id: "screenshots",
                  icon: "camera",
                  label: T.screenshots || "Screenshots",
                },
              ]
            : []),
        ],
      },
      {
        key: "TIME_ATTENDANCE",
        label: T.sectionTimeAttendance || "Time & Attendance",
        items: [
          ...(appSettings.perm_emp_see_timesheet
            ? [
                {
                  id: "timesheet",
                  icon: "calendar",
                  label: T.myTimesheet || "My Timesheet",
                },
              ]
            : []),
        ],
      },
      {
        key: "TIME_OFF",
        label: T.sectionTimeOff || "Time Off",
        items: [
          {
            id: "leave",
            icon: "leaf",
            label: T.leaveAndRequests || "Leave & Requests",
          },
          {
            id: "holidays",
            icon: "settings",
            label: T.publicHolidays || "Public Holidays",
          },
        ],
      },
      {
        key: "PAY",
        label: T.sectionPay || "Pay",
        items: [
          {
            id: "my-pay",
            icon: "dollar",
            label: T.myPay || "My Pay",
            group: true,
            children: [
              {
                id: "earnings",
                icon: "trending_up",
                label: T.earningsEstimate || "Estimate",
              },
              ...(appSettings.payroll_enabled
                ? [
                    {
                      id: "payroll",
                      icon: "receipt",
                      label: T.payslips || "Payslips",
                    },
                  ]
                : []),
            ],
          },
        ],
      },
      {
        key: "INSIGHTS",
        label: T.sectionInsights || "Insights",
        items: [
          {
            id: "ai-summary",
            icon: "insights",
            label: T.dailySummary || "Daily Summary",
          },
        ],
      },
      {
        key: "MY_DATA",
        label: T.sectionMyData || "My Data",
        items: [
          {
            id: "my-data",
            icon: "database",
            label: T.myData || "My Data",
            group: true,
            children: [
              {
                id: "whats-recorded",
                icon: "database",
                label: T.whatsRecorded || "What's Recorded",
              },
              {
                id: "who-viewed",
                icon: "history",
                label: T.whoViewedMyData || "Who Viewed My Data",
              },
              {
                id: "privacy-controls",
                icon: "privacy",
                label: T.privacyControls || "Privacy Controls",
              },
              {
                id: "corrections",
                icon: "info",
                label: T.correctionsDisputes || "Corrections & Disputes",
              },
              {
                id: "data-export",
                icon: "download",
                label: T.myDataExport || "My Data Export",
              },
            ],
          },
        ],
      },
      {
        key: "ACCOUNT",
        label: T.sectionAccount || "Account",
        items: [
          { id: "profile", icon: "user", label: T.profile || "Profile" },
          {
            id: "notifications",
            icon: "bell",
            label: T.notifications || "Notifications",
          },
          { id: "help", icon: "help", label: T.help || "Help" },
          { id: "settings", icon: "settings", label: T.settings || "Settings" },
        ],
      },
    ],
    [T, appSettings],
  );

  // Flatten for hover/look-ups + back-compat consumers (NAV.find(...)).
  const NAV = useMemo(
    () =>
      SECTIONS.flatMap((s) =>
        s.items.flatMap((it) => (it.group ? [it, ...it.children] : [it])),
      ),
    [SECTIONS],
  );

  const card = (ex = {}) => ({
    background: C.surface,
    borderRadius: 12,
    border: `1px solid ${C.border}`,
    boxShadow: "0 1px 3px rgba(0,0,0,0.04)",
    ...ex,
  });
  const lbl = {
    fontSize: 11,
    fontWeight: 600,
    color: C.textLight,
    textTransform: "uppercase",
    letterSpacing: "0.7px",
    display: "block",
    marginBottom: 5,
  };
  const inp = {
    width: "100%",
    border: `1px solid ${C.border}`,
    borderRadius: C.rSm,
    padding: "9px 12px",
    fontSize: 13,
    fontFamily: "inherit",
    color: C.text,
    outline: "none",
    boxSizing: "border-box",
    background: C.surface,
  };

  // Distribute wall-clock seconds since clock-in into 24 hour-of-day buckets
  // so each bar in the Activities chart represents the local hour the user
  // was working. Reading `elapsed` keeps this re-computing every tick.
  // Previous version hardcoded bars at indices 9..11 which, against the
  // 24-bar chart with "9AM..9PM" labels, visually landed at 12PM–3PM
  // regardless of when the user actually clocked in (BUG-003).
  const actHours = (() => {
    void elapsed; // re-render trigger — depends on the per-second tick
    const arr = new Array(24).fill(0);
    // While clocked in, the ref holds the live start; after clock-out the
    // ref is nulled but `clockInTimeUTC` state still holds the start of the
    // session that just ended, so the chart stays accurate post-clock-out.
    const startMs =
      clockInMsRef.current ??
      (clockInTimeUTC ? new Date(clockInTimeUTC).getTime() : null);
    if (!startMs) return arr;
    const endMs = clockOutTimeUTC
      ? new Date(clockOutTimeUTC).getTime()
      : Date.now();
    if (endMs <= startMs) return arr;
    let cur = new Date(startMs);
    while (cur.getTime() < endMs) {
      const hr = cur.getHours();
      const next = new Date(cur);
      next.setHours(cur.getHours() + 1, 0, 0, 0);
      const segEnd = Math.min(next.getTime(), endMs);
      arr[hr] += (segEnd - cur.getTime()) / 1000;
      cur = new Date(segEnd);
    }
    // 1.0 = a full hour worked (3600s) → bar fills to the top.
    return arr.map((s) => Math.min(1, s / 3600));
  })();
  const maxAct = Math.max(...actHours, 1);
  // ── Top Apps & Websites — derived from /ai/summary/{uid} ───────────────────
  // Backend returns `top_apps: [{app_name, count}, ...]` already sorted by
  // count desc. We pick the first 6, normalise counts to percentages, and
  // attach a colour. Falls back to a single "No data" slice when the user
  // hasn't been tracked yet so the pie chart never renders empty.
  const APP_COLORS = [
    { color: "#8B5CF6", bg: "#EDE9FE" },
    { color: "#06B6D4", bg: "#CFFAFE" },
    { color: "#22C55E", bg: "#DCFCE7" },
    { color: "#3B82F6", bg: "#DBEAFE" },
    { color: "#F59E0B", bg: "#FEF3C7" },
    { color: "#EF4444", bg: "#FEE2E2" },
  ];
  const apps = useMemo(() => {
    const raw = (aiSummary?.top_apps || []).slice(0, 6);
    if (raw.length === 0) {
      return [
        {
          name: T.noData || "No data yet",
          pct: 100,
          color: "#CBD5E1",
          bg: "#F1F5F9",
        },
      ];
    }
    const total = raw.reduce((s, a) => s + (a.count || 0), 0) || 1;
    let used = 0;
    return raw.map((a, i) => {
      // Round so the slices sum to exactly 100% on the last entry
      const pct =
        i === raw.length - 1
          ? Math.max(0, 100 - used)
          : Math.round((a.count / total) * 100);
      used += pct;
      return {
        name: a.app_name || "Unknown",
        pct,
        ...APP_COLORS[i % APP_COLORS.length],
      };
    });
    // APP_COLORS holds only static literals (constant value every render), so it
    // is intentionally excluded from the deps to keep this memo effective.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aiSummary, T]);

  // ── Top Tools — desktop applications the user works in (Eclipse, VS Code,
  // Figma, Office…), surfaced separately from websites. Browsers dominate the
  // raw top_apps list by count, so without this filter a user's actual tools
  // never appear. Process variants are merged under one friendly label.
  const tools = useMemo(() => {
    // Prefer the backend's dedicated, browser-excluded list (not capped by the
    // 5-app top_apps slice, so lower-ranked tools like Eclipse still surface).
    // Fall back to filtering top_apps for older backends that don't send it.
    const raw =
      aiSummary?.top_tools && aiSummary.top_tools.length
        ? aiSummary.top_tools
        : (aiSummary?.top_apps || []).filter((a) => isDesktopTool(a.app_name));
    const merged = {};
    raw.forEach((a) => {
      const label = prettyApp(a.app_name);
      merged[label] = (merged[label] || 0) + (a.count || 0);
    });
    const arr = Object.entries(merged)
      .map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 6);
    const total = arr.reduce((s, t) => s + t.count, 0) || 1;
    return arr.map((t, i) => ({
      ...t,
      pct: Math.round((t.count / total) * 100),
      ...APP_COLORS[i % APP_COLORS.length],
    }));
    // APP_COLORS is a static literal list (see note on `apps` above).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aiSummary]);
  const tsDate = new Date();
  tsDate.setDate(tsDate.getDate() + tsDateOffset);
  const isTodayTs = tsDateOffset === 0;
  const tsLabel = tsDate.toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: getOrgTimeZone(),
  });

  // Values rendered by the My Timesheet card — pick the live state when the
  // user is viewing today, otherwise the historical day-summary fetch. This
  // lets the same JSX serve both modes without duplicating the layout.
  const tsView = isTodayTs
    ? {
        loading: false,
        hasData: clockedIn || !!clockInTimeUTC,
        clockedIn: clockedIn,
        clockInUTC: clockInTimeUTC,
        clockOutUTC: clockOutTimeUTC,
        // Worked time keeps ticking live from the local timer.
        workedSecs: dailyWorkedSecs,
        // Active time comes from the backend's tick-accurate aggregate
        // (re-polled every 60s). Cap at workedSecs so a stale tick can't
        // make active > worked. Falls back to 0 until the first poll
        // returns rather than re-using workedSecs (which would conflate
        // "present" with "actively typing/clicking" — BUG-007).
        activeSecs: Math.min(
          tsHistory?.active_seconds || 0,
          dailyWorkedSecs || tsHistory?.active_seconds || 0,
        ),
        workedDays: clockedIn || clockInTimeUTC ? 1 : 0,
      }
    : {
        loading: tsHistoryLoading,
        hasData: !!tsHistory?.has_data,
        clockedIn: !!tsHistory?.clocked_in,
        clockInUTC: tsHistory?.clock_in_time || null,
        clockOutUTC: tsHistory?.clock_out_time || null,
        workedSecs: tsHistory?.worked_seconds || 0,
        activeSecs: tsHistory?.active_seconds || 0,
        workedDays: tsHistory?.worked_days || 0,
      };
  const tsClockInStr = fmtTimeIST(tsView.clockInUTC);
  const tsClockOutStr = fmtTimeIST(tsView.clockOutUTC);

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500;9..40,600;9..40,700;9..40,800;9..40,900&display=swap');
        *{box-sizing:border-box;margin:0;padding:0}
        html,body,#root{height:100%;font-family:'DM Sans',system-ui,sans-serif}
        body{background:${C.bg};color:${C.text};transition:background .25s,color .25s}
        /* Scrollbar styling lives in index.css (wide brand-blue thumb,
           green hover). Local override removed so it stays consistent. */
        @keyframes popIn{from{opacity:0;transform:scale(0.94) translateY(16px)}to{opacity:1;transform:none}}
        @keyframes fadeIn{from{opacity:0}to{opacity:1}}
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
        @keyframes spin{to{transform:rotate(360deg)}}
        .nav-item{transition:all 0.15s ease}
        .nav-item[data-active="false"]:hover{background:${C.brandAccentBg} !important; color:${C.brandAccent} !important}
        /* Green hover system — matches the AdminSidebar accent so every
           interactive surface in the employee dashboard reads as part of the
           same family as the admin shell. */
        .row-h{transition: background .15s ease}
        .row-h:hover{background:${C.brandAccentBg} !important}
        .card-hover{transition:transform .2s ease, box-shadow .2s ease, border-color .2s ease}
        .card-hover:hover{box-shadow:0 6px 18px ${C.brandAccent}1F;transform:translateY(-1px)}
        button:active{opacity:0.9;transform:scale(0.98)}

        /* ── Employee Sidebar — interactive hover system, mirrors AdminSidebar
              so the two surfaces feel like one product:
              - Green-tinted background + text + icon on hover
              - 2px slide-right + icon rotate / scale on hover
              - Diagonal sheen swipe across the button
              - Soft pulsing glow ring on the active item
              - Click ripple at the cursor (handled by spawnRipple JS) */
        @keyframes zteSheen {
          0%   { transform: translateX(-120%) skewX(-18deg); opacity: 0; }
          40%  { opacity: 1; }
          100% { transform: translateX(220%)  skewX(-18deg); opacity: 0; }
        }
        @keyframes zteGlow {
          0%,100% { box-shadow: inset 3px 0 0 ${C.brand}, 0 0 0 rgba(68,185,123,0); }
          50%     { box-shadow: inset 3px 0 0 ${C.brand}, 0 0 14px rgba(68,185,123,.28); }
        }

        .zte-nav { isolation: isolate; transition: background .18s ease, color .18s ease, transform .18s cubic-bezier(.34,1.56,.64,1); }
        .zte-nav::before {
          content: ""; position: absolute; inset: 0;
          background: linear-gradient(120deg, transparent 0%, rgba(68,185,123,.22) 50%, transparent 100%);
          transform: translateX(-120%) skewX(-18deg);
          pointer-events: none; opacity: 0; z-index: 0;
        }
        .zte-nav:hover::before { animation: zteSheen .65s ease forwards; }
        .zte-nav > * { position: relative; z-index: 1; }

        .zte-nav[data-active="false"]:hover {
          background: ${C.brandAccentBg} !important;
          color: ${C.brandAccent} !important;
          transform: translateX(2px);
        }
        .zte-nav[data-active="false"]:hover svg { stroke: ${C.brandAccent} !important; }
        .zte-nav[data-active="false"]:hover .zte-nav-ic {
          background: ${isDark ? "rgba(95,204,147,.16)" : "rgba(68,185,123,.14)"} !important;
        }
        .zte-nav[data-active="false"]:hover .zte-nav-dot {
          background: ${C.brandAccent} !important;
          transform: scale(1.4);
        }
        .zte-nav[data-active="true"]:hover { transform: translateX(2px); }
        .zte-nav:active { transform: translateX(2px) scale(.985); }
        .zte-nav:hover .zte-nav-ic { transform: rotate(-6deg) scale(1.12); }
        .zte-nav[data-active="true"] { animation: zteGlow 2.6s ease-in-out infinite; }
        .zte-nav[data-active="true"] .zte-nav-ic { transform: scale(1.08); }

        .zte-nav-ic { transition: color .18s ease, background .18s ease, transform .25s cubic-bezier(.34,1.56,.64,1); }
        .zte-nav svg { transition: stroke .18s ease, transform .25s ease; }

        /* Sub-items inherit the same green system, just lighter motion. */
        .zte-nav-child { transition: background .18s ease, color .18s ease, transform .18s cubic-bezier(.34,1.56,.64,1); }
        .zte-nav-child[data-active="false"]:hover {
          background: ${C.brandAccentBg} !important;
          color: ${C.brandAccent} !important;
          transform: translateX(3px);
        }
        .zte-nav-child[data-active="false"]:hover svg { stroke: ${C.brandAccent} !important; }
        .zte-nav-child[data-active="false"]:hover .zte-nav-dot {
          background: ${C.brandAccent} !important;
          transform: scale(1.4);
        }
        .zte-nav-child[data-active="true"]:hover { transform: translateX(3px); }

        /* Smooth grid-row collapse for sidebar groups (My Pay) */
        .zte-collapse { display: grid; grid-template-rows: 0fr; transition: grid-template-rows .22s ease; }
        .zte-collapse[data-open="true"] { grid-template-rows: 1fr; }
        .zte-collapse > .zte-collapse-inner { overflow: hidden; min-height: 0; }

        /* Click ripple — subtle */
        @keyframes zteRipple { to { transform: scale(2.2); opacity: 0; } }
        .zte-ripple {
          position: absolute; border-radius: 50%;
          background: rgba(68,185,123,.28); pointer-events: none;
          width: 14px; height: 14px; transform: scale(0); opacity: 1;
          animation: zteRipple .45s ease-out forwards; z-index: 0;
        }
      `}</style>

      {/* Loading screen */}
      {loading && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: "#F0F2F5",
            flexDirection: "column",
            gap: 16,
          }}
        >
          <img
            src={activeLogo}
            alt="Workspace"
            style={{ height: 40, width: "auto", objectFit: "contain" }}
          />
          <div
            style={{
              width: 32,
              height: 32,
              border: "3px solid #E5E7EB",
              borderTopColor: "#11496A",
              borderRadius: "50%",
              animation: "spin 0.8s linear infinite",
            }}
          />
        </div>
      )}

      {/* Global error toast */}
      {error && (
        <div
          style={{
            position: "fixed",
            top: 20,
            right: 20,
            zIndex: 9999,
            background: "#FEF2F2",
            border: "1px solid #FECACA",
            borderRadius: 8,
            padding: "12px 18px",
            fontSize: 13,
            color: "#DC2626",
            fontWeight: 600,
            boxShadow: "0 4px 16px rgba(0,0,0,0.1)",
            maxWidth: 360,
          }}
        >
          {error}
        </div>
      )}

      {/* ── SCREENSHOT LIGHTBOX ── */}
      {empLightbox && (
        <div
          onClick={() => setEmpLightbox(null)}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 99999,
            background: "rgba(0,0,0,0.88)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            cursor: "zoom-out",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              position: "relative",
              maxWidth: "92vw",
              borderRadius: 12,
              overflow: "hidden",
              boxShadow: "0 24px 80px rgba(0,0,0,0.7)",
              display: "flex",
              flexDirection: "column",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "10px 16px",
                background: "rgba(10,13,22,0.95)",
                backdropFilter: "blur(12px)",
                borderBottom: "1px solid rgba(255,255,255,0.08)",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                {empLightbox.appName && (
                  <div
                    style={{
                      width: 34,
                      height: 34,
                      borderRadius: "50%",
                      background: `hsl(${(empLightbox.appName.charCodeAt(0) * 37) % 360},60%,45%)`,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      color: "#fff",
                      fontWeight: 800,
                      fontSize: 13,
                      flexShrink: 0,
                    }}
                  >
                    {empLightbox.appName[0].toUpperCase()}
                  </div>
                )}
                <div>
                  <p
                    style={{
                      color: "#fff",
                      fontWeight: 700,
                      fontSize: 13,
                      margin: 0,
                      lineHeight: 1.3,
                    }}
                  >
                    {name}
                  </p>
                  {empLightbox.appName && (
                    <p
                      style={{
                        color: "#11496A",
                        fontSize: 11,
                        fontWeight: 600,
                        margin: 0,
                      }}
                    >
                      {empLightbox.appName}
                    </p>
                  )}
                </div>
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 16,
                  marginLeft: 24,
                }}
              >
                {empLightbox.timestamp && (
                  <span
                    style={{
                      color: "#A0ADB9",
                      fontSize: 12,
                      whiteSpace: "nowrap",
                    }}
                  >
                    🕐{" "}
                    {new Date(
                      /[Z+]/.test(String(empLightbox.timestamp).slice(-6))
                        ? empLightbox.timestamp
                        : empLightbox.timestamp + "Z",
                    ).toLocaleString("en-IN", {
                      day: "numeric",
                      month: "long",
                      year: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                      hour12: true,
                      timeZone: getOrgTimeZone(),
                    })}
                  </span>
                )}
                {empLightbox.keyPresses != null && (
                  <span
                    style={{
                      color: "#A0ADB9",
                      fontSize: 12,
                      whiteSpace: "nowrap",
                    }}
                  >
                    ⌨️ {T.keyPressCountLabel || "Key press count"}:{" "}
                    {empLightbox.keyPresses}
                  </span>
                )}
                {empLightbox.mouseClicks != null && (
                  <span
                    style={{
                      color: "#A0ADB9",
                      fontSize: 12,
                      whiteSpace: "nowrap",
                    }}
                  >
                    🖱️ {T.mouseClickCountLabel || "Mouse click count"}:{" "}
                    {empLightbox.mouseClicks}
                  </span>
                )}
                <button
                  onClick={() => setEmpLightbox(null)}
                  style={{
                    width: 28,
                    height: 28,
                    borderRadius: "50%",
                    border: "none",
                    background: "rgba(255,255,255,0.12)",
                    color: "#fff",
                    fontSize: 16,
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    lineHeight: 1,
                    flexShrink: 0,
                  }}
                >
                  ×
                </button>
              </div>
            </div>
            {lightboxImageFailed ? (
              <div
                style={{
                  width: "min(720px, 92vw)",
                  height: "min(480px, 60vh)",
                  background: "#0A0D16",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 14,
                }}
              >
                <Ico n="camera" s={64} c="#3A4658" />
                <p
                  style={{
                    color: "#A0ADB9",
                    fontSize: 14,
                    fontWeight: 600,
                    margin: 0,
                  }}
                >
                  {T.imageNotAvailable || "Image not available"}
                </p>
                <p
                  style={{
                    color: "#5A6472",
                    fontSize: 12,
                    margin: 0,
                    textAlign: "center",
                    maxWidth: 360,
                    lineHeight: 1.5,
                  }}
                >
                  Activity was recorded but the screen capture could not be
                  saved for this moment.
                </p>
              </div>
            ) : (
              <AuthImage
                url={empLightbox.url}
                alt={T.screenshotImageAlt || "screenshot"}
                eager
                decoding="async"
                style={{
                  display: "block",
                  maxWidth: "92vw",
                  maxHeight: "80vh",
                  objectFit: "contain",
                }}
                onError={() => {
                  setLightboxImageFailed(true);
                }}
              />
            )}
          </div>
        </div>
      )}

      {!loading && (
        <div
          style={{
            display: "flex",
            height: "100vh",
            overflow: "hidden",
            animation: "fadeIn 0.2s",
          }}
        >
          {/* ══ SIDEBAR ══ */}
          <aside
            style={{
              width: 220,
              flexShrink: 0,
              background: C.surface,
              borderRight: `1px solid ${C.border}`,
              display: "flex",
              flexDirection: "column",
              height: "100vh",
            }}
          >
            <div
              style={{
                padding: "18px 20px 14px",
                borderBottom: `1px solid ${C.borderLight}`,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: 4,
              }}
            >
              <img
                src={workspaceLogo || (isDark ? TemverisLogoWhite : TemverisLogo)}
                alt="Workspace"
                onClick={() => setTab("today")}
                style={{
                  width: "100%",
                  maxWidth: 180,
                  height: "auto",
                  maxHeight: 28,
                  objectFit: "contain",
                  display: "block",
                  cursor: "pointer",
                }}
              />
            </div>

            {/* Profile section */}
            <div
              style={{
                padding: "16px 16px 12px",
                borderBottom: `1px solid ${C.borderLight}`,
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div
                  style={{
                    width: 40,
                    height: 40,
                    borderRadius: 12,
                    background: "linear-gradient(135deg,#11496A,#44B97B)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: "#fff",
                    fontWeight: 700,
                    fontSize: 14,
                    flexShrink: 0,
                    boxShadow: "0 2px 8px rgba(17,73,106,0.25)",
                  }}
                >
                  {init}
                </div>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div
                    style={{ display: "flex", alignItems: "center", gap: 6 }}
                  >
                    <p
                      style={{
                        fontSize: 13,
                        fontWeight: 700,
                        color: C.text,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {name}
                    </p>
                    {["admin", "hr"].includes(localStorage.getItem("role")) && (
                      <span
                        style={{
                          fontSize: 8,
                          fontWeight: 800,
                          color: "#44B97B",
                          background: "#EDE9FE",
                          padding: "2px 6px",
                          borderRadius: 4,
                          textTransform: "uppercase",
                          letterSpacing: "0.5px",
                          flexShrink: 0,
                        }}
                      >
                        {localStorage.getItem("role")}
                      </span>
                    )}
                  </div>
                  <p
                    style={{
                      fontSize: 10,
                      color: C.textSub,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {email}
                  </p>
                </div>
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginTop: 10,
                  padding: "6px 10px",
                  borderRadius: 6,
                  background: onBreak
                    ? isDark
                      ? "rgba(251,191,36,0.1)"
                      : "#FFFBEB"
                    : clockedIn
                      ? isDark
                        ? "rgba(34,197,94,0.1)"
                        : "#F0FDF4"
                      : isDark
                        ? "#1F2937"
                        : "#F9FAFB",
                }}
              >
                <div
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    background: onBreak
                      ? "#F59E0B"
                      : clockedIn
                        ? "#22C55E"
                        : "#D1D5DB",
                    animation: clockedIn ? "pulse 2s infinite" : "none",
                  }}
                />
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: onBreak
                      ? "#B45309"
                      : clockedIn
                        ? "#2F8F5C"
                        : C.textLight,
                  }}
                >
                  {onBreak
                    ? T.on_break || "On Break"
                    : clockedIn
                      ? T.working || "Working"
                      : T.not_clocked_in || "Offline"}
                </span>
              </div>
            </div>

            <nav
              style={{
                flex: 1,
                overflowY: "auto",
                padding: "10px 12px",
                display: "flex",
                flexDirection: "column",
              }}
            >
              {SECTIONS.map(({ key, label, items }) => {
                if (!items.length) return null;
                return (
                  <div
                    key={key}
                    style={{
                      marginBottom: 6,
                      ...(key === "ACCOUNT"
                        ? { marginTop: "auto", paddingTop: 8 }
                        : {}),
                    }}
                  >
                    <p
                      style={{
                        fontSize: 10,
                        fontWeight: 700,
                        color: C.textLight,
                        padding: "10px 8px 5px",
                        textTransform: "uppercase",
                        letterSpacing: "0.8px",
                        display: "flex",
                        alignItems: "center",
                        gap: 5,
                      }}
                    >
                      {label}
                    </p>
                    {items.map((item) => {
                      // Group with sub-items ──
                      if (item.group) {
                        const childActive = item.children?.some(
                          (c) => c.id === tab,
                        );
                        const isOpen = openGroups[item.id] ?? childActive;
                        return (
                          <div key={item.id}>
                            <button
                              className="zte-nav nav-item"
                              data-active={childActive ? "true" : "false"}
                              onClick={(e) => {
                                spawnRipple(e);
                                setOpenGroups((p) => ({
                                  ...p,
                                  [item.id]: !(p[item.id] ?? childActive),
                                }));
                                // My Pay group defaults to Estimate tab.
                                if (item.id === "my-pay") setTab("earnings");
                              }}
                              style={{
                                position: "relative",
                                overflow: "hidden",
                                width: "100%",
                                display: "flex",
                                alignItems: "center",
                                gap: 10,
                                padding: "9px 10px",
                                borderRadius: 8,
                                border: "none",
                                background: childActive
                                  ? C.brandBg
                                  : "transparent",
                                color: childActive ? C.brand : C.textSub,
                                fontWeight: childActive ? 600 : 400,
                                fontSize: 13,
                                cursor: "pointer",
                                fontFamily: "inherit",
                                textAlign: "left",
                                marginBottom: 2,
                                transition:
                                  "background .18s, color .18s, transform .18s cubic-bezier(.34,1.56,.64,1)",
                              }}
                            >
                              <div
                                className="zte-nav-ic"
                                style={{
                                  width: 28,
                                  height: 28,
                                  borderRadius: 7,
                                  background: childActive
                                    ? `${C.brand}15`
                                    : "transparent",
                                  display: "flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  transition:
                                    "transform .25s cubic-bezier(.34,1.56,.64,1)",
                                }}
                              >
                                <Ico
                                  n={item.icon}
                                  s={15}
                                  c={childActive ? C.brand : C.textSub}
                                />
                              </div>
                              <span style={{ flex: 1 }}>{item.label}</span>
                              {item.children?.length > 0 && (
                                <span
                                  style={{
                                    display: "inline-flex",
                                    transition: "transform .22s ease",
                                    transform: isOpen
                                      ? "rotate(90deg)"
                                      : "rotate(0deg)",
                                  }}
                                >
                                  <Ico
                                    n="chevron_right2"
                                    s={12}
                                    c={childActive ? C.brand : C.textLight}
                                  />
                                </span>
                              )}
                            </button>
                            <div
                              className="zte-collapse"
                              data-open={
                                isOpen && item.children?.length > 0
                                  ? "true"
                                  : "false"
                              }
                              aria-hidden={!isOpen || !item.children?.length}
                            >
                              <div className="zte-collapse-inner">
                                <div style={{ paddingLeft: 18 }}>
                                  {item.children.map((c) => {
                                    const ca = tab === c.id;
                                    return (
                                      <button
                                        key={c.id}
                                        className="zte-nav zte-nav-child nav-item"
                                        data-active={ca ? "true" : "false"}
                                        onClick={(e) => {
                                          spawnRipple(e);
                                          setTab(c.id);
                                        }}
                                        style={{
                                          position: "relative",
                                          overflow: "hidden",
                                          width: "100%",
                                          display: "flex",
                                          alignItems: "center",
                                          gap: 8,
                                          padding: "7px 10px",
                                          borderRadius: 8,
                                          border: "none",
                                          background: ca
                                            ? C.brandBg
                                            : "transparent",
                                          color: ca ? C.brand : C.textSub,
                                          fontWeight: ca ? 600 : 400,
                                          fontSize: 12.5,
                                          cursor: "pointer",
                                          fontFamily: "inherit",
                                          marginBottom: 1,
                                          textAlign: "left",
                                          transition:
                                            "background .18s, color .18s, transform .18s cubic-bezier(.34,1.56,.64,1)",
                                        }}
                                      >
                                        <span
                                          className="zte-nav-dot"
                                          style={{
                                            width: 4,
                                            height: 4,
                                            borderRadius: "50%",
                                            background: ca
                                              ? C.brand
                                              : C.textLight,
                                            flexShrink: 0,
                                            transition:
                                              "transform .2s, background .15s ease",
                                            transform: ca
                                              ? "scale(1.6)"
                                              : "scale(1)",
                                          }}
                                        />
                                        <span
                                          className="zte-nav-ic"
                                          style={{
                                            display: "inline-flex",
                                            transition:
                                              "transform .25s cubic-bezier(.34,1.56,.64,1)",
                                          }}
                                        >
                                          <Ico
                                            n={c.icon}
                                            s={13}
                                            c={ca ? C.brand : C.textSub}
                                          />
                                        </span>
                                        <span style={{ flex: 1 }}>
                                          {c.label}
                                        </span>
                                      </button>
                                    );
                                  })}
                                </div>
                              </div>
                            </div>
                          </div>
                        );
                      }
                      // Leaf item ──
                      const active = tab === item.id;
                      return (
                        <button
                          key={item.id}
                          onClick={(e) => {
                            spawnRipple(e);
                            setTab(item.id);
                          }}
                          className="zte-nav nav-item"
                          data-active={active ? "true" : "false"}
                          style={{
                            position: "relative",
                            overflow: "hidden",
                            width: "100%",
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            padding: "9px 10px",
                            borderRadius: 8,
                            border: "none",
                            background: active ? C.brandBg : "transparent",
                            color: active ? C.brand : C.textSub,
                            fontWeight: active ? 600 : 400,
                            fontSize: 13,
                            cursor: "pointer",
                            fontFamily: "inherit",
                            textAlign: "left",
                            marginBottom: 2,
                            transition:
                              "background .18s, color .18s, transform .18s cubic-bezier(.34,1.56,.64,1)",
                          }}
                        >
                          <div
                            className="zte-nav-ic"
                            style={{
                              width: 28,
                              height: 28,
                              borderRadius: 7,
                              background: active
                                ? `${C.brand}15`
                                : "transparent",
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "center",
                              transition:
                                "transform .25s cubic-bezier(.34,1.56,.64,1)",
                            }}
                          >
                            <Ico
                              n={item.icon}
                              s={15}
                              c={active ? C.brand : C.textSub}
                            />
                          </div>
                          <span style={{ flex: 1 }}>{item.label}</span>
                        </button>
                      );
                    })}
                  </div>
                );
              })}
            </nav>

            <div
              style={{
                padding: "12px 14px",
                borderTop: `1px solid ${C.borderLight}`,
              }}
            >
              <button
                onClick={() => {
                  logout({ redirect: false });
                  navigate("/", { replace: true });
                }}
                style={{
                  width: "100%",
                  padding: "9px 12px",
                  borderRadius: 8,
                  border: `1px solid ${isDark ? "#7F1D1D" : "#FEE2E2"}`,
                  background: isDark ? "rgba(239,68,68,0.08)" : "#FEF2F2",
                  color: "#DC2626",
                  fontWeight: 600,
                  fontSize: 12,
                  cursor: "pointer",
                  fontFamily: "inherit",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 6,
                  transition: "all .15s",
                }}
              >
                <Ico n="logout" s={14} c="#DC2626" />
                {T.sign_out || "Sign Out"}
              </button>
            </div>
          </aside>

          {/* ══ MAIN ══ */}
          <div
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              minWidth: 0,
              background: C.bg,
            }}
          >
            <header
              style={{
                height: 60,
                background: C.surface,
                borderBottom: `1px solid ${C.border}`,
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "0 28px",
                flexShrink: 0,
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <p style={{ fontWeight: 700, fontSize: 18, color: C.text }}>
                  {NAV.find((n) => n.id === tab)?.label}
                </p>
                {clockedIn && (
                  <span
                    style={{
                      fontSize: 10,
                      fontWeight: 700,
                      color: "#22C55E",
                      background: "#F0FDF4",
                      border: "1px solid #BBF7D0",
                      padding: "3px 10px",
                      borderRadius: 20,
                      display: "flex",
                      alignItems: "center",
                      gap: 4,
                    }}
                  >
                    <span
                      style={{
                        width: 6,
                        height: 6,
                        borderRadius: "50%",
                        background: "#22C55E",
                        animation: "pulse 2s infinite",
                      }}
                    />
                    {T.liveIndicator || "Live"}
                  </span>
                )}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                {/* First in the action cluster, sized "sm" so it matches the
                    Admin Panel button beside it. Renders nothing inside the
                    desktop shell — see DownloadAppButton. */}
                <DownloadAppButton
                  size="sm"
                  label={T.downloadApp || "Download App"}
                  title={T.downloadDesktopApp || "Download the desktop app"}
                  gapColor={C.surface}
                />
                {["admin", "hr"].includes(localStorage.getItem("role")) && (
                  <button
                    onClick={() => {
                      if (IS_DESKTOP_APP) {
                        openOnWebsite("/admin");
                        return;
                      }
                      navigate("/admin");
                    }}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                      padding: "7px 14px",
                      borderRadius: 8,
                      border: `1.5px solid ${C.brandBdr}`,
                      background: C.brandBg,
                      color: C.brand,
                      fontWeight: 700,
                      fontSize: 12,
                      cursor: "pointer",
                      fontFamily: "inherit",
                      transition: "all .15s",
                      whiteSpace: "nowrap",
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.background = C.brand;
                      e.currentTarget.style.color = "#fff";
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.background = C.brandBg;
                      e.currentTarget.style.color = C.brand;
                    }}
                  >
                    <Ico n="shield" s={14} c="currentColor" fw="2.5" />
                    {T.adminPanel || "Admin Panel"}
                  </button>
                )}
                <LangSwitcherComponent isDark={isDark} />
                <button
                  onClick={toggleDark}
                  style={{
                    width: 34,
                    height: 34,
                    borderRadius: 8,
                    border: `1px solid ${C.border}`,
                    background: C.surface,
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    transition: "all .15s",
                  }}
                >
                  <Ico n={isDark ? "sun" : "moon"} s={15} c={C.textSub} />
                </button>
                {/* ── Profile avatar + dropdown ── */}
                <div ref={profileMenuRef} style={{ position: "relative" }}>
                  <button
                    onClick={() => setProfileMenuOpen((o) => !o)}
                    title={name || "Profile"}
                    style={{
                      width: 36,
                      height: 36,
                      borderRadius: 10,
                      background: "linear-gradient(135deg,#11496A,#44B97B)",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      color: "#fff",
                      fontWeight: 700,
                      fontSize: 14,
                      boxShadow: "0 2px 8px rgba(17,73,106,0.25)",
                      cursor: "pointer",
                      border: profileMenuOpen
                        ? "2px solid #44B97B"
                        : "2px solid transparent",
                      outline: "none",
                      transition: "border-color .15s",
                      padding: 0,
                    }}
                  >
                    {init}
                  </button>

                  {profileMenuOpen && (
                    <div
                      style={{
                        position: "absolute",
                        top: "calc(100% + 8px)",
                        right: 0,
                        width: 220,
                        borderRadius: 12,
                        background: C.surface,
                        border: `1px solid ${C.border}`,
                        boxShadow: "0 8px 32px rgba(0,0,0,0.18)",
                        zIndex: 9999,
                        overflow: "hidden",
                        animation: "fadeIn .15s ease",
                      }}
                    >
                      {/* Header */}
                      <div
                        style={{
                          padding: "14px 16px 10px",
                          borderBottom: `1px solid ${C.border}`,
                        }}
                      >
                        <div
                          style={{
                            width: 40,
                            height: 40,
                            borderRadius: 10,
                            background:
                              "linear-gradient(135deg,#11496A,#44B97B)",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            color: "#fff",
                            fontWeight: 700,
                            fontSize: 15,
                            marginBottom: 8,
                          }}
                        >
                          {init}
                        </div>
                        <div
                          style={{
                            fontWeight: 700,
                            fontSize: 13.5,
                            color: C.text,
                            lineHeight: 1.2,
                          }}
                        >
                          {name || "—"}
                        </div>
                        <div
                          style={{
                            fontSize: 11.5,
                            color: C.textSub,
                            marginTop: 2,
                          }}
                        >
                          {localStorage.getItem("email") || ""}
                        </div>
                      </div>

                      {/* Menu items */}
                      {[
                        {
                          icon: "user",
                          label: T.profile || "My Profile",
                          action: () => {
                            setTab("profile");
                            setProfileMenuOpen(false);
                          },
                        },
                        {
                          icon: "bell",
                          label: T.notifications || "Notifications",
                          action: () => {
                            setTab("notifications");
                            setProfileMenuOpen(false);
                          },
                        },
                        {
                          icon: "settings",
                          label: T.settings || "Settings",
                          action: () => {
                            setTab("settings");
                            setProfileMenuOpen(false);
                          },
                        },
                      ].map(({ icon, label, action }) => (
                        <button
                          key={label}
                          onClick={action}
                          style={{
                            width: "100%",
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            padding: "11px 16px",
                            background: "none",
                            border: "none",
                            cursor: "pointer",
                            color: C.text,
                            fontSize: 13.5,
                            fontFamily: "inherit",
                            textAlign: "left",
                            transition: "background .12s",
                          }}
                          onMouseEnter={(e) =>
                            (e.currentTarget.style.background = isDark
                              ? "rgba(255,255,255,0.06)"
                              : "#F9FAFB")
                          }
                          onMouseLeave={(e) =>
                            (e.currentTarget.style.background = "none")
                          }
                        >
                          <Ico n={icon} s={15} c={C.textSub} />
                          {label}
                        </button>
                      ))}

                      {/* Divider + Sign out */}
                      <div style={{ borderTop: `1px solid ${C.border}` }} />
                      <button
                        onClick={() => {
                          setProfileMenuOpen(false);
                          logout({ redirect: false });
                          navigate("/", { replace: true });
                        }}
                        style={{
                          width: "100%",
                          display: "flex",
                          alignItems: "center",
                          gap: 10,
                          padding: "11px 16px",
                          background: "none",
                          border: "none",
                          cursor: "pointer",
                          color: "#EF4444",
                          fontSize: 13.5,
                          fontFamily: "inherit",
                          textAlign: "left",
                          transition: "background .12s",
                        }}
                        onMouseEnter={(e) =>
                          (e.currentTarget.style.background = isDark
                            ? "rgba(239,68,68,0.1)"
                            : "#FEF2F2")
                        }
                        onMouseLeave={(e) =>
                          (e.currentTarget.style.background = "none")
                        }
                      >
                        <Ico n="logout" s={15} c="#EF4444" />
                        {T.signOut || "Sign Out"}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            </header>

            <main style={{ flex: 1, overflowY: "auto", padding: "24px 28px" }}>
              {/* ── Deep Work Alert ── */}
              {showDeepWorkAlert && (
                <div
                  style={{
                    background: isDark ? "rgba(245,158,11,0.12)" : "#FFFBEB",
                    border: `1px solid ${isDark ? "#92400E" : "#FCD34D"}`,
                    borderRadius: 12,
                    padding: "16px 20px",
                    marginBottom: 20,
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 14,
                    animation: "fadeIn 0.3s",
                  }}
                >
                  <div
                    style={{
                      width: 40,
                      height: 40,
                      borderRadius: 10,
                      background: isDark ? "rgba(245,158,11,0.2)" : "#FEF3C7",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                    }}
                  >
                    <span style={{ fontSize: 20 }}>&#9888;</span>
                  </div>
                  <div style={{ flex: 1 }}>
                    <p
                      style={{
                        fontWeight: 700,
                        fontSize: 14,
                        color: isDark ? "#FCD34D" : "#92400E",
                        marginBottom: 4,
                      }}
                    >
                      {T.deepWorkAlertTitle || "Deep Work Alert"}
                    </p>
                    <p
                      style={{
                        fontSize: 13,
                        color: isDark ? "#D4A017" : "#78350F",
                        lineHeight: 1.5,
                        marginBottom: 8,
                      }}
                    >
                      {T.deepWorkAlertMsg ||
                        "You've been working continuously for over 4 hours. Extended sessions can reduce overall performance."}
                    </p>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 16,
                        fontSize: 12,
                        color: isDark ? "#D4A017" : "#92400E",
                        fontWeight: 500,
                      }}
                    >
                      <span>{T.hydrate || "Hydrate"}</span>
                      <span>{T.moveAround || "Move around"}</span>
                      <span>{T.restYourEyes || "Rest your eyes"}</span>
                    </div>
                  </div>
                  <div
                    style={{
                      display: "flex",
                      gap: 8,
                      flexShrink: 0,
                      alignItems: "center",
                    }}
                  >
                    {/* Deep-work alert's shortcut into the break picker. Hidden
                        with every other attendance action on the website; the
                        alert itself (and Dismiss) still shows. */}
                    {attendanceActionsAllowed && (
                    <button
                      onClick={() => {
                        setShowBreakPicker(true);
                        setShowDeepWorkAlert(false);
                        deepWorkDismissedRef.current = true;
                      }}
                      style={{
                        padding: "8px 16px",
                        borderRadius: 8,
                        border: "none",
                        background: isDark ? "#B45309" : "#F59E0B",
                        color: "#fff",
                        fontWeight: 700,
                        fontSize: 12,
                        cursor: "pointer",
                        fontFamily: "inherit",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {T.take_break || "Take a Break"}
                    </button>
                    )}
                    <button
                      onClick={() => {
                        setShowDeepWorkAlert(false);
                        deepWorkDismissedRef.current = true;
                      }}
                      style={{
                        padding: "8px 12px",
                        borderRadius: 8,
                        border: `1px solid ${isDark ? "#92400E" : "#FCD34D"}`,
                        background: "transparent",
                        color: isDark ? "#FCD34D" : "#92400E",
                        fontWeight: 600,
                        fontSize: 12,
                        cursor: "pointer",
                        fontFamily: "inherit",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {T.dismiss || "Dismiss"}
                    </button>
                  </div>
                </div>
              )}

              {/* ─── MY WORK ─── */}
              {tab === "my-work" && (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 20,
                    maxWidth: 1200,
                    animation: "fadeIn 0.2s",
                  }}
                >
                  {/* ── Desktop-only workers: no time UI on the website ──────
                      For an organization covered by the desktop-only clock-in
                      policy, employees get NO live timer, NO Take a Break, NO
                      Clock Out and NO activity tiles here — all of it lives in
                      the desktop app. Explicit product decision (2026-08-03).

                      No explanatory banner either (removed 2026-08-05): workers
                      read "Please clock in using the Temveris Desktop App" on
                      the website as "Temveris is broken", which cost more in
                      support than the hint was worth. The desktop app is the
                      only place they act, so the website simply stays quiet.
                      DESKTOP_APP_CLOCK_IN_MSG is still used by handleClockIn's
                      403 handler for the stale-tab case.

                      NOTE: this hides the UI only; POST /employees/clock-out is
                      deliberately still open, so a stranded session can always
                      be closed from the desktop app or by an admin via
                      /employees/{id}/admin-clock. Nobody is permanently stuck.

                      Admins/HR are never covered by the policy, so their own
                      timer on this page is untouched. */}

                  {/* ── Live Work Timer ── */}
                  {/* Live Work Timer card. Hidden entirely on the website: it is
                      the attendance ACTION surface (running timer + Take a
                      Break / Resume Work / Clock Out), all of which live in the
                      desktop app. The read-only numbers below — Total Worked
                      Hours, Active Time, Idle %, Productive Apps %, the
                      CLOCK-IN / CLOCK-OUT / BREAK tiles, Activities and Today's
                      Summary — are unaffected and still render. */}
                  {clockedIn && attendanceActionsAllowed && (
                    <div
                      style={{
                        ...card(),
                        padding: 0,
                        overflow: "hidden",
                        background: `linear-gradient(135deg, ${isDark ? "#1A1A1A" : "#FAFBFF"}, ${isDark ? "#0F0F0F" : "#F0F4FF"})`,
                        border: `1px solid ${isDark ? "#2D3748" : "#E0E7FF"}`,
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          padding: "28px 32px",
                          flexWrap: "wrap",
                          gap: 20,
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 20,
                          }}
                        >
                          <div
                            style={{
                              width: 56,
                              height: 56,
                              borderRadius: 16,
                              background:
                                "linear-gradient(135deg,#11496A,#44B97B)",
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "center",
                              boxShadow: "0 4px 16px rgba(17,73,106,0.3)",
                            }}
                          >
                            <Ico n="clock" s={26} c="#fff" />
                          </div>
                          <div>
                            <p
                              style={{
                                fontSize: 13,
                                fontWeight: 600,
                                color: C.textSub,
                                marginBottom: 4,
                                letterSpacing: "0.5px",
                                textTransform: "uppercase",
                              }}
                            >
                              {T.liveWorkTimer || "Live Work Timer"}
                            </p>
                            <p
                              style={{
                                fontSize: 42,
                                fontWeight: 800,
                                color: C.text,
                                fontVariantNumeric: "tabular-nums",
                                letterSpacing: 2,
                                lineHeight: 1,
                              }}
                            >
                              <LiveWorkedClock
                                mode="worked"
                                clockInMsRef={clockInMsRef}
                                totalBreakMsRef={totalBreakMsRef}
                                breakStartMsRef={breakStartMsRef}
                                prevWorkedSecsRef={prevWorkedSecsRef}
                              />
                            </p>
                            <p
                              style={{
                                fontSize: 12,
                                color: C.textSub,
                                marginTop: 6,
                              }}
                            >
                              {T.startedAt || "Started at"}{" "}
                              <strong style={{ color: C.text }}>
                                {clockInStr}
                              </strong>{" "}
                              IST
                            </p>
                          </div>
                        </div>
                        {/* Take a Break / Resume Work + Clock Out. No separate
                            check needed: the whole card is already hidden on the
                            website by the gate above, so this would be dead
                            code. Kept intact for when the flag is lifted. */}
                        <div style={{ display: "flex", gap: 10 }}>
                          <button
                            onClick={() => {
                              if (onBreak) handleBreakToggle();
                              else setShowBreakPicker(true);
                            }}
                            style={{
                              padding: "12px 24px",
                              borderRadius: 10,
                              border: "none",
                              background: onBreak ? "#11496A" : "#FEF3C7",
                              color: onBreak ? "#fff" : "#92400E",
                              fontWeight: 700,
                              fontSize: 14,
                              cursor: "pointer",
                              fontFamily: "inherit",
                              display: "flex",
                              alignItems: "center",
                              gap: 8,
                              transition: "all .15s",
                              boxShadow: onBreak
                                ? "0 2px 10px rgba(17,73,106,0.3)"
                                : "0 1px 4px rgba(0,0,0,0.06)",
                            }}
                          >
                            <Ico
                              n="clock"
                              s={16}
                              c={onBreak ? "#fff" : "#92400E"}
                            />
                            {onBreak
                              ? T.resume || "Resume Work"
                              : T.take_break || "Take a Break"}
                          </button>
                          <button
                            onClick={handleClockOut}
                            disabled={clockOutBusy}
                            style={{
                              padding: "12px 24px",
                              borderRadius: 10,
                              border: "none",
                              background: "#FEF2F2",
                              color: "#DC2626",
                              fontWeight: 700,
                              fontSize: 14,
                              cursor: clockOutBusy ? "not-allowed" : "pointer",
                              opacity: clockOutBusy ? 0.6 : 1,
                              fontFamily: "inherit",
                              display: "flex",
                              alignItems: "center",
                              gap: 8,
                              transition: "all .15s",
                              boxShadow: "0 1px 4px rgba(0,0,0,0.06)",
                            }}
                          >
                            <Ico n="logout" s={16} c="#DC2626" />
                            {clockOutBusy
                              ? (T.clockingOut || "Clocking out…")
                              : (T.clock_out || "Clock Out")}
                          </button>
                        </div>
                      </div>
                      {onBreak && (
                        <div
                          style={{
                            background: isDark
                              ? "rgba(251,191,36,0.1)"
                              : "#FFFBEB",
                            borderTop: `1px solid ${isDark ? "#78350F" : "#FDE68A"}`,
                            padding: "10px 32px",
                            display: "flex",
                            alignItems: "center",
                            gap: 8,
                          }}
                        >
                          <div
                            style={{
                              width: 8,
                              height: 8,
                              borderRadius: "50%",
                              background: "#F59E0B",
                              animation: "pulse 1.5s infinite",
                            }}
                          />
                          <span
                            style={{
                              fontSize: 12,
                              fontWeight: 600,
                              color: "#B45309",
                            }}
                          >
                            {T.on_break || "On Break"} —{" "}
                            <LiveWorkedClock
                              mode="break"
                              clockInMsRef={clockInMsRef}
                              totalBreakMsRef={totalBreakMsRef}
                              breakStartMsRef={breakStartMsRef}
                              prevWorkedSecsRef={prevWorkedSecsRef}
                            />
                          </span>
                        </div>
                      )}
                    </div>
                  )}

                  {/* ── Not clocked in banner — shown both before first clock-in and after clock-out ──
                      Hidden entirely on the website: the banner exists only to
                      offer Clock In, which is a desktop-app-only action, so a
                      headline prompting an action the page cannot perform would
                      be worse than showing nothing. `attendanceActionsAllowed`
                      already subsumes the desktop-only-employee policy. */}
                  {!clockedIn && attendanceActionsAllowed && (
                    <div
                      style={{
                        ...card(),
                        padding: "28px 32px",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        background: `linear-gradient(135deg, ${isDark ? "#0F1F14" : "#F0FDF4"}, ${isDark ? "#0F0F0F" : "#E7F0F6"})`,
                        border: `1px solid ${isDark ? "#166534" : "#BBF7D0"}`,
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 16,
                        }}
                      >
                        <div
                          style={{
                            width: 48,
                            height: 48,
                            borderRadius: 14,
                            background:
                              "linear-gradient(135deg,#22C55E,#16A34A)",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            boxShadow: "0 4px 12px rgba(34,197,94,0.3)",
                          }}
                        >
                          <Ico n="play" s={22} c="#fff" />
                        </div>
                        <div>
                          <p
                            style={{
                              fontSize: 16,
                              fontWeight: 700,
                              color: C.text,
                            }}
                          >
                            {clockOutTimeUTC
                              ? T.stoppedWorking || "You stopped working"
                              : T.not_clocked_in ||
                                "You are not clocked in yet"}
                          </p>
                          <p
                            style={{
                              fontSize: 12,
                              color: C.textSub,
                              marginTop: 2,
                            }}
                          >
                            {clockOutTimeUTC
                              ? (
                                  T.resumeTrackingMsg ||
                                  "Worked {time} today — clock in again to resume tracking"
                                ).replace("{time}", fmtHM(dailyWorkedSecs))
                              : T.clockInStartMsg ||
                                "Clock in to start tracking your work time"}
                          </p>
                          {/* ── Motivational message after clock-out ── */}
                          {clockOutTimeUTC &&
                            clockOutMsgIdx !== null &&
                            (() => {
                              const hrs = dailyWorkedSecs / 3600;
                              const firstName =
                                (name || "").split(" ")[0] || name || "there";
                              const great = [
                                `Great job today, ${firstName}! 🎉`,
                                `Awesome work, ${firstName}! 💪`,
                                `Your efforts really matter, ${firstName}! 🙌`,
                                `Fantastic consistency, ${firstName}! 🔥`,
                                `Well done, ${firstName}, keep it up! 🚀`,
                              ];
                              const good = [
                                `Good work, ${firstName}, keep pushing! 👍`,
                                `Almost there, ${firstName}, stay focused! 💡`,
                                `Nice effort today, ${firstName}! 😊`,
                                `Keep improving every day, ${firstName}! 📈`,
                                `You're doing well, ${firstName}, push a bit more! 💪`,
                              ];
                              const improve = [
                                `Let's aim better tomorrow, ${firstName} 🙂`,
                                `Stay focused and consistent, ${firstName} 📌`,
                                `You can do better, ${firstName}! 💡`,
                                `Keep trying, ${firstName}, you'll improve! 🚀`,
                                `Small steps lead to big results, ${firstName} 🌱`,
                              ];
                              // Rate against the employee's OWN required hours,
                              // resolved SERVER-side (GET /employees/me →
                              // expected_work_seconds): ≥100% great, ≥80% good,
                              // else improve. P9 removed the client-side 8h/4h
                              // table this used to compute, which disagreed with
                              // the clock-out gate once an org configured
                              // anything. Until the value arrives we show the
                              // neutral "good" copy rather than guessing a
                              // threshold — never a rating we cannot justify.
                              const _expHrs = Number(expectedWorkSecs) > 0
                                ? Number(expectedWorkSecs) / 3600 : null;
                              const _tier = _expHrs == null ? "unknown"
                                : hrs >= _expHrs ? "great"
                                : hrs >= _expHrs * 0.8 ? "good" : "improve";
                              const pool = _tier === "great" ? great
                                : _tier === "improve" ? improve : good;
                              const msg = pool[clockOutMsgIdx % pool.length];
                              const accent =
                                _tier === "great"   ? "#16A34A"
                                : _tier === "improve" ? "#D97706"
                                                      : "#2563EB";
                              return (
                                <p
                                  style={{
                                    fontSize: 13,
                                    fontWeight: 600,
                                    color: accent,
                                    marginTop: 8,
                                    letterSpacing: "0.1px",
                                  }}
                                >
                                  {msg}
                                </p>
                              );
                            })()}
                        </div>
                      </div>
                      {/* No desktop-policy branch here, and no attendance-action
                          branch either: when either rule applies this whole
                          banner is hidden by the gate on the block above, so a
                          second check would be dead code. */}
                      <button
                        onClick={handleClockIn}
                        style={{
                          padding: "12px 28px",
                          borderRadius: 10,
                          border: "none",
                          background: "linear-gradient(135deg,#22C55E,#16A34A)",
                          color: "#fff",
                          fontWeight: 700,
                          fontSize: 14,
                          cursor: "pointer",
                          fontFamily: "inherit",
                          boxShadow: "0 4px 16px rgba(34,197,94,0.35)",
                          transition: "all .15s",
                        }}
                      >
                        <span
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 8,
                          }}
                        >
                          <Ico n="play" s={15} c="#fff" fw="2.5" />
                          {clockOutTimeUTC
                            ? T.clockInAgain || "Clock In Again"
                            : T.clock_in || "Clock In"}
                        </span>
                      </button>
                    </div>
                  )}

                  {/* ── Stats Cards ── */}
                  {/* Hidden for desktop-only workers: Total Worked / Active
                      Time / Idle % / Productive Apps % are all work-time UI,
                      which belongs in the desktop app for these employees. */}
                  {!desktopClockInRequired && (
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(4, 1fr)",
                      gap: 16,
                    }}
                  >
                    {(() => {
                      // Values mirror the Activity Log page (/api/activity-log/summary):
                      //   Active Time = tick seconds minus per-tick idle seconds
                      //   Idle %      = idle_seconds / (active + idle)
                      //   Productive %= productive / (productive + neutral + distracting)
                      // Total Worked stays clock-derived (`dailyWorkedSecs`) because
                      // that's what the Live Work Timer is counting up to.
                      const { activeSecs, idleSecs, productivePct } =
                        todayActivity;
                      const trackedSecs = activeSecs + idleSecs;
                      const idlePct =
                        trackedSecs > 0
                          ? Math.round((idleSecs / trackedSecs) * 100)
                          : 0;
                      return [
                        {
                          label: T.total_worked || "Total Worked",
                          value: fmtHM(dailyWorkedSecs),
                          icon: "clock",
                          color: "#11496A",
                          bg: isDark ? "#0D2847" : "#E7F0F6",
                          bdr: "#B2DDFF",
                        },
                        {
                          label: T.active_time || "Active Time",
                          value: fmtHM(activeSecs),
                          icon: "check",
                          color: "#2F8F5C",
                          bg: isDark ? "#0D3D1F" : "#F0FDF4",
                          bdr: "#A9EFC5",
                        },
                        {
                          label: T.idle_time || "Idle Time",
                          value: `${idlePct}%`,
                          icon: "moon",
                          color: "#B54708",
                          bg: isDark ? "#3D2A0A" : "#FFFAEB",
                          bdr: "#FEDF89",
                        },
                        {
                          label: T.productive_apps || "Productive Apps",
                          value: `${productivePct}%`,
                          icon: "check",
                          color: "#44B97B",
                          bg: isDark ? "#1A1A1A" : "#F5F3FF",
                          bdr: "#C4B5FD",
                        },
                      ];
                    })().map((m) => (
                      <div
                        key={m.label}
                        style={{
                          ...card(),
                          padding: "20px 22px",
                          // Even-weighted colored border on all four sides
                          // (instead of a left-only accent) so each card
                          // reads as a unified, finished tile rather than a
                          // generic "AI"-looking left-stripe panel.
                          border: `1.5px solid ${m.bdr}`,
                          boxShadow: `0 1px 0 ${m.bdr}40 inset, 0 4px 12px -8px ${m.color}33`,
                          transition:
                            "transform .18s ease, box-shadow .18s ease, border-color .18s ease",
                        }}
                        className="card-hover"
                        onMouseEnter={(e) => {
                          e.currentTarget.style.borderColor = m.color;
                          e.currentTarget.style.boxShadow = `0 1px 0 ${m.bdr}40 inset, 0 10px 24px -8px ${m.color}55`;
                          e.currentTarget.style.transform = "translateY(-1px)";
                        }}
                        onMouseLeave={(e) => {
                          e.currentTarget.style.borderColor = m.bdr;
                          e.currentTarget.style.boxShadow = `0 1px 0 ${m.bdr}40 inset, 0 4px 12px -8px ${m.color}33`;
                          e.currentTarget.style.transform = "none";
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            marginBottom: 14,
                          }}
                        >
                          <div
                            style={{
                              width: 36,
                              height: 36,
                              borderRadius: 10,
                              background: m.bg,
                              border: `1px solid ${m.bdr}`,
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "center",
                            }}
                          >
                            <Ico n={m.icon} s={17} c={m.color} />
                          </div>
                          <span
                            style={{
                              fontSize: 12,
                              color: C.textSub,
                              fontWeight: 500,
                            }}
                          >
                            {m.label}
                          </span>
                        </div>
                        <p
                          style={{
                            fontSize: 28,
                            fontWeight: 800,
                            color: C.text,
                            fontVariantNumeric: "tabular-nums",
                            letterSpacing: -0.5,
                          }}
                        >
                          {m.value}
                        </p>
                      </div>
                    ))}
                  </div>
                  )}

                  {/* ── Clock In / Clock Out / Break Time ── */}
                  {/* Also hidden for desktop-only workers: these three tiles
                      restate today's clock-in time, clock-out time and break
                      total — the same work-time data as the timer above. */}
                  {!desktopClockInRequired && (
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr 1fr",
                      gap: 16,
                    }}
                  >
                    {[
                      {
                        label: T.clock_in_time || "Clock In",
                        value: clockInStr,
                        icon: "play",
                        color: "#22C55E",
                      },
                      {
                        label: T.clock_out_time || "Clock Out",
                        value: clockOutStr,
                        icon: "x",
                        color: "#EF4444",
                      },
                      {
                        label: T.breakLabel || "Break Time",
                        value: fmtHM(breakElapsed),
                        icon: "clock",
                        color: "#F59E0B",
                      },
                    ].map((m) => (
                      <div
                        key={m.label}
                        style={{
                          ...card(),
                          padding: "18px 22px",
                          display: "flex",
                          alignItems: "center",
                          gap: 14,
                        }}
                        className="card-hover"
                      >
                        <div
                          style={{
                            width: 40,
                            height: 40,
                            borderRadius: 12,
                            background: `${m.color}12`,
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            flexShrink: 0,
                          }}
                        >
                          <Ico n={m.icon} s={18} c={m.color} />
                        </div>
                        <div>
                          <p
                            style={{
                              fontSize: 11,
                              color: C.textSub,
                              fontWeight: 500,
                              marginBottom: 4,
                              textTransform: "uppercase",
                              letterSpacing: "0.5px",
                            }}
                          >
                            {m.label}
                          </p>
                          <p
                            style={{
                              fontSize: 22,
                              fontWeight: 700,
                              color: C.text,
                              fontVariantNumeric: "tabular-nums",
                            }}
                          >
                            {m.value}
                          </p>
                        </div>
                      </div>
                    ))}
                  </div>
                  )}

                  {/* ── Two Column: Activity Timeline + Top Apps ── */}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr",
                      gap: 16,
                    }}
                  >
                    {/* Activity Timeline */}
                    <div
                      style={{
                        ...card(),
                        padding: "22px 24px",
                        display: "flex",
                        flexDirection: "column",
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          marginBottom: 18,
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 8,
                          }}
                        >
                          <div
                            style={{
                              width: 32,
                              height: 32,
                              borderRadius: 8,
                              background: C.brandBg,
                              border: `1px solid ${C.brandBdr}`,
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "center",
                            }}
                          >
                            <Ico n="clock" s={15} c={C.brand} />
                          </div>
                          <p
                            style={{
                              fontSize: 14,
                              fontWeight: 700,
                              color: C.text,
                            }}
                          >
                            {T.activities || "Activity Timeline"}
                          </p>
                        </div>
                        <span
                          style={{
                            fontSize: 11,
                            color: C.textLight,
                            fontWeight: 500,
                          }}
                        >
                          {now.toLocaleDateString("en-IN", {
                            weekday: "short",
                            month: "short",
                            day: "numeric",
                            timeZone: getOrgTimeZone(),
                          })}
                        </span>
                      </div>
                      <div style={{ flex: 1, display: "flex", gap: 10 }}>
                        <div
                          style={{
                            display: "flex",
                            flexDirection: "column",
                            justifyContent: "space-between",
                            height: 140,
                            flexShrink: 0,
                            paddingBottom: 22,
                          }}
                        >
                          {["1h", "30m", "0"].map((l) => (
                            <span
                              key={l}
                              style={{
                                fontSize: 10,
                                color: C.textLight,
                                fontWeight: 500,
                              }}
                            >
                              {l}
                            </span>
                          ))}
                        </div>
                        <div
                          style={{
                            flex: 1,
                            height: 140,
                            display: "flex",
                            flexDirection: "column",
                          }}
                        >
                          <div
                            style={{
                              flex: 1,
                              position: "relative",
                              borderBottom: `1px solid ${C.borderLight}`,
                              borderLeft: `1px solid ${C.borderLight}`,
                            }}
                          >
                            {[0, 1].map((i) => (
                              <div
                                key={i}
                                style={{
                                  position: "absolute",
                                  top: `${(i + 1) * 33}%`,
                                  left: 0,
                                  right: 0,
                                  borderBottom: `1px dashed ${C.borderLight}`,
                                }}
                              />
                            ))}
                            <div
                              style={{
                                position: "absolute",
                                bottom: 0,
                                left: 0,
                                right: 0,
                                display: "flex",
                                alignItems: "flex-end",
                                gap: 3,
                                height: "100%",
                                padding: "0 4px",
                              }}
                            >
                              {actHours.map((h, i) => (
                                <div
                                  key={i}
                                  style={{
                                    flex: 1,
                                    height:
                                      h > 0
                                        ? `${Math.max(8, (h / maxAct) * 100)}%`
                                        : "3px",
                                    background:
                                      h > 0
                                        ? `linear-gradient(180deg,${C.brand},#44B97B)`
                                        : isDark
                                          ? "#374151"
                                          : "#E5E7EB",
                                    borderRadius: "3px 3px 0 0",
                                    transition: "height 0.5s ease",
                                    opacity: h > 0 ? 0.85 : 0.4,
                                  }}
                                />
                              ))}
                            </div>
                          </div>
                          <div
                            style={{
                              display: "flex",
                              justifyContent: "space-between",
                              paddingTop: 6,
                            }}
                          >
                            {/* 24 bars span the full day (00..23). Five labels
                                at quarter-points (0, 6, 12, 18, 24) align with
                                the bar grid via `space-between`. */}
                            {["12AM", "6AM", "12PM", "6PM", "12AM"].map(
                              (l, i) => (
                                <span
                                  key={`${l}-${i}`}
                                  style={{
                                    fontSize: 9,
                                    color: C.textLight,
                                    fontWeight: 500,
                                  }}
                                >
                                  {l}
                                </span>
                              ),
                            )}
                          </div>
                        </div>
                      </div>
                    </div>

                    {/* Top Apps & Websites */}
                    <div
                      style={{
                        ...card(),
                        padding: "22px 24px",
                        display: "flex",
                        flexDirection: "column",
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                          marginBottom: 18,
                        }}
                      >
                        <div
                          style={{
                            width: 32,
                            height: 32,
                            borderRadius: 8,
                            background: isDark ? "#1A1A1A" : "#F5F3FF",
                            border: "1px solid #C4B5FD",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                          }}
                        >
                          <Ico n="globe" s={15} c="#44B97B" />
                        </div>
                        <p
                          style={{
                            fontSize: 14,
                            fontWeight: 700,
                            color: C.text,
                          }}
                        >
                          {T.top_apps || "Top Apps & Websites"}
                        </p>
                      </div>
                      <div
                        style={{
                          flex: 1,
                          display: "grid",
                          gridTemplateColumns: "1fr auto",
                          gap: 20,
                          alignItems: "start",
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            flexDirection: "column",
                            gap: 6,
                          }}
                        >
                          {apps.map((app) => (
                            <div
                              key={app.name}
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: 10,
                              }}
                            >
                              <div
                                style={{
                                  width: 28,
                                  height: 28,
                                  borderRadius: 7,
                                  background: app.bg,
                                  display: "flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  flexShrink: 0,
                                }}
                              >
                                <div
                                  style={{
                                    width: 10,
                                    height: 10,
                                    borderRadius: 3,
                                    background: app.color,
                                  }}
                                />
                              </div>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div
                                  style={{
                                    display: "flex",
                                    justifyContent: "space-between",
                                    marginBottom: 3,
                                  }}
                                >
                                  <span
                                    style={{
                                      fontSize: 12,
                                      fontWeight: 600,
                                      color: C.text,
                                      overflow: "hidden",
                                      textOverflow: "ellipsis",
                                      whiteSpace: "nowrap",
                                    }}
                                  >
                                    {app.name}
                                  </span>
                                  <span
                                    style={{
                                      fontSize: 11,
                                      fontWeight: 600,
                                      color: C.textSub,
                                      flexShrink: 0,
                                    }}
                                  >
                                    {app.pct}%
                                  </span>
                                </div>
                                <div
                                  style={{
                                    height: 4,
                                    borderRadius: 2,
                                    background: C.borderLight,
                                    overflow: "hidden",
                                  }}
                                >
                                  <div
                                    style={{
                                      height: "100%",
                                      width: `${app.pct}%`,
                                      borderRadius: 2,
                                      background: app.color,
                                      transition: "width 0.5s",
                                    }}
                                  />
                                </div>
                              </div>
                            </div>
                          ))}
                        </div>
                        <svg
                          viewBox="0 0 140 140"
                          width={120}
                          height={120}
                          style={{ flexShrink: 0, alignSelf: "center" }}
                        >
                          {apps.reduce((acc, app, i) => {
                            const prev = apps
                              .slice(0, i)
                              .reduce((s, a) => s + a.pct, 0);
                            const r = 56,
                              ri = 36,
                              cx = 70,
                              cy = 70;
                            const s1 = (prev / 100) * 2 * Math.PI - Math.PI / 2,
                              e1 =
                                ((prev + app.pct) / 100) * 2 * Math.PI -
                                Math.PI / 2;
                            const lg = app.pct > 50 ? 1 : 0;
                            const x1 = cx + r * Math.cos(s1),
                              y1 = cy + r * Math.sin(s1),
                              x2 = cx + r * Math.cos(e1),
                              y2 = cy + r * Math.sin(e1);
                            const xi1 = cx + ri * Math.cos(s1),
                              yi1 = cy + ri * Math.sin(s1),
                              xi2 = cx + ri * Math.cos(e1),
                              yi2 = cy + ri * Math.sin(e1);
                            acc.push(
                              <path
                                key={app.name}
                                d={`M${x1} ${y1} A${r} ${r} 0 ${lg} 1 ${x2} ${y2} L${xi2} ${yi2} A${ri} ${ri} 0 ${lg} 0 ${xi1} ${yi1}Z`}
                                fill={app.color}
                                opacity={0.8}
                              />,
                            );
                            return acc;
                          }, [])}
                          <circle cx="70" cy="70" r="28" fill={C.surface} />
                          <text
                            x="70"
                            y="66"
                            textAnchor="middle"
                            style={{
                              fontSize: 14,
                              fontWeight: 800,
                              fill: C.text,
                            }}
                          >
                            {apps.reduce((s, a) => s + a.pct, 0)}%
                          </text>
                          <text
                            x="70"
                            y="80"
                            textAnchor="middle"
                            style={{
                              fontSize: 8,
                              fontWeight: 500,
                              fill: C.textLight,
                            }}
                          >
                            {T.usageLabel || "Usage"}
                          </text>
                        </svg>
                      </div>
                    </div>

                    {/* Top Tools — desktop apps the user works in (Eclipse, VS
                        Code, Figma, Office…). Surfaced separately because the
                        websites/donut above are browser-dominated, so the user's
                        actual tools would otherwise never show. */}
                    {tools.length > 0 && (
                      <div style={{ marginTop: 18, paddingTop: 16, borderTop: `1px solid ${C.border}` }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                          <div style={{ width: 28, height: 28, borderRadius: 7, background: C.brandBg, border: `1px solid ${C.brandBdr}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
                            <Ico n="briefcase" s={14} c={C.brand} />
                          </div>
                          <p style={{ fontSize: 14, fontWeight: 700, color: C.text }}>
                            {T.topTools || "Top Tools"}
                          </p>
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                          {tools.map((t) => (
                            <div key={t.name}>
                              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
                                <span style={{ width: 8, height: 8, borderRadius: 3, background: t.color, flexShrink: 0 }} />
                                <span style={{ flex: 1, minWidth: 0, fontSize: 12, fontWeight: 600, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.name}</span>
                                <span style={{ fontSize: 11, fontWeight: 700, color: C.brand, flexShrink: 0 }}>{t.pct}%</span>
                              </div>
                              <div style={{ height: 4, borderRadius: 99, background: C.borderLight, overflow: "hidden" }}>
                                <div style={{ height: "100%", width: `${t.pct}%`, background: t.color, borderRadius: 99, transition: "width .4s" }} />
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>

                  {/* ── Today's Summary Card ── */}
                  <div
                    style={{
                      ...card(),
                      padding: "22px 24px",
                      background: `linear-gradient(135deg, ${isDark ? "#0F1F14" : "#F0FDF4"}, ${isDark ? "#0F0F0F" : "#F0F4FF"})`,
                      border: `1px solid ${isDark ? "#166534" : "#D1FAE5"}`,
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        marginBottom: 16,
                      }}
                    >
                      <div
                        style={{
                          width: 32,
                          height: 32,
                          borderRadius: 8,
                          background: "#DCFCE7",
                          border: "1px solid #A9EFC5",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                        }}
                      >
                        <Ico n="check" s={15} c="#2F8F5C" />
                      </div>
                      <p
                        style={{ fontSize: 14, fontWeight: 700, color: C.text }}
                      >
                        {T.todaysSummary || "Today's Summary"}
                      </p>
                      <span
                        style={{
                          marginLeft: "auto",
                          fontSize: 11,
                          fontWeight: 500,
                          color: C.textLight,
                        }}
                      >
                        {now.toLocaleDateString("en-IN", {
                          weekday: "long",
                          month: "long",
                          day: "numeric",
                          year: "numeric",
                          timeZone: getOrgTimeZone(),
                        })}
                      </span>
                    </div>
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "repeat(5,1fr)",
                        gap: 12,
                      }}
                    >
                      {[
                        {
                          label: T.workHoursSummary || "Work Hours",
                          value: fmtHM(dailyWorkedSecs),
                          color: "#11496A",
                        },
                        {
                          label: T.activeTimeSummary || "Active Time",
                          // Real active duration (from the existing /activity-log
                          // summary state) — NOT dailyWorkedSecs, which is Work Hours.
                          value: fmtHM(todayActivity.activeSecs),
                          color: "#2F8F5C",
                        },
                        {
                          label: T.breakTimeSummary || "Break Time",
                          value: fmtHM(breakElapsed),
                          color: "#F59E0B",
                        },
                        {
                          label: T.productiveSummary || "Productive",
                          // Real productivity % from the same summary state
                          // (safe 0% when there is no categorised activity).
                          value: `${todayActivity.productivePct || 0}%`,
                          color: "#44B97B",
                        },
                        {
                          label: T.status || "Status",
                          value: onBreak
                            ? T.onBreak || "On Break"
                            : clockedIn
                              ? T.working || "Working"
                              : T.offline || "Offline",
                          color: onBreak
                            ? "#F59E0B"
                            : clockedIn
                              ? "#22C55E"
                              : "#9CA3AF",
                        },
                      ].map((m) => (
                        <div
                          key={m.label}
                          style={{
                            textAlign: "center",
                            padding: "12px 8px",
                            background: C.surface,
                            borderRadius: 10,
                            border: `1px solid ${C.borderLight}`,
                          }}
                        >
                          <p
                            style={{
                              fontSize: 18,
                              fontWeight: 800,
                              color: m.color,
                              fontVariantNumeric: "tabular-nums",
                              marginBottom: 4,
                            }}
                          >
                            {m.value}
                          </p>
                          <p
                            style={{
                              fontSize: 10,
                              fontWeight: 600,
                              color: C.textSub,
                              textTransform: "uppercase",
                              letterSpacing: "0.4px",
                            }}
                          >
                            {m.label}
                          </p>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* ── Upcoming Public Holidays ── */}
                  {(() => {
                    const todayIso = new Date().toISOString().slice(0, 10);
                    const upcoming = (Array.isArray(holidays) ? holidays : [])
                      .filter((h) => (h.date || "") >= todayIso)
                      .sort((a, b) => (a.date || "").localeCompare(b.date || ""))
                      .slice(0, 5);
                    const chip = (type) => {
                      const t = (type || "Public").toLowerCase();
                      if (t.includes("company")) return { bg: "#EFF6FF", fg: "#1D4ED8", bd: "#BFDBFE" };
                      if (t.includes("optional")) return { bg: "#FEF3C7", fg: "#B45309", bd: "#FCD34D" };
                      return { bg: "#DCFCE7", fg: "#15803D", bd: "#A9EFC5" };
                    };
                    return (
                      <div style={{ ...card(), padding: "22px 24px" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
                          <div style={{ width: 32, height: 32, borderRadius: 8, background: isDark ? "#3D2A0A" : "#FEF3C7", display: "flex", alignItems: "center", justifyContent: "center" }}>
                            <Ico n="calendar" s={15} c="#F59E0B" />
                          </div>
                          <p style={{ fontSize: 14, fontWeight: 700, color: C.text }}>
                            {T.upcomingPublicHolidays || "Upcoming Public Holidays"}
                          </p>
                          <button
                            onClick={() => setTab("holidays")}
                            style={{ marginLeft: "auto", fontSize: 11, fontWeight: 600, color: C.brand, background: "none", border: "none", cursor: "pointer", fontFamily: "inherit" }}
                          >
                            {T.viewAll || "View All →"}
                          </button>
                        </div>
                        {upcoming.length === 0 ? (
                          <p style={{ fontSize: 13, color: C.textSub, textAlign: "center", padding: "16px 0" }}>
                            {T.noUpcomingHolidays || "No upcoming public holidays."}
                          </p>
                        ) : (
                          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                            {upcoming.map((h) => {
                              const d = new Date((h.date || "") + "T00:00:00");
                              const c = chip(h.holiday_type);
                              return (
                                <div key={h.id || h.date} style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 12px", background: C.surface, borderRadius: 10, border: `1px solid ${C.borderLight}` }}>
                                  <div style={{ width: 40, height: 40, borderRadius: 10, background: isDark ? "#3D2A0A" : "#FEF3C7", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                                    <span style={{ fontSize: 15, fontWeight: 900, color: "#F59E0B", lineHeight: 1 }}>{isNaN(d) ? "—" : d.getDate()}</span>
                                    <span style={{ fontSize: 8, fontWeight: 700, color: "#F59E0B", textTransform: "uppercase" }}>{isNaN(d) ? "" : d.toLocaleDateString(lang || undefined, { month: "short" })}</span>
                                  </div>
                                  <div style={{ flex: 1, minWidth: 0 }}>
                                    <p style={{ fontSize: 13, fontWeight: 700, color: C.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{h.name}</p>
                                    <p style={{ fontSize: 11, color: C.textSub }}>{isNaN(d) ? (h.day || "") : d.toLocaleDateString(lang || undefined, { weekday: "long" })}</p>
                                  </div>
                                  <span style={{ fontSize: 10, fontWeight: 700, color: c.fg, background: c.bg, border: `1px solid ${c.bd}`, borderRadius: 12, padding: "3px 10px", flexShrink: 0 }}>
                                    {h.holiday_type || "Public"}
                                  </span>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    );
                  })()}
                </div>
              )}

              {/* ─── MY SCHEDULE ─── */}
              {tab === "my-schedule" && (
                <div style={{ animation: "fadeIn 0.2s" }}>
                  <Suspense fallback={<TabFallback />}>
                    <MyScheduleTab />
                  </Suspense>
                </div>
              )}

              {/* ─── ACTIVITY LOG ─── */}
              {tab === "activity-log" && (
                <div style={{ animation: "fadeIn 0.2s" }}>
                  <Suspense fallback={<TabFallback />}>
                    <ActivityLog
                      token={getToken()}
                      isAdmin={false}
                      colors={C}
                      compact={false}
                    />
                  </Suspense>
                </div>
              )}

              {/* ─── SCREENSHOTS ─── */}
              {tab === "screenshots" && (
                <div style={{ animation: "fadeIn 0.2s" }}>
                  {/* toolbar */}
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 10,
                      marginBottom: 14,
                      flexWrap: "wrap",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 5,
                        fontSize: 13,
                        fontWeight: 700,
                        color: C.text,
                      }}
                    >
                      <Ico n="camera" s={14} c={C.textSub} />
                      {T.screenshots}
                      <span
                        style={{
                          fontSize: 11,
                          color: C.textLight,
                          fontWeight: 400,
                        }}
                      >
                        ({empScreenshots.length})
                      </span>
                    </div>
                    <div style={{ flex: 1 }} />
                    {/* Date navigator */}
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 4,
                        background: C.bg,
                        border: `1px solid ${C.border}`,
                        borderRadius: C.rSm,
                        padding: "3px 6px",
                      }}
                    >
                      <button
                        onClick={() => {
                          const d = new Date(empSsDate);
                          d.setDate(d.getDate() - 1);
                          setEmpSsDate(d.toISOString().split("T")[0]);
                        }}
                        style={{
                          background: "none",
                          border: "none",
                          cursor: "pointer",
                          padding: "2px 5px",
                          borderRadius: 4,
                          color: C.brand,
                          fontWeight: 700,
                          fontSize: 14,
                          lineHeight: 1,
                          display: "flex",
                          alignItems: "center",
                        }}
                      >
                        ‹
                      </button>
                      <input
                        type="date"
                        value={empSsDate}
                        onChange={(e) => setEmpSsDate(e.target.value)}
                        style={{
                          border: "none",
                          background: "transparent",
                          color: C.text,
                          fontSize: 12,
                          fontFamily: "inherit",
                          outline: "none",
                          cursor: "pointer",
                          textAlign: "center",
                        }}
                      />
                      <button
                        onClick={() => {
                          const d = new Date(empSsDate);
                          d.setDate(d.getDate() + 1);
                          const today = new Date().toISOString().split("T")[0];
                          const next = d.toISOString().split("T")[0];
                          if (next <= today) setEmpSsDate(next);
                        }}
                        style={{
                          background: "none",
                          border: "none",
                          cursor: "pointer",
                          padding: "2px 5px",
                          borderRadius: 4,
                          color:
                            empSsDate >= new Date().toISOString().split("T")[0]
                              ? C.textLight
                              : C.brand,
                          fontWeight: 700,
                          fontSize: 14,
                          lineHeight: 1,
                          display: "flex",
                          alignItems: "center",
                        }}
                      >
                        ›
                      </button>
                    </div>
                    {empSsDate !== new Date().toISOString().split("T")[0] && (
                      <button
                        onClick={() =>
                          setEmpSsDate(new Date().toISOString().split("T")[0])
                        }
                        style={{
                          padding: "5px 10px",
                          borderRadius: C.rSm,
                          border: `1px solid ${C.brandBdr || C.border}`,
                          background: C.brandBg,
                          color: C.brand,
                          fontSize: 11,
                          fontWeight: 700,
                          cursor: "pointer",
                          fontFamily: "inherit",
                        }}
                      >
                        {T.today || "Today"}
                      </button>
                    )}
                    <button
                      onClick={() =>
                        setEmpSsSort((s) =>
                          s === "newest" ? "oldest" : "newest",
                        )
                      }
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 5,
                        padding: "5px 11px",
                        borderRadius: C.rSm,
                        border: `1px solid ${C.border}`,
                        background: C.bg,
                        color: C.brand,
                        fontSize: 12,
                        fontWeight: 700,
                        cursor: "pointer",
                        fontFamily: "inherit",
                      }}
                    >
                      {empSsSort === "newest"
                        ? T.newest || "Newest"
                        : T.oldest || "Oldest"}
                      <Ico
                        n={empSsSort === "newest" ? "arrowdown" : "arrowup"}
                        s={11}
                        c={C.brand}
                      />
                    </button>
                  </div>
                  {empSsLoading ? (
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        height: 200,
                        gap: 10,
                      }}
                    >
                      <div
                        style={{
                          width: 22,
                          height: 22,
                          borderRadius: "50%",
                          border: `3px solid ${C.brand}`,
                          borderTopColor: "transparent",
                          animation: "spin .8s linear infinite",
                        }}
                      />
                      <span style={{ color: C.textSub, fontSize: 13 }}>
                        {T.loading || "Loading..."}
                      </span>
                    </div>
                  ) : empScreenshots.length > 0 ? (
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns:
                          "repeat(auto-fill,minmax(220px,1fr))",
                        gap: 14,
                      }}
                    >
                      {empScreenshots.map((ss, i) => {
                        const inactive = ss.is_active === false;
                        // Use DB thumbnail for grid card (fast), full-res for lightbox (clear)
                        const thumbSrc = ss.thumbnail
                          ? ss.thumbnail.startsWith("http") ||
                            ss.thumbnail.startsWith("/")
                            ? ss.thumbnail
                            : `data:image/jpeg;base64,${ss.thumbnail}`
                          : ss.file_url
                            ? ss.file_url.startsWith("http")
                              ? ss.file_url
                              : `${API_BASE}${ss.file_url}`
                            : `${API_BASE}/api/screenshots/${ss.id}/image`;
                        const fullSrc = ss.file_url
                          ? ss.file_url.startsWith("http")
                            ? ss.file_url
                            : `${API_BASE}${ss.file_url}`
                          : `${API_BASE}/api/screenshots/${ss.id}/image`;
                        const ts = ss.timestamp || ss.captured_at;
                        const tsUTC = ts
                          ? /[Z+]/.test(String(ts).slice(-6))
                            ? ts
                            : ts + "Z"
                          : null;
                        const timeStr = tsUTC
                          ? new Date(tsUTC).toLocaleTimeString("en-IN", {
                              hour: "2-digit",
                              minute: "2-digit",
                              hour12: true,
                              timeZone: getOrgTimeZone(),
                            })
                          : "";
                        const ssFailed = empSsFailed.has(ss.id);
                        return (
                          <div
                            key={i}
                            onClick={() => {
                              if (fullSrc) {
                                setLightboxImageFailed(ssFailed);
                                setEmpLightbox({
                                  url: fullSrc,
                                  appName: ss.app_name,
                                  timestamp: ts,
                                  mouseClicks: ss.mouse_clicks || 0,
                                  keyPresses: ss.key_presses || 0,
                                });
                              }
                            }}
                            style={{
                              background: C.surface,
                              borderRadius: 10,
                              border: `2px solid ${inactive ? "#EF4444" : C.border}`,
                              overflow: "hidden",
                              cursor: fullSrc ? "zoom-in" : "default",
                              transition: "border .15s,box-shadow .15s",
                            }}
                            onMouseEnter={(e) =>
                              (e.currentTarget.style.boxShadow =
                                "0 4px 14px rgba(0,0,0,.1)")
                            }
                            onMouseLeave={(e) =>
                              (e.currentTarget.style.boxShadow = "none")
                            }
                          >
                            <div
                              style={{
                                position: "relative",
                                height: 128,
                                background: C.bg,
                                overflow: "hidden",
                              }}
                            >
                              {thumbSrc && !ssFailed ? (
                                <AuthImage
                                  url={thumbSrc}
                                  alt={T.dashboardScreenshotAlt || "screenshot"}
                                  decoding="async"
                                  style={{
                                    width: "100%",
                                    height: "100%",
                                    objectFit: "cover",
                                    display: "block",
                                    filter: appSettings.screenshot_blur
                                      ? "blur(6px)"
                                      : "none",
                                  }}
                                  onError={() => {
                                    setEmpSsFailed((prev) => {
                                      if (prev.has(ss.id)) return prev;
                                      const next = new Set(prev);
                                      next.add(ss.id);
                                      return next;
                                    });
                                  }}
                                />
                              ) : (
                                <div
                                  style={{
                                    height: "100%",
                                    display: "flex",
                                    flexDirection: "column",
                                    alignItems: "center",
                                    justifyContent: "center",
                                    gap: 6,
                                  }}
                                >
                                  <Ico n="camera" s={26} c={C.textLight} />
                                  {ssFailed && (
                                    <span
                                      style={{
                                        color: C.textLight,
                                        fontSize: 10,
                                        fontWeight: 500,
                                      }}
                                    >
                                      {T.imageNotAvailable ||
                                        "Image not available"}
                                    </span>
                                  )}
                                </div>
                              )}
                              {inactive && (
                                <div
                                  style={{
                                    position: "absolute",
                                    inset: 0,
                                    background: "rgba(239,68,68,0.12)",
                                    pointerEvents: "none",
                                  }}
                                />
                              )}
                              {inactive && (
                                <div
                                  style={{
                                    position: "absolute",
                                    top: 7,
                                    right: 7,
                                    background: "#EF4444",
                                    color: "#fff",
                                    fontSize: 9,
                                    fontWeight: 800,
                                    padding: "2px 7px",
                                    borderRadius: 4,
                                    letterSpacing: ".5px",
                                  }}
                                >
                                  {T.idleTagLabel || "IDLE"}
                                </div>
                              )}
                              {ss.app_name && (
                                <div
                                  style={{
                                    position: "absolute",
                                    bottom: 7,
                                    left: 7,
                                    display: "flex",
                                    alignItems: "center",
                                    gap: 5,
                                    background: "rgba(0,0,0,.55)",
                                    backdropFilter: "blur(4px)",
                                    padding: "3px 8px",
                                    borderRadius: 5,
                                  }}
                                >
                                  <div
                                    style={{
                                      width: 14,
                                      height: 14,
                                      borderRadius: 3,
                                      background: `hsl(${(ss.app_name.charCodeAt(0) * 37) % 360},60%,55%)`,
                                      display: "flex",
                                      alignItems: "center",
                                      justifyContent: "center",
                                      fontSize: 8,
                                      color: "#fff",
                                      fontWeight: 800,
                                      flexShrink: 0,
                                    }}
                                  >
                                    {(ss.app_name[0] || "?").toUpperCase()}
                                  </div>
                                  <span
                                    style={{
                                      fontSize: 10,
                                      color: "#fff",
                                      fontWeight: 600,
                                      overflow: "hidden",
                                      textOverflow: "ellipsis",
                                      whiteSpace: "nowrap",
                                      maxWidth: 100,
                                    }}
                                  >
                                    {ss.app_name}
                                  </span>
                                </div>
                              )}
                            </div>
                            <div style={{ padding: "9px 11px 10px" }}>
                              <div
                                style={{
                                  display: "flex",
                                  justifyContent: "space-between",
                                  alignItems: "center",
                                  marginBottom: 4,
                                }}
                              >
                                <span
                                  style={{
                                    fontSize: 11,
                                    fontWeight: 700,
                                    color: C.text,
                                    overflow: "hidden",
                                    textOverflow: "ellipsis",
                                    whiteSpace: "nowrap",
                                    maxWidth: 130,
                                  }}
                                >
                                  {name}
                                </span>
                                <span
                                  style={{
                                    fontSize: 10,
                                    color: C.textLight,
                                    whiteSpace: "nowrap",
                                  }}
                                >
                                  {timeStr}
                                </span>
                              </div>
                              <div
                                style={{
                                  display: "flex",
                                  justifyContent: "space-between",
                                  alignItems: "center",
                                }}
                              >
                                <div style={{ display: "flex", gap: 12 }}>
                                  <span
                                    style={{
                                      display: "flex",
                                      alignItems: "center",
                                      gap: 4,
                                      fontSize: 11,
                                      color: C.textSub,
                                    }}
                                  >
                                    <span style={{ fontSize: 12 }}>🖱</span>
                                    {ss.mouse_clicks || 0}
                                  </span>
                                  <span
                                    style={{
                                      display: "flex",
                                      alignItems: "center",
                                      gap: 4,
                                      fontSize: 11,
                                      color: C.textSub,
                                    }}
                                  >
                                    <span style={{ fontSize: 12 }}>⌨</span>
                                    {ss.key_presses || 0}
                                  </span>
                                </div>
                                {fullSrc && (
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setEmpLightbox({
                                        url: fullSrc,
                                        appName: ss.app_name,
                                        timestamp: ts,
                                        mouseClicks: ss.mouse_clicks || 0,
                                        keyPresses: ss.key_presses || 0,
                                      });
                                    }}
                                    style={{
                                      fontSize: 10,
                                      color: C.brand,
                                      background: "none",
                                      border: `1px solid ${C.brandBdr || C.border}`,
                                      borderRadius: 4,
                                      padding: "2px 8px",
                                      cursor: "pointer",
                                      fontFamily: "inherit",
                                      fontWeight: 600,
                                    }}
                                  >
                                    {T.open || "Open"}
                                  </button>
                                )}
                              </div>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div
                      style={{
                        ...card(),
                        padding: "80px 40px",
                        textAlign: "center",
                      }}
                    >
                      <Ico n="camera" s={40} c={C.textLight} fw="1.5" />
                      <p
                        style={{
                          fontSize: 15,
                          color: C.textSub,
                          fontWeight: 600,
                          marginTop: 14,
                        }}
                      >
                        {T.noScreenshotsForDate || "No screenshots for"}{" "}
                        {empSsDate === new Date().toISOString().split("T")[0]
                          ? T.todayPeriod || "today"
                          : empSsDate}
                      </p>
                      <p
                        style={{
                          fontSize: 13,
                          color: C.textLight,
                          marginTop: 6,
                          lineHeight: 1.6,
                        }}
                      >
                        {T.screenshotsInstructions ||
                          "Clock in and ensure the Temveris desktop app is running. Screenshots are captured automatically at your configured interval."}
                      </p>
                    </div>
                  )}
                </div>
              )}

              {/* ─── TIMESHEET ─── */}
              {tab === "timesheet" && (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 20,
                    animation: "fadeIn 0.2s",
                  }}
                >
                  {/* Header bar */}
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      flexWrap: "wrap",
                      gap: 12,
                    }}
                  >
                    <div
                      style={{ display: "flex", alignItems: "center", gap: 12 }}
                    >
                      <button
                        onClick={() => setTsDateOffset((o) => o - 1)}
                        style={{
                          width: 34,
                          height: 34,
                          borderRadius: 8,
                          border: `1px solid ${C.border}`,
                          background: C.surface,
                          cursor: "pointer",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          transition: "all .12s",
                        }}
                      >
                        <Ico n="chevleft" s={14} c={C.textSub} />
                      </button>
                      <div
                        style={{
                          padding: "8px 20px",
                          borderRadius: 8,
                          background: C.surface,
                          border: `1px solid ${C.border}`,
                          fontSize: 14,
                          fontWeight: 700,
                          color: C.text,
                          minWidth: 180,
                          textAlign: "center",
                        }}
                      >
                        {isTodayTs ? (
                          <>
                            <span style={{ color: C.brand }}>
                              {T.today || "Today"}
                            </span>{" "}
                            — {tsLabel}
                          </>
                        ) : (
                          tsLabel
                        )}
                      </div>
                      <button
                        onClick={() =>
                          setTsDateOffset((o) => Math.min(0, o + 1))
                        }
                        style={{
                          width: 34,
                          height: 34,
                          borderRadius: 8,
                          border: `1px solid ${C.border}`,
                          background: C.surface,
                          cursor: "pointer",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          transition: "all .12s",
                        }}
                      >
                        <Ico n="chevright" s={14} c={C.textSub} />
                      </button>
                    </div>
                    <button
                      onClick={() => {
                        // Build CSV from the currently visible timesheet row.
                        // The button had no handler before — clicking it did
                        // literally nothing (BUG-007). Generate inline so we
                        // don't add a new backend endpoint just for one row.
                        const empName =
                          localStorage.getItem("name") || "Employee";
                        const empEmail = localStorage.getItem("email") || "";
                        const dateLabel =
                          typeof tsLabel === "string"
                            ? tsLabel
                            : String(tsLabel);
                        const rows = [
                          [
                            "Date",
                            "Employee",
                            "Email",
                            "Clock In",
                            "Clock Out",
                            "Worked Hours",
                            "Active Time",
                            "Worked Days",
                          ],
                          [
                            dateLabel,
                            empName,
                            empEmail,
                            tsClockInStr || "—",
                            tsClockOutStr || "—",
                            fmtHM(tsView.workedSecs || 0),
                            fmtHM(tsView.activeSecs || 0),
                            String(tsView.workedDays || 0),
                          ],
                        ];
                        const csv = rows
                          .map((r) =>
                            r
                              .map((v) => {
                                const s = String(v ?? "");
                                return /[",\n]/.test(s)
                                  ? `"${s.replace(/"/g, '""')}"`
                                  : s;
                              })
                              .join(","),
                          )
                          .join("\r\n");
                        const blob = new Blob([csv], {
                          type: "text/csv;charset=utf-8",
                        });
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement("a");
                        a.href = url;
                        a.download = `timesheet_${empName.replace(/\s+/g, "_")}_${dateLabel.replace(/[^\w-]/g, "_")}.csv`;
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        URL.revokeObjectURL(url);
                      }}
                      disabled={!tsView.hasData}
                      title={
                        tsView.hasData
                          ? "Export timesheet as CSV"
                          : "No data to export"
                      }
                      style={{
                        padding: "9px 20px",
                        borderRadius: 8,
                        border: "none",
                        background: tsView.hasData ? C.brand : C.borderLight,
                        color: tsView.hasData ? "#fff" : C.textLight,
                        fontWeight: 600,
                        fontSize: 13,
                        cursor: tsView.hasData ? "pointer" : "not-allowed",
                        fontFamily: "inherit",
                        display: "flex",
                        alignItems: "center",
                        gap: 7,
                        boxShadow: tsView.hasData
                          ? "0 2px 8px rgba(17,73,106,0.2)"
                          : "none",
                      }}
                    >
                      <Ico
                        n="export"
                        s={14}
                        c={tsView.hasData ? "#fff" : C.textLight}
                      />
                      {T.exportCsv || "Export"}
                    </button>
                  </div>

                  {/* Timesheet card */}
                  <div style={{ ...card(), overflow: "hidden" }}>
                    {/* Table header */}
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "220px 1fr 220px",
                        background: isDark ? "#1F2937" : "#F8FAFC",
                        borderBottom: `1px solid ${C.border}`,
                      }}
                    >
                      <div
                        style={{
                          padding: "14px 20px",
                          fontSize: 11,
                          fontWeight: 700,
                          color: C.textSub,
                          textTransform: "uppercase",
                          letterSpacing: "1px",
                          borderRight: `1px solid ${C.border}`,
                          display: "flex",
                          alignItems: "center",
                          gap: 6,
                        }}
                      >
                        <Ico n="user" s={13} c={C.textLight} />
                        {T.employeeLabel || "EMPLOYEE"}
                      </div>
                      <div
                        style={{
                          padding: "14px 20px",
                          fontSize: 11,
                          fontWeight: 700,
                          color: C.textSub,
                          textTransform: "uppercase",
                          letterSpacing: "1px",
                          borderRight: `1px solid ${C.border}`,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          gap: 6,
                        }}
                      >
                        <Ico n="clock" s={13} c={C.textLight} />
                        {T.timeDetailsHeader || "TIME DETAILS"}
                      </div>
                      <div
                        style={{
                          padding: "14px 20px",
                          fontSize: 11,
                          fontWeight: 700,
                          color: C.textSub,
                          textTransform: "uppercase",
                          letterSpacing: "1px",
                          display: "flex",
                          alignItems: "center",
                          gap: 6,
                        }}
                      >
                        <Ico n="check" s={13} c={C.textLight} />
                        {T.totalWorkSummary || "SUMMARY"}
                      </div>
                    </div>
                    {/* Table body */}
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "220px 1fr 220px",
                        minHeight: 110,
                      }}
                    >
                      <div
                        style={{
                          padding: "20px",
                          display: "flex",
                          alignItems: "center",
                          gap: 12,
                          borderRight: `1px solid ${C.border}`,
                        }}
                      >
                        <div
                          style={{
                            width: 40,
                            height: 40,
                            borderRadius: 12,
                            background:
                              "linear-gradient(135deg,#11496A,#44B97B)",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            color: "#fff",
                            fontWeight: 700,
                            fontSize: 14,
                            flexShrink: 0,
                            boxShadow: "0 2px 8px rgba(17,73,106,0.2)",
                          }}
                        >
                          {init}
                        </div>
                        <div>
                          <span
                            style={{
                              fontSize: 14,
                              fontWeight: 700,
                              color: C.text,
                              display: "block",
                            }}
                          >
                            {name}
                          </span>
                          <span
                            style={{
                              fontSize: 11,
                              color: tsView.clockedIn ? C.green : C.textLight,
                              fontWeight: 500,
                              display: "flex",
                              alignItems: "center",
                              gap: 4,
                              marginTop: 2,
                            }}
                          >
                            <span
                              style={{
                                width: 6,
                                height: 6,
                                borderRadius: "50%",
                                background: tsView.clockedIn
                                  ? "#22C55E"
                                  : "#D1D5DB",
                              }}
                            />
                            {tsView.clockedIn
                              ? T.activeStatus || "Active"
                              : T.offlineStatus || "Offline"}
                          </span>
                        </div>
                      </div>
                      <div
                        style={{
                          padding: "16px 20px",
                          borderRight: `1px solid ${C.border}`,
                          display: "flex",
                          alignItems: "center",
                        }}
                      >
                        {tsView.loading ? (
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              color: C.textLight,
                              fontSize: 13,
                              gap: 6,
                            }}
                          >
                            <Ico n="info" s={14} c={C.textLight} />
                            {T.loading || "Loading…"}
                          </div>
                        ) : tsView.hasData ? (
                          <div
                            style={{
                              display: "grid",
                              gridTemplateColumns: "repeat(4,1fr)",
                              gap: 12,
                              width: "100%",
                            }}
                          >
                            {[
                              {
                                label: T.clockInColLabel || "IN",
                                value: tsClockInStr,
                                color: C.green,
                                bg: isDark ? "rgba(34,197,94,0.08)" : "#F0FDF4",
                                bdr: "#BBF7D0",
                              },
                              {
                                label: T.clockOutColLabel || "OUT",
                                value: tsView.clockedIn
                                  ? T.working || "Working"
                                  : tsClockOutStr,
                                color: tsView.clockedIn ? C.brand : C.textSub,
                                bg: tsView.clockedIn
                                  ? isDark
                                    ? "rgba(17,73,106,0.08)"
                                    : "#E7F0F6"
                                  : isDark
                                    ? "#1F2937"
                                    : "#F9FAFB",
                                bdr: tsView.clockedIn ? "#B2DDFF" : C.border,
                              },
                              {
                                label: T.workLabel || "WORK",
                                value: fmtHM(tsView.workedSecs),
                                color: C.text,
                                bg: isDark ? "#1F2937" : "#F9FAFB",
                                bdr: C.border,
                              },
                              {
                                label: T.activeLabel || "ACTIVE",
                                value: fmtHM(tsView.activeSecs),
                                color: C.brand,
                                bg: isDark ? "rgba(17,73,106,0.08)" : "#E7F0F6",
                                bdr: "#B2DDFF",
                              },
                            ].map((m) => (
                              <div
                                key={m.label}
                                style={{
                                  padding: "10px 12px",
                                  borderRadius: 8,
                                  background: m.bg,
                                  border: `1px solid ${m.bdr}`,
                                  textAlign: "center",
                                }}
                              >
                                <p
                                  style={{
                                    fontSize: 9,
                                    fontWeight: 700,
                                    color: m.color,
                                    textTransform: "uppercase",
                                    letterSpacing: "0.8px",
                                    marginBottom: 4,
                                  }}
                                >
                                  {m.label}
                                </p>
                                <p
                                  style={{
                                    fontSize: 15,
                                    fontWeight: 700,
                                    color: C.text,
                                    fontVariantNumeric: "tabular-nums",
                                  }}
                                >
                                  {m.value}
                                </p>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              color: C.textLight,
                              fontSize: 13,
                              gap: 6,
                            }}
                          >
                            <Ico n="info" s={14} c={C.textLight} />
                            {T.noData || "No data for this date"}
                          </div>
                        )}
                      </div>
                      <div
                        style={{
                          padding: "16px 20px",
                          display: "flex",
                          alignItems: "center",
                        }}
                      >
                        <div
                          style={{
                            width: "100%",
                            background: isDark
                              ? "rgba(251,191,36,0.08)"
                              : "#FFFAEB",
                            border: `1px solid ${C.amberBdr}`,
                            borderRadius: 10,
                            padding: "12px 16px",
                          }}
                        >
                          {[
                            [
                              fmtHM(tsView.workedSecs),
                              T.workedHours || "Worked",
                              "#11496A",
                            ],
                            [
                              fmtHM(tsView.activeSecs),
                              T.activeDuration || "Active",
                              "#2F8F5C",
                            ],
                            [
                              String(tsView.workedDays),
                              T.workedDays || "Days",
                              "#B54708",
                            ],
                          ].map(([v, l2, c], i) => (
                            <div
                              key={i}
                              style={{
                                display: "flex",
                                justifyContent: "space-between",
                                alignItems: "center",
                                padding: "5px 0",
                                borderBottom:
                                  i < 2 ? `1px solid ${C.amberBdr}` : "none",
                              }}
                            >
                              <span
                                style={{
                                  fontWeight: 800,
                                  color: c,
                                  fontSize: 14,
                                  fontVariantNumeric: "tabular-nums",
                                }}
                              >
                                {v}
                              </span>
                              <span
                                style={{
                                  color: C.textSub,
                                  fontSize: 11,
                                  fontWeight: 500,
                                }}
                              >
                                {l2}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {/* ─── APPLY LEAVE ─── */}
              {tab === "leave" && (
                <div style={{ margin: "-8px -24px" }}>
                  <Suspense fallback={<TabFallback />}>
                    <LeavePage embedded />
                  </Suspense>
                </div>
              )}

              {/* ─── VIEW HOLIDAY ─── */}
              {tab === "holidays" && (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 20,
                    maxWidth: 960,
                    animation: "fadeIn 0.2s",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                    }}
                  >
                    <div
                      style={{ display: "flex", alignItems: "center", gap: 10 }}
                    >
                      <div
                        style={{
                          width: 36,
                          height: 36,
                          borderRadius: 10,
                          background: C.brandBg,
                          border: `1px solid ${C.brandBdr}`,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                        }}
                      >
                        <Ico n="calendar" s={17} c={C.brand} />
                      </div>
                      <h2
                        style={{ fontSize: 18, fontWeight: 700, color: C.text }}
                      >
                        {T.holidays_title || "Public Holidays"}
                      </h2>
                    </div>
                    <div
                      style={{
                        background: C.brandBg,
                        border: `1px solid ${C.brandBdr}`,
                        borderRadius: 20,
                        padding: "6px 16px",
                        fontSize: 12,
                        fontWeight: 700,
                        color: C.brand,
                      }}
                    >
                      {holidays.length} {T.holidays || "holidays"}
                    </div>
                  </div>
                  <div style={{ ...card(), overflow: "hidden" }}>
                    {holidays.length === 0 ? (
                      <div
                        style={{ padding: "48px 24px", textAlign: "center" }}
                      >
                        <p style={{ color: C.textSub, fontSize: 13 }}>
                          {T.noHolidaysConfiguredYet ||
                            "No holidays have been configured yet."}
                        </p>
                      </div>
                    ) : (
                      <table
                        style={{
                          width: "100%",
                          borderCollapse: "collapse",
                          fontSize: 13,
                        }}
                      >
                        <thead>
                          <tr
                            style={{
                              background: isDark ? "#1F2937" : "#F8FAFC",
                            }}
                          >
                            {[
                              T.holiday || "Holiday",
                              T.date || "Date",
                              T.day || "Day",
                              T.type || "Type",
                            ].map((h) => (
                              <th
                                key={h}
                                style={{
                                  padding: "14px 20px",
                                  textAlign: "left",
                                  fontSize: 11,
                                  fontWeight: 700,
                                  color: C.textSub,
                                  textTransform: "uppercase",
                                  letterSpacing: "0.8px",
                                  borderBottom: `1px solid ${C.border}`,
                                }}
                              >
                                {h}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {holidays.map((hol) => {
                            const typeColors = {
                              Public: {
                                bg: "#DCFCE7",
                                text: "#2F8F5C",
                                border: "#BBF7D0",
                              },
                              Company: {
                                bg: "#DBEAFE",
                                text: "#1D4ED8",
                                border: "#BFDBFE",
                              },
                              Optional: {
                                bg: "#FEF9C3",
                                text: "#B45309",
                                border: "#FDE68A",
                              },
                            };
                            const tc =
                              typeColors[hol.holiday_type] || typeColors.Public;
                            const fmtDate = hol.date
                              ? new Date(
                                  hol.date + "T00:00:00",
                                ).toLocaleDateString(lang || undefined, {
                                  day: "2-digit",
                                  month: "short",
                                  year: "numeric",
                                })
                              : hol.date;
                            return (
                              <tr
                                key={hol.id}
                                className="row-h"
                                style={{
                                  borderBottom: `1px solid ${C.borderLight}`,
                                }}
                              >
                                <td
                                  style={{
                                    padding: "14px 20px",
                                    fontWeight: 700,
                                    color: C.text,
                                  }}
                                >
                                  {hol.name}
                                </td>
                                <td
                                  style={{
                                    padding: "14px 20px",
                                    color: C.textSub,
                                    fontVariantNumeric: "tabular-nums",
                                  }}
                                >
                                  {fmtDate}
                                </td>
                                <td
                                  style={{
                                    padding: "14px 20px",
                                    color: C.textSub,
                                  }}
                                >
                                  {hol.day}
                                </td>
                                <td style={{ padding: "14px 20px" }}>
                                  <span
                                    style={{
                                      fontSize: 10,
                                      fontWeight: 700,
                                      color: tc.text,
                                      background: tc.bg,
                                      border: `1px solid ${tc.border}`,
                                      padding: "4px 12px",
                                      borderRadius: 20,
                                      letterSpacing: "0.3px",
                                    }}
                                  >
                                    {hol.holiday_type}
                                  </span>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    )}
                  </div>
                  <div
                    style={{
                      ...card(),
                      padding: "20px 24px",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      background: `linear-gradient(135deg, ${isDark ? "#1A1A1A" : "#E7F0F6"}, ${isDark ? "#0F0F0F" : "#F5F3FF"})`,
                      border: `1px solid ${isDark ? "#1E40AF" : "#BFDBFE"}`,
                    }}
                  >
                    <div>
                      <p
                        style={{ fontSize: 15, fontWeight: 700, color: C.text }}
                      >
                        {T.plan_leaves || "Plan your leaves"}
                      </p>
                      <p
                        style={{ fontSize: 12, color: C.textSub, marginTop: 4 }}
                      >
                        {T.plan_leaves_sub ||
                          "Apply for leave based on the holiday calendar"}
                      </p>
                    </div>
                    <button
                      onClick={() => setTab("leave")}
                      style={{
                        padding: "10px 24px",
                        borderRadius: 8,
                        border: "none",
                        background: C.brand,
                        color: "#fff",
                        fontWeight: 700,
                        fontSize: 13,
                        cursor: "pointer",
                        fontFamily: "inherit",
                        boxShadow: "0 2px 8px rgba(17,73,106,0.2)",
                        transition: "all .15s",
                      }}
                    >
                      {T.apply_leave_btn || "Apply Leave"}
                    </button>
                  </div>
                </div>
              )}

              {/* ─── PAYROLL CALCULATOR ─── */}
              {tab === "payroll" && (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 18,
                    maxWidth: 960,
                    animation: "fadeIn 0.2s",
                  }}
                >
                  <div
                    style={{ display: "flex", alignItems: "center", gap: 8 }}
                  >
                    <h2
                      style={{ fontSize: 18, fontWeight: 700, color: C.text }}
                    >
                      {T.payroll}
                    </h2>
                    <span
                      style={{
                        fontSize: 10,
                        fontWeight: 700,
                        color: "#EF4444",
                        background: "#FEE2E2",
                        padding: "2px 8px",
                        borderRadius: 4,
                      }}
                    >
                      {T.beta}
                    </span>
                  </div>
                  <div
                    style={{
                      ...card(),
                      padding: 20,
                      display: "grid",
                      gridTemplateColumns: "180px 1fr",
                      gap: 24,
                      alignItems: "start",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        alignItems: "center",
                        gap: 8,
                        paddingRight: 20,
                        borderRight: `1px solid ${C.borderLight}`,
                      }}
                    >
                      <div
                        style={{
                          width: 52,
                          height: 52,
                          borderRadius: 12,
                          background: C.brandBg,
                          border: `1px solid ${C.brandBdr}`,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                        }}
                      >
                        <Ico n="user" s={26} c={C.brand} />
                      </div>
                      <p
                        style={{ fontSize: 14, fontWeight: 700, color: C.text }}
                      >
                        {name.split(" ")[0]}
                      </p>
                    </div>
                    <div>
                      <p
                        style={{
                          fontSize: 14,
                          fontWeight: 600,
                          color: C.text,
                          marginBottom: 14,
                        }}
                      >
                        {T.performanceOverview || "Performance Overview"}
                      </p>
                      <div
                        style={{
                          display: "grid",
                          gridTemplateColumns: "repeat(3,1fr)",
                          gap: 12,
                        }}
                      >
                        {[
                          {
                            label: T.billable_hours,
                            value: fmtHM(dailyWorkedSecs),
                            bg: C.brandBg,
                            color: C.brand,
                          },
                          {
                            label: T.paid_amount,
                            value: `$${net.toLocaleString("en-US") || "0"}`,
                            bg: C.greenBg,
                            color: C.green,
                          },
                          {
                            label: T.pending_amount,
                            value: "—",
                            bg: C.redBg,
                            color: C.red,
                          },
                        ].map((m) => (
                          <div
                            key={m.label}
                            style={{
                              borderRadius: C.rSm,
                              padding: 14,
                              background: m.bg,
                            }}
                          >
                            <p
                              style={{
                                fontSize: 18,
                                fontWeight: 700,
                                color: C.text,
                                marginBottom: 2,
                              }}
                            >
                              {m.value}
                            </p>
                            <p
                              style={{
                                fontSize: 12,
                                fontWeight: 600,
                                color: m.color,
                              }}
                            >
                              {m.label}
                            </p>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                  <div
                    style={{
                      display: "flex",
                      gap: 0,
                      borderBottom: `1px solid ${C.border}`,
                    }}
                  >
                    {[
                      { k: "time-entries", l: T.time_entries },
                      { k: "payroll-details", l: T.payroll_details },
                      { k: "payroll-settings", l: T.payroll_settings },
                    ].map((tb) => (
                      <button
                        key={tb.k}
                        onClick={() => setPayTab(tb.k)}
                        style={{
                          padding: "10px 18px",
                          border: "none",
                          borderBottom: `2px solid ${payTab === tb.k ? C.brand : "transparent"}`,
                          background: "transparent",
                          color: payTab === tb.k ? C.brand : C.textSub,
                          fontWeight: payTab === tb.k ? 600 : 400,
                          fontSize: 13,
                          cursor: "pointer",
                          fontFamily: "inherit",
                        }}
                      >
                        {tb.l}
                      </button>
                    ))}
                  </div>
                  {payTab === "time-entries" && (
                    <div style={{ ...card(), overflow: "hidden" }}>
                      <table
                        style={{
                          width: "100%",
                          borderCollapse: "collapse",
                          fontSize: 13,
                        }}
                      >
                        <thead>
                          <tr style={{ background: C.bg }}>
                            {[
                              T.date || "Date",
                              T.clock_in_time || "Clock In",
                              T.clock_out_time || "Clock Out",
                              T.duration || "Duration",
                              T.status || "Status",
                            ].map((h) => (
                              <th
                                key={h}
                                style={{
                                  padding: "10px 16px",
                                  textAlign: "left",
                                  fontSize: 11,
                                  fontWeight: 600,
                                  color: C.textSub,
                                  textTransform: "uppercase",
                                  letterSpacing: "0.6px",
                                  borderBottom: `1px solid ${C.border}`,
                                }}
                              >
                                {h}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          <tr className="row-h">
                            <td
                              style={{
                                padding: "12px 16px",
                                color: C.text,
                                borderBottom: `1px solid ${C.borderLight}`,
                              }}
                            >
                              {now.toLocaleDateString("en-IN", {
                                month: "short",
                                day: "numeric",
                                year: "numeric",
                                timeZone: getOrgTimeZone(),
                              })}
                            </td>
                            <td
                              style={{
                                padding: "12px 16px",
                                color: C.text,
                                fontVariantNumeric: "tabular-nums",
                                borderBottom: `1px solid ${C.borderLight}`,
                              }}
                            >
                              {clockInStr}
                            </td>
                            <td
                              style={{
                                padding: "12px 16px",
                                color: C.text,
                                borderBottom: `1px solid ${C.borderLight}`,
                              }}
                            >
                              {clockOutStr}
                            </td>
                            <td
                              style={{
                                padding: "12px 16px",
                                color: C.brand,
                                fontWeight: 600,
                                borderBottom: `1px solid ${C.borderLight}`,
                              }}
                            >
                              {fmtHM(dailyWorkedSecs)}
                            </td>
                            <td
                              style={{
                                padding: "12px 16px",
                                borderBottom: `1px solid ${C.borderLight}`,
                              }}
                            >
                              {clockedIn && (
                                <span
                                  style={{
                                    fontSize: 11,
                                    fontWeight: 600,
                                    color: C.green,
                                    background: C.greenBg,
                                    padding: "2px 8px",
                                    borderRadius: 99,
                                  }}
                                >
                                  {T.active}
                                </span>
                              )}
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  )}
                  {payTab === "payroll-details" && (
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "1fr 1fr",
                        gap: 16,
                      }}
                    >
                      <div style={{ ...card(), padding: 18 }}>
                        <p
                          style={{
                            fontSize: 13,
                            fontWeight: 600,
                            color: C.text,
                            marginBottom: 14,
                          }}
                        >
                          {T.salary_components}
                        </p>
                        <div
                          style={{
                            display: "flex",
                            flexDirection: "column",
                            gap: 10,
                          }}
                        >
                          {[
                            { k: "basic", l: T.basic, d: false },
                            { k: "hra", l: T.hra, d: false },
                            { k: "da", l: T.da, d: false },
                            { k: "ta", l: T.ta, d: false },
                            { k: "pf", l: T.pf, d: true },
                            { k: "tax", l: T.tax, d: true },
                          ].map((f) => (
                            <div key={f.k}>
                              <span style={lbl}>
                                {f.l}
                                {f.d && (
                                  <span style={{ color: C.red }}>
                                    {" "}
                                    ({T.deduction})
                                  </span>
                                )}
                              </span>
                              <div style={{ position: "relative" }}>
                                <span
                                  style={{
                                    position: "absolute",
                                    left: 10,
                                    top: "50%",
                                    transform: "translateY(-50%)",
                                    color: C.textSub,
                                    fontSize: 13,
                                  }}
                                >
                                  $
                                </span>
                                <input
                                  type="number"
                                  value={pay[f.k]}
                                  onChange={(e) =>
                                    setPay((p) => ({
                                      ...p,
                                      [f.k]: e.target.value,
                                    }))
                                  }
                                  placeholder="0"
                                  style={{ ...inp, paddingLeft: 24 }}
                                />
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div style={{ ...card(), padding: 18 }}>
                        <p
                          style={{
                            fontSize: 13,
                            fontWeight: 600,
                            color: C.text,
                            marginBottom: 14,
                          }}
                        >
                          {T.breakdown}
                        </p>
                        {[
                          {
                            l: T.gross_earnings,
                            v: `$${gross.toLocaleString("en-US")}`,
                            c: C.text,
                          },
                          {
                            l: T.total_deductions,
                            v: `-$${deduct.toLocaleString("en-US")}`,
                            c: C.red,
                          },
                          {
                            l: T.net_salary,
                            v: `$${net.toLocaleString("en-US")}`,
                            c: C.green,
                            big: true,
                          },
                        ].map((r) => (
                          <div
                            key={r.l}
                            style={{
                              display: "flex",
                              justifyContent: "space-between",
                              alignItems: "center",
                              padding: "12px 0",
                              borderBottom: `1px solid ${C.borderLight}`,
                            }}
                          >
                            <span style={{ fontSize: 13, color: C.textSub }}>
                              {r.l}
                            </span>
                            <span
                              style={{
                                fontSize: r.big ? 18 : 14,
                                fontWeight: r.big ? 700 : 500,
                                color: r.c,
                              }}
                            >
                              {r.v}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {payTab === "payroll-settings" && (
                    <div
                      style={{ ...card(), padding: 24, textAlign: "center" }}
                    >
                      <Ico n="briefcase" s={28} c={C.textLight} />
                      <p
                        style={{
                          fontSize: 13,
                          color: C.textSub,
                          marginTop: 12,
                        }}
                      >
                        {T.settings_admin}
                      </p>
                    </div>
                  )}
                </div>
              )}

              {/* ─── AI DAILY SUMMARY ─── */}
              {tab === "ai-summary" && (
                <Suspense fallback={<TabFallback />}>
                  <DailySummaryView
                    summary={aiSummary}
                    weekly={aiWeekly}
                    loading={aiLoading}
                    isDark={isDark}
                    C={C}
                    T={T}
                    date={aiSummaryDate}
                    onDateChange={setAiSummaryDate}
                    onRefresh={() => setAiRefreshNonce((n) => n + 1)}
                  />
                </Suspense>
              )}

              {tab === "profile" && (
                <Suspense fallback={<TabFallback />}>
                  <ProfileContent embedded maxWidth={1100} />
                </Suspense>
              )}

              {/* ── My Data module (5 worker-facing sub-pages) ── */}
              {[
                "whats-recorded",
                "who-viewed",
                "privacy-controls",
                "corrections",
                "data-export",
              ].includes(tab) && (
                <Suspense fallback={<TabFallback />}>
                  <MyDataModule pane={tab} />
                </Suspense>
              )}

              {/* ── Notifications settings (Account → Notifications) ── */}
              {tab === "notifications" && (
                <Suspense fallback={<TabFallback />}>
                  <NotificationsModule />
                </Suspense>
              )}

              {/* ── Help (Account → Help) ── */}
              {tab === "help" && (
                <Suspense fallback={<TabFallback />}>
                  <HelpModule onJumpToTab={(t) => setTab(t)} />
                </Suspense>
              )}

              {/* ── Settings (Account → Settings) ── */}
              {tab === "settings" && (
                <Suspense fallback={<TabFallback />}>
                  <SettingsModule
                    onJumpToTab={(t) => setTab(t)}
                    onPreferencesChange={(p) =>
                      setWorkerPrefs((prev) => ({ ...prev, ...p }))
                    }
                  />
                </Suspense>
              )}

              {/* ── My Pay → Estimate (Tier-0 Spec) ── */}
              {tab === "earnings" &&
                (() => {
                  const ed = earningsData;
                  // The server's currency for THIS workspace. It fell back to
                  // "INR" and then rendered ₹ for any unrecognised code, so a
                  // tenant paid in AED saw their salary labelled in rupees.
                  const currency = ed?.currency || "";
                  const SYMBOLS = { USD: "$", EUR: "€", GBP: "£", INR: "₹",
                                    AUD: "$", CAD: "$", SGD: "$", AED: "د.إ",
                                    JPY: "¥", CNY: "¥" };
                  // Unknown/unset code → show the ISO code, never another
                  // country's symbol.
                  const sym = SYMBOLS[currency] || (currency ? currency + " " : "");
                  // Money formatter honours workerPrefs.number_format so the user's
                  // chosen thousand/decimal style applies everywhere on this tab.
                  // mask=true (workerPrefs.hide_payroll_values) returns "•••" instead
                  // so screen sharers / shoulder readers can't see real values until
                  // the worker explicitly opts in via the Reveal eye toggle.
                  const mask =
                    !!workerPrefs.hide_payroll_values && !payrollRevealed;
                  const fmt = (n) => (mask ? "•••••" : fmtNumberPref(n, 2));
                  const fmtH = (h) => `${Number(h || 0).toFixed(1)}h`;

                  const periodLabel = ed
                    ? `${new Date(ed.period_start + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" })} – ${new Date(ed.period_end + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}`
                    : "—";

                  // Timeline steps
                  const tlSteps = [
                    {
                      label: T.dashboardStepPayPeriod || "Pay period",
                      done: true,
                    },
                    {
                      label: T.dashboardStepApproval || "Approval",
                      done: !!(ed && ed.worked_hours_approved > 0),
                      active: !(ed && ed.worked_hours_approved > 0),
                    },
                    { label: "Payroll review", done: false },
                    { label: "Payslip", done: false },
                    { label: "Pay date", done: false },
                  ];

                  // Breakdown rows derived from config components
                  const breakdownComponents = ed
                    ? [
                        {
                          label: T.dashboardCompBasicSalary || "Basic Salary",
                          value: ed.basic_salary,
                          status: "approved",
                          type: "Regular",
                        },
                        {
                          label: "HRA",
                          value: ed.hra,
                          status: "approved",
                          type: "Allowance",
                        },
                        {
                          label: "Dearness Allowance",
                          value: ed.da,
                          status: "approved",
                          type: "Allowance",
                        },
                        {
                          label: "Travel Allowance",
                          value: ed.ta,
                          status: "approved",
                          type: "Allowance",
                        },
                        {
                          label: "Bonus",
                          value: ed.bonus,
                          status: "pending",
                          type: "Bonus",
                        },
                      ].filter((r) => r.value > 0)
                    : [];

                  const filteredRows = breakdownComponents.filter((r) =>
                    earningsBreakFilter === "all"
                      ? true
                      : earningsBreakFilter === "approved"
                        ? r.status === "approved"
                        : earningsBreakFilter === "pending"
                          ? r.status === "pending"
                          : earningsBreakFilter === "adjustments"
                            ? r.type === "Bonus"
                            : true,
                  );

                  // "What could change" drivers
                  const changeDrivers = ed
                    ? [
                        ed.worked_hours_today > 0 && {
                          text: `${fmtH(ed.worked_hours_today)} of today's hours awaiting timesheet approval`,
                          impact: `~${sym}${fmt((ed.worked_hours_today || 0) * (ed.hourly_rate || (ed.gross || 0) / 160))}`,
                        },
                        !(ed.pf > 0 || ed.tax > 0) && {
                          text: "Final tax and statutory deductions not yet calculated",
                          impact: null,
                        },
                        ed.ta > 0 && {
                          text: "Travel reimbursements subject to manager approval",
                          impact: `+${sym}${fmt(ed.ta)}`,
                        },
                      ].filter(Boolean)
                    : [];

                  // Submit question handler
                  const submitQuestion = async () => {
                    if (!earningsQText.trim()) return;
                    setEarningsQSubmitting(true);
                    const tok = getToken();
                    try {
                      await fetch(`${API_BASE}/notifications/`, {
                        method: "POST",
                        headers: {
                          "Content-Type": "application/json",
                          ...(tok ? { Authorization: `Bearer ${tok}` } : {}),
                        },
                        body: JSON.stringify({
                          category: earningsQCategory || "pay_question",
                          message: earningsQText,
                          meta: { date: earningsQDate },
                        }),
                      });
                    } catch {
                      /* best-effort */
                    }
                    setEarningsQSent(true);
                    setEarningsQSubmitting(false);
                    setTimeout(() => {
                      setEarningsQOpen(false);
                      setEarningsQSent(false);
                      setEarningsQText("");
                      setEarningsQCategory("");
                      setEarningsQDate("");
                    }, 2200);
                  };

                  return (
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: 0,
                        maxWidth: 900,
                        animation: "fadeIn 0.2s",
                      }}
                    >
                      {/* ── My Pay tab strip ── */}
                      <div
                        style={{
                          display: "flex",
                          gap: 0,
                          borderBottom: `2px solid ${C.border}`,
                          marginBottom: 20,
                        }}
                      >
                        {[
                          { k: "earnings", l: "Estimate" },
                          ...(appSettings.payroll_enabled
                            ? [{ k: "payroll", l: "Payslips" }]
                            : []),
                        ].map((t) => (
                          <button
                            key={t.k}
                            onClick={() => setTab(t.k)}
                            style={{
                              padding: "10px 20px",
                              border: "none",
                              borderBottom: `2.5px solid ${tab === t.k ? C.brand : "transparent"}`,
                              background: "transparent",
                              color: tab === t.k ? C.brand : C.textSub,
                              fontWeight: tab === t.k ? 700 : 500,
                              fontSize: 14,
                              cursor: "pointer",
                              fontFamily: "inherit",
                              marginBottom: -2,
                              transition: "color .15s",
                            }}
                          >
                            {t.l}
                          </button>
                        ))}
                      </div>

                      {/* ── Legal disclaimer ── */}
                      <div
                        style={{
                          background: `${C.amber}10`,
                          border: `1px solid ${C.amberBdr}`,
                          borderRadius: 8,
                          padding: "10px 14px",
                          marginBottom: 16,
                        }}
                      >
                        <p
                          style={{
                            fontSize: 12,
                            color: C.amber,
                            lineHeight: 1.5,
                          }}
                        >
                          This is an estimate for the current pay period. It is
                          not a final payslip — final pay may differ after
                          payroll review and approval.
                        </p>
                      </div>

                      {/* ── Loading ── */}
                      {earningsLoading && (
                        <div
                          style={{
                            ...card(),
                            padding: 60,
                            textAlign: "center",
                          }}
                        >
                          <p style={{ fontSize: 13, color: C.textSub }}>
                            {T.dashboardLoadingEarnings ||
                              "Loading your earnings data…"}
                          </p>
                        </div>
                      )}

                      {/* ── No config state ── */}
                      {!earningsLoading && !ed && (
                        <div
                          style={{
                            ...card(),
                            padding: "60px 40px",
                            textAlign: "center",
                          }}
                        >
                          <div
                            style={{
                              width: 56,
                              height: 56,
                              borderRadius: "50%",
                              background: `${C.brand}12`,
                              border: `1.5px solid ${C.brandBdr}`,
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "center",
                              margin: "0 auto 16px",
                            }}
                          >
                            <Ico n="dollar" s={24} c={C.brand} />
                          </div>
                          <p
                            style={{
                              fontSize: 16,
                              fontWeight: 800,
                              color: C.text,
                              marginBottom: 8,
                            }}
                          >
                            No pay configuration yet
                          </p>
                          <p
                            style={{
                              fontSize: 13,
                              color: C.textSub,
                              maxWidth: 360,
                              margin: "0 auto 20px",
                              lineHeight: 1.6,
                            }}
                          >
                            Your payroll configuration hasn't been set up.
                            Contact HR or your administrator to get started.
                          </p>
                          <button
                            onClick={() => setEarningsQOpen(true)}
                            style={{
                              padding: "9px 20px",
                              borderRadius: 8,
                              border: `1px solid ${C.brand}`,
                              background: `${C.brand}15`,
                              color: C.brand,
                              fontSize: 13,
                              fontWeight: 700,
                              cursor: "pointer",
                              fontFamily: "inherit",
                            }}
                          >
                            Ask a question
                          </button>
                        </div>
                      )}

                      {!earningsLoading && ed && (
                        <div
                          style={{
                            display: "flex",
                            flexDirection: "column",
                            gap: 16,
                          }}
                        >
                          {/* ── Top controls row ── */}
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: 10,
                              flexWrap: "wrap",
                            }}
                          >
                            <div
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: 8,
                                padding: "7px 14px",
                                borderRadius: 8,
                                border: `1px solid ${C.border}`,
                                background: C.surface,
                                fontSize: 13,
                                color: C.textSub,
                              }}
                            >
                              <Ico n="calendar" s={13} c={C.textSub} />
                              <span style={{ fontWeight: 600, color: C.text }}>
                                {periodLabel}
                              </span>
                            </div>
                            <span
                              style={{
                                display: "inline-flex",
                                alignItems: "center",
                                gap: 5,
                                fontSize: 11,
                                fontWeight: 700,
                                padding: "5px 10px",
                                borderRadius: 99,
                                background: `${C.green}15`,
                                color: C.green,
                              }}
                            >
                              <span
                                style={{
                                  width: 5,
                                  height: 5,
                                  borderRadius: "50%",
                                  background: C.green,
                                }}
                              />
                              Live estimate
                            </span>
                            <span style={{ fontSize: 11, color: C.textSub }}>
                              Updated just now
                            </span>
                            <div
                              style={{
                                marginLeft: "auto",
                                display: "flex",
                                alignItems: "center",
                                gap: 8,
                              }}
                            >
                              {workerPrefs.hide_payroll_values && (
                                <button
                                  onClick={() => setPayrollRevealed((v) => !v)}
                                  title={
                                    payrollRevealed
                                      ? T.dashboardHidePayValues ||
                                        "Hide pay values"
                                      : T.dashboardShowPayValues ||
                                        "Show pay values"
                                  }
                                  aria-pressed={payrollRevealed}
                                  style={{
                                    display: "flex",
                                    alignItems: "center",
                                    gap: 6,
                                    padding: "8px 14px",
                                    borderRadius: 8,
                                    border: `1px solid ${C.border}`,
                                    background: payrollRevealed
                                      ? C.brandBg
                                      : C.surface,
                                    color: payrollRevealed
                                      ? C.brand
                                      : C.textSub,
                                    fontSize: 13,
                                    fontWeight: 600,
                                    cursor: "pointer",
                                    fontFamily: "inherit",
                                  }}
                                >
                                  <Ico
                                    n={payrollRevealed ? "eye_off" : "eye"}
                                    s={13}
                                    c={payrollRevealed ? C.brand : C.textSub}
                                  />
                                  {payrollRevealed
                                    ? T.dashboardHide || "Hide"
                                    : T.dashboardReveal || "Reveal"}
                                </button>
                              )}
                              <button
                                onClick={() => setEarningsQOpen(true)}
                                style={{
                                  display: "flex",
                                  alignItems: "center",
                                  gap: 6,
                                  padding: "8px 16px",
                                  borderRadius: 8,
                                  border: `1px solid ${C.border}`,
                                  background: C.surface,
                                  color: C.text,
                                  fontSize: 13,
                                  fontWeight: 600,
                                  cursor: "pointer",
                                  fontFamily: "inherit",
                                }}
                              >
                                <Ico n="help" s={13} c={C.brand} />
                                Ask a question
                              </button>
                            </div>
                          </div>

                          {/* ── Headline + What could change ── */}
                          <div
                            style={{
                              display: "grid",
                              gridTemplateColumns: "1fr 320px",
                              gap: 16,
                            }}
                          >
                            {/* Headline estimate */}
                            <div style={{ ...card(), padding: 28 }}>
                              <p
                                style={{
                                  fontSize: 11,
                                  fontWeight: 700,
                                  color: C.textSub,
                                  textTransform: "uppercase",
                                  letterSpacing: ".6px",
                                  marginBottom: 8,
                                }}
                              >
                                {T.dashboardEstimatedEarningsPeriod ||
                                  "Estimated earnings this period"}
                              </p>
                              <p
                                style={{
                                  fontSize: 44,
                                  fontWeight: 900,
                                  color: C.text,
                                  letterSpacing: "-1px",
                                  lineHeight: 1,
                                }}
                              >
                                {sym}
                                {fmt(ed.estimated_gross)}
                              </p>
                              <p
                                style={{
                                  fontSize: 12,
                                  color: C.textSub,
                                  marginTop: 4,
                                  marginBottom: 20,
                                }}
                              >
                                estimated gross ·{" "}
                                {ed.pay_frequency || "monthly"}
                              </p>
                              <div
                                style={{
                                  display: "flex",
                                  gap: 20,
                                  flexWrap: "wrap",
                                }}
                              >
                                <div>
                                  <p
                                    style={{
                                      fontSize: 11,
                                      color: C.textSub,
                                      marginBottom: 2,
                                    }}
                                  >
                                    {T.dashboardApprovedHours ||
                                      "Approved hours"}
                                  </p>
                                  <p
                                    style={{
                                      fontSize: 16,
                                      fontWeight: 800,
                                      color: C.green,
                                    }}
                                  >
                                    {fmtH(ed.worked_hours_approved)}
                                  </p>
                                </div>
                                {ed.worked_hours_today > 0 && (
                                  <div>
                                    <p
                                      style={{
                                        fontSize: 11,
                                        color: C.textSub,
                                        marginBottom: 2,
                                      }}
                                    >
                                      {T.dashboardTodayPending ||
                                        "Today (pending)"}
                                    </p>
                                    <p
                                      style={{
                                        fontSize: 16,
                                        fontWeight: 800,
                                        color: C.amber,
                                      }}
                                    >
                                      {fmtH(ed.worked_hours_today)}
                                    </p>
                                  </div>
                                )}
                                {ed.pay_type !== "hourly" && (
                                  <div>
                                    <p
                                      style={{
                                        fontSize: 11,
                                        color: C.textSub,
                                        marginBottom: 2,
                                      }}
                                    >
                                      {T.dashboardPayType || "Pay type"}
                                    </p>
                                    <p
                                      style={{
                                        fontSize: 14,
                                        fontWeight: 700,
                                        color: C.text,
                                        textTransform: "capitalize",
                                      }}
                                    >
                                      {ed.pay_type || "Salaried"}
                                    </p>
                                  </div>
                                )}
                              </div>
                            </div>

                            {/* What could change */}
                            <div style={{ ...card(), padding: 20 }}>
                              <p
                                style={{
                                  fontSize: 12,
                                  fontWeight: 700,
                                  color: C.text,
                                  marginBottom: 14,
                                }}
                              >
                                {T.dashboardWhatCouldChange ||
                                  "What could change"}
                              </p>
                              {changeDrivers.length === 0 && (
                                <p style={{ fontSize: 12, color: C.textSub }}>
                                  {T.dashboardNoPendingItems ||
                                    "No pending items at this time."}
                                </p>
                              )}
                              <div
                                style={{
                                  display: "flex",
                                  flexDirection: "column",
                                  gap: 10,
                                }}
                              >
                                {changeDrivers.map((d, i) => (
                                  <div
                                    key={i}
                                    style={{
                                      display: "flex",
                                      justifyContent: "space-between",
                                      gap: 8,
                                      padding: "8px 10px",
                                      background: C.bg,
                                      borderRadius: 8,
                                      border: `1px solid ${C.border}`,
                                    }}
                                  >
                                    <p
                                      style={{
                                        fontSize: 12,
                                        color: C.text,
                                        lineHeight: 1.4,
                                        flex: 1,
                                      }}
                                    >
                                      {d.text}
                                    </p>
                                    {d.impact && (
                                      <span
                                        style={{
                                          fontSize: 12,
                                          fontWeight: 700,
                                          color: C.amber,
                                          flexShrink: 0,
                                        }}
                                      >
                                        {d.impact}
                                      </span>
                                    )}
                                  </div>
                                ))}
                              </div>
                            </div>
                          </div>

                          {/* ── Pay timeline stepper ── */}
                          <div style={{ ...card(), padding: "16px 24px" }}>
                            <div
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: 0,
                              }}
                            >
                              {tlSteps.map((step, i) => (
                                <React.Fragment key={step.label}>
                                  <div
                                    style={{
                                      display: "flex",
                                      flexDirection: "column",
                                      alignItems: "center",
                                      gap: 4,
                                      minWidth: 80,
                                    }}
                                  >
                                    <div
                                      style={{
                                        width: 28,
                                        height: 28,
                                        borderRadius: "50%",
                                        border: `2px solid ${step.done ? C.green : step.active ? C.brand : C.border}`,
                                        background: step.done
                                          ? C.green
                                          : step.active
                                            ? `${C.brand}20`
                                            : C.bg,
                                        display: "flex",
                                        alignItems: "center",
                                        justifyContent: "center",
                                      }}
                                    >
                                      {step.done ? (
                                        <Ico n="check" s={12} c="#fff" fw="3" />
                                      ) : (
                                        <span
                                          style={{
                                            width: 8,
                                            height: 8,
                                            borderRadius: "50%",
                                            background: step.active
                                              ? C.brand
                                              : C.border,
                                          }}
                                        />
                                      )}
                                    </div>
                                    <p
                                      style={{
                                        fontSize: 10,
                                        fontWeight:
                                          step.active || step.done ? 700 : 400,
                                        color: step.done
                                          ? C.green
                                          : step.active
                                            ? C.brand
                                            : C.textSub,
                                        textAlign: "center",
                                        whiteSpace: "nowrap",
                                      }}
                                    >
                                      {step.done || step.active
                                        ? step.label
                                        : T.dashboardStepExpected || "Expected"}
                                    </p>
                                  </div>
                                  {i < tlSteps.length - 1 && (
                                    <div
                                      style={{
                                        flex: 1,
                                        height: 2,
                                        background:
                                          i <
                                            tlSteps.findIndex(
                                              (s) => !s.done && !s.active,
                                            ) -
                                              1 ||
                                          (i === 0 && tlSteps[1]?.done)
                                            ? C.green
                                            : C.border,
                                        margin: "0 2px",
                                        marginBottom: 20,
                                      }}
                                    />
                                  )}
                                </React.Fragment>
                              ))}
                            </div>
                          </div>

                          {/* ── Earnings breakdown ── */}
                          <div style={{ ...card(), overflow: "hidden" }}>
                            <div
                              style={{
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "space-between",
                                padding: "14px 18px",
                                borderBottom: `1px solid ${C.border}`,
                              }}
                            >
                              <p
                                style={{
                                  fontSize: 13,
                                  fontWeight: 700,
                                  color: C.text,
                                }}
                              >
                                {T.dashboardEarningsBreakdown ||
                                  "Earnings breakdown"}
                              </p>
                              <div style={{ display: "flex", gap: 6 }}>
                                {[
                                  "all",
                                  "approved",
                                  "pending",
                                  "adjustments",
                                ].map((f) => (
                                  <button
                                    key={f}
                                    onClick={() => setEarningsBreakFilter(f)}
                                    style={{
                                      padding: "5px 12px",
                                      borderRadius: 6,
                                      border: `1px solid ${earningsBreakFilter === f ? C.brand : C.border}`,
                                      background:
                                        earningsBreakFilter === f
                                          ? C.brand
                                          : "transparent",
                                      color:
                                        earningsBreakFilter === f
                                          ? "#fff"
                                          : C.textSub,
                                      fontSize: 12,
                                      fontWeight: 600,
                                      cursor: "pointer",
                                      fontFamily: "inherit",
                                      textTransform: "capitalize",
                                    }}
                                  >
                                    {f}
                                  </button>
                                ))}
                              </div>
                            </div>
                            {filteredRows.length === 0 ? (
                              <div
                                style={{
                                  padding: "32px 18px",
                                  textAlign: "center",
                                }}
                              >
                                <p style={{ fontSize: 13, color: C.textSub }}>
                                  {T.dashboardNoEntriesFilter ||
                                    "No entries for this filter."}
                                </p>
                              </div>
                            ) : (
                              <table
                                style={{
                                  width: "100%",
                                  borderCollapse: "collapse",
                                  fontSize: 13,
                                }}
                              >
                                <thead>
                                  <tr style={{ background: C.bg }}>
                                    {[
                                      "Component",
                                      "Type",
                                      "Amount",
                                      "Status",
                                    ].map((h) => (
                                      <th
                                        key={h}
                                        style={{
                                          padding: "10px 18px",
                                          textAlign: "left",
                                          fontSize: 11,
                                          fontWeight: 600,
                                          color: C.textSub,
                                          textTransform: "uppercase",
                                          letterSpacing: ".5px",
                                          borderBottom: `1px solid ${C.border}`,
                                        }}
                                      >
                                        {h}
                                      </th>
                                    ))}
                                  </tr>
                                </thead>
                                <tbody>
                                  {filteredRows.map((row, i) => (
                                    <tr key={i} className="row-h">
                                      <td
                                        style={{
                                          padding: "12px 18px",
                                          color: C.text,
                                          fontWeight: 600,
                                          borderBottom: `1px solid ${C.borderLight}`,
                                        }}
                                      >
                                        {row.label}
                                      </td>
                                      <td
                                        style={{
                                          padding: "12px 18px",
                                          color: C.textSub,
                                          borderBottom: `1px solid ${C.borderLight}`,
                                        }}
                                      >
                                        {row.type}
                                      </td>
                                      <td
                                        style={{
                                          padding: "12px 18px",
                                          color: C.text,
                                          fontWeight: 700,
                                          borderBottom: `1px solid ${C.borderLight}`,
                                        }}
                                      >
                                        {sym}
                                        {fmt(row.value)}
                                      </td>
                                      <td
                                        style={{
                                          padding: "12px 18px",
                                          borderBottom: `1px solid ${C.borderLight}`,
                                        }}
                                      >
                                        <span
                                          style={{
                                            fontSize: 11,
                                            fontWeight: 700,
                                            padding: "2px 8px",
                                            borderRadius: 99,
                                            background:
                                              row.status === "approved"
                                                ? `${C.green}15`
                                                : `${C.amber}15`,
                                            color:
                                              row.status === "approved"
                                                ? C.green
                                                : C.amber,
                                          }}
                                        >
                                          {row.status}
                                        </span>
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                                <tfoot>
                                  <tr style={{ background: C.bg }}>
                                    <td
                                      colSpan={2}
                                      style={{
                                        padding: "12px 18px",
                                        fontWeight: 700,
                                        color: C.text,
                                        fontSize: 13,
                                      }}
                                    >
                                      {T.dashboardTotal || "Total"}
                                    </td>
                                    <td
                                      style={{
                                        padding: "12px 18px",
                                        fontWeight: 800,
                                        color: C.brand,
                                        fontSize: 15,
                                      }}
                                    >
                                      {sym}
                                      {fmt(
                                        filteredRows.reduce(
                                          (s, r) => s + Number(r.value || 0),
                                          0,
                                        ),
                                      )}
                                    </td>
                                    <td />
                                  </tr>
                                </tfoot>
                              </table>
                            )}
                          </div>

                          {/* ── Deductions (conditional — only when data exists) ── */}
                          {(ed.pf > 0 || ed.tax > 0) && (
                            <div style={{ ...card(), padding: 20 }}>
                              <p
                                style={{
                                  fontSize: 13,
                                  fontWeight: 700,
                                  color: C.text,
                                  marginBottom: 14,
                                }}
                              >
                                {T.dashboardEstimatedDeductions ||
                                  "Estimated deductions"}
                              </p>
                              <div
                                style={{
                                  display: "flex",
                                  flexDirection: "column",
                                  gap: 8,
                                }}
                              >
                                {ed.pf > 0 && (
                                  <div
                                    style={{
                                      display: "flex",
                                      justifyContent: "space-between",
                                      padding: "10px 14px",
                                      background: C.bg,
                                      borderRadius: 8,
                                      border: `1px solid ${C.border}`,
                                    }}
                                  >
                                    <span
                                      style={{ fontSize: 13, color: C.textSub }}
                                    >
                                      {T.dashboardProvidentFund ||
                                        "Provident Fund (PF)"}
                                    </span>
                                    <span
                                      style={{
                                        fontSize: 13,
                                        fontWeight: 700,
                                        color: C.red,
                                      }}
                                    >
                                      -{sym}
                                      {fmt(ed.pf)}
                                    </span>
                                  </div>
                                )}
                                {ed.tax > 0 && (
                                  <div
                                    style={{
                                      display: "flex",
                                      justifyContent: "space-between",
                                      padding: "10px 14px",
                                      background: C.bg,
                                      borderRadius: 8,
                                      border: `1px solid ${C.border}`,
                                    }}
                                  >
                                    <span
                                      style={{ fontSize: 13, color: C.textSub }}
                                    >
                                      {T.dashboardTaxTds || "Tax (TDS)"}
                                    </span>
                                    <span
                                      style={{
                                        fontSize: 13,
                                        fontWeight: 700,
                                        color: C.red,
                                      }}
                                    >
                                      -{sym}
                                      {fmt(ed.tax)}
                                    </span>
                                  </div>
                                )}
                              </div>
                              <div
                                style={{
                                  display: "flex",
                                  justifyContent: "space-between",
                                  padding: "14px 0 0",
                                  marginTop: 10,
                                  borderTop: `1px solid ${C.border}`,
                                }}
                              >
                                <span
                                  style={{ fontSize: 13, color: C.textSub }}
                                >
                                  {T.dashboardGrossEarnings || "Gross earnings"}
                                </span>
                                <span
                                  style={{
                                    fontSize: 14,
                                    fontWeight: 700,
                                    color: C.text,
                                  }}
                                >
                                  {sym}
                                  {fmt(ed.gross)}
                                </span>
                              </div>
                              <div
                                style={{
                                  display: "flex",
                                  justifyContent: "space-between",
                                  padding: "8px 0 0",
                                }}
                              >
                                <span
                                  style={{ fontSize: 13, color: C.textSub }}
                                >
                                  {T.dashboardTotalDeductions ||
                                    "Total deductions"}
                                </span>
                                <span
                                  style={{
                                    fontSize: 14,
                                    fontWeight: 700,
                                    color: C.red,
                                  }}
                                >
                                  -{sym}
                                  {fmt(ed.deductions)}
                                </span>
                              </div>
                              <div
                                style={{
                                  display: "flex",
                                  justifyContent: "space-between",
                                  alignItems: "center",
                                  padding: "13px 16px",
                                  marginTop: 12,
                                  background: `${C.green}10`,
                                  borderRadius: 10,
                                  border: `1px solid ${C.green}25`,
                                }}
                              >
                                <span
                                  style={{
                                    fontSize: 14,
                                    fontWeight: 800,
                                    color: C.text,
                                  }}
                                >
                                  {T.dashboardEstimatedNetPay ||
                                    "Estimated net pay"}
                                </span>
                                <span
                                  style={{
                                    fontSize: 20,
                                    fontWeight: 900,
                                    color: C.green,
                                  }}
                                >
                                  {sym}
                                  {fmt(ed.net)}
                                </span>
                              </div>
                              <p
                                style={{
                                  fontSize: 11,
                                  color: C.textSub,
                                  marginTop: 10,
                                  lineHeight: 1.5,
                                }}
                              >
                                {T.dashboardDeductionsDisclaimer ||
                                  "Deductions shown are estimates only. Final amounts are confirmed after payroll review."}
                              </p>
                            </div>
                          )}

                          {/* ── Year-to-date summary (compact) ── */}
                          <div style={{ ...card(), padding: 18 }}>
                            <p
                              style={{
                                fontSize: 12,
                                fontWeight: 700,
                                color: C.text,
                                marginBottom: 12,
                              }}
                            >
                              {T.dashboardYtdSummary || "Year-to-date summary"}
                            </p>
                            <div
                              style={{
                                display: "grid",
                                gridTemplateColumns: "repeat(3,1fr)",
                                gap: 12,
                              }}
                            >
                              {[
                                {
                                  label:
                                    T.dashboardYtdApprovedHours ||
                                    "Total approved hours",
                                  value: fmtH(ed.worked_hours_approved),
                                  note:
                                    T.dashboardYtdThisPeriod || "This period",
                                },
                                {
                                  label:
                                    T.dashboardYtdGrossPay ||
                                    "Gross pay (period)",
                                  value: `${sym}${fmt(ed.estimated_gross)}`,
                                  note: T.dashboardYtdEstimated || "Estimated",
                                },
                                {
                                  label:
                                    T.dashboardYtdNetPay || "Net pay (period)",
                                  value:
                                    ed.deductions > 0
                                      ? `${sym}${fmt(ed.net)}`
                                      : "—",
                                  note:
                                    ed.deductions > 0
                                      ? T.dashboardYtdAfterDeductions ||
                                        "After deductions"
                                      : T.dashboardYtdDeductionsPending ||
                                        "Deductions pending",
                                },
                              ].map((item) => (
                                <div
                                  key={item.label}
                                  style={{
                                    padding: "12px 14px",
                                    background: C.bg,
                                    borderRadius: 10,
                                    border: `1px solid ${C.border}`,
                                  }}
                                >
                                  <p
                                    style={{
                                      fontSize: 11,
                                      color: C.textSub,
                                      marginBottom: 4,
                                      lineHeight: 1.3,
                                    }}
                                  >
                                    {item.label}
                                  </p>
                                  <p
                                    style={{
                                      fontSize: 18,
                                      fontWeight: 800,
                                      color: C.text,
                                    }}
                                  >
                                    {item.value}
                                  </p>
                                  <p
                                    style={{
                                      fontSize: 10,
                                      color: C.textSub,
                                      marginTop: 2,
                                    }}
                                  >
                                    {item.note}
                                  </p>
                                </div>
                              ))}
                            </div>
                          </div>
                        </div>
                      )}

                      {/* ── Ask a question modal ── */}
                      {earningsQOpen && (
                        <div
                          onClick={() => setEarningsQOpen(false)}
                          style={{
                            position: "fixed",
                            inset: 0,
                            background: "rgba(0,0,0,0.45)",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            zIndex: 9999,
                            padding: 20,
                          }}
                        >
                          <div
                            onClick={(e) => e.stopPropagation()}
                            style={{
                              width: "100%",
                              maxWidth: 480,
                              background: C.surface,
                              borderRadius: 16,
                              border: `1px solid ${C.border}`,
                              padding: 28,
                              boxShadow: "0 20px 60px rgba(0,0,0,0.3)",
                            }}
                          >
                            {earningsQSent ? (
                              <div
                                style={{
                                  textAlign: "center",
                                  padding: "20px 0",
                                }}
                              >
                                <div
                                  style={{
                                    width: 52,
                                    height: 52,
                                    borderRadius: "50%",
                                    background: `${C.green}15`,
                                    border: `1.5px solid ${C.green}`,
                                    display: "flex",
                                    alignItems: "center",
                                    justifyContent: "center",
                                    margin: "0 auto 14px",
                                  }}
                                >
                                  <Ico n="check" s={22} c={C.green} fw="3" />
                                </div>
                                <p
                                  style={{
                                    fontSize: 16,
                                    fontWeight: 800,
                                    color: C.text,
                                    marginBottom: 6,
                                  }}
                                >
                                  Question submitted
                                </p>
                                <p style={{ fontSize: 13, color: C.textSub }}>
                                  Your question has been routed to HR. You'll
                                  receive a response within 1 business day.
                                </p>
                              </div>
                            ) : (
                              <>
                                <div
                                  style={{
                                    display: "flex",
                                    alignItems: "center",
                                    justifyContent: "space-between",
                                    marginBottom: 20,
                                  }}
                                >
                                  <p
                                    style={{
                                      fontSize: 16,
                                      fontWeight: 800,
                                      color: C.text,
                                    }}
                                  >
                                    {T.dashboardAskAboutEstimate ||
                                      "Ask a question about this estimate"}
                                  </p>
                                  <button
                                    onClick={() => setEarningsQOpen(false)}
                                    style={{
                                      width: 30,
                                      height: 30,
                                      borderRadius: 8,
                                      border: `1px solid ${C.border}`,
                                      background: "transparent",
                                      cursor: "pointer",
                                      display: "flex",
                                      alignItems: "center",
                                      justifyContent: "center",
                                    }}
                                  >
                                    <Ico n="x" s={13} c={C.textSub} />
                                  </button>
                                </div>
                                <div style={{ marginBottom: 16 }}>
                                  <p
                                    style={{
                                      fontSize: 12,
                                      fontWeight: 600,
                                      color: C.text,
                                      marginBottom: 8,
                                    }}
                                  >
                                    {T.dashboardWhatIsThisAbout ||
                                      "What is this about?"}
                                  </p>
                                  <div
                                    style={{
                                      display: "flex",
                                      flexDirection: "column",
                                      gap: 8,
                                    }}
                                  >
                                    {[
                                      {
                                        k: "missing_hours",
                                        l: "Missing or incorrect hours",
                                      },
                                      { k: "pay_rate", l: "Pay rate question" },
                                      {
                                        k: "overtime",
                                        l: "Overtime calculation",
                                      },
                                      {
                                        k: "adjustment",
                                        l: "Adjustment, bonus or reimbursement",
                                      },
                                      {
                                        k: "leave",
                                        l: "Leave or paid time off",
                                      },
                                      { k: "other", l: "Something else" },
                                    ].map((opt) => (
                                      <label
                                        key={opt.k}
                                        style={{
                                          display: "flex",
                                          alignItems: "center",
                                          gap: 8,
                                          cursor: "pointer",
                                          padding: "8px 12px",
                                          borderRadius: 8,
                                          border: `1px solid ${earningsQCategory === opt.k ? C.brand : C.border}`,
                                          background:
                                            earningsQCategory === opt.k
                                              ? `${C.brand}10`
                                              : "transparent",
                                          transition: "all .15s",
                                        }}
                                      >
                                        <input
                                          type="radio"
                                          name="eq-cat"
                                          value={opt.k}
                                          checked={earningsQCategory === opt.k}
                                          onChange={() =>
                                            setEarningsQCategory(opt.k)
                                          }
                                          style={{ accentColor: C.brand }}
                                        />
                                        <span
                                          style={{
                                            fontSize: 13,
                                            color: C.text,
                                          }}
                                        >
                                          {opt.l}
                                        </span>
                                      </label>
                                    ))}
                                  </div>
                                </div>
                                <div style={{ marginBottom: 14 }}>
                                  <p
                                    style={{
                                      fontSize: 12,
                                      fontWeight: 600,
                                      color: C.text,
                                      marginBottom: 6,
                                    }}
                                  >
                                    {T.dashboardWhatIsTheIssue ||
                                      "What is the issue?"}
                                  </p>
                                  <textarea
                                    value={earningsQText}
                                    onChange={(e) =>
                                      setEarningsQText(e.target.value)
                                    }
                                    rows={3}
                                    placeholder={
                                      T.dashboardIssuePlaceholder ||
                                      "Describe the issue in your own words…"
                                    }
                                    style={{
                                      width: "100%",
                                      padding: "10px 12px",
                                      borderRadius: 8,
                                      border: `1px solid ${C.border}`,
                                      background: C.bg,
                                      color: C.text,
                                      fontSize: 13,
                                      outline: "none",
                                      fontFamily: "inherit",
                                      resize: "vertical",
                                      boxSizing: "border-box",
                                    }}
                                  />
                                </div>
                                <div style={{ marginBottom: 20 }}>
                                  <p
                                    style={{
                                      fontSize: 12,
                                      fontWeight: 600,
                                      color: C.text,
                                      marginBottom: 6,
                                    }}
                                  >
                                    {T.dashboardWhenDidThisHappen ||
                                      "When did this happen?"}{" "}
                                    <span
                                      style={{
                                        fontWeight: 400,
                                        color: C.textSub,
                                      }}
                                    >
                                      {T.dashboardOptional || "(optional)"}
                                    </span>
                                  </p>
                                  <input
                                    type="date"
                                    value={earningsQDate}
                                    onChange={(e) =>
                                      setEarningsQDate(e.target.value)
                                    }
                                    style={{
                                      padding: "9px 12px",
                                      borderRadius: 8,
                                      border: `1px solid ${C.border}`,
                                      background: C.bg,
                                      color: C.text,
                                      fontSize: 13,
                                      outline: "none",
                                      fontFamily: "inherit",
                                      width: "100%",
                                      boxSizing: "border-box",
                                    }}
                                  />
                                </div>
                                <div
                                  style={{
                                    display: "flex",
                                    gap: 10,
                                    justifyContent: "flex-end",
                                  }}
                                >
                                  <button
                                    onClick={() => setEarningsQOpen(false)}
                                    style={{
                                      padding: "9px 18px",
                                      borderRadius: 8,
                                      border: `1px solid ${C.border}`,
                                      background: "transparent",
                                      color: C.text,
                                      fontSize: 13,
                                      fontWeight: 600,
                                      cursor: "pointer",
                                      fontFamily: "inherit",
                                    }}
                                  >
                                    {T.dashboardCancel || "Cancel"}
                                  </button>
                                  <button
                                    onClick={submitQuestion}
                                    disabled={
                                      !earningsQText.trim() ||
                                      earningsQSubmitting
                                    }
                                    style={{
                                      padding: "9px 20px",
                                      borderRadius: 8,
                                      border: "none",
                                      background: earningsQText.trim()
                                        ? C.brand
                                        : C.border,
                                      color: "#fff",
                                      fontSize: 13,
                                      fontWeight: 700,
                                      cursor: earningsQText.trim()
                                        ? "pointer"
                                        : "default",
                                      fontFamily: "inherit",
                                      transition: "background .15s",
                                    }}
                                  >
                                    {earningsQSubmitting
                                      ? T.dashboardSending || "Sending…"
                                      : T.dashboardSendQuestion ||
                                        "Send question"}
                                  </button>
                                </div>
                              </>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })()}
            </main>

            {/* Status badge — removed: duplicated the floating timer pill at bottom-right */}
          </div>
        </div>
      )}

      {/* Break-type picker — short vs long break. Opens from "Take a Break"
          buttons; the chosen type drives the overtime-alert threshold. */}
      {showBreakPicker && (
        <div
          onClick={() => setShowBreakPicker(false)}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 10000,
            background: "rgba(0,0,0,0.5)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 20,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              width: "100%",
              maxWidth: 440,
              background: C.surface,
              borderRadius: 16,
              border: `1px solid ${C.border}`,
              padding: 28,
              boxShadow: "0 24px 60px rgba(0,0,0,0.3)",
              animation: "popIn .2s ease",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                marginBottom: 6,
              }}
            >
              <p style={{ fontSize: 16, fontWeight: 800, color: C.text }}>
                {T.startBreakTitle || "Start a Break"}
              </p>
              <button
                onClick={() => setShowBreakPicker(false)}
                style={{
                  width: 30,
                  height: 30,
                  borderRadius: 8,
                  border: `1px solid ${C.border}`,
                  background: "transparent",
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <Ico n="x" s={13} c={C.textSub} />
              </button>
            </div>
            <p
              style={{
                fontSize: 12.5,
                color: C.textSub,
                marginBottom: 18,
                lineHeight: 1.5,
              }}
            >
              {T.startBreakHint ||
                `Pick the break type so we can warn you if it runs over (${breakThresholds.short_minutes}m for a short break, ${breakThresholds.lunch_minutes}m for a long break).`}
            </p>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: lunchAllowed ? "1fr 1fr" : "1fr",
                gap: 12,
                marginBottom: 16,
              }}
            >
              <button
                onClick={() => {
                  setShowBreakPicker(false);
                  handleBreakToggle("break");
                }}
                style={{
                  padding: "16px 12px",
                  borderRadius: 12,
                  border: `1.5px solid ${isDark ? "#92400E" : "#FCD34D"}`,
                  background: isDark ? "rgba(251,191,36,0.1)" : "#FFFBEB",
                  color: isDark ? "#FCD34D" : "#B45309",
                  fontWeight: 700,
                  fontSize: 13.5,
                  cursor: "pointer",
                  fontFamily: "inherit",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: 8,
                  transition: "transform .15s",
                }}
              >
                <Ico n="clock" s={22} c={isDark ? "#FCD34D" : "#B45309"} />
                {T.shortBreak || "Short Break"}
                <span
                  style={{ fontSize: 11, fontWeight: 600, color: C.textSub }}
                >
                  {breakThresholds.short_minutes}m limit
                </span>
              </button>
              {lunchAllowed && (
                <button
                  onClick={() => {
                    setShowBreakPicker(false);
                    handleBreakToggle("lunch");
                  }}
                  style={{
                    padding: "16px 12px",
                    borderRadius: 12,
                    border: `1.5px solid ${C.brand}`,
                    background: `${C.brand}15`,
                    color: C.brand,
                    fontWeight: 700,
                    fontSize: 13.5,
                    cursor: "pointer",
                    fontFamily: "inherit",
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    gap: 8,
                    transition: "transform .15s",
                  }}
                >
                  <span style={{ fontSize: 22 }}>🍱</span>
                  {T.longBreak || "Long Break"}
                  <span
                    style={{ fontSize: 11, fontWeight: 600, color: C.textSub }}
                  >
                    {breakThresholds.lunch_minutes}m limit
                  </span>
                </button>
              )}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button
                onClick={() => setShowBreakPicker(false)}
                style={{
                  padding: "9px 18px",
                  borderRadius: 8,
                  border: `1px solid ${C.border}`,
                  background: "transparent",
                  color: C.text,
                  fontSize: 13,
                  fontWeight: 600,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                {T.cancel || "Cancel"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Break-overrun blocking modal — worker-facing alert when a break runs
          past its allowance. Center screen, no backdrop dismiss; the worker
          must choose Resume Work or Later. Admin + HR are already notified by
          the server-side break-overrun monitor. */}
      {/* TEMPORARILY DISABLED: Early Clock Out is inactive — the modal is never
          opened. Kept for future re-enable (EARLY_CLOCK_OUT_ENABLED). */}
      {EARLY_CLOCK_OUT_ENABLED && earlyClockOut && (
        <EarlyClockOutModal
          reason={earlyReason}
          onReasonChange={(v) => {
            setEarlyReason(v);
            if (earlyError) setEarlyError("");
          }}
          error={earlyError}
          submitting={earlySubmitting}
          onCancel={cancelEarlyClockOut}
          onSubmit={submitEarlyClockOut}
        />
      )}

      {showOverrunModal && overrunInfo && (
        <div
          role="alertdialog"
          aria-modal="true"
          aria-labelledby="zt-overrun-title"
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 10001,
            background: "rgba(0,0,0,0.55)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 20,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              width: "100%",
              maxWidth: 420,
              background: C.surface,
              borderRadius: 16,
              border: `1px solid ${isDark ? "rgba(239,68,68,0.5)" : "#FCA5A5"}`,
              padding: 26,
              boxShadow: "0 24px 60px rgba(0,0,0,0.3)",
              animation: "popIn .2s ease",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: 12,
                marginBottom: 14,
              }}
            >
              <div
                style={{
                  flexShrink: 0,
                  width: 40,
                  height: 40,
                  borderRadius: 12,
                  background: isDark ? "rgba(239,68,68,0.16)" : "#FEE2E2",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 20,
                }}
              >
                ⚠️
              </div>
              <div>
                <p
                  id="zt-overrun-title"
                  style={{
                    fontSize: 16,
                    fontWeight: 800,
                    color: C.text,
                    margin: "0 0 4px",
                  }}
                >
                  {overrunInfo.break_type === "lunch"
                    ? T.lunchBreakExceeded || "Lunch Break Exceeded"
                    : T.breakTimeExceeded || "Break Time Exceeded"}
                </p>
                <p
                  style={{
                    fontSize: 12.5,
                    color: C.textSub,
                    margin: 0,
                    lineHeight: 1.5,
                  }}
                >
                  You've been on{" "}
                  {overrunInfo.break_type === "lunch" ? "lunch" : "your break"}{" "}
                  for {Math.floor((overrunInfo.elapsed_seconds || 0) / 60)}m
                  (limit {overrunInfo.allowed_minutes}m). Please resume work now
                  — your admin and HR have been notified.
                </p>
              </div>
            </div>
            <div
              style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}
            >
              <button
                onClick={snoozeOverrun}
                style={{
                  padding: "9px 18px",
                  borderRadius: 8,
                  border: `1px solid ${C.border}`,
                  background: "transparent",
                  color: C.text,
                  fontSize: 13,
                  fontWeight: 600,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                {T.later || "Later"}
              </button>
              {/* Resume Work ends a break — an attendance action, so it is
                  desktop-app only. The overrun warning and "Later" still show;
                  the worker resumes from the desktop app. */}
              {attendanceActionsAllowed && (
              <button
                onClick={resumeFromOverrun}
                style={{
                  padding: "9px 20px",
                  borderRadius: 8,
                  border: "none",
                  background: `linear-gradient(135deg,${C.brand},#44B97B)`,
                  color: "#fff",
                  fontSize: 13,
                  fontWeight: 700,
                  cursor: "pointer",
                  fontFamily: "inherit",
                  boxShadow: "0 4px 12px rgba(17,73,106,.3)",
                }}
              >
                {T.resumeWork || "Resume Work"}
              </button>
              )}
            </div>
          </div>
        </div>
      )}

      <FloatingAssistantBot bottom={24} />
    </>
  );
}
